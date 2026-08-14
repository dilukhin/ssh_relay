#!/usr/bin/env python3
"""Тесты status/stop во время занятой команды и reconnect."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssh_relay  # noqa: F401
import ssh_relay_core as core


ROOT = Path(__file__).resolve().parents[1]
DAEMON_RUNNER = Path(__file__).with_name("daemon_exec_fake_runner.py")


class BusyShutdownTests(unittest.TestCase):
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

        env = os.environ.copy()
        env.update(self.overrides)
        env["SSH_RELAY_FAKE_CONTROL"] = str(self.control)
        env["PYTHONIOENCODING"] = "utf-8"
        self.process: subprocess.Popen[str] | None = subprocess.Popen(
            [sys.executable, "-u", str(DAEMON_RUNNER)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.session = self.wait_session()
        self.wait_connected()

    def tearDown(self) -> None:
        try:
            (self.control / "release_blocked").touch(exist_ok=True)
            if self.process is not None:
                process = self.process
                if process.poll() is None:
                    try:
                        core.request_daemon(self.session, "stop", response_timeout=2)
                    except core.RelayError:
                        pass
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.communicate(timeout=3)
        finally:
            self.env_patcher.stop()
            self.tmp.cleanup()

    def fail_if_exited(self) -> None:
        if self.process is None or self.process.poll() is None:
            return
        stdout, stderr = self.process.communicate()
        self.fail(
            f"Busy-shutdown daemon завершился раньше времени, код {self.process.returncode}.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def wait_session(self, timeout: float = 5.0) -> dict:
        path = core.session_file_path("ci-core")
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.fail_if_exited()
            try:
                json.loads(path.read_text(encoding="utf-8"))
                return core.read_session("ci-core")
            except (OSError, json.JSONDecodeError, core.RelayError) as exc:
                last_error = exc
            time.sleep(0.02)
        if last_error is not None:
            raise AssertionError(f"Session-файл busy-shutdown daemon не стал доступен: {last_error}") from last_error
        raise AssertionError("Session-файл busy-shutdown daemon не появился.")

    def status(self) -> dict:
        self.fail_if_exited()
        return core.request_daemon(self.session, "status", response_timeout=1)

    def wait_connected(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self.status()
            if last.get("ok") and last.get("ssh_status") == "connected":
                return last
            time.sleep(0.03)
        self.fail(f"Busy-shutdown daemon не перешёл в connected: {last}")

    def wait_nonconnected(self, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self.status()
            if last.get("ssh_status") != "connected":
                return last
            time.sleep(0.03)
        self.fail(f"SSH остался connected: {last}")

    def request_exec(self, command: str, *, timeout: float = 4.0) -> dict:
        return core.request_daemon(
            self.session,
            "exec",
            command=command,
            risky=False,
            receipt_path=core.DEFAULT_RISKY_RECEIPT_PATH,
            response_timeout=timeout,
        )

    def event_lines(self) -> list[str]:
        path = self.control / "events.log"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def wait_event(self, expected: str, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if expected in self.event_lines():
                return
            time.sleep(0.01)
        self.fail(f"Не дождались события {expected}: {self.event_lines()}")

    def command_lines(self) -> list[str]:
        path = self.control / "commands.log"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def finish_process(self) -> tuple[str, str]:
        assert self.process is not None
        process = self.process
        stdout, stderr = process.communicate(timeout=4)
        self.assertEqual(0, process.returncode, stdout + stderr)
        self.process = None
        return stdout, stderr

    def assert_listener_closed_and_session_removed(self) -> None:
        port = int(self.session["daemon_port"])
        self.assertFalse(core.session_file_path("ci-core").exists())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)

    def test_status_remains_responsive_while_remote_operation_holds_lock(self) -> None:
        result: dict[str, dict] = {}
        worker = threading.Thread(
            target=lambda: result.__setitem__("exec", self.request_exec("test:blocked")),
            daemon=True,
        )
        worker.start()
        self.wait_event("start:blocked")

        status = self.status()
        self.assertTrue(status.get("ok"))
        self.assertEqual("connected", status.get("ssh_status"))
        self.assertNotIn("end:blocked", self.event_lines())

        (self.control / "release_blocked").touch()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result["exec"].get("ok"))
        self.assertEqual("blocked-ok\n", result["exec"].get("stdout"))
        self.assertEqual(["start:blocked", "end:blocked"], self.event_lines())

    def test_stop_during_busy_command_exits_without_waiting_for_command_completion(self) -> None:
        outcome: dict[str, object] = {}

        def run_exec() -> None:
            try:
                outcome["result"] = self.request_exec("test:blocked", timeout=4)
            except core.RelayError as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=run_exec, daemon=True)
        worker.start()
        self.wait_event("start:blocked")

        response = core.request_daemon(self.session, "stop", response_timeout=2)
        self.assertTrue(response.get("ok"))
        self.finish_process()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, self.command_lines().count("test:blocked"))
        self.assertNotIn("end:blocked", self.event_lines())
        self.assert_listener_closed_and_session_removed()

        if "result" in outcome:
            self.assertFalse(bool(outcome["result"].get("ok")))  # type: ignore[union-attr]
        else:
            self.assertIn("error", outcome)

    def test_stop_during_reconnect_exits_cleanly_without_new_remote_operation(self) -> None:
        (self.control / "reconnect_failures.txt").write_text("100", encoding="utf-8")
        trigger = self.request_exec("test:disconnect-after-success")
        self.assertTrue(trigger.get("ok"))
        nonconnected = self.wait_nonconnected()
        self.assertIn(nonconnected.get("ssh_status"), {"reconnecting", "disconnected"})

        response = core.request_daemon(self.session, "stop", response_timeout=2)
        self.assertTrue(response.get("ok"))
        self.finish_process()
        self.assert_listener_closed_and_session_removed()
        self.assertEqual(1, self.command_lines().count("test:disconnect-after-success"))


if __name__ == "__main__":
    unittest.main()
