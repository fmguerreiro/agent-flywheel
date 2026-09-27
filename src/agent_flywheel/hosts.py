"""HostProject implementations: the repository whose agent rules evolve.

A host answers two questions the rest of the package refuses to guess: where
in a checkout the agent's configuration lives, and how to describe the
repository to the model drafting a fix for it.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

# _run_dispatch reassigns os.environ["HOME"] to scope ingest at the temp home,
# so the real one has to be captured before any run can shadow it.
_REAL_HOME = Path.home()

DOTFILES_DESCRIPTION = (
    "This is a dotfiles repository. It holds the agent's own configuration -"
    " rule files, skills and harness settings - in per-program role"
    " directories that are staged into $HOME. A fix to it edits those files"
    " in place; there is no application code here to change."
)


# Held fixed across both arms: the model config is not the artifact under test.
# A revision carries its own config.yml, so materializing it would vary the
# model alongside the skills and confound every verdict.
def pinned_omp_config() -> Path | None:
    override = os.environ.get("AGENT_FLYWHEEL_EVAL_CONFIG")
    if override:
        return Path(override).expanduser()
    live = _REAL_HOME / ".omp" / "agent" / "config.yml"
    return live if live.is_file() else None


def _resolve_evals_root(repo: Path, evals_root: str | Path) -> Path:
    candidate = Path(evals_root).expanduser()
    return candidate if candidate.is_absolute() else repo / candidate


class DotfilesHost:
    """The host this flywheel grew up in: omp and Claude Code configuration
    staged out of an Ansible-style dotfiles tree.
    """

    def __init__(
        self,
        repo: str | Path,
        *,
        evals_root: str | Path = "specs/agent-evals",
        description: str = DOTFILES_DESCRIPTION,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.evals_root = _resolve_evals_root(self.repo, evals_root)
        self._description = description

    def stage_home(self, tree: Path, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        omp_agent_src = tree / "roles" / "omp" / "files" / "agent"
        if omp_agent_src.is_dir():
            agent_dst = home / ".omp" / "agent"
            shutil.copytree(omp_agent_src, agent_dst, dirs_exist_ok=True)
            pinned = pinned_omp_config()
            if pinned is not None:
                shutil.copyfile(pinned, agent_dst / "config.yml")
        claude_src = tree / "roles" / "claude_code" / "files" / ".claude"
        if claude_src.is_dir():
            shutil.copytree(claude_src, home / ".claude", dirs_exist_ok=True)

    def describe(self) -> str:
        return self._description


class SimpleHost:
    """Any repository that can stage its agent configuration by copying: a
    mapping of repo-relative sources to home-relative destinations.

    Sources may be directories or single files, because the common shape is
    one `AGENTS.md` at the root beside a `.claude/` tree. A source that does
    not exist in a given revision is skipped, not an error - the flywheel
    compares revisions, and an older one predates the file.
    """

    def __init__(
        self,
        repo: str | Path,
        *,
        stage: Mapping[str, str],
        description: str,
        evals_root: str | Path = "specs/agent-evals",
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.evals_root = _resolve_evals_root(self.repo, evals_root)
        self._stage = {source: _checked(source, dest) for source, dest in stage.items()}
        self._description = description

    def stage_home(self, tree: Path, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        for source, destination in self._stage.items():
            src = tree / source
            dst = home / destination
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)

    def describe(self) -> str:
        return self._description


def _checked(source: str, destination: str) -> str:
    """Both ends stay inside their tree. A mapping is hand-written config, so
    the risk being caught is a typo that writes outside the eval home, where
    it would silently contaminate the real one.
    """
    for label, value in (("source", source), ("destination", destination)):
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"stage {label} must be a relative path inside the tree: {value!r}")
    return destination
