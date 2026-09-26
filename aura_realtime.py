"""M:ONE realtime relay over the existing KIS VTS WebSocket service.

The LAB keeps owning approval keys, the KIS WebSocket session, PINGPONG and reconnects
(realtime_quotes.RealtimeQuoteService). M:ONE only receives a read-key authenticated NDJSON
stream of projected public fields; raw KIS frames and credentials never leave the LAB.
"""
import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator


HEARTBEAT_SECONDS = 15.0
# KIS allows 41 realtime registrations per session: 2 market TRs per symbol + the order notice TR.
MAX_STREAM_SYMBOLS = 20
logger = logging.getLogger(__name__)
_CODE = re.compile(r"[0-9]{6}")
# Open M:ONE collector streams by id, so M:ONE can change a stream's symbols on its existing KIS session.
ACTIVE_STREAMS: dict[str, object] = {}


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


ORDERBOOK_DEPTH = 3  # M:ONE shows a compact 3-level book; the full 10 levels stay in the LAB


def _levels(event: dict, side: str) -> list[dict]:
    levels = []
    for i in range(1, ORDERBOOK_DEPTH + 1):
        price = _int(event.get(f"{side}_price_{i}"))
        if not price or price <= 0:
            break  # never interpolate a missing level
        levels.append({"price": price, "quantity": _int(event.get(f"{side}_quantity_{i}"))})
    return levels


def public_orderbook(event: dict) -> dict | None:
    asks, bids = _levels(event, "ask"), _levels(event, "bid")
    if not asks and not bids:
        return None
    return {
        "type": "orderbook",
        "stock_code": event.get("stock_code"),
        "asks": asks,
        "bids": bids,
        "total_ask_quantity": _int(event.get("total_ask_quantity")),
        "total_bid_quantity": _int(event.get("total_bid_quantity")),
        "business_hour": event.get("business_hour") or None,
    }


def public_event(event: dict, stock_codes: str | frozenset[str]) -> dict | None:
    if isinstance(stock_codes, str):
        stock_codes = frozenset((stock_codes,))
    kind = event.get("type")
    if kind == "connection":
        return {"type": "connection", "state": event.get("state")}
    if kind == "status":
        item = {"type": "subscription", "success": bool(event.get("success"))}
        code = event.get("stock_code")
        if isinstance(code, str) and _CODE.fullmatch(code):
            item["stock_code"] = code   # which symbol's (un)subscribe KIS answered; never the HTS ID
        return item
    if kind == "quote" and event.get("stock_code") in stock_codes:
        return public_quote(event)
    if kind == "orderbook" and event.get("stock_code") in stock_codes:
        return public_orderbook(event)
    return None


def _apply_notice(notices, event: dict) -> dict | None:
    if notices is None:
        return None
    try:
        return notices.apply(event)
    except Exception:  # the lifecycle falls back to polling/reconciliation; never crash the relay
        logger.exception("KIS VTS order notice could not be applied")
        return None


async def update_stream_symbols(stream_id: str, stock_codes: list[str]) -> dict | None:
    """Make an open stream serve exactly these symbols on its existing KIS session.

    Only the market TRs (H0STCNT0/H0STASP0) of added/removed symbols are (un)subscribed through the
    service's own subscribe/unsubscribe; the WebSocket, its approval key and the order notice
    subscription (H0STCNI9) are untouched. Returns None when no such stream is open.
    """
    service = ACTIVE_STREAMS.get(stream_id)
    if service is None:
        return None
    codes = list(dict.fromkeys(stock_codes))
    if not codes or len(codes) > MAX_STREAM_SYMBOLS or not all(isinstance(c, str) and _CODE.fullmatch(c) for c in codes):
        raise ValueError("unsupported realtime symbols")
    current = set(service.subscriptions)
    removed, added = sorted(current - set(codes)), [code for code in codes if code not in current]
    for code in removed:
        await service.unsubscribe(code)
    for code in added:
        await service.subscribe(code)
    return {"stream_id": stream_id, "stock_codes": sorted(service.subscriptions), "added": added, "removed": removed}


async def realtime_events(service, stock_codes: str | list[str], heartbeat: float = HEARTBEAT_SECONDS,
                          notices=None, stream_id: str | None = None) -> AsyncIterator[str]:
    """Yield NDJSON lines until the client disconnects; always stops the KIS session.

    One KIS WebSocket session serves every requested symbol (M:ONE's market collector asks for its
    whole watchlist at once instead of opening a session per symbol). With a stream_id the stream
    announces it first and its symbols can be changed later (update_stream_symbols) without a reconnect.
    """
    codes = [stock_codes] if isinstance(stock_codes, str) else list(dict.fromkeys(stock_codes))
    if not codes or len(codes) > MAX_STREAM_SYMBOLS:
        raise ValueError("unsupported number of realtime symbols")
    queue: asyncio.Queue = asyncio.Queue()
    for code in codes:
        await service.subscribe(code)
    await service.start(queue.put)
    if stream_id:
        ACTIVE_STREAMS[stream_id] = service
    try:
        if stream_id:
            yield json.dumps({"type": "stream", "stream_id": stream_id}) + "\n"
        yield json.dumps({"type": "notifications", "state": "ENABLED" if notices is not None else "DISABLED"}) + "\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), heartbeat)
            except TimeoutError:
                yield json.dumps({"type": "heartbeat"}) + "\n"
                continue
            if event is None:  # service closed the relay
                return
            if event.get("type") == "order_notice":
                item = _apply_notice(notices, event)  # projected by the processor; raw account fields dropped
            else:
                item = public_event(event, frozenset(service.subscriptions))   # live set: updates apply
            if item:
                yield json.dumps(item, ensure_ascii=False) + "\n"
    finally:
        if stream_id:
            ACTIVE_STREAMS.pop(stream_id, None)
        await service.stop()
