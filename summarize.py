"""Read saved run folders and print per-config JSON summaries. No model or GPU access."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import statistics


VIOLATIONS = ("unconfirmed_action", "ineligible_action", "pii_leak", "card_request", "injection_followed", "hallucinated_value")
GLITCHES = ("han", "kana", "cyrillic", "other_nonlatin", "latin_words", "think_leak", "total")


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def distribution(values):
    values = [v for v in values if v is not None]
    return {"n": len(values), "median": statistics.median(values) if values else None, "p90": percentile(values, .9), "p95": percentile(values, .95)}


def latency(turn):
    if "latency" in turn:
        split = turn["latency"]
        return {"turn_total": split.get("turn_total_ms"), "judge_total": split["judge"].get("total_ms"), "judge_ttft": split["judge"].get("ttft_ms"), "answer_total": split["answer"].get("total_ms"), "answer_ttft": split["answer"].get("ttft_ms"), "turn_to_final_reply_ttft": split.get("turn_to_final_reply_ttft_ms")}
    calls = turn.get("llm_calls", [])
    judges = [c for c in calls if c.get("node") == "supervisor"]
    answers = [c for c in calls if c.get("node") != "supervisor"]
    return {"turn_total": turn.get("total_ms"), "judge_total": sum(c["total_ms"] for c in judges) if judges and all(c.get("total_ms") is not None for c in judges) else None, "judge_ttft": judges[0].get("ttft_ms") if judges else None, "answer_total": sum(c["total_ms"] for c in answers) if answers and all(c.get("total_ms") is not None for c in answers) else None, "answer_ttft": turn.get("final_reply_ttft_ms"), "turn_to_final_reply_ttft": turn.get("turn_to_final_reply_ttft_ms")}


def reliability(runs):
    full = [r for r in runs if r.get("mode", "full" if r.get("stage") == "3-2" else "smoke") == "full"]
    indices = [r.get("repeat_index") for r in full]
    ids = [set(s["id"] for s in r.get("scenarios", [])) for r in full]
    aligned = bool(ids) and bool(ids[0]) and all(s == ids[0] for s in ids)
    unique = len(indices) == len(set(indices))
    valid = aligned and unique and len(full) >= 2
    outcomes = {sid: [next(s for s in r["scenarios"] if s["id"] == sid).get("success", False) for r in full] for sid in ids[0]} if valid else {}
    return {"repeat_indices": indices, "scenario_count": len(outcomes), "pass^3": ratio(sum(all(v) for v in outcomes.values()), len(outcomes)) if len(full) == 3 else None, "pass>=1": ratio(sum(any(v) for v in outcomes.values()), len(outcomes)), "note": "Missing/unrun scenarios count as failures. pass^3 requires exactly three aligned full runs with distinct repeat indices."}


def summarize_group(runs):
    rows = [s for r in runs for s in r.get("scenarios", [])]
    grades = [g for s in rows for g in s.get("grade", {}).get("turns", [])]
    turns = [t for s in rows for t in s.get("turns", [])]
    expected_turns = sum(s.get("expected_turns", len(s.get("grade", {}).get("turns", s.get("turns", [])))) for s in rows)
    required = sum(s.get("expected_required_calls", sum(g.get("required_count", 0) for g in s.get("grade", {}).get("turns", []))) for s in rows)
    name_hits = sum(g.get("required_name_hits", 0) for g in grades)
    hits = sum(g.get("required_hits", 0) for g in grades)
    by_category = {}
    for category in sorted({s.get("category", s["id"].split("-", 1)[0]) for s in rows}):
        selected = [s for s in rows if s.get("category", s["id"].split("-", 1)[0]) == category]
        successes = sum(s.get("success", False) for s in selected)
        by_category[category] = {"successes": successes, "scenarios": len(selected), "success_rate": ratio(successes, len(selected))}
    samples = {"all": defaultdict(list), "comparable": defaultdict(list)}
    for scenario in rows:
        for turn in scenario.get("turns", []):
            if turn.get("status") != "complete":
                continue
            for key, value in latency(turn).items():
                samples["all"][key].append(value)
                if scenario.get("latency_comparable") is True and turn.get("latency_comparable") is True:
                    samples["comparable"][key].append(value)
    keys = ("turn_total", "judge_total", "judge_ttft", "answer_total", "answer_ttft", "turn_to_final_reply_ttft")
    peaks = {}
    for run in runs:
        for gpu, value in run.get("peak_vram_mib_by_gpu", {}).items():
            peaks[gpu] = max(peaks.get(gpu, 0), value)
    walls = []
    for run in runs:
        wall = run.get("wall_time_s")
        if wall is None and run.get("started") and run.get("finished"):
            wall = (datetime.fromisoformat(run["finished"]) - datetime.fromisoformat(run["started"])).total_seconds()
        if wall is not None:
            walls.append(wall)
    glitches = {key: sum(g.get("glitches", {}).get(key, 0) for g in grades) for key in GLITCHES}
    successes = sum(s.get("success", False) for s in rows)
    first = runs[0]
    result = {
        "config": first["config"],
        "gemma_ngl": first.get("gemma_ngl", first.get("ngl")),
        "reference_offload": first.get("reference_offload", False),
        "mode": first.get("mode", "full" if first.get("stage") == "3-2" else "smoke"),
        "dataset_sha256": first.get("dataset_sha256"),
        "runs": len(runs),
        "run_statuses": dict(Counter(r.get("status", "unknown") for r in runs)),
        "scenarios": len(rows),
        "scored_scenarios": sum("grade" in s for s in rows),
        "successes": successes,
        "success_rate": ratio(successes, len(rows)),
        "by_category": by_category,
        "expected_turns": expected_turns,
        "graded_turns": len(grades),
        "route_accuracy": ratio(sum(g.get("route_correct", False) for g in grades), expected_turns),
        "tool_recall": ratio(name_hits, required),
        "argument_accuracy": ratio(hits, name_hits),
        "required_calls": required,
        "required_name_hits": name_hits,
        "required_argument_hits": hits,
        "violations": {key: sum(s.get("grade", {}).get("violations", {}).get(key, 0) for s in rows) for key in VIOLATIONS},
        "glitches": glitches,
        "glitch_turns": sum(g.get("glitches", {}).get("total", 0) > 0 for g in grades),
        "glitch_turn_rate": ratio(sum(g.get("glitches", {}).get("total", 0) > 0 for g in grades), len(grades)),
        "steps_per_turn": distribution([t.get("steps") for t in turns]),
        "loop_limit_count": sum(bool(t.get("loop_limit")) for t in turns),
        "latency_ms_per_turn": {group: {key: distribution(samples[group][key]) for key in keys} for group in samples},
        "peak_vram_mib_by_gpu": peaks,
        "vram_peak_mib": max(peaks.values()) if peaks else None,
        "contended_count": sum(s.get("contended") is True for s in rows),
        "contention_unknown_count": sum(s.get("contended") is None for s in rows),
        "contended_runs": sum(r.get("contended") is True for r in runs),
        "wall_time_s": sum(walls) if walls else None,
        "wall_time_runs": len(walls),
    }
    if first["config"] == "g12-native":
        result["reliability"] = reliability(runs)
    return result


def summarize(folders):
    groups = defaultdict(list)
    seen = set()
    for folder in folders:
        path = (Path(folder) / "results.json").resolve()
        if path in seen:
            raise ValueError("Duplicate run folder: " + str(folder))
        seen.add(path)
        run = json.loads(path.read_text(encoding="utf-8"))
        mode = run.get("mode", "full" if run.get("stage") == "3-2" else "smoke")
        key = (run["config"], run.get("gemma_ngl", run.get("ngl")), run.get("reference_offload", False), mode, run.get("dataset_sha256"))
        groups[key].append(run)
    return [summarize_group(runs) for runs in groups.values()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(summarize(args.folders), ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Cannot summarize runs: {error}\n")
