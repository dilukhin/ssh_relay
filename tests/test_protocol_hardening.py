#!/usr/bin/env python3
"""Hardening-тесты локального протокола, лимитов, сериализации и shutdown."""

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


class MessageProtocolUnitTests(unittest.TestCase):
    def read_payload(self, payload: bytes, *, max_size: int | None = None):
        reader, writer = socket.socketpair()
        try:
            writer.sendall(payload)
            writer.shutdown(socket.SHUT_WR)
            if max_size is None:
                return core.read_message(reader)
            with patch.object(core, "MAX_MESSAGE_SIZE", max_size):
                return core.read_message(reader)
        finally:
            reader.close()
            writer.close()

    def test_valid_json_object_is_accepted(self) -> None:
        self.assertEqual({"action": "status"}, self.read_payload(b'{"action":"status"}'))

    def test_empty_malformed_utf8_and_non_object_are_rejected(self) -> None:
        cases = [
            (b"", "Пустой запрос"),
            (b"{", "Некорректный JSON-запрос"),
            (b"\xff", "Некорректный JSON-запрос"),
            (b"[]", "Некорректный формат запроса"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(core.RelayError) as captured:
                    self.read_payload(payload)
                self.assertIn(expected, str(captured.exception))

    def test_oversized_message_is_rejected_before_json_parsing(self) -> None:
        with self.assertRaises(core.RelayError) as captured:
            self.read_payload(b"123456789", max_size=8)
        self.assertIn("Слишком большой локальный запрос", str(captured.exception))


class _OutputChannel:
    def __init__(self, stdout: bytes, stderr: bytes, exit_code: int = 0) -> None:
        self.stdout = bytearray(stdout)
        self.stderr = bytearray(stderr)
        self.exit_code = exit_code
        self.closed = False

    def exec_command(self, command: str) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        pass

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv(self, size: int) -> bytes:
        chunk = bytes(self.stdout[:size])
        del self.stdout[:size]
        return chunk

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, size: int) -> bytes:
        chunk = bytes(self.stderr[:size])
        del self.stderr[:size]
        return chunk

    def exit_status_ready(self) -> bool:
        return True

    def recv_exit_status(self) -> int:
        return self.exit_code

    def close(self) -> None:
        self.closed = True


class _OutputTransport:
    def __init__(self, channel: _OutputChannel) -> None:
        self.channel = channel

    def open_session(self, timeout: int = 10) -> _OutputChannel:
        return self.channel


class _OutputClient:
    def __init__(self, channel: _OutputChannel) -> None:
        self.transport = _OutputTransport(channel)

    def get_transport(self) -> _OutputTransport:
        return self.transport


class OutputLimitTests(unittest.TestCase):
    def test_combined_stdout_stderr_at_limit_succeeds(self) -> None:
        channel = _OutputChannel(b"1234", b"5678", exit_code=7)
        with patch.object(core, "MAX_OUTPUT_SIZE", 8):
            result = core.execute_remote_command(_OutputClient(channel), "test", 1)
        self.assertEqual("1234", result["stdout"])
        self.assertEqual("5678", result["stderr"])
        self.assertEqual(7, result["exit_code"])
        self.assertTrue(channel.closed)

    def test_combined_stdout_stderr_over_limit_is_rejected_and_channel_closed(self) -> None:
        channel = _OutputChannel(b"12345", b"6789")
        with patch.object(core, "MAX_OUTPUT_SIZE", 8):
            with self.assertRaises(core.RelayError) as captured:
                core.execute_remote_command(_OutputClient(channel), "test", 1)
        self.assertIn("превышает допустимый размер", str(captured.exception))
        self.assertTrue(channel.closed)


class DaemonHardeningIntegrationTests(unittest.TestCase):
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
        self.process: subprocess.Popen[str] | None = subprocess.Popen(
            [sys.executable, "-u", str(DAEMON_RUNNER)],
            cwd=str(ROOT),
            env=child_env,
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
            f"Hardening-daemon завершился раньше времени, код {self.process.returncode}.\n"
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
            raise AssertionError(f"Session-файл hardening-daemon не стал доступен: {last_error}") from last_error
        raise AssertionError("Session-файл hardening-daemon не появился.")

    def wait_connected(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            self.fail_if_exited()
            last = core.request_daemon(self.session, "status", response_timeout=1)
            if last.get("ok") and last.get("ssh_status") == "connected":
                return last
            time.sleep(0.03)
        self.fail(f"Hardening-daemon не перешёл в connected: {last}")

    def request_exec(self, command: str) -> dict:
        return core.request_daemon(
            self.session,
            "exec",
            command=command,
            risky=False,
            receipt_path=core.DEFAULT_RISKY_RECEIPT_PATH,
            response_timeout=4,
        )

    def raw_exchange(self, payload: bytes) -> dict:
        port = int(self.session["daemon_port"])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return json.loads(b"".join(chunks).decode("utf-8"))

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

    def test_malformed_requests_return_structured_error_and_daemon_survives(self) -> None:
        cases = [
            (b"", "Пустой запрос"),
            (b"{", "Некорректный JSON-запрос"),
            (b"[]", "Некорректный формат запроса"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                response = self.raw_exchange(payload)
                self.assertFalse(response.get("ok"))
                self.assertIn(expected, str(response.get("protocol_error")))

        wrong_type = json.dumps(
            {"auth_token": self.session["auth_token"], "action": "exec", "command": 123}
        ).encode("utf-8")
        response = self.raw_exchange(wrong_type)
        self.assertFalse(response.get("ok"))
        self.assertIn("Поле command должно быть строкой", str(response.get("protocol_error")))

        status = core.request_daemon(self.session, "status", response_timeout=1)
        self.assertTrue(status.get("ok"))
        self.assertEqual("connected", status.get("ssh_status"))

    def test_client_can_close_without_reading_response_and_daemon_survives(self) -> None:
        port = int(self.session["daemon_port"])
        request = json.dumps(
            {"auth_token": self.session["auth_token"], "action": "status"}
        ).encode("utf-8")
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        client.sendall(request)
        client.close()
        time.sleep(0.1)

        status = core.request_daemon(self.session, "status", response_timeout=1)
        self.assertTrue(status.get("ok"))
        self.assertEqual("connected", status.get("ssh_status"))

    def test_remote_operations_are_strictly_serialized(self) -> None:
        results: dict[str, dict] = {}

        first = threading.Thread(
            target=lambda: results.__setitem__("first", self.request_exec("test:serialized-first")),
            daemon=True,
        )
        first.start()
        self.wait_event("start:first")

        second = threading.Thread(
            target=lambda: results.__setitem__("second", self.request_exec("test:serialized-second")),
            daemon=True,
        )
        second.start()
        first.join(timeout=3)
        second.join(timeout=3)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(results["first"].get("ok"))
        self.assertTrue(results["second"].get("ok"))
        self.assertEqual(
            ["start:first", "end:first", "start:second", "end:second"],
            self.event_lines(),
        )

    def test_stop_removes_registration_closes_listener_and_exits_cleanly(self) -> None:
        assert self.process is not None
        process = self.process
        port = int(self.session["daemon_port"])
        session_path = core.session_file_path("ci-core")

        response = core.request_daemon(self.session, "stop", response_timeout=2)
        self.assertTrue(response.get("ok"))
        stdout, stderr = process.communicate(timeout=4)
        self.assertEqual(0, process.returncode, stdout + stderr)
        self.process = None

        deadline = time.monotonic() + 2.0
        while session_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(session_path.exists())

        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)


if __name__ == "__main__":
    unittest.main()
