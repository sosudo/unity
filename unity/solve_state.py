"""Authoritative, run-scoped state for the :mod:`unity solve` runtime.

The solve pipeline has two different kinds of work--discovering a mathematical
solution and formalizing the accepted solution--but they belong to one run.  This
module keeps both in ``.unity/solve/state.json`` so a context reset, phase change,
or isolated Git worktree cannot lose the live collaboration state.

All mutations are serialized by ``flock`` and persisted with ``os.replace``.
Candidate identities are immutable: a changed paper, diff, or source tree is a new
candidate rather than an edit to an existing record.
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
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1
EVENT_LIMIT = 2_000

RUN_STAGES = {
    "uninitialized", "solving", "solution_review", "chunking", "formalizing",
    "critic", "complete", "stopped", "exhausted", "failed",
}
RESUMABLE_STAGES = {"solving", "solution_review", "chunking", "formalizing", "critic"}
OUTCOMES = {None, "complete", "stopped", "exhausted", "failed"}
STRATEGY_STAGES = {"solving", "formalizing"}
STRATEGY_STATUSES = {
    "registered", "claimed", "paused", "succeeded", "incorrect", "cancelled",
    "superseded",
}
SUBGOAL_STATUSES = {"open", "solved", "blocked", "cancelled", "superseded"}
FORMAL_TASK_STATUSES = {
    "pending", "claimed", "complete", "failed", "cancelled", "superseded",
}
SOLUTION_CANDIDATE_STATUSES = {
    "submitted", "reviewable", "blocked", "accepted", "rejected", "superseded",
}
FORMAL_CANDIDATE_STATUSES = {
    "submitted", "verifying", "verified", "blocked", "accepted", "failed",
    "rejected", "superseded",
}
OBJECTION_TARGETS = {"solution_candidate", "formal_candidate"}

_ACTIVE_STRATEGIES = {"registered", "claimed", "paused"}
_ACTIVE_FORMAL_TASKS = {"pending", "claimed", "failed"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_FULL_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")


def solve_dir(unity_dir: Path) -> Path:
    """Return the solve run directory from either ``.unity`` or ``.unity/solve``."""
    path = Path(unity_dir)
    return path if path.name == "solve" and path.parent.name == ".unity" else path / "solve"


def state_path(unity_dir: Path) -> Path:
    return solve_dir(unity_dir) / "state.json"


def _default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "run_id": "",
        "stage": "uninitialized",
        "outcome": None,
        "resume_stage": None,
        "initialized_at": None,
        "updated_at": None,
        "problem": {"artifact_id": "", "sha256": ""},
        "gates": {
            "solution": {
                "revision": 0,
                "status": "waiting",
                "repair_mode": "search",
                "source_fix_id": None,
                "accepted_candidate_id": None,
                "artifact_id": "",
                "sha256": "",
                "updated_at": None,
            },
            "formalization": {
                "revision": 0,
                "status": "waiting",
                "solution_candidate_id": None,
                "accepted_candidate_ids": [],
                "integrated_main_sha": "",
                "updated_at": None,
            },
        },
        "subgoals": {},
        "strategies": {},
        "findings": {},
        "solution_candidates": {},
        "formal_tasks": {},
        "formal_candidates": {},
        "objections": {},
        "obstacles": {},
        "source_fixes": {},
        "formalization_verdicts": [],
        "events": [],
    }


def _read_unlocked(unity_dir: Path) -> dict:
    path = state_path(unity_dir)
    if not path.exists():
        return _default_state()
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"solve state exists but is unreadable; preserving it at {path}: {exc}"
        ) from exc
    if not isinstance(state, dict):
        raise RuntimeError(f"solve state at {path} is not a JSON object")
    schema = state.get("schema_version")
    if schema is not None and schema != SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported solve state schema {schema!r} at {path}; expected {SCHEMA_VERSION}"
        )
    base = _default_state()
    base.update(state)
    default_gates = _default_state()["gates"]
    stored_gates = state.get("gates") or {}
    base["gates"] = {
        name: {**defaults, **(stored_gates.get(name) or {})}
        for name, defaults in default_gates.items()
    }
    for name in (
        "subgoals", "strategies", "findings", "solution_candidates", "formal_tasks",
        "formal_candidates", "objections", "obstacles", "source_fixes",
    ):
        if not isinstance(base.get(name), dict):
            base[name] = {}
    for name in ("events", "formalization_verdicts"):
        if not isinstance(base.get(name), list):
            base[name] = []
    return base


def load_state(unity_dir: Path) -> dict:
    """Read the current state; callers must use public mutators to change it."""
    return _read_unlocked(Path(unity_dir))


def _write_unlocked(unity_dir: Path, state: dict) -> None:
    directory = solve_dir(unity_dir)
    directory.mkdir(parents=True, exist_ok=True)
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = time.time()
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, state_path(unity_dir))
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def transaction(unity_dir: Path) -> Iterator[dict]:
    """Lock, mutate, and atomically persist one solve state transaction."""
    unity_dir = Path(unity_dir)
    directory = solve_dir(unity_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the lock outside the resettable run directory. A fresh solve may clear
    # `.unity/solve/`, but must never replace the inode serializing state writers.
    with (directory.parent / "solve-state.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = _read_unlocked(unity_dir)
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                state["revision"] = int(state.get("revision", 0)) + 1
                _write_unlocked(unity_dir, state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _event(state: dict, kind: str, **fields) -> dict:
    event = {
        "event_id": "event-" + uuid.uuid4().hex[:12],
        "kind": kind,
        "timestamp": time.time(),
        **fields,
    }
    state.setdefault("events", []).append(event)
    state["events"] = state["events"][-EVENT_LIMIT:]
    return event


def _id(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex[:12]


def _text(value: str, field: str, limit: int = 4_000, *, required: bool = True) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    if required and not value:
        raise ValueError(f"{field} must be non-empty")
    if len(value) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return value


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _sha256(value: str, field: str = "sha256", *, required: bool = True) -> str:
    value = str(value or "").strip().lower()
    if not value and not required:
        return ""
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a 64-character hexadecimal SHA-256")
    return value


def _require_initialized(state: dict) -> None:
    if not state.get("run_id") or state.get("stage") == "uninitialized":
        raise ValueError("solve state is not initialized")


def _strategy_stage(state: dict, requested: str | None) -> str:
    if requested:
        stage = requested
    else:
        stage = "formalizing" if state.get("stage") in {"formalizing", "critic"} else "solving"
    if stage not in STRATEGY_STAGES:
        raise ValueError("strategy stage must be solving or formalizing")
    return stage


def _gate_revision(state: dict, stage: str) -> int:
    gate = "formalization" if stage == "formalizing" else "solution"
    return int(state["gates"][gate]["revision"])


def _require_active_strategy_stage(state: dict, stage: str) -> int:
    """Require that ``stage`` is the live, writable strategy gate."""
    gate_name = "formalization" if stage == "formalizing" else "solution"
    gate = state["gates"][gate_name]
    if state.get("stage") != stage or gate.get("status") != "open":
        raise ValueError(
            f"{stage} strategies require the active {gate_name} gate to be open"
        )
    return int(gate["revision"])


def _require_active_formalization(state: dict) -> dict:
    """Return the writable formalization gate, rejecting post-interrupt writes."""
    _require_initialized(state)
    gate = state["gates"]["formalization"]
    if state.get("stage") != "formalizing" or gate.get("status") != "open":
        raise ValueError("formal candidate work requires the active formalization gate")
    return gate


def initialize(
    unity_dir: Path,
    problem_artifact_id: str = "",
    problem_sha256: str | bool = "",
    *,
    reset: bool = False,
    problem_path: Path | str | None = None,
) -> dict:
    """Initialize one solve run, or return the existing run idempotently."""
    # Backward-compatible convenience for ``initialize(run_dir, problem_path, reset)``.
    if isinstance(problem_sha256, bool):
        reset, problem_sha256 = problem_sha256, ""
    # Convenience for runtimes: ``initialize(unity, unity / "UNITY.md")``.
    possible_path = Path(problem_artifact_id) if problem_artifact_id else None
    if problem_path is None and possible_path is not None and possible_path.is_file():
        problem_path, problem_artifact_id = possible_path, ""
    source_path = ""
    if problem_path is not None:
        source = Path(problem_path)
        payload = source.read_bytes()
        source_path = str(source)
        problem_sha256 = hashlib.sha256(payload).hexdigest()
    problem_sha256 = _sha256(problem_sha256, "problem_sha256", required=False)
    with transaction(unity_dir) as state:
        if state.get("run_id") and not reset:
            return state
        state.clear()
        state.update(_default_state())
        now = time.time()
        state.update({
            "run_id": "solve-" + uuid.uuid4().hex[:12],
            "stage": "solving",
            "initialized_at": now,
            "problem": {
                "artifact_id": _text(problem_artifact_id, "problem_artifact_id", 300,
                                     required=False),
                "sha256": problem_sha256,
                "source_path": source_path,
            },
        })
        state["gates"]["solution"].update(
            revision=1, status="open", updated_at=now
        )
        _event(state, "solve_initialized", run_id=state["run_id"], gate_revision=1)
    return load_state(unity_dir)


# ── Mathematical and formalization strategies ────────────────────────────────

def register_strategy(
    unity_dir: Path,
    author: str,
    description: str,
    *,
    stage: str | None = None,
    subgoal_id: str = "",
    strategy_family: str = "",
) -> dict:
    author = _text(author, "author", 100)
    description = _text(description, "description")
    family = _key(strategy_family)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        stage = _strategy_stage(state, stage)
        revision = _require_active_strategy_stage(state, stage)
        if subgoal_id and subgoal_id not in state["subgoals"]:
            raise ValueError(f"unknown subgoal '{subgoal_id}'")
        description_key = _key(description)
        for strategy in state["strategies"].values():
            if (
                strategy["stage"] == stage
                and strategy["gate_revision"] == revision
                and strategy.get("subgoal_id", "") == subgoal_id
                and strategy["status"] in _ACTIVE_STRATEGIES
                and (strategy["description_key"] == description_key
                     or (family and strategy.get("strategy_family") == family))
            ):
                basis = "description" if strategy["description_key"] == description_key else "strategy_family"
                return {"status": "duplicate", "duplicate_basis": basis, "strategy": strategy}
        sid = _id("strategy")
        strategy = {
            "strategy_id": sid,
            "stage": stage,
            "gate_revision": revision,
            "subgoal_id": subgoal_id,
            "creator": author,
            "owner": None,
            "assistants": {},
            "description": description,
            "description_key": description_key,
            "strategy_family": family,
            "status": "registered",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        state["strategies"][sid] = strategy
        event = _event(state, "strategy_registered", strategy_id=sid, author=author,
                       stage=stage, gate_revision=revision, subgoal_id=subgoal_id)
    return {"status": "registered", "strategy": strategy, "event": event}


def claim_strategy(unity_dir: Path, strategy_id: str, author: str) -> dict:
    author = _text(author, "author", 100)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        revision = _require_active_strategy_stage(state, strategy["stage"])
        if strategy["gate_revision"] != revision:
            return {"status": "stale", "strategy": strategy}
        if strategy["status"] == "claimed":
            if strategy["owner"] == author:
                return {"status": "claimed", "idempotent": True, "strategy": strategy}
            return {"status": "conflict", "owner": strategy["owner"], "strategy": strategy}
        if strategy["status"] not in {"registered", "paused"}:
            return {"status": strategy["status"], "strategy": strategy}
        strategy.update(owner=author, status="claimed", updated_at=time.time())
        event = _event(state, "strategy_claimed", strategy_id=strategy_id, author=author,
                       stage=strategy["stage"])
    return {"status": "claimed", "strategy": strategy, "event": event}


def assist_strategy(
    unity_dir: Path, strategy_id: str, author: str, contribution: str = ""
) -> dict:
    author = _text(author, "author", 100)
    contribution = _text(contribution, "contribution", 1_000, required=False)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        revision = _require_active_strategy_stage(state, strategy["stage"])
        if strategy["gate_revision"] != revision:
            raise ValueError("cannot assist a strategy from a stale gate revision")
        if strategy["status"] not in {"claimed", "paused"} or not strategy.get("owner"):
            raise ValueError("strategy must have an owner before it can receive assistance")
        if strategy["owner"] == author:
            raise ValueError("the strategy owner cannot also be its assistant")
        repeated = author in strategy["assistants"]
        strategy["assistants"][author] = {
            "contribution": contribution, "updated_at": time.time()
        }
        strategy["updated_at"] = time.time()
        event = _event(state, "strategy_assisted", strategy_id=strategy_id, author=author,
                       owner=strategy["owner"], stage=strategy["stage"])
    return {"status": "already_assisting" if repeated else "assisting",
            "strategy": strategy, "event": event}


def release_strategy(unity_dir: Path, strategy_id: str, author: str, reason: str = "") -> dict:
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 1_000, required=False)
    with transaction(unity_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if strategy.get("owner") != author:
            raise ValueError("only the strategy owner may release it")
        if strategy["status"] != "claimed":
            return {"status": strategy["status"], "idempotent": True, "strategy": strategy}
        strategy.update(owner=None, status="registered", release_reason=reason,
                        updated_at=time.time())
        event = _event(state, "strategy_released", strategy_id=strategy_id, author=author,
                       reason=reason)
    return {"status": "registered", "strategy": strategy, "event": event}


def finish_strategy(
    unity_dir: Path,
    strategy_id: str,
    author: str,
    outcome: str,
    reason: str = "",
    finding_ids: list[str] | None = None,
    evidence: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    if outcome not in {"succeeded", "incorrect", "cancelled"}:
        raise ValueError("strategy outcome must be succeeded, incorrect, or cancelled")
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 2_000, required=False)
    evidence = _text(evidence, "evidence", 4_000, required=False)
    evidence_sha256 = _sha256(evidence_sha256, "evidence_sha256", required=False)
    finding_ids = list(dict.fromkeys(finding_ids or []))
    with transaction(unity_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if strategy.get("owner") != author or strategy.get("status") != "claimed":
            raise ValueError("only the current owner may finish a claimed strategy")
        if strategy["gate_revision"] != _gate_revision(state, strategy["stage"]):
            raise ValueError("cannot finish a strategy from a stale gate revision")
        missing = [fid for fid in finding_ids if fid not in state["findings"]]
        if missing:
            raise ValueError(f"unknown findings: {', '.join(missing)}")
        strategy.update(status=outcome, outcome_reason=reason, finding_ids=finding_ids,
                        evidence=evidence, evidence_artifact_id=evidence_artifact_id,
                        evidence_sha256=evidence_sha256, evidence_bytes=evidence_bytes,
                        updated_at=time.time())
        event = _event(state, "strategy_finished", strategy_id=strategy_id, author=author,
                       outcome=outcome, reason=reason)
    return {"status": outcome, "strategy": strategy, "event": event}


# ── Subgoals, findings, and obstacles ─────────────────────────────────────────

def create_subgoal(
    unity_dir: Path,
    author: str,
    title: str,
    description: str = "",
    *,
    stage: str | None = None,
    parent_id: str = "",
    dependencies: list[str] | None = None,
) -> dict:
    author = _text(author, "author", 100)
    title = _text(title, "title", 300)
    description = _text(description, "description", 4_000, required=False)
    dependencies = list(dict.fromkeys(dependencies or []))
    with transaction(unity_dir) as state:
        _require_initialized(state)
        stage = _strategy_stage(state, stage)
        _require_active_strategy_stage(state, stage)
        if parent_id and parent_id not in state["subgoals"]:
            raise ValueError(f"unknown parent subgoal '{parent_id}'")
        missing = [item for item in dependencies if item not in state["subgoals"]]
        if missing:
            raise ValueError(f"unknown subgoal dependencies: {', '.join(missing)}")
        gid = _id("subgoal")
        subgoal = {
            "subgoal_id": gid, "stage": stage,
            "gate_revision": _gate_revision(state, stage), "author": author,
            "title": title, "description": description, "parent_id": parent_id,
            "dependencies": dependencies, "status": "open",
            "created_at": time.time(), "updated_at": time.time(),
        }
        state["subgoals"][gid] = subgoal
        event = _event(state, "subgoal_created", subgoal_id=gid, author=author,
                       stage=stage, parent_id=parent_id)
    return {"status": "open", "subgoal": subgoal, "event": event}


def set_subgoal_status(
    unity_dir: Path, subgoal_id: str, status: str, author: str, reason: str = ""
) -> dict:
    if status not in SUBGOAL_STATUSES:
        raise ValueError(f"invalid subgoal status '{status}'")
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 2_000, required=False)
    with transaction(unity_dir) as state:
        subgoal = state["subgoals"].get(subgoal_id)
        if not subgoal:
            raise ValueError(f"unknown subgoal '{subgoal_id}'")
        subgoal.update(status=status, status_reason=reason, updated_at=time.time())
        event = _event(state, "subgoal_status_changed", subgoal_id=subgoal_id,
                       author=author, status=status, reason=reason)
    return {"status": status, "subgoal": subgoal, "event": event}


def publish_finding(
    unity_dir: Path,
    author: str,
    kind: str,
    title: str,
    content: str,
    confidence: int,
    *,
    stage: str | None = None,
    strategy_id: str = "",
    subgoal_id: str = "",
    evidence: str = "",
    artifact_id: str = "",
    artifact_sha256: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    author = _text(author, "author", 100)
    kind = _key(_text(kind, "kind", 100))
    title = _text(title, "title", 300)
    content = _text(content, "content")
    evidence = _text(evidence, "evidence", 4_000, required=False)
    artifact_id = _text(artifact_id or evidence_artifact_id, "artifact_id", 300, required=False)
    artifact_sha256 = _sha256(
        artifact_sha256 or evidence_sha256, "artifact_sha256", required=False
    )
    if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
        raise ValueError("confidence must be an integer from 0 through 100")
    if confidence == 100 and not (evidence or artifact_id):
        raise ValueError("confidence 100 requires evidence or an artifact")
    with transaction(unity_dir) as state:
        _require_initialized(state)
        stage = _strategy_stage(state, stage)
        revision = _gate_revision(state, stage)
        if strategy_id:
            strategy = state["strategies"].get(strategy_id)
            if not strategy:
                raise ValueError(f"unknown strategy '{strategy_id}'")
            if (strategy.get("stage") != stage
                    or strategy.get("gate_revision") != revision):
                raise ValueError("finding strategy belongs to a stale or wrong-stage gate")
        if subgoal_id and subgoal_id not in state["subgoals"]:
            raise ValueError(f"unknown subgoal '{subgoal_id}'")
        title_key = _key(title)
        for finding in state["findings"].values():
            if (finding["status"] == "active" and finding["stage"] == stage
                    and finding["gate_revision"] == revision and finding["kind"] == kind
                    and finding["title_key"] == title_key
                    and finding.get("subgoal_id", "") == subgoal_id):
                return {"status": "duplicate", "finding": finding}
        fid = _id("finding")
        finding = {
            "finding_id": fid, "stage": stage, "gate_revision": revision,
            "author": author, "kind": kind, "title": title, "title_key": title_key,
            "content": content, "confidence": confidence, "strategy_id": strategy_id,
            "subgoal_id": subgoal_id, "evidence": evidence, "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256, "evidence_artifact_id": artifact_id,
            "evidence_sha256": artifact_sha256, "evidence_bytes": evidence_bytes,
            "status": "active",
            "created_at": time.time(), "updated_at": time.time(),
        }
        state["findings"][fid] = finding
        event = _event(state, "finding_published", finding_id=fid, author=author,
                       stage=stage, confidence=confidence)
    return {"status": "published", "finding": finding, "event": event}


def supersede_finding(
    unity_dir: Path, finding_id: str, author: str, reason: str,
    replacement_id: str = ""
) -> dict:
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 2_000)
    with transaction(unity_dir) as state:
        finding = state["findings"].get(finding_id)
        if not finding:
            raise ValueError(f"unknown finding '{finding_id}'")
        if replacement_id and replacement_id not in state["findings"]:
            raise ValueError(f"unknown replacement finding '{replacement_id}'")
        if finding["status"] == "superseded":
            return {"status": "superseded", "idempotent": True, "finding": finding}
        finding.update(status="superseded", superseded_by=replacement_id,
                       superseded_reason=reason, updated_at=time.time())
        event = _event(state, "finding_superseded", finding_id=finding_id, author=author,
                       replacement_id=replacement_id, reason=reason)
    return {"status": "superseded", "finding": finding, "event": event}


def report_obstacle(
    unity_dir: Path,
    author: str,
    goal_state: str,
    *,
    stage: str | None = None,
    strategy_id: str = "",
    subgoal_id: str = "",
    tried: list[str] | None = None,
    hypothesis: str = "",
    evidence: str = "",
    artifact_id: str = "",
    artifact_sha256: str = "",
) -> dict:
    author = _text(author, "author", 100)
    goal_state = _text(goal_state, "goal_state")
    tried = [_text(item, "tried item", 500) for item in tried or []]
    hypothesis = _text(hypothesis, "hypothesis", 2_000, required=False)
    evidence = _text(evidence, "evidence", 4_000, required=False)
    artifact_sha256 = _sha256(artifact_sha256, "artifact_sha256", required=False)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        stage = _strategy_stage(state, stage)
        if strategy_id:
            strategy = state["strategies"].get(strategy_id)
            if not strategy:
                raise ValueError(f"unknown strategy '{strategy_id}'")
            if (strategy.get("stage") != stage
                    or strategy.get("gate_revision") != _gate_revision(state, stage)):
                raise ValueError("obstacle strategy belongs to a stale or wrong-stage gate")
        if subgoal_id and subgoal_id not in state["subgoals"]:
            raise ValueError(f"unknown subgoal '{subgoal_id}'")
        oid = _id("obstacle")
        obstacle = {
            "obstacle_id": oid, "stage": stage, "gate_revision": _gate_revision(state, stage),
            "author": author, "title": goal_state[:300], "description": goal_state,
            "goal_state": goal_state, "tried": tried, "hypothesis": hypothesis,
            "strategy_id": strategy_id, "subgoal_id": subgoal_id,
            "evidence": evidence, "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256, "status": "open",
            "created_at": time.time(), "updated_at": time.time(),
        }
        state["obstacles"][oid] = obstacle
        event = _event(state, "obstacle_reported", obstacle_id=oid, author=author,
                       stage=stage)
    return {"status": "open", "obstacle": obstacle, "event": event}


def resolve_obstacle(
    unity_dir: Path, obstacle_id: str, author: str, resolution: str
) -> dict:
    author = _text(author, "author", 100)
    resolution = _text(resolution, "resolution", 2_000)
    with transaction(unity_dir) as state:
        obstacle = state["obstacles"].get(obstacle_id)
        if not obstacle:
            raise ValueError(f"unknown obstacle '{obstacle_id}'")
        if obstacle["status"] == "resolved":
            return {"status": "resolved", "idempotent": True, "obstacle": obstacle}
        obstacle.update(status="resolved", resolution=resolution, resolved_by=author,
                        updated_at=time.time())
        event = _event(state, "obstacle_resolved", obstacle_id=obstacle_id,
                       author=author, resolution=resolution)
    return {"status": "resolved", "obstacle": obstacle, "event": event}


# ── Immutable natural-language solution candidates and the solution gate ──────

def submit_solution_candidate(
    unity_dir: Path,
    author: str,
    artifact_id: str,
    sha256: str,
    *,
    notes: str = "",
    supersedes: str = "",
    artifact_bytes: int = 0,
    source_path: str = "",
    strategy_id: str = "",
) -> dict:
    author = _text(author, "author", 100)
    artifact_id = _text(artifact_id, "artifact_id", 300)
    sha256 = _sha256(sha256)
    notes = _text(notes, "notes", 4_000, required=False)
    source_path = _text(source_path, "source_path", 1_000, required=False)
    if isinstance(artifact_bytes, bool) or not isinstance(artifact_bytes, int) or artifact_bytes < 0:
        raise ValueError("artifact_bytes must be a non-negative integer")
    with transaction(unity_dir) as state:
        _require_initialized(state)
        gate = state["gates"]["solution"]
        for candidate in state["solution_candidates"].values():
            if candidate["gate_revision"] == gate["revision"] and candidate["sha256"] == sha256:
                return {"status": "duplicate", "candidate": candidate}
        if gate["status"] != "open" or state["stage"] != "solving":
            current = next(
                (candidate for candidate in state["solution_candidates"].values()
                 if candidate["gate_revision"] == gate["revision"]
                 and candidate["status"] in {"submitted", "reviewable", "blocked"}),
                None,
            )
            suffix = (
                f"; candidate {current['candidate_id']} already owns review"
                if current else ""
            )
            raise ValueError("the solution gate is not accepting another candidate" + suffix)
        if supersedes:
            previous = state["solution_candidates"].get(supersedes)
            if not previous:
                raise ValueError(f"unknown superseded solution candidate '{supersedes}'")
            if previous["status"] == "accepted":
                raise ValueError("reopen the solution gate before replacing its accepted candidate")
            previous.update(status="superseded", superseded_by_sha256=sha256,
                            updated_at=time.time())
            _supersede_open_objections(
                state, "solution_candidate", {supersedes}, "candidate superseded"
            )
        if strategy_id:
            strategy = state["strategies"].get(strategy_id)
            if not strategy:
                raise ValueError(f"unknown strategy '{strategy_id}'")
            if (strategy.get("stage") != "solving"
                    or strategy.get("gate_revision") != gate["revision"]):
                raise ValueError("solution candidate strategy belongs to a stale or wrong gate")
            if author != strategy.get("owner") and author not in strategy.get("assistants", {}):
                raise ValueError("candidate author must own or explicitly assist its strategy")
        cid = _id("solution")
        candidate = {
            "candidate_id": cid, "gate_revision": gate["revision"], "author": author,
            "artifact_id": artifact_id, "sha256": sha256, "artifact_bytes": artifact_bytes,
            "source_path": source_path, "strategy_id": strategy_id,
            "notes": notes, "supersedes": supersedes, "status": "submitted",
            "reviews": [], "created_at": time.time(), "updated_at": time.time(),
        }
        state["solution_candidates"][cid] = candidate
        gate.update(status="review", updated_at=time.time())
        state["stage"] = "solution_review"
        paused = []
        for strategy in state["strategies"].values():
            if (strategy["stage"] == "solving" and strategy["gate_revision"] == gate["revision"]
                    and strategy["status"] == "claimed"):
                strategy.update(status="paused", paused_for_candidate=cid,
                                updated_at=time.time())
                paused.append(strategy["strategy_id"])
        event = _event(state, "solution_candidate_submitted", candidate_id=cid,
                       author=author, sha256=sha256, gate_revision=gate["revision"],
                       paused_strategies=paused)
    return {"status": "submitted", "candidate": candidate, "event": event}


def review_solution_candidate(
    unity_dir: Path,
    candidate_id: str,
    author: str,
    verdict: str,
    rationale: str,
    *,
    evidence: str = "",
    artifact_id: str = "",
    artifact_sha256: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    """Record an independent exact-revision review; objections use the common graph."""
    if verdict not in {"approve", "object"}:
        raise ValueError("solution review verdict must be approve or object")
    author = _text(author, "author", 100)
    rationale = _text(rationale, "rationale", 4_000)
    if verdict == "object":
        return add_objection(
            unity_dir, "solution_candidate", candidate_id, author, rationale,
            evidence=evidence, artifact_id=artifact_id or evidence_artifact_id,
            artifact_sha256=artifact_sha256 or evidence_sha256,
        )
    evidence = _text(evidence, "evidence", 4_000, required=False)
    artifact_id = artifact_id or evidence_artifact_id
    artifact_sha256 = _sha256(
        artifact_sha256 or evidence_sha256, "artifact_sha256", required=False
    )
    with transaction(unity_dir) as state:
        candidate = state["solution_candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown solution candidate '{candidate_id}'")
        if candidate["author"] == author:
            raise ValueError("a solution candidate author cannot independently approve it")
        if candidate["status"] not in {"submitted", "reviewable"}:
            raise ValueError(f"cannot approve a {candidate['status']} solution candidate")
        existing = next((r for r in candidate["reviews"] if r["author"] == author), None)
        if existing:
            return {"status": candidate["status"], "idempotent": True,
                    "candidate": candidate, "review": existing}
        review = {
            "review_id": _id("review"), "author": author, "verdict": "approve",
            "rationale": rationale, "evidence": evidence, "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256, "evidence_bytes": evidence_bytes,
            "created_at": time.time(),
        }
        candidate["reviews"].append(review)
        candidate.update(status="reviewable", updated_at=time.time())
        event = _event(state, "solution_candidate_reviewed", candidate_id=candidate_id,
                       review_id=review["review_id"], author=author, verdict="approve")
    return {"status": "reviewable", "candidate": candidate, "review": review,
            "event": event}


def accept_solution_candidate(
    unity_dir: Path, candidate_id: str, author: str, rationale: str = ""
) -> dict:
    author = _text(author, "author", 100)
    rationale = _text(rationale, "rationale", 2_000, required=False)
    with transaction(unity_dir) as state:
        candidate = state["solution_candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown solution candidate '{candidate_id}'")
        gate = state["gates"]["solution"]
        if candidate["gate_revision"] != gate["revision"]:
            raise ValueError("solution candidate belongs to a stale gate revision")
        if candidate["status"] == "accepted" and gate["accepted_candidate_id"] == candidate_id:
            return {"status": "accepted", "idempotent": True, "candidate": candidate}
        if candidate["status"] not in {"submitted", "reviewable"}:
            raise ValueError(f"cannot accept a {candidate['status']} solution candidate")
        if not any(review.get("verdict") == "approve" for review in candidate["reviews"]):
            raise ValueError("solution candidate requires an independent approval before acceptance")
        open_objections = [item for item in state["objections"].values()
                           if item["target_id"] == candidate_id and item["status"] == "open"]
        if open_objections:
            raise ValueError("solution candidate has unresolved objections")
        candidate.update(status="accepted", accepted_by=author,
                         accepted_rationale=rationale, updated_at=time.time())
        for other in state["solution_candidates"].values():
            if (other["candidate_id"] != candidate_id
                    and other["gate_revision"] == gate["revision"]
                    and other["status"] in {"submitted", "reviewable", "blocked"}):
                other.update(status="superseded", superseded_by=candidate_id,
                             updated_at=time.time())
                _supersede_open_objections(
                    state, "solution_candidate", {other["candidate_id"]},
                    "another solution candidate was accepted",
                )
        for strategy in state["strategies"].values():
            if (strategy["stage"] == "solving" and strategy["gate_revision"] == gate["revision"]
                    and strategy["status"] in _ACTIVE_STRATEGIES):
                strategy.update(status="cancelled", cancelled_reason="solution accepted",
                                updated_at=time.time())
        for subgoal in state["subgoals"].values():
            if (subgoal["stage"] == "solving" and subgoal["gate_revision"] == gate["revision"]
                    and subgoal["status"] in {"open", "blocked"}):
                subgoal.update(status="superseded", status_reason="solution accepted",
                               updated_at=time.time())
        gate.update(status="accepted", accepted_candidate_id=candidate_id,
                    artifact_id=candidate["artifact_id"], sha256=candidate["sha256"],
                    repair_mode="none",
                    updated_at=time.time())
        source_fix_id = gate.get("source_fix_id")
        if source_fix_id and source_fix_id in state["source_fixes"]:
            state["source_fixes"][source_fix_id].update(
                status="resolved", replacement_candidate_id=candidate_id,
                resolved_at=time.time(), updated_at=time.time(),
            )
        state["gates"]["formalization"].update(
            status="waiting", solution_candidate_id=candidate_id,
            accepted_candidate_ids=[], updated_at=time.time()
        )
        state["stage"] = "chunking"
        event = _event(state, "solution_candidate_accepted", candidate_id=candidate_id,
                       author=author, sha256=candidate["sha256"],
                       gate_revision=gate["revision"])
    return {"status": "accepted", "candidate": candidate, "event": event}


def reject_solution_candidate(
    unity_dir: Path, candidate_id: str, author: str, reason: str
) -> dict:
    """Reject one exact paper revision and immediately resume mathematical work."""
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 4_000)
    with transaction(unity_dir) as state:
        candidate = state["solution_candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown solution candidate '{candidate_id}'")
        if candidate["status"] == "rejected":
            return {"status": "rejected", "idempotent": True, "candidate": candidate}
        if candidate["status"] == "accepted":
            raise ValueError("reopen the solution gate to reject an accepted candidate")
        candidate.update(status="rejected", rejected_by=author,
                         rejection_reason=reason, updated_at=time.time())
        _supersede_open_objections(
            state, "solution_candidate", {candidate_id}, "candidate rejected"
        )
        gate = state["gates"]["solution"]
        resumed = []
        if candidate["gate_revision"] == gate["revision"]:
            pending = any(item["candidate_id"] != candidate_id
                          and item["gate_revision"] == gate["revision"]
                          and item["status"] in {"submitted", "reviewable"}
                          for item in state["solution_candidates"].values())
            gate.update(status="review" if pending else "open", updated_at=time.time())
            state["stage"] = "solution_review" if pending else "solving"
            if not pending:
                resumed = _resume_paused_strategies(state, "solving", gate["revision"])
        event = _event(state, "solution_candidate_rejected", candidate_id=candidate_id,
                       author=author, reason=reason, resumed_strategies=resumed)
    return {"status": "rejected", "candidate": candidate, "event": event}


# ── Objections and gate revision transitions ──────────────────────────────────

def add_objection(
    unity_dir: Path,
    target_kind: str,
    target_id: str,
    author: str,
    reason: str,
    *,
    evidence: str = "",
    artifact_id: str = "",
    artifact_sha256: str = "",
) -> dict:
    if target_kind not in OBJECTION_TARGETS:
        raise ValueError("target_kind must be solution_candidate or formal_candidate")
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 4_000)
    evidence = _text(evidence, "evidence", 4_000, required=False)
    artifact_sha256 = _sha256(artifact_sha256, "artifact_sha256", required=False)
    key = "solution_candidates" if target_kind == "solution_candidate" else "formal_candidates"
    with transaction(unity_dir) as state:
        target = state[key].get(target_id)
        if not target:
            raise ValueError(f"unknown {target_kind} '{target_id}'")
        if target["status"] in {"rejected", "superseded", "failed"}:
            raise ValueError(f"cannot object to a {target['status']} candidate")
        duplicate = next((item for item in state["objections"].values()
                          if item["target_kind"] == target_kind
                          and item["target_id"] == target_id
                          and item["author"] == author and item["status"] == "open"), None)
        if duplicate:
            return {"status": "blocked", "idempotent": True,
                    "objection": duplicate, "candidate": target}
        oid = _id("objection")
        objection = {
            "objection_id": oid, "target_kind": target_kind, "target_id": target_id,
            "author": author, "reason": reason, "evidence": evidence,
            "artifact_id": artifact_id, "artifact_sha256": artifact_sha256,
            "status": "open", "created_at": time.time(), "updated_at": time.time(),
        }
        state["objections"][oid] = objection
        target.update(status="blocked", updated_at=time.time())
        if target_kind == "solution_candidate":
            gate = state["gates"]["solution"]
            if gate.get("accepted_candidate_id") == target_id:
                _reopen_solution_gate(state, author, reason)
            elif target["gate_revision"] == gate["revision"]:
                gate.update(status="open", updated_at=time.time())
                state["stage"] = "solving"
                _resume_paused_strategies(state, "solving", gate["revision"])
        else:
            gate = state["gates"]["formalization"]
            if gate["status"] == "accepted" and target_id in gate["accepted_candidate_ids"]:
                repair_task_id = target["task_id"]
                _reopen_formalization_gate(state, author, reason)
                _recreate_formal_tasks(
                    state, [repair_task_id], author,
                    repair_reason=reason, repair_trigger_id=oid,
                )
        event = _event(state, "candidate_objected", objection_id=oid,
                       target_kind=target_kind, target_id=target_id, author=author,
                       reason=reason)
    return {"status": "blocked", "objection": objection, "candidate": target,
            "event": event}


def resolve_objection(
    unity_dir: Path, objection_id: str, author: str, resolution: str,
    *, target_id: str = "",
) -> dict:
    author = _text(author, "author", 100)
    resolution = _text(resolution, "resolution", 4_000)
    with transaction(unity_dir) as state:
        objection = state["objections"].get(objection_id)
        if not objection:
            raise ValueError(f"unknown objection '{objection_id}'")
        if target_id and objection["target_id"] != target_id:
            raise ValueError("objection does not belong to the supplied candidate")
        if objection["status"] != "open":
            return {"status": objection["status"], "idempotent": True,
                    "objection": objection}
        if author not in {objection["author"], "Unity"}:
            raise ValueError("only the objector or Unity may resolve an objection")
        objection.update(status="resolved", resolution=resolution, resolved_by=author,
                          updated_at=time.time())
        key = "solution_candidates" if objection["target_kind"] == "solution_candidate" else "formal_candidates"
        candidate = state[key][objection["target_id"]]
        still_open = any(item["target_kind"] == objection["target_kind"]
                         and item["target_id"] == objection["target_id"]
                         and item["status"] == "open" for item in state["objections"].values())
        if not still_open and candidate["status"] == "blocked":
            if objection["target_kind"] == "solution_candidate":
                gate = state["gates"]["solution"]
                if candidate["gate_revision"] == gate["revision"]:
                    candidate["status"] = "reviewable" if candidate["reviews"] else "submitted"
                    gate["status"] = "review"
                    state["stage"] = "solution_review"
            else:
                verification = candidate.get("verification") or {}
                candidate["status"] = "verified" if verification.get("status") == "passed" else "submitted"
            candidate["updated_at"] = time.time()
        event = _event(state, "objection_resolved", objection_id=objection_id,
                       target_kind=objection["target_kind"], target_id=objection["target_id"],
                       author=author, resolution=resolution)
    return {"status": "resolved", "objection": objection, "candidate": candidate,
            "event": event}


def _resume_paused_strategies(state: dict, stage: str, revision: int) -> list[str]:
    resumed = []
    for strategy in state["strategies"].values():
        if (strategy["stage"] == stage and strategy["gate_revision"] == revision
                and strategy["status"] == "paused"):
            strategy.update(status="claimed", updated_at=time.time())
            resumed.append(strategy["strategy_id"])
    return resumed


def _supersede_open_objections(
    state: dict, target_kind: str, target_ids: set[str], reason: str
) -> list[str]:
    """Stop stale objections from blocking or resurfacing superseded candidate bytes."""
    superseded = []
    for objection in state["objections"].values():
        if (objection["target_kind"] == target_kind
                and objection["target_id"] in target_ids
                and objection["status"] == "open"):
            objection.update(
                status="superseded", superseded_reason=reason, updated_at=time.time()
            )
            superseded.append(objection["objection_id"])
    return superseded


def _supersede_formal_strategies(state: dict, revision: int, reason: str) -> list[str]:
    superseded = []
    for strategy in state["strategies"].values():
        if (strategy["stage"] == "formalizing"
                and strategy["gate_revision"] == revision
                and strategy["status"] in _ACTIVE_STRATEGIES):
            strategy.update(
                status="superseded", superseded_reason=reason, updated_at=time.time()
            )
            superseded.append(strategy["strategy_id"])
    return superseded


def _supersede_formal_work(state: dict, reason: str) -> None:
    for task in state["formal_tasks"].values():
        if task["status"] in _ACTIVE_FORMAL_TASKS:
            task.update(status="superseded", status_reason=reason, updated_at=time.time())
    for candidate in state["formal_candidates"].values():
        if candidate["status"] not in {"accepted", "failed", "rejected", "superseded"}:
            candidate.update(status="superseded", superseded_reason=reason,
                             updated_at=time.time())


def _reopen_solution_gate(
    state: dict,
    author: str,
    reason: str,
    *,
    repair_mode: str = "search",
    source_fix_id: str | None = None,
) -> None:
    gate = state["gates"]["solution"]
    old_formal_revision = int(state["gates"]["formalization"]["revision"])
    accepted = gate.get("accepted_candidate_id")
    if accepted and accepted in state["solution_candidates"]:
        state["solution_candidates"][accepted].update(
            status="blocked", updated_at=time.time()
        )
    _supersede_formal_work(state, "solution gate reopened")
    _supersede_formal_strategies(state, old_formal_revision, "solution gate reopened")
    gate.update(revision=int(gate["revision"]) + 1, status="open",
                accepted_candidate_id=None, artifact_id="", sha256="",
                repair_mode=repair_mode, source_fix_id=source_fix_id,
                reopened_by=author, reopen_reason=reason, updated_at=time.time())
    state["gates"]["formalization"].update(
        status="waiting", solution_candidate_id=None, accepted_candidate_ids=[],
        integrated_main_sha="",
        updated_at=time.time()
    )
    state["stage"] = "solving"
    state["outcome"] = None


def _reopen_formalization_gate(state: dict, author: str, reason: str) -> None:
    gate = state["gates"]["formalization"]
    old_revision = int(gate["revision"])
    _supersede_formal_work(state, "formalization gate reopened")
    _supersede_formal_strategies(state, old_revision, "formalization gate reopened")
    gate.update(revision=int(gate["revision"]) + 1, status="open",
                accepted_candidate_ids=[], integrated_main_sha="",
                reopened_by=author, reopen_reason=reason,
                updated_at=time.time())
    state["stage"] = "formalizing"
    state["outcome"] = None


def _recreate_formal_tasks(
    state: dict,
    old_ids: list[str],
    author: str,
    *,
    repair_reason: str = "",
    repair_trigger_id: str = "",
) -> list[dict]:
    """Clone selected historical tasks into the current formal gate revision."""
    replacement_ids = {old_id: _id("formal-task") for old_id in old_ids}
    created = []
    for old_id in old_ids:
        old = state["formal_tasks"][old_id]
        tid = replacement_ids[old_id]
        task = {
            **{k: v for k, v in old.items() if k not in {
                "task_id", "owner", "status", "created_at", "updated_at",
                "completed_by", "accepted_candidate_id", "result", "artifact_id",
                "artifact_sha256", "finding_ids", "status_reason", "strategy_id",
            }},
            "task_id": tid,
            "gate_revision": state["gates"]["formalization"]["revision"],
            "creator": author, "owner": None, "status": "pending",
            "supersedes_task_id": old_id,
            "repair_reason": repair_reason,
            "repair_trigger_id": repair_trigger_id,
            "dependencies": [replacement_ids[dependency]
                             for dependency in old.get("dependencies", [])
                             if dependency in replacement_ids],
            "created_at": time.time(), "updated_at": time.time(),
        }
        state["formal_tasks"][tid] = task
        created.append(task)
    return created


def reopen_gate(unity_dir: Path, gate: str, author: str, reason: str) -> dict:
    if gate not in {"solution", "formalization"}:
        raise ValueError("gate must be solution or formalization")
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 4_000)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        if gate == "solution":
            _reopen_solution_gate(state, author, reason)
        else:
            if state["gates"]["solution"]["status"] != "accepted":
                raise ValueError("cannot reopen formalization without an accepted solution")
            old_revision = state["gates"]["formalization"]["revision"]
            reopen_ids = [
                task["task_id"] for task in state["formal_tasks"].values()
                if task["gate_revision"] == old_revision
                and task["status"] not in {"cancelled", "superseded"}
            ]
            _reopen_formalization_gate(state, author, reason)
            _recreate_formal_tasks(
                state, reopen_ids, author,
                repair_reason=reason, repair_trigger_id="manual-gate-reopen",
            )
        record = state["gates"][gate]
        event = _event(state, f"{gate}_gate_reopened", author=author, reason=reason,
                       gate_revision=record["revision"])
    return {"status": "open", "gate": record, "event": event}


# ── Formalization tasks and immutable Lean candidates ─────────────────────────

def begin_formalization(unity_dir: Path, author: str) -> dict:
    author = _text(author, "author", 100)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        solution = state["gates"]["solution"]
        gate = state["gates"]["formalization"]
        if solution["status"] != "accepted":
            raise ValueError("formalization requires an accepted solution candidate")
        if gate["status"] == "open" and state["stage"] == "formalizing":
            return {"status": "open", "idempotent": True, "gate": gate}
        if gate["status"] not in {"waiting", "accepted"}:
            raise ValueError(f"cannot begin formalization from gate status {gate['status']}")
        gate.update(revision=int(gate["revision"]) + 1, status="open",
                    solution_candidate_id=solution["accepted_candidate_id"],
                    accepted_candidate_ids=[], integrated_main_sha="",
                    updated_at=time.time())
        state["stage"] = "formalizing"
        event = _event(state, "formalization_started", author=author,
                       gate_revision=gate["revision"],
                       solution_candidate_id=gate["solution_candidate_id"])
    return {"status": "open", "gate": gate, "event": event}


def initialize_formalization_tasks(
    unity_dir: Path, dag: Path | dict, author: str = "Unity"
) -> dict:
    """Initialize one formal task per semantic DAG chunk, idempotently.

    The exact canonical DAG hash is bound to the formalization gate. Repeating this
    call with the same DAG returns the existing tasks; changing it after work starts
    requires an explicit gate reopen.
    """
    if isinstance(dag, (str, Path)):
        dag_data = json.loads(Path(dag).read_text())
    else:
        dag_data = dag
    if not isinstance(dag_data, dict) or not isinstance(dag_data.get("chunks"), list):
        raise ValueError("formalization DAG must contain a chunks list")
    canonical = json.dumps(dag_data, sort_keys=True, separators=(",", ":"))
    dag_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
    author = _text(author, "author", 100)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        solution = state["gates"]["solution"]
        gate = state["gates"]["formalization"]
        if solution["status"] != "accepted":
            raise ValueError("formalization tasks require an accepted solution candidate")
        if gate["status"] == "waiting":
            gate.update(revision=int(gate["revision"]) + 1, status="open",
                        solution_candidate_id=solution["accepted_candidate_id"],
                        accepted_candidate_ids=[], integrated_main_sha="",
                        updated_at=time.time())
            state["stage"] = "formalizing"
            _event(state, "formalization_started", author=author,
                   gate_revision=gate["revision"],
                   solution_candidate_id=gate["solution_candidate_id"])
        if gate["status"] != "open":
            raise ValueError("formalization gate is not open")
        existing = [task for task in state["formal_tasks"].values()
                    if task["gate_revision"] == gate["revision"]]
        if gate.get("dag_sha256") == dag_sha256 and existing:
            return {"status": "initialized", "idempotent": True,
                    "dag_sha256": dag_sha256, "tasks": existing, "gate": gate}
        if existing and gate.get("dag_sha256") != dag_sha256:
            raise ValueError("formalization gate is already bound to a different DAG")

        chunks = dag_data["chunks"]
        chunk_ids = []
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise ValueError(f"chunk #{index + 1} must be an object")
            chunk_id = _text(str(chunk.get("id") or ""), "chunk id", 500)
            if chunk_id in chunk_ids:
                raise ValueError(f"duplicate chunk id '{chunk_id}'")
            chunk_ids.append(chunk_id)
        task_by_chunk = {
            chunk_id: "formal-task-" + hashlib.sha256(
                f"{gate['revision']}:{chunk_id}".encode()
            ).hexdigest()[:12]
            for chunk_id in chunk_ids
        }
        created = []
        for chunk, chunk_id in zip(chunks, chunk_ids):
            raw_dependencies = chunk.get("dependencies") or []
            dependency_chunks = [
                str(item.get("chunk_id")) if isinstance(item, dict) else str(item)
                for item in raw_dependencies
            ]
            missing = [dep for dep in dependency_chunks if dep not in task_by_chunk]
            if missing:
                raise ValueError(
                    f"chunk '{chunk_id}' has unknown dependencies: {', '.join(missing)}"
                )
            refs = chunk.get("source_refs") or []
            if isinstance(refs, str):
                refs = [refs]
            supplied_hash = str(
                chunk.get("source_sha256") or chunk.get("source_hash") or ""
            ).strip().lower()
            source_hash_kind = "provided" if _SHA256_RE.fullmatch(supplied_hash) else "derived"
            chunk_hash = supplied_hash if source_hash_kind == "provided" else hashlib.sha256(
                json.dumps(chunk, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            tid = task_by_chunk[chunk_id]
            task = {
                "task_id": tid, "gate_revision": gate["revision"], "creator": author,
                "owner": None, "kind": _key(str(chunk.get("type") or "formalization")),
                "description": _text(
                    str(chunk.get("summary") or chunk.get("title") or chunk_id),
                    "chunk description",
                ),
                "task_key": _key(chunk_id),
                "dependencies": [task_by_chunk[dep] for dep in dependency_chunks],
                "dependency_chunks": dependency_chunks,
                "parent_task_id": "", "subgoal_id": "", "chunk_id": chunk_id,
                "source_hash": chunk_hash, "source_sha256": chunk_hash,
                "source_hash_kind": source_hash_kind,
                "source_refs": [_text(str(ref), "source_ref", 1_000) for ref in refs],
                "statement": str(chunk.get("statement") or ""),
                "solution_candidate_id": gate["solution_candidate_id"],
                # DAG content is model-authored. Only deterministic Unity verification
                # may complete a formal task; a chunk's claimed status is never trusted.
                "accepted_solution_sha256": solution["sha256"], "status": "pending",
                "created_at": time.time(), "updated_at": time.time(),
            }
            state["formal_tasks"][tid] = task
            created.append(task)
        gate.update(dag_sha256=dag_sha256, dag_chunk_count=len(created),
                    updated_at=time.time())
        event = _event(state, "formalization_tasks_initialized", author=author,
                       gate_revision=gate["revision"], dag_sha256=dag_sha256,
                       task_ids=[task["task_id"] for task in created])
    return {"status": "initialized", "idempotent": False,
            "dag_sha256": dag_sha256, "tasks": created, "gate": gate, "event": event}


def all_formalization_tasks_complete(unity_dir: Path) -> bool:
    """Whether every non-cancelled task in the current formal gate is complete."""
    state = load_state(unity_dir)
    gate = state["gates"]["formalization"]
    tasks = [task for task in state["formal_tasks"].values()
             if task["gate_revision"] == gate["revision"]
             and task["status"] not in {"cancelled", "superseded"}]
    return bool(tasks) and all(task["status"] == "complete" for task in tasks)


def create_formal_task(
    unity_dir: Path,
    author: str,
    kind: str,
    description: str,
    *,
    task_key: str = "",
    dependencies: list[str] | None = None,
    parent_task_id: str = "",
    subgoal_id: str = "",
    chunk_id: str = "",
    source_hash: str = "",
    source_refs: list[str] | None = None,
) -> dict:
    author = _text(author, "author", 100)
    kind = _key(_text(kind, "kind", 100))
    description = _text(description, "description")
    dependencies = list(dict.fromkeys(dependencies or []))
    normalized_key = _key(task_key) or _key(f"{kind} {description}")
    chunk_id = _text(chunk_id, "chunk_id", 500, required=False)
    source_hash = _sha256(source_hash, "source_hash", required=False)
    source_refs = [_text(item, "source_ref", 1_000) for item in source_refs or []]
    with transaction(unity_dir) as state:
        _require_initialized(state)
        gate = state["gates"]["formalization"]
        if gate["status"] != "open" or state["stage"] != "formalizing":
            raise ValueError("formal tasks require an open formalization gate")
        missing = [item for item in dependencies if item not in state["formal_tasks"]]
        if missing:
            raise ValueError(f"unknown formal task dependencies: {', '.join(missing)}")
        if parent_task_id and parent_task_id not in state["formal_tasks"]:
            raise ValueError(f"unknown parent formal task '{parent_task_id}'")
        if subgoal_id and subgoal_id not in state["subgoals"]:
            raise ValueError(f"unknown subgoal '{subgoal_id}'")
        for task in state["formal_tasks"].values():
            if (task["gate_revision"] == gate["revision"] and task["task_key"] == normalized_key
                    and task["status"] not in {"cancelled", "superseded"}):
                return {"status": "duplicate", "task": task}
        tid = _id("formal-task")
        task = {
            "task_id": tid, "gate_revision": gate["revision"], "creator": author,
            "owner": None, "kind": kind, "description": description,
            "task_key": normalized_key, "dependencies": dependencies,
            "parent_task_id": parent_task_id, "subgoal_id": subgoal_id,
            "chunk_id": chunk_id, "source_hash": source_hash, "source_refs": source_refs,
            "solution_candidate_id": gate["solution_candidate_id"], "status": "pending",
            "created_at": time.time(), "updated_at": time.time(),
        }
        state["formal_tasks"][tid] = task
        event = _event(state, "formal_task_created", task_id=tid, author=author,
                       task_kind=kind, gate_revision=gate["revision"])
    return {"status": "pending", "task": task, "event": event}


def claim_formal_task(unity_dir: Path, task_id: str, author: str) -> dict:
    author = _text(author, "author", 100)
    with transaction(unity_dir) as state:
        gate = _require_active_formalization(state)
        task = state["formal_tasks"].get(task_id)
        if not task:
            raise ValueError(f"unknown formal task '{task_id}'")
        if task["gate_revision"] != gate["revision"]:
            return {"status": "stale", "task": task}
        if task["status"] == "claimed":
            if task["owner"] == author:
                return {"status": "claimed", "idempotent": True, "task": task}
            return {"status": "conflict", "owner": task["owner"], "task": task}
        existing = next(
            (item for item in state["formal_tasks"].values()
             if item["task_id"] != task_id
             and item["gate_revision"] == gate["revision"]
             and item.get("owner") == author and item["status"] == "claimed"),
            None,
        )
        if existing:
            return {
                "status": "conflict", "owner": author,
                "reason": "author_already_claims_formal_task",
                "existing_task": existing, "task": task,
            }
        unresolved = [dep for dep in task["dependencies"]
                      if state["formal_tasks"][dep]["status"] != "complete"]
        if unresolved:
            return {"status": "blocked", "dependencies": unresolved, "task": task}
        if task["status"] not in {"pending", "failed"}:
            return {"status": task["status"], "task": task}
        task.update(status="claimed", owner=author, updated_at=time.time())
        event = _event(state, "formal_task_claimed", task_id=task_id, author=author)
    return {"status": "claimed", "task": task, "event": event}


def release_formal_task(
    unity_dir: Path, task_id: str, author: str, reason: str = ""
) -> dict:
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 2_000, required=False)
    with transaction(unity_dir) as state:
        task = state["formal_tasks"].get(task_id)
        if not task:
            raise ValueError(f"unknown formal task '{task_id}'")
        if task.get("owner") != author:
            raise ValueError("only the formal task owner may release it")
        if task["status"] != "claimed":
            return {"status": task["status"], "idempotent": True, "task": task}
        task.update(status="pending", owner=None, release_reason=reason,
                    updated_at=time.time())
        event = _event(state, "formal_task_released", task_id=task_id,
                       author=author, reason=reason)
    return {"status": "pending", "task": task, "event": event}


def complete_formal_task(
    unity_dir: Path,
    task_id: str,
    author: str,
    *,
    result: str = "",
    artifact_id: str = "",
    artifact_sha256: str = "",
    finding_ids: list[str] | None = None,
) -> dict:
    author = _text(author, "author", 100)
    result = _text(result, "result", 4_000, required=False)
    artifact_sha256 = _sha256(artifact_sha256, "artifact_sha256", required=False)
    finding_ids = list(dict.fromkeys(finding_ids or []))
    with transaction(unity_dir) as state:
        task = state["formal_tasks"].get(task_id)
        if not task:
            raise ValueError(f"unknown formal task '{task_id}'")
        if task["status"] == "complete":
            return {"status": "complete", "idempotent": True, "task": task}
        if task["status"] == "claimed" and task.get("owner") != author:
            raise ValueError("only the formal task owner may complete claimed work")
        if task["status"] in {"pending", "failed"} and task["creator"] != author:
            raise ValueError("claim the formal task before completing another creator's work")
        if task["status"] not in {"pending", "claimed", "failed"}:
            raise ValueError(f"cannot complete a {task['status']} formal task")
        missing = [fid for fid in finding_ids if fid not in state["findings"]]
        if missing:
            raise ValueError(f"unknown findings: {', '.join(missing)}")
        task.update(status="complete", completed_by=author, result=result,
                    artifact_id=artifact_id, artifact_sha256=artifact_sha256,
                    finding_ids=finding_ids, updated_at=time.time())
        event = _event(state, "formal_task_completed", task_id=task_id, author=author)
    return {"status": "complete", "task": task, "event": event}


def submit_formal_candidate(
    unity_dir: Path,
    author: str,
    commit_sha: str,
    *,
    task_id: str = "",
    strategy_id: str = "",
    source_hash: str = "",
    artifact_id: str = "",
    declarations: list[str] | None = None,
    notes: str = "",
    supersedes: str = "",
    base_main_sha: str = "",
    diff_sha256: str = "",
) -> dict:
    author = _text(author, "author", 100)
    commit_sha = str(commit_sha or "").strip().lower()
    if not _COMMIT_RE.fullmatch(commit_sha):
        raise ValueError("commit_sha must be a 7-40 character hexadecimal Git commit")
    source_hash_was_provided = bool(source_hash)
    source_hash = _sha256(
        source_hash or hashlib.sha256(commit_sha.encode()).hexdigest(), "source_hash"
    )
    diff_sha256 = _sha256(diff_sha256, "diff_sha256", required=False)
    base_main_sha = str(base_main_sha or "").strip().lower()
    if base_main_sha and not _COMMIT_RE.fullmatch(base_main_sha):
        raise ValueError("base_main_sha must be a 7-40 character hexadecimal Git commit")
    notes = _text(notes, "notes", 4_000, required=False)
    declarations = [_text(item, "declaration", 500) for item in declarations or []]
    with transaction(unity_dir) as state:
        gate = _require_active_formalization(state)
        if not task_id:
            owned = [item for item in state["formal_tasks"].values()
                     if item["gate_revision"] == gate["revision"]
                     and item.get("owner") == author and item["status"] == "claimed"]
            if len(owned) == 1:
                task_id = owned[0]["task_id"]
            else:
                raise ValueError(
                    "task_id is required unless the author owns exactly one current formal task"
                )
        task = state["formal_tasks"].get(task_id)
        if not task:
            raise ValueError(f"unknown formal task '{task_id}'")
        if task["gate_revision"] != gate["revision"] or gate["status"] != "open":
            raise ValueError("formal task belongs to a stale or closed gate revision")
        if task["status"] != "claimed" or task.get("owner") != author:
            raise ValueError("candidate author must hold the formal task claim")
        if strategy_id:
            strategy = state["strategies"].get(strategy_id)
            if not strategy:
                raise ValueError(f"unknown strategy '{strategy_id}'")
            if (strategy["stage"] != "formalizing"
                    or strategy["gate_revision"] != gate["revision"]):
                raise ValueError("formal candidate strategy belongs to a stale or wrong-stage gate")
            if author != strategy.get("owner") and author not in strategy.get("assistants", {}):
                raise ValueError("candidate author must own or explicitly assist its strategy")
        for candidate in state["formal_candidates"].values():
            if (candidate["gate_revision"] == gate["revision"]
                    and candidate["task_id"] == task_id
                    and candidate["commit_sha"] == commit_sha
                    and candidate["source_hash"] == source_hash):
                return {"status": "duplicate", "candidate": candidate}
        if supersedes:
            previous = state["formal_candidates"].get(supersedes)
            if not previous:
                raise ValueError(f"unknown superseded formal candidate '{supersedes}'")
            if previous["status"] == "accepted":
                raise ValueError("reopen formalization before replacing an accepted candidate")
            previous.update(status="superseded", superseded_by_source_hash=source_hash,
                            updated_at=time.time())
            _supersede_open_objections(
                state, "formal_candidate", {supersedes}, "candidate superseded"
            )
        cid = _id("formal-candidate")
        candidate = {
            "candidate_id": cid, "task_id": task_id, "gate_revision": gate["revision"],
            "solution_candidate_id": gate["solution_candidate_id"], "author": author,
            "commit_sha": commit_sha, "source_hash": source_hash,
            "source_hash_kind": "provided" if source_hash_was_provided else "commit_derived",
            "base_main_sha": base_main_sha, "diff_sha256": diff_sha256,
            "accepted_solution_sha256": state["gates"]["solution"]["sha256"],
            "solution_sha256": state["gates"]["solution"]["sha256"],
            "artifact_id": artifact_id, "strategy_id": strategy_id,
            "declarations": declarations, "notes": notes,
            "supersedes": supersedes, "status": "submitted", "verification": None,
            "created_at": time.time(), "updated_at": time.time(),
        }
        state["formal_candidates"][cid] = candidate
        event = _event(state, "formal_candidate_submitted", candidate_id=cid,
                       task_id=task_id, author=author, commit_sha=commit_sha,
                       source_hash=source_hash, gate_revision=gate["revision"])
    return {"status": "submitted", "candidate": candidate, "event": event}


def record_formal_verification(
    unity_dir: Path,
    candidate_id: str,
    verifier: str,
    status: str,
    *,
    artifact_id: str = "",
    artifact_sha256: str = "",
    issues: list[str] | None = None,
    details: dict | None = None,
) -> dict:
    if status not in {"passed", "failed", "blocked"}:
        raise ValueError("formal verification status must be passed, failed, or blocked")
    verifier = _text(verifier, "verifier", 100)
    artifact_sha256 = _sha256(artifact_sha256, "artifact_sha256", required=False)
    issues = [_text(item, "issue", 2_000) for item in issues or []]
    if details is not None and not isinstance(details, dict):
        raise ValueError("verification details must be an object")
    if status == "passed" and not (artifact_id or artifact_sha256):
        raise ValueError("passed formal verification requires an immutable verification artifact")
    with transaction(unity_dir) as state:
        gate = _require_active_formalization(state)
        candidate = state["formal_candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown formal candidate '{candidate_id}'")
        if candidate["gate_revision"] != gate["revision"]:
            raise ValueError("formal candidate belongs to a stale gate revision")
        if candidate["status"] in {"accepted", "rejected", "superseded"}:
            raise ValueError(f"cannot verify a {candidate['status']} formal candidate")
        previous = candidate.get("verification")
        if previous and previous["status"] == status and previous.get("artifact_sha256") == artifact_sha256:
            return {"status": candidate["status"], "idempotent": True,
                    "candidate": candidate, "verification": previous}
        verification = {
            "verification_id": _id("verification"), "verifier": verifier,
            "status": status, "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256, "issues": issues,
            "candidate_commit_sha": candidate["commit_sha"],
            "candidate_source_hash": candidate["source_hash"], "created_at": time.time(),
        }
        if details:
            verification["details"] = details
        candidate.update(verification=verification,
                         status={"passed": "verified", "failed": "failed",
                                 "blocked": "blocked"}[status],
                         updated_at=time.time())
        event = _event(state, "formal_candidate_verified", candidate_id=candidate_id,
                       verifier=verifier, status=status,
                       candidate_source_hash=candidate["source_hash"])
    return {"status": candidate["status"], "candidate": candidate,
            "verification": verification, "event": event}


def begin_formal_candidate_verification(
    unity_dir: Path, candidate_id: str, verifier: str
) -> dict:
    """Atomically reserve deterministic verification of an exact formal candidate."""
    verifier = _text(verifier, "verifier", 100)
    with transaction(unity_dir) as state:
        gate = _require_active_formalization(state)
        candidate = state["formal_candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown formal candidate '{candidate_id}'")
        if candidate["gate_revision"] != gate["revision"]:
            raise ValueError("formal candidate belongs to a stale gate revision")
        if candidate["status"] == "verifying":
            if candidate.get("verification_owner") == verifier:
                return {"status": "verifying", "idempotent": True, "candidate": candidate}
            return {"status": "conflict", "owner": candidate.get("verification_owner"),
                    "candidate": candidate}
        if candidate["status"] != "submitted":
            return {"status": candidate["status"], "candidate": candidate}
        candidate.update(status="verifying", verification_owner=verifier,
                         verification_started_at=time.time(), updated_at=time.time())
        event = _event(state, "formal_candidate_verification_started",
                       candidate_id=candidate_id, verifier=verifier,
                       commit_sha=candidate["commit_sha"], source_hash=candidate["source_hash"])
    return {"status": "verifying", "candidate": candidate, "event": event}


def finish_formal_candidate_verification(
    unity_dir: Path,
    candidate_id: str,
    verifier: str,
    status: str | dict,
    **kwargs,
) -> dict:
    """Finish a reserved verification; the persisted record remains byte-bound."""
    if isinstance(status, dict):
        details = status
        status = str(details.get("status") or "failed")
        kwargs.setdefault("artifact_id", str(details.get("artifact_id") or ""))
        kwargs.setdefault("issues", list(details.get("issues") or []))
        kwargs.setdefault("details", details)
    if status not in {"passed", "failed", "blocked"}:
        raise ValueError("formal verification status must be passed, failed, or blocked")
    verifier = _text(verifier, "verifier", 100)
    artifact_id = str(kwargs.pop("artifact_id", "") or "")
    artifact_sha256 = _sha256(
        kwargs.pop("artifact_sha256", ""), "artifact_sha256", required=False
    )
    issues = [_text(item, "issue", 2_000) for item in kwargs.pop("issues", [])]
    details = kwargs.pop("details", None)
    if kwargs:
        raise TypeError("unexpected verification fields: " + ", ".join(sorted(kwargs)))
    if details is not None and not isinstance(details, dict):
        raise ValueError("verification details must be an object")
    if status == "passed" and not (artifact_id or artifact_sha256):
        raise ValueError("passed formal verification requires an immutable verification artifact")
    with transaction(unity_dir) as state:
        gate = _require_active_formalization(state)
        candidate = state["formal_candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown formal candidate '{candidate_id}'")
        if candidate["gate_revision"] != gate["revision"]:
            raise ValueError("formal candidate belongs to a stale gate revision")
        owner = candidate.get("verification_owner")
        if candidate["status"] != "verifying" or owner != verifier:
            raise ValueError(
                f"formal candidate verification is not reserved by '{verifier}'"
            )
        verification = {
            "verification_id": _id("verification"), "verifier": verifier,
            "status": status, "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256, "issues": issues,
            "candidate_commit_sha": candidate["commit_sha"],
            "candidate_source_hash": candidate["source_hash"],
            "created_at": time.time(),
        }
        if details:
            verification["details"] = details
        candidate.update(
            verification=verification,
            status={"passed": "verified", "failed": "failed", "blocked": "blocked"}[status],
            verification_owner=None, updated_at=time.time(),
        )
        event = _event(
            state, "formal_candidate_verified", candidate_id=candidate_id,
            verifier=verifier, status=status,
            candidate_source_hash=candidate["source_hash"],
        )
    return {"status": candidate["status"], "candidate": candidate,
            "verification": verification, "event": event}


# Runtime-facing spelling retained separately from the shorter state primitive.
record_formalization_verification = record_formal_verification


def accept_formal_candidate(
    unity_dir: Path, candidate_id: str, author: str, rationale: str = ""
) -> dict:
    author = _text(author, "author", 100)
    rationale = _text(rationale, "rationale", 2_000, required=False)
    with transaction(unity_dir) as state:
        gate = _require_active_formalization(state)
        candidate = state["formal_candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown formal candidate '{candidate_id}'")
        if candidate["gate_revision"] != gate["revision"]:
            raise ValueError("formal candidate belongs to a stale gate revision")
        if candidate["status"] == "accepted":
            return {"status": "accepted", "idempotent": True, "candidate": candidate}
        if candidate["status"] != "verified" or (candidate.get("verification") or {}).get("status") != "passed":
            raise ValueError("formal candidate must pass deterministic verification before acceptance")
        if any(item["target_kind"] == "formal_candidate"
               and item["target_id"] == candidate_id and item["status"] == "open"
               for item in state["objections"].values()):
            raise ValueError("formal candidate has unresolved objections")
        candidate.update(status="accepted", accepted_by=author,
                         accepted_rationale=rationale, updated_at=time.time())
        strategy_id = candidate.get("strategy_id")
        if strategy_id and strategy_id in state["strategies"]:
            strategy = state["strategies"][strategy_id]
            if (strategy["stage"] == "formalizing"
                    and strategy["gate_revision"] == gate["revision"]
                    and strategy["status"] in _ACTIVE_STRATEGIES):
                strategy.update(
                    status="succeeded", outcome_reason="formal candidate accepted",
                    accepted_candidate_id=candidate_id, updated_at=time.time(),
                )
        task = state["formal_tasks"][candidate["task_id"]]
        task.update(status="complete", completed_by=author,
                    accepted_candidate_id=candidate_id, updated_at=time.time())
        for other in state["formal_candidates"].values():
            if (other["candidate_id"] != candidate_id and other["task_id"] == task["task_id"]
                    and other["gate_revision"] == gate["revision"]
                    and other["status"] not in {"accepted", "failed", "rejected", "superseded"}):
                other.update(status="superseded", superseded_by=candidate_id,
                             updated_at=time.time())
                _supersede_open_objections(
                    state, "formal_candidate", {other["candidate_id"]},
                    "another formal candidate for the task was accepted",
                )
        if candidate_id not in gate["accepted_candidate_ids"]:
            gate["accepted_candidate_ids"].append(candidate_id)
        gate["updated_at"] = time.time()
        event = _event(state, "formal_candidate_accepted", candidate_id=candidate_id,
                       task_id=task["task_id"], author=author,
                       source_hash=candidate["source_hash"])
    return {"status": "accepted", "candidate": candidate, "task": task, "event": event}


def close_formalization_gate(
    unity_dir: Path, author: str, rationale: str = "", *, integrated_main_sha: str
) -> dict:
    author = _text(author, "author", 100)
    rationale = _text(rationale, "rationale", 2_000, required=False)
    integrated_main_sha = str(integrated_main_sha or "").strip().lower()
    if not _FULL_COMMIT_RE.fullmatch(integrated_main_sha):
        raise ValueError("integrated_main_sha must be a full 40-character Git commit")
    with transaction(unity_dir) as state:
        gate = state["gates"]["formalization"]
        if gate["status"] == "accepted":
            if gate.get("integrated_main_sha") != integrated_main_sha:
                raise ValueError("formalization gate is already closed on a different main SHA")
            return {"status": "accepted", "idempotent": True, "gate": gate}
        if gate["status"] != "open":
            raise ValueError("formalization gate is not open")
        current = [task for task in state["formal_tasks"].values()
                   if task["gate_revision"] == gate["revision"]
                   and task["status"] not in {"cancelled", "superseded"}]
        unfinished = [task["task_id"] for task in current if task["status"] != "complete"]
        if unfinished:
            raise ValueError("formalization has unfinished tasks: " + ", ".join(unfinished))
        if not gate["accepted_candidate_ids"]:
            raise ValueError("formalization has no accepted verified candidate")
        invalid_accepted = [
            candidate_id for candidate_id in gate["accepted_candidate_ids"]
            if state["formal_candidates"].get(candidate_id, {}).get("status") != "accepted"
        ]
        open_objections = [
            objection["objection_id"] for objection in state["objections"].values()
            if objection.get("target_kind") == "formal_candidate"
            and objection.get("target_id") in gate["accepted_candidate_ids"]
            and objection.get("status") == "open"
        ]
        if invalid_accepted or open_objections:
            raise ValueError(
                "formalization has blocked accepted candidates"
                + (": " + ", ".join(invalid_accepted + open_objections)
                   if invalid_accepted or open_objections else "")
            )
        gate.update(status="accepted", integrated_main_sha=integrated_main_sha,
                    closed_by=author, close_rationale=rationale,
                    updated_at=time.time())
        state["stage"] = "critic"
        event = _event(state, "formalization_gate_closed", author=author,
                       gate_revision=gate["revision"],
                       integrated_main_sha=integrated_main_sha,
                       accepted_candidate_ids=list(gate["accepted_candidate_ids"]))
    return {"status": "accepted", "gate": gate, "event": event}


# ── Formalization feedback to the mathematical solution and terminal outcome ─

def propose_source_fix(
    unity_dir: Path,
    author: str,
    summary: str,
    *,
    candidate_id: str = "",
    subgoal_id: str = "",
    evidence: str = "",
    artifact_id: str = "",
    artifact_sha256: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    """Record a proposed paper repair without changing or reopening the accepted bytes."""
    author = _text(author, "author", 100)
    summary = _text(summary, "summary", 4_000)
    evidence = _text(evidence, "evidence", 4_000, required=False)
    artifact_id = artifact_id or evidence_artifact_id
    artifact_sha256 = _sha256(
        artifact_sha256 or evidence_sha256, "artifact_sha256", required=False
    )
    with transaction(unity_dir) as state:
        accepted = state["gates"]["solution"].get("accepted_candidate_id")
        candidate_id = candidate_id or accepted or ""
        if candidate_id and candidate_id not in state["solution_candidates"]:
            raise ValueError(f"unknown solution candidate '{candidate_id}'")
        if subgoal_id and subgoal_id not in state["subgoals"]:
            raise ValueError(f"unknown subgoal '{subgoal_id}'")
        fid = _id("source-fix")
        fix = {
            "source_fix_id": fid, "fix_id": fid, "candidate_id": candidate_id,
            "subgoal_id": subgoal_id, "author": author, "summary": summary,
            "reason": summary, "evidence": evidence, "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256, "evidence_artifact_id": artifact_id,
            "evidence_sha256": artifact_sha256, "evidence_bytes": evidence_bytes,
            "status": "proposed", "created_at": time.time(), "updated_at": time.time(),
        }
        state["source_fixes"][fid] = fix
        event = _event(state, "source_fix_proposed", source_fix_id=fid,
                       candidate_id=candidate_id, subgoal_id=subgoal_id, author=author)
    return {"status": "proposed", "source_fix": fix, "event": event}

def request_solution_revision(
    unity_dir: Path,
    author: str,
    reason: str,
    *,
    candidate_id: str = "",
    evidence: str = "",
    artifact_id: str = "",
    artifact_sha256: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 4_000)
    evidence = _text(evidence, "evidence", 4_000, required=False)
    artifact_id = artifact_id or evidence_artifact_id
    artifact_sha256 = _sha256(
        artifact_sha256 or evidence_sha256, "artifact_sha256", required=False
    )
    with transaction(unity_dir) as state:
        accepted = state["gates"]["solution"].get("accepted_candidate_id")
        if not accepted:
            raise ValueError("there is no accepted solution candidate to revise")
        if candidate_id and candidate_id != accepted:
            raise ValueError("source-fix candidate_id must be the currently accepted solution")
        fid = _id("source-fix")
        fix = {
            "source_fix_id": fid, "fix_id": fid, "candidate_id": accepted, "author": author,
            "reason": reason, "evidence": evidence, "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256, "evidence_artifact_id": artifact_id,
            "evidence_sha256": artifact_sha256, "evidence_bytes": evidence_bytes,
            "status": "proposed",
            "created_at": time.time(), "updated_at": time.time(),
        }
        state["source_fixes"][fid] = fix
        _reopen_solution_gate(
            state, author, reason,
            repair_mode="reopen_solving", source_fix_id=fid,
        )
        event = _event(state, "solution_revision_requested", source_fix_id=fid,
                       candidate_id=accepted, author=author, reason=reason,
                       gate_revision=state["gates"]["solution"]["revision"])
    return {"status": "proposed", "source_fix": fix,
            "solution_gate": state["gates"]["solution"], "event": event}


def replace_solution_candidate(
    unity_dir: Path,
    author: str,
    accepted_candidate_id: str,
    artifact_id: str,
    sha256: str,
    *,
    reason: str,
    notes: str = "",
    artifact_bytes: int = 0,
    source_path: str = "",
    strategy_id: str = "",
    subgoal_id: str = "",
    evidence: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    """Atomically reopen an accepted solution gate and submit its exact replacement.

    Artifact creation happens before this state transition. All supplied metadata is
    validated before the accepted gate is mutated, so a duplicate or invalid
    replacement cannot strand the run between reopen and submission.
    """
    author = _text(author, "author", 100)
    accepted_candidate_id = _text(accepted_candidate_id, "accepted_candidate_id", 300)
    artifact_id = _text(artifact_id, "artifact_id", 300)
    sha256 = _sha256(sha256)
    reason = _text(reason, "reason", 4_000)
    notes = _text(notes, "notes", 4_000, required=False) or reason
    source_path = _text(source_path, "source_path", 1_000, required=False)
    evidence = _text(evidence, "evidence", 4_000, required=False)
    evidence_artifact_id = _text(
        evidence_artifact_id, "evidence_artifact_id", 300, required=False
    )
    evidence_sha256 = _sha256(evidence_sha256, "evidence_sha256", required=False)
    if isinstance(artifact_bytes, bool) or not isinstance(artifact_bytes, int) or artifact_bytes < 0:
        raise ValueError("artifact_bytes must be a non-negative integer")
    if isinstance(evidence_bytes, bool) or not isinstance(evidence_bytes, int) or evidence_bytes < 0:
        raise ValueError("evidence_bytes must be a non-negative integer")

    with transaction(unity_dir) as state:
        _require_initialized(state)
        gate = state["gates"]["solution"]
        if (gate["status"] != "accepted"
                or gate.get("accepted_candidate_id") != accepted_candidate_id):
            raise ValueError("accepted_candidate_id is not the current accepted solution")
        previous = state["solution_candidates"].get(accepted_candidate_id)
        if not previous or previous["status"] != "accepted":
            raise ValueError("accepted solution candidate record is unavailable")
        duplicate = next(
            (candidate for candidate in state["solution_candidates"].values()
             if candidate["gate_revision"] == gate["revision"]
             and candidate["sha256"] == sha256),
            None,
        )
        if duplicate:
            return {"status": "duplicate", "candidate": duplicate}
        if strategy_id and strategy_id not in state["strategies"]:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if subgoal_id and subgoal_id not in state["subgoals"]:
            raise ValueError(f"unknown subgoal '{subgoal_id}'")

        source_fix_id = _id("source-fix")
        candidate_id = _id("solution")
        source_fix = {
            "source_fix_id": source_fix_id, "fix_id": source_fix_id,
            "candidate_id": accepted_candidate_id, "replacement_candidate_id": candidate_id,
            "subgoal_id": subgoal_id, "author": author, "summary": reason,
            "reason": reason, "evidence": evidence,
            "artifact_id": evidence_artifact_id, "artifact_sha256": evidence_sha256,
            "evidence_artifact_id": evidence_artifact_id,
            "evidence_sha256": evidence_sha256, "evidence_bytes": evidence_bytes,
            "status": "submitted", "created_at": time.time(), "updated_at": time.time(),
        }

        _reopen_solution_gate(
            state, author, reason,
            repair_mode="targeted_source_fix", source_fix_id=source_fix_id,
        )
        previous.update(
            status="superseded", superseded_by=candidate_id,
            superseded_by_sha256=sha256, updated_at=time.time(),
        )
        _supersede_open_objections(
            state, "solution_candidate", {accepted_candidate_id},
            "accepted candidate replaced by corrected bytes",
        )
        candidate = {
            "candidate_id": candidate_id, "gate_revision": gate["revision"],
            "author": author, "artifact_id": artifact_id, "sha256": sha256,
            "artifact_bytes": artifact_bytes, "source_path": source_path,
            "strategy_id": strategy_id, "notes": notes,
            "supersedes": accepted_candidate_id, "source_fix_id": source_fix_id,
            "status": "submitted", "reviews": [],
            "created_at": time.time(), "updated_at": time.time(),
        }
        state["source_fixes"][source_fix_id] = source_fix
        state["solution_candidates"][candidate_id] = candidate
        gate.update(status="review", updated_at=time.time())
        state["stage"] = "solution_review"
        event = _event(
            state, "solution_candidate_replaced", candidate_id=candidate_id,
            supersedes=accepted_candidate_id, source_fix_id=source_fix_id,
            author=author, sha256=sha256, gate_revision=gate["revision"],
        )
    return {"status": "submitted", "source_fix": source_fix,
            "candidate": candidate, "event": event}


def record_formalization_verdict(
    unity_dir: Path,
    author: str,
    verdict: str,
    rationale: str,
    *,
    reopen_task_ids: list[str] | None = None,
    source_fix: str = "",
    expected_reviewed_main_sha: str,
) -> dict:
    if verdict not in {"approved", "revise_formalization", "source_fix", "revise_solution"}:
        raise ValueError(
            "verdict must be approved, revise_formalization, source_fix, or revise_solution"
        )
    author = _text(author, "author", 100)
    rationale = _text(rationale, "rationale", 4_000)
    expected_reviewed_main_sha = str(expected_reviewed_main_sha or "").strip().lower()
    if not _FULL_COMMIT_RE.fullmatch(expected_reviewed_main_sha):
        raise ValueError("expected_reviewed_main_sha must be a full 40-character Git commit")
    reopen_task_ids = list(dict.fromkeys(reopen_task_ids or []))
    source_fix = _text(source_fix, "source_fix", 4_000, required=False)
    with transaction(unity_dir) as state:
        missing = [tid for tid in reopen_task_ids if tid not in state["formal_tasks"]]
        if missing:
            raise ValueError(f"unknown formal tasks: {', '.join(missing)}")
        if state.get("stage") != "critic" or state["gates"]["formalization"]["status"] != "accepted":
            raise ValueError("formalization verdicts require the closed gate's critic stage")
        integrated_main_sha = state["gates"]["formalization"].get("integrated_main_sha", "")
        if expected_reviewed_main_sha != integrated_main_sha:
            raise ValueError(
                "critic reviewed_main_sha does not match the closed formalization gate"
            )
        if verdict == "revise_formalization":
            current_revision = state["gates"]["formalization"]["revision"]
            if not reopen_task_ids:
                reopen_task_ids = [
                    task["task_id"] for task in state["formal_tasks"].values()
                    if task["gate_revision"] == current_revision
                    and task["status"] not in {"cancelled", "superseded"}
                ]
            if not reopen_task_ids:
                raise ValueError("formalization gate has no current tasks to reopen")
            stale = [tid for tid in reopen_task_ids
                     if state["formal_tasks"][tid]["gate_revision"] != current_revision]
            if stale:
                raise ValueError(
                    "reopen tasks must belong to the current formalization gate: "
                    + ", ".join(stale)
                )
        record = {
            "verdict_id": _id("formal-verdict"), "author": author,
            "verdict": verdict, "rationale": rationale,
            "solution_gate_revision": state["gates"]["solution"]["revision"],
            "formalization_gate_revision": state["gates"]["formalization"]["revision"],
            "reviewed_main_sha": expected_reviewed_main_sha,
            "reopen_task_ids": reopen_task_ids, "source_fix": source_fix,
            "status": "active",
            "created_at": time.time(),
        }
        state["formalization_verdicts"].append(record)
        if verdict == "approved":
            if state["gates"]["formalization"]["status"] != "accepted":
                raise ValueError("approved verdict requires a closed formalization gate")
            state.update(stage="complete", outcome="complete")
        elif verdict == "source_fix":
            accepted_candidate_id = state["gates"]["solution"].get("accepted_candidate_id")
            if not accepted_candidate_id:
                raise ValueError("targeted source repair requires an accepted solution candidate")
            source_fix_id = _id("source-fix")
            fix = {
                "source_fix_id": source_fix_id, "fix_id": source_fix_id,
                "candidate_id": accepted_candidate_id, "author": author,
                "summary": source_fix or rationale, "reason": rationale,
                "suggested_fix": source_fix, "status": "requested",
                "trigger_verdict_id": record["verdict_id"],
                "created_at": time.time(), "updated_at": time.time(),
            }
            state["source_fixes"][source_fix_id] = fix
            record["source_fix_id"] = source_fix_id
            _reopen_solution_gate(
                state, author, rationale,
                repair_mode="targeted_source_fix", source_fix_id=source_fix_id,
            )
        elif verdict == "revise_solution":
            _reopen_solution_gate(state, author, rationale)
        else:
            _reopen_formalization_gate(state, author, rationale)
            # Recreate only explicitly requested tasks in the new revision. Historical
            # task records remain immutable audit data under their old revision.
            _recreate_formal_tasks(
                state, reopen_task_ids, author,
                repair_reason=rationale, repair_trigger_id=record["verdict_id"],
            )
        event = _event(state, "formalization_verdict_recorded",
                       verdict_id=record["verdict_id"], author=author, verdict=verdict,
                       rationale=rationale)
    return {"status": verdict, "verdict": record, "event": event,
            "stage": state["stage"]}


def invalidate_critic_approval(
    unity_dir: Path,
    author: str,
    expected_reviewed_main_sha: str,
    reason: str,
) -> dict:
    """Invalidate an exact critic approval when post-review main no longer matches it."""
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 4_000)
    expected_reviewed_main_sha = str(expected_reviewed_main_sha or "").strip().lower()
    if not _FULL_COMMIT_RE.fullmatch(expected_reviewed_main_sha):
        raise ValueError("expected_reviewed_main_sha must be a full 40-character Git commit")
    with transaction(unity_dir) as state:
        approved = next(
            (record for record in reversed(state["formalization_verdicts"])
             if record.get("verdict") == "approved"),
            None,
        )
        if (approved and approved.get("status") == "invalidated"
                and approved.get("reviewed_main_sha") == expected_reviewed_main_sha
                and state.get("stage") == "critic"):
            return {"status": "invalidated", "idempotent": True,
                    "verdict": approved, "stage": "critic"}
        if state.get("stage") != "complete" or state.get("outcome") != "complete":
            raise ValueError("critic approval can only be invalidated from a completed run")
        if not approved or approved.get("reviewed_main_sha") != expected_reviewed_main_sha:
            raise ValueError("latest critic approval is not bound to expected_reviewed_main_sha")
        if (state["gates"]["formalization"].get("integrated_main_sha")
                != expected_reviewed_main_sha):
            raise ValueError("formalization gate is not bound to expected_reviewed_main_sha")
        approved.update(
            status="invalidated", invalidated_by=author,
            invalidation_reason=reason, invalidated_at=time.time(),
        )
        state.update(stage="critic", outcome=None, resume_stage=None)
        event = _event(
            state, "critic_approval_invalidated", verdict_id=approved["verdict_id"],
            author=author, reviewed_main_sha=expected_reviewed_main_sha, reason=reason,
        )
    return {"status": "invalidated", "verdict": approved,
            "stage": "critic", "event": event}


def set_outcome(unity_dir: Path, outcome: str, author: str, reason: str = "") -> dict:
    if outcome not in {"complete", "stopped", "exhausted", "failed"}:
        raise ValueError("outcome must be complete, stopped, exhausted, or failed")
    author = _text(author, "author", 100)
    reason = _text(reason, "reason", 4_000, required=False)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        if outcome == "complete" and state["gates"]["formalization"]["status"] != "accepted":
            raise ValueError("complete outcome requires an accepted formalization gate")
        resume_stage = None
        if outcome != "complete":
            current_stage = state.get("stage")
            resume_stage = (
                current_stage if current_stage in RESUMABLE_STAGES
                else state.get("resume_stage")
            )
        state.update(stage=outcome, outcome=outcome, resume_stage=resume_stage)
        event = _event(state, "solve_outcome_set", outcome=outcome,
                       author=author, reason=reason, resume_stage=resume_stage)
    return {"status": outcome, "stage": outcome, "resume_stage": resume_stage,
            "event": event}


def _infer_resume_stage(state: dict) -> str:
    """Infer a safe continuation point for state written before ``resume_stage`` existed."""
    solution = state["gates"]["solution"]
    formalization = state["gates"]["formalization"]
    if solution.get("status") == "review":
        return "solution_review"
    if solution.get("status") != "accepted":
        return "solving"
    if formalization.get("status") == "waiting":
        return "chunking"
    if formalization.get("status") == "open":
        return "formalizing"
    return "critic"


def resume_run(unity_dir: Path, author: str = "Unity") -> dict:
    """Resume a stopped, exhausted, or failed run at its last authoritative stage."""
    author = _text(author, "author", 100)
    with transaction(unity_dir) as state:
        _require_initialized(state)
        if state.get("stage") in RESUMABLE_STAGES and state.get("outcome") is None:
            return {"status": "running", "idempotent": True,
                    "stage": state["stage"]}
        if state.get("outcome") == "complete" or state.get("stage") == "complete":
            return {"status": "complete", "idempotent": True, "stage": "complete"}
        if state.get("outcome") not in {"stopped", "exhausted", "failed"}:
            raise ValueError("solve run is not resumable")
        previous_outcome = state["outcome"]
        resume_stage = state.get("resume_stage")
        if resume_stage not in RESUMABLE_STAGES:
            resume_stage = _infer_resume_stage(state)
        reclaimed_tasks: list[str] = []
        reclaimed_verifications: list[str] = []
        if resume_stage == "formalizing":
            gate_revision = state["gates"]["formalization"]["revision"]
            for task in state["formal_tasks"].values():
                if task["gate_revision"] == gate_revision and task["status"] == "claimed":
                    task.update(
                        status="pending", owner=None,
                        release_reason="runtime resumed after interruption",
                        updated_at=time.time(),
                    )
                    reclaimed_tasks.append(task["task_id"])
            for candidate in state["formal_candidates"].values():
                if (candidate["gate_revision"] == gate_revision
                        and candidate["status"] == "verifying"):
                    candidate.update(
                        status="submitted", verification_owner=None,
                        verification_started_at=None, updated_at=time.time(),
                    )
                    reclaimed_verifications.append(candidate["candidate_id"])
        state.update(stage=resume_stage, outcome=None, resume_stage=None)
        event = _event(
            state, "solve_run_resumed", author=author,
            previous_outcome=previous_outcome, resume_stage=resume_stage,
            reclaimed_tasks=reclaimed_tasks,
            reclaimed_verifications=reclaimed_verifications,
        )
    return {"status": "running", "stage": resume_stage,
            "previous_outcome": previous_outcome,
            "reclaimed_tasks": reclaimed_tasks,
            "reclaimed_verifications": reclaimed_verifications,
            "event": event}


submit_formalization_verdict = record_formalization_verdict


def materialize_accepted_solution(unity_dir: Path) -> dict:
    """Return the immutable artifact metadata currently admitted through the solution gate."""
    state = load_state(unity_dir)
    gate = state["gates"]["solution"]
    candidate_id = gate.get("accepted_candidate_id")
    if gate.get("status") != "accepted" or not candidate_id:
        raise ValueError("there is no accepted solution artifact")
    candidate = state["solution_candidates"].get(candidate_id)
    if not candidate:
        raise ValueError("accepted solution candidate is missing from state")
    return {
        "candidate_id": candidate_id,
        "gate_revision": gate["revision"],
        "artifact_id": candidate["artifact_id"],
        "sha256": candidate["sha256"],
        "artifact_bytes": candidate.get("artifact_bytes", 0),
        "source_path": candidate.get("source_path", ""),
    }
