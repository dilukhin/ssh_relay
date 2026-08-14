#!/usr/bin/env python3
"""Интеграционные тесты жизненного цикла локальных session-файлов."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ssh_relay
import ssh_relay_core as core


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "state-root"
        self.env = patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.root),
                "XDG_STATE_HOME": str(self.root),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def session(self, name: str, token: str, *, daemon_port: int = 41000) -> dict:
        return {
            "name": name,
            "host": "198.51.100.42",
            "port": 22,
            "user": "donpedro",
            "daemon_port": daemon_port,
            "auth_token": token,
            "pid": 12345,
            "version": ssh_relay.__version__,
        }

    def cleanup(self, name: str, token: str) -> None:
        core.remove_session_file(name, token)

    def test_named_sessions_are_independent(self) -> None:
        alpha = self.session("alpha", "token-alpha", daemon_port=41001)
        beta = self.session("beta", "token-beta", daemon_port=41002)
        core.write_session("alpha", alpha)
        core.write_session("beta", beta)
        try:
            self.assertEqual(["alpha", "beta"], core.iter_session_names())
            self.assertEqual("token-alpha", core.read_session("alpha")["auth_token"])
            self.assertEqual("token-beta", core.read_session("beta")["auth_token"])

            core.remove_session_file("alpha", "token-alpha")
            self.assertFalse(core.session_file_path("alpha").exists())
            self.assertEqual("token-beta", core.read_session("beta")["auth_token"])
        finally:
            self.cleanup("alpha", "token-alpha")
            self.cleanup("beta", "token-beta")

    def test_foreign_token_replacement_is_not_removed_by_old_owner(self) -> None:
        old = self.session("shared", "old-token")
        new = self.session("shared", "new-token", daemon_port=42000)
        core.write_session("shared", old)
        core.write_session("shared", new)
        try:
            core.remove_session_file("shared", "old-token")
            current = core.read_session("shared")
            self.assertEqual("new-token", current["auth_token"])
            self.assertEqual(42000, current["daemon_port"])
        finally:
            self.cleanup("shared", "new-token")

    def test_legacy_default_is_read_and_migrated_without_touching_named_session(self) -> None:
        core.prepare_session_directory()
        legacy = self.session("default", "legacy-token", daemon_port=43000)
        core.legacy_session_file_path().write_text(json.dumps(legacy), encoding="utf-8")
        named = self.session("other", "other-token", daemon_port=43001)
        core.write_session("other", named)
        try:
            read_legacy = core.read_session("default")
            self.assertEqual("legacy-token", read_legacy["auth_token"])
            self.assertEqual(str(core.legacy_session_file_path()), read_legacy["_session_file_path"])

            current = self.session("default", "current-token", daemon_port=43002)
            core.write_session("default", current)
            self.assertFalse(core.legacy_session_file_path().exists())
            self.assertEqual("current-token", core.read_session("default")["auth_token"])
            self.assertEqual("other-token", core.read_session("other")["auth_token"])
        finally:
            self.cleanup("default", "current-token")
            self.cleanup("other", "other-token")
            core.legacy_session_file_path().unlink(missing_ok=True)

    @unittest.skipIf(os.name == "nt", "POSIX-права проверяются только на Linux/Unix")
    def test_posix_state_and_session_permissions_are_restricted(self) -> None:
        session = self.session("perm", "perm-token")
        path = core.write_session("perm", session)
        try:
            self.assertEqual(0o700, stat.S_IMODE(core.state_directory().stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(core.sessions_directory().stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        finally:
            self.cleanup("perm", "perm-token")

    def test_failed_atomic_replace_keeps_old_session_and_removes_tmp(self) -> None:
        old = self.session("atomic", "old-token")
        new = self.session("atomic", "new-token", daemon_port=44000)
        path = core.write_session("atomic", old)
        temporary = path.with_suffix(".tmp")
        try:
            with patch.object(core.os, "replace", side_effect=PermissionError("тестовый отказ replace")):
                with self.assertRaises(PermissionError):
                    core.write_session("atomic", new)

            self.assertFalse(temporary.exists())
            current = core.read_session("atomic")
            self.assertEqual("old-token", current["auth_token"])
        finally:
            self.cleanup("atomic", "old-token")

    def test_corrupted_one_session_does_not_break_other_named_session(self) -> None:
        good = self.session("good", "good-token")
        core.write_session("good", good)
        core.prepare_session_directory()
        core.session_file_path("broken").write_text("not-json", encoding="utf-8")
        try:
            self.assertEqual(["broken", "good"], core.iter_session_names())
            with self.assertRaises(core.RelayError):
                core.read_session("broken")
            self.assertEqual("good-token", core.read_session("good")["auth_token"])
        finally:
            self.cleanup("good", "good-token")
            core.session_file_path("broken").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
