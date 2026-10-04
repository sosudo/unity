"""Compiler errors become independently scheduled declaration repair tasks.

The immutable original obligation universe is larger than the active repair
queue. A file is never a declaration task merely because it failed to compile.
"""
from __future__ import annotations

from copy import deepcopy

from .bump_inventory import digest, validate_index
from .bump_diagnostics import occurrence_ids_at


def _components(rows: dict) -> list[list[str]]:
    """Original mutual declarations are the only mandatory declaration groups."""
    # Iterative traversal does not turn a long declaration chain into a Python
    # recursion failure before model dispatch.
    visited, order = set(), []
    reverse = {key: [] for key in rows}
    for key, row in rows.items():
        for dependency in row["dependencies"]:
            reverse[dependency].append(key)
    for root in sorted(rows):
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(rows[root]["dependencies"]))]
        while stack:
            key, children = stack[-1]
            try:
                child = next(children)
            except StopIteration:
                order.append(key)
                stack.pop()
                continue
            if child not in visited:
                visited.add(child)
                stack.append((child, iter(rows[child]["dependencies"])))
    assigned, result = set(), []
    for root in reversed(order):
        if root in assigned:
            continue
        assigned.add(root)
        members, pending = [], [root]
        while pending:
            key = pending.pop()
            members.append(key)
            for parent in reverse[key]:
                if parent not in assigned:
                    assigned.add(parent)
                    pending.append(parent)
        result.append(sorted(members))
    return result


def declaration_group_kind(occurrences: dict, ids: list[str]) -> str:
    """Classify actual source declarations, never arbitrary same-file work.

    Multiple raw kernel occurrences may come from one exact source declaration.
    Unranged generated members need structural dependency connections to that
    native-ranged family; sharing a file or overlapping lines is insufficient.
    """
    members = set(ids)
    if not members or len(members) != len(ids) or not members <= occurrences.keys():
        raise ValueError("declaration group requires distinct original occurrences")
    if len(members) == 1:
        return "declaration"
    components = _components(occurrences)
    if any(members == set(component) for component in components):
        return "mutual"
    ranged = [occurrences[key] for key in members if occurrences[key].get("range")]
    if (not ranged or len({occurrences[key]["path"] for key in members}) != 1
            or any(row["range"] != ranged[0]["range"] for row in ranged)):
        raise ValueError("unrelated declarations require separate assignments")
    if len(ranged) == len(members):
        return "declaration"
    neighbors = {key: set(occurrences[key]["dependencies"]) & members for key in members}
    for key in members:
        for dependency in tuple(neighbors[key]):
            neighbors[dependency].add(key)
    reached = {key for key in members if occurrences[key].get("range")}
    pending = list(reached)
    while pending:
        key = pending.pop()
        new = neighbors[key] - reached
        reached.update(new)
        pending.extend(new)
    if reached != members:
        raise ValueError("unranged generated occurrences must connect structurally to their declaration owner")
    return "declaration"


def empty_module_commands(index: dict) -> dict:
    """Static command obligations for exactly the native zero-declaration modules."""
    return {"source-command-" + digest([module, row["path"], row["source_sha256"]]):
            {"module": module, "path": row["path"], "source_sha256": row["source_sha256"]}
            for module, row in sorted(index["modules"].items()) if not row["occurrence_ids"]}


def plan_repairs(index: dict, diagnostics: dict, source: dict, prior_state: dict | None = None) -> dict:
    validate_index(index)
    occurrences = index["occurrences"]
    commands = empty_module_commands(index)
    command_by_module = {row["module"]: key for key, row in commands.items()}
    components = _components(occurrences)
    by_occurrence = {key: members for members in components for key in members}
    refs = {row["path"].removeprefix(".unity/source/"): row["ref_id"] for row in source["source_refs"]}
    by_file = {entry["path"]: module for module, entry in index["modules"].items()}
    errors = [row for row in diagnostics["diagnostics"] if row["severity"] == "error"]
    if diagnostics.get("unmapped_error_count") or any(row["path"] not in by_file for row in errors):
        raise ValueError("target build has an unlocated or out-of-scope failure; compiler log is preserved")
    # Missing local imports are consequences of failing prerequisites. They do
    # not invent repairs for every declaration in every downstream module.
    failing_modules = {by_file[row["path"]] for row in errors if row["kind"] != "import"}
    imports = diagnostics.get("current_imports", {module: row["imports"] for module, row in index["modules"].items()})
    blocked_modules = set(diagnostics.get("blocked_modules", []))
    for module in index["modules"]:
        seen, pending = set(), list(imports.get(module, []))
        while pending:
            dependency = pending.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            pending.extend(imports.get(dependency, []))
        if seen & failing_modules:
            blocked_modules.add(module)
    prior = (prior_state or {}).get("formal_tasks", {})
    nodes = {key: deepcopy(row) for key, row in prior.items() if row.get("migration")}
    existing_owners = {}
    for task_id, node in nodes.items():
        for original_id in node["migration"]["original_ids"]:
            existing_owners.setdefault(original_id, set()).add(task_id)
    active, grouped = set(), {}
    active.update(key for key, node in nodes.items() if node["migration"].get("critic_reopen")
                  or node.get("faithfulness", {}).get("status") == "changes_requested")
    active.update(key for key, node in nodes.items() if node.get("status") != "complete"
                  and node["migration"]["module"] in blocked_modules)
    for error in errors:
        module, path = by_file[error["path"]], error["path"]
        if error["kind"] == "import" and failing_modules:
            dependencies = set(imports.get(module, []))
            pending = list(dependencies)
            while pending:
                imported = pending.pop()
                for dependency in imports.get(imported, []):
                    if dependency not in dependencies:
                        dependencies.add(dependency)
                        pending.append(dependency)
            if dependencies & failing_modules:
                continue
        direct = error.get("occurrence_ids") or occurrence_ids_at(
            index, path, error.get("original_line", error["line"]), error.get("column", 0))
        members = sorted({key for item in direct for key in by_occurrence[item]})
        replacements = sorted({owner for original_id in members for owner in existing_owners.get(original_id, ())})
        if replacements:
            # Refinement owns these original identities now. Compiler refresh
            # cannot recreate the retired parent or lose its replacement work.
            for task_id in replacements:
                previous = nodes[task_id]["migration"]
                active.add(task_id)
                grouped.setdefault(task_id, {"members": list(previous["original_ids"]),
                    "module": previous["module"], "path": previous["path"], "kind": previous["kind"],
                    "diagnostics": [], "line": error.get("original_line", error["line"])})["diagnostics"].append(error)
            continue
        if members:
            task_id = members[0] if len(members) == 1 else "decl-" + digest(members)
            kind = declaration_group_kind(occurrences, members)
        else:
            # An import/syntax command has an explicit location and its own
            # assignment. It is not a module-wide declaration repair request.
            location = error.get("original_line", error["line"])
            lengths = index["modules"][module].get("line_lengths", [])
            if type(location) is not int or not 1 <= location <= len(lengths):
                raise ValueError("source-command diagnostic lies outside the sealed original source lines")
            task_id = "command-" + digest([path, location])
            kind = "command"
        active.add(task_id)
        grouped.setdefault(task_id, {"members": members, "module": module, "path": path,
                                    "kind": kind, "diagnostics": [], "line": error.get("original_line", error["line"])})["diagnostics"].append(error)
    owner = {key: task_id for task_id, row in grouped.items() for key in row["members"]}
    for task_id, row in nodes.items():
        for key in row["migration"]["original_ids"]:
            owner.setdefault(key, task_id)
    for task_id, group in grouped.items():
        ids, module, path = group["members"], group["module"], group["path"]
        governed = ids or index["modules"][module]["occurrence_ids"]
        command_obligation = command_by_module.get(module) if not ids else None
        if not governed and not command_obligation:
            raise ValueError("source-command repair has no original declaration obligation; retained for explicit planning")
        obligations = governed or [command_obligation]
        names = [occurrences[key]["display_name"] for key in ids]
        title = ", ".join(names) if names else f"Source command at {path}:{group['line']}"
        if task_id not in nodes:
            nodes[task_id] = {"id": task_id, "task_id": task_id, "title": title,
                "predicted_kind": occurrences[ids[0]]["kind"] if len(ids) == 1 else group["kind"],
                "source_components": [refs["project/" + path]],
                "anchor_ids": ["anchor-" + key for key in obligations],
                "requirement_ids": ["requirement-" + key for key in obligations],
                "informal_statement": ("Migrate the original declaration " + title + "; preserve its original meaning and trust."
                    if ids else f"Repair the source command at {path}:{group['line']} while preserving original declarations."),
                "informal_proof": None, "statement_dependencies": [], "proof_dependencies": [],
                "proposed_formal_statement": None, "proposed_formal_strategy": None,
                "lean_file": path, "lean_decl": names[0] if len(names) == 1 else ""}
        dependencies = set(nodes[task_id].get("dependencies", [])) if task_id in prior else set()
        pending, seen = [dependency for key in ids for dependency in occurrences[key]["dependencies"]], set(ids)
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            if key in owner and owner[key] != task_id:
                dependencies.add(owner[key])
            else:
                pending.extend(occurrences[key]["dependencies"])
        node = nodes[task_id]
        node["proof_dependencies"] = sorted(dependencies)
        node["dependencies"] = sorted(set(node.get("statement_dependencies", [])) | dependencies)
        node["migration"] = {**node.get("migration", {}), "original_ids": ids, "module": module, "path": path, "kind": group["kind"],
            "original_ranges": [occurrences[key].get("range") for key in ids],
            "diagnostic_ids": [error["id"] for error in group["diagnostics"]],
            "diagnostics": deepcopy(group["diagnostics"]), "command_line": group["line"] if not ids else None}
        if not ids:
            line = group["line"]
            node["migration"]["original_ranges"] = [{"start_line": line, "start_column": 0,
                "end_line": line, "end_column": index["modules"][module]["line_lengths"][line - 1]}]
            if command_obligation:
                node["migration"]["command_obligation"] = command_obligation
    # Requirements describe the entire original universe even when a declaration
    # compiles unchanged and has no repair task. Refinement changes ownership only.
    anchors, requirements, arguments = [], [], []
    used_refs = set()
    for key, row in sorted(occurrences.items()):
        ref = refs["project/" + row["path"]]
        used_refs.add(ref)
        anchor = "anchor-" + key
        anchors.append({"id": anchor, "source_ref": ref, "location": json_location(row),
                        "excerpt": row["display_name"]})
        tasks = sorted(task_id for task_id, node in nodes.items() if "requirement-" + key in node["requirement_ids"])
        requirement = "requirement-" + key
        requirements.append({"id": requirement, "statement": "Preserve original " + row["kind"] + " " + row["display_name"]
            + " in " + row["module"] + ", including its behavior and inherited trust boundary.",
            "source_components": [ref], "tasks": tasks, "anchor_ids": [anchor]})
        arguments.append({"requirement_id": requirement, "anchor_ids": [anchor],
                          "outline": "Compare the bumped declaration and explicit refinements against this immutable original occurrence.",
                          "prerequisites": [], "repair_ids": []})
    for key, row in commands.items():
        ref = refs["project/" + row["path"]]
        used_refs.add(ref)
        anchor = "anchor-" + key
        anchors.append({"id": anchor, "source_ref": ref, "location": row["path"],
                        "excerpt": "Original command-only module " + row["module"] + " at source SHA256 " + row["source_sha256"]})
        requirement = "requirement-" + key
        requirements.append({"id": requirement,
            "statement": "Preserve the import and source-command role of original command-only module " + row["module"]
                         + "; it has no original declarations, and repairs require a separately bounded command assignment.",
            "source_components": [ref], "tasks": sorted(task_id for task_id, node in nodes.items()
                if requirement in node["requirement_ids"]), "anchor_ids": [anchor]})
        arguments.append({"requirement_id": requirement, "anchor_ids": [anchor],
            "outline": "Review the original source commands against the selected build and native empty-module evidence; do not invent a declaration.",
            "prerequisites": [], "repair_ids": []})
    references = []
    for ref in source["source_refs"]:
        if ref["ref_id"] not in used_refs:
            key = "reference-" + digest(ref["ref_id"])
            anchors.append({"id": key, "source_ref": ref["ref_id"], "location": ref["path"],
                            "excerpt": "Immutable migration input " + ref["path"]})
            references.append(key)
    return {"version": 1, "solution_candidate": source["candidate_id"], "solution_sha256": source["sha256"],
        "chunks": [nodes[key] for key in sorted(nodes)], "compiler_tasks": sorted(active),
        "requirements": requirements, "spec": {"version": 1, "anchors": anchors,
            "scope": {"targets": ["anchor-" + key for key in [*sorted(occurrences), *commands]], "references": references, "excluded": []},
            "prerequisites": [], "arguments": arguments}, "diagnostics_source_sha256": diagnostics["source_sha256"]}


def json_location(row: dict) -> str:
    span = row.get("range")
    return row["path"] + (f":{span['start_line']}:{span['start_column']}" if span else " (generated occurrence)")
