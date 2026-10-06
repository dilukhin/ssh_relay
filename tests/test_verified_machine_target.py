"""Контроль точного pin без вызова реального удалённого узла."""

import io
import hashlib
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import ssh_relay
import ssh_relay_core as core
import ssh_relay_p0_contract as p0
import ssh_relay_outcomes as outcomes


PIN = {
    "schema_version": 1, "remote_host": "198.51.100.42", "remote_port": 22,
    "remote_user": "donpedro", "host_key_algorithm": "ssh-ed25519",
    "remote_host_key_sha256": "SHA256:" + "A" * 43, "trusted_known_hosts": True,
    "daemon_instance_id": "24743dd2-f1cc-4dba-8643-41d054fe0eb7",
    "connection_generation": 1, "daemon_source_sha": "a" * 40,
}
SESSION = {
    "name": "synthetic", "host": "198.51.100.42", "port": 22, "user": "donpedro",
    "command_timeout": 30, "reconnect_wait": 1,
}


def flags():
    return ["--require-verified-identity", "--expected-remote-host", PIN["remote_host"],
            "--expected-remote-port", "22", "--expected-remote-user", PIN["remote_user"],
            "--expected-host-key-algorithm", PIN["host_key_algorithm"],
            "--expected-host-key-sha256", PIN["remote_host_key_sha256"],
            "--expected-daemon-instance-id", PIN["daemon_instance_id"],
            "--expected-connection-generation", "1", "--expected-daemon-source-sha", "a" * 40]


class VerifiedMachineTargetTests(unittest.TestCase):
    def run_case(self, status_identity=None, reply_factory=None, argv=None):
        args = ssh_relay.build_parser().parse_args(
            argv or ["exec", "--name", "synthetic", "--json", "--risky", *flags(),
                     "--transaction-id", "test-tx", "true"])
        calls = []

        def request(_session, action, **kwargs):
            calls.append((action, kwargs))
            if action == "status":
                return {"ok": True, "version": ssh_relay.__version__, "ssh_status": "connected",
                        "receipt_schema_version": 1, "risky_identity_schema_version": 1,
                        "verified_identity_schema_version": 1,
                        "verified_identity": status_identity if status_identity is not None else PIN}
            if reply_factory is None:
                raise AssertionError("Основная команда не должна стартовать")
            return reply_factory(kwargs)

        with (patch.object(core, "read_session", return_value=SESSION),
              patch.object(core, "request_daemon", side_effect=request),
              patch.object(p0, "source_sha", return_value="a" * 40),
              patch.object(outcomes, "source_sha", return_value="a" * 40),
              redirect_stdout(io.StringIO()) as output):
            code = args.handler(args)
        return code, json.loads(output.getvalue()), calls

    def test_verified_nonrisky_exec_pins_transport_without_receipt(self):
        def reply(kwargs):
            self.assertEqual(PIN, kwargs["expected_verified_identity"])
            self.assertFalse(kwargs["risky"])
            self.assertNotIn("receipt_id", kwargs)
            return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": "", "verified_identity": PIN}
        code, payload, calls = self.run_case(reply_factory=reply, argv=[
            "exec", "--name", "synthetic", "--json", *flags(), "true"])
        self.assertEqual(0, code)
        self.assertEqual("succeeded", payload["operation_status"])
        self.assertEqual(PIN, payload["verified_identity"])
        self.assertEqual("not_requested", payload["receipt_status"])
        self.assertFalse(payload["risky"])
        self.assertEqual(["status", "exec"], [action for action, _ in calls])

    def test_nonrisky_pin_drift_or_missing_proof_is_not_success(self):
        argv = ["exec", "--name", "synthetic", "--json", *flags(), "true"]
        code, payload, calls = self.run_case(status_identity={**PIN, "connection_generation": 2}, argv=argv)
        self.assertEqual(10, code)
        self.assertEqual(["status"], [action for action, _ in calls])
        for proof in (None, {**PIN, "remote_host_key_sha256": "SHA256:" + "B" * 43}):
            with self.subTest(proof=proof):
                code, payload, calls = self.run_case(argv=argv, reply_factory=lambda _kw: {
                    "ok": True, "exit_code": 0, "verified_identity": proof})
                self.assertEqual(13, code)
                self.assertEqual("unknown", payload["operation_status"])
                self.assertEqual(1, sum(action == "exec" for action, _ in calls))

    def test_nonrisky_partial_pin_is_rejected(self):
        code, payload, calls = self.run_case(argv=["exec", "--json", "--name", "synthetic",
                                                  "--expected-remote-host", PIN["remote_host"], "true"])
        self.assertEqual(10, code)
        self.assertEqual("invalid_verified_identity", payload["error_code"])
        self.assertEqual([], calls)

    def test_preflight_wrong_key_or_generation_blocks_before_command(self):
        for change in ({"remote_host_key_sha256": "SHA256:" + "B" * 43},
                       {"connection_generation": 2}):
            with self.subTest(change=change):
                code, payload, calls = self.run_case(status_identity={**PIN, **change})
                self.assertEqual(10, code)
                self.assertEqual("not_started", payload["operation_status"])
                self.assertEqual(["status"], [action for action, _ in calls])

    def test_verified_request_passes_exact_pin_to_same_daemon_exec(self):
        def reply(kwargs):
            self.assertEqual(PIN, kwargs["expected_verified_identity"])
            return {"ok": True, "exit_code": 0, "stdout": "ok\n", "stderr": "",
                    "verified_identity": PIN, "identity_current_after_result": True,
                    "remote_host": PIN["remote_host"], "remote_port": 22, "remote_user": PIN["remote_user"],
                    "remote_host_key_sha256": PIN["remote_host_key_sha256"],
                    "risky_receipt": {"receipt_status": "succeeded", "transaction_id": "test-tx",
                                      "receipt_id": kwargs["receipt_id"], "receipt_hash": "a" * 64,
                                      "command_hash": hashlib.sha256(b"true").hexdigest(),
                                      "remote_host_key_sha256": PIN["remote_host_key_sha256"],
                                      "verified_identity": PIN}}

        code, payload, calls = self.run_case(reply_factory=reply)
        self.assertEqual(0, code)
        self.assertEqual("succeeded", payload["operation_status"])
        self.assertEqual(PIN, payload["verified_identity"])
        self.assertEqual(hashlib.sha256(b"true").hexdigest(), payload["command_hash"])
        self.assertEqual(["status", "exec"], [action for action, _ in calls])

    def test_daemon_rejects_pin_drift_before_ssh_command(self):
        code, payload, calls = self.run_case(reply_factory=lambda _kw: {
            "ok": False, "command_started": False, "error_code": "verified_identity_mismatch",
            "verified_identity": {**PIN, "connection_generation": 2},
        })
        self.assertEqual(10, code)
        self.assertEqual("not_started", payload["operation_status"])
        self.assertEqual(["status", "exec"], [action for action, _ in calls])

    def test_missing_verified_result_after_possible_execution_is_unknown(self):
        code, payload, _calls = self.run_case(reply_factory=lambda _kw: {
            "ok": True, "exit_code": 0, "risky_receipt": {"receipt_status": "succeeded"},
        })
        self.assertEqual(13, code)
        self.assertEqual("unknown", payload["operation_status"])

    def test_verified_remote_nonzero_has_separate_exit_and_no_receipt(self):
        code, payload, _calls = self.run_case(reply_factory=lambda _kw: {
            "ok": True, "exit_code": 7, "stdout": "", "stderr": "failed",
            "verified_identity": PIN, "remote_host_key_sha256": PIN["remote_host_key_sha256"],
            "remote_host": PIN["remote_host"], "remote_port": 22, "remote_user": PIN["remote_user"],
        })
        self.assertEqual(11, code)
        self.assertEqual("command_failed", payload["operation_status"])
        self.assertEqual(7, payload["command_exit_code"])
        self.assertEqual("not_attempted", payload["receipt_status"])

    def test_verified_failed_or_unknown_receipt_is_partial_not_success(self):
        for receipt_status in ("failed", "unknown"):
            with self.subTest(receipt_status=receipt_status):
                def reply(kwargs):
                    command = {"ok": True, "exit_code": 0, "stdout": "", "stderr": "",
                               "verified_identity": PIN, "remote_host_key_sha256": PIN["remote_host_key_sha256"],
                               "remote_host": PIN["remote_host"], "remote_port": 22, "remote_user": PIN["remote_user"]}
                    receipt = {"receipt_status": receipt_status, "transaction_id": "test-tx",
                               "receipt_id": kwargs["receipt_id"], "receipt_hash": "a" * 64,
                               "remote_host_key_sha256": PIN["remote_host_key_sha256"]}
                    return {"ok": False, "command_result": command, "receipt_result": receipt,
                            "verified_identity": PIN}

                code, payload, calls = self.run_case(reply_factory=reply)
                self.assertEqual(12, code, payload)
                self.assertEqual("partial_success", payload["operation_status"])
                self.assertTrue(payload["partial_success"])
                self.assertEqual(1, sum(action == "exec" for action, _ in calls))

    def test_lost_response_after_possible_delivery_keeps_only_preflight(self):
        def lost(_kwargs):
            raise core.DaemonRequestError("response lost", request_sent=True,
                                          error_code="daemon_response_lost")

        code, payload, calls = self.run_case(reply_factory=lost)
        self.assertEqual(13, code)
        self.assertEqual("unknown", payload["operation_status"])
        self.assertEqual(PIN, payload["preflight_verified_identity"])
        self.assertNotIn("verified_identity", payload)
        self.assertEqual(1, sum(action == "exec" for action, _ in calls))

    def test_tampered_receipt_identity_cannot_claim_success(self):
        def reply(kwargs):
            return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "",
                    "verified_identity": PIN, "remote_host_key_sha256": PIN["remote_host_key_sha256"],
                    "remote_host": PIN["remote_host"], "remote_port": 22, "remote_user": PIN["remote_user"],
                    "risky_receipt": {"receipt_status": "succeeded", "transaction_id": "test-tx",
                                      "receipt_id": kwargs["receipt_id"], "receipt_hash": "a" * 64,
                                      "command_hash": hashlib.sha256(b"true").hexdigest(),
                                      "remote_host_key_sha256": PIN["remote_host_key_sha256"],
                                      "verified_identity": {**PIN, "connection_generation": 2}}}

        code, payload, _calls = self.run_case(reply_factory=reply)
        self.assertEqual(13, code)
        self.assertEqual("unknown", payload["operation_status"])

    def test_legacy_mode_is_distinct_and_partial_pin_fails_closed(self):
        argv = ["exec", "--name", "synthetic", "--json", "--risky",
                "--expected-remote-host", PIN["remote_host"], "true"]
        code, payload, calls = self.run_case(argv=argv)
        self.assertEqual(10, code)
        self.assertEqual("invalid_verified_identity", payload["error_code"])
        self.assertEqual([], calls)

    def test_status_json_refuses_old_daemon_identity(self):
        args = ssh_relay.build_parser().parse_args(["status", "--name", "synthetic", "--json"])
        with (patch.object(core, "read_session", return_value=SESSION),
              patch.object(core, "request_daemon", return_value={"ok": True, "ssh_status": "connected"}),
              patch.object(ssh_relay, "source_sha", return_value="a" * 40),
              redirect_stdout(io.StringIO()) as output):
            code = args.handler(args)
        self.assertEqual(10, code)
        self.assertIsNone(json.loads(output.getvalue())["verified_identity"])

    def test_status_json_reports_only_daemon_attested_current_identity(self):
        args = ssh_relay.build_parser().parse_args(["status", "--name", "synthetic", "--json"])
        status = {"ok": True, "ssh_status": "connected", "version": ssh_relay.__version__,
                  "verified_identity_schema_version": 1, "verified_identity": PIN,
                  "receipt_schema_version": 1}
        with (patch.object(core, "read_session", return_value=SESSION),
              patch.object(core, "request_daemon", return_value=status),
              patch.object(ssh_relay, "source_sha", return_value="a" * 40),
              redirect_stdout(io.StringIO()) as output):
            code = args.handler(args)
        self.assertEqual(0, code)
        self.assertEqual(PIN, json.loads(output.getvalue())["verified_identity"])


if __name__ == "__main__":
    unittest.main()
