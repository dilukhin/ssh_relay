from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'ssh_relay.py'

for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, 'reconfigure', None)
    if callable(reconfigure):
        reconfigure(encoding='utf-8', errors='replace')

with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'cp1252'
    if os.name == 'nt':
        env['LOCALAPPDATA'] = tmp
    else:
        env['XDG_STATE_HOME'] = tmp

    proc = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            'exec',
            '--json',
            '--risky',
            '--transaction-id',
            'tx-utf8-regression',
            'true',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 10, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stderr == b'', f"stderr hex={proc.stderr.hex()} repr={proc.stderr!r}"
    decoded = proc.stdout.decode('utf-8')
    result = json.loads(decoded)
    assert result['operation_status'] == 'not_started', result
    assert result['transaction_id'] == 'tx-utf8-regression', result
    assert isinstance(result['error_message'], str) and result['error_message'], result
    assert 'Сессия' in result['error_message'], result

print('Проверка UTF-8 вывода CLI пройдена.')
