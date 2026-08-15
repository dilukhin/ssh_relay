#!/usr/bin/env python3
"""Регрессии структурированных исходов daemon transport и удалённых команд."""

from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

import ssh_relay  # noqa: F401 — устанавливает расширения поверх core.
import ssh_relay_core as core


class _Socket:
    def __init__(self, *, recv_chunks=None, send_error: Exception | None = None) -> None:
        self.recv_chunks = list(recv_chunks or [])
        self.send_error = send_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def sendall(self, _data: bytes) -> None:
        if self.send_error is not None:
            raise self.send_error

    def shutdown(self, _how: int) -> None:
        pass

    def settimeout(self, _timeout) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        if not self.recv_chunks:
            return b""
        item = self.recv_chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _Channel:
    def __init__(
        self,
        *,
        exec_error: Exception | None = None,
        stdin_error: Exception | None = None,
        stdout_chunks=None,
        stderr_chunks=None,
        exit_code: int = 0,
        ready: bool = True,
    ) -> None:
        self.exec_error = exec_error
        self.stdin_error = stdin_error
        self.stdout_chunks = list(stdout_chunks or [])
        self.stderr_chunks = list(stderr_chunks or [])
        self.exit_code = exit_code
        self.ready = ready
        self.closed = False

    def exec_command(self, _command: str) -> None:
        if self.exec_error is not None:
            raise self.exec_error

    def sendall(self, _data: bytes) -> None:
        if self.stdin_error is not None:
            raise self.stdin_error

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return bool(self.stdout_chunks)

    def recv(self, _size: int) -> bytes:
        return self.stdout_chunks.pop(0)

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr_chunks)

    def recv_stderr(self, _size: int) -> bytes:
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self) -> bool:
        return self.ready

    def recv_exit_status(self) -> int:
        return self.exit_code

    def close(self) -> None:
        self.closed = True


class _Transport:
    def __init__(self, *, channel: _Channel | None = None, open_error: Exception | None = None) -> None:
        self.channel = channel
        self.open_error = open_error

    def open_session(self, timeout=10):
        if self.open_error is not None:
            raise self.open_error
        return self.channel


class _Client:
    def __init__(self, transport) -> None:
        self.transport = transport

    def get_transport(self):
        return self.transport


class DaemonRequestOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = {"auth_token": "token", "daemon_port": 41234}

    def test_connection_failure_is_not_started(self) -> None:
        with patch.object(core.socket, "create_connection", side_effect=ConnectionRefusedError("refused")):
            with self.assertRaises(core.DaemonRequestError) as captured:
                core.request_daemon(self.session, "exec", command="true")
        self.assertFalse(captured.exception.request_sent)
        self.assertEqual("daemon_unavailable", captured.exception.error_code)
        self.assertIsInstance(captured.exception.__cause__, ConnectionRefusedError)

    def test_send_failure_after_connect_is_unknown(self) -> None:
        sock = _Socket(send_error=OSError("send failed"))
        with patch.object(core.socket, "create_connection", return_value=sock):
            with self.assertRaises(core.DaemonRequestError) as captured:
                core.request_daemon(self.session, "exec", command="true")
        self.assertTrue(captured.exception.request_sent)
        self.assertEqual("daemon_response_lost", captured.exception.error_code)

    def test_response_timeout_after_send_is_unknown(self) -> None:
        sock = _Socket(recv_chunks=[socket.timeout("timed out")])
        with patch.object(core.socket, "create_connection", return_value=sock):
            with self.assertRaises(core.DaemonRequestError) as captured:
                core.request_daemon(self.session, "exec", command="true")
        self.assertTrue(captured.exception.request_sent)
        self.assertEqual("daemon_response_lost", captured.exception.error_code)

    def test_invalid_response_after_send_is_unknown(self) -> None:
        sock = _Socket(recv_chunks=[b"not-json", b""])
        with patch.object(core.socket, "create_connection", return_value=sock):
            with self.assertRaises(core.DaemonRequestError) as captured:
                core.request_daemon(self.session, "exec", command="true")
        self.assertTrue(captured.exception.request_sent)
        self.assertEqual("response_invalid", captured.exception.error_code)


class RemoteCommandOutcomeTests(unittest.TestCase):
    def test_open_session_failure_is_not_started(self) -> None:
        client = _Client(_Transport(open_error=OSError("open failed")))
        with self.assertRaises(core.RemoteCommandError) as captured:
            core.execute_remote_command(client, "true", 1)
        self.assertFalse(captured.exception.command_started)
        self.assertEqual("command_not_started", captured.exception.error_code)

    def test_exec_request_failure_is_unknown_and_closes_channel(self) -> None:
        channel = _Channel(exec_error=OSError("exec failed"))
        client = _Client(_Transport(channel=channel))
        with self.assertRaises(core.RemoteCommandError) as captured:
            core.execute_remote_command(client, "true", 1)
        self.assertTrue(captured.exception.command_started)
        self.assertEqual("command_result_unknown", captured.exception.error_code)
        self.assertTrue(channel.closed)

    def test_timeout_is_unknown_and_closes_channel(self) -> None:
        channel = _Channel(ready=False)
        client = _Client(_Transport(channel=channel))
        with patch.object(core.time, "monotonic", side_effect=[100.0, 102.0]), patch.object(
            core.time, "sleep"
        ):
            with self.assertRaises(core.RemoteCommandError) as captured:
                core.execute_remote_command(client, "sleep 10", 1)
        self.assertTrue(captured.exception.command_started)
        self.assertEqual("command_result_unknown", captured.exception.error_code)
        self.assertIn("Превышено время выполнения команды", str(captured.exception))
        self.assertTrue(channel.closed)

    def test_output_limit_preserves_partial_output(self) -> None:
        channel = _Channel(stdout_chunks=[b"abcd"], ready=False)
        client = _Client(_Transport(channel=channel))
        with patch.object(core, "MAX_OUTPUT_SIZE", 2):
            with self.assertRaises(core.RemoteCommandError) as captured:
                core.execute_remote_command(client, "printf abcd", 1)
        self.assertTrue(captured.exception.command_started)
        self.assertEqual("command_result_unknown", captured.exception.error_code)
        self.assertEqual("abcd", captured.exception.stdout)
        self.assertIn("превышает допустимый размер", str(captured.exception))
        self.assertTrue(channel.closed)

    def test_sudo_stdin_secret_is_not_retained_in_exception_chain(self) -> None:
        secret = "TEST_SUDO_MACHINE_OUTCOME_SECRET_4c7c"
        channel = _Channel(stdin_error=OSError(f"send failed with stdin={secret}"))
        client = _Client(_Transport(channel=channel))
        with self.assertRaises(core.RemoteCommandError) as captured:
            core.execute_remote_command(
                client,
                "sudo -S -p '' -- sh -c true",
                1,
                stdin_data=(secret + "\n").encode("utf-8"),
            )
        exc = captured.exception
        self.assertTrue(exc.command_started)
        self.assertEqual("command_result_unknown", exc.error_code)
        self.assertNotIn(secret, str(exc))
        self.assertNotIn(secret, exc.stdout)
        self.assertNotIn(secret, exc.stderr)
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)
        self.assertTrue(channel.closed)

    def test_success_contract_is_unchanged(self) -> None:
        channel = _Channel(stdout_chunks=[b"out\n"], stderr_chunks=[b"err\n"], exit_code=7)
        client = _Client(_Transport(channel=channel))
        result = core.execute_remote_command(client, "test", 1)
        self.assertTrue(result["ok"])
        self.assertEqual("out\n", result["stdout"])
        self.assertEqual("err\n", result["stderr"])
        self.assertEqual(7, result["exit_code"])
        self.assertTrue(channel.closed)


if __name__ == "__main__":
    unittest.main()
