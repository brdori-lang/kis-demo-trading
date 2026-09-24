from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.aura_routes import integration_service
from aura_integration import AuraIntegrationService, order_key
from config import settings
from kis_virtual_orders import KISVirtualOrderClient, KISVirtualOrderService, normalize_broker_order
from lab_repository import SQLiteLabRepository


BASE = "/api/integrations/aura"
NOW = datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)


def plan(**overrides):
    # Full shape produced by AURA's ExecutionPlan.model_dump(mode="json").
    data = {
        "execution_contract_version": "1.0", "plan_id": "PLAN-demo-001",
        "idempotency_key": "aura-plan-demo-001", "created_at": NOW.isoformat(),
        "symbol": "005930", "side": "BUY", "signal": "BUY",
        "strategy_id": "trend-following-v1", "strategy_version": 1,
        "decision_id": "DEC-demo", "snapshot_id": "SNAP-demo", "data_hash": "a" * 64,
        "regime": "TREND", "confidence": "HIGH", "target_quantity": 2,
        "target_weight": .0014, "reference_price": 70000.0,
        "max_order_amount": 200000.0, "max_position_weight": .2,
        "stop_loss": 65100.0, "take_profit": 80500.0,
        "valid_until": (NOW + timedelta(minutes=30)).isoformat(), "dispatched_at": None,
        "risk_checks": [{"name": "ORDER_AMOUNT", "passed": True, "actual": 140000.0,
                         "limit": 200000.0, "reason": "within limit"}],
        "rationale": ["Test decision"], "execution_mode": "KIS_VIRTUAL", "status": "READY",
        "provenance": {
            "dataset_id": "DATA-demo", "snapshot_id": "SNAP-demo", "data_hash": "a" * 64,
            "strategy_id": "trend-following-v1", "strategy_version": 1,
            "backtest_run_id": None, "ablation_result_reference": None,
            "parameter_sweep_reference": None, "oos_result_reference": None,
            "walk_forward_reference": None, "regime": "TREND", "decision_id": "DEC-demo",
            "git_commit_hash": "b" * 40,
        },
    }
    return {**data, **overrides}


class FakeBroker(KISVirtualOrderClient):
    def __init__(self):
        # Preview/build_order_request remain real and enforce the VTS guards.
        super().__init__()
        self.calls = []
        self.inquiries = []
        self.snapshots = []
        self.fail_submit = False
        self.fail_refresh = False
        self.reject = False
        self.missing_order_number = False

    def submit_order(self, *args):
        self.calls.append(args)
        if self.fail_submit:
            raise httpx.ReadTimeout("private-token private-secret 12345678-01")
        return {
            "success": not self.reject,
            "broker_order_id": None if self.missing_order_number or self.reject else "12345",
            "broker_org_no": "91252", "order_time": "100000",
            "message_code": "ERR" if self.reject else "OK",
            "message": "private-token private-secret 12345678-01",
        }

    def inquire_orders(self, inquiry_date=None):
        self.inquiries.append(inquiry_date)
        if self.fail_refresh:
            raise RuntimeError("private-token private-secret 12345678-01")
        return self.snapshots


@pytest.fixture
def context(tmp_path, monkeypatch):
    for name, value in {
        "KIS_ENV": "virtual", "KIS_ACCOUNT_NO": "12345678-01",
        "KIS_APP_KEY": "private-key", "KIS_APP_SECRET": "private-secret",
        "PAPER_ORDER_ENABLED": True, "KIS_VIRTUAL_ORDER_SUBMIT_ENABLED": True,
        "MAX_ORDER_AMOUNT": 1_000_000, "MAX_ORDER_QUANTITY": 100,
    }.items():
        monkeypatch.setattr(settings, name, value)
    def no_network(*args, **kwargs):
        pytest.fail("Real HTTP transport must never be used by integration tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    repository = SQLiteLabRepository(tmp_path / "aura.db")
    broker = FakeBroker()
    orders = KISVirtualOrderService(repository, client=broker)
    orders.submit = Mock(wraps=orders.submit)
    clock = [NOW]
    service = AuraIntegrationService(repository, orders, lambda: clock[0])
    main.app.dependency_overrides[integration_service] = lambda: service
    client = TestClient(main.app)
    yield client, service, broker, clock, repository
    main.app.dependency_overrides.pop(integration_service, None)


def preview(context, body=None):
    response = context[0].post(BASE + "/previews", json=body or plan())
    assert response.status_code == 200, response.text
    return response.json()


def submit(context, receipt, **overrides):
    return context[0].post(f"{BASE}/executions/{receipt['execution_id']}/submit", json={
        "confirmed": True, "preview_hash": receipt["preview_hash"], **overrides,
    })


def snapshot(filled=0, remaining=2):
    return normalize_broker_order({
        "odno": "12345", "ord_gno_brno": "91252", "pdno": "005930",
        "sll_buy_dvsn_cd": "02", "ord_qty": "2", "rmn_qty": str(remaining),
        "tot_ccld_qty": str(filled), "avg_prvs": "70000", "ord_unpr": "70000",
        "cncl_yn": "N", "ord_dt": "20260930", "ord_tmd": "100000",
    })


def test_full_plan_preview_is_virtual_limit_and_never_submits(context):
    result = preview(context)
    assert result["status"] == "PREVIEWED"
    assert result["execution_id"] == result["plan_id"] == plan()["plan_id"]
    assert result["delivery_id"] == result["plan_id"]
    assert result["preview"]["execution_mode"] == "KIS_VIRTUAL"
    assert result["preview"]["order_type"] == "LIMIT"
    assert result["preview"]["price"] == 70000
    assert result["preview"]["quantity"] == 2
    assert result["preview"]["account"] == "***5678"
    assert result["preview_hash"].startswith("sha256:")
    assert context[2].calls == []
    assert context[1].orders.submit.call_count == 0
    assert context[1].orders.store.list() == []


@pytest.mark.parametrize("overrides", [
    {"execution_mode": "PAPER"}, {"execution_mode": "LIVE"}, {"status": "DISPATCHED"},
    {"status": "REJECTED"}, {"side": None}, {"target_quantity": 0}, {"target_quantity": True},
    {"target_quantity": 1.5}, {"symbol": "AAPL"}, {"symbol": "005930.KS"},
    {"symbol": "００５９３０"}, {"reference_price": 70000.5}, {"reference_price": 0},
    {"reference_price": "70000"}, {"reference_price": True}, {"reference_price": "NaN"},
    {"execution_contract_version": "2.0"}, {"risk_checks": []},
    {"risk_checks": [{"name": "FAIL", "passed": False, "reason": "risk failed"}]},
    {"max_order_amount": 100000}, {"created_at": "2026-09-30T01:00:00"},
    {"valid_until": "2026-09-30T01:30:00"}, {"account_no": "private-account"},
    {"access_token": "private-token"}, {"order_type": "MARKET"},
])
def test_invalid_contract_never_submits(context, overrides):
    response = context[0].post(BASE + "/previews", json=plan(**overrides))
    assert response.status_code == 422
    assert context[2].calls == []
    assert "private-account" not in response.text and "private-token" not in response.text


def test_expired_plan_cannot_create_preview(context):
    context[3][0] = NOW + timedelta(hours=1)
    assert context[0].post(BASE + "/previews", json=plan()).status_code == 409
    assert context[2].calls == []


@pytest.mark.parametrize("setting,value", [("MAX_ORDER_AMOUNT", 100000), ("MAX_ORDER_QUANTITY", 1)])
def test_lab_risk_limit_independent_of_aura_limit(context, monkeypatch, setting, value):
    monkeypatch.setattr(settings, setting, value)
    assert context[0].post(BASE + "/previews", json=plan()).status_code == 400
    assert context[2].calls == []


@pytest.mark.parametrize("confirmation,code", [
    ({"confirmed": False}, 400), ({"confirmed": "true"}, 422),
    ({"preview_hash": "sha256:" + "0" * 64}, 409),
    ({"quantity": 10}, 422), ({"account_no": "private-account"}, 422),
])
def test_submit_requires_exact_confirmation(context, confirmation, code):
    receipt = preview(context)
    assert submit(context, receipt, **confirmation).status_code == code
    assert context[2].calls == []


def test_preview_expiry_blocks_submit(context):
    receipt = preview(context)
    context[3][0] += timedelta(minutes=30)
    assert submit(context, receipt).status_code == 409
    assert context[1].get(receipt["plan_id"])["status"] == "EXPIRED"
    assert context[2].calls == []


def test_same_preview_is_persistent_and_same_id_different_content_conflicts(context):
    first = preview(context)
    assert preview(context)["preview_hash"] == first["preview_hash"]
    assert context[0].post(BASE + "/previews", json=plan(target_quantity=1)).status_code == 409
    assert context[0].post(BASE + "/previews", json=plan(plan_id="PLAN-other")).status_code == 409


def test_double_submit_and_expired_replay_return_same_order(context):
    receipt = preview(context)
    first = submit(context, receipt).json()
    context[3][0] += timedelta(hours=1)
    second = submit(context, receipt).json()
    assert first["status"] == second["status"] == "ACKNOWLEDGED"
    assert first["order_id"] == second["order_id"]
    assert preview(context)["order_id"] == first["order_id"]
    assert len(context[2].calls) == context[1].orders.submit.call_count == 1


def test_concurrent_submit_across_service_instances_is_at_most_once(context):
    from aura_integration import ConfirmPreview
    receipt = preview(context)
    services = [AuraIntegrationService(context[4], context[1].orders, lambda: NOW) for _ in range(8)]
    barrier = threading.Barrier(len(services))
    confirmation = ConfirmPreview(confirmed=True, preview_hash=receipt["preview_hash"])
    def send(service):
        barrier.wait(timeout=10)
        return service.submit(receipt["plan_id"], confirmation)
    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        results = list(pool.map(send, services))
    assert len(context[2].calls) == context[1].orders.submit.call_count == 1
    assert len(context[1].orders.store.list()) == 1
    assert all(r["status"] in {"UNKNOWN", "ACKNOWLEDGED"} for r in results)


def test_reopened_database_retains_duplicate_protection(context):
    receipt = preview(context)
    first = submit(context, receipt).json()
    repo = SQLiteLabRepository(context[4].db_path)
    restarted = AuraIntegrationService(repo, KISVirtualOrderService(repo, context[2]), lambda: NOW)
    main.app.dependency_overrides[integration_service] = lambda: restarted
    assert submit(context, receipt).json()["order_id"] == first["order_id"]
    assert len(context[2].calls) == 1


def test_timeout_is_unknown_and_never_retried(context):
    receipt = preview(context)
    context[2].fail_submit = True
    result = submit(context, receipt).json()
    assert result["status"] == "UNKNOWN" and result["lab_status"] == "ERROR"
    assert result["requires_review"] and result["stale"]
    assert submit(context, receipt).json()["order_id"] == result["order_id"]
    assert context[1].get(receipt["plan_id"], refresh=True)["status"] == "UNKNOWN"
    assert len(context[2].calls) == 1 and context[2].inquiries == []


def test_interrupted_claim_without_order_never_retries(context):
    receipt = preview(context)
    assert context[1].store.claim(receipt["plan_id"], NOW.isoformat())
    response = submit(context, receipt).json()
    assert response["status"] == "UNKNOWN" and response["requires_review"]
    assert context[2].calls == []


def test_receipt_persistence_failure_recovers_by_fixed_key(context, monkeypatch):
    receipt = preview(context)
    original = context[1].store.attach
    count = [0]
    def fail_once(*args):
        count[0] += 1
        if count[0] == 1:
            raise RuntimeError("DB receipt failure")
        return original(*args)
    monkeypatch.setattr(context[1].store, "attach", fail_once)
    result = submit(context, receipt).json()
    assert result["status"] == "ACKNOWLEDGED"
    assert context[1].store.get(receipt["plan_id"])["lab_order_id"] == result["order_id"]
    assert submit(context, receipt).json()["order_id"] == result["order_id"]
    assert len(context[2].calls) == 1


@pytest.mark.parametrize("change", ["account", "product", "key"])
def test_changed_account_context_blocks_submit(context, monkeypatch, change):
    receipt = preview(context)
    if change == "account": monkeypatch.setattr(settings, "KIS_ACCOUNT_NO", "87654321-01")
    elif change == "product": monkeypatch.setattr(settings, "KIS_ACCOUNT_NO", "12345678-02")
    else: monkeypatch.setattr(settings, "KIS_APP_KEY", "other-key")
    assert submit(context, receipt).status_code == 409
    assert context[1].get(receipt["plan_id"])["preview"]["submit_enabled"] is False
    assert context[2].calls == []


@pytest.mark.parametrize("flag", ["PAPER_ORDER_ENABLED", "KIS_VIRTUAL_ORDER_SUBMIT_ENABLED"])
def test_existing_submission_flags_still_apply(context, monkeypatch, flag):
    receipt = preview(context)
    monkeypatch.setattr(settings, flag, False)
    assert submit(context, receipt).status_code == 403
    assert context[2].calls == []
    assert context[1].store.get(receipt["plan_id"])["submission_state"] == "PREVIEWED"


def test_risk_limit_revalidated_at_submit(context, monkeypatch):
    receipt = preview(context)
    monkeypatch.setattr(settings, "MAX_ORDER_AMOUNT", 100000)
    assert submit(context, receipt).status_code == 400
    assert context[2].calls == []


def test_refresh_failure_retains_last_good_order(context):
    receipt = preview(context)
    submit(context, receipt)
    context[2].snapshots = [snapshot(1, 1)]
    path = f"{BASE}/executions/{receipt['plan_id']}?refresh=true"
    good = context[0].get(path).json()
    assert good["status"] == "PARTIALLY_FILLED" and good["filled_quantity"] == 1
    assert good["stale"] is False and good["last_broker_sync_at"]
    context[2].fail_refresh = True
    failed = context[0].get(path).json()
    assert failed["status"] == good["status"]
    assert failed["filled_quantity"] == 1 and failed["stale"] is True
    assert failed["last_broker_sync_at"] == good["last_broker_sync_at"]
    assert failed["error"] == "BROKER_SYNC_FAILED"


def test_local_get_does_not_query_broker_and_missing_snapshot_stays_stale(context):
    receipt = preview(context)
    submit(context, receipt)
    local = context[1].get(receipt["plan_id"])
    assert context[2].inquiries == []
    assert context[1].get(receipt["plan_id"], refresh=True)["stale"] is True
    assert context[1].get(receipt["plan_id"])["status"] == local["status"]
    assert context[1].get(receipt["plan_id"])["last_broker_sync_at"] is None


def test_refresh_to_filled_uses_order_date_kst(context):
    receipt = preview(context)
    submit(context, receipt)
    # Existing service timestamps are real UTC; use a deterministic submitted time.
    order = context[1].orders.store.find_by_idempotency_key(order_key(receipt["plan_id"]))
    context[1].orders.store.transition(order["id"], "ACKNOWLEDGED", submitted_at="2026-09-29T15:30:00+00:00")
    context[2].snapshots = [snapshot(2, 0)]
    result = context[1].get(receipt["plan_id"], refresh=True)
    assert result["status"] == "FILLED" and result["filled_quantity"] == 2
    assert result["average_fill_price"] == 70000
    assert context[2].inquiries == ["20260930"]


def test_broker_rejection_is_not_http_successful_order(context):
    receipt = preview(context)
    context[2].reject = True
    result = submit(context, receipt).json()
    assert result["status"] == "REJECTED" and result["error"] == "BROKER_REJECTED"
    assert result["stale"] is False and result["requires_review"] is False
    submit(context, receipt)
    assert len(context[2].calls) == 1


@pytest.mark.parametrize("column,value", [
    ("order_snapshot_json", lambda s: s.replace('"quantity":2', '"quantity":20')),
    ("order_snapshot_json", lambda s: s.replace('"price":70000', '"price":1')),
    ("order_snapshot_json", lambda s: s.replace('"side":"BUY"', '"side":"SELL"')),
    ("valid_until", lambda s: "2026-12-31T00:00:00+00:00"),
    ("plan_json", lambda s: s.replace('"symbol":"005930"', '"symbol":"000660"')),
])
def test_tampered_stored_preview_is_never_submitted(context, column, value):
    receipt = preview(context)
    link = context[1].store.get(receipt["plan_id"])
    tampered = value(link[column])
    assert tampered != link[column]
    with context[4]._connect() as db:
        db.execute(f"UPDATE aura_execution_links SET {column}=? WHERE execution_id=?",
                   (tampered, receipt["plan_id"]))
    assert submit(context, receipt).status_code == 409
    assert context[2].calls == [] and context[1].orders.store.list() == []
    assert context[1].store.get(receipt["plan_id"])["submission_state"] == "PREVIEWED"


def test_non_virtual_environment_is_forbidden(context, monkeypatch):
    receipt = preview(context)
    monkeypatch.setattr(settings, "KIS_ENV", "real")
    assert context[0].post(BASE + "/previews", json=plan(plan_id="PLAN-real", idempotency_key="aura-plan-real")).status_code == 403
    assert submit(context, receipt).status_code == 403
    result = context[0].get(f"{BASE}/executions/{receipt['plan_id']}").json()
    assert result["preview"]["submit_enabled"] is False
    assert context[2].calls == []


def test_success_without_broker_order_number_is_unknown(context):
    receipt = preview(context)
    context[2].missing_order_number = True
    assert submit(context, receipt).json()["status"] == "UNKNOWN"
    submit(context, receipt)
    assert len(context[2].calls) == 1


def test_responses_and_database_snapshot_do_not_contain_broker_secrets(context):
    receipt = preview(context)
    context[2].reject = True
    response = submit(context, receipt)
    link = context[1].store.get(receipt["plan_id"])
    for text in (json.dumps(receipt), response.text, link["order_snapshot_json"]):
        for secret in ("12345678", "private-key", "private-secret", "private-token"):
            assert secret not in text


def test_unknown_execution_is_404(context):
    assert context[0].get(BASE + "/executions/PLAN-missing").status_code == 404


def test_dashboard_contains_separate_read_only_aura_panel(context):
    page = context[0].get("/ui/dashboard?aura_execution_id=PLAN-demo-001")
    assert page.status_code == 200
    assert "setupAuraPanel()" in page.text and "submitAuraOrder()" in page.text
    assert "auraOrderDetails" in page.text and "aura_execution_id" in page.text
    assert "confirmed:true,preview_hash:auraPreview.preview_hash" in page.text
    assert "자동 재전송하지 않습니다" in page.text


def test_only_one_new_table_and_no_order_schema_change(context):
    with context[4]._connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(aura_execution_links)").fetchall()}
        order_columns = {row[1] for row in db.execute("PRAGMA table_info(kis_virtual_orders)").fetchall()}
    assert "source_idempotency_key" in columns and "lab_order_id" in columns
    assert "plan_id" not in order_columns and "execution_id" not in order_columns
