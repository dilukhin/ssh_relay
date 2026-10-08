"""Контракт маршрута, проверка обоих ключей и настоящий обратный SSH-туннель."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ssh_relay
import ssh_relay_core as core
import ssh_relay_route as route


class RouteContractTests(unittest.TestCase):
    def arguments(self, **extra):
        return SimpleNamespace(via_host="via", via_user="via-user", via_target_port=2222, **extra)

    def test_validation_before_network_and_credentials(self):
        self.assertIsNone(route.Route.from_args(SimpleNamespace()))
        for args in (SimpleNamespace(via_user="user"), SimpleNamespace(via_host="via"),
                     self.arguments(via_port=0), self.arguments(via_target_host=""),
                     self.arguments(via_ask_key_passphrase=True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                route.Route.from_args(args)
        parsed = ssh_relay.build_parser().parse_args(["daemon", "--host", "pc", "--via-host", "via"])
        with patch.object(core, "load_paramiko") as load, patch.object(core.getpass, "getpass") as ask:
            with patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(2, core.daemon(parsed))
            load.assert_not_called()
            ask.assert_not_called()

    def test_exact_channel_target_identity_and_cleanup(self):
        via, target, channel = Mock(), Mock(), Mock()
        module = Mock()
        module.SSHClient.side_effect = [via, target]
        via.get_transport.return_value.open_channel.return_value = channel
        config = route.Route.from_args(self.arguments())
        client = route.open_client(module, config, target_host="unique-pc.invalid", target_port=22,
            target_user="pc-user", target_known_hosts="pc-known", target_identity="pc-key",
            target_password=None, target_passphrase="pc-passphrase", via_identity="via-key",
            via_password=None, via_passphrase="via-passphrase", keepalive=30)
        via.get_transport.return_value.open_channel.assert_called_once_with(
            "direct-tcpip", ("127.0.0.1", 2222), ("127.0.0.1", 0), timeout=10)
        self.assertEqual("unique-pc.invalid", target.connect.call_args.args[0])
        self.assertIs(channel, target.connect.call_args.kwargs["sock"])
        self.assertEqual("pc-key", target.connect.call_args.kwargs["key_filename"])
        self.assertEqual("via-key", via.connect.call_args.kwargs["key_filename"])
        self.assertFalse(target.connect.call_args.kwargs["allow_agent"])
        target.get_transport.return_value.is_active.return_value = True
        via.get_transport.return_value.is_active.return_value = False
        self.assertFalse(client.get_transport().is_active())
        self.assertIs(target.get_transport.return_value.get_remote_server_key(),
                      client.get_transport().get_remote_server_key())
        target.close.side_effect = OSError("synthetic")
        client.close()
        channel.close.assert_called_once()
        via.close.assert_called_once()
        self.assertNotIn("passphrase", json.dumps(config.public()))

    def test_failed_second_handshake_closes_all_owned_resources(self):
        module, via, target, channel = Mock(), Mock(), Mock(), Mock()
        module.SSHClient.side_effect = [via, target]
        via.get_transport.return_value.open_channel.return_value = channel
        target.connect.side_effect = ValueError("synthetic key rejection")
        with self.assertRaises(ValueError):
            route.open_client(module, route.Route.from_args(self.arguments()), target_host="pc",
                target_port=22, target_user="pc-user", target_known_hosts=None, target_identity=None,
                target_password="pc-test-password", target_passphrase=None, via_identity=None,
                via_password="via-test-password", via_passphrase=None, keepalive=30)
        for resource in (via, target, channel):
            resource.close.assert_called_once()

    def test_detach_cannot_hide_intermediate_password_prompt(self):
        args = ssh_relay.build_parser().parse_args([
            "daemon", "--host", "pc", "-i", "pc-key", "--detach",
            "--via-host", "via", "--via-user", "user", "--via-target-port", "2222"])
        with patch("sys.stderr", new=io.StringIO()), patch.object(core.subprocess, "Popen") as spawn:
            self.assertEqual(2, core.daemon(args))
        spawn.assert_not_called()


# Тесты реального SSH используют уже имеющуюся обязательную зависимость Paramiko.
try:
    import paramiko
except ImportError:
    paramiko = None

if paramiko is not None:
    import test_real_ssh_integration as direct_ssh_tests
    from localhost_ssh_server import LoopbackSSHServer
    from reverse_ssh_fixture import ReverseSSHFixture

    class ReverseSSHIntegrationTests(direct_ssh_tests.RealSSHIntegrationTests):
        """Весь прежний контракт exec/receipts/reconnect повторяется через reverse SSH."""
        def setUp(self):
            super().setUp()
            self.server.stop()
            self.remote_root = Path(self.tmp.name) / "remote"
            self.server = LoopbackSSHServer(self.host_key, sftp_root=self.remote_root)
            self.server.start()
            self.via_key = paramiko.RSAKey.generate(2048)
            self.via_hosts = Path(self.tmp.name) / "via-known-hosts"
            self.forward = ReverseSSHFixture(self.via_key, self.server.port)
            self.forward.start(self.via_hosts)
            keys = paramiko.HostKeys()
            keys.add("unique-pc.invalid", self.host_key.get_name(), self.host_key)
            keys.save(str(self.known_hosts))
            self.overrides.update({
                "SSH_RELAY_REAL_VIA_PORT": str(self.forward.port),
                "SSH_RELAY_REAL_VIA_FORWARD_PORT": str(self.forward.forward_port),
                "SSH_RELAY_REAL_VIA_KNOWN_HOSTS": str(self.via_hosts),
            })

        def tearDown(self):
            try:
                super().tearDown()
            finally:
                self.forward.stop()

        # Базовые проверки специфичных 127.0.0.1/динамического порта имеют отдельные
        # реализации для логического конечного узла, остальные наследуются.
        def test_machine_risky_reports_key_from_active_verified_connection(self):
            self.start_daemon()
            result = self.exec("test:real-success")
            self.assertTrue(result["ok"], result)
            identity = self.status()["verified_identity"]
            self.assertEqual("unique-pc.invalid", identity["remote_host"])
            self.assertEqual(22, identity["remote_port"])
            fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(self.host_key.asbytes()).digest()).decode().rstrip("=")
            self.assertEqual(fingerprint, identity["remote_host_key_sha256"])
            intermediary = self.status()["verified_intermediate_identity"]
            self.assertNotEqual(fingerprint, intermediary["remote_host_key_sha256"])
            self.assertEqual(self.forward.port, intermediary["remote_port"])

        def test_unknown_or_wrong_host_key_is_rejected_before_authentication(self):
            # Для обеих проверок отдельно подтверждаем отсутствие target exec.
            for hop in ("via", "target"):
                for wrong in (False, True):
                    with self.subTest(hop=hop, wrong=wrong):
                        path = self.via_hosts if hop == "via" else self.known_hosts
                        original = path.read_text()
                        keys = paramiko.HostKeys()
                        if wrong:
                            alias = f"[127.0.0.1]:{self.forward.port}" if hop == "via" else "unique-pc.invalid"
                            keys.add(alias, self.wrong_host_key.get_name(), self.wrong_host_key)
                        keys.save(str(path))
                        process = self.start_daemon(expect_session=False)
                        stdout, stderr = process.communicate(timeout=15)
                        self.assertEqual(1, process.returncode, (stdout, stderr))
                        self.assertEqual([], self.server.commands)
                        path.write_text(original)
                        self.process = None

        def test_forward_blocked_or_reverse_tunnel_missing_fails_without_fallback(self):
            for blocked in (True, False):
                with self.subTest(blocked=blocked):
                    self.forward.block_forward = blocked
                    if not blocked:
                        self.forward.stop_reverse()
                    process = self.start_daemon(expect_session=False)
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(1, process.returncode, (stdout, stderr))
                    self.assertIn("обратный порт", stderr)
                    self.assertEqual([], self.server.commands)
                    self.process = None

        def test_reconnect_both_hops_and_stop_preserves_independent_tunnel(self):
            self.start_daemon()
            before = self.status()["verified_identity"]
            self.forward.drop_relay_connections()
            import time
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                after = self.status()
                if (after.get("verified_identity") or {}).get("connection_generation", 0) > before["connection_generation"]:
                    break
                time.sleep(0.05)
            self.assertGreater(after["verified_identity"]["connection_generation"], before["connection_generation"])
            self.assertTrue(self.exec("test:real-success")["ok"])
            self.stop_daemon()
            self.assertTrue(self.forward.reverse_client.get_transport().is_active())
            self.start_daemon()
            self.assertTrue(self.exec("test:real-success")["ok"])

        def test_sftp_over_reverse_ssh(self):
            self.start_daemon()
            # Передача идёт через тот же checked route; target DNS имени .invalid невозможен.
            client = route.open_client(paramiko, route.Route.from_args(SimpleNamespace(
                via_host="127.0.0.1", via_port=self.forward.port, via_user="via-user",
                via_target_port=self.forward.forward_port, via_known_hosts=str(self.via_hosts))),
                target_host="unique-pc.invalid", target_port=22, target_user="donpedro",
                target_known_hosts=str(self.known_hosts), target_identity=None,
                target_password="relay-test-password", target_passphrase=None,
                via_identity=None, via_password="via-test-password", via_passphrase=None, keepalive=30)
            try:
                with client.open_sftp() as sftp:
                    with sftp.open("/file.bin", "wb") as stream:
                        stream.write(b"\x00\xffreverse-ssh\n")
                    with sftp.open("/file.bin", "rb") as stream:
                        self.assertEqual(b"\x00\xffreverse-ssh\n", stream.read())
                self.assertEqual(b"\x00\xffreverse-ssh\n", (self.remote_root / "file.bin").read_bytes())
            finally:
                client.close()
            import subprocess
            source = Path(self.tmp.name) / "upload.bin"
            destination = Path(self.tmp.name) / "download.bin"
            payload = bytes(range(256)) * 1025
            source.write_bytes(payload)
            child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            for arguments in (["upload", "--name", "ci-real-ssh", str(source), "/daemon.bin"],
                              ["download", "--name", "ci-real-ssh", "/daemon.bin", str(destination)]):
                completed = subprocess.run([sys.executable, str(Path(core.__file__).with_name("ssh_relay.py")),
                    *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", env=child_env, timeout=15)
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertEqual(payload, destination.read_bytes())

        def test_command_with_lost_result_is_not_repeated_after_reconnect(self):
            original = self.server.execute_command
            def execute(channel, command):
                if command == "test:lost-result":
                    channel.get_transport().close()
                else:
                    original(channel, command)
            self.server.execute_command = execute
            self.start_daemon()
            result = self.exec("test:lost-result")
            self.assertFalse(result["ok"], result)
            self.wait_connected()
            self.assertEqual(1, self.server.commands.count("test:lost-result"))
            self.assertTrue(self.exec("test:real-success")["ok"])
            self.assertEqual(1, self.server.commands.count("test:lost-result"))

else:
    @unittest.skip("Для реального SSH нужна обязательная зависимость Paramiko; CI устанавливает её.")
    class ReverseSSHIntegrationTests(unittest.TestCase):
        def test_reverse_ssh_dependency(self):
            pass


if __name__ == "__main__":
    unittest.main()
