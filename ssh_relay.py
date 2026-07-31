#!/usr/bin/env python3
"""
ssh_relay.py — локальный SSH-relay для выполнения неинтерактивных команд.

Примеры:
  py ssh_relay.py daemon --host 198.51.100.42 --user donpedro
  py ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i ~/.ssh/id_ed25519
  py ssh_relay.py exec "hostname"
  py ssh_relay.py download /tmp/result.txt ./result.txt
  py ssh_relay.py upload ./config.json /tmp/config.json
  py ssh_relay.py status
  py ssh_relay.py stop
"""

__version__ = "0.6.0"

import argparse
import atexit
import base64
import getpass
import hashlib
import json
import os
import posixpath
import re
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

BUFFER_SIZE = 64 * 1024
MAX_OUTPUT_SIZE = 4 * 1024 * 1024
MAX_MESSAGE_SIZE = 96 * 1024 * 1024
DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_DOWNLOAD_TIMEOUT = 300
DEFAULT_DOWNLOAD_MAX_SIZE = 64 * 1024 * 1024
DEFAULT_UPLOAD_TIMEOUT = 300
DEFAULT_UPLOAD_MAX_SIZE = 64 * 1024 * 1024
DEFAULT_RISKY_RECEIPT_PATH = "~/.local/state/agent-safe/changes.jsonl"
MACHINE_EXIT_NOT_STARTED = 10
MACHINE_EXIT_COMMAND_FAILED = 11
MACHINE_EXIT_PARTIAL_SUCCESS = 12
MACHINE_EXIT_UNKNOWN = 13
TRANSACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
REQUIRED_SESSION_FIELDS = {
    "host": str,
    "port": int,
    "user": str,
    "daemon_port": int,
    "auth_token": str,
    "pid": int,
    "version": str,
}


class RelayError(Exception):
    """Ожидаемая ошибка relay, предназначенная для вывода пользователю."""


class DaemonRequestError(RelayError):
    """Ошибка локального протокола с признаком возможной отправки запроса daemon."""

    def __init__(self, message: str, *, request_sent: bool, error_code: str) -> None:
        super().__init__(message)
        self.request_sent = request_sent
        self.error_code = error_code


class RemoteCommandError(RelayError):
    """Ошибка выполнения с признаком возможного запуска удалённой команды."""

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


class RussianArgumentParser(argparse.ArgumentParser):
    """ArgumentParser с русскими заголовками и диагностикой."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self._positionals.title = "позиционные аргументы"
        self._optionals.title = "параметры"
        self.add_argument("-h", "--help", action="help", help="Показать эту справку и выйти.")

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "использование:", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "использование:", 1)

    def error(self, message: str) -> None:
        message = message.replace("the following arguments are required:", "обязательные аргументы не заданы:")
        message = message.replace("unrecognized arguments:", "неизвестные аргументы:")
        message = message.replace("argument ", "аргумент ")
        message = message.replace("expected one argument", "требуется одно значение")
        message = message.replace("invalid choice:", "недопустимое значение:")
        if message.endswith(": command"):
            message = message[:-len("command")] + "команда"
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: ошибка: {message}\n")


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("порт должен быть целым числом") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("порт должен находиться в диапазоне от 1 до 65535")
    return port


def parse_positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("время должно быть целым числом секунд") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("время должно быть положительным числом секунд")
    return seconds


def parse_size_bytes(value: str) -> int:
    """Разбирает размер в байтах с необязательным суффиксом K/M/G."""
    cleaned = value.strip().replace(" ", "")
    match = re.fullmatch(r"(\d+)([A-Za-zА-Яа-я]*)", cleaned)
    if not match:
        raise argparse.ArgumentTypeError("размер должен быть числом байт или значением с суффиксом K, M или G")

    number = int(match.group(1))
    suffix = match.group(2).lower()
    multipliers = {
        "": 1,
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "б": 1,
        "байт": 1,
        "байта": 1,
        "байтов": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "к": 1024,
        "кб": 1024,
        "m": 1024 * 1024,
        "mb": 1024 * 1024,
        "mib": 1024 * 1024,
        "м": 1024 * 1024,
        "мб": 1024 * 1024,
        "g": 1024 * 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
        "gib": 1024 * 1024 * 1024,
        "г": 1024 * 1024 * 1024,
        "гб": 1024 * 1024 * 1024,
    }
    if suffix not in multipliers:
        raise argparse.ArgumentTypeError("поддерживаются только суффиксы K, M или G")
    size = number * multipliers[suffix]
    if size <= 0:
        raise argparse.ArgumentTypeError("размер должен быть положительным")
    return size


def _validate_no_controls(value: str, *, field_name: str, max_length: int, allow_space: bool = True) -> str:
    if len(value) > max_length:
        raise argparse.ArgumentTypeError(f"{field_name}: длина не должна превышать {max_length} символов")
    for char in value:
        code = ord(char)
        if code == 0 or code == 127 or code < 32:
            if allow_space and char == " ":
                continue
            raise argparse.ArgumentTypeError(f"{field_name}: управляющие символы запрещены")
    return value


def parse_transaction_id(value: str) -> str:
    if not TRANSACTION_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "идентификатор транзакции должен содержать 1-128 символов: латинские буквы, цифры, точку, двоеточие, дефис или подчёркивание"
        )
    return value


def parse_change_target(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("target изменения не должен быть пустым")
    return _validate_no_controls(value, field_name="target изменения", max_length=1024)


def parse_change_description(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("описание изменения не должно быть пустым")
    return _validate_no_controls(value, field_name="описание изменения", max_length=512)


def parse_receipt_path(value: str) -> str:
    try:
        return validate_receipt_path(value)
    except RelayError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def validate_receipt_path(value: str) -> str:
    if not value.strip():
        raise RelayError("Путь receipt-файла не должен быть пустым.")
    if len(value) > 4096:
        raise RelayError("Путь receipt-файла слишком длинный.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RelayError("Путь receipt-файла содержит управляющие символы.")
    return value


def validate_transaction_id(value: str) -> str:
    if not TRANSACTION_ID_PATTERN.fullmatch(value):
        raise RelayError("Некорректный transaction_id.")
    return value


def validate_change_target(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return parse_change_target(value)
    except argparse.ArgumentTypeError as exc:
        raise RelayError(str(exc)) from exc


def validate_change_description(value: str) -> str:
    try:
        return parse_change_description(value)
    except argparse.ArgumentTypeError as exc:
        raise RelayError(str(exc)) from exc


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_prefixed(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def command_hash(command: str) -> str:
    return sha256_prefixed(command.encode("utf-8"))


def supports_operation_protocol(version: object) -> bool:
    """Проверяет поддержку машинного протокола v1 без требования точного patch."""
    if not isinstance(version, str):
        return False
    match = VERSION_PATTERN.fullmatch(version)
    current = VERSION_PATTERN.fullmatch(__version__)
    if match is None or current is None:
        return False
    major, minor, _ = (int(part) for part in match.groups())
    current_major, current_minor, _ = (int(part) for part in current.groups())
    return major == current_major and minor >= current_minor


def stored_receipt_hash(payload: dict[str, Any]) -> str:
    """Проверяет self-hash новой записи либо вычисляет anchor для receipt 0.5.x."""
    value = payload.get("receipt_hash")
    if isinstance(value, str) and HASH_PATTERN.fullmatch(value):
        body = dict(payload)
        body.pop("receipt_hash", None)
        expected = sha256_prefixed(canonical_json_bytes(body))
        if value != expected:
            raise RelayError("Последняя запись risky receipt имеет неверный receipt_hash; команда не запускалась.")
        return value
    if payload.get("tool") == "ssh_relay" and payload.get("status") == "done" and isinstance(payload.get("command"), str):
        # Старые записи 0.5.x не имели self-hash. Их канонический hash становится
        # однократным anchor при продолжении журнала в формате 0.6.0.
        return sha256_prefixed(canonical_json_bytes(payload))
    raise RelayError("Последняя запись risky receipt не содержит проверяемый receipt_hash; команда не запускалась.")


def format_bytes(size: int) -> str:
    """Возвращает компактное человекочитаемое представление размера."""
    units = ((1024 * 1024 * 1024, "ГиБ"), (1024 * 1024, "МиБ"), (1024, "КиБ"))
    for factor, suffix in units:
        if size >= factor and size % factor == 0:
            return f"{size // factor} {suffix}"
    return f"{size} байт"


SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEFAULT_SESSION_NAME = "default"


def state_directory() -> Path:
    """Возвращает фиксированный пользовательский каталог состояния relay."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "ssh_relay"


def legacy_session_file_path() -> Path:
    """Возвращает путь старого одиночного session-файла для совместимости."""
    return state_directory() / ".ssh_relay_session.json"


def sessions_directory() -> Path:
    """Возвращает каталог именованных session-файлов."""
    return state_directory() / "sessions"


def validate_session_name(name: str) -> str:
    """Проверяет имя сессии перед использованием в имени файла."""
    if not SESSION_NAME_PATTERN.fullmatch(name):
        raise RelayError(
            "Недопустимое имя сессии. Используйте 1-64 символа: латинские буквы, цифры, точка, дефис или подчёркивание."
        )
    if name in {".", ".."}:
        raise RelayError("Недопустимое имя сессии.")
    return name


def session_file_path(name: str) -> Path:
    """Возвращает путь нового именованного session-файла."""
    name = validate_session_name(name)
    return sessions_directory() / f"{name}.json"


def existing_session_file_path(name: str) -> Path:
    """Возвращает существующий session-файл с учётом legacy default-сессии."""
    current = session_file_path(name)
    if current.exists():
        return current
    legacy = legacy_session_file_path()
    if name == DEFAULT_SESSION_NAME and legacy.exists():
        return legacy
    return current


def prepare_session_directory() -> None:
    state_directory().mkdir(parents=True, exist_ok=True)
    sessions_directory().mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(state_directory(), 0o700)
        os.chmod(sessions_directory(), 0o700)


def remove_session_file(name: str, expected_token: str | None = None) -> None:
    """Удаляет файл только указанной сессии, если задан ожидаемый токен."""
    try:
        path = existing_session_file_path(name)
        if expected_token is not None and path.exists():
            current = read_session(name)
            if current["auth_token"] != expected_token:
                return
        path.unlink(missing_ok=True)
    except (OSError, RelayError):
        pass


def write_session(name: str, session: dict[str, Any]) -> Path:
    prepare_session_directory()
    path = session_file_path(name)
    temporary = path.with_suffix(".tmp")
    data = json.dumps(session, ensure_ascii=False, indent=2)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
        if name == DEFAULT_SESSION_NAME:
            legacy_session_file_path().unlink(missing_ok=True)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def read_session(name: str) -> dict[str, Any]:
    path = existing_session_file_path(name)
    if not path.exists():
        raise RelayError(f"Сессия {name} не найдена. Сначала запустите команду daemon --name {name}.")
    try:
        with path.open("r", encoding="utf-8") as source:
            session = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RelayError(f"Файл сессии повреждён или недоступен: {path}") from exc

    if not isinstance(session, dict):
        raise RelayError(f"Файл сессии имеет неверный формат: {path}")
    for field, expected_type in REQUIRED_SESSION_FIELDS.items():
        if not isinstance(session.get(field), expected_type):
            raise RelayError(f"Файл сессии имеет неверный формат: отсутствует поле {field}.")
    session.setdefault("name", name)
    session["_session_file_path"] = str(path)
    return session


def iter_session_names() -> list[str]:
    """Возвращает имена всех известных session-файлов."""
    names: set[str] = set()
    directory = sessions_directory()
    if directory.exists():
        for item in directory.glob("*.json"):
            candidate = item.stem
            if SESSION_NAME_PATTERN.fullmatch(candidate):
                names.add(candidate)
    if legacy_session_file_path().exists():
        names.add(DEFAULT_SESSION_NAME)
    return sorted(names)


def read_message(sock: socket.socket) -> dict[str, Any]:
    parts: list[bytes] = []
    size = 0
    while True:
        chunk = sock.recv(BUFFER_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_MESSAGE_SIZE:
            raise RelayError("Полученное сообщение превышает допустимый размер.")
        parts.append(chunk)
    if not parts:
        raise RelayError("Получено пустое сообщение от relay.")
    try:
        result = json.loads(b"".join(parts).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayError("Получено повреждённое сообщение от relay.") from exc
    if not isinstance(result, dict):
        raise RelayError("Получено сообщение relay неверного формата.")
    return result


def send_message(conn: socket.socket, message: dict[str, Any]) -> None:
    conn.sendall(json.dumps(message, ensure_ascii=False).encode("utf-8"))


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
                return read_message(sock)
            except RelayError as exc:
                raise DaemonRequestError(
                    "Daemon вернул повреждённый или неполный ответ; результат операции неизвестен.",
                    request_sent=True,
                    error_code="response_invalid",
                ) from exc
    except DaemonRequestError:
        raise
    except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
        if request_sent:
            raise DaemonRequestError(
                "Запрос отправлен daemon, но ответ не получен; результат операции неизвестен.",
                request_sent=True,
                error_code="daemon_response_lost",
            ) from exc
        raise DaemonRequestError(
            "Daemon недоступен; удалённая команда не отправлялась.",
            request_sent=False,
            error_code="daemon_unavailable",
        ) from exc


def load_paramiko():
    try:
        import paramiko
    except ImportError as exc:
        raise RelayError("Не установлена зависимость paramiko. Выполните: py -m pip install paramiko") from exc
    return paramiko


def execute_remote_command(
    client: Any,
    command: str,
    timeout_seconds: int,
    stdin_data: bytes | None = None,
) -> dict[str, Any]:
    """Выполняет команду без PTY, одновременно вычитывая stdout и stderr."""
    channel = None
    command_started = False
    output: list[bytes] = []
    errors: list[bytes] = []
    try:
        channel = client.get_transport().open_session(timeout=10)
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
                chunk = channel.recv(BUFFER_SIZE)
                output.append(chunk)
                total_size += len(chunk)
                read_any = True
            while channel.recv_stderr_ready():
                chunk = channel.recv_stderr(BUFFER_SIZE)
                errors.append(chunk)
                total_size += len(chunk)
                read_any = True

            if total_size > MAX_OUTPUT_SIZE:
                raise RemoteCommandError(
                    "Вывод удалённой команды превышает допустимый размер 4 МиБ; результат команды неизвестен.",
                    error_code="output_limit_exceeded",
                    command_started=True,
                    stdout=b"".join(output).decode("utf-8", errors="replace"),
                    stderr=b"".join(errors).decode("utf-8", errors="replace"),
                )
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic() - started > timeout_seconds:
                raise RemoteCommandError(
                    f"Превышено время выполнения команды: {timeout_seconds} с; результат команды неизвестен.",
                    error_code="command_timeout",
                    command_started=True,
                    stdout=b"".join(output).decode("utf-8", errors="replace"),
                    stderr=b"".join(errors).decode("utf-8", errors="replace"),
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
            raise RemoteCommandError(
                "SSH-канал завершился до получения достоверного результата команды.",
                error_code="command_result_unknown",
                command_started=True,
                stdout=b"".join(output).decode("utf-8", errors="replace"),
                stderr=b"".join(errors).decode("utf-8", errors="replace"),
            ) from exc
        raise RemoteCommandError(
            "Не удалось открыть канал для удалённой команды; команда не запускалась.",
            error_code="command_not_started",
            command_started=False,
        ) from exc
    finally:
        if channel is not None:
            channel.close()


def verify_sudo_password(client: Any, sudo_password: str, timeout_seconds: int) -> None:
    """Проверяет sudo-пароль без сохранения результата sudo timestamp."""
    result = execute_remote_command(
        client,
        "sudo -k && sudo -S -p '' -v",
        timeout_seconds,
        stdin_data=(sudo_password + "\n").encode("utf-8"),
    )
    if result.get("exit_code") != 0:
        raise RelayError("Проверка sudo-пароля не прошла. SSH-соединение будет закрыто.")


def execute_sudo_command(
    client: Any,
    command: str,
    timeout_seconds: int,
    sudo_password: str,
) -> dict[str, Any]:
    """Выполняет команду через sudo, передавая пароль только во внутренний stdin."""
    wrapped_command = "sudo -S -p '' -- sh -c " + shlex.quote(command)
    return execute_remote_command(
        client,
        wrapped_command,
        timeout_seconds,
        stdin_data=(sudo_password + "\n").encode("utf-8"),
    )


def quote_posix_path(path: str) -> str:
    """Экранирует POSIX-путь, сохраняя расширение ~/ на удалённой стороне."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def normalize_remote_sftp_path(remote_path: str) -> str:
    """Нормализует Windows-style путь для SFTP, не меняя POSIX-пути."""
    if "\\" in remote_path:
        return remote_path.replace("\\", "/")
    return remote_path


def execute_auxiliary_command(
    client: Any,
    command: str,
    *,
    sudo: bool,
    timeout_seconds: int,
    sudo_password: str | None,
) -> dict[str, Any]:
    if sudo:
        if sudo_password is None:
            raise RelayError("Режим sudo недоступен: sudo-пароль отсутствует в памяти daemon.")
        return execute_sudo_command(client, command, timeout_seconds, sudo_password)
    return execute_remote_command(client, command, timeout_seconds)


def read_previous_receipt_hash(
    client: Any,
    *,
    receipt_path: str,
    sudo: bool,
    timeout_seconds: int,
    sudo_password: str | None,
) -> str | None:
    """Проверяет последнюю запись журнала до запуска изменяющей команды."""
    path = validate_receipt_path(receipt_path)
    quoted = quote_posix_path(path)
    command = (
        f"if [ -L {quoted} ]; then exit 4; "
        f"elif [ ! -e {quoted} ] || [ ! -s {quoted} ]; then exit 3; "
        f"else tail -n 1 -- {quoted}; fi"
    )
    result = execute_auxiliary_command(
        client,
        command,
        sudo=sudo,
        timeout_seconds=timeout_seconds,
        sudo_password=sudo_password,
    )
    exit_code = int(result.get("exit_code", 1))
    if exit_code == 3:
        return None
    if exit_code == 4:
        raise RelayError("Путь risky receipt является символической ссылкой; команда не запускалась.")
    if exit_code != 0:
        raise RelayError("Не удалось прочитать последнюю запись risky receipt до выполнения команды.")
    line = str(result.get("stdout", "")).rstrip("\r\n")
    if not line:
        raise RelayError("Последняя строка risky receipt пуста; команда не запускалась.")
    try:
        payload = json.loads(line.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise RelayError("Последняя запись risky receipt повреждена; команда не запускалась.") from exc
    if not isinstance(payload, dict):
        raise RelayError("Последняя запись risky receipt имеет неверный формат; команда не запускалась.")
    return stored_receipt_hash(payload)


def ensure_transaction_id_unused(
    client: Any,
    *,
    receipt_path: str,
    transaction_id: str,
    sudo: bool,
    timeout_seconds: int,
    sudo_password: str | None,
) -> None:
    """Отклоняет risky-операцию, если transaction_id уже есть в новом журнале."""
    path = validate_receipt_path(receipt_path)
    validate_transaction_id(transaction_id)
    quoted_path = quote_posix_path(path)
    needle = '"transaction_id":' + json.dumps(transaction_id, ensure_ascii=False)
    command = (
        f"if [ -L {quoted_path} ]; then exit 4; "
        f"elif [ ! -e {quoted_path} ] || [ ! -s {quoted_path} ]; then exit 0; "
        f"elif grep -F -q -- {shlex.quote(needle)} {quoted_path}; then exit 5; "
        f"else code=$?; [ \"$code\" -eq 1 ] && exit 0; exit 6; fi"
    )
    result = execute_auxiliary_command(
        client,
        command,
        sudo=sudo,
        timeout_seconds=timeout_seconds,
        sudo_password=sudo_password,
    )
    exit_code = int(result.get("exit_code", 1))
    if exit_code == 0:
        return
    if exit_code == 4:
        raise RelayError("Путь risky receipt является символической ссылкой; команда не запускалась.")
    if exit_code == 5:
        raise RelayError("transaction_id уже присутствует в risky receipt; команда не запускалась.")
    raise RelayError("Не удалось проверить уникальность transaction_id; команда не запускалась.")


def build_risky_receipt_payload(
    *,
    session: dict[str, Any],
    action: str,
    transaction_id: str,
    receipt_id: str,
    change_target: str | None,
    change_description: str,
    command_hash_value: str,
    command_exit_code: int,
    previous_receipt_hash: str | None,
    timestamp_utc: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "timestamp_utc": timestamp_utc,
        "tool": "ssh_relay",
        "tool_version": __version__,
        "session": session_display_name(session),
        "remote_host": session["host"],
        "remote_port": session["port"],
        "remote_user": session["user"],
        "action": action,
        "sudo": action == "sudo-exec",
        "transaction_id": transaction_id,
        "receipt_id": receipt_id,
        "change_target": change_target,
        "change_description": change_description,
        "command_status": "succeeded",
        "command_hash": command_hash_value,
        "command_exit_code": command_exit_code,
        "previous_receipt_hash": previous_receipt_hash,
    }
    payload["receipt_hash"] = sha256_prefixed(canonical_json_bytes(payload))
    return payload


def write_risky_receipt(
    client: Any,
    *,
    receipt_path: str,
    payload: dict[str, Any],
    sudo: bool,
    timeout_seconds: int,
    sudo_password: str | None,
) -> tuple[str, str | None]:
    """Добавляет receipt и проверяет последнюю строку; возвращает status и диагностику."""
    path = validate_receipt_path(receipt_path)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    directory = posixpath.dirname(path.rstrip("/")) or "."
    quoted_path = quote_posix_path(path)
    command = (
        "umask 077 && "
        f"mkdir -p {quote_posix_path(directory)} && "
        f"if [ -L {quoted_path} ]; then exit 4; fi && "
        f"touch {quoted_path} && chmod 600 -- {quoted_path} && "
        f"printf '%s\\n' {shlex.quote(line)} >> {quoted_path}"
    )
    try:
        append_result = execute_auxiliary_command(
            client,
            command,
            sudo=sudo,
            timeout_seconds=timeout_seconds,
            sudo_password=sudo_password,
        )
    except RemoteCommandError as exc:
        if exc.command_started:
            return "unknown", "Подтверждение записи receipt не получено."
        return "failed", "Команда записи receipt не была запущена."
    except RelayError as exc:
        return "failed", str(exc)
    append_exit_code = int(append_result.get("exit_code", 1))
    if append_exit_code == 4:
        return "failed", "Путь risky receipt является символической ссылкой."
    if append_exit_code != 0:
        return "failed", "Удалённая команда записи receipt завершилась ошибкой."

    verify_command = f"tail -n 1 -- {quote_posix_path(path)}"
    try:
        verify_result = execute_auxiliary_command(
            client,
            verify_command,
            sudo=sudo,
            timeout_seconds=timeout_seconds,
            sudo_password=sudo_password,
        )
    except RemoteCommandError:
        return "unknown", "Receipt мог быть записан, но контрольное чтение не завершилось."
    except RelayError:
        return "unknown", "Receipt мог быть записан, но контрольное чтение не выполнено."
    if int(verify_result.get("exit_code", 1)) != 0:
        return "unknown", "Receipt мог быть записан, но контрольное чтение завершилось ошибкой."
    verified_line = str(verify_result.get("stdout", "")).rstrip("\r\n")
    try:
        verified = json.loads(verified_line.splitlines()[-1]) if verified_line else None
    except json.JSONDecodeError:
        verified = None
    if not isinstance(verified, dict):
        return "unknown", "Последняя запись receipt после добавления имеет неверный формат."
    if verified.get("receipt_id") != payload["receipt_id"] or verified.get("receipt_hash") != payload["receipt_hash"]:
        return "unknown", "Не удалось подтвердить, что последняя запись принадлежит текущей транзакции."
    try:
        verified_hash = stored_receipt_hash(verified)
    except RelayError:
        return "unknown", "Контрольная запись receipt не прошла проверку hash."
    if verified_hash != payload["receipt_hash"]:
        return "unknown", "Контрольный hash receipt не совпадает с ожидаемым."
    return "written", None


def machine_result_base(
    *,
    session: dict[str, Any] | None,
    action: str,
    risky: bool,
    transaction_id: str,
    transaction_id_source: str,
    change_target: str | None,
    change_description: str,
    receipt_path: str | None,
    command_hash_value: str,
    started_at_utc: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool": "ssh_relay",
        "tool_version": __version__,
        "action": action,
        "operation_status": "not_started",
        "session": session_display_name(session) if session else None,
        "remote_host": session.get("host") if session else None,
        "remote_port": session.get("port") if session else None,
        "remote_user": session.get("user") if session else None,
        "sudo": action == "sudo-exec",
        "risky": risky,
        "transaction_id": transaction_id,
        "transaction_id_source": transaction_id_source,
        "change_target": change_target,
        "change_description": change_description,
        "command_status": "not_started",
        "command_exit_code": None,
        "command_hash": command_hash_value,
        "receipt_status": "not_attempted" if risky else "not_requested",
        "receipt_path": receipt_path if risky else None,
        "receipt_id": str(uuid.uuid4()) if risky else None,
        "receipt_hash": None,
        "previous_receipt_hash": None,
        "partial_success": False,
        "stdout": "",
        "stderr": "",
        "output_encoding": "utf-8-replace",
        "error_code": None,
        "error_stage": None,
        "error_message": None,
        "started_at_utc": started_at_utc,
        "finished_at_utc": None,
    }


def finish_machine_result(
    result: dict[str, Any],
    *,
    operation_status: str,
    error_code: str | None = None,
    error_stage: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    result["operation_status"] = operation_status
    result["error_code"] = error_code
    result["error_stage"] = error_stage
    result["error_message"] = error_message
    result["partial_success"] = (
        result.get("command_status") == "succeeded"
        and result.get("receipt_status") in {"failed", "unknown"}
    )
    result["finished_at_utc"] = utc_now()
    return result


def execute_command_operation(
    client: Any,
    *,
    session: dict[str, Any],
    action: str,
    command: str,
    risky: bool,
    receipt_path: str,
    transaction_id: str,
    transaction_id_source: str,
    change_target: str | None,
    change_description: str,
    timeout_seconds: int,
    sudo_password: str | None,
) -> dict[str, Any]:
    sudo = action == "sudo-exec"
    result = machine_result_base(
        session=session,
        action=action,
        risky=risky,
        transaction_id=transaction_id,
        transaction_id_source=transaction_id_source,
        change_target=change_target,
        change_description=change_description,
        receipt_path=receipt_path,
        command_hash_value=command_hash(command),
        started_at_utc=utc_now(),
    )

    if sudo and sudo_password is None:
        return finish_machine_result(
            result,
            operation_status="not_started",
            error_code="sudo_disabled",
            error_stage="daemon",
            error_message="Режим sudo недоступен: sudo-пароль отсутствует в памяти daemon.",
        )

    if risky:
        try:
            result["previous_receipt_hash"] = read_previous_receipt_hash(
                client,
                receipt_path=receipt_path,
                sudo=sudo,
                timeout_seconds=timeout_seconds,
                sudo_password=sudo_password,
            )
            ensure_transaction_id_unused(
                client,
                receipt_path=receipt_path,
                transaction_id=transaction_id,
                sudo=sudo,
                timeout_seconds=timeout_seconds,
                sudo_password=sudo_password,
            )
        except (RelayError, RemoteCommandError) as exc:
            error_code = "transaction_id_exists" if "уже присутствует" in str(exc) else "receipt_preflight_failed"
            return finish_machine_result(
                result,
                operation_status="not_started",
                error_code=error_code,
                error_stage="receipt",
                error_message=str(exc),
            )

    try:
        if sudo:
            command_result = execute_sudo_command(client, command, timeout_seconds, sudo_password or "")
        else:
            command_result = execute_remote_command(client, command, timeout_seconds)
    except RemoteCommandError as exc:
        result["stdout"] = exc.stdout
        result["stderr"] = exc.stderr
        if exc.command_started:
            result["command_status"] = "unknown"
            return finish_machine_result(
                result,
                operation_status="unknown",
                error_code=exc.error_code,
                error_stage="command",
                error_message=str(exc),
            )
        return finish_machine_result(
            result,
            operation_status="not_started",
            error_code=exc.error_code,
            error_stage="command",
            error_message=str(exc),
        )

    result["stdout"] = str(command_result.get("stdout", ""))
    result["stderr"] = str(command_result.get("stderr", ""))
    exit_code = int(command_result.get("exit_code", 1))
    result["command_exit_code"] = exit_code
    if exit_code != 0:
        result["command_status"] = "failed"
        return finish_machine_result(
            result,
            operation_status="command_failed",
            error_code="command_failed",
            error_stage="command",
            error_message=f"Удалённая команда завершилась с кодом {exit_code}.",
        )

    result["command_status"] = "succeeded"
    if not risky:
        result["receipt_status"] = "not_requested"
        return finish_machine_result(result, operation_status="succeeded")

    receipt_payload = build_risky_receipt_payload(
        session=session,
        action=action,
        transaction_id=transaction_id,
        receipt_id=str(result["receipt_id"]),
        change_target=change_target,
        change_description=change_description,
        command_hash_value=str(result["command_hash"]),
        command_exit_code=exit_code,
        previous_receipt_hash=result.get("previous_receipt_hash"),
        timestamp_utc=utc_now(),
    )
    result["receipt_hash"] = receipt_payload["receipt_hash"]
    status, diagnostic = write_risky_receipt(
        client,
        receipt_path=receipt_path,
        payload=receipt_payload,
        sudo=sudo,
        timeout_seconds=timeout_seconds,
        sudo_password=sudo_password,
    )
    result["receipt_status"] = status
    if status == "written":
        return finish_machine_result(result, operation_status="succeeded")
    return finish_machine_result(
        result,
        operation_status="partial_success",
        error_code="receipt_write_failed" if status == "failed" else "receipt_status_unknown",
        error_stage="receipt",
        error_message=diagnostic or "Статус receipt неизвестен.",
    )


def legacy_operation_response(result: dict[str, Any]) -> dict[str, Any]:
    """Преобразует новый результат в формат CLI 0.5.x для совместимости."""
    status = result.get("operation_status")
    if status in {"succeeded", "command_failed"}:
        return {
            "ok": True,
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "exit_code": result.get("command_exit_code", 1),
            "risky_receipt": {
                "path": result.get("receipt_path"),
                "receipt_id": result.get("receipt_id"),
                "receipt_hash": result.get("receipt_hash"),
            } if result.get("risky") and result.get("receipt_status") == "written" else None,
        }
    if status == "partial_success":
        return {
            "ok": False,
            "protocol_error": (
                "Удалённая команда выполнена, но risky receipt не подтверждён. "
                "Состояние хоста изменено; требуется проверка."
            ),
            "command_result": {
                "ok": True,
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "exit_code": result.get("command_exit_code", 0),
            },
        }
    return {
        "ok": False,
        "protocol_error": result.get("error_message") or "Операция relay не выполнена.",
    }


def machine_exit_code(result: dict[str, Any]) -> int:
    status = result.get("operation_status")
    if status == "succeeded":
        return 0
    if status == "command_failed":
        return MACHINE_EXIT_COMMAND_FAILED
    if status == "partial_success":
        return MACHINE_EXIT_PARTIAL_SUCCESS
    if status == "unknown":
        return MACHINE_EXIT_UNKNOWN
    return MACHINE_EXIT_NOT_STARTED


def download_remote_file(
    client: Any,
    remote_path: str,
    local_path: str,
    *,
    overwrite: bool,
    create_dirs: bool,
    max_size: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Скачивает один обычный удалённый файл через SFTP в локальный файл."""
    if not remote_path.strip():
        raise RelayError("Передан пустой путь удалённого файла.")
    if not local_path.strip():
        raise RelayError("Передан пустой локальный путь для сохранения файла.")

    remote_source = normalize_remote_sftp_path(remote_path)
    target = Path(local_path).expanduser()
    if not target.is_absolute():
        raise RelayError("Локальный путь должен быть абсолютным.")
    if not target.name:
        raise RelayError("Локальный путь должен указывать на файл, а не на корень диска или файловой системы.")
    if target.exists() and target.is_dir():
        raise RelayError("Локальный путь указывает на каталог, а не на файл.")
    if target.exists() and not overwrite:
        raise RelayError("Локальный файл уже существует. Укажите --overwrite для перезаписи.")

    parent = target.parent
    if create_dirs:
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RelayError(f"Не удалось создать локальный каталог для скачивания: {parent}") from exc
    elif not parent.is_dir():
        raise RelayError("Локальный каталог для сохранения не существует. Укажите --create-dirs или создайте его вручную.")

    temporary = target.with_name(f".{target.name}.ssh-relay-{uuid.uuid4().hex}.tmp")
    started = time.monotonic()
    try:
        sftp = client.open_sftp()
    except Exception as exc:
        raise RelayError("Не удалось открыть SFTP-канал через активную SSH-сессию.") from exc
    received = 0
    try:
        try:
            remote_stat = sftp.stat(remote_source)
        except OSError as exc:
            raise RelayError(f"Удалённый файл не найден или недоступен: {remote_source}") from exc

        mode = getattr(remote_stat, "st_mode", 0)
        if stat.S_ISDIR(mode):
            raise RelayError("Удалённый путь указывает на каталог. Скачивание каталогов не поддерживается.")
        if mode and not stat.S_ISREG(mode):
            raise RelayError("Удалённый путь не является обычным файлом. Скачивание специальных файлов не поддерживается.")

        remote_size = int(getattr(remote_stat, "st_size", 0) or 0)
        if remote_size > max_size:
            raise RelayError(
                f"Размер удалённого файла {format_bytes(remote_size)} превышает лимит "
                f"{format_bytes(max_size)}. Перезапустите daemon с большим --download-max-size, "
                "если это безопасно."
            )

        try:
            with sftp.open(remote_source, "rb") as remote_file, temporary.open("xb") as output:
                while True:
                    chunk = remote_file.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_size:
                        raise RelayError(
                            f"Скачивание остановлено: получено больше лимита {format_bytes(max_size)}."
                        )
                    if time.monotonic() - started > timeout_seconds:
                        raise RelayError(
                            f"Превышено время скачивания файла: {timeout_seconds} с. "
                            "Relay предназначен для коротких контролируемых передач."
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except RelayError:
            raise
        except OSError as exc:
            raise RelayError(f"Ошибка при скачивании или записи файла: {exc}") from exc

        if target.exists() and not overwrite:
            raise RelayError("Локальный файл появился во время скачивания. Повторите команду с --overwrite при необходимости.")
        os.replace(temporary, target)
        return {
            "ok": True,
            "remote_path": remote_source,
            "local_path": str(target),
            "bytes_downloaded": received,
        }
    finally:
        try:
            sftp.close()
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def remote_parent_directory(remote_path: str) -> str:
    """Возвращает родительский каталог POSIX-пути на удалённой стороне."""
    stripped = remote_path.rstrip("/")
    if not stripped:
        raise RelayError("Удалённый путь должен указывать на файл, а не на корень файловой системы.")
    name = posixpath.basename(stripped)
    if not name or name in {".", ".."}:
        raise RelayError("Удалённый путь должен указывать на файл с допустимым именем.")
    parent = posixpath.dirname(stripped)
    return parent or "."


def ensure_remote_directory(sftp: Any, remote_directory: str) -> None:
    """Создаёт родительские каталоги на удалённой стороне через SFTP."""
    if remote_directory in {"", "."}:
        return
    normalized = posixpath.normpath(remote_directory)
    if normalized == "/":
        return

    current = "/" if normalized.startswith("/") else ""
    for part in normalized.strip("/").split("/"):
        if not part or part == ".":
            continue
        current = posixpath.join(current, part) if current else part
        try:
            attrs = sftp.stat(current)
            mode = getattr(attrs, "st_mode", 0)
            if mode and not stat.S_ISDIR(mode):
                raise RelayError(f"Удалённый путь {current} существует, но не является каталогом.")
        except RelayError:
            raise
        except OSError:
            try:
                sftp.mkdir(current)
            except OSError as exc:
                raise RelayError(f"Не удалось создать удалённый каталог: {current}") from exc


def remote_temporary_path(remote_path: str) -> str:
    """Возвращает временный POSIX-путь рядом с удалённым целевым файлом."""
    stripped = remote_path.rstrip("/")
    parent = remote_parent_directory(stripped)
    name = posixpath.basename(stripped)
    temporary_name = f".{name}.ssh-relay-{uuid.uuid4().hex}.tmp"
    if parent in {"", "."}:
        return temporary_name
    return posixpath.join(parent, temporary_name)


def upload_file_content(
    client: Any,
    local_path: str,
    content: bytes,
    remote_path: str,
    *,
    overwrite: bool,
    create_dirs: bool,
    max_size: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Загружает переданное клиентом содержимое файла через SFTP на удалённый сервер."""
    if not local_path.strip():
        raise RelayError("Передан пустой путь локального файла.")
    if not remote_path.strip():
        raise RelayError("Передан пустой путь удалённого файла.")
    if remote_path.endswith("/"):
        raise RelayError("Удалённый путь должен указывать на файл, а не на каталог.")
    if "\x00" in remote_path:
        raise RelayError("Удалённый путь содержит недопустимый нулевой символ.")

    local_size = len(content)
    if local_size > max_size:
        raise RelayError(
            f"Размер локального файла {format_bytes(local_size)} превышает лимит "
            f"{format_bytes(max_size)}. Перезапустите daemon с большим --upload-max-size, "
            "если это безопасно."
        )

    remote_target = normalize_remote_sftp_path(remote_path).rstrip("/")
    remote_parent = remote_parent_directory(remote_target)
    temporary = remote_temporary_path(remote_target)
    started = time.monotonic()

    try:
        sftp = client.open_sftp()
    except Exception as exc:
        raise RelayError("Не удалось открыть SFTP-канал через активную SSH-сессию.") from exc

    sent = 0
    try:
        if create_dirs:
            ensure_remote_directory(sftp, remote_parent)
        else:
            try:
                attrs = sftp.stat(remote_parent)
                mode = getattr(attrs, "st_mode", 0)
                if mode and not stat.S_ISDIR(mode):
                    raise RelayError("Удалённый родительский путь существует, но не является каталогом.")
            except RelayError:
                raise
            except OSError as exc:
                raise RelayError(
                    "Удалённый каталог назначения не существует. Укажите --create-dirs или создайте его вручную."
                ) from exc

        try:
            existing = sftp.stat(remote_target)
            mode = getattr(existing, "st_mode", 0)
            if mode and stat.S_ISDIR(mode):
                raise RelayError("Удалённый путь указывает на каталог, а не на файл.")
            if mode and not stat.S_ISREG(mode):
                raise RelayError("Удалённый путь существует, но не является обычным файлом.")
            if not overwrite:
                raise RelayError("Удалённый файл уже существует. Укажите --overwrite для перезаписи.")
        except RelayError:
            raise
        except OSError:
            pass

        try:
            sftp.stat(temporary)
        except OSError:
            pass
        else:
            raise RelayError("Временный удалённый файл уже существует. Повторите команду.")

        try:
            with sftp.open(temporary, "wb") as remote_file:
                for offset in range(0, local_size, BUFFER_SIZE):
                    chunk = content[offset:offset + BUFFER_SIZE]
                    sent += len(chunk)
                    if sent > max_size:
                        raise RelayError(f"Загрузка остановлена: отправлено больше лимита {format_bytes(max_size)}.")
                    if time.monotonic() - started > timeout_seconds:
                        raise RelayError(
                            f"Превышено время загрузки файла: {timeout_seconds} с. "
                            "Relay предназначен для коротких контролируемых передач."
                        )
                    remote_file.write(chunk)
                remote_file.flush()
        except RelayError:
            raise
        except OSError as exc:
            raise RelayError(f"Ошибка при чтении или загрузке файла: {exc}") from exc

        try:
            sftp.stat(remote_target)
            target_exists = True
        except OSError:
            target_exists = False
        if target_exists and not overwrite:
            raise RelayError("Удалённый файл появился во время загрузки. Повторите команду с --overwrite при необходимости.")

        if overwrite:
            try:
                sftp.posix_rename(temporary, remote_target)
            except AttributeError:
                try:
                    sftp.remove(remote_target)
                except OSError:
                    pass
                sftp.rename(temporary, remote_target)
            except OSError:
                try:
                    sftp.remove(remote_target)
                except OSError:
                    pass
                sftp.rename(temporary, remote_target)
        else:
            sftp.rename(temporary, remote_target)

        return {
            "ok": True,
            "local_path": local_path,
            "remote_path": remote_target,
            "bytes_uploaded": sent,
        }
    finally:
        try:
            sftp.remove(temporary)
        except OSError:
            pass
        finally:
            sftp.close()


def session_display_name(session: dict[str, Any]) -> str:
    return str(session.get("name") or DEFAULT_SESSION_NAME)


def format_session_target(session: dict[str, Any]) -> str:
    return f"{session['user']}@{session['host']}:{session['port']}"


def start_detached_daemon(args: argparse.Namespace) -> int:
    """Запускает daemon в отдельном процессе и ждёт появления активной сессии."""
    if not args.identity_file:
        print("--detach поддерживается только вместе с --identity-file, чтобы не скрывать password prompt.", file=sys.stderr)
        return 2
    if args.ask_key_passphrase:
        print("--detach несовместим с --ask-key-passphrase.", file=sys.stderr)
        return 2
    if args.enable_sudo:
        print("--detach несовместим с --enable-sudo, потому что sudo-пароль вводится интерактивно.", file=sys.stderr)
        return 2

    session_name = validate_session_name(args.name)
    if check_existing_session(session_name):
        return 1

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "daemon",
        "--name", session_name,
        "--host", args.host,
        "--port", str(args.port),
        "--user", args.user,
        "--identity-file", args.identity_file,
        "--command-timeout", str(args.command_timeout),
        "--download-timeout", str(args.download_timeout),
        "--download-max-size", str(args.download_max_size),
        "--upload-timeout", str(args.upload_timeout),
        "--upload-max-size", str(args.upload_max_size),
    ]
    if args.known_hosts:
        command.extend(["--known-hosts", args.known_hosts])

    log_path = Path(args.detach_log).expanduser() if args.detach_log else state_directory() / f"{session_name}.daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=creationflags)

    deadline = time.monotonic() + 15
    last_error = "daemon не успел создать активную сессию."
    while time.monotonic() < deadline:
        try:
            session = read_session(session_name)
            result = request_daemon(session, "status")
            if result.get("ok") and result.get("status") == "active":
                print(f"Сессия {session_name} запущена в фоне: {format_session_target(session)}")
                print(f"Лог daemon: {log_path}")
                return 0
        except RelayError as exc:
            last_error = str(exc)
        time.sleep(0.5)

    print(f"Не удалось подтвердить запуск detached daemon: {last_error}", file=sys.stderr)
    print(f"Проверьте лог: {log_path}", file=sys.stderr)
    return 1



def check_existing_session(name: str) -> bool:
    path = existing_session_file_path(name)
    if not path.exists():
        return False
    try:
        session = read_session(name)
        result = request_daemon(session, "status")
        if result.get("ok"):
            print(
                f"Сессия {name} уже активна: {format_session_target(session)}.",
                file=sys.stderr,
            )
            print(f"Сначала завершите её командой: stop --name {name}", file=sys.stderr)
            return True
    except DaemonRequestError as exc:
        if exc.request_sent:
            print(
                f"Состояние сессии {name} неизвестно: запрос status был отправлен, но ответ не получен. "
                "Session-файл сохранён; новый daemon с этим именем не запускается.",
                file=sys.stderr,
            )
            return True
        remove_session_file(name)
    except RelayError:
        remove_session_file(name)
    return False


def daemon(args: argparse.Namespace) -> int:
    if getattr(args, "detach", False):
        return start_detached_daemon(args)

    try:
        session_name = validate_session_name(args.name)
    except RelayError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.ask_key_passphrase and not args.identity_file:
        print("Параметр --ask-key-passphrase допустим только вместе с --identity-file.", file=sys.stderr)
        return 2

    if check_existing_session(session_name):
        return 1

    identity_file: str | None = None
    password: str | None = None
    passphrase: str | None = None
    sudo_password: str | None = None
    if args.identity_file:
        identity_path = Path(args.identity_file).expanduser()
        if not identity_path.is_file():
            print(f"Файл ключа или сертификата не найден: {identity_path}", file=sys.stderr)
            return 1
        identity_file = str(identity_path)

    try:
        paramiko = load_paramiko()
    except RelayError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if identity_file:
        if args.ask_key_passphrase:
            passphrase = getpass.getpass(f"Passphrase SSH-ключа для {args.user}@{args.host}: ")
    else:
        password = getpass.getpass(f"SSH-пароль для {args.user}@{args.host}: ")

    client = paramiko.SSHClient()
    try:
        if args.known_hosts:
            client.load_system_host_keys(args.known_hosts)
        else:
            client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            args.host,
            port=args.port,
            username=args.user,
            password=password,
            key_filename=identity_file,
            passphrase=passphrase,
            look_for_keys=False,
            allow_agent=False,
            timeout=10,
        )
    except Exception as exc:
        client.close()
        print(f"Не удалось установить SSH-соединение: {exc}", file=sys.stderr)
        if identity_file:
            print(
                "Проверьте доступность сервера, файл ключа или сертификата, его passphrase "
                "и наличие подтверждённого ключа сервера в known_hosts.",
                file=sys.stderr,
            )
        else:
            print(
                "Проверьте доступность сервера, пароль и наличие подтверждённого ключа сервера в known_hosts.",
                file=sys.stderr,
            )
        return 1
    finally:
        password = None
        passphrase = None

    if args.enable_sudo:
        sudo_password = getpass.getpass(f"sudo-пароль для {args.user}@{args.host}: ")
        try:
            verify_sudo_password(client, sudo_password, args.command_timeout)
        except RelayError as exc:
            sudo_password = None
            client.close()
            print(str(exc), file=sys.stderr)
            return 1
        print("Режим sudo включён: пароль проверен и хранится только в памяти daemon.")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        server.settimeout(0.5)
    except OSError as exc:
        server.close()
        client.close()
        print(f"Не удалось открыть локальный порт relay: {exc}", file=sys.stderr)
        return 1

    auth_token = str(uuid.uuid4())
    daemon_port = server.getsockname()[1]
    session = {
        "schema_version": 2,
        "name": session_name,
        "version": __version__,
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "daemon_port": daemon_port,
        "auth_token": auth_token,
        "pid": os.getpid(),
        "sudo_enabled": bool(args.enable_sudo),
        "command_timeout": args.command_timeout,
        "download_timeout": args.download_timeout,
        "download_max_size": args.download_max_size,
        "upload_timeout": args.upload_timeout,
        "upload_max_size": args.upload_max_size,
    }
    try:
        session_path = write_session(session_name, session)
    except OSError as exc:
        server.close()
        client.close()
        print(f"Не удалось безопасно записать файл сессии: {exc}", file=sys.stderr)
        return 1

    stop_event = threading.Event()
    command_lock = threading.Lock()
    cleanup_done = False

    def cleanup() -> None:
        nonlocal cleanup_done, sudo_password
        if cleanup_done:
            return
        cleanup_done = True
        sudo_password = None
        remove_session_file(session_name, auth_token)
        client.close()

    atexit.register(cleanup)

    def handle_client(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)

            def reply(message: dict[str, Any]) -> None:
                try:
                    send_message(conn, message)
                except OSError:
                    pass

            try:
                request = read_message(conn)
                if request.get("auth_token") != auth_token:
                    reply({"ok": False, "protocol_error": "Доступ к relay отклонён."})
                    return

                action = request.get("action")
                if action == "status":
                    reply({
                        "ok": True,
                        "status": "active",
                        "version": __version__,
                        "sudo_enabled": bool(args.enable_sudo),
                        "name": session_name,
                    })
                    return
                if action == "stop":
                    reply({"ok": True, "status": "stopping"})
                    stop_event.set()
                    return
                if action not in {"exec", "sudo_exec", "download", "upload"}:
                    reply({"ok": False, "protocol_error": "Неизвестное действие relay."})
                    return

                if action == "download":
                    remote_path = request.get("remote_path")
                    local_path = request.get("local_path")
                    overwrite = request.get("overwrite")
                    create_dirs = request.get("create_dirs")
                    if not isinstance(remote_path, str) or not isinstance(local_path, str):
                        reply({"ok": False, "protocol_error": "Для скачивания нужны удалённый и локальный путь."})
                        return
                    if not isinstance(overwrite, bool) or not isinstance(create_dirs, bool):
                        reply({"ok": False, "protocol_error": "Некорректные параметры скачивания."})
                        return
                    with command_lock:
                        result = download_remote_file(
                            client,
                            remote_path,
                            local_path,
                            overwrite=overwrite,
                            create_dirs=create_dirs,
                            max_size=args.download_max_size,
                            timeout_seconds=args.download_timeout,
                        )
                    reply(result)
                    return

                if action == "upload":
                    local_path = request.get("local_path")
                    remote_path = request.get("remote_path")
                    content_b64 = request.get("content_b64")
                    overwrite = request.get("overwrite")
                    create_dirs = request.get("create_dirs")
                    if not isinstance(local_path, str) or not isinstance(remote_path, str):
                        reply({"ok": False, "protocol_error": "Для загрузки нужны локальный и удалённый путь."})
                        return
                    if not isinstance(content_b64, str):
                        reply({"ok": False, "protocol_error": "Для загрузки нужно содержимое файла."})
                        return
                    if not isinstance(overwrite, bool) or not isinstance(create_dirs, bool):
                        reply({"ok": False, "protocol_error": "Некорректные параметры загрузки."})
                        return
                    try:
                        content = base64.b64decode(content_b64.encode("ascii"), validate=True)
                    except (ValueError, UnicodeEncodeError):
                        reply({"ok": False, "protocol_error": "Содержимое загружаемого файла повреждено."})
                        return
                    with command_lock:
                        result = upload_file_content(
                            client,
                            local_path,
                            content,
                            remote_path,
                            overwrite=overwrite,
                            create_dirs=create_dirs,
                            max_size=args.upload_max_size,
                            timeout_seconds=args.upload_timeout,
                        )
                    reply(result)
                    return

                command = request.get("command")
                risky = request.get("risky", False)
                receipt_path = request.get("receipt_path", DEFAULT_RISKY_RECEIPT_PATH)
                transaction_id = request.get("transaction_id")
                transaction_id_source = request.get("transaction_id_source")
                change_target = request.get("change_target")
                change_description = request.get("change_description", "Удалённое изменение")
                machine_mode = request.get("machine_mode", False)
                protocol_version = request.get("operation_protocol_version")
                if protocol_version is not None and protocol_version != 1:
                    reply({"ok": False, "protocol_error": "Неподдерживаемая версия протокола операции."})
                    return
                legacy_request = protocol_version is None
                if legacy_request:
                    transaction_id = str(uuid.uuid4())
                    transaction_id_source = "relay"
                    change_target = None
                    change_description = "Удалённое изменение"
                    machine_mode = False

                if not isinstance(command, str) or not command.strip():
                    reply({"ok": False, "protocol_error": "Передана пустая удалённая команда."})
                    return
                if not isinstance(risky, bool) or not isinstance(machine_mode, bool):
                    reply({"ok": False, "protocol_error": "Некорректные флаги операции."})
                    return
                if not isinstance(receipt_path, str):
                    reply({"ok": False, "protocol_error": "Некорректный путь risky receipt."})
                    return
                if not isinstance(transaction_id, str) or not isinstance(transaction_id_source, str):
                    reply({"ok": False, "protocol_error": "Не передан корректный transaction_id."})
                    return
                if change_target is not None and not isinstance(change_target, str):
                    reply({"ok": False, "protocol_error": "Некорректный target изменения."})
                    return
                if not isinstance(change_description, str):
                    reply({"ok": False, "protocol_error": "Некорректное описание изменения."})
                    return
                try:
                    validate_transaction_id(transaction_id)
                    validate_receipt_path(receipt_path)
                    change_target = validate_change_target(change_target)
                    change_description = validate_change_description(change_description)
                except RelayError as exc:
                    reply({"ok": False, "protocol_error": str(exc)})
                    return
                if transaction_id_source not in {"caller", "relay"}:
                    reply({"ok": False, "protocol_error": "Некорректный источник transaction_id."})
                    return

                cli_action = "sudo-exec" if action == "sudo_exec" else "exec"
                with command_lock:
                    result = execute_command_operation(
                        client,
                        session=session,
                        action=cli_action,
                        command=command,
                        risky=risky,
                        receipt_path=receipt_path,
                        transaction_id=transaction_id,
                        transaction_id_source=transaction_id_source,
                        change_target=change_target,
                        change_description=change_description,
                        timeout_seconds=args.command_timeout,
                        sudo_password=sudo_password,
                    )
                if protocol_version == 1:
                    reply(result)
                else:
                    reply(legacy_operation_response(result))
            except (socket.timeout, TimeoutError):
                reply({"ok": False, "protocol_error": "Истекло время ожидания локального запроса."})
            except RelayError as exc:
                reply({"ok": False, "protocol_error": str(exc)})
            except Exception:
                reply({"ok": False, "protocol_error": "Внутренняя ошибка daemon."})


    print(f"SSH-соединение установлено: {args.user}@{args.host}:{args.port}")
    print(f"Имя сессии: {session_name}")
    print(f"Relay слушает локальный адрес 127.0.0.1:{daemon_port}")
    print(f"Файл сессии: {session_path}")
    print(f"Режим sudo: {'включён' if args.enable_sudo else 'выключен'}")
    print(f"Для завершения нажмите Ctrl+C или выполните команду: stop --name {session_name}")

    try:
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle_client, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        print("\nПолучен Ctrl+C, сессия завершается.")
    finally:
        server.close()
        cleanup()
    return 0


def print_legacy_command_result(result: dict[str, Any]) -> int:
    if not result.get("ok"):
        print(f"Ошибка relay: {result.get('protocol_error', 'неизвестная ошибка')}", file=sys.stderr)
        return 1
    if result.get("stdout"):
        sys.stdout.write(str(result["stdout"]))
    if result.get("stderr"):
        sys.stderr.write(str(result["stderr"]))
    return int(result.get("exit_code", 1))


def print_command_result(result: dict[str, Any]) -> int:
    if result.get("stdout"):
        sys.stdout.write(str(result["stdout"]))
    if result.get("stderr"):
        sys.stderr.write(str(result["stderr"]))

    status = result.get("operation_status")
    if status == "succeeded":
        return int(result.get("command_exit_code") or 0)
    if status == "command_failed":
        return int(result.get("command_exit_code") or 1)
    message = str(result.get("error_message") or "Неизвестная ошибка relay.")
    if status == "partial_success":
        print(
            f"Ошибка relay: удалённая команда выполнена успешно, но receipt не подтверждён. {message} "
            "Состояние хоста изменено; требуется проверка.",
            file=sys.stderr,
        )
    elif status == "unknown":
        print(f"Ошибка relay: результат удалённой команды неизвестен. {message}", file=sys.stderr)
    else:
        print(f"Ошибка relay: {message}", file=sys.stderr)
    return 1


def print_machine_result(result: dict[str, Any]) -> int:
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return machine_exit_code(result)


def local_machine_failure(
    *,
    session: dict[str, Any] | None,
    action: str,
    risky: bool,
    transaction_id: str,
    transaction_id_source: str,
    change_target: str | None,
    change_description: str,
    receipt_path: str,
    command: str,
    unknown: bool,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    result = machine_result_base(
        session=session,
        action=action,
        risky=risky,
        transaction_id=transaction_id,
        transaction_id_source=transaction_id_source,
        change_target=change_target,
        change_description=change_description,
        receipt_path=receipt_path,
        command_hash_value=command_hash(command),
        started_at_utc=utc_now(),
    )
    if unknown:
        result["command_status"] = "unknown"
        if risky:
            result["receipt_status"] = "unknown"
        return finish_machine_result(
            result,
            operation_status="unknown",
            error_code=error_code,
            error_stage="protocol",
            error_message=error_message,
        )
    return finish_machine_result(
        result,
        operation_status="not_started",
        error_code=error_code,
        error_stage="session",
        error_message=error_message,
    )


def run_exec_command(args: argparse.Namespace, *, daemon_action: str, cli_action: str) -> int:
    transaction_id = args.transaction_id or str(uuid.uuid4())
    transaction_id_source = "caller" if args.transaction_id else "relay"
    change_description = args.change_description or "Удалённое изменение"
    session: dict[str, Any] | None = None
    try:
        session_name = validate_session_name(args.name)
        session = read_session(session_name)
        if (args.risky or args.json) and not supports_operation_protocol(session.get("version")):
            raise RelayError(
                f"Активный daemon имеет версию {session.get('version')}, требуется {__version__} "
                "или совместимая более новая minor/patch-версия. Перезапустите daemon перед выполнением операции."
            )
        response_timeout = int(session.get("command_timeout", DEFAULT_COMMAND_TIMEOUT)) + 10
        result = request_daemon(
            session,
            daemon_action,
            command=args.remote_command,
            risky=bool(args.risky),
            receipt_path=args.receipt_path,
            transaction_id=transaction_id,
            transaction_id_source=transaction_id_source,
            change_target=args.change_target,
            change_description=change_description,
            machine_mode=bool(args.json),
            operation_protocol_version=1,
            response_timeout=response_timeout,
        )
        if "operation_status" not in result:
            if not args.json and not args.risky:
                return print_legacy_command_result(result)
            raise DaemonRequestError(
                "Daemon вернул ответ устаревшего формата; результат операции неизвестен.",
                request_sent=True,
                error_code="response_invalid",
            )
    except DaemonRequestError as exc:
        if not exc.request_sent and exc.error_code == "daemon_unavailable":
            remove_session_file(getattr(args, "name", DEFAULT_SESSION_NAME))
        if args.json:
            failure = local_machine_failure(
                session=session,
                action=cli_action,
                risky=bool(args.risky),
                transaction_id=transaction_id,
                transaction_id_source=transaction_id_source,
                change_target=args.change_target,
                change_description=change_description,
                receipt_path=args.receipt_path,
                command=args.remote_command,
                unknown=exc.request_sent,
                error_code=exc.error_code,
                error_message=str(exc),
            )
            return print_machine_result(failure)
        print(str(exc), file=sys.stderr)
        return 1
    except RelayError as exc:
        if args.json:
            failure = local_machine_failure(
                session=session,
                action=cli_action,
                risky=bool(args.risky),
                transaction_id=transaction_id,
                transaction_id_source=transaction_id_source,
                change_target=args.change_target,
                change_description=change_description,
                receipt_path=args.receipt_path,
                command=args.remote_command,
                unknown=False,
                error_code="session_unavailable",
                error_message=str(exc),
            )
            return print_machine_result(failure)
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        return print_machine_result(result)
    return print_command_result(result)


def exec_cmd(args: argparse.Namespace) -> int:
    return run_exec_command(args, daemon_action="exec", cli_action="exec")


def sudo_exec_cmd(args: argparse.Namespace) -> int:
    return run_exec_command(args, daemon_action="sudo_exec", cli_action="sudo-exec")


def download_cmd(args: argparse.Namespace) -> int:
    try:
        session_name = validate_session_name(args.name)
        session = read_session(session_name)
        local_path = Path(args.local_path).expanduser().resolve(strict=False)
        response_timeout = int(session.get("download_timeout", DEFAULT_DOWNLOAD_TIMEOUT)) + 10
        result = request_daemon(
            session,
            "download",
            response_timeout=response_timeout,
            remote_path=args.remote_path,
            local_path=str(local_path),
            overwrite=bool(args.overwrite),
            create_dirs=bool(args.create_dirs),
        )
    except DaemonRequestError as exc:
        if not exc.request_sent:
            remove_session_file(getattr(args, "name", DEFAULT_SESSION_NAME))
        print(str(exc), file=sys.stderr)
        return 1
    except RelayError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not result.get("ok"):
        print(f"Ошибка relay: {result.get('protocol_error', 'неизвестная ошибка')}", file=sys.stderr)
        return 1

    bytes_downloaded = int(result.get("bytes_downloaded", 0))
    print(f"Скачано: {format_bytes(bytes_downloaded)}")
    print(f"Удалённый файл: {result.get('remote_path', args.remote_path)}")
    print(f"Локальный файл: {result.get('local_path', local_path)}")
    return 0


def upload_cmd(args: argparse.Namespace) -> int:
    try:
        session_name = validate_session_name(args.name)
        session = read_session(session_name)
        local_path = Path(args.local_path).expanduser().resolve(strict=False)
        if not local_path.is_file():
            raise RelayError(f"Локальный файл не найден или не является обычным файлом: {local_path}")
        local_size = local_path.stat().st_size
        max_size = int(session.get("upload_max_size", DEFAULT_UPLOAD_MAX_SIZE))
        if local_size > max_size:
            raise RelayError(
                f"Размер локального файла {format_bytes(local_size)} превышает лимит "
                f"{format_bytes(max_size)}. Перезапустите daemon с большим --upload-max-size, если это безопасно."
            )
        content_b64 = base64.b64encode(local_path.read_bytes()).decode("ascii")
        response_timeout = int(session.get("upload_timeout", DEFAULT_UPLOAD_TIMEOUT)) + 10
        result = request_daemon(
            session,
            "upload",
            response_timeout=response_timeout,
            local_path=str(local_path),
            content_b64=content_b64,
            remote_path=args.remote_path,
            overwrite=bool(args.overwrite),
            create_dirs=bool(args.create_dirs),
        )
    except DaemonRequestError as exc:
        if not exc.request_sent:
            remove_session_file(getattr(args, "name", DEFAULT_SESSION_NAME))
        print(str(exc), file=sys.stderr)
        return 1
    except RelayError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not result.get("ok"):
        print(f"Ошибка relay: {result.get('protocol_error', 'неизвестная ошибка')}", file=sys.stderr)
        return 1

    bytes_uploaded = int(result.get("bytes_uploaded", 0))
    print(f"Загружено: {format_bytes(bytes_uploaded)}")
    print(f"Локальный файл: {result.get('local_path', local_path)}")
    print(f"Удалённый файл: {result.get('remote_path', args.remote_path)}")
    return 0


def stop_one_session(name: str) -> int:
    try:
        session = read_session(name)
        result = request_daemon(session, "stop")
    except DaemonRequestError as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        if exc.request_sent:
            print(
                f"{name}: команда stop могла быть получена daemon; session-файл сохранён до повторной проверки status.",
                file=sys.stderr,
            )
        else:
            remove_session_file(name)
            print(f"{name}: файл достоверно недоступной сессии удалён.", file=sys.stderr)
        return 1
    except RelayError as exc:
        remove_session_file(name)
        print(f"{name}: {exc}", file=sys.stderr)
        print(f"{name}: повреждённый или недоступный session-файл удалён.", file=sys.stderr)
        return 1

    if not result.get("ok"):
        print(f"{name}: не удалось остановить relay: {result.get('protocol_error', 'неизвестная ошибка')}", file=sys.stderr)
        return 1
    print(f"{name}: команда завершения отправлена активному daemon.")
    return 0


def stop(args: argparse.Namespace) -> int:
    if args.all:
        names = iter_session_names()
        if not names:
            print("Известные сессии не найдены.")
            return 0
        exit_code = 0
        for name in names:
            if stop_one_session(name) != 0:
                exit_code = 1
        return exit_code
    try:
        name = validate_session_name(args.name)
    except RelayError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return stop_one_session(name)


def print_status(name: str, session: dict[str, Any], result: dict[str, Any]) -> None:
    print(f"Сессия: {name}")
    print(f"Активна: {format_session_target(session)}")
    print(f"Локальный порт: {session['daemon_port']}")
    print(f"Версия relay: {result.get('version', session['version'])}")
    print(f"Режим sudo: {'включён' if result.get('sudo_enabled') else 'выключен'}")
    print(f"Файл сессии: {session.get('_session_file_path', existing_session_file_path(name))}")


def status_one_session(name: str, *, cleanup_stale: bool) -> int:
    try:
        session = read_session(name)
        result = request_daemon(session, "status")
    except DaemonRequestError as exc:
        if cleanup_stale and not exc.request_sent:
            remove_session_file(name)
        print(f"{name}: {exc}", file=sys.stderr)
        if exc.request_sent:
            print(f"{name}: состояние неизвестно; session-файл сохранён.", file=sys.stderr)
        return 1
    except RelayError as exc:
        if cleanup_stale:
            remove_session_file(name)
        print(f"{name}: {exc}", file=sys.stderr)
        return 1

    if not result.get("ok") or result.get("status") != "active":
        print(f"{name}: daemon не подтвердил активную сессию.", file=sys.stderr)
        return 1
    print_status(name, session, result)
    return 0


def status(args: argparse.Namespace) -> int:
    if args.all:
        names = iter_session_names()
        if not names:
            print("Известные сессии не найдены.")
            return 0
        exit_code = 0
        first = True
        for name in names:
            if not first:
                print()
            first = False
            if status_one_session(name, cleanup_stale=False) != 0:
                exit_code = 1
        return exit_code
    try:
        name = validate_session_name(args.name)
    except RelayError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return status_one_session(name, cleanup_stale=True)


def list_sessions(_: argparse.Namespace) -> int:
    names = iter_session_names()
    if not names:
        print("Известные сессии не найдены.")
        return 0

    print("Имя\tСостояние\tSSH\tSudo\tПорт relay\tВерсия")
    exit_code = 0
    for name in names:
        try:
            session = read_session(name)
            result = request_daemon(session, "status")
            if result.get("ok") and result.get("status") == "active":
                state = "активна"
                sudo = "вкл." if result.get("sudo_enabled") else "выкл."
                version = str(result.get("version", session["version"]))
            else:
                state = "ошибка"
                sudo = "?"
                version = str(session["version"])
                exit_code = 1
            print(f"{name}\t{state}\t{format_session_target(session)}\t{sudo}\t{session['daemon_port']}\t{version}")
        except RelayError:
            exit_code = 1
            try:
                session = read_session(name)
                target = format_session_target(session)
                port = session.get("daemon_port", "?")
                version = session.get("version", "?")
            except RelayError:
                target = "?"
                port = "?"
                version = "?"
            print(f"{name}\tнедоступна\t{target}\t?\t{port}\t{version}")
    return exit_code


def add_session_name_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--name",
        "-n",
        default=DEFAULT_SESSION_NAME,
        help=f"Имя relay-сессии, по умолчанию {DEFAULT_SESSION_NAME}.",
    )


def add_exec_operation_arguments(parser: argparse.ArgumentParser, *, sudo: bool) -> None:
    parser.add_argument(
        "--risky",
        action="store_true",
        help="После успешной команды записать безопасный JSONL receipt об изменении на удалённом хосте.",
    )
    parser.add_argument(
        "--receipt-path",
        type=parse_receipt_path,
        default=DEFAULT_RISKY_RECEIPT_PATH,
        help=f"Удалённый JSONL-файл для --risky, по умолчанию {DEFAULT_RISKY_RECEIPT_PATH}.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести один машиночитаемый JSON-объект вместо обычного stdout/stderr.",
    )
    parser.add_argument(
        "--transaction-id",
        type=parse_transaction_id,
        help="Идентификатор внешней транзакции; если не задан, relay создаёт UUIDv4.",
    )
    parser.add_argument(
        "--change-target",
        type=parse_change_target,
        help="Безопасное описание изменяемого объекта без секретов.",
    )
    parser.add_argument(
        "--change-description",
        type=parse_change_description,
        help="Краткое безопасное описание изменения без секретов.",
    )
    parser.add_argument(
        "remote_command",
        help=(
            "Неинтерактивная команда для удалённого сервера без префикса sudo."
            if sudo
            else "Неинтерактивная команда для удалённого сервера."
        ),
    )



def build_parser() -> argparse.ArgumentParser:
    parser = RussianArgumentParser(
        description="Локальный SSH-relay для коротких неинтерактивных удалённых команд.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"ssh_relay {__version__}", help="Показать версию и выйти.")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=RussianArgumentParser)

    daemon_parser = subparsers.add_parser(
        "daemon", help="Открыть SSH-сессию и запустить локальный relay."
    )
    add_session_name_argument(daemon_parser)
    daemon_parser.add_argument("--host", required=True, help="Имя или адрес SSH-сервера.")
    daemon_parser.add_argument("--port", type=parse_port, default=22, help="Порт SSH-сервера, по умолчанию 22.")
    daemon_parser.add_argument("--user", "-u", default=getpass.getuser(), help="Имя SSH-пользователя.")
    daemon_parser.add_argument(
        "--identity-file",
        "-i",
        help=(
            "Путь к приватному SSH-ключу или OpenSSH-сертификату *-cert.pub; "
            "если не задан, запрашивается SSH-пароль."
        ),
    )
    daemon_parser.add_argument(
        "--ask-key-passphrase",
        action="store_true",
        help="Запросить passphrase для зашифрованного ключа, указанного через --identity-file.",
    )
    daemon_parser.add_argument(
        "--known-hosts",
        help="Путь к проверенному файлу known_hosts; по умолчанию используется ~/.ssh/known_hosts.",
    )
    daemon_parser.add_argument(
        "--command-timeout",
        type=parse_positive_seconds,
        default=DEFAULT_COMMAND_TIMEOUT,
        help=f"Предельное время одной команды в секундах, по умолчанию {DEFAULT_COMMAND_TIMEOUT}.",
    )
    daemon_parser.add_argument(
        "--download-timeout",
        type=parse_positive_seconds,
        default=DEFAULT_DOWNLOAD_TIMEOUT,
        help=f"Предельное время одного скачивания в секундах, по умолчанию {DEFAULT_DOWNLOAD_TIMEOUT}.",
    )
    daemon_parser.add_argument(
        "--download-max-size",
        type=parse_size_bytes,
        default=DEFAULT_DOWNLOAD_MAX_SIZE,
        help=f"Предельный размер одного скачиваемого файла, по умолчанию {format_bytes(DEFAULT_DOWNLOAD_MAX_SIZE)}.",
    )
    daemon_parser.add_argument(
        "--upload-timeout",
        type=parse_positive_seconds,
        default=DEFAULT_UPLOAD_TIMEOUT,
        help=f"Предельное время одной загрузки файла в секундах, по умолчанию {DEFAULT_UPLOAD_TIMEOUT}.",
    )
    daemon_parser.add_argument(
        "--upload-max-size",
        type=parse_size_bytes,
        default=DEFAULT_UPLOAD_MAX_SIZE,
        help=f"Предельный размер одного загружаемого файла, по умолчанию {format_bytes(DEFAULT_UPLOAD_MAX_SIZE)}.",
    )
    daemon_parser.add_argument(
        "--enable-sudo",
        action="store_true",
        help="Включить явный режим sudo с ручным вводом sudo-пароля в терминале daemon.",
    )
    daemon_parser.add_argument(
        "--detach",
        action="store_true",
        help="Запустить daemon в отдельном фоне и дождаться активной сессии. Требует --identity-file без passphrase prompt и без sudo.",
    )
    daemon_parser.add_argument(
        "--detach-log",
        help="Путь к лог-файлу detached daemon; по умолчанию используется каталог состояния ssh_relay.",
    )
    daemon_parser.set_defaults(handler=daemon)

    exec_parser = subparsers.add_parser("exec", help="Выполнить одну команду через активный relay.")
    add_session_name_argument(exec_parser)
    add_exec_operation_arguments(exec_parser, sudo=False)
    exec_parser.set_defaults(handler=exec_cmd)

    sudo_exec_parser = subparsers.add_parser(
        "sudo-exec",
        help="Выполнить одну неинтерактивную команду через sudo в активном relay.",
    )
    add_session_name_argument(sudo_exec_parser)
    add_exec_operation_arguments(sudo_exec_parser, sudo=True)
    sudo_exec_parser.set_defaults(handler=sudo_exec_cmd)

    download_parser = subparsers.add_parser("download", help="Скачать один файл с удалённого сервера через активный relay.")
    add_session_name_argument(download_parser)
    download_parser.add_argument("remote_path", help="Путь удалённого файла для скачивания.")
    download_parser.add_argument("local_path", help="Локальный путь для сохранения файла.")
    download_parser.add_argument("--overwrite", action="store_true", help="Перезаписать локальный файл, если он уже существует.")
    download_parser.add_argument("--create-dirs", action="store_true", help="Создать локальный каталог назначения, если он отсутствует.")
    download_parser.set_defaults(handler=download_cmd)

    upload_parser = subparsers.add_parser("upload", help="Загрузить один файл на удалённый сервер через активный relay.")
    add_session_name_argument(upload_parser)
    upload_parser.add_argument("local_path", help="Путь локального файла для загрузки.")
    upload_parser.add_argument("remote_path", help="Удалённый путь для сохранения файла.")
    upload_parser.add_argument("--overwrite", action="store_true", help="Перезаписать удалённый файл, если он уже существует.")
    upload_parser.add_argument("--create-dirs", action="store_true", help="Создать удалённый каталог назначения, если он отсутствует.")
    upload_parser.set_defaults(handler=upload_cmd)

    stop_parser = subparsers.add_parser("stop", help="Корректно остановить активный daemon.")
    add_session_name_argument(stop_parser)
    stop_parser.add_argument("--all", action="store_true", help="Остановить все известные relay-сессии через их токены.")
    stop_parser.set_defaults(handler=stop)

    status_parser = subparsers.add_parser("status", help="Проверить активную сессию daemon.")
    add_session_name_argument(status_parser)
    status_parser.add_argument("--all", action="store_true", help="Проверить все известные relay-сессии.")
    status_parser.set_defaults(handler=status)

    list_parser = subparsers.add_parser("list", help="Показать все известные relay-сессии.")
    list_parser.set_defaults(handler=list_sessions)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
