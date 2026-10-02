"""Native raw-module occurrence controls on the installed Poly toolchains.

These dependency-free, temporary projects deliberately contain compatible
same-name theorem realizations with different proof assumptions. Nothing is
downloaded, no models are called, and no live evaluation is resumed.
"""
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from unity import bump_migration_contract as contract


class BumpOccurrenceNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        elan = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
        versions = ("4.28.0-rc1", "4.34.1")
        cls.chains = [elan / "toolchains" / ("leanprover--lean4---v" + version)
                      for version in versions]
        if not all((chain / "bin" / name).is_file()
                   for chain in cls.chains for name in ("lean", "lake", "leanc")):
            raise unittest.SkipTest("Bump occurrence fixtures require installed Lean 4.28.0-rc1 and 4.34.1; no downloads")
        directory = tempfile.TemporaryDirectory(prefix="unity-bump-occurrences-")
        cls.addClassCleanup(directory.cleanup)
        cls.directory, cls.binaries = Path(directory.name).resolve(), {}
        source = Path(contract.__file__).with_name("bump_inspect.lean")
        for index, chain in enumerate(cls.chains):
            generated = cls.directory / f"inspector-{index}.c"
            binary = cls.directory / f"inspector-{index}"
            for command in ([str(chain / "bin/lean"), "-R", str(source.parent), "-c", str(generated), str(source)],
                            [str(chain / "bin/leanc"), "-o", str(binary), str(generated), "-rdynamic"]):
                result = subprocess.run(command, cwd=cls.directory, env=cls.environment(chain),
                                        capture_output=True, text=True, timeout=240)
                if result.returncode:
                    raise AssertionError(result.stdout + result.stderr)
            cls.binaries[chain] = binary

    @staticmethod
    def environment(chain):
        return {**os.environ, "PATH": str(chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                "LEAN_SYSROOT": str(chain), "LEAN_PATH": "", "UNITY_AGENT_NAME": ""}

    def project(self, chain, files):
        temporary = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.directory)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "lean-toolchain").write_text("leanprover/lean4:v" + chain.name.rsplit("---v", 1)[-1] + "\n")
        (root / "lakefile.toml").write_text('name = "occurrence_fixture"\n' + "".join(
            f'[[lean_lib]]\nname = "{module}"\n' for module in files))
        for module, body in files.items():
            (root / (module + ".lean")).write_text(body)
        result = self.command(chain, root, "update")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return root

    def command(self, chain, root, *args):
        return subprocess.run([str(chain / "bin/lake"), *args], cwd=root,
                              env=self.environment(chain), capture_output=True, text=True, timeout=120)

    def inspect(self, chain, root, module, owned):
        built = self.command(chain, root, "build", "+" + module)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        inspected = self.command(chain, root, "env", str(self.binaries[chain]), module, "--owned", *owned)
        self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
        report = json.loads(inspected.stdout)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["declaration_inventory"], "raw-module-constants-v1")
        self.assertEqual(report["raw_declaration_count"], len(report["declarations"]))
        self.assertTrue(all(row["module"] == module for row in report["declarations"].values()))
        return report

    def inspect_bound(self, chain, root, module, owned):
        """Use the installed production report/environment boundary too."""
        environment = self.environment(chain)

        def run(root, command, **kwargs):
            return subprocess.run(command, cwd=root, env=environment,
                                  capture_output=True, text=True, timeout=120)

        with patch.dict(os.environ, environment), \
             patch.object(contract.bump_project, "_run", side_effect=run), \
             patch.object(contract, "_native_executable", return_value=self.binaries[chain]):
            return contract.inspect_module(root, module, owned)

    foundation = "prelude\ninductive P : Prop where | mk : P\naxiom trustA : P\naxiom trustB : P\n"
    left = "prelude\nimport Foundation\ntheorem shared : P := trustA\ntheorem leftResult : P := shared\n"
    right = "prelude\nimport Foundation\ntheorem shared : P := trustB\ntheorem rightResult : P := shared\n"

    def sibling_fixture(self, chain, *, right=None):
        files = {"Foundation": self.foundation, "Left": self.left,
                 "Right": self.right if right is None else right,
                 "Forward": "prelude\nimport Left\nimport Right\ntheorem joined : P := shared\n",
                 "Reverse": "prelude\nimport Right\nimport Left\ntheorem joined : P := shared\n"}
        return self.project(chain, files), list(files)

    def test_same_name_sibling_occurrences_keep_separate_proof_trust(self):
        for chain in self.chains:
            root, owned = self.sibling_fixture(chain)
            left = self.inspect(chain, root, "Left", owned)
            right = self.inspect(chain, root, "Right", owned)
            self.assertEqual(left["declarations"]["shared"]["axioms"], ["trustA"])
            self.assertEqual(right["declarations"]["shared"]["axioms"], ["trustB"])
            self.assertEqual(left["declarations"]["leftResult"]["axioms"], ["trustA"])
            self.assertEqual(right["declarations"]["rightResult"]["axioms"], ["trustB"])
            self.assertEqual(left["meanings"]["shared"]["meaning"], right["meanings"]["shared"]["meaning"])
            self.assertEqual(left["meanings"]["shared"]["module"], "Left")
            self.assertEqual(right["meanings"]["shared"]["module"], "Right")

    def test_reversed_import_order_tracks_actual_native_context_not_first_owner(self):
        for chain in self.chains:
            root, owned = self.sibling_fixture(chain)
            forward = self.inspect(chain, root, "Forward", owned)
            reverse = self.inspect(chain, root, "Reverse", owned)
            # Lean imports the later compatible theorem body while retaining
            # the first module's name-to-owner hint. Proof trust must follow
            # actual ConstantInfo, never that unrelated first-owner hint.
            self.assertEqual(forward["declarations"]["joined"]["axioms"], ["trustB"])
            self.assertEqual(reverse["declarations"]["joined"]["axioms"], ["trustA"])
            self.assertNotIn("shared", forward["declarations"])
            self.assertNotIn("shared", reverse["declarations"])
            self.assertEqual(set(forward["declarations"]), {"joined"})
            self.assertEqual(set(reverse["declarations"]), {"joined"})

    def test_deleted_occurrence_is_not_replaced_by_an_imported_namesake(self):
        for chain in self.chains:
            original, owned = self.sibling_fixture(chain)
            before = self.inspect(chain, original, "Right", owned)
            changed, _ = self.sibling_fixture(chain, right="prelude\nimport Left\ntheorem rightResult : P := shared\n")
            after = self.inspect(chain, changed, "Right", owned)
            self.assertIn("shared", before["declarations"])
            self.assertNotIn("shared", after["declarations"])
            self.assertIn("Left", after["imported_modules"])
            self.assertEqual(before["raw_declaration_count"], after["raw_declaration_count"] + 1)
            self.assertEqual(after["declarations"]["rightResult"]["axioms"], ["trustA"])

    def test_one_occurrence_trust_change_is_visible_without_global_union(self):
        for chain in self.chains:
            original, owned = self.sibling_fixture(chain, right=self.right.replace("trustB", "P.mk"))
            before = self.inspect(chain, original, "Right", owned)
            changed, _ = self.sibling_fixture(chain)
            after = self.inspect(chain, changed, "Right", owned)
            self.assertEqual(before["declarations"]["shared"]["axioms"], [])
            self.assertEqual(after["declarations"]["shared"]["axioms"], ["trustB"])
            self.assertEqual(before["declarations"]["rightResult"]["axioms"], [])
            self.assertEqual(after["declarations"]["rightResult"]["axioms"], ["trustB"])
            self.assertEqual(before["meanings"]["shared"]["meaning"], after["meanings"]["shared"]["meaning"])

    def test_conflicting_same_name_definitions_remain_a_native_error(self):
        for chain in self.chains:
            root = self.project(chain, {"Left": "def conflicting : Nat := 0\n",
                "Right": "def conflicting : Nat := 1\n",
                "Together": "prelude\nimport Left\nimport Right\n"})
            result = self.command(chain, root, "build", "+Together")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already", result.stdout + result.stderr)

    def test_actual_generated_congruence_theorem_occurs_in_both_siblings(self):
        generated = """import Foundation
run_elab do
  let result ← Lean.Meta.mkCongrSimpForConst? `subject []
  if result.isNone then throwError "congruence realization was not generated"
"""
        for chain in self.chains:
            root = self.project(chain, {
                "Foundation": "import Lean\ndef subject (n : Nat) : Nat := n\n",
                "Left": generated, "Right": generated,
                "Together": "import Left\nimport Right\n"})
            owned = ["Foundation", "Left", "Right", "Together"]
            left = self.inspect(chain, root, "Left", owned)
            right = self.inspect(chain, root, "Right", owned)
            joined = self.inspect(chain, root, "Together", owned)
            name = "subject.congr_simp"
            self.assertIn(name, left["declarations"])
            self.assertIn(name, right["declarations"])
            self.assertEqual(left["meanings"][name]["meaning"], right["meanings"][name]["meaning"])
            self.assertNotIn(name, joined["declarations"])
            self.assertEqual(joined["raw_declaration_count"], 0)

    def test_new_helper_cannot_borrow_an_original_custom_assumption(self):
        for chain in self.chains:
            files = {"Foundation": self.foundation,
                     "Fixture": "prelude\nimport Foundation\ntheorem prior : P := trustA\n"}
            before = self.inspect_bound(chain, self.project(chain, files), "Fixture", sorted(files))
            files["Fixture"] += "theorem newHelper : P := trustA\n"
            after = self.inspect_bound(chain, self.project(chain, files), "Fixture", sorted(files))
            result = contract.compare_module(before, after)
            self.assertFalse(result["passed"], result)
            self.assertIn("new helper introduces", "\n".join(result["issues"]))

    def test_fake_standard_axiom_names_are_not_standard_kernel_assumptions(self):
        for chain in self.chains:
            for fake in ("propext", "Classical.choice", "Quot.sound"):
                with self.subTest(toolchain=chain.name, fake=fake):
                    files = {"Foundation": "prelude\ninductive P : Prop where | mk : P\n" + f"axiom {fake} : P\n",
                             "Fixture": "prelude\nimport Foundation\n" + f"theorem prior : P := {fake}\n"}
                    before = self.inspect_bound(chain, self.project(chain, files), "Fixture", sorted(files))
                    files["Fixture"] += f"theorem newHelper : P := {fake}\n"
                    after = self.inspect_bound(chain, self.project(chain, files), "Fixture", sorted(files))
                    self.assertEqual(after["meanings"][fake]["module"], "Foundation")
                    result = contract.compare_module(before, after)
                    self.assertFalse(result["passed"], result)
                    self.assertIn("new helper introduces", "\n".join(result["issues"]))

    def test_new_helper_can_use_an_inherited_real_standard_assumption(self):
        for chain in self.chains:
            files = {"Fixture": "theorem prior : True = True := propext Iff.rfl\n"}
            before = self.inspect_bound(chain, self.project(chain, files), "Fixture", sorted(files))
            files["Fixture"] += "theorem newHelper : True = True := propext Iff.rfl\n"
            after = self.inspect_bound(chain, self.project(chain, files), "Fixture", sorted(files))
            self.assertIn("propext", before["declarations"]["prior"]["axioms"])
            result = contract.compare_module(before, after)
            self.assertTrue(result["passed"], result)


def record_existing_poly_inventory(root, scope_path, binary, output):
    """Read-only native inspection of existing compiled Poly modules.

    This diagnostic does not build, resolve packages, change project files, or
    resume an evaluation. Generated raw reports are saved outside the project.
    It is explicitly NOT target-migration or final-acceptance validation.
    """
    root, scope_path, binary, output = map(Path, (root, scope_path, binary, output))
    output.mkdir(parents=True, exist_ok=False)
    scope = json.loads(scope_path.read_text())
    modules = sorted(scope["selected_modules"])
    chain = Path.home() / ".elan/toolchains/leanprover--lean4---v4.28.0-rc1"
    started = datetime.now(timezone.utc).isoformat()
    reports, summaries, occurrences = {}, [], {}
    for module in modules:
        run = subprocess.run([str(chain / "bin/lake"), "env", str(binary), module, "--owned", *modules],
                             cwd=root, env=BumpOccurrenceNativeTests.environment(chain),
                             capture_output=True, text=True, timeout=180)
        if run.returncode:
            raise RuntimeError(f"native inventory failed for {module}: {run.returncode}")
        report = json.loads(run.stdout)
        if (report.get("issues") or report.get("schema_version") != 2
                or report.get("declaration_inventory") != "raw-module-constants-v1"
                or report.get("raw_declaration_count") != len(report["declarations"])):
            raise RuntimeError(f"native occurrence inventory invalid for {module}")
        artifact = output / (module + ".json")
        artifact.write_text(run.stdout)
        reports[module] = report
        summaries.append({"module": module, "declaration_count": len(report["declarations"]),
                          "meaning_count": len(report["meanings"]), "report_sha256": hashlib.sha256(run.stdout.encode()).hexdigest()})
        for name in report["declarations"]:
            occurrences.setdefault(name, []).append(module)
        print(json.dumps({"inspected": module, "raw_occurrences": len(report["declarations"])}), flush=True)
    duplicates = []
    for name, owners in sorted(occurrences.items()):
        if len(owners) < 2:
            continue
        exact_meanings = [reports[owner]["meanings"][name]["meaning"] for owner in owners]
        duplicates.append({"name": name, "modules": owners,
                           "exact_meanings_equal": all(row == exact_meanings[0] for row in exact_meanings),
                           "axioms_by_module": {owner: reports[owner]["declarations"][name]["axioms"] for owner in owners}})
    receipt = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
               "root": str(root), "scope_sha256": hashlib.sha256(scope_path.read_bytes()).hexdigest(),
               "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "selected_module_count": len(modules), "raw_occurrence_count": sum(row["declaration_count"] for row in summaries),
               "unique_display_name_count": len(occurrences), "module_reports": summaries,
               "duplicate_occurrences": duplicates, "builds": 0, "model_calls": 0,
               "proof_submissions": 0, "evaluation_resumed": False, "target_migration_acceptance": False}
    (output / "inventory-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: value for key, value in receipt.items() if key != "module_reports"}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--existing-poly-inventory":
        record_existing_poly_inventory(*sys.argv[2:])
    else:
        unittest.main()
