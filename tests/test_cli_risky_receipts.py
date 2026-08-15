#!/usr/bin/env python3
"""Cross-platform тесты safe risky receipt v1 и его CLI-ограничений."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ssh_relay
import ssh_relay_core as core
import ssh_relay_receipts as receipts


class RiskyReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = {
            "name": "ci",
            "version": ssh_relay.__version__,
            "host": "198.51.100.42",
            "port": 22,
            "user": "donpedro",
            "daemon_port": 41000,
            "auth_token": "test-session-token",
            "command_timeout": 12,
            "reconnect_wait": 5,
        }

    def test_payload_hashes_are_canonical_and_do_not_store_command_or_secrets(self) -> None:
        command = "printf 'test-secret'"
        payload = receipts.build_receipt_payload(
            core,
            session=self.session,
            action="exec",
            command=command,
            sudo=False,
            transaction_id="tx-123",
            receipt_id="receipt-123",
            change_target="/etc/app.conf",
            change_description="обновлена конфигурация",
            timestamp_utc="2026-08-15T00:00:00Z",
        )

        self.assertEqual(hashlib.sha256(command.encode("utf-8")).hexdigest(), payload["command_hash"])
        self.assertNotIn("command", payload)
        self.assertNotIn("stdout", payload)
        self.assertNotIn("stderr", payload)
        self.assertNotIn("auth_token", payload)
        self.assertNotIn("previous_receipt_hash", payload)
        serialized = receipts.canonical_json(payload)
        self.assertNotIn("test-secret", serialized)
        self.assertNotIn("test-session-token", serialized)

        without_hash = dict(payload)
        receipt_hash = without_hash.pop("receipt_hash")
        expected_hash = hashlib.sha256(receipts.canonical_json(without_hash).encode("utf-8")).hexdigest()
        self.assertEqual(expected_hash, receipt_hash)

    def test_validation_rejects_unsafe_path_transaction_and_metadata(self) -> None:
        for value in ("", " ", "/tmp/dir/", "/tmp/bad\nname", "x" * 4097):
            with self.subTest(path=value), self.assertRaises(core.RelayError):
                receipts.validate_receipt_path(core, value)

        for value in ("bad id", "x" * 129, "\n"):
            with self.subTest(transaction=value), self.assertRaises(core.RelayError):
                receipts.normalize_transaction_id(core, value)

        with self.assertRaises(core.RelayError):
            receipts.normalize_optional_text(core, "bad\nvalue", field="change_target", max_bytes=512)
        with self.assertRaises(core.RelayError):
            receipts.normalize_optional_text(core, "x" * 513, field="change_target", max_bytes=512)

    def test_safe_wire_payload_never_sets_legacy_risky_true(self) -> None:
        prepared = receipts.prepare_safe_request_payload(
            core,
            {"risky": True, "receipt_path": "~/.local/state/agent-safe/changes.jsonl"},
            {
                "transaction_id": "tx-safe",
                "change_target": "/etc/app.conf",
                "change_description": "обновлена конфигурация",
            },
        )
        self.assertIs(prepared["risky"], False)
        self.assertEqual(1, prepared["receipt_schema_version"])
        self.assertEqual("tx-safe", prepared["transaction_id"])
        self.assertNotIn("command", prepared)

    def test_parser_exposes_safe_metadata_only_for_risky_commands(self) -> None:
        parser = ssh_relay.build_parser()
        args = parser.parse_args([
            "exec",
            "--name", "ci",
            "--risky",
            "--transaction-id", "tx-123",
            "--change-target", "/etc/app.conf",
            "--change-description", "обновлена конфигурация",
            "true",
        ])
        self.assertEqual("tx-123", args.transaction_id)
        self.assertEqual("/etc/app.conf", args.change_target)
        self.assertEqual("обновлена конфигурация", args.change_description)

        args = parser.parse_args([
            "sudo-exec", "--risky", "--transaction-id", "tx-sudo", "id"
        ])
        self.assertEqual("tx-sudo", args.transaction_id)

        plain = parser.parse_args(["exec", "--transaction-id", "tx-invalid", "true"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(2, plain.handler(plain))
        self.assertIn("только вместе с --risky", stderr.getvalue())

    def test_old_daemon_capability_is_rejected_before_risky_command(self) -> None:
        parser = ssh_relay.build_parser()
        args = parser.parse_args(["exec", "--name", "ci", "--risky", "true"])
        calls: list[str] = []

        def request(_session, action, **_kwargs):
            calls.append(action)
            self.assertEqual("status", action)
            return {"ok": True, "ssh_status": "connected"}

        with (
            patch.object(core, "read_session", return_value=self.session),
            patch.object(core, "request_daemon", side_effect=request),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(1, core.exec_cmd(args))
        self.assertEqual(["status"], calls)
        self.assertIn("safe receipt v1", stderr.getvalue())

    def test_execute_receipt_returns_safe_summary_for_exec_and_sudo(self) -> None:
        with patch.object(core, "execute_remote_command", return_value={"ok": True, "exit_code": 0}) as execute:
            result = core.execute_risky_receipt(
                object(),
                session=self.session,
                action="exec",
                command="printf 'test-secret'",
                sudo=False,
                receipt_path="~/.local/state/agent-safe/changes.jsonl",
                timeout_seconds=5,
                sudo_password=None,
            )
        self.assertEqual("succeeded", result["receipt_status"])
        self.assertEqual("safe-writer-v1", result["receipt_command"])
        self.assertNotIn("test-secret", json.dumps(result, ensure_ascii=False))
        writer = execute.call_args.args[1]
        self.assertNotIn("test-secret", writer)
        self.assertIn("command_hash", writer)

        with self.assertRaises(core.RelayError):
            core.execute_risky_receipt(
                object(),
                session=self.session,
                action="sudo_exec",
                command="id",
                sudo=True,
                receipt_path="~/changes.jsonl",
                timeout_seconds=5,
                sudo_password=None,
            )

        with patch.object(core, "execute_sudo_command", return_value={"ok": True, "exit_code": 0}) as sudo_execute:
            sudo_result = core.execute_risky_receipt(
                object(),
                session=self.session,
                action="sudo_exec",
                command="id",
                sudo=True,
                receipt_path="~/changes.jsonl",
                timeout_seconds=5,
                sudo_password="test-sudo-secret",
            )
        self.assertEqual("succeeded", sudo_result["receipt_status"])
        self.assertNotIn("test-sudo-secret", json.dumps(sudo_result, ensure_ascii=False))
        self.assertNotIn("id\"", sudo_execute.call_args.args[1])
        self.assertEqual("test-sudo-secret", sudo_execute.call_args.args[3])

    def test_receipt_transport_unknown_is_distinct_from_definite_failure(self) -> None:
        unknown = core.RemoteCommandError(
            "transport lost",
            error_code="command_result_unknown",
            command_started=True,
        )
        with patch.object(core, "execute_remote_command", side_effect=unknown):
            result = core.execute_risky_receipt(
                object(), session=self.session, action="exec", command="true", sudo=False,
                receipt_path="~/changes.jsonl", timeout_seconds=5, sudo_password=None,
            )
        self.assertEqual("unknown", result["receipt_status"])
        self.assertEqual("receipt_write_unknown", result["error_code"])

        failed = core.RemoteCommandError(
            "not started",
            error_code="command_not_started",
            command_started=False,
        )
        with patch.object(core, "execute_remote_command", side_effect=failed):
            result = core.execute_risky_receipt(
                object(), session=self.session, action="exec", command="true", sudo=False,
                receipt_path="~/changes.jsonl", timeout_seconds=5, sudo_password=None,
            )
        self.assertEqual("failed", result["receipt_status"])
        self.assertEqual("command_not_started", result["error_code"])

    @unittest.skipIf(os.name == "nt", "POSIX writer проверяется только на Linux")
    def test_posix_writer_rejects_duplicate_and_symlink_and_uses_mode_0600(self) -> None:
        payload = receipts.build_receipt_payload(
            core,
            session=self.session,
            action="exec",
            command="true",
            sudo=False,
            transaction_id="tx-shell",
            receipt_id="receipt-1",
            change_target=None,
            change_description=None,
            timestamp_utc="2026-08-15T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "receipts" / "changes.jsonl"
            first = subprocess.run(
                ["sh", "-c", receipts.build_writer_command(core, path=str(path), payload=payload)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(payload, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

            duplicate = dict(payload)
            duplicate["receipt_id"] = "receipt-2"
            without_hash = dict(duplicate)
            without_hash.pop("receipt_hash")
            duplicate["receipt_hash"] = hashlib.sha256(
                receipts.canonical_json(without_hash).encode("utf-8")
            ).hexdigest()
            second = subprocess.run(
                ["sh", "-c", receipts.build_writer_command(core, path=str(path), payload=duplicate)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(receipts.WRITER_EXIT_DUPLICATE, second.returncode)
            self.assertEqual(1, len(path.read_text(encoding="utf-8").splitlines()))

            target = Path(temp_dir) / "target.jsonl"
            target.write_text("sentinel\n", encoding="utf-8")
            link = Path(temp_dir) / "link.jsonl"
            link.symlink_to(target)
            symlink = subprocess.run(
                ["sh", "-c", receipts.build_writer_command(core, path=str(link), payload=payload)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(receipts.WRITER_EXIT_SYMLINK, symlink.returncode)
            self.assertEqual("sentinel\n", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
