"""Optimize-owned worktree names, with shared Git mechanics only.

Never reuse or prune another pipeline's branches, even for the same agent name.
Completed worktrees are retained as evidence; explicit cleanup is a user action.
"""

from pathlib import Path
import json
import shutil
import uuid

from . import worktree as _shared

main_commit = _shared.main_commit
symlink_lake_cache = _shared.symlink_lake_cache


def link_runtime_state(worktree_path: Path, project_path: Path) -> None:
    """Private service configuration with only exact shared runtime children.

    Copy configuration so worker-specific MCP settings cannot overwrite the
    source project's settings. Source files and candidate build artifacts stay
    private; forum/artifact/job records and stop requests remain run-wide.
    """
    project_path = Path(project_path).resolve()
    source = project_path / ".unity"
    target = Path(worktree_path) / ".unity"
    if not source.is_dir():
        raise ValueError("Optimize needs an initialized project")
    if target.is_symlink():
        raise ValueError("Optimize requires private worktree service configuration")
    _shared.ensure_git_excludes(worktree_path, (".unity",))
    marker = target / "optimize-worktree.json"
    binding = {"schema": "optimize-worktree-v1", "project_root": str(project_path)}
    if target.exists():
        if not marker.is_file() or json.loads(marker.read_text()) != binding:
            raise ValueError("existing Optimize runtime binding must be recovered, not overwritten")
        return
    target.mkdir()
    for name in ("forum", "artifacts", "logs", "jobs", "tmp"):
        shared = source / name
        shared.mkdir(parents=True, exist_ok=True)
        (target / name).symlink_to(shared.resolve(), target_is_directory=True)
    (target / "stop-requested").symlink_to(source / "stop-requested")
    for name in ("agents.yaml", "config.yaml", ".env", "UNITY.md"):
        entry = source / name
        if entry.is_file():
            shutil.copy2(entry, target / name)
    if (source / "metrics").is_dir():
        shutil.copytree(source / "metrics", target / "metrics")
    marker.write_text(json.dumps(binding, sort_keys=True) + "\n")


def _name(name: str) -> str:
    return "optimize-" + name


def agent_branch(name: str) -> str:
    return _shared.agent_branch(_name(name))


def agent_worktree(project_path: Path, name: str) -> Path:
    return _shared.agent_worktree(project_path, _name(name))


def create_worktree(name: str, project_path: Path) -> Path:
    return _shared.create_worktree(_name(name), project_path, preserve_existing=True,
                                  runtime_linker=link_runtime_state)


def verify_candidate_commit(project_path, name, commit_sha, *, allow_unchanged=False):
    return _shared.verify_candidate_commit(
        project_path, _name(name), commit_sha, allow_unchanged=allow_unchanged)


def force_sync_from_main(project_path, name):
    # The controller creates a fresh candidate view instead of resetting a
    # potentially valuable competing strategy. Never discard a prior attempt.
    raise ValueError("Optimize retains attempt worktrees; create a fresh view to sync")


def cleanup_worktree(name, worktree_path, project_path):
    """Keep completed/partial private work available for inspection and continuation."""
    if Path(worktree_path) != agent_worktree(project_path, name):
        raise ValueError("not an Optimize-owned worktree")


def role_view(project_path: Path, role: str) -> Path:
    """Give planning/review a fresh checkout, not write access to the user's source tree.

    The caller supplies trusted roles. Native backend restrictions and final
    context validation remain required; a worktree alone is not a security sandbox.
    """
    if role not in {"architect", "preparation", "exploration", "chunking", "optimizing", "critic", "retrospective"}:
        raise ValueError("unknown Optimize view role")
    tree = create_worktree("view-" + role + "-" + uuid.uuid4().hex[:12], project_path)
    if role != "architect":
        symlink_lake_cache(tree, project_path)
    return tree
