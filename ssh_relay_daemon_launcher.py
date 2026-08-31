#!/usr/bin/env python3
"""Launcher дочернего daemon, создаваемого режимом --detach."""

from __future__ import annotations

from ssh_relay_entrypoint import _configure_stdio
from ssh_relay_logging import install_daemon_timestamp_streams


def main() -> int:
    _configure_stdio()
    install_daemon_timestamp_streams()

    import ssh_relay

    return int(ssh_relay.main())


if __name__ == "__main__":
    raise SystemExit(main())
