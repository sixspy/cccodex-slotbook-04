"""候补晋升同册：夹具先红，再修服务。

口径（2026-11-21 当日锁死，不混用）：
  甲路径——持有档期锁后，在同一事务里插占位并改候补为已晋升；
  任一笔失败，两边同滚，占位与候补态零增。
  禁止只在前端防连点；两人同晋同一候补必须抢档锁，后到只留争用痕。

本文件所有夹具跑本机文件库（tmp_path 下的真实 SQLite 文件），不用 :memory:。

修复前现场（红）：
  1) 晋升第二笔写失败时，finally 仍 commit，占位行被提交而候补态停在“候补中”，
     盘面出现“占位 + 仍在排队”的双胞胎；
  2) 两人同晋同一候补时两条都返回 200，会插出两个占位。
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
import db as dbmod  # noqa: E402
import locks  # noqa: E402
import waitlist_svc  # noqa: E402
from db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """本机文件库夹具：每个用例一个全新的 app.db 文件。"""
    db = tmp_path / "promote.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield db


def _login(c: TestClient, user="admin", password="123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _db_snapshot(path: Path) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "waitlist": [dict(r) for r in conn.execute("SELECT * FROM waitlist ORDER BY id")],
            "holds": [dict(r) for r in conn.execute("SELECT * FROM holds ORDER BY id")],
            "slots": [dict(r) for r in conn.execute("SELECT * FROM slots ORDER BY id")],
            "locks": [dict(r) for r in conn.execute("SELECT * FROM slot_locks ORDER BY lock_key")],
            "contention": [dict(r) for r in conn.execute("SELECT * FROM contention_log ORDER BY id")],
            "audit": [dict(r) for r in conn.execute("SELECT * FROM audit_trail ORDER BY id")],
        }
    finally:
        conn.close()


class _FaultConn:
    """在“改候补已晋升”这一笔写上注入失败，模拟晋升半成功。"""

    def __init__(self, real: sqlite3.Connection):
        self._real = real
        self.faulted = False

    def execute(self, sql, params=()):
        if not self.faulted and "UPDATE waitlist" in sql and "已晋升" in sql:
            self.faulted = True
            raise sqlite3.OperationalError("injected: waitlist promote update failed")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


@pytest.fixture
def promote_with_fault(monkeypatch):
    """晋升假绿夹具：晋升第二笔写坏掉，返回 (响应状态, 库内快照)。

    修复前用它能造出“占位插出来了、候补态仍显示排队”的假绿现场；
    修复后同一夹具下必须两边零增。
    """

    def run():
        real_connect = waitlist_svc.connect

        def fault_connect():
            return _FaultConn(real_connect())

        monkeypatch.setattr(waitlist_svc, "connect", fault_connect)
        with TestClient(app, raise_server_exceptions=False) as c:
            h = _login(c)
            r = c.post("/api/waitlist/promote", headers=h, json={"wait_id": 1})
        return r.status_code, _db_snapshot(dbmod.DB_PATH)

    return run


def test_promote_must_not_leave_dual_state(promote_with_fault):
    """双态并存失败测：晋升要么占位+候补态同成，要么两边零增。"""
    status, snap = promote_with_fault()
    assert status >= 500, f"注入故障应报错，实得 {status}"
    hold = [h for h in snap["holds"] if h["party_key"] == "P-WAIT"]
    wait = [w for w in snap["waitlist"] if w["id"] == 1][0]
    assert hold == [], f"晋升第二笔写失败仍留下占位行：{hold}"
    assert wait["status"] == "候补中", f"候补态被半改：{wait}"
    # 无成功晋升审计；档锁已释放，不留死锁
    assert not [a for a in snap["audit"] if a["action"] == "waitlist_promote"]
    assert snap["locks"] == [], f"失败后档锁未释放：{snap['locks']}"
    with TestClient(app) as c:
        h = _login(c)
        recon = c.get("/api/reconcile", headers=h).json()
    assert recon["all_ok"] is True, recon


def test_promote_rejects_when_slot_lock_held(_fresh_db):
    """后到撞上他人持有的档锁：只留争用痕，不写业务。"""
    with TestClient(app) as c:
        h_clerk = _login(c, "clerk")
        h_admin = _login(c, "admin")
        r = c.post("/api/locks/acquire", headers=h_clerk, json={"lock_key": "slot:S-AM-01"})
        assert r.status_code == 200, r.text

        r = c.post("/api/waitlist/promote", headers=h_admin, json={"wait_id": 1})
        assert r.status_code == 409, r.text
        assert "争用" in r.json()["detail"]

    snap = _db_snapshot(_fresh_db)
    assert [h for h in snap["holds"] if h["party_key"] == "P-WAIT"] == []
    assert [w for w in snap["waitlist"] if w["id"] == 1][0]["status"] == "候补中"
    contention = [x for x in snap["contention"] if x["lock_key"] == "slot:S-AM-01"]
    assert any(x["actor"] == "admin" for x in contention), contention

    # 争用者退出后，原候补仍可正常晋升，且只进一占位
    with TestClient(app) as c:
        h_clerk = _login(c, "clerk")
        h_admin = _login(c, "admin")
        c.post("/api/locks/release", headers=h_clerk, json={"lock_key": "slot:S-AM-01"})
        r = c.post("/api/waitlist/promote", headers=h_admin, json={"wait_id": 1})
        assert r.status_code == 200, r.text

    snap = _db_snapshot(_fresh_db)
    assert len([h for h in snap["holds"] if h["party_key"] == "P-WAIT"]) == 1
    assert [w for w in snap["waitlist"] if w["id"] == 1][0]["status"] == "已晋升"


def test_try_acquire_is_atomic_under_contention(monkeypatch, _fresh_db):
    """两人同一瞬间抢同一档锁：只能有一个拿到。"""
    barrier = threading.Barrier(2, timeout=10)
    results: list[tuple[str, object]] = []

    def worker(holder: str):
        conn = dbmod.connect()
        try:
            barrier.wait()
            try:
                got = locks.try_acquire(conn, "slot:S-AM-01", holder)
            except Exception as exc:  # noqa: BLE001 - 夹具要看见基线抛错
                got = f"ERR:{type(exc).__name__}"
            results.append((holder, got))
            conn.commit()
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=("admin",)),
        threading.Thread(target=worker, args=("clerk",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert sorted(r[1] for r in results) == [False, True], f"抢锁结果应一真一假：{results}"


def test_two_people_promote_same_wait_only_one_hold(_fresh_db):
    """两人同时晋同一候补：只准一占位；后到不写业务。"""
    barrier = threading.Barrier(2, timeout=10)

    with TestClient(app) as c_admin, TestClient(app) as c_clerk:
        h_admin = _login(c_admin, "admin")
        h_clerk = _login(c_clerk, "clerk")
        results: list[tuple[str, int]] = []

        def fire(name, client, headers):
            barrier.wait()
            r = client.post("/api/waitlist/promote", headers=headers, json={"wait_id": 1})
            results.append((name, r.status_code))

        t1 = threading.Thread(target=fire, args=("admin", c_admin, h_admin))
        t2 = threading.Thread(target=fire, args=("clerk", c_clerk, h_clerk))
        t1.start()
        t2.start()
        t1.join(30)
        t2.join(30)

    codes = sorted(code for _, code in results)
    assert codes == [200, 409], f"应一成一败，实得 {results}"

    snap = _db_snapshot(_fresh_db)
    promoted = [h for h in snap["holds"] if h["party_key"] == "P-WAIT"]
    assert len(promoted) == 1, f"一候补只准进一占位，实得 {promoted}"
    assert promoted[0]["status"] == "占位中"
    wait = [w for w in snap["waitlist"] if w["id"] == 1][0]
    assert wait["status"] == "已晋升"
    # 若后到者撞在持锁窗口内，必须留争用痕
    losers = [n for n, code in results if code == 409]
    contention = [x for x in snap["contention"] if x["lock_key"] == "slot:S-AM-01"]
    if contention:
        assert any(any(n in (x["actor"] or "") for n in losers) for x in contention), contention


def test_repeated_promote_same_wait_still_one_hold(_fresh_db):
    """同一候补连点两次：第二次拒绝，占位仍只有一个。"""
    with TestClient(app) as c:
        h = _login(c)
        first = c.post("/api/waitlist/promote", headers=h, json={"wait_id": 1})
        assert first.status_code == 200, first.text
        second = c.post("/api/waitlist/promote", headers=h, json={"wait_id": 1})
        assert second.status_code == 409, second.text

    snap = _db_snapshot(_fresh_db)
    assert len([h for h in snap["holds"] if h["party_key"] == "P-WAIT"]) == 1
    assert len([a for a in snap["audit"] if a["action"] == "waitlist_promote"]) == 1
    with TestClient(app) as c:
        h = _login(c)
        recon = c.get("/api/reconcile/S-AM-01", headers=h).json()
    assert recon["ok"] is True, recon
    assert recon["promoted_waits"] == 1
    assert recon["promotion_holds"] == 1


def test_stale_prelock_read_cannot_double_promote(monkeypatch, _fresh_db):
    """确定性根因测：后到者不得沿用抢锁前的旧候补快照。

    构造“已读完候补、尚未持锁”的窗口：后到线程卡在抢锁前，先让别人完整晋升，
    再放行后到者。持锁后必须重读候补态；否则会插出第二个占位。
    """
    import waitlist_svc  # noqa: F401 - 确保服务已加载

    entered = threading.Event()
    proceed = threading.Event()
    real_try = locks.try_acquire
    first_call = {"done": False}
    guard = threading.Lock()

    def gated_try_acquire(*args, **kwargs):
        with guard:
            mine = not first_call["done"]
            first_call["done"] = True
        if mine:
            # 第一次抢锁属于“后到者”：卡在持锁前，暴露旧快照窗口
            entered.set()
            assert proceed.wait(10), "后到线程未被放行"
        return real_try(*args, **kwargs)

    monkeypatch.setattr(locks, "try_acquire", gated_try_acquire)

    results: list[tuple[int, str]] = []

    with TestClient(app) as c_early, TestClient(app) as c_later:
        h_early = _login(c_early, "admin")
        h_later = _login(c_later, "clerk")

        def later():
            r = c_later.post("/api/waitlist/promote", headers=h_later, json={"wait_id": 1})
            results.append((r.status_code, r.json().get("detail", "")))

        t = threading.Thread(target=later, name="later")
        t.start()
        assert entered.wait(10), "后到线程未进入抢锁点"

        # 先到者完整晋升并释放档锁
        first = c_early.post("/api/waitlist/promote", headers=h_early, json={"wait_id": 1})
        assert first.status_code == 200, first.text

        proceed.set()
        t.join(30)

    assert [code for code, _ in results] == [409], f"后到者应被已晋升态挡住，实得 {results}"
    # 必须是“持锁后重读”挡下的，而不是靠 wait_id 唯一索引兜底
    assert results[0][1] == "候补非候补中，禁止晋升", (
        f"后到者沿用了抢锁前旧快照，未重读候补态：{results}"
    )
    snap = _db_snapshot(_fresh_db)
    holds = [h for h in snap["holds"] if h["party_key"] == "P-WAIT"]
    assert len(holds) == 1, f"旧快照导致重复晋升：{holds}"
    assert snap["waitlist"][0]["status"] == "已晋升"


def test_hold_exposes_wait_id_for_frontend_trace(_fresh_db):
    """占位行必须带 wait_id，候补列表据此显示“对应占位”，双态才看得出。"""
    with TestClient(app) as c:
        h = _login(c)
        r = c.post("/api/waitlist/promote", headers=h, json={"wait_id": 1})
        assert r.status_code == 200, r.text
        hold_id = r.json()["hold_id"]

        holds = c.get("/api/holds", headers=h).json()["items"]
        promoted = [x for x in holds if x["id"] == hold_id][0]
        assert promoted["wait_id"] == 1, promoted

        waits = c.get("/api/waitlist", headers=h).json()["items"]
        assert waits[0]["id"] == 1 and waits[0]["status"] == "已晋升"


def test_drift_path_guard_rejects_and_audits(_fresh_db):
    """出现乙路径草稿表即判偏航：记审计、拒写、业务零增。"""
    conn = sqlite3.connect(_fresh_db)
    try:
        conn.execute("CREATE TABLE promotion_drafts(id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as c:
        h = _login(c)
        r = c.post("/api/waitlist/promote", headers=h, json={"wait_id": 1})
        assert r.status_code == 409, r.text
        assert "偏航" in r.json()["detail"]

    snap = _db_snapshot(_fresh_db)
    assert [x for x in snap["holds"] if x["party_key"] == "P-WAIT"] == []
    assert [w for w in snap["waitlist"] if w["id"] == 1][0]["status"] == "候补中"
    drift = [a for a in snap["audit"] if a["action"] == "promotion_path_drift"]
    assert drift, "偏航没有留审计"
    assert "promotion_drafts" in (drift[0]["payload"] or "")
