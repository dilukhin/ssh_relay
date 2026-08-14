#!/usr/bin/env python3
"""Тестовый daemon с управляемым fake Paramiko для exec/reconnect."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssh_relay
import ssh_relay_core as core


CONTROL_DIR = Path(os.environ["SSH_RELAY_FAKE_CONTROL"])
CONTROL_DIR.mkdir(parents=True, exist_ok=True)
CONNECT_LOG = CONTROL_DIR / "connect.log"
COMMAND_LOG = CONTROL_DIR / "commands.log"
RECONNECT_FAILURES = CONTROL_DIR / "reconnect_failures.txt"


def append_line(path: Path, value: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(value + "\n")
        output.flush()


def read_reconnect_failures() -> int:
    try:
        return max(0, int(RECONNECT_FAILURES.read_text(encoding="utf-8").strip() or "0"))
    except (OSError, ValueError):
        return 0


def consume_reconnect_failure() -> bool:
    remaining = read_reconnect_failures()
    if remaining <= 0:
        return False
    RECONNECT_FAILURES.write_text(str(remaining - 1), encoding="utf-8")
    return True


class FakeChannel:
    def __init__(self, transport: "FakeTransport") -> None:
        self.transport = transport
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.exit_code = 0
        self.finished = False
        self.closed = False

    def exec_command(self, command: str) -> None:
        append_line(COMMAND_LOG, command)

        if command == "test:success":
            self.stdout.extend(b"stdout-ok\n")
            self.finished = True
            return
        if command == "test:mixed":
            self.stdout.extend(b"stdout-ok\n")
            self.stderr.extend(b"stderr-ok\n")
            self.finished = True
            return
        if command == "test:exit7":
            self.stderr.extend(b"failed\n")
            self.exit_code = 7
            self.finished = True
            return
        if command == "test:empty":
            self.finished = True
            return
        if command == "test:disconnect-after-success":
            self.stdout.extend(b"disconnecting\n")
            self.finished = True
            self.transport.active = False
            return
        if command == "test:drop-during":
            self.transport.active = False
            raise OSError("тестовый обрыв SSH во время команды")
        if command == "test:hang":
            return

        self.stdout.extend(f"executed:{command}\n".encode("utf-8"))
        self.finished = True

    def sendall(self, data: bytes) -> None:
        pass

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
        self.closed = True


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
        pass

    def set_missing_host_key_policy(self, policy) -> None:
        pass

    def connect(self, *args, **kwargs) -> None:
        connect_number = 1
        if CONNECT_LOG.exists():
            connect_number = len(CONNECT_LOG.read_text(encoding="utf-8").splitlines()) + 1
        append_line(CONNECT_LOG, str(connect_number))

        if connect_number > 1 and consume_reconnect_failure():
            self.transport.active = False
            raise OSError("тестовый отказ reconnect")
        self.transport.active = True

    def get_transport(self) -> FakeTransport:
        return self.transport

    def close(self) -> None:
        self.transport.active = False


class FakeRejectPolicy:
    pass


class FakeParamiko:
    SSHClient = FakeSSHClient
    RejectPolicy = FakeRejectPolicy


def main() -> int:
    core.load_paramiko = lambda: FakeParamiko
    core.getpass.getpass = lambda _prompt: "test-password"
    core.DEFAULT_RECONNECT_WAIT = 1
    core.RECONNECT_DELAYS = (0.20, 0.20, 0.20)
    core.SSH_MONITOR_INTERVAL = 0.05

    args = ssh_relay.build_parser().parse_args([
        "daemon",
        "--name",
        "ci-core",
        "--host",
        "198.51.100.42",
        "--user",
        "donpedro",
        "--command-timeout",
        "1",
    ])
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
