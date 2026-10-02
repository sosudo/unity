"""Offline Bump project isolation tests: temporary Git fixtures, no model runs."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_migration_project as project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="unity-bump-project-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "project"
        self.root.mkdir()
        self.write("lean-toolchain", "leanprover/lean4:v4.33.0\n")
        self.write("lakefile.toml", 'name = "fixture"\ndefaultTargets = ["Fixture"]\n\n[[lean_lib]]\nname = "Fixture"\n')
        self.write("lake-manifest.json", json.dumps({"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []}))
        self.write("Fixture.lean", "import Fixture.Basic\ntheorem result : True := Fixture.basic\n")
        self.write("Fixture/Basic.lean", "theorem Fixture.basic : True := by trivial\n")
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Bump Fixture")
        self.commit()

    def write(self, name, text, root=None):
        path = (root or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args, root=None):
        result = subprocess.run(["git", *args], cwd=root or self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def prepare(self, **kwargs):
        return project.prepare(self.root, kwargs.get("version", "v4.34.0"), kwargs.get("pins", {}),
                               run_id=kwargs.get("run_id", "test-run"))

    def dependencies(self):
        packages = [{"name": "mathlib", "type": "git", "url": "https://example.invalid/mathlib",
                     "rev": "1" * 40, "inputRev": "v4.33.0", "inherited": False},
                    {"name": "support", "type": "git", "url": "https://example.invalid/support",
                     "rev": "2" * 40, "inputRev": "v1.0.0", "inherited": False}]
        self.write("lake-manifest.json", json.dumps({"packagesDir": ".lake/packages", "packages": packages}))
        self.write("lakefile.toml", (self.root / "lakefile.toml").read_text() +
                   '\n[[require]]\nname = "mathlib"\ngit = "https://example.invalid/mathlib"\nrev = "v4.33.0" # retain\n'
                   '\n[[require]]\nname = "support"\ngit = "https://example.invalid/support"\nrev = "v1.0.0"\n')
        self.commit()

    def test_isolated_original_target_and_original_checkout_unchanged(self):
        before = project.snapshot(self.root)
        baseline = self.prepare()
        original, target = project.resolve_paths(self.root, baseline)
        self.assertEqual(before, project.snapshot(self.root))
        self.assertEqual(before, project.snapshot(original))
        self.assertEqual(baseline["source_commit"], baseline["head"])
        self.assertEqual(Path(baseline["target_path"]), target)
        self.assertFalse((original / ".lake").exists())
        self.assertFalse((target / ".lake").exists())
        self.assertEqual((target / "lean-toolchain").read_text(), "leanprover/lean4:v4.34.0\n")
        self.assertEqual(project.validate_original(self.root, baseline), [])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_no_native_or_resolver_commands_in_prepare(self):
        original = project._run
        calls = []

        def record(root, command, **kwargs):
            calls.append(command)
            return original(root, command, **kwargs)

        with patch.object(project, "_run", side_effect=record):
            self.prepare()
        self.assertTrue(calls)
        self.assertTrue(all(command[0] == "git" for command in calls))

    def test_fresh_work_is_never_overwritten(self):
        baseline = self.prepare()
        with self.assertRaisesRegex(project.ProjectError, "resumed explicitly"):
            self.prepare()
        self.assertTrue(Path(baseline["original"]).exists())

    def test_dirty_original_blocks(self):
        self.write("Fixture/Basic.lean", "theorem Fixture.basic : True := by sorry\n")
        with self.assertRaisesRegex(project.ProjectError, "must be clean"):
            self.prepare()

    def test_exact_versions_only(self):
        for version in ("latest", "stable", "nightly", "master", "v4", "v4.34.0;touch nope", "../v4.34.0"):
            with self.subTest(version=version), self.assertRaises(project.ProjectError):
                self.prepare(version=version)

    def test_invalid_run_identifier_blocks_escape(self):
        for value in ("../escape", "x/y", "-x", ""):
            with self.subTest(value=value), self.assertRaises(project.ProjectError):
                self.prepare(run_id=value)

    def test_dependency_option_injection_is_rejected(self):
        with self.assertRaisesRegex(project.ProjectError, "safe package identifiers"):
            self.prepare(pins={"--help": "v4.34.0"})

    def test_original_source_mutation_is_detected(self):
        baseline = self.prepare()
        self.write("Fixture.lean", "theorem result : True := by trivial\n", root=Path(baseline["original"]))
        self.assertIn("original worktree bytes changed", project.validate_original(self.root, baseline))

    def test_baseline_forgery_and_workspace_redirection_fail(self):
        baseline = self.prepare()
        baseline["target"] = str(self.root)
        with self.assertRaises(project.ProjectError):
            project.resolve_paths(self.root, baseline)

    def test_no_pin_resolution_never_runs_blanket_update(self):
        baseline = self.prepare()
        original = project._run
        calls = []

        def record(root, command, **kwargs):
            calls.append(command)
            return original(root, command, **kwargs)

        with patch.object(project, "_run", side_effect=record):
            receipt = project.resolve_dependencies(Path(baseline["target"]), baseline)
        self.assertTrue(receipt["passed"])
        self.assertEqual(receipt["command"], [])
        self.assertFalse(any(command[0] == "lake" for command in calls))

    def test_sealed_config_catches_manifest_and_toolchain_drift(self):
        baseline = self.prepare()
        target = Path(baseline["target"])
        self.assertTrue(project.validate_target(target, baseline))
        sealed = project.seal_target(target, baseline)
        self.assertEqual(project.validate_target(target, sealed), [])
        with self.assertRaisesRegex(project.ProjectError, "sealed target"):
            project.resolve_dependencies(target, sealed)
        self.write("lake-manifest.json", (target / "lake-manifest.json").read_text() + "\n", root=target)
        self.assertIn("Target configuration changed: lake-manifest.json", project.validate_target(target, sealed))
        self.write("lean-toolchain", "leanprover/lean4:v4.35.0\n", root=target)
        self.assertIn("Target toolchain does not match requested version", project.validate_target(target, sealed))

    def test_only_requested_toml_dependency_changes(self):
        self.dependencies()
        baseline = self.prepare(pins={"mathlib": "v4.34.0"})
        target = Path(baseline["target"])
        config = (target / "lakefile.toml").read_text()
        self.assertIn('rev = "v4.34.0" # retain', config)
        self.assertIn('rev = "v1.0.0"', config)
        self.assertNotIn("LeanArchitect", config)
        self.assertIn('rev = "v4.33.0"', (self.root / "lakefile.toml").read_text())

    def test_dependency_resolution_rejects_unrequested_transitive_change(self):
        self.dependencies()
        baseline = self.prepare(pins={"mathlib": "v4.34.0"})
        target = Path(baseline["target"])
        original = project._run

        def resolve(root, command, **kwargs):
            if command[0] == "lake":
                self.assertEqual(command, ["lake", "update", "mathlib"])
                data = json.loads((target / "lake-manifest.json").read_text())
                data["packages"][0].update(rev="3" * 40, inputRev="v4.34.0")
                data["packages"][1]["rev"] = "4" * 40
                self.write("lake-manifest.json", json.dumps(data), root=target)
                return subprocess.CompletedProcess(command, 0, "resolved", "")
            return original(root, command, **kwargs)

        with patch.object(project, "_run", side_effect=resolve):
            receipt = project.resolve_dependencies(target, baseline)
        self.assertFalse(receipt["passed"])
        self.assertIn("Unrequested dependency changed: support", receipt["errors"])
        with self.assertRaisesRegex(project.ProjectError, "Invalid dependency-resolution"):
            project.seal_target(target, baseline, resolution=receipt)

    def test_exact_requested_dependency_can_be_sealed(self):
        self.dependencies()
        baseline = self.prepare(pins={"mathlib": "3" * 40})
        target = Path(baseline["target"])
        original = project._run

        def resolve(root, command, **kwargs):
            if command[0] == "lake":
                data = json.loads((target / "lake-manifest.json").read_text())
                data["packages"][0].update(rev="3" * 40, inputRev="3" * 40)
                self.write("lake-manifest.json", json.dumps(data), root=target)
                return subprocess.CompletedProcess(command, 0, "", "")
            return original(root, command, **kwargs)

        with patch.object(project, "_run", side_effect=resolve):
            receipt = project.resolve_dependencies(target, baseline)
        self.assertTrue(receipt["passed"], receipt)
        sealed = project.seal_target(target, baseline, resolution=receipt)
        self.assertNotEqual(sealed["identity"], baseline["identity"])
        self.assertEqual(project.validate_target(target, sealed), [])

    def test_parent_relative_dependency_blocks(self):
        self.write("lake-manifest.json", json.dumps({"packages": [{"name": "shared", "type": "path", "dir": "../shared"}]}))
        self.commit()
        with self.assertRaisesRegex(project.ProjectError, "not an isolated Git dependency"):
            self.prepare()

    def test_inventory_includes_optional_sources_and_ignores_comments(self):
        self.write("Optional/Tool.lean", '/- import Fake\n/- import More -/\n-/\npublic import Fixture.Basic -- note\n#check "import Fake"\n')
        modules = project.inventory_modules(self.root)
        self.assertEqual(modules["Optional.Tool"]["imports"], ["Fixture.Basic"])
        self.assertNotIn("lakefile", modules)

    def test_ignored_lean_sources_block_incomplete_copy(self):
        self.write(".gitignore", "Ignored.lean\n")
        self.commit()
        self.write("Ignored.lean", "theorem ignored : True := by trivial\n")
        with self.assertRaisesRegex(project.ProjectError, "omitted project files"):
            self.prepare()

    def test_snapshot_sees_untracked_lean_but_not_cache(self):
        before = project.snapshot(self.root)
        self.write(".lake/build/Test.lean", "cache")
        self.write(".unity/draft.lean", "state")
        self.write(".bump-runtime/worker.lean", "worker scratch")
        self.assertEqual(before, project.snapshot(self.root))
        self.write("New.lean", "theorem newResult : True := by trivial\n")
        self.assertNotEqual(before, project.snapshot(self.root))

    def test_cache_symlink_is_rejected(self):
        baseline = self.prepare()
        target = Path(baseline["target"])
        (target / ".lake").symlink_to(self.root)
        self.assertTrue(any("symlink" in error for error in project.validate_target(target, baseline)))
        with self.assertRaisesRegex(project.ProjectError, "symlink"):
            project.build(target, ["Fixture.Basic"])

    def test_native_graph_uses_compiler_dependencies(self):
        original = project._run

        def deps(root, command, **kwargs):
            if command[0] == "lake":
                imported = str(self.root / ".lake/build/lib/lean/Fixture/Basic.olean") + "\n" if command[-1] == "Fixture.lean" else ""
                return subprocess.CompletedProcess(command, 0, imported, "")
            return original(root, command, **kwargs)

        with patch.object(project, "_run", side_effect=deps):
            graph = project.compiler_modules(self.root)
        self.assertEqual(graph["Fixture"]["imports"], ["Fixture.Basic"])
        self.assertTrue(graph["Fixture"]["compiler_derived"])

    def test_native_graph_missing_local_import_blocks(self):
        original = project._run

        def deps(root, command, **kwargs):
            if command[0] == "lake":
                return subprocess.CompletedProcess(command, 0, "", "")
            return original(root, command, **kwargs)

        with patch.object(project, "_run", side_effect=deps), self.assertRaisesRegex(project.ProjectError, "coverage"):
            project.compiler_modules(self.root)

    def test_module_build_is_partial_not_whole_project(self):
        original = project._run
        calls = []

        def run(root, command, **kwargs):
            if command[0] == "lake":
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "Built Fixture.Basic", "")
            return original(root, command, **kwargs)

        with patch.object(project, "_run", side_effect=run):
            receipt = project.build(self.root, ["Fixture.Basic"])
        self.assertTrue(receipt["passed"])
        self.assertEqual(calls, [["lake", "--rehash", "build", "+Fixture.Basic"]])

    def test_build_source_mutation_invalidates_receipt(self):
        original = project._run

        def run(root, command, **kwargs):
            if command[0] == "lake":
                self.write("Fixture/Basic.lean", "theorem altered : True := by trivial\n")
                return subprocess.CompletedProcess(command, 0, "", "")
            return original(root, command, **kwargs)

        with patch.object(project, "_run", side_effect=run):
            receipt = project.build(self.root)
        self.assertFalse(receipt["passed"])
        self.assertFalse(receipt["source_unchanged"])

    def test_build_command_injection_is_rejected(self):
        with self.assertRaises(project.ProjectError):
            project.build(self.root, ["Fixture;touch BAD"])

    def test_lean_lakefile_precise_revision_replacement(self):
        source = 'import Lake\nopen Lake DSL\npackage fixture\nrequire mathlib from git "https://example.invalid/m" @ "v4.33.0"\n'
        updated = project._edit_lean(source, {"mathlib": "v4.34.0"})
        self.assertEqual(updated, source.replace('"v4.33.0"', '"v4.34.0"'))

    def test_lean_lakefile_computed_or_commented_revision_blocks(self):
        for source in ('require mathlib from git "url" @ revision\n',
                       '/-\nrequire mathlib from git "url" @ "v4.33.0"\n-/\n',
                       'require "scope" / "mathlib" @ git "v4.33.0"\n'):
            with self.subTest(source=source), self.assertRaises(project.ProjectError):
                project._edit_lean(source, {"mathlib": "v4.34.0"})

    def package_checkout(self):
        package = self.root / ".lake/packages/support"
        package.mkdir(parents=True)
        self.git("init", "-q", root=package)
        self.git("config", "user.email", "fixture@example.invalid", root=package)
        self.git("config", "user.name", "Bump Fixture", root=package)
        self.write("Support.lean", "theorem support : True := by trivial\n", root=package)
        self.git("add", ".", root=package)
        self.git("commit", "-qm", "package", root=package)
        self.git("remote", "add", "origin", "https://example.invalid/support", root=package)
        revision = self.git("rev-parse", "HEAD", root=package)
        self.write("lake-manifest.json", json.dumps({"packages": [{"name": "support", "type": "git",
                   "url": "https://example.invalid/support", "rev": revision, "inputRev": "v1.0.0"}]}))
        return package

    def test_dependency_actual_checkout_identity_and_cleanliness(self):
        package = self.package_checkout()
        self.assertEqual(project.validate_dependencies(self.root), [])
        self.assertFalse((package / ".unity").exists())
        self.write("Support.lean", "axiom support : False\n", root=package)
        self.assertIn("Dependency source checkout is dirty: support", project.validate_dependencies(self.root))

    def test_dependency_actual_origin_drift_is_detected(self):
        package = self.package_checkout()
        self.git("remote", "set-url", "origin", "https://example.invalid/other", root=package)
        self.assertIn("Dependency origin differs from manifest: support", project.validate_dependencies(self.root))

    def test_dependency_actual_commit_drift_is_detected(self):
        package = self.package_checkout()
        self.write("Other.lean", "theorem other : True := by trivial\n", root=package)
        self.git("add", ".", root=package)
        self.git("commit", "-qm", "drift", root=package)
        self.assertIn("Dependency checkout commit differs from manifest: support", project.validate_dependencies(self.root))

    def test_dependency_symlink_is_rejected(self):
        package = self.package_checkout()
        moved = self.root.parent / "external-package"
        package.rename(moved)
        package.symlink_to(moved)
        self.assertIn("Dependency checkout is missing or escapes its environment: support", project.validate_dependencies(self.root))

    def test_command_environment_isolates_native_toolchain_and_cache(self):
        with patch.dict("os.environ", {"ELAN_TOOLCHAIN": "wrong", "LEAN_PATH": "/wrong"}), \
                patch.object(project.bump_jobs, "run") as run:
            project._run(self.root, ["lake", "env", "lean", "--version"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["ELAN_TOOLCHAIN"], "leanprover/lean4:v4.33.0")
        self.assertNotIn("LEAN_PATH", env)
        self.assertTrue(env["LAKE_CACHE_DIR"].startswith(str(self.root / ".unity")))
        self.assertEqual(run.call_args.kwargs["cwd"], self.root)
        self.assertEqual(run.call_args.args[0], self.root)

    def test_bounded_timeout_uses_shared_cancellation_registry(self):
        with patch.object(project.bump_jobs, "run", side_effect=subprocess.TimeoutExpired(["lake"], 1)) as run, \
                self.assertRaises(subprocess.TimeoutExpired):
            project._run(self.root, ["lake", "build"], timeout=1)
        self.assertEqual(run.call_args.kwargs["timeout"], 1)

    def test_dependency_command_uses_parent_unity_registry_without_dirtying_package(self):
        package = self.package_checkout()
        (self.root / ".unity").mkdir(exist_ok=True)
        with patch.object(project.bump_jobs, "run") as run:
            project._run(package, ["git", "status", "--porcelain"])
        self.assertEqual(run.call_args.args[0], self.root)
        self.assertEqual(run.call_args.kwargs["cwd"], package)
        self.assertFalse((package / ".unity").exists())

    def test_native_compiler_graph_and_separate_version_builds(self):
        if not shutil.which("elan"):
            self.skipTest("Native fixture requires two already-installed Lean versions")
        available = subprocess.run(["elan", "toolchain", "list"], text=True, capture_output=True, timeout=20)
        installed = {line.split()[0] for line in available.stdout.splitlines() if line.strip()}
        if not {"leanprover/lean4:v4.33.0", "leanprover/lean4:v4.34.1"}.issubset(installed):
            self.skipTest("Native fixture never installs missing toolchains")
        baseline = self.prepare(version="v4.34.1")
        original, target = project.resolve_paths(self.root, baseline)
        original_build = project.build(original)
        self.assertTrue(original_build["passed"], original_build)
        old_graph = project.compiler_modules(original)
        self.assertEqual(old_graph["Fixture"]["imports"], ["Fixture.Basic"])
        sealed = project.seal_target(target, baseline)
        target_build = project.build(target, ["Fixture.Basic"])
        self.assertTrue(target_build["passed"], target_build)
        self.assertFalse((target / ".lake/build/lib/lean/Fixture.olean").exists())
        self.assertTrue((original / ".lake/build/lib/lean/Fixture.olean").exists())
        self.assertFalse((target / ".lake").is_symlink())
        self.assertEqual(project.validate_original(self.root, sealed), [])
        self.assertEqual(project.validate_target(target, sealed), [])


if __name__ == "__main__":
    unittest.main()
