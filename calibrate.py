"""Load/measure only; never executes shopping scenarios."""

import argparse
from datetime import datetime, timezone
import json
import re

from langchain_core.messages import HumanMessage

from shop.agents import Agents, LocalLLM, handoff
from shop.servers import Monitor, SHARED_GROWTH_LIMIT_MIB, VRAM_LIMIT_MIB, mark_contention, preflight, processes, resident
from shop.shared import ROOT, dm, runtime
from shop.tracing import Tracing


def calibrate(args):
    folder = ROOT / "runs" / args.tag
    folder.mkdir(parents=True, exist_ok=False)
    tracing = Tracing()
    result = {"purpose": "resident pair calibration only; zero scenarios", "started": datetime.now(timezone.utc).isoformat(), "candidates": args.ngl, "threshold_mib": VRAM_LIMIT_MIB, "shared_growth_threshold_mib": SHARED_GROWTH_LIMIT_MIB, "attempts": [], "selected_ngl": None, "errors": []}
    try:
        with dm.stop_on_signal():
            for ngl in args.ngl:
                safe = True
                for name in ("jev-g12", "clef-g12", "pplx-g12"):
                    attempt = {"config": name, "gemma_ngl": ngl, "decision_ngl": 99, "servers": [], "status": "starting"}
                    result["attempts"].append(attempt)
                    target = folder / f"{name}-ngl{ngl}"
                    target.mkdir()
                    monitor = None
                    llm = None
                    try:
                        attempt["preflight"], errors = preflight(name, args.port)
                        if errors:
                            raise runtime.PreflightError("; ".join(errors))
                        with Monitor(attempt["servers"]) as monitor:
                            with resident(name, ngl, target, args.port, args.load_timeout, attempt["servers"]) as (server, backend):
                                mark_contention(attempt, monitor=monitor)
                                if attempt["contended"] is not False:
                                    raise RuntimeError("Resident contention detected or memory counters unavailable; calibration rejected")
                                llm = LocalLLM(server.port, 180)
                                with tracing.local():
                                    _, timing = llm.call([HumanMessage(content="한 단어로 인사하세요.")], [handoff("transfer_back_to_supervisor")], "warmup")
                                    _, _, decision_timing = Agents(llm, "C01", backend).supervise([HumanMessage(content="지원 결제수단은 무엇인가요?")], 0)
                                mark_contention(attempt, monitor=monitor)
                                attempt.update(status="measured", reply_timing=timing, decision_timing=decision_timing)
                    except runtime.PreflightError as error:
                        attempt.update(status="blocked", error=tracing.safe(dm.error_text(error)))
                        raise
                    except Exception as error:
                        safe = False
                        attempt.update(status="failed", error=tracing.safe(dm.error_text(error)))
                    finally:
                        if llm:
                            llm.close()
                        if monitor:
                            attempt["peak_vram_mib_by_gpu"] = monitor.peak
                            attempt["vram_errors"] = sorted(monitor.errors)
                            attempt["new_external_gpu_processes"] = monitor.external
                            attempt["gpu_process_monitor_errors"] = sorted(monitor.process_errors)
                        mark_contention(attempt, monitor=monitor, sample=False)
                        if monitor and attempt["contended"] is not False:
                            safe = False
                            attempt["status"] = "rejected"
                        dm.save_json(folder / "results.json", result)
                if safe:
                    result["selected_ngl"] = ngl
                    break
    except KeyboardInterrupt:
        result["errors"].append("KeyboardInterrupt")
    except Exception as error:
        result["errors"].append(tracing.safe(dm.error_text(error)))
    finally:
        tracing.finish()
        result["llama_server_after"] = processes()
        result["finished"] = datetime.now(timezone.utc).isoformat()
        dm.save_json(folder / "results.json", result)
    print(json.dumps({"selected_ngl": result["selected_ngl"], "result": str(folder / "results.json")}))
    return int(result["selected_ngl"] is None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--ngl", type=lambda text: [int(v) for v in text.split(",")], default=[16, 12, 8, 4, 0])
    parser.add_argument("--port", type=int, default=18480)
    parser.add_argument("--load-timeout", type=int, default=300)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.tag) or len(set(args.ngl)) != len(args.ngl) or not all(0 <= n < 99 for n in args.ngl) or not 1024 <= args.port <= 65534:
        parser.error("Invalid tag, ngl candidates or port")
    raise SystemExit(calibrate(args))
