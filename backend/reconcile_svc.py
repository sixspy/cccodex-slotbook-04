"""对账：档期 booked、正式册、占位、候补晋升、审计五处同态校验。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def promotion_state(conn, code: str) -> dict:
    """某档期的晋升五处同态快照。

    候补态 / 占位行 / 余席 / 对账 / 审计：
    - promoted_waits：该档已晋升候补数；
    - promotion_holds：带 wait_id 的占位行数；
    - dual_state：仍处于“候补中却有占位行”的错位条数（必须为 0）；
    - free_seats：按容量-已订-占位中算出的余席；
    - audit_ok：每次带 wait_id 的占位都有对应 waitlist_promote 审计。
    """
    slot = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
    if not slot:
        raise HTTPException(404, "档期不存在")
    promoted_waits = conn.execute(
        "SELECT COUNT(*) c FROM waitlist WHERE slot_id=? AND status='已晋升'",
        (slot["id"],),
    ).fetchone()["c"]
    promotion_holds = conn.execute(
        "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND wait_id IS NOT NULL",
        (slot["id"],),
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COALESCE(SUM(seats),0) s FROM holds WHERE slot_id=? AND status='占位中'",
        (slot["id"],),
    ).fetchone()["s"]
    dual_state = conn.execute(
        """SELECT COUNT(*) c FROM waitlist w
           WHERE w.slot_id=? AND w.status='候补中'
             AND EXISTS (SELECT 1 FROM holds h WHERE h.wait_id=w.id)""",
        (slot["id"],),
    ).fetchone()["c"]
    promoted_ids = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM waitlist WHERE slot_id=? AND status='已晋升'", (slot["id"],)
        ).fetchall()
    }
    hold_wait_ids = {
        r["wait_id"]
        for r in conn.execute(
            "SELECT wait_id FROM holds WHERE slot_id=? AND wait_id IS NOT NULL",
            (slot["id"],),
        ).fetchall()
    }
    # 审计按 wait_id 精确对：每次带 wait_id 的占位都要有对应晋升审计
    audited_wait_ids = set()
    for r in conn.execute(
        """SELECT payload FROM audit_trail
           WHERE action='waitlist_promote' AND target IN (
             SELECT party_key FROM waitlist WHERE slot_id=?
           )""",
        (slot["id"],),
    ).fetchall():
        try:
            wid = json.loads(r["payload"] or "{}").get("wait_id")
        except (ValueError, TypeError):
            continue
        if isinstance(wid, int):
            audited_wait_ids.add(wid)
    audit_ok = (hold_wait_ids - {None}) <= audited_wait_ids
    free_seats = int(slot["capacity"]) - int(slot["booked"]) - int(pending)
    same_ledger = (
        promoted_ids == hold_wait_ids and dual_state == 0 and audit_ok
    )
    return {
        "slot_code": code,
        "promoted_waits": promoted_waits,
        "promotion_holds": promotion_holds,
        "dual_state": dual_state,
        "free_seats": free_seats,
        "audit_ok": audit_ok,
        "same_ledger": same_ledger,
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        slots = conn.execute("SELECT * FROM slots ORDER BY code").fetchall()
        items = []
        for s in slots:
            booked_sum = conn.execute(
                """SELECT COALESCE(SUM(seats),0) s FROM bookings
                   WHERE slot_id=? AND status='已确认'""",
                (s["id"],),
            ).fetchone()["s"]
            promo = promotion_state(conn, s["code"])
            ok = int(booked_sum) == int(s["booked"]) and promo["same_ledger"]
            items.append(
                {
                    "slot_code": s["code"],
                    "slot_booked": s["booked"],
                    "booking_seats": booked_sum,
                    "generation": s["generation"],
                    "ok": ok,
                    "status": s["status"],
                    "promoted_waits": promo["promoted_waits"],
                    "promotion_holds": promo["promotion_holds"],
                    "dual_state": promo["dual_state"],
                    "free_seats": promo["free_seats"],
                    "audit_ok": promo["audit_ok"],
                    "same_ledger": promo["same_ledger"],
                }
            )
        return {"items": items, "all_ok": all(i["ok"] for i in items)}


@router.get("/api/reconcile/{code}")
def reconcile_one(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        s = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not s:
            raise HTTPException(404, "档期不存在")
        booked_sum = conn.execute(
            """SELECT COALESCE(SUM(seats),0) s FROM bookings
               WHERE slot_id=? AND status='已确认'""",
            (s["id"],),
        ).fetchone()["s"]
        holds = conn.execute(
            "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
            (s["id"],),
        ).fetchone()["c"]
        promo = promotion_state(conn, code)
        return {
            "slot_code": code,
            "slot_booked": s["booked"],
            "booking_seats": booked_sum,
            "open_holds": holds,
            "ok": int(booked_sum) == int(s["booked"]),
            "generation": s["generation"],
            **promo,
        }
