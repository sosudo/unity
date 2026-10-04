"""Change-focused native/build routing; no models, providers, or evaluations."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_contract as contract


class DeltaContractTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bump-delta-contract-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.layout = {
            "project_scope": "changes", "build_dir": ".lake/build",
            "modules": {"A.lean": "A", "B.lean": "B", "Broken.lean": "Broken"},
            "verification_modules": {"A.lean": "A"}, "traces": {},
            "libraries": ["A", "B"], "source_roots": [], "issues": [], "unmatched": [],
        }
        for filename in self.layout["modules"]:
            (self.root / filename).write_text("-- fixture\n")
        self.executable = self.root / "inspector"
        self.executable.write_bytes(b"fixture")

    def report(self, module, *, target=None, imported=None):
        imported = imported or ["Init", module]
        return {
            "targets": {target: {"module": module, "name": target}} if target else {},
            "project_declarations": [{"name": "overlapping", "module": module, "kind": "def"}],
            "project_records": {"overlapping": {"module": module, "value": module}},
            "external_declarations": {}, "prerequisite_declarations": {},
            "project_axioms": [], "project_sorries": [], "project_used_axioms": [],
            "issues": [], "declaration_errors": [], "imported_modules": imported,
            "compiled_modules": [str(self.root / ".lake/build/lib/lean" / f"{name}.olean")
                                 if name != "Init" else "/fixture/toolchain/Init.olean" for name in imported],
        }

    def inspect(self, tasks, response, **kwargs):
        def run(root, command, **unused):
            data, code = response(command)
            return subprocess.CompletedProcess(command, code, json.dumps(data), "")
        with patch.object(contract.bump_native, "executable", return_value=self.executable), \
             patch.object(contract.bump_jobs, "run", side_effect=run) as calls:
            result = contract.inspect_environment(self.root, tasks, layout=self.layout, **kwargs)
        return result, calls.call_args_list

    def test_sibling_environments_never_imported_together_or_inventory_flattened(self):
        tasks = [{"lean_file": "A.lean", "lean_decl": "newA"},
                 {"lean_file": "B.lean", "lean_decl": "newB"}]
        def response(command):
            module = command[3]
            self.assertEqual(command[4], "--")
            self.assertIn("--owned", command)
            return self.report(module, target="new" + module), 0
        result, calls = self.inspect(tasks, response)
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(result["contexts"]), {"A", "B"})
        self.assertNotIn("project_records", result)
        for module in ("A", "B"):
            self.assertEqual(result["contexts"][module]["project_records"]["overlapping"]["value"], module)
        self.assertEqual({row["context"] for row in result["project_declarations"]}, {"A", "B"})
        self.assertEqual(set(result["targets"]), {"newA", "newB"})

    def test_import_failure_cause_precedes_missing_success_provenance(self):
        response = lambda command: ({"targets": {}, "issues": [
            "kernel contract extraction failed: declaration already declared 'sameName'"]}, 1)
        with self.assertRaisesRegex(contract.ContractInspectionError, "already declared 'sameName'"):
            self.inspect([], response, module_context=["A"], _inventory_only=True)

    def test_transitive_owned_import_allowed_without_importing_unrelated_siblings(self):
        result, calls = self.inspect([{"lean_file": "A.lean", "lean_decl": "newA"}],
            lambda command: (self.report("A", target="newA", imported=["Init", "B", "A"]), 0))
        self.assertEqual(calls[0].args[1][3:5], ["A", "--"])
        self.assertEqual(result["contexts"]["A"]["imported_modules"], ["Init", "B", "A"])

    def test_missing_evidence_is_rejected_after_all_actual_contexts_checked(self):
        with self.assertRaisesRegex(contract.ContractInspectionError, "not found in any actual output context"):
            self.inspect([{"lean_file": "A.lean", "lean_decl": "newA"}],
                lambda command: (self.report("A", target="newA"), 0),
                prerequisite_declarations=["missing"])

    def test_affected_context_without_output_is_still_in_compiled_evidence(self):
        self.layout["verification_modules"]["B.lean"] = "B"
        def response(command):
            module = command[3]
            data = self.report(module, target="newA" if module == "A" else None)
            if module == "B":
                self.assertIn("--inventory-only", command)
                data["project_records"]["overlapping"] = {
                    "name": "overlapping", "module": "B", "target_kind": "def",
                    "type": ["sort", 0], "level_params": [], "is_internal_detail": False,
                    "direct_dependencies": [], "declaration_meaning": {}, "proof_body": [],
                }
            return data, 0
        result, calls = self.inspect([{"lean_file": "A.lean", "lean_decl": "newA"}], response)
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(result["contexts"]), {"A", "B"})
        self.assertIn(str(self.root / ".lake/build/lib/lean/B.olean"), result["compiled_modules"])

    def test_requested_context_must_be_one_owned_module(self):
        for contexts in ([], ["A", "B"], ["Unowned"]):
            with self.subTest(contexts=contexts), self.assertRaisesRegex(ValueError, "one actual"):
                contract.inspect_environment(self.root, [], layout=self.layout,
                    module_context=contexts, _inventory_only=True)

    def test_changes_build_uses_normal_defaults_and_only_changed_or_affected_modules(self):
        with patch.object(contract.bump_jobs, "run", return_value=
                          subprocess.CompletedProcess([], 0, "ok", "")) as run:
            checked = contract.build_sources(self.root, full=True, layout=self.layout)
        self.assertEqual(checked["returncode"], 0)
        self.assertEqual([call.args[1] for call in run.call_args_list], [
            ["lake", "build"], ["lake", "--rehash", "build", "+A"]])

    def test_pristine_baseline_only_runs_normal_default_build(self):
        layout = {**self.layout, "verification_modules": {}}
        with patch.object(contract.bump_jobs, "run", return_value=
                          subprocess.CompletedProcess([], 0, "ok", "")) as run:
            contract.build_sources(self.root, full=True, layout=layout)
        self.assertEqual([call.args[1] for call in run.call_args_list], [["lake", "build"]])

    def test_coverage_names_byte_only_modules_without_semantic_claim(self):
        baseline = {"project_scope": "changes", "sha256": "baseline"}
        with patch.object(contract, "workspace_layout", return_value=self.layout), \
             patch.object(contract, "scoped_layout", return_value=self.layout):
            coverage = contract.project_verification(self.root, baseline)
        self.assertEqual(coverage["contexts"], ["A"])
        self.assertEqual(coverage["byte_only_modules"], {"B.lean": "B", "Broken.lean": "Broken"})
        self.assertEqual(coverage["inspection_policy"], 2)

    def test_changed_compiled_context_disables_build_reuse_even_with_identical_source(self):
        identity = {"main_sha": "head", "source_sha256": "same-source", "environment": {}}
        candidate = {"build": {"returncode": 0}, "verification": {
            "status": "passed", "policy_sha256": "policy", "source_identity": identity,
            "compiled_receipt": {"artifact_id": "all-contexts", "sha256": "hash"}}}
        active = {"project_baseline": {"version": 2, "project_scope": "changes"}}
        with patch.object(contract, "policy_hash", return_value="policy"), \
             patch.object(contract.bump_cache, "compiled_receipt_current", return_value=False) as check:
            self.assertFalse(contract._candidate_build_reusable(self.root, active, candidate, identity))
            check.assert_called_once_with(self.root, candidate["verification"]["compiled_receipt"])
        with patch.object(contract, "policy_hash", return_value="policy"), \
             patch.object(contract.bump_cache, "compiled_receipt_current", return_value=True):
            self.assertTrue(contract._candidate_build_reusable(self.root, active, candidate, identity))

    def test_explicit_legacy_build_reuse_semantics_are_unchanged(self):
        identity = {"main_sha": "head", "source_sha256": "source", "environment": {}}
        candidate = {"build": {"returncode": 0}, "verification": {
            "status": "passed", "policy_sha256": "policy", "source_identity": identity}}
        with patch.object(contract, "policy_hash", return_value="policy"), \
             patch.object(contract.bump_cache, "compiled_receipt_current") as check:
            self.assertTrue(contract._candidate_build_reusable(self.root,
                {"project_baseline": {"version": 1}}, candidate, identity))
            check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
