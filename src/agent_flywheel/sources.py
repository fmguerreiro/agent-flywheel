"""Transcript sources: one per agent harness.

Everything that branched on harness in the original single-host version lives
here - metadata parsing, skill-call extraction, and the subagent test. What
did not branch stayed in `ingest`.

Roots are expanded when asked, never captured at construction, so a source
pointed at a throwaway home keeps answering after that home is created.
"""

from __future__ import annotations

import glob
import json
import os
import re

from . import ingest

OMP_ROOT = "~/.omp/agent/sessions"
CLAUDE_ROOT = "~/.claude/projects"

_COMMAND_NAME_RE = re.compile(r"<command-name>(/[^<]+)</command-name>")


def _within(path: str | os.PathLike, root: str) -> bool:
    try:
        resolved = os.path.realpath(os.fspath(path))
        base = os.path.realpath(os.path.expanduser(root))
    except OSError:
        return False
    return resolved == base or resolved.startswith(base + os.sep)


class OmpTranscripts:
    def __init__(self, *, root: str = OMP_ROOT, name: str = "omp") -> None:
        self.name = name
        self.root = root

    def sessions(self) -> list[tuple[str, str]]:
        # One glob level on purpose: omp keeps subagent transcripts a
        # directory deeper, and only top-level sessions are real sessions.
        base = os.path.expanduser(self.root)
        return [(p, self.name) for p in glob.glob(base + "/*/*.jsonl")]

    def owns(self, path: str | os.PathLike) -> bool:
        return _within(path, self.root)

    def meta(self, path: str | os.PathLike) -> dict:
        path = os.fspath(path)
        meta = ingest.empty_meta(path, self.name)
        got_session = got_model = False
        with open(path, errors="replace") as fh:
            for line in fh:
                if not got_session and '"session"' not in line:
                    continue
                if got_session and not got_model and '"model_change"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                t = obj.get("type")
                if t == "session" and not got_session:
                    meta["session_id"] = obj.get("id")
                    meta["cwd"] = obj.get("cwd")
                    meta["started_at"] = obj.get("timestamp")
                    got_session = True
                elif t == "model_change" and not got_model:
                    # A session can switch models mid-run; the first
                    # model_change is what the run started under.
                    meta["model"] = obj.get("model")
                    got_model = True
                if got_session and got_model:
                    break
        if meta["session_id"] is None:
            meta["session_id"] = os.path.basename(path)[: -len(".jsonl")]
        return meta

    def corrections(self, path: str | os.PathLike) -> list[dict]:
        return ingest.correction_candidates(path)

    def is_subagent(self, path: str | os.PathLike) -> bool:
        if not self.owns(path):
            return False
        base = os.path.realpath(os.path.expanduser(self.root))
        rel = os.path.relpath(os.path.realpath(os.fspath(path)), base)
        return len(rel.split(os.sep)) > 2

    def skills_used(self, path: str | os.PathLike) -> list[str]:
        return ingest.ordered_unique(_scan(path, _omp_skill_calls))


class ClaudeTranscripts:
    def __init__(self, *, root: str = CLAUDE_ROOT, name: str = "claude") -> None:
        self.name = name
        self.root = root

    def sessions(self) -> list[tuple[str, str]]:
        base = os.path.expanduser(self.root)
        return [(p, self.name) for p in glob.glob(base + "/**/*.jsonl", recursive=True)]

    def owns(self, path: str | os.PathLike) -> bool:
        return _within(path, self.root)

    def meta(self, path: str | os.PathLike) -> dict:
        path = os.fspath(path)
        meta = ingest.empty_meta(path, self.name)
        got_ids = got_model = False
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not got_ids:
                    sid = obj.get("sessionId")
                    cwd = obj.get("cwd")
                    version = obj.get("version")
                    if sid:
                        meta["session_id"] = sid
                    if cwd:
                        meta["cwd"] = cwd
                    if version:
                        meta["harness_version"] = version
                    if meta["started_at"] is None and obj.get("timestamp"):
                        meta["started_at"] = obj.get("timestamp")
                    if meta["session_id"] and meta["cwd"] and meta["harness_version"]:
                        got_ids = True
                if not got_model:
                    model = (obj.get("message") or {}).get("model")
                    if model:
                        meta["model"] = model
                        got_model = True
                if got_ids and got_model:
                    break
        if meta["session_id"] is None:
            meta["session_id"] = os.path.basename(path)[: -len(".jsonl")]
        return meta

    def corrections(self, path: str | os.PathLike) -> list[dict]:
        return ingest.correction_candidates(path)

    def is_subagent(self, path: str | os.PathLike) -> bool:
        return False

    def skills_used(self, path: str | os.PathLike) -> list[str]:
        def calls(obj: dict) -> list[str]:
            return _claude_skill_calls(obj) + _claude_slash_calls(obj)

        return ingest.ordered_unique(_scan(path, calls))


def _scan(path: str | os.PathLike, extract) -> list[str]:
    names: list[str] = []
    with open(os.fspath(path), errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            names.extend(extract(obj))
    return names


def _omp_skill_calls(obj: dict) -> list[str]:
    if obj.get("type") != "message":
        return []
    content = (obj.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "toolCall":
            continue
        if block.get("name") != "read":
            continue
        path = (block.get("arguments") or {}).get("path")
        if isinstance(path, str) and path.startswith("skill://"):
            names.append(path[len("skill://") :])
    return names


def _claude_skill_calls(obj: dict) -> list[str]:
    content = (obj.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "Skill":
            skill = (block.get("input") or {}).get("skill")
            if isinstance(skill, str):
                names.append(skill)
    return names


def _claude_slash_calls(obj: dict) -> list[str]:
    if obj.get("type") != "user":
        return []
    content = (obj.get("message") or {}).get("content")
    text = content if isinstance(content, str) else ""
    if not text and isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    names = []
    for m in _COMMAND_NAME_RE.finditer(text):
        name = m.group(1).lstrip("/")
        if name:
            names.append(name)
    return names


def default_sources() -> tuple[OmpTranscripts, ClaudeTranscripts, ClaudeTranscripts]:
    """The three stores the original scanned, in the order it scanned them."""
    return (
        OmpTranscripts(),
        ClaudeTranscripts(root="~/.claude/projects", name="claude"),
        ClaudeTranscripts(root="~/.claude-sakana/projects", name="claude-sakana"),
    )
