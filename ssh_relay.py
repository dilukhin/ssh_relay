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

__version__ = "0.5.0"

import argparse
import atexit
import getpass
import json
import os
import posixpath
import re
import shlex
import socket
import stat
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

BUFFER_SIZE = 64 * 1024
MAX_OUTPUT_SIZE = 4 * 1024 * 1024
MAX_MESSAGE_SIZE = 32 * 1024 * 1024
DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_DOWNLOAD_TIMEOUT = 300
DEFAULT_DOWNLOAD_MAX_SIZE = 64 * 1024 * 1024
DEFAULT_UPLOAD_TIMEOUT = 300
DEFAULT_UPLOAD_MAX_SIZE = 64 * 1024 * 1024
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
    try:
        with socket.create_connection(("127.0.0.1", session["daemon_port"]), timeout=5) as sock:
            sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(response_timeout)
            return read_message(sock)
    except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
        raise RelayError("Daemon недоступен или не ответил вовремя.") from exc


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
    channel = client.get_transport().open_session(timeout=10)
    channel.exec_command(command)
    if stdin_data is not None:
        channel.sendall(stdin_data)
    channel.shutdown_write()
    output: list[bytes] = []
    errors: list[bytes] = []
    total_size = 0
    started = time.monotonic()

    try:
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
                raise RelayError(
                    "Вывод удалённой команды превышает допустимый размер 4 МиБ. "
                    "Используйте фильтрацию или запись результата в файл на сервере."
                )
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic() - started > timeout_seconds:
                raise RelayError(
                    f"Превышено время выполнения команды: {timeout_seconds} с. "
                    "Relay предназначен для коротких команд."
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
    finally:
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
            remote_stat = sftp.stat(remote_path)
        except OSError as exc:
            raise RelayError(f"Удалённый файл не найден или недоступен: {remote_path}") from exc

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
            with sftp.open(remote_path, "rb") as remote_file, temporary.open("xb") as output:
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
            "remote_path": remote_path,
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


def upload_local_file(
    client: Any,
    local_path: str,
    remote_path: str,
    *,
    overwrite: bool,
    create_dirs: bool,
    max_size: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Загружает один обычный локальный файл через SFTP на удалённый сервер."""
    if not local_path.strip():
        raise RelayError("Передан пустой путь локального файла.")
    if not remote_path.strip():
        raise RelayError("Передан пустой путь удалённого файла.")
    if remote_path.endswith("/"):
        raise RelayError("Удалённый путь должен указывать на файл, а не на каталог.")
    if "\x00" in remote_path:
        raise RelayError("Удалённый путь содержит недопустимый нулевой символ.")

    source = Path(local_path).expanduser()
    if not source.is_absolute():
        raise RelayError("Локальный путь должен быть абсолютным.")
    if not source.exists():
        raise RelayError(f"Локальный файл не найден: {source}")
    if not source.is_file():
        raise RelayError("Локальный путь должен указывать на обычный файл. Загрузка каталогов не поддерживается.")

    try:
        local_size = source.stat().st_size
    except OSError as exc:
        raise RelayError(f"Не удалось прочитать параметры локального файла: {source}") from exc
    if local_size > max_size:
        raise RelayError(
            f"Размер локального файла {format_bytes(local_size)} превышает лимит "
            f"{format_bytes(max_size)}. Перезапустите daemon с большим --upload-max-size, "
            "если это безопасно."
        )

    remote_target = remote_path.rstrip("/")
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
            with source.open("rb") as local_file, sftp.open(temporary, "wb") as remote_file:
                while True:
                    chunk = local_file.read(BUFFER_SIZE)
                    if not chunk:
                        break
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
            "local_path": str(source),
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
    except RelayError:
        remove_session_file(name)
    return False


def daemon(args: argparse.Namespace) -> int:
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
                if action == "sudo_exec" and not args.enable_sudo:
                    reply({
                        "ok": False,
                        "protocol_error": "Режим sudo не включён. Перезапустите daemon с параметром --enable-sudo.",
                    })
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
                    overwrite = request.get("overwrite")
                    create_dirs = request.get("create_dirs")
                    if not isinstance(local_path, str) or not isinstance(remote_path, str):
                        reply({"ok": False, "protocol_error": "Для загрузки нужны локальный и удалённый путь."})
                        return
                    if not isinstance(overwrite, bool) or not isinstance(create_dirs, bool):
                        reply({"ok": False, "protocol_error": "Некорректные параметры загрузки."})
                        return
                    with command_lock:
                        result = upload_local_file(
                            client,
                            local_path,
                            remote_path,
                            overwrite=overwrite,
                            create_dirs=create_dirs,
                            max_size=args.upload_max_size,
                            timeout_seconds=args.upload_timeout,
                        )
                    reply(result)
                    return

                command = request.get("command")
                if not isinstance(command, str) or not command.strip():
                    reply({"ok": False, "protocol_error": "Передана пустая удалённая команда."})
                    return

                with command_lock:
                    if action == "sudo_exec":
                        if sudo_password is None:
                            result = {
                                "ok": False,
                                "protocol_error": "Режим sudo недоступен: sudo-пароль отсутствует в памяти daemon.",
                            }
                        else:
                            result = execute_sudo_command(client, command, args.command_timeout, sudo_password)
                    else:
                        result = execute_remote_command(client, command, args.command_timeout)
                reply(result)
            except (socket.timeout, TimeoutError):
                reply({"ok": False, "protocol_error": "Истекло время ожидания локального запроса."})
            except RelayError as exc:
                reply({"ok": False, "protocol_error": str(exc)})
            except Exception as exc:
                reply({"ok": False, "protocol_error": f"Внутренняя ошибка daemon: {exc}"})

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


def print_command_result(result: dict[str, Any]) -> int:
    if not result.get("ok"):
        print(f"Ошибка relay: {result.get('protocol_error', 'неизвестная ошибка')}", file=sys.stderr)
        return 1
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
    return int(result.get("exit_code", 1))


def exec_cmd(args: argparse.Namespace) -> int:
    try:
        session_name = validate_session_name(args.name)
        session = read_session(session_name)
        response_timeout = int(session.get("command_timeout", DEFAULT_COMMAND_TIMEOUT)) + 10
        result = request_daemon(session, "exec", command=args.remote_command, response_timeout=response_timeout)
    except RelayError as exc:
        remove_session_file(getattr(args, "name", DEFAULT_SESSION_NAME))
        print(str(exc), file=sys.stderr)
        return 1

    return print_command_result(result)


def sudo_exec_cmd(args: argparse.Namespace) -> int:
    try:
        session_name = validate_session_name(args.name)
        session = read_session(session_name)
        response_timeout = int(session.get("command_timeout", DEFAULT_COMMAND_TIMEOUT)) + 10
        result = request_daemon(session, "sudo_exec", command=args.remote_command, response_timeout=response_timeout)
    except RelayError as exc:
        remove_session_file(getattr(args, "name", DEFAULT_SESSION_NAME))
        print(str(exc), file=sys.stderr)
        return 1

    return print_command_result(result)


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
    except RelayError as exc:
        remove_session_file(getattr(args, "name", DEFAULT_SESSION_NAME))
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
        response_timeout = int(session.get("upload_timeout", DEFAULT_UPLOAD_TIMEOUT)) + 10
        result = request_daemon(
            session,
            "upload",
            response_timeout=response_timeout,
            local_path=str(local_path),
            remote_path=args.remote_path,
            overwrite=bool(args.overwrite),
            create_dirs=bool(args.create_dirs),
        )
    except RelayError as exc:
        remove_session_file(getattr(args, "name", DEFAULT_SESSION_NAME))
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
    except RelayError as exc:
        remove_session_file(name)
        print(f"{name}: {exc}", file=sys.stderr)
        print(f"{name}: файл неактивной сессии удалён; завершение daemon не подтверждено.", file=sys.stderr)
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
    daemon_parser.set_defaults(handler=daemon)

    exec_parser = subparsers.add_parser("exec", help="Выполнить одну команду через активный relay.")
    add_session_name_argument(exec_parser)
    exec_parser.add_argument("remote_command", help="Неинтерактивная команда для удалённого сервера.")
    exec_parser.set_defaults(handler=exec_cmd)

    sudo_exec_parser = subparsers.add_parser(
        "sudo-exec",
        help="Выполнить одну неинтерактивную команду через sudo в активном relay.",
    )
    add_session_name_argument(sudo_exec_parser)
    sudo_exec_parser.add_argument("remote_command", help="Неинтерактивная команда для удалённого сервера без префикса sudo.")
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
