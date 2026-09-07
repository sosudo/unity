"""Source-evidence bookkeeping for autoformalization, not an English proof checker.

The controller checks identities, references and coverage. A separate critic must
read the bound documents and judge the stated scope and argument correspondence.
"""

from __future__ import annotations

import hashlib
import json


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} requires nonempty text")
    return value.strip()


def _refs(value, field: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{field} requires {'a nonempty' if nonempty else 'a'} list")
    if any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in value):
        raise ValueError(f"{field} has invalid references")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} has duplicate references")
    return sorted(value)


def _object(value, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} requires exactly: {', '.join(sorted(fields))}")
    return value


def _rows(value, name: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _known(values, allowed, field: str) -> None:
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(f"{field} has unknown references: {', '.join(sorted(unknown))}")


def _task_map(tasks) -> dict[str, dict]:
    if isinstance(tasks, dict):
        return tasks
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        raise ValueError("tasks must be a task mapping or list")
    result = {_text(task.get("task_id", task.get("id")), "task id"): task for task in tasks}
    if len(result) != len(tasks):
        raise ValueError("tasks have duplicate ids")
    return result


def normalize_requirements(requirements, tasks, source_refs) -> list[dict]:
    """Normalize the existing ledger, now with explicit source-anchor references.

    Whole-bundle accounting belongs to the spec: a reference or excluded document
    does not need an artificial theorem requirement merely to account for its file.
    """
    tasks = _task_map(tasks)
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("formalization requires a nonempty requirements ledger")
    result, seen = [], set()
    for row in requirements:
        _object(row, {"id", "statement", "source_components", "tasks", "anchor_ids"}, "requirement")
        identifier = _text(row["id"], "requirement id")
        if identifier in seen:
            raise ValueError("requirements have duplicate ids")
        seen.add(identifier)
        sources = _refs(row["source_components"], "requirement source_components")
        task_ids = _refs(row["tasks"], "requirement tasks")
        anchors = _refs(row["anchor_ids"], "requirement anchor_ids")
        _known(sources, source_refs, "requirement source_components")
        _known(task_ids, tasks, "requirement tasks")
        covered = {ref for task_id in task_ids for ref in tasks[task_id].get("source_components", [])}
        if set(sources) - covered:
            raise ValueError(f"requirement {identifier} sources are not covered by its tasks")
        result.append({"id": identifier, "statement": _text(row["statement"], "requirement statement"),
                       "source_components": sources, "tasks": task_ids, "anchor_ids": anchors})
    return sorted(result, key=lambda row: row["id"])


def normalize_spec(value, *, source, requirements, tasks, allow_unresolved: bool = False) -> dict:
    """Validate evidence links without inventing missing source or review evidence."""
    _object(value, {"version", "anchors", "scope", "prerequisites", "arguments"}, "spec")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported autoformalize spec version; re-chunk with source evidence")
    tasks = _task_map(tasks)
    refs = {row["ref_id"] for row in source.get("source_refs", [])}
    if not refs:
        raise ValueError("spec requires the immutable supplied-source references")
    requirements = normalize_requirements(requirements, tasks, refs)
    ledger = {row["id"]: row for row in requirements}
    anchors = {}
    for row in _rows(value["anchors"], "anchors"):
        _object(row, {"id", "source_ref", "location", "excerpt"}, "anchor")
        item = {key: _text(row[key], f"anchor {key}") for key in row}
        if item["id"] in anchors:
            raise ValueError("anchors have duplicate ids")
        _known([item["source_ref"]], refs, "anchor source_ref")
        anchors[item["id"]] = item
    requirement_anchors = set()
    for row in requirements:
        _known(row["anchor_ids"], anchors, "requirement anchor_ids")
        if {anchors[key]["source_ref"] for key in row["anchor_ids"]} != set(row["source_components"]):
            raise ValueError(f"requirement {row['id']} anchors do not match its source components")
        requirement_anchors.update(row["anchor_ids"])
    scope = _object(value["scope"], {"targets", "references", "excluded"}, "scope")
    targets = _refs(scope["targets"], "scope targets")
    _known(targets, anchors, "scope targets")
    if set(targets) - requirement_anchors:
        raise ValueError("every scope target anchor must be covered by a requirement")
    if any(not set(row["anchor_ids"]).intersection(targets) for row in requirements):
        raise ValueError("every requirement must map at least one scope target anchor")
    references = _refs(scope["references"], "scope references", nonempty=False)
    _known(references, anchors, "scope references")
    exclusions, excluded = [], set()
    for row in _rows(scope["excluded"], "scope excluded"):
        _object(row, {"anchor_ids", "reason"}, "scope exclusion")
        ids = _refs(row["anchor_ids"], "excluded anchor_ids")
        _known(ids, anchors, "excluded anchor_ids")
        if excluded.intersection(ids):
            raise ValueError("scope exclusions duplicate anchors")
        excluded.update(ids)
        exclusions.append({"anchor_ids": ids, "reason": _text(row["reason"], "exclusion reason")})
    if set(targets).intersection(references) or excluded.intersection(set(targets) | set(references)):
        raise ValueError("scope target/reference/excluded anchors must be disjoint")
    if requirement_anchors - set(targets) - set(references):
        raise ValueError("requirement anchors must be in scope targets or references")
    if set(targets) | set(references) | excluded != set(anchors):
        raise ValueError("scope must account for every declared source anchor")
    if {row["source_ref"] for row in anchors.values()} != refs:
        raise ValueError("scope anchors must account for every supplied document")
    prerequisites = {}
    for row in _rows(value["prerequisites"], "prerequisites"):
        _object(row, {"id", "statement", "anchor_ids", "needed_by", "resolution"}, "prerequisite")
        identifier = _text(row["id"], "prerequisite id")
        if identifier in prerequisites:
            raise ValueError("prerequisites have duplicate ids")
        ids = _refs(row["anchor_ids"], "prerequisite anchor_ids")
        _known(ids, anchors, "prerequisite anchor_ids")
        _known(ids, set(targets) | set(references), "prerequisite in-scope/reference anchors")
        needed = _refs(row["needed_by"], "prerequisite needed_by")
        _known(needed, tasks, "prerequisite needed_by")
        resolution = row["resolution"]
        kind = resolution.get("kind") if isinstance(resolution, dict) else None
        field = {"library": "declaration", "task": "task_id", "unresolved": "issue_id"}.get(kind)
        if field is None:
            raise ValueError("prerequisite resolution must be library, task, or unresolved")
        _object(resolution, {"kind", field}, "prerequisite resolution")
        resolution = {"kind": kind, field: _text(resolution[field], f"prerequisite {field}")}
        if kind == "task":
            _known([resolution[field]], tasks, "prerequisite task")
            for task_id in needed:
                if resolution[field] == task_id or resolution[field] not in tasks[task_id].get("dependencies", []):
                    raise ValueError("task prerequisite requires a direct consumer dependency edge")
        if kind == "unresolved" and not allow_unresolved:
            raise ValueError("unresolved prerequisite needs exploration or a source repair before freezing")
        prerequisites[identifier] = {"id": identifier, "statement": _text(row["statement"], "prerequisite statement"),
                                     "anchor_ids": ids, "needed_by": needed, "resolution": resolution}
    arguments, used_prerequisites = {}, set()
    for row in _rows(value["arguments"], "arguments"):
        _object(row, {"requirement_id", "anchor_ids", "outline", "prerequisites", "repair_ids"}, "argument mapping")
        identifier = _text(row["requirement_id"], "argument requirement_id")
        _known([identifier], ledger, "argument requirement_id")
        if identifier in arguments:
            raise ValueError("arguments have duplicate requirement mappings")
        ids = _refs(row["anchor_ids"], "argument anchor_ids")
        _known(ids, ledger[identifier]["anchor_ids"], "argument anchor_ids")
        prereqs = _refs(row["prerequisites"], "argument prerequisites", nonempty=False)
        _known(prereqs, prerequisites, "argument prerequisites")
        for prerequisite in prereqs:
            if not set(prerequisites[prerequisite]["needed_by"]).intersection(ledger[identifier]["tasks"]):
                raise ValueError("argument prerequisite is unrelated to its implementing tasks")
        used_prerequisites.update(prereqs)
        arguments[identifier] = {"requirement_id": identifier, "anchor_ids": ids,
                                 "outline": _text(row["outline"], "argument outline"), "prerequisites": prereqs,
                                 "repair_ids": _refs(row["repair_ids"], "argument repair_ids", nonempty=False)}
    if set(arguments) != set(ledger):
        raise ValueError("arguments must exactly cover the requirements ledger")
    if used_prerequisites != set(prerequisites):
        raise ValueError("every prerequisite must be used by an argument mapping")
    return {"version": 1, "anchors": [anchors[key] for key in sorted(anchors)],
            "scope": {"targets": targets, "references": references,
                      "excluded": sorted(exclusions, key=lambda row: row["anchor_ids"])},
            "prerequisites": [prerequisites[key] for key in sorted(prerequisites)],
            "arguments": [arguments[key] for key in sorted(arguments)]}


def library_declarations(spec: dict) -> list[str]:
    return sorted({row["resolution"]["declaration"] for row in spec["prerequisites"]
                   if row["resolution"]["kind"] == "library"})


def task_spec_hash(task: dict, requirements: list[dict], spec: dict, contract: dict) -> str:
    """Hash task-local meaning/evidence; callers propagate dependency invalidation."""
    task_id = task.get("task_id", task.get("id"))
    rows = [row for row in requirements if task_id in row["tasks"]]
    ids = {row["id"] for row in rows}
    arguments = [row for row in spec["arguments"] if row["requirement_id"] in ids]
    prereq_ids = {key for row in arguments for key in row["prerequisites"]}
    prerequisites = [row for row in spec["prerequisites"]
                     if row["id"] in prereq_ids or task_id in row["needed_by"]]
    anchor_ids = {key for row in [*rows, *arguments, *prerequisites] for key in row["anchor_ids"]}
    global_ids = set(spec["scope"]["references"]) | {
        key for row in spec["scope"]["excluded"] for key in row["anchor_ids"]
    }
    externals = {row["resolution"]["declaration"] for row in prerequisites if row["resolution"]["kind"] == "library"}
    return digest({
        "version": 1, "task": {"id": task_id, "lean_decl": task["lean_decl"],
            "lean_file": task["lean_file"], "dependencies": sorted(task.get("dependencies", [])),
            "source_components": sorted(task.get("source_components", [])),
            "title": task.get("title") or task_id,
            "description": task.get("description") or task.get("summary") or ""},
        "requirements": rows, "arguments": arguments, "prerequisites": prerequisites,
        "anchors": [row for row in spec["anchors"] if row["id"] in anchor_ids | global_ids],
        "scope": {"targets": sorted(set(spec["scope"]["targets"]).intersection(anchor_ids)),
                  "references": spec["scope"]["references"], "excluded": spec["scope"]["excluded"]},
        "target": contract["targets"][task["lean_decl"]],
        "external_declarations": {key: contract.get("external_declarations", {}).get(key) for key in sorted(externals)},
        "solution_sha256": contract["solution_sha256"], "environment": contract["environment"],
    })
