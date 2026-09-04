"""Guard Lake commands launched by ``unity solve`` formalization workers.

Solve worktrees intentionally share ``.lake/packages`` with the main checkout.
That makes dependency caches fast, but it also means commands such as
``lake clean`` mutate every worker's environment. Formalizers receive a small
``lake`` shim in ``PATH`` which enters here. Safe diagnostics remain available,
while destructive workspace operations are controller-only and long-running
Lean processes are registered for cancellation.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Mapping, Sequence

from . import solve_jobs


_CONTROLLER_ONLY = {"clean", "update", "upgrade", "init", "new"}


def _command_index(args: Sequence[str], command: str) -> int | None:
    try:
        return args.index(command)
    except ValueError:
        return None


def _rejection(args: Sequence[str]) -> str | None:
    for command in _CONTROLLER_ONLY:
        if command in args:
            return (
                f"`lake {command}` is controller-only in unity solve because "
                ".lake/packages is shared by every worktree"
            )

    exe = _command_index(args, "exe")
    if exe is not None and exe + 1 < len(args) and args[exe + 1] == "cache":
        return (
            "`lake exe cache` is controller-only in unity solve because the "
            "Mathlib cache is shared by every worktree"
        )

    build = _command_index(args, "build")
    if build is not None:
        targets = [arg for arg in args[build + 1:] if arg and not arg.startswith("-")]
        if not targets:
            return (
                "bare `lake build` is reserved for Unity's authoritative main build; "
                "use Lean LSP, `lake env lean <file>`, or `lake build <target>`"
            )
    return None


def run(
    args: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    """Validate and run one worker Lake command, returning its exit status."""
    env = os.environ if environ is None else environ
    rejected = _rejection(args)
    if rejected:
        print(f"error: {rejected}", file=sys.stderr)
        return 2

    real_lake = str(env.get("UNITY_REAL_LAKE") or "").strip()
    project_root = str(env.get("UNITY_SOLVE_PROJECT_ROOT") or "").strip()
    if not real_lake or not Path(real_lake).is_file():
        print("error: solve Lake guard has no valid UNITY_REAL_LAKE", file=sys.stderr)
        return 2
    if not project_root or not Path(project_root).is_dir():
        print("error: solve Lake guard has no valid UNITY_SOLVE_PROJECT_ROOT", file=sys.stderr)
        return 2

    env_index = _command_index(args, "env")
    command = next((item for item in args if item in {"build", "env"}), "")
    # Serializing targeted builds and `lake env lean` prevents every worktree
    # from loading a large Lean environment simultaneously. Other cheap Lake
    # queries remain cancellable but do not hold the build lock.
    serialize = command == "build" or (
        env_index is not None and "lean" in args[env_index + 1:]
    )
    completed = solve_jobs.run(
        Path(project_root),
        [real_lake, *args],
        cwd=Path(cwd or Path.cwd()),
        owner=str(env.get("UNITY_AGENT_NAME") or "solve-worker"),
        task_id=str(env.get("UNITY_SOLVE_TASK_ID") or ""),
        serialize_build=serialize,
    )
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return int(completed.returncode)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
