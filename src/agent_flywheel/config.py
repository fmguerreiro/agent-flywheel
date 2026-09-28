"""Build a `Flywheel` from environment and an optional TOML file.

TOML rather than a Python entry point or environment variables alone:
`tomllib` is already a dependency for eval cases, the file sits beside the
database it configures, and a host is mostly paths, which read badly as
environment variables and badly again as a plugin.

With no file present the dotfiles layout is assumed, and said so on stderr.
A silent default here would mean staging the wrong tree into an eval home
and reading a verdict that answered a different question.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

from .host import Flywheel
from .hosts import DotfilesHost, SimpleHost
from .runner import OmpRunner
from .sandbox import MacSandbox, NullSandbox
from .sources import ClaudeTranscripts, OmpTranscripts, default_sources

CONFIG_NAME = "flywheel.toml"
DEFAULT_HOME = Path("~/.local/share/agent-flywheel").expanduser()
DEFAULT_STATE = Path("~/.local/state/agent-flywheel").expanduser()
DEFAULT_REPO = Path("~/projects/dotfiles").expanduser()


def _home() -> Path:
    return Path(os.environ.get("AGENT_FLYWHEEL_HOME") or DEFAULT_HOME).expanduser()


def _state() -> Path:
    return Path(os.environ.get("AGENT_FLYWHEEL_STATE") or DEFAULT_STATE).expanduser()


def _load_toml(home: Path) -> dict:
    path = home / CONFIG_NAME
    if not path.is_file():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _build_host(data: dict, *, repo_override: str | None):
    host_data = data.get("host", {})
    repo = Path(repo_override or host_data.get("repo") or DEFAULT_REPO).expanduser().resolve()
    evals_root = host_data.get("evals_root", "specs/agent-evals")

    kind = host_data.get("kind", "dotfiles")
    if kind == "dotfiles":
        return DotfilesHost(repo, evals_root=evals_root)
    if kind == "simple":
        stage = host_data.get("stage")
        if not stage:
            raise ValueError("host.kind = 'simple' needs a host.stage table of repo path -> home path")
        return SimpleHost(
            repo,
            evals_root=evals_root,
            stage=dict(stage),
            description=host_data.get("description", f"The repository at {repo}."),
        )
    raise ValueError(f"unknown host.kind {kind!r}")


def _build_sandbox(data: dict):
    kind = data.get("sandbox", {}).get("kind", "macos")
    if kind == "macos":
        return MacSandbox()
    if kind == "none":
        return NullSandbox()
    raise ValueError(f"unknown sandbox.kind {kind!r}")


def _build_sources(data: dict) -> tuple:
    rows = data.get("sources")
    if not rows:
        return default_sources()
    built = []
    for row in rows:
        kind = row.get("kind")
        if kind == "omp":
            built.append(OmpTranscripts(root=row.get("root", "~/.omp/agent/sessions"), name=row.get("name", "omp")))
        elif kind == "claude":
            built.append(ClaudeTranscripts(root=row.get("root", "~/.claude/projects"), name=row.get("name", "claude")))
        else:
            raise ValueError(f"unknown source kind {kind!r}")
    return tuple(built)


def build(*, repo: str | None = None, quiet: bool = False) -> Flywheel:
    home = _home()
    data = _load_toml(home)
    if not data and not quiet:
        print(
            f"agent-flywheel: no {home / CONFIG_NAME}, assuming the dotfiles host at {DEFAULT_REPO}",
            file=sys.stderr,
        )
    return Flywheel(
        host=_build_host(data, repo_override=repo),
        runner=OmpRunner(),
        sandbox=_build_sandbox(data),
        sources=_build_sources(data),
        home=home,
        state=_state(),
    )
