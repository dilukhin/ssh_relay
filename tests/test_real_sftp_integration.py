#!/usr/bin/env python3
"""Интеграционные тесты upload/download через настоящий loopback SFTP."""

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

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ssh_relay_core as core
from localhost_ssh_server import LoopbackSSHServer


ROOT = Path(__file__).resolve().parents[1]
DAEMON_RUNNER = Path(__file__).with_name("daemon_real_ssh_runner.py")
PASSWORD = "relay-test-password"
SESSION_NAME = "ci-real-ssh"


class RealSFTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.remote_root = self.root / "remote"
        self.local_root = self.root / "local"
        self.state.mkdir()
        self.local_root.mkdir()
        self.known_hosts = self.root / "known_hosts"

        self.server = LoopbackSSHServer(
            self.host_key,
            password=PASSWORD,
            sftp_root=self.remote_root,
        )
        self.server.start()
        self.server.write_known_hosts(self.known_hosts)

        self.overrides = {
            "LOCALAPPDATA": str(self.state),
            "XDG_STATE_HOME": str(self.state),
        }
        self.env_patcher = patch.dict(os.environ, self.overrides, clear=False)
        self.env_patcher.start()
        self.process: subprocess.Popen[str] | None = None
        self.session: dict | None = None
        self.start_daemon()

    def tearDown(self) -> None:
        try:
            self.stop_daemon()
        finally:
            self.server.stop()
            self.env_patcher.stop()
            self.tmp.cleanup()

    def child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.overrides)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def start_daemon(self) -> None:
        env = self.child_env()
        env["SSH_RELAY_REAL_SSH_PASSWORD"] = PASSWORD
        env["SSH_RELAY_REAL_SSH_PORT"] = str(self.server.port)
        env["SSH_RELAY_REAL_KNOWN_HOSTS"] = str(self.known_hosts)
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(DAEMON_RUNNER)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.session = self.wait_session()
        self.wait_connected()

    def stop_daemon(self) -> tuple[str, str]:
        if self.process is None:
            return "", ""
        process = self.process
        if process.poll() is None and self.session is not None:
            try:
                core.request_daemon(self.session, "stop", response_timeout=2)
            except core.RelayError:
                pass
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=3)
        self.process = None
        self.session = None
        return stdout, stderr

    def fail_if_exited(self) -> None:
        if self.process is None or self.process.poll() is None:
            return
        stdout, stderr = self.process.communicate()
        self.fail(
            f"Real-SFTP daemon завершился раньше времени, код {self.process.returncode}.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def wait_session(self, timeout: float = 7.0) -> dict:
        path = core.session_file_path(SESSION_NAME)
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.fail_if_exited()
            try:
                json.loads(path.read_text(encoding="utf-8"))
                return core.read_session(SESSION_NAME)
            except (OSError, json.JSONDecodeError, core.RelayError) as exc:
                last_error = exc
            time.sleep(0.02)
        if last_error is not None:
            raise AssertionError(f"Session-файл real-SFTP daemon не стал доступен: {last_error}") from last_error
        raise AssertionError("Session-файл real-SFTP daemon не появился.")

    def wait_connected(self, timeout: float = 7.0) -> None:
        assert self.session is not None
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            self.fail_if_exited()
            last = core.request_daemon(self.session, "status", response_timeout=2)
            if last.get("ok") and last.get("ssh_status") == "connected":
                return
            time.sleep(0.03)
        self.fail(f"Real SFTP SSH transport не перешёл в connected: {last}")

    def run_cli(self, *arguments: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        self.fail_if_exited()
        return subprocess.run(
            [sys.executable, str(ROOT / "ssh_relay.py"), *arguments],
            cwd=str(ROOT),
            env=self.child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def payload() -> bytes:
        # Чуть больше 1 МиБ, чтобы новый transfer-протокол выполнил минимум два чанка.
        return bytes(range(256)) * 4097 + b"ssh-relay-real-sftp-tail"

    def test_real_sftp_multichunk_upload_download_roundtrip(self) -> None:
        payload = self.payload()
        source = self.local_root / "source.bin"
        source.write_bytes(payload)
        remote = self.remote_root / "nested" / "roundtrip.bin"

        without_dirs = self.run_cli(
            "upload",
            "--name",
            SESSION_NAME,
            str(source),
            "/nested/roundtrip.bin",
        )
        self.assertEqual(1, without_dirs.returncode, without_dirs.stdout + without_dirs.stderr)
        self.assertFalse((self.remote_root / "nested").exists())

        upload = self.run_cli(
            "upload",
            "--name",
            SESSION_NAME,
            "--create-dirs",
            str(source),
            "/nested/roundtrip.bin",
        )
        self.assertEqual(0, upload.returncode, upload.stdout + upload.stderr)
        self.assertEqual(payload, remote.read_bytes())
        self.assertFalse((remote.parent / ".roundtrip.bin.ssh-relay.part").exists())
        self.assertIn("Загружено:", upload.stdout)

        target = self.local_root / "downloaded.bin"
        download = self.run_cli(
            "download",
            "--name",
            SESSION_NAME,
            "/nested/roundtrip.bin",
            str(target),
        )
        self.assertEqual(0, download.returncode, download.stdout + download.stderr)
        self.assertEqual(payload, target.read_bytes())
        self.assertFalse((target.parent / ".downloaded.bin.ssh-relay.part").exists())
        self.assertIn("Скачано:", download.stdout)

    def test_real_sftp_upload_overwrite_uses_safe_replace(self) -> None:
        source = self.local_root / "replace-source.bin"
        source.write_bytes(b"new-content")
        remote = self.remote_root / "replace.bin"
        remote.write_bytes(b"old-content")

        refused = self.run_cli(
            "upload",
            "--name",
            SESSION_NAME,
            str(source),
            "/replace.bin",
        )
        self.assertEqual(1, refused.returncode, refused.stdout + refused.stderr)
        self.assertEqual(b"old-content", remote.read_bytes())
        self.assertFalse((self.remote_root / ".replace.bin.ssh-relay.part").exists())

        replaced = self.run_cli(
            "upload",
            "--name",
            SESSION_NAME,
            "--overwrite",
            str(source),
            "/replace.bin",
        )
        self.assertEqual(0, replaced.returncode, replaced.stdout + replaced.stderr)
        self.assertEqual(b"new-content", remote.read_bytes())
        self.assertFalse((self.remote_root / ".replace.bin.ssh-relay.part").exists())

    def test_real_sftp_existing_partial_requires_explicit_discard(self) -> None:
        source = self.local_root / "resume-source.bin"
        source.write_bytes(b"complete-new-file")
        partial = self.remote_root / ".resume.bin.ssh-relay.part"
        partial.write_bytes(b"old-partial")

        refused = self.run_cli(
            "upload",
            "--name",
            SESSION_NAME,
            str(source),
            "/resume.bin",
        )
        self.assertEqual(1, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("Найден частичный удалённый файл", refused.stderr)
        self.assertEqual(b"old-partial", partial.read_bytes())
        self.assertFalse((self.remote_root / "resume.bin").exists())

        restarted = self.run_cli(
            "upload",
            "--name",
            SESSION_NAME,
            "--discard-partial",
            str(source),
            "/resume.bin",
        )
        self.assertEqual(0, restarted.returncode, restarted.stdout + restarted.stderr)
        self.assertEqual(b"complete-new-file", (self.remote_root / "resume.bin").read_bytes())
        self.assertFalse(partial.exists())

    def test_real_sftp_download_overwrite_preserves_existing_until_authorized(self) -> None:
        remote = self.remote_root / "download.bin"
        remote.write_bytes(b"remote-new")
        target = self.local_root / "download-target.bin"
        target.write_bytes(b"local-old")

        refused = self.run_cli(
            "download",
            "--name",
            SESSION_NAME,
            "/download.bin",
            str(target),
        )
        self.assertEqual(1, refused.returncode, refused.stdout + refused.stderr)
        self.assertEqual(b"local-old", target.read_bytes())

        replaced = self.run_cli(
            "download",
            "--name",
            SESSION_NAME,
            "--overwrite",
            "/download.bin",
            str(target),
        )
        self.assertEqual(0, replaced.returncode, replaced.stdout + replaced.stderr)
        self.assertEqual(b"remote-new", target.read_bytes())


if __name__ == "__main__":
    unittest.main()
