"""Profile-scoped MCP control plane for the solve runtime.

This module is deliberately separate from :mod:`unity.forum.server`.  The latter is
the frozen Forum/prove compatibility surface; this server owns solve-only schemas and
stores authoritative state through :mod:`unity.solve_state`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from fastmcp import FastMCP

from .. import artifacts as artifact_store
from .. import solve_state as solve_store
from . import server as legacy_forum


PROFILES = {
    "solving", "solution_review", "chunking", "formalizing", "critic",
    "retrospective",
}

RUN_DIR = Path(".unity/solve")
PROJECT_ROOT = Path.cwd()
FORUM_DIR = RUN_DIR / "forum"

_STATE_INLINE_CHARS = 4_000
_DEFAULT_FORUM_POST_BYTES = 16_000
_DEFAULT_FORUM_READ_BYTES = 64_000
_DEFAULT_STATUS_BYTES = 96_000
_DEFAULT_STATUS_RECORDS = 40
_OMIT = object()


def configure(run_dir: Path, project_root: Path, forum_dir: Path | None = None) -> None:
    """Configure process-local paths before serving or calling tools in tests."""
    global RUN_DIR, PROJECT_ROOT, FORUM_DIR
    RUN_DIR = Path(run_dir).resolve()
    PROJECT_ROOT = Path(project_root).resolve()
    FORUM_DIR = (
        Path(forum_dir).resolve() if forum_dir is not None
        else RUN_DIR / "forum"
    )
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    legacy_forum.FORUM_DIR = FORUM_DIR
    legacy_forum.PROJECT_ROOT = PROJECT_ROOT
    # Solve coordination has no activity economy.  The legacy Forum is reused only
    # as a discussion/artifact substrate, never as its authoritative state.
    legacy_forum.ICRL_ENABLED = False


def _unity_dir() -> Path:
    # solve_state deliberately accepts .unity rather than the nested solve run dir.
    return RUN_DIR.parent


def _artifacts_dir() -> Path:
    return _unity_dir() / "artifacts"


def _require_bound_author(author: str) -> str:
    """Bind model-visible authorship to the identity of the spawned agent.

    Unity starts one solve MCP process per agent with ``UNITY_AGENT_NAME`` in
    its environment.  Trusting a model-supplied ``author`` without comparing it
    to that process identity would let one worker claim, review, or release work
    as another.  Keeping the environment optional preserves direct in-process
    administration and tests; controller mutations use :mod:`solve_state`
    directly and therefore do not pass through this model-facing guard.
    """
    claimed = str(author or "").strip()
    if not claimed:
        raise ValueError("author must be a non-empty agent name")
    bound = str(os.getenv("UNITY_AGENT_NAME", "") or "").strip()
    if bound and claimed != bound:
        raise PermissionError(
            f"author '{claimed}' does not match this solve worker's bound identity"
        )
    return claimed


def _configured_limit(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _forum_post_limit() -> int:
    return _configured_limit(
        "UNITY_SOLVE_FORUM_POST_BYTES", _DEFAULT_FORUM_POST_BYTES, 256, 32_000,
    )


def _validate_forum_content(content: str, *, label: str = "forum content") -> str:
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{label} must be a non-empty string")
    content_bytes = len(content.encode("utf-8"))
    maximum = _forum_post_limit()
    if content_bytes > maximum:
        raise ValueError(
            f"{label} is {content_bytes} bytes; the limit is {maximum}. "
            "Store large evidence as an artifact and post its artifact id instead."
        )
    return content


def _status_record_limit() -> int:
    return _configured_limit(
        "UNITY_SOLVE_STATUS_RECORDS", _DEFAULT_STATUS_RECORDS, 5, 100,
    )


def _json_bytes(value) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8"))


def _truncate_utf8(value: str, maximum: int) -> tuple[str, bool]:
    payload = value.encode("utf-8")
    if len(payload) <= maximum:
        return value, False
    if maximum <= 3:
        return "", True
    suffix = "…"
    available = maximum - len(suffix.encode("utf-8"))
    return payload[:available].decode("utf-8", errors="ignore") + suffix, True


def _fit_json(value, budget: int, *, depth: int = 0) -> tuple[object, bool]:
    """Return a JSON value that fits an exact UTF-8 byte budget.

    State and Forum files are telemetry stores and can legitimately become
    large.  MCP responses are model context, so this walks them in their
    existing priority order, caps individual strings/containers, and omits the
    tail once the byte budget is exhausted.
    """
    if budget < 2:
        return _OMIT, True
    if depth >= 10:
        marker, _ = _truncate_utf8("[nested value omitted]", budget - 2)
        if _json_bytes(marker) <= budget:
            return marker, True
        return _OMIT, True
    if value is None or isinstance(value, (bool, int, float)):
        return (value, False) if _json_bytes(value) <= budget else (_OMIT, True)
    if isinstance(value, str):
        # A single field must not monopolize a response even when the aggregate
        # budget is large.  Exact source and tool output belong in artifacts.
        clipped, truncated = _truncate_utf8(value, min(4_000, max(0, budget - 2)))
        while clipped and _json_bytes(clipped) > budget:
            clipped, _ = _truncate_utf8(clipped[:-1], max(0, budget - 3))
            truncated = True
        if _json_bytes(clipped) <= budget:
            return clipped, truncated
        return _OMIT, True
    if isinstance(value, (list, tuple)):
        result: list = []
        truncated = len(value) > 100
        current_size = 2
        for item in list(value)[:100]:
            delimiter = 1 if result else 0
            child, child_truncated = _fit_json(
                item, budget - current_size - delimiter, depth=depth + 1,
            )
            if child is _OMIT:
                truncated = True
                break
            result.append(child)
            current_size = _json_bytes(result)
            truncated = truncated or child_truncated
        return result, truncated
    if isinstance(value, dict):
        result: dict[str, object] = {}
        items = list(value.items())
        truncated = len(items) > 200
        current_size = 2
        for raw_key, item in items[:200]:
            key = str(raw_key)
            delimiter = 1 if result else 0
            key_cost = _json_bytes(key) + 1
            child, child_truncated = _fit_json(
                item, budget - current_size - delimiter - key_cost,
                depth=depth + 1,
            )
            if child is _OMIT:
                truncated = True
                break
            result[key] = child
            current_size = _json_bytes(result)
            truncated = truncated or child_truncated
        return result, truncated
    return _fit_json(str(value), budget, depth=depth)


def _bounded_mapping(payload: dict, *, env_name: str, default: int) -> dict:
    maximum = _configured_limit(env_name, default, 2_048, 256_000)
    bounds = {"max_bytes": maximum, "truncated": False}
    result: dict = {"payload_bounds": bounds}
    truncated = False
    for key, value in payload.items():
        delimiter = 1 if len(result) else 0
        available = maximum - _json_bytes(result) - delimiter - _json_bytes(str(key)) - 1
        child, child_truncated = _fit_json(value, available, depth=1)
        if child is _OMIT:
            truncated = True
            break
        result[key] = child
        truncated = truncated or child_truncated
    bounds["truncated"] = truncated
    # ``true`` and ``false`` differ by one byte; keep the hard bound even at an
    # adversarially small configured value.
    if _json_bytes(result) > maximum:
        result = {"payload_bounds": {"max_bytes": maximum, "truncated": True}}
    return result


def _project_file(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"file '{path}' does not exist") from exc
    if not candidate.is_relative_to(PROJECT_ROOT) or not candidate.is_file():
        raise ValueError("path must name a file inside the project")
    return candidate


def _prepare_evidence(text: str, *, author: str, kind: str, metadata: dict) -> dict:
    """Normalize inline evidence or retain oversized exact bytes as an artifact.

    ``solve_state`` normalizes whitespace before persisting inline text and caps
    it at 4,000 characters.  Hashing the pre-normalized input made the recorded
    digest disagree with the stored inline evidence, while the generic artifact
    threshold could pass text that the state layer then rejected.  Mirror the
    state normalization here and leave artifact-backed evidence out of the
    inline field entirely.
    """
    raw = str(text or "")
    normalized = re.sub(r"\s+", " ", raw.strip())
    normalized_payload = normalized.encode("utf-8")
    if (len(normalized) <= _STATE_INLINE_CHARS
            and len(raw.encode("utf-8")) <= artifact_store.inline_threshold()):
        return {
            "evidence": normalized,
            "evidence_artifact_id": "",
            "evidence_sha256": (
                hashlib.sha256(normalized_payload).hexdigest() if normalized else ""
            ),
            "evidence_bytes": len(normalized_payload),
        }
    record = artifact_store.store_text(
        _artifacts_dir(), raw, kind=kind, producer=author,
        source="solve forum evidence", metadata=metadata,
    )
    return {
        "evidence": "",
        "evidence_artifact_id": record["artifact_id"],
        "evidence_sha256": record["sha256"],
        "evidence_bytes": record["bytes"],
    }


def _state() -> dict:
    return solve_store.load_state(_unity_dir())


def _bounded(value: str, limit: int) -> str:
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _agent_slug(author: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(author or "").strip()).strip("_")
    if not slug:
        raise ValueError("author must be a non-empty agent name")
    return slug


def _discussion_thread_id(thread_id: str) -> str:
    thread_id = str(thread_id or "").strip()
    if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", thread_id)
            or ".." in thread_id):
        raise ValueError("thread_id must be a simple 1-100 character identifier")
    return thread_id


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip()
            or f"git {' '.join(args)} failed with exit code {result.returncode}"
        )
    return result.stdout


def _redact_solution_candidate(candidate: dict) -> dict:
    """Expose review participation without leaking independent review judgments."""
    redacted = copy.deepcopy(candidate)
    reviews = redacted.get("reviews") or []
    redacted["review_count"] = len(reviews)
    redacted["reviews"] = [
        {
            "review_id": review.get("review_id"),
            "author": review.get("author"),
            "created_at": review.get("created_at"),
            "sealed": True,
        }
        for review in reviews[:_status_record_limit()]
    ]
    return redacted


def _sealed_review(review: dict | None) -> dict:
    """Return review receipt metadata without its judgment or supporting text."""
    review = review or {}
    return {
        "review_id": review.get("review_id"),
        "author": review.get("author"),
        "created_at": review.get("created_at"),
        "sealed": True,
    }


def _public_solution_review_result(result: dict) -> dict:
    """Strip prior independent reviews from a model-facing mutation response."""
    public = copy.deepcopy(result)
    candidate = public.get("candidate")
    if isinstance(candidate, dict):
        public["candidate"] = _redact_solution_candidate(candidate)
    if isinstance(public.get("review"), dict):
        public["review"] = _sealed_review(public["review"])
    if isinstance(public.get("event"), dict):
        public["event"] = _redact_status_event(public["event"])
    return public


def _redact_status_event(event: dict) -> dict:
    redacted = copy.deepcopy(event)
    if redacted.get("kind") == "solution_candidate_reviewed":
        redacted.pop("verdict", None)
        redacted["review_sealed"] = True
    return redacted


def _redact_discussion_post(post: dict) -> dict:
    """Seal review mirrors written by older solve-server revisions on read."""
    public = copy.deepcopy(post)
    if public.get("act") != "solve_solution_review":
        return public
    fields = public.get("fields") or {}
    candidate_id = fields.get("candidate_id") or "unknown"
    public["content"] = f"SEALED SOLUTION REVIEW {candidate_id}"
    public["fields"] = {
        "candidate_id": fields.get("candidate_id"),
        "review_id": fields.get("review_id"),
        "author": public.get("author"),
        "sealed": True,
    }
    return public


def _effective_control_stage(state: dict) -> str:
    """Return the live loop, including the resume target of a terminal run."""
    stage = str(state.get("stage") or "uninitialized")
    if stage in {"stopped", "exhausted", "failed"}:
        resume = str(state.get("resume_stage") or "")
        if resume in PROFILES:
            return resume
        solution = state.get("gates", {}).get("solution", {})
        formalization = state.get("gates", {}).get("formalization", {})
        if solution.get("status") == "review":
            return "solution_review"
        if solution.get("status") != "accepted":
            return "solving"
        if formalization.get("status") == "waiting":
            return "chunking"
        if formalization.get("status") == "open":
            return "formalizing"
        return "critic"
    return stage


def _current_state_window(state: dict) -> dict:
    """Bound historical records while retaining every item relevant to live gates."""
    solution = state.get("gates", {}).get("solution", {})
    formalization = state.get("gates", {}).get("formalization", {})
    solution_revision = solution.get("revision")
    formal_revision = formalization.get("revision")
    accepted_solution = solution.get("accepted_candidate_id")
    active_source_fix_id = solution.get("source_fix_id")
    active_source_fix = state.get("source_fixes", {}).get(active_source_fix_id, {})
    repair_candidate = active_source_fix.get("candidate_id")

    record_counts: dict[str, dict[str, int]] = {}

    def window(
        name: str,
        current,
        recent: int = 20,
        pinned_keys: set[str] | None = None,
    ) -> dict:
        records = state.get(name, {})
        maximum = _status_record_limit()
        pinned_keys = pinned_keys or set()
        live_items = [(key, value) for key, value in records.items() if current(value)]
        live_items.sort(
            key=lambda pair: (
                pair[0] in pinned_keys,
                pair[1].get("updated_at", pair[1].get("created_at", 0)),
            ),
            reverse=True,
        )
        selected_live = live_items[:maximum]
        live_keys = {key for key, _ in live_items}
        history = sorted(
            ((key, value) for key, value in records.items() if key not in live_keys),
            key=lambda pair: pair[1].get("updated_at", pair[1].get("created_at", 0)),
            reverse=True,
        )[:min(recent, max(0, maximum - len(selected_live)))]
        selected = [*selected_live, *history]
        record_counts[name] = {
            "total": len(records),
            "live": len(live_items),
            "returned": len(selected),
        }
        return dict(selected)

    result = {
        key: state.get(key)
        for key in (
            "schema_version", "revision", "run_id", "stage", "outcome",
            "resume_stage", "initialized_at", "updated_at", "problem", "gates",
        )
    }
    result["active_stage"] = _effective_control_stage(state)
    result["subgoals"] = window(
        "subgoals",
        lambda item: item.get("status") in {"open", "blocked"} and (
            (item.get("stage") == "solving"
             and item.get("gate_revision") == solution_revision)
            or (item.get("stage") == "formalizing"
                and item.get("gate_revision") == formal_revision)
        ),
    )
    result["strategies"] = window(
        "strategies",
        lambda item: item.get("status") in {"registered", "claimed", "paused"} and ((
            item.get("stage") == "solving" and item.get("gate_revision") == solution_revision
        ) or (
            item.get("stage") == "formalizing" and item.get("gate_revision") == formal_revision
        )),
    )
    result["findings"] = window(
        "findings",
        lambda item: item.get("status") == "active" and (
            (item.get("stage") == "solving" and item.get("gate_revision") == solution_revision)
            or (item.get("stage") == "formalizing" and item.get("gate_revision") == formal_revision)
        ),
    )
    result["solution_candidates"] = window(
        "solution_candidates",
        lambda item: item.get("gate_revision") == solution_revision
        or item.get("candidate_id") in {accepted_solution, repair_candidate},
        pinned_keys={item for item in {accepted_solution, repair_candidate} if item},
    )
    result["solution_candidates"] = {
        key: _redact_solution_candidate(item)
        for key, item in result["solution_candidates"].items()
    }
    result["formal_tasks"] = window(
        "formal_tasks", lambda item: item.get("gate_revision") == formal_revision,
    )
    result["formal_candidates"] = window(
        "formal_candidates", lambda item: item.get("gate_revision") == formal_revision,
        pinned_keys=set(formalization.get("accepted_candidate_ids") or []),
    )
    visible_candidate_ids = {
        *result["solution_candidates"].keys(), *result["formal_candidates"].keys(),
    }
    result["objections"] = window(
        "objections",
        lambda item: item.get("status") == "open"
        and item.get("target_id") in visible_candidate_ids,
    )
    result["obstacles"] = window(
        "obstacles", lambda item: item.get("status") == "open" and (
            (item.get("stage") == "solving"
             and item.get("gate_revision") == solution_revision)
            or (item.get("stage") == "formalizing"
                and item.get("gate_revision") == formal_revision)
        ), recent=10,
    )
    relevant_solution_ids = set(result["solution_candidates"])
    result["source_fixes"] = window(
        "source_fixes", lambda item: (
            item.get("status") in {"proposed", "active", "submitted"}
            and (
                (active_source_fix_id and
                 item.get("source_fix_id", item.get("fix_id")) == active_source_fix_id)
                or
                item.get("candidate_id") in relevant_solution_ids
                or item.get("replacement_candidate_id") in relevant_solution_ids
            )
        ), recent=10,
        pinned_keys={active_source_fix_id} if active_source_fix_id else set(),
    )
    maximum = _status_record_limit()
    result["formalization_verdicts"] = state.get("formalization_verdicts", [])[-maximum:]
    result["events"] = [
        _redact_status_event(event) for event in state.get("events", [])[-maximum:]
    ]
    result["window"] = {
        "kind": "current_plus_recent",
        "event_count": len(result["events"]),
        "total_event_count": len(state.get("events", [])),
        "record_counts": record_counts,
    }
    return result


def solve_status(subgoal_id: str = "") -> dict:
    """Return bounded authoritative state, optionally narrowed to one subgoal."""
    state = _state()
    if not subgoal_id:
        return _bounded_mapping(
            _current_state_window(state),
            env_name="UNITY_SOLVE_STATUS_BYTES", default=_DEFAULT_STATUS_BYTES,
        )
    subgoal = state.get("subgoals", {}).get(subgoal_id)
    if subgoal is None:
        raise ValueError(f"unknown subgoal '{subgoal_id}'")
    stage = subgoal.get("stage", "solving")
    revision = subgoal.get("gate_revision")
    maximum = _status_record_limit()

    def recent(items: list[dict]) -> list[dict]:
        return sorted(
            items,
            key=lambda item: item.get("updated_at", item.get("created_at", 0)),
            reverse=True,
        )[:maximum]

    payload = {
        "schema_version": state.get("schema_version"),
        "revision": state.get("revision", 0),
        "run_id": state.get("run_id"),
        "stage": state.get("stage"),
        "active_stage": _effective_control_stage(state),
        "outcome": state.get("outcome"),
        "gates": state.get("gates", {}),
        "subgoal": subgoal,
        "strategies": recent([
            item for item in state.get("strategies", {}).values()
            if item.get("stage") == stage and item.get("gate_revision") == revision
            and item.get("subgoal_id") in ("", None, subgoal_id)
        ]),
        "findings": recent([
            item for item in state.get("findings", {}).values()
            if item.get("stage") == stage and item.get("gate_revision") == revision
            and item.get("status") == "active"
            and item.get("subgoal_id") in ("", None, subgoal_id)
        ]),
        "obstacles": recent([
            item for item in state.get("obstacles", {}).values()
            if item.get("stage") == stage and item.get("gate_revision") == revision
            and item.get("status") == "open" and item.get("subgoal_id") == subgoal_id
        ]),
        "solution_candidates": [
            _redact_solution_candidate(item) for item in recent([
                item for item in state.get("solution_candidates", {}).values()
                if item.get("gate_revision") == state.get("gates", {}).get(
                    "solution", {},
                ).get("revision")
                or item.get("candidate_id") == state.get("gates", {}).get(
                    "solution", {},
                ).get("accepted_candidate_id")
            ])
        ],
        "events": [
            _redact_status_event(event)
            for event in state.get("events", [])
            if event.get("subgoal_id") in (None, "", subgoal_id)
        ][-maximum:],
    }
    return _bounded_mapping(
        payload, env_name="UNITY_SOLVE_STATUS_BYTES", default=_DEFAULT_STATUS_BYTES,
    )


def solve_brief(author: str) -> str:
    """Return a bounded, stage-aware digest of current solve state for one agent."""
    author = _require_bound_author(author)
    state = _state()
    gates = state.get("gates", {})
    solution_gate = gates.get("solution", {})
    formal_gate = gates.get("formalization", {})
    solution_revision = solution_gate.get("revision", 0)
    formal_revision = formal_gate.get("revision", 0)
    accepted_solution_id = solution_gate.get("accepted_candidate_id")
    accepted_formal_ids = set(formal_gate.get("accepted_candidate_ids") or [])
    active_source_fix_id = solution_gate.get("source_fix_id")
    active_source_fix = state.get("source_fixes", {}).get(active_source_fix_id, {})
    repair_candidate_id = active_source_fix.get("candidate_id")
    stage = state.get("stage", "unknown")
    control_stage = _effective_control_stage(state)
    if control_stage in {"solving", "solution_review"}:
        active_stage = "solving"
        active_revision = solution_revision
    elif control_stage in {"chunking", "formalizing", "critic"}:
        active_stage = "formalizing"
        active_revision = formal_revision
    else:
        active_stage = ""
        active_revision = -1

    # Briefs are live control-plane views, not historical transcripts.  Keep the
    # exact accepted records visible, but do not make workers reason over stale
    # candidates or previous gate revisions unless they explicitly query status.
    candidates = [
        item for item in state.get("solution_candidates", {}).values()
        if item.get("gate_revision") == solution_revision
        or item.get("candidate_id") in {accepted_solution_id, repair_candidate_id}
    ]
    formal_candidates = [
        item for item in state.get("formal_candidates", {}).values()
        if item.get("gate_revision") == formal_revision
        or item.get("candidate_id") in accepted_formal_ids
    ]
    relevant_candidate_ids = {
        *(item.get("candidate_id") for item in candidates),
        *(item.get("candidate_id") for item in formal_candidates),
    }
    relevant_candidate_ids.discard(None)
    strategies = [
        item for item in state.get("strategies", {}).values()
        if item.get("stage") == active_stage
        and item.get("gate_revision") == active_revision
        and item.get("status") in {"registered", "claimed", "paused"}
    ]
    findings = [
        item for item in state.get("findings", {}).values()
        if item.get("stage") == active_stage
        and item.get("gate_revision") == active_revision
        and item.get("status") == "active"
    ]
    objections = [
        item for item in state.get("objections", {}).values()
        if item.get("status") == "open"
        and item.get("target_id") in relevant_candidate_ids
    ]

    problem = state.get("problem", {})
    accepted_formal = ",".join(sorted(accepted_formal_ids)) or "none"
    lines = [
        "AUTHORITATIVE SOLVE IDENTITY (copy exact IDs and full hashes into reviews):",
        f"Run: id={state.get('run_id') or 'none'} state_revision={state.get('revision', 0)} "
        f"stage={stage} active_loop={control_stage} "
        f"outcome={state.get('outcome') or 'running'}.",
        f"Problem: artifact_id={problem.get('artifact_id') or 'none'} "
        f"sha256={problem.get('sha256') or 'none'} "
        f"source={problem.get('source_path') or 'none'}.",
        f"Solution gate: revision={solution_revision} "
        f"status={solution_gate.get('status', 'unresolved')} "
        f"accepted_candidate_id={accepted_solution_id or 'none'} "
        f"artifact_id={solution_gate.get('artifact_id') or 'none'} "
        f"sha256={solution_gate.get('sha256') or 'none'}.",
        f"Formalization gate: revision={formal_revision} "
        f"status={formal_gate.get('status', 'not_started')} "
        f"solution_candidate_id={formal_gate.get('solution_candidate_id') or 'none'} "
        f"accepted_candidate_ids=[{accepted_formal}] "
        f"integrated_main_sha={formal_gate.get('integrated_main_sha') or 'none'}.",
    ]

    candidates.sort(
        key=lambda item: (
            item.get("candidate_id") == accepted_solution_id,
            item.get("updated_at", item.get("created_at", 0)),
        ),
        reverse=True,
    )
    if candidates:
        lines.append("Current solution candidates (reviews bind to exact artifacts):")
        for item in candidates[:4]:
            lines.append(
                f"  - {item.get('candidate_id')} {item.get('status', 'submitted')} "
                f"by {item.get('author', '?')} artifact={item.get('artifact_id', '?')} "
                f"sha256={item.get('sha256') or 'none'}"
            )

    formal_candidates.sort(
        key=lambda item: (
            item.get("candidate_id") in accepted_formal_ids,
            item.get("updated_at", item.get("created_at", 0)),
        ),
        reverse=True,
    )
    if formal_candidates:
        lines.append("Current formalization candidates (verification binds to exact commits):")
        for item in formal_candidates[:4]:
            verification = (item.get("verification") or {}).get("status", "pending")
            lines.append(
                f"  - {item.get('candidate_id')} {item.get('status', 'submitted')} "
                f"by {item.get('author', '?')} commit={item.get('commit_sha') or 'none'} "
                f"source_sha256={item.get('source_hash') or 'none'} verification={verification}"
            )

    verdicts = state.get("formalization_verdicts") or []
    if verdicts:
        latest_verdict = verdicts[-1]
        reopened_ids = list(latest_verdict.get("reopen_task_ids") or [])
        replacement_ids = [
            item.get("task_id") for item in state.get("formal_tasks", {}).values()
            if item.get("gate_revision") == formal_revision
            and item.get("supersedes_task_id") in reopened_ids
        ]
        lines.append(
            "Latest critic decision: "
            f"id={latest_verdict.get('verdict_id') or 'none'} "
            f"verdict={latest_verdict.get('verdict') or 'none'} "
            f"reviewed_main_sha={latest_verdict.get('reviewed_main_sha') or 'none'}."
        )
        lines.append(
            "  Rationale: " + _bounded(latest_verdict.get("rationale"), 400)
        )
        if reopened_ids or replacement_ids:
            lines.append(
                "  Task linkage: reviewed_tasks=["
                + ",".join(reopened_ids)
                + "] current_replacements=["
                + ",".join(item for item in replacement_ids if item)
                + "]."
            )

    if objections:
        lines.append("Actionable objections:")
        for item in objections[:5]:
            lines.append(
                f"  - {item.get('objection_id')} on {item.get('target_id')}: "
                f"{_bounded(item.get('reason') or item.get('rationale'), 180)}"
            )

    owned = [item for item in strategies
             if item.get("owner") == author and item.get("status") in {"claimed", "paused"}]
    formal_owned = [item for item in state.get("formal_tasks", {}).values()
                    if item.get("gate_revision") == formal_revision
                    and item.get("owner") == author and item.get("status") == "claimed"]
    if owned or formal_owned:
        lines.append("Your current work:")
        for item in owned[:3]:
            lines.append(f"  - strategy {item.get('strategy_id')}: {_bounded(item.get('description'), 180)}")
        for item in formal_owned[:3]:
            lines.append(f"  - formal task {item.get('task_id')}: {_bounded(item.get('description'), 180)}")

    formal_tasks = [item for item in state.get("formal_tasks", {}).values()
                    if active_stage == "formalizing"
                    and item.get("gate_revision") == formal_revision
                    and item.get("status") in {"pending", "claimed", "failed"}]
    if formal_tasks:
        lines.append("Active formalization tasks:")
        for item in formal_tasks[:8]:
            lines.append(
                f"  - {item.get('task_id')} {item.get('status')} owner={item.get('owner') or 'none'} "
                f"kind={item.get('kind')}: {_bounded(item.get('description'), 150)}"
            )

    subgoals = [item for item in state.get("subgoals", {}).values()
                if item.get("stage") == active_stage
                and item.get("gate_revision") == active_revision
                and item.get("status") not in {"solved", "cancelled", "superseded"}]
    if subgoals:
        lines.append("Open mathematical subgoals:")
        for item in subgoals[:8]:
            lines.append(
                f"  - {item.get('subgoal_id')} {item.get('status', 'open')}: "
                f"{_bounded(item.get('title') or item.get('description'), 160)}"
            )

    if strategies:
        lines.append("Active strategies:")
        for item in strategies[:8]:
            lines.append(
                f"  - {item.get('strategy_id')} family={item.get('strategy_family') or 'unkeyed'} "
                f"status={item.get('status')} owner={item.get('owner') or 'none'}: "
                f"{_bounded(item.get('description'), 150)}"
            )

    findings.sort(key=lambda item: (item.get("confidence", 0), item.get("updated_at", 0)), reverse=True)
    if findings:
        lines.append("High-value live findings:")
        for item in findings[:6]:
            lines.append(
                f"  - {item.get('finding_id')} {item.get('kind')} "
                f"confidence={item.get('confidence', 0)}/100: "
                f"{_bounded(item.get('title'), 100)} — {_bounded(item.get('content'), 160)}"
            )

    obstacles = [item for item in state.get("obstacles", {}).values()
                 if item.get("stage") == active_stage
                 and item.get("gate_revision") == active_revision
                 and item.get("status") == "open"]
    if obstacles:
        lines.append("Open obstacles:")
        for item in obstacles[:5]:
            lines.append(
                f"  - {item.get('obstacle_id')} [{item.get('subgoal_id') or 'global'}]: "
                f"{_bounded(item.get('goal_state') or item.get('hypothesis'), 180)}"
            )

    candidate_scope = {
        *(item.get("candidate_id") for item in candidates),
        accepted_solution_id,
    }
    candidate_scope.discard(None)
    source_fixes = [
        item for item in state.get("source_fixes", {}).values()
        if (active_source_fix_id and
            item.get("source_fix_id", item.get("fix_id")) == active_source_fix_id)
        or item.get("candidate_id") in candidate_scope
        or item.get("replacement_candidate_id") in candidate_scope
    ]
    if source_fixes:
        lines.append("Source-fix requests:")
        for item in source_fixes[-4:]:
            lines.append(
                f"  - {item.get('fix_id')}: "
                f"candidate={item.get('candidate_id') or 'none'} "
                f"trigger={item.get('trigger_verdict_id') or 'none'} "
                f"{_bounded(item.get('suggested_fix') or item.get('reason') or item.get('summary'), 220)}"
            )

    try:
        questions = [item for item in legacy_forum._acts("qa", "question")
                     if (item.get("fields") or {}).get("status") == "open"
                     and (item.get("fields") or {}).get("to", "") in {"", author}]
    except (OSError, ValueError):
        questions = []
    if questions:
        lines.append("Open questions relevant to you:")
        for item in questions[-5:]:
            lines.append(
                f"  - {item.get('post_id')} from {item.get('author', '?')}: "
                f"{_bounded(item.get('content'), 180)}"
            )

    try:
        configured_maximum = int(os.getenv("UNITY_SOLVE_BRIEF_CHARS", "12000") or "12000")
    except ValueError:
        configured_maximum = 12_000
    maximum = max(2_000, min(configured_maximum, 32_000))
    text = "\n".join(lines)
    return text if len(text) <= maximum else text[:maximum].rstrip() + "\n...[brief truncated]"


def forum_post(
    thread_id: str,
    author: str,
    content: str,
    reply_to: list[str] | None = None,
) -> dict:
    """Post free-form discussion to the legacy Forum; this never reserves solve work."""
    author = _require_bound_author(author)
    thread_id = _discussion_thread_id(thread_id)
    content = _validate_forum_content(content)
    reply_to = reply_to or []
    if len(reply_to) > 50:
        raise ValueError("reply_to may contain at most 50 post ids")
    invalid = [post_id for post_id in reply_to if not legacy_forum._POST_ID_RE.match(post_id)]
    if invalid:
        raise ValueError(f"reply_to contains invalid post ids: {invalid}")
    with legacy_forum._thread_lock(thread_id):
        legacy_forum._ensure_thread(thread_id, thread_id.replace("-", " ").title())
        return legacy_forum._forum_post_locked(thread_id, author, content, reply_to)


def forum_read(thread_id: str, sort: str = "hot", limit: int = 20) -> dict:
    """Read a bounded discussion page only when the compact solve brief is insufficient."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    limit = max(1, min(limit, 50))
    thread_id = _discussion_thread_id(thread_id)
    legacy_forum._ensure_thread(thread_id, thread_id.replace("-", " ").title())
    raw = legacy_forum.forum_read(thread_id, sort)
    posts = [
        _redact_discussion_post(post) for post in raw.get("posts", [])[:limit]
    ]
    payload = {
        "thread_id": raw.get("thread_id", thread_id),
        "title": raw.get("title", ""),
        "description": raw.get("description", ""),
        "created_at": raw.get("created_at"),
        "post_count": raw.get("post_count", len(posts)),
        "sort": raw.get("sort", sort),
        "limit": limit,
        "available_count": len(posts),
        "returned_count": len(posts),
        "active_dimensions": raw.get("active_dimensions", []),
        "posts": posts,
    }
    result = _bounded_mapping(
        payload,
        env_name="UNITY_SOLVE_FORUM_READ_BYTES",
        default=_DEFAULT_FORUM_READ_BYTES,
    )
    result["returned_count"] = len(result.get("posts", []))
    return result


def artifact_info(artifact_id: str) -> dict:
    """Return immutable artifact metadata without loading its content into context."""
    return artifact_store.artifact_info(_artifacts_dir(), artifact_id)


def artifact_read(
    artifact_id: str,
    offset: int = 0,
    limit: int = artifact_store.DEFAULT_READ_LIMIT,
) -> dict:
    """Read one bounded page from an immutable solve artifact."""
    return artifact_store.read_artifact(_artifacts_dir(), artifact_id, offset=offset, limit=limit)


COMMON_TOOLS: tuple[Callable, ...] = (
    solve_brief, solve_status, forum_post, forum_read, artifact_info, artifact_read,
)


def _mirror(author: str, act: str, content: str, fields: dict, subgoal_id: str = "") -> None:
    """Best-effort conversational mirror; authoritative mutation has already happened."""
    try:
        thread = f"solve-{subgoal_id}" if subgoal_id else "solve-global"
        legacy_forum._typed_post(
            thread,
            f"Solve: {subgoal_id}" if subgoal_id else "Solve Control Plane",
            author,
            f"solve_{act}",
            fields,
            content,
        )
    except Exception:
        # A display mirror must never roll back or falsify committed solve state.
        pass


def create_subgoal(
    author: str,
    title: str,
    description: str,
    parent_id: str = "",
    dependencies: list[str] | None = None,
) -> dict:
    """Create a mathematical subgoal dynamically as the solution attack develops."""
    author = _require_bound_author(author)
    result = solve_store.create_subgoal(
        _unity_dir(), author, title, description,
        parent_id=parent_id, dependencies=dependencies or [],
    )
    item = result.get("subgoal", {})
    _mirror(author, "subgoal", f"SUBGOAL {item.get('subgoal_id')}: {title}", item,
            item.get("subgoal_id", ""))
    return result


def register_strategy(
    author: str,
    description: str,
    subgoal_id: str = "",
    strategy_family: str = "",
) -> dict:
    """Register a distinct attack without claiming it; structured duplicates collide."""
    author = _require_bound_author(author)
    result = solve_store.register_strategy(
        _unity_dir(), author, description,
        subgoal_id=subgoal_id, strategy_family=strategy_family,
    )
    if result.get("status") == "registered":
        item = result.get("strategy", {})
        _mirror(author, "strategy", f"STRATEGY {item.get('strategy_id')}: {description}", item,
                item.get("subgoal_id", ""))
    return result


def claim_strategy(strategy_id: str, author: str) -> dict:
    """Atomically reserve one registered solve strategy."""
    author = _require_bound_author(author)
    result = solve_store.claim_strategy(_unity_dir(), strategy_id, author)
    if result.get("status") in {"claimed", "already_owned"}:
        item = result.get("strategy", {})
        _mirror(author, "strategy_claim", f"CLAIM STRATEGY {strategy_id}", item,
                item.get("subgoal_id", ""))
    return result


def assist_strategy(strategy_id: str, author: str, contribution: str = "") -> dict:
    """Join an owned strategy for an explicit, non-duplicative supporting contribution."""
    author = _require_bound_author(author)
    result = solve_store.assist_strategy(
        _unity_dir(), strategy_id, author, contribution=contribution,
    )
    if result.get("status") in {"assisting", "already_assisting"}:
        item = result.get("strategy", {})
        _mirror(author, "strategy_assist", f"ASSIST STRATEGY {strategy_id}: {contribution}",
                {"strategy_id": strategy_id, "contribution": contribution},
                item.get("subgoal_id", ""))
    return result


def unclaim_strategy(strategy_id: str, author: str, reason: str = "") -> dict:
    """Release an owned but still viable strategy for another solver."""
    author = _require_bound_author(author)
    result = solve_store.release_strategy(_unity_dir(), strategy_id, author, reason=reason)
    item = result.get("strategy", {})
    _mirror(author, "strategy_release", f"RELEASE STRATEGY {strategy_id}: {reason}",
            {"strategy_id": strategy_id, "reason": reason}, item.get("subgoal_id", ""))
    return result


def mark_strategy_incorrect(
    strategy_id: str,
    author: str,
    reason: str,
    evidence: str = "",
) -> dict:
    """Close a strategy whose failure is established, retaining exact supporting evidence."""
    author = _require_bound_author(author)
    prepared = _prepare_evidence(
        evidence, author=author, kind="solve_strategy_failure",
        metadata={"strategy_id": strategy_id},
    )
    result = solve_store.finish_strategy(
        _unity_dir(), strategy_id, author, outcome="incorrect", reason=reason, **prepared,
    )
    item = result.get("strategy", {})
    _mirror(author, "strategy_incorrect", f"INCORRECT STRATEGY {strategy_id}: {reason}",
            {"strategy_id": strategy_id, "reason": reason, **prepared},
            item.get("subgoal_id", ""))
    return result


def publish_finding(
    author: str,
    kind: str,
    title: str,
    content: str,
    confidence: int,
    subgoal_id: str = "",
    strategy_id: str = "",
    evidence: str = "",
) -> dict:
    """Publish bounded live mathematical or formalization knowledge to solve state."""
    author = _require_bound_author(author)
    prepared = _prepare_evidence(
        evidence, author=author, kind="solve_finding_evidence",
        metadata={"subgoal_id": subgoal_id, "strategy_id": strategy_id, "title": title},
    )
    result = solve_store.publish_finding(
        _unity_dir(), author, kind, title, content, confidence,
        subgoal_id=subgoal_id, strategy_id=strategy_id, **prepared,
    )
    if result.get("status") == "published":
        item = result.get("finding", {})
        _mirror(author, "finding", f"FINDING[{kind}] {title}: {_bounded(content, 400)}",
                item, item.get("subgoal_id", ""))
    return result


def supersede_finding(
    finding_id: str,
    author: str,
    reason: str,
    replacement_id: str = "",
) -> dict:
    """Retire a false, stale, or replaced live finding without erasing history."""
    author = _require_bound_author(author)
    result = solve_store.supersede_finding(
        _unity_dir(), finding_id, author, reason, replacement_id=replacement_id,
    )
    item = result.get("finding", {})
    _mirror(author, "finding_superseded", f"SUPERSEDE FINDING {finding_id}: {reason}",
            {"finding_id": finding_id, "reason": reason, "replacement_id": replacement_id},
            item.get("subgoal_id", ""))
    return result


def report_obstacle(
    author: str,
    goal_state: str,
    subgoal_id: str = "",
    strategy_id: str = "",
    tried: list[str] | None = None,
    hypothesis: str = "",
    evidence: str = "",
) -> dict:
    """Record an actionable blocker in solve state and mirror it to Forum discussion."""
    author = _require_bound_author(author)
    prepared = _prepare_evidence(
        evidence, author=author, kind="solve_obstacle_evidence",
        metadata={"subgoal_id": subgoal_id, "strategy_id": strategy_id},
    )
    result = solve_store.report_obstacle(
        _unity_dir(), author, goal_state,
        subgoal_id=subgoal_id, strategy_id=strategy_id, tried=tried or [],
        hypothesis=hypothesis, evidence=prepared["evidence"],
        artifact_id=prepared["evidence_artifact_id"],
        artifact_sha256=prepared["evidence_sha256"],
    )
    try:
        legacy_forum.forum_obstacle(
            subgoal_id or "solve-global", author, goal_state, tried or [], hypothesis,
        )
    except Exception:
        pass
    return result


def ask_question(author: str, body: str, to: str = "", subgoal_id: str = "") -> dict:
    """Ask a teammate or the whole team a targeted solve question."""
    author = _require_bound_author(author)
    rendered = f"QUESTION{f' @{to}' if to else ''}{f' [{subgoal_id}]' if subgoal_id else ''}: {body}"
    _validate_forum_content(rendered, label="question")
    return legacy_forum.forum_question(author, body, to, subgoal_id)


def answer_question(question_id: str, author: str, body: str) -> dict:
    """Answer and close a targeted solve question by its Forum post id."""
    author = _require_bound_author(author)
    _validate_forum_content(f"ANSWER: {body}", label="answer")
    return legacy_forum.forum_answer(question_id, author, body)


def emit_solution_candidate(
    author: str,
    path: str = "",
    strategy_id: str = "",
    notes: str = "",
    supersedes: str = "",
) -> dict:
    """Snapshot and submit one exact natural-language solution for independent review."""
    author = _require_bound_author(author)
    source_path = path or f".unity/solve/drafts/{_agent_slug(author)}/PROOF.tex"
    target = _project_file(source_path)
    content = target.read_text(errors="replace")
    record = artifact_store.store_text(
        _artifacts_dir(), content, kind="solution_candidate", producer=author,
        source=str(target.relative_to(PROJECT_ROOT)),
        metadata={"strategy_id": strategy_id, "supersedes": supersedes},
    )
    result = solve_store.submit_solution_candidate(
        _unity_dir(), author, record["artifact_id"], record["sha256"],
        source_path=str(target.relative_to(PROJECT_ROOT)), strategy_id=strategy_id,
        notes=notes, supersedes=supersedes, artifact_bytes=record["bytes"],
    )
    item = result.get("candidate", {})
    public_item = _redact_solution_candidate(item)
    _mirror(author, "solution_candidate",
            f"SOLUTION CANDIDATE {item.get('candidate_id')} artifact={record['artifact_id']} "
            f"sha256={record['sha256']}", public_item)
    return {**_public_solution_review_result(result), "artifact": record}


def review_solution_candidate(
    candidate_id: str,
    author: str,
    verdict: str,
    review: str,
    evidence: str = "",
) -> dict:
    """Record an independent approve/object review of one immutable solution candidate."""
    author = _require_bound_author(author)
    if verdict not in {"approve", "object"}:
        raise ValueError("verdict must be approve or object")
    prepared = _prepare_evidence(
        evidence, author=author, kind="solution_review_evidence",
        metadata={"candidate_id": candidate_id, "verdict": verdict},
    )
    result = solve_store.review_solution_candidate(
        _unity_dir(), candidate_id, author, verdict, review, **prepared,
    )
    review_record = result.get("review") or {}
    _mirror(
        author,
        "solution_review_sealed",
        f"SEALED SOLUTION REVIEW {candidate_id}",
        {
            "candidate_id": candidate_id,
            "review_id": review_record.get("review_id"),
            "author": author,
            "sealed": True,
        },
    )
    return _public_solution_review_result(result)


def resolve_solution_objection(
    candidate_id: str,
    objection_id: str,
    author: str,
    resolution: str,
) -> dict:
    """Resolve the caller's objection after checking that its exact concern is addressed."""
    author = _require_bound_author(author)
    result = solve_store.resolve_objection(
        _unity_dir(), objection_id, author, resolution=resolution,
        target_id=candidate_id,
    )
    _mirror(author, "solution_objection_resolved",
            f"RESOLVE {objection_id} ON {candidate_id}: {_bounded(resolution, 400)}",
            {"candidate_id": candidate_id, "objection_id": objection_id,
             "resolution": resolution})
    return _public_solution_review_result(result)


def emit_formalization_candidate(
    author: str,
    commit_sha: str,
    task_id: str = "",
    strategy_id: str = "",
    declarations: list[str] | None = None,
    notes: str = "",
    supersedes: str = "",
) -> dict:
    """Submit an exact committed Lean revision; self-reported build success is not accepted."""
    author = _require_bound_author(author)
    from .. import worktree

    resolved = worktree.verify_candidate_commit(PROJECT_ROOT, author, commit_sha)
    main_sha = worktree.main_commit(PROJECT_ROOT)
    base_sha = _git("merge-base", main_sha, resolved).strip()
    diff = _git("diff", "--binary", "--no-ext-diff", base_sha, resolved)
    if not diff:
        raise ValueError("candidate commit contains no source diff relative to its merge base")
    diff_sha256 = hashlib.sha256(diff.encode()).hexdigest()
    artifact = artifact_store.store_text(
        _artifacts_dir(), diff, kind="solve_formalization_diff", producer=author,
        source=f"git diff {base_sha}..{resolved}",
        metadata={
            "commit_sha": resolved,
            "base_main_sha": base_sha,
            "diff_sha256": diff_sha256,
            "task_id": task_id,
            "strategy_id": strategy_id,
        },
    )
    result = solve_store.submit_formal_candidate(
        _unity_dir(), author, resolved,
        task_id=task_id, strategy_id=strategy_id, source_hash=diff_sha256,
        artifact_id=artifact["artifact_id"], declarations=declarations or [],
        notes=notes, supersedes=supersedes, base_main_sha=base_sha,
        diff_sha256=diff_sha256,
    )
    item = result.get("candidate", {})
    _mirror(author, "formalization_candidate",
            f"FORMALIZATION CANDIDATE {item.get('candidate_id')} commit={resolved}", item)
    return {**result, "artifact": artifact}


def claim_formal_task(task_id: str, author: str) -> dict:
    """Atomically reserve one ready formalization task."""
    author = _require_bound_author(author)
    result = solve_store.claim_formal_task(_unity_dir(), task_id, author)
    if result.get("status") == "claimed":
        _mirror(author, "formal_task_claim", f"CLAIM FORMAL TASK {task_id}",
                result.get("task", {"task_id": task_id}))
    return result


def release_formal_task(task_id: str, author: str, reason: str = "") -> dict:
    """Release an owned formalization task so another worker may claim it."""
    author = _require_bound_author(author)
    result = solve_store.release_formal_task(_unity_dir(), task_id, author, reason=reason)
    _mirror(author, "formal_task_release", f"RELEASE FORMAL TASK {task_id}: {reason}",
            result.get("task", {"task_id": task_id, "reason": reason}))
    return result


def sync_from_main(author: str, reason: str = "") -> dict:
    """Release obsolete solve work and force the caller's worktree to current main."""
    author = _require_bound_author(author)
    from .. import worktree

    released_strategies: list[str] = []
    released_tasks: list[str] = []
    state = _state()
    for item in state.get("strategies", {}).values():
        if item.get("owner") != author or item.get("status") not in {"claimed", "paused"}:
            continue
        try:
            solve_store.release_strategy(
                _unity_dir(), item["strategy_id"], author,
                reason=reason or "synchronizing from main",
            )
            released_strategies.append(item["strategy_id"])
        except (ValueError, KeyError):
            pass
    for item in state.get("formal_tasks", {}).values():
        if item.get("owner") != author or item.get("status") not in {"claimed", "active"}:
            continue
        try:
            solve_store.release_formal_task(
                _unity_dir(), item["task_id"], author,
                reason=reason or "synchronizing from main",
            )
            released_tasks.append(item["task_id"])
        except (ValueError, KeyError):
            pass
    synced = worktree.force_sync_from_main(PROJECT_ROOT, author)
    result = {
        "status": "synced",
        "author": author,
        "main_sha": synced["main_sha"],
        "worktree": synced["worktree"],
        "released_strategies": released_strategies,
        "released_tasks": released_tasks,
    }
    _mirror(author, "worktree_sync",
            f"SYNC FROM MAIN {synced['main_sha']}: {reason}", result)
    return result


def propose_source_fix(
    author: str,
    summary: str,
    evidence: str,
    path: str = "",
    candidate_id: str = "",
    subgoal_id: str = "",
) -> dict:
    """Snapshot a corrected paper, reopen its source gate, and submit the exact new revision."""
    author = _require_bound_author(author)
    state = _state()
    accepted_id = state.get("gates", {}).get("solution", {}).get("accepted_candidate_id")
    if not accepted_id:
        raise ValueError("there is no accepted solution candidate to revise")
    if candidate_id and candidate_id != accepted_id:
        raise ValueError("candidate_id must be the currently accepted solution candidate")

    source_path = path or f".unity/solve/drafts/{_agent_slug(author)}/PROOF.tex"
    target = _project_file(source_path)
    content = target.read_text(errors="replace")
    candidate_artifact = artifact_store.store_text(
        _artifacts_dir(), content, kind="solution_candidate", producer=author,
        source=str(target.relative_to(PROJECT_ROOT)),
        metadata={
            "source_fix_for": accepted_id,
            "summary": summary,
            "subgoal_id": subgoal_id,
        },
    )
    accepted = state.get("solution_candidates", {}).get(accepted_id, {})
    if candidate_artifact["sha256"] == accepted.get("sha256"):
        raise ValueError("the proposed source fix has the same bytes as the accepted solution")

    prepared = _prepare_evidence(
        evidence, author=author, kind="solution_source_fix_evidence",
        metadata={"candidate_id": accepted_id, "subgoal_id": subgoal_id},
    )
    submitted = solve_store.replace_solution_candidate(
        _unity_dir(), author, accepted_id,
        candidate_artifact["artifact_id"], candidate_artifact["sha256"],
        reason=summary, notes=summary,
        artifact_bytes=candidate_artifact["bytes"],
        source_path=str(target.relative_to(PROJECT_ROOT)),
        subgoal_id=subgoal_id, **prepared,
    )
    _mirror(author, "source_fix", f"SOURCE FIX PROPOSED: {_bounded(summary, 400)}",
            {
                "source_fix": submitted.get("source_fix", {}),
                "candidate": submitted.get("candidate", {}),
                "artifact_id": candidate_artifact["artifact_id"],
                "sha256": candidate_artifact["sha256"],
            }, subgoal_id)
    return {
        "status": submitted.get("status", "submitted"),
        "source_fix": submitted.get("source_fix"),
        "candidate": submitted.get("candidate"),
        "artifact": candidate_artifact,
        "event": submitted.get("event"),
    }


def reopen_solving(
    author: str,
    reason: str,
    evidence: str = "",
    candidate_id: str = "",
) -> dict:
    """Explicitly reopen mathematical solving for an evidenced defect in the accepted solution."""
    author = _require_bound_author(author)
    prepared = _prepare_evidence(
        evidence, author=author, kind="solution_reopen_evidence",
        metadata={"candidate_id": candidate_id},
    )
    result = solve_store.request_solution_revision(
        _unity_dir(), author, reason, candidate_id=candidate_id, **prepared,
    )
    _mirror(author, "solving_reopened", f"REOPEN SOLVING: {_bounded(reason, 400)}",
            {"candidate_id": candidate_id, "reason": reason, **prepared})
    return result


def submit_formalization_verdict(
    author: str,
    verdict: str,
    summary: str,
    reviewed_main_sha: str,
    evidence: str = "",
    reopen_tasks: list[str] | None = None,
    source_fix: str = "",
) -> dict:
    """Submit a critic decision bound to the exact reviewed main revision."""
    author = _require_bound_author(author)
    public_to_state = {
        "approved": "approved",
        "lean_reopen": "revise_formalization",
        "source_fix": "source_fix",
        "reopen_solving": "revise_solution",
    }
    if verdict not in public_to_state:
        raise ValueError(
            "verdict must be approved, lean_reopen, source_fix, or reopen_solving"
        )
    prepared = _prepare_evidence(
        evidence, author=author, kind="formalization_critic_evidence",
        metadata={"verdict": verdict, "reopen_tasks": reopen_tasks or []},
    )
    rationale = f"[requested verdict: {verdict}] {summary}"
    if prepared.get("evidence_artifact_id"):
        rationale += f" [evidence: {prepared['evidence_artifact_id']}]"
    if source_fix:
        rationale += f" [source fix: {source_fix}]"
    # The persisted gate identity protects against a stale verdict, while this
    # immediate repository check prevents approval of bytes that are no longer
    # actually at main even if state has not yet observed the movement.
    from .. import worktree

    reviewed_main_sha = str(reviewed_main_sha or "").strip().lower()
    current_main_sha = worktree.main_commit(PROJECT_ROOT).lower()
    if reviewed_main_sha != current_main_sha:
        raise ValueError(
            "reviewed_main_sha does not match the current project main commit"
        )
    if _git("status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("the project main checkout has tracked changes")
    result = solve_store.record_formalization_verdict(
        _unity_dir(), author, public_to_state[verdict], rationale,
        reopen_task_ids=reopen_tasks or [],
        source_fix=source_fix,
        expected_reviewed_main_sha=reviewed_main_sha,
    )
    _mirror(author, "formalization_verdict",
            f"FORMALIZATION VERDICT {verdict}: {_bounded(summary, 400)}",
            {"verdict": verdict, "summary": summary, "source_fix": source_fix,
             "reopen_tasks": reopen_tasks or [],
             "reviewed_main_sha": reviewed_main_sha, **prepared})
    return {**result, "requested_verdict": verdict}


SOLVING_TOOLS: tuple[Callable, ...] = (
    create_subgoal,
    register_strategy,
    claim_strategy,
    assist_strategy,
    unclaim_strategy,
    mark_strategy_incorrect,
    publish_finding,
    supersede_finding,
    report_obstacle,
    ask_question,
    answer_question,
    emit_solution_candidate,
)

SOLUTION_REVIEW_TOOLS: tuple[Callable, ...] = (
    publish_finding,
    report_obstacle,
    ask_question,
    answer_question,
    review_solution_candidate,
    resolve_solution_objection,
)

FORMALIZING_TOOLS: tuple[Callable, ...] = (
    claim_formal_task,
    release_formal_task,
    register_strategy,
    claim_strategy,
    assist_strategy,
    unclaim_strategy,
    mark_strategy_incorrect,
    publish_finding,
    supersede_finding,
    report_obstacle,
    ask_question,
    answer_question,
    emit_formalization_candidate,
    sync_from_main,
    propose_source_fix,
    reopen_solving,
)

CRITIC_TOOLS: tuple[Callable, ...] = (submit_formalization_verdict,)
CHUNKING_TOOLS: tuple[Callable, ...] = ()
RETROSPECTIVE_TOOLS: tuple[Callable, ...] = ()

PROFILE_TOOLS: dict[str, tuple[Callable, ...]] = {
    "solving": SOLVING_TOOLS,
    "solution_review": SOLUTION_REVIEW_TOOLS,
    "chunking": CHUNKING_TOOLS,
    "formalizing": FORMALIZING_TOOLS,
    "critic": CRITIC_TOOLS,
    "retrospective": RETROSPECTIVE_TOOLS,
}


def build_server(profile: str) -> FastMCP:
    """Build a server whose advertised schema contains only one profile's tools."""
    if profile not in PROFILES:
        raise ValueError(f"unknown solve MCP profile '{profile}'")
    server = FastMCP(f"unity-solve-forum-{profile}")
    seen: set[str] = set()
    for function in (*COMMON_TOOLS, *PROFILE_TOOLS[profile]):
        if function.__name__ in seen:
            continue
        seen.add(function.__name__)
        server.tool()(function)
    return server


def run(
    run_dir: Path,
    project_root: Path,
    profile: str,
    forum_dir: Path | None = None,
) -> None:
    """Configure and run one solve profile server."""
    configure(run_dir, project_root, forum_dir)
    build_server(profile).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Unity solve-only Forum MCP server")
    parser.add_argument("--run-dir", required=True, help="Run-scoped .unity/solve directory")
    parser.add_argument("--project-root", required=True, help="Trusted main project checkout")
    parser.add_argument("--forum-dir", default=None, help="Legacy discussion Forum directory")
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    args = parser.parse_args()
    run(Path(args.run_dir), Path(args.project_root), args.profile,
        Path(args.forum_dir) if args.forum_dir else None)


if __name__ == "__main__":
    main()
