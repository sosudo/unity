"""Independent, phase-scoped Forum MCP interface for ``unity autoformalize``.

Supplied-source, strategy, candidate and review state lives in this pipeline's
own forum directory. Neither solve nor prove imports this interface.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

from .. import artifacts, autoformalize_state, worktree
from ..autoformalize_review import SemanticReview
from ..autoformalize_spec import normalize_outputs
from . import server as discussion


FORUM_DIR = Path("forum")
PROJECT_ROOT: Path | None = None
PROFILE = "chunking"
PROFILES = {"chunking", "formalizing", "critic", "retrospective", "source_repair"}


def configure(forum_dir: Path, project_root: Path, profile: str = "chunking") -> None:
    global FORUM_DIR, PROJECT_ROOT, PROFILE
    if profile not in PROFILES:
        raise ValueError(f"unknown autoformalize Forum profile '{profile}'")
    FORUM_DIR = Path(forum_dir)
    PROJECT_ROOT = Path(project_root).resolve()
    PROFILE = profile
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    discussion.FORUM_DIR = FORUM_DIR
    discussion.PROJECT_ROOT = PROJECT_ROOT
    discussion.ICRL_ENABLED = False


def _root() -> Path:
    if PROJECT_ROOT is None:
        raise ValueError("autoformalize Forum requires a configured project root")
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
    return "autoformalize-" + (key or "global")


def _ensure_thread(thread_id: str) -> str:
    tid = _thread_id(thread_id)
    discussion.forum_create_thread(tid, f"Autoformalize: {thread_id or 'Global'}")
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
    path = FORUM_DIR / f"autoformalize-finalize-{safe}.lock"
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
) -> dict:
    resolved = worktree.verify_candidate_commit(
        _root(), author, commit_sha, allow_unchanged=True,
    )
    base = _git(_root(), "merge-base", resolved, worktree.main_commit(_root())).stdout.strip()
    diff = _git(
        _root(), "diff", "--no-ext-diff", "--no-textconv",
        "--binary", "--full-index", base, resolved,
    ).stdout
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()
    result = autoformalize_state.submit_formal_candidate(
        FORUM_DIR, strategy_id, author, task_id, resolved, base, diff_sha,
        notes=notes, supersedes=supersedes, stage=stage, outputs=outputs,
    )
    if result["status"] == "submitted":
        candidate = result["candidate"]
        _mirror(author, f"FORMAL CANDIDATE {candidate['candidate_id']}",
                f"task {task_id}, commit {resolved}, diff SHA-256 {diff_sha}", task_id)
    return result


def autoformalize_status() -> dict:
    """Return exact authoritative state for the current autoformalize run."""
    return autoformalize_state.load_state(FORUM_DIR)


def validate_chunks() -> dict:
    """Check dag.json bookkeeping without builds, adoption or faithfulness approval."""
    from ..config import Paths
    from ..autoformalize_input import autoformalize_paths, require_source_matches
    from ..autoformalize_runtime import validate_formalization_dag

    try:
        state = autoformalize_state.load_state(FORUM_DIR)
        source = autoformalize_state.formal_source(state)
        if state.get("phase") != "chunking" or not source:
            raise ValueError("validate_chunks is only available during chunking")
        paths = autoformalize_paths(Paths.from_unity_dir(_root() / ".unity"))
        require_source_matches(paths, state)
        dag = validate_formalization_dag(paths, source["sha256"])
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]}
    return {"ok": True, "chunk_count": len(dag["chunks"])}


def read_metrics(forum_dir: Path, project_root: Path) -> dict:
    """Read one workspace's telemetry without changing process-global tool routing."""
    state = autoformalize_state.load_state(forum_dir)
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
            if context.get("command") == "autoformalize" and context.get("run_id") == state.get("run_id"):
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
    started = first_by_kind.get("supplied_source_bound", 0)
    submitted = first_by_kind.get("formal_candidate_submitted", 0)
    accepted = first_by_kind.get("formal_candidate_merged", 0)
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


def autoformalize_metrics() -> dict:
    """Return compact run-scoped timing, worker, token, and cost telemetry."""
    return read_metrics(FORUM_DIR, _root())


def _task_focus(state: dict, author: str, task_id: str = "") -> tuple[set[str], set[str]]:
    """Keep direct assignments distinct from their supporting prerequisites."""
    tasks = state.get("formal_tasks", {})
    assigned = task_id or os.getenv("UNITY_AUTOFORMALIZE_TASK_ID", "")
    if assigned and assigned not in tasks:
        raise ValueError(f"unknown task '{assigned}'")
    focus = {assigned} if assigned else {
        item["target"] for item in state.get("strategies", {}).values()
        if item.get("target") in tasks and item.get("status") in {"claimed", "paused"}
        and autoformalize_state.participates(item, author)
        and autoformalize_state.strategy_is_current(state, item)
    }
    related = set(focus)
    while True:
        dependencies = {
            dependency for target in related
            for dependency in tasks[target].get("dependencies", [])
            if dependency in tasks
        }
        if dependencies <= related:
            return focus, related
        related |= dependencies


def _spec(state: dict) -> dict:
    formal = state.get("formalization", {})
    return formal.get("spec") or (formal.get("contract") or {}).get("spec") or {}


def _detail(payload: dict, source: str) -> str:
    compacted = artifacts.compact_text(
        _artifacts_dir(), json.dumps(payload, sort_keys=True),
        kind="autoformalize_detail", producer="Unity", source=source,
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


def autoformalize_requirements(offset: int = 0, limit: int = 20) -> str:
    """Read the complete global coverage ledger in stable-ID-sorted pages.

    Continue with next_offset until null. Task-filtered views never establish
    full source coverage. Large pages are stored as exact readable artifacts.
    """
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be from 1 through 100")
    state = autoformalize_state.load_state(FORUM_DIR)
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
        **_requirement_spec(state, rows[offset:end]),
    }, "requirements")


def autoformalize_task(task_id: str) -> str:
    """Read one task's exact requirements, source citations and current evidence."""
    state = autoformalize_state.load_state(FORUM_DIR)
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
    return _detail({
        "run_id": state.get("run_id"), "revision": state.get("revision"),
        "contract_sha256": (state["formalization"].get("contract") or {}).get("sha256"),
        "task": task, "assignment": autoformalize_state.assignment_view(state, task_id),
        "requirements": requirements,
        "source_refs": [
            ref for ref in (state.get("input_source") or {}).get("source_refs", [])
            if ref["ref_id"] in source_ids
        ],
        **spec,
        "dependencies": [tasks.get(dep, state.get("retired_tasks", {}).get(dep, {"task_id": dep}))
                         for dep in task.get("dependencies", [])],
        "candidates": [
            item for item in state.get("formal_candidates", {}).values()
            if item.get("task_id") == task_id
        ],
        "source_issues": [
            item for item in state.get("source_issues", {}).values()
            if not item.get("task_ids") or task_id in item["task_ids"]
        ],
        "findings": [
            item for item in state.get("findings", {}).values()
            if item.get("status") == "active" and item.get("target") in {"", task_id}
        ],
    }, task_id)


def autoformalize_brief(author: str, task_id: str = "") -> str:
    """Return bounded task-focused state; global review uses the paged ledger."""
    author = _author(author)
    state = autoformalize_state.load_state(FORUM_DIR)
    formal = state["formalization"]
    source = state.get("input_source") or {}
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
        f"AUTOFORMALIZE RUN {state.get('run_id') or 'uninitialized'}",
        f"Phase: {state.get('phase', 'chunking')}; state revision: {state.get('revision', 0)}",
        f"Problem SHA-256: {state.get('problem_sha256') or 'unavailable'}",
        f"Supplied source snapshot: {source.get('candidate_id')}; SHA-256: {source.get('sha256')}",
        f"Formalization gate: {formal.get('status')} (revision {formal.get('revision')})",
        f"Global source issues not resolved: {len(issues)}; "
        f"pending replan requests: {len(queued)}",
    ]
    if focus:
        lines.extend(["", "YOUR ASSIGNED/CLAIMED TASKS"])
        for target in sorted(focus):
            task = tasks[target]
            lines.append(f"- {target} [{task.get('status')}]: {task.get('title') or task.get('lean_decl')} "
                         f"{task.get('description', '')[:220]}")
        lines.append("Exact requirements, source citations and evidence: autoformalize_task(task_id).")
    candidates = [
        item for item in state.get("formal_candidates", {}).values()
        if autoformalize_state.candidate_is_current(state, item)
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
        lines.append("Explore gaps and submit a source repair proposal; original documents stay unchanged.")
    for item in queued[:3]:
        lines.append(f"Queued replan: {item.get('reason', '')[:200]}")
    obstacles = [
        item for item in state.get("obstacles", {}).values()
        if item.get("status") == "open" and (not related or item.get("target") in related | {"", None})
    ]
    if obstacles:
        lines.extend(["", "OPEN OBSTACLES"])
        for item in obstacles[-5:]:
            lines.append(f"- {item['obstacle_id']} task={item.get('target') or 'global'}: "
                         f"{item.get('goal_state', '')[:250]}")
    snapshot = formal.get("review_snapshot")
    if snapshot:
        lines.extend([
            f"Machine snapshot: {snapshot.get('snapshot_id')} "
            f"({'passed' if snapshot.get('passed') else 'failed'}); main {snapshot.get('main_sha')}",
            f"Machine evidence artifact: {snapshot.get('artifact_id')}; "
            "scaffold axiom lists are historical, not current verification.",
        ])
    requirements = formal.get("requirements", [])
    lines.extend([
        "", f"GLOBAL COVERAGE LEDGER: {len(requirements)} requirements",
        "Read autoformalize_requirements(offset=0) and follow next_offset to null for ALL requirements. "
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
    lines.extend(["", "SUPPLIED SOURCE REFERENCES"])
    for ref in relevant_refs[:6]:
        lines.append(f"- {ref.get('ref_id')}: {str(ref.get('path', ''))[:140]} "
                     f"artifact {ref.get('artifact_id')} SHA-256 {ref.get('sha256')}")
    if len(relevant_refs) > 6:
        lines.append(f"- {len(relevant_refs) - 6} more references in the detail tools.")
    owned = [
        item for item in state.get("strategies", {}).values()
        if item.get("status") in {"claimed", "paused"}
        and autoformalize_state.strategy_is_current(state, item)
        and autoformalize_state.participates(item, author)
    ]
    if owned:
        lines.extend(["", "YOUR CLAIMED/ASSISTED STRATEGIES"])
        for item in owned[:6]:
            lines.append(f"- {item['strategy_id']} [{item['status']}] task={item.get('target')}: "
                         f"{item.get('description', '')[:200]}")
    visible_tasks = [tasks[target] for target in sorted(related)] if related else list(tasks.values())
    if visible_tasks:
        lines.extend(["", "RELEVANT TASK STATUS" if related else "TASK PREVIEW"])
        for task in visible_tasks[:10]:
            assignment = autoformalize_state.assignment_view(state, task['task_id'])
            lines.append(f"- {task['task_id']} [{task['status']}]: {task.get('title') or task.get('lean_decl')}; "
                         f"representation={task.get('representation', {}).get('status', 'legacy')}, "
                         f"verification={task.get('verification', {}).get('status', 'pending')}, "
                         f"faithfulness={task.get('faithfulness', {}).get('status', 'unreviewed')}; "
                         f"owners={','.join(assignment['owners'])}; "
                         f"statement deps={','.join(task.get('statement_dependencies', []))}; "
                         f"proof deps={','.join(task.get('proof_dependencies', task.get('dependencies', [])))}")
    findings = [
        item for item in state.get("findings", {}).values()
        if item.get("status") == "active" and (not related or item.get("target") in related | {"", None})
    ]
    findings.sort(key=lambda item: (item.get("confidence", 0), item.get("created_at", 0)), reverse=True)
    if findings:
        lines.extend(["", "LIVE FINDINGS"])
        for item in findings[:6]:
            lines.append(f"- {item['finding_id']} ({item.get('confidence')}/100) "
                         f"{item.get('title')}: {item.get('content', '')[:300]}")
            if item.get("evidence"):
                lines.append(f"  evidence: {item['evidence'][:180]}")
    questions = [
        item for item in state.get("questions", {}).values()
        if item.get("status") == "open" and (
            not item.get("to") or autoformalize_state.author_key(item.get("to"))
            == autoformalize_state.author_key(author)
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
        lines.extend(["", "LATEST FORMALIZATION VERDICT",
                      f"- {verdict.get('verdict')} by {verdict.get('author')}: "
                      f"{verdict.get('summary', '')[:350]}"])
    if state.get("final_report"):
        lines.append(f"Run report: artifact {state['final_report'].get('artifact_id')}")
    text = "\n".join(lines)
    try:
        limit = max(2_000, min(32_000, int(os.getenv("UNITY_AUTOFORMALIZE_BRIEF_CHARS", "12000"))))
    except ValueError:
        limit = 12_000
    suffix = "\n...[brief truncated] Use autoformalize_task or autoformalize_requirements."
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
    return artifacts.read_artifact(_artifacts_dir(), artifact_id, offset=offset, limit=limit)


def register_strategy(
    author: str,
    description: str,
    target: str = "",
    strategy_family: str = "",
    central_claim: str = "",
) -> dict:
    """Register one distinct strategy for the current formalization target."""
    author = _author(author)
    result = autoformalize_state.register_strategy(
        FORUM_DIR, author, description, target=target, family=strategy_family,
        central_claim=central_claim,
    )
    if result["status"] != "duplicate":
        item = result["strategy"]
        _mirror(author, f"STRATEGY {item['strategy_id']}", description, target)
    return result


def claim_strategy(strategy_id: str, author: str) -> dict:
    """Atomically reserve a registered strategy."""
    return autoformalize_state.claim_strategy(FORUM_DIR, strategy_id, _author(author))


def assist_strategy(strategy_id: str, author: str, contribution: str = "") -> dict:
    """Join a claimed strategy with a distinct supporting contribution."""
    return autoformalize_state.assist_strategy(FORUM_DIR, strategy_id, _author(author), contribution)


def unclaim_strategy(strategy_id: str, author: str, reason: str = "") -> dict:
    """Release an owned strategy that may remain viable."""
    return autoformalize_state.release_strategy(
        FORUM_DIR, strategy_id, _author(author), reason=reason, incorrect=False,
    )


def mark_strategy_incorrect(strategy_id: str, author: str, reason: str) -> dict:
    """Close an owned strategy after establishing why it cannot work."""
    return autoformalize_state.release_strategy(
        FORUM_DIR, strategy_id, _author(author), reason=reason, incorrect=True,
    )


def publish_finding(
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
            _artifacts_dir(), evidence, kind="autoformalize_finding_evidence",
            producer=author, source=title,
        )
        evidence = f"artifact {record['artifact_id']} SHA-256 {record['sha256']}"
    result = autoformalize_state.publish_finding(
        FORUM_DIR, author, kind, title, content, confidence,
        target=target, strategy_id=strategy_id, evidence=evidence, supersedes=supersedes,
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
    """Report a concrete blocker visible to every autoformalize worker."""
    author = _author(author)
    result = autoformalize_state.report_obstacle(
        FORUM_DIR, author, goal_state, target=target, tried=tried, hypothesis=hypothesis,
    )
    _mirror(author, f"OBSTACLE {result['obstacle_id']}", goal_state, target)
    return result


def ask_question(author: str, body: str, to: str = "", target: str = "") -> dict:
    """Ask a targeted or global autoformalize question."""
    return autoformalize_state.ask_question(FORUM_DIR, _author(author), body, to=to, target=target)


def answer_question(question_id: str, author: str, body: str) -> dict:
    """Answer an open autoformalize question."""
    return autoformalize_state.answer_question(FORUM_DIR, question_id, _author(author), body)


def emit_formalization_candidate(
    strategy_id: str,
    author: str,
    task_id: str,
    commit_sha: str,
    notes: str = "",
    supersedes: str = "",
    stage: Literal["representation", "complete"] = "complete",
    outputs: list[dict] | None = None,
) -> dict:
    """Compatibility API for submitting an already-committed implementation."""
    author = _author(author)
    with _finalization_lock(author):
        return _submit_formal_commit(
            strategy_id, author, task_id, commit_sha,
            notes=notes, supersedes=supersedes, stage=stage, outputs=outputs,
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
) -> dict:
    """Commit current worktree bytes and submit one immutable formal candidate.

    Unchanged work submits the existing commit for authoritative re-verification.

    This is deliberately not a build assertion.  The autoformalize controller applies
    the exact resulting commit to main and performs the sole authoritative full
    build and declaration review there.
    """
    author = _author(author)
    if stage not in {"representation", "complete"}:
        raise ValueError("candidate stage must be representation or complete")
    outputs = normalize_outputs(outputs) if outputs is not None else None
    with _finalization_lock(author):
        state = autoformalize_state.load_state(FORUM_DIR)
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
            or not autoformalize_state.strategy_is_current(state, strategy)
            or strategy.get("status") != "claimed"
            or not autoformalize_state.participates(strategy, author)
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
                "commit", "-m", f"UNITY: autoformalize candidate for {task_id}",
                check=False,
            )
            if commit.returncode:
                raise ValueError(commit.stderr.strip() or "could not commit formalization")
            committed = True

        head = _git(tree, "rev-parse", "HEAD").stdout.strip()
        result = _submit_formal_commit(
            strategy_id, author, task_id, head,
            notes=notes, supersedes=supersedes, stage=stage, outputs=outputs,
        )
        return {
            **result,
            "committed": committed,
            "changed_paths": staged,
            "commit_sha": head,
        }


def _current_formal_candidate(state: dict, candidate: dict) -> bool:
    return autoformalize_state.candidate_is_current(state, candidate)


def has_pending_formal_candidate(state: dict, author: str = "") -> bool:
    """Whether current-revision candidate bytes must remain on an author's branch."""
    return any(
        _current_formal_candidate(state, candidate)
        and candidate.get("status") in {"submitted", "merging"}
        and (not author or autoformalize_state.author_key(candidate.get("author"))
             == autoformalize_state.author_key(author))
        for candidate in state.get("formal_candidates", {}).values()
    )


def unresolved_formal_tasks(state: dict, author: str) -> list[str]:
    """Current claimed/assisted work and queued candidate targets for an author."""
    targets = {
        strategy.get("target", "")
        for strategy in state.get("strategies", {}).values()
        if strategy.get("phase") == "formalizing"
        and autoformalize_state.strategy_is_current(state, strategy)
        and strategy.get("status") in {"claimed", "paused"}
        and autoformalize_state.participates(strategy, author)
    }
    targets.update(
        candidate.get("task_id", "")
        for candidate in state.get("formal_candidates", {}).values()
        if _current_formal_candidate(state, candidate)
        and candidate.get("status") in {"submitted", "merging"}
        and autoformalize_state.author_key(candidate.get("author")) == autoformalize_state.author_key(author)
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
            or task.get("status") != "pending" or autoformalize_state.task_ready(state, task)
            or autoformalize_state.source_issues_blocking_task(state, task_id)):
        return []
    pending = list(task.get("statement_dependencies", []))
    seen, ready = {task_id}, []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        dependency = tasks.get(key, {})
        if autoformalize_state.interface_available(state, dependency):
            continue
        if autoformalize_state.task_ready(state, dependency):
            ready.append(dependency)
        else:
            pending.extend(dependency.get("statement_dependencies", []))
    return sorted(ready, key=lambda item: item["task_id"])


def prepare_formal_worktree(
    author: str,
    previous_task: str = "",
    next_task: str = "",
    *,
    expected_revision: int | None = None,
) -> dict:
    """Prepare a stopped worker for another task without erasing unresolved work.

    Only an explicitly completed previous assignment authorizes discarding its
    obsolete attempt. This is controller-only, not an agent tool. The state lock
    serializes the final guard with new claims; the author lock protects both
    candidate submission APIs and their immutable commit ancestry.
    """
    author = _author(author)
    with _merge_lock(), _finalization_lock(author), autoformalize_state.transaction(FORUM_DIR) as state:
        formal = state["formalization"]
        if state.get("phase") != "formalizing" or (
            expected_revision is not None and formal.get("revision") != expected_revision
        ):
            return _sync_blocked("phase_changed", "Formalization phase/revision changed; work preserved.")
        target_task = state.get("formal_tasks", {}).get(next_task, {})
        if target_task.get("status") != "pending":
            return _sync_blocked("task_unavailable", "The next formal task is no longer pending.")
        if not autoformalize_state.task_ready(state, target_task):
            return _sync_blocked("dependencies_pending", "The next formal task has unresolved dependencies.")
        if has_pending_formal_candidate(state, author):
            return _sync_blocked("candidate_pending", "Candidate review is pending; its branch is preserved.")
        tree = worktree.agent_worktree(_root(), author)
        if not tree.is_dir():
            return _sync_blocked("missing_worktree", "The agent has no active worktree.")
        retired = state.get("retired_tasks", {}).get(previous_task, {})
        continuing_refinement = next_task in retired.get("replaced_by", [])
        if previous_task == next_task or continuing_refinement:
            result = {"ok": True, "preserved": True, "worktree": str(tree)}
            # Representation-only adoption keeps this task alive. Bring a clean
            # stopped tree onto accepted main so later candidates do not submit
            # its already-integrated representation again. Never erase edits.
            main_sha = _accepted_formal_main(state)
            if ((formal.get("contract") or {}).get("version") == 3 and main_sha
                    and not _git(tree, "status", "--porcelain").stdout.strip()):
                merged = _git(tree, "merge", "--no-edit", "--no-autostash", "--no-overwrite-ignore",
                              main_sha, check=False)
                if merged.returncode:
                    result["sync_warning"] = "Resolve the preserved worktree merge conflict before finalizing."
            return result
        previous = state.get("formal_tasks", {}).get(previous_task, {})
        # A refinement can introduce a missing interface and block every former
        # assignment. Only an unclaimed clean ancestor tree may move upstream;
        # this exception never resets or parks private work.
        prerequisite_reassignment = next_task in {
            item["task_id"] for item in ready_statement_prerequisites(state, previous_task)
        }
        if unresolved_formal_tasks(state, author) or (
            previous_task and previous.get("status") != "complete" and not prerequisite_reassignment
        ):
            return _sync_blocked("unresolved_work", "Unfinished task work is preserved; resume it first.")
        main_sha = _accepted_formal_main(state)
        if not main_sha:
            return _sync_blocked("main_changed", "Main differs from the accepted formalization revision.")
        if previous.get("status") == "complete":
            return worktree.force_sync_from_main(_root(), author)

        # A fresh/unassigned tree has no known obsolete task. Never reset it:
        # accept a clean ancestor of main, but preserve unexplained local work.
        if _git(tree, "status", "--porcelain").stdout.strip():
            return _sync_blocked("dirty_worktree", "Local edits are preserved; reconcile them before changing tasks.")
        if prerequisite_reassignment:
            ignored = _git(tree, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z").stdout
            if any(path.split("/", 1)[0] not in {".unity", ".lake"}
                   for path in ignored.split("\0") if path):
                return _sync_blocked("ignored_work", "Ignored private files are preserved; reconcile them before changing tasks.")
        if _git(tree, "merge-base", "--is-ancestor", "HEAD", main_sha, check=False).returncode:
            return _sync_blocked("local_commits", "Private commits are preserved; reconcile them before changing tasks.")
        merged = _git(tree, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", main_sha, check=False)
        if merged.returncode:
            return _sync_blocked("local_commits", merged.stderr.strip() or "Unassigned commits are preserved.")
        worktree.link_runtime_state(tree, _root())
        worktree.symlink_lake_cache(tree, _root())
        return {"ok": True, "main_sha": main_sha, "worktree": str(tree)}


def sync_from_main(author: str, reason: str = "") -> dict:
    """Merge accepted main without discarding edits, candidate commits, or claims."""
    author = _author(author)
    with _merge_lock(), _finalization_lock(author), autoformalize_state.transaction(FORUM_DIR) as state:
        if state.get("phase") != "formalizing":
            return _sync_blocked("phase_changed", "Synchronization is only available during formalizing.")
        if has_pending_formal_candidate(state, author):
            return _sync_blocked("candidate_pending", "Candidate review is pending; its branch is preserved.")
        tree = worktree.agent_worktree(_root(), author)
        if not tree.is_dir():
            return _sync_blocked("missing_worktree", "The agent has no active worktree.")
        if _git(tree, "status", "--porcelain").stdout.strip():
            return _sync_blocked("dirty_worktree", "Local changes preserved. Commit or resolve them before syncing.")
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
                  changes: autoformalize_state.ChunkRefinement) -> dict:
    """Revise informal nodes with an atomic state-revision check and explicit split/merge lineage.

    Upserts are complete informal node rows. Replacements name old_ids, new_ids,
    and a reason. Source obligations and machine-owned status cannot be edited.
    """
    author = _author(author)
    with _merge_lock():
        result = autoformalize_state.refine_chunks(FORUM_DIR, author, expected_revision, changes)
        if result.get("status") == "conflict":
            return result
        state = autoformalize_state.load_state(FORUM_DIR)
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
    issue = autoformalize_state.report_source_issue(
        FORUM_DIR, author, anchor_ids, description, task_ids=task_ids,
    )
    _mirror(author, f"SOURCE ISSUE {issue['issue_id']}", description)
    return issue


def submit_source_repair(
    author: str, issue_id: str, explanation: str, evidence: str, replacement: str = "",
) -> dict:
    """Propose an evidence-backed local repair; this is not an approval or source rewrite.

    Original files remain immutable. Unity binds the proposal to the source issue
    and requires explicit argument/critic review before accepting the repaired proof.
    """
    author = _author(author)
    state = autoformalize_state.load_state(FORUM_DIR)
    if issue_id not in state.get("source_issues", {}):
        raise ValueError(f"unknown source issue '{issue_id}'")
    explanation = autoformalize_state._text(explanation, "explanation", 16000)
    evidence = autoformalize_state._text(evidence, "evidence", 16000)
    replacement = autoformalize_state._text(replacement, "replacement", 16000, required=False)
    payload = {"author": author, "issue_id": issue_id, "explanation": explanation,
               "evidence": evidence, "replacement": replacement,
               "source_candidate": (state.get("input_source") or {}).get("candidate_id"),
               "source_sha256": (state.get("input_source") or {}).get("sha256")}
    record = artifacts.store_text(
        _artifacts_dir(), json.dumps(payload, sort_keys=True),
        kind="autoformalize_source_repair", producer=author, source=issue_id,
    )
    repair = autoformalize_state.submit_source_repair(
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
    result = autoformalize_state.request_rechunk(FORUM_DIR, author, reason, task_ids=task_ids)
    _mirror(author, "FORMALIZATION REPLAN REQUESTED", reason)
    return result


def submit_formalization_verdict(
    author: str,
    verdict: str,
    summary: str,
    review: SemanticReview,
    reopen_tasks: list[str] | None = None,
    evidence: str = "",
) -> dict:
    """Submit snapshot-bound semantic evidence. Approval requires every requirement to pass.

    Read autoformalize_status() for the current snapshot_id and immutable requirements.
    Free-text evidence is optional context, never a substitute for structured review.
    Approval remains pending until the controller verifies that source bytes are unchanged.
    """
    author = _author(author)
    result = autoformalize_state.submit_critic_verdict(
        FORUM_DIR, author, verdict, summary,
        review=SemanticReview.model_validate(review).model_dump(),
        reopen_tasks=reopen_tasks, evidence=evidence,
    )
    _mirror(author, f"FORMALIZATION VERDICT: {verdict}", summary)
    return result


COMMON = (
    autoformalize_status, autoformalize_metrics, autoformalize_brief,
    autoformalize_task, autoformalize_requirements,
    forum_post, forum_read, artifact_info, artifact_read,
)
COORDINATION = (
    register_strategy, claim_strategy, assist_strategy, unclaim_strategy,
    mark_strategy_incorrect, publish_finding, report_obstacle,
    ask_question, answer_question,
)
SOURCE_FEEDBACK = (publish_finding, report_obstacle, ask_question, answer_question,
                   report_source_issue, submit_source_repair)
PROFILE_TOOLS = {
    "chunking": COMMON + SOURCE_FEEDBACK + (validate_chunks,),
    "formalizing": COMMON + COORDINATION + (
        finalize_formalization, emit_formalization_candidate, sync_from_main, request_rechunk,
        report_source_issue, submit_source_repair, refine_chunks,
    ),
    "critic": COMMON + SOURCE_FEEDBACK + (request_rechunk, submit_formalization_verdict),
    "retrospective": COMMON,
    "source_repair": COMMON + SOURCE_FEEDBACK,
}


def build_server(profile: str) -> FastMCP:
    """Expose only the independent autoformalize tools required by this phase."""
    if profile not in PROFILES:
        raise ValueError(f"Unknown autoformalize MCP profile: {profile}")
    server = FastMCP(f"unity-autoformalize-forum-{profile}")
    for tool in PROFILE_TOOLS[profile]:
        server.tool(tool)
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
