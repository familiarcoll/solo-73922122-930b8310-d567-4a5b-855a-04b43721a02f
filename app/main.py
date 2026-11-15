"""FastAPI 对账服务入口。"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import basis, db
from .matching import (
    Order,
    Receipt,
    suggest,
    validate_confirmation,
)
from .validation import (
    ImportError_,
    demo_orders,
    demo_receipts,
    demo_supplement_receipt,
    parse_csv,
    stale_demo_orders,
    stale_demo_receipts,
    stale_demo_supplement_other,
    stale_demo_supplement_same,
    validate_orders_payload,
    validate_receipts_payload,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 建议依据令牌的签名密钥：环境变量优先，否则在库内生成并持久化，
# 保证容器重启后未刷新页面持有的依据仍能被正确校验（而不是误判为非法）。
TOKEN_SECRET = os.environ.get("RECON_TOKEN_SECRET") or ""
_secret_lock = threading.Lock()


def _token_secret(conn: sqlite3.Connection) -> str:
    """取签名密钥；缺省时从库内加载（不存在则生成并持久化）。"""
    global TOKEN_SECRET
    if TOKEN_SECRET:
        return TOKEN_SECRET
    with _secret_lock:
        if not TOKEN_SECRET:
            TOKEN_SECRET = db.get_or_create_token_secret(conn)
    return TOKEN_SECRET

STALE_CODE = "stale_suggestion"
STALE_MESSAGE = "建议已过期：数据已被同员工补录或确认改变，本组确认已拒绝，整组未写入。请刷新后重新选择。"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global TOKEN_SECRET
    conn = db.connect()
    try:
        db.init_db(conn)
        if not TOKEN_SECRET:
            TOKEN_SECRET = db.get_or_create_token_secret(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="工资支付单-银行回执对账", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------- 数据装配

def _open_sets(conn: sqlite3.Connection):
    order_rows = db.fetch_open_orders(conn)
    receipt_rows = db.fetch_open_receipts(conn)
    orders = [Order(r["order_no"], r["employee_no"], r["amount"]) for r in order_rows]
    receipts = [Receipt(r["receipt_no"], r["employee_no"], r["amount"]) for r in receipt_rows]
    return orders, receipts


def _suggestion_payload(conn: sqlite3.Connection) -> dict:
    orders, receipts = _open_sets(conn)
    suggestion = suggest(orders, receipts)
    order_map = {o.order_no: {"employee_no": o.employee_no, "amount": o.amount} for o in orders}
    receipt_map = {r.receipt_no: {"employee_no": r.employee_no, "amount": r.amount} for r in receipts}

    def enrich_order(order_no):
        info = order_map[order_no]
        return {"order_no": order_no, **info}

    def enrich_combo(receipt_nos):
        return [
            {"receipt_no": rn, **receipt_map[rn]} for rn in receipt_nos if rn in receipt_map
        ]

    certain = [
        {
            "order": enrich_order(order_no),
            "receipts": enrich_combo(receipt_nos),
        }
        for order_no, receipt_nos in sorted(suggestion["certain"].items())
    ]
    pending = [
        {
            "order": enrich_order(order_no),
            "combos": [enrich_combo(combo) for combo in combos],
        }
        for order_no, combos in sorted(suggestion["pending"].items())
    ]
    unmatched = [enrich_order(no) for no in suggestion["unmatched"]]
    unused = [
        {"receipt_no": rn, **receipt_map[rn]}
        for rn in suggestion["unused_receipts"]
        if rn in receipt_map
    ]
    return {
        "max_matched": suggestion["max_matched"],
        "certain": certain,
        "pending": pending,
        "unmatched": unmatched,
        "unused_receipts": unused,
        "counts": {
            "open_orders": len(orders),
            "open_receipts": len(receipts),
            "max_orders": db.MAX_ORDERS,
            "max_receipts": db.MAX_RECEIPTS,
        },
    }


def _confirmed_payload(conn: sqlite3.Connection) -> List[dict]:
    evidence = db.fetch_evidence(conn)
    groups: Dict[int, dict] = {}
    for row in evidence:
        g = groups.setdefault(
            row["confirmation_id"],
            {
                "confirmation_id": row["confirmation_id"],
                "confirmed_at": row["confirmed_at"],
                "items": {},
            },
        )
        item = g["items"].setdefault(
            row["order_no"],
            {
                "order_no": row["order_no"],
                "employee_no": row["employee_no"],
                "order_amount": row["order_amount"],
                "receipts": [],
            },
        )
        item["receipts"].append(
            {"receipt_no": row["receipt_no"], "amount": row["receipt_amount"]}
        )
    result = []
    for g in groups.values():
        g["items"] = sorted(g["items"].values(), key=lambda x: x["order_no"])
        for item in g["items"]:
            item["receipts"].sort(key=lambda x: x["receipt_no"])
        result.append(g)
    result.sort(key=lambda g: g["confirmation_id"])
    return result


# --------------------------------------------------------------- 路由：页面

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


# --------------------------------------------------------------- 路由：查询

@app.get("/api/state")
def get_state() -> dict:
    conn = db.connect()
    try:
        secret = _token_secret(conn)  # 可能写库，必须在只读事务外完成
        with db.read_transaction(conn):
            # 建议、令牌与已确认依据必须来自同一个已提交快照
            payload = _state_snapshot_locked(conn, secret)
            payload["orders"] = [dict(r) for r in db.fetch_all_orders(conn)]
            payload["receipts"] = [dict(r) for r in db.fetch_all_receipts(conn)]
            return payload
    finally:
        conn.close()


def _state_snapshot_locked(conn: sqlite3.Connection, secret: str) -> dict:
    """调用方须已开启读事务，保证建议与依据令牌同源。"""
    revisions = db.employee_revisions(conn)
    return {
        "suggestion": _suggestion_payload(conn),
        "confirmed": _confirmed_payload(conn),
        "revisions": revisions,
        "suggestion_basis": basis.issue(secret, revisions),
    }


# --------------------------------------------------------------- 路由：导入

@app.post("/api/import/orders")
async def import_orders(file: Optional[UploadFile] = File(default=None)) -> dict:
    """导入支付单。支持 JSON（application/json 走 /api/import/orders-json）或 CSV 上传。"""
    if file is None:
        raise HTTPException(status_code=400, detail="请上传 CSV 文件，或调用 JSON 接口")
    raw = (await file.read()).decode("utf-8-sig")
    try:
        payload = {"orders": parse_csv(raw)}
        records = validate_orders_payload(payload)
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_orders(records)


class OrdersJson(BaseModel):
    orders: List[dict] = Field(min_length=1)


@app.post("/api/import/orders-json")
def import_orders_json(body: OrdersJson) -> dict:
    try:
        records = validate_orders_payload(body.model_dump())
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_orders(records)


def _persist_orders(records) -> dict:
    conn = db.connect()
    try:
        existing = db.count_orders(conn)
        if existing + len(records) > db.MAX_ORDERS:
            raise HTTPException(
                status_code=400,
                detail=f"同月支付单上限 {db.MAX_ORDERS} 张：已有 {existing} 张，本次 {len(records)} 张",
            )
        existing_periods = {
            r[0] for r in conn.execute("SELECT DISTINCT period FROM payment_orders")
        }
        incoming_periods = {r.period for r in records}
        if existing_periods and incoming_periods != existing_periods:
            raise HTTPException(
                status_code=400,
                detail=f"月份不一致：库内为 {sorted(existing_periods)}，本次为 {sorted(incoming_periods)}",
            )
        now = time.time()
        try:
            with db.immediate_transaction(conn):
                db.insert_orders(
                    conn, [(r.no, r.employee_no, r.amount, r.period, now) for r in records]
                )
                # 新支付单改变了这些员工的建议依据
                db.bump_employee_revisions(conn, [r.employee_no for r in records])
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail=f"支付单编号冲突：{exc}")
        return {"imported": len(records), "state": _state_snapshot(conn)}
    finally:
        conn.close()


@app.post("/api/import/receipts")
async def import_receipts(file: Optional[UploadFile] = File(default=None)) -> dict:
    if file is None:
        raise HTTPException(status_code=400, detail="请上传 CSV 文件，或调用 JSON 接口")
    raw = (await file.read()).decode("utf-8-sig")
    try:
        payload = {"receipts": parse_csv(raw)}
        records = validate_receipts_payload(payload)
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_receipts(records)


class ReceiptsJson(BaseModel):
    receipts: List[dict] = Field(min_length=1)


@app.post("/api/import/receipts-json")
def import_receipts_json(body: ReceiptsJson) -> dict:
    try:
        records = validate_receipts_payload(body.model_dump())
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_receipts(records)


def _persist_receipts(records) -> dict:
    conn = db.connect()
    try:
        existing = db.count_receipts(conn)
        if existing + len(records) > db.MAX_RECEIPTS:
            raise HTTPException(
                status_code=400,
                detail=f"同月回执上限 {db.MAX_RECEIPTS} 条：已有 {existing} 条，本次 {len(records)} 条",
            )
        existing_periods = {
            r[0] for r in conn.execute("SELECT DISTINCT period FROM receipts")
        }
        incoming_periods = {r.period for r in records}
        if existing_periods and incoming_periods != existing_periods:
            raise HTTPException(
                status_code=400,
                detail=f"月份不一致：库内为 {sorted(existing_periods)}，本次为 {sorted(incoming_periods)}",
            )
        now = time.time()
        try:
            with db.immediate_transaction(conn):
                db.insert_receipts(
                    conn, [(r.no, r.employee_no, r.amount, r.period, now) for r in records]
                )
                # 补录回执改变了这些员工的建议依据
                db.bump_employee_revisions(conn, [r.employee_no for r in records])
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail=f"回执编号冲突：{exc}")
        return {"imported": len(records), "state": _state_snapshot(conn)}
    finally:
        conn.close()


def _state_snapshot(conn: sqlite3.Connection) -> dict:
    secret = _token_secret(conn)
    with db.read_transaction(conn):
        return _state_snapshot_locked(conn, secret)


# --------------------------------------------------------------- 路由：确认

class ConfirmItem(BaseModel):
    order_no: str
    receipt_nos: List[str] = Field(min_length=1, max_length=3)


class ConfirmRequest(BaseModel):
    items: List[ConfirmItem] = Field(min_length=1)
    # 生成本组建议时的依据令牌；确认必须与建议来自同一个已提交状态
    basis: Optional[str] = None


class StaleSuggestion(Exception):
    """请求所依据的建议快照已被同员工数据变化推翻。"""


@app.post("/api/confirm")
def confirm(body: ConfirmRequest):
    """整组确认：全部合法才落库，任一回执被占用或建议过期则整体失败、数据不变。"""
    pairing = {item.order_no: item.receipt_nos for item in body.items}
    if len(pairing) != len(body.items):
        raise HTTPException(status_code=400, detail="支付单在请求中重复出现")
    if not body.basis:
        raise HTTPException(
            status_code=400,
            detail="缺少建议依据：请先刷新建议后再确认，页面不会自动重提旧选择",
        )

    conn = db.connect()
    try:
        try:
            expected_revisions = basis.verify(_token_secret(conn), body.basis)
        except basis.InvalidBasis as exc:
            raise HTTPException(status_code=400, detail=f"建议依据无效：{exc}")
        try:
            with db.immediate_transaction(conn):
                # 拿锁后重新读取，保证并发确认/补录下不会放行过期请求
                orders, receipts = _open_sets(conn)
                all_receipts = [
                    Receipt(r["receipt_no"], r["employee_no"], r["amount"])
                    for r in db.fetch_all_receipts(conn)
                ]
                used = db.used_receipt_nos(conn)

                # 建议与确认依据必须来自同一已提交状态：逐员工比对版本。
                # 仅比对本组涉及的员工，其他员工的补录不影响本组确认。
                # 未知订单（不存在/已确认）也必须能识别版本，先查全量订单。
                current_revisions = db.employee_revisions(conn)
                employee_by_order = {
                    r["order_no"]: r["employee_no"] for r in db.fetch_all_orders(conn)
                }
                for order_no in pairing:
                    employee_no = employee_by_order.get(order_no)
                    if employee_no is None:
                        raise HTTPException(status_code=409, detail=f"支付单 {order_no} 不存在")
                    if expected_revisions.get(employee_no, 0) != current_revisions.get(
                        employee_no, 0
                    ):
                        raise StaleSuggestion()

                ok, reason = validate_confirmation(
                    pairing, orders, receipts, frozenset(used), all_receipts
                )
                if not ok:
                    raise HTTPException(status_code=409, detail=reason)
                confirmation_id = db.save_confirmation(
                    conn,
                    pairing,
                    {o: employee_by_order[o] for o in pairing},
                    time.time(),
                )
        except StaleSuggestion:
            return JSONResponse(
                status_code=409,
                content={"detail": STALE_MESSAGE, "code": STALE_CODE},
            )
        return {
            "confirmation_id": confirmation_id,
            "confirmed_items": len(pairing),
            "state": _state_snapshot(conn),
        }
    finally:
        conn.close()


# --------------------------------------------------------------- 路由：依据下载

@app.get("/api/evidence.csv")
def download_evidence_csv() -> Response:
    conn = db.connect()
    try:
        rows = db.fetch_evidence(conn)
        if not rows:
            raise HTTPException(status_code=404, detail="尚无已确认依据，请先确认配对")
        buf = io.StringIO()
        buf.write("﻿")  # UTF-8 BOM，便于 Excel 直接打开
        writer = csv.writer(buf)
        writer.writerow(
            [
                "确认批次",
                "确认时间",
                "支付单号",
                "员工号",
                "应付金额(分)",
                "回执编号",
                "回执金额(分)",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["confirmation_id"],
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["confirmed_at"])),
                    r["order_no"],
                    r["employee_no"],
                    r["order_amount"],
                    r["receipt_no"],
                    r["receipt_amount"],
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=evidence.csv"},
        )
    finally:
        conn.close()


# --------------------------------------------------------------- 路由：演示/重置

def _reset_and_load(orders: List[dict], receipts: List[dict]) -> dict:
    """清库后载入一组演示数据；返回最新快照（含新依据令牌）。"""
    conn = db.connect()
    try:
        db.reset_db(conn)
        now = time.time()
        with db.immediate_transaction(conn):
            db.insert_orders(
                conn,
                [(o["order_no"], o["employee_no"], o["amount"], o["period"], now) for o in orders],
            )
            db.insert_receipts(
                conn,
                [(r["receipt_no"], r["employee_no"], r["amount"], r["period"], now) for r in receipts],
            )
            db.bump_employee_revisions(
                conn,
                [o["employee_no"] for o in orders] + [r["employee_no"] for r in receipts],
            )
        return _state_snapshot(conn)
    finally:
        conn.close()


@app.post("/api/demo")
def load_demo() -> dict:
    orders = demo_orders()
    receipts = demo_receipts()
    return {
        "loaded_orders": len(orders),
        "loaded_receipts": len(receipts),
        "state": _reset_and_load(orders, receipts),
    }


@app.post("/api/demo/supplement")
def demo_supplement() -> dict:
    """补录演示：新增 E005 的回执，触发建议重算。"""
    return _import_receipts_records([demo_supplement_receipt()])


@app.post("/api/demo/stale")
def load_stale_demo() -> dict:
    """演示「过期建议」场景：E710 一张 100 分支付单 + 一条 100 分回执（确定配对）。"""
    return {"state": _reset_and_load(stale_demo_orders(), stale_demo_receipts())}


@app.post("/api/demo/stale/supplement-same")
def supplement_other_window_same_employee() -> dict:
    """模拟另一窗口：同员工补录第二条 100 分回执。"""
    return _import_receipts_records([stale_demo_supplement_same()])


@app.post("/api/demo/stale/supplement-other")
def supplement_other_window_other_employee() -> dict:
    """模拟另一窗口：其他员工补录，不改变 E710 的建议依据。"""
    return _import_receipts_records([stale_demo_supplement_other()])


def _import_receipts_records(raw_records) -> dict:
    try:
        records = validate_receipts_payload({"receipts": raw_records})
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_receipts(records)


@app.post("/api/reset")
def reset() -> dict:
    conn = db.connect()
    try:
        db.reset_db(conn)
        return {"ok": True, "state": _state_snapshot(conn)}
    finally:
        conn.close()
