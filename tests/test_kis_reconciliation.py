import pytest

import kis_virtual_orders as orders
from lab_repository import SQLiteLabRepository


@pytest.fixture
def repository(tmp_path):
    return SQLiteLabRepository(tmp_path / "reconciliation.db")


@pytest.fixture(autouse=True)
def virtual_only(monkeypatch):
    monkeypatch.setattr(orders.settings, "KIS_ENV", "virtual")


def snapshot(order_id="12345", filled=0, remaining=10):
    return orders.normalize_broker_order({
        "odno": order_id, "ord_gno_brno": "91252", "pdno": "005930",
        "sll_buy_dvsn_cd": "02", "ord_qty": "10", "ord_unpr": "70000",
        "tot_ccld_qty": str(filled), "rmn_qty": str(remaining),
        "avg_prvs": "70100", "cncl_yn": "N", "ord_dt": "20260904",
        "ord_tmd": "101530",
    })


class ReconciliationClient:
    def __init__(self, broker_orders):
        self.broker_orders = broker_orders

    def inquire_orders(self, inquiry_date=None):
        return self.broker_orders


def local_order(store):
    item = store.create({
        "idempotency_key": "reconcile-key-0001", "stock_code": "005930",
        "side": "BUY", "quantity": 10, "price": 70000, "order_type": "LIMIT",
    })
    return store.transition(
        item["id"], "ACKNOWLEDGED", broker_order_id="12345", broker_org_no="91252"
    )


def test_matched_order_and_position_are_recorded(repository):
    service = orders.KISVirtualOrderService(
        repository, client=ReconciliationClient([snapshot()]),
        balance_provider=lambda: {"output1": []},
    )
    local_order(service.store)

    result = service.reconcile()

    assert result["matched"] == 1
    assert result["corrected"] == 0
    assert service.store.reconciliation_runs(1)[0]["id"] == result["run_id"]


def test_changed_fill_is_corrected_and_updates_position_once(repository):
    service = orders.KISVirtualOrderService(
        repository, client=ReconciliationClient([snapshot(filled=4, remaining=6)]),
        balance_provider=lambda: {"output1": [{"pdno": "005930", "hldg_qty": "4"}]},
    )
    item = local_order(service.store)

    result = service.reconcile()

    assert result["corrected"] == 1
    assert result["matched"] == 1
    assert service.store.get(item["id"])["status"] == "PARTIALLY_FILLED"
    assert service.store.positions()[0]["quantity"] == 4
    assert service.store.executions(item["id"])[0]["filled_quantity"] == 4


def test_unknown_kis_order_requires_manual_review_and_is_not_imported(repository):
    service = orders.KISVirtualOrderService(
        repository, client=ReconciliationClient([snapshot(order_id="UNKNOWN")]),
        balance_provider=lambda: {"output1": []},
    )

    result = service.reconcile()

    assert result["manual_review_required"] == 1
    assert result["items"][0]["details"]["reason"] == "KIS_ORDER_NOT_FOUND_LOCALLY"
    assert service.store.list() == []


def test_local_only_order_is_mismatch(repository):
    service = orders.KISVirtualOrderService(
        repository, client=ReconciliationClient([]), balance_provider=lambda: {"output1": []}
    )
    local_order(service.store)

    result = service.reconcile()

    assert result["mismatch"] == 1
    assert result["items"][0]["details"]["reason"] == "LOCAL_ORDER_NOT_FOUND_AT_KIS"


class SubmitTrackingClient(ReconciliationClient):
    def __init__(self, broker_orders):
        super().__init__(broker_orders)
        self.submitted = []

    def submit_order(self, *args, **kwargs):
        self.submitted.append((args, kwargs))
        raise AssertionError("reconciliation must not submit broker orders")

    def cancel_order(self, *args, **kwargs):
        self.submitted.append((args, kwargs))
        raise AssertionError("reconciliation must not cancel broker orders")


def reconcile_position(repository, local_quantity, kis_quantity, kis_average="259500"):
    client = SubmitTrackingClient([])
    service = orders.KISVirtualOrderService(
        repository, client=client,
        balance_provider=lambda: {"output1": [{
            "pdno": "005930", "hldg_qty": str(kis_quantity), "pchs_avg_pric": kis_average,
        }]},
    )
    if local_quantity:
        service.store.apply_position_fill("005930", "BUY", local_quantity, 259500)
    result = service.reconcile()
    assert client.submitted == []
    [item] = [item for item in result["items"] if item["entity_type"] == "POSITION"]
    return service, result, item


def test_external_holding_without_lab_position_needs_no_review(repository):
    service, result, item = reconcile_position(repository, 0, 3)

    assert result["manual_review_required"] == 0
    assert result["mismatch"] == 0
    assert item["result"] == "MATCHED"
    assert item["details"]["external_surplus_qty"] == 3
    assert service.store.positions() == []


def test_kis_surplus_over_lab_position_is_external_and_ignores_blended_average(repository):
    service, result, item = reconcile_position(repository, 2, 3, kis_average="250000")

    assert result["manual_review_required"] == 0
    assert item["result"] == "MATCHED"
    assert item["details"]["external_surplus_qty"] == 1
    assert [(p["quantity"], p["average_price"]) for p in service.store.positions()] == [(2, 259500)]


def test_equal_position_with_matching_average_is_matched(repository):
    _, result, item = reconcile_position(repository, 2, 2)

    assert result["manual_review_required"] == 0
    assert item["result"] == "MATCHED"
    assert item["details"]["external_surplus_qty"] == 0


def test_equal_position_with_wrong_average_requires_manual_review(repository):
    _, result, item = reconcile_position(repository, 2, 2, kis_average="250000")

    assert result["manual_review_required"] == 1
    assert item["result"] == "MANUAL_REVIEW_REQUIRED"


def test_kis_short_of_lab_position_requires_manual_review(repository):
    _, result, item = reconcile_position(repository, 2, 1)

    assert result["manual_review_required"] == 1
    assert item["result"] == "MANUAL_REVIEW_REQUIRED"
    assert item["details"]["external_surplus_qty"] == 0
