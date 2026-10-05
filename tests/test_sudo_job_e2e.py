"""Настоящие SSH → sudo с паролем → systemd, только на одноразовом runner Ubuntu.

Запускать после подготовки пользователя relayci в sudo-jobs.yml. В обычном
discover тесты пропускаются. Пользовательские VPS этот набор не использует.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paramiko
import ssh_relay
import ssh_relay_sudo_jobs as jobs

PROJECT = Path(__file__).resolve().parents[1]
PASSWORD = "relay-ci-artificial-password-52"


@unittest.skipUnless(os.environ.get("SSH_RELAY_SUDO_JOB_E2E") == "1" and
                     os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux",
                     "Требуется отдельный одноразовый runner Ubuntu с systemd")
class SudoJobE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="relay-root-e2e-")
        cls.root = Path(cls.tmp.name)
        cls.state = cls.root / "state"
        cls.state.mkdir()
        key = cls.root / "host-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        config = cls.root / "sshd_config"
        config.write_text(f"Port {cls.port}\nListenAddress 127.0.0.1\nHostKey {key}\n"
                          "PasswordAuthentication yes\nKbdInteractiveAuthentication no\nUsePAM no\n"
                          "PermitRootLogin no\nAllowUsers relayci\nLogLevel ERROR\n"
                          f"PidFile {cls.root / 'sshd.pid'}\n", encoding="utf-8")
        cls.sshd_log = (cls.root / "sshd.log").open("wb")
        cls.sshd = subprocess.Popen(["sudo", "-n", "/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
                                    stdout=cls.sshd_log, stderr=cls.sshd_log)
        cls.known = cls.root / "known_hosts"
        pub = key.with_suffix(".pub").read_text().split()
        cls.known.write_text(f"[127.0.0.1]:{cls.port} {pub[0]} {pub[1]}\n")
        for _attempt in range(100):
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        cls.env = {**os.environ, "XDG_STATE_HOME": str(cls.state),
                   "SSH_RELAY_SUDO_JOB_TEST_PASSWORD": PASSWORD, "SSH_RELAY_SUDO_JOB_TEST_PORT": str(cls.port),
                   "SSH_RELAY_SUDO_JOB_TEST_KNOWN_HOSTS": str(cls.known), "PYTHONIOENCODING": "utf-8"}
        cls.client = paramiko.SSHClient()
        cls.client.load_host_keys(str(cls.known))
        cls.client.connect("127.0.0.1", port=cls.port, username="relayci", password=PASSWORD,
                           allow_agent=False, look_for_keys=False)
        _stdin, stdout, _stderr = cls.client.exec_command("sudo -k -n true")
        if stdout.channel.recv_exit_status() == 0:
            raise AssertionError("Испытательный пользователь обязан требовать пароль sudo")
        cls.process = None
        cls.transcripts = []
        cls.start_daemon()

    @classmethod
    def start_daemon(cls):
        cls.process = subprocess.Popen([sys.executable, "-u", str(PROJECT / "tests/daemon_sudo_job_runner.py")],
                                       cwd=PROJECT, env=cls.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        path = cls.state / "ssh_relay/sessions/ci-sudo-job.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError(cls.process.communicate())
            try:
                session = json.loads(path.read_text())
                status = ssh_relay._core.request_daemon(session, "status", response_timeout=2)
                if status.get("sudo_jobs_enabled") and status.get("verified_identity"):
                    cls.session = session
                    cls.identity = status["verified_identity"]
                    cls.pin = cls.root / "expected.json"
                    cls.pin.write_text(json.dumps(cls.identity))
                    return
            except (OSError, ValueError, ssh_relay.RelayError):
                pass
            time.sleep(0.05)
        raise AssertionError("Не дождались активного daemon")

    @classmethod
    def stop_daemon(cls):
        if cls.process is None:
            return
        if cls.process.poll() is None:
            ssh_relay._core.request_daemon(cls.session, "stop", response_timeout=3)
        cls.transcripts.extend(cls.process.communicate(timeout=10))
        cls.process = None

    @classmethod
    def tearDownClass(cls):
        try:
            cls.stop_daemon()
            cls.client.close()
            cls.sshd.terminate()
            try:
                cls.sshd.wait(timeout=5)
            except subprocess.TimeoutExpired:
                subprocess.run(["sudo", "-n", "kill", "-TERM", str(cls.sshd.pid)], check=False)
            cls.sshd_log.close()
            for transcript in cls.transcripts:
                if PASSWORD.encode() in transcript:
                    raise AssertionError("Пароль sudo попал в вывод daemon")
        finally:
            cls.tmp.cleanup()

    def launch(self, command):
        request = {"schema_version": 1, "operation": "start", "job_id": str(uuid.uuid4()),
                   "transaction_id": str(uuid.uuid4()), "command": command,
                   "command_sha256": hashlib.sha256(command.encode()).hexdigest()}
        result = self.rpc(request)
        self.assertIn(result["state"], ("running", "succeeded", "failed"), result)
        return request, result

    def rpc(self, request):
        return ssh_relay._core.request_daemon(self.session, "sudo_job", response_timeout=35,
                                             sudo_job=request, expected_verified_identity=self.identity)

    def status(self, request, operation="status", **extra):
        return self.rpc({**request, "operation": operation, **extra})

    def wait(self, request, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.status(request)
            if result["state"] != "running":
                return result
            time.sleep(0.1)
        self.fail("Задание не завершилось за испытательный срок")

    def test_01_real_package_install_and_dpkg_audit(self):
        request, _ = self.launch("DEBIAN_FRONTEND=noninteractive apt-get -y install hello")
        result = self.wait(request, 180)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["completion_witness"]["command_sha256"], request["command_sha256"])
        check = ssh_relay._core.request_daemon(self.session, "exec", command="dpkg-query -W -f='${Status}' hello; dpkg --audit")
        self.assertEqual(check["exit_code"], 0)
        self.assertEqual(check["stdout"].strip(), "install ok installed")
        # Искусственный пароль не должен встречаться даже в защищённых файлах root.
        check = ssh_relay._core.request_daemon(self.session, "sudo_exec",
                    command=f"grep -R -l -- {PASSWORD} /var/lib/ssh-relay-sudo-jobs", risky=False)
        self.assertEqual(check["exit_code"], 1)
        self.assertEqual(check["stdout"], "")

    def test_02_nonzero_and_bounded_output(self):
        request, _ = self.launch("/usr/bin/python3 -c 'import sys; sys.stdout.write(\"x\" * 1200000); sys.stderr.write(\"bad\\n\")'; exit 7")
        result = self.wait(request)
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["completion_witness"]["logs_truncated"]["stdout"])
        tail = self.status(request, "tail", stream="stdout", max_bytes=1024)
        self.assertEqual(len(tail["log"]), 1024)
        self.assertEqual(tail["log"], "x" * 1024)

    def test_03_daemon_restart_preserves_running_and_result(self):
        request, _ = self.launch("sleep 4; exit 0")
        self.stop_daemon()
        self.start_daemon()
        result = self.wait(request)
        self.assertEqual(result["state"], "succeeded", result)
        duplicate = self.rpc(request)
        self.assertEqual(duplicate["state"], "not_started", duplicate)
        self.assertEqual(self.status(request)["completion_witness"], result["completion_witness"])

    def test_04_lost_launch_reply_read_back_without_relaunch(self):
        request = {"schema_version": 1, "operation": "start", "job_id": str(uuid.uuid4()),
                   "transaction_id": str(uuid.uuid4()), "command": "sleep 1; exit 0",
                   "command_sha256": hashlib.sha256(b"sleep 1; exit 0").hexdigest()}
        with socket.create_connection(("127.0.0.1", self.session["daemon_port"])) as conn:
            ssh_relay._core.send_message(conn, {"action": "sudo_job", "auth_token": self.session["auth_token"],
                                              "sudo_job": request, "expected_verified_identity": self.identity})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.status(request)
            if result["state"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(result["state"], "succeeded", result)

    def test_05_soft_stop_and_foreign_binding(self):
        request, _ = self.launch("sleep 30")
        foreign = self.status({**request, "command_sha256": "b" * 64}, "stop")
        self.assertEqual(foreign["state"], "unknown")
        self.assertEqual(self.status(request)["state"], "running")
        stopped = self.status(request, "stop")
        self.assertTrue(stopped.get("stop_requested"), stopped)
        result = self.wait(request)
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["exit_code"], 143)

    def test_06_wrong_password_before_launch(self):
        request = {"schema_version": 1, "operation": "start", "job_id": str(uuid.uuid4()),
                   "transaction_id": str(uuid.uuid4()), "command": "exit 0",
                   "command_sha256": hashlib.sha256(b"exit 0").hexdigest(),
                   "target": {key: self.identity[key] for key in jobs.TARGET_FIELDS}}
        result = jobs.exchange(self.client, "wrong-fixture", request)
        self.assertEqual(result["state"], "not_started", result)
        self.assertEqual(self.status(request)["error_code"], "job_not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
