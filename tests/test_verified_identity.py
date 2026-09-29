"""Локальные unit-тесты identity без реального SSH."""

import base64
import hashlib
import unittest
from unittest.mock import Mock

import ssh_relay_identity as identity


class VerifiedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.key = Mock()
        self.key.asbytes.return_value = b"fake-public-host-key"
        self.key.get_name.return_value = "ssh-ed25519"
        self.transport = Mock()
        self.transport.is_active.return_value = True
        self.transport.is_authenticated.return_value = True
        self.transport.get_remote_server_key.return_value = self.key
        self.client = Mock()
        self.client.get_transport.return_value = self.transport

    def observed(self):
        return identity.observed_identity(
            self.client, host="198.51.100.42", port=22, user="donpedro",
            daemon_instance_id="24fd0686-7911-49f2-bda8-f86410f3674d", connection_generation=1,
        )

    def test_identity_comes_from_authenticated_transport_public_key(self):
        actual = self.observed()
        digest = base64.b64encode(hashlib.sha256(b"fake-public-host-key").digest()).decode("ascii").rstrip("=")
        self.assertEqual("SHA256:" + digest, actual["host_key_sha256"])
        self.assertEqual("ssh-ed25519", actual["host_key_algorithm"])
        self.assertTrue(identity.trusted_match(dict(actual), actual))

    def test_inactive_or_unauthenticated_transport_has_no_identity(self):
        for active, authenticated in ((False, True), (True, False)):
            with self.subTest(active=active, authenticated=authenticated):
                self.transport.is_active.return_value = active
                self.transport.is_authenticated.return_value = authenticated
                self.assertIsNone(self.observed())

    def test_strict_mismatch_and_reconnect_generation(self):
        actual = self.observed()
        for field, value in (("host_key_sha256", "SHA256:" + "A" * 43),
                             ("remote_host", "198.51.100.99"), ("remote_port", 2222),
                             ("remote_user", "other"), ("connection_generation", 2),
                             ("daemon_instance_id", "cc38fcdf-7ea9-4797-a13b-32dfc44ec561")):
            with self.subTest(field=field):
                expected = dict(actual, **{field: value})
                self.assertFalse(identity.trusted_match(expected, actual))
        self.assertFalse(identity.valid_expected(dict(actual, extra="unknown")))
        self.assertFalse(identity.valid_expected(dict(actual, trusted_host_key=False)))


if __name__ == "__main__":
    unittest.main()
