"""Graph v2: 담당자 도구 호출을 실행 전에 중단하고 고객 동의로 재개한다."""

import copy
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware, hook_config
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command
from pydantic import Field

from .agents import handoff
from .graph import MAX_TRANSFER_BACK_PER_TURN
from .grader import output_values, value_tokens
from .shared import AGENTS
from .tools import ACTION_LABELS, JUDGE_CONSENT, IRREVERSIBLE, NODE_POLICY_IDS, SCOPES, schemas


MAX_MODEL_CALLS = 8
MAX_TOOL_CALLS = 6
GROUNDING_FALLBACK = "확인된 정보만으로 날짜나 금액을 안내하기 어렵습니다. 추가 확인이 필요합니다."


def ungrounded_values(reply, messages, ctx):
    reference_date = ctx.shop.reference_date
    known = value_tokens("\n".join(m.content for m in messages if isinstance(m, HumanMessage)), reference_date)
    known |= value_tokens(reference_date)
    for call in ctx.shop.trace:
        if call.get("kind") == "tool" and call["turn"] <= ctx.turn and not call.get("scope_error"):
            known |= output_values(call["output"], reference_date)
    for node in ctx.policy_context:
        known |= value_tokens("\n".join(ctx.shop.policy_context(node).values()), reference_date)
    known |= value_tokens(ctx.confirmation_card or "", reference_date)
    return sorted(value for value in value_tokens(reply, reference_date) - known if value[0].isdigit())


class Turn:
    """Separate specialist-model and real-tool budgets; steps is informational only.

    steps keeps the old model/routing/tools-round count for saved-result readers.
    tool_count includes domain functions and declined recall attempts, but excludes routing,
    handoffs, order prefetch and validation errors that never execute.
    llm_calls remains the timing-record list, including router routing.
    """

    def __init__(self, agents, shop, turn, emit, pending, assist):
        self.agents, self.shop, self.turn, self.emit, self.assist = agents, shop, turn, emit, assist
        self.pending = copy.deepcopy(pending)
        self.pending_at_start = copy.deepcopy(pending)
        self.confirmation_card = pending.get("confirmation_card") if pending else None
        self.policy_context = {}
        self.order_context = []
        self.sequence = 0
        self.pending_events = []
        self.consent_judgment = None
        self.resolving = None
        self.interrupted_messages = None
        self.customer_text = None
        self.resuming = False
        self.lock = threading.RLock()
        self.agent = ""
        self.routes, self.llm_calls, self.visible, self.ungrounded = [], [], [], []
        self.steps = self.tool_count = self.model_call_count = self.transfers = self.grounding_retries = self.force_tool_retries = 0
        self.loop_limit = self.grounding_failed = self.transfer = self.tools_ran = False
        self.limit_reached = False
        self.limit_reached_on = []
        self.reply = ""
        self.approval_state = ""

    def public(self):
        self.check_limits()
        return {
            "consent_mode": "tool_interrupt", "blocked_attempts": 0,
            "pending_at_start": copy.deepcopy(self.pending_at_start),
            "pending": copy.deepcopy(self.pending),
            "pending_events": copy.deepcopy(self.pending_events),
            "consent_judgment": copy.deepcopy(self.consent_judgment),
            "policy_context": copy.deepcopy(self.policy_context),
            "order_context": copy.deepcopy(self.order_context),
            "confirmation_card": self.confirmation_card,
            "approval_state": self.approval_state, "messages": [],
            "agent": self.agent, "routes": self.routes, "steps": self.steps,
            "tool_count": self.tool_count, "model_call_count": self.model_call_count,
            "limit_reached": self.limit_reached, "limit_reached_on": self.limit_reached_on,
            "transfers": self.transfers, "loop_limit": self.loop_limit,
            "reply": self.reply, "llm_calls": self.llm_calls, "visible": self.visible,
            "grounding_retries": self.grounding_retries, "force_tool_retries": self.force_tool_retries,
            "grounding_failed": self.grounding_failed, "ungrounded_values": self.ungrounded,
        }

    def event(self, event, pending, **fields):
        record = {"event": event, "turn": self.turn, "proposal": copy.deepcopy(pending), **copy.deepcopy(fields)}
        self.pending_events.append(record)
        if self.emit:
            self.emit("pending_lifecycle", lifecycle_event=event, **{k: v for k, v in record.items() if k != "event"})

    def clear(self, reason):
        pending, self.pending = self.pending, None
        if pending:
            self.event("cleared", pending, reason=reason)
        self.approval_state = ""

    def model(self, messages, tools, node):
        if self.model_call_count >= MAX_MODEL_CALLS:
            self.loop_limit = True
            return None
        self.model_call_count += 1
        try:
            message, timing = self.agents.call(messages, tools, node)
        except BaseException as error:
            if hasattr(error, "shop_llm_call"):
                self.llm_calls.append(error.shop_llm_call)
            raise
        self.llm_calls.append(timing)
        self.steps += 1
        return message

    def check_limits(self):
        # A last answer may end the turn without another before_model hook.
        limits = (("tool_count", MAX_TOOL_CALLS), ("model_call_count", MAX_MODEL_CALLS))
        self.loop_limit |= any(getattr(self, name) > maximum for name, maximum in limits)
        self.limit_reached_on = [name for name, maximum in limits if getattr(self, name) == maximum] if not self.loop_limit else []
        self.limit_reached = bool(self.limit_reached_on)


class ScopeModel(BaseChatModel):
    """Chat model adapter over Agents.call: keeps llm_calls records, timing/TTFT, emit events and LocalLLM's fixed
    tool_choice="auto" / parallel_tool_calls=False (LocalLLM.call applies them, so bind_tools ignores its arguments)."""

    ctx: Any = None
    node: str = ""
    bound: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "shopagent-local"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.model_copy(update={"bound": [convert_to_openai_tool(tool) for tool in tools]})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.ctx.model_call_count += 1
        try:
            message, timing = self.ctx.agents.call(messages, self.bound, self.node)
        except BaseException as error:
            if hasattr(error, "shop_llm_call"):
                self.ctx.llm_calls.append(error.shop_llm_call)
            raise
        self.ctx.llm_calls.append(timing)
        return ChatResult(generations=[ChatGeneration(message=message)])


def validate_arguments(value, schema):
    """Validate the object/string/array/enum subset emitted by tools.schemas, strictly.

    Fail closed for unsupported types; never coerce model arguments or add defaults.
    """
    kind = schema["type"]
    if isinstance(kind, list):
        for alternative in kind:
            try:
                validate_arguments(value, {**schema, "type": alternative})
                return
            except ValueError:
                pass
        raise ValueError("argument does not match any allowed type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError("arguments must be an object")
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= value.keys():
            raise ValueError("missing required arguments")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError("unexpected arguments")
        for key, item in value.items():
            if key in properties:
                validate_arguments(item, properties[key])
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError("argument must be a string")
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError("argument must be an array")
        for item in value:
            validate_arguments(item, schema["items"])
    elif kind == "null":
        if value is not None:
            raise ValueError("argument must be null")
    else:
        raise ValueError("unsupported schema type: " + kind)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("argument outside enum")


class ConsentRejectMessageMiddleware(HumanInTheLoopMiddleware):
    """거절 시 HITL 기본 영어 ToolMessage 대신 consent_resume_message 템플릿으로 바꾼다.

    LangChain은 거절(reject)을 status="error", 애매(respond)를 status="success" ToolMessage로 만든다.
    그래서 error만 바꾸고, 승인·수정·애매 결과와 name, tool_call_id, status는 그대로 둔다.
    """

    def __init__(self, ctx, **kwargs):
        super().__init__(**kwargs)
        self.ctx = ctx

    def _resolve_per_call(self, request):
        result = super()._resolve_per_call(request)
        if isinstance(result, ToolMessage) and result.status == "error":
            return result.model_copy(update={"content": consent_resume_message(
                "no", self.ctx.resume_decision["message"], request.tool_call)})
        return result


def make_tools(ctx, scope):
    def tool(schema, function):
        spec = schema["function"]
        return StructuredTool(name=spec["name"], description=spec["description"], args_schema=spec["parameters"], func=function)

    def domain(name):
        def run(**arguments):
            with ctx.lock:
                ctx.tool_count += 1
                pending = copy.deepcopy(ctx.resolving or ctx.pending) if name in IRREVERSIBLE else None
                with ctx.shop.defer_call_events():
                    output = ctx.shop.invoke(name, arguments, ctx.turn)
                    if pending:
                        ctx.shop.trace[-1]["proposal_id"] = pending["id"]
                if pending:
                    ctx.event("executed", pending, output=output)
                    ctx.clear("executed")
                    ctx.resolving = None
                return json.dumps(output, ensure_ascii=False)
        return run

    tools = [tool(schema, domain(schema["function"]["name"])) for schema in schemas(scope, v2=True)]
    return tools + [tool(handoff("transfer_back_to_router"), lambda **arguments: "")]


class TurnMiddleware(AgentMiddleware):
    """Limits, transfer_back, force_tool, the ungrounded-values rewrite (port of graph.py)."""

    def __init__(self, ctx, scope):
        super().__init__()
        self.ctx, self.scope = ctx, scope

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        ctx = self.ctx
        if ctx.tools_ran:
            # Compatibility information only; tools rounds consume neither budget.
            ctx.tools_ran = False
            ctx.steps += 1
        if ctx.model_call_count >= MAX_MODEL_CALLS and not ctx.transfer:
            ctx.loop_limit = True
        if ctx.loop_limit or ctx.transfer:
            return {"jump_to": "end"}
        return None

    @hook_config(can_jump_to=["model", "tools", "end"])
    def after_model(self, state, runtime):
        ctx = self.ctx
        messages = state["messages"]
        message = messages[-1]
        calls = message.tool_calls
        ctx.steps += 1
        ctx.reply = message.content if not calls else ""
        ctx.visible.append(message.content)
        if calls and ctx.grounding_retries == 0:
            return {"jump_to": "tools"}
        if not ctx.assist:
            # Rules off: keep the model's own answer; only note which values no tool output supports.
            ctx.ungrounded = ungrounded_values(message.content, messages, ctx)
            return None
        if not calls and ctx.force_tool_retries == 0 and ctx.grounding_retries == 0:
            user = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
            required = []
            if "영수증" in user:
                required.append("issue_receipt")
            missing = [name for name in required if name in SCOPES[self.scope] and not any(call.get("kind") == "tool" and call["turn"] == ctx.turn and call["name"] == name for call in ctx.shop.trace)]
            if missing:
                note = SystemMessage(content="Before answering, call " + ", ".join(missing) + " for the user's request. Lookup and eligibility tools do not require confirmation. Do not invent eligibility, policy text or receipt details. Check order ownership before further lookups. Keep confirmation requirements for irreversible actions; obtain consent on a later user turn.")
                ctx.reply = ""
                ctx.visible.pop()
                ctx.force_tool_retries = 1
                return {"messages": [note], "jump_to": "model"}
        unseen = ungrounded_values(message.content, messages, ctx)
        ctx.grounding_failed = bool(unseen or calls)
        ctx.ungrounded = unseen
        if unseen or calls:
            ctx.visible.pop()
            if ctx.grounding_retries == 0:
                note = SystemMessage(content="Answer the user's request again. Do not invent dates or amounts. Use only tool outputs from this turn and user messages in the history. Omit values that these sources do not support. Return a final answer without tool calls.")
                ctx.reply = ""
                ctx.grounding_retries = 1
                return {"messages": [RemoveMessage(id=message.id), note], "jump_to": "model"}
            ctx.reply = GROUNDING_FALLBACK
            ctx.visible.append(GROUNDING_FALLBACK)
            return {"messages": [ToolMessage(content="grounding retry limit reached", tool_call_id=c["id"], name=c["name"]) for c in calls] + [AIMessage(content=GROUNDING_FALLBACK)], "jump_to": "end"}
        return None

    def wrap_tool_call(self, request, handler):
        # Serialized so a multi-call message behaves like v1's sequential loop (count/limit/trace order).
        with self.ctx.lock:
            return self._tool_call(request, handler)

    def _tool_call(self, request, handler):
        ctx = self.ctx
        call = request.tool_call
        name, arguments = call["name"], call["args"]

        def answer(output, status="success"):
            return ToolMessage(content=json.dumps(output, ensure_ascii=False), tool_call_id=call["id"], name=name, status=status)

        # Let the interrupted call consume its HITL decision first. Only a new
        # attempt is guarded, even when the model reuses the original call ID.
        resolving = ctx.resolving
        declined = None
        if name in IRREVERSIBLE and not (resolving and resolving.get("tool_call_id") == call["id"]):
            declined = next((entry for entry in request.runtime.context["declined_actions"]
                             if entry["turn"] == ctx.turn and entry["tool"] == name
                             and entry["order_id"] == arguments.get("order_id")), None)
        message = next(m for m in reversed(request.state["messages"]) if isinstance(m, AIMessage))
        if name in IRREVERSIBLE and not declined:
            gated = [c for c in message.tool_calls if c["name"] in IRREVERSIBLE]
            if gated and call["id"] != gated[0]["id"]:
                return ToolMessage(content="한 응답에서는 첫 번째 실행 요청만 확인할 수 있습니다. 추가 요청은 실행하지 않았습니다.", tool_call_id=call["id"], name=name, status="error")
        if ctx.loop_limit:
            return answer({"error": "tool call limit reached"})
        if name != "transfer_back_to_router" and ctx.tool_count >= MAX_TOOL_CALLS:
            ctx.loop_limit = ctx.tools_ran = True
            return answer({"error": "tool call limit reached"})
        if name == "transfer_back_to_router":
            if ctx.transfers >= MAX_TRANSFER_BACK_PER_TURN or len(message.tool_calls) != 1 or arguments:
                ctx.loop_limit = True
                output = {"error": f"transfer_back_to_router allowed up to {MAX_TRANSFER_BACK_PER_TURN} times per turn, alone, without arguments"}
            else:
                ctx.transfers += 1
                ctx.transfer = True
                output = {"transferred": True}
            ctx.shop.record_call({"kind": "handoff", "turn": ctx.turn, "name": name, "arguments": arguments, "output": output, "changes": {}})
            ctx.tools_ran = True
            return answer(output)
        allowed = name in SCOPES[self.scope]
        if request.tool is None or not allowed:
            output = {"error": "tool outside agent scope"}
            ctx.shop.record_call({"kind": "tool", "turn": ctx.turn, "name": name, "arguments": arguments, "output": output, "changes": {}, "scope_error": True})
            ctx.tools_ran = True
            return answer(output)
        if declined:
            output = {"ok": False, "error": "DECLINED_BY_CUSTOMER",
                      "message": f"고객이 이번 요청에서 이미 이 작업({name}, 주문 {arguments.get('order_id')})을 거절했습니다. 다시 실행하지 말고 고객에게 답변하세요."}
            ctx.tool_count += 1
            ctx.shop.record_call({"kind": "blocked", "turn": ctx.turn, "name": name,
                                  "arguments": copy.deepcopy(arguments), "output": output, "changes": {},
                                  "proposal_id": declined["proposal_id"], "declined_guard": True})
            ctx.tools_ran = True
            return answer(output, status="error")
        result = handler(request)
        ctx.tools_ran = True
        ctx.resolving = None
        return result


class SpecialistContext(TypedDict):
    declined_actions: list[dict]


def build_agent(ctx, scope, agents, shop):
    def describe(call, state, runtime):
        # HITL은 재개 때도 설명을 생성하므로 같은 제안은 다시 기록하지 않는다.
        pending = ctx.resolving or ctx.pending
        if pending and pending.get("tool_call_id") == call["id"]:
            return pending["message"]
        ctx.sequence += 1
        card = ctx.shop.confirmation_card(call["name"], call["args"])
        message = next(m for m in reversed(state["messages"]) if isinstance(m, AIMessage))
        text = message.content + "\n\n" + card if message.content else card
        pending = {"id": f"proposal-{ctx.sequence}", "scope": scope, "name": call["name"],
                   "args": copy.deepcopy(call["args"]), "message": text,
                   "confirmation_card": card, "turn": ctx.turn, "tool_call_id": call["id"]}
        ctx.pending = pending
        ctx.confirmation_card = card
        ctx.reply = text
        ctx.approval_state = "고객 확인 대기"
        if ctx.visible and ctx.visible[-1] == message.content:
            ctx.visible[-1] = text
        else:
            ctx.visible.append(text)
        ctx.ungrounded = ungrounded_values(message.content, state["messages"], ctx)
        ctx.interrupted_messages = state["messages"]
        ctx.event("proposed", pending)
        return text

    gate = ConsentRejectMessageMiddleware(ctx,
        interrupt_mode="per_call",
        interrupt_on={name: {"allowed_decisions": ["approve", "reject", "respond"], "description": describe}
                      for name in SCOPES[scope] if name in IRREVERSIBLE})
    # 거절된 재호출은 바깥 미들웨어에서 횟수를 세고 HITL 전에 차단한다.
    return create_agent(ScopeModel(ctx=ctx, node=scope), make_tools(ctx, scope),
                        system_prompt=agents.messages(scope, [], v2=True, shop=shop)[0],
                        context_schema=SpecialistContext,
                        middleware=[TurnMiddleware(ctx, scope), gate], name=scope)


class State(TypedDict):
    messages: Annotated[list, add_messages]
    agent: str
    declined_actions: list[dict]


class TurnContext:
    """Resolve fresh invocation metrics inside nodes, model adapters and tool workers.

    No counters or runtime objects enter checkpointed state.
    """

    def __getattr__(self, name):
        return getattr(get_config()["configurable"]["turn_context"], name)

    def __setattr__(self, name, value):
        setattr(get_config()["configurable"]["turn_context"], name, value)


def build_graph(agents, checkpointer, shop):
    ctx = TurnContext()

    def observed(name, function, scope=None):
        def visit(state, config: RunnableConfig):
            policy_node = scope or name
            if policy_node in NODE_POLICY_IDS:
                ctx.policy_context[policy_node] = list(NODE_POLICY_IDS[policy_node])
            effective = scope
            if effective:
                ctx.agent = effective
                if scope or not ctx.routes or ctx.routes[-1] != effective:
                    ctx.routes.append(effective)
            if scope == "order_delivery" and not ctx.resuming:
                user = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
                known = {item["order_id"] for item in ctx.order_context}
                prefetched = []
                for order_id in dict.fromkeys(re.findall(r"(?<![A-Za-z0-9])O\d+(?![A-Za-z0-9])", user)):
                    if order_id not in known:
                        with ctx.shop.defer_call_events():
                            output = ctx.shop.invoke("get_order", {"order_id": order_id}, ctx.turn)
                            output = ctx.shop.order_summary(output)
                            ctx.shop.trace[-1]["output"] = copy.deepcopy(output)
                            ctx.shop.trace[-1]["prefetched"] = True
                        ctx.order_context.append({"order_id": order_id, "prefetched": True, "output": copy.deepcopy(output)})
                        call_id = f"prefetch-{ctx.turn}-{order_id}"
                        prefetched.extend([
                            AIMessage(id=call_id, content="", tool_calls=[{"name": "get_order", "args": {"order_id": order_id}, "id": call_id, "type": "tool_call"}]),
                            ToolMessage(id=call_id + "-result", content=json.dumps(output, ensure_ascii=False), tool_call_id=call_id, name="get_order"),
                        ])
                state = {**state, "messages": add_messages(state["messages"], prefetched)}
            start = time.perf_counter()
            started = datetime.now(timezone.utc).isoformat()
            if ctx.emit:
                ctx.emit("node_start", node=name, agent=ctx.agent, started=started)
            status = "error"
            try:
                value = function(state, config)
                status = "complete"
                return value
            except GraphInterrupt:
                status = "interrupted"
                raise
            finally:
                if ctx.emit:
                    ctx.emit("node_end", node=name, status=status, started=started, finished=datetime.now(timezone.utc).isoformat(), total_ms=(time.perf_counter() - start) * 1000)
        return visit

    def router(state, config):
        route, message, timing = ctx.agents.supervise(state["messages"], ctx.turn, v2=True)
        ctx.llm_calls.append(timing)
        ctx.agent = route
        ctx.steps += 1
        ctx.transfer = False
        update = {"agent": route}
        if message is not None:
            ctx.visible.append(message.content)
            update["messages"] = [message, ToolMessage(content="담당 에이전트로 연결했습니다.", tool_call_id=message.tool_calls[0]["id"])]
        return Command(update=update, goto="escalate" if route == "human" else route)

    def escalate(state, config):
        if ctx.tool_count >= MAX_TOOL_CALLS:
            ctx.loop_limit = True
            return {}
        user = next(m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        arguments = {"reason": "Human route: " + user, "summary": user.strip() or "Human assistance requested"}
        ctx.tool_count += 1
        output = ctx.shop.invoke("escalate_to_human", arguments, ctx.turn)
        reply = "상담원에게 요청을 접수했습니다." if output.get("escalated") else "상담원 접수에 실패했습니다. 다시 요청해 주세요."
        call_id = f"human-{ctx.turn}"
        call = AIMessage(content="", tool_calls=[{"name": "escalate_to_human", "args": arguments, "id": call_id, "type": "tool_call"}])
        ctx.reply = reply
        ctx.visible.append(reply)
        ctx.steps += 1
        return {"messages": [call, ToolMessage(content=json.dumps(output, ensure_ascii=False), tool_call_id=call_id), AIMessage(content=reply)]}

    def specialist(scope):
        agent = build_agent(ctx, scope, agents, shop)

        def run(state, config):
            # The child's interrupted tools task retains its old state. Supply
            # the parent's checkpointed declines as fresh runtime context on replay.
            result = agent.invoke({"messages": state["messages"]}, config,
                                  context={"declined_actions": state.get("declined_actions", [])})
            ctx.resuming = False
            # The child reducer may remove an ungrounded answer. Propagate those
            # deletions explicitly through the parent's add_messages reducer.
            kept = {m.id for m in result["messages"]}
            removed = [RemoveMessage(id=m.id) for m in state["messages"] if m.id not in kept]
            messages = removed + result["messages"]
            if ctx.customer_text is not None:
                # 중단된 도구 응답 뒤에 고객 발화를 보존한다. AI 도구 요청/응답 쌍은 유지한다.
                position = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], ToolMessage)), len(messages) - 1) + 1
                messages.insert(position, HumanMessage(content=ctx.customer_text))
                ctx.customer_text = None
            return {"messages": messages}
        return run

    def after_agent(state):
        ctx.check_limits()
        if ctx.transfer and not ctx.loop_limit:
            return "router"
        return "finish"

    def finish(state, config):
        return {}

    builder = StateGraph(State)
    builder.add_node("router", observed("router", router), destinations=("escalate", *[a for a in AGENTS if a != "human"]))
    builder.add_node("escalate", observed("escalate", escalate, "human"))
    builder.add_edge("escalate", "finish")
    builder.add_node("finish", observed("finish", finish))
    builder.add_edge("finish", END)
    builder.add_edge(START, "router")
    for scope in AGENTS:
        if scope != "human":
            builder.add_node(scope, observed(scope, specialist(scope), scope))
            paths = {name: name for name in ("router", "finish")}
            builder.add_conditional_edges(scope, after_agent, path_map=paths)
    return builder.compile(checkpointer=checkpointer)


class ScenarioGraph:
    """시나리오별 체크포인트와 그래프 밖 동의 판정 및 재개를 관리한다."""

    def __init__(self, agents, shop, config=None, emit=None, assist=False):
        self.agents, self.shop, self.emit, self.assist = agents, shop, emit, assist
        self.checkpointer = InMemorySaver()
        self.thread_id = str(uuid4())
        self.config = {"recursion_limit": 64, **(config or {})}
        self.config["configurable"] = {**self.config.get("configurable", {}), "thread_id": self.thread_id}
        self.pending = None
        self.sequence = 0
        self.graph = build_graph(agents, self.checkpointer, shop)

    def _context(self, turn_index):
        ctx = Turn(self.agents, self.shop, turn_index, self.emit, self.pending, self.assist)
        ctx.sequence = self.sequence
        return ctx

    def run_turn(self, turn_index, user_text):
        ctx = self._context(turn_index)
        start = time.perf_counter()
        try:
            if self.graph.get_state(self.config).interrupts:
                decision = judge_consent(ctx, user_text)
                return self.resume_turn(turn_index, decision, user_text, _ctx=ctx, _start=start)
            return self._invoke(ctx, {"messages": [HumanMessage(content=user_text)], "declined_actions": []}, start)
        except BaseException as error:
            error.shop_state = ctx.public()
            error.shop_total_ms = (time.perf_counter() - start) * 1000
            raise

    def resume_turn(self, turn_index, decision, customer_text, *, _ctx=None, _start=None):
        """프런트엔드의 approve/reject/respond를 LLM 판정 없이 그대로 재개한다."""
        snapshot = self.graph.get_state(self.config)
        if not snapshot.interrupts or not self.pending:
            raise ValueError("No pending tool approval")
        if decision.get("type") not in ("approve", "reject", "respond"):
            raise ValueError("Expected approve/reject/respond")
        if decision["type"] == "reject" and "message" not in decision:
            decision = {**decision, "message": customer_text}
        if decision["type"] in ("reject", "respond") and not isinstance(decision.get("message"), str):
            raise ValueError("A rejection or response requires a message")
        ctx = _ctx or self._context(turn_index)
        ctx.resume_decision = copy.deepcopy(decision)
        pending = copy.deepcopy(ctx.pending)
        ctx.resolving = pending
        ctx.customer_text = customer_text
        ctx.agent = pending["scope"]
        if ctx.consent_judgment is None:
            verdict = {"approve": "yes", "reject": "no", "respond": "unclear"}[decision["type"]]
            ctx.consent_judgment = {"proposal_id": pending["id"], "verdict": verdict, "reason": "고객 확인 버튼", "customer_text": customer_text}
            ctx.event("judged", pending, **{k: v for k, v in ctx.consent_judgment.items() if k != "proposal_id"})
        if decision["type"] != "approve":
            ctx.clear(ctx.consent_judgment["verdict"])
        update = None
        if decision["type"] == "reject":
            declined = {"tool": pending["name"], "order_id": pending["args"].get("order_id"),
                        "proposal_id": pending["id"], "turn": turn_index}
            update = {"declined_actions": [*snapshot.values.get("declined_actions", []), declined]}
        return self._invoke(ctx, Command(resume={snapshot.interrupts[0].id: decision}, update=update),
                            _start if _start is not None else time.perf_counter())

    def _invoke(self, ctx, value, start):
        ctx.resuming = isinstance(value, Command) or value is None
        config = {**self.config, "configurable": {**self.config["configurable"], "turn_context": ctx}}
        try:
            result = self.graph.invoke(value, config)
        except BaseException as error:
            error.shop_state = ctx.public()
            error.shop_total_ms = (time.perf_counter() - start) * 1000
            raise
        finally:
            self.pending = copy.deepcopy(ctx.pending)
            self.sequence = ctx.sequence
        final = ctx.public()
        final["declined_actions"] = copy.deepcopy(result.get("declined_actions", []))
        final["messages"] = ctx.interrupted_messages or result["messages"]
        final["total_ms"] = (time.perf_counter() - start) * 1000
        calls = final["llm_calls"]
        answer_index = next((i for i in range(len(calls) - 1, -1, -1) if calls[i]["node"] not in ("router", "consent_judge")), None)
        answer = calls[answer_index] if answer_index is not None else {}
        final["final_reply_ttft_ms"] = answer.get("content_ttft_ms") if final["agent"] != "human" and not final["grounding_failed"] and final["reply"] else None
        final["turn_to_final_reply_ttft_ms"] = final["total_ms"] - sum(c["total_ms"] for c in calls[answer_index:]) + final["final_reply_ttft_ms"] if final["final_reply_ttft_ms"] is not None else None
        return final


def judge_consent(ctx, customer_text):
    """그래프와 분리된 기존 LLM 판정을 일반 HITL 결정으로 바꾼다."""
    pending = ctx.pending
    message = ctx.model([
        SystemMessage(content=ctx.agents.prompts["consent_judge"]),
        HumanMessage(content=json.dumps({"proposal": {"action": pending["name"], "arguments": pending["args"],
                                                     "message": pending["message"]},
                                        "customer_message": customer_text}, ensure_ascii=False)),
    ], [JUDGE_CONSENT], "consent_judge")
    calls = message.tool_calls
    if len(calls) != 1 or calls[0]["name"] != "judge_consent":
        raise ValueError("Consent judge must call judge_consent once")
    arguments = calls[0]["args"]
    validate_arguments(arguments, JUDGE_CONSENT["function"]["parameters"])
    ctx.consent_judgment = {"proposal_id": pending["id"], **arguments}
    ctx.event("judged", pending, **arguments)
    return consent_resume_decision(arguments["verdict"], customer_text)


def consent_resume_decision(verdict, customer_text):
    """Map a recorded consent verdict to the deterministic specialist resume."""
    if verdict == "yes":
        return {"type": "approve"}
    if verdict in ("no", "changed"):
        return {"type": "reject", "message": customer_text}
    return {"type": "respond", "message": consent_resume_message(verdict, customer_text)}


def consent_resume_message(verdict, customer_text, tool_call=None):
    """One Korean result template for HITL and offline resume display."""
    if verdict in ("no", "changed"):
        name = tool_call["name"]
        order_id = tool_call["args"].get("order_id", "미지정")
        return (f"실행되지 않았습니다. 고객이 이 {ACTION_LABELS[name]}({name}, 주문 {order_id})을(를) 승인하지 않았습니다. "
                f"완료되었다고 안내하지 마세요. 고객 답변: {customer_text}")
    return ("실행되지 않았습니다. 고객 답변이 불분명합니다. 완료되었다고 안내하지 마세요. "
            "필요하면 다시 확인을 요청하세요. 고객 답변: " + customer_text)
