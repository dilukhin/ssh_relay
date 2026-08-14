#!/usr/bin/env python3
"""Интеграционные тесты базового exec и автоматического SSH reconnect."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Сначала устанавливаем расширения CLI над core.
import ssh_relay  # noqa: F401
import ssh_relay_core as core


ROOT = Path(__file__).resolve().parents[1]
DAEMON_RUNNER = Path(__file__).with_name("daemon_exec_fake_runner.py")


class CoreExecReconnectIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.control = Path(self.tmp.name) / "control"
        self.state.mkdir()
        self.control.mkdir()

        self.overrides = {
            "LOCALAPPDATA": str(self.state),
            "XDG_STATE_HOME": str(self.state),
        }
        self.env_patcher = patch.dict(os.environ, self.overrides, clear=False)
        self.env_patcher.start()

        child_env = os.environ.copy()
        child_env.update(self.overrides)
        child_env["SSH_RELAY_FAKE_CONTROL"] = str(self.control)
        child_env["PYTHONIOENCODING"] = "utf-8"

        self.process = subprocess.Popen(
            [sys.executable, "-u", str(DAEMON_RUNNER)],
            cwd=str(ROOT),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        session_path = core.session_file_path("ci-core")
        self.session = self.wait_session(session_path)
        self.wait_connected()

    def tearDown(self) -> None:
        try:
            if self.process.poll() is None:
                try:
                    core.request_daemon(self.session, "stop", response_timeout=2)
                except core.RelayError:
                    pass
                try:
                    self.process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    self.process.communicate(timeout=5)
            else:
                self.process.communicate()
        finally:
            self.env_patcher.stop()
            self.tmp.cleanup()

    def fail_if_process_exited(self) -> None:
        if self.process.poll() is None:
            return
        stdout, stderr = self.process.communicate()
        self.fail(
            f"Тестовый daemon завершился раньше времени, код {self.process.returncode}.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def wait_session(self, path: Path, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.fail_if_process_exited()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("daemon_port"):
                    return core.read_session("ci-core")
            except (OSError, json.JSONDecodeError, core.RelayError) as exc:
                last_error = exc
            time.sleep(0.02)
        if last_error is not None:
            raise AssertionError(f"Session-файл не стал доступен: {last_error}") from last_error
        raise AssertionError("Session-файл тестового daemon не появился.")

    def status(self) -> dict:
        self.fail_if_process_exited()
        return core.request_daemon(self.session, "status", response_timeout=1)

    def wait_connected(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self.status()
            if last.get("ok") and last.get("ssh_status") == "connected":
                return last
            time.sleep(0.03)
        self.fail(f"SSH не перешёл в connected: {last}")

    def request_exec(self, command: str, response_timeout: float = 4.0) -> dict:
        self.fail_if_process_exited()
        return core.request_daemon(
            self.session,
            "exec",
            command=command,
            risky=False,
            receipt_path=core.DEFAULT_RISKY_RECEIPT_PATH,
            response_timeout=response_timeout,
        )

    def set_reconnect_failures(self, count: int) -> None:
        (self.control / "reconnect_failures.txt").write_text(str(count), encoding="utf-8")

    def command_lines(self) -> list[str]:
        path = self.control / "commands.log"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def connect_count(self) -> int:
        path = self.control / "connect.log"
        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

    def test_exec_preserves_stdout_stderr_empty_output_and_exit_code(self) -> None:
        mixed = self.request_exec("test:mixed")
        self.assertTrue(mixed.get("ok"))
        self.assertEqual("stdout-ok\n", mixed.get("stdout"))
        self.assertEqual("stderr-ok\n", mixed.get("stderr"))
        self.assertEqual(0, mixed.get("exit_code"))

        failed = self.request_exec("test:exit7")
        self.assertTrue(failed.get("ok"))
        self.assertEqual("", failed.get("stdout"))
        self.assertEqual("failed\n", failed.get("stderr"))
        self.assertEqual(7, failed.get("exit_code"))

        empty = self.request_exec("test:empty")
        self.assertTrue(empty.get("ok"))
        self.assertEqual("", empty.get("stdout"))
        self.assertEqual("", empty.get("stderr"))
        self.assertEqual(0, empty.get("exit_code"))

    def test_dead_transport_reconnects_before_next_exec_without_replay(self) -> None:
        self.set_reconnect_failures(3)
        trigger = self.request_exec("test:disconnect-after-success")
        self.assertTrue(trigger.get("ok"))
        self.assertEqual(0, trigger.get("exit_code"))

        observed_non_connected = False
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            current = self.status()
            if current.get("ssh_status") != "connected":
                observed_non_connected = True
            if self.connect_count() >= 5 and current.get("ssh_status") == "connected":
                break
            time.sleep(0.03)

        self.assertTrue(observed_non_connected, "Не зафиксировано состояние reconnect/disconnected.")
        self.assertGreaterEqual(self.connect_count(), 5)
        self.assertEqual("connected", self.wait_connected().get("ssh_status"))

        result = self.request_exec("test:success")
        self.assertTrue(result.get("ok"))
        self.assertEqual("stdout-ok\n", result.get("stdout"))

        lines = self.command_lines()
        self.assertEqual(1, lines.count("test:disconnect-after-success"))
        self.assertEqual(1, lines.count("test:success"))

    def test_drop_during_exec_is_unknown_and_command_is_not_retried(self) -> None:
        result = self.request_exec("test:drop-during")
        self.assertFalse(result.get("ok"))
        self.assertIn("Результат операции неизвестен", str(result.get("protocol_error")))
        self.assertIn("автоматически не повторялась", str(result.get("protocol_error")))
        self.assertEqual(1, self.command_lines().count("test:drop-during"))

        self.wait_connected()
        follow_up = self.request_exec("test:success")
        self.assertTrue(follow_up.get("ok"))
        self.assertEqual(1, self.command_lines().count("test:success"))

    def test_reconnect_timeout_does_not_start_waiting_command(self) -> None:
        self.set_reconnect_failures(100)
        trigger = self.request_exec("test:disconnect-after-success")
        self.assertTrue(trigger.get("ok"))

        result = self.request_exec("test:never-run", response_timeout=4)
        self.assertFalse(result.get("ok"))
        self.assertIn("удалённый запрос не выполнялся", str(result.get("protocol_error")))
        self.assertEqual(0, self.command_lines().count("test:never-run"))

    def test_command_timeout_does_not_break_daemon(self) -> None:
        result = self.request_exec("test:hang", response_timeout=4)
        self.assertFalse(result.get("ok"))
        self.assertIn("Превышено время выполнения команды: 1 с", str(result.get("protocol_error")))
        self.assertEqual(1, self.command_lines().count("test:hang"))

        status = self.status()
        self.assertTrue(status.get("ok"))
        self.assertEqual("connected", status.get("ssh_status"))

        follow_up = self.request_exec("test:success")
        self.assertTrue(follow_up.get("ok"))
        self.assertEqual(0, follow_up.get("exit_code"))

    def test_wrong_token_and_unknown_action_are_rejected_without_daemon_crash(self) -> None:
        wrong = dict(self.session)
        wrong["auth_token"] = "wrong-token"
        denied = core.request_daemon(wrong, "status", response_timeout=1)
        self.assertFalse(denied.get("ok"))
        self.assertIn("Доступ к relay отклонён", str(denied.get("protocol_error")))

        unknown = core.request_daemon(self.session, "unsupported-action", response_timeout=1)
        self.assertFalse(unknown.get("ok"))
        self.assertIn("Неизвестное действие relay", str(unknown.get("protocol_error")))

        self.assertEqual("connected", self.wait_connected().get("ssh_status"))


if __name__ == "__main__":
    unittest.main()
