"""Frozen library verification boundaries: offline, temporary Git, mocked native metadata."""

import copy
import hashlib
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from unity import bump_contract as contract, bump_files, bump_project as project
from unity import bump_scope as scope, bump_workspace
from test_bump_project import git, inventory, record


class LibraryScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="unity-library-scope-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.layout = {
            "modules": {"Lib/Main.lean": "Lib.Main", "Extra.lean": "Extra",
                        "scripts/Required.lean": "Required", "scripts/Broken.lean": "Broken"},
            "module_owners": {
                "Lib/Main.lean": {"libraries": ["Lib"], "executables": []},
                "Extra.lean": {"libraries": ["Extra"], "executables": []},
                "scripts/Required.lean": {"libraries": [], "executables": ["Required"]},
                "scripts/Broken.lean": {"libraries": [], "executables": ["Broken"]}},
            "libraries": ["Extra", "Lib"], "build_dir": ".lake/build", "traces": {},
            "source_roots": [], "unmatched": ["Scratch.lean"], "issues": [],
        }
        self.imports = {"Lib/Main.lean": ["Required", "Mathlib"], "Extra.lean": ["Init"],
                        "scripts/Required.lean": ["Init"]}
        for path in [*self.layout["modules"], "Scratch.lean"]:
            source = self.root / path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("-- original source " + path + "\n")
        for path, content in {".gitignore": ".unity/\n.lake/\n.worktrees/\n",
                              "lakefile.toml": 'name = "fixture"\n', "lake-manifest.json": '{"packages": []}',
                              "lean-toolchain": "leanprover/lean4:test\n", "README.md": "preserved\n"}.items():
            (self.root / path).write_text(content)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "unity@example.test")
        git(self.root, "config", "user.name", "Unity Test")
        git(self.root, "config", "commit.gpgsign", "false")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fixture")
        self.records = {"target": record("target", module="Lib.Main", hole=True),
                        "stable": record("stable", kind="def", module="Lib.Main"),
                        "extra": record("extra", module="Extra"),
                        "required": record("required", kind="def", module="Required"),
                        "aux_hole": record("aux_hole", module="Required", hole=True)}
        self.inspection = inventory(self.records, sorries=["target", "aux_hole"], used=["sorryAx"])
        self.header_calls = []

        def headers(root, files):
            self.header_calls.append(set(files))
            return {path: copy.deepcopy(self.imports[path]) for path in files}

        replacements = [patch.object(bump_workspace, "read_imports", headers, create=True),
                        patch.object(contract, "workspace_layout", lambda root: copy.deepcopy(self.layout)),
                        patch.object(contract, "environment_identity", lambda root: self.environment()),
                        patch.object(contract, "_dependencies", lambda root: {}),
                        patch.object(contract, "inspect_environment", lambda *a, **k: copy.deepcopy(self.inspection)),
                        patch.object(contract, "build_sources", return_value={"returncode": 0, "output": "ok"})]
        self.build = None
        for replacement in replacements:
            mocked = replacement.start()
            self.addCleanup(replacement.stop)
            self.build = mocked

    def environment(self):
        return {"lean_version": "fixture", "dependencies": {}, "config": {
            path: hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            for path in ("lakefile.toml", "lake-manifest.json", "lean-toolchain")}}

    def capture(self, targets="All"):
        return project.capture_baseline(self.root, targets, project_scope="libraries")

    def policy(self):
        return scope.capture(self.root, self.layout, "libraries")

    def add_library_helper(self):
        path = "Lib/New.lean"
        self.layout["modules"][path] = "Lib.New"
        self.layout["module_owners"][path] = {"libraries": ["Lib"], "executables": []}
        self.imports[path] = ["Lib.Main"]
        (self.root / path).write_text("-- approved new helper\n")
        return path

    def test_capture_includes_nondefault_library_and_required_aux_not_broken_optional(self):
        baseline = self.capture()
        policy = baseline["verification_scope"]
        self.assertEqual(set(policy["editable_modules"]), {"Lib/Main.lean", "Extra.lean"})
        self.assertEqual(set(policy["verification_modules"]), set(self.imports))
        self.assertEqual(baseline["scope"]["existing_targets"], ["target"])
        self.assertIn("aux_hole", baseline["declarations"])
        self.assertTrue(all("scripts/Broken.lean" not in paths for paths in self.header_calls))
        self.assertEqual(self.build.call_args.kwargs["layout"]["project_scope"], "libraries")
        self.assertIn("scripts/Broken.lean", baseline["files"])
        self.assertFalse(project.baseline_errors(baseline))

    def test_readonly_explicit_and_natural_target_selection_rejected(self):
        with self.assertRaisesRegex(ValueError, "read-only auxiliary"):
            self.capture("aux_hole")
        baseline = self.capture("complete the intended library result")
        with self.assertRaisesRegex(ValueError, "read-only auxiliary"):
            project.bind_scope(baseline, {"existing_targets": ["aux_hole"], "chunks": []})
        bound = project.bind_scope(baseline, {"existing_targets": ["target"], "chunks": []})
        self.assertEqual(bound["scope"]["existing_targets"], ["target"])

    def test_required_auxiliary_build_failure_is_fatal(self):
        self.build.return_value = {"returncode": 1, "output": "Required.lean: failed"}
        with self.assertRaisesRegex(ValueError, "Required.lean: failed"):
            self.capture()

    def test_executable_only_empty_libraries_fail_before_build(self):
        self.layout["libraries"] = []
        for row in self.layout["module_owners"].values():
            row.update(libraries=[], executables=["program"])
        with self.assertRaisesRegex(ValueError, "nonempty configured Lean library"):
            self.capture()
        self.build.assert_not_called()

    def test_new_owned_library_helper_is_verified_but_does_not_rewrite_policy(self):
        policy = self.policy()
        previous = copy.deepcopy(policy)
        path = self.add_library_helper()
        applied = scope.apply(self.root, self.layout, policy)
        self.assertIn(path, applied["editable_modules"])
        self.assertIn(path, applied["verification_modules"])
        self.assertEqual(policy, previous)
        self.assertEqual(applied["modules"], self.layout["modules"])

    def test_new_cross_boundary_import_in_original_or_helper_is_rejected(self):
        policy = self.policy()
        for path in ("Lib/Main.lean", self.add_library_helper()):
            with self.subTest(path=path):
                self.imports[path].append("Broken")
                with self.assertRaisesRegex(ValueError, "frozen verification boundary"):
                    scope.apply(self.root, self.layout, policy)
                self.imports[path].remove("Broken")

    def test_removing_import_does_not_shrink_captured_readonly_inventory(self):
        policy = self.policy()
        self.imports["Lib/Main.lean"] = ["Init"]
        applied = scope.apply(self.root, self.layout, policy)
        self.assertIn("scripts/Required.lean", applied["verification_modules"])
        self.assertNotIn("scripts/Required.lean", applied["editable_modules"])

    def test_new_executable_never_becomes_a_library_by_directory_prefix(self):
        policy = self.policy()
        self.layout["modules"]["Lib/New.lean"] = "Lib.New"
        self.layout["module_owners"]["Lib/New.lean"] = {"libraries": [], "executables": ["New"]}
        with self.assertRaisesRegex(ValueError, "must belong to a selected Lean library"):
            scope.apply(self.root, self.layout, policy)

    def test_original_ownership_or_library_selection_change_rejected(self):
        policy = self.policy()
        self.layout["module_owners"]["scripts/Broken.lean"]["libraries"] = ["Lib"]
        with self.assertRaisesRegex(ValueError, "ownership changed"):
            scope.apply(self.root, self.layout, policy)
        self.layout["module_owners"]["scripts/Broken.lean"]["libraries"] = []
        self.layout["libraries"].append("New")
        with self.assertRaisesRegex(ValueError, "library selection changed"):
            scope.apply(self.root, self.layout, policy)

    def test_scope_tamper_missing_metadata_and_resealed_invalid_closure_fail_closed(self):
        policy = self.policy()
        broken = copy.deepcopy(policy)
        broken["editable_modules"]["scripts/Broken.lean"] = "Broken"
        self.assertTrue(scope.errors(broken))
        self.assertTrue(scope.errors(scope._seal(broken)))
        missing = copy.deepcopy(self.layout)
        del missing["module_owners"]
        with self.assertRaisesRegex(ValueError, "native Lake ownership"):
            scope.capture(self.root, missing, "libraries")
        baseline = self.capture()
        baseline["verification_scope"] = broken
        self.assertTrue(project.baseline_errors(project._seal(baseline)))

    def test_frozen_auxiliary_manifest_cannot_unlock_pinned_bytes_or_outputs(self):
        baseline = self.capture()
        state = {"project_baseline": baseline}
        for path in ("scripts/Broken.lean", "scripts/Required.lean", "Scratch.lean"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "frozen auxiliary"):
                    project.require_pinned_inputs(self.root, baseline, allowed_new_paths={path})
                candidate = {"task_id": "t", "outputs": [{"file": path, "declaration": "claimed"}]}
                issues = bump_files.validate_candidate_files(state, candidate, changed_paths=[path], deleted_paths=[])
                self.assertTrue(any(row["code"] == "project_scope_violation" for row in issues))
                issues = bump_files.validate_candidate_files(state, candidate, changed_paths=[], deleted_paths=[path])
                self.assertTrue(any(row["code"] == "project_scope_violation" for row in issues))

    def test_file_reservations_cannot_claim_frozen_auxiliary(self):
        from unity import bump_state
        baseline = self.capture()

        @contextmanager
        def transaction(_):
            yield {"project_baseline": baseline}

        with patch.object(bump_state, "transaction", transaction), self.assertRaisesRegex(ValueError, "frozen auxiliary"):
            bump_files.reserve_files(self.root, "Agent", "task", ["scripts/Broken.lean"])

    def test_removing_scope_policy_cannot_downgrade_library_baseline_to_all(self):
        baseline = self.capture()
        del baseline["verification_scope"]
        self.assertTrue(project.baseline_errors(project._seal(baseline)))
        with self.assertRaisesRegex(ValueError, "missing its verification scope"):
            scope.mode(baseline)

    def test_inventory_cannot_escape_scoped_module_boundary(self):
        baseline = self.capture()
        records = {**self.records, "excluded": record("excluded", module="Broken")}
        self.inspection = inventory(records, sorries=["target", "aux_hole"], used=["sorryAx"])
        self.assertTrue(any("inventory escaped" in issue for issue in project.validate_baseline(self.root, baseline)))

    def test_frozen_edit_and_config_edit_rejected_before_native_inspection(self):
        baseline = self.capture()
        for path in ("scripts/Broken.lean", "lakefile.toml"):
            with self.subTest(path=path):
                original = (self.root / path).read_text()
                (self.root / path).write_text(original + "-- change\n")
                with patch.object(contract, "workspace_layout") as native:
                    self.assertTrue(project.validate_baseline(self.root, baseline))
                    native.assert_not_called()
                (self.root / path).write_text(original)

    def test_required_auxiliary_definition_and_library_proof_bodies_stay_protected(self):
        baseline = self.capture()
        for name in ("required", "extra", "stable"):
            with self.subTest(name=name):
                previous = copy.deepcopy(self.inspection)
                row = self.inspection["project_records"][name]
                row["proof_body"] = ["const", "changed"]
                if row["target_kind"] == "def":
                    row["declaration_meaning"]["value"] = row["proof_body"]
                issues = project.validate_baseline(self.root, baseline)
                self.assertTrue(any("protected existing declaration changed: " + name in issue for issue in issues), issues)
                self.inspection = previous

    def test_approved_helper_path_must_pass_fresh_native_library_ownership(self):
        baseline = self.capture()
        path = self.add_library_helper()
        self.assertFalse(project.validate_baseline(self.root, baseline, allowed_new_paths={path}))
        with self.assertRaises(ValueError):
            scope.require_writable_paths(baseline, ["../escape.lean"])
        issues = project.validate_baseline(self.root, baseline, allowed_new_paths={"Scratch.lean"})
        self.assertTrue(issues)

    def test_legacy_all_scope_needs_no_new_ownership_metadata(self):
        policy = scope.capture(self.root, {"modules": {}}, "all")
        layout = scope.apply(self.root, {"modules": {"Main.lean": "Main"}}, policy)
        self.assertEqual(layout["verification_modules"], layout["modules"])
        self.assertEqual(scope.mode({}), "all")
        scope.require_writable_paths({}, ["scripts/Tool.lean"])


if __name__ == "__main__":
    unittest.main()
