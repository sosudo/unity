"""Existing-project contracts: offline native receipts and temporary Git only."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import bump_contract as contract, bump_input, bump_project as project


def declaration(name, *, axioms=()):
    return {"name": name, "target_kind": "theorem", "module": "Project", "level_params": [],
            "type": ["const", "True"], "meanings": {}, "proof_dependencies": [], "axioms": list(axioms)}


def preserved(name, *, proof="True.intro"):
    return {"name": name, "target_kind": "theorem", "module": "Project", "level_params": [],
            "is_internal_detail": False, "direct_dependencies": [],
            "type": ["const", "True"], "declaration_meaning": {"name": ["str", ["anonymous"], name],
                "module": "Project", "level_params": [], "kind": "theorem", "type": ["const", "True"]},
            "proof_body": ["const", proof]}


class ContextContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="unity-bump-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Project.lean").write_text("theorem target : True := by sorry\n")
        (self.root / "UNITY.md").write_text("# Goal\nPreserve this scope.\n## State\nInitial.\n")
        for args in (["init", "-q", "--initial-branch=main"], ["config", "user.email", "fixture@example.invalid"],
                     ["config", "user.name", "Fixture"], ["add", "."], ["commit", "-qm", "baseline"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        self.head = contract._git(self.root, "rev-parse", "HEAD")
        self.layout = {"modules": {"Project.lean": "Project"}, "build_dir": ".lake/build"}
        self.paths = SimpleNamespace(project_root=self.root, forum=self.root / ".unity/forum/bump",
                                     artifacts=self.root / ".unity/artifacts", unity_md=self.root / "UNITY.md")
        self.baseline = project._seal({"version": 1, "head": self.head, "branch": "main", "files": {
            "Project.lean": hashlib.sha256((self.root / "Project.lean").read_bytes()).hexdigest(),
            "UNITY.md": hashlib.sha256(self.paths.unity_md.read_bytes()).hexdigest()},
            "tracked_files": ["Project.lean", "UNITY.md"], "environment": {}, "layout": self.layout,
            "declarations": {"Project.target": preserved("Project.target", proof="sorryAx")},
            "target_scope": "Project.target", "scope": {"mode": "explicit", "existing_targets": ["Project.target"], "bound": True},
            "project_axioms": [], "project_sorries": ["Project.target"], "project_used_axioms": ["sorryAx"]})
        self.tasks = [{"id": "one", "task_id": "one", "status": "complete"}]
        self.outputs = [{"declaration": "Project.target", "file": "Project.lean"}]
        self.spec = {"version": 1, "prerequisites": []}
        self.current = contract._seal_contract({"version": 3, "fingerprint_version": 2,
            "solution_candidate": "source", "solution_sha256": "source-hash", "requirements": [],
            "spec": self.spec, "spec_sha256": contract.digest(self.spec), "environment": {},
            "source_main_sha": self.head, "obligation_ids": ["one"], "bindings": {}, "targets": {},
            "external_declarations": {}, "project_baseline": deepcopy(self.baseline)})
        self.audit = {"project_axioms": ["Unrelated.assumption"], "project_sorries": ["Unrelated.todo"],
                      "project_used_axioms": ["Unrelated.assumption", "sorryAx", "Lean.ofReduceBool"]}
        self.target = declaration("Project.target")

    def check(self, *, stage="complete", final=False, context_issues=()):
        inspection = {"targets": {self.target["name"]: deepcopy(self.target)},
                      "external_declarations": {}, **self.audit}
        with patch.object(contract, "inspect_environment", return_value=inspection), \
             patch.object(project, "require_pinned_inputs", return_value=None), \
             patch.object(project, "validate_baseline", return_value=list(context_issues)) as validate:
            checked = contract.check_formal_contract(self.root, self.current, self.tasks, completed=set(),
                layout=self.layout, environment={}, proposed_outputs=self.outputs, task_id="one", stage=stage, final=final)
        return checked, validate

    def test_unchanged_unrelated_holes_custom_native_axioms_do_not_block_scoped_completion(self):
        checked, validate = self.check(final=True)
        self.assertTrue(checked["passed"], checked)
        self.assertEqual(checked["proposed_contract"]["project_baseline"], self.baseline)
        self.assertEqual(validate.call_args.kwargs["allowed_new_paths"], {"Project.lean"})
        self.assertEqual(validate.call_args.kwargs["allowed_incomplete_declarations"], set())

    def test_completed_target_closure_still_forbids_each_unsafe_axiom(self):
        for axiom in ("sorryAx", "Unrelated.assumption", "Lean.ofReduceBool", "P._native.decide.ax_1"):
            with self.subTest(axiom=axiom):
                self.target["axioms"] = [axiom]
                checked, _ = self.check(final=True)
                self.assertFalse(checked["passed"])
                self.assertTrue(any(row["code"] == "target_forbidden_axioms" for row in checked["blockers"]))

    def test_representation_holes_are_only_allowed_for_manifest_outputs(self):
        self.target["axioms"] = ["sorryAx"]
        checked, validate = self.check(stage="representation")
        self.assertTrue(checked["passed"], checked)
        self.assertEqual(validate.call_args.kwargs["allowed_incomplete_declarations"], {"Project.target"})

    def test_context_failure_rejects_even_an_independently_correct_target(self):
        checked, _ = self.check(context_issues=["protected existing declaration changed: Project.other"])
        self.assertFalse(checked["passed"])
        self.assertNotIn("proposed_contract", checked)
        self.assertTrue(any(row["code"] == "project_context_changed" for row in checked["blockers"]))

    def test_prerequisite_witness_cannot_borrow_unrelated_preexisting_axiom(self):
        witness = {**declaration("Project.support", axioms=["Unrelated.assumption"]), "signature": "True"}
        changed = deepcopy(self.current)
        changed["spec"]["prerequisites"] = [{"id": "P1", "needed_by": ["one"],
            "resolution": {"kind": "declaration", "declaration": "Project.support"}}]
        changed["spec_sha256"] = contract.digest(changed["spec"])
        changed = contract._seal_contract(changed)
        with patch.object(contract, "inspect_environment", return_value={"targets": {"Project.target": self.target},
                "external_declarations": {}, "prerequisite_declarations": {"Project.support": witness}, **self.audit}), \
             patch.object(project, "require_pinned_inputs", return_value=None), \
             patch.object(project, "validate_baseline", return_value=[]):
            checked = contract.check_formal_contract(self.root, changed, self.tasks, completed=set(),
                layout=self.layout, environment={}, proposed_outputs=self.outputs, task_id="one", final=True)
        self.assertFalse(checked["passed"])
        self.assertTrue(any(row["code"] == "prerequisite_forbidden_axioms" for row in checked["blockers"]))

    def test_final_manifest_must_cover_original_targets_after_graph_replacement(self):
        self.outputs = [{"declaration": "Project.proxy", "file": "Project.lean"}]
        self.target = declaration("Project.proxy")
        checked, _ = self.check(final=True)
        self.assertFalse(checked["passed"])
        self.assertTrue(any("original project targets lack completed adopted outputs: Project.target" in message
                            for message in checked["issues"]))

    def test_representation_reopen_keeps_original_signature_guard(self):
        adopted, _ = self.check(stage="representation")
        reopened, affected = contract.invalidate_bindings(adopted["proposed_contract"], {"one"})
        self.assertEqual(reopened["project_baseline"], self.baseline)
        self.assertEqual(affected, {"one"})
        self.current = reopened
        self.target["type"] = ["const", "False"]
        checked, _ = self.check(context_issues=["existing target signature or namespace/module changed: Project.target"])
        self.assertFalse(checked["passed"])

    def test_plan_requires_capture_then_binds_and_preserves_origin(self):
        source = {"candidate_id": "source", "sha256": "source-hash", "source_refs": []}
        dag = {"solution_candidate": "source", "solution_sha256": "source-hash", "chunks": self.tasks,
               "requirements": [], "spec": self.spec, "existing_targets": ["Project.target"]}
        with patch("unity.bump_state.formal_source", return_value=source), \
             patch.object(contract, "normalize_requirements", return_value=[]), \
             patch.object(contract, "normalize_spec", return_value=self.spec):
            with self.assertRaisesRegex(ValueError, "verified existing-project baseline"):
                contract.prepare_source_contract(self.paths, dag, state={}, environment={}, main_sha=self.head)
            first = contract.prepare_source_contract(self.paths, dag,
                state={"project_baseline": self.baseline}, environment={}, main_sha=self.head)
            again = contract.prepare_source_contract(self.paths, dag,
                state={"project_baseline": self.baseline, "formalization": {"contract": first}},
                environment={}, main_sha=self.head)
        self.assertEqual(first["project_baseline"], again["project_baseline"])
        self.assertTrue(contract._baseline_matches({"project_baseline": self.baseline}, first))
        changed = deepcopy(self.baseline)
        changed["head"] = "different"
        changed = project._seal(changed)
        self.assertFalse(contract._baseline_matches({"project_baseline": changed}, first))

    def test_legacy_scaffold_cannot_checkpoint_or_accept_without_baseline(self):
        with patch.object(contract, "_git", side_effect=AssertionError("must not mutate Git")), \
             patch.object(contract, "build_sources", side_effect=AssertionError("must not build scaffold")):
            with self.assertRaisesRegex(ValueError, "version-3"):
                contract.freeze_formal_contract(self.paths, {})
        with patch.object(contract, "source_identity", return_value={}):
            self.assertFalse(contract.snapshot_is_current(self.paths,
                {"formalization": {}, "formal_tasks": {}}, {"passed": True}))

    def test_resealing_changed_original_context_does_not_rebind_origin(self):
        for field, value in (("files", {}), ("declarations", {}), ("tracked_files", []),
                             ("environment", {"changed": True}), ("head", "0" * 40),
                             ("project_used_axioms", ["newAxiom"])):
            with self.subTest(field=field):
                altered = deepcopy(self.baseline)
                altered["origin_sha256"] = self.baseline["sha256"]
                altered[field] = value
                altered = project._seal(altered)
                self.assertFalse(contract._baseline_matches({"project_baseline": self.baseline},
                                                            {"project_baseline": altered}))
        altered = deepcopy(self.baseline)
        altered["origin_sha256"] = self.baseline["sha256"]
        altered["scope"]["existing_targets"] = []
        self.assertFalse(contract._baseline_matches({"project_baseline": self.baseline},
                                                    {"project_baseline": project._seal(altered)}))

    def test_natural_scope_can_only_select_original_holes_once(self):
        natural = deepcopy(self.baseline)
        natural.update(target_scope="Fill the missing claim")
        natural["scope"] = {"mode": "natural", "existing_targets": [], "bound": False}
        natural = project._seal(natural)
        bound = project.bind_scope(natural, {"existing_targets": ["Project.target"], "chunks": []})
        self.assertTrue(contract._baseline_matches({"project_baseline": natural}, {"project_baseline": bound}))
        changed = deepcopy(bound)
        changed["scope"]["mode"] = "all"
        self.assertFalse(contract._baseline_matches({"project_baseline": natural},
                                                    {"project_baseline": project._seal(changed)}))

    def test_scope_excludes_progress_notes_but_not_instructions(self):
        state = {"problem_sha256": hashlib.sha256(bump_input.scope_bytes(self.paths)).hexdigest()}
        self.assertTrue(contract._problem_matches(self.paths, state))
        self.paths.unity_md.write_text("# Goal\nPreserve this scope.\n## State\nUpdated progress.\n")
        self.assertTrue(contract._problem_matches(self.paths, state))
        self.paths.unity_md.write_text("# Goal\nWeakened scope.\n## State\nUpdated progress.\n")
        self.assertFalse(contract._problem_matches(self.paths, state))

    def test_final_snapshot_binds_baseline_and_cannot_accept_missing_context(self):
        checked, _ = self.check(final=True)
        adopted = checked["proposed_contract"]
        state = {"project_baseline": self.baseline,
            "input_source": {"candidate_id": "source", "sha256": "source-hash"},
            "problem_sha256": hashlib.sha256(bump_input.scope_bytes(self.paths)).hexdigest(),
            "formalization": {"contract": adopted, "spec": self.spec, "main_sha": self.head, "revision": 1,
                              "solution_candidate": "source", "solution_sha256": "source-hash"},
            "formal_tasks": {"one": {**self.tasks[0], "outputs": self.outputs, "accepted_candidate": "candidate"}},
            "formal_candidates": {}}
        identity = {"main_sha": self.head, "source_sha256": "f" * 64, "environment": {}}
        with patch.object(contract, "source_identity", return_value=identity), \
             patch.object(project, "require_pinned_inputs", return_value=None), \
             patch.object(contract, "build_sources", return_value={"returncode": 0, "output": "fixture"}), \
             patch.object(contract, "check_formal_contract", return_value=checked), \
             patch.object(bump_input, "source_matches", return_value=True), \
             patch.object(contract.bump_cache, "compiled_receipt_current", return_value=True):
            report = contract.verify_final_project(self.paths, state)
            self.assertTrue(report["passed"], report)
            self.assertEqual(report["project_baseline_sha256"], self.baseline["sha256"])
            self.assertTrue(contract.snapshot_is_current(self.paths, state, report))
            wrong_hash = {**report, "project_baseline_sha256": "0" * 64}
            self.assertFalse(contract.snapshot_is_current(self.paths, state, wrong_hash))
            missing = deepcopy(state)
            missing.pop("project_baseline")
            self.assertFalse(contract.snapshot_is_current(self.paths, missing, report))
            with self.assertRaisesRegex(ValueError, "existing-project context is missing"):
                contract.verify_final_project(self.paths, missing)
            subprocess.run(["git", "switch", "-qc", "another"], cwd=self.root, check=True, capture_output=True)
            self.assertFalse(contract.snapshot_is_current(self.paths, state, report))
            with self.assertRaisesRegex(ValueError, "branch changed"):
                contract.verify_final_project(self.paths, state)

    def test_pinned_input_failure_precedes_any_native_or_lake_check(self):
        with patch.object(project, "require_pinned_inputs", side_effect=ValueError("lakefile changed")), \
             patch.object(contract, "workspace_layout", side_effect=AssertionError("must not execute Lake")), \
             patch.object(contract, "inspect_environment", side_effect=AssertionError("must not inspect")):
            checked = contract.check_formal_contract(self.root, self.current, self.tasks, completed=set(),
                task_id="one", proposed_outputs=self.outputs, final=True)
        self.assertFalse(checked["passed"])
        self.assertIn("lakefile changed", checked["issues"][0])


class InventoryInspectorTests(unittest.TestCase):
    def test_empty_targets_only_allowed_in_explicit_inventory_mode(self):
        with tempfile.TemporaryDirectory(prefix="unity-bump-inventory-") as directory:
            root = Path(directory)
            data = {"targets": {}, "issues": [], "external_declarations": {}, "prerequisite_declarations": {},
                    "project_declarations": [{"name": "Project.original", "module": "Project", "kind": "theorem"}],
                    "project_records": {"Project.original": preserved("Project.original")},
                    "project_axioms": [], "project_sorries": [], "project_used_axioms": []}
            with patch.object(contract, "workspace_layout", return_value={"modules": {"Project.lean": "Project"}}), \
                 patch.object(contract.bump_native, "executable", return_value=root / "inspector"), \
                 patch.object(contract.bump_jobs, "run", return_value=subprocess.CompletedProcess(
                     [], 0, json.dumps(data), "")) as run, \
                 patch.object(contract.bump_cache, "compiled_receipt", return_value=None):
                with self.assertRaisesRegex(ValueError, "no declarations"):
                    contract.inspect_environment(root, [])
                result = contract.inspect_environment(root, [], _inventory_only=True)
            self.assertEqual(result["project_records"], data["project_records"])
            self.assertIn("--inventory-only", run.call_args.args[1])

    def test_inventory_mode_fails_closed_on_missing_complete_records(self):
        with tempfile.TemporaryDirectory(prefix="unity-bump-inventory-") as directory:
            root = Path(directory)
            data = {"targets": {}, "issues": [], "external_declarations": {}, "prerequisite_declarations": {},
                    "project_declarations": [{"name": "Project.original", "module": "Project", "kind": "theorem"}],
                    "project_axioms": [], "project_sorries": [], "project_used_axioms": []}
            with patch.object(contract, "workspace_layout", return_value={"modules": {"Project.lean": "Project"}}), \
                 patch.object(contract.bump_native, "executable", return_value=root / "inspector"), \
                 patch.object(contract.bump_jobs, "run", return_value=subprocess.CompletedProcess(
                     [], 0, json.dumps(data), "")), \
                 patch.object(contract.artifacts, "store_text", return_value={"artifact_id": "diagnostic"}):
                with self.assertRaisesRegex(ValueError, "complete project preservation records"):
                    contract.inspect_environment(root, [], _inventory_only=True)


class InstalledLeanInventoryTests(unittest.TestCase):
    """Real helper execution using only an already installed Lean binary."""

    def test_inventory_records_preserve_proofs_and_report_unrelated_holes(self):
        elan = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
        chains = [elan / "toolchains" / ("leanprover--lean4---v" + version)
                  for version in ("4.28.0", "4.33.1", "4.34.1")]
        chain = next((path for path in chains if (path / "bin/lean").is_file()), None)
        if chain is None:
            self.skipTest("requires an already installed supported Lean; never downloads")
        with tempfile.TemporaryDirectory(prefix="unity-bump-native-inventory-") as directory:
            root = Path(directory)
            source = root / "Fixture.lean"
            environment = {**os.environ, "LEAN_PATH": str(root), "LEAN_SYSROOT": str(chain)}
            helper = Path(contract.__file__).with_suffix(".lean")

            def inspect(proof):
                source.write_text("namespace Fixture\n"
                    "def stable : Nat := 0\n"
                    "theorem existing : True := " + proof + "\n"
                    "theorem unrelated : True := by sorry\n"
                    "axiom priorAssumption : False\n"
                    "theorem target (n : Nat) : n = n := by sorry\n"
                    "end Fixture\n")
                built = subprocess.run([str(chain / "bin/lean"), "-o", str(root / "Fixture.olean"), str(source)],
                    cwd=root, env=environment, capture_output=True, text=True, timeout=90)
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                checked = subprocess.run([str(chain / "bin/lean"), "--run", str(helper),
                    "Fixture", "--", "--inventory-only"], cwd=root, env=environment,
                    capture_output=True, text=True, timeout=90)
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                return json.loads(checked.stdout.strip().splitlines()[-1])

            before = inspect("True.intro")
            after = inspect("(fun h : True => h) True.intro")
            self.assertEqual(before["targets"], {})
            self.assertEqual(set(before["project_records"]), {row["name"] for row in before["project_declarations"]})
            self.assertIn("Fixture.unrelated", before["project_sorries"])
            self.assertIn("Fixture.priorAssumption", before["project_axioms"])
            first, second = before["project_records"]["Fixture.existing"], after["project_records"]["Fixture.existing"]
            self.assertEqual(first["declaration_meaning"], second["declaration_meaning"])
            self.assertNotEqual(first["proof_body"], second["proof_body"])
            self.assertEqual(before["project_records"]["Fixture.stable"], after["project_records"]["Fixture.stable"])

    def test_real_project_scoped_adoption_and_original_context_rejection(self):
        """Capture -> build -> inventory -> candidate check, without an agent."""
        chain = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan")) / "toolchains/leanprover--lean4---v4.28.0"
        if not all((chain / "bin" / name).is_file() for name in ("lean", "lake", "leanc")):
            self.skipTest("requires already installed Lean 4.28.0; never downloads")
        environment = {"PATH": str(chain / "bin") + os.pathsep + os.environ.get("PATH", ""),
                       "LEAN_PATH": "", "LEAN_SYSROOT": str(chain), "UNITY_AGENT_NAME": ""}
        with tempfile.TemporaryDirectory(prefix="unity-bump-native-contract-") as directory, \
             patch.dict(os.environ, environment):
            root = Path(directory).resolve()
            (root / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n")
            (root / "lakefile.toml").write_text('name = "bump_contract_fixture"\n[[lean_lib]]\nname = "Fixture"\n')
            (root / ".gitignore").write_text(".lake/\n.unity/\n.worktrees/\n")
            source = root / "Fixture.lean"

            def write_source(*, body="by sorry", target_type="n = n", old_proof="True.intro"):
                source.write_text("namespace Fixture\n"
                    "def stable : Nat := 0\n"
                    "theorem existing : True := " + old_proof + "\n"
                    "theorem unrelated : True := by sorry\n"
                    "axiom priorAssumption : True\n"
                    "theorem target (n : Nat) : " + target_type + " := " + body + "\n"
                    "end Fixture\n")

            write_source()
            # This explicitly dependency-free unit fixture prepares its empty
            # lockfile before baseline capture; the runtime never runs update.
            subprocess.run([str(chain / "bin/lake"), "update"], cwd=root, check=True,
                           capture_output=True, text=True, timeout=90)
            for args in (["init", "-q", "--initial-branch=main"], ["config", "user.email", "fixture@example.invalid"],
                         ["config", "user.name", "Fixture"], ["config", "commit.gpgsign", "false"],
                         ["add", "."], ["commit", "-qm", "baseline"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            baseline = project.capture_baseline(root, target_scope="Fixture.target")
            self.assertIn("Fixture.unrelated", baseline["project_sorries"])
            self.assertIn("Fixture.priorAssumption", baseline["project_axioms"])
            spec = {"prerequisites": []}
            initial = contract._seal_contract({"version": 3, "fingerprint_version": 2,
                "solution_candidate": "source", "solution_sha256": "source-hash", "requirements": [],
                "spec": spec, "spec_sha256": contract.digest(spec), "environment": baseline["environment"],
                "source_main_sha": baseline["head"], "obligation_ids": ["target"], "bindings": {}, "targets": {},
                "external_declarations": {}, "project_baseline": baseline})
            outputs = [{"declaration": "Fixture.target", "file": "Fixture.lean"}]
            tasks = [{"id": "target", "task_id": "target"}]

            def verify():
                build = contract.build_sources(root, full=True)
                self.assertEqual(build["returncode"], 0, build["output"])
                return contract.check_formal_contract(root, initial, tasks, completed=set(),
                    task_id="target", proposed_outputs=outputs, final=True)

            write_source(body="by rfl")
            accepted = verify()
            self.assertTrue(accepted["passed"], accepted)
            self.assertEqual(accepted["verified_tasks"], ["target"])
            self.assertEqual(set(accepted["verified_targets"]), {"Fixture.target"})

            write_source(body="Fixture.priorAssumption", target_type="True")
            drift = verify()
            self.assertFalse(drift["passed"])
            self.assertTrue(any("target signature" in issue for issue in drift["issues"]))
            self.assertTrue(any("forbidden axioms" in issue for issue in drift["issues"]))

            write_source(body="by rfl", old_proof="(fun h : True => h) True.intro")
            unrelated_edit = verify()
            self.assertFalse(unrelated_edit["passed"])
            self.assertTrue(any("protected existing declaration changed: Fixture.existing" in issue
                                for issue in unrelated_edit["issues"]))

    def test_all_scope_diagnostic_for_structure_definition_and_instance_holes(self):
        chain = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan")) / "toolchains/leanprover--lean4---v4.28.0"
        if not (chain / "bin/lean").is_file():
            self.skipTest("requires already installed Lean 4.28.0; never downloads")
        with tempfile.TemporaryDirectory(prefix="unity-bump-native-scope-") as directory:
            root = Path(directory)
            source = root / "ScopeFixture.lean"
            source.write_text("namespace ScopeFixture\n"
                "noncomputable def partialValue : Nat := by sorry\n"
                "structure WithProof where\n  val : Nat\n  valid : val = val\n"
                "noncomputable def partialStructure : WithProof := ⟨0, by sorry⟩\n"
                "noncomputable instance : Inhabited WithProof := ⟨partialStructure⟩\n"
                "def partialFunction : Nat → Nat\n  | 0 => by sorry\n  | n + 1 => n\n"
                "end ScopeFixture\n")
            env = {**os.environ, "LEAN_PATH": str(root), "LEAN_SYSROOT": str(chain)}
            built = subprocess.run([str(chain / "bin/lean"), "-o", str(root / "ScopeFixture.olean"), str(source)],
                                   cwd=root, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            result = subprocess.run([str(chain / "bin/lean"), "--run", str(Path(contract.__file__).with_suffix(".lean")),
                "ScopeFixture", "--", "--inventory-only"], cwd=root, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            inspected = json.loads(result.stdout.strip().splitlines()[-1])
            holes = set(inspected["project_axioms"]) | set(inspected["project_sorries"])
            records = inspected["project_records"]
            self.assertIn("ScopeFixture.partialStructure._proof_1", holes)
            self.assertTrue(records["ScopeFixture.partialStructure._proof_1"]["is_internal_detail"])
            self.assertNotIn("ScopeFixture.partialStructure", holes)
            for selection in ("All", "ScopeFixture.partialStructure._proof_1", "ScopeFixture.partialStructure"):
                with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, "editable-owner"):
                    project._initial_scope(selection, records, holes)
            selected = project._initial_scope("ScopeFixture.partialValue, ScopeFixture.partialFunction",
                                              records, holes)["existing_targets"]
            self.assertEqual(selected, ["ScopeFixture.partialFunction", "ScopeFixture.partialValue"])


if __name__ == "__main__":
    unittest.main()
