#!/usr/bin/env python3
"""Защита локальной регистрации relay-сессии и status control-plane."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

STATUS_RETRY_DELAYS = (0.1, 0.3)
SESSION_GUARD_INTERVAL = 1.0


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    """Возвращает только сохраняемые поля session-файла без служебных ключей чтения."""
    return {key: value for key, value in session.items() if not str(key).startswith("_")}


def restore_session_file_if_missing(core: Any, name: str, session: dict[str, Any]) -> Path | None:
    """Публикует целый session-файл атомарно и не перезаписывает чужую сессию."""
    path = core.session_file_path(name)
    if path.exists():
        return None

    core.prepare_session_directory()
    data = json.dumps(_session_payload(session), ensure_ascii=False, indent=2)
    temporary = path.with_name(f".{path.name}.restore-{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)

        try:
            # Hard link создаёт целевой dir entry только после закрытия временного
            # файла и атомарно отказывает, если имя уже занято другой сессией.
            os.link(temporary, path)
        except FileExistsError:
            return None
        if os.name != "nt":
            os.chmod(path, 0o600)
        if name == core.DEFAULT_SESSION_NAME:
            core.legacy_session_file_path().unlink(missing_ok=True)
        return path
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def install(core: Any) -> None:
    """Устанавливает защиту session-файла поверх существующего core без смены протокола."""
    if getattr(core, "_session_safety_installed", False):
        return

    original_request_daemon = core.request_daemon
    original_remove_session_file = core.remove_session_file
    original_write_session = core.write_session

    guard_lock = threading.Lock()
    guards: dict[str, tuple[str, threading.Event]] = {}

    def stop_guard(name: str, expected_token: str) -> None:
        with guard_lock:
            current = guards.get(name)
            if current is None or current[0] != expected_token:
                return
            current[1].set()
            del guards[name]

    def start_guard(name: str, session: dict[str, Any]) -> None:
        snapshot = _session_payload(session)
        token = str(snapshot.get("auth_token") or "")
        if not token:
            return

        stop_event = threading.Event()
        with guard_lock:
            previous = guards.get(name)
            if previous is not None:
                if previous[0] == token:
                    return
                previous[1].set()
            guards[name] = (token, stop_event)

        def guard_loop() -> None:
            reported_error: str | None = None
            while not stop_event.wait(SESSION_GUARD_INTERVAL):
                try:
                    restored = restore_session_file_if_missing(core, name, snapshot)
                except Exception as exc:
                    error_text = f"{exc.__class__.__name__}: {exc}"
                    if error_text != reported_error:
                        print(
                            f"Не удалось восстановить файл сессии {name}: {error_text}",
                            file=sys.stderr,
                            flush=True,
                        )
                    reported_error = error_text
                    continue

                reported_error = None
                if restored is not None:
                    print(f"Файл сессии восстановлен: {restored}", file=sys.stderr, flush=True)

        threading.Thread(
            target=guard_loop,
            name=f"ssh-relay-session-guard-{name}",
            daemon=True,
        ).start()

    def protected_write_session(name: str, session: dict[str, Any]) -> Path:
        path = original_write_session(name, session)
        start_guard(name, session)
        return path

    def protected_remove_session_file(name: str, expected_token: str | None = None) -> None:
        # Локальный timeout не доказывает, что daemon умер. Удалять регистрацию
        # разрешено только владельцу токена при штатном завершении daemon.
        if expected_token is None:
            return
        stop_guard(name, expected_token)
        original_remove_session_file(name, expected_token)

    def protected_request_daemon(
        session: dict[str, Any],
        action: str,
        *,
        response_timeout: float | None = 5,
        **payload: Any,
    ) -> dict[str, Any]:
        # Повторять exec/upload/stop нельзя: после потери ответа результат может
        # быть неизвестен. Status read-only, поэтому для него допустим короткий retry.
        if action != "status":
            return original_request_daemon(
                session,
                action,
                response_timeout=response_timeout,
                **payload,
            )

        last_error: Exception | None = None
        delays = (0.0, *STATUS_RETRY_DELAYS)
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            try:
                return original_request_daemon(
                    session,
                    action,
                    response_timeout=response_timeout,
                    **payload,
                )
            except core.DaemonUnavailableError as exc:
                last_error = exc
                if attempt == len(delays) - 1:
                    raise
        assert last_error is not None
        raise last_error

    def protected_stop_one_session(name: str) -> int:
        try:
            session = core.read_session(name)
            result = core.request_daemon(session, "stop")
        except core.DaemonUnavailableError as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            print(
                f"{name}: файл сессии сохранён; завершение daemon не подтверждено.",
                file=sys.stderr,
            )
            return 1
        except core.RelayError as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            return 1

        if not result.get("ok"):
            print(
                f"{name}: не удалось остановить relay: "
                f"{result.get('protocol_error', 'неизвестная ошибка')}",
                file=sys.stderr,
            )
            return 1
        print(f"{name}: команда завершения отправлена активному daemon.")
        return 0

    core.request_daemon = protected_request_daemon
    core.remove_session_file = protected_remove_session_file
    core.write_session = protected_write_session
    core.stop_one_session = protected_stop_one_session
    core._session_safety_installed = True
