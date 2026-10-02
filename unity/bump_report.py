"""Controller-written, evidence-bound reports for the bump workflow.

The report records machine checks and a critic's semantic judgment separately.
It performs no proof search, model calls or Lean builds and does not change gates.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import fcntl
import hashlib
import json

from . import artifacts, bump_contract, bump_state
from .bump_input import require_source_matches
from .bump_review import SemanticReview


def _project_verification(state: dict, snapshot: dict, *, accepted: bool = False) -> dict:
    """Report the two-version boundary without calling inherited holes proofs."""
    baseline = ((state.get("formalization", {}).get("contract") or {}).get("project_baseline")
                or state.get("project_baseline") or {})
    migration = baseline.get("migration") or {}
    scope = baseline.get("build_scope") or {}
    current = snapshot.get("project_verification")
    modules = {row["path"]: module for module, row in baseline.get("compiler_modules", {}).items()}
    if accepted:
        from . import bump_migration_project
        if (baseline.get("policy") != "migration-v1" or baseline.get("version") != 5
                or baseline.get("occurrence_policy") != 1
                or baseline.get("scope_policy") != 1 or scope != migration.get("scope")
                or bump_migration_project.scope_errors(scope, migration.get("source_files"))
                or current != bump_contract._migration_project_verification(baseline)):
            raise ValueError("accepted migration report requires matching current two-version coverage")
    inherited = {
        module: {name: {"kind": row.get("kind"), "direct_sorry": row.get("direct_sorry"),
                        "axioms": row.get("axioms", [])}
                 for name, row in report.get("declarations", {}).items()
                 if row.get("kind") == "axiom" or row.get("direct_sorry") or "sorryAx" in row.get("axioms", [])}
        for module, report in baseline.get("original_reports", {}).items()
    }
    return {
        "mode": "migration", "policy": baseline.get("policy"),
        "baseline_sha256": baseline.get("sha256"),
        "original_source_commit": migration.get("source_commit"),
        "original_source_sha256": migration.get("source_hash"),
        "target_version": migration.get("target_version"),
        "original_verification_modules": modules,
        "project_scope": scope.get("mode"), "scope_sha256": scope.get("sha256"),
        "byte_preserved_excluded_files": deepcopy(scope.get("excluded_files", {})),
        "byte_only_modules": {path: name for name, path in scope.get("excluded_modules", {}).items()},
        "inherited_assumptions_and_holes": inherited,
        "declaration_occurrences": deepcopy(current.get("declaration_occurrences", {})) if current else {},
        "current_snapshot_coverage": current,
        "qualification": (
            "Cross-version preservation checks compare each selected original module's declarations and "
            "meanings with its migrated native context, including modules with no declarations. "
            "Same-named declarations in different original module artifacts remain separate obligations "
            "with separate type, meaning, proof-assumption and compiled-context evidence. "
            "Excluded files are preserved byte-for-byte, not compiled, repaired, or claimed newly "
            "kernel-verified. No whole-repository compilation claim follows from scoped acceptance. "
            "A successful target build is not sufficient. Original axioms and incomplete proofs "
            "are recorded inherited assumptions, not newly proved results; the migration must not "
            "introduce or broaden them. The original checkout remains separate from the migrated "
            "target. Structural checks and the independent critic's semantic judgment are distinct."
        ),
    }


def completion_report(state: dict, *, accepted: bool = True) -> dict:
    """Describe exact recorded coverage; accepted reports require valid gate evidence."""
    if (state.get("formalization", {}).get("contract") or {}).get("migration_policy") == 2:
        return _completion_report_v2(state, accepted=accepted)
    formal = state.get("formalization", {})
    snapshot = formal.get("review_snapshot") or {}
    verdict = None
    if accepted:
        if state.get("phase") != "complete" or formal.get("status") != "accepted":
            raise ValueError("formalization has not been accepted")
        if ((formal.get("contract") or {}).get("migration_policy") != 1
                or (formal.get("contract") or {}).get("migration_scope_policy") != 1
                or (formal.get("contract") or {}).get("migration_occurrence_policy") != 1):
            raise ValueError("accepted Bump report requires the migration contract policy")
        if not bump_contract._baseline_matches(state, formal.get("contract") or {}):
            raise ValueError("accepted Bump report requires the original project baseline")
        bump_state._validate_snapshot_binding(state, snapshot, require_passed=True)
        verdict = next((item for item in state.get("critic_verdicts", [])
                        if item.get("verdict_id") == formal.get("accepted_verdict_id")), None)
        if (not verdict or verdict.get("verdict") != "approved"
                or verdict.get("snapshot_id") != snapshot.get("snapshot_id")
                or verdict.get("snapshot_sha256") != bump_state._report_digest(snapshot)
                or verdict.get("requirements_sha256")
                != bump_state._report_digest(formal.get("requirements", []))):
            raise ValueError("accepted critic evidence does not match the recorded snapshot")
        review = SemanticReview.model_validate(verdict["review"]).model_dump()
        if review["snapshot_id"] != snapshot["snapshot_id"] or verdict.get("reopen_tasks"):
            raise ValueError("accepted semantic evidence refers to another snapshot or reopens work")
        bump_state._validate_semantic_review(
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
                "migration_module": task.get("migration_module"),
                "lean_file": task.get("lean_file"), "lean_decl": task.get("lean_decl"),
                "outputs": task.get("outputs", []),
                "representation": task.get("representation"),
                "implementation_verification": task.get("verification"),
                "faithfulness": task.get("faithfulness"),
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
        "schema_version": 1, "pipeline": "bump", "run_id": state.get("run_id"),
        "status": "accepted" if accepted else "incomplete", "phase": state.get("phase"),
        "original_source": state.get("input_source"),
        "scope_sha256": state.get("problem_sha256"),
        "project_baseline_sha256": (state.get("project_baseline") or {}).get("sha256"),
        "project_scope": "migration",
        "project_verification": _project_verification(state, snapshot, accepted=accepted),
        "formalization_revision": formal.get("revision"), "main_sha": formal.get("main_sha"),
        "contract_sha256": (formal.get("contract") or {}).get("sha256"),
        "scope": spec.get("scope", {}), "source_anchors": spec.get("anchors", []),
        "prerequisites": spec.get("prerequisites", []),
        "source_issues": list(state.get("source_issues", {}).values()),
        "source_repairs": repairs, "source_repairs_sha256": bump_state.repair_digest(state),
        "machine_review": snapshot, "critic_verdict": verdict,
        "machine_review_scope": "accepted exact revision" if accepted else "historical recorded evidence only",
        "coverage": coverage,
        "qualification": (
            "Machine checks bind the original project and exact migrated revision to their "
            "respective Lean environments. The critic separately judges migration faithfulness; "
            "this is not a proof of arbitrary cross-version equivalence. Preserved original "
            "axioms or incomplete proofs are not certified as new proofs. No source paper is "
            "required and no user project changes are implied by a private target's acceptance."
        ),
    })


def _completion_report_v2(state: dict, *, accepted: bool) -> dict:
    """Policy-2 coverage is the original ledger, not the currently active repair queue."""
    from . import bump_checker_v2
    formal = state.get("formalization", {})
    contract = formal.get("contract") or {}
    snapshot = formal.get("review_snapshot") or {}
    verdict = None
    if accepted:
        if state.get("phase") != "complete" or formal.get("status") != "accepted":
            raise ValueError("migration has not been accepted")
        bump_checker_v2.validate_contract(contract)
        bump_contract.validate_migration_snapshot(state, snapshot)
        bump_state._validate_snapshot_binding(state, snapshot, require_passed=True)
        verdict = next((row for row in state.get("critic_verdicts", [])
            if row.get("verdict_id") == formal.get("accepted_verdict_id")), None)
        if (not verdict or verdict.get("verdict") != "approved"
                or verdict.get("snapshot_id") != snapshot.get("snapshot_id")
                or verdict.get("snapshot_sha256") != bump_state._report_digest(snapshot)
                or verdict.get("requirements_sha256") != bump_state._report_digest(formal.get("requirements", []))):
            raise ValueError("accepted semantic verdict does not match the migration snapshot")
        review = SemanticReview.model_validate(verdict["review"]).model_dump()
        if review["snapshot_id"] != snapshot["snapshot_id"] or verdict.get("reopen_tasks"):
            raise ValueError("semantic review is stale or reopens migration obligations")
        bump_state._validate_semantic_review(state, review, approved=True, author=verdict["author"])
    elif snapshot:
        verdict = next((row for row in reversed(state.get("critic_verdicts", []))
            if row.get("snapshot_id") == snapshot.get("snapshot_id")), None)
    reviews = {row["requirement_id"]: row for row in (verdict or {}).get("review", {}).get("requirements", [])}
    coverage = []
    for requirement in contract.get("requirements", []):
        groups = requirement["tasks"]
        obligations = sorted({key for group in groups for key in contract["task_bindings"][group]["obligation_ids"]})
        coverage.append({**deepcopy(requirement), "original_occurrence_ids": obligations,
            "execution_groups": {key: deepcopy(contract["task_bindings"][key]) for key in groups},
            "mapping_groups": {key: deepcopy(row) for key, row in contract["mapping"].items()
                if set(row["original_ids"]).intersection(obligations)},
            "machine_receipts": {key: deepcopy(snapshot.get("module_receipts", {}).get(key)) for key in groups},
            "critic_evidence": deepcopy(reviews.get(requirement["id"]))})
    return {"schema_version": 2, "pipeline": "bump", "migration_policy": 2, "inspection_policy": 5,
        "run_id": state.get("run_id"), "status": "accepted" if accepted else "incomplete", "phase": state.get("phase"),
        "main_sha": formal.get("main_sha"), "original_source": deepcopy(state.get("input_source")),
        "project_baseline_sha256": contract.get("project_baseline_sha256"),
        "baseline_ref": deepcopy(contract.get("baseline_ref")),
        "original_index_ref": deepcopy(contract.get("original_index_ref")),
        "original_index_sha256": contract.get("original_index_sha256"),
        "mapping_sha256": contract.get("mapping_sha256"), "contract_sha256": contract.get("sha256"),
        "project_verification": deepcopy(snapshot.get("project_verification")),
        "coverage": coverage, "machine_review": deepcopy(snapshot), "critic_verdict": deepcopy(verdict),
        "machine_review_scope": "accepted exact revision" if accepted else "historical recorded evidence only",
        "semantic_equivalence_proved": False,
        "qualification": (
            "Every selected original declaration occurrence is covered, including clean modules with no model repair task. "
            "Native checks compile complete target module contexts, bind local type/value identity or explicitly declared "
            "correspondence, and enforce per-original no-new-trust. Theorem proof bodies may change. "
            "Recursive equality of upstream library definitions is not checked; compatibility of the pinned upgraded "
            "imports is an explicit assumption. Declared correspondence artifacts and compilation do not prove mathematical "
            "equivalence: the independent critic separately judges fidelity to frozen original source. Original axioms and "
            "holes are inherited, not newly proved. Excluded files are byte-preserved, not migrated or kernel-verified."
        )}


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
    with _merge_lock(paths), bump_state.transaction(paths.forum) as state:
        report = completion_report(state, accepted=accepted)
        snapshot = state["formalization"].get("review_snapshot") or {}

        def require_current():
            if accepted:
                require_source_matches(paths, state)
                if not bump_contract.snapshot_is_current(paths, state, snapshot):
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
            paths.artifacts, payload, kind="bump_completion_report", producer="Unity",
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
