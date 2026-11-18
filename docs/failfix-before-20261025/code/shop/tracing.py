from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import uuid
import warnings


class Tracing:
    def __init__(self, env_path=None):
        path = Path(env_path) if env_path else Path.home() / ".config/shopagent/langsmith.env"
        self.secrets = []
        self.messages = []
        self.client = None
        self.status = {"enabled": path.is_file(), "mode": "explicit execution traces", "project": "shopagent", "runs": [], "errors": self.messages, "warnings": []}
        if not path.is_file():
            os.environ["LANGSMITH_TRACING"] = "false"
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as error:
            self.messages.append(f"{type(error).__name__}: Cannot read LangSmith environment file (contents withheld)")
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                self.messages.append("ValueError: Invalid KEY=VALUE line in LangSmith environment file (contents withheld)")
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if "KEY" in key.upper() or "TOKEN" in key.upper() or "SECRET" in key.upper():
                self.secrets.append(value)
            if key.startswith(("LANGSMITH_", "LANGCHAIN_")):
                os.environ[key] = value
        os.environ["LANGSMITH_TRACING"] = "true"
        for key in ("LANGSMITH_AGENT_ID", "LANGSMITH_AGENT_ENVIRONMENT"):
            os.environ.pop(key, None)
        self.project = os.environ.get("LANGSMITH_PROJECT", "shopagent")
        self.status["project"] = self.project
        parent = self

        class SafeHandler(logging.Handler):
            def emit(self, record):
                parent.messages.append(parent.safe(self.format(record)))

        logger = logging.getLogger("langsmith")
        logger.handlers = [SafeHandler()]
        logger.propagate = False
        try:
            from langsmith import Client
            from urllib3.util.retry import Retry
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.client = Client(auto_batch_tracing=False, timeout_ms=10000, retry_config=Retry(total=0), omit_traced_runtime_info=True)
            self.status["warnings"] = [self.safe(str(w.message)) for w in caught]
        except Exception as error:
            self.messages.append(self.safe(f"{type(error).__name__}: {error}"))

    def safe(self, value):
        text = str(value)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return re.sub(r"(?:lsv2_pt_|lsv2_sk_|ls__)[A-Za-z0-9_-]+", "[REDACTED]", text)

    @contextmanager
    def local(self):
        # Explicit uploads exclude credentials and embedding payloads by construction.
        context = None
        try:
            from langsmith import tracing_context
            context = tracing_context(enabled=False)
            context.__enter__()
        except Exception as error:
            context = None
            self.messages.append(self.safe(f"{type(error).__name__}: {error}"))
        try:
            yield
        finally:
            if context is not None:
                try:
                    context.__exit__(None, None, None)
                except Exception as error:
                    self.messages.append(self.safe(f"{type(error).__name__}: {error}"))

    def upload(self, config, repeat, scenario_id, inputs, outputs, started, finished):
        if self.client is None:
            return
        run_id = uuid.uuid4()
        metadata = {"config": config, "repeat_index": repeat, "scenario_id": scenario_id}
        tags = ["shopagent", config, f"repeat:{repeat}", scenario_id, "stage:3-1" if repeat == 0 else "stage:3-2"]
        status = {"id": str(run_id), "scenario_id": scenario_id, "uploaded": False, "verified": False}
        self.status["runs"].append(status)
        try:
            inputs = json.loads(self.safe(json.dumps(inputs, ensure_ascii=False)))
            outputs = json.loads(self.safe(json.dumps(outputs, ensure_ascii=False)))
            self.client.create_run(id=run_id, name=f"{config}/{scenario_id}", run_type="chain", project_name=self.project, start_time=started, end_time=finished, inputs=inputs, outputs=outputs, extra={"metadata": metadata}, tags=tags)
            status["uploaded"] = True
            for index, turn in enumerate(outputs.get("turns", [])):
                turn_id = uuid.uuid4()
                self.client.create_run(id=turn_id, parent_run_id=run_id, name=f"turn-{index}", run_type="chain", project_name=self.project, start_time=turn.get("started", started), end_time=turn.get("finished", finished), inputs={"user": turn["user"]}, outputs={"reply": turn.get("reply"), "routes": turn.get("routes"), "status": turn["status"]}, extra={"metadata": metadata}, tags=tags)
                for event in turn.get("llm_calls", []) + turn.get("calls", []):
                    kind = "tool" if event.get("kind") in ("tool", "handoff") else "llm"
                    self.client.create_run(id=uuid.uuid4(), parent_run_id=turn_id, name=event.get("node", event.get("name", kind)), run_type=kind, project_name=self.project, start_time=event.get("started", started), end_time=event.get("finished", finished), inputs={"arguments": event.get("arguments", {})} if kind == "tool" else event.get("request", {"node": event.get("node")}), outputs=event, extra={"metadata": metadata}, tags=tags)
            self.client.read_run(run_id)
            status["verified"] = True
        except Exception as error:
            status["error"] = self.safe(f"{type(error).__name__}: {error}")
            self.messages.append(status["error"])

    def finish(self):
        if self.client is not None:
            try:
                self.client.flush(timeout=15)
                self.client.close()
            except Exception as error:
                self.messages.append(self.safe(f"{type(error).__name__}: {error}"))
        self.status["upload_errors"] = bool(self.messages)
