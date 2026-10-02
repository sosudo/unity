"""Bump-owned worktree names, with shared Git mechanics only.

Never reuse or prune another pipeline's branches, even for the same agent name.
Completed worktrees are retained as evidence; explicit cleanup is a user action.
"""

from pathlib import Path
import hashlib
import uuid

from . import worktree as _shared

main_commit = _shared.main_commit
link_runtime_state = _shared.link_runtime_state
symlink_lake_cache = _shared.symlink_lake_cache


def _name(name: str, project_path: Path | None = None) -> str:
    # Fresh migrations are worktrees of the same original Git repository, so
    # their branches share one namespace even though their source roots differ.
    # Bind branches and worker paths to the canonical private target, not merely
    # the roster name. Existing targets retain a stable name when continued.
    run = (hashlib.sha256(str(Path(project_path).resolve()).encode()).hexdigest()[:16] + "-"
           if project_path is not None else "")
    return "bump-" + run + name


def agent_branch(name: str, project_path: Path | None = None) -> str:
    return _shared.agent_branch(_name(name, project_path))


def agent_worktree(project_path: Path, name: str) -> Path:
    return _shared.agent_worktree(project_path, _name(name, project_path))


def create_worktree(name: str, project_path: Path) -> Path:
    path = agent_worktree(project_path, name)
    branch = agent_branch(name, project_path)
    if path.exists() or path.is_symlink() or _shared._git(
        project_path, "show-ref", "--verify", "--quiet", "refs/heads/" + branch,
    ).returncode == 0:
        raise ValueError("existing Bump work must be recovered, not overwritten")
    _shared.ensure_git_excludes(project_path, (".worktrees", ".unity/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    _shared._git(project_path, "worktree", "add", "-b", branch, str(path), check=True)
    link_runtime_state(path, project_path)
    return path


def verify_candidate_commit(project_path, name, commit_sha, *, allow_unchanged=False):
    return _shared.verify_candidate_commit(
        project_path, _name(name, project_path), commit_sha, allow_unchanged=allow_unchanged)


def force_sync_from_main(project_path, name):
    # Callers must checkpoint the exact owned tree before requesting this.
    return _shared.force_sync_from_main(project_path, _name(name, project_path))


def cleanup_worktree(name, worktree_path, project_path):
    """Keep completed/partial private work available for inspection and continuation."""
    if Path(worktree_path) != agent_worktree(project_path, name):
        raise ValueError("not a Bump-owned worktree")


def role_view(project_path: Path, role: str) -> Path:
    """Give planning/review a fresh checkout, not write access to the user's source tree.

    The caller supplies trusted roles. Native backend restrictions and final
    context validation remain required; a worktree alone is not a security sandbox.
    """
    if role not in {"chunking", "critic", "retrospective"}:
        raise ValueError("unknown Bump view role")
    tree = create_worktree("view-" + role + "-" + uuid.uuid4().hex[:12], project_path)
    symlink_lake_cache(tree, project_path)
    return tree
