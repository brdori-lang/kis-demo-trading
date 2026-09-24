import json

from fastapi.testclient import TestClient

import app.aura_routes as routes
from app.main import app


class FakeRealtimeService:
    """Stands in for RealtimeQuoteService: no approval key, no KIS WebSocket."""

    def __init__(self, events):
        self.events = events
        self.subscriptions = []
        self.stopped = False

    async def subscribe(self, stock_code):
        self.subscriptions.append(stock_code)

    async def start(self, handler):
        for event in self.events:
            await handler(event)
        await handler(None)

    async def stop(self):
        self.stopped = True


def kis_quote(stock_code="005930", price="70100"):
    return {"type": "quote", "stock_code": stock_code, "trade_time": "101530", "current_price": price,
            "change": "-400", "change_rate": "-0.57", "trade_volume": "12", "accumulated_volume": "900",
            "business_date": "20260924", "ask_price": "70200", "trading_halt": "N"}


def test_realtime_stream_requires_read_key_and_projects_public_quote_fields(monkeypatch):
    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")
    service = FakeRealtimeService([
        {"type": "connection", "state": "connected"},
        {"type": "status", "stock_code": "005930", "success": True, "message": "SUBSCRIBE SUCCESS"},
        kis_quote(),
        kis_quote(stock_code="000660"),  # not the requested symbol
        kis_quote(price=""),  # malformed price is dropped, never fabricated
    ])
    app.dependency_overrides[routes.realtime_service] = lambda: service
    app.dependency_overrides[routes.order_notifications] = lambda: None
    try:
        client = TestClient(app)
        path = "/api/integrations/aura/realtime/stream?stock_code=005930"
        assert client.get(path).status_code == 403
        assert service.subscriptions == []
        assert client.get(path.replace("005930", "5930"),
                          headers={"X-Aura-Read-Key": "test-read-key"}).status_code == 422
        with client.stream("GET", path, headers={"X-Aura-Read-Key": "test-read-key"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/x-ndjson")
            events = [json.loads(line) for line in response.iter_lines() if line]
        assert service.subscriptions == ["005930"] and service.stopped
        assert events == [
            {"type": "notifications", "state": "DISABLED"},
            {"type": "connection", "state": "connected"},
            {"type": "subscription", "success": True},
            {"type": "quote", "stock_code": "005930", "price": 70100, "change": -400, "change_rate": -0.57,
             "trade_volume": 12, "accumulated_volume": 900, "trade_time": "101530",
             "business_date": "20260924"},
        ]
    finally:
        app.dependency_overrides.clear()


def test_idle_stream_sends_heartbeat_and_stops_kis_session_when_mone_disconnects():
    import asyncio
    from aura_realtime import realtime_events

    class Silent(FakeRealtimeService):
        async def start(self, handler):
            pass

    async def scenario():
        service = Silent([])
        stream = realtime_events(service, "005930", heartbeat=0.01)
        assert json.loads(await anext(stream)) == {"type": "notifications", "state": "DISABLED"}
        assert json.loads(await anext(stream)) == {"type": "heartbeat"}
        await stream.aclose()
        return service

    assert asyncio.run(scenario()).stopped


def orderbook_frame(stock_code="005930", asks=("70200", "70300", "70400"), bids=("70100", "70000", "69900")):
    import realtime_quotes as quotes
    values = {name: "" for name in quotes.REALTIME_ORDERBOOK_COLUMNS}
    values.update(stock_code=stock_code, business_hour="101531", hour_class_code="0",
                  total_ask_quantity="5000", total_bid_quantity="6000")
    for i, (a, b) in enumerate(zip(asks, bids), start=1):
        values.update({f"ask_price_{i}": a, f"bid_price_{i}": b,
                       f"ask_quantity_{i}": str(100 * i), f"bid_quantity_{i}": str(200 * i)})
    return f"0|{quotes.REALTIME_ORDERBOOK_TR_ID}|1|" + "^".join(values[c] for c in quotes.REALTIME_ORDERBOOK_COLUMNS)


def test_h0stasp0_orderbook_is_parsed_with_official_column_order_and_subscribed_with_price():
    import asyncio
    import realtime_quotes as quotes
    from aura_realtime import public_event

    assert json.loads(quotes.subscription_message("approval", "005930", True, "H0STASP0"))["body"]["input"] == {
        "tr_id": "H0STASP0", "tr_key": "005930"}
    try:
        quotes.subscription_message("approval", "005930", True, "H0STCNI0")  # never a real-account TR
        raise AssertionError("unsupported TR accepted")
    except ValueError:
        pass
    event = quotes.parse_kis_message(orderbook_frame())
    assert event["type"] == "orderbook" and event["ask_price_1"] == "70200" and event["bid_quantity_3"] == "600"
    assert public_event(event, "005930") == {
        "type": "orderbook", "stock_code": "005930",
        "asks": [{"price": 70200, "quantity": 100}, {"price": 70300, "quantity": 200}, {"price": 70400, "quantity": 300}],
        "bids": [{"price": 70100, "quantity": 200}, {"price": 70000, "quantity": 400}, {"price": 69900, "quantity": 600}],
        "total_ask_quantity": 5000, "total_bid_quantity": 6000, "business_hour": "101531"}
    thin = public_event(quotes.parse_kis_message(orderbook_frame(asks=("70200", "", "70400"))), "005930")
    assert thin["asks"] == [{"price": 70200, "quantity": 100}]  # a gap ends the book; no interpolation
    assert public_event(quotes.parse_kis_message(orderbook_frame(asks=("",) * 3, bids=("",) * 3)), "005930") is None
    assert public_event(event, "000660") is None

    sent = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            sent.append(json.loads(message)["body"]["input"])

        async def recv(self):
            await asyncio.Event().wait()

    async def scenario():
        service = routes.realtime_service()
        service.approval_provider = lambda: asyncio.sleep(0, result="approval")
        service.connect_factory = lambda *a, **k: Socket()
        await service.subscribe("005930")
        await service.start(lambda e: asyncio.sleep(0))
        for _ in range(50):
            if len(sent) == 2:
                break
            await asyncio.sleep(0.01)
        await service.stop()

    asyncio.run(scenario())
    assert sent == [{"tr_id": "H0STCNT0", "tr_key": "005930"}, {"tr_id": "H0STASP0", "tr_key": "005930"}]


def test_order_notices_reach_mone_only_through_the_lifecycle_processor(monkeypatch):
    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")
    raw = {"type": "order_notice", "account_no": "5012345601", "customer_id": "HTSUSER1", "fill_flag": "2"}

    class Processor:
        def __init__(self):
            self.seen = []

        def apply(self, notice):
            self.seen.append(notice)
            return {"type": "order_notice", "kind": "FILL", "order_id": "ORDER-1", "status": "FILLED"}

    class Broken:
        def apply(self, notice):
            raise RuntimeError("store unavailable")

    for processor, expected in ((Processor(), [{"type": "order_notice", "kind": "FILL", "order_id": "ORDER-1",
                                                 "status": "FILLED"}]), (Broken(), [])):
        app.dependency_overrides[routes.realtime_service] = lambda: FakeRealtimeService([raw])
        app.dependency_overrides[routes.order_notifications] = lambda p=processor: p
        try:
            with TestClient(app).stream("GET", "/api/integrations/aura/realtime/stream?stock_code=005930",
                                        headers={"X-Aura-Read-Key": "test-read-key"}) as response:
                lines = [json.loads(line) for line in response.iter_lines() if line]
        finally:
            app.dependency_overrides.clear()
        assert lines[0] == {"type": "notifications", "state": "ENABLED"}
        assert lines[1:] == expected  # raw account fields never forwarded; a failing processor is skipped
        assert "5012345601" not in json.dumps(lines)
