import httpx

from config import settings


KIS_VIRTUAL_DOMAIN = "https://openapivts.koreainvestment.com:29443"


def get_access_token() -> str:
    url = f"{KIS_VIRTUAL_DOMAIN}/oauth2/tokenP"

    body = {
        "grant_type": "client_credentials",
        "appkey": settings.KIS_APP_KEY,
        "appsecret": settings.KIS_APP_SECRET,
    }

    response = httpx.post(
        url,
        json=body,
        timeout=10.0,
    )

    response.raise_for_status()

    data = response.json()

    access_token = data.get("access_token")

    if not access_token:
        raise RuntimeError(f"토큰 발급 실패: {data}")

    return access_token


if __name__ == "__main__":
    get_access_token()
    print("KIS 모의투자 Access Token 발급 성공")