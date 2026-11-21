"""候补队列：列表、取消、晋升（同册）。

晋升口径见 docs/晋升同册-2026-11-21.md：只走甲路径——持档锁后在同一事务里
插占位并把候补改为已晋升；任一笔失败两边同滚。禁止草稿/影子表等乙路径混用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import slot_lock
from reconcile_svc import promotion_state

router = APIRouter(tags=["waitlist"])

# 甲路径标识：库内容与代码约定不一致即判偏航
SAME_LEDGER_PATH = "甲:持档锁同事务"
FORBIDDEN_TABLES = ("promotion_drafts", "promotion_shadow")


class CancelIn(BaseModel):
    wait_id: int


class PromoteIn(BaseModel):
    wait_id: int


def guard_same_ledger_path(conn, actor: str) -> None:
    """晋升前偏航检查；命中即记偏航审计并拒写。"""
    if SAME_LEDGER_PATH != "甲:持档锁同事务":
        write_audit(
            conn, actor, "promotion_path_drift",
            "waitlist_promote", {"reason": "路径标识非甲", "path": SAME_LEDGER_PATH},
        )
        conn.commit()
        raise HTTPException(409, "晋升同册偏航：路径标识非甲，拒绝写入")
    placeholders = ",".join("?" for _ in FORBIDDEN_TABLES)
    rows = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
        FORBIDDEN_TABLES,
    ).fetchall()
    if rows:
        names = [r["name"] for r in rows]
        write_audit(
            conn, actor, "promotion_path_drift",
            "waitlist_promote", {"reason": "出现草稿/影子表", "tables": names},
        )
        conn.commit()
        raise HTTPException(409, "晋升同册偏航：检测到乙路径草稿表，拒绝写入")


@router.get("/api/waitlist")
def list_wait(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT w.*, s.code AS slot_code FROM waitlist w
               JOIN slots s ON s.id=w.slot_id ORDER BY w.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/waitlist/cancel")
def cancel_wait(body: CancelIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM waitlist WHERE id=?", (body.wait_id,)).fetchone()
        if not row:
            raise HTTPException(404, "候补不存在")
        if row["status"] != "候补中":
            raise HTTPException(409, "候补状态不可取消")
        conn.execute(
            "UPDATE waitlist SET status='已取消' WHERE id=?", (body.wait_id,)
        )
        write_audit(conn, user, "waitlist_cancel", str(body.wait_id), {})
        conn.commit()
    return {"ok": True}


@router.post("/api/waitlist/promote")
def promote_wait(body: PromoteIn, authorization: AuthHeader = None):
    """有空位时把候补转成占位（仍须再确认才入正式册）。

    甲路径：持档锁 → 同一事务插占位 + 改候补已晋升 → 同成败。
    """
    user = require_writer(authorization)
    with connect() as conn:
        guard_same_ledger_path(conn, user)
        wrow = conn.execute("SELECT * FROM waitlist WHERE id=?", (body.wait_id,)).fetchone()
        if not wrow:
            raise HTTPException(404, "候补不存在")
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (wrow["slot_id"],)).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"

        with slot_lock(conn, lock_key, user):
            # 持锁后重读：等锁期间状态可能已被别人改掉
            wrow = conn.execute(
                "SELECT * FROM waitlist WHERE id=?", (body.wait_id,)
            ).fetchone()
            if wrow["status"] != "候补中":
                raise HTTPException(409, "候补非候补中，禁止晋升")
            slot = conn.execute(
                "SELECT * FROM slots WHERE id=?", (wrow["slot_id"],)
            ).fetchone()
            if slot["status"] != "开放":
                raise HTTPException(409, "档期未开放，禁止晋升")

            pending = conn.execute(
                "SELECT COALESCE(SUM(seats),0) s FROM holds WHERE slot_id=? AND status='占位中'",
                (slot["id"],),
            ).fetchone()["s"]
            free = int(slot["capacity"]) - int(slot["booked"]) - int(pending)
            if wrow["seats"] > free:
                raise HTTPException(409, "仍无足够空位，禁止晋升")

            now = datetime.now().isoformat(timespec="seconds")
            expire = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
            # 两笔业务写：同一事务，同成败
            try:
                cur = conn.execute(
                    """INSERT INTO holds(slot_id,party_key,seats,status,expire_at,created_at,wait_id)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        slot["id"],
                        wrow["party_key"],
                        wrow["seats"],
                        "占位中",
                        expire,
                        now,
                        wrow["id"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # ux_holds_wait_id 兜底：同一候补已有占位，整笔不落
                raise HTTPException(409, "该候补已晋升过占位，禁止重复晋升") from exc
            conn.execute(
                """UPDATE waitlist SET status='已晋升' WHERE id=? AND status='候补中'""",
                (body.wait_id,),
            )
            write_audit(
                conn,
                user,
                "waitlist_promote",
                wrow["party_key"],
                {
                    "wait_id": body.wait_id,
                    "hold_id": cur.lastrowid,
                    "slot": slot["code"],
                    "path": SAME_LEDGER_PATH,
                },
            )
            hold_id = cur.lastrowid

        # 出锁后同态回读：五处口径一并返回，便于前端与对账互证
        state = promotion_state(conn, slot["code"])
        return {"ok": True, "hold_id": hold_id, "state": state}
