"""Original/target isolation and pinning without models or a compiler."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_preparation as prep


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for args in (("init", "-b", "main"), ("config", "user.name", "Bump Test"),
                     ("config", "user.email", "bump@example.test")):
            prep.git(self.root, *args)
        (self.root / ".gitignore").write_text(".unity/\n.lake/\n")
        (self.root / "lean-toolchain").write_text("leanprover/lean4:v4.28.0-rc1\n")
        (self.root / "lakefile.toml").write_text('name = "Fixture"\n[[require]]\nname = "mathlib"\ngit = "https://example.invalid/mathlib"\nrev = "old"\n')
        self.manifest = {"version": "1.1.0", "packagesDir": ".lake/packages", "packages": [
            {"name": "mathlib", "type": "git", "url": "https://example.invalid/mathlib", "rev": "a" * 40,
             "inputRev": "old", "inherited": False},
            {"name": "support", "type": "git", "url": "https://example.invalid/support", "rev": "c" * 40,
             "inputRev": "old", "inherited": True}]}
        (self.root / "lake-manifest.json").write_text(json.dumps(self.manifest))
        (self.root / "Fixture.lean").write_text("def original : Nat := 1\n")
        prep.git(self.root, "add", ".")
        prep.git(self.root, "commit", "-qm", "fixture")
        native = patch.object(prep.bump_workspace, "discover", return_value={"build_dir": ".lake/build"})
        self.native = native.start()
        self.addCleanup(native.stop)

    def test_prepare_preserves_original_and_changes_only_target_requested_config(self):
        before = prep.source_files(self.root)
        migration = prep.prepare(self.root, "v4.34.1", {"mathlib": "b" * 40}, run_id="bump-123456789abc", project_scope="build")
        original, target = Path(migration["original_root"]), Path(migration["target_root"])
        self.assertEqual(prep.source_files(self.root), before)
        self.assertEqual(prep.source_files(original), before)
        self.assertEqual((target / "Fixture.lean").read_bytes(), (original / "Fixture.lean").read_bytes())
        self.assertIn("v4.34.1", (target / "lean-toolchain").read_text())
        self.assertIn("b" * 40, (target / "lakefile.toml").read_text())

    def test_resolution_rejects_unrequested_transitive_changes(self):
        migration = prep.prepare(self.root, "v4.34.1", {"mathlib": "b" * 40}, run_id="bump-123456789abc", project_scope="build")
        target = Path(migration["target_root"])
        def resolve(*args, **kwargs):
            data = json.loads((target / "lake-manifest.json").read_text())
            data["packages"][0]["rev"] = "b" * 40
            data["packages"][1]["rev"] = "d" * 40
            (target / "lake-manifest.json").write_text(json.dumps(data))
            return subprocess.CompletedProcess([], 0, "", "")
        with patch.object(prep.bump_jobs, "run", side_effect=resolve):
            with self.assertRaisesRegex(ValueError, "unrequested dependency"):
                prep.resolve_dependencies(target, migration)

    def test_native_scope_selects_defaults_without_notes(self):
        migration = {"original_files": prep.source_files(self.root), "scope": {"mode": "build", "build_dir": ".lake/build"}}
        layout = {"modules": {"Fixture.lean": "Fixture", "Notes.lean": "Notes"},
                  "default_modules": {"Fixture.lean": "Fixture"}, "unknown_default_targets": [], "build_dir": ".lake/build"}
        with patch.object(prep.bump_workspace, "discover", return_value=layout), patch.object(prep.bump_workspace, "read_imports", return_value={"Fixture.lean": ["Init"]}):
            scope = prep.capture_scope(self.root, migration)
        self.assertEqual(scope["selected_modules"], {"Fixture": "Fixture.lean"})
        self.assertIn(".gitignore", scope["excluded_files"])

    def test_layout_only_empty_modules_gets_native_source_ownership_without_compiling_notes(self):
        (self.root / "Notes.lean").write_text("intentionally not valid Lean\n")
        migration = {"original_files": prep.source_files(self.root), "scope": {"mode": "build", "build_dir": ".lake/build"}}
        layout = {"modules": {}, "default_modules": {"Fixture.lean": "Fixture"},
                  "unknown_default_targets": [], "build_dir": ".lake/build"}
        ownership = {**layout, "modules": {"Fixture.lean": "Fixture"}, "unmatched": ["Notes.lean"]}
        with patch.object(prep.bump_workspace, "discover", side_effect=[layout, ownership]) as discover, \
             patch.object(prep.bump_workspace, "read_imports", return_value={"Fixture.lean": ["Init"]}) as headers:
            scope = prep.capture_scope(self.root, migration)
        self.assertEqual(discover.call_args_list[1].args[1], ["Fixture.lean", "Notes.lean"])
        headers.assert_called_once_with(self.root, ["Fixture.lean"])
        self.assertEqual(scope["selected_modules"], {"Fixture": "Fixture.lean"})
        self.assertIn("Notes.lean", scope["excluded_files"])

    def test_opaque_default_is_rejected(self):
        layout = {"default_modules": {}, "unknown_default_targets": ["custom"], "build_dir": ".lake/build"}
        with patch.object(prep.bump_workspace, "discover", return_value=layout):
            with self.assertRaisesRegex(ValueError, "native default"):
                prep.capture_scope(self.root, {"original_files": prep.source_files(self.root),
                    "scope": {"mode": "build", "build_dir": ".lake/build"}})

    def test_source_files_excludes_only_sealed_custom_build_subtree(self):
        generated = self.root / "generated"
        (generated / "build").mkdir(parents=True)
        (generated / "build" / "Fixture.olean").write_bytes(b"compiled-cache")
        (generated / "reference.txt").write_text("source input")
        files = prep.source_files(self.root, build_dir="generated/build")
        self.assertNotIn("generated/build/Fixture.olean", files)
        self.assertIn("generated/reference.txt", files)
        self.native.assert_not_called()

    def test_custom_build_dir_is_captured_before_source_map_and_worktree_copy(self):
        (self.root / ".gitignore").write_text(".unity/\n.lake/\ngenerated/build/\n")
        prep.git(self.root, "add", ".gitignore")
        prep.git(self.root, "commit", "-qm", "custom build directory")
        output = self.root / "generated/build"
        output.mkdir(parents=True)
        (output / "Fixture.olean").write_bytes(b"original compiled cache")
        self.native.return_value = {"build_dir": "generated/build"}
        migration = prep.prepare(self.root, "v4.34.1", {}, run_id="bump-123456789abc", project_scope="build")
        self.native.assert_called_once_with(self.root, ["--layout-only"])
        self.assertEqual(migration["scope"]["build_dir"], "generated/build")
        self.assertFalse(any(path.startswith("generated/build/") for path in migration["original_files"]))
        self.assertEqual(prep.source_files(Path(migration["original_root"]), build_dir="generated/build"),
                         migration["original_files"])

    def test_missing_nonlocal_or_redirected_native_build_dir_is_rejected(self):
        for directory in (None, "", ".", "../outside", str(self.root / "absolute"), "generated/../output"):
            self.native.return_value = {"build_dir": directory}
            with self.subTest(directory=directory), self.assertRaisesRegex(ValueError, "build directory"):
                prep.prepare(self.root, "v4.34.1", {}, run_id="bump-123456789abc", project_scope="build")
        (self.root / "redirect").symlink_to(self.root / ".lake", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "build directory"):
            prep.source_files(self.root, build_dir="redirect/build")

    def test_dependency_update_may_refresh_sealed_custom_build_output_only(self):
        self.native.return_value = {"build_dir": "generated/build"}
        migration = prep.prepare(self.root, "v4.34.1", {"mathlib": "b" * 40},
                                 run_id="bump-123456789abc", project_scope="build")
        target = Path(migration["target_root"])
        def resolve(*args, **kwargs):
            data = json.loads((target / "lake-manifest.json").read_text())
            data["packages"][0]["rev"] = "b" * 40
            (target / "lake-manifest.json").write_text(json.dumps(data))
            generated = target / "generated/build"
            generated.mkdir(parents=True)
            (generated / "Fixture.olean").write_bytes(b"new compiled cache")
            return subprocess.CompletedProcess([], 0, "", "")
        with patch.object(prep.bump_jobs, "run", side_effect=resolve):
            resolved = prep.resolve_dependencies(target, migration)
        self.assertEqual(resolved["packages"][0]["rev"], "b" * 40)

    def test_native_build_dir_cannot_hide_tracked_original_inputs(self):
        (self.root / "inputs").mkdir()
        (self.root / "inputs" / "source.txt").write_text("preserve")
        prep.git(self.root, "add", "inputs/source.txt")
        prep.git(self.root, "commit", "-qm", "tracked input")
        self.native.return_value = {"build_dir": "inputs"}
        with self.assertRaisesRegex(ValueError, "tracked original inputs"):
            prep.prepare(self.root, "v4.34.1", {}, run_id="bump-123456789abc", project_scope="build")

    def test_capture_scope_rejects_changed_native_build_dir(self):
        self.native.return_value = {"build_dir": "different/output"}
        with self.assertRaisesRegex(ValueError, "build directory changed"):
            prep.capture_scope(self.root, {"original_files": prep.source_files(self.root),
                "scope": {"mode": "build", "build_dir": ".lake/build"}})

    def test_unknown_dependency_rejected_before_native_job(self):
        with self.assertRaisesRegex(ValueError, "absent from the original manifest"):
            prep.prepare(self.root, "v4.34.1", {"missing": "b" * 40},
                         run_id="bump-123456789abc", project_scope="build")
        self.native.assert_not_called()

    def test_malformed_config_rejected_before_native_job(self):
        (self.root / "lakefile.toml").write_text("[broken TOML\n")
        prep.git(self.root, "add", "lakefile.toml")
        prep.git(self.root, "commit", "-qm", "malformed original configuration")
        with self.assertRaises(ValueError):
            prep.prepare(self.root, "v4.34.1", {}, run_id="bump-123456789abc", project_scope="build")
        self.native.assert_not_called()

    def test_lean_config_edit_keeps_unrelated_bytes(self):
        old = 'import Lake\nopen Lake DSL\nrequire mathlib from git\n  "https://example.invalid/mathlib" @ "old"\npackage Fixture\n'
        edited = prep._edit_lean(old, {"mathlib": "b" * 40})
        self.assertEqual(edited, old.replace('"old"', '"' + "b" * 40 + '"'))

    def test_toml_edit_rejects_unknown_pin(self):
        with self.assertRaisesRegex(ValueError, "not a direct"):
            prep._edit_toml((self.root / "lakefile.toml").read_text(), {"missing": "b" * 40})
