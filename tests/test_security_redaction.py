#!/usr/bin/env python3
"""Unit-тесты редукции известных relay-секретов из исключений."""

from __future__ import annotations

import types
import unittest

import ssh_relay_session as session_safety

SSH_SECRET = "TEST_SSH_SECRET_CHAIN_9f03d8"
PASSPHRASE_SECRET = "TEST_PASSPHRASE_SECRET_CHAIN_1b6a77"
SUDO_SECRET = "TEST_SUDO_SECRET_CHAIN_44c821"


class FakeRelayError(Exception):
    pass


class SecretBearingSSHClient:
    def connect(self, *args, **kwargs):
        raise OSError(
            f"connect failed password={kwargs.get('password')} passphrase={kwargs.get('passphrase')}"
        )


class FakeParamiko:
    @staticmethod
    def SSHClient():
        return SecretBearingSSHClient()


class SecretRedactionTests(unittest.TestCase):
    def make_core(self):
        def execute_remote_command(client, command, timeout_seconds, stdin_data=None):
            if stdin_data is None:
                raise OSError("ошибка без известного секрета")
            raise OSError(f"channel failed with stdin={stdin_data.decode('utf-8', errors='replace')}")

        core = types.SimpleNamespace(
            RelayError=FakeRelayError,
            load_paramiko=lambda: FakeParamiko,
            execute_remote_command=execute_remote_command,
            _session_safety_installed=True,
        )
        session_safety.install(core)
        return core

    def assert_clean_exception(self, exc: BaseException, *secrets: str) -> None:
        text = str(exc)
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertIn(session_safety.REDACTED_SECRET, text)
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)

    def test_ssh_password_and_passphrase_are_removed_without_secret_exception_chain(self) -> None:
        core = self.make_core()
        client = core.load_paramiko().SSHClient()

        with self.assertRaises(FakeRelayError) as captured:
            client.connect(password=SSH_SECRET, passphrase=PASSPHRASE_SECRET)

        self.assert_clean_exception(captured.exception, SSH_SECRET, PASSPHRASE_SECRET)

    def test_sudo_stdin_is_removed_without_secret_exception_chain(self) -> None:
        core = self.make_core()

        with self.assertRaises(FakeRelayError) as captured:
            core.execute_remote_command(
                object(),
                "sudo -S -p '' -- sh -c true",
                10,
                stdin_data=(SUDO_SECRET + "\n").encode("utf-8"),
            )

        self.assert_clean_exception(captured.exception, SUDO_SECRET)

    def test_exception_without_known_secret_is_preserved(self) -> None:
        core = self.make_core()

        with self.assertRaises(OSError) as captured:
            core.execute_remote_command(object(), "true", 10)

        self.assertEqual("ошибка без известного секрета", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
