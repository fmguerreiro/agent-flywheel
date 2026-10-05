"""sqlite registry for the flywheel: sessions -> signals -> cases -> eval_runs.

WAL mode and 0600/0700 perms because the db lives under
~/.local/share/agent-flywheel alongside transcript-derived signal payloads,
which can carry prompt text worth keeping off shared/group-readable paths.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("AGENT_FLYWHEEL_HOME", "~/.local/share/agent-flywheel")).expanduser()
SCHEMA_VERSION = 2

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        harness TEXT NOT NULL,
        session_id TEXT NOT NULL,
        cwd TEXT,
        transcript_path TEXT,
        trace_id TEXT,
        model TEXT,
        harness_version TEXT,
        dotfiles_sha TEXT,
        started_at TEXT,
        ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(harness, session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        component TEXT,
        reason TEXT,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        case_id TEXT,
        adjudicated_by TEXT,
        adjudication_confidence REAL,
        adjudicated_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cases (
        id TEXT PRIMARY KEY,
        fingerprint TEXT UNIQUE,
        component TEXT,
        severity TEXT,
        status TEXT NOT NULL DEFAULT 'candidate',
        diagnosis TEXT,
        eval_path TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eval_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT,
        baseline_sha TEXT,
        candidate_sha TEXT,
        model TEXT,
        result TEXT,
        metrics_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adjudication_runs (
        id TEXT PRIMARY KEY,
        trigger TEXT NOT NULL,
        mode TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        catalog_hash TEXT NOT NULL,
        started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        finished_at TEXT,
        result TEXT,
        metrics_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adjudication_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        signal_id INTEGER NOT NULL,
        proposed_action TEXT,
        proposed_case_id TEXT,
        proposed_confidence REAL,
        proposed_fingerprint TEXT,
        proposed_component TEXT,
        proposed_rationale TEXT,
        proposed_evidence_json TEXT,
        verified_action TEXT,
        verified_case_id TEXT,
        verified_confidence REAL,
        verified_rationale TEXT,
        verified_evidence_json TEXT,
        outcome TEXT NOT NULL,
        retry_after TEXT,
        applied_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(run_id, signal_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draft_attempts (
        id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL REFERENCES cases(id),
        status TEXT NOT NULL DEFAULT 'running',
        outcome TEXT,
        base_sha TEXT NOT NULL,
        head_sha TEXT,
        branch_name TEXT NOT NULL,
        worktree_path TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        result_json TEXT,
        error TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        retry_after TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        completed_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS one_running_draft_per_case
    ON draft_attempts(case_id) WHERE status = 'running'
    """,
)


def home() -> Path:
    return DEFAULT_HOME


def _user_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _migrate(conn) -> None:
    if _user_version(conn) == SCHEMA_VERSION:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _user_version(conn)
        if version == SCHEMA_VERSION:
            conn.commit()
            return
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than supported version {SCHEMA_VERSION}")

        for statement in _SCHEMA[:4]:
            conn.execute(statement)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
        for name, kind in (
            ("adjudicated_by", "TEXT"),
            ("adjudication_confidence", "REAL"),
            ("adjudicated_at", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {kind}")

        conn.execute(
            """
            UPDATE signals
            SET adjudicated_by = 'human', adjudicated_at = created_at
            WHERE status != 'open' AND adjudicated_by IS NULL
            """
        )
        for statement in _SCHEMA[4:]:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def connect(path: Path | None = None) -> sqlite3.Connection:
    home_dir = path.parent if path is not None else DEFAULT_HOME
    home_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(home_dir, 0o700)
    db_path = path if path is not None else home_dir / "flywheel.sqlite"

    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    _migrate(conn)

    os.chmod(db_path, 0o600)
    return conn


@contextmanager
def immediate_transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def record_session(
    conn,
    *,
    harness,
    session_id,
    cwd,
    transcript_path=None,
    trace_id=None,
    model=None,
    harness_version=None,
    dotfiles_sha=None,
    started_at=None,
) -> str:
    existing = conn.execute(
        "SELECT id FROM sessions WHERE harness = ? AND session_id = ?",
        (harness, session_id),
    ).fetchone()
    if existing is not None:
        row_id = existing["id"]
        conn.execute(
            """
            UPDATE sessions SET
                cwd = ?, transcript_path = ?, trace_id = ?, model = ?,
                harness_version = ?, dotfiles_sha = ?, started_at = ?
            WHERE id = ?
            """,
            (cwd, transcript_path, trace_id, model, harness_version, dotfiles_sha, started_at, row_id),
        )
        conn.commit()
        return row_id

    row_id = _new_id()
    conn.execute(
        """
        INSERT INTO sessions (
            id, harness, session_id, cwd, transcript_path, trace_id, model,
            harness_version, dotfiles_sha, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row_id, harness, session_id, cwd, transcript_path, trace_id, model, harness_version, dotfiles_sha, started_at),
    )
    conn.commit()
    return row_id


def add_signal(conn, *, session_id, source, kind, component=None, reason=None, payload=None) -> int:
    payload_json = json.dumps(payload) if payload is not None else None
    cur = conn.execute(
        """
        INSERT INTO signals (session_id, source, kind, component, reason, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, source, kind, component, reason, payload_json),
    )
    conn.commit()
    return cur.lastrowid


def add_signal_once(conn, *, session_id, source, kind, component=None, reason=None, payload=None):
    """Insert once per source turn while preserving later recurrences."""
    payload_json = json.dumps(payload, sort_keys=True) if payload is not None else None
    assistant = payload.get("assistant") if isinstance(payload, dict) else None
    assistant_text = assistant.get("text") if isinstance(assistant, dict) else None
    turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
    occurred_at = payload.get("occurred_at") if isinstance(payload, dict) else None
    event_second = occurred_at[:19] if isinstance(occurred_at, str) else None
    if event_second is not None:
        existing = conn.execute(
            """
            SELECT id FROM signals
            WHERE source = ? AND kind = ? AND reason IS ?
              AND (
                substr(json_extract(payload_json, '$.occurred_at'), 1, 19) = ?
                OR (
                  ? IS NOT NULL
                  AND json_extract(payload_json, '$.assistant.text') = ?
                )
                OR (
                  json_extract(payload_json, '$.occurred_at') IS NULL
                  AND session_id IS ?
                  AND json_extract(payload_json, '$.assistant.text') IS ?
                )
              )
            """,
            (
                source,
                kind,
                reason,
                event_second,
                assistant_text,
                assistant_text,
                session_id,
                assistant_text,
            ),
        ).fetchone()
    else:
        existing = conn.execute(
            """
            SELECT id FROM signals
            WHERE source = ? AND kind = ? AND reason IS ?
              AND (
                json_extract(payload_json, '$.turn_id') IS ?
                OR (
                  ? IS NOT NULL
                  AND json_extract(payload_json, '$.assistant.text') = ?
                )
                OR (
                  json_extract(payload_json, '$.turn_id') IS NULL
                  AND session_id IS ?
                  AND json_extract(payload_json, '$.assistant.text') IS ?
                )
              )
            """,
            (
                source,
                kind,
                reason,
                turn_id,
                assistant_text,
                assistant_text,
                session_id,
                assistant_text,
            ),
        ).fetchone()
    if existing is not None:
        return None
    cur = conn.execute(
        """
        INSERT INTO signals (session_id, source, kind, component, reason, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, source, kind, component, reason, payload_json),
    )
    conn.commit()
    return cur.lastrowid


def open_signals(conn, limit=None):
    query = "SELECT * FROM signals WHERE status = 'open' ORDER BY created_at ASC"
    if limit is not None:
        query += " LIMIT ?"
        return conn.execute(query, (limit,)).fetchall()
    return conn.execute(query).fetchall()


def resolve_signal(conn, signal_id, status, case_id=None, *, actor="human", confidence=None) -> None:
    conn.execute(
        """
        UPDATE signals
        SET status = ?, case_id = ?, adjudicated_by = ?,
            adjudication_confidence = ?,
            adjudicated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (status, case_id, actor, confidence, signal_id),
    )


def promote_case(
    conn,
    *,
    signal_ids,
    fingerprint,
    component,
    severity,
    diagnosis,
    eval_path=None,
    case_id=None,
    actor="human",
    confidence=None,
) -> str:
    case_id = case_id or _new_id()
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO cases (id, fingerprint, component, severity, diagnosis, eval_path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (case_id, fingerprint, component, severity, diagnosis, eval_path),
        )
        conn.executemany(
            """
            UPDATE signals
            SET status = 'promoted', case_id = ?, adjudicated_by = ?,
                adjudication_confidence = ?,
                adjudicated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            [(case_id, actor, confidence, sid) for sid in signal_ids],
        )
    return case_id


def ensure_case_stub(conn, case_id, *, component=None, severity=None, diagnosis=None) -> None:
    """No-op if `case_id` already has a row; otherwise inserts a minimal
    active one, so an eval run can never point at a case that doesn't exist.
    """
    conn.execute(
        """
        INSERT INTO cases (id, component, severity, status, diagnosis)
        VALUES (?, ?, ?, 'active', ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (case_id, component, severity, diagnosis or "eval-authored: no signal was ever promoted into this case"),
    )
    conn.commit()


def activate_case(conn, case_id) -> None:
    """A case proves itself by a `fixed` eval verdict (base failed, candidate
    passed against a real revision), not by a human's promotion call alone.
    """
    conn.execute(
        "UPDATE cases SET status = 'active' WHERE id = ? AND status = 'candidate'",
        (case_id,),
    )
    conn.commit()


def cases(conn, status=None):
    if status is not None:
        return conn.execute("SELECT * FROM cases WHERE status = ? ORDER BY created_at ASC", (status,)).fetchall()
    return conn.execute("SELECT * FROM cases ORDER BY created_at ASC").fetchall()


def record_eval_run(conn, *, case_id, baseline_sha, candidate_sha, model, result, metrics=None) -> int:
    metrics_json = json.dumps(metrics) if metrics is not None else None
    cur = conn.execute(
        """
        INSERT INTO eval_runs (case_id, baseline_sha, candidate_sha, model, result, metrics_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, baseline_sha, candidate_sha, model, result, metrics_json),
    )
    conn.commit()
    return cur.lastrowid


def start_adjudication_run(conn, *, trigger, mode, model, prompt_version, catalog_hash) -> str:
    run_id = _new_id()
    conn.execute(
        """
        INSERT INTO adjudication_runs (id, trigger, mode, model, prompt_version, catalog_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, trigger, mode, model, prompt_version, catalog_hash),
    )
    return run_id


def finish_adjudication_run(conn, run_id, *, result, metrics=None) -> None:
    conn.execute(
        """
        UPDATE adjudication_runs
        SET finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            result = ?, metrics_json = ?
        WHERE id = ?
        """,
        (result, json.dumps(metrics, sort_keys=True) if metrics is not None else None, run_id),
    )


def record_adjudication_decision(
    conn,
    *,
    run_id,
    signal_id,
    proposed=None,
    verified=None,
    outcome,
    retry_after=None,
    applied=False,
) -> None:
    proposed = proposed or {}
    verified = verified or {}
    conn.execute(
        """
        INSERT INTO adjudication_decisions (
            run_id, signal_id,
            proposed_action, proposed_case_id, proposed_confidence,
            proposed_fingerprint, proposed_component, proposed_rationale,
            proposed_evidence_json,
            verified_action, verified_case_id, verified_confidence,
            verified_rationale, verified_evidence_json,
            outcome, retry_after, applied_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CASE
            WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ELSE NULL
        END)
        """,
        (
            run_id,
            signal_id,
            proposed.get("action"),
            proposed.get("case_id"),
            proposed.get("confidence"),
            proposed.get("fingerprint"),
            proposed.get("component"),
            proposed.get("rationale"),
            json.dumps(proposed.get("evidence"), sort_keys=True),
            verified.get("action"),
            verified.get("case_id"),
            verified.get("confidence"),
            verified.get("rationale"),
            json.dumps(verified.get("evidence"), sort_keys=True),
            outcome,
            retry_after,
            applied,
        ),
    )


def merge_signal_automatically(conn, *, signal_id, case_id, actor, confidence) -> bool:
    with immediate_transaction(conn):
        case = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if case is None:
            return False
        updated = conn.execute(
            """
            UPDATE signals
            SET status = 'merged', case_id = ?, adjudicated_by = ?,
                adjudication_confidence = ?,
                adjudicated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ? AND status = 'open'
            """,
            (case_id, actor, confidence, signal_id),
        )
        return updated.rowcount == 1


def promote_signals_automatically(
    conn,
    *,
    signal_ids,
    fingerprint,
    component,
    diagnosis,
    actor,
    confidence,
    minimum_sessions=3,
):
    with immediate_transaction(conn):
        if conn.execute("SELECT id FROM cases WHERE fingerprint = ?", (fingerprint,)).fetchone():
            return "fingerprint_collision", None

        placeholders = ",".join("?" for _ in signal_ids)
        rows = conn.execute(
            f"""
            SELECT id, session_id, created_at FROM signals
            WHERE id IN ({placeholders}) AND status = 'open'
            ORDER BY created_at, id
            """,
            signal_ids,
        ).fetchall()
        sessions = {row["session_id"] for row in rows if row["session_id"]}
        if len(rows) != len(signal_ids) or len(sessions) < minimum_sessions:
            return "changed", None

        case_id = _new_id()
        try:
            conn.execute(
                """
                INSERT INTO cases (id, fingerprint, component, severity, diagnosis)
                VALUES (?, ?, ?, 'medium', ?)
                """,
                (case_id, fingerprint, component, diagnosis),
            )
        except sqlite3.IntegrityError:
            return "fingerprint_collision", None

        oldest = rows[0]["id"]
        conn.execute(
            f"""
            UPDATE signals
            SET status = CASE WHEN id = ? THEN 'promoted' ELSE 'merged' END,
                case_id = ?, adjudicated_by = ?, adjudication_confidence = ?,
                adjudicated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id IN ({placeholders})
            """,
            (oldest, case_id, actor, confidence, *signal_ids),
        )
        return "promoted", case_id


def case_eval_runs(conn, case_id):
    return conn.execute(
        "SELECT * FROM eval_runs WHERE case_id = ? ORDER BY created_at DESC",
        (case_id,),
    ).fetchall()


def counts(conn) -> dict:
    def one(query, *params):
        return conn.execute(query, params).fetchone()[0]

    return {
        "sessions": one("SELECT COUNT(*) FROM sessions"),
        "signals_open": one("SELECT COUNT(*) FROM signals WHERE status = 'open'"),
        "cases_candidate": one("SELECT COUNT(*) FROM cases WHERE status = 'candidate'"),
        "cases_active": one("SELECT COUNT(*) FROM cases WHERE status = 'active'"),
        "eval_runs": one("SELECT COUNT(*) FROM eval_runs"),
    }


# Only a transient infra failure (subprocess crash, timeout, git error) ever
# auto-retries. A drafter-declared `blocked`, or a `needs_revision` from a
# real eval run, means the *content* was wrong, not the environment - retrying
# blind would just spend another model call to reach the same answer. Both
# recover only through `draft-retry`.
MAX_DRAFT_RETRIES = 3
DRAFT_RETRY_BACKOFF_HOURS = 1


def recover_running_attempts(conn) -> None:
    """Fail leases left behind after the process-wide drafter lock was released."""
    conn.execute(
        """
        UPDATE draft_attempts
        SET retry_count = retry_count + 1,
            status = CASE
                WHEN retry_count + 1 >= ? THEN 'blocked'
                ELSE 'failed'
            END,
            retry_after = CASE
                WHEN retry_count + 1 >= ? THEN NULL
                ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            END,
            error = 'drafter process exited before completing its lease',
            completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE status = 'running'
        """,
        (MAX_DRAFT_RETRIES, MAX_DRAFT_RETRIES),
    )
    conn.commit()


def lease_candidate(conn, *, model, prompt_version, base_sha, branch_name, worktree_path, case_id=None):
    """Atomically select an eligible candidate and open a `running` attempt
    for it, so the 15-minute LaunchAgent and a manual `--case` invocation can
    never draft the same case twice. Returns `(attempt_id, case_id)`, or
    `None` if nothing is eligible (or, for an explicit `case_id`, if it isn't
    a candidate or already has a running attempt).

    Omitting `case_id` only considers cases whose latest attempt - if any -
    isn't `running`/`needs_revision`/`blocked`, and whose `failed` backoff (if
    any) has elapsed; `draft-retry` passes `case_id` to bypass that history.
    """
    with immediate_transaction(conn):
        if case_id is not None:
            row = conn.execute(
                """
                SELECT c.id FROM cases c
                WHERE c.id = ? AND c.status = 'candidate'
                  AND NOT EXISTS (
                      SELECT 1 FROM draft_attempts a WHERE a.case_id = c.id AND a.status = 'running'
                  )
                """,
                (case_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT c.id
                FROM cases c
                LEFT JOIN (
                    SELECT case_id, status, retry_after,
                           ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY created_at DESC) AS rn
                    FROM draft_attempts
                ) latest ON latest.case_id = c.id AND latest.rn = 1
                WHERE c.status = 'candidate'
                  AND (latest.status IS NULL OR latest.status NOT IN ('running', 'needs_revision', 'blocked'))
                  AND (
                      latest.status IS NULL OR latest.status != 'failed'
                      OR latest.retry_after <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                  )
                ORDER BY (c.severity = 'high') DESC, c.created_at ASC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None

        case_id = row["id"]
        retry_count = conn.execute(
            "SELECT COALESCE(MAX(retry_count), 0) FROM draft_attempts WHERE case_id = ?",
            (case_id,),
        ).fetchone()[0]
        attempt_id = _new_id()
        conn.execute(
            """
            INSERT INTO draft_attempts
                (id, case_id, status, base_sha, branch_name, worktree_path, model, prompt_version, retry_count)
            VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, case_id, base_sha, branch_name, worktree_path, model, prompt_version, retry_count),
        )
        return attempt_id, case_id


def complete_attempt(conn, attempt_id, *, status, outcome=None, head_sha=None, result_json=None, error=None) -> None:
    conn.execute(
        """
        UPDATE draft_attempts
        SET status = ?, outcome = ?, head_sha = ?, result_json = ?, error = ?,
            completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (status, outcome, head_sha, result_json, error, attempt_id),
    )
    conn.commit()


def fail_attempt(conn, attempt_id, *, error) -> str:
    """A transient infra failure: retry with backoff, or - past
    `MAX_DRAFT_RETRIES` - fall through to `blocked` (same recovery path as a
    drafter-declared block: `draft-retry`, not another automatic attempt).
    Returns the resulting status.
    """
    retry_count = conn.execute(
        "SELECT retry_count FROM draft_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()["retry_count"] + 1
    status = "blocked" if retry_count >= MAX_DRAFT_RETRIES else "failed"
    retry_after_expr = (
        "NULL" if status == "blocked" else f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+{DRAFT_RETRY_BACKOFF_HOURS} hours')"
    )
    conn.execute(
        f"""
        UPDATE draft_attempts
        SET status = ?, retry_count = ?, error = ?, retry_after = {retry_after_expr},
            completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (status, retry_count, error, attempt_id),
    )
    conn.commit()
    return status


def attempts(conn, *, case_id=None, status=None):
    clauses, params = [], []
    if case_id is not None:
        clauses.append("case_id = ?")
        params.append(case_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    query = "SELECT * FROM draft_attempts"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC"
    return conn.execute(query, params).fetchall()
