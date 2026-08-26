from __future__ import annotations

import contextlib
import io
import sys
import unittest
from unittest import mock

import ssh_relay_entrypoint


class PackagedEntrypointTests(unittest.TestCase):
    def test_doctor_loads_runtime_dependency_without_network(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = ssh_relay_entrypoint.doctor()

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("ssh_relay 0.9.0", output)
        self.assertIn("paramiko:", output)
        self.assertIn("Runtime: ok", output)

    def test_regular_cli_delegates_to_existing_main(self) -> None:
        fake_module = mock.Mock()
        fake_module.main.return_value = 17
        with mock.patch.dict(sys.modules, {"ssh_relay": fake_module}), mock.patch.object(
            sys, "argv", ["ssh_relay", "status"]
        ):
            result = ssh_relay_entrypoint.main()

        self.assertEqual(result, 17)
        fake_module.main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
