"""KIS VTS order history: continuation (all pages) and canceled-order normalization.

Shapes follow the actual 2026-09-28 KIS VTS responses: page 1 = 15 rows (tr_cont "F"), page 2 = 11 rows
(tr_cont "D"); the canceled 005930 SELL came back as tot_ccld_qty 0 / rmn_qty 0 / cncl_cfrm_qty 1 with a
separate cancel-request row (cncl_yn "Y", orgn_odno = the canceled order).
"""
import pytest

import kis_virtual_orders as orders
from lab_repository import SQLiteLabRepository


@pytest.fixture(autouse=True)
def virtual_account(monkeypatch):
    monkeypatch.setattr(orders.settings, "KIS_ENV", "virtual")
    monkeypatch.setattr(orders.settings, "KIS_ACCOUNT_NO", "12345678-01")
    monkeypatch.setattr(orders.settings, "KIS_APP_KEY", "app-key")
    monkeypatch.setattr(orders.settings, "KIS_APP_SECRET", "app-secret")
    monkeypatch.setattr(orders, "get_access_token", lambda: "token")


def row(odno, *, qty="1", filled="1", remaining="0", cancel_yn="N", cancel_confirmed="0", original="0000000000",
        side="02", avg="70000"):
    return {"odno": odno, "orgn_odno": original, "ord_gno_brno": "00950", "pdno": "005930",
            "sll_buy_dvsn_cd": side, "ord_qty": qty, "ord_unpr": "70000", "tot_ccld_qty": filled,
            "rmn_qty": remaining, "cncl_yn": cancel_yn, "cncl_cfrm_qty": cancel_confirmed, "rjct_qty": "0",
            "avg_prvs": avg if filled not in ("0", "") else "0", "ord_dt": "20260928", "ord_tmd": "090700"}


class FakeResponse:
    def __init__(self, rows, tr_cont, fk="", nk=""):
        self.headers = {"tr_cont": tr_cont}
        self._payload = {"rt_cd": "0", "msg_cd": "KIOK0510", "output1": rows,
                         "ctx_area_fk100": fk, "ctx_area_nk100": nk}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class PagedKIS:
    def __init__(self, pages):
        self.pages, self.calls, self.sleeps = list(pages), [], []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"headers": dict(headers), "params": dict(params)})
        return self.pages[len(self.calls) - 1]

    def client(self):
        return orders.KISVirtualOrderClient(get_transport=self.get, sleep=self.sleeps.append)


def ids(prefix, count, start=0):
    return [f"{prefix}{n:04d}" for n in range(start, start + count)]


# --- A. single page ----------------------------------------------------------------------------
def test_single_page_is_one_request_without_continuation():
    kis = PagedKIS([FakeResponse([row("0000004283")], "D")])

    result = kis.client().inquire_orders("20260928")

    assert [o["broker_order_id"] for o in result] == ["0000004283"]
    assert len(kis.calls) == 1 and "tr_cont" not in kis.calls[0]["headers"]
    assert kis.calls[0]["params"]["CTX_AREA_FK100"] == "" and kis.calls[0]["params"]["CTX_AREA_NK100"] == ""
    assert kis.sleeps == []


# --- B. multiple pages (today's 15 + 11) -------------------------------------------------------------
def test_all_pages_are_read_in_order_like_todays_15_plus_11():
    first, second = ids("00001", 15), ids("00002", 11)
    kis = PagedKIS([
        FakeResponse([row(i) for i in first], "F", fk="FK1   ", nk="NK1   "),
        FakeResponse([row(i) for i in second], "D"),
    ])

    result = kis.client().inquire_orders("20260928")

    assert [o["broker_order_id"] for o in result] == first + second
    assert len(result) == 26 and len(kis.calls) == 2


def test_middle_page_marker_m_also_continues():
    kis = PagedKIS([
        FakeResponse([row("A0001")], "F", fk="F1", nk="N1"),
        FakeResponse([row("A0002")], "M", fk="F2", nk="N2"),
        FakeResponse([row("A0003")], "E"),
    ])

    assert [o["broker_order_id"] for o in kis.client().inquire_orders()] == ["A0001", "A0002", "A0003"]
    assert len(kis.calls) == 3


# --- C. continuation keys propagated, rate spacing kept -------------------------------------------------
def test_next_request_sends_tr_cont_n_and_previous_context_keys():
    kis = PagedKIS([
        FakeResponse([row("B0001")], "F", fk="12345678^01^20260928   ", nk="0000031936^   "),
        FakeResponse([row("B0002")], "D"),
    ])

    kis.client().inquire_orders("20260928")

    follow = kis.calls[1]
    assert follow["headers"]["tr_cont"] == "N"
    assert follow["params"]["CTX_AREA_FK100"] == "12345678^01^20260928"
    assert follow["params"]["CTX_AREA_NK100"] == "0000031936^"
    assert follow["headers"]["tr_id"] == orders.ORDER_INQUIRY_TR_ID
    assert kis.sleeps == [orders.ORDER_INQUIRY_PAGE_INTERVAL_SECONDS]      # spacing between pages only


# --- D. duplicates across pages --------------------------------------------------------------------
def test_a_row_repeated_on_the_next_page_is_one_order():
    kis = PagedKIS([
        FakeResponse([row("C0001"), row("C0002")], "F", fk="F1", nk="N1"),
        FakeResponse([row("C0002"), row("C0003")], "D"),
    ])

    assert [o["broker_order_id"] for o in kis.client().inquire_orders()] == ["C0001", "C0002", "C0003"]


# --- E. malformed continuation terminates safely -------------------------------------------------------
def test_more_rows_without_context_keys_fails_instead_of_returning_a_partial_list():
    kis = PagedKIS([FakeResponse([row("D0001")], "F", fk="", nk="")])

    with pytest.raises(RuntimeError):
        kis.client().inquire_orders()
    assert len(kis.calls) == 1


def test_repeated_context_keys_do_not_loop():
    kis = PagedKIS([FakeResponse([row("D0001")], "F", fk="F1", nk="N1"),
                    FakeResponse([row("D0002")], "F", fk="F1", nk="N1")])

    with pytest.raises(RuntimeError):
        kis.client().inquire_orders()
    assert len(kis.calls) == 2


def test_page_limit_stops_an_endless_continuation(monkeypatch):
    monkeypatch.setattr(orders, "ORDER_INQUIRY_MAX_PAGES", 3)
    kis = PagedKIS([FakeResponse([row(f"E000{n}")], "F", fk=f"F{n}", nk=f"N{n}") for n in range(5)])

    with pytest.raises(RuntimeError):
        kis.client().inquire_orders()
    assert len(kis.calls) == 3


def test_kis_error_on_a_later_page_fails_the_inquiry():
    bad = FakeResponse([], "D")
    bad._payload["rt_cd"] = "1"
    kis = PagedKIS([FakeResponse([row("G0001")], "F", fk="F1", nk="N1"), bad])

    with pytest.raises(RuntimeError):
        kis.client().inquire_orders()


# --- canceled-order normalization (today's exact shape) -----------------------------------------------
def test_todays_canceled_sell_is_canceled_never_filled():
    order = orders.normalize_broker_order(
        row("0000026089", side="01", qty="1", filled="0", remaining="0", cancel_confirmed="1"))

    assert order["status"] == "CANCELED"
    assert order["filled_quantity"] == 0 and order["remaining_quantity"] == 0
    assert order["cancel_confirmed_quantity"] == 1 and order["original_order_id"] is None


def test_kis_cancel_request_row_points_at_the_canceled_order():
    request = orders.normalize_broker_order(
        row("0000031936", side="01", qty="1", filled="0", remaining="0", cancel_yn="Y", original="0000026089"))

    assert request["status"] == "CANCELED" and request["canceled"] is True
    assert request["original_order_id"] == "0000026089"


@pytest.mark.parametrize("fields, status, filled", [
    (dict(qty="1", filled="1", remaining="0"), "FILLED", 1),
    (dict(qty="1", filled="0", remaining="1"), "ACKNOWLEDGED", 0),
    (dict(qty="10", filled="4", remaining="6"), "PARTIALLY_FILLED", 4),
    (dict(qty="10", filled="4", remaining="0", cancel_confirmed="6"), "CANCELED", 4),   # LAB cancel() keeps fills
])
def test_existing_statuses_are_kept(fields, status, filled):
    order = orders.normalize_broker_order(row("F0001", **fields))

    assert order["status"] == status and order["filled_quantity"] == filled


def test_filled_is_derived_only_when_kis_omits_it():
    item = row("F0002", qty="10", remaining="3", cancel_confirmed="2")
    item.pop("tot_ccld_qty")

    assert orders.normalize_broker_order(item)["filled_quantity"] == 5


# --- F. reconciliation sees every page and today's cancel correctly ---------------------------------------
@pytest.fixture
def service(tmp_path):
    def build(kis, holdings=()):
        return orders.KISVirtualOrderService(
            SQLiteLabRepository(tmp_path / "inquiry.db"), client=kis.client(),
            balance_provider=lambda: {"output1": list(holdings)})
    return build


def local(store, key, broker_id, status, *, side="BUY", filled=1, remaining=0, avg=70000.0):
    item = store.create({"idempotency_key": key, "stock_code": "005930", "side": side, "quantity": 1,
                         "price": 70000, "order_type": "LIMIT"})
    store.transition(item["id"], "ACKNOWLEDGED", broker_order_id=broker_id, broker_org_no="00950")
    return store.transition(item["id"], status, filled_quantity=filled, remaining_quantity=remaining,
                            average_fill_price=avg if filled else None)


def test_reconciliation_matches_orders_on_page_two(service):
    first, second = ids("00003", 15), ids("00004", 11)
    kis = PagedKIS([FakeResponse([row(i) for i in first], "F", fk="F1", nk="N1"),
                    FakeResponse([row(i) for i in second], "D")])
    svc = service(kis)
    for n, broker_id in enumerate(first + second):
        local(svc.store, f"page-key-{n:04d}", broker_id, "FILLED")

    result = svc.reconcile("20260928")

    assert result["mismatch"] == 0 and result["manual_review_required"] == 0 and result["corrected"] == 0
    order_items = [i for i in result["items"] if i["entity_type"] == "ORDER"]
    assert len(order_items) == 26 and all(i["result"] == "MATCHED" for i in order_items)


def test_reconciliation_keeps_todays_canceled_order_and_links_the_cancel_row(service):
    kis = PagedKIS([FakeResponse([
        row("0000031936", side="01", filled="0", remaining="0", cancel_yn="Y", original="0000026089"),
        row("0000026089", side="01", filled="0", remaining="0", cancel_confirmed="1"),
    ], "D")])
    svc = service(kis, holdings=[{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "285000"}])
    canceled = local(svc.store, "cancel-key-0001", "0000026089", "CANCELED", side="SELL", filled=0, remaining=0)

    result = svc.reconcile("20260928")

    assert result["mismatch"] == 0 and result["manual_review_required"] == 0 and result["corrected"] == 0
    link = next(i for i in result["items"] if i["reference"] == "0000031936")
    assert link["entity_type"] == "CANCEL_REQUEST" and link["details"]["original_order_id"] == "0000026089"
    stored = svc.store.get(canceled["id"])
    assert stored["status"] == "CANCELED" and stored["filled_quantity"] == 0      # never rewritten to FILLED


def test_cancel_row_for_an_order_lab_does_not_know_still_needs_review(service):
    kis = PagedKIS([FakeResponse([
        row("0000099999", side="01", filled="0", remaining="0", cancel_yn="Y", original="0000088888"),
    ], "D")])

    result = service(kis).reconcile("20260928")

    assert result["manual_review_required"] == 1
