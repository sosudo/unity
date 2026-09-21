"""Phase-scoped Forum for solve's informal and formal work.

Both phases share solve-state.json and discussion; formal tools are independent
solve-owned copies. Prove and standalone autoformalize use other interfaces.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import stat
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from .. import artifacts, solve_contract, solve_state, worktree
from ..solve_review import (
    SemanticReview, RepresentationRepairRequest, RepresentationReview, SourceDiagnosis,
)
from .. import solve_representation, solve_files
from ..solve_spec import normalize_outputs
from . import server as discussion


FORUM_DIR = Path("forum")
PROJECT_ROOT: Path | None = None
PROFILE = "solving"
PROFILES = {"solving", "solution_review", "chunking", "formalizing", "critic", "retrospective", "source_repair", "representation_review"}


def configure(forum_dir: Path, project_root: Path, profile: str = "solving") -> None:
    global FORUM_DIR, PROJECT_ROOT, PROFILE
    if profile not in PROFILES:
        raise ValueError(f"unknown solve Forum profile '{profile}'")
    FORUM_DIR = Path(forum_dir)
    PROJECT_ROOT = Path(project_root).resolve()
    PROFILE = profile
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    discussion.FORUM_DIR = FORUM_DIR
    discussion.PROJECT_ROOT = PROJECT_ROOT
    discussion.ICRL_ENABLED = False


def _root() -> Path:
    if PROJECT_ROOT is None:
        raise ValueError("solve Forum requires a configured project root")
    return PROJECT_ROOT


def _artifacts_dir() -> Path:
    return _root() / ".unity" / "artifacts"


def _author(author: str) -> str:
    value = str(author or "").strip()
    if not value:
        raise ValueError("author is required")
    bound = os.getenv("UNITY_AGENT_NAME", "").strip()
    if bound and value.casefold() != bound.casefold():
        raise ValueError(f"this worker is bound to author '{bound}'")
    return bound or value


def _thread_id(thread_id: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(thread_id or "global")).strip("-")
    return "solve-" + (key or "global")


def _ensure_thread(thread_id: str) -> str:
    tid = _thread_id(thread_id)
    discussion.forum_create_thread(tid, f"Solve: {thread_id or 'Global'}")
    return tid


def _mirror(author: str, title: str, body: str, target: str = "") -> None:
    tid = _ensure_thread(target or "global")
    discussion.forum_post(tid, author, f"{title}\n\n{body}"[:8000])


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )
    if check and result.returncode:
        raise ValueError(
            result.stderr.strip() or result.stdout.strip()
            or f"git {' '.join(args)} failed with exit code {result.returncode}"
        )
    return result


@contextmanager
def _finalization_lock(author: str):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", author)
    path = FORUM_DIR / f"solve-finalize-{safe}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _merge_lock():
    """Serialize contract/source reopen with controller integration and final acceptance."""
    path = _root() / ".unity" / "forum" / "merge.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _try_merge_lock():
    """Never wait for main while holding a worker's finalization lock."""
    path = _root() / ".unity" / "forum" / "merge.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _submit_formal_commit(
    strategy_id: str,
    author: str,
    task_id: str,
    commit_sha: str,
    *,
    notes: str = "",
    supersedes: str = "",
    stage: str = "complete",
    outputs: list[dict] | None = None,
    obsolete_files: list[dict] | None = None,
    submission_context: dict | None = None,
) -> dict:
    from .. import solve_cache, solve_files

    if submission_context is None:
        preflight = solve_state.preflight_formal_submission(
            FORUM_DIR, strategy_id, author, task_id, stage=stage, outputs=outputs,
        )
        if preflight["status"] == "ok":
            submission_context = preflight["context"]
        elif preflight["status"] != "conflict":
            return preflight
        # A committed retry has no worktree side effects. Let the state layer
        # identify its exact existing candidate before enforcing mutable gates.
    resolved = worktree.verify_candidate_commit(
        _root(), author, commit_sha, allow_unchanged=True,
    )
    base = _git(_root(), "merge-base", resolved, worktree.main_commit(_root())).stdout.strip()
    diff = _git(
        _root(), "diff", "--no-ext-diff", "--no-textconv",
        "--binary", "--full-index", base, resolved,
    ).stdout
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()
    actual_paths = solve_files.immutable_git_paths(_root(), base, resolved)
    representation_observation = None
    if stage == "representation":
        state = solve_state.load_state(FORUM_DIR)
        task = state.get("formal_tasks", {}).get(task_id, {})
        if task.get("representation", {}).get("status") == "adopted":
            observed = solve_state._representation_submission_context(state, task_id)
            accepted_main = observed["main_sha"]
            if accepted_main and re.fullmatch(r"[0-9a-f]{40}", accepted_main):
                # Compare all tracked files, not only outputs: an unchanged
                # target file may import a helper that the candidate changed.
                candidate_tree = _git(_root(), "rev-parse", "--verify", f"{resolved}^{{tree}}").stdout.strip()
                accepted_tree = _git(_root(), "rev-parse", "--verify", f"{accepted_main}^{{tree}}").stdout.strip()
                if candidate_tree == accepted_tree:
                    representation_observation = observed
    # Normal submissions do not fingerprint dependencies just for a cache lookup.
    # Only a prior deterministic rejection justifies that read. Acquire main
    # nonblockingly: sync uses merge -> author locks, and this caller owns author.
    state = solve_state.load_state(FORUM_DIR)
    retry = any(item.get("task_id") == task_id
                and item.get("status") == "failed"
                and item.get("failure_context", {}).get("cacheable")
                and solve_state.candidate_is_current(state, item)
                for item in state.get("formal_candidates", {}).values())
    bindings = normalize_outputs(outputs) if outputs is not None else state.get("formal_tasks", {}).get(task_id, {}).get("outputs", [])
    receipt, inventory = None, []
    policy_sha = solve_contract.policy_hash()
    for item in state.get("formal_candidates", {}).values():
        checked = item.get("verification") or {}
        if (checked.get("status") != "passed" or not checked.get("inventory_artifact")
                or checked.get("policy_sha256") != policy_sha
                or checked.get("source_identity", {}).get("main_sha")
                != state.get("formalization", {}).get("main_sha")):
            continue
        names = solve_files.checked_inventory(_artifacts_dir(), checked)
        if names and solve_files.inventory_blockers(bindings, names):
            receipt, inventory = checked, names
            break
    with (_try_merge_lock() if retry or receipt else nullcontext(False)) as locked:
        observation = None
        inventory_observation = None
        if locked:
            state = solve_state.load_state(FORUM_DIR)
            try:
                if retry:
                    observation = solve_contract.observe_failure_inputs(
                        _root(), state["formalization"].get("contract") or {},
                    )
                if (receipt and receipt["source_identity"]["main_sha"] == state["formalization"].get("main_sha")
                        and _git(_root(), "rev-parse", f"{resolved}^{{tree}}").stdout.strip()
                        == _git(_root(), "rev-parse", f"{receipt['source_identity']['main_sha']}^{{tree}}").stdout.strip()
                        and solve_contract.source_identity(_root()) == receipt["source_identity"]
                        and solve_cache.compiled_receipt_current(_root(), receipt.get("compiled_receipt"))):
                    inventory_observation = {
                        "state_context": solve_state.failure_state_context(state),
                        "project_declarations": inventory,
                    }
            except (OSError, ValueError):
                pass  # An unavailable cache observation never establishes rejection.
        result = solve_state.submit_formal_candidate(
            FORUM_DIR, strategy_id, author, task_id, resolved, base, diff_sha,
            notes=notes, supersedes=supersedes, stage=stage, outputs=outputs,
            representation_observation=representation_observation,
            failure_observation=observation,
            inventory_observation=inventory_observation,
            submission_context=submission_context,
            obsolete_files=obsolete_files, **actual_paths,
        )
    if result["status"] == "submitted" and not result.get("idempotent"):
        candidate = result["candidate"]
        _mirror(author, f"FORMAL CANDIDATE {candidate['candidate_id']}",
                f"task {task_id}, commit {resolved}, diff SHA-256 {diff_sha}", task_id)
    return result


def solve_status() -> dict:
    """Return exact authoritative state for the current solve run."""
    return solve_state.load_state(FORUM_DIR)


def read_metrics(forum_dir: Path, project_root: Path) -> dict:
    """Read telemetry; repair latency ends at diagnostic clearance, not acceptance."""
    state = solve_state.load_state(forum_dir)
    events = state.get("events", [])
    first_by_kind: dict[str, float] = {}
    counts: dict[str, int] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        if kind and kind not in first_by_kind:
            first_by_kind[kind] = float(event.get("timestamp") or 0)
    runs = []
    run_log = project_root / ".unity" / "logs" / "run.jsonl"
    if run_log.exists():
        for line in run_log.read_text(errors="replace").splitlines()[-5000:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            context = row.get("context") or {}
            if context.get("command") == "solve" and context.get("run_id") == state.get("run_id"):
                runs.append(row)
    by_phase: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_task: dict[str, dict] = {}
    for row in runs:
        context = row.get("context") or {}
        phase = context.get("phase") or "unknown"
        model = row.get("model") or "unknown"
        usage = row.get("usage") or {}
        task_key = context.get("task_id") or f"role:{context.get('role') or phase}"
        for key, bucket_key in ((phase, by_phase), (model, by_model), (task_key, by_task)):
            bucket = bucket_key.setdefault(key, {
                "turns": 0, "seconds": 0.0, "cost_usd": 0.0,
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            })
            bucket["turns"] += 1
            bucket["seconds"] += float(row.get("seconds") or 0)
            bucket["cost_usd"] += float(row.get("cost_usd") or 0)
            for token_key in ("input_tokens", "output_tokens", "total_tokens"):
                bucket[token_key] += int(usage.get(token_key) or 0)
    started = first_by_kind.get("solve_initialized", 0)
    submitted = first_by_kind.get("formal_candidate_submitted", 0)
    accepted = first_by_kind.get("formal_candidate_merged", 0)
    completed = max((
        float(event.get("timestamp") or 0) for event in events
        if event.get("kind") == "critic_review_completed"
    ), default=0)
    repair_records = list(state.get("manifest_repairs", {}).values())
    repair_counts: dict[str, int] = {}
    repair_attempt_counts: dict[str, int] = {}
    repair_timestamps = []
    current_repairs = {row["repair_id"] for row in solve_state.current_manifest_repairs(state)}
    for repair in repair_records:
        status = str(repair.get("status") or "unknown")
        repair_counts[status] = repair_counts.get(status, 0) + 1
        attempts = repair.get("attempts", [])
        for attempt in attempts:
            outcome = str(attempt.get("status") or "unknown")
            repair_attempt_counts[outcome] = repair_attempt_counts.get(outcome, 0) + 1
        created = repair.get("created_at")
        finished = repair.get("resolved_at")
        if finished is None:
            finished = repair.get("exhausted_at")
        latency = (round(finished - created, 3)
                   if isinstance(created, (int, float)) and isinstance(finished, (int, float))
                   and finished >= created else None)
        repair_timestamps.append({
            "repair_id": repair.get("repair_id"), "task_id": repair.get("task_id"),
            "kind": repair.get("kind"), "status": repair.get("status"),
            "is_current": repair.get("repair_id") in current_repairs,
            "diagnostic_latency_seconds": latency,
            "created_at": repair.get("created_at"),
            "resolved_at": repair.get("resolved_at"),
            "exhausted_at": repair.get("exhausted_at"),
            "attempts": [{key: attempt.get(key) for key in
                          ("author", "status", "started_at", "finished_at")}
                         for attempt in attempts],
        })
    return {
        "run_id": state.get("run_id"),
        "phase": state.get("phase"),
        "event_counts": counts,
        "worker_turns": len(runs),
        "worker_seconds": round(sum(float(row.get("seconds") or 0) for row in runs), 1),
        "cost_usd": round(sum(float(row.get("cost_usd") or 0) for row in runs), 6),
        "time_to_first_candidate_seconds": round(submitted - started, 3) if submitted and started else None,
        "candidate_review_seconds": round(accepted - submitted, 3) if accepted and submitted else None,
        "post_candidate_seconds": round(completed - submitted, 3) if completed and submitted else None,
        "by_phase": by_phase,
        "by_model": by_model,
        "by_task": by_task,
        "manifest_repairs": {
            "total": len(repair_records), "by_status": repair_counts,
            "attempts_by_status": repair_attempt_counts,
            "records": repair_timestamps,
        },
    }


def solve_metrics() -> dict:
    """Return compact run-scoped timing, worker, token, and cost telemetry."""
    if PROFILE in {"solving", "solution_review", "retrospective"}:
        return _informal_metrics()
    return read_metrics(FORUM_DIR, _root())


def _task_focus(state: dict, author: str, task_id: str = "") -> tuple[set[str], set[str]]:
    """Keep direct assignments distinct from their supporting prerequisites."""
    tasks = state.get("formal_tasks", {})
    assigned = task_id or os.getenv("UNITY_SOLVE_TASK_ID", "")
    if assigned and assigned not in tasks:
        raise ValueError(f"unknown task '{assigned}'")
    focus = {assigned} if assigned else {
        item["target"] for item in state.get("strategies", {}).values()
        if item.get("target") in tasks and item.get("status") in {"claimed", "paused"}
        and solve_state.participates(item, author)
        and solve_state.strategy_is_current(state, item)
    }
    return focus, _related_tasks(state, focus)


def _related_tasks(state: dict, task_ids: set[str]) -> set[str]:
    """Include the prerequisite closure without broadening a focused view."""
    tasks = {**state.get("retired_tasks", {}), **state.get("formal_tasks", {})}
    related = set(task_ids)
    while True:
        dependencies = {
            dependency for target in related
            for dependency in tasks.get(target, {}).get("dependencies", [])
            if dependency in tasks
        }
        if dependencies <= related:
            return related
        related |= dependencies


def _finding_view(state: dict, finding: dict) -> dict:
    """Annotate capture-context drift without modifying the durable evidence."""
    view = dict(finding)
    context = finding.get("code_context")
    if not isinstance(context, dict) or not context:
        return view  # Legacy findings have no observed code context to compare.
    source = solve_state.formal_source(state)
    task_id = context.get("task_id")
    task = state.get("formal_tasks", {}).get(task_id)
    current = {
        "run_id": state.get("run_id"), "source_candidate": source.get("candidate_id"),
        "source_sha256": source.get("sha256"), "task_id": finding.get("target") or "",
        "task_revision": task.get("revision") if task else None,
    }
    if ((task_id and not task)
            or any(value != current[key] for key, value in context.items() if key in current)):
        view["context_status"] = "potentially stale"
    return view


def _relevant_findings(state: dict, related: set[str]) -> list[dict]:
    findings = [
        _finding_view(state, item) for item in state.get("findings", {}).values()
        if item.get("status") == "active"
        and (not related or item.get("target") in related | {"", None})
    ]
    findings.sort(key=lambda item: (
        bool(item.get("code_artifacts")), item.get("target") in related,
        item.get("context_status") != "potentially stale", bool(item.get("declarations")),
        item.get("confidence") or 0, item.get("created_at") or 0,
    ), reverse=True)
    return findings


def _verified_dependency_outputs(state: dict, task_ids: set[str]) -> list[dict]:
    """Advertise only current controller-verified outputs, never local claims."""
    contract = state.get("formalization", {}).get("contract") or {}
    results = []
    for task_id in sorted(task_ids):
        task = state.get("formal_tasks", {}).get(task_id, {})
        verification = task.get("verification") or {}
        candidate_id = task.get("accepted_candidate")
        candidate = state.get("formal_candidates", {}).get(candidate_id, {})
        receipt = candidate.get("verification") or {}
        outputs = task.get("outputs") or ([
            {"declaration": task["lean_decl"], "file": task["lean_file"]}
        ] if task.get("lean_decl") and task.get("lean_file") else [])
        if (task.get("status") != "complete" or verification.get("status") != "verified"
                or not candidate_id or verification.get("candidate_id") != candidate_id
                or candidate.get("status") != "merged" or candidate.get("task_id") != task_id
                or candidate.get("stage", "complete") != "complete"
                or not solve_state.candidate_is_current(state, candidate)
                or receipt.get("status") != "passed" or not outputs or not contract):
            continue
        if contract.get("version") == 3:
            targets = contract.get("targets", {})
            if (contract.get("bindings", {}).get(task_id) != outputs
                    or candidate.get("outputs") != outputs
                    or any(not targets.get(row["declaration"], {}).get("fingerprint")
                           or receipt.get("verified_targets", {}).get(row["declaration"])
                           != targets[row["declaration"]]["fingerprint"] for row in outputs)):
                continue
        elif receipt.get("contract_sha256") != contract.get("sha256"):
            revalidation = task.get("revalidation") or {}
            if (revalidation.get("status") != "passed"
                    or revalidation.get("contract_sha256") != contract.get("sha256")
                    or task_id not in revalidation.get("task_ids", [])):
                continue
        results.append({
            "task_id": task_id, "candidate_id": candidate_id, "outputs": outputs,
            "main_sha": candidate.get("main_sha"),
            "verification_artifact": receipt.get("artifact_id"),
            "build_artifact": (candidate.get("build") or {}).get("artifact_id"),
        })
    return results


def _spec(state: dict) -> dict:
    formal = state.get("formalization", {})
    return formal.get("spec") or (formal.get("contract") or {}).get("spec") or {}


def _detail(payload: dict, source: str) -> str:
    compacted = artifacts.compact_text(
        _artifacts_dir(), json.dumps(payload, sort_keys=True),
        kind="solve_detail", producer="Unity", source=source,
    )
    return artifacts.format_compacted(compacted)


def _requirement_spec(state: dict, requirements: list[dict]) -> dict:
    """Resolve only the immutable citations/prerequisites needed by these rows."""
    spec = _spec(state)
    requirement_ids = {item["id"] for item in requirements}
    task_ids = {target for item in requirements for target in item.get("tasks", [])}
    arguments = [item for item in spec.get("arguments", [])
                 if item.get("requirement_id") in requirement_ids]
    prerequisite_ids = {key for item in arguments for key in item.get("prerequisites", [])}
    prerequisites = [item for item in spec.get("prerequisites", [])
                     if item.get("id") in prerequisite_ids
                     or task_ids.intersection(item.get("needed_by", []))]
    anchor_ids = {key for item in requirements + arguments + prerequisites
                  for key in item.get("anchor_ids", [])}
    repair_ids = {key for item in arguments for key in item.get("repair_ids", [])}
    return {
        "anchors": [item for item in spec.get("anchors", []) if item["id"] in anchor_ids],
        "arguments": arguments, "prerequisites": prerequisites,
        "source_repairs": [item for key, item in state.get("source_repairs", {}).items()
                           if key in repair_ids],
    }


def verification_blockers(state: dict, task_id: str = "") -> list[dict]:
    """Last checked candidate failures, never future global completion conditions."""
    formal = state.get("formalization", {})
    contract = formal.get("contract") or {}
    if contract.get("version") != 3:
        return []
    rows = []
    # Keep exact rejection evidence only for the current task/contract/main.
    # Its repair can concern another task, but it is not a new dependency edge.
    latest = {}
    for candidate in state.get("formal_candidates", {}).values():
        if not solve_state.candidate_is_current(state, candidate):
            continue
        target = candidate.get("task_id")
        if task_id and target != task_id:
            continue
        if candidate.get("updated_at", 0) >= latest.get(target, {}).get("updated_at", 0):
            latest[target] = candidate
    for candidate in latest.values():
        context = candidate.get("failure_context") or {}
        if (candidate.get("status") != "failed"
                or context.get("main_sha") != formal.get("main_sha")
                or context.get("contract_sha256") != contract.get("sha256")):
            continue
        rows.extend({**row, "candidate_id": candidate["candidate_id"]}
                    for row in candidate.get("blockers", []))
    seen, result = set(), []
    for row in rows:
        identity = (row.get("code"), row.get("prerequisite_id"),
                    tuple(row.get("task_ids", [])), row.get("message"))
        if identity not in seen:
            seen.add(identity)
            result.append(row)
    return result


def global_completion_requirements(state: dict) -> list[dict]:
    contract = state.get("formalization", {}).get("contract") or {}
    if contract.get("version") != 3:
        return []
    completed = {key for key, task in state.get("formal_tasks", {}).items()
                 if task.get("status") == "complete"}
    return [{**row, "scope": "global_completion"} for row in
            solve_contract.prerequisite_blockers(contract, completed=completed, final=True)]


def task_readiness(state: dict, task_id: str) -> dict:
    """Expose the actual graph without interpreting free-text obstacle reports."""
    task = state.get("formal_tasks", {}).get(task_id, {})
    def dependencies(kind):
        return [{"task_id": key,
                 "interface_available": solve_state.interface_available(state, key),
                 "proof_complete": state.get("formal_tasks", {}).get(key, {}).get("status") == "complete"}
                for key in task.get(kind, task.get("dependencies", []) if kind == "statement_dependencies" else [])]
    return {"runnable": solve_state.task_ready(state, task),
            "statement_dependencies": dependencies("statement_dependencies"),
            "proof_dependencies": dependencies("proof_dependencies")}


def solve_requirements(offset: int = 0, limit: int = 20) -> str:
    """Read the complete global coverage ledger in stable-ID-sorted pages.

    Continue with next_offset until null. Task-filtered views never establish
    full source coverage. Large pages are stored as exact readable artifacts.
    """
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be from 1 through 100")
    state = solve_state.load_state(FORUM_DIR)
    formal = state["formalization"]
    rows = sorted(formal.get("requirements", []), key=lambda item: item["id"])
    end = min(len(rows), offset + limit)
    spec = _spec(state)
    scope = spec.get("scope", {})
    scope_ids = set(scope.get("targets", [])) | set(scope.get("references", []))
    scope_ids.update(key for item in scope.get("excluded", []) for key in item.get("anchor_ids", []))
    return _detail({
        "run_id": state.get("run_id"), "revision": state.get("revision"),
        "formalization_revision": formal.get("revision"),
        "contract_sha256": (formal.get("contract") or {}).get("sha256"),
        "scope": scope if offset == 0 else None,
        "scope_anchors": [item for item in spec.get("anchors", []) if item["id"] in scope_ids]
                         if offset == 0 else [],
        "scope_details_at_offset": 0,
        "total": len(rows), "offset": offset,
        "next_offset": end if end < len(rows) else None,
        "requirements": rows[offset:end],
        "remaining_global_requirements": global_completion_requirements(state) if offset == 0 else [],
        **_requirement_spec(state, rows[offset:end]),
    }, "requirements")


def solve_task(task_id: str) -> str:
    """Read one task's exact requirements, source citations and current evidence."""
    state = solve_state.load_state(FORUM_DIR)
    tasks = state.get("formal_tasks", {})
    task = tasks.get(task_id) or state.get("retired_tasks", {}).get(task_id)
    if task is None:
        raise ValueError(f"unknown task '{task_id}'")
    requirements = [
        item for item in state["formalization"].get("requirements", [])
        if task_id in item["tasks"]
    ]
    source_ids = {
        ref for item in requirements for ref in item.get("source_components", [])
    } | set(task.get("source_components", []))
    spec = _requirement_spec(state, requirements)
    source_ids.update(item["source_ref"] for item in spec["anchors"])
    related = _related_tasks(state, {task_id})
    return _detail({
        "run_id": state.get("run_id"), "revision": state.get("revision"),
        "contract_sha256": (state["formalization"].get("contract") or {}).get("sha256"),
        "task": task, "assignment": solve_state.assignment_view(state, task_id),
        "requirements": requirements,
        "verification_blockers": verification_blockers(state, task_id),
        "submission_blockers": solve_state.submission_blockers(state, task_id)
            if task_id in tasks else [],
        "readiness": task_readiness(state, task_id),
        "remaining_global_requirements": global_completion_requirements(state),
        "representation_review": solve_representation.current_representation_review(state, task_id),
        "manifest_repairs": [row for row in solve_state.current_manifest_repairs(state)
                             if row.get("task_id") == task_id],
        "file_reservations": {path: row for path, row in solve_files.reservations(state).items()
                              if task_id in {row["owner_task"], *row["shared_with"]}},
        "source_refs": [
            ref for ref in (solve_state.formal_source(state)).get("source_refs", [])
            if ref["ref_id"] in source_ids
        ],
        **spec,
        "dependencies": [tasks.get(dep, state.get("retired_tasks", {}).get(dep, {"task_id": dep}))
                         for dep in task.get("dependencies", [])],
        "candidates": [
            item for item in state.get("formal_candidates", {}).values()
            if item.get("task_id") == task_id
        ],
        "yielded_attempts": [
            {"author": author, **records[task_id]}
            for author, records in state.get("task_yields", {}).items()
            if task_id in records
        ],
        "checkpoints": [
            record for records in state.get("worktree_checkpoints", {}).values()
            if (record := records.get(task_id))
        ],
        "source_issues": [
            item for item in state.get("source_issues", {}).values()
            if not item.get("task_ids") or task_id in item["task_ids"]
        ],
        "findings": _relevant_findings(state, related),
        "verified_dependency_outputs": _verified_dependency_outputs(state, related - {task_id}),
    }, task_id)


def _formal_brief(author: str, task_id: str = "") -> str:
    """Return bounded task-focused state; global review uses the paged ledger."""
    author = _author(author)
    state = solve_state.load_state(FORUM_DIR)
    formal = state["formalization"]
    source = solve_state.formal_source(state)
    focus, related = _task_focus(state, author, task_id)
    review_phase = PROFILE == "critic" or state.get("phase") == "critic"
    if review_phase:
        focus, related = set(), set()  # Critic coverage is never task-filtered.
    tasks = state.get("formal_tasks", {})
    issues = [item for item in state.get("source_issues", {}).values()
              if item.get("status") != "resolved"]
    requests = list(state.get("replan_requests", {}).values())
    queued = [item for item in requests if item.get("status") == "queued"]
    lines = [
        f"SOLVE RUN {state.get('run_id') or 'uninitialized'}",
        f"Phase: {state.get('phase', 'chunking')}; state revision: {state.get('revision', 0)}",
        f"Problem SHA-256: {state.get('problem_sha256') or 'unavailable'}",
        f"Accepted paper snapshot: {source.get('candidate_id')}; SHA-256: {source.get('sha256')}",
        f"Solution gate: {state.get('solution', {}).get('status')} "
        f"(revision {state.get('solution', {}).get('revision')}); original problem: .unity/UNITY.md",
        f"Formalization gate: {formal.get('status')} (revision {formal.get('revision')})",
        f"Global source issues not resolved: {len(issues)}; "
        f"pending replan requests: {len(queued)}",
    ]
    focus_id = next(iter(focus)) if len(focus) == 1 else ""
    blockers = verification_blockers(state, focus_id)
    preflight = solve_state.submission_blockers(state, focus_id) if focus_id else []
    blocker_lines = []
    if blockers:
        blocker_lines.extend(["", f"LAST CHECKED CANDIDATE REJECTIONS ({len(blockers)}; showing up to 6)",
                      "These apply to the identified candidate, not every task. "
                      "A provider named in a rejection is not an additional dependency for unrelated work."])
        for row in blockers[:6]:
            blocker_lines.append(f"- {row.get('code')}: {row.get('prerequisite_id') or ''} "
                         f"tasks={','.join(row.get('task_ids', []))}: {row.get('message', '')[:240]}")
            blocker_lines.append(f"  Next: {row.get('required_action', '')[:240]}")
        blocker_lines.append("Exact rejection and source-accounting records: solve_task / solve_requirements.")
    if preflight:
        blocker_lines.extend(["", "APPLICABLE FINAL-SUBMISSION PREFLIGHT",
                              "These conditions would block this task's complete submission, not independent proof research."])
        for row in preflight[:6]:
            blocker_lines.append(f"- {row.get('code')}: {row.get('prerequisite_id')}: {row.get('message', '')[:240]}")
            blocker_lines.append(f"  Next: {row.get('required_action', '')[:240]}")
    if focus_id:
        readiness = task_readiness(state, focus_id)
        blocker_lines.extend(["", "DECLARED TASK DEPENDENCIES",
            "Statement dependencies: " + (", ".join(row["task_id"] for row in readiness["statement_dependencies"]) or "none"),
            "Proof dependencies: " + (", ".join(row["task_id"] for row in readiness["proof_dependencies"]) or "none"),
            "An unfinished global obligation or agent-reported obstacle does not add a dependency."])
    if not review_phase:
        lines.extend(blocker_lines)
    snapshot = formal.get("review_snapshot")
    snapshot_lines = ([
        f"Machine snapshot: {snapshot.get('snapshot_id')} "
        f"({'passed' if snapshot.get('passed') else 'failed'}); main {snapshot.get('main_sha')}",
        f"Machine evidence artifact: {snapshot.get('artifact_id')}; "
        "scaffold axiom lists are historical, not current verification.",
    ] if snapshot else [])
    if review_phase and snapshot:
        lines.extend(["", "CURRENT MACHINE SNAPSHOT", *snapshot_lines])
        snapshot_tasks = snapshot.get("task_statuses", {})
        accepted = snapshot.get("accepted_candidates", {})
        lines.append(f"SNAPSHOT TASK EVIDENCE (showing {min(10, len(snapshot_tasks))} of {len(snapshot_tasks)})")
        for target, status in list(snapshot_tasks.items())[:10]:
            lines.append(f"- {target}: status={status}; accepted_candidate={accepted.get(target) or 'none'}")
        snapshot_issues = snapshot.get("issues", [])
        lines.append(f"SNAPSHOT ISSUES (showing {min(5, len(snapshot_issues))} of {len(snapshot_issues)})")
        for issue in snapshot_issues[:5]:
            lines.append(f"- {issue[:240]}")
        lines.append("Global snapshot failure does not mean every task failed. "
                     "Read the snapshot artifact and solve_task(task_id) for exact detail.")
    if review_phase:
        lines.extend(blocker_lines)  # Never truncate the critic's current snapshot behind repair prose.
    repairs = [row for row in solve_state.current_manifest_repairs(state)
               if not related or row.get("task_id") in related]
    if repairs:
        lines.extend(["", "CURRENT MANIFEST/REPRESENTATION REPAIR REQUESTS — not acceptance evidence"])
        for row in repairs[:6]:
            lines.append(f"- {row['task_id']}: {row['kind']}; {row['status']}; "
                         f"repair {row['repair_id']}; attempts={len(row.get('attempts', []))}")
            for blocker in row.get("blockers", [])[:2]:
                lines.append(f"  {blocker.get('code')}: {blocker.get('message', '')[:240]}")
        lines.append("Read solve_task(task_id).manifest_repairs for exact scope and blockers; "
                     "repair completion does not approve mathematical correspondence.")
    reviews = [(key, solve_representation.current_representation_review(state, key))
               for key in tasks if not related or key in related]
    reviews = [(key, row) for key, row in reviews if row]
    if reviews:
        lines.extend(["", "CURRENT REPRESENTATION REVIEWS — source correspondence, not proof completion"])
        for key, row in reviews[:6]:
            lines.append(f"- {key}: {row['status']}; input {row['input_sha256'][:12]}; "
                         f"solve_task('{key}') for exact evidence")
    if focus:
        lines.extend(["", "YOUR ASSIGNED/CLAIMED TASKS"])
        for target in sorted(focus):
            task = tasks[target]
            lines.append(f"- {target} [{task.get('status')}]: {task.get('title') or task.get('lean_decl')} "
                         f"{task.get('description', '')[:220]}")
        lines.append("Exact requirements, source citations and evidence: solve_task(task_id).")
        owned_files = [(path, row) for path, row in solve_files.reservations(state).items()
                       if focus.intersection({row["owner_task"], *row["shared_with"]})]
        if owned_files:
            lines.append("Reserved files: " + "; ".join(
                f"{path} (owner {row['owner_task']})" for path, row in owned_files[:6]))
    if review_phase and (last_round := formal.get("last_round")):
        lines.extend(["", "LAST FORMALIZATION ATTEMPT"])
        for owner, reason in list(last_round.get("blocked_launches", {}).items())[:5]:
            lines.append(f"- {owner}: {reason[:240]}")
    checkpoints = [record
                   for owner, records in state.get("worktree_checkpoints", {}).items()
                   for target, record in records.items()
                   if (review_phase or owner == solve_state.author_key(author))
                   and (not related or target in related)]
    if checkpoints:
        lines.extend(["", "SAVED PRIVATE WORK (not accepted candidates)"])
        for record in checkpoints[-5:]:
            lines.append(f"- {record['author']} / {record['task_id']} revision {record['task_revision']}: "
                         f"{record['ref']}; artifact {record['manifest_artifact']}")
    candidates = [
        item for item in state.get("formal_candidates", {}).values()
        if solve_state.candidate_is_current(state, item)
        and (not related or item.get("task_id") in related)
    ]
    candidates.sort(key=lambda item: (
        item.get("status") not in {"submitted", "merging"},
        item.get("status") != "failed", -(item.get("created_at") or 0),
    ))
    if candidates:
        lines.extend(["", "CURRENT FORMALIZATION CANDIDATES"])
        for item in candidates[:6]:
            lines.append(f"- {item['candidate_id']} [{item['status']}] stage={item.get('stage', 'complete')} task={item['task_id']} "
                         f"by {item['author']} at {item['commit_sha'][:12]}")
            if item.get("error"):
                lines.append(f"  failure: {item['error'][:240]}")
            for record in (item.get("build") or {}, item.get("verification") or {}):
                if record.get("artifact_id"):
                    lines.append(f"  artifact {record['artifact_id']}")
    if issues:
        lines.extend(["", "SOURCE ISSUES — RESOLVE WITH EVIDENCE, NEVER SILENTLY REWRITE"])
        relevant = sorted(issues, key=lambda item: bool(
            related and item.get("task_ids") and not related.intersection(item["task_ids"])
        ))
        for item in relevant[:5]:
            lines.append(f"- {item['issue_id']} [{item['status']}]: {item.get('description', '')[:220]}")
            for repair_id in item.get("repair_ids", [])[-2:]:
                repair = state.get("source_repairs", {}).get(repair_id, {})
                lines.append(f"  proposal {repair_id} by {repair.get('author')}: "
                             f"{repair.get('explanation', '')[:180]}; artifact {repair.get('artifact_id')}")
        lines.append("Reports require diagnosis: false alarm, encoding error, genuine source defect, or uncertain. "
                     "A proposed repair alone does not queue a replan or amend the accepted paper. "
                     "Use propose_source_fix for independent solution review, or reopen_solving for missing mathematics.")
    for item in queued[:3]:
        lines.append(f"Queued replan: {item.get('reason', '')[:200]}")
    verified_dependencies = _verified_dependency_outputs(state, related - focus)
    if verified_dependencies:
        lines.extend(["", "MACHINE-VERIFIED DEPENDENCY OUTPUTS — not source-faithfulness approval"])
        for record in verified_dependencies[:6]:
            lines.append(f"- {record['task_id']}: current merged candidate {record['candidate_id']}; "
                         f"solve_task('{record['task_id']}')")
            for output in record["outputs"][:6]:
                lines.append(f"  {output['declaration']} — {output['file']}")
            if len(record["outputs"]) > 6:
                lines.append(f"  {len(record['outputs']) - 6} more outputs via solve_task.")
            if record.get("verification_artifact"):
                lines.append(f"  verification artifact {record['verification_artifact']}")
    findings = _relevant_findings(state, related)
    if findings:
        lines.extend(["", "LIVE FINDINGS — agent-reported, not Unity acceptance"])
        for item in findings[:6]:
            lines.append(f"- {item['finding_id']} by {item.get('author') or 'unknown'} "
                         f"task={item.get('target') or 'global'} "
                         f"(agent-reported confidence {item.get('confidence')}/100): "
                         f"{str(item.get('title') or '')[:160]}; read_finding('{item['finding_id']}')")
            if item.get("context_status"):
                lines.append(f"  capture context: {item['context_status']}; retained bytes are historical evidence.")
            if item.get("declarations"):
                declarations = item["declarations"]
                lines.append(f"  agent-reported declarations: {', '.join(declarations[:6])}"
                             + (f" (+{len(declarations) - 6} more)" if len(declarations) > 6 else ""))
            code = item.get("code_artifacts") or []
            for attachment in code[:3]:
                lines.append(f"  code {attachment['path']}: artifact {attachment['artifact_id']}")
            if len(code) > 3:
                lines.append(f"  {len(code) - 3} more code files via read_finding.")
            lines.append(f"  {item.get('content', '')[:220]}")
            if item.get("evidence"):
                lines.append(f"  agent-reported evidence: {item['evidence'][:180]}")
        lines.append("Read attached bytes with artifact_read; consume content and follow next_offset to null.")
    obstacles = [
        item for item in state.get("obstacles", {}).values()
        if item.get("status") == "open" and (not related or item.get("target") in related | {"", None})
    ]
    if obstacles:
        lines.extend(["", "AGENT-REPORTED OBSTACLES — not additional scheduling constraints"])
        for item in obstacles[-5:]:
            lines.append(f"- {item['obstacle_id']} task={item.get('target') or 'global'}: "
                         f"{item.get('goal_state', '')[:250]}")
    global_requirements = global_completion_requirements(state)
    if global_requirements:
        lines.extend(["", "REMAINING GLOBAL COMPLETION REQUIREMENTS — not local proof blockers",
                      "Independent work may continue. Missing helper proofs are work to develop or assign."])
        for row in global_requirements[:4]:
            lines.append(f"- {row.get('prerequisite_id')}: {row.get('message', '')[:200]}")
        lines.append("All source-accounting details: solve_requirements(offset=0).")
    if snapshot and not review_phase:
        lines.extend(snapshot_lines)
    requirements = formal.get("requirements", [])
    lines.extend([
        "", f"GLOBAL COVERAGE LEDGER: {len(requirements)} requirements",
        "Read solve_requirements(offset=0) and follow next_offset to null for ALL requirements. "
        "A focused task view or this bounded brief is not complete coverage.",
    ])
    selected_requirements = [
        item for item in requirements if not related or related.intersection(item["tasks"])
    ]
    if selected_requirements:
        lines.append("RELEVANT REQUIREMENT SUMMARIES" if related else "REQUIREMENT PREVIEW")
        for item in selected_requirements[:8]:
            lines.append(f"- {item['id']} → tasks {', '.join(item['tasks'])}: "
                         f"{item['statement'][:180]}")
        if len(selected_requirements) > 8:
            lines.append(f"- {len(selected_requirements) - 8} additional entries available through detail tools.")
    refs = source.get("source_refs") or []
    relevant_source_ids = {
        ref for item in selected_requirements for ref in item.get("source_components", [])
    }
    relevant_refs = [ref for ref in refs if not related or ref["ref_id"] in relevant_source_ids]
    lines.extend(["", "ACCEPTED PAPER AND SOURCE REFERENCES"])
    for ref in relevant_refs[:6]:
        lines.append(f"- {ref.get('ref_id')}: {str(ref.get('path', ''))[:140]} "
                     f"artifact {ref.get('artifact_id')} SHA-256 {ref.get('sha256')}")
    if len(relevant_refs) > 6:
        lines.append(f"- {len(relevant_refs) - 6} more references in the detail tools.")
    owned = [
        item for item in state.get("strategies", {}).values()
        if item.get("status") in {"claimed", "paused"}
        and solve_state.strategy_is_current(state, item)
        and solve_state.participates(item, author)
    ]
    if owned:
        lines.extend(["", "YOUR CLAIMED/ASSISTED STRATEGIES"])
        for item in owned[:6]:
            lines.append(f"- {item['strategy_id']} [{item['status']}] task={item.get('target')}: "
                         f"{item.get('description', '')[:200]}")
    yielded = [
        (owner, target, record)
        for owner, records in state.get("task_yields", {}).items()
        for target, record in records.items()
        if tasks.get(target, {}).get("status") == "pending"
        and (review_phase or owner == solve_state.author_key(author) or target in related)
    ]
    if yielded:
        yielded.sort(key=lambda row: (row[0] != solve_state.author_key(author), row[1], row[0]))
        lines.extend(["", "YIELDED ATTEMPTS (agent-local; independent approaches may continue)"])
        for owner, target, record in yielded[:8]:
            availability = "ready to reconsider" if solve_state.task_available_to(state, owner, target) else "deferred"
            lines.append(f"- {owner} / {target} [{availability}]: {record.get('reason', '')[:250]}; "
                         f"waiting for={','.join(record.get('waiting_for', [])) or 'new relevant work'}")
    visible_tasks = [tasks[target] for target in sorted(related) if target in tasks] if related else list(tasks.values())
    if visible_tasks:
        lines.extend(["", "RELEVANT TASK STATUS" if related else "TASK PREVIEW"])
        for task in visible_tasks[:10]:
            assignment = solve_state.assignment_view(state, task['task_id'])
            lines.append(f"- {task['task_id']} [{task['status']}]: {task.get('title') or task.get('lean_decl')}; "
                         f"representation={task.get('representation', {}).get('status', 'legacy')}, "
                         f"verification={task.get('verification', {}).get('status', 'pending')}, "
                         f"faithfulness={task.get('faithfulness', {}).get('status', 'unreviewed')}; "
                         f"owners={','.join(assignment['owners'])}; "
                         f"statement deps={','.join(task.get('statement_dependencies', []))}; "
                         f"proof deps={','.join(task.get('proof_dependencies', task.get('dependencies', [])))}")
    questions = [
        item for item in state.get("questions", {}).values()
        if item.get("status") == "open" and (
            not item.get("to") or solve_state.author_key(item.get("to"))
            == solve_state.author_key(author)
        ) and (not related or item.get("target") in related | {"", None})
    ]
    if questions:
        lines.extend(["", "OPEN QUESTIONS"])
        for item in questions[-5:]:
            lines.append(f"- {item['question_id']}: {item.get('body', '')[:250]}")
    attempts = [item for item in state.get("chunking_attempts", [])
                if item.get("candidate_id") == source.get("candidate_id")]
    if attempts and not related:
        lines.extend(["", "RECENT CHUNKING ATTEMPTS"])
        for item in attempts[-3:]:
            lines.append(f"- {item.get('author')} attempt {item.get('attempt')} "
                         f"[{item.get('status')}]: {item.get('reason', '')[:250]}")
    verdicts = state.get("critic_verdicts", [])
    if verdicts:
        verdict = verdicts[-1]
        if review_phase:
            lines.extend(["", "PRIOR CRITIC FEEDBACK — historical, not current task verification",
                          f"- snapshot={verdict.get('snapshot_id')}; main={verdict.get('main_sha')}"])
        else:
            lines.extend(["", "LATEST FORMALIZATION VERDICT"])
        lines.append(f"- {verdict.get('verdict')} by {verdict.get('author')}: "
                     f"{verdict.get('summary', '')[:350]}")
    if state.get("final_report"):
        lines.append(f"Run report: artifact {state['final_report'].get('artifact_id')}")
    text = "\n".join(lines)
    try:
        limit = max(2_000, min(32_000, int(os.getenv("UNITY_SOLVE_BRIEF_CHARS", "12000"))))
    except ValueError:
        limit = 12_000
    suffix = "\n...[brief truncated] Use solve_task or solve_requirements."
    return text if len(text) <= limit else text[:limit - len(suffix)].rstrip() + suffix



def forum_post(thread_id: str, author: str, content: str, reply_to: list[str] | None = None) -> dict:
    """Post free-form discussion; this does not reserve work or submit results."""
    author = _author(author)
    return discussion.forum_post(_ensure_thread(thread_id), author, content, reply_to)


def forum_read(thread_id: str, sort: str = "hot") -> dict:
    """Read one raw discussion thread when the compact brief is insufficient."""
    return discussion.forum_read(_ensure_thread(thread_id), sort)


def artifact_info(artifact_id: str) -> dict:
    """Return immutable artifact metadata without loading its content."""
    return artifacts.artifact_info(_artifacts_dir(), artifact_id)


def artifact_read(artifact_id: str, offset: int = 0, limit: int = 12000) -> dict:
    """Read one bounded page of an immutable artifact."""
    page = artifacts.read_artifact(_artifacts_dir(), artifact_id, offset=offset, limit=limit)
    if page["kind"] != "solve_finding_code":
        return page
    # Shared byte previews may split a UTF-8 character. Code must roundtrip
    # exactly; retain byte offsets, but return only whole characters.
    payload = artifacts.artifact_bytes(_artifacts_dir(), artifact_id)
    if hashlib.sha256(payload).hexdigest() != page["sha256"]:
        raise ValueError("Finding code artifact changed; refusing retrieval")
    start, end = page["offset"], page["offset"] + page["returned_bytes"]
    if start < len(payload) and payload[start] & 0xC0 == 0x80:
        raise ValueError("offset must be a UTF-8 boundary; follow next_offset")
    while end > start and end < len(payload) and payload[end] & 0xC0 == 0x80:
        end -= 1
    if end == start and start < len(payload):
        # A limit smaller than one character returns that character (<=4 bytes).
        end += 1
        while end < len(payload) and payload[end] & 0xC0 == 0x80:
            end += 1
    return {**page, "content": payload[start:end].decode("utf-8"),
            "returned_bytes": end - start, "next_offset": end if end < len(payload) else None}


def read_finding(finding_id: str) -> str:
    """Read an exact finding and its immutable code references, including superseded work."""
    state = solve_state.load_state(FORUM_DIR)
    finding = state.get("findings", {}).get(finding_id)
    if finding is None:
        raise ValueError(f"unknown finding '{finding_id}'")
    return _detail({"finding": _finding_view(state, finding)}, finding_id)


def register_strategy(
    author: str,
    description: str,
    target: str = "",
    strategy_family: str = "",
    central_claim: str = "",
) -> dict:
    """Register one distinct strategy for the current solving or formalization target."""
    author = _author(author)
    result = solve_state.register_strategy(
        FORUM_DIR, author, description, target=target, family=strategy_family,
        central_claim=central_claim,
    )
    if result["status"] != "duplicate":
        item = result["strategy"]
        _mirror(author, f"STRATEGY {item['strategy_id']}", description, target)
    return result


def claim_strategy(strategy_id: str, author: str) -> dict:
    """Atomically reserve a registered strategy."""
    return solve_state.claim_strategy(FORUM_DIR, strategy_id, _author(author))


def reserve_files(author: str, task_id: str, paths: list[str],
                  share_with: list[str] | None = None) -> dict:
    """Reserve task files; only the owning task can explicitly grant other tasks sharing."""
    from .. import solve_files

    author = _author(author)
    paths = solve_files.normalize_paths(paths)
    tree = worktree.agent_worktree(_root(), author).resolve()
    for path in paths:
        if not (tree / path).resolve().is_relative_to(tree):
            raise ValueError("reserved files must stay inside the agent worktree")
    return solve_files.reserve_files(FORUM_DIR, author, task_id, paths, share_with=share_with)


def assist_strategy(strategy_id: str, author: str, contribution: str = "") -> dict:
    """Join a claimed strategy with a distinct supporting contribution."""
    return solve_state.assist_strategy(FORUM_DIR, strategy_id, _author(author), contribution)


def unclaim_strategy(strategy_id: str, author: str, reason: str = "") -> dict:
    """Release an owned strategy that may remain viable."""
    return solve_state.release_strategy(
        FORUM_DIR, strategy_id, _author(author), reason=reason, incorrect=False,
    )


def yield_task(author: str, task_id: str, reason: str,
               waiting_for: list[str] | None = None) -> dict:
    """End this agent's attempt, preserving work and other agents' approaches."""
    return solve_state.yield_task(
        FORUM_DIR, _author(author), task_id, reason, waiting_for=waiting_for,
    )


def mark_strategy_incorrect(strategy_id: str, author: str, reason: str) -> dict:
    """Close an owned strategy after establishing why it cannot work."""
    return solve_state.release_strategy(
        FORUM_DIR, strategy_id, _author(author), reason=reason, incorrect=True,
    )


MAX_FINDING_FILES = 8
MAX_FINDING_FILE_BYTES = 256 * 1024
MAX_FINDING_TOTAL_BYTES = 1024 * 1024


def _read_finding_file(tree_fd: int, relative: Path) -> bytes:
    """Read a stable bounded source file, without following any path-component symlink."""
    directory = os.dup(tree_fd)
    fd = None
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("finding code must be a regular file, not a hardlink or special file")
        if before.st_size > MAX_FINDING_FILE_BYTES:
            raise ValueError(f"finding code exceeds {MAX_FINDING_FILE_BYTES} bytes per file")
        chunks, size = [], 0
        while size <= MAX_FINDING_FILE_BYTES:
            chunk = os.read(fd, min(65536, MAX_FINDING_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        current = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns, row.st_nlink)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError("finding code changed during capture; finish editing and retry")
        if size > MAX_FINDING_FILE_BYTES:
            raise ValueError(f"finding code exceeds {MAX_FINDING_FILE_BYTES} bytes per file")
        return b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"cannot snapshot {relative}: use an existing private file without symlinks") from exc
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _snapshot_finding_files(author: str, files: list[str], target: str) -> tuple[list[dict], dict]:
    """Save explicitly shared source; never create/commit/reset an agent's worktree."""
    if not isinstance(files, list) or len(files) > MAX_FINDING_FILES:
        raise ValueError(f"files must be a list of at most {MAX_FINDING_FILES} private Lean paths")
    names = []
    for name in files:
        if not isinstance(name, str):
            raise ValueError("finding file paths must be strings")
        path = Path(name)
        if (path.is_absolute() or ".." in path.parts or path.suffix != ".lean"
                or any(part in {".git", ".unity", ".lake", ".worktrees"} for part in path.parts)):
            raise ValueError("finding files must be relative .lean paths outside runtime/build directories")
        if path not in names:
            names.append(path)
    state = solve_state.load_state(FORUM_DIR)
    if PROFILE != "formalizing" or state.get("phase") != "formalizing":
        raise ValueError("private code attachments are only available during formalizing")
    if target and target not in state.get("formal_tasks", {}):
        raise ValueError(f"unknown finding task '{target}'")
    source = solve_state.formal_source(state)
    context = {"run_id": state.get("run_id"), "source_candidate": source.get("candidate_id"),
               "source_sha256": source.get("sha256"), "task_id": target,
               "task_revision": state.get("formal_tasks", {}).get(target, {}).get("revision")}
    tree = worktree.agent_worktree(_root(), author)
    expected = f"worktree {tree}\0"
    registered = _git(_root(), "worktree", "list", "--porcelain", "-z").stdout
    if not any(record.startswith(expected)
               and f"branch refs/heads/{worktree.agent_branch(author)}" in record.split("\0")
               for record in registered.split("\0\0")):
        raise ValueError(f"no registered private worktree for '{author}'")
    # Anchor traversal at the trusted main root. Even swapping a parent for a
    # symlink cannot redirect a file read to another worker or outside the repo.
    directory = os.open(_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    contents, total = [], 0
    try:
        for part in (".worktrees", tree.name):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        for path in names:
            data = _read_finding_file(directory, path)
            total += len(data)
            if total > MAX_FINDING_TOTAL_BYTES:
                raise ValueError(f"finding files exceed {MAX_FINDING_TOTAL_BYTES} combined bytes")
            contents.append((path.as_posix(), data.decode("utf-8")))
    except (OSError, UnicodeError) as exc:
        raise ValueError("finding files must be private UTF-8 Lean source without symlinks") from exc
    finally:
        os.close(directory)
    # Validate every file before storing anything. No source bytes go in state.
    records = []
    for name, content in contents:
        record = artifacts.store_text(_artifacts_dir(), content, kind="solve_finding_code",
                                      producer=author, source=name, metadata=context)
        records.append({"path": name, **{key: record[key] for key in ("artifact_id", "sha256", "bytes")}})
    return records, context


def publish_finding(
    author: str,
    kind: str,
    title: str,
    content: str,
    confidence: Annotated[int, Field(
        ge=0, le=100, strict=True,
        description="Integer confidence from 0 to 100; 95 means 95%, not 0.95.",
    )],
    target: str = "",
    strategy_id: str = "",
    evidence: str = "",
    supersedes: str = "",
    declarations: list[str] | None = None,
    files: list[str] | None = None,
) -> dict:
    """Publish live knowledge; optionally snapshot named private Lean files during formalizing.

    Declarations and checks are agent-reported, not verified by publication.
    Include any private imports needed for reuse; files are never auto-imported or merged.
    Attach up to eight files, 256 KiB per file and 1 MiB combined.
    """
    author = _author(author)
    if files is not None and not isinstance(files, list):
        raise ValueError("files must be a list of private Lean paths")
    if len(evidence) > 4000:
        record = artifacts.store_text(
            _artifacts_dir(), evidence, kind="solve_finding_evidence",
            producer=author, source=title,
        )
        evidence = f"artifact {record['artifact_id']} SHA-256 {record['sha256']}"
    with _finalization_lock(author) if files else nullcontext():
        code_artifacts, code_context = _snapshot_finding_files(author, files, target) if files else ([], None)
        result = solve_state.publish_finding(
            FORUM_DIR, author, kind, title, content, confidence,
            target=target, strategy_id=strategy_id, evidence=evidence, supersedes=supersedes,
            declarations=declarations, code_artifacts=code_artifacts, code_context=code_context,
        )
    _mirror(author, f"FINDING {result['finding_id']}: {title}", content, target)
    return result


def report_obstacle(
    author: str,
    goal_state: str,
    target: str = "",
    tried: str = "",
    hypothesis: str = "",
) -> dict:
    """Report a concrete blocker visible to every solve worker."""
    author = _author(author)
    result = solve_state.report_obstacle(
        FORUM_DIR, author, goal_state, target=target, tried=tried, hypothesis=hypothesis,
    )
    _mirror(author, f"OBSTACLE {result['obstacle_id']}", goal_state, target)
    return result


def ask_question(author: str, body: str, to: str = "", target: str = "") -> dict:
    """Ask a targeted or global solve question."""
    return solve_state.ask_question(FORUM_DIR, _author(author), body, to=to, target=target)


def answer_question(question_id: str, author: str, body: str) -> dict:
    """Answer an open solve question."""
    return solve_state.answer_question(FORUM_DIR, question_id, _author(author), body)


def emit_formalization_candidate(
    strategy_id: str,
    author: str,
    task_id: str,
    commit_sha: str,
    notes: str = "",
    supersedes: str = "",
    stage: Literal["representation", "complete"] = "complete",
    outputs: list[dict] | None = None,
    obsolete_files: list[dict] | None = None,
) -> dict:
    """Compatibility API for submitting an already-committed implementation."""
    author = _author(author)
    with _finalization_lock(author):
        return _submit_formal_commit(
            strategy_id, author, task_id, commit_sha,
            notes=notes, supersedes=supersedes, stage=stage, outputs=outputs,
            obsolete_files=obsolete_files,
        )


def finalize_formalization(
    strategy_id: str,
    author: str,
    task_id: str,
    changed_paths: list[str] | None = None,
    notes: str = "",
    supersedes: str = "",
    stage: Literal["representation", "complete"] = "complete",
    outputs: list[dict] | None = None,
    obsolete_files: list[dict] | None = None,
) -> dict:
    """Commit current worktree bytes and submit one immutable formal candidate.

    Unchanged complete work submits the existing commit for re-verification.
    An unchanged, already-adopted representation returns its existing acceptance.

    This is deliberately not a build assertion.  The solve controller applies
    the exact resulting commit to main and performs the sole authoritative full
    build and declaration review there.
    """
    author = _author(author)
    if stage not in {"representation", "complete"}:
        raise ValueError("candidate stage must be representation or complete")
    outputs = normalize_outputs(outputs) if outputs is not None else None
    with _finalization_lock(author):
        preflight = solve_state.preflight_formal_submission(
            FORUM_DIR, strategy_id, author, task_id, stage=stage, outputs=outputs,
        )
        if preflight["status"] != "ok":
            return preflight
        state = solve_state.load_state(FORUM_DIR)
        task = state.get("formal_tasks", {}).get(task_id)
        if state.get("phase") != "formalizing" or not task:
            raise ValueError("formalization task is unavailable")
        if task.get("status") != "pending":
            raise ValueError(f"formalization task is {task.get('status')}, not finalizable")
        if ((state['formalization'].get('contract') or {}).get('version') == 3
                and not (outputs or task.get('outputs'))):
            raise ValueError("a first candidate requires its declaration/file outputs")
        strategy = state.get("strategies", {}).get(strategy_id)
        if (
            not strategy
            or strategy.get("phase") != "formalizing"
            or strategy.get("target") != task_id
            or not solve_state.strategy_is_current(state, strategy)
            or strategy.get("status") != "claimed"
            or not solve_state.participates(strategy, author)
        ):
            raise ValueError("author must own or assist a strategy for this formal task")

        tree = worktree.agent_worktree(_root(), author).resolve()
        if not tree.is_dir():
            raise ValueError(f"no active worktree for agent '{author}'")

        selected: list[str] = []
        for raw in changed_paths or []:
            relative = Path(str(raw))
            if relative.is_absolute():
                try:
                    relative = relative.resolve().relative_to(tree)
                except ValueError as exc:
                    raise ValueError("changed paths must be inside the agent worktree") from exc
            resolved = (tree / relative).resolve()
            try:
                normalized = resolved.relative_to(tree).as_posix()
            except ValueError as exc:
                raise ValueError("changed paths must be inside the agent worktree") from exc
            if not normalized or normalized.split("/", 1)[0] in {
                ".git", ".unity", ".lake", ".worktrees",
            }:
                raise ValueError(f"runtime/build path cannot be finalized: {normalized}")
            selected.append(normalized)

        if selected:
            _git(tree, "add", "--", *selected)
        else:
            _git(tree, "add", "--all")

        staged = [
            item for item in _git(
                tree, "diff", "--cached", "--name-only", "-z"
            ).stdout.split("\0") if item
        ]
        blocked = [
            path for path in staged
            if path.split("/", 1)[0] in {".git", ".unity", ".lake", ".worktrees"}
        ]
        if blocked:
            _git(tree, "reset", check=False)
            raise ValueError("candidate includes runtime/build paths: " + ", ".join(blocked))

        committed = False
        if staged:
            expected_file = str(task.get("lean_file") or "").strip().lstrip("./")
            # The proof may already be committed; the staged repair can live in
            # another file. Check the complete candidate, not just this commit.
            base = _git(tree, "merge-base", "HEAD", worktree.main_commit(_root())).stdout.strip()
            candidate_paths = _git(
                tree, "diff", "--cached", "--name-only", "-z", base,
            ).stdout.split("\0")
            if ((state['formalization'].get('contract') or {}).get('version') != 3
                    and expected_file and expected_file not in candidate_paths):
                _git(tree, "reset", check=False)
                raise ValueError(
                    f"candidate does not change the target file '{expected_file}'"
                )
            commit = _git(
                tree,
                "-c", f"user.name=Unity ({author})",
                "-c", "user.email=unity@localhost",
                "commit", "-m", f"UNITY: solve candidate for {task_id}",
                check=False,
            )
            if commit.returncode:
                raise ValueError(commit.stderr.strip() or "could not commit formalization")
            committed = True

        head = _git(tree, "rev-parse", "HEAD").stdout.strip()
        result = _submit_formal_commit(
            strategy_id, author, task_id, head,
            notes=notes, supersedes=supersedes, stage=stage, outputs=outputs,
            obsolete_files=obsolete_files,
            submission_context=preflight["context"],
        )
        return {
            **result,
            "committed": committed,
            "changed_paths": staged,
            "commit_sha": head,
        }


def _current_formal_candidate(state: dict, candidate: dict) -> bool:
    return solve_state.candidate_is_current(state, candidate)


def has_pending_formal_candidate(state: dict, author: str = "") -> bool:
    """Whether current-revision candidate bytes must remain on an author's branch."""
    return any(
        _current_formal_candidate(state, candidate)
        and candidate.get("status") in {"submitted", "merging"}
        and (not author or solve_state.author_key(candidate.get("author"))
             == solve_state.author_key(author))
        for candidate in state.get("formal_candidates", {}).values()
    )


def unresolved_formal_tasks(state: dict, author: str) -> list[str]:
    """Current claimed/assisted work and queued candidate targets for an author."""
    targets = {
        strategy.get("target", "")
        for strategy in state.get("strategies", {}).values()
        if strategy.get("phase") == "formalizing"
        and solve_state.strategy_is_current(state, strategy)
        and strategy.get("status") in {"claimed", "paused"}
        and solve_state.participates(strategy, author)
    }
    targets.update(
        candidate.get("task_id", "")
        for candidate in state.get("formal_candidates", {}).values()
        if _current_formal_candidate(state, candidate)
        and candidate.get("status") in {"submitted", "merging"}
        and solve_state.author_key(candidate.get("author")) == solve_state.author_key(author)
    )
    return sorted(
        target for target in targets
        if target and state.get("formal_tasks", {}).get(target, {}).get("status")
        in {"pending", "candidate_pending"}
    )


def _sync_blocked(reason: str, error: str) -> dict:
    return {"ok": False, "blocked": True, "reason": reason, "error": error}


def _accepted_formal_main(state: dict) -> str:
    target = str(state.get("formalization", {}).get("main_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", target) or worktree.main_commit(_root()) != target:
        return ""
    return target


def ready_statement_prerequisites(state: dict, task_id: str) -> list[dict]:
    """Ready missing interfaces upstream of a blocked informal assignment only."""
    tasks = state.get("formal_tasks", {})
    task = tasks.get(task_id, {})
    if ((state.get("formalization", {}).get("contract") or {}).get("version") != 3
            or task.get("status") != "pending" or solve_state.task_ready(state, task)
            or solve_state.source_issues_blocking_task(state, task_id)):
        return []
    pending = list(task.get("statement_dependencies", []))
    seen, ready = {task_id}, []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        dependency = tasks.get(key, {})
        if solve_state.interface_available(state, dependency):
            continue
        if solve_state.task_ready(state, dependency):
            ready.append(dependency)
        else:
            pending.extend(dependency.get("statement_dependencies", []))
    return sorted(ready, key=lambda item: item["task_id"])


def _worktree_source(state: dict) -> dict:
    source = solve_state.formal_source(state)
    return {"source_candidate": source.get("candidate_id"), "source_sha256": source.get("sha256")}


def _checkpoint_manifest_path(author: str, task_id: str, run_id: str, source: dict) -> Path:
    key = solve_state.digest([run_id, solve_state.author_key(author), task_id,
                              source.get("source_candidate"), source.get("source_sha256")])
    return FORUM_DIR / "worktree-checkpoints" / f"{key}.json"


def _worktree_assignment_path(author: str, state: dict) -> Path:
    key = solve_state.digest([state.get("run_id", ""), solve_state.author_key(author)])
    return FORUM_DIR / "worktree-checkpoints" / f"{key}.assignment.json"


def _record_worktree_assignment(author: str, task_id: str, revision: int | None,
                               state: dict, *, pending: bool = False) -> dict:
    assignment = {"task_id": task_id, "task_revision": revision, "pending": pending,
                  **_worktree_source(state)}
    path = _worktree_assignment_path(author, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    artifacts._atomic_write(path, (json.dumps(assignment) + "\n").encode())
    if not pending:
        state.setdefault("worker_tasks", {})[solve_state.author_key(author)] = task_id
    return assignment


def _saved_task_checkpoint(author: str, task_id: str, state: dict) -> dict | None:
    identity = solve_state.author_key(author)
    checkpoint = state.get("worktree_checkpoints", {}).get(identity, {}).get(task_id)
    source = _worktree_source(state)
    path = _checkpoint_manifest_path(author, task_id, state.get("run_id", ""), source)
    if path.is_file():
        checkpoint = json.loads(path.read_text())
    if not checkpoint:
        return None
    if any(checkpoint.get(key) != value for key, value in source.items()):
        # An old paper's same-named task is not the current specification.
        # Legacy source-less checkpoints remain archived, never auto-restored.
        return None
    if (solve_state.author_key(checkpoint["author"]) != identity
            or checkpoint["task_id"] != task_id or checkpoint.get("run_id") != state.get("run_id", "")):
        raise ValueError("Checkpoint assignment identity changed; work preserved")
    manifest = json.loads(artifacts.artifact_bytes(_artifacts_dir(), checkpoint["manifest_artifact"]))
    if manifest != {key: value for key, value in checkpoint.items() if key != "manifest_artifact"}:
        raise ValueError("Checkpoint manifest changed; refusing restore")
    return checkpoint


def _checkpoint_task_worktree(tree: Path, author: str, task_id: str, task_revision: int | None,
                             *, source: dict | None = None) -> dict:
    """Checkpoint a stopped, locked tree before reassignment; never accept it.

    The immutable ref/artifacts and small recovery manifest precede any reset.
    Ignored private files are not added to Git or mixed into future candidates.
    """
    files = []
    ignored = _git(tree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                   "--", ".", ":(exclude).unity", ":(exclude).lake").stdout
    for relative in ignored.split("\0"):
        if not relative:
            continue
        path = tree / relative
        info = path.lstat()
        entry = {"path": relative, "mode": stat.S_IMODE(info.st_mode)}
        if stat.S_ISLNK(info.st_mode):
            entry["symlink"] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            entry["data"] = base64.b64encode(path.read_bytes()).decode("ascii")
        else:
            raise ValueError(f"Cannot checkpoint private special file {relative}; work preserved")
        files.append(entry)
    state = solve_state.load_state(FORUM_DIR)
    source = _worktree_source(state) if source is None else source
    archived = artifacts.store_text(
        _artifacts_dir(), json.dumps({"files": files}), kind="solve_private_files",
        producer=author, metadata={"task_id": task_id, "task_revision": task_revision},
    ) if files else None
    # Enumerate before staging: explicit ignored exclusion pathspecs make
    # `git add` fail, and forcing them would accidentally track shared state.
    names = _git(tree, "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                 "--", ".", ":(exclude).unity", ":(exclude).lake").stdout.split("\0")
    names = sorted({name for name in names if name})
    for start in range(0, len(names), 256):
        _git(tree, "--literal-pathspecs", "add", "-A", "--", *names[start:start + 256])
    staged = _git(tree, "diff", "--cached", "--quiet", check=False)
    if staged.returncode not in {0, 1}:
        raise ValueError("Cannot inspect checkpoint index; work preserved")
    if staged.returncode or _git(tree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        _git(tree, "commit", "-m", "UNITY: checkpoint private solve attempt")
    head = _git(tree, "rev-parse", "HEAD").stdout.strip()
    reference = f"refs/unity/solve-checkpoints/{uuid.uuid4().hex}"
    _git(tree, "update-ref", reference, head)
    run_id = state.get("run_id", "")
    checkpoint = {"author": author, "task_id": task_id, "task_revision": task_revision,
                  "run_id": run_id, "ref": reference, "commit_sha": head,
                  "source_candidate": source.get("source_candidate"),
                  "source_sha256": source.get("source_sha256"),
                  "ignored_artifact": archived["artifact_id"] if archived else None}
    manifest = artifacts.store_text(
        _artifacts_dir(), json.dumps(checkpoint), kind="solve_task_checkpoint",
        producer=author, metadata={"task_id": task_id, "run_id": run_id},
    )
    checkpoint["manifest_artifact"] = manifest["artifact_id"]
    path = _checkpoint_manifest_path(author, task_id, run_id, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    # This small durable pointer recovers the checkpoint if the process exits
    # after reset but before its Forum transaction is committed.
    artifacts._atomic_write(path, (json.dumps(checkpoint) + "\n").encode())
    return checkpoint


def _restore_private_files(tree: Path, artifact_id: str) -> list[str]:
    """Restore only private paths; never follow a parent symlink out of the tree."""
    record = artifacts.artifact_info(_artifacts_dir(), artifact_id)
    payload = artifacts.artifact_bytes(_artifacts_dir(), artifact_id)
    if hashlib.sha256(payload).hexdigest() != record["sha256"]:
        raise ValueError("Private checkpoint artifact changed; refusing restore")
    entries = json.loads(payload)["files"]
    conflicts = []
    for entry in entries:
        relative = Path(entry["path"])
        if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                or relative.parts[0] in {".git", ".unity", ".lake"}):
            raise ValueError("Private checkpoint contains an unsafe path")
        path = tree / relative
        if (not path.parent.resolve().is_relative_to(tree.resolve())
                or path.exists() or path.is_symlink()):
            conflicts.append(str(relative))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if "symlink" in entry:
            path.symlink_to(entry["symlink"])
        else:
            # Exclusive creation also protects newly appeared files.
            with path.open("xb") as handle:
                handle.write(base64.b64decode(entry["data"], validate=True))
            path.chmod(entry["mode"] & 0o777)
    return conflicts


def _restore_task_checkpoint_if_current(
    tree: Path, author: str, task_id: str, task_revision: int | None, state: dict, main_sha: str,
) -> dict:
    identity = solve_state.author_key(author)
    checkpoint = _saved_task_checkpoint(author, task_id, state)
    if not checkpoint:
        return {}
    state.setdefault("worktree_checkpoints", {}).setdefault(identity, {})[task_id] = checkpoint
    if (task_revision is None or checkpoint["task_revision"] != task_revision
            or state.get("formal_tasks", {}).get(task_id, {}).get("revision") != task_revision):
        return {"checkpoint": checkpoint, "sync_warning":
                "Saved checkpoint is not bound to the current task revision; reuse it selectively, not as the current specification."}
    if _git(tree, "rev-parse", checkpoint["ref"]).stdout.strip() != checkpoint["commit_sha"]:
        raise ValueError("Checkpoint ref changed; refusing restore")
    _git(tree, "reset", "--hard", checkpoint["commit_sha"])
    merged = _git(tree, "merge", "--no-edit", "--no-autostash", "--no-overwrite-ignore", main_sha, check=False)
    conflicts = (_restore_private_files(tree, checkpoint["ignored_artifact"])
                 if checkpoint.get("ignored_artifact") else [])
    result = {"checkpoint": checkpoint, "checkpoint_restored": True}
    if merged.returncode or conflicts:
        result["sync_warning"] = (
            "Saved work was restored; resolve preserved merge conflicts or private-file collisions "
            "before finalizing. Unrestored private files remain in checkpoint artifact "
            f"{checkpoint.get('ignored_artifact') or checkpoint['manifest_artifact']}."
        )
    return result


def _reset_formal_assignment(tree: Path, author: str, task_id: str, revision: int | None,
                             state: dict, main_sha: str) -> dict:
    # The previous task is already checkpointed (or complete/clean). Persist
    # the intended assignment BEFORE changing Git, so an interrupted restore
    # cannot subsequently checkpoint the target bytes under the previous task.
    _record_worktree_assignment(author, task_id, revision, state, pending=True)
    result = worktree.force_sync_from_main(_root(), author)
    if result.get("ok"):
        result.update(_restore_task_checkpoint_if_current(
            tree, author, task_id, revision, state, main_sha,
        ))
        _record_worktree_assignment(author, task_id, revision, state)
    return result


def prepare_formal_worktree(
    author: str,
    previous_task: str = "",
    next_task: str = "",
    *,
    expected_revision: int | None = None,
) -> dict:
    """Prepare a stopped worker for another task without erasing unresolved work.

    Completed work may be refreshed; yielded/prerequisite work is checkpointed
    before reassignment, including ignored private files. This is
    controller-only, not an agent tool. The state lock
    serializes the final guard with new claims; the author lock protects both
    candidate submission APIs and their immutable commit ancestry.
    """
    author = _author(author)
    with _merge_lock(), _finalization_lock(author), solve_state.transaction(FORUM_DIR) as state:
        assignments = state.setdefault("worker_tasks", {})
        identity = solve_state.author_key(author)
        # Forum registration can change an intended target, but cannot change
        # which task's private source this worktree actually contains.
        previous_task = assignments.get(identity, previous_task)
        formal = state["formalization"]
        if state.get("phase") != "formalizing" or (
            expected_revision is not None and formal.get("revision") != expected_revision
        ):
            return _sync_blocked("phase_changed", "Formalization phase/revision changed; work preserved.")
        target_task = state.get("formal_tasks", {}).get(next_task, {})
        if target_task.get("status") != "pending":
            return _sync_blocked("task_unavailable", "The next formal task is no longer pending.")
        if not solve_state.task_ready(state, target_task):
            return _sync_blocked("dependencies_pending", "The next formal task has unresolved dependencies.")
        if has_pending_formal_candidate(state, author):
            return _sync_blocked("candidate_pending", "Candidate review is pending; its branch is preserved.")
        tree = worktree.agent_worktree(_root(), author)
        if not tree.is_dir():
            return _sync_blocked("missing_worktree", "The agent has no active worktree.")
        assignment_path = _worktree_assignment_path(author, state)
        assignment = json.loads(assignment_path.read_text()) if assignment_path.is_file() else {}
        source = _worktree_source(state)
        if assignment and any(assignment.get(key) != value for key, value in source.items()):
            # Paper revision invalidates the task graph, not the agent's private
            # work. Save it before either the pending-reset or same-task path;
            # task names and revision numbers can be reused by the new graph.
            main_sha = _accepted_formal_main(state)
            if not main_sha:
                return _sync_blocked("main_changed", "Main differs from the accepted formalization revision.")
            checkpoint = _checkpoint_task_worktree(
                tree, author, assignment["task_id"], assignment.get("task_revision"), source=assignment,
            )
            for history in reversed(state.get("formalization_history", [])):
                if history.get("solution_candidate") == assignment.get("source_candidate"):
                    history.setdefault("worktree_checkpoints", {}).setdefault(identity, {})[
                        assignment["task_id"]] = checkpoint
                    break
            result = _reset_formal_assignment(
                tree, author, next_task, target_task.get("revision"), state, main_sha,
            )
            # The immutable Git ref and artifact also survive a reset failure
            # or interrupted Forum transaction; the durable pointer is source-bound.
            result["parked_checkpoint"] = checkpoint
            return result
        recovered = {}
        if assignment.get("pending"):
            main_sha = _accepted_formal_main(state)
            if not main_sha:
                return _sync_blocked("main_changed", "Main differs from the accepted formalization revision.")
            recovered = _reset_formal_assignment(
                tree, author, assignment["task_id"], assignment["task_revision"], state, main_sha,
            )
            if not recovered.get("ok"):
                return recovered
            assignment["pending"] = False
        if assignment:
            previous_task = assignment["task_id"]
            assignments[identity] = previous_task
        # A legacy assignment has no reliable revision binding; save it for
        # selective reuse, never relabel its bytes with today's task revision.
        previous_revision = assignment.get("task_revision")
        retired = state.get("retired_tasks", {}).get(previous_task, {})
        continuing_refinement = next_task in retired.get("replaced_by", [])
        if previous_task == next_task or continuing_refinement:
            result = {"ok": True, "preserved": True, "worktree": str(tree), **recovered}
            # Representation-only adoption keeps this task alive. Bring a clean
            # stopped tree onto accepted main so later candidates do not submit
            # its already-integrated representation again. Never erase edits.
            main_sha = _accepted_formal_main(state)
            if (not result.get("checkpoint_restored")
                    and (formal.get("contract") or {}).get("version") == 3 and main_sha
                    and not _git(tree, "status", "--porcelain", "--untracked-files=no").stdout.strip()):
                merged = _git(tree, "merge", "--no-edit", "--no-autostash", "--no-overwrite-ignore",
                              main_sha, check=False)
                if merged.returncode:
                    result["sync_warning"] = "Resolve the preserved worktree merge conflict before finalizing."
            _record_worktree_assignment(author, next_task, target_task.get("revision"), state)
            return result
        previous = state.get("formal_tasks", {}).get(previous_task, {})
        # A refinement can introduce a missing interface and block every former
        # assignment. An unclaimed attempt can help that prerequisite after its
        # private work has been saved.
        prerequisite_reassignment = next_task in {
            item["task_id"] for item in ready_statement_prerequisites(state, previous_task)
        }
        yielded_reassignment = solve_state.has_yielded(state, author, previous_task)
        unresolved = set(unresolved_formal_tasks(state, author))
        if yielded_reassignment:
            unresolved.discard(next_task)  # The agent may already have claimed its chosen next task.
        if unresolved or (
            previous_task and previous.get("status") != "complete"
            and not prerequisite_reassignment and not yielded_reassignment
        ):
            return _sync_blocked("unresolved_work", "Unfinished task work is preserved; resume it first.")
        main_sha = _accepted_formal_main(state)
        if not main_sha:
            return _sync_blocked("main_changed", "Main differs from the accepted formalization revision.")
        if previous.get("status") == "complete":
            return _reset_formal_assignment(
                tree, author, next_task, target_task.get("revision"), state, main_sha,
            )

        private_status = _git(tree, "status", "--porcelain", "--", ".",
                              ":(exclude).unity", ":(exclude).lake").stdout.strip()
        ignored = _git(tree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                       "--", ".", ":(exclude).unity", ":(exclude).lake").stdout
        private_commits = _git(tree, "merge-base", "--is-ancestor", "HEAD", main_sha, check=False).returncode != 0
        if (prerequisite_reassignment or yielded_reassignment) and (private_status or ignored or private_commits):
            checkpoint = _checkpoint_task_worktree(
                tree, author, previous_task, previous_revision,
            )
            state.setdefault("worktree_checkpoints", {}).setdefault(identity, {})[previous_task] = checkpoint
            result = _reset_formal_assignment(
                tree, author, next_task, target_task.get("revision"), state, main_sha,
            )
            if result.get("ok"):
                result["parked_checkpoint"] = checkpoint
            return result

        # A fresh/unassigned tree has no known obsolete task. Never reset it:
        # accept a clean ancestor of main, but preserve unexplained local work.
        if private_status:
            return _sync_blocked("dirty_worktree", "Local edits are preserved; reconcile them before changing tasks.")
        if ignored:
            return _sync_blocked("ignored_work", "Ignored private files are preserved; reconcile them before changing tasks.")
        if private_commits:
            return _sync_blocked("local_commits", "Private commits are preserved; reconcile them before changing tasks.")
        merged = _git(tree, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", main_sha, check=False)
        if merged.returncode:
            return _sync_blocked("local_commits", merged.stderr.strip() or "Unassigned commits are preserved.")
        worktree.link_runtime_state(tree, _root())
        worktree.symlink_lake_cache(tree, _root())
        if _saved_task_checkpoint(author, next_task, state):
            return _reset_formal_assignment(
                tree, author, next_task, target_task.get("revision"), state, main_sha,
            )
        _record_worktree_assignment(author, next_task, target_task.get("revision"), state)
        return {"ok": True, "main_sha": main_sha, "worktree": str(tree)}


def sync_from_main(author: str, reason: str = "") -> dict:
    """Merge accepted main without discarding edits, candidate commits, or claims."""
    author = _author(author)
    with _merge_lock(), _finalization_lock(author), solve_state.transaction(FORUM_DIR) as state:
        if state.get("phase") != "formalizing":
            return _sync_blocked("phase_changed", "Synchronization is only available during formalizing.")
        if has_pending_formal_candidate(state, author):
            return _sync_blocked("candidate_pending", "Candidate review is pending; its branch is preserved.")
        tree = worktree.agent_worktree(_root(), author)
        if not tree.is_dir():
            return _sync_blocked("missing_worktree", "The agent has no active worktree.")
        if _git(tree, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            return _sync_blocked("dirty_worktree", "Tracked changes preserved. Commit or resolve them before syncing.")
        main_sha = _accepted_formal_main(state)
        if not main_sha:
            return _sync_blocked("main_changed", "Main differs from the accepted formalization revision.")
        merged = _git(tree, "merge", "--no-edit", "--no-autostash", "--no-overwrite-ignore", main_sha, check=False)
        if merged.returncode:
            return {
                **_sync_blocked("merge_conflict", merged.stderr.strip() or merged.stdout.strip()),
                "main_sha": main_sha,
                "worktree": str(tree),
            }
        worktree.link_runtime_state(tree, _root())
        worktree.symlink_lake_cache(tree, _root())
        return {"ok": True, "main_sha": main_sha, "worktree": str(tree)}


def refine_chunks(author: str, expected_revision: int,
                  changes: solve_state.ChunkRefinement) -> dict:
    """Revise informal nodes with an atomic state-revision check and explicit split/merge lineage.

    Upserts are complete informal node rows. Replacements name old_ids, new_ids,
    and a reason. Source obligations and machine-owned status cannot be edited.
    """
    author = _author(author)
    with _merge_lock():
        result = solve_state.refine_chunks(FORUM_DIR, author, expected_revision, changes)
        if result.get("status") in {"conflict", "noop"}:
            return result
        state = solve_state.load_state(FORUM_DIR)
        formal = state["formalization"]
        dag = {key: formal[key] for key in ("solution_candidate", "solution_sha256", "requirements", "spec")}
        dag["chunks"] = list(state["formal_tasks"].values())
        # This is a recoverable materialized view, not a second source of truth.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=_root() / ".unity", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(dag, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, _root() / ".unity" / "dag.json")
        except OSError as exc:
            result["view_warning"] = f"State saved; dag.json view could not be refreshed: {exc}"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return result


def report_source_issue(
    author: str, anchor_ids: list[str], description: str,
    task_ids: list[str] | None = None,
) -> dict:
    """Report an exact source gap for investigation, without editing supplied bytes.

    Cite frozen anchor IDs, or original source-reference IDs before chunking has
    registered anchors; in that case put the precise location in description.
    """
    author = _author(author)
    issue = solve_state.report_source_issue(
        FORUM_DIR, author, anchor_ids, description, task_ids=task_ids,
    )
    _mirror(author, f"SOURCE ISSUE {issue['issue_id']}", description)
    return issue


def submit_source_repair(
    author: str, issue_id: str, explanation: str, evidence: str, replacement: str = "",
) -> dict:
    """Propose an evidence-backed local repair; this is not an approval or source rewrite.

    Accepted paper bytes remain immutable. A proposal cannot amend them: use
    propose_source_fix for a corrected full draft and independent solution review,
    or reopen_solving for substantial new mathematics.
    """
    author = _author(author)
    state = solve_state.load_state(FORUM_DIR)
    if issue_id not in state.get("source_issues", {}):
        raise ValueError(f"unknown source issue '{issue_id}'")
    explanation = solve_state._text(explanation, "explanation", 16000)
    evidence = solve_state._text(evidence, "evidence", 16000)
    replacement = solve_state._text(replacement, "replacement", 16000, required=False)
    payload = {"author": author, "issue_id": issue_id, "explanation": explanation,
               "evidence": evidence, "replacement": replacement,
               "source_candidate": (solve_state.formal_source(state)).get("candidate_id"),
               "source_sha256": (solve_state.formal_source(state)).get("sha256")}
    record = artifacts.store_text(
        _artifacts_dir(), json.dumps(payload, sort_keys=True),
        kind="solve_source_repair", producer=author, source=issue_id,
    )
    repair = solve_state.submit_source_repair(
        FORUM_DIR, author, issue_id, explanation, evidence, replacement,
        artifact_id=record["artifact_id"],
    )
    _mirror(author, f"SOURCE REPAIR PROPOSAL {repair['repair_id']}", explanation)
    return repair


def request_rechunk(author: str, reason: str, task_ids: list[str] | None = None) -> dict:
    """Queue a contract repair, naming affected tasks when known.

    This does not acquire the merge lock or interrupt verification in progress.
    Unity applies the request safely, preserving unaffected accepted work.
    """
    author = _author(author)
    result = solve_state.request_rechunk(FORUM_DIR, author, reason, task_ids=task_ids)
    if not result.get("idempotent"):
        _mirror(author, "FORMALIZATION REPLAN REQUESTED", reason)
    return result


def submit_representation_review(author: str, task_id: str, review: RepresentationReview) -> dict:
    """Submit evidence for the exact input assigned to this fresh representation reviewer."""
    with _merge_lock():
        return solve_representation.submit_representation_review(
            FORUM_DIR, _author(author), task_id, RepresentationReview.model_validate(review).model_dump(),
        )


def submit_source_diagnosis(author: str, issue_id: str, review: SourceDiagnosis) -> dict:
    """Diagnose paper versus encoding defects; changed paper bytes still need solution review."""
    with _merge_lock():
        return solve_representation.submit_source_diagnosis(
            FORUM_DIR, _author(author), issue_id, SourceDiagnosis.model_validate(review).model_dump(),
        )


def submit_formalization_verdict(
    author: str,
    verdict: str,
    summary: str,
    review: SemanticReview,
    reopen_tasks: list[str] | None = None,
    evidence: str = "",
    representation_repairs: list[RepresentationRepairRequest] | None = None,
) -> dict:
    """Submit snapshot-bound semantic evidence. Approval requires every requirement to pass.

    Read solve_status() for the current snapshot_id and immutable requirements.
    Free-text evidence is optional context, never a substitute for structured review.
    Approval remains pending until the controller verifies that source bytes are unchanged.
    Optional representation_repairs route focused v3 lean_reopen work; they never approve outputs.
    """
    author = _author(author)
    if representation_repairs is not None and not isinstance(representation_repairs, list):
        raise ValueError("representation_repairs must be a list")
    repairs = ([RepresentationRepairRequest.model_validate(row).model_dump()
                for row in representation_repairs] if representation_repairs is not None else None)
    result = solve_state.submit_critic_verdict(
        FORUM_DIR, author, verdict, summary,
        review=SemanticReview.model_validate(review).model_dump(),
        reopen_tasks=reopen_tasks, evidence=evidence,
        representation_repairs=repairs,
    )
    _mirror(author, f"FORMALIZATION VERDICT: {verdict}", summary)
    return result


def _source_path(author: str, path: str, *, default: str) -> Path:
    raw = Path(path) if path else Path(default)
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        # Formalizers run in isolated worktrees. Prefer their local relative
        # path when it exists; informal solvers write shared .unity drafts in
        # the main checkout, which remains the fallback.
        tree_candidate = (worktree.agent_worktree(_root(), author) / raw).resolve()
        root_candidate = (_root() / raw).resolve()
        candidate = tree_candidate if tree_candidate.is_file() else root_candidate
    allowed = [_root().resolve()]
    tree = worktree.agent_worktree(_root(), author)
    if tree.exists():
        allowed.append(tree.resolve())
    if not any(candidate == base or base in candidate.parents for base in allowed):
        raise ValueError("source path must be inside the project or the author's worktree")
    if not candidate.is_file():
        raise ValueError(f"source file does not exist: {candidate}")
    return candidate


def _informal_metrics() -> dict:
    """Return compact solve-only timing, worker, token, and cost telemetry."""
    state = solve_state.load_state(FORUM_DIR)
    events = state.get("events", [])
    first_by_kind: dict[str, float] = {}
    counts: dict[str, int] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        if kind and kind not in first_by_kind:
            first_by_kind[kind] = float(event.get("timestamp") or 0)
    runs = []
    run_log = _root() / ".unity" / "logs" / "run.jsonl"
    if run_log.exists():
        for line in run_log.read_text(errors="replace").splitlines()[-5000:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            context = row.get("context") or {}
            if context.get("command") == "solve" and context.get("run_id") == state.get("run_id"):
                runs.append(row)
    by_phase: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_task: dict[str, dict] = {}
    for row in runs:
        context = row.get("context") or {}
        phase = context.get("phase") or "unknown"
        model = row.get("model") or "unknown"
        usage = row.get("usage") or {}
        task_key = context.get("task_id") or f"role:{context.get('role') or phase}"
        for key, bucket_key in ((phase, by_phase), (model, by_model), (task_key, by_task)):
            bucket = bucket_key.setdefault(key, {
                "turns": 0, "seconds": 0.0, "cost_usd": 0.0,
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            })
            bucket["turns"] += 1
            bucket["seconds"] += float(row.get("seconds") or 0)
            bucket["cost_usd"] += float(row.get("cost_usd") or 0)
            for token_key in ("input_tokens", "output_tokens", "total_tokens"):
                bucket[token_key] += int(usage.get(token_key) or 0)
    started = first_by_kind.get("solve_initialized", 0)
    submitted = first_by_kind.get("solution_candidate_submitted", 0)
    accepted = first_by_kind.get("solution_candidate_accepted", 0)
    completed = max((
        float(event.get("timestamp") or 0) for event in events
        if event.get("kind") == "critic_review_completed"
    ), default=0)
    return {
        "run_id": state.get("run_id"),
        "phase": state.get("phase"),
        "event_counts": counts,
        "worker_turns": len(runs),
        "worker_seconds": round(sum(float(row.get("seconds") or 0) for row in runs), 1),
        "cost_usd": round(sum(float(row.get("cost_usd") or 0) for row in runs), 6),
        "time_to_first_candidate_seconds": round(submitted - started, 3) if submitted and started else None,
        "candidate_review_seconds": round(accepted - submitted, 3) if accepted and submitted else None,
        "post_candidate_seconds": round(completed - submitted, 3) if completed and submitted else None,
        "by_phase": by_phase,
        "by_model": by_model,
        "by_task": by_task,
    }


def _informal_brief(author: str) -> str:
    """Return a bounded, prioritized digest of the live solve state."""
    author = _author(author)
    state = solve_state.load_state(FORUM_DIR)
    solution = state["solution"]
    formal = state["formalization"]
    lines = [
        f"SOLVE RUN {state.get('run_id') or 'uninitialized'}",
        f"Phase: {state.get('phase', 'solving')}",
        f"Problem SHA-256: {state.get('problem_sha256') or 'unavailable'}",
        f"Solution gate: {solution.get('status')} (revision {solution.get('revision')})",
        f"Formalization gate: {formal.get('status')} (revision {formal.get('revision')})",
    ]
    snapshot = formal.get("review_snapshot")
    if snapshot:
        lines.extend([
            f"Review snapshot: {snapshot.get('snapshot_id')} (deterministic checks: "
            f"{'passed' if snapshot.get('passed') else 'failed'})",
            f"Reviewed main: {snapshot.get('main_sha')}; source SHA-256: {snapshot.get('source_sha256')}",
        ])
        if snapshot.get("artifact_id"):
            lines.append(f"Machine review artifact: {snapshot['artifact_id']} (artifact_read)")
    if formal.get("requirements"):
        lines.extend(["", f"REQUIRED SEMANTIC CHECKS ({len(formal['requirements'])})",
                      "Full immutable ledger and structured verdict evidence: solve_status() "
                      "(formalization and critic_verdicts), also .unity/forum/solve-state.json."])
        if (formal.get("contract") or {}).get("artifact_id"):
            lines.append(
                f"Frozen pre-proof specification artifact: {formal['contract']['artifact_id']} (artifact_read). "
                "Its scaffold proof-axiom lists are historical, not current verification."
            )
        for requirement in formal["requirements"]:
            lines.append(f"- {requirement['id']} → tasks {', '.join(requirement['tasks'])}: "
                         f"{requirement['statement'][:240]}")
        lines.append("Approve only after checking every requirement against actual Lean statements and definitions.")
    safe_author = re.sub(r"[^a-zA-Z0-9_-]", "_", author)
    draft = _root() / ".unity" / "source" / "drafts" / safe_author / "PROOF.tex"
    if draft.is_file() and draft.stat().st_size:
        payload = draft.read_bytes()
        lines.extend([
            "",
            "YOUR EXISTING DRAFT",
            f"- {draft.relative_to(_root())}",
            f"- {len(payload)} bytes; SHA-256 {hashlib.sha256(payload).hexdigest()}",
            "- Read these exact bytes before starting new research. If they already form a "
            "complete rigorous solution, emit the candidate immediately.",
        ])
    accepted_id = solution.get("accepted_candidate")
    current_id = solution.get("current_candidate")
    previous_id = solution.get("previous_candidate")
    if current_id:
        candidate = state["solution_candidates"].get(current_id, {})
        lines.extend([
            "",
            "CURRENT SOLUTION CANDIDATE",
            f"- {current_id}: {candidate.get('status')} by {candidate.get('author')}",
            f"- artifact {candidate.get('artifact_id')} SHA-256 {candidate.get('sha256')}",
        ])
        if candidate.get("components"):
            lines.append(
                "- incorporated component revisions: "
                + ", ".join(item["result_id"] for item in candidate["components"])
            )
        for review in candidate.get("reviews", []):
            lines.append(
                f"- review by {review.get('author')}: {review.get('verdict')} — "
                f"{review.get('review', '')[:500]}"
            )
    if accepted_id and accepted_id != current_id:
        candidate = state["solution_candidates"].get(accepted_id, {})
        lines.append(
            f"Accepted solution: {accepted_id}, artifact {candidate.get('artifact_id')}, "
            f"SHA-256 {candidate.get('sha256')}"
        )
    if previous_id and not accepted_id:
        candidate = state["solution_candidates"].get(previous_id, {})
        lines.append(
            f"Previous accepted solution now under revision: {previous_id}, artifact "
            f"{candidate.get('artifact_id')}, SHA-256 {candidate.get('sha256')}"
        )
        if candidate.get("components"):
            lines.append(
                "Reusable prior component revisions: "
                + ", ".join(item["result_id"] for item in candidate["components"])
            )
    if solution.get("reopen_reason"):
        lines.append(f"Solution blocker/reopen reason: {solution['reopen_reason']}")
    chunking_attempts = [
        item for item in state.get("chunking_attempts", [])
        if item.get("candidate_id") == accepted_id
    ]
    if chunking_attempts:
        lines.extend(["", "CHUNKING ATTEMPTS"])
        for item in chunking_attempts[-10:]:
            detail = f" — {item.get('reason', '')[:700]}" if item.get("reason") else ""
            lines.append(
                f"- {item.get('author')} attempt {item.get('attempt')} "
                f"[{item.get('status')}]{detail}"
            )
    verdicts = state.get("critic_verdicts", [])
    if verdicts:
        verdict = verdicts[-1]
        lines.extend([
            "",
            "LATEST FORMALIZATION VERDICT",
            f"- {verdict.get('verdict')} by {verdict.get('author')}: "
            f"{verdict.get('summary', '')[:1000]}",
        ])
        if verdict.get("reopen_tasks"):
            lines.append("- reopened tasks: " + ", ".join(verdict["reopen_tasks"]))
        if verdict.get("evidence"):
            lines.append(f"- evidence: {verdict['evidence'][:500]}")
        if verdict.get("review"):
            lines.append(f"- semantic review snapshot: {verdict['review']['snapshot_id']}")
            for entry in verdict["review"]["requirements"]:
                lines.append(f"- {entry['requirement_id']}: {entry['status']} "
                             f"({', '.join(entry['declarations']) or 'no declaration checked'})")
            lines.append("- Full structured evidence and rationale: solve_status().critic_verdicts")

    owned_strategy = next((
        item for item in state["strategies"].values()
        if str(item.get("owner") or "").casefold() == author.casefold()
        and item.get("status") == "claimed"
    ), None)
    assigned_task = (owned_strategy or {}).get("target")
    all_informal_tasks = [
        item for item in state.get("informal_tasks", {}).values()
        if item.get("solution_revision") == solution.get("revision")
        and item.get("status") not in {"superseded", "cancelled"}
    ]
    if all_informal_tasks:
        resolved_count = sum(item.get("status") == "resolved" for item in all_informal_tasks)
        lines.append(
            f"Informal work: {resolved_count}/{len(all_informal_tasks)} tasks resolved; "
            f"{len(state.get('informal_results', {}))} component revisions recorded"
        )
    visible_task_ids: set[str] | None = None
    if assigned_task and assigned_task in state.get("informal_tasks", {}):
        item = state["informal_tasks"][assigned_task]
        visible_task_ids = {assigned_task, *item.get("dependencies", [])}
        visible_task_ids.update(
            task["task_id"] for task in all_informal_tasks
            if assigned_task in task.get("dependencies", [])
        )
        lines.extend([
            "", "YOUR INFORMAL TASK",
            f"- {item['task_id']} [{item['status']}; {item.get('kind')}]: {item['title']}",
            f"- {item['description'][:1000]}",
        ])
        if item.get("dependencies"):
            lines.append("- dependencies: " + ", ".join(item["dependencies"]))
    informal_tasks = (
        [item for item in all_informal_tasks if item["task_id"] in visible_task_ids]
        if visible_task_ids is not None else all_informal_tasks
    )
    if informal_tasks:
        lines.extend(["", "INFORMAL TASKS"])
        for item in informal_tasks[-20:]:
            deps = item.get("dependencies", [])
            lines.append(
                f"- {item['task_id']} [{item['status']}; {item.get('kind')}]: "
                f"{item['title']} — {item['description'][:400]}"
                + (f"; deps={','.join(deps)}" if deps else "")
            )
        omitted = len(all_informal_tasks) - len(informal_tasks)
        if omitted:
            lines.append(f"- ... {omitted} unrelated tasks omitted; use solve_status for full state")

    results = [
        item for item in state.get("informal_results", {}).values()
        if item.get("solution_revision") == solution.get("revision")
        and item.get("status") != "superseded"
    ]
    if visible_task_ids is not None:
        results = [item for item in results if item.get("task_id") in visible_task_ids]
    if results:
        lines.extend(["", "INFORMAL COMPONENT RESULTS"])
        for item in results[-16:]:
            lines.append(
                f"- {item['result_id']} [{item['status']}; {item.get('kind')}] "
                f"task={item['task_id']} by {item['author']}: {item['summary'][:600]}"
            )
            lines.append(f"  artifact {item['artifact_id']} SHA-256 {item['sha256']}")

    issues = [item for item in state.get("review_issues", {}).values()
              if item.get("status") == "open"]
    if issues:
        lines.extend(["", "ACTIONABLE PAPER REVIEW ISSUES"])
        for item in issues[-12:]:
            lines.append(
                f"- {item['issue_id']} ({item['kind']}): {item['description'][:700]} "
                f"→ repair task {item.get('repair_task')}"
            )

    tasks = list(state.get("formal_tasks", {}).values())
    if tasks:
        lines.extend(["", "FORMALIZATION TASKS"])
        for task in tasks[:40]:
            lines.append(
                f"- {task['task_id']} [{task['status']}]: {task.get('lean_decl')}"
                + (f"; deps={','.join(task.get('dependencies', []))}" if task.get("dependencies") else "")
            )

    formal_candidates = list(state.get("formal_candidates", {}).values())
    if formal_candidates:
        lines.extend(["", "RECENT FORMALIZATION CANDIDATES"])
        for item in formal_candidates[-12:]:
            lines.append(
                f"- {item['candidate_id']} [{item['status']}] task={item['task_id']} "
                f"by {item['author']} at {item['commit_sha'][:12]}"
            )
            if item.get("error"):
                lines.append(f"  failure: {item['error'][:800]}")
            build_artifact = (item.get("build") or {}).get("artifact_id")
            verification_artifact = (item.get("verification") or {}).get("artifact_id")
            if build_artifact or verification_artifact:
                lines.append(
                    "  artifacts: "
                    + ", ".join(filter(None, [build_artifact, verification_artifact]))
                )

    owned = [item for item in state["strategies"].values()
             if str(item.get("owner") or "").casefold() == author.casefold()
             and item.get("status") == "claimed"]
    if owned:
        lines.extend(["", "YOUR CLAIMED STRATEGIES"])
        for item in owned[:8]:
            lines.append(f"- {item['strategy_id']} target={item.get('target') or 'global'}: {item['description']}")

    active = [item for item in state["strategies"].values()
              if item.get("status") in {"registered", "claimed", "paused"}
              and item.get("phase") == state.get("phase")]
    if visible_task_ids is not None:
        active = [item for item in active if not item.get("target") or item.get("target") in visible_task_ids]
    if active:
        lines.extend(["", "ACTIVE STRATEGIES"])
        for item in active[-16:]:
            owner = item.get("owner") or "unclaimed"
            lines.append(
                f"- {item['strategy_id']} [{item['status']}; {owner}] "
                f"target={item.get('target') or 'global'}: {item['description'][:500]}"
            )

    findings = [item for item in state["findings"].values() if item.get("status") == "active"]
    if visible_task_ids is not None:
        findings = [item for item in findings if not item.get("target") or item.get("target") in visible_task_ids]
    if findings:
        lines.extend(["", "RECENT FINDINGS"])
        for item in findings[-16:]:
            lines.append(
                f"- {item['finding_id']} ({item['confidence']}%, {item['kind']}) "
                f"{item['title']}: {item['content'][:600]}"
            )

    obstacles = [item for item in state["obstacles"].values() if item.get("status") == "open"]
    if visible_task_ids is not None:
        obstacles = [item for item in obstacles if not item.get("target") or item.get("target") in visible_task_ids]
    if obstacles:
        lines.extend(["", "OPEN OBSTACLES"])
        for item in obstacles[-10:]:
            lines.append(f"- {item['obstacle_id']} by {item['author']}: {item['goal_state'][:600]}")

    questions = [item for item in state["questions"].values()
                 if item.get("status") == "open"
                 and (not item.get("to") or item.get("to", "").casefold() == author.casefold())]
    if questions:
        lines.extend(["", "OPEN QUESTIONS"])
        for item in questions[-10:]:
            lines.append(f"- {item['question_id']} from {item['author']}: {item['body'][:500]}")

    text = "\n".join(lines)
    limit = 12_000
    try:
        limit = max(2_000, min(32_000, int(os.getenv("UNITY_SOLVE_BRIEF_CHARS", "12000"))))
    except ValueError:
        pass
    return text if len(text) <= limit else text[:limit].rstrip() + "\n...[brief truncated]"


def create_subgoal(
    author: str,
    title: str,
    description: str,
    parent_id: str = "",
    dependencies: list[str] | None = None,
) -> dict:
    """Create a mathematical subgoal discovered during informal solving."""
    author = _author(author)
    result = solve_state.create_subgoal(
        FORUM_DIR, author, title, description,
        parent_id=parent_id, dependencies=dependencies,
    )
    _mirror(author, f"SUBGOAL {result['task_id']}: {title}", description, result["task_id"])
    return result


def create_informal_task(
    author: str,
    kind: str,
    title: str,
    description: str,
    dependencies: list[str] | None = None,
    parent_task: str = "",
) -> dict:
    """Create a dependency-aware mathematical, writing, synthesis, or repair task."""
    author = _author(author)
    result = solve_state.create_informal_task(
        FORUM_DIR, author, kind, title, description,
        parent_id=parent_task, dependencies=dependencies,
    )
    if result["status"] == "created":
        task = result["task"]
        _mirror(author, f"INFORMAL TASK {task['task_id']}: {title}", description, task["task_id"])
    return result


def _informal_publish_finding(
    author: str,
    kind: str,
    title: str,
    content: str,
    confidence: int,
    target: str = "",
    strategy_id: str = "",
    evidence: str = "",
    supersedes: str = "",
) -> dict:
    """Publish or correct concise live knowledge with agent-chosen kind/confidence."""
    author = _author(author)
    if len(evidence) > 4000:
        record = artifacts.store_text(
            _artifacts_dir(), evidence, kind="solve_finding_evidence",
            producer=author, source=title,
        )
        evidence = f"artifact {record['artifact_id']} SHA-256 {record['sha256']}"
    result = solve_state.publish_finding(
        FORUM_DIR, author, kind, title, content, confidence,
        target=target, strategy_id=strategy_id, evidence=evidence, supersedes=supersedes,
    )
    _mirror(author, f"FINDING {result['finding_id']}: {title}", content, target)
    return result


def emit_informal_result(
    author: str,
    task_id: str,
    strategy_id: str,
    path: str,
    summary: str,
    kind: str = "",
    supersedes: str = "",
) -> dict:
    """Snapshot a reusable argument or paper component produced for an informal task."""
    author = _author(author)
    source = _source_path(author, path, default="")
    content = source.read_text(errors="replace")
    record = artifacts.store_text(
        _artifacts_dir(), content, kind="solve_informal_component",
        producer=author, source=str(source),
        metadata={"task_id": task_id, "strategy_id": strategy_id},
    )
    result = solve_state.submit_informal_result(
        FORUM_DIR, author, task_id, strategy_id,
        record["artifact_id"], record["sha256"], str(source), summary,
        kind=kind, supersedes=supersedes,
    )
    if result["status"] == "submitted":
        item = result["result"]
        _mirror(author, f"INFORMAL RESULT {item['result_id']}", summary, task_id)
    return result


def review_informal_result(
    result_id: str,
    author: str,
    verdict: str,
    review: str,
) -> dict:
    """Support or object to one exact immutable informal component."""
    author = _author(author)
    result = solve_state.review_informal_result(
        FORUM_DIR, result_id, author, verdict, review,
    )
    _mirror(author, f"INFORMAL RESULT REVIEW {result_id}: {verdict}", review,
            result["result"]["task_id"])
    return result


def emit_solution_candidate(
    author: str,
    path: str = "",
    strategy_id: str = "",
    notes: str = "",
    supersedes: str = "",
    component_ids: list[str] | None = None,
) -> dict:
    """Snapshot and submit an exact natural-language solution for independent review."""
    author = _author(author)
    source = _source_path(
        author, path,
        default=f".unity/source/drafts/{re.sub(r'[^a-zA-Z0-9_-]', '_', author)}/PROOF.tex",
    )
    content = source.read_text(errors="replace")
    record = artifacts.store_text(
        _artifacts_dir(), content, kind="solve_solution_candidate",
        producer=author, source=str(source),
    )
    result = solve_state.submit_solution_candidate(
        FORUM_DIR, author, record["artifact_id"], record["sha256"], str(source),
        strategy_id=strategy_id, notes=notes, supersedes=supersedes,
        component_ids=component_ids,
    )
    if result["status"] == "submitted":
        candidate = result["candidate"]
        _mirror(author, f"SOLUTION CANDIDATE {candidate['candidate_id']}",
                f"artifact {candidate['artifact_id']} SHA-256 {candidate['sha256']}")
    return result


def review_solution_candidate(
    candidate_id: str,
    author: str,
    verdict: str,
    review: str,
    evidence: str = "",
    issues: list[dict] | None = None,
) -> dict:
    """Approve or object to the exact immutable solution candidate under review."""
    author = _author(author)
    result = solve_state.review_solution_candidate(
        FORUM_DIR, candidate_id, author, verdict, review,
        evidence=evidence, issues=issues,
    )
    _mirror(author, f"SOLUTION REVIEW {candidate_id}: {verdict}", review)
    return result


def propose_source_fix(
    author: str,
    path: str,
    reason: str,
    supersedes: str = "",
) -> dict:
    """Submit corrected paper bytes and return the pipeline to independent solution review."""
    author = _author(author)
    source = _source_path(author, path, default=".unity/source/PROOF.tex")
    content = source.read_text(errors="replace")
    record = artifacts.store_text(
        _artifacts_dir(), content, kind="solve_solution_candidate",
        producer=author, source=str(source), metadata={"reason": reason},
    )
    with _merge_lock():
        result = solve_state.submit_solution_candidate(
            FORUM_DIR, author, record["artifact_id"], record["sha256"], str(source),
            notes=reason, supersedes=supersedes, replace_accepted=True,
        )
    _mirror(author, "SOURCE FIX PROPOSED", reason)
    return result


def reopen_solving(author: str, reason: str) -> dict:
    """Return to informal solving because the accepted mathematics is substantively wrong."""
    author = _author(author)
    with _merge_lock():
        result = solve_state.reopen_solution(FORUM_DIR, author, reason)
    _mirror(author, "SOLVING REOPENED", reason)
    return result



def solve_brief(author: str, task_id: str = "") -> str:
    """Return the current phase's bounded solve memory without changing forums."""
    if PROFILE in {"solving", "solution_review", "retrospective"}:
        return _informal_brief(author)
    return _formal_brief(author, task_id=task_id)


FORMAL_COMMON = (
    solve_status, solve_metrics, solve_brief,
    solve_task, solve_requirements, read_finding,
    forum_post, forum_read, artifact_info, artifact_read,
)
FORMAL_COORDINATION = (
    register_strategy, claim_strategy, assist_strategy, unclaim_strategy,
    mark_strategy_incorrect, yield_task, publish_finding, report_obstacle,
    ask_question, answer_question,
)
SOURCE_FEEDBACK = (publish_finding, report_obstacle, ask_question, answer_question,
                   report_source_issue, submit_source_repair)


def validate_chunks() -> dict:
    """Read and validate this attempt's draft DAG; do not accept it or change state."""
    draft_path = os.getenv("UNITY_SOLVE_DRAFT_PATH", "")
    if not draft_path:
        raise ValueError("validate_chunks requires an assigned chunking draft path")
    from ..solve_formal_runtime import validate_chunking_draft
    from ..config import Paths

    paths = Paths.from_unity_dir(_root() / ".unity")
    return validate_chunking_draft(paths, Path(draft_path))


INFORMAL_COMMON = (
    solve_status, solve_metrics, _informal_brief, forum_post, forum_read,
    artifact_info, artifact_read,
)
INFORMAL_COORDINATION = (
    register_strategy, claim_strategy, assist_strategy, unclaim_strategy,
    mark_strategy_incorrect, _informal_publish_finding, report_obstacle,
    ask_question, answer_question,
)
PROFILE_TOOLS = {
    "solving": INFORMAL_COMMON + INFORMAL_COORDINATION + (
        create_subgoal, create_informal_task, emit_informal_result,
        review_informal_result, emit_solution_candidate,
    ),
    "solution_review": INFORMAL_COMMON + (
        _informal_publish_finding, ask_question, answer_question, review_solution_candidate,
    ),
    "chunking": FORMAL_COMMON + SOURCE_FEEDBACK + (validate_chunks, propose_source_fix, reopen_solving),
    "formalizing": FORMAL_COMMON + FORMAL_COORDINATION + (
        finalize_formalization, emit_formalization_candidate, sync_from_main, request_rechunk,
        report_source_issue, submit_source_repair, refine_chunks, reserve_files,
        propose_source_fix, reopen_solving,
    ),
    "critic": FORMAL_COMMON + SOURCE_FEEDBACK + (
        request_rechunk, submit_formalization_verdict, propose_source_fix, reopen_solving,
    ),
    "retrospective": INFORMAL_COMMON,
    "source_repair": FORMAL_COMMON + SOURCE_FEEDBACK + (
        submit_source_diagnosis, propose_source_fix, reopen_solving,
    ),
    "representation_review": FORMAL_COMMON + (submit_representation_review,),
}

def build_server(profile: str) -> FastMCP:
    """Expose only the independent solve tools required by this phase."""
    if profile not in PROFILES:
        raise ValueError(f"Unknown solve MCP profile: {profile}")
    server = FastMCP(f"unity-solve-forum-{profile}")
    for tool in PROFILE_TOOLS[profile]:
        name = {"_informal_brief": "solve_brief", "_informal_publish_finding": "publish_finding"}.get(tool.__name__, tool.__name__)
        server.tool(name=name)(tool)
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
