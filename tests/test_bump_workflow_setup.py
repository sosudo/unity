"""Offline upgrade-first preparation and dependency coordination regression tests."""
from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import shutil
import unittest
from unittest.mock import patch

from unity import bump_dependencies as deps, bump_setup as setup, bump_recursive as recursive
from unity.config import Paths


A = "a" * 40
B = "b" * 40
C = "c" * 40
VERSION = "leanprover/lean4:v4.34.1"


def completed(args=(), stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


class SetupFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="unity-bump-setup-test-")
        self.root = Path(self.temp.name).resolve() / "project"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.write(".gitignore", ".unity/\n.lake/\n")
        self.write("lean-toolchain", "leanprover/lean4:v4.28.0-rc1\n")
        self.write("lakefile.toml", 'name = "Fixture"\n[[lean_lib]]\nname = "Fixture"\n')
        self.write("Fixture.lean", "def oldBroken : Nat := noSuchOldDeclaration\n")
        self.write("Extra.lean", "def extra := 1\n")
        self.write("Notes.lean", "def note := 2\n")
        self.write(".unity/UNITY.md", "Fixture scope\n")
        self.write(".unity/agents.yaml", "agents: []\n")
        self.write(".unity/.env", "TEST_ONLY_FIXTURE=not-a-real-secret\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.paths = Paths.from_unity_dir(self.root / ".unity")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, root=None):
        result = subprocess.run(["git", *args], cwd=root or self.root, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def write(self, path, content):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    def manifest(self, packages, *, root=None, **extra):
        value = {"version": "1.1.0", "packagesDir": ".lake/packages", "packages": packages, **extra}
        (root or self.root).joinpath("lake-manifest.json").write_text(json.dumps(value))
        return value

    def package(self, name="dep", revision=A, **extra):
        return {"name": name, "type": "git", "url": "https://example.invalid/" + name,
                "rev": revision, "inputRev": "old", **extra}

    def requirement(self, name="dep", revision="old"):
        self.write("lakefile.toml", 'name = "Fixture"\n[[require]]\nname = ' + json.dumps(name) +
                   '\ngit = "https://example.invalid/' + name + '"\nrev = ' + json.dumps(revision) + "\n")

    def fake_scope(self, root, **kwargs):
        return {"mode": kwargs.get("project_scope", "build"), "selected_modules": {"Fixture": "Fixture.lean"},
                "default_modules": {"Fixture": "Fixture.lean"}, "excluded_files": {"Notes.lean": "outside scope"},
                "build_dir": ".lake/build", "lake_args": list(kwargs.get("lake_args", ())) }


class PreparationTests(SetupFixture):
    def prepare(self, **kwargs):
        with patch.object(setup, "verify_toolchain", return_value={"target_version": VERSION}), \
             patch.object(deps, "resolve_dependencies", return_value={"status": "resolved"}), \
             patch.object(setup, "discover_scope", side_effect=self.fake_scope):
            return setup.prepare(self.paths, "v4.34.1", {}, **kwargs)

    def test_upgrade_first_never_builds_old_sources_and_creates_one_target(self):
        def verify(root, version):
            self.assertTrue((root / ".unity/forum/bump").is_dir())
            self.assertEqual((root / "lean-toolchain").read_text(), VERSION + "\n")
            return {"target_version": version}
        with patch.object(setup, "verify_toolchain", side_effect=verify), \
             patch.object(deps, "resolve_dependencies", return_value={"status": "resolved"}), \
             patch.object(setup, "discover_scope", side_effect=self.fake_scope):
            paths = setup.prepare(self.paths, "v4.34.1", {})
        self.assertEqual(paths.forum, paths.project_root / ".unity/forum/bump")
        self.assertFalse((paths.project_root.parent / "original").exists())
        self.assertFalse((paths.unity / "source").exists())
        self.assertEqual(setup.read_setup(paths)["phase"], "ready")
        self.assertIn("v4.28.0", (self.root / "lean-toolchain").read_text())
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_private_runtime_copies(self):
        paths = self.prepare()
        for name in ("UNITY.md", "agents.yaml", ".env"):
            self.assertEqual((paths.unity / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual((paths.unity / name).read_bytes(), (self.paths.unity / name).read_bytes())

    def test_missing_toolchain_retains_resumable_blocker_and_skips_resolution(self):
        with patch.object(setup, "verify_toolchain", side_effect=ValueError("missing toolchain")), \
             patch.object(deps, "resolve_dependencies") as resolution:
            paths = setup.prepare(self.paths, "v4.34.1", {})
        data = setup.read_setup(paths)
        self.assertEqual(data["phase"], "blocked")
        self.assertEqual(data["pending_tasks"][0]["step"], "toolchain")
        self.assertFalse(data["pending_tasks"][0]["retryable"])
        resolution.assert_not_called()
        self.assertEqual(setup.resume(self.paths).project_root, paths.project_root)

    def test_dependency_failure_becomes_configuration_work_with_forum(self):
        with patch.object(setup, "verify_toolchain", return_value={}), \
             patch.object(deps, "resolve_dependencies", return_value={"status": "blocked", "reason": "update failed"}):
            paths = setup.prepare(self.paths, "v4.34.1", {})
        task = setup.read_setup(paths)["pending_tasks"][0]
        self.assertEqual(task["kind"], "configuration")
        self.assertTrue(task["retryable"])
        self.assertTrue(paths.forum.is_dir())

    def test_reresolution_increments_configuration_revision(self):
        paths = self.prepare()
        with patch.object(setup, "verify_toolchain", return_value={}), \
             patch.object(deps, "resolve_dependencies", return_value={"status": "resolved"}), \
             patch.object(setup, "discover_scope", side_effect=self.fake_scope):
            result = setup.prepare_environment(paths.project_root, VERSION, {})
        self.assertEqual(result["configuration_revision"], 2)

    def test_resume_does_not_require_unchanged_project_bytes_or_old_build(self):
        paths = self.prepare()
        (paths.project_root / "Fixture.lean").write_text("def changed := 1\n")
        self.assertEqual(setup.resume(self.paths).project_root, paths.project_root)

    def test_resume_rejects_origin_or_request_mismatch(self):
        paths = self.prepare()
        with self.assertRaisesRegex(ValueError, "Lean version"):
            setup.resume(self.paths, "v4.35.0")
        with self.assertRaisesRegex(ValueError, "modules"):
            setup.resume(self.paths, modules=["Extra"])
        (paths.unity / "bump-origin.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "origin"):
            setup.resume(self.paths)

    def test_resume_rejects_legacy_schema(self):
        self.prepare()
        path = self.paths.unity / "bump/active.json"
        data = json.loads(path.read_text())
        data["schema"] = "migration-v1"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "compiler migration"):
            setup.resume(self.paths)

    def test_setup_refuses_existing_pointer_and_dirty_source(self):
        self.write("Fixture.lean", "def unrelatedEdit := 1\n")
        with self.assertRaisesRegex(ValueError, "tracked edits"):
            self.prepare()
        self.git("add", "Fixture.lean")
        self.git("commit", "-qm", "user edit")
        self.prepare()
        with self.assertRaisesRegex(ValueError, "existing Bump workspace"):
            self.prepare()

    def test_untracked_source_is_not_silently_omitted_from_target(self):
        self.write("UserWork.lean", "def uncommitted := 10\n")
        with self.assertRaisesRegex(ValueError, "untracked project inputs"):
            self.prepare()


class ManifestTests(SetupFixture):
    def test_git_and_relative_vendored_manifest(self):
        (self.root / "vendor/dep").mkdir(parents=True)
        expected = self.manifest([self.package(), {"name": "local", "type": "path", "dir": "vendor/dep"}])
        self.assertEqual(deps.read_manifest(self.root), expected)

    def test_rejects_escaping_and_symlink_path_dependencies(self):
        for directory in ("../other", str(self.root.parent), "vendor/../../other"):
            with self.subTest(directory=directory):
                self.manifest([{"name": "local", "type": "path", "dir": directory}])
                with self.assertRaises(ValueError):
                    deps.read_manifest(self.root)
        (self.root / "vendor").symlink_to(self.root.parent)
        self.manifest([{"name": "local", "type": "path", "dir": "vendor/project"}])
        with self.assertRaises(ValueError):
            deps.read_manifest(self.root)

    def test_unique_resolved_git_identities(self):
        for packages in ([self.package(), self.package()], [self.package(revision="main")]):
            self.manifest(packages)
            with self.assertRaises(ValueError):
                deps.read_manifest(self.root)

    def test_no_old_manifest_is_required(self):
        self.assertEqual(deps.read_manifest(self.root, required=False), {"packages": []})


class ResolutionTests(SetupFixture):
    def setUp(self):
        super().setUp()
        self.write("lean-toolchain", VERSION + "\n")

    def test_missing_manifest_resolves_exact_dependency_source_before_recursion(self):
        self.requirement()
        def run(root, command, **kwargs):
            if "--tags" in command:
                return completed(command)
            if command[0] == "git":
                return completed(command, B + "\trefs/tags/old\n")
            self.manifest([self.package(revision=B)])
            return completed(command)
        with patch.object(deps, "_run", side_effect=run):
            result = deps.resolve_dependencies(self.root, VERSION, {})
        self.assertEqual(result["dependency_candidates"][0]["revision"], B)

    def test_lake_args_cannot_redirect_project(self):
        for arguments in (["--dir=/unrelated"], ["-Kenv=dev", "-Kenv=other"], ["build"]):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "Lake"):
                deps.checked_lake_args(arguments)

    def test_always_updates_without_pins_and_allows_transitive_set_changes(self):
        self.manifest([self.package("removed")])
        def run(root, command, **kwargs):
            self.assertEqual(command, ["lake", "--keep-toolchain", "-Kenv=dev", "update"])
            self.manifest([self.package("new", B)])
            return completed(command)
        with patch.object(deps, "_run", side_effect=run):
            result = deps.resolve_dependencies(self.root, VERSION, {}, lake_args=["-Kenv=dev"])
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["changes"], {"added": ["new"], "removed": ["removed"], "changed": []})

    def test_selects_exact_release_and_preserves_release_selected_transitives(self):
        self.requirement()
        self.manifest([self.package(), self.package("transitive")])
        def run(root, command, **kwargs):
            if command[0] == "git":
                return completed(command, B + "\trefs/tags/v4.34.1\n")
            self.assertIn('rev = "v4.34.1"', (self.root / "lakefile.toml").read_text())
            self.manifest([self.package(revision=B, inputRev="v4.34.1"), self.package("transitive", C, inputRev="v4.34.0")])
            return completed(command)
        with patch.object(deps, "_run", side_effect=run):
            result = deps.resolve_dependencies(self.root, VERSION, {})
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["dependency_candidates"], [])
        self.assertEqual(result["changes"]["changed"], ["dep", "transitive"])

    def test_missing_release_records_exact_child_candidate_not_transitive_relabel(self):
        self.requirement()
        self.manifest([self.package(), self.package("transitive")])
        with patch.object(deps, "_run", return_value=completed()):
            result = deps.resolve_dependencies(self.root, VERSION, {})
        self.assertEqual(len(result["dependency_candidates"]), 1)
        self.assertEqual(result["dependency_candidates"][0]["revision"], A)
        self.assertEqual(result["dependency_candidates"][0]["name"], "dep")

    def test_explicit_pin_cannot_be_overridden_by_release_selection(self):
        self.requirement()
        self.manifest([self.package()])
        def run(root, command, **kwargs):
            self.assertEqual(command[0], "lake")
            self.assertIn(B, (self.root / "lakefile.toml").read_text())
            self.manifest([self.package(revision=B)])
            return completed(command)
        with patch.object(deps, "_run", side_effect=run):
            result = deps.resolve_dependencies(self.root, VERSION, {"dep": B})
        self.assertEqual(result["selected_releases"], [])

    def test_unfulfilled_explicit_pin_rejected(self):
        self.requirement()
        self.manifest([self.package()])
        with patch.object(deps, "_run", return_value=completed()), self.assertRaisesRegex(ValueError, "explicit pin"):
            deps.resolve_dependencies(self.root, VERSION, {"dep": B})

    def test_update_failure_retains_artifact_not_raw_diagnostic_in_reason(self):
        with patch.object(deps, "_run", return_value=completed(returncode=1, stderr="test-only diagnostic")):
            result = deps.resolve_dependencies(self.root, VERSION, {})
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("test-only diagnostic", result["reason"])
        self.assertTrue(result["log_ref"]["artifact_id"])

    def test_release_lookup_prefers_peeled_annotated_commit(self):
        output = A + "\trefs/tags/v4.34.1\n" + B + "\trefs/tags/v4.34.1^{}\n"
        with patch.object(deps, "_run", return_value=completed(stdout=output)):
            result = deps.discover_compatible_ref(self.root, "https://example.invalid/dep", VERSION)
        self.assertEqual(result["revision"], B)

    def test_release_lookup_rejects_executable_remote_helpers(self):
        with self.assertRaisesRegex(ValueError, "repository"):
            deps.discover_compatible_ref(self.root, "ext::echo bad", VERSION)

    def test_literal_lean_revision_edit_and_path_conversion(self):
        (self.root / "lakefile.toml").unlink()
        self.write("lakefile.lean", 'import Lake\nopen Lake DSL\nrequire dep from git\n  "https://example.invalid/dep" @ "old"\npackage Fixture\n')
        deps.edit_requirement(self.root, "dep", revision="v4.34.1")
        self.assertIn('@ "v4.34.1"', (self.root / "lakefile.lean").read_text())
        (self.root / "vendor/dep").mkdir(parents=True)
        deps.edit_requirement(self.root, "dep", path="vendor/dep")
        self.assertIn('require dep from "vendor/dep"', (self.root / "lakefile.lean").read_text())

    def test_unsupported_computed_requirement_is_not_guessed(self):
        (self.root / "lakefile.toml").unlink()
        self.write("lakefile.lean", 'import Lake\nrequire dep from git getURL @ getVersion\n')
        before = (self.root / "lakefile.lean").read_bytes()
        with self.assertRaisesRegex(ValueError, "nonliteral"):
            deps.edit_requirement(self.root, "dep", revision="v4.34.1")
        self.assertEqual(before, (self.root / "lakefile.lean").read_bytes())

    def test_transaction_holds_one_lock_across_nested_edits_and_update(self):
        events = []
        class Lock:
            def __enter__(self):
                events.append("enter")
            def __exit__(self, *args):
                events.append("exit")
        with patch.object(deps.bump_jobs, "_build_lock", return_value=Lock()):
            with deps.configuration_transaction(self.root):
                events.append("edit")
                with deps.configuration_transaction(self.root):
                    events.append("update")
                events.append("check")
        self.assertEqual(events, ["enter", "edit", "update", "check", "exit"])


class ScopeTests(SetupFixture):
    def test_current_source_files_includes_new_files_but_excludes_runtime_vendor_and_build(self):
        self.write("New.lean", "def added := 1\n")
        self.write("vendor/dep/Dep.lean", "def dependency := 1\n")
        self.write("generated/Ignore.lean", "def generated := 1\n")
        self.write(".unity/Ignore.lean", "def runtime := 1\n")
        self.assertEqual(setup.current_source_files(self.root, "generated"),
                         ["Extra.lean", "Fixture.lean", "New.lean", "Notes.lean"])

    def test_default_plus_supplemental_scope_and_local_import_closure(self):
        layout = {"default_modules": {"Fixture.lean": "Fixture"}, "unknown_default_targets": [], "build_dir": ".lake/build"}
        owned = {"modules": {"Fixture.lean": "Fixture", "Extra.lean": "Extra", "Notes.lean": "Notes"}, "unmatched": []}
        def imports(root, files, **kwargs):
            return {path: ["Extra"] if path == "Fixture.lean" else [] for path in files}
        with patch.object(setup.bump_workspace, "discover", side_effect=[layout, owned]), \
             patch.object(setup.bump_workspace, "read_imports", side_effect=imports):
            scope = setup.discover_scope(self.root, modules=["Extra"])
        self.assertEqual(scope["selected_modules"], {"Extra": "Extra.lean", "Fixture": "Fixture.lean"})
        self.assertEqual(scope["default_modules"], {"Fixture": "Fixture.lean"})
        self.assertIn("Notes.lean", scope["excluded_files"])

    def test_unknown_defaults_need_scope_review(self):
        with patch.object(setup.bump_workspace, "discover", return_value={"default_modules": {}, "unknown_default_targets": ["custom"]}), \
             self.assertRaisesRegex(ValueError, "scope review"):
            setup.discover_scope(self.root)


class ChildDependencyTests(SetupFixture):
    def vendor_fixture(self):
        self.write("LICENSE", "Fixture license\n")
        self.git("add", "LICENSE")
        self.git("commit", "-qm", "license")
        revision = self.git("rev-parse", "HEAD")
        task = deps.dependency_task({}, "dep", str(self.root), revision, VERSION)
        child = deps.materialize_dependency(self.root, task)
        self.requirement()
        return child, {"build_passed": True, "review_approved": True, "verified_commit": revision}

    def test_interrupted_clone_retains_incomplete_stage_and_retries_without_reset(self):
        revision = self.git("rev-parse", "HEAD")
        task = deps.dependency_task({}, "dep", str(self.root), revision, VERSION)
        original = deps._run
        def interrupted(root, command, **kwargs):
            result = original(root, command, **kwargs)
            if command[:2] == ["git", "clone"]:
                raise InterruptedError("interrupted clone")
            return result
        with patch.object(deps, "_run", side_effect=interrupted), self.assertRaises(InterruptedError):
            deps.materialize_dependency(self.root, task)
        parent = self.root / ".unity/bump/dependencies"
        journal_path = parent / (task["task_id"] + ".materialization.json")
        old_stage = parent / json.loads(journal_path.read_text())["staging"]
        self.assertTrue((old_stage / ".git").is_dir())
        target = deps.materialize_dependency(self.root, task)
        self.assertTrue(old_stage.is_dir())
        self.assertEqual(self.git("rev-parse", "HEAD", root=target), revision)
        self.assertEqual(len(json.loads(journal_path.read_text())["attempts"]), 2)

    def test_completed_clone_stage_is_published_on_resume_without_reclone(self):
        revision = self.git("rev-parse", "HEAD")
        task = deps.dependency_task({}, "dep", str(self.root), revision, VERSION)
        original = deps._write_json
        def interrupted(path, value):
            original(path, value)
            if path.name == "dependency-origin.json":
                raise InterruptedError("before clone publication")
        with patch.object(deps, "_write_json", side_effect=interrupted), self.assertRaises(InterruptedError):
            deps.materialize_dependency(self.root, task)
        with patch.object(deps, "_run", wraps=deps._run) as runner:
            target = deps.materialize_dependency(self.root, task)
        self.assertFalse(any(call.args[1][:2] == ["git", "clone"] for call in runner.call_args_list))
        self.assertEqual(self.git("rev-parse", "HEAD", root=target), revision)

    def test_vendor_resume_after_publication_before_requirement_edit(self):
        child, provenance = self.vendor_fixture()
        with patch.object(deps, "edit_requirement", side_effect=InterruptedError("after publish")), \
             self.assertRaises(InterruptedError):
            deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        destination = self.root / "vendor/dep"
        before = deps._tree_inventory(destination)
        record = deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        self.assertEqual(deps._tree_inventory(destination), before)
        self.assertIn('path = "vendor/dep"', (self.root / "lakefile.toml").read_text())
        deps.validate_vendor_publication(self.root, record)

    def test_vendor_resume_after_requirement_edit_before_completion_receipt(self):
        child, provenance = self.vendor_fixture()
        original = deps.edit_requirement
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise InterruptedError("after atomic requirement edit")
        with patch.object(deps, "edit_requirement", side_effect=interrupted), self.assertRaises(InterruptedError):
            deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        before = (self.root / "lakefile.toml").read_bytes()
        with patch.object(deps, "edit_requirement", side_effect=AssertionError("must not rewrite completed require")):
            deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        self.assertEqual((self.root / "lakefile.toml").read_bytes(), before)

    def test_interrupted_vendor_export_is_retained_and_new_stage_published(self):
        child, provenance = self.vendor_fixture()
        def interrupted(tar, path, *args, **kwargs):
            Path(path, "partial").write_text("interrupted export")
            raise InterruptedError("during archive extraction")
        with patch.object(deps.tarfile.TarFile, "extractall", new=interrupted), self.assertRaises(InterruptedError):
            deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        journal_path = self.root / ".unity/bump/vendor-publications/dep.json"
        stage = self.root / ".unity/bump/vendor-staging" / json.loads(journal_path.read_text())["staging"]
        self.assertFalse((self.root / "vendor/dep").exists())
        deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        self.assertTrue((stage / "partial").is_file())
        self.assertEqual(len(json.loads(journal_path.read_text())["attempts"]), 2)

    def test_vendor_reconciliation_rejects_unknown_or_modified_destinations(self):
        child, provenance = self.vendor_fixture()
        destination = self.root / "vendor/dep"
        destination.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "already exists"):
            deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        destination.rmdir()  # Empty directory created only by this test.
        record = deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        (destination / "Fixture.lean").write_text("def changed := 4\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            deps.vendor_dependency(self.root, child, "dep", provenance=provenance)
        with self.assertRaisesRegex(ValueError, "changed"):
            deps.validate_vendor_publication(self.root, record)

    def test_child_deduplication_preserves_attempts_and_multiple_parents(self):
        registry = {}
        parent_a = deps.dependency_task(registry, "a", "https://example.invalid/a", A, VERSION)
        parent_b = deps.dependency_task(registry, "b", "https://example.invalid/b", B, VERSION)
        child = deps.dependency_task(registry, "dep", "https://example.invalid/dep", C, VERSION, parent_id=parent_a["task_id"])
        child["attempts"] = 3
        duplicate = deps.dependency_task(registry, "depAlias", "https://example.invalid/dep", C, VERSION, parent_id=parent_b["task_id"])
        self.assertIs(child, duplicate)
        self.assertEqual(duplicate["attempts"], 3)
        self.assertEqual(len(duplicate["parents"]), 2)

    def test_recursion_cycle_detected_even_across_different_revision(self):
        registry = {}
        first = deps.dependency_task(registry, "a", "https://example.invalid/a", A, VERSION)
        second = deps.dependency_task(registry, "b", "https://example.invalid/b", B, VERSION, parent_id=first["task_id"])
        with self.assertRaisesRegex(ValueError, "cycle"):
            deps.dependency_task(registry, "a", "https://example.invalid/a", C, VERSION, parent_id=second["task_id"])

    def test_materialize_local_repository_exact_revision_and_no_reset_on_reuse(self):
        revision = self.git("rev-parse", "HEAD")
        task = deps.dependency_task({}, "Fixture", str(self.root), revision, VERSION)
        target = deps.materialize_dependency(self.root, task)
        self.assertEqual(self.git("rev-parse", "HEAD", root=target), revision)
        (target / "Fixture.lean").write_text("def inProgress := 5\n")
        self.assertEqual(deps.materialize_dependency(self.root, task), target)
        self.assertIn("inProgress", (target / "Fixture.lean").read_text())

    def test_vendor_exports_licensed_committed_source_without_git_or_private_inputs(self):
        self.write("LICENSE", "Fixture license\n")
        self.git("add", "LICENSE")
        self.git("commit", "-qm", "license")
        revision = self.git("rev-parse", "HEAD")
        task = deps.dependency_task({}, "dep", str(self.root), revision, VERSION)
        child = deps.materialize_dependency(self.root, task)
        self.requirement()
        record = deps.vendor_dependency(self.root, child, "dep", provenance={"build_passed": True, "review_approved": True,
                                                                           "verified_commit": revision})
        vendor = self.root / "vendor/dep"
        self.assertTrue((vendor / "LICENSE").is_file())
        self.assertFalse((vendor / ".git").exists())
        self.assertFalse((vendor / ".unity").exists())
        self.assertIn('path = "vendor/dep"', (self.root / "lakefile.toml").read_text())
        self.assertEqual(record["commit"], revision)

    def test_vendor_requires_exact_build_revision(self):
        self.requirement()
        self.git("add", "lakefile.toml")
        self.git("commit", "-qm", "dependency")
        with self.assertRaisesRegex(ValueError, "successful child build"):
            deps.vendor_dependency(self.root, self.root, "dep", provenance={"build_passed": True, "verified_commit": A})


class ToolchainTests(SetupFixture):
    def test_executes_lean_and_lake_and_checks_requested_version(self):
        binaries = {name: self.root / name for name in ("lean", "lake")}
        def run(root, command, **kwargs):
            name = Path(command[0]).name
            return completed(command, "Lean (version 4.34.1, commit 5045d0056413)" if name == "lean" else "Lake version 5.0.0-src+5045d00")
        with patch.object(setup, "_actual_executable", side_effect=lambda name, version: binaries[name]), \
             patch.object(setup.bump_jobs, "run", side_effect=run) as runner:
            evidence = setup.verify_toolchain(self.root, VERSION)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(evidence["lean"]["executable"], str(binaries["lean"]))

    def test_mismatched_compiler_rejected(self):
        with patch.object(setup, "_actual_executable", side_effect=lambda name, version: self.root / name), \
             patch.object(setup.bump_jobs, "run", side_effect=[completed(stdout="Lean (version 4.28.0, commit aaaaaaa)"),
                                                              completed(stdout="Lake version 5.0.0-src+aaaaaaa")]), \
             self.assertRaisesRegex(ValueError, "requested release"):
            setup.verify_toolchain(self.root, VERSION)

    def test_dangling_elan_registration_is_not_invoked_or_installed(self):
        elan = self.root / ".elan"
        (elan / "bin").mkdir(parents=True)
        (elan / "bin/lean").write_text("test proxy")
        (elan / "toolchains").mkdir()
        (elan / "toolchains/leanprover--lean4---v4.34.1").symlink_to(self.root / "missing")
        with patch.dict(os.environ, {"ELAN_HOME": str(elan)}), \
             patch.object(setup.shutil, "which", side_effect=lambda name: str(elan / "bin" / name)), \
             self.assertRaisesRegex(ValueError, "missing or dangling"):
            setup._actual_executable("lean", VERSION)


class RecursiveWorkflowTests(SetupFixture):
    def environment(self):
        return {"lake_args": [], "dependencies": {"dependency_candidates": [{"name": "dep", "repository": str(self.root),
                    "revision": self.git("rev-parse", "HEAD"), "version": VERSION}]}}

    def prepare_parent(self):
        self.write("LICENSE", "Fixture license\n")
        self.git("add", "LICENSE")
        self.git("commit", "-qm", "license")
        environment = self.environment()
        self.requirement()
        return environment

    def test_child_callback_is_same_workflow_and_success_is_persistently_deduplicated(self):
        environment = self.prepare_parent()
        seen = []
        async def migrate(paths, child_setup, *, ancestors):
            seen.append(paths.project_root)
            self.assertEqual(ancestors, ((str(self.root), VERSION),))
            self.assertEqual(paths.forum, paths.project_root / ".unity/forum/bump")
            return {"build_passed": True, "review_approved": True,
                    "verified_commit": self.git("rev-parse", "HEAD", root=paths.project_root)}
        with patch.object(setup, "prepare_environment", return_value={"phase": "ready", "schema": deps.SCHEMA}):
            changed = asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate))
        self.assertTrue(changed)
        self.assertEqual(len(seen), 1)
        self.assertFalse(asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate)))
        self.assertEqual(len(seen), 1)
        registry = json.loads((self.paths.unity / "bump/dependencies.json").read_text())
        self.assertEqual(next(iter(registry["tasks"].values()))["status"], "verified")

    def test_unapproved_child_never_vendored(self):
        environment = self.prepare_parent()
        async def migrate(paths, child_setup, **kwargs):
            return {"build_passed": True, "review_approved": False,
                    "verified_commit": self.git("rev-parse", "HEAD", root=paths.project_root)}
        with patch.object(setup, "prepare_environment", return_value={"phase": "ready", "schema": deps.SCHEMA}), \
                self.assertRaisesRegex(ValueError, "primary review"):
            asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate))
        self.assertFalse((self.root / "vendor/dep").exists())
        registry = json.loads((self.paths.unity / "bump/dependencies.json").read_text())
        self.assertEqual(next(iter(registry["tasks"].values()))["status"], "blocked")

    def test_interrupted_integration_receipt_reconciles_without_rerunning_child(self):
        environment = self.prepare_parent()
        calls = []
        async def migrate(paths, child_setup, **kwargs):
            calls.append(paths.project_root)
            return {"build_passed": True, "review_approved": True,
                    "verified_commit": self.git("rev-parse", "HEAD", root=paths.project_root)}
        original = deps.vendor_dependency
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise InterruptedError("after publication before registry receipt")
        with patch.object(setup, "prepare_environment", return_value={"phase": "ready", "schema": deps.SCHEMA}), \
             patch.object(deps, "vendor_dependency", side_effect=interrupted), self.assertRaises(InterruptedError):
            asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate))
        self.assertTrue(asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate)))
        self.assertEqual(len(calls), 1)
        self.assertFalse(asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate)))
        (self.root / "vendor/dep/Fixture.lean").write_text("def unreviewed := 17\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate))

    def test_ancestor_cycle_rejected_before_clone_or_model(self):
        environment = self.prepare_parent()
        async def migrate(*args, **kwargs):
            self.fail("cycle must fail before child callback")
        with self.assertRaisesRegex(ValueError, "cycle"):
            asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate,
                                              ancestors=((str(self.root), VERSION),)))

    def test_empty_shared_registry_loads_saved_child_progress_on_continue(self):
        environment = self.prepare_parent()
        async def migrate(paths, child_setup, **kwargs):
            return {"build_passed": True, "review_approved": True,
                    "verified_commit": self.git("rev-parse", "HEAD", root=paths.project_root)}
        with patch.object(setup, "prepare_environment", return_value={"phase": "ready", "schema": deps.SCHEMA}):
            asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate))
        shared = {}
        self.assertFalse(asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate, registry=shared)))
        self.assertEqual(len(shared["tasks"]), 1)

    def test_shared_child_reused_for_different_parent_and_integration_keys(self):
        environment = self.prepare_parent()
        seen = []
        async def migrate(paths, child_setup, **kwargs):
            seen.append(paths.project_root)
            return {"build_passed": True, "review_approved": True,
                    "verified_commit": self.git("rev-parse", "HEAD", root=paths.project_root)}
        shared = {}
        with patch.object(setup, "prepare_environment", return_value={"phase": "ready", "schema": deps.SCHEMA}):
            asyncio.run(recursive.run_children(self.paths, environment, migrate_child=migrate, registry=shared))
        other = self.root.parent / "other"
        other.mkdir()
        (other / "lakefile.toml").write_text((self.root / "lakefile.toml").read_text().replace('path = "vendor/dep"',
                                           'git = "https://example.invalid/dep"\nrev = "old"'))
        other_paths = Paths.from_unity_dir(other / ".unity")
        self.assertTrue(asyncio.run(recursive.run_children(other_paths, environment, migrate_child=migrate, registry=shared)))
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(next(iter(shared["tasks"].values()))["integrations"]), 2)
        self.assertTrue((other / "vendor/dep/LICENSE").is_file())


class InstalledToolchainPreparationTests(SetupFixture):
    def test_real_upgrade_setup_with_broken_source_requires_no_original_build(self):
        chain = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))) / "toolchains/leanprover--lean4---v4.34.1"
        if not all((chain / "bin" / name).is_file() for name in ("lean", "lake", "leanc")):
            self.skipTest("requires already installed Lean 4.34.1; never downloads")
        self.write("lakefile.toml", 'name = "Fixture"\ndefaultTargets = ["Fixture"]\n'
                   '[[lean_lib]]\nname = "Fixture"\n[[lean_lib]]\nname = "Extra"\n')
        self.git("add", "lakefile.toml")
        self.git("commit", "-qm", "explicit scope")
        environment = {"PATH": str(chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                       "LEAN_SYSROOT": str(chain), "LEAN_PATH": "", "UNITY_REAL_LAKE": str(chain / "bin/lake")}
        with patch.dict(os.environ, environment):
            paths = setup.prepare(self.paths, VERSION, {}, modules=["Extra"])
        result = setup.read_setup(paths)
        self.assertEqual(result["phase"], "ready", result.get("pending_tasks"))
        self.assertEqual(result["scope"]["selected_modules"], {"Extra": "Extra.lean", "Fixture": "Fixture.lean"})
        self.assertTrue((paths.project_root / "lake-manifest.json").is_file())
        self.assertFalse((paths.project_root / ".lake/build/lib/lean/Fixture.olean").exists())
        self.assertIn("noSuchOldDeclaration", (paths.project_root / "Fixture.lean").read_text())

    def test_updated_manifest_build_materializes_changed_local_git_dependency(self):
        """Prove whether main can reconcile a candidate manifest without update."""
        chain = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))) / "toolchains/leanprover--lean4---v4.34.1"
        if not all((chain / "bin" / name).is_file() for name in ("lean", "lake", "leanc")):
            self.skipTest("requires already installed Lean 4.34.1; never downloads")
        environment = {**os.environ, "PATH": str(chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                       "LEAN_SYSROOT": str(chain), "LEAN_PATH": ""}
        def lake(root, *arguments):
            result = subprocess.run([str(chain / "bin/lake"), *arguments], cwd=root,
                                    env=environment, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result
        dependency = self.root.parent / "local-dependency"
        dependency.mkdir()
        self.git("init", "-q", root=dependency)
        self.git("config", "user.name", "Test", root=dependency)
        self.git("config", "user.email", "test@example.invalid", root=dependency)
        (dependency / "lean-toolchain").write_text(VERSION + "\n")
        (dependency / "lakefile.toml").write_text('name = "local_dep"\n[[lean_lib]]\nname = "Dep"\n')
        (dependency / "Dep.lean").write_text("def dependencyValue : Nat := 1\n")
        self.git("add", ".", root=dependency)
        self.git("commit", "-qm", "first", root=dependency)
        first = self.git("rev-parse", "HEAD", root=dependency)
        (dependency / "Dep.lean").write_text("def dependencyValue : Nat := 2\n")
        self.git("add", "Dep.lean", root=dependency)
        self.git("commit", "-qm", "second", root=dependency)
        second = self.git("rev-parse", "HEAD", root=dependency)
        config = ('name = "Fixture"\ndefaultTargets = ["Fixture"]\n[[lean_lib]]\nname = "Fixture"\n'
                  '[[require]]\nname = "local_dep"\ngit = ' + json.dumps(str(dependency)) + '\nrev = ' + json.dumps(first) + '\n')
        self.write("lean-toolchain", VERSION + "\n")
        self.write("lakefile.toml", config)
        self.write("Fixture.lean", "import Dep\nexample : dependencyValue = 1 := by rfl\n")
        lake(self.root, "update")
        lake(self.root, "build")
        cached = self.root / ".lake/packages/local_dep"
        self.assertEqual(self.git("rev-parse", "HEAD", root=cached), first)
        candidate = self.root.parent / "isolated-integration"
        candidate.mkdir()
        for name in ("lean-toolchain", "lakefile.toml", "Fixture.lean", "lake-manifest.json"):
            shutil.copyfile(self.root / name, candidate / name)
        (candidate / "lakefile.toml").write_text(config.replace(first, second))
        (candidate / "Fixture.lean").write_text("import Dep\nexample : dependencyValue = 2 := by rfl\n")
        lake(candidate, "update")
        lake(candidate, "build")
        for name in ("lakefile.toml", "Fixture.lean", "lake-manifest.json"):
            shutil.copyfile(candidate / name, self.root / name)
        self.assertEqual(self.git("rev-parse", "HEAD", root=cached), first)
        manifest_before = (self.root / "lake-manifest.json").read_bytes()
        lake(self.root, "build")
        self.assertEqual(self.git("rev-parse", "HEAD", root=cached), second)
        self.assertEqual((self.root / "lake-manifest.json").read_bytes(), manifest_before)


if __name__ == "__main__":
    unittest.main()
