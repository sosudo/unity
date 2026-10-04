"""Offline change-focused preservation tests; no providers or evaluations."""
import copy
import unittest
from unittest.mock import patch

from tests.test_bump_project import ExistingProjectTests, inventory, record
from unity import bump_contract as contract, bump_delta as delta, bump_project as project


class DeltaProjectTests(ExistingProjectTests):
    # Reuse only the Git fixture; legacy tests stay in their original class.
    def capture(self, targets="Add a new theorem about the supplied source."):
        self.layout["default_modules"] = {"Main.lean": "Main"}
        self.layout["unknown_default_targets"] = []
        with patch("unity.bump_scope._headers", side_effect=lambda root, paths: {p: [] for p in paths}):
            return project.capture_baseline(self.root, targets, project_scope="changes")

    def test_lightweight_capture_never_requests_native_inventory(self):
        with patch.object(contract, "inspect_environment", side_effect=AssertionError("joint import")), \
             patch.object(contract, "build_sources", return_value={"returncode": 0, "output": "ok"}) as build:
            baseline = self.capture()
        self.assertEqual(baseline["version"], 2)
        self.assertEqual(baseline["declarations"], {})
        self.assertEqual(baseline["original_contexts"], {})
        self.assertTrue(project.baseline_is_valid(baseline))
        self.assertTrue(build.call_args.kwargs["full"])
        self.assertEqual(build.call_args.kwargs["layout"]["verification_modules"], {})

    def test_explicit_all_is_not_silently_a_subset(self):
        with self.assertRaisesRegex(ValueError, "bounded targets"):
            self.capture("All")

    def test_natural_empty_existing_targets_bind_without_imports(self):
        baseline = self.capture()
        with patch.object(contract, "inspect_environment", side_effect=AssertionError("inspection")):
            bound = project.bind_scope(baseline, {"existing_targets": [], "chunks": []}, root=self.root)
        self.assertTrue(bound["scope"]["bound"])
        self.assertTrue(delta.baseline_matches(baseline, bound))
        changed = project._seal({**bound, "head": "a" * 40})
        self.assertFalse(delta.baseline_matches(baseline, changed))

    def test_append_is_allowed_but_existing_commands_are_protected(self):
        baseline = self.capture()
        original = (self.root / "Main.lean").read_text()
        (self.root / "Main.lean").write_text(original + "\ntheorem addition : True := by trivial\n")
        project.require_pinned_inputs(self.root, baseline, allowed_new_paths={"Main.lean"})
        (self.root / "Main.lean").write_text("theorem target : False := by sorry\n")
        with self.assertRaisesRegex(ValueError, "protected existing source command"):
            project.require_pinned_inputs(self.root, baseline, allowed_new_paths={"Main.lean"})

    def test_new_file_cannot_change_attributes_on_existing_declarations(self):
        baseline = self.capture()
        (self.root / "New.lean").write_text("import Main\nattribute [simp] target\n")
        with self.assertRaisesRegex(ValueError, "environment-changing"):
            project.require_pinned_inputs(self.root, baseline, allowed_new_paths={"New.lean"})

    def test_wrapped_attribute_command_is_not_an_append_loophole(self):
        baseline = self.capture()
        original = (self.root / "Main.lean").read_text()
        (self.root / "Main.lean").write_text(original + "\nopen Nat in attribute [simp] target\n")
        with self.assertRaisesRegex(ValueError, "protected environment"):
            project.require_pinned_inputs(self.root, baseline, allowed_new_paths={"Main.lean"})

    def test_unapproved_new_file_rejected_before_native_execution(self):
        baseline = self.capture()
        (self.root / "injected.txt").write_text("new payload")
        with self.assertRaisesRegex(ValueError, "unapproved new project input before Lake"):
            project.require_pinned_inputs(self.root, baseline)

    def test_changed_dependency_is_rejected_before_lazy_original_inspection(self):
        baseline = self.capture()
        self.env["dependencies"]["Mathlib"]["sources"] = "changed"
        with patch.object(contract, "inspect_environment") as inspect:
            with self.assertRaisesRegex(ValueError, "pinned dependencies"):
                delta.original_context(self.root, baseline, "Main")
            inspect.assert_not_called()

    def test_selected_hole_body_edit_not_statement_or_neighbor(self):
        baseline = self.capture("target")
        original = inventory({"target": record("target", hole=True)}, sorries=["target"], used=["sorryAx"])
        receipt = project._seal({**delta._receipt_identity(baseline, "Main"), "inspection": original})
        dag = {"existing_targets": ["target"], "chunks": [{"lean_decl": "target", "lean_file": "Main.lean"}]}
        with patch.object(delta, "original_context", return_value=receipt):
            bound = project.bind_scope(baseline, dag, root=self.root)
        self.assertTrue(project.baseline_is_valid(bound))
        (self.root / "Main.lean").write_text("theorem target : True := by trivial\n")
        project.require_pinned_inputs(self.root, bound)
        (self.root / "Main.lean").write_text("theorem target : False := by trivial\n")
        with self.assertRaisesRegex(ValueError, "protected existing source command"):
            project.require_pinned_inputs(self.root, bound)

    def test_selected_target_requires_exact_original_source_path(self):
        baseline = self.capture("target")
        with self.assertRaisesRegex(ValueError, "exact original lean_file"):
            project.bind_scope(baseline, {"existing_targets": ["target"], "chunks": []}, root=self.root)

    def test_native_context_receipt_cannot_cross_module_identity(self):
        baseline = self.capture()
        receipt = project._seal({**delta._receipt_identity(baseline, "Main"),
                                "inspection": inventory({"other": record("other", module="Other")})})
        self.assertFalse(delta._receipt_valid(receipt, baseline, "Main"))

    def test_receipt_cannot_survive_native_policy_change(self):
        baseline = self.capture()
        receipt = project._seal({**delta._receipt_identity(baseline, "Main"),
                                "inspection": inventory({"target": record("target", hole=True)}, sorries=["target"])})
        self.assertTrue(delta._receipt_valid(receipt, baseline, "Main"))
        with patch.object(contract, "policy_hash", return_value="changed-policy"):
            self.assertFalse(delta._receipt_valid(receipt, baseline, "Main"))

    def test_selected_body_cannot_smuggle_attribute_command(self):
        baseline = self.capture("target")
        original = inventory({"target": record("target", hole=True)}, sorries=["target"])
        receipt = project._seal({**delta._receipt_identity(baseline, "Main"), "inspection": original})
        with patch.object(delta, "original_context", return_value=receipt):
            bound = project.bind_scope(baseline, {"existing_targets": ["target"],
                "chunks": [{"lean_decl": "target", "lean_file": "Main.lean"}]}, root=self.root)
        (self.root / "Main.lean").write_text("theorem target : True := by trivial\nattribute [simp] target\n")
        with self.assertRaisesRegex(ValueError, "environment-changing|protected environment"):
            project.require_pinned_inputs(self.root, bound)

    def test_optional_downstream_is_not_invented_as_a_new_default_target(self):
        baseline = self.capture()
        layout = copy.deepcopy(self.layout)
        layout["modules"]["Broken.lean"] = "Broken"
        baseline["layout"]["modules"]["Broken.lean"] = "Broken"
        baseline["import_headers"]["Broken.lean"] = ["Main"]
        baseline["files"]["Broken.lean"] = "fixed"
        (self.root / "Main.lean").write_text("theorem target : True := by sorry\n\ntheorem added : True := by trivial\n")
        hashes = {**baseline["files"], "Main.lean": "changed"}
        with patch.object(contract, "_file_hashes", return_value=hashes), \
             patch("unity.bump_scope._headers", return_value={"Main.lean": [], "Other.lean": [], "Broken.lean": ["Main"]}):
            modules = delta.verification_modules(self.root, baseline, layout)
        self.assertEqual(modules, {"Main.lean": "Main"})

    def test_selected_contexts_allow_unrelated_duplicate_names(self):
        baseline = self.capture("one, two")
        contexts = {
            "Main": inventory({"one": record("one", hole=True), "duplicate": record("duplicate")}, sorries=["one"]),
            "Other": inventory({"two": record("two", module="Other", hole=True),
                                "duplicate": record("duplicate", module="Other")}, sorries=["two"]),
        }
        def original(root, baseline, module):
            return project._seal({**delta._receipt_identity(baseline, module), "inspection": contexts[module]})
        with patch.object(delta, "original_context", side_effect=original):
            bound = project.bind_scope(baseline, {"existing_targets": ["one", "two"], "chunks": [
                {"lean_decl": "one", "lean_file": "Main.lean"},
                {"lean_decl": "two", "lean_file": "Other.lean"}]}, root=self.root)
        self.assertTrue(project.baseline_is_valid(bound), project.baseline_errors(bound))
        self.assertEqual(set(bound["declarations"]), {"one", "two"})

    def test_used_optional_downstream_enters_preservation_boundary(self):
        baseline = self.capture()
        layout = copy.deepcopy(self.layout)
        layout["modules"].update({"Used.lean": "Used", "New.lean": "New"})
        baseline["layout"]["modules"]["Used.lean"] = "Used"
        baseline["import_headers"]["Used.lean"] = ["Main"]
        baseline["files"]["Used.lean"] = "fixed"
        hashes = {**baseline["files"], "Main.lean": "changed", "New.lean": "new"}
        with patch.object(contract, "_file_hashes", return_value=hashes), \
             patch("unity.bump_scope._headers", return_value={"Main.lean": [], "Other.lean": [],
                                                                   "Used.lean": ["Main"], "New.lean": ["Used"]}):
            modules = delta.verification_modules(self.root, baseline, layout)
        self.assertEqual(set(modules), {"Main.lean", "Used.lean", "New.lean"})

    def test_origin_binding_cannot_reinterpret_unbound_explicit_scope(self):
        baseline = self.capture("target")
        modified = copy.deepcopy(baseline)
        modified["origin_sha256"] = baseline["sha256"]
        modified["scope"] = {"mode": "natural", "bound": True, "existing_targets": []}
        modified = project._seal(modified)
        self.assertTrue(project.baseline_is_valid(modified))
        self.assertFalse(delta.baseline_matches(baseline, modified))

    def test_new_header_import_preserves_old_commands_for_native_recheck(self):
        baseline = self.capture()
        original = (self.root / "Main.lean").read_text()
        (self.root / "Main.lean").write_text("import NewHelper\n" + original)
        project.require_pinned_inputs(self.root, baseline, allowed_new_paths={"Main.lean"})
        (self.root / "Main.lean").write_text("import NewHelper\nattribute [simp] target\n" + original)
        with self.assertRaisesRegex(ValueError, "protected existing source command"):
            project.require_pinned_inputs(self.root, baseline, allowed_new_paths={"Main.lean"})

    def test_bound_existing_target_refinement_needs_no_output_manifest(self):
        baseline = self.capture("target")
        original = inventory({"target": record("target", hole=True)}, sorries=["target"])
        receipt = project._seal({**delta._receipt_identity(baseline, "Main"), "inspection": original})
        with patch.object(delta, "original_context", return_value=receipt):
            bound = project.bind_scope(baseline, {"existing_targets": ["target"], "chunks": [
                {"lean_decl": "target", "lean_file": "Main.lean"}]}, root=self.root)
        normalized_refinement = {"existing_targets": ["target"], "chunks": [{"id": "refined-obligation"}]}
        with patch.object(delta, "original_context", side_effect=AssertionError("recaptured original context")):
            rebound = project.bind_scope(bound, normalized_refinement)
        self.assertEqual(rebound, bound)
        self.assertIsNot(rebound, bound)
        with self.assertRaisesRegex(ValueError, "omits or changes"):
            project.bind_scope(bound, {"existing_targets": [], "chunks": []})

    def test_bound_refinement_does_not_accept_corrupt_original_receipt(self):
        baseline = self.capture()
        bound = project.bind_scope(baseline, {"existing_targets": [], "chunks": []})
        bound["files"]["Main.lean"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "integrity mismatch"):
            delta.bind_scope(None, bound, {"existing_targets": [], "chunks": []})


# Do not inherit and rerun the legacy policy expectations with a v2 capture.
for _name in vars(ExistingProjectTests):
    if _name.startswith("test_") and _name not in vars(DeltaProjectTests):
        setattr(DeltaProjectTests, _name, None)


if __name__ == "__main__":
    unittest.main()
