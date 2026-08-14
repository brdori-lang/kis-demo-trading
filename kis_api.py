import httpx

from auth import get_access_token
from config import settings


KIS_VIRTUAL_DOMAIN = "https://openapivts.koreainvestment.com:29443"


def get_current_price(stock_code: str):
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

    response = httpx.get(
        url,
        headers=headers,
        params=params,
        timeout=10.0,
    )

    response.raise_for_status()

    data = response.json()

    return data


if __name__ == "__main__":
    result = get_current_price("005930")

    output = result.get("output", {})

    print("종목코드: 005930")
    print("현재가:", output.get("stck_prpr"))