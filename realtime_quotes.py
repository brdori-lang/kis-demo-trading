import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx
import websockets

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


def subscription_message(approval_key: str, stock_code: str, subscribe: bool) -> str:
    if len(stock_code) != 6 or not stock_code.isdigit():
        raise ValueError("종목코드는 6자리 숫자여야 합니다.")
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": REALTIME_PRICE_TR_ID, "tr_key": stock_code}},
        },
        ensure_ascii=False,
    )


def parse_kis_message(message: str) -> dict | None:
    if not message:
        return None
    if message[0] in {"0", "1"}:
        parts = message.split("|", 3)
        if len(parts) != 4 or parts[1] != REALTIME_PRICE_TR_ID:
            return None
        values = parts[3].split("^")
        if len(values) < len(REALTIME_PRICE_COLUMNS):
            raise ValueError("KIS 실시간 체결가 필드 수가 올바르지 않습니다.")
        return {"type": "quote", **dict(zip(REALTIME_PRICE_COLUMNS, values))}

    payload = json.loads(message)
    if payload.get("header", {}).get("tr_id") == "PINGPONG":
        return {"type": "ping", "raw": message}
    body = payload.get("body", {})
    return {
        "type": "status",
        "stock_code": payload.get("header", {}).get("tr_key"),
        "success": body.get("rt_cd") == "0",
        "message": body.get("msg1", ""),
    }


class RealtimeQuoteService:
    def __init__(self, approval_provider=issue_vts_approval_key, connect_factory=websockets.connect):
        self.approval_provider = approval_provider
        self.connect_factory = connect_factory
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
                    await handler({"type": "connection", "state": self.state})
                    for stock_code in sorted(self.subscriptions):
                        await websocket.send(subscription_message(approval_key, stock_code, True))
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
            done, pending = await asyncio.wait(
                {receive_task, command_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if receive_task in done:
                raw = receive_task.result()
                event = parse_kis_message(raw)
                if event and event["type"] == "ping":
                    await websocket.send(event["raw"])
                elif event:
                    await handler(event)
            if command_task in done:
                action, stock_code = command_task.result()
                await websocket.send(
                    subscription_message(approval_key, stock_code, action == "subscribe")
                )
