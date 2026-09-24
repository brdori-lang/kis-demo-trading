"""Rehearsal: encrypted H0STCNI9 frames -> RealtimeQuoteService -> relay -> lifecycle -> VTTC0081R.

Fake KIS WebSocket frames only (AES256 with the key/IV from the fake SUBSCRIBE SUCCESS); no KIS
connection and no order. Proves ACK -> PARTIAL -> FILLED in realtime, that a repeated fill notice
is not double counted, and that the authoritative VTTC0081R inquiry afterwards adds nothing.
"""
import asyncio
import json

import kis_virtual_orders as orders
import realtime_quotes as quotes
from aura_realtime import realtime_events
from lab_repository import SQLiteLabRepository
from order_notifications import OrderNotificationProcessor
from test_order_notifications import SUBSCRIBE_SUCCESS, encrypted_frame, notice_values


class InquiryClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def inquire_orders(self, inquiry_date=None):
        return [self.snapshot]


def test_encrypted_notices_drive_the_lifecycle_and_reconciliation_stays_authoritative(tmp_path, monkeypatch):
    monkeypatch.setattr(orders.settings, "KIS_ENV", "virtual")
    repository = SQLiteLabRepository(tmp_path / "rehearsal.db")
    store = orders.KISOrderStore(repository)
    order = store.create({"idempotency_key": "rehearsal", "stock_code": "005930", "side": "BUY",
                          "quantity": 2, "price": 70000, "order_type": "LIMIT"})
    order = store.transition(order["id"], "ACKNOWLEDGED", broker_order_id="0000012345", broker_org_no="06010")

    fill_1 = notice_values(fill_flag="2", fill_qty="1", fill_price="70100", order_qty="2", time="101530")
    fill_2 = notice_values(fill_flag="2", fill_qty="1", fill_price="70200", order_qty="2", time="101531")
    frames = [SUBSCRIBE_SUCCESS, encrypted_frame(notice_values(order_qty="2")),
              encrypted_frame(fill_1), encrypted_frame(fill_1), encrypted_frame(fill_2)]

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            return None

        async def recv(self):
            if frames:
                return frames.pop(0)
            await asyncio.Event().wait()

    async def relay():
        service = quotes.RealtimeQuoteService(approval_provider=lambda: asyncio.sleep(0, result="approval"),
                                              connect_factory=lambda *a, **k: Socket(), notice_tr_key="HTSUSER1")
        stream = realtime_events(service, "005930", notices=OrderNotificationProcessor(store))
        seen = []
        try:
            async with asyncio.timeout(5):
                async for line in stream:
                    event = json.loads(line)
                    if event["type"] == "order_notice":
                        seen.append(event)
                    if len(seen) == 4:
                        break
        finally:
            await stream.aclose()
        return seen

    seen = asyncio.run(relay())
    assert [(e["kind"], e["status"], e["filled_quantity"]) for e in seen] == [
        ("ACCEPTED", "ACKNOWLEDGED", 0), ("FILL", "PARTIALLY_FILLED", 1),
        ("FILL", "PARTIALLY_FILLED", 1),  # the repeated notice changes nothing
        ("FILL", "FILLED", 2)]
    assert all("5012345601" not in json.dumps(e) and "HTSUSER1" not in json.dumps(e) for e in seen)
    assert sum(x["filled_quantity"] for x in store.executions(order["id"])) == 2
    assert [(p["stock_code"], p["quantity"]) for p in store.positions()] == [("005930", 2)]

    # VTTC0081R inquiry afterwards is authoritative and adds no second fill.
    snapshot = orders.normalize_broker_order({
        "odno": "0000012345", "ord_gno_brno": "06010", "pdno": "005930", "sll_buy_dvsn_cd": "02",
        "ord_qty": "2", "ord_unpr": "70000", "tot_ccld_qty": "2", "rmn_qty": "0", "avg_prvs": "70150",
        "cncl_yn": "N", "ord_dt": "20260925", "ord_tmd": "101531"})
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(snapshot))
    refreshed = service.refresh()[0]
    assert (refreshed["status"], refreshed["filled_quantity"], refreshed["average_fill_price"]) == ("FILLED", 2, 70150)
    assert sum(x["filled_quantity"] for x in store.executions(order["id"])) == 2
    assert [(p["stock_code"], p["quantity"]) for p in store.positions()] == [("005930", 2)]
