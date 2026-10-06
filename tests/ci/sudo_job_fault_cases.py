"""Настоящие конкурентные запросы, повреждённые свидетельства и ENOSPC в CI."""

import concurrent.futures
import errno
import hashlib
import json
import shlex
import threading
import time
import uuid

import ssh_relay_sudo_jobs as jobs
from ci.ubuntu_vm import PASSWORD

ROOT = "/var/lib/ssh-relay-sudo-jobs"


class SudoJobFaultCases:
    def test_08_concurrent_reservation_runs_command_once(self):
        marker = "/run/relay-ci-" + uuid.uuid4().hex
        command = f"printf 'start\\n' >> {marker}; sleep 2"
        request = {"schema_version": 1, "operation": "start", "job_id": str(uuid.uuid4()),
                   "transaction_id": str(uuid.uuid4()), "command": command,
                   "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                   "target": {key: self.identity[key] for key in jobs.TARGET_FIELDS}}
        barrier = threading.Barrier(2)

        def launch_independent():
            client = self.new_client()
            try:
                barrier.wait(timeout=15)
                return jobs.exchange(client, PASSWORD, request)
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(launch_independent) for _ in range(2)]
            results = [future.result(timeout=60) for future in futures]
        self.assertEqual(sum(value["state"] == "not_started" for value in results), 1, results)
        self.assertEqual(self.wait(request)["state"], "succeeded")
        self.assertEqual(self.admin(f"cat {marker}").strip(), "start")
        self.assertEqual(self.rpc(request)["state"], "not_started")
        self.assertEqual(self.admin(f"cat {marker}").strip(), "start")

    def test_09_ordinary_user_and_corrupt_completion(self):
        request, _ = self.launch("exit 0")
        original = self.wait(request)
        path = ROOT + "/" + request["job_id"]
        client = self.new_client()
        try:
            for command in (f"cat {path}/metadata.json", f"printf hacked >> {path}/command.json"):
                _stdin, stdout, _stderr = client.exec_command(command)
                self.assertNotEqual(stdout.channel.recv_exit_status(), 0)
        finally:
            client.close()
        self.admin(f"cp {path}/completion.json {path}/saved.json; printf '{{}}' > {path}/completion.json")
        try:
            self.assertEqual(self.status(request)["state"], "unknown")
            self.assertEqual(self.status(request, "stop")["state"], "unknown")
            self.admin(f"rm {path}/completion.json; ln -s /etc/passwd {path}/completion.json")
            self.assertEqual(self.status(request)["state"], "unknown")
        finally:
            self.admin(f"rm -f {path}/completion.json; mv {path}/saved.json {path}/completion.json")
        self.assertEqual(self.status(request)["completion_witness"], original["completion_witness"])

    def test_10_real_enospc_preserves_success_without_relaunch(self):
        # Маленький отдельный tmpfs скрывает только завершённые записи этой серии.
        # Системный диск не заполняется. Нет apt/dpkg и фоновых полезных работ.
        marker = "/run/relay-ci-" + uuid.uuid4().hex
        release = marker + "-release"
        mounted = False
        request = None
        try:
            self.admin(f"mkdir -p {ROOT}; chmod 700 {ROOT}; mount -t tmpfs -o size=1m,mode=0700 tmpfs {ROOT}")
            mounted = True
            request, _ = self.launch(f"printf 'start\\n' >> {marker}; while [ ! -e {release} ]; do sleep 0.2; done")
            self.assertEqual(self.status(request)["state"], "running")
            path = ROOT + "/" + request["job_id"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.admin(f"test -f {path}/stdout.log && test -f {path}/stderr.log && echo ready || true").strip():
                    break
                time.sleep(0.1)
            else:
                self.fail("Журналы задания не готовы")
            filler = (
                "import errno\n"
                f"f=open({ROOT + '/filler'!r},'wb',buffering=0)\n"
                "try:\n"
                " while True: f.write(b'x'*4096)\n"
                "except OSError as e:\n"
                " assert e.errno==errno.ENOSPC\n"
                " print('ENOSPC')\n"
                "finally: f.close()\n"
            )
            self.assertEqual(self.admin("python3 -c " + shlex.quote(filler)).strip(), "ENOSPC")
            self.admin(f"touch {release}")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                result = self.status(request)
                if result["state"] != "running":
                    break
                time.sleep(0.2)
            self.assertEqual(result["state"], "succeeded", result)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["accounting_status"], "failed")
            self.assertIsNone(result.get("completion_witness"))
            self.assertEqual(self.admin(f"cat {marker}").strip(), "start")
            # Удаляем только искусственный заполнитель, затем проверяем UUID.
            self.admin(f"rm {ROOT}/filler")
            self.assertEqual(self.rpc(request)["state"], "not_started")
            self.assertEqual(self.admin(f"cat {marker}").strip(), "start")
        finally:
            if mounted:
                self.admin(f"touch {release}; rm -f {ROOT}/filler")
                if request is not None:
                    self.wait(request, timeout=30)
                self.admin(f"umount {ROOT}")
