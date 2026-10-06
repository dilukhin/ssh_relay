#!/usr/bin/env python3
"""P0.3: единый machine contract для risky exec/sudo-exec и receipt outcomes."""

from __future__ import annotations

import io
import base64
import hashlib
import json
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import ssh_relay  # noqa: F401 — устанавливает P0 layers поверх core.
import ssh_relay_core as core
import ssh_relay_receipts as receipts


SESSION = {
    "name": "ci-p0",
    "version": "0.8.2",
    "host": "198.51.100.42",
    "port": 22,
    "user": "donpedro",
    "daemon_port": 41234,
    "auth_token": "test-session-token",
    "pid": 123,
    "command_timeout": 120,
    "reconnect_wait": 30,
}
FINGERPRINT = "SHA256:" + "A" * 43


class _InboundSocket:
    def __init__(self, payload: dict) -> None:
        self._chunks = [json.dumps(payload, ensure_ascii=False).encode("utf-8"), b""]

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0)


class UnifiedRiskyMachineTests(unittest.TestCase):
    def run_machine(self, argv: list[str], command_result=None, command_error: Exception | None = None, session=None):
        current_session = dict(session or SESSION)
        parser = ssh_relay.build_parser()
        args = parser.parse_args(argv)
        calls: list[tuple[str, dict]] = []
        stdout = io.StringIO()
        stderr = io.StringIO()

        def request(_session, action, **kwargs):
            calls.append((action, dict(kwargs)))
            if action == "status":
                return {"ok": True, "status": "active", "ssh_status": "connected", "receipt_schema_version": 1,
                        "version": core.__version__, "risky_identity_schema_version": 1}
            if command_error is not None:
                raise command_error
            response = command_result(kwargs) if callable(command_result) else command_result
            if not isinstance(response, dict):
                return response
            response = dict(response)
            command = response if response.get("ok") else response.get("command_result")
            if isinstance(command, dict) and command.get("ok"):
                for key, value in (("remote_host", _session["host"]), ("remote_port", _session["port"]),
                                   ("remote_user", _session["user"]), ("remote_host_key_sha256", FINGERPRINT)):
                    command.setdefault(key, value)
                receipt = command.get("risky_receipt") if response.get("ok") else response.get("receipt_result")
                if isinstance(receipt, dict):
                    receipt.setdefault("remote_host_key_sha256", FINGERPRINT)
            return response

        with patch.object(core, "read_session", return_value=current_session), patch.object(
            core, "request_daemon", side_effect=request
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = int(args.handler(args))
        return code, json.loads(stdout.getvalue()), stderr.getvalue(), calls

    def test_risky_exec_success_requires_confirmed_receipt(self) -> None:
        def result(kwargs):
            return {
                "ok": True,
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "risky_receipt": {
                    "receipt_status": "succeeded",
                    "transaction_id": "tx-success",
                    "receipt_id": kwargs["receipt_id"],
                    "receipt_hash": "a" * 64,
                    "receipt_path": "~/.local/state/agent-safe/changes.jsonl",
                    "exit_code": 0,
                    "error_code": None,
                    "error_message": None,
                },
            }

        code, payload, stderr, calls = self.run_machine(
            [
                "exec", "--json", "--risky", "--name", "ci-p0",
                "--transaction-id", "tx-success", "--change-target", "/etc/app.conf",
                "--change-description", "обновлена конфигурация", "true",
            ],
            command_result=result,
        )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual("succeeded", payload["operation_status"])
        self.assertEqual("succeeded", payload["command_status"])
        self.assertEqual("succeeded", payload["receipt_status"])
        self.assertFalse(payload["partial_success"])
        self.assertEqual("tx-success", payload["transaction_id"])
        uuid.UUID(payload["receipt_id"])
        self.assertEqual("a" * 64, payload["receipt_hash"])
        self.assertEqual("/etc/app.conf", payload["change_target"])
        self.assertEqual(["status", "exec"], [action for action, _ in calls])
        self.assertTrue(calls[1][1]["risky"])
        self.assertTrue(calls[1][1]["machine"])
        self.assertEqual(payload["receipt_id"], calls[1][1]["receipt_id"])

    def test_receipt_failed_after_command_success_is_partial_success(self) -> None:
        def result(kwargs):
            return {
                "ok": False,
                "protocol_error": "receipt failed",
                "command_result": {"ok": True, "stdout": "changed\n", "stderr": "", "exit_code": 0},
                "receipt_result": {
                    "receipt_status": "failed",
                    "transaction_id": "tx-dup",
                    "receipt_id": kwargs["receipt_id"],
                    "receipt_hash": "b" * 64,
                    "receipt_path": "~/changes.jsonl",
                    "exit_code": 76,
                    "error_code": "duplicate_transaction_id",
                    "error_message": "duplicate",
                },
            }

        code, payload, stderr, _ = self.run_machine(
            ["exec", "--json", "--risky", "--transaction-id", "tx-dup", "--receipt-path", "~/changes.jsonl", "true"],
            command_result=result,
        )
        self.assertEqual(12, code)
        self.assertEqual("", stderr)
        self.assertEqual("partial_success", payload["operation_status"])
        self.assertEqual("succeeded", payload["command_status"])
        self.assertEqual(0, payload["command_exit_code"])
        self.assertEqual("failed", payload["receipt_status"])
        self.assertTrue(payload["partial_success"])
        self.assertEqual("duplicate_transaction_id", payload["error_code"])
        self.assertEqual("receipt", payload["error_stage"])

    def test_receipt_unknown_after_command_success_is_partial_success(self) -> None:
        def result(kwargs):
            return {
                "ok": False,
                "protocol_error": "receipt unknown",
                "command_result": {"ok": True, "stdout": "", "stderr": "", "exit_code": 0},
                "receipt_result": {
                    "receipt_status": "unknown",
                    "transaction_id": "tx-unknown-receipt",
                    "receipt_id": kwargs["receipt_id"],
                    "receipt_hash": "c" * 64,
                    "receipt_path": "~/changes.jsonl",
                    "exit_code": 1,
                    "error_code": "receipt_write_unknown",
                    "error_message": "unknown",
                },
            }

        code, payload, _, _ = self.run_machine(
            ["exec", "--json", "--risky", "--transaction-id", "tx-unknown-receipt", "true"],
            command_result=result,
        )
        self.assertEqual(12, code)
        self.assertEqual("partial_success", payload["operation_status"])
        self.assertEqual("unknown", payload["receipt_status"])
        self.assertTrue(payload["partial_success"])
        self.assertEqual("receipt_write_unknown", payload["error_code"])
        uuid.UUID(payload["receipt_id"])

    def test_command_failure_does_not_report_receipt_attempt(self) -> None:
        code, payload, _, calls = self.run_machine(
            ["exec", "--json", "--risky", "--transaction-id", "tx-failed", "false"],
            command_result={"ok": True, "stdout": "", "stderr": "failed\n", "exit_code": 7},
        )
        self.assertEqual(11, code)
        self.assertEqual("command_failed", payload["operation_status"])
        self.assertEqual("failed", payload["command_status"])
        self.assertEqual(7, payload["command_exit_code"])
        self.assertEqual("not_attempted", payload["receipt_status"])
        self.assertFalse(payload["partial_success"])
        self.assertEqual(["status", "exec"], [action for action, _ in calls])

    def test_lost_command_response_keeps_precreated_receipt_id_and_unknown_outcome(self) -> None:
        error = core.DaemonRequestError(
            "response lost",
            request_sent=True,
            error_code="daemon_response_lost",
        )
        code, payload, _, calls = self.run_machine(
            ["exec", "--json", "--risky", "--transaction-id", "tx-lost", "true"],
            command_error=error,
        )
        self.assertEqual(13, code)
        self.assertEqual("unknown", payload["operation_status"])
        self.assertEqual("unknown", payload["command_status"])
        self.assertEqual("unknown", payload["receipt_status"])
        self.assertFalse(payload["partial_success"])
        uuid.UUID(payload["receipt_id"])
        self.assertEqual(payload["receipt_id"], calls[1][1]["receipt_id"])

    def test_missing_capability_is_not_started_and_command_is_not_sent(self) -> None:
        parser = ssh_relay.build_parser()
        args = parser.parse_args(["exec", "--json", "--risky", "true"])
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls: list[str] = []

        def request(_session, action, **_kwargs):
            calls.append(action)
            return {"ok": True, "status": "active"}

        with patch.object(core, "read_session", return_value=dict(SESSION)), patch.object(
            core, "request_daemon", side_effect=request
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = int(args.handler(args))
        payload = json.loads(stdout.getvalue())
        self.assertEqual(10, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(["status"], calls)
        self.assertEqual("not_started", payload["operation_status"])
        self.assertEqual("not_attempted", payload["receipt_status"])
        self.assertEqual("receipt_capability_missing", payload["error_code"])

    def test_sudo_risky_success_uses_same_contract(self) -> None:
        def result(kwargs):
            return {
                "ok": True,
                "stdout": "root\n",
                "stderr": "",
                "exit_code": 0,
                "risky_receipt": {
                    "receipt_status": "succeeded",
                    "transaction_id": "tx-sudo",
                    "receipt_id": kwargs["receipt_id"],
                    "receipt_hash": "d" * 64,
                    "receipt_path": "~/changes.jsonl",
                },
            }
        code, payload, _, calls = self.run_machine(
            ["sudo-exec", "--json", "--risky", "--transaction-id", "tx-sudo", "whoami"],
            command_result=result,
        )
        self.assertEqual(0, code)
        self.assertTrue(payload["sudo"])
        self.assertEqual("succeeded", payload["receipt_status"])
        self.assertEqual(["status", "sudo_exec"], [action for action, _ in calls])

    def test_wrong_verified_key_or_receipt_identity_never_reports_success(self) -> None:
        for altered in ("remote_host_key_sha256", "receipt_id", "transaction_id"):
            with self.subTest(altered=altered):
                def response(kwargs):
                    command = {"ok": True, "stdout": "", "stderr": "", "exit_code": 0,
                               "risky_receipt": {"receipt_status": "succeeded", "transaction_id": "tx-identity",
                                                 "receipt_id": kwargs["receipt_id"],
                                                 "remote_host_key_sha256": FINGERPRINT}}
                    if altered == "remote_host_key_sha256":
                        command[altered] = "SHA256:" + "B" * 43
                    else:
                        command["risky_receipt"][altered] = "wrong"
                    return command
                code, payload, _, _ = self.run_machine(
                    ["exec", "--json", "--risky", "--transaction-id", "tx-identity", "true"],
                    command_result=response,
                )
                self.assertEqual(13, code)
                self.assertEqual("unknown", payload["operation_status"])
                self.assertEqual("risky_identity_unconfirmed", payload["error_code"])

    def test_old_daemon_is_rejected_before_command(self) -> None:
        args = ssh_relay.build_parser().parse_args(["exec", "--json", "--risky", "true"])
        output = io.StringIO()
        calls = []
        def request(_session, action, **_kwargs):
            calls.append(action)
            return {"ok": True, "receipt_schema_version": 1, "version": "0.10.1"}
        with patch.object(core, "read_session", return_value=dict(SESSION)), patch.object(
            core, "request_daemon", side_effect=request
        ), redirect_stdout(output):
            code = args.handler(args)
        self.assertEqual(10, code)
        self.assertEqual(["status"], calls)
        self.assertEqual("risky_daemon_incompatible", json.loads(output.getvalue())["error_code"])

    def test_named_sessions_keep_identity_and_transactions_separate(self) -> None:
        def make_result(tx):
            def result(kwargs):
                return {
                    "ok": True,
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "risky_receipt": {
                        "receipt_status": "succeeded",
                        "transaction_id": tx,
                        "receipt_id": kwargs["receipt_id"],
                        "receipt_hash": "e" * 64,
                        "receipt_path": "~/changes.jsonl",
                    },
                }
            return result

        one = dict(SESSION, name="one", host="198.51.100.42", user="donpedro")
        two = dict(SESSION, name="two", host="198.51.100.43", user="otheruser")
        _, first, _, _ = self.run_machine(
            ["exec", "--json", "--risky", "--name", "one", "--transaction-id", "tx-one", "true"],
            command_result=make_result("tx-one"), session=one,
        )
        _, second, _, _ = self.run_machine(
            ["exec", "--json", "--risky", "--name", "two", "--transaction-id", "tx-two", "true"],
            command_result=make_result("tx-two"), session=two,
        )
        self.assertEqual(("one", "198.51.100.42", "donpedro", "tx-one"),
                         (first["session"], first["remote_host"], first["remote_user"], first["transaction_id"]))
        self.assertEqual(("two", "198.51.100.43", "otheruser", "tx-two"),
                         (second["session"], second["remote_host"], second["remote_user"], second["transaction_id"]))
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])

    def test_command_secret_is_not_echoed_in_machine_metadata(self) -> None:
        secret = "P0_COMMAND_SECRET_7f84"
        def result(kwargs):
            return {
                "ok": True,
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "risky_receipt": {
                    "receipt_status": "succeeded",
                    "transaction_id": "tx-secret",
                    "receipt_id": kwargs["receipt_id"],
                    "receipt_hash": "f" * 64,
                    "receipt_path": "~/changes.jsonl",
                },
            }
        _, payload, _, _ = self.run_machine(
            ["exec", "--json", "--risky", "--transaction-id", "tx-secret", f"printf {secret} >/dev/null"],
            command_result=result,
        )
        self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("test-session-token", json.dumps(payload, ensure_ascii=False))


class ReceiptIdPropagationTests(unittest.TestCase):
    def test_safe_request_requires_and_reuses_precreated_receipt_id(self) -> None:
        receipt_id = str(uuid.uuid4())
        inbound = _InboundSocket(
            {
                "auth_token": "x",
                "action": "exec",
                "machine": True,
                "command": "true",
                "risky": False,
                "receipt_schema_version": 1,
                "transaction_id": "tx-daemon",
                "receipt_id": receipt_id,
                "receipt_path": "~/changes.jsonl",
                "change_target": None,
                "change_description": None,
            }
        )
        message = core.read_message(inbound)
        self.assertTrue(message["risky"])
        with patch.object(core, "execute_remote_command", return_value={"ok": True, "stdout": "", "stderr": "", "exit_code": 0}):
            result = core.execute_risky_receipt(
                object(),
                session=dict(SESSION),
                action="exec",
                command="true",
                sudo=False,
                receipt_path="~/changes.jsonl",
                timeout_seconds=5,
                sudo_password=None,
            )
        self.assertEqual(receipt_id, result["receipt_id"])
        self.assertEqual("tx-daemon", result["transaction_id"])

    def test_safe_request_without_receipt_id_is_rejected_before_command(self) -> None:
        inbound = _InboundSocket(
            {
                "auth_token": "x",
                "action": "exec",
                "machine": True,
                "command": "true",
                "risky": False,
                "receipt_schema_version": 1,
                "transaction_id": "tx-no-id",
                "receipt_path": "~/changes.jsonl",
            }
        )
        with self.assertRaisesRegex(core.RelayError, "receipt_id"):
            core.read_message(inbound)


class VerifiedHostKeyTests(unittest.TestCase):
    def test_fingerprint_comes_from_active_transport_key(self) -> None:
        class Transport:
            def is_active(self): return True
            def is_authenticated(self): return True
            def get_remote_server_key(self): return self
            def asbytes(self): return b"verified-test-host-key"
        class Client:
            def get_transport(self): return Transport()
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(b"verified-test-host-key").digest()).decode("ascii").rstrip("=")
        self.assertEqual(expected, core.verified_host_key_fingerprint(Client()))

    def test_inactive_transport_cannot_supply_risky_identity(self) -> None:
        class Client:
            def get_transport(self): return None
        with self.assertRaises(core.RelayError):
            core.verified_host_key_fingerprint(Client())


if __name__ == "__main__":
    unittest.main(verbosity=2)
