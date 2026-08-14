#!/usr/bin/env python3
"""Тестовый daemon с управляемым fake Paramiko для security-тестов."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssh_relay_core as core


CONTROL_DIR = Path(os.environ["SSH_RELAY_SECURITY_CONTROL"])
CONTROL_DIR.mkdir(parents=True, exist_ok=True)
MODE = os.environ.get("SSH_RELAY_SECURITY_MODE", "normal")
SSH_SECRET = os.environ["SSH_RELAY_TEST_SSH_SECRET"]
SUDO_SECRET = os.environ["SSH_RELAY_TEST_SUDO_SECRET"]

HOST_KEYS_LOG = CONTROL_DIR / "host_keys.log"
POLICY_LOG = CONTROL_DIR / "policy.log"
CONNECT_LOG = CONTROL_DIR / "connect.log"
COMMAND_LOG = CONTROL_DIR / "commands.log"
STDIN_LOG = CONTROL_DIR / "stdin.log"
REJECT_RECONNECT = CONTROL_DIR / "reject_reconnect"


def append_line(path: Path, value: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(value + "\n")
        output.flush()


class FakeChannel:
    def __init__(self, transport: "FakeTransport") -> None:
        self.transport = transport
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.exit_code = 0
        self.finished = False

    def exec_command(self, command: str) -> None:
        append_line(COMMAND_LOG, command)

        if command == "sudo -k && sudo -S -p '' -v":
            self.exit_code = 1 if MODE == "sudo-fail" else 0
            if self.exit_code:
                self.stderr.extend(b"sudo: authentication failed\n")
            self.finished = True
            return

        if "test:sudo-success" in command:
            self.stdout.extend(b"sudo-ok\n")
            self.finished = True
            return
        if "test:sudo-exit7" in command:
            self.stderr.extend(b"sudo-failed\n")
            self.exit_code = 7
            self.finished = True
            return
        if "test:sudo-drop" in command:
            self.transport.active = False
            raise OSError("тестовый обрыв SSH во время sudo-команды")
        if command == "test:disconnect-and-reject":
            REJECT_RECONNECT.write_text("1", encoding="utf-8")
            self.stdout.extend(b"disconnecting\n")
            self.finished = True
            self.transport.active = False
            return
        if command == "test:success":
            self.stdout.extend(b"ok\n")
            self.finished = True
            return

        self.stdout.extend(f"executed:{command}\n".encode("utf-8"))
        self.finished = True

    def sendall(self, data: bytes) -> None:
        append_line(STDIN_LOG, data.decode("utf-8", errors="replace").rstrip("\n"))

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv(self, size: int) -> bytes:
        chunk = bytes(self.stdout[:size])
        del self.stdout[:size]
        return chunk

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, size: int) -> bytes:
        chunk = bytes(self.stderr[:size])
        del self.stderr[:size]
        return chunk

    def exit_status_ready(self) -> bool:
        return self.finished

    def recv_exit_status(self) -> int:
        return self.exit_code

    def close(self) -> None:
        pass


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

    def open_session(self, timeout: int = 10) -> FakeChannel:
        if not self.active:
            raise OSError("тестовый SSH transport неактивен")
        return FakeChannel(self)


class FakeSSHClient:
    def __init__(self) -> None:
        self.transport = FakeTransport()

    def load_system_host_keys(self, filename=None) -> None:
        append_line(HOST_KEYS_LOG, "<system>" if filename is None else str(filename))

    def set_missing_host_key_policy(self, policy) -> None:
        append_line(POLICY_LOG, policy.__class__.__name__)

    def connect(self, *args, **kwargs) -> None:
        connect_number = 1
        if CONNECT_LOG.exists():
            connect_number = len(CONNECT_LOG.read_text(encoding="utf-8").splitlines()) + 1
        append_line(CONNECT_LOG, str(connect_number))

        password = kwargs.get("password")
        if MODE == "connect-error-secret":
            self.transport.active = False
            raise OSError(f"тестовая ошибка подключения с секретом {password}")
        if MODE == "host-key-fail":
            self.transport.active = False
            raise OSError("Ключ сервера не совпадает с known_hosts")
        if MODE == "reconnect-host-key" and connect_number > 1 and REJECT_RECONNECT.exists():
            self.transport.active = False
            raise OSError("Ключ сервера изменился при reconnect")
        self.transport.active = True

    def get_transport(self) -> FakeTransport:
        return self.transport

    def close(self) -> None:
        self.transport.active = False


class FakeRejectPolicy:
    pass


class FakeAutoAddPolicy:
    def __init__(self) -> None:
        raise AssertionError("AutoAddPolicy не должен использоваться")


class FakeParamiko:
    SSHClient = FakeSSHClient
    RejectPolicy = FakeRejectPolicy
    AutoAddPolicy = FakeAutoAddPolicy


def fake_getpass(prompt: str) -> str:
    if prompt.startswith("sudo-пароль"):
        return SUDO_SECRET
    return SSH_SECRET


def main() -> int:
    # Сначала подменяем внешнюю SSH-сторону, затем импортируем CLI. Так production
    # install-слои оборачивают fake тем же порядком, что и настоящий Paramiko.
    core.load_paramiko = lambda: FakeParamiko
    core.getpass.getpass = fake_getpass
    core.DEFAULT_RECONNECT_WAIT = 1
    core.RECONNECT_DELAYS = (0.10, 0.10, 0.10)
    core.SSH_MONITOR_INTERVAL = 0.05

    import ssh_relay

    arguments = [
        "daemon",
        "--name",
        "ci-security",
        "--host",
        "198.51.100.42",
        "--user",
        "donpedro",
        "--command-timeout",
        "1",
    ]

    if MODE in {"sudo", "sudo-fail"}:
        arguments.append("--enable-sudo")
    if MODE == "known-hosts":
        known_hosts = CONTROL_DIR / "known_hosts"
        known_hosts.write_text("test fixture\n", encoding="utf-8")
        arguments.extend(["--known-hosts", str(known_hosts)])

    args = ssh_relay.build_parser().parse_args(arguments)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
