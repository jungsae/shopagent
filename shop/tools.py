import copy
from datetime import date, datetime, timezone
import inspect
import json
import time

from .shared import REFERENCE_DATE, leaves, patched


IRREVERSIBLE = {"cancel_order", "cancel_items", "request_refund", "request_exchange", "change_shipping_address", "schedule_return_pickup"}
SCOPES = {
    "product": ["search_products", "get_product", "check_stock", "get_reviews"],
    "order_delivery": ["list_orders", "get_order", "track_shipment", "change_shipping_address", "cancel_order", "cancel_items"],
    "refund_exchange": ["get_order", "check_refund_eligibility", "request_refund", "request_exchange", "schedule_return_pickup"],
    "payment": ["get_payment", "get_refund_status", "issue_receipt", "list_payment_methods"],
    "qna": ["search_faq", "get_policy"],
    "human": ["escalate_to_human"],
}
REASONS = ("단순변심", "상품불량")
RETURN_FEE = {"단순변심": 3000, "상품불량": 0}
EXCHANGE_FEE = {"단순변심": 6000, "상품불량": 0}
FREE_SHIPPING_MIN = 30000
SHIPPING_FEE = 3000
BEFORE_DISPATCH = ("결제완료", "상품준비중", "부분취소")
RETURN_WINDOW_DAYS = 7
MESSAGES = {
    "NOT_FOUND": "주문을 찾을 수 없습니다.",
    "FORBIDDEN": "로그인 고객의 주문이 아니어서 조회하거나 처리할 수 없습니다.",
    "INTERNAL_ERROR": "일시적인 시스템 오류입니다.",
    "INVALID_ARGUMENT": "입력값이 올바르지 않습니다.",
    "UNKNOWN_TOOL": "알 수 없는 도구입니다.",
    "INVALID_REASON": "반품/교환 사유는 단순변심 또는 상품불량이어야 합니다.",
    "RETURN_PERIOD_EXPIRED": "배송완료 후 7일이 지나 반품/교환할 수 없습니다.",
    "ALREADY_CONFIRMED": "구매확정된 주문은 반품/교환할 수 없습니다.",
    "NOT_DELIVERED": "배송완료 전 주문은 반품/교환할 수 없습니다.",
    "HYGIENE_OPENED": "개봉한 위생상품은 반품/교환할 수 없습니다.",
    "ALREADY_RETURNED": "이미 반품 또는 교환이 접수된 주문입니다.",
    "ALREADY_SHIPPED": "출고 후에는 취소나 배송지 변경을 할 수 없습니다. 수령 후 반품을 신청하세요.",
    "ALREADY_CANCELLED": "이미 취소된 주문입니다.",
    "ITEM_NOT_IN_ORDER": "주문에 없거나 이미 처리된 상품입니다.",
    "OUT_OF_STOCK": "교환할 옵션의 재고가 없습니다.",
    "OPTION_NOT_AVAILABLE": "같은 상품에 없는 옵션입니다.",
    "INVALID_DATE": "수거일은 기준일 이후의 YYYY-MM-DD 날짜여야 합니다.",
    "NOT_RETURNABLE_STATE": "반품접수 또는 교환접수 상태에서만 수거를 예약할 수 있습니다.",
}
ELIGIBILITY_CODES = ("RETURN_PERIOD_EXPIRED", "ALREADY_CONFIRMED", "NOT_DELIVERED", "HYGIENE_OPENED", "ALREADY_RETURNED")


def fail(code, message=None, **extra):
    return {"ok": False, "error": code, "message": message or MESSAGES.get(code, code), **extra}


def ok(**values):
    return {"ok": True, **values}


def item_list(item_ids):
    if item_ids is None or item_ids == "":
        return None
    if isinstance(item_ids, str):
        return [part.strip() for part in item_ids.split(",") if part.strip()]
    return [str(item) for item in item_ids]


class Shop:
    def __init__(self, base, customer_id, db_patch=None, faults=None, emit=None, reference_date=None):
        self.db = patched(base, db_patch or {})
        self.initial = copy.deepcopy(self.db)
        self.customer_id = customer_id
        self.reference_date = reference_date or self.db.get("reference_date") or REFERENCE_DATE
        self.faults = copy.deepcopy(faults or {})
        self.counts = {}
        self.trace = []
        self.emit = emit

    def record_call(self, record):
        record.setdefault("started", datetime.now(timezone.utc).isoformat())
        record.setdefault("finished", datetime.now(timezone.utc).isoformat())
        self.trace.append(record)
        if self.emit:
            self.emit("tool", **record)

    def invoke(self, name, arguments, turn):
        start = time.perf_counter()
        before = copy.deepcopy(leaves(self.db))
        self.counts[name] = self.counts.get(name, 0) + 1
        record = {"kind": "tool", "turn": turn, "name": name, "arguments": copy.deepcopy(arguments), "irreversible": name in IRREVERSIBLE, "started": datetime.now(timezone.utc).isoformat()}
        fault = self.faults.get(name)
        if name not in {tool for tools in SCOPES.values() for tool in tools}:
            output = fail("UNKNOWN_TOOL", "Unknown domain tool: " + name)
        elif fault and self.counts[name] == fault.get("call", 1):
            output = copy.deepcopy(fault["output"])
            record["fault_injected"] = True
        else:
            try:
                output = getattr(self, name)(**arguments)
            except TypeError as error:
                output = fail("INVALID_ARGUMENT", f"{type(error).__name__}: {error}")
            except Exception as error:
                output = fail("INTERNAL_ERROR", f"{type(error).__name__}: {error}")
        after = copy.deepcopy(leaves(self.db))
        record.update(output=copy.deepcopy(output), changes={key: {"before": before.get(key), "after": after.get(key)} for key in before.keys() | after.keys() if before.get(key) != after.get(key)})
        record["finished"] = datetime.now(timezone.utc).isoformat()
        record["total_ms"] = (time.perf_counter() - start) * 1000
        self.record_call(record)
        return output

    # 공통: 주문 존재와 소유자 확인
    def _owned(self, order_id):
        order = self.db["orders"].get(order_id)
        if order is None:
            return None, fail("NOT_FOUND", order_id=order_id)
        if order["customer_id"] != self.customer_id:
            return None, fail("FORBIDDEN", order_id=order_id)
        return order, None

    def _items(self, order):
        return order.get("items") or [{"item_id": order["order_id"] + "-1", "product_id": order["product_id"], "option": order["option"], "amount": order["amount"], "status": "주문"}]

    def _status_error(self, order):
        if order["status"] == "취소":
            return fail("ALREADY_CANCELLED", order_id=order["order_id"])
        if order["status"] not in BEFORE_DISPATCH:
            return fail("ALREADY_SHIPPED", order_id=order["order_id"])
        return None

    def search_products(self, query: str, category: str = ""):
        return [p for p in self.db["products"].values() if (query.casefold() in json.dumps(p, ensure_ascii=False).casefold()) and (not category or p["category"] == category)]

    def get_product(self, product_id: str):
        product = self.db["products"].get(product_id)
        return ok(**copy.deepcopy(product)) if product else fail("NOT_FOUND", "상품을 찾을 수 없습니다.", product_id=product_id)

    def check_stock(self, product_id: str, option: str):
        product = self.db["products"].get(product_id)
        if not product:
            return fail("NOT_FOUND", "상품을 찾을 수 없습니다.", product_id=product_id)
        return ok(product_id=product_id, option=option, stock=product["options"].get(option, 0))

    def get_reviews(self, product_id: str):
        product = self.db["products"].get(product_id)
        return copy.deepcopy(product["reviews"]) if product else fail("NOT_FOUND", "상품을 찾을 수 없습니다.", product_id=product_id)

    def list_orders(self):
        return [copy.deepcopy(o) for o in self.db["orders"].values() if o["customer_id"] == self.customer_id]

    def get_order(self, order_id: str):
        order, error = self._owned(order_id)
        return error or ok(**copy.deepcopy(order))

    def track_shipment(self, order_id: str):
        order, error = self._owned(order_id)
        return error or ok(**{key: order[key] for key in ("order_id", "status", "tracking_number", "delivered_at")})

    def change_shipping_address(self, order_id: str, address: str):
        order, error = self._owned(order_id)
        error = error or self._status_error(order)
        if error:
            return error
        order["address"] = address
        return ok(order_id=order_id, address=address, changed=True)

    def cancel_order(self, order_id: str):
        order, error = self._owned(order_id)
        error = error or self._status_error(order)
        if error:
            return error
        items = self._items(order)
        amount = sum(item["amount"] for item in items if item["status"] == "주문")
        for item in items:
            if item["status"] == "주문":
                item["status"] = "취소"
        if "items" in order:
            order["items"] = items
        order["status"] = "취소"
        payment = self.db["payments"][order_id]
        payment.update(refunded=payment.get("refunded", 0) + amount, pending_refund=0, refund_status="환불완료")
        return ok(order_id=order_id, status="취소", refunded=amount)

    def cancel_items(self, order_id: str, item_ids: list):
        order, error = self._owned(order_id)
        error = error or self._status_error(order)
        if error:
            return error
        wanted = item_list(item_ids)
        items = self._items(order)
        active = {item["item_id"]: item for item in items if item["status"] == "주문"}
        if not wanted or any(item_id not in active for item_id in wanted):
            return fail("ITEM_NOT_IN_ORDER", order_id=order_id, item_ids=wanted or [])
        amount = sum(active[item_id]["amount"] for item_id in set(wanted))
        for item_id in set(wanted):
            active[item_id]["status"] = "취소"
        order["items"] = items
        remaining = [item for item in items if item["status"] == "주문"]
        order["status"] = "부분취소" if remaining else "취소"
        payment = self.db["payments"][order_id]
        payment.update(refunded=payment.get("refunded", 0) + amount, pending_refund=0, refund_status="부분환불완료" if remaining else "환불완료")
        return ok(order_id=order_id, status=order["status"], item_ids=sorted(set(wanted)), refunded=amount)

    def check_refund_eligibility(self, order_id: str, reason: str = "단순변심", item_ids: list = None):
        order, error = self._owned(order_id)
        if error:
            return error
        if reason not in REASONS:
            return fail("INVALID_REASON", order_id=order_id)
        def ineligible(code, **extra):
            return fail(code, order_id=order_id, eligible=False, reference_date=self.reference_date, **extra)
        if order["status"] == "구매확정":
            return ineligible("ALREADY_CONFIRMED")
        if order["status"] in ("반품접수", "교환접수"):
            return ineligible("ALREADY_RETURNED")
        if order["status"] != "배송완료" or not order.get("delivered_at"):
            return ineligible("NOT_DELIVERED")
        days = (date.fromisoformat(self.reference_date) - date.fromisoformat(order["delivered_at"])).days
        if days < 0:
            return ineligible("NOT_DELIVERED")
        if days > RETURN_WINDOW_DAYS:
            return ineligible("RETURN_PERIOD_EXPIRED", days_since_delivery=days)
        items = self._items(order)
        active = [item for item in items if item["status"] == "주문"]
        wanted = item_list(item_ids)
        if wanted is None:
            selected = active
        else:
            known = {item["item_id"]: item for item in items}
            if any(item_id not in known for item_id in wanted):
                return fail("ITEM_NOT_IN_ORDER", order_id=order_id, eligible=False, item_ids=wanted)
            if any(known[item_id]["status"] != "주문" for item_id in wanted):
                return ineligible("ALREADY_RETURNED")
            selected = [item for item in active if item["item_id"] in set(wanted)]
        if not selected:
            return ineligible("ALREADY_RETURNED")
        if order.get("opened") and any(self.db["products"].get(item["product_id"], {}).get("hygiene") for item in selected):
            return ineligible("HYGIENE_OPENED")
        product_amount = sum(item["amount"] for item in selected)
        remaining = sum(item["amount"] for item in active) - product_amount
        original = sum(item["amount"] for item in items if item["status"] != "취소")
        fee = RETURN_FEE[reason]
        # RF-03: 단순변심 일부 반품으로 남은 금액이 무료배송 기준 미만이면 처음 면제된 배송비를 뺀다.
        clawback = SHIPPING_FEE if reason == "단순변심" and remaining > 0 and remaining < FREE_SHIPPING_MIN and order.get("shipping_fee", 0) == 0 and original >= FREE_SHIPPING_MIN else 0
        return ok(order_id=order_id, eligible=True, reason=reason, item_ids=[item["item_id"] for item in selected], product_amount=product_amount, fee=fee, shipping_fee_clawback=clawback, expected_refund=max(0, product_amount - fee - clawback), days_since_delivery=days, reference_date=self.reference_date)

    def request_refund(self, order_id: str, reason: str, item_ids: list = None):
        order, error = self._owned(order_id)
        if error:
            return error
        if reason not in REASONS:
            return fail("INVALID_REASON", order_id=order_id)
        check = self.check_refund_eligibility(order_id, reason, item_ids)
        if not check.get("ok"):
            return check
        items = self._items(order)
        for item in items:
            if item["item_id"] in check["item_ids"]:
                item["status"] = "반품접수"
        if "items" in order:
            order["items"] = items
        order.update(status="반품접수", return_reason=reason, refund_amount=check["expected_refund"])
        self.db["payments"][order_id].update(refund_status="반품검수대기", pending_refund=check["expected_refund"])
        return ok(order_id=order_id, status="반품접수", item_ids=check["item_ids"], fee=check["fee"], shipping_fee_clawback=check["shipping_fee_clawback"], refund_amount=check["expected_refund"])

    def request_exchange(self, order_id: str, option: str, reason: str = "단순변심"):
        order, error = self._owned(order_id)
        if error:
            return error
        check = self.check_refund_eligibility(order_id, reason)
        if not check.get("ok"):
            return check
        options = self.db["products"][order["product_id"]]["options"]
        if option not in options:
            return fail("OPTION_NOT_AVAILABLE", order_id=order_id, option=option)
        if options[option] < 1:
            return fail("OUT_OF_STOCK", order_id=order_id, option=option)
        order.update(status="교환접수", exchange_option=option)
        return ok(order_id=order_id, status="교환접수", option=option, fee=EXCHANGE_FEE[reason])

    def schedule_return_pickup(self, order_id: str, pickup_date: str):
        order, error = self._owned(order_id)
        if error:
            return error
        if order["status"] not in ("반품접수", "교환접수"):
            return fail("NOT_RETURNABLE_STATE", order_id=order_id)
        try:
            valid = date.fromisoformat(pickup_date) > date.fromisoformat(self.reference_date)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            return fail("INVALID_DATE", order_id=order_id, reference_date=self.reference_date)
        order["pickup_date"] = pickup_date
        return ok(order_id=order_id, pickup_date=pickup_date, scheduled=True)

    def get_payment(self, order_id: str):
        order, error = self._owned(order_id)
        return error or ok(**copy.deepcopy(self.db["payments"][order_id]))

    def get_refund_status(self, order_id: str):
        order, error = self._owned(order_id)
        if error:
            return error
        payment = self.db["payments"][order_id]
        return ok(**{key: payment[key] for key in ("order_id", "refunded", "pending_refund", "refund_status")})

    def issue_receipt(self, order_id: str):
        order, error = self._owned(order_id)
        if error:
            return error
        payment = self.db["payments"][order_id]
        payment["receipt_issued"] = True
        return ok(order_id=order_id, receipt_id="R-" + order_id, amount=payment["amount"])

    def list_payment_methods(self):
        return copy.deepcopy(self.db["payment_methods"])

    def search_faq(self, query: str):
        return [copy.deepcopy(p) for p in self.db["policies"].values() if query.casefold() in (p["title"] + p["text"]).casefold()]

    def get_policy(self, policy_id: str):
        policy = self.db["policies"].get(policy_id)
        return ok(**copy.deepcopy(policy)) if policy else fail("NOT_FOUND", "정책을 찾을 수 없습니다.", policy_id=policy_id)

    def escalate_to_human(self, reason: str, summary: str):
        if not reason.strip() or not summary.strip():
            return fail("INVALID_ARGUMENT", "reason and summary required")
        entry = {"customer_id": self.customer_id, "reason": reason, "summary": summary}
        self.db["escalations"].append(entry)
        return ok(escalated=True, queue="상담원", ticket_id="H-" + str(len(self.db["escalations"])))


def schemas(agent):
    result = []
    for name in SCOPES[agent]:
        signature = inspect.signature(getattr(Shop, name))
        params = {key: {"type": "string"} for key in signature.parameters if key != "self"}
        if "reason" in params and name != "escalate_to_human":
            params["reason"]["enum"] = list(REASONS)
        if "item_ids" in params:
            params["item_ids"] = {"type": "array", "items": {"type": "string"}}
        required = [key for key, p in signature.parameters.items() if key != "self" and p.default is inspect.Parameter.empty]
        description = name + (". 되돌릴 수 없는 행동. 직전 사용자 턴에서 대상과 행동의 명시적 동의가 필요합니다." if name in IRREVERSIBLE else ". Mock 쇼핑몰 도구.")
        result.append({"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": params, "required": required, "additionalProperties": False}}})
    return result
