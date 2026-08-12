#!/usr/bin/env python3
"""CLI-слой ssh_relay с управляемыми длительными задачами.

Основная реализация существующего relay находится в ``ssh_relay_core`` и сохранена
без функциональных изменений. Этот модуль добавляет CLI ``job`` и использует
обычный короткий daemon-action ``exec`` как транспорт для служебных job-команд.
"""

from __future__ import annotations

__version__ = "0.7.0"

import argparse
import sys
import time
from typing import Any

import ssh_relay_core as _core
import ssh_relay_jobs as relay_jobs
from ssh_relay_core import *  # noqa: F403 — сохраняем публичный интерфейс прежнего модуля.

_core.__version__ = __version__


def parse_positive_float_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("время должно быть числом секунд") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("время должно быть положительным числом секунд")
    return seconds


def parse_nonnegative_float_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("время должно быть числом секунд") from exc
    if seconds < 0 or seconds > 60:
        raise argparse.ArgumentTypeError("время должно быть от 0 до 60 секунд")
    return seconds


def parse_tail_lines(value: str) -> int:
    try:
        lines = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("число строк должно быть целым") from exc
    if not 1 <= lines <= relay_jobs.MAX_TAIL_LINES:
        raise argparse.ArgumentTypeError(f"число строк должно быть от 1 до {relay_jobs.MAX_TAIL_LINES}")
    return lines


def parse_tail_bytes(value: str) -> int:
    size = parse_size_bytes(value)  # noqa: F405
    if size > relay_jobs.MAX_TAIL_BYTES:
        raise argparse.ArgumentTypeError(
            f"лимит tail не должен превышать {format_bytes(relay_jobs.MAX_TAIL_BYTES)}"  # noqa: F405
        )
    return size


def daemon(args: argparse.Namespace) -> int:
    """Запускает прежний daemon, сохраняя путь внешнего CLI для ``--detach``."""
    original_file = _core.__file__
    _core.__file__ = __file__
    try:
        return int(_core.daemon(args))
    finally:
        _core.__file__ = original_file


def _job_context(args: argparse.Namespace, *, require_job: bool = True) -> tuple[dict[str, Any], str | None, float]:
    session_name = validate_session_name(args.name)  # noqa: F405
    job_name: str | None = None
    if require_job:
        try:
            job_name = relay_jobs.validate_job_name(args.job)
        except ValueError as exc:
            raise RelayError(str(exc)) from exc  # noqa: F405
    session = read_session(session_name)  # noqa: F405
    response_timeout = (
        int(session.get("command_timeout", DEFAULT_COMMAND_TIMEOUT))  # noqa: F405
        + int(session.get("reconnect_wait", DEFAULT_RECONNECT_WAIT))  # noqa: F405
        + 10
    )
    return session, job_name, response_timeout


def _run_job_control(
    session: dict[str, Any],
    remote_command: str,
    *,
    response_timeout: float,
) -> dict[str, Any]:
    """Выполняет одну короткую служебную job-команду без risky receipt и автоповтора."""
    return request_daemon(  # noqa: F405
        session,
        "exec",
        command=remote_command,
        risky=False,
        receipt_path=DEFAULT_RISKY_RECEIPT_PATH,  # noqa: F405
        response_timeout=response_timeout,
    )


def _service_error(result: dict[str, Any]) -> str | None:
    if not result.get("ok"):
        return str(result.get("protocol_error", "неизвестная ошибка"))
    code = int(result.get("exit_code", 1))
    stdout = str(result.get("stdout", ""))
    reason = relay_jobs.classify_job_command_failure(code, stdout)
    messages = {
        "job_not_found": "Job не найден.",
        "job_active_exists": "Активная задача с таким именем уже существует.",
        "job_unknown_existing": "Для этого имени найдено неполное состояние с неизвестным результатом; новый запуск запрещён.",
        "setsid_missing": "На удалённой машине не найден setsid.",
        "base64_missing": "На удалённой машине не найден base64.",
        "launcher_failed": "Не удалось подтвердить запуск detached job-runner.",
        "identity_mismatch": "Сохранённая идентичность процесса не подтверждена; остановка запрещена.",
        "still_running": "После SIGTERM задача продолжает работать; при необходимости повторите с --force.",
        "start_locked": "Одновременный запуск job с таким именем уже обрабатывается.",
    }
    if reason:
        return messages.get(reason, f"Служебная job-команда завершилась с ошибкой: {reason}.")
    if code != 0:
        stderr = str(result.get("stderr", "")).strip()
        return stderr or f"Служебная job-команда завершилась с кодом {code}."
    return None


def _status_from_control(result: dict[str, Any]) -> dict[str, Any]:
    error = _service_error(result)
    if error:
        raise RelayError(error)  # noqa: F405
    return relay_jobs.parse_job_status(str(result.get("stdout", "")))


def print_job_status(result: dict[str, Any]) -> None:
    print(f"Job: {result.get('job', '?')}")
    print(f"Состояние: {result.get('state', 'unknown')}")
    print(f"PID: {result.get('pid') if result.get('pid') is not None else '-'}")
    print(f"Время работы: {int(result.get('elapsed', 0) or 0)} с")
    print(f"Код завершения: {result.get('exit_code') if result.get('exit_code') is not None else '-'}")
    print(f"Размер журнала: {format_bytes(int(result.get('log_size', 0) or 0))}")  # noqa: F405
    log_age = int(result.get("log_age", -1) if result.get("log_age") is not None else -1)
    print(f"Возраст журнала: {log_age} с" if log_age >= 0 else "Возраст журнала: -")


def job_start_cmd(args: argparse.Namespace) -> int:
    try:
        session, job_name, response_timeout = _job_context(args)
        assert job_name is not None
        remote_command = relay_jobs.build_job_start_command(job_name, args.remote_command)
        result = _run_job_control(session, remote_command, response_timeout=response_timeout)
    except DaemonUnavailableError as exc:  # noqa: F405
        print(str(exc), file=sys.stderr)
        print(
            "Результат job start неизвестен. Не повторяйте запуск автоматически; после восстановления relay "
            f"проверьте job status --name {args.name} --job {args.job} или job list.",
            file=sys.stderr,
        )
        return 1
    except (RelayError, ValueError) as exc:  # noqa: F405
        print(str(exc), file=sys.stderr)
        return 1

    if not result.get("ok"):
        message = str(result.get("protocol_error", "неизвестная ошибка"))
        print(f"Ошибка relay: {message}", file=sys.stderr)
        if "Результат операции неизвестен" in message:
            print(
                "Не повторяйте job start автоматически; после reconnect сначала проверьте job status/job list.",
                file=sys.stderr,
            )
        return 1
    error = _service_error(result)
    if error:
        print(f"Ошибка relay: {error}", file=sys.stderr)
        return 1
    status = relay_jobs.parse_job_status(str(result.get("stdout", "")))
    print_job_status(status)
    print("Запуск механизма job подтверждён; это не подтверждение успешного завершения длительной команды.")
    return 0


def _job_status_request(session: dict[str, Any], job_name: str, response_timeout: float) -> dict[str, Any]:
    result = _run_job_control(
        session,
        relay_jobs.build_job_status_command(job_name),
        response_timeout=response_timeout,
    )
    return _status_from_control(result)


def job_status_cmd(args: argparse.Namespace) -> int:
    try:
        session, job_name, response_timeout = _job_context(args)
        assert job_name is not None
        status = _job_status_request(session, job_name, response_timeout)
    except (RelayError, ValueError) as exc:  # noqa: F405
        print(str(exc), file=sys.stderr)
        return 1
    print_job_status(status)
    return 0 if status.get("state") in {"running", "succeeded", "failed"} else 1


def job_tail_cmd(args: argparse.Namespace) -> int:
    try:
        session, job_name, response_timeout = _job_context(args)
        assert job_name is not None
        command = relay_jobs.build_job_tail_command(job_name, lines=args.lines, max_bytes=args.max_bytes)
        result = _run_job_control(session, command, response_timeout=response_timeout)
    except (RelayError, ValueError) as exc:  # noqa: F405
        print(str(exc), file=sys.stderr)
        return 1
    error = _service_error(result)
    if error:
        print(f"Ошибка relay: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(str(result.get("stdout", "")))
    return 0


def job_wait_cmd(args: argparse.Namespace) -> int:
    try:
        session, job_name, response_timeout = _job_context(args)
        assert job_name is not None
    except (RelayError, ValueError) as exc:  # noqa: F405
        print(str(exc), file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.timeout
    last_status: dict[str, Any] = {"job": job_name, "state": "unknown"}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print_job_status(last_status)
            print(
                f"Локальный предел ожидания {args.timeout:g} с истёк; удалённая задача не остановлена.",
                file=sys.stderr,
            )
            return 124
        try:
            daemon_status = request_daemon(session, "status", response_timeout=min(5.0, remaining))  # noqa: F405
            if daemon_status.get("ssh_status", daemon_status.get("status")) != "connected":
                last_status = {"job": job_name, "state": "unknown"}
            else:
                last_status = _job_status_request(
                    session,
                    job_name,
                    min(response_timeout, max(1.0, remaining)),
                )
        except DaemonUnavailableError as exc:  # noqa: F405
            print(str(exc), file=sys.stderr)
            print(
                "Локальный wait прерван; это не означает завершение или ошибку удалённой задачи.",
                file=sys.stderr,
            )
            return 1
        except RelayError as exc:  # noqa: F405
            print(str(exc), file=sys.stderr)
            return 1

        state = last_status.get("state")
        if state == "succeeded":
            print_job_status(last_status)
            return 0
        if state == "failed":
            print_job_status(last_status)
            exit_code = last_status.get("exit_code")
            return int(exit_code) if isinstance(exit_code, int) and 1 <= exit_code <= 255 else 1
        time.sleep(min(args.poll_interval, max(0.0, deadline - time.monotonic())))


def job_stop_cmd(args: argparse.Namespace) -> int:
    try:
        session, job_name, response_timeout = _job_context(args)
        assert job_name is not None
        command = relay_jobs.build_job_stop_command(job_name, force=bool(args.force), grace_seconds=float(args.grace))
        result = _run_job_control(session, command, response_timeout=response_timeout)
    except (RelayError, ValueError) as exc:  # noqa: F405
        print(str(exc), file=sys.stderr)
        return 1
    error = _service_error(result)
    if error:
        print(f"Ошибка relay: {error}", file=sys.stderr)
        return 1
    print_job_status(relay_jobs.parse_job_status(str(result.get("stdout", ""))))
    return 0


def job_list_cmd(args: argparse.Namespace) -> int:
    try:
        session, _, response_timeout = _job_context(args, require_job=False)
        result = _run_job_control(session, relay_jobs.build_job_list_command(), response_timeout=response_timeout)
    except (RelayError, ValueError) as exc:  # noqa: F405
        print(str(exc), file=sys.stderr)
        return 1
    error = _service_error(result)
    if error:
        print(f"Ошибка relay: {error}", file=sys.stderr)
        return 1
    items = relay_jobs.parse_job_list(str(result.get("stdout", "")))
    if not items:
        print("Управляемые длительные задачи не найдены.")
        return 0
    print("Job\tСостояние\tPID\tКод завершения")
    for item in items:
        print(
            f"{item.get('job', '?')}\t{item.get('state', 'unknown')}\t"
            f"{item.get('pid') if item.get('pid') is not None else '-'}\t"
            f"{item.get('exit_code') if item.get('exit_code') is not None else '-'}"
        )
    return 0


def _top_level_subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("В базовом parser не найдены подкоманды.")


def build_parser() -> argparse.ArgumentParser:
    parser = _core.build_parser()
    subparsers = _top_level_subparsers(parser)

    # При --detach прежняя реализация должна повторно запустить внешний ssh_relay.py,
    # а не внутренний модуль ssh_relay_core.py.
    subparsers.choices["daemon"].set_defaults(handler=daemon)

    job_parser = subparsers.add_parser("job", help="Управлять длительными неинтерактивными задачами.")
    job_subparsers = job_parser.add_subparsers(
        dest="job_command",
        required=True,
        parser_class=RussianArgumentParser,  # noqa: F405
    )

    start_parser = job_subparsers.add_parser("start", help="Запустить detached длительную задачу.")
    add_session_name_argument(start_parser)  # noqa: F405
    start_parser.add_argument("--job", required=True, help="Безопасное имя задачи.")
    start_parser.add_argument("remote_command", help="Неинтерактивная команда для удалённого Linux shell.")
    start_parser.set_defaults(handler=job_start_cmd)

    status_parser = job_subparsers.add_parser("status", help="Показать сохранённое состояние длительной задачи.")
    add_session_name_argument(status_parser)  # noqa: F405
    status_parser.add_argument("--job", required=True, help="Имя длительной задачи.")
    status_parser.set_defaults(handler=job_status_cmd)

    tail_parser = job_subparsers.add_parser("tail", help="Показать ограниченный хвост журнала длительной задачи.")
    add_session_name_argument(tail_parser)  # noqa: F405
    tail_parser.add_argument("--job", required=True, help="Имя длительной задачи.")
    tail_parser.add_argument(
        "--lines",
        type=parse_tail_lines,
        default=relay_jobs.DEFAULT_TAIL_LINES,
        help=f"Число последних строк, по умолчанию {relay_jobs.DEFAULT_TAIL_LINES}, максимум {relay_jobs.MAX_TAIL_LINES}.",
    )
    tail_parser.add_argument(
        "--bytes",
        dest="max_bytes",
        type=parse_tail_bytes,
        default=relay_jobs.DEFAULT_TAIL_BYTES,
        help=f"Лимит прочитанных байт, по умолчанию {format_bytes(relay_jobs.DEFAULT_TAIL_BYTES)}, максимум {format_bytes(relay_jobs.MAX_TAIL_BYTES)}.",  # noqa: F405
    )
    tail_parser.set_defaults(handler=job_tail_cmd)

    wait_parser = job_subparsers.add_parser("wait", help="Локально опрашивать состояние до завершения или timeout.")
    add_session_name_argument(wait_parser)  # noqa: F405
    wait_parser.add_argument("--job", required=True, help="Имя длительной задачи.")
    wait_parser.add_argument(
        "--poll-interval",
        type=parse_positive_float_seconds,
        default=relay_jobs.DEFAULT_WAIT_POLL_INTERVAL,
        help=f"Интервал опроса в секундах, по умолчанию {relay_jobs.DEFAULT_WAIT_POLL_INTERVAL:g}.",
    )
    wait_parser.add_argument(
        "--timeout",
        type=parse_positive_float_seconds,
        default=relay_jobs.DEFAULT_WAIT_TIMEOUT,
        help=f"Локальный предел ожидания, по умолчанию {relay_jobs.DEFAULT_WAIT_TIMEOUT:g} с; job не останавливает.",
    )
    wait_parser.set_defaults(handler=job_wait_cmd)

    stop_parser = job_subparsers.add_parser("stop", help="Безопасно остановить задачу по сохранённой идентичности.")
    add_session_name_argument(stop_parser)  # noqa: F405
    stop_parser.add_argument("--job", required=True, help="Имя длительной задачи.")
    stop_parser.add_argument(
        "--grace",
        type=parse_nonnegative_float_seconds,
        default=relay_jobs.DEFAULT_STOP_GRACE,
        help=f"Ожидание после SIGTERM, по умолчанию {relay_jobs.DEFAULT_STOP_GRACE:g} с, максимум 60.",
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="После неуспешного SIGTERM выполнить отдельную ступень SIGKILL.",
    )
    stop_parser.set_defaults(handler=job_stop_cmd)

    list_parser = job_subparsers.add_parser("list", help="Показать длительные задачи выбранной relay-сессии.")
    add_session_name_argument(list_parser)  # noqa: F405
    list_parser.set_defaults(handler=job_list_cmd)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
