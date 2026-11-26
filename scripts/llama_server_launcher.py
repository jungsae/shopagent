"""Loopback-only web launcher for one local llama-server in WSL."""

import argparse
import ast
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import parse_qs


BIND_HOST = "127.0.0.1"
DEFAULT_PRESET = "g12-native"
SERVER_SOURCE = Path(__file__).resolve().parents[1] / "shop" / "servers.py"
PAGE = Path(__file__).with_suffix(".html")
LLAMA_SERVER = Path.home() / "llama.cpp/build/bin/llama-server"
MODEL_DIR = Path.home() / "models"
GEMMA_DEFAULTS = {
    "ctx": 16384, "np": 1, "ngl": 99, "port": 18380, "host": BIND_HOST,
    "ctk": "q8_0", "ctv": "q8_0", "flash_attn": "on", "thinking": False,
    "reasoning_budget": 0,
}
CACHE_TYPES = ("f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1")
FLASH_OPTIONS = ("on", "off", "auto")
STOP_TIMEOUT = 3


def _preset_value(node, declarations):
    if isinstance(node, ast.Name):
        return _preset_value(declarations[node.id], declarations)
    if isinstance(node, ast.Dict):
        return {_preset_value(key, declarations): _preset_value(value, declarations)
                for key, value in zip(node.keys, node.values)}
    return ast.literal_eval(node)


def load_presets(source=SERVER_SOURCE):
    # Importing shop.servers also imports the benchmark's external runtime.
    # Read only its declarative preset data (literals and names such as GEMMA).
    module = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    declarations = {stmt.targets[0].id: stmt.value for stmt in module.body
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)}
    configs = _preset_value(declarations["CONFIGS"], declarations)
    gemma_ngl = _preset_value(declarations["GEMMA_NGL"], declarations)
    labels = {"gemma": "Gemma 12B", "clef": "Clef", "pplx": "pplx", "jev": "Omni"}
    presets = {}
    for name, config in configs.items():
        backend = config["backend"]
        if backend == "remote_api" or "decision" not in config:
            continue
        defaults = GEMMA_DEFAULTS.copy()
        defaults.update({key: config[key] for key in defaults.keys() & config.keys()})
        if backend == "gemma" and "ngl" not in config:
            defaults["ngl"] = gemma_ngl.get(name, defaults["ngl"])
        defaults["model"] = str(MODEL_DIR / config["decision"])
        presets[name] = {
            "label": labels.get(backend, name), "backend": backend,
            "paired": "reply" in config, "defaults": defaults,
            "n_cpu_moe": config.get("n_cpu_moe", 0),
        }
    return presets


def integer(value, label, low, high=2**31 - 1):
    if isinstance(value, str) and value.isascii() and value.removeprefix("-").isdecimal():
        try:
            value = int(value)
        except ValueError:
            pass
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} 값은 {low}~{high} 사이의 정수여야 합니다.")
    return value


def local_endpoint(host, port):
    try:
        address = IPv4Address(host) if isinstance(host, str) else None
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise ValueError("서버 주소에는 127.0.0.1과 같은 로컬 주소만 사용할 수 있습니다.")
    return str(address), integer(port, "서버 포트", 1, 65535)


def build_command(presets, name, overrides):
    if not isinstance(name, str) or name not in presets:
        raise ValueError("로컬 서버 설정을 선택해 주세요.")
    preset = presets[name]
    options = preset["defaults"] | overrides
    model = options["model"]
    if not isinstance(model, str) or not model.strip() or "\0" in model:
        raise ValueError("GGUF 모델 파일 경로를 입력해 주세요.")
    try:
        model = str(Path(model).expanduser())
    except RuntimeError as error:
        raise ValueError("모델 파일의 홈 폴더를 찾을 수 없습니다.") from error
    host, port = local_endpoint(options["host"], options["port"])
    ctx = integer(options["ctx"], "컨텍스트 길이", 0)
    np = integer(options["np"], "동시 처리 수", 1)
    ngl = integer(options["ngl"], "GPU에 올릴 레이어 수", 0)
    ctk, ctv, flash = (options[key] for key in ("ctk", "ctv", "flash_attn"))
    if ctk not in CACHE_TYPES or ctv not in CACHE_TYPES:
        raise ValueError("지원되는 KV 캐시 형식을 선택해 주세요.")
    if flash not in FLASH_OPTIONS:
        raise ValueError("Flash Attention 설정을 선택해 주세요.")
    thinking = options["thinking"]
    if type(thinking) is not bool:
        raise ValueError("추론 사용 여부를 선택해 주세요.")
    budget = options["reasoning_budget"]
    if thinking and not preset["defaults"]["thinking"] and "reasoning_budget" not in overrides:
        budget = -1
    budget = integer(budget, "추론 토큰 한도", -1) if thinking else 0

    command = [
        str(LLAMA_SERVER), "-m", model, "--jinja", "-ngl", str(ngl),
        "-ctk", ctk, "-ctv", ctv, "-fa", flash, "-np", str(np), "-c", str(ctx),
        "--host", host, "--port", str(port), "--n-cpu-moe", str(preset["n_cpu_moe"]),
        "--reasoning", "on" if thinking else "off",
        "--reasoning-budget", str(budget),
    ]
    # Match the decision-model request modes used by shop.servers.make.
    backend = preset["backend"]
    if backend in ("clef", "pplx", "jev"):
        command.extend(["-b", str(ctx), "-ub", str(ctx)])
    if backend in ("pplx", "jev"):
        command.extend(["--embedding", "--pooling", "none", "--embd-normalize", "-1"])
    return command, host, port


def existing_llama_servers():
    result = subprocess.run(["ps", "-C", "llama-server", "-o", "pid="],
                            capture_output=True, text=True, timeout=2)
    if result.returncode not in (0, 1):
        raise RuntimeError("실행 중인 llama-server를 확인할 수 없습니다.")
    return result.stdout.split()


def port_is_busy(host, port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            probe.listen(1)
        except OSError:
            return True
    return False


def health(host, port):
    host, port = local_endpoint(host, port)
    connection = HTTPConnection(host, port, timeout=1)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status == 503:
            return "모델 불러오는 중 (HTTP 503)"
        if response.status == 200:
            data = json.loads(response.read(4096))
            if isinstance(data, dict) and data.get("status") == "ok":
                return "응답 준비 완료"
        return f"예상과 다른 응답 (HTTP {response.status})"
    except (OSError, HTTPException):
        return "응답 없음"
    except ValueError:
        return "응답 형식을 확인할 수 없음"
    finally:
        connection.close()


def vram():
    wsl_smi = Path("/usr/lib/wsl/lib/nvidia-smi")
    executable = shutil.which("nvidia-smi")
    if executable is None and wsl_smi.is_file():
        executable = str(wsl_smi)
    if executable is None:
        return [], "nvidia-smi를 찾을 수 없습니다."
    try:
        result = subprocess.run(
            [executable, "--query-gpu=index,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=2,
        )
        rows = []
        for line in result.stdout.splitlines():
            if line.strip():
                index, used, total = map(int, line.split(","))
                rows.append({"index": index, "used_mib": used, "total_mib": total})
        return rows, None if rows else "표시할 GPU 메모리 정보가 없습니다."
    except FileNotFoundError:
        return [], "nvidia-smi를 찾을 수 없습니다."
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], "GPU 메모리 사용량을 확인할 수 없습니다."


def log_tail(path):
    try:
        with path.open("rb") as log:
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - 65536))
            lines = log.read(65536).splitlines()
    except FileNotFoundError:
        return ""
    return b"\n".join(lines[-80:]).decode("utf-8", errors="replace")


class Launcher:
    def __init__(self, state_dir):
        self.pid_path = state_dir / "llama-server.pid"
        self.log_path = state_dir / "llama-server.log"
        self.process = None
        self.endpoint = None
        self.exit_code = None
        self.lock = Lock()

    def _reap(self):
        if self.process is not None and (code := self.process.poll()) is not None:
            self.process = None
            self.endpoint = None
            self.exit_code = code
            self.pid_path.unlink(missing_ok=True)

    def _stop_child(self):
        # Only signal the original Popen child. A PID read from disk could be stale.
        child = self.process
        child.terminate()
        try:
            child.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=STOP_TIMEOUT)
        self._reap()

    def start(self, command, host, port):
        with self.lock:
            self._reap()
            if self.process is not None:
                raise RuntimeError("이 런처에서 이미 서버를 실행 중입니다.")
            if existing_llama_servers():
                raise RuntimeError("다른 llama-server가 이미 실행 중입니다. 기존 서버를 종료한 뒤 다시 시도해 주세요.")
            if port_is_busy(host, port):
                raise RuntimeError(f"{host}:{port} 포트를 이미 사용 중입니다.")
            # Prevent inherited llama.cpp options from changing the previewed flags.
            env = {name: value for name, value in os.environ.items() if not name.startswith("LLAMA_ARG_")}
            with self.log_path.open("wb") as log:
                self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                                stdout=log, stderr=subprocess.STDOUT,
                                                env=env, start_new_session=True)
            self.endpoint = (host, port)
            self.exit_code = None
            try:
                self.pid_path.write_text(f"{self.process.pid}\n", encoding="ascii")
            except OSError:
                self._stop_child()
                raise
            return self.process.pid

    def stop(self):
        with self.lock:
            self._reap()
            if self.process is None:
                return False
            self._stop_child()
            self.exit_code = None
            return True

    def status(self):
        with self.lock:
            self._reap()
            return {"pid": self.process.pid if self.process else None, "endpoint": self.endpoint,
                    "exit_code": self.exit_code, "log": log_tail(self.log_path)}


class LauncherServer(ThreadingHTTPServer):
    # Finish any in-flight start/stop request before main() cleans up the child.
    daemon_threads = False

    def __init__(self, port, launcher, presets):
        self.launcher = launcher
        self.presets = presets
        super().__init__((BIND_HOST, port), Handler)
        self.authority = f"{BIND_HOST}:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode())

    def _local_request(self, mutation=False):
        authority = self.server.authority
        return self.headers.get("Host") == authority and (
            not mutation or self.headers.get("Origin") == f"http://{authority}")

    def do_GET(self):
        if not self._local_request():
            self._json(403, {"error": "런처의 로컬 주소에서 열어 주세요."})
            return
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
        elif path == "/api/presets":
            self._json(200, {"presets": self.server.presets, "default": DEFAULT_PRESET,
                             "cache_types": CACHE_TYPES, "flash_options": FLASH_OPTIONS})
        elif path == "/api/status":
            params = parse_qs(query)
            try:
                server_health = health(params.get("host", [""])[0], params.get("port", [""])[0])
            except ValueError as error:
                server_health = str(error)
            gpu, gpu_error = vram()
            self._json(200, {**self.server.launcher.status(), "health": server_health,
                             "gpu": gpu, "gpu_error": gpu_error})
        else:
            self._json(404, {"error": "페이지를 찾을 수 없습니다."})

    def do_POST(self):
        if not self._local_request(mutation=True):
            self._json(403, {"error": "런처 화면에서 다시 시도해 주세요."})
            return
        if self.path not in ("/api/start", "/api/stop", "/api/preview"):
            self._json(404, {"error": "페이지를 찾을 수 없습니다."})
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("요청 형식을 확인해 주세요.")
            length = integer(self.headers.get("Content-Length"), "요청 크기", 1, 16384)
            try:
                data = json.loads(self.rfile.read(length))
            except ValueError as error:
                raise ValueError("요청 형식을 확인해 주세요.") from error
            if not isinstance(data, dict) or not isinstance(data.get("options", {}), dict):
                raise ValueError("요청 형식을 확인해 주세요.")
            if self.path == "/api/stop":
                result = {"stopped": self.server.launcher.stop()}
            else:
                command, host, port = build_command(self.server.presets, data.get("preset"), data.get("options", {}))
                result = {"command": shlex.join(command)}
                if self.path == "/api/start":
                    result["pid"] = self.server.launcher.start(command, host, port)
        except ValueError as error:
            self._json(400, {"error": str(error)})
        except RuntimeError as error:
            self._json(409, {"error": str(error)})
        except (OSError, subprocess.SubprocessError) as error:
            detail = error.strerror if isinstance(error, OSError) else "시간이 초과되었습니다."
            self._json(500, {"error": f"서버를 제어할 수 없습니다. {detail or ''}".strip()})
        else:
            self._json(200, result)

    def log_message(self, format, *args):
        # The page continuously polls status; keep the terminal focused on startup.
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="WSL에서 llama-server를 제어하는 로컬 웹 런처")
    parser.add_argument("--port", type=int, default=8765, help="런처 웹 포트 (기본값: %(default)s)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port는 1~65535 사이여야 합니다.")

    def stop_on_signal(signum, frame):
        raise KeyboardInterrupt

    with TemporaryDirectory(prefix="shopagent-llama-", dir="/tmp") as directory:
        launcher = Launcher(Path(directory))
        previous = signal.signal(signal.SIGTERM, stop_on_signal)
        try:
            with LauncherServer(args.port, launcher, load_presets()) as server:
                print(f"브라우저에서 http://{server.authority} 열기", flush=True)
                server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous)
            launcher.stop()


if __name__ == "__main__":
    main()
