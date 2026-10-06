from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_flywheel import adjudicate, cli, config, evals, ingest, store
from agent_flywheel.runner import MODEL, OmpRunner, prepare_runtime_home


class ExternalRunner:
    def __init__(self, label: str) -> None:
        self.label = label
        self.classified = False

    def classify(self, payload, *, system, timeout=None):
        self.classified = True
        return {}

    def draft_argv(self, prompt_path, *, cwd, scratch, tools, timeout):
        return []


class BrokenRunner:
    def classify(self, payload, *, system, timeout=None):
        return {}


class ExternalSource:
    def __init__(self, name: str) -> None:
        self.name = name

    def sessions(self):
        return []

    def owns(self, path):
        return path == "opaque-session"

    def meta(self, path):
        return {}

    def corrections(self, path):
        return [{"correction": "stop"}]

    def skills_used(self, path):
        return []

    def is_subagent(self, path):
        return False


class BrokenSource:
    name = "broken"


def make_runner(label: str):
    return ExternalRunner(label)


def make_source(name: str):
    return ExternalSource(name)


def make_broken_source():
    return BrokenSource()


def make_broken_runner():
    return BrokenRunner()




class ConfigAdapterTest(unittest.TestCase):
    def build(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "flywheel.toml").write_text(text)
            with patch.dict(os.environ, {"AGENT_FLYWHEEL_HOME": str(home)}):
                return config.build(quiet=True)

    def test_uses_configured_runner_and_source_factories(self):
        flywheel = self.build(
            f'''\
[host]
kind = "simple"
repo = "."
stage = {{ "rules" = ".rules" }}

[runner]
factory = "{__name__}:make_runner"
kwargs = {{ label = "external" }}

[[sources]]
factory = "{__name__}:make_source"
kwargs = {{ name = "external" }}
'''
        )
        self.assertEqual(flywheel.runner.label, "external")
        self.assertEqual(flywheel.sources[0].name, "external")



    def test_uses_omp_runner_without_runner_configuration(self):
        self.assertIsInstance(config._build_runner({}), OmpRunner)

    def test_rejects_source_missing_protocol_methods(self):
        with self.assertRaisesRegex(ValueError, "missing sessions"):
            self.build(
                f'''\
[host]
kind = "simple"
repo = "."
stage = {{ "rules" = ".rules" }}

[[sources]]
factory = "{__name__}:make_broken_source"
'''
            )

    def test_rejects_runner_missing_protocol_methods(self):
        with self.assertRaisesRegex(ValueError, "missing draft_argv"):
            self.build(
                f'''\
[host]
kind = "simple"
repo = "."
stage = {{ "rules" = ".rules" }}

[runner]
factory = "{__name__}:make_broken_runner"
'''
            )

    def test_adjudicate_uses_configured_runner(self):
        runner = ExternalRunner("external")
        flywheel = SimpleNamespace(runner=runner, db_path=Path("/tmp/flywheel.sqlite"))
        args = SimpleNamespace(
            min_signals=5,
            max_age_hours=24,
            batch_size=20,
            confidence=0.6,
            promotion_sessions=3,
            retry_hours=24,
            shadow=False,
            force=False,
            model=None,
            timeout=None,
        )

        def run(_conn, selected_runner, **_kwargs):
            selected_runner.classify({}, system="test")
            return {"result": "ok"}

        with (
            patch.object(store, "connect", return_value=object()),
            patch.object(adjudicate, "run", side_effect=run),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.cmd_adjudicate(flywheel, args), 0)
        self.assertTrue(runner.classified)

    def test_uses_source_correction_parser(self):
        source = ExternalSource("external")
        self.assertEqual(
            ingest.corrections((source,), "opaque-session"),
            [{"correction": "stop"}],
        )

    def test_runtime_home_copies_wal_backed_agent_credential_database(self):
        with tempfile.TemporaryDirectory() as directory:
            source_home = Path(directory) / "source"
            source_db = source_home / ".omp" / "agent" / "agent.db"
            source_db.parent.mkdir(parents=True)
            with sqlite3.connect(source_db) as source:
                source.execute("PRAGMA journal_mode=WAL")
                source.execute("CREATE TABLE credentials (token TEXT)")
                source.execute("INSERT INTO credentials VALUES ('available')")
                source.commit()
                self.assertTrue(source_db.with_name("agent.db-wal").is_file())
                with tempfile.TemporaryDirectory() as runtime:
                    with patch("agent_flywheel.runner.Path.home", return_value=source_home):
                        target_home = prepare_runtime_home(Path(runtime))
                    with sqlite3.connect(target_home / ".omp" / "agent" / "agent.db") as target:
                        self.assertEqual(
                            target.execute("SELECT token FROM credentials").fetchone(),
                            ("available",),
                        )

    def test_eval_uses_flywheel_model_without_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(evals._pinned_omp_model(), MODEL)
