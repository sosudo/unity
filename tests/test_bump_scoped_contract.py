"""Offline boundaries for scoped Bump evidence; no models, services or Lean runs."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_contract as contract, bump_project as project, bump_report
from unity import bump_migration_contract as native, bump_migration_project as migration
if __package__:
    from .test_bump_contract import migration_baseline, seal
else:
    from test_bump_contract import migration_baseline, seal


def baseline_fixture():
    baseline = migration_baseline()
    extra = {"Scratch.lean": "a" * 64, "Notes.lean": "b" * 64, "README.md": "c" * 64}
    baseline["files"].update(extra)
    baseline["migration"]["source_files"].update(extra)
    modules = {"Fixture.lean": "Fixture", "Empty.lean": "Empty", "Scratch.lean": "Scratch"}
    scope = {
        "version": 1, "mode": "build", "kind": "default_build_closure", "default_build_required": True,
        "selected_modules": {"Fixture": "Fixture.lean", "Empty": "Empty.lean"},
        "excluded_modules": {"Scratch": "Scratch.lean"}, "excluded_files": extra,
        "native_default_modules": {"Empty": "Empty.lean"},
        "native_metadata": {"modules": modules,
            "module_owners": {path: {"libraries": [module], "executables": []} for path, module in modules.items()},
            "source_roots": [{"kind": "library", "name": module, "path": "."} for module in modules.values()],
            "default_targets": [{"kind": "library", "name": "Empty", "path": "."}],
            "build_dir": ".lake/build"},
    }
    scope["sha256"] = migration._digest(scope)
    baseline["build_scope"] = scope
    baseline["migration"]["scope"] = deepcopy(scope)
    baseline["migration"].pop("identity")
    baseline["migration"]["identity"] = migration._digest(baseline["migration"])
    baseline["layout"]["modules"] = modules
    for report in baseline["original_reports"].values():
        report["source_hashes"].update({name: sha for name, sha in extra.items() if name.endswith(".lean")})
        report["source_sha256"] = native.digest(report["source_hashes"])
        seal(report)
    return project._seal(baseline)


def contract_fixture():
    baseline = baseline_fixture()
    bindings, targets = contract._migration_bindings(baseline)
    value = {"version": 3, "migration_policy": 1, "migration_scope_policy": 1, "migration_occurrence_policy": 1,
        "inspection_policy": 4, "fingerprint_version": 2, "project_baseline": baseline,
        "bindings": bindings, "targets": targets, "obligation_ids": sorted(bindings),
        "spec": {}, "spec_sha256": contract.digest({}), "environment": baseline["environment"]}
    value["adopted_outputs"] = contract.adopted_output_records(value)
    return contract._seal_contract(value)


class ScopedContractTests(unittest.TestCase):
    def test_selected_native_and_excluded_byte_only_partition(self):
        baseline = baseline_fixture()
        self.assertTrue(project.baseline_is_valid(baseline), project.baseline_errors(baseline))
        coverage = contract.project_verification(Path("/target"), baseline)
        self.assertEqual(coverage["inspection_policy"], 4)
        self.assertEqual(coverage["contexts"], ["Empty", "Fixture"])
        self.assertEqual(coverage["byte_only_modules"], {"Scratch.lean": "Scratch"})
        self.assertEqual(set(coverage["byte_preserved_files"]), {"Notes.lean", "Scratch.lean", "README.md"})
        self.assertNotIn("Scratch", coverage["contexts"])

    def test_old_baseline_or_missing_scope_never_downgrades(self):
        for action in (lambda b: b.update(version=3), lambda b: b.pop("scope_policy"),
                       lambda b: b.pop("build_scope"), lambda b: b["migration"].pop("scope")):
            baseline = baseline_fixture()
            action(baseline)
            self.assertFalse(project.baseline_is_valid(project._seal(baseline)))

    def test_resealed_graph_cannot_expand_selected_boundary(self):
        baseline = baseline_fixture()
        baseline["compiler_modules"]["Scratch"] = {"path": "Scratch.lean", "imports": [], "compiler_derived": True}
        self.assertFalse(project.baseline_is_valid(project._seal(baseline)))

    def test_original_report_cannot_already_import_excluded_module(self):
        baseline = baseline_fixture()
        row = baseline["original_reports"]["Fixture"]
        row["imported_modules"].append("Scratch")
        seal(row)
        self.assertIn("frozen migration build scope", " ".join(project.baseline_errors(project._seal(baseline))))

    def guarded_inputs(self, files, baseline=None):
        baseline = baseline or baseline_fixture()
        with patch.object(migration, "validate_original", return_value=[]), \
             patch.object(migration, "config_hashes", return_value=baseline["migration"]["target_config"]), \
             patch.object(migration, "validate_dependencies", return_value=[]), \
             patch.object(migration, "source_files", return_value=files), \
             patch.object(migration, "validate_build_scope", return_value=[]) as scope:
            with self.assertRaises(ValueError):
                project.require_pinned_inputs(Path("/target"), baseline)
        scope.assert_not_called()

    def test_excluded_edits_fail_before_native_scope_query(self):
        baseline = baseline_fixture()
        for name in ("Scratch.lean", "Notes.lean", "README.md"):
            files = deepcopy(baseline["files"])
            files[name] = "e" * 64
            with self.subTest(name=name):
                self.guarded_inputs(files, baseline)

    def test_file_additions_and_deletions_fail_before_native_scope_query(self):
        baseline = baseline_fixture()
        for add in (True, False):
            files = deepcopy(baseline["files"])
            if add:
                files["Another.lean"] = "e" * 64
            else:
                files.pop("Notes.lean")
            self.guarded_inputs(files, baseline)

    def test_untracked_non_lean_input_is_not_outside_preservation_guard(self):
        baseline = baseline_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in baseline["files"]:
                (root / name).write_text("fixture")
            all_files = {**baseline["files"], "untracked-input.txt": "e" * 64}
            with patch.object(migration, "validate_original", return_value=[]), \
                 patch.object(migration, "config_hashes", return_value=baseline["migration"]["target_config"]), \
                 patch.object(migration, "validate_dependencies", return_value=[]), \
                 patch.object(migration, "source_files", return_value=baseline["files"]), \
                 patch.object(contract, "_file_hashes", return_value=all_files), \
                 patch.object(migration, "validate_build_scope", return_value=[]) as scope:
                with self.assertRaisesRegex(ValueError, "unaccounted"):
                    project.require_pinned_inputs(root, baseline)
            scope.assert_not_called()

    def test_selected_module_edits_still_reach_native_boundary_validation(self):
        baseline = baseline_fixture()
        current = {**baseline["files"], "Fixture.lean": "e" * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in baseline["files"]:
                (root / name).write_text("fixture")
            with patch.object(migration, "validate_original", return_value=[]), \
                 patch.object(migration, "config_hashes", return_value=baseline["migration"]["target_config"]), \
                 patch.object(migration, "validate_dependencies", return_value=[]), \
                 patch.object(migration, "source_files", return_value=current), \
                 patch.object(contract, "_file_hashes", return_value=current), \
                 patch.object(migration, "validate_build_scope", return_value=[]) as scope:
                project.require_pinned_inputs(root, baseline)
            scope.assert_called_once_with(root.resolve(), baseline["build_scope"])

    def check(self, *, final=False, mutation=None, after_error=False):
        value = contract_fixture()
        reports = deepcopy(value["project_baseline"]["original_reports"])
        for row in reports.values():
            row["compiled_modules"] = [path.replace("/source/", "/target/") for path in row["compiled_modules"]]
            row["compiled_inputs"] = {path: {"path": path, "sha256": "a" * 64} for path in row["compiled_modules"]}
            seal(row)
        if mutation:
            mutation(reports["Fixture"])
            seal(reports["Fixture"])
        identity = {"main_sha": "1" * 40, "source_sha256": "f" * 64, "environment": value["environment"]}
        with patch.object(project, "require_pinned_inputs", side_effect=[None, ValueError("scope changed")] if after_error else None), \
             patch.object(contract, "source_identity", return_value=identity), \
             patch.object(native, "inspect_module", side_effect=lambda root, module, owned: reports[module]) as inspected, \
             patch.object(contract.bump_cache, "compiled_receipt", return_value={"artifact_id": "a", "sha256": "b"}), \
             patch.object(contract.bump_cache, "compiled_receipt_current", return_value=True):
            checked = contract.check_formal_contract(Path("/target"), value, [], completed={"Fixture"},
                task_id="Fixture", proposed_outputs=value["bindings"]["Fixture"], final=final)
        return value, checked, inspected

    def test_partial_and_final_only_inspect_selected_modules(self):
        for final, expected in ((False, ["Fixture"]), (True, ["Empty", "Fixture"])):
            value, checked, inspected = self.check(final=final)
            self.assertTrue(checked["passed"], checked)
            self.assertEqual([row.args[1] for row in inspected.call_args_list], expected)
            self.assertEqual(checked["module_receipt"]["scope_sha256"], value["project_baseline"]["build_scope"]["sha256"])

    def test_current_native_import_into_excluded_module_blocks(self):
        _, checked, _ = self.check(mutation=lambda row: row["imported_modules"].append("Scratch"))
        self.assertFalse(checked["passed"])
        self.assertIn("frozen migration build scope", " ".join(checked["issues"]))

    def test_unknown_unowned_local_artifact_blocks_without_invented_ownership(self):
        def mutate(row):
            filename = "/target/.lake/build/lib/lean/Notes.olean"
            row["imported_modules"].append("Notes")
            row["compiled_modules"].append(filename)
            row["compiled_inputs"][filename] = {"path": filename, "sha256": "a" * 64}
        _, checked, _ = self.check(mutation=mutate)
        self.assertFalse(checked["passed"])
        self.assertIn("native local artifact crosses", " ".join(checked["issues"]))

    def test_missing_native_import_or_artifact_provenance_blocks(self):
        for key in ("imported_modules", "compiled_modules", "compiled_inputs"):
            _, checked, _ = self.check(mutation=lambda row: row.pop(key))
            self.assertFalse(checked["passed"], key)

    def test_selected_module_cannot_resolve_to_external_shadow(self):
        def mutate(row):
            index = row["imported_modules"].index("Fixture")
            filename = "/other/Fixture.olean"
            row["compiled_modules"][index] = filename
            row["compiled_inputs"][filename] = {"path": filename, "sha256": "a" * 64}
        _, checked, _ = self.check(mutation=mutate)
        self.assertFalse(checked["passed"])
        self.assertIn("selected module resolved outside", " ".join(checked["issues"]))

    def test_boundary_is_revalidated_after_inspection(self):
        _, checked, _ = self.check(after_error=True)
        self.assertFalse(checked["passed"])
        self.assertIn("scope changed", checked["issues"])

    def test_snapshot_rejects_scope_receipt_tamper_and_byte_only_native_claim(self):
        value, checked, _ = self.check(final=True)
        state = {"project_baseline": value["project_baseline"], "formalization": {"contract": value}}
        report = {**checked, **checked["source_identity"], "policy_sha256": contract.policy_hash(),
            "declarations": contract.snapshot_declarations(value),
            "declaration_occurrences": contract.declaration_occurrences(value),
            "project_baseline_sha256": value["project_baseline"]["sha256"],
            "project_verification": contract.project_verification(Path("/target"), value["project_baseline"])}
        contract.validate_migration_snapshot(state, report)
        bad = deepcopy(report)
        bad["module_receipts"]["Fixture"]["scope_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "invalid native receipt"):
            contract.validate_migration_snapshot(state, bad)
        bad = deepcopy(report)
        bad["project_verification"]["contexts"].append("Scratch")
        with self.assertRaisesRegex(ValueError, "coverage changed"):
            contract.validate_migration_snapshot(state, bad)

    def test_report_explicitly_disclaims_excluded_kernel_coverage(self):
        baseline = baseline_fixture()
        state = {"project_baseline": baseline}
        snapshot = {"project_verification": contract.project_verification(Path("/target"), baseline)}
        result = bump_report._project_verification(state, snapshot, accepted=True)
        self.assertEqual(result["byte_preserved_excluded_files"], baseline["build_scope"]["excluded_files"])
        self.assertIn("not compiled", result["qualification"])
        self.assertNotIn("Scratch", result["inherited_assumptions_and_holes"])

    def test_contract_without_scope_policy_is_not_resumable(self):
        value = contract_fixture()
        state = {"project_baseline": value["project_baseline"]}
        self.assertTrue(contract._baseline_matches(state, value))
        value.pop("migration_scope_policy")
        self.assertFalse(contract._baseline_matches(state, value))

    def test_path_resolution_cache_is_shared_only_within_one_baseline_check(self):
        baseline = baseline_fixture()
        original = Path.resolve
        calls = []

        def resolve(path, *args, **kwargs):
            calls.append(str(path))
            return original(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self.assertEqual(project.baseline_errors(baseline), [])
            self.assertEqual(calls.count("/toolchain/Init.olean"), 1)
            self.assertEqual(calls.count("/source/.lake/build/lib/lean/Fixture.olean"), 1)
            self.assertEqual(project.baseline_errors(baseline), [])
            self.assertEqual(calls.count("/toolchain/Init.olean"), 2)
            self.assertEqual(calls.count("/source/.lake/build/lib/lean/Fixture.olean"), 2)

    def test_canonical_aliases_and_changed_symlink_are_rechecked_next_invocation(self):
        baseline = baseline_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            artifact = root / ".lake/build/lib/lean/Fixture.olean"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"public fixture")
            external = root / "external/Fixture.olean"
            external.parent.mkdir()
            external.write_bytes(b"other public fixture")
            alias = root / "import-alias.olean"
            alias.symlink_to(artifact)
            row = {"module": "Fixture", "imported_modules": ["Fixture"],
                   "compiled_modules": [str(alias)],
                   "compiled_inputs": {str(alias): {"path": str(artifact), "sha256": "a" * 64}}}
            # Different path strings are allowed only when their actual
            # canonical artifact is identical, as in the uncached guard.
            self.assertEqual(project.migration_inspection_scope_errors(row, baseline, root=root), [])
            alias.unlink()
            alias.symlink_to(external)
            self.assertIn("not bound", " ".join(
                project.migration_inspection_scope_errors(row, baseline, root=root)))
            row["compiled_inputs"][str(alias)]["path"] = str(external)
            self.assertIn("selected module resolved outside", " ".join(
                project.migration_inspection_scope_errors(row, baseline, root=root)))


if __name__ == "__main__":
    unittest.main()
