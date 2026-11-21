"""确认入册：占位 → 正式预订，写快照 generation。

与晋升同一套档锁语义：持档锁 → 同事务写正式册、改占位、加已订 → 同成败。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import slot_lock

router = APIRouter(tags=["confirm"])


class ConfirmIn(BaseModel):
    hold_id: int


@router.get("/api/bookings")
def list_bookings(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.*, s.code AS slot_code FROM bookings b
               JOIN slots s ON s.id=b.slot_id ORDER BY b.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/bookings/confirm")
def confirm_hold(body: ConfirmIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id=?", (body.hold_id,)).fetchone()
        if not hold:
            raise HTTPException(404, "占位不存在")
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (hold["slot_id"],)).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"

        expired = False
        booking_id = None
        snapshot_gen = None
        with slot_lock(conn, lock_key, user):
            # 持锁后重读，防止等锁期间被别人改掉
            hold = conn.execute("SELECT * FROM holds WHERE id=?", (body.hold_id,)).fetchone()
            slot = conn.execute(
                "SELECT * FROM slots WHERE id=?", (hold["slot_id"],)
            ).fetchone()
            if hold["status"] != "占位中":
                raise HTTPException(409, "占位已非占位中，禁止确认")
            if hold["expire_at"] < datetime.now().isoformat(timespec="seconds"):
                # 过期是有效状态变更：随本次事务提交，再在锁外报 409
                conn.execute("UPDATE holds SET status='已过期' WHERE id=?", (hold["id"],))
                write_audit(conn, user, "hold_expire", str(hold["id"]), {})
                expired = True
            else:
                remain = int(slot["capacity"]) - int(slot["booked"])
                if hold["seats"] > remain:
                    raise HTTPException(409, "确认时容量不足，请释放或改候补")
                now = datetime.now().isoformat(timespec="seconds")
                cur = conn.execute(
                    """INSERT INTO bookings(slot_id,hold_id,party_key,seats,snapshot_gen,status,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        slot["id"],
                        hold["id"],
                        hold["party_key"],
                        hold["seats"],
                        slot["generation"],
                        "已确认",
                        now,
                    ),
                )
                conn.execute("UPDATE holds SET status='已确认' WHERE id=?", (hold["id"],))
                conn.execute(
                    "UPDATE slots SET booked = booked + ? WHERE id=?",
                    (hold["seats"], slot["id"]),
                )
                write_audit(
                    conn,
                    user,
                    "booking_confirm",
                    hold["party_key"],
                    {"booking_id": cur.lastrowid, "gen": slot["generation"]},
                )
                booking_id = cur.lastrowid
                snapshot_gen = slot["generation"]

        if expired:
            raise HTTPException(409, "占位已过期")
        return {"ok": True, "booking_id": booking_id, "snapshot_gen": snapshot_gen}
