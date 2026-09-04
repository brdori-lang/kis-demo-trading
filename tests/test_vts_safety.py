import pytest
from pydantic import ValidationError

import kis_safety
from config import Settings


def test_settings_reject_non_virtual_environment():
    with pytest.raises(ValidationError):
        Settings(
            KIS_APP_KEY="key", KIS_APP_SECRET="secret",
            KIS_ACCOUNT_NO="12345678", KIS_ENV="real",
            _env_file=None,
        )


def test_runtime_environment_guard_rejects_manipulation(monkeypatch):
    monkeypatch.setattr(kis_safety.settings, "KIS_ENV", "real")

    with pytest.raises(RuntimeError, match="virtual"):
        kis_safety.require_virtual_environment()


def test_vts_hosts_are_fixed_and_real_hosts_are_rejected():
    assert kis_safety.KIS_VTS_REST_BASE_URL == "https://openapivts.koreainvestment.com:29443"
    assert kis_safety.KIS_VTS_WEBSOCKET_URL == "ws://ops.koreainvestment.com:31000"
    with pytest.raises(RuntimeError, match="VTS REST"):
        kis_safety.require_vts_rest_url("https://openapi.koreainvestment.com:9443/uapi/test")
    with pytest.raises(RuntimeError, match="VTS WebSocket"):
        kis_safety.require_vts_websocket_url("ws://ops.koreainvestment.com:21000")


def test_only_vts_order_tr_ids_are_allowed():
    for tr_id in ("VTTC0011U", "VTTC0012U", "VTTC0013U"):
        kis_safety.require_virtual_order_tr_id(tr_id)
    for tr_id in ("TTTC0011U", "TTTC0012U", "TTTC0013U"):
        with pytest.raises(RuntimeError, match="TR_ID"):
            kis_safety.require_virtual_order_tr_id(tr_id)
