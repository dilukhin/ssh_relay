#!/usr/bin/env python3
"""Запуск настоящего relay-daemon против локального Paramiko SSH-server."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssh_relay_core as core


PASSWORD = os.environ["SSH_RELAY_REAL_SSH_PASSWORD"]


def main() -> int:
    core.getpass.getpass = lambda prompt: "via-test-password" if "посредника" in prompt else PASSWORD
    core.DEFAULT_RECONNECT_WAIT = 2
    core.RECONNECT_DELAYS = (0.10, 0.10, 0.20, 0.20)
    core.SSH_MONITOR_INTERVAL = 0.05

    import ssh_relay

    arguments = [
            "daemon",
            "--name",
            "ci-real-ssh",
            "--host",
            "127.0.0.1",
            "--port",
            os.environ["SSH_RELAY_REAL_SSH_PORT"],
            "--user",
            "donpedro",
            "--known-hosts",
            os.environ["SSH_RELAY_REAL_KNOWN_HOSTS"],
            "--command-timeout",
            "2",
        ]
    if os.environ.get("SSH_RELAY_REAL_ENABLE_SUDO") == "1":
        arguments.append("--enable-sudo")
    if "SSH_RELAY_REAL_VIA_PORT" in os.environ:
        arguments[arguments.index("--host") + 1] = "unique-pc.invalid"
        arguments[arguments.index("--port") + 1] = "22"
        arguments.extend(["--via-host", "127.0.0.1", "--via-port", os.environ["SSH_RELAY_REAL_VIA_PORT"],
                          "--via-user", "via-user", "--via-known-hosts", os.environ["SSH_RELAY_REAL_VIA_KNOWN_HOSTS"],
                          "--via-target-port", os.environ["SSH_RELAY_REAL_VIA_FORWARD_PORT"]])
    args = ssh_relay.build_parser().parse_args(arguments)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
