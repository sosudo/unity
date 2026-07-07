"""Per-agent git worktrees for formalization. Ported from unity_agent/pipeline.py."""

import logging
import re
import subprocess
from pathlib import Path

from . import lake


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def create_worktree(name: str, project_path: Path) -> Path:
    """Create a git worktree for `name` under <project>/.worktrees/; return its path."""
    safe = _safe(name)
    worktree_path = project_path / ".worktrees" / safe
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    gitignore = project_path / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if ".worktrees/" not in existing.splitlines():
        with gitignore.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(".worktrees/\n")
    lake.run(["git", "worktree", "add", "-b", f"worktree/{safe}", str(worktree_path)], cwd=project_path)
    return worktree_path


def symlink_lake_cache(worktree_path: Path, project_path: Path) -> None:
    """Symlink .lake/packages/ from the main project into the worktree to share the cache."""
    packages_src = project_path / ".lake" / "packages"
    if not packages_src.exists():
        return
    lake_dir = worktree_path / ".lake"
    lake_dir.mkdir(exist_ok=True)
    packages_link = lake_dir / "packages"
    if not packages_link.exists():
        packages_link.symlink_to(packages_src.resolve())


def detect_main_branch(project_path: Path) -> str:
    res = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=project_path, capture_output=True, text=True,
    )
    return res.stdout.strip() or "main"


def cleanup_worktree(name: str, worktree_path: Path, project_path: Path) -> None:
    """Rescue any uncommitted work (commit it on the branch), then remove the worktree + branch."""
    if worktree_path.exists():
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=worktree_path, capture_output=True, text=True,
        )
        if status.returncode == 0 and status.stdout.strip():
            subprocess.run(["git", "-C", str(worktree_path), "add", "-A"], capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", str(worktree_path), "commit", "-m",
                 f"EMERGENCY: auto-commit dirty worktree for {name}"],
                capture_output=True, text=True,
            )
            logging.error(f"[worktree] rescued dirty worktree for {name} via EMERGENCY commit")

    safe = _safe(name)
    lake.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=project_path)
    branch = f"worktree/{safe}"
    if subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=project_path,
    ).returncode == 0:
        lake.run(["git", "branch", "-D", branch], cwd=project_path)
