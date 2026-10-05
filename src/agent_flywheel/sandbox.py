"""Sandbox implementations. Shared by drafting and by behavioural evals."""

from __future__ import annotations

import json
import shutil
import textwrap
from collections.abc import Sequence
from pathlib import Path


def _subpath(path: Path) -> str:
    return f"(subpath {json.dumps(str(path.resolve()))})"


class MacSandbox:
    """macOS `sandbox-exec`, confining writes and leaving reads open.

    Reads stay open deliberately. An agent CLI resolves its plugin registry,
    model registry and login keychain through paths that move between
    releases, and enumerating them silently unregistered a model provider.
    Writes are the boundary that actually keeps the drafter inside its lease.
    """

    name = "sandbox-exec"

    def __init__(self) -> None:
        self._bin = shutil.which("sandbox-exec")
        self.available = self._bin is not None

    def wrap(
        self,
        argv: Sequence[str],
        *,
        writable: Sequence[Path],
        profile_dir: Path,
    ) -> list[str]:
        if self._bin is None:
            raise RuntimeError("sandbox-exec not found; this host cannot confine the drafter")
        allow = "\n".join(f"  {_subpath(path)}" for path in writable)
        profile = profile_dir / "drafter.sb"
        profile.write_text(
            textwrap.dedent("""
                (version 1)
                (deny default)
                (allow process*)
                (allow signal (target self))
                (allow sysctl-read)
                (allow mach-lookup)
                (allow network*)
                (allow file-read*)
                (allow file-write*
            """).strip()
            + "\n"
            + allow
            + '\n  (literal "/dev/null"))\n'
        )
        return [self._bin, "-f", str(profile), *argv]


class NullSandbox:
    """No confinement. For hosts that isolate at a coarser grain - a
    container, a VM, a throwaway CI runner - where an inner sandbox would be
    redundant. Never silently substituted for a real one.
    """

    name = "none"
    available = True

    def wrap(
        self,
        argv: Sequence[str],
        *,
        writable: Sequence[Path],
        profile_dir: Path,
    ) -> list[str]:
        return list(argv)
