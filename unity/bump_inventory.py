"""Compact native declaration identities and dependencies for migration planning.

This inventory schedules work; it never certifies successful migration. Detailed
local meanings and trust are requested separately by the native final checker.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import artifacts, bump_jobs, bump_native

MAX_INDEX_BYTES = 64 * 1024 * 1024


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def name_key(value) -> tuple:
    if value == ["anonymous"]:
        return ("anonymous",)
    if not isinstance(value, list) or len(value) != 3 or value[0] not in {"str", "num"}:
        raise ValueError("invalid structural Lean name")
    if (value[0] == "str" and not isinstance(value[2], str)) or (
            value[0] == "num" and (type(value[2]) is not int or value[2] < 0)):
        raise ValueError("invalid structural Lean name component")
    return value[0], name_key(value[1]), value[2]


def occurrence_id(module: str, name_ast) -> str:
    name_key(name_ast)
    if not isinstance(module, str) or not module:
        raise ValueError("missing declaration module")
    return "occ-" + digest([module, name_ast])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native JSON key")
        result[key] = value
    return result


def read_native_module(root: Path, module: str, *, local_meanings: bool = False,
                       environment: dict | None = None, executable: Path | None = None) -> dict:
    root = Path(root).resolve()
    executable = executable or bump_native.executable(root, Path(__file__).with_suffix(".lean"),
                                                       name="inventory")
    command = ["lake", "env", str(executable), module]
    if local_meanings:
        command.append("--local-meanings")
    result = bump_jobs.run(root, command, cwd=root, task_id="inventory:" + module,
                           serialize_build=True)
    if result.returncode:
        raise ValueError("native declaration inventory failed: " + module)
    if not local_meanings and len(result.stdout.encode()) > MAX_INDEX_BYTES:
        raise ValueError("compact native inventory exceeds size bound")
    report = json.loads(result.stdout, object_pairs_hook=_unique)
    mode = "local-meanings" if local_meanings else "index"
    if (report.get("schema_version") != 1 or report.get("mode") != mode
            or report.get("module") != module or report.get("declaration_inventory") != "raw-module-constants-v1"
            or not isinstance(report.get("declarations"), list)
            or report.get("raw_declaration_count") != len(report["declarations"])):
        raise ValueError("incomplete native declaration inventory")
    identities = [name_key(row["name_ast"]) for row in report["declarations"]]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate native declaration identity")
    return report


def assemble_index(reports: dict, graph: dict, source_files: dict, *, scope_sha256: str,
                   environment: dict, native_refs: dict | None = None) -> dict:
    if set(reports) != set(graph):
        raise ValueError("selected modules and native reports differ")
    occurrences, modules = {}, {}
    for module, entry in sorted(graph.items()):
        report = reports[module]
        if report.get("mode") != "index" or report.get("raw_declaration_count") != len(report["declarations"]):
            raise ValueError("incomplete or noncompact module inventory")
        ids = []
        for row in report["declarations"]:
            if "meaning" in row or "axioms" in row:
                raise ValueError("structural closure cannot enter the scheduling inventory")
            key = occurrence_id(module, row["name_ast"])
            if key in occurrences:
                raise ValueError("duplicate original occurrence")
            local, external = [], []
            for reference in row["dependencies"]:
                name_key(reference["name_ast"])
                if reference["module"] in graph:
                    local.append(occurrence_id(reference["module"], reference["name_ast"]))
                else:
                    external.append(reference)
            path = entry["path"]
            occurrences[key] = {"module": module, "name_ast": row["name_ast"],
                "display_name": row["display_name"], "kind": row["kind"], "path": path,
                "source_sha256": source_files[path], "range": row.get("range"),
                "range_origin": "native" if row.get("range") else "module",
                "dependencies": sorted(set(local)), "external_dependencies": external,
                "direct_sorry": row["direct_sorry"], "is_internal": row.get("is_internal", False)}
            ids.append(key)
        modules[module] = {"path": entry["path"], "imports": sorted(set(entry["imports"])),
            "source_sha256": source_files[entry["path"]], "occurrence_ids": sorted(ids),
            "native_artifact_ref": (native_refs or {}).get(module)}
        if "line_lengths" in entry:
            modules[module]["line_lengths"] = list(entry["line_lengths"])
    result = {"schema_version": 1, "kind": "bump-original-index-v1", "modules": modules,
              "occurrences": occurrences, "environment": environment,
              "source_sha256": digest(source_files), "scope_sha256": scope_sha256}
    result["index_sha256"] = digest(result)
    validate_index(result)
    return result


def validate_index(index: dict) -> None:
    if (index.get("schema_version") != 1 or index.get("kind") != "bump-original-index-v1"
            or index.get("index_sha256") != digest({k: v for k, v in index.items() if k != "index_sha256"})):
        raise ValueError("original declaration inventory seal is invalid")
    rows, modules = index["occurrences"], index["modules"]
    owned = []
    for module, entry in modules.items():
        if "line_lengths" in entry and (not isinstance(entry["line_lengths"], list)
                or any(type(length) is not int or length < 0 for length in entry["line_lengths"])):
            raise ValueError("invalid original source line lengths")
        owned.extend(entry["occurrence_ids"])
        if any(rows[key]["module"] != module or rows[key]["path"] != entry["path"]
               for key in entry["occurrence_ids"]):
            raise ValueError("declaration inventory module ownership differs")
    if len(set(owned)) != len(owned) or set(owned) != set(rows):
        raise ValueError("declaration inventory does not cover all original occurrences")
    for key, row in rows.items():
        if key != occurrence_id(row["module"], row["name_ast"]) or set(row["dependencies"]) - set(rows):
            raise ValueError("invalid original declaration identity or dependency")
        if "meaning" in row or "axioms" in row:
            raise ValueError("scheduling inventory contains noncompact evidence")


def capture_original_index(original_root: Path, sealed_scope: dict, *, artifact_dir: Path) -> dict:
    from . import bump_contract, bump_preparation, bump_workspace
    root = Path(original_root).resolve()
    before = bump_preparation.source_files(root, build_dir=sealed_scope["build_dir"])
    selected = sealed_scope["selected_modules"]
    headers = bump_workspace.read_imports(root, list(selected.values()))
    graph = {module: {"path": path, "imports": [name for name in headers[path] if name in selected],
                      "line_lengths": [len(line) for line in (root / path).read_text().splitlines()]}
             for module, path in selected.items()}
    environment = bump_contract.environment_identity(root)
    executable = bump_native.executable(root, Path(__file__).with_suffix(".lean"), name="inventory")
    reports, refs = {}, {}
    for module in sorted(graph):
        report = read_native_module(root, module, executable=executable)
        record = artifacts.store_text(artifact_dir, json.dumps(report, sort_keys=True),
                                      kind="bump_original_module_index", producer="Unity", source=module)
        reports[module], refs[module] = report, {"artifact_id": record["artifact_id"], "sha256": record["sha256"]}
    if (bump_preparation.source_files(root, build_dir=sealed_scope["build_dir"]) != before
            or bump_contract.environment_identity(root) != environment):
        raise ValueError("original inputs changed while capturing declaration inventory")
    index = assemble_index(reports, graph, before, scope_sha256=sealed_scope["sha256"],
                           environment=environment, native_refs=refs)
    record = artifacts.store_text(artifact_dir, json.dumps(index, sort_keys=True),
                                  kind="bump_original_index", producer="Unity")
    return {"index": index, "index_ref": {"artifact_id": record["artifact_id"], "sha256": record["sha256"]},
            "index_sha256": index["index_sha256"], "module_count": len(graph), "occurrence_count": len(index["occurrences"])}


def load_original_index(artifact_root: Path, reference: dict) -> dict:
    raw = artifacts.artifact_bytes(artifact_root, reference["artifact_id"])
    if len(raw) > MAX_INDEX_BYTES or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("original index artifact identity differs")
    index = json.loads(raw, object_pairs_hook=_unique)
    validate_index(index)
    return index
