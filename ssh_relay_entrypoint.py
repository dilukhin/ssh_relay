#!/usr/bin/env python3
"""Installed console entry point for ssh_relay."""

from __future__ import annotations

import sys


def doctor() -> int:
    """Validate the local packaged runtime without opening an SSH connection."""
    try:
        import paramiko
    except ImportError as exc:
        print(f"ssh_relay runtime error: paramiko import failed: {exc}", file=sys.stderr)
        return 1

    from ssh_relay import __version__

    paramiko_version = getattr(paramiko, "__version__", "unknown")
    print(f"ssh_relay {__version__}")
    print(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print(f"paramiko: {paramiko_version}")
    print("Runtime: ok")
    return 0


def main() -> int:
    if sys.argv[1:] == ["doctor"]:
        return doctor()

    from ssh_relay import main as relay_main

    return int(relay_main())


if __name__ == "__main__":
    raise SystemExit(main())
