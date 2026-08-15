#!/usr/bin/env python3
"""Структурированные исходы локального daemon transport и удалённых команд."""

from __future__ import annotations

from typing import Any

REDACTED_STDIN = "[СКРЫТО]"


def _find_connection_refused(exc: BaseException) -> ConnectionRefusedError | None:
    """Возвращает подтверждённый отказ локального listener из exception chain."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _decode_chunks(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace")


def _redact_stdin(text: str, stdin_data: bytes | None) -> str:
    if stdin_data is None:
        return text
    stdin_text = stdin_data.decode("utf-8", errors="replace")
    candidates = (stdin_text, stdin_text.rstrip("\r\n"))
    redacted = text
    for candidate in candidates:
        if candidate:
            redacted = redacted.replace(candidate, REDACTED_STDIN)
    return redacted


def install(core: Any) -> None:
    """Добавляет машинно различимые причины отказа без изменения публичного CLI."""
    if getattr(core, "_machine_outcomes_installed", False):
        return
    required = ("DaemonUnavailableError", "RelayError", "request_daemon", "execute_remote_command")
    if any(not hasattr(core, name) for name in required):
        return

    original_request_daemon = core.request_daemon
    original_execute_remote_command = core.execute_remote_command

    class DaemonRequestError(core.DaemonUnavailableError):
        """Ошибка локального запроса с признаком возможной доставки daemon."""

        def __init__(self, message: str, *, request_sent: bool, error_code: str) -> None:
            super().__init__(message)
            self.request_sent = request_sent
            self.error_code = error_code

    class RemoteCommandError(core.RelayError):
        """Ошибка SSH-команды с признаком возможного запуска на удалённой стороне."""

        def __init__(
            self,
            message: str,
            *,
            error_code: str,
            command_started: bool,
            stdout: str = "",
            stderr: str = "",
        ) -> None:
            super().__init__(message)
            self.error_code = error_code
            self.command_started = command_started
            self.stdout = stdout
            self.stderr = stderr

    def request_daemon(
        session: dict[str, Any],
        action: str,
        *,
        response_timeout: float | None = 5,
        **payload: Any,
    ) -> dict[str, Any]:
        try:
            return original_request_daemon(
                session,
                action,
                response_timeout=response_timeout,
                **payload,
            )
        except core.DaemonUnavailableError as exc:
            refused = _find_connection_refused(exc)
            # ConnectionRefused доказывает, что локальный daemon запрос не получил.
            # Одновременно сохраняем прежний непосредственный cause для compatibility/tests.
            if refused is not None:
                raise DaemonRequestError(
                    str(exc),
                    request_sent=False,
                    error_code="daemon_unavailable",
                ) from refused
            # Любой другой transport failure трактуем консервативно как возможную доставку.
            raise DaemonRequestError(
                str(exc),
                request_sent=True,
                error_code="daemon_response_lost",
            ) from exc
        except core.RelayError as exc:
            # Повреждённый/неполный ответ возможен только после локальной отправки запроса.
            raise DaemonRequestError(
                str(exc),
                request_sent=True,
                error_code="response_invalid",
            ) from exc

    class _CommandState:
        def __init__(self) -> None:
            self.command_started = False
            self.stdout: list[bytes] = []
            self.stderr: list[bytes] = []
            self.channel: Any | None = None
            self.channel_closed = False

    class _ChannelProxy:
        def __init__(self, channel: Any, state: _CommandState) -> None:
            self._channel = channel
            self._state = state
            state.channel = channel

        def exec_command(self, command: str) -> Any:
            # Вызов может успеть отправить SSH exec-request до локальной ошибки.
            self._state.command_started = True
            return self._channel.exec_command(command)

        def recv(self, size: int) -> bytes:
            chunk = self._channel.recv(size)
            self._state.stdout.append(chunk)
            return chunk

        def recv_stderr(self, size: int) -> bytes:
            chunk = self._channel.recv_stderr(size)
            self._state.stderr.append(chunk)
            return chunk

        def close(self) -> Any:
            self._state.channel_closed = True
            return self._channel.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._channel, name)

    class _TransportProxy:
        def __init__(self, transport: Any, state: _CommandState) -> None:
            self._transport = transport
            self._state = state

        def open_session(self, *args: Any, **kwargs: Any) -> _ChannelProxy:
            channel = self._transport.open_session(*args, **kwargs)
            return _ChannelProxy(channel, self._state)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._transport, name)

    class _ClientProxy:
        def __init__(self, client: Any, state: _CommandState) -> None:
            self._client = client
            self._state = state

        def get_transport(self) -> Any:
            transport = self._client.get_transport()
            if transport is None:
                return None
            return _TransportProxy(transport, self._state)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._client, name)

    def execute_remote_command(
        client: Any,
        command: str,
        timeout_seconds: int,
        stdin_data: bytes | None = None,
    ) -> dict[str, Any]:
        state = _CommandState()
        sanitized_error: RemoteCommandError | None = None
        try:
            return original_execute_remote_command(
                _ClientProxy(client, state),
                command,
                timeout_seconds,
                stdin_data=stdin_data,
            )
        except Exception as exc:
            started = state.command_started
            message = str(exc) or exc.__class__.__name__
            stdout = _decode_chunks(state.stdout)
            stderr = _decode_chunks(state.stderr)
            if stdin_data is not None:
                message = _redact_stdin(message, stdin_data)
                stdout = _redact_stdin(stdout, stdin_data)
                stderr = _redact_stdin(stderr, stdin_data)
            wrapped = RemoteCommandError(
                message,
                error_code="command_result_unknown" if started else "command_not_started",
                command_started=started,
                stdout=stdout,
                stderr=stderr,
            )
            # stdin_data сейчас используется для sudo-пароля. Поднимаем новое исключение
            # вне except, чтобы секрет не сохранился в __cause__/__context__.
            if stdin_data is not None:
                sanitized_error = wrapped
            else:
                raise wrapped from exc
        finally:
            # Старый core закрывает канал в собственном finally после успешного exec-request.
            # Если open_session успел пройти, а exec/send упал раньше этого блока, закрываем здесь.
            if state.channel is not None and not state.channel_closed:
                state.channel.close()
                state.channel_closed = True

        if sanitized_error is not None:
            raise sanitized_error
        raise AssertionError("Недостижимое состояние выполнения удалённой команды")

    core.DaemonRequestError = DaemonRequestError
    core.RemoteCommandError = RemoteCommandError
    core.request_daemon = request_daemon
    core.execute_remote_command = execute_remote_command
    core._machine_outcomes_installed = True
