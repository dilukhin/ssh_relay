from __future__ import annotations

import builtins
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ssh_relay_build as build
import ssh_relay_entrypoint


SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


class PackagedEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        build._invocation_identity_recorded = False

    def test_doctor_loads_runtime_dependency_without_network(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(build, "_SOURCE_SHA", None), mock.patch.dict(
            os.environ, {"SSH_RELAY_SOURCE_SHA": SOURCE_SHA}, clear=False
        ), contextlib.redirect_stdout(stdout):
            result = ssh_relay_entrypoint.doctor()

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("ssh_relay 0.9.1.01234567", output)
        self.assertIn(f"Source SHA: {SOURCE_SHA}", output)
        self.assertIn("paramiko:", output)
        self.assertIn("Runtime: ok", output)

    def test_version_is_exact_and_does_not_import_operational_runtime(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(build, "_SOURCE_SHA", None), mock.patch.dict(
            os.environ, {"SSH_RELAY_SOURCE_SHA": SOURCE_SHA}, clear=False
        ), mock.patch.object(sys, "argv", ["ssh_relay", "--version"]), mock.patch.dict(
            sys.modules, {"ssh_relay": None}
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = ssh_relay_entrypoint.main()

        self.assertEqual(0, result)
        self.assertEqual("ssh_relay 0.9.1.01234567\n", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_doctor_dependency_error_contains_identity(self) -> None:
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "paramiko":
                raise ImportError("test missing dependency")
            return real_import(name, *args, **kwargs)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(build, "_SOURCE_SHA", None), mock.patch.dict(
            os.environ, {"SSH_RELAY_SOURCE_SHA": SOURCE_SHA}, clear=False
        ), mock.patch("builtins.__import__", side_effect=guarded_import), contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(stderr):
            result = ssh_relay_entrypoint.doctor()

        self.assertEqual(1, result)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("ssh_relay 0.9.1.01234567", stderr.getvalue())
        self.assertIn("test missing dependency", stderr.getvalue())

    def test_regular_cli_preserves_stdio_and_records_identity_separately(self) -> None:
        fake_module = mock.Mock()

        def fake_main() -> int:
            sys.stdout.write("remote-out\n")
            sys.stderr.write("remote-err\n")
            return 17

        fake_module.main.side_effect = fake_main
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "diagnostics.log"
            with mock.patch.object(build, "_SOURCE_SHA", None), mock.patch.dict(
                os.environ,
                {
                    "SSH_RELAY_SOURCE_SHA": SOURCE_SHA,
                    "SSH_RELAY_DIAGNOSTIC_LOG": str(log_path),
                },
                clear=False,
            ), mock.patch.dict(sys.modules, {"ssh_relay": fake_module}), mock.patch.object(
                sys, "argv", ["ssh_relay", "exec", "--name", "ci", "printf test"]
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = ssh_relay_entrypoint.main()

            lines = log_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result, 17)
        self.assertEqual("remote-out\n", stdout.getvalue())
        self.assertEqual("remote-err\n", stderr.getvalue())
        fake_module.main.assert_called_once_with()
        self.assertEqual(1, len(lines))
        self.assertIn("ssh_relay 0.9.1.01234567", lines[0])
        self.assertIn(f"source_sha={SOURCE_SHA}", lines[0])
        self.assertNotIn("printf test", lines[0])

    def test_machine_stdout_remains_one_json_document(self) -> None:
        fake_module = mock.Mock()

        def fake_main() -> int:
            sys.stdout.write('{"operation_status":"succeeded"}\n')
            return 0

        fake_module.main.side_effect = fake_main
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "diagnostics.log"
            with mock.patch.object(build, "_SOURCE_SHA", None), mock.patch.dict(
                os.environ,
                {
                    "SSH_RELAY_SOURCE_SHA": SOURCE_SHA,
                    "SSH_RELAY_DIAGNOSTIC_LOG": str(log_path),
                },
                clear=False,
            ), mock.patch.dict(sys.modules, {"ssh_relay": fake_module}), mock.patch.object(
                sys, "argv", ["ssh_relay", "exec", "--json", "true"]
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = ssh_relay_entrypoint.main()

        import json

        self.assertEqual(0, result)
        self.assertEqual({"operation_status": "succeeded"}, json.loads(stdout.getvalue()))
        self.assertEqual("", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
