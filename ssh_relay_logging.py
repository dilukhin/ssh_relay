"""Форматирование локального журнала daemon с метками времени."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from typing import Callable, TextIO


def format_local_timestamp(moment: datetime | None = None) -> str:
    """Возвращает ISO 8601 с миллисекундами и локальным смещением UTC."""
    current = moment if moment is not None else datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    return current.isoformat(timespec="milliseconds")


def _looks_like_secret_prompt(text: str) -> bool:
    stripped = text.rstrip()
    return stripped.endswith(":") and ("пароль" in stripped.lower() or "passphrase" in stripped.lower())


class TimestampedDaemonStream:
    """Добавляет метку времени к завершённым строкам, не ломая password prompt."""

    def __init__(
        self,
        stream: TextIO,
        *,
        timestamp_factory: Callable[[], str] = format_local_timestamp,
    ) -> None:
        self._stream = stream
        self._timestamp_factory = timestamp_factory
        self._pending = ""
        self._lock = threading.Lock()
        self._ssh_relay_timestamped_daemon_stream = True

    def _emit_line(self, line: str) -> None:
        if line in {"\n", "\r\n"}:
            self._stream.write(line)
            return
        self._stream.write(f"[{self._timestamp_factory()}] {line}")

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("ожидалась строка")
        if not text:
            return 0
        with self._lock:
            if not self._pending and "\n" not in text and "\r" not in text and _looks_like_secret_prompt(text):
                self._stream.write(text)
                return len(text)

            self._pending += text
            while True:
                lf = self._pending.find("\n")
                if lf < 0:
                    break
                line = self._pending[: lf + 1]
                self._pending = self._pending[lf + 1 :]
                self._emit_line(line)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                self._emit_line(self._pending)
                self._pending = ""
            self._stream.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str | None:
        return self._stream.encoding

    @property
    def errors(self) -> str | None:
        return getattr(self._stream, "errors", None)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def install_daemon_timestamp_streams() -> None:
    """Устанавливает timestamp-обёртки stdout/stderr текущего daemon-процесса."""
    if not getattr(sys.stdout, "_ssh_relay_timestamped_daemon_stream", False):
        sys.stdout = TimestampedDaemonStream(sys.stdout)  # type: ignore[assignment]
    if not getattr(sys.stderr, "_ssh_relay_timestamped_daemon_stream", False):
        sys.stderr = TimestampedDaemonStream(sys.stderr)  # type: ignore[assignment]
