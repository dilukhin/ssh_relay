#!/usr/bin/env python3
"""Тестовый subprocess-runner daemon с fake Paramiko без внешнего SSH."""

from __future__ import annotations

import sys
import types
from pathlib import Path

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


def main() -> int:
    core.load_paramiko = lambda: FAKE_PARAMIKO
    core.getpass.getpass = lambda _prompt: "test-password"
    args = ssh_relay.build_parser().parse_args([
        "daemon",
        "--name",
        "ci-session",
        "--host",
        "198.51.100.42",
        "--user",
        "donpedro",
    ])
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
