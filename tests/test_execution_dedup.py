"""One broker fill, one execution: H0STCNI9 notices and VTTC0081R inquiry must not both apply it.

Incident 2026-09-29 13:12, 034020 BUY 1: KIS held 1 and M:ONE owned 1, but the LAB position was 2.
The fill notice and an inquiry that had read the order just before the notice landed each recorded
the fill under their own execution id and each moved the position.
"""
import threading

import pytest

import kis_virtual_orders as orders
import realtime_quotes as quotes
from lab_repository import SQLiteLabRepository
from order_notifications import OrderNotificationProcessor
from test_order_notifications import notice_values


BROKER_ORDER_ID = "0000031712"


@pytest.fixture(autouse=True)
def virtual_only(monkeypatch):
    monkeypatch.setattr(orders.settings, "KIS_ENV", "virtual")


@pytest.fixture
def repository(tmp_path):
    return SQLiteLabRepository(tmp_path / "dedup.db")


def acknowledged(store, quantity=1, stock_code="034020", key="dedup-key"):
    item = store.create({"idempotency_key": key, "stock_code": stock_code, "side": "BUY",
                         "quantity": quantity, "price": 60000, "order_type": "LIMIT"})
    return store.transition(item["id"], "ACKNOWLEDGED", broker_order_id=BROKER_ORDER_ID, broker_org_no="06010")


def fill_notice(qty, price="60000", time="131205", order_qty="1"):
    values = notice_values(order_no=BROKER_ORDER_ID, fill_flag="2", fill_qty=str(qty), fill_price=price,
                           order_qty=order_qty, time=time)
    values[8] = "034020"
    return {"type": "order_notice", **dict(zip(quotes.REALTIME_ORDER_NOTICE_COLUMNS, values))}


def inquiry_row(filled, quantity=1, average="60000", canceled="N", cancel_confirmed="0"):
    return orders.normalize_broker_order({
        "odno": BROKER_ORDER_ID, "ord_gno_brno": "06010", "pdno": "034020", "sll_buy_dvsn_cd": "02",
        "ord_qty": str(quantity), "ord_unpr": "60000", "tot_ccld_qty": str(filled),
        "rmn_qty": str(quantity - filled - int(cancel_confirmed)), "avg_prvs": average,
        "cncl_yn": canceled, "cncl_cfrm_qty": cancel_confirmed, "ord_dt": "20260929", "ord_tmd": "131204",
    })


class InquiryClient:
    def __init__(self, *rows):
        self.rows = list(rows)

    def inquire_orders(self, inquiry_date=None):
        return list(self.rows)


def lab_position(store, stock_code="034020"):
    return next((p["quantity"] for p in store.positions() if p["stock_code"] == stock_code), 0)


def test_034020_notice_landing_while_an_inquiry_holds_the_order_is_applied_once(repository):
    """The incident: the inquiry read the order (filled 0), then the notice applied the fill."""
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(1)))
    order = acknowledged(service.store)
    notices = OrderNotificationProcessor(service.store)
    read_orders, landed = service.store.list, []

    def list_then_notice():
        snapshot = read_orders()              # the inquiry's view of the order
        if not landed:                        # the realtime notice lands in between, once
            landed.append(True)
            notices.apply(fill_notice(1))
        return snapshot

    service.store.list = list_then_notice
    service.refresh()

    assert lab_position(service.store) == 1
    assert sum(x["filled_quantity"] for x in service.store.executions(order["id"])) == 1
    assert service.store.get(order["id"])["filled_quantity"] == 1


def test_034020_lagging_inquiry_neither_takes_the_fill_back_nor_applies_it_again(repository):
    """The incident as recorded: FILLED 04:06:50 (notice) -> ACKNOWLEDGED 04:06:50.7 (inquiry still
    at 0) -> FILLED 04:07:24 (inquiry at 1), with the position moved twice."""
    lagging, caught_up = InquiryClient(inquiry_row(0)), InquiryClient(inquiry_row(1, average="79200"))
    service = orders.KISVirtualOrderService(repository, client=lagging)
    order = acknowledged(service.store)
    OrderNotificationProcessor(service.store).apply(fill_notice(1, price="79200"))

    held = service.refresh()[0]
    assert (held["status"], held["filled_quantity"]) == ("FILLED", 1)    # never back to ACKNOWLEDGED
    service.client = caught_up
    done = service.refresh()[0]

    assert (done["status"], done["filled_quantity"]) == ("FILLED", 1)
    assert lab_position(service.store) == 1
    assert len(service.store.executions(order["id"])) == 1
    assert [e["event_type"] for e in service.store.events(order["id"])].count("ACKNOWLEDGED") == 1


def test_a_lagging_inquiry_between_partial_notices_does_not_inflate_the_next_fill(repository):
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(0, quantity=5)))
    order = acknowledged(service.store, quantity=5)
    notices = OrderNotificationProcessor(service.store)
    notices.apply(fill_notice(2, time="131205", order_qty="5"))

    service.refresh()   # KIS inquiry has not seen the first fill yet
    notices.apply(fill_notice(3, time="131207", order_qty="5"))

    assert lab_position(service.store) == 5
    assert [x["filled_quantity"] for x in service.store.executions(order["id"])] == [2, 3]


def test_reconciliation_reports_a_lagging_inquiry_instead_of_correcting_the_fill_away(repository):
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(0)),
                                            balance_provider=lambda: {"output1": [{"pdno": "034020", "hldg_qty": "1"}]})
    order = acknowledged(service.store)
    OrderNotificationProcessor(service.store).apply(fill_notice(1))

    result = service.reconcile("20260929")

    [item] = [i for i in result["items"] if i["entity_type"] == "ORDER"]
    assert (item["result"], item["details"]["reason"]) == ("MISMATCH", "KIS_INQUIRY_BEHIND_LAB_FILL")
    assert result["corrected"] == 0
    assert service.store.get(order["id"])["filled_quantity"] == 1
    assert lab_position(service.store) == 1


def test_notice_then_inquiry_adds_the_fill_once(repository):
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(1)))
    order = acknowledged(service.store)
    OrderNotificationProcessor(service.store).apply(fill_notice(1))
    assert lab_position(service.store) == 1

    refreshed = service.refresh()[0]

    assert (refreshed["status"], refreshed["filled_quantity"]) == ("FILLED", 1)
    assert lab_position(service.store) == 1
    assert len(service.store.executions(order["id"])) == 1


def test_inquiry_then_notice_adds_the_fill_once(repository):
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(1)))
    order = acknowledged(service.store)
    service.refresh()
    assert lab_position(service.store) == 1

    result = OrderNotificationProcessor(service.store).apply(fill_notice(1))

    assert (result["status"], result["filled_quantity"]) == ("FILLED", 1)
    assert lab_position(service.store) == 1
    assert len(service.store.executions(order["id"])) == 1


def test_both_paths_name_the_same_fill_with_the_same_execution_id(repository):
    first = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(1)))
    polled = acknowledged(first.store, key="polled")
    first.refresh()
    polled_id = first.store.executions(polled["id"])[0]["broker_execution_id"]

    other = SQLiteLabRepository(repository.db_path.replace("dedup.db", "dedup-ws.db"))
    store = orders.KISOrderStore(other)
    noticed = acknowledged(store, key="noticed")
    OrderNotificationProcessor(store).apply(fill_notice(1))
    notice_id = store.executions(noticed["id"])[0]["broker_execution_id"]

    assert polled_id == notice_id == "31712:1"   # broker order number + cumulative filled quantity


def test_racing_notices_and_inquiries_apply_the_fill_exactly_once(repository):
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(1)))
    order = acknowledged(service.store)
    workers, errors = 8, []
    start = threading.Barrier(workers)

    def run(index):
        try:
            start.wait()
            if index % 2:
                OrderNotificationProcessor(service.store).apply(fill_notice(1))
            else:
                service.refresh()
        except Exception as error:  # surfaced below; a thread cannot fail the test itself
            errors.append(error)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert lab_position(service.store) == 1
    assert [x["filled_quantity"] for x in service.store.executions(order["id"])] == [1]
    assert service.store.get(order["id"])["filled_quantity"] == 1


def test_two_partial_fills_are_both_applied_and_a_repeated_one_is_not(repository):
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(5, quantity=5, average="60040")))
    order = acknowledged(service.store, quantity=5)
    notices = OrderNotificationProcessor(service.store)

    assert notices.apply(fill_notice(2, time="131205", order_qty="5"))["filled_quantity"] == 2
    assert lab_position(service.store) == 2
    assert notices.apply(fill_notice(2, time="131205", order_qty="5"))["filled_quantity"] == 2   # same notice again
    assert lab_position(service.store) == 2
    done = notices.apply(fill_notice(3, price="60100", time="131207", order_qty="5"))
    assert (done["status"], done["filled_quantity"]) == ("FILLED", 5)
    assert lab_position(service.store) == 5

    service.refresh()   # the inquiry reports the same two fills as cumulative 5

    assert lab_position(service.store) == 5
    assert [x["filled_quantity"] for x in service.store.executions(order["id"])] == [2, 3]


def test_a_repeated_partial_fill_from_the_inquiry_is_not_applied_twice(repository):
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(inquiry_row(2, quantity=5)))
    order = acknowledged(service.store, quantity=5)
    notices = OrderNotificationProcessor(service.store)
    notices.apply(fill_notice(2, order_qty="5"))

    service.refresh()
    service.refresh()

    assert lab_position(service.store) == 2
    assert [x["filled_quantity"] for x in service.store.executions(order["id"])] == [2]
    assert service.store.get(order["id"])["status"] == "PARTIALLY_FILLED"


def test_partially_filled_then_canceled_keeps_its_fill_once_and_ends_canceled(repository):
    service = orders.KISVirtualOrderService(repository, client=InquiryClient(
        inquiry_row(2, quantity=5, cancel_confirmed="3")))
    order = acknowledged(service.store, quantity=5)
    OrderNotificationProcessor(service.store).apply(fill_notice(2, order_qty="5"))

    refreshed = service.refresh()[0]

    assert (refreshed["status"], refreshed["filled_quantity"], refreshed["remaining_quantity"]) == ("CANCELED", 2, 0)
    assert lab_position(service.store) == 2
    assert len(service.store.executions(order["id"])) == 1
