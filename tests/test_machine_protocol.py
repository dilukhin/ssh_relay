from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'ssh_relay.py'
spec = importlib.util.spec_from_file_location('ssh_relay_tested', MODULE_PATH)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

session = {
    'name': 'prod',
    'host': '198.51.100.42',
    'port': 22,
    'user': 'donpedro',
}

orig_read = m.read_previous_receipt_hash
orig_unused = m.ensure_transaction_id_unused
orig_write = m.write_risky_receipt
orig_exec = m.execute_remote_command
orig_sudo = m.execute_sudo_command

try:
    m.read_previous_receipt_hash = lambda *a, **k: 'sha256:' + '1' * 64
    m.ensure_transaction_id_unused = lambda *a, **k: None
    m.execute_remote_command = lambda *a, **k: {'ok': True, 'stdout': 'ok\n', 'stderr': '', 'exit_code': 0}
    m.write_risky_receipt = lambda *a, **k: ('written', None)
    result = m.execute_command_operation(
        object(), session=session, action='exec', command='touch /tmp/x', risky=True,
        receipt_path='/tmp/changes.jsonl', transaction_id='agent-safe:tx1',
        transaction_id_source='caller', change_target='/tmp/x',
        change_description='Создание тестового файла', timeout_seconds=10, sudo_password=None,
    )
    assert result['operation_status'] == 'succeeded', result
    assert result['receipt_status'] == 'written', result
    assert result['command_status'] == 'succeeded', result
    assert result['partial_success'] is False
    assert result['command_hash'].startswith('sha256:')
    assert result['previous_receipt_hash'] == 'sha256:' + '1' * 64
    assert m.machine_exit_code(result) == 0

    m.execute_remote_command = lambda *a, **k: {'ok': True, 'stdout': '', 'stderr': 'bad\n', 'exit_code': 7}
    result = m.execute_command_operation(
        object(), session=session, action='exec', command='false', risky=True,
        receipt_path='/tmp/changes.jsonl', transaction_id='tx2',
        transaction_id_source='caller', change_target=None,
        change_description='Удалённое изменение', timeout_seconds=10, sudo_password=None,
    )
    assert result['operation_status'] == 'command_failed', result
    assert result['command_exit_code'] == 7
    assert result['receipt_status'] == 'not_attempted'
    assert m.machine_exit_code(result) == 11

    m.execute_remote_command = lambda *a, **k: {'ok': True, 'stdout': '', 'stderr': '', 'exit_code': 0}
    m.write_risky_receipt = lambda *a, **k: ('failed', 'test failure')
    result = m.execute_command_operation(
        object(), session=session, action='exec', command='touch /tmp/y', risky=True,
        receipt_path='/tmp/changes.jsonl', transaction_id='tx3',
        transaction_id_source='caller', change_target='/tmp/y',
        change_description='Создание тестового файла', timeout_seconds=10, sudo_password=None,
    )
    assert result['operation_status'] == 'partial_success', result
    assert result['partial_success'] is True
    assert result['receipt_status'] == 'failed'
    assert m.machine_exit_code(result) == 12

    def unknown(*a, **k):
        raise m.RemoteCommandError('timeout', error_code='command_timeout', command_started=True, stdout='part', stderr='')
    m.execute_remote_command = unknown
    result = m.execute_command_operation(
        object(), session=session, action='exec', command='sleep 20', risky=False,
        receipt_path='/tmp/changes.jsonl', transaction_id='tx4',
        transaction_id_source='caller', change_target=None,
        change_description='Удалённое изменение', timeout_seconds=10, sudo_password=None,
    )
    assert result['operation_status'] == 'unknown', result
    assert result['command_status'] == 'unknown'
    assert result['stdout'] == 'part'
    assert m.machine_exit_code(result) == 13

    payload = m.build_risky_receipt_payload(
        session=session, action='exec', transaction_id='tx5', receipt_id='rid',
        change_target='/tmp/z', change_description='Проверка',
        command_hash_value=m.command_hash('echo SECRET_TOKEN'), command_exit_code=0,
        previous_receipt_hash=None, timestamp_utc='2026-07-29T00:00:00Z',
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    assert 'SECRET_TOKEN' not in encoded
    receipt_hash = payload.pop('receipt_hash')
    assert receipt_hash == m.sha256_prefixed(m.canonical_json_bytes(payload))
finally:
    m.read_previous_receipt_hash = orig_read
    m.ensure_transaction_id_unused = orig_unused
    m.write_risky_receipt = orig_write
    m.execute_remote_command = orig_exec
    m.execute_sudo_command = orig_sudo

with tempfile.TemporaryDirectory() as tmp:
    env = dict(**__import__('os').environ, XDG_STATE_HOME=tmp)
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH), 'exec', '--json', '--risky', '--transaction-id', 'tx-local', 'true'],
        text=True, capture_output=True, env=env,
    )
    assert proc.returncode == 10, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stderr == '', proc.stderr
    data = json.loads(proc.stdout)
    assert data['operation_status'] == 'not_started', data
    assert data['command_status'] == 'not_started', data
    assert data['transaction_id'] == 'tx-local'

print('Все локальные проверки пройдены.')

# Совместимость версий протокола.
assert m.supports_operation_protocol('0.6.0')
assert m.supports_operation_protocol('0.6.9')
assert m.supports_operation_protocol('0.7.0')
assert not m.supports_operation_protocol('0.5.9')
assert not m.supports_operation_protocol('1.0.0')
assert not m.supports_operation_protocol('broken')

# Проверка self-hash и обнаружение подмены новой записи.
new_record = m.build_risky_receipt_payload(
    session=session, action='exec', transaction_id='hash-new', receipt_id='rid-new',
    change_target='/tmp/hash', change_description='Проверка hash',
    command_hash_value=m.command_hash('true'), command_exit_code=0,
    previous_receipt_hash=None, timestamp_utc='2026-07-29T00:00:00Z',
)
assert m.stored_receipt_hash(new_record) == new_record['receipt_hash']
tampered = dict(new_record)
tampered['change_description'] = 'Подменено'
try:
    m.stored_receipt_hash(tampered)
except m.RelayError:
    pass
else:
    raise AssertionError('Подмена receipt не обнаружена')

# Журнал 0.5.x допускается только как однократный legacy anchor.
legacy_record = {
    'timestamp_utc': '2026-07-23T00:00:00Z', 'tool': 'ssh_relay',
    'session': 'prod', 'target': 'donpedro@198.51.100.42:22',
    'action': 'exec', 'sudo': False, 'command': 'touch /tmp/legacy', 'status': 'done',
}
legacy_anchor = m.stored_receipt_hash(legacy_record)
assert legacy_anchor == m.sha256_prefixed(m.canonical_json_bytes(legacy_record))

# Команда записи receipt заранее ограничивает права и отклоняет symlink.
captured = {}
def capture_aux(client, command, **kwargs):
    captured.setdefault('commands', []).append(command)
    if command.startswith('tail '):
        return {'ok': True, 'stdout': json.dumps(new_record, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n', 'stderr': '', 'exit_code': 0}
    return {'ok': True, 'stdout': '', 'stderr': '', 'exit_code': 0}
orig_aux = m.execute_auxiliary_command
try:
    m.execute_auxiliary_command = capture_aux
    status, diagnostic = m.write_risky_receipt(
        object(), receipt_path='/tmp/secure-changes.jsonl', payload=new_record,
        sudo=False, timeout_seconds=10, sudo_password=None,
    )
    assert status == 'written', (status, diagnostic)
    write_command = captured['commands'][0]
    assert 'umask 077' in write_command
    assert 'chmod 600' in write_command
    assert ' -L ' in write_command
finally:
    m.execute_auxiliary_command = orig_aux

print('Расширенные проверки пройдены.')

# Непустой журнал с пустой последней строкой не должен обнулять hash-цепочку.
orig_aux = m.execute_auxiliary_command
try:
    m.execute_auxiliary_command = lambda *a, **k: {'ok': True, 'stdout': '\n', 'stderr': '', 'exit_code': 0}
    try:
        m.read_previous_receipt_hash(
            object(), receipt_path='/tmp/blank.jsonl', sudo=False,
            timeout_seconds=10, sudo_password=None,
        )
    except m.RelayError:
        pass
    else:
        raise AssertionError('Пустая последняя строка журнала не была отклонена')
finally:
    m.execute_auxiliary_command = orig_aux

# Session-файл нельзя удалять, если запрос daemon уже мог быть получен.
removed = []
orig_read_session = m.read_session
orig_request_daemon = m.request_daemon
orig_remove_session = m.remove_session_file
orig_existing_session_path = m.existing_session_file_path
try:
    m.read_session = lambda name: {
        'name': name, 'host': '198.51.100.42', 'port': 22, 'user': 'donpedro',
        'daemon_port': 12345, 'auth_token': 'token', 'pid': 1, 'version': '0.6.0',
    }
    m.request_daemon = lambda *a, **k: (_ for _ in ()).throw(
        m.DaemonRequestError('ответ потерян', request_sent=True, error_code='daemon_response_lost')
    )
    m.remove_session_file = lambda name, expected_token=None: removed.append(name)
    class ExistingPath:
        @staticmethod
        def exists():
            return True
    m.existing_session_file_path = lambda name: ExistingPath()
    assert m.status_one_session('prod', cleanup_stale=True) == 1
    assert removed == [], removed
    assert m.stop_one_session('prod') == 1
    assert removed == [], removed
    assert m.check_existing_session('prod') is True
    assert removed == [], removed
finally:
    m.read_session = orig_read_session
    m.request_daemon = orig_request_daemon
    m.remove_session_file = orig_remove_session
    m.existing_session_file_path = orig_existing_session_path

print('Проверки сохранения session-файла пройдены.')

# Повторный transaction_id блокируется до запуска основной команды.
orig_aux = m.execute_auxiliary_command
try:
    m.execute_auxiliary_command = lambda *a, **k: {'ok': True, 'stdout': '', 'stderr': '', 'exit_code': 5}
    try:
        m.ensure_transaction_id_unused(
            object(), receipt_path='/tmp/changes.jsonl', transaction_id='tx-existing',
            sudo=False, timeout_seconds=10, sudo_password=None,
        )
    except m.RelayError as exc:
        assert 'уже присутствует' in str(exc)
    else:
        raise AssertionError('Повторный transaction_id не был отклонён')
finally:
    m.execute_auxiliary_command = orig_aux

print('Проверка повторного transaction_id пройдена.')

# Duplicate preflight формирует стабильный machine error до команды.
orig_read = m.read_previous_receipt_hash
orig_unused = m.ensure_transaction_id_unused
orig_exec = m.execute_remote_command
try:
    m.read_previous_receipt_hash = lambda *a, **k: None
    m.ensure_transaction_id_unused = lambda *a, **k: (_ for _ in ()).throw(
        m.RelayError('transaction_id уже присутствует в risky receipt; команда не запускалась.')
    )
    executed = []
    m.execute_remote_command = lambda *a, **k: executed.append(True)
    duplicate = m.execute_command_operation(
        object(), session=session, action='exec', command='touch /tmp/duplicate', risky=True,
        receipt_path='/tmp/changes.jsonl', transaction_id='tx-existing',
        transaction_id_source='caller', change_target='/tmp/duplicate',
        change_description='Повтор', timeout_seconds=10, sudo_password=None,
    )
    assert duplicate['operation_status'] == 'not_started', duplicate
    assert duplicate['error_code'] == 'transaction_id_exists', duplicate
    assert executed == [], executed
finally:
    m.read_previous_receipt_hash = orig_read
    m.ensure_transaction_id_unused = orig_unused
    m.execute_remote_command = orig_exec

print('Machine error повторного transaction_id пройден.')
