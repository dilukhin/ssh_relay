"""Версия, provenance и локальная diagnostic identity установленного ssh_relay."""

from __future__ import annotations

import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

SEMANTIC_VERSION = "0.9.1"
_SOURCE_SHA: str | None = None
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_invocation_identity_recorded = False


def _normalize_source_sha(value: object) -> str | None:
    text = str(value or "").strip()
    if not _SHA_PATTERN.fullmatch(text):
        return None
    return text.lower()


def source_sha() -> str | None:
    """Возвращает полный exact source SHA, внедрённый в сборку.

    Переменная окружения предназначена только для source/tests и не может
    переопределить SHA, уже встроенный в установленный runtime.
    """
    if _SOURCE_SHA is not None:
        embedded = _normalize_source_sha(_SOURCE_SHA)
        if embedded is None:
            raise RuntimeError("Повреждены build metadata ssh_relay: source SHA имеет неверный формат.")
        return embedded
    return _normalize_source_sha(os.environ.get("SSH_RELAY_SOURCE_SHA"))


def canonical_identity() -> str:
    """Возвращает каноническую компактную диагностическую идентичность."""
    exact_sha = source_sha()
    suffix = exact_sha[:8] if exact_sha is not None else "unknown"
    return f"ssh_relay {SEMANTIC_VERSION}.{suffix}"


def diagnostic_log_path() -> Path:
    """Возвращает путь локального журнала diagnostic identity."""
    override = os.environ.get("SSH_RELAY_DIAGNOSTIC_LOG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "ssh_relay" / "diagnostics.log"


def _append_diagnostic_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    if path.is_symlink():
        raise OSError("diagnostic log не должен быть symlink")

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("diagnostic log должен быть обычным файлом")
        payload = line.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("не удалось записать diagnostic identity")
            offset += written
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def record_invocation_identity() -> bool:
    """Best-effort записывает identity ровно один раз за процесс.

    Команда, argv, stdout/stderr и другие потенциально чувствительные данные в
    запись не входят. Ошибка диагностики не должна менять исход операции.
    """
    global _invocation_identity_recorded
    if _invocation_identity_recorded:
        return False
    _invocation_identity_recorded = True

    try:
        exact_sha = source_sha()
        identity = canonical_identity()
    except RuntimeError:
        exact_sha = None
        identity = f"ssh_relay {SEMANTIC_VERSION}.invalid-build"

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    line = f"{timestamp} {identity} source_sha={exact_sha or 'unknown'} pid={os.getpid()}\n"
    try:
        _append_diagnostic_line(diagnostic_log_path(), line)
    except (OSError, ValueError):
        return False
    return True
