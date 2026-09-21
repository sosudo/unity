"""Task-owned source files and explicit cleanup for solve candidates.

Reservations coordinate integration, not filesystem sandboxing. Private source is
never deleted or reset by this module. Git remains the authority for changed paths.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess


def normalize_paths(paths: list[str]) -> list[str]:
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("paths must be a list of project-relative filenames")
    result = []
    for path in paths:
        parsed = PurePosixPath(path)
        if (not path or parsed.is_absolute() or ".." in parsed.parts or "\\" in path
                or str(parsed) != path or path == "." or "\0" in path
                or parsed.parts[0] in {".git", ".unity", ".lake", ".worktrees"}):
            raise ValueError(f"not a candidate source path: {path!r}")
        result.append(path)
    return sorted(set(result))


def normalize_cleanup(value: list[dict] | None) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("obsolete_files must be a list")
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"path", "replacement_candidate_id"}:
            raise ValueError("obsolete_files requires path and replacement_candidate_id")
        path = normalize_paths([row["path"]])[0]
        replacement = row["replacement_candidate_id"]
        if not isinstance(replacement, str) or not replacement.strip() or path in seen:
            raise ValueError("obsolete_files requires distinct paths and a replacement candidate")
        seen.add(path)
        result.append({"path": path, "replacement_candidate_id": replacement})
    return sorted(result, key=lambda row: row["path"])


def immutable_git_paths(root: Path, base_sha: str, commit_sha: str) -> dict:
    """Treat renames as deletion/addition, so neither source path bypasses policy."""
    def read(*extra):
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
             "--name-only", "-z", *extra, base_sha, commit_sha, "--"],
            cwd=root, capture_output=True, text=True,
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or "could not inspect candidate paths")
        return normalize_paths([path for path in result.stdout.split("\0") if path])
    return {"changed_paths": read(), "deleted_paths": read("--diff-filter=D")}


def checked_inventory(artifacts_dir: Path, verification: dict) -> list[dict]:
    """Load exact checked declaration names without retaining them in live state."""
    from . import artifacts

    reference = verification.get("inventory_artifact") or {}
    try:
        payload = artifacts.artifact_bytes(artifacts_dir, reference["artifact_id"])
        if hashlib.sha256(payload).hexdigest() != reference["sha256"]:
            return []
        inventory = json.loads(payload).get("project_declarations", [])
        if not isinstance(inventory, list) or any(
            not isinstance(row, dict)
            or any(not isinstance(row.get(key), str) for key in ("name", "module", "kind"))
            for row in inventory
        ):
            return []
        return inventory
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        return []  # Unavailable evidence cannot establish a missing declaration.


def reservations(state: dict) -> dict:
    """Read persisted reservations, deriving compatible ownership for older runs."""
    def successors(task_id):
        pending, seen, current = [task_id], set(), set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            if key in state.get("formal_tasks", {}):
                current.add(key)
            else:
                pending.extend(state.get("retired_tasks", {}).get(key, {}).get("replaced_by", []))
        return current

    result = deepcopy(state.get("file_reservations", {}))
    persisted = set(result)
    for task_id, task in {**state.get("retired_tasks", {}), **state.get("formal_tasks", {})}.items():
        outputs = list(task.get("outputs", []))
        for history in task.get("history", []):
            outputs.extend(history.get("outputs", []))
        for output in outputs:
            path = output["file"]
            if path in persisted:
                continue
            row = result.setdefault(path, {"owner_task": task_id, "shared_with": []})
            if task_id != row["owner_task"] and task_id not in row["shared_with"]:
                row["shared_with"].append(task_id)
    for row in result.values():
        owner = row["owner_task"]
        descendants = successors(owner)
        shared = {key for task_id in row["shared_with"] for key in successors(task_id)}
        if owner in state.get("retired_tasks", {}) and descendants:
            # Explicit DAG replacement, not a new claim, transfers the file.
            # One deterministic owner can authorize further collaboration.
            row["owner_task"] = min(descendants)
            row["inherited_from"] = sorted(set(row.get("inherited_from", [])) | {owner})
            shared.update(descendants)
        row["shared_with"] = sorted(shared - {row["owner_task"]})
    return result


def reserve_files(forum_dir: Path, author: str, task_id: str, paths: list[str],
                  *, share_with: list[str] | None = None) -> dict:
    from . import solve_state as runtime

    paths = normalize_paths(paths)
    if not isinstance(share_with or [], list) or any(not isinstance(key, str) for key in share_with or []):
        raise ValueError("share_with must be a list of task IDs")
    share_with = sorted(set(share_with or []) - {task_id})
    with runtime.transaction(forum_dir) as state:
        if (state.get("phase") != "formalizing" or task_id not in state["formal_tasks"]
                or not any(row.get("target") == task_id and row.get("status") in {"claimed", "succeeded"}
                           and runtime.strategy_is_current(state, row) and runtime.participates(row, author)
                           for row in state["strategies"].values())):
            raise ValueError("reserve_files requires a current claimed or successful task strategy")
        if set(share_with) - state["formal_tasks"].keys():
            raise ValueError("sharing requires current task IDs")
        rows = reservations(state)
        for path in paths:
            existing = rows.get(path)
            if existing and (task_id not in {existing["owner_task"], *existing["shared_with"]}
                             or (share_with and existing["owner_task"] != task_id)):
                return {"status": "conflict", "path": path, **existing,
                        "next_action": "Use a separate file or ask its owner task to grant sharing. Private work is preserved."}
        for path in paths:
            row = rows.setdefault(path, {"owner_task": task_id, "shared_with": []})
            row["shared_with"] = sorted(set(row["shared_with"]) | set(share_with))
        if rows != state.get("file_reservations", {}):
            state["file_reservations"] = rows
            runtime._event(state, "files_reserved", task_id=task_id, author=author,
                           paths=paths, share_with=share_with)
        return {"status": "reserved", "files": {path: rows[path] for path in paths}}


def validate_candidate_files(state: dict, candidate: dict, *, changed_paths: list[str] | None = None,
                             deleted_paths: list[str] | None = None) -> list[dict]:
    from . import solve_state as runtime

    changed = normalize_paths(changed_paths if changed_paths is not None else candidate.get("changed_paths", []))
    deleted = normalize_paths(deleted_paths if deleted_paths is not None else candidate.get("deleted_paths", []))
    cleanup = normalize_cleanup(candidate.get("obsolete_files"))
    task_id, rows, blockers = candidate["task_id"], reservations(state), []

    def reject(code, message, path):
        blockers.append({"code": code, "message": message, "path": path,
                         "task_ids": [task_id], "deterministic": code != "file_owned_by_other_task",
                         "required_action": "Coordinate file ownership or correct explicit cleanup evidence; preserve private work."})

    cleaned = {row["path"]: row for row in cleanup}
    protected = {output["file"] for outputs in (state.get("formalization", {}).get("contract") or {}).get("bindings", {}).values()
                 for output in outputs}
    for path in deleted:
        if rows.get(path, {}).get("inherited_from") and path not in cleaned:
            reject("cleanup_evidence_required",
                   f"Inherited scaffold {path} needs explicit obsolete_files replacement evidence before deletion.", path)
    for path, row in cleaned.items():
        replacement = state.get("formal_candidates", {}).get(row["replacement_candidate_id"], {})
        owner = state.get("formal_tasks", {}).get(replacement.get("task_id"), {})
        if (replacement.get("status") != "merged" or replacement.get("stage") != "complete"
                or owner.get("status") != "complete" or owner.get("accepted_candidate") != row["replacement_candidate_id"]
                or not runtime.candidate_is_current(state, replacement)
                or replacement.get("verification", {}).get("status") != "passed"):
            reject("cleanup_replacement_unaccepted", f"Replacement for {path} is not a current verified complete candidate.", path)
        if path not in deleted:
            reject("cleanup_not_deleted", f"Explicit obsolete file {path} is not deleted in the candidate diff.", path)
        if path in protected:
            reject("cleanup_protected_output", f"Cannot delete {path}: it still contains an adopted output.", path)
    for path in sorted(set(changed) | {row["file"] for row in candidate.get("outputs", [])}):
        owner = rows.get(path)
        if not owner or task_id in {owner["owner_task"], *owner["shared_with"]}:
            continue
        retired = state.get("retired_tasks", {}).get(owner["owner_task"], {})
        if path in cleaned and task_id in retired.get("replaced_by", []):
            continue  # Its accepted replacement and protected outputs were checked above.
        reject("file_owned_by_other_task", f"{path} belongs to task {owner['owner_task']}; task {task_id} must coordinate sharing.", path)
    return blockers


def record_candidate_reservations(state: dict, candidate: dict) -> None:
    """Legacy/first submissions atomically reserve previously unowned changed files."""
    rows = reservations(state)
    for path in set(candidate.get("changed_paths", [])) | {row["file"] for row in candidate.get("outputs", [])}:
        rows.setdefault(path, {"owner_task": candidate["task_id"], "shared_with": []})
    state["file_reservations"] = rows


def inventory_blockers(outputs: list[dict], inventory: list[dict]) -> list[dict]:
    """Only call with a controller inventory bound to the exact candidate inputs.

    Quoted/root-qualified spellings need Lean's name parser; never guess using a
    text regex. They retain the regular kernel-inspection path.
    """
    names = {row["name"] for row in inventory}
    return [{"code": "target_not_found", "deterministic": True,
             "message": f"Target {row['declaration']} is absent from the unchanged checked environment.",
             "required_action": "Use the exact declaration name from the checked inventory.",
             "declaration": row["declaration"]}
            for row in outputs if row["declaration"] not in names
            and not any(mark in row["declaration"] for mark in ("«", "»", "_root_."))]
