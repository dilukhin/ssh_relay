#!/usr/bin/env python3
"""Installed console entry point for ssh_relay."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ssh_relay_build import canonical_identity, record_invocation_identity, source_sha


def _configure_stdio() -> None:
    """Keep the installed CLI UTF-8 safe when Windows output is redirected/captured."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def doctor() -> int:
    """Проверяет локальный packaged runtime без открытия SSH-соединения."""
    identity = canonical_identity()
    try:
        import paramiko
    except ImportError as exc:
        print(f"{identity}: ошибка runtime: не удалось импортировать paramiko: {exc}", file=sys.stderr)
        return 1

    paramiko_version = getattr(paramiko, "__version__", "unknown")
    print(identity)
    print(f"Source SHA: {source_sha() or 'unknown'}")
    print(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print(f"paramiko: {paramiko_version}")
    print("Runtime: ok")
    return 0


def main() -> int:
    _configure_stdio()
    arguments = sys.argv[1:]
    if arguments in (["--version"], ["-v"]):
        print(canonical_identity())
        return 0
    if arguments == ["doctor"]:
        return doctor()

    # Диагностика идёт в отдельный локальный журнал и не меняет stdout/stderr
    # команды, включая text-mode remote output и machine JSON.
    record_invocation_identity()

    import ssh_relay

    if sys.argv[1:2] == ["daemon"]:
        from ssh_relay_logging import install_daemon_timestamp_streams

        install_daemon_timestamp_streams()
        # Старый --detach повторно запускает путь из ssh_relay.__file__.
        # Направляем его на launcher, который также включает timestamp-потоки.
        ssh_relay.__file__ = str(Path(__file__).with_name("ssh_relay_daemon_launcher.py"))

    return int(ssh_relay.main())


if __name__ == "__main__":
    raise SystemExit(main())
