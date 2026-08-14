#!/usr/bin/env python3
"""Интеграционные тесты host key, секретов и sudo."""

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

import ssh_relay  # noqa: F401
import ssh_relay_core as core


ROOT = Path(__file__).resolve().parents[1]
DAEMON_RUNNER = Path(__file__).with_name("daemon_security_fake_runner.py")
SSH_SECRET = "TEST_SSH_SECRET_DO_NOT_LEAK_8d6470"
SUDO_SECRET = "TEST_SUDO_SECRET_DO_NOT_LEAK_1b39ac"


class SecurityIntegrationTests(unittest.TestCase):
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
        self.process: subprocess.Popen[str] | None = None
        self.session: dict | None = None

    def tearDown(self) -> None:
        try:
            self.stop_daemon()
        finally:
            self.env_patcher.stop()
            self.tmp.cleanup()

    def start_daemon(self, mode: str = "normal", *, expect_session: bool = True) -> subprocess.Popen[str]:
        child_env = os.environ.copy()
        child_env.update(self.overrides)
        child_env["SSH_RELAY_SECURITY_CONTROL"] = str(self.control)
        child_env["SSH_RELAY_SECURITY_MODE"] = mode
        child_env["SSH_RELAY_TEST_SSH_SECRET"] = SSH_SECRET
        child_env["SSH_RELAY_TEST_SUDO_SECRET"] = SUDO_SECRET
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
        if expect_session:
            self.session = self.wait_session()
            self.wait_status("connected")
        return self.process

    def stop_daemon(self) -> tuple[str, str]:
        if self.process is None:
            return "", ""

        process = self.process
        if process.poll() is None and self.session is not None:
            try:
                core.request_daemon(self.session, "stop", response_timeout=2)
            except core.RelayError:
                pass
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=3)
        self.process = None
        self.session = None
        return stdout, stderr

    def wait_session(self, timeout: float = 5.0) -> dict:
        path = core.session_file_path("ci-security")
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.fail_if_exited()
            try:
                json.loads(path.read_text(encoding="utf-8"))
                return core.read_session("ci-security")
            except (OSError, json.JSONDecodeError, core.RelayError) as exc:
                last_error = exc
            time.sleep(0.02)
        if last_error is not None:
            raise AssertionError(f"Session-файл не стал доступен: {last_error}") from last_error
        raise AssertionError("Session-файл security-daemon не появился.")

    def fail_if_exited(self) -> None:
        if self.process is None or self.process.poll() is None:
            return
        stdout, stderr = self.process.communicate()
        self.fail(
            f"Security-daemon завершился раньше времени, код {self.process.returncode}.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def status(self) -> dict:
        assert self.session is not None
        return core.request_daemon(self.session, "status", response_timeout=1)

    def wait_status(self, expected: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            self.fail_if_exited()
            last = self.status()
            if last.get("ssh_status") == expected:
                return last
            time.sleep(0.03)
        self.fail(f"SSH status не стал {expected}: {last}")

    def wait_nonconnected(self, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self.status()
            if last.get("ssh_status") != "connected":
                return last
            time.sleep(0.03)
        self.fail(f"SSH остался connected: {last}")

    def request(self, action: str, **payload) -> dict:
        assert self.session is not None
        return core.request_daemon(self.session, action, response_timeout=4, **payload)

    def exec(self, command: str) -> dict:
        return self.request(
            "exec",
            command=command,
            risky=False,
            receipt_path=core.DEFAULT_RISKY_RECEIPT_PATH,
        )

    def sudo_exec(self, command: str) -> dict:
        return self.request(
            "sudo_exec",
            command=command,
            risky=False,
            receipt_path=core.DEFAULT_RISKY_RECEIPT_PATH,
        )

    def log_lines(self, name: str) -> list[str]:
        path = self.control / name
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def assert_no_secrets(self, *texts: str) -> None:
        combined = "\n".join(texts)
        self.assertNotIn(SSH_SECRET, combined)
        self.assertNotIn(SUDO_SECRET, combined)

    def test_known_hosts_and_reject_policy_are_used(self) -> None:
        self.start_daemon("known-hosts")
        expected = str(self.control / "known_hosts")
        self.assertEqual([expected], self.log_lines("host_keys.log"))
        self.assertEqual(["FakeRejectPolicy"], self.log_lines("policy.log"))

    def test_host_key_failure_blocks_startup_without_secret_leak(self) -> None:
        process = self.start_daemon("host-key-fail", expect_session=False)
        stdout, stderr = process.communicate(timeout=5)
        self.process = None

        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertIn("known_hosts", stderr)
        self.assertFalse(core.session_file_path("ci-security").exists())
        self.assert_no_secrets(stdout, stderr)

    def test_reconnect_rechecks_host_key_and_rejects_changed_key(self) -> None:
        self.start_daemon("reconnect-host-key")
        result = self.exec("test:disconnect-and-reject")
        self.assertTrue(result.get("ok"))

        status = self.wait_nonconnected()
        self.assertIn(status.get("ssh_status"), {"reconnecting", "disconnected"})

        deadline = time.monotonic() + 2.0
        while len(self.log_lines("connect.log")) < 3 and time.monotonic() < deadline:
            time.sleep(0.03)

        self.assertGreaterEqual(len(self.log_lines("connect.log")), 3)
        self.assertGreaterEqual(len(self.log_lines("host_keys.log")), 3)
        self.assertTrue(all(item == "FakeRejectPolicy" for item in self.log_lines("policy.log")))
        self.assertNotEqual("connected", self.status().get("ssh_status"))

    def test_dependency_error_cannot_echo_ssh_password(self) -> None:
        process = self.start_daemon("connect-error-secret", expect_session=False)
        stdout, stderr = process.communicate(timeout=5)
        self.process = None

        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertFalse(core.session_file_path("ci-security").exists())
        self.assert_no_secrets(stdout, stderr)

    def test_sudo_password_is_memory_only_and_not_printed(self) -> None:
        self.start_daemon("sudo")
        assert self.session is not None
        session_path = core.session_file_path("ci-security")
        raw_session = session_path.read_text(encoding="utf-8")
        self.assert_no_secrets(raw_session)

        result = self.sudo_exec("test:sudo-success")
        self.assertTrue(result.get("ok"))
        self.assertEqual("sudo-ok\n", result.get("stdout"))
        self.assertEqual(0, result.get("exit_code"))

        stdin_lines = self.log_lines("stdin.log")
        self.assertGreaterEqual(stdin_lines.count(SUDO_SECRET), 2)

        auth_token = str(self.session["auth_token"])
        stdout, stderr = self.stop_daemon()
        self.assert_no_secrets(stdout, stderr)
        self.assertNotIn(auth_token, stdout)
        self.assertNotIn(auth_token, stderr)

    def test_sudo_disabled_is_rejected(self) -> None:
        self.start_daemon("normal")
        result = self.sudo_exec("test:sudo-success")
        self.assertFalse(result.get("ok"))
        self.assertIn("Режим sudo не включён", str(result.get("protocol_error")))

    def test_sudo_preserves_nonzero_exit_and_does_not_replay_on_drop(self) -> None:
        self.start_daemon("sudo")
        failed = self.sudo_exec("test:sudo-exit7")
        self.assertTrue(failed.get("ok"))
        self.assertEqual("", failed.get("stdout"))
        self.assertEqual("sudo-failed\n", failed.get("stderr"))
        self.assertEqual(7, failed.get("exit_code"))

        dropped = self.sudo_exec("test:sudo-drop")
        self.assertFalse(dropped.get("ok"))
        self.assertIn("Результат операции неизвестен", str(dropped.get("protocol_error")))

        commands = self.log_lines("commands.log")
        self.assertEqual(1, sum("test:sudo-exit7" in item for item in commands))
        self.assertEqual(1, sum("test:sudo-drop" in item for item in commands))

    def test_wrong_sudo_password_prevents_session_creation_without_leak(self) -> None:
        process = self.start_daemon("sudo-fail", expect_session=False)
        stdout, stderr = process.communicate(timeout=5)
        self.process = None

        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertIn("Проверка sudo-пароля не прошла", stderr)
        self.assertFalse(core.session_file_path("ci-security").exists())
        self.assert_no_secrets(stdout, stderr)


if __name__ == "__main__":
    unittest.main()
