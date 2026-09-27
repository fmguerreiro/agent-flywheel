"""The seams that tie a flywheel to its host.

Most of this package does not care where it runs. The SQLite store, the
adjudication thresholds, the eval verdict algebra and the git publish step
work against any repository. These four protocols name what does care, and
a host supplies one implementation of each.

The split was drawn from a measurement rather than a guess: in the original
single-host version, the only host-specific code was the transcript reader,
the two shell-outs to a model CLI, the macOS sandbox wrapper, and two lines
naming where agent configuration lives inside the repo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class TranscriptSource(Protocol):
    """Where sessions are read from: one implementation per agent harness.

    Paths are opaque to the caller - a source may key sessions by file, by
    directory, or by anything else it can later resolve.
    """

    name: str

    def sessions(self) -> list[tuple[str, str]]:
        """Every known transcript, as (path, harness) pairs."""

    def meta(self, path: str | Path) -> dict:
        """Session metadata: harness, session_id, cwd, transcript_path,
        trace_id, model, harness_version, started_at. Missing keys are None.
        """

    def corrections(self, path: str | Path) -> list[dict]:
        """Candidate corrections: the user turns that push back on the agent,
        each with the assistant text it was answering.
        """

    def skills_used(self, path: str | Path) -> list[str]:
        """Skill or command identifiers invoked during the session."""

    def is_subagent(self, path: str | Path) -> bool:
        """True for a delegated child session. Corrections in a child are
        addressed to its parent's instructions, not to the user's.
        """


class ModelRunner(Protocol):
    """How a model gets called.

    Two call shapes, because they have different failure modes: adjudication
    wants one JSON object back from a model with no tools, while drafting
    wants a file-editing agent loose in a worktree.
    """

    def classify(self, payload: dict, *, system: str, timeout: int) -> dict:
        """One JSON object from a tool-less model. Raises on anything else."""

    def draft_argv(
        self, prompt_path: Path, *, cwd: Path, scratch: Path, tools: str, timeout: int
    ) -> list[str]:
        """The argv that runs a file-editing agent against `prompt_path`.

        Returned rather than executed so the caller can wrap it in a sandbox;
        composing the two here would make the sandbox seam untestable.

        `scratch` is a caller-owned writable directory the runner may use for
        an isolated HOME and other per-call state. The caller creates it,
        grants it to the sandbox and removes it, so the runner never has to
        guess a location the sandbox has not been told about.
        """


class Sandbox(Protocol):
    """Confinement for the drafting subprocess, and only that.

    Writes are the boundary that matters: the drafter must not touch anything
    outside its leased worktree. Reads are left open by the macOS
    implementation because enumerating an agent CLI's lookup paths proved
    unstable across releases.

    Eval cases confine themselves separately and more strictly - a structural
    case denies reads of the real home outright. That confinement is the
    acceptance gate's own business and deliberately does not come through
    here, where reads are open.
    """

    available: bool

    def wrap(
        self,
        argv: Sequence[str],
        *,
        writable: Sequence[Path],
        profile_dir: Path,
    ) -> list[str]:
        """Wrap `argv` so it may write only under `writable`."""


class HostProject(Protocol):
    """The repository whose agent configuration is being evolved."""

    repo: Path
    evals_root: Path

    def stage_home(self, tree: Path, home: Path) -> None:
        """Populate `home` with the agent configuration found in `tree`, a
        materialised checkout at some revision. This is what makes a
        behavioural eval case see the configuration under test.
        """

    def describe(self) -> str:
        """One or two sentences naming this repository for the drafting
        prompt - what it holds and what a fix to it looks like.
        """


@dataclass(frozen=True)
class Flywheel:
    """Everything one flywheel instance needs. Built once, passed down."""

    host: HostProject
    runner: ModelRunner
    sandbox: Sandbox
    sources: tuple[TranscriptSource, ...]
    home: Path
    state: Path

    @property
    def db_path(self) -> Path:
        return self.home / "flywheel.sqlite"

    def source(self, harness: str) -> TranscriptSource:
        for source in self.sources:
            if source.name == harness:
                return source
        raise KeyError(f"no transcript source for harness {harness!r}")
