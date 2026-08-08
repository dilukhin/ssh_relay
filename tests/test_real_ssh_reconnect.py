from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

if os.environ.get('SSH_RELAY_REAL_SSH_TEST') != '1':
    print('Реальный SSH reconnect-тест пропущен: SSH_RELAY_REAL_SSH_TEST != 1.')
    raise SystemExit(0)

MODULE_PATH = Path(__file__).resolve().parents[1] / 'ssh_relay.py'
spec = importlib.util.spec_from_file_location('ssh_relay_real_reconnect_tested', MODULE_PATH)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

m.DEFAULT_RECONNECT_WAIT = 3
m.SSH_MONITOR_INTERVAL = 0.05
m.RECONNECT_DELAYS = (0.05, 0.05, 0.1)
m.SSH_KEEPALIVE_INTERVAL = 1

TARGET_HOST = os.environ.get('SSH_RELAY_TEST_SSH_HOST', '127.0.0.1')
TARGET_PORT = int(os.environ.get('SSH_RELAY_TEST_SSH_PORT', '2222'))
TEST_USER = os.environ.get('SSH_RELAY_TEST_USER', 'relaytest')
IDENTITY_FILE = Path(os.environ['SSH_RELAY_TEST_IDENTITY']).resolve()
assert IDENTITY_FILE.is_file(), IDENTITY_FILE


class TcpFaultProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(20)
        self.listener.settimeout(0.2)
        self.port = int(self.listener.getsockname()[1])
        self.stop_event = threading.Event()
        self.allow_new = threading.Event()
        self.allow_new.set()
        self.lock = threading.Lock()
        self.pairs: list[tuple[socket.socket, socket.socket]] = []
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.allow_new.is_set():
                client.close()
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=2)
            except OSError:
                client.close()
                continue
            pair = (client, upstream)
            with self.lock:
                self.pairs.append(pair)
            threading.Thread(target=self._pipe, args=(pair, client, upstream), daemon=True).start()
            threading.Thread(target=self._pipe, args=(pair, upstream, client), daemon=True).start()

    def _pipe(
        self,
        pair: tuple[socket.socket, socket.socket],
        source: socket.socket,
        target: socket.socket,
    ) -> None:
        try:
            while not self.stop_event.is_set():
                data = source.recv(65536)
                if not data:
                    return
                target.sendall(data)
        except OSError:
            return
        finally:
            self._close_pair(pair)

    def _close_pair(self, pair: tuple[socket.socket, socket.socket]) -> None:
        with self.lock:
            if pair not in self.pairs:
                return
            self.pairs.remove(pair)
        for sock in pair:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def drop_connections(self) -> None:
        with self.lock:
            pairs = list(self.pairs)
        for pair in pairs:
            self._close_pair(pair)

    def pause(self) -> None:
        self.allow_new.clear()
        self.drop_connections()

    def resume(self) -> None:
        self.allow_new.set()

    def close(self) -> None:
        self.stop_event.set()
        self.allow_new.set()
        self.drop_connections()
        try:
            self.listener.close()
        except OSError:
            pass
        self.thread.join(timeout=1)


def raw_request(port: int, request: dict[str, object], timeout: float = 10.0) -> dict[str, object]:
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


def operation(
    session: dict[str, object],
    command: str,
    tx: str,
    *,
    action: str = 'exec',
    risky: bool = False,
    receipt_path: str | None = None,
    change_target: str | None = None,
) -> dict[str, object]:
    return {
        'auth_token': session['auth_token'],
        'action': action,
        'command': command,
        'risky': risky,
        'receipt_path': receipt_path or m.DEFAULT_RISKY_RECEIPT_PATH,
        'transaction_id': tx,
        'transaction_id_source': 'caller',
        'change_target': change_target,
        'change_description': 'Реальный SSH reconnect-тест',
        'machine_mode': True,
        'operation_protocol_version': m.OPERATION_PROTOCOL_VERSION,
    }


def wait_status(session: dict[str, object], expected: set[str], timeout: float = 8.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = raw_request(int(session['daemon_port']), {
            'auth_token': session['auth_token'], 'action': 'status',
        })
        if last.get('ssh_status') in expected:
            return last
        time.sleep(0.05)
    raise AssertionError((expected, last))


proxy = TcpFaultProxy(TARGET_HOST, TARGET_PORT)
try:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        known_hosts = root / 'known_hosts'
        scan = subprocess.run(
            ['ssh-keyscan', '-p', str(proxy.port), '127.0.0.1'],
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert scan.returncode == 0 and scan.stdout.strip(), (scan.returncode, scan.stdout, scan.stderr)
        known_hosts.write_text(scan.stdout, encoding='utf-8')

        old_state = os.environ.get('XDG_STATE_HOME')
        os.environ['XDG_STATE_HOME'] = str(root / 'state')
        try:
            args = SimpleNamespace(
                name='real', host='127.0.0.1', port=proxy.port, user=TEST_USER,
                identity_file=str(IDENTITY_FILE), ask_key_passphrase=False,
                known_hosts=str(known_hosts), command_timeout=6,
                download_timeout=6, download_max_size=1024 * 1024,
                upload_timeout=6, upload_max_size=1024 * 1024,
                enable_sudo=True, detach=False, detach_log=None,
            )
            daemon_result: list[int] = []
            m.getpass.getpass = lambda _prompt: 'relaypass'
            thread = threading.Thread(target=lambda: daemon_result.append(m.daemon(args)), daemon=True)
            thread.start()

            session_path = m.session_file_path('real')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not session_path.exists():
                time.sleep(0.05)
            assert session_path.exists()
            session = m.read_session('real')
            wait_status(session, {'connected'})

            initial = raw_request(
                int(session['daemon_port']),
                operation(session, 'printf real-ok', 'real-initial'),
            )
            assert initial['operation_status'] == 'succeeded', initial
            assert initial['stdout'] == 'real-ok', initial

            # Реальная risky-операция пишет безопасный JSONL receipt без текста команды.
            receipt_path = '/tmp/ssh-relay-real-changes.jsonl'
            raw_request(
                int(session['daemon_port']),
                operation(session, f'rm -f {receipt_path} /tmp/ssh-relay-real-risky', 'real-clean-risky'),
            )
            risky = raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    'touch /tmp/ssh-relay-real-risky',
                    'real-risky-1',
                    risky=True,
                    receipt_path=receipt_path,
                    change_target='/tmp/ssh-relay-real-risky',
                ),
            )
            assert risky['operation_status'] == 'succeeded', risky
            assert risky['receipt_status'] == 'written', risky
            receipt_read = raw_request(
                int(session['daemon_port']),
                operation(session, f'tail -n 1 {receipt_path}', 'real-read-receipt'),
            )
            record = json.loads(str(receipt_read['stdout']).strip())
            assert record['transaction_id'] == 'real-risky-1', record
            assert 'command' not in record, record
            assert str(record['command_hash']).startswith('sha256:'), record
            assert str(record['receipt_hash']).startswith('sha256:'), record

            duplicate = raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    'printf duplicate >> /tmp/ssh-relay-real-risky',
                    'real-risky-1',
                    risky=True,
                    receipt_path=receipt_path,
                    change_target='/tmp/ssh-relay-real-risky',
                ),
            )
            assert duplicate['operation_status'] == 'not_started', duplicate
            assert duplicate['error_code'] == 'transaction_id_exists', duplicate

            # Ошибка записи receipt после успешной команды должна стать partial_success.
            ro_dir = '/tmp/ssh-relay-real-ro-receipt'
            raw_request(
                int(session['daemon_port']),
                operation(session, f'rm -rf {ro_dir} && mkdir -m 700 {ro_dir}', 'real-ro-setup'),
            )
            partial = raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    f'chmod 500 {ro_dir}',
                    'real-partial',
                    risky=True,
                    receipt_path=f'{ro_dir}/changes.jsonl',
                    change_target=ro_dir,
                ),
            )
            assert partial['operation_status'] == 'partial_success', partial
            assert partial['command_status'] == 'succeeded', partial
            assert partial['receipt_status'] == 'failed', partial
            raw_request(
                int(session['daemon_port']),
                operation(session, f'chmod 700 {ro_dir} && rm -rf {ro_dir}', 'real-ro-clean'),
            )

            # Повреждённая последняя запись блокирует risky-команду до запуска.
            corrupt_path = '/tmp/ssh-relay-real-corrupt.jsonl'
            raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    f"printf '%s\n' '{{broken-json' > {corrupt_path}; rm -f /tmp/ssh-relay-should-not-exist",
                    'real-corrupt-setup',
                ),
            )
            corrupt = raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    'touch /tmp/ssh-relay-should-not-exist',
                    'real-corrupt-risky',
                    risky=True,
                    receipt_path=corrupt_path,
                    change_target='/tmp/ssh-relay-should-not-exist',
                ),
            )
            assert corrupt['operation_status'] == 'not_started', corrupt
            absent = raw_request(
                int(session['daemon_port']),
                operation(session, 'test ! -e /tmp/ssh-relay-should-not-exist', 'real-corrupt-verify'),
            )
            assert absent['operation_status'] == 'succeeded', absent

            # Реальный sudo-exec использует тот же машинный контракт и receipt.
            sudo_receipt = '/var/tmp/ssh-relay-real-sudo-changes.jsonl'
            sudo_target = '/var/tmp/ssh-relay-real-sudo-target'
            sudo_result = raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    f'printf root-ok > {sudo_target}',
                    'real-sudo-risky',
                    action='sudo_exec',
                    risky=True,
                    receipt_path=sudo_receipt,
                    change_target=sudo_target,
                ),
            )
            assert sudo_result['operation_status'] == 'succeeded', sudo_result
            assert sudo_result['receipt_status'] == 'written', sudo_result
            assert sudo_result['sudo'] is True, sudo_result
            sudo_verify = raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    f'cat {sudo_target}',
                    'real-sudo-verify',
                    action='sudo_exec',
                ),
            )
            assert sudo_verify['stdout'] == 'root-ok', sudo_verify
            raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    f'rm -f {sudo_target} {sudo_receipt}',
                    'real-sudo-clean',
                    action='sudo_exec',
                ),
            )

            # Реальные SFTP upload/download проверяются через локальный relay-протокол.
            payload = b'real-sftp-payload'
            upload = raw_request(int(session['daemon_port']), {
                'auth_token': session['auth_token'],
                'action': 'upload',
                'local_path': str(root / 'source.bin'),
                'remote_path': '/tmp/ssh-relay-real-sftp.bin',
                'content_b64': __import__('base64').b64encode(payload).decode('ascii'),
                'overwrite': True,
                'create_dirs': False,
            })
            assert upload['ok'] is True and upload['bytes_uploaded'] == len(payload), upload
            local_download = root / 'download.bin'
            download = raw_request(int(session['daemon_port']), {
                'auth_token': session['auth_token'],
                'action': 'download',
                'remote_path': '/tmp/ssh-relay-real-sftp.bin',
                'local_path': str(local_download),
                'overwrite': True,
                'create_dirs': False,
            })
            assert download['ok'] is True and download['bytes_downloaded'] == len(payload), download
            assert local_download.read_bytes() == payload

            # Новая команда может ждать reconnect, но должна выполниться один раз.
            proxy.pause()
            wait_status(session, {'reconnecting', 'disconnected'})
            box: list[dict[str, object]] = []
            waiting = threading.Thread(
                target=lambda: box.append(raw_request(
                    int(session['daemon_port']),
                    operation(session, 'printf after-reconnect', 'real-wait'),
                )),
                daemon=True,
            )
            waiting.start()
            time.sleep(0.3)
            proxy.resume()
            waiting.join(timeout=8)
            assert not waiting.is_alive()
            assert box and box[0]['operation_status'] == 'succeeded', box
            assert box[0]['stdout'] == 'after-reconnect', box[0]
            wait_status(session, {'connected'})

            # Начатая команда после сетевого обрыва не повторяется автоматически.
            cleanup = raw_request(
                int(session['daemon_port']),
                operation(session, 'rm -f /tmp/ssh-relay-real-once', 'real-clean-before'),
            )
            assert cleanup['operation_status'] == 'succeeded', cleanup

            uncertain_box: list[dict[str, object]] = []
            long_command = (
                "sh -c 'printf x >> /tmp/ssh-relay-real-once; "
                "sleep 3; printf y >> /tmp/ssh-relay-real-once'"
            )
            uncertain_thread = threading.Thread(
                target=lambda: uncertain_box.append(raw_request(
                    int(session['daemon_port']),
                    operation(session, long_command, 'real-unknown'),
                    timeout=10,
                )),
                daemon=True,
            )
            uncertain_thread.start()
            time.sleep(0.5)
            proxy.drop_connections()
            uncertain_thread.join(timeout=8)
            assert not uncertain_thread.is_alive()
            assert uncertain_box, uncertain_box
            uncertain = uncertain_box[0]
            assert uncertain['operation_status'] == 'unknown', uncertain
            assert uncertain['command_status'] == 'unknown', uncertain

            wait_status(session, {'connected'}, timeout=8)
            verify = raw_request(
                int(session['daemon_port']),
                operation(session, 'cat /tmp/ssh-relay-real-once 2>/dev/null || true', 'real-verify'),
            )
            assert verify['operation_status'] == 'succeeded', verify
            observed = str(verify['stdout'])
            assert observed.count('x') == 1, observed

            raw_request(
                int(session['daemon_port']),
                operation(
                    session,
                    f'rm -f /tmp/ssh-relay-real-once /tmp/ssh-relay-real-risky {receipt_path} {corrupt_path} /tmp/ssh-relay-real-sftp.bin',
                    'real-clean-after',
                ),
            )
            stopped = raw_request(int(session['daemon_port']), {
                'auth_token': session['auth_token'], 'action': 'stop',
            })
            assert stopped['status'] == 'stopping', stopped
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert daemon_result == [0], daemon_result
        finally:
            if old_state is None:
                os.environ.pop('XDG_STATE_HOME', None)
            else:
                os.environ['XDG_STATE_HOME'] = old_state
finally:
    proxy.close()

print('Реальный Paramiko reconnect-тест пройден.')
