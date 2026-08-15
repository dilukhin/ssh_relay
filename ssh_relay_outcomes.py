#!/usr/bin/env python3
"""Структурированные исходы daemon transport и машинный контракт коротких команд."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any

REDACTED_STDIN = "[СКРЫТО]"
MACHINE_SCHEMA_VERSION = 1
MACHINE_EXIT_SUCCEEDED = 0
MACHINE_EXIT_NOT_STARTED = 10
MACHINE_EXIT_COMMAND_FAILED = 11
MACHINE_EXIT_PARTIAL_SUCCESS = 12
MACHINE_EXIT_UNKNOWN = 13

_machine_context = threading.local()


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _reset_machine_request(message: dict[str, Any]) -> None:
    action = message.get("action")
    requested = bool(message.get("machine")) and action in {"exec", "sudo_exec"}
    _machine_context.requested = requested
    _machine_context.action = action if requested else None
    _machine_context.command_started = False
    _machine_context.error_code = None
    _machine_context.stdout = ""
    _machine_context.stderr = ""


def _record_machine_command_state(
    *,
    command_started: bool,
    error_code: str | None,
    stdout: str = "",
    stderr: str = "",
) -> None:
    if not getattr(_machine_context, "requested", False):
        return
    _machine_context.command_started = command_started
    _machine_context.error_code = error_code
    _machine_context.stdout = stdout
    _machine_context.stderr = stderr


def _enrich_machine_reply(message: dict[str, Any]) -> dict[str, Any]:
    if not getattr(_machine_context, "requested", False):
        return message
    enriched = dict(message)
    started = bool(getattr(_machine_context, "command_started", False))
    if enriched.get("ok"):
        enriched.setdefault("command_started", True if "exit_code" in enriched else started)
        return enriched

    enriched.setdefault("command_started", started)
    enriched.setdefault(
        "error_code",
        getattr(_machine_context, "error_code", None)
        or ("command_result_unknown" if started else "command_not_started"),
    )
    enriched.setdefault("stdout", str(getattr(_machine_context, "stdout", "")))
    enriched.setdefault("stderr", str(getattr(_machine_context, "stderr", "")))
    return enriched


def _subparser_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _machine_result_base(core: Any, args: argparse.Namespace, *, action: str, started_at: str) -> dict[str, Any]:
    return {
        "schema_version": MACHINE_SCHEMA_VERSION,
        "tool": "ssh_relay",
        "tool_version": str(getattr(core, "__version__", "")),
        "action": action,
        "operation_status": "not_started",
        "session": str(getattr(args, "name", getattr(core, "DEFAULT_SESSION_NAME", "default"))),
        "remote_host": None,
        "remote_port": None,
        "remote_user": None,
        "sudo": action == "sudo-exec",
        "risky": bool(getattr(args, "risky", False)),
        "command_status": "not_started",
        "command_exit_code": None,
        "receipt_status": "not_attempted" if bool(getattr(args, "risky", False)) else "not_requested",
        "partial_success": False,
        "stdout": "",
        "stderr": "",
        "output_encoding": "utf-8-replace",
        "error_code": None,
        "error_stage": None,
        "error_message": None,
        "started_at_utc": started_at,
        "finished_at_utc": None,
    }


def _apply_session_identity(result: dict[str, Any], session: dict[str, Any]) -> None:
    result["session"] = str(session.get("name") or result["session"])
    result["remote_host"] = session.get("host")
    result["remote_port"] = session.get("port")
    result["remote_user"] = session.get("user")


def _print_machine_result(result: dict[str, Any], exit_code: int) -> int:
    result["finished_at_utc"] = _utc_now()
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return exit_code


def _machine_exec_cmd(core: Any, args: argparse.Namespace, *, action: str) -> int:
    started_at = _utc_now()
    result = _machine_result_base(core, args, action=action, started_at=started_at)

    if bool(getattr(args, "risky", False)):
        result["error_code"] = "risky_machine_contract_not_ready"
        result["error_stage"] = "validation"
        result["error_message"] = (
            "Машинный режим --risky ещё не включён: безопасный receipt-контракт реализуется отдельно."
        )
        return _print_machine_result(result, MACHINE_EXIT_NOT_STARTED)

    try:
        session_name = core.validate_session_name(args.name)
    except core.RelayError as exc:
        result["error_code"] = "invalid_session"
        result["error_stage"] = "session"
        result["error_message"] = str(exc)
        return _print_machine_result(result, MACHINE_EXIT_NOT_STARTED)

    result["session"] = session_name
    try:
        session = core.read_session(session_name)
    except core.RelayError as exc:
        result["error_code"] = "session_unavailable"
        result["error_stage"] = "session"
        result["error_message"] = str(exc)
        return _print_machine_result(result, MACHINE_EXIT_NOT_STARTED)

    _apply_session_identity(result, session)
    response_timeout = (
        int(session.get("command_timeout", core.DEFAULT_COMMAND_TIMEOUT))
        + int(session.get("reconnect_wait", core.DEFAULT_RECONNECT_WAIT))
        + 10
    )
    daemon_action = "sudo_exec" if action == "sudo-exec" else "exec"
    try:
        daemon_result = core.request_daemon(
            session,
            daemon_action,
            command=args.remote_command,
            risky=False,
            receipt_path=args.receipt_path,
            machine=True,
            response_timeout=response_timeout,
        )
    except core.DaemonRequestError as exc:
        unknown = bool(exc.request_sent)
        result["operation_status"] = "unknown" if unknown else "not_started"
        result["command_status"] = "unknown" if unknown else "not_started"
        result["error_code"] = str(exc.error_code)
        result["error_stage"] = "transport"
        result["error_message"] = str(exc)
        return _print_machine_result(
            result,
            MACHINE_EXIT_UNKNOWN if unknown else MACHINE_EXIT_NOT_STARTED,
        )
    except core.DaemonUnavailableError as exc:
        result["operation_status"] = "unknown"
        result["command_status"] = "unknown"
        result["error_code"] = "daemon_response_lost"
        result["error_stage"] = "transport"
        result["error_message"] = str(exc)
        return _print_machine_result(result, MACHINE_EXIT_UNKNOWN)
    except core.RelayError as exc:
        result["operation_status"] = "unknown"
        result["command_status"] = "unknown"
        result["error_code"] = "protocol_error"
        result["error_stage"] = "transport"
        result["error_message"] = str(exc)
        return _print_machine_result(result, MACHINE_EXIT_UNKNOWN)

    result["stdout"] = str(daemon_result.get("stdout", ""))
    result["stderr"] = str(daemon_result.get("stderr", ""))
    if daemon_result.get("ok"):
        exit_code = int(daemon_result.get("exit_code", 1))
        result["command_exit_code"] = exit_code
        if exit_code == 0:
            result["operation_status"] = "succeeded"
            result["command_status"] = "succeeded"
            return _print_machine_result(result, MACHINE_EXIT_SUCCEEDED)
        result["operation_status"] = "command_failed"
        result["command_status"] = "failed"
        result["error_code"] = "remote_exit_nonzero"
        result["error_stage"] = "command"
        result["error_message"] = "Удалённая команда завершилась с ненулевым кодом."
        return _print_machine_result(result, MACHINE_EXIT_COMMAND_FAILED)

    command_started = daemon_result.get("command_started")
    if command_started is False:
        result["operation_status"] = "not_started"
        result["command_status"] = "not_started"
        process_exit = MACHINE_EXIT_NOT_STARTED
    else:
        result["operation_status"] = "unknown"
        result["command_status"] = "unknown"
        process_exit = MACHINE_EXIT_UNKNOWN
    result["error_code"] = str(
        daemon_result.get("error_code")
        or ("command_not_started" if command_started is False else "command_result_unknown")
    )
    result["error_stage"] = "command" if command_started else "daemon"
    result["error_message"] = str(daemon_result.get("protocol_error", "Неизвестная ошибка relay."))
    return _print_machine_result(result, process_exit)


def _extend_machine_parser(core: Any, parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    choices = _subparser_choices(parser)
    for name, action in (("exec", "exec"), ("sudo-exec", "sudo-exec")):
        command_parser = choices.get(name)
        if command_parser is None:
            continue
        if not any("--json" in item.option_strings for item in command_parser._actions):
            command_parser.add_argument(
                "--json",
                action="store_true",
                help="Вернуть один машиночитаемый JSON-объект вместо обычного текстового вывода.",
            )
        original_handler = command_parser.get_default("handler")

        def dispatch(args: argparse.Namespace, *, _action: str = action, _original: Any = original_handler) -> int:
            if getattr(args, "json", False):
                return _machine_exec_cmd(core, args, action=_action)
            return int(_original(args))

        command_parser.set_defaults(handler=dispatch)
    return parser


def install(core: Any) -> None:
    """Добавляет структурированные исходы и staging machine contract без изменения text-mode."""
    if getattr(core, "_machine_outcomes_installed", False):
        return
    required = ("DaemonUnavailableError", "RelayError", "request_daemon", "execute_remote_command")
    if any(not hasattr(core, name) for name in required):
        return

    original_request_daemon = core.request_daemon
    original_execute_remote_command = core.execute_remote_command
    original_read_message = getattr(core, "read_message", None)
    original_send_message = getattr(core, "send_message", None)
    original_build_parser = getattr(core, "build_parser", None)

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
            if refused is not None:
                raise DaemonRequestError(
                    str(exc),
                    request_sent=False,
                    error_code="daemon_unavailable",
                ) from refused
            raise DaemonRequestError(
                str(exc),
                request_sent=True,
                error_code="daemon_response_lost",
            ) from exc
        except core.RelayError as exc:
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
            self._state.command_started = True
            _record_machine_command_state(command_started=True, error_code=None)
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
            result = original_execute_remote_command(
                _ClientProxy(client, state),
                command,
                timeout_seconds,
                stdin_data=stdin_data,
            )
            _record_machine_command_state(
                command_started=state.command_started,
                error_code=None,
                stdout=str(result.get("stdout", "")),
                stderr=str(result.get("stderr", "")),
            )
            return result
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
            _record_machine_command_state(
                command_started=started,
                error_code=wrapped.error_code,
                stdout=stdout,
                stderr=stderr,
            )
            if stdin_data is not None:
                sanitized_error = wrapped
            else:
                raise wrapped from exc
        finally:
            if state.channel is not None and not state.channel_closed:
                state.channel.close()
                state.channel_closed = True

        if sanitized_error is not None:
            raise sanitized_error
        raise AssertionError("Недостижимое состояние выполнения удалённой команды")

    if original_read_message is not None:
        def read_message(sock: Any) -> dict[str, Any]:
            message = original_read_message(sock)
            _reset_machine_request(message)
            return message

        core.read_message = read_message

    if original_send_message is not None:
        def send_message(conn: Any, message: dict[str, Any]) -> None:
            original_send_message(conn, _enrich_machine_reply(message))

        core.send_message = send_message

    if original_build_parser is not None:
        def build_parser() -> argparse.ArgumentParser:
            return _extend_machine_parser(core, original_build_parser())

        core.build_parser = build_parser

    core.DaemonRequestError = DaemonRequestError
    core.RemoteCommandError = RemoteCommandError
    core.request_daemon = request_daemon
    core.execute_remote_command = execute_remote_command
    core._machine_outcomes_installed = True
