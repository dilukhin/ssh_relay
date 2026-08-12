import base64
import io
import posixpath
import stat
import time
import types
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssh_relay_transfers as transfers


class RelayError(Exception):
    pass


class FakeAttrs:
    def __init__(self, size=0, mode=None, mtime=100):
        self.st_size = size
        self.st_mode = stat.S_IFREG | 0o600 if mode is None else mode
        self.st_mtime = mtime


class FakeChannel:
    def __init__(self):
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value


class FakeFile:
    def __init__(self, sftp, path, mode):
        self.sftp = sftp
        self.path = path
        self.mode = mode
        self.offset = 0
        if "w" in mode:
            self.sftp.files[path] = bytearray()
        if path not in self.sftp.files:
            raise OSError("not found")
        if "a" in mode:
            self.offset = len(self.sftp.files[path])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def seek(self, offset):
        self.offset = offset

    def read(self, size=-1):
        if self.sftp.read_delay:
            time.sleep(self.sftp.read_delay)
        data = self.sftp.files[self.path]
        if size < 0:
            size = len(data) - self.offset
        chunk = bytes(data[self.offset:self.offset + size])
        self.offset += len(chunk)
        return chunk

    def write(self, data):
        if self.sftp.write_delay:
            time.sleep(self.sftp.write_delay)
        buf = self.sftp.files[self.path]
        end = self.offset + len(data)
        if len(buf) < end:
            buf.extend(b"\x00" * (end - len(buf)))
        buf[self.offset:end] = data
        self.offset = end
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


class FakeSFTP:
    def __init__(self, files=None, mtimes=None, read_delay=0.0, write_delay=0.0, posix_rename=True):
        self.files = {path: bytearray(data) for path, data in (files or {}).items()}
        self.mtimes = dict(mtimes or {})
        self.directories = {".", "/", "/tmp", "tmp"}
        self.read_delay = read_delay
        self.write_delay = write_delay
        self.channel = FakeChannel()
        self.closed = False
        self.support_posix_rename = posix_rename

    def get_channel(self):
        return self.channel

    def stat(self, path):
        if path in self.directories:
            return FakeAttrs(0, stat.S_IFDIR | 0o700, 100)
        if path not in self.files:
            raise OSError("not found")
        return FakeAttrs(len(self.files[path]), mtime=self.mtimes.get(path, 100))

    def open(self, path, mode):
        return FakeFile(self, path, mode)

    def mkdir(self, path):
        self.directories.add(path)

    def remove(self, path):
        if path not in self.files:
            raise OSError("not found")
        del self.files[path]

    def rename(self, source, target):
        if source not in self.files:
            raise OSError("not found")
        if target in self.files:
            raise OSError("target exists")
        self.files[target] = self.files.pop(source)

    def posix_rename(self, source, target):
        if not self.support_posix_rename:
            raise OSError("unsupported")
        if source not in self.files:
            raise OSError("not found")
        self.files[target] = self.files.pop(source)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, sftp):
        self.sftp = sftp

    def open_sftp(self):
        return self.sftp


def remote_parent_directory(path):
    stripped = path.rstrip("/")
    return posixpath.dirname(stripped) or "."


def ensure_remote_directory(sftp, remote_directory):
    if remote_directory in {"", ".", "/"}:
        return
    current = "/" if remote_directory.startswith("/") else ""
    for part in remote_directory.strip("/").split("/"):
        current = posixpath.join(current, part) if current else part
        sftp.directories.add(current)


def format_bytes(size):
    return f"{size} байт"


def legacy_download(*args, **kwargs):
    return {"ok": True, "legacy": "download"}


def legacy_upload(*args, **kwargs):
    return {"ok": True, "legacy": "upload"}


FAKE_CORE = types.SimpleNamespace(
    RelayError=RelayError,
    download_remote_file=legacy_download,
    upload_file_content=legacy_upload,
    normalize_remote_sftp_path=lambda value: value.replace("\\", "/"),
    format_bytes=format_bytes,
    remote_parent_directory=remote_parent_directory,
    ensure_remote_directory=ensure_remote_directory,
)
transfers.install(FAKE_CORE)


def envelope(path, kind, phase, **kwargs):
    return transfers.encode_transfer_path(path, kind=kind, phase=phase, **kwargs)


class TransferTests(unittest.TestCase):
    def test_percent_and_speed(self):
        self.assertEqual(transfers.percent(0, 10), 0.0)
        self.assertEqual(transfers.percent(5, 10), 50.0)
        self.assertEqual(transfers.percent(10, 10), 100.0)
        self.assertEqual(transfers.percent(0, 0), 100.0)
        snapshot = transfers.progress_snapshot(200, 1000, 2.0)
        self.assertEqual(snapshot["percent"], 20.0)
        self.assertEqual(snapshot["speed"], 100.0)
        line = transfers.format_progress(snapshot)
        for field in ("transferred_bytes=200", "total_bytes=1000", "percent=20.0", "elapsed=2.0s", "speed=100.0B/s"):
            self.assertIn(field, line)

    def test_legacy_requests_are_delegated(self):
        client = FakeClient(FakeSFTP())
        self.assertEqual(
            transfers.download_remote_file(
                client, "/x", "C:/x", overwrite=False, create_dirs=False, max_size=10, timeout_seconds=5
            )["legacy"],
            "download",
        )
        self.assertEqual(
            transfers.upload_file_content(
                client, "C:/x", b"x", "/x", overwrite=False, create_dirs=False, max_size=10, timeout_seconds=5
            )["legacy"],
            "upload",
        )

    def test_download_progress_grows_and_reconstructs_file(self):
        data = b"a" * (transfers.TRANSFER_CHUNK_SIZE + 123)
        sftp = FakeSFTP({"/tmp/data.bin": data})
        client = FakeClient(sftp)
        probe = transfers.download_remote_file(
            client,
            "/tmp/data.bin",
            envelope("C:/data.bin", "download", "probe", idle_timeout=1, remaining_timeout=10),
            overwrite=False,
            create_dirs=False,
            max_size=len(data) + 1,
            timeout_seconds=10,
        )
        self.assertEqual(probe["total_bytes"], len(data))
        collected = bytearray()
        offsets = []
        offset = 0
        while offset < len(data):
            result = transfers.download_remote_file(
                client,
                "/tmp/data.bin",
                envelope(
                    "C:/data.bin",
                    "download",
                    "chunk",
                    idle_timeout=1,
                    remaining_timeout=10,
                    offset=offset,
                    expected_size=len(data),
                    expected_mtime=probe["remote_mtime"],
                ),
                overwrite=False,
                create_dirs=False,
                max_size=len(data) + 1,
                timeout_seconds=10,
            )
            chunk = base64.b64decode(result["chunk_b64"])
            collected.extend(chunk)
            offset = result["transferred_bytes"]
            offsets.append(offset)
        self.assertEqual(bytes(collected), data)
        self.assertGreater(len(offsets), 1)
        self.assertEqual(offsets[-1], len(data))
        self.assertTrue(all(a < b for a, b in zip(offsets, offsets[1:])))

    def test_download_longer_than_idle_succeeds_when_each_chunk_progresses(self):
        data = b"b" * (transfers.TRANSFER_CHUNK_SIZE * 2)
        sftp = FakeSFTP({"/tmp/slow.bin": data}, read_delay=0.03)
        client = FakeClient(sftp)
        offset = 0
        started = time.monotonic()
        while offset < len(data):
            result = transfers.download_remote_file(
                client,
                "/tmp/slow.bin",
                envelope(
                    "C:/slow.bin",
                    "download",
                    "chunk",
                    idle_timeout=0.05,
                    remaining_timeout=1,
                    offset=offset,
                    expected_size=len(data),
                    expected_mtime=100,
                ),
                overwrite=False,
                create_dirs=False,
                max_size=len(data),
                timeout_seconds=1,
            )
            offset = result["transferred_bytes"]
        self.assertGreater(time.monotonic() - started, 0.05)
        self.assertEqual(offset, len(data))

    def test_download_idle_timeout_is_clear(self):
        sftp = FakeSFTP({"/tmp/slow.bin": b"abc"}, read_delay=0.03)
        client = FakeClient(sftp)
        with self.assertRaisesRegex(RelayError, "нет прогресса"):
            transfers.download_remote_file(
                client,
                "/tmp/slow.bin",
                envelope(
                    "C:/slow.bin",
                    "download",
                    "chunk",
                    idle_timeout=0.01,
                    remaining_timeout=1,
                    offset=0,
                    expected_size=3,
                    expected_mtime=100,
                ),
                overwrite=False,
                create_dirs=False,
                max_size=10,
                timeout_seconds=1,
            )

    def test_download_max_size_is_preserved(self):
        client = FakeClient(FakeSFTP({"/tmp/too-big": b"123456"}))
        with self.assertRaisesRegex(RelayError, "превышает лимит"):
            transfers.download_remote_file(
                client,
                "/tmp/too-big",
                envelope("C:/x", "download", "probe", idle_timeout=1, remaining_timeout=1),
                overwrite=False,
                create_dirs=False,
                max_size=5,
                timeout_seconds=1,
            )

    def test_partial_download_name_is_not_final_name(self):
        target = Path("result.bin")
        partial = transfers.local_partial_path(target)
        self.assertNotEqual(partial, target)
        self.assertTrue(partial.name.endswith(".ssh-relay.part"))

    def test_upload_partial_is_not_final_and_resume_is_rejected(self):
        sftp = FakeSFTP()
        client = FakeClient(sftp)
        begin_meta = envelope(
            "C:/input.bin",
            "upload",
            "begin",
            total_size=6,
            idle_timeout=1,
            remaining_timeout=10,
            discard_partial=False,
        )
        begin = transfers.upload_file_content(
            client, begin_meta, b"", "/tmp/out.bin", overwrite=False, create_dirs=False, max_size=10, timeout_seconds=10
        )
        partial = begin["partial_path"]
        self.assertIn(partial, sftp.files)
        self.assertNotIn("/tmp/out.bin", sftp.files)
        chunk_meta = envelope(
            "C:/input.bin", "upload", "chunk", total_size=6, idle_timeout=1, remaining_timeout=10, offset=0
        )
        transfers.upload_file_content(
            client, chunk_meta, b"abc", "/tmp/out.bin", overwrite=False, create_dirs=False, max_size=10, timeout_seconds=10
        )
        self.assertEqual(bytes(sftp.files[partial]), b"abc")
        self.assertNotIn("/tmp/out.bin", sftp.files)
        with self.assertRaisesRegex(RelayError, "resume не поддерживается"):
            transfers.upload_file_content(
                client, begin_meta, b"", "/tmp/out.bin", overwrite=False, create_dirs=False, max_size=10, timeout_seconds=10
            )

    def test_upload_success_and_safe_overwrite(self):
        sftp = FakeSFTP({"/tmp/out.bin": b"old"})
        client = FakeClient(sftp)
        probe = transfers.upload_file_content(
            client,
            envelope("C:/input.bin", "upload", "probe", total_size=3, idle_timeout=1, remaining_timeout=10),
            b"",
            "/tmp/out.bin",
            overwrite=True,
            create_dirs=False,
            max_size=10,
            timeout_seconds=10,
        )
        self.assertTrue(probe["target_exists"])
        transfers.upload_file_content(
            client,
            envelope(
                "C:/input.bin",
                "upload",
                "begin",
                total_size=3,
                idle_timeout=1,
                remaining_timeout=10,
                discard_partial=False,
            ),
            b"",
            "/tmp/out.bin",
            overwrite=True,
            create_dirs=False,
            max_size=10,
            timeout_seconds=10,
        )
        chunk = transfers.upload_file_content(
            client,
            envelope("C:/input.bin", "upload", "chunk", total_size=3, idle_timeout=1, remaining_timeout=10, offset=0),
            b"new",
            "/tmp/out.bin",
            overwrite=True,
            create_dirs=False,
            max_size=10,
            timeout_seconds=10,
        )
        self.assertEqual(chunk["bytes_uploaded"], 3)
        finish = transfers.upload_file_content(
            client,
            envelope("C:/input.bin", "upload", "finish", total_size=3, idle_timeout=1, remaining_timeout=10, offset=3),
            b"",
            "/tmp/out.bin",
            overwrite=True,
            create_dirs=False,
            max_size=10,
            timeout_seconds=10,
        )
        self.assertEqual(finish["bytes_uploaded"], 3)
        self.assertEqual(bytes(sftp.files["/tmp/out.bin"]), b"new")
        self.assertNotIn(transfers.remote_partial_path("/tmp/out.bin"), sftp.files)

    def test_upload_overwrite_does_not_delete_final_when_posix_rename_fails(self):
        sftp = FakeSFTP({"/tmp/out.bin": b"old"}, posix_rename=False)
        client = FakeClient(sftp)
        transfers.upload_file_content(
            client,
            envelope(
                "C:/input.bin", "upload", "begin", total_size=3, idle_timeout=1, remaining_timeout=10, discard_partial=False
            ),
            b"",
            "/tmp/out.bin",
            overwrite=True,
            create_dirs=False,
            max_size=10,
            timeout_seconds=10,
        )
        transfers.upload_file_content(
            client,
            envelope("C:/input.bin", "upload", "chunk", total_size=3, idle_timeout=1, remaining_timeout=10, offset=0),
            b"new",
            "/tmp/out.bin",
            overwrite=True,
            create_dirs=False,
            max_size=10,
            timeout_seconds=10,
        )
        with self.assertRaisesRegex(RelayError, "безопасно заменить"):
            transfers.upload_file_content(
                client,
                envelope("C:/input.bin", "upload", "finish", total_size=3, idle_timeout=1, remaining_timeout=10, offset=3),
                b"",
                "/tmp/out.bin",
                overwrite=True,
                create_dirs=False,
                max_size=10,
                timeout_seconds=10,
            )
        self.assertEqual(bytes(sftp.files["/tmp/out.bin"]), b"old")
        self.assertIn(transfers.remote_partial_path("/tmp/out.bin"), sftp.files)

    def test_upload_probe_with_create_dirs_has_no_side_effect(self):
        sftp = FakeSFTP()
        client = FakeClient(sftp)
        result = transfers.upload_file_content(
            client,
            envelope("C:/input.bin", "upload", "probe", total_size=3, idle_timeout=1, remaining_timeout=10),
            b"",
            "/new/path/out.bin",
            overwrite=False,
            create_dirs=True,
            max_size=10,
            timeout_seconds=10,
        )
        self.assertFalse(result["parent_exists"])
        self.assertNotIn("/new", sftp.directories)
        self.assertNotIn("/new/path", sftp.directories)

    def test_upload_progress_grows_by_confirmed_chunks(self):
        total = transfers.TRANSFER_CHUNK_SIZE + 7
        sftp = FakeSFTP()
        client = FakeClient(sftp)
        transfers.upload_file_content(
            client,
            envelope("C:/input.bin", "upload", "begin", total_size=total, idle_timeout=1, remaining_timeout=10),
            b"",
            "/tmp/out.bin",
            overwrite=False,
            create_dirs=False,
            max_size=total,
            timeout_seconds=10,
        )
        first = transfers.upload_file_content(
            client,
            envelope("C:/input.bin", "upload", "chunk", total_size=total, idle_timeout=1, remaining_timeout=10, offset=0),
            b"a" * transfers.TRANSFER_CHUNK_SIZE,
            "/tmp/out.bin",
            overwrite=False,
            create_dirs=False,
            max_size=total,
            timeout_seconds=10,
        )
        second = transfers.upload_file_content(
            client,
            envelope(
                "C:/input.bin",
                "upload",
                "chunk",
                total_size=total,
                idle_timeout=1,
                remaining_timeout=10,
                offset=first["bytes_uploaded"],
            ),
            b"b" * 7,
            "/tmp/out.bin",
            overwrite=False,
            create_dirs=False,
            max_size=total,
            timeout_seconds=10,
        )
        self.assertEqual(first["bytes_uploaded"], transfers.TRANSFER_CHUNK_SIZE)
        self.assertEqual(second["bytes_uploaded"], total)
        self.assertGreater(second["bytes_uploaded"], first["bytes_uploaded"])

    def test_upload_idle_timeout_is_clear_and_final_stays_untouched(self):
        sftp = FakeSFTP(write_delay=0.03)
        client = FakeClient(sftp)
        transfers.upload_file_content(
            client,
            envelope(
                "C:/input.bin", "upload", "begin", total_size=3, idle_timeout=1, remaining_timeout=10
            ),
            b"",
            "/tmp/out.bin",
            overwrite=False,
            create_dirs=False,
            max_size=10,
            timeout_seconds=10,
        )
        with self.assertRaisesRegex(RelayError, "нет прогресса"):
            transfers.upload_file_content(
                client,
                envelope(
                    "C:/input.bin", "upload", "chunk", total_size=3, idle_timeout=0.01,
                    remaining_timeout=1, offset=0
                ),
                b"abc",
                "/tmp/out.bin",
                overwrite=False,
                create_dirs=False,
                max_size=10,
                timeout_seconds=1,
            )
        self.assertNotIn("/tmp/out.bin", sftp.files)
        self.assertIn(transfers.remote_partial_path("/tmp/out.bin"), sftp.files)

    def test_upload_max_size_is_preserved(self):
        client = FakeClient(FakeSFTP())
        with self.assertRaisesRegex(RelayError, "превышает лимит"):
            transfers.upload_file_content(
                client,
                envelope("C:/input.bin", "upload", "probe", total_size=11, idle_timeout=1, remaining_timeout=10),
                b"",
                "/tmp/out.bin",
                overwrite=False,
                create_dirs=False,
                max_size=10,
                timeout_seconds=10,
            )


if __name__ == "__main__":
    unittest.main()
