#!/usr/bin/env python3
"""
ssh_relay.py — локальный SSH-relay для выполнения неинтерактивных команд.

Примеры:
  py ssh_relay.py daemon --host 198.51.100.42 --user donpedro
  py ssh_relay.py daemon --host 198.51.100.42 --user donpedro -i ~/.ssh/id_ed25519
  py ssh_relay.py exec "hostname"
  py ssh_relay.py status
  py ssh_relay.py stop
"""

__version__ = "0.1.0"

import argparse
import atexit
import getpass
import json
import os
import socket
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


def session_file_path() -> Path:
    """Возвращает фиксированное пользовательское расположение файла сессии."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "ssh_relay" / ".ssh_relay_session.json"


SESSION_FILE = session_file_path()


def prepare_session_directory() -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(SESSION_FILE.parent, 0o700)


def remove_session_file(expected_token: str | None = None) -> None:
    """Удаляет файл только текущей сессии, если задан ожидаемый токен."""
    try:
        if expected_token is not None and SESSION_FILE.exists():
            current = read_session()
            if current["auth_token"] != expected_token:
                return
        SESSION_FILE.unlink(missing_ok=True)
    except (OSError, RelayError):
        pass


def write_session(session: dict[str, Any]) -> None:
    prepare_session_directory()
    temporary = SESSION_FILE.with_suffix(".tmp")
    data = json.dumps(session, ensure_ascii=False, indent=2)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, SESSION_FILE)
        if os.name != "nt":
            os.chmod(SESSION_FILE, 0o600)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_session() -> dict[str, Any]:
    if not SESSION_FILE.exists():
        raise RelayError("Активная сессия не найдена. Сначала запустите команду daemon.")
    try:
        with SESSION_FILE.open("r", encoding="utf-8") as source:
            session = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RelayError(f"Файл сессии повреждён или недоступен: {SESSION_FILE}") from exc

    if not isinstance(session, dict):
        raise RelayError(f"Файл сессии имеет неверный формат: {SESSION_FILE}")
    for field, expected_type in REQUIRED_SESSION_FIELDS.items():
        if not isinstance(session.get(field), expected_type):
            raise RelayError(f"Файл сессии имеет неверный формат: отсутствует поле {field}.")
    return session


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


def request_daemon(session: dict[str, Any], action: str, **payload: Any) -> dict[str, Any]:
    request = {"auth_token": session["auth_token"], "action": action, **payload}
    try:
        with socket.create_connection(("127.0.0.1", session["daemon_port"]), timeout=5) as sock:
            sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            return read_message(sock)
    except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
        raise RelayError("Daemon недоступен. Файл устаревшей сессии будет удалён.") from exc


def load_paramiko():
    try:
        import paramiko
    except ImportError as exc:
        raise RelayError("Не установлена зависимость paramiko. Выполните: py -m pip install paramiko") from exc
    return paramiko


def execute_remote_command(client: Any, command: str, timeout_seconds: int) -> dict[str, Any]:
    """Выполняет команду без PTY, одновременно вычитывая stdout и stderr."""
    channel = client.get_transport().open_session(timeout=10)
    channel.exec_command(command)
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


def check_existing_session() -> bool:
    if not SESSION_FILE.exists():
        return False
    try:
        session = read_session()
        result = request_daemon(session, "status")
        if result.get("ok"):
            print(
                f"Уже существует активная сессия: {session['user']}@{session['host']}:{session['port']}.",
                file=sys.stderr,
            )
            print("Сначала завершите её командой stop.", file=sys.stderr)
            return True
    except RelayError:
        remove_session_file()
    return False


def daemon(args: argparse.Namespace) -> int:
    if args.ask_key_passphrase and not args.identity_file:
        print("Параметр --ask-key-passphrase допустим только вместе с --identity-file.", file=sys.stderr)
        return 2

    if check_existing_session():
        return 1

    identity_file: str | None = None
    password: str | None = None
    passphrase: str | None = None
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
        "version": __version__,
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "daemon_port": daemon_port,
        "auth_token": auth_token,
        "pid": os.getpid(),
    }
    try:
        write_session(session)
    except OSError as exc:
        server.close()
        client.close()
        print(f"Не удалось безопасно записать файл сессии: {exc}", file=sys.stderr)
        return 1

    stop_event = threading.Event()
    command_lock = threading.Lock()
    cleanup_done = False

    def cleanup() -> None:
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        remove_session_file(auth_token)
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
                    reply({"ok": True, "status": "active", "version": __version__})
                    return
                if action == "stop":
                    reply({"ok": True, "status": "stopping"})
                    stop_event.set()
                    return
                if action != "exec":
                    reply({"ok": False, "protocol_error": "Неизвестное действие relay."})
                    return

                command = request.get("command")
                if not isinstance(command, str) or not command.strip():
                    reply({"ok": False, "protocol_error": "Передана пустая удалённая команда."})
                    return

                with command_lock:
                    result = execute_remote_command(client, command, args.command_timeout)
                reply(result)
            except (socket.timeout, TimeoutError):
                reply({"ok": False, "protocol_error": "Истекло время ожидания локального запроса."})
            except RelayError as exc:
                reply({"ok": False, "protocol_error": str(exc)})
            except Exception as exc:
                reply({"ok": False, "protocol_error": f"Внутренняя ошибка daemon: {exc}"})

    print(f"SSH-соединение установлено: {args.user}@{args.host}:{args.port}")
    print(f"Relay слушает локальный адрес 127.0.0.1:{daemon_port}")
    print(f"Файл сессии: {SESSION_FILE}")
    print("Для завершения нажмите Ctrl+C или выполните команду stop.")

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


def exec_cmd(args: argparse.Namespace) -> int:
    try:
        session = read_session()
        result = request_daemon(session, "exec", command=args.remote_command)
    except RelayError as exc:
        remove_session_file()
        print(str(exc), file=sys.stderr)
        return 1

    if not result.get("ok"):
        print(f"Ошибка relay: {result.get('protocol_error', 'неизвестная ошибка')}", file=sys.stderr)
        return 1
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
    return int(result.get("exit_code", 1))


def stop(_: argparse.Namespace) -> int:
    try:
        session = read_session()
        result = request_daemon(session, "stop")
    except RelayError as exc:
        remove_session_file()
        print(str(exc), file=sys.stderr)
        print("Файл неактивной сессии удалён; завершение daemon не подтверждено.", file=sys.stderr)
        return 1

    if not result.get("ok"):
        print(f"Не удалось остановить relay: {result.get('protocol_error', 'неизвестная ошибка')}", file=sys.stderr)
        return 1
    print("Команда завершения отправлена активному daemon.")
    return 0


def status(_: argparse.Namespace) -> int:
    try:
        session = read_session()
        result = request_daemon(session, "status")
    except RelayError as exc:
        if SESSION_FILE.exists():
            remove_session_file()
        print(str(exc), file=sys.stderr)
        return 1

    if not result.get("ok") or result.get("status") != "active":
        print("Daemon не подтвердил активную сессию.", file=sys.stderr)
        return 1
    print(f"Активна: {session['user']}@{session['host']}:{session['port']}")
    print(f"Локальный порт: {session['daemon_port']}")
    print(f"Версия relay: {result.get('version', session['version'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = RussianArgumentParser(
        description="Локальный SSH-relay для коротких неинтерактивных удалённых команд.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"ssh_relay {__version__}", help="Показать версию и выйти.")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=RussianArgumentParser)

    daemon_parser = subparsers.add_parser(
        "daemon", help="Открыть SSH-сессию и запустить локальный relay."
    )
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
    daemon_parser.set_defaults(handler=daemon)

    exec_parser = subparsers.add_parser("exec", help="Выполнить одну команду через активный relay.")
    exec_parser.add_argument("remote_command", help="Неинтерактивная команда для удалённого сервера.")
    exec_parser.set_defaults(handler=exec_cmd)

    stop_parser = subparsers.add_parser("stop", help="Корректно остановить активный daemon.")
    stop_parser.set_defaults(handler=stop)

    status_parser = subparsers.add_parser("status", help="Проверить активную сессию daemon.")
    status_parser.set_defaults(handler=status)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
