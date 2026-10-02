"""Explicit direct/inherited pin transitions; temporary Git, no network or models."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests import test_bump_project as fixtures
from unity import bump_migration_project as project


class LeanRequirementTests(unittest.TestCase):
    def test_unpinned_literal_single_and_two_line_urls_preserve_surroundings(self):
        for separator in (" ", "\n  "):
            with self.subTest(separator=separator):
                source = ('import Lake\nopen Lake DSL\npackage fixture\n'
                          'require mathlib from git' + separator + '"https://example.invalid/mathlib" -- keep\n'
                          '@[default_target]\nlean_lib Fixture\n')
                updated = project._edit_lean(source, {"mathlib": "v4.34.1"})
                self.assertEqual(updated, source.replace('" -- keep', '" @ "v4.34.1" -- keep'))

    def test_existing_literal_revisions_preserve_single_and_multiline_layout(self):
        for separator in (" ", "\n  "):
            with self.subTest(separator=separator):
                source = ('require mathlib from git\n  "https://example.invalid/mathlib"' + separator +
                          '@ "v4.28.0-rc1" -- old pin\nlean_lib Fixture\n')
                self.assertEqual(project._edit_lean(source, {"mathlib": "a" * 40}),
                                 source.replace('"v4.28.0-rc1"', '"' + "a" * 40 + '"'))

    def test_commented_ambiguous_or_computed_requires_are_not_rewritten(self):
        literal = 'require mathlib from git "https://example.invalid/mathlib"'
        cases = [
            '/-\n' + literal + '\n-/\n', '-- ' + literal + '\n',
            literal + '\n' + literal + '\n',
            '/-\n' + literal + '\n-/\n' + literal + '\n',
            'require mathlib from git repoUrl\n',
            'require mathlib from git ("https://example.invalid/" ++ packageName)\n',
            literal + ' @ revision\n', literal + '\n  @ revision\n',
            literal + '\n  @ "v4.28.0-rc1" ++ suffix\n',
            literal + '\n-- continuation comment\n  @ revision\n',
            literal + '\nwith packageConfig\n', literal + '\n  with packageConfig\n',
            'if enabled then\n  ' + literal + '\n',
            'require mathlib from git\n  /- ambiguous -/ "https://example.invalid/mathlib"\n',
            'require mathlib from git "https://example.invalid/\\"computed"\n',
        ]
        for source in cases:
            with self.subTest(source=source), self.assertRaises(project.ProjectError):
                project._edit_lean(source, {"mathlib": "v4.34.1"})


class DependencyTransitionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.root = fixture, fixture.root
        (self.root / "lakefile.toml").unlink()
        self.source = ('import Lake\nopen Lake DSL\npackage fixture\n'
                       'require mathlib from git\n  "https://example.invalid/mathlib"\n'
                       '@[default_target]\nlean_lib Fixture\n')
        fixture.write("lakefile.lean", self.source)
        self.manifest = {"version": "1.1.0", "packagesDir": ".lake/packages", "packages": [
            {"name": "mathlib", "type": "git", "url": "https://example.invalid/mathlib",
             "rev": "1" * 40, "inputRev": "master", "inherited": False},
            {"name": "support", "type": "git", "url": "https://example.invalid/support",
             "rev": "2" * 40, "inputRev": "v1.0.0", "inherited": True},
            {"name": "untouched", "type": "git", "url": "https://example.invalid/untouched",
             "rev": "3" * 40, "inputRev": "v1.0.0", "inherited": True}]}
        fixture.write("lake-manifest.json", json.dumps(self.manifest))
        fixture.commit()

    def prepare(self, pins=None):
        return project.prepare(self.root, "v4.34.1", pins or {"mathlib": "v4.34.1", "support": "5" * 40},
                               run_id="dependency-transition")

    def resolve(self, baseline, mutate=None):
        target = Path(baseline["target"])
        original_run, calls = project._run, []

        def run(root, command, **kwargs):
            if command[0] != "lake":
                return original_run(root, command, **kwargs)
            calls.append(command)
            manifest = deepcopy(self.manifest)
            manifest["packages"][0].update(rev="4" * 40, inputRev="v4.34.1")
            manifest["packages"][1].update(rev="5" * 40, inputRev="v2.0.0")
            if mutate:
                mutate(manifest, target)
            self.fixture.write("lake-manifest.json", json.dumps(manifest), root=target)
            return subprocess.CompletedProcess(command, 0, "resolved", "")

        with patch.object(project, "_run", side_effect=run):
            receipt = project.resolve_dependencies(target, baseline)
        self.assertEqual(calls, [["lake", "update", "mathlib"]])
        return receipt

    def test_only_direct_config_is_edited_and_exact_inherited_resolution_is_sealed(self):
        original = project.snapshot(self.root)
        baseline = self.prepare()
        target = Path(baseline["target"])
        self.assertEqual((target / "lakefile.lean").read_text(),
                         self.source.replace('"https://example.invalid/mathlib"',
                                             '"https://example.invalid/mathlib" @ "v4.34.1"'))
        self.assertEqual(baseline["dependency_pins"], {"mathlib": "v4.34.1", "support": "5" * 40})
        receipt = self.resolve(baseline)
        self.assertTrue(receipt["passed"], receipt)
        sealed = project.seal_target(target, baseline, resolution=receipt)
        self.assertEqual(project.validate_target(target, sealed), [])
        self.assertEqual(project.snapshot(self.root), original)
        self.assertEqual(sealed["target_manifest"]["packages"][2], self.manifest["packages"][2])

    def test_all_pin_names_are_checked_before_lakefile_rewriting(self):
        with patch.object(project, "_edit_lean", side_effect=AssertionError("must validate pins first")), \
                self.assertRaisesRegex(project.ProjectError, "already exist"):
            self.prepare({"absent": "4" * 40})
        self.assertFalse((self.root / ".unity/bump/dependency-transition").exists())

    def test_inherited_pin_without_direct_or_with_floating_tag_is_rejected(self):
        for pins, message in (({"support": "5" * 40}, "at least one explicit direct"),
                              ({"mathlib": "v4.34.1", "support": "v2.0.0"}, "exact full commit")):
            with self.subTest(pins=pins), self.assertRaisesRegex(project.ProjectError, message):
                self.prepare(pins)

    def test_ambiguous_manifest_inheritance_is_rejected(self):
        manifest = deepcopy(self.manifest)
        manifest["packages"][1]["inherited"] = "true"
        with self.assertRaisesRegex(project.ProjectError, "ambiguous inherited"):
            project._partition_dependency_pins(manifest, {"mathlib": "v4.34.1", "support": "5" * 40})

    def test_toml_direct_requirements_use_the_same_inherited_partition(self):
        direct, inherited = project._partition_dependency_pins(self.manifest,
                                                              {"mathlib": "v4.34.1", "support": "5" * 40})
        source = 'name = "fixture"\n[[require]]\nname = "mathlib"\ngit = "https://example.invalid/mathlib"\n'
        updated = project._edit_toml(source, direct)
        self.assertIn('rev = "v4.34.1"', updated)
        self.assertNotIn("support", updated)
        self.assertEqual(inherited, {"support": "5" * 40})

    def test_wrong_expected_inherited_commit_is_rejected(self):
        baseline = self.prepare()
        receipt = self.resolve(baseline, lambda manifest, target: manifest["packages"][1].update(rev="6" * 40))
        self.assertFalse(receipt["passed"])
        self.assertIn("Requested dependency commit mismatch: support", receipt["errors"])

    def test_unrequested_transitive_change_is_rejected(self):
        baseline = self.prepare()
        receipt = self.resolve(baseline, lambda manifest, target: manifest["packages"][2].update(rev="6" * 40))
        self.assertFalse(receipt["passed"])
        self.assertIn("Unrequested dependency changed: untouched", receipt["errors"])

    def test_requested_dependency_source_metadata_cannot_change(self):
        baseline = self.prepare()
        receipt = self.resolve(baseline, lambda manifest, target: manifest["packages"][1].update(url="https://other.invalid/source"))
        self.assertFalse(receipt["passed"])
        self.assertIn("Requested dependency changed unrelated metadata: support", receipt["errors"])

    def test_resolver_cannot_add_or_remove_dependencies(self):
        baseline = self.prepare()
        receipt = self.resolve(baseline, lambda manifest, target: manifest["packages"].pop())
        self.assertFalse(receipt["passed"])
        self.assertTrue(any("Dependency set changed" in error for error in receipt["errors"]))

    def test_resolver_cannot_edit_project_proofs(self):
        baseline = self.prepare()
        receipt = self.resolve(baseline, lambda manifest, target:
                               self.fixture.write("Fixture/Basic.lean", "axiom Fixture.basic : True\n", root=target))
        self.assertFalse(receipt["passed"])
        self.assertIn("Dependency resolver changed project source or non-manifest configuration", receipt["errors"])

    def test_preexisting_target_proof_edit_blocks_resolver_dispatch(self):
        baseline = self.prepare()
        target = Path(baseline["target"])
        self.fixture.write("Fixture/Basic.lean", "axiom Fixture.basic : True\n", root=target)
        with self.assertRaisesRegex(project.ProjectError, "source changed before"):
            project.resolve_dependencies(target, baseline)


if __name__ == "__main__":
    unittest.main()
