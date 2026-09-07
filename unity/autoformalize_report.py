"""Controller-written, evidence-bound reports for the autoformalize workflow.

The report records machine checks and a critic's semantic judgment separately.
It performs no proof search, model calls or Lean builds and does not change gates.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import fcntl
import hashlib
import json

from . import artifacts, autoformalize_contract, autoformalize_state
from .autoformalize_input import require_source_matches
from .autoformalize_review import SemanticReview


def completion_report(state: dict, *, accepted: bool = True) -> dict:
    """Describe exact recorded coverage; accepted reports require valid gate evidence."""
    formal = state.get("formalization", {})
    snapshot = formal.get("review_snapshot") or {}
    verdict = None
    if accepted:
        if state.get("phase") != "complete" or formal.get("status") != "accepted":
            raise ValueError("formalization has not been accepted")
        autoformalize_state._validate_snapshot_binding(state, snapshot, require_passed=True)
        verdict = next((item for item in state.get("critic_verdicts", [])
                        if item.get("verdict_id") == formal.get("accepted_verdict_id")), None)
        if (not verdict or verdict.get("verdict") != "approved"
                or verdict.get("snapshot_id") != snapshot.get("snapshot_id")
                or verdict.get("snapshot_sha256") != autoformalize_state._report_digest(snapshot)
                or verdict.get("requirements_sha256")
                != autoformalize_state._report_digest(formal.get("requirements", []))):
            raise ValueError("accepted critic evidence does not match the recorded snapshot")
        review = SemanticReview.model_validate(verdict["review"]).model_dump()
        if review["snapshot_id"] != snapshot["snapshot_id"] or verdict.get("reopen_tasks"):
            raise ValueError("accepted semantic evidence refers to another snapshot or reopens work")
        autoformalize_state._validate_semantic_review(
            state, review, approved=True, author=verdict["author"],
        )
    elif snapshot:
        # Historical evidence may explain an incomplete run, but never grants acceptance.
        verdict = next((item for item in reversed(state.get("critic_verdicts", []))
                        if item.get("snapshot_id") == snapshot.get("snapshot_id")), None)

    spec = formal.get("spec") or (formal.get("contract") or {}).get("spec") or {}
    anchors = {item["id"]: item for item in spec.get("anchors", [])}
    arguments = {item["requirement_id"]: item for item in spec.get("arguments", [])}
    reviews = {item["requirement_id"]: item
               for item in (verdict or {}).get("review", {}).get("requirements", [])}
    tasks = state.get("formal_tasks", {})
    candidates = state.get("formal_candidates", {})
    coverage = []
    for requirement in formal.get("requirements", []):
        implementation = []
        for task_id in requirement.get("tasks", []):
            task = tasks.get(task_id, {})
            candidate = candidates.get(task.get("accepted_candidate"), {})
            implementation.append({
                "task_id": task_id, "status": task.get("status", "missing"),
                "lean_file": task.get("lean_file"), "lean_decl": task.get("lean_decl"),
                "candidate_id": task.get("accepted_candidate"),
                "candidate_commit_sha": candidate.get("commit_sha"),
                "candidate_diff_sha256": candidate.get("diff_sha256"),
                "verification": candidate.get("verification"), "build": candidate.get("build"),
            })
        coverage.append({
            **requirement,
            "source_citations": [anchors[key] for key in requirement.get("anchor_ids", [])
                                 if key in anchors],
            "source_argument": arguments.get(requirement["id"]),
            "implementation": implementation,
            "critic_evidence": reviews.get(requirement["id"]),
        })
    adopted = {key for item in spec.get("arguments", []) for key in item.get("repair_ids", [])}
    repairs = [{**item, "adopted_in_argument": key in adopted}
               for key, item in state.get("source_repairs", {}).items()]
    return deepcopy({
        "schema_version": 1, "pipeline": "autoformalize", "run_id": state.get("run_id"),
        "status": "accepted" if accepted else "incomplete", "phase": state.get("phase"),
        "original_source": state.get("input_source"),
        "scope_sha256": state.get("problem_sha256"),
        "formalization_revision": formal.get("revision"), "main_sha": formal.get("main_sha"),
        "contract_sha256": (formal.get("contract") or {}).get("sha256"),
        "scope": spec.get("scope", {}), "source_anchors": spec.get("anchors", []),
        "prerequisites": spec.get("prerequisites", []),
        "source_issues": list(state.get("source_issues", {}).values()),
        "source_repairs": repairs, "source_repairs_sha256": autoformalize_state.repair_digest(state),
        "machine_review": snapshot, "critic_verdict": verdict,
        "machine_review_scope": "accepted exact revision" if accepted else "historical recorded evidence only",
        "coverage": coverage,
        "qualification": (
            "Machine checks validate the recorded Lean revision. Faithfulness and source-repair "
            "justification are the recorded critic's semantic judgments, not mechanically proven "
            "equivalence to natural language. The original documents are identified by the "
            "immutable input snapshot; repairs are explicit proposals or argument amendments."
        ),
    })


@contextmanager
def _merge_lock(paths):
    path = paths.project_root / ".unity" / "forum" / "merge.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def persist_report(paths, *, accepted: bool = True) -> dict:
    """Publish an idempotent immutable report without recompiling or changing acceptance.

    Incomplete reports describe observed recorded state, not a currently verified
    or accepted source revision. Accepted reports are rechecked against current
    inputs under the same merge lock used by candidate integration.
    """
    with _merge_lock(paths), autoformalize_state.transaction(paths.forum) as state:
        report = completion_report(state, accepted=accepted)
        snapshot = state["formalization"].get("review_snapshot") or {}

        def require_current():
            if accepted:
                require_source_matches(paths, state)
                if not autoformalize_contract.snapshot_is_current(paths, state, snapshot):
                    raise ValueError("cannot publish an accepted report for a stale source revision")

        require_current()
        payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
        sha256 = hashlib.sha256(payload.encode()).hexdigest()
        previous = state.get("final_report") or {}
        if previous.get("sha256") == sha256:
            try:
                existing = artifacts.artifact_bytes(paths.artifacts, previous["artifact_id"])
            except (OSError, ValueError, KeyError):
                pass
            else:
                if hashlib.sha256(existing).hexdigest() == sha256:
                    return dict(previous)
        record = artifacts.store_text(
            paths.artifacts, payload, kind="autoformalize_completion_report", producer="Unity",
            source=str(state.get("run_id") or ""),
            metadata={"status": report["status"], "snapshot_id": snapshot.get("snapshot_id")},
        )
        stored = artifacts.artifact_bytes(paths.artifacts, record["artifact_id"])
        if hashlib.sha256(stored).hexdigest() != sha256:
            raise ValueError("completion report artifact does not contain its exact recorded bytes")
        require_current()  # Do not publish if inputs changed while the artifact was written.
        reference = {"artifact_id": record["artifact_id"], "sha256": record["sha256"],
                     "status": report["status"], "run_id": state.get("run_id"),
                     "snapshot_id": snapshot.get("snapshot_id"),
                     "verdict_id": (report.get("critic_verdict") or {}).get("verdict_id"),
                     "main_sha": report["main_sha"]}
        state["final_report"] = reference
        return dict(reference)
