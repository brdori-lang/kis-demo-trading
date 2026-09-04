import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import realtime_quotes as quotes


def quote_frame(stock_code="005930", price="70000", change="1000", rate="1.45"):
    values = [""] * len(quotes.REALTIME_PRICE_COLUMNS)
    values[0] = stock_code
    values[1] = "101530"
    values[2] = price
    values[4] = change
    values[5] = rate
    return f"0|{quotes.REALTIME_PRICE_TR_ID}|1|" + "^".join(values)


def test_subscription_contract_is_vts_only_and_uses_official_shape():
    payload = json.loads(quotes.subscription_message("approval", "005930", True))

    assert quotes.KIS_VTS_WEBSOCKET_URL == "ws://ops.koreainvestment.com:31000"
    assert "21000" not in quotes.KIS_VTS_WEBSOCKET_URL
    assert payload["header"] == {
        "approval_key": "approval",
        "custtype": "P",
        "tr_type": "1",
        "content-type": "utf-8",
    }
    assert payload["body"]["input"] == {"tr_id": "H0STCNT0", "tr_key": "005930"}
    assert json.loads(quotes.subscription_message("approval", "005930", False))["header"]["tr_type"] == "2"


def test_realtime_price_and_ping_frames_are_parsed():
    event = quotes.parse_kis_message(quote_frame())

    assert event["type"] == "quote"
    assert event["stock_code"] == "005930"
    assert event["current_price"] == "70000"
    assert event["change"] == "1000"
    assert event["change_rate"] == "1.45"
    ping = '{"header":{"tr_id":"PINGPONG"},"body":{}}'
    assert quotes.parse_kis_message(ping) == {"type": "ping", "raw": ping}


def test_non_virtual_environment_is_rejected_before_network(monkeypatch):
    monkeypatch.setattr(quotes.settings, "KIS_ENV", "real")

    async def run():
        with pytest.raises(RuntimeError, match="virtual"):
            await quotes.issue_vts_approval_key()

    asyncio.run(run())


def test_service_reconnects_and_resubscribes_with_fake_websocket():
    asyncio.run(_assert_service_reconnects())


async def _assert_service_reconnects():
    connected = []
    quote_received = asyncio.Event()

    class FakeSocket:
        def __init__(self, fail=False):
            self.fail = fail
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            self.sent.append(message)

        async def recv(self):
            if self.fail:
                raise ConnectionError("closed")
            if not quote_received.is_set():
                return quote_frame()
            await asyncio.Event().wait()

    sockets = []

    def connect_factory(*args, **kwargs):
        socket = FakeSocket(fail=not sockets)
        sockets.append(socket)
        return socket

    async def handler(event):
        connected.append(event)
        if event.get("type") == "quote":
            quote_received.set()

    service = quotes.RealtimeQuoteService(
        approval_provider=lambda: asyncio.sleep(0, result="approval"),
        connect_factory=connect_factory,
    )
    await service.subscribe("005930")
    await service.start(handler)
    await asyncio.wait_for(quote_received.wait(), timeout=3)
    await service.stop()

    assert len(sockets) >= 2
    assert any(event.get("state") == "reconnecting" for event in connected)
    assert json.loads(sockets[1].sent[0])["body"]["input"]["tr_key"] == "005930"


def test_internal_websocket_route_forwards_fake_quote(monkeypatch):
    class FakeService:
        async def start(self, handler):
            self.handler = handler
            await handler({"type": "connection", "state": "connected"})

        async def subscribe(self, stock_code):
            await self.handler({
                "type": "quote", "stock_code": stock_code,
                "current_price": "70000", "change": "1000", "change_rate": "1.45",
            })

        async def unsubscribe(self, stock_code):
            return None

        async def stop(self):
            return None

    monkeypatch.setattr(main_module, "RealtimeQuoteService", FakeService)
    client = TestClient(main_module.app)

    with client.websocket_connect("/ws/quotes") as websocket:
        assert websocket.receive_json() == {"type": "connection", "state": "connected"}
        websocket.send_json({"action": "subscribe", "stock_code": "005930"})
        event = websocket.receive_json()

    assert event["type"] == "quote"
    assert event["stock_code"] == "005930"
