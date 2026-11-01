import json
import re
import time
from datetime import datetime, timezone
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .grader import output_values, value_tokens
from .shared import ROOT, REFERENCE_DATE, load
from .tools import SCOPES


MAX_TRANSFER_BACK_PER_TURN = 3


class State(TypedDict):
    messages: list
    agent: str
    routes: list
    pending: list
    steps: int
    tool_count: int
    transfers: int
    loop_limit: bool
    reply: str
    llm_calls: list
    visible: list
    grounding_retries: int
    force_tool_retries: int
    grounding_failed: bool
    ungrounded_values: list


def ungrounded_values(reply, messages, shop, turn):
    known = value_tokens("\n".join(m.content for m in messages if isinstance(m, HumanMessage)))
    for call in shop.trace:
        # CS-08: tool outputs from earlier turns are also evidence.
        if call.get("kind") == "tool" and call["turn"] <= turn:
            known |= output_values(call["output"])
    known |= value_tokens(load(ROOT / "data/prompts.json")["common"].format(customer_id=shop.customer_id, reference_date=REFERENCE_DATE))
    # The grader also extracts IDs, which this guard does not check.
    return sorted(value for value in value_tokens(reply) - known if value[0].isdigit())


def build_graph(agents, shop, turn, emit=None):
    def supervisor(state):
        if state["steps"] >= 8:
            return Command(update={"loop_limit": True}, goto=END)
        route, message, timing = agents.supervise(state["messages"], turn)
        messages = state["messages"][:]
        visible = state["visible"][:]
        if message is not None:
            messages.extend([message, ToolMessage(content="담당 에이전트로 연결했습니다.", tool_call_id=message.tool_calls[0]["id"])])
            visible.append(message.content)
        return Command(update={"messages": messages, "visible": visible, "routes": state["routes"] + [route], "agent": route, "steps": state["steps"] + 1, "llm_calls": state["llm_calls"] + [timing]}, goto="escalate" if route == "human" else "agent_llm")

    def escalate(state):
        if state["steps"] >= 8 or state["tool_count"] >= 6:
            return Command(update={"loop_limit": True}, goto=END)
        user = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        arguments = {"reason": "Human route: " + user, "summary": user.strip() or "Human assistance requested"}
        output = shop.invoke("escalate_to_human", arguments, turn)
        reply = "상담원에게 요청을 접수했습니다." if output.get("escalated") else "상담원 접수에 실패했습니다. 다시 요청해 주세요."
        call_id = f"human-{turn}"
        call = AIMessage(content="", tool_calls=[{"name": "escalate_to_human", "args": arguments, "id": call_id, "type": "tool_call"}])
        messages = state["messages"] + [call, ToolMessage(content=json.dumps(output, ensure_ascii=False), tool_call_id=call_id), AIMessage(content=reply)]
        return Command(update={"messages": messages, "reply": reply, "visible": state["visible"] + [reply], "pending": [], "steps": state["steps"] + 1, "tool_count": state["tool_count"] + 1}, goto=END)

    def agent_llm(state):
        if state["steps"] >= 8:
            return Command(update={"loop_limit": True, "pending": []}, goto=END)
        message, timing = agents.respond(state["agent"], state["messages"])
        update = {"messages": state["messages"] + [message], "steps": state["steps"] + 1, "pending": message.tool_calls, "reply": message.content if not message.tool_calls else "", "visible": state["visible"] + [message.content], "llm_calls": state["llm_calls"] + [timing]}
        if message.tool_calls and state["grounding_retries"] == 0:
            return Command(update=update, goto="tools")
        if not message.tool_calls and state["force_tool_retries"] == 0 and state["grounding_retries"] == 0 and update["steps"] < 8:
            user = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
            required = []
            if re.search(r"(?<![A-Za-z0-9])P\d+(?![A-Za-z0-9])", user):
                required.append("get_policy")
            if "영수증" in user:
                required.append("issue_receipt")
            if state["agent"] == "refund_exchange" and re.search(r"(?<![A-Za-z0-9])O\d+(?![A-Za-z0-9])", user) and re.search(r"가능|신청하지|예외|환불|교환|반품", user) and "동의" not in user:
                required.extend(["get_order", "check_refund_eligibility"])
            missing = [name for name in required if name in SCOPES[state["agent"]] and not any(call.get("kind") == "tool" and call["turn"] == turn and call["name"] == name for call in shop.trace)]
            if missing:
                note = SystemMessage(content="Before answering, call " + ", ".join(missing) + " for the user's request. Lookup and eligibility tools do not require confirmation. Do not invent eligibility, policy text or receipt details. Check order ownership before further lookups. Keep confirmation requirements for irreversible actions; obtain consent on a later user turn.")
                update.update(messages=update["messages"] + [note], reply="", visible=state["visible"][:], force_tool_retries=1)
                return Command(update=update, goto="agent_llm")
        unseen = ungrounded_values(message.content, state["messages"], shop, turn)
        update.update(grounding_failed=bool(unseen or message.tool_calls), ungrounded_values=unseen)
        if unseen or message.tool_calls:
            update["visible"] = state["visible"][:]
            if state["grounding_retries"] == 0 and update["steps"] < 8:
                note = SystemMessage(content="Rewrite the previous answer. Do not invent dates or amounts. Use only tool outputs from this turn and user messages in the history. Omit values that these sources do not support. Return a final answer without tool calls.")
                update.update(messages=update["messages"] + [note], reply="", grounding_retries=1)
                return Command(update=update, goto="agent_llm")
            reply = "확인된 정보만으로 날짜나 금액을 안내하기 어렵습니다. 추가 확인이 필요합니다."
            update.update(messages=state["messages"] + [AIMessage(content=reply)], pending=[], reply=reply, visible=state["visible"] + [reply])
        return Command(update=update, goto=END)

    def tools(state):
        if state["steps"] >= 8:
            return Command(update={"loop_limit": True}, goto=END)
        messages = state["messages"][:]
        count = state["tool_count"]
        transfers = state["transfers"]
        target = "agent_llm"
        limit = False
        calls = state["pending"]
        for call in calls:
            if count >= 6:
                limit = True
                break
            name = call["name"]
            count += 1
            if name == "transfer_back_to_supervisor":
                if transfers >= MAX_TRANSFER_BACK_PER_TURN or len(calls) != 1 or call["args"]:
                    limit = True
                    output = {"error": f"transfer_back_to_supervisor allowed up to {MAX_TRANSFER_BACK_PER_TURN} times per turn, alone, without arguments"}
                else:
                    transfers += 1
                    target = "supervisor"
                    output = {"transferred": True}
                shop.record_call({"kind": "handoff", "turn": turn, "name": name, "arguments": call["args"], "output": output, "changes": {}})
            elif name not in SCOPES[state["agent"]]:
                output = {"error": "tool outside agent scope"}
                shop.record_call({"kind": "tool", "turn": turn, "name": name, "arguments": call["args"], "output": output, "changes": {}, "scope_error": True})
            else:
                output = shop.invoke(name, call["args"], turn)
            messages.append(ToolMessage(content=json.dumps(output, ensure_ascii=False), tool_call_id=call["id"]))
        steps = state["steps"] + 1
        limit = limit or steps >= 8
        return Command(update={"messages": messages, "pending": [], "steps": steps, "tool_count": count, "transfers": transfers, "loop_limit": limit}, goto=END if limit else target)

    def observed(name, function):
        def visit(state):
            start = time.perf_counter()
            started = datetime.now(timezone.utc).isoformat()
            if emit:
                emit("node_start", node=name, agent=state["agent"], started=started)
            status = "error"
            try:
                value = function(state)
                status = "complete"
                return value
            finally:
                if emit:
                    emit("node_end", node=name, status=status, started=started, finished=datetime.now(timezone.utc).isoformat(), total_ms=(time.perf_counter() - start) * 1000)
        return visit

    builder = StateGraph(State)
    builder.add_node("supervisor", observed("supervisor", supervisor), destinations=("agent_llm", "escalate", END))
    builder.add_node("escalate", observed("escalate", escalate), destinations=(END,))
    builder.add_node("agent_llm", observed("agent_llm", agent_llm), destinations=("agent_llm", "tools", END))
    builder.add_node("tools", observed("tools", tools), destinations=("supervisor", "agent_llm", END))
    builder.add_edge(START, "supervisor")
    return builder.compile()


def run_turn(agents, shop, turn, user, history, config=None, emit=None):
    start = time.perf_counter()
    state = {"messages": history + [HumanMessage(content=user)], "agent": "", "routes": [], "pending": [], "steps": 0, "tool_count": 0, "transfers": 0, "loop_limit": False, "reply": "", "llm_calls": [], "visible": [], "grounding_retries": 0, "force_tool_retries": 0, "grounding_failed": False, "ungrounded_values": []}
    final = None
    try:
        for value in build_graph(agents, shop, turn, emit).stream(state, config={"recursion_limit": 32, **(config or {})}, stream_mode="values"):
            final = value
    except BaseException as error:
        error.shop_state = final or state
        error.shop_total_ms = (time.perf_counter() - start) * 1000
        raise
    final["total_ms"] = (time.perf_counter() - start) * 1000
    calls = final["llm_calls"]
    final["final_reply_ttft_ms"] = calls[-1]["content_ttft_ms"] if calls and final["agent"] != "human" and not final["grounding_failed"] and final["reply"] and "content_ttft_ms" in calls[-1] else None
    final["turn_to_final_reply_ttft_ms"] = final["total_ms"] - calls[-1]["total_ms"] + final["final_reply_ttft_ms"] if final["final_reply_ttft_ms"] is not None else None
    return final
