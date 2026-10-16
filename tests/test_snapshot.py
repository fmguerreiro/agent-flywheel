from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_flywheel import cli, store  # pyright: ignore[reportMissingImports]


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "flywheel.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, db_path: Path):
        args = cli.build_parser().parse_args(["snapshot", "--json"])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = args.func(SimpleNamespace(db_path=db_path), args)
        return code, stdout.getvalue(), stderr.getvalue()

    def candidates_command(self, db_path=None, limit=None):
        argv = ["candidates", "--json"]
        if limit is not None:
            argv.extend(["--limit", str(limit)])
        args = cli.build_parser().parse_args(argv)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = args.func(SimpleNamespace(db_path=db_path or self.db_path), args)
        return code, stdout.getvalue(), stderr.getvalue()

    def proposal_command(self, runner, signal_id=1, db_path=None):
        args = cli.build_parser().parse_args(["triage-propose", str(signal_id), "--json"])
        stdout = io.StringIO()
        stderr = io.StringIO()
        fly = SimpleNamespace(db_path=db_path or self.db_path, runner=runner)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = args.func(fly, args)
        return code, stdout.getvalue(), stderr.getvalue()

    def promotion_command(self, signal_id=1, fingerprint="confirmed-correction"):
        args = cli.build_parser().parse_args([
            "triage-promote", str(signal_id), "--json",
            "--fingerprint", fingerprint, "--severity", "medium",
            "--diagnosis", "Review the correction evidence before drafting.",
        ])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = args.func(SimpleNamespace(db_path=self.db_path), args)
        return code, stdout.getvalue(), stderr.getvalue()

    def database_state(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return tuple(conn.iterdump())
        finally:
            conn.close()

    def seed(self):
        conn = store.connect(self.db_path)
        store.record_session(
            conn,
            harness="omp",
            session_id="secret-session",
            cwd="/secret/cwd",
            transcript_path="/secret/transcript.jsonl",
            trace_id="secret-trace",
            model="secret-model",
            dotfiles_sha="secret-sha",
        )
        conn.executemany(
            """
            INSERT INTO signals (session_id, source, kind, reason, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("secret-session", "manual", "corrected", "secret-reason", '{"assistant":{"text":"agent explained display mode"}}', "open", "2026-01-01T00:00:00Z"),
                ("secret-session", "manual", "corrected", "secret-reason", '{"secret":"payload"}', "open", "2026-01-01T00:00:01Z"),
                ("secret-session", "manual", "corrected", "secret-reason", '{"secret":"payload"}', "resolved", "2026-01-01T00:00:02Z"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO cases (id, fingerprint, component, severity, status, diagnosis, eval_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("case-1", "secret-fingerprint-1", "ingest", "low", "candidate", "secret-diagnosis", "/secret/eval", "2026-01-01T00:00:01Z"),
                ("case-2", "secret-fingerprint-2", None, None, "candidate", "secret-diagnosis", "/secret/eval", "2026-01-01T00:00:02Z"),
                ("case-3", "secret-fingerprint-3", "triage", "medium", "candidate", "secret-diagnosis", "/secret/eval", "2026-01-01T00:00:03Z"),
                ("case-4", "secret-fingerprint-4", "eval", "high", "candidate", "secret-diagnosis", "/secret/eval", "2026-01-01T00:00:04Z"),
                ("case-5", "secret-fingerprint-5", "draft", "high", "candidate", "secret-diagnosis", "/secret/eval", "2026-01-01T00:00:05Z"),
                ("case-6", "secret-fingerprint-6", "draft", "critical", "active", "secret-diagnosis", "/secret/eval", "2026-01-01T00:00:06Z"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO eval_runs (case_id, baseline_sha, candidate_sha, model, result, metrics_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("case-5", "secret-base", "secret-candidate", "secret-model", "regressed", '{"secret":"metrics"}', "2026-01-01T00:01:00Z"),
                ("case-6", "secret-base", "secret-candidate", "secret-model", "fixed", '{"secret":"metrics"}', "2026-01-01T00:02:00Z"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO draft_attempts (
                id, case_id, status, outcome, base_sha, head_sha, branch_name,
                worktree_path, model, prompt_version, result_json, error,
                retry_count, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("draft-1", "case-5", "failed", "secret-outcome", "secret-base", "secret-head", "secret-branch", "/secret/worktree", "secret-model", "secret-prompt", '{"secret":"result"}', "secret-error", 1, "2026-01-01T00:03:00Z", "2026-01-01T00:04:00Z"),
                ("draft-2", "case-6", "completed", "secret-outcome", "secret-base", "secret-head", "secret-branch", "/secret/worktree", "secret-model", "secret-prompt", '{"secret":"result"}', "secret-error", 2, "2026-01-01T00:05:00Z", "2026-01-01T00:06:00Z"),
            ],
        )
        conn.commit()
        conn.close()

    def test_snapshot_is_compact_safe_projection(self):
        self.seed()

        code, stdout, stderr = self.command(self.db_path)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("\n"), 1)
        snapshot = json.loads(stdout)
        self.assertEqual(set(snapshot), {"schema_version", "generated_at", "counts", "latest_eval", "latest_draft", "recent_cases"})
        self.assertRegex(snapshot.pop("generated_at"), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
        self.assertEqual(
            snapshot,
            {
                "schema_version": 1,
                "counts": {"sessions": 1, "signals_open": 2, "cases_candidate": 5, "cases_active": 1, "eval_runs": 2},
                "latest_eval": {"case_id": "case-6", "result": "fixed", "created_at": "2026-01-01T00:02:00Z"},
                "latest_draft": {"case_id": "case-6", "status": "completed", "created_at": "2026-01-01T00:05:00Z", "completed_at": "2026-01-01T00:06:00Z", "retry_count": 2},
                "recent_cases": [
                    {"id": "case-6", "component": "draft", "severity": "critical", "status": "active", "created_at": "2026-01-01T00:00:06Z"},
                    {"id": "case-5", "component": "draft", "severity": "high", "status": "candidate", "created_at": "2026-01-01T00:00:05Z"},
                    {"id": "case-4", "component": "eval", "severity": "high", "status": "candidate", "created_at": "2026-01-01T00:00:04Z"},
                    {"id": "case-3", "component": "triage", "severity": "medium", "status": "candidate", "created_at": "2026-01-01T00:00:03Z"},
                    {"id": "case-2", "component": None, "severity": None, "status": "candidate", "created_at": "2026-01-01T00:00:02Z"},
                ],
            },
        )
        for sensitive in ("secret-session", "/secret/cwd", "/secret/transcript", "secret-reason", "payload", "fingerprint", "diagnosis", "secret-base", "secret-branch", "secret-model"):
            self.assertNotIn(sensitive, stdout)

    def test_candidates_include_older_cases_diagnosis_and_latest_draft_without_mutation(self):
        self.seed()
        conn = store.connect(self.db_path)
        conn.execute("UPDATE cases SET diagnosis = NULL WHERE id = 'case-2'")
        conn.executemany(
            """
            INSERT INTO draft_attempts (
                id, case_id, status, base_sha, branch_name, worktree_path,
                model, prompt_version, error, created_at
            ) VALUES (?, ?, ?, 'secret-base', 'secret-branch', '/secret/worktree',
                      'secret-model', 'secret-prompt', ?, ?)
            """,
            [
                ("attempt-b", "case-2", "needs_revision", "revise behavior", "2026-01-02T00:00:00Z"),
                ("attempt-a", "case-2", "blocked", "older tied attempt", "2026-01-02T00:00:00Z"),
                ("attempt-z", "case-2", "failed", "older timestamp", "2026-01-01T00:00:00Z"),
                ("attempt-running", "case-3", "running", None, "2026-01-02T00:00:00Z"),
            ],
        )
        conn.commit()
        conn.close()
        before = self.database_state()

        code, stdout, stderr = self.candidates_command()

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(stdout),
            {
                "schema_version": 1,
                "candidates": [
                    {
                        "id": "case-1", "component": "ingest", "severity": "low",
                        "diagnosis": "secret-diagnosis", "created_at": "2026-01-01T00:00:01Z",
                        "latest_draft": None,
                    },
                    {
                        "id": "case-2", "component": None, "severity": None,
                        "diagnosis": None, "created_at": "2026-01-01T00:00:02Z",
                        "latest_draft": {"status": "needs_revision", "error": "revise behavior"},
                    },
                    {
                        "id": "case-3", "component": "triage", "severity": "medium",
                        "diagnosis": "secret-diagnosis", "created_at": "2026-01-01T00:00:03Z",
                        "latest_draft": {"status": "running", "error": None},
                    },
                    {
                        "id": "case-4", "component": "eval", "severity": "high",
                        "diagnosis": "secret-diagnosis", "created_at": "2026-01-01T00:00:04Z",
                        "latest_draft": None,
                    },
                    {
                        "id": "case-5", "component": "draft", "severity": "high",
                        "diagnosis": "secret-diagnosis", "created_at": "2026-01-01T00:00:05Z",
                        "latest_draft": {"status": "failed", "error": "secret-error"},
                    },
                ],
            },
        )
        self.assertEqual(self.database_state(), before)
        for sensitive in ("secret-session", "/secret/", "payload", "fingerprint", "secret-base", "secret-branch", "secret-model", "secret-result"):
            self.assertNotIn(sensitive, stdout)

    def test_candidates_order_ties_by_id_and_respect_default_and_explicit_limits(self):
        conn = store.connect(self.db_path)
        conn.executemany(
            """
            INSERT INTO cases (id, fingerprint, status, created_at)
            VALUES (?, ?, 'candidate', '2026-01-01T00:00:00Z')
            """,
            [(f"case-{index:02}", f"fingerprint-{index:02}") for index in reversed(range(25))],
        )
        conn.commit()
        conn.close()

        for limit, expected in ((None, 20), (2, 2), (0, 0), (30, 25)):
            with self.subTest(limit=limit):
                code, stdout, stderr = self.candidates_command(limit=limit)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(
                    [row["id"] for row in json.loads(stdout)["candidates"]],
                    [f"case-{index:02}" for index in range(expected)],
                )

    def test_candidates_bound_diagnosis_and_latest_error(self):
        self.seed()
        conn = store.connect(self.db_path)
        conn.execute("UPDATE cases SET diagnosis = ? WHERE id = 'case-5'", ("é" * 2001,))
        conn.execute("UPDATE draft_attempts SET error = ? WHERE id = 'draft-1'", ("é" * 501,))
        conn.commit()
        conn.close()

        code, stdout, stderr = self.candidates_command()

        self.assertEqual((code, stderr), (0, ""))
        candidate = json.loads(stdout)["candidates"][-1]
        self.assertEqual(candidate["diagnosis"], "é" * 2000)
        self.assertEqual(candidate["latest_draft"], {"status": "failed", "error": "é" * 500})

    def test_candidates_missing_database_stays_missing(self):
        missing = Path(self.tmp.name) / "missing.sqlite"

        code, stdout, stderr = self.candidates_command(db_path=missing)

        self.assertEqual((code, stdout, stderr), (2, "", "agent-flywheel: candidates unavailable\n"))
        self.assertFalse(missing.exists())

    def test_candidates_incompatible_database_is_not_migrated(self):
        conn = sqlite3.connect(self.db_path)
        conn.close()
        before = self.database_state()

        code, stdout, stderr = self.candidates_command()

        self.assertEqual((code, stdout, stderr), (2, "", "agent-flywheel: candidates unavailable\n"))
        self.assertEqual(self.database_state(), before)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        conn.close()

    def test_candidates_empty_database_returns_empty_list(self):
        conn = store.connect(self.db_path)
        conn.close()

        code, stdout, stderr = self.candidates_command()

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"schema_version": 1, "candidates": []})

    def test_signal_projection_and_human_triage_actions(self):
        self.seed()

        def command(argv):
            args = cli.build_parser().parse_args(argv)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = args.func(SimpleNamespace(db_path=self.db_path), args)
            return code, stdout.getvalue(), stderr.getvalue()

        code, stdout, stderr = command(["signals", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                "schema_version": 1,
                "signals": [
                    {
                        "id": 1,
                        "kind": "corrected",
                        "component": None,
                        "reason": "secret-reason",
                        "assistant_text": "agent explained display mode",
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": 2,
                        "kind": "corrected",
                        "component": None,
                        "reason": "secret-reason",
                        "assistant_text": None,
                        "created_at": "2026-01-01T00:00:01Z",
                    },
                ],
            },
        )

        code, stdout, stderr = command(["triage-reject", "1"])

        self.assertEqual((code, stdout, stderr), (0, "rejected signal 1\n", ""))

        code, stdout, stderr = command([
            "triage-promote",
            "2",
            "--fingerprint",
            "signals-test",
            "--severity",
            "medium",
            "--diagnosis",
            "test diagnosis",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertRegex(stdout, r"^promoted signal 2 to case [0-9a-f-]+\n$")
        conn = store.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT status FROM signals WHERE id = 2").fetchone()[0], "promoted")
        self.assertEqual(
            conn.execute(
                "SELECT c.fingerprint FROM signals AS s JOIN cases AS c ON c.id = s.case_id WHERE s.id = 2"
            ).fetchone()[0],
            "signals-test",
        )
        conn.close()

        code, stdout, stderr = command(["triage-reject", "1"])

        self.assertEqual((code, stdout, stderr), (2, "", "agent-flywheel: open signal 1 not found\n"))

    def test_json_promotion_returns_exact_persisted_case_for_selected_signal(self):
        self.seed()

        code, stdout, stderr = self.promotion_command(signal_id=2)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(stdout.splitlines()), 1)
        result = json.loads(stdout)
        conn = store.connect(self.db_path)
        try:
            signal = conn.execute("SELECT status, case_id FROM signals WHERE id = 2").fetchone()
            case = conn.execute("SELECT * FROM cases WHERE id = ?", (result["case_id"],)).fetchone()
            self.assertEqual(result, {
                "schema_version": 1,
                "signal_id": 2,
                "case_id": signal["case_id"],
            })
            self.assertEqual(signal["status"], "promoted")
            self.assertEqual(case["status"], "candidate")
            self.assertEqual(case["fingerprint"], "confirmed-correction")
            self.assertEqual(case["diagnosis"], "Review the correction evidence before drafting.")
            self.assertEqual(conn.execute("SELECT status FROM signals WHERE id = 1").fetchone()[0], "open")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0], 7)
        finally:
            conn.close()

    def test_json_promotion_failures_return_no_case_id_and_preserve_saved_state(self):
        self.seed()
        before = self.database_state()

        for signal_id, fingerprint in ((3, "confirmed-correction"), (1, "secret-fingerprint-1")):
            with self.subTest(signal_id=signal_id, fingerprint=fingerprint):
                code, stdout, stderr = self.promotion_command(signal_id, fingerprint)
                self.assertEqual((code, stdout), (2, ""))
                self.assertTrue(stderr)
                self.assertEqual(self.database_state(), before)

    def test_triage_proposal_does_not_change_saved_signals_or_create_candidates(self):
        self.seed()
        before = self.database_state()
        runner = Mock()
        runner.classify.return_value = {
            "action": "unmatched",
            "fingerprint": "missed-correction",
            "severity": "medium",
            "diagnosis": "The assistant missed a correction. Check the evidence before diagnosing.",
        }

        code, stdout, stderr = self.proposal_command(runner, signal_id=2)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.database_state(), before)


    def test_triage_proposal_rejects_invalid_or_incomplete_decisions_without_mutation(self):
        self.seed()
        before = self.database_state()
        valid = {
            "action": "unmatched",
            "fingerprint": "ignored-correction",
            "severity": "medium",
            "diagnosis": "The assistant ignored an explicit correction.",
        }
        invalid = [None, [], "not JSON", {}, {"action": "merge"}]
        for field in valid:
            invalid.append({key: value for key, value in valid.items() if key != field})
        for field in ("fingerprint", "diagnosis"):
            for value in (None, 1, [], {}, "", " \n\t", "\x00", "x" * (161 if field == "fingerprint" else 2001)):
                invalid.append({**valid, field: value})
        for severity in ("critical", "Medium", "", None, 1, [], {}):
            invalid.append({**valid, "severity": severity})
        for decision in invalid:
            with self.subTest(decision=decision):
                runner = Mock()
                runner.classify.return_value = decision
                code, stdout, stderr = self.proposal_command(runner)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("triage proposal failed:", stderr)
                self.assertEqual(self.database_state(), before)
                runner.draft_argv.assert_not_called()

    def test_triage_proposal_model_failures_do_not_mutate_database(self):
        self.seed()
        before = self.database_state()
        for error in (
            RuntimeError("model unavailable"),
            ValueError("classifier assistant message is not JSON"),
            subprocess.TimeoutExpired("omp", 17),
            subprocess.CalledProcessError(1, "omp"),
        ):
            with self.subTest(error=error):
                runner = Mock()
                runner.classify.side_effect = error
                code, stdout, stderr = self.proposal_command(runner)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("triage proposal failed:", stderr)
                self.assertEqual(self.database_state(), before)

    def test_triage_proposal_refuses_missing_or_closed_signal_before_model_call(self):
        self.seed()
        before = self.database_state()
        for signal_id in (3, 999):
            with self.subTest(signal_id=signal_id):
                runner = Mock()
                result = self.proposal_command(runner, signal_id=signal_id)
                self.assertEqual(result, (2, "", f"agent-flywheel: open signal {signal_id} not found\n"))
                runner.classify.assert_not_called()
                self.assertEqual(self.database_state(), before)

    def test_triage_proposal_refuses_signal_changed_during_model_call(self):
        self.seed()

        def classify(payload, *, system):
            conn = store.connect(self.db_path)
            conn.execute("UPDATE signals SET status = 'rejected' WHERE id = 1")
            conn.close()
            return {
                "action": "unmatched",
                "fingerprint": "ignored-correction",
                "severity": "low",
                "diagnosis": "The assistant ignored a correction.",
            }

        runner = Mock()
        runner.classify.side_effect = classify
        code, stdout, stderr = self.proposal_command(runner)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("signal 1 changed during analysis", stderr)
        conn = store.connect_readonly(self.db_path)
        self.assertEqual(conn.execute("SELECT status FROM signals WHERE id = 1").fetchone()[0], "rejected")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0], 6)
        conn.close()

    def test_triage_proposal_missing_database_stays_missing(self):
        missing = Path(self.tmp.name) / "missing.sqlite"
        runner = Mock()
        code, stdout, stderr = self.proposal_command(runner, db_path=missing)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("triage proposal unavailable:", stderr)
        self.assertFalse(missing.exists())
        runner.classify.assert_not_called()

    def test_triage_proposal_incompatible_database_is_not_migrated(self):
        conn = sqlite3.connect(self.db_path)
        conn.close()
        runner = Mock()
        code, stdout, stderr = self.proposal_command(runner)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("triage proposal unavailable:", stderr)
        runner.classify.assert_not_called()
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(), [])
        conn.close()

    def test_missing_database_stays_missing(self):
        missing = Path(self.tmp.name) / "missing.sqlite"

        code, stdout, stderr = self.command(missing)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "agent-flywheel: snapshot unavailable\n")
        self.assertFalse(missing.exists())

    def test_incompatible_database_is_not_migrated(self):
        conn = sqlite3.connect(self.db_path)
        conn.close()

        code, stdout, stderr = self.command(self.db_path)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "agent-flywheel: snapshot unavailable\n")
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(), [])
        conn.close()

    def test_empty_database_uses_null_latest_values(self):
        conn = store.connect(self.db_path)
        conn.close()

        code, stdout, stderr = self.command(self.db_path)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        snapshot = json.loads(stdout)
        self.assertIsNone(snapshot["latest_eval"])
        self.assertIsNone(snapshot["latest_draft"])
        self.assertEqual(snapshot["recent_cases"], [])


if __name__ == "__main__":
    unittest.main()
