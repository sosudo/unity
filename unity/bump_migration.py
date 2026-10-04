"""Migration checks around the copied Formalize orchestration.

A declaration repair receipt records checked compiler progress. It is not a
kernel certificate. Acceptance always builds and compares the complete original
declaration universe, followed by the inherited independent semantic review.
"""
from __future__ import annotations

from copy import deepcopy
import difflib
import hashlib
import json
from pathlib import Path
import re
import uuid

from .bump_spec import digest

POLICY = "declaration-migration-v1"


def is_migration(value: dict | None) -> bool:
    value = value or {}
    return value.get("policy") == "migration-v1" or value.get("migration_policy") == 1


def seal(value: dict) -> dict:
    value = deepcopy(value)
    value.pop("sha256", None)
    return {**value, "sha256": digest(value)}


def _safe_file(root: Path, name: str) -> Path:
    from .bump_files import normalize_paths
    normalize_paths([name])
    path = root / name
    if any(p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root)):
        raise ValueError("migration input became a symlink: " + name)
    return path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_baseline(root: Path, *, migration: dict, original_index: dict) -> dict:
    from . import bump_contract as contract, bump_project as project
    root = Path(root).resolve()
    migration = deepcopy(migration)
    migration["original_index"] = deepcopy(original_index)
    layout = workspace_layout(root, migration)
    baseline = seal({
        "version": 3, "policy": "migration-v1", "project_root": str(root),
        "project_scope": migration["scope"]["mode"],
        "branch": project._git(root, "branch", "--show-current"),
        "head": project._git(root, "rev-parse", "HEAD"),
        "files": migration["original_files"], "tracked_files": sorted(migration["original_files"]),
        "environment": migration["target_environment"], "layout": layout,
        "target_scope": "Preserve all selected original Lean declarations during the version upgrade.",
        "scope": {"mode": "migration", "bound": True, "existing_targets": sorted(original_index["occurrences"])},
        "migration": migration,
    })
    issues = baseline_errors(baseline)
    if issues:
        raise ValueError("; ".join(issues))
    require_inputs(root, baseline)
    return baseline


def baseline_errors(baseline: dict) -> list[str]:
    try:
        if (baseline.get("version") != 3 or baseline.get("policy") != "migration-v1"
                or baseline.get("sha256") != seal(baseline)["sha256"]):
            return ["migration baseline checksum or policy mismatch"]
        migration = baseline["migration"]
        index = migration["original_index"]
        selected = migration["selected_modules"]
        from . import bump_inventory, bump_contract
        bump_inventory.validate_index(index)
        scope = migration["scope"]
        if (scope.get("sha256") != seal(scope)["sha256"]
                or scope.get("version") != 1
                or scope.get("mode") != baseline["project_scope"]
                or scope.get("selected_modules") != selected
                or scope.get("excluded_files") != migration["excluded_files"]
                or index.get("scope_sha256") != scope["sha256"]
                or index.get("environment") != migration["original_environment"]
                or index.get("source_sha256") != digest(migration["original_files"])
                or migration["excluded_files"] != {name: sha for name, sha in migration["original_files"].items()
                    if name not in selected.values() and name not in bump_contract._CONFIGS}
                or baseline["project_root"] != migration["target_root"]):
            return ["migration baseline scope, environment or index binding differs"]
        if (not isinstance(selected, dict) or not selected
                or baseline["files"] != migration["original_files"]
                or baseline["environment"] != migration["target_environment"]
                or baseline["project_scope"] not in {"build", "all"}
                or baseline["scope"] != {"mode": "migration", "bound": True,
                    "existing_targets": sorted(index["occurrences"])}
                or set(index["modules"]) != set(selected)):
            return ["migration baseline does not cover the sealed original universe"]
        from .bump_files import normalize_paths
        normalize_paths(list(migration["original_files"]))
        normalize_paths(list(selected.values()))
        for module, path in selected.items():
            if path not in migration["original_files"] or index["modules"][module]["path"] != path:
                return ["migration index module ownership differs from selected source"]
        for key, row in index["occurrences"].items():
            if row["module"] not in selected or row["path"] != selected[row["module"]]:
                return ["migration occurrence escapes the sealed source scope"]
        if not all(isinstance(migration[k], str) and Path(migration[k]).is_absolute()
                   for k in ("source_root", "original_root", "target_root")):
            return ["migration workspace identities are incomplete"]
    except (KeyError, TypeError, ValueError, AttributeError):
        return ["migration baseline is malformed"]
    return []


def workspace_layout(root: Path, migration: dict) -> dict:
    from . import bump_workspace as workspace, bump_contract
    root = Path(root).resolve()
    selected = migration["selected_modules"]
    executable = workspace._executable(root)
    new_paths = {str(path.relative_to(root)) for path in bump_contract.source_files(
        root, build_dir=migration["scope"]["build_dir"])
        if path.suffix == ".lean" and str(path.relative_to(root)) not in migration["original_files"]}
    layout = workspace.discover(root, sorted(set(selected.values()) | new_paths), executable=executable)
    if layout.get("issues") or any(layout.get("modules", {}).get(path) != module
                                   for module, path in selected.items()) or new_paths - layout.get("modules", {}).keys():
        raise ValueError("native Lake ownership differs from the selected migration modules")
    return {**layout, "verification_modules": dict(layout["modules"]),
            "editable_modules": dict(layout["modules"]),
            "project_scope": migration["scope"]["mode"],
            "scope_sha256": migration["scope"]["sha256"]}


def require_inputs(root: Path, baseline: dict, *, allowed_new_paths=()) -> None:
    """Read-only source/config/dependency guard, before executing Lake code."""
    from . import bump_contract as contract
    errors = baseline_errors(baseline)
    if errors:
        raise ValueError("; ".join(errors))
    root = Path(root).resolve()
    migration = baseline["migration"]
    target_root = Path(migration["target_root"]).resolve()
    if root != target_root:
        # Only the controller's dedicated Bump worktrees may share this seal.
        if (root.parent != target_root / ".worktrees" or not root.name.startswith("bump-")
                or not (root / ".git").is_file()
                or contract._git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
                != contract._git(target_root, "rev-parse", "--path-format=absolute", "--git-common-dir")):
            raise ValueError("migration input is not the sealed target or an owned Bump worktree")
    writable = set(migration["selected_modules"].values())
    original = Path(migration["original_root"])
    source = Path(migration["source_root"])
    for preserved in (original, source):
        if contract._file_hashes(preserved, build_dir=migration["scope"]["build_dir"]) != migration["original_files"]:
            raise ValueError("original migration source changed (complete inventory): " + str(preserved))
    for name, expected in migration["original_files"].items():
        target = _safe_file(root, name)
        if not target.is_file():
            raise ValueError("original migration input was deleted: " + name)
        if name not in writable and name not in contract._CONFIGS and _hash(target) != expected:
            raise ValueError("excluded migration input changed: " + name)
    config = {name: _hash(_safe_file(root, name)) for name in contract._CONFIGS if (root / name).exists()}
    if config != baseline["environment"].get("config"):
        raise ValueError("target toolchain or dependency configuration changed after preparation")
    # Hash dependency source content without loading their Lake configuration.
    if contract._dependencies(root) != baseline["environment"].get("dependencies"):
        raise ValueError("target dependency sources changed after preparation")
    before = set(migration["original_files"])
    actual = contract._file_hashes(root, build_dir=baseline["layout"]["build_dir"])
    for name in actual.keys() - before:
        if not name.endswith(".lean") or name in contract._CONFIGS:
            raise ValueError("unexpected non-Lean migration input: " + name)


def coverage(baseline: dict) -> dict:
    migration = baseline["migration"]
    return {"mode": baseline["project_scope"], "policy": POLICY,
            "baseline_sha256": baseline["sha256"], "scope_sha256": migration["scope"]["sha256"],
            "verification_modules": {p: m for m, p in migration["selected_modules"].items()},
            "original_occurrences": sorted(migration["original_index"]["occurrences"]),
            "excluded_files": migration["excluded_files"], "normal_default_build": True}


def original_ids(contract: dict, task: dict) -> list[str]:
    metadata = task.get("migration", {})
    explicit = metadata.get("original_ids", metadata.get("original_occurrences"))
    if explicit is not None:
        return sorted(explicit)
    requirements = set(task.get("requirement_ids", []))
    index = contract["project_baseline"]["migration"]["original_index"]["occurrences"]
    return sorted(key for key in index if key in requirements or "preserve-" + key in requirements
                  or "requirement-" + key in requirements)


def require_original_obligations(baseline: dict, requirements: list[dict]) -> None:
    """Refinement changes task ownership, never the immutable source universe."""
    from .bump_planner import empty_module_commands
    inventory = baseline["migration"]["original_index"]
    index = set(inventory["occurrences"]) | set(empty_module_commands(inventory))
    by_id = {row["id"]: row for row in requirements}
    if len(by_id) != len(requirements) or set(by_id) != {"requirement-" + key for key in index}:
        raise ValueError("migration requirements must cover every original occurrence exactly once")
    for key in index:
        if by_id["requirement-" + key].get("anchor_ids") != ["anchor-" + key]:
            raise ValueError("migration requirement changed its immutable original occurrence anchor")


def refine(state: dict, changes: dict, *, previous_tasks: dict) -> None:
    """Carry original occurrence ownership through inherited explicit refinement."""
    contract = state["formalization"]["contract"]
    if not is_migration(contract):
        return
    require_original_obligations(contract["project_baseline"], state["formalization"]["requirements"])
    index = contract["project_baseline"]["migration"]["original_index"]["occurrences"]
    for key, task in state["formal_tasks"].items():
        if key in previous_tasks and previous_tasks[key].get("migration"):
            task["migration"] = deepcopy(previous_tasks[key]["migration"])
        else:
            ids = set(original_ids(contract, task))
            if not ids or not ids <= index.keys():
                raise ValueError("refined migration task must retain explicit original declaration obligations")
            paths = {index[item]["path"] for item in ids}
            modules = {index[item]["module"] for item in ids}
            if len(paths) != 1 or len(modules) != 1:
                raise ValueError("refine cross-file work into separate declaration tasks with dependency edges")
            parents = [row.get("migration", {}) for row in previous_tasks.values()
                       if ids.intersection(original_ids(contract, row))]
            from .bump_planner import declaration_group_kind
            task["migration"] = {"kind": declaration_group_kind(index, ids),
                "original_ids": sorted(ids), "path": next(iter(paths)), "module": next(iter(modules)),
                "original_ranges": [index[item]["range"] for item in sorted(ids) if index[item].get("range")],
                "diagnostic_ids": sorted({item for parent in parents for item in parent.get("diagnostic_ids", [])})}


def prepare_contract(paths, dag: dict, *, state: dict, environment=None, main_sha=None) -> dict:
    from . import bump_contract as contract, bump_state as runtime
    from .bump_spec import normalize_requirements, normalize_spec
    baseline = state["project_baseline"]
    errors = baseline_errors(baseline)
    if errors:
        raise ValueError("; ".join(errors))
    source = runtime.formal_source(state)
    if (dag.get("solution_candidate") != source["candidate_id"]
            or dag.get("solution_sha256") != source["sha256"]):
        raise ValueError("migration plan changed its immutable original input")
    requirements = normalize_requirements(dag["requirements"], dag["chunks"],
                                         {row["ref_id"] for row in source["source_refs"]})
    spec = normalize_spec(dag["spec"], source=source, requirements=requirements,
                          tasks=dag["chunks"], allow_unresolved=True)
    previous = state.get("formalization", {}).get("contract") or {}
    if previous and (previous.get("project_baseline") != baseline or not is_migration(previous)):
        raise ValueError("migration replanning changed the original baseline")
    require_inputs(paths.project_root, baseline)
    actual_environment = environment or contract.environment_identity(paths.project_root)
    if actual_environment != baseline["environment"]:
        raise ValueError("migration target environment changed")
    index = baseline["migration"]["original_index"]
    require_original_obligations(baseline, requirements)
    return contract._seal_contract({
        "version": 3, "migration_policy": 1, "fingerprint_version": 2,
        "solution_candidate": source["candidate_id"], "solution_sha256": source["sha256"],
        "requirements": requirements, "spec": spec, "spec_sha256": digest(spec),
        "project_baseline": baseline, "environment": actual_environment,
        "source_main_sha": main_sha or contract._git(paths.project_root, "rev-parse", "HEAD"),
        "obligation_ids": sorted(row.get("task_id", row.get("id")) for row in dag["chunks"]),
        "bindings": deepcopy(previous.get("bindings", {})),
        "targets": deepcopy(previous.get("targets", {})), "external_declarations": {},
        "prerequisite_declarations": {}, "adopted_outputs": contract.adopted_output_records(previous),
        "migration_correspondences": deepcopy(previous.get("migration_correspondences", {
            key: {"module": row["module"], "declaration": row["display_name"], "name_ast": row["name_ast"]}
            for key, row in index["occurrences"].items()})),
    })


def _name_key(value) -> tuple:
    if value == ["anonymous"]:
        return ("anonymous",)
    if not isinstance(value, list) or len(value) != 3 or value[0] not in {"str", "num"}:
        raise ValueError("malformed structural Lean name")
    if (value[0] == "str" and not isinstance(value[2], str)) or (
            value[0] == "num" and (type(value[2]) is not int or value[2] < 0)):
        raise ValueError("malformed structural Lean name component")
    return value[0], _name_key(value[1]), value[2]


def _rename(value, names: dict[tuple, list]):
    if isinstance(value, list):
        if value and value[0] in ("anonymous", "str", "num"):
            return deepcopy(names.get(_name_key(value), value))
        return [_rename(item, names) for item in value]
    if isinstance(value, dict):
        return {key: _rename(item, names) for key, item in value.items()}
    return value


def _rows(report: dict) -> dict[str, dict]:
    rows = report.get("declarations")
    if (report.get("mode") != "local-meanings" or report.get("schema_version") != 1
            or report.get("declaration_inventory") != "raw-module-constants-v1"
            or not isinstance(rows, list) or report.get("raw_declaration_count") != len(rows)):
        raise ValueError("incomplete native original-occurrence report")
    result, typed = {}, set()
    for row in rows:
        key = _name_key(row["name_ast"])
        display = row["display_name"]
        if key in typed or display in result:
            raise ValueError("ambiguous native declaration identity or display name")
        if (not isinstance(row.get("meaning"), dict) or not isinstance(row.get("axioms"), list)
                or row["meaning"].get("name") != row["name_ast"]
                or row["meaning"].get("kind") != row["kind"]):
            raise ValueError("native declaration meaning or trust evidence is incomplete")
        typed.add(key)
        result[display] = row
    return result


def _trust(row: dict, names: dict) -> set[str]:
    result = set()
    for axiom in row["axioms"]:
        if (not isinstance(axiom, dict) or not isinstance(axiom.get("reference"), dict)
                or axiom.get("kind") != "axiom" or not isinstance(axiom.get("type"), list)
                or not isinstance(axiom.get("level_params"), list)
                or type(axiom.get("unsafe")) is not bool):
            raise ValueError("native transitive assumption evidence is incomplete")
        reference = axiom["reference"]
        _name_key(reference["name_ast"])
        # Imported packages may move a declaration between modules. Exact
        # structural names, assumption types/universes and safety remain bound.
        result.add(digest(_rename({"name": reference["name_ast"],
            "type": axiom["type"], "level_params": axiom["level_params"], "unsafe": axiom["unsafe"]}, names)))
    return result


def compare_reports(original: dict, current: dict, correspondences: dict[str, str] | None = None,
                    *, renames: dict[tuple, list] | None = None) -> dict:
    """Compare local meanings and each declaration's transitive trust closure.

    Upgraded imported declarations are explicitly assumed compatible; their
    recursive definitions are not copied into or compared by this migration.
    """
    before, after = _rows(original), _rows(current)
    mapping = {name: (correspondences or {}).get(name, name) for name in before}
    if set(correspondences or {}) - set(before) or len(set(mapping.values())) != len(mapping):
        raise ValueError("declaration correspondence must be injective on original occurrences")
    names = dict(renames or {})
    for old, new in mapping.items():
        if new in after:
            if old == new and _name_key(before[old]["name_ast"]) != _name_key(after[new]["name_ast"]):
                raise ValueError("same display label cannot silently change structural declaration identity")
            names[_name_key(before[old]["name_ast"])] = after[new]["name_ast"]
    issues, verified = [], []
    for old, new in mapping.items():
        if new not in after:
            issues.append("original declaration is missing: " + old)
            continue
        left, right = before[old], after[new]
        if _rename(left["meaning"], names) != right["meaning"]:
            issues.append("original declaration meaning changed: " + old)
        elif (right.get("direct_sorry") and not left.get("direct_sorry")) or not _trust(right, {}) <= _trust(left, names):
            issues.append("trusted assumptions expanded: " + old)
        else:
            verified.append(old)
    # New auxiliaries cannot introduce holes/axioms even if unreferenced.
    for name in after.keys() - set(mapping.values()):
        row = after[name]
        if row["kind"] == "axiom" or row.get("direct_sorry"):
            issues.append("new axiom or sorry-bearing declaration: " + name)
    return {"passed": not issues, "issues": issues, "verified_original_names": sorted(verified)}


def _git_text(root: Path, revision: str, name: str) -> str:
    from . import bump_jobs
    result = bump_jobs.run(root, ["git", "show", f"{revision}:{name}"], cwd=root,
                           owner="Unity", task_id="migration-source")
    if result.returncode:
        raise ValueError("candidate source cannot be read from its exact Git identity: " + name)
    return result.stdout


def _patch_issues(root: Path, contract: dict, task: dict, candidate: dict) -> list[str]:
    """Constrain each integration to its assigned original declarations/commands."""
    from .bump_preparation import _strip_comments

    def offsets(lines: list[str]) -> list[int]:
        result = [0]
        for line in lines:
            result.append(result[-1] + len(line))
        return result

    def edits(left: str, right: str) -> list[tuple[str, int, int, int, int]]:
        # Refine changed line hunks, never all characters of a large file. For
        # a large ambiguous hunk retain its conservative replacement boundary.
        old, new = left.splitlines(keepends=True), right.splitlines(keepends=True)
        old_at, new_at = offsets(old), offsets(new)
        result = []
        for tag, a, b, c, d in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
            i, j, k, l = old_at[a], old_at[b], new_at[c], new_at[d]
            if tag != "equal" and (j - i) * (l - k) <= 1_000_000:
                for kind, p, q, r, s in difflib.SequenceMatcher(
                        a=left[i:j], b=right[k:l], autojunk=False).get_opcodes():
                    result.append((kind, i + p, i + q, k + r, k + s))
            else:
                result.append((tag, i, j, k, l))
        return result

    def boundary(position: int, changes: list[tuple], *, start: bool) -> int | None:
        possible = []
        for tag, i, j, k, l in changes:
            if i < position < j:
                # A prior replacement crossing a native boundary cannot be
                # attributed safely to either neighboring declaration.
                return k + position - i if tag == "equal" else None
            if position == i:
                possible.append(k)
            if position == j:
                possible.append(l)
        # Insertions exactly at a boundary remain outside that original range.
        return (max(possible) if start else min(possible)) if possible else None

    migration = contract["project_baseline"]["migration"]
    index = migration["original_index"]["occurrences"]
    metadata = task.get("migration", {})
    ids = original_ids(contract, task)
    by_path = {}
    for key in ids:
        row = index[key]
        if row.get("range"):
            by_path.setdefault(row["path"], []).append(row["range"])
    if metadata.get("kind") == "command":
        path = metadata.get("path")
        locations = metadata.get("original_ranges", [])
        if metadata.get("range"):
            locations = [*locations, metadata["range"]]
        if metadata.get("command_line"):
            locations = [*locations, {"start_line": metadata["command_line"], "end_line": metadata["command_line"]}]
        if path and locations:
            by_path.setdefault(path, []).extend(item for item in locations if item)
    issues = []
    if ids and not by_path:
        return ["assigned generated occurrence has no native source range; refine the task with its exact "
                "source-owning original declaration IDs before editing, never the whole module"]
    for path in candidate.get("changed_paths", []):
        before = (_git_text(root, candidate["base_main_sha"], path)
                  if path in migration["original_files"] else "")
        after = _git_text(root, candidate["commit_sha"], path)
        # This cheap source guard applies to supporting files too. It is not
        # the final per-declaration native trust/meaning certificate.
        before_code, after_code = _strip_comments(before), _strip_comments(after)
        for token in ("sorry", "admit", "axiom"):
            pattern = r"(?<![\w'])" + token + r"(?![\w'])"
            if len(re.findall(pattern, after_code)) > len(re.findall(pattern, before_code)):
                issues.append("candidate introduces a new proof hole or axiom token: " + path)
        if path not in migration["original_files"]:
            # Supporting files remain subject to final native no-new-trust and
            # original-declaration checks, plus the inherited path policy.
            continue
        if path not in by_path:
            issues.append("candidate edits source outside its declaration assignment: " + path)
            continue
        original = (Path(migration["original_root"]) / path).read_text()
        original_lines = original.splitlines(keepends=True)
        original_offsets = offsets(original_lines)
        original_to_base = edits(original, before)
        assigned = []
        for span in by_path[path]:
            first, last = span["start_line"] - 1, span["end_line"] - 1
            if not (0 <= first <= last < len(original_lines)):
                continue
            # Lean FileMap Position columns count Unicode scalar characters,
            # not UTF-8 bytes. Native end positions are exclusive.
            start_column = span.get("start_column", 0)
            end_column = span.get("end_column", len(original_lines[last].rstrip("\r\n")))
            if not (0 <= start_column <= len(original_lines[first].rstrip("\r\n"))
                    and 0 <= end_column <= len(original_lines[last].rstrip("\r\n"))):
                continue
            start = boundary(original_offsets[first] + start_column, original_to_base, start=True)
            end = boundary(original_offsets[last] + end_column, original_to_base, start=False)
            if start is not None and end is not None and start < end:
                assigned.append((start, end))
        for tag, start, end, _, _ in edits(before, after):
            if tag == "equal":
                continue
            if not any((left <= start < end <= right) if start != end else (left < start < right)
                       for left, right in assigned):
                issues.append("candidate changes another declaration or source command: " + path)
                break
    return issues


def verify_candidate(root: Path, contract: dict, task: dict, candidate: dict, *,
                     formal_tasks: list[dict], build: dict, layout: dict) -> dict:
    """Authorize bounded compiler progress; native acceptance remains pending."""
    from . import bump_contract as checked
    from .bump_diagnostics import diagnostics_from_output
    from .bump_spec import normalize_outputs
    baseline = contract["project_baseline"]
    migration = baseline["migration"]
    require_inputs(root, baseline)
    issues = _patch_issues(root, contract, task, candidate)
    diagnostic_path = root / ".unity" / "bump" / "diagnostics.json"
    previous = json.loads(diagnostic_path.read_text())
    if previous.get("main_sha") != checked._git(root, "rev-parse", "HEAD"):
        issues.append("compiler diagnostics are stale for the current accepted main")
    current = diagnostics_from_output(root, migration["original_index"], build.get("output", ""),
                                      build["returncode"], original_root=Path(migration["original_root"]),
                                      build_dir=migration["scope"]["build_dir"])
    ids = set(original_ids(contract, task))
    rows = current.get("errors", current.get("diagnostics", []))
    rows = [row for row in rows if row.get("severity", "error") == "error"]
    metadata = task.get("migration", {})
    if metadata.get("kind") == "command":
        spans = metadata.get("original_ranges") or [{"start_line": metadata.get("command_line"),
                                                     "end_line": metadata.get("command_line")}]
        if any(row.get("path") == metadata.get("path") and not row.get("occurrence_ids")
               and any(span and type(span.get("start_line")) is int and type(span.get("end_line")) is int
                       and span["start_line"] <= row.get("original_line", row.get("line", 0)) <= span["end_line"]
                       for span in spans) for row in rows):
            issues.append("assigned source command still has compiler errors")
    elif not ids:
        issues.append("declaration repair has no original occurrence binding")
    elif any(ids.intersection(row.get("occurrence_ids", [])) for row in rows):
        issues.append("assigned declaration still has compiler errors")
    if build["returncode"] and not rows:
        issues.append("failed compiler execution did not yield mapped source diagnostics")
    if current.get("unmapped_error_count") or current.get("unmapped"):
        issues.append("compiler produced unresolved diagnostics outside the repair mapping")
    # Work may expose downstream errors, which become new declaration tasks.
    # Errors in other, unrelated declarations cannot be accepted as progress.
    index = migration["original_index"]["occurrences"]
    affected = set(ids)
    while True:
        expanded = affected | {key for key, row in index.items()
                               if affected.intersection(row.get("dependencies", []))}
        if expanded == affected:
            break
        affected = expanded
    def error_key(row):
        return (row.get("path"), tuple(sorted(row.get("occurrence_ids", []))), row.get("message"))
    old_errors = {error_key(row) for row in previous.get("errors", previous.get("diagnostics", []))
                  if row.get("severity", "error") == "error"}
    for row in rows:
        owners = set(row.get("occurrence_ids", []))
        if error_key(row) not in old_errors and not owners.intersection(affected):
            issues.append("candidate introduces an unrelated compiler error: " + str(row.get("path")))
    outputs = normalize_outputs(candidate.get("outputs", []))
    proposed = deepcopy(contract)
    mapping = proposed["migration_correspondences"]
    for key in ids:
        original = index[key]
        named = [row for row in outputs if row["declaration"] == mapping[key]["declaration"]]
        if not named and len(ids) == 1 and len(outputs) == 1:
            named = outputs
        if len(named) != 1:
            issues.append("output mapping must identify the original declaration explicitly: " + original["display_name"])
            continue
        output = named[0]
        module = layout["modules"].get(output["file"])
        if not module:
            issues.append("mapped declaration lacks native module ownership: " + output["file"])
            continue
        mapping[key] = {"module": module, "declaration": output["declaration"],
                        "name_ast": original["name_ast"] if output["declaration"] == original["display_name"] else None}
    if outputs:
        proposed["bindings"][task["task_id"]] = outputs
        for output in outputs:
            name = output["declaration"]
            if name not in proposed["targets"]:
                proposed["targets"][name] = {"fingerprint": digest({"provisional_output": output}),
                    "module": layout["modules"].get(output["file"]), "signature": "native verification pending",
                    "axioms": [], "native_pending": True, "meaning_dependencies": [], "verification_dependencies": []}
    proposed["adopted_outputs"] = checked.adopted_output_records(contract, task_id=task["task_id"], outputs=outputs)
    proposed = checked._seal_contract(proposed)
    return {"status": "failed" if issues else "passed", "mode": "diagnostic_repair", "native_pending": True,
            "issues": sorted(set(issues)), "policy_sha256": checked.policy_hash(),
            "contract_sha256": proposed["sha256"], "base_contract_sha256": contract["sha256"],
            "proposed_contract": proposed, "original_occurrences": sorted(ids),
            "diagnostics_sha256": digest(current), "verified_targets": {}, "verified_tasks": []}


def _checked_native(root: Path, module: str, *, layout: dict | None = None) -> tuple[dict, dict]:
    from . import bump_inventory, bump_cache
    inventory = bump_inventory.read_native_module(root, module)
    compiled = bump_cache.compiled_identity(inventory["compiled_modules"])
    report = bump_inventory.read_native_module(root, module, local_meanings=True)
    if (report["compiled_modules"] != inventory["compiled_modules"]
            or bump_cache.compiled_identity(report["compiled_modules"]) != compiled):
        raise ValueError("compiled artifacts changed during native declaration inspection")
    if layout is not None:
        # Excluded local artifacts must not enter the selected import context,
        # including stale artifacts without a currently selected source file.
        build_root = (root / layout["build_dir"]).resolve()
        allowed = {(root / name).with_suffix(".olean").resolve() for name in layout["traces"].values()}
        for filename in report["compiled_modules"]:
            path = Path(filename).resolve()
            if path.is_relative_to(build_root) and path not in allowed:
                raise ValueError("native import context contains an excluded local compiled module")
    _rows(report)
    return report, compiled


def verify_final(paths, state: dict) -> dict:
    """Build every selected module and audit every original occurrence afresh."""
    from . import artifacts, bump_cache, bump_contract as contract, bump_state as runtime
    from .bump_input import require_source_matches
    from .bump_representation import snapshot as representation_snapshot
    from .bump_planner import empty_module_commands
    root = paths.project_root
    formal = state["formalization"]
    frozen = formal["contract"]
    baseline = frozen["project_baseline"]
    migration = baseline["migration"]
    index = migration["original_index"]
    require_source_matches(paths, state)
    require_inputs(root, baseline)
    before = contract.source_identity(root)
    issues = []
    if before["main_sha"] != formal["main_sha"]:
        issues.append("target main differs from the recorded integrated repairs")
    if any(row.get("status") != "complete" for row in state["formal_tasks"].values()):
        issues.append("declaration repair tasks remain unfinished")
    original_root = Path(migration["original_root"])
    if contract.environment_identity(original_root) != migration["original_environment"]:
        issues.append("original toolchain or dependency environment changed")
    original_layout = workspace_layout(original_root, migration)
    original_build = contract.build_sources(original_root, full=True, layout=original_layout)
    if original_build["returncode"]:
        issues.append("immutable original project no longer builds")
    target_layout = workspace_layout(root, migration)
    build = contract.build_sources(root, full=True, baseline=baseline)
    verified, receipts, compiled_inputs = [], [], {}
    if build["returncode"]:
        issues.append("final selected project build failed")
    elif not original_build["returncode"]:
        original_reports, target_reports = {}, {}
        for module in migration["selected_modules"]:
            original_report, original_compiled = _checked_native(original_root, module, layout=original_layout)
            current_report, target_compiled = _checked_native(root, module, layout=target_layout)
            original_reports[module] = original_report
            target_reports[module] = current_report
            compiled_inputs.update(original_compiled)
            compiled_inputs.update(target_compiled)
            for side, report in (("original", original_report), ("target", current_report)):
                record = artifacts.store_text(paths.artifacts, json.dumps(report, sort_keys=True) + "\n",
                    kind="bump_native_occurrences", producer="Unity", source=module,
                    metadata={"side": side, "baseline_sha256": baseline["sha256"]})
                receipts.append({"module": module, "side": side, "artifact_id": record["artifact_id"],
                                 "sha256": record["sha256"]})
        for module in sorted(set(target_layout["modules"].values()) - migration["selected_modules"].keys()):
            current_report, target_compiled = _checked_native(root, module, layout=target_layout)
            target_reports[module] = current_report
            compiled_inputs.update(target_compiled)
            record = artifacts.store_text(paths.artifacts, json.dumps(current_report, sort_keys=True) + "\n",
                kind="bump_native_occurrences", producer="Unity", source=module,
                metadata={"side": "helper", "baseline_sha256": baseline["sha256"]})
            receipts.append({"module": module, "side": "helper", "artifact_id": record["artifact_id"],
                             "sha256": record["sha256"]})
        mapping = frozen.get("migration_correspondences", {})
        if set(mapping) != set(index["occurrences"]):
            issues.append("migration correspondence omits original occurrences")
        if len({(row.get("module"), row.get("declaration")) for row in mapping.values()}) != len(mapping):
            issues.append("migration correspondence merges distinct original occurrences")
        renames = {}
        for key, row in index["occurrences"].items():
            target = mapping.get(key, {})
            target_report = target_reports.get(target.get("module"))
            actual = _rows(target_report).get(target.get("declaration")) if target_report else None
            if actual is None:
                issues.append("mapped original declaration missing: " + key)
                continue
            if target.get("name_ast") is not None and actual["name_ast"] != target["name_ast"]:
                issues.append("mapped declaration structural identity changed: " + key)
                continue
            identity = _name_key(row["name_ast"])
            if identity in renames and renames[identity] != actual["name_ast"]:
                issues.append("ambiguous cross-module declaration rename: " + key)
            renames[identity] = actual["name_ast"]
        for module, original in original_reports.items():
            declared = {key: row for key, row in index["occurrences"].items() if row["module"] == module}
            native_names = {_name_key(row["name_ast"]) for row in original["declarations"]}
            if native_names != {_name_key(row["name_ast"]) for row in declared.values()}:
                issues.append("immutable original declaration index no longer matches native inventory: " + module)
            # A declaration can move between selected modules via an explicit
            # task output mapping. Compare only each original occurrence's
            # named target, retaining the original module occurrence identity.
            target_rows = []
            display_mapping = {}
            for key, row in declared.items():
                target = mapping.get(key, {})
                native = target_reports.get(target.get("module"))
                found = _rows(native).get(target.get("declaration")) if native else None
                if found:
                    target_rows.append(found)
                display_mapping[row["display_name"]] = target.get("declaration", row["display_name"])
            comparison = compare_reports(original, {**original, "declarations": target_rows,
                "raw_declaration_count": len(target_rows)}, display_mapping, renames=renames)
            issues.extend(comparison["issues"])
            verified.extend(key for key, row in declared.items()
                            if row["display_name"] in comparison["verified_original_names"])
        mapped = {(row["module"], row["declaration"]) for row in mapping.values()}
        for module, report in target_reports.items():
            for row in report["declarations"]:
                if (module, row["display_name"]) in mapped:
                    continue
                if row["kind"] == "axiom" or row.get("direct_sorry"):
                    issues.append("new axiom or sorry-bearing declaration: " + module + ":" + row["display_name"])
                for axiom in row["axioms"]:
                    # New helper declarations may use only Lean's ordinary
                    # logical assumptions, never inherit a project's old hole.
                    standard = {("str", ("anonymous",), "propext"),
                        ("str", ("str", ("anonymous",), "Classical"), "choice"),
                        ("str", ("str", ("anonymous",), "Quot"), "sound")}
                    if _name_key(axiom["reference"]["name_ast"]) not in standard:
                        issues.append("new declaration expands project trust: " + module + ":" + row["display_name"])
    require_inputs(root, baseline)
    require_source_matches(paths, state)
    if contract.source_identity(root) != before:
        issues.append("source/environment changed during final migration verification")
    compiled_receipt = bump_cache.compiled_receipt(root, compiled_inputs) if compiled_inputs else None
    if not compiled_receipt or not bump_cache.compiled_receipt_current(root, compiled_receipt):
        issues.append("compiled native inputs are missing or changed during final review")
    native_complete = not issues and set(verified) == set(index["occurrences"])
    if not native_complete and not issues:
        issues.append("native checks did not cover all original occurrences")
    report = {**before, "snapshot_id": "review-" + uuid.uuid4().hex,
        "policy_sha256": contract.policy_hash(), "project_baseline_sha256": baseline["sha256"],
        "project_verification": coverage(baseline),
        "compiled_receipt": compiled_receipt,
        "passed": not issues, "issues": sorted(set(issues)), "blockers": [],
        "solution_candidate": formal["solution_candidate"], "solution_sha256": formal["solution_sha256"],
        "formalization_revision": formal["revision"], "contract_sha256": frozen["sha256"],
        "spec_sha256": digest(formal["spec"]), "repairs_sha256": runtime.repair_digest(state),
        "external_declarations": {}, "prerequisite_declarations": {},
        "representation_reviews": representation_snapshot(state),
        "accepted_candidates": {key: row.get("accepted_candidate") for key, row in state["formal_tasks"].items()},
        "task_statuses": {key: row["status"] for key, row in state["formal_tasks"].items()},
        "declarations": {output["declaration"]: key for key, row in state["formal_tasks"].items()
                         for output in row.get("outputs", [])},
        "migration_review": {"policy": POLICY, "native_complete": native_complete,
            "original_occurrences": sorted(index["occurrences"]), "verified_occurrences": sorted(set(verified)),
            "empty_module_commands": empty_module_commands(index),
            "occurrence_declarations": {key: row["declaration"] for key, row in frozen["migration_correspondences"].items()},
            "helper_modules": sorted(set(target_layout["modules"].values()) - migration["selected_modules"].keys()),
            "scope_sha256": migration["scope"]["sha256"], "native_reports": receipts,
            "correspondences_sha256": digest(frozen["migration_correspondences"])},
        "build": build}
    record = artifacts.store_text(paths.artifacts, json.dumps(report, sort_keys=True) + "\n",
                                  kind="bump_machine_review", producer="Unity")
    return {key: value for key, value in report.items() if key != "build"} | {"artifact_id": record["artifact_id"]}


def validate_native_snapshot(state: dict, report: dict) -> None:
    """Pure saved-evidence binding; performs no build, imports or publication."""
    from .bump_planner import empty_module_commands
    frozen = state["formalization"]["contract"]
    baseline = frozen["project_baseline"]
    migration = baseline["migration"]
    review = report.get("migration_review") or {}
    require_original_obligations(baseline, frozen.get("requirements", []))
    expected = sorted(migration["original_index"]["occurrences"])
    if (review.get("policy") != POLICY or review.get("original_occurrences") != expected
            or review.get("empty_module_commands", {}) != empty_module_commands(migration["original_index"])
            or review.get("occurrence_declarations") != {key: row["declaration"]
                for key, row in frozen.get("migration_correspondences", {}).items()}
            or review.get("scope_sha256") != migration["scope"]["sha256"]
            or review.get("correspondences_sha256") != digest(frozen.get("migration_correspondences", {}))
            or report.get("project_verification") != coverage(baseline)):
        raise ValueError("native migration review has stale scope or original obligations")
    if report.get("passed"):
        if (review.get("native_complete") is not True or review.get("verified_occurrences") != expected
                or not report.get("compiled_receipt")
                or len(review.get("native_reports", [])) != 2 * len(migration["selected_modules"]) + len(review.get("helper_modules", []))
                or {(row.get("module"), row.get("side")) for row in review.get("native_reports", [])}
                != ({(module, side) for module in migration["selected_modules"] for side in ("original", "target")}
                    | {(module, "helper") for module in review.get("helper_modules", [])})):
            raise ValueError("provisional repair receipts cannot establish final native migration acceptance")


def snapshot_is_current(paths, state: dict, report: dict, *, require_complete: bool = True) -> bool:
    from . import artifacts, bump_cache, bump_contract as contract, bump_state as runtime
    from .bump_input import require_source_matches
    try:
        if not report:
            return False
        validate_native_snapshot(state, report)
        runtime._validate_snapshot_binding(state, report, require_passed=report.get("passed") is True)
        baseline = state["formalization"]["contract"]["project_baseline"]
        require_inputs(paths.project_root, baseline)
        require_source_matches(paths, state)
        current = contract.source_identity(paths.project_root)
        if any(report.get(key) != current[key] for key in current):
            return False
        if require_complete and not runtime.all_formal_tasks_complete(state):
            return False
        if report.get("passed") and not bump_cache.compiled_receipt_current(paths.project_root, report.get("compiled_receipt")):
            return False
        for reference in report.get("migration_review", {}).get("native_reports", []):
            content = artifacts.artifact_bytes(paths.artifacts, reference["artifact_id"])
            if hashlib.sha256(content).hexdigest() != reference["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
