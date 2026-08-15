#!/usr/bin/env python3
"""Безопасный risky receipt v1 для коротких exec/sudo-exec операций."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

RECEIPT_SCHEMA_VERSION = 1
MAX_RECEIPT_PATH_BYTES = 4096
MAX_TRANSACTION_ID_LENGTH = 128
MAX_CHANGE_TARGET_BYTES = 512
MAX_CHANGE_DESCRIPTION_BYTES = 2048
TRANSACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RECEIPT_COMMAND_PLACEHOLDER = "safe-writer-v1"

WRITER_EXIT_SYMLINK = 71
WRITER_EXIT_NOT_REGULAR = 72
WRITER_EXIT_PARENT = 73
WRITER_EXIT_CREATE = 74
WRITER_EXIT_CHMOD = 75
WRITER_EXIT_DUPLICATE = 76
WRITER_EXIT_SCAN = 77
WRITER_EXIT_APPEND = 78
WRITER_EXIT_VERIFY = 79

WRITER_ERROR_CODES = {
    WRITER_EXIT_SYMLINK: "receipt_path_symlink",
    WRITER_EXIT_NOT_REGULAR: "receipt_path_not_regular",
    WRITER_EXIT_PARENT: "receipt_parent_unavailable",
    WRITER_EXIT_CREATE: "receipt_create_failed",
    WRITER_EXIT_CHMOD: "receipt_permissions_failed",
    WRITER_EXIT_DUPLICATE: "duplicate_transaction_id",
    WRITER_EXIT_SCAN: "receipt_duplicate_scan_failed",
    WRITER_EXIT_APPEND: "receipt_append_failed",
    WRITER_EXIT_VERIFY: "receipt_verify_failed",
}

_client_context = threading.local()
_receipt_context = threading.local()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _contains_control(text: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def validate_receipt_path(core: Any, path: object) -> str:
    if not isinstance(path, str) or not path.strip():
        raise core.RelayError("Путь receipt-файла не должен быть пустым.")
    if path.endswith("/"):
        raise core.RelayError("Путь receipt должен указывать на файл, а не на каталог.")
    if _contains_control(path):
        raise core.RelayError("Путь receipt содержит управляющие символы.")
    if len(path.encode("utf-8")) > MAX_RECEIPT_PATH_BYTES:
        raise core.RelayError(f"Путь receipt превышает лимит {MAX_RECEIPT_PATH_BYTES} байт UTF-8.")
    return path


def normalize_transaction_id(core: Any, value: object | None) -> str:
    if value is None or value == "":
        return str(uuid.uuid4())
    if not isinstance(value, str) or not TRANSACTION_ID_PATTERN.fullmatch(value):
        raise core.RelayError(
            "transaction_id должен содержать 1-128 ASCII-символов: буквы, цифры, точку, дефис, подчёркивание или двоеточие."
        )
    return value


def normalize_optional_text(core: Any, value: object | None, *, field: str, max_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise core.RelayError(f"{field} не должен быть пустым, если параметр задан.")
    if _contains_control(value):
        raise core.RelayError(f"{field} содержит управляющие символы.")
    if len(value.encode("utf-8")) > max_bytes:
        raise core.RelayError(f"{field} превышает лимит {max_bytes} байт UTF-8.")
    return value


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_receipt_payload(
    core: Any,
    *,
    session: dict[str, Any],
    action: str,
    command: str,
    sudo: bool,
    transaction_id: str,
    receipt_id: str,
    change_target: str | None,
    change_description: str | None,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    normalized_action = "sudo-exec" if sudo or action == "sudo_exec" else "exec"
    payload: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "timestamp_utc": timestamp_utc or _utc_now(),
        "tool": "ssh_relay",
        "tool_version": str(getattr(core, "__version__", "")),
        "session": str(session.get("name") or getattr(core, "DEFAULT_SESSION_NAME", "default")),
        "remote_host": str(session.get("host", "")),
        "remote_port": int(session.get("port", 22)),
        "remote_user": str(session.get("user", "")),
        "action": normalized_action,
        "sudo": bool(sudo),
        "transaction_id": transaction_id,
        "receipt_id": receipt_id,
        "change_target": change_target,
        "change_description": change_description,
        "command_status": "succeeded",
        "command_exit_code": 0,
        "command_hash": hashlib.sha256(command.encode("utf-8")).hexdigest(),
    }
    payload["receipt_hash"] = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return payload


def build_writer_command(core: Any, *, path: str, payload: dict[str, Any]) -> str:
    path = validate_receipt_path(core, path)
    transaction_id = normalize_transaction_id(core, payload.get("transaction_id"))
    line = canonical_json(payload)
    directory = core.posixpath.dirname(path.rstrip("/")) or "."
    path_q = core.quote_posix_path(path)
    directory_q = core.quote_posix_path(directory)
    line_q = shlex.quote(line)
    needle_q = shlex.quote(f'"transaction_id":"{transaction_id}"')
    return (
        "umask 077; "
        f"mkdir -p {directory_q} 2>/dev/null || exit {WRITER_EXIT_PARENT}; "
        f"if [ -L {path_q} ]; then exit {WRITER_EXIT_SYMLINK}; fi; "
        f"if [ -e {path_q} ] && [ ! -f {path_q} ]; then exit {WRITER_EXIT_NOT_REGULAR}; fi; "
        f"if [ ! -e {path_q} ]; then : >> {path_q} 2>/dev/null || exit {WRITER_EXIT_CREATE}; fi; "
        f"chmod 600 {path_q} 2>/dev/null || exit {WRITER_EXIT_CHMOD}; "
        f"grep -F -q -- {needle_q} {path_q} 2>/dev/null; receipt_grep=$?; "
        f"if [ \"$receipt_grep\" -eq 0 ]; then exit {WRITER_EXIT_DUPLICATE}; fi; "
        f"if [ \"$receipt_grep\" -ne 1 ]; then exit {WRITER_EXIT_SCAN}; fi; "
        f"printf '%s\\n' {line_q} >> {path_q} 2>/dev/null || exit {WRITER_EXIT_APPEND}; "
        f"receipt_last=$(tail -n 1 -- {path_q} 2>/dev/null) || exit {WRITER_EXIT_VERIFY}; "
        f"[ \"$receipt_last\" = {line_q} ] || exit {WRITER_EXIT_VERIFY}; "
        "exit 0"
    )


def _summary(
    *,
    path: str,
    payload: dict[str, Any] | None,
    receipt_status: str,
    exit_code: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_status": receipt_status,
        "receipt_path": path,
        "transaction_id": payload.get("transaction_id") if payload else None,
        "receipt_id": payload.get("receipt_id") if payload else None,
        "receipt_hash": payload.get("receipt_hash") if payload else None,
        "exit_code": int(exit_code),
        "error_code": error_code,
        "error_message": error_message,
        "receipt_command": RECEIPT_COMMAND_PLACEHOLDER,
    }


def _metadata_from_request(core: Any, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "transaction_id": normalize_transaction_id(core, message.get("transaction_id")),
        "change_target": normalize_optional_text(
            core, message.get("change_target"), field="change_target", max_bytes=MAX_CHANGE_TARGET_BYTES
        ),
        "change_description": normalize_optional_text(
            core,
            message.get("change_description"),
            field="change_description",
            max_bytes=MAX_CHANGE_DESCRIPTION_BYTES,
        ),
    }


def _metadata_from_args(core: Any, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "transaction_id": normalize_transaction_id(core, getattr(args, "transaction_id", None)),
        "change_target": normalize_optional_text(
            core, getattr(args, "change_target", None), field="change_target", max_bytes=MAX_CHANGE_TARGET_BYTES
        ),
        "change_description": normalize_optional_text(
            core,
            getattr(args, "change_description", None),
            field="change_description",
            max_bytes=MAX_CHANGE_DESCRIPTION_BYTES,
        ),
    }


def prepare_safe_request_payload(core: Any, payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(payload)
    prepared["risky"] = False
    prepared["receipt_schema_version"] = RECEIPT_SCHEMA_VERSION
    prepared["transaction_id"] = normalize_transaction_id(core, metadata.get("transaction_id"))
    prepared["change_target"] = normalize_optional_text(
        core, metadata.get("change_target"), field="change_target", max_bytes=MAX_CHANGE_TARGET_BYTES
    )
    prepared["change_description"] = normalize_optional_text(
        core,
        metadata.get("change_description"),
        field="change_description",
        max_bytes=MAX_CHANGE_DESCRIPTION_BYTES,
    )
    return prepared


def _receipt_public_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": result.get("schema_version", RECEIPT_SCHEMA_VERSION),
        "receipt_status": result.get("receipt_status"),
        "receipt_path": result.get("receipt_path"),
        "transaction_id": result.get("transaction_id"),
        "receipt_id": result.get("receipt_id"),
        "receipt_hash": result.get("receipt_hash"),
        "exit_code": result.get("exit_code"),
        "error_code": result.get("error_code"),
        "error_message": result.get("error_message"),
    }


def install(core: Any) -> None:
    if getattr(core, "_safe_receipts_installed", False):
        return
    required = (
        "RelayError",
        "request_daemon",
        "read_message",
        "send_message",
        "execute_risky_receipt",
        "build_risky_receipt_command",
        "build_parser",
        "exec_cmd",
        "sudo_exec_cmd",
        "read_session",
    )
    if any(not hasattr(core, name) for name in required):
        return

    original_request_daemon = core.request_daemon
    original_read_message = core.read_message
    original_send_message = core.send_message
    original_exec_cmd = core.exec_cmd
    original_sudo_exec_cmd = core.sudo_exec_cmd
    original_build_parser = core.build_parser

    def safe_build_risky_receipt_command(*, path: str, session: dict[str, Any], action: str, command: str, sudo: bool) -> str:
        payload = build_receipt_payload(
            core,
            session=session,
            action=action,
            command=command,
            sudo=sudo,
            transaction_id=str(uuid.uuid4()),
            receipt_id=str(uuid.uuid4()),
            change_target=None,
            change_description=None,
        )
        return build_writer_command(core, path=path, payload=payload)

    def safe_execute_risky_receipt(
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
        path = validate_receipt_path(core, receipt_path)
        metadata = getattr(_receipt_context, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {
                "transaction_id": normalize_transaction_id(core, None),
                "change_target": None,
                "change_description": None,
            }
        payload = build_receipt_payload(
            core,
            session=session,
            action=action,
            command=command,
            sudo=sudo,
            transaction_id=str(metadata["transaction_id"]),
            receipt_id=str(uuid.uuid4()),
            change_target=metadata.get("change_target"),
            change_description=metadata.get("change_description"),
        )
        writer = build_writer_command(core, path=path, payload=payload)
        if sudo and sudo_password is None:
            raise core.RelayError("Нельзя записать sudo receipt: sudo-пароль отсутствует в памяти daemon.")
        try:
            if sudo:
                command_result = core.execute_sudo_command(client, writer, timeout_seconds, sudo_password)
            else:
                command_result = core.execute_remote_command(client, writer, timeout_seconds)
        except core.RelayError as exc:
            unknown = bool(getattr(exc, "command_started", False))
            result = _summary(
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
            _receipt_context.last_result = result
            return result

        exit_code = int(command_result.get("exit_code", 1))
        if exit_code == 0:
            result = _summary(path=path, payload=payload, receipt_status="succeeded", exit_code=0)
        else:
            error_code = WRITER_ERROR_CODES.get(exit_code, "receipt_writer_failed")
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
            result = _summary(
                path=path,
                payload=payload,
                receipt_status="failed",
                exit_code=exit_code,
                error_code=error_code,
                error_message=messages[error_code],
            )
        _receipt_context.last_result = result
        return result

    def receipt_read_message(sock: Any) -> dict[str, Any]:
        message = original_read_message(sock)
        _receipt_context.action = message.get("action")
        _receipt_context.last_result = None
        _receipt_context.metadata = None
        action = message.get("action")
        if action is None:
            return message
        legacy_risky = message.get("risky") is True
        schema = message.get("receipt_schema_version")
        safe_risky = schema is not None
        if not legacy_risky and not safe_risky:
            return message
        if action not in {"exec", "sudo_exec"}:
            raise core.RelayError("Safe receipt допустим только для exec и sudo-exec.")
        if safe_risky and schema != RECEIPT_SCHEMA_VERSION:
            raise core.RelayError("Неподдерживаемая версия safe receipt protocol.")
        path = validate_receipt_path(core, message.get("receipt_path"))
        metadata = _metadata_from_request(core, message)
        _receipt_context.metadata = metadata
        translated = dict(message)
        translated["receipt_path"] = path
        translated["transaction_id"] = metadata["transaction_id"]
        translated["change_target"] = metadata["change_target"]
        translated["change_description"] = metadata["change_description"]
        if safe_risky:
            translated["risky"] = True
        return translated

    def receipt_send_message(conn: Any, message: dict[str, Any]) -> None:
        enriched = dict(message)
        if getattr(_receipt_context, "action", None) == "status" and enriched.get("ok"):
            enriched.setdefault("receipt_schema_version", RECEIPT_SCHEMA_VERSION)
        last_result = getattr(_receipt_context, "last_result", None)
        if isinstance(last_result, dict):
            public = _receipt_public_summary(last_result)
            command_result = enriched.get("command_result")
            if isinstance(command_result, dict):
                safe_command_result = dict(command_result)
                safe_command_result["risky_receipt"] = public
                enriched["command_result"] = safe_command_result
                enriched["receipt_result"] = public
            elif enriched.get("ok") and isinstance(enriched.get("risky_receipt"), dict):
                enriched["risky_receipt"] = public
        original_send_message(conn, enriched)

    def receipt_request_daemon(
        session: dict[str, Any], action: str, *, response_timeout: float | None = 5, **payload: Any
    ) -> dict[str, Any]:
        safe_requested = action in {"exec", "sudo_exec"} and payload.get("risky") is True
        request_payload = dict(payload)
        if safe_requested:
            metadata = getattr(_client_context, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {
                    "transaction_id": normalize_transaction_id(core, None),
                    "change_target": None,
                    "change_description": None,
                }
            request_payload = prepare_safe_request_payload(core, request_payload, metadata)
        result = original_request_daemon(session, action, response_timeout=response_timeout, **request_payload)
        if not safe_requested or not result.get("ok") or int(result.get("exit_code", 1)) != 0:
            return result
        receipt = result.get("risky_receipt")
        if isinstance(receipt, dict) and receipt.get("receipt_status") == "succeeded":
            return result
        return {
            "ok": False,
            "protocol_error": (
                "Удалённая команда выполнена, но безопасный receipt не подтверждён; "
                "автоматический повтор запрещён. Перезапустите daemon текущей версией relay."
            ),
            "command_result": dict(result),
            "receipt_result": {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "receipt_status": "unknown",
                "receipt_path": request_payload.get("receipt_path"),
                "transaction_id": request_payload.get("transaction_id"),
                "receipt_id": None,
                "receipt_hash": None,
                "exit_code": 1,
                "error_code": "receipt_capability_missing",
                "error_message": "Daemon не подтвердил safe receipt v1.",
            },
        }

    def _text_dispatch(original_handler: Any, args: argparse.Namespace) -> int:
        risky = bool(getattr(args, "risky", False))
        metadata_values = (
            getattr(args, "transaction_id", None),
            getattr(args, "change_target", None),
            getattr(args, "change_description", None),
        )
        if not risky:
            if any(value is not None for value in metadata_values):
                print("Параметры transaction/change допустимы только вместе с --risky.", file=sys.stderr)
                return 2
            return int(original_handler(args))
        try:
            validate_receipt_path(core, getattr(args, "receipt_path", ""))
            metadata = _metadata_from_args(core, args)
            session_name = core.validate_session_name(args.name)
            session = core.read_session(session_name)
            status = core.request_daemon(session, "status")
        except core.RelayError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not status.get("ok") or status.get("receipt_schema_version") != RECEIPT_SCHEMA_VERSION:
            print(
                "Активный daemon не подтвердил safe receipt v1. Остановите его и запустите заново текущим relay; "
                "risky-команда не отправлена.",
                file=sys.stderr,
            )
            return 1
        _client_context.metadata = metadata
        try:
            return int(original_handler(args))
        finally:
            _client_context.metadata = None

    def safe_exec_cmd(args: argparse.Namespace) -> int:
        return _text_dispatch(original_exec_cmd, args)

    def safe_sudo_exec_cmd(args: argparse.Namespace) -> int:
        return _text_dispatch(original_sudo_exec_cmd, args)

    def safe_build_parser() -> argparse.ArgumentParser:
        parser = original_build_parser()
        choices: dict[str, argparse.ArgumentParser] = {}
        for parser_action in parser._actions:
            if isinstance(parser_action, argparse._SubParsersAction):
                choices = parser_action.choices
                break
        for name in ("exec", "sudo-exec"):
            command_parser = choices.get(name)
            if command_parser is None:
                continue
            existing = {option for item in command_parser._actions for option in item.option_strings}
            if "--transaction-id" not in existing:
                command_parser.add_argument(
                    "--transaction-id", help="Идентификатор risky-транзакции; если не задан, генерируется UUID."
                )
            if "--change-target" not in existing:
                command_parser.add_argument(
                    "--change-target", help="Краткое безопасное описание объекта изменения без секретов."
                )
            if "--change-description" not in existing:
                command_parser.add_argument(
                    "--change-description", help="Краткое безопасное описание изменения без секретов."
                )
        return parser

    core.build_risky_receipt_command = safe_build_risky_receipt_command
    core.execute_risky_receipt = safe_execute_risky_receipt
    core.read_message = receipt_read_message
    core.send_message = receipt_send_message
    core.request_daemon = receipt_request_daemon
    core.exec_cmd = safe_exec_cmd
    core.sudo_exec_cmd = safe_sudo_exec_cmd
    core.build_parser = safe_build_parser
    core.RECEIPT_SCHEMA_VERSION = RECEIPT_SCHEMA_VERSION
    core._safe_receipts_installed = True
