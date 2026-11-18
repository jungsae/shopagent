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
        if order["status"] not in ("\uacb0\uc81c\uc644\ub8cc", "\uc0c1\ud488\uc900\ube44\uc911"):
            return {"error": "address change unavailable after dispatch"}
        order["address"] = address
        return {"order_id": order_id, "address": address, "changed": True}

    def cancel_order(self, order_id: str):
        order = self.db["orders"][order_id]
        if order["status"] not in ("\uacb0\uc81c\uc644\ub8cc", "\uc0c1\ud488\uc900\ube44\uc911"):
            return {"error": "cancellation unavailable after dispatch"}
        order["status"] = "\ucde8\uc18c"
        self.db["payments"][order_id].update(refunded=order["amount"], pending_refund=0, refund_status="\ud658\ubd88\uc644\ub8cc")
        return {"order_id": order_id, "status": order["status"], "refunded": order["amount"]}

    def check_refund_eligibility(self, order_id: str):
        order = self.db["orders"][order_id]
        days = (date.fromisoformat(REFERENCE_DATE) - date.fromisoformat(order["delivered_at"])).days if order["delivered_at"] else None
        product = self.db["products"][order["product_id"]]
        eligible = order["status"] == "\ubc30\uc1a1\uc644\ub8cc" and days is not None and 0 <= days <= 7 and not (product["hygiene"] and order["opened"])
        return {"order_id": order_id, "eligible": eligible, "days_since_delivery": days, "fee": 3000, "reference_date": REFERENCE_DATE, "policy_id": "P01" if eligible else "P03", "reason": "7\uc77c \uc774\ub0b4 \ubbf8\ud655\uc815 \ubc18\ud488" if eligible else "\ubc30\uc1a1\uc0c1\ud0dc, \ubc18\ud488\uae30\ud55c \ub610\ub294 \uac1c\ubd09 \uc704\uc0dd\uc0c1\ud488 \uc81c\ud55c"}

    def request_refund(self, order_id: str, reason: str):
        if reason not in ("\ub2e8\uc21c\ubcc0\uc2ec", "\uc0c1\ud488\ubd88\ub7c9"):
            return {"error": "unknown return reason"}
        if not self.check_refund_eligibility(order_id)["eligible"]:
            return {"error": "refund ineligible"}
        order = self.db["orders"][order_id]
        fee = 0 if reason == "\uc0c1\ud488\ubd88\ub7c9" else 3000
        order.update(status="\ubc18\ud488\uc811\uc218", return_reason=reason, refund_amount=order["amount"] - fee)
        self.db["payments"][order_id].update(refund_status="\ubc18\ud488\uac80\uc218\ub300\uae30", pending_refund=order["amount"] - fee)
        return {"order_id": order_id, "status": "\ubc18\ud488\uc811\uc218", "fee": fee, "refund_amount": order["refund_amount"]}

    def request_exchange(self, order_id: str, option: str):
        if not self.check_refund_eligibility(order_id)["eligible"]:
            return {"error": "exchange ineligible"}
        order = self.db["orders"][order_id]
        if self.db["products"][order["product_id"]]["options"].get(option, 0) < 1:
            return {"error": "exchange option out of stock"}
        order.update(status="\uad50\ud658\uc811\uc218", exchange_option=option)
        return {"order_id": order_id, "status": "\uad50\ud658\uc811\uc218", "option": option}

    def schedule_return_pickup(self, order_id: str, pickup_date: str):
        order = self.db["orders"][order_id]
        if order["status"] not in ("\ubc18\ud488\uc811\uc218", "\uad50\ud658\uc811\uc218") or date.fromisoformat(pickup_date) <= date.fromisoformat(REFERENCE_DATE):
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
        return {"escalated": True, "queue": "\uc0c1\ub2f4\uc6d0", "ticket_id": "H-" + str(len(self.db["escalations"]))}


def schemas(agent):
    result = []
    for name in SCOPES[agent]:
        signature = inspect.signature(getattr(Shop, name))
        params = {key: {"type": "string"} for key in signature.parameters if key != "self"}
        if name == "request_refund":
            params["reason"]["enum"] = ["\ub2e8\uc21c\ubcc0\uc2ec", "\uc0c1\ud488\ubd88\ub7c9"]
        required = [key for key, p in signature.parameters.items() if key != "self" and p.default is inspect.Parameter.empty]
        description = name + (". \ub418\ub3cc\ub9b4 \uc218 \uc5c6\ub294 \ud589\ub3d9. \uc9c1\uc804 \uc0ac\uc6a9\uc790 \ud134\uc5d0\uc11c \ub300\uc0c1\uacfc \ud589\ub3d9\uc758 \uba85\uc2dc\uc801 \ub3d9\uc758\uac00 \ud544\uc694\ud569\ub2c8\ub2e4." if name in IRREVERSIBLE else ". Mock \uc1fc\ud551\ubab0 \ub3c4\uad6c.")
        result.append({"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": params, "required": required, "additionalProperties": False}}})
    return result
