"""Policy-2 boundaries; no model, service or real compiler calls in this suite."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import artifacts, bump_checker_v2 as checker, bump_contract as api
from unity import bump_inventory as inventory, bump_migration_contract as native


def name(value):
    return ["str", ["anonymous"], value]


def declaration(label="item", *, kind="theorem", axioms=None, direct_sorry=False, value=0):
    meaning = {"name": name(label), "kind": kind, "type": ["sort", ["zero"]], "level_params": []}
    if kind in {"def", "opaque"}:
        meaning.update(value=["natVal", value], all=[name(label)])
        if kind == "def":
            meaning.update(hints=["regular", 1], safety="safe")
        else:
            meaning["unsafe"] = False
    if kind == "axiom":
        meaning["unsafe"] = False
    return {"name_ast": name(label), "display_name": label, "kind": kind, "range": None,
        "is_internal": False, "direct_sorry": direct_sorry, "dependencies": [],
        "meaning": meaning, "axioms": axioms or []}


def axiom(label="custom", *, type_value=0):
    return {"reference": {"module": "Imports", "name_ast": name(label)},
        "display_name": label, "kind": "axiom", "unsafe": False,
        "type": ["sort", ["succ", ["zero"]]] if type_value else ["sort", ["zero"]], "level_params": []}


def report(rows=None, module="Basic", owned=None):
    rows = [declaration()] if rows is None else copy.deepcopy(rows)
    value = {"schema_version": 1, "mode": "local-meanings", "module": module,
        "owned_modules": owned or ["Basic"], "kind": "bump_local_module_inspection", "verified": True,
        "complete_inventory": True, "inspection_policy": 5, "declaration_inventory": "raw-module-constants-v1",
        "raw_declaration_count": len(rows), "declarations": rows,
        "environment": {"lean_sysroot": "/fixture/toolchain", "config": {"lean-toolchain": "a" * 64}},
        "compiled_modules": ["/fixture/Basic.olean"], "imported_modules": [module],
        "compiled_inputs": {"/fixture/Basic.olean": {"path": "/fixture/Basic.olean", "sha256": "b" * 64}},
        "source_hashes": {"Basic.lean": "c" * 64}, "source_sha256": native.digest({"Basic.lean": "c" * 64}),
        "inspector_sha256": "d" * 64, "executable_sha256": "e" * 64}
    value["evidence_sha256"] = native.digest(value)
    return value


def reseal(report_value):
    report_value["evidence_sha256"] = native.digest({k: v for k, v in report_value.items() if k != "evidence_sha256"})
    return report_value


def index_for(rows=None, module="Basic"):
    rows = [declaration()] if rows is None else rows
    raw = {"schema_version": 1, "mode": "index", "module": module,
        "declaration_inventory": "raw-module-constants-v1", "raw_declaration_count": len(rows),
        "declarations": [{k: v for k, v in row.items() if k not in {"meaning", "axioms"}} for row in rows]}
    return inventory.assemble_index({module: raw}, {module: {"path": module + ".lean", "imports": [], "compiler_derived": True}},
        {module + ".lean": "c" * 64}, scope_sha256="f" * 64,
        environment={"lean_sysroot": "/fixture/toolchain", "config": {"lean-toolchain": "a" * 64}})


def contract_for(index):
    groups = checker.task_bindings(index)
    mapping = checker.default_mapping(index)
    source = {"source_refs": [{"ref_id": ref} for ref in ["source:transition.json", "source:original-index.json", *[
        "source:project/" + row["path"] for row in index["modules"].values()]]]}
    requirements, spec = checker.source_spec(index, source)
    value = {"version": 4, "migration_policy": 2, "inspection_policy": 5,
        "migration_scope_policy": 1, "migration_occurrence_policy": 1,
        "targets": checker._targets(index), "obligation_ids": sorted(index["occurrences"]),
        "task_bindings": groups, "mapping": mapping, "mapping_sha256": checker.digest(mapping),
        "original_index_sha256": index["index_sha256"], "spec": spec, "spec_sha256": checker.digest(spec),
        "requirements": requirements, "artifact_root": "/fixture/.unity/artifacts",
        "scope_sha256": "f" * 64,
        "bindings": {module: [{"declaration": index["occurrences"][key]["display_name"], "file": row["files"][0]}
            for key in row["obligation_ids"]] for module, row in groups.items()}}
    return checker.seal(value)


class LocalComparisonTests(unittest.TestCase):
    def compare(self, original, current, mapping=None, index=None):
        index = index or index_for(original["declarations"])
        return native.compare_local_module_v2(original, current, groups=mapping or checker.default_mapping(index),
                                               occurrences=index["occurrences"])

    def test_identical_local_meaning_is_not_equivalence_claim(self):
        result = self.compare(report(), report())
        self.assertTrue(result["passed"])
        self.assertFalse(result["semantic_equivalence_proved"])

    def test_local_definition_drift_requires_declaration(self):
        original, current = report([declaration(kind="def")]), report([declaration(kind="def", value=1)])
        self.assertFalse(self.compare(original, current)["passed"])
        index = index_for(original["declarations"])
        mapping = checker.default_mapping(index)
        group = next(iter(mapping.values()))
        group.update(mode="declared", reason="Compatibility refactor", relation="Preserves the old operation",
            evidence_refs=[{"artifact_id": "artifact-" + "a" * 12, "sha256": "b" * 64}])
        group["targets"][0]["expected_meaning_sha256"] = native.digest(current["declarations"][0]["meaning"])
        result = self.compare(original, current, mapping, index)
        self.assertTrue(result["passed"])
        self.assertEqual(result["declared_correspondences"], list(mapping))
        self.assertFalse(result["semantic_equivalence_proved"])

    def test_declared_drift_still_checks_exact_current_meaning(self):
        original, current = report(), report()
        index = index_for()
        mapping = checker.default_mapping(index)
        group = next(iter(mapping.values()))
        group["mode"] = "declared"
        group["targets"][0]["expected_meaning_sha256"] = "0" * 64
        self.assertFalse(self.compare(original, current, mapping, index)["passed"])

    def test_dropped_assumption_is_permitted(self):
        self.assertTrue(self.compare(report([declaration(axioms=[axiom()])]), report())["passed"])

    def test_new_assumption_and_broadened_same_name_axiom_are_rejected(self):
        self.assertFalse(self.compare(report(), report([declaration(axioms=[axiom()])]))["passed"])
        self.assertFalse(self.compare(report([declaration(axioms=[axiom()])]),
            report([declaration(axioms=[axiom(type_value=1)])]))["passed"])

    def test_new_direct_sorry_is_rejected(self):
        self.assertFalse(self.compare(report(), report([declaration(direct_sorry=True)]))["passed"])

    def test_helper_cannot_borrow_custom_baseline_trust(self):
        original = report([declaration(axioms=[axiom()])])
        current = report([declaration(axioms=[axiom()]), declaration("helper", axioms=[axiom()])])
        self.assertFalse(self.compare(original, current)["passed"])

    def test_untrusted_helper_is_not_hidden_by_mapping(self):
        self.assertFalse(self.compare(report(), report([declaration(), declaration("helper", kind="axiom")]))["passed"])

    def test_missing_occurrence_is_rejected(self):
        self.assertFalse(self.compare(report(), report([]))["passed"])

    def test_no_recursive_upstream_meanings_are_required(self):
        original = report()
        original["declarations"][0]["meaning"]["type"] = ["const", name("External"), []]
        reseal(original)
        result = self.compare(original, copy.deepcopy(original))
        self.assertTrue(result["passed"])
        self.assertNotIn("meanings", original)

    def test_raw_count_and_duplicate_typed_name_fail_closed(self):
        malformed = report()
        malformed["raw_declaration_count"] += 1
        with self.assertRaises(ValueError):
            native.local_report_records(reseal(malformed))
        with self.assertRaises(ValueError):
            native.local_report_records(report([declaration(), declaration()]))

    def test_same_typed_name_in_siblings_cannot_receive_ambiguous_automatic_renames(self):
        index = index_for()
        original_id = next(iter(index["occurrences"]))
        duplicate = copy.deepcopy(index["occurrences"][original_id])
        duplicate.update(module="Sibling", path="Sibling.lean")
        sibling_id = inventory.occurrence_id("Sibling", duplicate["name_ast"])
        index["occurrences"][sibling_id] = duplicate
        mapping = checker.default_mapping(index)
        mapping[sibling_id]["targets"][0].update(native_name=name("other"), declaration="other")
        with self.assertRaisesRegex(ValueError, "ambiguous cross-module"):
            self.compare(report(owned=["Basic", "Sibling"]), report(owned=["Basic", "Sibling"]), mapping, index)

    def test_split_or_merge_never_unions_trust_allowances(self):
        old_rows = [declaration("first", axioms=[axiom()]), declaration("second")]
        original = report(old_rows)
        index = index_for(old_rows)
        current = report([declaration("joined", axioms=[axiom()])])
        group = {"original_ids": list(index["occurrences"]), "mode": "declared", "targets": [{
            "module": "Basic", "declaration": "joined", "file": "Basic.lean", "native_name": name("joined"),
            "expected_meaning_sha256": native.digest(current["declarations"][0]["meaning"])}]}
        self.assertFalse(self.compare(original, current, {"joined": group}, index)["passed"])


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.index = index_for()
        self.contract = contract_for(self.index)

    def test_identity_mapping_and_empty_repair_plan_keep_requirements(self):
        checker.validate_contract(self.contract)
        self.assertEqual(len(self.contract["requirements"]), 1)
        self.assertEqual(self.contract["requirements"][0]["tasks"], ["Basic"])
        self.assertNotIn("project_baseline", self.contract)
        self.assertNotIn("original_reports", json.dumps(self.contract))

    def test_empty_module_still_has_requirement_and_execution_group(self):
        value = contract_for(index_for([]))
        checker.validate_contract(value)
        self.assertEqual(value["bindings"], {"Basic": []})
        self.assertEqual(len(value["requirements"]), 1)

    def test_missing_and_duplicate_mapping_obligations_are_rejected(self):
        with self.assertRaises(ValueError):
            checker.validate_mapping(self.contract, {})
        mapping = copy.deepcopy(self.contract["mapping"])
        mapping["duplicate"] = copy.deepcopy(next(iter(mapping.values())))
        with self.assertRaises(ValueError):
            checker.validate_mapping(self.contract, mapping)

    def test_mapping_cannot_expand_write_scope(self):
        mapping = copy.deepcopy(self.contract["mapping"])
        next(iter(mapping.values()))["targets"][0]["file"] = "Other.lean"
        with self.assertRaises(ValueError):
            checker.validate_mapping(self.contract, mapping)

    def test_declared_mapping_requires_evidence_and_expected_meaning(self):
        mapping = copy.deepcopy(self.contract["mapping"])
        group = next(iter(mapping.values()))
        group.update(mode="declared", reason="refactor", relation="same operation", evidence_refs=[])
        with self.assertRaises(ValueError):
            checker.validate_mapping(self.contract, mapping)
        group["evidence_refs"] = [{"artifact_id": "artifact-" + "a" * 12, "sha256": "b" * 64}]
        with self.assertRaises(ValueError):
            checker.validate_mapping(self.contract, mapping)
        group["targets"][0]["expected_meaning_sha256"] = "c" * 64
        checker.validate_mapping(self.contract, mapping)

    def test_unsupported_v4_policy_cannot_fall_back_to_legacy(self):
        value = copy.deepcopy(self.contract)
        value["migration_policy"] = 1
        value = checker.seal(value)
        result = api.check_formal_contract(Path("/unused"), value, [], completed=set())
        self.assertFalse(result["passed"])
        self.assertIn("compact migration contract", result["issues"][0])

    def test_active_task_subset_manifest_and_whole_ledger_snapshot(self):
        outputs = self.contract["bindings"]["Basic"]
        self.assertEqual(api.output_manifest_blockers(self.contract, task_ids={"Basic"}, task_id="Basic", proposed_outputs=outputs), [])
        self.assertEqual(set(api.snapshot_declarations(self.contract)), set(self.index["occurrences"]))
        self.assertTrue(api.output_manifest_blockers(self.contract, task_ids=set(), task_id="Basic", proposed_outputs=outputs))

    def test_policy2_inline_baseline_is_rejected(self):
        value = checker.seal({**self.contract, "project_baseline": {}})
        with self.assertRaises(ValueError):
            checker.validate_contract(value)

    def test_new_mapping_invalidates_occurrence_fingerprints(self):
        before = checker.output_fingerprints(self.contract, "Basic")
        value = copy.deepcopy(self.contract)
        value["mapping_sha256"] = "0" * 64
        self.assertNotEqual(before, checker.output_fingerprints(value, "Basic"))

    def test_every_source_reference_must_be_accounted(self):
        with self.assertRaises(ValueError):
            checker.source_spec(self.index, {"source_refs": []})

    def test_source_spec_is_canonical_for_complete_ledger_roundtrip(self):
        from unity.bump_spec import normalize_requirements, normalize_spec
        source = {"source_refs": [{"ref_id": ref} for ref in [
            "source:transition.json", "source:original-index.json", "source:project/Basic.lean"]]}
        requirements, spec = checker.source_spec(self.index, source)
        tasks = {row["tasks"][0]: {"source_components": row["source_components"]}
                 for row in requirements}
        self.assertEqual(requirements, normalize_requirements(requirements, tasks,
            {row["ref_id"] for row in source["source_refs"]}))
        self.assertEqual(spec, normalize_spec(spec, source=source, requirements=requirements, tasks=tasks))


class ArtifactEvidenceTests(unittest.TestCase):
    def test_read_hashes_bytes_and_creates_no_missing_artifact_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "artifacts"
            with self.assertRaises(OSError):
                checker.read_artifact(root, {"artifact_id": "artifact-" + "a" * 12, "sha256": "b" * 64})
            self.assertFalse(root.exists())
            ref = checker.store_artifact(root, {"value": 1}, kind="fixture")
            self.assertEqual(checker.read_artifact(root, ref), {"value": 1})
            (root / "blobs" / ref["sha256"]).write_text('{"value":2}\n')
            with self.assertRaises(ValueError):
                checker.read_artifact(root, ref)

    def test_declared_evidence_may_be_authenticated_text_but_not_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = artifacts.store_text(root, "Explicit compatibility argument", kind="fixture")
            ref = {key: record[key] for key in ("artifact_id", "sha256")}
            self.assertEqual(checker.read_artifact(root, ref, decode_json=False), b"Explicit compatibility argument")
            empty = artifacts.store_text(root, "", kind="fixture")
            with self.assertRaises(ValueError):
                checker.read_artifact(root, {key: empty[key] for key in ("artifact_id", "sha256")}, decode_json=False)

    def test_changed_mapping_evidence_is_resolved_before_adoption(self):
        index = index_for()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            contract = checker.seal({**contract_for(index), "artifact_root": str(root)})
            mapping = copy.deepcopy(contract["mapping"])
            group = next(iter(mapping.values()))
            group.update(mode="declared", reason="compatibility", relation="same operation",
                evidence_refs=[{"artifact_id": "artifact-" + "a" * 12, "sha256": "b" * 64}])
            group["targets"][0]["expected_meaning_sha256"] = "c" * 64
            with self.assertRaises(OSError):
                checker.with_mapping(root, contract, mapping)


class FinalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.contract = contract_for(index_for())
        self.contract["project_baseline_sha256"] = "b" * 64
        self.contract = checker.seal(self.contract)
        self.baseline = {"policy": "migration-v2", "sha256": "b" * 64, "project_root": "/fixture",
            "build_scope": {"sha256": "f" * 64, "excluded_files": {}},
            "original_index_ref": {"artifact_id": "artifact-" + "1" * 12, "sha256": "c" * 64},
            "original_index_sha256": self.contract["original_index_sha256"], "occurrence_count": 1,
            "compiler_modules": {"Basic": {"path": "Basic.lean"}}}
        self.state = {"project_baseline": self.baseline, "formal_tasks": {},
            "formalization": {"contract": self.contract, "requirements": self.contract["requirements"],
                "spec": self.contract["spec"]}, "migration_plan": {"source_sha256": "s" * 64},
            "migration_plan_main_sha": "m" * 40}
        reference = {"artifact_id": "artifact-" + "2" * 12, "sha256": "a" * 64}
        targets = checker.output_fingerprints(self.contract, "Basic")
        compiled = {"digest": "c" * 64}
        receipt = {"schema_version": 2, "task_id": "Basic", "passed": True, "migration_policy": 2,
            "inspection_policy": 5, "contract_sha256": self.contract["sha256"], "scope_sha256": "f" * 64,
            "mapping_sha256": self.contract["mapping_sha256"], "original_index_sha256": self.contract["original_index_sha256"],
            "verified_targets": targets, "policy_sha256": "p" * 64, "compiled_receipt": compiled,
            "source_sha256": "s" * 64, "evidence_refs": [{"module": "Basic", "original": reference,
                "current": reference, "comparison": reference, "comparison_sha256": "x" * 64}]}
        # Comparison seals require SHA hex, unlike opaque mocked source identifiers.
        receipt["evidence_refs"][0]["comparison_sha256"] = "a" * 64
        self.report = {"migration_policy": 2, "inspection_policy": 5, "policy_sha256": "p" * 64,
            "local_mapping_identity_version": 1,
            "group_mapping_content_sha256": {key: api.digest(api.migration_group_mapping_content(self.contract, key))
                                             for key in self.contract["task_bindings"]},
            "project_baseline_sha256": "b" * 64, "contract_sha256": self.contract["sha256"],
            "mapping_sha256": self.contract["mapping_sha256"], "original_index_sha256": self.contract["original_index_sha256"],
            "project_verification": api.project_verification(Path("/fixture"), self.baseline),
            "passed": True, "main_sha": "m" * 40, "source_sha256": "s" * 64,
            "compiled_receipt": compiled, "module_receipts": {"Basic": receipt}, "verified_targets": targets,
            "declarations": api.snapshot_declarations(self.contract),
            "declaration_occurrences": checker.declaration_occurrences(self.contract)}
        self.baseline_patch = patch.object(api, "_baseline_matches", return_value=True)
        self.policy_patch = patch.object(api, "policy_hash", return_value="p" * 64)
        self.baseline_patch.start()
        self.policy_patch.start()
        self.addCleanup(self.baseline_patch.stop)
        self.addCleanup(self.policy_patch.stop)

    def test_clean_empty_repair_queue_still_requires_all_original_group_receipts(self):
        checker.validate_snapshot(self.state, self.report)
        self.report["module_receipts"] = {}
        with self.assertRaisesRegex(ValueError, "omits original"):
            checker.validate_snapshot(self.state, self.report)

    def test_fresh_snapshot_cannot_strip_or_downgrade_local_mapping_identity(self):
        for fields in (("group_mapping_content_sha256",), ("local_mapping_identity_version",),
                       ("group_mapping_content_sha256", "local_mapping_identity_version")):
            report = copy.deepcopy(self.report)
            for field in fields:
                report.pop(field)
            with self.assertRaisesRegex(ValueError, "local correspondence"):
                checker.validate_snapshot(self.state, report)
        report = copy.deepcopy(self.report)
        report["local_mapping_identity_version"] = True
        with self.assertRaisesRegex(ValueError, "local correspondence"):
            checker.validate_snapshot(self.state, report)

    def test_exact_persisted_legacy_snapshot_retains_conservative_validation(self):
        report = copy.deepcopy(self.report)
        report.pop("local_mapping_identity_version")
        report.pop("group_mapping_content_sha256")
        report["snapshot_id"] = "review-legacy"
        self.state["review_snapshots"] = {report["snapshot_id"]: copy.deepcopy(report)}
        checker.validate_snapshot(self.state, report)
        report["mapping_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            checker.validate_snapshot(self.state, report)

    def test_pending_mapping_invalidates_machine_snapshot_without_source_change(self):
        self.state["migration_mapping_proposals"] = {"proposal": {"status": "proposed"}}
        with self.assertRaisesRegex(ValueError, "pending correspondence"):
            checker.validate_snapshot(self.state, self.report)

    def test_diagnostic_refresh_and_stale_plan_invalidate_snapshot(self):
        self.state["migration_refresh_required"] = True
        with self.assertRaises(ValueError):
            checker.validate_snapshot(self.state, self.report)
        self.state["migration_refresh_required"] = False
        self.state["migration_plan"]["source_sha256"] = "t" * 64
        with self.assertRaises(ValueError):
            checker.validate_snapshot(self.state, self.report)

    def test_changed_requirements_cannot_relabel_same_native_result(self):
        self.state["formalization"]["requirements"] = []
        with self.assertRaises(ValueError):
            checker.validate_snapshot(self.state, self.report)

    def test_exact_compiled_receipt_and_native_artifact_refs_required(self):
        self.report["module_receipts"]["Basic"]["compiled_receipt"] = {"digest": "wrong"}
        with self.assertRaises(ValueError):
            checker.validate_snapshot(self.state, self.report)
        self.report["module_receipts"]["Basic"]["compiled_receipt"] = self.report["compiled_receipt"]
        self.report["module_receipts"]["Basic"]["evidence_refs"] = []
        with self.assertRaises(ValueError):
            checker.validate_snapshot(self.state, self.report)

    def test_mapping_revision_invalidates_native_receipt(self):
        self.report["module_receipts"]["Basic"]["mapping_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            checker.validate_snapshot(self.state, self.report)

    def test_report_cannot_claim_acceptance_from_machine_pass_alone(self):
        from unity import bump_report
        self.state["phase"] = "formalizing"
        self.state["formalization"].update(status="active", review_snapshot=self.report)
        with self.assertRaisesRegex(ValueError, "has not been accepted"):
            bump_report.completion_report(self.state, accepted=True)
        partial = bump_report.completion_report(self.state, accepted=False)
        self.assertEqual(partial["schema_version"], 2)
        self.assertFalse(partial["semantic_equivalence_proved"])
        self.assertEqual(len(partial["coverage"]), 1)


class FailureRoutingTests(unittest.TestCase):
    def test_invalid_contract_is_global_blocker_without_guessed_group(self):
        result = checker.check(Path("/unused"), {}, task_id="Basic")
        self.assertFalse(result["passed"])
        self.assertTrue(result["global_blocker"])
        self.assertEqual(result["failed_group_ids"], [])

    def test_only_completed_local_comparison_names_failed_group(self):
        from contextlib import ExitStack
        from unity import bump_cache, bump_project
        index = index_for()
        value = contract_for(index)
        value["environment"] = report()["environment"]
        value = checker.seal(value)
        baseline = {"layout": {}, "build_scope": {"sha256": "f" * 64},
                    "migration": {"original_path": "/original"}}
        identity = {"source_sha256": "a" * 64, "environment": value["environment"]}
        ref = {"artifact_id": "artifact-" + "a" * 12, "sha256": "b" * 64}
        comparison = {"passed": False, "issues": ["trusted assumptions expanded"],
                      "evidence_sha256": "c" * 64, "declared_correspondences": []}
        with ExitStack() as stack:
            stack.enter_context(patch.object(checker, "resolved_baseline", return_value=baseline))
            stack.enter_context(patch.object(checker, "original_index", return_value=index))
            stack.enter_context(patch.object(checker, "_original_report", return_value=(report(), ref)))
            stack.enter_context(patch.object(checker, "store_artifact", return_value=ref))
            stack.enter_context(patch.object(api, "source_identity", return_value=identity))
            stack.enter_context(patch.object(bump_project, "require_pinned_inputs"))
            stack.enter_context(patch.object(bump_project, "migration_inspection_scope_errors", return_value=[]))
            stack.enter_context(patch.object(native, "inspect_module_v2", return_value=report()))
            stack.enter_context(patch.object(native, "compare_local_module_v2", return_value=comparison))
            result = checker.check(Path("/target"), value, task_id="Basic")
        self.assertFalse(result["passed"])
        self.assertFalse(result["global_blocker"])
        self.assertEqual(result["failed_group_ids"], ["Basic"])


class SnapshotArtifactLinkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.index = index_for()
        self.old, self.current = report(), report()
        self.contract = contract_for(self.index)
        self.contract.update(artifact_root=str(self.root), environment=self.current["environment"],
            original_index_ref=checker.store_artifact(self.root, self.index, kind="fixture_index"))
        self.contract = checker.seal(self.contract)
        self.baseline = {"migration": {"source_files": self.old["source_hashes"]}}
        self.comparison = native.compare_local_module_v2(self.old, self.current,
            groups=self.contract["mapping"], occurrences=self.index["occurrences"])
        self.refs = {"module": "Basic", "comparison_sha256": self.comparison["evidence_sha256"]}
        for kind, value in (("original", self.old), ("current", self.current), ("comparison", self.comparison)):
            self.refs[kind] = checker.store_artifact(self.root, value, kind="fixture_" + kind)
        self.snapshot = {"module_receipts": {"Basic": {"evidence_refs": [self.refs]}},
            "compiled_receipt": checker.store_artifact(self.root, self.current["compiled_inputs"], kind="fixture_compiled")}
        machine = checker.store_artifact(self.root, {**self.snapshot, "build": {"returncode": 0}}, kind="fixture_machine")
        self.snapshot.update(artifact_id=machine["artifact_id"], artifact_sha256=machine["sha256"])

    def test_complete_links_are_pure_reads(self):
        before = {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.rglob("*") if path.is_file()}
        with patch.object(native, "inspect_module_v2", side_effect=AssertionError("native inspection forbidden")):
            checker.validate_snapshot_artifact_links(self.contract, self.snapshot, self.baseline)
        after = {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_corrupted_original_current_and_comparison_bytes_are_rejected(self):
        for kind in ("original", "current", "comparison"):
            with self.subTest(kind=kind):
                path = self.root / "blobs" / self.refs[kind]["sha256"]
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                with self.assertRaises(ValueError):
                    checker.validate_snapshot_artifact_links(self.contract, self.snapshot, self.baseline)
                path.write_bytes(before)

    def test_valid_but_unlinked_comparison_artifact_is_rejected(self):
        changed = copy.deepcopy(self.comparison)
        changed["original_evidence"] = "0" * 64
        changed["evidence_sha256"] = native.digest({key: value for key, value in changed.items() if key != "evidence_sha256"})
        self.refs["comparison"] = checker.store_artifact(self.root, changed, kind="fixture_wrong_comparison")
        self.refs["comparison_sha256"] = changed["evidence_sha256"]
        with self.assertRaisesRegex(ValueError, "artifact links"):
            checker.validate_snapshot_artifact_links(self.contract, self.snapshot, self.baseline)

    def test_missing_declared_correspondence_evidence_is_rejected(self):
        group = next(iter(self.contract["mapping"].values()))
        group.update(mode="declared", evidence_refs=[{"artifact_id": "artifact-" + "9" * 12, "sha256": "f" * 64}])
        with self.assertRaises(OSError):
            checker.validate_snapshot_artifact_links(self.contract, self.snapshot, self.baseline)

    def test_substituted_compiled_receipt_is_rejected(self):
        self.snapshot["compiled_receipt"] = checker.store_artifact(self.root, {}, kind="fixture_wrong_compiled")
        with self.assertRaisesRegex(ValueError, "compiled receipt"):
            checker.validate_snapshot_artifact_links(self.contract, self.snapshot, self.baseline)

    def test_missing_machine_review_blob_is_rejected(self):
        (self.root / "blobs" / self.snapshot["artifact_sha256"]).unlink()
        with self.assertRaises(OSError):
            checker.validate_snapshot_artifact_links(self.contract, self.snapshot, self.baseline)

    def test_currentness_fails_before_other_work_when_artifact_links_break(self):
        from types import SimpleNamespace
        paths = SimpleNamespace(project_root=self.root)
        saved = {"formalization": {"contract": self.contract}}
        with patch.object(checker, "validate_snapshot"), \
                patch.object(checker, "resolved_baseline", return_value=self.baseline), \
                patch.object(checker, "validate_snapshot_artifact_links", side_effect=ValueError("broken links")) as guard, \
                patch.object(api, "source_identity", side_effect=AssertionError("must not run")):
            self.assertFalse(checker.snapshot_is_current(paths, saved, {"passed": True}))
        guard.assert_called_once()


class LazyOriginalDependencyTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        from unity import bump_migration_project, bump_cache
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.index, self.report = index_for(), report()
        self.baseline = {"artifact_root": str(self.root / "artifacts"),
            "migration": {"original_path": str(self.root / "original"), "source_files": self.report["source_hashes"]}}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.dependencies = self.stack.enter_context(patch.object(bump_migration_project, "validate_dependencies", return_value=[]))
        self.inspect = self.stack.enter_context(patch.object(native, "inspect_module_v2", return_value=self.report))
        self.stack.enter_context(patch.object(native, "_source_hashes", return_value=self.report["source_hashes"]))
        self.stack.enter_context(patch.object(bump_cache, "compiled_identity", return_value=self.report["compiled_inputs"]))

    def test_fresh_and_reused_original_evidence_both_check_dependencies_twice(self):
        first = checker._original_report(self.baseline, self.index, "Basic")
        second = checker._original_report(self.baseline, self.index, "Basic")
        self.assertEqual(first, second)
        self.assertEqual(self.inspect.call_count, 1)
        self.assertEqual(self.dependencies.call_count, 4)

    def test_dirty_original_dependency_blocks_before_native_inspection(self):
        self.dependencies.return_value = ["Dependency source checkout is dirty"]
        with self.assertRaisesRegex(ValueError, "original dependency"):
            checker._original_report(self.baseline, self.index, "Basic")
        self.inspect.assert_not_called()
        self.assertFalse(Path(self.baseline["artifact_root"]).exists())

    def test_dependency_change_during_inspection_does_not_publish_cache_pointer(self):
        self.dependencies.side_effect = [[], ["Dependency checkout commit differs from manifest"]]
        with self.assertRaisesRegex(ValueError, "during inspection"):
            checker._original_report(self.baseline, self.index, "Basic")
        self.inspect.assert_called_once()
        self.assertFalse(Path(self.baseline["artifact_root"]).exists())

    def test_dirty_dependency_also_blocks_cached_original_reuse(self):
        checker._original_report(self.baseline, self.index, "Basic")
        self.dependencies.return_value = ["Dependency source checkout is dirty"]
        with self.assertRaisesRegex(ValueError, "original dependency"):
            checker._original_report(self.baseline, self.index, "Basic")
        self.assertEqual(self.inspect.call_count, 1)


if __name__ == "__main__":
    unittest.main()
