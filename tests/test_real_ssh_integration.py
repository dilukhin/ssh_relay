#!/usr/bin/env python3
"""Интеграция relay с настоящим loopback SSH transport Paramiko."""

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

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ssh_relay  # noqa: F401
import ssh_relay_core as core
from localhost_ssh_server import LoopbackSSHServer


ROOT = Path(__file__).resolve().parents[1]
DAEMON_RUNNER = Path(__file__).with_name("daemon_real_ssh_runner.py")
PASSWORD = "relay-test-password"


class RealSSHIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.host_key = paramiko.RSAKey.generate(2048)
        cls.wrong_host_key = paramiko.RSAKey.generate(2048)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.known_hosts = Path(self.tmp.name) / "known_hosts"
        self.server = LoopbackSSHServer(self.host_key, password=PASSWORD)
        self.server.start()
        self.server.write_known_hosts(self.known_hosts)

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
            self.server.stop()
            self.env_patcher.stop()
            self.tmp.cleanup()

    def start_daemon(
        self,
        *,
        password: str = PASSWORD,
        expect_session: bool = True,
    ) -> subprocess.Popen[str]:
        child_env = os.environ.copy()
        child_env.update(self.overrides)
        child_env["SSH_RELAY_REAL_SSH_PASSWORD"] = password
        child_env["SSH_RELAY_REAL_SSH_PORT"] = str(self.server.port)
        child_env["SSH_RELAY_REAL_KNOWN_HOSTS"] = str(self.known_hosts)
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
            self.wait_connected()
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

    def fail_if_exited(self) -> None:
        if self.process is None or self.process.poll() is None:
            return
        stdout, stderr = self.process.communicate()
        self.fail(
            f"Real-SSH daemon завершился раньше времени, код {self.process.returncode}.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def wait_session(self, timeout: float = 7.0) -> dict:
        path = core.session_file_path("ci-real-ssh")
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.fail_if_exited()
            try:
                json.loads(path.read_text(encoding="utf-8"))
                return core.read_session("ci-real-ssh")
            except (OSError, json.JSONDecodeError, core.RelayError) as exc:
                last_error = exc
            time.sleep(0.02)
        if last_error is not None:
            raise AssertionError(f"Session-файл real-SSH daemon не стал доступен: {last_error}") from last_error
        raise AssertionError("Session-файл real-SSH daemon не появился.")

    def status(self) -> dict:
        assert self.session is not None
        self.fail_if_exited()
        return core.request_daemon(self.session, "status", response_timeout=2)

    def wait_connected(self, timeout: float = 7.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self.status()
            if last.get("ok") and last.get("ssh_status") == "connected":
                return last
            time.sleep(0.03)
        self.fail(f"Real SSH не перешёл в connected: {last}")

    def exec(self, command: str) -> dict:
        assert self.session is not None
        return core.request_daemon(
            self.session,
            "exec",
            command=command,
            risky=False,
            receipt_path=core.DEFAULT_RISKY_RECEIPT_PATH,
            response_timeout=5,
        )

    def test_real_password_auth_known_hosts_and_exec_contract(self) -> None:
        self.start_daemon()
        self.assertTrue(self.server.wait_for_connections(1))
        self.assertIn(("donpedro", PASSWORD), self.server.auth_attempts)

        success = self.exec("test:real-success")
        self.assertTrue(success.get("ok"))
        self.assertEqual("real-stdout\n", success.get("stdout"))
        self.assertEqual("real-stderr\n", success.get("stderr"))
        self.assertEqual(0, success.get("exit_code"))

        failed = self.exec("test:real-exit7")
        self.assertTrue(failed.get("ok"))
        self.assertEqual("", failed.get("stdout"))
        self.assertEqual("real-failed\n", failed.get("stderr"))
        self.assertEqual(7, failed.get("exit_code"))

        empty = self.exec("test:real-empty")
        self.assertTrue(empty.get("ok"))
        self.assertEqual("", empty.get("stdout"))
        self.assertEqual("", empty.get("stderr"))
        self.assertEqual(0, empty.get("exit_code"))
        self.assertEqual(
            ["test:real-success", "test:real-exit7", "test:real-empty"],
            self.server.commands,
        )

    def test_real_host_key_mismatch_prevents_session_creation(self) -> None:
        self.server.write_known_hosts(self.known_hosts, key=self.wrong_host_key)
        process = self.start_daemon(expect_session=False)
        stdout, stderr = process.communicate(timeout=8)
        self.process = None

        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertFalse(core.session_file_path("ci-real-ssh").exists())
        self.assertNotIn(PASSWORD, stdout)
        self.assertNotIn(PASSWORD, stderr)
        self.assertEqual([], self.server.auth_attempts)

    def test_real_wrong_password_prevents_session_creation(self) -> None:
        process = self.start_daemon(password="wrong-test-password", expect_session=False)
        stdout, stderr = process.communicate(timeout=8)
        self.process = None

        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertFalse(core.session_file_path("ci-real-ssh").exists())
        self.assertIn(("donpedro", "wrong-test-password"), self.server.auth_attempts)
        self.assertNotIn("wrong-test-password", stdout)
        self.assertNotIn("wrong-test-password", stderr)

    def test_real_transport_drop_reconnects_and_next_exec_succeeds(self) -> None:
        self.start_daemon()
        first = self.exec("test:real-success")
        self.assertTrue(first.get("ok"))
        self.assertEqual(1, self.server.commands.count("test:real-success"))

        initial_connections = self.server.connection_count
        self.server.drop_all_transports()
        self.assertTrue(
            self.server.wait_for_connections(initial_connections + 1, timeout=7),
            "Daemon не открыл новое реальное SSH-соединение после transport drop.",
        )
        self.wait_connected(timeout=7)

        second = self.exec("test:real-success")
        self.assertTrue(second.get("ok"))
        self.assertEqual("real-stdout\n", second.get("stdout"))
        self.assertEqual(2, self.server.commands.count("test:real-success"))
        self.assertGreaterEqual(self.server.connection_count, initial_connections + 1)


if __name__ == "__main__":
    unittest.main()
