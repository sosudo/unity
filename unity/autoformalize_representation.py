"""Task-local encoding review within the existing autoformalize worker runtime.

Kernel-checked statements supply immutable inputs. These receipts are scoped
model judgments; neither a passing receipt nor source diagnosis approves a proof.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import time

from .autoformalize_review import RepresentationReview, SourceDiagnosis


def _state():
    # State calls these helpers while holding its existing transaction.
    from . import autoformalize_state
    return autoformalize_state


@lru_cache(maxsize=1)
def _policy_hash() -> str:
    root = Path(__file__).parent
    files = (Path(__file__), root / "autoformalize_review.py",
             root / "prompts/autoformalize/REPRESENTATION_REVIEW.md",
             root / "prompts/AUTOFORMALIZE_REPRESENTATION_REVIEW_TOOLS.md",
             root / "prompts/autoformalize/SOURCE_REPAIR.md")
    return _state().digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files})


def representation_review_input(state: dict, task_id: str) -> dict | None:
    """Identity excludes proof bytes, receipt IDs and unrelated main commits."""
    formal = state.get("formalization", {})
    contract = formal.get("contract") or {}
    task = state.get("formal_tasks", {}).get(task_id, {})
    outputs = contract.get("bindings", {}).get(task_id, [])
    if (contract.get("representation_review_policy") != 1 or not outputs
            or task.get("representation", {}).get("status") != "adopted"):
        return None
    targets = {row["declaration"]: contract.get("targets", {}).get(row["declaration"], {}).get("fingerprint")
               for row in outputs}
    if not all(isinstance(value, str) and value for value in targets.values()):
        return None
    requirements = sorted(({key: value for key, value in row.items() if key != "tasks"}
                           for row in formal.get("requirements", []) if task_id in row.get("tasks", [])),
                          key=lambda row: row["id"])
    anchor_ids = {key for row in requirements for key in row.get("anchor_ids", [])}
    anchors = sorted((row for row in formal.get("spec", {}).get("anchors", []) if row["id"] in anchor_ids),
                     key=lambda row: row["id"])
    requirement_ids = {row["id"] for row in requirements}
    repair_ids = {key for row in formal.get("spec", {}).get("arguments", [])
                  if row["requirement_id"] in requirement_ids for key in row.get("repair_ids", [])}
    payload = {"task_id": task_id, "source_sha256": _state().formal_source(state).get("sha256"),
               "requirements": requirements, "anchors": anchors,
               "informal_statement": task.get("informal_statement", ""),
               "outputs": sorted(outputs, key=lambda row: (row["declaration"], row["file"])),
               "targets": targets,
               "repairs": {key: state.get("source_repairs", {}).get(key, {}).get("sha256") for key in sorted(repair_ids)},
               "environment": contract.get("environment"), "policy_sha256": _policy_hash()}
    return {**deepcopy(payload), "input_sha256": _state().digest(payload)}


def current_representation_review(state: dict, task_id: str) -> dict | None:
    payload = representation_review_input(state, task_id)
    if payload is None:
        return None
    record = state.get("representation_reviews", {}).get(payload["input_sha256"])
    return deepcopy(record) if record else None


def snapshot(state: dict) -> dict:
    """Compact current semantic evidence for final snapshots, never historical IDs."""
    if (state.get("formalization", {}).get("contract") or {}).get("representation_review_policy") != 1:
        return {}
    result = {}
    for task_id in sorted(state.get("formal_tasks", {})):
        payload = representation_review_input(state, task_id)
        record = state.get("representation_reviews", {}).get((payload or {}).get("input_sha256"), {})
        result[task_id] = {"input_sha256": (payload or {}).get("input_sha256"),
                           "status": record.get("status", "missing")}
    return result


def queue_representation_review(state: dict, task_id: str) -> dict | None:
    """Called inside the merge transaction; matching reviews are never reset."""
    payload = representation_review_input(state, task_id)
    if payload is None:
        return None
    records = state.setdefault("representation_reviews", {})
    key = payload["input_sha256"]
    feedback = state["formal_tasks"][task_id].get("faithfulness", {}).get("feedback_sha256")
    if key not in records:
        task = state["formal_tasks"][task_id]
        candidate = state.get("formal_candidates", {}).get(task.get("representation", {}).get("candidate_id"), {})
        records[key] = {"task_id": task_id, "input_sha256": key, "input": payload,
                        "status": "pending", "owner": None, "attempts": [],
                        "feedback_sha256": feedback, "critic_feedback_sha256": feedback,
                        "representation_author": candidate.get("author", ""),
                        "main_sha": candidate.get("main_sha") or state["formalization"].get("main_sha"),
                        "created_at": time.time()}
        _state()._event(state, "representation_review_queued", task_id=task_id, input_sha256=key)
    elif (records[key]["status"] in {"uncertain", "exhausted"} and feedback
          and feedback != records[key].get("critic_feedback_sha256", records[key].get("feedback_sha256"))):
        records[key].update(status="pending", owner=None, feedback_sha256=feedback, critic_feedback_sha256=feedback)
        _state()._event(state, "representation_review_reopened", task_id=task_id,
                       input_sha256=key, feedback_sha256=feedback)
    elif records[key]["status"] == "source_issue" and records[key].get("issue_id"):
        diagnosis = source_diagnosis_current(state, records[key]["issue_id"])
        if diagnosis and diagnosis["verdict"] == "false_alarm":
            epoch = "diagnosis:" + _state().digest({name: diagnosis[name] for name in ("input_sha256", "verdict", "evidence")})
            if epoch != records[key].get("feedback_sha256"):
                records[key].update(status="pending", owner=None, feedback_sha256=epoch,
                                    source_diagnosis=diagnosis)
                _state()._event(state, "representation_review_reopened", task_id=task_id,
                               input_sha256=key, source_diagnosis_sha256=epoch)
    if records[key]["status"] == "encoding_error":
        # Proof-only re-adoption retains rejection and its semantic retry budget.
        _state()._reconcile_rejected_representation(state, task_id, records[key])
    return deepcopy(records[key])


def attempted_reviewers(record: dict) -> set[str]:
    """New diagnostic guidance, not tool chatter, permits another outer attempt."""
    return {_state().author_key(item["author"]) for item in record.get("attempts", [])
            if item["status"] != "interrupted"
            and item.get("feedback_sha256") == record.get("feedback_sha256")}


def pending_representation_reviews(state: dict) -> list[dict]:
    return [record for key in state.get("formal_tasks", {})
            if (record := current_representation_review(state, key)) and record["status"] == "pending"]


def claim_representation_review(forum_dir: Path, task_id: str, author: str) -> dict:
    module = _state()
    with module.transaction(forum_dir) as state:
        if state.get("phase") != "formalizing" or module.pending_replan(state):
            return {"status": "unavailable", "review": None}
        record = queue_representation_review(state, task_id)
        if record is None or record["status"] != "pending":
            return {"status": "unavailable", "review": record}
        row = state["representation_reviews"][record["input_sha256"]]
        if module.author_key(author) in attempted_reviewers(row):
            return {"status": "exhausted", "review": deepcopy(row)}
        row.update(status="reviewing", owner=module._text(author, "author", 100))
        row["attempts"].append({"author": author, "status": "active", "started_at": time.time(),
                                "feedback_sha256": row.get("feedback_sha256")})
        return {"status": "claimed", "review": deepcopy(row)}


def _source_issue(state: dict, author: str, task_id: str, anchors: list[str], description: str,
                  input_sha256: str) -> str:
    module = _state()
    source = module.formal_source(state)
    for row in state["source_issues"].values():
        if (row["description"] == description and set(row["anchor_ids"]) == set(anchors)
                and row.get("task_ids") == [task_id] and row.get("source_sha256") == source["sha256"]
                and row.get("representation_input_sha256") == input_sha256):
            return row["issue_id"]
    key = module._id("source-issue")
    state["source_issues"][key] = {
        "issue_id": key, "author": author, "anchor_ids": anchors, "task_ids": [task_id],
        "description": description, "source_candidate": source["candidate_id"],
        "source_sha256": source["sha256"], "status": "open", "owner": None,
        "representation_input_sha256": input_sha256,
        "attempts": [], "repair_ids": [], "created_at": time.time(),
    }
    module._event(state, "source_issue_reported", issue_id=key, author=author, task_ids=[task_id])
    return key


def submit_representation_review(forum_dir: Path, author: str, task_id: str, review: dict) -> dict:
    module = _state()
    evidence = RepresentationReview.model_validate(review).model_dump()
    with module.transaction(forum_dir) as state:
        if state.get("phase") != "formalizing":
            raise ValueError("representation review requires the active formalizing runtime")
        payload = representation_review_input(state, task_id)
        if payload is None or payload["input_sha256"] != evidence["input_sha256"]:
            raise ValueError("representation review is stale; inspect the current exact input")
        record = state.get("representation_reviews", {}).get(evidence["input_sha256"])
        if (record and record.get("status") in {"aligned", "encoding_error", "source_issue", "uncertain"}
                and record.get("review") == evidence
                and module.author_key(record.get("reviewer")) == module.author_key(author)):
            return deepcopy(record)
        if (not record or record["status"] != "reviewing"
                or module.author_key(record.get("owner")) != module.author_key(author)):
            raise ValueError("representation review is not owned by this worker")
        anchors = {row["id"] for row in payload["anchors"]}
        if set(evidence["checked_anchor_ids"]) != anchors or len(evidence["checked_anchor_ids"]) != len(anchors):
            raise ValueError("representation review must cover the exact source anchors")
        record.update(status=evidence["verdict"], owner=None, review=evidence, reviewer=author,
                      finished_at=time.time())
        record["attempts"][-1].update(status="submitted", finished_at=time.time())
        if evidence["verdict"] == "encoding_error":
            module._invalidate_informal_tasks(state, {task_id}, reason="Encoding review: " + evidence["rationale"])
        elif evidence["verdict"] == "source_issue":
            record["issue_id"] = _source_issue(state, author, task_id, sorted(anchors),
                evidence["rationale"] + "\nEvidence: " + evidence["evidence"], evidence["input_sha256"])
            diagnosis = source_diagnosis_current(state, record["issue_id"])
            if diagnosis and diagnosis["verdict"] == "false_alarm":
                # Repeating an already diagnosed report is inconclusive review,
                # not new source work. Only new critic guidance can retry it.
                record["status"] = "uncertain"
        module._invalidate_review(state)
        module._event(state, "representation_review_submitted", task_id=task_id,
                      input_sha256=evidence["input_sha256"], verdict=evidence["verdict"], author=author)
        return deepcopy(record)


def finish_representation_attempt(forum_dir: Path, input_sha256: str, author: str,
                                  reviewers: list[str], error: str = "", *, interrupted: bool = False) -> dict:
    module = _state()
    with module.transaction(forum_dir) as state:
        row = state.get("representation_reviews", {}).get(input_sha256)
        if (row and row["status"] == "reviewing"
                and module.author_key(row.get("owner")) == module.author_key(author)):
            current = representation_review_input(state, row["task_id"])
            interrupted = interrupted or not current or current["input_sha256"] != input_sha256
            row["attempts"][-1].update(status="interrupted" if interrupted else "failed",
                                       error=error or "No structured review submitted",
                                       finished_at=time.time())
            tried = attempted_reviewers(row)
            row.update(status="exhausted" if set(map(module.author_key, reviewers)) <= tried else "pending", owner=None)
        return deepcopy(row or {})


def recover_representation_reviews(forum_dir: Path) -> None:
    """Controller-only, after terminating prior workers on resume."""
    module = _state()
    with module.transaction(forum_dir) as state:
        for row in state.get("representation_reviews", {}).values():
            if row["status"] != "reviewing":
                continue
            for attempt in row.get("attempts", []):
                if attempt["status"] == "active":
                    attempt.update(status="interrupted", error="Controller resumed after interruption",
                                   finished_at=time.time())
            row.update(status="pending", owner=None)
            module._event(state, "representation_review_recovered", task_id=row["task_id"],
                          input_sha256=row["input_sha256"])
        for task_id in state.get("formal_tasks", {}):
            queue_representation_review(state, task_id)


def source_diagnosis_input(state: dict, issue_id: str) -> dict:
    issue = state.get("source_issues", {}).get(issue_id)
    if not issue:
        raise ValueError("unknown source issue")
    targets = issue.get("task_ids") or sorted(state.get("formal_tasks", {}))
    formal = state.get("formalization", {})
    contract = formal.get("contract") or {}
    representations = {key: {
        "statement": state.get("formal_tasks", {}).get(key, {}).get("informal_statement"),
        "targets": {row["declaration"]: contract.get("targets", {}).get(row["declaration"], {}).get("fingerprint")
                    for row in contract.get("bindings", {}).get(key, [])},
    } for key in targets}
    payload = {"issue_id": issue_id, "source_sha256": _state().formal_source(state).get("sha256"),
               "description": issue["description"], "anchor_ids": issue["anchor_ids"], "task_ids": targets,
               "representations": representations, "environment": contract.get("environment"),
               "policy_sha256": _policy_hash()}
    return {**payload, "input_sha256": _state().digest(payload)}


def source_diagnosis_current(state: dict, issue_id: str) -> dict | None:
    row = state.get("source_issues", {}).get(issue_id, {}).get("diagnosis")
    return deepcopy(row) if row and row["input_sha256"] == source_diagnosis_input(state, issue_id)["input_sha256"] else None


def submit_source_diagnosis(forum_dir: Path, author: str, issue_id: str, review: dict) -> dict:
    module = _state()
    evidence = SourceDiagnosis.model_validate(review).model_dump()
    with module.transaction(forum_dir) as state:
        issue = state.get("source_issues", {}).get(issue_id)
        if not issue:
            raise ValueError("source issue is unknown or already resolved")
        if issue.get("diagnosis", {}).get("input_sha256") == evidence["input_sha256"] and all(
                issue["diagnosis"].get(key) == value for key, value in evidence.items()):
            return deepcopy(issue)
        if issue["status"] == "resolved":
            raise ValueError("source issue is already resolved")
        if evidence["input_sha256"] != source_diagnosis_input(state, issue_id)["input_sha256"]:
            raise ValueError("source diagnosis is stale; inspect the current issue and encoding")
        if module.author_key(issue.get("owner")) != module.author_key(author):
            raise ValueError("source diagnosis requires the current source-repair owner")
        issue["diagnosis"] = {**evidence, "author": author, "created_at": time.time()}
        if evidence["verdict"] in {"false_alarm", "encoding_error"}:
            if evidence["verdict"] == "encoding_error":
                targets = set(issue.get("task_ids") or state["formal_tasks"])
                if targets:
                    module._invalidate_informal_tasks(state, targets, reason="Source diagnosis: " + evidence["evidence"])
            issue.update(status="resolved", owner=None, resolution=evidence["verdict"], resolved_at=time.time())
            if evidence["verdict"] == "false_alarm":
                for task_id in state.get("formal_tasks", {}):
                    record = current_representation_review(state, task_id)
                    if record and record.get("issue_id") == issue_id:
                        queue_representation_review(state, task_id)
        elif (evidence["verdict"] == "source_defect" and module._live_repair_ids(issue)
              and state["phase"] in {"formalizing", "critic"}):
            module._queue_replan(state, author, "Diagnosed source repair for " + issue_id, issue["task_ids"])
        module._invalidate_review(state)
        module._event(state, "source_issue_diagnosed", issue_id=issue_id, author=author, verdict=evidence["verdict"])
        return deepcopy(issue)


async def representation_review_turn(agent, roster, paths, task_id: str,
                                     *, interrupt_event: asyncio.Event | None = None) -> dict:
    """One fresh review context using existing workers, worktrees and telemetry."""
    from . import artifacts, worktree
    from .autoformalize_orchestrator import _preamble, build_autoformalize_mcp, stop_requested
    from .autoformalize_runtime import _agent_runtime_env, _formal_worktree
    from .autoformalize_spawn import spawn

    if stop_requested(paths.project_root):
        return {"status": "stopped"}
    claim = claim_representation_review(paths.forum, task_id, agent.name)
    if claim["status"] != "claimed":
        return claim
    record = claim["review"]
    error = ""
    interrupted = False
    try:
        state = _state().load_state(paths.forum)
        current = representation_review_input(state, task_id)
        if not current or current["input_sha256"] != record["input_sha256"]:
            return {"status": "stale"}
        tree = _formal_worktree(paths.project_root, agent.name)
        worktree.link_runtime_state(tree, paths.project_root)
        worktree.symlink_lake_cache(tree, paths.project_root)
        contract = state["formalization"]["contract"]
        material = {"input": record["input"], "main_sha": record["main_sha"],
                    "declarations": {name: contract["targets"][name] for name in record["input"]["targets"]},
                    "repairs": {key: state["source_repairs"][key] for key in record["input"]["repairs"]},
                    "source_diagnosis": record.get("source_diagnosis")}
        artifact = artifacts.store_text(paths.project_root / ".unity" / "artifacts",
            json.dumps(material, sort_keys=True), kind="autoformalize_representation_review",
            producer="Unity", source=record["input_sha256"])
        prompt_dir = Path(__file__).parent / "prompts"
        system = (_preamble(agent, roster, icrl_enabled=False)
                  + "\n" + (prompt_dir / "autoformalize/REPRESENTATION_REVIEW.md").read_text()
                  + "\n" + (prompt_dir / "AUTOFORMALIZE_REPRESENTATION_REVIEW_TOOLS.md").read_text())
        env = _agent_runtime_env(paths, state, agent.name, task_id=task_id)
        env["UNITY_AUTOFORMALIZE_PROFILE"] = "representation_review"
        result = await spawn(agent, system,
            f"Review task {task_id}, input {record['input_sha256']}. Exact review artifact: {artifact['artifact_id']}. "
            "Read it with artifact_read. Your own worktree can contain unrelated private work; preserve it. "
            "Inspect the bound accepted revision, not your worktree's current files. Submit the structured review.",
            tree, build_autoformalize_mcp(paths, "representation_review"),
            interrupt_event=interrupt_event, env_overrides=env, own_process_group=True,
            mcp_profile="autoformalize", log_context={"command": "autoformalize", "run_id": state.get("run_id"),
                "phase": "formalizing", "role": "representation_review", "task_id": task_id,
                "input_sha256": record["input_sha256"]})
        if isinstance(result, BaseException):
            error = f"{type(result).__name__}: {result}"
    except asyncio.CancelledError:
        error = "representation review interrupted"
        interrupted = True
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        record = finish_representation_attempt(paths.forum, record["input_sha256"], agent.name,
            [row.name for row in roster.agents], error,
            interrupted=interrupted or bool(interrupt_event and interrupt_event.is_set()))
    return {"status": "attempted", "review": record, "error": error}
