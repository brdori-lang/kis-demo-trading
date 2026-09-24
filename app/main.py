import asyncio

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from indicators import calculate_technical_indicators
from kis_api import get_account_balance, get_current_price
from market_data import DailyMarketDataService
from kis_virtual_orders import DuplicateOrderError, KISVirtualOrderService, OrderSafetyError
from operations import JOB_DEFINITIONS, OperationsService
from realtime_quotes import RealtimeQuoteService
from app.operations_ui import render_operations_ui
from app.aura_routes import router as aura_router
from strategy_engine import evaluate_lab_strategy_v1
from trading_lab import calculate_signal, search_stocks, stock_name, store


app = FastAPI(title="KIS Open API LAB")
app.include_router(aura_router)

StockCode = Annotated[str, Field(pattern=r"^\d{6}$")]


class SecureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class WatchRequest(SecureRequest):
    stock_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ConditionRequest(SecureRequest):
    stock_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    buy_below: int | None = Field(default=None, gt=0, le=10_000_000)
    sell_above: int | None = Field(default=None, gt=0, le=10_000_000)


class MockOrderRequest(SecureRequest):
    stock_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: int = Field(le=1_000_000)
    price: int | None = Field(default=None, le=10_000_000)


class OperationSettingsRequest(SecureRequest):
    automation_enabled: bool | None = None
    dry_run: bool | None = None
    max_positions: int | None = Field(default=None, ge=1, le=50)
    max_order_amount: int | None = Field(default=None, ge=10_000)
    stop_loss_rate: float | None = Field(default=None, gt=0, le=100)
    take_profit_rate: float | None = Field(default=None, gt=0, le=1000)
    analysis_schedule: str | None = Field(default=None, min_length=5, max_length=100, pattern=r"^(?:([01]\d|2[0-3]):[0-5]\d|[0-9*/,-]+(?: [0-9*/,-]+){4})$")


class UniverseSettingsRequest(SecureRequest):
    enabled: bool | None = None
    strategy_name: str | None = Field(default=None, min_length=1, max_length=100)
    target_weight: float | None = Field(default=None, gt=0, le=100)


class KISVirtualOrderRequest(SecureRequest):
    idempotency_key: str = Field(min_length=8, max_length=100)
    stock_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price: int = Field(ge=0)
    order_type: Literal["LIMIT", "MARKET"]
    execution_mode: Literal["KIS_VIRTUAL"] = "KIS_VIRTUAL"


class CurrentPriceResponse(BaseModel):
    stock_code: str
    current_price: str | None = None
    change: str | None = None
    change_rate: str | None = None


class HoldingResponse(BaseModel):
    stock_code: str | None = None
    stock_name: str | None = None
    quantity: str | None = None
    average_price: str | None = None
    evaluation_amount: str | None = None
    profit_loss_amount: str | None = None
    profit_loss_rate: str | None = None


class AccountBalanceResponse(BaseModel):
    total_evaluation_amount: str | None = None
    total_purchase_amount: str | None = None
    deposit_amount: str | None = None
    holdings: list[HoldingResponse]


class DashboardResponse(BaseModel):
    account: AccountBalanceResponse
    account_available: bool
    account_error: str | None = None
    watchlist: list[dict[str, Any]]
    orders: list[dict[str, Any]]


def operations_service():
    return OperationsService(store.repository)


def kis_order_service():
    return KISVirtualOrderService(store.repository)


@app.get("/")
def root():
    return {"message": "KIS Open API LAB 정상 실행"}


@app.get("/ui/account-balance", response_class=HTMLResponse)
def account_balance_ui():
    return """
    <!doctype html>
    <html lang="ko">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>KIS 계좌 잔고</title>
      <style>
        body { font-family: Arial, sans-serif; background: #f5f7fb; margin: 0; padding: 32px; color: #1f2937; }
        .wrap { max-width: 1100px; margin: 0 auto; background: #fff; border-radius: 16px; box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08); padding: 24px; }
        h1 { margin-top: 0; }
        .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: #eef5ff; border-radius: 12px; padding: 16px; }
        .label { display: block; font-size: 12px; color: #64748b; margin-bottom: 8px; }
        .value { font-size: 24px; font-weight: 700; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { border-bottom: 1px solid #e5e7eb; padding: 12px 10px; text-align: left; }
        th { background: #f8fafc; }
        button { margin-top: 16px; background: #2563eb; color: white; border: none; border-radius: 10px; padding: 10px 18px; font-size: 14px; cursor: pointer; }
      </style>
    </head>
    <body>
      <div class="wrap">
        <h1>KIS 계좌 잔고</h1>
        <div id="status">조회 중...</div>
        <div class="summary">
          <div class="card"><span class="label">총 평가금액</span><div id="totalEvaluation" class="value">-</div></div>
          <div class="card"><span class="label">총 매입금액</span><div id="totalPurchase" class="value">-</div></div>
          <div class="card"><span class="label">예수금</span><div id="deposit" class="value">-</div></div>
        </div>
        <button id="refreshButton">새로고침</button>
        <table>
          <thead><tr><th>종목코드</th><th>종목명</th><th>보유수량</th><th>평균단가</th><th>평가금액</th><th>손익금액</th><th>손익률</th></tr></thead>
          <tbody id="holdingRows"></tbody>
        </table>
      </div>
      <script>
        async function loadBalance() {
          const statusEl = document.getElementById('status');
          const holdingRowsEl = document.getElementById('holdingRows');
          statusEl.textContent = '조회 중...';
          try {
            const response = await fetch('/api/account/balance');
            if (!response.ok) throw new Error('balance request failed');
            const data = await response.json();
            document.getElementById('totalEvaluation').textContent = data.total_evaluation_amount || '0';
            document.getElementById('totalPurchase').textContent = data.total_purchase_amount || '0';
            document.getElementById('deposit').textContent = data.deposit_amount || '0';
            const holdings = Array.isArray(data.holdings) ? data.holdings : [];
            holdingRowsEl.innerHTML = holdings.length ? holdings.map(item => `
              <tr>
                <td>${item.stock_code || '-'}</td><td>${item.stock_name || '-'}</td>
                <td>${item.quantity || '0'}</td><td>${item.average_price || '0'}</td>
                <td>${item.evaluation_amount || '0'}</td><td>${item.profit_loss_amount || '0'}</td>
                <td>${item.profit_loss_rate || '0'}%</td>
              </tr>`).join('') : '<tr><td colspan="7">보유종목이 없습니다.</td></tr>';
            statusEl.textContent = '조회 완료';
          } catch (error) {
            statusEl.textContent = '잔고 조회에 실패했습니다.';
          }
        }
        document.getElementById('refreshButton').addEventListener('click', loadBalance);
        loadBalance();
      </script>
    </body>
    </html>
    """


@app.get("/api/price/{stock_code}", response_model=CurrentPriceResponse)
def current_price(stock_code: StockCode):
    try:
        result = get_current_price(stock_code)
        output = result.get("output", {})
        return {
            "stock_code": stock_code,
            "current_price": output.get("stck_prpr"),
            "change": output.get("prdy_vrss"),
            "change_rate": output.get("prdy_ctrt"),
        }
    except Exception:
        raise HTTPException(status_code=502, detail="현재가 조회에 실패했습니다.") from None


@app.get("/api/account/balance", response_model=AccountBalanceResponse)
def account_balance():
    try:
        result = get_account_balance()
        output1_raw = result.get("output1", {})
        output2_raw = result.get("output2", [])

        records = []
        for output in (output1_raw, output2_raw):
            if isinstance(output, list):
                records.extend(item for item in output if isinstance(item, dict))
            elif isinstance(output, dict):
                records.append(output)

        summary = next(
            (
                item
                for item in records
                if any(key in item for key in ["dnca_tot_amt", "tot_evlu_amt", "tot_pchs_amt", "nass_amt"])
            ),
            {},
        )

        holdings = []
        for item in records:
            if any(key in item for key in ["pdno", "prdt_name", "hldg_qty", "pchs_avg_pric"]):
                holdings.append(
                    {
                        "stock_code": item.get("pdno"),
                        "stock_name": item.get("prdt_name"),
                        "quantity": item.get("hldg_qty"),
                        "average_price": item.get("pchs_avg_pric"),
                        "evaluation_amount": item.get("evlu_amt"),
                        "profit_loss_amount": item.get("evlu_pfls_amt"),
                        "profit_loss_rate": item.get("evlu_pfls_rt"),
                    }
                )

        total_eval = summary.get("tot_evlu_amt") or summary.get("nass_amt")
        total_purchase = summary.get("tot_pchs_amt") or summary.get("pchs_amt_smtl_amt")
        deposit_amount = summary.get("dnca_tot_amt")

        return {
            "total_evaluation_amount": total_eval,
            "total_purchase_amount": total_purchase,
            "deposit_amount": deposit_amount,
            "holdings": holdings,
        }
    except Exception:
        raise HTTPException(status_code=502, detail="잔고 조회에 실패했습니다.") from None


def _price_for(stock_code: str):
    result = get_current_price(stock_code)
    value = result.get("output", {}).get("stck_prpr")
    if value is None:
        raise ValueError("현재가가 없습니다.")
    return int(value)


@app.get("/api/stocks/search")
def stock_search(q: Annotated[str, Query(min_length=1, max_length=100)]):
    return {"items": search_stocks(q)}


@app.get("/api/stocks/{stock_code}/daily")
def daily_prices(
    stock_code: StockCode,
    days: int = Query(default=100, ge=1, le=100),
    refresh: bool = False,
):
    try:
        service = DailyMarketDataService(store.repository)
        return service.get_daily_prices(stock_code, days, refresh)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=502, detail="일봉 조회에 실패했습니다.") from None


@app.get("/api/stocks/{stock_code}/indicators")
def technical_indicators(
    stock_code: StockCode,
    recent: int = Query(default=20, ge=1, le=100),
):
    try:
        service = DailyMarketDataService(store.repository)
        market_data = service.get_daily_prices(stock_code, days=100)
        indicator_items = calculate_technical_indicators(market_data["items"])
        latest = indicator_items[-1] if indicator_items else None
        return {
            "stock_code": market_data["stock_code"],
            "as_of_date": latest["trade_date"] if latest else None,
            "data_points": len(indicator_items),
            "latest": latest,
            "items": indicator_items[-recent:],
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=502, detail="기술지표 계산에 실패했습니다.") from None


@app.get("/api/stocks/{stock_code}/strategy")
def lab_strategy(stock_code: StockCode):
    try:
        service = DailyMarketDataService(store.repository)
        market_data = service.get_daily_prices(stock_code, days=100)
        indicators = calculate_technical_indicators(market_data["items"])
        closes = {item["trade_date"]: item["close_price"] for item in market_data["items"]}
        rows = [dict(item, close_price=closes[item["trade_date"]]) for item in indicators]
        result = evaluate_lab_strategy_v1(rows)
        return {"stock_code": market_data["stock_code"], **result}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=502, detail="Strategy 평가에 실패했습니다.") from None


@app.post("/api/watchlist")
def add_watchlist(payload: WatchRequest):
    return {"item": store.add_watch(payload.stock_code, stock_name(payload.stock_code))}


@app.get("/api/watchlist")
def get_watchlist():
    items = []
    for item in store.list_watch():
        enriched = dict(item)
        try:
            current = _price_for(item["stock_code"])
            enriched["current_price"] = current
            enriched["signal"] = calculate_signal(current, store.get_condition(item["stock_code"]))
        except Exception:
            enriched["current_price"] = None
            enriched["signal"] = "UNAVAILABLE"
        items.append(enriched)
    return {"items": items}


@app.post("/api/conditions")
def set_condition(payload: ConditionRequest):
    try:
        return {"condition": store.set_condition(payload.stock_code, payload.buy_below, payload.sell_above)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.get("/api/signals/{stock_code}")
def get_signal(stock_code: StockCode):
    try:
        current = _price_for(stock_code)
        condition = store.get_condition(stock_code)
        return {"stock_code": stock_code, "stock_name": stock_name(stock_code), "current_price": current, "condition": condition, "signal": calculate_signal(current, condition)}
    except Exception:
        raise HTTPException(status_code=502, detail="Signal 계산에 필요한 현재가 조회에 실패했습니다.") from None


@app.post("/api/mock-orders")
def create_mock_order(payload: MockOrderRequest):
    try:
        price = payload.price if payload.price is not None else _price_for(payload.stock_code)
        return {"order": store.add_order(payload.stock_code, stock_name(payload.stock_code), payload.side, payload.quantity, price)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=502, detail="모의주문 가격 조회에 실패했습니다.") from None


@app.get("/api/mock-orders")
def get_mock_orders():
    return {"orders": store.list_orders()}


@app.post("/api/kis/orders/preview")
def preview_kis_virtual_order(payload: KISVirtualOrderRequest):
    try:
        return {"preview": kis_order_service().preview(payload.model_dump())}
    except (ValueError, OrderSafetyError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.post("/api/kis/orders")
def submit_kis_virtual_order(payload: KISVirtualOrderRequest):
    try:
        return {"order": kis_order_service().submit(payload.model_dump())}
    except OrderSafetyError as error:
        raise HTTPException(status_code=403, detail=str(error)) from None
    except DuplicateOrderError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=502, detail="KIS VTS 주문 제출에 실패했습니다.") from None


@app.get("/api/kis/orders")
def list_kis_virtual_orders():
    return {"orders": kis_order_service().store.list()}


@app.get("/api/kis/orders/{order_id}/events")
def list_kis_virtual_order_events(order_id: str):
    return {"events": kis_order_service().store.events(order_id)}


@app.get("/api/kis/orders/{order_id}/executions")
def list_kis_virtual_order_executions(order_id: str):
    return {"executions": kis_order_service().store.executions(order_id)}


@app.post("/api/kis/orders/refresh")
def refresh_kis_virtual_orders():
    try:
        return {"orders": kis_order_service().refresh()}
    except Exception:
        raise HTTPException(status_code=502, detail="KIS VTS 주문 조회에 실패했습니다.") from None


@app.post("/api/kis/orders/{order_id}/cancel")
def cancel_kis_virtual_order(order_id: str):
    try:
        return {"order": kis_order_service().cancel(order_id)}
    except OrderSafetyError as error:
        raise HTTPException(status_code=403, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=502, detail="KIS VTS 주문 취소에 실패했습니다.") from None


@app.post("/api/kis/reconcile")
def reconcile_kis_virtual_orders():
    try:
        return {"reconciliation": kis_order_service().reconcile()}
    except Exception:
        raise HTTPException(status_code=502, detail="KIS VTS 정합성 검증에 실패했습니다.") from None


@app.get("/api/kis/reconciliations")
def list_kis_reconciliations(limit: int = Query(default=20, ge=1, le=100)):
    return {"reconciliations": kis_order_service().store.reconciliation_runs(limit)}


@app.get("/api/dashboard", response_model=DashboardResponse)
def dashboard_data():
    account_error = None
    try:
        account = account_balance()
    except HTTPException as error:
        account = {
            "total_evaluation_amount": None,
            "total_purchase_amount": None,
            "deposit_amount": None,
            "holdings": [],
        }
        account_error = error.detail

    return {
        "account": account,
        "account_available": account_error is None,
        "account_error": account_error,
        "watchlist": get_watchlist()["items"],
        "orders": store.list_orders(),
    }


@app.get("/api/operations/overview")
def operations_overview():
    return operations_service().overview(store)


@app.get("/api/operations/settings")
def get_operation_settings():
    return operations_service().get_settings()


@app.put("/api/operations/settings")
def update_operation_settings(payload: OperationSettingsRequest):
    try:
        values = payload.model_dump(exclude_none=True)
        return operations_service().save_settings(values)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.get("/api/operations/signals")
def operation_signals():
    return {"items": operations_service().list_signals()}


@app.get("/api/operations/universe")
def operation_universe():
    return {"items": operations_service().list_universe(store)}


@app.put("/api/operations/universe/{stock_code}")
def update_operation_universe(stock_code: StockCode, payload: UniverseSettingsRequest):
    try:
        return operations_service().update_universe(
            store, stock_code, payload.model_dump(exclude_none=True)
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.get("/api/operations/data-status")
def operation_data_status():
    return {"items": operations_service().data_status(store)}


@app.get("/api/operations/backtests")
def operation_backtests():
    return {"items": operations_service().list_backtests()}


@app.get("/api/operations/jobs")
def operation_jobs():
    return {"items": operations_service().list_jobs()}


@app.get("/api/operations/job-runs")
def operation_job_runs(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": operations_service().list_job_runs(limit)}


@app.post("/api/operations/jobs/{job_name}/run")
def run_operation_job(job_name: str):
    if job_name not in JOB_DEFINITIONS:
        raise HTTPException(status_code=404, detail="등록되지 않은 작업입니다.")
    try:
        return operations_service().run_job(job_name, store)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=500, detail="배치 작업 실행에 실패했습니다.") from None


@app.get("/api/operations/logs")
def operation_logs(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": operations_service().list_logs(limit)}


@app.websocket("/ws/quotes")
async def realtime_quotes(websocket: WebSocket):
    await websocket.accept()
    outgoing: asyncio.Queue[dict] = asyncio.Queue()
    service = RealtimeQuoteService()

    async def publish(event: dict):
        await outgoing.put(event)

    async def send_events():
        while True:
            await websocket.send_json(await outgoing.get())

    await service.start(publish)
    sender = asyncio.create_task(send_events())
    try:
        while True:
            request = await websocket.receive_json()
            action = request.get("action")
            stock_code = request.get("stock_code", "")
            if action == "subscribe":
                await service.subscribe(stock_code)
            elif action == "unsubscribe":
                await service.unsubscribe(stock_code)
            else:
                await websocket.send_json({"type": "error", "message": "지원하지 않는 실시간 명령입니다."})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sender.cancel()
        await service.stop()


@app.get("/ui/operations", response_class=HTMLResponse)
def operations_ui():
    return render_operations_ui()
    return """
    <!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>KIS Trading LAB · 운영센터</title><style>
    :root{color-scheme:dark;--bg:#070b14;--panel:#101827;--line:#243147;--text:#f4f7fb;--muted:#8d9bb0;--blue:#4c8dff;--green:#22c99a;--red:#ff5268;--amber:#f6b94b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,"Noto Sans KR",sans-serif}.header{position:sticky;top:0;z-index:2;display:flex;justify-content:space-between;align-items:center;padding:16px 24px;background:#080d17;border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0}.header a{color:#b8c5d8;text-decoration:none}.wrap{max-width:1500px;margin:auto;padding:20px}.tabs{display:flex;gap:8px;overflow:auto;margin-bottom:16px}.tab,.btn{border:1px solid var(--line);background:#111c2e;color:#cbd5e1;border-radius:9px;padding:10px 14px;cursor:pointer;font-weight:700}.tab.active,.btn.primary{background:#285fcf;color:white}.page{display:none}.page.active{display:block}.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.card,.panel{border:1px solid var(--line);background:linear-gradient(145deg,#111a2a,#0c1320);border-radius:12px;padding:16px}.label{font-size:11px;color:var(--muted)}.value{font-size:25px;font-weight:850;margin-top:8px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:12px}.panel h2{font-size:15px;margin:0 0 13px}table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid #1d293b;text-align:left;font-size:12px}th{color:#71839d;font-size:10px}.status{min-height:22px;color:var(--green);font-size:12px;margin:10px 0}.jobs{display:grid;gap:9px}.job{display:flex;justify-content:space-between;align-items:center;border:1px solid var(--line);border-radius:10px;padding:12px}.settings{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.field{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:#09111d;color:white}.badge{padding:4px 8px;border-radius:999px;background:#26354b;font-size:10px}.BUY{color:var(--red)}.SELL{color:#69a6ff}@media(max-width:800px){.wrap{padding:10px}.kpis{grid-template-columns:1fr 1fr}.grid,.settings{grid-template-columns:1fr}.header{padding:12px}.header span{display:none}}
    </style></head><body><header class="header"><h1>KIS Trading LAB · 운영센터</h1><a href="/ui/dashboard">← 트레이딩 대시보드 <span>돌아가기</span></a></header><main class="wrap">
    <nav class="tabs"><button class="tab active" data-page="summary">종합현황</button><button class="tab" data-page="automation">자동매매</button><button class="tab" data-page="quant">퀀트분석</button><button class="tab" data-page="performance">성과</button><button class="tab" data-page="system">시스템관리</button></nav>
    <div id="status" class="status">데이터를 불러오는 중입니다</div>
    <section id="summary" class="page active"><div class="kpis"><div class="card"><div class="label">대상종목</div><div id="watchKpi" class="value">-</div></div><div class="card"><div class="label">퀀트시그널</div><div id="signalKpi" class="value">-</div></div><div class="card"><div class="label">매수시그널</div><div id="buyKpi" class="value">-</div></div><div class="card"><div class="label">모의주문</div><div id="orderKpi" class="value">-</div></div><div class="card"><div class="label">백테스트</div><div id="backtestKpi" class="value">-</div></div></div><div class="grid"><article class="panel"><h2>자동매매 상태</h2><div id="automationState"></div></article><article class="panel"><h2>최근 배치 상태</h2><div id="recentJobs"></div></article></div></section>
    <section id="automation" class="page"><div class="grid"><article class="panel"><h2>대상종목</h2><p class="label">관심종목을 가져온 뒤 종목별 자동매매 사용 여부와 목표비중을 관리합니다.</p><div id="universe"></div><a class="btn" href="/ui/dashboard">관심종목 편집</a></article><article class="panel"><h2>매매설정</h2><div class="settings"><label>최대 보유종목<input id="maxPositions" class="field" type="number"/></label><label>종목당 최대금액<input id="maxAmount" class="field" type="number"/></label><label>손절률(%)<input id="stopLoss" class="field" type="number"/></label><label>익절률(%)<input id="takeProfit" class="field" type="number"/></label></div><p><label><input id="automationEnabled" type="checkbox"/> 자동매매 후보 생성 활성화</label></p><button class="btn primary" onclick="saveSettings()">설정 저장</button></article></div><article class="panel" style="margin-top:12px"><h2>매매 실행</h2><p class="label">실제 주문은 전송하지 않고 주문 후보만 생성합니다.</p><button class="btn primary" onclick="runJob('auto_trade_dry_run')">모의 실행</button><div id="candidates"></div></article></section>
    <section id="quant" class="page"><div class="panel"><h2>퀀트 시그널</h2><button class="btn primary" onclick="runJob('analyze_watchlist')">시그널 분석 실행</button><div id="signals"></div></div><div class="panel" style="margin-top:12px"><h2>백테스팅</h2><button class="btn primary" onclick="runJob('backtest_watchlist')">관심종목 백테스트</button><div id="backtests"></div></div></section>
    <section id="performance" class="page"><div class="grid"><article class="panel"><h2>성과분석</h2><div id="performanceSummary" class="label">백테스트 결과가 쌓이면 평균수익률과 MDD를 분석합니다.</div></article><article class="panel"><h2>리밸런싱</h2><p class="label">퀀트 점수와 최대 보유종목 설정을 바탕으로 자동매매 후보를 생성합니다.</p><button class="btn" onclick="runJob('auto_trade_dry_run')">리밸런싱 후보 계산</button></article></div><article class="panel" style="margin-top:12px"><h2>매매내역</h2><a class="btn" href="/ui/dashboard">모의주문 내역 보기</a></article></section>
    <section id="system" class="page"><div class="grid"><article class="panel"><h2>데몬 / 스케줄러</h2><div id="jobs" class="jobs"></div></article><article class="panel"><h2>시스템 설정</h2><label>분석 스케줄<input id="schedule" class="field"/></label><p class="label">Cron 형식으로 저장됩니다. 현재는 수동 실행 기반입니다.</p><button class="btn" onclick="saveSettings()">저장</button><h2 style="margin-top:20px">데이터 최신성</h2><div id="dataStatus"></div></article></div><article class="panel" style="margin-top:12px"><h2>시스템 로그</h2><div id="logs"></div></article></section>
    </main><script>
    const api=(u,o)=>fetch(u,o).then(async r=>{const d=await r.json();if(!r.ok)throw Error(d.detail||'요청 실패');return d}),fmt=v=>v==null?'-':Number(v).toLocaleString('ko-KR'),table=(heads,rows)=>`<table><thead><tr>${heads.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${heads.length}">데이터가 없습니다</td></tr>`}</tbody></table>`;
    document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.page').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.page).classList.add('active')});
    async function load(){try{const [o,s,b,l,u]=await Promise.all([api('/api/operations/overview'),api('/api/operations/signals'),api('/api/operations/backtests'),api('/api/operations/logs?limit=30'),api('/api/operations/universe')]);watchKpi.textContent=o.watchlist_count;signalKpi.textContent=o.signal_count;buyKpi.textContent=o.buy_signal_count;orderKpi.textContent=o.mock_order_count;backtestKpi.textContent=o.backtest_count;automationState.innerHTML=`<b>${o.automation.automation_enabled?'활성':'대기'}</b><p class="label">모드: 모의 실행 · 최대 ${o.automation.max_positions}종목 · ${fmt(o.automation.max_order_amount)}원</p>`;maxPositions.value=o.automation.max_positions;maxAmount.value=o.automation.max_order_amount;stopLoss.value=o.automation.stop_loss_rate;takeProfit.value=o.automation.take_profit_rate;automationEnabled.checked=o.automation.automation_enabled;schedule.value=o.automation.analysis_schedule;recentJobs.innerHTML=o.latest_jobs.map(j=>`<p>${j.description}: <span class="badge">${j.latest_run?.status||'미실행'}</span></p>`).join('');jobs.innerHTML=o.latest_jobs.map(j=>`<div class="job"><span>${j.description}<br><small class="label">${j.latest_run?.finished_at||'실행 이력 없음'}</small></span><button class="btn" onclick="runJob('${j.job_name}')">실행</button></div>`).join('');universe.innerHTML=table(['사용','종목','전략','목표비중'],u.items.map(x=>`<tr><td><input type="checkbox" ${x.enabled?'checked':''} onchange="setUniverse('${x.stock_code}',this.checked)"></td><td>${x.stock_name}<br><small>${x.stock_code}</small></td><td>${x.strategy_name}</td><td>${x.target_weight??'-'}%</td></tr>`));dataStatus.innerHTML=table(['종목','건수','최신일'],o.data_status.map(x=>`<tr><td>${x.stock_name}</td><td>${x.data_points}</td><td>${x.latest_trade_date||'-'}</td></tr>`));signals.innerHTML=table(['종목','기준일','시그널','점수'],s.items.map(x=>`<tr><td>${x.stock_name}<br><small>${x.stock_code}</small></td><td>${x.as_of_date||'-'}</td><td class="${x.signal}">${x.signal}</td><td>${x.score}</td></tr>`));backtests.innerHTML=table(['종목','수익률','MDD','거래수','실행일'],b.items.map(x=>`<tr><td>${x.stock_code}</td><td>${(x.total_return*100).toFixed(2)}%</td><td>${(x.mdd*100).toFixed(2)}%</td><td>${x.trade_count}</td><td>${x.created_at.slice(0,19)}</td></tr>`));performanceSummary.textContent=o.average_backtest_return==null?'백테스트를 먼저 실행하세요':`평균 백테스트 수익률 ${(o.average_backtest_return*100).toFixed(2)}%`;logs.innerHTML=table(['시간','레벨','분류','메시지'],l.items.map(x=>`<tr><td>${x.created_at.slice(0,19)}</td><td>${x.level}</td><td>${x.category}</td><td>${x.message}</td></tr>`));status.textContent='운영 데이터 갱신 완료'}catch(e){status.textContent=e.message}}
    async function saveSettings(){try{await api('/api/operations/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({automation_enabled:automationEnabled.checked,dry_run:true,max_positions:Number(maxPositions.value),max_order_amount:Number(maxAmount.value),stop_loss_rate:Number(stopLoss.value),take_profit_rate:Number(takeProfit.value),analysis_schedule:schedule.value})});status.textContent='설정을 저장했습니다';load()}catch(e){status.textContent=e.message}}
    async function runJob(name){status.textContent='배치를 실행하는 중입니다';try{const r=await api(`/api/operations/jobs/${name}/run`,{method:'POST'});status.textContent=`${r.job_name}: ${r.items.length}건 완료`;if(name==='auto_trade_dry_run')candidates.innerHTML=table(['종목','시그널','점수','최대금액'],r.items.map(x=>`<tr><td>${x.stock_name}</td><td>${x.signal}</td><td>${x.score}</td><td>${fmt(x.max_order_amount)}원</td></tr>`));load()}catch(e){status.textContent=e.message}}
    async function setUniverse(code,enabled){try{await api(`/api/operations/universe/${code}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});status.textContent='대상종목 설정을 저장했습니다';load()}catch(e){status.textContent=e.message}}
    load();</script></body></html>
    """


@app.get("/ui/dashboard", response_class=HTMLResponse)
def dashboard_ui():
    return """
    <!doctype html>
    <html lang="ko"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>KIS Trading LAB</title><style>
      :root{color-scheme:dark;--bg:#070b14;--surface:#0d1422;--surface-2:#111b2d;--line:#202c40;--text:#f4f7fb;--muted:#8d9bb0;--red:#ff4d61;--red-soft:#351824;--blue:#4c8dff;--blue-soft:#13294b;--green:#23c99a;--amber:#f6b94b;--neutral:#99a6b8;--radius:16px}
      *{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}body{margin:0;background:radial-gradient(circle at 48% -20%,#172b4d 0,transparent 32%),var(--bg);color:var(--text);font-family:Inter,Pretendard,"Noto Sans KR",system-ui,-apple-system,sans-serif;font-variant-numeric:tabular-nums}button,input,select{font:inherit}button{cursor:pointer}.app-header{height:72px;border-bottom:1px solid var(--line);background:#080d17e8;backdrop-filter:blur(14px);position:sticky;top:0;z-index:20}.header-inner{max-width:1540px;height:100%;margin:auto;padding:0 24px;display:flex;align-items:center;justify-content:space-between;gap:18px;min-width:0}.brand{display:flex;align-items:center;gap:13px;min-width:0}.brand>div:last-child{min-width:0}.brand-mark{width:38px;height:38px;flex:0 0 38px;border-radius:11px;display:grid;place-items:center;font-weight:900;background:linear-gradient(145deg,var(--red),#e1253f);box-shadow:0 0 24px #ff4d6140}.brand h1{font-size:17px;margin:0;letter-spacing:.02em;white-space:nowrap}.brand p{font-size:12px;color:var(--muted);margin:4px 0 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.header-actions{display:flex;align-items:center;gap:10px;flex:0 0 auto}.market-state{font-size:12px;color:var(--green);display:flex;align-items:center;gap:7px}.market-state:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}
      .layout{max-width:1540px;margin:auto;padding:22px 24px 38px}.kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:16px}.card,.panel{background:linear-gradient(145deg,#101827,#0c1320);border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 14px 40px #00000024}.kpi{padding:19px 20px;min-height:112px;position:relative;overflow:hidden}.kpi:after{content:"";position:absolute;width:90px;height:90px;border-radius:50%;right:-35px;top:-42px;background:#4c8dff12}.kpi-label{color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.04em}.kpi-value{font-size:25px;font-weight:800;margin-top:14px;letter-spacing:-.03em}.kpi-unit{color:var(--muted);font-size:12px;margin-left:4px}.kpi-note{font-size:11px;color:#66758d;margin-top:7px}.main-grid{display:grid;grid-template-columns:minmax(250px,.8fr) minmax(410px,1.45fr) minmax(280px,.85fr);gap:14px;align-items:stretch}.panel{padding:18px}.panel-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}.panel-title h2{font-size:15px;margin:0}.eyebrow{font-size:10px;color:#6f819b;text-transform:uppercase;letter-spacing:.12em;margin-bottom:6px}.subtle{font-size:11px;color:var(--muted)}
      .btn{border:1px solid transparent;border-radius:9px;padding:10px 13px;font-size:12px;font-weight:750;color:white;background:#28364d;transition:.18s ease}.btn:hover{filter:brightness(1.13);transform:translateY(-1px)}.btn-ghost{background:#111b2b;border-color:#26354b;color:#bdc8d8}.btn-primary{background:linear-gradient(135deg,#3d76ef,#2458c9)}.btn-buy{background:linear-gradient(135deg,#ff5d6f,#e52f49);box-shadow:0 7px 20px #e52f4930}.btn-sell{background:linear-gradient(135deg,#4e91ff,#2865d7);box-shadow:0 7px 20px #2865d730}.btn-block{width:100%}.search-row{display:grid;grid-template-columns:1fr auto;gap:8px}.field{width:100%;border:1px solid #28364c;border-radius:10px;background:#090f1a;color:var(--text);padding:11px 12px;outline:none;transition:.18s}.field:focus{border-color:#4b78c4;box-shadow:0 0 0 3px #356dcc22}.field::placeholder{color:#526176}.search-result{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:8px}.search-result select{min-width:0}.watch-list{display:flex;flex-direction:column;gap:8px;margin-top:16px;max-height:430px;overflow:auto}.watch-item{border:1px solid #1e2a3c;background:#0a111d;border-radius:12px;padding:13px 14px;display:grid;grid-template-columns:1fr auto;gap:8px;transition:.18s;cursor:pointer}.watch-item:hover{border-color:#334a6d;background:#101a2a}.watch-item.active{border-color:#4779c2;background:linear-gradient(135deg,#13233b,#0d1726);box-shadow:inset 3px 0 #4c8dff}.stock-name{font-weight:750;font-size:13px}.stock-code{font-size:10px;color:#6f819a;margin-top:4px}.watch-price{font-weight:800;font-size:14px;text-align:right}.watch-meta{display:flex;align-items:center;gap:7px;margin-top:8px;color:var(--muted);font-size:10px}.badge{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;padding:5px 9px;font-size:10px;font-weight:900;letter-spacing:.05em}.badge.BUY,.side-buy{color:#ff6c7c;background:var(--red-soft);border:1px solid #6b2a3a}.badge.SELL,.side-sell{color:#68a4ff;background:var(--blue-soft);border:1px solid #244d84}.badge.HOLD,.badge.UNAVAILABLE{color:#a8b3c3;background:#202a38;border:1px solid #354255}.empty{color:#66758a;text-align:center;padding:30px 12px;font-size:12px;border:1px dashed #263349;border-radius:11px}.loading{color:#8492a7;animation:pulse 1.2s ease-in-out infinite}@keyframes pulse{50%{opacity:.45}}
      .detail-head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;padding-bottom:17px;border-bottom:1px solid var(--line)}.selected-name{font-size:20px;font-weight:850}.selected-code{color:var(--muted);font-size:11px;margin-top:5px}.hero-price{text-align:right}.hero-price strong{display:block;font-size:30px;letter-spacing:-.04em}.hero-price span{font-size:10px;color:var(--muted)}.signal-hero{display:flex;align-items:center;justify-content:space-between;padding:18px 0}.signal-hero .badge{font-size:15px;padding:9px 15px}.signal-caption{font-size:11px;color:var(--muted);margin-top:6px}.indicator-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}.indicator{background:#09111d;border:1px solid #1d2a3d;border-radius:11px;padding:13px}.indicator-label{color:var(--muted);font-size:10px}.indicator-value{font-size:17px;font-weight:800;margin-top:7px}.condition-box{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}.condition-fields{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;margin-top:10px}.condition-fields .field{padding:9px 10px;font-size:12px}
      .paper-label{display:flex;align-items:center;gap:8px;background:#241b0c;border:1px solid #604819;color:#f7c764;border-radius:10px;padding:10px 12px;font-size:11px;font-weight:750;margin-bottom:15px}.paper-label:before{content:"VTS";font-size:9px;background:#22c99a;color:#061711;padding:3px 5px;border-radius:4px}.order-reference{background:#09111d;border:1px solid #1d2a3d;border-radius:11px;padding:13px;margin-bottom:14px}.order-reference div{display:flex;justify-content:space-between;margin-top:7px;font-size:12px}.order-reference div:first-child{margin-top:0}.order-reference span{color:var(--muted)}.form-group{margin-top:12px}.form-label{display:block;color:var(--muted);font-size:10px;margin-bottom:7px}.order-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:14px}.order-warning{color:#69778c;font-size:10px;line-height:1.55;margin:13px 2px 0}.status-line{min-height:18px;margin-top:10px;font-size:11px;color:var(--green)}
      .bottom-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.table-panel{overflow:hidden;padding:0}.table-head{padding:18px 20px 4px}.table-wrap{overflow-x:auto;padding:0 12px 14px}table{width:100%;border-collapse:collapse;min-width:580px}th{color:#73839b;text-transform:uppercase;letter-spacing:.05em;font-size:9px;font-weight:800;text-align:left;padding:12px 10px;border-bottom:1px solid var(--line)}td{font-size:12px;padding:13px 10px;border-bottom:1px solid #182437;color:#d7deea}tbody tr{transition:.15s}tbody tr:hover{background:#121d2d}tbody tr:last-child td{border-bottom:0}.number{text-align:right}.profit{color:var(--red);font-weight:750}.loss{color:var(--blue);font-weight:750}.neutral{color:var(--neutral)}
      .layout{max-width:1720px;padding:10px 14px 24px}.kpi-grid{gap:7px;margin-bottom:8px}.card,.panel{border-radius:10px;box-shadow:0 8px 24px #0002}.kpi{padding:9px 14px;min-height:64px;display:grid;grid-template-columns:1fr auto;align-items:center}.kpi-value{font-size:18px;margin:0 12px 0 0;grid-row:1/3;grid-column:2}.kpi-label{font-size:9px}.kpi-note{font-size:8px;margin-top:2px}.main-grid{grid-template-columns:minmax(235px,.68fr) minmax(600px,2fr) minmax(270px,.76fr);gap:8px}.panel{padding:11px}.panel-title{margin-bottom:9px}.watch-head{display:grid;grid-template-columns:1fr 72px 54px 48px;gap:5px;padding:8px 7px 5px;color:#60718a;font-size:8px;border-bottom:1px solid var(--line)}.watch-list{gap:0;margin-top:7px;max-height:530px}.watch-item{border:0;border-left:2px solid transparent;border-bottom:1px solid #192538;border-radius:0;padding:8px 7px;grid-template-columns:1fr 72px 54px 48px;align-items:center;gap:5px}.watch-item.active{border-color:#4c8dff;background:#13223a;box-shadow:none}.watch-item .badge{font-size:7px;padding:3px 5px}.stock-name{font-size:10px}.stock-code{font-size:8px}.watch-price{font-size:10px}.watch-change{text-align:right;font-size:8px;font-weight:800}.detail-head{padding-bottom:8px}.selected-name{font-size:16px}.selected-code{font-size:8px}.hero-price strong{display:inline;font-size:24px}.price-change{font-size:9px;font-weight:800;margin-left:7px}.chart-toolbar{height:27px;display:flex;justify-content:space-between;align-items:center;color:#718198;font-size:8px}.legend{display:flex;gap:10px}.legend span:before{content:"";display:inline-block;width:11px;height:2px;margin-right:4px;vertical-align:middle}.legend .sma5:before{background:#f6b94b}.legend .sma20:before{background:#51a8ff}.chart-shell{height:325px;background:#080e18;border:1px solid #1a2739;border-radius:6px;padding:3px;position:relative}.chart-shell canvas{width:100%;height:100%;display:block}.indicator-strip{display:grid;grid-template-columns:130px 1fr 130px;align-items:center;gap:10px;margin-top:6px;padding:7px 9px;background:#09111d;border:1px solid #1d2a3d;border-radius:6px}.indicator-compact span{color:var(--muted);font-size:8px}.indicator-compact strong{display:block;font-size:13px;margin-top:2px}.meter{height:4px;background:#1d293a;border-radius:4px;overflow:hidden}.meter i{display:block;height:100%;background:linear-gradient(90deg,#3973df,#ff5268)}.strategy-line{display:flex;align-items:center;justify-content:space-between;margin-top:6px;padding:6px 9px;border:1px solid #1b293c;border-radius:6px}.condition-box{margin-top:5px;padding:0;border:0}.condition-box summary{cursor:pointer;color:#71839d;font-size:8px;padding:5px 1px}.condition-fields{margin-top:4px;gap:5px}.condition-fields .field{padding:7px;font-size:9px}.paper-label{padding:7px 9px;margin-bottom:8px;font-size:9px}.ticket-tabs{display:grid;grid-template-columns:1fr 1fr;border:1px solid #27354a;border-radius:7px;overflow:hidden;margin-bottom:8px}.ticket-tab{border:0;padding:8px;background:#111a29;color:#8090a6;font-weight:800}.ticket-tab.active.buy{background:var(--red-soft);color:#ff6678}.ticket-tab.active.sell{background:var(--blue-soft);color:#66a3ff}.order-reference{padding:9px;margin-bottom:8px}.order-reference div{font-size:10px;margin-top:5px}.form-group{margin-top:8px}.form-label{display:flex;justify-content:space-between;font-size:9px;margin-bottom:5px}.estimated{display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding:9px 1px;border-block:1px solid var(--line);font-size:10px}.estimated strong{font-size:14px}.order-warning{font-size:8px;margin-top:8px}.bottom-grid{display:block;margin-top:8px}.table-head{padding:8px 11px 0}.data-tabs{display:flex}.data-tab{border:0;background:transparent;color:#718198;padding:8px 11px;border-bottom:2px solid transparent;font-size:9px;font-weight:800}.data-tab.active{color:#e7edf6;border-color:#4c8dff}.table-pane{display:none}.table-pane.active{display:block}.table-wrap{padding:0 8px 8px;max-height:210px}th{font-size:8px;padding:7px 8px}td{font-size:10px;padding:7px 8px}
      @media(max-width:1150px){.main-grid{grid-template-columns:235px minmax(0,1fr)}.order-panel{grid-column:1/-1}.order-layout{display:grid;grid-template-columns:1fr 1fr;gap:12px}}@media(max-width:720px){.app-header{height:auto}.header-inner{padding:10px 12px;gap:8px}.brand{gap:9px}.brand p{display:none}.header-actions .btn{padding:9px}.market-state{display:none}.layout{padding:8px;min-width:0}.kpi-grid{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.main-grid{grid-template-columns:minmax(0,1fr)}.kpi-grid>*,.main-grid>*,.panel{min-width:0}.order-panel{grid-column:auto}.order-layout{display:block}.chart-shell{height:270px}.kpi{display:block}.kpi-value{margin-top:5px}.panel{padding:10px}.header-actions .btn span{display:none}}
    </style></head><body>
      <header class="app-header"><div class="header-inner"><div class="brand"><div class="brand-mark">K</div><div><h1>KIS Trading LAB</h1><p>모의투자 데이터 기반 트레이딩 실습 대시보드</p></div></div><div class="header-actions"><div class="market-state" id="marketState">KIS 연결 확인 중</div><a class="btn btn-ghost" href="/ui/operations" style="text-decoration:none">운영센터</a><button class="btn btn-ghost" onclick="load()">↻ <span>전체 갱신</span></button></div></div></header>
      <main class="layout">
        <section class="kpi-grid">
          <article class="card kpi"><div class="kpi-label">총 평가금액</div><div class="kpi-value" id="evalAmount">-</div><div class="kpi-note">KIS 모의투자 계좌 기준</div></article>
          <article class="card kpi"><div class="kpi-label">예수금</div><div class="kpi-value" id="deposit">-</div><div class="kpi-note">주문 전 참고 잔액</div></article>
          <article class="card kpi"><div class="kpi-label">총 매입금액</div><div class="kpi-value" id="purchase">-</div><div class="kpi-note">현재 보유종목 매입 기준</div></article>
          <article class="card kpi"><div class="kpi-label">보유 종목</div><div class="kpi-value"><span id="holdingCount">-</span><span class="kpi-unit">종목</span></div><div class="kpi-note" id="lastUpdated">데이터 불러오는 중</div></article>
        </section>
        <section class="main-grid">
          <article class="panel"><div class="panel-title"><div><div class="eyebrow">Watchlist</div><h2>종목 검색 · 관심종목</h2></div><span class="subtle" id="watchCount">0</span></div>
            <div class="search-row"><input class="field" id="query" placeholder="종목명 또는 005930" autocomplete="off"/><button class="btn btn-primary" onclick="searchStocks()">검색</button></div>
            <div class="search-result"><select class="field" id="results"><option value="">검색 결과를 선택하세요</option></select><button class="btn btn-ghost" onclick="addWatch()">+ 추가</button></div>
            <div class="watch-head"><span>종목</span><span class="number">현재가</span><span class="number">등락</span><span class="number">Signal</span></div>
            <div class="watch-list" id="watch"><div class="empty loading">관심종목을 불러오는 중입니다</div></div>
          </article>
          <article class="panel"><div class="panel-title"><div><div class="eyebrow">Market Overview</div><h2>선택 종목 상세</h2></div><span class="subtle" id="indicatorDate">-</span></div>
            <div id="stockDetail"><div class="empty">관심종목에서 종목을 선택하세요</div></div>
          </article>
          <article class="panel order-panel"><div class="panel-title"><div><div class="eyebrow">KIS VTS LIVE CONNECTION</div><h2>한국투자증권 모의투자 주문</h2></div></div>
            <div class="paper-label" style="background:#10251f;border-color:#285141;color:#7ce3c1">KIS VTS · 실제 모의투자 서버 전송</div><div class="ticket-tabs"><button id="buyTab" class="ticket-tab active buy" onclick="setOrderSide('buy')">매수</button><button id="sellTab" class="ticket-tab" onclick="setOrderSide('sell')">매도</button></div><div class="order-reference"><div><span>선택 종목</span><strong id="orderStock">-</strong></div><div><span>현재가</span><strong id="orderCurrent">-</strong></div><div><span>주문가능금액 참고</span><strong id="availableAmount">-</strong></div></div>
            <div class="form-group"><label class="form-label" for="orderPrice"><span>주문가격</span><span>KRW</span></label><input class="field" id="orderPrice" type="number" min="1" placeholder="현재가" oninput="updateEstimate()"/></div><div class="form-group"><label class="form-label" for="orderQuantity"><span>수량</span><span>주</span></label><input class="field" id="orderQuantity" type="number" min="1" value="1" oninput="updateEstimate()"/></div><div class="estimated"><span>예상 주문금액</span><strong id="estimatedAmount">-</strong></div><button id="orderSubmit" class="btn btn-buy btn-block" style="margin-top:9px" onclick="submitKISVirtualOrder()">VTS 매수 주문 전송</button><div class="status-line" id="orderStatus"></div><p class="order-warning">주문 버튼을 누르면 확인 후 한국투자증권 VTS 모의투자 계좌로 실제 전송됩니다. 실전투자 계좌로는 전송되지 않습니다.</p>
          </article>
        </section>
        <section class="bottom-grid"><article class="panel table-panel"><div class="table-head"><div class="data-tabs"><button id="holdingsTab" class="data-tab active" onclick="showDataTab('holdings')">보유종목</button><button id="ordersTab" class="data-tab" onclick="showDataTab('orders')">KIS VTS 주문 내역</button></div></div><div id="holdingsPane" class="table-pane active table-wrap"><table><thead><tr><th>종목</th><th class="number">수량</th><th class="number">평균단가</th><th class="number">평가금액</th><th class="number">손익금액</th><th class="number">손익률</th></tr></thead><tbody id="holdings"><tr><td colspan="6" class="neutral">불러오는 중</td></tr></tbody></table></div><div id="ordersPane" class="table-pane table-wrap"><table><thead><tr><th>주문번호</th><th>종목</th><th>구분</th><th class="number">수량</th><th class="number">가격</th><th>상태</th></tr></thead><tbody id="orders"><tr><td colspan="6" class="neutral">불러오는 중</td></tr></tbody></table></div></article></section>
      </main><script>
      let dashboard=null,selectedCode=null,orderSide='buy',chartPayload=null,quoteSocket=null,realtimeCode=null,reconnectTimer=null;
      const api=(url,opt)=>fetch(url,opt).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.detail||'요청에 실패했습니다');return d});
      const money=v=>v===null||v===undefined||v===''?'-':`${Number(v).toLocaleString('ko-KR')}원`;
      const number=v=>v===null||v===undefined?'-':Number(v).toLocaleString('ko-KR');
      const decimal=(v,d=2)=>v===null||v===undefined?'-':Number(v).toFixed(d);
      const safe=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
      const badge=s=>`<span class="badge ${safe(s||'UNAVAILABLE')}">${safe(s||'UNAVAILABLE')}</span>`;
      const emptyRow=(cols,text)=>`<tr><td colspan="${cols}" class="neutral">${text}</td></tr>`;
      const auraExecutionId=new URLSearchParams(location.search).get('aura_execution_id');
      let auraPreview=null,auraSubmitting=false,auraRequestVersion=0;
      function setupAuraPanel(){
        if(auraExecutionId===null)return;
        const normalPanel=document.querySelector('.order-panel');normalPanel.style.display='none';
        normalPanel.insertAdjacentHTML('afterend',`<article class="panel" id="auraOrderPanel">
          <div class="panel-title"><h2>AURA 주문 Preview</h2></div>
          <div class="paper-label">KIS_VIRTUAL · 사용자 확인 후 VTS 전송</div>
          <div id="auraOrderDetails" class="order-reference">저장된 Preview를 불러오는 중입니다.</div>
          <p class="order-warning">스냅샷 기준가를 LIMIT 지정가로 사용합니다. 주문 내용은 읽기 전용이며 손절·익절 자동주문은 실행하지 않습니다.</p>
          <button id="auraSubmit" class="btn btn-buy btn-block" disabled onclick="submitAuraOrder()">확인 후 VTS 주문 전송</button>
          <button id="auraRefresh" class="btn btn-block" onclick="loadAuraPreview(true)">주문 상태 새로고침</button>
          <div id="auraOrderStatus" class="status-line" role="status"></div></article>`);
        loadAuraPreview(false);
      }
      function renderAuraPreview(d){
        auraPreview=d;const p=d.preview;
        const rows=[['AURA Plan ID',d.plan_id],['모드',d.execution_mode],['종목',p.stock_code],['방향',p.side],['수량',`${p.quantity}주`],['LIMIT 가격',money(p.price)],['예상 주문금액',money(p.quantity*p.price)],['계좌',p.account],['Preview 만료',new Date(d.expires_at).toLocaleString('ko-KR')]];
        document.getElementById('auraOrderDetails').innerHTML=rows.map(([k,v])=>`<div><span>${safe(k)}</span><strong>${safe(v)}</strong></div>`).join('');
        document.getElementById('auraSubmit').disabled=auraSubmitting||d.status!=='PREVIEWED'||!p.submit_enabled||Date.now()>=Date.parse(d.expires_at);
        document.getElementById('auraOrderStatus').textContent=`${d.status} · 주문번호 ${d.broker_order_id||'-'} · 체결 ${d.filled_quantity} / 잔량 ${d.remaining_quantity}${d.requires_review?' · 접수 여부 확인 필요 · 재주문 금지':''}${d.stale?' · 최신 상태 미확인':''}${d.error?' · '+d.error:''}`;
      }
      async function loadAuraPreview(refresh=false){
        if(auraSubmitting)return;const version=++auraRequestVersion;
        try{const d=await api(`/api/integrations/aura/executions/${encodeURIComponent(auraExecutionId)}?refresh=${refresh}`);if(version===auraRequestVersion)renderAuraPreview(d)}
        catch(e){if(version!==auraRequestVersion)return;document.getElementById('auraSubmit').disabled=true;document.getElementById('auraOrderStatus').textContent=`Preview 조회 실패: ${e.message}`}
      }
      async function submitAuraOrder(){
        if(auraSubmitting||!auraPreview||auraPreview.status!=='PREVIEWED'||!auraPreview.preview.submit_enabled)return;
        if(Date.now()>=Date.parse(auraPreview.expires_at)){document.getElementById('auraSubmit').disabled=true;document.getElementById('auraOrderStatus').textContent='Preview가 만료되었습니다';return}
        const p=auraPreview.preview;
        if(!confirm(`한국투자증권 VTS 모의투자 계좌로 AURA ${auraPreview.plan_id}: ${p.stock_code} ${p.side} ${p.quantity}주를 LIMIT ${money(p.price)}에 전송합니다. 계속할까요?`))return;
        auraSubmitting=true;++auraRequestVersion;document.getElementById('auraSubmit').disabled=true;document.getElementById('auraOrderStatus').textContent='VTS 주문 전송 중...';
        try{const d=await api(`/api/integrations/aura/executions/${encodeURIComponent(auraExecutionId)}/submit`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:true,preview_hash:auraPreview.preview_hash})});auraSubmitting=false;renderAuraPreview(d)}
        catch(e){document.getElementById('auraOrderStatus').textContent=`전송 결과 확인 필요: ${e.message} · 상태를 조회하세요. 자동 재전송하지 않습니다.`}
        finally{auraSubmitting=false}
      }
      function connectQuotes(){clearTimeout(reconnectTimer);const protocol=location.protocol==='https:'?'wss:':'ws:';quoteSocket=new WebSocket(`${protocol}//${location.host}/ws/quotes`);quoteSocket.onopen=()=>{marketState.textContent='KIS 실시간 연결 중';if(selectedCode)subscribeRealtime(selectedCode)};quoteSocket.onmessage=e=>{const event=JSON.parse(e.data);if(event.type==='connection'){marketState.textContent=event.state==='connected'?'KIS VTS Realtime Connected':event.state==='reconnecting'?'KIS 실시간 재연결 중':'KIS 실시간 연결 중';marketState.style.color=event.state==='connected'?'var(--green)':'var(--amber)';if(event.state==='connected'&&selectedCode)subscribeRealtime(selectedCode)}else if(event.type==='quote'){applyRealtimeQuote(event)}};quoteSocket.onclose=()=>{marketState.textContent='KIS 실시간 재연결 대기';marketState.style.color='var(--amber)';realtimeCode=null;reconnectTimer=setTimeout(connectQuotes,2000)}}
      function subscribeRealtime(code){if(!quoteSocket||quoteSocket.readyState!==WebSocket.OPEN)return;if(realtimeCode&&realtimeCode!==code)quoteSocket.send(JSON.stringify({action:'unsubscribe',stock_code:realtimeCode}));realtimeCode=code;quoteSocket.send(JSON.stringify({action:'subscribe',stock_code:code}))}
      function applyRealtimeQuote(q){const item=dashboard?.watchlist.find(x=>x.stock_code===q.stock_code);if(!item)return;const current=Number(q.current_price),change=Number(q.change),rate=Number(q.change_rate);if(Number.isFinite(current)&&current>0)item.current_price=current;if(Number.isFinite(rate))item.changeRate=rate;renderWatch();if(selectedCode!==q.stock_code)return;orderCurrent.textContent=money(current);const priceEl=document.querySelector('.hero-price strong'),changeEl=document.querySelector('.hero-price .price-change');if(priceEl)priceEl.textContent=money(current);if(changeEl){changeEl.className=`price-change ${tone(change)}`;changeEl.textContent=`${change>0?'+':''}${number(change)} (${rate>0?'+':''}${decimal(rate)}%)`}}
      async function searchStocks(){const q=query.value.trim();if(!q)return;results.innerHTML='<option>검색 중...</option>';try{const d=await api('/api/stocks/search?q='+encodeURIComponent(q));results.innerHTML=d.items.length?d.items.map(x=>`<option value="${safe(x.stock_code)}">${safe(x.stock_name)} (${safe(x.stock_code)})</option>`).join(''):'<option value="">검색 결과가 없습니다</option>'}catch(e){results.innerHTML='<option value="">검색에 실패했습니다</option>'}}
      async function addWatch(){if(!results.value)return;await api('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock_code:results.value})});selectedCode=results.value;await load()}
      function tone(v){return Number(v)>0?'profit':Number(v)<0?'loss':'neutral'}
      function renderWatch(){watchCount.textContent=`${dashboard.watchlist.length}종목`;watch.innerHTML=dashboard.watchlist.length?dashboard.watchlist.map(x=>{const validRate=Number.isFinite(x.changeRate);return `<div class="watch-item ${x.stock_code===selectedCode?'active':''}" onclick="subscribeRealtime('${safe(x.stock_code)}');selectStock('${safe(x.stock_code)}')"><div><div class="stock-name">${safe(x.stock_name)}</div><div class="stock-code">${safe(x.stock_code)}</div></div><div class="watch-price">${number(x.current_price)}</div><div class="watch-change ${validRate?tone(x.changeRate):'neutral'}">${validRate?`${x.changeRate>0?'+':''}${decimal(x.changeRate)}%`:'-'}</div><div class="number">${badge(x.signal)}</div></div>`}).join(''):'<div class="empty">등록된 관심종목이 없습니다</div>'}
      async function enrichWatchPrices(){for(const item of dashboard.watchlist){try{const p=await api(`/api/price/${item.stock_code}`),current=Number(p.current_price),rate=p.change_rate===''?NaN:Number(p.change_rate);if(Number.isFinite(current)&&current>0)item.current_price=current;if(Number.isFinite(rate)){item.changeRate=rate}else{const daily=await api(`/api/stocks/${item.stock_code}/daily?days=2`),previous=daily.items.length>1?Number(daily.items[daily.items.length-2].close_price):NaN;item.changeRate=Number.isFinite(previous)&&previous?(item.current_price-previous)/previous*100:undefined}}catch(e){item.changeRate=undefined}renderWatch()}}
      async function selectStock(code){selectedCode=code;renderWatch();const item=dashboard.watchlist.find(x=>x.stock_code===code);orderStock.textContent=item?`${item.stock_name} · ${item.stock_code}`:code;orderCurrent.textContent=money(item?.current_price);orderPrice.value=item?.current_price||'';updateEstimate();stockDetail.innerHTML='<div class="empty loading">100일 시세와 지표를 불러오는 중입니다</div>';try{const [daily,ind,strategy,price]=await Promise.all([api(`/api/stocks/${code}/daily?days=100`),api(`/api/stocks/${code}/indicators?recent=100`),api(`/api/stocks/${code}/strategy`),api(`/api/price/${code}`).catch(()=>null)]);const latest=ind.latest||{},current=Number(price?.current_price||item?.current_price||0),liveChange=price?.change===''?NaN:Number(price?.change),liveRate=price?.change_rate===''?NaN:Number(price?.change_rate),previous=daily.items.length>1?Number(daily.items[daily.items.length-2].close_price):null,change=Number.isFinite(liveChange)?liveChange:Number.isFinite(previous)?current-previous:null,rate=Number.isFinite(liveRate)?liveRate:Number.isFinite(previous)&&previous?change/previous*100:null,changeText=rate===null?'-':`${change>0?'+':''}${number(change)} (${rate>0?'+':''}${decimal(rate)}%)`;if(item){item.current_price=current;if(rate!==null)item.changeRate=rate}orderCurrent.textContent=money(current);orderPrice.value=current||'';updateEstimate();indicatorDate.textContent=ind.as_of_date?`${ind.as_of_date.slice(0,4)}.${ind.as_of_date.slice(4,6)}.${ind.as_of_date.slice(6)}`:'-';stockDetail.innerHTML=`<div class="detail-head"><div><div class="selected-name">${safe(item?.stock_name||code)}</div><div class="selected-code">KRX · ${safe(code)} · 일봉 100</div></div><div class="hero-price"><strong>${money(current)}</strong><span class="price-change ${tone(change)}">${changeText}</span></div></div><div class="chart-toolbar"><div class="legend"><span class="sma5">SMA5</span><span class="sma20">SMA20</span></div><span>수정주가 · 일봉</span></div><div class="chart-shell"><canvas id="priceChart"></canvas></div><div class="indicator-strip"><div class="indicator-compact"><span>RSI 14</span><strong>${decimal(latest.rsi_14)}</strong></div><div class="meter"><i style="width:${Math.max(0,Math.min(100,Number(latest.rsi_14||0)))}%"></i></div><div class="indicator-compact number"><span>VOLUME RATIO</span><strong>${decimal(latest.volume_ratio)}x</strong></div></div><div class="strategy-line"><div><div class="eyebrow">LAB Strategy v1</div><span class="subtle">최근 지표 기준</span></div>${badge(strategy.signal)}</div><details class="condition-box"><summary>Legacy 가격조건 Signal 설정</summary><div class="condition-fields"><input class="field" id="buyCondition" type="number" placeholder="매수 기준가"/><input class="field" id="sellCondition" type="number" placeholder="매도 기준가"/><button class="btn btn-ghost" onclick="saveCondition()">저장</button></div></details>`;chartPayload={bars:daily.items,indicators:ind.items};drawChart();renderWatch()}catch(e){stockDetail.innerHTML=`<div class="empty">상세 데이터를 불러오지 못했습니다<br><span class="subtle">${safe(e.message)}</span></div>`}}
      function drawChart(){if(!chartPayload)return;const canvas=document.getElementById('priceChart');if(!canvas)return;const rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=rect.width,h=rect.height;canvas.width=w*dpr;canvas.height=h*dpr;const c=canvas.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,w,h);const bars=chartPayload.bars,inds=chartPayload.indicators,pad={l:7,r:55,t:12,b:20},pw=w-pad.l-pad.r,ph=h-pad.t-pad.b,values=bars.flatMap(x=>[x.high_price,x.low_price]).concat(inds.flatMap(x=>[x.sma_5,x.sma_20]).filter(x=>x!==null));let min=Math.min(...values),max=Math.max(...values),range=max-min||1;min-=range*.04;max+=range*.04;const y=v=>pad.t+(max-v)/(max-min)*ph,x=i=>pad.l+(i+.5)*pw/bars.length;c.strokeStyle='#182538';c.lineWidth=1;c.fillStyle='#60718a';c.font='9px sans-serif';for(let i=0;i<5;i++){const yy=pad.t+ph*i/4,val=max-(max-min)*i/4;c.beginPath();c.moveTo(pad.l,yy);c.lineTo(pad.l+pw,yy);c.stroke();c.fillText(Math.round(val).toLocaleString(),pad.l+pw+5,yy+3)}const cw=Math.max(2,pw/bars.length*.62);bars.forEach((b,i)=>{const up=b.close_price>=b.open_price,color=up?'#ff4d61':'#4c8dff',xx=x(i);c.strokeStyle=color;c.fillStyle=color;c.beginPath();c.moveTo(xx,y(b.high_price));c.lineTo(xx,y(b.low_price));c.stroke();const top=y(Math.max(b.open_price,b.close_price)),bottom=y(Math.min(b.open_price,b.close_price));c.fillRect(xx-cw/2,top,cw,Math.max(1,bottom-top))});const line=(key,color)=>{c.strokeStyle=color;c.lineWidth=1.4;c.beginPath();let started=false;inds.forEach((v,i)=>{if(v[key]===null)return;const xx=x(i),yy=y(v[key]);started?c.lineTo(xx,yy):c.moveTo(xx,yy);started=true});c.stroke()};line('sma_5','#f6b94b');line('sma_20','#51a8ff');c.fillStyle='#60718a';c.font='8px sans-serif';[0,Math.floor(bars.length/2),bars.length-1].forEach(i=>{const d=bars[i].trade_date;c.fillText(`${d.slice(4,6)}.${d.slice(6)}`,Math.min(x(i)-12,w-78),h-5)})}
      async function saveCondition(){if(!selectedCode)return;const buy=buyCondition.value,sell=sellCondition.value;await api('/api/conditions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock_code:selectedCode,buy_below:buy?Number(buy):null,sell_above:sell?Number(sell):null})});orderStatus.textContent='가격 조건을 저장했습니다';await load()}
      function setOrderSide(side){orderSide=side;buyTab.className=`ticket-tab ${side==='buy'?'active buy':''}`;sellTab.className=`ticket-tab ${side==='sell'?'active sell':''}`;orderSubmit.className=`btn ${side==='buy'?'btn-buy':'btn-sell'} btn-block`;orderSubmit.textContent=`VTS ${side==='buy'?'매수':'매도'} 주문 전송`}
      function updateEstimate(){const price=Number(orderPrice.value||0),quantity=Number(orderQuantity.value||0);estimatedAmount.textContent=price>0&&quantity>0?money(price*quantity):'-'}
      function showDataTab(tab){holdingsTab.classList.toggle('active',tab==='holdings');ordersTab.classList.toggle('active',tab==='orders');holdingsPane.classList.toggle('active',tab==='holdings');ordersPane.classList.toggle('active',tab==='orders')}
      async function submitKISVirtualOrder(){if(!selectedCode){orderStatus.textContent='관심종목을 먼저 선택하세요';return}const quantity=Number(orderQuantity.value),price=Number(orderPrice.value),side=orderSide.toUpperCase();if(!Number.isInteger(quantity)||quantity<1||!Number.isInteger(price)||price<1){orderStatus.textContent='가격과 수량을 확인하세요';return}if(!confirm(`한국투자증권 VTS 모의투자 계좌로 ${selectedCode} ${side==='BUY'?'매수':'매도'} ${quantity}주를 ${money(price)}에 전송합니다. 계속할까요?`))return;const idempotencyKey=`dashboard-${Date.now()}-${crypto.randomUUID()}`;orderSubmit.disabled=true;orderStatus.textContent='KIS VTS 주문 전송 중...';try{const result=await api('/api/kis/orders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idempotency_key:idempotencyKey,stock_code:selectedCode,side,quantity,price,order_type:'LIMIT',execution_mode:'KIS_VIRTUAL'})});orderStatus.textContent=`VTS 주문 접수 · ${result.order.status} · 주문번호 ${result.order.broker_order_id||'-'}`;await load()}catch(e){orderStatus.textContent=e.message}finally{orderSubmit.disabled=false}}
      function renderTables(){const hs=dashboard.account.holdings||[];holdingCount.textContent=hs.length;holdings.innerHTML=hs.length?hs.map(x=>{const rate=Number(x.profit_loss_rate||0),tone=rate>0?'profit':rate<0?'loss':'neutral';return `<tr><td><strong>${safe(x.stock_name)}</strong><div class="stock-code">${safe(x.stock_code)}</div></td><td class="number">${number(x.quantity)}</td><td class="number">${money(x.average_price)}</td><td class="number">${money(x.evaluation_amount)}</td><td class="number ${tone}">${money(x.profit_loss_amount||0)}</td><td class="number ${tone}">${rate>0?'+':''}${decimal(rate)}%</td></tr>`}).join(''):emptyRow(6,'현재 보유종목이 없습니다');orders.innerHTML=dashboard.orders.length?dashboard.orders.map(x=>`<tr><td>${safe(x.broker_order_id||x.id)}</td><td><strong>${safe(x.stock_code)}</strong></td><td><span class="badge side-${safe(x.side.toLowerCase())}">VTS ${x.side==='BUY'?'매수':'매도'}</span></td><td class="number">${number(x.quantity)}</td><td class="number">${money(x.requested_price)}</td><td><span class="neutral">${safe(x.status)}</span></td></tr>`).join(''):emptyRow(6,'KIS VTS 주문 내역이 없습니다')}
      async function load(){lastUpdated.textContent='갱신 중...';try{const [dashboardData,kisOrders]=await Promise.all([api('/api/dashboard'),api('/api/kis/orders')]);dashboard=dashboardData;dashboard.orders=kisOrders.orders;const connected=dashboard.account_available!==false,unit=v=>v===null||v===undefined?'-':`${number(v)}<span class="kpi-unit">원</span>`;evalAmount.innerHTML=unit(dashboard.account.total_evaluation_amount);deposit.innerHTML=unit(dashboard.account.deposit_amount);purchase.innerHTML=unit(dashboard.account.total_purchase_amount);availableAmount.textContent=money(dashboard.account.deposit_amount);marketState.textContent=connected?'KIS VTS Connected':'KIS 계좌 연결 실패';marketState.style.color=connected?'var(--green)':'var(--amber)';if(!selectedCode||!dashboard.watchlist.some(x=>x.stock_code===selectedCode))selectedCode=dashboard.watchlist[0]?.stock_code||null;renderWatch();renderTables();lastUpdated.textContent=connected?`${new Date().toLocaleTimeString('ko-KR')} 기준`:'계좌 조회 실패 · 로컬 기능 사용 가능';if(selectedCode)await selectStock(selectedCode);enrichWatchPrices()}catch(e){lastUpdated.textContent='대시보드 갱신 실패';watch.innerHTML=`<div class="empty">${safe(e.message)}</div>`;orders.innerHTML=emptyRow(6,'내역을 불러오지 못했습니다')}}
      query.addEventListener('keydown',e=>{if(e.key==='Enter')searchStocks()});window.addEventListener('resize',()=>drawChart());setOrderSide('buy');updateEstimate();setupAuraPanel();connectQuotes();load();
    </script></body></html>
    """
