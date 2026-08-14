import httpx
import json
import time
from pathlib import Path

from config import settings


KIS_VIRTUAL_DOMAIN = "https://openapivts.koreainvestment.com:29443"
TOKEN_CACHE_FILE = Path(".kis_token_cache.json")


def _load_token_cache() -> dict:
    """캐시 파일에서 토큰 정보 로드"""
    if not TOKEN_CACHE_FILE.exists():
        return {}
    
    try:
        with open(TOKEN_CACHE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_token_cache(token_data: dict) -> None:
    """토큰 정보를 캐시 파일에 저장"""
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(token_data, f, indent=2)


def _is_token_valid(cache_data: dict) -> bool:
    """캐시된 토큰의 유효성 확인"""
    if not cache_data or "expires_at" not in cache_data:
        return False
    
    current_time = time.time()
    expires_at = cache_data.get("expires_at", 0)
    
    # 만료 시간 30초 전부터 새로운 토큰 발급 (안전마진)
    return current_time < (expires_at - 30)


def _fetch_new_token() -> dict:
    """KIS API에서 새로운 토큰 발급"""
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

    # 토큰 캐시 데이터 생성
    expires_in = data.get("expires_in", 3600)  # 기본값 1시간
    issued_at = time.time()
    expires_at = issued_at + expires_in

    token_cache = {
        "access_token": access_token,
        "issued_at": issued_at,
        "expires_in": expires_in,
        "expires_at": expires_at,
    }

    return token_cache


def get_access_token() -> str:
    """
    Access Token 조회
    
    캐시된 토큰이 유효하면 재사용하고,
    만료되었으면 새로운 토큰을 발급합니다.
    """
    # 1. 캐시에서 토큰 로드
    cache_data = _load_token_cache()
    
    # 2. 캐시된 토큰이 유효한지 확인
    if _is_token_valid(cache_data):
        print("[Token] 캐시된 토큰 사용")
        return cache_data["access_token"]
    
    # 3. 캐시가 없거나 만료되었으면 새로운 토큰 발급
    print("[Token] 새로운 토큰 발급 중...")
    token_cache = _fetch_new_token()
    
    # 4. 새로운 토큰을 캐시에 저장
    _save_token_cache(token_cache)
    print(f"[Token] 토큰 발급 완료 (만료까지 {token_cache['expires_in']}초)")
    
    return token_cache["access_token"]


if __name__ == "__main__":
    token = get_access_token()
    print("KIS 모의투자 Access Token 발급 성공")
    print(f"Token: {token[:20]}...")