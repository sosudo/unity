"""Temporary real Lean fixtures only; no downloads, models or evaluation runs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_inventory, bump_diagnostics, bump_planner, bump_migration_project
from tests import test_bump_occurrence_native as native_fixtures


class BumpInventoryNativeTests(unittest.TestCase):
    environment = staticmethod(native_fixtures.BumpOccurrenceNativeTests.environment)
    project = native_fixtures.BumpOccurrenceNativeTests.project
    command = native_fixtures.BumpOccurrenceNativeTests.command

    @classmethod
    def setUpClass(cls):
        elan = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
        cls.chains = [elan / "toolchains" / ("leanprover--lean4---v" + version)
                      for version in ("4.28.0-rc1", "4.34.1")]
        if not all((chain / "bin" / name).is_file() for chain in cls.chains for name in ("lean", "lake", "leanc")):
            raise unittest.SkipTest("Bump slim inventory requires installed 4.28.0-rc1 and 4.34.1; no downloads")
        directory = tempfile.TemporaryDirectory(prefix="bump-slim-native-")
        cls.addClassCleanup(directory.cleanup)
        cls.directory, cls.binaries = Path(directory.name), {}
        source = Path(bump_inventory.__file__).with_suffix(".lean")
        for index, chain in enumerate(cls.chains):
            generated, binary = cls.directory / f"inventory-{index}.c", cls.directory / f"inventory-{index}"
            for command in ([str(chain / "bin/lean"), "-R", str(source.parent), "-c", str(generated), str(source)],
                            [str(chain / "bin/leanc"), "-o", str(binary), str(generated), "-rdynamic"]):
                result = subprocess.run(command, cwd=cls.directory, env=cls.environment(chain),
                                        capture_output=True, text=True, timeout=240)
                if result.returncode:
                    raise AssertionError(result.stdout + result.stderr)
            cls.binaries[chain] = binary

    def inspect(self, chain, root, module, local=False):
        built = self.command(chain, root, "build", "+" + module)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        args = ["env", str(self.binaries[chain]), module] + (["--local-meanings"] if local else [])
        result = self.command(chain, root, *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["raw_declaration_count"], len(report["declarations"]))
        return report

    def test_raw_same_name_inventory_is_separate_and_default_has_no_expressions(self):
        for chain in self.chains:
            fixture = native_fixtures.BumpOccurrenceNativeTests
            root = self.project(chain, {"Foundation": fixture.foundation, "Left": fixture.left, "Right": fixture.right,
                "Together": "prelude\nimport Left\nimport Right\n"})
            reports = {module: self.inspect(chain, root, module) for module in ("Foundation", "Left", "Right", "Together")}
            left = next(row for row in reports["Left"]["declarations"] if row["display_name"] == "shared")
            right = next(row for row in reports["Right"]["declarations"] if row["display_name"] == "shared")
            self.assertEqual(left["name_ast"], right["name_ast"])
            self.assertNotEqual(bump_inventory.occurrence_id("Left", left["name_ast"]),
                                bump_inventory.occurrence_id("Right", right["name_ast"]))
            self.assertEqual(reports["Together"]["declarations"], [])
            for report in reports.values():
                for row in report["declarations"]:
                    self.assertNotIn("meaning", row)
                    self.assertNotIn("axioms", row)
                    self.assertNotIn("type", row)

    def test_lazy_local_meanings_preserve_individual_trust_without_external_ast_closure(self):
        for chain in self.chains:
            fixture = native_fixtures.BumpOccurrenceNativeTests
            root = self.project(chain, {"Foundation": fixture.foundation, "Left": fixture.left, "Right": fixture.right})
            for module, expected in (("Left", "trustA"), ("Right", "trustB")):
                report = self.inspect(chain, root, module, local=True)
                row = next(row for row in report["declarations"] if row["display_name"] == "shared")
                self.assertEqual([axiom["display_name"] for axiom in row["axioms"]], [expected])
                self.assertIn("type", row["axioms"][0])
                self.assertIn("meaning", row)
                self.assertNotIn("meanings", report)
                self.assertTrue(all(entry["display_name"] not in {"trustA", "trustB"} for entry in report["declarations"]))

    def test_generated_same_display_aliases_survive_as_typed_array_rows(self):
        body = '''import Lean
run_elab do
  let names : List Lean.Name := [.str .anonymous "a.b»", .str (.str .anonymous "a") "b»"]
  for name in names do
    Lean.addDecl (.axiomDecl { name := name, levelParams := [], type := Lean.mkConst ``True, isUnsafe := false })
'''
        for chain in self.chains:
            report = self.inspect(chain, self.project(chain, {"Fixture": body}), "Fixture")
            rows = [row for row in report["declarations"] if "b»" in row["display_name"]]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["display_name"], rows[1]["display_name"])
            self.assertNotEqual(rows[0]["name_ast"], rows[1]["name_ast"])

    def test_native_target_imports_order_failing_bodies_and_discover_header_repairs(self):
        project = bump_migration_project
        for chain in self.chains:
            with self.subTest(toolchain=chain.name):
                root = self.project(chain, {"A": "theorem a : True := by trivial\n",
                                            "B": "theorem b : True := by trivial\n"})
                for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                             ("config", "user.name", "Bump fixture"), ("add", "."), ("commit", "-qm", "fixture")):
                    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                reports = {module: self.inspect(chain, root, module) for module in ("A", "B")}
                with patch.dict(os.environ, self.environment(chain)):
                    files = project.source_files(root)
                    scope = project._scope_seal({"version": 1, "mode": "all", "kind": "all_project_modules",
                        "default_build_required": True, "selected_modules": {"A": "A.lean", "B": "B.lean"},
                        "excluded_modules": {}, "excluded_files": {name: sha for name, sha in files.items()
                            if name not in project._CONFIG and name not in {"A.lean", "B.lean"}},
                        "native_default_modules": {}, "native_metadata": {}})
                    graph = {module: {"path": module + ".lean", "imports": []} for module in ("A", "B")}
                    index = bump_inventory.assemble_index(reports, graph, files,
                        scope_sha256=scope["sha256"], environment={})
                    (root / "A.lean").write_text("import B\ntheorem a : True := unknownA\n")
                    (root / "B.lean").write_text("theorem b : True := unknownB\n")
                    receipt = bump_diagnostics.collect_build_diagnostics(root, index,
                        artifact_dir=root / ".unity/artifacts", scope=scope)
                    self.assertEqual(receipt["target_imports"]["modules"]["A"]["imports"], ["B"])
                    plan = bump_planner.plan_repairs(index, receipt)
                    self.assertEqual(plan["tasks"]["A"]["blocked_by"], ["B"])
                    self.assertEqual(plan["tasks"]["B"]["status"], "repair")
                    self.assertNotIn("A", {row["module"] for row in receipt["module_checks"]})
                    (root / "A.lean").write_text("import\n")
                    (root / "B.lean").write_text("theorem b : True := by trivial\n")
                    refreshed = bump_diagnostics.collect_build_diagnostics(root, index,
                        artifact_dir=root / ".unity/artifacts", scope=scope)
                    self.assertEqual(refreshed["target_imports"]["modules"]["A"]["unavailable_reason"], "header_syntax")
                    repairs = bump_planner.plan_repairs(index, refreshed, prior_plan=plan)
                    self.assertEqual(repairs["tasks"]["A"]["status"], "repair")
                    self.assertTrue(any(row["kind"] in {"module", "syntax", "import"} and row["diagnostic_ids"]
                                        for row in repairs["tasks"]["A"]["declaration_subtasks"]))


if __name__ == "__main__":
    unittest.main()
