"""Existing-project policy: real temporary Git, mocked native Lean receipts only."""

import copy
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import formalize_contract as contract, formalize_project as project


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=True).stdout.strip()


def record(name, kind="theorem", module="Main", *, hole=False):
    meaning = {"name": name, "module": module, "kind": kind,
               "type": ["const", "Nat" if kind == "def" else "True"], "level_params": []}
    body = ["const", "sorryAx" if hole else "True.intro"] if kind != "axiom" else None
    if kind == "def":
        meaning.update(value=body, safety="safe", hints=["regular", 1])
    return {"name": name, "target_kind": kind, "module": module,
            "type": meaning["type"], "level_params": [],
            "is_internal_detail": False, "direct_dependencies": [],
            "declaration_meaning": meaning, "proof_body": body}


def inventory(records, *, sorries=(), axioms=(), used=()):
    return {"project_records": copy.deepcopy(records),
            "project_declarations": [{"name": name, "module": row["module"],
                                      "kind": row["target_kind"]} for name, row in records.items()],
            "project_sorries": list(sorries), "project_axioms": list(axioms),
            "project_used_axioms": list(used)}


class ExistingProjectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "unity@example.test")
        git(self.root, "config", "user.name", "Unity Test")
        for name, text in {".gitignore": ".unity/\n.lake/\n.worktrees/\n",
                           "Main.lean": "theorem target : True := by sorry\n",
                           "Other.lean": "axiom unrelated : True\n",
                           "lean-toolchain": "leanprover/lean4:test\n",
                           "lakefile.toml": 'name = "fixture"\n',
                           "lake-manifest.json": '{"packages": []}\n',
                           "README.md": "existing context\n"}.items():
            (self.root / name).write_text(text)
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "existing project")
        self.head = git(self.root, "rev-parse", "HEAD")
        self.records = {"target": record("target", hole=True),
                        "stable": record("stable", kind="def"),
                        "other_proof": record("other_proof"),
                        "unrelated": record("unrelated", kind="axiom", module="Other")}
        self.receipt = inventory(self.records, sorries=["target"], axioms=["unrelated"],
                                 used=["sorryAx", "unrelated"])
        self.layout = {"modules": {"Main.lean": "Main", "Other.lean": "Other"},
                       "build_dir": ".lake/build", "traces": {}, "source_roots": ["."]}
        self.env = {"lean_version": "Lean fixture", "config": {},
                    "dependencies": {"Mathlib": {"path": "/fixture/mathlib", "sources": "fixed"}}}
        for name, replacement in {
            "workspace_layout": lambda root: copy.deepcopy(self.layout),
            "environment_identity": lambda root: self.environment(),
            "_dependencies": lambda root: copy.deepcopy(self.env["dependencies"]),
            "build_sources": lambda *a, **k: {"returncode": 0, "output": "ok"},
            "inspect_environment": lambda *a, **k: copy.deepcopy(self.receipt),
        }.items():
            mock = patch.object(contract, name, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def environment(self):
        result = copy.deepcopy(self.env)
        result["config"] = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                            for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json")
                            if (self.root / name).exists()}
        return result

    def capture(self, targets="target"):
        return project.capture_baseline(self.root, targets)

    def solve_target(self, name="target"):
        self.receipt["project_sorries"] = [n for n in self.receipt["project_sorries"] if n != name]
        self.receipt["project_axioms"] = [n for n in self.receipt["project_axioms"] if n != name]
        row = self.receipt["project_records"][name]
        row["proof_body"] = ["const", "True.intro"]
        if row["target_kind"] == "axiom":
            row["target_kind"] = "theorem"
            row["declaration_meaning"]["kind"] = "theorem"
            next(r for r in self.receipt["project_declarations"] if r["name"] == name)["kind"] = "theorem"

    def test_capture_is_immutable_json_snapshot_without_git_changes(self):
        baseline = self.capture()
        self.assertTrue(project.baseline_is_valid(baseline))
        self.assertEqual(baseline["head"], self.head)
        self.assertEqual(baseline["scope"]["existing_targets"], ["target"])
        self.receipt["project_records"]["target"]["type"] = []
        self.assertNotEqual(baseline["declarations"]["target"]["type"], [])
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.head)

    def test_dirty_tracked_input_stops_before_build(self):
        (self.root / "Main.lean").write_text("user in-progress proof")
        with patch.object(contract, "build_sources") as build:
            with self.assertRaisesRegex(ValueError, "clean tracked/untracked"):
                self.capture()
            build.assert_not_called()
        self.assertEqual((self.root / "Main.lean").read_text(), "user in-progress proof")

    def test_untracked_user_file_is_not_discarded(self):
        (self.root / "draft.txt").write_text("keep me")
        with self.assertRaisesRegex(ValueError, "draft.txt"):
            self.capture()
        self.assertEqual((self.root / "draft.txt").read_text(), "keep me")

    def test_generated_runtime_state_is_not_dirty_user_work(self):
        (self.root / ".unity").mkdir()
        (self.root / ".unity/log.txt").write_text("runtime")
        self.assertTrue(project.baseline_is_valid(self.capture()))

    def test_ignored_user_input_blocks_before_dispatch(self):
        with (self.root / ".gitignore").open("a") as handle:
            handle.write("private-data.txt\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-qm", "user ignore")
        (self.root / "private-data.txt").write_text("retain this input")
        with self.assertRaisesRegex(ValueError, "cannot be preserved in worktrees"):
            self.capture()
        self.assertEqual((self.root / "private-data.txt").read_text(), "retain this input")

    def test_staged_rename_is_detected(self):
        git(self.root, "mv", "README.md", "context with spaces.md")
        with self.assertRaisesRegex(ValueError, "context with spaces"):
            self.capture()

    def test_failed_existing_build_is_blocked(self):
        with patch.object(contract, "build_sources", return_value={"returncode": 1, "output": "bad build"}):
            with self.assertRaisesRegex(ValueError, "must build.*bad build"):
                self.capture()

    def test_capture_detects_input_change_during_inspection(self):
        def inspect(*args, **kwargs):
            (self.root / "README.md").write_text("raced")
            return self.receipt
        with patch.object(contract, "inspect_environment", inspect):
            with self.assertRaisesRegex(ValueError, "clean tracked/untracked|changed while"):
                self.capture()

    def test_capture_requires_complete_native_inventory(self):
        self.receipt["project_records"].pop("unrelated")
        with self.assertRaisesRegex(ValueError, "entire project"):
            self.capture()

    def test_native_missing_meaning_or_proof_body_fails_closed(self):
        original = copy.deepcopy(self.receipt)
        for field, value in (("declaration_meaning", {}), ("proof_body", None)):
            self.receipt = copy.deepcopy(original)
            self.receipt["project_records"]["target"][field] = value
            with self.assertRaisesRegex(ValueError, "meaning/body evidence"):
                self.capture()

    def test_duplicate_or_mismatched_native_inventory_is_rejected(self):
        self.receipt["project_declarations"].append(copy.deepcopy(self.receipt["project_declarations"][0]))
        with self.assertRaisesRegex(ValueError, "entire project"):
            self.capture()
        self.receipt["project_declarations"].pop()
        self.receipt["project_declarations"][0]["module"] = "Other"
        with self.assertRaisesRegex(ValueError, "incomplete native"):
            self.capture()

    def test_all_selects_holes_not_completed_definitions(self):
        self.assertEqual(self.capture("All")["scope"]["existing_targets"], ["target", "unrelated"])

    def test_generated_hole_selection_fails_closed_but_unrelated_scope_remains_available(self):
        auxiliary = record("container._proof_1", hole=True)
        auxiliary["is_internal_detail"] = True
        self.records[auxiliary["name"]] = auxiliary
        self.records["container"] = record("container", "def")
        self.records["container"]["direct_dependencies"] = [auxiliary["name"]]
        self.receipt = inventory(self.records, sorries=["target", auxiliary["name"]],
                                 axioms=["unrelated"], used=["sorryAx", "unrelated"])
        for selection in ("All", auxiliary["name"], "container"):
            with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, "editable-owner"):
                self.capture(selection)
        self.assertEqual(self.capture("target")["scope"]["existing_targets"], ["target"])
        natural = self.capture("fill the requested pending theorem")
        with self.assertRaisesRegex(ValueError, "editable-owner"):
            project.bind_scope(natural, {"existing_targets": [auxiliary["name"]], "chunks": []})

    def test_explicit_unknown_name_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unknown explicit"):
            self.capture("target, typo")

    def test_scope_binding_cannot_drop_selected_target(self):
        with self.assertRaisesRegex(ValueError, "omits or changes selected"):
            project.bind_scope(self.capture("target, unrelated"), {"existing_targets": ["target"], "chunks": []})

    def test_scope_binding_cannot_claim_protected_declaration(self):
        with self.assertRaisesRegex(ValueError, "protected existing"):
            project.bind_scope(self.capture(), {"existing_targets": ["target"], "chunks": [{"lean_decl": "target"}, {"lean_decl": "stable"}]})

    def test_natural_scope_requires_explicit_selection_then_freezes(self):
        baseline = self.capture("finish the requested main theorem")
        self.assertFalse(baseline["scope"]["bound"])
        dag = {"chunks": [{"lean_decl": "target"}]}
        with self.assertRaisesRegex(ValueError, "requires existing_targets"):
            project.bind_scope(baseline, dag)
        bound = project.bind_scope(baseline, {**dag, "existing_targets": ["target"]})
        self.assertTrue(bound["scope"]["bound"])
        self.assertEqual(bound["origin_sha256"], baseline["sha256"])
        self.assertFalse(baseline["scope"]["bound"])
        with self.assertRaisesRegex(ValueError, "omits or changes selected"):
            project.bind_scope(bound, {"chunks": [], "existing_targets": []})

    def test_informal_plan_binds_without_premature_output_manifest(self):
        baseline = self.capture()
        bound = project.bind_scope(baseline, {"existing_targets": ["target"],
                                            "chunks": [{"id": "T1", "informal_statement": "Finish target"}]})
        self.assertEqual(bound["scope"]["existing_targets"], ["target"])

    def test_natural_scope_cannot_unlock_completed_definition(self):
        baseline = self.capture("finish the requested main theorem")
        with self.assertRaisesRegex(ValueError, "only select existing incomplete"):
            project.bind_scope(baseline, {"chunks": [{"lean_decl": "stable"}], "existing_targets": ["stable"]})

    def test_checksum_tampering_rejected_without_native_inspection(self):
        baseline = self.capture()
        baseline["scope"]["existing_targets"].append("stable")
        with patch.object(contract, "inspect_environment") as inspect:
            self.assertIn("integrity mismatch", project.validate_baseline(self.root, baseline)[0])
            inspect.assert_not_called()

    def test_malformed_resealed_scope_is_invalid_without_exception(self):
        baseline = self.capture()
        baseline["scope"]["existing_targets"] = [{}]
        malformed = project._seal(baseline)
        self.assertFalse(project.baseline_is_valid(malformed))

    def test_target_proof_completion_preserves_unrelated_axiom(self):
        baseline = self.capture()
        self.solve_target()
        (self.root / "Main.lean").write_text("theorem target : True := by trivial\n")
        self.assertEqual(project.validate_baseline(self.root, baseline, final=True), [])

    def test_target_signature_changes_are_blocked(self):
        baseline = self.capture()
        self.receipt["project_records"]["target"]["type"] = ["const", "False"]
        self.receipt["project_records"]["target"]["declaration_meaning"]["type"] = ["const", "False"]
        self.assertTrue(any("signature" in e for e in project.validate_baseline(self.root, baseline)))

    def test_unrelated_definition_changed_inside_writable_file_is_blocked(self):
        baseline = self.capture()
        self.receipt["project_records"]["stable"]["declaration_meaning"]["value"] = ["nat", 9]
        self.receipt["project_records"]["stable"]["proof_body"] = ["nat", 9]
        self.assertTrue(any("protected existing declaration changed: stable" in e
                            for e in project.validate_baseline(self.root, baseline)))

    def test_unrelated_theorem_proof_change_is_blocked(self):
        baseline = self.capture()
        self.receipt["project_records"]["other_proof"]["proof_body"] = ["const", "alternative"]
        self.assertTrue(any("other_proof" in e for e in project.validate_baseline(self.root, baseline)))

    def test_explicit_completed_definition_body_stays_protected(self):
        baseline = self.capture("stable")
        self.receipt["project_records"]["stable"]["declaration_meaning"]["value"] = ["nat", 9]
        self.receipt["project_records"]["stable"]["proof_body"] = ["nat", 9]
        self.assertTrue(any("completed existing definition" in e for e in project.validate_baseline(self.root, baseline)))

    def test_explicit_completed_theorem_is_verify_only_and_body_stays_protected(self):
        baseline = self.capture("other_proof")
        self.assertEqual(project.validate_baseline(self.root, baseline, final=True), [])
        self.receipt["project_records"]["other_proof"]["proof_body"] = ["const", "alternative"]
        self.assertTrue(any("completed existing declaration changed: other_proof" in e
                            for e in project.validate_baseline(self.root, baseline, final=True)))

    def test_selected_partial_definition_body_can_be_filled_but_metadata_cannot(self):
        self.records["partial"] = record("partial", "def", hole=True)
        self.receipt = inventory(self.records, sorries=["target", "partial"], axioms=["unrelated"],
                                 used=["sorryAx", "unrelated"])
        baseline = self.capture("partial")
        row = self.receipt["project_records"]["partial"]
        row["proof_body"] = ["nat", 1]
        row["declaration_meaning"]["value"] = ["nat", 1]
        self.receipt["project_sorries"].remove("partial")
        self.assertEqual(project.validate_baseline(self.root, baseline, final=True), [])
        row["declaration_meaning"]["safety"] = "unsafe"
        self.assertTrue(any("metadata changed" in e for e in project.validate_baseline(self.root, baseline)))

    def test_selected_axiom_can_become_theorem(self):
        baseline = self.capture("unrelated")
        self.solve_target("unrelated")
        self.assertEqual(project.validate_baseline(self.root, baseline, final=True), [])

    def test_selected_axiom_cannot_become_definition(self):
        baseline = self.capture("unrelated")
        self.receipt["project_records"]["unrelated"]["target_kind"] = "def"
        self.receipt["project_records"]["unrelated"]["declaration_meaning"]["kind"] = "def"
        self.receipt["project_records"]["unrelated"]["declaration_meaning"]["value"] = ["const", "True.intro"]
        self.receipt["project_records"]["unrelated"]["proof_body"] = ["const", "True.intro"]
        next(row for row in self.receipt["project_declarations"] if row["name"] == "unrelated")["kind"] = "def"
        self.assertTrue(any("kind changed" in e for e in project.validate_baseline(self.root, baseline)))

    def test_preexisting_out_of_scope_sorry_can_remain(self):
        self.records["other_hole"] = record("other_hole", hole=True, module="Other")
        self.receipt = inventory(self.records, sorries=["target", "other_hole"], axioms=["unrelated"],
                                 used=["sorryAx", "unrelated"])
        baseline = self.capture()
        self.solve_target()
        self.assertEqual(project.validate_baseline(self.root, baseline, final=True), [])

    def test_new_holes_require_manifest_and_are_never_final(self):
        baseline = self.capture()
        self.receipt["project_records"]["helper"] = record("helper", hole=True)
        self.receipt["project_declarations"].append({"name": "helper", "module": "Main", "kind": "theorem"})
        self.receipt["project_sorries"].append("helper")
        self.assertTrue(any("out-of-scope" in e for e in project.validate_baseline(self.root, baseline)))
        self.assertEqual(project.validate_baseline(self.root, baseline, allowed_incomplete_declarations=["helper"]), [])
        self.solve_target()
        self.assertTrue(any("out-of-scope" in e for e in project.validate_baseline(
            self.root, baseline, final=True, allowed_incomplete_declarations=["helper"])))

    def test_new_axiom_or_native_dependency_is_rejected(self):
        baseline = self.capture()
        self.receipt["project_axioms"].append("new_axiom")
        self.receipt["project_used_axioms"].append("helper._native.decide.ax_1")
        errors = project.validate_baseline(self.root, baseline)
        self.assertTrue(any("new project axioms" in e for e in errors))
        self.assertTrue(any("new forbidden axiom dependencies" in e for e in errors))

    def test_toolchain_dependency_and_config_drift_are_rejected(self):
        baseline = self.capture()
        (self.root / "lean-toolchain").write_text("different")
        self.env["dependencies"]["Mathlib"]["sources"] = "modified"
        self.assertTrue(any("toolchain" in e for e in project.validate_baseline(self.root, baseline)))

    def test_pinned_config_guard_runs_before_any_lake_helper(self):
        baseline = self.capture()
        (self.root / "lakefile.toml").write_text('name = "changed"\n')
        with patch.object(contract, "workspace_layout") as layout, \
                patch.object(contract, "environment_identity") as environment, \
                patch.object(contract, "inspect_environment") as inspect, \
                patch.object(contract, "_dependencies") as dependencies:
            with self.assertRaisesRegex(ValueError, "configuration changed before Lake"):
                project.require_pinned_inputs(self.root, baseline)
            self.assertTrue(project.validate_baseline(self.root, baseline))
            layout.assert_not_called()
            environment.assert_not_called()
            inspect.assert_not_called()
            dependencies.assert_not_called()

    def test_pinned_dependency_guard_is_read_only_and_blocks_before_lake(self):
        baseline = self.capture()
        self.env["dependencies"]["Mathlib"]["sources"] = "changed"
        with patch.object(contract, "workspace_layout") as layout, \
                patch.object(contract, "environment_identity") as environment, \
                patch.object(contract, "inspect_environment") as inspect:
            with self.assertRaisesRegex(ValueError, "dependency source bytes changed"):
                project.require_pinned_inputs(self.root, baseline)
            self.assertTrue(project.validate_baseline(self.root, baseline))
            layout.assert_not_called()
            environment.assert_not_called()
            inspect.assert_not_called()

    def test_new_lake_configuration_and_symlink_rejected_before_execution(self):
        baseline = self.capture()
        (self.root / "lakefile.lean").write_text("import Lake\n")
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            project.require_pinned_inputs(self.root, baseline)
        (self.root / "lakefile.lean").unlink()
        original = self.root / "lakefile.toml"
        saved = self.root / "original.toml"
        original.rename(saved)
        original.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, "symlink"):
            project.require_pinned_inputs(self.root, baseline)

    def test_nontarget_module_guard_precedes_lake_but_target_body_can_change(self):
        baseline = self.capture()
        (self.root / "Main.lean").write_text("theorem target : True := by trivial\n")
        project.require_pinned_inputs(self.root, baseline)
        (self.root / "Other.lean").write_text("-- unauthorized context change\n")
        with self.assertRaisesRegex(ValueError, "pinned project input changed before Lake.*Other.lean"):
            project.require_pinned_inputs(self.root, baseline)

    def test_writable_target_symlink_is_never_allowed(self):
        baseline = self.capture()
        (self.root / "Main.lean").unlink()
        (self.root / "Main.lean").symlink_to(self.root / "Other.lean")
        with self.assertRaisesRegex(ValueError, "symlink"):
            project.require_pinned_inputs(self.root, baseline)

    def test_unrelated_source_and_context_bytes_are_protected(self):
        baseline = self.capture()
        for name in ("Other.lean", "README.md"):
            path = self.root / name
            original = path.read_text()
            path.write_text("changed user input")
            errors = project.validate_baseline(self.root, baseline)
            self.assertTrue(any("pinned project input changed before Lake execution: " + name in e for e in errors))
            path.write_text(original)

    def test_new_support_file_requires_controller_authorization(self):
        baseline = self.capture()
        (self.root / "Support.lean").write_text("-- helper")
        self.layout["modules"]["Support.lean"] = "Support"
        self.assertTrue(any("unapproved new project input" in e for e in project.validate_baseline(self.root, baseline)))
        self.assertEqual(project.validate_baseline(self.root, baseline, allowed_new_paths=["Support.lean"]), [])
        self.assertTrue(any("invalid" in e for e in project.validate_baseline(
            self.root, baseline, allowed_new_paths=["../outside.lean"])))

    def test_new_helper_in_existing_file_keeps_original_declarations_protected(self):
        baseline = self.capture()
        (self.root / "Other.lean").write_text("axiom unrelated : True\ntheorem helper : True := by trivial\n")
        self.receipt["project_records"]["helper"] = record("helper", module="Other")
        self.receipt["project_declarations"].append({"name": "helper", "module": "Other", "kind": "theorem"})
        self.assertEqual(project.validate_baseline(self.root, baseline, allowed_new_paths=["Other.lean"]), [])
        self.receipt["project_records"]["unrelated"]["type"] = ["const", "False"]
        self.receipt["project_records"]["unrelated"]["declaration_meaning"]["type"] = ["const", "False"]
        self.assertTrue(any("protected existing declaration" in error for error in
                            project.validate_baseline(self.root, baseline, allowed_new_paths=["Other.lean"])))

    def test_module_relocation_and_removed_declaration_are_rejected(self):
        baseline = self.capture()
        self.layout["modules"]["Main.lean"] = "Changed"
        del self.receipt["project_records"]["stable"]
        self.receipt["project_declarations"] = [r for r in self.receipt["project_declarations"] if r["name"] != "stable"]
        errors = project.validate_baseline(self.root, baseline)
        self.assertTrue(any("module ownership" in e for e in errors))
        self.assertTrue(any("declaration removed: stable" in e for e in errors))

    def test_inspection_failure_blocks_instead_of_accepting_old_receipt(self):
        baseline = self.capture()
        with patch.object(contract, "inspect_environment", side_effect=ValueError("native failed")):
            self.assertTrue(any("native failed" in e for e in project.validate_baseline(self.root, baseline)))

    def test_main_branch_change_is_rejected_even_at_same_head(self):
        baseline = self.capture()
        git(self.root, "switch", "-c", "elsewhere")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), baseline["head"])
        self.assertTrue(any("branch changed" in e for e in project.validate_baseline(self.root, baseline)))
        with self.assertRaisesRegex(ValueError, "branch changed"):
            project.require_original_branch(self.root, baseline)

    def test_private_candidate_worktree_may_use_its_own_branch(self):
        baseline = self.capture()
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        candidate = Path(parent.name) / "candidate"
        git(self.root, "worktree", "add", "-b", "worker/Ada", str(candidate))
        self.assertEqual(project.validate_baseline(candidate, baseline), [])

    def test_incomplete_selected_target_cannot_pass_final(self):
        self.assertTrue(any("remain incomplete" in e for e in project.validate_baseline(
            self.root, self.capture(), final=True)))


if __name__ == "__main__":
    unittest.main()
