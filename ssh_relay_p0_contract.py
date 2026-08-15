#!/usr/bin/env python3
"""Финальная связка machine contract и safe risky receipt для коротких команд."""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any

import ssh_relay_outcomes as outcomes
import ssh_relay_receipts as receipts


def normalize_receipt_id(core: Any, value: object | None) -> str:
    """Возвращает канонический UUID receipt, создавая его до risky-запроса при необходимости."""
    if value is None or value == "":
        return str(uuid.uuid4())
    if not isinstance(value, str):
        raise core.RelayError("receipt_id должен быть UUID.")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise core.RelayError("receipt_id должен быть UUID.") from exc


def _subparser_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _apply_receipt_summary(
    result: dict[str, Any],
    receipt: object,
    *,
    fallback_status: str,
    transaction_id: str | None,
    receipt_id: str | None,
    receipt_path: str | None,
) -> dict[str, Any]:
    summary = receipt if isinstance(receipt, dict) else {}
    result["receipt_status"] = str(summary.get("receipt_status") or fallback_status)
    result["transaction_id"] = summary.get("transaction_id") or transaction_id
    result["receipt_id"] = summary.get("receipt_id") or receipt_id
    result["receipt_hash"] = summary.get("receipt_hash")
    result["receipt_path"] = summary.get("receipt_path") or receipt_path
    return summary


def _machine_risky_cmd(core: Any, args: argparse.Namespace, *, action: str) -> int:
    started_at = outcomes._utc_now()
    result = outcomes._machine_result_base(core, args, action=action, started_at=started_at)
    result.update(
        {
            "transaction_id": None,
            "receipt_id": None,
            "receipt_hash": None,
            "receipt_path": getattr(args, "receipt_path", None),
            "change_target": getattr(args, "change_target", None),
            "change_description": getattr(args, "change_description", None),
        }
    )

    try:
        receipt_path = receipts.validate_receipt_path(core, getattr(args, "receipt_path", ""))
        transaction_id = receipts.normalize_transaction_id(core, getattr(args, "transaction_id", None))
        receipt_id = normalize_receipt_id(core, None)
        change_target = receipts.normalize_optional_text(
            core,
            getattr(args, "change_target", None),
            field="change_target",
            max_bytes=receipts.MAX_CHANGE_TARGET_BYTES,
        )
        change_description = receipts.normalize_optional_text(
            core,
            getattr(args, "change_description", None),
            field="change_description",
            max_bytes=receipts.MAX_CHANGE_DESCRIPTION_BYTES,
        )
    except core.RelayError as exc:
        result["error_code"] = "invalid_risky_metadata"
        result["error_stage"] = "validation"
        result["error_message"] = str(exc)
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_NOT_STARTED)

    result["transaction_id"] = transaction_id
    result["receipt_id"] = receipt_id
    result["receipt_path"] = receipt_path
    result["change_target"] = change_target
    result["change_description"] = change_description

    try:
        session_name = core.validate_session_name(args.name)
    except core.RelayError as exc:
        result["error_code"] = "invalid_session"
        result["error_stage"] = "session"
        result["error_message"] = str(exc)
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_NOT_STARTED)
    result["session"] = session_name

    try:
        session = core.read_session(session_name)
    except core.RelayError as exc:
        result["error_code"] = "session_unavailable"
        result["error_stage"] = "session"
        result["error_message"] = str(exc)
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_NOT_STARTED)
    outcomes._apply_session_identity(result, session)

    # Capability проверяется отдельным read-only запросом до отправки пользовательской команды.
    try:
        status = core.request_daemon(session, "status", response_timeout=5)
    except core.RelayError as exc:
        result["error_code"] = "receipt_capability_unconfirmed"
        result["error_stage"] = "capability"
        result["error_message"] = str(exc)
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_NOT_STARTED)
    if not status.get("ok") or status.get("receipt_schema_version") != receipts.RECEIPT_SCHEMA_VERSION:
        result["error_code"] = "receipt_capability_missing"
        result["error_stage"] = "capability"
        result["error_message"] = (
            "Активный daemon не подтвердил safe receipt v1. Остановите его и запустите заново текущим relay."
        )
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_NOT_STARTED)

    response_timeout = (
        int(session.get("command_timeout", core.DEFAULT_COMMAND_TIMEOUT))
        + int(session.get("reconnect_wait", core.DEFAULT_RECONNECT_WAIT))
        + 10
    )
    daemon_action = "sudo_exec" if action == "sudo-exec" else "exec"
    metadata = {
        "transaction_id": transaction_id,
        "change_target": change_target,
        "change_description": change_description,
    }
    receipts._client_context.metadata = metadata
    try:
        daemon_result = core.request_daemon(
            session,
            daemon_action,
            command=args.remote_command,
            risky=True,
            receipt_path=receipt_path,
            receipt_id=receipt_id,
            machine=True,
            response_timeout=response_timeout,
        )
    except core.DaemonRequestError as exc:
        unknown = bool(exc.request_sent)
        result["operation_status"] = "unknown" if unknown else "not_started"
        result["command_status"] = "unknown" if unknown else "not_started"
        result["receipt_status"] = "unknown" if unknown else "not_attempted"
        result["error_code"] = str(exc.error_code)
        result["error_stage"] = "transport"
        result["error_message"] = str(exc)
        return outcomes._print_machine_result(
            result,
            outcomes.MACHINE_EXIT_UNKNOWN if unknown else outcomes.MACHINE_EXIT_NOT_STARTED,
        )
    except core.DaemonUnavailableError as exc:
        result["operation_status"] = "unknown"
        result["command_status"] = "unknown"
        result["receipt_status"] = "unknown"
        result["error_code"] = "daemon_response_lost"
        result["error_stage"] = "transport"
        result["error_message"] = str(exc)
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_UNKNOWN)
    except core.RelayError as exc:
        result["operation_status"] = "unknown"
        result["command_status"] = "unknown"
        result["receipt_status"] = "unknown"
        result["error_code"] = "protocol_error"
        result["error_stage"] = "transport"
        result["error_message"] = str(exc)
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_UNKNOWN)
    finally:
        receipts._client_context.metadata = None

    if daemon_result.get("ok"):
        result["stdout"] = str(daemon_result.get("stdout", ""))
        result["stderr"] = str(daemon_result.get("stderr", ""))
        exit_code = int(daemon_result.get("exit_code", 1))
        result["command_exit_code"] = exit_code
        if exit_code != 0:
            result["operation_status"] = "command_failed"
            result["command_status"] = "failed"
            result["receipt_status"] = "not_attempted"
            result["error_code"] = "remote_exit_nonzero"
            result["error_stage"] = "command"
            result["error_message"] = "Удалённая команда завершилась с ненулевым кодом; receipt не создавался."
            return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_COMMAND_FAILED)

        result["command_status"] = "succeeded"
        receipt = _apply_receipt_summary(
            result,
            daemon_result.get("risky_receipt"),
            fallback_status="unknown",
            transaction_id=transaction_id,
            receipt_id=receipt_id,
            receipt_path=receipt_path,
        )
        if result["receipt_status"] == "succeeded":
            result["operation_status"] = "succeeded"
            return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_SUCCEEDED)
        result["operation_status"] = "partial_success"
        result["partial_success"] = True
        result["error_stage"] = "receipt"
        result["error_code"] = str(receipt.get("error_code") or "receipt_result_unknown")
        result["error_message"] = str(
            receipt.get("error_message") or "Команда выполнена, но safe receipt не подтверждён; повтор запрещён."
        )
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_PARTIAL_SUCCESS)

    command_result = daemon_result.get("command_result")
    receipt_result = daemon_result.get("receipt_result")
    if isinstance(command_result, dict) and command_result.get("ok"):
        result["stdout"] = str(command_result.get("stdout", ""))
        result["stderr"] = str(command_result.get("stderr", ""))
        command_exit = int(command_result.get("exit_code", 1))
        result["command_exit_code"] = command_exit
        if command_exit != 0:
            result["operation_status"] = "command_failed"
            result["command_status"] = "failed"
            result["receipt_status"] = "not_attempted"
            result["error_code"] = "remote_exit_nonzero"
            result["error_stage"] = "command"
            result["error_message"] = "Удалённая команда завершилась с ненулевым кодом; receipt не создавался."
            return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_COMMAND_FAILED)

        result["command_status"] = "succeeded"
        receipt = _apply_receipt_summary(
            result,
            receipt_result,
            fallback_status="unknown",
            transaction_id=transaction_id,
            receipt_id=receipt_id,
            receipt_path=receipt_path,
        )
        result["operation_status"] = "partial_success"
        result["partial_success"] = True
        result["error_stage"] = "receipt"
        result["error_code"] = str(receipt.get("error_code") or "receipt_result_unknown")
        result["error_message"] = str(
            receipt.get("error_message")
            or daemon_result.get("protocol_error")
            or "Команда выполнена, но safe receipt не подтверждён; повтор запрещён."
        )
        return outcomes._print_machine_result(result, outcomes.MACHINE_EXIT_PARTIAL_SUCCESS)

    command_started = daemon_result.get("command_started")
    if command_started is False:
        result["operation_status"] = "not_started"
        result["command_status"] = "not_started"
        result["receipt_status"] = "not_attempted"
        process_exit = outcomes.MACHINE_EXIT_NOT_STARTED
    else:
        result["operation_status"] = "unknown"
        result["command_status"] = "unknown"
        result["receipt_status"] = "unknown"
        process_exit = outcomes.MACHINE_EXIT_UNKNOWN
    result["stdout"] = str(daemon_result.get("stdout", ""))
    result["stderr"] = str(daemon_result.get("stderr", ""))
    result["error_code"] = str(
        daemon_result.get("error_code")
        or ("command_not_started" if command_started is False else "command_result_unknown")
    )
    result["error_stage"] = "command" if command_started else "daemon"
    result["error_message"] = str(daemon_result.get("protocol_error", "Неизвестная ошибка relay."))
    return outcomes._print_machine_result(result, process_exit)


def install(core: Any) -> None:
    """Включает risky machine contract поверх outcomes/receipts, не меняя text-mode."""
    if getattr(core, "_p0_contract_installed", False):
        return
    required = ("build_parser", "request_daemon", "read_message", "execute_risky_receipt")
    if any(not hasattr(core, name) for name in required):
        return

    original_request_daemon = core.request_daemon
    original_read_message = core.read_message
    original_execute_risky_receipt = core.execute_risky_receipt
    original_build_parser = core.build_parser

    def request_daemon(
        session: dict[str, Any],
        action: str,
        *,
        response_timeout: float | None = 5,
        **payload: Any,
    ) -> dict[str, Any]:
        prepared = dict(payload)
        if action in {"exec", "sudo_exec"} and prepared.get("risky") is True:
            prepared["receipt_id"] = normalize_receipt_id(core, prepared.get("receipt_id"))
        return original_request_daemon(
            session,
            action,
            response_timeout=response_timeout,
            **prepared,
        )

    def read_message(sock: Any) -> dict[str, Any]:
        message = original_read_message(sock)
        action = message.get("action")
        schema = message.get("receipt_schema_version")
        if action in {"exec", "sudo_exec"} and schema == receipts.RECEIPT_SCHEMA_VERSION:
            if not message.get("receipt_id"):
                raise core.RelayError("Safe receipt v1 требует receipt_id, созданный до отправки risky-команды.")
            receipt_id = normalize_receipt_id(core, message.get("receipt_id"))
            metadata = getattr(receipts._receipt_context, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}
                receipts._receipt_context.metadata = metadata
            metadata["receipt_id"] = receipt_id
        elif action in {"exec", "sudo_exec"} and message.get("risky") is True:
            metadata = getattr(receipts._receipt_context, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}
                receipts._receipt_context.metadata = metadata
            metadata.setdefault("receipt_id", normalize_receipt_id(core, None))
        return message

    def execute_risky_receipt(
        client: Any,
        *,
        session: dict[str, Any],
        action: str,
        command: str,
        sudo: bool,
        receipt_path: str,
        timeout_seconds: int,
        sudo_password: str | None,
    ) -> dict[str, Any]:
        path = receipts.validate_receipt_path(core, receipt_path)
        metadata = getattr(receipts._receipt_context, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {
                "transaction_id": receipts.normalize_transaction_id(core, None),
                "receipt_id": normalize_receipt_id(core, None),
                "change_target": None,
                "change_description": None,
            }
        transaction_id = receipts.normalize_transaction_id(core, metadata.get("transaction_id"))
        receipt_id = normalize_receipt_id(core, metadata.get("receipt_id"))
        payload = receipts.build_receipt_payload(
            core,
            session=session,
            action=action,
            command=command,
            sudo=sudo,
            transaction_id=transaction_id,
            receipt_id=receipt_id,
            change_target=metadata.get("change_target"),
            change_description=metadata.get("change_description"),
        )
        writer = receipts.build_writer_command(core, path=path, payload=payload)
        if sudo and sudo_password is None:
            raise core.RelayError("Нельзя записать sudo receipt: sudo-пароль отсутствует в памяти daemon.")
        try:
            if sudo:
                command_result = core.execute_sudo_command(client, writer, timeout_seconds, sudo_password)
            else:
                command_result = core.execute_remote_command(client, writer, timeout_seconds)
        except core.RelayError as exc:
            unknown = bool(getattr(exc, "command_started", False))
            result = receipts._summary(
                path=path,
                payload=payload,
                receipt_status="unknown" if unknown else "failed",
                exit_code=1,
                error_code="receipt_write_unknown" if unknown else str(getattr(exc, "error_code", "receipt_write_not_started")),
                error_message=(
                    "Результат записи receipt неизвестен; автоматический повтор запрещён."
                    if unknown
                    else "Запись receipt достоверно не была запущена."
                ),
            )
            receipts._receipt_context.last_result = result
            return result

        exit_code = int(command_result.get("exit_code", 1))
        if exit_code == 0:
            result = receipts._summary(path=path, payload=payload, receipt_status="succeeded", exit_code=0)
        else:
            error_code = receipts.WRITER_ERROR_CODES.get(exit_code, "receipt_writer_failed")
            messages = {
                "receipt_path_symlink": "Receipt-файл является symlink; запись отклонена.",
                "receipt_path_not_regular": "Receipt-путь существует, но не является обычным файлом.",
                "receipt_parent_unavailable": "Не удалось подготовить каталог receipt.",
                "receipt_create_failed": "Не удалось безопасно создать receipt-файл.",
                "receipt_permissions_failed": "Не удалось установить права 0600 для receipt-файла.",
                "duplicate_transaction_id": "Receipt с таким transaction_id уже существует; повторная запись отклонена.",
                "receipt_duplicate_scan_failed": "Не удалось проверить журнал на повтор transaction_id.",
                "receipt_append_failed": "Не удалось добавить receipt в журнал.",
                "receipt_verify_failed": "Добавленная receipt-строка не прошла контрольное чтение.",
                "receipt_writer_failed": f"Удалённый writer receipt завершился с кодом {exit_code}.",
            }
            result = receipts._summary(
                path=path,
                payload=payload,
                receipt_status="failed",
                exit_code=exit_code,
                error_code=error_code,
                error_message=messages[error_code],
            )
        receipts._receipt_context.last_result = result
        return result

    def build_parser() -> argparse.ArgumentParser:
        parser = original_build_parser()
        for name, action_name in (("exec", "exec"), ("sudo-exec", "sudo-exec")):
            command_parser = _subparser_choices(parser).get(name)
            if command_parser is None:
                continue
            original_handler = command_parser.get_default("handler")

            def dispatch(
                args: argparse.Namespace,
                *,
                _action: str = action_name,
                _original: Any = original_handler,
            ) -> int:
                if getattr(args, "json", False) and getattr(args, "risky", False):
                    return _machine_risky_cmd(core, args, action=_action)
                return int(_original(args))

            command_parser.set_defaults(handler=dispatch)
        return parser

    core.request_daemon = request_daemon
    core.read_message = read_message
    core.execute_risky_receipt = execute_risky_receipt
    core.build_parser = build_parser
    core.normalize_receipt_id = lambda value=None: normalize_receipt_id(core, value)
    core._p0_contract_installed = True
