"""Real Lean regressions for change-focused preservation; no agents/services.

Fixtures are dependency-free, temporary Git projects. Only already installed
Lean toolchains are used: UNITY_TEST_LEAN_TOOLCHAIN can name one explicitly.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import formalize_contract as contract
from unity import formalize_project as project


class InstalledLeanDeltaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        explicit = os.environ.get("UNITY_TEST_LEAN_TOOLCHAIN")
        elan = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
        candidates = ([Path(explicit)] if explicit else [
            elan / "toolchains" / ("leanprover--lean4---v" + version)
            for version in ("4.34.1", "4.34.0", "4.33.1", "4.28.0")])
        cls.chain = next((candidate.resolve() for candidate in candidates
                          if all((candidate / "bin" / binary).is_file()
                                 for binary in ("lean", "leanc", "lake"))), None)
        if cls.chain is None:
            raise unittest.SkipTest("requires an already installed Lean; never downloads")
        directory = tempfile.TemporaryDirectory(prefix="unity-formalize-delta-native-")
        cls.addClassCleanup(directory.cleanup)
        cls.directory = Path(directory.name).resolve()
        cls.environment = {**os.environ,
                           "PATH": str(cls.chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                           "LEAN_SYSROOT": str(cls.chain), "LEAN_PATH": "", "UNITY_AGENT_NAME": ""}
        cls.toolchain = "leanprover/lean4:v" + cls.chain.name.rsplit("---v", 1)[-1]
        cls.binaries = {}
        for name in ("workspace", "contract"):
            source = Path(contract.__file__).with_name("formalize_" + name + ".lean")
            generated, executable = cls.directory / (name + ".c"), cls.directory / name
            commands = ([str(cls.chain / "bin/lean"), "-R", str(source.parent), "-c", str(generated), str(source)],
                        [str(cls.chain / "bin/leanc"), "-o", str(executable), str(generated),
                         *(["-lLake"] if name == "workspace" else []), "-rdynamic"])
            for command in commands:
                result = subprocess.run(command, cwd=cls.directory, env=cls.environment,
                                        capture_output=True, text=True, timeout=240)
                if result.returncode:
                    raise AssertionError(result.stdout + result.stderr)
            cls.binaries[name] = executable

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.directory)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.commands = []
        environment = patch.dict(os.environ, self.environment)
        environment.start()
        self.addCleanup(environment.stop)

        def native_executable(root, source, *, name, **kwargs):
            return self.binaries[name]

        def bounded_native_job(root, command, *, cwd, **kwargs):
            self.commands.append(list(command))
            return subprocess.run(command, cwd=cwd, env=self.environment,
                                  capture_output=True, text=True, timeout=180)

        for mocked in (patch.object(contract.formalize_native, "executable", side_effect=native_executable),
                       patch.object(contract.formalize_jobs, "run", side_effect=bounded_native_job)):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.write("lean-toolchain", self.toolchain + "\n")
        self.write("lakefile.toml", 'name = "delta_fixture"\ndefaultTargets = ["Fixture"]\n'
                   '[[lean_lib]]\nname = "Fixture"\n'
                   '[[lean_lib]]\nname = "Alpha"\n'
                   '[[lean_exe]]\nname = "brokenTool"\nroot = "tools.Broken"\n')
        self.write(".gitignore", ".lake/\n.unity/\n.worktrees/\n")
        self.write("Fixture.lean", "import Fixture.Base\nimport Fixture.Dependent\n")
        self.base = ("def sharedValue : Nat := 1\n"
                     "theorem oldHole : True := by sorry\n"
                     "theorem selectedHole (n : Nat) : n = n := by sorry\n"
                     "theorem existingProof : True := True.intro\n")
        self.write("Fixture/Base.lean", self.base)
        self.write("Fixture/Dependent.lean", "import Fixture.Base\ndef dependentValue := sharedValue\n")
        self.write("Alpha.lean", "def sharedValue : Nat := 2\n")
        self.write("tools/Broken.lean", "import Fixture\nthis is deliberately invalid Lean\n")
        # No dependency requests exist. This creates only an empty Lake lockfile.
        self.command([str(self.chain / "bin/lake"), "update"])
        for args in (["init", "-q", "--initial-branch=main"], ["config", "user.email", "fixture@example.invalid"],
                     ["config", "user.name", "Delta Fixture"], ["config", "commit.gpgsign", "false"],
                     ["add", "."], ["commit", "-qm", "immutable fixture baseline"]):
            self.command(["git", *args])

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def command(self, args, *, good=True):
        result = subprocess.run(args, cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=180)
        if good:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def recommit_fixture(self):
        self.command(["git", "add", "."])
        self.command(["git", "commit", "-qm", "specialized immutable test fixture"])

    def baseline(self, *, existing=(), outputs=()):
        captured = project.capture_baseline(self.root, "Add bounded new declarations; preserve existing content.",
                                            project_scope="changes")
        dag = {"existing_targets": list(existing),
               "chunks": [{"id": "proof", "outputs": [
                   {"declaration": row["declaration"], "lean_file": row["file"]} for row in outputs]}]}
        return project.bind_scope(captured, dag, root=self.root)

    def build(self, baseline, *, good=True):
        result = contract.build_sources(self.root, full=True, baseline=baseline)
        if good:
            self.assertEqual(result["returncode"], 0, result["output"])
        return result

    def check(self, baseline, outputs):
        spec = {"prerequisites": []}
        initial = contract._seal_contract({"version": 3, "fingerprint_version": 2, "inspection_policy": 2,
            "solution_candidate": "source", "solution_sha256": "source-hash", "requirements": [],
            "spec": spec, "spec_sha256": contract.digest(spec), "environment": baseline["environment"],
            "source_main_sha": baseline["head"], "obligation_ids": ["proof"], "bindings": {}, "targets": {},
            "external_declarations": {}, "project_baseline": baseline})
        self.build(baseline)
        return contract.check_formal_contract(self.root, initial, [{"id": "proof", "task_id": "proof"}],
            completed=set(), task_id="proof", proposed_outputs=outputs, final=True)

    def test_baseline_builds_defaults_without_semantically_auditing_existing_modules(self):
        with patch.object(contract, "inspect_environment", side_effect=AssertionError("startup inventory forbidden")):
            baseline = project.capture_baseline(self.root, "Add a new bounded theorem.", project_scope="changes")
        self.assertEqual(baseline["version"], 2)
        self.assertEqual(baseline["declarations"], {})
        self.assertIn("tools/Broken.lean", baseline["files"])
        self.assertIn("Alpha.lean", baseline["files"])
        self.assertEqual(self.command(["git", "status", "--porcelain"]).stdout, "")
        self.assertFalse((self.root / ".lake/build/lib/lean/Alpha.olean").exists())
        self.assertFalse((self.root / ".lake/build/lib/lean/tools/Broken.olean").exists())

    def test_disjoint_library_duplicate_names_work_in_separate_native_contexts(self):
        baseline = self.baseline()
        self.command([str(self.chain / "bin/lake"), "build", "Alpha"])
        layout = contract.scoped_layout(self.root, contract.workspace_layout(self.root), baseline)
        first = contract.inspect_module_context(self.root, "Fixture.Base", layout=layout, inventory_only=True)
        second = contract.inspect_module_context(self.root, "Alpha", layout=layout, inventory_only=True)
        self.assertNotEqual(first["project_records"]["sharedValue"]["proof_body"],
                            second["project_records"]["sharedValue"]["proof_body"])

    def test_untouched_optional_malformed_import_header_does_not_block_startup_or_new_output(self):
        self.write("tools/Broken.lean", "import\n")
        self.recommit_fixture()
        outputs = [{"declaration": "newResult", "file": "Fixture/New.lean"}]
        baseline = self.baseline(outputs=outputs)
        self.write("Fixture/New.lean", "import Fixture.Base\ntheorem newResult : True := True.intro\n")
        result = self.check(baseline, outputs)
        self.assertTrue(result["passed"], result)
        self.assertEqual((self.root / "tools/Broken.lean").read_text(), "import\n")
        self.assertFalse((self.root / ".lake/build/lib/lean/tools/Broken.olean").exists())

    def test_new_file_verified_without_rejecting_unrelated_preexisting_hole(self):
        outputs = [{"declaration": "newResult", "file": "Fixture/New.lean"}]
        baseline = self.baseline(outputs=outputs)
        self.write("Fixture/New.lean", "import Fixture.Base\ntheorem newResult (n : Nat) : n = n := by rfl\n")
        result = self.check(baseline, outputs)
        self.assertTrue(result["passed"], result)
        self.assertEqual(set(result["verified_targets"]), {"newResult"})

    def test_append_preserves_original_declarations(self):
        outputs = [{"declaration": "addedResult", "file": "Fixture/Base.lean"}]
        baseline = self.baseline(outputs=outputs)
        self.write("Fixture/Base.lean", self.base + "theorem addedResult : True := True.intro\n")
        result = self.check(baseline, outputs)
        self.assertTrue(result["passed"], result)

    def test_selected_hole_completion_preserves_statement(self):
        outputs = [{"declaration": "selectedHole", "file": "Fixture/Base.lean"}]
        baseline = self.baseline(existing=["selectedHole"], outputs=outputs)
        # Planner refinements may omit their earlier file/output prediction.
        # Once bound, the immutable original context and selected names remain.
        rebound = project.bind_scope(baseline, {"existing_targets": ["selectedHole"],
                                              "chunks": [{"id": "proof"}]}, root=self.root)
        self.assertEqual(rebound, baseline)
        self.write("Fixture/Base.lean", self.base.replace("n = n := by sorry", "n = n := by rfl"))
        result = self.check(baseline, outputs)
        self.assertTrue(result["passed"], result)

    def test_selected_hole_statement_weakening_rejected(self):
        outputs = [{"declaration": "selectedHole", "file": "Fixture/Base.lean"}]
        baseline = self.baseline(existing=["selectedHole"], outputs=outputs)
        self.write("Fixture/Base.lean", self.base.replace("n = n := by sorry", "True := True.intro"))
        result = self.check(baseline, outputs)
        self.assertFalse(result["passed"])
        self.assertTrue(any("signature" in issue or "statement" in issue or "protected existing source command" in issue
                            for issue in result["issues"]), result)

    def test_new_proof_depending_on_old_hole_fails_forbidden_axiom_gate(self):
        outputs = [{"declaration": "badResult", "file": "Fixture/New.lean"}]
        baseline = self.baseline(outputs=outputs)
        self.write("Fixture/New.lean", "import Fixture.Base\ntheorem badResult : True := oldHole\n")
        result = self.check(baseline, outputs)
        self.assertFalse(result["passed"])
        self.assertTrue(any("forbidden axioms" in issue for issue in result["issues"]), result)

    def test_existing_definition_rewrite_fails_before_changed_configuration_can_execute(self):
        baseline = self.baseline()
        self.write("Fixture/Base.lean", self.base.replace("sharedValue : Nat := 1", "sharedValue : Nat := 9"))
        with patch.object(contract, "workspace_layout", side_effect=AssertionError("must reject before Lake")):
            errors = project.validate_baseline(self.root, baseline, final=True,
                                                allowed_new_paths={"Fixture/Base.lean"})
        self.assertTrue(errors)

    def test_configuration_and_symlink_tampering_rejected_before_native_jobs(self):
        baseline = self.baseline()
        original = (self.root / "lakefile.toml").read_text()
        self.write("lakefile.toml", original + "\n# unauthorized configuration change\n")
        with patch.object(contract, "workspace_layout", side_effect=AssertionError("must reject before Lake")):
            self.assertTrue(project.validate_baseline(self.root, baseline, final=True))
        self.write("lakefile.toml", original)
        (self.root / "Fixture/Link.lean").symlink_to(self.root / "Fixture/Base.lean")
        with patch.object(contract, "workspace_layout", side_effect=AssertionError("must reject before Lake")):
            self.assertTrue(project.validate_baseline(self.root, baseline, final=True,
                                                       allowed_new_paths={"Fixture/Link.lean"}))

    def test_added_import_of_conflicting_library_fails_in_actual_output_environment(self):
        baseline = self.baseline()
        self.write("Fixture/New.lean", "import Fixture.Base\nimport Alpha\ntheorem newResult : True := True.intro\n")
        result = self.build(baseline, good=False)
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("sharedValue", result["output"])

    def test_append_instance_rechecks_unchanged_downstream_elaboration(self):
        self.write("Fixture/Base.lean", self.base + "class Pick where\n  value : Nat\n"
                   "instance (priority := 100) lowPick : Pick := ⟨1⟩\n")
        self.write("Fixture/Dependent.lean", "import Fixture.Base\ndef picked : Nat := Pick.value\n")
        self.recommit_fixture()
        baseline = self.baseline()
        original = (self.root / "Fixture/Base.lean").read_text()
        self.write("Fixture/Base.lean", original + "instance (priority := 200) highPick : Pick := ⟨2⟩\n")
        self.build(baseline)
        errors = project.validate_baseline(self.root, baseline, final=True,
                                            allowed_new_paths={"Fixture/Base.lean"})
        self.assertTrue(any("picked" in error for error in errors), errors)

    def test_pinned_path_dependency_source_mutation_rejected_before_lake(self):
        dependency = self.root.parent / (self.root.name + "-dependency")
        dependency.mkdir()
        (dependency / "lakefile.toml").write_text('name = "fixture_dep"\n[[lean_lib]]\nname = "FixtureDep"\n')
        (dependency / "FixtureDep.lean").write_text("def dependencyValue : Nat := 1\n")
        original = (self.root / "lakefile.toml").read_text()
        self.write("lakefile.toml", original + '\n[[require]]\nname = "fixture_dep"\npath = "'
                   + str(dependency) + '"\n')
        self.command([str(self.chain / "bin/lake"), "update"])
        self.recommit_fixture()
        baseline = self.baseline()
        (dependency / "FixtureDep.lean").write_text("def dependencyValue : Nat := 9\n")
        with patch.object(contract, "workspace_layout", side_effect=AssertionError("must reject before Lake")):
            errors = project.validate_baseline(self.root, baseline, final=True)
        self.assertTrue(any("dependency" in error for error in errors), errors)

    def test_individually_valid_candidates_conflicting_after_merge_are_rejected(self):
        baseline = self.baseline()
        self.write("Fixture/First.lean", "def claimedName : Nat := 1\n")
        self.build(baseline)
        self.write("Fixture/Second.lean", "def claimedName : Nat := 2\n")
        self.build(baseline)
        self.write("Fixture/Merged.lean", "import Fixture.First\nimport Fixture.Second\n")
        result = self.build(baseline, good=False)
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("claimedName", result["output"])

    def test_stale_compilation_cannot_hide_source_mutation(self):
        outputs = [{"declaration": "newResult", "file": "Fixture/New.lean"}]
        baseline = self.baseline(outputs=outputs)
        self.write("Fixture/New.lean", "theorem newResult : True := True.intro\n")
        self.assertTrue(self.check(baseline, outputs)["passed"])
        self.write("Fixture/Base.lean", self.base.replace("sharedValue : Nat := 1", "sharedValue : Nat := 7"))
        self.assertTrue(project.validate_baseline(self.root, baseline, final=True,
                                                  allowed_new_paths={"Fixture/New.lean"}))


if __name__ == "__main__":
    unittest.main()
