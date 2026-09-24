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
        assert json.loads(await anext(stream)) == {"type": "heartbeat"}
        await stream.aclose()
        return service

    assert asyncio.run(scenario()).stopped
