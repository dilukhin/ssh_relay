#!/usr/bin/env python3
"""Регрессионные тесты защиты локальной relay-сессии."""

from __future__ import annotations

import io
import json
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import ssh_relay_session as session_safety


class FakeRelayError(Exception):
    pass


class FakeDaemonUnavailableError(FakeRelayError):
    pass


class SessionSafetyTests(unittest.TestCase):
    def make_core(self, root: Path):
        calls: dict[str, list] = {"request": [], "remove": [], "write": []}

        def state_directory() -> Path:
            return root

        def sessions_directory() -> Path:
            return root / "sessions"

        def session_file_path(name: str) -> Path:
            return sessions_directory() / f"{name}.json"

        def legacy_session_file_path() -> Path:
            return root / ".ssh_relay_session.json"

        def prepare_session_directory() -> None:
            sessions_directory().mkdir(parents=True, exist_ok=True)

        def original_write_session(name: str, session: dict):
            prepare_session_directory()
            path = session_file_path(name)
            path.write_text(json.dumps(session), encoding="utf-8")
            calls["write"].append((name, dict(session)))
            return path

        def original_remove_session_file(name: str, expected_token=None):
            calls["remove"].append((name, expected_token))
            session_file_path(name).unlink(missing_ok=True)

        def original_request_daemon(session, action, *, response_timeout=5, **payload):
            calls["request"].append((action, response_timeout, dict(payload)))
            return {"ok": True, "status": "active"}

        core = types.SimpleNamespace(
            DEFAULT_SESSION_NAME="default",
            RelayError=FakeRelayError,
            DaemonUnavailableError=FakeDaemonUnavailableError,
            state_directory=state_directory,
            sessions_directory=sessions_directory,
            session_file_path=session_file_path,
            legacy_session_file_path=legacy_session_file_path,
            prepare_session_directory=prepare_session_directory,
            write_session=original_write_session,
            remove_session_file=original_remove_session_file,
            request_daemon=original_request_daemon,
            read_session=lambda name: {"name": name, "auth_token": "token"},
            stop_one_session=lambda name: 0,
        )
        return core, calls

    def test_ambiguous_remove_without_token_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            core, calls = self.make_core(Path(tmp))
            session_safety.install(core)

            core.remove_session_file("default")
            self.assertEqual([], calls["remove"])

            core.remove_session_file("default", "token")
            self.assertEqual([("default", "token")], calls["remove"])

    def test_status_is_retried_but_exec_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            core, calls = self.make_core(Path(tmp))
            attempts = {"status": 0, "exec": 0}

            def flaky_request(session, action, *, response_timeout=5, **payload):
                calls["request"].append((action, response_timeout, dict(payload)))
                attempts[action] += 1
                if action == "status" and attempts[action] < 3:
                    raise FakeDaemonUnavailableError("временный timeout")
                if action == "exec":
                    raise FakeDaemonUnavailableError("неизвестный результат")
                return {"ok": True, "status": "active"}

            core.request_daemon = flaky_request
            session_safety.install(core)

            with patch.object(session_safety.time, "sleep", return_value=None):
                result = core.request_daemon({"auth_token": "token"}, "status")
            self.assertTrue(result["ok"])
            self.assertEqual(3, attempts["status"])

            with self.assertRaises(FakeDaemonUnavailableError):
                core.request_daemon({"auth_token": "token"}, "exec", command="hostname")
            self.assertEqual(1, attempts["exec"])

    def test_restore_creates_only_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            core, _ = self.make_core(Path(tmp))
            session = {
                "name": "default",
                "auth_token": "old-token",
                "pid": 123,
                "daemon_port": 456,
                "_session_file_path": "служебное поле",
            }

            restored = session_safety.restore_session_file_if_missing(core, "default", session)
            self.assertIsNotNone(restored)
            data = json.loads(core.session_file_path("default").read_text(encoding="utf-8"))
            self.assertEqual("old-token", data["auth_token"])
            self.assertNotIn("_session_file_path", data)

            core.session_file_path("default").write_text(
                json.dumps({"auth_token": "new-token"}),
                encoding="utf-8",
            )
            restored_again = session_safety.restore_session_file_if_missing(core, "default", session)
            self.assertIsNone(restored_again)
            data = json.loads(core.session_file_path("default").read_text(encoding="utf-8"))
            self.assertEqual("new-token", data["auth_token"])

    def test_write_starts_guard_and_restores_deleted_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            core, _ = self.make_core(Path(tmp))
            session = {
                "name": "default",
                "auth_token": "token",
                "pid": 123,
                "daemon_port": 456,
            }

            with patch.object(session_safety, "SESSION_GUARD_INTERVAL", 0.01):
                session_safety.install(core)
                path = core.write_session("default", session)
                path.unlink()
                deadline = time.monotonic() + 1.0
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(path.exists())
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual("token", data["auth_token"])

                core.remove_session_file("default", "token")
                time.sleep(0.05)
                self.assertFalse(path.exists())

    def test_stop_timeout_preserves_session_and_reports_uncertain_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            core, calls = self.make_core(Path(tmp))
            core.prepare_session_directory()
            core.session_file_path("default").write_text("{}", encoding="utf-8")

            def unavailable(*args, **kwargs):
                raise FakeDaemonUnavailableError("Daemon недоступен или не ответил вовремя.")

            core.request_daemon = unavailable
            session_safety.install(core)

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = core.stop_one_session("default")
            self.assertEqual(1, rc)
            self.assertTrue(core.session_file_path("default").exists())
            self.assertEqual([], calls["remove"])
            self.assertIn("файл сессии сохранён", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
