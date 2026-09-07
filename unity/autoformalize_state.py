"""Authoritative, file-backed coordination state for ``unity autoformalize``.

Supplied source documents enter at chunking, without an English solving or
paper-approval phase. Coordination, formal candidates and snapshot-bound critic
acceptance are owned by this workflow.
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

from .autoformalize_review import SemanticReview


SCHEMA_VERSION = 4
PHASES = {"chunking", "formalizing", "critic", "complete"}
STRATEGY_PHASES = {"formalizing"}
STRATEGY_STATUSES = {"registered", "claimed", "paused", "incorrect", "succeeded", "cancelled"}
FORMAL_STATUSES = {"waiting", "active", "review", "approval_pending", "accepted"}
_ACTIVE_STRATEGIES = {"registered", "claimed", "paused"}
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def state_path(forum_dir: Path) -> Path:
    return Path(forum_dir) / "autoformalize-state.json"


def _default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "run_id": "",
        "pipeline": "autoformalize",
        "phase": "chunking",
        "input_source": None,
        "problem_sha256": "",
        "solution": {
            "revision": 1,
            "status": "not_required",
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
        "strategies", "findings", "obstacles", "questions",
        "solution_candidates", "formal_tasks", "formal_candidates",
        "review_snapshots",
    ):
        if not isinstance(base.get(key), dict):
            base[key] = {}
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
    fd, temporary = tempfile.mkstemp(prefix=".autoformalize-state-", suffix=".json", dir=forum_dir)
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
    with (forum_dir / "autoformalize-state.lock").open("a+") as lock:
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


def author_key(value) -> str:
    """Compare bound roster identities consistently, including legacy records."""
    return str(value or "").strip().casefold()


def participates(strategy: dict, author: str) -> bool:
    identity = author_key(author)
    return bool(identity) and identity in {
        author_key(strategy.get("owner")),
        *(author_key(item) for item in strategy.get("assistants", [])),
    }


def _event(state: dict, kind: str, **fields) -> dict:
    event = {"event_id": _id("event"), "kind": kind, "timestamp": time.time(), **fields}
    state.setdefault("events", []).append(event)
    state["events"] = state["events"][-1000:]
    return event


def formal_source(state: dict) -> dict:
    """Return detached supplied-source provenance, never an informal approval."""
    source = state.get("input_source") or {}
    return deepcopy(source) if source.get("kind") == "supplied_sources" else {}


def source_refs(state: dict) -> set[str]:
    """The exact supplied documents a formalization must cover."""
    return {item["ref_id"] for item in formal_source(state).get("source_refs", [])}


def source_reference_ids(state: dict) -> set[str]:
    """Named accessor for source-reference identifiers used by worker routing."""
    return source_refs(state)


def initialize_source(forum_dir: Path, problem_sha256: str, main_sha: str,
                      source: dict, *, reset: bool = False) -> dict:
    """Bind supplied documents and start at chunking, without an English review gate."""
    if not _ARTIFACT_SHA_RE.fullmatch(problem_sha256):
        raise ValueError("problem_sha256 must be a full SHA-256")
    if not _FULL_SHA_RE.fullmatch(main_sha):
        raise ValueError("formalization requires a full main commit")
    if (not isinstance(source, dict) or source.get("kind") != "supplied_sources"
            or not _ARTIFACT_SHA_RE.fullmatch(str(source.get("sha256") or ""))
            or source.get("candidate_id") != "source-" + source["sha256"]):
        raise ValueError("autoformalize requires an immutable supplied-source identity")
    refs = source.get("source_refs")
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(item, dict)
                   or not isinstance(item.get("ref_id"), str) or not item["ref_id"].strip()
                   or not isinstance(item.get("path"), str) or not item["path"].strip()
                   or not _ARTIFACT_SHA_RE.fullmatch(str(item.get("sha256") or ""))
                   for item in refs)
            or len({item["ref_id"] for item in refs}) != len(refs)):
        raise ValueError("supplied source requires unique, byte-bound source references")

    def identity(record: dict) -> dict:
        return {"kind": record.get("kind"), "candidate_id": record.get("candidate_id"),
                "sha256": record.get("sha256"),
                "source_refs": sorted(
                    ({key: item.get(key) for key in ("ref_id", "path", "sha256")}
                     for item in record.get("source_refs", [])), key=lambda item: item["ref_id"],
                )}

    with transaction(forum_dir) as state:
        if reset or not state.get("run_id"):
            state.clear()
            state.update(_default_state())
            state.update(pipeline="autoformalize", run_id=_id("autoformalize"),
                         phase="chunking", problem_sha256=problem_sha256,
                         input_source=deepcopy(source))
            state["solution"]["status"] = "not_required"
            state["formalization"]["main_sha"] = main_sha
            _event(state, "supplied_source_bound", source_id=source["candidate_id"],
                   sha256=source["sha256"], problem_sha256=problem_sha256, main_sha=main_sha)
        elif state.get("pipeline") != "autoformalize":
            raise ValueError("this state does not belong to autoformalize")
        elif state.get("problem_sha256") != problem_sha256:
            raise ValueError("UNITY.md changed since this autoformalize run began; start a fresh run")
        elif identity(state.get("input_source") or {}) != identity(source):
            raise ValueError("supplied source changed since this autoformalize run began; start a fresh run")
    return load_state(forum_dir)


def set_phase(forum_dir: Path, phase: str, *, reason: str = "") -> dict:
    if phase not in PHASES:
        raise ValueError(f"unknown autoformalize phase '{phase}'")
    with transaction(forum_dir) as state:
        previous = state.get("phase")
        state["phase"] = phase
        _event(state, "phase_changed", previous=previous, phase=phase, reason=reason)
    return load_state(forum_dir)


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
            raise ValueError("strategies can only be registered during formalizing")
        revision = state["formalization"]["revision"]
        if target:
            collection = state["formal_tasks"]
            if target not in collection:
                raise ValueError(f"unknown {phase} target '{target}'")
            expected_status = "pending"
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
        revision = state["formalization"]["revision"]
        if strategy.get("phase_revision") != revision:
            raise ValueError("strategy belongs to an obsolete revision")
        active_owned = next((
            item for item in state["strategies"].values()
            if item.get("strategy_id") != strategy_id
            and item.get("phase") == strategy["phase"]
            and author_key(item.get("owner")) == author_key(author)
            and item.get("status") == "claimed"
        ), None)
        if active_owned is not None:
            return {
                "status": "agent_busy",
                "strategy": strategy,
                "active_strategy": active_owned,
            }
        if strategy["status"] == "claimed":
            if author_key(strategy["owner"]) == author_key(author):
                return {"status": "claimed", "strategy": strategy, "idempotent": True}
            return {"status": "conflict", "strategy": strategy, "owner": strategy["owner"]}
        if strategy["status"] != "registered":
            raise ValueError(f"strategy is {strategy['status']}, not claimable")
        strategy["status"] = "claimed"
        strategy["owner"] = _text(author, "author", 100)
        strategy["updated_at"] = time.time()
        _event(state, "strategy_claimed", strategy_id=strategy_id, author=author,
               phase=strategy["phase"], target=strategy["target"])
    return {"status": "claimed", "strategy": strategy}


def assist_strategy(forum_dir: Path, strategy_id: str, author: str, contribution: str = "") -> dict:
    with transaction(forum_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy or strategy.get("status") != "claimed":
            raise ValueError("strategy is not actively claimed")
        if not participates(strategy, author):
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
        if author_key(strategy.get("owner")) != author_key(author):
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
            if (
                author_key(strategy.get("owner")) != author_key(author)
                or strategy.get("status") != "claimed"
            ):
                continue
            strategy["owner"] = None
            strategy["status"] = "registered"
            strategy["updated_at"] = time.time()
            released.append(strategy["strategy_id"])
        if released:
            _event(state, "claims_released", author=author, strategies=released, reason=reason)
    return released


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


def chunking_attempt_count(state: dict, candidate_id: str, author: str) -> int:
    """Count attempts already allocated to an agent for one immutable source bundle."""
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
        if formal_source(state).get("candidate_id") != candidate_id:
            raise ValueError("chunking attempt does not target the bound formalization source")

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


def request_rechunk(forum_dir: Path, author: str, reason: str) -> dict:
    """Invalidate an encoding contract without changing the supplied source binding."""
    author = _text(author, "author", 100)
    reason = _text(reason, "reason")
    with transaction(forum_dir) as state:
        if state["phase"] not in {"formalizing", "critic"}:
            raise ValueError("re-chunking can only be requested during formalizing or critic")
        formal = state["formalization"]
        if formal_source(state).get("candidate_id") != formal.get("solution_candidate"):
            raise ValueError("re-chunking requires the current bound formalization source")
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
        source = formal_source(state)
        if source.get("candidate_id") != solution_candidate:
            raise ValueError("formal task graph does not target the bound formalization source")
        if source.get("sha256") != solution_sha256:
            raise ValueError("formal task graph does not match the bound source SHA-256")
        if not _FULL_SHA_RE.fullmatch(main_sha):
            raise ValueError("formalization requires a full main commit")
        if not chunks:
            raise ValueError("formalization DAG contains no chunks")
        ids = [str(chunk.get("id") or "").strip() for chunk in chunks]
        if any(not item for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("formalization chunks require unique nonempty ids")
        tasks = {}
        declarations = set()
        refs = source_refs(state)
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
            if set(sources) - refs:
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
        requirements = _validate_requirements(requirements, tasks, refs)
        if not isinstance(contract, dict) or not _ARTIFACT_SHA_RE.fullmatch(str(contract.get("sha256") or "")):
            raise ValueError("formalization requires a controller-built contract SHA-256")
        if (contract.get("solution_candidate") != source["candidate_id"]
                or contract.get("solution_sha256") != source["sha256"]):
            raise ValueError("formalization contract does not match the bound supplied source")
        if not isinstance(contract.get("targets"), dict) or set(contract["targets"]) != declarations:
            raise ValueError("formalization contract targets must exactly match chunk declarations")
        if not isinstance(contract.get("environment"), dict):
            raise ValueError("formalization contract requires its build environment")
        if "requirements" in contract and _validate_requirements(contract["requirements"], tasks, refs) != requirements:
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
        raise ValueError("requirements ledger does not cover every supplied source component")
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
        if (
            not strategy
            or strategy.get("phase") != "formalizing"
            or strategy.get("target") != task_id
            or strategy.get("phase_revision") != state["formalization"]["revision"]
        ):
            raise ValueError("candidate strategy does not target this formal task/revision")
        if strategy.get("status") not in {"claimed", "paused"}:
            raise ValueError("candidate strategy is no longer active")
        if not participates(strategy, author):
            raise ValueError("candidate author does not own or assist the strategy")
        for existing in state["formal_candidates"].values():
            if (
                existing.get("task_id") == task_id
                and existing.get("formalization_revision") == state["formalization"]["revision"]
                and existing.get("status") in {"submitted", "merging"}
            ):
                if (
                    existing.get("strategy_id") == strategy_id
                    and author_key(existing.get("author")) == author_key(author)
                    and existing.get("commit_sha") == commit_sha
                    and existing.get("base_main_sha") == base_main_sha.casefold()
                    and existing.get("diff_sha256") == diff_sha256
                ):
                    return {"status": "submitted", "candidate": existing, "idempotent": True}
                return {"status": "conflict", "candidate": existing}
        if strategy.get("status") != "claimed" or task.get("status") != "pending":
            raise ValueError("formal strategy/task is not accepting a new candidate")
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
        if _validate_requirements(contract["requirements"], state["formal_tasks"], source_refs(state)) != formal["requirements"]:
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
    source = formal_source(state)
    if not source or source.get("candidate_id") != solution_id:
        raise ValueError("review snapshot no longer targets the bound formalization source")
    if source.get("sha256") != formal.get("solution_sha256"):
        raise ValueError("review snapshot has stale bound source bytes")
    if (contract.get("solution_candidate") != source["candidate_id"]
            or contract.get("solution_sha256") != source["sha256"]):
        raise ValueError("review contract does not match the bound supplied source")
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
    if verdict not in {"approved", "lean_reopen"}:
        raise ValueError("verdict must be approved or lean_reopen")
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
            if obstacle.get("status") == "open":
                obstacle["status"] = "resolved"
                obstacle["resolved_by"] = verdict_id
        _event(state, "critic_review_completed", snapshot_id=snapshot_id, verdict_id=verdict_id)
    return load_state(forum_dir)


def events_after(state: dict, seen: set[str]) -> list[dict]:
    return [event for event in state.get("events", []) if event.get("event_id") not in seen]
