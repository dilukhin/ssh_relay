from __future__ import annotations

import hashlib
import importlib.util
import io
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / 'ssh_relay.py'
spec = importlib.util.spec_from_file_location('ssh_relay_transfer_tested', MODULE_PATH)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CommitWriter(io.BytesIO):
    def __init__(self, sftp: 'FakeSFTP', path: str, *, fail: bool = False) -> None:
        super().__init__()
        self.sftp = sftp
        self.path = path
        self.fail = fail

    def write(self, data: bytes) -> int:
        if self.fail:
            raise OSError('test write failure')
        return super().write(data)

    def close(self) -> None:
        if not self.closed and not self.fail:
            self.sftp.files[self.path] = self.getvalue()
        super().close()


class FakeSFTP:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {'.', '/', '/tmp', 'C:', 'C:/Windows', 'C:/Windows/Temp'}
        self.fail_next_write = False

    def stat(self, path: str):
        if path in self.dirs:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_size=0)
        if path in self.files:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=len(self.files[path]))
        raise OSError('not found')

    def mkdir(self, path: str) -> None:
        self.dirs.add(path)

    def open(self, path: str, mode: str):
        if mode == 'rb':
            if path not in self.files:
                raise OSError('not found')
            return io.BytesIO(self.files[path])
        if mode == 'wb':
            fail = self.fail_next_write
            self.fail_next_write = False
            return CommitWriter(self, path, fail=fail)
        raise AssertionError(mode)

    def remove(self, path: str) -> None:
        if path not in self.files:
            raise OSError('not found')
        del self.files[path]

    def rename(self, source: str, target: str) -> None:
        if source not in self.files:
            raise OSError('source not found')
        self.files[target] = self.files.pop(source)

    def posix_rename(self, source: str, target: str) -> None:
        self.rename(source, target)

    def close(self) -> None:
        pass


class FakeClient:
    def __init__(self, sftp: FakeSFTP) -> None:
        self.sftp = sftp
        self.open_calls = 0

    def open_sftp(self) -> FakeSFTP:
        self.open_calls += 1
        return self.sftp


def assert_no_temporary_files(sftp: FakeSFTP) -> None:
    leftovers = [path for path in sftp.files if '.ssh-relay-' in path and path.endswith('.tmp')]
    assert leftovers == [], leftovers


sftp = FakeSFTP()
client = FakeClient(sftp)

# Windows-style удалённый путь нормализуется до SFTP-формы.
assert m.normalize_remote_sftp_path(r'C:\Windows\Temp\tool.ps1') == 'C:/Windows/Temp/tool.ps1'

# Overwrite должен менять содержимое даже при том же размере файла.
target = 'C:/Windows/Temp/tool.ps1'
sftp.files[target] = b'old!'
result = m.upload_file_content(
    client,
    r'C:\work\tool.ps1',
    b'new!',
    r'C:\Windows\Temp\tool.ps1',
    overwrite=True,
    create_dirs=False,
    max_size=1024,
    timeout_seconds=5,
)
assert result['remote_path'] == target, result
assert result['bytes_uploaded'] == 4, result
assert sftp.files[target] == b'new!'
assert hashlib.sha256(sftp.files[target]).digest() == hashlib.sha256(b'new!').digest()
assert_no_temporary_files(sftp)

# Создание вложенных каталогов и атомарный rename временного файла.
nested = '/tmp/deep/tree/config.bin'
result = m.upload_file_content(
    client,
    '/local/config.bin',
    b'abcdef',
    nested,
    overwrite=False,
    create_dirs=True,
    max_size=1024,
    timeout_seconds=5,
)
assert result['bytes_uploaded'] == 6, result
assert sftp.files[nested] == b'abcdef'
assert '/tmp/deep' in sftp.dirs
assert '/tmp/deep/tree' in sftp.dirs
assert_no_temporary_files(sftp)

# Ошибка записи не должна оставлять временный файл.
sftp.fail_next_write = True
try:
    m.upload_file_content(
        client,
        '/local/broken.bin',
        b'broken',
        '/tmp/broken.bin',
        overwrite=True,
        create_dirs=False,
        max_size=1024,
        timeout_seconds=5,
    )
except m.RelayError:
    pass
else:
    raise AssertionError('Ошибка SFTP-записи не была передана вызывающей стороне')
assert '/tmp/broken.bin' not in sftp.files
assert_no_temporary_files(sftp)

# Download создаёт локальный каталог, заменяет файл только с overwrite и сохраняет байты.
sftp.files['/tmp/download.bin'] = b'remote-data'
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    local = root / 'nested' / 'download.bin'
    result = m.download_remote_file(
        client,
        '/tmp/download.bin',
        str(local),
        overwrite=False,
        create_dirs=True,
        max_size=1024,
        timeout_seconds=5,
    )
    assert result['bytes_downloaded'] == len(b'remote-data'), result
    assert local.read_bytes() == b'remote-data'
    assert hashlib.sha256(local.read_bytes()).digest() == hashlib.sha256(b'remote-data').digest()

    sftp.files['/tmp/download.bin'] = b'new-content'
    result = m.download_remote_file(
        client,
        '/tmp/download.bin',
        str(local),
        overwrite=True,
        create_dirs=False,
        max_size=1024,
        timeout_seconds=5,
    )
    assert result['bytes_downloaded'] == len(b'new-content'), result
    assert local.read_bytes() == b'new-content'
    assert not list(local.parent.glob('.*.ssh-relay-*.tmp'))

print('Автоматические проверки upload/download пройдены.')
