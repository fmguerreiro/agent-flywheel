from __future__ import annotations

import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("bwrap"), "requires Linux bubblewrap")
    def test_linux_sandbox_confines_online_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree, home, scratch, live_home = (root / name for name in
                                               ("tree", "home", "scratch", "live-home"))
            for path in (tree, home, scratch, live_home):
                path.mkdir()
            case = self.make_case(root, "behavioral", harness="omp")
            (tree / "revision").write_text("revision")
            (case.path / "case-data").write_text("case")
            (live_home / "secret").write_text("secret")
            launcher = live_home / "omp"
            launcher.write_text('#!/bin/bash\nexec "/bin/sh" "$@"\n')
            (root / "host-temp").write_text("secret")
            (scratch / "escape").symlink_to(tree, target_is_directory=True)
            sandbox_temp = Path("/tmp") / f"{root.name}-isolated"
            script = f"""
                test "$(cat {tree}/revision)" = revision || exit 1
                test "$(cat {case.path}/case-data)" = case || exit 2
                test ! -e {live_home}/secret || exit 3
                test ! -e {root}/host-temp || exit 4
                echo ok > {home}/allowed || exit 5
                echo ok > {scratch}/allowed || exit 6
                echo bad > {tree}/revision 2>/dev/null && exit 7
                echo bad > {case.path}/case-data 2>/dev/null && exit 8
                echo bad > {scratch}/escape/revision 2>/dev/null && exit 9
                echo sandbox > {sandbox_temp} || exit 10
                exit 0
            """
            real_which = shutil.which
            with (mock.patch.object(evals, "_REAL_HOME", live_home),
                  mock.patch.object(evals.shutil, "which",
                                    side_effect=lambda name: str(launcher) if name == "omp" else real_which(name)),
                  mock.patch.object(evals, "_harness_command", return_value=["omp", "-c", script])):
                proc = evals._run_online_harness(
                    case=case, tree=tree, home=home, cwd=scratch, harness_bin="omp",
                    prompt="", env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((tree / "revision").read_text(), "revision")
            self.assertEqual((case.path / "case-data").read_text(), "case")
            self.assertFalse(sandbox_temp.exists())

    def test_linux_rejects_unsupported_launchers(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "omp"
            for content in (b"#!/bin/sh\necho unsafe\n", b"\xffnot-elf"):
                launcher.write_bytes(content)
                with mock.patch.object(evals.shutil, "which", return_value=str(launcher)):
                    with self.assertRaisesRegex(RuntimeError, "unsupported Linux omp launcher"):
                        evals._linux_omp_executable("omp")

    def test_linux_missing_bwrap_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.make_case(root, "dispatch", harness="omp")
            with (mock.patch.object(evals.sys, "platform", "linux"),
                  mock.patch.object(evals.shutil, "which", return_value=None),
                  mock.patch.object(evals.subprocess, "run") as run):
                with self.assertRaisesRegex(RuntimeError, "bwrap is required"):
                    evals._run_online_harness(
                        case=case, tree=root, home=root, cwd=root, harness_bin="omp",
                        prompt="", env={},
                    )
                run.assert_not_called()

    def test_sandbox_start_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.make_case(root, "dispatch", harness="omp")
            with (mock.patch.object(evals.sys, "platform", "darwin"),
                  mock.patch.object(evals.shutil, "which", return_value="/sandbox-exec"),
                  mock.patch.object(evals.subprocess, "run", side_effect=OSError("denied"))):
                with self.assertRaisesRegex(RuntimeError, "cannot start online harness sandbox"):
                    evals._run_online_harness(
                        case=case, tree=root, home=root, cwd=root, harness_bin="omp",
                        prompt="", env={},
                    )
