import pytest

import kis_virtual_orders as orders
from lab_repository import SQLiteLabRepository


@pytest.fixture
def repository(tmp_path):
    return SQLiteLabRepository(tmp_path / "orders.db")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(orders.settings, "KIS_ENV", "virtual")
    monkeypatch.setattr(orders.settings, "PAPER_ORDER_ENABLED", True)
    monkeypatch.setattr(orders.settings, "KIS_VIRTUAL_ORDER_SUBMIT_ENABLED", True)
    monkeypatch.setattr(orders.settings, "MAX_ORDER_QUANTITY", 100)
    monkeypatch.setattr(orders.settings, "MAX_ORDER_AMOUNT", 1_000_000)


class FakeOrderClient:
    def __init__(self, success=True):
        self.success = success
        self.calls = []

    def submit_order(self, stock_code, side, quantity, price, order_type):
        self.calls.append((stock_code, side, quantity, price, order_type))
        return {
            "success": self.success,
            "broker_order_id": "0000123456" if self.success else None,
            "broker_org_no": "91252" if self.success else None,
            "order_time": "101530",
            "message_code": "APBK0013" if self.success else "ERR",
            "message": "주문 전송 완료" if self.success else "주문 거부",
        }


def values(**overrides):
    base = {
        "idempotency_key": "order-key-0001", "stock_code": "005930", "side": "BUY",
        "quantity": 2, "price": 70000, "order_type": "LIMIT",
        "execution_mode": "KIS_VIRTUAL",
    }
    return {**base, **overrides}


def test_paper_and_submit_flags_block_before_transport(repository, monkeypatch):
    client = FakeOrderClient()
    service = orders.KISVirtualOrderService(repository, client=client)
    monkeypatch.setattr(orders.settings, "PAPER_ORDER_ENABLED", False)
    monkeypatch.setattr(orders.settings, "KIS_VIRTUAL_ORDER_SUBMIT_ENABLED", True)
    with pytest.raises(orders.OrderSafetyError, match="PAPER_ORDER_ENABLED"):
        service.submit(values())
    monkeypatch.setattr(orders.settings, "PAPER_ORDER_ENABLED", True)
    monkeypatch.setattr(orders.settings, "KIS_VIRTUAL_ORDER_SUBMIT_ENABLED", False)
    with pytest.raises(orders.OrderSafetyError, match="KIS_VIRTUAL_ORDER_SUBMIT_ENABLED"):
        service.submit(values())
    assert client.calls == []
    assert service.store.list() == []


@pytest.mark.parametrize(
    ("side", "tr_id"), (("BUY", "VTTC0012U"), ("SELL", "VTTC0011U"))
)
def test_buy_and_sell_request_use_only_vts_tr(side, tr_id, enabled):
    client = orders.KISVirtualOrderClient()
    request = client.build_order_request("005930", side, 1, 70000, "LIMIT")

    assert request["tr_id"] == tr_id
    assert request["url"] == orders.KIS_VTS_REST_BASE_URL + orders.ORDER_PATH
    assert request["body"]["ORD_DVSN"] == "00"
    assert request["body"]["ORD_UNPR"] == "70000"
    assert request["body"]["EXCG_ID_DVSN_CD"] == "KRX"


def test_market_and_limit_validation(enabled):
    client = orders.KISVirtualOrderClient()
    assert client.build_order_request("005930", "BUY", 1, 0, "MARKET")["body"]["ORD_DVSN"] == "01"
    with pytest.raises(ValueError, match="시장가"):
        client.build_order_request("005930", "BUY", 1, 1, "MARKET")
    with pytest.raises(ValueError, match="지정가"):
        client.build_order_request("005930", "BUY", 1, 0, "LIMIT")


@pytest.mark.parametrize("quantity,price", [(0, 1), (101, 1), (1, -1)])
def test_invalid_quantity_and_price(quantity, price, enabled):
    with pytest.raises(ValueError):
        orders.validate_order_values("005930", "BUY", quantity, price, "LIMIT")


def test_acknowledged_transition_broker_id_and_duplicate_submit(repository, enabled):
    client = FakeOrderClient()
    service = orders.KISVirtualOrderService(repository, client=client)

    order = service.submit(values())

    assert order["status"] == "ACKNOWLEDGED"
    assert order["broker_order_id"] == "0000123456"
    assert [event["event_type"] for event in service.store.events(order["id"])] == [
        "CREATED", "VALIDATED", "SUBMITTING", "ACKNOWLEDGED"
    ]
    with pytest.raises(orders.DuplicateOrderError):
        service.submit(values())
    assert len(client.calls) == 1


def test_broker_response_is_normalized_without_sensitive_values(enabled, monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"rt_cd": "0", "msg_cd": "OK", "msg1": "완료", "output": {
                "ODNO": "123", "KRX_FWDG_ORD_ORGNO": "91252", "ORD_TMD": "101530"
            }}

    def transport(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(orders, "get_access_token", lambda: "secret-token")
    client = orders.KISVirtualOrderClient(post_transport=transport)
    result = client.submit_order("005930", "BUY", 1, 70000, "LIMIT")

    assert result["broker_order_id"] == "123"
    assert result["broker_org_no"] == "91252"
    assert result["order_time"] == "101530"
    assert "secret-token" not in str(result)
    assert orders.mask_sensitive(captured["json"]["CANO"]).startswith("***")


def test_order_amount_limit_uses_current_price_for_market(repository, enabled):
    service = orders.KISVirtualOrderService(
        repository, client=FakeOrderClient(),
        price_provider=lambda code: {"output": {"stck_prpr": "600000"}},
    )
    with pytest.raises(ValueError, match="최대 주문금액"):
        service.submit(values(quantity=2, price=0, order_type="MARKET"))
