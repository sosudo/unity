"""Offline scope/build/inspection boundaries; no models or external services."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import formalize_contract as contract
from unity import formalize_runtime as runtime
from unity import formalize_scope as scope, formalize_project as project, formalize_input
import test_formalize_context_contract as context_fixture


class ScopeContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="unity-scope-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = {
            "modules": {"Lib.lean": "Lib", "Aux.lean": "Aux", "Broken.lean": "Broken"},
            "traces": {name: f".lake/build/{name}.trace" for name in ("Lib", "Aux", "Broken")},
            "build_dir": ".lake/build", "libraries": ["Lib"],
            "source_roots": [], "unmatched": [], "issues": [],
        }
        self.layout = {**deepcopy(self.raw), "project_scope": "libraries", "scope_sha256": "scope-one",
                       "verification_modules": {"Lib.lean": "Lib", "Aux.lean": "Aux"},
                       "editable_modules": {"Lib.lean": "Lib"}}
        self.executable = self.root / "inspector"
        self.executable.write_bytes(b"offline native fixture")
        for path in self.raw["modules"]:
            (self.root / path).write_text("-- test fixture\n")
        for path in self.raw["traces"].values():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("old compiled trace")

    def build(self, layout):
        with patch.object(contract.formalize_jobs, "run", return_value=
                          subprocess.CompletedProcess([], 0, "ok", "")) as run:
            result = contract.build_sources(self.root, full=True, layout=layout)
        self.assertEqual(result["returncode"], 0)
        return [call.args[1] for call in run.call_args_list]

    def test_library_build_never_uses_unqualified_default_or_excluded_facets(self):
        self.assertEqual(self.build(self.layout), [
            ["lake", "build", "Lib"], ["lake", "--rehash", "build", "+Aux", "+Lib"]])
        self.assertTrue((self.root / self.raw["traces"]["Broken"]).exists())
        self.assertFalse((self.root / self.raw["traces"]["Aux"]).exists())
        receipt = json.loads((self.root / ".unity/formalize-build-inputs.json").read_text())
        self.assertEqual(receipt["scope_sha256"], "scope-one")
        self.assertEqual(receipt["modules"], self.layout["verification_modules"])

    def test_all_mode_preserves_default_and_every_owned_module(self):
        self.assertEqual(self.build(self.raw), [
            ["lake", "build"], ["lake", "--rehash", "build", "+Aux", "+Broken", "+Lib"]])

    def test_required_build_failure_is_not_ignored(self):
        with patch.object(contract.formalize_jobs, "run", return_value=
                          subprocess.CompletedProcess([], 1, "required tool failed", "")) as run:
            result = contract.build_sources(self.root, full=True, layout=self.layout)
        self.assertEqual(result["returncode"], 1)
        self.assertIn("required tool failed", result["output"])
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.root / ".unity/formalize-build-inputs.json").exists())

    def test_scope_change_invalidates_build_receipt(self):
        self.build(self.layout)
        path = self.root / self.raw["traces"]["Lib"]
        path.write_text("fresh trace")
        changed = {**self.layout, "scope_sha256": "scope-two"}
        self.build(changed)
        self.assertFalse(path.exists())

    def test_full_source_identity_is_independent_of_derived_scope_annotations(self):
        with patch.object(contract, "environment_identity", return_value={}), \
             patch.object(contract, "_git", return_value="head"):
            original = contract.source_identity(self.root, layout=self.raw)
            self.assertEqual(original, contract.source_identity(self.root, layout=self.layout))
            (self.root / "Broken.lean").write_text("-- auxiliary drift\n")
            self.assertNotEqual(original, contract.source_identity(self.root, layout=self.layout))

    def inspect(self, imported, compiled=None):
        data = {"targets": {}, "project_declarations": [], "project_records": {},
                "external_declarations": {}, "prerequisite_declarations": {},
                "issues": [], "declaration_errors": [], "project_axioms": [],
                "project_sorries": [], "project_used_axioms": [], "compiled_modules": []}
        if imported is not None:
            data["imported_modules"] = imported
            data["compiled_modules"] = compiled if compiled is not None else [
                str(self.root / ".lake/build/lib/lean" / f"{name}.olean")
                if name in self.raw["modules"].values() else f"/fixture/toolchain/{name}.olean"
                for name in imported]
        with patch.object(contract.formalize_native, "executable", return_value=self.executable), \
             patch.object(contract.formalize_jobs, "run", return_value=
                          subprocess.CompletedProcess([], 0, json.dumps(data), "")) as run:
            result = contract.inspect_environment(self.root, [], layout=self.layout, _inventory_only=True)
        return result, run.call_args.args[1]

    def test_required_auxiliary_is_an_inspected_project_root(self):
        result, command = self.inspect(["Init", "Aux", "Lib"])
        self.assertEqual(command[3:5], ["Aux", "Lib"])
        self.assertNotIn("Broken", command)
        self.assertEqual(result["imported_modules"], ["Init", "Aux", "Lib"])

    def test_actual_kernel_imports_cannot_smuggle_excluded_owned_module(self):
        with self.assertRaisesRegex(contract.ContractInspectionError, "cross the frozen"):
            self.inspect(["Init", "Aux", "Lib", "Broken"])

    def test_missing_or_incomplete_native_import_evidence_fails_closed(self):
        for imports in (None, ["Lib"], ["Aux", "Lib", 1]):
            with self.subTest(imports=imports), self.assertRaisesRegex(
                    contract.ContractInspectionError, "actual imported module"):
                self.inspect(imports)

    def test_unowned_local_artifact_cannot_be_misclassified_as_external(self):
        for location in (".lake/build/lib/lean/Unmatched.olean", "Unmatched.olean"):
            with self.subTest(location=location), self.assertRaisesRegex(
                    contract.ContractInspectionError, "provenance crosses"):
                self.inspect(["Aux", "Lib", "Unmatched"], [
                    str(self.root / ".lake/build/lib/lean/Aux.olean"),
                    str(self.root / ".lake/build/lib/lean/Lib.olean"), str(self.root / location)])

    def test_pinned_dependency_within_project_is_not_auxiliary_project_source(self):
        dependency = self.root / ".lake/packages/dep"
        with patch.object(contract, "_dependencies", return_value={"dep": {"path": str(dependency)}}):
            result, _ = self.inspect(["Aux", "Lib", "External"], [
                str(self.root / ".lake/build/lib/lean/Aux.olean"),
                str(self.root / ".lake/build/lib/lean/Lib.olean"),
                str(dependency / ".lake/build/lib/lean/External.olean")])
        self.assertIn("External", result["imported_modules"])

    def test_incomplete_provenance_and_project_module_shadow_fail_closed(self):
        for paths in ([], ["relative.olean", "other.olean"],
                      ["/fixture/toolchain/Aux.olean", "/fixture/toolchain/Lib.olean"]):
            with self.subTest(paths=paths), self.assertRaisesRegex(
                    contract.ContractInspectionError, "provenance"):
                self.inspect(["Aux", "Lib"], paths)

    def test_agent_plan_offers_only_editable_incomplete_declarations(self):
        baseline = {"scope": {"existing_targets": [], "bound": False},
                    "project_axioms": ["aux_hole"], "project_sorries": ["target"],
                    "declarations": {"aux_hole": {"module": "Aux"}, "target": {"module": "Lib"}},
                    "layout": self.raw, "verification_scope": {"editable_modules": {"Lib.lean": "Lib"}}}
        state = {"formalization": {}, "project_baseline": baseline}
        paths = SimpleNamespace(forum=self.root / "forum")
        with patch.object(runtime.formalize_state, "load_state", return_value=state):
            file = runtime.write_formalization_plan(paths, {"candidate_id": "source", "sha256": "hash", "source_refs": []})
        plan = json.loads(file.read_text())["project_baseline"]
        self.assertEqual(set(plan["existing_incomplete_declarations"]), {"target"})
        self.assertEqual(plan["verification_scope"], baseline["verification_scope"])

    def test_current_report_distinguishes_semantic_and_byte_only_coverage(self):
        baseline = {"verification_scope": {"sha256": "scope-one", "selected_libraries": ["Lib"]}}
        with patch.object(contract, "workspace_layout", return_value=self.raw), \
             patch.object(contract, "scoped_layout", return_value=self.layout):
            result = contract.project_verification(self.root, baseline)
        self.assertEqual(result["readonly_imported_modules"], {"Aux.lean": "Aux"})
        self.assertEqual(result["byte_only_modules"], {"Broken.lean": "Broken"})
        self.assertEqual(result["editable_modules"], {"Lib.lean": "Lib"})
        self.assertIsNone(contract.project_verification(self.root, {}))

    def test_inspection_cache_is_bound_to_scope_even_with_same_modules_and_source(self):
        (self.root / ".git").mkdir()
        identity = {"source_sha256": "unchanged", "environment": {"dependencies": {}}}
        with patch.object(contract, "source_identity", return_value=identity), \
             patch.object(contract.formalize_cache, "lookup", return_value=(None, None)) as lookup, \
             patch.object(contract.formalize_cache, "publish", return_value=None):
            self.inspect(["Aux", "Lib"])
            first = lookup.call_args.args[1]
            self.layout["scope_sha256"] = "different-policy"
            self.inspect(["Aux", "Lib"])
            second = lookup.call_args.args[1]
        self.assertNotEqual(first, second)


class LibrarySnapshotTests(unittest.TestCase):
    setUp = context_fixture.ContextContractTests.setUp
    check = context_fixture.ContextContractTests.check

    def test_final_snapshot_requires_exact_current_library_coverage(self):
        self.layout.update(libraries=["Project"], module_owners={
            "Project.lean": {"libraries": ["Project"], "executables": []}})
        with patch.object(scope, "_headers", return_value={"Project.lean": ["Init"]}):
            policy = scope.capture(self.root, self.layout, "libraries")
            self.layout = scope.apply(self.root, self.layout, policy)
            self.baseline = project._seal({**self.baseline, "layout": self.layout, "verification_scope": policy})
            self.current = contract._seal_contract({**self.current, "project_baseline": self.baseline})
            checked, _ = self.check(final=True)
        self.assertTrue(checked["passed"], checked)
        adopted = checked["proposed_contract"]
        state = {"project_baseline": self.baseline,
                 "input_source": {"candidate_id": "source", "sha256": "source-hash"},
                 "problem_sha256": hashlib.sha256(formalize_input.scope_bytes(self.paths)).hexdigest(),
                 "formalization": {"contract": adopted, "spec": self.spec, "main_sha": self.head, "revision": 1,
                                   "solution_candidate": "source", "solution_sha256": "source-hash"},
                 "formal_tasks": {"one": {**self.tasks[0], "outputs": self.outputs, "accepted_candidate": "candidate"}},
                 "formal_candidates": {}}
        identity = {"main_sha": self.head, "source_sha256": "f" * 64, "environment": {}}
        with patch.object(contract, "source_identity", return_value=identity), \
             patch.object(project, "require_pinned_inputs", return_value=None), \
             patch.object(contract, "build_sources", return_value={"returncode": 0, "output": "fixture"}) as build, \
             patch.object(contract, "check_formal_contract", return_value=checked), \
             patch.object(contract, "workspace_layout", return_value=self.layout), \
             patch.object(scope, "_headers", return_value={"Project.lean": ["Init"]}), \
             patch.object(formalize_input, "source_matches", return_value=True), \
             patch.object(contract.formalize_cache, "compiled_receipt_current", return_value=True):
            report = contract.verify_final_project(self.paths, state)
            self.assertTrue(report["passed"], report)
            self.assertEqual(build.call_args.kwargs["baseline"], self.baseline)
            self.assertTrue(contract.snapshot_is_current(self.paths, state, report))
            for replacement in (None, {**report["project_verification"], "scope_sha256": "other"},
                                {**report["project_verification"], "verification_modules": {}}):
                altered = {**report, "project_verification": replacement}
                self.assertFalse(contract.snapshot_is_current(self.paths, state, altered))


if __name__ == "__main__":
    unittest.main()
