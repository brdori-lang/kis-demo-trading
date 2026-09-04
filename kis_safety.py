from urllib.parse import urlsplit

from config import settings


KIS_VTS_REST_BASE_URL = "https://openapivts.koreainvestment.com:29443"
KIS_VTS_WEBSOCKET_URL = "ws://ops.koreainvestment.com:31000"
KIS_VIRTUAL_ORDER_TR_IDS = frozenset({"VTTC0011U", "VTTC0012U", "VTTC0013U"})


def require_virtual_environment() -> None:
    if settings.KIS_ENV != "virtual":
        raise RuntimeError("KIS Trading LAB은 virtual 환경만 허용합니다.")


def require_vts_rest_url(url: str) -> None:
    require_virtual_environment()
    parsed = urlsplit(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base != KIS_VTS_REST_BASE_URL:
        raise RuntimeError("KIS VTS REST host만 허용합니다.")


def require_vts_websocket_url(url: str) -> None:
    require_virtual_environment()
    if url != KIS_VTS_WEBSOCKET_URL:
        raise RuntimeError("KIS VTS WebSocket host만 허용합니다.")


def require_virtual_order_tr_id(tr_id: str) -> None:
    if tr_id not in KIS_VIRTUAL_ORDER_TR_IDS:
        raise RuntimeError("허용되지 않은 KIS 주문 TR_ID입니다.")
