from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_ROOT = (PROJECT_ROOT / "data").resolve()
KIS_ALLOWED_HOSTS = {
    "openapivts.koreainvestment.com",
    "new.real.download.dws.co.kr",
}


def safe_data_path(filename: str) -> Path:
    root = DATA_ROOT
    candidate = (root / filename).resolve()
    if root not in candidate.parents:
        raise ValueError("file path must stay inside the project data directory")
    return candidate


def validate_outbound_url(url: str, allowed_hosts: set[str] = KIS_ALLOWED_HOSTS) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or host not in allowed_hosts:
        raise ValueError("outbound URL is not an allowed KIS HTTPS endpoint")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("outbound host could not be resolved") from exc
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("outbound host resolved to a non-public address")
    return url
