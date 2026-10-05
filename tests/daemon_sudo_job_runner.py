"""Только испытательный запуск: пароль искусственного пользователя берётся из окружения CI."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssh_relay

ssh_relay._core.getpass.getpass = lambda _prompt: os.environ["SSH_RELAY_SUDO_JOB_TEST_PASSWORD"]
arguments = ["daemon", "--name", "ci-sudo-job", "--host", "127.0.0.1",
             "--port", os.environ["SSH_RELAY_SUDO_JOB_TEST_PORT"], "--user", "relayci",
             "--known-hosts", os.environ["SSH_RELAY_SUDO_JOB_TEST_KNOWN_HOSTS"],
             "--enable-sudo", "--enable-sudo-jobs"]
raise SystemExit(ssh_relay.build_parser().parse_args(arguments).handler(
    ssh_relay.build_parser().parse_args(arguments)))
