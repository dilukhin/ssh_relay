#!/usr/bin/env python3
"""Контракт staging machine-mode для exec и sudo-exec."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import ssh_relay  # noqa: F401 — устанавливает расширения поверх core.
import ssh_relay_core as core


SESSION = {
    "name": "ci-machine",
    "version": "0.8.2",
    "host": "198.51.100.42",
    "port": 22,
    "user": "donpedro",
    "daemon_port": 41234,
    "auth_token": "test-token",
    "pid": 123,
    "command_timeout": 120,
    "reconnect_wait": 30,
}


class _InboundSocket:
    def __init__(self, payload: dict) -> None:
        self._chunks = [json.dumps(payload, ensure_ascii=False).encode("utf-8"), b""]

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0)


class _OutboundSocket:
    def __init__(self) -> None:
        self.data = b""

    def sendall(self, data: bytes) -> None:
        self.data += data


class _Channel:
    def __init__(self, *, exec_error: Exception | None = None) -> None:
        self.exec_error = exec_error
        self.closed = False

    def exec_command(self, _command: str) -> None:
        if self.exec_error is not None:
            raise self.exec_error

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return False

    def recv_stderr_ready(self) -> bool:
        return False

    def exit_status_ready(self) -> bool:
        return True

    def recv_exit_status(self) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


class _Transport:
    def __init__(self, channel: _Channel) -> None:
        self.channel = channel

    def open_session(self, timeout=10):
        return self.channel


class _Client:
    def __init__(self, channel: _Channel) -> None:
        self.transport = _Transport(channel)

    def get_transport(self):
        return self.transport


class MachineCliContractTests(unittest.TestCase):
    def run_machine(self, argv: list[str], daemon_result=None, daemon_error: Exception | None = None):
        parser = ssh_relay.build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        stderr = io.StringIO()

        def request(*_args, **_kwargs):
            if daemon_error is not None:
                raise daemon_error
            return daemon_result

        with patch.object(core, "read_session", return_value=dict(SESSION)), patch.object(
            core, "request_daemon", side_effect=request
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = int(args.handler(args))
        payload = json.loads(stdout.getvalue())
        return code, payload, stderr.getvalue()

    def test_exec_json_success_is_single_object_and_does_not_echo_command(self) -> None:
        secret_command = "printf MACHINE_COMMAND_SECRET_8d7d"
        code, payload, stderr = self.run_machine(
            ["exec", "--json", "--name", "ci-machine", secret_command],
            daemon_result={"ok": True, "stdout": "ok\n", "stderr": "", "exit_code": 0},
        )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual("exec", payload["action"])
        self.assertEqual("succeeded", payload["operation_status"])
        self.assertEqual("succeeded", payload["command_status"])
        self.assertEqual(0, payload["command_exit_code"])
        self.assertEqual("ok\n", payload["stdout"])
        self.assertEqual("not_requested", payload["receipt_status"])
        self.assertFalse(payload["partial_success"])
        self.assertNotIn(secret_command, json.dumps(payload, ensure_ascii=False))

    def test_sudo_json_nonzero_preserves_remote_exit_code(self) -> None:
        code, payload, stderr = self.run_machine(
            ["sudo-exec", "--json", "--name", "ci-machine", "false"],
            daemon_result={"ok": True, "stdout": "", "stderr": "failed\n", "exit_code": 7},
        )
        self.assertEqual(11, code)
        self.assertEqual("", stderr)
        self.assertTrue(payload["sudo"])
        self.assertEqual("command_failed", payload["operation_status"])
        self.assertEqual("failed", payload["command_status"])
        self.assertEqual(7, payload["command_exit_code"])
        self.assertEqual("failed\n", payload["stderr"])
        self.assertEqual("remote_exit_nonzero", payload["error_code"])

    def test_local_connection_refused_is_not_started(self) -> None:
        error = core.DaemonRequestError(
            "Daemon недоступен.",
            request_sent=False,
            error_code="daemon_unavailable",
        )
        code, payload, stderr = self.run_machine(
            ["exec", "--json", "--name", "ci-machine", "true"],
            daemon_error=error,
        )
        self.assertEqual(10, code)
        self.assertEqual("", stderr)
        self.assertEqual("not_started", payload["operation_status"])
        self.assertEqual("not_started", payload["command_status"])
        self.assertEqual("daemon_unavailable", payload["error_code"])

    def test_lost_response_after_possible_delivery_is_unknown(self) -> None:
        error = core.DaemonRequestError(
            "Daemon не ответил.",
            request_sent=True,
            error_code="daemon_response_lost",
        )
        code, payload, stderr = self.run_machine(
            ["exec", "--json", "--name", "ci-machine", "true"],
            daemon_error=error,
        )
        self.assertEqual(13, code)
        self.assertEqual("", stderr)
        self.assertEqual("unknown", payload["operation_status"])
        self.assertEqual("unknown", payload["command_status"])

    def test_daemon_not_started_and_unknown_are_distinct(self) -> None:
        not_started = {"ok": False, "protocol_error": "not started", "command_started": False, "error_code": "command_not_started"}
        code, payload, _ = self.run_machine(
            ["exec", "--json", "--name", "ci-machine", "true"], daemon_result=not_started
        )
        self.assertEqual(10, code)
        self.assertEqual("not_started", payload["command_status"])

        unknown = {"ok": False, "protocol_error": "unknown", "command_started": True, "error_code": "command_result_unknown"}
        code, payload, _ = self.run_machine(
            ["exec", "--json", "--name", "ci-machine", "true"], daemon_result=unknown
        )
        self.assertEqual(13, code)
        self.assertEqual("unknown", payload["command_status"])

    def test_old_unstructured_daemon_error_is_conservatively_unknown(self) -> None:
        code, payload, _ = self.run_machine(
            ["exec", "--json", "--name", "ci-machine", "true"],
            daemon_result={"ok": False, "protocol_error": "old daemon error"},
        )
        self.assertEqual(13, code)
        self.assertEqual("unknown", payload["operation_status"])
        self.assertEqual("command_result_unknown", payload["error_code"])

    def test_risky_json_is_routed_to_final_p0_contract(self) -> None:
        parser = ssh_relay.build_parser()
        args = parser.parse_args(["exec", "--json", "--risky", "--name", "ci-machine", "true"])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(core, "read_session", side_effect=core.RelayError("session missing")), patch.object(
            core, "request_daemon"
        ) as request, redirect_stdout(stdout), redirect_stderr(stderr):
            code = int(args.handler(args))
        payload = json.loads(stdout.getvalue())
        request.assert_not_called()
        self.assertEqual(10, code)
        self.assertEqual("", stderr.getvalue())
        self.assertTrue(payload["risky"])
        self.assertEqual("not_attempted", payload["receipt_status"])
        self.assertEqual("session_unavailable", payload["error_code"])
        self.assertNotEqual("risky_machine_contract_not_ready", payload["error_code"])

    def test_text_mode_still_defaults_json_false(self) -> None:
        args = ssh_relay.build_parser().parse_args(["exec", "true"])
        self.assertFalse(args.json)


class DaemonMachineReplyTests(unittest.TestCase):
    def begin_machine_request(self, action: str = "exec") -> None:
        inbound = _InboundSocket({"auth_token": "x", "action": action, "machine": True, "command": "true"})
        message = core.read_message(inbound)
        self.assertTrue(message["machine"])

    def test_preflight_failure_is_enriched_as_not_started(self) -> None:
        self.begin_machine_request()
        outbound = _OutboundSocket()
        core.send_message(outbound, {"ok": False, "protocol_error": "preflight"})
        payload = json.loads(outbound.data.decode("utf-8"))
        self.assertFalse(payload["command_started"])
        self.assertEqual("command_not_started", payload["error_code"])
        self.assertEqual("", payload["stdout"])
        self.assertEqual("", payload["stderr"])

    def test_exec_failure_is_enriched_as_unknown(self) -> None:
        self.begin_machine_request()
        channel = _Channel(exec_error=OSError("exec failed"))
        with self.assertRaises(core.RemoteCommandError):
            core.execute_remote_command(_Client(channel), "true", 1)
        outbound = _OutboundSocket()
        core.send_message(outbound, {"ok": False, "protocol_error": "unknown"})
        payload = json.loads(outbound.data.decode("utf-8"))
        self.assertTrue(payload["command_started"])
        self.assertEqual("command_result_unknown", payload["error_code"])
        self.assertTrue(channel.closed)

    def test_text_reply_is_not_enriched(self) -> None:
        inbound = _InboundSocket({"auth_token": "x", "action": "exec", "command": "true"})
        core.read_message(inbound)
        outbound = _OutboundSocket()
        core.send_message(outbound, {"ok": False, "protocol_error": "plain"})
        payload = json.loads(outbound.data.decode("utf-8"))
        self.assertNotIn("command_started", payload)
        self.assertNotIn("error_code", payload)


if __name__ == "__main__":
    unittest.main()
