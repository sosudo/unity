"""Fail-closed preservation policy for formalizing an existing Lean project.

This is not a semantic critic. It freezes the project's original kernel
declarations and environment, then grants narrowly scoped completion rights.
It never stashes, commits, resets, scaffolds, or repairs the user's project.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path

_RUNTIME = {".git", ".lake", ".unity", ".worktrees", "lake-packages", "__pycache__"}
_KINDS = {"theorem", "def", "opaque", "inductive", "constructor", "recursor", "quot", "axiom"}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _seal(value: dict) -> dict:
    value = copy.deepcopy(value)
    value.pop("sha256", None)
    value["sha256"] = _digest(value)
    return value


def _baseline_digest(value: dict) -> str:
    """Recompute the identical seal without copying immutable evidence to discard it.

    This is deliberately recomputed on every validation. No cached digest or
    earlier validation result can conceal mutation of a nested native record.
    Publication still uses ``_seal`` and owns an independent deep copy.
    """
    return _digest({key: item for key, item in value.items() if key != "sha256"})


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "existing project Git inspection failed")
    return result.stdout.strip()


def _safe_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and not any(
        part in _RUNTIME for part in path.parts)


def _tracked(root: Path) -> list[str]:
    return sorted(name for name in _git(root, "ls-files", "-z").split("\0")
                  if name and _safe_path(name))


def _require_clean(root: Path) -> None:
    """Runtime directories may be generated; all user work must stay untouched."""
    result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            cwd=root, capture_output=True, check=True)
    # NUL framing handles whitespace and rename destinations without shell parsing.
    entries = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    dirty = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status, name = entry[:2], entry[3:]
        if _safe_path(name):
            dirty.append(name)
        if "R" in status or "C" in status:
            if index < len(entries) and _safe_path(entries[index]):
                dirty.append(entries[index])
            index += 1
    if dirty:
        raise ValueError("existing-project baseline requires clean tracked/untracked user inputs; "
                         "preserve or commit them yourself before starting: " + ", ".join(sorted(set(dirty))))


def _records(inspection: dict) -> dict:
    records = inspection.get("project_records")
    inventory = inspection.get("project_declarations")
    if not isinstance(records, dict) or not isinstance(inventory, list):
        raise ValueError("existing-project baseline requires complete native declaration inventory")
    if (any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in inventory)
            or len(inventory) != len(records)
            or set(records) != {row["name"] for row in inventory}):
        raise ValueError("native declaration records do not cover the entire project inventory")
    inventory_by_name = {row["name"]: row for row in inventory}
    for name, row in records.items():
        if (not isinstance(row, dict) or row.get("name") != name
                or row.get("target_kind") not in _KINDS or not isinstance(row.get("module"), str)
                or not isinstance(row.get("type"), list) or not isinstance(row.get("level_params"), list)
                or not isinstance(row.get("is_internal_detail"), bool)
                or not isinstance(row.get("direct_dependencies"), list)
                or any(not isinstance(dep, str) for dep in row.get("direct_dependencies", []))
                or not isinstance(row.get("declaration_meaning"), dict) or "proof_body" not in row
                or (row["proof_body"] is not None and not isinstance(row["proof_body"], list))
                or row["module"] != inventory_by_name[name].get("module")
                or row["target_kind"] != inventory_by_name[name].get("kind")):
            raise ValueError(f"incomplete native preservation evidence for {name}")
        meaning = row["declaration_meaning"]
        if (not {"name", "module", "kind", "type", "level_params"} <= meaning.keys()
                or meaning["module"] != row["module"] or meaning["kind"] != row["target_kind"]
                or meaning["type"] != row["type"] or meaning["level_params"] != row["level_params"]
                or (row["target_kind"] in {"theorem", "def", "opaque"} and not isinstance(row["proof_body"], list))
                or (row["target_kind"] in {"def", "opaque"} and meaning.get("value") != row["proof_body"])):
            raise ValueError(f"incomplete native declaration meaning/body evidence for {name}")
    for key in ("project_axioms", "project_sorries", "project_used_axioms"):
        if not isinstance(inspection.get(key), list) or any(not isinstance(name, str)
                                                         for name in inspection[key]):
            raise ValueError(f"native declaration inventory omitted {key}")
    return copy.deepcopy(records)


def _require_editable_targets(selected, records: dict, holes: set[str]) -> None:
    """Reject generated-hole scopes until native source ownership is available.

    Lean can move a structure-field sorry into `definition._proof_1`. A name
    prefix is not reliable evidence that changing that auxiliary preserves the
    original definition's other fields. Do not invent such ownership rights.
    Unrelated generated declarations remain protected and may stay out of scope.
    """
    visited, pending, unsupported = set(), list(selected), set()
    while pending:
        name = pending.pop()
        if name in visited or name not in records:
            continue
        visited.add(name)
        row = records[name]
        if row["is_internal_detail"] and (name in holes or name in selected):
            unsupported.add(name)
        pending.extend(row["direct_dependencies"])
    if unsupported:
        raise ValueError("selected scope contains compiler-generated/internal proof holes without reliable "
                         "editable-owner evidence; this scope is not supported yet. Choose explicit ordinary "
                         "targets independent of these holes, or preserve this project unchanged: "
                         + ", ".join(sorted(unsupported)))


def _initial_scope(target_scope: str, records: dict, holes: set[str], *, eligible: set[str] | None = None) -> dict:
    if not isinstance(target_scope, str) or not target_scope.strip():
        raise ValueError("target scope must be nonempty")
    text = target_scope.strip()
    eligible = set(records) if eligible is None else eligible
    if text.lower() == "all":
        selected = holes & eligible
        _require_editable_targets(selected, records, holes)
        return {"mode": "all", "existing_targets": sorted(selected), "bound": True}
    tokens = [token for token in re.split(r"[,\s]+", text) if token]
    if tokens and all(token in records for token in tokens):
        if set(tokens) - eligible:
            raise ValueError("existing targets are read-only auxiliary declarations: " + ", ".join(sorted(set(tokens) - eligible)))
        _require_editable_targets(set(tokens), records, holes)
        return {"mode": "explicit", "existing_targets": sorted(set(tokens)), "bound": True}
    # Names-only syntax with an unknown identifier is likely a typo, not license
    # to let a planner reinterpret it as a broad natural-language request.
    if all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'.]*", token) for token in tokens) and (
            len(tokens) == 1 or "," in text or any("." in token for token in tokens)):
        raise ValueError("unknown explicit existing target(s): " + ", ".join(
            token for token in tokens if token not in records))
    return {"mode": "natural", "existing_targets": [], "bound": False}


def capture_baseline(root: Path, target_scope: str = "All", inspection: dict | None = None,
                     *, project_scope: str = "all", migration: dict | None = None,
                     original_inspections: dict | None = None, compiler_modules: dict | None = None) -> dict:
    """Build and freeze a clean, existing project before any agent dispatch.

    The optional native receipt is an internal testing/reuse hook; it is never an
    agent-supplied tool argument. Native build/environment checks still run.
    """
    from . import bump_contract as contract, bump_scope
    if migration is not None:
        return _capture_migration_baseline(root, migration, original_inspections, compiler_modules)
    if project_scope == "changes":
        from . import bump_delta
        if inspection is not None:
            raise ValueError("change-focused baselines do not accept a whole-project inventory")
        return bump_delta.capture(root, target_scope)
    root = Path(root).resolve()
    _require_clean(root)
    head = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise ValueError("existing-project baseline requires a named branch")
    layout = contract.workspace_layout(root)
    policy = bump_scope.capture(root, layout, project_scope)
    layout = bump_scope.apply(root, layout, policy)
    before = contract._file_hashes(root, build_dir=layout["build_dir"])
    tracked = _tracked(root)
    uncopied = set(before) - set(tracked)
    if uncopied:
        raise ValueError("existing-project inputs are ignored/untracked and cannot be preserved in worktrees: "
                         + ", ".join(sorted(uncopied)))
    environment = contract.environment_identity(root)
    build = contract.build_sources(root, full=True, layout=layout, task_id="project-baseline")
    if build["returncode"]:
        raise ValueError("existing project must build before formalization: " + build["output"][-3000:])
    if inspection is None:
        inspection = contract.inspect_environment(root, [], layout=layout, _inventory_only=True)
    records = _records(inspection)
    if any(row["module"] not in set(layout["verification_modules"].values()) for row in records.values()):
        raise ValueError("native preservation inventory escaped the verification scope")
    holes = set(inspection["project_axioms"]) | set(inspection["project_sorries"])
    if holes - records.keys():
        raise ValueError("native hole inventory contains unaccounted project declarations")
    _require_clean(root)
    if (head != _git(root, "rev-parse", "HEAD")
            or before != contract._file_hashes(root, build_dir=layout["build_dir"])
            or environment != contract.environment_identity(root)):
        raise ValueError("project inputs changed while capturing the existing-project baseline")
    eligible = {name for name, row in records.items() if row["module"] in set(layout["editable_modules"].values())}
    return _seal({"version": 1, "project_root": str(root), "branch": branch, "head": head, "files": before,
                  "tracked_files": tracked, "environment": environment, "layout": layout,
                  "declarations": records, "target_scope": target_scope.strip(),
                  "scope": _initial_scope(target_scope, records, holes, eligible=eligible),
                  **({"verification_scope": policy} if project_scope == "libraries" else {}),
                  **{key: sorted(inspection[key]) for key in
                     ("project_axioms", "project_sorries", "project_used_axioms")}})


def _capture_migration_baseline(root, migration, reports, graph) -> dict:
    from . import bump_contract, bump_migration_contract as native, bump_migration_project as project
    root = Path(root).resolve()
    errors = project.validate_original(Path(migration["root"]), migration) + project.validate_target(root, migration)
    if errors:
        raise ValueError("; ".join(errors))
    _require_clean(root)
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise ValueError("Bump target baseline requires a named branch")
    if not isinstance(graph, dict) or not graph or not isinstance(reports, dict) or set(reports) != set(graph):
        raise ValueError("Bump requires native original reports for every compiler module")
    scope = migration.get("scope")
    errors = project.scope_errors(scope, migration.get("source_files"))
    if errors or scope.get("selected_modules") != {name: row["path"] for name, row in graph.items()}:
        raise ValueError("Bump requires a sealed selected-module build scope: " + "; ".join(errors))
    for module, report in sorted(reports.items()):
        issues = native._report_issues(report)
        if issues or report.get("module") != module or set(report.get("owned_modules", [])) != set(graph):
            raise ValueError("invalid original native inspection for " + module + ": " + "; ".join(issues))
    declarations = migration_declarations(reports)
    layout = bump_contract.workspace_layout(root)
    expected = {row["path"]: name for name, row in graph.items()}
    if any(layout["modules"].get(path) != name for path, name in expected.items()):
        raise ValueError("target Lake ownership differs from the selected original compiler graph")
    files = project.source_files(root)
    value = _seal({"version": 5, "policy": "migration-v1", "scope_policy": 1, "occurrence_policy": 1,
        "build_scope": copy.deepcopy(scope), "project_root": str(root),
        "branch": branch, "head": _git(root, "rev-parse", "HEAD"), "files": files,
        "tracked_files": _tracked(root), "environment": bump_contract.environment_identity(root),
        "layout": layout, "migration": copy.deepcopy(migration),
        "compiler_modules": copy.deepcopy(graph), "original_reports": copy.deepcopy(reports),
        "declarations": declarations, "target_scope": "All", "scope": {"mode": "all", "bound": True,
            "existing_targets": sorted(declarations)},
        "project_axioms": sorted(n for n, r in declarations.items() if r["kind"] == "axiom"),
        "project_sorries": sorted(n for n, r in declarations.items() if r["direct_sorry"]),
        "project_used_axioms": sorted({a for r in declarations.values() for a in r["axioms"]})})
    errors = _migration_baseline_errors(value)
    if errors:
        raise ValueError("; ".join(errors))
    _require_migration_inputs(root, value)
    return value


def capture_baseline_v2(root: Path, *, migration: dict, original_index_ref: dict,
                        compiler_modules: dict, index: dict | None = None) -> dict:
    """Capture policy 2 metadata without copying native expression inventories."""
    from . import bump_contract, bump_migration_project as project
    from .bump_inventory import load_original_index
    root = Path(root).resolve(strict=True)
    artifact_root = root / ".unity" / "artifacts"
    loaded = load_original_index(artifact_root, original_index_ref)
    if index is not None and index != loaded:
        raise ValueError("supplied original index differs from its immutable artifact")
    errors = project.validate_original(Path(migration["root"]), migration) + project.validate_target(root, migration)
    if errors:
        raise ValueError("; ".join(errors))
    _require_clean(root)
    graph = compiler_modules
    scope = migration.get("scope")
    if (project.scope_errors(scope, migration.get("source_files")) or not graph
            or scope["selected_modules"] != {name: row["path"] for name, row in graph.items()}
            or set(loaded["modules"]) != set(graph)
            or any(loaded["modules"][name]["path"] != row["path"] for name, row in graph.items())
            or loaded["scope_sha256"] != scope["sha256"]):
        raise ValueError("original index does not cover the sealed build scope")
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise ValueError("Bump target baseline requires a named branch")
    files = project.source_files(root)
    layout = bump_contract.workspace_layout(root)
    if any(layout["modules"].get(row["path"]) != module for module, row in graph.items()):
        raise ValueError("target Lake ownership differs from the original compiler graph")
    value = _seal({"version": 6, "policy": "migration-v2", "scope_policy": 1, "occurrence_policy": 1,
        "build_scope": copy.deepcopy(scope), "project_root": str(root), "branch": branch,
        "head": _git(root, "rev-parse", "HEAD"), "files": files, "tracked_files": _tracked(root),
        "environment": bump_contract.environment_identity(root), "layout": layout,
        "migration": copy.deepcopy(migration), "compiler_modules": copy.deepcopy(graph),
        "artifact_root": str(artifact_root), "original_index_ref": copy.deepcopy(original_index_ref),
        "original_index_sha256": loaded["index_sha256"],
        "occurrence_count": len(loaded["occurrences"]), "target_scope": "All",
        "scope": {"mode": "all", "bound": True}})
    errors = _migration_v2_baseline_errors(value)
    if errors:
        raise ValueError("; ".join(errors))
    _require_migration_inputs(root, value)
    return value


def _migration_v2_baseline_errors(baseline: dict) -> list[str]:
    """Pure compact schema guard; artifact bytes are resolved at check boundaries."""
    from . import bump_migration_project as project
    try:
        if (baseline.get("version") != 6 or baseline.get("policy") != "migration-v2"
                or baseline.get("scope_policy") != 1 or baseline.get("occurrence_policy") != 1
                or baseline.get("sha256") != _baseline_digest(baseline)
                or any(key in baseline for key in ("original_reports", "declarations", "meanings"))):
            return ["compact migration baseline integrity mismatch"]
        migration, graph, scope = baseline["migration"], baseline["compiler_modules"], baseline["build_scope"]
        reference = baseline["original_index_ref"]
        if (not isinstance(reference, dict)
                or not re.fullmatch(r"artifact-[0-9a-f]{12}", str(reference.get("artifact_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(baseline.get("original_index_sha256", "")))
                or type(baseline.get("occurrence_count")) is not int or baseline["occurrence_count"] < 0
                or not graph or not baseline["branch"] or not baseline["environment"]
                or baseline["project_root"] != migration["target"]
                or baseline["artifact_root"] != str(Path(baseline["project_root"]) / ".unity" / "artifacts")
                or migration.get("target_sealed") is not True
                or migration.get("identity") != project._digest({k: v for k, v in migration.items() if k != "identity"})
                or scope != migration.get("scope") or project.scope_errors(scope, migration.get("source_files"))
                or baseline["scope"] != {"mode": "all", "bound": True}):
            return ["compact migration baseline has inconsistent pinned metadata"]
        paths = [row["path"] for row in graph.values()]
        if (len(paths) != len(set(paths)) or scope["selected_modules"] != {key: row["path"] for key, row in graph.items()}
                or any(not _safe_path(row["path"]) or row["path"] not in baseline["files"]
                    or Path(row["path"]).suffix != ".lean" or row.get("compiler_derived") is not True
                    or not isinstance(row.get("imports"), list) or set(row["imports"]) - set(graph)
                    or baseline["layout"]["modules"].get(row["path"]) != key for key, row in graph.items())
                or any(baseline["files"].get(path) != value for path, value in scope["excluded_files"].items())):
            return ["compact migration baseline has inconsistent module ownership"]
        return []
    except (KeyError, TypeError, ValueError, AttributeError):
        return ["compact migration baseline is incomplete"]


def migration_occurrence_id(module: str, native_name: object) -> str:
    """A kernel Name in one raw module inventory, not a global first owner.

    The same Name can occur in multiple module artifacts with independently
    checked meanings and proof assumptions. Display names are never parsed to
    recover identity, and an occurrence in one module cannot discharge another.
    """
    from . import bump_migration_contract as native
    if not isinstance(module, str) or not module:
        raise ValueError("migration occurrence requires an exact module")
    native._name(native_name)
    return "occurrence-" + native.digest({"module": module, "name": native_name})


def migration_declarations(reports: dict) -> dict:
    """Lossless aggregate retaining every module's own declaration and trust."""
    declarations = {}
    for module, report in sorted(reports.items()):
        for name, row in sorted(report["declarations"].items()):
            meaning = report["meanings"][name]["meaning"]
            occurrence = migration_occurrence_id(module, meaning["name"])
            if occurrence in declarations:
                raise ValueError("duplicate native declaration occurrence: " + module + ": " + name)
            declarations[occurrence] = {**copy.deepcopy(row), "module": module,
                "occurrence_id": occurrence, "declaration": name,
                "native_name": copy.deepcopy(meaning["name"]), "target_kind": row["kind"],
                "declaration_meaning": copy.deepcopy(meaning)}
    return declarations


def _migration_declarations_match(reports: dict, declarations: dict) -> bool:
    """Compare every exact occurrence without allocating another native tree.

    Only validation uses these short-lived views. Published inventories still
    deep-copy their records, and duplicates/extras are checked independently.
    """
    if not isinstance(declarations, dict):
        return False
    seen = set()
    for module, report in sorted(reports.items()):
        for name, row in sorted(report["declarations"].items()):
            meaning = report["meanings"][name]["meaning"]
            occurrence = migration_occurrence_id(module, meaning["name"])
            if occurrence in seen:
                return False
            seen.add(occurrence)
            expected = {**row, "module": module, "occurrence_id": occurrence,
                "declaration": name, "native_name": meaning["name"],
                "target_kind": row["kind"], "declaration_meaning": meaning}
            if declarations.get(occurrence) != expected:
                return False
    return seen == declarations.keys()


def _migration_baseline_errors(baseline: dict) -> list[str]:
    from . import bump_migration_contract as native, bump_migration_project as project
    try:
        if (baseline.get("version") != 5 or baseline.get("scope_policy") != 1
                or baseline.get("occurrence_policy") != 1
                or baseline.get("sha256") != _baseline_digest(baseline)):
            return ["migration baseline integrity mismatch"]
        migration, graph, reports = baseline["migration"], baseline["compiler_modules"], baseline["original_reports"]
        scope = baseline.get("build_scope")
        scope_errors = project.scope_errors(scope, migration.get("source_files"))
        if scope_errors or scope != migration.get("scope"):
            return ["migration baseline has missing or inconsistent sealed build scope", *scope_errors]
        if (migration.get("target_sealed") is not True or migration.get("identity") != project._digest(
                {k: v for k, v in migration.items() if k != "identity"})
                or not graph or set(graph) != set(reports) or not baseline["environment"]
                or baseline["project_root"] != migration["target"] or not baseline["branch"]
                or baseline["scope"] != {"mode": "all", "bound": True,
                    "existing_targets": sorted(baseline["declarations"])}):
            return ["migration baseline has inconsistent sealed environment/module coverage"]
        paths = []
        # Original contexts share most toolchain imports. Canonicalize each
        # path once in this validation only; never retain filesystem answers
        # across a later baseline/candidate check.
        resolved_paths: dict[str, Path] = {}
        for module, row in graph.items():
            path = row["path"]
            if (not _safe_path(path) or Path(path).suffix != ".lean" or path not in baseline["files"]
                    or not isinstance(row["imports"], list) or set(row["imports"]) - set(graph)
                    or row.get("compiler_derived") is not True):
                return ["migration baseline has invalid compiler module ownership"]
            paths.append(path)
            report = reports[module]
            if native._report_issues(report) or report["module"] != module or set(report["owned_modules"]) != set(graph):
                return ["migration baseline has invalid original native evidence: " + module]
            scope_issues = migration_inspection_scope_errors(
                report, baseline, root=Path(migration.get("original", migration["root"])),
                _resolved_paths=resolved_paths)
            if scope_issues:
                return scope_issues
            if (report["source_hashes"] != {path: value for path, value in migration["source_files"].items()
                                           if path.endswith(".lean")}
                    or report["environment"].get("config") != migration["original_config"]):
                return ["original native context does not match the sealed original sources/configuration: " + module]
        if len(paths) != len(set(paths)) or not _migration_declarations_match(reports, baseline["declarations"]):
            return ["migration original declaration inventory is inconsistent"]
        expected = {module: row["path"] for module, row in graph.items()}
        if (scope["selected_modules"] != expected
                or any(baseline["layout"]["modules"].get(path) != module for module, path in expected.items())
                or any(baseline["files"].get(path) != value for path, value in scope["excluded_files"].items())):
            return ["migration selected native/byte-preserved ownership is inconsistent"]
        return []
    except (KeyError, TypeError, ValueError, AttributeError):
        return ["migration baseline is incomplete or unsupported"]


def migration_inspection_scope_errors(report: dict, baseline: dict, *, root: Path,
                                     _resolved_paths: dict[str, Path] | None = None) -> list[str]:
    """Require actual transitive native imports to stay inside the sealed boundary."""
    scope = baseline.get("build_scope") or {}
    imported = report.get("imported_modules")
    if (not isinstance(imported, list) or not imported
            or any(not isinstance(module, str) or not module for module in imported)
            or len(imported) != len(set(imported)) or report.get("module") not in imported):
        return ["migration inspection lacks complete native imported-module evidence"]
    excluded = set(scope.get("excluded_modules", {})) & set(imported)
    if excluded:
        return ["native imports cross the frozen migration build scope: " + ", ".join(sorted(excluded))]
    compiled, identities = report.get("compiled_modules"), report.get("compiled_inputs")
    if (not isinstance(compiled, list) or len(compiled) != len(imported)
            or not isinstance(identities, dict)
            or any(not isinstance(path, str) or not Path(path).is_absolute()
                   or Path(path).suffix != ".olean" for path in compiled)):
        return ["migration inspection lacks paired native artifact provenance"]
    build_dir = scope.get("native_metadata", {}).get("build_dir", ".lake/build")
    resolved = {} if _resolved_paths is None else _resolved_paths

    def canonical(path: Path | str) -> Path:
        key = str(path)
        if key not in resolved:
            resolved[key] = Path(path).resolve()
        return resolved[key]

    local_artifacts = canonical(Path(root) / build_dir / "lib" / "lean")
    local_parts = local_artifacts.parts
    selected = scope.get("selected_modules", {})
    for module, filename in zip(imported, compiled):
        identity = identities.get(filename)
        if (not isinstance(identity, dict) or not isinstance(identity.get("path"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("sha256", "")))
                or canonical(identity["path"]) != canonical(filename)):
            return ["native import artifact is not bound to its compiled receipt: " + module]
        artifact = canonical(identity["path"])
        if module in selected:
            expected = canonical(local_artifacts / (module.replace(".", "/") + ".olean"))
            if artifact != expected:
                return ["selected module resolved outside its sealed local artifact: " + module]
        # Both sides are already canonical absolute paths. Component comparison
        # preserves containment semantics without allocating Path.parents.
        if artifact.parts[:len(local_parts)] == local_parts and module not in selected:
            # An unmatched original Notes.lean has no invented ownership label.
            # Its stale local artifact still cannot enter a selected native context.
            return ["native local artifact crosses the selected migration boundary: " + module]
    return []


def _require_migration_inputs(root: Path, baseline: dict) -> None:
    from . import bump_contract as contract, bump_migration_project as project
    errors = (_migration_v2_baseline_errors(baseline) if baseline.get("policy") == "migration-v2"
              else _migration_baseline_errors(baseline))
    if errors:
        raise ValueError("; ".join(errors))
    root, migration = Path(root).resolve(), baseline["migration"]
    errors = project.validate_original(Path(migration["root"]), migration)
    # Candidates are independent Git worktrees, not the controller's target path.
    # The sealed config/dependency bytes apply equally in every candidate.
    if project.config_hashes(root) != migration["target_config"]:
        errors.append("sealed target configuration changed")
    if errors:
        raise ValueError("; ".join(errors))
    # Check excluded source bytes BEFORE Lake configuration or compiler import
    # queries. Broken out-of-scope files are preserved, not compiled or repaired.
    errors.extend(project.validate_dependencies(root))
    files = project.source_files(root)
    if set(files) != set(baseline["files"]):
        errors.append("migration added or removed an original project file")
    editable = {row["path"] for row in baseline["compiler_modules"].values()}
    for name in set(files) & set(baseline["files"]):
        if name not in editable and files[name] != baseline["files"][name]:
            errors.append("protected non-module input changed: " + name)
        if name in editable and ((root / name).is_symlink() or not (root / name).is_file()):
            errors.append("module input is missing or symlinked: " + name)
    if errors:
        raise ValueError("; ".join(errors))
    # The Git-based inventory also deliberately scans untracked Lean files;
    # independently account for untracked/ignored non-Lean inputs before a
    # compiler can consume them through include_str or an elaborator helper.
    all_inputs = contract._file_hashes(root, build_dir=baseline["layout"].get("build_dir", ".lake/build"))
    if all_inputs != files:
        raise ValueError("migration has unaccounted, symlinked or changed non-runtime input files")
    errors = project.validate_build_scope(root, baseline["build_scope"])
    if errors:
        raise ValueError("; ".join(errors))


def baseline_errors(baseline: dict) -> list[str]:
    from . import bump_scope
    if isinstance(baseline, dict) and baseline.get("policy") == "migration-v2":
        return _migration_v2_baseline_errors(baseline)
    if isinstance(baseline, dict) and baseline.get("policy") == "migration-v1":
        return _migration_baseline_errors(baseline)
    if isinstance(baseline, dict) and baseline.get("version") == 2:
        from . import bump_delta
        return bump_delta.errors(baseline)
    if not isinstance(baseline, dict) or baseline.get("version") != 1:
        return ["existing-project baseline is missing or unsupported"]
    expected = _baseline_digest(baseline)
    if baseline.get("sha256") != expected:
        return ["existing-project baseline integrity mismatch"]
    required = {"branch", "head", "files", "tracked_files", "environment", "layout", "declarations",
                "target_scope", "scope", "project_axioms", "project_sorries", "project_used_axioms"}
    if not required <= baseline.keys():
        return ["existing-project baseline is incomplete"]
    if (any(not isinstance(baseline[key], str) or not baseline[key]
            for key in ("branch", "head", "target_scope"))
            or not isinstance(baseline["declarations"], dict) or not isinstance(baseline["files"], dict)
            or not isinstance(baseline["environment"], dict)
            or not isinstance(baseline["layout"], dict) or not isinstance(baseline["layout"].get("modules"), dict)
            or not isinstance(baseline["layout"].get("build_dir"), str)
            or any(not isinstance(baseline[key], list) or any(not isinstance(name, str) for name in baseline[key])
                   for key in ("tracked_files", "project_axioms", "project_sorries", "project_used_axioms"))
            or not isinstance(baseline["scope"], dict)
            or baseline["scope"].get("mode") not in {"all", "explicit", "natural"}
            or not isinstance(baseline["scope"].get("bound"), bool)
            or not isinstance(baseline["scope"].get("existing_targets"), list)
            or any(not isinstance(name, str) for name in baseline["scope"]["existing_targets"])
            or not set(baseline["scope"]["existing_targets"]) <= baseline["declarations"].keys()):
        return ["existing-project baseline has invalid scope/declaration evidence"]
    try:
        if baseline["layout"].get("project_scope") == "libraries" and "verification_scope" not in baseline:
            return ["library baseline is missing its verification scope"]
        if "verification_scope" in baseline:
            issues = bump_scope.errors(baseline["verification_scope"], baseline["layout"])
            if issues:
                return issues
            policy = baseline["verification_scope"]
            if policy["mode"] != "libraries":
                return ["unexpected stored verification scope mode"]
            verified = set(policy["verification_modules"].values())
            editable = set(policy["editable_modules"].values())
            if (any(row.get("module") not in verified for row in baseline["declarations"].values())
                    or any(baseline["declarations"][name].get("module") not in editable
                           for name in baseline["scope"]["existing_targets"])):
                return ["baseline declaration/target crosses the verification boundary"]
        _records({"project_records": baseline["declarations"],
                  "project_declarations": [{"name": name, "module": row["module"], "kind": row["target_kind"]}
                                           for name, row in baseline["declarations"].items()],
                  **{key: baseline[key] for key in ("project_axioms", "project_sorries", "project_used_axioms")}})
    except (ValueError, KeyError, TypeError, AttributeError):
        return ["existing-project baseline has incomplete native declaration evidence"]
    return []


def baseline_is_valid(baseline: dict) -> bool:
    """Cheap checksum/schema guard; not a substitute for native verification."""
    return not baseline_errors(baseline)


def require_original_branch(root: Path, baseline: dict) -> None:
    """Controller continuation guard; call on the original project checkout."""
    if baseline.get("policy") in {"migration-v1", "migration-v2"}:
        if str(Path(root).resolve()) != baseline["project_root"] or _git(root, "branch", "--show-current") != baseline["branch"]:
            raise ValueError("Bump target branch identity changed")
        return
    branch = _git(Path(root).resolve(), "branch", "--show-current")
    if branch != baseline.get("branch"):
        raise ValueError("existing project branch changed from the preserved baseline")


def require_pinned_inputs(root: Path, baseline: dict, *, allowed_new_paths=()) -> None:
    """Read-only pre-Lake guard for a previously trusted project snapshot.

    Inspect configuration bytes before reading its dependency manifest, and
    inspect dependency sources before any Lake configuration/elaboration runs.
    This is preservation checking, not an adversarial Lean execution sandbox.
    """
    if baseline.get("policy") in {"migration-v1", "migration-v2"}:
        _require_migration_inputs(root, baseline)
        return
    from . import bump_contract as contract, bump_scope
    errors = baseline_errors(baseline)
    if errors:
        raise ValueError("; ".join(errors))
    root = Path(root).resolve()
    allowed = set(allowed_new_paths)
    if any(not isinstance(path, str) or not _safe_path(path) or Path(path).suffix != ".lean"
           for path in allowed):
        raise ValueError("invalid controller-approved supporting source path")
    bump_scope.require_writable_paths(baseline, allowed)

    def file_hash(name: str) -> str:
        if not _safe_path(name):
            raise ValueError("unsafe path in existing-project baseline")
        path = root / name
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root and parent.is_relative_to(root)):
            raise ValueError(f"pinned project input became a symlink: {name}")
        if not path.is_file():
            raise ValueError(f"pinned project input is missing or not a regular file: {name}")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    # Even a newly introduced second Lake configuration is a policy change.
    actual_config = {name: file_hash(name) for name in sorted(contract._CONFIGS)
                     if (root / name).exists() or (root / name).is_symlink()}
    if actual_config != baseline["environment"].get("config", {}):
        raise ValueError("pinned project toolchain/build configuration changed before Lake execution")

    selected = set(baseline["scope"]["existing_targets"])
    modules = {baseline["declarations"][name]["module"] for name in selected
               if name in baseline["declarations"]}
    writable = {name for name, module in baseline["layout"]["modules"].items() if module in modules}
    writable.update(allowed)
    for name, expected in baseline["files"].items():
        # Check existence/symlinks even for a writable proof module. All
        # non-target modules stay byte-locked unless a controller manifest
        # expressly permits a new supporting declaration in that module.
        actual = file_hash(name)
        if name not in writable and actual != expected:
            raise ValueError(f"pinned project input changed before Lake execution: {name}")
    if contract._dependencies(root) != baseline["environment"].get("dependencies", {}):
        raise ValueError("pinned dependency source bytes changed before Lake execution")
    if baseline.get("version") == 2:
        from . import bump_delta
        current_files = contract._file_hashes(root, build_dir=baseline["layout"]["build_dir"])
        extra = set(current_files) - set(baseline["files"]) - allowed
        if extra:
            raise ValueError("unapproved new project input before Lake execution: " + ", ".join(sorted(extra)))
        bump_delta.require_source_edits(root, baseline, allowed_new_paths=writable)


def _output_names(dag: dict) -> set[str]:
    names = set()
    for chunk in dag.get("chunks", []):
        if isinstance(chunk.get("lean_decl"), str) and chunk["lean_decl"]:
            names.add(chunk["lean_decl"])
        for output in chunk.get("outputs", []):
            if isinstance(output, dict):
                name = output.get("declaration", output.get("lean_decl"))
                if isinstance(name, str):
                    names.add(name)
    return names


def bind_scope(baseline: dict, dag: dict, *, root: Path | None = None) -> dict:
    if baseline.get("policy") == "migration-v1":
        if baseline_errors(baseline):
            raise ValueError("invalid migration baseline")
        if {row.get("task_id", row.get("id")) for row in dag.get("chunks", [])} != set(baseline["compiler_modules"]):
            raise ValueError("migration plan must cover every original module")
        return copy.deepcopy(baseline)
    """Bind natural-language scope to exact planned existing names once.

    Mechanical binding only grants completion rights to pre-existing holes. The
    independent critic still judges whether those names cover the user's words.
    """
    errors = baseline_errors(baseline)
    if errors:
        raise ValueError("; ".join(errors))
    if baseline.get("version") == 2:
        from . import bump_delta
        return bump_delta.bind_scope(root, baseline, dag)
    result = copy.deepcopy(baseline)
    outputs = _output_names(dag)
    existing_outputs = outputs & baseline["declarations"].keys()
    scope = result["scope"]
    proposed = dag.get("existing_targets")
    if not isinstance(proposed, list) or any(not isinstance(name, str) for name in proposed):
        raise ValueError("plan requires existing_targets listing exact existing declaration names")
    if len(proposed) != len(set(proposed)):
        raise ValueError("existing_targets must not contain duplicate declaration names")
    if not scope.get("bound"):
        selected = set(proposed)
        incomplete = set(baseline["project_axioms"]) | set(baseline["project_sorries"])
        if not selected <= incomplete:
            raise ValueError("natural-language plan may only select existing incomplete declarations: "
                             + ", ".join(sorted(selected - incomplete)))
        scope.update(existing_targets=sorted(selected), bound=True)
    selected = set(scope["existing_targets"])
    policy = baseline.get("verification_scope")
    if policy is not None:
        editable = set(policy["editable_modules"].values())
        if any(baseline["declarations"][name]["module"] not in editable for name in selected):
            raise ValueError("plan cannot select read-only auxiliary declarations")
    _require_editable_targets(selected, baseline["declarations"],
                              set(baseline["project_axioms"]) | set(baseline["project_sorries"]))
    if set(proposed) != selected:
        raise ValueError("plan omits or changes selected existing targets: " + ", ".join(sorted(selected ^ set(proposed))))
    if existing_outputs - selected:
        raise ValueError("plan claims protected existing declarations: "
                         + ", ".join(sorted(existing_outputs - selected)))
    result.setdefault("origin_sha256", baseline["sha256"])
    return _seal(result)


def _signature(row: dict) -> dict:
    # Type ASTs are authoritative. Pretty-printed signatures are never identity.
    return {key: row.get(key) for key in ("name", "module", "level_params", "type")}


def _preserved_meaning(row: dict) -> dict:
    return {key: row.get(key) for key in
            ("name", "module", "target_kind", "level_params", "type", "declaration_meaning", "proof_body")}


def validate_baseline(root: Path, baseline: dict, inspection: dict | None = None, *,
                      final: bool = False, allowed_new_paths=(), allowed_incomplete_declarations=(),
                      claimed_declarations=None, allowed_incomplete_files=None) -> list[str]:
    """Check original project preservation without changing sources or Git state.

    Caller builds fresh inputs before inspection. Allowed new paths/placeholder
    names must come from controller-validated manifests, never unchecked prose.
    """
    if baseline.get("policy") == "migration-v1":
        try:
            _require_migration_inputs(root, baseline)
            return []
        except (ValueError, OSError) as exc:
            return [str(exc)]
    from . import bump_contract as contract, bump_scope
    errors = baseline_errors(baseline)
    if errors:
        return errors
    root = Path(root).resolve()
    if baseline.get("version") == 2:
        from . import bump_delta
        return bump_delta.validate(root, baseline, inspection, final=final,
            allowed_new_paths=allowed_new_paths,
            allowed_incomplete_declarations=allowed_incomplete_declarations,
            claimed_declarations=claimed_declarations, allowed_incomplete_files=allowed_incomplete_files)
    selected = set(baseline["scope"]["existing_targets"])
    if final and not baseline["scope"].get("bound"):
        errors.append("existing-project scope has not been bound to exact targets")
    new_paths = set(allowed_new_paths)
    if any(not isinstance(path, str) or not _safe_path(path) or Path(path).suffix != ".lean"
           for path in new_paths):
        return errors + ["invalid controller-approved supporting source path"]
    try:
        # Private Git worktrees legitimately use task branches. The original
        # checkout (including an original that itself is a worktree) must not.
        if str(root) == baseline.get("project_root") or (root / ".git").is_dir():
            require_original_branch(root, baseline)
        require_pinned_inputs(root, baseline, allowed_new_paths=new_paths)
        if _git(root, "merge-base", "--is-ancestor", baseline["head"], "HEAD"):
            errors.append("current project no longer descends from the preserved baseline")
        layout = contract.workspace_layout(root)
        if baseline.get("verification_scope") is not None:
            layout = bump_scope.apply(root, layout, baseline["verification_scope"])
            if new_paths - set(layout["editable_modules"]):
                raise ValueError("supporting outputs must be owned by selected Lean libraries: "
                                 + ", ".join(sorted(new_paths - set(layout["editable_modules"]))))
        current_files = contract._file_hashes(root, build_dir=layout["build_dir"])
        if contract.environment_identity(root) != baseline["environment"]:
            errors.append("existing-project toolchain, build configuration or dependency bytes changed")
        if inspection is None:
            inspection = contract.inspect_environment(root, [], layout=layout, _inventory_only=True)
        records = _records(inspection)
        if baseline.get("verification_scope") is not None and any(
                row["module"] not in set(layout["verification_modules"].values()) for row in records.values()):
            raise ValueError("native preservation inventory escaped the verification scope")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        return errors + ["cannot verify existing-project preservation: " + str(exc)]
    old = baseline["declarations"]
    mutable_modules = {old[name]["module"] for name in selected}
    mutable_paths = {name for name, module in baseline["layout"]["modules"].items()
                     if module in mutable_modules}
    # A manifest may add a genuinely new helper in an existing Lean file. Its
    # original declarations still undergo exact kernel/body preservation below.
    mutable_paths.update(new_paths & baseline["files"].keys())
    for path, digest in baseline["files"].items():
        if path not in current_files:
            errors.append(f"existing project input deleted: {path}")
        elif current_files[path] != digest and path not in mutable_paths:
            errors.append(f"protected project input changed: {path}")
    for path in current_files.keys() - baseline["files"].keys():
        if path not in new_paths:
            errors.append(f"unapproved new project input: {path}")
    for path, module in baseline["layout"]["modules"].items():
        if layout["modules"].get(path) != module:
            errors.append(f"existing module ownership changed: {path}")
    incomplete = set(baseline["project_sorries"]) | set(baseline["project_axioms"])
    for name, previous in old.items():
        current = records.get(name)
        if current is None:
            errors.append(f"existing declaration removed: {name}")
            continue
        if name not in selected:
            if _preserved_meaning(current) != _preserved_meaning(previous):
                errors.append(f"protected existing declaration changed: {name}")
            continue
        if name not in incomplete:
            if _preserved_meaning(current) != _preserved_meaning(previous):
                label = "definition" if previous["target_kind"] not in {"axiom", "theorem"} else "declaration"
                errors.append(f"completed existing {label} changed: {name}")
            continue
        if _signature(current) != _signature(previous):
            errors.append(f"existing target signature or namespace/module changed: {name}")
        before_kind, after_kind = previous["target_kind"], current["target_kind"]
        if after_kind != before_kind and (before_kind, after_kind) != ("axiom", "theorem"):
            errors.append(f"existing target declaration kind changed: {name}")
        if before_kind not in {"axiom", "theorem"}:
            # Incomplete bodies are writable. Safety, reducibility, constructors,
            # recursors and mutual-family metadata remain fixed.
            before_meaning = {k: v for k, v in previous["declaration_meaning"].items() if k != "value"}
            after_meaning = {k: v for k, v in current["declaration_meaning"].items() if k != "value"}
            if before_meaning != after_meaning:
                errors.append(f"existing partial definition metadata changed: {name}")
    new_axioms = set(inspection["project_axioms"]) - set(baseline["project_axioms"])
    if new_axioms:
        errors.append("new project axioms: " + ", ".join(sorted(new_axioms)))
    permitted_sorries = set(baseline["project_sorries"])
    if not final:
        permitted = set(allowed_incomplete_declarations)
        if allowed_incomplete_files is not None:
            if (not isinstance(allowed_incomplete_files, dict)
                    or any(not isinstance(name, str) or not isinstance(files, (set, list, tuple))
                           or any(not isinstance(path, str) or path not in new_paths for path in files)
                           for name, files in allowed_incomplete_files.items())):
                return errors + ["invalid exact-file placeholder provenance"]
            permitted &= {name for name, files in allowed_incomplete_files.items()
                          if name in records and records[name]["target_kind"] == "theorem"
                          and any(layout["modules"].get(path) == records[name]["module"] for path in files)
                          and '"sorryAx"' not in json.dumps([
                              records[name]["type"], records[name]["declaration_meaning"]])}
        permitted_sorries.update(permitted)
    new_sorries = set(inspection["project_sorries"]) - permitted_sorries
    if new_sorries:
        errors.append("new out-of-scope proof holes: " + ", ".join(sorted(new_sorries)))
    new_axiom_uses = (set(inspection["project_used_axioms"]) - set(baseline["project_used_axioms"])
                      - contract.AXIOMS - {"sorryAx"})
    if new_axiom_uses:
        errors.append("new forbidden axiom dependencies: " + ", ".join(sorted(new_axiom_uses)))
    if final:
        unfinished = selected & (set(inspection["project_axioms"]) | set(inspection["project_sorries"]))
        if unfinished:
            errors.append("selected existing targets remain incomplete: " + ", ".join(sorted(unfinished)))
    return errors
