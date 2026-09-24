"""KIS VTS realtime order/fill notices (H0STCNI9) -> existing LAB order lifecycle.

Notices are the realtime primary signal; VTTC0081R inquiry/reconciliation stays authoritative.
Fill notices only ever move filled_quantity forward to max(local, sum of unique notices), capped
at the order quantity, so a late or repeated notice can never double-count a fill that polling
already applied. Nothing here submits, retries or cancels an order.
"""
import logging


logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "ERROR"}
SIDES = {"01": "SELL", "02": "BUY"}  # SELN_BYOV_CLS


def _int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _broker_id(value) -> str:
    return (value or "").strip().lstrip("0")


def classify(notice: dict) -> str | None:
    """Map official codes (CNTG_YN, RFUS_YN, RCTF_CLS, ACPT_YN) to a notice kind."""
    fill_flag, receipt = notice.get("fill_flag"), notice.get("receipt_class")
    if fill_flag == "2":
        return "FILL"
    if fill_flag != "1":
        return None
    if notice.get("rejected") == "1":
        return {"1": "MODIFY_REJECTED", "2": "CANCEL_REJECTED"}.get(receipt, "REJECTED")
    if notice.get("acceptance") == "3" or receipt == "2":  # IOC/FOK remainder, or approved cancel
        return "CANCELED"
    if receipt == "1":
        return "MODIFIED"
    return "ACCEPTED"


class OrderNotificationProcessor:
    def __init__(self, store):
        self.store = store
        self._fills: dict[str, dict[tuple, int]] = {}

    def _find(self, broker_order_id: str) -> dict | None:
        if not broker_order_id:
            return None
        return next((order for order in self.store.list()
                     if _broker_id(order.get("broker_order_id")) == broker_order_id), None)

    def apply(self, notice: dict) -> dict | None:
        kind = classify(notice)
        if not kind:
            return None
        # Cancel/modify notices carry their own order number; the original is OODER_NO.
        linked = notice.get("original_order_no") if notice.get("receipt_class") in {"1", "2"} else notice.get("order_no")
        order = self._find(_broker_id(linked))
        fill_quantity, fill_price = _int(notice.get("fill_quantity")), _int(notice.get("fill_price"))
        public = {
            "type": "order_notice", "kind": kind, "order_id": None,
            "stock_code": (notice.get("stock_code") or "").strip() or None,
            "side": SIDES.get(notice.get("side_code")),
            "fill_quantity": fill_quantity if kind == "FILL" else None,
            "fill_price": fill_price if kind == "FILL" else None,
            "time": (notice.get("fill_time") or "").strip() or None,
        }
        if not order:
            return public  # not a LAB order (or not acknowledged yet): M:ONE refreshes, reconciliation decides
        order = self._apply(order, kind, notice, fill_quantity, fill_price)
        public.update(order_id=order["id"], status=order["status"], quantity=order["quantity"],
                      filled_quantity=order["filled_quantity"], remaining_quantity=order["remaining_quantity"])
        return public

    def _apply(self, order: dict, kind: str, notice: dict, fill_quantity: int, fill_price: int) -> dict:
        details = {"source": "KIS_VTS_WS", "notice": kind}
        if order["status"] in TERMINAL_STATUSES:
            return order
        if kind == "FILL":
            return self._apply_fill(order, notice, fill_quantity, fill_price, details)
        if kind == "REJECTED" and not order["filled_quantity"]:
            return self.store.transition(order["id"], "REJECTED", details, last_error="KIS VTS 주문 거부 통보")
        if kind == "CANCELED":
            return self.store.transition(order["id"], "CANCELED", details, remaining_quantity=0)
        self.store.record_event(order["id"], f"NOTICE_{kind}", details)
        return self.store.get(order["id"])

    def _apply_fill(self, order: dict, notice: dict, quantity: int, price: int, details: dict) -> dict:
        if quantity <= 0 or price <= 0:
            return order
        key = (notice.get("order_no"), notice.get("fill_time"), quantity, price)
        fills = self._fills.setdefault(order["id"], {})
        fills[key] = quantity
        previous = order["filled_quantity"] or 0
        target = min(order["quantity"], max(previous, sum(fills.values())))
        delta = target - previous
        if delta <= 0:
            return order  # already applied (by polling or an earlier notice)
        average = ((order["average_fill_price"] or 0) * previous + price * delta) / target
        broker_id = order["broker_order_id"]
        if self.store.add_execution(order["id"], f"{broker_id}:{target}:WS", order["stock_code"],
                                    delta, price, notice.get("fill_time") or ""):
            self.store.apply_position_fill(order["stock_code"], order["side"], delta, price)
        status = "FILLED" if target >= order["quantity"] else "PARTIALLY_FILLED"
        return self.store.transition(order["id"], status, details, filled_quantity=target,
                                     remaining_quantity=order["quantity"] - target, average_fill_price=average)
