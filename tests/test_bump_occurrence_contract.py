"""Per-module original occurrence preservation; no models or real Lean runs."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_contract as contract, bump_project as project, bump_state as state
from unity import bump_migration_contract as native

if __package__:
    from .test_bump_contract import migration_baseline, meaning, seal
else:
    from test_bump_contract import migration_baseline, meaning, seal


def occurrence_contract():
    baseline = migration_baseline()
    reports = baseline["original_reports"]
    source = deepcopy(reports["Fixture"])
    # Both raw module artifacts contain exactly the same kernel Name, but the
    # second occurrence has an independently inherited proof assumption.
    other = reports["Empty"]
    other["declarations"] = deepcopy(source["declarations"])
    other["declarations"]["result"].update(module="Empty", axioms=["TrustB"])
    other["meanings"] = deepcopy(source["meanings"])
    other["meanings"]["result"]["module"] = "Empty"
    other["meanings"]["TrustB"] = meaning("TrustB", "axiom", module="External")
    seal(other)
    baseline["declarations"] = project.migration_declarations(reports)
    baseline["scope"]["existing_targets"] = sorted(baseline["declarations"])
    baseline = project._seal(baseline)
    bindings, targets = contract._migration_bindings(baseline)
    value = {"version": 3, "migration_policy": 1, "migration_scope_policy": 1,
             "migration_occurrence_policy": 1, "inspection_policy": 4, "fingerprint_version": 2,
             "solution_candidate": "original", "solution_sha256": "c" * 64,
             "project_baseline": baseline, "bindings": bindings, "targets": targets,
             "obligation_ids": sorted(bindings), "spec": {"arguments": [], "prerequisites": []},
             "requirements": [], "environment": baseline["environment"]}
    value["spec_sha256"] = contract.digest(value["spec"])
    value["adopted_outputs"] = contract.adopted_output_records(value)
    return contract._seal_contract(value)


def checked(value, *, module="Fixture", final=False, mutate=None):
    reports = deepcopy(value["project_baseline"]["original_reports"])
    for row in reports.values():
        row["compiled_modules"] = [p.replace("/source/", "/target/") for p in row["compiled_modules"]]
        row["compiled_inputs"] = {p: {"path": p, "sha256": "a" * 64} for p in row["compiled_modules"]}
        seal(row)
    if mutate:
        mutate(reports)
    identity = {"main_sha": "1" * 40, "source_sha256": "f" * 64, "environment": value["environment"]}
    with patch.object(project, "require_pinned_inputs"), \
            patch.object(contract, "source_identity", return_value=identity), \
            patch.object(native, "inspect_module", side_effect=lambda root, mod, owned: reports[mod]), \
            patch.object(contract.bump_cache, "compiled_receipt", return_value={"artifact": "a" * 64}), \
            patch.object(contract.bump_cache, "compiled_receipt_current", return_value=True):
        result = contract.check_formal_contract(Path("/target"), value, [], completed={module},
            task_id=module, proposed_outputs=value["bindings"][module], final=final)
    return {**result, "status": "passed" if result["passed"] else "failed",
            "contract_sha256": value["sha256"], "policy_sha256": contract.policy_hash()}


def review_snapshot(value):
    result = checked(value, final=True)
    return {**result, **result["source_identity"],
            "project_baseline_sha256": value["project_baseline"]["sha256"],
            "project_verification": contract.project_verification(Path("/target"), value["project_baseline"]),
            "declarations": contract.snapshot_declarations(value),
            "declaration_occurrences": contract.declaration_occurrences(value)}


class BumpOccurrenceContractTests(unittest.TestCase):
    def test_aggregate_retains_each_same_named_occurrence_and_its_own_trust(self):
        value = occurrence_contract()
        baseline = value["project_baseline"]
        self.assertTrue(project.baseline_is_valid(baseline), project.baseline_errors(baseline))
        self.assertEqual(len(baseline["declarations"]), 2)
        self.assertEqual(len(value["targets"]), 2)
        first = contract.output_target_key(value, "Fixture", "result")
        second = contract.output_target_key(value, "Empty", "result")
        self.assertNotEqual(first, second)
        self.assertEqual(value["targets"][first]["axioms"], [])
        self.assertEqual(value["targets"][second]["axioms"], ["TrustB"])

    def test_occurrence_identity_uses_typed_name_and_module_not_display(self):
        values = [["str", ["anonymous"], "1"], ["num", ["anonymous"], 1],
                  ["str", ["anonymous"], "a.b"], ["str", ["str", ["anonymous"], "a"], "b"]]
        ids = {project.migration_occurrence_id(module, name) for module in ("A", "B") for name in values}
        self.assertEqual(len(ids), 8)
        with self.assertRaises(ValueError):
            project.migration_occurrence_id("A", ["num", ["anonymous"], True])

    def test_baseline_rejects_flattened_legacy_shape_even_if_resealed(self):
        baseline = occurrence_contract()["project_baseline"]
        for mutate in (lambda b: b.update(version=4), lambda b: b.pop("occurrence_policy"),
                       lambda b: b["declarations"].pop(next(iter(b["declarations"]))),
                       lambda b: b["declarations"].update({"result": next(iter(b["declarations"].values()))})):
            changed = deepcopy(baseline)
            mutate(changed)
            self.assertFalse(project.baseline_is_valid(project._seal(changed)))

    def test_context_order_does_not_collapse_occurrences_or_change_ids(self):
        baseline = occurrence_contract()["project_baseline"]
        reports = dict(reversed(list(baseline["original_reports"].items())))
        self.assertEqual(project.migration_declarations(reports), baseline["declarations"])

    def test_candidate_checks_exactly_its_original_occurrence(self):
        value = occurrence_contract()
        first, second = checked(value, module="Fixture"), checked(value, module="Empty")
        self.assertTrue(first["passed"], first)
        self.assertTrue(second["passed"], second)
        self.assertFalse(set(first["verified_targets"]) & set(second["verified_targets"]))
        self.assertEqual(first["module_receipt"]["verified_targets"], first["verified_targets"])

    def test_final_checks_both_occurrences_and_each_module_receipt(self):
        value = occurrence_contract()
        review = review_snapshot(value)
        current = {"project_baseline": value["project_baseline"], "formalization": {"contract": value}}
        self.assertTrue(review["passed"], review)
        self.assertEqual(len(review["verified_targets"]), 2)
        contract.validate_migration_snapshot(current, review)
        for field in ("verified_targets", "declarations", "declaration_occurrences"):
            altered = deepcopy(review)
            altered[field].pop(next(iter(altered[field])))
            with self.subTest(field=field), self.assertRaises(ValueError):
                contract.validate_migration_snapshot(current, altered)

    def test_other_occurrence_cannot_launder_new_proof_assumption(self):
        value = occurrence_contract()
        def expand(reports):
            row = reports["Fixture"]
            row["declarations"]["result"]["axioms"] = ["TrustB"]
            row["meanings"]["TrustB"] = meaning("TrustB", "axiom", module="External")
            seal(row)
        result = checked(value, final=True, mutate=expand)
        self.assertFalse(result["passed"])
        self.assertEqual(result["verified_targets"], {})
        self.assertIn("trusted assumptions expanded", " ".join(result["issues"]).lower())

    def test_deleted_occurrence_is_not_satisfied_by_same_name_elsewhere(self):
        def remove(reports):
            reports["Fixture"]["declarations"].clear()
            seal(reports["Fixture"])
        result = checked(occurrence_contract(), final=True, mutate=remove)
        self.assertFalse(result["passed"])
        self.assertEqual(result["verified_targets"], {})

    def test_changed_type_in_one_occurrence_still_rejected(self):
        def change(reports):
            reports["Empty"]["meanings"]["result"]["meaning"]["type"] = ["sort", ["succ", ["zero"]]]
            seal(reports["Empty"])
        self.assertFalse(checked(occurrence_contract(), final=True, mutate=change)["passed"])

    def test_receipt_from_sibling_module_cannot_mark_task_complete(self):
        value = occurrence_contract()
        task = {"task_id": "Fixture", "migration_module": "Fixture", "outputs": value["bindings"]["Fixture"]}
        receipt = checked(value, module="Fixture")
        state._require_migration_receipt(task, value, receipt)
        sibling = checked(value, module="Empty")
        receipt["verified_targets"] = sibling["verified_targets"]
        receipt["module_receipt"]["verified_targets"] = sibling["verified_targets"]
        with self.assertRaises(ValueError):
            state._require_migration_receipt(task, value, receipt)

    def test_final_swapped_or_missing_module_receipts_rejected(self):
        value = occurrence_contract()
        current = {"project_baseline": value["project_baseline"], "formalization": {"contract": value}}
        for mutate in (lambda r: r["module_receipts"].pop("Fixture"),
                       lambda r: r["module_receipts"]["Fixture"].update(
                           verified_targets=r["module_receipts"]["Empty"]["verified_targets"]),
                       lambda r: r["module_receipts"]["Fixture"].pop("occurrence_policy")):
            review = review_snapshot(value)
            mutate(review)
            with self.assertRaises(ValueError):
                contract.validate_migration_snapshot(current, review)

    def test_critic_reference_is_scoped_and_cannot_ambiguously_cover_two_modules(self):
        value = occurrence_contract()
        first = contract.output_target_key(value, "Fixture", "result")
        second = contract.output_target_key(value, "Empty", "result")
        self.assertEqual(contract.resolve_review_declarations(value, ["Fixture"], ["result"]), [first])
        self.assertEqual(contract.resolve_review_declarations(value, ["Empty"], ["result"]), [second])
        self.assertEqual(contract.resolve_review_declarations(value, ["Fixture", "Empty"], [first, second]), [first, second])
        for owners, refs in [(["Fixture", "Empty"], ["result"]), (["Fixture"], [second]),
                             (["Fixture"], [first, "result"])]:
            with self.assertRaises(ValueError):
                contract.resolve_review_declarations(value, owners, refs)

    def test_missing_occurrence_policy_blocks_contract_resume(self):
        value = occurrence_contract()
        current = {"project_baseline": value["project_baseline"]}
        self.assertTrue(contract._baseline_matches(current, value))
        value.pop("migration_occurrence_policy")
        self.assertFalse(contract._baseline_matches(current, value))
        with self.assertRaises(ValueError):
            contract.output_target_key(value, "Fixture", "result")

    def test_controller_merge_marks_only_its_occurrence_complete_and_survives_reload(self):
        value = occurrence_contract()
        with tempfile.TemporaryDirectory() as directory:
            forum = Path(directory)
            with state.transaction(forum) as current:
                current.update(phase="formalizing", run_id="bump-occurrence-fixture",
                    input_source={"kind": "supplied_sources", "candidate_id": "original", "sha256": "c" * 64},
                    project_baseline=value["project_baseline"])
                current["formalization"].update(contract=value, revision=1, main_sha="1" * 40,
                    solution_candidate="original", solution_sha256="c" * 64)
                current["formal_tasks"] = {key: {"task_id": key, "revision": 1,
                    "status": "pending", "outputs": [], "dependencies": row["imports"]}
                    for key, row in value["project_baseline"]["compiler_modules"].items()}
            state.seed_migration_tasks(forum, value["project_baseline"]["compiler_modules"],
                                       value["project_baseline"]["original_reports"])
            state.record_migration_module_check(forum, "Fixture", checked(value), main_sha="1" * 40)
            loaded = state.load_state(forum)
            self.assertEqual(loaded["formal_tasks"]["Fixture"]["status"], "complete")
            self.assertEqual(loaded["formal_tasks"]["Empty"]["status"], "pending")
            self.assertEqual(loaded["formalization"]["contract"], value)
            state.record_migration_module_check(forum, "Empty", checked(value, module="Empty"), main_sha="1" * 40)
            loaded = state.load_state(forum)
            self.assertTrue(state.all_formal_tasks_complete(loaded))
            contract.validate_migration_state(loaded)
            receipts = [row["verification"]["verified_targets"] for row in loaded["formal_candidates"].values()]
            self.assertEqual(len(set().union(*(set(row) for row in receipts))), 2)
            for change in ("remove_output", "remove_task", "copy_sibling_receipt", "legacy_policy"):
                bad = deepcopy(loaded)
                if change == "remove_output":
                    bad["formal_tasks"]["Fixture"]["outputs"] = []
                elif change == "remove_task":
                    bad["formal_tasks"].pop("Fixture")
                elif change == "copy_sibling_receipt":
                    one = bad["formal_tasks"]["Fixture"]["accepted_candidate"]
                    two = bad["formal_tasks"]["Empty"]["accepted_candidate"]
                    bad["formal_candidates"][one]["verification"] = bad["formal_candidates"][two]["verification"]
                else:
                    bad["formalization"]["contract"].pop("migration_occurrence_policy")
                with self.subTest(change=change), self.assertRaises(ValueError):
                    contract.validate_migration_state(bad)

    def test_final_extension_cannot_inject_or_drop_occurrence_policy(self):
        value = occurrence_contract()
        with tempfile.TemporaryDirectory() as directory:
            forum = Path(directory)
            with state.transaction(forum) as current:
                current["phase"] = "formalizing"
                current["formalization"]["contract"] = value
            for field, replacement in (("migration_occurrence_policy", None), ("inspection_policy", 3),
                                       ("migration_scope_policy", 0)):
                proposed = deepcopy(value)
                if replacement is None:
                    proposed.pop(field)
                else:
                    proposed[field] = replacement
                proposed = contract._seal_contract(proposed)
                report = {"passed": True, "snapshot_id": "fixture-snapshot",
                    "proposed_contract": proposed, "contract_sha256": proposed["sha256"],
                    "base_contract_sha256": value["sha256"]}
                with patch.object(state, "_validate_snapshot_binding") as binding:
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        state.record_review_snapshot(forum, report)
                    binding.assert_not_called()
                self.assertEqual(state.load_state(forum)["formalization"]["contract"], value)


if __name__ == "__main__":
    unittest.main()
