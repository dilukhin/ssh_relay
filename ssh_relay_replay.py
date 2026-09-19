"""Ограниченное локальное хранилище исходных stdout/stderr. SSH здесь не используется."""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ssh_relay_replay_platform import (
    ReplayError, alive, check_path, owner_chain, private_directory, process_identity, sanitize_chain,
)

STREAM_LIMIT = 4 * 1024 * 1024
REQUEST_LIMIT = 2 * STREAM_LIMIT
SESSION_LIMIT = 16 * 1024 * 1024
GLOBAL_LIMIT = 64 * 1024 * 1024
SESSION_COUNT = 8
GLOBAL_COUNT = 32
CLEAN_TTL = 300
SUSPECT_TTL = 1800
SCAN_LIMIT = 128
METADATA_LIMIT = 16384
FIXED_FILES = {"stdout.bin", "stderr.bin", "metadata.json", "metadata.tmp"}
STREAMS = ("stdout", "stderr")


def request_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value.lower():
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError, AttributeError):
        raise ReplayError("request_id должен быть каноническим UUIDv4.") from None


def utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError
    result = datetime.fromisoformat(value).timestamp()
    if not 0 < result < 253402300799:
        raise ValueError
    return result


def open_file(path: Path, *, create: bool = False, write: bool = False):
    check_path(path.parent, directory=True)
    if not create:
        before = check_path(path, directory=False)
    flags = (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        info = check_path(path, directory=False)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ReplayError("Объект replay изменился во время открытия.")
        if not create and (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise ReplayError("Объект replay заменён.")
        return os.fdopen(fd, "r+b" if write else "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def read_json(path: Path) -> dict:
    with open_file(path) as stream:
        data = stream.read(METADATA_LIMIT + 1)
    if len(data) > METADATA_LIMIT:
        raise ReplayError("Метаданные replay превышают лимит.")
    try:
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise ReplayError("Метаданные replay повреждены.") from None


def write_json(directory: Path, data: dict) -> None:
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > METADATA_LIMIT:
        raise ReplayError("Метаданные replay превышают лимит.")
    temporary = directory / "metadata.tmp"
    # Временный файл имеет фиксированное имя, писатель записи единственный.
    if temporary.exists():
        check_path(temporary, directory=False)
        temporary.unlink()
    with open_file(temporary, create=True, write=True) as output:
        output.write(encoded)
        os.fsync(output.fileno())
    target = directory / "metadata.json"
    if target.exists():
        check_path(target, directory=False)
    os.replace(temporary, target)


class Store:
    def __init__(self, state: Path):
        self.state = state.absolute()
        self.root = self.state / "replay" / "v1"
        self.requests = self.root / "requests"

    def prepare(self, *, create: bool = True) -> None:
        if os.name == "nt":
            allowed = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")).absolute()
            if not self.state.is_relative_to(allowed):
                raise ReplayError("Replay должен находиться под LOCALAPPDATA.")
        if create:
            # Родитель state уже существует у запущенного daemon; CLI чтения ничего не создаёт.
            for path in (self.state, self.state / "replay", self.root, self.requests):
                if path == self.state:
                    check_path(path, directory=True, private=False)
                else:
                    private_directory(path)
        else:
            for path in (self.state / "replay", self.root, self.requests):
                check_path(path, directory=True)

    @contextmanager
    def locked(self):
        path = self.root / "gc.lock"
        identity = process_identity(os.getpid())
        data = json.dumps({"owner": identity, "created": utc(time.time())}).encode()
        for attempt in range(2):
            try:
                stream = open_file(path, create=True, write=True)
                break
            except FileExistsError:
                info = check_path(path, directory=False)
                record = read_json(path)
                if attempt or alive(record.get("owner", {})) is not False:
                    raise ReplayError("Хранилище replay занято.")
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise ReplayError("Блокировка replay изменилась.")
                path.unlink()
        inode = os.fstat(stream.fileno())
        try:
            stream.write(data)
            os.fsync(stream.fileno())
            inode = os.fstat(stream.fileno())
            yield
        finally:
            stream.close()
            try:
                info = check_path(path, directory=False)
                if (info.st_dev, info.st_ino) == (inode.st_dev, inode.st_ino):
                    path.unlink()
            except (OSError, ReplayError):
                pass

    def metadata(self, directory: Path) -> dict:
        data = read_json(directory / "metadata.json")
        try:
            if data.get("request_id") != directory.name or data.get("schema_version") != 1:
                raise ValueError
            if data.get("state") not in {"active", "completed", "unknown", "abandoned"}:
                raise ValueError
            if not isinstance(data.get("session"), str) or len(data["session"]) > 64:
                raise ValueError
            if data.get("retention_class") not in {"clean", "suspect"}:
                raise ValueError
            timestamp(data["created_at_utc"])
            if data["state"] != "active":
                timestamp(data["finished_at_utc"])
            if data["state"] in {"completed", "unknown"}:
                for name in STREAMS:
                    stream = data[name]
                    if not isinstance(stream, dict):
                        raise ValueError
                    for key in ("stored_bytes", "total_bytes", "dropped_prefix_bytes"):
                        if type(stream.get(key)) is not int or stream[key] < 0:
                            raise ValueError
                    if stream["stored_bytes"] > STREAM_LIMIT or not isinstance(stream.get("sha256_stored"), str):
                        raise ValueError
            if not isinstance(data.get("writer_identity"), dict):
                raise ValueError
            if data.get("command_exit_code") is not None and type(data["command_exit_code"]) is not int:
                raise ValueError
            return data
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ReplayError("Метаданные replay повреждены.") from None

    def records(self) -> list[tuple[Path, dict | None, int]]:
        records = []
        with os.scandir(self.requests) as entries:
            for index, entry in enumerate(entries):
                if index >= SCAN_LIMIT:
                    raise ReplayError("Превышен предел безопасного сканирования replay.")
                request_id(entry.name)
                directory = self.requests / entry.name
                check_path(directory, directory=True)
                names = set()
                with os.scandir(directory) as children:
                    for child in children:
                        names.add(child.name)
                        if child.name not in FIXED_FILES:
                            raise ReplayError("Неизвестный объект в каталоге replay.")
                        check_path(directory / child.name, directory=False)
                size = sum((directory / f"{s}.bin").stat().st_size for s in STREAMS if f"{s}.bin" in names)
                try:
                    metadata = self.metadata(directory)
                except (ReplayError, OSError):
                    metadata = None
                if metadata and metadata["state"] == "active" and alive(metadata.get("writer_identity", {})) is False:
                    metadata.update(state="abandoned", retention_class="suspect", finished_at_utc=utc(time.time()))
                    write_json(directory, metadata)
                records.append((directory, metadata, size))
        return records

    def remove(self, directory: Path) -> bool:
        try:
            check_path(directory, directory=True)
            names = list(directory.iterdir())
            if any(path.name not in FIXED_FILES for path in names):
                return False
            for path in names:
                check_path(path, directory=False)
            for path in names:
                path.unlink()
            directory.rmdir()
            return True
        except (OSError, ReplayError):
            return False

    @staticmethod
    def expired(data: dict, now: float) -> bool:
        if data["state"] == "active":
            return False
        ttl = CLEAN_TTL if data["retention_class"] == "clean" else SUSPECT_TTL
        return now - timestamp(data["finished_at_utc"]) >= ttl

    def collect(self, records: list, *, session: str | None = None, reserve: bool = False) -> list:
        now = time.time()
        for record in list(records):
            path, data, _ = record
            if data and self.expired(data, now) and self.remove(path):
                records.remove(record)
        if not reserve:
            return records

        def fits():
            global_bytes = session_bytes = global_count = session_count = 0
            for _, data, size in records:
                # Повреждённая запись консервативно относится к каждой сессии.
                weight = max(REQUEST_LIMIT, size) if not data or data["state"] == "active" else size
                global_bytes += weight
                in_session = not data or data["session"] == session
                if in_session:
                    session_bytes += weight
                global_count += 1
                session_count += int(in_session)
            return (global_bytes + REQUEST_LIMIT <= GLOBAL_LIMIT and session_bytes + REQUEST_LIMIT <= SESSION_LIMIT
                    and global_count < GLOBAL_COUNT and session_count < SESSION_COUNT)

        def rank(record):
            _, data, _ = record
            chain = sanitize_chain(data.get("owner_chain"))
            dead = bool(chain) and all(alive(i) is False for i in chain)
            return (data["retention_class"] != "clean", not dead, timestamp(data["finished_at_utc"]))

        candidates = sorted((r for r in records if r[1] and r[1]["state"] != "active"), key=rank)
        for record in candidates:
            if fits():
                break
            if self.remove(record[0]):
                records.remove(record)
        if not fits():
            raise ReplayError("Нет свободного бюджета replay.")
        return records

    def gc(self) -> None:
        try:
            self.prepare(create=False)
            with self.locked():
                self.collect(self.records())
        except (OSError, ReplayError, ValueError, TypeError):
            pass

    def begin(self, *, rid: str, session: str, action: str, command: str, risky: bool, owners: list) -> Writer:
        rid = request_id(rid)
        self.prepare()
        with self.locked():
            records = self.records()
            if any(path.name == rid for path, _, _ in records):
                raise ReplayError("request_id уже присутствует в replay; запись не заменяется.")
            self.collect(records, session=session, reserve=True)
            directory = self.requests / rid
            private_directory(directory)
            data = dict(schema_version=1, request_id=rid, session=session, source_action=action,
                        sudo=action == "sudo-exec", risky=risky,
                        command_sha256=hashlib.sha256(command.encode("utf-8")).hexdigest(),
                        created_at_utc=utc(time.time()), finished_at_utc=None, state="active",
                        operation_status="unknown", command_status="unknown", command_exit_code=None,
                        receipt_status="not_attempted" if risky else "not_requested",
                        initial_encoding="utf-8", initial_decode_status="unknown", retention_class="suspect",
                        owner_chain=sanitize_chain(owners), writer_identity=process_identity(os.getpid()))
            try:
                for stream in STREAMS:
                    with open_file(directory / f"{stream}.bin", create=True, write=True):
                        pass
                write_json(directory, data)
                return Writer(self, directory, data)
            except (OSError, ReplayError):
                self.remove(directory)
                raise

    def replay(self, *, rid: str | None, session: str, encoding: str, owners: list) -> dict:
        try:
            codec = codecs.lookup(encoding)
            if not getattr(codec, "_is_text_encoding", False):
                raise LookupError
            # bytes->text codec обязателен, base64/hex и прочие transforms не подходят.
            b"".decode(codec.name, errors="strict")
        except (LookupError, TypeError):
            raise ReplayError("Неизвестная текстовая кодировка replay.") from None
        self.prepare(create=False)
        with self.locked():
            records = self.collect(self.records())
            if rid is None:
                candidates = [r for r in records if r[1] and r[1]["session"] == session and r[1]["state"] != "active"]
                current = {(i["pid"], i["platform"], i["start"]) for i in sanitize_chain(owners) if i["start"]}
                related = [r for r in candidates if current.intersection(
                    (i["pid"], i["platform"], i["start"]) for i in sanitize_chain(r[1].get("owner_chain")) if i["start"])]
                candidates = related or candidates
                if len(candidates) != 1:
                    raise ReplayError("ambiguous_last: требуется явный --request-id." if candidates else "Replay не найден.")
                selected = candidates[0]
            else:
                rid = request_id(rid)
                selected = next((r for r in records if r[0].name == rid), None)
                if selected is None:
                    raise ReplayError("Replay не найден или срок хранения истёк.")
            directory, data, _ = selected
            if data and (data["state"] == "active" or data["session"] != session):
                raise ReplayError("Replay ещё активен или принадлежит другой сессии.")
            complete = bool(data and data["state"] == "completed")
            result = dict(schema_version=1, tool="ssh_relay", action="replay", request_id=directory.name,
                          session=data["session"] if data else None, source_action=data.get("source_action") if data else None,
                          source_operation_status=data.get("operation_status", "unknown") if data else "unknown",
                          source_command_status=data.get("command_status", "unknown") if data else "unknown",
                          source_command_exit_code=data.get("command_exit_code") if data else None,
                          encoding=codec.name, error_code=None, error_message=None)
            for name in STREAMS:
                with open_file(directory / f"{name}.bin") as stream:
                    raw = stream.read(STREAM_LIMIT + 1)
                if len(raw) > STREAM_LIMIT:
                    raise ReplayError("Размер replay превышает лимит.")
                meta = data.get(name, {}) if data else {}
                if data and data["state"] in {"completed", "unknown"}:
                    if meta.get("stored_bytes") != len(raw) or meta.get("sha256_stored") != hashlib.sha256(raw).hexdigest():
                        raise ReplayError("Контрольная сумма replay не совпадает.")
                truncated = bool(meta.get("dropped_prefix_bytes", 0))
                complete &= not truncated
                try:
                    result[name] = raw.decode(codec.name, errors="strict")
                except UnicodeError:
                    raise ReplayError("Байты replay не декодируются в выбранной кодировке без потерь.") from None
                result[name + "_truncated"] = truncated
            if data and codec.name != "utf-8" and data["retention_class"] == "clean":
                data["retention_class"] = "suspect"
                write_json(directory, data)  # Исходный finished_at_utc неизменен.
            result["replay_complete"] = bool(complete)
            return result


class Writer:
    def __init__(self, store: Store, directory: Path, data: dict):
        self.store, self.directory, self.data = store, directory, data
        self.total = dict.fromkeys(STREAMS, 0)
        self.hashes = {name: hashlib.sha256() for name in STREAMS}
        self.failed = False
        self.closed = False

    def capture(self, name: str, chunk: bytes) -> None:
        if self.closed or self.failed or not chunk:
            return
        try:
            self.total[name] += len(chunk)
            self.hashes[name].update(chunk)
            with open_file(self.directory / f"{name}.bin", write=True) as output:
                size = output.seek(0, 2)
                if len(chunk) >= STREAM_LIMIT:
                    output.seek(0)
                    output.write(chunk[-STREAM_LIMIT:])
                    output.truncate(STREAM_LIMIT)
                else:
                    drop = max(0, size + len(chunk) - STREAM_LIMIT)
                    if drop:
                        offset = drop
                        while offset < size:
                            output.seek(offset)
                            block = output.read(min(65536, size - offset))
                            output.seek(offset - drop)
                            output.write(block)
                            offset += len(block)
                    output.seek(size - drop)
                    output.write(chunk)
                    output.truncate()
        except (OSError, ReplayError):
            self.failed = True

    def finish(self, *, operation_status: str, command_status: str, command_exit_code: int | None,
               receipt_status: str) -> dict:
        self.closed = True
        if self.failed:
            # Частичная запись после ошибки диска не держит живую reservation бессрочно.
            # Хеши полного потока больше не доказывают полноту фиксированных файлов.
            self.data.update(state="abandoned", retention_class="suspect", finished_at_utc=utc(time.time()))
            try:
                write_json(self.directory, self.data)
            except (OSError, ReplayError):
                pass
            return {"replay_status": "unavailable", "replay_truncated": True}
        try:
            clean = operation_status != "unknown"
            truncated = False
            for name in STREAMS:
                hasher = hashlib.sha256()
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                size = 0
                with open_file(self.directory / f"{name}.bin", write=True) as output:
                    os.fsync(output.fileno())
                    while chunk := output.read(65536):
                        size += len(chunk)
                        hasher.update(chunk)
                        if decoder is not None:
                            try:
                                decoder.decode(chunk)
                            except UnicodeError:
                                clean, decoder = False, None
                    if decoder is not None:
                        try:
                            decoder.decode(b"", final=True)
                        except UnicodeError:
                            clean = False
                dropped = max(0, self.total[name] - size)
                truncated |= dropped > 0
                self.data[name] = dict(total_bytes=self.total[name], stored_bytes=size,
                                       dropped_prefix_bytes=dropped, sha256_full=self.hashes[name].hexdigest(),
                                       sha256_stored=hasher.hexdigest())
            clean &= not truncated
            self.data.update(state="unknown" if operation_status == "unknown" else "completed",
                             operation_status=operation_status, command_status=command_status,
                             command_exit_code=command_exit_code, receipt_status=receipt_status,
                             finished_at_utc=utc(time.time()), initial_decode_status="clean" if clean else "suspect",
                             retention_class="clean" if clean else "suspect")
            # Атомарная финализация только уменьшает reservation; ждать GC-lock не требуется.
            write_json(self.directory, self.data)
            self.store.gc()
            return {"replay_status": "available" if clean or (not truncated and operation_status != "unknown") else "partial",
                    "replay_truncated": truncated}
        except (OSError, ReplayError, ValueError):
            self.failed = True
            return {"replay_status": "unavailable", "replay_truncated": True}
