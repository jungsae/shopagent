#!/usr/bin/env python3
"""Build failure_classification_v31.md from rescored_v2 results + scenarios.json."""
import json
from pathlib import Path
from collections import Counter, defaultdict

BASE = Path.home() / "llama.cpp/bench/shopagent"
OUT = BASE / "docs/reports/failure_classification_v31.md"
RUNS = [
    "g12-native-r1",
    "g12-native-r2",
    "g12-native-r3",
    "g12-native-ngl0-r1",
    "clef-g12-r1",
    "jev-g12-r1",
    "pplx-g12-r1",
]
BUCKETS = [
    ("A", "escalate_to_human 누락 / human 라우트 미실행"),
    ("B", "handoff/transfer_back_to_supervisor 캡(현재 1회) 가능"),
    ("C", "step>=8 또는 tools>=6 리밋 적중"),
    ("D", "missing required reply / reply_must_include"),
    ("E", "final_state mismatch"),
    ("F", "hallucinated_value"),
    ("G", "missing refusal phrase"),
    ("H", "other"),
]
PRIORITY = [b[0] for b in BUCKETS]

with open(BASE / "data/scenarios.json") as f:
    _raw = json.load(f)
    SCENARIOS = {s["id"]: s for s in (_raw.get("scenarios") if isinstance(_raw, dict) else _raw)}


def expected_calls(sid):
    sc = SCENARIOS.get(sid) or {}
    names = []
    for t in sc.get("turns") or []:
        exp = t.get("expect") or {}
        for c in exp.get("required_calls") or []:
            if isinstance(c, dict):
                names.append(c.get("name"))
            else:
                names.append(c)
    return names


def expects_escalate(sid):
    return "escalate_to_human" in expected_calls(sid)


def evidence(s):
    turns = s.get("turns") or []
    ev = {
        "routes": [],
        "max_steps": 0,
        "max_tools": 0,
        "max_transfers": 0,
        "loop_limit": False,
        "tool_names": [],
        "has_escalate": False,
        "transfer_back": 0,
        "transfer_cap_error": False,
        "include_missing": [],
        "hallucinated_values": [],
        "empty_reply_turns": 0,
    }
    for t in turns:
        ev["routes"].extend(t.get("routes") or [])
        ev["max_steps"] = max(ev["max_steps"], int(t.get("steps") or 0))
        ev["max_tools"] = max(ev["max_tools"], int(t.get("tool_count") or 0))
        ev["max_transfers"] = max(ev["max_transfers"], int(t.get("transfers") or 0))
        if t.get("loop_limit"):
            ev["loop_limit"] = True
        if not (t.get("reply") or "").strip():
            ev["empty_reply_turns"] += 1
        for c in t.get("calls") or []:
            if not isinstance(c, dict):
                continue
            name = c.get("name") or ""
            ev["tool_names"].append(name)
            if name == "escalate_to_human":
                ev["has_escalate"] = True
            if name == "transfer_back_to_supervisor":
                ev["transfer_back"] += 1
            out = c.get("output")
            out_s = json.dumps(out, ensure_ascii=False) if not isinstance(out, str) else (out or "")
            if "allowed once" in out_s or "transfer_back_to_supervisor allowed once" in out_s:
                ev["transfer_cap_error"] = True
    g = s.get("grade") or {}
    for tg in g.get("turns") or []:
        ev["include_missing"].extend(tg.get("include_missing") or [])
        ev["hallucinated_values"].extend(tg.get("hallucinated_values") or [])
    return ev


def classify(s):
    rft = list(s.get("remaining_failure_types") or [])
    g = s.get("grade") or {}
    violations = g.get("violations") or {}
    final_state = g.get("final_state") or {}
    ev = evidence(s)
    sid = s["id"]
    labels = []
    notes = []

    # A
    human_routed = "human" in ev["routes"]
    need_esc = expects_escalate(sid) or (
        isinstance(final_state, dict)
        and any(
            (e.get("path") == "escalations")
            for e in (final_state.get("errors") or [])
            if isinstance(e, dict)
        )
    )
    rft_handoff = any("handoff tool not called" in t.lower() for t in rft)
    if (need_esc or (human_routed and rft_handoff) or (human_routed and not ev["has_escalate"] and any("final_state" in t.lower() for t in rft))) and not ev["has_escalate"]:
        labels.append("A")
        notes.append(f"human_routed={human_routed} need_esc={need_esc} has_escalate={ev['has_escalate']} rft_handoff={rft_handoff}")

    # B
    if ev["transfer_cap_error"] or (ev["transfer_back"] >= 1 and ev["max_transfers"] >= 1 and (ev["loop_limit"] or rft_handoff)):
        labels.append("B")
        notes.append(f"transfers={ev['max_transfers']} transfer_back_calls={ev['transfer_back']} cap_err={ev['transfer_cap_error']}")

    # C
    if ev["loop_limit"] or ev["max_steps"] >= 8 or ev["max_tools"] >= 6:
        labels.append("C")
        notes.append(f"loop_limit={ev['loop_limit']} steps={ev['max_steps']} tools={ev['max_tools']}")

    # D
    if any("missing required reply" in t.lower() for t in rft) or ev["include_missing"] or ev["empty_reply_turns"]:
        labels.append("D")
        notes.append(f"include_missing={ev['include_missing'][:5]} empty_reply_turns={ev['empty_reply_turns']}")

    # E
    if any("final_state" in t.lower() for t in rft) or (isinstance(final_state, dict) and final_state.get("match") is False):
        labels.append("E")
        errs = (final_state.get("errors") or [])[:2] if isinstance(final_state, dict) else []
        notes.append(f"final_state_errors={json.dumps(errs, ensure_ascii=False)[:200]}")

    # F
    if any("hallucin" in t.lower() for t in rft) or (violations.get("hallucinated_value") or 0) > 0 or ev["hallucinated_values"]:
        labels.append("F")
        notes.append(f"hallucinated_values={ev['hallucinated_values'][:5]} viol={violations.get('hallucinated_value')}")

    # G
    if any("refusal" in t.lower() for t in rft):
        labels.append("G")
        notes.append("rft missing refusal phrase")

    # H
    if not labels:
        labels.append("H")
        notes.append(f"unclassified rft={rft} tools={ev['tool_names'][:8]}")
    elif any("required call" in t.lower() for t in rft) and not any(x in labels for x in ("A", "B", "F", "G")):
        # annotate only
        notes.append("also: required call missing/wrong args")

    primary = next(b for b in PRIORITY if b in labels)
    return primary, labels, notes, ev, rft


def analyze(run):
    with open(BASE / "runs" / "archive" / run / "results_rescored_v2.json") as f:
        data = json.load(f)
    fails = [s for s in data["scenarios"] if not s.get("success")]
    rows = []
    pc, mc = Counter(), Counter()
    for s in fails:
        primary, labels, notes, ev, rft = classify(s)
        pc[primary] += 1
        for L in labels:
            mc[L] += 1
        rows.append({
            "id": s["id"],
            "category": s.get("category"),
            "old_success": s.get("old_success"),
            "primary": primary,
            "labels": labels,
            "rft": rft,
            "routes": ev["routes"],
            "steps": ev["max_steps"],
            "tools": ev["max_tools"],
            "transfers": ev["max_transfers"],
            "transfer_back": ev["transfer_back"],
            "loop_limit": ev["loop_limit"],
            "has_escalate": ev["has_escalate"],
            "tool_names": ev["tool_names"],
            "include_missing": ev["include_missing"],
            "hallucinated_values": ev["hallucinated_values"],
            "notes": notes,
            "expected_calls": expected_calls(s["id"]),
        })
    return {
        "run": run,
        "n": len(data["scenarios"]),
        "n_pass": sum(1 for s in data["scenarios"] if s.get("success")),
        "n_fail": len(fails),
        "primary": dict(pc),
        "multi": dict(mc),
        "rows": rows,
    }


results = [analyze(r) for r in RUNS]
r1 = next(r for r in results if r["run"] == "g12-native-r1")

lines = []
lines.append("# ShopAgent 실패 분류 v3.1 (pre-GPU)")
lines.append("")
lines.append("- 작성 목적: rescore 이후 NEW-fail 원인을 버킷 A–H로 분류해 그래프 구조 수정 우선순위를 정함.")
lines.append("- 데이터: `runs/archive/*/results_rescored_v2.json` (7개) + `data/scenarios.json` 기대치.")
lines.append("- 규칙: JSON 증거(routes/steps/tool_count/transfers/loop_limit/calls/grade/RFT)만 사용. 추정 시 명시.")
lines.append("- GPU/remeasure 없음.")
lines.append("")
lines.append("## 버킷 정의")
lines.append("")
lines.append("| 버킷 | 의미 |")
lines.append("|---|---|")
for k, v in BUCKETS:
    lines.append(f"| {k} | {v} |")
lines.append("")
lines.append("Primary = 우선순위 A>B>C>D>E>F>G>H 중 해당 시나리오에 붙은 첫 버킷. Multi = 한 시나리오에 여러 버킷이 동시에 붙을 수 있음.")
lines.append("")
lines.append("## 전체 런 요약 (primary counts)")
lines.append("")
header = "| run | pass | fail |" + "".join(f" {k} |" for k, _ in BUCKETS)
lines.append(header)
lines.append("|" + "---|" * (3 + len(BUCKETS)))
for r in results:
    cells = [r["run"], str(r["n_pass"]), str(r["n_fail"])]
    for k, _ in BUCKETS:
        cells.append(str(r["primary"].get(k, 0)))
    lines.append("| " + " | ".join(cells) + " |")
lines.append("")
lines.append("## 전체 런 요약 (multi-label counts)")
lines.append("")
header = "| run | fail |" + "".join(f" {k} |" for k, _ in BUCKETS)
lines.append(header)
lines.append("|" + "---|" * (2 + len(BUCKETS)))
for r in results:
    cells = [r["run"], str(r["n_fail"])]
    for k, _ in BUCKETS:
        cells.append(str(r["multi"].get(k, 0)))
    lines.append("| " + " | ".join(cells) + " |")
lines.append("")
lines.append("## g12-native-r1 상세 (NEW-fail = success=False, n={})".format(r1["n_fail"]))
lines.append("")
lines.append(f"- pass={r1['n_pass']}, fail={r1['n_fail']}")
lines.append(f"- primary: {json.dumps(r1['primary'], ensure_ascii=False)}")
lines.append(f"- multi: {json.dumps(r1['multi'], ensure_ascii=False)}")
lines.append("")
lines.append("| id | primary | labels | routes | steps | tools | transfers | loop_limit | escalate? | RFT | 증거 메모 |")
lines.append("|---|---|---|---|---:|---:|---:|---|---|---|---|")
for row in r1["rows"]:
    lines.append(
        "| {id} | {primary} | {labels} | {routes} | {steps} | {tools} | {transfers} | {loop} | {esc} | {rft} | {notes} |".format(
            id=row["id"],
            primary=row["primary"],
            labels=",".join(row["labels"]),
            routes=",".join(row["routes"]),
            steps=row["steps"],
            tools=row["tools"],
            transfers=row["transfers"],
            loop=row["loop_limit"],
            esc=row["has_escalate"],
            rft="; ".join(row["rft"]),
            notes="; ".join(row["notes"])[:180].replace("|", "/"),
        )
    )
lines.append("")
lines.append("### g12-native-r1 시나리오별 근거")
lines.append("")
for row in r1["rows"]:
    lines.append(f"#### {row['id']} → primary **{row['primary']}** (labels={row['labels']})")
    lines.append(f"- category={row['category']}, old_success={row['old_success']}")
    lines.append(f"- routes={row['routes']}, steps={row['steps']}, tool_count={row['tools']}, transfers={row['transfers']}, loop_limit={row['loop_limit']}")
    lines.append(f"- tools_called={row['tool_names']}")
    lines.append(f"- expected_required_names={row['expected_calls']}")
    lines.append(f"- has_escalate_to_human={row['has_escalate']}")
    if row["include_missing"]:
        lines.append(f"- include_missing={row['include_missing']}")
    if row["hallucinated_values"]:
        lines.append(f"- hallucinated_values={row['hallucinated_values']}")
    lines.append(f"- RFT={row['rft']}")
    lines.append(f"- notes={row['notes']}")
    lines.append("")

lines.append("## 분류에서 본 그래프 수정 함의")
lines.append("")
a_total = sum(r["primary"].get("A", 0) for r in results)
b_total = sum(r["primary"].get("B", 0) for r in results)
c_total = sum(r["primary"].get("C", 0) for r in results)
f_total = sum(r["primary"].get("F", 0) for r in results)
lines.append(f"1. **Deterministic human path (A)**: g12-native-r1에서 handoff-05/policy-06/policy-07이 route=`human`인데 `escalate_to_human` 미호출(프로즈만). 전 런 primary A 합≈{a_total}. → 그래프가 human 라우트 시 도구를 Gemma에 맡기지 말고 확정 실행해야 함.")
lines.append(f"2. **transfer_back cap 1→3 (B)**: g12-native-r1 NEW-fail에서는 transfers=0이라 캡 적중 증거 없음(primary B={r1['primary'].get('B',0)}). 다만 구조 제약으로 멀티 핸드오프 턴을 막을 수 있어 **유지 권고**(useless로 단정할 증거 없음). 전 런 primary B 합≈{b_total}.")
lines.append(f"3. **step/tool limit (C)**: g12-native-r1 fail에서 loop_limit/steps>=8/tools>=6 적중 없음(primary C={r1['primary'].get('C',0)}). 전 런 primary C 합≈{c_total}.")
lines.append(f"4. **Post-answer grounding (F)**: chain-01/03/06 등에서 `2026-10-29` 환각(도구 결과에 없는 마감일). 전 런 primary F 합≈{f_total}. → 최종 답변의 날짜/금액이 tool/user에 없으면 재시도 또는 마크.")
lines.append("5. **D/G**: 주로 모델 응답 품질(필수 문구/거절 문구). 그래프만으로 완전 해결은 어려우나 grounding/human path와 병행.")
lines.append("6. **Irreversible gate (optional)**: single-12는 확인 질문 후 `cancel_order` 미실행 → final_state 실패. 확인 게이트는 follow-up으로 문서화 가능.")
lines.append("")
lines.append("## 기타 런 fail id 목록 (요약)")
lines.append("")
for r in results:
    if r["run"] == "g12-native-r1":
        continue
    ids = ", ".join(f"{row['id']}({row['primary']})" for row in r["rows"])
    lines.append(f"- **{r['run']}** fail={r['n_fail']}: {ids}")
lines.append("")
lines.append("---")
lines.append("생성 스크립트: build_classification_v31.py (CPU only)")

OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("WROTE", OUT)
print("R1 primary", r1["primary"])
print("R1 multi", r1["multi"])
for row in r1["rows"]:
    print(row["id"], row["primary"], row["labels"])
# also dump json sidecar for codex context (new name)
side = BASE / "docs/reports/failure_classification_v31.json"
side.write_text(json.dumps({"buckets": BUCKETS, "runs": [
    {k: v for k, v in r.items() if k != "rows"} | {"failures": r["rows"]}
    for r in results
]}, ensure_ascii=False, indent=2), encoding="utf-8")
print("WROTE", side)
