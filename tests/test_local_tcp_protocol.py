from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / 'ssh_relay.py'
spec = importlib.util.spec_from_file_location('ssh_relay_protocol_tested', MODULE_PATH)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeChannel:
    def __init__(self) -> None:
        self.stdout = b''
        self.stderr = b''
        self.exit_code = 0

    def exec_command(self, command: str) -> None:
        if command == 'printf protocol-ok':
            self.stdout = b'protocol-ok'
        elif command == 'printf legacy-ok':
            self.stdout = b'legacy-ok'
        else:
            self.stdout = b''

    def sendall(self, _: bytes) -> None:
        pass

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv(self, _: int) -> bytes:
        value, self.stdout = self.stdout, b''
        return value

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, _: int) -> bytes:
        value, self.stderr = self.stderr, b''
        return value

    def exit_status_ready(self) -> bool:
        return not self.stdout and not self.stderr

    def recv_exit_status(self) -> int:
        return self.exit_code

    def close(self) -> None:
        pass


class FakeTransport:
    def __init__(self) -> None:
        self.active = True
        self.authenticated = True
        self.keepalive = None

    def is_active(self) -> bool:
        return self.active

    def is_authenticated(self) -> bool:
        return self.authenticated

    def set_keepalive(self, interval: int) -> None:
        self.keepalive = interval

    def open_session(self, timeout: int = 10) -> FakeChannel:
        assert timeout == 10
        return FakeChannel()


class FakeSSHClient:
    def __init__(self) -> None:
        self.transport = FakeTransport()

    def load_system_host_keys(self, *_: object) -> None:
        pass

    def set_missing_host_key_policy(self, _: object) -> None:
        pass

    def connect(self, *_: object, **__: object) -> None:
        pass

    def get_transport(self) -> FakeTransport:
        return self.transport

    def close(self) -> None:
        self.transport.active = False
        self.transport.authenticated = False


class FakeRejectPolicy:
    pass


def raw_request(port: int, request: dict[str, object]) -> dict[str, object]:
    with socket.create_connection(('127.0.0.1', port), timeout=2) as sock:
        sock.sendall(json.dumps(request, ensure_ascii=False).encode('utf-8'))
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(3)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b''.join(chunks).decode('utf-8'))


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    identity = root / 'id_ed25519'
    identity.write_text('test', encoding='utf-8')
    env_before = os.environ.get('XDG_STATE_HOME')
    os.environ['XDG_STATE_HOME'] = str(root / 'state')
    try:
        m.load_paramiko = lambda: SimpleNamespace(SSHClient=FakeSSHClient, RejectPolicy=FakeRejectPolicy)
        args = SimpleNamespace(
            name='proto', host='198.51.100.42', port=22, user='donpedro',
            identity_file=str(identity), ask_key_passphrase=False, known_hosts=None,
            command_timeout=5, download_timeout=5, download_max_size=1024,
            upload_timeout=5, upload_max_size=1024, enable_sudo=False,
            detach=False, detach_log=None,
        )
        output = io.StringIO()
        daemon_result = []

        def run_daemon() -> None:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                daemon_result.append(m.daemon(args))

        thread = threading.Thread(target=run_daemon, daemon=True)
        thread.start()
        session_path = m.session_file_path('proto')
        deadline = time.monotonic() + 5
        while not session_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert session_path.exists(), output.getvalue()
        session = m.read_session('proto')
        assert session['version'] == '0.7.0', session
        assert session['operation_protocol_version'] == m.OPERATION_PROTOCOL_VERSION, session
        status = raw_request(session['daemon_port'], {
            'auth_token': session['auth_token'], 'action': 'status',
        })
        assert status['operation_protocol_version'] == m.OPERATION_PROTOCOL_VERSION, status

        # Запрос старого CLI 0.5.x: новых полей нет, ответ остаётся старого формата.
        legacy = raw_request(session['daemon_port'], {
            'auth_token': session['auth_token'],
            'action': 'exec',
            'command': 'printf legacy-ok',
            'risky': False,
            'receipt_path': m.DEFAULT_RISKY_RECEIPT_PATH,
        })
        assert legacy['ok'] is True, legacy
        assert legacy['stdout'] == 'legacy-ok', legacy
        assert legacy['exit_code'] == 0, legacy

        unsupported = raw_request(session['daemon_port'], {
            'auth_token': session['auth_token'],
            'action': 'exec',
            'command': 'printf protocol-ok',
            'operation_protocol_version': [],
        })
        assert unsupported['ok'] is False, unsupported
        assert 'Неподдерживаемая версия' in unsupported['protocol_error'], unsupported

        # Новый CLI проходит через реальный локальный сокет и получает один JSON.
        env = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH), 'exec', '--name', 'proto', '--json', 'printf protocol-ok'],
            text=True, capture_output=True, env=env, timeout=10,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert proc.stderr == '', proc.stderr
        result = json.loads(proc.stdout)
        assert result['operation_status'] == 'succeeded', result
        assert result['command_status'] == 'succeeded', result
        assert result['stdout'] == 'protocol-ok', result
        assert result['receipt_status'] == 'not_requested', result
        assert result['operation_protocol_version'] == m.OPERATION_PROTOCOL_VERSION, result

        stopped = raw_request(session['daemon_port'], {
            'auth_token': session['auth_token'], 'action': 'stop',
        })
        assert stopped['status'] == 'stopping', stopped
        thread.join(timeout=3)
        assert not thread.is_alive(), output.getvalue()
        assert daemon_result == [0], daemon_result
    finally:
        if env_before is None:
            os.environ.pop('XDG_STATE_HOME', None)
        else:
            os.environ['XDG_STATE_HOME'] = env_before

print('Локальный протокольный тест пройден.')
