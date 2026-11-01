"""Offline Solar Decide request shaping and remote-call guards."""

import json
import os
from pathlib import Path


DEFAULT_DECIDE_MODEL = "solar-decide"
DEFAULT_BASE_URL = "https://api.upstage.ai"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def build_route_item(text, turn, prompts) -> dict:
    question = {"type": "choice", "instructions": prompts["supervisor"], "options": prompts["routes"]}
    return {"id": "route_" + str(turn), "text": text, "questions": {"route": question}}


def dump_decide_payloads(scenario_ids=None, dataset_path=DATA_DIR / "scenarios.json", prompts_path=DATA_DIR / "prompts.json") -> list:
    """Build offline payloads using user turns only; later turns omit unknown assistant replies."""
    dataset = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    prompts = json.loads(Path(prompts_path).read_text(encoding="utf-8"))
    scenarios = {scenario["id"]: scenario for scenario in dataset["scenarios"]}
    ids = dataset["smoke_ids"] if scenario_ids is None else scenario_ids
    records = []
    for scenario_id in ids:
        if scenario_id not in scenarios:
            raise ValueError(f"Unknown scenario ID: {scenario_id}")
        history = []
        for turn_index, turn in enumerate(scenarios[scenario_id]["turns"]):
            history.append("human: " + turn["user"])
            item = build_route_item("\n".join(history[-10:]), turn_index, prompts)
            records.append({"scenario_id": scenario_id, "turn_index": turn_index, "item": item, "body": build_decide_body(item)})
    return records


def build_decide_body(item, model=None) -> dict:
    if "text" not in item or item["text"] is None:
        raise ValueError("Decide item is missing text")
    if not isinstance(item.get("questions"), dict) or not item["questions"]:
        raise ValueError("Decide item is missing questions")
    questions = {}
    for key, question in item["questions"].items():
        kind = question.get("type")
        if kind not in ("choice", "score", "noul"):
            raise ValueError(f"Unsupported Decide question type: {kind}")
        native = {"type": kind, "instructions": question["instructions"]}
        if kind == "choice":
            native["criteria"] = dict(question["options"])
        elif kind == "score":
            native["criteria"] = list(question["levels"].values())
        questions[key] = native
    return {"model": model or DEFAULT_DECIDE_MODEL, "state": item["text"], "questions": questions}


def get_upstage_api_key(environ=None) -> str | None:
    environ = os.environ if environ is None else environ
    return environ.get("UPSTAGE_API_KEY", "").strip() or None


def require_upstage_api_key(environ=None) -> str:
    key = get_upstage_api_key(environ)
    if key is None:
        raise RuntimeError("Remote Upstage calls are refused because UPSTAGE_API_KEY is missing/empty")
    return key


def assert_upstage_remote_allowed(environ=None) -> None:
    require_upstage_api_key(environ)
    environ = os.environ if environ is None else environ
    if environ.get("SHOPAGENT_UPSTAGE_ALLOW_HTTP") != "1":
        raise RuntimeError("Live Upstage HTTP is not enabled (dry-run/default)")


def post_systemone(body, environ=None) -> None:
    assert_upstage_remote_allowed(environ)
    raise RuntimeError("Upstage HTTP client not implemented")
