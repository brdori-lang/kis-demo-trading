import json
import time
import uuid
from datetime import datetime, timezone

import httpx

from auth import get_access_token
from config import settings
from kis_api import _parse_account_info, get_account_balance, get_current_price
from kis_safety import (
    KIS_VTS_REST_BASE_URL,
    require_virtual_environment,
    require_virtual_order_tr_id,
    require_vts_rest_url,
)


ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
ORDER_INQUIRY_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
ORDER_TR_IDS = {"BUY": "VTTC0012U", "SELL": "VTTC0011U"}
CANCEL_TR_ID = "VTTC0013U"
ORDER_INQUIRY_TR_ID = "VTTC0081R"
# KIS continuation: response header tr_cont "F"/"M" = more rows follow; the next request sends
# tr_cont "N" with the ctx_area_fk100 / ctx_area_nk100 values of the previous response.
ORDER_INQUIRY_MORE = {"F", "M"}
ORDER_INQUIRY_MAX_PAGES = 50
ORDER_INQUIRY_PAGE_INTERVAL_SECONDS = 1.05   # same spacing as kis_api's KIS REST throttle
ORDER_DIVISIONS = {"LIMIT": "00", "MARKET": "01"}
TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "ERROR"}


class OrderSafetyError(RuntimeError):
    pass


class DuplicateOrderError(ValueError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def mask_sensitive(value: str | None) -> str | None:
    if not value:
        return value
    return "***" + value[-4:] if len(value) >= 4 else "***"


def validate_order_values(stock_code, side, quantity, price, order_type):
    if not isinstance(stock_code, str) or len(stock_code) != 6 or not stock_code.isdigit():
        raise ValueError("종목코드는 6자리 숫자여야 합니다.")
    if side not in ORDER_TR_IDS:
        raise ValueError("side는 BUY 또는 SELL이어야 합니다.")
    if type(quantity) is not int or quantity <= 0 or quantity > settings.MAX_ORDER_QUANTITY:
        raise ValueError("주문수량이 허용 범위를 벗어났습니다.")
    if type(price) is not int or price < 0:
        raise ValueError("주문가격은 0 이상의 정수여야 합니다.")
    if order_type not in ORDER_DIVISIONS:
        raise ValueError("지원하지 않는 주문유형입니다.")
    if order_type == "LIMIT" and price <= 0:
        raise ValueError("지정가 주문가격은 0보다 커야 합니다.")
    if order_type == "MARKET" and price != 0:
        raise ValueError("시장가 주문가격은 0이어야 합니다.")


def require_order_submission_enabled(execution_mode="KIS_VIRTUAL"):
    require_virtual_environment()
    if execution_mode != "KIS_VIRTUAL":
        raise OrderSafetyError("KIS_VIRTUAL 실행 모드만 허용합니다.")
    if not settings.PAPER_ORDER_ENABLED:
        raise OrderSafetyError("PAPER_ORDER_ENABLED가 비활성화되어 있습니다.")
    if not settings.KIS_VIRTUAL_ORDER_SUBMIT_ENABLED:
        raise OrderSafetyError("KIS_VIRTUAL_ORDER_SUBMIT_ENABLED가 비활성화되어 있습니다.")


class KISVirtualOrderClient:
    def __init__(self, post_transport=None, get_transport=None, sleep=None):
        self.post_transport = post_transport or httpx.post
        self.get_transport = get_transport or httpx.get
        self.sleep = sleep or time.sleep

    def build_order_request(self, stock_code, side, quantity, price, order_type):
        validate_order_values(stock_code, side, quantity, price, order_type)
        require_virtual_environment()
        account_no = (settings.KIS_ACCOUNT_NO or "").strip()
        if not account_no:
            raise ValueError("KIS 계좌 설정이 필요합니다.")
        cano, product_code = _parse_account_info(account_no)
        tr_id = ORDER_TR_IDS[side]
        require_virtual_order_tr_id(tr_id)
        url = f"{KIS_VTS_REST_BASE_URL}{ORDER_PATH}"
        require_vts_rest_url(url)
        return {
            "url": url,
            "tr_id": tr_id,
            "body": {
                "CANO": cano,
                "ACNT_PRDT_CD": product_code,
                "PDNO": stock_code,
                "ORD_DVSN": ORDER_DIVISIONS[order_type],
                "ORD_QTY": str(quantity),
                "ORD_UNPR": str(price),
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if side == "SELL" else "",
                "CNDT_PRIC": "",
            },
        }

    def preview_order(self, stock_code, side, quantity, price, order_type):
        request = self.build_order_request(stock_code, side, quantity, price, order_type)
        return {
            "execution_mode": "KIS_VIRTUAL",
            "host": KIS_VTS_REST_BASE_URL,
            "path": ORDER_PATH,
            "tr_id": request["tr_id"],
            "stock_code": stock_code,
            "side": side,
            "quantity": quantity,
            "price": price,
            "order_type": order_type,
            "account": mask_sensitive(request["body"]["CANO"]),
            "submit_enabled": bool(
                settings.PAPER_ORDER_ENABLED and settings.KIS_VIRTUAL_ORDER_SUBMIT_ENABLED
            ),
        }

    def submit_order(self, stock_code, side, quantity, price, order_type):
        require_order_submission_enabled()
        request = self.build_order_request(stock_code, side, quantity, price, order_type)
        response = self.post_transport(
            request["url"],
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {get_access_token()}",
                "appkey": settings.KIS_APP_KEY,
                "appsecret": settings.KIS_APP_SECRET,
                "tr_id": request["tr_id"],
                "custtype": "P",
            },
            json=request["body"],
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        output = payload.get("output") or {}
        return {
            "success": payload.get("rt_cd") == "0",
            "broker_order_id": output.get("ODNO"),
            "broker_org_no": output.get("KRX_FWDG_ORD_ORGNO"),
            "order_time": output.get("ORD_TMD"),
            "message_code": payload.get("msg_cd"),
            "message": payload.get("msg1"),
        }

    def inquire_orders(self, inquiry_date=None):
        require_virtual_environment()
        account_no = (settings.KIS_ACCOUNT_NO or "").strip()
        if not account_no:
            raise ValueError("KIS 계좌 설정이 필요합니다.")
        cano, product_code = _parse_account_info(account_no)
        inquiry_date = inquiry_date or datetime.now().strftime("%Y%m%d")
        url = f"{KIS_VTS_REST_BASE_URL}{ORDER_INQUIRY_PATH}"
        require_vts_rest_url(url)
        # KIS returns one page at a time (VTS: 15 rows). Follow the continuation until the last page;
        # a partial list would make reconciliation report real orders as missing.
        orders, seen, context = [], set(), ("", "")
        for page in range(ORDER_INQUIRY_MAX_PAGES):
            headers = {
                "authorization": f"Bearer {get_access_token()}",
                "appkey": settings.KIS_APP_KEY,
                "appsecret": settings.KIS_APP_SECRET,
                "tr_id": ORDER_INQUIRY_TR_ID,
                "custtype": "P",
            }
            if page:
                headers["tr_cont"] = "N"
                self.sleep(ORDER_INQUIRY_PAGE_INTERVAL_SECONDS)
            response = self.get_transport(
                url,
                headers=headers,
                params={
                    "CANO": cano, "ACNT_PRDT_CD": product_code,
                    "INQR_STRT_DT": inquiry_date, "INQR_END_DT": inquiry_date,
                    "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": "00",
                    "INQR_DVSN": "00", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "",
                    "ODNO": "", "INQR_DVSN_1": "", "CTX_AREA_FK100": context[0],
                    "CTX_AREA_NK100": context[1], "EXCG_ID_DVSN_CD": "KRX",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("rt_cd") != "0":
                raise RuntimeError("KIS VTS 주문 조회에 실패했습니다.")
            for item in payload.get("output1") or []:
                order = normalize_broker_order(item)
                if order["broker_order_id"] in seen:
                    continue            # a row repeated across pages is one order
                seen.add(order["broker_order_id"])
                orders.append(order)
            if (response.headers.get("tr_cont") or "").strip() not in ORDER_INQUIRY_MORE:
                return orders
            following = ((payload.get("ctx_area_fk100") or "").strip(), (payload.get("ctx_area_nk100") or "").strip())
            if not any(following) or following == context:
                # "More rows" without a new position cannot be followed safely: fail instead of
                # returning a partial list that would look like missing orders.
                raise RuntimeError("KIS VTS 주문 조회 연속조회 정보가 올바르지 않습니다.")
            context = following
        raise RuntimeError("KIS VTS 주문 조회 페이지 수가 한도를 초과했습니다.")

    def cancel_order(self, order):
        require_order_submission_enabled(order.get("execution_mode"))
        if not order.get("broker_order_id") or not order.get("broker_org_no"):
            raise ValueError("취소에 필요한 KIS 주문번호가 없습니다.")
        require_virtual_order_tr_id(CANCEL_TR_ID)
        account_no = (settings.KIS_ACCOUNT_NO or "").strip()
        cano, product_code = _parse_account_info(account_no)
        url = f"{KIS_VTS_REST_BASE_URL}{CANCEL_PATH}"
        require_vts_rest_url(url)
        response = self.post_transport(
            url,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {get_access_token()}",
                "appkey": settings.KIS_APP_KEY,
                "appsecret": settings.KIS_APP_SECRET,
                "tr_id": CANCEL_TR_ID,
                "custtype": "P",
            },
            json={
                "CANO": cano, "ACNT_PRDT_CD": product_code,
                "KRX_FWDG_ORD_ORGNO": order["broker_org_no"],
                "ORGN_ODNO": order["broker_order_id"],
                "ORD_DVSN": ORDER_DIVISIONS[order["order_type"]],
                "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX",
            },
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        output = payload.get("output") or {}
        return {
            "success": payload.get("rt_cd") == "0",
            "broker_order_id": output.get("ODNO"),
            "order_time": output.get("ORD_TMD"),
            "message_code": payload.get("msg_cd"), "message": payload.get("msg1"),
        }


def _integer(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_broker_order(item):
    quantity = _integer(item.get("ord_qty"))
    remaining = _integer(item.get("rmn_qty"))
    cancel_confirmed = _integer(item.get("cncl_cfrm_qty"))
    if item.get("tot_ccld_qty") not in (None, ""):
        filled = _integer(item.get("tot_ccld_qty"))         # KIS's filled quantity is authoritative
    else:
        filled = max(quantity - remaining - cancel_confirmed, 0)
    # cncl_yn "Y" marks KIS's cancel-request row itself; its orgn_odno is the order it cancels.
    canceled = item.get("cncl_yn") == "Y"
    original = (item.get("orgn_odno") or "").strip()
    status = (
        "CANCELED" if canceled else "FILLED" if quantity > 0 and filled >= quantity
        # Nothing left open and the rest confirmed canceled: terminal CANCELED, the same state
        # LAB's own cancel() records (remaining 0, filled quantity kept).
        else "CANCELED" if cancel_confirmed > 0 and remaining == 0
        else "PARTIALLY_FILLED" if filled > 0 else "ACKNOWLEDGED"
    )
    return {
        "broker_order_id": item.get("odno"),
        "broker_org_no": item.get("ord_gno_brno") or item.get("krx_fwdg_ord_orgno"),
        "stock_code": item.get("pdno"),
        "side": "BUY" if item.get("sll_buy_dvsn_cd") == "02" else "SELL",
        "ordered_quantity": quantity,
        "filled_quantity": filled,
        "remaining_quantity": remaining,
        "order_price": _integer(item.get("ord_unpr")),
        "average_fill_price": _number(item.get("avg_prvs")),
        "order_date": item.get("ord_dt"),
        "order_time": item.get("ord_tmd"),
        "status": status,
        "canceled": canceled,
        "cancel_confirmed_quantity": cancel_confirmed,
        "original_order_id": original if canceled and original.strip("0") else None,
    }


def canonical_execution_id(broker_order_id, cumulative_filled):
    """One broker fill has one id, whether the H0STCNI9 notice or the VTTC0081R inquiry sees it.

    The two paths share only two broker facts: the order number (ODER_NO / odno) and the order's
    cumulative filled quantity after the fill (the inquiry's tot_ccld_qty; for a notice, the
    filled quantity before it plus CNTG_QTY). Time and price are not shared: the inquiry carries
    the order time and the average price, the notice the fill's own. The source is not part of it.
    """
    return f"{(broker_order_id or '').strip().lstrip('0')}:{cumulative_filled}"


class _FillAlreadyRecorded(Exception):
    pass


def _move_position(connection, stock_code, side, quantity, price):
    row = connection.execute(
        "SELECT * FROM kis_positions WHERE stock_code=?", (stock_code,)
    ).fetchone()
    current_quantity = row["quantity"] if row else 0
    current_average = row["average_price"] if row else 0
    if side == "BUY":
        new_quantity = current_quantity + quantity
        new_average = (
            (current_quantity * current_average + quantity * price) / new_quantity
            if new_quantity else 0
        )
    else:
        new_quantity = max(0, current_quantity - quantity)
        new_average = current_average if new_quantity else 0
    connection.execute(
        """INSERT INTO kis_positions(stock_code,quantity,average_price,updated_at)
           VALUES (?,?,?,?) ON CONFLICT(stock_code) DO UPDATE SET
           quantity=excluded.quantity,average_price=excluded.average_price,
           updated_at=excluded.updated_at""",
        (stock_code, new_quantity, new_average, now_iso()),
    )


class KISOrderStore:
    def __init__(self, repository):
        self.repository = repository
        self._create_tables()

    def _create_tables(self):
        with self.repository._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS kis_virtual_orders (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    stock_code TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity BIGINT NOT NULL,
                    requested_price BIGINT NOT NULL,
                    order_type TEXT NOT NULL,
                    broker_order_id TEXT,
                    broker_org_no TEXT,
                    execution_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    filled_quantity BIGINT NOT NULL DEFAULT 0,
                    remaining_quantity BIGINT NOT NULL,
                    average_fill_price DOUBLE PRECISION,
                    created_at TEXT NOT NULL,
                    submitted_at TEXT,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS kis_order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (order_id) REFERENCES kis_virtual_orders(id)
                );
                CREATE TABLE IF NOT EXISTS kis_order_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    broker_execution_id TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    filled_quantity BIGINT NOT NULL,
                    fill_price DOUBLE PRECISION NOT NULL,
                    execution_time TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(order_id, broker_execution_id),
                    FOREIGN KEY (order_id) REFERENCES kis_virtual_orders(id)
                );
                CREATE TABLE IF NOT EXISTS kis_positions (
                    stock_code TEXT PRIMARY KEY,
                    quantity BIGINT NOT NULL,
                    average_price DOUBLE PRECISION NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kis_reconciliation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    matched BIGINT NOT NULL,
                    corrected BIGINT NOT NULL,
                    mismatch BIGINT NOT NULL,
                    manual_review_required BIGINT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kis_reconciliation_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id BIGINT NOT NULL,
                    result_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    reference TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def create(self, values):
        if self.find_by_idempotency_key(values["idempotency_key"]):
            raise DuplicateOrderError("동일 idempotency key의 주문이 이미 존재합니다.")
        order_id = str(uuid.uuid4())
        timestamp = now_iso()
        with self.repository._connect() as connection:
            connection.execute(
                """INSERT INTO kis_virtual_orders(
                   id,idempotency_key,stock_code,side,quantity,requested_price,order_type,
                   execution_mode,status,remaining_quantity,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, values["idempotency_key"], values["stock_code"], values["side"],
                 values["quantity"], values["price"], values["order_type"], "KIS_VIRTUAL",
                 "CREATED", values["quantity"], timestamp, timestamp),
            )
        self.transition(order_id, "CREATED")
        return self.get(order_id)

    def transition(self, order_id, status, details=None, filled_at_most=None, **updates):
        """`filled_at_most`: apply only while the order's filled quantity is not above it (atomic);
        otherwise nothing is written and the order is returned unchanged."""
        allowed_updates = {
            "broker_order_id", "broker_org_no", "submitted_at", "filled_quantity",
            "remaining_quantity", "average_fill_price", "last_error",
        }
        unknown = set(updates) - allowed_updates
        if unknown:
            raise ValueError("지원하지 않는 주문 상태 필드입니다.")
        assignments = ["status=?", "updated_at=?"]
        params = [status, now_iso()]
        for key, value in updates.items():
            assignments.append(f"{key}=?")
            params.append(value)
        condition = "id=?"
        params.append(order_id)
        if filled_at_most is not None:
            condition += " AND filled_quantity<=?"
            params.append(filled_at_most)
        safe_details = details or {}
        with self.repository._connect() as connection:
            changed = connection.execute(
                f"UPDATE kis_virtual_orders SET {','.join(assignments)} WHERE {condition} RETURNING id", params
            ).fetchone()
            if changed:
                connection.execute(
                    "INSERT INTO kis_order_events(order_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
                    (order_id, status, json.dumps(safe_details, ensure_ascii=False), now_iso()),
                )
        return self.get(order_id)

    def get(self, order_id):
        with self.repository._connect() as connection:
            row = connection.execute("SELECT * FROM kis_virtual_orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def list(self):
        with self.repository._connect() as connection:
            rows = connection.execute("SELECT * FROM kis_virtual_orders ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def find_by_idempotency_key(self, key):
        with self.repository._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kis_virtual_orders WHERE idempotency_key=?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def events(self, order_id):
        with self.repository._connect() as connection:
            rows = connection.execute(
                "SELECT event_type,details_json,created_at FROM kis_order_events WHERE order_id=? ORDER BY id",
                (order_id,),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def record_event(self, order_id, event_type, details=None):
        with self.repository._connect() as connection:
            connection.execute(
                "INSERT INTO kis_order_events(order_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
                (order_id, event_type, json.dumps(details or {}, ensure_ascii=False), now_iso()),
            )

    def add_execution(self, order_id, broker_execution_id, stock_code, quantity, price, execution_time):
        with self.repository._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM kis_order_executions WHERE order_id=? AND broker_execution_id=?",
                (order_id, broker_execution_id),
            ).fetchone()
            if existing:
                return False
            connection.execute(
                """INSERT INTO kis_order_executions(
                   order_id,broker_execution_id,stock_code,filled_quantity,fill_price,
                   execution_time,created_at) VALUES (?,?,?,?,?,?,?)""",
                (order_id, broker_execution_id, stock_code, quantity, price, execution_time, now_iso()),
            )
        return True

    def executions(self, order_id):
        with self.repository._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM kis_order_executions WHERE order_id=? ORDER BY id", (order_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_fill(self, order_id, filled_quantity, fill_price, execution_time):
        """Advance an order to KIS's cumulative `filled_quantity` exactly once, from any path.

        The execution row, the order's filled quantity and the LAB position move in one
        transaction or not at all. The order is re-read here, never taken from the caller: a
        notice and an inquiry that both saw "filled 0" race on the compare-and-set below, and the
        loser finds the fill already applied. Returns the order as it was before this fill, or
        None when there was nothing left to apply.
        """
        for _ in range(3):
            before = self.get(order_id)
            previous = before["filled_quantity"] or 0
            quantity = filled_quantity - previous
            if quantity <= 0:
                return None
            try:
                with self.repository._connect() as connection:
                    # The compare-and-set is the first write: it takes the order's row lock
                    # (SQLite: the database write lock) before anything else is touched.
                    advanced = connection.execute(
                        """UPDATE kis_virtual_orders SET filled_quantity=?, updated_at=?
                           WHERE id=? AND filled_quantity=? RETURNING id""",
                        (filled_quantity, now_iso(), order_id, previous),
                    ).fetchone()
                    if not advanced:
                        continue    # another path moved the order first: re-read and re-decide
                    recorded = connection.execute(
                        """INSERT INTO kis_order_executions(
                           order_id,broker_execution_id,stock_code,filled_quantity,fill_price,
                           execution_time,created_at) VALUES (?,?,?,?,?,?,?)
                           ON CONFLICT(order_id,broker_execution_id) DO NOTHING RETURNING id""",
                        (order_id, canonical_execution_id(before["broker_order_id"], filled_quantity),
                         before["stock_code"], quantity, fill_price, execution_time, now_iso()),
                    ).fetchone()
                    if not recorded:
                        raise _FillAlreadyRecorded
                    _move_position(connection, before["stock_code"], before["side"], quantity, fill_price)
                return before
            except _FillAlreadyRecorded:
                return None     # rolled back: this fill moved the position once already
        return None

    def apply_position_fill(self, stock_code, side, quantity, price):
        with self.repository._connect() as connection:
            _move_position(connection, stock_code, side, quantity, price)

    def positions(self):
        with self.repository._connect() as connection:
            rows = connection.execute("SELECT * FROM kis_positions ORDER BY stock_code").fetchall()
        return [dict(row) for row in rows]

    def save_reconciliation(self, items):
        counts = {name: sum(item["result"] == name for item in items) for name in (
            "MATCHED", "CORRECTED", "MISMATCH", "MANUAL_REVIEW_REQUIRED"
        )}
        with self.repository._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO kis_reconciliation_runs(
                   matched,corrected,mismatch,manual_review_required,created_at)
                   VALUES (?,?,?,?,?)""",
                (counts["MATCHED"], counts["CORRECTED"], counts["MISMATCH"],
                 counts["MANUAL_REVIEW_REQUIRED"], now_iso()),
            )
            run_id = cursor.lastrowid
            connection.executemany(
                """INSERT INTO kis_reconciliation_items(
                   run_id,result_type,entity_type,reference,details_json,created_at)
                   VALUES (?,?,?,?,?,?)""",
                [(run_id, item["result"], item["entity_type"], item.get("reference"),
                  json.dumps(item.get("details", {}), ensure_ascii=False), now_iso())
                 for item in items],
            )
        return {"run_id": run_id, **{key.lower(): value for key, value in counts.items()}, "items": items}

    def reconciliation_runs(self, limit=20):
        with self.repository._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM kis_reconciliation_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


class KISVirtualOrderService:
    def __init__(self, repository, client=None, price_provider=None, balance_provider=None):
        self.store = KISOrderStore(repository)
        self.client = client or KISVirtualOrderClient()
        self.price_provider = price_provider or get_current_price
        self.balance_provider = balance_provider or get_account_balance

    def preview(self, values):
        self._validate_risk(values)
        return self.client.preview_order(
            values["stock_code"], values["side"], values["quantity"],
            values["price"], values["order_type"],
        )

    def submit(self, values):
        require_order_submission_enabled(values.get("execution_mode", "KIS_VIRTUAL"))
        self._validate_risk(values)
        order = self.store.create(values)
        order = self.store.transition(order["id"], "VALIDATED")
        order = self.store.transition(order["id"], "SUBMITTING")
        try:
            result = self.client.submit_order(
                order["stock_code"], order["side"], order["quantity"],
                order["requested_price"], order["order_type"],
            )
            if not result["success"]:
                return self.store.transition(
                    order["id"], "REJECTED",
                    {"message_code": result["message_code"], "message": result["message"]},
                    last_error=result["message"],
                )
            return self.store.transition(
                order["id"], "ACKNOWLEDGED",
                {"message_code": result["message_code"], "message": result["message"]},
                broker_order_id=result["broker_order_id"],
                broker_org_no=result["broker_org_no"], submitted_at=now_iso(),
            )
        except Exception:
            self.store.transition(order["id"], "ERROR", last_error="KIS VTS 주문 제출 실패")
            raise

    def _validate_risk(self, values):
        validate_order_values(
            values["stock_code"], values["side"], values["quantity"],
            values["price"], values["order_type"],
        )
        risk_price = values["price"]
        if values["order_type"] == "MARKET":
            output = self.price_provider(values["stock_code"]).get("output", {})
            risk_price = int(output.get("stck_prpr") or 0)
            if risk_price <= 0:
                raise ValueError("시장가 위험검증용 현재가를 조회할 수 없습니다.")
        if risk_price * values["quantity"] > settings.MAX_ORDER_AMOUNT:
            raise ValueError("1회 최대 주문금액을 초과했습니다.")

    def refresh(self, inquiry_date=None):
        broker_orders = self.client.inquire_orders(inquiry_date)
        results = []
        local_by_broker_id = {
            item["broker_order_id"]: item for item in self.store.list() if item["broker_order_id"]
        }
        for broker in broker_orders:
            local = local_by_broker_id.get(broker["broker_order_id"])
            if not local:
                continue
            results.append(self._apply_broker_snapshot(local, broker))
        return results

    def _apply_broker_snapshot(self, local, broker):
        current_filled = broker["filled_quantity"]
        if current_filled > local["filled_quantity"]:
            # `local` may predate a fill notice applied meanwhile: record_fill re-reads the order.
            self.store.record_fill(
                local["id"], current_filled, broker["average_fill_price"],
                "".join(filter(None, [broker["order_date"], broker["order_time"]])),
            )
        # KIS's cumulative fill count only grows. An inquiry below what LAB already recorded lags a
        # fill notice and never takes the fill back (034020, 2026-09-29: FILLED -> ACKNOWLEDGED -> FILLED).
        return self.store.transition(
            local["id"], broker["status"], {"source": "KIS_VTS_INQUIRY"},
            filled_at_most=current_filled,
            filled_quantity=current_filled,
            remaining_quantity=broker["remaining_quantity"],
            average_fill_price=broker["average_fill_price"],
        )

    def cancel(self, order_id):
        order = self.store.get(order_id)
        if not order:
            raise ValueError("주문을 찾을 수 없습니다.")
        if order["status"] in {"FILLED", "CANCELED", "REJECTED", "ERROR"}:
            raise ValueError("현재 상태에서는 주문을 취소할 수 없습니다.")
        previous_status = order["status"]
        self.store.transition(order_id, "CANCEL_REQUESTED")
        try:
            result = self.client.cancel_order(order)
        except Exception:
            self.store.transition(order_id, previous_status, last_error="KIS VTS 취소 요청 실패")
            self.store.record_event(order_id, "CANCEL_REJECTED", {"reason": "transport_error"})
            raise
        if result["success"]:
            return self.store.transition(
                order_id, "CANCELED",
                {"message_code": result["message_code"], "message": result["message"]},
                remaining_quantity=0,
            )
        self.store.transition(order_id, previous_status, last_error=result["message"])
        self.store.record_event(
            order_id, "CANCEL_REJECTED",
            {"message_code": result["message_code"], "message": result["message"]},
        )
        return self.store.get(order_id)

    def reconcile(self, inquiry_date=None):
        require_virtual_environment()
        broker_orders = self.client.inquire_orders(inquiry_date)
        local_orders = self.store.list()
        local_by_broker = {item["broker_order_id"]: item for item in local_orders if item["broker_order_id"]}
        broker_ids = {item["broker_order_id"] for item in broker_orders}
        items = []

        for broker in broker_orders:
            local = local_by_broker.get(broker["broker_order_id"])
            if not local and broker.get("original_order_id") in local_by_broker:
                # KIS lists a cancel request as its own row pointing (orgn_odno) at the local order
                # it canceled. It is not a separate order; the original order is compared itself.
                items.append({
                    "result": "MATCHED", "entity_type": "CANCEL_REQUEST",
                    "reference": broker["broker_order_id"],
                    "details": {"original_order_id": broker["original_order_id"]},
                })
                continue
            if not local:
                items.append({
                    "result": "MANUAL_REVIEW_REQUIRED", "entity_type": "ORDER",
                    "reference": broker["broker_order_id"],
                    "details": {"reason": "KIS_ORDER_NOT_FOUND_LOCALLY"},
                })
                continue
            differs = any((
                local["status"] != broker["status"],
                local["quantity"] != broker["ordered_quantity"],
                local["filled_quantity"] != broker["filled_quantity"],
                local["remaining_quantity"] != broker["remaining_quantity"],
                broker["filled_quantity"] > 0
                and _number(local["average_fill_price"]) != broker["average_fill_price"],
            ))
            if differs:
                applied = self._apply_broker_snapshot(local, broker)
                if applied["filled_quantity"] > broker["filled_quantity"]:
                    # Not corrected: LAB keeps the fill KIS already notified; the next inquiry catches up.
                    items.append({
                        "result": "MISMATCH", "entity_type": "ORDER",
                        "reference": broker["broker_order_id"],
                        "details": {"reason": "KIS_INQUIRY_BEHIND_LAB_FILL",
                                    "local_filled_quantity": applied["filled_quantity"],
                                    "kis_filled_quantity": broker["filled_quantity"]},
                    })
                    continue
                items.append({
                    "result": "CORRECTED", "entity_type": "ORDER",
                    "reference": broker["broker_order_id"],
                    "details": {"previous_status": local["status"], "kis_status": broker["status"]},
                })
            else:
                items.append({
                    "result": "MATCHED", "entity_type": "ORDER",
                    "reference": broker["broker_order_id"], "details": {},
                })

        for local in local_orders:
            if local["broker_order_id"] and local["broker_order_id"] not in broker_ids:
                items.append({
                    "result": "MISMATCH", "entity_type": "ORDER",
                    "reference": local["broker_order_id"],
                    "details": {"reason": "LOCAL_ORDER_NOT_FOUND_AT_KIS"},
                })

        balance = self.balance_provider()
        holdings_raw = balance.get("output1") or []
        if isinstance(holdings_raw, dict):
            holdings_raw = [holdings_raw]
        kis_positions = {
            item.get("pdno"): {
                "quantity": _integer(item.get("hldg_qty")),
                "average_price": _number(item.get("pchs_avg_pric"))
                if item.get("pchs_avg_pric") not in (None, "") else None,
            }
            for item in holdings_raw if item.get("pdno")
        }
        local_positions = {item["stock_code"]: item for item in self.store.positions()}
        for stock_code in sorted(set(kis_positions) | set(local_positions)):
            local_position = local_positions.get(stock_code) or {"quantity": 0, "average_price": 0}
            kis_position = kis_positions.get(stock_code) or {"quantity": 0, "average_price": None}
            local_quantity = local_position["quantity"]
            kis_quantity = kis_position["quantity"]
            # The KIS account may also hold shares LAB never ordered. LAB only claims its own
            # quantity: a KIS surplus is an external holding, not a LAB discrepancy, and the
            # blended KIS average price says nothing about LAB's fills.
            external_surplus_qty = max(kis_quantity - local_quantity, 0)
            if kis_quantity < local_quantity:
                matched = False
            elif external_surplus_qty:
                matched = True
            else:
                matched = (
                    kis_position["average_price"] is None
                    or abs(_number(local_position["average_price"]) - kis_position["average_price"]) < 0.01
                )
            items.append({
                "result": "MATCHED" if matched else "MANUAL_REVIEW_REQUIRED",
                "entity_type": "POSITION", "reference": stock_code,
                "details": {
                    "local_quantity": local_quantity, "kis_quantity": kis_quantity,
                    "local_average_price": local_position["average_price"],
                    "kis_average_price": kis_position["average_price"],
                    "external_surplus_qty": external_surplus_qty,
                },
            })
        return self.store.save_reconciliation(items)
