#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_flywheel import drafting, evals
from agent_flywheel.host import Flywheel
from agent_flywheel.sandbox import NullSandbox


class StubHost:
    def __init__(self, evals_root: Path) -> None:
        self.repo = Path(evals_root).parent.parent
        self.evals_root = Path(evals_root)

    def stage_home(self, tree: Path, home: Path) -> None:
        pass

    def describe(self) -> str:
        return "A test repository."


class StubRunner:
    def classify(self, payload, *, system, timeout=None):
        raise AssertionError("drafting never classifies")

    def draft_argv(self, prompt_path, *, cwd, tools, timeout, scratch):
        runtime_home = scratch / "home"
        runtime_home.mkdir(parents=True, exist_ok=True)
        return ["/usr/bin/env", "-i", f"HOME={runtime_home}", "/bin/echo", str(prompt_path)]


def fly_for(evals_root: Path) -> Flywheel:
    return Flywheel(
        host=StubHost(evals_root),
        runner=StubRunner(),
        sandbox=NullSandbox(),
        sources=(),
        home=Path(evals_root),
        state=Path(evals_root),
    )


class Cursor:
    def fetchone(self):
        return {
            "id": "case-1",
            "component": "rules",
            "severity": "high",
            "diagnosis": "ignores the project tracker",
        }

    def fetchall(self):
        return []


class Connection:
    def execute(self, *_args, **_kwargs):
        return Cursor()


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


CASE_FILES = {
    "case.toml": 'id = "case-1"\nkind = "structural"\n',
    "check.py": "raise SystemExit(0)\n",
    "negative/rule.md": "same trigger, wrong action\n",
}

class DrafterResultTest(unittest.TestCase):
    def test_parses_only_a_trailing_json_object(self):
        output = 'reason with {braces} first\n{"outcome":"patch","nested":{"ok":true}}\n'
        self.assertEqual(drafting.parse_drafter_result(output)["outcome"], "patch")
        self.assertIsNone(drafting.parse_drafter_result(output + "trailing prose"))


class PublicationTest(unittest.TestCase):
    def test_reports_git_push_stderr(self):
        failed_push = SimpleNamespace(returncode=1, stdout="", stderr="remote rejected")
        with patch.object(drafting, "_run", return_value=failed_push):
            with self.assertRaisesRegex(RuntimeError, "remote rejected"):
                drafting.publish_to_main(Path("/repo"), "head")

class RunAttemptTest(unittest.TestCase):
    def run_fixed_attempt(self, publish, *, outcome="patch", extra_rows=()):
        events = []
        compare_kwargs = {}
        record_kwargs = {}
        record_rows = []
        complete_kwargs = {}

        def fake_run(args, **_kwargs):
            stdout = "base\n" if args[-1] == "origin/main" else ""
            return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

        def compare(*_args, **kwargs):
            compare_kwargs.update(kwargs)
            verdict = "same-pass" if outcome == "coverage_only" else "fixed"
            return [{"id": "case-1", "verdict": verdict}, *extra_rows]

        def record(_conn, _root, rows, **kwargs):
            events.append("record")
            record_rows.extend(rows)
            record_kwargs.update(kwargs)
            return {"cases_active": 1}

        def complete(*_args, **kwargs):
            events.append(f"complete:{kwargs['status']}")
            complete_kwargs.update(kwargs)

        def fail(*_args, **_kwargs):
            events.append("fail")
            return "failed"

        def wrapped_publish(*args, **kwargs):
            events.append("publish")
            return publish(*args, **kwargs)

        verifier_result = {
            "outcome": "case",
            "case_kind": "structural",
            "case_paths": ["specs/agent-evals/cases/case-1/case.toml"],
            "rationale": "checks activation and action",
        }
        fix_result = {
            "outcome": outcome,
            "fix_paths": [] if outcome == "coverage_only" else ["roles/source.py"],
            "coverage_paths": ["roles/example.py"] if outcome == "coverage_only" else [],
            "rationale": "ok",
        }

        def run_drafter(*, label, **_kwargs):
            events.append(label)
            return json.dumps(verifier_result if label == "verifier" else fix_result), 0

        bracket = (
            ([{"id": "case-1", "verdict": "fixed"}], "older", "newer")
            if outcome == "coverage_only"
            else None
        )
        find_bracket = Mock(return_value=bracket)

        with (
            tempfile.TemporaryDirectory() as state,
            patch.object(drafting, "DEFAULT_STATE", Path(state)),
            patch.object(drafting, "_repo_root", return_value=Path("/repo")),
            patch.object(drafting, "_run", side_effect=fake_run),
            patch.object(drafting.store, "recover_running_attempts"),
            patch.object(drafting.store, "lease_candidate", return_value=("attempt-1", "case-1")),
            patch.object(drafting.store, "complete_attempt", side_effect=complete),
            patch.object(drafting.store, "fail_attempt", side_effect=fail),
            patch.object(drafting, "build_verifier_prompt", return_value="verifier prompt"),
            patch.object(drafting, "build_fix_prompt", return_value="fix prompt"),
            patch.object(drafting, "run_drafter", side_effect=run_drafter),
            patch.object(drafting, "preflight_case", return_value=None),
            patch.object(drafting, "preflight_fix", return_value=None),
            patch.object(drafting, "commit_case", side_effect=lambda *_a, **_k: events.append("commit_case")),
            patch.object(drafting, "commit_worktree", return_value="head"),
            patch.object(drafting, "publish_to_main", side_effect=wrapped_publish),
            patch.object(drafting, "find_coverage_bracket", find_bracket),
            patch.object(evals, "compare", side_effect=compare),
            patch.object(evals, "record_results", side_effect=record),
        ):
            result = drafting.run_attempt(
                fly_for(Path("/repo/specs/agent-evals")), Connection(), case_id="case-1"
            )
        return SimpleNamespace(
            result=result,
            events=events,
            compare_kwargs=compare_kwargs,
            record_kwargs=record_kwargs,
            record_rows=record_rows,
            complete_kwargs=complete_kwargs,
            bracket=find_bracket,
        )

    def test_case_is_drafted_and_committed_before_the_fix_is_drafted(self):
        run = self.run_fixed_attempt(lambda *_args, **_kwargs: None)
        self.assertEqual(run.events[:3], ["verifier", "commit_case", "fix"])

    def test_gates_on_the_whole_suite_and_records_only_after_publish(self):
        run = self.run_fixed_attempt(lambda *_args, **_kwargs: None)
        self.assertTrue(run.compare_kwargs["online"])
        self.assertTrue(run.compare_kwargs["autonomous"])
        self.assertIsNone(run.compare_kwargs.get("case_id"))
        self.assertEqual(run.events[3:], ["publish", "record", "complete:fixed"])
        self.assertEqual(run.result["status"], "fixed")

    def test_sibling_pass_to_fail_regression_blocks_publish(self):
        run = self.run_fixed_attempt(
            lambda *_args, **_kwargs: None,
            extra_rows=[{"id": "case-9", "verdict": "regressed"}],
        )
        self.assertEqual(run.events[3:], ["record", "complete:needs_revision"])
        self.assertIn("case-9", run.complete_kwargs["error"])
        self.assertEqual([row["id"] for row in run.record_rows], ["case-9"])

    def test_case_already_failing_at_base_does_not_block(self):
        run = self.run_fixed_attempt(
            lambda *_args, **_kwargs: None,
            extra_rows=[{"id": "case-9", "verdict": "same-fail"}],
        )
        self.assertEqual(run.events[3:], ["publish", "record", "complete:fixed"])

    def test_coverage_only_requires_and_records_historical_fixed_bracket(self):
        run = self.run_fixed_attempt(
            lambda *_args, **_kwargs: None, outcome="coverage_only"
        )
        run.bracket.assert_called_once()
        self.assertEqual(run.record_kwargs["base"], "older")
        self.assertEqual(run.record_kwargs["candidate"], "newer")
        self.assertEqual(run.events[3:], ["publish", "record", "complete:fixed"])
        self.assertEqual(run.result["status"], "fixed")

    def test_publish_failure_marks_attempt_failed_without_recording(self):
        def reject(*_args, **_kwargs):
            raise subprocess.CalledProcessError(1, ["git", "push"])

        run = self.run_fixed_attempt(reject)
        self.assertEqual(run.events[3:], ["publish", "fail"])
        self.assertEqual(run.result["status"], "failed")


class PreflightCaseTest(unittest.TestCase):
    def test_negative_fixture_must_be_a_directory(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            drafting, "_changed_paths", return_value=[]
        ):
            worktree = Path(root)
            case_dir = worktree / "specs" / "agent-evals" / "cases" / "case-1"
            case_dir.mkdir(parents=True)
            (case_dir / "case.toml").write_text(
                'id = "case-1"\nkind = "structural"\n'
            )
            (case_dir / "negative.txt").write_text("not a fixture directory")
            error = drafting.preflight_case(
                worktree=worktree,
                evals_rel="specs/agent-evals",
                case_id="case-1",
                result={"outcome": "case", "case_kind": "structural", "case_paths": []},
            )
        self.assertEqual(error, "structural case has no negative*/ fixture")

    def test_source_edit_cannot_be_declared_as_a_case_path(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            drafting, "_changed_paths", return_value=["roles/source.py"]
        ):
            worktree = Path(root)
            source = worktree / "roles" / "source.py"
            source.parent.mkdir()
            source.write_text("changed")
            error = drafting.preflight_case(
                worktree=worktree,
                evals_rel="specs/agent-evals",
                case_id="case-1",
                result={
                    "outcome": "case",
                    "case_kind": "structural",
                    "case_paths": ["roles/source.py"],
                },
            )
        self.assertIn("outside the leased case directory", error)


class PreflightFixTest(unittest.TestCase):
    def test_fix_cannot_touch_the_case_that_judges_it(self):
        error = self.fix_error(
            changed=["roles/source.py", "specs/agent-evals/cases/case-1/check.py"],
            fix_paths=["roles/source.py", "specs/agent-evals/cases/case-1/check.py"],
        )
        self.assertEqual(
            error,
            "fix must not touch the eval case that judges it: "
            "specs/agent-evals/cases/case-1/check.py",
        )

    def test_undeclared_edit_is_rejected(self):
        error = self.fix_error(
            changed=["roles/source.py", "roles/sibling.py"],
            fix_paths=["roles/source.py"],
        )
        self.assertIn("roles/sibling.py", error)

    def test_declared_path_never_written_is_rejected(self):
        error = self.fix_error(
            changed=["roles/source.py"],
            fix_paths=["roles/source.py", "roles/planned.py"],
        )
        self.assertIn("roles/planned.py", error)

    def fix_error(self, *, changed, fix_paths):
        with tempfile.TemporaryDirectory() as root, patch.object(
            drafting, "_changed_paths", return_value=changed
        ):
            worktree = Path(root)
            for rel in changed:
                full = worktree / rel
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text("changed")
            return drafting.preflight_fix(
                worktree=worktree,
                evals_rel="specs/agent-evals",
                case_id="case-1",
                result={
                    "outcome": "patch",
                    "case_kind": "structural",
                    "fix_paths": fix_paths,
                    "coverage_paths": [],
                },
            )


class CoverageBracketTest(unittest.TestCase):
    def test_walks_path_history_until_nearest_fixed_pair(self):
        def fake_run(args, **_kwargs):
            self.assertIn("--max-count=100", args)
            self.assertEqual(args[-2:], ["--", "roles/example.py"])
            return SimpleNamespace(stdout="base\nolder\noldest\n", stderr="", returncode=0)

        compare = Mock(
            side_effect=[
                [{"id": "case-1", "verdict": "same-pass"}],
                [{"id": "case-1", "verdict": "fixed"}],
            ]
        )
        with patch.object(drafting, "_run", side_effect=fake_run), patch.object(
            evals, "compare", compare
        ):
            result = drafting.find_coverage_bracket(
                repo=Path("/repo"),
                evals_root=Path("/repo/specs/agent-evals"),
                base_sha="base",
                head_sha="generated-case",
                case_id="case-1",
                coverage_paths=["roles/example.py"],
                host=StubHost(Path("/repo/specs/agent-evals")),
            )

        self.assertEqual(result[1:], ("oldest", "older"))
        self.assertEqual(compare.call_args_list[0].kwargs["case_source"], "generated-case")
        self.assertTrue(compare.call_args_list[0].kwargs["autonomous"])


class DrafterSandboxTest(unittest.TestCase):
    def test_runtime_credentials_are_removed_after_subprocess(self):
        def fake_run(args, **_kwargs):
            home = next(a for a in args if a.startswith("HOME="))
            runtime_home = Path(home.removeprefix("HOME="))
            self.assertTrue(runtime_home.is_dir())
            return SimpleNamespace(stdout="ok", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as root, patch.object(
            drafting.shutil, "which", return_value="/bin/echo"
        ), patch.object(drafting, "_run", side_effect=fake_run):
            attempt_dir = Path(root) / "attempt"
            worktree = Path(root) / "repo"
            attempt_dir.mkdir()
            worktree.mkdir()
            stdout, returncode = drafting.run_drafter(
                runner=StubRunner(), sandbox=NullSandbox(),
                worktree=worktree, attempt_dir=attempt_dir, prompt="prompt", label="verifier"
            )
            self.assertEqual((stdout, returncode), ("ok", 0))
            self.assertFalse((attempt_dir / "tmp-verifier").exists())
            self.assertEqual((attempt_dir / "verifier.stdout.log").read_text(), "ok")


class WorktreeCleanupTest(unittest.TestCase):
    def build_repo(self, root: Path) -> tuple[Path, Path]:
        origin = root / "origin.git"
        repo = root / "repo"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        git(repo, "config", "user.email", "drafter@example.test")
        git(repo, "config", "user.name", "Drafter")
        git(repo, "config", "commit.gpgsign", "false")
        hooks = root / "no-hooks"
        hooks.mkdir()
        git(repo, "config", "core.hooksPath", str(hooks))
        (repo / "specs" / "agent-evals").mkdir(parents=True)
        (repo / "specs" / "agent-evals" / "README.md").write_text("evals\n")
        (repo / "roles").mkdir()
        (repo / "roles" / "source.py").write_text("original\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "base")
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "origin", "main")
        return repo, origin

    def write_case(self, worktree: Path) -> list[str]:
        case_dir = worktree / "specs" / "agent-evals" / "cases" / "case-1"
        paths = []
        for name, text in CASE_FILES.items():
            target = case_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            paths.append(str(target.relative_to(worktree)))
        return paths

    def test_failed_fix_call_leaves_no_committed_case_behind(self):
        unparseable = ("the fix call rambled and never closed its JSON", 0)
        crash = subprocess.SubprocessError("sandbox-exec died")
        for label, fix_behaviour in (("unparseable", unparseable), ("crash", crash)):
            with self.subTest(label), tempfile.TemporaryDirectory() as root:
                repo, origin = self.build_repo(Path(root))
                evals_root = repo / "specs" / "agent-evals"

                def run_drafter(*, worktree, label, **_kwargs):
                    if label == "fix":
                        if isinstance(fix_behaviour, Exception):
                            raise fix_behaviour
                        return fix_behaviour
                    return json.dumps({
                        "outcome": "case",
                        "case_kind": "structural",
                        "case_paths": self.write_case(worktree),
                        "rationale": "case",
                    }), 0

                with (
                    patch.object(drafting, "DEFAULT_STATE", Path(root) / "state"),
                    patch.object(drafting.store, "recover_running_attempts"),
                    patch.object(
                        drafting.store, "lease_candidate", return_value=("attempt-1", "case-1")
                    ),
                    patch.object(drafting.store, "fail_attempt", return_value="failed"),
                    patch.object(drafting, "run_drafter", side_effect=run_drafter),
                ):
                    result = drafting.run_attempt(
                        fly_for(evals_root), Connection(), case_id="case-1"
                    )

                self.assertEqual(result["status"], "failed")
                branches = subprocess.run(
                    ["git", "-C", str(repo), "branch", "--list", "agent-feedback/*"],
                    check=True, capture_output=True, text=True,
                ).stdout
                self.assertEqual(branches, "")
                worktrees = subprocess.run(
                    ["git", "-C", str(repo), "worktree", "list"],
                    check=True, capture_output=True, text=True,
                ).stdout.splitlines()
                self.assertEqual(len(worktrees), 1)
                origin_log = subprocess.run(
                    ["git", "-C", str(origin), "log", "--format=%s", "main"],
                    check=True, capture_output=True, text=True,
                ).stdout.split()
                self.assertEqual(origin_log, ["base"])


if __name__ == "__main__":
    unittest.main()
