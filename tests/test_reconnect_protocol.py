from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
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
spec = importlib.util.spec_from_file_location('ssh_relay_reconnect_tested', MODULE_PATH)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# Ускоряем только тестовый daemon; production-значения в файле не меняются.
m.DEFAULT_RECONNECT_WAIT = 1
m.SSH_MONITOR_INTERVAL = 0.01
m.RECONNECT_DELAYS = (0.02, 0.02, 0.02)

CANARY = 'CANARY_RECONNECT_SECRET'


class Controller:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clients: list[FakeSSHClient] = []
        self.connect_attempts = 0
        self.fail_connects_remaining = 0
        self.command_calls: Counter[str] = Counter()
        self.sftp_open_calls = 0
        self.drop_next_sftp = False

    def make_client(self) -> 'FakeSSHClient':
        client = FakeSSHClient(self)
        with self.lock:
            self.clients.append(client)
        return client

    def on_connect(self, client: 'FakeSSHClient') -> None:
        with self.lock:
            self.connect_attempts += 1
            should_fail = self.fail_connects_remaining > 0
            if should_fail:
                self.fail_connects_remaining -= 1
        if should_fail:
            client.transport.active = False
            client.transport.authenticated = False
            raise RuntimeError(CANARY)
        client.transport.active = True
        client.transport.authenticated = True

    def current_transport(self) -> 'FakeTransport':
        with self.lock:
            assert self.clients
            return self.clients[-1].transport


class FakeChannel:
    def __init__(self, controller: Controller, transport: 'FakeTransport') -> None:
        self.controller = controller
        self.transport = transport
        self.stdout = b''
        self.stderr = b''
        self.exit_code = 0

    def exec_command(self, command: str) -> None:
        self.controller.command_calls[command] += 1
        if command == 'disconnect-during-command':
            self.transport.active = False
            self.transport.authenticated = False
            raise OSError('transport dropped')
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
        return self.exit_code

    def close(self) -> None:
        pass


class FakeTransport:
    def __init__(self, controller: Controller) -> None:
        self.controller = controller
        self.active = False
        self.authenticated = False
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
        return FakeChannel(self.controller, self)


class FakeSSHClient:
    def __init__(self, controller: Controller) -> None:
        self.controller = controller
        self.transport = FakeTransport(controller)

    def load_system_host_keys(self, *_: object) -> None:
        pass

    def set_missing_host_key_policy(self, _: object) -> None:
        pass

    def connect(self, *_: object, **__: object) -> None:
        self.controller.on_connect(self)

    def get_transport(self) -> FakeTransport:
        return self.transport

    def open_sftp(self):
        with self.controller.lock:
            self.controller.sftp_open_calls += 1
            drop = self.controller.drop_next_sftp
            self.controller.drop_next_sftp = False
        if drop:
            self.transport.active = False
            self.transport.authenticated = False
            raise OSError('sftp transport dropped')
        raise AssertionError('Успешный SFTP в этом тесте не ожидается')

    def close(self) -> None:
        self.transport.active = False
        self.transport.authenticated = False


class FakeRejectPolicy:
    pass


def raw_request(port: int, request: dict[str, object], timeout: float = 3.0) -> dict[str, object]:
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


def operation_request(
    session: dict[str, object], command: str, tx: str, *, risky: bool = False
) -> dict[str, object]:
    return {
        'auth_token': session['auth_token'],
        'action': 'exec',
        'command': command,
        'risky': risky,
        'receipt_path': m.DEFAULT_RISKY_RECEIPT_PATH,
        'transaction_id': tx,
        'transaction_id_source': 'caller',
        'change_target': '/tmp/reconnect-test' if risky else None,
        'change_description': 'Тест reconnect',
        'machine_mode': True,
        'operation_protocol_version': m.OPERATION_PROTOCOL_VERSION,
    }


def wait_status(port: int, token: str, expected: set[str], timeout: float = 2.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = raw_request(port, {'auth_token': token, 'action': 'status'})
        if last.get('ssh_status') in expected:
            return last
        time.sleep(0.01)
    raise AssertionError((expected, last))


controller = Controller()
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    identity = root / 'id_ed25519'
    identity.write_text('test', encoding='utf-8')
    env_before = os.environ.get('XDG_STATE_HOME')
    os.environ['XDG_STATE_HOME'] = str(root / 'state')
    output = io.StringIO()
    try:
        m.load_paramiko = lambda: SimpleNamespace(SSHClient=controller.make_client, RejectPolicy=FakeRejectPolicy)
        args = SimpleNamespace(
            name='reconnect', host='198.51.100.42', port=22, user='donpedro',
            identity_file=str(identity), ask_key_passphrase=False, known_hosts=None,
            command_timeout=2, download_timeout=2, download_max_size=1024,
            upload_timeout=2, upload_max_size=1024, enable_sudo=False,
            detach=False, detach_log=None,
        )
        daemon_result: list[int] = []

        def run_daemon() -> None:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                daemon_result.append(m.daemon(args))

        thread = threading.Thread(target=run_daemon, daemon=True)
        thread.start()
        session_path = m.session_file_path('reconnect')
        deadline = time.monotonic() + 3
        while not session_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session_path.exists(), output.getvalue()
        session = m.read_session('reconnect')
        port = int(session['daemon_port'])
        token = str(session['auth_token'])

        connected = wait_status(port, token, {'connected'})
        assert connected['daemon_status'] == 'active', connected
        assert controller.current_transport().keepalive == m.SSH_KEEPALIVE_INTERVAL

        # Обрыв между запросами: daemon остаётся жив, запрос ждёт reconnect и выполняется ровно один раз.
        controller.fail_connects_remaining = 3
        first_transport = controller.current_transport()
        first_transport.active = False
        first_transport.authenticated = False
        wait_status(port, token, {'reconnecting', 'disconnected'})
        assert session_path.exists()

        result_box: list[dict[str, object]] = []
        request_thread = threading.Thread(
            target=lambda: result_box.append(raw_request(port, operation_request(session, 'printf recovered', 'tx-recovered'))),
            daemon=True,
        )
        request_thread.start()
        time.sleep(0.08)
        controller.fail_connects_remaining = 0
        request_thread.join(timeout=2)
        assert not request_thread.is_alive(), output.getvalue()
        recovered = result_box[0]
        assert recovered['operation_status'] == 'succeeded', recovered
        assert recovered['stdout'] == 'recovered', recovered
        assert controller.command_calls['printf recovered'] == 1
        wait_status(port, token, {'connected'})

        # Если reconnect не успел до лимита, команда достоверно не запускалась.
        controller.fail_connects_remaining = 1000
        current = controller.current_transport()
        current.active = False
        current.authenticated = False
        wait_status(port, token, {'reconnecting', 'disconnected'})
        before = controller.command_calls['printf should-not-run']
        not_started = raw_request(port, operation_request(session, 'printf should-not-run', 'tx-wait-timeout'), timeout=3)
        assert not_started['operation_status'] == 'not_started', not_started
        assert not_started['command_status'] == 'not_started', not_started
        assert not_started['error_code'] == 'ssh_reconnecting', not_started
        assert controller.command_calls['printf should-not-run'] == before
        assert session_path.exists()

        controller.fail_connects_remaining = 0
        wait_status(port, token, {'connected'}, timeout=2)

        # Обрыв после старта команды: результат unknown, повтор команды запрещён.
        unknown = raw_request(port, operation_request(session, 'disconnect-during-command', 'tx-unknown'))
        assert unknown['operation_status'] == 'unknown', unknown
        assert unknown['command_status'] == 'unknown', unknown
        assert controller.command_calls['disconnect-during-command'] == 1
        wait_status(port, token, {'connected'}, timeout=2)
        final = raw_request(port, operation_request(session, 'printf final', 'tx-final'))
        assert final['operation_status'] == 'succeeded', final
        assert final['stdout'] == 'final', final
        assert controller.command_calls['printf final'] == 1

        # Обрыв на preflight risky receipt: основная команда не должна запускаться.
        orig_read_hash = m.read_previous_receipt_hash
        orig_unused = m.ensure_transaction_id_unused
        orig_write_receipt = m.write_risky_receipt
        try:
            def drop_during_preflight(client, **_: object):
                transport = client.get_transport()
                transport.active = False
                transport.authenticated = False
                raise m.RemoteCommandError(
                    'preflight transport dropped',
                    error_code='command_result_unknown',
                    command_started=True,
                )

            m.read_previous_receipt_hash = drop_during_preflight
            before = controller.command_calls['printf risky-preflight']
            preflight = raw_request(
                port,
                operation_request(session, 'printf risky-preflight', 'tx-preflight-drop', risky=True),
            )
            assert preflight['operation_status'] == 'not_started', preflight
            assert preflight['command_status'] == 'not_started', preflight
            assert preflight['error_stage'] == 'receipt', preflight
            assert controller.command_calls['printf risky-preflight'] == before
            wait_status(port, token, {'connected'}, timeout=2)

            # Команда успешна, но SSH теряется при подтверждении receipt: partial_success.
            m.read_previous_receipt_hash = lambda *a, **k: None
            m.ensure_transaction_id_unused = lambda *a, **k: None

            def drop_during_receipt(client, **_: object):
                transport = client.get_transport()
                transport.active = False
                transport.authenticated = False
                return 'unknown', 'Receipt мог быть записан; связь потеряна.'

            m.write_risky_receipt = drop_during_receipt
            receipt_drop = raw_request(
                port,
                operation_request(session, 'printf risky-changed', 'tx-receipt-drop', risky=True),
            )
            assert receipt_drop['operation_status'] == 'partial_success', receipt_drop
            assert receipt_drop['command_status'] == 'succeeded', receipt_drop
            assert receipt_drop['receipt_status'] == 'unknown', receipt_drop
            assert receipt_drop['partial_success'] is True, receipt_drop
            assert controller.command_calls['printf risky-changed'] == 1
            wait_status(port, token, {'connected'}, timeout=2)
        finally:
            m.read_previous_receipt_hash = orig_read_hash
            m.ensure_transaction_id_unused = orig_unused
            m.write_risky_receipt = orig_write_receipt

        # Передачи файлов после начала также не повторяются автоматически.
        for action, payload in (
            ('upload', {
                'local_path': str(root / 'payload.bin'),
                'remote_path': '/tmp/payload.bin',
                'content_b64': base64.b64encode(b'payload').decode('ascii'),
                'overwrite': True,
                'create_dirs': False,
            }),
            ('download', {
                'remote_path': '/tmp/payload.bin',
                'local_path': str(root / 'download.bin'),
                'overwrite': True,
                'create_dirs': False,
            }),
        ):
            controller.drop_next_sftp = True
            before_sftp = controller.sftp_open_calls
            transfer = raw_request(port, {
                'auth_token': token,
                'action': action,
                **payload,
            })
            assert transfer['ok'] is False, transfer
            assert 'Результат операции неизвестен' in str(transfer['protocol_error']), transfer
            assert controller.sftp_open_calls == before_sftp + 1
            wait_status(port, token, {'connected'}, timeout=2)

        # Ошибка reconnect не должна протащить тестовый секрет в status или вывод daemon.
        assert CANARY not in output.getvalue(), output.getvalue()
        status = raw_request(port, {'auth_token': token, 'action': 'status'})
        assert CANARY not in json.dumps(status, ensure_ascii=False)

        stopped = raw_request(port, {'auth_token': token, 'action': 'stop'})
        assert stopped['status'] == 'stopping', stopped
        thread.join(timeout=3)
        assert not thread.is_alive(), output.getvalue()
        assert daemon_result == [0], daemon_result
        assert not session_path.exists()
    finally:
        if env_before is None:
            os.environ.pop('XDG_STATE_HOME', None)
        else:
            os.environ['XDG_STATE_HOME'] = env_before

print('Автоматические проверки reconnect пройдены.')
