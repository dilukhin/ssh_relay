"""CLI и связка replay с существующим коротким выполнением."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from contextlib import contextmanager

from ssh_relay_replay import ReplayError, Store, request_id
from ssh_relay_replay_platform import owner_chain

_capture = threading.local()
_client = threading.local()


def capture_chunk(name: str, chunk: bytes) -> None:
    writer = getattr(_capture, "writer", None)
    if writer is not None:
        writer.capture(name, chunk)


@contextmanager
def capturing(writer):
    previous = getattr(_capture, "writer", None)
    _capture.writer = writer
    try:
        yield
    finally:
        _capture.writer = previous


def validate_request(request: dict) -> None:
    if "request_id" in request:
        request["request_id"] = request_id(request["request_id"])
    if "no_replay" in request and type(request["no_replay"]) is not bool:
        raise ReplayError("Некорректный флаг no_replay.")


def begin(core, request: dict, session: dict):
    # Внутренние job/receipt-вызовы и старые клиенты не передают request_id.
    if not request.get("request_id") or request.get("no_replay"):
        return None
    try:
        return Store(core.state_directory()).begin(
            rid=request["request_id"], session=session["name"],
            action="sudo-exec" if request["action"] == "sudo_exec" else "exec",
            command=request["command"], risky=bool(request.get("risky")), owners=request.get("owner_chain", []))
    except (OSError, ReplayError, ValueError):
        return None


def finish(writer, request: dict, result: dict) -> dict:
    if not request.get("request_id"):
        return result
    enriched = dict(result)
    enriched.update(request_id=request["request_id"], replay_status="disabled" if request.get("no_replay") else "unavailable",
                    replay_truncated=False)
    if writer is None:
        return enriched
    command = result.get("command_result", result)
    code = command.get("exit_code") if command.get("ok") else None
    status = "unknown"
    command_status = "unknown"
    receipt_status = "not_attempted" if request.get("risky") else "not_requested"
    if code is not None:
        command_status = "succeeded" if code == 0 else "failed"
        status = "succeeded" if code == 0 else "command_failed"
        if request.get("risky") and code == 0:
            # Публичная сводка receipt ещё обогащается send_message-обёрткой.
            from ssh_relay_receipts import _receipt_context
            receipt = getattr(_receipt_context, "last_result", None) or result.get("receipt_result") or command.get("risky_receipt") or {}
            receipt_status = receipt.get("receipt_status", "unknown")
            if receipt_status != "succeeded":
                status = "partial_success"
    elif result.get("command_started") is False:
        status = command_status = "not_started"
    enriched.update(writer.finish(operation_status=status, command_status=command_status,
                                 command_exit_code=code, receipt_status=receipt_status))
    return enriched


def client_fields() -> dict:
    context = getattr(_client, "value", None)
    if context is None:
        return {}
    return {key: context[key] for key in ("request_id", "replay_status", "replay_truncated")}


def result_fields(result: dict) -> None:
    context = getattr(_client, "value", None)
    if context is not None:
        for key in ("replay_status", "replay_truncated"):
            if key in result:
                context[key] = result[key]


def install(core) -> None:
    if getattr(core, "_replay_client_installed", False):
        return
    original = core.request_daemon

    def request(session, action, *, response_timeout=5, **payload):
        context = getattr(_client, "value", None)
        if action in {"exec", "sudo_exec"} and context is not None:
            payload.update(request_id=context["request_id"], no_replay=context["no_replay"], owner_chain=context["owner_chain"])
        result = original(session, action, response_timeout=response_timeout, **payload)
        if action in {"exec", "sudo_exec"}:
            result_fields(result)
        return result

    core.request_daemon = request
    core._replay_client_installed = True


def _uuid(value: str) -> str:
    try:
        return request_id(value)
    except ReplayError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def extend_parser(core, subparsers) -> None:
    for name in ("exec", "sudo-exec"):
        parser = subparsers.choices[name]
        parser.add_argument("--request-id", type=_uuid, help="UUIDv4 запроса для локального replay; по умолчанию создаётся автоматически.")
        parser.add_argument("--no-replay", action="store_true", help="Не сохранять исходные байты чувствительного вывода.")
        original = parser.get_default("handler")

        def dispatch(args, _original=original):
            _client.value = dict(request_id=args.request_id or str(uuid.uuid4()), no_replay=args.no_replay,
                                 owner_chain=owner_chain(), replay_status="disabled" if args.no_replay else "unavailable",
                                 replay_truncated=False)
            try:
                return _original(args)
            finally:
                _client.value = None
        parser.set_defaults(handler=dispatch)
    parser = subparsers.add_parser("replay", help="Повторно декодировать локальные байты без выполнения команды.")
    core.add_session_name_argument(parser)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--request-id", type=_uuid, help="UUIDv4 сохранённого запроса.")
    group.add_argument("--last", action="store_true", help="Выбрать единственную подходящую запись своей сессии.")
    parser.add_argument("--encoding", required=True, help="Кодировка сохранённых байтов, например cp866, cp1251 или utf-8.")
    parser.add_argument("--json", action="store_true", help="Вернуть один JSON-объект.")
    parser.set_defaults(handler=lambda args: replay_cmd(core, args))


def replay_cmd(core, args) -> int:
    try:
        session = core.validate_session_name(args.name)
        result = Store(core.state_directory()).replay(rid=args.request_id, session=session,
                                                       encoding=args.encoding, owners=owner_chain())
    except (OSError, ReplayError, core.RelayError) as exc:
        # OSError может содержать лишний путь; в публичный ответ он не копируется.
        message = str(exc) if isinstance(exc, (ReplayError, core.RelayError)) else "Не удалось безопасно прочитать replay."
        result = dict(schema_version=1, tool="ssh_relay", action="replay", request_id=args.request_id,
                      session=args.name, source_action=None, source_operation_status="unknown",
                      source_command_status="unknown", source_command_exit_code=None,
                      encoding=args.encoding, stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
                      replay_complete=False, error_code="replay_error", error_message=message)
    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    elif result["error_code"]:
        print(result["error_message"], file=sys.stderr)
    else:
        try:
            # Проверка обоих терминальных кодировщиков до выдачи первого потока.
            for stream, name in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
                if getattr(stream, "encoding", None):
                    result[name].encode(stream.encoding, errors="strict")
            sys.stdout.write(result["stdout"])
            sys.stderr.write(result["stderr"])
        except (UnicodeError, OSError):
            print("Терминал не может вывести replay без потерь.", file=sys.stderr)
            return 1
    return 1 if result["error_code"] else (0 if result["replay_complete"] else 2)
