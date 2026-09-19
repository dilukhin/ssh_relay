"""Приватные каталоги и идентичность процессов для локального replay."""
from __future__ import annotations

import ctypes
import os
import re
import stat
from pathlib import Path


class ReplayError(Exception):
    """Безопасная диагностическая ошибка без содержимого команды и вывода."""


def _windows():
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    declarations = [
        (k, "GetCurrentProcess", [], w.HANDLE),
        (k, "CloseHandle", [w.HANDLE], w.BOOL),
        (k, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p),
        (k, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        (k, "GetProcessTimes", [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        (k, "CreateDirectoryW", [w.LPCWSTR, ctypes.c_void_p], w.BOOL),
        (a, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (a, "GetTokenInformation", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        (a, "ConvertSidToStringSidW", [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)], w.BOOL),
        (a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p], w.BOOL),
        (a, "GetFileSecurityW", [w.LPCWSTR, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        (a, "ConvertStringSidToSidW", [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)], w.BOOL),
        (a, "GetSecurityDescriptorOwner", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL)], w.BOOL),
        (a, "GetSecurityDescriptorDacl", [ctypes.c_void_p, ctypes.POINTER(w.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL)], w.BOOL),
        (a, "GetAce", [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)], w.BOOL),
        (a, "EqualSid", [ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
    ]
    for lib, name, args, result in declarations:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
    return k, a, w


def _current_sid() -> str:
    k, a, w = _windows()
    token = w.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ReplayError("Не удалось проверить владельца replay.")
    try:
        length = w.DWORD()
        a.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buf = ctypes.create_string_buffer(length.value)
        if not a.GetTokenInformation(token, 1, buf, len(buf), ctypes.byref(length)):
            raise ReplayError("Не удалось проверить владельца replay.")
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        value = w.LPWSTR()
        if not a.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise ReplayError("Не удалось проверить владельца replay.")
        try:
            return value.value
        finally:
            k.LocalFree(value)
    finally:
        k.CloseHandle(token)


def windows_private(path: Path, *, create: bool = False) -> None:
    """Создаёт каталог сразу с закрытой DACL; существующие права не исправляет."""
    k, a, w = _windows()
    sid = _current_sid()
    if create:
        class Attributes(ctypes.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]
        sd = ctypes.c_void_p()
        sddl = f"D:P(A;OICI;FA;;;{sid})"
        if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            raise ReplayError("Не удалось создать приватную ACL replay.")
        try:
            attrs = Attributes(ctypes.sizeof(Attributes), sd, False)
            if not k.CreateDirectoryW(str(path), ctypes.byref(attrs)) and ctypes.get_last_error() != 183:
                raise ReplayError("Не удалось создать приватный каталог replay.")
        finally:
            k.LocalFree(sd)
    length = w.DWORD()
    a.GetFileSecurityW(str(path), 5, None, 0, ctypes.byref(length))
    buf = ctypes.create_string_buffer(length.value)
    if not a.GetFileSecurityW(str(path), 5, buf, len(buf), ctypes.byref(length)):
        raise ReplayError("Не удалось проверить ACL replay.")
    # Сравниваем бинарные SID: SDDL может сокращать SID владельца до LA и других псевдонимов.
    trusted = []
    try:
        for name in (sid, "S-1-5-18", "S-1-5-32-544"):
            pointer = ctypes.c_void_p()
            if not a.ConvertStringSidToSidW(name, ctypes.byref(pointer)):
                raise ReplayError("Не удалось проверить SID replay.")
            trusted.append(pointer)
        owner, acl = ctypes.c_void_p(), ctypes.c_void_p()
        defaulted, present = w.BOOL(), w.BOOL()
        if not a.GetSecurityDescriptorOwner(buf, ctypes.byref(owner), ctypes.byref(defaulted)):
            raise ReplayError("Не удалось проверить владельца replay.")
        if not owner or not any(a.EqualSid(owner, value) for value in trusted):
            raise ReplayError("Не подтверждён владелец каталога replay.")
        if not a.GetSecurityDescriptorDacl(buf, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
            raise ReplayError("Не удалось проверить DACL replay.")
        if not present.value or not acl.value:
            raise ReplayError("Replay требует приватную DACL.")
        count = ctypes.c_ushort.from_address(acl.value + 4).value
        owner_access = False
        for index in range(count):
            ace = ctypes.c_void_p()
            if not a.GetAce(acl, index, ctypes.byref(ace)):
                raise ReplayError("Не удалось прочитать ACE replay.")
            header = ctypes.string_at(ace, 4)
            if header[0] != 0 or int.from_bytes(header[2:4], "little") < 16:
                raise ReplayError("Неподдерживаемая запись ACL replay.")
            trustee = ctypes.c_void_p(ace.value + 8)
            if not any(a.EqualSid(trustee, value) for value in trusted):
                raise ReplayError("Replay доступен посторонним пользователям.")
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            if a.EqualSid(trustee, trusted[0]) and not header[1] & 8:
                owner_access |= mask & 0x1f01ff == 0x1f01ff or bool(mask & 0x10000000)
        if not owner_access:
            raise ReplayError("Не подтверждены права владельца replay.")
    finally:
        for pointer in trusted:
            k.LocalFree(pointer)


def check_path(path: Path, *, directory: bool, private: bool = True) -> os.stat_result:
    """Проверяет компоненты пути без разрешения ссылок; владельцу той же учётной записи доверяем."""
    path = path.absolute()
    for component in [*reversed(path.parents), path]:
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReplayError("Ссылки и reparse points в пути replay запрещены.")
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ReplayError("Недопустимый тип объекта replay.")
    if not directory and info.st_nlink != 1:
        raise ReplayError("Жёсткие ссылки на файлы replay запрещены.")
    if private:
        if os.name == "nt":
            windows_private(path)
        elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ReplayError("Replay требует приватные права владельца.")
    return info


def private_directory(path: Path) -> None:
    if not path.exists():
        check_path(path.parent, directory=True, private=False)
        if os.name == "nt":
            windows_private(path, create=True)
        else:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
    check_path(path, directory=True)


def process_identity(pid: int) -> dict:
    weak = {"pid": pid, "ppid": None, "platform": "weak", "start": None}
    if os.name == "nt":
        k, _, w = _windows()
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            return weak
        try:
            times = [w.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return weak
            start = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return {"pid": pid, "ppid": _windows_parent(pid), "platform": "windows", "start": str(start)}
        finally:
            k.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"pid": pid, "ppid": int(fields[1]), "platform": "linux", "start": boot + ":" + fields[19]}
    except (OSError, ValueError, IndexError):
        return weak


def _windows_parent(pid: int) -> int | None:
    k, _, w = _windows()
    class Entry(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                    ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                    ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                    ("exe", w.WCHAR * 260)]
    k.CreateToolhelp32Snapshot.argtypes, k.CreateToolhelp32Snapshot.restype = [w.DWORD, w.DWORD], w.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(k, name).argtypes, getattr(k, name).restype = [w.HANDLE, ctypes.POINTER(Entry)], w.BOOL
    handle = k.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        ok = k.Process32FirstW(handle, ctypes.byref(entry))
        for _ in range(65536):
            if not ok:
                break
            if entry.pid == pid:
                return int(entry.parent)
            ok = k.Process32NextW(handle, ctypes.byref(entry))
    finally:
        k.CloseHandle(handle)
    return None


def alive(identity: dict) -> bool | None:
    """False только при доказанной смерти или повторном использовании PID."""
    if not isinstance(identity, dict):
        return None
    if identity.get("platform") not in {"linux", "windows"} or not identity.get("start"):
        return None
    pid = identity.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return None
    current = process_identity(pid)
    if current["platform"] == identity["platform"] and current["start"]:
        return current["start"] == identity["start"]
    if os.name == "nt":
        k, _, _ = _windows()
        handle = k.OpenProcess(0x1000, False, pid)
        if handle:
            k.CloseHandle(handle)
            return None
        return False if ctypes.get_last_error() == 87 else None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return None


def owner_chain() -> list[dict]:
    result = []
    pid = os.getpid()
    for _ in range(16):
        if not pid or pid <= 1 or pid in {item["pid"] for item in result}:
            break
        identity = process_identity(pid)
        result.append(identity)
        pid = identity["ppid"]
    return result


def sanitize_chain(value: object) -> list[dict]:
    result = []
    if not isinstance(value, list) or len(value) > 16:
        return result
    for item in value:
        if not isinstance(item, dict):
            return []
        pid, start, platform = item.get("pid"), item.get("start"), item.get("platform")
        if type(pid) is not int or not 1 < pid < 2**32 or platform not in {"linux", "windows", "weak"}:
            return []
        if start is not None and (not isinstance(start, str) or not re.fullmatch(r"[0-9a-fA-F:-]{1,100}", start)):
            return []
        ppid = item.get("ppid")
        result.append({"pid": pid, "ppid": ppid if type(ppid) is int else None, "platform": platform, "start": start})
    return result
