"""Deterministic error-driven Bump planning; module groups are promotion units.

Original occurrence obligations never disappear when diagnostics disappear.
Declaration chunks are work labels, not permission to compile truncated source
or to merge a partial module. Module groups own each file exclusively.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .bump_inventory import digest, validate_index
from .bump_diagnostics import (validate_diagnostics, scheduling_graph,
                               validate_target_imports, diagnostic_content_key, _require_acyclic)


def _group_contexts(index, source_hashes, environment_sha256, scheduling, target_imports, tasks, content_keys):
    """Stable local work inputs; no global revision, log offsets, or artifact IDs."""
    modules = index["modules"]
    if (not isinstance(source_hashes, dict) or set(source_hashes) != set(modules)
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in source_hashes.values())):
        raise ValueError("missing exact per-module source inputs")
    contexts = {}
    for module in sorted(modules):
        closure, pending = set(), [module]
        while pending:
            name = pending.pop()
            if name not in closure:
                closure.add(name)
                pending.extend(scheduling["dependencies"][name])
        task = tasks.get(module, {})
        subtasks = task.get("declaration_subtasks", [])
        identities = {row["id"]: digest([row["kind"], sorted(row["original_ids"])]) for row in subtasks}
        value = {"version": 1, "original_index_sha256": index["index_sha256"],
            "environment_sha256": environment_sha256, "module": module,
            "own_source_sha256": source_hashes[module],
            "import_closure": [{"module": name, "source_sha256": source_hashes[name],
                "imports": scheduling["dependencies"][name],
                "provenance": scheduling["dependency_provenance"][name],
                "header_status": target_imports["modules"][name]["status"]} for name in sorted(closure)],
            "diagnostic_content_keys": sorted(content_keys[key] for key in task.get("diagnostic_ids", [])),
            "subtasks": sorted([{"kind": row["kind"], "original_ids": sorted(row["original_ids"]),
                "dependencies": sorted(identities[key] for key in row["dependencies"]),
                "diagnostic_content_keys": sorted(content_keys[key] for key in row["diagnostic_ids"])}
                for row in subtasks], key=digest),
            "routing": {"status": task.get("status", "compiled"),
                        "header_repair_only": task.get("header_repair_only", False)}}
        contexts[module] = {**value, "sha256": digest(value)}
    return contexts


def _components(nodes: list[str], edges: dict[str, list[str]]) -> list[list[str]]:
    """Iterative SCC decomposition (large generated inventories need no recursion)."""
    allowed, visited, order = set(nodes), set(), []
    for start in sorted(nodes):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(edges.get(start, [])))]
        while stack:
            node, pending = stack[-1]
            child = next(pending, None)
            if child is None:
                order.append(node)
                stack.pop()
            elif child in allowed and child not in visited:
                visited.add(child)
                stack.append((child, iter(edges.get(child, []))))
    reverse = {node: [] for node in nodes}
    for node in nodes:
        for child in edges.get(node, []):
            if child in allowed:
                reverse[child].append(node)
    visited, result = set(), []
    for start in reversed(order):
        if start in visited:
            continue
        component, pending = [], [start]
        visited.add(start)
        while pending:
            node = pending.pop()
            component.append(node)
            for child in reverse[node]:
                if child not in visited:
                    visited.add(child)
                    pending.append(child)
        result.append(sorted(component))
    return sorted(result)


def _covers(row: dict, diagnostic: dict) -> bool:
    bounds = row["range"]
    line, column = diagnostic.get("line"), diagnostic.get("column")
    if bounds is None or type(line) is not int or type(column) is not int:
        return False
    return ((bounds["start_line"], bounds["start_column"]) <= (line, column)
            < (bounds["end_line"], bounds["end_column"]))


def plan_repairs(index: dict, diagnostics: dict, prior_plan: dict | None = None) -> dict:
    validate_index(index)
    validate_diagnostics(diagnostics)
    if diagnostics["original_index_sha256"] != index["index_sha256"]:
        raise ValueError("diagnostics belong to a different original index")
    if prior_plan is not None:
        validate_repair_plan(index, prior_plan)
    modules, occurrences = index["modules"], index["occurrences"]
    scheduling = scheduling_graph(index, diagnostics["target_imports"])
    dependencies = scheduling["dependencies"]
    compiled = set(diagnostics["compiled_modules"])
    if compiled - set(modules) or set(diagnostics["modules"]) - set(modules):
        raise ValueError("diagnostic modules cross the original inventory")
    by_file = {row["path"]: module for module, row in modules.items()}
    errors = [row for row in diagnostics["diagnostics"] if row["severity"] == "error"]
    content_keys = {}
    for row in errors:
        if row.get("message_truncated") and "content_sha256" not in row:
            raise ValueError("truncated diagnostic lacks its full content identity")
        content_keys[row["id"]] = row.get("content_sha256", diagnostic_content_key(row, row["message"]))
    uncertain = {name for component in scheduling["unresolved_import_cycles"] for name in component}
    located = {module: [] for module in modules}
    unmapped = []
    for row in errors:
        module = by_file.get(row["path"])
        if module is None:
            unmapped.append(row["id"])
        else:
            located[module].append(row)
    if any(located[module] for module in compiled) or (diagnostics["passed"] and unmapped):
        raise ValueError("successful build contradicts error diagnostics")
    tasks, bindings = {}, {}
    for module, group in sorted(modules.items()):
        ids = group["occurrence_ids"]
        bindings[module] = {"modules": [module], "files": [group["path"]], "obligation_ids": list(ids)}
        if module in compiled:
            continue
        components = _components(ids, {key: occurrences[key]["dependencies"] for key in ids})
        membership, subtasks = {}, {}
        for component in components:
            subtask_id = "decl-" + digest([module, component])
            for key in component:
                membership[key] = subtask_id
            subtasks[subtask_id] = {"id": subtask_id, "kind": "declaration", "original_ids": component,
                "diagnostic_ids": [], "dependencies": []}
        for component in components:
            subtask = subtasks[membership[component[0]]]
            subtask["dependencies"] = sorted({membership[dep] for key in component
                for dep in occurrences[key]["dependencies"] if dep in membership and membership[dep] != subtask["id"]})
        for row in located[module]:
            # Old source positions are never silently reused after edits.
            unchanged = diagnostics.get("module_source_hashes", {}).get(module) == group["source_sha256"]
            hits = {membership[key] for key in ids if unchanged and _covers(occurrences[key], row)}
            if row["kind"] == "declaration" and len(hits) == 1:
                subtasks[next(iter(hits))]["diagnostic_ids"].append(row["id"])
            else:
                kind = row["kind"] if row["kind"] in {"syntax", "import"} else "module"
                subtask_id = "file-" + digest([module, kind])
                subtask = subtasks.setdefault(subtask_id, {"id": subtask_id, "kind": kind,
                    "original_ids": [], "diagnostic_ids": [], "dependencies": []})
                subtask["diagnostic_ids"].append(row["id"])
        blocked_by = sorted(set(dependencies[module]) - compiled)
        header_repair = bool(module in uncertain
            and scheduling["dependency_provenance"][module] == "original_fallback"
            and any(row["kind"] in {"syntax", "import"} for row in located[module]) and not unmapped)
        prior = (prior_plan or {}).get("tasks", {}).get(module)
        tasks[module] = {"task_id": module, "migration_module": module, "lean_file": group["path"],
            "dependencies": dependencies[module], "declaration_subtasks": [subtasks[key] for key in sorted(subtasks)],
            "diagnostic_ids": sorted(row["id"] for row in located[module]),
            "status": "repair" if header_repair else ("blocked" if blocked_by or unmapped else ("repair" if located[module] else "build_required")),
            "header_repair_only": header_repair,
            "blocked_by": blocked_by, "execution_unit": "module", "single_writer": True,
            "lineage": ([{"generation": prior_plan["generation"], "task_id": module,
                          "plan_sha256": prior_plan["plan_sha256"]}] if prior else [])}
    contexts = _group_contexts(index, diagnostics["module_source_hashes"], diagnostics["environment_sha256"],
                              scheduling, diagnostics["target_imports"], tasks, content_keys)
    for module, task in tasks.items():
        task["input_sha256"] = contexts[module]["sha256"]
    result = {"version": 2, "kind": "bump-diagnostic-plan-v2", "original_index_sha256": index["index_sha256"],
        "source_sha256": diagnostics["source_sha256"], "environment_sha256": diagnostics["environment_sha256"],
        "diagnostic_snapshot_sha256": diagnostics["snapshot_sha256"], "diagnostic_artifact": diagnostics["artifact_ref"],
        "generation": 1 if prior_plan is None else prior_plan["generation"] + 1,
        "dependencies": dependencies, "target_imports": diagnostics["target_imports"],
        "scheduling_policy": "current-native-imports-v1",
        "dependency_provenance": scheduling["dependency_provenance"],
        "unresolved_import_cycles": scheduling["unresolved_import_cycles"],
        "module_source_hashes": diagnostics["module_source_hashes"],
        "diagnostic_content_keys": content_keys, "group_contexts": contexts,
        "task_bindings": bindings, "tasks": tasks, "compiled_modules": sorted(compiled),
        "diagnostic_ids": sorted(row["id"] for row in errors), "unmapped_diagnostic_ids": sorted(unmapped)}
    result["plan_sha256"] = digest(result)
    validate_repair_plan(index, result)
    # A later diagnostic capture/refinement must not mutate an already sealed
    # prior plan through nested dictionaries shared with its input receipt.
    return deepcopy(result)


def validate_repair_plan(index: dict, plan: dict) -> None:
    validate_index(index)
    fields = {"version", "kind", "original_index_sha256", "source_sha256", "environment_sha256",
        "diagnostic_snapshot_sha256", "diagnostic_artifact", "generation", "dependencies", "task_bindings",
        "tasks", "compiled_modules", "diagnostic_ids", "unmapped_diagnostic_ids", "plan_sha256", "target_imports"}
    version = plan.get("version") if isinstance(plan, dict) else None
    if version == 2:
        fields.update({"scheduling_policy", "dependency_provenance", "unresolved_import_cycles", "module_source_hashes",
                       "diagnostic_content_keys", "group_contexts"})
    if (not isinstance(plan, dict) or set(plan) != fields or version not in {1, 2}
            or plan.get("kind") != f"bump-diagnostic-plan-v{version}"
            or plan.get("original_index_sha256") != index["index_sha256"]
            or plan.get("plan_sha256") != digest({k: v for k, v in plan.items() if k != "plan_sha256"})
            or type(plan.get("generation")) is not int or plan["generation"] < 1):
        raise ValueError("invalid repair plan binding")
    modules = index["modules"]
    if version == 1:
        # Read old plans under their old conservative rule, never reinterpret an
        # already sealed union graph as current-only evidence on continuation.
        validate_target_imports(plan.get("target_imports"), index)
        dependencies = {name: sorted(set(row["imports"]) | set(plan["target_imports"]["modules"][name]["imports"]))
                        for name, row in modules.items()}
        _require_acyclic(dependencies)
        scheduling = None
    else:
        scheduling = scheduling_graph(index, plan.get("target_imports"))
        dependencies = scheduling["dependencies"]
        if (plan.get("scheduling_policy") != "current-native-imports-v1"
                or any(plan.get(key) != scheduling[key] for key in ("dependency_provenance", "unresolved_import_cycles"))):
            raise ValueError("repair plan changes native scheduling provenance")
    if (plan.get("dependencies") != dependencies
            or any(plan["target_imports"][field] != plan[field] for field in (
                "source_sha256", "environment_sha256", "original_index_sha256"))):
        raise ValueError("repair plan changes source-bound module prerequisites")
    bindings, tasks, compiled = plan.get("task_bindings"), plan.get("tasks"), plan.get("compiled_modules")
    if (not isinstance(bindings, dict) or set(bindings) != set(modules) or not isinstance(tasks, dict)
            or not isinstance(compiled, list) or len(set(compiled)) != len(compiled)
            or set(tasks) & set(compiled) or set(tasks) | set(compiled) != set(modules)):
        raise ValueError("repair plan does not cover every original module")
    diagnostic_ids = plan.get("diagnostic_ids")
    unmapped = plan.get("unmapped_diagnostic_ids")
    if (not isinstance(diagnostic_ids, list) or len(diagnostic_ids) != len(set(diagnostic_ids))
            or not isinstance(unmapped, list) or len(unmapped) != len(set(unmapped))
            or set(unmapped) - set(diagnostic_ids)):
        raise ValueError("invalid diagnostic partition")
    assigned_diagnostics = set(unmapped)
    if version == 2:
        keys = plan.get("diagnostic_content_keys")
        if (not isinstance(keys, dict) or set(keys) != set(diagnostic_ids)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in keys.values())):
            raise ValueError("diagnostic content identities are incomplete")
    for module, original in modules.items():
        if bindings[module] != {"modules": [module], "files": [original["path"]], "obligation_ids": original["occurrence_ids"]}:
            raise ValueError("task binding drops, moves or duplicates original obligations")
        if module not in tasks:
            continue
        task = tasks[module]
        if (not isinstance(task, dict) or task.get("task_id") != module or task.get("migration_module") != module
                or task.get("lean_file") != original["path"] or task.get("dependencies") != dependencies[module]
                or task.get("execution_unit") != "module" or task.get("single_writer") is not True
                or task.get("blocked_by") != sorted(set(dependencies[module]) - set(compiled))
                or task.get("status") not in {"repair", "blocked", "build_required"}
                or not isinstance(task.get("declaration_subtasks"), list)
                or not isinstance(task.get("diagnostic_ids"), list)
                or len(task["diagnostic_ids"]) != len(set(task["diagnostic_ids"]))):
            raise ValueError("invalid module execution group")
        header_repair = task.get("header_repair_only", False) if version == 2 else False
        if version == 2:
            uncertain = {name for component in scheduling["unresolved_import_cycles"] for name in component}
            eligible_header = bool(module in uncertain
                and scheduling["dependency_provenance"][module] == "original_fallback"
                and any(row.get("kind") in {"syntax", "import"} and row.get("diagnostic_ids")
                        for row in task["declaration_subtasks"]) and not unmapped)
            if type(header_repair) is not bool or header_repair != eligible_header:
                raise ValueError("header repair is not bound to unresolved native import evidence")
            if header_repair and task["status"] != "repair":
                raise ValueError("unresolved header repair must retain repair routing")
        if ((task["blocked_by"] or unmapped) and not header_repair and task["status"] != "blocked"):
            raise ValueError("blocked prerequisites cannot be scheduled")
        if set(task["diagnostic_ids"]) & assigned_diagnostics:
            raise ValueError("diagnostic assigned to more than one module")
        assigned_diagnostics.update(task["diagnostic_ids"])
        owned, errors, names = set(), set(), set()
        for subtask in task["declaration_subtasks"]:
            if (not isinstance(subtask, dict) or set(subtask) != {"id", "kind", "original_ids", "diagnostic_ids", "dependencies"}
                    or not isinstance(subtask["id"], str) or subtask["id"] in names
                    or subtask["kind"] not in {"declaration", "module", "syntax", "import"}
                    or any(not isinstance(subtask[key], list) or len(subtask[key]) != len(set(subtask[key]))
                           for key in ("original_ids", "diagnostic_ids", "dependencies"))
                    or set(subtask["original_ids"]) & owned or set(subtask["diagnostic_ids"]) & errors):
                raise ValueError("invalid declaration subtask partition")
            names.add(subtask["id"])
            owned.update(subtask["original_ids"])
            errors.update(subtask["diagnostic_ids"])
        if owned != set(original["occurrence_ids"]) or errors != set(task["diagnostic_ids"]):
            raise ValueError("subtasks drop original IDs or current diagnostics")
        for subtask in task["declaration_subtasks"]:
            if set(subtask["dependencies"]) - names or subtask["id"] in subtask["dependencies"]:
                raise ValueError("subtask dependency crosses the module group")
        assignment = {key: row["id"] for row in task["declaration_subtasks"] for key in row["original_ids"]}
        for subtask in task["declaration_subtasks"]:
            required = {assignment[dep] for key in subtask["original_ids"]
                        for dep in index["occurrences"][key]["dependencies"]
                        if dep in assignment and assignment[dep] != subtask["id"]}
            if required - set(subtask["dependencies"]):
                raise ValueError("subtask refinement drops an original local dependency")
        graph = {row["id"]: row["dependencies"] for row in task["declaration_subtasks"]}
        if any(len(component) > 1 for component in _components(sorted(graph), graph)):
            raise ValueError("mutually dependent declarations must remain one subtask group")
    if assigned_diagnostics != set(diagnostic_ids):
        raise ValueError("repair plan drops current error diagnostics")
    if version == 2:
        contexts = _group_contexts(index, plan["module_source_hashes"], plan["environment_sha256"],
                                  scheduling, plan["target_imports"], tasks, plan["diagnostic_content_keys"])
        if (plan["group_contexts"] != contexts
                or any(task.get("input_sha256") != contexts[module]["sha256"] for module, task in tasks.items())):
            raise ValueError("repair plan group inputs differ from current scoped evidence")
