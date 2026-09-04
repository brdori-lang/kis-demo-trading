import pytest

import kis_virtual_orders as orders
from lab_repository import SQLiteLabRepository


@pytest.fixture
def repository(tmp_path):
    return SQLiteLabRepository(tmp_path / "lifecycle.db")


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(orders.settings, "KIS_ENV", "virtual")
    monkeypatch.setattr(orders.settings, "PAPER_ORDER_ENABLED", True)
    monkeypatch.setattr(orders.settings, "KIS_VIRTUAL_ORDER_SUBMIT_ENABLED", True)


def acknowledged(store, quantity=10):
    item = store.create({
        "idempotency_key": "lifecycle-key", "stock_code": "005930", "side": "BUY",
        "quantity": quantity, "price": 70000, "order_type": "LIMIT",
    })
    return store.transition(
        item["id"], "ACKNOWLEDGED", broker_order_id="12345", broker_org_no="91252"
    )


def broker_snapshot(filled, remaining, canceled="N"):
    return orders.normalize_broker_order({
        "odno": "12345", "ord_gno_brno": "91252", "pdno": "005930",
        "sll_buy_dvsn_cd": "02", "ord_qty": "10", "ord_unpr": "70000",
        "tot_ccld_qty": str(filled), "rmn_qty": str(remaining), "avg_prvs": "70100",
        "cncl_yn": canceled, "ord_dt": "20260904", "ord_tmd": "101530",
    })


class LifecycleClient:
    def __init__(self, snapshots=None, cancel_success=True):
        self.snapshots = snapshots or []
        self.cancel_success = cancel_success
        self.cancel_calls = []

    def inquire_orders(self, inquiry_date=None):
        return self.snapshots.pop(0)

    def cancel_order(self, order):
        self.cancel_calls.append(order["id"])
        return {
            "success": self.cancel_success, "broker_order_id": "54321",
            "order_time": "102000", "message_code": "OK" if self.cancel_success else "REJECT",
            "message": "취소 완료" if self.cancel_success else "취소 거부",
        }


def test_partial_fill_then_filled_and_duplicate_execution(repository):
    client = LifecycleClient(snapshots=[[broker_snapshot(4, 6)], [broker_snapshot(10, 0)]])
    service = orders.KISVirtualOrderService(repository, client=client)
    order = acknowledged(service.store)

    partial = service.refresh()[0]
    assert partial["status"] == "PARTIALLY_FILLED"
    assert partial["filled_quantity"] == 4
    assert partial["remaining_quantity"] == 6
    assert service.store.executions(order["id"])[0]["filled_quantity"] == 4

    filled = service.refresh()[0]
    assert filled["status"] == "FILLED"
    assert filled["filled_quantity"] == 10
    assert filled["remaining_quantity"] == 0
    executions = service.store.executions(order["id"])
    assert [item["filled_quantity"] for item in executions] == [4, 6]
    assert service.store.add_execution(
        order["id"], executions[-1]["broker_execution_id"], "005930", 6, 70100, ""
    ) is False


def test_cancel_requested_then_canceled(repository):
    client = LifecycleClient(cancel_success=True)
    service = orders.KISVirtualOrderService(repository, client=client)
    order = acknowledged(service.store)

    canceled = service.cancel(order["id"])

    assert canceled["status"] == "CANCELED"
    events = [item["event_type"] for item in service.store.events(order["id"])]
    assert events[-2:] == ["CANCEL_REQUESTED", "CANCELED"]
    assert client.cancel_calls == [order["id"]]


def test_cancel_reject_restores_previous_state_and_records_event(repository):
    service = orders.KISVirtualOrderService(
        repository, client=LifecycleClient(cancel_success=False)
    )
    order = acknowledged(service.store)

    result = service.cancel(order["id"])

    assert result["status"] == "ACKNOWLEDGED"
    assert result["last_error"] == "취소 거부"
    assert service.store.events(order["id"])[-1]["event_type"] == "CANCEL_REJECTED"


def test_filled_or_canceled_order_cannot_be_canceled(repository):
    service = orders.KISVirtualOrderService(repository, client=LifecycleClient())
    order = acknowledged(service.store)
    service.store.transition(order["id"], "FILLED")
    with pytest.raises(ValueError, match="취소"):
        service.cancel(order["id"])


def test_cancel_request_uses_official_vts_contract(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"rt_cd": "0", "msg_cd": "OK", "msg1": "완료", "output": {"ODNO": "9"}}

    def transport(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(orders, "get_access_token", lambda: "token")
    client = orders.KISVirtualOrderClient(post_transport=transport)
    result = client.cancel_order({
        "execution_mode": "KIS_VIRTUAL", "broker_order_id": "12345",
        "broker_org_no": "91252", "order_type": "LIMIT",
    })

    assert result["success"] is True
    assert captured["headers"]["tr_id"] == "VTTC0013U"
    assert captured["json"]["RVSE_CNCL_DVSN_CD"] == "02"
    assert captured["json"]["QTY_ALL_ORD_YN"] == "Y"
    assert captured["url"].endswith("/order-rvsecncl")
