#!/usr/bin/env python3
"""Наблюдаемая identity аутентифицированного SSH-транспорта (без секретов)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from typing import Any

IDENTITY_SCHEMA_VERSION = 1
IDENTITY_KEYS = frozenset({
    "schema_version", "remote_host", "remote_port", "remote_user",
    "host_key_algorithm", "host_key_sha256", "trusted_host_key",
    "daemon_instance_id", "connection_generation",
})


def observed_identity(client: Any, *, host: str, port: int, user: str,
                      daemon_instance_id: str, connection_generation: int) -> dict[str, Any] | None:
    """Снимает ключ именно активного, аутентифицированного транспорта."""
    try:
        transport = client.get_transport()
        if transport is None or not transport.is_active() or not transport.is_authenticated():
            return None
        key = transport.get_remote_server_key()
        if key is None or not key.asbytes() or not key.get_name():
            return None
        fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
    except (AttributeError, OSError, ValueError, TypeError):
        return None
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "remote_host": host,
        "remote_port": port,
        "remote_user": user,
        "host_key_algorithm": key.get_name(),
        "host_key_sha256": "SHA256:" + fingerprint,
        "trusted_host_key": True,
        "daemon_instance_id": daemon_instance_id,
        "connection_generation": connection_generation,
    }


def valid_expected(expected: object) -> bool:
    """Проверяет полную точную identity без слабого сравнения с session label."""
    if not isinstance(expected, dict) or set(expected) != IDENTITY_KEYS:
        return False
    if type(expected["schema_version"]) is not int or expected["schema_version"] != IDENTITY_SCHEMA_VERSION:
        return False
    if type(expected["remote_port"]) is not int or not 1 <= expected["remote_port"] <= 65535:
        return False
    if type(expected["connection_generation"]) is not int or expected["connection_generation"] < 1:
        return False
    if expected["trusted_host_key"] is not True:
        return False
    if any(not isinstance(expected[field], str) or not expected[field].strip() or "\x00" in expected[field]
           for field in ("remote_host", "remote_user", "host_key_algorithm", "host_key_sha256", "daemon_instance_id")):
        return False
    try:
        uuid.UUID(expected["daemon_instance_id"])
    except (TypeError, ValueError, AttributeError):
        return False
    digest = expected["host_key_sha256"]
    if not digest.startswith("SHA256:") or len(digest) != 50:
        return False
    try:
        raw = base64.b64decode(digest[7:] + "=", validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    return len(raw) == 32


def trusted_match(expected: object, observed: object) -> bool:
    return valid_expected(expected) and isinstance(observed, dict) and observed == expected


def expected_from_args(args: Any) -> dict[str, Any] | None:
    """Отказывает при частично заданной identity, не подставляя session metadata."""
    names = ("expected_remote_host", "expected_remote_port", "expected_remote_user",
             "expected_host_key_algorithm", "expected_host_key_sha256",
             "expected_daemon_instance_id", "expected_connection_generation")
    values = [getattr(args, name, None) for name in names]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("Для verified-machine вызова нужны все поля ожидаемой SSH identity.")
    expected = dict(zip(names, values))
    result = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "remote_host": expected["expected_remote_host"],
        "remote_port": expected["expected_remote_port"],
        "remote_user": expected["expected_remote_user"],
        "host_key_algorithm": expected["expected_host_key_algorithm"],
        "host_key_sha256": expected["expected_host_key_sha256"],
        "trusted_host_key": True,
        "daemon_instance_id": expected["expected_daemon_instance_id"],
        "connection_generation": expected["expected_connection_generation"],
    }
    if not valid_expected(result):
        raise ValueError("Некорректная ожидаемая SSH identity.")
    return result
