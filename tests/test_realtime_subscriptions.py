"""M:ONE collector stream: symbols change on the one existing KIS VTS WebSocket session (no reconnect).

No approval key, no KIS WebSocket, no order: fake socket / fake service only.
"""
import asyncio
import json

from fastapi.testclient import TestClient

import aura_realtime
import app.aura_routes as routes
import realtime_quotes as quotes
from app.main import app


class Socket:
    """One fake KIS WebSocket that records every (tr_id, tr_key, tr_type) it is sent."""

    def __init__(self, sent):
        self.sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def send(self, message):
        body = json.loads(message)
        self.sent.append((body["body"]["input"]["tr_id"], body["body"]["input"]["tr_key"], body["header"]["tr_type"]))

    async def recv(self):
        await asyncio.Event().wait()


async def settle(sent, count):
    for _ in range(200):
        if len(sent) >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"sent {sent}")


def test_symbols_are_added_and_removed_on_the_same_kis_session_and_notices_stay():
    sent, connects, approvals = [], [], []

    def connect(*args, **kwargs):
        connects.append(args)
        return Socket(sent)

    async def approval():
        approvals.append(1)
        return "approval"

    async def scenario():
        service = quotes.RealtimeQuoteService(approval_provider=approval, connect_factory=connect,
                                              tr_ids=(quotes.REALTIME_PRICE_TR_ID, quotes.REALTIME_ORDERBOOK_TR_ID),
                                              notice_tr_key="HTSUSER1")
        for code in ("005930", "000660"):
            await service.subscribe(code)
        await service.start(lambda e: asyncio.sleep(0))
        await settle(sent, 5)
        initial = list(sent)
        await service.subscribe("373220")                          # add
        await service.subscribe("373220")                          # duplicate add: no-op
        await service.unsubscribe("000660")                        # remove
        await service.unsubscribe("035420")                        # unknown remove: no-op
        await settle(sent, 9)
        await asyncio.sleep(0.05)
        changes = sent[len(initial):]
        await service.stop()
        return initial, changes, sorted(service.subscriptions)

    initial, changes, remaining = asyncio.run(scenario())
    assert initial == [("H0STCNT0", "000660", "1"), ("H0STASP0", "000660", "1"), ("H0STCNT0", "005930", "1"),
                       ("H0STASP0", "005930", "1"), ("H0STCNI9", "HTSUSER1", "1")]
    assert changes == [("H0STCNT0", "373220", "1"), ("H0STASP0", "373220", "1"),
                       ("H0STCNT0", "000660", "2"), ("H0STASP0", "000660", "2")]   # market TRs only
    assert len(connects) == 1 and len(approvals) == 1                                   # never reconnected
    assert remaining == ["005930", "373220"]


def test_commands_queued_before_a_reconnect_are_not_sent_twice():
    sent = []

    async def scenario():
        service = quotes.RealtimeQuoteService(approval_provider=lambda: asyncio.sleep(0, result="approval"),
                                              connect_factory=lambda *a, **k: Socket(sent),
                                              tr_ids=(quotes.REALTIME_PRICE_TR_ID,))
        service.state = "connected"                                   # link just dropped, loop not yet reset
        await service.subscribe("005930")                             # queued AND in the set
        await service.start(lambda e: asyncio.sleep(0))
        await settle(sent, 1)
        await asyncio.sleep(0.05)
        await service.stop()

    asyncio.run(scenario())
    assert sent == [("H0STCNT0", "005930", "1")]


class FakeService:
    def __init__(self, events=()):
        self.events, self.subscriptions, self.calls, self.stopped = list(events), set(), [], False
        self.handler = None

    async def subscribe(self, code):
        if code not in self.subscriptions:
            self.subscriptions.add(code)
            self.calls.append(("subscribe", code))

    async def unsubscribe(self, code):
        if code in self.subscriptions:
            self.subscriptions.discard(code)
            self.calls.append(("unsubscribe", code))

    async def start(self, handler):
        self.handler = handler

    async def stop(self):
        self.stopped = True


def kis_quote(code):
    return {"type": "quote", "stock_code": code, "trade_time": "101530", "current_price": "70100", "change": "0",
            "change_rate": "0", "trade_volume": "1", "accumulated_volume": "10", "business_date": "20260928"}


def test_an_open_stream_changes_symbols_through_its_registered_service():
    async def scenario():
        service = FakeService()
        stream = aura_realtime.realtime_events(service, ["005930", "000660"], heartbeat=5, stream_id="S" * 22)
        assert json.loads(await anext(stream)) == {"type": "stream", "stream_id": "S" * 22}
        assert json.loads(await anext(stream))["type"] == "notifications"
        assert aura_realtime.ACTIVE_STREAMS["S" * 22] is service
        result = await aura_realtime.update_stream_symbols("S" * 22, ["005930", "373220"])
        assert result == {"stream_id": "S" * 22, "stock_codes": ["005930", "373220"], "added": ["373220"],
                          "removed": ["000660"]}
        assert await aura_realtime.update_stream_symbols("S" * 22, ["005930", "373220"]) == {
            "stream_id": "S" * 22, "stock_codes": ["005930", "373220"], "added": [], "removed": []}   # idempotent
        for code in ("000660", "373220"):                             # removed symbol is filtered, added passes
            await service.handler(kis_quote(code))
        assert json.loads(await anext(stream))["stock_code"] == "373220"
        for bad in (["5930"], [], [f"{n:06d}" for n in range(21)]):
            try:
                await aura_realtime.update_stream_symbols("S" * 22, bad)
                raise AssertionError("accepted")
            except ValueError:
                pass
        assert await aura_realtime.update_stream_symbols("unknown-stream-id", ["005930"]) is None
        await stream.aclose()
        return service

    service = asyncio.run(scenario())
    assert service.calls == [("subscribe", "005930"), ("subscribe", "000660"), ("unsubscribe", "000660"),
                             ("subscribe", "373220")]
    assert service.stopped and aura_realtime.ACTIVE_STREAMS == {}


def test_subscriptions_route_is_read_key_protected_and_addresses_one_open_stream(monkeypatch):
    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")
    service = FakeService()
    service.subscriptions = {"005930"}
    monkeypatch.setitem(aura_realtime.ACTIVE_STREAMS, "stream-0123456789ab", service)
    client, headers = TestClient(app), {"X-Aura-Read-Key": "test-read-key"}
    path = "/api/integrations/aura/realtime/subscriptions"
    body = {"stream_id": "stream-0123456789ab", "stock_codes": ["005930", "373220"]}
    assert client.put(path, json=body).status_code == 403
    assert service.calls == []
    response = client.put(path, json=body, headers=headers)
    assert response.status_code == 200 and response.json()["added"] == ["373220"]
    assert client.put(path, json={**body, "stream_id": "missing-0123456789"}, headers=headers).status_code == 404
    for bad in ({**body, "stock_codes": ["5930"]}, {**body, "stock_codes": []}, {**body, "stream_id": "x"},
                {**body, "stock_codes": [f"{n:06d}" for n in range(21)]}):
        assert client.put(path, json=bad, headers=headers).status_code == 422
    assert service.calls == [("subscribe", "373220")]


def test_only_the_multi_symbol_collector_stream_gets_a_stream_id(monkeypatch):
    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")

    class Ending(FakeService):
        async def start(self, handler):
            await handler(None)

    app.dependency_overrides[routes.realtime_service] = Ending
    app.dependency_overrides[routes.order_notifications] = lambda: None
    try:
        client, headers = TestClient(app), {"X-Aura-Read-Key": "test-read-key"}
        base = "/api/integrations/aura/realtime/stream"
        multi = [json.loads(l) for l in client.get(f"{base}?stock_codes=005930,000660", headers=headers).iter_lines() if l]
        single = [json.loads(l) for l in client.get(f"{base}?stock_code=005930", headers=headers).iter_lines() if l]
    finally:
        app.dependency_overrides.clear()
    assert multi[0]["type"] == "stream" and len(multi[0]["stream_id"]) >= 16
    assert single[0]["type"] == "notifications"
    assert aura_realtime.ACTIVE_STREAMS == {}                       # closed streams are unregistered


def test_subscription_answers_name_the_symbol_but_never_the_hts_id():
    assert aura_realtime.public_event({"type": "status", "stock_code": "373220", "success": False}, "005930") == {
        "type": "subscription", "success": False, "stock_code": "373220"}
    assert aura_realtime.public_event({"type": "status", "stock_code": None, "success": True}, "005930") == {
        "type": "subscription", "success": True}                  # H0STCNI9 answer: tr_key is the HTS ID
    assert aura_realtime.public_event({"type": "status", "stock_code": "HTSUSER1", "success": True}, "005930") == {
        "type": "subscription", "success": True}
