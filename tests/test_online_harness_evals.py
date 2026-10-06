from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_flywheel import evals
from agent_flywheel.host import OnlineEvaluation


class OnlineHarnessEvalTest(unittest.TestCase):
    def make_case(self, root: Path, kind: str, **kwargs) -> evals.Case:
        case_dir = root / "case"
        case_dir.mkdir()
        (case_dir / "prompt.txt").write_text("prompt")
        harness = kwargs.pop("harness", "external")
        return evals.Case(
            id="case",
            kind=kind,
            component="test",
            severity="low",
            harness=harness,
            online=True,
            origin="test",
            path=case_dir,
            prompt_file="prompt.txt",
            **kwargs,
        )

    def test_external_dispatch_matches_evidence(self):
        class Harness:
            name = "external"

            def run(self, prompt, *, cwd, home):
                self.prompt, self.cwd, self.home = prompt, cwd, home
                return OnlineEvaluation(True, dispatch_evidence=("expected",))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree, home = root / "tree", root / "home"
            tree.mkdir()
            harness = Harness()
            result = evals.run_case(
                self.make_case(root, "dispatch", expect_skill=["expected"]),
                tree=tree,
                home=home,
                online=True,
                online_harnesses=(harness,),
            )

        self.assertTrue(result.passed)
        self.assertEqual(harness.prompt, "prompt")
        self.assertEqual(harness.cwd, tree)
        self.assertEqual(harness.home, home)

    def test_external_behavioral_receives_staged_home(self):
        class Harness:
            name = "external"

            def run(self, _prompt, *, cwd, home):
                self.cwd, self.home = cwd, home
                (cwd / "result").write_text("done")
                return OnlineEvaluation(True)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree, home = root / "tree", root / "home"
            tree.mkdir()
            case = self.make_case(
                root,
                "behavioral",
                fixture="fixture",
                verify="verify.sh",
            )
            (case.path / "fixture").mkdir()
            (case.path / "verify.sh").write_text('[ "$(cat result)" = done ]\n')
            harness = Harness()
            result = evals.run_case(
                case,
                tree=tree,
                home=home,
                online=True,
                online_harnesses=(harness,),
            )

        self.assertTrue(result.passed)
        self.assertNotEqual(harness.cwd, harness.home)
        self.assertEqual(harness.home, home)

    def test_failed_external_harness_is_skipped_infrastructure(self):
        class Harness:
            name = "external"

            def run(self, _prompt, *, cwd, home):
                return OnlineEvaluation(False, "offline")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            result = evals.run_case(
                self.make_case(root, "dispatch"),
                tree=tree,
                home=root / "home",
                online=True,
                online_harnesses=(Harness(),),
            )

        self.assertEqual(result, evals.Result(False, True, "infra: offline", {"infra": True}))

    def test_unconfigured_harness_skips_without_executable_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree"
            tree.mkdir()
            result = evals.run_case(
                self.make_case(root, "dispatch", harness="unconfigured"),
                tree=tree,
                home=root / "home",
                online=True,
            )

        self.assertTrue(result.skipped)
        self.assertIn("not configured", result.detail)
