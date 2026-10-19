"""Guard Lake commands launched by ``unity optimize`` workers.

Optimize worktrees intentionally share ``.lake/packages`` with the main checkout.
That makes dependency caches fast, but it also means commands such as
``lake clean`` mutate every worker's environment. Workers receive a small
``lake`` shim in ``PATH`` which enters here. Safe diagnostics remain available,
while destructive workspace operations are controller-only and long-running
Lean processes are registered for cancellation.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence

from . import optimize_jobs


_CONTROLLER_ONLY = {"clean", "update", "upgrade", "init", "new"}


def _command_index(args: Sequence[str], command: str) -> int | None:
    try:
        return args.index(command)
    except ValueError:
        return None


def _rejection(args: Sequence[str], *, architect: bool = False) -> str | None:
    update = _command_index(args, "update")
    if architect and update is not None and list(args[update + 1:]) != ["LeanArchitect"]:
        return "the architect bootstrap may only run `lake update LeanArchitect` in its isolated worktree"
    for command in _CONTROLLER_ONLY:
        if command in args and not (architect and command == "update"):
            return (
                f"`lake {command}` is controller-only in unity optimize because "
                ".lake/packages is shared by every worktree"
            )

    exe = _command_index(args, "exe")
    if not architect and exe is not None and exe + 1 < len(args) and args[exe + 1] == "cache":
        return (
            "`lake exe cache` is controller-only in unity optimize because the "
            "Mathlib cache is shared by every worktree"
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
    try:
        options = json.loads(env.get("UNITY_OPTIMIZE_LAKE_ARGS", "[]"))
        if not isinstance(options, list) or any(not isinstance(arg, str) or not arg.startswith("-K") for arg in options):
            raise ValueError("invalid configured Lake options")
        supplied = [arg for arg in args if arg.startswith("-K")]
        if supplied and supplied != options:
            raise ValueError("worker Lake options disagree with the controller configuration")
        args = [*options, *[arg for arg in args if not arg.startswith("-K")]]
    except (ValueError, TypeError):
        print("error: worker must use the controller's configured Lake options", file=sys.stderr)
        return 2
    worker_cwd = Path(cwd or Path.cwd()).resolve()
    packages = worker_cwd / ".lake" / "packages"
    # LeanArchitect bootstrap is the existing pre-baseline Optimize phase.
    # It may resolve its dependency only in an isolated private package tree;
    # ordinary worker phases keep shared cache mutation controller-only.
    private_packages = not packages.is_symlink() and packages.resolve().is_relative_to(worker_cwd)
    architect = env.get("UNITY_OPTIMIZE_PROFILE") == "architect" and private_packages
    rejected = _rejection(args, architect=architect)
    if rejected:
        print(f"error: {rejected}", file=sys.stderr)
        return 2

    real_lake = str(env.get("UNITY_REAL_LAKE") or "").strip()
    project_root = str(env.get("UNITY_OPTIMIZE_PROJECT_ROOT") or "").strip()
    if not real_lake or not Path(real_lake).is_file():
        print("error: optimize Lake guard has no valid UNITY_REAL_LAKE", file=sys.stderr)
        return 2
    if not project_root or not Path(project_root).is_dir():
        print("error: optimize Lake guard has no valid UNITY_OPTIMIZE_PROJECT_ROOT", file=sys.stderr)
        return 2

    env_index = _command_index(args, "env")
    command = next((item for item in args if item in {"build", "env"}), "")
    # Serializing targeted builds and `lake env lean` prevents every worktree
    # from loading a large Lean environment simultaneously. Other cheap Lake
    # queries remain cancellable but do not hold the build lock.
    serialize = command == "build" or (
        env_index is not None and "lean" in args[env_index + 1:]
    )
    # The LSP protocol is bidirectional: buffering until server exit deadlocks
    # initialization. Only serve uses live stdio; batch checks stay captured.
    interactive = next((arg for arg in args if not arg.startswith("-")), None) == "serve"
    completed = optimize_jobs.run(
        Path(project_root),
        [real_lake, *args],
        cwd=worker_cwd,
        owner=str(env.get("UNITY_AGENT_NAME") or "optimize-worker"),
        task_id=str(env.get("UNITY_OPTIMIZE_TASK_ID") or ""),
        serialize_build=serialize,
        **({"passthrough_stdio": True} if interactive else {}),
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
