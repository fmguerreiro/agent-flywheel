"""Read metadata, skill use, and correction candidates from agent sessions.

Metadata parsing streams only the early records. Correction extraction reads
the full transcript, excludes known harness-authored user rows, and emits
triage candidates rather than treating matched prose as ground truth.

Functions here take the sources they work over rather than a whole Flywheel:
the eval runner points a source at a throwaway home to find what a test run
produced, and there is no Flywheel for that.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Sequence

from .host import TranscriptSource

STALE_SECONDS = 12 * 60 * 60

_CORRECTION_RE = re.compile(
    r"^\s*(?:no[,!.]|nope\b|don't\b|do not\b|stop\b|wrong\b|actually(?:[,:]|\s)|"
    r"that's not\b|that is not\b|revert\b|undo\b|why did you\b)",
    re.IGNORECASE,
)
_CORRECTION_ANYWHERE_RE = re.compile(
    r"\b(?:why did you|what the fuck|don'?t use|do not use)\b",
    re.IGNORECASE,
)
_NON_HUMAN_PREFIXES = (
    "<system-notice>",
    "### Session update",
    "<system-reminder>",
    "Current interruptible wait interrupted:",
    "Stop hook feedback:",
    'Do not use tools. Reply on the first line with exactly "auto-compact-continued"',
)
_PAYLOAD_TEXT_LIMIT = 2_000


def empty_meta(path: str, harness: str) -> dict:
    return {
        "harness": harness,
        "session_id": None,
        "cwd": None,
        "transcript_path": path,
        "model": None,
        "harness_version": None,
        "started_at": None,
    }


def ordered_unique(names: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _owner(sources: Sequence[TranscriptSource], path: str | os.PathLike) -> TranscriptSource:
    for source in sources:
        if source.owns(path):
            return source
    raise ValueError(f"no transcript source owns {os.fspath(path)!r}")


def session_meta(sources: Sequence[TranscriptSource], transcript_path: str | os.PathLike) -> dict:
    """The seven fixed keys for one transcript, streaming the file.

    Stops reading as soon as the source has what it needs: the stores are
    gigabytes and most of what we want lands in the first few lines.
    """
    return _owner(sources, transcript_path).meta(transcript_path)


def is_subagent_session(
    sources: Sequence[TranscriptSource], transcript_path: str | os.PathLike
) -> bool:
    """A delegated child session. Corrections inside one are aimed at its
    parent's instructions, not at the user's, so callers drop them.
    """
    for source in sources:
        if source.owns(transcript_path):
            return source.is_subagent(transcript_path)
    return False


def skills_used(
    sources: Sequence[TranscriptSource], transcript_path: str | os.PathLike
) -> list[str]:
    return _owner(sources, transcript_path).skills_used(transcript_path)


def session_files(sources: Sequence[TranscriptSource]) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for source in sources:
        files.extend(source.sessions())
    return files


def current_session(
    sources: Sequence[TranscriptSource], cwd: str | None = None
) -> dict | None:
    """Newest session file recorded against `cwd`, modified in the last 12h.

    A stale match is worse than None: `mark --session current` writes a
    signal against whatever this returns, and attributing today's feedback to
    a session from last week silently corrupts the case it gets promoted
    into. So an old match is treated the same as no match.
    """
    target = os.path.realpath(cwd or os.getcwd())
    now = time.time()
    best_path = None
    best_mtime = -1.0
    best_harness = None
    for path, harness in session_files(sources):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime > STALE_SECONDS:
            continue
        if mtime <= best_mtime:
            continue
        meta = session_meta(sources, path)
        recorded_cwd = meta.get("cwd")
        if not recorded_cwd:
            continue
        if os.path.realpath(recorded_cwd) != target:
            continue
        best_path, best_mtime, best_harness = path, mtime, harness
    if best_path is None:
        return None
    meta = session_meta(sources, best_path)
    meta["harness"] = best_harness
    return meta


def _since_cutoff(since: str | None) -> float:
    if since is None:
        return 0.0
    if since.endswith("d") and since[:-1].isdigit():
        return time.time() - int(since[:-1]) * 86400
    if since.endswith("h") and since[:-1].isdigit():
        return time.time() - int(since[:-1]) * 3600
    # ISO date/datetime, e.g. "2026-08-01"
    from datetime import datetime, timezone

    return datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp()


def recent_sessions(
    sources: Sequence[TranscriptSource],
    since: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    cutoff = _since_cutoff(since)
    rows = []
    for path, harness in session_files(sources):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        rows.append((mtime, path, harness))
    rows.sort(key=lambda r: r[0], reverse=True)
    if limit is not None:
        rows = rows[:limit]
    out = []
    for _, path, harness in rows:
        try:
            meta = session_meta(sources, path)
        except OSError:
            continue
        meta["harness"] = harness
        out.append(meta)
    return out


def _content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") in ("text", "input_text")
    ).strip()


def _assistant_snapshot(obj: dict) -> dict | None:
    message = obj.get("message") or {}
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    tools = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in ("toolCall", "tool_use") or not block.get("name"):
                continue
            name = block["name"]
            skill = (block.get("input") or {}).get("skill")
            path = (block.get("arguments") or {}).get("path")
            if name == "read" and isinstance(path, str) and path.startswith("skill://"):
                name, skill = "Skill", path.removeprefix("skill://").split("/", 1)[0]
            tools.append(f"{name}:{skill}" if name == "Skill" and skill else name)
    return {
        "text": _content_text(content)[:_PAYLOAD_TEXT_LIMIT],
        "tool_calls": tools,
    }


def correction_candidates(transcript_path: str | os.PathLike) -> list[dict]:
    """Candidate user corrections paired with the preceding assistant action.

    These are triage inputs, not labels: the anchored phrases intentionally
    trade recall for fewer ordinary follow-up prompts. Both transcript
    schemas are normalised here, which is why this takes no source.
    """
    candidates = []
    previous_assistant = None
    with open(transcript_path, errors="replace") as fh:
        for line_number, line in enumerate(fh, 1):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            assistant = _assistant_snapshot(obj)
            if assistant is not None:
                if previous_assistant is None:
                    previous_assistant = assistant
                else:
                    if assistant["text"]:
                        previous_assistant["text"] = assistant["text"]
                    previous_assistant["tool_calls"].extend(
                        name
                        for name in assistant["tool_calls"]
                        if name not in previous_assistant["tool_calls"]
                    )
                continue
            message = obj.get("message") or {}
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = _content_text(message.get("content"))
            if not text:
                continue
            is_correction = (
                previous_assistant is not None
                and not text.startswith(_NON_HUMAN_PREFIXES)
                and (_CORRECTION_RE.match(text) or _CORRECTION_ANYWHERE_RE.search(text))
            )
            if is_correction:
                candidates.append(
                    {
                        "turn_id": obj.get("uuid") or obj.get("id") or f"line-{line_number}",
                        "occurred_at": obj.get("timestamp") or message.get("timestamp"),
                        "correction": text[:_PAYLOAD_TEXT_LIMIT],
                        "assistant": previous_assistant,
                    }
                )
            previous_assistant = None
    return candidates
