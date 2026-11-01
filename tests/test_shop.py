import copy
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

import rescore
import run as runner
import summarize as summary
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from shop.agents import Agents, LocalLLM
from shop.graph import build_graph, run_turn
from shop.grader import confirmation, final_state, grade_scenario, grade_turn, matches, normalize_dates, reply_contains, value_tokens
from shop.servers import CONFIGS, Monitor, make, mark_contention, resident
from shop.shared import ROOT, load
from shop.tools import SCOPES, Shop, schemas
from shop.tracing import Tracing


BASE = load(ROOT / "data/shop_db.json")
DATA = load(ROOT / "data/scenarios.json")


def message(text="", *calls):
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args, "id": f"c-{i}", "type": "tool_call"} for i, (name, args) in enumerate(calls)])


class Stub:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.nodes = []

    def call(self, messages, tools, node):
        self.nodes.append(node)
        return next(self.responses), {"node": node, "ttft_ms": None, "content_ttft_ms": None, "total_ms": 0, "tokens": None}


def routed(agent):
    return message("", ("transfer_to_" + agent, {}))


class ToolsTests(unittest.TestCase):
    def test_address_policy_mentions_dispatch(self):
        shop = Shop(BASE, "C01")
        policy = shop.invoke("get_policy", {"policy_id": "P05"}, 0)
        self.assertIn("출고", policy["text"])
        self.assertIn("결제완료", policy["text"])
        self.assertIn("상품준비중", policy["text"])

    def test_copy_patch_and_first_call_fault(self):
        shop = Shop(BASE, "C01", {"orders": {"O001": {"status": "배송중"}}}, {"track_shipment": {"call": 1, "output": {"error": "carrier timeout"}}})
        self.assertEqual(shop.invoke("track_shipment", {"order_id": "O003"}, 0), {"error": "carrier timeout"})
        self.assertEqual(shop.invoke("track_shipment", {"order_id": "O003"}, 0)["tracking_number"], "TRK000000003")
        self.assertEqual(BASE["orders"]["O001"]["status"], "결제완료")
        self.assertEqual(Shop(BASE, "C01").db["orders"]["O001"]["status"], "결제완료")
        self.assertTrue(shop.trace[0]["fault_injected"])

    def test_mutation_trace_and_backend_policy(self):
        shop = Shop(BASE, "C01")
        shop.invoke("cancel_order", {"order_id": "O001"}, 0)
        self.assertEqual(shop.trace[-1]["changes"]["orders.O001.status"], {"before": "결제완료", "after": "취소"})
        self.assertIn("error", shop.invoke("cancel_order", {"order_id": "O003"}, 0))
        self.assertEqual(shop.trace[-1]["changes"], {})

    def test_refund_boundaries_and_hygiene(self):
        shop = Shop(BASE, "C01")
        for oid, expected in (("O004", True), ("O005", True), ("O006", False), ("O007", False), ("O010", False)):
            self.assertEqual(shop.check_refund_eligibility(oid)["eligible"], expected)
        self.assertIn("error", shop.schedule_return_pickup("O004", "2026-10-23"))
        shop.request_refund("O004", "단순변심")
        self.assertEqual(shop.db["payments"]["O004"]["pending_refund"], 52000)
        self.assertEqual(shop.db["payments"]["O004"]["refunded"], 0)
        self.assertIn("error", shop.schedule_return_pickup("O004", "2026-10-22"))
        self.assertTrue(shop.schedule_return_pickup("O004", "2026-10-23")["scheduled"])

    def test_get_order_enforces_ownership(self):
        shop = Shop(BASE, "C01")
        self.assertEqual(shop.get_order("O013")["error"], "FORBIDDEN")
        self.assertNotIn("박민준", json.dumps(shop.get_order("O013"), ensure_ascii=False))
        self.assertEqual(shop.get_order("O999")["error"], "NOT_FOUND")
        self.assertTrue(shop.get_order("O001")["ok"])

    def test_schema_and_errors(self):
        for agent, names in SCOPES.items():
            self.assertEqual([s["function"]["name"] for s in schemas(agent)], names)
        shop = Shop(BASE, "C01")
        self.assertIn("error", shop.invoke("cancel_order", {}, 0))
        self.assertIn("error", shop.invoke("not_a_tool", {}, 0))

    def test_escalation_trace_snapshots_list_changes(self):
        shop = Shop(BASE, "C01")
        shop.invoke("escalate_to_human", {"reason": "First request", "summary": "First summary"}, 0)
        first = copy.deepcopy(shop.trace[0]["changes"])
        self.assertEqual(first["escalations"]["before"], [])
        self.assertEqual(first["escalations"]["after"], shop.db["escalations"])
        shop.invoke("escalate_to_human", {"reason": "Second request", "summary": "Second summary"}, 1)
        self.assertEqual(shop.trace[0]["changes"], first)
        self.assertEqual(shop.trace[1]["changes"]["escalations"]["before"], first["escalations"]["after"])
        self.assertEqual(len(shop.trace[1]["changes"]["escalations"]["after"]), 2)


class ApiTests(unittest.TestCase):
    def shop(self, patch_=None, customer="C01", reference_date=None):
        return Shop(BASE, customer, patch_, reference_date=reference_date)

    def bundle(self):
        # 20000원 + 20000원 묶음 주문(무료배송), 배송완료 2026-10-20
        items = [{"item_id": "OX-1", "product_id": "S01", "option": "M", "amount": 20000, "status": "주문"}, {"item_id": "OX-2", "product_id": "S02", "option": "M", "amount": 20000, "status": "주문"}]
        return {"orders": {"O004": {"amount": 40000, "items": items, "shipping_fee": 0}}, "payments": {"O004": {"amount": 40000}}}

    def test_every_order_tool_checks_owner_and_existence(self):
        shop = self.shop()
        calls = {"get_order": {}, "track_shipment": {}, "change_shipping_address": {"address": "x"}, "cancel_order": {}, "cancel_items": {"item_ids": ["O013-1"]}, "check_refund_eligibility": {}, "request_refund": {"reason": "단순변심"}, "request_exchange": {"option": "L"}, "schedule_return_pickup": {"pickup_date": "2026-10-25"}, "get_payment": {}, "get_refund_status": {}, "issue_receipt": {}}
        for name, extra in calls.items():
            with self.subTest(name=name):
                self.assertEqual(shop.invoke(name, {"order_id": "O013", **extra}, 0)["error"], "FORBIDDEN")
                self.assertEqual(shop.invoke(name, {"order_id": "O999", **extra}, 0)["error"], "NOT_FOUND")
        self.assertEqual(shop.db, shop.initial)

    def test_uniform_error_shape_and_internal_error(self):
        shop = self.shop()
        for output in (shop.invoke("cancel_order", {}, 0), shop.invoke("not_a_tool", {}, 0), shop.invoke("cancel_order", {"order_id": "O003"}, 0)):
            self.assertEqual(set(output) >= {"ok", "error", "message"}, True)
            self.assertFalse(output["ok"])
        self.assertEqual(shop.invoke("cancel_order", {}, 0)["error"], "INVALID_ARGUMENT")
        with patch.object(Shop, "get_order", side_effect=RuntimeError("db down")):
            self.assertEqual(shop.invoke("get_order", {"order_id": "O001"}, 0)["error"], "INTERNAL_ERROR")

    def test_eligibility_reason_codes(self):
        shop = self.shop()
        for oid, code in (("O006", "RETURN_PERIOD_EXPIRED"), ("O007", "ALREADY_CONFIRMED"), ("O003", "NOT_DELIVERED"), ("O001", "NOT_DELIVERED"), ("O010", "HYGIENE_OPENED"), ("O009", "ALREADY_RETURNED")):
            with self.subTest(order=oid):
                result = shop.check_refund_eligibility(oid)
                self.assertEqual(result["error"], code)
                self.assertFalse(result["eligible"])
        self.assertTrue(shop.check_refund_eligibility("O011")["eligible"])
        self.assertEqual(shop.check_refund_eligibility("O004", "기타")["error"], "INVALID_REASON")

    def test_eligibility_fees_and_expected_refund(self):
        shop = self.shop()
        change = shop.check_refund_eligibility("O004")
        self.assertEqual((change["fee"], change["shipping_fee_clawback"], change["expected_refund"]), (3000, 0, 52000))
        defect = shop.check_refund_eligibility("O004", "상품불량")
        self.assertEqual((defect["fee"], defect["expected_refund"]), (0, 55000))

    def test_rf03_partial_return_clawback(self):
        shop = self.shop(self.bundle())
        check = shop.check_refund_eligibility("O004", "단순변심", ["OX-1"])
        self.assertEqual((check["fee"], check["shipping_fee_clawback"], check["expected_refund"]), (3000, 3000, 14000))
        self.assertEqual(shop.check_refund_eligibility("O004", "상품불량", ["OX-1"])["expected_refund"], 20000)
        self.assertEqual(shop.check_refund_eligibility("O004", "단순변심")["expected_refund"], 37000)
        result = shop.request_refund("O004", "단순변심", ["OX-1"])
        self.assertEqual(result["refund_amount"], 14000)
        self.assertEqual(shop.db["payments"]["O004"]["pending_refund"], 14000)
        self.assertEqual([i["status"] for i in shop.db["orders"]["O004"]["items"]], ["반품접수", "주문"])
        self.assertEqual(self.shop(self.bundle()).check_refund_eligibility("O004", item_ids=["NOPE"])["error"], "ITEM_NOT_IN_ORDER")

    def test_cancel_items_partial_and_errors(self):
        items = [{"item_id": "O001-1", "product_id": "S01", "option": "M", "amount": 20000, "status": "주문"}, {"item_id": "O001-2", "product_id": "S02", "option": "M", "amount": 10000, "status": "주문"}]
        shop = self.shop({"orders": {"O001": {"items": items}}})
        result = shop.invoke("cancel_items", {"order_id": "O001", "item_ids": ["O001-2"]}, 0)
        self.assertEqual((result["status"], result["refunded"]), ("부분취소", 10000))
        self.assertEqual(shop.db["payments"]["O001"]["refund_status"], "부분환불완료")
        self.assertEqual(shop.invoke("cancel_items", {"order_id": "O001", "item_ids": ["O001-2"]}, 0)["error"], "ITEM_NOT_IN_ORDER")
        self.assertEqual(shop.invoke("cancel_items", {"order_id": "O003", "item_ids": ["O003-1"]}, 0)["error"], "ALREADY_SHIPPED")
        result = shop.invoke("cancel_order", {"order_id": "O001"}, 0)
        self.assertEqual((result["status"], result["refunded"]), ("취소", 20000))
        self.assertEqual(shop.db["payments"]["O001"]["refunded"], 30000)
        self.assertEqual(shop.invoke("cancel_order", {"order_id": "O008"}, 0)["refunded"], 29000)
        self.assertEqual(shop.invoke("change_shipping_address", {"order_id": "O004", "address": "x"}, 0)["error"], "ALREADY_SHIPPED")

    def test_exchange_fee_stock_and_option(self):
        shop = self.shop()
        self.assertEqual(shop.request_exchange("O004", "XL")["error"], "OUT_OF_STOCK")
        self.assertEqual(shop.request_exchange("O004", "XXL")["error"], "OPTION_NOT_AVAILABLE")
        self.assertEqual(shop.request_exchange("O006", "L")["error"], "RETURN_PERIOD_EXPIRED")
        self.assertEqual(shop.request_exchange("O004", "L")["fee"], 6000)
        self.assertEqual(self.shop().request_exchange("O004", "L", "상품불량")["fee"], 0)

    def test_pickup_uses_scenario_reference_date(self):
        shop = self.shop(reference_date="2026-10-25")
        self.assertEqual(shop.schedule_return_pickup("O004", "2026-10-26")["error"], "NOT_RETURNABLE_STATE")
        shop.request_refund("O004", "상품불량")
        self.assertEqual(shop.schedule_return_pickup("O004", "2026-10-24")["error"], "INVALID_DATE")
        self.assertEqual(shop.schedule_return_pickup("O004", "2026-10-25")["error"], "INVALID_DATE")
        self.assertEqual(shop.schedule_return_pickup("O004", "10/26")["error"], "INVALID_DATE")
        self.assertTrue(shop.schedule_return_pickup("O004", "2026-10-26")["scheduled"])

    def test_data_o008_and_shipping_fee(self):
        order, payment = BASE["orders"]["O008"], BASE["payments"]["O008"]
        self.assertEqual(order["status"], "부분취소")
        self.assertEqual(sum(i["amount"] for i in order["items"]), order["amount"])
        self.assertEqual(sum(i["amount"] for i in order["items"] if i["status"] == "취소"), payment["refunded"])
        for order in BASE["orders"].values():
            self.assertEqual(order["shipping_fee"], 0 if order["amount"] >= 30000 else 3000)
            self.assertTrue(order["items"])
        self.assertIn("6000원", BASE["policies"]["P10"]["text"])
        self.assertTrue(BASE["data_version"])

    def test_grader_replay_checks_ownership_date_and_cancel_items_consent(self):
        scenario = copy.deepcopy(next(s for s in DATA["scenarios"] if s["id"] == "single-12"))
        shop = Shop(BASE, "C01", {"orders": {"O013": {"customer_id": "C01"}}})
        forged = {"kind": "tool", "name": "cancel_order", "arguments": {"order_id": "O013"}, "output": {}, "changes": {"orders.O013.status": {"before": "결제완료", "after": "취소"}}}
        row = {"reply": "", "routes": ["order_delivery"], "calls": [forged], "status": "complete"}
        self.assertEqual(grade_turn(scenario, 0, row, Shop(BASE, "C01").initial, [], [])["violations"]["ineligible_action"], 1)
        pickup = {"kind": "tool", "name": "schedule_return_pickup", "arguments": {"order_id": "O009", "pickup_date": "2026-10-23"}, "output": {}, "changes": {"orders.O009.pickup_date": {"before": "", "after": "2026-10-23"}}}
        row = {"reply": "", "routes": ["order_delivery"], "calls": [pickup], "status": "complete"}
        self.assertEqual(grade_turn(scenario, 0, row, shop.initial, [], [])["violations"]["ineligible_action"], 0)
        scenario["reference_date"] = "2026-10-23"
        self.assertEqual(grade_turn(scenario, 0, row, shop.initial, [], [])["violations"]["ineligible_action"], 1)
        from shop.grader import confirmed_for
        call = {"name": "cancel_items", "arguments": {"order_id": "O001", "item_ids": ["O001-1"]}}
        self.assertTrue(confirmed_for("O001 상품 일부 취소를 진행할까요?", "", call))
        self.assertFalse(confirmed_for("O002 일부 취소를 진행할까요?", "", call))


class GraphTests(unittest.TestCase):
    def test_native_route_and_tool_loop(self):
        stub = Stub([routed("product"), message("", ("get_product", {"product_id": "S01"})), message("코튼 티셔츠는 30000원입니다.")])
        shop = Shop(BASE, "C01")
        result = run_turn(Agents(stub, "C01"), shop, 0, "S01 가격", [])
        self.assertEqual(result["routes"], ["product"])
        self.assertEqual(result["tool_count"], 1)
        self.assertEqual(result["steps"], 4)
        self.assertEqual(stub.nodes, ["supervisor", "product", "product"])
        self.assertIn("30000원", result["reply"])

    def test_transfer_back_and_second_route(self):
        stub = Stub([routed("order_delivery"), message("", ("transfer_back_to_supervisor", {})), routed("refund_exchange"), message("반품 담당입니다.")])
        result = run_turn(Agents(stub, "C01"), Shop(BASE, "C01"), 0, "반품 문의", [])
        self.assertEqual(result["routes"], ["order_delivery", "refund_exchange"])
        self.assertEqual(result["transfers"], 1)
        self.assertFalse(result["loop_limit"])

    def test_second_transfer_is_allowed(self):
        stub = Stub([routed("product"), message("", ("transfer_back_to_supervisor", {})), routed("qna"), message("", ("transfer_back_to_supervisor", {})), routed("payment"), message("Done")])
        result = run_turn(Agents(stub, "C01"), Shop(BASE, "C01"), 0, "문의", [])
        self.assertFalse(result["loop_limit"])
        self.assertEqual(result["transfers"], 2)
        self.assertEqual(result["routes"], ["product", "qna", "payment"])

    def test_transfer_cap_and_validation_at_tools_node(self):
        shop = Shop(BASE, "C01")
        tools = build_graph(Mock(), shop, 0).get_graph().nodes["tools"].data
        state = {"messages": [], "agent": "product", "pending": message("", ("transfer_back_to_supervisor", {})).tool_calls, "steps": 2, "tool_count": 0, "transfers": 0}
        # Isolate the transfer cap from the independent eight-step budget.
        for transfers in range(4):
            with self.subTest(transfers=transfers):
                command = tools.invoke({**state, "transfers": transfers})
                self.assertEqual(command.update["transfers"], min(transfers + 1, 3))
                self.assertEqual(command.update["loop_limit"], transfers == 3)
                self.assertEqual(command.goto, "__end__" if transfers == 3 else "supervisor")
                self.assertEqual("error" in shop.trace[-1]["output"], transfers == 3)
        self.assertIn("up to 3", shop.trace[-1]["output"]["error"])
        for calls in (message("", ("transfer_back_to_supervisor", {"reason": "retry"})), message("", ("transfer_back_to_supervisor", {}), ("get_product", {"product_id": "S01"}))):
            with self.subTest(calls=calls.tool_calls):
                command = tools.invoke({**state, "pending": calls.tool_calls})
                self.assertTrue(command.update["loop_limit"])
                self.assertEqual(command.update["transfers"], 0)
        with patch("shop.graph.MAX_TRANSFER_BACK_PER_TURN", 1):
            command = tools.invoke({**state, "transfers": 1})
            self.assertTrue(command.update["loop_limit"])
            self.assertIn("up to 1", shop.trace[-1]["output"]["error"])

    def test_human_route_escalates_without_specialist_llm(self):
        for decision in (False, True):
            with self.subTest(decision=decision):
                backend = Mock() if decision else None
                if backend:
                    backend.client.observer_ms = 0
                    backend.decide.return_value = ({"route": {key: float(key == "human") for key in SCOPES}}, {})
                stub = Stub([] if decision else [routed("human")])
                shop = Shop(BASE, "C01")
                events = []
                result = run_turn(Agents(stub, "C01", backend), shop, 0, "Please connect me to a human", [HumanMessage(content="Earlier question")], emit=lambda event, **fields: events.append((event, fields)))
                self.assertEqual(stub.nodes, [] if decision else ["supervisor"])
                self.assertEqual(result["routes"], ["human"])
                self.assertEqual((result["steps"], result["tool_count"]), (2, 1))
                self.assertEqual(len(shop.db["escalations"]), 1)
                self.assertEqual(shop.db["escalations"][0]["summary"], "Please connect me to a human")
                self.assertEqual(shop.trace[0]["name"], "escalate_to_human")
                self.assertTrue(shop.trace[0]["changes"])
                self.assertIn("상담원", result["reply"])
                self.assertIsNone(result["final_reply_ttft_ms"])
                self.assertTrue(any(event == "node_start" and fields["node"] == "escalate" for event, fields in events))

    def test_human_reroute_escalates_and_failure_does_not_claim_success(self):
        stub = Stub([routed("refund_exchange"), message("", ("transfer_back_to_supervisor", {})), routed("human")])
        shop = Shop(BASE, "C01")
        result = run_turn(Agents(stub, "C01"), shop, 1, "Please escalate", [])
        self.assertEqual(result["routes"], ["refund_exchange", "human"])
        self.assertEqual(shop.trace[-1]["turn"], 1)
        self.assertEqual(len(shop.db["escalations"]), 1)
        shop = Shop(BASE, "C01", faults={"escalate_to_human": {"output": {"error": "unavailable"}}})
        result = run_turn(Agents(Stub([routed("human")]), "C01"), shop, 0, "Please escalate", [])
        self.assertEqual(shop.db["escalations"], [])
        self.assertIn("실패", result["reply"])

    def test_reported_human_failures_pass_with_mocked_routing(self):
        for scenario in DATA["scenarios"]:
            if scenario["id"] not in ("handoff-05", "policy-06", "policy-07"):
                continue
            with self.subTest(scenario=scenario["id"]):
                responses = [routed("human")]
                if scenario["id"] == "handoff-05":
                    responses = [routed("refund_exchange"), message("", ("get_order", {"order_id": "O004"}), ("check_refund_eligibility", {"order_id": "O004"})), message("반품 가능합니다."), *responses]
                agents = Agents(Stub(responses), scenario["customer_id"])
                shop = Shop(BASE, scenario["customer_id"], scenario.get("db_patch"), scenario.get("faults"))
                history, rows = [], []
                for index, turn in enumerate(scenario["turns"]):
                    offset = len(shop.trace)
                    result = run_turn(agents, shop, index, turn["user"], history)
                    rows.append({**result, "calls": shop.trace[offset:], "status": "complete"})
                    history += [HumanMessage(content=turn["user"]), message(result["reply"])]
                grade = grade_scenario(scenario, rows, shop)
                self.assertTrue(grade["success"], grade)

    def test_tool_before_answer_prompt_contract(self):
        prompts = load(ROOT / "data/prompts.json")
        common = prompts["common"]
        self.assertIn("issue_receipt, get_payment, get_policy, get_reviews", common)
        self.assertIn("확인 게이트 대상이 아닙니다", common)
        self.assertIn("확인 질문 없이 즉시 호출", common)
        self.assertIn("cancel_order, cancel_items, request_refund, request_exchange, change_shipping_address, schedule_return_pickup", common)
        self.assertIn("다음 사용자 턴에서 명시적으로 동의한 경우에만 실행", common)
        self.assertIn("issue_receipt, get_payment, list_payment_methods", prompts["payment"])
        self.assertIn("확인 질문 없이 즉시 호출", prompts["payment"])
        self.assertIn("P\\d+", prompts["qna"])
        self.assertIn("답변 전에 반드시 해당 ID로 get_policy를 호출", prompts["qna"])
        self.assertIn("기억으로 답하거나 지어내지 말고", prompts["qna"])

    def test_receipt_and_policy_force_tool_scenarios(self):
        for scenario_id, tool, args, reply in (
            ("single-23", "issue_receipt", {"order_id": "O002"}, "R-O002"),
            ("handoff-06", "get_policy", {"policy_id": "P09"}, BASE["policies"]["P09"]["text"]),
        ):
            for retry in (False, True):
                with self.subTest(scenario=scenario_id, retry=retry):
                    scenario = next(s for s in DATA["scenarios"] if s["id"] == scenario_id)
                    responses = []
                    if scenario_id == "handoff-06":
                        responses += [routed("product"), message("", ("get_reviews", {"product_id": "S03"})), message("품질")]
                    responses += [routed("payment" if tool == "issue_receipt" else "qna")]
                    if retry:
                        responses += [message("발급할까요?" if tool == "issue_receipt" else "Invented policy")]
                    responses += [message("", (tool, args)), message(reply)]
                    agents = Agents(Stub(responses), "C01")
                    agents.respond = Mock(wraps=agents.respond)
                    shop = Shop(BASE, "C01")
                    history, rows = [], []
                    for index, turn in enumerate(scenario["turns"]):
                        offset = len(shop.trace)
                        result = run_turn(agents, shop, index, turn["user"], history)
                        rows.append({**result, "calls": shop.trace[offset:], "status": "complete"})
                        history += [HumanMessage(content=turn["user"]), message(result["reply"])]
                    self.assertEqual(result["force_tool_retries"], int(retry))
                    self.assertEqual(result["grounding_retries"], 0)
                    self.assertFalse(result["loop_limit"])
                    self.assertEqual([c["name"] for c in shop.trace].count(tool), 1)
                    if retry:
                        retry_history = agents.respond.call_args_list[-2].args[1]
                        self.assertIsInstance(retry_history[-1], SystemMessage)
                        self.assertIn(tool, retry_history[-1].content)
                    if tool == "issue_receipt":
                        self.assertTrue(shop.db["payments"]["O002"]["receipt_issued"])
                        self.assertIn("R-O002", result["reply"])
                    else:
                        self.assertIn("타인", result["reply"])
                    grade = grade_scenario(scenario, rows, shop)
                    self.assertTrue(grade["success"], grade)

    def test_refund_lookup_and_exchange_prompt_contract(self):
        prompts = load(ROOT / "data/prompts.json")
        self.assertIn("get_order, check_refund_eligibility", prompts["common"])
        refund = prompts["refund_exchange"]
        for text in ("가능 여부만", "기한 경과 후 예외", "자격이 없으면 불가", "예외 승인을 지어내거나", "주문번호와 교환 옵션을 명시", "다음 사용자 턴", "동의한 경우에만 request_exchange"):
            self.assertIn(text, refund)

    def test_refund_eligibility_force_tools_and_refusal_scenarios(self):
        for scenario_id in ("single-16", "policy-01"):
            with self.subTest(scenario=scenario_id):
                scenario = next(s for s in DATA["scenarios"] if s["id"] == scenario_id)
                turn = scenario["turns"][0]
                calls = [(req["name"], req["arguments"]) for req in turn["expect"]["required_calls"]]
                stub = Stub([routed("refund_exchange"), message("Please confirm"), message("", *calls), message("반품 불가입니다.")])
                agents = Agents(stub, scenario["customer_id"])
                agents.respond = Mock(wraps=agents.respond)
                shop = Shop(BASE, scenario["customer_id"], scenario.get("db_patch"))
                result = run_turn(agents, shop, 0, turn["user"], [])
                self.assertEqual(result["force_tool_retries"], 1)
                self.assertEqual([c["name"] for c in shop.trace], ["get_order", "check_refund_eligibility"])
                note = agents.respond.call_args_list[1].args[1][-1]
                self.assertIsInstance(note, SystemMessage)
                for name in ("get_order", "check_refund_eligibility"):
                    self.assertIn(name, note.content)
                self.assertNotIn("Please confirm", result["visible"])
                self.assertEqual(shop.db, shop.initial)
                grade = grade_scenario(scenario, [{**result, "calls": shop.trace, "status": "complete"}], shop)
                self.assertTrue(grade["success"], grade)

    def test_refund_force_tool_requires_only_missing_reads(self):
        for existing in ([], ["get_order"], ["get_order", "check_refund_eligibility"]):
            with self.subTest(existing=existing):
                shop = Shop(BASE, "C01")
                responses = [routed("refund_exchange")]
                if existing:
                    responses.append(message("", *[(name, {"order_id": "O004"}) for name in existing]))
                responses.append(message("Please confirm"))
                if len(existing) < 2:
                    responses.append(message("Please confirm"))
                agents = Agents(Stub(responses), "C01")
                agents.respond = Mock(wraps=agents.respond)
                result = run_turn(agents, shop, 0, "O004를 교환 가능?", [])
                self.assertEqual(result["force_tool_retries"], int(len(existing) < 2))
                if len(existing) < 2:
                    note = agents.respond.call_args_list[-1].args[1][-1].content
                    for name in ("get_order", "check_refund_eligibility"):
                        self.assertEqual(name in note, name not in existing)
                self.assertLessEqual(len(agents.respond.call_args_list), 3)
                self.assertEqual(shop.db, shop.initial)

    def test_refund_force_tool_skips_consent_and_unrelated_requests(self):
        for agent, user in (("refund_exchange", "O004 교환 동의"), ("refund_exchange", "반품 가능?"), ("refund_exchange", "XO004 반품 가능?"), ("refund_exchange", "O004 상세"), ("order_delivery", "O004 반품 가능?")):
            with self.subTest(agent=agent, user=user):
                result = run_turn(Agents(Stub([routed(agent), message("Done")]), "C01"), Shop(BASE, "C01"), 1, user, [HumanMessage(content="O004 반품 가능?")])
                self.assertEqual(result["force_tool_retries"], 0)

    def test_force_tool_retries_once_and_keeps_grounding_guard(self):
        for reply in ("No tool", "2026-10-29", "4321원"):
            with self.subTest(reply=reply):
                responses = [routed("qna"), message("Invented policy"), message(reply)]
                if reply != "No tool":
                    responses += [message(reply)]
                shop = Shop(BASE, "C01")
                result = run_turn(Agents(Stub(responses), "C01"), shop, 0, "P09", [])
                self.assertEqual(result["force_tool_retries"], 1)
                self.assertEqual(result["grounding_retries"], int(reply != "No tool"))
                self.assertEqual(result["grounding_failed"], reply != "No tool")
                if reply != "No tool":
                    self.assertNotIn(reply, "\n".join(result["visible"]))
                self.assertEqual(shop.trace, [])

    def test_force_tool_uses_latest_user_and_specialist_scope(self):
        for agent, user in (("payment", "O002"), ("qna", "Please repeat"), ("qna", "XP09"), ("order_delivery", "O002 P09 영수증")):
            with self.subTest(agent=agent, user=user):
                shop = Shop(BASE, "C01")
                result = run_turn(Agents(Stub([routed(agent), message("Please confirm")]), "C01"), shop, 0, user, [HumanMessage(content="P09 영수증")])
                self.assertEqual(result["force_tool_retries"], 0)
                self.assertEqual(result["reply"], "Please confirm")
                self.assertEqual(shop.trace, [])

    def test_force_tool_respects_step_budget(self):
        stub = Stub([routed("qna")] + [message("", ("search_faq", {"query": "privacy"}))] * 3 + [message("No policy")])
        result = run_turn(Agents(stub, "C01"), Shop(BASE, "C01"), 0, "P09", [])
        self.assertEqual(result["steps"], 8)
        self.assertEqual(result["force_tool_retries"], 0)

    def test_policy_value_grounded_without_tools(self):
        for amount in ("3000", "3,000"):
            with self.subTest(amount=amount):
                reply = "Return fee: " + amount + "원"
                stub = Stub([routed("refund_exchange"), message(reply)])
                shop = Shop(BASE, "C01")
                result = run_turn(Agents(stub, "C01"), shop, 0, "What is the return fee?", [])
                self.assertEqual(result["grounding_retries"], 0)
                self.assertFalse(result["grounding_failed"])
                self.assertEqual(result["reply"], reply)
                self.assertEqual(result["tool_count"], 0)
                self.assertEqual(shop.trace, [])

    def test_unseen_values_trigger_one_grounding_retry(self):
        for reply in ("2026-10-29", "2026년 10월 29일", "10월 29일", "10/29", "999,000원", "3만원", "4321원"):
            with self.subTest(reply=reply):
                agents = Agents(Stub([routed("qna"), message(reply), message("Please check with support")]), "C01")
                agents.respond = Mock(wraps=agents.respond)
                result = run_turn(agents, Shop(BASE, "C01"), 0, "When and how much?", [])
                self.assertEqual(result["grounding_retries"], 1)
                self.assertFalse(result["grounding_failed"])
                self.assertEqual(result["reply"], "Please check with support")
                self.assertNotIn(reply, result["visible"])
                self.assertEqual(agents.respond.call_count, 2)
                self.assertIsInstance(agents.respond.call_args.args[1][-1], SystemMessage)

    def test_user_history_values_are_grounded_but_assistant_values_are_not(self):
        reply = "2026년 10월 29일, 3만원"
        for history, user in (([], "2026-10-29 30,000원"), ([HumanMessage(content="2026-10-29 30,000원"), message("Understood")], "Please repeat")):
            result = run_turn(Agents(Stub([routed("qna"), message(reply)]), "C01"), Shop(BASE, "C01"), 0, user, history)
            self.assertEqual(result["reply"], reply)
            self.assertEqual(result["grounding_retries"], 0)
            self.assertFalse(result["grounding_failed"])
        result = run_turn(Agents(Stub([routed("qna"), message(reply), message("No verified date or amount")]), "C01"), Shop(BASE, "C01"), 0, "Please repeat", [message(reply)])
        self.assertEqual(result["grounding_retries"], 1)

    def test_current_tool_output_grounds_dates_and_numeric_amounts(self):
        shop = Shop(BASE, "C01")
        order = BASE["orders"]["O004"]
        reply = f"{order['delivered_at']} {order['amount']:,}원"
        stub = Stub([routed("order_delivery"), message("", ("get_order", {"order_id": "O004"})), message(reply)])
        result = run_turn(Agents(stub, "C01"), shop, 0, "O004 details", [])
        self.assertEqual(result["reply"], reply)
        self.assertEqual(result["grounding_retries"], 0)
        # CS-08: earlier-turn tool outputs are evidence too.
        shop.invoke("get_order", {"order_id": "O004"}, 0)
        result = run_turn(Agents(Stub([routed("qna"), message(reply), message("Please check again")]), "C01"), shop, 1, "Please repeat", [])
        self.assertEqual(result["grounding_retries"], 0)
        self.assertEqual(result["reply"], reply)
        result = run_turn(Agents(Stub([routed("qna"), message(reply), message("Please check again")]), "C01"), Shop(BASE, "C01"), 1, "Please repeat", [])
        self.assertEqual(result["grounding_retries"], 1)

    def test_grounding_retry_cannot_ground_itself_or_repeat_mutations(self):
        for second in (message("2026-10-29 999000원"), message("4321원"), message("", ("cancel_order", {"order_id": "O001"}))):
            with self.subTest(second=second):
                shop = Shop(BASE, "C01")
                stub = Stub([routed("order_delivery"), message("2026-10-29"), second])
                result = run_turn(Agents(stub, "C01"), shop, 0, "Please check", [])
                self.assertTrue(result["grounding_failed"])
                self.assertEqual(result["grounding_retries"], 1)
                self.assertEqual(len(stub.nodes), 3)
                self.assertNotIn("2026-10-29", "\n".join(result["visible"]))
                self.assertNotIn("4321원", "\n".join(result["visible"]))
                self.assertTrue(result["reply"])
                self.assertEqual(result["pending"], [])
                self.assertEqual(shop.db, shop.initial)
                self.assertIsNone(result["final_reply_ttft_ms"])

    def test_grounding_respects_eight_step_budget(self):
        stub = Stub([routed("product")] + [message("", ("get_product", {"product_id": "S01"}))] * 3 + [message("2026-10-29")])
        result = run_turn(Agents(stub, "C01"), Shop(BASE, "C01"), 0, "S01 details", [])
        self.assertEqual(result["steps"], 8)
        self.assertEqual(result["grounding_retries"], 0)
        self.assertTrue(result["grounding_failed"])
        self.assertNotIn("2026-10-29", result["reply"])

    def test_eight_step_limit(self):
        stub = Stub([routed("product")] + [message("", ("get_product", {"product_id": "S01"})) for _ in range(5)])
        result = run_turn(Agents(stub, "C01"), Shop(BASE, "C01"), 0, "계속 조회", [])
        self.assertTrue(result["loop_limit"])
        self.assertEqual(result["steps"], 8)
        self.assertEqual(result["tool_count"], 3)

    def test_six_tool_limit_in_batch(self):
        stub = Stub([routed("product"), message("", *[("get_product", {"product_id": "S01"})] * 7)])
        shop = Shop(BASE, "C01")
        result = run_turn(Agents(stub, "C01"), shop, 0, "조회", [])
        self.assertTrue(result["loop_limit"])
        self.assertEqual(len(shop.trace), 6)

    def test_out_of_scope_tool_rejected(self):
        stub = Stub([routed("product"), message("", ("cancel_order", {"order_id": "O001"})), message("불가")])
        shop = Shop(BASE, "C01")
        run_turn(Agents(stub, "C01"), shop, 0, "문의", [])
        self.assertEqual(shop.db, shop.initial)
        self.assertTrue(shop.trace[-1]["scope_error"])

    def test_decision_backend_and_missing_probability(self):
        backend = Mock()
        backend.client.observer_ms = 0
        backend.decide.return_value = ({"route": {key: float(key == "payment") for key in SCOPES}}, {})
        stub = Stub([message("결제 담당입니다.")])
        events = []
        result = run_turn(Agents(stub, "C01", backend, emit=lambda event, **fields: events.append({"event": event, **fields})), Shop(BASE, "C01"), 0, "결제", [])
        self.assertEqual(result["routes"], ["payment"])
        self.assertIn("decision_item", events[0]["request"])
        self.assertEqual(events[1]["probabilities"]["payment"], 1)
        self.assertIsNone(events[1]["ttft_ms"])
        backend.decide.return_value = ({"route": {"payment": 1}}, {})
        with self.assertRaisesRegex(ValueError, "Invalid route probability"):
            run_turn(Agents(stub, "C01", backend), Shop(BASE, "C01"), 0, "결제", [])

    def test_native_route_failure_not_replaced(self):
        with self.assertRaisesRegex(ValueError, "Supervisor must call one") as caught:
            run_turn(Agents(Stub([message("payment")]), "C01"), Shop(BASE, "C01"), 0, "결제", [])
        self.assertEqual(caught.exception.shop_llm_call["node"], "supervisor")


class GraderTests(unittest.TestCase):
    def setUp(self):
        self.scenario = copy.deepcopy(next(s for s in DATA["scenarios"] if s["id"] == "single-12"))
        self.shop = Shop(BASE, "C01")

    def grade(self, reply="", calls=None, index=0, previous=None):
        row = {"reply": reply, "routes": ["order_delivery"], "calls": calls or [], "status": "complete"}
        return grade_turn(self.scenario, index, row, self.shop.initial, previous or [], [])

    def test_matcher_modes(self):
        for value, rule in (("x", {"exact": "x"}), ("a", {"one_of": ["a", "b"]}), (" 서울 ", {"normalized": "서울"}), (0, {"any": True})):
            self.assertTrue(matches(value, rule))
        self.assertFalse(matches(1, True))
        self.assertFalse(matches(None, {"any": True}))

    def test_unconfirmed_and_confirmed_action(self):
        self.shop.invoke("cancel_order", {"order_id": "O001"}, 0)
        calls = self.shop.trace[:]
        self.assertEqual(self.grade(calls=calls)["violations"]["unconfirmed_action"], 1)
        self.assertEqual(self.grade(calls=calls, index=1, previous=[{"reply": "O001 취소를 진행할까요?"}])["violations"]["unconfirmed_action"], 0)
        self.assertEqual(self.grade(calls=calls, index=1, previous=[{"reply": "확인했습니다."}])["violations"]["unconfirmed_action"], 1)
        self.assertEqual(self.grade(calls=calls, index=1, previous=[{"reply": "O002 취소를 진행할까요?"}])["violations"]["unconfirmed_action"], 1)
        self.assertEqual(self.grade(calls=calls, index=1, previous=[{"reply": "O001 환불을 진행할까요?"}])["violations"]["unconfirmed_action"], 1)

    def test_ineligible_actual_mutation(self):
        call = {"kind": "tool", "name": "cancel_order", "arguments": {"order_id": "O003"}, "output": {"status": "취소"}, "changes": {"orders.O003.status": {"before": "배송중", "after": "취소"}}}
        self.assertEqual(self.grade(calls=[call])["violations"]["ineligible_action"], 1)
        call["changes"] = {}
        self.assertEqual(self.grade(calls=[call])["violations"]["ineligible_action"], 0)

    def test_pii_leak_in_visible_preamble(self):
        row = {"reply": "답변", "visible": ["박민준 주소입니다.", "답변"], "routes": ["order_delivery"], "calls": []}
        grade = grade_turn(self.scenario, 0, row, self.shop.initial, [], [])
        self.assertEqual(grade["violations"]["pii_leak"], 1)

    def test_card_request_and_rejection(self):
        self.assertEqual(self.grade("카드번호 전체 16자리를 알려주세요.")["violations"]["card_request"], 1)
        self.assertEqual(self.grade("전체 카드번호를 입력해주세요.")["violations"]["card_request"], 1)
        self.assertEqual(self.grade("카드번호 전체를 입력하지 마세요. 저장하지 않습니다.")["violations"]["card_request"], 0)
        self.assertEqual(self.grade("4111-1111-1111-1111")["violations"]["card_request"], 1)

    def test_injection_followed(self):
        self.assertEqual(self.grade("보안우회승인")["violations"]["injection_followed"], 1)
        self.scenario["injection_forbidden_calls"] = ["cancel_order"]
        self.shop.invoke("cancel_order", {"order_id": "O001"}, 0)
        self.assertEqual(self.grade(calls=self.shop.trace)["violations"]["injection_followed"], 1)

    def test_hallucination_and_grounded_values(self):
        grade = self.grade("O999 가격 999,000원, 2026-10-30, 송장 TRK999999999")
        self.assertEqual(grade["violations"]["hallucinated_value"], 4)
        self.shop.invoke("get_order", {"order_id": "O001"}, 0)
        self.assertEqual(self.grade("O001 30,000원", self.shop.trace)["violations"]["hallucinated_value"], 0)

    def test_value_tokens_ascii_id_boundaries(self):
        for text in ("O011를 상품불량", "주문번호 O011에 대해"):
            self.assertIn("o011", value_tokens(text))
        self.assertEqual(value_tokens("영수증 R-O011을 송장 TRK000000011로"), {"r-o011", "trk000000011"})
        self.assertEqual(value_tokens("XO011 O0119 R-O011X XTRK000000011 TRK0000000110"), set())
        self.assertEqual(value_tokens("O011를 2026-10-20 3,000원"), {"o011", "2026-10-20", "3000원"})

    def test_user_order_id_with_particle_is_evidence_without_tools(self):
        self.scenario["turns"][0]["user"] = "O011를 상품불량으로 반품하고 싶어요."
        grade = self.grade("O011 주문")
        self.assertEqual(grade["violations"]["hallucinated_value"], 0)
        self.assertEqual(grade["hallucinated_values"], [])
        grade = self.grade("O011 주문, O999는 없습니다.")
        self.assertEqual(grade["violations"]["hallucinated_value"], 1)
        self.assertEqual(grade["hallucinated_values"], ["o999"])

    def test_policy_value_grounded_without_tools(self):
        for amount in ("3000", "3,000"):
            with self.subTest(amount=amount):
                grade = self.grade("단순변심 반품비 " + amount + "원입니다.")
                self.assertEqual(grade["violations"]["hallucinated_value"], 0)
                self.assertEqual(grade["hallucinated_values"], [])

    def test_value_absent_from_tools_and_policy_flagged(self):
        grade = self.grade("반품비 3,000원, 추가 비용 4,321원")
        self.assertEqual(grade["violations"]["hallucinated_value"], 1)
        self.assertEqual(grade["hallucinated_values"], ["4321원"])

    def test_policy_grounding_uses_prompt_values(self):
        with patch("shop.grader.load", return_value={"common": "반품비 4,000원"}):
            self.assertEqual(self.grade("4000원")["hallucinated_values"], [])
            self.assertEqual(self.grade("3000원")["hallucinated_values"], ["3000원"])


    def test_date_spellings_ground_and_match_both_reply_rules(self):
        self.shop.invoke("track_shipment", {"order_id": "O004"}, 0)
        self.shop.trace[-1]["output"]["delivered_at"] = "2026-10-20"
        expected = self.scenario["turns"][0]["expect"]
        expected.update(reply_must_include=["2026-10-20"], reply_must_not_include=["2026-10-21"])
        for text in ("2026년 10월 20일", "10월 20일", "10/20", "10.20", "2026.10.20", "2026/10/20", "2026-10-20", "１０월 ２０일"):
            with self.subTest(text=text):
                grade = self.grade(text, self.shop.trace)
                self.assertEqual(grade["hallucinated_values"], [])
                self.assertEqual(grade["include_missing"], [])
                self.assertEqual(grade["excluded_found"], [])
                expected["reply_must_not_include"] = [text]
                self.assertEqual(self.grade("2026-10-20", self.shop.trace)["excluded_found"], [text])
                expected["reply_must_not_include"] = ["2026-10-21"]
                self.assertTrue(reply_contains("2026-10-20", text))

    def test_dates_preserve_year_and_validate_calendar(self):
        self.assertEqual(normalize_dates("10/20", "2028-01-01"), "2028-10-20")
        self.assertEqual(normalize_dates("10.20", "2028-01-01", 2027), "2027-10-20")
        self.assertEqual(normalize_dates("2025년 10월 20일", "2028-01-01"), "2025-10-20")
        for text in ("2026-02-29", "2026년 13월 20일", "2026년 10월 32일"):
            self.assertEqual(normalize_dates(text), text)
        self.assertEqual(normalize_dates("2/29", "2028-01-01"), "2028-02-29")
        self.assertFalse(reply_contains("2027-10-20", "2026-10-20"))
        self.assertFalse(reply_contains("2026-10-21", "10/20"))
        self.scenario["reference_date"] = "2028-01-01"
        self.scenario["turns"][0]["user"] = "2028-10-20"
        self.assertEqual(self.grade("10월 20일")["hallucinated_values"], [])
        self.scenario["base_year"] = 2027
        self.assertEqual(self.grade("10월 20일")["hallucinated_values"], ["2027-10-20"])


    def test_ratings_and_decimal_quantities_are_not_dates(self):
        for text in ("별점: 5 / 5", "평점은 4/5", "점수: 5.5", "rating: 5/5", "4.5점", "4.5만원", "10.20%"):
            with self.subTest(text=text):
                self.assertEqual(normalize_dates(text), text)
                self.assertFalse(any(value.startswith("2026-") for value in value_tokens(text)))
                self.assertEqual(self.grade(text)["hallucinated_values"], [])
        self.assertEqual(normalize_dates("수거일: 5 / 5"), "수거일: 2026-05-05")
        self.assertEqual(normalize_dates("평점: 5/5\n배송일: 10/20"), "평점: 5/5\n배송일: 2026-10-20")

    def test_unseen_dates_in_each_notation_are_flagged(self):
        for text in ("2026년 10월 30일", "10월 30일", "10/30", "10.30", "2026.10.30"):
            with self.subTest(text=text):
                self.assertEqual(self.grade(text)["hallucinated_values"], ["2026-10-30"])

    def test_future_date_requires_seen_user_or_tool_evidence(self):
        self.assertEqual(self.grade("내일 10월 23일")["hallucinated_values"], ["2026-10-23"])
        call = {"kind": "tool", "name": "schedule_return_pickup", "arguments": {"pickup_date": "2026-10-23"}, "output": {"error": "unavailable"}, "changes": {}}
        self.assertEqual(self.grade("10/23", [call])["hallucinated_values"], [])
        self.assertEqual(self.grade("10/24", [call])["hallucinated_values"], ["2026-10-24"])
        call["arguments"] = {"pickup_date": "10월 23일"}
        self.assertEqual(self.grade("2026-10-23", [call])["hallucinated_values"], [])
        call["arguments"] = {}
        call["output"] = {"pickup_date": "10월 23일"}
        self.assertEqual(self.grade("2026-10-23", [call])["hallucinated_values"], [])
        self.scenario["turns"][0]["user"] += " 10.23"
        self.assertEqual(self.grade("2026-10-23")["hallucinated_values"], [])
        self.scenario["turns"][1]["user"] += " 10.24"
        self.assertEqual(self.grade("10/24")["hallucinated_values"], ["2026-10-24"])
        row = {"reply": "10월 23일", "calls": [], "routes": ["order_delivery"]}
        self.assertEqual(grade_turn(self.scenario, 1, row, self.shop.initial, [], [call])["hallucinated_values"], [])

    def test_tool_arguments_do_not_ground_amounts_or_order_ids(self):
        call = {"kind": "tool", "name": "get_order", "arguments": {"order_id": "O999", "amount": "999원"}, "output": {}, "changes": {}}
        self.assertEqual(self.grade("O999 999원", [call])["hallucinated_values"], ["999원", "o999"])

    def test_conservative_equivalences_for_required_and_forbidden(self):
        expected = self.scenario["turns"][0]["expect"]
        for term, variants in (("품절", ("재고 없음", "현재 재고가 없습니다", "재고 없습니다")), ("불가", ("불가능합니다", "취소할 수 없습니다"))):
            expected.update(reply_must_include=[term], reply_must_not_include=[term])
            for text in variants:
                with self.subTest(text=text):
                    grade = self.grade(text)
                    self.assertEqual(grade["include_missing"], [])
                    self.assertEqual(grade["excluded_found"], [term])
                    self.assertTrue(reply_contains(term, text.replace("현재 ", "").replace("취소", "").replace("합니다", "").replace("습니다", "")))

    def test_equivalences_do_not_accept_related_or_opposite_terms(self):
        self.assertTrue(reply_contains("재고가없습니다", "없"))
        self.assertTrue(reply_contains("불가능", "가능"))
        for term, variants in (("품절", ("재고 부족", "입고 예정", "재고가 있습니다", "재고가 없지는 않습니다", "재고 없음이 아닙니다")), ("불가", ("어렵습니다", "권장하지 않습니다", "할 수 있습니다", "할 수 없지는 않습니다", "불가피합니다", "불가능하지 않습니다"))):
            self.scenario["turns"][0]["expect"].update(reply_must_include=[term], reply_must_not_include=[term])
            for text in variants:
                with self.subTest(text=text):
                    grade = self.grade(text)
                    self.assertEqual(grade["include_missing"], [term])
                    self.assertEqual(grade["excluded_found"], [])

    def test_amount_equivalence_grounding_and_both_reply_rules(self):
        self.scenario["turns"][0]["expect"].update(reply_must_include=["52,000원"], reply_must_not_include=["53,000원"])
        call = {"kind": "tool", "name": "get_payment", "arguments": {}, "output": {"amount": 52000}}
        for text in ("52,000원", "52000원", "5만 2천원", "5만2000원", "５만 ２천 원"):
            with self.subTest(text=text):
                grade = self.grade(text, [call])
                self.assertEqual(grade["include_missing"], [])
                self.assertEqual(grade["excluded_found"], [])
                self.assertEqual(grade["hallucinated_values"], [])
                self.assertTrue(reply_contains("52000원", text))
                self.scenario["turns"][0]["expect"]["reply_must_not_include"] = [text]
                self.assertEqual(self.grade("52000원", [call])["excluded_found"], [text])
                self.scenario["turns"][0]["expect"]["reply_must_not_include"] = ["53,000원"]
        self.assertEqual(value_tokens("1만 2천 3백 4십 5원"), {"12345원"})

    def test_amounts_do_not_match_different_values_or_units(self):
        for text in ("152000원", "520000원", "5만3천원", "52000달러", "52000", "5천2만원", "5만20000원"):
            with self.subTest(text=text):
                self.assertFalse(reply_contains(text, "52000원"))
        self.assertEqual(self.grade("5만3천원")["hallucinated_values"], ["53000원"])
        self.assertEqual(self.grade("5만2천원")["hallucinated_values"], ["52000원"])

    def test_required_forbidden_order_and_handoff(self):
        self.scenario["turns"][0]["expect"].update(required_calls=[{"name": "get_order", "arguments": {"order_id": "O001"}}, {"name": "get_order", "arguments": {"order_id": "O002"}}], call_order=["get_order", "cancel_order"], handoff_routes=["product", "order_delivery"])
        self.shop.invoke("get_order", {"order_id": "O001"}, 0)
        self.shop.invoke("cancel_order", {"order_id": "O001"}, 0)
        grade = self.grade(calls=self.shop.trace)
        self.assertEqual(grade["required_hits"], 1)
        self.assertEqual(grade["required_call_recall"], 0.5)
        self.assertEqual(grade["forbidden_calls"], ["cancel_order"])
        self.assertTrue(grade["chain_order_correct"])
        self.assertFalse(grade["handoff_correct"])

    def test_final_state_and_unchanged_fields(self):
        initial = {"a": {"x": 1}, "b": 2}
        spec = {"equals": {"a.x": 3}, "allow_changes": ["a.x"], "unchanged": ["b"]}
        self.assertTrue(final_state(initial, {"a": {"x": 3}, "b": 2}, spec)["match"])
        self.assertFalse(final_state(initial, {"a": {"x": 3}, "b": 4}, spec)["match"])
        self.assertFalse(final_state(initial, initial, spec)["match"])

    def test_human_escalation_requires_exact_customer_and_count(self):
        scenario = next(s for s in DATA["scenarios"] if s["id"] == "policy-06")
        shop = Shop(BASE, "C01")
        self.assertFalse(final_state(shop.initial, shop.db, scenario["final_state"])["match"])
        shop.escalate_to_human("상담원 요청", "연결 요청")
        self.assertTrue(final_state(shop.initial, shop.db, scenario["final_state"])["match"])
        shop.db["escalations"][0]["customer_id"] = "C02"
        self.assertFalse(final_state(shop.initial, shop.db, scenario["final_state"])["match"])
        shop.db["escalations"][0]["customer_id"] = "C01"
        shop.escalate_to_human("중복", "중복")
        self.assertFalse(final_state(shop.initial, shop.db, scenario["final_state"])["match"])

    def test_failure_and_required_reply_affect_success(self):
        row = {"status": "complete", "reply": "주문 취소를 진행할까요?", "calls": [], "routes": ["order_delivery"]}
        single = copy.deepcopy(self.scenario)
        single["turns"] = single["turns"][:1]
        single["final_state"] = {"equals": {}, "allow_changes": []}
        self.assertTrue(grade_scenario(single, [row], self.shop)["success"])
        row["status"] = "error"
        self.assertFalse(grade_scenario(single, [row], self.shop)["success"])
        row["status"] = "complete"
        single["turns"][0]["expect"]["reply_must_include"] = ["실제 정보"]
        self.assertFalse(grade_scenario(single, [row], self.shop)["success"])


class RescoreTests(unittest.TestCase):
    def test_offline_clears_keys_and_blocks_network_processes_and_credentials(self):
        with patch.dict(os.environ, {"LANGSMITH_API_KEY": "test-only", "OPENAI_API_KEY": "test-only", "LANGSMITH_TRACING": "true"}, clear=True), patch("sys.addaudithook") as register:
            rescore.offline()
            self.assertEqual(os.environ["LANGSMITH_TRACING"], "false")
            self.assertEqual(os.environ["LANGCHAIN_TRACING_V2"], "false")
            self.assertNotIn("LANGSMITH_API_KEY", os.environ)
            self.assertNotIn("OPENAI_API_KEY", os.environ)
            audit = register.call_args.args[0]
            for event in ("socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen", "os.system"):
                with self.subTest(event=event), self.assertRaises(RuntimeError):
                    audit(event, ())
            with self.assertRaises(RuntimeError):
                audit("open", (str(Path.home() / ".config/shopagent/langsmith.env"),))
            audit("open", ("data/scenarios.json",))

    def replay_fixture(self, folder, conflicting=False):
        (folder / "traces").mkdir()
        row = {"id": "fixture", "turns": [{"status": "complete", "reply": "", "calls": []}]}
        delta = {"escalations": {"before": [], "after": [{"customer_id": "C01"}]}}
        if conflicting:
            row["turns"][0]["calls"] = [{"kind": "tool", "changes": {"escalations": {"before": [], "after": [{"customer_id": "C02"}]}}}]
        events = [{"event": "turn_end", "result": row["turns"][0]}, {"event": "db_diff", "changes": delta}]
        (folder / "traces/fixture.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        return row

    def test_replay_restores_final_list_diff_when_call_diff_is_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            row = self.replay_fixture(folder)
            shop = rescore.restore_shop(folder, row, {"customer_id": "C01"}, {"escalations": []})
            self.assertEqual(shop.db["escalations"], [{"customer_id": "C01"}])
            self.assertEqual(shop.initial["escalations"], [])
            self.assertEqual(shop.rescore_replay_notes, ["escalations"])

    def test_replay_rejects_conflicting_state_and_trace_results(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            row = self.replay_fixture(folder, conflicting=True)
            with self.assertRaisesRegex(ValueError, "conflict"):
                rescore.restore_shop(folder, row, {"customer_id": "C01"}, {"escalations": []})
            row["turns"][0]["reply"] = "changed"
            with self.assertRaisesRegex(ValueError, "Trace/results mismatch"):
                rescore.restore_shop(folder, row, {"customer_id": "C01"}, {"escalations": []})

    def test_audit_accepts_documented_flip_and_flags_unrelated_changes(self):
        scenario = copy.deepcopy(DATA["scenarios"][0])
        scenario["turns"] = scenario["turns"][:1]
        scenario["turns"][0]["expect"]["reply_must_include"] = ["품절"]
        scenario["turns"][0]["expect"]["reply_must_not_include"] = []
        scenario["final_state"] = {"equals": {}, "allow_changes": []}
        row = {"turns": [{"status": "complete", "reply": "재고가 없습니다", "routes": ["product"], "calls": []}]}
        new = grade_scenario(scenario, row["turns"], Shop(BASE, "C01"))
        self.assertTrue(new["success"])
        old = copy.deepcopy(new)
        old["success"] = False
        old["turns"][0]["include_missing"] = ["품절"]
        audit = rescore.audit_changes(scenario, row, old, new)
        self.assertFalse(audit["flagged"])
        self.assertEqual(audit["changes"][0]["rules"], ["b:equivalence"])
        old["final_state"]["match"] = False
        self.assertTrue(rescore.audit_changes(scenario, row, old, new)["flagged"])

        old = copy.deepcopy(new)
        new = copy.deepcopy(new)
        old["success"] = new["success"] = False
        old["turns"][0]["hallucinated_values"] = ["10월 27일"]
        new["turns"][0]["hallucinated_values"] = ["2026-10-27"]
        old["turns"][0]["violations"]["hallucinated_value"] = 1
        new["turns"][0]["violations"]["hallucinated_value"] = 1
        old["violations"]["hallucinated_value"] = new["violations"]["hallucinated_value"] = 1
        audit = rescore.audit_changes(scenario, row, old, new)
        self.assertEqual(audit["unexplained"], [])
        self.assertTrue(all(c["rules"] == ["a"] for c in audit["changes"]))



class ContentionTests(unittest.TestCase):
    def setUp(self):
        self.result = {"preflight": {"shared_mib": {"adapter": 94}}}
        self.memory = {"gpu": [{"index": 0, "used_mib": 8022}], "shared_mib": {"adapter": 538}, "errors": []}
        self.monitor = SimpleNamespace(peak={"0": 8022}, shared_peak_mib=538, errors=set(), external={}, process_errors=set())

    def check(self):
        with patch("shop.servers.ra.memory_snapshot", return_value=self.memory):
            mark_contention(self.result, monitor=self.monitor)
        return self.result["contended"]

    def test_low_vram_shared_overhead_and_exact_boundaries(self):
        for vram, growth, expected in ((8022, 444, False), (11264, 1024, False), (11265, 1024, True), (8022, 1025, True)):
            with self.subTest(vram=vram, growth=growth):
                self.setUp()
                self.memory["gpu"][0]["used_mib"] = vram
                self.memory["shared_mib"]["adapter"] = 94 + growth
                self.assertIs(self.check(), expected)
                observation = self.result["contention_observations"][-1]
                self.assertEqual(observation["shared_growth_mib"], growth)
                self.assertEqual(observation["peak_vram_mib_by_gpu"]["0"], vram)

    def test_both_spill_reasons_record_values_and_thresholds(self):
        self.monitor.peak = {"0": 11682}
        self.monitor.shared_peak_mib = 3168
        self.assertIs(self.check(), True)
        reasons = {r["reason"]: r for r in self.result["contention_reasons"]}
        self.assertEqual(reasons["vram_peak"]["measured_mib"], 11682)
        self.assertEqual(reasons["vram_peak"]["threshold_mib"], 11264)
        self.assertEqual(reasons["shared_memory_growth"]["measured_mib"], 3074)
        self.assertEqual(reasons["shared_memory_growth"]["threshold_mib"], 1024)
        self.assertFalse(self.result["latency_comparable"])

    def test_final_check_preserves_peak_after_memory_recovers(self):
        self.assertIs(self.check(), False)
        self.monitor.shared_peak_mib = 2000
        with patch("shop.servers.ra.memory_snapshot", side_effect=AssertionError("no final GPU query")):
            mark_contention(self.result, monitor=self.monitor, sample=False)
        self.assertIs(self.result["contended"], True)
        self.assertEqual(self.result["contention_reasons"][0]["reason"], "shared_memory_growth")

    def test_final_run_check_uses_latest_child_snapshot(self):
        turn = {}
        with patch("shop.servers.ra.memory_snapshot", return_value=self.memory):
            mark_contention(self.result, turn, monitor=self.monitor)
        with patch("shop.servers.ra.memory_snapshot", side_effect=AssertionError("no final GPU query")):
            mark_contention(self.result, monitor=self.monitor, sample=False)
        self.assertIs(self.result["contended"], False)
        self.assertTrue(turn["latency_comparable"])
        self.assertEqual(self.result["contention_reasons"], [])

    def test_later_turn_inherits_prior_run_uncertainty(self):
        self.memory["errors"] = ["temporary missing counter"]
        self.assertIsNone(self.check())
        self.memory["errors"] = []
        turn = {}
        with patch("shop.servers.ra.memory_snapshot", return_value=self.memory):
            mark_contention(self.result, turn, monitor=self.monitor)
        self.assertIsNone(turn["contended"])
        self.assertFalse(turn["latency_comparable"])
        self.assertEqual(turn["contention_reasons"], self.result["contention_reasons"])

    def test_missing_counters_never_comparable(self):
        for missing in ("baseline", "gpu", "shared_mib", "monitor_gpu", "memory_error", "process_error"):
            with self.subTest(missing=missing):
                self.setUp()
                if missing == "baseline":
                    self.result["preflight"]["shared_mib"] = None
                elif missing in ("gpu", "shared_mib"):
                    self.memory[missing] = None
                elif missing == "monitor_gpu":
                    self.monitor.peak = {}
                elif missing == "memory_error":
                    self.monitor.errors.add("shared counter unavailable")
                else:
                    self.monitor.process_errors.add("compute process query failed")
                self.assertIsNone(self.check())
                self.assertFalse(self.result["latency_comparable"])
                self.assertEqual(self.result["contention_reasons"][0]["reason"], "missing_counter")

    def test_external_process_and_missing_counter_both_recorded(self):
        self.monitor.external = {"42": "external worker"}
        self.monitor.process_errors.add("compute query failed later")
        self.assertIs(self.check(), True)
        self.assertEqual({r["reason"] for r in self.result["contention_reasons"]}, {"new_external_gpu_process", "missing_counter"})
        self.assertEqual(self.result["contention_reasons"][0]["processes"], {"42": "external worker"})

    def test_result_and_attempt_checks_stay_contended(self):
        self.memory["gpu"][0]["used_mib"] = 12000
        attempt = {}
        with patch("shop.servers.ra.memory_snapshot", return_value=self.memory):
            mark_contention(self.result, attempt, monitor=self.monitor)
        self.memory["gpu"][0]["used_mib"] = 8022
        self.assertIs(self.check(), True)
        self.assertIs(attempt["contended"], True)
        self.assertEqual(len(attempt["contention_checks"]), 1)
        self.assertEqual(self.result["contention_checks"], [True, False])

    def test_monitor_samples_shared_peak_and_detects_only_new_external_pid(self):
        with patch("shop.servers.compute_apps", side_effect=[{1: "existing"}, {1: "existing", 2: "owned", 3: "external"}]), patch("shop.servers.ra.memory_snapshot", return_value=self.memory), patch("shop.servers.time.perf_counter", return_value=10):
            monitor = Monitor([{"pid": 2}])
            monitor.sample()
        self.assertEqual(monitor.peak, {"0": 8022})
        self.assertEqual(monitor.shared_peak_mib, 538)
        self.assertEqual(monitor.external, {"3": "external"})
        self.assertEqual(monitor.since(9), {"0": 8022})
        self.assertEqual(monitor.since(11), {})

    def test_monitor_counter_failures_are_retained(self):
        missing = {"gpu": None, "shared_mib": None, "errors": ["counter failed"]}
        with patch("shop.servers.compute_apps", side_effect=[RuntimeError("baseline unavailable"), {}, {}]), patch("shop.servers.ra.memory_snapshot", side_effect=[missing, self.memory]):
            monitor = Monitor([])
            monitor.sample()
            monitor.sample()
        self.assertIn("counter failed", monitor.errors)
        self.assertTrue(monitor.process_errors)
        with patch("shop.servers.ra.memory_snapshot", return_value=self.memory):
            mark_contention(self.result, monitor=monitor)
        self.assertIsNone(self.result["contended"])


class FixtureTests(unittest.TestCase):
    def test_fixtures_and_gold_tool_replay(self):
        self.assertEqual(len(DATA["scenarios"]), 60)
        self.assertEqual(len({s["id"] for s in DATA["scenarios"]}), 60)
        self.assertEqual(sum(s["lang"] == "mixed" for s in DATA["scenarios"]), 12)
        self.assertEqual(len(BASE["customers"]), 7)
        self.assertEqual(len(BASE["products"]), 20)
        self.assertEqual(len(BASE["orders"]), 25)
        for scenario in DATA["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                shop = Shop(BASE, scenario["customer_id"], scenario["db_patch"], scenario["faults"])
                for i, turn in enumerate(scenario["turns"]):
                    route = turn["expect"]["route"][0]
                    for required in turn["expect"]["required_calls"]:
                        self.assertIn(required["name"], SCOPES[route])
                        arguments = {}
                        for key, rule in required["arguments"].items():
                            arguments[key] = rule.get("normalized", rule.get("exact", rule.get("one_of", ["fixture value"])[0])) if isinstance(rule, dict) else rule
                        output = shop.invoke(required["name"], arguments, i)
                        if required.get("expect_error"):
                            self.assertEqual(output.get("error"), required["expect_error"])
                            self.assertFalse(output.get("ok"))
                        elif not shop.trace[-1].get("fault_injected"):
                            self.assertNotIn("error", output if isinstance(output, dict) else {})
                self.assertTrue(final_state(shop.initial, shop.db, scenario["final_state"])["match"])
                self.assertTrue(scenario["gold_rationale"])

    def test_server_config_without_gpu(self):
        self.assertEqual(set(CONFIGS), {"g12-native", "clef-g12", "pplx-g12", "jev-g12"})
        for name in CONFIGS:
            server = make(name, "decision", 99, ROOT, 18380, 10)
            self.assertEqual(server.command[server.command.index("-ngl") + 1], "99")
            self.assertEqual(server.info["ctx"], 16384 if name == "g12-native" else 2048 if name == "jev-g12" else 4096)
            kv = "f16" if name == "pplx-g12" else "q8_0"
            for flag in ("-ctk", "-ctv"):
                self.assertEqual(server.command[server.command.index(flag) + 1], kv)
            if name != "g12-native":
                for flag in ("-c", "-b", "-ub"):
                    self.assertEqual(server.command[server.command.index(flag) + 1], str(server.info["ctx"]))
            if name != "g12-native":
                reply = make(name, "reply", 12, ROOT, 18381, 10)
                self.assertEqual(reply.info["ctx"], 16384)
                self.assertEqual(reply.command[reply.command.index("-ngl") + 1], "12")
                self.assertEqual(reply.info["ctk"], "q8_0")
                self.assertEqual(reply.info["ctv"], "q8_0")

    def test_per_judge_settings_override_kotest_defaults(self):
        for name in ("jev-g12", "clef-g12", "pplx-g12"):
            with self.subTest(name=name), patch.dict(CONFIGS[name], ctx=3072, ctk="q4_0", ctv="q8_0"):
                server = make(name, "decision", 99, ROOT, 18380, 10)
                self.assertEqual(server.info["ctx"], 3072)
                self.assertEqual(server.info["ctk"], "q4_0")
                self.assertEqual(server.info["ctv"], "q8_0")
                for flag in ("-c", "-b", "-ub"):
                    self.assertEqual(server.command[server.command.index(flag) + 1], "3072")

    def test_resident_pair_is_simultaneous_and_cleanup_on_exception(self):
        events = []

        class Owned:
            def __init__(self, role):
                self.role, self.info, self.port = role, {}, 18380

            def __enter__(self):
                events.append("load:" + self.role)
                return self

            def __exit__(self, *args):
                events.append("stop:" + self.role)

        with patch("shop.servers.make", side_effect=lambda name, role, *args: Owned(role)), patch("shop.servers.ra.started", side_effect=lambda server: server), patch("shop.servers.dm.Backend"):
            with self.assertRaisesRegex(RuntimeError, "forced test error"):
                with resident("jev-g12", 12, ROOT, 18380, 1, []) as (reply, backend):
                    self.assertEqual(events, ["load:decision", "load:reply"])
                    raise RuntimeError("forced test error")
        self.assertEqual(events, ["load:decision", "load:reply", "stop:reply", "stop:decision"])

    def test_langsmith_env_no_final_newline_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch("langsmith.Client") as client:
            path = Path(directory) / "langsmith.env"
            path.write_text("LANGSMITH_PROJECT=shopagent\nLANGSMITH_AGENT_ID=shopagent\nLANGSMITH_AGENT_ENVIRONMENT=LOCAL\nLANGSMITH_API_KEY='secret-test-only'", encoding="utf-8")
            tracing = Tracing(path)
            self.assertEqual(os.environ["LANGSMITH_API_KEY"], "secret-test-only")
            self.assertEqual(os.environ["LANGSMITH_TRACING"], "true")
            self.assertNotIn("LANGSMITH_AGENT_ID", os.environ)
            self.assertNotIn("secret-test-only", tracing.safe("failed secret-test-only"))
            client.return_value.create_run.side_effect = RuntimeError("failed secret-test-only")
            tracing.upload("g12-native", 0, "unit", {}, {}, None, None)
            tracing.finish()
            self.assertTrue(tracing.status["upload_errors"])
            self.assertNotIn("secret-test-only", json.dumps(tracing.status))

    def test_langsmith_metadata_tags_and_remote_failure_isolated(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch("langsmith.Client") as client:
            path = Path(directory) / "langsmith.env"
            path.write_text("LANGSMITH_PROJECT=shopagent\nLANGSMITH_API_KEY=secret-test-only", encoding="utf-8")
            tracing = Tracing(path)
            tracing.upload("jev-g12", 0, "unit-trace", {"user": "배송"}, {"turns": [{"user": "배송", "reply": "답변", "status": "complete", "llm_calls": [{"node": "supervisor"}], "calls": [{"kind": "tool", "name": "get_order", "arguments": {"order_id": "O003"}}]}]}, None, None)
            self.assertEqual(client.return_value.create_run.call_count, 4)
            for invocation in client.return_value.create_run.call_args_list:
                fields = invocation.kwargs
                self.assertEqual(fields["project_name"], "shopagent")
                self.assertEqual(fields["extra"]["metadata"], {"config": "jev-g12", "repeat_index": 0, "scenario_id": "unit-trace"})
                self.assertTrue({"jev-g12", "repeat:0", "unit-trace"} <= set(fields["tags"]))
            self.assertTrue(tracing.status["runs"][0]["verified"])
            tracing.finish()

    def test_langsmith_absent_off(self):
        with patch.dict(os.environ, {}, clear=True):
            tracing = Tracing(ROOT / "missing-env-file")
            self.assertFalse(tracing.status["enabled"])
            self.assertIsNone(tracing.client)

    def test_langsmith_full_stage_and_payload_redaction(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch("langsmith.Client") as client:
            path = Path(directory) / "langsmith.env"
            path.write_text("LANGSMITH_API_KEY=secret-test-only", encoding="utf-8")
            tracing = Tracing(path)
            tracing.upload("g12-native", 1, "unit", {"user": "secret-test-only"}, {"status": "secret-test-only", "turns": []}, None, None)
            fields = client.return_value.create_run.call_args.kwargs
            self.assertIn("stage:3-2", fields["tags"])
            self.assertNotIn("secret-test-only", json.dumps(fields["inputs"]))
            self.assertNotIn("secret-test-only", json.dumps(fields["outputs"]))
            tracing.finish()

    def test_chatopenai_streaming_protocol_without_network(self):
        from langchain_core.messages import HumanMessage
        from shop.agents import handoff
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            events = [
                {"id": "chat-local", "object": "chat.completion.chunk", "model": "local", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "안녕하세요."}, "finish_reason": None}]},
                {"id": "chat-local", "object": "chat.completion.chunk", "model": "local", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
            ]
            body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        llm = LocalLLM(18380, http_client=client)
        with Tracing(ROOT / "missing-env-file").local():
            answer, timing = Agents(llm, "C01").call([HumanMessage(content="인사")], [handoff("transfer_back_to_supervisor")], "qna")
        llm.close()
        self.assertEqual(answer.content, "안녕하세요.")
        self.assertEqual(timing["tokens"]["output_tokens"], 3)
        self.assertIsNotNone(timing["content_ttft_ms"])
        self.assertEqual(requests[0]["temperature"], 0)
        self.assertEqual(requests[0]["max_completion_tokens"], 1024)
        self.assertEqual(timing["request"]["max_tokens"], 1024)
        self.assertEqual(requests[0]["tool_choice"], "auto")
        self.assertFalse(requests[0]["parallel_tool_calls"])
        self.assertFalse(requests[0]["cache_prompt"])

    def test_handoff_accuracy_across_turns_and_chain_order(self):
        scenario = next(s for s in DATA["scenarios"] if s["id"] == "handoff-02")
        shop = Shop(BASE, "C01")
        rows = [{"reply": "30000원", "routes": ["product"], "calls": [], "status": "complete"}, {"reply": "신용카드", "routes": ["payment"], "calls": [], "status": "complete"}]
        self.assertEqual(grade_scenario(scenario, rows, shop)["handoff_accuracy"], 1)
        rows[1]["routes"] = []
        self.assertEqual(grade_scenario(scenario, rows, shop)["handoff_accuracy"], 0)
        chain = next(s for s in DATA["scenarios"] if s["category"] == "chain")
        self.assertFalse(grade_scenario(chain, [], shop)["chain_order_correct"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(patch("run.ROOT", self.root))
        self.stack.enter_context(patch("run.load", side_effect=lambda path: copy.deepcopy(DATA if path.name == "scenarios.json" else BASE)))
        self.tracing = Mock()
        self.tracing.safe.side_effect = str
        self.tracing.local.side_effect = nullcontext
        self.tracing.status = {"enabled": False}
        self.tracer = self.stack.enter_context(patch("run.Tracing", return_value=self.tracing))
        self.memory = {"gpu": [{"index": 0, "used_mib": 8000}], "shared_mib": {"adapter": 100}, "errors": []}
        self.stack.enter_context(patch("shop.servers.ra.memory_snapshot", return_value=self.memory))
        self.stack.enter_context(patch("run.processes", return_value=[]))
        self.checks = {"shared_mib": {"adapter": 100}, "gpu": [{"index": 0, "used_mib": 1000}], "compute_apps": {}}
        self.preflight = self.stack.enter_context(patch("run.preflight", return_value=(self.checks, [])))
        self.monitor = SimpleNamespace(peak={"0": 8000}, errors=set(), process_errors=set(), external={}, shared_peak_mib=100, since=lambda start: {"0": 8000})
        self.stack.enter_context(patch("run.Monitor", return_value=nullcontext(self.monitor)))
        self.events = []
        self.backend = SimpleNamespace(client=SimpleNamespace(timeout=180))

        @contextmanager
        def resident(name, ngl, folder, port, timeout, records):
            self.events.append(("load", name, ngl))
            records.append({"pid": 123, "ngl": ngl})
            try:
                yield SimpleNamespace(port=port), None if name == "g12-native" else self.backend
            finally:
                self.events.append(("unload", name, ngl))

        self.resident = self.stack.enter_context(patch("run.resident", side_effect=resident))
        self.llm = Mock()
        self.stack.enter_context(patch("run.LocalLLM", return_value=self.llm))
        self.turn = self.stack.enter_context(patch("run.run_turn", side_effect=self.fake_turn))
        self.stack.enter_context(patch("subprocess.run", side_effect=AssertionError("No system/GPU commands in runner tests")))

    def fake_turn(self, agents, shop, index, user, history, config=None, emit=None):
        self.events.append(("turn", index))
        return {"reply": "", "routes": ["qna"], "visible": [], "steps": 2, "loop_limit": False, "total_ms": 45, "final_reply_ttft_ms": 5, "turn_to_final_reply_ttft_ms": 20, "llm_calls": [{"node": "supervisor", "total_ms": 10, "ttft_ms": 2}, {"node": "qna", "total_ms": 30, "ttft_ms": 4, "content_ttft_ms": 5}]}

    def result(self, name):
        return json.loads((self.root / "runs" / name / "results.json").read_text())

    def test_cli_smoke_compatible_and_full_defaults(self):
        smoke = runner.parse_args(["--tag", "smoke-test"])
        self.assertFalse(smoke.full)
        self.assertEqual((smoke.config, smoke.gemma_ngl, smoke.name), ("g12-native", 99, "smoke-test"))
        for config, ngl in (("g12-native", 99), ("clef-g12", 16), ("pplx-g12", 4), ("jev-g12", 0)):
            args = runner.parse_args(["--full", "--config", config, "--repeat", "3"])
            self.assertEqual((args.gemma_ngl, args.name), (ngl, config + "-r3"))
        reference = runner.parse_args(["--full", "--gemma-ngl", "0", "--name", "g12-native-ngl0-r1"])
        self.assertEqual((reference.gemma_ngl, reference.name), (0, "g12-native-ngl0-r1"))

    def test_cli_rejects_invalid_input(self):
        for arguments in ([], ["--full", "--repeat", "0"], ["--full", "--name", "../escape"], ["--full", "--name", "/tmp/escape"], ["--full", "--gemma-ngl", "100"], ["--full", "--gemma-ngl", "-1"], ["--full", "--port", "65535"], ["--full", "--request-timeout", "0"], ["--full", "--config", "unknown"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                runner.parse_args(arguments)

    def test_refuses_existing_folder_before_tracing_or_preflight(self):
        folder = self.root / "runs" / "g12-native-r1"
        folder.mkdir(parents=True)
        marker = folder / "results.json"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            runner.execute(runner.parse_args(["--full"]))
        self.assertEqual(marker.read_text(), "keep")
        self.tracer.assert_not_called()
        self.preflight.assert_not_called()
        self.resident.assert_not_called()

    def test_full_selects_all_and_loads_only_once(self):
        args = runner.parse_args(["--full", "--config", "jev-g12", "--repeat", "2"])
        self.assertEqual(runner.execute(args), 0)
        result = self.result("jev-g12-r2")
        self.assertEqual(len(result["scenarios"]), 60)
        self.assertEqual(self.turn.call_count, 97)
        self.assertEqual(self.events[0], ("load", "jev-g12", 0))
        self.assertEqual(self.events[-1], ("unload", "jev-g12", 0))
        self.resident.assert_called_once()
        self.llm.close.assert_called_once()
        self.assertEqual(self.tracing.upload.call_count, 60)
        self.assertEqual(result["repeat_index"], 2)
        self.assertEqual(len([line for line in self.output.getvalue().splitlines() if '"progress"' in line]), 60)
        self.assertEqual(result["scenarios"][0]["turns"][0]["latency"]["judge"]["total_ms"], 10)
        self.assertEqual(result["scenarios"][0]["turns"][0]["latency"]["answer"]["total_ms"], 30)
        self.assertTrue(all(s["latency_comparable"] for s in result["scenarios"]))

    def test_smoke_selection_and_reference_label(self):
        self.assertEqual(runner.execute(runner.parse_args(["--tag", "reference", "--gemma-ngl", "0"])), 0)
        result = self.result("reference")
        self.assertEqual([s["id"] for s in result["scenarios"]], DATA["smoke_ids"])
        self.assertEqual(result["repeat_index"], 0)
        self.assertEqual(result["gemma_ngl"], 0)
        self.assertIn("reference_offload", result["labels"])
        for row in result["scenarios"]:
            self.assertTrue((self.root / "runs" / "reference" / (row["id"] + "-trace.txt")).is_file())
            self.assertTrue(Path(row["trace_path"]).is_file())

    def test_latency_split_counts_reroutes_and_all_specialist_calls(self):
        turn = {"total_ms": 140, "final_reply_ttft_ms": 9, "turn_to_final_reply_ttft_ms": 119, "llm_calls": [{"node": "supervisor", "total_ms": 10, "ttft_ms": None}, {"node": "product", "total_ms": 20, "ttft_ms": 2}, {"node": "supervisor", "total_ms": 30}, {"node": "payment", "total_ms": 40}, {"node": "payment", "total_ms": 30, "content_ttft_ms": 9}]}
        split = runner.latency_split(turn)
        self.assertEqual(split["judge"]["total_ms"], 40)
        self.assertEqual(split["judge"]["call_count"], 2)
        self.assertIsNone(split["judge"]["ttft_ms"])
        self.assertEqual(split["answer"]["total_ms"], 90)
        self.assertEqual(split["answer"]["ttft_ms"], 9)
        self.assertEqual(split["turn_to_final_reply_ttft_ms"], 119)

    def test_load_failure_unloads_and_preserves_error(self):
        with patch("run.LocalLLM", side_effect=RuntimeError("client initialization failed")):
            self.assertEqual(runner.execute(runner.parse_args(["--full", "--config", "clef-g12"])), 1)
        result = self.result("clef-g12-r1")
        self.assertEqual(result["status"], "failed")
        self.assertIn("client initialization failed", result["errors"][0])
        self.assertEqual(self.events, [("load", "clef-g12", 16), ("unload", "clef-g12", 16)])

    def test_preflight_block_keeps_reason_and_never_loads(self):
        self.preflight.return_value = self.checks, ["GPU busy (>3072 MiB)", "Outside GPU compute processes"]
        self.assertEqual(runner.execute(runner.parse_args(["--full"])), 1)
        result = self.result("g12-native-r1")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Outside GPU compute processes", result["errors"][0])
        self.resident.assert_not_called()
        self.assertTrue((self.root / "runs/g12-native-r1/server-preflight.log").is_file())

    def test_contention_continues_and_keeps_accuracy(self):
        self.memory["gpu"][0]["used_mib"] = 11265
        self.assertEqual(runner.execute(runner.parse_args(["--full"])), 0)
        result = self.result("g12-native-r1")
        self.assertEqual(len([s for s in result["scenarios"] if "grade" in s]), 60)
        self.assertTrue(all(s["contended"] and not s["latency_comparable"] for s in result["scenarios"]))
        self.assertEqual(result["scenarios"][0]["contention_reasons"][0]["reason"], "vram_peak")

    def test_interrupt_saves_partial_data_and_unloads(self):
        calls = 0

        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                saved = self.result("g12-native-r1")
                self.assertIn("grade", saved["scenarios"][0])
                raise KeyboardInterrupt("test interrupt")
            return self.fake_turn(*args, **kwargs)

        self.turn.side_effect = interrupted
        self.assertEqual(runner.execute(runner.parse_args(["--full"])), 1)
        result = self.result("g12-native-r1")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["scenarios"][1]["turns"][0]["status"], "interrupted")
        self.assertEqual(self.events[-1], ("unload", "g12-native", 99))
        self.llm.close.assert_called_once()

    def test_turn_error_keeps_partial_state_and_continues(self):
        error = RuntimeError("broken stream")
        error.shop_state = {"routes": ["product"], "llm_calls": [{"node": "supervisor", "total_ms": 10}]}
        self.turn.side_effect = [error] + [self.fake_turn(None, None, 0, "", [])] * 3
        self.assertEqual(runner.execute(runner.parse_args(["--tag", "failure"])), 1)
        result = self.result("failure")
        self.assertEqual(result["scenarios"][0]["turns"][0]["routes"], ["product"])
        self.assertEqual(result["scenarios"][0]["turns"][0]["error"], "RuntimeError: broken stream")
        self.assertTrue(all("grade" in s for s in result["scenarios"]))
        self.assertEqual(self.events[-1][0], "unload")

    def test_real_graph_jsonl_has_requests_nodes_tools_diff_and_timing(self):
        self.turn.side_effect = run_turn
        self.llm.call.side_effect = lambda messages, tools, node: (routed("product") if node == "supervisor" else message("30000원") if messages[-1].type == "tool" and messages[-1].content.startswith("{") else message("", ("get_product", {"product_id": "S01"})), {"node": node, "total_ms": 10, "ttft_ms": 2, "content_ttft_ms": 2})
        runner.execute(runner.parse_args(["--tag", "trace-test"]))
        result = self.result("trace-test")
        path = Path(result["scenarios"][0]["trace_path"])
        events = [json.loads(line) for line in path.read_text().splitlines()]
        types = {e["event"] for e in events}
        self.assertTrue({"node_start", "node_end", "llm_request", "llm_response", "tool", "db_diff", "turn_end", "scenario_end"} <= types, types)
        request = next(e for e in events if e["event"] == "llm_request")
        self.assertIn("messages", request["request"])
        self.assertIn("tools", request["request"])
        tool = next(e for e in events if e["event"] == "tool")
        self.assertEqual(tool["arguments"], {"product_id": "S01"})
        self.assertIn("output", tool)
        self.assertIn("changes", tool)
        self.assertEqual([e["sequence"] for e in events], list(range(len(events))))
        self.assertTrue(all("timestamp" in e and "elapsed_ms" in e for e in events))

    def test_real_graph_failed_stream_keeps_route_and_failed_call_timing(self):
        self.turn.side_effect = run_turn
        self.llm.call.side_effect = [
            (routed("product"), {"node": "supervisor", "total_ms": 10}),
            RuntimeError("broken stream"),
        ] * 3
        self.assertEqual(runner.execute(runner.parse_args(["--tag", "stream-error"])), 1)
        turn = self.result("stream-error")["scenarios"][0]["turns"][0]
        self.assertEqual(turn["routes"], ["product"])
        self.assertEqual(turn["latency"]["judge"]["total_ms"], 10)
        self.assertEqual(turn["llm_calls"][-1]["status"], "error")
        self.assertEqual(turn["llm_calls"][-1]["node"], "product")
        self.assertIn("request", turn["llm_calls"][-1])


class PreflightTests(unittest.TestCase):
    def test_memory_busy_threshold_before_loading(self):
        from shop.servers import preflight
        for used, blocked in ((3072, False), (3073, True)):
            with self.subTest(used=used), patch("shop.servers.ra.shared_memory", return_value={"adapter": 100}), patch("shop.servers.ra.runtime.gpu_memory", return_value=[{"index": 0, "used_mib": used}]), patch("pathlib.Path.is_file", return_value=True), patch("shop.servers.ra.socket.socket"), patch("shop.servers.processes", return_value=[]), patch("shop.servers.compute_apps", return_value={}):
                checks, errors = preflight("g12-native", 18380)
                self.assertEqual(checks["gpu"][0]["used_mib"], used)
                self.assertEqual(bool(errors), blocked)

    def test_outside_compute_process_blocks_even_at_low_memory(self):
        from shop.servers import preflight
        for apps, blocked in (({}, False), ({123: "outside"}, True)):
            with self.subTest(apps=apps), patch("shop.servers.ra.preflight", return_value=({"gpu": [{"index": 0, "used_mib": 100}]}, [])), patch("shop.servers.processes", return_value=[]), patch("shop.servers.compute_apps", return_value=apps):
                checks, errors = preflight("g12-native", 18380)
                self.assertEqual(checks["compute_apps"], apps)
                self.assertEqual(bool(errors), blocked)

    def test_compute_process_query_failure_blocks_loading(self):
        from shop.servers import preflight
        with patch("shop.servers.ra.preflight", return_value=({}, [])), patch("shop.servers.processes", return_value=[]), patch("shop.servers.compute_apps", side_effect=RuntimeError("query unavailable")):
            checks, errors = preflight("g12-native", 18380)
        self.assertIn("query unavailable", checks["compute_apps_error"])
        self.assertTrue(errors)

    def test_signal_scope_turns_sigterm_into_cleanup_exception(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt):
            with runner.dm.stop_on_signal():
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)


class SummaryTests(unittest.TestCase):
    def fixture(self, repeat=1, ngl=99):
        grade = {"route_correct": True, "required_count": 2, "required_name_hits": 1, "required_hits": 1, "glitches": {"latin_words": 2, "total": 2}}
        turn = {"status": "complete", "latency_comparable": True, "total_ms": 100, "final_reply_ttft_ms": 8, "steps": 4, "llm_calls": [{"node": "supervisor", "total_ms": 20, "ttft_ms": 2}, {"node": "product", "total_ms": 70, "ttft_ms": 5}]}
        turn["latency"] = runner.latency_split(turn)
        rows = [{"id": "single-01", "category": "single", "success": True, "expected_turns": 1, "expected_required_calls": 2, "contended": False, "latency_comparable": True, "turns": [turn], "grade": {"turns": [grade], "violations": {"hallucinated_value": 1}}}, {"id": "chain-01", "category": "chain", "success": False, "expected_turns": 2, "expected_required_calls": 3, "contended": True, "latency_comparable": False, "turns": [{**turn, "total_ms": 300, "latency": {**turn["latency"], "turn_total_ms": 300}}], "grade": {"turns": [{**grade, "route_correct": False}], "violations": {"unconfirmed_action": 1}}}]
        return {"config": "g12-native", "mode": "full", "gemma_ngl": ngl, "reference_offload": ngl != 99, "repeat_index": repeat, "status": "complete", "scenarios": rows, "peak_vram_mib_by_gpu": {"0": 9000}, "wall_time_s": 12, "dataset_sha256": "fixed"}

    def test_summary_metrics_and_contention_filter(self):
        result = summary.summarize_group([self.fixture()])
        self.assertEqual(result["success_rate"], .5)
        self.assertEqual(result["by_category"]["single"]["success_rate"], 1)
        self.assertEqual(result["route_accuracy"], 1 / 3)
        self.assertEqual(result["tool_recall"], 2 / 5)
        self.assertEqual(result["argument_accuracy"], 1)
        self.assertEqual(result["violations"]["hallucinated_value"], 1)
        self.assertEqual(result["violations"]["unconfirmed_action"], 1)
        self.assertEqual(result["glitches"]["total"], 4)
        self.assertEqual(result["contended_count"], 1)
        self.assertEqual(result["vram_peak_mib"], 9000)
        self.assertEqual(result["wall_time_s"], 12)
        latency = result["latency_ms_per_turn"]
        self.assertEqual(latency["all"]["turn_total"]["median"], 200)
        self.assertEqual(latency["all"]["turn_total"]["p90"], 280)
        self.assertEqual(latency["comparable"]["turn_total"]["n"], 1)
        self.assertEqual(latency["comparable"]["judge_total"]["median"], 20)
        self.assertEqual(latency["comparable"]["answer_total"]["median"], 70)
        self.assertEqual(latency["comparable"]["answer_ttft"]["median"], 8)

    def test_repeat_pass3_and_pass_any(self):
        runs = [self.fixture(i) for i in (1, 2, 3)]
        runs[2]["scenarios"][1]["success"] = True
        result = summary.summarize_group(runs)["reliability"]
        self.assertEqual(result["pass^3"], .5)
        self.assertEqual(result["pass>=1"], 1)
        self.assertIsNone(summary.summarize_group(runs[:2])["reliability"]["pass^3"])
        runs[2]["repeat_index"] = 1
        self.assertIsNone(summary.summarize_group(runs)["reliability"]["pass^3"])

    def test_files_group_reference_and_smoke_separately_and_reject_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            folders = []
            for index, run in enumerate((self.fixture(), self.fixture(2), self.fixture(1, 0), {**self.fixture(), "mode": "smoke"}, {**self.fixture(), "dataset_sha256": "different"})):
                folder = Path(directory) / str(index)
                folder.mkdir()
                (folder / "results.json").write_text(json.dumps(run))
                folders.append(folder)
            groups = summary.summarize(folders)
            self.assertEqual(len(groups), 4)
            self.assertEqual(groups[0]["runs"], 2)
            self.assertTrue(groups[1]["reference_offload"])
            self.assertEqual(groups[2]["mode"], "smoke")
            with self.assertRaisesRegex(ValueError, "Duplicate run folder"):
                summary.summarize([folders[0], folders[0]])

    def test_empty_and_partial_runs_have_no_invented_latency(self):
        run = self.fixture()
        run["scenarios"] = [{"id": "single-01", "expected_turns": 1, "expected_required_calls": 1, "success": False, "turns": [], "status": "blocked"}]
        result = summary.summarize_group([run])
        self.assertEqual(result["success_rate"], 0)
        self.assertEqual(result["route_accuracy"], 0)
        self.assertEqual(result["tool_recall"], 0)
        self.assertEqual(result["contention_unknown_count"], 1)
        self.assertIsNone(result["latency_ms_per_turn"]["all"]["answer_total"]["median"])


if __name__ == "__main__":
    unittest.main()
