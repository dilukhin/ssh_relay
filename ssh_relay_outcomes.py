#!/usr/bin/env python3
"""Структурированные исходы локального daemon transport и удалённых команд."""

from __future__ import annotations

import json
import socket
import time
from typing import Any


def install(core: Any) -> None:
    """Добавляет машинно различимые причины отказа без изменения публичного CLI."""
    if getattr(core, "_machine_outcomes_installed", False):
        return

    required = (
        "DaemonUnavailableError",
        "RelayError",
        "request_daemon",
        "execute_remote_command",
        "read_message",
        "BUFFER_SIZE",
        "MAX_OUTPUT_SIZE",
    )
    if any(not hasattr(core, name) for name in required):
        return

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
        request = {"auth_token": session["auth_token"], "action": action, **payload}
        request_sent = False
        try:
            with socket.create_connection(("127.0.0.1", session["daemon_port"]), timeout=5) as sock:
                # После установления соединения ошибка отправки трактуется консервативно:
                # daemon мог получить полный запрос до локальной ошибки сокета.
                request_sent = True
                sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8"))
                sock.shutdown(socket.SHUT_WR)
                sock.settimeout(response_timeout)
                try:
                    return core.read_message(sock)
                except core.RelayError as exc:
                    raise DaemonRequestError(
                        str(exc),
                        request_sent=True,
                        error_code="response_invalid",
                    ) from exc
        except DaemonRequestError:
            raise
        except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
            raise DaemonRequestError(
                "Daemon недоступен или не ответил вовремя.",
                request_sent=request_sent,
                error_code="daemon_response_lost" if request_sent else "daemon_unavailable",
            ) from exc

    def execute_remote_command(
        client: Any,
        command: str,
        timeout_seconds: int,
        stdin_data: bytes | None = None,
    ) -> dict[str, Any]:
        """Выполняет команду и сохраняет доказуемую границу её возможного запуска."""
        channel = None
        command_started = False
        output: list[bytes] = []
        errors: list[bytes] = []
        sanitized_error: RemoteCommandError | None = None
        try:
            transport = client.get_transport()
            if transport is None:
                raise OSError("SSH transport отсутствует")
            channel = transport.open_session(timeout=10)

            # После открытия канала вызов exec_command может отправить запрос до ошибки,
            # поэтому с этого момента исход команды считается потенциально изменившим remote state.
            command_started = True
            channel.exec_command(command)
            if stdin_data is not None:
                channel.sendall(stdin_data)
            channel.shutdown_write()
            total_size = 0
            started = time.monotonic()

            while True:
                read_any = False
                while channel.recv_ready():
                    chunk = channel.recv(core.BUFFER_SIZE)
                    output.append(chunk)
                    total_size += len(chunk)
                    read_any = True
                while channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(core.BUFFER_SIZE)
                    errors.append(chunk)
                    total_size += len(chunk)
                    read_any = True

                stdout_text = b"".join(output).decode("utf-8", errors="replace")
                stderr_text = b"".join(errors).decode("utf-8", errors="replace")
                if total_size > core.MAX_OUTPUT_SIZE:
                    raise RemoteCommandError(
                        "Вывод удалённой команды превышает допустимый размер 4 МиБ.",
                        error_code="output_limit_exceeded",
                        command_started=True,
                        stdout=stdout_text,
                        stderr=stderr_text,
                    )
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() - started > timeout_seconds:
                    raise RemoteCommandError(
                        f"Превышено время выполнения команды: {timeout_seconds} с.",
                        error_code="command_timeout",
                        command_started=True,
                        stdout=stdout_text,
                        stderr=stderr_text,
                    )
                if not read_any:
                    time.sleep(0.01)

            exit_code = channel.recv_exit_status()
            return {
                "ok": True,
                "stdout": b"".join(output).decode("utf-8", errors="replace"),
                "stderr": b"".join(errors).decode("utf-8", errors="replace"),
                "exit_code": exit_code,
            }
        except RemoteCommandError:
            raise
        except Exception as exc:
            if command_started:
                wrapped = RemoteCommandError(
                    "SSH-канал завершился до получения достоверного результата команды.",
                    error_code="command_result_unknown",
                    command_started=True,
                    stdout=b"".join(output).decode("utf-8", errors="replace"),
                    stderr=b"".join(errors).decode("utf-8", errors="replace"),
                )
            else:
                wrapped = RemoteCommandError(
                    "Не удалось открыть канал для удалённой команды; команда не запускалась.",
                    error_code="command_not_started",
                    command_started=False,
                )

            # stdin_data сейчас используется для sudo-пароля. Не сохраняем исходное
            # исключение в chain: оно может содержать переданный секрет.
            if stdin_data is not None:
                sanitized_error = wrapped
            else:
                raise wrapped from exc
        finally:
            if channel is not None:
                channel.close()

        # Поднимаем вне except, чтобы секретное исходное исключение не осталось
        # ни в __cause__, ни в __context__.
        if sanitized_error is not None:
            raise sanitized_error
        raise AssertionError("Недостижимое состояние выполнения удалённой команды")

    core.DaemonRequestError = DaemonRequestError
    core.RemoteCommandError = RemoteCommandError
    core.request_daemon = request_daemon
    core.execute_remote_command = execute_remote_command
    core._machine_outcomes_installed = True
