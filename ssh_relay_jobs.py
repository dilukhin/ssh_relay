"""Удалённый протокол управляемых длительных задач ssh_relay.

Модуль не открывает SSH самостоятельно: он только формирует короткие POSIX shell-команды
и разбирает их ограниченный вывод. Это позволяет тестировать жизненный цикл job локально.
"""

from __future__ import annotations

import base64
import re
import shlex
import time
from typing import Any, Callable

JOB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_JOB_COMMAND_BYTES = 64 * 1024
DEFAULT_TAIL_LINES = 80
MAX_TAIL_LINES = 1000
DEFAULT_TAIL_BYTES = 64 * 1024
MAX_TAIL_BYTES = 256 * 1024
DEFAULT_WAIT_POLL_INTERVAL = 5.0
DEFAULT_WAIT_TIMEOUT = 3600.0
DEFAULT_STOP_GRACE = 5.0

_START_ACTIVE = 17
_START_UNKNOWN = 18
_NOT_FOUND = 44


def validate_job_name(name: str) -> str:
    """Проверяет имя job, используемое как один компонент удалённого пути."""
    if not JOB_NAME_PATTERN.fullmatch(name) or name in {".", ".."}:
        raise ValueError(
            "Недопустимое имя job. Используйте 1-64 символа: латинские буквы, цифры, точка, дефис или подчёркивание."
        )
    return name


def validate_tail_limits(lines: int, max_bytes: int) -> tuple[int, int]:
    if not 1 <= lines <= MAX_TAIL_LINES:
        raise ValueError(f"Число строк tail должно быть от 1 до {MAX_TAIL_LINES}.")
    if not 1 <= max_bytes <= MAX_TAIL_BYTES:
        raise ValueError(f"Лимит tail должен быть от 1 до {MAX_TAIL_BYTES} байт.")
    return lines, max_bytes


def _process_alive_shell() -> str:
    return r'''
job_process_alive() {
  p=$1; expected_ticks=$2
  case "$p:$expected_ticks" in ""|:*|*:) return 1 ;; esac
  case "$p" in *[!0-9]*) return 1 ;; esac
  [ -r "/proc/$p/stat" ] || return 1
  proc_stat=$(cat "/proc/$p/stat" 2>/dev/null) || return 1
  proc_rest=${proc_stat##*) }
  set -- $proc_rest
  [ "$#" -ge 20 ] || return 1
  proc_state=$1; current_ticks=${20}
  [ "$proc_state" != "Z" ] && [ "$proc_state" != "X" ] || return 1
  [ "$current_ticks" = "$expected_ticks" ] || return 1
  kill -0 "$p" 2>/dev/null
}
'''.strip()


def _state_prelude(job: str) -> str:
    validate_job_name(job)
    return "\n".join(
        [
            "set -u",
            "umask 077",
            'root="${XDG_STATE_HOME:-$HOME/.local/state}/ssh_relay/jobs"',
            f"job={shlex.quote(job)}",
            'jobdir="$root/$job"',
            _process_alive_shell(),
        ]
    )

def _status_values_shell() -> str:
    return r'''
now=$(date +%s)
pid=""
start_ticks=""
started_epoch=""
exit_code=""
state="unknown"
elapsed="0"
log_size="0"
log_age="-1"
if [ -r "$jobdir/pid" ]; then pid=$(cat "$jobdir/pid" 2>/dev/null || true); fi
if [ -r "$jobdir/start_ticks" ]; then start_ticks=$(cat "$jobdir/start_ticks" 2>/dev/null || true); fi
if [ -r "$jobdir/started_epoch" ]; then started_epoch=$(cat "$jobdir/started_epoch" 2>/dev/null || true); fi
alive=0
if job_process_alive "$pid" "$start_ticks"; then alive=1; fi
if [ -r "$jobdir/exit_code" ]; then
  exit_code=$(cat "$jobdir/exit_code" 2>/dev/null || true)
  case "$exit_code" in
    ''|*[!0-9-]*) state="unknown" ;;
    0) state="succeeded" ;;
    *) state="failed" ;;
  esac
elif [ "$alive" -eq 1 ]; then
  state="running"
fi
case "$started_epoch" in
  ''|*[!0-9]*) elapsed=0 ;;
  *) if [ "$now" -ge "$started_epoch" ]; then elapsed=$((now-started_epoch)); fi ;;
esac
if [ -f "$jobdir/log" ]; then
  log_size=$(wc -c < "$jobdir/log" 2>/dev/null | tr -d ' ' || printf '0')
  log_mtime=$(stat -c %Y "$jobdir/log" 2>/dev/null || printf '')
  case "$log_mtime" in ''|*[!0-9]*) log_age=-1 ;; *) log_age=$((now-log_mtime)) ;; esac
fi
'''.strip()


def _emit_status_shell() -> str:
    return "\n".join(
        [
            _status_values_shell(),
            r'''printf 'job=%s\nstate=%s\npid=%s\nelapsed=%s\nexit_code=%s\nlog_size=%s\nlog_age=%s\n' \
  "$job" "$state" "$pid" "$elapsed" "$exit_code" "$log_size" "$log_age"''',
        ]
    )

def build_job_status_command(job: str) -> str:
    return "\n".join(
        [
            _state_prelude(job),
            'if [ ! -d "$jobdir" ]; then printf "job=%s\\nstate=unknown\\nnot_found=1\\n" "$job"; exit 44; fi',
            _emit_status_shell(),
        ]
    )


def build_job_start_command(job: str, command: str) -> str:
    """Формирует короткий launcher; полная команда не сохраняется в job-state."""
    validate_job_name(job)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Команда job не должна быть пустой.")
    if "\x00" in command:
        raise ValueError("Команда job содержит недопустимый нулевой символ.")
    command_size = len(command.encode("utf-8"))
    if command_size > MAX_JOB_COMMAND_BYTES:
        raise ValueError(f"Команда job превышает допустимый размер {MAX_JOB_COMMAND_BYTES} байт.")

    command_b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    runner = r'''
set -u
umask 077
jobdir=$1
pid=$$
printf '%s\n' "$pid" > "$jobdir/pid.tmp" && mv "$jobdir/pid.tmp" "$jobdir/pid"
self_stat=$(cat "/proc/$$/stat" 2>/dev/null || printf '')
self_rest=${self_stat##*) }
set -- $self_rest
start_ticks=''
if [ "$#" -ge 20 ]; then start_ticks=${20}; fi
printf '%s\n' "$start_ticks" > "$jobdir/start_ticks.tmp" && mv "$jobdir/start_ticks.tmp" "$jobdir/start_ticks"
date +%s > "$jobdir/started_epoch.tmp" && mv "$jobdir/started_epoch.tmp" "$jobdir/started_epoch"
: > "$jobdir/log"
command_b64=$(cat)
read_rc=$?
exec </dev/null
if [ "$read_rc" -ne 0 ]; then
  printf '126\n' > "$jobdir/exit_code.tmp" && mv "$jobdir/exit_code.tmp" "$jobdir/exit_code"
  exit 126
fi
command_text=$(printf '%s' "$command_b64" | base64 -d 2>/dev/null)
decode_rc=$?
command_b64=''
if [ "$decode_rc" -ne 0 ]; then
  printf '126\n' > "$jobdir/exit_code.tmp" && mv "$jobdir/exit_code.tmp" "$jobdir/exit_code"
  exit 126
fi
set +e
child_pid=''
trap ':' HUP INT TERM
sh -c "$command_text" >> "$jobdir/log" 2>&1 &
child_pid=$!
command_text=''
while :; do
  wait "$child_pid"
  rc=$?
  if ! kill -0 "$child_pid" 2>/dev/null; then break; fi
done
trap - HUP INT TERM
printf '%s\n' "$rc" > "$jobdir/exit_code.tmp" && mv "$jobdir/exit_code.tmp" "$jobdir/exit_code"
exit "$rc"
'''.strip()

    return "\n".join(
        [
            _state_prelude(job),
            'command -v setsid >/dev/null 2>&1 || { printf "start_error=setsid_missing\\n"; exit 19; }',
            'command -v base64 >/dev/null 2>&1 || { printf "start_error=base64_missing\\n"; exit 19; }',
            'mkdir -p "$root"',
            'lock="$root/.$job.start-lock"',
            'if ! mkdir "$lock" 2>/dev/null; then printf "start_error=start_locked\\n"; exit 17; fi',
            "trap 'rmdir \"$lock\" 2>/dev/null || true' EXIT HUP INT TERM",
            'if [ -d "$jobdir" ]; then',
            '  if [ -r "$jobdir/exit_code" ]; then',
            '    rm -rf "$jobdir"',
            '  else',
            '    pid=""; old_ticks=""; alive=0',
            '    if [ -r "$jobdir/pid" ]; then pid=$(cat "$jobdir/pid" 2>/dev/null || true); fi',
            '    if [ -r "$jobdir/start_ticks" ]; then old_ticks=$(cat "$jobdir/start_ticks" 2>/dev/null || true); fi',
            '    if job_process_alive "$pid" "$old_ticks"; then alive=1; fi',
            '    if [ "$alive" -eq 1 ]; then printf "start_error=active_exists\\n"; exit 17; fi',
            '    printf "start_error=unknown_existing\\n"; exit 18',
            '  fi',
            'fi',
            'mkdir "$jobdir"',
            f"printf '%s\\n' {shlex.quote(command_b64)} | setsid sh -c {shlex.quote(runner)} sh \"$jobdir\" >/dev/null 2>&1 &",
            'launcher_pid=$!',
            'i=0; while [ "$i" -lt 40 ] && [ ! -r "$jobdir/pid" ] && [ ! -r "$jobdir/exit_code" ]; do sleep 0.05; i=$((i+1)); done',
            'if [ ! -r "$jobdir/pid" ]; then printf "start_error=launcher_failed\\nlauncher_pid=%s\\n" "$launcher_pid"; exit 19; fi',
            _emit_status_shell(),
        ]
    )


def build_job_tail_command(job: str, *, lines: int = DEFAULT_TAIL_LINES, max_bytes: int = DEFAULT_TAIL_BYTES) -> str:
    lines, max_bytes = validate_tail_limits(lines, max_bytes)
    return "\n".join(
        [
            _state_prelude(job),
            'if [ ! -d "$jobdir" ]; then exit 44; fi',
            'if [ ! -f "$jobdir/log" ]; then exit 0; fi',
            f'tail -c {max_bytes} -- "$jobdir/log" 2>/dev/null | tail -n {lines}',
        ]
    )


def build_job_stop_command(job: str, *, force: bool, grace_seconds: float = DEFAULT_STOP_GRACE) -> str:
    validate_job_name(job)
    if grace_seconds < 0 or grace_seconds > 60:
        raise ValueError("Период мягкого завершения должен быть от 0 до 60 секунд.")
    checks = max(0, int(round(grace_seconds * 10)))
    force_flag = "1" if force else "0"
    return "\n".join(
        [
            _state_prelude(job),
            'if [ ! -d "$jobdir" ]; then printf "stop_error=not_found\\n"; exit 44; fi',
            'if [ -r "$jobdir/exit_code" ]; then ' + _emit_status_shell() + '; exit 0; fi',
            'pid=""; old_ticks=""',
            'if [ -r "$jobdir/pid" ]; then pid=$(cat "$jobdir/pid" 2>/dev/null || true); fi',
            'if [ -r "$jobdir/start_ticks" ]; then old_ticks=$(cat "$jobdir/start_ticks" 2>/dev/null || true); fi',
            'valid=0',
            'if job_process_alive "$pid" "$old_ticks"; then valid=1; fi',
            'if [ "$valid" -ne 1 ]; then printf "stop_error=identity_mismatch\\n"; ' + _emit_status_shell() + '; exit 20; fi',
            '/bin/kill -TERM -- "-$pid" 2>/dev/null || /bin/kill -TERM "$pid" 2>/dev/null || true',
            f'i=0; while [ "$i" -lt {checks} ]; do if ! job_process_alive "$pid" "$old_ticks"; then break; fi; sleep 0.1; i=$((i+1)); done',
            f'if job_process_alive "$pid" "$old_ticks" && [ "{force_flag}" = "1" ]; then /bin/kill -KILL -- "-$pid" 2>/dev/null || /bin/kill -KILL "$pid" 2>/dev/null || true; sleep 0.05; fi',
            f'if job_process_alive "$pid" "$old_ticks"; then printf "stop_error=still_running\\n"; {_emit_status_shell()}; exit 21; fi',
            f'if [ ! -r "$jobdir/exit_code" ]; then if [ "{force_flag}" = "1" ]; then code=137; else code=143; fi; printf "%s\\n" "$code" > "$jobdir/exit_code.tmp" && mv "$jobdir/exit_code.tmp" "$jobdir/exit_code"; fi',
            _emit_status_shell(),
        ]
    )


def build_job_list_command() -> str:
    status_body = _status_values_shell()
    return "\n".join(
        [
            "set -u",
            "umask 077",
            'root="${XDG_STATE_HOME:-$HOME/.local/state}/ssh_relay/jobs"',
            _process_alive_shell(),
            'if [ ! -d "$root" ]; then exit 0; fi',
            'for jobdir in "$root"/*; do',
            '  [ -d "$jobdir" ] || continue',
            '  job=${jobdir##*/}',
            '  case "$job" in .*) continue ;; esac',
            "  case \"$job\" in *[!A-Za-z0-9_.-]*|'') continue ;; esac",
            status_body,
            "  printf '%s\\t%s\\t%s\\t%s\\n' \"$job\" \"$state\" \"$pid\" \"$exit_code\"",
            'done',
        ]
    )


def parse_job_status(text: str) -> dict[str, Any]:
    """Разбирает ограниченный key=value-ответ status/start/stop."""
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"job", "state", "pid", "elapsed", "exit_code", "log_size", "log_age", "not_found", "start_error", "stop_error"}:
            raw[key] = value
    result: dict[str, Any] = {
        "job": raw.get("job", ""),
        "state": raw.get("state", "unknown"),
        "pid": None,
        "elapsed": 0,
        "exit_code": None,
        "log_size": 0,
        "log_age": -1,
    }
    for key in ("pid", "elapsed", "exit_code", "log_size", "log_age"):
        value = raw.get(key, "")
        if value and re.fullmatch(r"-?\d+", value):
            result[key] = int(value)
    if raw.get("not_found") == "1":
        result["not_found"] = True
    if "start_error" in raw:
        result["start_error"] = raw["start_error"]
    if "stop_error" in raw:
        result["stop_error"] = raw["stop_error"]
    return result


def parse_job_list(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        job, state, pid_text, exit_text = parts
        try:
            validate_job_name(job)
        except ValueError:
            continue
        pid = int(pid_text) if pid_text.isdigit() else None
        exit_code = int(exit_text) if re.fullmatch(r"-?\d+", exit_text or "") else None
        items.append({"job": job, "state": state, "pid": pid, "exit_code": exit_code})
    return items



def wait_for_terminal_status(
    fetch_status: Callable[[], dict[str, Any]],
    *,
    poll_interval: float = DEFAULT_WAIT_POLL_INTERVAL,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], bool]:
    """Локально опрашивает status; True во втором элементе означает локальный timeout."""
    if poll_interval <= 0:
        raise ValueError("Интервал опроса должен быть положительным.")
    if timeout <= 0:
        raise ValueError("Предел ожидания должен быть положительным.")
    deadline = monotonic() + timeout
    last_status: dict[str, Any] = {"state": "unknown"}
    while True:
        last_status = fetch_status()
        if last_status.get("state") in {"succeeded", "failed"}:
            return last_status, False
        remaining = deadline - monotonic()
        if remaining <= 0:
            return last_status, True
        sleep(min(poll_interval, remaining))

def classify_job_command_failure(exit_code: int, stdout: str) -> str | None:
    """Возвращает понятную причину служебного отказа job-команды."""
    parsed = parse_job_status(stdout)
    if exit_code == _NOT_FOUND or parsed.get("not_found"):
        return "job_not_found"
    if exit_code == _START_ACTIVE or parsed.get("start_error") in {"active_exists", "start_locked"}:
        return "job_active_exists"
    if exit_code == _START_UNKNOWN or parsed.get("start_error") == "unknown_existing":
        return "job_unknown_existing"
    if parsed.get("start_error"):
        return str(parsed["start_error"])
    if parsed.get("stop_error"):
        return str(parsed["stop_error"])
    return None
