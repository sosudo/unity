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
from contextlib import contextmanager
from pathlib import Path

from fastmcp import FastMCP

from .. import artifacts, autoformalize_state, worktree
from ..autoformalize_review import SemanticReview
from . import server as discussion


FORUM_DIR = Path("forum")
PROJECT_ROOT: Path | None = None
PROFILE = "chunking"
PROFILES = {"chunking", "formalizing", "critic", "retrospective"}


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
) -> dict:
    resolved = worktree.verify_candidate_commit(_root(), author, commit_sha)
    base = _git(_root(), "merge-base", resolved, worktree.main_commit(_root())).stdout.strip()
    diff = _git(
        _root(), "diff", "--no-ext-diff", "--no-textconv",
        "--binary", "--full-index", base, resolved,
    ).stdout
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()
    result = autoformalize_state.submit_formal_candidate(
        FORUM_DIR, strategy_id, author, task_id, resolved, base, diff_sha,
        notes=notes, supersedes=supersedes,
    )
    if result["status"] == "submitted":
        candidate = result["candidate"]
        _mirror(author, f"FORMAL CANDIDATE {candidate['candidate_id']}",
                f"task {task_id}, commit {resolved}, diff SHA-256 {diff_sha}", task_id)
    return result


def autoformalize_status() -> dict:
    """Return exact authoritative state for the current autoformalize run."""
    return autoformalize_state.load_state(FORUM_DIR)


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


def autoformalize_brief(author: str) -> str:
    """Return bounded source, task, candidate, and review state for this run."""
    author = _author(author)
    state = autoformalize_state.load_state(FORUM_DIR)
    formal = state["formalization"]
    source = state.get("input_source") or {}
    refs = source.get("source_refs") or []
    lines = [
        f"AUTOFORMALIZE RUN {state.get('run_id') or 'uninitialized'}",
        f"Phase: {state.get('phase', 'chunking')}",
        f"Problem SHA-256: {state.get('problem_sha256') or 'unavailable'}",
        "Source: user-supplied, immutable; no informal solving gate",
        f"Source snapshot: {source.get('candidate_id')}; SHA-256: {source.get('sha256')}",
        f"Formalization gate: {formal.get('status')} (revision {formal.get('revision')})",
        "SUPPLIED SOURCE REFERENCES (full manifest: autoformalize_status().input_source)",
    ]
    for ref in refs[:8]:
        lines.append(f"- {ref.get('ref_id')}: {str(ref.get('path', ''))[:160]} "
                     f"artifact {ref.get('artifact_id')} SHA-256 {ref.get('sha256')}")
    if len(refs) > 8:
        lines.append(f"- {len(refs) - 8} more source references in the manifest")
    lines.append("Preserve supplied source bytes. Report source defects as obstacles; "
                 "request_rechunk for a wrong Lean encoding, never rewrite the supplied argument.")
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
                      "Full immutable ledger and verdict evidence: autoformalize_status() "
                      "(formalization and critic_verdicts), also "
                      ".unity/forum/autoformalize/autoformalize-state.json."])
        if (formal.get("contract") or {}).get("artifact_id"):
            lines.append(f"Frozen pre-proof specification artifact: {formal['contract']['artifact_id']} "
                         "(artifact_read). Its scaffold proof-axiom lists are historical, not current verification.")
        for requirement in formal["requirements"]:
            lines.append(f"- {requirement['id']} → tasks {', '.join(requirement['tasks'])}: "
                         f"{requirement['statement'][:240]}")
        lines.append("Approve only after checking every requirement against actual Lean statements and definitions.")
    attempts = [item for item in state.get("chunking_attempts", [])
                if item.get("candidate_id") == source.get("candidate_id")]
    if attempts:
        lines.extend(["", "CHUNKING ATTEMPTS"])
        for item in attempts[-8:]:
            lines.append(f"- {item.get('author')} attempt {item.get('attempt')} "
                         f"[{item.get('status')}]: {item.get('reason', '')[:500]}")
    verdicts = state.get("critic_verdicts", [])
    if verdicts:
        verdict = verdicts[-1]
        lines.extend(["", "LATEST FORMALIZATION VERDICT",
                      f"- {verdict.get('verdict')} by {verdict.get('author')}: "
                      f"{verdict.get('summary', '')[:1000]}"])
        if verdict.get("reopen_tasks"):
            lines.append("- reopened tasks: " + ", ".join(verdict["reopen_tasks"]))
        if verdict.get("review"):
            lines.append("- Full structured evidence and rationale: autoformalize_status().critic_verdicts")
    owned = [item for item in state.get("strategies", {}).values()
             if item.get("status") in {"claimed", "paused"}
             and autoformalize_state.participates(item, author)]
    if owned:
        lines.extend(["", "YOUR CLAIMED/ASSISTED STRATEGIES"])
        for item in owned[:8]:
            lines.append(f"- {item['strategy_id']} [{item['status']}] task={item.get('target')}: "
                         f"{item.get('description', '')[:500]}")
    tasks = list(state.get("formal_tasks", {}).values())
    if tasks:
        lines.extend(["", "FORMALIZATION TASKS"])
        for task in tasks[:40]:
            lines.append(f"- {task['task_id']} [{task['status']}]: {task.get('lean_decl')}"
                         + (f"; deps={','.join(task['dependencies'])}" if task.get("dependencies") else ""))
    candidates = list(state.get("formal_candidates", {}).values())
    if candidates:
        lines.extend(["", "RECENT FORMALIZATION CANDIDATES"])
        for item in candidates[-12:]:
            lines.append(f"- {item['candidate_id']} [{item['status']}] task={item['task_id']} "
                         f"by {item['author']} at {item['commit_sha'][:12]}")
            if item.get("error"):
                lines.append(f"  failure: {item['error'][:800]}")
            for record in (item.get("build") or {}, item.get("verification") or {}):
                if record.get("artifact_id"):
                    lines.append(f"  artifact {record['artifact_id']}")
    strategies = [item for item in state.get("strategies", {}).values()
                  if item.get("status") in {"registered", "claimed", "paused"}]
    if strategies:
        lines.extend(["", "ACTIVE STRATEGIES"])
        for item in strategies[-16:]:
            lines.append(f"- {item['strategy_id']} [{item['status']}] task={item.get('target')} "
                         f"owner={item.get('owner') or 'none'}: {item.get('description', '')[:300]}")
    findings = [item for item in state.get("findings", {}).values() if item.get("status") == "active"]
    findings.sort(key=lambda item: (item.get("confidence", 0), item.get("created_at", 0)), reverse=True)
    if findings:
        lines.extend(["", "LIVE FINDINGS"])
        for item in findings[:10]:
            lines.append(f"- {item['finding_id']} ({item.get('confidence')}/100) "
                         f"{item.get('title')}: {item.get('content', '')[:600]}")
            if item.get("evidence"):
                lines.append(f"  evidence: {item['evidence'][:300]}")
    obstacles = [item for item in state.get("obstacles", {}).values() if item.get("status") == "open"]
    if obstacles:
        lines.extend(["", "OPEN OBSTACLES"])
        for item in obstacles[-10:]:
            lines.append(f"- {item['obstacle_id']} task={item.get('target') or 'global'}: "
                         f"{item.get('goal_state', '')[:500]}")
    questions = [item for item in state.get("questions", {}).values()
                 if item.get("status") == "open" and (
                     not item.get("to") or autoformalize_state.author_key(item.get("to"))
                     == autoformalize_state.author_key(author))]
    if questions:
        lines.extend(["", "OPEN QUESTIONS"])
        for item in questions[-8:]:
            lines.append(f"- {item['question_id']}: {item.get('body', '')[:500]}")
    text = "\n".join(lines)
    try:
        limit = max(2_000, min(32_000, int(os.getenv("UNITY_AUTOFORMALIZE_BRIEF_CHARS", "12000"))))
    except ValueError:
        limit = 12_000
    suffix = "\n...[brief truncated]"
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
) -> dict:
    """Compatibility API for submitting an already-committed implementation."""
    author = _author(author)
    with _finalization_lock(author):
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

    This is deliberately not a build assertion.  The autoformalize controller applies
    the exact resulting commit to main and performs the sole authoritative full
    build and declaration review there.
    """
    author = _author(author)
    with _finalization_lock(author):
        state = autoformalize_state.load_state(FORUM_DIR)
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
            or strategy.get("phase_revision") != state["formalization"]["revision"]
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
            if expected_file and expected_file not in candidate_paths:
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
            notes=notes, supersedes=supersedes,
        )
        return {
            **result,
            "committed": committed,
            "changed_paths": staged,
            "commit_sha": head,
        }


def _current_formal_candidate(state: dict, candidate: dict) -> bool:
    formal = state.get("formalization", {})
    return (
        candidate.get("formalization_revision") == formal.get("revision")
        and candidate.get("solution_candidate") == formal.get("solution_candidate")
        and candidate.get("solution_sha256") == formal.get("solution_sha256")
    )


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
    revision = state.get("formalization", {}).get("revision")
    targets = {
        strategy.get("target", "")
        for strategy in state.get("strategies", {}).values()
        if strategy.get("phase") == "formalizing"
        and strategy.get("phase_revision") == revision
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
        if any(
            state.get("formal_tasks", {}).get(dependency, {}).get("status") != "complete"
            for dependency in target_task.get("dependencies", [])
        ):
            return _sync_blocked("dependencies_pending", "The next formal task has unresolved dependencies.")
        if has_pending_formal_candidate(state, author):
            return _sync_blocked("candidate_pending", "Candidate review is pending; its branch is preserved.")
        tree = worktree.agent_worktree(_root(), author)
        if not tree.is_dir():
            return _sync_blocked("missing_worktree", "The agent has no active worktree.")
        if previous_task == next_task:
            return {"ok": True, "preserved": True, "worktree": str(tree)}
        previous = state.get("formal_tasks", {}).get(previous_task, {})
        if unresolved_formal_tasks(state, author) or (
            previous_task and previous.get("status") != "complete"
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
            return _sync_blocked("dirty_worktree", "Unassigned local edits are preserved.")
        if _git(tree, "merge-base", "--is-ancestor", "HEAD", main_sha, check=False).returncode:
            return _sync_blocked("local_commits", "Unassigned local commits are preserved.")
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


def request_rechunk(author: str, reason: str) -> dict:
    """Rebuild an incorrect encoding/signature contract without changing supplied sources.

    Report defects in the supplied mathematics as obstacles with evidence. This
    pipeline cannot rewrite the source or switch to an informal solving phase.
    """
    author = _author(author)
    with _merge_lock():
        result = autoformalize_state.request_rechunk(FORUM_DIR, author, reason)
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
    forum_post, forum_read, artifact_info, artifact_read,
)
COORDINATION = (
    register_strategy, claim_strategy, assist_strategy, unclaim_strategy,
    mark_strategy_incorrect, publish_finding, report_obstacle,
    ask_question, answer_question,
)
SOURCE_FEEDBACK = (publish_finding, report_obstacle, ask_question, answer_question)
PROFILE_TOOLS = {
    "chunking": COMMON + SOURCE_FEEDBACK,
    "formalizing": COMMON + COORDINATION + (
        finalize_formalization, emit_formalization_candidate, sync_from_main, request_rechunk,
    ),
    "critic": COMMON + SOURCE_FEEDBACK + (request_rechunk, submit_formalization_verdict),
    "retrospective": COMMON,
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
