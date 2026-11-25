"""Replay saved v2 consent judgments through the Omni decision classifier."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.legacy.compare_supervisor_omni_vs_gemma import (
    CONFIGS, ROOT, dm, latency_stats, load, make, ms, omni_backend, percent,
    preflight, processes, rate, run_name,
)
from shop.tools import JUDGE_CONSENT


VERDICTS = tuple(JUDGE_CONSENT["function"]["parameters"]["properties"]["verdict"]["enum"])
PROMPT = load(ROOT / "data/prompts.json")["consent_judge"]
OPTIONS = dict(zip(VERDICTS, (
    "제안의 행동과 인자에 동의", "제안을 거절",
    "행동이나 대상 또는 조건의 변경 요청", "동의 여부가 불명확",
)))
CUSTOMER_VERDICTS = {"approve": "yes", "reject": "no", "change": "changed"}
REFERENCE_NOTE = (
    "기대 판정은 저장된 grade.turns의 expected_verdict를 우선 사용한다. "
    "고객 시뮬레이터가 있으면 그 결정에서, 없으면 시나리오의 expect.consent_verdict에서 온 값이다. "
    "저장된 기대 판정이 없으면 simulated_customer.decision을 대응시킨다. "
    "독립적인 사람의 정답이 아니며, 기준이 없으면 정확도 분모에서 제외한다."
)


def consent_payload(request_or_saved_human_json):
    value = request_or_saved_human_json
    if isinstance(value, dict) and "messages" in value:
        humans = [m for m in value["messages"] if m.get("type") == "human"]
        if len(humans) != 1:
            raise ValueError("Expected exactly one saved human message")
        value = humans[0]["content"]
    if isinstance(value, str):
        value = json.loads(value)
    proposal = value["proposal"]
    if (not isinstance(proposal["action"], str) or not isinstance(proposal["arguments"], dict)
            or not isinstance(proposal["message"], str) or not isinstance(value["customer_message"], str)):
        raise ValueError("Invalid saved consent payload")
    return value


def build_consent_item(request_or_saved_human_json, n):
    """Convert saved proposal/reply data without inferring a verdict or changing it."""
    payload = consent_payload(request_or_saved_human_json)
    proposal = payload["proposal"]
    text = (f"제안된 처리: {proposal['action']}\n"
            f"제안된 인자: {json.dumps(proposal['arguments'], ensure_ascii=False, sort_keys=True)}\n"
            f"제안 내용(고객에게 보인 문구): {proposal['message']}\n"
            f"고객 답변: {payload['customer_message']}")
    question = {"type": "choice", "instructions": PROMPT + " 고객 답변에 맞는 선택지 하나를 고르세요.",
                "options": dict(OPTIONS)}
    return {"id": f"consent_{n}", "text": text, "questions": {"verdict": question}}


def expected_reference(grade_turn, turn):
    """Return the reference, saved location and origin; never invent a human label."""
    simulation = turn.get("simulated_customer") or {}
    saved = grade_turn.get("expected_verdict")
    if saved in VERDICTS:
        origin = "simulated_customer.decision" if simulation else "scenario.expect.consent_verdict"
        return saved, "grade.turns.expected_verdict", origin
    fallback = CUSTOMER_VERDICTS.get(simulation.get("decision"))
    return (fallback, "turn.simulated_customer.decision", "simulated_customer.decision") if fallback else (None, None, None)


def collect_decisions(source, dataset):
    gold = {s["id"]: s for s in dataset["scenarios"]}
    entries = []
    for scenario in source["scenarios"]:
        definition = gold.get(scenario["id"], {})
        grades = (scenario.get("grade") or {}).get("turns", [])
        for index, turn in enumerate(scenario.get("turns", [])):
            grade = grades[index] if index < len(grades) else {}
            expected, reference, origin = expected_reference(grade, turn)
            simulation = turn.get("simulated_customer") or {}
            for call in turn.get("llm_calls", []):
                if call.get("node") != "consent_judge":
                    continue
                calls = call.get("tool_calls") or (call.get("output") or {}).get("tool_calls") or []
                arguments = calls[0].get("args", {}) if calls else {}
                verdict = arguments.get("verdict")
                skip = "source_not_complete" if call.get("status") != "complete" else None
                if skip is None and verdict not in VERDICTS:
                    skip = "missing_gemma_verdict"
                payload, input_error = None, None
                try:
                    payload = consent_payload(call["request"])
                except Exception as error:
                    input_error = dm.error_text(error)
                proposal = payload["proposal"] if payload else {}
                row = {
                    "decision_id": f"consent_{len(entries) + 1:04d}",
                    "scenario_id": scenario["id"], "turn": index,
                    "scenario_description": definition.get("gold_rationale") or definition.get("category") or scenario.get("category", ""),
                    "proposal_action": proposal.get("action"), "proposal_arguments": proposal.get("arguments"),
                    "customer_reply": payload["customer_message"] if payload else None,
                    "simulated_customer_decision": simulation.get("decision"),
                    "used_scripted_text": simulation.get("used_scripted_text"),
                    "expected_verdict": expected, "expected_reference": reference, "expected_reference_origin": origin,
                    "grader_judged_verdict": grade.get("judged_verdict"), "grader_judge_correct": grade.get("judge_correct"),
                    "gemma_verdict": verdict, "gemma_reason": arguments.get("reason"),
                    "omni_verdict": None, "omni_probabilities": None, "omni_confidence": None,
                    "agree": None, "gemma_correct": verdict == expected if expected is not None and verdict in VERDICTS else None,
                    "omni_correct": None, "omni_latency_ms": None, "observer_ms": None,
                    "gemma_latency_ms": call.get("total_ms"), "source_status": call.get("status"),
                    "status": "skipped" if skip else "pending", "skip_reason": skip,
                    "error": call.get("error") if skip else input_error,
                }
                if "attempt_index" in scenario:
                    row["attempt_index"] = scenario["attempt_index"]
                entries.append((row, call))
    return entries


def replay_one(row, call, backend):
    start = time.perf_counter()
    observer_before = getattr(backend.client, "observer_ms", None)
    try:
        item = build_consent_item(call["request"], row["decision_id"].removeprefix("consent_"))
        start = time.perf_counter()
        answers, _ = backend.decide(item)
        probabilities = answers["verdict"]
        if not dm.valid_probs(item["questions"]["verdict"], probabilities):
            raise ValueError("Invalid consent probability distribution")
        verdict = max(probabilities, key=probabilities.get)
        row.update(status="complete", error=None, omni_verdict=verdict,
                   omni_probabilities={k: round(float(v), 4) for k, v in probabilities.items()},
                   omni_confidence=float(probabilities[verdict]), agree=verdict == row["gemma_verdict"],
                   omni_correct=verdict == row["expected_verdict"] if row["expected_verdict"] is not None else None)
    except Exception as error:
        row.update(status="error", error=dm.error_text(error))
    finally:
        row["omni_latency_ms"] = (time.perf_counter() - start) * 1000
        observer_after = getattr(backend.client, "observer_ms", None)
        row["observer_ms"] = observer_after - observer_before if observer_before is not None and observer_after is not None else None


def decision_counts(rows):
    eligible = [r for r in rows if r["source_status"] == "complete"]
    return {
        "total_consent_judge_calls": len(rows),
        "skipped_non_complete": sum(r["source_status"] != "complete" for r in rows),
        "expected_reference_coverage": {"numerator": sum(r["expected_verdict"] is not None for r in eligible), "denominator": len(eligible)},
        "expected_reference_origins": dict(Counter(r["expected_reference_origin"] or "missing" for r in eligible)),
        "gemma_verdict_distribution": {v: sum(r["gemma_verdict"] == v for r in eligible) for v in VERDICTS},
        "selected": sum(r["status"] in ("pending", "complete", "error") for r in rows),
        "attempted": sum(r["status"] in ("complete", "error") for r in rows),
        "evaluated": sum(r["status"] == "complete" for r in rows),
        "omni_errors": sum(r["status"] == "error" for r in rows),
        "skip_reasons": dict(Counter(r["skip_reason"] for r in rows if r["status"] == "skipped")),
    }


def summarize(rows, low_confidence=.6):
    attempted = [r for r in rows if r["status"] in ("complete", "error")]
    evaluated = [r for r in rows if r["status"] == "complete"]
    pairs = Counter((r["gemma_verdict"], r["omni_verdict"]) for r in evaluated)
    return {
        "agreement": rate(r["agree"] for r in evaluated),
        "accuracy": {"gemma": rate(r["gemma_correct"] for r in attempted),
                     "omni": rate(r["omni_correct"] for r in evaluated)},
        "confusion_table": [{"gemma_verdict": g, "omni_verdict": o, "count": n} for (g, o), n in sorted(pairs.items())],
        "disagreements": [dict(r) for r in evaluated if r["agree"] is False],
        "omni_expected_misses": [dict(r) for r in evaluated if r["omni_correct"] is False],
        "gemma_expected_misses": [dict(r) for r in attempted if r["gemma_correct"] is False],
        "low_confidence": [dict(r) for r in evaluated if r["omni_confidence"] < low_confidence],
        "omni_latency_ms": latency_stats(r["omni_latency_ms"] for r in evaluated),
        "gemma_latency_ms": latency_stats(r["gemma_latency_ms"] for r in evaluated),
    }


def describe(row):
    def plain(value):
        return str(value).replace("\n", " ")
    attempt = f", 시도 {row['attempt_index']}" if "attempt_index" in row else ""
    confidence = "자료 없음" if row["omni_confidence"] is None else f"{row['omni_confidence']:.4f}"
    return (f"- {row['decision_id']} / {row['scenario_id']}{attempt}, 턴 {row['turn']}: "
            f"{plain(row['scenario_description'])} 고객 답변: “{plain(row['customer_reply'])}” "
            f"Gemma={row['gemma_verdict']}, Omni={row['omni_verdict']}, 최대 확률={confidence}, "
            f"기대 판정={row['expected_verdict']}, 기준 출처={row['expected_reference']} "
            f"({row['expected_reference_origin']}), 고객 결정={row['simulated_customer_decision']}, "
            f"대본 사용={row['used_scripted_text']}, "
            f"Omni 확률={json.dumps(row['omni_probabilities'], ensure_ascii=False)}")


def markdown(result):
    counts, metrics = result["counts"], result["summary"]
    coverage = counts["expected_reference_coverage"]
    lines = ["# 저장된 동의 판정 비교", "",
             f"원본: `{result['source_run']}`. 상태: {result['status']}.",
             "고객에게 보인 제안과 확인 카드, 행동과 인자, 고객 답변을 고정해 Omni로 다시 판단했다. 실제 행동은 실행하지 않았다.",
             f"전체 {counts['total_consent_judge_calls']}건, 원본 미완료 제외 {counts['skipped_non_complete']}건, "
             f"Omni 시도 {counts['attempted']}건, 성공 {counts['evaluated']}건, 오류 {counts['omni_errors']}건.",
             f"원본 완료 호출의 기대 판정 기준: {coverage['numerator']}/{coverage['denominator']}. 턴 번호는 0부터 센다.",
             REFERENCE_NOTE,
             f"기준 종류별 건수: {json.dumps(counts['expected_reference_origins'], ensure_ascii=False)}.",
             f"저장된 Gemma 판정 분포: {json.dumps(counts['gemma_verdict_distribution'], ensure_ascii=False)}.", "",
             "| 지표 | 값 |", "| --- | --- |",
             f"| 일치율 | {percent(metrics['agreement'])} |",
             f"| Gemma 정확도 | {percent(metrics['accuracy']['gemma'])} |",
             f"| Omni 정확도 | {percent(metrics['accuracy']['omni'])} |"]
    for key, label in (("mean", "평균"), ("median", "중앙값"), ("p95", "95백분위"), ("min", "최소"), ("max", "최대")):
        lines.append(f"| Omni 지연 {label} (성공 {metrics['omni_latency_ms']['n']}건) | {ms(metrics['omni_latency_ms'][key])} |")
    lines += [f"| Gemma 지연 중앙값 (같은 성공 표본) | {ms(metrics['gemma_latency_ms']['median'])} |", "",
              "## Gemma와 Omni 판정별 건수", "", "| Gemma | Omni | 건수 |", "| --- | --- | --- |"]
    lines += [f"| {r['gemma_verdict']} | {r['omni_verdict']} | {r['count']} |" for r in metrics["confusion_table"]]
    for title, key in (("판정이 다른 모든 사례", "disagreements"),
                       ("Omni와 기대 판정이 다른 사례", "omni_expected_misses"),
                       ("Gemma와 기대 판정이 다른 사례", "gemma_expected_misses"),
                       (f"확률이 낮은 사례 (최대 확률 < {result['low_confidence_threshold']})", "low_confidence")):
        lines += ["", "## " + title, ""]
        lines += [describe(row) for row in metrics[key]] or ["해당 사례 없음."]
    lines += ["", "## 해석할 때 주의할 점", "",
              "- " + REFERENCE_NOTE,
              "- Omni는 제안과 확인 카드 및 고객 답변을 하나의 텍스트로 본다. 도구 호출 형식은 전달하지 않는다. 현재 data/prompts.json의 consent_judge와 같은 뜻의 네 선택지를 사용한다. 번호 목록에는 판정 코드와 설명이 함께 표시되고 결과 키는 원래 코드다.",
              "- 동의 판정은 기본 원본에서 22건으로 적어 한 건 차이에도 비율이 크게 바뀐다. --limit은 표본을 더 줄인다. 전체 상담 성공률이나 독립적인 사람 평가로 해석할 수 없다.",
              "- 원본 미완료와 --limit 제외는 비교 분모에서 뺀다. Omni 오류는 Omni 정확도와 일치율에서 빼고 Gemma 정확도에는 시도한 원본을 포함한다. 오류와 제외 사유는 results.json의 각 판정에 남긴다.",
              "- 문맥 한도는 2048이다. 개별 오류는 기록하고 계속한다. 지연은 성공한 backend.decide 전체 시간이며 observer_ms는 제공될 때 별도 기록한다. 예열은 하지 않으며 저장된 Gemma 지연과 실행 환경이 달라 속도 비교에는 한계가 있다.",
              "- 임베딩과 전체 요청 메시지는 저장하지 않는다. 판정 이후의 대화와 행동은 재현하지 않는다."]
    if result.get("error"):
        lines += ["", "실행 오류: " + result["error"]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=run_name, default="full-v2-item-names-domain-prefetch-decide-off")
    parser.add_argument("--out-name", type=run_name, default="compare-consent-judge-omni-vs-gemma-item-names-off")
    parser.add_argument("--port", type=int, default=18380)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--low-confidence", type=float, default=.6)
    parser.add_argument("--load-timeout", type=float, default=300)
    parser.add_argument("--dry-run", action="store_true", help="No server, GPU or writes; print counts and two constructed texts")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.load_timeout <= 0 or not 0 <= args.low_confidence <= 1:
        parser.error("Invalid port, timeout or probability threshold")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    source = load(ROOT / "runs" / args.source_run / "results.json")
    entries = collect_decisions(source, load(ROOT / "data/scenarios.json"))
    selected = [entry for entry in entries if entry[0]["status"] == "pending"]
    if args.limit is not None:
        for row, _ in selected[args.limit:]:
            row.update(status="skipped", skip_reason="limit")
        selected = selected[:args.limit]
    rows = [row for row, _ in entries]
    if args.dry_run:
        print(json.dumps(decision_counts(rows), ensure_ascii=False, indent=2))
        print(REFERENCE_NOTE)
        for index, (row, call) in enumerate(selected):
            try:
                item = build_consent_item(call["request"], index + 1)
                if index < 2:
                    print(f"\n{row['decision_id']} / {row['scenario_id']} / turn {row['turn']}\n{item['text']}")
            except Exception as error:
                print(f"{row['decision_id']}: {dm.error_text(error)}")
        return 0

    config = CONFIGS["jev-g12"]
    model = dm.MODELS / config["decision"]
    head = model.parent / "decision-head-f32.npz"
    # Wait and reject unsafe startup before creating any output directory.
    checks, errors = preflight(args.port, model, head, port_wait=60)
    if errors:
        print("Preflight failed; no output folder created: " + "; ".join(errors))
        return 1
    folder = ROOT / "runs" / args.out_name
    folder.mkdir(exist_ok=False)
    now = lambda: datetime.now(timezone.utc).isoformat()
    result = {
        "source_run": args.source_run, "data_version": source.get("data_version"),
        "dataset_sha256": source.get("dataset_sha256"), "prompts_sha256": source.get("prompts_sha256"),
        "current_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                           for name in ("data/scenarios.json", "data/prompts.json")},
        "model_paths": {"decision": str(model), "head": str(head)},
        "started": now(), "finished": None, "status": "running", "server_command": None,
        "preflight": checks, "preflight_errors": errors, "expected_reference_note": REFERENCE_NOTE,
        "low_confidence_threshold": args.low_confidence, "limit": args.limit, "decisions": rows,
    }
    exit_code = 0
    try:
        server = make("jev-g12", "decision", 99, folder, args.port, args.load_timeout)
        result.update(server=server.info, server_command=server.info["command"])
        with omni_backend(server) as backend:
            for row, call in selected:
                replay_one(row, call, backend)
                print(f"{row['decision_id']}: {row['status']} {row['gemma_verdict']} -> {row['omni_verdict']}", flush=True)
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
