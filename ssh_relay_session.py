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
REDACTED_SECRET = "[СКРЫТО]"


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    """Возвращает только сохраняемые поля session-файла без служебных ключей чтения."""
    return {key: value for key, value in session.items() if not str(key).startswith("_")}


def _caused_by_connection_refused(exc: BaseException) -> bool:
    """Проверяет цепочку исключений на подтверждённый отказ локального listener."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _redact_known_secrets(text: str, secrets: tuple[str | None, ...]) -> str:
    """Скрывает только явно известные relay секреты, не маскируя полезную диагностику целиком."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED_SECRET)
    return redacted


def _install_exception_redaction(core: Any) -> None:
    """Не позволяет исключениям Paramiko/канала случайно вывести переданные секреты."""
    if getattr(core, "_secret_redaction_installed", False):
        return
    if not hasattr(core, "load_paramiko") or not hasattr(core, "execute_remote_command"):
        return

    original_load_paramiko = core.load_paramiko
    original_execute_remote_command = core.execute_remote_command

    class SSHClientProxy:
        def __init__(self, client: Any) -> None:
            self._client = client

        def connect(self, *args: Any, **kwargs: Any) -> Any:
            redacted_error: str | None = None
            try:
                return self._client.connect(*args, **kwargs)
            except Exception as exc:
                original = str(exc)
                redacted = _redact_known_secrets(
                    original,
                    (
                        str(kwargs.get("password")) if kwargs.get("password") is not None else None,
                        str(kwargs.get("passphrase")) if kwargs.get("passphrase") is not None else None,
                    ),
                )
                if redacted == original:
                    raise
                redacted_error = redacted or exc.__class__.__name__
            # Поднимаем новое исключение уже вне except, чтобы исходное исключение
            # с секретом не осталось ни в __cause__, ни в __context__.
            raise core.RelayError(redacted_error)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._client, name)

    class ParamikoProxy:
        def __init__(self, module: Any) -> None:
            self._module = module

        def SSHClient(self) -> SSHClientProxy:
            return SSHClientProxy(self._module.SSHClient())

        def __getattr__(self, name: str) -> Any:
            return getattr(self._module, name)

    def protected_load_paramiko() -> Any:
        return ParamikoProxy(original_load_paramiko())

    def protected_execute_remote_command(
        client: Any,
        command: str,
        timeout_seconds: int,
        stdin_data: bytes | None = None,
    ) -> dict[str, Any]:
        redacted_error: str | None = None
        try:
            return original_execute_remote_command(
                client,
                command,
                timeout_seconds,
                stdin_data=stdin_data,
            )
        except Exception as exc:
            if stdin_data is None:
                raise
            stdin_text = stdin_data.decode("utf-8", errors="replace")
            original = str(exc)
            redacted = _redact_known_secrets(original, (stdin_text, stdin_text.rstrip("\r\n")))
            if redacted == original:
                raise
            redacted_error = redacted or exc.__class__.__name__
        # Секретное исходное исключение не сохраняется в exception chain.
        raise core.RelayError(redacted_error)

    core.load_paramiko = protected_load_paramiko
    core.execute_remote_command = protected_execute_remote_command
    core._secret_redaction_installed = True


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
    """Устанавливает защиту session-файла и секретов поверх core без смены протокола."""
    _install_exception_redaction(core)
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

    def protected_check_existing_session(name: str) -> bool:
        """Не допускает второй daemon, если состояние существующей регистрации неоднозначно."""
        path = core.existing_session_file_path(name)
        if not path.exists():
            return False

        try:
            session = core.read_session(name)
        except core.RelayError as exc:
            print(f"Сессия {name} зарегистрирована, но session-файл не удалось прочитать: {exc}", file=sys.stderr)
            print(
                "Автоматический запуск второго daemon запрещён. Удалите повреждённый session-файл вручную "
                "только после проверки, что старый daemon действительно не работает.",
                file=sys.stderr,
            )
            return True

        try:
            result = core.request_daemon(session, "status")
        except core.DaemonUnavailableError as exc:
            if _caused_by_connection_refused(exc):
                token = str(session.get("auth_token") or "")
                if token:
                    original_remove_session_file(name, token)
                if core.existing_session_file_path(name).exists():
                    print(
                        f"Регистрация сессии {name} изменилась во время проверки; "
                        "запуск второго daemon запрещён.",
                        file=sys.stderr,
                    )
                    return True
                print(
                    f"Старая регистрация сессии {name} удалена: локальный порт daemon не слушает.",
                    file=sys.stderr,
                )
                return False

            print(f"Сессия {name} зарегистрирована, но состояние daemon не подтверждено: {exc}", file=sys.stderr)
            print(
                "Запуск второго daemon с тем же именем запрещён, пока состояние первой сессии неизвестно.",
                file=sys.stderr,
            )
            return True
        except core.RelayError as exc:
            print(f"Сессия {name} зарегистрирована, но проверка daemon завершилась ошибкой: {exc}", file=sys.stderr)
            print("Запуск второго daemon с тем же именем запрещён.", file=sys.stderr)
            return True

        if result.get("ok"):
            print(f"Сессия {name} уже активна.", file=sys.stderr)
            print(f"Сначала завершите её командой: stop --name {name}", file=sys.stderr)
            return True

        print(f"Сессия {name} существует, но daemon не подтвердил активное состояние.", file=sys.stderr)
        print("Запуск второго daemon с тем же именем запрещён.", file=sys.stderr)
        return True

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
    core.check_existing_session = protected_check_existing_session
    core.stop_one_session = protected_stop_one_session
    core._session_safety_installed = True
