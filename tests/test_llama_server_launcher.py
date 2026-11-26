import errno
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from scripts import llama_server_launcher as launcher


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.presets = launcher.load_presets()

    def test_g12_native_command(self):
        command, host, port = launcher.build_command(self.presets, "g12-native", {})
        self.assertEqual((host, port), ("127.0.0.1", 18380))
        self.assertEqual(command, [
            str(Path.home() / "llama.cpp/build/bin/llama-server"), "-m",
            str(Path.home() / "models/gemma-4-12b-it-Q4_K_M.gguf"),
            "--jinja", "-ngl", "99", "-ctk", "q8_0", "-ctv", "q8_0", "-fa", "on",
            "-np", "1", "-c", "16384", "--host", "127.0.0.1", "--port", "18380",
            "--n-cpu-moe", "0", "--chat-template-kwargs", '{"enable_thinking": false}',
            "--reasoning-budget", "0",
        ])

    def test_thinking_and_budget(self):
        for overrides, enabled, budget in (
            ({"thinking": True}, True, "-1"),
            ({"thinking": True, "reasoning_budget": "128"}, True, "128"),
            ({"thinking": False, "reasoning_budget": "128"}, False, "0"),
        ):
            with self.subTest(overrides=overrides):
                command, _, _ = launcher.build_command(self.presets, "g12-native", overrides)
                self.assertEqual(json.loads(command[command.index("--chat-template-kwargs") + 1]),
                                 {"enable_thinking": enabled})
                self.assertEqual(command[command.index("--reasoning-budget") + 1], budget)

    def test_local_decision_presets(self):
        for name, label, ctx, cache in (
            ("clef-g12", "Clef", "4096", "q8_0"),
            ("pplx-g12", "pplx", "4096", "f16"),
            ("jev-g12", "Omni", "2048", "q8_0"),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.presets[name]["label"], label)
                self.assertTrue(self.presets[name]["paired"])
                command, _, _ = launcher.build_command(self.presets, name, {})
                for flag, value in (("-c", ctx), ("-b", ctx), ("-ub", ctx), ("-ctk", cache), ("-ctv", cache)):
                    self.assertEqual(command[command.index(flag) + 1], value)
                self.assertEqual("--embedding" in command, name != "clef-g12")
        self.assertNotIn("solar-pro4", self.presets)
        with self.assertRaises(ValueError):
            launcher.build_command(self.presets, "solar-pro4", {})

    def test_preset_changes_are_read_without_executing_module(self):
        source = Mock()
        source.read_text.return_value = """
raise RuntimeError('should not execute')
MODEL = 'changed.gguf'
CONFIGS = {'g12-native': {'backend': 'gemma', 'decision': MODEL, 'ctx': 8192},
           'other-local': {'backend': 'custom', 'decision': 'custom.gguf'}}
GEMMA_NGL = {'g12-native': 42}
"""
        presets = launcher.load_presets(source)
        self.assertIn("other-local", presets)
        defaults = presets["g12-native"]["defaults"]
        self.assertEqual((defaults["ctx"], defaults["ngl"]), (8192, 42))
        self.assertEqual(Path(defaults["model"]).name, "changed.gguf")

    def test_rejects_nonlocal_addresses_and_invalid_ports(self):
        for overrides in ({"host": "0.0.0.0"}, {"host": "::"}, {"host": "192.168.1.1"},
                          {"port": 0}, {"port": "65536"}, {"port": True}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                launcher.build_command(self.presets, "g12-native", overrides)
        _, host, port = launcher.build_command(self.presets, "g12-native", {"host": "127.0.0.2", "port": "18381"})
        self.assertEqual((host, port), ("127.0.0.2", 18381))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp"))
        self.launcher = launcher.Launcher(Path(directory))
        presets = launcher.load_presets()
        self.command = launcher.build_command(presets, "g12-native", {})
        self.other_servers = self.enterContext(patch.object(launcher, "existing_llama_servers", return_value=[]))
        self.busy_port = self.enterContext(patch.object(launcher, "port_is_busy", return_value=False))
        self.spawn = self.enterContext(patch.object(launcher.subprocess, "Popen"))
        self.enterContext(patch.dict(os.environ, {"LLAMA_ARG_HOST": "0.0.0.0"}, clear=True))
        self.child = self.spawn.return_value
        self.child.pid = 4312
        self.child.poll.return_value = None

    def test_refuses_foreign_server_before_checking_port(self):
        self.other_servers.return_value = ["9999"]
        with self.assertRaisesRegex(RuntimeError, "다른 llama-server"):
            self.launcher.start(*self.command)
        self.busy_port.assert_not_called()
        self.spawn.assert_not_called()

    def test_refuses_busy_port(self):
        self.busy_port.return_value = True
        with self.assertRaisesRegex(RuntimeError, "18380 포트"):
            self.launcher.start(*self.command)
        self.busy_port.assert_called_once_with("127.0.0.1", 18380)
        self.spawn.assert_not_called()

    def test_stop_uses_only_the_tracked_child_and_escalates_on_timeout(self):
        self.assertEqual(self.launcher.start(*self.command), 4312)
        self.assertEqual(self.launcher.pid_path.read_text(), "4312\n")
        self.assertEqual(self.spawn.call_args.args[0], self.command[0])
        self.assertNotIn("LLAMA_ARG_HOST", self.spawn.call_args.kwargs["env"])
        self.assertEqual(self.launcher.status()["endpoint"], ("127.0.0.1", 18380))
        with self.assertRaisesRegex(RuntimeError, "이미 서버를 실행 중"):
            self.launcher.start(*self.command)
        self.spawn.assert_called_once()

        def killed():
            self.child.poll.return_value = -9

        timeout = launcher.STOP_TIMEOUT
        self.child.wait.side_effect = [subprocess.TimeoutExpired("llama-server", timeout), -9]
        self.child.kill.side_effect = killed
        self.child.reset_mock()
        self.assertTrue(self.launcher.stop())
        self.assertEqual(self.child.method_calls, [call.poll(), call.terminate(), call.wait(timeout=timeout),
                                                   call.kill(), call.wait(timeout=timeout), call.poll()])
        self.assertFalse(self.launcher.pid_path.exists())
        self.assertIsNone(self.launcher.status()["pid"])

    def test_stale_pid_file_cannot_signal_a_foreign_process(self):
        self.launcher.pid_path.write_text("9999\n")
        with patch.object(os, "kill") as kill:
            self.assertFalse(self.launcher.stop())
        kill.assert_not_called()
        self.spawn.assert_not_called()

    def test_process_exit_keeps_recent_log(self):
        self.launcher.start(*self.command)
        self.launcher.log_path.write_text("\n".join(f"로그 {number}" for number in range(100)))
        self.child.poll.return_value = 1
        status = self.launcher.status()
        self.assertFalse(self.launcher.pid_path.exists())
        self.assertIsNone(status["pid"])
        self.assertEqual(status["exit_code"], 1)
        self.assertEqual(status["log"].splitlines(), [f"로그 {number}" for number in range(20, 100)])


class MonitorTests(unittest.TestCase):
    def test_process_check_parses_ps_and_fails_closed(self):
        with patch.object(launcher.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="  42\n 105\n")
            self.assertEqual(launcher.existing_llama_servers(), ["42", "105"])
            run.return_value = SimpleNamespace(returncode=2)
            with self.assertRaises(RuntimeError):
                launcher.existing_llama_servers()

    def test_port_probe_checks_the_requested_address(self):
        with patch.object(launcher.socket, "socket") as sock:
            probe = sock.return_value.__enter__.return_value
            self.assertFalse(launcher.port_is_busy("127.0.0.1", 18380))
            probe.bind.assert_called_once_with(("127.0.0.1", 18380))
            probe.bind.side_effect = OSError(errno.EADDRINUSE, "busy")
            self.assertTrue(launcher.port_is_busy("127.0.0.1", 18380))

    def test_health_parses_ready_loading_and_unreachable(self):
        with patch.object(launcher, "HTTPConnection") as connect:
            connection = connect.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"status":"ok"}'
            self.assertEqual(launcher.health("127.0.0.1", 18380), "응답 준비 완료")
            connection.request.assert_called_once_with("GET", "/health")
            connect.assert_called_once_with("127.0.0.1", 18380, timeout=1)
            connection.close.assert_called_once()
            response.status = 503
            self.assertIn("불러오는 중", launcher.health("127.0.0.1", 18380))
            connection.request.side_effect = ConnectionRefusedError
            self.assertEqual(launcher.health("127.0.0.1", 18380), "응답 없음")

    def test_vram_parses_nvidia_smi_csv(self):
        with patch.object(launcher.shutil, "which", return_value="/mock/nvidia-smi"), \
                patch.object(launcher.subprocess, "run", return_value=SimpleNamespace(stdout="0, 123, 456\n1, 3, 8\n")) as run:
            rows, error = launcher.vram()
        self.assertIsNone(error)
        self.assertEqual(rows, [{"index": 0, "used_mib": 123, "total_mib": 456},
                                {"index": 1, "used_mib": 3, "total_mib": 8}])
        self.assertEqual(run.call_args.args[0][0], "/mock/nvidia-smi")

    def test_missing_nvidia_smi_is_reported_without_running_subprocess(self):
        with patch.object(launcher.shutil, "which", return_value=None), patch.object(Path, "is_file", return_value=False), \
                patch.object(launcher.subprocess, "run") as run:
            self.assertEqual(launcher.vram(), ([], "nvidia-smi를 찾을 수 없습니다."))
        run.assert_not_called()


class WebTests(unittest.TestCase):
    def test_web_server_binds_only_127_0_0_1(self):
        def bind(server, address, handler):
            server.server_port = address[1]

        with patch.object(ThreadingHTTPServer, "__init__", autospec=True, side_effect=bind) as listen:
            server = launcher.LauncherServer(8765, Mock(), {})
        listen.assert_called_once_with(server, ("127.0.0.1", 8765), launcher.Handler)

    def test_web_preview_and_start_share_the_exact_argv(self):
        handler = launcher.Handler.__new__(launcher.Handler)
        handler.server = SimpleNamespace(authority="127.0.0.1:8765", presets=launcher.load_presets(), launcher=Mock())
        handler.server.launcher.start.return_value = 4312
        body = json.dumps({"preset": "g12-native", "options": {"model": "/models/with space.gguf"}}).encode()
        handler.headers = Mock()
        handler.headers.get.side_effect = {"Host": handler.server.authority, "Origin": "http://127.0.0.1:8765",
                                           "Content-Length": str(len(body))}.get
        handler.headers.get_content_type.return_value = "application/json"
        handler._json = Mock()
        for path in ("/api/preview", "/api/start"):
            handler.path = path
            handler.rfile = io.BytesIO(body)
            handler.do_POST()
        [preview, start] = handler._json.call_args_list
        self.assertEqual(preview.args[0], 200)
        self.assertEqual(start.args, (200, {**preview.args[1], "pid": 4312}))
        handler.server.launcher.start.assert_called_once_with(shlex.split(preview.args[1]["command"]), "127.0.0.1", 18380)


if __name__ == "__main__":
    unittest.main()
