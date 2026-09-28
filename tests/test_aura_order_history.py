"""M:ONE order views use fake LAB storage and never call KIS."""
from fastapi.testclient import TestClient

import app.aura_routes as routes
from app.main import app


class FakeStore:
    def __init__(self):
        self.run = {"created_at": "2026-09-24T10:00:00+00:00", "matched": 1,
                    "corrected": 0, "mismatch": 1, "manual_review_required": 0,
                    "sensitive": "private-token"}

    def list(self):
        return [{"id": "ORDER-1", "created_at": "2026-09-24T10:00:00+00:00",
                 "stock_code": "005930", "side": "BUY", "quantity": 2,
                 "filled_quantity": 1, "remaining_quantity": 1,
                 "requested_price": 70000, "status": "PARTIALLY_FILLED",
                 "idempotency_key": "private-key", "last_error": "private-token"}]

    def reconciliation_runs(self, limit):
        assert limit == 1
        return [self.run]

    def get(self, order_id):
        return self.list()[0] if order_id == "ORDER-1" else None


class FakeService:
    def __init__(self):
        self.store = FakeStore()
        self.refreshes = self.reconciles = 0

    def refresh(self):
        self.refreshes += 1

    def reconcile(self):
        self.reconciles += 1

    def cancel(self, order_id):
        self.cancels = getattr(self, "cancels", 0) + 1
        return {**self.store.get(order_id), "status": "CANCELED", "remaining_quantity": 0}


def test_order_history_and_reconciliation_are_authenticated_and_projected(monkeypatch):
    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")
    service = FakeService()
    app.dependency_overrides[routes.order_service] = lambda: service
    try:
        client = TestClient(app)
        path = "/api/integrations/aura/orders?refresh=true"
        assert client.get(path).status_code == 403
        headers = {"X-Aura-Read-Key": "test-read-key"}
        response = client.get(path, headers=headers)
        assert response.status_code == 200 and service.refreshes == 1
        order = response.json()["orders"][0]
        assert order["remaining_quantity"] == 1 and order["status"] == "PARTIALLY_FILLED"
        assert "private" not in response.text
        result = client.get("/api/integrations/aura/reconciliation", headers=headers).json()
        assert result["reconciliation"]["mismatch"] == 1
        assert "private" not in str(result)
        refreshed = client.post("/api/integrations/aura/reconciliation/refresh", headers=headers)
        assert refreshed.status_code == 200 and service.reconciles == 1
    finally:
        app.dependency_overrides.clear()


def test_order_events_are_authenticated_read_only_and_expose_only_the_broker_reason(monkeypatch, tmp_path):
    import kis_virtual_orders as orders
    from lab_repository import SQLiteLabRepository

    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")

    class NoOrderClient:
        def __getattr__(self, name):
            raise AssertionError(f"order events must not call KIS ({name})")

    service = orders.KISVirtualOrderService(SQLiteLabRepository(tmp_path / "events.db"), client=NoOrderClient(),
                                            balance_provider=lambda: {"output1": []})
    item = service.store.create({"idempotency_key": "events-key-0001", "stock_code": "005930", "side": "BUY",
                                 "quantity": 1, "price": 286500, "order_type": "LIMIT"})
    service.store.transition(item["id"], "VALIDATED")
    service.store.transition(item["id"], "SUBMITTING")
    service.store.transition(item["id"], "REJECTED",
                             {"message_code": "40100000", "message": "모의투자 영업일이 아닙니다.",
                              "raw": "private-payload"}, last_error="모의투자 영업일이 아닙니다.")
    app.dependency_overrides[routes.order_service] = lambda: service
    try:
        client = TestClient(app)
        path = f"/api/integrations/aura/orders/{item['id']}/events"
        assert client.get(path).status_code == 403
        headers = {"X-Aura-Read-Key": "test-read-key"}
        body = client.get(path, headers=headers).json()
        assert body["order_id"] == item["id"] and body["status"] == "REJECTED"
        assert [e["event_type"] for e in body["events"]] == ["CREATED", "VALIDATED", "SUBMITTING", "REJECTED"]
        rejected = body["events"][-1]
        assert (rejected["message_code"], rejected["message"]) == ("40100000", "모의투자 영업일이 아닙니다.")
        assert all(e["at"] for e in body["events"]) and "private" not in str(body)
        assert client.get("/api/integrations/aura/orders/NOPE-1/events", headers=headers).status_code == 404
        assert service.store.get(item["id"])["status"] == "REJECTED"     # read-only
    finally:
        app.dependency_overrides.clear()


def test_cancel_requires_explicit_authenticated_action_and_remaining_quantity(monkeypatch):
    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")
    service = FakeService()
    app.dependency_overrides[routes.order_service] = lambda: service
    try:
        client = TestClient(app)
        path = "/api/integrations/aura/orders/ORDER-1/cancel"
        assert client.post(path).status_code == 403
        assert getattr(service, "cancels", 0) == 0
        headers = {"X-Aura-Read-Key": "test-read-key"}
        assert client.post(path, headers=headers).json()["order"]["status"] == "CANCELED"
        assert service.cancels == 1
        service.store.get = lambda order_id: {**service.store.list()[0], "remaining_quantity": 0}
        assert client.post(path, headers=headers).status_code == 409
        assert service.cancels == 1
    finally:
        app.dependency_overrides.clear()


def test_order_history_reports_whether_the_lab_would_submit(monkeypatch):
    # M:ONE shows "order submission disabled" instead of "ready" when the LAB's own gate is off.
    import kis_virtual_orders as orders
    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")
    monkeypatch.setattr(orders.settings, "KIS_ENV", "virtual")
    app.dependency_overrides[routes.order_service] = lambda: FakeService()
    headers = {"X-Aura-Read-Key": "test-read-key"}
    try:
        client = TestClient(app)
        for paper, vts, expected in [(False, False, False), (True, False, False), (False, True, False), (True, True, True)]:
            monkeypatch.setattr(orders.settings, "PAPER_ORDER_ENABLED", paper)
            monkeypatch.setattr(orders.settings, "KIS_VIRTUAL_ORDER_SUBMIT_ENABLED", vts)
            body = client.get("/api/integrations/aura/orders", headers=headers).json()
            assert body["submit_enabled"] is expected and body["orders"][0]["id"] == "ORDER-1"
    finally:
        app.dependency_overrides.clear()
