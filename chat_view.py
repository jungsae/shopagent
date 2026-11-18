"""Build an offline conversation viewer from saved benchmark results (stdlib only)."""

import argparse
from collections import Counter
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TURN_FIELDS = "consent_mode user scripted_user simulated_customer routes reply visible confirmation_card pending_at_start pending_events action_decision consent_judgment order_context policy_context tool_count model_call_count ungrounded_values grounding_retries force_tool_retries loop_limit limit_reached transfers status error total_ms".split()
CALL_FIELDS = "kind name arguments output prefetched scope_error fault_injected irreversible total_ms".split()


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


def clean_turn(turn):
    turn = mapping(turn)
    result = {key: clean(turn[key]) for key in TURN_FIELDS if key in turn}
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
               "grade": clean(grade), "turns": [clean_turn(t) for t in turns],
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
@media(prefers-color-scheme:dark){:root{--bg:#17222d;--panel:#253240;--text:#e5edf4;--muted:#a6b7c8;--line:#405163;--yellow:#d4b748;--bad:#ff93a2;--badbg:#412a33;--good:#83dca9;--selected:#314d66}.pass{background:#214936}}
@media(max-width:720px){.shell{height:auto;display:block}.sidebar{width:100%;height:42dvh;border-right:0;border-bottom:1px solid var(--line)}.controls{padding:10px 14px}h1{margin-bottom:6px}.conversation{padding:18px 14px 50px}main{overflow:visible}.bubble{max-width:94%}}
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
const nodes = {supervisor:'총괄', order_delivery:'주문·배송', refund_exchange:'반품·교환',
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
    b.onclick = () => selectScenario(s);
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
    const handoff = c.kind === 'handoff' || c.name === 'transfer_back_to_supervisor';
    const d = detail(block, (handoff ? '↩ 담당 이전: ' : '🔧 ') + actionText(c));
    const summary = d.firstChild;
    if (c.prefetched) badge(summary, '사전 조회');
    if (c.output_error || obj(c.output).ok === false || c.scope_error) badge(summary, '오류', 'fail');
    if (c.irreversible) badge(summary, '되돌릴 수 없는 실행', 'fail');
    if (c.fault_injected) badge(summary, '장애 주입', 'fail');
    table(d, {arguments:c.arguments, output:c.output, ...(c.total_ms !== undefined ? {total_ms:c.total_ms} : {})});
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
  if (Object.keys(j).length) {
    const v = j.verdict;
    add(parent, 'div', 'event', '동의 판정: ' + (verdicts[v] || '미저장') + ' (' + (v || '?') + ')'
      + (j.reason ? ' · ' + j.reason : ''));
    const expected = {approve:'yes', reject:'no', change:'changed'}[sim.decision];
    if (expected) add(parent, 'div', 'event' + (expected !== v ? ' warning' : ''),
      '시뮬레이션 고객과 ' + (expected === v ? '일치' : '불일치') + ' · 기대 ' + expected + ' / 실제 ' + v);
  }
  for (const c of arr(t.calls).filter(c => obj(c).kind === 'tool' && c.irreversible)) {
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
function renderTurn(parent, t, i, grade) {
  t = obj(t);
  const block = add(parent, 'section', 'turn');
  const routes = arr(t.routes).map(n => (nodes[n] ? nodes[n] + ' ' : '') + '(' + n + ')').join(' → ');
  add(block, 'div', 'turn-title', '턴 ' + (i + 1) + ' · 담당 노드: ' + (routes || '미저장'));
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
  renderCalls(block, t);
  renderEvents(block, t);
  const visible = arr(t.visible).filter(v => typeof v === 'string' && v.trim());
  if (!visible.length && typeof t.reply === 'string' && t.reply.trim()) visible.push(t.reply);
  if (visible.length) visible.forEach(v => message(block, v));
  else message(block, '');
  if (t.confirmation_card) add(detail(block, '확인 카드', true, 'card'), 'pre', '', pretty(t.confirmation_card));
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
