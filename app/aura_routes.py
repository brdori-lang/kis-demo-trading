import hmac
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

import secrets

from pydantic import BaseModel, Field

from aura_realtime import MAX_STREAM_SYMBOLS, realtime_events, update_stream_symbols
from aura_integration import (
    AuraExecutionPlan, AuraIntegrationService, AuraManualOrder, ConfirmPreview, Identifier, IntegrationError,
)
from kis_virtual_orders import OrderSafetyError
from kis_api import get_buying_power
from config import settings


class IntegrationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default errors echo input, possibly including supplied secrets.
                return JSONResponse(status_code=422, content={"detail": [
                    {"loc": e["loc"], "type": e["type"], "msg": "Invalid integration input"}
                    for e in exc.errors()
                ]})
            except HTTPException:
                raise
            except IntegrationError as exc:
                raise HTTPException(exc.status_code, str(exc)) from None
            except OrderSafetyError:
                raise HTTPException(403, "KIS VTS 주문 제출이 비활성화되어 있습니다.") from None
            except ValueError:
                raise HTTPException(400, "KIS VTS 주문 검증에 실패했습니다.") from None
            except Exception:
                raise HTTPException(503, "AURA 연결 처리에 실패했습니다. 자동 재주문하지 마세요.") from None
        return safe_handler


router = APIRouter(prefix="/api/integrations/aura", tags=["AURA integration"], route_class=IntegrationRoute)


def integration_service():
    # Resolve the existing factory lazily; do not create DBs during router import.
    from app.main import kis_order_service, store
    return AuraIntegrationService(store.repository, kis_order_service())


def require_aura_read_key(x_aura_read_key: str | None = Header(default=None)):
    expected = settings.AURA_INTEGRATION_READ_KEY
    if not expected or not x_aura_read_key or not hmac.compare_digest(expected, x_aura_read_key):
        raise HTTPException(403, "M:ONE 읽기 권한이 필요합니다.")


@router.get("/buying-power", dependencies=[Depends(require_aura_read_key)])
def buying_power(stock_code: str = Query(pattern=r"^[0-9]{6}$"),
                 order_price: int = Query(gt=0, le=100_000_000)):
    return get_buying_power(stock_code, order_price)


def order_service():
    from app.main import kis_order_service
    return kis_order_service()


def public_order(order: dict, origins: dict | None = None) -> dict:
    fields = ("id", "created_at", "stock_code", "side", "quantity", "filled_quantity",
              "remaining_quantity", "requested_price", "status")
    item = {name: order.get(name) for name in fields}
    if origins is not None:
        # Provenance for M:ONE: which of its executions sent this order (None: not sent by M:ONE).
        execution_id, origin = origins.get(order.get("id"), (None, None))
        item.update(mone_execution_id=execution_id, mone_origin=origin)
    return item


def order_origins(service=Depends(integration_service)):
    try:
        return service.store.origins()
    except Exception:
        return None   # unknown provenance is reported as unknown, never as external


def public_reconciliation(run: dict | None) -> dict | None:
    if not run:
        return None
    fields = ("created_at", "matched", "corrected", "mismatch", "manual_review_required")
    return {name: run.get(name) for name in fields}


@router.get("/orders", dependencies=[Depends(require_aura_read_key)])
def orders(refresh: bool = Query(False), service=Depends(order_service), origins=Depends(order_origins)):
    if refresh:
        service.refresh()
    return {"orders": [public_order(item, origins) for item in service.store.list()[:50]]}


PUBLIC_EVENT_DETAILS = ("message_code", "message", "source", "notice")


def public_event(event: dict) -> dict:
    # Only the broker's own code/message and where the event came from; never raw payloads.
    details = event.get("details") or {}
    item = {"at": event.get("created_at"), "event_type": event.get("event_type")}
    item.update({key: str(details[key])[:200] for key in PUBLIC_EVENT_DETAILS if details.get(key) not in (None, "")})
    return item


@router.get("/orders/{order_id}/events", dependencies=[Depends(require_aura_read_key)])
def order_events(order_id: Identifier, service=Depends(order_service)):
    # Read-only: the recorded status events of one order (M:ONE trade journal: ack / fill / reject reason).
    order = service.store.get(order_id)
    if not order:
        raise HTTPException(404, "주문을 찾을 수 없습니다.")
    return {"order_id": order_id, "status": order["status"],
            "events": [public_event(event) for event in service.store.events(order_id)]}


@router.get("/reconciliation", dependencies=[Depends(require_aura_read_key)])
def reconciliation(service=Depends(order_service)):
    runs = service.store.reconciliation_runs(1)
    return {"reconciliation": public_reconciliation(runs[0] if runs else None)}


@router.post("/reconciliation/refresh", dependencies=[Depends(require_aura_read_key)])
def refresh_reconciliation(service=Depends(order_service)):
    service.reconcile()
    return {"reconciliation": public_reconciliation(service.store.reconciliation_runs(1)[0])}


@router.post("/orders/{order_id}/cancel", dependencies=[Depends(require_aura_read_key)])
def cancel_order(order_id: Identifier, service=Depends(order_service)):
    order = service.store.get(order_id)
    if not order:
        raise HTTPException(404, "주문을 찾을 수 없습니다.")
    if order["status"] not in {"ACKNOWLEDGED", "PARTIALLY_FILLED"} or order["remaining_quantity"] <= 0:
        raise HTTPException(409, "취소 가능한 미체결 주문이 아닙니다.")
    result = service.cancel(order_id)
    return {"order": public_order(result)}


def realtime_service():
    from realtime_quotes import REALTIME_ORDERBOOK_TR_ID, REALTIME_PRICE_TR_ID, RealtimeQuoteService
    return RealtimeQuoteService(tr_ids=(REALTIME_PRICE_TR_ID, REALTIME_ORDERBOOK_TR_ID),
                                notice_tr_key=settings.KIS_HTS_ID or None)


def order_notifications():
    # H0STCNI9 needs the HTS ID; without it notices stay off and polling/reconciliation remain.
    from realtime_quotes import valid_hts_id
    if not valid_hts_id(settings.KIS_HTS_ID):
        return None
    from app.main import kis_order_service
    from order_notifications import OrderNotificationProcessor
    return OrderNotificationProcessor(kis_order_service().store)


@router.get("/realtime/stream", dependencies=[Depends(require_aura_read_key)])
def realtime_stream(stock_code: str | None = Query(None, pattern=r"^[0-9]{6}$"),
                    stock_codes: str | None = Query(None, pattern=r"^[0-9]{6}(,[0-9]{6}){0,19}$"),
                    service=Depends(realtime_service), notices=Depends(order_notifications)):
    # One LAB-owned KIS VTS WebSocket session per M:ONE relay; closed when M:ONE disconnects.
    # stock_codes (max 20) lets M:ONE's market collector cover its watchlist with one session.
    if (stock_code is None) == (stock_codes is None):
        raise HTTPException(422, "stock_code 또는 stock_codes 중 하나만 지정하세요.")
    codes = stock_code if stock_codes is None else stock_codes.split(",")
    stream_id = secrets.token_urlsafe(16) if stock_codes is not None else None   # the collector's stream
    return StreamingResponse(realtime_events(service, codes, notices=notices, stream_id=stream_id),
                             media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


class RealtimeSubscriptions(BaseModel):
    stream_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    stock_codes: list[str] = Field(min_length=1, max_length=MAX_STREAM_SYMBOLS)


@router.put("/realtime/subscriptions", dependencies=[Depends(require_aura_read_key)])
async def realtime_subscriptions(payload: RealtimeSubscriptions):
    # M:ONE's market collector changes its open stream's symbols on the same KIS session (no reconnect).
    # async on purpose: it runs on the event loop that owns the stream's RealtimeQuoteService.
    try:
        result = await update_stream_symbols(payload.stream_id, payload.stock_codes)
    except ValueError as exc:
        raise HTTPException(422, "종목코드는 6자리 숫자 1~20개여야 합니다.") from exc
    if result is None:
        raise HTTPException(404, "열린 실시간 스트림이 없습니다.")
    return result


@router.post("/previews")
def preview(payload: AuraExecutionPlan, service=Depends(integration_service)):
    return service.preview(payload)


@router.post("/manual-previews")
def manual_preview(payload: AuraManualOrder, service=Depends(integration_service)):
    # M:ONE manual order (no research decision): same preview hash, confirmation and submit path.
    return service.preview(payload)


@router.post("/executions/{execution_id}/submit")
def submit(execution_id: Identifier, payload: ConfirmPreview, service=Depends(integration_service)):
    return service.submit(execution_id, payload)


@router.get("/executions/{execution_id}")
def result(execution_id: Identifier, refresh: bool = Query(False), service=Depends(integration_service)):
    return service.get(execution_id, refresh=refresh)
