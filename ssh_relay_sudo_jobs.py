"""Протокол длительных sudo-заданий: секрет остаётся в памяти daemon."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import ssh_relay_verified_identity as identity
from ssh_relay_build import source_sha

SCHEMA = 1
READY = b"SSH_RELAY_SUDO_JOB_READY_1\n"
MAX_COMMAND = 32768
MAX_RESPONSE = 262144
TARGET_FIELDS = ("remote_host", "remote_port", "remote_user", "host_key_algorithm", "remote_host_key_sha256")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def remote_source() -> bytes:
    return Path(__file__).with_name("ssh_relay_sudo_job_remote.py").read_bytes()


def valid_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def validate_payload(value: object) -> bool:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA:
        return False
    if value.get("operation") not in ("start", "status", "tail", "stop"):
        return False
    if not all(valid_uuid(value.get(key)) for key in ("job_id", "transaction_id")):
        return False
    if not isinstance(value.get("command_sha256"), str) or not HASH.fullmatch(value["command_sha256"]):
        return False
    if value["operation"] == "start":
        command = value.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            return False
        try:
            data = command.encode("utf-8")
        except UnicodeError:
            return False
        if len(data) > MAX_COMMAND or hashlib.sha256(data).hexdigest() != value["command_sha256"]:
            return False
    if value["operation"] == "tail":
        if value.get("stream") not in ("stdout", "stderr"):
            return False
        if type(value.get("max_bytes")) is not int or not 1 <= value["max_bytes"] <= 65536:
            return False
    return True


def exchange(client: Any, password: str, payload: dict[str, Any], *, timeout: float = 25) -> dict[str, Any]:
    """Одно SSH-обращение. READY отделяет пароль sudo от полезных данных.

    До отправки полезных данных исключение означает not_started. После отправки
    любые ошибки означают unknown. Текст stderr никогда не возвращается клиенту.
    """
    channel = None
    sent_payload = False
    started = time.monotonic()
    try:
        source = remote_source()
        prefix = source.split(b'if __name__ == "__main__":')[0].decode("utf-8")
        code = prefix + "\nraise SystemExit(main(" + repr(source) + "))\n"
        prompt = ("SSH_RELAY_PASSWORD_" + uuid.uuid4().hex + "\n").encode("ascii")
        command = ("sudo -k -S -p " + shlex.quote(prompt.decode("ascii")) +
                   " -- /usr/bin/python3 -I -c " + shlex.quote(code))
        transport = client.get_transport()
        channel = transport.open_session(timeout=min(timeout, 5))
        channel.settimeout(min(timeout, 5))
        channel.exec_command(command)
        output = bytearray()
        errors = bytearray()
        password_sent = False
        while time.monotonic() - started < timeout:
            progress = False
            if channel.recv_ready():
                output.extend(channel.recv(32768))
                progress = True
            if channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(32768))
                progress = True
            if len(output) > MAX_RESPONSE or len(errors) > 8192:
                raise ValueError("bounded_output_exceeded")
            if prompt in errors and not password_sent:
                # sudo -k исключает зависимость от чужого timestamp/cache.
                channel.sendall((password + "\n").encode("utf-8"))
                password_sent = True
                errors.clear()
            elif prompt in errors and password_sent:
                return {"state": "not_started", "error_code": "sudo_authentication_failed"}
            if not sent_payload and output.startswith(READY):
                output = output[len(READY):]
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                # Флаг ставится ДО sendall: частичная доставка тоже неоднозначна.
                sent_payload = True
                channel.sendall(data)
                channel.shutdown_write()
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                code = channel.recv_exit_status()
                if not sent_payload:
                    return {"state": "not_started", "error_code": "sudo_or_helper_unavailable"}
                if code != 0:
                    return {"state": "unknown", "error_code": "helper_exit_without_result"}
                result = json.loads(bytes(output))
                if (not isinstance(result, dict) or type(result.get("schema_version")) is not int or result["schema_version"] != SCHEMA or
                        result.get("state") not in ("not_started", "running", "succeeded", "failed", "unknown")):
                    raise ValueError("invalid_helper_result")
                return result
            if not progress:
                time.sleep(0.01)
        raise TimeoutError
    except Exception:
        return {"state": "unknown" if sent_payload else "not_started",
                "error_code": "ssh_response_unknown" if sent_payload else "sudo_job_transport_unavailable"}
    finally:
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass


def daemon_request(request: dict[str, Any], observed: object, client: Any,
                   password: str | None, enabled: bool) -> dict[str, Any]:
    expected = request.get("expected_verified_identity")
    payload = request.get("sudo_job")
    refusal = {"ok": True, "state": "not_started" if isinstance(payload, dict) and payload.get("operation") == "start" else "unknown",
               "request_not_started": True, "schema_version": SCHEMA}
    if not enabled or password is None:
        return {**refusal, "error_code": "sudo_jobs_not_enabled"}
    if not validate_payload(payload):
        return {**refusal, "error_code": "invalid_sudo_job_request"}
    if (not identity.matches(expected, observed) or source_sha() is None or
            observed["daemon_source_sha"] != source_sha()):
        return {**refusal, "error_code": "verified_identity_mismatch"}
    # target берётся из удостоверенного транспорта, а не из произвольного JSON клиента.
    payload = {**payload, "target": {key: observed[key] for key in TARGET_FIELDS}}
    result = exchange(client, password, payload)
    if result["state"] == "not_started":
        result["request_not_started"] = True
        if payload["operation"] != "start":
            result["state"] = "unknown"
    return {"ok": True, "schema_version": SCHEMA, **result, "verified_identity": observed}


def add_parser(core: Any, subparsers: Any) -> None:
    parent = subparsers.add_parser("sudo-job", help="Управлять длительным заданием root через systemd.")
    commands = parent.add_subparsers(dest="sudo_job_operation", required=True, parser_class=core.RussianArgumentParser)
    for operation, help_text in (
        ("start", "Запустить новое задание; успешный запуск не означает завершение."),
        ("status", "Прочитать состояние и свидетельство завершения."),
        ("tail", "Прочитать ограниченный фрагмент одного журнала."),
        ("wait", "Ожидать завершение; предел ожидания не останавливает задание."),
        ("stop", "Запросить SIGTERM; принудительной остановки нет."),
    ):
        parser = commands.add_parser(operation, help=help_text)
        core.add_session_name_argument(parser)
        parser.add_argument("--job-id", required=True, help="UUID задания, созданный до запуска.")
        parser.add_argument("--transaction-id", required=True, help="UUID операции, созданный до запуска.")
        parser.add_argument("--expected-identity-file", required=True,
                            help="JSON с точной ожидаемой SSH-целью, поколением соединения и SHA сборки.")
        if operation == "start":
            parser.add_argument("remote_command", help="Разрешённая неинтерактивная команда root.")
        else:
            parser.add_argument("--command-sha256", required=True, help="SHA-256 исходных UTF-8 байт команды.")
        if operation == "tail":
            parser.add_argument("--stream", choices=("stdout", "stderr"), default="stdout", help="Какой журнал читать.")
            parser.add_argument("--bytes", type=int, default=16384, dest="max_bytes", help="От 1 до 65536 байт.")
        if operation == "wait":
            parser.add_argument("--timeout", type=int, default=300, help="Локальное ожидание 1–86400 секунд.")
            parser.add_argument("--poll-interval", type=int, default=2, help="Интервал проверки 1–60 секунд.")
        parser.set_defaults(handler=lambda args: cli(core, args))


def cli(core: Any, args: argparse.Namespace) -> int:
    operation = args.sudo_job_operation
    payload = {"schema_version": SCHEMA, "operation": "status" if operation == "wait" else operation,
               "job_id": args.job_id, "transaction_id": args.transaction_id,
               "command_sha256": getattr(args, "command_sha256", None)}
    result = {"schema_version": SCHEMA, "tool": "ssh_relay", "tool_version": core.__version__,
              "result_type": "sudo_job", "operation": operation, "job_id": args.job_id,
              "transaction_id": args.transaction_id, "state": "not_started" if operation == "start" else "unknown"}

    def emit(value: dict[str, Any]) -> int:
        value = {**result, **value}
        # start с running означает лишь подтверждение запуска.
        state = value.get("state")
        if value.get("wait_timed_out"):
            code = 124
        elif state == "failed":
            code = int(value.get("exit_code", 1)) or 1
        elif state in ("running", "succeeded") and not value.get("error_code"):
            code = 0
        elif state == "unknown":
            code = 3
        else:
            code = 2
        print(json.dumps({**value, "process_exit_code": code}, ensure_ascii=False, separators=(",", ":")))
        return code

    try:
        if operation == "start":
            payload["command"] = args.remote_command
            payload["command_sha256"] = hashlib.sha256(args.remote_command.encode("utf-8")).hexdigest()
        if operation == "tail":
            payload.update(stream=args.stream, max_bytes=args.max_bytes)
        result["command_sha256"] = payload["command_sha256"]
        if not validate_payload(payload):
            raise ValueError
        if operation == "wait" and (not 1 <= args.timeout <= 86400 or not 1 <= args.poll_interval <= 60):
            raise ValueError
        path = Path(args.expected_identity_file).expanduser()
        if path.stat().st_size > 8192:
            raise ValueError
        expected = json.loads(path.read_text(encoding="utf-8"))
        if not identity.validate_expected(expected) or source_sha() != expected["daemon_source_sha"]:
            raise ValueError
        session = core.read_session(core.validate_session_name(args.name))
        status = core.request_daemon(session, "status", response_timeout=5)
        if (status.get("sudo_job_schema_version") != SCHEMA or not status.get("sudo_jobs_enabled") or
                not identity.matches(expected, status.get("verified_identity"))):
            return emit({"error_code": "sudo_job_preflight_mismatch"})
    except (ValueError, TypeError, UnicodeError, OSError, core.RelayError):
        return emit({"error_code": "invalid_metadata_or_unavailable_daemon"})

    deadline = time.monotonic() + (args.timeout if operation == "wait" else 30)
    while True:
        try:
            response = core.request_daemon(session, "sudo_job", response_timeout=35,
                                           sudo_job=payload, expected_verified_identity=expected)
        except core.RelayError:
            return emit({"state": "unknown", "error_code": "daemon_response_unknown"})
        if (not response.get("ok") or response.get("schema_version") != SCHEMA or
                response.get("state") not in ("not_started", "running", "succeeded", "failed", "unknown")):
            return emit({"state": "unknown", "error_code": "invalid_daemon_result"})
        # Допускается отсутствие identity только при отказе ДО отправки запроса.
        if response["state"] != "not_started" and not identity.matches(expected, response.get("verified_identity")):
            return emit({"state": "unknown", "error_code": "response_identity_mismatch"})
        if response["state"] == "running" and not isinstance(response.get("start_witness"), dict):
            return emit({"state": "unknown", "error_code": "start_witness_missing"})
        if response["state"] in ("succeeded", "failed"):
            code = response.get("exit_code")
            if (type(code) is not int or not 0 <= code <= 255 or
                    (response["state"] == "succeeded") != (code == 0) or
                    not (isinstance(response.get("completion_witness"), dict) or
                         (code == 0 and response.get("accounting_status") == "failed" and
                          isinstance(response.get("start_witness"), dict)))):
                return emit({"state": "unknown", "error_code": "completion_result_invalid"})
        for key in ("start_witness", "completion_witness"):
            proof = response.get(key)
            if proof is not None:
                if not isinstance(proof, dict):
                    return emit({"state": "unknown", "error_code": "witness_binding_mismatch"})
                original = {field: value for field, value in proof.items() if field != "witness_sha256"}
                canonical = json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if (proof.get("witness_sha256") != hashlib.sha256(canonical).hexdigest() or
                        any(proof.get(field) != payload[field] for field in ("job_id", "transaction_id", "command_sha256")) or
                        proof.get("target") != {field: expected[field] for field in TARGET_FIELDS} or
                        proof.get("phase") != ("start" if key == "start_witness" else "completion") or
                        not valid_uuid(proof.get("boot_id")) or
                        proof.get("unit") != "ssh-relay-sudo-" + uuid.UUID(payload["job_id"]).hex + ".service" or
                        not isinstance(proof.get("invocation_id"), str) or
                        not re.fullmatch("[0-9a-f]{32}", proof["invocation_id"]) or
                        (key == "completion_witness" and proof.get("exit_code") != response.get("exit_code"))):
                    return emit({"state": "unknown", "error_code": "witness_binding_mismatch"})
        if operation != "wait" or response["state"] != "running":
            return emit(response)
        if time.monotonic() >= deadline:
            return emit({**response, "wait_timed_out": True})
        time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))
