#!/usr/bin/env python3
"""Локальный Paramiko SSH/SFTP-server для интеграционных тестов relay."""

from __future__ import annotations

import errno
import os
import posixpath
import socket
import threading
import time
from pathlib import Path, PurePosixPath

import paramiko


class _RelayTestServer(paramiko.ServerInterface):
    def __init__(self, owner: "LoopbackSSHServer") -> None:
        self.owner = owner

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_auth_password(self, username: str, password: str) -> int:
        self.owner.record_auth(username, password)
        if username == self.owner.username and password == self.owner.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel: paramiko.Channel, command: bytes | str) -> bool:
        if isinstance(command, bytes):
            text = command.decode("utf-8", errors="replace")
        else:
            text = command
        self.owner.record_command(text)
        threading.Thread(
            target=self.owner.execute_command,
            args=(channel, text),
            name="ssh-relay-test-exec",
            daemon=True,
        ).start()
        return True


class _RootedSFTPServer(paramiko.SFTPServerInterface):
    """Минимальный SFTP-интерфейс, жёстко ограниченный временным каталогом теста."""

    def __init__(self, server: paramiko.ServerInterface, *, root: str) -> None:
        super().__init__(server)
        self.root = Path(root).resolve()

    @staticmethod
    def _error(exc: OSError) -> int:
        return paramiko.SFTPServer.convert_errno(exc.errno or errno.EIO)

    def _local(self, path: str) -> Path:
        normalized = posixpath.normpath("/" + str(path).replace("\\", "/").lstrip("/"))
        relative = PurePosixPath(normalized.lstrip("/"))
        candidate = self.root.joinpath(*relative.parts).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise PermissionError(errno.EACCES, "Выход за корень тестового SFTP запрещён", path)
        return candidate

    def canonicalize(self, path: str) -> str:
        return posixpath.normpath("/" + str(path).replace("\\", "/").lstrip("/"))

    def stat(self, path: str):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._local(path)))
        except OSError as exc:
            return self._error(exc)

    def lstat(self, path: str):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._local(path)))
        except OSError as exc:
            return self._error(exc)

    def open(self, path: str, flags: int, attr: paramiko.SFTPAttributes):
        try:
            local = self._local(path)
            mode = int(getattr(attr, "st_mode", 0) or 0o666)
            fd = os.open(local, flags, mode)
            if flags & os.O_RDWR:
                file_mode = "r+b"
            elif flags & os.O_WRONLY:
                file_mode = "ab" if flags & os.O_APPEND else "wb"
            else:
                file_mode = "rb"
            stream = os.fdopen(fd, file_mode, buffering=0)
        except OSError as exc:
            return self._error(exc)

        handle = paramiko.SFTPHandle(flags)
        handle.filename = str(local)
        if flags & os.O_RDWR:
            handle.readfile = stream
            handle.writefile = stream
        elif flags & os.O_WRONLY:
            handle.writefile = stream
        else:
            handle.readfile = stream
        return handle

    def mkdir(self, path: str, attr: paramiko.SFTPAttributes) -> int:
        try:
            mode = int(getattr(attr, "st_mode", 0) or 0o777)
            os.mkdir(self._local(path), mode)
            return paramiko.SFTP_OK
        except OSError as exc:
            return self._error(exc)

    def rmdir(self, path: str) -> int:
        try:
            os.rmdir(self._local(path))
            return paramiko.SFTP_OK
        except OSError as exc:
            return self._error(exc)

    def remove(self, path: str) -> int:
        try:
            os.remove(self._local(path))
            return paramiko.SFTP_OK
        except OSError as exc:
            return self._error(exc)

    def rename(self, oldpath: str, newpath: str) -> int:
        try:
            source = self._local(oldpath)
            target = self._local(newpath)
            if target.exists():
                return paramiko.SFTP_FAILURE
            os.rename(source, target)
            return paramiko.SFTP_OK
        except OSError as exc:
            return self._error(exc)

    def posix_rename(self, oldpath: str, newpath: str) -> int:
        try:
            os.replace(self._local(oldpath), self._local(newpath))
            return paramiko.SFTP_OK
        except OSError as exc:
            return self._error(exc)


class LoopbackSSHServer:
    """Минимальный SSH-server с password auth, exec и опциональным rooted SFTP."""

    def __init__(
        self,
        host_key: paramiko.PKey,
        *,
        username: str = "donpedro",
        password: str = "relay-test-password",
        sftp_root: Path | None = None,
    ) -> None:
        self.host_key = host_key
        self.username = username
        self.password = password
        self.sftp_root = sftp_root.resolve() if sftp_root is not None else None
        if self.sftp_root is not None:
            self.sftp_root.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._transports: list[paramiko.Transport] = []
        self._lock = threading.Lock()
        self._auth_attempts: list[tuple[str, str]] = []
        self._commands: list[str] = []
        self.port = 0
        self.connection_count = 0

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(0.2)
        self._listener = listener
        self.port = int(listener.getsockname()[1])
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="ssh-relay-test-accept",
            daemon=True,
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        self.drop_all_transports()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
        for worker in list(self._workers):
            worker.join(timeout=2)

    def write_known_hosts(self, path: Path, *, key: paramiko.PKey | None = None) -> None:
        host_keys = paramiko.HostKeys()
        selected = key if key is not None else self.host_key
        host_keys.add(f"[127.0.0.1]:{self.port}", selected.get_name(), selected)
        host_keys.save(str(path))

    def record_auth(self, username: str, password: str) -> None:
        with self._lock:
            self._auth_attempts.append((username, password))

    def record_command(self, command: str) -> None:
        with self._lock:
            self._commands.append(command)

    @property
    def auth_attempts(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._auth_attempts)

    @property
    def commands(self) -> list[str]:
        with self._lock:
            return list(self._commands)

    def drop_all_transports(self) -> None:
        with self._lock:
            transports = list(self._transports)
        for transport in transports:
            try:
                transport.close()
            except Exception:
                pass

    def wait_for_connections(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self.connection_count >= count:
                    return True
            time.sleep(0.02)
        return False

    def execute_command(self, channel: paramiko.Channel, command: str) -> None:
        try:
            # Небольшая задержка даёт Paramiko завершить подтверждение exec request
            # прежде, чем тестовый server отправит exit-status и EOF.
            time.sleep(0.01)
            if command == "test:real-success":
                channel.sendall(b"real-stdout\n")
                channel.send_stderr(b"real-stderr\n")
                exit_code = 0
            elif command == "test:real-exit7":
                channel.send_stderr(b"real-failed\n")
                exit_code = 7
            elif command == "test:real-empty":
                exit_code = 0
            else:
                channel.sendall(f"executed:{command}\n".encode("utf-8"))
                exit_code = 0
            channel.send_exit_status(exit_code)
        finally:
            try:
                channel.shutdown_write()
            except Exception:
                pass
            time.sleep(0.01)
            try:
                channel.close()
            except Exception:
                pass

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                client_socket, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(
                target=self._serve_connection,
                args=(client_socket,),
                name="ssh-relay-test-connection",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _serve_connection(self, client_socket: socket.socket) -> None:
        transport = paramiko.Transport(client_socket)
        with self._lock:
            self._transports.append(transport)
            self.connection_count += 1
        try:
            transport.add_server_key(self.host_key)
            if self.sftp_root is not None:
                transport.set_subsystem_handler(
                    "sftp",
                    paramiko.SFTPServer,
                    _RootedSFTPServer,
                    root=str(self.sftp_root),
                )
            transport.start_server(server=_RelayTestServer(self))
            while not self._stop.is_set() and transport.is_active():
                channel = transport.accept(timeout=0.2)
                if channel is None:
                    continue
                while not self._stop.is_set() and transport.is_active() and not channel.closed:
                    time.sleep(0.01)
        except Exception:
            # Ошибка negotiation ожидаема, например когда клиент отверг host key.
            pass
        finally:
            try:
                transport.close()
            except Exception:
                pass
            try:
                client_socket.close()
            except OSError:
                pass
            with self._lock:
                try:
                    self._transports.remove(transport)
                except ValueError:
                    pass
