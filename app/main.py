from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from kis_api import get_account_balance, get_current_price
from trading_lab import calculate_signal, search_stocks, stock_name, store


app = FastAPI(title="KIS Open API LAB")


class WatchRequest(BaseModel):
    stock_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ConditionRequest(BaseModel):
    stock_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    buy_below: int | None = None
    sell_above: int | None = None


class MockOrderRequest(BaseModel):
    stock_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    side: str
    quantity: int
    price: int | None = None


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


@app.get("/api/price/{stock_code}")
def current_price(stock_code: str):
    try:
        result = get_current_price(stock_code)
        output = result.get("output", {})
        return {"stock_code": stock_code, "current_price": output.get("stck_prpr")}
    except Exception:
        raise HTTPException(status_code=502, detail="현재가 조회에 실패했습니다.") from None


@app.get("/api/account/balance")
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
def stock_search(q: str):
    return {"items": search_stocks(q)}


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
def get_signal(stock_code: str):
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


@app.get("/api/dashboard")
def dashboard_data():
    return {"account": account_balance(), "watchlist": get_watchlist()["items"], "orders": store.list_orders()}


@app.get("/ui/dashboard", response_class=HTMLResponse)
def dashboard_ui():
    return """
    <!doctype html><html lang="ko"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>KIS Trading LAB</title><style>
      body{margin:0;font-family:Arial,sans-serif;background:#f1f5f9;color:#0f172a}.nav{background:#0f172a;color:white;padding:18px 28px}.wrap{max-width:1200px;margin:auto;padding:24px}
      .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}.card,.panel{background:white;border-radius:14px;padding:18px;box-shadow:0 6px 20px #0f172a12}.panel{margin-top:18px}
      input,select,button{padding:10px;border:1px solid #cbd5e1;border-radius:8px}button{background:#2563eb;color:white;border:0;cursor:pointer}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{padding:10px;border-bottom:1px solid #e2e8f0;text-align:left}
      .BUY{color:#dc2626;font-weight:bold}.SELL{color:#2563eb;font-weight:bold}.HOLD{color:#64748b;font-weight:bold}.row{display:flex;gap:8px;flex-wrap:wrap}
    </style></head><body><div class="nav"><b>KIS 모의투자 Trading LAB</b></div><div class="wrap">
      <div class="grid"><div class="card">총 평가금액<h2 id="evalAmount">-</h2></div><div class="card">총 매입금액<h2 id="purchase">-</h2></div><div class="card">예수금<h2 id="deposit">-</h2></div></div>
      <div class="panel"><h2>종목명 검색 / 관심종목</h2><div class="row"><input id="query" placeholder="삼성전자 또는 005930"/><button onclick="searchStocks()">검색</button><select id="results"></select><button onclick="addWatch()">관심종목 추가</button></div>
      <table><thead><tr><th>종목</th><th>현재가</th><th>Signal</th><th>매수 기준</th><th>매도 기준</th><th>조건</th><th>모의주문</th></tr></thead><tbody id="watch"></tbody></table></div>
      <div class="panel"><h2>보유종목</h2><table><thead><tr><th>종목</th><th>수량</th><th>평균단가</th><th>평가금액</th><th>손익률</th></tr></thead><tbody id="holdings"></tbody></table></div>
      <div class="panel"><h2>모의주문 내역</h2><table><thead><tr><th>ID</th><th>종목</th><th>구분</th><th>수량</th><th>가격</th><th>상태</th></tr></thead><tbody id="orders"></tbody></table></div>
    </div><script>
      const api=(url,opt)=>fetch(url,opt).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.detail||'요청 실패');return d});
      async function searchStocks(){const d=await api('/api/stocks/search?q='+encodeURIComponent(query.value));results.innerHTML=d.items.map(x=>`<option value="${x.stock_code}">${x.stock_name} (${x.stock_code})</option>`).join('')}
      async function addWatch(){if(results.value)await api('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock_code:results.value})});await load()}
      async function saveCondition(code){const buy=document.getElementById('b'+code).value,sell=document.getElementById('s'+code).value;await api('/api/conditions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock_code:code,buy_below:buy?Number(buy):null,sell_above:sell?Number(sell):null})});await load()}
      async function mockOrder(code,side,price){const q=prompt('수량을 입력하세요','1');if(q)await api('/api/mock-orders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock_code:code,side,quantity:Number(q),price})});await load()}
      async function load(){try{const d=await api('/api/dashboard');evalAmount.textContent=d.account.total_evaluation_amount||0;purchase.textContent=d.account.total_purchase_amount||0;deposit.textContent=d.account.deposit_amount||0;
        holdings.innerHTML=d.account.holdings.map(x=>`<tr><td>${x.stock_name} (${x.stock_code})</td><td>${x.quantity}</td><td>${x.average_price}</td><td>${x.evaluation_amount}</td><td>${x.profit_loss_rate||0}%</td></tr>`).join('');
        watch.innerHTML=d.watchlist.map(x=>`<tr><td>${x.stock_name} (${x.stock_code})</td><td>${x.current_price??'-'}</td><td class="${x.signal}">${x.signal}</td><td><input id="b${x.stock_code}" type="number"/></td><td><input id="s${x.stock_code}" type="number"/></td><td><button onclick="saveCondition('${x.stock_code}')">저장</button></td><td><button onclick="mockOrder('${x.stock_code}','buy',${x.current_price})">매수</button> <button onclick="mockOrder('${x.stock_code}','sell',${x.current_price})">매도</button></td></tr>`).join('');
        orders.innerHTML=d.orders.map(x=>`<tr><td>${x.id}</td><td>${x.stock_name}</td><td>${x.side}</td><td>${x.quantity}</td><td>${x.price}</td><td>${x.status}</td></tr>`).join('');
      }catch(e){alert(e.message)}}load();
    </script></body></html>
    """
