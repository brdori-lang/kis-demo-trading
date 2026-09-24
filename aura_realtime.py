"""M:ONE realtime relay over the existing KIS VTS WebSocket service.

The LAB keeps owning approval keys, the KIS WebSocket session, PINGPONG and reconnects
(realtime_quotes.RealtimeQuoteService). M:ONE only receives a read-key authenticated NDJSON
stream of projected public fields; raw KIS frames and credentials never leave the LAB.
"""
import asyncio
import json
from collections.abc import AsyncIterator


HEARTBEAT_SECONDS = 15.0


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def public_quote(event: dict) -> dict | None:
    price = _int(event.get("current_price"))
    if not price or price <= 0:
        return None
    return {
        "type": "quote",
        "stock_code": event.get("stock_code"),
        "price": price,
        "change": _int(event.get("change")),
        "change_rate": _float(event.get("change_rate")),
        "trade_volume": _int(event.get("trade_volume")),
        "accumulated_volume": _int(event.get("accumulated_volume")),
        "trade_time": event.get("trade_time") or None,
        "business_date": event.get("business_date") or None,
    }


def public_event(event: dict, stock_code: str) -> dict | None:
    kind = event.get("type")
    if kind == "connection":
        return {"type": "connection", "state": event.get("state")}
    if kind == "status":
        return {"type": "subscription", "success": bool(event.get("success"))}
    if kind == "quote" and event.get("stock_code") == stock_code:
        return public_quote(event)
    return None


async def realtime_events(service, stock_code: str, heartbeat: float = HEARTBEAT_SECONDS) -> AsyncIterator[str]:
    """Yield NDJSON lines until the client disconnects; always stops the KIS session."""
    queue: asyncio.Queue = asyncio.Queue()
    await service.subscribe(stock_code)
    await service.start(queue.put)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), heartbeat)
            except TimeoutError:
                yield json.dumps({"type": "heartbeat"}) + "\n"
                continue
            if event is None:  # service closed the relay
                return
            item = public_event(event, stock_code)
            if item:
                yield json.dumps(item, ensure_ascii=False) + "\n"
    finally:
        await service.stop()
