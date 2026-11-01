import time
from datetime import datetime, timezone

import httpx
from langchain_core.messages import SystemMessage, message_chunk_to_message
from langchain_openai import ChatOpenAI

from .shared import AGENTS, ROOT, REFERENCE_DATE, dm, load
from .tools import schemas


def handoff(name):
    return {"type": "function", "function": {"name": name, "description": "해당 담당자로 제어를 넘깁니다.", "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}}


class LocalLLM:
    def __init__(self, port, timeout=180, http_client=None):
        self.client = ChatOpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key="local", model="local", temperature=0, max_tokens=1024, timeout=timeout, max_retries=0, stream_usage=True, http_client=http_client or httpx.Client(trust_env=False), extra_body={"cache_prompt": False})

    def close(self):
        self.client.http_client.close()

    def call(self, messages, tools, node):
        started = datetime.now(timezone.utc).isoformat()
        start = time.perf_counter()
        first = None
        content_first = None
        combined = None
        for chunk in self.client.bind_tools(tools, tool_choice="required" if node == "supervisor" else "auto", parallel_tool_calls=False).stream(messages):
            elapsed = (time.perf_counter() - start) * 1000
            if first is None and (chunk.content or chunk.tool_call_chunks):
                first = elapsed
            if content_first is None and chunk.content:
                content_first = elapsed
            combined = chunk if combined is None else combined + chunk
        if combined is None:
            raise RuntimeError("Empty LLM stream")
        if combined.invalid_tool_calls:
            raise ValueError("Invalid tool-call JSON: " + str(combined.invalid_tool_calls))
        if combined.response_metadata.get("finish_reason") == "length":
            raise RuntimeError("LLM output token limit reached")
        message = message_chunk_to_message(combined)
        return message, {"node": node, "started": started, "finished": datetime.now(timezone.utc).isoformat(), "ttft_ms": first, "content_ttft_ms": content_first, "total_ms": (time.perf_counter() - start) * 1000, "tokens": combined.usage_metadata, "finish_reason": combined.response_metadata.get("finish_reason"), "text": message.content, "tool_calls": message.tool_calls}


class Agents:
    def __init__(self, llm, customer_id, backend=None, emit=None):
        self.llm, self.customer_id, self.backend = llm, customer_id, backend
        self.emit = emit
        self.prompts = load(ROOT / "data/prompts.json")

    def observe(self, node, request, call):
        start = time.perf_counter()
        started = datetime.now(timezone.utc).isoformat()
        if self.emit:
            self.emit("llm_request", node=node, started=started, request=request)
        try:
            value, timing = call()
        except BaseException as error:
            timing = {"node": node, "started": started, "finished": datetime.now(timezone.utc).isoformat(), "total_ms": (time.perf_counter() - start) * 1000, "ttft_ms": None, "status": "error", "error": dm.error_text(error), "request": request}
            error.shop_llm_call = timing
            if self.emit:
                self.emit("llm_response", **timing)
            raise
        timing.update(request=request, status="complete")
        if self.emit:
            self.emit("llm_response", **timing)
        return value, timing

    def call(self, messages, tools, node):
        request = {"messages": [m.model_dump(mode="json") for m in messages], "tools": tools, "tool_choice": "required" if node == "supervisor" else "auto", "parallel_tool_calls": False, "temperature": 0, "max_tokens": 1024, "cache_prompt": False}
        return self.observe(node, request, lambda: self.llm.call(messages, tools, node))

    def messages(self, role, history):
        text = self.prompts["common"].format(customer_id=self.customer_id, reference_date=REFERENCE_DATE)
        text += "\n" + self.prompts[role]
        return [SystemMessage(content=text), *history]

    def supervise(self, history, turn):
        if self.backend is None:
            message, timing = self.call(self.messages("supervisor", history), [handoff("transfer_to_" + agent) for agent in AGENTS], "supervisor")
            calls = message.tool_calls
            if len(calls) != 1 or calls[0]["name"] not in ["transfer_to_" + agent for agent in AGENTS] or calls[0]["args"]:
                error = ValueError("Supervisor must call one transfer_to_<agent> without arguments")
                error.shop_llm_call = timing
                raise error
            return calls[0]["name"][12:], message, timing
        text = "\n".join(f"{m.type}: {m.content}" for m in history[-10:])
        question = {"type": "choice", "instructions": self.prompts["supervisor"], "options": self.prompts["routes"]}
        item = {"id": "route_" + str(turn), "text": text, "questions": {"route": question}}
        def decide():
            start = time.perf_counter()
            started = datetime.now(timezone.utc).isoformat()
            observer_before = getattr(self.backend.client, "observer_ms", 0)
            answers, details = self.backend.decide(item)
            probabilities = answers["route"]
            if not dm.valid_probs(question, probabilities):
                raise ValueError("Invalid route probability distribution")
            route = max(probabilities, key=probabilities.get)
            return route, {"node": "supervisor", "started": started, "finished": datetime.now(timezone.utc).isoformat(), "total_ms": (time.perf_counter() - start) * 1000, "observer_ms": getattr(self.backend.client, "observer_ms", 0) - observer_before, "ttft_ms": None, "tokens": None, "probabilities": probabilities, "details": details}
        route, timing = self.observe("supervisor", {"decision_item": item}, decide)
        return route, None, timing

    def respond(self, agent, history):
        return self.call(self.messages(agent, history), schemas(agent) + [handoff("transfer_back_to_supervisor")], agent)
