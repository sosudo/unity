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
import time

from fastmcp import FastMCP

from .. import artifacts, bump_tasks as tasks
from . import server as discussion

FORUM_DIR = Path("forum")
PROJECT_ROOT: Path | None = None
PROFILE = "repairing"
PROFILES = {"preparing", "repairing", "critic", "retrospective", "assisting"}
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


def _status_counts(rows: dict) -> dict:
    counts = {}
    for row in rows.values():
        status = row.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def bump_status(summary: bool = False) -> dict:
    """Current status; opt into a compact summary to avoid full task and candidate records."""
    if type(summary) is not bool:
        raise ValueError("summary must be a boolean")
    state = tasks.load(FORUM_DIR)
    if not summary:
        # Preserve the original no-argument result for existing callers.
        counts = {}
        for task in state["tasks"].values(): counts[task["status"]] = counts.get(task["status"], 0) + 1
        return {key: state[key] for key in ("run_id", "revision", "phase", "main_sha", "target_version", "config", "review")} | {
            "task_counts": counts, "tasks": list(state["tasks"].values()), "candidates": list(state["candidates"].values())}
    scope = state.get("config", {}).get("scope") or {}
    review = state.get("review") or {}
    snapshot = review.get("snapshot") or {}
    verdict = review.get("verdict") or {}
    return {"run_id": state.get("run_id"), "revision": state.get("revision"), "phase": state.get("phase"),
            "main_sha": state.get("main_sha"), "target_version": state.get("target_version"),
            "config_revision": state.get("config", {}).get("revision"),
            "scope_counts": {"selected_modules": len(scope.get("selected_modules") or {}),
                             "default_modules": len(scope.get("default_modules") or {}),
                             "excluded_files": len(scope.get("excluded_files") or {})},
            "task_counts": _status_counts(state["tasks"]), "candidate_counts": _status_counts(state.get("candidates", {})),
            "task_total": len(state["tasks"]), "candidate_total": len(state.get("candidates", {})),
            "review": {"snapshot_id": snapshot.get("snapshot_id"), "passed": snapshot.get("passed"),
                       "reviewer": review.get("reviewer"), "verdict": verdict.get("verdict"),
                       "approved": verdict.get("approved")},
            "qualification": "Current summary, not a fixed snapshot or acceptance evidence."}


def bump_status_page(kind: str, offset: int = 0, limit: int = 20) -> dict:
    """Read a bounded page of current task or candidate summaries, ordered by ID."""
    if not isinstance(kind, str) or kind not in {"tasks", "candidates"}: raise ValueError("kind must be tasks or candidates")
    if type(offset) is not int or offset < 0: raise ValueError("offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 50: raise ValueError("limit must be between 1 and 50")
    state = tasks.load(FORUM_DIR)
    rows = state[kind]
    keys = sorted(rows)
    selected = keys[offset:offset + limit]
    if kind == "tasks":
        items = [{"task_id": key, "kind": rows[key].get("kind"), "status": rows[key].get("status"),
                  "owner": rows[key].get("owner"), "revision": rows[key].get("revision"),
                  "attempts": rows[key].get("attempts"), "file_count": len(rows[key].get("files") or []),
                  "chunk_count": len(rows[key].get("chunk_ids") or [])} for key in selected]
    else:
        items = [{"candidate_id": key, "task_id": rows[key].get("task_id"),
                  "status": rows[key].get("status"), "author": rows[key].get("author"),
                  "task_revision": rows[key].get("task_revision")} for key in selected]
    next_offset = offset + len(items) if offset + len(items) < len(keys) else None
    return {"kind": kind, "revision": state.get("revision"), "offset": offset, "limit": limit,
            "total": len(keys), "next_offset": next_offset, "items": items,
            "qualification": "Current read; pages may change between calls. Use bump_task for task details and controller evidence for status."}


_REVIEW_CONTEXT_KINDS = {"finding", "obstacle", "question", "answer", "assistance", "assistance_request"}


def bump_review_context(snapshot_id: str, offset: int = 0, limit: int = 20,
                        event_cutoff: int | None = None) -> dict:
    """Page immutable agent reports for one exact current critic snapshot."""
    if PROFILE != "critic":
        raise ValueError("review context is available only to the Bump critic")
    if type(offset) is not int or offset < 0:
        raise ValueError("review context offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("review context limit must be between 1 and 50")

    state = tasks.load(FORUM_DIR)
    if state.get("phase") != "critic":
        raise ValueError("Bump is no longer in critic review")
    review = state.get("review") or {}
    snapshot = review.get("snapshot") or review
    if (not snapshot_id or snapshot.get("snapshot_id") != snapshot_id
            or snapshot.get("main_sha") != state.get("main_sha")
            or snapshot.get("config_revision") != state.get("config", {}).get("revision")):
        raise ValueError("review context snapshot is stale or does not bind current source/configuration")

    all_events = state.get("events", [])
    if event_cutoff is None:
        event_cutoff = len(all_events)
    elif type(event_cutoff) is not int or not 0 <= event_cutoff <= len(all_events):
        raise ValueError("review context event cutoff is invalid")

    strategies = state.get("strategies", {})
    tasks_by_id = state.get("tasks", {})
    rows = []
    for event in all_events[:event_cutoff]:
        if event.get("kind") not in _REVIEW_CONTEXT_KINDS:
            continue
        task_id = event.get("task_id")
        if not task_id and event.get("kind") == "assistance":
            task_id = (strategies.get(event.get("strategy_id")) or {}).get("task_id")
        task = tasks_by_id.get(task_id, {})
        rows.append({
            "event_id": event.get("event_id"),
            "question_id": event.get("question_id"),
            "strategy_id": event.get("strategy_id"),
            "task_id": task_id,
            "task_files": list(task.get("files", [])),
            "author": event.get("author"),
            "kind": event.get("kind"),
            "content": event.get("content", event.get("contribution", "")),
            "evidence": event.get("evidence", ""),
            "at": event.get("at"),
        })

    page = rows[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(rows) else None
    return {
        "snapshot_id": snapshot_id,
        "main_sha": snapshot["main_sha"],
        "config_revision": snapshot["config_revision"],
        "event_cutoff": event_cutoff,
        "offset": offset,
        "limit": limit,
        "total": len(rows),
        "next_offset": next_offset,
        "items": page,
        "qualification": "Agent-reported collaboration context, not verified findings or workflow status.",
    }


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


def _candidate_outcomes(state: dict, task_id: str, files: list[str]) -> dict:
    """Bounded exact-task summaries; worker notes and controller feedback stay distinct."""
    def short(value, limit=400):
        if not isinstance(value, str):
            return None
        return value if len(value) <= limit else value[:limit] + "…"

    def preview(rows):
        if not isinstance(rows, list):
            return {"count": None, "items": []}
        relevant = [row for row in rows if isinstance(row, dict) and row.get("path") in files]
        other = [row for row in rows if isinstance(row, dict) and row.get("path") not in files]
        return {"count": len(rows), "relevant_count": len(relevant), "items": [
            {"path": short(row.get("path")), "line": row.get("line") if type(row.get("line")) is int else None,
             "column": row.get("column") if type(row.get("column")) is int else None,
             "kind": short(row.get("kind"), 80), "message": short(row.get("message"))}
            for row in (relevant + other)[:3]]}

    candidates = [row for row in state.get("candidates", {}).values() if row.get("task_id") == task_id]
    candidates.sort(key=lambda row: (str(row.get("submitted") or ""), str(row.get("candidate_id") or "")), reverse=True)
    items = []
    for row in candidates[:3]:
        feedback = row.get("feedback") if isinstance(row.get("feedback"), dict) else None
        summary = None
        if feedback is not None:
            build = feedback.get("build") if isinstance(feedback.get("build"), dict) else {}
            log = build.get("log_ref") if isinstance(build.get("log_ref"), dict) else {}
            command = build.get("command")
            resolved = feedback.get("resolved_diagnostic_ids")
            summary = {"accepted": feedback.get("accepted") if type(feedback.get("accepted")) is bool else None,
                       "made_progress": feedback.get("made_progress") if type(feedback.get("made_progress")) is bool else None,
                       "reason": short(feedback.get("reason")),
                       "resolved_diagnostic_count": len(resolved) if isinstance(resolved, list) else None,
                       "remaining_errors": preview(feedback.get("remaining_errors")),
                       "regressions": preview(feedback.get("regressions")),
                       "build_command": [short(part) for part in command[:12]] if isinstance(command, list) else None,
                       "build_command_omitted": max(0, len(command) - 12) if isinstance(command, list) else None,
                       "build_log_ref": {key: short(log.get(key)) for key in ("artifact_id", "sha256") if key in log}}
        items.append({"candidate_id": short(row.get("candidate_id")), "author": short(row.get("author")),
                      "status": short(row.get("status")), "submitted": short(row.get("submitted")),
                      "base_main_sha": short(row.get("base_main_sha")), "commit_sha": short(row.get("commit_sha")),
                      "diff_sha256": short(row.get("diff_sha256")), "worker_notes": short(row.get("notes")),
                      "controller_feedback": summary})
    return {"total": len(candidates), "omitted": max(0, len(candidates) - len(items)), "items": items,
            "qualification": "Exact-task snapshot; worker notes are reports. Controller feedback is candidate-specific; merged partial progress is not final acceptance."}


def bump_task(task_id: str) -> dict:
    if PROFILE == "assisting": _task_id(task_id)
    state = tasks.load(FORUM_DIR)
    task = state["tasks"][task_id]
    available = []
    for cid, row in state["chunks"].items():
        if (row["path"] not in task["files"] or row.get("scope_excluded") or row.get("after_exit")
                or row.get("status") == "excluded"):
            continue
        owners = [other.get("owner") for other in state["tasks"].values()
                  if cid in other.get("chunk_ids", []) and other.get("owner")]
        lease_owner = state["leases"].get(row["path"], {}).get("author")
        available.append({"chunk_id": cid, "path": row["path"], "name": row["name"],
                          "status": row["status"], "owner": owners[0] if owners else lease_owner})
    available.sort(key=lambda row: (row["path"], state["chunks"][row["chunk_id"]]["start_line"], row["chunk_id"]))
    return {"task": task, "chunks": [state["chunks"][cid] for cid in task["chunk_ids"] if cid in state["chunks"]],
            "available_file_chunks": available,
            "candidate_outcomes": _candidate_outcomes(state, task_id, task["files"]),
            "strategies": [s for s in state["strategies"].values() if s["task_id"] == task_id]}


_TASK_CONTEXT_KINDS = {"finding", "obstacle", "question", "answer", "assistance", "assistance_request", "yield"}


def bump_task_context(task_id: str, offset: int = 0, limit: int = 20,
                      event_cutoff: int | None = None) -> dict:
    """Page collaboration reports tied to this exact Bump task."""
    if PROFILE not in {"preparing", "repairing", "assisting"}:
        raise ValueError("task context is available only to Bump workers")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task context needs a task ID")
    if type(offset) is not int or offset < 0:
        raise ValueError("task context offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("task context limit must be between 1 and 50")

    state = tasks.load(FORUM_DIR)
    if task_id not in state.get("tasks", {}):
        raise ValueError("unknown Bump task")
    if PROFILE == "assisting":
        bound = os.getenv("UNITY_BUMP_TASK_ID", "").strip()
        cursor, seen = task_id, set()
        while cursor != bound and cursor not in seen:
            seen.add(cursor)
            node = state["tasks"].get(cursor, {})
            if node.get("status") != "grouped": break
            cursor = node.get("grouped_into")
        if not bound or cursor != bound:
            raise ValueError("task context is not a grouped predecessor of the bound assignment")
    all_events = state.get("events", [])
    if event_cutoff is None:
        event_cutoff = len(all_events)
    elif type(event_cutoff) is not int or not 0 <= event_cutoff <= len(all_events):
        raise ValueError("task context event cutoff is invalid")

    strategies = state.get("strategies", {})
    rows = []
    report_counts = {}
    for event in all_events[:event_cutoff]:
        if event.get("kind") not in _TASK_CONTEXT_KINDS:
            continue
        event_task_id = event.get("task_id")
        if not event_task_id and event.get("kind") == "assistance":
            event_task_id = (strategies.get(event.get("strategy_id")) or {}).get("task_id")
        if event_task_id:
            report_counts[event_task_id] = report_counts.get(event_task_id, 0) + 1
        if event_task_id != task_id:
            continue
        rows.append({
            "event_id": event.get("event_id"),
            "question_id": event.get("question_id"),
            "strategy_id": event.get("strategy_id"),
            "task_id": task_id,
            "author": event.get("author"),
            "kind": event.get("kind"),
            "content": event.get("content", event.get("contribution", "")),
            "evidence": event.get("evidence", ""),
            "reason": event.get("reason", ""),
            "at": event.get("at"),
        })

    grouped_children = {}
    for child_id, child in state["tasks"].items():
        parent_id = child.get("grouped_into")
        if child.get("status") == "grouped" and isinstance(parent_id, str):
            grouped_children.setdefault(parent_id, []).append(child_id)
    grouped_predecessor_task_ids = set()
    visited = {task_id}
    pending = [task_id]
    omitted_empty_grouped_predecessor_count = 0
    while pending:
        parent_id = pending.pop()
        for child_id in grouped_children.get(parent_id, []):
            if child_id in visited:
                continue
            visited.add(child_id)
            pending.append(child_id)
            if report_counts.get(child_id, 0):
                grouped_predecessor_task_ids.add(child_id)
            else:
                omitted_empty_grouped_predecessor_count += 1

    page = rows[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(rows) else None
    return {
        "task_id": task_id,
        "grouped_predecessor_task_ids": sorted(grouped_predecessor_task_ids),
        "omitted_empty_grouped_predecessor_count": omitted_empty_grouped_predecessor_count,
        "grouped_predecessor_qualification": (
            "Only explicitly grouped descendants with communication reports in this event prefix are listed; "
            "empty grouped nodes were traversed to find reporting descendants."
        ),
        "event_cutoff": event_cutoff,
        "offset": offset,
        "limit": limit,
        "total": len(rows),
        "next_offset": next_offset,
        "items": page,
        "qualification": "Agent-reported task context, not verified findings or workflow status.",
    }


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


def _assistance_current(state: dict, request: dict) -> bool:
    task = state.get("tasks", {}).get(request.get("task_id"), {})
    return (task.get("status") == "running" and not task.get("yield_requested")
            and task.get("owner") == request.get("author")
            and task.get("revision") == request.get("task_revision")
            and task.get("attempts") == request.get("attempt")
            and task.get("base_main_sha") == request.get("base_main_sha")
            and task.get("config_revision") == request.get("config_revision")
            and state.get("config", {}).get("revision") == request.get("config_revision")
            and not any(r.get("status") == "pending" for r in state.get("config_requests", {}).values())
            and bool(task.get("files")) and all(
                state.get("leases", {}).get(path, {}).get("task_id") == request.get("task_id")
                and state["leases"][path].get("author") == request.get("author")
                and state["leases"][path].get("task_revision") == request.get("task_revision")
                for path in task.get("files", [])))


def request_assistance(author: str, task_id: str, question: str) -> dict:
    """Request one bounded advisory turn for this owned assignment."""
    if PROFILE != "repairing": raise ValueError("assistance requests require a repair worker")
    author, task_id = _author(author), _task_id(task_id)
    if not isinstance(question, str) or not question.strip() or len(question) > 12000:
        raise ValueError("provide a focused question and relevant uncommitted context")
    with tasks.transaction(FORUM_DIR) as state:
        task = tasks._owned(state, author, task_id)
        request = {"task_id": task_id, "author": author, "task_revision": task.get("revision"),
                   "attempt": task.get("attempts"), "base_main_sha": task.get("base_main_sha"),
                   "config_revision": task.get("config_revision")}
        if not _assistance_current(state, request): raise ValueError("assignment is not eligible for assistance")
        if any(e.get("kind") == "assistance_request" and e.get("task_id") == task_id
               and e.get("task_revision") == task["revision"] for e in state["events"]):
            raise ValueError("assistance already requested for this assignment")
        event = tasks._event(state, "assistance_request", author, content=question, **{k: v for k, v in request.items() if k != "author"})
    return event


def artifact_read(artifact_id: str, offset: int = 0, limit: int = artifacts.DEFAULT_READ_LIMIT) -> dict:
    """Read a bounded page of immutable Bump evidence or compacted tool output."""
    return artifacts.read_artifact(_root() / ".unity" / "artifacts", artifact_id, offset=offset, limit=limit)


def answer_assistance(author: str, request_id: str, answer: str) -> dict:
    """Publish advisory findings only for the controller-assigned active request."""
    author = _author(author)
    if PROFILE != "assisting" or request_id != os.getenv("UNITY_BUMP_ASSISTANCE_REQUEST", ""):
        raise ValueError("answer does not match the bound assistance request")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 12000:
        raise ValueError("answer must be nonempty and bounded")
    with tasks.transaction(FORUM_DIR) as state:
        request = next ( (e for e in state["events"] if e.get("event_id") == request_id and e.get("kind") == "assistance_request"), None)
        started = next((e for e in state["events"] if e.get("kind") == "assistance_started" and e.get("request_id") == request_id), None)
        if (not request or not started or started.get("helper") != author
                or not isinstance(started.get("deadline_at"), (int, float))
                or time.time() >= started["deadline_at"]
                or any(e.get("kind") in {"assistance_stopping", "assistance_finished"}
                       and e.get("request_id") == request_id for e in state["events"])
                or _task_id(request["task_id"]) != request["task_id"]
                or not _assistance_current(state, request)
                or any(e.get("kind") == "answer" and e.get("question_id") == request_id for e in state["events"])):
            raise ValueError("assistance request is no longer active or assigned")
        result = tasks._event(state, "answer", author, task_id=request["task_id"], question_id=request_id, content=answer)
    return result


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
                         reason: str, expected_revision: int, include_file_chunks: bool = False) -> dict:
    return tasks.request_scope(FORUM_DIR, _author(author), _task_id(task_id), chunk_ids, files, reason,
                               expected_revision, include_file_chunks=include_file_chunks)


def request_configuration_change(author: str, task_id: str, files: list[str], reason: str,
                                 proposed_changes: str, modules: list[str] | None = None,
                                 lake_args: list[str] | None = None, project_scope: str | None = None,
                                 module_renames: dict[str, str] | None = None) -> dict:
    """Queue changes for the controller's serialized configuration transaction."""
    author, task_id = _author(author), _task_id(task_id)
    if not reason.strip() or not proposed_changes.strip(): raise ValueError("configuration request needs rationale and proposed edits")
    checked = sorted({tasks.relative_path(path) for path in files})
    requested = {}
    if modules is not None:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'.]*(?:\.[A-Za-z_][A-Za-z0-9_']*)*", m) for m in modules):
            raise ValueError("supplemental modules must be Lean module names")
        requested["modules"] = sorted(set(modules))
    if module_renames is not None:
        name = r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
        if (not isinstance(module_renames, dict) or any(
                not isinstance(old, str) or not isinstance(new, str)
                or not re.fullmatch(name, old) or not re.fullmatch(name, new) or old == new
                for old, new in module_renames.items())
                or len(set(module_renames.values())) != len(module_renames)
                or bool(set(module_renames) & set(module_renames.values()))):
            raise ValueError("module renames must be one-to-one Lean module names")
        if module_renames and not checked:
            raise ValueError("module renames require leased source paths")
        requested["module_renames"] = dict(module_renames)
    if lake_args is not None:
        from ..bump_dependencies import checked_lake_args
        requested["lake_args"] = checked_lake_args(lake_args)
    if project_scope is not None:
        if project_scope not in {"build", "all"}: raise ValueError("unknown project scope")
        requested["project_scope"] = project_scope
    with tasks.transaction(FORUM_DIR) as state:
        if task_id not in state["tasks"]: raise ValueError("unknown task")
        if state.get("required_project_checks") and requested.get("lake_args"):
            raise ValueError("configured Lake options are unsupported with exact required project checks")
        if requested.get("project_scope") == "build" and state["config"].get("project_scope") == "all":
            raise ValueError("workers cannot drop required all-module build scope")
        if "modules" in requested:
            requested["modules"] = sorted(set(requested["modules"]) | set(state["config"].get("modules", [])))
        task = state["tasks"][task_id]
        eligible = set(state["config"].get("modules", [])) & set(state["config"].get("scope", {}).get("selected_modules", {}))
        if "module_renames" in requested:
            unsupported = set(requested["module_renames"]) - eligible
            if unsupported:
                raise ValueError("module rename sources must be selected supplemental modules: " + ", ".join(sorted(unsupported))
                                 + "; other source moves can be made in leased files without a module rename")
        old_renames = task.get("requested_config", {}).get("module_renames", {})
        invalid_old = set(old_renames) - eligible
        retained = {old: new for old, new in old_renames.items() if old in eligible}
        if invalid_old and "module_renames" in requested and any(
                requested["module_renames"].get(old) != new for old, new in retained.items()):
            raise ValueError("rename correction must retain every valid existing mapping")
        effective = {**state["config"], **task.get("requested_config", {})}
        effective["modules"] = sorted(set(state["config"].get("modules", [])) |
                                      set(task.get("requested_config", {}).get("modules", [])))
        covered_options = all(
            (set(value) <= set(effective.get("modules", [])) if key == "modules" else
             all(effective.get("module_renames", {}).get(old) == new for old, new in value.items())
                 if key == "module_renames" else value == effective.get(key))
            for key, value in requested.items())
        owns_files = (set(checked) <= set(task["files"]) and all(
            state["leases"].get(path, {}).get("task_id") == task_id
            and state["leases"].get(path, {}).get("author") == author for path in checked))
        if (task.get("kind") in {"configuration", "dependency", "scope_review"}
                and task.get("owner") == author and task.get("status") == "running"
                and owns_files and covered_options):
            corrected = bool(invalid_old and "module_renames" in requested and requested["module_renames"] == retained)
            if corrected:
                task["requested_config"]["module_renames"] = dict(retained)
                task["revision"] += 1
                for lease in state["leases"].values():
                    if lease.get("task_id") == task_id:
                        lease["task_revision"] = task["revision"]
                tasks._event(state, "configuration_rename_corrected", author, task_id=task_id,
                             original_renames=deepcopy(old_renames), corrected_renames=deepcopy(retained),
                             reason=reason, proposed_changes=proposed_changes)
            guidance = ("Invalid pending rename entries were removed. Continue editing leased files in your private worktree and call submit_repair."
                        if corrected else "Edit the leased files in your private worktree and call submit_repair.")
            tasks._event(state, "configuration_already_assigned", author, task_id=task_id,
                         files=checked, reason=reason, proposed_changes=proposed_changes,
                         requested_config=requested)
            response = {"status": "corrected" if corrected else "already_assigned", "task_id": task_id,
                        "files": checked, "requested_config": requested, "guidance": guidance}
            if corrected:
                response.update(corrected_renames=deepcopy(retained), removed_rename_sources=sorted(invalid_old))
            return response
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
        if task["status"] != "running" or task.get("check_only_requested"): raise ValueError("task already submitted or deferred")
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


def request_project_checks(author: str, task_id: str, commands: list[list[str]], reason: str,
                           check_only: bool = False) -> dict:
    """Request controller-run final checks; optionally defer a check-only review assignment."""
    from .. import bump_project_checks
    author, task_id = _author(author), _task_id(task_id)
    if PROFILE not in {"preparing", "repairing"}: raise ValueError("project checks require an assigned worker")
    if type(check_only) is not bool or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000: raise ValueError("provide a reason and a boolean check_only")
    with tasks.transaction(FORUM_DIR) as state:
        task = tasks._owned(state, author, task_id)
        if task["status"] != "running" or task.get("config_revision") != state["config"]["revision"]:
            raise ValueError("assignment is stale or submitted")
        if check_only:
            if (task.get("kind") not in {"configuration", "scope_review"} or task.get("chunk_ids")
                    or not task.get("review_snapshot_id") or not task.get("worktree")):
                raise ValueError("only a check-only review configuration assignment can be deferred")
            root = Path(task["worktree"]).resolve()
            if (root == _root() or Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root
                    or _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip() != _git(_root(), "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
                    or _git(root, "rev-parse", "HEAD").strip() != task.get("base_main_sha")
                    or any(state["leases"].get(path, {}).get("task_id") != task_id for path in task.get("files", []))
                    or _git(root, "status", "--porcelain", "--untracked-files=all")):
                raise ValueError("check-only deferral requires an unchanged assigned worktree")
        requested = bump_project_checks.add_to_state(state, commands, author, task_id=task_id)
        if check_only:
            task["check_only_requested"] = True
            task["project_checks"] = requested
            tasks._event(state, "project_check_deferred", author, task_id=task_id, commands=deepcopy(requested), reason=reason)
        return {"commands": requested, "check_revision": state.get("project_check_revision", 0),
                "status": "defer_after_worker_closure" if check_only else "required"}


def request_review_checks(author: str, snapshot_id: str, commands: list[list[str]], reason: str) -> dict:
    """Queue exact controller checks and a no-edit review obligation."""
    from .. import bump_project_checks
    author = _author(author)
    if PROFILE != "critic" or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000: raise ValueError("critic review and a reason are required")
    with tasks.transaction(FORUM_DIR) as state:
        review = state.get("review", {})
        snapshot = review.get("snapshot", review)
        if (state.get("phase") != "critic" or not snapshot_id or snapshot.get("snapshot_id") != snapshot_id
                or snapshot.get("main_sha") != state.get("main_sha")
                or snapshot.get("config_revision") != state.get("config", {}).get("revision")
                or review.get("reviewer") != author):
            raise ValueError("critic snapshot or reviewer is stale")
        requested = bump_project_checks.add_to_state(state, commands, author, snapshot_id=snapshot_id)
        if (snapshot.get("passed") and all(command in snapshot.get("required_project_checks", []) for command in requested)):
            from .. import bump_verification
            bump_verification.require_project_check_receipts(state, snapshot)
            return {"task_ids": [], "commands": requested, "status": "already_satisfied"}
        tid = "review-check-" + tasks.digest([snapshot_id, requested])[:16]
        if tid in state["tasks"]:
            return {"task_ids": [tid], "commands": requested, "status": state["tasks"][tid]["status"]}
        task = tasks._task(tid, "project_check", [], [])
        task.update(status="awaiting_checks", reason=reason, review_snapshot_id=snapshot_id,
                    review_author=author, project_checks=deepcopy(requested), check_only_requested=True)
        state["tasks"][tid] = task
        tasks._event(state, "review_checks_requested", author, snapshot_id=snapshot_id, task_id=tid,
                     commands=deepcopy(requested), reason=reason)
        return {"task_ids": [tid], "commands": requested, "status": "awaiting_checks"}


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
        if not review.get("reviewer") or author != review["reviewer"]:
            raise ValueError("migration review requires the controller-assigned primary")
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
        task.pop("check_only_requested", None)
        task.pop("project_checks", None)
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
        if not review.get("reviewer") or author != review["reviewer"]:
            raise ValueError("migration review requires the controller-assigned primary")
        queued = {tid for tid, task in state["tasks"].items() if task.get("review_snapshot_id") == snapshot_id
                  and task["status"] not in {"complete", "grouped"}}
        if queued and (approved or not queued <= set(reopen)):
            raise ValueError("unresolved review repair tasks must all be included in a rejection verdict")
        if approved:
            from .. import bump_verification
            bump_verification.require_project_check_receipts(state, snapshot)
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


COMMON = (bump_status, bump_status_page, bump_brief, bump_task, artifact_read, forum_post, forum_read, publish_finding,
          report_obstacle, ask_question, answer_question, record_workflow_change)
WORK = (request_project_checks, register_strategy, claim_strategy, assist_strategy, yield_task, request_repair_scope,
        request_configuration_change, submit_repair, sync_from_main)
TASK_CONTEXT = (bump_task_context,)
PROFILE_TOOLS = {"preparing": COMMON + TASK_CONTEXT + WORK, "repairing": COMMON + TASK_CONTEXT + WORK + (request_assistance,),
                 "critic": COMMON + (bump_review_context, request_review_checks, request_review_repair, submit_migration_verdict,),
                 "retrospective": COMMON,
                 "assisting": (bump_brief, bump_task, bump_task_context, artifact_read, answer_assistance)}


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
