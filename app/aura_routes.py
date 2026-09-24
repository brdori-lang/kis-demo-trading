from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from aura_integration import AuraExecutionPlan, AuraIntegrationService, ConfirmPreview, Identifier, IntegrationError
from kis_virtual_orders import OrderSafetyError


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


@router.post("/previews")
def preview(payload: AuraExecutionPlan, service=Depends(integration_service)):
    return service.preview(payload)


@router.post("/executions/{execution_id}/submit")
def submit(execution_id: Identifier, payload: ConfirmPreview, service=Depends(integration_service)):
    return service.submit(execution_id, payload)


@router.get("/executions/{execution_id}")
def result(execution_id: Identifier, refresh: bool = Query(False), service=Depends(integration_service)):
    return service.get(execution_id, refresh=refresh)
