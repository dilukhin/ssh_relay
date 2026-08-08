from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / 'ssh_relay.py'
spec = importlib.util.spec_from_file_location('ssh_relay_multi_session_tested', MODULE_PATH)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

m.DEFAULT_RECONNECT_WAIT = 0.5
m.SSH_MONITOR_INTERVAL = 0.01
m.RECONNECT_DELAYS = (0.02, 0.02, 0.02)


class HostState:
    def __init__(self, host: str) -> None:
        self.host = host
        self.lock = threading.Lock()
        self.transports: list[FakeTransport] = []
        self.fail_connects_remaining = 0
        self.commands: Counter[str] = Counter()

    def newest_transport(self) -> 'FakeTransport':
        with self.lock:
            assert self.transports
            return self.transports[-1]


class MultiController:
    def __init__(self) -> None:
        self.states = {
            '198.51.100.41': HostState('198.51.100.41'),
            '198.51.100.42': HostState('198.51.100.42'),
        }

    def make_client(self) -> 'FakeSSHClient':
        return FakeSSHClient(self)


class FakeChannel:
    def __init__(self, state: HostState, transport: 'FakeTransport') -> None:
        self.state = state
        self.transport = transport
        self.stdout = b''
        self.stderr = b''

    def exec_command(self, command: str) -> None:
        if not self.transport.active:
            raise OSError('transport inactive')
        self.state.commands[command] += 1
        if command.startswith('printf '):
            self.stdout = command[7:].encode('utf-8')

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
        return 0

    def close(self) -> None:
        pass


class FakeTransport:
    def __init__(self, state: HostState) -> None:
        self.state = state
        self.active = True
        self.authenticated = True
        self.keepalive: int | None = None

    def is_active(self) -> bool:
        return self.active

    def is_authenticated(self) -> bool:
        return self.authenticated

    def set_keepalive(self, interval: int) -> None:
        self.keepalive = interval

    def open_session(self, timeout: int = 10) -> FakeChannel:
        assert timeout == 10
        if not self.active:
            raise OSError('transport inactive')
        return FakeChannel(self.state, self)


class FakeSSHClient:
    def __init__(self, controller: MultiController) -> None:
        self.controller = controller
        self.state: HostState | None = None
        self.transport: FakeTransport | None = None

    def load_system_host_keys(self, *_: object) -> None:
        pass

    def set_missing_host_key_policy(self, _: object) -> None:
        pass

    def connect(self, host: str, **_: object) -> None:
        state = self.controller.states[host]
        with state.lock:
            if state.fail_connects_remaining > 0:
                state.fail_connects_remaining -= 1
                raise OSError('planned reconnect failure')
            transport = FakeTransport(state)
            state.transports.append(transport)
        self.state = state
        self.transport = transport

    def get_transport(self) -> FakeTransport | None:
        return self.transport

    def close(self) -> None:
        if self.transport is not None:
            self.transport.active = False
            self.transport.authenticated = False


class FakeRejectPolicy:
    pass


def raw_request(port: int, request: dict[str, object], timeout: float = 2.0) -> dict[str, object]:
    with socket.create_connection(('127.0.0.1', port), timeout=2) as sock:
        sock.sendall(json.dumps(request, ensure_ascii=False).encode('utf-8'))
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(timeout)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b''.join(chunks).decode('utf-8'))


def operation(session: dict[str, object], command: str, tx: str) -> dict[str, object]:
    return {
        'auth_token': session['auth_token'],
        'action': 'exec',
        'command': command,
        'risky': False,
        'receipt_path': m.DEFAULT_RISKY_RECEIPT_PATH,
        'transaction_id': tx,
        'transaction_id_source': 'caller',
        'change_target': None,
        'change_description': 'Проверка независимости сессий',
        'machine_mode': True,
        'operation_protocol_version': m.OPERATION_PROTOCOL_VERSION,
    }


def wait_session(name: str, timeout: float = 3.0) -> dict[str, object]:
    path = m.session_file_path(name)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return m.read_session(name)
        time.sleep(0.01)
    raise AssertionError(f'Сессия {name} не запустилась')


def wait_ssh(session: dict[str, object], expected: set[str], timeout: float = 2.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = raw_request(int(session['daemon_port']), {
            'auth_token': session['auth_token'], 'action': 'status',
        })
        if last.get('ssh_status') in expected:
            return last
        time.sleep(0.01)
    raise AssertionError((expected, last))


controller = MultiController()
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    identity = root / 'id_ed25519'
    identity.write_text('test', encoding='utf-8')
    old_state = os.environ.get('XDG_STATE_HOME')
    os.environ['XDG_STATE_HOME'] = str(root / 'state')
    try:
        m.load_paramiko = lambda: SimpleNamespace(SSHClient=controller.make_client, RejectPolicy=FakeRejectPolicy)

        results: dict[str, int] = {}
        threads: list[threading.Thread] = []
        for name, host in (('one', '198.51.100.41'), ('two', '198.51.100.42')):
            args = SimpleNamespace(
                name=name, host=host, port=22, user='donpedro',
                identity_file=str(identity), ask_key_passphrase=False, known_hosts=None,
                command_timeout=2, download_timeout=2, download_max_size=1024,
                upload_timeout=2, upload_max_size=1024, enable_sudo=False,
                detach=False, detach_log=None,
            )
            thread = threading.Thread(
                target=lambda n=name, a=args: results.__setitem__(n, m.daemon(a)),
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        one = wait_session('one')
        two = wait_session('two')
        assert one['host'] == '198.51.100.41', one
        assert two['host'] == '198.51.100.42', two
        assert one['daemon_port'] != two['daemon_port']

        r1 = raw_request(int(one['daemon_port']), operation(one, 'printf one', 'tx-one-1'))
        r2 = raw_request(int(two['daemon_port']), operation(two, 'printf two', 'tx-two-1'))
        assert r1['stdout'] == 'one' and r1['remote_host'] == '198.51.100.41', r1
        assert r2['stdout'] == 'two' and r2['remote_host'] == '198.51.100.42', r2

        # Первая сессия теряет SSH и не должна влиять на вторую.
        state_one = controller.states['198.51.100.41']
        state_one.fail_connects_remaining = 20
        broken = state_one.newest_transport()
        broken.active = False
        broken.authenticated = False
        wait_ssh(one, {'reconnecting', 'disconnected'})

        r2_live = raw_request(int(two['daemon_port']), operation(two, 'printf two-live', 'tx-two-2'))
        assert r2_live['operation_status'] == 'succeeded', r2_live
        assert r2_live['stdout'] == 'two-live', r2_live
        assert r2_live['remote_host'] == '198.51.100.42', r2_live
        assert controller.states['198.51.100.42'].commands['printf two-live'] == 1

        # После восстановления первая сессия продолжает использовать свой target.
        state_one.fail_connects_remaining = 0
        wait_ssh(one, {'connected'}, timeout=2)
        r1_live = raw_request(int(one['daemon_port']), operation(one, 'printf one-live', 'tx-one-2'))
        assert r1_live['operation_status'] == 'succeeded', r1_live
        assert r1_live['stdout'] == 'one-live', r1_live
        assert r1_live['remote_host'] == '198.51.100.41', r1_live
        assert controller.states['198.51.100.41'].commands['printf one-live'] == 1

        # list видит обе сессии, а stop завершает их независимо.
        assert set(m.iter_session_names()) == {'one', 'two'}
        assert m.list_sessions(SimpleNamespace()) == 0
        for session in (one, two):
            stopped = raw_request(int(session['daemon_port']), {
                'auth_token': session['auth_token'], 'action': 'stop',
            })
            assert stopped['status'] == 'stopping', stopped
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        assert results == {'one': 0, 'two': 0}, results
        assert m.iter_session_names() == []
    finally:
        if old_state is None:
            os.environ.pop('XDG_STATE_HOME', None)
        else:
            os.environ['XDG_STATE_HOME'] = old_state

print('Автоматические проверки двух именованных сессий пройдены.')
