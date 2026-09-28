import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import store

PROMPT_VERSION = "v1"

_SYSTEM_PROMPT = """Classify agent-feedback signals. Return one JSON object only.
Allowed actions are merge and unmatched. For merge, case_id must name one
supplied case. For unmatched, provide a stable fingerprint and component.
Always provide confidence from 0 to 1, a concise rationale, and a non-empty
evidence array quoting the supplied signal. Never reject a signal.
"""


@dataclass(frozen=True)
class Policy:
    min_signals: int = 5
    max_age_hours: int = 24
    batch_size: int = 20
    confidence: float = 0.6
    promotion_sessions: int = 3
    retry_hours: int = 24


def _iso(value):
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _signal(row):
    payload = None
    if row["payload_json"]:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = None
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "source": row["source"],
        "kind": row["kind"],
        "component": row["component"],
        "reason": row["reason"],
        "payload": payload,
        "created_at": row["created_at"],
    }


def _case(row):
    return {
        "id": row["id"],
        "fingerprint": row["fingerprint"],
        "component": row["component"],
        "severity": row["severity"],
        "status": row["status"],
        "diagnosis": row["diagnosis"],
    }


def _validate(value, case_ids):
    if not isinstance(value, dict) or value.get("action") not in {"merge", "unmatched"}:
        raise ValueError("invalid action")
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise ValueError("invalid confidence")
    rationale = value.get("rationale")
    evidence = value.get("evidence")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("missing rationale")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(item, str) and item.strip() for item in evidence)
    ):
        raise ValueError("missing evidence")
    result = {
        "action": value["action"],
        "confidence": float(confidence),
        "rationale": rationale.strip(),
        "evidence": evidence,
    }
    if value["action"] == "merge":
        case_id = value.get("case_id")
        if not isinstance(case_id, str) or case_id not in case_ids:
            raise ValueError("invalid case target")
        result["case_id"] = case_id
    else:
        fingerprint = value.get("fingerprint")
        component = value.get("component")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("missing fingerprint")
        if not isinstance(component, str) or not component.strip():
            raise ValueError("missing component")
        result["fingerprint"] = fingerprint.strip()
        result["component"] = component.strip()
    return result


def _call(runner, request, *, phase, case_ids):
    decision = runner.classify(
        {"phase": phase, **request}, system=_SYSTEM_PROMPT
    )
    return _validate(decision, case_ids)


def _eligible(conn, rows, now):
    eligible = []
    for row in rows:
        retry = conn.execute(
            """
            SELECT retry_after FROM adjudication_decisions
            WHERE signal_id = ? AND retry_after IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        if retry is None or retry["retry_after"] <= _iso(now):
            eligible.append(row)
    return eligible


def _pending_groups(conn, confidence):
    rows = conn.execute(
        """
        SELECT s.id, s.session_id, s.created_at, s.reason, s.kind,
               d.proposed_fingerprint AS fingerprint,
               d.proposed_component AS component,
               d.proposed_rationale AS rationale,
               d.proposed_confidence AS confidence
        FROM signals AS s
        JOIN adjudication_decisions AS d ON d.id = (
            SELECT d2.id
            FROM adjudication_decisions AS d2
            JOIN adjudication_runs AS r2 ON r2.id = d2.run_id
            WHERE d2.signal_id = s.id AND r2.mode = 'apply'
            ORDER BY d2.id DESC LIMIT 1
        )
        WHERE s.status = 'open'
          AND d.proposed_action = 'unmatched'
          AND d.proposed_confidence >= ?
        ORDER BY s.created_at, s.id
        """,
        (confidence,),
    ).fetchall()
    groups = {}
    for row in rows:
        groups.setdefault(row["fingerprint"], []).append(row)
    return groups


def run(conn, runner, *, policy=None, shadow=False, force=False, now=None):
    policy = policy or Policy()
    now = now or datetime.now(timezone.utc)
    open_rows = store.open_signals(conn)
    oldest_hours = 0.0
    if open_rows:
        oldest_hours = (
            now - _parse_time(open_rows[0]["created_at"])
        ).total_seconds() / 3600
    if (
        not force
        and len(open_rows) < policy.min_signals
        and oldest_hours < policy.max_age_hours
    ):
        return {
            "result": "below_threshold",
            "model_calls": 0,
            "processed": 0,
            "applied": 0,
        }

    rows = _eligible(conn, open_rows, now)[: policy.batch_size]
    if not rows:
        return {"result": "cooldown", "model_calls": 0, "processed": 0, "applied": 0}

    catalog_rows = store.cases(conn)
    catalog = [_case(row) for row in catalog_rows]
    case_ids = {item["id"] for item in catalog}
    catalog_hash = hashlib.sha256(
        json.dumps(catalog, sort_keys=True).encode()
    ).hexdigest()
    model = getattr(runner, "model_id", "injected")
    trigger = (
        "forced"
        if force
        else ("count" if len(open_rows) >= policy.min_signals else "age")
    )
    run_id = store.start_adjudication_run(
        conn,
        trigger=trigger,
        mode="shadow" if shadow else "apply",
        model=model,
        prompt_version=PROMPT_VERSION,
        catalog_hash=catalog_hash,
    )
    calls = applied = errors = 0
    retry_after = _iso(now + timedelta(hours=policy.retry_hours))

    for row in rows:
        request = {"signal": _signal(row), "cases": catalog}
        proposed = verified = None
        outcome = "ambiguous"
        try:
            calls += 1
            proposed = _call(runner, request, phase="classify", case_ids=case_ids)
            if (
                proposed["action"] == "merge"
                and proposed["confidence"] >= policy.confidence
            ):
                calls += 1
                verified = _call(
                    runner,
                    {
                        "signal": request["signal"],
                        "cases": list(reversed(catalog)),
                        "proposed_case_id": proposed["case_id"],
                    },
                    phase="verify_merge",
                    case_ids=case_ids,
                )
                agreed = (
                    verified["action"] == "merge"
                    and verified["case_id"] == proposed["case_id"]
                    and verified["confidence"] >= policy.confidence
                )
                if agreed:
                    if shadow:
                        outcome = "shadow_merge"
                    elif store.merge_signal_automatically(
                        conn,
                        signal_id=row["id"],
                        case_id=proposed["case_id"],
                        actor=f"auto:{model}",
                        confidence=min(proposed["confidence"], verified["confidence"]),
                    ):
                        outcome = "merged"
                        applied += 1
                    else:
                        outcome = "changed"
            elif (
                proposed["action"] == "unmatched"
                and proposed["confidence"] >= policy.confidence
            ):
                outcome = "pending_group"
            else:
                outcome = "low_confidence"
        except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            errors += 1
            outcome = f"classifier_error:{type(exc).__name__}"

        store.record_adjudication_decision(
            conn,
            run_id=run_id,
            signal_id=row["id"],
            proposed=proposed,
            verified=verified,
            outcome=outcome,
            retry_after=None if shadow or outcome == "merged" else retry_after,
            applied=outcome == "merged",
        )

    if not shadow:
        current_ids = {row["id"] for row in rows}
        for fingerprint, group in _pending_groups(conn, policy.confidence).items():
            components = {row["component"] for row in group}
            if len(components) != 1:
                continue
            session_ids = {row["session_id"] for row in group if row["session_id"]}
            if len(session_ids) < policy.promotion_sessions:
                continue
            selected = list(group)
            evidence = [
                _signal(
                    conn.execute(
                        "SELECT * FROM signals WHERE id = ?", (row["id"],)
                    ).fetchone()
                )
                for row in selected
            ]
            proposed = {
                "action": "unmatched",
                "fingerprint": fingerprint,
                "component": selected[0]["component"],
                "confidence": min(row["confidence"] for row in selected),
                "rationale": selected[0]["rationale"],
                "evidence": [str(item["reason"] or item["kind"]) for item in evidence],
            }
            for row in selected:
                if row["id"] not in current_ids:
                    member_proposed = {
                        "action": "unmatched",
                        "fingerprint": fingerprint,
                        "component": row["component"],
                        "confidence": row["confidence"],
                        "rationale": row["rationale"],
                        "evidence": [str(row["reason"] or row["kind"])],
                    }
                    store.record_adjudication_decision(
                        conn,
                        run_id=run_id,
                        signal_id=row["id"],
                        proposed=member_proposed,
                        outcome="pending_group",
                        retry_after=retry_after,
                    )

            verified = None
            outcome = "group_disagreed"
            try:
                calls += 1
                verified = _call(
                    runner,
                    {"signals": evidence, "cases": catalog, "proposed": proposed},
                    phase="verify_group",
                    case_ids=case_ids,
                )
                agreed = (
                    verified["action"] == "unmatched"
                    and verified["fingerprint"] == fingerprint
                    and verified["component"] == proposed["component"]
                    and verified["confidence"] >= policy.confidence
                )
                if agreed:
                    outcome, _ = store.promote_signals_automatically(
                        conn,
                        signal_ids=[row["id"] for row in selected],
                        fingerprint=fingerprint,
                        component=proposed["component"],
                        diagnosis=verified["rationale"],
                        actor=f"auto:{model}",
                        confidence=min(proposed["confidence"], verified["confidence"]),
                        minimum_sessions=policy.promotion_sessions,
                    )
                    if outcome == "promoted":
                        applied += len(selected)
            except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
                errors += 1
                outcome = f"classifier_error:{type(exc).__name__}"

            verified = verified or {}
            conn.execute(
                """
                UPDATE adjudication_decisions
                SET verified_action = ?, verified_confidence = ?,
                    verified_rationale = ?, verified_evidence_json = ?,
                    outcome = ?,
                    applied_at = CASE WHEN ? = 'promoted'
                        THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ELSE applied_at
                    END,
                    retry_after = CASE WHEN ? = 'promoted' THEN NULL ELSE ? END
                WHERE run_id = ? AND signal_id IN ({})
                """.format(",".join("?" for _ in selected)),
                (
                    verified.get("action"),
                    verified.get("confidence"),
                    verified.get("rationale"),
                    json.dumps(verified.get("evidence"), sort_keys=True),
                    outcome,
                    outcome,
                    outcome,
                    retry_after,
                    run_id,
                    *[row["id"] for row in selected],
                ),
            )
    result = "ok" if errors == 0 else "partial"
    metrics = {
        "model_calls": calls,
        "processed": len(rows),
        "applied": applied,
        "errors": errors,
    }
    store.finish_adjudication_run(conn, run_id, result=result, metrics=metrics)
    return {"result": result, "run_id": run_id, **metrics}
