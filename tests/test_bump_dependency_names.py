"""Lake name serialization regressions: no services, agents, or evaluations."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import bump_contract as contract


class DependencyNameTests(unittest.TestCase):
    CASES = {
        "mathlib": "mathlib",
        "«doc-gen4»": "doc-gen4",
        "foo.bar": "foo.bar",
        "«foo.bar»": "foo.bar",
        "foo.«bar-baz»": "foo.bar-baz",
        "foo.001": "foo.1",
        "«001»": "001",
        "000": "0",
        "« spaced name »": " spaced name ",
        "«a«b»": "a«b",
        "foo.«».bar": "foo..bar",
        "α₂.café": "α₂.café",
        "«中文»": "中文",
    }

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="unity-bump-dependency-name-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.root = self.directory / "project"
        self.root.mkdir()

    def manifest(self, packages, packages_dir=".lake/packages"):
        (self.root / "lake-manifest.json").write_text(json.dumps({
            "packagesDir": packages_dir, "packages": packages,
        }))

    def dependency(self, directory):
        directory.mkdir(parents=True)
        (directory / "Proof.lean").write_text("theorem proof : True := by trivial\n")
        (directory / "input.json").write_text('{"version": 1}\n')
        return directory

    def test_lean_name_components_map_to_one_directory(self):
        for serialized, directory in self.CASES.items():
            with self.subTest(serialized=serialized):
                self.assertEqual(contract._dependency_directory_name(serialized), directory)

    def test_quoted_git_package_uses_unescaped_directory_and_raw_identity_key(self):
        directory = self.dependency(self.root / ".lake/packages/doc-gen4")
        self.manifest([{"name": "«doc-gen4»", "type": "git"}])
        result = contract._dependencies(self.root)
        self.assertEqual(set(result), {"«doc-gen4»"})
        self.assertEqual(result["«doc-gen4»"]["path"], str(directory.resolve()))
        self.assertEqual(result["«doc-gen4»"]["sources"],
                         contract.digest(contract._dependency_file_hashes(directory)))

    def test_quoted_dot_and_hierarchical_names_both_use_literal_dot_directory(self):
        directory = self.dependency(self.root / ".lake/packages/foo.bar")
        for name in ("foo.bar", "«foo.bar»"):
            with self.subTest(name=name):
                self.manifest([{"name": name, "type": "git"}])
                self.assertEqual(contract._dependencies(self.root)[name]["path"],
                                 str(directory.resolve()))

    def test_packages_dir_is_literal_not_a_lean_name(self):
        directory = self.dependency(self.root / "«packages»/doc-gen4")
        self.manifest([{"name": "«doc-gen4»", "type": "git"}], "«packages»")
        self.assertEqual(contract._dependencies(self.root)["«doc-gen4»"]["path"],
                         str(directory.resolve()))

    def test_path_dependency_uses_exact_dir_and_preserves_source_hashing(self):
        directory = self.dependency(self.directory / "«shared-dependency»")
        self.manifest([{"name": "«path-dependency»", "type": "path",
                        "dir": "../«shared-dependency»"}])
        before = contract._dependencies(self.root)
        self.assertEqual(before["«path-dependency»"]["path"], str(directory.resolve()))
        (directory / "Proof.lean").write_text("theorem proof : True := True.intro\n")
        self.assertNotEqual(before, contract._dependencies(self.root))

    def test_escaped_dependency_full_source_bytes_still_bound(self):
        directory = self.dependency(self.root / ".lake/packages/doc-gen4")
        self.manifest([{"name": "«doc-gen4»", "type": "git", "rev": "unchanged-pin"}])
        for name, content in (("Proof.lean", "theorem proof : True := True.intro\n"),
                              ("input.json", '{"version": 2}\n'),
                              ("Untracked.lean", "def newHelper := 3\n")):
            with self.subTest(name=name):
                before = contract._dependencies(self.root)
                (directory / name).write_text(content)
                self.assertNotEqual(before, contract._dependencies(self.root))

    def test_invalid_or_unsafe_names_rejected_before_any_hashing(self):
        invalid = (None, 3, "", "[anonymous]", "doc-gen4", "foo..bar", ".foo", "foo.",
                   "«unterminated", "foo»", "«foo»bar", "«foo»..bar", "1x", "中文", "١",
                   "«»", "«.»", "«..»", "«../escape»", "«/absolute»", "«a/b»", "«a\\b»",
                   "«a\x00b»", "«a\nb»", "«a\x7fb»", "«.git»", "«.lake»")
        for name in invalid:
            with self.subTest(name=name), patch.object(contract, "_dependency_file_hashes") as hashes:
                self.manifest([{"name": "valid", "type": "git"}, {"name": name, "type": "git"}])
                with self.assertRaises(contract.ContractEnvironmentError):
                    contract._dependencies(self.root)
                hashes.assert_not_called()

    def test_colliding_materialized_names_rejected_before_any_hashing(self):
        for first, second in (("foo", "foo"), ("foo", "«foo»"), ("foo.bar", "«foo.bar»"),
                              ("foo.001", "foo.1"), ("«001»", "«001»")):
            with self.subTest(names=(first, second)), patch.object(contract, "_dependency_file_hashes") as hashes:
                self.manifest([{"name": name, "type": "git"} for name in (first, second)])
                with self.assertRaisesRegex(contract.ContractEnvironmentError, "ambiguous"):
                    contract._dependencies(self.root)
                hashes.assert_not_called()

    def test_missing_source_still_fails_closed(self):
        self.manifest([{"name": "«missing-dep»", "type": "git"}])
        with self.assertRaisesRegex(contract.ContractEnvironmentError, "missing dependency source"):
            contract._dependencies(self.root)

    def test_decoder_matches_installed_native_lean_without_loading_project(self):
        # Select an installed toolchain binary directly: never invoke elan's downloader.
        candidates = sorted((Path.home() / ".elan/toolchains").glob("*/bin/lean"))
        if not candidates:
            self.skipTest("No locally installed Lean binary for native serialization oracle")
        oracle = self.directory / "NameOracle.lean"
        oracle.write_text('''import Lean.Data.Json.FromToJson.Basic
def main (args : List String) : IO Unit := do
  for arg in args do
    match (Lean.fromJson? (Lean.Json.str arg) : Except String Lean.Name) with
    | .ok name => IO.println (Lean.Json.str (name.toString (escape := false))).compress
    | .error _ => IO.println "null"
''')
        result = subprocess.run([str(candidates[-1]), "--run", str(oracle), *self.CASES],
                                cwd=self.directory, text=True, capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()],
                         list(self.CASES.values()))


if __name__ == "__main__":
    unittest.main()
