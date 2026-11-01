"""Shopping benchmark: smoke by default, or all scenarios with --full."""

import argparse
from datetime import datetime, timezone
import json
import re
import time

from langchain_core.messages import AIMessage, HumanMessage

from shop.agents import Agents, LocalLLM
from shop.graph import run_turn
from shop.grader import grade_scenario
from shop.servers import CONFIGS, GEMMA_NGL, Monitor, mark_contention, preflight, processes, resident
from shop.shared import DATA_VERSION, ROOT, dm, leaves, load, runtime
from shop.tools import Shop
from shop.tracing import Tracing


def now():
    return datetime.now(timezone.utc)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", choices=CONFIGS, default="g12-native")
    parser.add_argument("--full", action="store_true", help="Run all 60 scenarios instead of the three smoke scenarios")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat index, not the number of executions")
    parser.add_argument("--gemma-ngl", type=int)
    parser.add_argument("--name", "--tag", dest="name", help="New folder name under runs/; --tag remains a smoke-compatible alias")
    parser.add_argument("--port", type=int, default=18380)
    parser.add_argument("--load-timeout", type=int, default=300)
    parser.add_argument("--request-timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if args.repeat < 1 or not 1024 <= args.port <= 65534 or args.load_timeout <= 0 or args.request_timeout <= 0:
        parser.error("Repeat and timeouts must be positive; port must be 1024..65534")
    if args.gemma_ngl is None:
        args.gemma_ngl = GEMMA_NGL[args.config]
    if not 0 <= args.gemma_ngl <= 99:
        parser.error("Gemma ngl must be 0..99")
    if not args.name:
        if not args.full:
            parser.error("Smoke requires --tag or --name")
        args.name = f"{args.config}-r{args.repeat}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.name):
        parser.error("Invalid folder name")
    return args


def latency_split(turn):
    result = {"turn_total_ms": turn.get("total_ms"), "turn_to_final_reply_ttft_ms": turn.get("turn_to_final_reply_ttft_ms")}
    for role in ("judge", "answer"):
        calls = [c for c in turn.get("llm_calls", []) if (c.get("node") == "supervisor") == (role == "judge")]
        result[role] = {"call_count": len(calls), "total_ms": sum(c["total_ms"] for c in calls) if all(c.get("total_ms") is not None for c in calls) else None, "ttft_ms": calls[0].get("ttft_ms") if role == "judge" and calls else turn.get("final_reply_ttft_ms") if role == "answer" else None, "calls": [{key: c.get(key) for key in ("node", "ttft_ms", "content_ttft_ms", "total_ms", "observer_ms", "status")} for c in calls]}
    return result


def save(folder, result, tracing):
    safe = tracing.safe(json.dumps(result, ensure_ascii=False, allow_nan=False))
    dm.save_json(folder / "results.json", json.loads(safe))
    if result["mode"] == "smoke":
        for row in result["scenarios"]:
            text = json.dumps(row, ensure_ascii=False, indent=2)
            (folder / (row["id"] + "-trace.txt")).write_text(tracing.safe(text) + "\n", encoding="utf-8")


def execute(args):
    folder = ROOT / "runs" / args.name
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "traces").mkdir()
    tracing = Tracing()
    start = time.perf_counter()
    reference = args.gemma_ngl != GEMMA_NGL[args.config]
    result = {"data_version": DATA_VERSION, "stage": "3-2" if args.full else "3-1", "mode": "full" if args.full else "smoke", "config": args.config, "repeat_index": args.repeat if args.full else 0, "ngl": args.gemma_ngl, "gemma_ngl": args.gemma_ngl, "default_gemma_ngl": GEMMA_NGL[args.config], "reference_offload": reference, "labels": ["reference_offload"] if reference else [], "ctx": 16384, "decision_ctx": CONFIGS[args.config].get("ctx"), "decision_ngl": 99 if args.config != "g12-native" else None, "status": "starting", "started": now().isoformat(), "servers": [], "errors": [], "peak_vram_mib_by_gpu": {}, "contended": None, "latency_comparable": None, "scenarios": []}
    monitor = None
    llm = None
    active_row = None
    try:
        dataset, base = load(ROOT / "data/scenarios.json"), load(ROOT / "data/shop_db.json")
        scenarios = dataset["scenarios"] if args.full else [next(s for s in dataset["scenarios"] if s["id"] == sid) for sid in dataset["smoke_ids"]]
        result["dataset_sha256"] = dm.fingerprint(dataset)
        result["scenarios"] = [{"id": s["id"], "category": s["category"], "expected_turns": len(s["turns"]), "expected_required_calls": sum(len(t["expect"].get("required_calls", [])) for t in s["turns"]), "status": "not_run", "success": False, "turns": [], "contended": None, "latency_comparable": False, "contention_reasons": [], "trace_path": str(folder / "traces" / (s["id"] + ".jsonl"))} for s in scenarios]
        save(folder, result, tracing)
        with dm.stop_on_signal():
            result["preflight"], errors = preflight(args.config, args.port)
            result["preflight_errors"] = errors
            save(folder, result, tracing)
            if errors:
                raise runtime.PreflightError("; ".join(errors))
            with Monitor(result["servers"]) as monitor:
                with resident(args.config, args.gemma_ngl, folder, args.port, args.load_timeout, result["servers"]) as (server, backend):
                    mark_contention(result, monitor=monitor)
                    llm = LocalLLM(server.port, args.request_timeout)
                    if backend is not None:
                        backend.client.timeout = args.request_timeout
                    result["status"] = "running"
                    for position, (scenario, row) in enumerate(zip(scenarios, result["scenarios"]), 1):
                        active_row = row
                        started = now()
                        scenario_start = time.perf_counter()
                        row.update(status="running", started=started.isoformat())
                        current_turn = None
                        sequence = 0
                        with (folder / "traces" / (scenario["id"] + ".jsonl")).open("x", encoding="utf-8") as trace:
                            def emit(event, **fields):
                                nonlocal sequence
                                record = {**fields, "event": event, "sequence": sequence, "scenario_id": scenario["id"], "turn": current_turn, "timestamp": now().isoformat(), "elapsed_ms": (time.perf_counter() - scenario_start) * 1000}
                                trace.write(tracing.safe(json.dumps(record, ensure_ascii=False, allow_nan=False)) + "\n")
                                trace.flush()
                                sequence += 1

                            shop = Shop(base, scenario["customer_id"], scenario["db_patch"], scenario["faults"], emit=emit, reference_date=scenario.get("reference_date"))
                            agents, history = Agents(llm, scenario["customer_id"], backend, emit=emit), []
                            emit("scenario_start", config=args.config, repeat_index=result["repeat_index"])
                            try:
                                for index, turn in enumerate(scenario["turns"]):
                                    current_turn = index
                                    offset = len(shop.trace)
                                    turn_start = time.perf_counter()
                                    turn_started = now().isoformat()
                                    emit("turn_start", user=turn["user"])
                                    interrupted = None
                                    try:
                                        with tracing.local():
                                            state = run_turn(agents, shop, index, turn["user"], history, {"metadata": {"config": args.config, "repeat_index": result["repeat_index"], "scenario_id": scenario["id"]}, "tags": [args.config, f"repeat:{result['repeat_index']}", scenario["id"]]}, emit=emit)
                                        history += [HumanMessage(content=turn["user"]), AIMessage(content=state["reply"])]
                                        t = {key: value for key, value in state.items() if key not in ("messages", "pending", "agent")}
                                        t.update(user=turn["user"], status="complete", calls=shop.trace[offset:])
                                    except (Exception, KeyboardInterrupt) as error:
                                        t = {key: value for key, value in getattr(error, "shop_state", {}).items() if key not in ("messages", "pending", "agent")}
                                        t.update(user=turn["user"], status="interrupted" if isinstance(error, KeyboardInterrupt) else "error", error=tracing.safe(dm.error_text(error)), reply="", calls=shop.trace[offset:], total_ms=getattr(error, "shop_total_ms", (time.perf_counter() - turn_start) * 1000))
                                        if hasattr(error, "shop_llm_call"):
                                            t["llm_calls"] = t.get("llm_calls", []) + [error.shop_llm_call]
                                        if isinstance(error, KeyboardInterrupt):
                                            interrupted = error
                                    t["sampled_turn_peak_vram_mib_by_gpu"] = monitor.since(turn_start)
                                    t.update(started=turn_started, finished=now().isoformat(), llm_call_count=len(t.get("llm_calls", [])), domain_tool_count=sum(c.get("kind") == "tool" for c in t["calls"]))
                                    t["latency"] = latency_split(t)
                                    mark_contention(result, t, monitor=monitor)
                                    row["turns"].append(t)
                                    emit("turn_end", result=t)
                                    save(folder, result, tracing)
                                    if interrupted:
                                        raise interrupted
                                    if t["status"] == "error":
                                        break
                            finally:
                                row["grade"] = grade_scenario(scenario, row["turns"], shop)
                                row.update(status="complete" if row["grade"]["complete"] else "failed", success=row["grade"]["success"], finished=now().isoformat(), wall_time_s=time.perf_counter() - scenario_start)
                                mark_contention(result, row, monitor=monitor)
                                before, after = leaves(shop.initial), leaves(shop.db)
                                emit("db_diff", changes={key: {"before": before.get(key), "after": after.get(key)} for key in before.keys() | after.keys() if before.get(key) != after.get(key)})
                                emit("scenario_end", grade=row["grade"], status=row["status"], contended=row["contended"], contention_reasons=row["contention_reasons"], wall_time_s=row["wall_time_s"])
                                save(folder, result, tracing)
                                print(json.dumps({"scenario": scenario["id"], "progress": f"{position}/{len(scenarios)}", "success": row["success"], "status": row["status"], "contended": row["contended"]}), flush=True)
                        tracing.upload(args.config, result["repeat_index"], scenario["id"], {"user_turns": [t["user"] for t in scenario["turns"]]}, {"status": row["status"], "turns": row["turns"]}, started, now())
                        save(folder, result, tracing)
            result["status"] = "complete" if all(r["status"] == "complete" for r in result["scenarios"]) else "failed"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
    except Exception as error:
        message = tracing.safe(dm.error_text(error))
        result.update(status="blocked" if isinstance(error, runtime.PreflightError) else "failed")
        result["errors"].append(message)
        for row in result["scenarios"]:
            if row["status"] == "not_run":
                row.update(status="blocked", error=message)
    finally:
        if llm is not None:
            try:
                llm.close()
            except Exception as error:
                result["errors"].append(tracing.safe(dm.error_text(error)))
        if monitor is not None:
            result.update(peak_vram_mib_by_gpu=monitor.peak, vram_errors=sorted(monitor.errors))
            result.update(new_external_gpu_processes=monitor.external, gpu_process_monitor_errors=sorted(monitor.process_errors))
        mark_contention(result, active_row, monitor=monitor, sample=False)
        try:
            result["llama_server_after"] = processes()
            if result["llama_server_after"]:
                result["errors"].append("llama-server processes remain (not terminated without ownership): " + str(result["llama_server_after"]))
        except Exception as error:
            result["errors"].append(tracing.safe(dm.error_text(error)))
        tracing.finish()
        result["langsmith"] = tracing.status
        result.update(finished=now().isoformat(), wall_time_s=time.perf_counter() - start)
        save(folder, result, tracing)
        (folder / "server-preflight.log").write_text(tracing.safe(json.dumps({"checks": result.get("preflight"), "errors": result["errors"], "langsmith": tracing.status}, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")
    print(json.dumps({"folder": str(folder), "status": result["status"], "successes": sum(r["success"] for r in result["scenarios"]), "selected": len(result["scenarios"])}, ensure_ascii=False))
    return int(result["status"] != "complete")


if __name__ == "__main__":
    args = parse_args()
    try:
        raise SystemExit(execute(args))
    except FileExistsError:
        raise SystemExit("Refusing to overwrite an existing run folder: " + args.name)
