"""The omp CLI as a `ModelRunner`: the two call shapes and their parsing.

`classify` runs a tool-less model and must get one JSON object back.
`draft_argv` builds the command for a file-editing agent but never runs it,
so the caller can wrap it in a sandbox.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

MODEL = "openai-codex/gpt-5.6-terra"
CLASSIFY_TIMEOUT = 300
_OMP_AGENT_AUTH_FILES = ("agent.db",)
_OMP_AUTH_FILES = ("auth-broker.token", "auth-gateway.token", "install-id")
_ENV_ALLOWLIST = ("PATH", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "TZ", "CLAUDE_CONFIG_DIR")


class OmpRunner:
    def __init__(self, *, model: str | None = None, timeout: int = CLASSIFY_TIMEOUT) -> None:
        self.model_id = model
        self.timeout = timeout

    def draft_output(self, on_activity=None):
        return _DraftOutput(on_activity)

    def classify(self, payload: dict, *, system: str, timeout: int | None = None) -> dict:
        timeout = self.timeout if timeout is None else timeout
        workdir = tempfile.gettempdir()
        command = [
            "omp",
            "-p",
            "--mode",
            "json",
            "--no-tools",
            "--no-session",
            "--no-skills",
            "--no-rules",
            "--cwd",
            workdir,
            *(["--model", self.model_id] if self.model_id is not None else []),
            "--max-time",
            str(timeout),
            "--system-prompt",
            system,
            json.dumps(payload, sort_keys=True),
        ]
        completed = subprocess.run(
            command,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=True,
        )
        return _extract_decision(completed.stdout)

    def draft_argv(
        self, prompt_path: Path, *, cwd: Path, tools: str, timeout: int, scratch: Path
    ) -> list[str]:
        omp_bin = shutil.which("omp")
        if omp_bin is None:
            raise RuntimeError("omp not found on PATH")
        runtime_home = prepare_runtime_home(scratch)
        # The env the original passed to subprocess.run, carried as argv so the
        # sandbox can wrap the whole thing.
        env = [f"{key}={os.environ[key]}" for key in _ENV_ALLOWLIST if key in os.environ]
        return [
            "/usr/bin/env",
            "-i",
            *env,
            f"HOME={runtime_home}",
            f"TMPDIR={scratch}",
            omp_bin, "-p", f"@{prompt_path}",
            "--cwd", str(cwd),
            "--mode", "json",
            *(["--model", self.model_id] if self.model_id is not None else []),
            "--tools", tools,
            "--no-skills", "--no-rules",
            "--auto-approve", "--no-session",
            "--max-time", str(timeout),
        ]


class _DraftOutput:
    """Decode OMP events without mixing tool results or thinking into the answer."""

    def __init__(self, on_activity=None):
        self.on_activity = on_activity
        self.pending = ""
        self.text = ""
        self.current = ""

    def feed(self, chunk: str) -> None:
        self.pending += chunk
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self._event(line)

    def finish(self) -> str:
        if self.pending:
            self._event(self.pending)
            self.pending = ""
        return self.current or self.text

    def _activity(self, text: str) -> None:
        if self.on_activity is not None and text:
            self.on_activity(text)

    def _event(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, Mapping):
            return
        kind = event.get("type")
        if kind == "tool_execution_start":
            name = event.get("toolName")
            if isinstance(name, str):
                self._activity(f"Using {name}")
        elif kind == "message_start":
            message = event.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                self.current = ""
                self.text = ""
        elif kind == "message_update":
            update = event.get("assistantMessageEvent")
            if isinstance(update, Mapping) and update.get("type") == "text_delta":
                delta = update.get("delta")
                if isinstance(delta, str):
                    self.current += delta
                    self._activity(self.current)
        elif kind == "message_end":
            message = event.get("message")
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                return
            content = message.get("content")
            if not isinstance(content, list):
                return
            text = "".join(
                block["text"] for block in content
                if isinstance(block, Mapping) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
            self.current = ""
            self.text = text
            if text:
                self._activity(text)


def copy_omp_credentials(home: Path, *, source_home: Path | None = None) -> None:
    real_omp = (source_home or Path.home()) / ".omp"
    omp_dir = home / ".omp"
    agent_dir = omp_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    source_db = real_omp / "agent" / "agent.db"
    if source_db.is_file():
        target_db = agent_dir / "agent.db"
        target_db.touch(mode=0o600)
        target_db.chmod(0o600)
        # SQLite backup includes committed WAL data without sharing writable credentials.
        with (
            closing(sqlite3.connect(f"{source_db.as_uri()}?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(target_db)) as target,
        ):
            source.backup(target)
    for name in _OMP_AUTH_FILES:
        source = real_omp / name
        if source.is_file():
            target = omp_dir / name
            shutil.copyfile(source, target)
            target.chmod(0o600)


def prepare_runtime_home(tmp_dir: Path) -> Path:
    home = tmp_dir / "home"
    copy_omp_credentials(home)
    omp_dir = home / ".omp"
    agent_dir = omp_dir / "agent"
    real_omp = Path.home() / ".omp"
    # OAuth plugins need the login keychain located under the real $HOME.
    plugins = real_omp / "plugins"
    if plugins.is_dir():
        (omp_dir / "plugins").symlink_to(plugins)
    keychains = Path.home() / "Library" / "Keychains"
    if keychains.is_dir():
        (home / "Library").mkdir(parents=True, exist_ok=True)
        (home / "Library" / "Keychains").symlink_to(keychains)
    config = real_omp / "agent" / "config.yml"
    if config.is_file():
        target = agent_dir / "config.yml"
        target.write_text(_without_extensions(config.read_text()))
        target.chmod(0o600)
    return home


def _without_extensions(config_text: str) -> str:
    # The copied config declares hooks by ~/.omp path, which resolve against
    # the isolated home and fail to load; one of them also publishes ntfy
    # notifications. Plugin discovery is unaffected.
    kept, skipping = [], False
    for line in config_text.splitlines():
        if line.startswith("extensions:"):
            skipping = True
            continue
        if skipping and (not line.strip() or line[:1].isspace() or line.startswith("-")):
            continue
        skipping = False
        kept.append(line)
    return "\n".join(kept) + "\n"


def _unfence(text):
    # A fence strip, not a brace scan: prose around a fence must still fail.
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped[3:].removesuffix("```")
    newline = body.find("\n")
    if newline == -1:
        return body.strip()
    return body[newline + 1 :].strip()


def _extract_decision(output):
    events = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and value.get("type") == "message_end":
            events.append(value)
    if not events:
        raise ValueError("classifier returned no assistant message")

    message = events[-1].get("message")
    if (
        not isinstance(message, Mapping)
        or message.get("role") != "assistant"
        or message.get("stopReason") != "stop"
    ):
        raise ValueError("classifier assistant message did not complete")
    content = message.get("content")
    if content is None:
        raise ValueError("classifier assistant message has no content")
    if not isinstance(content, list):
        raise TypeError("classifier assistant content is not a list")
    text = "".join(
        block["text"]
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    try:
        decision = json.loads(_unfence(text))
    except json.JSONDecodeError as exc:
        raise ValueError("classifier assistant message is not JSON") from exc
    if not isinstance(decision, Mapping) or decision.get("action") not in {
        "merge",
        "unmatched",
    }:
        raise ValueError("classifier JSON contained no decision")
    return dict(decision)
