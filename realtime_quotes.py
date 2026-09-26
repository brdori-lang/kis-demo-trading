import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx
import websockets
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from config import settings
from kis_safety import KIS_VTS_REST_BASE_URL, KIS_VTS_WEBSOCKET_URL, require_virtual_environment, require_vts_websocket_url
from security import validate_outbound_url


KIS_VTS_APPROVAL_URL = f"{KIS_VTS_REST_BASE_URL}/oauth2/Approval"
REALTIME_PRICE_TR_ID = "H0STCNT0"
REALTIME_PRICE_COLUMNS = (
    "stock_code", "trade_time", "current_price", "change_sign", "change",
    "change_rate", "weighted_average_price", "open_price", "high_price",
    "low_price", "ask_price", "bid_price", "trade_volume", "accumulated_volume",
    "accumulated_trading_value", "sell_trade_count", "buy_trade_count",
    "net_buy_trade_count", "trade_strength", "total_sell_quantity",
    "total_buy_quantity", "trade_type", "buy_ratio", "previous_volume_rate",
    "open_time", "open_change_sign", "open_change", "high_time",
    "high_change_sign", "high_change", "low_time", "low_change_sign",
    "low_change", "business_date", "market_open_code", "trading_halt",
    "ask_quantity", "bid_quantity", "total_ask_quantity", "total_bid_quantity",
    "volume_turnover_rate", "previous_same_time_volume",
    "previous_same_time_volume_rate", "hour_class_code", "market_close_code",
    "vi_standard_price",
)
REALTIME_ORDERBOOK_TR_ID = "H0STASP0"
REALTIME_ORDERBOOK_COLUMNS = (
    "stock_code", "business_hour", "hour_class_code",
    *(f"ask_price_{i}" for i in range(1, 11)), *(f"bid_price_{i}" for i in range(1, 11)),
    *(f"ask_quantity_{i}" for i in range(1, 11)), *(f"bid_quantity_{i}" for i in range(1, 11)),
    "total_ask_quantity", "total_bid_quantity",
)
# tr_id -> (event type, official column order). Only market data TRs keyed by stock code.
REALTIME_MARKET_TRS = {
    REALTIME_PRICE_TR_ID: ("quote", REALTIME_PRICE_COLUMNS),
    REALTIME_ORDERBOOK_TR_ID: ("orderbook", REALTIME_ORDERBOOK_COLUMNS),
}
# 국내주식 실시간체결통보 [실시간-005]: VTS TR only. The real-account H0STCNI0 is never subscribed.
REALTIME_ORDER_NOTICE_TR_ID = "H0STCNI9"
REALTIME_ORDER_NOTICE_COLUMNS = (
    "customer_id", "account_no", "order_no", "original_order_no", "side_code",
    "receipt_class", "order_kind", "order_condition", "stock_code", "fill_quantity",
    "fill_price", "fill_time", "rejected", "fill_flag", "acceptance", "branch_no",
    "order_quantity", "account_name", "condition_price", "exchange", "popup", "filler",
    "credit_class", "credit_loan_date", "stock_name", "order_price",
)
ORDER_NOTICE_MIN_FIELDS = 17  # through ORDER_QTY; later fields are not needed for the lifecycle

logger = logging.getLogger(__name__)
EventHandler = Callable[[dict], Awaitable[None]]


async def issue_vts_approval_key() -> str:
    require_virtual_environment()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            validate_outbound_url(KIS_VTS_APPROVAL_URL),
            headers={"content-type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "appkey": settings.KIS_APP_KEY,
                "secretkey": settings.KIS_APP_SECRET,
            },
        )
    response.raise_for_status()
    approval_key = response.json().get("approval_key")
    if not approval_key:
        raise RuntimeError("KIS WebSocket approval key 발급에 실패했습니다.")
    return approval_key


def subscription_message(approval_key: str, stock_code: str, subscribe: bool,
                         tr_id: str = REALTIME_PRICE_TR_ID) -> str:
    if len(stock_code) != 6 or not stock_code.isdigit():
        raise ValueError("종목코드는 6자리 숫자여야 합니다.")
    if tr_id not in REALTIME_MARKET_TRS:
        raise ValueError("지원하지 않는 실시간 TR입니다.")
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": stock_code}},
        },
        ensure_ascii=False,
    )


# tr_key = HTS ID: string, length 12 [실시간-005]. Printable ASCII without spaces.
HTS_ID_PATTERN = re.compile(r"[!-~]{1,12}")


def valid_hts_id(hts_id: str | None) -> bool:
    return bool(hts_id) and HTS_ID_PATTERN.fullmatch(hts_id) is not None


def notice_subscription_message(approval_key: str, hts_id: str, subscribe: bool) -> str:
    if not valid_hts_id(hts_id):
        raise ValueError("HTS ID 형식이 올바르지 않습니다.")
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": REALTIME_ORDER_NOTICE_TR_ID, "tr_key": hts_id}},
        },
        ensure_ascii=False,
    )


def subscription_cipher(message: str) -> tuple[str, bytes, bytes] | None:
    """AES256 key/IV from a SUBSCRIBE SUCCESS response (body.output.key / body.output.iv)."""
    try:
        payload = json.loads(message)
        output = payload["body"]["output"]
        key, iv = output["key"].encode("utf-8"), output["iv"].encode("utf-8")
        tr_id = payload["header"]["tr_id"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if len(key) != 32 or len(iv) != 16:
        return None
    return tr_id, key, iv


def decrypt_notice(cipher_text: str, key: bytes, iv: bytes) -> str:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(base64.b64decode(cipher_text, validate=True)) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def _parse_order_notice(encrypted: bool, body: str, ciphers: dict | None) -> dict | None:
    # A notice that cannot be decrypted or parsed is dropped, never guessed; VTTC0081R
    # reconciliation stays authoritative. Raw values (account, name) never leave the LAB.
    if encrypted:
        cipher = (ciphers or {}).get(REALTIME_ORDER_NOTICE_TR_ID)
        if not cipher:
            logger.warning("KIS VTS order notice received before its AES key; ignored")
            return None
        try:
            body = decrypt_notice(body, *cipher)
        except (ValueError, UnicodeDecodeError):
            logger.warning("KIS VTS order notice could not be decrypted; ignored")
            return None
    values = body.split("^")
    if len(values) < ORDER_NOTICE_MIN_FIELDS:
        logger.warning("KIS VTS order notice has too few fields; ignored")
        return None
    return {"type": "order_notice", **dict(zip(REALTIME_ORDER_NOTICE_COLUMNS, values))}


def parse_kis_message(message: str, ciphers: dict | None = None) -> dict | None:
    if not message:
        return None
    if message[0] in {"0", "1"}:
        parts = message.split("|", 3)
        if len(parts) == 4 and parts[1] == REALTIME_ORDER_NOTICE_TR_ID:
            return _parse_order_notice(message[0] == "1", parts[3], ciphers)
        if len(parts) != 4 or parts[1] not in REALTIME_MARKET_TRS:
            return None
        event_type, columns = REALTIME_MARKET_TRS[parts[1]]
        values = parts[3].split("^")
        if len(values) < len(columns):
            raise ValueError("KIS 실시간 시세 필드 수가 올바르지 않습니다.")
        return {"type": event_type, **dict(zip(columns, values))}

    payload = json.loads(message)
    if payload.get("header", {}).get("tr_id") == "PINGPONG":
        return {"type": "ping", "raw": message}
    body = payload.get("body", {})
    header = payload.get("header", {})
    return {
        "type": "status",
        # The order-notice tr_key is the HTS ID, not a stock code; it is never echoed.
        "stock_code": None if header.get("tr_id") == REALTIME_ORDER_NOTICE_TR_ID else header.get("tr_key"),
        "success": body.get("rt_cd") == "0",
        "message": body.get("msg1", ""),
    }


class RealtimeQuoteService:
    def __init__(self, approval_provider=issue_vts_approval_key, connect_factory=websockets.connect,
                 tr_ids: tuple[str, ...] = (REALTIME_PRICE_TR_ID,), notice_tr_key: str | None = None):
        if not tr_ids or any(tr_id not in REALTIME_MARKET_TRS for tr_id in tr_ids):
            raise ValueError("지원하지 않는 실시간 TR입니다.")
        self.approval_provider = approval_provider
        self.connect_factory = connect_factory
        self.tr_ids = tuple(tr_ids)
        if notice_tr_key and not valid_hts_id(notice_tr_key):
            logger.warning("KIS_HTS_ID format is invalid; realtime order notices stay off")
            notice_tr_key = None  # never let a bad HTS ID take market data down with it
        self.notice_tr_key = notice_tr_key  # HTS ID for H0STCNI9; None keeps notices off
        self._ciphers: dict[str, tuple[bytes, bytes]] = {}
        self.subscriptions: set[str] = set()
        self.commands: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.state = "disconnected"
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self, handler: EventHandler):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(handler))

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self.state = "disconnected"

    async def subscribe(self, stock_code: str):
        if len(stock_code) != 6 or not stock_code.isdigit():
            raise ValueError("종목코드는 6자리 숫자여야 합니다.")
        if stock_code not in self.subscriptions:
            self.subscriptions.add(stock_code)
            if self.state == "connected":
                await self.commands.put(("subscribe", stock_code))

    async def unsubscribe(self, stock_code: str):
        if stock_code in self.subscriptions:
            self.subscriptions.remove(stock_code)
            if self.state == "connected":
                await self.commands.put(("unsubscribe", stock_code))

    async def _run(self, handler: EventHandler):
        retry = 0
        while not self._stop.is_set():
            try:
                self.state = "connecting"
                await handler({"type": "connection", "state": self.state})
                approval_key = await self.approval_provider()
                require_vts_websocket_url(KIS_VTS_WEBSOCKET_URL)
                async with self.connect_factory(
                    KIS_VTS_WEBSOCKET_URL, ping_interval=30, ping_timeout=20, close_timeout=5
                ) as websocket:
                    self.state = "connected"
                    retry = 0
                    self._ciphers.clear()  # every subscription answers with its own AES key/IV
                    await handler({"type": "connection", "state": self.state})
                    # The full set is (re)sent right here, so commands queued before this point are stale
                    # (they would subscribe twice or unsubscribe a symbol that is no longer registered).
                    while not self.commands.empty():
                        self.commands.get_nowait()
                    for stock_code in sorted(self.subscriptions):
                        for tr_id in self.tr_ids:
                            await websocket.send(subscription_message(approval_key, stock_code, True, tr_id))
                    if self.notice_tr_key:
                        await websocket.send(notice_subscription_message(approval_key, self.notice_tr_key, True))
                    await self._connected_loop(websocket, approval_key, handler)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state = "reconnecting"
                retry += 1
                delay = min(30, 2 ** min(retry - 1, 5))
                logger.warning("KIS VTS WebSocket reconnect in %ss: %s", delay, type(error).__name__)
                await handler({"type": "connection", "state": self.state, "retry_in": delay})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def _connected_loop(self, websocket, approval_key: str, handler: EventHandler):
        while not self._stop.is_set():
            receive_task = asyncio.create_task(websocket.recv())
            command_task = asyncio.create_task(self.commands.get())
            tasks = {receive_task, command_task}
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                # Drain child tasks even when the browser disconnects during wait.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if receive_task in done:
                raw = receive_task.result()
                if raw and raw[0] == "{" and (cipher := subscription_cipher(raw)):
                    self._ciphers[cipher[0]] = cipher[1:]
                event = parse_kis_message(raw, self._ciphers)
                if event and event["type"] == "ping":
                    await websocket.send(event["raw"])
                elif event:
                    await handler(event)
            if command_task in done:
                action, stock_code = command_task.result()
                for tr_id in self.tr_ids:
                    await websocket.send(
                        subscription_message(approval_key, stock_code, action == "subscribe", tr_id)
                    )
