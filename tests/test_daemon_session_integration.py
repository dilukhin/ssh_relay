#!/usr/bin/env python3
"""Интеграционный тест self-heal session-файла живого daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Загружаем CLI-расширения до test_transfers: тот модуль устанавливает собственный
# FAKE_CORE, и полный unittest discover не должен зависеть от порядка выполнения тестов.
import ssh_relay  # noqa: F401
import ssh_relay_core as core


ROOT = Path(__file__).resolve().parents[1]
DAEMON_RUNNER = Path(__file__).with_name("daemon_fake_runner.py")


class DaemonSessionIntegrationTests(unittest.TestCase):
    def collect_process_diagnostics(self, process: subprocess.Popen[str], port: int | None = None) -> str:
        netstat_lines: list[str] = []
        if os.name == "nt" and port is not None:
            try:
                netstat = subprocess.run(
                    ["netstat", "-ano"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=5,
                    check=False,
                ).stdout
                needle = f":{port}"
                netstat_lines = [line for line in netstat.splitlines() if needle in line or str(process.pid) in line]
            except Exception as exc:  # pragma: no cover - только диагностика CI
                netstat_lines = [f"netstat недоступен: {exc}"]

        if process.poll() is None:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5.0)
        return (
            f"PID subprocess: {process.pid}; код завершения: {process.returncode}; port: {port}\n"
            f"netstat:\n" + "\n".join(netstat_lines) + "\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def fail_if_process_exited(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            return
        stdout, stderr = process.communicate()
        self.fail(
            f"Тестовый daemon завершился раньше времени, код {process.returncode}.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def wait_session(
        self,
        path: Path,
        *,
        process: subprocess.Popen[str],
        token: str | None = None,
        timeout: float = 5.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.fail_if_process_exited(process)
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

    def wait_status(
        self,
        session: dict,
        *,
        process: subprocess.Popen[str],
        timeout: float = 5.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.fail_if_process_exited(process)
            try:
                result = core.request_daemon(session, "status", response_timeout=1)
                if result.get("ok") and result.get("daemon_status") == "active":
                    return result
            except core.RelayError as exc:
                last_error = exc
            time.sleep(0.05)
        diagnostics = self.collect_process_diagnostics(process, int(session["daemon_port"]))
        if last_error is not None:
            raise AssertionError(
                f"Control-plane daemon не стал доступен: {last_error}\n{diagnostics}"
            ) from last_error
        raise AssertionError(f"Control-plane daemon не подтвердил активное состояние.\n{diagnostics}")

    def test_live_daemon_restores_missing_session_without_overwriting_foreign_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = {
                "LOCALAPPDATA": tmp,
                "XDG_STATE_HOME": tmp,
            }
            child_env = os.environ.copy()
            child_env.update(overrides)

            with patch.dict(os.environ, overrides, clear=False):
                process = subprocess.Popen(
                    [sys.executable, "-u", str(DAEMON_RUNNER)],
                    cwd=str(ROOT),
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                original_session: dict | None = None
                streams_collected = False
                try:
                    session_path = core.session_file_path("ci-session")
                    session_data = self.wait_session(session_path, process=process)
                    self.assertEqual(process.pid, int(session_data["pid"]))
                    original_token = str(session_data["auth_token"])
                    original_session = core.read_session("ci-session")

                    status = self.wait_status(original_session, process=process)
                    self.assertEqual("active", status.get("daemon_status"))

                    session_path.unlink()
                    restored = self.wait_session(
                        session_path,
                        process=process,
                        token=original_token,
                        timeout=4.0,
                    )
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
                    self.fail_if_process_exited(process)
                    still_foreign = json.loads(session_path.read_text(encoding="utf-8"))
                    self.assertEqual("foreign-token", still_foreign["auth_token"])

                    stop_result = core.request_daemon(original_session, "stop")
                    self.assertTrue(stop_result.get("ok"))
                    stdout, stderr = process.communicate(timeout=5.0)
                    streams_collected = True
                    self.assertEqual(0, process.returncode, stdout + stderr)

                    # Cleanup старого daemon не должен удалить регистрацию с чужим токеном.
                    final_data = json.loads(session_path.read_text(encoding="utf-8"))
                    self.assertEqual("foreign-token", final_data["auth_token"])
                finally:
                    if process.poll() is None:
                        if original_session is not None:
                            try:
                                core.request_daemon(original_session, "stop", response_timeout=2)
                            except core.RelayError:
                                pass
                        try:
                            process.communicate(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            process.terminate()
                            process.communicate(timeout=5.0)
                    elif not streams_collected:
                        process.communicate()


if __name__ == "__main__":
    unittest.main()
