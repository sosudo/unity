"""Author-bound, phase-scoped Optimize forum and candidate coordination tools."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re

from fastmcp import FastMCP

from .. import artifacts, optimize_tasks as tasks
from . import server as discussion

FORUM_DIR = Path("forum")
PROJECT_ROOT: Path | None = None
PROFILE = "optimizing"
BOUND_AUTHOR = ""
PROFILES = {"preparation", "architect", "exploration", "chunking", "optimizing", "critic", "retrospective"}
# Bound the actual JSON data before FastMCP copies it into text and structured
# content. Full evidence stays in immutable artifacts and in workflow state.
MAX_RESPONSE_BYTES = 64 * 1024


def configure(forum_dir: Path, project_root: Path, profile: str = "optimizing", author: str = "") -> None:
    global FORUM_DIR, PROJECT_ROOT, PROFILE, BOUND_AUTHOR
    if profile not in PROFILES: raise ValueError("unknown Optimize forum profile")
    root = Path(project_root).resolve()
    state = tasks.load(forum_dir)
    if Path(state["root"]).resolve() != root: raise ValueError("Optimize forum project root mismatch")
    if author: tasks._agent(state, author)
    FORUM_DIR, PROJECT_ROOT, PROFILE, BOUND_AUTHOR = Path(forum_dir), root, profile, author
    discussion.FORUM_DIR, discussion.PROJECT_ROOT = FORUM_DIR, PROJECT_ROOT
    discussion.ICRL_ENABLED = False


def _root() -> Path:
    if PROJECT_ROOT is None: raise ValueError("Optimize forum is not configured")
    return PROJECT_ROOT


def _author(author: str) -> str:
    bound = BOUND_AUTHOR or os.getenv("UNITY_OPTIMIZE_AUTHOR", "").strip() or os.getenv("UNITY_AGENT_NAME", "").strip()
    if not author.strip() or (bound and bound != author):
        raise ValueError("author does not match the bound worker")
    tasks._agent(tasks.load(FORUM_DIR), bound or author)
    return bound or author


def _task_id(task_id: str) -> str:
    bound = os.getenv("UNITY_OPTIMIZE_TASK_ID", "").strip()
    if not task_id or (bound and bound != task_id):
        raise ValueError("task does not match the bound worker")
    if task_id not in tasks.load(FORUM_DIR)["tasks"]:
        raise ValueError("unknown Optimize task ID; phase names are not tasks. "
                         "Use optimize_brief for phase work and optimize_status for existing tasks")
    return task_id


def _profile(*allowed: str) -> None:
    if PROFILE not in allowed: raise ValueError("tool not available in this Optimize phase")


def _assignment(author: str, assignment_id: str) -> dict:
    state = tasks.load(FORUM_DIR)
    row = tasks._owned(state, author, assignment_id)
    _task_id(row["task_id"])
    bound = os.getenv("UNITY_OPTIMIZE_WORKTREE", "").strip()
    if bound and Path(bound).resolve() != Path(row["worktree"]).resolve():
        raise ValueError("assignment worktree does not match bound worker")
    return row


def _thread(thread_id: str) -> str:
    return "optimize-" + (re.sub(r"[^a-zA-Z0-9_-]", "-", thread_id).strip("-") or "global")


def _mirror(author: str, title: str, body: str, task_id: str = "global") -> None:
    thread_id = _thread(task_id)
    discussion.forum_create_thread(thread_id, "Optimize: " + task_id)
    discussion.forum_post(thread_id, author, title + "\n\n" + body)


def optimize_status() -> dict:
    """Bounded current summaries; artifact references retain complete evidence."""
    state = tasks.load(FORUM_DIR)
    counts = {}
    for task in state["tasks"].values(): counts[task["status"]] = counts.get(task["status"], 0) + 1
    candidate_counts = {}
    for row in state["candidates"].values():
        candidate_counts[row["status"]] = candidate_counts.get(row["status"], 0) + 1
    rounds, review = state["rounds"], state["review"]
    scope = state["scope"] if isinstance(state["scope"], dict) else {"targets": state["scope"] or "Universe"}
    response = {key: deepcopy(state[key]) for key in ("run_id", "revision", "phase", "main_sha")} | {
        "metric": _summary_fields(state["metric"], ("name", "builtin", "direction", "units", "sha256", "qualification"))
                  if isinstance(state["metric"], dict) else _summary_text(state["metric"]),
        "scope": {"targets": _summary_text(state["config"].get("targets", scope.get("targets", "Universe"))),
                  "file_count": len(scope.get("files", []))},
        "config": _summary_fields(state["config"], ("revision", "primary", "max_rounds")),
        "rounds": {"started": rounds["started"], "completed": rounds["completed"],
                   "active": _summary_fields(rounds.get("active") or {}, ("number", "started_at")),
                   "history_count": len(rounds.get("history", []))},
        "review": {**_summary_fields(review, ("reviewer", "status")),
                   "snapshot": _verification_summary(review.get("snapshot") or {}),
                   "verdict": _summary_fields(review.get("verdict") or {},
                       ("author", "approved", "verdict", "snapshot_id", "snapshot_sha256", "reasoning")),
                   "repair_request_count": len(review.get("repair_requests", []))},
        "task_counts": counts, "candidate_counts": candidate_counts,
        "tasks": [_task_summary(row) for row in state["tasks"].values()],
        "candidates": [_candidate_summary(state, row) for row in state["candidates"].values()],
        "baseline_sealed": state["baseline_sealed"], "phase_dispatches": _phase_summaries(state),
        "records": {key: _brief_artifact(state, key, state[key]) for key in
                    ("tasks", "candidates", "rounds", "review", "metric", "scope", "config")}}
    return _bounded_response(state, "status", response)


def _summary_text(value, limit: int = 512) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "… [see full artifact]"


def _summary_fields(row: dict, keys) -> dict:
    prose = {"notes", "feedback", "reason", "reasoning", "qualification", "definition", "description", "title"}
    return {key: (_summary_text(row[key]) if key in prose and isinstance(row[key], str) else deepcopy(row[key]))
            for key in keys if key in row and not isinstance(row[key], (dict, list))}


def _task_summary(row: dict) -> dict:
    return _summary_fields(row, ("task_id", "status", "revision", "title", "reason")) | {
        "chunk_ids": deepcopy(row.get("chunk_ids", [])),
        "dependencies": deepcopy(row.get("dependencies", []))}


def _verification_summary(row: dict) -> dict:
    build, audit = row.get("build") or {}, row.get("hole_audit") or {}
    return _summary_fields(row, ("passed", "snapshot_id", "sha256", "main_sha", "metric_sha256", "baseline_sha256",
                                "config_revision", "reason", "stale", "qualification")) | {
        "issue_count": len(row.get("issues", [])),
        "build": _summary_fields(build, ("passed", "source_stable")) | {
            "unchecked_file_count": len(build.get("unchecked_files", []))},
        "hole_audit": _summary_fields(audit, ("passed", "holes_remaining", "qualification")) | {
            "issue_count": len(audit.get("issues", [])), "inherited_count": len(audit.get("inherited", [])),
            "introduced_count": len(audit.get("introduced", []))}}


def _candidate_summary(state: dict, row: dict) -> dict:
    verification = row.get("verification") or {}
    return _summary_fields(row, ("candidate_id", "task_id", "author", "status", "assignment_id", "strategy_id",
        "base_main_sha", "task_revision", "config_revision", "commit_sha", "diff_sha256", "notes", "feedback")) | {
        "verification": _verification_summary(verification),
        "comparisons": {cid: _summary_fields(value, ("subject", "before", "after", "improved", "regressed",
            "delta", "improvement_percent", "improvement_score", "evidence_kind", "qualification"))
            for cid, value in verification.get("comparisons", {}).items()},
        "measurements": {cid: _summary_fields(value, ("subject", "raw_value", "units", "direction", "evidence_kind",
            "metric_sha256", "source_sha256", "method_sha256", "sha256"))
            for cid, value in row.get("measurements", {}).items()},
        "selection": _summary_fields(row.get("selection") or {}, ("author", "reason", "at")),
        "endorsement_count": len(row.get("endorsements", [])), "objection_count": len(row.get("objections", [])),
        "records": {"candidate": _brief_artifact(state, "candidate", row)}}


def _bounded_response(state: dict, key: str, response: dict) -> dict:
    # Count escaped JSON, not a row count or the CLI's visible text preview.
    if len(json.dumps(response, ensure_ascii=True).encode("utf-8")) <= MAX_RESPONSE_BYTES:
        return response
    # The index retains every summary and its full-evidence references. No rows
    # are silently dropped and neither workflow state nor evidence is trimmed.
    header = _summary_fields(response, ("run_id", "revision", "phase", "main_sha", "candidate_id", "task_id", "status"))
    # Never turn a long identifier or qualitative score into a different value.
    # Oversized identifiers remain exact in the referenced index, not clipped.
    result = {key: value for key, value in header.items() if len(json.dumps(value).encode("utf-8")) <= 1024}
    result.update(summary_only=True, counts={key: len(value) for key, value in response.items()
                                            if isinstance(value, (dict, list))},
                  records={"response": _brief_artifact(state, key + "_response", response)},
                  next_action="Read records.response with artifact_read; follow next_offset for the full index and evidence references.")
    return result


def _phase_summaries(state: dict, limit: int = 5) -> dict:
    rows = list(state.get("phase_dispatches", {}).values())
    return {"count": len(rows), "recent": [
        {"dispatch_id": row["dispatch_id"], "profile": row["profile"], "status": row["status"],
         "reason": _summary_text(row.get("reason", ""), 240), "result_count": len(row.get("results", {})),
         "results": [{"author": result["author"], "status": result["status"],
                      "summary": _summary_text(result["summary"], 240)}
                     for result in list(row.get("results", {}).values())[:10]]}
        for row in rows[-limit:]], "full_records": "optimize_brief.records.phase_dispatches"}


def _brief_artifact(state: dict, key: str, value) -> dict:
    """Retain complete data without duplicating it in every model-facing brief."""
    record = artifacts.store_text(_root() / ".unity" / "artifacts",
        # ASCII escapes preserve exact Unicode data across the existing
        # byte-paged artifact reader, including pages split inside an escape.
        json.dumps(value, sort_keys=True, ensure_ascii=True), kind="optimize_brief",
        source=key, metadata={"run_id": state["run_id"], "revision": state["revision"]})
    return {field: record[field] for field in ("artifact_id", "sha256", "bytes")}


def optimize_brief() -> dict:
    """Compact phase/task instructions; complete records are paged artifacts."""
    state = tasks.load(FORUM_DIR)
    raw_scope, baseline = state["scope"], state["baseline"] or {}
    scope = raw_scope if isinstance(raw_scope, dict) else {"targets": raw_scope or "Universe"}
    proposals = list(state["plan_proposals"].values())
    bound_task = os.getenv("UNITY_OPTIMIZE_TASK_ID", "").strip()
    if bound_task:
        _task_id(bound_task)
    full = {"metric": state["metric"], "scope": raw_scope, "baseline": baseline,
            "chunks": state["chunks"], "plan_proposals": state["plan_proposals"],
            "baseline_measurements": state["baseline_measurements"],
            "current_measurements": state["measurements"],
            "phase_dispatches": state.get("phase_dispatches", {}),
            "tasks": state["tasks"], "candidates": state["candidates"],
            "rounds": state["rounds"], "review": state["review"]}
    metric = state["metric"] if isinstance(state["metric"], dict) else {"name": state["metric"]}
    next_actions = [
        "Read optimize_task only for a nonempty bound_task; phase names are not task IDs.",
        "Use optimize_inventory with an exact project-relative path or exact declaration name; follow next_offset.",
        "Read complete records with artifact_read and follow next_offset for remaining bytes.",
        "For phase findings/questions/obstacles use task_id=''.",
    ]
    if PROFILE in {"architect", "exploration", "chunking"}:
        next_actions.append("Use report_phase_result to record your completed, blocked or eligible skipped preparation contribution.")
    response = {"root": state["root"], "run_id": state["run_id"], "revision": state["revision"],
            "phase": state["phase"], "profile": PROFILE,
            "metric": {key: _summary_text(metric[key]) for key in
                       ("name", "builtin", "direction", "units", "definition", "sha256") if key in metric},
            "scope": {"targets": _summary_text(state["config"].get("targets", scope.get("targets", "Universe"))),
                      "file_count": len(scope.get("files", []))},
            "primary": state["config"].get("primary"),
            "baseline": {"captured": bool(baseline), "sealed": state["baseline_sealed"],
                         "sha256": baseline.get("sha256"),
                         "build_passed": baseline.get("build", {}).get("passed"),
                         "measurement_count": len(state["baseline_measurements"])},
            "inventory": {"count": len(state["chunks"]), "sha256": tasks.inventory_identity(state)},
            "plan_proposals": {"count": len(proposals), "recent": [
                {"proposal_id": row.get("proposal_id"), "author": row.get("author"),
                 "task_count": len(row.get("tasks", {})), "exclusion_count": len(row.get("exclusions", {})),
                 "reason": _summary_text(row.get("reason", ""), 240)} for row in proposals[-5:]]},
            "records": {key: _brief_artifact(state, key, value) for key, value in full.items()},
            "turn_kind": "task" if bound_task else "phase", "bound_task": bound_task,
            "phase_dispatch_id": os.getenv("UNITY_OPTIMIZE_PHASE_DISPATCH_ID", "").strip(),
            "phase_dispatches": _phase_summaries(state, limit=1),
            "next_actions": next_actions, "rules": [
                "Every roster member, including the primary, may optimize.",
                "Use distinct competing strategies in separate assigned private worktrees.",
                "Preserve the original baseline; report current measurements separately.",
                "Do not weaken statements or add holes/axioms to improve a score.",
                "The primary selects among checked candidates; only the controller integrates them.",
                "Only the configured primary reviews the exact final snapshot, read-only.",
                "Distinguish measured scores from agent judgments; no improvement found is not global optimality."]}
    return _bounded_response(state, "brief", response)


def optimize_task(task_id: str) -> dict:
    state = tasks.load(FORUM_DIR)
    if task_id not in state["tasks"]:
        raise ValueError("unknown Optimize task ID; phase names are not tasks. "
                         "Use optimize_brief for phase work and optimize_status for existing tasks")
    task = state["tasks"][task_id]
    full = {"task": task, "chunks": [state["chunks"][cid] for cid in task["chunk_ids"]],
            "strategies": [row for row in state["strategies"].values() if row["task_id"] == task_id],
            "assignments": [row for row in state["assignments"].values() if row["task_id"] == task_id],
            "candidates": [row for row in state["candidates"].values() if row["task_id"] == task_id]}
    response = {"task": _task_summary(task),
                "chunks": [_summary_fields(row, ("chunk_id", "name", "path", "kind", "start_line", "end_line", "source_sha256"))
                           for row in full["chunks"]],
                "strategies": [_summary_fields(row, ("strategy_id", "author", "status", "title", "description"))
                               for row in full["strategies"]],
                "assignments": [_summary_fields(row, ("assignment_id", "author", "status", "base_main_sha", "task_revision", "config_revision"))
                                for row in full["assignments"]],
                "candidates": [_candidate_summary(state, row) for row in full["candidates"]],
                "records": {key: _brief_artifact(state, key, value) for key, value in full.items()}}
    return _bounded_response(state, "task", response)


def optimize_inventory(path: str = "", name: str = "", offset: int = 0, limit: int = 50,
                       inventory_sha256: str = "") -> dict:
    """Page canonical chunks; path and qualified declaration name match exactly.

    Supply the first page's inventory_sha256 on following pages to reject stale
    inventory. This identity does not change when unrelated forum events occur.
    """
    return tasks.inventory_page(tasks.load(FORUM_DIR), path=path, name=name, offset=offset,
                                limit=limit, inventory_sha256=inventory_sha256)


def forum_post(thread_id: str, author: str, content: str, reply_to: list[str] | None = None) -> dict:
    author = _author(author)
    tid = _thread(thread_id)
    discussion.forum_create_thread(tid, "Optimize: " + thread_id)
    return discussion.forum_post(tid, author, content, reply_to=reply_to)


def forum_read(thread_id: str, sort: str = "hot") -> dict:
    return discussion.forum_read(_thread(thread_id), sort=sort)


def artifact_info(artifact_id: str) -> dict:
    """Read immutable artifact metadata in this project only."""
    return artifacts.artifact_info(_root() / ".unity" / "artifacts", artifact_id)


def artifact_read(artifact_id: str, offset: int = 0, limit: int = artifacts.DEFAULT_READ_LIMIT) -> dict:
    """Read one bounded page; use the returned next_offset for remaining bytes."""
    return artifacts.read_artifact(_root() / ".unity" / "artifacts", artifact_id, offset=offset, limit=limit)


def _communication(kind: str, author: str, task_id: str, content: str, **details) -> dict:
    author = _author(author)
    if not content.strip(): raise ValueError("discussion content required")
    with tasks.transaction(FORUM_DIR) as state:
        if task_id and task_id not in state["tasks"]:
            raise ValueError("unknown Optimize task ID; use task_id='' for phase findings, questions or obstacles")
        if not task_id:
            details.update(phase=PROFILE,
                phase_dispatch_id=os.getenv("UNITY_OPTIMIZE_PHASE_DISPATCH_ID", "").strip())
        row = tasks._event(state, kind, author, task_id=task_id, content=content, **details)
    _mirror(author, kind.replace("_", " "), content, task_id or "global")
    return row


def publish_finding(author: str, task_id: str, finding: str, evidence: str) -> dict:
    return _communication("finding", author, task_id, finding, evidence=evidence)


def report_obstacle(author: str, task_id: str, obstacle: str, evidence: str) -> dict:
    return _communication("obstacle", author, task_id, obstacle, evidence=evidence)


def ask_question(author: str, task_id: str, question: str) -> dict:
    return _communication("question", author, task_id, question)


def answer_question(author: str, question_id: str, answer: str) -> dict:
    state = tasks.load(FORUM_DIR)
    question = next((row for row in state["events"] if row["event_id"] == question_id and row["kind"] == "question"), None)
    if question is None: raise ValueError("unknown question")
    return _communication("answer", author, question["task_id"], answer, question_id=question_id)


def report_phase_result(author: str, status: str, summary: str, evidence: dict | list | str) -> dict:
    """Record complete/blocked preparation work; only optional Architect may skip."""
    _profile("architect", "exploration", "chunking")
    author = _author(author)
    dispatch_id = os.getenv("UNITY_OPTIMIZE_PHASE_DISPATCH_ID", "").strip()
    state = tasks.load(FORUM_DIR)
    dispatch = state.get("phase_dispatches", {}).get(dispatch_id)
    if not dispatch_id or not dispatch or dispatch.get("profile") != PROFILE:
        raise ValueError("phase result requires this worker's current bound phase dispatch")
    return tasks.record_phase_result(FORUM_DIR, author, dispatch_id, status, summary, evidence)


def propose_chunk_plan(author: str, chunks: list[dict], reason: str, exclusions: dict | None = None,
                       exclude_outside_paths: list[str] | None = None, exclusion_reason: str = "",
                       inventory_sha256: str = "") -> dict:
    """Propose a full dependency DAG with evidenced groups, not only error chunks."""
    _profile("chunking")
    author = _author(author)
    try:
        return tasks.propose_plan(FORUM_DIR, author, chunks, reason, exclusions,
            exclude_outside_paths=exclude_outside_paths, exclusion_reason=exclusion_reason,
            inventory_sha256=inventory_sha256)
    except tasks.PlanValidationError as exc:
        state = tasks.load(FORUM_DIR)
        # Store normalized validator facts, not the submitted free text or raw
        # provider/tool diagnostics. Rejected plans do not mutate workflow state.
        report = {"schema": "optimize-plan-validation-v1", "run_id": state["run_id"],
                  "inventory_sha256": tasks.inventory_identity(state), "author": author,
                  "phase": PROFILE, "phase_dispatch_id": os.getenv("UNITY_OPTIMIZE_PHASE_DISPATCH_ID", ""),
                  "code": exc.code, "details": exc.details}
        record = artifacts.store_text(_root() / ".unity" / "artifacts",
            json.dumps(report, sort_keys=True), kind="optimize_plan_validation", producer=author)
        raise ValueError(_summary_text(str(exc), 1000) + "; "
                         + _summary_text(json.dumps(exc.details, sort_keys=True), 1800)
                         + "; use optimize_inventory for canonical IDs. "
                         "Validation details: " + record["artifact_id"]) from exc


def submit_baseline_measurement(author: str, chunk_id: str, measurement: dict) -> dict:
    _profile("chunking")
    return tasks.record_baseline_measurement(FORUM_DIR, _author(author), chunk_id, measurement)


def record_measurement(author: str, chunk_id: str, raw_value: int | float | str,
                       method: dict, evidence: dict | list | str, evidence_kind: str = "agent_judged",
                       baseline: bool = False, assignment_id: str = "", units: str | None = None,
                       comparison_judgment: dict | None = None) -> dict:
    """Bind evidenced measured/judged values to baseline or actual worker bytes.

    This records supplied evidence; it does not invent or execute a scorer.
    Commit a candidate before measuring if its method includes the Git commit.
    """
    from .. import optimize_metrics, optimize_verification
    author = _author(author)
    _judgment(comparison_judgment)
    state = tasks.load(FORUM_DIR)
    if chunk_id not in state["chunks"]: raise ValueError("unknown measurement chunk")
    if baseline:
        _profile("chunking")
        identity = state["baseline"].get("source_identity", state["baseline"].get("identity"))
    else:
        _profile("optimizing")
        assignment = _assignment(author, assignment_id)
        if chunk_id not in state["tasks"][assignment["task_id"]]["chunk_ids"]:
            raise ValueError("measurement chunk not assigned")
        identity = optimize_verification.file_identity(Path(assignment["worktree"]), scope=state["scope"],
                                                       lake_args=state["config"].get("lake_args", []))
    measurement = optimize_metrics.record_measurement(state["metric"], subject=chunk_id,
        raw_value=raw_value, source_identity=identity, method=method, evidence=evidence,
        evidence_kind=evidence_kind, units=units)
    if baseline:
        return tasks.record_baseline_measurement(FORUM_DIR, author, chunk_id, measurement)
    with tasks.transaction(FORUM_DIR) as current:
        row = tasks._owned(current, author, assignment_id)
        row.setdefault("measurements", {})[chunk_id] = deepcopy(measurement)
        if comparison_judgment is not None:
            row.setdefault("judgments", {})[chunk_id] = deepcopy(comparison_judgment)
    return measurement


def measure_candidate(author: str, assignment_id: str, chunk_id: str, raw_value: int | float | str,
                      method: dict, evidence: dict | list | str, evidence_kind: str = "agent_judged",
                      units: str | None = None, comparison_judgment: dict | None = None) -> dict:
    """Record a candidate score against this assigned private tree's actual bytes."""
    return record_measurement(author, chunk_id, raw_value, method, evidence, evidence_kind,
                              assignment_id=assignment_id, units=units, comparison_judgment=comparison_judgment)


def _judgment(judgment: dict | None) -> None:
    if judgment is not None and (not isinstance(judgment.get("improved"), bool)
                                or not str(judgment.get("reason", "")).strip()
                                or ("regressed" in judgment and not isinstance(judgment["regressed"], bool))):
        raise ValueError("qualitative comparison judgment needs improved boolean and concrete reason")


def record_current_measurement(author: str, chunk_id: str, raw_value: int | float | str,
                               method: dict, evidence: dict | list | str,
                               evidence_kind: str = "agent_judged", units: str | None = None,
                               comparison_judgment: dict | None = None) -> dict:
    """Score current integrated source without changing the immutable baseline."""
    from .. import optimize_metrics, optimize_verification
    _profile("optimizing")
    author = _author(author)
    _judgment(comparison_judgment)
    with tasks.transaction(FORUM_DIR) as state:
        if chunk_id not in state["chunks"]: raise ValueError("unknown measurement chunk")
        identity = optimize_verification.file_identity(_root(), scope=state["scope"],
                                                       lake_args=state["config"].get("lake_args", []))
        if identity.get("main_sha") != state["main_sha"]:
            raise ValueError("current integrated source changed; measurement is stale")
        previous = state["baseline"].get("measurements", {}).get(chunk_id)
        if previous is None: raise ValueError("chunk has no original baseline measurement")
        measurement = optimize_metrics.record_measurement(state["metric"], subject=chunk_id,
            raw_value=raw_value, source_identity=identity, method=method, evidence=evidence,
            evidence_kind=evidence_kind, units=units)
        # Comparison validates method, units, metric and subject without inventing
        # an improvement judgment for custom qualitative values.
        for key in ("subject", "metric_sha256", "method_sha256", "units", "direction"):
            if previous.get(key) != measurement.get(key):
                raise ValueError("current measurement differs from the original measurement method")
        if optimize_verification.file_identity(_root(), scope=state["scope"],
                                               lake_args=state["config"].get("lake_args", [])) != identity:
            raise ValueError("integrated source changed while recording the measurement")
        state["measurements"][chunk_id] = deepcopy(measurement)
        if comparison_judgment is not None:
            state.setdefault("judgments", {})[chunk_id] = deepcopy(comparison_judgment)
        tasks._event(state, "current_measurement", author, chunk_id=chunk_id)
    return measurement


def register_strategy(author: str, task_id: str, title: str, description: str) -> dict:
    _profile("optimizing")
    author, task_id = _author(author), _task_id(task_id)
    row = tasks.register_strategy(FORUM_DIR, author, task_id, title, description)
    _mirror(author, title, description, task_id)
    return row


def claim_strategy(author: str, strategy_id: str) -> dict:
    """Claim a distinct strategy and bind this dispatched private worktree."""
    _profile("optimizing")
    author = _author(author)
    row = tasks.load(FORUM_DIR)["strategies"][strategy_id]
    _task_id(row["task_id"])
    bound = os.getenv("UNITY_OPTIMIZE_STRATEGY_ID", "").strip()
    if bound and bound != strategy_id: raise ValueError("strategy does not match bound worker")
    worktree, base = os.getenv("UNITY_OPTIMIZE_WORKTREE", "").strip(), os.getenv("UNITY_OPTIMIZE_BASE_SHA", "").strip()
    if not worktree or not base: raise ValueError("strategy claim requires a controller-bound private worktree and base")
    tasks.claim_strategy(FORUM_DIR, author, strategy_id)
    return tasks.configure_assignment(FORUM_DIR, author, strategy_id, Path(worktree), base)


def assist_strategy(author: str, strategy_id: str, contribution: str) -> dict:
    _profile("optimizing", "exploration", "chunking")
    author = _author(author)
    if not contribution.strip(): raise ValueError("assistance needs a concrete contribution")
    with tasks.transaction(FORUM_DIR) as state:
        row = state["strategies"][strategy_id]
        if author not in row["assistants"]: row["assistants"].append(author)
        tasks._event(state, "assistance", author, strategy_id=strategy_id, contribution=contribution)
        result = deepcopy(row)
    _mirror(author, "Assistance: " + row["title"], contribution, row["task_id"])
    return result


def submit_candidate(author: str, assignment_id: str, notes: str, measurements: dict | None = None) -> dict:
    """Submit a private-tree proposal; controller collects and checks actual Git evidence."""
    _profile("optimizing")
    author = _author(author)
    assignment = _assignment(author, assignment_id)
    selected = measurements if measurements is not None else assignment.get("measurements", {})
    candidate = tasks.submit_candidate(FORUM_DIR, author, assignment_id, notes, selected,
                                      judgments=assignment.get("judgments", {}))
    state = tasks.load(FORUM_DIR)
    return _bounded_response(state, "submitted_candidate", _candidate_summary(state, candidate))


def endorse_candidate(author: str, candidate_id: str, reason: str, evidence: str = "") -> dict:
    _profile("optimizing")
    return tasks.candidate_opinion(FORUM_DIR, _author(author), candidate_id, endorse=True, reason=reason, evidence=evidence)


def object_candidate(author: str, candidate_id: str, reason: str, evidence: str = "") -> dict:
    _profile("optimizing")
    return tasks.candidate_opinion(FORUM_DIR, _author(author), candidate_id, endorse=False, reason=reason, evidence=evidence)


def select_candidate(author: str, candidate_id: str, reason: str) -> dict:
    _profile("optimizing")
    candidate = tasks.select_candidate(FORUM_DIR, _author(author), candidate_id, reason)
    state = tasks.load(FORUM_DIR)
    return _bounded_response(state, "selected_candidate", _candidate_summary(state, candidate))


def request_scope_group(author: str, task_id: str, chunk_ids: list[str], files: list[str],
                        reason: str, evidence: str) -> dict:
    _profile("chunking", "optimizing")
    return tasks.request_scope(FORUM_DIR, _author(author), _task_id(task_id), chunk_ids, files, reason, evidence)


def record_no_improvement(author: str, assignment_id: str, reason: str, evidence: str) -> dict:
    _profile("optimizing")
    author = _author(author)
    _assignment(author, assignment_id)
    return tasks.record_no_improvement(FORUM_DIR, author, assignment_id, reason, evidence)


def record_workflow_change(author: str, original: str, problem: str, evidence: str,
                           revised: str, result: str) -> dict:
    return tasks.record_workflow_change(FORUM_DIR, _author(author), original=original, problem=problem,
                                       evidence=evidence, revised=revised, result=result)


def _review(state: dict, author: str, snapshot_id: str) -> tuple[dict, dict]:
    tasks._primary(state, author)
    review = state["review"]
    snapshot = review.get("snapshot", {})
    if review.get("reviewer") != author: raise ValueError("review is not assigned to the configured primary")
    if not snapshot_id or snapshot.get("snapshot_id") != snapshot_id: raise ValueError("review snapshot is stale")
    if snapshot.get("main_sha") != state["main_sha"] or snapshot.get("metric_sha256") != state["metric"]["sha256"]:
        raise ValueError("review snapshot does not bind current source and metric")
    if "config_revision" in snapshot and snapshot["config_revision"] != state["config"]["revision"]:
        raise ValueError("review snapshot configuration is stale")
    if snapshot.get("sha256") != tasks.digest({key: value for key, value in snapshot.items() if key != "sha256"}):
        raise ValueError("review snapshot evidence is not intact")
    return review, snapshot


def request_review_repair(author: str, snapshot_id: str, task_ids: list[str], reason: str) -> dict:
    """Record precise final-review fixes; no source edits, extra role or acceptance."""
    _profile("critic")
    author = _author(author)
    if not task_ids or not reason.strip(): raise ValueError("review repair needs precise tasks and reason")
    with tasks.transaction(FORUM_DIR) as state:
        review, _ = _review(state, author, snapshot_id)
        if not set(task_ids) <= state["tasks"].keys(): raise ValueError("unknown review task")
        row = {"task_ids": sorted(set(task_ids)), "reason": reason, "author": author, "at": tasks.now()}
        review.setdefault("repair_requests", []).append(row)
        tasks._event(state, "review_repair_requested", author, snapshot_id=snapshot_id,
                     task_ids=row["task_ids"], reason=reason)
    return row


def submit_optimization_verdict(author: str, snapshot_id: str, approved: bool, reasoning: str,
                                 reopen_tasks: list[str] | None = None,
                                 no_improvement_found: bool = False) -> dict:
    _profile("critic")
    author = _author(author)
    reopen = sorted(set(reopen_tasks or []))
    if not reasoning.strip(): raise ValueError("primary review reasoning required")
    if approved and reopen: raise ValueError("approved review cannot reopen tasks")
    if no_improvement_found and (approved or reopen):
        raise ValueError("no-improvement outcome is neither approval nor a repair request")
    with tasks.transaction(FORUM_DIR) as state:
        review, snapshot = _review(state, author, snapshot_id)
        if not set(reopen) <= state["tasks"].keys(): raise ValueError("unknown reopen task")
        requested = {tid for row in review.get("repair_requests", []) for tid in row["task_ids"]}
        if requested and (approved or not requested <= set(reopen)):
            raise ValueError("all requested repairs must appear in the rejection verdict")
        if approved and snapshot.get("passed") is not True:
            raise ValueError("approval requires passed current evidence")
        if no_improvement_found:
            if any(task["status"] not in {"no_improvement", "grouped"} for task in state["tasks"].values()):
                raise ValueError("no-improvement outcome requires closed, evidenced unsuccessful strategies")
            if ({issue.get("kind") for issue in snapshot.get("issues", [])} != {"no_improvement_found"}
                    or any(row.get("improved") or row.get("regressed") for row in snapshot.get("comparisons", {}).values())):
                raise ValueError("no-improvement outcome requires current comparable non-regressing evidence")
        if not approved and not reopen and not no_improvement_found:
            raise ValueError("rejection must identify precise tasks to revisit")
        verdict = {"author": author, "approved": approved,
                   "verdict": "no_improvement" if no_improvement_found else "approved" if approved else "rejected",
                   "snapshot_id": snapshot_id, "snapshot_sha256": snapshot["sha256"], "reasoning": reasoning,
                   "reopen_tasks": reopen, "reopen_task_ids": reopen, "at": tasks.now()}
        review["verdict"] = verdict
        tasks._event(state, "optimization_verdict", author, snapshot_id=snapshot_id, approved=approved)
    return deepcopy(verdict)


COMMON = (optimize_status, optimize_brief, optimize_task, optimize_inventory, forum_post, forum_read, artifact_info, artifact_read, publish_finding,
          report_obstacle, ask_question, answer_question, record_workflow_change)
WORK = (register_strategy, claim_strategy, assist_strategy, record_measurement, measure_candidate,
        record_current_measurement, submit_candidate,
        endorse_candidate, object_candidate, select_candidate, request_scope_group, record_no_improvement)
PROFILE_TOOLS = {
    "preparation": COMMON,
    "architect": COMMON + (report_phase_result,), "exploration": COMMON + (report_phase_result,),
    "chunking": COMMON + (report_phase_result, propose_chunk_plan, submit_baseline_measurement, record_measurement, request_scope_group),
    "optimizing": COMMON + WORK,
    "critic": COMMON + (request_review_repair, submit_optimization_verdict),
    "retrospective": COMMON,
}


def build_server(profile: str) -> FastMCP:
    if profile not in PROFILES: raise ValueError("unknown Optimize forum profile")
    server = FastMCP("unity-optimize-forum-" + profile)
    for function in PROFILE_TOOLS[profile]: server.tool(function)
    return server


def run(forum_dir: Path, project_root: Path, profile: str, author: str = "") -> None:
    configure(forum_dir, project_root, profile, author)
    build_server(profile).run()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forum-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--author", default="")
    args = parser.parse_args()
    run(args.forum_dir, args.project_root, args.profile, args.author)


if __name__ == "__main__":
    main()
