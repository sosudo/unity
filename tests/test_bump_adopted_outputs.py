"""Adopted-file provenance survives semantic reopening; no agents/providers."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_bump_manifest_repair as manifest_tests
from tests import test_bump_delta_project as delta_tests
from tests import test_bump_project as project_tests
from tests import test_bump_delta_native as native_tests
from tests.test_bump_manifest_repair import output, machine_snapshot
from tests.test_bump_project import inventory, record
from unity import bump_contract as contract, bump_state as state
from unity import bump_project as project, bump_delta as delta, bump_runtime as runtime


class AdoptedOutputStateTests(manifest_tests.ManifestStateFixture):
    def reopen(self):
        before = self.current()
        state.refine_chunks(self.forum, "Ada", before["revision"], {
            "reopen_representations": [{"task_id": "alpha", "reason": "Correct representation."}]})
        return before, self.current()

    def test_reopen_retains_only_controller_adopted_outputs_and_no_acceptance(self):
        before, after = self.reopen()
        current = after["formalization"]["contract"]
        self.assertEqual(contract.adopted_output_records(current),
                         contract.adopted_output_records(before["formalization"]["contract"]))
        self.assertNotIn("alpha", current["bindings"])
        self.assertNotIn(output("alpha")["declaration"], current["targets"])
        self.assertEqual(after["formal_tasks"]["alpha"]["representation"]["status"], "stale")
        self.assertIsNone(after["formal_tasks"]["alpha"]["accepted_candidate"])
        with self.assertRaises(ValueError):
            state.record_review_snapshot(self.forum, machine_snapshot(before))

    def test_legacy_fallback_ignores_reservations_and_history(self):
        value = deepcopy(self.current()["formalization"]["contract"])
        value.pop("adopted_outputs", None)
        value["file_reservations"] = {"Injected.lean": {"owner_task": "alpha"}}
        value["history"] = [{"outputs": [{"declaration": "bad", "file": "Bad.lean"}]}]
        self.assertEqual(contract.adopted_output_paths(value), {"Example/alpha.lean", "Example/other.lean"})

    def test_exact_publication_rejects_provenance_injection_or_loss(self):
        self.reopen()
        strategy = self.claim("alpha")
        candidate = state.submit_formal_candidate(self.forum, strategy, "Ada", "alpha", "d" * 40,
            self.current()["formalization"]["main_sha"], "e" * 64,
            stage="representation", outputs=[output("alpha"), output("helper")])["candidate"]
        state.begin_formal_merge(self.forum, candidate["candidate_id"])
        current = self.current()["formalization"]["contract"]
        proposed = deepcopy(current)
        proposed["bindings"]["alpha"] = candidate["outputs"]
        proposed["adopted_outputs"] = contract.adopted_output_records(current,
            task_id="alpha", outputs=candidate["outputs"])
        for row in candidate["outputs"]:
            proposed["targets"][row["declaration"]] = {"fingerprint": contract.digest(row["declaration"])}
        for mode in ("injected", "lost"):
            invalid = deepcopy(proposed)
            if mode == "injected":
                invalid["adopted_outputs"] = contract.adopted_output_records(invalid,
                    task_id="alpha", outputs=[{"declaration": "Bad", "file": "Bad.lean"}])
            else:
                invalid["adopted_outputs"] = invalid["adopted_outputs"][:-1]
            invalid = contract._seal_contract(invalid)
            receipt = {"status": "passed", "contract_sha256": invalid["sha256"],
                       "policy_sha256": contract.policy_hash()}
            before = self.current()
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                state.finish_formal_merge(self.forum, candidate["candidate_id"], success=True,
                    main_sha="f" * 40, verification=receipt, proposed_contract=invalid)
            self.assertEqual(self.current(), before)
        proposed = contract._seal_contract(proposed)
        state.finish_formal_merge(self.forum, candidate["candidate_id"], success=True, main_sha="f" * 40,
            verification={"status": "passed", "contract_sha256": proposed["sha256"],
                          "policy_sha256": contract.policy_hash()}, proposed_contract=proposed)
        self.assertEqual(self.current()["formalization"]["contract"]["adopted_outputs"], proposed["adopted_outputs"])

    def test_replan_carries_provenance_without_reviving_stale_binding(self):
        _, after = self.reopen()
        after["phase"] = "chunking"
        supplied = deepcopy(after["formalization"]["contract"])
        supplied["adopted_outputs"] = []
        supplied = contract._seal_contract(supplied)
        prepared = state.prepare_informal_plan(after, self.dag, main_sha="d" * 40, contract=supplied)
        self.assertEqual(prepared["formalization"]["contract"]["adopted_outputs"],
                         after["formalization"]["contract"]["adopted_outputs"])
        self.assertNotIn("alpha", prepared["formalization"]["contract"]["bindings"])

    def test_final_snapshot_cannot_inject_or_drop_provenance(self):
        current = self.current()["formalization"]["contract"]
        injected = contract.adopted_output_records(current, task_id="alpha",
            outputs=[{"declaration": "Bad", "file": "Bad.lean"}])
        for outputs in ([], injected):
            proposed = deepcopy(current)
            proposed["adopted_outputs"] = outputs
            proposed = contract._seal_contract(proposed)
            report = machine_snapshot(self.current(), {"proposed_contract": proposed,
                "base_contract_sha256": current["sha256"], "contract_sha256": proposed["sha256"]})
            before = self.current()
            with self.subTest(outputs=outputs), self.assertRaisesRegex(ValueError, "provenance"):
                state.record_review_snapshot(self.forum, report)
            self.assertEqual(self.current(), before)

    def test_rename_and_repeated_invalidation_retain_exact_pairs_not_bindings(self):
        _, after = self.reopen()
        previous = after["formalization"]["contract"]
        renamed = deepcopy(previous)
        outputs = [{"declaration": "Example.alpha", "file": "Example/Moved.lean"}]
        renamed["adopted_outputs"] = contract.adopted_output_records(previous, task_id="alpha", outputs=outputs)
        renamed["bindings"]["alpha"] = outputs
        renamed["targets"]["Example.alpha"] = {"fingerprint": "checked"}
        reopened, _ = contract.invalidate_bindings(renamed, {"alpha"})
        self.assertEqual(contract.adopted_output_paths(reopened),
                         {"Example/alpha.lean", "Example/Moved.lean", "Example/other.lean"})
        self.assertNotIn("alpha", reopened["bindings"])


class AdoptedOutputProjectTests(unittest.TestCase):
    setUp = project_tests.ExistingProjectTests.setUp
    environment = project_tests.ExistingProjectTests.environment
    capture = delta_tests.DeltaProjectTests.capture

    def prepared(self):
        baseline = project.bind_scope(self.capture(), {"existing_targets": [], "chunks": []}, root=self.root)
        (self.root / "New.lean").write_text("theorem stale : True := by sorry\n")
        self.layout["modules"]["New.lean"] = "New"
        value = contract._seal_contract({"bindings": {"one": [{"declaration": "stale", "file": "New.lean"}]},
            "targets": {"stale": {}}, "project_baseline": baseline})
        reopened, _ = contract.invalidate_bindings(value, {"one"})
        return baseline, reopened

    def test_real_pinned_guard_accepts_retained_but_rejects_unknown_and_original_edits(self):
        baseline, value = self.prepared()
        paths = contract.adopted_output_paths(value)
        project.require_pinned_inputs(self.root, baseline, allowed_new_paths=paths)
        (self.root / "Unknown.lean").write_text("theorem x : True := by trivial\n")
        with self.assertRaisesRegex(ValueError, "unapproved new project input"):
            project.require_pinned_inputs(self.root, baseline, allowed_new_paths=paths)
        (self.root / "Unknown.lean").unlink()
        (self.root / "Main.lean").write_text("theorem target : False := by sorry\n")
        with self.assertRaisesRegex(ValueError, "pinned project input changed"):
            project.require_pinned_inputs(self.root, baseline, allowed_new_paths=paths)

    def test_dependency_guard_survives_retained_path(self):
        baseline, value = self.prepared()
        self.env["dependencies"]["Mathlib"]["sources"] = "changed"
        with self.assertRaisesRegex(ValueError, "dependency source bytes changed"):
            project.require_pinned_inputs(self.root, baseline,
                allowed_new_paths=contract.adopted_output_paths(value))

    def test_runtime_preapply_guard_admits_reopened_file_before_candidate_identity(self):
        _, value = self.prepared()
        current = {"formalization": {"contract": value}}
        paths = SimpleNamespace(project_root=self.root, forum=self.root / ".unity/forum")
        candidate = {"author": "Ada", "commit_sha": "a" * 40}
        with patch.object(runtime.bump_state, "load_state", return_value=current), \
             patch.object(runtime, "require_source_matches"), \
             patch.object(runtime.bump_state, "candidate_is_current", return_value=True), \
             patch.object(runtime, "_candidate_preflight", return_value=[]), \
             patch.object(runtime.worktree, "verify_candidate_commit", side_effect=ValueError("identity stop")) as verify:
            result = runtime._apply_formal_candidate(paths, candidate, {"task_id": "two"})
        self.assertEqual(result["error"], "candidate identity failed: identity stop")
        verify.assert_called_once()

    def check_stale(self, baseline, value, *, final=False, extra=None):
        contexts = {"New": inventory({"stale": record("stale", module="New", hole=True)},
                                    sorries=["stale"], used=["sorryAx"])}
        contexts.update(extra or {})
        with patch.object(delta, "verification_modules", return_value={p: m for p, m in self.layout["modules"].items()
                                                                      if p in contract.adopted_output_paths(value)}), \
             patch.object(contract, "inspect_environment", side_effect=lambda root, tasks, **kw:
                          contexts[kw["module_context"][0]]):
            return contract._project_context_issues(self.root, value, [], completed=set(), final=final)

    def test_exact_nonfinal_theorem_hole_only_final_stays_strict(self):
        baseline, value = self.prepared()
        self.assertEqual(self.check_stale(baseline, value), [])
        self.assertTrue(any("proof holes" in issue for issue in self.check_stale(baseline, value, final=True)))
        for kind in ("def", "type_hole"):
            row = record("stale", module="New", kind="def" if kind == "def" else "theorem", hole=True)
            if kind == "type_hole":
                row["type"] = ["const", "sorryAx"]
                row["declaration_meaning"]["type"] = row["type"]
            issues = self.check_stale(baseline, value, extra={"New": inventory({"stale": row},
                sorries=["stale"], used=["sorryAx"])})
            self.assertTrue(any("proof holes" in issue for issue in issues), issues)

    def test_same_name_in_wrong_admitted_module_does_not_get_allowance(self):
        baseline, value = self.prepared()
        (self.root / "OtherNew.lean").write_text("theorem allowed : True := by trivial\ntheorem stale : True := by sorry\n")
        self.layout["modules"]["OtherNew.lean"] = "OtherNew"
        value["adopted_outputs"] = contract.adopted_output_records(value, task_id="two",
            outputs=[{"declaration": "allowed", "file": "OtherNew.lean"}])
        issues = self.check_stale(baseline, value, extra={"OtherNew": inventory({
            "allowed": record("allowed", module="OtherNew"), "stale": record("stale", module="OtherNew", hole=True)},
            sorries=["stale"], used=["sorryAx"])})
        self.assertTrue(any("proof holes in OtherNew: stale" in issue for issue in issues), issues)

    def test_cleanup_does_not_require_retained_declarations_or_deleted_modules(self):
        baseline, value = self.prepared()
        (self.root / "New.lean").unlink()
        self.layout["modules"].pop("New.lean")
        self.assertEqual(contract.adopted_output_tasks(value, root=self.root), [])
        project.require_pinned_inputs(self.root, baseline, allowed_new_paths=contract.adopted_output_paths(value))


class InstalledLeanAdoptedOutputTests(unittest.TestCase):
    """One bounded real-native sequence; uses only already-installed Lean."""
    setUpClass = classmethod(native_tests.InstalledLeanDeltaTests.setUpClass.__func__)
    setUp = native_tests.InstalledLeanDeltaTests.setUp
    write = native_tests.InstalledLeanDeltaTests.write
    command = native_tests.InstalledLeanDeltaTests.command
    baseline = native_tests.InstalledLeanDeltaTests.baseline
    build = native_tests.InstalledLeanDeltaTests.build

    def test_native_retained_theorem_hole_reopen_repair_and_strict_negatives(self):
        baseline = self.baseline()
        spec = {"prerequisites": []}
        current = contract._seal_contract({"version": 3, "fingerprint_version": 2, "inspection_policy": 2,
            "solution_candidate": "source", "solution_sha256": "source-hash", "requirements": [],
            "spec": spec, "spec_sha256": contract.digest(spec), "environment": baseline["environment"],
            "source_main_sha": baseline["head"], "obligation_ids": ["one", "two"], "bindings": {}, "targets": {},
            "external_declarations": {}, "project_baseline": baseline})
        tasks = [{"task_id": "one"}, {"task_id": "two"}]
        old = [{"declaration": "stale", "file": "Fixture/New.lean"}]
        other = [{"declaration": "other", "file": "Fixture/OtherNew.lean"}]
        self.write(old[0]["file"], "import Fixture.Base\ntheorem stale : True := by sorry\n")
        self.build(baseline)
        adopted = contract.check_formal_contract(self.root, current, tasks, completed=set(),
            task_id="one", proposed_outputs=old, stage="representation")
        self.assertTrue(adopted["passed"], adopted["issues"])
        self.command(["git", "add", old[0]["file"]])
        self.command(["git", "commit", "-qm", "checked representation fixture"])
        current, _ = contract.invalidate_bindings(adopted["proposed_contract"], {"one"})
        project.require_pinned_inputs(self.root, baseline, allowed_new_paths=contract.adopted_output_paths(current))
        self.write(other[0]["file"], "import Fixture.Base\ntheorem other : True := True.intro\n")
        self.build(baseline)
        unrelated = contract.check_formal_contract(self.root, current, tasks, completed=set(),
            task_id="two", proposed_outputs=other)
        self.assertTrue(unrelated["passed"], unrelated["issues"])
        current = unrelated["proposed_contract"]
        for text, changed_file in (
            ("import Fixture.Base\ndef stale : Prop := sorry\n", old[0]["file"]),
            ("import Fixture.Base\ntheorem stale : (by exact (sorry : Prop)) := by sorry\n", old[0]["file"]),
            ("import Fixture.Base\ntheorem other : True := True.intro\ntheorem stale : True := by sorry\n", other[0]["file"])):
            original = (self.root / changed_file).read_text()
            self.write(changed_file, text)
            self.build(baseline)
            checked = contract.check_formal_contract(self.root, current, tasks, completed={"two"})
            self.assertFalse(checked["passed"], text)
            self.assertTrue(any("proof holes" in issue for issue in checked["issues"]), checked["issues"])
            self.write(changed_file, original)
        self.build(baseline)
        final = contract.check_formal_contract(self.root, current, tasks, completed={"two"}, final=True)
        self.assertFalse(final["passed"])
        self.assertTrue(any("proof holes" in issue for issue in final["issues"]), final["issues"])
        self.write(old[0]["file"], "import Fixture.Base\ntheorem stale : True := True.intro\n")
        self.build(baseline)
        repaired = contract.check_formal_contract(self.root, current, tasks, completed={"two"},
            task_id="one", proposed_outputs=old, final=True)
        self.assertTrue(repaired["passed"], repaired["issues"])


if __name__ == "__main__":
    unittest.main()
