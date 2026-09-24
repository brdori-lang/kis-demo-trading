"""AURA v1.0 adapter. The existing VTS service remains the only order sender.

Dispatch is a durable, at-most-once attempt: an interrupted claim is never leased
or retried automatically. Broker uncertainty requires operator reconciliation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from config import settings
from kis_safety import require_virtual_environment
from kis_virtual_orders import KISVirtualOrderService, require_order_submission_enabled


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")]
KST = timezone(timedelta(hours=9))
_refresh_lock = threading.Lock()


class IntegrationError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class AuraRiskCheck(ContractModel):
    name: str = Field(min_length=1, max_length=80)
    passed: StrictBool
    actual: str | float | int | bool | None = None
    limit: str | float | int | bool | None = None
    reason: str = Field(min_length=1, max_length=300)


class AuraProvenance(ContractModel):
    dataset_id: str
    snapshot_id: str
    data_hash: str
    strategy_id: str
    strategy_version: int = Field(gt=0, strict=True)
    backtest_run_id: str | None = None
    ablation_result_reference: str | None = None
    parameter_sweep_reference: str | None = None
    oos_result_reference: str | None = None
    walk_forward_reference: str | None = None
    regime: str
    decision_id: str
    git_commit_hash: str


class AuraExecutionPlan(ContractModel):
    """Full AURA ExecutionPlan.model_dump(), narrowed to actionable VTS plans."""

    execution_contract_version: Literal["1.0"]
    plan_id: Identifier
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    created_at: AwareDatetime
    symbol: str = Field(pattern=r"^[0-9]{6}$")
    side: Literal["BUY", "SELL"]
    signal: Literal["BUY", "SELL", "HOLD"]
    strategy_id: str
    strategy_version: int = Field(gt=0, strict=True)
    decision_id: str
    snapshot_id: str
    data_hash: str
    regime: str
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    target_quantity: int = Field(gt=0, le=1_000_000, strict=True)
    target_weight: float = Field(ge=0, le=1)
    reference_price: Decimal = Field(gt=0)
    max_order_amount: Decimal = Field(gt=0)
    max_position_weight: float = Field(gt=0, le=1)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    valid_until: AwareDatetime
    dispatched_at: AwareDatetime | None = None
    risk_checks: list[AuraRiskCheck] = Field(min_length=1)
    rationale: list[str]
    execution_mode: Literal["KIS_VIRTUAL"]
    status: Literal["READY"]
    provenance: AuraProvenance

    @field_validator("reference_price", "max_order_amount", mode="before")
    @classmethod
    def numeric_amount(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("amount must be a JSON number")
        return value

    @model_validator(mode="after")
    def actionable(self):
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be after created_at")
        if self.reference_price != self.reference_price.to_integral_value():
            raise ValueError("reference_price must be an exact integer KRW amount")
        if not all(check.passed for check in self.risk_checks):
            raise ValueError("all risk checks must pass")
        if self.reference_price * self.target_quantity > self.max_order_amount:
            raise ValueError("AURA max_order_amount exceeded")
        return self


class ConfirmPreview(ContractModel):
    confirmed: StrictBool
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def order_key(plan_id):
    return "aura:" + hashlib.sha256(plan_id.encode("utf-8")).hexdigest()


def _plan_hash(plan_json):
    # AURA may later mark the same plan dispatched; that is not a content change.
    return _hash({k: v for k, v in plan_json.items() if k not in {"status", "dispatched_at"}})


def _preview_hash(plan_hash, snapshot, valid_until):
    return "sha256:" + _hash([plan_hash, snapshot, valid_until])


def _order_values(plan_json):
    return {
        "idempotency_key": order_key(plan_json["plan_id"]), "stock_code": plan_json["symbol"],
        "side": plan_json["side"], "quantity": plan_json["target_quantity"],
        # Already validated as an exact integer; never rounded.
        "price": int(Decimal(str(plan_json["reference_price"]))),
        "order_type": "LIMIT", "execution_mode": "KIS_VIRTUAL",
    }


def _verified_snapshot(link):
    """Only submit a stored snapshot that still matches its confirmed preview hash."""
    snapshot = json.loads(link["order_snapshot_json"])
    plan_json = json.loads(link["plan_json"])
    intact = (
        plan_json.get("plan_id") == link["execution_id"]
        and _plan_hash(plan_json) == link["plan_hash"]
        and datetime.fromisoformat(plan_json["valid_until"]) == datetime.fromisoformat(link["valid_until"])
        and snapshot.get("order") == _order_values(plan_json)
        and hmac.compare_digest(link["preview_hash"],
                                _preview_hash(link["plan_hash"], snapshot, link["valid_until"]))
    )
    if not intact:
        raise IntegrationError("저장된 Preview 무결성 검증에 실패했습니다.")
    return snapshot


def _account_context():
    # Never persist the raw account or credentials. Include the product suffix.
    try:
        require_virtual_environment()
    except RuntimeError:
        raise IntegrationError("KIS Trading LAB은 virtual 환경만 허용합니다.", 403) from None
    return _hash([settings.KIS_ENV, settings.KIS_ACCOUNT_NO.strip(), settings.KIS_APP_KEY])


class AuraExecutionStore:
    def __init__(self, repository):
        self.repository = repository
        with repository._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS aura_execution_links (
                    execution_id TEXT PRIMARY KEY,
                    source_idempotency_key TEXT NOT NULL UNIQUE,
                    plan_hash TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    order_snapshot_json TEXT NOT NULL,
                    preview_hash TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    submission_state TEXT NOT NULL,
                    confirmed_at TEXT,
                    lab_order_id TEXT REFERENCES kis_virtual_orders(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_broker_sync_at TEXT,
                    last_sync_error TEXT
                );
            """)

    def get(self, execution_id):
        with self.repository._connect() as db:
            row = db.execute("SELECT * FROM aura_execution_links WHERE execution_id=?", (execution_id,)).fetchone()
        return dict(row) if row else None

    def create(self, plan, plan_json, plan_hash, snapshot, preview_hash, timestamp):
        # ON CONFLICT works on SQLite and the existing PostgreSQL adapter.
        with self.repository._connect() as db:
            db.execute("""INSERT INTO aura_execution_links (
                execution_id,source_idempotency_key,plan_hash,plan_json,order_snapshot_json,
                preview_hash,valid_until,submission_state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'PREVIEWED',?,?) ON CONFLICT DO NOTHING""",
                (plan.plan_id, plan.idempotency_key, plan_hash, _json(plan_json),
                 _json(snapshot), preview_hash, plan.valid_until.isoformat(), timestamp, timestamp))
        item = self.get(plan.plan_id)
        if not item or item["plan_hash"] != plan_hash:
            raise IntegrationError("Plan ID 또는 idempotency key가 다른 내용에 사용되었습니다.")
        return item

    def claim(self, execution_id, timestamp):
        with self.repository._connect() as db:
            # Do not rely on cursor.rowcount: PostgresCursor exposes fetchone only.
            row = db.execute("""UPDATE aura_execution_links
                SET submission_state='SUBMITTING', confirmed_at=?, updated_at=?
                WHERE execution_id=? AND submission_state='PREVIEWED' AND lab_order_id IS NULL
                RETURNING execution_id""", (timestamp, timestamp, execution_id)).fetchone()
        return row is not None

    def attach(self, execution_id, order_id, timestamp):
        with self.repository._connect() as db:
            db.execute("""UPDATE aura_execution_links SET lab_order_id=?,
                submission_state='SUBMITTED', updated_at=? WHERE execution_id=?""",
                (order_id, timestamp, execution_id))

    def unknown(self, execution_id, timestamp):
        with self.repository._connect() as db:
            db.execute("""UPDATE aura_execution_links SET submission_state='UNKNOWN', updated_at=?
                WHERE execution_id=? AND submission_state='SUBMITTING'""", (timestamp, execution_id))

    def sync_result(self, execution_id, timestamp, error):
        with self.repository._connect() as db:
            db.execute("""UPDATE aura_execution_links SET
                last_broker_sync_at=COALESCE(?,last_broker_sync_at), last_sync_error=?
                WHERE execution_id=?""", (timestamp, error, execution_id))


class AuraIntegrationService:
    def __init__(self, repository, order_service=None, clock=None):
        self.orders = order_service or KISVirtualOrderService(repository)
        self.store = AuraExecutionStore(repository)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return self.clock().astimezone(timezone.utc)

    def _link(self, execution_id):
        item = self.store.get(execution_id)
        if not item:
            raise IntegrationError("AURA 실행계획을 찾을 수 없습니다.", 404)
        return item

    def _recover_order(self, link):
        order = self.orders.store.find_by_idempotency_key(order_key(link["execution_id"]))
        if order and link["lab_order_id"] != order["id"]:
            self.store.attach(link["execution_id"], order["id"], self._now().isoformat())
        return order

    def preview(self, plan: AuraExecutionPlan):
        plan_json = plan.model_dump(mode="json")
        plan_hash = _plan_hash(plan_json)
        existing = self.store.get(plan.plan_id)
        if existing:
            if existing["plan_hash"] != plan_hash:
                raise IntegrationError("동일 Plan ID의 내용을 변경할 수 없습니다.")
            return self.get(plan.plan_id)
        if self._now() >= plan.valid_until:
            raise IntegrationError("Execution Plan이 만료되었습니다.")
        values = _order_values(plan_json)
        context = _account_context()
        preview = self.orders.preview(values)
        if context != _account_context():
            raise IntegrationError("계좌 설정이 변경되었습니다. Preview를 다시 확인하세요.")
        snapshot = {"order": values, "preview": preview, "account_context": context}
        preview_hash = _preview_hash(plan_hash, snapshot, plan.valid_until.isoformat())
        self.store.create(plan, plan_json, plan_hash, snapshot, preview_hash, self._now().isoformat())
        return self.get(plan.plan_id)

    def submit(self, execution_id: str, confirmation: ConfirmPreview):
        link = self._link(execution_id)
        if not confirmation.confirmed:
            raise IntegrationError("사용자 확인이 필요합니다.", 400)
        if not hmac.compare_digest(confirmation.preview_hash, link["preview_hash"]):
            raise IntegrationError("확인한 Preview와 일치하지 않습니다.")
        # Replays, including expired/uncertain attempts, only return existing data.
        order = self._recover_order(link)
        if order or link["submission_state"] != "PREVIEWED":
            return self.get(execution_id)
        if self._now() >= datetime.fromisoformat(link["valid_until"]):
            raise IntegrationError("Preview가 만료되었습니다.")
        snapshot = _verified_snapshot(link)
        if snapshot["account_context"] != _account_context():
            raise IntegrationError("Preview 이후 계좌 설정이 변경되었습니다.")
        require_order_submission_enabled("KIS_VIRTUAL")
        # Repeat current LAB risk validation before irrevocably claiming the attempt.
        self.orders.preview(snapshot["order"])
        if self._now() >= datetime.fromisoformat(link["valid_until"]):
            raise IntegrationError("Preview가 만료되었습니다.")
        if not self.store.claim(execution_id, self._now().isoformat()):
            return self.get(execution_id)
        try:
            if snapshot["account_context"] != _account_context() or self._now() >= datetime.fromisoformat(link["valid_until"]):
                raise IntegrationError("Preview context expired or changed during submission")
            order = self.orders.submit(snapshot["order"])
            self.store.attach(execution_id, order["id"], self._now().isoformat())
        except Exception:
            # Includes transport errors and failures saving a receipt. No retry.
            self.store.unknown(execution_id, self._now().isoformat())
        return self.get(execution_id)

    def get(self, execution_id: str, refresh=False):
        link = self._link(execution_id)
        order = self._recover_order(link)
        snapshot = json.loads(link["order_snapshot_json"])
        if refresh and order and order.get("broker_order_id"):
            # Existing refresh updates account-wide snapshots. Serialize integration
            # refreshes within the supported single-worker competition deployment.
            with _refresh_lock:
                try:
                    if snapshot["account_context"] != _account_context():
                        raise IntegrationError("account context changed")
                    submitted = datetime.fromisoformat(order["submitted_at"] or order["created_at"])
                    updated = self.orders.refresh(submitted.astimezone(KST).strftime("%Y%m%d"))
                    if not any(item["id"] == order["id"] for item in updated):
                        raise IntegrationError("order missing from broker inquiry")
                    self.store.sync_result(execution_id, self._now().isoformat(), None)
                except Exception:
                    self.store.sync_result(execution_id, None, "BROKER_SYNC_FAILED")
                order = self.orders.store.get(order["id"])
        link = self._link(execution_id)
        status = "PREVIEWED"
        lab_status = order["status"] if order else None
        if order:
            status = lab_status if lab_status in {
                "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED"
            } else "UNKNOWN"
            if status in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"} and not order.get("broker_order_id"):
                status = "UNKNOWN"
        elif link["submission_state"] != "PREVIEWED":
            status = "UNKNOWN"
        elif self._now() >= datetime.fromisoformat(link["valid_until"]):
            status = "EXPIRED"
        preview = dict(snapshot["preview"])
        try:
            context_matches = snapshot["account_context"] == _account_context()
        except IntegrationError:
            context_matches = False
        preview["submit_enabled"] = bool(
            status == "PREVIEWED" and context_matches
            and settings.PAPER_ORDER_ENABLED and settings.KIS_VIRTUAL_ORDER_SUBMIT_ENABLED
        )
        error = ("SUBMISSION_UNCERTAIN" if status == "UNKNOWN" else
                 "BROKER_REJECTED" if status == "REJECTED" else
                 "ACCOUNT_CONTEXT_CHANGED" if not context_matches else link["last_sync_error"])
        return {
            "execution_contract_version": "1.0", "delivery_id": execution_id,
            "plan_id": execution_id, "execution_id": execution_id,
            "execution_mode": "KIS_VIRTUAL", "status": status, "lab_status": lab_status,
            "received_at": link["created_at"], "preview_hash": link["preview_hash"],
            "expires_at": link["valid_until"],
            "review_path": f"/ui/dashboard?aura_execution_id={execution_id}", "preview": preview,
            "order_id": order["id"] if order else None,
            "broker_order_id": order["broker_order_id"] if order else None,
            "filled_quantity": order["filled_quantity"] if order else 0,
            "remaining_quantity": order["remaining_quantity"] if order else snapshot["order"]["quantity"],
            "average_fill_price": order["average_fill_price"] if order else None,
            "requires_review": status == "UNKNOWN",
            "last_broker_sync_at": link["last_broker_sync_at"],
            # A synchronous broker rejection has no broker order to sync, so it is final.
            "stale": status == "UNKNOWN" or bool(order and status != "REJECTED" and (
                not link["last_broker_sync_at"] or link["last_sync_error"] or not context_matches)),
            # Never return raw upstream exceptions, messages, request bodies or events.
            "error": error,
        }
