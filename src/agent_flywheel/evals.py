"""Baseline-vs-candidate eval runner for the agent-feedback flywheel.

Cases use a separate structural check or `verify.sh`, not model self-grading.
`compare()` reports "skipped" rather than guessing a verdict for cases that
did not run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import ingest, sources
from .host import HostProject, OnlineEvaluationHarness
from .runner import MODEL, copy_omp_credentials

import tomllib

# Structural commands and verify.sh are expected to be fast, local checks;
# online harness calls need real wall time for a model round trip.
CASE_TIMEOUT = 30
ONLINE_TIMEOUT = 300
_AUTONOMOUS_PATH = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Captured before any run can shadow it: an isolated env hands the harness a
# temporary HOME, and the real one is still needed to find credentials.
_REAL_HOME = Path.home()


@dataclass
class Case:
    id: str
    kind: str  # structural | dispatch | behavioral
    component: str
    severity: str
    harness: str  # omp | claude | any
    online: bool
    origin: str
    path: Path  # directory holding case.toml, prompt_file, fixture, verify
    command: str | None = None
    prompt_file: str | None = None
    expect_skill: list[str] = field(default_factory=list)
    reject_skill: list[str] = field(default_factory=list)
    fixture: str | None = None
    verify: str | None = None


def _required_case_field(value: str | None, case: Case, field: str) -> str:
    if value is None:
        raise ValueError(f"{case.kind} case {case.id!r} requires {field}")
    return value


@dataclass
class Result:
    passed: bool
    skipped: bool
    detail: str
    metrics: dict


def _parse_case(case_dir: Path, data: dict) -> Case:
    kind = data["kind"]
    section = data.get(kind, {})
    case_id = data["id"]
    if case_id != case_dir.name:
        raise ValueError(
            f"case id {case_id!r} does not match directory {case_dir.name!r}"
        )
    return Case(
        id=case_id,
        kind=kind,
        component=data["component"],
        severity=data["severity"],
        harness=data.get("harness", "any"),
        online=bool(data.get("online", False)),
        origin=data.get("origin", ""),
        path=case_dir,
        command=section.get("command"),
        prompt_file=section.get("prompt_file"),
        expect_skill=list(section.get("expect_skill", [])),
        reject_skill=list(section.get("reject_skill", [])),
        fixture=section.get("fixture"),
        verify=section.get("verify"),
    )


def load_suite(
    root: Path, suite: str | None = None, *, case_id: str | None = None
) -> list[Case]:
    root = Path(root)
    suite = suite or "default"
    with open(root / "suites.toml", "rb") as f:
        suites = tomllib.load(f)
    if suite not in suites:
        raise KeyError(f"unknown suite {suite!r} (have: {', '.join(sorted(suites))})")
    include = suites[suite].get("include", "all")
    holdout_ids = set(suites.get("holdout", {}).get("cases", []))

    cases = []
    for case_toml in sorted((root / "cases").glob("*/case.toml")):
        with open(case_toml, "rb") as f:
            data = tomllib.load(f)
        case = _parse_case(case_toml.parent, data)
        if case_id is not None and case.id != case_id:
            continue
        if include == "offline" and case.online:
            continue
        # holdout is an allowlist reached only by name, never by "all": an
        # optimizer that asks for "all" must never see the cases it's meant
        # to be graded against.
        if suite == "holdout":
            if case.id not in holdout_ids:
                continue
        elif case.id in holdout_ids:
            continue
        cases.append(case)
    # An unmatched case_id must be loud: a drafter's own eval step asking for
    # its one just-written case must never silently fall through to "run
    # nothing" and be misread as a clean, empty pass.
    if case_id is not None and not cases:
        raise KeyError(f"case {case_id!r} not found (or excluded by suite {suite!r})")
    return cases


def _resolve_harness(harness: str) -> str | None:
    candidates = ("omp", "claude") if harness == "any" else (harness,)
    for name in candidates:
        if shutil.which(name):
            return name
    return None


def _harness_command(harness_bin: str, prompt: str) -> list[str]:
    if harness_bin == "omp":
        args = [
            harness_bin,
            "-p",
            "--approval-mode=write",
            "--tools=read,edit,write,grep,glob",
        ]
        if model := _pinned_omp_model():
            args.append(f"--model={model}")
        return [*args, prompt]
    return [harness_bin, "-p", prompt]


def _sbpl_subpath(path: Path) -> str:
    return f"(subpath {json.dumps(str(path.resolve()))})"


def _structural_sandbox_profile(*, tree: Path, case: Case, home: Path) -> str:
    protected_roots = (
        Path("/Users"),
        Path("/Volumes"),
        Path("/private/tmp"),
        Path("/private/var/folders"),
    )
    allowed_roots = (tree, case.path, home)
    protected = "\n".join(f"  {_sbpl_subpath(path)}" for path in protected_roots)
    allowed = "\n".join(f"  {_sbpl_subpath(path)}" for path in allowed_roots)
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process-exec process-fork)\n"
        "(allow signal (target self))\n"
        "(allow sysctl-read)\n"
        "(allow file-read*)\n"
        f"(deny file-read-data\n{protected})\n"
        f"(allow file-read-data\n{allowed})\n"
        f'(allow file-write*\n{allowed}\n  (literal "/dev/null"))\n'
    )


def _online_sandbox_profile(
    *, tree: Path, case: Case, home: Path, cwd: Path, harness_bin: str
) -> str:
    protected_roots = (
        Path("/Users"),
        Path("/Volumes"),
        Path("/private/tmp"),
        Path("/private/var/folders"),
    )
    readable_roots = (tree, case.path, home, cwd)
    writable_roots = (home, cwd)
    readable_files = [Path(shutil.which(harness_bin) or harness_bin).resolve()]
    protected = "\n".join(f"  {_sbpl_subpath(path)}" for path in protected_roots)
    readable = "\n".join(f"  {_sbpl_subpath(path)}" for path in readable_roots)
    files = "\n".join(f"  (literal {json.dumps(str(path))})" for path in readable_files)
    writable = "\n".join(f"  {_sbpl_subpath(path)}" for path in writable_roots)
    return (
        "(version 1)\n"
        "(allow default)\n"
        f"(deny file-read-data\n{protected})\n"
        f"(allow file-read-data\n{readable}\n{files})\n"
        "(deny file-write*)\n"
        f'(allow file-write*\n{writable}\n  (literal "/dev/null"))\n'
    )


def _run_online_harness(
    *,
    case: Case,
    tree: Path,
    home: Path,
    cwd: Path,
    harness_bin: str,
    prompt: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    args = _harness_command(harness_bin, prompt)
    if harness_bin == "omp":
        sandbox = shutil.which("sandbox-exec")
        if sandbox is None:
            raise RuntimeError("sandbox-exec is required for online omp evaluations")
        args = [
            sandbox,
            "-p",
            _online_sandbox_profile(
                tree=tree,
                case=case,
                home=home,
                cwd=cwd,
                harness_bin=harness_bin,
            ),
            *args,
        ]
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=ONLINE_TIMEOUT,
    )


def _run_check(
    command: str,
    case: Case,
    cwd: Path,
    *,
    tree: Path | None = None,
    home: Path | None = None,
    autonomous: bool = False,
) -> subprocess.CompletedProcess:
    env = {
        **_base_env(),
        "HOME": str(home or _REAL_HOME),
        "CASE_DIR": str(case.path.resolve()),
    }
    args = command
    if autonomous:
        if tree is None or home is None:
            raise ValueError("autonomous structural checks require tree and home")
        env["PATH"] = _AUTONOMOUS_PATH
        args = [
            "sandbox-exec",
            "-p",
            _structural_sandbox_profile(tree=tree, case=case, home=home),
            "/bin/sh",
            "-c",
            command,
        ]
    return subprocess.run(
        args,
        shell=not autonomous,
        cwd=cwd,
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=CASE_TIMEOUT,
    )


# Every `<case_dir>/negative*/` fixture must fail.
def _run_structural(
    case: Case, tree: Path, home: Path, *, autonomous: bool = False
) -> Result:
    try:
        proc = _run_check(
            case.command, case, tree, tree=tree, home=home, autonomous=autonomous
        )
    except subprocess.TimeoutExpired:
        return Result(
            False, False, f"command timed out after {CASE_TIMEOUT}s", {"guarded": False}
        )
    positive_ok = proc.returncode == 0
    detail = "ok" if positive_ok else (proc.stdout + proc.stderr).strip()[:500]
    metrics = {"returncode": proc.returncode}

    negative_dirs = [case.path / "negative"]
    negative_dirs.extend(sorted(case.path.glob("negative-*")))
    negative_dirs = [path for path in negative_dirs if path.is_dir()]
    if not negative_dirs:
        metrics["guarded"] = False
        if positive_ok:
            detail = (
                "ok (UNGUARDED: no negative fixture, check never proven able to fail)"
            )
        return Result(positive_ok, False, detail, metrics)

    metrics["guarded"] = True
    returncodes = {}
    timed_out = []
    uncaught = []
    for negative_dir in negative_dirs:
        try:
            neg_proc = _run_check(
                case.command,
                case,
                negative_dir,
                tree=tree,
                home=home,
                autonomous=autonomous,
            )
        except subprocess.TimeoutExpired:
            timed_out.append(negative_dir.name)
            continue
        returncodes[negative_dir.name] = neg_proc.returncode
        if neg_proc.returncode == 0:
            uncaught.append(negative_dir.name)

    if returncodes:
        metrics["negative_returncode"] = next(iter(returncodes.values()))
    metrics["negative_returncodes"] = returncodes
    if timed_out:
        return Result(
            False,
            False,
            f"negative controls timed out: {', '.join(timed_out)}",
            metrics,
        )
    if uncaught:
        return Result(
            False,
            False,
            f"negative controls did not fail: {', '.join(uncaught)}",
            metrics,
        )
    if not positive_ok:
        return Result(False, False, detail, metrics)
    return Result(True, False, "ok", metrics)


def _claude_auth_env() -> dict[str, str]:
    # claude's OAuth session lives in the macOS keychain keyed to the OS
    # user, not $HOME, but the CLI still reports "Not logged in" under an
    # isolated $HOME. CLAUDE_CODE_OAUTH_TOKEN is the same headless path
    # `claude setup-token` documents, so read the token straight out of the
    # keychain item the real interactive login already created.
    if shutil.which("security") is None:
        return {}
    try:
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-w",
                "-s",
                "Claude Code-credentials",
                "-a",
                os.environ.get("USER", ""),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        token = json.loads(proc.stdout)["claudeAiOauth"]["accessToken"]
    except (ValueError, KeyError):
        return {}
    return {"CLAUDE_CODE_OAUTH_TOKEN": token}


def _harness_auth_env(harness_bin: str, home: Path) -> dict[str, str]:
    if harness_bin == "omp":
        copy_omp_credentials(home, source_home=_REAL_HOME)
        return _claude_auth_env()
    if harness_bin == "claude":
        return _claude_auth_env()
    return {}


# The environment any case runs in is built up from nothing, not inherited
# and then stripped. A denylist admits the next variable that points at
# live config or credentials. Everything a case may reach is either named
# here or bridged deliberately. Notably absent, and meant to be:
# SSH_AUTH_SOCK, AWS_*, and every *_API_KEY the parent shell carries.
_ENV_ALLOWLIST = (
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TZ",
)

# Accepted risk, not an oversight, and online-only: the dispatch prompts
# describe real GitHub triage, and with no credential the harness refuses
# the task and dispatches nothing (measured: 0/3 runs routed without one,
# 2/2 with the parent environment), so the case would grade credential
# presence instead of skill routing. An online case therefore runs with
# whatever GitHub access the invoking shell has, writes included.
# Structural commands never need it. See specs/agent-evals/README.md,
# "Isolation boundary".
_ONLINE_CREDENTIAL_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_PAT",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)


def _base_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}


def _isolated_env(harness_bin: str, home: Path) -> dict[str, str]:
    env = _base_env()
    env.update({k: os.environ[k] for k in _ONLINE_CREDENTIAL_VARS if k in os.environ})
    env["HOME"] = str(home)
    tmpdir = home / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmpdir)
    env.update(_harness_auth_env(harness_bin, home))
    return env


# A run that never reached the pinned provider/model must not be scored as a
# case failure. The final assistant turn is the transcript's outcome.
def _final_assistant_message(transcript_path: str) -> tuple[dict | None, str | None]:
    final = None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    msg = json.loads(line).get("message") or {}
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    final = msg
    except OSError as exc:
        return None, str(exc)
    return final, None

_COMPLETED_STOP_REASONS = {"stop"}


def _provider_error(transcript_path: str) -> str | None:
    msg, read_error = _final_assistant_message(transcript_path)
    if read_error is not None:
        return read_error
    if msg is None:
        return "no assistant turn"
    stop_reason = msg.get("stopReason")
    if stop_reason in {"error", "aborted"} or msg.get("errorStatus"):
        return (
            msg.get("errorMessage")
            or (f"status {msg['errorStatus']}" if msg.get("errorStatus") else None)
            or f"stopReason {stop_reason}"
        )
    if stop_reason not in _COMPLETED_STOP_REASONS:
        return f"incomplete stopReason {stop_reason}"
    return None


_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"}


def _provider_identity(transcript_path: str) -> tuple[str, str] | None:
    msg, read_error = _final_assistant_message(transcript_path)
    if (
        read_error is not None
        or msg is None
        or msg.get("stopReason") not in _COMPLETED_STOP_REASONS
        or msg.get("errorStatus")
        or not isinstance(msg.get("provider"), str)
        or not isinstance(msg.get("model"), str)
    ):
        return None
    return msg["provider"], msg["model"]


def _expected_model_identity(spec: str) -> tuple[str, str] | None:
    if "/" not in spec:
        return None
    provider, model = spec.split("/", 1)
    base, separator, suffix = model.rpartition(":")
    if separator and suffix in _THINKING_LEVELS:
        model = base
    return (provider, model) if provider and model else None


def _sessions_under(home: Path, limit: int | None = None) -> list[dict]:
    """Transcripts under a throwaway HOME, newest first.

    The original swapped os.environ["HOME"] and re-read the default roots.
    Sources carry their root explicitly, so point them at this home: no
    global mutation, and correct if two cases ever run at once.
    """
    roots = (
        sources.OmpTranscripts(root=str(home / ".omp" / "agent" / "sessions")),
        sources.ClaudeTranscripts(root=str(home / ".claude" / "projects")),
    )
    return ingest.recent_sessions(roots, limit=limit)


def _skills_under(home: Path, transcript: str) -> list[str]:
    roots = (
        sources.OmpTranscripts(root=str(home / ".omp" / "agent" / "sessions")),
        sources.ClaudeTranscripts(root=str(home / ".claude" / "projects")),
    )
    return ingest.skills_used(roots, transcript)


def _reached_provider(home: Path, harness_bin: str) -> str | None:
    sessions = _sessions_under(home, limit=1)
    if not sessions:
        return "harness produced no session"
    transcript = sessions[0]["transcript_path"]
    if error := _provider_error(transcript):
        return error
    if harness_bin != "omp":
        return None
    expected_spec = _pinned_omp_model()
    expected = _expected_model_identity(expected_spec) if expected_spec else None
    if expected is None:
        return "pinned OMP provider/model is unavailable"
    actual = _provider_identity(transcript)
    if actual is None:
        return "assistant turn omitted provider/model"
    if actual != expected:
        return f"resolved {'/'.join(actual)}, expected {'/'.join(expected)}"
    return None


def _harness_failure(proc: subprocess.CompletedProcess) -> Result | None:
    if proc.returncode == 0:
        return None
    output = (proc.stdout + proc.stderr).strip()
    detail = f": {output[:450]}" if output else ""
    return Result(
        False,
        True,
        f"infra: harness exited {proc.returncode}{detail}",
        {"infra": True, "returncode": proc.returncode},
    )


def _run_external_dispatch(
    case: Case, tree: Path, home: Path, harness: OnlineEvaluationHarness
) -> Result:
    prompt_file = _required_case_field(case.prompt_file, case, "prompt_file")
    evaluation = harness.run(
        (case.path / prompt_file).read_text(), cwd=tree, home=home
    )
    if not evaluation.succeeded:
        return Result(False, True, f"infra: {evaluation.detail}", {"infra": True})
    evidence = list(evaluation.dispatch_evidence)
    expect_ok = not case.expect_skill or any(
        skill in evidence for skill in case.expect_skill
    )
    reject_ok = not any(skill in evidence for skill in case.reject_skill)
    return Result(
        expect_ok and reject_ok,
        False,
        f"skills_used={evidence}",
        {"skills_used": evidence},
    )


def _run_external_behavioral(
    case: Case, home: Path, harness: OnlineEvaluationHarness
) -> Result:
    prompt_file = _required_case_field(case.prompt_file, case, "prompt_file")
    fixture = _required_case_field(case.fixture, case, "fixture")
    verify = _required_case_field(case.verify, case, "verify")
    with tempfile.TemporaryDirectory(prefix="agent-eval-behavioral-") as scratch_dir:
        scratch = Path(scratch_dir)
        fixture_src = case.path / fixture
        if fixture_src.is_dir():
            shutil.copytree(fixture_src, scratch, dirs_exist_ok=True)

        evaluation = harness.run(
            (case.path / prompt_file).read_text(), cwd=scratch, home=home
        )
        if not evaluation.succeeded:
            return Result(False, True, f"infra: {evaluation.detail}", {"infra": True})

        try:
            proc = subprocess.run(
                ["bash", str(case.path / verify)],
                cwd=scratch,
                check=False,
                capture_output=True,
                text=True,
                timeout=CASE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return Result(False, False, "verify.sh timed out", {})
        passed = proc.returncode == 0
        detail = "ok" if passed else (proc.stdout + proc.stderr).strip()[:500]
        return Result(passed, False, detail, {"returncode": proc.returncode})


def _external_harness(
    name: str, online_harnesses: tuple[OnlineEvaluationHarness, ...]
) -> OnlineEvaluationHarness | None:
    if name == "any":
        return None
    return next((harness for harness in online_harnesses if harness.name == name), None)


def _run_dispatch(case: Case, tree: Path, home: Path, harness_bin: str) -> Result:
    prompt = (case.path / case.prompt_file).read_text()
    env = _isolated_env(harness_bin, home)
    try:
        harness_proc = _run_online_harness(
            case=case,
            tree=tree,
            home=home,
            cwd=tree,
            harness_bin=harness_bin,
            prompt=prompt,
            env=env,
        )
    except RuntimeError as exc:
        return Result(False, True, f"infra: {exc}", {"infra": True})
    except subprocess.TimeoutExpired:
        return Result(False, False, "harness timed out", {})
    if failure := _harness_failure(harness_proc):
        return failure

    infra = _reached_provider(home, harness_bin)
    if infra is not None:
        return Result(False, True, f"infra: {infra}", {"infra": True})

    sessions = _sessions_under(home, limit=1)
    used = _skills_under(home, sessions[0]["transcript_path"])
    expect_ok = not case.expect_skill or any(s in used for s in case.expect_skill)
    reject_ok = not any(s in used for s in case.reject_skill)
    return Result(
        expect_ok and reject_ok, False, f"skills_used={used}", {"skills_used": used}
    )


def _run_behavioral(case: Case, tree: Path, home: Path, harness_bin: str) -> Result:
    with tempfile.TemporaryDirectory(prefix="agent-eval-behavioral-") as scratch_dir:
        scratch = Path(scratch_dir)
        fixture_src = case.path / case.fixture
        if fixture_src.is_dir():
            shutil.copytree(fixture_src, scratch, dirs_exist_ok=True)

        prompt = (case.path / case.prompt_file).read_text()
        env = _isolated_env(harness_bin, home)
        try:
            harness_proc = _run_online_harness(
                case=case,
                tree=tree,
                home=home,
                cwd=scratch,
                harness_bin=harness_bin,
                prompt=prompt,
                env=env,
            )
        except RuntimeError as exc:
            return Result(False, True, f"infra: {exc}", {"infra": True})
        except subprocess.TimeoutExpired:
            return Result(False, False, "harness timed out", {})
        if failure := _harness_failure(harness_proc):
            return failure

        infra = _reached_provider(home, harness_bin)
        if infra is not None:
            return Result(False, True, f"infra: {infra}", {"infra": True})

        try:
            proc = subprocess.run(
                ["bash", str(case.path / case.verify)],
                cwd=scratch,
                check=False,
                capture_output=True,
                text=True,
                timeout=CASE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return Result(False, False, "verify.sh timed out", {})
        passed = proc.returncode == 0
        detail = "ok" if passed else (proc.stdout + proc.stderr).strip()[:500]
        return Result(passed, False, detail, {"returncode": proc.returncode})


def run_case(
    case: Case,
    *,
    tree: Path,
    home: Path,
    online: bool,
    autonomous: bool = False,
    online_harnesses: tuple[OnlineEvaluationHarness, ...] = (),
) -> Result:
    tree, home = Path(tree), Path(home)
    if case.kind == "structural":
        return _run_structural(case, tree, home, autonomous=autonomous)

    if autonomous:
        return Result(
            False,
            True,
            f"{case.kind} cases are not allowed for autonomous drafting",
            {},
        )
    if not online:
        return Result(
            False, True, f"{case.kind} case skipped (pass --online to run)", {}
        )

    if harness := _external_harness(case.harness, online_harnesses):
        if case.kind == "dispatch":
            return _run_external_dispatch(case, tree, home, harness)
        if case.kind == "behavioral":
            return _run_external_behavioral(case, home, harness)
        raise ValueError(f"unknown case kind {case.kind!r}")

    if case.harness not in {"omp", "claude", "any"}:
        return Result(False, True, f"harness {case.harness!r} is not configured", {})

    harness_bin = _resolve_harness(case.harness)
    if harness_bin is None:
        return Result(False, True, f"harness {case.harness!r} not installed", {})

    if case.kind == "dispatch":
        return _run_dispatch(case, tree, home, harness_bin)
    if case.kind == "behavioral":
        return _run_behavioral(case, tree, home, harness_bin)
    raise ValueError(f"unknown case kind {case.kind!r}")


def _materialize(repo: Path, ref: str, dest: Path) -> None:
    # `git worktree add` needs matching bookkeeping and cleanup, and this repo
    # already carries ~20 live worktrees plus the user's dirty tree. `git
    # archive | tar -x` is a one-shot untar into a scratch dir that touches
    # neither, so it needs no cleanup and can't collide with in-progress work.
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", ref], cwd=repo, capture_output=True, check=True
    )
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)


# Held fixed across both arms: the model config is not the artifact under test.
# A revision carries its own config.yml, so materializing it would vary the
# model alongside the skills and confound every verdict.
def _pinned_omp_config() -> Path | None:
    override = os.environ.get("AGENT_FLYWHEEL_EVAL_CONFIG")
    if override:
        return Path(override).expanduser()
    live = _REAL_HOME / ".omp" / "agent" / "config.yml"
    return live if live.is_file() else None


def _pinned_omp_model() -> str | None:
    if override := os.environ.get("AGENT_FLYWHEEL_EVAL_MODEL"):
        return override
    return MODEL

def _setup_home(host: HostProject, tree: Path, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    host.stage_home(tree, home)
    # Pinning is eval semantics, not host layout: it holds the model fixed
    # across the two arms so a verdict reflects the diff, not a model change.
    agent_dst = home / ".omp" / "agent"
    if agent_dst.is_dir():
        pinned = _pinned_omp_config()
        if pinned is not None:
            shutil.copyfile(pinned, agent_dst / "config.yml")


def _prepare_revision(host: HostProject, repo: Path, ref: str) -> tuple[Path, Path]:
    tree = Path(tempfile.mkdtemp(prefix="agent-eval-tree-"))
    home = Path(tempfile.mkdtemp(prefix="agent-eval-home-"))
    _materialize(repo, ref, tree)
    _setup_home(host, tree, home)
    return tree, home


def _verdict(case: Case, base: Result, candidate: Result) -> dict:
    # Never claim fixed/regressed/same-* for a case that did not actually run.
    if base.skipped or candidate.skipped:
        detail = base.detail if base.skipped else candidate.detail
        return {
            "id": case.id,
            "kind": case.kind,
            "base": None,
            "candidate": None,
            "verdict": "skipped",
            "detail": detail,
        }
    if base.passed == candidate.passed:
        verdict = "same-pass" if base.passed else "same-fail"
    elif candidate.passed:
        verdict = "fixed"
    else:
        verdict = "regressed"
    return {
        "id": case.id,
        "kind": case.kind,
        "base": base.passed,
        "candidate": candidate.passed,
        "verdict": verdict,
        "guarded": candidate.metrics.get("guarded"),
        "negative_controls": len(candidate.metrics.get("negative_returncodes", {})),
    }


def compare(
    root: Path,
    *,
    host: HostProject,
    base: str,
    candidate: str,
    suite: str | None = None,
    online: bool = False,
    case_id: str | None = None,
    autonomous: bool = False,
    case_source: str | None = None,
    online_harnesses: tuple[OnlineEvaluationHarness, ...] = (),
) -> list[dict]:
    root = Path(root)
    repo = Path(
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )

    base_tree = base_home = candidate_tree = candidate_home = case_tree = case_home = (
        None
    )
    try:
        base_tree, base_home = _prepare_revision(host, repo, base)
        candidate_tree, candidate_home = _prepare_revision(host, repo, candidate)
        evals_relative = root.resolve().relative_to(repo.resolve())
        if case_source is not None and case_source != candidate:
            case_tree, case_home = _prepare_revision(host, repo, case_source)
        cases_root = (case_tree or candidate_tree) / evals_relative
        cases = load_suite(cases_root, suite, case_id=case_id)
        results = []
        for case in cases:
            base_result = run_case(
                case,
                tree=base_tree,
                home=base_home,
                online=online,
                online_harnesses=online_harnesses,
                autonomous=autonomous,
            )
            candidate_result = run_case(
                case,
                tree=candidate_tree,
                home=candidate_home,
                online=online,
                online_harnesses=online_harnesses,
                autonomous=autonomous,
            )
            results.append(_verdict(case, base_result, candidate_result))
        return results
    finally:
        for path in (
            base_tree,
            base_home,
            candidate_tree,
            candidate_home,
            case_tree,
            case_home,
        ):
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)


def record_results(
    conn, root: Path, results: list[dict], *, base: str, candidate: str
) -> dict:
    """Persist each non-skipped verdict and activate a case on `fixed`.
    Shared by `agent-feedback eval` and the drafter's publish step, so there
    is exactly one place that decides "fixed means active".
    """
    import tomllib

    from . import store

    root = Path(root)
    for row in results:
        if row["verdict"] == "skipped":
            continue
        component = severity = None
        case_toml = root / "cases" / row["id"] / "case.toml"
        if case_toml.is_file():
            with open(case_toml, "rb") as f:
                data = tomllib.load(f)
            component, severity = data.get("component"), data.get("severity")
        store.ensure_case_stub(conn, row["id"], component=component, severity=severity)
        store.record_eval_run(
            conn,
            case_id=row["id"],
            baseline_sha=base,
            candidate_sha=candidate,
            model=None,
            result=row["verdict"],
        )
        if row["verdict"] == "fixed":
            store.activate_case(conn, row["id"])

    return {
        "fixed": sum(1 for r in results if r["verdict"] == "fixed"),
        "regressed": sum(1 for r in results if r["verdict"] == "regressed"),
        "skipped": sum(1 for r in results if r["verdict"] == "skipped"),
    }
