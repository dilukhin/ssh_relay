"""Отказы, неоднозначная доставка и привязка свидетельств без удалённых изменений."""

import hashlib
import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssh_relay
import ssh_relay_sudo_jobs as jobs


def expected_identity():
    return {"schema_version": 1, "remote_host": "198.51.100.42", "remote_port": 22,
            "remote_user": "donpedro", "host_key_algorithm": "ssh-ed25519",
            "remote_host_key_sha256": "SHA256:" + "A" * 43, "trusted_known_hosts": True,
            "daemon_instance_id": str(uuid.uuid4()), "connection_generation": 1,
            "daemon_source_sha": "a" * 40}


def payload(command="exit 0", operation="start"):
    return {"schema_version": 1, "operation": operation, "job_id": str(uuid.uuid4()),
            "transaction_id": str(uuid.uuid4()), "command": command,
            "command_sha256": hashlib.sha256(command.encode()).hexdigest()}


class Channel:
    def __init__(self, *, nopasswd=False, wrong_password=False, lose=False):
        self.nopasswd = nopasswd
        self.wrong_password = wrong_password
        self.lose = lose
        self.output = bytearray()
        self.errors = bytearray()
        self.closed = False
        self.done = False
        self.sent = []

    def settimeout(self, value):
        pass

    def exec_command(self, command):
        self.command = command
        import shlex
        argv = shlex.split(command)
        self.prompt = argv[argv.index("-p") + 1].encode()
        self.errors.extend(self.prompt) if not self.nopasswd else self.output.extend(jobs.READY)

    def recv_ready(self):
        return bool(self.output)

    def recv_stderr_ready(self):
        return bool(self.errors)

    def recv(self, count):
        data = bytes(self.output[:count])
        del self.output[:count]
        return data

    def recv_stderr(self, count):
        data = bytes(self.errors[:count])
        del self.errors[:count]
        return data

    def sendall(self, data):
        self.sent.append(data)
        if data.startswith(b"{"):
            if self.lose:
                raise OSError("delivery lost")
            self.output.extend(b'{"schema_version":1,"state":"running"}')
            self.done = True
        elif self.wrong_password:
            self.errors.extend(self.prompt)
        else:
            self.output.extend(jobs.READY)

    def shutdown_write(self):
        pass

    def exit_status_ready(self):
        return self.done

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


def client_for(channel):
    client = mock.Mock()
    client.get_transport.return_value.open_session.return_value = channel
    return client


class ProtocolTests(unittest.TestCase):
    def test_password_never_in_argv_or_payload_and_no_cache_dependency(self):
        channel = Channel()
        secret = "sudo-fixture-secret-946"
        request = payload("printf 'root command bytes'")
        result = jobs.exchange(client_for(channel), secret, request)
        self.assertEqual(result["state"], "running")
        self.assertNotIn(secret, channel.command)
        self.assertNotIn(request["command"], channel.command)
        self.assertTrue(channel.command.startswith("sudo -k -S "))
        self.assertEqual(channel.sent[0], (secret + "\n").encode())
        self.assertEqual(json.loads(channel.sent[1]), request)
        self.assertNotIn(secret.encode(), channel.sent[1])
        self.assertTrue(channel.closed)

    def test_nopasswd_does_not_send_password_into_command_input(self):
        channel = Channel(nopasswd=True)
        self.assertEqual(jobs.exchange(client_for(channel), "secret", payload())["state"], "running")
        self.assertEqual(len(channel.sent), 1)
        self.assertTrue(channel.sent[0].startswith(b"{"))

    def test_wrong_password_is_not_started_no_command_sent(self):
        channel = Channel(wrong_password=True)
        result = jobs.exchange(client_for(channel), "bad", payload())
        self.assertEqual(result["state"], "not_started")
        self.assertEqual(len(channel.sent), 1)

    def test_partial_payload_delivery_is_unknown_and_never_repeated(self):
        channel = Channel(lose=True)
        client = client_for(channel)
        result = jobs.exchange(client, "secret", payload())
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(len(channel.sent), 2)
        client.get_transport.return_value.open_session.assert_called_once()
        client.get_transport.return_value.close.assert_called_once()

    def test_failed_channel_invalidates_stale_active_transport_without_retry(self):
        client = mock.Mock()
        transport = client.get_transport.return_value
        active = {"value": True}
        transport.is_active.side_effect = lambda: active["value"]
        transport.open_session.side_effect = OSError("зависший транспорт после сброса")
        transport.close.side_effect = lambda: active.update(value=False)
        result = jobs.exchange(client, "secret", payload())
        self.assertEqual(result["state"], "not_started")
        self.assertFalse(transport.is_active())
        transport.open_session.assert_called_once()
        transport.close.assert_called_once()

    def test_channel_open_failure_cannot_have_started(self):
        client = mock.Mock()
        client.get_transport.side_effect = OSError
        self.assertEqual(jobs.exchange(client, "secret", payload())["state"], "not_started")

    def test_close_failure_does_not_destroy_known_result(self):
        channel = Channel()
        channel.close = mock.Mock(side_effect=OSError)
        self.assertEqual(jobs.exchange(client_for(channel), "secret", payload())["state"], "running")

    def test_pinned_identity_checked_before_remote_mutation(self):
        expected = expected_identity()
        request = {"expected_verified_identity": expected, "sudo_job": payload()}
        with mock.patch.object(jobs, "exchange") as call, mock.patch.object(jobs, "source_sha", return_value="a" * 40):
            result = jobs.daemon_request(request, {**expected, "connection_generation": 2}, object(), "secret", True)
        call.assert_not_called()
        self.assertEqual(result["state"], "not_started")

    def test_disabled_sudo_jobs_cannot_mutate(self):
        with mock.patch.object(jobs, "exchange") as call:
            result = jobs.daemon_request({"sudo_job": payload()}, None, object(), "secret", False)
        call.assert_not_called()
        self.assertTrue(result["request_not_started"])

    def test_target_is_derived_from_verified_transport(self):
        expected = expected_identity()
        request = {"expected_verified_identity": expected, "sudo_job": {**payload(), "target": {"remote_host": "foreign"}}}
        with mock.patch.object(jobs, "exchange", return_value={"state": "running"}) as call, mock.patch.object(jobs, "source_sha", return_value="a" * 40):
            jobs.daemon_request(request, expected, object(), "secret", True)
        self.assertEqual(call.call_args.args[2]["target"]["remote_host"], expected["remote_host"])

    def test_invalid_payload_bounds_and_hash(self):
        valid = payload()
        for invalid in ({**valid, "schema_version": True}, {**valid, "job_id": "../../escape"}, {**valid, "command": "different"},
                        payload("x" * (jobs.MAX_COMMAND + 1)), payload("\x00"),
                        {**valid, "operation": "tail", "stream": "stdout", "max_bytes": 65537}):
            self.assertFalse(jobs.validate_payload(invalid))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.identity = expected_identity()
        self.path = Path(self.tmp.name) / "expected.json"
        self.path.write_text(json.dumps(self.identity), encoding="utf-8")
        self.request = payload()

    def tearDown(self):
        self.tmp.cleanup()

    def proof(self, phase='start', code=0):
        value = {key: self.request[key] for key in ('schema_version', 'job_id', 'transaction_id', 'command_sha256')}
        value.update(target={key: self.identity[key] for key in jobs.TARGET_FIELDS}, boot_id=str(uuid.uuid4()),
                     unit='ssh-relay-sudo-' + uuid.UUID(self.request['job_id']).hex + '.service',
                     phase=phase, invocation_id='a' * 32)
        if phase == 'completion':
            value['exit_code'] = code
        value['witness_sha256'] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return value

    def args(self, operation="start", *extra):
        arguments = ["sudo-job", operation, "--job-id", self.request["job_id"],
                     "--transaction-id", self.request["transaction_id"], "--expected-identity-file", str(self.path)]
        if operation == "start":
            arguments.append(self.request["command"])
        else:
            arguments.extend(["--command-sha256", self.request["command_sha256"]])
        return ssh_relay.build_parser().parse_args([*arguments, *extra])

    def run_cli(self, args, response, capability=1):
        status = {"sudo_job_schema_version": capability, "sudo_jobs_enabled": True, "verified_identity": self.identity}
        output = io.StringIO()
        with mock.patch.object(jobs, "source_sha", return_value="a" * 40), mock.patch.object(ssh_relay._core, "read_session", return_value={}), mock.patch.object(
            ssh_relay._core, "request_daemon", side_effect=[status, response]
        ) as call, redirect_stdout(output):
            code = args.handler(args)
        return code, json.loads(output.getvalue()), call

    def test_old_daemon_refused_before_start(self):
        code, result, call = self.run_cli(self.args(), {}, capability=0)
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "not_started")
        self.assertEqual(call.call_count, 1)

    def test_launch_success_only_reports_running(self):
        code, result, call = self.run_cli(self.args(), {"ok": True, "schema_version": 1, "state": "running", "verified_identity": self.identity, "start_witness": self.proof()})
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "running")
        self.assertNotIn("exit_code", result)
        self.assertEqual(call.call_count, 2)

    def test_lost_daemon_reply_is_unknown_not_retried(self):
        code, result, call = self.run_cli(self.args(), ssh_relay.RelayError("secret not exposed"))
        self.assertEqual(code, 3)
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(call.call_count, 2)

    def test_command_success_accounting_failure_not_disguised_as_not_started(self):
        response = {"ok": True, "schema_version": 1, "state": "succeeded", "exit_code": 0,
                    "accounting_status": "failed", "error_code": "completion_record_failed", "verified_identity": self.identity, "start_witness": self.proof()}
        code, result, _ = self.run_cli(self.args("status"), response)
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["exit_code"], 0)

    def test_corrupt_witness_rejected(self):
        response = {"ok": True, "schema_version": 1, "state": "succeeded", "exit_code": 0, "verified_identity": self.identity,
                    "completion_witness": {"job_id": str(uuid.uuid4()), "witness_sha256": "a" * 64}}
        code, result, _ = self.run_cli(self.args("status"), response)
        self.assertEqual(code, 3)
        self.assertEqual(result["error_code"], "witness_binding_mismatch")

    def test_terminal_success_without_evidence_is_unknown(self):
        response = {"ok": True, "schema_version": 1, "state": "succeeded", "exit_code": 0,
                    "verified_identity": self.identity}
        code, result, _ = self.run_cli(self.args("status"), response)
        self.assertEqual(code, 3)
        self.assertEqual(result["state"], "unknown")

    def test_correctly_hashed_wrong_phase_is_not_completion(self):
        response = {"ok": True, "schema_version": 1, "state": "succeeded", "exit_code": 0,
                    "verified_identity": self.identity, "completion_witness": self.proof('start')}
        code, result, _ = self.run_cli(self.args("status"), response)
        self.assertEqual(code, 3)
        self.assertEqual(result["error_code"], "witness_binding_mismatch")

    def test_running_without_start_witness_is_unknown(self):
        response = {"ok": True, "schema_version": 1, "state": "running", "verified_identity": self.identity}
        code, result, _ = self.run_cli(self.args(), response)
        self.assertEqual(code, 3)
        self.assertEqual(result["error_code"], "start_witness_missing")

    def test_timeout_never_sends_stop(self):
        args = self.args("wait", "--timeout", "1")
        response = {"ok": True, "schema_version": 1, "state": "running", "verified_identity": self.identity, "start_witness": self.proof()}
        with mock.patch.object(jobs.time, "monotonic", side_effect=[0, 2]):
            code, result, call = self.run_cli(args, response)
        self.assertEqual(code, 124)
        self.assertTrue(result["wait_timed_out"])
        self.assertEqual(call.call_args.kwargs["sudo_job"]["operation"], "status")

    def test_sudo_jobs_require_explicit_sudo(self):
        args = ssh_relay.build_parser().parse_args(["daemon", "--host", "198.51.100.42", "--enable-sudo-jobs"])
        with mock.patch.object(ssh_relay._core, "load_paramiko") as load:
            self.assertEqual(ssh_relay._core.daemon(args), 2)
        load.assert_not_called()


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0, "Нужны Linux и изолированная среда root")
class RemoteStorageTests(unittest.TestCase):
    def setUp(self):
        import ssh_relay_sudo_job_remote as remote
        self.remote = remote
        self.tmp = tempfile.TemporaryDirectory(prefix="relay-sudo-test-", dir="/var/lib")
        os.chmod(self.tmp.name, 0o700)
        self.root = Path(self.tmp.name)
        self.root_patch = mock.patch.object(remote, "ROOT", self.root)
        self.root_patch.start()
        self.request = payload()
        self.request["target"] = {key: expected_identity()[key] for key in jobs.TARGET_FIELDS}
        self.path = self.root / self.request["job_id"]
        self.path.mkdir(mode=0o700)
        self.metadata = {key: self.request[key] for key in ("schema_version", "job_id", "transaction_id", "command_sha256", "target")}
        self.metadata.update(unit="ssh-relay-sudo-" + uuid.UUID(self.request["job_id"]).hex + ".service", boot_id=remote.boot_id(), helper_sha256=hashlib.sha256(b"fixture").hexdigest())
        remote.write_json(self.path / "metadata.json", self.metadata)
        remote.atomic_write(self.path / "runner.py", b"fixture")

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def test_symlink_and_hardlink_records_rejected(self):
        target = self.path / "runner.py"
        link = self.path / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            self.remote.read_bytes(link)
        link.unlink()
        os.link(target, link)
        with self.assertRaises(self.remote.Refusal):
            self.remote.read_bytes(target)

    def test_group_writable_parent_rejected(self):
        os.chmod(self.root, 0o720)
        with self.assertRaises(self.remote.Refusal):
            self.remote.secure_directory(self.path)
        os.chmod(self.root, 0o700)

    def test_reboot_without_completion_is_unknown(self):
        self.metadata["boot_id"] = "old-boot"
        self.assertEqual(self.remote.observed(self.path, self.metadata)["state"], "unknown")

    def test_completion_survives_reboot_without_unit(self):
        proof = self.remote.witness(self.metadata, "completion", invocation_id="a" * 32, exit_code=7)
        self.remote.write_json(self.path / "completion.json", proof)
        with mock.patch.object(self.remote, "boot_id", return_value="new-boot"), mock.patch.object(self.remote, "properties") as props:
            result = self.remote.observed(self.path, self.metadata)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 7)
        props.assert_not_called()

    def test_wrong_invocation_refuses_stop_no_signal_sent(self):
        self.remote.write_json(self.path / "started.json", self.remote.witness(self.metadata, "start", invocation_id="a" * 32))
        with mock.patch.object(self.remote, "properties", return_value={"LoadState": "loaded", "InvocationID": "b" * 32, "SubState": "running"}), mock.patch.object(self.remote.subprocess, "run") as run:
            with self.assertRaises(self.remote.Refusal):
                self.remote.control({**self.request, "operation": "stop"})
        run.assert_not_called()

    def test_success_with_failed_witness_write_keeps_command_result(self):
        self.remote.write_json(self.path / "started.json", self.remote.witness(self.metadata, "start", invocation_id="a" * 32))
        properties = {"LoadState": "loaded", "InvocationID": "a" * 32, "SubState": "exited", "ExecMainCode": "1", "ExecMainStatus": "0"}
        with mock.patch.object(self.remote, "properties", return_value=properties):
            result = self.remote.observed(self.path, self.metadata)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["accounting_status"], "failed")

    def test_foreign_job_command_hash_refuses_access(self):
        with self.assertRaises(self.remote.Refusal):
            self.remote.control({**self.request, "operation": "status", "command_sha256": "b" * 64})

    def test_log_reads_bounded(self):
        self.remote.atomic_write(self.path / "stdout.log", b"abcdefghij")
        with mock.patch.object(self.remote, "observed", return_value={"state": "running"}):
            result = self.remote.control({**self.request, "operation": "tail", "stream": "stdout", "max_bytes": 4})
        self.assertEqual(result["log"], "ghij")

    def test_reserved_transaction_cannot_execute_again(self):
        with mock.patch.object(self.remote, "preflight"), mock.patch.object(self.remote.subprocess, "run") as run:
            with self.assertRaises(self.remote.Refusal):
                self.remote.start({**self.request, "job_id": str(uuid.uuid4())}, b"fixture")
        run.assert_not_called()

    def test_quota_preserves_all_old_evidence(self):
        with mock.patch.object(self.remote, "preflight"), mock.patch.object(self.remote, "MAX_RECORDS", 1):
            with self.assertRaises(self.remote.Refusal):
                self.remote.start(payload(), b"fixture")
        self.assertTrue((self.path / "metadata.json").is_file())

    def run_worker(self, command, *, fail_completion=False):
        self.metadata["command_sha256"] = hashlib.sha256(command.encode()).hexdigest()
        self.remote.write_json(self.path / "metadata.json", self.metadata)
        self.remote.write_json(self.path / "command.json", {"command": command})
        writer = self.remote.write_json

        def write(path, value):
            if fail_completion and path.name == "completion.json":
                raise OSError("искусственный отказ записи")
            return writer(path, value)

        with mock.patch.dict(os.environ, {"INVOCATION_ID": "a" * 32}), mock.patch.object(
            self.remote, "properties", return_value={"InvocationID": "a" * 32}
        ), mock.patch.object(self.remote, "cgroup_members", return_value=set()), mock.patch.object(
            self.remote, "write_json", side_effect=write
        ), mock.patch.object(self.remote.signal, "signal"):
            return self.remote.run_worker(self.path)

    def test_worker_preserves_exact_nonzero_and_separate_streams(self):
        code = self.run_worker("printf output; printf error >&2; exit 7")
        self.assertEqual(code, 7)
        self.assertEqual(self.remote.read_bytes(self.path / "stdout.log"), b"output")
        self.assertEqual(self.remote.read_bytes(self.path / "stderr.log"), b"error")
        result = self.remote.observed(self.path, self.metadata)
        self.assertEqual(result["completion_witness"]["exit_code"], 7)

    def test_worker_drains_overflow_without_growing_log(self):
        command = "/usr/bin/python3 -c 'import sys; sys.stdout.write(\"x\" * 1200000); sys.stderr.write(\"y\" * 1200000)'"
        self.assertEqual(self.run_worker(command), 0)
        for name in ("stdout", "stderr"):
            self.assertEqual((self.path / (name + ".log")).stat().st_size, self.remote.MAX_LOG)
        result = self.remote.observed(self.path, self.metadata)
        self.assertTrue(all(result["completion_witness"]["logs_truncated"].values()))

    def test_worker_accounting_failure_keeps_real_code(self):
        self.assertEqual(self.run_worker("exit 0", fail_completion=True), 0)
        self.assertFalse((self.path / "completion.json").exists())
        self.assertTrue((self.path / "started.json").exists())

    def test_worker_rejects_changed_command_before_spawn(self):
        self.remote.write_json(self.path / "command.json", {"command": "unexpected"})
        with mock.patch.dict(os.environ, {"INVOCATION_ID": "a" * 32}), mock.patch.object(
            self.remote, "properties", return_value={"InvocationID": "a" * 32}
        ), mock.patch.object(self.remote.subprocess, "Popen") as spawn:
            with self.assertRaises(self.remote.Refusal):
                self.remote.run_worker(self.path)
        spawn.assert_not_called()

    def test_unknown_identity_stop_does_not_signal(self):
        with mock.patch.object(self.remote, "observed", return_value={"state": "unknown"}), mock.patch.object(self.remote.subprocess, "run") as run:
            result = self.remote.control({**self.request, "operation": "stop"})
        self.assertEqual(result["error_code"], "stop_refused_unknown_identity")
        run.assert_not_called()

    def test_soft_stop_targets_bound_unit_without_kill9(self):
        with mock.patch.object(self.remote, "observed", return_value={"state": "running"}), mock.patch.object(self.remote.subprocess, "run") as run:
            result = self.remote.control({**self.request, "operation": "stop"})
        self.assertTrue(result["stop_requested"])
        self.assertEqual(run.call_args.args[0][-1], self.metadata["unit"])
        self.assertIn("--signal=SIGTERM", run.call_args.args[0])

    def test_start_reserves_record_before_launch_and_lost_result_remains_unknown(self):
        import subprocess
        # Удаляем только искусственный объект, чтобы новая транзакция могла начать работу.
        import shutil
        shutil.rmtree(self.path)
        with mock.patch.object(self.remote, "preflight"), mock.patch.object(self.remote, "properties", return_value={"LoadState": "not-found"}), mock.patch.object(
            self.remote.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemd-run", 10)
        ) as run:
            result = self.remote.start(self.request, b"fixture")
        self.assertEqual(result["state"], "unknown")
        self.assertTrue((self.path / "metadata.json").exists())
        self.assertEqual(self.remote.read_json(self.path / "command.json")["command"], self.request["command"])
        self.assertIn("--property=SendSIGKILL=no", run.call_args.args[0])

    def test_duplicate_live_unit_refuses_before_state_creation(self):
        request = {**self.request, "job_id": str(uuid.uuid4()), "transaction_id": str(uuid.uuid4())}
        with mock.patch.object(self.remote, "preflight"), mock.patch.object(self.remote, "properties", return_value={"LoadState": "loaded"}):
            with self.assertRaises(self.remote.Refusal):
                self.remote.start(request, b"fixture")
        self.assertFalse((self.root / request["job_id"]).exists())


if __name__ == "__main__":
    unittest.main()
