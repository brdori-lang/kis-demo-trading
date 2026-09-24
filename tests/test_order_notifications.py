"""H0STCNI9 realtime order/fill notices: decryption, parsing and lifecycle (fake frames, no KIS)."""
import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import kis_virtual_orders as orders
import realtime_quotes as quotes
from lab_repository import SQLiteLabRepository
from order_notifications import OrderNotificationProcessor, classify


KEY, IV = "abcdefghijklmnopabcdefghijklmnop", "0123456789abcdef"  # shape of the official example
SUBSCRIBE_SUCCESS = json.dumps({
    "header": {"tr_id": "H0STCNI9", "tr_key": "HTSUSER1", "encrypt": "N"},
    "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS", "output": {"iv": IV, "key": KEY}},
})


def notice_values(*, order_no="0000012345", original="", side="02", receipt="0", fill_qty="0", fill_price="0",
                  rejected="0", fill_flag="1", acceptance="1", order_qty="10", time="101530"):
    return ["HTSUSER1", "5012345601", order_no, original, side, receipt, "00", "0", "005930", fill_qty,
            fill_price, time, rejected, fill_flag, acceptance, "06010", order_qty, "홍길동", "", "1", "Y", "",
            "", "", "삼성전자", "70000"]


def encrypted_frame(values):
    padder = padding.PKCS7(128).padder()
    data = padder.update("^".join(values).encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(KEY.encode()), modes.CBC(IV.encode())).encryptor()
    return "1|H0STCNI9|001|" + base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode()


def test_notice_subscription_is_vts_only_and_keys_come_from_the_subscribe_response():
    message = json.loads(quotes.notice_subscription_message("approval", "HTSUSER1", True))
    assert message["body"]["input"] == {"tr_id": "H0STCNI9", "tr_key": "HTSUSER1"}
    assert quotes.REALTIME_ORDER_NOTICE_TR_ID == "H0STCNI9"  # the real-account H0STCNI0 is never used
    for bad in ("", "a^b", "x" * 17):
        with pytest.raises(ValueError):
            quotes.notice_subscription_message("approval", bad, True)
    assert quotes.subscription_cipher(SUBSCRIBE_SUCCESS) == ("H0STCNI9", KEY.encode(), IV.encode())
    assert quotes.subscription_cipher(json.dumps({"header": {"tr_id": "H0STCNT0"}, "body": {"rt_cd": "0"}})) is None
    status = quotes.parse_kis_message(SUBSCRIBE_SUCCESS)
    assert status["type"] == "status" and status["success"] and status["stock_code"] is None  # HTS ID not echoed


def test_encrypted_notice_is_decrypted_and_parsed_with_official_columns():
    ciphers = {"H0STCNI9": (KEY.encode(), IV.encode())}
    event = quotes.parse_kis_message(encrypted_frame(notice_values(fill_flag="2", fill_qty="4", fill_price="70100")), ciphers)
    assert event["type"] == "order_notice"
    assert (event["order_no"], event["stock_code"], event["fill_quantity"], event["fill_price"], event["fill_flag"]) == (
        "0000012345", "005930", "4", "70100", "2")
    assert quotes.parse_kis_message(encrypted_frame(notice_values()), None) is None  # no key yet: dropped, not guessed
    assert quotes.parse_kis_message("1|H0STCNI9|001|not-base64!!", ciphers) is None
    assert quotes.parse_kis_message("0|H0STCNI9|001|" + "^".join(notice_values()[:10]), ciphers) is None


def test_service_subscribes_notices_after_market_trs_and_decrypts_with_the_session_key():
    sent, received = [], []
    frames = [SUBSCRIBE_SUCCESS, encrypted_frame(notice_values(fill_flag="2", fill_qty="1", fill_price="70100"))]

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            sent.append(json.loads(message)["body"]["input"])

        async def recv(self):
            if frames:
                return frames.pop(0)
            await asyncio.Event().wait()

    async def scenario():
        service = quotes.RealtimeQuoteService(approval_provider=lambda: asyncio.sleep(0, result="approval"),
                                              connect_factory=lambda *a, **k: Socket(), notice_tr_key="HTSUSER1")
        await service.subscribe("005930")

        async def handler(event):
            received.append(event)

        await service.start(handler)
        for _ in range(100):
            if any(e["type"] == "order_notice" for e in received):
                break
            await asyncio.sleep(0.01)
        await service.stop()

    asyncio.run(scenario())
    assert sent == [{"tr_id": "H0STCNT0", "tr_key": "005930"}, {"tr_id": "H0STCNI9", "tr_key": "HTSUSER1"}]
    notice = next(e for e in received if e["type"] == "order_notice")
    assert notice["fill_quantity"] == "1"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(orders.settings, "KIS_ENV", "virtual")
    return orders.KISOrderStore(SQLiteLabRepository(tmp_path / "notices.db"))


def acknowledged(store, quantity=10):
    item = store.create({"idempotency_key": "notice-key", "stock_code": "005930", "side": "BUY",
                         "quantity": quantity, "price": 70000, "order_type": "LIMIT"})
    return store.transition(item["id"], "ACKNOWLEDGED", broker_order_id="0000012345", broker_org_no="06010")


def fill(qty, price="70100", time="101530"):
    return {"type": "order_notice", **dict(zip(quotes.REALTIME_ORDER_NOTICE_COLUMNS, notice_values(
        fill_flag="2", fill_qty=str(qty), fill_price=price, time=time)))}


def notice(**kwargs):
    return {"type": "order_notice", **dict(zip(quotes.REALTIME_ORDER_NOTICE_COLUMNS, notice_values(**kwargs)))}


def test_official_codes_map_to_lifecycle_kinds():
    assert classify(notice()) == "ACCEPTED"
    assert classify(notice(fill_flag="2")) == "FILL"
    assert classify(notice(rejected="1")) == "REJECTED"
    assert classify(notice(receipt="2")) == "CANCELED"
    assert classify(notice(receipt="2", rejected="1")) == "CANCEL_REJECTED"
    assert classify(notice(receipt="1")) == "MODIFIED"
    assert classify(notice(acceptance="3")) == "CANCELED"  # IOC/FOK remainder
    assert classify(notice(fill_flag="")) is None


def test_fill_notices_advance_lifecycle_once_and_never_double_count_polling(store):
    order = acknowledged(store)
    processor = OrderNotificationProcessor(store)
    accepted = processor.apply(notice())
    assert accepted["kind"] == "ACCEPTED" and accepted["status"] == "ACKNOWLEDGED"
    partial = processor.apply(fill(4))
    assert (partial["status"], partial["filled_quantity"], partial["remaining_quantity"]) == ("PARTIALLY_FILLED", 4, 6)
    assert processor.apply(fill(4))["filled_quantity"] == 4  # the same notice again changes nothing
    assert len(store.executions(order["id"])) == 1
    # VTTC0081R polling already moved the order to 7; a late notice for a fill it covered adds nothing.
    store.transition(order["id"], "PARTIALLY_FILLED", filled_quantity=7, remaining_quantity=3)
    assert processor.apply(fill(3, time="101531"))["filled_quantity"] == 7
    done = processor.apply(fill(3, time="101532"))
    assert (done["status"], done["filled_quantity"], done["remaining_quantity"]) == ("FILLED", 10, 0)
    assert processor.apply(fill(5, time="101533"))["filled_quantity"] == 10  # capped, terminal is final
    public = json.dumps(done, ensure_ascii=False)
    assert "5012345601" not in public and "HTSUSER1" not in public and "홍길동" not in public


def test_reject_and_cancel_notices_and_unknown_orders(store):
    processor = OrderNotificationProcessor(store)
    order = acknowledged(store)
    canceled = processor.apply(notice(order_no="0000099999", original="0000012345", receipt="2"))
    assert (canceled["order_id"], canceled["status"], canceled["remaining_quantity"]) == (order["id"], "CANCELED", 0)
    other = store.create({"idempotency_key": "reject-key", "stock_code": "005930", "side": "SELL",
                          "quantity": 1, "price": 70000, "order_type": "LIMIT"})
    store.transition(other["id"], "ACKNOWLEDGED", broker_order_id="0000077777", broker_org_no="06010")
    rejected = processor.apply(notice(order_no="0000077777", rejected="1", side="01"))
    assert (rejected["status"], rejected["side"]) == ("REJECTED", "SELL")
    unknown = processor.apply(fill(1) | {"order_no": "0000055555"})
    assert unknown["order_id"] is None and unknown["kind"] == "FILL" and "status" not in unknown
