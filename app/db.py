"""SQLite 持久化层。

表：
- payment_orders  支付单（导入后不可变；确认后 status=confirmed）
- receipts        银行回执（补录进同一批次；确认后被占用）
- confirmations   一次确认动作（整组事务），对应一份可下载实付依据
- confirmation_items  确认明细（支付单 ↔ 回执配对）
- employee_revisions  员工维度的已提交状态版本（建议/确认依据一致性）

匹配按员工相互独立（回执只与同员工支付单配对），因此每个员工的开放
支付单/回执每发生一次变化就把该员工版本 +1。确认请求携带生成建议时的
版本，服务端在写锁内重新读取比对；同员工补录后旧页面的确认必然过期，
其他员工的变化不影响本组确认。
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Dict, Iterable, Iterator, List, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_orders (
    order_no     TEXT PRIMARY KEY,
    employee_no  TEXT NOT NULL,
    amount       INTEGER NOT NULL CHECK (amount > 0),
    period       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','confirmed')),
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_no   TEXT PRIMARY KEY,
    employee_no  TEXT NOT NULL,
    amount       INTEGER NOT NULL CHECK (amount > 0),
    period       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','used')),
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmed_at REAL NOT NULL,
    item_count   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmation_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_id INTEGER NOT NULL REFERENCES confirmations(id),
    order_no        TEXT NOT NULL REFERENCES payment_orders(order_no),
    receipt_no      TEXT NOT NULL REFERENCES receipts(receipt_no) UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_items_conf ON confirmation_items(confirmation_id);
CREATE INDEX IF NOT EXISTS idx_items_order ON confirmation_items(order_no);
CREATE TABLE IF NOT EXISTS employee_revisions (
    employee_no  TEXT PRIMARY KEY,
    revision     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

MAX_ORDERS = 8
MAX_RECEIPTS = 16


def default_db_path() -> str:
    return os.environ.get("RECON_DB_PATH", os.path.join("data", "recon.db"))


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    with immediate_transaction(conn):
        seed_employee_revisions(conn)


def seed_employee_revisions(conn: sqlite3.Connection) -> None:
    """为既有数据补齐版本行（只补缺，不重置已有版本）。

    升级前已存在的员工数据也参与版本比对；此后任一补录/确认都会把版本 +1。
    """
    conn.execute(
        "INSERT OR IGNORE INTO employee_revisions(employee_no, revision)"
        " SELECT employee_no, 0 FROM payment_orders"
        " UNION SELECT employee_no, 0 FROM receipts"
    )


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读一致性快照：建议数据与员工版本必须来自同一次已提交状态。"""
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即加写锁的事务上下文，配合 busy_timeout 实现整组成功/全不改。

    两个窗口同时确认时，第二个窗口阻塞在 BEGIN IMMEDIATE，
    拿到锁后重新读取状态校验，若回执已被占用则整体回滚。
    """
    last_err: sqlite3.OperationalError | None = None
    for _ in range(5):  # 重试，规避锁等待超时的边界情况
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:  # database is locked
            last_err = exc
            time.sleep(0.1)
    else:
        raise last_err  # type: ignore[misc]
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reset_db(conn: sqlite3.Connection) -> None:
    with immediate_transaction(conn):
        affected = {
            row[0]
            for row in conn.execute(
                "SELECT employee_no FROM payment_orders"
                " UNION SELECT employee_no FROM receipts"
            )
        }
        conn.execute("DELETE FROM confirmation_items")
        conn.execute("DELETE FROM confirmations")
        conn.execute("DELETE FROM receipts")
        conn.execute("DELETE FROM payment_orders")
        # 版本号只增不减：清空后旧页面持有的版本必然过期（不会因归零而“复活”）
        bump_employee_revisions(conn, affected)


def insert_orders(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    conn.executemany(
        "INSERT INTO payment_orders(order_no, employee_no, amount, period, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def insert_receipts(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    conn.executemany(
        "INSERT INTO receipts(receipt_no, employee_no, amount, period, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def bump_employee_revisions(conn: sqlite3.Connection, employee_nos: Iterable[str]) -> None:
    """在调用方事务内把相关员工的状态版本 +1。"""
    conn.executemany(
        "INSERT INTO employee_revisions(employee_no, revision) VALUES (?, 1)"
        " ON CONFLICT(employee_no) DO UPDATE SET revision = revision + 1",
        [(e,) for e in sorted(set(employee_nos))],
    )


def employee_revisions(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        row["employee_no"]: row["revision"]
        for row in conn.execute("SELECT employee_no, revision FROM employee_revisions")
    }


def get_or_create_token_secret(conn: sqlite3.Connection) -> str:
    """建议依据令牌的签名密钥，随库持久化，容器重启后旧页面令牌仍可校验。"""
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key='token_secret'"
    ).fetchone()
    if row is not None:
        return row[0]
    candidate = secrets.token_hex(32)
    conn.execute(
        "INSERT OR IGNORE INTO app_meta(key, value) VALUES ('token_secret', ?)",
        (candidate,),
    )
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key='token_secret'"
    ).fetchone()
    return row[0]


def count_orders(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0]


def count_receipts(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]


def fetch_open_orders(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT order_no, employee_no, amount FROM payment_orders"
        " WHERE status='open' ORDER BY order_no"
    ).fetchall()


def fetch_open_receipts(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT receipt_no, employee_no, amount FROM receipts"
        " WHERE status='open' ORDER BY receipt_no"
    ).fetchall()


def fetch_all_orders(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT order_no, employee_no, amount, period, status FROM payment_orders"
        " ORDER BY order_no"
    ).fetchall()


def fetch_all_receipts(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT receipt_no, employee_no, amount, period, status FROM receipts"
        " ORDER BY receipt_no"
    ).fetchall()


def used_receipt_nos(conn: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in conn.execute("SELECT receipt_no FROM confirmation_items")
    }


def save_confirmation(
    conn: sqlite3.Connection,
    pairing: dict,
    employees_by_order: dict,
    now: float,
) -> int:
    """在已开启的事务内写入确认，返回 confirmation id。"""
    cur = conn.execute(
        "INSERT INTO confirmations(confirmed_at, item_count) VALUES (?, ?)",
        (now, len(pairing)),
    )
    confirmation_id = cur.lastrowid
    items: List[tuple] = []
    for order_no, receipt_nos in pairing.items():
        for rn in receipt_nos:
            items.append((confirmation_id, order_no, rn))
    conn.executemany(
        "INSERT INTO confirmation_items(confirmation_id, order_no, receipt_no)"
        " VALUES (?, ?, ?)",
        items,
    )
    order_nos = list(pairing.keys())
    receipt_nos = [rn for nos in pairing.values() for rn in nos]
    conn.executemany(
        "UPDATE payment_orders SET status='confirmed' WHERE order_no=?",
        [(o,) for o in order_nos],
    )
    conn.executemany(
        "UPDATE receipts SET status='used' WHERE receipt_no=?",
        [(r,) for r in receipt_nos],
    )
    # 确认后这些员工的开放池变化，其他持有旧快照的页面必须重新计算
    bump_employee_revisions(conn, [employees_by_order[o] for o in order_nos])
    return confirmation_id


def fetch_evidence(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.id AS confirmation_id, c.confirmed_at,
               i.order_no, po.employee_no,
               po.amount AS order_amount,
               i.receipt_no, r.amount AS receipt_amount
        FROM confirmations c
        JOIN confirmation_items i ON i.confirmation_id = c.id
        JOIN payment_orders po ON po.order_no = i.order_no
        JOIN receipts r ON r.receipt_no = i.receipt_no
        ORDER BY c.id, i.order_no, i.receipt_no
        """
    ).fetchall()
