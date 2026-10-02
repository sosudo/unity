"""Small real native Bump fixtures; installed Lean only, no services/models.

The inspected projects are isolated temporary dependency-free Lake projects.
Missing installed toolchains skip explicitly; no elan/network install is used.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import bump_migration_contract as contract


class BumpNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        elan = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
        explicit = os.environ.get("UNITY_TEST_LEAN_TOOLCHAIN")
        candidates = ([Path(explicit)] if explicit else [elan / "toolchains" / ("leanprover--lean4---v" + v)
                     for v in ("4.34.1", "4.34.0", "4.33.1", "4.33.0")])
        cls.chains = [p.resolve() for p in candidates
                      if all((p / "bin" / n).is_file() for n in ("lean", "leanc", "lake"))][:2]
        if not cls.chains:
            raise unittest.SkipTest("Bump native tests require installed Lean 4.33/4.34; no downloads")
        directory = tempfile.TemporaryDirectory(prefix="unity-bump-native-")
        cls.addClassCleanup(directory.cleanup)
        cls.directory = Path(directory.name).resolve()
        cls.binaries = {}
        source = Path(contract.__file__).with_name("bump_inspect.lean")
        for index, chain in enumerate(cls.chains):
            generated = cls.directory / f"inspector-{index}.c"
            binary = cls.directory / f"inspector-{index}"
            commands = ([str(chain / "bin/lean"), "-R", str(source.parent), "-c", str(generated), str(source)],
                        [str(chain / "bin/leanc"), "-o", str(binary), str(generated), "-rdynamic"])
            for command in commands:
                result = subprocess.run(command, cwd=cls.directory, env=cls.environment(chain),
                                        capture_output=True, text=True, timeout=240)
                if result.returncode:
                    raise AssertionError("Bump inspector failed on installed toolchain " + str(chain) + "\n" + result.stdout + result.stderr)
            cls.binaries[chain] = binary

    @staticmethod
    def environment(chain):
        return {**os.environ, "PATH": str(chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                "LEAN_SYSROOT": str(chain), "LEAN_PATH": "", "UNITY_REAL_LAKE": str(chain / "bin/lake"),
                "UNITY_AGENT_NAME": ""}

    def project(self, foundation, body, chain=None):
        chain = chain or self.chains[0]
        directory = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.directory)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "lean-toolchain").write_text("leanprover/lean4:v" + chain.name.rsplit("---v", 1)[-1] + "\n")
        (root / "lakefile.toml").write_text('name = "bump_native_fixture"\ndefaultTargets = ["Fixture"]\n'
            '[[lean_lib]]\nname = "Foundation"\n[[lean_lib]]\nname = "Fixture"\n[[lean_lib]]\nname = "Unrelated"\n')
        (root / "Foundation.lean").write_text(foundation)
        (root / "Fixture.lean").write_text(body)
        (root / "Unrelated.lean").write_text("this module is deliberately broken\n")
        result = subprocess.run([str(chain / "bin/lake"), "update"], cwd=root, env=self.environment(chain),
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return root, chain

    def inspect(self, project):
        root, chain = project
        environment = self.environment(chain)

        def bounded_job(root, command, **kwargs):
            return subprocess.run(command, cwd=root, env=environment,
                                  capture_output=True, text=True, timeout=120)

        with patch.dict(os.environ, environment), \
             patch.object(contract.bump_project, "_run", side_effect=bounded_job), \
             patch.object(contract, "_native_executable", return_value=self.binaries[chain]):
            return contract.inspect_module(root, "Fixture", ["Foundation", "Fixture", "Unrelated"])

    foundation = "prelude\ninductive P : Prop where | mk : P\ninductive Q : Prop where | mk : Q\ndef claim : Prop := P\n"
    body = "prelude\nimport Foundation\ntheorem result : claim := P.mk\n"

    def test_original_and_target_contexts_build_without_unrelated_modules(self):
        before = self.inspect(self.project(self.foundation, self.body))
        after_project = self.project(self.foundation, self.body.replace("P.mk", "(fun (p : P) => p) P.mk"))
        after = self.inspect(after_project)
        self.assertTrue(contract.compare_module(before, after)["passed"])
        self.assertFalse((after_project[0] / ".lake/build/lib/lean/Unrelated.olean").exists())
        self.assertIn("claim", before["meanings"])
        self.assertIn("P.rec", before["meanings"])
        self.assertEqual(before["declarations"]["result"]["axioms"], [])

    def test_same_named_external_definition_change_is_rejected_natively(self):
        before = self.inspect(self.project(self.foundation, self.body))
        after = self.inspect(self.project(self.foundation.replace("claim : Prop := P", "claim : Prop := Q"),
                                          self.body.replace("P.mk", "Q.mk")))
        result = contract.compare_module(before, after)
        self.assertFalse(result["passed"])
        self.assertIn("semantic meaning changed: claim", "\n".join(result["issues"]))

    def test_explicit_external_rename_is_verified_natively(self):
        before = self.inspect(self.project(self.foundation, self.body))
        after = self.inspect(self.project(self.foundation.replace("claim", "claimNew"), self.body.replace("claim", "claimNew")))
        self.assertFalse(contract.compare_module(before, after)["passed"])
        result = contract.compare_module(before, after, correspondences={"claim": "claimNew"})
        self.assertTrue(result["passed"], result)

    def test_private_and_generated_declarations_are_not_filtered(self):
        body = self.body + "private def secret : Prop := P\ninductive Local where | unit : Local\n"
        before = self.inspect(self.project(self.foundation, body))
        names = set(before["declarations"])
        self.assertTrue(any(n.startswith("_private.") for n in names), names)
        self.assertIn("Local.rec", names)
        after = self.inspect(self.project(self.foundation, self.body))
        result = contract.compare_module(before, after)
        self.assertFalse(result["passed"])
        self.assertIn("original declaration removed", "\n".join(result["issues"]))

    def test_baseline_axioms_are_preserved_but_per_declaration_expansion_fails(self):
        foundation = self.foundation + "axiom trust : P\n"
        body = "prelude\nimport Foundation\ntheorem first : P := trust\ntheorem second : P := P.mk\n"
        before = self.inspect(self.project(foundation, body))
        self.assertEqual(before["declarations"]["first"]["axioms"], ["trust"])
        self.assertTrue(contract.compare_module(before, before)["passed"])
        after = self.inspect(self.project(foundation, body.replace("second : P := P.mk", "second : P := trust")))
        result = contract.compare_module(before, after)
        self.assertFalse(result["passed"])
        self.assertIn("trusted assumptions expanded for second", "\n".join(result["issues"]))

    def test_two_installed_toolchains_are_inspected_separately(self):
        if len(self.chains) < 2:
            self.skipTest("cross-version native test requires two already installed Lean toolchains")
        before = self.inspect(self.project(self.foundation, self.body, self.chains[1]))
        after = self.inspect(self.project(self.foundation, self.body, self.chains[0]))
        self.assertNotEqual(before["environment"]["lean_version"], after["environment"]["lean_version"])
        self.assertNotEqual(before["executable_sha256"], after["executable_sha256"])
        result = contract.compare_module(before, after)
        self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
