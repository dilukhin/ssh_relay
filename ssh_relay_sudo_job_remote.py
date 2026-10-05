#!/usr/bin/env python3
"""Самодостаточный помощник root; передаётся через проверенный SSH, без установки.

Этот файл также служит наблюдателем внутри отдельной службы systemd.
Пароль sudo сюда не передаётся. Все входные данные читаются после READY.
"""

import fcntl
import hashlib
import json
import os
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCHEMA = 1
READY = "SSH_RELAY_SUDO_JOB_READY_1"
ROOT = Path("/var/lib/ssh-relay-sudo-jobs")
MAX_INPUT = 131072
MAX_COMMAND = 32768
MAX_RECORDS = 256
MAX_LOG = 1048576
MAX_READ = 65536
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}
TARGET_FIELDS = ("remote_host", "remote_port", "remote_user", "host_key_algorithm", "remote_host_key_sha256")


class Refusal(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise Refusal("invalid_identifier")
    return value


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def secure_directory(path, create=False):
    # Проверяются все предки: обычный пользователь не может подменить путь.
    for parent in reversed((path, *path.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if not create or parent != path:
                raise
            parent.mkdir(mode=0o700)
            info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refusal("unsafe_directory")
    if path == ROOT or path.parent == ROOT:
        if path.stat().st_mode & 0o077:
            raise Refusal("unsafe_directory_permissions")


def open_file(path, flags=os.O_RDONLY):
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_nlink != 1 or info.st_mode & 0o077):
            raise Refusal("unsafe_file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_bytes(path, limit=MAX_INPUT):
    descriptor = open_file(path)
    try:
        value = os.read(descriptor, limit + 1)
        if len(value) > limit:
            raise Refusal("oversized_record")
        return value
    finally:
        os.close(descriptor)


def read_json(path):
    return json.loads(read_bytes(path))


def atomic_write(path, value):
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    descriptor = open_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        # Только root имеет доступ к каталогу, замена не следует ссылке.
        if path.exists() or path.is_symlink():
            check = open_file(path)
            os.close(check)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, canonical(value))


def properties(unit):
    result = subprocess.run(
        ["/usr/bin/systemctl", "--no-ask-password", "show", unit,
         "--property=LoadState,ActiveState,SubState,InvocationID,ExecMainCode,ExecMainStatus,ControlGroup"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=ENV, timeout=5, check=False,
    )
    values = dict(line.split("=", 1) for line in result.stdout.decode("utf-8").splitlines() if "=" in line)
    if result.returncode and values.get("LoadState") != "not-found":
        raise Refusal("systemd_query_failed")
    return values


def preflight():
    if os.geteuid() != 0:
        raise Refusal("root_required")
    if Path("/proc/1/comm").read_text().strip() != "systemd":
        raise Refusal("systemd_required")
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        raise Refusal("cgroup_v2_required")
    for name in ("/usr/bin/python3", "/usr/bin/systemctl", "/usr/bin/systemd-run", "/bin/sh"):
        resolved = Path(name).resolve(strict=True)
        secure_directory(resolved.parent)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refusal("unsafe_binary")
    properties("ssh-relay-nonexistent-preflight.service")


def validate(request):
    if not isinstance(request, dict) or request.get("schema_version") != SCHEMA:
        raise Refusal("unsupported_schema")
    if request.get("operation") not in ("start", "status", "tail", "stop"):
        raise Refusal("invalid_operation")
    identifier(request.get("job_id"))
    identifier(request.get("transaction_id"))
    command_hash = request.get("command_sha256")
    if not isinstance(command_hash, str) or not re.fullmatch("[0-9a-f]{64}", command_hash):
        raise Refusal("invalid_command_hash")
    target = request.get("target")
    if not isinstance(target, dict) or set(target) != set(TARGET_FIELDS):
        raise Refusal("invalid_target")
    if type(target["remote_port"]) is not int or not 1 <= target["remote_port"] <= 65535:
        raise Refusal("invalid_target")
    for field in TARGET_FIELDS:
        if field != "remote_port" and (not isinstance(target[field], str) or not target[field] or "\x00" in target[field]):
            raise Refusal("invalid_target")
    if pwd.getpwuid(int(os.environ.get("SUDO_UID", os.getuid()))).pw_name != target["remote_user"]:
        raise Refusal("sudo_user_mismatch")
    if request["operation"] == "start":
        command = request.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise Refusal("invalid_command")
        encoded = command.encode("utf-8")
        if len(encoded) > MAX_COMMAND or digest(encoded) != command_hash:
            raise Refusal("command_hash_mismatch")
    if request["operation"] == "tail":
        if request.get("stream") not in ("stdout", "stderr"):
            raise Refusal("invalid_stream")
        if type(request.get("max_bytes")) is not int or not 1 <= request["max_bytes"] <= MAX_READ:
            raise Refusal("invalid_log_limit")


def witness(metadata, phase, **extra):
    value = {key: metadata[key] for key in (
        "schema_version", "job_id", "transaction_id", "command_sha256", "target", "boot_id", "unit"
    )}
    value.update(phase=phase, **extra)
    return {**value, "witness_sha256": digest(canonical(value))}


def record_matches(metadata, request):
    for key in ("schema_version", "job_id", "transaction_id", "command_sha256", "target"):
        if metadata.get(key) != request.get(key):
            raise Refusal("job_binding_mismatch")
    if metadata.get("unit") != "ssh-relay-sudo-" + uuid.UUID(request["job_id"]).hex + ".service":
        raise Refusal("unit_binding_mismatch")


def observed(path, metadata):
    complete = path / "completion.json"
    if complete.exists() or complete.is_symlink():
        result = read_json(complete)
        saved_hash = result.pop("witness_sha256", None)
        if saved_hash != digest(canonical(result)):
            raise Refusal("corrupt_completion")
        for key in ("schema_version", "job_id", "transaction_id", "command_sha256", "target", "unit", "boot_id"):
            if result.get(key) != metadata.get(key):
                raise Refusal("completion_binding_mismatch")
        if (result.get("phase") != "completion" or type(result.get("exit_code")) is not int or
                not 0 <= result["exit_code"] <= 255 or not re.fullmatch("[0-9a-f]{32}", result.get("invocation_id", ""))):
            raise Refusal("corrupt_completion")
        return {"state": "succeeded" if result["exit_code"] == 0 else "failed",
                "exit_code": result["exit_code"], "completion_witness": {**result, "witness_sha256": saved_hash},
                "accounting_status": "recorded"}
    if metadata["boot_id"] != boot_id():
        return {"state": "unknown", "error_code": "remote_reboot_without_completion"}
    current = properties(metadata["unit"])
    if current.get("LoadState") == "not-found":
        return {"state": "unknown", "error_code": "unit_missing_without_completion"}
    started_path = path / "started.json"
    if not started_path.exists():
        return {"state": "unknown", "error_code": "start_not_confirmed"}
    started = read_json(started_path)
    expected = witness(metadata, "start", invocation_id=started.get("invocation_id"))
    if started != expected or not re.fullmatch("[0-9a-f]{32}", started.get("invocation_id", "")):
        raise Refusal("corrupt_start")
    if current.get("InvocationID") != started["invocation_id"]:
        raise Refusal("invocation_mismatch")
    if current.get("SubState") == "exited" and current.get("ExecMainCode") == "1":
        # Наблюдатель возвращает код команды даже при ошибке записи completion.
        code = int(current["ExecMainStatus"])
        if 0 <= code <= 255:
            return {"state": "succeeded" if code == 0 else "failed", "exit_code": code,
                    "start_witness": started, "accounting_status": "failed",
                    "completion_witness": None, "error_code": "completion_record_failed"}
    if current.get("SubState") in ("running", "start", "start-pre", "stop-sigterm", "stop"):
        return {"state": "running", "start_witness": started, "accounting_status": "pending"}
    return {"state": "unknown", "start_witness": started, "error_code": "supervisor_result_unknown"}


def start(request, source):
    try:
        preflight()
    except (OSError, subprocess.SubprocessError):
        raise Refusal("sudo_job_prerequisites_unavailable") from None
    secure_directory(ROOT, create=True)
    lock = open_file(ROOT / ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = [item for item in ROOT.iterdir() if item.name != ".lock"]
        if len(entries) >= MAX_RECORDS:
            raise Refusal("job_storage_full")
        for entry in entries:
            secure_directory(entry)
            old = read_json(entry / "metadata.json")
            if old.get("transaction_id") == request["transaction_id"]:
                raise Refusal("transaction_already_reserved")
        path = ROOT / request["job_id"]
        if path.exists() or path.is_symlink():
            raise Refusal("job_already_reserved")
        unit = "ssh-relay-sudo-" + uuid.UUID(request["job_id"]).hex + ".service"
        if properties(unit).get("LoadState") != "not-found":
            raise Refusal("unit_already_exists")
        # После mkdir любые исключения означают unknown: резервирование никогда не переиспользуется.
        path.mkdir(mode=0o700)
        try:
            metadata = {key: request[key] for key in (
                "schema_version", "job_id", "transaction_id", "command_sha256", "target"
            )}
            metadata.update(boot_id=boot_id(), unit=unit, helper_sha256=digest(source), created_at=int(time.time()))
            write_json(path / "metadata.json", metadata)
            write_json(path / "command.json", {"command": request["command"]})
            atomic_write(path / "runner.py", source)
            subprocess.run(
                ["/usr/bin/systemd-run", "--no-ask-password", "--quiet", "--unit=" + unit,
                 "--property=Type=exec", "--property=RemainAfterExit=yes", "--property=UMask=0077",
                 "--property=KillMode=control-group", "--property=SendSIGKILL=no",
                 "--property=TimeoutStopSec=infinity", "--property=StandardOutput=null",
                 "--property=StandardError=null", "/usr/bin/python3", "-I", str(path / "runner.py"), str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=ENV, timeout=10, check=True,
            )
            deadline = time.monotonic() + 3
            while not (path / "started.json").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            return {"state": "unknown", "error_code": "start_not_confirmed"} if not (path / "started.json").exists() else observed(path, metadata)
        except Exception:
            return {"state": "unknown", "error_code": "launch_or_accounting_unknown"}
    finally:
        os.close(lock)


def control(request):
    try:
        secure_directory(ROOT)
    except FileNotFoundError:
        return {"state": "unknown", "error_code": "job_not_found"}
    path = ROOT / request["job_id"]
    try:
        secure_directory(path)
    except FileNotFoundError:
        return {"state": "unknown", "error_code": "job_not_found"}
    metadata = read_json(path / "metadata.json")
    record_matches(metadata, request)
    if digest(read_bytes(path / "runner.py")) != metadata.get("helper_sha256"):
        raise Refusal("helper_hash_mismatch")
    result = observed(path, metadata)
    if request["operation"] == "stop" and result["state"] == "running":
        # Проверка InvocationID выше выполняется в том же root-процессе перед сигналом.
        # Имя unit зарезервировано навсегда; обычный SSH-пользователь не может его заменить.
        subprocess.run(["/usr/bin/systemctl", "--no-ask-password", "kill", "--kill-whom=all",
                        "--signal=SIGTERM", metadata["unit"]],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=ENV, timeout=5, check=True)
        result["stop_requested"] = True
    elif request["operation"] == "stop" and result["state"] == "unknown":
        result["error_code"] = "stop_refused_unknown_identity"
    if request["operation"] == "tail":
        log_path = path / (request["stream"] + ".log")
        try:
            descriptor = open_file(log_path)
        except FileNotFoundError:
            data = b""
        else:
            try:
                size = os.fstat(descriptor).st_size
                if size > MAX_LOG:
                    raise Refusal("oversized_log")
                os.lseek(descriptor, max(0, size - request["max_bytes"]), os.SEEK_SET)
                data = os.read(descriptor, request["max_bytes"])
            finally:
                os.close(descriptor)
        result.update(log=data.decode("utf-8", errors="replace"), stream=request["stream"],
                      log_limit=MAX_LOG, log_policy="first_bytes_then_discard")
    return result


def cgroup_members():
    lines = Path("/proc/self/cgroup").read_text().splitlines()
    group = next(line[3:] for line in lines if line.startswith("0::"))
    members = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
    return {int(item) for item in members.read_text().split()} - {os.getpid()}


def run_worker(path):
    secure_directory(ROOT)
    secure_directory(path)
    metadata = read_json(path / "metadata.json")
    if path.name != metadata["job_id"] or digest(read_bytes(path / "runner.py")) != metadata["helper_sha256"]:
        raise Refusal("worker_binding_mismatch")
    invocation = os.environ.get("INVOCATION_ID", "")
    if not re.fullmatch("[0-9a-f]{32}", invocation) or boot_id() != metadata["boot_id"]:
        raise Refusal("worker_identity_mismatch")
    if properties(metadata["unit"]).get("InvocationID") != invocation:
        raise Refusal("worker_invocation_mismatch")
    command = read_json(path / "command.json")["command"]
    if digest(command.encode("utf-8")) != metadata["command_sha256"]:
        raise Refusal("worker_command_mismatch")
    # Отказ до durable-свидетельства означает, что полезная команда не выполнялась.
    write_json(path / "started.json", witness(metadata, "start", invocation_id=invocation))
    # systemctl kill посылает TERM всем членам группы. Наблюдатель остаётся жив,
    # чтобы собрать код и сохранить свидетельство; SIGKILL автоматически не применяется.
    signal.signal(signal.SIGTERM, lambda _number, _frame: None)
    process = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/", env=ENV)
    selector = selectors.DefaultSelector()
    logs = {}
    counts = {"stdout": 0, "stderr": 0}
    for name in counts:
        descriptor = open_file(path / (name + ".log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        logs[name] = descriptor
        stream = getattr(process, name)
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    try:
        while selector.get_map():
            for key, _events in selector.select(timeout=0.2):
                chunk = os.read(key.fd, 32768)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                remaining = max(0, MAX_LOG - counts[name])
                if remaining:
                    with os.fdopen(os.dup(logs[name]), "wb", closefd=True) as stream:
                        stream.write(chunk[:remaining])
                counts[name] += len(chunk)
        code = process.wait()
        # Даже закрывший stdout фоновый потомок удерживает задание в running.
        while cgroup_members():
            time.sleep(0.1)
    finally:
        selector.close()
        for descriptor in logs.values():
            os.close(descriptor)
    code = code if code >= 0 else min(255, 128 - code)
    try:
        write_json(path / "completion.json", witness(
            metadata, "completion", invocation_id=invocation, exit_code=code,
            completed_at=int(time.time()), output_bytes=counts,
            logs_truncated={key: value > MAX_LOG for key, value in counts.items()},
        ))
    except OSError:
        # systemd получает фактический код даже при отказе диска.
        pass
    return code


def main(source=None):
    if len(sys.argv) == 2:
        return run_worker(Path(sys.argv[1]))
    if os.geteuid() != 0:
        return 77
    print(READY, flush=True)
    request = None
    try:
        # Поток завершает daemon, sudo-пароль уже прочитан самим sudo.
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise Refusal("oversized_request")
        request = json.loads(raw)
        validate(request)
        result = start(request, source) if request["operation"] == "start" else control(request)
    except Refusal as exc:
        result = {"state": "not_started" if request and request.get("operation") == "start" else "unknown",
                  "error_code": str(exc)}
    except Exception:
        result = {"state": "unknown", "error_code": "remote_helper_failure"}
    # Содержимое команд и необработанные исключения в ответ не включаются.
    print(json.dumps({"schema_version": SCHEMA, **result}, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(__file__).read_bytes()))
