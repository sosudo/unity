"""Authoritative, file-backed coordination state for ``unity solve``.

The solve pipeline has two gates: an informal mathematical solution and a Lean
formalization of that accepted solution.  This module deliberately mirrors the
small transaction/state layer used by :mod:`unity.prove_state`; orchestration
remains in ``commands/solve.py`` and ``solve_runtime.py``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Iterator

from .solve_review import SemanticReview


SCHEMA_VERSION = 4
PHASES = {"solving", "solution_review", "chunking", "formalizing", "critic", "complete"}
STRATEGY_PHASES = {"solving", "formalizing"}
STRATEGY_STATUSES = {"registered", "claimed", "paused", "incorrect", "succeeded", "cancelled"}
INFORMAL_TASK_STATUSES = {"open", "result_available", "resolved", "blocked", "superseded", "cancelled"}
INFORMAL_RESULT_STATUSES = {"submitted", "supported", "objected", "incorporated", "superseded"}
SOLUTION_STATUSES = {"open", "review", "accepted"}
FORMAL_STATUSES = {"waiting", "active", "review", "approval_pending", "accepted"}
_ACTIVE_STRATEGIES = {"registered", "claimed", "paused"}
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def state_path(forum_dir: Path) -> Path:
    return Path(forum_dir) / "solve-state.json"


def _default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "run_id": "",
        "phase": "solving",
        "problem_sha256": "",
        "solution": {
            "revision": 1,
            "status": "open",
            "current_candidate": None,
            "accepted_candidate": None,
            "previous_candidate": None,
            "reopen_reason": "",
        },
        "formalization": {
            "revision": 0,
            "status": "waiting",
            "solution_candidate": None,
            "solution_sha256": "",
            "main_sha": "",
            "contract": None,
            "requirements": [],
            "review_snapshot": None,
            "pending_verdict_id": None,
        },
        "informal_tasks": {},
        "informal_results": {},
        "review_issues": {},
        "strategies": {},
        "findings": {},
        "obstacles": {},
        "questions": {},
        "solution_candidates": {},
        "formal_tasks": {},
        "formal_candidates": {},
        "chunking_attempts": [],
        "critic_verdicts": [],
        "review_snapshots": {},
        "events": [],
    }


def _read_unlocked(forum_dir: Path) -> dict:
    path = state_path(forum_dir)
    if not path.exists():
        return _default_state()
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return _default_state()
    base = _default_state()
    base.update(state)
    for key in (
        "informal_tasks", "informal_results", "review_issues",
        "strategies", "findings", "obstacles", "questions",
        "solution_candidates", "formal_tasks", "formal_candidates",
        "review_snapshots",
    ):
        if not isinstance(base.get(key), dict):
            base[key] = {}
    # Early solve-runtime states called these records subgoals. They become
    # generalized informal tasks on first read without affecting prove state.
    for task_id, item in (state.get("subgoals") or {}).items():
        migrated = dict(item)
        migrated.setdefault("task_id", migrated.pop("subgoal_id", task_id))
        migrated.setdefault("kind", "subgoal")
        migrated.setdefault("status", "open")
        migrated.setdefault("resolved_result", None)
        base["informal_tasks"].setdefault(task_id, migrated)
    if not isinstance(base.get("events"), list):
        base["events"] = []
    if not isinstance(base.get("chunking_attempts"), list):
        base["chunking_attempts"] = []
    if not isinstance(base.get("critic_verdicts"), list):
        base["critic_verdicts"] = []
    solution = _default_state()["solution"]
    solution.update(base.get("solution") or {})
    base["solution"] = solution
    formalization = _default_state()["formalization"]
    formalization.update(base.get("formalization") or {})
    base["formalization"] = formalization
    return base


def load_state(forum_dir: Path) -> dict:
    return _read_unlocked(Path(forum_dir))


def _write_unlocked(forum_dir: Path, state: dict) -> None:
    forum_dir = Path(forum_dir)
    forum_dir.mkdir(parents=True, exist_ok=True)
    state["schema_version"] = SCHEMA_VERSION
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".solve-state-", suffix=".json", dir=forum_dir)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
        Path(temporary).replace(state_path(forum_dir))
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def transaction(forum_dir: Path) -> Iterator[dict]:
    forum_dir = Path(forum_dir)
    forum_dir.mkdir(parents=True, exist_ok=True)
    with (forum_dir / "solve-state.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = _read_unlocked(forum_dir)
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                state["revision"] = int(state.get("revision", 0)) + 1
                _write_unlocked(forum_dir, state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _text(value: str, field: str, limit: int = 4000, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return result


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _event(state: dict, kind: str, **fields) -> dict:
    event = {"event_id": _id("event"), "kind": kind, "timestamp": time.time(), **fields}
    state.setdefault("events", []).append(event)
    state["events"] = state["events"][-1000:]
    return event


def initialize(forum_dir: Path, problem_sha256: str, main_sha: str, *, reset: bool = False) -> dict:
    if not _ARTIFACT_SHA_RE.fullmatch(problem_sha256):
        raise ValueError("problem_sha256 must be a full SHA-256")
    with transaction(forum_dir) as state:
        if reset or not state.get("run_id"):
            state.clear()
            state.update(_default_state())
            state["run_id"] = _id("solve")
            state["problem_sha256"] = problem_sha256
            state["formalization"]["main_sha"] = main_sha
            _event(state, "solve_initialized", problem_sha256=problem_sha256, main_sha=main_sha)
        elif state.get("problem_sha256") != problem_sha256:
            raise ValueError(
                "UNITY.md changed since this solve run began; start a fresh run without --continue"
            )
    return load_state(forum_dir)


def set_phase(forum_dir: Path, phase: str, *, reason: str = "") -> dict:
    if phase not in PHASES:
        raise ValueError(f"unknown solve phase '{phase}'")
    with transaction(forum_dir) as state:
        previous = state.get("phase")
        state["phase"] = phase
        _event(state, "phase_changed", previous=previous, phase=phase, reason=reason)
    return load_state(forum_dir)


def create_informal_task(
    forum_dir: Path,
    author: str,
    kind: str,
    title: str,
    description: str,
    *,
    parent_id: str = "",
    dependencies: list[str] | None = None,
) -> dict:
    kind = _key(_text(kind, "kind", 80))
    title = _text(title, "title", 200)
    description = _text(description, "description")
    with transaction(forum_dir) as state:
        if state["phase"] != "solving":
            raise ValueError("informal tasks can only be created during solving")
        if parent_id and parent_id not in state["informal_tasks"]:
            raise ValueError(f"unknown parent task '{parent_id}'")
        dependencies = list(dict.fromkeys(dependencies or []))
        unknown = [item for item in dependencies if item not in state["informal_tasks"]]
        if unknown:
            raise ValueError("unknown informal-task dependencies: " + ", ".join(unknown))
        title_key = _key(title)
        for existing in state["informal_tasks"].values():
            if (
                existing.get("solution_revision") == state["solution"]["revision"]
                and existing.get("kind") == kind
                and existing.get("title_key") == title_key
                and existing.get("parent_id") == (parent_id or None)
                and existing.get("status") not in {"superseded", "cancelled"}
            ):
                return {"status": "duplicate", "task": existing}
        task_id = _id("task")
        item = {
            "task_id": task_id,
            "kind": kind,
            "author": _text(author, "author", 100),
            "title": title,
            "title_key": title_key,
            "description": description,
            "parent_id": parent_id or None,
            "dependencies": dependencies,
            "status": "open",
            "resolved_result": None,
            "solution_revision": state["solution"]["revision"],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        state["informal_tasks"][task_id] = item
        _event(state, "informal_task_created", task_id=task_id, task_kind=kind,
               author=author, dependencies=dependencies)
    return {"status": "created", "task": item}


def create_subgoal(
    forum_dir: Path,
    author: str,
    title: str,
    description: str,
    *,
    parent_id: str = "",
    dependencies: list[str] | None = None,
) -> dict:
    """Compatibility alias for the initial solve-specific Forum API."""
    return create_informal_task(
        forum_dir, author, "subgoal", title, description,
        parent_id=parent_id, dependencies=dependencies,
    )["task"]


def ready_informal_tasks(state: dict) -> list[dict]:
    tasks = state.get("informal_tasks", {})
    revision = state.get("solution", {}).get("revision")
    return [
        task for task in tasks.values()
        if task.get("solution_revision") == revision
        and task.get("status") == "open"
        and all(tasks.get(dep, {}).get("status") == "resolved"
                for dep in task.get("dependencies", []))
    ]


def register_strategy(
    forum_dir: Path,
    author: str,
    description: str,
    *,
    target: str = "",
    family: str = "",
    central_claim: str = "",
) -> dict:
    description = _text(description, "description")
    family_key = _key(family)
    claim_key = _key(central_claim)
    with transaction(forum_dir) as state:
        phase = state["phase"]
        if phase not in STRATEGY_PHASES:
            raise ValueError("strategies can only be registered during solving or formalizing")
        revision = (
            state["solution"]["revision"] if phase == "solving"
            else state["formalization"]["revision"]
        )
        if target:
            collection = state["informal_tasks"] if phase == "solving" else state["formal_tasks"]
            if target not in collection:
                raise ValueError(f"unknown {phase} target '{target}'")
            expected_status = "open" if phase == "solving" else "pending"
            if collection[target].get("status") != expected_status:
                raise ValueError(
                    f"{phase} target '{target}' is {collection[target].get('status')}, "
                    "not accepting new strategies"
                )
        description_key = re.sub(r"\s+", " ", description.casefold()).strip()
        for existing in state["strategies"].values():
            if (
                existing.get("phase") == phase
                and existing.get("phase_revision") == revision
                and existing.get("target", "") == target
                and existing.get("status") in _ACTIVE_STRATEGIES
                and (
                    (family_key and existing.get("family_key") == family_key)
                    or (claim_key and existing.get("central_claim_key") == claim_key)
                    or existing.get("description_key") == description_key
                )
            ):
                return {"status": "duplicate", "strategy": existing}
        strategy_id = _id("strategy")
        strategy = {
            "strategy_id": strategy_id,
            "phase": phase,
            "phase_revision": revision,
            "target": target,
            "description": description,
            "description_key": description_key,
            "family": _text(family, "family", 120, required=False),
            "family_key": family_key,
            "central_claim": _text(central_claim, "central_claim", 300, required=False),
            "central_claim_key": claim_key,
            "creator": _text(author, "author", 100),
            "owner": None,
            "assistants": [],
            "status": "registered",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        state["strategies"][strategy_id] = strategy
        _event(state, "strategy_registered", strategy_id=strategy_id, author=author,
               phase=phase, target=target)
    return {"status": "registered", "strategy": strategy}


def claim_strategy(forum_dir: Path, strategy_id: str, author: str) -> dict:
    with transaction(forum_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if strategy["phase"] != state["phase"]:
            raise ValueError("strategy belongs to a different phase")
        active_owned = next((
            item for item in state["strategies"].values()
            if item.get("strategy_id") != strategy_id
            and item.get("phase") == strategy["phase"]
            and item.get("owner") == author
            and item.get("status") == "claimed"
        ), None)
        if active_owned is not None:
            return {
                "status": "agent_busy",
                "strategy": strategy,
                "active_strategy": active_owned,
            }
        if strategy["status"] == "claimed":
            if strategy["owner"] == author:
                return {"status": "claimed", "strategy": strategy, "idempotent": True}
            return {"status": "conflict", "strategy": strategy, "owner": strategy["owner"]}
        if strategy["status"] != "registered":
            raise ValueError(f"strategy is {strategy['status']}, not claimable")
        target_task = state["informal_tasks"].get(strategy.get("target", ""), {})
        if strategy["phase"] == "solving" and target_task.get("kind") == "synthesis":
            active_synthesis = next((
                item for item in state["strategies"].values()
                if item.get("strategy_id") != strategy_id
                and item.get("phase") == "solving"
                and item.get("target") == strategy.get("target")
                and item.get("status") == "claimed"
            ), None)
            if active_synthesis is not None:
                return {
                    "status": "task_conflict",
                    "strategy": strategy,
                    "task": target_task,
                    "active_strategy": active_synthesis,
                    "owner": active_synthesis.get("owner"),
                }
        strategy["status"] = "claimed"
        strategy["owner"] = _text(author, "author", 100)
        strategy["updated_at"] = time.time()
        _event(state, "strategy_claimed", strategy_id=strategy_id, author=author,
               phase=strategy["phase"], target=strategy["target"])
        if (
            strategy["phase"] == "solving"
            and state["informal_tasks"].get(strategy["target"], {}).get("kind") == "synthesis"
        ):
            _event(state, "synthesis_started", strategy_id=strategy_id,
                   task_id=strategy["target"], author=author)
    return {"status": "claimed", "strategy": strategy}


def assist_strategy(forum_dir: Path, strategy_id: str, author: str, contribution: str = "") -> dict:
    with transaction(forum_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy or strategy.get("status") != "claimed":
            raise ValueError("strategy is not actively claimed")
        if author != strategy["owner"] and author not in strategy["assistants"]:
            strategy["assistants"].append(_text(author, "author", 100))
        strategy["updated_at"] = time.time()
        _event(state, "strategy_assisted", strategy_id=strategy_id, author=author,
               phase=strategy["phase"], target=strategy["target"],
               contribution=_text(contribution, "contribution", 1000, required=False))
    return {"status": "assisting", "strategy": strategy}


def release_strategy(
    forum_dir: Path,
    strategy_id: str,
    author: str,
    *,
    reason: str = "",
    incorrect: bool = False,
) -> dict:
    with transaction(forum_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if strategy.get("owner") != author:
            raise ValueError("only the strategy owner can release it")
        strategy["status"] = "incorrect" if incorrect else "registered"
        strategy["owner"] = None
        strategy["updated_at"] = time.time()
        _event(state, "strategy_incorrect" if incorrect else "strategy_released",
               strategy_id=strategy_id, author=author,
               reason=_text(reason, "reason", 2000, required=incorrect))
    return {"status": strategy["status"], "strategy": strategy}


def release_author_claims(forum_dir: Path, author: str, reason: str) -> list[str]:
    released: list[str] = []
    with transaction(forum_dir) as state:
        for strategy in state["strategies"].values():
            if strategy.get("owner") != author or strategy.get("status") != "claimed":
                continue
            strategy["owner"] = None
            strategy["status"] = "registered"
            strategy["updated_at"] = time.time()
            released.append(strategy["strategy_id"])
        if released:
            _event(state, "claims_released", author=author, strategies=released, reason=reason)
    return released


def submit_informal_result(
    forum_dir: Path,
    author: str,
    task_id: str,
    strategy_id: str,
    artifact_id: str,
    sha256: str,
    source_path: str,
    summary: str,
    *,
    kind: str = "",
    supersedes: str = "",
) -> dict:
    """Publish one immutable mathematical or paper component."""
    if not _ARTIFACT_SHA_RE.fullmatch(sha256):
        raise ValueError("sha256 must be a full SHA-256")
    with transaction(forum_dir) as state:
        if state["phase"] != "solving":
            raise ValueError("informal results can only be submitted during solving")
        task = state["informal_tasks"].get(task_id)
        if not task or task.get("solution_revision") != state["solution"]["revision"]:
            raise ValueError("informal result targets an unknown or stale task")
        if task.get("status") in {"superseded", "cancelled"}:
            raise ValueError(f"informal task is {task['status']}")
        strategy = state["strategies"].get(strategy_id)
        if (
            not strategy
            or strategy.get("phase") != "solving"
            or strategy.get("target") != task_id
        ):
            raise ValueError("result strategy does not target this informal task")
        if author != strategy.get("owner") and author not in strategy.get("assistants", []):
            raise ValueError("result author does not own or assist the strategy")
        if supersedes:
            old = state["informal_results"].get(supersedes)
            if not old or old.get("task_id") != task_id:
                raise ValueError("superseded result must belong to the same task")
        for existing in state["informal_results"].values():
            if existing.get("task_id") != task_id:
                continue
            if existing.get("sha256") == sha256:
                return {"status": "duplicate", "result": existing}
            if existing.get("status") in {"submitted", "supported", "incorporated"}:
                return {"status": "conflict", "result": existing}
        result_id = _id("result")
        result = {
            "result_id": result_id,
            "task_id": task_id,
            "strategy_id": strategy_id,
            "author": _text(author, "author", 100),
            "kind": _key(kind) or task.get("kind", "component"),
            "artifact_id": _text(artifact_id, "artifact_id", 100),
            "sha256": sha256,
            "source_path": _text(source_path, "source_path", 1000),
            "summary": _text(summary, "summary", 3000),
            "solution_revision": state["solution"]["revision"],
            "supersedes": supersedes or None,
            "status": "submitted",
            "reviews": [],
            "created_at": time.time(),
        }
        if supersedes:
            state["informal_results"][supersedes]["status"] = "superseded"
        state["informal_results"][result_id] = result
        task["status"] = "result_available"
        task["updated_at"] = time.time()
        for item in state["strategies"].values():
            if (
                item.get("phase") == "solving"
                and item.get("target") == task_id
                and item.get("status") in _ACTIVE_STRATEGIES
            ):
                item["paused_from"] = item["status"]
                item["status"] = "paused"
        _event(state, "informal_result_submitted", result_id=result_id,
               task_id=task_id, author=author, strategy_id=strategy_id)
    return {"status": "submitted", "result": result}


def review_informal_result(
    forum_dir: Path,
    result_id: str,
    author: str,
    verdict: str,
    review: str,
) -> dict:
    verdict = verdict.strip().casefold()
    if verdict not in {"support", "object"}:
        raise ValueError("verdict must be support or object")
    with transaction(forum_dir) as state:
        result = state["informal_results"].get(result_id)
        if not result or result.get("status") not in {"submitted", "supported"}:
            raise ValueError("informal result is not reviewable")
        task = state["informal_tasks"][result["task_id"]]
        if (
            task.get("solution_revision") != state["solution"]["revision"]
            or task.get("status") in {"superseded", "cancelled"}
        ):
            raise ValueError("informal result targets a stale or closed task")
        if result.get("author") == author:
            raise ValueError("authors cannot review their own informal result")
        if any(item.get("author") == author for item in result.get("reviews", [])):
            raise ValueError("reviewer already reviewed this informal result")
        item = {
            "review_id": _id("result-review"),
            "author": _text(author, "author", 100),
            "verdict": verdict,
            "review": _text(review, "review", 3000),
            "timestamp": time.time(),
        }
        result["reviews"].append(item)
        if verdict == "support":
            result["status"] = "supported"
            task["status"] = "resolved"
            task["resolved_result"] = result_id
            for strategy in state["strategies"].values():
                if (
                    strategy.get("phase") == "solving"
                    and strategy.get("target") == task["task_id"]
                    and strategy.get("status") in _ACTIVE_STRATEGIES
                ):
                    strategy["status"] = (
                        "succeeded" if strategy["strategy_id"] == result["strategy_id"]
                        else "cancelled"
                    )
                    strategy.pop("paused_from", None)
            for obstacle in state["obstacles"].values():
                if obstacle.get("status") == "open" and obstacle.get("target") == task["task_id"]:
                    obstacle["status"] = "resolved"
                    obstacle["resolved_by"] = result_id
            if task.get("kind") == "counterexample_check" and task.get("parent_id"):
                parent = state["informal_tasks"].get(task["parent_id"])
                if parent and parent.get("status") not in {"resolved", "superseded", "cancelled"}:
                    parent["status"] = "superseded"
                    parent["superseded_by"] = result_id
                    parent["supersession_reason"] = (
                        f"supported counterexample check {task['task_id']}"
                    )
                    parent["updated_at"] = time.time()
                    for strategy in state["strategies"].values():
                        if (
                            strategy.get("phase") == "solving"
                            and strategy.get("target") == parent["task_id"]
                            and strategy.get("status") in _ACTIVE_STRATEGIES
                        ):
                            strategy["status"] = "cancelled"
                            strategy["cancellation_reason"] = parent["supersession_reason"]
                            strategy["updated_at"] = time.time()
                    _event(
                        state,
                        "informal_task_superseded",
                        task_id=parent["task_id"],
                        superseded_by=result_id,
                        reason=parent["supersession_reason"],
                    )
            _event(state, "informal_task_resolved", task_id=task["task_id"],
                   result_id=result_id, author=author)
        else:
            result["status"] = "objected"
            task["status"] = "open"
            task["resolved_result"] = None
            for strategy in state["strategies"].values():
                if (
                    strategy.get("phase") == "solving"
                    and strategy.get("target") == task["task_id"]
                    and strategy.get("status") == "paused"
                ):
                    strategy["status"] = strategy.pop("paused_from", "registered")
            _event(state, "informal_task_reopened", task_id=task["task_id"],
                   result_id=result_id, author=author, reason=review[:1000])
        task["updated_at"] = time.time()
        _event(state, f"informal_result_{'supported' if verdict == 'support' else 'objected'}",
               result_id=result_id, task_id=task["task_id"], author=author,
               review=review[:1000])
    return {"result": result, "review": item, "task": task}


def publish_finding(
    forum_dir: Path,
    author: str,
    kind: str,
    title: str,
    content: str,
    confidence: int,
    *,
    target: str = "",
    strategy_id: str = "",
    evidence: str = "",
    supersedes: str = "",
) -> dict:
    if not isinstance(confidence, int) or not 0 <= confidence <= 100:
        raise ValueError("confidence must be an integer from 0 through 100")
    with transaction(forum_dir) as state:
        if strategy_id and strategy_id not in state["strategies"]:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        old_finding = None
        if supersedes:
            old_finding = state["findings"].get(supersedes)
            if old_finding is None:
                raise ValueError(f"unknown superseded finding '{supersedes}'")
            if old_finding.get("status") != "active":
                raise ValueError("only an active finding can be superseded")
        finding_id = _id("finding")
        finding = {
            "finding_id": finding_id,
            "phase": state["phase"],
            "target": target,
            "strategy_id": strategy_id or None,
            "author": _text(author, "author", 100),
            "kind": _text(kind, "kind", 80),
            "title": _text(title, "title", 200),
            "content": _text(content, "content"),
            "confidence": confidence,
            "evidence": _text(evidence, "evidence", 4000, required=False),
            "supersedes": supersedes or None,
            "status": "active",
            "created_at": time.time(),
        }
        if old_finding is not None:
            old_finding["status"] = "superseded"
            old_finding["superseded_by"] = finding_id
            old_finding["updated_at"] = time.time()
        state["findings"][finding_id] = finding
        _event(state, "finding_published", finding_id=finding_id, author=author,
               phase=state["phase"], target=target, supersedes=supersedes or None)
    return finding


def report_obstacle(
    forum_dir: Path,
    author: str,
    goal_state: str,
    *,
    target: str = "",
    tried: str = "",
    hypothesis: str = "",
) -> dict:
    with transaction(forum_dir) as state:
        obstacle_id = _id("obstacle")
        obstacle = {
            "obstacle_id": obstacle_id,
            "phase": state["phase"],
            "target": target,
            "author": _text(author, "author", 100),
            "goal_state": _text(goal_state, "goal_state"),
            "tried": _text(tried, "tried", 3000, required=False),
            "hypothesis": _text(hypothesis, "hypothesis", 2000, required=False),
            "status": "open",
            "created_at": time.time(),
        }
        state["obstacles"][obstacle_id] = obstacle
        _event(state, "obstacle_reported", obstacle_id=obstacle_id, author=author,
               phase=state["phase"], target=target)
    return obstacle


def ask_question(
    forum_dir: Path,
    author: str,
    body: str,
    *,
    to: str = "",
    target: str = "",
) -> dict:
    with transaction(forum_dir) as state:
        question_id = _id("question")
        question = {
            "question_id": question_id,
            "phase": state["phase"],
            "target": target,
            "author": _text(author, "author", 100),
            "to": _text(to, "to", 100, required=False),
            "body": _text(body, "body"),
            "status": "open",
            "answers": [],
            "created_at": time.time(),
        }
        state["questions"][question_id] = question
        _event(state, "question_asked", question_id=question_id, author=author, to=to,
               target=target)
    return question


def answer_question(forum_dir: Path, question_id: str, author: str, body: str) -> dict:
    with transaction(forum_dir) as state:
        question = state["questions"].get(question_id)
        if not question:
            raise ValueError(f"unknown question '{question_id}'")
        question["answers"].append({
            "author": _text(author, "author", 100),
            "body": _text(body, "body"),
            "timestamp": time.time(),
        })
        question["status"] = "answered"
        _event(state, "question_answered", question_id=question_id, author=author)
    return question


def submit_solution_candidate(
    forum_dir: Path,
    author: str,
    artifact_id: str,
    sha256: str,
    source_path: str,
    *,
    strategy_id: str = "",
    notes: str = "",
    supersedes: str = "",
    replace_accepted: bool = False,
    component_ids: list[str] | None = None,
) -> dict:
    if not _ARTIFACT_SHA_RE.fullmatch(sha256):
        raise ValueError("sha256 must be a full SHA-256")
    with transaction(forum_dir) as state:
        phase = state["phase"]
        if phase != "solving" and not replace_accepted:
            raise ValueError("solution candidates can only be submitted during solving")
        if replace_accepted:
            previous_accepted = state["solution"].get("accepted_candidate")
            if not previous_accepted:
                raise ValueError("there is no accepted solution to replace")
            supersedes = supersedes or previous_accepted
            previous_components = list(
                state["solution_candidates"].get(previous_accepted, {}).get("components", [])
            )
            _reopen_solution_in_tx(state, author, notes or "source correction proposed")
            if component_ids is None:
                component_ids = [item["result_id"] for item in previous_components]
        if not supersedes:
            rejected = [
                item for item in state["solution_candidates"].values()
                if item.get("status") == "rejected"
                and item.get("solution_revision") == state["solution"]["revision"]
            ]
            if rejected:
                supersedes = max(rejected, key=lambda item: item.get("created_at", 0))["candidate_id"]
        current_id = state["solution"].get("current_candidate")
        if current_id:
            current = state["solution_candidates"].get(current_id, {})
            if current.get("status") in {"submitted", "review"}:
                if current.get("sha256") == sha256 and current.get("author") == author:
                    return {"status": "submitted", "candidate": current, "idempotent": True}
                return {"status": "conflict", "candidate": current}
        if strategy_id:
            strategy = state["strategies"].get(strategy_id)
            if not strategy or strategy.get("phase") != "solving":
                raise ValueError("candidate strategy is not a solving strategy")
            if author != strategy.get("owner") and author not in strategy.get("assistants", []):
                raise ValueError("candidate author does not own or assist the strategy")
        if supersedes and supersedes not in state["solution_candidates"]:
            raise ValueError(f"unknown superseded candidate '{supersedes}'")
        if component_ids is None:
            component_ids = [
                result_id for result_id, result in state["informal_results"].items()
                if result.get("solution_revision") == state["solution"]["revision"]
                and result.get("status") in {"supported", "incorporated"}
            ]
        component_ids = list(dict.fromkeys(component_ids))
        components = []
        for result_id in component_ids:
            result = state["informal_results"].get(result_id)
            if not result or result.get("status") not in {"supported", "incorporated"}:
                raise ValueError(f"candidate component '{result_id}' is unavailable")
            components.append({
                "result_id": result_id,
                "task_id": result["task_id"],
                "artifact_id": result["artifact_id"],
                "sha256": result["sha256"],
                "kind": result.get("kind", "component"),
                "summary": result.get("summary", ""),
            })
        candidate_id = _id("solution")
        candidate = {
            "candidate_id": candidate_id,
            "author": _text(author, "author", 100),
            "artifact_id": _text(artifact_id, "artifact_id", 100),
            "sha256": sha256,
            "source_path": source_path,
            "strategy_id": strategy_id or None,
            "notes": _text(notes, "notes", 2000, required=False),
            "supersedes": supersedes or None,
            "components": components,
            "solution_revision": state["solution"]["revision"],
            "status": "review",
            "reviews": [],
            "created_at": time.time(),
        }
        state["solution_candidates"][candidate_id] = candidate
        state["solution"]["current_candidate"] = candidate_id
        state["solution"]["status"] = "review"
        state["phase"] = "solution_review"
        for strategy in state["strategies"].values():
            if strategy.get("phase") == "solving" and strategy.get("status") in _ACTIVE_STRATEGIES:
                strategy["paused_from"] = strategy["status"]
                strategy["status"] = "paused"
        _event(state, "solution_candidate_submitted", candidate_id=candidate_id,
               author=author, sha256=sha256)
    return {"status": "submitted", "candidate": candidate}


def review_solution_candidate(
    forum_dir: Path,
    candidate_id: str,
    author: str,
    verdict: str,
    review: str,
    *,
    evidence: str = "",
    issues: list[dict] | None = None,
) -> dict:
    verdict = verdict.strip().casefold()
    if verdict not in {"approve", "object"}:
        raise ValueError("verdict must be approve or object")
    with transaction(forum_dir) as state:
        candidate = state["solution_candidates"].get(candidate_id)
        if not candidate or candidate.get("status") != "review":
            raise ValueError("candidate is not under review")
        if candidate["author"] == author:
            raise ValueError("candidate authors cannot review their own candidate")
        if any(item["author"] == author for item in candidate["reviews"]):
            raise ValueError("reviewer already submitted a verdict for this candidate")
        item = {
            "review_id": _id("review"),
            "author": _text(author, "author", 100),
            "verdict": verdict,
            "review": _text(review, "review"),
            "evidence": _text(evidence, "evidence", 4000, required=False),
            "timestamp": time.time(),
            "issues": [],
        }
        if verdict == "object":
            raw_issues = issues or [{"kind": "general", "description": review}]
            for raw in raw_issues:
                if not isinstance(raw, dict):
                    raise ValueError("each review issue must be an object")
                issue_id = _id("issue")
                component_ids = list(dict.fromkeys(raw.get("component_ids") or []))
                candidate_components = {part["result_id"] for part in candidate.get("components", [])}
                unknown = [part for part in component_ids if part not in candidate_components]
                if unknown:
                    raise ValueError("review issue names unknown candidate components: " + ", ".join(unknown))
                issue = {
                    "issue_id": issue_id,
                    "candidate_id": candidate_id,
                    "kind": _key(raw.get("kind") or "general"),
                    "description": _text(raw.get("description") or review, "issue description", 3000),
                    "component_ids": component_ids,
                    "author": _text(author, "author", 100),
                    "status": "open",
                    "solution_revision": state["solution"]["revision"],
                    "repair_task": None,
                    "created_at": time.time(),
                }
                component_task_by_result = {
                    part["result_id"]: part["task_id"]
                    for part in candidate.get("components", [])
                }
                repair_dependencies = list(dict.fromkeys(
                    component_task_by_result[result_id]
                    for result_id in component_ids
                    if result_id in component_task_by_result
                ))
                task_id = _id("task")
                task = {
                    "task_id": task_id,
                    "kind": "paper_edit" if issue["kind"] == "exposition" else "review_fix",
                    "author": author,
                    "title": f"Repair {issue['kind']} in {candidate_id}",
                    "title_key": _key(f"repair {issue['kind']} {candidate_id}"),
                    "description": issue["description"],
                    "parent_id": None,
                    "dependencies": repair_dependencies,
                    "status": "open",
                    "resolved_result": None,
                    "solution_revision": state["solution"]["revision"],
                    "candidate_id": candidate_id,
                    "review_issue_id": issue_id,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
                issue["repair_task"] = task_id
                state["review_issues"][issue_id] = issue
                state["informal_tasks"][task_id] = task
                item["issues"].append(issue_id)
                _event(state, "review_issue_created", issue_id=issue_id,
                       candidate_id=candidate_id, task_id=task_id, issue_kind=issue["kind"])
        candidate["reviews"].append(item)
        _event(state, "solution_candidate_reviewed", candidate_id=candidate_id,
               author=author, verdict=verdict)
    return {"candidate": candidate, "review": item}


def accept_solution_candidate(forum_dir: Path, candidate_id: str, author: str) -> dict:
    with transaction(forum_dir) as state:
        candidate = state["solution_candidates"].get(candidate_id)
        if not candidate or candidate.get("status") != "review":
            raise ValueError("candidate is not reviewable")
        if not any(item.get("verdict") == "approve" for item in candidate["reviews"]):
            raise ValueError("candidate has no independent approval")
        if any(item.get("verdict") == "object" for item in candidate["reviews"]):
            raise ValueError("candidate has an unresolved objection")
        candidate["status"] = "accepted"
        candidate["accepted_at"] = time.time()
        state["solution"].update({
            "status": "accepted",
            "accepted_candidate": candidate_id,
            "current_candidate": candidate_id,
            "previous_candidate": state["solution"].get("previous_candidate"),
            "reopen_reason": "",
        })
        state["phase"] = "chunking"
        included = {item["result_id"] for item in candidate.get("components", [])}
        for result_id in included:
            result = state["informal_results"].get(result_id)
            if result:
                result["status"] = "incorporated"
                task = state["informal_tasks"].get(result.get("task_id"), {})
                task["status"] = "resolved"
                task["resolved_result"] = result_id
                task["updated_at"] = time.time()
        for obstacle in state["obstacles"].values():
            if obstacle.get("status") == "open" and obstacle.get("phase") == "solving":
                obstacle["status"] = "resolved"
                obstacle["resolved_by"] = candidate_id
        lineage: set[str] = set()
        ancestor = candidate.get("supersedes")
        while ancestor and ancestor not in lineage:
            lineage.add(ancestor)
            ancestor = state["solution_candidates"].get(ancestor, {}).get("supersedes")
        for issue in state["review_issues"].values():
            if issue.get("status") == "open" and issue.get("candidate_id") in lineage:
                issue["status"] = "resolved"
                issue["resolved_by"] = candidate_id
                repair = state["informal_tasks"].get(issue.get("repair_task"), {})
                if repair and repair.get("status") not in {"superseded", "cancelled"}:
                    repair["status"] = "resolved"
                    repair["updated_at"] = time.time()
        for task in state["informal_tasks"].values():
            if (
                task.get("solution_revision") == state["solution"]["revision"]
                and task.get("status") in {"open", "result_available", "blocked"}
            ):
                task.update(
                    status="superseded",
                    superseded_by=candidate_id,
                    cancellation_reason="accepted complete informal solution",
                    updated_at=time.time(),
                )
                _event(state, "informal_task_superseded",
                       task_id=task["task_id"], superseded_by=candidate_id,
                       reason=task["cancellation_reason"])
        for strategy in state["strategies"].values():
            if strategy.get("phase") == "solving" and strategy.get("status") in _ACTIVE_STRATEGIES:
                strategy["status"] = "succeeded" if strategy.get("strategy_id") == candidate.get("strategy_id") else "cancelled"
                strategy.pop("paused_from", None)
        _event(state, "solution_candidate_accepted", candidate_id=candidate_id, author=author,
               sha256=candidate["sha256"])
    return candidate


def reject_solution_candidate(forum_dir: Path, candidate_id: str, author: str, reason: str) -> dict:
    with transaction(forum_dir) as state:
        candidate = state["solution_candidates"].get(candidate_id)
        if not candidate or candidate.get("status") != "review":
            raise ValueError("candidate is not reviewable")
        candidate["status"] = "rejected"
        candidate["rejection_reason"] = _text(reason, "reason")
        state["solution"].update({"status": "open", "current_candidate": None,
                                  "accepted_candidate": None, "reopen_reason": reason})
        state["phase"] = "solving"
        for strategy in state["strategies"].values():
            if strategy.get("phase") == "solving" and strategy.get("status") == "paused":
                strategy["status"] = strategy.pop("paused_from", "registered")
        _event(state, "solution_candidate_rejected", candidate_id=candidate_id,
               author=author, reason=reason)
    return candidate


def chunking_attempt_count(state: dict, candidate_id: str, author: str) -> int:
    """Count attempts already allocated to an agent for one immutable paper."""
    return sum(
        item.get("candidate_id") == candidate_id
        and not item.get("obsolete")
        and str(item.get("author", "")).casefold() == str(author).casefold()
        for item in state.get("chunking_attempts", [])
    )


def begin_chunking_attempt(forum_dir: Path, candidate_id: str, author: str) -> dict:
    """Persist an attempt before dispatch so crashes and interrupted runs still count."""
    with transaction(forum_dir) as state:
        if state.get("phase") != "chunking":
            raise ValueError("chunking attempt can only start during chunking")
        if state["solution"].get("accepted_candidate") != candidate_id:
            raise ValueError("chunking attempt does not target the accepted solution")
        if candidate_id not in state.get("solution_candidates", {}):
            raise ValueError("unknown solution candidate")

        now = time.time()
        for previous in state.get("chunking_attempts", []):
            if previous.get("candidate_id") == candidate_id and previous.get("status") == "active":
                previous.update({
                    "status": "failed",
                    "reason": "chunking process ended before recording an outcome",
                    "completed_at": now,
                })
                _event(
                    state,
                    "chunking_attempt_failed",
                    attempt_id=previous.get("attempt_id"),
                    candidate_id=candidate_id,
                    author=previous.get("author"),
                    reason=previous["reason"],
                )

        ordinal = chunking_attempt_count(state, candidate_id, author) + 1
        attempt = {
            "attempt_id": _id("chunking-attempt"),
            "candidate_id": candidate_id,
            "author": _text(author, "author", 200),
            "attempt": ordinal,
            "status": "active",
            "reason": "",
            "started_at": now,
            "completed_at": None,
        }
        state["chunking_attempts"].append(attempt)
        state["chunking_attempts"] = state["chunking_attempts"][-500:]
        _event(
            state,
            "chunking_attempt_started",
            attempt_id=attempt["attempt_id"],
            candidate_id=candidate_id,
            author=attempt["author"],
            attempt=ordinal,
        )
    return dict(attempt)


def finish_chunking_attempt(
    forum_dir: Path,
    attempt_id: str,
    *,
    succeeded: bool,
    reason: str = "",
    chunk_count: int = 0,
) -> dict:
    """Record the deterministic outcome of a previously allocated attempt."""
    with transaction(forum_dir) as state:
        attempt = next((
            item for item in state.get("chunking_attempts", [])
            if item.get("attempt_id") == attempt_id
        ), None)
        if attempt is None:
            raise ValueError("unknown chunking attempt")
        if attempt.get("status") != "active":
            return dict(attempt)
        failure = _text(reason, "reason", required=not succeeded)
        attempt.update({
            "status": "succeeded" if succeeded else "failed",
            "reason": failure,
            "chunk_count": max(0, int(chunk_count)),
            "completed_at": time.time(),
        })
        _event(
            state,
            "chunking_attempt_succeeded" if succeeded else "chunking_attempt_failed",
            attempt_id=attempt_id,
            candidate_id=attempt["candidate_id"],
            author=attempt["author"],
            attempt=attempt["attempt"],
            reason=failure,
            chunk_count=attempt["chunk_count"],
        )
    return dict(attempt)


def _reopen_solution_in_tx(state: dict, author: str, reason: str) -> None:
    accepted = state["solution"].get("accepted_candidate")
    state["solution"] = {
        "revision": int(state["solution"].get("revision", 0)) + 1,
        "status": "open",
        "current_candidate": None,
        "accepted_candidate": None,
        "previous_candidate": accepted,
        "reopen_reason": _text(reason, "reason"),
    }
    state["formalization"] = {
        "revision": int(state["formalization"].get("revision", 0)) + 1,
        "status": "waiting",
        "solution_candidate": None,
        "solution_sha256": "",
        "main_sha": state["formalization"].get("main_sha", ""),
        "contract": None,
        "requirements": [],
        "review_snapshot": None,
        "pending_verdict_id": None,
    }
    # A new informal-paper revision invalidates the entire old formalization,
    # including tasks that had compiled against the superseded paper.
    for task in state["formal_tasks"].values():
        task["status"] = "superseded"
    for task in state["informal_tasks"].values():
        if task.get("status") not in {"superseded", "cancelled"}:
            task["status"] = "superseded"
    for issue in state["review_issues"].values():
        if issue.get("status") == "open":
            issue["status"] = "superseded"
    for strategy in state["strategies"].values():
        if strategy.get("status") in _ACTIVE_STRATEGIES:
            strategy["status"] = "cancelled"
    state["phase"] = "solving"
    _event(state, "solution_reopened", author=author, reason=reason,
           previous_candidate=accepted, solution_revision=state["solution"]["revision"])


def reopen_solution(forum_dir: Path, author: str, reason: str) -> dict:
    with transaction(forum_dir) as state:
        _reopen_solution_in_tx(state, author, reason)
    return load_state(forum_dir)


def request_rechunk(forum_dir: Path, author: str, reason: str) -> dict:
    """Invalidate an encoding contract without changing the accepted mathematics."""
    author = _text(author, "author", 100)
    reason = _text(reason, "reason")
    with transaction(forum_dir) as state:
        if state["phase"] not in {"formalizing", "critic"}:
            raise ValueError("re-chunking can only be requested during formalizing or critic")
        formal = state["formalization"]
        if state["solution"].get("accepted_candidate") != formal.get("solution_candidate"):
            raise ValueError("re-chunking requires the current accepted solution")
        _invalidate_review(state)
        formal.update({"revision": int(formal.get("revision", 0)) + 1,
                       "status": "waiting", "contract": None, "requirements": [],
                       "rechunk_reason": reason})
        for task in state["formal_tasks"].values():
            task["status"] = "superseded"
        for candidate in state["formal_candidates"].values():
            if candidate.get("status") in {"submitted", "merging"}:
                candidate["status"] = "superseded"
        for strategy in state["strategies"].values():
            if strategy.get("phase") == "formalizing" and strategy.get("status") in _ACTIVE_STRATEGIES:
                strategy["status"] = "cancelled"
        for attempt in state["chunking_attempts"]:
            if attempt.get("candidate_id") == formal.get("solution_candidate"):
                attempt["obsolete"] = True
        state["phase"] = "chunking"
        _event(state, "contract_reopened", author=author, reason=reason,
               formalization_revision=formal["revision"], solution_candidate=formal.get("solution_candidate"))
    return load_state(forum_dir)


def initialize_formal_tasks(
    forum_dir: Path,
    chunks: list[dict],
    *,
    solution_candidate: str,
    solution_sha256: str,
    main_sha: str,
    requirements: list[dict],
    contract: dict,
) -> dict:
    with transaction(forum_dir) as state:
        if state["phase"] != "chunking":
            raise ValueError("formal tasks can only be initialized during chunking")
        if state["solution"].get("accepted_candidate") != solution_candidate:
            raise ValueError("formal task graph does not target the accepted solution candidate")
        accepted = state["solution_candidates"].get(solution_candidate, {})
        if accepted.get("sha256") != solution_sha256:
            raise ValueError("formal task graph does not match the accepted solution SHA-256")
        if not _FULL_SHA_RE.fullmatch(main_sha):
            raise ValueError("formalization requires a full main commit")
        if not chunks:
            raise ValueError("formalization DAG contains no chunks")
        ids = [str(chunk.get("id") or "").strip() for chunk in chunks]
        if any(not item for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("formalization chunks require unique nonempty ids")
        tasks = {}
        declarations = set()
        source_refs = {f"paper:{solution_candidate}"} | {
            item["result_id"] for item in accepted.get("components", [])
        }
        for chunk in chunks:
            task_id = str(chunk["id"])
            dependencies = [str(item) for item in chunk.get("dependencies", [])]
            unknown = [item for item in dependencies if item not in ids]
            if unknown:
                raise ValueError(f"task {task_id} has unknown dependencies: {', '.join(unknown)}")
            lean_decl = str(chunk.get("lean_decl") or "").strip()
            if not lean_decl:
                raise ValueError(f"task {task_id} is missing lean_decl")
            if lean_decl in declarations:
                raise ValueError("formalization chunks require unique lean_decl values")
            declarations.add(lean_decl)
            sources = _reference_list(chunk.get("source_components"), "source_components")
            if set(sources) - source_refs:
                raise ValueError(f"task {task_id} has unknown source components")
            tasks[task_id] = {
                "task_id": task_id,
                "title": str(chunk.get("title") or task_id),
                "description": str(chunk.get("summary") or chunk.get("description") or ""),
                "lean_decl": lean_decl,
                "lean_file": str(chunk.get("lean_file") or ""),
                "dependencies": dependencies,
                "source_components": sources,
                "status": "pending",
                "accepted_candidate": None,
            }
        remaining = set(tasks)
        while remaining:
            ready = {task_id for task_id in remaining
                     if not (set(tasks[task_id]["dependencies"]) & remaining)}
            if not ready:
                raise ValueError("formalization DAG contains a dependency cycle")
            remaining -= ready
        requirements = _validate_requirements(requirements, tasks, source_refs)
        if not isinstance(contract, dict) or not _ARTIFACT_SHA_RE.fullmatch(str(contract.get("sha256") or "")):
            raise ValueError("formalization requires a controller-built contract SHA-256")
        if not isinstance(contract.get("targets"), dict) or set(contract["targets"]) != declarations:
            raise ValueError("formalization contract targets must exactly match chunk declarations")
        if not isinstance(contract.get("environment"), dict):
            raise ValueError("formalization contract requires its build environment")
        if "requirements" in contract and _validate_requirements(contract["requirements"], tasks, source_refs) != requirements:
            raise ValueError("requirements ledger differs from the frozen formalization contract")
        revision = int(state["formalization"].get("revision", 0)) + 1
        state["formal_tasks"] = tasks
        state["formal_candidates"] = {}
        state["formalization"] = {
            "revision": revision,
            "status": "active",
            "solution_candidate": solution_candidate,
            "solution_sha256": solution_sha256,
            "main_sha": main_sha,
            "requirements": deepcopy(requirements),
            "contract": deepcopy(contract),
            "review_snapshot": None,
            "pending_verdict_id": None,
        }
        state["phase"] = "formalizing"
        _event(state, "formalization_initialized", formalization_revision=revision,
               tasks=ids, solution_candidate=solution_candidate)
    return load_state(forum_dir)


def _reference_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() or item != item.strip() for item in value
    ):
        raise ValueError(f"{field} requires a nonempty list of nonempty references")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} contains duplicate references")
    return list(value)


def _validate_requirements(requirements, tasks: dict, source_refs: set[str]) -> list[dict]:
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("formalization requires a nonempty requirements ledger")
    result, ids, covered_sources = [], set(), set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise ValueError("each requirement must be an object")
        requirement_id = _text(requirement.get("id"), "requirement id", 200)
        if requirement_id in ids:
            raise ValueError("requirements require unique nonempty ids")
        ids.add(requirement_id)
        statement = _text(requirement.get("statement"), "requirement statement", 16000)
        sources = _reference_list(requirement.get("source_components"), "requirement source_components")
        task_ids = _reference_list(requirement.get("tasks"), "requirement tasks")
        if set(sources) - source_refs:
            raise ValueError(f"requirement {requirement_id} has unknown source components")
        if set(task_ids) - tasks.keys():
            raise ValueError(f"requirement {requirement_id} has unknown tasks")
        if set(sources) - {source for task_id in task_ids for source in tasks[task_id]["source_components"]}:
            raise ValueError(f"requirement {requirement_id} sources are not covered by its tasks")
        covered_sources.update(sources)
        result.append({"id": requirement_id, "statement": statement,
                       "source_components": sources, "tasks": task_ids})
    if source_refs - covered_sources:
        raise ValueError("requirements ledger does not cover every accepted source component")
    return result


def ready_formal_tasks(state: dict) -> list[dict]:
    tasks = state.get("formal_tasks", {})
    return [
        task for task in tasks.values()
        if task.get("status") == "pending"
        and all(tasks.get(dep, {}).get("status") == "complete"
                for dep in task.get("dependencies", []))
    ]


def submit_formal_candidate(
    forum_dir: Path,
    strategy_id: str,
    author: str,
    task_id: str,
    commit_sha: str,
    base_main_sha: str,
    diff_sha256: str,
    *,
    notes: str = "",
    supersedes: str = "",
) -> dict:
    commit_sha = commit_sha.casefold()
    if not _FULL_SHA_RE.fullmatch(commit_sha):
        raise ValueError("commit_sha must be a full 40-character commit")
    if not _FULL_SHA_RE.fullmatch(base_main_sha.casefold()):
        raise ValueError("base_main_sha must be a full 40-character commit")
    if not _ARTIFACT_SHA_RE.fullmatch(diff_sha256):
        raise ValueError("diff_sha256 must be a full SHA-256")
    with transaction(forum_dir) as state:
        if state["phase"] != "formalizing":
            raise ValueError("formal candidates can only be submitted during formalizing")
        task = state["formal_tasks"].get(task_id)
        if not task or task.get("status") == "complete":
            raise ValueError("formal task is unknown or already complete")
        strategy = state["strategies"].get(strategy_id)
        if not strategy or strategy.get("phase") != "formalizing" or strategy.get("target") != task_id:
            raise ValueError("candidate strategy does not target this formal task")
        if author != strategy.get("owner") and author not in strategy.get("assistants", []):
            raise ValueError("candidate author does not own or assist the strategy")
        for existing in state["formal_candidates"].values():
            if existing.get("task_id") == task_id and existing.get("status") in {"submitted", "merging"}:
                if existing.get("commit_sha") == commit_sha:
                    return {"status": "submitted", "candidate": existing, "idempotent": True}
                return {"status": "conflict", "candidate": existing}
        if supersedes and supersedes not in state["formal_candidates"]:
            raise ValueError(f"unknown superseded candidate '{supersedes}'")
        candidate_id = _id("formal")
        candidate = {
            "candidate_id": candidate_id,
            "task_id": task_id,
            "strategy_id": strategy_id,
            "author": author,
            "commit_sha": commit_sha,
            "base_main_sha": base_main_sha.casefold(),
            "diff_sha256": diff_sha256,
            "solution_candidate": state["formalization"]["solution_candidate"],
            "solution_sha256": state["formalization"]["solution_sha256"],
            "formalization_revision": state["formalization"]["revision"],
            "source_components": list(task.get("source_components", [])),
            "notes": _text(notes, "notes", 2000, required=False),
            "supersedes": supersedes or None,
            "status": "submitted",
            "created_at": time.time(),
        }
        state["formal_candidates"][candidate_id] = candidate
        task["status"] = "candidate_pending"
        for item in state["strategies"].values():
            if item.get("phase") == "formalizing" and item.get("target") == task_id and item.get("status") in _ACTIVE_STRATEGIES:
                item["paused_from"] = item["status"]
                item["status"] = "paused"
        _event(state, "formal_candidate_submitted", candidate_id=candidate_id,
               task_id=task_id, author=author)
    return {"status": "submitted", "candidate": candidate}


def begin_formal_merge(forum_dir: Path, candidate_id: str) -> dict:
    with transaction(forum_dir) as state:
        candidate = state["formal_candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown formal candidate '{candidate_id}'")
        if (state["phase"] != "formalizing"
                or candidate.get("formalization_revision") != state["formalization"].get("revision")
                or candidate.get("solution_candidate") != state["formalization"].get("solution_candidate")):
            return {"candidate": candidate, "conflict": True}
        if candidate.get("status") == "merged":
            return {"candidate": candidate, "idempotent": True}
        if candidate.get("status") != "submitted":
            return {"candidate": candidate, "conflict": True}
        candidate["status"] = "merging"
        _event(state, "formal_candidate_merging", candidate_id=candidate_id,
               task_id=candidate["task_id"])
    return {"candidate": candidate}


def finish_formal_merge(
    forum_dir: Path,
    candidate_id: str,
    *,
    success: bool,
    main_sha: str = "",
    error: str = "",
    build: dict | None = None,
    verification: dict | None = None,
) -> dict:
    with transaction(forum_dir) as state:
        candidate = state["formal_candidates"].get(candidate_id)
        if (state["phase"] != "formalizing" or not candidate
                or candidate.get("formalization_revision") != state["formalization"].get("revision")
                or candidate.get("solution_candidate") != state["formalization"].get("solution_candidate")
                or candidate.get("solution_sha256") != state["formalization"].get("solution_sha256")):
            return {"candidate": candidate, "stale": True}
        if not candidate or candidate.get("status") != "merging":
            raise ValueError("formal candidate is not being merged")
        task = state["formal_tasks"][candidate["task_id"]]
        candidate["build"] = build or {}
        candidate["verification"] = verification or {}
        candidate["updated_at"] = time.time()
        if success:
            if not _FULL_SHA_RE.fullmatch(main_sha.casefold()):
                raise ValueError("successful merge requires a full main commit")
            if (not verification or verification.get("status") != "passed"
                    or verification.get("contract_sha256") != (state["formalization"].get("contract") or {}).get("sha256")
                    or not state["formalization"].get("contract")):
                raise ValueError("successful merge requires verification against the current formal contract")
            candidate["status"] = "merged"
            candidate["main_sha"] = main_sha.casefold()
            task["status"] = "complete"
            task["accepted_candidate"] = candidate_id
            state["formalization"]["main_sha"] = main_sha.casefold()
            _invalidate_review(state)
            for obstacle in state["obstacles"].values():
                if obstacle.get("status") == "open" and obstacle.get("target") == task["task_id"]:
                    obstacle["status"] = "resolved"
                    obstacle["resolved_by"] = candidate_id
            for strategy in state["strategies"].values():
                if strategy.get("phase") == "formalizing" and strategy.get("target") == task["task_id"] and strategy.get("status") in _ACTIVE_STRATEGIES:
                    strategy["status"] = "succeeded" if strategy["strategy_id"] == candidate["strategy_id"] else "cancelled"
                    strategy.pop("paused_from", None)
            _event(state, "formal_candidate_merged", candidate_id=candidate_id,
                   task_id=task["task_id"], main_sha=main_sha.casefold())
        else:
            candidate["status"] = "failed"
            candidate["error"] = _text(error, "error", 4000, required=False)
            task["status"] = "pending"
            for strategy in state["strategies"].values():
                if strategy.get("phase") == "formalizing" and strategy.get("target") == task["task_id"] and strategy.get("status") == "paused":
                    strategy["status"] = strategy.pop("paused_from", "registered")
            _event(state, "formal_candidate_failed", candidate_id=candidate_id,
                   task_id=task["task_id"], error=error[:1000])
    return {"candidate": candidate, "task": task}


def all_formal_tasks_complete(state: dict) -> bool:
    tasks = state.get("formal_tasks", {})
    return bool(tasks) and all(task.get("status") == "complete" for task in tasks.values())


def _invalidate_review(state: dict) -> None:
    """Keep old snapshots and verdicts as history, never as live approval evidence."""
    formal = state["formalization"]
    formal["review_snapshot"] = None
    formal["pending_verdict_id"] = None


def _report_digest(report: dict | list) -> str:
    return hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_snapshot_binding(state: dict, report: dict, *, require_passed: bool) -> None:
    formal = state["formalization"]
    contract = formal.get("contract")
    if not isinstance(contract, dict) or not formal.get("requirements"):
        raise ValueError("formalization is missing its immutable contract or requirements; re-chunk it")
    if "requirements" in contract:
        source_refs = {f"paper:{formal.get('solution_candidate')}"} | {
            item["result_id"] for item in state["solution_candidates"].get(formal.get("solution_candidate"), {}).get("components", [])
        }
        if _validate_requirements(contract["requirements"], state["formal_tasks"], source_refs) != formal["requirements"]:
            raise ValueError("requirements ledger differs from the frozen formalization contract")
    if not isinstance(report, dict) or not isinstance(report.get("snapshot_id"), str) or not report["snapshot_id"].strip():
        raise ValueError("review requires a controller-verified snapshot_id")
    if type(report.get("passed")) is not bool:
        raise ValueError("review snapshot requires a boolean passed result")
    if require_passed and report["passed"] is not True:
        raise ValueError("review snapshot did not pass the deterministic checks")
    for field, pattern in (("main_sha", _FULL_SHA_RE), ("source_sha256", _ARTIFACT_SHA_RE),
                           ("solution_sha256", _ARTIFACT_SHA_RE), ("contract_sha256", _ARTIFACT_SHA_RE)):
        if not isinstance(report.get(field), str) or not pattern.fullmatch(report[field]):
            raise ValueError(f"review snapshot requires a full {field}")
    expected = {"main_sha": formal.get("main_sha"),
                "solution_candidate": formal.get("solution_candidate"),
                "solution_sha256": formal.get("solution_sha256"),
                "formalization_revision": formal.get("revision"),
                "contract_sha256": contract.get("sha256")}
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("review snapshot is stale for the current formalization")
    solution_id = formal.get("solution_candidate")
    if state["solution"].get("status") != "accepted" or state["solution"].get("accepted_candidate") != solution_id:
        raise ValueError("review snapshot no longer targets the accepted solution")
    if state["solution_candidates"].get(solution_id, {}).get("sha256") != formal.get("solution_sha256"):
        raise ValueError("review snapshot has stale accepted solution bytes")
    tasks = state["formal_tasks"]
    accepted = {task_id: task.get("accepted_candidate") for task_id, task in tasks.items()}
    if report.get("accepted_candidates") != accepted:
        raise ValueError("review snapshot has stale accepted candidates")
    expected_declarations = {task["lean_decl"]: task_id for task_id, task in tasks.items()}
    if report.get("declarations") != expected_declarations:
        raise ValueError("review snapshot declarations do not match the formal tasks")
    if set(contract.get("targets", {})) != set(expected_declarations):
        raise ValueError("formalization contract does not match the formal tasks")
    if require_passed and not all_formal_tasks_complete(state):
        raise ValueError("review requires all formal tasks to be complete")
    for task_id, candidate_id in accepted.items():
        if candidate_id is None and not require_passed:
            continue
        candidate = state["formal_candidates"].get(candidate_id, {})
        if (candidate.get("status") != "merged" or candidate.get("task_id") != task_id
                or candidate.get("solution_candidate") != solution_id
                or candidate.get("solution_sha256") != formal.get("solution_sha256")
                or candidate.get("formalization_revision") != formal.get("revision")):
            raise ValueError("review snapshot requires current merged candidates for every task")


def record_review_snapshot(forum_dir: Path, report: dict) -> dict:
    """Record controller checks; this internal API is not exposed to workers via MCP."""
    report = deepcopy(report)
    with transaction(forum_dir) as state:
        if state["phase"] not in {"formalizing", "critic"}:
            raise ValueError("review snapshots can only be recorded for an active formalization")
        _validate_snapshot_binding(state, report, require_passed=report.get("passed") is True)
        existing = state["review_snapshots"].get(report["snapshot_id"])
        if existing is not None and existing != report:
            raise ValueError("snapshot_id already identifies a different immutable report")
        previous = state["formalization"].get("review_snapshot")
        if previous != report:
            _invalidate_review(state)
            if state["formalization"].get("status") == "approval_pending":
                state["formalization"]["status"] = "review"
        state["review_snapshots"][report["snapshot_id"]] = report
        state["formalization"]["review_snapshot"] = report
        _event(state, "review_snapshot_recorded", snapshot_id=report["snapshot_id"], passed=report["passed"])
    return load_state(forum_dir)


def reopen_after_machine_failure(forum_dir: Path, report: dict) -> dict:
    """Atomically retain failed controller evidence and require all tasks to be repaired."""
    report = deepcopy(report)
    with transaction(forum_dir) as state:
        if state["phase"] not in {"formalizing", "critic"}:
            raise ValueError("machine failure can only reopen an active formalization")
        _validate_snapshot_binding(state, report, require_passed=False)
        if report.get("passed") is not False:
            raise ValueError("machine failure reopening requires a failed report")
        existing = state["review_snapshots"].get(report["snapshot_id"])
        if existing is not None and existing != report:
            raise ValueError("snapshot_id already identifies a different immutable report")
        state["review_snapshots"][report["snapshot_id"]] = report
        for task in state["formal_tasks"].values():
            candidate_id = task.get("accepted_candidate")
            if candidate_id in state["formal_candidates"]:
                state["formal_candidates"][candidate_id]["status"] = "superseded"
            task["status"] = "pending"
            task["accepted_candidate"] = None
        for strategy in state["strategies"].values():
            if strategy.get("phase") == "formalizing" and strategy.get("status") in _ACTIVE_STRATEGIES:
                strategy["status"] = "cancelled"
        _invalidate_review(state)
        state["formalization"]["status"] = "active"
        state["formalization"]["machine_review_failure"] = report["snapshot_id"]
        state["phase"] = "formalizing"
        obstacle_id = _id("obstacle")
        state["obstacles"][obstacle_id] = {
            "obstacle_id": obstacle_id, "phase": "formalizing", "target": "", "author": "Unity",
            "goal_state": "Deterministic final review failed: " + json.dumps(report.get("issues", [])),
            "tried": "controller preflight", "hypothesis": "Repair against the frozen contract, or request_rechunk for an encoding correction.",
            "status": "open", "created_at": time.time(), "snapshot_id": report["snapshot_id"],
        }
        _event(state, "machine_review_failed", snapshot_id=report["snapshot_id"],
               issues=report.get("issues", []), reopened_tasks=list(state["formal_tasks"]))
    return load_state(forum_dir)


def _current_snapshot(state: dict, snapshot_id: str) -> dict:
    report = state["formalization"].get("review_snapshot")
    if not isinstance(report, dict) or report.get("snapshot_id") != snapshot_id:
        raise ValueError("semantic review refers to a stale or unknown snapshot")
    if state.get("review_snapshots", {}).get(snapshot_id) != report:
        raise ValueError("review snapshot differs from its immutable controller report")
    _validate_snapshot_binding(state, report, require_passed=True)
    return report


def begin_critic(forum_dir: Path) -> dict:
    with transaction(forum_dir) as state:
        if state["phase"] not in {"formalizing", "critic"}:
            raise ValueError("critic can only start for an active formalization")
        if not all_formal_tasks_complete(state):
            raise ValueError("critic cannot start before all formal tasks are complete")
        report = state["formalization"].get("review_snapshot") or {}
        _current_snapshot(state, report.get("snapshot_id", ""))
        if state["formalization"].get("status") == "approval_pending":
            raise ValueError("critic approval is awaiting controller finalization")
        state["formalization"]["status"] = "review"
        state["phase"] = "critic"
        _event(state, "critic_started", main_sha=state["formalization"].get("main_sha", ""))
    return load_state(forum_dir)


def submit_critic_verdict(
    forum_dir: Path,
    author: str,
    verdict: str,
    summary: str,
    *,
    review: dict,
    reopen_tasks: list[str] | None = None,
    evidence: str = "",
) -> dict:
    verdict = verdict.strip().casefold()
    if verdict not in {"approved", "lean_reopen", "reopen_solving"}:
        raise ValueError("verdict must be approved, lean_reopen, or reopen_solving")
    review = SemanticReview.model_validate(review).model_dump()
    with transaction(forum_dir) as state:
        if state["phase"] != "critic" or state["formalization"].get("status") != "review":
            raise ValueError("critic verdicts are only accepted during critic")
        report = _current_snapshot(state, review["snapshot_id"])
        _validate_semantic_review(state, review, approved=verdict == "approved")
        task_ids = list(dict.fromkeys(reopen_tasks or []))
        if verdict == "approved" and task_ids:
            raise ValueError("approved verdict cannot reopen formal tasks")
        if verdict == "lean_reopen":
            if not task_ids:
                raise ValueError("lean_reopen requires at least one formal task")
            unknown = [item for item in task_ids if item not in state["formal_tasks"]]
            if unknown:
                raise ValueError("unknown reopen tasks: " + ", ".join(unknown))
        item = {
            "verdict_id": _id("verdict"),
            "author": _text(author, "author", 100),
            "verdict": verdict,
            "summary": _text(summary, "summary"),
            "reopen_tasks": task_ids,
            "evidence": _text(evidence, "evidence", 4000, required=False),
            "review": review,
            "snapshot_id": report["snapshot_id"],
            "snapshot_sha256": _report_digest(report),
            "requirements_sha256": _report_digest(state["formalization"]["requirements"]),
            "main_sha": state["formalization"].get("main_sha", ""),
            "timestamp": time.time(),
        }
        state["critic_verdicts"].append(item)
        if verdict == "approved":
            state["formalization"]["status"] = "approval_pending"
            state["formalization"]["pending_verdict_id"] = item["verdict_id"]
        elif verdict == "lean_reopen":
            reopened = set(task_ids)
            while True:
                dependents = {task_id for task_id, task in state["formal_tasks"].items()
                              if set(task.get("dependencies", [])) & reopened}
                if dependents <= reopened:
                    break
                reopened |= dependents
            item["reopened_tasks"] = sorted(reopened)
            for task_id in reopened:
                task = state["formal_tasks"][task_id]
                accepted = task.get("accepted_candidate")
                if accepted in state["formal_candidates"]:
                    state["formal_candidates"][accepted]["status"] = "superseded"
                task["status"] = "pending"
                task["accepted_candidate"] = None
            for strategy in state["strategies"].values():
                if (strategy.get("phase") == "formalizing" and strategy.get("target") in reopened
                        and strategy.get("status") in _ACTIVE_STRATEGIES):
                    strategy["status"] = "cancelled"
            _invalidate_review(state)
            state["formalization"]["status"] = "active"
            state["phase"] = "formalizing"
        else:
            _reopen_solution_in_tx(state, author, summary)
        _event(state, "critic_verdict", verdict_id=item["verdict_id"], author=author,
               verdict=verdict, reopen_tasks=task_ids)
    return {"verdict": item, "state": load_state(forum_dir)}


def _validate_semantic_review(state: dict, review: dict, *, approved: bool) -> None:
    ledger = {item["id"]: item for item in state["formalization"]["requirements"]}
    seen = set()
    declarations = state["formalization"]["review_snapshot"]["declarations"]
    for entry in review["requirements"]:
        requirement_id = entry["requirement_id"]
        if requirement_id not in ledger:
            raise ValueError(f"unknown reviewed requirement '{requirement_id}'")
        if requirement_id in seen:
            raise ValueError(f"duplicate reviewed requirement '{requirement_id}'")
        seen.add(requirement_id)
        if not entry["rationale"].strip():
            raise ValueError("requirement review rationale is required")
        refs = entry["declarations"]
        if len(refs) != len(set(refs)):
            raise ValueError("requirement review has duplicate declaration references")
        for declaration in refs:
            if declaration not in declarations:
                raise ValueError(f"unknown reviewed declaration '{declaration}'")
            if declarations[declaration] not in ledger[requirement_id]["tasks"]:
                raise ValueError(f"declaration '{declaration}' is unrelated to requirement '{requirement_id}'")
        if approved and (entry["status"] != "pass" or not refs):
            raise ValueError("approval requires every requirement to pass with declaration references")
    if approved and seen != set(ledger):
        raise ValueError("approval requires exact coverage of every requirement")


def complete_critic_review(forum_dir: Path, snapshot_id: str, verdict_id: str) -> dict:
    """CAS approval after the controller rechecks source bytes under the merge lock."""
    with transaction(forum_dir) as state:
        formal = state["formalization"]
        if (state["phase"] != "critic" or formal.get("status") != "approval_pending"
                or formal.get("pending_verdict_id") != verdict_id):
            raise ValueError("critic approval is no longer pending for this verdict")
        report = _current_snapshot(state, snapshot_id)
        verdict = next((item for item in state["critic_verdicts"] if item.get("verdict_id") == verdict_id), None)
        if (not verdict or verdict.get("verdict") != "approved"
                or verdict.get("snapshot_id") != snapshot_id
                or verdict.get("snapshot_sha256") != _report_digest(report)
                or verdict.get("requirements_sha256") != _report_digest(formal["requirements"])):
            raise ValueError("critic approval evidence no longer matches the current snapshot")
        review = SemanticReview.model_validate(verdict["review"]).model_dump()
        if review["snapshot_id"] != snapshot_id or verdict.get("reopen_tasks"):
            raise ValueError("critic approval does not target this snapshot")
        _validate_semantic_review(state, review, approved=True)
        formal["status"] = "accepted"
        formal["pending_verdict_id"] = None
        formal["accepted_verdict_id"] = verdict_id
        state["phase"] = "complete"
        for obstacle in state["obstacles"].values():
            if obstacle.get("status") == "open" and obstacle.get("phase") != "solving":
                obstacle["status"] = "resolved"
                obstacle["resolved_by"] = verdict_id
        _event(state, "critic_review_completed", snapshot_id=snapshot_id, verdict_id=verdict_id)
    return load_state(forum_dir)


def events_after(state: dict, seen: set[str]) -> list[dict]:
    return [event for event in state.get("events", []) if event.get("event_id") not in seen]
