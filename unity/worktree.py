"""Per-agent git worktrees for formalization. Ported from unity_agent/pipeline.py."""

import logging
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

import fcntl

from . import artifacts, lake


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def agent_branch(name: str) -> str:
    return f"worktree/{_safe(name)}"


def agent_worktree(project_path: Path, name: str) -> Path:
    return Path(project_path) / ".worktrees" / _safe(name)


def _ensure_git_excludes(repo_path: Path, patterns: tuple[str, ...]) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=repo_path, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return
    exclude = Path(result.stdout.strip())
    if not exclude.is_absolute():
        exclude = Path(repo_path) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(errors="replace") if exclude.exists() else ""
    present = set(existing.splitlines())
    missing = [pattern for pattern in patterns if pattern not in present]
    if not missing:
        return
    with exclude.open("a") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write("".join(pattern + "\n" for pattern in missing))


def create_worktree(name: str, project_path: Path) -> Path:
    """Create a git worktree for `name` under <project>/.worktrees/; return its path.
    Tolerates leftovers from a crashed run: stale worktrees/branches are pruned first."""
    safe = _safe(name)
    worktree_path = agent_worktree(project_path, name)
    _ensure_git_excludes(project_path, (".worktrees", ".unity"))
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Clear stale state from a previous crashed run before re-adding.
    if worktree_path.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)],
                       cwd=project_path, capture_output=True)
    subprocess.run(["git", "worktree", "prune"], cwd=project_path, capture_output=True)
    branch = agent_branch(name)
    if subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                      cwd=project_path).returncode == 0:
        subprocess.run(["git", "branch", "-D", branch],
                       cwd=project_path, capture_output=True)

    lake.run(["git", "worktree", "add", "-b", f"worktree/{safe}", str(worktree_path)], cwd=project_path)
    link_runtime_state(worktree_path, project_path)
    return worktree_path


def link_runtime_state(worktree_path: Path, project_path: Path) -> None:
    """Expose the main checkout's run state inside an isolated source worktree."""
    source = Path(project_path) / ".unity"
    target = Path(worktree_path) / ".unity"
    if not source.is_dir():
        return
    _ensure_git_excludes(worktree_path, (".unity",))
    if target.is_symlink() and target.resolve() == source.resolve():
        return
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"cannot link shared runtime state over existing {target}")
    target.symlink_to(source.resolve(), target_is_directory=True)


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


def _git(project: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or f"git {' '.join(args)} failed with exit code {result.returncode}")
    return result


def main_commit(project_path: Path) -> str:
    return _git(Path(project_path), "rev-parse", "HEAD", check=True).stdout.strip()


def worktree_matches_main(project_path: Path, name: str) -> bool:
    worktree_path = agent_worktree(project_path, name)
    if not worktree_path.is_dir():
        return False
    if main_commit(project_path) != _git(
        worktree_path, "rev-parse", "HEAD", check=True
    ).stdout.strip():
        return False
    return not _git(worktree_path, "status", "--porcelain", check=True).stdout.strip()


def verify_candidate_commit(project_path: Path, name: str, commit_sha: str) -> str:
    """Resolve a candidate SHA and require it to belong to the caller's worktree branch."""
    project_path = Path(project_path)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit_sha.strip()):
        raise ValueError("commit_sha must be a Git commit SHA")
    resolved = _git(project_path, "rev-parse", f"{commit_sha}^{{commit}}", check=True).stdout.strip()
    branch = agent_branch(name)
    if _git(project_path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
        raise ValueError(f"no active worktree branch for agent '{name}'")
    if _git(project_path, "merge-base", "--is-ancestor", resolved, branch).returncode != 0:
        raise ValueError(f"commit {resolved} does not belong to agent '{name}'")
    if _git(project_path, "diff", "--quiet", main_commit(project_path), resolved).returncode == 0:
        raise ValueError("candidate commit contains no changes relative to main")
    return resolved


@contextmanager
def _merge_lock(project_path: Path):
    lock_path = Path(project_path) / ".unity" / "forum" / "merge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def sync_candidate_to_main(project_path: Path, candidate: dict) -> dict:
    """Apply one exact candidate commit to clean main, build, and commit atomically."""
    project_path = Path(project_path)
    candidate_sha = candidate["commit_sha"]
    decl = candidate["decl"]
    with _merge_lock(project_path):
        dirty = _git(project_path, "status", "--porcelain", "--untracked-files=no", check=True)
        if dirty.stdout.strip():
            return {"ok": False, "error": "main has tracked changes; refusing candidate merge"}
        before = main_commit(project_path)
        applied = _git(project_path, "cherry-pick", "--no-commit", candidate_sha)
        if applied.returncode != 0:
            _git(project_path, "reset", "--hard", before)
            return {"ok": False, "error": applied.stderr.strip() or "candidate conflicts with main"}
        try:
            build = subprocess.run(
                ["lake", "build"], cwd=project_path, capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            _git(project_path, "reset", "--hard", before)
            return {"ok": False, "error": f"could not run lake build: {exc}"}
        build_output = "\n".join(
            part.rstrip("\n") for part in (build.stdout, build.stderr) if part
        )
        build_record = None
        if build_output:
            stored = artifacts.store_text(
                project_path / ".unity" / "artifacts",
                build_output,
                kind="candidate_merge_build",
                source="lake build",
                metadata={
                    "candidate_id": candidate.get("candidate_id", ""),
                    "decl": decl,
                    "commit_sha": candidate_sha,
                    "returncode": build.returncode,
                },
            )
            build_record = {
                "returncode": build.returncode,
                "artifact_id": stored["artifact_id"],
                "sha256": stored["sha256"],
                "bytes": stored["bytes"],
                "lines": stored["lines"],
            }
        else:
            build_record = {"returncode": build.returncode}
        if build.returncode != 0:
            _git(project_path, "reset", "--hard", before)
            summary = artifacts.preview_text(build_output, 4000)
            return {
                "ok": False,
                "error": f"lake build failed: {summary}",
                "build": build_record,
            }
        if _git(project_path, "diff", "--quiet").returncode != 0:
            _git(project_path, "reset", "--hard", before)
            return {
                "ok": False,
                "error": "lake build changed tracked files outside the candidate index",
                "build": build_record,
            }
        commit = _git(project_path, "commit", "-m", f"UNITY: merge chunk {decl}")
        if commit.returncode != 0:
            _git(project_path, "reset", "--hard", before)
            return {"ok": False, "error": commit.stderr.strip() or "could not commit candidate"}
        merged_sha = main_commit(project_path)
        return {
            "ok": True,
            "main_sha": merged_sha,
            "previous_main_sha": before,
            "build": build_record,
        }


def force_sync_from_main(project_path: Path, name: str) -> dict:
    """Discard an agent worktree's tracked and untracked attempt work in favor of main."""
    project_path = Path(project_path)
    worktree_path = agent_worktree(project_path, name)
    if not worktree_path.is_dir():
        raise ValueError(f"no active worktree for agent '{name}'")
    target = main_commit(project_path)
    _git(worktree_path, "reset", "--hard", target, check=True)
    _git(worktree_path, "clean", "-fdx", "-e", ".unity", "-e", ".lake", check=True)
    link_runtime_state(worktree_path, project_path)
    symlink_lake_cache(worktree_path, project_path)
    return {"ok": True, "main_sha": target, "worktree": str(worktree_path)}


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

    # Best-effort: a half-removed worktree must not abort the other agents' cleanup.
    subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)],
                   cwd=project_path, capture_output=True)
    subprocess.run(["git", "worktree", "prune"], cwd=project_path, capture_output=True)
    branch = agent_branch(name)
    if subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=project_path,
    ).returncode == 0:
        subprocess.run(["git", "branch", "-D", branch], cwd=project_path, capture_output=True)
