"""Phase-scoped Forum MCP server for ``unity solve``.

The server has a different tool surface from the prove Forum so additions needed
for informal mathematics never enlarge the prove agents' schema.  Every profile
uses the same ``solve-state.json`` and discussion directory.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from fastmcp import FastMCP

from .. import artifacts, solve_state, worktree
from ..solve_review import SemanticReview
from . import server as discussion


FORUM_DIR = Path("forum")
PROJECT_ROOT: Path | None = None
PROFILE = "solving"
PROFILES = {"solving", "solution_review", "chunking", "formalizing", "critic", "retrospective"}


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
    return value


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


def _submit_formal_commit(
    strategy_id: str,
    author: str,
    task_id: str,
    commit_sha: str,
    *,
    notes: str = "",
    supersedes: str = "",
) -> dict:
    resolved = worktree.verify_candidate_commit(_root(), author, commit_sha)
    parent = _git(_root(), "rev-parse", f"{resolved}^").stdout.strip()
    diff = _git(_root(), "show", "--format=", "--binary", resolved).stdout
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()
    result = solve_state.submit_formal_candidate(
        FORUM_DIR, strategy_id, author, task_id, resolved, parent, diff_sha,
        notes=notes, supersedes=supersedes,
    )
    if result["status"] == "submitted":
        candidate = result["candidate"]
        _mirror(author, f"FORMAL CANDIDATE {candidate['candidate_id']}",
                f"task {task_id}, commit {resolved}, diff SHA-256 {diff_sha}", task_id)
    return result


def solve_status() -> dict:
    """Return exact authoritative state for the current solve run."""
    return solve_state.load_state(FORUM_DIR)


def solve_metrics() -> dict:
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


def solve_brief(author: str) -> str:
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
            lines.append(f"Frozen specification artifact: {formal['contract']['artifact_id']} (artifact_read)")
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


def assist_strategy(strategy_id: str, author: str, contribution: str = "") -> dict:
    """Join a claimed strategy with a distinct supporting contribution."""
    return solve_state.assist_strategy(FORUM_DIR, strategy_id, _author(author), contribution)


def unclaim_strategy(strategy_id: str, author: str, reason: str = "") -> dict:
    """Release an owned strategy that may remain viable."""
    return solve_state.release_strategy(
        FORUM_DIR, strategy_id, _author(author), reason=reason, incorrect=False,
    )


def mark_strategy_incorrect(strategy_id: str, author: str, reason: str) -> dict:
    """Close an owned strategy after establishing why it cannot work."""
    return solve_state.release_strategy(
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


def emit_formalization_candidate(
    strategy_id: str,
    author: str,
    task_id: str,
    commit_sha: str,
    notes: str = "",
    supersedes: str = "",
) -> dict:
    """Compatibility API for submitting an already-committed implementation."""
    author = _author(author)
    return _submit_formal_commit(
        strategy_id, author, task_id, commit_sha,
        notes=notes, supersedes=supersedes,
    )


def finalize_formalization(
    strategy_id: str,
    author: str,
    task_id: str,
    changed_paths: list[str] | None = None,
    notes: str = "",
    supersedes: str = "",
) -> dict:
    """Commit current worktree bytes and submit one immutable formal candidate.

    This is deliberately not a build assertion.  The solve controller applies
    the exact resulting commit to main and performs the sole authoritative full
    build and declaration review there.
    """
    author = _author(author)
    with _finalization_lock(author):
        state = solve_state.load_state(FORUM_DIR)
        task = state.get("formal_tasks", {}).get(task_id)
        if state.get("phase") != "formalizing" or not task:
            raise ValueError("formalization task is unavailable")
        if task.get("status") != "pending":
            raise ValueError(f"formalization task is {task.get('status')}, not finalizable")
        strategy = state.get("strategies", {}).get(strategy_id)
        if (
            not strategy
            or strategy.get("phase") != "formalizing"
            or strategy.get("target") != task_id
            or author not in {strategy.get("owner"), *strategy.get("assistants", [])}
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
            if expected_file and expected_file not in staged:
                _git(tree, "reset", check=False)
                raise ValueError(
                    f"candidate does not change the target file '{expected_file}'"
                )
            staged_diff = _git(tree, "diff", "--cached", "--no-ext-diff").stdout
            forbidden = sorted({
                match.group(1)
                for line in staged_diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
                for match in re.finditer(
                    r"\b(sorry|admit|axiom|native_decide)\b",
                    line[1:].split("--", 1)[0],
                )
            })
            if forbidden:
                _git(tree, "reset", check=False)
                raise ValueError(
                    "candidate adds forbidden construct(s): " + ", ".join(forbidden)
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
            notes=notes, supersedes=supersedes,
        )
        return {
            **result,
            "committed": committed,
            "changed_paths": staged,
            "commit_sha": head,
        }


def sync_from_main(author: str, reason: str = "") -> dict:
    """Discard obsolete worktree changes and synchronize exactly to accepted main."""
    author = _author(author)
    released = solve_state.release_author_claims(FORUM_DIR, author, reason or "syncing from main")
    result = worktree.force_sync_from_main(_root(), author)
    return {**result, "released_strategies": released}


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


def request_rechunk(author: str, reason: str) -> dict:
    """Rebuild a wrong encoding/signature contract while keeping the accepted paper.

    Use this when a frozen declaration or encoding is wrong. Use reopen_solving
    instead when the accepted mathematics is substantively wrong.
    """
    author = _author(author)
    with _merge_lock():
        result = solve_state.request_rechunk(FORUM_DIR, author, reason)
    _mirror(author, "FORMALIZATION CONTRACT REOPENED", reason)
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

    Read solve_status() for the current snapshot_id and immutable requirements.
    Free-text evidence is optional context, never a substitute for structured review.
    Approval remains pending until the controller verifies that source bytes are unchanged.
    """
    author = _author(author)
    result = solve_state.submit_critic_verdict(
        FORUM_DIR, author, verdict, summary,
        review=SemanticReview.model_validate(review).model_dump(),
        reopen_tasks=reopen_tasks, evidence=evidence,
    )
    _mirror(author, f"FORMALIZATION VERDICT: {verdict}", summary)
    return result


COMMON: tuple[Callable, ...] = (
    solve_status, solve_metrics, solve_brief, forum_post, forum_read, artifact_info, artifact_read,
)
COORDINATION: tuple[Callable, ...] = (
    register_strategy, claim_strategy, assist_strategy, unclaim_strategy,
    mark_strategy_incorrect, publish_finding, report_obstacle, ask_question, answer_question,
)
PROFILE_TOOLS: dict[str, tuple[Callable, ...]] = {
    "solving": COMMON + COORDINATION + (
        create_subgoal, create_informal_task, emit_informal_result,
        review_informal_result, emit_solution_candidate,
    ),
    "solution_review": COMMON + (publish_finding, ask_question, answer_question,
                                  review_solution_candidate),
    "chunking": COMMON,
    "formalizing": COMMON + COORDINATION + (
        finalize_formalization, emit_formalization_candidate,
        sync_from_main, propose_source_fix, reopen_solving, request_rechunk,
    ),
    "critic": COMMON + (propose_source_fix, reopen_solving, request_rechunk, submit_formalization_verdict),
    "retrospective": COMMON,
}


def build_server(profile: str) -> FastMCP:
    if profile not in PROFILES:
        raise ValueError(f"unknown solve Forum profile '{profile}'")
    server = FastMCP(f"unity-solve-forum-{profile}")
    for tool in PROFILE_TOOLS[profile]:
        server.tool()(tool)
    return server


def run(forum_dir: Path, project_root: Path, profile: str) -> None:
    configure(forum_dir, project_root, profile)
    build_server(profile).run()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forum-dir", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    args = parser.parse_args()
    run(Path(args.forum_dir), Path(args.project_root), args.profile)


if __name__ == "__main__":
    main()
