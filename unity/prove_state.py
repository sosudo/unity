"""Authoritative, file-backed coordination state for the prove runtime."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 2
STRATEGY_STATUSES = {
    "registered", "claimed", "paused", "incorrect", "succeeded", "cancelled",
}
CANDIDATE_STATUSES = {
    "submitted", "blocked", "acceptable", "merging", "merged", "failed", "superseded",
    "rejected",
}
_ACTIVE_STRATEGIES = {"registered", "claimed", "paused"}
_TERMINAL_CANDIDATES = {"merged", "failed", "superseded", "rejected"}
FINDING_STATUSES = {"active", "superseded"}

_FINDING_KIND_MAX_LENGTH = 64
_FINDING_TITLE_MAX_LENGTH = 200
_FINDING_CONTENT_MAX_LENGTH = 4000
_FINDING_EVIDENCE_MAX_LENGTH = 4000


def state_path(forum_dir: Path) -> Path:
    return Path(forum_dir) / "prove-state.json"


def _default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "declarations": {},
        "strategies": {},
        "findings": {},
        "candidates": {},
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
    for key in ("declarations", "strategies", "findings", "candidates"):
        if not isinstance(base.get(key), dict):
            base[key] = {}
    if not isinstance(base.get("events"), list):
        base["events"] = []
    return base


def load_state(forum_dir: Path) -> dict:
    """Read current state. Mutations must use :func:`transaction`."""
    return _read_unlocked(Path(forum_dir))


def _write_unlocked(forum_dir: Path, state: dict) -> None:
    forum_dir = Path(forum_dir)
    forum_dir.mkdir(parents=True, exist_ok=True)
    state["schema_version"] = SCHEMA_VERSION
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".prove-state-", suffix=".json", dir=forum_dir)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, state_path(forum_dir))
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def transaction(forum_dir: Path) -> Iterator[dict]:
    """Lock, load, mutate, and atomically persist the prove state."""
    forum_dir = Path(forum_dir)
    forum_dir.mkdir(parents=True, exist_ok=True)
    with (forum_dir / "prove-state.lock").open("a+") as lock:
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


def _event(state: dict, kind: str, **fields) -> dict:
    event = {
        "event_id": "event-" + uuid.uuid4().hex[:12],
        "kind": kind,
        "timestamp": time.time(),
        **fields,
    }
    state.setdefault("events", []).append(event)
    # Events are wake-up/audit records, not model memory. Keep the live file bounded.
    state["events"] = state["events"][-1000:]
    return event


def initialize_from_dag(
    forum_dir: Path,
    dag_path: Path,
    main_sha: str,
    *,
    reset: bool = False,
) -> dict:
    dag = json.loads(Path(dag_path).read_text())
    chunks = dag.get("chunks", [])
    with transaction(forum_dir) as state:
        if reset:
            state.clear()
            state.update(_default_state())
        declarations = state.setdefault("declarations", {})
        wanted = set()
        for chunk in chunks:
            decl = str(chunk.get("lean_decl") or chunk.get("id") or "").strip()
            if not decl:
                continue
            wanted.add(decl)
            current = declarations.get(decl, {})
            declarations[decl] = {
                "decl": decl,
                "chunk_id": chunk.get("id", decl),
                "title": chunk.get("title", decl),
                "file": chunk.get("lean_file", ""),
                "dependencies": list(chunk.get("dependencies", [])),
                "status": current.get("status", "unresolved"),
                "accepted_candidate": current.get("accepted_candidate"),
                "merged_commit": current.get("merged_commit"),
                "updated_at": time.time(),
            }
        for decl in list(declarations):
            if decl not in wanted and declarations[decl].get("status") != "solved":
                del declarations[decl]
        state["main_sha"] = main_sha
        _event(state, "runtime_initialized", declarations=sorted(wanted), main_sha=main_sha)
    return load_state(forum_dir)


def normalize_strategy(description: str) -> str:
    return re.sub(r"\s+", " ", description.strip()).casefold()


def normalize_finding_kind(kind: str) -> str:
    """Return a stable key for an agent-defined finding kind."""
    normalized = re.sub(r"[^a-z0-9]+", "_", kind.strip().casefold()).strip("_")
    if not normalized:
        raise ValueError("kind must contain at least one letter or number")
    if len(normalized) > _FINDING_KIND_MAX_LENGTH:
        raise ValueError(f"normalized kind must be at most {_FINDING_KIND_MAX_LENGTH} characters")
    return normalized


def _finding_text(value: str, field: str, limit: int, *, required: bool = True) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    if required and not cleaned:
        raise ValueError(f"{field} must be non-empty")
    if len(cleaned) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return cleaned


def publish_finding(
    forum_dir: Path,
    author: str,
    kind: str,
    title: str,
    content: str,
    confidence: int,
    decl: str = "",
    strategy_id: str = "",
    evidence: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    """Publish a live proof-search finding into authoritative prove state.

    Kinds are agent-defined and normalized for exact duplicate detection. Confidence is
    an integer from 0 through 100; 100 is reserved for directly verified findings and
    therefore requires concrete evidence or an artifact reference.
    """
    author = _finding_text(author, "author", 100)
    kind_key = normalize_finding_kind(kind)
    title = _finding_text(title, "title", _FINDING_TITLE_MAX_LENGTH)
    content = _finding_text(content, "content", _FINDING_CONTENT_MAX_LENGTH)
    evidence = _finding_text(
        evidence, "evidence", _FINDING_EVIDENCE_MAX_LENGTH, required=False
    )
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        raise ValueError("confidence must be an integer from 0 through 100")
    if not 0 <= confidence <= 100:
        raise ValueError("confidence must be an integer from 0 through 100")
    if confidence == 100 and not (evidence or evidence_artifact_id):
        raise ValueError("confidence 100 requires concrete evidence or an artifact reference")

    with transaction(forum_dir) as state:
        if decl and decl not in state["declarations"]:
            raise ValueError(f"unknown prove declaration '{decl}'")
        if strategy_id:
            strategy = state["strategies"].get(strategy_id)
            if not strategy:
                raise ValueError(f"unknown strategy '{strategy_id}'")
            strategy_decl = strategy.get("decl", "")
            if decl and strategy_decl and strategy_decl != decl:
                raise ValueError("finding declaration does not match its strategy declaration")
            if not decl:
                decl = strategy_decl

        title_key = normalize_strategy(title)
        for finding in state["findings"].values():
            if (
                finding.get("status") == "active"
                and finding.get("decl", "") == decl
                and finding.get("kind") == kind_key
                and finding.get("title_key") == title_key
            ):
                return {"status": "duplicate", "finding": finding}

        finding_id = "finding-" + uuid.uuid4().hex[:12]
        now = time.time()
        finding = {
            "finding_id": finding_id,
            "decl": decl,
            "strategy_id": strategy_id or None,
            "author": author,
            "kind": kind_key,
            "title": title,
            "title_key": title_key,
            "content": content,
            "confidence": confidence,
            "evidence": evidence,
            "evidence_artifact_id": evidence_artifact_id or None,
            "evidence_sha256": evidence_sha256 or None,
            "evidence_bytes": evidence_bytes or 0,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        state["findings"][finding_id] = finding
        event = _event(
            state,
            "finding_published",
            finding_id=finding_id,
            decl=decl,
            strategy_id=strategy_id,
            author=author,
            finding_kind=kind_key,
            confidence=confidence,
        )
        return {"status": "published", "finding": finding, "event": event}


def supersede_finding(
    forum_dir: Path,
    finding_id: str,
    author: str,
    reason: str,
    replacement_id: str = "",
) -> dict:
    """Retire an active finding whose live conclusion is no longer current."""
    author = _finding_text(author, "author", 100)
    reason = _finding_text(reason, "reason", 1000)
    with transaction(forum_dir) as state:
        finding = state["findings"].get(finding_id)
        if not finding:
            raise ValueError(f"unknown finding '{finding_id}'")
        if finding.get("status") == "superseded":
            return {"status": "superseded", "finding": finding, "idempotent": True}

        replacement = None
        if replacement_id:
            if replacement_id == finding_id:
                raise ValueError("a finding cannot supersede itself")
            replacement = state["findings"].get(replacement_id)
            if not replacement:
                raise ValueError(f"unknown replacement finding '{replacement_id}'")
            if replacement.get("status") != "active":
                raise ValueError("replacement finding must be active")
            if replacement.get("decl", "") != finding.get("decl", ""):
                raise ValueError("replacement finding must concern the same declaration")

        now = time.time()
        finding.update(
            status="superseded",
            superseded_at=now,
            superseded_by=replacement_id or None,
            superseded_by_author=author,
            supersession_reason=reason,
            updated_at=now,
        )
        event = _event(
            state,
            "finding_superseded",
            finding_id=finding_id,
            decl=finding.get("decl", ""),
            author=author,
            reason=reason,
            replacement_id=replacement_id,
        )
        return {
            "status": "superseded",
            "finding": finding,
            "replacement": replacement,
            "event": event,
        }


def register_strategy(forum_dir: Path, author: str, description: str, decl: str = "") -> dict:
    description = re.sub(r"\s+", " ", description.strip())
    if not author.strip():
        raise ValueError("author must be non-empty")
    if not description:
        raise ValueError("description must be non-empty")
    with transaction(forum_dir) as state:
        if decl and decl not in state["declarations"]:
            raise ValueError(f"unknown prove declaration '{decl}'")
        if decl and state["declarations"][decl].get("status") == "solved":
            raise ValueError(f"declaration '{decl}' is already solved")
        if decl and state["declarations"][decl].get("status") == "candidate_pending":
            pending = [candidate["candidate_id"] for candidate in state["candidates"].values()
                       if candidate.get("decl") == decl
                       and candidate.get("status") in {"submitted", "acceptable", "merging"}]
            return {"status": "candidate_pending", "decl": decl, "candidates": pending}
        key = normalize_strategy(description)
        for strategy in state["strategies"].values():
            if (strategy.get("decl", "") == decl and strategy.get("strategy_key") == key
                    and strategy.get("status") in _ACTIVE_STRATEGIES):
                return {"status": "duplicate", "strategy": strategy}
        strategy_id = "strategy-" + uuid.uuid4().hex[:12]
        now = time.time()
        strategy = {
            "strategy_id": strategy_id,
            "decl": decl,
            "description": description,
            "strategy_key": key,
            "creator": author,
            "owner": None,
            "status": "registered",
            "created_at": now,
            "updated_at": now,
            "attempts": [],
            "candidate_id": None,
        }
        state["strategies"][strategy_id] = strategy
        _event(state, "strategy_registered", strategy_id=strategy_id, decl=decl, author=author)
        return {"status": "registered", "strategy": strategy}


def claim_strategy(forum_dir: Path, strategy_id: str, author: str, main_sha: str) -> dict:
    with transaction(forum_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if strategy["status"] == "claimed":
            if strategy.get("owner") == author:
                return {"status": "already_owned", "strategy": strategy}
            return {
                "status": "conflict",
                "strategy_id": strategy_id,
                "owner": strategy.get("owner"),
                "strategy_status": strategy["status"],
            }
        if strategy["status"] != "registered":
            return {"status": "unavailable", "strategy": strategy}
        decl = strategy.get("decl", "")
        if decl and state["declarations"].get(decl, {}).get("status") == "solved":
            strategy["status"] = "cancelled"
            strategy["cancel_reason"] = "declaration already solved"
            strategy["updated_at"] = time.time()
            return {"status": "unavailable", "strategy": strategy}
        strategy.update(owner=author, status="claimed", claimed_at=time.time(),
                        updated_at=time.time(), claimed_main_sha=main_sha)
        _event(state, "strategy_claimed", strategy_id=strategy_id, decl=decl, author=author)
        return {"status": "claimed", "strategy": strategy}


def unclaim_strategy(forum_dir: Path, strategy_id: str, author: str, reason: str = "") -> dict:
    with transaction(forum_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if strategy.get("owner") != author:
            raise ValueError(f"strategy '{strategy_id}' is not owned by {author}")
        if strategy["status"] not in {"claimed", "paused"}:
            return {"status": "unchanged", "strategy": strategy}
        strategy["attempts"].append({
            "author": author, "outcome": "released", "reason": reason, "timestamp": time.time(),
        })
        strategy.update(owner=None, status="registered", updated_at=time.time())
        strategy.pop("paused_from", None)
        _event(state, "strategy_unclaimed", strategy_id=strategy_id,
               decl=strategy.get("decl", ""), author=author, reason=reason)
        return {"status": "registered", "strategy": strategy}


def mark_strategy_incorrect(
    forum_dir: Path,
    strategy_id: str,
    author: str,
    reason: str,
    evidence: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    if not reason.strip():
        raise ValueError("reason is required")
    with transaction(forum_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if strategy.get("owner") not in (None, author) and strategy.get("creator") != author:
            raise ValueError(f"strategy '{strategy_id}' is owned by {strategy.get('owner')}")
        if strategy["status"] in {"succeeded", "cancelled"}:
            return {"status": "unchanged", "strategy": strategy}
        strategy["attempts"].append({
            "author": author, "outcome": "incorrect", "reason": reason,
            "evidence": evidence,
            "evidence_artifact_id": evidence_artifact_id or None,
            "evidence_sha256": evidence_sha256 or None,
            "evidence_bytes": evidence_bytes or 0,
            "timestamp": time.time(),
        })
        strategy.update(owner=None, status="incorrect", incorrect_reason=reason,
                        evidence=evidence,
                        evidence_artifact_id=evidence_artifact_id or None,
                        evidence_sha256=evidence_sha256 or None,
                        evidence_bytes=evidence_bytes or 0,
                        updated_at=time.time())
        _event(state, "strategy_incorrect", strategy_id=strategy_id,
               decl=strategy.get("decl", ""), author=author, reason=reason)
        return {"status": "incorrect", "strategy": strategy}


def release_author_claims(forum_dir: Path, author: str, reason: str) -> list[str]:
    released = []
    with transaction(forum_dir) as state:
        for strategy in state["strategies"].values():
            if strategy.get("owner") != author or strategy.get("status") not in {"claimed", "paused"}:
                continue
            strategy["attempts"].append({
                "author": author, "outcome": "cancelled", "reason": reason,
                "timestamp": time.time(),
            })
            strategy.update(owner=None, status="registered", updated_at=time.time())
            strategy.pop("paused_from", None)
            released.append(strategy["strategy_id"])
        if released:
            _event(state, "author_claims_released", author=author, strategies=released, reason=reason)
    return released


def emit_candidate(
    forum_dir: Path,
    strategy_id: str,
    author: str,
    commit_sha: str,
    decl: str = "",
    notes: str = "",
    supersedes: str = "",
) -> dict:
    with transaction(forum_dir) as state:
        strategy = state["strategies"].get(strategy_id)
        if not strategy:
            raise ValueError(f"unknown strategy '{strategy_id}'")
        if strategy.get("owner") != author or strategy.get("status") != "claimed":
            raise ValueError(f"strategy '{strategy_id}' is not actively owned by {author}")
        target = decl or strategy.get("decl", "")
        if not target:
            raise ValueError("decl is required for a general strategy")
        if strategy.get("decl") and strategy["decl"] != target:
            raise ValueError("candidate declaration does not match the strategy declaration")
        declaration = state["declarations"].get(target)
        if not declaration:
            raise ValueError(f"unknown prove declaration '{target}'")
        if declaration.get("status") == "solved":
            raise ValueError(f"declaration '{target}' is already solved")
        if supersedes:
            prior = state["candidates"].get(supersedes)
            if not prior or prior.get("decl") != target:
                raise ValueError("supersedes must name a candidate for the same declaration")
            if prior["status"] not in _TERMINAL_CANDIDATES:
                prior["status"] = "superseded"
                prior["superseded_by_pending"] = True
                prior["updated_at"] = time.time()
        candidate_id = "candidate-" + uuid.uuid4().hex[:12]
        now = time.time()
        candidate = {
            "candidate_id": candidate_id,
            "decl": target,
            "strategy_id": strategy_id,
            "author": author,
            "commit_sha": commit_sha,
            "notes": notes,
            "supersedes": supersedes or None,
            "status": "submitted",
            "endorsements": [],
            "objections": [],
            "created_at": now,
            "updated_at": now,
        }
        state["candidates"][candidate_id] = candidate
        strategy.update(status="succeeded", candidate_id=candidate_id, updated_at=now)
        declaration.update(status="candidate_pending", updated_at=now)
        paused = []
        for other in state["strategies"].values():
            if other["strategy_id"] == strategy_id or other.get("decl") != target:
                continue
            if other.get("status") in {"registered", "claimed"}:
                other["paused_from"] = other["status"]
                other["status"] = "paused"
                other["pause_reason"] = f"candidate {candidate_id} submitted"
                other["updated_at"] = now
                paused.append(other["strategy_id"])
        event = _event(state, "candidate_submitted", candidate_id=candidate_id, decl=target,
                       author=author, paused_strategies=paused)
        return {"status": "submitted", "candidate": candidate, "event": event,
                "paused_strategies": paused}


def _recompute_candidate(state: dict, candidate: dict) -> None:
    if candidate["status"] in _TERMINAL_CANDIDATES | {"merging"}:
        return
    open_objections = [item for item in candidate["objections"] if item["status"] == "open"]
    if open_objections:
        candidate["status"] = "blocked"
    elif candidate["endorsements"]:
        candidate["status"] = "acceptable"
    else:
        candidate["status"] = "submitted"
    candidate["updated_at"] = time.time()


def endorse_candidate(forum_dir: Path, candidate_id: str, author: str, review: str = "") -> dict:
    with transaction(forum_dir) as state:
        candidate = state["candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown candidate '{candidate_id}'")
        if candidate["author"] == author:
            raise ValueError("candidate authors cannot endorse their own candidate")
        if candidate["status"] in _TERMINAL_CANDIDATES | {"merging"}:
            return {"status": "unchanged", "candidate": candidate}
        existing = next((item for item in candidate["endorsements"] if item["author"] == author), None)
        if not existing:
            candidate["endorsements"].append({
                "author": author, "review": review, "timestamp": time.time(),
            })
        _recompute_candidate(state, candidate)
        event = _event(state, "candidate_endorsed", candidate_id=candidate_id,
                       decl=candidate["decl"], author=author, candidate_status=candidate["status"])
        return {"status": candidate["status"], "candidate": candidate, "event": event}


def _unpause_strategies(state: dict, decl: str, reason: str) -> list[str]:
    unpaused = []
    for strategy in state["strategies"].values():
        if strategy.get("decl") != decl or strategy.get("status") != "paused":
            continue
        strategy["status"] = strategy.pop("paused_from", "registered")
        strategy["resume_reason"] = reason
        strategy["updated_at"] = time.time()
        unpaused.append(strategy["strategy_id"])
    return unpaused


def object_candidate(
    forum_dir: Path,
    candidate_id: str,
    author: str,
    reason: str,
    evidence: str = "",
    evidence_artifact_id: str = "",
    evidence_sha256: str = "",
    evidence_bytes: int = 0,
) -> dict:
    if not reason.strip():
        raise ValueError("reason is required")
    with transaction(forum_dir) as state:
        candidate = state["candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown candidate '{candidate_id}'")
        if candidate["author"] == author:
            raise ValueError("candidate authors cannot object to their own candidate")
        if candidate["status"] in _TERMINAL_CANDIDATES | {"merging"}:
            return {"status": "unchanged", "candidate": candidate}
        objection_id = "objection-" + uuid.uuid4().hex[:12]
        candidate["objections"].append({
            "objection_id": objection_id,
            "author": author,
            "reason": reason,
            "evidence": evidence,
            "evidence_artifact_id": evidence_artifact_id or None,
            "evidence_sha256": evidence_sha256 or None,
            "evidence_bytes": evidence_bytes or 0,
            "status": "open",
            "timestamp": time.time(),
        })
        _recompute_candidate(state, candidate)
        unpaused = _unpause_strategies(state, candidate["decl"], f"candidate {candidate_id} objected")
        state["declarations"][candidate["decl"]].update(status="unresolved", updated_at=time.time())
        event = _event(state, "candidate_objected", candidate_id=candidate_id,
                       objection_id=objection_id, decl=candidate["decl"], author=author,
                       unpaused_strategies=unpaused)
        return {"status": "blocked", "candidate": candidate, "event": event,
                "unpaused_strategies": unpaused}


def resolve_objection(
    forum_dir: Path,
    candidate_id: str,
    objection_id: str,
    author: str,
    resolution: str,
) -> dict:
    with transaction(forum_dir) as state:
        candidate = state["candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown candidate '{candidate_id}'")
        objection = next((item for item in candidate["objections"]
                           if item["objection_id"] == objection_id), None)
        if not objection:
            raise ValueError(f"unknown objection '{objection_id}'")
        if objection["author"] != author:
            raise ValueError("only the objector can resolve this objection")
        if objection["status"] == "resolved":
            return {"status": candidate["status"], "candidate": candidate}
        objection.update(status="resolved", resolution=resolution, resolved_at=time.time())
        _recompute_candidate(state, candidate)
        if candidate["status"] in {"submitted", "acceptable"}:
            state["declarations"][candidate["decl"]].update(
                status="candidate_pending", updated_at=time.time()
            )
        event = _event(state, "candidate_objection_resolved", candidate_id=candidate_id,
                       objection_id=objection_id, decl=candidate["decl"], author=author,
                       candidate_status=candidate["status"])
        return {"status": candidate["status"], "candidate": candidate, "event": event}


def begin_merge(forum_dir: Path, candidate_id: str, author: str) -> dict:
    with transaction(forum_dir) as state:
        candidate = state["candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown candidate '{candidate_id}'")
        if candidate["status"] == "merged":
            return {"status": "merged", "candidate": candidate, "idempotent": True}
        if candidate["status"] == "merging":
            return {"status": "merging", "candidate": candidate, "conflict": True}
        if candidate["status"] != "acceptable":
            raise ValueError(f"candidate '{candidate_id}' is not acceptable")
        candidate.update(status="merging", merging_by=author, updated_at=time.time())
        event = _event(state, "candidate_merge_started", candidate_id=candidate_id,
                       decl=candidate["decl"], author=author)
        return {"status": "merging", "candidate": candidate, "event": event}


def finish_merge(
    forum_dir: Path,
    candidate_id: str,
    *,
    success: bool,
    main_sha: str = "",
    error: str = "",
    build: dict | None = None,
) -> dict:
    with transaction(forum_dir) as state:
        candidate = state["candidates"].get(candidate_id)
        if not candidate:
            raise ValueError(f"unknown candidate '{candidate_id}'")
        if candidate["status"] == "merged":
            return {"status": "merged", "candidate": candidate, "idempotent": True}
        decl = candidate["decl"]
        if build:
            candidate["merge_build"] = build
        if success:
            candidate.update(status="merged", merged_commit=main_sha, updated_at=time.time())
            declaration = state["declarations"][decl]
            declaration.update(status="solved", accepted_candidate=candidate_id,
                               merged_commit=main_sha, updated_at=time.time())
            state["main_sha"] = main_sha
            cancelled = []
            for strategy in state["strategies"].values():
                if strategy.get("decl") != decl or strategy.get("strategy_id") == candidate["strategy_id"]:
                    continue
                if strategy.get("status") not in {"incorrect", "succeeded", "cancelled"}:
                    strategy.update(status="cancelled", owner=None,
                                    cancel_reason=f"candidate {candidate_id} merged",
                                    updated_at=time.time())
                    cancelled.append(strategy["strategy_id"])
            for other in state["candidates"].values():
                if (other["candidate_id"] != candidate_id and other.get("decl") == decl
                        and other.get("status") not in _TERMINAL_CANDIDATES):
                    other.update(status="superseded", superseded_by=candidate_id,
                                 updated_at=time.time())
            event = _event(
                state,
                "candidate_merged",
                candidate_id=candidate_id,
                decl=decl,
                main_sha=main_sha,
                cancelled_strategies=cancelled,
                build_artifact_id=(build or {}).get("artifact_id"),
                build_returncode=(build or {}).get("returncode"),
            )
            return {"status": "merged", "candidate": candidate, "event": event,
                    "cancelled_strategies": cancelled}
        candidate.update(status="failed", merge_error=error, updated_at=time.time())
        state["declarations"][decl].update(status="unresolved", updated_at=time.time())
        unpaused = _unpause_strategies(state, decl, f"candidate {candidate_id} merge failed")
        event = _event(
            state,
            "candidate_merge_failed",
            candidate_id=candidate_id,
            decl=decl,
            error=error,
            unpaused_strategies=unpaused,
            build_artifact_id=(build or {}).get("artifact_id"),
            build_returncode=(build or {}).get("returncode"),
        )
        return {"status": "failed", "candidate": candidate, "event": event,
                "unpaused_strategies": unpaused}


def record_sync(forum_dir: Path, author: str, main_sha: str, released: list[str]) -> dict:
    with transaction(forum_dir) as state:
        event = _event(state, "worktree_synced", author=author, main_sha=main_sha,
                       released_strategies=released)
        return {"status": "synced", "author": author, "main_sha": main_sha, "event": event}


def apply_critic_reopens(forum_dir: Path, requested: object) -> dict:
    """Reopen solved declarations named by a structured critic verdict.

    Critic output is model-authored, so malformed or stale entries are rejected without
    mutating unrelated goals. Accepted source remains in Git for the next candidate to repair.
    """
    if not isinstance(requested, list):
        return {"reopened": [], "rejected": [{"reason": "reopen must be a list"}]}

    reopened: list[dict] = []
    rejected: list[dict] = []
    with transaction(forum_dir) as state:
        for raw in requested:
            if not isinstance(raw, dict):
                rejected.append({"entry": raw, "reason": "reopen entry must be an object"})
                continue
            decl = str(raw.get("decl") or "").strip()
            candidate_id = str(raw.get("candidate_id") or "").strip()
            reason = str(raw.get("reason") or "").strip()
            declaration = state.get("declarations", {}).get(decl)
            if not decl or declaration is None:
                rejected.append({"entry": raw, "reason": f"unknown declaration '{decl}'"})
                continue
            if not reason:
                rejected.append({"entry": raw, "reason": "reopen reason is required"})
                continue
            if declaration.get("status") != "solved":
                rejected.append({"entry": raw, "reason": f"declaration '{decl}' is not solved"})
                continue

            accepted_id = declaration.get("accepted_candidate") or ""
            if candidate_id and candidate_id != accepted_id:
                rejected.append({
                    "entry": raw,
                    "reason": f"candidate '{candidate_id}' is not the accepted candidate for '{decl}'",
                })
                continue
            candidate_id = candidate_id or accepted_id
            candidate = state.get("candidates", {}).get(candidate_id) if candidate_id else None
            if candidate_id and candidate is None:
                rejected.append({"entry": raw, "reason": f"unknown candidate '{candidate_id}'"})
                continue

            now = time.time()
            if candidate is not None:
                candidate.update(
                    status="rejected",
                    critic_rejection_reason=reason,
                    rejected_at=now,
                    updated_at=now,
                )
            declaration.update(
                status="unresolved",
                accepted_candidate=None,
                merged_commit=None,
                critic_rejection_reason=reason,
                updated_at=now,
            )
            event = _event(
                state,
                "critic_reopened",
                decl=decl,
                candidate_id=candidate_id,
                reason=reason,
            )
            reopened.append({
                "decl": decl,
                "candidate_id": candidate_id,
                "reason": reason,
                "event_id": event["event_id"],
            })
    return {"reopened": reopened, "rejected": rejected}


def all_solved(state: dict) -> bool:
    declarations = state.get("declarations", {})
    return bool(declarations) and all(item.get("status") == "solved" for item in declarations.values())


def events_after(state: dict, timestamp: float) -> list[dict]:
    return [event for event in state.get("events", []) if event.get("timestamp", 0) > timestamp]
