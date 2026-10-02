"""Offline negative controls for Bump's independent semantic contract."""

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import bump_migration_contract as contract
from unity import bump_contract as adapter, bump_project, bump_migration_project


def name(value):
    result = ["anonymous"]
    for part in value.split("."):
        result = ["str", result, part]
    return result


def const(value):
    return ["const", name(value), []]


def meaning(value, kind="theorem", *, type_=None, body=None, deps=(), module="Fixture"):
    row = {"name": name(value), "kind": kind, "level_params": [],
           "type": type_ or ["sort", ["zero"]]}
    if kind == "def":
        row.update(value=body if body is not None else ["natVal", 1], hints=["regular", 0], safety="safe", all=[])
    elif kind == "axiom":
        row.update(unsafe=False)
    return {"meaning": row, "module": module, "dependencies": list(deps)}


def seal(value):
    if value.get("declaration_inventory") == contract.DECLARATION_INVENTORY:
        value["raw_declaration_count"] = len(value.get("declarations", {}))
    value.pop("evidence_sha256", None)
    value["evidence_sha256"] = contract.digest(value)
    return value


def report(*, axiom=False):
    result = {
        "schema_version": 2, "kind": "bump_module_inspection", "verified": True,
        "declaration_inventory": "raw-module-constants-v1", "raw_declaration_count": 1,
        "complete_inventory": True, "module": "Fixture", "owned_modules": ["Fixture"],
        "imported_modules": ["Init", "External", "Fixture"],
        "issues": [], "environment": {"lean_version": "fixture-v1"},
        "compiled_inputs": {"/fixture/Fixture.olean": {"sha256": "a" * 64}},
        "source_hashes": {"Fixture.lean": "f" * 64}, "source_sha256": contract.digest({"Fixture.lean": "f" * 64}),
        "inspector_sha256": "b" * 64, "executable_sha256": "c" * 64, "policy_sha256": "d" * 64,
        "declarations": {"result": {"name": "result", "module": "Fixture", "kind": "theorem",
                                    "direct_sorry": False, "axioms": ["Trust"] if axiom else []}},
        "meanings": {"result": meaning("result", type_=const("External.value"), deps=["External.value"]),
                     "External.value": meaning("External.value", "def", module="External")},
    }
    if axiom:
        result["meanings"]["Trust"] = meaning("Trust", "axiom", module="External")
    return seal(result)


class BumpContractTests(unittest.TestCase):
    def compare(self, old, new, passed, fragment="", **kwargs):
        result = contract.compare_module(old, new, **kwargs)
        self.assertEqual(result["passed"], passed, result)
        self.assertEqual(result["evidence_sha256"], contract.digest(
            {k: v for k, v in result.items() if k != "evidence_sha256"}))
        if fragment:
            self.assertIn(fragment, "\n".join(result["issues"]))
        return result

    def test_exact_identity_and_changed_compiler_receipt_are_allowed(self):
        old = report()
        new = copy.deepcopy(old)
        new["environment"]["lean_version"] = "fixture-v2"
        new["executable_sha256"] = "e" * 64
        self.compare(old, seal(new), True)

    def test_same_named_external_definition_drift_is_rejected(self):
        old = report()
        new = copy.deepcopy(old)
        new["meanings"]["External.value"]["meaning"]["value"] = ["natVal", 2]
        self.compare(old, seal(new), False, "semantic meaning changed: External.value")

    def test_transitive_external_drift_is_rejected(self):
        old = report()
        old["meanings"]["External.value"] = meaning("External.value", "def", body=const("External.deep"), deps=["External.deep"], module="External")
        old["meanings"]["External.deep"] = meaning("External.deep", "def", module="External")
        seal(old)
        new = copy.deepcopy(old)
        new["meanings"]["External.deep"]["meaning"]["value"] = ["natVal", 2]
        self.compare(old, seal(new), False, "semantic meaning changed: External.deep")

    def test_explicit_bijective_rename_checks_bodies_and_types(self):
        old = report()
        new = copy.deepcopy(old)
        new["meanings"]["External.new"] = new["meanings"].pop("External.value")
        new["meanings"]["External.new"]["meaning"]["name"] = name("External.new")
        new["meanings"]["External.new"]["module"] = "MovedLibrary"
        new["meanings"]["result"]["meaning"]["type"] = const("External.new")
        new["meanings"]["result"]["dependencies"] = ["External.new"]
        seal(new)
        self.compare(old, new, False, "removed or unmapped")
        mapping = {"External.value": "External.new"}
        self.compare(old, new, True, correspondences=mapping)
        new["meanings"]["External.new"]["meaning"]["value"] = ["natVal", 2]
        self.compare(old, seal(new), False, "semantic meaning changed", correspondences=mapping)

    def test_non_bijective_rename_and_missing_target_fail(self):
        original = report()
        self.compare(original, original, False, "not bijective", correspondences={"External.value": "result"})
        self.compare(original, original, False, "invalid", correspondences={"External.value": "Missing"})

    def test_renames_never_rename_binder_or_universe_names(self):
        original = report()
        original["meanings"]["result"]["meaning"]["type"] = ["forallE", name("External.value"), const("External.value"), ["bvar", 0], "default"]
        seal(original)
        new = copy.deepcopy(original)
        new["meanings"]["External.new"] = new["meanings"].pop("External.value")
        new["meanings"]["External.new"]["meaning"]["name"] = name("External.new")
        new["meanings"]["result"]["meaning"]["type"][2] = const("External.new")
        new["meanings"]["result"]["dependencies"] = ["External.new"]
        self.compare(original, seal(new), True, correspondences={"External.value": "External.new"})

    def test_removed_original_declaration_fails_even_if_its_meaning_survives(self):
        original = report()
        current = copy.deepcopy(original)
        current["declarations"].clear()
        self.compare(original, seal(current), False, "original declaration removed")

    def test_per_declaration_axiom_expansion_is_rejected(self):
        old = report(axiom=True)
        old["meanings"]["other"] = meaning("other")
        old["declarations"]["other"] = {"name": "other", "module": "Fixture", "kind": "theorem", "direct_sorry": False, "axioms": []}
        seal(old)
        new = copy.deepcopy(old)
        new["declarations"]["other"]["axioms"] = ["Trust"]
        self.compare(old, seal(new), False, "trusted assumptions expanded for other")

    def test_old_axiom_may_remain_but_same_named_axiom_type_may_not_change(self):
        old = report(axiom=True)
        self.compare(old, old, True)
        new = copy.deepcopy(old)
        new["meanings"]["Trust"]["meaning"]["type"] = ["sort", ["succ", ["zero"]]]
        self.compare(old, seal(new), False, "semantic meaning changed: Trust")

    def test_proof_can_drop_assumptions(self):
        self.compare(report(axiom=True), report(), True)

    def test_preexisting_hole_can_remain_but_new_hole_cannot(self):
        old = report()
        new = copy.deepcopy(old)
        new["meanings"]["sorryAx"] = meaning("sorryAx", "axiom", module="Init")
        new["declarations"]["result"].update(direct_sorry=True, axioms=["sorryAx"])
        seal(new)
        self.compare(old, new, False, "new direct proof hole")
        self.compare(new, new, True)

    def test_new_helpers_are_audited_and_cannot_add_native_trust(self):
        old = report()
        for kind, assumption in [("theorem", None), ("axiom", None), ("theorem", "Lean.trustCompiler"), ("theorem", "foo._native.native_decide.ax_1")]:
            with self.subTest(kind=kind, assumption=assumption):
                new = copy.deepcopy(old)
                new["meanings"]["helper"] = meaning("helper", kind)
                new["declarations"]["helper"] = {"name": "helper", "module": "Fixture", "kind": kind, "direct_sorry": False, "axioms": [assumption] if assumption else []}
                if assumption:
                    new["meanings"][assumption] = meaning(assumption, "axiom", module="Init")
                self.compare(old, seal(new), kind == "theorem" and assumption is None)

    def test_missing_closure_prettyprint_only_and_unsupported_reports_fail(self):
        old = report()
        for field in ("declarations", "meanings", "compiled_inputs", "environment"):
            new = copy.deepcopy(old)
            del new[field]
            self.compare(old, seal(new), False)
        new = copy.deepcopy(old)
        del new["meanings"]["External.value"]
        self.compare(old, seal(new), False, "missing transitive semantic evidence")
        new = copy.deepcopy(old)
        new["meanings"]["result"]["meaning"]["type"] = "External.value"
        self.compare(old, seal(new), False)
        new = copy.deepcopy(old)
        new["schema_version"] = 999
        self.compare(old, seal(new), False, "unsupported")
        self.compare(None, old, False, "missing inspection")

    def test_new_helper_cannot_spread_inherited_custom_assumptions(self):
        original = report(axiom=True)
        current = copy.deepcopy(original)
        current["meanings"]["helper"] = meaning("helper")
        current["declarations"]["helper"] = {"name": "helper", "module": "Fixture",
            "kind": "theorem", "direct_sorry": False, "axioms": ["Trust"]}
        self.compare(original, seal(current), False, "new helper introduces")

    def test_new_helper_standard_assumption_still_requires_original_allowance(self):
        original = report()
        original["meanings"]["Classical.choice"] = meaning("Classical.choice", "axiom", module="Init.Prelude")
        original["environment"]["lean_sysroot"] = "/toolchain"
        original["imported_modules"] = ["Init.Prelude", "External", "Fixture"]
        original["compiled_modules"] = ["/toolchain/lib/lean/Init/Prelude.olean", "/fixture/External.olean", "/fixture/Fixture.olean"]
        original["compiled_inputs"]["/toolchain/lib/lean/Init/Prelude.olean"] = {
            "path": "/toolchain/lib/lean/Init/Prelude.olean", "sha256": "a" * 64}
        original["declarations"]["result"]["axioms"] = ["Classical.choice"]
        seal(original)
        current = copy.deepcopy(original)
        current["meanings"]["helper"] = meaning("helper")
        current["declarations"]["helper"] = {"name": "helper", "module": "Fixture",
            "kind": "theorem", "direct_sorry": False, "axioms": ["Classical.choice"]}
        self.compare(original, seal(current), True)
        self.compare(report(), current, False, "new helper introduces")
        for changed in ("local_origin", "shadowed_core", "missing_path", "unsafe", "owned_core"):
            with self.subTest(changed=changed):
                altered = copy.deepcopy(current)
                if changed == "local_origin":
                    altered["meanings"]["Classical.choice"]["module"] = "Fixture"
                elif changed == "shadowed_core":
                    altered["compiled_inputs"]["/toolchain/lib/lean/Init/Prelude.olean"]["path"] = "/fixture/Init/Prelude.olean"
                elif changed == "missing_path":
                    altered.pop("compiled_modules")
                elif changed == "unsafe":
                    altered["meanings"]["Classical.choice"]["meaning"]["unsafe"] = True
                else:
                    altered["owned_modules"].append("Init.Prelude")
                self.compare(original, seal(altered), False, "new helper introduces")

    def test_tampered_report_digest_fails(self):
        old = report()
        new = copy.deepcopy(old)
        new["meanings"]["External.value"]["meaning"]["value"] = ["natVal", 9]
        self.compare(old, new, False, "digest mismatch")

    def test_raw_module_inventory_is_explicit_complete_and_integer(self):
        original = report()
        for marker, count in [(None, 1), ("merged-environment", 1),
                              (contract.DECLARATION_INVENTORY, 0),
                              (contract.DECLARATION_INVENTORY, 2),
                              (contract.DECLARATION_INVENTORY, True)]:
            with self.subTest(marker=marker, count=count):
                current = copy.deepcopy(original)
                current.update(declaration_inventory=marker, raw_declaration_count=count)
                current.pop("evidence_sha256")
                current["evidence_sha256"] = contract.digest(current)
                self.compare(original, current, False, "raw module declaration inventory")

    def test_legacy_merged_environment_report_cannot_claim_occurrence_coverage(self):
        original = report()
        legacy = copy.deepcopy(original)
        legacy["schema_version"] = 1
        self.compare(original, seal(legacy), False, "unsupported")

    def test_unknown_structural_expression_fails_even_when_equal(self):
        value = report()
        value["meanings"]["result"]["meaning"]["type"] = ["unimplementedFutureNode", "text"]
        seal(value)
        self.compare(value, value, False, "unsupported")

    def test_empty_non_mapping_correspondence_is_invalid(self):
        value = report()
        self.compare(value, value, False, "invalid", correspondences=[])

    def test_definition_missing_body_and_hidden_external_reference_fail(self):
        old = report()
        new = copy.deepcopy(old)
        del new["meanings"]["External.value"]["meaning"]["value"]
        self.compare(old, seal(new), False, "incomplete structural meaning")
        new = copy.deepcopy(old)
        new["meanings"]["result"]["dependencies"] = []
        self.compare(old, seal(new), False, "incomplete structural meaning")

    def test_native_context_arguments_fail_before_launch(self):
        with patch.object(contract, "_run", side_effect=AssertionError("must not launch")):
            for module, owned in [("-x", ["-x"]), ("Fixture", []), ("Fixture/Bad", ["Fixture/Bad"])]:
                with self.assertRaises(ValueError):
                    contract.inspect_module(Path.cwd(), module, owned)


def migration_baseline():
    reports = {"Fixture": report(), "Empty": report()}
    for module, row in reports.items():
        row.update(module=module, owned_modules=["Empty", "Fixture"],
                   imported_modules=["Init", "External", "Fixture", *(["Empty"] if module == "Empty" else [])])
        row["compiled_modules"] = [("/source/.lake/build/lib/lean/" if name in {"Empty", "Fixture"}
                                    else "/toolchain/") + name + ".olean" for name in row["imported_modules"]]
        row["compiled_inputs"] = {path: {"path": path, "sha256": "a" * 64} for path in row["compiled_modules"]}
        row["source_hashes"] = {"Fixture.lean": "f" * 64, "Empty.lean": "e" * 64}
        row["source_sha256"] = contract.digest(row["source_hashes"])
        row["environment"]["config"] = {"lean-toolchain": "a" * 64}
        if module == "Empty":
            row["declarations"], row["meanings"] = {}, {}
        seal(row)
    graph = {"Fixture": {"path": "Fixture.lean", "imports": [], "compiler_derived": True},
             "Empty": {"path": "Empty.lean", "imports": ["Fixture"], "compiler_derived": True}}
    migration = {"root": "/source", "target": "/target", "target_sealed": True,
                 "target_config": {"lean-toolchain": "a" * 64}, "source_commit": "1" * 40,
                 "source_files": {"Fixture.lean": "f" * 64, "Empty.lean": "e" * 64},
                 "original_config": {"lean-toolchain": "a" * 64},
                 "source_hash": "2" * 64, "target_version": "leanprover/lean4:v4.34.1"}
    scope = {"version": 1, "mode": "all", "kind": "all_project_modules", "default_build_required": True,
             "selected_modules": {module: row["path"] for module, row in graph.items()},
             "excluded_modules": {}, "excluded_files": {}, "native_default_modules": {}, "native_metadata": {}}
    scope["sha256"] = bump_migration_project._digest(scope)
    migration["scope"] = scope
    migration["identity"] = bump_migration_project._digest(migration)
    records = bump_project.migration_declarations(reports)
    return bump_project._seal({"version": 5, "policy": "migration-v1", "scope_policy": 1, "occurrence_policy": 1,
        "build_scope": copy.deepcopy(scope), "project_root": "/target",
        "branch": "target", "files": {"Fixture.lean": "f" * 64, "Empty.lean": "e" * 64,
                                      "lean-toolchain": "a" * 64},
        "environment": {"lean_version": "target"}, "migration": migration,
        "compiler_modules": graph, "original_reports": reports, "declarations": records,
        "scope": {"mode": "all", "bound": True, "existing_targets": sorted(records)},
        "layout": {"modules": {"Fixture.lean": "Fixture", "Empty.lean": "Empty"}}})


def migration_contract():
    baseline = migration_baseline()
    bindings, targets = adapter._migration_bindings(baseline)
    value = {"version": 3, "migration_policy": 1, "migration_scope_policy": 1, "migration_occurrence_policy": 1,
             "inspection_policy": 4, "fingerprint_version": 2,
             "project_baseline": baseline, "bindings": bindings, "targets": targets,
             "obligation_ids": sorted(bindings), "spec": {}, "spec_sha256": adapter.digest({}),
             "environment": baseline["environment"]}
    value["adopted_outputs"] = adapter.adopted_output_records(value)
    return adapter._seal_contract(value)


class BumpMigrationAdapterTests(unittest.TestCase):
    def test_baseline_binds_native_originals_and_compiler_ownership(self):
        baseline = migration_baseline()
        self.assertTrue(bump_project.baseline_is_valid(baseline))
        baseline["compiler_modules"]["Empty"]["path"] = "Fixture.lean"
        self.assertFalse(bump_project.baseline_is_valid(bump_project._seal(baseline)))

    def test_empty_module_has_fixed_empty_inventory(self):
        value = migration_contract()
        self.assertEqual(value["bindings"]["Empty"], [])
        self.assertEqual(adapter.output_manifest_blockers(value, task_ids={"Fixture", "Empty"},
                                                         task_id="Empty", proposed_outputs=[]), [])
        self.assertEqual(adapter.adopted_output_paths(value), {"Fixture.lean", "Empty.lean"})

    def test_original_inventory_cannot_be_removed_or_extended(self):
        value = migration_contract()
        for outputs in ([], [{"declaration": "new", "file": "Fixture.lean"}]):
            self.assertTrue(adapter.output_manifest_blockers(value, task_ids={"Fixture", "Empty"},
                                                            task_id="Fixture", proposed_outputs=outputs))

    def test_module_build_never_builds_unrelated_broken_target(self):
        with patch.object(bump_project, "require_pinned_inputs"), patch.object(bump_migration_project, "build",
                return_value={"passed": True, "returncode": 0, "diagnostics": "ok"}) as build:
            adapter.build_sources(Path("/target"), full=True, baseline=migration_baseline(),
                                  tasks=[{"task_id": "Fixture"}])
        build.assert_called_once_with(Path("/target"), ["Fixture"], scope=migration_baseline()["build_scope"])

    def test_final_build_includes_defaults_and_every_module(self):
        with patch.object(bump_project, "require_pinned_inputs"), patch.object(bump_migration_project, "build",
                return_value={"passed": True, "returncode": 0, "diagnostics": "ok"}) as build:
            adapter.build_sources(Path("/target"), full=True, baseline=migration_baseline())
        self.assertEqual([row.args[1] for row in build.call_args_list], [None, ["Empty", "Fixture"]])

    def run_check(self, module, *, final=False, mutate=None):
        value = migration_contract()
        reports = copy.deepcopy(value["project_baseline"]["original_reports"])
        for row in reports.values():
            row["compiled_modules"] = [path.replace("/source/", "/target/") for path in row["compiled_modules"]]
            row["compiled_inputs"] = {path: {"path": path, "sha256": "a" * 64} for path in row["compiled_modules"]}
            seal(row)
        if mutate:
            mutate(reports)
        identity = {"main_sha": "1" * 40, "source_sha256": "f" * 64, "environment": value["environment"]}
        with patch.object(bump_project, "require_pinned_inputs"), patch.object(adapter, "source_identity", return_value=identity), \
             patch.object(contract, "inspect_module", side_effect=lambda root, module, owned: reports[module]) as inspect, \
             patch.object(adapter.bump_cache, "compiled_receipt", return_value={"artifact_id": "a", "sha256": "b"}), \
             patch.object(adapter.bump_cache, "compiled_receipt_current", return_value=True):
            result = adapter.check_formal_contract(Path("/target"), value, [], completed={module}, task_id=module,
                proposed_outputs=value["bindings"][module], final=final)
        return result, inspect

    def test_partial_native_check_and_empty_module_receipt(self):
        result, inspect = self.run_check("Empty")
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["verified_targets"], {})
        self.assertTrue(result["module_receipt"]["passed"])
        self.assertEqual(result["module_receipt"]["module"], "Empty")
        self.assertEqual([call.args[1] for call in inspect.call_args_list], ["Empty"])

    def test_final_native_check_covers_every_module(self):
        result, inspect = self.run_check("Fixture", final=True)
        self.assertTrue(result["passed"], result)
        self.assertEqual([call.args[1] for call in inspect.call_args_list], ["Empty", "Fixture"])
        value = migration_contract()
        self.assertEqual(result["verified_targets"], adapter.output_fingerprints(value, "Fixture"))

    def test_native_external_drift_reaches_production_rejection(self):
        def mutate(reports):
            reports["Fixture"]["meanings"]["External.value"]["meaning"]["value"] = ["natVal", 2]
            seal(reports["Fixture"])
        result, _ = self.run_check("Fixture", mutate=mutate)
        self.assertFalse(result["passed"])
        self.assertIn("semantic meaning changed", " ".join(result["issues"]))

    def test_tampered_original_contract_fails_before_inspection(self):
        value = migration_contract()
        value["targets"][adapter.output_target_key(value, "Fixture", "result")]["fingerprint"] = "e" * 64
        value = adapter._seal_contract(value)
        with patch.object(contract, "inspect_module") as inspect:
            result = adapter.check_formal_contract(Path("/target"), value, [], completed={"Fixture"}, task_id="Fixture")
        self.assertFalse(result["passed"])
        inspect.assert_not_called()

    def test_final_snapshot_requires_every_native_module_receipt(self):
        value = migration_contract()
        state = {"project_baseline": value["project_baseline"], "formalization": {"contract": value}}
        checked, _ = self.run_check("Fixture", final=True)
        review = {**checked, **checked["source_identity"], "policy_sha256": adapter.policy_hash(),
                  "declarations": adapter.snapshot_declarations(value),
                  "declaration_occurrences": adapter.declaration_occurrences(value),
                  "project_baseline_sha256": value["project_baseline"]["sha256"],
                  "project_verification": adapter.project_verification(Path("/target"), value["project_baseline"])}
        adapter.validate_migration_snapshot(state, review)
        del review["module_receipts"]["Empty"]
        with self.assertRaisesRegex(ValueError, "complete native module"):
            adapter.validate_migration_snapshot(state, review)

    def test_original_native_receipt_is_bound_to_original_source_bytes(self):
        baseline = migration_baseline()
        row = baseline["original_reports"]["Empty"]
        row["source_hashes"]["Empty.lean"] = "d" * 64
        row["source_sha256"] = contract.digest(row["source_hashes"])
        seal(row)
        baseline = bump_project._seal(baseline)
        self.assertFalse(bump_project.baseline_is_valid(baseline))

    def test_protected_configuration_rejection_precedes_lake(self):
        baseline = migration_baseline()
        with patch.object(bump_migration_project, "validate_original", return_value=[]), \
             patch.object(bump_migration_project, "config_hashes", return_value={}), \
             patch.object(bump_migration_project, "validate_dependencies") as dependencies:
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                bump_project.require_pinned_inputs(Path("/target"), baseline)
        dependencies.assert_not_called()


if __name__ == "__main__":
    unittest.main()
