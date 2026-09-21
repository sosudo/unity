"""Explicit source-bound test fixtures for the duplicated solve formal runtime.

These construct controller evidence in tests; they never loosen production
validation or mock away source/task/contract binding checks.
"""

from copy import deepcopy

from unity import solve_contract, solve_representation, solve_state
from unity.solve_spec import digest


def formal_fixture(candidate, chunks, *, requirements=None, contract=None):
    """Return (requirements, contract) for a legacy explicit declaration graph."""
    refs = [f"paper:{candidate['candidate_id']}",
            *[part["result_id"] for part in candidate.get("components", [])]]
    requirements = deepcopy(requirements) if requirements is not None else [{
        "id": "R1", "statement": "The Lean result faithfully proves the accepted paper.",
        "source_components": refs, "tasks": [row.get("id", row.get("task_id")) for row in chunks],
    }]
    anchors = []
    for req in requirements:
        req.setdefault("anchor_ids", [f"{req['id']}-anchor-{index}" for index, _ in enumerate(req["source_components"])])
        for index, anchor in enumerate(req["anchor_ids"]):
            if any(row["id"] == anchor for row in anchors):
                continue
            anchors.append({"id": anchor, "source_ref": req["source_components"][index % len(req["source_components"])],
                            "location": "Source claim " + req["id"], "excerpt": req["statement"]})
    spec = {"version": 1, "anchors": anchors,
            "scope": {"targets": [row["id"] for row in anchors], "references": [], "excluded": []},
            "prerequisites": [], "arguments": [{"requirement_id": req["id"], "anchor_ids": req["anchor_ids"],
                "outline": "The Lean proof follows the stated source argument.", "prerequisites": [], "repair_ids": []}
                for req in requirements]}
    contract = deepcopy(contract or {})
    contract.setdefault("version", 2)
    contract.setdefault("targets", {row["lean_decl"]: {} for row in chunks})
    contract.setdefault("environment", {})
    contract.update(solution_candidate=candidate["candidate_id"], solution_sha256=candidate["sha256"],
                    requirements=deepcopy(requirements), spec=spec, spec_sha256=digest(spec))
    contract["sha256"] = digest({key: value for key, value in contract.items() if key not in {"sha256", "artifact_id"}})
    return requirements, contract


def initialize_graph(forum, chunks, **kwargs):
    """Build valid fixture metadata and call the actual state transition."""
    state = solve_state.load_state(forum)
    candidate = state["solution_candidates"][kwargs["solution_candidate"]]
    requirements, contract = formal_fixture(candidate, chunks,
        requirements=kwargs.pop("requirements", None), contract=kwargs.pop("contract", None))
    return solve_state.initialize_formal_tasks(forum, chunks, requirements=requirements, contract=contract, **kwargs)


def source_binding(candidate):
    return solve_state.formal_source({"solution": {"status": "accepted", "accepted_candidate": candidate["candidate_id"]},
        "solution_candidates": {candidate["candidate_id"]: {**candidate, "status": "accepted"}}})


def informal_dag(candidate, chunks, *, requirements=None):
    requirements, contract = formal_fixture(candidate, chunks, requirements=requirements)
    nodes = []
    for row in chunks:
        task_id = row.get("id", row.get("task_id"))
        reqs = [req for req in requirements if task_id in req["tasks"]]
        nodes.append({"id": task_id, "title": row.get("title", task_id), "predicted_kind": "theorem",
            "informal_statement": row.get("summary", "The source claim holds."), "informal_proof": "Direct proof.",
            "statement_dependencies": [], "proof_dependencies": list(row.get("dependencies", [])),
            "source_components": list(row["source_components"]),
            "anchor_ids": sorted({key for req in reqs for key in req["anchor_ids"]}),
            "requirement_ids": [req["id"] for req in reqs],
            "proposed_formal_statement": None, "proposed_formal_strategy": None})
    return {"solution_candidate": candidate["candidate_id"], "solution_sha256": candidate["sha256"],
            "requirements": requirements, "spec": contract["spec"], "chunks": nodes}


def machine_snapshot(state, report=None):
    formal = state["formalization"]
    contract = formal["contract"]
    declarations = {output["declaration"]: key for key, task in state["formal_tasks"].items()
                    for output in (task.get("outputs") or [{"declaration": task["lean_decl"]}])}
    result = {"snapshot_id": f"snapshot-{state['revision']}", "passed": True,
              "main_sha": formal["main_sha"], "source_sha256": "9" * 64,
              "solution_candidate": formal["solution_candidate"], "solution_sha256": formal["solution_sha256"],
              "formalization_revision": formal["revision"], "contract_sha256": contract["sha256"],
              "policy_sha256": solve_contract.policy_hash(), "spec_sha256": digest(formal["spec"]),
              "repairs_sha256": solve_state.repair_digest(state), "environment": deepcopy(contract.get("environment", {})),
              "representation_reviews": solve_representation.snapshot(state),
              "external_declarations": solve_contract._external_evidence(contract),
              "prerequisite_declarations": {name: {key: row.get(key) for key in ("fingerprint", "module", "signature", "axioms")}
                    for name, row in contract.get("prerequisite_declarations", {}).items()},
              "accepted_candidates": {key: task.get("accepted_candidate") for key, task in state["formal_tasks"].items()},
              "task_statuses": {key: task["status"] for key, task in state["formal_tasks"].items()},
              "declarations": declarations, "issues": []}
    result.update(deepcopy(report or {}))
    return result


def semantic_evidence(state):
    return {"snapshot_id": state["formalization"]["review_snapshot"]["snapshot_id"],
            "scope_rationale": "The review covers the accepted paper and its arguments.",
            "requirements": [{"requirement_id": req["id"], "status": "pass",
                "declarations": [output["declaration"] for key in req["tasks"]
                                 for output in (state["formal_tasks"][key].get("outputs") or
                                                [{"declaration": state["formal_tasks"][key]["lean_decl"]}])],
                "checked_anchor_ids": req["anchor_ids"], "checked_prerequisite_ids": sorted({
                    key for argument in state["formalization"]["spec"]["arguments"]
                    if argument["requirement_id"] == req["id"] for key in argument["prerequisites"]}),
                "rationale": "The exact statement preserves the source claim.",
                "argument_rationale": "The checked Lean proof implements the source argument."}
                for req in state["formalization"]["requirements"]]}


def verification_receipt(state):
    return {"status": "passed", "contract_sha256": state["formalization"]["contract"]["sha256"],
            "policy_sha256": solve_contract.policy_hash()}
