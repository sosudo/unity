"""Small immutable original-occurrence index for Bump task discovery.

This is scheduling/source-location evidence, NOT a semantic equivalence or
proof-trust verdict. Full local checker evidence is requested lazily through
``read_native_module(..., local_meanings=True)`` and never put in this index.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import artifacts, bump_migration_project as project

MAX_INDEX_BYTES = 64 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = {"theorem", "def", "axiom", "opaque", "inductive", "constructor", "recursor", "quot"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def name_key(value) -> tuple:
    if not isinstance(value, list) or not value:
        raise ValueError("invalid typed Lean name")
    if value == ["anonymous"]:
        return ("anonymous",)
    if len(value) != 3 or value[0] not in {"str", "num"}:
        raise ValueError("invalid typed Lean name")
    parent = name_key(value[1])
    if (value[0] == "str" and not isinstance(value[2], str)) or (
            value[0] == "num" and (type(value[2]) is not int or value[2] < 0)):
        raise ValueError("invalid typed Lean name component")
    return value[0], parent, value[2]


def occurrence_id(module: str, name_ast) -> str:
    if not isinstance(module, str) or not module:
        raise ValueError("occurrence module is missing")
    name_key(name_ast)
    return "occ-" + digest(["bump-original-occurrence-v1", module, name_ast])


def _path(path: str) -> bool:
    return (isinstance(path, str) and bool(path) and not Path(path).is_absolute()
            and ".." not in Path(path).parts and Path(path).as_posix() == path)


def _range(value) -> bool:
    keys = {"start_line", "start_column", "end_line", "end_column"}
    return (isinstance(value, dict) and set(value) == keys
            and all(type(value[k]) is int and value[k] >= (1 if k.endswith("line") else 0) for k in keys)
            and (value["start_line"], value["start_column"]) <= (value["end_line"], value["end_column"]))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key in Bump inventory")
        result[key] = value
    return result


def read_native_module(root: Path, module: str, *, local_meanings: bool = False,
                       environment: dict | None = None, executable: Path | None = None) -> dict:
    """Native raw occurrence report; no build, model, or dependency resolution.

    Callers must bind source/environment/build freshness around this operation.
    The local-meanings mode is deliberately not accepted by ``assemble_index``.
    """
    from . import bump_migration_contract as native
    root = Path(root).resolve()
    environment = environment or native._environment(root)
    executable = executable or native._native_executable(root, Path(__file__).with_suffix(".lean"), environment)
    command = ["lake", "env", str(executable), module]
    if local_meanings:
        command.append("--local-meanings")
    result = project._run(root, command, timeout=300)
    if result.returncode:
        raise ValueError("Bump original-index native inspection failed (diagnostics retained by caller)")
    if len(result.stdout.encode()) > MAX_INDEX_BYTES and not local_meanings:
        raise ValueError("Bump original-index module exceeds explicit size bound")
    report = json.loads(result.stdout, object_pairs_hook=_unique)
    expected_mode = "local-meanings" if local_meanings else "index"
    if (report.get("schema_version") != 1 or report.get("mode") != expected_mode
            or report.get("module") != module or report.get("declaration_inventory") != "raw-module-constants-v1"
            or not isinstance(report.get("declarations"), list)
            or type(report.get("raw_declaration_count")) is not int
            or len(report["declarations"]) != report["raw_declaration_count"]):
        raise ValueError("malformed Bump raw inventory")
    seen = set()
    for row in report["declarations"]:
        key = name_key(row.get("name_ast"))
        if key in seen:
            raise ValueError("duplicate raw declaration identity")
        seen.add(key)
    return report


def read_native_imports(root: Path, filename: str, *, environment: dict | None = None,
                        executable: Path | None = None) -> dict:
    """Parser-only header evidence, retaining imports observed before syntax errors."""
    from . import bump_migration_contract as native, bump_workspace
    root = Path(root).resolve()
    bump_workspace._checked_files(root, [filename])
    environment = environment or native._environment(root)
    executable = executable or native._native_executable(root, Path(__file__).with_suffix(".lean"), environment)
    result = project._run(root, ["lake", "env", str(executable), "--imports", filename], timeout=300)
    if result.returncode:
        raise ValueError("Bump native import parser failed operationally")
    report = json.loads(result.stdout, object_pairs_hook=_unique)
    if (not isinstance(report, dict) or set(report) != {"schema_version", "mode", "path", "imports", "header_errors"}
            or report["schema_version"] != 1 or report["mode"] != "imports" or report["path"] != filename
            or type(report["header_errors"]) is not bool or not isinstance(report["imports"], list)
            or any(not isinstance(name, str) or not name for name in report["imports"])
            or len(report["imports"]) != len(set(report["imports"]))):
        raise ValueError("Malformed native import parser report")
    return report


def assemble_index(reports: dict, graph: dict, source_files: dict, *, scope_sha256: str,
                   environment: dict, native_refs: dict | None = None) -> dict:
    """Pure, deterministic assembly; no name flattening or dependency clipping."""
    if set(reports) != set(graph):
        raise ValueError("native inventory and sealed module graph differ")
    occurrences, modules = {}, {}
    by_name = {}
    for owner, report in reports.items():
        for row in report.get("declarations", []):
            by_name.setdefault(name_key(row["name_ast"]), set()).add(owner)
    for module, entry in sorted(graph.items()):
        report = reports[module]
        if (report.get("mode") != "index" or report.get("module") != module
                or report.get("declaration_inventory") != "raw-module-constants-v1"
                or type(report.get("raw_declaration_count")) is not int
                or len(report.get("declarations", [])) != report["raw_declaration_count"]):
            raise ValueError("incomplete or non-slim original inventory")
        ids = []
        for row in report["declarations"]:
            if "meaning" in row or "axioms" in row:
                raise ValueError("detailed evidence cannot enter scheduling index")
            key = occurrence_id(module, row["name_ast"])
            if key in occurrences:
                raise ValueError("duplicate original occurrence")
            references = row.get("dependencies", [])
            if not isinstance(references, list):
                raise ValueError("invalid native dependency list")
            local, external = [], []
            for ref in references:
                if not isinstance(ref, dict) or set(ref) != {"module", "name_ast"}:
                    raise ValueError("malformed native dependency identity")
                ref_id = occurrence_id(ref["module"], ref["name_ast"])
                if ref["module"] in graph:
                    owners = by_name.get(name_key(ref["name_ast"]), set())
                    if module in owners:
                        local.append(occurrence_id(module, ref["name_ast"]))
                    else:
                        reachable = owners & set(report.get("imported_modules", [ref["module"]]))
                        local.extend(occurrence_id(owner, ref["name_ast"]) for owner in reachable)
                        if not reachable:
                            local.append(ref_id)  # validation rejects a missing occurrence.
                else:
                    external.append(ref)
            path = entry["path"]
            occurrences[key] = {"module": module, "name_ast": row["name_ast"],
                "display_name": row["display_name"], "kind": row["kind"], "path": path,
                "source_sha256": source_files[path], "range": row.get("range"),
                "range_origin": "native" if row.get("range") is not None else "module",
                "dependencies": sorted(set(local)),
                "external_dependencies": sorted({digest(ref): ref for ref in external}.values(), key=digest),
                "direct_sorry": row["direct_sorry"], "is_internal": row.get("is_internal", False)}
            ids.append(key)
        modules[module] = {"path": entry["path"], "source_sha256": source_files[entry["path"]],
            "imports": sorted(set(entry["imports"])), "occurrence_ids": sorted(ids),
            "native_artifact_ref": (native_refs or {}).get(module)}
    index = {"schema_version": 1, "kind": "bump-original-index-v1",
        "source_sha256": digest(source_files), "scope_sha256": scope_sha256,
        "environment": environment, "modules": modules, "occurrences": occurrences}
    index["index_sha256"] = digest(index)
    validate_index(index)
    return index


def validate_index(index: dict) -> None:
    fields = {"schema_version", "kind", "source_sha256", "scope_sha256", "environment", "modules", "occurrences", "index_sha256"}
    if (not isinstance(index, dict) or set(index) != fields or index.get("schema_version") != 1
            or index.get("kind") != "bump-original-index-v1"
            or any(not isinstance(index.get(k), str) or not _SHA.fullmatch(index[k])
                   for k in ("source_sha256", "scope_sha256", "index_sha256"))
            or index["index_sha256"] != digest({k: v for k, v in index.items() if k != "index_sha256"})):
        raise ValueError("invalid original-index seal")
    modules, rows = index["modules"], index["occurrences"]
    if not isinstance(modules, dict) or not modules or not isinstance(rows, dict) or not isinstance(index["environment"], dict):
        raise ValueError("invalid original-index inventory")
    owned, files = set(), set()
    for module, group in modules.items():
        if (not isinstance(module, str) or not module or not isinstance(group, dict)
                or set(group) != {"path", "source_sha256", "imports", "occurrence_ids", "native_artifact_ref"}
                or not _path(group["path"]) or group["path"] in files
                or not isinstance(group["source_sha256"], str) or not _SHA.fullmatch(group["source_sha256"])
                or not isinstance(group["imports"], list) or group["imports"] != sorted(set(group["imports"]))
                or set(group["imports"]) - set(modules) or module in group["imports"]
                or not isinstance(group["occurrence_ids"], list)
                or group["occurrence_ids"] != sorted(set(group["occurrence_ids"]))):
            raise ValueError("invalid original module group")
        files.add(group["path"])
        for key in group["occurrence_ids"]:
            if key in owned or key not in rows or rows[key].get("module") != module:
                raise ValueError("original occurrence ownership is not a partition")
            owned.add(key)
    if owned != set(rows):
        raise ValueError("original inventory drops an occurrence")
    row_fields = {"module", "name_ast", "display_name", "kind", "path", "source_sha256", "range",
                  "range_origin", "dependencies", "external_dependencies", "direct_sorry", "is_internal"}
    for key, row in rows.items():
        if not isinstance(row, dict) or set(row) != row_fields:
            raise ValueError("non-slim original occurrence schema")
        group = modules[row["module"]]
        if (key != occurrence_id(row["module"], row["name_ast"]) or row["path"] != group["path"]
                or row["source_sha256"] != group["source_sha256"] or row["kind"] not in _KINDS
                or not isinstance(row["display_name"], str) or type(row["direct_sorry"]) is not bool
                or type(row["is_internal"]) is not bool
                or row["range_origin"] != ("native" if row["range"] is not None else "module")
                or (row["range"] is not None and not _range(row["range"]))
                or not isinstance(row["dependencies"], list)
                or row["dependencies"] != sorted(set(row["dependencies"]))
                or set(row["dependencies"]) - set(rows)
                or not isinstance(row["external_dependencies"], list)):
            raise ValueError("invalid original occurrence fields")
        for ref in row["external_dependencies"]:
            if (not isinstance(ref, dict) or set(ref) != {"module", "name_ast"}
                    or not isinstance(ref["module"], str) or not ref["module"] or ref["module"] in modules):
                raise ValueError("invalid external dependency reference")
            name_key(ref["name_ast"])


def load_original_index(artifact_root: Path, reference: dict) -> dict:
    if not isinstance(reference, dict) or set(reference) != {"artifact_id", "sha256"}:
        raise ValueError("invalid original index reference")
    info = artifacts.artifact_info(artifact_root, reference["artifact_id"])
    if info.get("sha256") != reference["sha256"] or not 0 < info.get("bytes", 0) <= MAX_INDEX_BYTES:
        raise ValueError("original index artifact identity or size changed")
    raw = artifacts.artifact_bytes(artifact_root, reference["artifact_id"])
    if len(raw) != info["bytes"] or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("original index bytes changed")
    result = json.loads(raw, object_pairs_hook=_unique)
    validate_index(result)
    return result


def capture_original_index(original_root: Path, sealed_scope: dict, *, artifact_dir: Path) -> dict:
    """Capture once after old build; return only immutable refs/counts/seals."""
    from . import bump_migration_contract as native
    root = Path(original_root).resolve()
    if project.validate_build_scope(root, sealed_scope):
        raise ValueError("original index scope is not current")
    before = project.source_files(root)
    graph = project.compiler_modules(root, scope=sealed_scope)
    environment = native._environment(root)
    executable = native._native_executable(root, Path(__file__).with_suffix(".lean"), environment)
    reports, refs = {}, {}
    for module in sorted(graph):
        report = read_native_module(root, module, environment=environment, executable=executable)
        encoded = json.dumps(report, sort_keys=True, separators=(",", ":"))
        record = artifacts.store_text(artifact_dir, encoded, kind="bump_original_module_index", producer="Unity", source=module)
        reports[module] = report
        refs[module] = {"artifact_id": record["artifact_id"], "sha256": record["sha256"]}
    if project.source_files(root) != before or native._environment(root) != environment:
        raise ValueError("original source/environment changed during index capture")
    index = assemble_index(reports, graph, before, scope_sha256=sealed_scope["sha256"],
                           environment=environment, native_refs=refs)
    encoded = json.dumps(index, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > MAX_INDEX_BYTES:
        raise ValueError("original index exceeds explicit size bound")
    record = artifacts.store_text(artifact_dir, encoded, kind="bump_original_index", producer="Unity")
    return {"index_ref": {"artifact_id": record["artifact_id"], "sha256": record["sha256"]},
        "index_sha256": index["index_sha256"], "module_count": len(graph),
        "occurrence_count": len(index["occurrences"]), "source_sha256": index["source_sha256"],
        "scope_sha256": index["scope_sha256"]}
