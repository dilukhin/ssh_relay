#!/usr/bin/env python3
"""Строгое сравнение ожидаемого SSH-узла с удостоверенным daemon транспортом."""

from __future__ import annotations

import re
import uuid
from typing import Any

SCHEMA_VERSION = 1
FIELDS = frozenset({
    "schema_version", "remote_host", "remote_port", "remote_user",
    "host_key_algorithm", "remote_host_key_sha256", "trusted_known_hosts",
    "daemon_instance_id", "connection_generation", "daemon_source_sha",
})
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")


def validate_expected(value: object) -> bool:
    """Только полная точная identity, без догадок по session-файлу или host-label."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        return False
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        return False
    if type(value["remote_port"]) is not int or not 1 <= value["remote_port"] <= 65535:
        return False
    if type(value["connection_generation"]) is not int or value["connection_generation"] < 1:
        return False
    if value["trusted_known_hosts"] is not True:
        return False
    if any(not isinstance(value[field], str) or not value[field].strip() or "\x00" in value[field]
           for field in ("remote_host", "remote_user", "host_key_algorithm",
                         "remote_host_key_sha256", "daemon_instance_id", "daemon_source_sha")):
        return False
    if not FINGERPRINT.fullmatch(value["remote_host_key_sha256"]):
        return False
    if not SOURCE_SHA.fullmatch(value["daemon_source_sha"]):
        return False
    try:
        uuid.UUID(value["daemon_instance_id"])
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def matches(expected: object, observed: object) -> bool:
    return validate_expected(expected) and isinstance(observed, dict) and observed == expected


def from_args(args: Any) -> dict[str, Any] | None:
    """Обязательные pinned аргументы только при явно запрошенном verified режиме."""
    mapping = {
        "remote_host": "expected_remote_host", "remote_port": "expected_remote_port",
        "remote_user": "expected_remote_user", "host_key_algorithm": "expected_host_key_algorithm",
        "remote_host_key_sha256": "expected_host_key_sha256",
        "daemon_instance_id": "expected_daemon_instance_id",
        "connection_generation": "expected_connection_generation",
        "daemon_source_sha": "expected_daemon_source_sha",
    }
    values = {field: getattr(args, arg, None) for field, arg in mapping.items()}
    verified = bool(getattr(args, "require_verified_identity", False))
    if not verified:
        if any(value is not None for value in values.values()):
            raise ValueError("Поля pin разрешены только с --require-verified-identity.")
        return None
    expected = {"schema_version": SCHEMA_VERSION, "trusted_known_hosts": True, **values}
    if not validate_expected(expected):
        raise ValueError("Для verified-режима требуется полная корректная ожидаемая SSH identity.")
    return expected
