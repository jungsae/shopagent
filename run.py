"""Shopping benchmark: smoke by default, or all scenarios with --full."""

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from shop import graph_v2
from shop.agents import Agents, LocalLLM
from shop.graph import run_turn
from shop.grader import expected_calls, grade_scenario
from shop.servers import CONFIGS, GEMMA_NGL, Monitor, mark_contention, preflight, processes, resident
from shop.shared import DATA_VERSION, ROOT, dm, leaves, load, runtime
from shop.tools import CUSTOMER_RESPONSE, Shop
from shop.tracing import Tracing
from shop.upstage import require_upstage_api_key


def now():
    return datetime.now(timezone.utc)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", choices=CONFIGS, default="g12-native")
    parser.add_argument("--full", action="store_true", help="Run all scenarios instead of the three smoke scenarios")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat index, not the number of executions")
    parser.add_argument("--attempts", type=int, default=1, help="Sequential attempts per scenario in the same run folder (at least 1)")
    parser.add_argument("--gemma-ngl", type=int)
    parser.add_argument("--name", "--tag", dest="name", help="New folder name under runs/; --tag remains a smoke-compatible alias")
    parser.add_argument("--graph", choices=("v1", "v2"), default="v1", help="v1: original graph. v2: create_agent + interrupt/resume (shop/graph_v2.py), its own score table")
    parser.add_argument("--assist", choices=("on", "off"), help="v2 only. on: keep the receipt lookup retry and ungrounded-value rewrite as a reference run. off (default): official score without them")
    parser.add_argument("--port", type=int, default=18380)
    parser.add_argument("--load-timeout", type=int, default=300)
    parser.add_argument("--request-timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if args.assist and args.graph == "v1":
        parser.error("--assist is only for --graph v2; v1 always applies the correction rules")
    if args.graph == "v2" and args.assist is None:
        args.assist = "off"
    if args.attempts < 1 or args.repeat < 1 or not 1024 <= args.port <= 65534 or args.load_timeout <= 0 or args.request_timeout <= 0:
        parser.error("Attempts, repeat and timeouts must be positive; port must be 1024..65534")
    remote = CONFIGS[args.config]["backend"] == "remote_api"
    if remote and args.gemma_ngl is not None:
        parser.error("--gemma-ngl does not apply to solar-pro4 (remote API)")
    if args.gemma_ngl is None:
        args.gemma_ngl = GEMMA_NGL[args.config]
    if not remote and not 0 <= args.gemma_ngl <= 99:
        parser.error("Gemma ngl must be 0..99")
    if not args.name:
        if not args.full:
            parser.error("Smoke requires --tag or --name")
        args.name = f"{args.config}-r{args.repeat}" if args.graph == "v1" else f"{args.config}-graph2{'-assist' if args.assist == 'on' else ''}-r{args.repeat}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.name):
        parser.error("Invalid folder name")
    return args


def latency_split(turn):
    result = {"turn_total_ms": turn.get("total_ms"), "turn_to_final_reply_ttft_ms": turn.get("turn_to_final_reply_ttft_ms")}
    for role in ("judge", "answer"):
        calls = [c for c in turn.get("llm_calls", []) if c.get("node") != "simulated_customer" and (c.get("node") in ("router", "supervisor", "consent_judge")) == (role == "judge")]
        result[role] = {"call_count": len(calls), "total_ms": sum(c["total_ms"] for c in calls) if all(c.get("total_ms") is not None for c in calls) else None, "ttft_ms": calls[0].get("ttft_ms") if role == "judge" and calls else turn.get("final_reply_ttft_ms") if role == "answer" else None, "calls": [{key: c.get(key) for key in ("node", "ttft_ms", "content_ttft_ms", "total_ms", "observer_ms", "status")} for c in calls]}
    customer_calls = [call for call in turn.get("llm_calls", []) if call.get("node") == "simulated_customer"]
    if customer_calls:
        result["simulated_customer"] = {"call_count": len(customer_calls), "total_ms": sum(call["total_ms"] for call in customer_calls), "calls": customer_calls}
    return result


def run_v2_turn(app, agents, scenario, index, pending=None):
    """Harness-side customer simulation, outside graph state and agent budgets."""
    turn = scenario["turns"][index]
    user_text = turn["user"]
    simulation = None
    timing = None
    simulation_started = time.perf_counter()
    current_pending = getattr(app, "pending", None)
    pending = current_pending if isinstance(current_pending, dict) else pending
    if turn.get("consent") and pending:
        card = pending.get("confirmation_card")
        if not card:
            raise ValueError("Simulated customer requires pending confirmation_card")
        response, timing = agents.call([
            SystemMessage(content=agents.prompts["v2_simulated_customer"]),
            HumanMessage(content=json.dumps({"earlier_customer_messages": [previous["user"] for previous in scenario["turns"][:index]],
                                            "confirmation_card": card}, ensure_ascii=False)),
        ], [CUSTOMER_RESPONSE], "simulated_customer")
        if len(response.tool_calls) != 1 or response.tool_calls[0]["name"] != "customer_response":
            error = ValueError("Simulated customer must call customer_response once")
            error.shop_llm_call = timing
            raise error
        try:
            arguments = response.tool_calls[0]["args"]
            graph_v2.validate_arguments(arguments, CUSTOMER_RESPONSE["function"]["parameters"])
        except ValueError as error:
            error.shop_llm_call = timing
            raise
        approved = arguments["decision"] == "approve"
        user_text = turn["user"] if approved else arguments["message"]
        simulation = {"decision": arguments["decision"], "used_scripted_text": approved, "message": user_text}
    simulation_ms = (time.perf_counter() - simulation_started) * 1000

    def attach(state):
        if simulation is not None:
            state.update(simulated_customer=simulation, user=user_text, scripted_user=turn["user"])
            state["llm_calls"] = [timing, *state.get("llm_calls", [])]
            state["total_ms"] = state.get("total_ms", 0) + simulation_ms
            if state.get("turn_to_final_reply_ttft_ms") is not None:
                state["turn_to_final_reply_ttft_ms"] += simulation_ms
        return state

    try:
        return attach(app.run_turn(index, user_text))
    except BaseException as error:
        state = getattr(error, "shop_state", {})
        state.setdefault("total_ms", getattr(error, "shop_total_ms", 0))
        error.shop_state = attach(state)
        error.shop_total_ms = error.shop_state.get("total_ms", getattr(error, "shop_total_ms", 0))
        raise


def attempt_stem(scenario_id, attempt_index=1):
    return scenario_id if attempt_index == 1 else f"{scenario_id}-attempt-{attempt_index}"


def langsmith_metadata(args, scenario_id, attempt=None, repeat_index=None):
    """Remote conversation identity, separate from the graph checkpointer UUID."""
    graph = getattr(args, "graph", "v1")
    metadata = {"thread_id": f"{args.name}:{scenario_id}", "run_name": args.name,
                "scenario_id": scenario_id, "graph": graph, "config": args.config,
                "repeat_index": (args.repeat if args.full else 0) if repeat_index is None else repeat_index}
    if graph == "v2":
        metadata["assist"] = getattr(args, "assist", None) or "off"
    if attempt is not None or getattr(args, "attempts", 1) > 1:
        metadata["attempt_index"] = 1 if attempt is None else attempt
        metadata["thread_id"] += f":{metadata['attempt_index']}"
    return metadata


def save(folder, result, tracing):
    safe = tracing.safe(json.dumps(result, ensure_ascii=False, allow_nan=False))
    dm.save_json(folder / "results.json", json.loads(safe))
    if result["mode"] == "smoke":
        for row in result["scenarios"]:
            text = json.dumps(row, ensure_ascii=False, indent=2)
            (folder / (attempt_stem(row["id"], row.get("attempt_index", 1)) + "-trace.txt")).write_text(tracing.safe(text) + "\n", encoding="utf-8")


def record_measurements(result, attempt=None, monitor=None, sample=True):
    if CONFIGS[result["config"]]["backend"] == "remote_api":
        for target in (result,) if attempt is None else (result, attempt):
            target.update(contended=None, latency_comparable=False, latency_comparability_reason="remote_api",
                          gpu_comparable=False, gpu_comparability_reason="remote_api",
                          peak_vram_mib_by_gpu={}, contention_reasons=[])
    else:
        mark_contention(result, attempt, monitor=monitor, sample=sample)


def execute(args):
    config = CONFIGS[args.config]
    remote = config["backend"] == "remote_api"
    api_key = require_upstage_api_key() if remote else None
    folder = ROOT / "runs" / args.name
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "traces").mkdir()
    tracing = Tracing()
    if remote:
        tracing.secrets.append(api_key)
    start = time.perf_counter()
    reference = args.gemma_ngl != GEMMA_NGL[args.config]
    result = {"data_version": DATA_VERSION, "stage": "3-2" if args.full else "3-1", "mode": "full" if args.full else "smoke", "config": args.config, "repeat_index": args.repeat if args.full else 0, "ngl": args.gemma_ngl, "gemma_ngl": args.gemma_ngl, "default_gemma_ngl": GEMMA_NGL[args.config], "reference_offload": reference, "labels": ["reference_offload"] if reference else [], "ctx": 16384, "decision_ctx": CONFIGS[args.config].get("ctx"), "decision_ngl": 99 if args.config != "g12-native" else None, "status": "starting", "started": now().isoformat(), "servers": [], "errors": [], "peak_vram_mib_by_gpu": {}, "contended": None, "latency_comparable": None, "scenarios": []}
    if remote:
        result.update(model=config["model"], base_url=config["base_url"], ctx=None, decision_ngl=None)
        record_measurements(result)
    if getattr(args, "graph", "v1") == "v2":
        result["graph"] = "v2"
        result["consent_mode"] = "tool_interrupt"
        result["assist"] = getattr(args, "assist", None) or "off"
    monitor = None
    llm = None
    active_row = None
    attempts = getattr(args, "attempts", 1)
    if attempts > 1:
        result["attempts"] = attempts
    try:
        dataset, base = load(ROOT / "data/scenarios.json"), load(ROOT / "data/shop_db.json")
        scenarios = dataset["scenarios"] if args.full else [next(s for s in dataset["scenarios"] if s["id"] == sid) for sid in dataset["smoke_ids"]]
        selected_attempts = [(scenario, attempt) for scenario in scenarios for attempt in range(1, attempts + 1)]
        result["dataset_sha256"] = dm.fingerprint(dataset)
        if getattr(args, "graph", "v1") == "v2":
            result["prompts_sha256"] = dm.fingerprint(load(ROOT / "data/prompts.json"))
        result["scenarios"] = [{"id": s["id"], "category": s["category"], "expected_turns": len(s["turns"]), "expected_required_calls": sum(len(expected_calls(t["expect"], getattr(args, "graph", "v1") == "v2")) for t in s["turns"]), "status": "not_run", "success": False, "turns": [], "contended": None, "latency_comparable": False, "contention_reasons": [], "trace_path": str(folder / "traces" / (attempt_stem(s["id"], attempt) + ".jsonl")), **({"attempt_index": attempt} if attempts > 1 else {})} for s, attempt in selected_attempts]
        if remote:
            for row in result["scenarios"]:
                record_measurements(result, row)
        save(folder, result, tracing)
        with dm.stop_on_signal():
            result["preflight"], errors = ({}, []) if remote else preflight(args.config, args.port)
            result["preflight_errors"] = errors
            save(folder, result, tracing)
            if errors:
                raise runtime.PreflightError("; ".join(errors))
            with (nullcontext() if remote else Monitor(result["servers"])) as monitor:
                with (nullcontext((None, None)) if remote else resident(args.config, args.gemma_ngl, folder, args.port, args.load_timeout, result["servers"])) as (server, backend):
                    record_measurements(result, monitor=monitor)
                    llm = LocalLLM(timeout=args.request_timeout, config=config) if remote else LocalLLM(server.port, args.request_timeout)
                    if backend is not None:
                        backend.client.timeout = args.request_timeout
                    result["status"] = "running"
                    for position, ((scenario, attempt), row) in enumerate(zip(selected_attempts, result["scenarios"]), 1):
                        active_row = row
                        started = now()
                        scenario_start = time.perf_counter()
                        row.update(status="running", started=started.isoformat())
                        current_turn = None
                        sequence = 0
                        with Path(row["trace_path"]).open("x", encoding="utf-8") as trace:
                            def emit(event, **fields):
                                nonlocal sequence
                                record = {**fields, "event": event, "sequence": sequence, "scenario_id": scenario["id"], "turn": current_turn, "timestamp": now().isoformat(), "elapsed_ms": (time.perf_counter() - scenario_start) * 1000}
                                if attempts > 1:
                                    record["attempt_index"] = attempt
                                trace.write(tracing.safe(json.dumps(record, ensure_ascii=False, allow_nan=False)) + "\n")
                                trace.flush()
                                sequence += 1

                            shop = Shop(base, scenario["customer_id"], scenario["db_patch"], scenario["faults"], emit=emit, reference_date=scenario.get("reference_date"))
                            agents, history = Agents(llm, scenario["customer_id"], backend, emit=emit), []
                            metadata = langsmith_metadata(args, scenario["id"], attempt if attempts > 1 else None, result["repeat_index"])
                            turn_config = {"metadata": metadata, "tags": [args.config, f"repeat:{result['repeat_index']}", scenario["id"], f"run:{args.name}"]}
                            if attempts > 1:
                                turn_config["tags"].append(f"attempt:{attempt}")
                            scenario_graph = graph_v2.ScenarioGraph(agents, shop, turn_config, emit=emit, assist=getattr(args, "assist", None) == "on") if getattr(args, "graph", "v1") == "v2" else None
                            emit("scenario_start", config=args.config, repeat_index=result["repeat_index"])
                            try:
                                for index, turn in enumerate(scenario["turns"]):
                                    current_turn = index
                                    offset = len(shop.trace)
                                    turn_start = time.perf_counter()
                                    turn_started = now().isoformat()
                                    emit("turn_start", user=turn["user"])
                                    interrupted = None
                                    private_fields = ("messages", "agent") if getattr(args, "graph", "v1") == "v2" else ("messages", "pending", "agent")
                                    try:
                                        with tracing.local():
                                            state = run_v2_turn(scenario_graph, agents, scenario, index, row["turns"][-1].get("pending") if row["turns"] else None) if scenario_graph else run_turn(agents, shop, index, turn["user"], history, turn_config, emit=emit)
                                        history += [HumanMessage(content=state.get("user", turn["user"])), AIMessage(content=state["reply"])]
                                        t = {key: value for key, value in state.items() if key not in private_fields}
                                        t.update(user=state.get("user", turn["user"]), status="complete", calls=shop.trace[offset:])
                                    except (Exception, KeyboardInterrupt) as error:
                                        t = {key: value for key, value in getattr(error, "shop_state", {}).items() if key not in private_fields}
                                        t.update(user=t.get("user", turn["user"]), status="interrupted" if isinstance(error, KeyboardInterrupt) else "error", error=tracing.safe(dm.error_text(error)), reply="", calls=shop.trace[offset:], total_ms=getattr(error, "shop_total_ms", (time.perf_counter() - turn_start) * 1000))
                                        if hasattr(error, "shop_llm_call"):
                                            t["llm_calls"] = t.get("llm_calls", []) + [error.shop_llm_call]
                                        if isinstance(error, KeyboardInterrupt):
                                            interrupted = error
                                    t["sampled_turn_peak_vram_mib_by_gpu"] = {} if remote else monitor.since(turn_start)
                                    t.update(started=turn_started, finished=now().isoformat(), llm_call_count=len(t.get("llm_calls", [])), domain_tool_count=sum(c.get("kind") == "tool" for c in t["calls"]))
                                    t["latency"] = latency_split(t)
                                    record_measurements(result, t, monitor=monitor)
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
                                record_measurements(result, row, monitor=monitor)
                                before, after = leaves(shop.initial), leaves(shop.db)
                                emit("db_diff", changes={key: {"before": before.get(key), "after": after.get(key)} for key in before.keys() | after.keys() if before.get(key) != after.get(key)})
                                emit("scenario_end", grade=row["grade"], status=row["status"], contended=row["contended"], contention_reasons=row["contention_reasons"], wall_time_s=row["wall_time_s"])
                                save(folder, result, tracing)
                                print(json.dumps({"scenario": scenario["id"], **({"attempt_index": attempt} if attempts > 1 else {}), "progress": f"{position}/{len(selected_attempts)}", "success": row["success"], "status": row["status"], "contended": row["contended"], **({"unconfirmed_executions": row["grade"].get("unconfirmed_executions", 0)} if getattr(args, "graph", "v1") == "v2" else {})}, ensure_ascii=False), flush=True)
                        tracing.upload(args.config, result["repeat_index"], attempt_stem(scenario["id"], attempt), {"user_turns": [t["user"] for t in scenario["turns"]]}, {"status": row["status"], "turns": row["turns"]}, started, now(), metadata=metadata)
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
        record_measurements(result, active_row, monitor=monitor, sample=False)
        try:
            result["llama_server_after"] = [] if remote else processes()
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
