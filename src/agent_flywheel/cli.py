"""argparse entrypoint. ingest/evals are imported lazily inside their commands
so a broken or absent sibling module never takes down `mark`/`status`, which
are the commands meant to run mid-session without failing the flow they watch.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from . import config, store


def _resolve_session_id(fly, session_arg, repo):
    if session_arg != "current":
        return session_arg
    try:
        from . import ingest
    except Exception:
        return None
    try:
        current = ingest.current_session(fly.sources, repo)
    except Exception:
        return None
    if not current:
        return None
    return current.get("session_id")


def cmd_mark(fly, args) -> int:
    conn = store.connect(fly.db_path)
    session_id = _resolve_session_id(fly, args.session, args.cwd)
    signal_id = store.add_signal(
        conn,
        session_id=session_id,
        source="mark",
        kind=args.kind,
        component=args.component,
        reason=args.reason,
    )
    print(f"signal {signal_id} recorded: kind={args.kind} session={session_id}")
    return 0


def cmd_ingest(fly, args) -> int:
    try:
        from . import ingest
    except Exception as exc:
        print(f"agent-flywheel: ingest unavailable: {exc}", file=sys.stderr)
        return 2

    conn = store.connect(fly.db_path)
    if args.transcript:
        if ingest.is_subagent_session(fly.sources, args.transcript):
            print("skipped OMP subagent transcript")
            return 0
        try:
            sessions = [ingest.session_meta(fly.sources, args.transcript)]
        except OSError as exc:
            print(f"agent-flywheel: cannot read transcript: {exc}", file=sys.stderr)
            return 2
    else:
        sessions = ingest.recent_sessions(fly.sources, since=args.since, limit=args.limit)
    derived = 0
    for session in sessions:
        store.record_session(conn, **session)
        if not args.derive_signals:
            continue
        try:
            candidates = ingest.corrections(fly.sources, session["transcript_path"])
        except OSError:
            continue
        for candidate in candidates:
            correction = candidate["correction"].splitlines()[0][:300]
            payload = candidate
            signal_id = store.add_signal_once(
                conn,
                session_id=session["session_id"],
                source="derived-correction",
                kind="corrected",
                reason=correction,
                payload=payload,
            )
            derived += signal_id is not None
    print(f"ingested {len(sessions)} session(s)")
    if args.derive_signals:
        print(f"derived {derived} correction candidate(s)")
    return 0


def cmd_triage(fly, args) -> int:
    conn = store.connect(fly.db_path)
    signals = store.open_signals(conn, limit=args.limit)

    if not sys.stdin.isatty():
        if not signals:
            print("no open signals")
            return 0
        for sig in signals:
            print(f"{sig['id']}\t{sig['kind']}\t{sig['component'] or '-'}\t{sig['reason'] or '-'}")
        return 0

    for sig in signals:
        print(f"\nsignal {sig['id']}: kind={sig['kind']} component={sig['component']} reason={sig['reason']}")
        choice = input("(r)eject / (m)erge / (p)romote / (s)kip? ").strip().lower()
        if choice == "r":
            store.resolve_signal(conn, sig["id"], "rejected")
        elif choice == "m":
            target = input("merge into case id: ").strip()
            store.resolve_signal(conn, sig["id"], "merged", case_id=target or None)
        elif choice == "p":
            fingerprint = input("fingerprint: ").strip()
            severity = input("severity (low/medium/high): ").strip() or "medium"
            diagnosis = input("diagnosis: ").strip()
            case_id = store.promote_case(
                conn,
                signal_ids=[sig["id"]],
                fingerprint=fingerprint,
                component=sig["component"],
                severity=severity,
                diagnosis=diagnosis,
            )
            print(f"promoted to case {case_id}")
        else:
            continue
    return 0


def cmd_adjudicate(fly, args) -> int:
    try:
        from . import adjudicate
        from .runner import OmpRunner
    except ImportError as exc:
        print(f"agent-flywheel: adjudicator unavailable: {exc}", file=sys.stderr)
        return 2

    runner = fly.runner
    if args.model is not None or args.timeout is not None:
        if not isinstance(runner, OmpRunner):
            print("agent-flywheel: --model and --timeout only apply to the built-in OMP runner", file=sys.stderr)
            return 2
        runner = OmpRunner(
            model=args.model or runner.model_id,
            timeout=args.timeout or runner.timeout,
        )
    policy = adjudicate.Policy(
        min_signals=args.min_signals,
        max_age_hours=args.max_age_hours,
        batch_size=args.batch_size,
        confidence=args.confidence,
        promotion_sessions=args.promotion_sessions,
        retry_hours=args.retry_hours,
    )
    try:
        result = adjudicate.run(
            store.connect(fly.db_path),
            runner,
            policy=policy,
            shadow=args.shadow,
            force=args.force,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"agent-flywheel: adjudication failed: {exc}", file=sys.stderr)
        return 2
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0 if result["result"] in {"ok", "below_threshold", "cooldown"} else 1


def cmd_eval(fly, args) -> int:
    try:
        from . import evals
    except Exception as exc:
        print(f"agent-flywheel: evals unavailable: {exc}", file=sys.stderr)
        return 2

    root = fly.host.evals_root
    results = evals.compare(
        root, host=fly.host, base=args.base, candidate=args.candidate,
        suite=args.suite, online=args.online,
        online_harnesses=fly.online_harnesses,
    )

    # compare() reports each arm as a bool, or None when the case never ran.
    # None must not print as "fail": a case nobody executed is not a failure.
    arm = {True: "pass", False: "fail", None: "-"}
    width = max((len(r["id"]) for r in results), default=4)
    print(f"{'case'.ljust(width)}  {'base':<6} {'candidate':<9} verdict")
    for row in results:
        base, cand = arm[row["base"]], arm[row["candidate"]]
        print(f"{row['id'].ljust(width)}  {base:<6} {cand:<9} {row['verdict']}")

    conn = store.connect(fly.db_path)
    counts = evals.record_results(conn, root, results, base=args.base, candidate=args.candidate)
    print(f"\nfixed: {counts['fixed']}  regressed: {counts['regressed']}  skipped: {counts['skipped']}")
    return 1 if counts["regressed"] else 0


# Roughly the threshold a reflective prompt optimizer (GEPA/HALO) needs before
# training against the same handful of cases used to author the skills would
# just measure memorization, not generalization.
ADJUDICATED_GATE = 30


def cmd_status(fly, args) -> int:
    conn = store.connect(fly.db_path)
    stats = store.counts(conn)
    for key, value in stats.items():
        print(f"{key}: {value}")

    latest = conn.execute("SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if latest is not None:
        print(f"latest_eval_run: case={latest['case_id']} result={latest['result']} at {latest['created_at']}")
    else:
        print("latest_eval_run: none")

    try:
        from . import evals
    except Exception as exc:
        print(f"agent-flywheel: evals unavailable: {exc}", file=sys.stderr)
        total_cases = holdout_cases = None
    else:
        root = fly.host.evals_root
        total_cases = len(evals.load_suite(root, "all"))
        holdout_cases = len(evals.load_suite(root, "holdout"))
    # Automated promotions remain useful cases, but only human decisions count
    # toward optimizer readiness.
    adjudicated = conn.execute(
        """
        SELECT COUNT(DISTINCT case_id) FROM signals
        WHERE status = 'promoted' AND case_id IS NOT NULL
          AND adjudicated_by = 'human'
        """
    ).fetchone()[0]
    if holdout_cases is None:
        holdout_state = "unavailable"
    elif holdout_cases:
        holdout_state = "present"
    else:
        holdout_state = "MISSING"

    print(f"cases_total: {'unavailable' if total_cases is None else total_cases}")
    print(f"cases_adjudicated: {adjudicated}")
    print(f"cases_holdout: {'unavailable' if holdout_cases is None else holdout_cases}")
    print(f"gepa readiness: {adjudicated}/{ADJUDICATED_GATE} adjudicated cases, holdout suite {holdout_state}")
    return 0


def cmd_draft(fly, args) -> int:
    try:
        from . import drafting
    except Exception as exc:
        print(f"agent-flywheel: drafting unavailable: {exc}", file=sys.stderr)
        return 2

    root = fly.host.evals_root
    conn = store.connect(fly.db_path)
    attempted = 0
    while True:
        result = drafting.run_attempt(fly, conn, case_id=args.case)
        if result is None:
            break
        attempted += 1
        suffix = f" ({result['error']})" if result.get("error") else ""
        print(f"{result['case_id']}: {result['status']}{suffix}")
        if args.case is not None:
            break
    if attempted == 0:
        print("no eligible candidate cases")
    return 0


def cmd_draft_list(fly, args) -> int:
    conn = store.connect(fly.db_path)
    rows = store.attempts(conn, status=args.status)
    width = max((len(r["case_id"]) for r in rows), default=4)
    print(f"{'case'.ljust(width)}  {'status':<14} {'outcome':<10} error")
    for row in rows:
        print(f"{row['case_id'].ljust(width)}  {row['status']:<14} {(row['outcome'] or '-'):<10} {row['error'] or ''}")
    return 0


def cmd_draft_retry(fly, args) -> int:
    try:
        from . import drafting
    except Exception as exc:
        print(f"agent-flywheel: drafting unavailable: {exc}", file=sys.stderr)
        return 2

    root = fly.host.evals_root
    conn = store.connect(fly.db_path)
    result = drafting.run_attempt(fly, conn, case_id=args.case_id)
    if result is None:
        print(f"{args.case_id}: not eligible (not a candidate, or already has a running attempt)")
        return 1
    suffix = f" ({result['error']})" if result.get("error") else ""
    print(f"{result['case_id']}: {result['status']}{suffix}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-flywheel")
    sub = parser.add_subparsers(dest="command", required=True)

    p_mark = sub.add_parser("mark", help="record a session outcome signal")
    p_mark.add_argument("kind", choices=["pass", "fail", "corrected", "abandoned", "escalated", "false-positive-eval"])
    p_mark.add_argument("--session", default="current")
    p_mark.add_argument("--component")
    p_mark.add_argument("--reason")
    p_mark.add_argument("--repo", dest="cwd", default=None)
    p_mark.set_defaults(func=cmd_mark)

    p_ingest = sub.add_parser("ingest", help="pull session metadata from local transcripts")
    p_ingest.add_argument("--since", default="7d")
    p_ingest.add_argument("--limit", type=int, default=None)
    p_ingest.add_argument("--transcript", help="ingest one transcript instead of scanning stores")
    p_ingest.add_argument(
        "--derive-signals",
        action="store_true",
        help="create open correction candidates from explicit user pushback",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_triage = sub.add_parser("triage", help="review open signals")
    p_triage.add_argument("--limit", type=int, default=None)
    p_triage.set_defaults(func=cmd_triage)

    p_adjudicate = sub.add_parser("adjudicate", help="classify open signals without prompting")
    p_adjudicate.add_argument("--shadow", action="store_true")
    p_adjudicate.add_argument("--force", action="store_true")
    p_adjudicate.add_argument("--model", default=None)
    p_adjudicate.add_argument("--timeout", type=int, default=None)
    p_adjudicate.add_argument("--min-signals", type=int, default=5)
    p_adjudicate.add_argument("--max-age-hours", type=int, default=24)
    p_adjudicate.add_argument("--batch-size", type=int, default=20)
    p_adjudicate.add_argument("--confidence", type=float, default=0.6)
    p_adjudicate.add_argument("--promotion-sessions", type=int, default=3)
    p_adjudicate.add_argument("--retry-hours", type=int, default=24)
    p_adjudicate.set_defaults(func=cmd_adjudicate)

    p_eval = sub.add_parser("eval", help="run baseline-vs-candidate eval cases")
    p_eval.add_argument("--base", default="HEAD")
    p_eval.add_argument("--candidate", default="HEAD")
    p_eval.add_argument("--suite", default=None)
    p_eval.add_argument("--online", action="store_true")
    p_eval.add_argument("--repo", default=None)
    p_eval.set_defaults(func=cmd_eval)

    p_status = sub.add_parser("status", help="print counts, the latest eval run, and gepa readiness")
    p_status.add_argument("--repo", default=None)
    p_status.set_defaults(func=cmd_status)

    p_draft = sub.add_parser(
        "draft", help="draft a fix + eval case for a candidate, prove it, and publish on a fixed verdict"
    )
    p_draft.add_argument("--case", default=None, help="draft this one case id, bypassing automatic eligibility history")
    p_draft.add_argument("--repo", default=None)
    p_draft.set_defaults(func=cmd_draft)

    p_draft_list = sub.add_parser("draft-list", help="list draft attempts")
    p_draft_list.add_argument("--status", default=None)
    p_draft_list.set_defaults(func=cmd_draft_list)

    p_draft_retry = sub.add_parser("draft-retry", help="re-queue one case for another draft attempt")
    p_draft_retry.add_argument("case_id")
    p_draft_retry.add_argument("--repo", default=None)
    p_draft_retry.set_defaults(func=cmd_draft_retry)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        fly = config.build(repo=getattr(args, "repo", None))
    except (OSError, ValueError) as exc:
        print(f"agent-flywheel: configuration failed: {exc}", file=sys.stderr)
        return 2
    return args.func(fly, args)


if __name__ == "__main__":
    raise SystemExit(main())
