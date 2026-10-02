"""Optional target instrumentation: mocked services, real isolated Git fixtures."""

import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests import test_bump_project as project_fixture
from unity import bump_architect as architect, bump_migration_project as project


class OptionalArchitectTests(unittest.TestCase):
    def setUp(self):
        self.fixture = project_fixture.ProjectTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.migration = project.prepare(self.root, "v4.34.1", {}, run_id="architect-fixture")
        self.target = Path(self.migration["target"])
        self.resolution = project.resolve_dependencies(self.target, self.migration)
        self.before = project.source_files(self.target)
        self.original = project.snapshot(self.root)
        self.revision = "3" * 40
        self.package = {"name": "LeanArchitect", "type": "git", "url": architect.LEANARCHITECT_GIT,
                        "rev": self.revision, "inputRev": self.revision, "inherited": False}
        self.calls = []
        self.real_run = project._run
        self.update_exit = 0
        self.build_exit = 0
        self.extra_package = False
        self.source_effect = False

    def run_mock(self, root, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(argv, 0, self.revision + "\trefs/tags/v4.34.1\n", "")
        if argv == ["lake", "update", "LeanArchitect"]:
            data = project._manifest(self.target)
            data["packages"].append(copy.deepcopy(self.package))
            if self.extra_package:
                data["packages"].append({**self.package, "name": "unexpected"})
            (self.target / "lake-manifest.json").write_text(json.dumps(data))
            if self.source_effect:
                (self.target / "Fixture.lean").write_text("-- unexpected source effect\n")
            return subprocess.CompletedProcess(argv, self.update_exit, "", "")
        if argv == ["lake", "build", "LeanArchitect"]:
            return subprocess.CompletedProcess(argv, self.build_exit, "", "")
        return self.real_run(root, argv, **kwargs)

    def invoke(self, **kwargs):
        with patch.object(project, "_run", side_effect=self.run_mock):
            return architect.prepare_optional_architect(self.target, self.migration, self.resolution, **kwargs)

    def test_disabled_does_not_lookup_or_edit(self):
        migration, resolution, receipt = self.invoke(mode="off")
        self.assertEqual((migration, resolution), (self.migration, self.resolution))
        self.assertEqual(receipt["reason"], "disabled")
        self.assertEqual(project.source_files(self.target), self.before)
        self.assertFalse(any(argv[:2] == ["git", "ls-remote"] for argv in self.calls))

    def test_success_adds_only_exact_matched_package_and_seals(self):
        migration, resolution, receipt = self.invoke()
        self.assertEqual(receipt["status"], "enabled")
        self.assertEqual(migration["auxiliary_dependencies"], {"LeanArchitect": self.package})
        self.assertIn('rev = "' + self.revision + '"', (self.target / "lakefile.toml").read_text())
        self.assertEqual(project.validate_original(self.root, migration), [])
        sealed = project.seal_target(self.target, migration, resolution=resolution)
        self.assertEqual(project.validate_target(self.target, sealed), [])
        self.assertEqual(project.snapshot(self.root), self.original)
        self.assertNotIn(["lake", "update"], self.calls)
        self.assertFalse(any("commit" in argv for argv in self.calls))

    def test_failed_update_restores_exact_configuration(self):
        self.update_exit = 1
        migration, resolution, receipt = self.invoke()
        self.assertEqual(receipt["status"], "skipped")
        self.assertEqual((migration, resolution), (self.migration, self.resolution))
        self.assertEqual(project.source_files(self.target), self.before)

    def test_failed_build_restores_exact_configuration(self):
        self.build_exit = 1
        _, _, receipt = self.invoke()
        self.assertEqual(receipt["reason"], "matching optional package did not build")
        self.assertEqual(project.source_files(self.target), self.before)

    def test_transitive_dependency_expansion_is_rejected_and_restored(self):
        self.extra_package = True
        _, _, receipt = self.invoke()
        self.assertEqual(receipt["status"], "skipped")
        self.assertEqual(project.source_files(self.target), self.before)

    def test_wrong_revision_fails_closed(self):
        self.package["rev"] = "4" * 40
        with self.assertRaisesRegex(ValueError, "exact approved source"):
            self.invoke()
        self.assertEqual(project.source_files(self.target), self.before)

    def test_source_mutation_is_not_disguised_as_optional_skip(self):
        self.source_effect = True
        with self.assertRaisesRegex(ValueError, "changed project source"):
            self.invoke()
        self.assertEqual((self.target / "Fixture.lean").read_text(), "-- unexpected source effect\n")
        self.assertEqual(project._config_hashes(self.target), self.resolution["config"])

    def test_no_matching_tag_skips_without_mutation(self):
        with patch.object(project, "_run", side_effect=lambda root, argv, **kw:
                          subprocess.CompletedProcess(argv, 0, "", "") if argv[:2] == ["git", "ls-remote"]
                          else self.real_run(root, argv, **kw)):
            _, _, receipt = architect.prepare_optional_architect(self.target, self.migration, self.resolution)
        self.assertEqual(receipt["reason"], "matching release unavailable")
        self.assertEqual(project.source_files(self.target), self.before)

    def test_timeout_is_bounded_optional_skip(self):
        with patch.object(project, "_run", side_effect=lambda root, argv, **kw:
                          (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 30))
                          if argv[:2] == ["git", "ls-remote"] else self.real_run(root, argv, **kw)):
            _, _, receipt = architect.prepare_optional_architect(self.target, self.migration, self.resolution)
        self.assertEqual(receipt["reason"], "optional release lookup unavailable")
        self.assertEqual(project.source_files(self.target), self.before)

    def test_sealed_or_stale_resolution_rejected(self):
        with self.assertRaisesRegex(ValueError, "precede target sealing"):
            architect.prepare_optional_architect(self.target, {**self.migration, "target_sealed": True}, self.resolution)
        with self.assertRaisesRegex(ValueError, "exact successful"):
            architect.prepare_optional_architect(self.target, self.migration, {**self.resolution, "baseline_identity": "0" * 64})

    def test_later_auxiliary_pin_mutation_rejected(self):
        migration, resolution, _ = self.invoke()
        sealed = project.seal_target(self.target, migration, resolution=resolution)
        manifest = project._manifest(self.target)
        manifest["packages"][0]["rev"] = "4" * 40
        self.assertIn("Optional dependency identity changed: LeanArchitect", project._pin_errors(manifest, sealed))


if __name__ == "__main__":
    unittest.main()
