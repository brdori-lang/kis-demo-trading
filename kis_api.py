import httpx
import threading
import time

from auth import get_access_token
from config import settings
from kis_safety import KIS_VTS_REST_BASE_URL, require_virtual_environment, require_vts_rest_url
from security import validate_outbound_url


KIS_VIRTUAL_DOMAIN = KIS_VTS_REST_BASE_URL
_REQUEST_INTERVAL_SECONDS = 1.05
_request_lock = threading.Lock()
_last_request_at = 0.0


def _kis_get(url: str, headers: dict, params: dict):
    require_vts_rest_url(url)
    url = validate_outbound_url(url)
    global _last_request_at
    with _request_lock:
        wait_seconds = _REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            return httpx.get(url, headers=headers, params=params, timeout=10.0)
        finally:
            _last_request_at = time.monotonic()


def _parse_account_info(account_no: str):
    """KIS 계좌번호를 CANO와 ACNT_PRDT_CD로 변환.

    모의투자 계좌는 보통 8자리 숫자(예: 12345678) 또는 8자리-01 형태로 들어오며,
    KIS는 CANO와 ACNT_PRDT_CD로 분리해 전송합니다.
    """
    normalized = "".join(ch for ch in account_no if ch.isdigit())

    if len(normalized) < 8:
        raise ValueError("KIS_ACCOUNT_NO 값이 올바르지 않습니다. 예: 12345678 또는 12345678-01")

    cano = normalized[:8]
    acnt_prdt_cd = normalized[8:10] if len(normalized) >= 10 else "01"

    return cano, acnt_prdt_cd


def get_current_price(stock_code: str):
    require_virtual_environment()
    access_token = get_access_token()

    url = f"{KIS_VIRTUAL_DOMAIN}/uapi/domestic-stock/v1/quotations/inquire-price"

    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": settings.KIS_APP_KEY,
        "appsecret": settings.KIS_APP_SECRET,
        "tr_id": "FHKST01010100",
        "custtype": "P",
    }

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
    }

    response = _kis_get(url, headers, params)

    response.raise_for_status()

    data = response.json()

    return data


def get_account_balance():
    require_virtual_environment()
    access_token = get_access_token()

    account_no = (settings.KIS_ACCOUNT_NO or "").strip()
    if not account_no:
        raise ValueError("KIS_ACCOUNT_NO 값이 비어 있습니다. .env를 확인하세요.")

    cano, acnt_prdt_cd = _parse_account_info(account_no)

    url = f"{KIS_VIRTUAL_DOMAIN}/uapi/domestic-stock/v1/trading/inquire-balance"

    tr_id = "VTTC8434R"

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "User-Agent": "Mozilla/5.0",
        "authorization": f"Bearer {access_token}",
        "appkey": settings.KIS_APP_KEY,
        "appsecret": settings.KIS_APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
        "tr_cont": "",
    }

    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "01",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_NK100": "",
        "CTX_AREA_FK100": "",
    }

    response = _kis_get(url, headers, params)

    response.raise_for_status()

    return response.json()


def get_buying_power(stock_code: str, order_price: int) -> dict:
    """VTS limit-order capacity using cash and quantity that exclude margin credit."""
    require_virtual_environment()
    if not isinstance(stock_code, str) or len(stock_code) != 6 or not stock_code.isdigit():
        raise ValueError("종목코드는 숫자 6자리여야 합니다.")
    if isinstance(order_price, bool) or not isinstance(order_price, int) or order_price <= 0:
        raise ValueError("주문 단가는 양의 정수여야 합니다.")
    account_no = (settings.KIS_ACCOUNT_NO or "").strip()
    if not account_no:
        raise ValueError("KIS 계좌 설정이 필요합니다.")
    cano, product_code = _parse_account_info(account_no)
    url = f"{KIS_VIRTUAL_DOMAIN}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {"authorization": f"Bearer {get_access_token()}", "appkey": settings.KIS_APP_KEY,
               "appsecret": settings.KIS_APP_SECRET, "tr_id": "VTTC8908R", "custtype": "P"}
    params = {"CANO": cano, "ACNT_PRDT_CD": product_code, "PDNO": stock_code,
              "ORD_UNPR": str(order_price), "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "N",
              "OVRS_ICLD_YN": "N"}
    response = _kis_get(url, headers, params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("rt_cd") != "0" or not isinstance(payload.get("output"), dict):
        raise ValueError("KIS 매수 가능 금액 조회에 실패했습니다.")
    output = payload["output"]
    def amount(name):
        value = output.get(name)
        if value is None or not str(value).replace(",", "").isdigit():
            raise ValueError("KIS 매수 가능 금액 응답이 불완전합니다.")
        return int(str(value).replace(",", ""))
    return {"stock_code": stock_code, "order_price": order_price,
            "cash_available": min(amount("ord_psbl_cash"), amount("nrcvb_buy_amt")),
            "quantity_available": amount("nrcvb_buy_qty"), "credit_excluded": True}


def get_daily_item_chart_price(stock_code: str, start_date: str, end_date: str):
    require_virtual_environment()
    access_token = get_access_token()
    url = f"{KIS_VIRTUAL_DOMAIN}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": settings.KIS_APP_KEY,
        "appsecret": settings.KIS_APP_SECRET,
        "tr_id": "FHKST03010100",
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_DATE_1": start_date,
        "FID_INPUT_DATE_2": end_date,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    response = _kis_get(url, headers, params)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    result = get_current_price("005930")

    output = result.get("output", {})

    print("종목코드: 005930")
    print("현재가:", output.get("stck_prpr"))
