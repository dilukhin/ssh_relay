"""Настоящий Windows-клиент → гостевая Ubuntu; QEMU, перезагрузки и отказ диска."""

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ssh_relay
import test_sudo_job_e2e as base
from ci.ubuntu_vm import PASSWORD, UbuntuVM

PROJECT = Path(__file__).resolve().parents[1]
ENABLED = (os.environ.get("SSH_RELAY_SUDO_JOB_VM_E2E") == "1" and
           os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "win32")


@unittest.skipUnless(ENABLED, "Требуется отдельный Windows runner с одноразовой Ubuntu в QEMU")
class WindowsUbuntuE2E(base.SudoJobE2E):
    # Переопределяется Linux-only условие родительского набора.
    __unittest_skip__ = not ENABLED

    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ["RUNNER_TEMP"]) / "relay-vm-e2e"
        cls.root.mkdir(exist_ok=True)
        cls.vm = UbuntuVM(cls.root / "vm", os.environ["SSH_RELAY_VM_RELEASE"])
        cls.process = None
        cls.transcripts = []
        try:
            cls.vm.start()
            cls.port = cls.vm.port
            cls.known = cls.vm.known
            cls.state = cls.root / "state"
            cls.state.mkdir(exist_ok=True)
            cls.env = {**os.environ, "LOCALAPPDATA": str(cls.state),
                       "SSH_RELAY_SUDO_JOB_TEST_PASSWORD": PASSWORD,
                       "SSH_RELAY_SUDO_JOB_TEST_PORT": str(cls.port),
                       "SSH_RELAY_SUDO_JOB_TEST_KNOWN_HOSTS": str(cls.known), "PYTHONIOENCODING": "utf-8"}
            cls.client = cls.vm.connect("relayci")
            _stdin, stdout, _stderr = cls.client.exec_command("sudo -k -n true")
            if stdout.channel.recv_exit_status() == 0:
                raise AssertionError("Испытательный sudo обязан требовать пароль")
            cls.start_daemon()
            print(f"Клиент: {platform.platform()}; сервер: " + cls.vm.command("uname -sr; cat /etc/os-release"), flush=True)
        except BaseException:
            cls.stop_daemon()
            cls.vm.close()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.stop_daemon()
            cls.client.close()
            for transcript in cls.transcripts:
                if PASSWORD.encode() in transcript:
                    raise AssertionError("Испытательный пароль попал в вывод daemon")
        finally:
            cls.vm.close()

    def admin(self, command):
        return self.vm.command(command, root=True)

    def new_client(self):
        return self.vm.connect("relayci")

    def reconnect(self):
        old = self.identity["connection_generation"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            value = ssh_relay._core.request_daemon(self.session, "status", response_timeout=5).get("verified_identity")
            if value and value["connection_generation"] > old:
                type(self).identity = value
                self.pin.write_text(json.dumps(value))
                self.client.close()
                type(self).client = self.new_client()
                return
            time.sleep(1)
        self.fail("Daemon не восстановил SSH с новым поколением")

    def test_07_real_ssh_disconnect_does_not_stop_root_job(self):
        request, _ = self.launch("sleep 20; exit 0")
        self.admin("pkill -TERM -u relayci")
        self.reconnect()
        result = self.wait(request)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["completion_witness"]["job_id"], request["job_id"])

    def test_11_abrupt_reboot_completed_and_unfinished(self):
        completed, _ = self.launch("exit 0")
        proof = self.wait(completed)["completion_witness"]
        marker = "/var/lib/relay-ci-" + uuid.uuid4().hex
        running, _ = self.launch(f"printf 'start\\n' >> {marker}; sleep 600")
        self.assertEqual(self.status(running)["state"], "running")
        # Отметка полезной команды нужна отдельно от свидетельства наблюдателя.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.admin(f"test -f {marker} && cat {marker} || true").strip() == "start":
                break
            time.sleep(0.2)
        else:
            self.fail("Полезная команда не начала работу")
        self.admin("sync")
        new_boot = self.vm.reset()
        self.reconnect()
        self.assertNotEqual(new_boot, proof["boot_id"])
        self.assertEqual(self.status(completed)["completion_witness"], proof)
        unknown = self.status(running)
        self.assertEqual(unknown["state"], "unknown", unknown)
        self.assertEqual(unknown["error_code"], "remote_reboot_without_completion")
        self.assertEqual(self.rpc(running)["state"], "not_started")
        self.assertEqual(self.admin(f"cat {marker}").strip(), "start")
        self.assertEqual(self.status(running, "stop")["state"], "unknown")

    def test_12_normal_reboot_preserves_completed_witness(self):
        request, _ = self.launch("exit 0")
        proof = self.wait(request)["completion_witness"]
        self.vm.reboot()
        self.reconnect()
        self.assertEqual(self.status(request)["completion_witness"], proof)

    def test_13_native_windows_cli_and_wait_timeout(self):
        command = "sleep 15; printf 'finished\\n'"
        request = {"job_id": str(uuid.uuid4()), "transaction_id": str(uuid.uuid4()),
                   "command_sha256": hashlib.sha256(command.encode()).hexdigest()}

        def cli(operation, extra):
            args = [sys.executable, str(PROJECT / "ssh_relay.py"), "sudo-job", operation,
                    "--name", "ci-sudo-job", "--job-id", request["job_id"],
                    "--transaction-id", request["transaction_id"], "--expected-identity-file", str(self.pin)]
            if operation != "start":
                args += ["--command-sha256", request["command_sha256"]]
            process = subprocess.run(args + extra, cwd=PROJECT, env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            self.assertNotIn(PASSWORD.encode(), process.stdout + process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(process.returncode, result["process_exit_code"])
            return process.returncode, result

        code, result = cli("start", [command])
        self.assertEqual(code, 0, result)
        code, result = cli("wait", ["--timeout", "1", "--poll-interval", "1"])
        self.assertEqual(code, 124, result)
        self.assertEqual(result["state"], "running")
        code, result = cli("wait", ["--timeout", "60", "--poll-interval", "1"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["state"], "succeeded")
        code, result = cli("tail", ["--bytes", "64"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["log"], "finished\n")


if __name__ == "__main__":
    if not ENABLED:
        raise SystemExit("Этот набор запускается только выделенным заданием Windows CI")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WindowsUbuntuE2E)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"source_sha": os.environ["SSH_RELAY_SOURCE_SHA"], "client_os": platform.platform(),
              "guest": os.environ["SSH_RELAY_VM_RELEASE"], "tests_run": result.testsRun,
              "failures": [case.id() for case, _ in result.failures],
              "errors": [case.id() for case, _ in result.errors],
              "skipped": [case.id() for case, _ in result.skipped],
              "elapsed_seconds": round(time.monotonic() - started, 1), "successful": result.wasSuccessful()}
    destination = Path(os.environ["RUNNER_TEMP"]) / "relay-vm-report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result.wasSuccessful() and result.testsRun == 13 and not result.skipped else 1)
