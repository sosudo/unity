"""Real typed-name/collision controls on the exact installed Poly toolchains.

Temporary dependency-free projects only; no downloads, services, or models.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import test_bump_native as native_fixtures
from unity import bump_migration_contract as contract


class BumpNameNativeTests(unittest.TestCase):
    environment = staticmethod(native_fixtures.BumpNativeTests.environment)
    project = native_fixtures.BumpNativeTests.project
    inspect = native_fixtures.BumpNativeTests.inspect

    @classmethod
    def setUpClass(cls):
        elan = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
        cls.chains = [elan / "toolchains" / ("leanprover--lean4---v" + version)
                      for version in ("4.28.0-rc1", "4.34.1")]
        if not all((chain / "bin" / name).is_file()
                   for chain in cls.chains for name in ("lean", "lake", "leanc")):
            raise unittest.SkipTest("Bump typed-name fixtures require installed Lean 4.28.0-rc1 and 4.34.1; no downloads")
        directory = tempfile.TemporaryDirectory(prefix="unity-bump-names-")
        cls.addClassCleanup(directory.cleanup)
        cls.directory, cls.binaries = Path(directory.name).resolve(), {}
        source = Path(contract.__file__).with_name("bump_inspect.lean")
        for index, chain in enumerate(cls.chains):
            generated, binary = cls.directory / f"inspect-{index}.c", cls.directory / f"inspect-{index}"
            commands = ([str(chain / "bin/lean"), "-R", str(source.parent), "-c", str(generated), str(source)],
                        [str(chain / "bin/leanc"), "-o", str(binary), str(generated), "-rdynamic"])
            for command in commands:
                result = subprocess.run(command, cwd=cls.directory, env=cls.environment(chain),
                                        capture_output=True, text=True, timeout=240)
                if result.returncode:
                    raise AssertionError(result.stdout + result.stderr)
            cls.binaries[chain] = binary

    def test_native_printers_preserve_typed_distinctions_and_expose_real_collisions(self):
        script = self.directory / "Names.lean"
        script.write_text('''import Lean
open Lean
private def nameJson : Name → Json
  | .anonymous => toJson (["anonymous"] : List String)
  | .str p s => Json.arr #[toJson "str", nameJson p, toJson s]
  | .num p n => Json.arr #[toJson "num", nameJson p, toJson n]
def main : IO Unit := do
  let names : List Name := [
    .str .anonymous "foo.bar", .str (.str .anonymous "foo") "bar",
    .num (.str .anonymous "foo") 7, .str (.str .anonymous "foo") "7",
    .str (.str .anonymous "CategoryTheory") "_aux_Poly_Bifunctor_Basic___macroRules_CategoryTheory_term_⋙₂__1",
    .str .anonymous "", .str .anonymous "λ₂",
    .str .anonymous "a.b»", .str (.str .anonymous "a") "b»"]
  IO.println <| (Json.arr <| names.toArray.map fun n =>
    Json.mkObj [("name", nameJson n), ("display", toJson n.toString)]).compress
''')
        reports = []
        for chain in self.chains:
            result = subprocess.run([str(chain / "bin/lean"), "--run", str(script)],
                                    cwd=self.directory, env=self.environment(chain),
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertNotEqual(rows[0]["name"], rows[1]["name"])
            self.assertNotEqual(rows[0]["display"], rows[1]["display"])
            self.assertNotEqual(rows[2]["name"], rows[3]["name"])
            self.assertNotEqual(rows[2]["display"], rows[3]["display"])
            self.assertIn("«_aux_", rows[4]["display"])
            self.assertEqual(rows[5]["display"], "«»")
            self.assertNotEqual(rows[7]["name"], rows[8]["name"])
            self.assertEqual(rows[7]["display"], rows[8]["display"])
            reports.append(rows)
        self.assertEqual(reports[0], reports[1])

    def test_quoted_declarations_compare_without_losing_body_type_or_trust_checks(self):
        foundation = "prelude\ninductive P : Prop where | mk : P\ninductive Q : Prop where | mk : Q\naxiom trust : P\n"
        body = ("prelude\nimport Foundation\ndef «foo.bar» : Prop := P\n"
                "namespace foo\ndef bar : Prop := P\nend foo\n"
                "def «⋙₂» : Prop := P\n"
                "theorem result : «⋙₂» := P.mk\ntheorem priorTrust : P := trust\n")
        reports = [self.inspect(self.project(foundation, body, chain)) for chain in self.chains]
        for report in reports:
            self.assertIn("«foo.bar»", report["declarations"])
            self.assertIn("foo.bar", report["declarations"])
            self.assertIn("«⋙₂»", report["declarations"])
            self.assertEqual(contract._report_issues(report), [])
        self.assertTrue(contract.compare_module(reports[0], reports[1])["passed"])
        chain = self.chains[1]
        altered = self.inspect(self.project(foundation, body.replace("def «foo.bar» : Prop := P", "def «foo.bar» : Prop := Q"), chain))
        self.assertFalse(contract.compare_module(reports[0], altered)["passed"])
        changed_type = self.inspect(self.project(foundation, body.replace("result : «⋙₂» := P.mk", "result : Q := Q.mk"), chain))
        self.assertFalse(contract.compare_module(reports[0], changed_type)["passed"])
        expanded = self.inspect(self.project(foundation, body.replace("result : «⋙₂» := P.mk", "result : «⋙₂» := trust"), chain))
        comparison = contract.compare_module(reports[0], expanded)
        self.assertFalse(comparison["passed"])
        self.assertIn("trusted assumptions expanded for result", "\n".join(comparison["issues"]))

    def test_actual_generated_macro_names_are_inventoried_without_filtering(self):
        body = 'import Lean\nnamespace CategoryTheory\nsyntax "⋙₂" : term\nmacro_rules | `(⋙₂) => `(True)\nend CategoryTheory\n'
        for chain in self.chains:
            report = self.inspect(self.project("prelude\n", body, chain))
            names = [name for name in report["declarations"] if "⋙₂" in name]
            self.assertTrue(names, report["declarations"].keys())
            self.assertTrue(any("«" in name for name in names), names)
            self.assertEqual(contract._report_issues(report), [])

    def test_distinct_kernel_names_with_equal_native_display_fail_before_json_collapse(self):
        body = '''import Lean
run_elab do
  let names : List Lean.Name := [
    .str .anonymous "a.b»", .str (.str .anonymous "a") "b»"]
  for name in names do
    Lean.addDecl (.axiomDecl { name := name, levelParams := [], type := Lean.mkConst ``True, isUnsafe := false })
'''
        for chain in self.chains:
            fixture = self.project("prelude\n", body, chain)
            built = subprocess.run([str(chain / "bin/lake"), "build", "+Fixture"],
                                   cwd=fixture[0], env=self.environment(chain),
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            with self.assertRaisesRegex(ValueError, "distinct declarations share native display key"):
                self.inspect(fixture)
            # Also cover collisions reached only through external semantic/
            # axiom closure, with no colliding local declaration labels.
            external = '''import Foundation
run_elab do
  let pairs : List (Lean.Name × Lean.Name) := [
    (`answerOne, .str .anonymous "a.b»"),
    (`answerTwo, .str (.str .anonymous "a") "b»")]
  for (name, witness) in pairs do
    Lean.addDecl (.thmDecl { name := name, levelParams := [], type := Lean.mkConst ``True, value := Lean.mkConst witness })
'''
            with self.assertRaisesRegex(ValueError, "distinct structural names share native display key"):
                self.inspect(self.project(body, external, chain))


if __name__ == "__main__":
    unittest.main()
