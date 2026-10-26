"""Неактивный контракт коротких привилегированных проверок, задача #58.

Подготовленное намерение — данные для защищённого владельца, НЕ разрешение.
В модуле нет исполнителя, выдачи грантов, SSH, shell или хранения вывода.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from types import MappingProxyType

import ssh_relay_verified_identity as identity


PROFILE = "ssh-relay-sudo-inspect-intent/v1"
MAX_LIFETIME_MS = 60_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


@dataclass(frozen=True)
class ProbeSpec:
    """Кандидат точной команды; наличие записи не даёт права её запускать."""

    probe_id: str
    argv: tuple[str, ...]
    timeout_seconds: int = 10
    max_output_bytes: int = 262_144


PROBES = MappingProxyType({
    spec.probe_id: spec for spec in (
        ProbeSpec("nft_summary", ("/usr/sbin/nft", "--json", "list", "ruleset")),
        ProbeSpec("iptables_v4_summary", ("/usr/sbin/iptables-save",)),
        ProbeSpec("iptables_v6_summary", ("/usr/sbin/ip6tables-save",)),
        ProbeSpec("ufw_summary", ("/usr/sbin/ufw", "status", "verbose")),
        ProbeSpec("ss_listeners_summary", ("/usr/bin/ss", "-H", "-lntup")),
    )
})


@dataclass(frozen=True)
class PreparedInspectIntent:
    """Неизменяемые канонические байты без ссылок на входные словари."""

    canonical_bytes: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def prepare_intent(
    *, probe_id: str, session: str, expected_identity: dict,
    transport_source_sha: str, transport_executable_sha256: str,
    transaction_id: str, deadline_unix_ms: int, now_unix_ms: int,
) -> PreparedInspectIntent:
    """Проверяет форму намерения; доверенность фактов обязан доказать владелец.

Время, SSH pins и identity исполняемого файла должны приобретаться доверенной
стороной. Вызов этой функции агентом не создаёт согласия или права исполнения.
"""
    if not isinstance(probe_id, str) or probe_id not in PROBES:
        raise ValueError("Проверка отсутствует в закрытом каталоге.")
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        raise ValueError("Некорректное точное имя relay-сессии.")
    if type(expected_identity) is not dict:
        raise ValueError("Требуется полная проверенная SSH identity.")
    pinned = expected_identity.copy()
    if not identity.validate_expected(pinned):
        raise ValueError("Требуется полная проверенная SSH identity.")
    if not isinstance(transport_source_sha, str) or not identity.SOURCE_SHA.fullmatch(transport_source_sha):
        raise ValueError("Требуется полный SHA исходников транспорта.")
    if transport_source_sha != pinned["daemon_source_sha"]:
        raise ValueError("Исходники клиента и daemon должны совпадать.")
    if not isinstance(transport_executable_sha256, str) or not _SHA256.fullmatch(transport_executable_sha256):
        raise ValueError("Требуется SHA-256 исполняемого транспорта.")
    try:
        if not isinstance(transaction_id, str) or str(uuid.UUID(transaction_id)) != transaction_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("Требуется канонический UUID транзакции.") from None
    if (type(now_unix_ms) is not int or type(deadline_unix_ms) is not int
            or now_unix_ms < 0 or not 0 < deadline_unix_ms - now_unix_ms <= MAX_LIFETIME_MS):
        raise ValueError("Срок намерения должен быть положительным и не превышать 60 секунд.")

    probe = PROBES[probe_id]
    operation = {
        "profile": PROFILE, "probe_id": probe_id,
        "execution_spec": {
            "argv": probe.argv, "environment": {"LC_ALL": "C"},
            "stdin": "closed", "timeout_seconds": probe.timeout_seconds,
            "max_output_bytes": probe.max_output_bytes,
            "normalizer_profile": "ssh-relay-inspect-summary/v1",
        },
        "session": session, "expected_identity": pinned,
        "transport_source_sha": transport_source_sha,
        "transport_executable_sha256": transport_executable_sha256,
        "transaction_id": transaction_id, "deadline_unix_ms": deadline_unix_ms,
    }
    return PreparedInspectIntent(json.dumps(
        operation, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))


def blocked_result() -> dict:
    """Безусловный отказ до исполнения, без отражения недоверенного payload.

Не принимает grant/approved/trusted и не имеет пути к старому sudo-exec.
Будущий исполнитель требует отдельного review и реализации #57.
"""
    return {
        "ok": True, "action": "sudo_inspect", "schema_version": 1,
        "operation_status": "not_started", "command_status": "not_started",
        "command_started": False, "request_not_started": True,
        "command_exit_code": None, "receipt_status": "not_requested",
        "replay_status": "disabled", "error_code": "sudo_inspect_not_implemented",
    }
