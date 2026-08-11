"""Locate the project's `.unity/` directory and load its settings."""

from dataclasses import dataclass
from pathlib import Path

import asyncclick as click
from dotenv import load_dotenv

UNITY_DIRNAME = ".unity"


@dataclass
class Paths:
    project_root: Path
    unity: Path
    env: Path
    unity_md: Path
    agents_yaml: Path
    forum: Path
    logs: Path

    @classmethod
    def from_unity_dir(cls, unity: Path) -> "Paths":
        return cls(
            project_root=unity.parent,
            unity=unity,
            env=unity / ".env",
            unity_md=unity / "UNITY.md",
            agents_yaml=unity / "agents.yaml",
            forum=unity / "forum",
            logs=unity / "logs",
        )


def find_unity_dir(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default: cwd) looking for a project `.unity/` directory.
    The global home `~/.unity` (the cross-run library) is never a project dir."""
    home_unity = (Path.home() / UNITY_DIRNAME).resolve()
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        candidate = d / UNITY_DIRNAME
        if candidate.is_dir() and candidate.resolve() != home_unity:
            return candidate
    return None


def require_unity_dir(start: Path | None = None) -> Path:
    """Like `find_unity_dir`, but fail with a clear message if none exists."""
    unity = find_unity_dir(start)
    if unity is None:
        raise click.ClickException("no .unity/ found — run 'unity init' first")
    return unity


def load_env(unity: Path) -> None:
    """Load `.unity/.env` into the process environment, if present."""
    env = unity / ".env"
    if env.is_file():
        load_dotenv(env)


def load_paths(start: Path | None = None) -> Paths:
    """Locate `.unity/`, load its `.env`, and return resolved paths."""
    unity = require_unity_dir(start)
    load_env(unity)
    return Paths.from_unity_dir(unity)


def active_forum_dir(paths: Paths) -> Path:
    """Forum for the active run, falling back to the legacy project forum.

    Prove worktrees never need an uncommitted ``.unity`` copy: their MCP bridge
    follows this project-global pointer to the run-scoped control plane.
    """
    try:
        import json
        try:
            control = json.loads((paths.unity / "state.json").read_text())
        except (OSError, ValueError):
            control = {}
        if control.get("command") not in (None, "prove"):
            return paths.forum
        from .prove_runtime import active_run_dir
        run_dir = active_run_dir(paths.unity)
        state_path = run_dir / "state" / "runtime.json" if run_dir is not None else None
        if state_path is not None and state_path.exists():
            state = json.loads(state_path.read_text())
            if state.get("status") != "complete" or control.get("phase") != "done":
                return run_dir / "forum"
    except (ImportError, OSError, ValueError, KeyError):
        pass
    return paths.forum
