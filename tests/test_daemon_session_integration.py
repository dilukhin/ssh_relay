#!/usr/bin/env python3
"""Интеграционный тест self-heal session-файла живого daemon."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssh_relay
import ssh_relay_core as core


class FakeTransport:
    def __init__(self) -> None:
        self.active = True
        self.keepalive = None

    def is_active(self) -> bool:
        return self.active

    def is_authenticated(self) -> bool:
        return self.active

    def set_keepalive(self, seconds: int) -> None:
        self.keepalive = seconds


class FakeSSHClient:
    def __init__(self) -> None:
        self.transport = FakeTransport()

    def load_system_host_keys(self, filename=None) -> None:
        pass

    def set_missing_host_key_policy(self, policy) -> None:
        pass

    def connect(self, *args, **kwargs) -> None:
        self.transport.active = True

    def get_transport(self) -> FakeTransport:
        return self.transport

    def close(self) -> None:
        self.transport.active = False


class FakeRejectPolicy:
    pass


FAKE_PARAMIKO = types.SimpleNamespace(
    SSHClient=FakeSSHClient,
    RejectPolicy=FakeRejectPolicy,
)


class DaemonSessionIntegrationTests(unittest.TestCase):
    def wait_session(self, path: Path, *, token: str | None = None, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if token is None or data.get("auth_token") == token:
                    return data
            except (OSError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(0.02)
        if last_error is not None:
            raise AssertionError(f"Session-файл не стал доступен: {last_error}") from last_error
        raise AssertionError("Session-файл не появился с ожидаемым токеном.")

    def test_live_daemon_restores_missing_session_without_overwriting_foreign_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "LOCALAPPDATA": tmp,
                "XDG_STATE_HOME": tmp,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(core, "load_paramiko", return_value=FAKE_PARAMIKO),
                patch.object(core.getpass, "getpass", return_value="test-password"),
            ):
                args = ssh_relay.build_parser().parse_args([
                    "daemon",
                    "--name",
                    "ci-session",
                    "--host",
                    "198.51.100.42",
                    "--user",
                    "donpedro",
                ])
                result: dict[str, object] = {}
                errors: list[BaseException] = []

                def run_daemon() -> None:
                    try:
                        result["code"] = args.handler(args)
                    except BaseException as exc:  # pragma: no cover - только диагностика CI
                        errors.append(exc)

                thread = threading.Thread(target=run_daemon, name="ci-relay-daemon", daemon=True)
                thread.start()

                session_path = core.session_file_path("ci-session")
                session_data = self.wait_session(session_path)
                original_token = str(session_data["auth_token"])
                original_session = core.read_session("ci-session")

                status = core.request_daemon(original_session, "status")
                self.assertTrue(status.get("ok"))
                self.assertEqual("active", status.get("daemon_status"))

                session_path.unlink()
                restored = self.wait_session(session_path, token=original_token, timeout=4.0)
                self.assertEqual(original_token, restored["auth_token"])
                self.assertEqual(original_session["daemon_port"], restored["daemon_port"])
                self.assertEqual(original_session["pid"], restored["pid"])

                restored_session = core.read_session("ci-session")
                status_after_restore = core.request_daemon(restored_session, "status")
                self.assertTrue(status_after_restore.get("ok"))
                self.assertEqual("active", status_after_restore.get("daemon_status"))

                foreign = dict(restored)
                foreign["auth_token"] = "foreign-token"
                session_path.write_text(json.dumps(foreign, ensure_ascii=False, indent=2), encoding="utf-8")
                time.sleep(2.2)
                still_foreign = json.loads(session_path.read_text(encoding="utf-8"))
                self.assertEqual("foreign-token", still_foreign["auth_token"])

                stop_result = core.request_daemon(original_session, "stop")
                self.assertTrue(stop_result.get("ok"))
                thread.join(timeout=5.0)

                self.assertFalse(thread.is_alive(), "daemon не завершился после stop")
                self.assertEqual([], errors)
                self.assertEqual(0, result.get("code"))

                # Cleanup старого daemon не должен удалить регистрацию с чужим токеном.
                final_data = json.loads(session_path.read_text(encoding="utf-8"))
                self.assertEqual("foreign-token", final_data["auth_token"])


if __name__ == "__main__":
    unittest.main()
