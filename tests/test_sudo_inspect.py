"""Искусственные проверки намерения; привилегированные команды не исполняются."""

import copy
import dataclasses
import json
import unittest
from unittest.mock import patch

import ssh_relay
import ssh_relay_core as core
import ssh_relay_sudo_inspect as inspect


PIN = {
    "schema_version": 1, "remote_host": "198.51.100.42", "remote_port": 22,
    "remote_user": "fixture", "host_key_algorithm": "ssh-ed25519",
    "remote_host_key_sha256": "SHA256:" + "A" * 43, "trusted_known_hosts": True,
    "daemon_instance_id": "24743dd2-f1cc-4dba-8643-41d054fe0eb7",
    "connection_generation": 1, "daemon_source_sha": "a" * 40,
}


def parameters():
    return {
        "probe_id": "nft_summary", "session": "fixture", "expected_identity": copy.deepcopy(PIN),
        "transport_source_sha": "a" * 40, "transport_executable_sha256": "b" * 64,
        "transaction_id": "d73ccf57-1b8a-41e1-aadb-8b929a104694",
        "deadline_unix_ms": 60_001, "now_unix_ms": 1,
    }


class InspectIntentTests(unittest.TestCase):
    def test_exact_closed_catalog_and_budgets(self):
        self.assertEqual({"nft_summary", "iptables_v4_summary", "iptables_v6_summary",
                          "ufw_summary", "ss_listeners_summary"}, set(inspect.PROBES))
        for probe_id, spec in inspect.PROBES.items():
            with self.subTest(probe=probe_id):
                intent = inspect.prepare_intent(**{**parameters(), "probe_id": probe_id})
                operation = json.loads(intent.canonical_bytes)
                self.assertEqual(list(spec.argv), operation["execution_spec"]["argv"])
                self.assertEqual({"LC_ALL": "C"}, operation["execution_spec"]["environment"])
                self.assertEqual("closed", operation["execution_spec"]["stdin"])
                self.assertEqual(10, operation["execution_spec"]["timeout_seconds"])
                self.assertEqual(262_144, operation["execution_spec"]["max_output_bytes"])
        with self.assertRaises(TypeError):
            inspect.PROBES["shell"] = inspect.PROBES["nft_summary"]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            inspect.PROBES["nft_summary"].argv = ("sh",)

    def test_injection_unknown_probe_and_custom_command_rejected(self):
        for value in ("nft_summary; id", "nft_summary\n", "nft_summary --file /tmp/x",
                      "/usr/sbin/nft", "sudo-exec", "", None, []):
            with self.subTest(probe=value), self.assertRaises(ValueError):
                inspect.prepare_intent(**{**parameters(), "probe_id": value})
        for field, value in (("argv", ["sh"]), ("command", "id"), ("approved", True),
                             ("grant", "fixture"), ("timeout_seconds", 3600)):
            with self.subTest(field=field), self.assertRaises(TypeError):
                inspect.prepare_intent(**parameters(), **{field: value})

    def test_all_identity_fields_required_and_strict(self):
        for field in PIN:
            value = parameters()
            del value["expected_identity"][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                inspect.prepare_intent(**value)
        for change in ({"remote_port": True}, {"connection_generation": True},
                       {"trusted_known_hosts": False}, {"daemon_source_sha": "a" * 8},
                       {"host_key_algorithm": ""}, {"remote_host_key_sha256": "bad"},
                       {"remote_user": "fixture\x00"}, {"extra": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                inspect.prepare_intent(**{**parameters(), "expected_identity": {**PIN, **change}})

    def test_snapshot_cannot_be_changed_via_original_dictionary(self):
        value = parameters()
        prepared = inspect.prepare_intent(**value)
        initial_hash = prepared.sha256
        value["expected_identity"]["connection_generation"] = 9
        self.assertEqual(1, json.loads(prepared.canonical_bytes)["expected_identity"]["connection_generation"])
        self.assertEqual(initial_hash, prepared.sha256)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            prepared.canonical_bytes = b"{}"
        self.assertEqual(prepared, inspect.prepare_intent(**parameters()))

    def test_digest_binds_every_pin_and_operation_component(self):
        initial = inspect.prepare_intent(**parameters()).sha256
        for field, replacement in (
            ("remote_host", "198.51.100.43"), ("remote_port", 2222), ("remote_user", "other"),
            ("host_key_algorithm", "ssh-rsa"), ("remote_host_key_sha256", "SHA256:" + "B" * 43),
            ("daemon_instance_id", "34743dd2-f1cc-4dba-8643-41d054fe0eb7"),
            ("connection_generation", 2), ("daemon_source_sha", "c" * 40),
        ):
            value = parameters()
            value["expected_identity"][field] = replacement
            if field == "daemon_source_sha":
                value["transport_source_sha"] = replacement
            with self.subTest(pin=field):
                self.assertNotEqual(initial, inspect.prepare_intent(**value).sha256)
        for field, replacement in (
            ("session", "other"), ("probe_id", "ufw_summary"), ("deadline_unix_ms", 60_000),
            ("transport_executable_sha256", "c" * 64),
            ("transaction_id", "e73ccf57-1b8a-41e1-aadb-8b929a104694"),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(initial, inspect.prepare_intent(**{**parameters(), field: replacement}).sha256)

    def test_identity_is_copied_before_validation(self):
        value = parameters()
        validate = inspect.identity.validate_expected

        def mutate_original(snapshot):
            value["expected_identity"]["connection_generation"] = 9
            return validate(snapshot)

        with patch.object(inspect.identity, "validate_expected", side_effect=mutate_original):
            prepared = inspect.prepare_intent(**value)
        self.assertEqual(1, json.loads(prepared.canonical_bytes)["expected_identity"]["connection_generation"])
        for invalid in (None, [], "identity"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                inspect.prepare_intent(**{**parameters(), "expected_identity": invalid})

    def test_deadline_and_malformed_binding_rejected(self):
        for field, replacement in (
            ("deadline_unix_ms", 1), ("deadline_unix_ms", 0), ("deadline_unix_ms", 60_002),
            ("deadline_unix_ms", True), ("deadline_unix_ms", 1.5),
            ("now_unix_ms", -1), ("now_unix_ms", False),
            ("transport_source_sha", "c" * 40), ("transport_source_sha", "a" * 8),
            ("transport_source_sha", None), ("transport_executable_sha256", "b" * 63),
            ("transport_executable_sha256", None), ("transaction_id", "not-a-uuid"),
            ("transaction_id", "D73CCF57-1B8A-41E1-AADB-8B929A104694"),
            ("transaction_id", None), ("session", "../fixture"), ("session", ""), ("session", None),
        ):
            with self.subTest(field=field, value=replacement), self.assertRaises(ValueError):
                inspect.prepare_intent(**{**parameters(), field: replacement})

    def test_disabled_result_has_no_execution_or_output_claim(self):
        result = inspect.blocked_result()
        self.assertEqual("not_started", result["operation_status"])
        self.assertFalse(result["command_started"])
        self.assertIsNone(result["command_exit_code"])
        self.assertEqual("disabled", result["replay_status"])
        self.assertEqual("not_requested", result["receipt_status"])
        self.assertTrue({"stdout", "stderr", "verified_identity", "grant", "summary"}.isdisjoint(result))
        result["operation_status"] = "succeeded"
        self.assertEqual("not_started", inspect.blocked_result()["operation_status"])

    def test_public_activation_is_not_available(self):
        with patch.object(core, "daemon", side_effect=AssertionError("Запуск запрещён")):
            for argv in (["sudo-inspect", "--json", "--probe", "nft_summary"],
                         ["daemon", "--host", "198.51.100.42", "--user", "fixture",
                          "--enable-sudo-inspect"]):
                with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit) as exc:
                    ssh_relay.build_parser().parse_args(argv)
                self.assertEqual(2, exc.exception.code)
