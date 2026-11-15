"""API 全链路自测（使用临时 SQLite 库）。"""
import os
import sqlite3
import tempfile
import threading
import time

import pytest

# 在导入应用前指定独立测试库
_TMPDIR = tempfile.mkdtemp(prefix="recon-test-")
os.environ["RECON_DB_PATH"] = os.path.join(_TMPDIR, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/reset")
        yield c


def load_demo(client):
    r = client.post("/api/demo")
    assert r.status_code == 200, r.text
    return r.json()


def snapshot(client):
    """取当前建议快照（含依据令牌）。"""
    r = client.get("/api/state")
    assert r.status_code == 200, r.text
    return r.json()


def confirm(client, items, basis=None, **kwargs):
    """带建议依据发起确认；缺省使用当前快照令牌。"""
    if basis is None:
        basis = snapshot(client)["suggestion_basis"]
    return client.post("/api/confirm", json={"items": items, "basis": basis}, **kwargs)


# ------------------------------------------------------------- 基础流程

def test_demo_load_and_suggestion_shape(client):
    data = load_demo(client)
    s = data["state"]["suggestion"]
    assert s["max_matched"] == 4
    certain_orders = {c["order"]["order_no"] for c in s["certain"]}
    assert certain_orders == {"PO20261001", "PO20261002", "PO20261003"}
    pending = {p["order"]["order_no"]: p["combos"] for p in s["pending"]}
    assert len(pending["PO20261004"]) == 2  # 两种 4000 组合
    assert s["unmatched"][0]["order_no"] == "PO20261005"
    assert any(r["receipt_no"] == "RC20261099" for r in s["unused_receipts"])


def test_state_counts_and_limits(client):
    load_demo(client)
    r = client.get("/api/state")
    s = r.json()["suggestion"]
    assert s["counts"] == {
        "open_orders": 5, "open_receipts": 11,
        "max_orders": 8, "max_receipts": 16,
    }
    # 再导入 4 张支付单 → 达到 9 张，超过 8 张上限
    payload = {"orders": [
        {"order_no": f"PX{i}", "employee_no": "E1", "amount": 100, "period": "2026-10"}
        for i in range(4)
    ]}
    r = client.post("/api/import/orders-json", json=payload)
    assert r.status_code == 400 and "上限" in r.json()["detail"]


def test_receipt_limit_16(client):
    load_demo(client)  # 11 条回执
    payload = {"receipts": [
        {"receipt_no": f"RN{i}", "employee_no": "E1", "amount": 100, "period": "2026-10"}
        for i in range(6)  # 11+6=17 > 16
    ]}
    r = client.post("/api/import/receipts-json", json=payload)
    assert r.status_code == 400 and "上限" in r.json()["detail"]


def test_validation_duplicate_and_amount(client):
    r = client.post("/api/import/orders-json", json={"orders": [
        {"order_no": "A1", "employee_no": "E1", "amount": 100},
        {"order_no": "A1", "employee_no": "E2", "amount": 200},
    ]})
    assert r.status_code == 400 and "重复" in r.json()["detail"]

    r = client.post("/api/import/orders-json", json={"orders": [
        {"order_no": "A2", "employee_no": "E1", "amount": 0},
    ]})
    assert r.status_code == 400 and "正整数" in r.json()["detail"]

    r = client.post("/api/import/receipts-json", json={"receipts": [
        {"receipt_no": "B1", "employee_no": "E1", "amount": -5},
    ]})
    assert r.status_code == 400


def test_unique_no_conflict_with_existing(client):
    load_demo(client)
    r = client.post("/api/import/receipts-json", json={"receipts": [
        {"receipt_no": "RC20261001", "employee_no": "E1", "amount": 100, "period": "2026-10"},
    ]})
    assert r.status_code == 409 and "冲突" in r.json()["detail"]


def test_period_must_match(client):
    load_demo(client)
    r = client.post("/api/import/orders-json", json={"orders": [
        {"order_no": "PM1", "employee_no": "E1", "amount": 100, "period": "2026-09"},
    ]})
    assert r.status_code == 400 and "月份不一致" in r.json()["detail"]


# ------------------------------------------------------------- 确认与依据

def test_confirm_certain_group_and_evidence(client):
    load_demo(client)
    items = [
        {"order_no": "PO20261001", "receipt_nos": ["RC20261001", "RC20261002"]},
        {"order_no": "PO20261002", "receipt_nos": ["RC20261003"]},
    ]
    r = confirm(client, items)
    assert r.status_code == 200, r.text
    cid = r.json()["confirmation_id"]

    state = client.get("/api/state").json()
    open_orders = {o["order_no"] for o in state["orders"] if o["status"] == "open"}
    open_receipts = {rc["receipt_no"] for rc in state["receipts"] if rc["status"] == "open"}
    assert "PO20261001" not in open_orders and "PO20261003" in open_orders
    assert "RC20261001" not in open_receipts and "RC20261004" in open_receipts
    assert state["confirmed"][0]["confirmation_id"] == cid

    r = client.get("/api/evidence.csv")
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "支付单号" in text and "PO20261001" in text and "RC20261002" in text


def test_confirm_rejects_reused_receipt_and_rolls_back(client):
    load_demo(client)
    # 先确认 PO...003（占用 RC...004/005/006）
    r = confirm(client, [
        {"order_no": "PO20261003", "receipt_nos": ["RC20261004", "RC20261005", "RC20261006"]},
    ])
    assert r.status_code == 200
    # 再尝试占用已使用回执 RC...004 → 整组失败（只带未确认订单，排除订单状态干扰）
    # RC...004(2000) + RC...007(1500) + RC...009(1000) = 4500 ≠ 4000，
    # 换成 RC...007+RC...008 本身合法，再额外拼入已占用的 RC...004 触发占用冲突
    r = confirm(client, [
        {"order_no": "PO20261002", "receipt_nos": ["RC20261003"]},
        {"order_no": "PO20261004", "receipt_nos": ["RC20261004", "RC20261007", "RC20261009"]},
    ])
    assert r.status_code == 409 and "已被占用" in r.json()["detail"]
    # 同组里合法的那一项也必须回滚：PO...002、PO...004 仍是 open
    state = client.get("/api/state").json()
    po2 = next(o for o in state["orders"] if o["order_no"] == "PO20261002")
    assert po2["status"] == "open"
    po4 = next(o for o in state["orders"] if o["order_no"] == "PO20261004")
    assert po4["status"] == "open"


def test_confirm_rejects_amount_mismatch(client):
    load_demo(client)
    r = confirm(client, [
        {"order_no": "PO20261001", "receipt_nos": ["RC20261001"]},  # 3000 != 7000
    ])
    assert r.status_code == 409 and "金额不平" in r.json()["detail"]


def test_manual_confirm_pending_combo(client):
    """待核组合允许人工选择确认。"""
    load_demo(client)
    r = confirm(client, [
        {"order_no": "PO20261004", "receipt_nos": ["RC20261007", "RC20261008"]},
    ])
    assert r.status_code == 200
    state = client.get("/api/state").json()
    used = {rc["receipt_no"] for rc in state["receipts"] if rc["status"] == "used"}
    assert {"RC20261007", "RC20261008"} <= used
    # 另一组待核回执仍开放，但 PO...004 已退出，建议中不再出现
    open_order_nos = {o["order_no"] for o in state["orders"] if o["status"] == "open"}
    assert "PO20261004" not in open_order_nos


# ------------------------------------------------------------- 补录重算

def test_supplement_recalculates_and_other_employee_unchanged(client):
    load_demo(client)
    before = client.get("/api/state").json()["suggestion"]
    before_certain = {c["order"]["order_no"]: [r["receipt_no"] for r in c["receipts"]]
                      for c in before["certain"]}

    r = client.post("/api/demo/supplement")
    assert r.status_code == 200
    after = client.get("/api/state").json()["suggestion"]
    after_certain = {c["order"]["order_no"]: [rc["receipt_no"] for rc in c["receipts"]]
                     for c in after["certain"]}

    # E005 新回执 → PO...005 进入确定配对，最大匹配数 +1
    assert after["max_matched"] == 5
    assert after_certain["PO20261005"] == ["RC20261011"]
    # 其他员工的确定配对保持不变
    for k, v in before_certain.items():
        assert after_certain[k] == v


def test_supplement_other_employee_keeps_confirmation(client):
    """补录其他员工回执不影响已确认依据。"""
    load_demo(client)
    confirm(client, [
        {"order_no": "PO20261002", "receipt_nos": ["RC20261003"]},
    ])
    client.post("/api/demo/supplement")
    state = client.get("/api/state").json()
    assert len(state["confirmed"]) == 1
    assert state["confirmed"][0]["items"][0]["order_no"] == "PO20261002"


# ------------------------------------------------------------- 持久化

def test_persistence_across_reconnect(client):
    load_demo(client)
    confirm(client, [
        {"order_no": "PO20261002", "receipt_nos": ["RC20261003"]},
    ])
    # 模拟重启：丢弃连接、重新建连接（lifespan 不会清库）
    conn = db.connect()
    try:
        orders = {r["order_no"]: r["status"] for r in db.fetch_all_orders(conn)}
        receipts = {r["receipt_no"]: r["status"] for r in db.fetch_all_receipts(conn)}
        assert orders["PO20261002"] == "confirmed"
        assert receipts["RC20261003"] == "used"
        assert db.fetch_evidence(conn)
    finally:
        conn.close()
    # 新连接下 API 仍可下载依据
    r = client.get("/api/evidence.csv")
    assert r.status_code == 200


# ------------------------------------------------------------- 并发确认

def test_concurrent_confirm_no_double_occupancy(client):
    """两个线程同时确认、争抢同一回执：只有一个成功，另一个整组失败、全不改。"""
    # K1/K2 同员工同额，R-a/R-b 各 100：
    # 两线程都尝试用 R-a 配平各自的订单（同一回执只能进一个事务）。
    # 失败线程附带独立订单 K3+R-c，用来证明失败组整组回滚。
    client.post("/api/import/orders-json", json={"orders": [
        {"order_no": "K1", "employee_no": "E1", "amount": 100, "period": "2026-10"},
        {"order_no": "K2", "employee_no": "E1", "amount": 100, "period": "2026-10"},
        {"order_no": "K3", "employee_no": "E2", "amount": 200, "period": "2026-10"},
    ]})
    client.post("/api/import/receipts-json", json={"receipts": [
        {"receipt_no": "R-a", "employee_no": "E1", "amount": 100, "period": "2026-10"},
        {"receipt_no": "R-b", "employee_no": "E1", "amount": 100, "period": "2026-10"},
        {"receipt_no": "R-c", "employee_no": "E2", "amount": 200, "period": "2026-10"},
    ]})
    barrier = threading.Barrier(2)
    results = []
    shared_basis = snapshot(client)["suggestion_basis"]

    def worker(payload):
        barrier.wait()
        r = client.post("/api/confirm", json={**payload, "basis": shared_basis})
        results.append((r.status_code, r.json()))

    t1 = threading.Thread(target=worker, args=({"items": [
        {"order_no": "K1", "receipt_nos": ["R-a"]},
    ]},))
    t2 = threading.Thread(target=worker, args=({"items": [
        {"order_no": "K2", "receipt_nos": ["R-a"]},
        {"order_no": "K3", "receipt_nos": ["R-c"]},
    ]},))
    t1.start(); t2.start(); t1.join(); t2.join()

    ok = [r for r in results if r[0] == 200]
    fail = [r for r in results if r[0] != 200]
    assert len(ok) == 1 and len(fail) == 1
    # 落败方无论是因建议过期还是回执被占用，都必须整组拒绝
    assert fail[0][1]["code"] == "stale_suggestion" or "已被占用" in fail[0][1]["detail"]

    state = client.get("/api/state").json()
    statuses = {o["order_no"]: o["status"] for o in state["orders"]}
    receipt_st = {rc["receipt_no"]: rc["status"] for rc in state["receipts"]}
    winner = ok[0][1]
    if winner["confirmed_items"] == 1:
        # t1 赢：仅 K1 落库；t2 整组回滚，K3/R-c 保持开放
        assert statuses == {"K1": "confirmed", "K2": "open", "K3": "open"}
        assert receipt_st["R-c"] == "open"
    else:
        # t2 赢：K2+K3 作为一个事务整体落库；K1 保持开放
        assert winner["confirmed_items"] == 2
        assert statuses == {"K1": "open", "K2": "confirmed", "K3": "confirmed"}
        assert receipt_st["R-c"] == "used"
    # 无论谁赢，R-a 只被使用一次、R-b 从未使用
    assert receipt_st["R-a"] == "used" and receipt_st["R-b"] == "open"
    # 回执唯一约束兜底：确认明细中每条回执仅一行
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT receipt_no, COUNT(*) c FROM confirmation_items GROUP BY receipt_no HAVING c > 1"
        ).fetchall()
        assert rows == []
        # 成功批次与其明细同生共死：失败线程不得留下任何残数据
        (n_conf,) = conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()
        (n_items,) = conn.execute("SELECT COUNT(*) FROM confirmation_items").fetchone()
        assert n_conf == 1 and n_items == winner["confirmed_items"]
    finally:
        conn.close()


# ------------------------------------------------------------- 过期建议拦截

def setup_stale_scenario(client):
    r = client.post("/api/demo/stale")
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    certain = {c["order"]["order_no"]: [rc["receipt_no"] for rc in c["receipts"]]
               for c in state["suggestion"]["certain"]}
    assert certain == {"PO7100": ["RC7100"], "PO8100": ["RC8100"]}
    return state["suggestion_basis"]


def test_stale_same_employee_supplement_rejects_old_page(client):
    old_basis = setup_stale_scenario(client)
    # 另一窗口同员工补录：金额仍相等、回执 RC7100 未被占用
    r = client.post("/api/demo/stale/supplement-same")
    assert r.status_code == 200

    # 旧页面的原配对仍能通过“金额相等/同员工/未占用”的静态校验，但必须因过期被拒
    r = confirm(client, [{"order_no": "PO7100", "receipt_nos": ["RC7100"]}], basis=old_basis)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "stale_suggestion"
    assert "刷新" in r.json()["detail"]

    state = snapshot(client)
    statuses = {o["order_no"]: o["status"] for o in state["orders"]}
    receipt_st = {rc["receipt_no"]: rc["status"] for rc in state["receipts"]}
    assert statuses == {"PO7100": "open", "PO8100": "open"}
    assert receipt_st["RC7100"] == "open" and receipt_st["RC7101"] == "open"
    assert state["confirmed"] == []


def test_stale_rejection_is_group_atomic_other_items_unchanged(client):
    old_basis = setup_stale_scenario(client)
    client.post("/api/demo/stale/supplement-same")
    # 一组里同时有合法项（其他员工）与过期项：整组不得写入
    r = confirm(client, [
        {"order_no": "PO8100", "receipt_nos": ["RC8100"]},
        {"order_no": "PO7100", "receipt_nos": ["RC7100"]},
    ], basis=old_basis)
    assert r.status_code == 409 and r.json()["code"] == "stale_suggestion"
    state = snapshot(client)
    assert {o["order_no"]: o["status"] for o in state["orders"]} == {
        "PO7100": "open", "PO8100": "open"
    }
    assert state["confirmed"] == []


def test_stale_other_employee_supplement_does_not_block(client):
    old_basis = setup_stale_scenario(client)
    # 另一窗口补录的是其他员工 E810 的回执
    r = client.post("/api/demo/stale/supplement-other")
    assert r.status_code == 200
    # 本页确认组只含 E710：其他员工的补录不阻止本组确认，即使本页未刷新
    r = confirm(client, [{"order_no": "PO7100", "receipt_nos": ["RC7100"]}], basis=old_basis)
    assert r.status_code == 200, r.text
    state = snapshot(client)
    statuses = {o["order_no"]: o["status"] for o in state["orders"]}
    assert statuses == {"PO7100": "confirmed", "PO8100": "open"}

    # 对照：若坚持用旧依据确认被补录员工 E810 的单，则应过期拒绝
    r = confirm(client, [{"order_no": "PO8100", "receipt_nos": ["RC8100"]}], basis=old_basis)
    assert r.status_code == 409 and r.json()["code"] == "stale_suggestion"


def test_refresh_allows_manual_pending_selection_without_auto_resubmit(client):
    old_basis = setup_stale_scenario(client)
    client.post("/api/demo/stale/supplement-same")
    r = confirm(client, [{"order_no": "PO7100", "receipt_nos": ["RC7100"]}], basis=old_basis)
    assert r.status_code == 409

    # 刷新后的建议：PO7100 变为待核（两种 100 分组合），不自动勾选/重提
    fresh = snapshot(client)
    pending = {p["order"]["order_no"]: p["combos"] for p in fresh["suggestion"]["pending"]}
    assert set(pending) == {"PO7100"}
    combos = {frozenset(rc["receipt_no"] for rc in combo) for combo in pending["PO7100"]}
    assert combos == {frozenset({"RC7100"}), frozenset({"RC7101"})}
    fresh_basis = fresh["suggestion_basis"]
    assert fresh_basis != old_basis

    # 用刷新后的依据 + 人工选择的组合（选补录前的那一条）可以确认
    r = confirm(client, [{"order_no": "PO7100", "receipt_nos": ["RC7100"]}], basis=fresh_basis)
    assert r.status_code == 200
    state = snapshot(client)
    assert {o["order_no"]: o["status"] for o in state["orders"]}["PO7100"] == "confirmed"
    assert {rc["receipt_no"]: rc["status"] for rc in state["receipts"]}["RC7100"] == "used"


def test_confirm_requires_valid_basis(client):
    setup_stale_scenario(client)
    r = client.post("/api/confirm", json={"items": [
        {"order_no": "PO7100", "receipt_nos": ["RC7100"]},
    ]})
    assert r.status_code == 400 and "建议依据" in r.json()["detail"]

    r = client.post("/api/confirm", json={
        "items": [{"order_no": "PO7100", "receipt_nos": ["RC7100"]}],
        "basis": "forged.token",
    })
    assert r.status_code == 400 and "无效" in r.json()["detail"]

    # 篡改签名
    good = snapshot(client)["suggestion_basis"]
    body, sig = good.split(".")
    r = client.post("/api/confirm", json={
        "items": [{"order_no": "PO7100", "receipt_nos": ["RC7100"]}],
        "basis": body + "." + ("A" if sig[0] != "A" else "B") + sig[1:],
    })
    assert r.status_code == 400 and "无效" in r.json()["detail"]


def test_concurrent_supplement_and_confirm_rejects_stale(client):
    """补录与确认并发：补录先拿到写锁并在确认校验前提交，旧依据必须被拒。"""
    old_basis = setup_stale_scenario(client)
    held = threading.Event()
    release = threading.Event()
    results = {}

    def hold_lock_and_supplement():
        conn = db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            db.insert_receipts(conn, [(
                "RC7101", "E710", 100, "2026-10", time.time(),
            )])
            db.bump_employee_revisions(conn, ["E710"])
            held.set()
            # 确认请求此时已被挡在 BEGIN IMMEDIATE 之外
            release.wait(timeout=10)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def do_confirm():
        held.wait(timeout=10)
        r = client.post("/api/confirm", json={
            "items": [{"order_no": "PO7100", "receipt_nos": ["RC7100"]}],
            "basis": old_basis,
        })
        results["confirm"] = (r.status_code, r.json())

    t1 = threading.Thread(target=hold_lock_and_supplement)
    t2 = threading.Thread(target=do_confirm)
    t1.start(); t2.start()
    held.wait(timeout=10)
    time.sleep(0.4)  # 让确认请求先进入等待写锁的状态
    release.set()
    t1.join(); t2.join()

    code, body = results["confirm"]
    assert code == 409 and body["code"] == "stale_suggestion"

    state = snapshot(client)
    assert state["confirmed"] == []
    assert {o["order_no"]: o["status"] for o in state["orders"]} == {
        "PO7100": "open", "PO8100": "open"
    }


def test_write_failure_rolls_back_whole_group(client, monkeypatch):
    """写入中途失败（如数据库异常）时整组回滚：单据、回执、确认批次都不留痕。"""
    setup_stale_scenario(client)
    basis_token = snapshot(client)["suggestion_basis"]
    real_save = db.save_confirmation

    def failing_save(conn, pairing, employees_by_order, now):
        real_save(conn, pairing, employees_by_order, now)  # 已写入批次/明细/状态
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(db, "save_confirmation", failing_save)
    with pytest.raises(sqlite3.OperationalError):
        client.post("/api/confirm", json={
            "items": [
                {"order_no": "PO7100", "receipt_nos": ["RC7100"]},
                {"order_no": "PO8100", "receipt_nos": ["RC8100"]},
            ],
            "basis": basis_token,
        })

    monkeypatch.setattr(db, "save_confirmation", real_save)
    state = snapshot(client)
    assert state["confirmed"] == []
    assert {o["order_no"]: o["status"] for o in state["orders"]} == {
        "PO7100": "open", "PO8100": "open"
    }
    assert {rc["receipt_no"]: rc["status"] for rc in state["receipts"]} == {
        "RC7100": "open", "RC8100": "open"
    }
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM confirmation_items").fetchone()[0] == 0
    finally:
        conn.close()


def test_basis_token_survives_restart(client):
    """签名密钥随库持久化：重启后旧页面（未刷新）的依据仍按真实状态校验。"""
    import app.main as main_module

    old_basis = setup_stale_scenario(client)
    saved_secret = main_module.TOKEN_SECRET
    try:
        # 模拟进程重启：丢掉内存密钥，走 lifespan 从库内重新加载
        main_module.TOKEN_SECRET = ""
        with TestClient(app):
            r = confirm(client, [{"order_no": "PO7100", "receipt_nos": ["RC7100"]}],
                        basis=old_basis)
            assert r.status_code == 200, r.text
    finally:
        main_module.TOKEN_SECRET = saved_secret


def test_basis_snapshot_consistent_with_suggestion(client):
    """建议与其依据令牌来自同一已提交读快照；员工数据变化后令牌随之变化。"""
    load_demo(client)
    state = snapshot(client)
    assert state["suggestion_basis"]
    client.post("/api/demo/supplement")
    assert snapshot(client)["suggestion_basis"] != state["suggestion_basis"]


def test_csv_import_and_evidence_download(client):
    csv_orders = "order_no,employee_no,amount,period\nC1,E1,500,2026-10\nC2,E2,700,2026-10\n"
    csv_receipts = (
        "receipt_no,employee_no,amount,period\n"
        "CR1,E1,200,2026-10\nCR2,E1,300,2026-10\n"
        "CR3,E2,700,2026-10\n"
    )
    r = client.post("/api/import/orders", files={"file": ("o.csv", csv_orders, "text/csv")})
    assert r.status_code == 200, r.text
    r = client.post("/api/import/receipts", files={"file": ("r.csv", csv_receipts, "text/csv")})
    assert r.status_code == 200, r.text

    state = client.get("/api/state").json()["suggestion"]
    assert state["max_matched"] == 2
    r = confirm(client, [
        {"order_no": "C1", "receipt_nos": ["CR1", "CR2"]},
        {"order_no": "C2", "receipt_nos": ["CR3"]},
    ])
    assert r.status_code == 200
    r = client.get("/api/evidence.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert b"\xef\xbb\xbf" in r.content  # BOM


def test_evidence_empty_returns_404(client):
    r = client.get("/api/evidence.csv")
    assert r.status_code == 404
