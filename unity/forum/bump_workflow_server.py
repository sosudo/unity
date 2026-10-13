"""Phase-scoped forum tools for compiler-driven Bump migrations only."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from fastmcp import FastMCP

from .. import artifacts, bump_tasks as tasks
from . import server as discussion

FORUM_DIR = Path("forum")
PROJECT_ROOT: Path | None = None
PROFILE = "repairing"
PROFILES = {"preparing", "repairing", "critic", "retrospective"}
_CONFIG_FILES = {"lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json"}


def configure(forum_dir: Path, project_root: Path, profile: str = "repairing") -> None:
    global FORUM_DIR, PROJECT_ROOT, PROFILE
    if profile not in PROFILES: raise ValueError("unknown compiler Bump forum profile")
    FORUM_DIR, PROJECT_ROOT, PROFILE = Path(forum_dir), Path(project_root).resolve(), profile
    state = tasks.load(FORUM_DIR)
    if Path(state["root"]).resolve() != PROJECT_ROOT:
        raise ValueError("forum project root mismatch")
    discussion.FORUM_DIR = FORUM_DIR
    discussion.PROJECT_ROOT = PROJECT_ROOT
    discussion.ICRL_ENABLED = False


def _root() -> Path:
    if PROJECT_ROOT is None: raise ValueError("Bump forum is not configured")
    return PROJECT_ROOT


def _author(author: str) -> str:
    bound = os.getenv("UNITY_AGENT_NAME", "").strip()
    if not author.strip() or (bound and bound != author):
        raise ValueError("author does not match the bound worker")
    return bound or author


def _task_id(task_id: str) -> str:
    bound = os.getenv("UNITY_BUMP_TASK_ID", "").strip()
    if not task_id or (bound and bound != task_id):
        raise ValueError("task does not match the bound assignment")
    return task_id


def _thread(thread_id: str) -> str:
    return "bump-" + (re.sub(r"[^a-zA-Z0-9_-]", "-", thread_id).strip("-") or "global")


def _mirror(author: str, title: str, body: str, task_id: str = "global") -> None:
    tid = _thread(task_id)
    discussion.forum_create_thread(tid, "Bump: " + task_id)
    discussion.forum_post(tid, author, title + "\n\n" + body)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode: raise ValueError("Git operation failed: " + result.stderr.strip()[:2000])
    return result.stdout


def bump_status() -> dict:
    """Current configuration, task counts and candidate/review status."""
    state = tasks.load(FORUM_DIR)
    counts = {}
    for task in state["tasks"].values(): counts[task["status"]] = counts.get(task["status"], 0) + 1
    return {key: state[key] for key in ("run_id", "revision", "phase", "main_sha", "target_version", "config", "review")} | {
        "task_counts": counts, "tasks": list(state["tasks"].values()), "candidates": list(state["candidates"].values())}


def bump_brief() -> dict:
    """Migration objective and coordination rules; compilation is not hole completion."""
    state = tasks.load(FORUM_DIR)
    return {"root": state["root"], "target_version": state["target_version"], "scope": state["config"]["scope"],
            "lake_args": state["config"]["lake_args"], "bound_task": os.getenv("UNITY_BUMP_TASK_ID", ""),
            "rules": ["Edit only your leased files in the assigned worktree.",
                      "Use compiler feedback; request larger module/API groups when needed.",
                      "Submit useful partial repairs without waiting for unrelated declarations.",
                      "Request shared configuration changes; do not run shared lake update.",
                      "Report holes, skipped scope and unperformed checks honestly.",
                      "Record workflow adjustments with evidence and observed results."]}


def bump_task(task_id: str) -> dict:
    state = tasks.load(FORUM_DIR)
    task = state["tasks"][task_id]
    return {"task": task, "chunks": [state["chunks"][cid] for cid in task["chunk_ids"] if cid in state["chunks"]],
            "strategies": [s for s in state["strategies"].values() if s["task_id"] == task_id]}


def forum_post(thread_id: str, author: str, content: str, reply_to: list[str] | None = None) -> dict:
    author = _author(author)
    tid = _thread(thread_id)
    discussion.forum_create_thread(tid, "Bump: " + thread_id)
    return discussion.forum_post(tid, author, content, reply_to=reply_to)


def forum_read(thread_id: str, sort: str = "hot") -> dict:
    return discussion.forum_read(_thread(thread_id), sort=sort)


def register_strategy(author: str, task_id: str, title: str, description: str) -> dict:
    author, task_id = _author(author), _task_id(task_id)
    with tasks.transaction(FORUM_DIR) as state:
        if task_id not in state["tasks"]: raise ValueError("unknown task")
        sid = "strategy-" + str(len(state["strategies"]) + 1)
        row = {"strategy_id": sid, "task_id": task_id, "author": author, "title": title,
               "description": description, "owner": None, "assistants": [], "status": "open"}
        state["strategies"][sid] = row
    _mirror(author, title, description, task_id)
    return row


def claim_strategy(author: str, strategy_id: str) -> dict:
    author = _author(author)
    with tasks.transaction(FORUM_DIR) as state:
        row = state["strategies"][strategy_id]
        _task_id(row["task_id"])
        if row["owner"] not in {None, author}: raise ValueError("strategy is claimed; offer assistance instead")
        row.update(owner=author, status="active")
        result = deepcopy(row)
    return result


def assist_strategy(author: str, strategy_id: str, contribution: str) -> dict:
    author = _author(author)
    with tasks.transaction(FORUM_DIR) as state:
        row = state["strategies"][strategy_id]
        if author not in row["assistants"]: row["assistants"].append(author)
        tasks._event(state, "assistance", author, strategy_id=strategy_id, contribution=contribution)
        result = deepcopy(row)
    _mirror(author, "Assistance: " + row["title"], contribution, row["task_id"])
    return result


def _communication(kind: str, author: str, task_id: str, content: str, **details) -> dict:
    author = _author(author)
    with tasks.transaction(FORUM_DIR) as state:
        if task_id and task_id not in state["tasks"]: raise ValueError("unknown task")
        result = tasks._event(state, kind, author, task_id=task_id, content=content, **details)
    _mirror(author, kind.replace("_", " "), content, task_id)
    return result


def publish_finding(author: str, task_id: str, finding: str, evidence: str) -> dict:
    return _communication("finding", author, task_id, finding, evidence=evidence)


def report_obstacle(author: str, task_id: str, obstacle: str, evidence: str) -> dict:
    return _communication("obstacle", author, task_id, obstacle, evidence=evidence)


def ask_question(author: str, task_id: str, question: str) -> dict:
    return _communication("question", author, task_id, question)


def answer_question(author: str, question_id: str, answer: str) -> dict:
    state = tasks.load(FORUM_DIR)
    question = next((e for e in state["events"] if e["event_id"] == question_id and e["kind"] == "question"), None)
    if not question: raise ValueError("unknown question")
    return _communication("answer", author, question["task_id"], answer, question_id=question_id)


def yield_task(author: str, task_id: str, reason: str) -> dict:
    """Request a handoff; controller retains leases until worker closure."""
    author, task_id = _author(author), _task_id(task_id)
    with tasks.transaction(FORUM_DIR) as state:
        task = tasks._owned(state, author, task_id)
        if task["status"] != "running": raise ValueError("submitted candidate cannot be yielded")
        task["yield_requested"] = {"reason": reason, "at": tasks.now()}
        result = tasks._event(state, "yield", author, task_id=task_id, reason=reason)
    return result


def request_repair_scope(author: str, task_id: str, chunk_ids: list[str], files: list[str],
                         reason: str, expected_revision: int) -> dict:
    return tasks.request_scope(FORUM_DIR, _author(author), _task_id(task_id), chunk_ids, files, reason, expected_revision)


def request_configuration_change(author: str, task_id: str, files: list[str], reason: str,
                                 proposed_changes: str, modules: list[str] | None = None,
                                 lake_args: list[str] | None = None, project_scope: str | None = None) -> dict:
    """Queue changes for the controller's serialized configuration transaction."""
    author, task_id = _author(author), _task_id(task_id)
    if not reason.strip() or not proposed_changes.strip(): raise ValueError("configuration request needs rationale and proposed edits")
    checked = sorted({tasks.relative_path(path) for path in files})
    requested = {}
    if modules is not None:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'.]*(?:\.[A-Za-z_][A-Za-z0-9_']*)*", m) for m in modules):
            raise ValueError("supplemental modules must be Lean module names")
        requested["modules"] = sorted(set(modules))
    if lake_args is not None:
        from ..bump_dependencies import checked_lake_args
        requested["lake_args"] = checked_lake_args(lake_args)
    if project_scope is not None:
        if project_scope not in {"build", "all"}: raise ValueError("unknown project scope")
        requested["project_scope"] = project_scope
    with tasks.transaction(FORUM_DIR) as state:
        if task_id not in state["tasks"]: raise ValueError("unknown task")
        if requested.get("project_scope") == "build" and state["config"].get("project_scope") == "all":
            raise ValueError("workers cannot drop required all-module build scope")
        if "modules" in requested:
            requested["modules"] = sorted(set(requested["modules"]) | set(state["config"].get("modules", [])))
        key = "configuration-" + str(len(state["config_requests"]) + 1)
        row = {"request_id": key, "task_id": task_id, "author": author, "files": checked,
               "reason": reason, "proposed_changes": proposed_changes, "status": "pending",
               "config_revision": state["config"]["revision"], "requested_config": requested}
        state["config_requests"][key] = row
        tasks._event(state, "configuration_requested", author, request_id=key, task_id=task_id)
    return row


def submit_repair(author: str, task_id: str, notes: str = "") -> dict:
    """Commit leased paths in this worker tree and submit immutable evidence.

    No merge or main-worktree mutation occurs here.  The coordination lock spans
    preflight, Git and publication so a scope/configuration race cannot sneak
    side effects past a failed ownership check.
    """
    author, task_id = _author(author), _task_id(task_id)
    with tasks.transaction(FORUM_DIR) as state:
        task = tasks._owned(state, author, task_id)
        if task["status"] != "running": raise ValueError("task already submitted")
        if task["config_revision"] != state["config"]["revision"]: raise ValueError("assignment configuration is stale")
        root = Path(task.get("worktree", "")).resolve()
        if not task.get("worktree") or root == _root(): raise ValueError("submission requires an assigned worker worktree")
        if Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
            raise ValueError("assigned path is not a Git worktree root")
        common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        expected = Path(_git(_root(), "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        if common.resolve() != expected.resolve(): raise ValueError("worker belongs to a different repository")
        allowed = {tasks.relative_path(path) for path in task["files"]}
        for path in allowed:
            if state["leases"].get(path, {}).get("task_id") != task_id: raise ValueError("file lease missing")
            if not (root / path).resolve().is_relative_to(root): raise ValueError("leased path escapes worker tree")
        if task["kind"] not in {"configuration", "dependency", "scope_review"} and any(Path(p).name in _CONFIG_FILES for p in allowed):
            raise ValueError("configuration edits require a configuration assignment")
        working = set(filter(None, _git(root, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0")))
        untracked = set(filter(None, _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")))
        staged = set(filter(None, _git(root, "diff", "--cached", "--name-only", "--no-renames", "-z").split("\0")))
        if not working | untracked | staged <= allowed:
            raise ValueError("worker contains changes outside leased files")
        base = task["base_main_sha"]
        _git(root, "merge-base", "--is-ancestor", base, "HEAD")
        existing = set(filter(None, _git(root, "diff", "--name-only", "--no-renames", "-z", base, "HEAD").split("\0")))
        if not existing <= allowed: raise ValueError("worker history changes unassigned files")
        changed = working | untracked | staged
        if changed:
            _git(root, "add", "--", *sorted(changed))
            _git(root, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "Bump repair: " + task_id)
        commit = _git(root, "rev-parse", "HEAD").strip()
        files = list(filter(None, _git(root, "diff", "--name-only", "--no-renames", "-z", base, commit).split("\0")))
        diff = _git(root, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", base, commit)
        record = artifacts.store_text(_root() / ".unity" / "artifacts", diff,
                                      kind="bump_migration_candidate", producer=author)
        return tasks._submit_candidate(state, author, task_id, commit_sha=commit, base_main_sha=base,
            task_revision=task["revision"], config_revision=task["config_revision"], files=files,
            notes=notes, diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
            artifact={"artifact_id": record["artifact_id"], "sha256": record["sha256"]})


def sync_from_main(author: str, task_id: str) -> dict:
    """Fast-forward an unmodified worker only; never discard edits or commits."""
    author, task_id = _author(author), _task_id(task_id)
    with tasks.transaction(FORUM_DIR) as state:
        task = tasks._owned(state, author, task_id)
        if task["status"] != "running": raise ValueError("submitted candidates are immutable")
        if task["config_revision"] != state["config"]["revision"]: raise ValueError("configuration changed; request a new assignment")
        if not task.get("worktree"): raise ValueError("no assigned worktree")
        root = Path(task["worktree"])
        if _git(root, "status", "--porcelain").strip(): raise ValueError("worker has local changes; no reset performed")
        _git(root, "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", state["main_sha"])
        task["base_main_sha"] = state["main_sha"]
        return {"status": "synced", "main_sha": state["main_sha"]}


def record_workflow_change(author: str, original: str, problem: str, evidence: str,
                           revised: str, result: str) -> dict:
    return tasks.record_workflow_change(FORUM_DIR, _author(author), original=original,
        problem=problem, evidence=evidence, revised=revised, result=result)


def request_review_repair(author: str, snapshot_id: str, chunk_ids: list[str], files: list[str],
                           reason: str) -> dict:
    """Queue a precise new critic finding, including previously clean chunks.

    This grants no writer lease, changes no source, and does not alter the
    reviewed snapshot. The rejection verdict must name every queued task.
    """
    author = _author(author)
    if PROFILE != "critic": raise ValueError("review repair requests require critic profile")
    if not reason.strip(): raise ValueError("review repair needs a concrete reason")
    with tasks.transaction(FORUM_DIR) as state:
        review = state["review"]
        snapshot = review.get("snapshot", review)
        if snapshot.get("snapshot_id") != snapshot_id or not snapshot_id:
            raise ValueError("critic snapshot is stale")
        if snapshot.get("main_sha") != state["main_sha"] or snapshot.get("config_revision") != state["config"]["revision"]:
            raise ValueError("critic snapshot does not bind current source/configuration")
        contributors = {c["author"].casefold() for c in state["candidates"].values() if c["status"] == "merged"}
        contributors.update(a.casefold() for a in snapshot.get("authors", []))
        if author.casefold() in contributors: raise ValueError("critic must be independent of migration contributors")
        members = sorted(set(chunk_ids))
        if not set(members) <= state["chunks"].keys(): raise ValueError("unknown review chunk")
        paths = sorted({tasks.relative_path(path) for path in files} | {state["chunks"][cid]["path"] for cid in members})
        if not paths: raise ValueError("review finding needs precise affected files or chunks")
        if any(path in state["leases"] for path in paths): raise ValueError("review cannot reassign an active writer")
        configuration = any(Path(path).name in _CONFIG_FILES or "vendor" in Path(path).parts for path in paths)
        overlaps = [task for task in state["tasks"].values() if task["status"] != "grouped"
                    and (set(task["chunk_ids"]) & set(members) or (not members and set(task["files"]) & set(paths)))]
        if any(task.get("owner") for task in overlaps): raise ValueError("review finding overlaps an owned assignment")
        if len(overlaps) == 1 and set(members) <= set(overlaps[0]["chunk_ids"]) and set(paths) <= set(overlaps[0]["files"]):
            task = overlaps[0]
            task["revision"] += 1
        else:
            key = "review-repair-" + tasks.digest([snapshot_id, members, paths])[:16]
            task = state["tasks"].get(key) or tasks._task(key, "configuration" if configuration else ("repair" if members else "command"), members, paths)
            task["attempts"] = max([task["attempts"], *[t["attempts"] for t in overlaps]])
            state["tasks"][key] = task
        task.update(status="pending", reason=reason, review_snapshot_id=snapshot_id,
                    review_author=author, config_revision=state["config"]["revision"])
        if configuration: task["kind"] = "configuration"
        tasks._event(state, "review_repair_requested", author, snapshot_id=snapshot_id,
                     task_id=task["task_id"], reason=reason)
        return {"task_ids": [task["task_id"]], "task": deepcopy(task)}


def submit_migration_verdict(author: str, snapshot_id: str, approved: bool, reasoning: str,
                             reopen_task_ids: list[str] | None = None) -> dict:
    author = _author(author)
    if PROFILE != "critic": raise ValueError("migration verdict requires critic profile")
    reopen = list(dict.fromkeys(reopen_task_ids or []))
    if not reasoning.strip(): raise ValueError("critic reasoning required")
    if approved and reopen: raise ValueError("approved review cannot reopen tasks")
    with tasks.transaction(FORUM_DIR) as state:
        review = state["review"]
        snapshot = review.get("snapshot", review)
        if not snapshot_id or snapshot.get("snapshot_id") != snapshot_id:
            raise ValueError("critic snapshot is stale")
        if snapshot.get("main_sha") != state["main_sha"] or snapshot.get("config_revision") != state["config"]["revision"]:
            raise ValueError("critic snapshot does not bind current source/configuration")
        contributors = {c["author"].casefold() for c in state["candidates"].values() if c["status"] == "merged"}
        contributors.update(a.casefold() for a in snapshot.get("authors", []))
        if author.casefold() in contributors: raise ValueError("critic must be independent of migration contributors")
        queued = {tid for tid, task in state["tasks"].items() if task.get("review_snapshot_id") == snapshot_id
                  and task["status"] not in {"complete", "grouped"}}
        if queued and (approved or not queued <= set(reopen)):
            raise ValueError("unresolved review repair tasks must all be included in a rejection verdict")
        if approved:
            snapshot_hash = hashlib.sha256(json.dumps({k: v for k, v in snapshot.items() if k != "sha256"},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            if not snapshot.get("passed") or snapshot.get("sha256") != snapshot_hash:
                raise ValueError("approval requires the intact passed machine-evidence snapshot")
        if any(tid not in state["tasks"] for tid in reopen): raise ValueError("unknown reopen task")
        if not approved and not reopen: raise ValueError("rejected review must identify precise tasks to reopen")
        verdict = {"author": author, "snapshot_id": snapshot_id, "approved": approved,
                   "verdict": "approved" if approved else "rejected", "snapshot_sha256": snapshot.get("sha256"),
                   "reasoning": reasoning, "reopen_task_ids": reopen, "at": tasks.now()}
        review["verdict"] = verdict
        tasks._event(state, "migration_verdict", author, snapshot_id=snapshot_id, approved=approved)
    return verdict


COMMON = (bump_status, bump_brief, bump_task, forum_post, forum_read, publish_finding,
          report_obstacle, ask_question, answer_question, record_workflow_change)
WORK = (register_strategy, claim_strategy, assist_strategy, yield_task, request_repair_scope,
        request_configuration_change, submit_repair, sync_from_main)
PROFILE_TOOLS = {"preparing": COMMON + WORK, "repairing": COMMON + WORK,
                 "critic": COMMON + (request_review_repair, submit_migration_verdict,), "retrospective": COMMON}


def build_server(profile: str) -> FastMCP:
    if profile not in PROFILES: raise ValueError("unknown compiler Bump profile")
    server = FastMCP("unity-bump-forum-" + profile)
    for function in PROFILE_TOOLS[profile]: server.tool(function)
    return server


def run(forum_dir: Path, project_root: Path, profile: str) -> None:
    configure(forum_dir, project_root, profile)
    build_server(profile).run()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forum-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    args = parser.parse_args()
    run(args.forum_dir, args.project_root, args.profile)


if __name__ == "__main__":
    main()
