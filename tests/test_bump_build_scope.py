"""Default-build native scope is distinct from byte-preserved repository inputs."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from unity import bump_migration_project as project
from tests import test_bump_project as fixtures


class BuildScopeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.root = fixture, fixture.root
        fixture.write("Notes/IntentionalError.lean", "#check deliberatelyUnknown\n")
        fixture.write("Optional/Support.lean", "theorem support : True := by trivial\n")
        fixture.write("Optional/Unused.lean", "#check intentionallyNotBuildable\n")
        fixture.write("README.md", "Original documentation must remain byte-identical.\n")
        fixture.commit()

    def prepare(self, mode="build"):
        return project.prepare(self.root, "v4.34.1", {}, run_id="scope-fixture", project_scope=mode)

    def metadata(self):
        modules = {"Fixture.lean": "Fixture", "Fixture/Basic.lean": "Fixture.Basic",
                   "Optional/Support.lean": "Optional.Support", "Optional/Unused.lean": "Optional.Unused"}
        return {"modules": modules,
            "module_owners": {path: {"libraries": ["Fixture" if name.startswith("Fixture") else "Optional"], "executables": []}
                              for path, name in modules.items()},
            "source_roots": [{"kind": "library", "name": name, "path": "."} for name in ("Fixture", "Optional")],
            "default_targets": [{"kind": "library", "name": "Fixture", "path": "."}],
            "build_dir": ".lake/build"}

    def capture(self):
        migration = self.prepare()
        original = Path(migration["original_path"])
        headers = {"Fixture": ["Fixture.Basic", "Optional.Support"], "Fixture.Basic": [], "Optional.Support": []}
        with patch.object(project, "_scope_native", return_value=(self.metadata(), {"Fixture": "Fixture.lean"}, project.source_files(original))), \
                patch.object(project, "_native_headers", side_effect=lambda _root, modules: {name: headers[name] for name in modules}):
            return project.capture_build_scope(original, migration)

    def test_prepare_defers_native_scope_until_after_default_build(self):
        with patch.object(project, "_scope_native") as native:
            migration = self.prepare()
        native.assert_not_called()
        self.assertEqual(migration["scope"], {"version": 1, "mode": "build", "pending": True, "default_build_required": True})

    def test_build_scope_does_not_lexically_parse_excluded_broken_notes(self):
        self.fixture.write("Notes/IntentionalError.lean", '/- intentionally unfinished note\n')
        self.fixture.commit()
        with patch.object(project, "inventory_modules", side_effect=AssertionError("excluded source was parsed")):
            migration = self.prepare()
        self.assertEqual(migration["modules"], {})

    def test_native_defaults_include_nondefault_import_closure_but_not_notes(self):
        migration = self.capture()
        scope = migration["scope"]
        self.assertEqual(set(scope["selected_modules"]), {"Fixture", "Fixture.Basic", "Optional.Support"})
        self.assertEqual(scope["excluded_modules"], {"Optional.Unused": "Optional/Unused.lean"})
        self.assertIn("Notes/IntentionalError.lean", scope["excluded_files"])
        self.assertIn("README.md", scope["excluded_files"])
        self.assertNotIn("lean-toolchain", scope["excluded_files"])
        self.assertEqual(project.scope_errors(scope, migration["source_files"]), [])
        self.assertEqual(project.validate_original(self.root, migration), [])

    def test_strict_all_scope_needs_no_lake_selection_and_excludes_no_lean(self):
        migration = self.prepare("all")
        with patch.object(project, "_scope_native") as native:
            migration = project.capture_build_scope(Path(migration["original_path"]), migration)
        native.assert_not_called()
        self.assertIn("Notes.IntentionalError", migration["scope"]["selected_modules"])
        self.assertEqual(migration["scope"]["excluded_modules"], {})
        self.assertEqual(set(migration["scope"]["excluded_files"]), {"README.md"})

    def test_scope_tampering_and_incomplete_file_partition_fail(self):
        migration = self.capture()
        scope = deepcopy(migration["scope"])
        scope["selected_modules"].pop("Optional.Support")
        self.assertTrue(project.scope_errors(scope))
        scope = deepcopy(migration["scope"])
        scope["excluded_files"].pop("README.md")
        scope = project._scope_seal(scope)
        self.assertTrue(project.scope_errors(scope, migration["source_files"]))

    def test_excluded_bytes_and_file_set_are_frozen_without_requiring_config_bytes(self):
        migration = self.capture()
        target, scope = Path(migration["target_path"]), migration["scope"]
        with patch.object(project, "_scope_native", return_value=(self.metadata(), scope["native_default_modules"], {})), \
                patch.object(project, "_native_headers", return_value={name: [] for name in scope["selected_modules"]}):
            self.assertEqual(project.validate_build_scope(target, scope), [])
            (target / "README.md").write_text("changed")
            self.assertTrue(project.validate_build_scope(target, scope))
            (target / "README.md").write_text((self.root / "README.md").read_text())
            (target / "New.lean").write_text("def newSource := 0\n")
            self.assertTrue(project.validate_build_scope(target, scope))

    def test_current_imports_can_change_within_selected_but_not_to_excluded(self):
        migration = self.capture()
        scope, target = migration["scope"], Path(migration["target_path"])
        headers = {"Fixture": ["Optional.Support"], "Fixture.Basic": [], "Optional.Support": []}
        with patch.object(project, "_scope_native", return_value=(self.metadata(), scope["native_default_modules"], {})), \
                patch.object(project, "_native_headers", return_value=headers):
            self.assertEqual(project.validate_build_scope(target, scope), [])
            headers["Fixture"] = ["Optional.Unused"]
            self.assertIn("excluded Bump module", project.validate_build_scope(target, scope)[0])

    def test_changed_native_defaults_or_ownership_cannot_broaden_scope(self):
        migration = self.capture()
        scope, target = migration["scope"], Path(migration["target_path"])
        changed = self.metadata()
        changed["default_targets"].append({"kind": "library", "name": "Optional", "path": "."})
        with patch.object(project, "_scope_native", return_value=(changed, scope["native_default_modules"], {})):
            self.assertIn("sealed Bump boundary", project.validate_build_scope(target, scope)[0])

    def test_scope_capture_is_one_shot(self):
        migration = self.capture()
        with self.assertRaisesRegex(project.ProjectError, "exactly once"):
            project.capture_build_scope(Path(migration["original_path"]), migration)

    def test_unregistered_notes_stale_artifact_cannot_enter_compiler_graph(self):
        migration = self.capture()
        original, scope = Path(migration["original_path"]), migration["scope"]
        stale = original / ".lake/build/lib/lean/Notes/IntentionalError.olean"
        with patch.object(project, "validate_build_scope", return_value=[]), \
                patch.object(project, "_native_headers", return_value={name: ["Notes.IntentionalError"] for name in scope["selected_modules"]}), \
                patch.object(project, "_run", return_value=subprocess.CompletedProcess([], 0, str(stale) + "\n", "")), \
                self.assertRaisesRegex(project.ProjectError, "Native import crosses the sealed Bump boundary"):
            project.compiler_modules(original, scope=scope)

    def test_opaque_custom_default_target_is_not_silently_omitted(self):
        from unity import bump_native, bump_workspace
        layout = {**self.metadata(), "default_modules": {"Fixture.lean": "Fixture"},
                  "unknown_default_targets": ["custom"], "unmatched": []}
        with patch.object(project, "source_files", return_value={"Fixture.lean": "a" * 64}), \
                patch.object(bump_workspace, "discover", return_value=layout), \
                patch.object(bump_native, "executable", return_value=Path("/public-fixture-helper")), \
                patch.object(project, "_run", return_value=subprocess.CompletedProcess([], 1,
                    '{"targets":[],"issues":["unsupported custom default target"]}', "")), \
                self.assertRaisesRegex(project.ProjectError, "Opaque/custom"):
            project._scope_native(self.root)


class NativeBuildScopeTests(unittest.TestCase):
    def test_old_and_new_native_scope_respects_srcdir_import_closure_and_notes(self):
        if not shutil.which("elan"):
            self.skipTest("Native scope fixture requires installed Lean 4.28.0-rc1 and 4.34.1")
        installed = subprocess.run(["elan", "toolchain", "list"], capture_output=True, text=True, timeout=20)
        versions = {line.split()[0] for line in installed.stdout.splitlines() if line.strip()}
        needed = {"leanprover/lean4:v4.28.0-rc1", "leanprover/lean4:v4.34.1"}
        if not needed <= versions:
            self.skipTest("Native scope fixture never installs missing toolchains")
        fixture = fixtures.ProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        fixture.write("lean-toolchain", "leanprover/lean4:v4.28.0-rc1\n")
        fixture.write("lakefile.toml", 'name = "fixture"\ndefaultTargets = ["Core", "app"]\n'
                      '[[lean_lib]]\nname = "Core"\nsrcDir = "src"\n'
                      '[[lean_lib]]\nname = "Optional"\nsrcDir = "src"\n'
                      '[[lean_exe]]\nname = "app"\nroot = "Runner"\nsrcDir = "src"\n')
        fixture.write("src/Core.lean", "import Core.Basic\nimport Optional.Support\n")
        fixture.write("src/Core/Basic.lean", "theorem Core.basic : True := by trivial\n")
        fixture.write("src/Optional/Support.lean", "theorem Optional.support : True := by trivial\n")
        fixture.write("src/Optional/Unused.lean", "#check intentionallyUnbuildable\n")
        fixture.write("src/Runner.lean", "import Core\ndef main : IO Unit := pure ()\n")
        fixture.write("Notes/IntentionalError.lean", "#check deliberatelyUnknown\n")
        fixture.write("README.md", "Keep these original bytes.\n")
        fixture.commit()
        before = project.snapshot(root)
        migration = project.prepare(root, "v4.34.1", {}, run_id="native-build-scope")
        original, target = project.resolve_paths(root, migration)
        result = project.build(original)
        self.assertTrue(result["passed"], result["diagnostics"])
        migration = project.capture_build_scope(original, migration)
        scope = migration["scope"]
        self.assertEqual(set(scope["selected_modules"]), {"Core", "Core.Basic", "Optional.Support", "Runner"})
        self.assertEqual({row["kind"] for row in scope["native_metadata"]["default_targets"]}, {"library", "executable"})
        self.assertEqual(scope["selected_modules"]["Core.Basic"], "src/Core/Basic.lean")
        self.assertIn("Notes/IntentionalError.lean", scope["excluded_files"])
        self.assertIn("Optional.Unused", scope["excluded_modules"])
        graph = project.compiler_modules(original, scope=scope)
        self.assertEqual(graph["Core"]["imports"], ["Core.Basic", "Optional.Support"])
        built = project.build(target)
        self.assertTrue(built["passed"], built["diagnostics"])
        self.assertEqual(project.validate_build_scope(target, scope), [])
        self.assertEqual(project.compiler_modules(target, scope=scope), graph)
        partial = project.build(target, ["Core.Basic"], scope=scope)
        self.assertTrue(partial["passed"], partial["diagnostics"])
        (target / "src/Core.lean").write_text("import Optional.Unused\n")
        self.assertIn("excluded Bump module", project.validate_build_scope(target, scope)[0])
        self.assertEqual(project.snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
