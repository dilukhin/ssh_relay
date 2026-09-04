from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ssh_relay
import ssh_relay_build as build


SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


class BuildIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        build._invocation_identity_recorded = False

    def test_semantic_version_matches_pyproject(self) -> None:
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"$', pyproject, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual("0.9.1", build.SEMANTIC_VERSION)
        self.assertEqual(build.SEMANTIC_VERSION, ssh_relay.__version__)
        self.assertEqual(build.SEMANTIC_VERSION, match.group(1))

    def test_source_environment_produces_canonical_identity(self) -> None:
        with mock.patch.object(build, "_SOURCE_SHA", None), mock.patch.dict(
            os.environ, {"SSH_RELAY_SOURCE_SHA": SOURCE_SHA}, clear=False
        ):
            self.assertEqual(SOURCE_SHA, build.source_sha())
            self.assertEqual("ssh_relay 0.9.1.01234567", build.canonical_identity())

    def test_embedded_sha_cannot_be_overridden_by_runtime_environment(self) -> None:
        with mock.patch.object(build, "_SOURCE_SHA", OTHER_SHA), mock.patch.dict(
            os.environ, {"SSH_RELAY_SOURCE_SHA": SOURCE_SHA}, clear=False
        ):
            self.assertEqual(OTHER_SHA, build.source_sha())
            self.assertEqual("ssh_relay 0.9.1.fedcba98", build.canonical_identity())

    def test_invocation_identity_is_written_once_without_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostics.log"
            with mock.patch.object(build, "_SOURCE_SHA", None), mock.patch.dict(
                os.environ,
                {
                    "SSH_RELAY_SOURCE_SHA": SOURCE_SHA,
                    "SSH_RELAY_DIAGNOSTIC_LOG": str(path),
                },
                clear=False,
            ):
                self.assertTrue(build.record_invocation_identity())
                self.assertFalse(build.record_invocation_identity())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(lines))
            self.assertIn("ssh_relay 0.9.1.01234567", lines[0])
            self.assertIn(f"source_sha={SOURCE_SHA}", lines[0])
            self.assertNotIn("remote_command", lines[0])


if __name__ == "__main__":
    unittest.main()
