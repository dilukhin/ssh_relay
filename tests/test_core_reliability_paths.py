#!/usr/bin/env python3
"""Тесты отказов и cleanup для core transport, daemon startup и legacy SFTP."""

from __future__ import annotations

import argparse
import contextlib
import io
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import ssh_relay
import ssh_relay_core as core
import ssh_relay_transfers as transfers


class _Transport:
    def __init__(self, *, active: bool = True, authenticated: bool = True) -> None:
        self.active = active
        self.authenticated = authenticated
        self.keepalive: int | None = None

    def is_active(self) -> bool:
        return self.active

    def is_authenticated(self) -> bool:
        return self.authenticated

    def set_keepalive(self, seconds: int) -> None:
        self.keepalive = seconds


class _SSHClient:
    def __init__(self, *, transport: _Transport | None = None, connect_error: Exception | None = None) -> None:
        self.transport = transport
        self.connect_error = connect_error
        self.closed = False

    def load_system_host_keys(self, filename=None) -> None:
        pass

    def set_missing_host_key_policy(self, policy) -> None:
        pass

    def connect(self, *args, **kwargs) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def get_transport(self):
        return self.transport

    def close(self) -> None:
        self.closed = True
        if self.transport is not None:
            self.transport.active = False


class _RejectPolicy:
    pass


class _Paramiko:
    RejectPolicy = _RejectPolicy

    def __init__(self, client: _SSHClient) -> None:
        self.client = client

    def SSHClient(self) -> _SSHClient:
        return self.client


class _ServerSocket:
    def __init__(self, *, bind_error: Exception | None = None) -> None:
        self.bind_error = bind_error
        self.closed = False

    def setsockopt(self, *_args) -> None:
        pass

    def bind(self, _address) -> None:
        if self.bind_error is not None:
            raise self.bind_error

    def listen(self, _backlog: int) -> None:
        pass

    def settimeout(self, _timeout: float) -> None:
        pass

    def getsockname(self):
        return ("127.0.0.1", 41000)

    def close(self) -> None:
        self.closed = True


class _TimeoutChannel:
    def __init__(self) -> None:
        self.closed = False

    def exec_command(self, _command: str) -> None:
        pass

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return False

    def recv_stderr_ready(self) -> bool:
        return False

    def exit_status_ready(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class _ChannelTransport:
    def __init__(self, channel: _TimeoutChannel) -> None:
        self.channel = channel

    def open_session(self, timeout: int = 10):
        return self.channel


class _CommandClient:
    def __init__(self, channel: _TimeoutChannel) -> None:
        self.transport = _ChannelTransport(channel)

    def get_transport(self):
        return self.transport


class _FailingReader:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self, _size: int) -> bytes:
        raise OSError("read failed")


class _Writer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.data = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def write(self, data: bytes) -> None:
        if self.fail:
            raise OSError("write failed")
        self.data.extend(data)

    def flush(self) -> None:
        pass


class CoreTransportReliabilityTests(unittest.TestCase):
    def test_request_daemon_wraps_local_connection_failure(self) -> None:
        session = {"auth_token": "token", "daemon_port": 9}
        with patch.object(core.socket, "create_connection", side_effect=ConnectionRefusedError("refused")):
            with self.assertRaises(core.DaemonUnavailableError) as captured:
                core.request_daemon(session, "status")
        self.assertIn("Daemon недоступен", str(captured.exception))
        self.assertIsInstance(captured.exception.__cause__, ConnectionRefusedError)

    def test_execute_remote_command_timeout_closes_channel(self) -> None:
        channel = _TimeoutChannel()
        with patch.object(core.time, "monotonic", side_effect=[100.0, 102.0]), patch.object(core.time, "sleep"):
            with self.assertRaisesRegex(core.RelayError, "Превышено время выполнения команды"):
                core.execute_remote_command(_CommandClient(channel), "sleep 10", 1)
        self.assertTrue(channel.closed)


class DaemonStartupReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ssh_relay.build_parser()

    def args(self, *extra: str) -> argparse.Namespace:
        return self.parser.parse_args([
            "daemon", "--name", "ci-reliability", "--host", "198.51.100.42", "--user", "donpedro", *extra
        ])

    def test_daemon_rejects_invalid_passphrase_combination_and_missing_identity(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, ssh_relay.daemon(self.args("--ask-key-passphrase")))
        with patch.object(core, "check_existing_session", return_value=False), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, ssh_relay.daemon(self.args("--identity-file", "definitely-missing-key")))

    def test_daemon_reports_paramiko_load_failure(self) -> None:
        with patch.object(core, "check_existing_session", return_value=False), patch.object(
            core, "load_paramiko", side_effect=core.RelayError("paramiko unavailable")
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(1, ssh_relay.daemon(self.args()))
        self.assertIn("paramiko unavailable", stderr.getvalue())

    def test_daemon_connect_failure_closes_ssh_client(self) -> None:
        client = _SSHClient(transport=_Transport(), connect_error=OSError("connect failed"))
        with patch.object(core, "check_existing_session", return_value=False), patch.object(
            core, "load_paramiko", return_value=_Paramiko(client)
        ), patch.object(core.getpass, "getpass", return_value="test-secret"), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(1, ssh_relay.daemon(self.args()))
        self.assertTrue(client.closed)
        self.assertIn("Не удалось установить SSH-соединение", stderr.getvalue())

    def test_daemon_rejects_inactive_transport_and_closes_client(self) -> None:
        client = _SSHClient(transport=_Transport(active=False, authenticated=False))
        with patch.object(core, "check_existing_session", return_value=False), patch.object(
            core, "load_paramiko", return_value=_Paramiko(client)
        ), patch.object(core.getpass, "getpass", return_value="test-secret"), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(1, ssh_relay.daemon(self.args()))
        self.assertTrue(client.closed)
        self.assertIn("не перешёл в активное аутентифицированное состояние", stderr.getvalue())

    def test_daemon_sudo_verification_failure_closes_client(self) -> None:
        client = _SSHClient(transport=_Transport())
        with patch.object(core, "check_existing_session", return_value=False), patch.object(
            core, "load_paramiko", return_value=_Paramiko(client)
        ), patch.object(core.getpass, "getpass", return_value="test-secret"), patch.object(
            core, "verify_sudo_password", side_effect=core.RelayError("sudo rejected")
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(1, ssh_relay.daemon(self.args("--enable-sudo")))
        self.assertTrue(client.closed)
        self.assertIn("sudo rejected", stderr.getvalue())

    def test_daemon_bind_failure_closes_local_and_ssh_resources(self) -> None:
        client = _SSHClient(transport=_Transport())
        server = _ServerSocket(bind_error=OSError("bind failed"))
        with patch.object(core, "check_existing_session", return_value=False), patch.object(
            core, "load_paramiko", return_value=_Paramiko(client)
        ), patch.object(core.getpass, "getpass", return_value="test-secret"), patch.object(
            core.socket, "socket", return_value=server
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(1, ssh_relay.daemon(self.args()))
        self.assertTrue(server.closed)
        self.assertTrue(client.closed)
        self.assertIn("Не удалось открыть локальный порт relay", stderr.getvalue())

    def test_daemon_session_write_failure_closes_resources(self) -> None:
        client = _SSHClient(transport=_Transport())
        server = _ServerSocket()
        with patch.object(core, "check_existing_session", return_value=False), patch.object(
            core, "load_paramiko", return_value=_Paramiko(client)
        ), patch.object(core.getpass, "getpass", return_value="test-secret"), patch.object(
            core.socket, "socket", return_value=server
        ), patch.object(core, "write_session", side_effect=OSError("disk full")), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(1, ssh_relay.daemon(self.args()))
        self.assertTrue(server.closed)
        self.assertTrue(client.closed)
        self.assertIn("Не удалось безопасно записать файл сессии", stderr.getvalue())


class DetachedDaemonReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ssh_relay.build_parser()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "daemon.log"

    def args(self, *extra: str) -> argparse.Namespace:
        return self.parser.parse_args([
            "daemon", "--name", "ci-detached", "--host", "198.51.100.42", "--user", "donpedro",
            "--detach", "--identity-file", "id_ed25519", "--detach-log", str(self.log_path), *extra
        ])

    def test_detached_rejects_interactive_modes(self) -> None:
        no_key = self.parser.parse_args([
            "daemon", "--name", "ci-detached", "--host", "198.51.100.42", "--user", "donpedro", "--detach"
        ])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, core.start_detached_daemon(no_key))
            self.assertEqual(2, core.start_detached_daemon(self.args("--ask-key-passphrase")))
            self.assertEqual(2, core.start_detached_daemon(self.args("--enable-sudo")))

    def test_detached_success_confirms_active_session(self) -> None:
        session = {"user": "donpedro", "host": "198.51.100.42", "port": 22}
        with patch.object(core, "check_existing_session", return_value=False), patch.object(
            core.subprocess, "Popen"
        ) as popen, patch.object(core, "read_session", return_value=session), patch.object(
            core, "request_daemon", return_value={"ok": True, "status": "active"}
        ), patch.object(core.time, "monotonic", side_effect=[100.0, 100.1]), contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, core.start_detached_daemon(self.args("--known-hosts", "known_hosts")))
        command = popen.call_args.args[0]
        self.assertIn("--known-hosts", command)
        self.assertIn("known_hosts", command)
        self.assertIn("запущена в фоне", stdout.getvalue())

    def test_detached_timeout_does_not_claim_success(self) -> None:
        with patch.object(core, "check_existing_session", return_value=False), patch.object(
            core.subprocess, "Popen"
        ), patch.object(core.time, "monotonic", side_effect=[100.0, 116.0]), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(1, core.start_detached_daemon(self.args()))
        self.assertIn("Не удалось подтвердить запуск detached daemon", stderr.getvalue())


class LegacySftpReliabilityTests(unittest.TestCase):
    @property
    def legacy_download(self):
        self.assertIsNotNone(transfers._legacy_download)
        return transfers._legacy_download

    @property
    def legacy_upload(self):
        self.assertIsNotNone(transfers._legacy_upload)
        return transfers._legacy_upload

    def test_legacy_download_open_and_stat_failures_are_clear(self) -> None:
        client = MagicMock()
        client.open_sftp.side_effect = OSError("channel failed")
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "file.bin")
            with self.assertRaisesRegex(core.RelayError, "Не удалось открыть SFTP-канал"):
                self.legacy_download(client, "/remote/file", target, overwrite=False, create_dirs=False, max_size=1024, timeout_seconds=5)

        sftp = MagicMock()
        sftp.stat.side_effect = OSError("missing")
        client.open_sftp.side_effect = None
        client.open_sftp.return_value = sftp
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "file.bin")
            with self.assertRaisesRegex(core.RelayError, "Удалённый файл не найден"):
                self.legacy_download(client, "/remote/file", target, overwrite=False, create_dirs=False, max_size=1024, timeout_seconds=5)
        sftp.close.assert_called_once()

    def test_legacy_download_rejects_directory_and_oversize(self) -> None:
        client = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "file.bin")
            for attrs, message in (
                (SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=0), "указывает на каталог"),
                (SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=2048), "превышает лимит"),
            ):
                with self.subTest(message=message):
                    sftp = MagicMock()
                    sftp.stat.return_value = attrs
                    client.open_sftp.return_value = sftp
                    with self.assertRaisesRegex(core.RelayError, message):
                        self.legacy_download(client, "/remote/file", target, overwrite=False, create_dirs=False, max_size=1024, timeout_seconds=5)
                    sftp.close.assert_called_once()

    def test_legacy_download_read_failure_removes_temporary_file(self) -> None:
        client = MagicMock()
        sftp = MagicMock()
        sftp.stat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=1)
        sftp.open.return_value = _FailingReader()
        client.open_sftp.return_value = sftp
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "file.bin"
            with self.assertRaisesRegex(core.RelayError, "Ошибка при скачивании или записи файла"):
                self.legacy_download(client, "/remote/file", str(target), overwrite=False, create_dirs=False, max_size=1024, timeout_seconds=5)
            self.assertFalse(target.exists())
            self.assertEqual([], list(root.glob(".*.ssh-relay-*.tmp")))
        sftp.close.assert_called_once()

    def test_ensure_remote_directory_rejects_file_and_mkdir_failure(self) -> None:
        sftp = MagicMock()
        sftp.stat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o644)
        with self.assertRaisesRegex(core.RelayError, "не является каталогом"):
            core.ensure_remote_directory(sftp, "/var/data")

        sftp = MagicMock()
        sftp.stat.side_effect = OSError("missing")
        sftp.mkdir.side_effect = OSError("denied")
        with self.assertRaisesRegex(core.RelayError, "Не удалось создать удалённый каталог"):
            core.ensure_remote_directory(sftp, "/var/data")

    def test_legacy_upload_parent_and_existing_target_errors_are_clear(self) -> None:
        client = MagicMock()
        sftp = MagicMock()
        sftp.stat.side_effect = OSError("missing parent")
        client.open_sftp.return_value = sftp
        with self.assertRaisesRegex(core.RelayError, "каталог назначения не существует"):
            self.legacy_upload(client, "C:/file.bin", b"x", "/missing/file.bin", overwrite=False, create_dirs=False, max_size=1024, timeout_seconds=5)
        sftp.close.assert_called_once()

        sftp = MagicMock()
        sftp.stat.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR | 0o755), SimpleNamespace(st_mode=stat.S_IFREG | 0o644)]
        client.open_sftp.return_value = sftp
        with self.assertRaisesRegex(core.RelayError, "Удалённый файл уже существует"):
            self.legacy_upload(client, "C:/file.bin", b"x", "/existing/file.bin", overwrite=False, create_dirs=False, max_size=1024, timeout_seconds=5)
        sftp.close.assert_called_once()

    def test_legacy_upload_write_failure_attempts_partial_cleanup(self) -> None:
        client = MagicMock()
        sftp = MagicMock()
        sftp.stat.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR | 0o755), OSError("target missing"), OSError("temporary missing")]
        sftp.open.return_value = _Writer(fail=True)
        client.open_sftp.return_value = sftp
        with patch.object(core, "remote_temporary_path", return_value="/remote/.file.tmp"):
            with self.assertRaisesRegex(core.RelayError, "Ошибка при чтении или загрузке файла"):
                self.legacy_upload(client, "C:/file.bin", b"payload", "/remote/file.bin", overwrite=False, create_dirs=False, max_size=1024, timeout_seconds=5)
        sftp.remove.assert_called_with("/remote/.file.tmp")
        sftp.close.assert_called_once()

    def test_legacy_upload_overwrite_rename_fallback_finishes_and_cleans_up(self) -> None:
        client = MagicMock()
        sftp = MagicMock()
        sftp.stat.side_effect = [
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o755), SimpleNamespace(st_mode=stat.S_IFREG | 0o644),
            OSError("temporary missing"), SimpleNamespace(st_mode=stat.S_IFREG | 0o644),
        ]
        sftp.open.return_value = _Writer()
        sftp.posix_rename.side_effect = OSError("extension unavailable")
        client.open_sftp.return_value = sftp
        with patch.object(core, "remote_temporary_path", return_value="/remote/.file.tmp"):
            result = self.legacy_upload(client, "C:/file.bin", b"payload", "/remote/file.bin", overwrite=True, create_dirs=False, max_size=1024, timeout_seconds=5)
        self.assertTrue(result["ok"])
        self.assertEqual(len(b"payload"), result["bytes_uploaded"])
        sftp.rename.assert_called_once_with("/remote/.file.tmp", "/remote/file.bin")
        self.assertTrue(sftp.close.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
