"""Compact, occurrence-bound migration policy 2.

Local kernel structure and trust are machine evidence.  An explicitly declared
change is not equivalence: it remains a source-bound semantic-review obligation.
No recursive equality of the old and new upstream libraries is performed here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"artifact-[0-9a-f]{12}")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def seal(value: dict) -> dict:
    value = copy.deepcopy(value)
    value.pop("sha256", None)
    value["sha256"] = digest(value)
    return value


def _sha(value) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate evidence JSON key")
        value[key] = item
    return value


def read_artifact(artifact_root: Path, ref: dict, *, decode_json: bool = True):
    """Read authenticated artifact bytes without creating directories or records."""
    if (not isinstance(ref, dict) or not _ID.fullmatch(str(ref.get("artifact_id", "")))
            or not _sha(ref.get("sha256"))):
        raise ValueError("invalid migration artifact reference")
    root = Path(artifact_root)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise ValueError("migration artifact authority is not canonical")
    record_path = root / "records" / (ref["artifact_id"] + ".json")
    blob_path = root / "blobs" / ref["sha256"]
    for path in (root / "records", root / "blobs", record_path, blob_path):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("migration artifact path escaped its authority")
    record_bytes = record_path.read_bytes()
    record = json.loads(record_bytes, object_pairs_hook=_unique)
    payload = blob_path.read_bytes()
    if (record.get("artifact_id") != ref["artifact_id"] or record.get("sha256") != ref["sha256"]
            or record.get("bytes") != len(payload)
            or hashlib.sha256(payload).hexdigest() != ref["sha256"]
            or record_path.read_bytes() != record_bytes):
        raise ValueError("migration artifact identity or bytes changed")
    if not decode_json:
        if not payload:
            raise ValueError("correspondence evidence cannot be empty")
        return payload
    return json.loads(payload, object_pairs_hook=_unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON evidence")))


def store_artifact(artifact_root: Path, value: dict, *, kind: str) -> dict:
    from . import artifacts
    record = artifacts.store_text(artifact_root, json.dumps(value, sort_keys=True,
        ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n",
        kind=kind, producer="Unity")
    return {"artifact_id": record["artifact_id"], "sha256": record["sha256"]}


def resolved_baseline(root: Path, contract: dict) -> dict:
    from . import bump_project
    baseline = read_artifact(Path(contract["artifact_root"]), contract["baseline_ref"])
    if (baseline.get("policy") != "migration-v2" or not bump_project.baseline_is_valid(baseline)
            or baseline.get("sha256") != contract.get("project_baseline_sha256")
            or baseline.get("original_index_ref") != contract.get("original_index_ref")
            or baseline.get("artifact_root") != contract.get("artifact_root")
            or baseline.get("environment") != contract.get("environment")):
        raise ValueError("migration contract baseline reference changed")
    return baseline


def original_index(contract: dict) -> dict:
    from .bump_inventory import load_original_index
    return load_original_index(Path(contract["artifact_root"]), contract["original_index_ref"])


def _targets(index: dict) -> dict:
    return {key: {"occurrence_id": key, "module": row["module"],
        "declaration": row["display_name"], "native_name": copy.deepcopy(row["name_ast"]),
        "target_kind": row["kind"], "file": row["path"],
        "fingerprint": digest({"index": index["index_sha256"], "occurrence": key})}
        for key, row in index["occurrences"].items()}


def default_mapping(index: dict) -> dict:
    return {key: {"original_ids": [key], "mode": "identity", "targets": [{
        "module": row["module"], "declaration": row["display_name"], "file": row["path"],
        "native_name": copy.deepcopy(row["name_ast"])}]}
        for key, row in index["occurrences"].items()}


def task_bindings(index: dict) -> dict:
    return {module: {"modules": [module], "files": [row["path"]],
        "obligation_ids": sorted(row["occurrence_ids"])}
        for module, row in index["modules"].items()}


def source_spec(index: dict, source: dict) -> tuple[list, dict]:
    """All original modules remain review obligations, even with no repair tasks."""
    from .bump_spec import normalize_requirements, normalize_spec
    refs = {row["ref_id"] for row in source.get("source_refs", [])}
    required = {"source:transition.json", "source:original-index.json"}
    required.update("source:project/" + row["path"] for row in index["modules"].values())
    if refs != required:
        raise ValueError("migration source bundle must account for every original module and index")
    anchors = [{"id": "transition", "source_ref": "source:transition.json",
        "location": "Pinned original-to-target transition", "excerpt": "Migrate the selected original project without new trust; upgraded external imports are a disclosed compatibility assumption."},
        {"id": "original-index", "source_ref": "source:original-index.json",
        "location": "Complete immutable occurrence ledger " + index["index_sha256"],
        "excerpt": "All original declaration occurrences remain obligations independently of repair-task discovery."}]
    requirements, arguments = [], []
    for module, row in sorted(index["modules"].items()):
        anchor = "module:" + module
        statement = ("Preserve the original local API, definition behavior and per-declaration trust of "
            + module + "; inspect explicit correspondence for any declared change, and compile its complete target module.")
        anchors.append({"id": anchor, "source_ref": "source:project/" + row["path"],
            "location": "Entire original module and all " + str(len(row["occurrence_ids"])) + " declaration occurrences",
            "excerpt": statement})
        ids = ["transition", "original-index", anchor]
        requirements.append({"id": "preserve:" + module, "statement": statement, "tasks": [module],
            "source_components": ["source:transition.json", "source:original-index.json", "source:project/" + row["path"]],
            "anchor_ids": ids})
        arguments.append({"requirement_id": "preserve:" + module, "anchor_ids": ids,
            "outline": "Machine checks establish exact local mapping, local structure or declared drift, compiled target provenance and no-new-trust. Independently judge source correspondence; compilation alone is not semantic equivalence.",
            "prerequisites": [], "repair_ids": []})
    spec = {"version": 1, "anchors": anchors,
        "scope": {"targets": [row["id"] for row in anchors], "references": [], "excluded": []},
        "prerequisites": [], "arguments": arguments}
    # The immutable review universe is every original module, not the currently
    # active repair queue.  Use the same canonical normalization as publication.
    tasks = {row["tasks"][0]: {"source_components": row["source_components"]}
        for row in requirements}
    requirements = normalize_requirements(requirements, tasks, refs)
    return requirements, normalize_spec(spec, source=source, requirements=requirements, tasks=tasks)


def validate_mapping(contract: dict, mapping: dict | None = None,
                     bindings: dict | None = None) -> None:
    """Pure coverage validation; it never approves a semantic correspondence."""
    mapping = contract.get("mapping") if mapping is None else mapping
    bindings = contract.get("task_bindings") if bindings is None else bindings
    originals = contract.get("targets")
    if not isinstance(originals, dict) or not isinstance(mapping, dict) or not isinstance(bindings, dict):
        raise ValueError("migration mapping is missing its immutable obligation inventory")
    seen, owners, allowed_files, allowed_modules = set(), {}, set(), set()
    for task_id, group in bindings.items():
        if (not isinstance(task_id, str) or not isinstance(group, dict)
                or not isinstance(group.get("modules"), list) or not group["modules"]
                or not isinstance(group.get("files"), list) or not group["files"]
                or not isinstance(group.get("obligation_ids"), list)):
            raise ValueError("invalid migration execution group")
        for key in group["obligation_ids"]:
            if key not in originals or key in owners:
                raise ValueError("migration obligations have missing or duplicate owners")
            owners[key] = task_id
            if (originals[key]["module"] not in group["modules"]
                    or originals[key]["file"] not in group["files"]):
                raise ValueError("migration execution group escapes original module ownership")
        allowed_files.update(group["files"])
        allowed_modules.update(group["modules"])
    if set(owners) != set(originals):
        raise ValueError("migration execution groups omit original obligations")
    target_owners = set()
    for group_id, group in mapping.items():
        if (not isinstance(group_id, str) or not isinstance(group, dict)
                or not isinstance(group.get("original_ids"), list) or not group["original_ids"]
                or not isinstance(group.get("targets"), list) or not group["targets"]
                or group.get("mode") not in {"identity", "declared"}):
            raise ValueError("invalid migration correspondence group")
        ids = group["original_ids"]
        if len(set(ids)) != len(ids) or set(ids) - originals.keys() or seen.intersection(ids):
            raise ValueError("migration correspondence duplicates or invents obligations")
        seen.update(ids)
        # A complete-module/file execution group is still the publication unit.
        if len({owners[key] for key in ids}) != 1:
            raise ValueError("correspondence spans independently published execution groups")
        owner = bindings[owners[ids[0]]]
        for target in group["targets"]:
            from .bump_migration_contract import _name
            if (not isinstance(target, dict) or not isinstance(target.get("declaration"), str)
                    or not target["declaration"] or target.get("module") not in owner["modules"]
                    or target.get("file") not in owner["files"]):
                raise ValueError("mapped target escapes its execution group's write scope")
            identity = (target["module"], _name(target.get("native_name")))
            if identity in target_owners:
                raise ValueError("target declaration belongs to multiple correspondence groups")
            target_owners.add(identity)
        if group["mode"] == "identity":
            if len(ids) != 1 or len(group["targets"]) != 1:
                raise ValueError("identity correspondence must be one-to-one")
        else:
            if (not isinstance(group.get("reason"), str) or not group["reason"].strip()
                    or not isinstance(group.get("relation"), str) or not group["relation"].strip()
                    or not isinstance(group.get("evidence_refs"), list) or not group["evidence_refs"]):
                raise ValueError("declared change requires explicit correspondence evidence")
            for ref in group["evidence_refs"]:
                if (not isinstance(ref, dict) or not _ID.fullmatch(str(ref.get("artifact_id", "")))
                        or not _sha(ref.get("sha256"))):
                    raise ValueError("declared correspondence has invalid evidence reference")
            if any(not _sha(target.get("expected_meaning_sha256")) for target in group["targets"]):
                raise ValueError("declared correspondence must bind every exact target meaning")
    if seen != set(originals):
        raise ValueError("migration correspondence omits original obligations")


def prepare_migration_contract(paths, *, baseline: dict, graph: dict, mapping=None,
                               source: dict | None = None, main_sha: str | None = None) -> dict:
    from . import bump_project, bump_contract
    from .bump_inventory import load_original_index
    if baseline.get("policy") != "migration-v2" or not bump_project.baseline_is_valid(baseline):
        raise ValueError("migration policy 2 requires a sealed compact baseline")
    index = load_original_index(Path(baseline["artifact_root"]), baseline["original_index_ref"])
    source = source or {}
    groups = task_bindings(index)
    mapping = default_mapping(index) if mapping is None else copy.deepcopy(mapping)
    outputs = {task: sorted(({"declaration": target["declaration"], "file": target["file"]}
        for group in mapping.values() if set(group["original_ids"]).intersection(binding["obligation_ids"])
        for target in group["targets"]), key=lambda row: (row["file"], row["declaration"]))
        for task, binding in groups.items()}
    requirements, spec = source_spec(index, source)
    value = {"version": 4, "migration_policy": 2, "inspection_policy": 5,
        "migration_scope_policy": 1, "migration_occurrence_policy": 1, "fingerprint_version": 2,
        "artifact_root": baseline["artifact_root"],
        "baseline_ref": store_artifact(paths.artifacts, baseline, kind="bump_migration_baseline_v2"),
        "project_baseline_sha256": baseline["sha256"], "original_index_ref": baseline["original_index_ref"],
        "scope_sha256": baseline["build_scope"]["sha256"],
        "original_index_sha256": index["index_sha256"], "environment": baseline["environment"],
        "solution_candidate": source.get("candidate_id"), "solution_sha256": source.get("sha256"),
        "source_main_sha": main_sha or bump_contract._git(paths.project_root, "rev-parse", "HEAD"),
        "requirements": requirements, "spec": spec, "spec_sha256": digest(spec),
        "task_bindings": groups, "bindings": outputs, "targets": _targets(index),
        "obligation_ids": sorted(index["occurrences"]), "mapping": mapping,
        "mapping_sha256": digest(mapping), "graph_sha256": digest(graph),
        "external_declarations": {}, "prerequisite_declarations": {}}
    validate_mapping(value)
    value["adopted_outputs"] = bump_contract.adopted_output_records(value)
    return seal(value)


def validate_contract(contract: dict) -> None:
    if (not isinstance(contract, dict) or contract.get("version") != 4
            or contract.get("migration_policy") != 2 or contract.get("inspection_policy") != 5
            or contract.get("migration_scope_policy") != 1 or contract.get("migration_occurrence_policy") != 1
            or "project_baseline" in contract or "original_reports" in contract
            or contract.get("sha256") != digest({k: v for k, v in contract.items() if k not in {"sha256", "artifact_id"}})
            or contract.get("mapping_sha256") != digest(contract.get("mapping"))
            or contract.get("spec_sha256") != digest(contract.get("spec"))
            or contract.get("obligation_ids") != sorted(contract.get("targets", {}))):
        raise ValueError("invalid or unsupported compact migration contract")
    validate_mapping(contract)


def with_mapping(root: Path, contract: dict, mapping: dict) -> dict:
    """Validate a declared change, not its truth; the publisher owns CAS/invalidation."""
    validate_contract(contract)
    result = copy.deepcopy(contract)
    result["mapping"] = copy.deepcopy(mapping)
    result["mapping_sha256"] = digest(mapping)
    validate_mapping(result)
    for group in mapping.values():
        for ref in group.get("evidence_refs", []):
            read_artifact(Path(contract["artifact_root"]), ref, decode_json=False)
    result["bindings"] = {task: sorted(({"declaration": target["declaration"], "file": target["file"]}
        for group in mapping.values() if set(group["original_ids"]).intersection(binding["obligation_ids"])
        for target in group["targets"]), key=lambda row: (row["file"], row["declaration"]))
        for task, binding in result["task_bindings"].items()}
    from .bump_contract import adopted_output_records
    result.pop("adopted_outputs", None)
    result["adopted_outputs"] = adopted_output_records(result)
    return seal({key: value for key, value in result.items() if key != "artifact_id"})


def output_target_key(contract: dict, task_id: str, declaration: str) -> str:
    group = contract.get("task_bindings", {}).get(task_id)
    if not group:
        raise ValueError("unknown migration execution group")
    matches = {key for key in group["obligation_ids"] if contract["targets"][key]["declaration"] == declaration}
    matches.update(key for mapping in contract["mapping"].values()
        if any(row["declaration"] == declaration for row in mapping["targets"])
        for key in mapping["original_ids"] if key in group["obligation_ids"])
    if len(matches) != 1:
        raise ValueError("ambiguous or missing original declaration occurrence")
    return next(iter(matches))


def output_fingerprints(contract: dict, task_id: str, outputs=None) -> dict:
    group = contract.get("task_bindings", {}).get(task_id)
    if not group or (outputs is not None and outputs != contract["bindings"][task_id]):
        raise ValueError("migration output inventory differs from its declared mapping")
    return {key: digest({"original": contract["targets"][key]["fingerprint"],
        "mapping_sha256": contract["mapping_sha256"]}) for key in group["obligation_ids"]}


def declaration_occurrences(contract: dict) -> dict:
    owners = {key: task for task, group in contract["task_bindings"].items() for key in group["obligation_ids"]}
    return {key: {"task_id": owners[key], **copy.deepcopy(row)} for key, row in contract["targets"].items()}


def require_receipt(task: dict, contract: dict, verification: dict) -> None:
    validate_contract(contract)
    task_id = task["task_id"]
    receipt = verification.get("module_receipt", {})
    expected = output_fingerprints(contract, task_id, task.get("outputs"))
    from .bump_contract import policy_hash
    if (verification.get("status", "passed" if verification.get("passed") else "failed") != "passed"
            or receipt.get("passed") is not True or receipt.get("task_id") != task_id
            or receipt.get("migration_policy") != 2 or receipt.get("inspection_policy") != 5
            or receipt.get("contract_sha256") != contract["sha256"]
            or receipt.get("mapping_sha256") != contract["mapping_sha256"]
            or receipt.get("original_index_sha256") != contract["original_index_sha256"]
            or receipt.get("scope_sha256") != contract.get("scope_sha256")
            or receipt.get("verified_targets") != expected or verification.get("verified_targets") != expected
            or receipt.get("policy_sha256") != policy_hash()
            or receipt.get("compiled_receipt") != verification.get("compiled_receipt")
            or not receipt.get("compiled_receipt")
            or receipt.get("source_sha256") != (verification.get("source_identity") or {}).get("source_sha256")):
        raise ValueError("migration completion lacks exact current scoped evidence")
    evidence = receipt.get("evidence_refs")
    if (not isinstance(evidence, list) or len(evidence) != len(contract["task_bindings"][task_id]["modules"])
            or {row.get("module") for row in evidence if isinstance(row, dict)}
            != set(contract["task_bindings"][task_id]["modules"])):
        raise ValueError("migration receipt omits local original/current comparison evidence")
    for row in evidence:
        if not _sha(row.get("comparison_sha256")):
            raise ValueError("migration comparison is not sealed")
        for field in ("original", "current", "comparison"):
            ref = row.get(field)
            if (not isinstance(ref, dict) or not _ID.fullmatch(str(ref.get("artifact_id", "")))
                    or not _sha(ref.get("sha256"))):
                raise ValueError("migration native evidence lacks immutable byte references")


def _original_report(baseline: dict, index: dict, module: str) -> tuple[dict, dict]:
    """Lazy, immutable local evidence cache; each reuse rechecks actual input bytes."""
    from . import bump_migration_contract as native, bump_cache, bump_json, bump_migration_project
    original = Path(baseline["migration"]["original_path"])
    root = Path(baseline["artifact_root"])
    dependency_errors = bump_migration_project.validate_dependencies(original)
    if dependency_errors:
        raise ValueError("original dependency checkout changed: " + "; ".join(dependency_errors))
    policy = digest({name: native._file_digest(Path(__file__).with_name(name)) for name in
        ("bump_checker_v2.py", "bump_inventory.py", "bump_inventory.lean", "bump_migration_contract.py")})
    key = digest({"index": index["index_sha256"], "module": module, "policy": policy})
    pointer = root / "bump-original-local-v2" / (key + ".json")
    report, ref = None, None
    if pointer.exists():
        if pointer.is_symlink() or pointer.parent.is_symlink():
            raise ValueError("original local evidence cache must not be symlinked")
        ref = json.loads(pointer.read_bytes(), object_pairs_hook=_unique)
        report = read_artifact(root, ref)
        native.local_report_records(report)
        if (report["source_hashes"] != native._source_hashes(original)
                or report["environment"] != index["environment"]
                or report["compiled_inputs"] != bump_cache.compiled_identity(report["compiled_modules"])):
            raise ValueError("cached original local evidence is stale")
    else:
        report = native.inspect_module_v2(original, module, sorted(index["modules"]))
    if (report["module"] != module or report["environment"] != index["environment"]
            or report["source_hashes"] != {name: sha for name, sha in baseline["migration"]["source_files"].items() if name.endswith(".lean")}):
        raise ValueError("original local evidence escaped its frozen source/environment")
    dependency_errors = bump_migration_project.validate_dependencies(original)
    if dependency_errors:
        raise ValueError("original dependency checkout changed during inspection: " + "; ".join(dependency_errors))
    if ref is None:
        # Do not publish a reusable pointer until both dependency checks passed.
        ref = store_artifact(root, report, kind="bump_original_local_v2")
        pointer.parent.mkdir(parents=True, exist_ok=True)
        bump_json.atomic_dump(pointer, ref)
    return report, ref


def check(root: Path, contract: dict, *, task_id: str | None, proposed_outputs=None,
          stage: str = "complete", final: bool = False) -> dict:
    from . import bump_contract as api, bump_project, bump_migration_contract as native, bump_cache, bump_jobs
    issues, comparisons, module_receipts, verified, checked, compiled, refs = [], {}, {}, {}, [], {}, {}
    failed_groups, global_blocker = set(), False
    before = None
    try:
        validate_contract(contract)
        baseline = resolved_baseline(root, contract)
        index = original_index(contract)
        source_refs = ["source:transition.json", "source:original-index.json"] + [
            "source:project/" + row["path"] for row in index["modules"].values()]
        requirements, spec = source_spec(index, {"source_refs": [{"ref_id": ref} for ref in source_refs]})
        if (_targets(index) != contract["targets"] or task_bindings(index) != contract["task_bindings"]
                or index["index_sha256"] != contract["original_index_sha256"]
                or index["scope_sha256"] != baseline["build_scope"]["sha256"]
                or contract["requirements"] != requirements or contract["spec"] != spec):
            raise ValueError("migration contract lost immutable original occurrence coverage")
        if stage != "complete":
            raise ValueError("policy 2 candidates must repair complete execution groups")
        selected = sorted(contract["task_bindings"]) if final else [task_id]
        if any(key not in contract["task_bindings"] for key in selected):
            raise ValueError("unknown migration execution group")
        if proposed_outputs is not None and proposed_outputs != contract["bindings"].get(task_id):
            raise ValueError("candidate outputs changed without an adopted mapping")
        bump_project.require_pinned_inputs(root, baseline)
        before = api.source_identity(root, layout=baseline["layout"])
        if before["environment"] != contract["environment"]:
            raise ValueError("target environment changed from the migration contract")
        for group in contract["mapping"].values():
            for ref in group.get("evidence_refs", []):
                read_artifact(Path(contract["artifact_root"]), ref, decode_json=False)
        for key in selected:
            binding = contract["task_bindings"][key]
            comparisons[key] = []
            refs[key] = []
            for module in binding["modules"]:
                old, old_ref = _original_report(baseline, index, module)
                current = native.inspect_module_v2(root, module, sorted(index["modules"]))
                for report, report_root in ((old, Path(baseline["migration"]["original_path"])), (current, root)):
                    errors = bump_project.migration_inspection_scope_errors(report, baseline, root=report_root)
                    if errors:
                        raise ValueError("; ".join(errors))
                comparison = native.compare_local_module_v2(old, current,
                    groups=contract["mapping"], occurrences=index["occurrences"])
                issues.extend(module + ": " + message for message in comparison["issues"])
                if not comparison["passed"]:
                    failed_groups.add(key)
                current_ref = store_artifact(Path(contract["artifact_root"]), current, kind="bump_target_local_v2")
                comparison_ref = store_artifact(Path(contract["artifact_root"]), comparison, kind="bump_local_comparison_v2")
                refs[key].append({"module": module, "original": old_ref, "current": current_ref,
                    "comparison": comparison_ref, "comparison_sha256": comparison["evidence_sha256"]})
                comparisons[key].append(comparison)
                for filename, identity in current["compiled_inputs"].items():
                    if filename in compiled and compiled[filename] != identity:
                        raise ValueError("compiled input changed between local module contexts")
                    compiled[filename] = identity
            if all(item["passed"] for item in comparisons[key]):
                checked.append(key)
                verified.update(output_fingerprints(contract, key))
        bump_project.require_pinned_inputs(root, baseline)
        if api.source_identity(root, layout=baseline["layout"]) != before:
            raise ValueError("target source/environment changed during scoped migration verification")
    except bump_jobs.JobCancelled:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        issues.append(str(exc))
        # An exception is not evidence of a repairable local meaning failure.
        # Preserve it as a global blocker instead of guessing from diagnostic text.
        global_blocker = True
    compiled_receipt = bump_cache.compiled_receipt(root, compiled) if compiled and not issues else None
    if not issues and not bump_cache.compiled_receipt_current(root, compiled_receipt):
        issues.append("missing or stale compiled-input evidence")
        global_blocker = True
    passed = not issues
    for key in comparisons:
        module_receipts[key] = {"schema_version": 2, "task_id": key,
            "module": contract["task_bindings"][key]["modules"][0], "passed": passed,
            "migration_policy": 2, "inspection_policy": 5, "occurrence_policy": 1,
            "contract_sha256": contract["sha256"], "mapping_sha256": contract["mapping_sha256"],
            "original_index_sha256": contract["original_index_sha256"],
            "scope_sha256": baseline["build_scope"]["sha256"],
            "verified_targets": output_fingerprints(contract, key) if passed else {},
            "policy_sha256": api.policy_hash(), "source_sha256": before["source_sha256"] if before else None,
            "compiled_receipt": compiled_receipt, "evidence_refs": refs[key],
            "declared_correspondences": sorted({g for c in comparisons[key] for g in c["declared_correspondences"]}),
            "semantic_equivalence_proved": False}
    return {"passed": passed, "issues": issues, "blockers": [], "targets": {},
        "failed_group_ids": sorted(failed_groups), "global_blocker": global_blocker,
        "source_identity": before, "verified_targets": verified if passed else {},
        "verified_tasks": checked if passed else [], "compiled_receipt": compiled_receipt,
        "final": final, "proposed_contract": contract, "module_receipts": module_receipts,
        "module_receipt": module_receipts.get(task_id, {"task_id": task_id, "passed": False}),
        "project_declarations": [], "migration_policy": 2, "inspection_policy": 5,
        "mapping_sha256": contract.get("mapping_sha256"),
        "original_index_sha256": contract.get("original_index_sha256"),
        "semantic_equivalence_proved": False}


def validate_saved_state(state: dict) -> None:
    from . import bump_contract as api, bump_state
    contract = state.get("formalization", {}).get("contract") or {}
    validate_contract(contract)
    if not api._baseline_matches(state, contract):
        raise ValueError("saved migration state lost its compact original baseline")
    tasks = state.get("formal_tasks", {})
    if not isinstance(tasks, dict) or set(tasks) - contract["task_bindings"].keys():
        raise ValueError("saved migration repair tasks escape the execution groups")
    for key, task in tasks.items():
        group = contract["task_bindings"][key]
        if (task.get("task_id") != key or task.get("migration_module") not in group["modules"]
                or task.get("lean_file") not in group["files"]
                or task.get("outputs") != contract["bindings"][key]
                or set(task.get("dependencies", [])) - contract["task_bindings"].keys()):
            raise ValueError("saved migration task changed its scope or mapping")
        if task.get("status") == "complete":
            candidate = state.get("formal_candidates", {}).get(task.get("accepted_candidate"), {})
            if (candidate.get("status") != "merged" or candidate.get("task_id") != key
                    or not bump_state.candidate_is_current(state, candidate)):
                raise ValueError("completed migration repair has no current merged candidate")
            require_receipt(task, contract, candidate.get("verification") or {})


def validate_snapshot(state: dict, report: dict) -> None:
    from . import bump_contract as api
    contract = state.get("formalization", {}).get("contract") or {}
    validate_contract(contract)
    baseline = state.get("project_baseline") or {}
    if (not api._baseline_matches(state, contract) or report.get("migration_policy") != 2
            or report.get("inspection_policy") != 5 or report.get("policy_sha256") != api.policy_hash()
            or report.get("project_baseline_sha256") != baseline.get("sha256")
            or report.get("contract_sha256") != contract["sha256"]
            or report.get("mapping_sha256") != contract["mapping_sha256"]
            or report.get("original_index_sha256") != contract["original_index_sha256"]
            or report.get("project_verification") != api.project_verification(Path(baseline["project_root"]), baseline)):
        raise ValueError("machine review does not bind the compact migration policy/context")
    if report.get("passed") is True:
        if (state.get("migration_refresh_required")
                or any(row.get("status") == "proposed" for row in state.get("migration_mapping_proposals", {}).values())
                or (state.get("migration_plan") or {}).get("source_sha256") != report.get("source_sha256")
                or state.get("migration_plan_main_sha") != report.get("main_sha")
                or state.get("formalization", {}).get("requirements") != contract["requirements"]
                or state.get("formalization", {}).get("spec") != contract["spec"]):
            raise ValueError("final review has stale diagnostics, pending correspondence or changed obligations")
        receipts = report.get("module_receipts")
        expected = {key: value for task in contract["task_bindings"] for key, value in output_fingerprints(contract, task).items()}
        if (not isinstance(receipts, dict) or set(receipts) != set(contract["task_bindings"])
                or report.get("verified_targets") != expected
                or report.get("declarations") != api.snapshot_declarations(contract)
                or report.get("declaration_occurrences") != declaration_occurrences(contract)):
            raise ValueError("final review omits original obligations or execution groups")
        for task, receipt in receipts.items():
            require_receipt({"task_id": task, "outputs": contract["bindings"][task]}, contract, {
                "status": "passed", "module_receipt": receipt,
                "verified_targets": output_fingerprints(contract, task),
                "compiled_receipt": report.get("compiled_receipt"),
                "source_identity": {"source_sha256": report.get("source_sha256")}})


def verify_final(paths, state: dict) -> dict:
    """Fresh all-obligation machine gate; native checks do not approve semantics."""
    import uuid
    from . import bump_contract as api, bump_project, bump_state, artifacts
    from .bump_input import source_matches
    formal = state["formalization"]
    contract = formal.get("contract") or {}
    validate_contract(contract)
    if not api._baseline_matches(state, contract):
        raise ValueError("final migration baseline changed")
    baseline = resolved_baseline(paths.project_root, contract)
    bump_project.require_original_branch(paths.project_root, baseline)
    bump_project.require_pinned_inputs(paths.project_root, baseline)
    before = api.source_identity(paths.project_root, layout=baseline["layout"])
    build = api.build_sources(paths.project_root, full=True, baseline=baseline)
    check_result = (check(paths.project_root, contract, task_id=None, final=True) if build["returncode"] == 0
        else {"passed": False, "issues": ["complete selected target build failed"], "targets": {},
            "failed_group_ids": [], "global_blocker": True})
    issues = list(check_result["issues"])
    tasks = state.get("formal_tasks", {})
    if any(task.get("status") != "complete" for task in tasks.values()):
        issues.append("repair tasks are incomplete")
    if any(row.get("status") == "proposed" for row in state.get("migration_mapping_proposals", {}).values()):
        issues.append("migration mapping proposals remain unresolved")
    if state.get("migration_refresh_required"):
        issues.append("migration repair diagnostics require refresh")
    if ((state.get("migration_plan") or {}).get("source_sha256") != before["source_sha256"]
            or state.get("migration_plan_main_sha") != before["main_sha"]):
        issues.append("migration repair plan is stale against the current source")
    if api.source_identity(paths.project_root, layout=baseline["layout"]) != before:
        issues.append("source changed during final migration verification")
    if before["main_sha"] != formal.get("main_sha"):
        issues.append("main differs from the accepted integration revision")
    if not api._problem_matches(paths, state) or not source_matches(paths, state):
        issues.append("frozen original source bundle changed")
    if formal.get("requirements") != contract["requirements"] or formal.get("spec") != contract["spec"]:
        issues.append("original obligation requirements or source specification changed")
    report = {**before, "snapshot_id": "review-" + uuid.uuid4().hex,
        "policy_sha256": api.policy_hash(), "migration_policy": 2, "inspection_policy": 5,
        "project_baseline_sha256": baseline["sha256"],
        "project_verification": api.project_verification(paths.project_root, baseline),
        "contract_sha256": contract["sha256"], "mapping_sha256": contract["mapping_sha256"],
        "original_index_sha256": contract["original_index_sha256"],
        "compiled_receipt": check_result.get("compiled_receipt"),
        "module_receipts": check_result.get("module_receipts", {}),
        "verified_targets": check_result.get("verified_targets", {}),
        "declaration_occurrences": declaration_occurrences(contract),
        "declarations": api.snapshot_declarations(contract), "passed": not issues, "issues": issues,
        "blockers": [], "failed_group_ids": check_result.get("failed_group_ids", []),
        "global_blocker": bool(check_result.get("global_blocker") or len(issues) > len(check_result["issues"])),
        "solution_candidate": formal.get("solution_candidate"),
        "solution_sha256": formal.get("solution_sha256"), "formalization_revision": formal["revision"],
        "spec_sha256": digest(formal.get("spec")), "repairs_sha256": bump_state.repair_digest(state),
        "external_declarations": {}, "prerequisite_declarations": {}, "representation_reviews": {},
        "accepted_candidates": {key: task.get("accepted_candidate") for key, task in tasks.items()},
        "task_statuses": {key: task.get("status") for key, task in tasks.items()},
        "semantic_equivalence_proved": False, "build": build}
    record = artifacts.store_text(paths.artifacts, json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        kind="bump_machine_review_v2", producer="Unity")
    return {key: value for key, value in report.items() if key != "build"} | {
        "artifact_id": record["artifact_id"], "artifact_sha256": record["sha256"]}


def validate_snapshot_artifact_links(contract: dict, snapshot: dict, baseline: dict) -> None:
    """Pure authenticated reads of recorded evidence, never native reinspection."""
    from . import bump_inventory, bump_migration_contract as native
    root = Path(contract["artifact_root"])
    index = read_artifact(root, contract["original_index_ref"])
    bump_inventory.validate_index(index)
    if (index["index_sha256"] != contract["original_index_sha256"]
            or _targets(index) != contract["targets"] or task_bindings(index) != contract["task_bindings"]):
        raise ValueError("recorded original index lost occurrence coverage")
    for group in contract["mapping"].values():
        for ref in group.get("evidence_refs", []):
            read_artifact(root, ref, decode_json=False)
    compiled = {}
    for receipt in snapshot["module_receipts"].values():
        for row in receipt["evidence_refs"]:
            old = read_artifact(root, row["original"])
            current = read_artifact(root, row["current"])
            comparison = read_artifact(root, row["comparison"])
            native.local_report_records(old)
            native.local_report_records(current)
            module = row["module"]
            expected_ids = index["modules"][module]["occurrence_ids"]
            expected_declared = sorted(key for key, group in contract["mapping"].items()
                if group["mode"] == "declared" and set(group["original_ids"]).intersection(expected_ids))
            if (old["module"] != module or current["module"] != module
                    or old["owned_modules"] != sorted(index["modules"])
                    or current["owned_modules"] != sorted(index["modules"])
                    or old["environment"] != index["environment"]
                    or old["source_hashes"] != {name: sha for name, sha in baseline["migration"]["source_files"].items() if name.endswith(".lean")}
                    or any(current["environment"].get(key) != contract["environment"].get(key)
                           for key in current["environment"])
                    or comparison.get("schema_version") != 2
                    or comparison.get("policy") != "bump-local-correspondence-v2"
                    or comparison.get("passed") is not True or comparison.get("issues") != []
                    or comparison.get("original_evidence") != old["evidence_sha256"]
                    or comparison.get("current_evidence") != current["evidence_sha256"]
                    or comparison.get("checked_occurrences") != expected_ids
                    or comparison.get("declared_correspondences") != expected_declared
                    or comparison.get("semantic_equivalence_proved") is not False
                    or comparison.get("evidence_sha256") != row["comparison_sha256"]
                    or comparison.get("evidence_sha256") != native.digest({key: value for key, value in comparison.items() if key != "evidence_sha256"})):
                raise ValueError("native evidence artifact links differ from the recorded migration check")
            for filename, identity in current["compiled_inputs"].items():
                if filename in compiled and compiled[filename] != identity:
                    raise ValueError("recorded native contexts disagree on compiled input identity")
                compiled[filename] = identity
    if read_artifact(root, snapshot["compiled_receipt"]) != compiled:
        raise ValueError("recorded compiled receipt differs from native evidence")
    machine = read_artifact(root, {"artifact_id": snapshot["artifact_id"], "sha256": snapshot["artifact_sha256"]})
    if ({key: value for key, value in machine.items() if key != "build"}
            != {key: value for key, value in snapshot.items() if key not in {"artifact_id", "artifact_sha256"}}):
        raise ValueError("machine review artifact bytes differ from the recorded snapshot")


def snapshot_is_current(paths, state: dict, snapshot: dict, *, require_complete=True) -> bool:
    from . import bump_contract as api, bump_project, bump_cache, bump_state
    from .bump_input import source_matches
    contract = state.get("formalization", {}).get("contract") or {}
    try:
        validate_snapshot(state, snapshot)
        baseline = resolved_baseline(paths.project_root, contract)
        if snapshot.get("passed"):
            validate_snapshot_artifact_links(contract, snapshot, baseline)
        bump_project.require_original_branch(paths.project_root, baseline)
        bump_project.require_pinned_inputs(paths.project_root, baseline)
        current = api.source_identity(paths.project_root, layout=baseline["layout"])
        formal, tasks = state["formalization"], state.get("formal_tasks", {})
        return (all(current[key] == snapshot.get(key) for key in ("main_sha", "source_sha256", "environment"))
            and current["main_sha"] == formal.get("main_sha")
            and (not snapshot.get("passed") or bump_cache.compiled_receipt_current(paths.project_root, snapshot.get("compiled_receipt")))
            and (not require_complete or all(task.get("status") == "complete" for task in tasks.values()))
            and snapshot.get("task_statuses") == {key: task.get("status") for key, task in tasks.items()}
            and snapshot.get("accepted_candidates") == {key: task.get("accepted_candidate") for key, task in tasks.items()}
            and snapshot.get("formalization_revision") == formal.get("revision")
            and snapshot.get("spec_sha256") == digest(formal.get("spec")) == contract["spec_sha256"]
            and formal.get("requirements") == contract["requirements"]
            and not state.get("migration_refresh_required")
            and not any(row.get("status") == "proposed" for row in state.get("migration_mapping_proposals", {}).values())
            and (state.get("migration_plan") or {}).get("source_sha256") == current["source_sha256"]
            and state.get("migration_plan_main_sha") == current["main_sha"]
            and snapshot.get("repairs_sha256") == bump_state.repair_digest(state)
            and snapshot.get("solution_candidate") == formal.get("solution_candidate") == contract.get("solution_candidate")
            and snapshot.get("solution_sha256") == formal.get("solution_sha256") == contract.get("solution_sha256")
            and api._problem_matches(paths, state) and source_matches(paths, state))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
