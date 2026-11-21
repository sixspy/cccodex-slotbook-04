"""档期锁与争用登记。

口径（2026-11-21 锁定）：写路径一律先抢档锁，抢到后用同一连接、同一事务
完成业务写；失败整笔回滚，后到只记争用痕，不写业务。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import connect, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str


def record_contention(conn, lock_key: str, actor: str, detail: str) -> None:
    """争用痕单独落库；调用方须自行 commit。"""
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (
            lock_key,
            actor,
            "争用",
            detail,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    """原子抢锁：INSERT OR IGNORE 判定归属，避免先查后插的竞态。"""
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT OR IGNORE INTO slot_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now),
    )
    if cur.rowcount == 1:
        return True
    row = conn.execute(
        "SELECT holder FROM slot_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is not None and row["holder"] == holder:
        return True
    record_contention(
        conn, lock_key, holder, f"持有者={row['holder'] if row else '?'}"
    )
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM slot_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


@contextmanager
def slot_lock(conn, lock_key: str, holder: str):
    """持档锁的同事务写窗。

    三段各自成事务，互不夹缠：
      1) 抢锁单独提交——其他人立刻能看到持有者，撞上就留争用痕；
      2) 业务写在 with 块内，成功一起提交、异常整笔回滚；
      3) 退出时释放档锁并提交，失败也不留死锁。

    调用方不得在 with 块内自行 commit/rollback，也不得另开连接写同一档。
    """
    # 1) 抢锁：单独提交，保证争用可见、可留痕
    if not try_acquire(conn, lock_key, holder):
        conn.commit()
        raise HTTPException(409, "争用：后到只记争用，不写业务")
    conn.commit()

    ok = False
    try:
        yield
        conn.commit()
        ok = True
    finally:
        # 2b) 失败整笔回滚
        if not ok and conn.in_transaction:
            conn.rollback()
        # 3) 释放档锁：用新事务，避免把回滚掉的内容又带出去
        release(conn, lock_key, holder)
        conn.commit()


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM slot_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire" if ok else "lock_contention",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：后到只记争用")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
    return {"ok": True}
