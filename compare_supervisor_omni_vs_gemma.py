"""Replay saved supervisor calls through the existing Omni decision path."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import statistics
import time

from langchain_core.messages import messages_from_dict

from shop.agents import Agents
from shop.servers import CONFIGS, compute_apps, make, processes
from shop.shared import ROOT, dm, load, ra, runtime


def history_from_request(request):
    """Saved model_dump dictionaries need the LangChain serialization envelope."""
    saved = request["messages"]
    if not saved or saved[0].get("type") != "system":
        raise ValueError("Expected the saved supervisor's first system message")
    return messages_from_dict([{"type": m["type"], "data": m} for m in saved[1:]])


def collect_decisions(source, dataset):
    """Return (public record, source call) pairs; source requests stay in memory."""
    scenarios = {s["id"]: s for s in dataset["scenarios"]}
    decisions = []
    for scenario in source["scenarios"]:
        gold = scenarios.get(scenario["id"], {})
        for turn_index, turn in enumerate(scenario.get("turns", [])):
            gold_turns = gold.get("turns", [])
            expected = (gold_turns[turn_index].get("expect", {}).get("route")
                        if turn_index < len(gold_turns) else None)
            expected = list(expected) if isinstance(expected, list) and expected else None
            position = 0
            for call in turn.get("llm_calls", []):
                if call.get("node") not in ("router", "supervisor"):
                    continue
                calls = call.get("tool_calls") or []
                name = calls[0].get("name", "") if calls else ""
                gemma = name.removeprefix("transfer_to_") if name.startswith("transfer_to_") else None
                skip = "source_not_complete" if call.get("status") != "complete" else None
                if skip is None and not gemma:
                    skip = "missing_gemma_route"
                record = {
                    "decision_id": f"decision_{len(decisions) + 1:04d}",
                    "scenario_id": scenario["id"], "turn": turn_index,
                    "position_in_turn": "initial" if position == 0 else "reroute",
                    "customer_line": turn.get("user") or turn.get("scripted_user", ""),
                    "scenario_description": gold.get("gold_rationale") or gold.get("category") or scenario.get("category", ""),
                    "expected_routes": expected, "gemma_route": gemma, "omni_route": None,
                    "omni_probabilities": None, "omni_confidence": None, "agree": None,
                    "gemma_correct": gemma in expected if expected is not None and gemma else None,
                    "omni_correct": None, "omni_latency_ms": None, "observer_ms": None,
                    "gemma_latency_ms": call.get("total_ms"), "prompt_tokens": None,
                    "source_status": call.get("status"), "status": "skipped" if skip else "pending",
                    "skip_reason": skip, "error": call.get("error") if skip else None,
                }
                if "attempt_index" in scenario:
                    record["attempt_index"] = scenario["attempt_index"]
                decisions.append((record, call))
                position += 1
    return decisions


def rate(values):
    values = [v for v in values if v is not None]
    return {"numerator": sum(values), "denominator": len(values),
            "rate": sum(values) / len(values) if values else None}


def latency_stats(values):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return dict.fromkeys(("mean", "median", "p95", "min", "max"), None) | {"n": 0}
    index = (len(values) - 1) * .95
    lo = int(index)
    p95 = values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo)
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "p95": p95, "min": values[0], "max": values[-1]}


def example(row):
    keys = ("decision_id", "scenario_id", "attempt_index", "turn", "position_in_turn",
            "scenario_description", "customer_line", "expected_routes", "gemma_route", "omni_route",
            "omni_probabilities", "omni_confidence", "gemma_correct", "omni_correct")
    return {k: row[k] for k in keys if k in row}


def summarize(rows, low_confidence=.6):
    attempted = [r for r in rows if r["status"] in ("complete", "error")]
    evaluated = [r for r in rows if r["status"] == "complete"]
    accuracy = {}
    for label in ("overall", "initial", "reroute"):
        group = [r for r in attempted if label == "overall" or r["position_in_turn"] == label]
        accuracy[label] = {
            "gemma": rate(r["gemma_correct"] for r in group),
            "omni": rate(r["omni_correct"] for r in group if r["status"] == "complete"),
        }
    pairs = Counter((r["gemma_route"], r["omni_route"]) for r in evaluated)
    return {
        "agreement": rate(r["agree"] for r in evaluated), "accuracy": accuracy,
        "confusion_table": [{"gemma_route": g, "omni_route": o, "count": n}
                            for (g, o), n in sorted(pairs.items())],
        "disagreements": [example(r) for r in evaluated if not r["agree"]],
        "omni_expected_misses": [example(r) for r in evaluated if r["omni_correct"] is False],
        "low_confidence": [example(r) for r in evaluated if r["omni_confidence"] < low_confidence],
        "omni_latency_ms": latency_stats(r["omni_latency_ms"] for r in evaluated),
        "gemma_latency_ms": latency_stats(r["gemma_latency_ms"] for r in evaluated),
    }


def decision_counts(rows):
    eligible = [r for r in rows if r["source_status"] == "complete"]
    return {
        "total_supervisor_calls": len(rows),
        "skipped_non_complete": sum(r["source_status"] != "complete" for r in rows),
        "initial": sum(r["position_in_turn"] == "initial" for r in eligible),
        "reroute": sum(r["position_in_turn"] == "reroute" for r in eligible),
        "expected_route_coverage": {"numerator": sum(r["expected_routes"] is not None for r in eligible),
                                    "denominator": len(eligible)},
        "selected": sum(r["status"] in ("pending", "complete", "error") for r in rows),
        "attempted": sum(r["status"] in ("complete", "error") for r in rows),
        "evaluated": sum(r["status"] == "complete" for r in rows),
        "omni_errors": sum(r["status"] == "error" for r in rows),
        "skip_reasons": dict(Counter(r["skip_reason"] for r in rows if r["status"] == "skipped")),
    }


def preflight(port, model, head, port_wait=0):
    checks, errors = {}, []
    checks["llama_server_before"] = processes()
    if checks["llama_server_before"]:
        errors.append("Existing llama-server processes; refusing to start")
    deadline = time.monotonic() + (port_wait if not checks["llama_server_before"] else 0)
    while True:
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            checks["port_available"] = True
            break
        except OSError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                checks["port_available"] = False
                errors.append(f"Port {port} unavailable: {error}")
                break
            time.sleep(min(2, remaining))
    try:
        checks["compute_apps"] = compute_apps()
        if checks["compute_apps"]:
            errors.append("Outside GPU compute processes: " + str(checks["compute_apps"]))
    except Exception as error:
        checks["compute_apps_error"] = dm.error_text(error)
        errors.append("Cannot inspect GPU compute processes: " + dm.error_text(error))
    for path in (model, head, runtime.SERVER):
        checks[str(path)] = path.is_file()
        if not path.is_file():
            errors.append("Missing file: " + str(path))
    return checks, errors


@contextmanager
def omni_backend(server):
    """Own only the Omni decision server and always stop it on exit."""
    config = CONFIGS["jev-g12"]
    model = dm.MODELS / config["decision"]
    with dm.stop_on_signal(), server:
        client = ra.Client(server.port, 180)
        yield dm.Backend(config["backend"], client, model, pplx_limit=config["ctx"])


def percent(metric):
    value = "자료 없음" if metric["rate"] is None else f"{metric['rate']:.1%}"
    return f"{value} ({metric['numerator']}/{metric['denominator']})"


def ms(value):
    return "자료 없음" if value is None else f"{value:.1f} ms"


def replay_one(row, call, agents):
    start = time.perf_counter()
    observer_before = getattr(agents.backend.client, "observer_ms", 0)
    try:
        history = history_from_request(call["request"])
        start = time.perf_counter()
        route, _, timing = agents.supervise(history, row["turn"], v2=True)
        probabilities = timing["probabilities"]
        details = timing.get("details") or {}
        tokens = timing.get("tokens") or {}
        row.update(status="complete", omni_route=route,
                   omni_probabilities={k: round(float(p), 4) for k, p in probabilities.items()},
                   omni_confidence=float(max(probabilities.values())),
                   agree=route == row["gemma_route"],
                   omni_correct=route in row["expected_routes"] if row["expected_routes"] is not None else None,
                   omni_latency_ms=timing["total_ms"], observer_ms=timing.get("observer_ms"),
                   prompt_tokens=timing.get("prompt_tokens", details.get("prompt_tokens", tokens.get("input_tokens"))))
    except Exception as error:
        row.update(status="error", error=dm.error_text(error),
                   omni_latency_ms=(time.perf_counter() - start) * 1000,
                   observer_ms=getattr(agents.backend.client, "observer_ms", 0) - observer_before)


def markdown(result):
    metrics, counts = result["summary"], result["counts"]

    lines = ["# 저장된 supervisor 선택 비교", "",
             f"원본: `{result['source_run']}`. 상태: {result['status']}.",
             "Gemma가 받은 저장 이력을 Omni로 다시 판단했다. 새로운 대화나 담당자 답변은 생성하지 않았다.",
             f"전체 {counts['total_supervisor_calls']}건, 원본 미완료 제외 {counts['skipped_non_complete']}건, "
             f"Omni 시도 {counts['attempted']}건, 성공 {counts['evaluated']}건, 오류 {counts['omni_errors']}건.",
             f"기대 경로가 있는 원본 완료 호출: {counts['expected_route_coverage']['numerator']}/"
             f"{counts['expected_route_coverage']['denominator']}. 턴 번호는 0부터 센다.", "",
             "| 지표 | 값 (맞은 수/분모) |", "| --- | --- |",
             f"| 일치율 | {percent(metrics['agreement'])} |"]
    for label, korean in (("overall", "전체"), ("initial", "초기"), ("reroute", "재라우팅")):
        for model in ("gemma", "omni"):
            lines.append(f"| {model} 정확도 · {korean} | {percent(metrics['accuracy'][label][model])} |")
    for key, korean in (("mean", "평균"), ("median", "중앙값"), ("p95", "95백분위"), ("min", "최소"), ("max", "최대")):
        lines.append(f"| Omni 지연 {korean} (성공 {metrics['omni_latency_ms']['n']}건) | {ms(metrics['omni_latency_ms'][key])} |")
    lines += [f"| Gemma 지연 중앙값 (같은 성공 표본) | {ms(metrics['gemma_latency_ms']['median'])} |", "",
              "## 선택이 달랐던 예", "", "기대 경로 판정이 다른 사례와 확률이 높은 사례를 먼저 표시한다.", ""]

    def describe(row):
        customer = str(row["customer_line"]).replace("\n", " ")
        description = str(row["scenario_description"]).replace("\n", " ")
        attempt = f", 시도 {row['attempt_index']}" if "attempt_index" in row else ""
        return (f"- {row['decision_id']} / {row['scenario_id']}{attempt}, 턴 {row['turn']} "
                f"({row['position_in_turn']}): {description} 고객: “{customer}” "
                f"Gemma={row['gemma_route']}, Omni={row['omni_route']}, "
                f"최대 확률={row['omni_confidence']:.4f}, 기대 경로={row['expected_routes']}, "
                f"Omni 확률={json.dumps(row['omni_probabilities'], ensure_ascii=False)}")

    disagreements = sorted(metrics["disagreements"], key=lambda r: (
        r["gemma_correct"] != r["omni_correct"], r["omni_confidence"]), reverse=True)
    lines += [describe(r) for r in disagreements[:10]] or ["해당 사례 없음."]
    lines += ["", f"## 확률이 낮은 선택 (최대 확률 < {result['low_confidence_threshold']})", ""]
    lines += [describe(r) for r in metrics["low_confidence"]] or ["해당 사례 없음."]
    lines += ["", "## 해석할 때 주의할 점", "",
              "- expect.route는 턴 전체에 정의된다. 채점의 route_correct는 턴의 마지막 경로, initial_route_correct는 첫 경로와 비교한다. 여기서는 각 supervisor 선택을 비교하므로 재라우팅이 기대 경로와 달라도 타당할 수 있다.",
              "- Omni는 첫 시스템 메시지를 빼고 마지막 10개 메시지의 종류와 content만 텍스트로 본다. 도구 호출 형식과 저장된 시스템 프롬프트 전체는 전달되지 않는다. 현재 data/prompts.json의 v2_router와 routes를 사용한다.",
              "- 문맥 한도는 2048이다. 한도 초과 등 개별 오류는 기록하고 계속한다. 기대 경로가 없으면 정확도 분모에서 제외한다. Omni 오류는 Omni 정확도와 일치율 분모에서 제외하고, Gemma 정확도에는 시도한 원본 선택을 포함한다.",
              "- --limit 제외 및 원본 미완료 호출은 비교 분모에서 제외한다. 지연은 성공한 호출의 backend 판단 전체 시간(HTTP와 관측 포함)이다. observer_ms는 별도 기록한다. 예열은 하지 않는다. 저장된 Gemma 지연은 실행 환경이 달라 직접적인 속도 비교에는 한계가 있다.",
              "- 현재 Jev 백엔드는 토큰 수를 노출하지 않아 prompt_tokens가 null일 수 있다. 임베딩과 전체 요청 메시지는 저장하지 않는다.",
              "- Gemma가 만든 이력을 고정해 다시 판단한 결과다. Omni 선택 이후의 대화와 도구 실행은 재현하지 않으므로 전체 상담 성공 점수가 아니다."]
    if result.get("error"):
        lines += ["", "실행 오류: " + result["error"]]
    return "\n".join(lines) + "\n"


def run_name(value):
    if not value or value in (".", "..") or "/" in value or "\\" in value or Path(value).name != value:
        raise argparse.ArgumentTypeError("Use a single run folder name")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=run_name, default="full-v2-item-names-domain-prefetch-decide-off")
    parser.add_argument("--out-name", type=run_name, default="compare-supervisor-omni-vs-gemma-item-names-off")
    parser.add_argument("--port", type=int, default=18380)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--low-confidence", type=float, default=.6)
    parser.add_argument("--load-timeout", type=float, default=300)
    parser.add_argument("--dry-run", action="store_true", help="No server/backend or writes; print counts and two texts")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.load_timeout <= 0 or not 0 <= args.low_confidence <= 1:
        parser.error("Invalid port, timeout or probability threshold")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    source_path = ROOT / "runs" / args.source_run / "results.json"
    source, dataset = load(source_path), load(ROOT / "data/scenarios.json")
    entries = collect_decisions(source, dataset)
    selected = [entry for entry in entries if entry[0]["status"] == "pending"]
    for row, _ in selected[args.limit:]:
        if args.limit is not None:
            row.update(status="skipped", skip_reason="limit")
    selected = selected[:args.limit]
    rows = [row for row, _ in entries]
    if args.dry_run:
        for _, call in selected:
            history_from_request(call["request"])
        print(json.dumps(decision_counts(rows), ensure_ascii=False, indent=2))
        samples = selected[:1]
        samples += next(([entry] for entry in selected[1:] if entry[0]["position_in_turn"] == "reroute"), selected[1:2])
        for row, call in samples:
            print(f"\n{row['decision_id']} / {row['scenario_id']} / turn {row['turn']} / {row['position_in_turn']}")
            print("\n".join(f"{m.type}: {m.content}" for m in history_from_request(call["request"])[-10:]))
        return 0
    folder = ROOT / "runs" / args.out_name
    folder.mkdir(exist_ok=False)
    config = CONFIGS["jev-g12"]
    model = dm.MODELS / config["decision"]
    head = model.parent / "decision-head-f32.npz"
    now = lambda: datetime.now(timezone.utc).isoformat()
    result = {"source_run": args.source_run, "data_version": source.get("data_version"),
              "dataset_sha256": source.get("dataset_sha256"), "prompts_sha256": source.get("prompts_sha256"),
              "current_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                 for name in ("data/scenarios.json", "data/prompts.json")},
              "started": now(), "finished": None, "model_paths": {"decision": str(model), "head": str(head)},
              "low_confidence_threshold": args.low_confidence, "limit": args.limit,
              "status": "running", "decisions": rows, "server_command": None}
    exit_code = 0
    try:
        server = make("jev-g12", "decision", 99, folder, args.port, args.load_timeout)
        result["server"] = server.info
        result["server_command"] = server.info["command"]
        result["preflight"], result["preflight_errors"] = preflight(args.port, model, head)
        if result["preflight_errors"]:
            raise RuntimeError("; ".join(result["preflight_errors"]))
        with omni_backend(server) as backend:
            agents = Agents(None, "C01", backend)
            for row, call in selected:
                replay_one(row, call, agents)
                print(f"{row['decision_id']}: {row['status']} {row['gemma_route']} -> {row['omni_route']}", flush=True)
        result["status"] = "complete"
    except (Exception, KeyboardInterrupt) as error:
        result.update(status="error", error=dm.error_text(error))
        exit_code = 1
    finally:
        for row in rows:
            if row["status"] == "pending":
                row.update(status="skipped", skip_reason="run_aborted")
        try:
            result["llama_server_after"] = processes()
        except Exception as error:
            result.update(llama_server_after=None, llama_server_after_error=dm.error_text(error))
        result.update(finished=now(), counts=decision_counts(rows), summary=summarize(rows, args.low_confidence))
        (folder / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / "summary.md").write_text(markdown(result), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
