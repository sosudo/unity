"""Thin subprocess wrappers around lake/git for project setup."""

import subprocess
from pathlib import Path

import asyncclick as click


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    """Run a command, streaming its output. Raises ClickException on failure if check."""
    rc = subprocess.run(cmd, cwd=cwd).returncode
    if check and rc != 0:
        raise click.ClickException(f"command failed (rc={rc}): {' '.join(map(str, cmd))}")
    return rc


def new_project(name: str, math: bool = False, cwd: Path | None = None) -> None:
    run(["lake", "new", name, *(["math"] if math else [])], cwd=cwd)


def has_mathlib(project: Path) -> bool:
    """True if the project depends on Mathlib (lakefile or manifest mentions it)."""
    for name in ("lakefile.toml", "lakefile.lean", "lake-manifest.json"):
        f = project / name
        if f.exists() and "mathlib" in f.read_text(errors="replace").lower():
            return True
    return False


def cache_get(project: Path) -> None:
    """Fetch the Mathlib build cache. `lake exe cache` only exists with Mathlib —
    it crashes on non-Mathlib projects, so skip when Mathlib isn't a dependency."""
    if not has_mathlib(project):
        return
    run(["lake", "exe", "cache", "get"], cwd=project)


def build(project: Path) -> None:
    run(["lake", "build"], cwd=project)


def ensure_initial_commit(project: Path) -> None:
    """`lake new` runs `git init` but makes no commit; create one so the tree is populated."""
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                          cwd=project, capture_output=True, text=True)
    if head.returncode != 0:
        subprocess.run(["git", "add", "-A"], cwd=project, check=False)
        subprocess.run(["git", "commit", "-m", "UNITY: initial project commit", "--allow-empty"],
                       cwd=project, check=False)
