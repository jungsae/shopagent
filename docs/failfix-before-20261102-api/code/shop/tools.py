import copy
from datetime import date, datetime, timezone
import inspect
import json
import time

from .shared import REFERENCE_DATE, leaves, patched


IRREVERSIBLE = {"cancel_order", "request_refund", "request_exchange", "change_shipping_address", "schedule_return_pickup"}
SCOPES = {
    "product": ["search_products", "get_product", "check_stock", "get_reviews"],
    "order_delivery": ["list_orders", "get_order", "track_shipment", "change_shipping_address", "cancel_order"],
    "refund_exchange": ["get_order", "check_refund_eligibility", "request_refund", "request_exchange", "schedule_return_pickup"],
    "payment": ["get_payment", "get_refund_status", "issue_receipt", "list_payment_methods"],
    "qna": ["search_faq", "get_policy"],
    "human": ["escalate_to_human"],
}


class Shop:
    def __init__(self, base, customer_id, db_patch=None, faults=None, emit=None):
        self.db = patched(base, db_patch or {})
        self.initial = copy.deepcopy(self.db)
        self.customer_id = customer_id
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
        try:
            if name not in {tool for tools in SCOPES.values() for tool in tools}:
                raise ValueError("Unknown domain tool: " + name)
            if fault and self.counts[name] == fault.get("call", 1):
                output = copy.deepcopy(fault["output"])
                record["fault_injected"] = True
            else:
                output = getattr(self, name)(**arguments)
        except (TypeError, ValueError, KeyError) as error:
            output = {"error": f"{type(error).__name__}: {error}"}
        after = copy.deepcopy(leaves(self.db))
        record.update(output=copy.deepcopy(output), changes={key: {"before": before.get(key), "after": after.get(key)} for key in before.keys() | after.keys() if before.get(key) != after.get(key)})
        record["finished"] = datetime.now(timezone.utc).isoformat()
        record["total_ms"] = (time.perf_counter() - start) * 1000
        self.record_call(record)
        return output

    def search_products(self, query: str, category: str = ""):
        return [p for p in self.db["products"].values() if (query.casefold() in json.dumps(p, ensure_ascii=False).casefold()) and (not category or p["category"] == category)]

    def get_product(self, product_id: str):
        return self.db["products"].get(product_id, {"error": "product not found"})

    def check_stock(self, product_id: str, option: str):
        product = self.db["products"][product_id]
        return {"product_id": product_id, "option": option, "stock": product["options"].get(option, 0)}

    def get_reviews(self, product_id: str):
        return self.db["products"][product_id]["reviews"]

    def list_orders(self):
        return [o for o in self.db["orders"].values() if o["customer_id"] == self.customer_id]

    def get_order(self, order_id: str):
        return self.db["orders"].get(order_id, {"error": "order not found"})

    def track_shipment(self, order_id: str):
        order = self.db["orders"][order_id]
        return {key: order[key] for key in ("order_id", "status", "tracking_number", "delivered_at")}

    def change_shipping_address(self, order_id: str, address: str):
        order = self.db["orders"][order_id]
        if order["status"] not in ("결제완료", "상품준비중"):
            return {"error": "address change unavailable after dispatch"}
        order["address"] = address
        return {"order_id": order_id, "address": address, "changed": True}

    def cancel_order(self, order_id: str):
        order = self.db["orders"][order_id]
        if order["status"] not in ("결제완료", "상품준비중"):
            return {"error": "cancellation unavailable after dispatch"}
        order["status"] = "취소"
        self.db["payments"][order_id].update(refunded=order["amount"], pending_refund=0, refund_status="환불완료")
        return {"order_id": order_id, "status": order["status"], "refunded": order["amount"]}

    def check_refund_eligibility(self, order_id: str):
        order = self.db["orders"][order_id]
        days = (date.fromisoformat(REFERENCE_DATE) - date.fromisoformat(order["delivered_at"])).days if order["delivered_at"] else None
        product = self.db["products"][order["product_id"]]
        eligible = order["status"] == "배송완료" and days is not None and 0 <= days <= 7 and not (product["hygiene"] and order["opened"])
        return {"order_id": order_id, "eligible": eligible, "days_since_delivery": days, "fee": 3000, "reference_date": REFERENCE_DATE, "policy_id": "P01" if eligible else "P03", "reason": "7일 이내 미확정 반품" if eligible else "배송상태, 반품기한 또는 개봉 위생상품 제한"}

    def request_refund(self, order_id: str, reason: str):
        if reason not in ("단순변심", "상품불량"):
            return {"error": "unknown return reason"}
        if not self.check_refund_eligibility(order_id)["eligible"]:
            return {"error": "refund ineligible"}
        order = self.db["orders"][order_id]
        fee = 0 if reason == "상품불량" else 3000
        order.update(status="반품접수", return_reason=reason, refund_amount=order["amount"] - fee)
        self.db["payments"][order_id].update(refund_status="반품검수대기", pending_refund=order["amount"] - fee)
        return {"order_id": order_id, "status": "반품접수", "fee": fee, "refund_amount": order["refund_amount"]}

    def request_exchange(self, order_id: str, option: str):
        if not self.check_refund_eligibility(order_id)["eligible"]:
            return {"error": "exchange ineligible"}
        order = self.db["orders"][order_id]
        if self.db["products"][order["product_id"]]["options"].get(option, 0) < 1:
            return {"error": "exchange option out of stock"}
        order.update(status="교환접수", exchange_option=option)
        return {"order_id": order_id, "status": "교환접수", "option": option}

    def schedule_return_pickup(self, order_id: str, pickup_date: str):
        order = self.db["orders"][order_id]
        if order["status"] not in ("반품접수", "교환접수") or date.fromisoformat(pickup_date) <= date.fromisoformat(REFERENCE_DATE):
            return {"error": "pickup requires accepted return and future date"}
        order["pickup_date"] = pickup_date
        return {"order_id": order_id, "pickup_date": pickup_date, "scheduled": True}

    def get_payment(self, order_id: str):
        return self.db["payments"][order_id]

    def get_refund_status(self, order_id: str):
        payment = self.db["payments"][order_id]
        return {key: payment[key] for key in ("order_id", "refunded", "pending_refund", "refund_status")}

    def issue_receipt(self, order_id: str):
        payment = self.db["payments"][order_id]
        payment["receipt_issued"] = True
        return {"order_id": order_id, "receipt_id": "R-" + order_id, "amount": payment["amount"]}

    def list_payment_methods(self):
        return self.db["payment_methods"]

    def search_faq(self, query: str):
        return [p for p in self.db["policies"].values() if query.casefold() in (p["title"] + p["text"]).casefold()]

    def get_policy(self, policy_id: str):
        return self.db["policies"][policy_id]

    def escalate_to_human(self, reason: str, summary: str):
        if not reason.strip() or not summary.strip():
            return {"error": "reason and summary required"}
        entry = {"customer_id": self.customer_id, "reason": reason, "summary": summary}
        self.db["escalations"].append(entry)
        return {"escalated": True, "queue": "상담원", "ticket_id": "H-" + str(len(self.db["escalations"]))}


def schemas(agent):
    result = []
    for name in SCOPES[agent]:
        signature = inspect.signature(getattr(Shop, name))
        params = {key: {"type": "string"} for key in signature.parameters if key != "self"}
        if name == "request_refund":
            params["reason"]["enum"] = ["단순변심", "상품불량"]
        required = [key for key, p in signature.parameters.items() if key != "self" and p.default is inspect.Parameter.empty]
        description = name + (". 되돌릴 수 없는 행동. 직전 사용자 턴에서 대상과 행동의 명시적 동의가 필요합니다." if name in IRREVERSIBLE else ". Mock 쇼핑몰 도구.")
        result.append({"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": params, "required": required, "additionalProperties": False}}})
    return result
