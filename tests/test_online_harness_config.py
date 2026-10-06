from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_flywheel import cli, config
from agent_flywheel.host import OnlineEvaluation


class ExternalHarness:
    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, prompt: str, *, cwd: Path, home: Path) -> OnlineEvaluation:
        return OnlineEvaluation(True)


def make_harness(name: str):
    return ExternalHarness(name)


class OnlineHarnessConfigTest(unittest.TestCase):
    def build(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "flywheel.toml").write_text(text)
            with patch.dict(os.environ, {"AGENT_FLYWHEEL_HOME": str(home)}):
                return config.build(quiet=True)

    def test_loads_external_online_harness(self):
        flywheel = self.build(
            f'''\
[host]
kind = "simple"
repo = "."
stage = {{ "rules" = ".rules" }}

[[online_harnesses]]
factory = "{__name__}:make_harness"
kwargs = {{ name = "external" }}
'''
        )
        self.assertEqual(flywheel.online_harnesses[0].name, "external")

    def test_rejects_reserved_online_harness_name(self):
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.build(
                f'''\
[host]
kind = "simple"
repo = "."
stage = {{ "rules" = ".rules" }}

[[online_harnesses]]
factory = "{__name__}:make_harness"
kwargs = {{ name = "omp" }}
'''
            )

    def test_rejects_duplicate_external_online_harness_names(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.build(
                f'''\
[host]
kind = "simple"
repo = "."
stage = {{ "rules" = ".rules" }}

[[online_harnesses]]
factory = "{__name__}:make_harness"
kwargs = {{ name = "external" }}

[[online_harnesses]]
factory = "{__name__}:make_harness"
kwargs = {{ name = "external" }}
'''
            )

    def test_cli_reports_malformed_online_harness_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "flywheel.toml").write_text(
                '''\
[host]
kind = "simple"
repo = "."
stage = { "rules" = ".rules" }

[online_harnesses]
'''
            )
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, {"AGENT_FLYWHEEL_HOME": str(home)}),
                redirect_stderr(stderr),
            ):
                self.assertEqual(cli.main(["status"]), 2)
            self.assertIn("agent-flywheel: configuration failed:", stderr.getvalue())
