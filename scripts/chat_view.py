"""Build an offline conversation viewer from saved benchmark results."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(ROOT))

from shop.tools import IRREVERSIBLE


TURN_FIELDS = "consent_mode user scripted_user simulated_customer routes reply reply_claim_judgment visible confirmation_card pending_at_start pending pending_events action_decision consent_judgment declined_actions approval_state order_context policy_context tool_count model_call_count ungrounded_values grounding_retries force_tool_retries loop_limit limit_reached transfers status error total_ms".split()
CALL_FIELDS = "kind name arguments output prefetched scope_error fault_injected irreversible proposal_id changes total_ms".split()
VERDICTS = {"yes": "승인", "no": "거절", "changed": "거절(변경 요청)", "unclear": "애매"}
VIOLATIONS = {
    "no_proposal_before_consent": "동의 전 제안 없음", "unconfirmed_action": "미승인 실행",
    "report_execution_mismatch": "답장과 실행 불일치", "hallucinated_value": "근거 없는 값",
    "pending_approval_state_mismatch": "확인 대기 상태 불일치", "ineligible_action": "불가 행동 실행",
    "pii_leak": "개인정보 노출", "card_request": "카드정보 요청", "injection_followed": "주입 지시 따름",
    "false_completion": "실행 안 됐는데 완료라고 답함",
}


def mapping(value):
    return value if isinstance(value, dict) else {}


def records(value):
    return value if isinstance(value, list) else []


def compact(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def read_json(path):
    def reject_constant(value):
        raise ValueError("Invalid JSON constant: " + value)
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def clean(value):
    """Discard private/bulky records even when nested inside lookup context."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()
                if k not in ("llm_calls", "request", "messages", "changes")
                and not any(word in k for word in ("memory", "contention", "vram", "contended"))}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def positive(value):
    return isinstance(value, (int, float)) and value > 0


def summary_warnings(grade):
    grade = mapping(grade)
    warnings = [f"{VIOLATIONS.get(key, '기록된 위반')} ({key}): {value}"
                for key, value in mapping(grade.get("violations")).items() if positive(value)]
    if grade.get("confirmation_correct") is False:
        warnings.append("확인 요청 불일치 (confirmation_correct=false)")
    recall = grade.get("required_call_recall")
    if isinstance(recall, (int, float)) and recall < 1:
        warnings.append(f"필수 호출 누락 (required_call_recall={recall})")
    for key, label in (("proposal_missing_when_expected", "필요한 제안 없음"),
                       ("unconfirmed_executions", "미승인 실행")):
        if positive(grade.get(key)):
            warnings.append(f"{label} ({key}): {grade[key]}")
    return warnings


def card_summary(proposal, fallback=None):
    proposal = mapping(proposal)
    text = proposal.get("confirmation_card")
    if not isinstance(text, str):
        text = fallback if isinstance(fallback, str) else ""
    fields, title = [], "행동 (기록 없음)"
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        fields.append({"key": key if separator else "", "value": value if separator else line})
        if separator and key.strip() == "행동":
            title = value.strip()
    return {"proposal_id": proposal.get("id", "(기록 없음)"),
            "tool": proposal.get("name", "(기록 없음)"), "title": title, "fields": fields, "text": text}


def judgment_summary(turn, events):
    judged = next((e for e in events if e.get("event") == "judged"), {})
    judgment = mapping(turn.get("consent_judgment")) or judged
    if not judgment:
        return None
    verdict = judgment.get("verdict")
    resume_type = {"yes": "approve", "no": "reject", "changed": "reject", "unclear": "respond"}.get(verdict, "-")
    pending = mapping(judged.get("proposal")) or mapping(turn.get("pending_at_start"))
    message, source = "(기록 없음)", "기록 없음"
    # Only the exact pending tool-call ID identifies the specialist's resume.
    # Do not inspect or classify message content; keep that recorded message intact.
    for call in records(turn.get("llm_calls")):
        call = mapping(call)
        if call.get("node") in ("router", "supervisor", "consent_judge", "simulated_customer", "reply_judge"):
            continue
        for item in records(mapping(call.get("request")).get("messages")):
            item = mapping(item)
            if (pending.get("tool_call_id") and item.get("tool_call_id") == pending["tool_call_id"]
                    and item.get("type", item.get("role")) == "tool" and "content" in item):
                message, source = item["content"], "모델 요청 기록"
                break
        if source != "기록 없음":
            break
    if source == "기록 없음" and resume_type != "-" and isinstance(turn.get("user"), str):
        from shop.graph_v2 import consent_resume_decision, consent_resume_message
        decision = consent_resume_decision(verdict, turn["user"])
        if "message" in decision:
            message, source = decision["message"], "graph_v2 규칙으로 복원"
            if decision["type"] == "reject" and pending.get("name") in IRREVERSIBLE:
                message = consent_resume_message(verdict, decision["message"], pending)
    return {"verdict": verdict, "label": VERDICTS.get(verdict, "(기록 없음)"),
            "proposal_id": judgment.get("proposal_id", pending.get("id", "(기록 없음)")),
            "reason": judgment.get("reason", "(기록 없음)"), "resume_type": resume_type,
            "resume_message": message, "resume_source": source}


def output_summary(output):
    output = mapping(output)
    keys = ("ok", "status", "error", "refund_amount", "refunded", "pickup_date", "refund_status",
            "pending_refund", "fee", "shipping_fee_clawback", "expected_refund", "extra_charge")
    return " · ".join(f"{key}={compact(output[key])}" for key in keys if key in output) or "(결과 요약 기록 없음)"


def turn_summary(turn, grade=None):
    """Compute the display exclusively from recorded state, calls and grades."""
    turn, grade = mapping(turn), mapping(grade)
    interrupt = turn.get("consent_mode") == "tool_interrupt"
    events = [e for e in records(turn.get("pending_events")) if isinstance(e, dict)]
    calls = [c for c in records(turn.get("calls")) if isinstance(c, dict)]
    cards = [card_summary(e.get("proposal"), turn.get("confirmation_card"))
             for e in events if e.get("event") == "proposed"] if interrupt else []
    judgment = judgment_summary(turn, events) if interrupt else None
    executions, covered = [], set()
    for call in calls:
        if call.get("name") not in IRREVERSIBLE:
            continue
        output = mapping(call.get("output"))
        reason = ""
        if output.get("error") == "DECLINED_BY_CUSTOMER":
            reason = "DECLINED_BY_CUSTOMER로 차단"
        elif call.get("scope_error") or call.get("kind") == "blocked":
            reason = "범위 밖/blocked"
        elif call.get("kind") != "tool":
            continue
        row = {"tool": call["name"], "proposal_id": call.get("proposal_id", "(기록 없음)"),
               "status": "실행 안 됨" if reason else "실행됨", "reason": reason,
               "result_summary": output_summary(call.get("output"))}
        executions.append(row)
        if call.get("proposal_id"):
            covered.add(call["proposal_id"])
    # Pauses/rejections do not create domain-call records. Represent those proposals
    # separately, while retaining every real attempted call above.
    proposals = {}
    starting = mapping(turn.get("pending_at_start"))
    if judgment and starting.get("id") and starting.get("name") in IRREVERSIBLE:
        proposals[starting["id"]] = starting
    for event in events:
        proposal = mapping(event.get("proposal"))
        if proposal.get("id") and proposal.get("name") in IRREVERSIBLE:
            proposals[proposal["id"]] = proposal
    pending = mapping(turn.get("pending"))
    if pending.get("id") and pending.get("name") in IRREVERSIBLE:
        proposals[pending["id"]] = pending
    for pid, proposal in proposals.items():
        if pid in covered or any(c.get("kind") in ("tool", "blocked") and not c.get("proposal_id")
                                 and c.get("name") == proposal["name"]
                                 and c.get("arguments") == proposal.get("args") for c in calls):
            continue
        executed = next((e for e in events if e.get("event") == "executed"
                         and mapping(e.get("proposal")).get("id") == pid), None)
        verdict = judgment.get("verdict") if judgment and judgment.get("proposal_id") == pid else None
        cleared = next((e.get("reason") for e in events if e.get("event") == "cleared"
                        and mapping(e.get("proposal")).get("id") == pid), None)
        reason = {"no": "고객 거절", "changed": "고객 거절 (변경 요청)", "unclear": "애매 응답"}.get(verdict or cleared)
        if not reason:
            proposed = any(e.get("event") == "proposed" and mapping(e.get("proposal")).get("id") == pid for e in events)
            paused = pending.get("id") == pid or proposed and not cleared and not verdict
            reason = "확인 대기 중(paused)" if paused else (cleared or "(실행 기록 없음)")
        executions.append({"tool": proposal["name"], "proposal_id": pid,
                           "status": "실행됨" if executed else "실행 안 됨", "reason": "" if executed else reason,
                           "result_summary": output_summary(executed.get("output")) if executed else ""})
    executed = [e for e in executions if e["status"] == "실행됨"]
    db_changes = [{"tool": c.get("name"), "proposal_id": c.get("proposal_id"), "changes": clean(c["changes"])}
                  for c in calls if c.get("changes")]
    if db_changes:
        db = f"변화 {sum(len(c['changes']) for c in db_changes)}건"
    elif (calls and not all("changes" in c for c in calls)) or (executed and not calls):
        db = "(기록 없음)"
    else:
        db = "변화 없음" if interrupt or calls else "-"
    execution = ("실행됨(" + ", ".join(e["tool"] for e in executed) + ")") if executed else ("없음" if interrupt or executions else "-")
    warnings = summary_warnings(grade)
    if (not executed and positive(mapping(grade.get("violations")).get("report_execution_mismatch"))):
        warnings.append("실행 안 했는데 완료라고 답함 (report_execution_mismatch)")
    comparison = None
    if execution != "-" and (turn.get("reply_claim_judgment") is not None or not executed or judgment and judgment["verdict"] in ("no", "changed", "unclear")):
        comparison = {"execution": "실행됨" if executed else "실행 안 됨", "reply": turn.get("reply", "")}
        if "reply_claim_judgment" in turn:
            comparison["reply_claim_judgment"] = clean(turn["reply_claim_judgment"])
    card = "표시됨(" + ", ".join(f"{c['proposal_id']} {c['tool']}" for c in cards) + ")" if cards else ("없음" if interrupt else "-")
    judgment_text = f"{judgment['label']}({judgment['verdict']})" if judgment else "-"
    return {"cards": cards, "card": card, "judgment": judgment, "judgment_text": judgment_text,
            "executions": executions, "execution": execution, "db": db, "db_changes": db_changes,
            "warnings": warnings, "warning_count": len(warnings), "comparison": comparison,
            "line": f"카드: {card} | 판단: {judgment_text} | 실행: {execution} | DB: {db} | 경고 {len(warnings)}"}


def clean_turn(turn, grade=None):
    turn = mapping(turn)
    result = {key: clean(turn[key]) for key in TURN_FIELDS if key in turn}
    result["summary"] = turn_summary(turn, grade)
    try:
        request = json.loads(turn.get("user", ""))
    except (TypeError, ValueError):
        request = None
    if (isinstance(request, dict) and isinstance(request.get("specialist_reply"), str)
            and isinstance(request.get("irreversible_tools"), list)):
        result["internal_decision_request"] = request
    result["calls"] = []
    for call in records(turn.get("calls")):
        if not isinstance(call, dict):
            continue
        row = {key: clean(call[key]) for key in CALL_FIELDS if key in call}
        output = row.get("output")
        row["output_error"] = mapping(output).get("ok") is False
        text = output if isinstance(output, str) else compact(output)
        if len(text) > 4000:
            row["output"] = text[:4000] + "\n… [도구 출력 4000자 이후 생략]"
        result["calls"].append(row)
    return result


def turn_problems(turn, grade, spec):
    """Describe saved checks; do not judge customer/agent language again."""
    turn, grade = mapping(turn), mapping(grade)
    problems = []
    for key, label in (("route_correct", "담당 경로 불일치"),
                       ("confirmation_correct", "확인 요청 불일치"),
                       ("chain_order_correct", "도구 순서 불일치"),
                       ("handoff_correct", "담당 이전 불일치")):
        if grade.get(key) is False:
            problems.append(f"{label} ({key}=false)")
    hits, count = grade.get("required_hits"), grade.get("required_count")
    if isinstance(hits, (int, float)) and isinstance(count, (int, float)) and hits < count:
        expected = mapping(mapping(spec).get("expect"))
        required = records(expected.get("v2_required_calls", expected.get("required_calls")))
        actual = Counter(c.get("name") for c in records(turn.get("calls"))
                         if isinstance(c, dict) and c.get("kind") == "tool" and not c.get("scope_error"))
        missing = []
        for call in required:
            name = mapping(call).get("name") if isinstance(call, dict) else call
            if not isinstance(name, str):
                continue
            if actual[name]:
                actual[name] -= 1
            else:
                missing.append(name)
        detail = "; 호출 기록에 없는 필수 이름: " + ", ".join(missing) if missing else "; 이름만으로 누락 구분 불가 (인자 조건 포함)"
        problems.append(f"필수 도구 충족 {hits}/{count}" + detail)
    for key, label in (("forbidden_calls", "금지 도구"), ("include_missing", "필수 문구 누락"),
                       ("excluded_found", "제외 문구 발견"), ("hallucinated_values", "근거 없는 값")):
        if grade.get(key):
            problems.append(f"{label}: {compact(grade[key])}")
    if grade.get("proposal_missing_when_expected"):
        problems.append("필요한 제안 없음 (proposal_missing_when_expected)")
    if grade.get("judge_correct") is False:
        problems.append(f"동의 판정 불일치: 기대 {grade.get('expected_verdict')}, 실제 {grade.get('judged_verdict')}")
    for name, value in mapping(grade.get("violations")).items():
        if value:
            problems.append(f"위반 {name}: {compact(value)}")
    for key in ("loop_limit", "limit_reached", "error"):
        if turn.get(key):
            problems.append(f"{key}: {compact(turn[key])}")
    if turn.get("status") not in (None, "complete"):
        problems.append(f"턴 상태: {turn['status']}")
    return problems


def failure_reasons(scenario, spec=None):
    scenario, spec = mapping(scenario), mapping(spec)
    grade = mapping(scenario.get("grade"))
    state = mapping(grade.get("final_state"))
    reasons = []
    for error in records(state.get("errors")):
        error = mapping(error)
        if "expected" in error or "actual" in error:
            reasons.append(f"최종 상태 {error.get('path', '?')}: 기대 {compact(error.get('expected'))}, 실제 {compact(error.get('actual'))}")
        else:
            reasons.append(f"최종 상태 {error.get('path', '?')}: {error.get('error', '변경 오류')}, 이전 {compact(error.get('before'))}, 이후 {compact(error.get('after'))}")
    if state.get("match") is False and not state.get("errors"):
        reasons.append("최종 상태 불일치 (세부 오류 미저장)")
    for name, count in mapping(grade.get("violations")).items():
        if count:
            reasons.append(f"총 위반 {name}: {compact(count)}")
    turns, grades, scripts = (records(x.get("turns")) for x in (scenario, grade, spec))
    for index in range(max(len(turns), len(grades))):
        problems = turn_problems(turns[index] if index < len(turns) else {},
                                 grades[index] if index < len(grades) else {},
                                 scripts[index] if index < len(scripts) else {})
        reasons.extend(f"턴 {index + 1}: {problem}" for problem in problems)
    for key, label in (("complete", "시나리오 미완료"), ("chain_order_correct", "전체 도구 순서 불일치")):
        if grade.get(key) is False:
            reasons.append(label)
    if scenario.get("error"):
        reasons.append(f"실행 오류: {scenario['error']}")
    return reasons


def build_run(name, raw, specs=None):
    """Build a small JSON-ready run, also usable with synthetic dictionaries."""
    if not isinstance(raw, dict) or not isinstance(raw.get("scenarios"), list):
        raise ValueError("scenarios 목록 없음")
    specs = specs or {}
    supported = raw.get("graph") == "v2"
    result = {"name": name, "supported": supported, "started": raw.get("started") or "",
              "graph": raw.get("graph", "v1"), "assist": raw.get("assist", "off"),
              "data_version": raw.get("data_version", "미저장"), "scenarios": [],
              "total": len(raw["scenarios"]),
              "passed": sum(bool(mapping(s).get("success")) for s in raw["scenarios"])}
    if not supported:
        result["reason"] = "v1 형식: 채팅 보기 미지원"
        return result
    empty = 0
    for source in raw["scenarios"]:
        source = mapping(source)
        if not records(source.get("turns")):
            empty += 1
            continue
        sid = str(source.get("id", "이름 없음"))
        spec = mapping(specs.get(sid))
        turns = records(source.get("turns"))
        category = source.get("category") or spec.get("category") or "미저장"
        description = spec.get("gold_rationale") or f"{category}: {mapping(turns[0]).get('user', '')}"
        grade = mapping(source.get("grade"))
        scripts, grades = records(spec.get("turns")), records(grade.get("turns"))
        row = {"id": sid, "category": category, "description": description,
               "customer_id": source.get("customer_id") or spec.get("customer_id") or "미저장",
               "success": bool(source.get("success", grade.get("success", False))),
               "status": source.get("status"), "wall_time_s": source.get("wall_time_s"),
               "grade": clean(grade),
               "turns": [clean_turn(t, grades[i] if i < len(grades) else {}) for i, t in enumerate(turns)],
               "warnings": ["최종 DB 불일치 (final_state.match=false)"] if mapping(grade.get("final_state")).get("match") is False else [],
               "reasons": failure_reasons(source, spec)}
        if source.get("attempt_index") is not None:
            row["attempt_index"] = source["attempt_index"]
        for index, turn in enumerate(row["turns"]):
            turn["problems"] = turn_problems(turns[index], grades[index] if index < len(grades) else {},
                                              scripts[index] if index < len(scripts) else {})
        result["scenarios"].append(row)
    result["empty_scenarios"] = empty
    if not result["scenarios"]:
        result.update(supported=False, reason="대화 턴이 있는 v2 시나리오 없음")
    return result


def load_data(root=ROOT, names=None):
    root = Path(root)
    notes, runs, specs = [], [], {}
    try:
        dataset = read_json(root / "data/scenarios.json")
        specs = {s["id"]: s for s in records(mapping(dataset).get("scenarios")) if isinstance(s, dict) and "id" in s}
    except (OSError, ValueError) as error:
        notes.append(f"시나리오 설명 읽기 실패: {type(error).__name__} (기본 설명 사용)")
    folders = sorted((root / "runs").iterdir()) if (root / "runs").is_dir() else []
    if names:
        # Names select direct children only; do not interpret paths outside runs/.
        selected = set(names)
        found = {p.name for p in folders if p.is_dir()}
        notes.extend(f"{n}: 실행 폴더 없음" for n in sorted(selected - found))
        folders = [p for p in folders if p.name in selected]
    for folder in folders:
        if not folder.is_dir():
            continue
        try:
            raw = read_json(folder / "results.json")
            run = build_run(folder.name, raw, specs)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            reason = "results.json 없음" if isinstance(error, FileNotFoundError) else f"results.json 읽기 실패 ({type(error).__name__})"
            run = {"name": folder.name, "supported": False, "reason": reason,
                   "started": "", "scenarios": []}
        runs.append(run)
        if not run["supported"]:
            notes.append(f"{folder.name}: {run['reason']}")
    runs.sort(key=lambda r: (str(r.get("started", "")), r["name"]), reverse=True)
    return {"runs": runs, "notes": notes}


HTML = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>쇼핑 상담 채팅 보기</title>
<style>
:root{color-scheme:light dark;--bg:#e7eff5;--panel:#fff;--text:#202b36;--muted:#607183;--line:#d9e1e8;--yellow:#ffe467;--yellowtext:#302800;--bad:#b52236;--badbg:#fff0f1;--good:#187544;--selected:#e8f2fc}
*{box-sizing:border-box}body{margin:0;color:var(--text);background:var(--bg);font:14px/1.65 'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',sans-serif}button,input,select{font:inherit}button,select{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #3187d1;outline-offset:2px}
.shell{display:flex;height:100dvh}.sidebar{width:320px;flex-shrink:0;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column}.controls{padding:18px 14px;border-bottom:1px solid var(--line)}h1{font-size:19px;margin:0 0 14px}label{display:block;margin:6px 0}select,input[type=search]{display:block;width:100%;padding:8px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--text)}#run-meta,.muted{color:var(--muted);font-size:12px}.scenario-list{overflow:auto;flex:1;padding:7px}.scenario-row{display:block;border:0;border-radius:8px;background:transparent;color:inherit;text-align:left;width:100%;padding:11px;margin-bottom:3px}.scenario-row:hover,.scenario-row.active{background:var(--selected)}.scenario-row.active{box-shadow:inset 3px 0 #287bc4}.one-line{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font-size:12px}.badge{display:inline-block;padding:0 7px;font-size:11px;border-radius:5px;margin:0 4px;background:var(--selected)}.pass{color:var(--good);background:#e0f3e8}.fail{color:var(--bad);background:var(--badbg)}option:disabled{color:#89939c}main{flex:1;overflow:auto;min-width:0}.conversation{max-width:1060px;margin:0 auto;padding:24px 32px 70px}.scenario-header{padding-bottom:16px;border-bottom:1px solid var(--line)}h2{margin:0;font-size:23px}h3{font-size:15px;margin:0 0 8px}p{margin:6px 0}.failure{background:var(--badbg);border:1px solid var(--bad);border-radius:10px;padding:16px;margin-top:16px;color:var(--bad)}.failure li{overflow-wrap:anywhere}.turn{margin:28px 0}.turn-title{text-align:center;color:var(--muted);font-size:12px;margin-bottom:16px}.message{display:flex;flex-direction:column;margin:12px 0;align-items:flex-start}.message.customer{align-items:flex-end}.bubble{max-width:85%;padding:12px 16px;border-radius:3px 16px 16px;background:var(--panel);box-shadow:0 1px 3px #0001;white-space:pre-wrap;overflow-wrap:anywhere}.customer .bubble{border-radius:16px 3px 16px 16px;background:var(--yellow);color:var(--yellowtext)}.avatar{font-size:12px;color:var(--muted);margin:0 0 4px 3px}.caption{max-width:85%;font-size:12px;color:var(--muted);margin:5px 0}.empty{color:var(--muted);font-style:italic}.card{border:1px solid #9aacc0;border-radius:8px;padding:10px 14px;background:var(--panel);margin:10px 0;max-width:90%}details{border-radius:7px;margin:8px 0;background:var(--panel);padding:8px 12px;border:1px solid var(--line)}summary{cursor:pointer;overflow-wrap:anywhere}details details{margin:7px 0}.problem{border-left:4px solid var(--bad)}.event{font-size:12px;margin:7px 0;padding:7px 11px;background:var(--panel);border-radius:6px;overflow-wrap:anywhere}.warning{color:var(--bad)}pre{font:12px/1.6 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;margin:7px 0}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:6px 8px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);overflow-wrap:anywhere}th{width:35%}td pre{margin:0}footer{border-top:1px solid var(--line);padding-top:18px;margin-top:30px}ul{padding-left:22px}.sidebar-note{padding:0 14px 12px;color:var(--muted);font-size:11px}
.turn{padding-top:22px}.turn-summary{padding:9px 12px;background:var(--selected);border-radius:7px;margin:8px 0;font-size:12px}.summary-scroll{overflow-x:auto;margin:18px 0}.summary-table th{width:auto;white-space:nowrap}.confirmation-ui{border:2px solid #3187d1;border-radius:14px;padding:18px;max-width:640px;background:var(--panel);margin:14px 0;box-shadow:0 4px 14px #0001}.confirmation-ui h3{font-size:18px}.card-fields{margin:12px 0}.card-field{display:grid;grid-template-columns:minmax(90px,1fr) 3fr;border-bottom:1px solid var(--line);padding:5px 0;white-space:pre-wrap;overflow-wrap:anywhere}.card-field .raw-line{grid-column:1 / -1}.card-buttons{display:flex;gap:10px;margin-top:14px}.card-buttons button{padding:7px 24px;border-radius:7px;border:1px solid var(--line);background:var(--selected);color:var(--text);cursor:default}.comparison{display:grid;grid-template-columns:1fr 2fr;gap:12px;background:var(--panel);padding:12px;border:1px solid var(--line);border-radius:8px}.comparison pre{margin:0}.execution-record{border-left:3px solid #3187d1;padding:8px 12px;margin:8px 0;background:var(--panel)}
@media(prefers-color-scheme:dark){:root{--bg:#17222d;--panel:#253240;--text:#e5edf4;--muted:#a6b7c8;--line:#405163;--yellow:#d4b748;--bad:#ff93a2;--badbg:#412a33;--good:#83dca9;--selected:#314d66}.pass{background:#214936}}
h2,.scenario-header{overflow-wrap:anywhere}.comparison>*,.card-field>*{min-width:0}.card-buttons{flex-wrap:wrap}
@media(max-width:720px){.shell{height:auto;display:block}.sidebar{width:100%;height:auto;border-right:0;border-bottom:1px solid var(--line)}.controls{padding:10px 14px}h1{margin-bottom:6px}.scenario-list{flex:none;max-height:45dvh;min-height:180px}.sidebar-note{display:none}.conversation{padding:18px 14px 50px}main{overflow:visible}h2{font-size:20px}.bubble,.caption{max-width:94%}.card{max-width:100%}.confirmation-ui{padding:14px}.comparison{grid-template-columns:1fr}.card-field{grid-template-columns:minmax(72px,1fr) 2fr}}
</style>
</head>
<body>
<div class="shell">
<aside class="sidebar" aria-label="실행 및 시나리오 선택">
<div class="controls"><h1>💬 쇼핑 상담 채팅 보기</h1>
<label for="run-picker">실행 선택</label><select id="run-picker"></select><p id="run-meta"></p>
<label for="filter">시나리오 검색</label><input id="filter" type="search" placeholder="ID · 설명 · 고객 ID">
<label><input id="failed-only" type="checkbox"> 실패만 보기</label></div>
<nav id="scenario-list" class="scenario-list" aria-label="시나리오 목록"></nav>
<div class="sidebar-note">↑ ↓ 시나리오 이동 · 주소의 # 링크 공유<br>저장된 결과만 표시 · 원본 실행 데이터 읽기 전용</div>
</aside><main id="main"><div id="conversation" class="conversation"></div></main>
</div>
<script id="chat-data" type="application/json">__CHAT_DATA__</script>
<script>
'use strict';
const DATA = JSON.parse(document.getElementById('chat-data').textContent);
const $ = id => document.getElementById(id);
const obj = x => x && typeof x === 'object' && !Array.isArray(x) ? x : {};
const arr = x => Array.isArray(x) ? x : [];
const pretty = x => typeof x === 'string' ? x : JSON.stringify(x, null, 2) ?? '미저장';
const compact = x => typeof x === 'string' ? x : JSON.stringify(x) ?? '';
const nodes = {router:'라우터', supervisor:'라우터', order_delivery:'주문·배송', refund_exchange:'반품·교환',
  payment:'결제', qna:'문의', product:'상품', human:'상담원'};
const verdicts = {yes:'동의', no:'거절', changed:'변경', unclear:'불명확'};
const decisions = {approve:'승인', reject:'거절', change:'변경'};
const labels = {
  route_correct:'담당 경로 일치', initial_route_correct:'첫 담당 경로 일치', handoff_correct:'담당 이전 일치',
  required_count:'필수 호출 수', required_hits:'필수 호출 충족', required_name_hits:'필수 호출 이름 일치',
  required_call_recall:'필수 호출 재현율', argument_accuracy:'인자 정확도', forbidden_calls:'금지 도구',
  extra_calls:'추가 도구', chain_order_correct:'도구 순서 일치', confirmation_correct:'확인 요청 일치',
  include_missing:'필수 문구 누락', excluded_found:'제외 문구 발견', violations:'위반',
  hallucinated_values:'근거 없는 값', glitches:'문자 이상 통계', judge_correct:'동의 판정 일치',
  expected_verdict:'기대 동의 판정', judged_verdict:'실제 동의 판정',
  proposal_missing_when_expected:'필요한 제안 누락', unconfirmed_executions:'미동의 실행',
  reply_phrase_checks:'답변 문구 검사', reply_safety_text_checks:'답변 안전 문구 검사'
};
let currentRun = null, currentScenario = null, filtered = [];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = String(text);
  return e;
}
function add(parent, tag, cls, text) {
  const e = el(tag, cls, text);
  parent.append(e);
  return e;
}
function detail(parent, title, open = false, cls = '') {
  const d = add(parent, 'details', cls);
  d.open = open;
  add(d, 'summary', '', title);
  return d;
}
function badge(parent, text, cls = '') { add(parent, 'span', 'badge ' + cls, text); }
function statusBadge(parent, s) { badge(parent, s.success ? '통과' : '실패', s.success ? 'pass' : 'fail'); }
function table(parent, fields) {
  const t = add(parent, 'table');
  for (const [key, val] of Object.entries(obj(fields))) {
    const row = add(t, 'tr');
    add(row, 'th', '', labels[key] ? labels[key] + ' (' + key + ')' : key);
    add(add(row, 'td'), 'pre', '', pretty(val));
  }
}
function actionText(action) {
  action = obj(action);
  const name = action.action || action.name || action.kind || '미저장';
  const args = Object.entries(obj(action.arguments || action.args));
  return name + '(' + args.map(([k,v]) => k + '=' + compact(v)).join(', ') + ')';
}
function runLabel(r) {
  return r.name + (r.supported
    ? ' · v2 assist ' + (r.assist === 'on' ? '켬' : '끔') + ' · ' + r.data_version + ' · ' + r.passed + '/' + r.total
    : ' · ' + r.reason);
}
function urlHash(s) {
  return '#' + encodeURIComponent(currentRun.name) + '/' + encodeURIComponent(s.id)
    + (s.attempt_index !== undefined ? '/' + encodeURIComponent(s.attempt_index) : '');
}
function scenarioList() {
  const list = $('scenario-list'), term = $('filter').value.toLowerCase();
  list.replaceChildren();
  filtered = arr(currentRun?.scenarios).filter(s =>
    (!$('failed-only').checked || !s.success) &&
    [s.id, s.description, s.customer_id, s.category].join(' ').toLowerCase().includes(term));
  for (const s of filtered) {
    const b = add(list, 'button', 'scenario-row' + (s === currentScenario ? ' active' : ''));
    b.type = 'button';
    b.setAttribute('aria-current', s === currentScenario ? 'true' : 'false');
    add(b, 'strong', '', s.id);
    statusBadge(b, s);
    if (s.attempt_index !== undefined) badge(b, '시도 ' + s.attempt_index);
    const desc = add(b, 'span', 'one-line', s.description);
    desc.title = s.description;
    b.onclick = () => {
      selectScenario(s);
      if (matchMedia('(max-width:720px)').matches) $('main').scrollIntoView({block:'start'});
    };
  }
  if (!filtered.length) add(list, 'p', 'muted', '조건에 맞는 시나리오가 없습니다.');
}
function selectRun(run) {
  currentRun = run;
  $('run-picker').value = run.name;
  $('run-meta').textContent = runLabel(run) + (run.empty_scenarios ? ' · 대화 없는 항목 ' + run.empty_scenarios + '개 제외' : '');
  currentScenario = null;
  scenarioList();
  selectScenario(filtered[0] || null);
}
function selectScenario(s, updateHash = true) {
  currentScenario = s;
  scenarioList();
  render();
  if (s && updateHash) location.hash = urlHash(s);
  $('main').scrollTop = 0;
}
function message(parent, text, customer = false) {
  const m = add(parent, 'div', 'message' + (customer ? ' customer' : ''));
  add(m, 'div', 'avatar', customer ? '고객' : '● 상담 에이전트');
  add(m, 'div', 'bubble' + (!text ? ' empty' : ''), text || '(답변 없음)');
  return m;
}
function renderCalls(parent, t) {
  const calls = arr(t.calls).filter(c => c && typeof c === 'object');
  const tools = calls.filter(c => c.kind === 'tool');
  if (!calls.length) return;
  const block = detail(parent, '조회 도구 ' + tools.length + '건 · 전체 호출 ' + calls.length + '건');
  for (const c of calls) {
    const handoff = c.kind === 'handoff' || ['transfer_back_to_router', 'transfer_back_to_supervisor'].includes(c.name);
    const d = detail(block, (handoff ? '↩ 담당 이전: ' : '🔧 ') + actionText(c));
    const summary = d.firstChild;
    if (c.prefetched) badge(summary, '사전 조회');
    if (c.output_error || obj(c.output).ok === false || c.scope_error) badge(summary, '오류', 'fail');
    if (c.irreversible) badge(summary, '확인 대상 도구');
    if (c.fault_injected) badge(summary, '장애 주입', 'fail');
    table(d, {arguments:c.arguments, output:c.output, ...(c.changes !== undefined ? {'DB 변경':c.changes} : {}),
      ...(c.total_ms !== undefined ? {total_ms:c.total_ms} : {})});
  }
}
function renderEvents(parent, t) {
  const decision = obj(t.action_decision);
  if (Object.keys(decision).length) add(parent, 'div', 'event', '제안(decide_action): ' + actionText(decision));
  for (const event of arr(t.pending_events)) {
    if (obj(event).event !== 'proposed') continue;
    const p = obj(event.proposal);
    if (!Object.keys(decision).length) add(parent, 'div', 'event', (t.consent_mode === 'tool_interrupt' ? '실행 전 확인: ' : '제안(decide_action): ') + actionText(p));
  }
  const j = obj(t.consent_judgment), sim = obj(t.simulated_customer);
  if (Object.keys(j).length && t.consent_mode !== 'tool_interrupt') {
    const v = j.verdict;
    add(parent, 'div', 'event', '동의 판정: ' + (verdicts[v] || '미저장') + ' (' + (v || '?') + ')'
      + (j.reason ? ' · ' + j.reason : ''));
    const expected = {approve:'yes', reject:'no', change:'changed'}[sim.decision];
    if (expected) add(parent, 'div', 'event' + (expected !== v ? ' warning' : ''),
      '시뮬레이션 고객과 ' + (expected === v ? '일치' : '불일치') + ' · 기대 ' + expected + ' / 실제 ' + v);
  }
  for (const c of arr(t.calls).filter(c => t.consent_mode !== 'tool_interrupt' && obj(c).kind === 'tool' && c.irreversible)) {
    add(parent, 'div', 'event', '실행: ' + actionText(c));
  }
  for (const key of ['ungrounded_values', 'grounding_retries', 'force_tool_retries', 'loop_limit', 'limit_reached']) {
    if (t[key] && (!Array.isArray(t[key]) || t[key].length)) {
      add(parent, 'div', 'event warning', (key === 'ungrounded_values' ? '근거 없는 값' : key) + ': ' + compact(t[key]));
    }
  }
  const contexts = [['order_context','주문 사전 조회 기록'], ['policy_context','정책 조회 기록'],
    ['pending_at_start','턴 시작 제안'], ['pending_events','제안 처리 기록']];
  for (const [key, label] of contexts) {
    if (t[key] && compact(t[key]) !== '[]' && compact(t[key]) !== '{}') {
      add(detail(parent, label), 'pre', '', pretty(t[key]));
    }
  }
}
function renderTurnSummary(parent, summary) {
  const s = obj(summary);
  add(parent, 'div', 'turn-summary', s.line || '-');
  for (const warning of arr(s.warnings)) badge(parent, '경고 · ' + warning, 'fail');
}
function renderConsent(parent, summary) {
  const s = obj(summary);
  for (const c of arr(s.cards)) {
    const card = add(parent, 'section', 'confirmation-ui');
    add(card, 'div', 'muted', '확인카드 표시됨');
    add(card, 'h3', '', c.title);
    add(card, 'div', 'muted', '제안 ID: ' + c.proposal_id + ' · 도구: ' + c.tool);
    const fields = add(card, 'div', 'card-fields');
    for (const field of arr(c.fields)) {
      const row = add(fields, 'div', 'card-field');
      if (field.key) {
        add(row, 'strong', '', field.key + ':');
        add(row, 'span', '', field.value);
      } else add(row, 'span', 'raw-line', field.value);
    }
    const buttons = add(card, 'div', 'card-buttons');
    for (const label of ['승인', '거절']) {
      const button = add(buttons, 'button', '', label);
      button.type = 'button';
      button.disabled = true;
    }
    add(card, 'div', 'muted', '저장된 확인카드 표시 · 버튼은 동작하지 않습니다.');
  }
  const j = s.judgment;
  if (j) {
    const block = add(parent, 'section', 'event');
    add(block, 'h3', '', '판단 결과: ' + j.label + ' (' + j.verdict + ')');
    table(block, {'제안 ID':j.proposal_id, '판정 이유':j.reason, '재개 결정':j.resume_type,
      '담당자에게 전달된 메시지':j.resume_message, '메시지 출처':j.resume_source});
  }
  if (arr(s.executions).length) {
    add(parent, 'h3', '', '되돌릴 수 없는 도구 실행 내역 (실행됨 / 실행 안 됨)');
    for (const execution of s.executions) {
      const row = add(parent, 'div', 'execution-record');
      add(row, 'strong', '', execution.tool + ' · ' + execution.status);
      add(row, 'div', 'muted', '제안 ID: ' + execution.proposal_id);
      if (execution.reason) add(row, 'div', '', '이유: ' + execution.reason);
      if (execution.result_summary) add(row, 'div', '', execution.result_summary);
    }
  }
  if (s.db && s.db !== '-') {
    const block = detail(parent, '턴 DB 변경 · ' + s.db, arr(s.db_changes).length > 0);
    for (const change of arr(s.db_changes)) {
      add(block, 'strong', '', change.tool + (change.proposal_id ? ' · ' + change.proposal_id : ''));
      table(block, change.changes);
    }
  }
}
function renderComparison(parent, summary) {
  const comparison = obj(summary).comparison;
  if (!comparison) return;
  add(parent, 'h3', '', '답장-실행 대조');
  const block = add(parent, 'div', 'comparison');
  add(block, 'div', '', '실행 여부: ' + comparison.execution);
  add(block, 'pre', '', '봇 답장: ' + (comparison.reply || '(답변 없음)'));
  if (comparison.reply_claim_judgment) {
    table(block, {'답장 완료 주장 판정':comparison.reply_claim_judgment});
  }
}
function renderScenarioSummary(parent, scenario) {
  const wrapper = add(parent, 'section', 'summary-scroll');
  add(wrapper, 'h3', '', '턴별 확인·판단·실행 요약');
  const t = add(wrapper, 'table', 'summary-table');
  const head = add(add(t, 'thead'), 'tr');
  for (const title of ['턴', '카드', '판단', '실행', 'DB', '경고']) add(head, 'th', '', title);
  const body = add(t, 'tbody');
  arr(scenario.turns).forEach((turn, i) => {
    const s = obj(turn.summary), row = add(body, 'tr');
    for (const value of [i + 1, s.card, s.judgment_text, s.execution, s.db, s.warning_count ?? 0]) {
      add(row, 'td', '', value ?? '-');
    }
  });
}
function renderTurn(parent, t, i, grade) {
  t = obj(t);
  const block = add(parent, 'section', 'turn');
  const routes = arr(t.routes).map(n => (nodes[n] ? nodes[n] + ' ' : '') + '(' + n + ')').join(' → ');
  add(block, 'div', 'turn-title', '턴 ' + (i + 1) + ' · 담당 노드: ' + (routes || '미저장'));
  renderTurnSummary(block, t.summary);
  const internal = t.internal_decision_request;
  const customer = internal ? detail(block, '내부 판단 요청') : message(block, t.user || '', true);
  if (internal) add(customer, 'pre', '', pretty(internal));
  const sim = obj(t.simulated_customer);
  if (!internal && Object.keys(sim).length) {
    add(customer, 'div', 'caption', '시뮬레이션 고객 결정: ' + (decisions[sim.decision] || sim.decision || '미저장'));
    if (t.scripted_user !== undefined && t.scripted_user !== t.user) {
      add(detail(customer, '원래 대본'), 'pre', '', t.scripted_user);
    }
  }
  renderConsent(block, t.summary);
  renderCalls(block, t);
  renderEvents(block, t);
  const visible = arr(t.visible).filter(v => typeof v === 'string' && v.trim());
  if (!visible.length && typeof t.reply === 'string' && t.reply.trim()) visible.push(t.reply);
  if (visible.length) visible.forEach(v => message(block, v));
  else message(block, '');
  if (t.confirmation_card && t.consent_mode !== 'tool_interrupt') add(detail(block, '확인 카드', true, 'card'), 'pre', '', pretty(t.confirmation_card));
  renderComparison(block, t.summary);
  const problems = arr(t.problems);
  const graded = detail(block, '턴 ' + (i + 1) + ' 채점' + (problems.length ? ' · 확인할 항목 ' + problems.length + '개' : ''),
    problems.length > 0, problems.length ? 'problem' : '');
  if (problems.length) {
    const ul = add(graded, 'ul', 'warning');
    problems.forEach(p => add(ul, 'li', '', p));
  }
  if (Object.keys(obj(grade)).length) table(graded, grade);
  else add(graded, 'p', 'muted', '턴 채점 미저장');
}
function render() {
  const box = $('conversation'), s = currentScenario;
  box.replaceChildren();
  if (!s) { add(box, 'p', 'muted', '표시할 대화가 없습니다.'); return; }
  const header = add(box, 'header', 'scenario-header');
  const h = add(header, 'h2', '', s.id + ' · ' + s.category);
  statusBadge(h, s);
  if (s.attempt_index !== undefined) badge(h, '시도 ' + s.attempt_index);
  add(header, 'p', '', s.description);
  const wall = typeof s.wall_time_s === 'number' ? s.wall_time_s.toFixed(2) + '초' : '미저장';
  add(header, 'p', 'muted', '고객 ' + s.customer_id + ' · 총 소요 시간 ' + wall + ' · ' + (s.status || '상태 미저장'));
  for (const warning of arr(s.warnings)) badge(header, '경고 · ' + warning, 'fail');
  renderScenarioSummary(box, s);
  if (!s.success) {
    const f = add(box, 'section', 'failure');
    add(f, 'h3', '', '실패 원인');
    const ul = add(f, 'ul');
    for (const reason of (s.reasons.length ? s.reasons : ['저장된 success=false · 세부 원인 미저장'])) {
      add(ul, 'li', '', reason);
    }
  }
  arr(s.turns).forEach((t, i) => renderTurn(box, t, i, arr(obj(s.grade).turns)[i]));
  const footer = add(box, 'footer'), g = obj(s.grade), state = obj(g.final_state);
  add(footer, 'h3', '', '최종 DB 기대값 vs 실제');
  for (const error of arr(state.errors)) {
    const block = add(footer, 'div', 'event');
    add(block, 'strong', '', error.path || '(경로 기록 없음)');
    table(block, {'기대값':error.expected, '실제':error.actual,
      ...(error.before !== undefined ? {'이전':error.before, '이후':error.after} : {})});
  }
  add(footer, 'h3', '', '최종 상태 및 총 위반');
  table(footer, {'최종 상태 일치 (match)':state.match ?? '미저장',
    '최종 상태 오류 (errors)':state.errors ?? '미저장', '총 위반 (violations)':g.violations ?? '미저장'});
}
function fromHash() {
  let parts;
  try { parts = location.hash.slice(1).split('/').map(decodeURIComponent); }
  catch { return false; }
  const r = DATA.runs.find(r => r.name === parts[0] && r.supported);
  if (!r) return false;
  currentRun = r;
  $('run-picker').value = r.name;
  $('run-meta').textContent = runLabel(r);
  const target = r.scenarios.find(s => s.id === parts[1] && (parts[2] === undefined || String(s.attempt_index) === parts[2]));
  if (target) {
    $('filter').value = '';
    $('failed-only').checked = false;
    selectScenario(target, false);
  } else selectRun(r);
  return true;
}
for (const r of DATA.runs) {
  const o = add($('run-picker'), 'option', '', runLabel(r));
  o.value = r.name;
  o.disabled = !r.supported;
}
$('run-picker').onchange = () => selectRun(DATA.runs.find(r => r.name === $('run-picker').value));
for (const id of ['filter', 'failed-only']) $(id).addEventListener('input', () => {
  scenarioList();
  if (!filtered.includes(currentScenario)) selectScenario(filtered[0] || null);
});
document.addEventListener('keydown', e => {
  if (!['ArrowUp', 'ArrowDown'].includes(e.key) || ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
  e.preventDefault();
  const index = filtered.indexOf(currentScenario), next = filtered[index + (e.key === 'ArrowDown' ? 1 : -1)];
  if (next) {
    selectScenario(next);
    $('scenario-list').querySelector('.active')?.scrollIntoView({block:'nearest'});
  }
});
window.addEventListener('hashchange', () => {
  if (currentScenario && location.hash === urlHash(currentScenario)) return;
  fromHash();
});
if (!fromHash()) {
  const first = DATA.runs.find(r => r.supported);
  if (first) selectRun(first);
  else { render(); $('run-picker').selectedIndex = -1; }
}
</script>
</body>
</html>
'''


def render_html(data):
    # Also prevent HTML script/comment tokenizer states within untrusted text.
    payload = compact(data).replace("<", "\\u003c")
    return HTML.replace("__CHAT_DATA__", payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="*", help="Optional run folder names")
    parser.add_argument("--out", type=Path, default=ROOT / "chat_view.html")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out.is_relative_to((ROOT / "runs").resolve()):
        parser.error("runs/ 안에는 출력할 수 없습니다 (읽기 전용)")
    data = load_data(ROOT, args.runs)
    out.write_text(render_html(data), encoding="utf-8")
    for run in data["runs"]:
        if run["supported"]:
            print(f"포함: {run['name']} — {len(run['scenarios'])}개 시나리오" +
                  (f" (대화 없는 {run['empty_scenarios']}개 제외)" if run["empty_scenarios"] else ""))
    for note in data["notes"]:
        print("건너뜀: " + note)
    included = [r for r in data["runs"] if r["supported"]]
    print(f"합계: {len(included)}개 실행, {sum(len(r['scenarios']) for r in included)}개 시나리오")
    print(f"출력: {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
