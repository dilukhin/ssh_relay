#!/usr/bin/env python3
"""Длительные SFTP-передачи поверх существующих action relay.

Новый CLI передаёт/получает файл небольшими подтверждаемыми чанками. Старые
клиенты продолжают использовать прежние функции core: install() сохраняет их
как fallback для запросов без служебного envelope.
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import socket
import stat
import time
from pathlib import Path
from typing import Any, Callable

TRANSFER_ENVELOPE_PREFIX = "ssh-relay-transfer-v1:"
TRANSFER_CHUNK_SIZE = 1024 * 1024
DEFAULT_IDLE_TIMEOUT = 60.0
PROGRESS_INTERVAL = 1.0

_core: Any | None = None
_legacy_download: Callable[..., dict[str, Any]] | None = None
_legacy_upload: Callable[..., dict[str, Any]] | None = None


def install(core_module: Any) -> None:
    """Подменяет только transfer-функции core, сохраняя legacy fallback."""
    global _core, _legacy_download, _legacy_upload
    if _core is core_module and _legacy_download is not None and _legacy_upload is not None:
        return
    _core = core_module
    _legacy_download = core_module.download_remote_file
    _legacy_upload = core_module.upload_file_content
    core_module.download_remote_file = download_remote_file
    core_module.upload_file_content = upload_file_content


def _require_core() -> Any:
    if _core is None:
        raise RuntimeError("ssh_relay_transfers.install() не был вызван")
    return _core


def encode_transfer_path(local_path: str, **metadata: Any) -> str:
    """Упаковывает служебные параметры в поле local_path без изменения wire-протокола."""
    payload = {"local_path": local_path, **metadata}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii")
    return TRANSFER_ENVELOPE_PREFIX + token


def decode_transfer_path(value: str) -> dict[str, Any] | None:
    if not value.startswith(TRANSFER_ENVELOPE_PREFIX):
        return None
    token = value[len(TRANSFER_ENVELOPE_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise _require_core().RelayError("Повреждены служебные параметры передачи файла.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("local_path"), str):
        raise _require_core().RelayError("Некорректные служебные параметры передачи файла.")
    return data


def local_partial_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.ssh-relay.part")


def remote_partial_path(remote_target: str) -> str:
    stripped = remote_target.rstrip("/")
    parent = posixpath.dirname(stripped) or "."
    name = posixpath.basename(stripped)
    partial_name = f".{name}.ssh-relay.part"
    return partial_name if parent == "." else posixpath.join(parent, partial_name)


def percent(transferred_bytes: int, total_bytes: int) -> float:
    if total_bytes <= 0:
        return 100.0
    return max(0.0, min(100.0, transferred_bytes * 100.0 / total_bytes))


def progress_snapshot(transferred_bytes: int, total_bytes: int, elapsed: float) -> dict[str, float | int]:
    safe_elapsed = max(0.0, elapsed)
    speed = transferred_bytes / safe_elapsed if safe_elapsed > 0 else 0.0
    return {
        "transferred_bytes": transferred_bytes,
        "total_bytes": total_bytes,
        "percent": percent(transferred_bytes, total_bytes),
        "elapsed": safe_elapsed,
        "speed": speed,
    }


def format_progress(snapshot: dict[str, float | int]) -> str:
    return (
        "Прогресс: "
        f"transferred_bytes={int(snapshot['transferred_bytes'])} "
        f"total_bytes={int(snapshot['total_bytes'])} "
        f"percent={float(snapshot['percent']):.1f} "
        f"elapsed={float(snapshot['elapsed']):.1f}s "
        f"speed={float(snapshot['speed']):.1f}B/s"
    )


class ProgressPrinter:
    """Редко печатает прогресс; финальное состояние можно форсировать."""

    def __init__(self, *, started: float | None = None, interval: float = PROGRESS_INTERVAL) -> None:
        self.started = time.monotonic() if started is None else started
        self.interval = interval
        self._last_print = self.started - interval
        self._last_transferred = -1

    def maybe_print(self, transferred_bytes: int, total_bytes: int, *, force: bool = False) -> bool:
        now = time.monotonic()
        if transferred_bytes == self._last_transferred and not force:
            return False
        if not force and now - self._last_print < self.interval:
            return False
        print(format_progress(progress_snapshot(transferred_bytes, total_bytes, now - self.started)), flush=True)
        self._last_print = now
        self._last_transferred = transferred_bytes
        return True


def _positive_float(meta: dict[str, Any], key: str, fallback: float) -> float:
    try:
        value = float(meta.get(key, fallback))
    except (TypeError, ValueError):
        value = fallback
    if value <= 0:
        value = fallback
    return value


def _effective_timeout(meta: dict[str, Any], fallback: float) -> tuple[float, str]:
    idle = _positive_float(meta, "idle_timeout", DEFAULT_IDLE_TIMEOUT)
    remaining = _positive_float(meta, "remaining_timeout", fallback)
    hard = max(0.001, float(fallback))
    effective = max(0.001, min(idle, remaining, hard))
    reason = "общий предел времени передачи" if remaining <= idle and remaining <= hard else "отсутствие прогресса"
    return effective, reason


def _set_sftp_timeout(sftp: Any, timeout_seconds: float) -> None:
    try:
        channel = sftp.get_channel()
        channel.settimeout(timeout_seconds)
    except (AttributeError, OSError):
        pass


def _timed_call(meta: dict[str, Any], fallback_timeout: float, operation: Callable[[], Any]) -> Any:
    core = _require_core()
    timeout_seconds, reason = _effective_timeout(meta, fallback_timeout)
    started = time.monotonic()
    try:
        result = operation()
    except (socket.timeout, TimeoutError) as exc:
        if reason == "отсутствие прогресса":
            raise core.RelayError(f"Передача остановлена: нет прогресса в течение {timeout_seconds:g} с.") from exc
        raise core.RelayError(f"Передача остановлена: превышен общий предел времени ({timeout_seconds:g} с осталось).") from exc
    elapsed = time.monotonic() - started
    if elapsed > timeout_seconds:
        if reason == "отсутствие прогресса":
            raise core.RelayError(f"Передача остановлена: нет прогресса в течение {timeout_seconds:g} с.")
        raise core.RelayError(f"Передача остановлена: превышен общий предел времени ({timeout_seconds:g} с осталось).")
    return result


def _remote_stat_regular(sftp: Any, remote_path: str) -> Any:
    core = _require_core()
    try:
        attrs = sftp.stat(remote_path)
    except (socket.timeout, TimeoutError):
        raise
    except OSError as exc:
        raise core.RelayError(f"Удалённый файл не найден или недоступен: {remote_path}") from exc
    mode = getattr(attrs, "st_mode", 0)
    if mode and stat.S_ISDIR(mode):
        raise core.RelayError("Удалённый путь указывает на каталог, а не на файл.")
    if mode and not stat.S_ISREG(mode):
        raise core.RelayError("Удалённый путь не является обычным файлом.")
    return attrs


def _optional_stat(
    sftp: Any,
    path: str,
    meta: dict[str, Any],
    fallback_timeout: float,
) -> Any | None:
    try:
        return _timed_call(meta, fallback_timeout, lambda: sftp.stat(path))
    except OSError:
        return None


def _remote_target_state(
    sftp: Any,
    remote_target: str,
    meta: dict[str, Any],
    fallback_timeout: float,
) -> tuple[bool, Any | None]:
    core = _require_core()
    attrs = _optional_stat(sftp, remote_target, meta, fallback_timeout)
    if attrs is None:
        return False, None
    mode = getattr(attrs, "st_mode", 0)
    if mode and stat.S_ISDIR(mode):
        raise core.RelayError("Удалённый путь указывает на каталог, а не на файл.")
    if mode and not stat.S_ISREG(mode):
        raise core.RelayError("Удалённый путь существует, но не является обычным файлом.")
    return True, attrs


def _optional_stat_size(
    sftp: Any,
    path: str,
    meta: dict[str, Any],
    fallback_timeout: float,
) -> int | None:
    attrs = _optional_stat(sftp, path, meta, fallback_timeout)
    if attrs is None:
        return None
    return int(getattr(attrs, "st_size", 0) or 0)


def _ensure_remote_directory(
    sftp: Any,
    remote_directory: str,
    meta: dict[str, Any],
    fallback_timeout: float,
) -> None:
    """Создаёт каталоги, не принимая SFTP timeout за отсутствие пути."""
    core = _require_core()
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
            attrs = _timed_call(meta, fallback_timeout, lambda path=current: sftp.stat(path))
        except core.RelayError:
            raise
        except OSError:
            try:
                _timed_call(meta, fallback_timeout, lambda path=current: sftp.mkdir(path))
            except core.RelayError:
                raise
            except OSError as exc:
                # Возможна гонка с другим создателем каталога: перепроверяем путь.
                try:
                    attrs = _timed_call(meta, fallback_timeout, lambda path=current: sftp.stat(path))
                except core.RelayError:
                    raise
                except OSError:
                    raise core.RelayError(f"Не удалось создать удалённый каталог: {current}") from exc
                mode = getattr(attrs, "st_mode", 0)
                if mode and stat.S_ISDIR(mode):
                    continue
                raise core.RelayError(f"Удалённый путь {current} существует, но не является каталогом.") from exc
            continue

        mode = getattr(attrs, "st_mode", 0)
        if mode and not stat.S_ISDIR(mode):
            raise core.RelayError(f"Удалённый путь {current} существует, но не является каталогом.")


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
    """Возвращает probe/chunk для нового CLI или делегирует legacy download."""
    meta = decode_transfer_path(local_path)
    if meta is None:
        assert _legacy_download is not None
        return _legacy_download(
            client,
            remote_path,
            local_path,
            overwrite=overwrite,
            create_dirs=create_dirs,
            max_size=max_size,
            timeout_seconds=timeout_seconds,
        )

    core = _require_core()
    if meta.get("kind") != "download":
        raise core.RelayError("Некорректный тип служебного transfer-запроса.")
    phase = meta.get("phase")
    if phase not in {"probe", "chunk"}:
        raise core.RelayError("Некорректная фаза скачивания.")
    if not remote_path.strip():
        raise core.RelayError("Передан пустой путь удалённого файла.")

    remote_source = core.normalize_remote_sftp_path(remote_path)
    try:
        sftp = client.open_sftp()
    except Exception as exc:
        raise core.RelayError("Не удалось открыть SFTP-канал через активную SSH-сессию.") from exc

    try:
        effective_timeout, _ = _effective_timeout(meta, timeout_seconds)
        _set_sftp_timeout(sftp, effective_timeout)
        attrs = _timed_call(meta, timeout_seconds, lambda: _remote_stat_regular(sftp, remote_source))
        remote_size = int(getattr(attrs, "st_size", 0) or 0)
        remote_mtime = int(getattr(attrs, "st_mtime", 0) or 0)
        if remote_size > max_size:
            raise core.RelayError(
                f"Размер удалённого файла {core.format_bytes(remote_size)} превышает лимит "
                f"{core.format_bytes(max_size)}. Перезапустите daemon с большим --download-max-size, если это безопасно."
            )

        base_result = {
            "ok": True,
            "remote_path": remote_source,
            "local_path": meta["local_path"],
            "total_bytes": remote_size,
            "remote_mtime": remote_mtime,
        }
        if phase == "probe":
            return base_result

        expected_size = meta.get("expected_size")
        expected_mtime = meta.get("expected_mtime")
        if not isinstance(expected_size, int) or expected_size != remote_size:
            raise core.RelayError("Удалённый файл изменился во время скачивания: изменился размер.")
        if isinstance(expected_mtime, int) and expected_mtime != 0 and remote_mtime != expected_mtime:
            raise core.RelayError("Удалённый файл изменился во время скачивания: изменилось время модификации.")

        offset = meta.get("offset")
        if not isinstance(offset, int) or offset < 0 or offset > remote_size:
            raise core.RelayError("Некорректное смещение чанка скачивания.")
        if offset == remote_size:
            return {**base_result, "chunk_b64": "", "transferred_bytes": offset}

        read_size = min(TRANSFER_CHUNK_SIZE, remote_size - offset)
        remote_file = None
        try:
            remote_file = _timed_call(meta, timeout_seconds, lambda: sftp.open(remote_source, "rb"))
            remote_file.seek(offset)
            chunk = _timed_call(meta, timeout_seconds, lambda: remote_file.read(read_size))
        except core.RelayError:
            raise
        except OSError as exc:
            raise core.RelayError(f"Ошибка при скачивании файла: {exc}") from exc
        finally:
            if remote_file is not None:
                try:
                    remote_file.close()
                except (AttributeError, OSError):
                    pass
        if not chunk:
            raise core.RelayError("Удалённый файл перестал отдавать данные до ожидаемого конца.")
        return {
            **base_result,
            "chunk_b64": base64.b64encode(chunk).decode("ascii"),
            "transferred_bytes": offset + len(chunk),
        }
    finally:
        sftp.close()


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
    """Обрабатывает probe/begin/chunk/finish нового upload или legacy upload."""
    meta = decode_transfer_path(local_path)
    if meta is None:
        assert _legacy_upload is not None
        return _legacy_upload(
            client,
            local_path,
            content,
            remote_path,
            overwrite=overwrite,
            create_dirs=create_dirs,
            max_size=max_size,
            timeout_seconds=timeout_seconds,
        )

    core = _require_core()
    if meta.get("kind") != "upload":
        raise core.RelayError("Некорректный тип служебного transfer-запроса.")
    phase = meta.get("phase")
    if phase not in {"probe", "begin", "chunk", "finish"}:
        raise core.RelayError("Некорректная фаза загрузки.")
    total_size = meta.get("total_size")
    if not isinstance(total_size, int) or total_size < 0:
        raise core.RelayError("Некорректный размер загружаемого файла.")
    if total_size > max_size:
        raise core.RelayError(
            f"Размер локального файла {core.format_bytes(total_size)} превышает лимит "
            f"{core.format_bytes(max_size)}. Перезапустите daemon с большим --upload-max-size, если это безопасно."
        )
    if not remote_path.strip() or remote_path.endswith("/") or "\x00" in remote_path:
        raise core.RelayError("Удалённый путь должен указывать на допустимое имя файла.")

    remote_target = core.normalize_remote_sftp_path(remote_path).rstrip("/")
    remote_parent = core.remote_parent_directory(remote_target)
    partial = remote_partial_path(remote_target)

    try:
        sftp = client.open_sftp()
    except Exception as exc:
        raise core.RelayError("Не удалось открыть SFTP-канал через активную SSH-сессию.") from exc

    try:
        effective_timeout, _ = _effective_timeout(meta, timeout_seconds)
        _set_sftp_timeout(sftp, effective_timeout)

        parent_exists = True
        if phase == "probe":
            try:
                parent_attrs = _timed_call(meta, timeout_seconds, lambda: sftp.stat(remote_parent))
                parent_mode = getattr(parent_attrs, "st_mode", 0)
                if parent_mode and not stat.S_ISDIR(parent_mode):
                    raise core.RelayError("Удалённый родительский путь существует, но не является каталогом.")
            except core.RelayError:
                raise
            except OSError as exc:
                if create_dirs:
                    parent_exists = False
                else:
                    raise core.RelayError(
                        "Удалённый каталог назначения не существует. Укажите --create-dirs или создайте его вручную."
                    ) from exc
        elif create_dirs:
            _ensure_remote_directory(sftp, remote_parent, meta, timeout_seconds)
        else:
            try:
                parent_attrs = _timed_call(meta, timeout_seconds, lambda: sftp.stat(remote_parent))
                parent_mode = getattr(parent_attrs, "st_mode", 0)
                if parent_mode and not stat.S_ISDIR(parent_mode):
                    raise core.RelayError("Удалённый родительский путь существует, но не является каталогом.")
            except core.RelayError:
                raise
            except OSError as exc:
                raise core.RelayError(
                    "Удалённый каталог назначения не существует. Укажите --create-dirs или создайте его вручную."
                ) from exc

        target_exists, _ = _remote_target_state(sftp, remote_target, meta, timeout_seconds)
        partial_size = _optional_stat_size(sftp, partial, meta, timeout_seconds)

        if phase == "probe":
            return {
                "ok": True,
                "local_path": meta["local_path"],
                "remote_path": remote_target,
                "total_bytes": total_size,
                "target_exists": target_exists,
                "partial_path": partial,
                "partial_size": partial_size,
                "parent_exists": parent_exists,
            }

        if phase == "begin":
            if target_exists and not overwrite:
                raise core.RelayError("Удалённый файл уже существует. Укажите --overwrite для перезаписи.")
            discard_partial = bool(meta.get("discard_partial", False))
            if partial_size is not None:
                if not discard_partial:
                    raise core.RelayError(
                        f"Найден частичный удалённый файл {partial} размером {partial_size} байт. "
                        "Автоматическое resume не поддерживается; сначала проверьте состояние, затем используйте --discard-partial для явного перезапуска."
                    )
                _timed_call(meta, timeout_seconds, lambda: sftp.remove(partial))
            remote_file = None
            try:
                remote_file = _timed_call(meta, timeout_seconds, lambda: sftp.open(partial, "wb"))
                _timed_call(meta, timeout_seconds, remote_file.flush)
            except core.RelayError:
                raise
            except OSError as exc:
                raise core.RelayError(f"Не удалось создать временный удалённый файл: {partial}") from exc
            finally:
                if remote_file is not None:
                    try:
                        remote_file.close()
                    except (AttributeError, OSError):
                        pass
            return {
                "ok": True,
                "local_path": meta["local_path"],
                "remote_path": remote_target,
                "partial_path": partial,
                "bytes_uploaded": 0,
                "total_bytes": total_size,
            }

        if partial_size is None:
            raise core.RelayError(
                f"Частичный удалённый файл {partial} не найден. Не продолжайте передачу вслепую; начните её заново после проверки состояния."
            )

        if phase == "chunk":
            offset = meta.get("offset")
            if not isinstance(offset, int) or offset < 0:
                raise core.RelayError("Некорректное смещение чанка загрузки.")
            if partial_size != offset:
                raise core.RelayError(
                    f"Размер частичного удалённого файла изменился: ожидается {offset} байт, фактически {partial_size} байт. "
                    "Автоматическое продолжение запрещено."
                )
            if not content:
                raise core.RelayError("Получен пустой чанк загрузки.")
            if offset + len(content) > total_size:
                raise core.RelayError("Чанк загрузки выходит за объявленный размер файла.")
            remote_file = None
            try:
                remote_file = _timed_call(meta, timeout_seconds, lambda: sftp.open(partial, "r+b"))
                remote_file.seek(offset)
                _timed_call(meta, timeout_seconds, lambda: remote_file.write(content))
                _timed_call(meta, timeout_seconds, remote_file.flush)
            except core.RelayError:
                raise
            except OSError as exc:
                raise core.RelayError(f"Ошибка при загрузке чанка файла: {exc}") from exc
            finally:
                if remote_file is not None:
                    try:
                        remote_file.close()
                    except (AttributeError, OSError):
                        pass
            actual_size = _optional_stat_size(sftp, partial, meta, timeout_seconds)
            expected_after = offset + len(content)
            if actual_size != expected_after:
                raise core.RelayError(
                    f"После записи чанка размер частичного файла не подтверждён: ожидается {expected_after}, фактически {actual_size}."
                )
            return {
                "ok": True,
                "local_path": meta["local_path"],
                "remote_path": remote_target,
                "partial_path": partial,
                "bytes_uploaded": actual_size,
                "total_bytes": total_size,
            }

        # finish
        if content:
            raise core.RelayError("Финальный запрос загрузки не должен содержать данные.")
        if partial_size != total_size:
            raise core.RelayError(
                f"Финальный размер временного файла не совпадает: ожидается {total_size}, фактически {partial_size}."
            )
        target_exists, _ = _remote_target_state(sftp, remote_target, meta, timeout_seconds)
        if target_exists and not overwrite:
            raise core.RelayError("Удалённый файл появился во время загрузки. Финальная замена не выполнена.")
        try:
            if target_exists:
                try:
                    rename = sftp.posix_rename
                except AttributeError as exc:
                    raise core.RelayError(
                        "SFTP-сервер не поддерживает безопасную атомарную замену существующего файла. "
                        "Готовый файл не удалён; частичный файл сохранён."
                    ) from exc
                try:
                    _timed_call(meta, timeout_seconds, lambda: rename(partial, remote_target))
                except OSError as exc:
                    raise core.RelayError(
                        "Не удалось безопасно заменить существующий удалённый файл через posix-rename; "
                        "готовый файл не удалён, частичный файл сохранён."
                    ) from exc
            else:
                _timed_call(meta, timeout_seconds, lambda: sftp.rename(partial, remote_target))
        except core.RelayError:
            raise
        except OSError as exc:
            raise core.RelayError(f"Не удалось переименовать временный удалённый файл: {exc}") from exc

        final_attrs = _timed_call(meta, timeout_seconds, lambda: _remote_stat_regular(sftp, remote_target))
        final_size = int(getattr(final_attrs, "st_size", 0) or 0)
        if final_size != total_size:
            raise core.RelayError(
                f"После финального переименования размер удалённого файла неожиданно равен {final_size}, ожидалось {total_size}."
            )
        return {
            "ok": True,
            "local_path": meta["local_path"],
            "remote_path": remote_target,
            "bytes_uploaded": final_size,
            "total_bytes": total_size,
        }
    finally:
        sftp.close()
