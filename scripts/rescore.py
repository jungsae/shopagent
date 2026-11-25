"""Offline rescoring: archive outputs by default, or stdout only with --v2-report."""

from collections import defaultdict
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


RUNS = ("g12-native-r1", "g12-native-r2", "g12-native-r3", "clef-g12-r1", "pplx-g12-r1", "jev-g12-r1", "g12-native-ngl0-r1")
RULES = (
    "(a) 날짜: YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD, YYYY년 M월 D일, M월 D일, M/D, M.D, M-D를 달력 검증 후 ISO 날짜로 통일한다. 전각 문자는 NFKC로 변환한다. 연도\xb7월 표기가 없는 숫자는 별점/평점/점수/rating/score 문맥이나 점\xb7원\xb7만\xb7천\xb7백\xb7십\xb7%\xb7퍼센트 단위가 붙으면 날짜로 해석하지 않는다.",
    "(a) 명시된 연도를 보존한다. 생략된 연도는 scenario.base_year, scenario.reference_date, 데이터셋 기준일(2026-10-22) 순으로 정한다. 월/일에 따른 연도 넘김 추측은 하지 않는다.",
    "(a) 같은 날짜 정규화를 사용자/도구 근거, 응답의 hallucinated_value, reply_must_include, reply_must_not_include에 적용한다.",
    "(b) 동의어 표는 품절 / 재고없음 / 재고가없 / 재고없 및 불가 / 불가능 / 할수없 두 그룹이다. 공백과 활용 어미를 고려하되, 확인된 부정\xb7예정\xb7임박\xb7불가피 표현은 동의어로 바꾸지 않는다. 재고 부족, 입고 예정, 어렵다, 권장하지 않는다 등은 추가하지 않는다.",
    "(b) 원화 금액: 쉼표\xb7공백\xb7전각 숫자를 정규화하고, 숫자와 내림차순 만(10000)\xb7천(1000)\xb7백(100)\xb7십(10) 단위를 정수 원화로 계산한다. 52,000원 = 52000원 = 5만 2천원 = 5만2000원. 원 단위 없는 숫자, 다른 통화, 뒤집힌 단위, 단위 범위를 넘는 잔여 숫자는 동치 처리하지 않는다.",
    "(b) 금액 정규화는 응답 조건과 grounding 양쪽에 적용한다. 날짜\xb7금액 경계를 보존하여 52000원이 152000원 내부에서 일치하지 않게 한다.",
    "(c) 미래 날짜를 계산만으로 허용하지 않는다. 현재 턴까지의 사용자 발화나 실제 도구 인자/출력에 해당 날짜가 있어야 한다. 도구 인자는 날짜만 근거로 추가하며 주문 ID\xb7금액에는 확대하지 않는다. 기준일과 공통 프롬프트의 기존 근거 인정은 유지한다.",
    "동의어 표에 해당하지 않는 조건은 기존 부분 문자열 매칭을 유지한다. 기존 NFKC\xb7공백 제거\xb7쉼표 제거\xb7대소문자 정규화 및 성공 게이트는 유지한다. 필수 호출\xb7라우팅 등 진단 지표를 새로운 성공 게이트로 추가하지 않는다.",
)


def offline(read_only=False):
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    for key in list(os.environ):
        if "API_KEY" in key.upper() or "APIKEY" in key.upper():
            os.environ.pop(key, None)
    sys.dont_write_bytecode = True
    secret = Path.home() / ".config/shopagent/langsmith.env"

    def audit(event, args):
        if event in ("socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
            raise RuntimeError("Offline rescoring blocks network and process execution")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)) and Path(os.fsdecode(args[0])).absolute() == secret:
            raise RuntimeError("Credential file access is forbidden")
        if read_only:
            if event == "open":
                mode, flags = args[1:3]
                if (mode and any(c in mode for c in "wax+")) or (flags and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                    raise RuntimeError("Read-only rescoring forbids file writes")
            if event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink", "os.truncate", "os.chmod", "os.chown", "os.utime"):
                raise RuntimeError("Read-only rescoring forbids filesystem changes")

    sys.addaudithook(audit)


def apply_changes(db, changes):
    for path, delta in changes.items():
        keys = path.split(".")
        target = db
        for key in keys[:-1]:
            target = target[key]
        if target.get(keys[-1]) != delta["before"]:
            raise ValueError("Saved diff precondition mismatch: " + path)
        target[keys[-1]] = copy.deepcopy(delta["after"])


class FinalStateReplayError(ValueError):
    """Trace turns are usable, but the exact final DB cannot be reconstructed."""


def trace_path(folder, row):
    attempt = row.get("attempt_index", 1)
    stem = row["id"] if attempt == 1 else f"{row['id']}-attempt-{attempt}"
    return folder / "traces" / (stem + ".jsonl")


def restore_shop(folder, row, scenario, base):
    from shop.tools import Shop

    shop = Shop(base, scenario["customer_id"], scenario.get("db_patch"), scenario.get("faults"), reference_date=scenario.get("reference_date"))
    events = [json.loads(line) for line in trace_path(folder, row).read_text(encoding="utf-8").splitlines()]
    turns = [e["result"] for e in events if e["event"] == "turn_end"]
    if len(turns) != len(row["turns"]):
        raise ValueError("Trace/results turn count mismatch: " + row["id"])
    for traced, saved in zip(turns, row["turns"]):
        for field in ("user", "reply", "visible", "calls", "routes", "status", "loop_limit", "consent_mode", "pending", "pending_at_start", "pending_events", "consent_judgment", "action_decision", "approval_state", "llm_calls", "policy_context", "confirmation_card", "simulated_customer", "scripted_user"):
            if traced.get(field) != saved.get(field):
                raise ValueError("Trace/results mismatch: " + row["id"] + "/" + field)
    try:
        return replay_final_state(shop, row, events)
    except (ValueError, KeyError, TypeError) as error:
        raise FinalStateReplayError(str(error)) from error


def replay_final_state(shop, row, events):
    from shop.shared import leaves

    for saved in row["turns"]:
        for call in saved.get("calls", []):
            if saved.get("consent_mode") not in ("proposal", "tool_interrupt") or call.get("kind") == "tool" and not call.get("scope_error"):
                apply_changes(shop.db, call.get("changes", {}))
    diffs = [e["changes"] for e in events if e["event"] == "db_diff"]
    if len(diffs) != 1:
        raise ValueError("Missing or duplicate final DB diff: " + row["id"])
    before, after = leaves(shop.initial), leaves(shop.db)
    observed = {key: {"before": before.get(key), "after": after.get(key)} for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    for path, delta in observed.items():
        if diffs[0].get(path) != delta:
            raise ValueError("Tool changes/final DB diff conflict: " + row["id"])
    shop.rescore_replay_notes = sorted(set(diffs[0]) - set(observed))
    # Mutable lists can be absent from per-call diffs; final db_diff is authoritative.
    shop.db = copy.deepcopy(shop.initial)
    apply_changes(shop.db, diffs[0])
    return shop


def matching_rules(text, term, reference_date, base_year):
    from shop.grader import normalize_amounts, normalize_dates, normalized, reply_contains

    rules = set()
    dates = [normalize_dates(v, reference_date, base_year) for v in (text, term)]
    if any(normalized(v) != normalized(w) for v, w in zip(dates, (text, term))):
        rules.add("a")
    amounts = [normalize_amounts(v) for v in (text, term)]
    if any(normalized(v) != normalized(w) for v, w in zip(amounts, (text, term))):
        rules.add("b:amount")
    if reply_contains(text, term, reference_date, base_year) != reply_contains(text, term, reference_date, base_year, equivalences=False):
        rules.add("b:equivalence")
    return rules


def audit_changes(scenario, row, old, new):
    from shop.grader import normalize_amounts, normalized, output_values, reply_contains, value_tokens
    from shop.shared import REFERENCE_DATE, ROOT, load

    reference_date = scenario.get("reference_date", REFERENCE_DATE)
    base_year = scenario.get("base_year")
    details, unknown = [], []
    allowed = {"success", "turns", "violations", "proposal_missing_when_expected", "plain_text_confirmation_without_proposal"}
    for key in old.keys() | new.keys():
        if key not in allowed and old.get(key) != new.get(key):
            unknown.append("scenario field: " + key)
    for key in old["violations"].keys() | new["violations"].keys():
        if key not in ("hallucinated_value", "no_proposal_before_consent") and old["violations"].get(key) != new["violations"].get(key):
            unknown.append("violation: " + key)
    calls = []
    for index, (before, after, turn) in enumerate(zip(old["turns"], new["turns"], row["turns"])):
        calls.extend(turn.get("calls", []))
        allowed_turn = {"include_missing", "excluded_found", "hallucinated_values", "violations", "proposal_missing_when_expected", "plain_text_confirmation_without_proposal"}
        for key in before.keys() | after.keys():
            if key not in allowed_turn and before.get(key) != after.get(key):
                unknown.append(f"turn {index + 1}: {key}")
        for key in before["violations"].keys() | after["violations"].keys():
            if key not in ("hallucinated_value", "no_proposal_before_consent") and before["violations"].get(key) != after["violations"].get(key):
                unknown.append(f"turn {index + 1} violation: {key}")
        for field in ("include_missing", "excluded_found"):
            text = turn.get("reply", "") if field == "include_missing" else "\n".join(turn.get("visible", [turn.get("reply", "")]))
            for term in sorted(set(before[field]) ^ set(after[field])):
                rules = matching_rules(text, term, reference_date, base_year)
                contained = reply_contains(text, term, reference_date, base_year)
                expected_present = not contained if field == "include_missing" else contained
                if not rules or (term in after[field]) != expected_present:
                    unknown.append(f"turn {index + 1}: {field} {term}")
                details.append({"turn": index + 1, "field": field, "value": term, "change": "removed" if term in before[field] else "added", "rules": sorted(rules)})
        evidence = "\n".join(t["user"] for t in scenario["turns"][:index + 1])
        evidence += "\n" + load(ROOT / "data/prompts.json")["common"].format(customer_id=scenario["customer_id"], reference_date=reference_date)
        known = value_tokens(evidence, reference_date, base_year)
        arg_dates = set()
        for call in calls:
            if call.get("kind") != "tool" or call.get("scope_error"):
                continue
            known |= output_values(call.get("output"), reference_date, base_year)
            if call.get("kind") == "tool":
                arg_dates |= {v for v in value_tokens(json.dumps(call.get("arguments", {}), ensure_ascii=False), reference_date, base_year) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)}
        visible = "\n".join(turn.get("visible", [turn.get("reply", "")]))
        for value in sorted(set(before["hallucinated_values"]) ^ set(after["hallucinated_values"])):
            canonical = value_tokens(value, reference_date, base_year)
            is_date = any(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in canonical)
            rules = set()
            if is_date:
                if canonical & known or canonical & set(after["hallucinated_values"]):
                    rules.add("a")
                elif canonical & arg_dates:
                    rules.add("c")
            elif value.endswith("원") and (canonical & known or normalized(normalize_amounts(visible)) != normalized(visible)):
                rules.add("b:amount")
            if not rules:
                unknown.append(f"turn {index + 1}: hallucinated_value {value}")
            details.append({"turn": index + 1, "field": "hallucinated_values", "value": value, "change": "removed" if value in before["hallucinated_values"] else "added", "rules": sorted(rules)})
    if len(old["turns"]) != len(new["turns"]):
        unknown.append("graded turn count changed")
    missing = new["violations"].get("no_proposal_before_consent", 0)
    if missing != old["violations"].get("no_proposal_before_consent", 0):
        details.append({"field": "no_proposal_before_consent", "change": "added", "value": missing, "rules": ["prior pending required"]})
    if old["success"] != new["success"] and not details:
        unknown.append("verdict changed without normalization differences")
    return {"changes": details, "unexplained": unknown, "flagged": not old["success"] and new["success"] and bool(unknown)}


def failure_types(scenario, row):
    from shop.grader import call_matches

    grade = row["grade"]
    kinds = set()
    if not grade["complete"]:
        kinds.add("실행 미완료")
    if not grade["final_state"]["match"]:
        kinds.add("final_state mismatch")
    for index, turn_grade in enumerate(grade["turns"]):
        if turn_grade["include_missing"]:
            refusal = {"불가", "불가능", "거부", "않", "없"}
            kinds.add("missing refusal phrase" if any(any(word in value for word in refusal) for value in turn_grade["include_missing"]) else "missing required reply")
        if turn_grade["excluded_found"]:
            kinds.add("forbidden reply phrase")
        calls = row["turns"][index].get("calls", [])
        for required in scenario["turns"][index]["expect"].get("required_calls", []):
            if not any(call_matches(call, required) for call in calls):
                kinds.add("handoff tool not called" if required["name"] == "escalate_to_human" else "required call missing / wrong arguments")
        if not turn_grade["route_correct"] or turn_grade["handoff_correct"] is False:
            kinds.add("route / handoff mismatch")
    for key, count in grade["violations"].items():
        if count:
            kinds.add(key)
    return sorted(kinds)


def score_text(rows, key):
    if not rows:
        return "0/0 (n/a)"
    return f"{sum(row[key] for row in rows)}/{len(rows)} ({100 * sum(row[key] for row in rows) / len(rows):.1f}%)"


def render_report(records, hashes):
    lines = ["# Shopagent 저장 실행 재채점 v2", "", "모델 호출\xb7다운로드\xb7패키지 설치 없이 저장 results/JSONL만 사용했다. LangSmith tracing 두 변수를 false로 설정하고 API 키를 프로세스 환경에서 제거했다. 감사 훅으로 네트워크 연결, 외부 프로세스 실행, 금지된 자격 증명 파일 접근을 차단했다.", "", "원본 results.json, 기존 보고서, 데이터\xb7gold\xb7trace의 SHA-256 보존을 확인했다. 최종 DB는 JSONL db_diff로 복원하고 도구 changes와 충돌이 없는지 및 원래 final_state 판정이 같은지 확인했다. 기존 도구 changes는 가변 리스트 append를 누락할 수 있어 최종 db_diff를 권위 있는 기록으로 사용했다. 차이는 각 결과의 rescore_replay_notes에 기록했다. 표의 old는 저장된 판정이며 new는 수정 grader의 판정이다.", "", "## 정규화\xb7동치 규칙 전체", ""]
    lines.extend("- " + rule for rule in RULES)
    lines += ["", "## 검증", "", "수정 전 grader로 저장된 420개 판정을 모두 재현했다. 수정 전 unittest 68개 통과. 수정 후 unittest 전체 82개 통과(추가 14개, 각 표기\xb7긍정\xb7부정 사례는 subTest로 검사). 요청한 `.venv/bin/python -m pytest -q tests`는 실행했으나 기존 .venv에 pytest가 없어 `No module named pytest`로 시작 실패했다. 설치 금지 조건을 준수했다.", "", "## 구성별 성공률", "", "g12-native는 r1-r3 총 180개를 합산한다. CPU 참고 실행은 별도 구성으로 구분한다.", "", "| 구성 | old | new |", "| --- | --- | --- |"]
    grouped = defaultdict(list)
    for record in records:
        grouped[record["config"]].extend(record["rows"])
    for config, rows in grouped.items():
        lines.append(f"| {config} | {score_text(rows, 'old_success')} | {score_text(rows, 'success')} |")
    lines += ["", "## 실행별 성공률 및 hallucinated_value 개수", "", "hallucinated_value는 시나리오별 턴 검출 개수의 합이다. 동일 값이 여러 턴에 나오면 각 턴을 센다.", "", "| 실행 | old 성공 | new 성공 | old hallucinated_value | new hallucinated_value |", "| --- | --- | --- | --- | --- |"]
    for record in records:
        rows = record["rows"]
        lines.append(f"| {record['run']} | {score_text(rows, 'old_success')} | {score_text(rows, 'success')} | {sum(r['old_grade']['violations']['hallucinated_value'] for r in rows)} | {sum(r['grade']['violations']['hallucinated_value'] for r in rows)} |")
    lines += ["", "## 범주별 성공률", "", "| 구성 | 범주 | old | new |", "| --- | --- | --- | --- |"]
    for config, rows in grouped.items():
        for category in sorted({r["category"] for r in rows}):
            selected = [r for r in rows if r["category"] == category]
            lines.append(f"| {config} | {category} | {score_text(selected, 'old_success')} | {score_text(selected, 'success')} |")
    lines += ["", "## 판정이 바뀐 모든 시나리오", "", "b:amount는 원화 정규화, b:equivalence는 위 동의어 표다. 각 행의 턴 번호는 1부터 센다.", "", "| 실행 | 시나리오 | old -> new | 원인 |", "| --- | --- | --- | --- |"]
    flips, flags = [], []
    for record in records:
        for row in record["rows"]:
            if row["old_success"] == row["success"]:
                continue
            flips.append(row)
            audit = row["rescore_audit"]
            reasons = [f"턴 {c.get('turn', 'scenario')} {c['field']} {c['change']}: {c['value']} ({', '.join(c['rules']) or '미분류'})" for c in audit["changes"]]
            if audit["flagged"]:
                flags.append(record["run"] + "/" + row["id"])
                reasons.append("FLAG: " + "; ".join(audit["unexplained"]))
            lines.append(f"| {record['run']} | {row['id']} | {'pass' if row['old_success'] else 'fail'} -> {'pass' if row['success'] else 'fail'} | {'; '.join(reasons)} |")
    forward = sum(not r["old_success"] and r["success"] for r in flips)
    lines += ["", f"fail -> pass: {forward}개. pass -> fail: {len(flips) - forward}개. 설명되지 않은 fail -> pass FLAG: {len(flags)}개." , "", "FLAG 목록: " + (", ".join(flags) if flags else "없음"), "", "## 남은 실패 유형", "", "실패 시나리오에 여러 유형을 중복 부여했다. 필수 호출\xb7라우팅은 기존 성공 게이트가 아닌 진단 지표다. 남은 실패는 자동 판정상 실패로, 실제 사용자 답변의 의미 평가는 추가 검토가 필요할 수 있다.", ""]
    failures = defaultdict(list)
    for record in records:
        for row in record["rows"]:
            if not row["success"]:
                for kind in row["remaining_failure_types"]:
                    failures[kind].append(record["run"] + "/" + row["id"])
    for kind, ids in sorted(failures.items()):
        lines += [f"### {kind} ({len(ids)}개)", "", ", ".join(ids), ""]
    lines += ["## 남은 실패별 구체적 근거", "", "| 실행/시나리오 | 성공 게이트 위반 및 진단 |", "| --- | --- |"]
    for record in records:
        for row in record["rows"]:
            if row["success"]:
                continue
            details = [", ".join(row["remaining_failure_types"])]
            for index, grade in enumerate(row["grade"]["turns"]):
                for key in ("include_missing", "excluded_found", "hallucinated_values"):
                    if grade[key]:
                        details.append(f"턴 {index + 1} {key}: {json.dumps(grade[key], ensure_ascii=False)}")
            if row["grade"]["final_state"]["errors"]:
                details.append("final_state: " + json.dumps(row["grade"]["final_state"]["errors"], ensure_ascii=False))
            lines.append(f"| {record['run']}/{row['id']} | {'; '.join(details)} |")
    lines += ["", "## 원본 보존 해시", "", f"원본 파일 {len(hashes)}개의 SHA-256을 실행 전후 대조했다. 각 새 results 파일의 rescore.source_sha256 및 rescore.protected_sha256에 기록했다.", ""]
    return "\n".join(lines), flags


def dataset_compatibility(saved, dataset):
    """Permit historical versions against current expectations, recording both fingerprints."""
    from shop.shared import dm

    current_hash = dm.fingerprint(dataset)
    exact = saved.get("dataset_sha256") == current_hash
    if not exact and saved.get("data_version") == dataset.get("data_version"):
        raise ValueError("Dataset fingerprint mismatch for current version")
    scenarios = {s["id"]: s for s in dataset["scenarios"]}
    for row in saved.get("scenarios", []):
        if row["id"] not in scenarios or len(row.get("turns", [])) > len(scenarios[row["id"]]["turns"]):
            raise ValueError("Historical scenario structure mismatch: " + row["id"])
    return {"source_data_version": saved.get("data_version"), "data_version": dataset.get("data_version"),
            "source_dataset_sha256": saved.get("dataset_sha256"), "dataset_sha256": current_hash,
            "historical_expectations_updated": not exact}


def grading_failure_types(grade, rows, structural=False):
    """Describe gate failures from saved metrics, without inspecting reply text."""
    failures = set()
    if not grade["complete"]:
        failures.add("incomplete_or_loop_limit")
    if not grade["final_state"]["match"]:
        failures.add("final_state_mismatch")
    failures.update(key for key, count in grade["violations"].items() if count)
    for row, turn in zip(rows, grade["turns"]):
        if turn.get("include_missing"):
            failures.add("missing_required_reply_phrase")
        if turn.get("excluded_found"):
            failures.add("forbidden_reply_phrase")
        if structural and row.get("consent_mode") in ("proposal", "tool_interrupt"):
            if turn["required_hits"] != turn["required_count"]:
                failures.add("required_calls_missing_or_wrong_arguments")
            if turn["forbidden_calls"]:
                failures.add("forbidden_calls")
            if not turn["chain_order_correct"]:
                failures.add("call_order_mismatch")
            if turn.get("proposal_missing_when_expected"):
                failures.add("proposal_missing_when_expected")
    if structural and rows and all(r.get("consent_mode") in ("proposal", "tool_interrupt") for r in rows) and not grade["chain_order_correct"]:
        failures.add("call_order_mismatch")
    return failures


def v2_report(folders):
    from shop.grader import grade_scenario
    from shop.shared import ROOT, load
    from shop.tools import Shop

    folders = [Path(folder).resolve() for folder in folders]
    sources = [ROOT / path for path in ("data/scenarios.json", "data/shop_db.json", "data/prompts.json", "shop/grader.py", "shop/shared.py")]
    sources.append(Path(__file__).resolve())

    def snapshot():
        paths = set(sources) | {p for folder in folders for p in folder.rglob("*") if p.is_file()}
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}

    before = snapshot()
    lines = ["Grading changes only; policy injection, confirmation cards and simulated customers were not replayed."]
    try:
        dataset, base = load(ROOT / "data/scenarios.json"), load(ROOT / "data/shop_db.json")
        scenarios = {s["id"]: dict(s, reference_date=s.get("reference_date", dataset["reference_date"])) for s in dataset["scenarios"]}
        for folder in folders:
            saved = load(folder / "results.json")
            dataset_compatibility(saved, dataset)
            if saved.get("graph") != "v2" and saved.get("consent_mode") not in ("proposal", "tool_interrupt") and not any(t.get("consent_mode") in ("proposal", "tool_interrupt") for r in saved["scenarios"] for t in r.get("turns", [])):
                raise ValueError("Expected a v2 run: " + str(folder))
            outcomes, changes, fallbacks = [], [], []
            reference_count = reference_hits = 0
            for row in saved["scenarios"]:
                scenario = scenarios[row["id"]]
                label = row["id"] + (f" attempt {row['attempt_index']}" if "attempt_index" in row else "")
                old = row.get("grade")
                old_success = row.get("success", False)
                if old is not None and old["success"] != old_success:
                    raise ValueError("Saved success fields disagree: " + label)
                fallback = None
                try:
                    if not row.get("turns") and row.get("status") in ("not_run", "blocked") and not trace_path(folder, row).exists():
                        shop = Shop(base, scenario["customer_id"], scenario.get("db_patch"), scenario.get("faults"), reference_date=scenario.get("reference_date"))
                    else:
                        shop = restore_shop(folder, row, scenario, base)
                except (FinalStateReplayError, FileNotFoundError) as error:
                    # final_state is a verdict, not a DB snapshot. Retain it explicitly;
                    # never invent a DB from expected gold values or pretend replay succeeded.
                    if old is None:
                        raise ValueError("Cannot restore final DB and no saved grade.final_state: " + label) from error
                    fallback = copy.deepcopy(old["final_state"])
                    if type(fallback.get("match")) is not bool or not isinstance(fallback.get("errors"), list):
                        raise ValueError("Invalid saved final_state: " + label) from error
                    shop = Shop(base, scenario["customer_id"], scenario.get("db_patch"), scenario.get("faults"), reference_date=scenario.get("reference_date"))
                    fallbacks.append(label + ": " + str(error))
                new = grade_scenario(scenario, row["turns"], shop, saved_final_state=fallback)
                reference_count += sum(turn.get("reference_count", 0) for turn in new["turns"])
                reference_hits += sum(turn.get("reference_hits", 0) for turn in new["turns"])
                outcomes.append({"old_success": old_success, "success": new["success"]})
                if old_success != new["success"]:
                    old_failures = grading_failure_types(old, row["turns"]) if old is not None else {"saved_grade_unavailable"}
                    new_failures = grading_failure_types(new, row["turns"], structural=True)
                    changes.append(f"  {label}: {old_success} -> {new['success']}; removed={json.dumps(sorted(old_failures - new_failures), ensure_ascii=False)}; added={json.dumps(sorted(new_failures - old_failures), ensure_ascii=False)}")
            lines.append(f"{folder.name}: {score_text(outcomes, 'old_success')} -> {score_text(outcomes, 'success')}")
            lines.append(f"  Reference call recall: {reference_hits}/{reference_count}" + (f" ({100 * reference_hits / reference_count:.1f}%)" if reference_count else " (n/a)"))
            lines.extend(changes or ["  No scenario success changes."])
            lines.append("  Saved final_state fallback rows: " + ("; ".join(fallbacks) if fallbacks else "none"))
    finally:
        if snapshot() != before:
            raise ValueError("Source files changed during read-only rescoring")
    lines.append(f"Source hashes verified: {len(before)} files unchanged.")
    print("\n".join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v2-report", nargs="+", type=Path, metavar="RUN_FOLDER", help="Print current grades and changes; never write files")
    args = parser.parse_args(argv)
    if args.v2_report:
        offline(read_only=True)
        return v2_report(args.v2_report)
    offline()
    from shop.grader import grade_scenario
    from shop.shared import ROOT, dm, load

    report_dir = ROOT / "docs/archive/reports"
    paths = [ROOT / "runs" / "archive" / run / "results_rescored_v2.json" for run in RUNS] + [report_dir / "shopagent_report_rescored.md"]
    for path in paths:
        if path.exists():
            raise FileExistsError("Refusing to overwrite: " + str(path))
    originals = [report_dir / "shopagent_report.md"] + list((ROOT / "data").rglob("*"))
    originals += [ROOT / "runs" / "archive" / run / "results.json" for run in RUNS]
    originals += [path for run in RUNS for path in (ROOT / "runs" / "archive" / run / "traces").glob("*.jsonl")]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in originals if p.is_file()}
    dataset = load(ROOT / "data/scenarios.json")
    scenarios = {s["id"]: dict(s, reference_date=s.get("reference_date", dataset["reference_date"])) for s in dataset["scenarios"]}
    base, records, outputs = load(ROOT / "data/shop_db.json"), [], []
    for run in RUNS:
        folder = ROOT / "runs" / "archive" / run
        saved = load(folder / "results.json")
        compatibility = dataset_compatibility(saved, dataset)
        result = copy.deepcopy(saved)
        result["rescore"] = {"version": 2, "source": "results.json", "source_sha256": hashes[f"runs/archive/{run}/results.json"], "protected_sha256": hashes, "grader_sha256": hashlib.sha256((ROOT / "shop/grader.py").read_bytes()).hexdigest(), "rules": RULES, "offline": True, **compatibility}
        for row in result["scenarios"]:
            scenario = scenarios[row["id"]]
            shop = restore_shop(folder, row, scenario, base)
            row["rescore_replay_notes"] = shop.rescore_replay_notes
            old = row["grade"]
            if row["success"] != old["success"]:
                raise ValueError("Saved success fields disagree: " + run + "/" + row["id"])
            new = grade_scenario(scenario, row["turns"], shop)
            if old["final_state"] != new["final_state"]:
                raise ValueError("Restored final state changed: " + run + "/" + row["id"])
            row.update(old_success=row["success"], old_grade=old, grade=new, success=new["success"])
            row["rescore_audit"] = audit_changes(scenario, row, old, new)
            row["remaining_failure_types"] = failure_types(scenario, row) if not new["success"] else []
        label = "g12-native-ngl0 (CPU 참고)" if run == "g12-native-ngl0-r1" else result["config"]
        records.append({"run": run, "config": label, "rows": result["scenarios"]})
        outputs.append(result)
    report, flags = render_report(records, hashes)
    for path, digest in hashes.items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise ValueError("Protected file changed: " + path)
    for path, result in zip(paths[:-1], outputs):
        with path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    with paths[-1].open("x", encoding="utf-8") as stream:
        stream.write(report)
    for record in records:
        print(record["run"], score_text(record["rows"], "old_success"), "->", score_text(record["rows"], "success"))
    print("FLAG:", len(flags), ", ".join(flags))


if __name__ == "__main__":
    main()
