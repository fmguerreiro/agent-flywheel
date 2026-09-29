from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_flywheel import adjudicate, store  # pyright: ignore[reportMissingImports]


class StubRunner:
    model_id = "test"

    def __init__(self, errors=0):
        self.calls = 0
        self.errors = errors

    def classify(self, payload, *, system, timeout=None):
        self.calls += 1
        if self.errors:
            self.errors -= 1
            raise ValueError("bad classifier output")
        return {
            "action": "unmatched",
            "fingerprint": "new-rule",
            "component": "rules",
            "confidence": 0.9,
            "rationale": "No supplied case covers this correction.",
            "evidence": [payload["signal"]["reason"]],
        }


class AdjudicationRetryTest(unittest.TestCase):
    def connection(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / "flywheel.sqlite")
        self.addCleanup(conn.close)
        return conn

    def add_signal(self, conn):
        return store.add_signal(
            conn,
            session_id="session-1",
            source="test",
            kind="correction",
            component="rules",
            reason="Fix the rule, not only its output.",
        )

    def test_stable_decision_waits_for_catalog_change(self):
        conn = self.connection()
        self.add_signal(conn)
        store.ensure_case_stub(conn, "existing-case", component="rules")
        runner = StubRunner()
        now = datetime(2026, 9, 29, tzinfo=UTC)

        first = adjudicate.run(conn, runner, force=True, now=now)
        unchanged = adjudicate.run(conn, runner, force=True, now=now + timedelta(days=2))
        store.ensure_case_stub(conn, "new-case", component="rules")
        changed = adjudicate.run(conn, runner, force=True, now=now + timedelta(days=2))

        self.assertEqual(first["model_calls"], 1)
        self.assertEqual(unchanged["result"], "cooldown")
        self.assertEqual(changed["model_calls"], 1)
        self.assertEqual(runner.calls, 2)

    def test_classifier_error_retries_after_cooldown(self):
        conn = self.connection()
        self.add_signal(conn)
        runner = StubRunner(errors=1)
        now = datetime(2026, 9, 29, tzinfo=UTC)

        failed = adjudicate.run(conn, runner, force=True, now=now)
        cooling = adjudicate.run(conn, runner, force=True, now=now + timedelta(hours=23))
        retried = adjudicate.run(conn, runner, force=True, now=now + timedelta(hours=25))

        self.assertEqual(failed["errors"], 1)
        self.assertEqual(cooling["result"], "cooldown")
        self.assertEqual(retried["model_calls"], 1)
        self.assertEqual(runner.calls, 2)

    def test_changed_decision_retries_after_cooldown(self):
        conn = self.connection()
        signal_id = self.add_signal(conn)
        runner = StubRunner()
        now = datetime(2026, 9, 29, tzinfo=UTC)

        first = adjudicate.run(conn, runner, force=True, now=now)
        conn.execute(
            "UPDATE adjudication_decisions SET outcome = 'changed' WHERE signal_id = ?",
            (signal_id,),
        )
        cooling = adjudicate.run(conn, runner, force=True, now=now + timedelta(hours=23))
        retried = adjudicate.run(conn, runner, force=True, now=now + timedelta(hours=25))

        self.assertEqual(first["model_calls"], 1)
        self.assertEqual(cooling["result"], "cooldown")
        self.assertEqual(retried["model_calls"], 1)
        self.assertEqual(runner.calls, 2)


if __name__ == "__main__":
    unittest.main()
