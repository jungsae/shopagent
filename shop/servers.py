from contextlib import ExitStack, contextmanager
import subprocess
import time
from types import SimpleNamespace

from .shared import dm, ra


GEMMA = "gemma-4-12b-it-Q4_K_M.gguf"
# Planned Mix1: upstage-decide-g12 (Solar Decide API + local Gemma), dry-run/docs only.
# Offline payloads: python -m shop.upstage_dryrun (not run.py --config).
CONFIGS = {
    "g12-native": {"backend": "gemma", "decision": GEMMA},
    "clef-g12": {"backend": "clef", "decision": "Clef-Flash-Q4_K_M.gguf", "reply": GEMMA, "ctx": 4096, "ctk": "q8_0", "ctv": "q8_0"},
    "pplx-g12": {"backend": "pplx", "decision": "pplx-decider-flash-v1.2-q8_0.gguf", "reply": GEMMA, "ctx": 4096, "ctk": "f16", "ctv": "f16"},
    "jev-g12": {"backend": "jev", "decision": "jev-omni/Jev-Omni-Q4_K_M.gguf", "reply": GEMMA, "ctx": 2048, "ctk": "q8_0", "ctv": "q8_0"},
}
GEMMA_NGL = {"g12-native": 99, "clef-g12": 16, "pplx-g12": 4, "jev-g12": 0}

VRAM_LIMIT_MIB = 11264
SHARED_GROWTH_LIMIT_MIB = 1024


def mark_contention(result, attempt=None, monitor=None, sample=True):
    attempt = result if attempt is None else attempt
    memory = ra.memory_snapshot() if sample else attempt.get("memory_after_warmup", {})
    if sample:
        result["memory_after_warmup"] = memory
        attempt["memory_after_warmup"] = memory
    peak = dict(monitor.peak) if monitor is not None else {}
    for row in memory.get("gpu") or []:
        key = str(row["index"])
        peak[key] = max(peak.get(key, 0), row["used_mib"])
    baseline = result.get("preflight", {}).get("shared_mib")
    shared = memory.get("shared_mib")
    shared_peak = sum(shared.values()) if shared else None
    if monitor is not None and monitor.shared_peak_mib is not None:
        shared_peak = max(shared_peak or 0, monitor.shared_peak_mib)
    reasons = []
    for gpu, value in peak.items():
        if value > VRAM_LIMIT_MIB:
            reasons.append({"reason": "vram_peak", "gpu": gpu, "measured_mib": value, "threshold_mib": VRAM_LIMIT_MIB})
    growth = shared_peak - sum(baseline.values()) if baseline and shared_peak is not None else None
    if growth is not None and growth > SHARED_GROWTH_LIMIT_MIB:
        reasons.append({"reason": "shared_memory_growth", "measured_mib": growth, "baseline_mib": sum(baseline.values()), "peak_mib": shared_peak, "threshold_mib": SHARED_GROWTH_LIMIT_MIB})
    external = dict(monitor.external) if monitor is not None else {}
    if external:
        reasons.append({"reason": "new_external_gpu_process", "processes": external})
    missing = []
    if not baseline:
        missing.append("preflight.shared_mib")
    if not memory.get("gpu"):
        missing.append("snapshot.gpu")
    if not shared:
        missing.append("snapshot.shared_mib")
    errors = list(memory.get("errors", []))
    if monitor is not None:
        if not monitor.peak:
            missing.append("monitor.gpu")
        errors += sorted(monitor.errors | monitor.process_errors)
    if missing or errors:
        reasons.append({"reason": "missing_counter", "counters": missing, "errors": errors})
    value = True if any(r["reason"] != "missing_counter" for r in reasons) else None if reasons else False
    observation = {"contended": value, "peak_vram_mib_by_gpu": peak, "shared_baseline_mib": sum(baseline.values()) if baseline else None, "shared_peak_mib": shared_peak, "shared_growth_mib": growth, "contention_reasons": reasons}
    targets = [result] if attempt is result else [result, attempt]
    for target in targets:
        checks = target.setdefault("contention_checks", [])
        checks.append(value if target is result else result["contended"])
        target.setdefault("contention_observations", []).append(observation)
        recorded = target.setdefault("contention_reasons", [])
        for reason in reasons if target is result else result["contention_reasons"]:
            if reason not in recorded:
                recorded.append(reason)
        target["contended"] = True if True in checks else None if None in checks else False
        target["latency_comparable"] = target["contended"] is False


def compute_apps():
    result = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader,nounits"], check=True, capture_output=True, text=True, timeout=10)
    return {int(line.split(",", 1)[0]): line.split(",", 1)[1].strip() for line in result.stdout.splitlines() if line.strip()}


class Monitor(dm.VramMonitor):
    def __init__(self, servers):
        super().__init__()
        self.servers = servers
        self.samples = []
        self.external = {}
        self.process_errors = set()
        self.shared_peak_mib = None
        try:
            self.baseline_apps = compute_apps()
        except Exception as error:
            self.baseline_apps = None
            self.process_errors.add(dm.error_text(error))

    def sample(self):
        try:
            memory = ra.memory_snapshot()
            self.samples.append((time.perf_counter(), memory))
            self.errors.update(memory["errors"])
            if not memory["gpu"] or not memory["shared_mib"]:
                self.errors.add("Missing GPU or shared-memory counter sample")
            if memory["shared_mib"]:
                self.shared_peak_mib = max(self.shared_peak_mib or 0, sum(memory["shared_mib"].values()))
            for row in memory["gpu"] or []:
                key = str(row["index"])
                self.peak[key] = max(self.peak.get(key, 0), row["used_mib"])
        except Exception as error:
            self.errors.add(dm.error_text(error))
        try:
            apps = compute_apps()
            owned = {s.get("pid") for s in self.servers}
            for pid, name in apps.items():
                if self.baseline_apps is not None and pid not in self.baseline_apps and pid not in owned:
                    self.external[str(pid)] = name
        except Exception as error:
            self.process_errors.add(dm.error_text(error))

    def since(self, start):
        peak = {}
        for timestamp, memory in list(self.samples):
            if timestamp >= start:
                for row in memory["gpu"] or []:
                    key = str(row["index"])
                    peak[key] = max(peak.get(key, 0), row["used_mib"])
        return peak


def processes():
    response = subprocess.run(["ps", "-C", "llama-server", "-o", "pid=,comm="], capture_output=True, text=True, check=False, timeout=10)
    if response.returncode not in (0, 1):
        raise RuntimeError("Cannot inspect llama-server processes: " + response.stderr.strip())
    return response.stdout.strip().splitlines()


def preflight(name, port):
    checks, errors = ra.preflight(CONFIGS[name], [port] if name == "g12-native" else [port, port + 1])
    checks["llama_server_before"] = processes()
    try:
        checks["compute_apps"] = compute_apps()
        if checks["compute_apps"]:
            errors.append("Outside GPU compute processes before loading: " + str(checks["compute_apps"]))
    except Exception as error:
        checks["compute_apps_error"] = dm.error_text(error)
        errors.append("Cannot inspect outside GPU compute processes: " + dm.error_text(error))
    if name == "jev-g12" and not (dm.MODELS / "jev-omni/decision-head-f32.npz").is_file():
        errors.append("Missing Jev decision-head-f32.npz")
    return checks, errors


def make(name, role, ngl, folder, port, timeout):
    config = CONFIGS[name]
    backend = config["backend"] if role == "decision" else "gemma"
    judge = role == "decision" and "reply" in config
    spec = SimpleNamespace(model=dm.MODELS / config["decision" if role == "decision" else "reply"], ctx=config["ctx"] if judge else 16384, load_timeout=timeout)
    server = dm.make_server(spec, folder, backend)
    server.port = port
    server.log_path = folder / f"server-{role}.log"
    server.command[server.command.index("--port") + 1] = str(port)
    server.command[server.command.index("-ngl") + 1] = str(ngl)
    if judge:
        for flag, key in (("-ctk", "ctk"), ("-ctv", "ctv")):
            server.command[server.command.index(flag) + 1] = config[key]
        if backend in ("jev", "pplx", "clef"):
            for flag in ("-b", "-ub"):
                server.command[server.command.index(flag) + 1] = str(spec.ctx)
    server.info.update(role=role, mode="resident", ngl=ngl, model=str(spec.model), backend=backend)
    server.info.update(ctk=server.command[server.command.index("-ctk") + 1], ctv=server.command[server.command.index("-ctv") + 1])
    return server


@contextmanager
def resident(name, ngl, folder, port, timeout, records):
    with ExitStack() as stack:
        if name == "g12-native":
            reply = make(name, "decision", ngl, folder, port, timeout)
            records.append(reply.info)
            stack.enter_context(reply)
            yield reply, None
        else:
            decision = make(name, "decision", 99, folder, port, timeout)
            records.append(decision.info)
            stack.enter_context(decision)
            reply = make(name, "reply", ngl, folder, port + 1, timeout)
            records.append(reply.info)
            stack.enter_context(ra.started(reply))
            client = ra.Client(decision.port, 180)
            backend = dm.Backend(CONFIGS[name]["backend"], client, dm.MODELS / CONFIGS[name]["decision"], pplx_limit=CONFIGS[name]["ctx"])
            yield reply, backend
