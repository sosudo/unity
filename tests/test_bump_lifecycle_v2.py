"""Bump-only local identity, private focus and historical critic boundaries."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from unity import bump_contract, bump_checker_v2 as checker, bump_planner as planner, bump_state as state
from tests.test_bump_diagnostics import fixture_diagnostics
from tests import test_bump_migration_state_v2 as migration_fixture


class BumpLifecycleV2Tests(unittest.TestCase):
    setUp = migration_fixture.MigrationStateV2Tests.setUp
    read = migration_fixture.MigrationStateV2Tests.read
    claim = migration_fixture.MigrationStateV2Tests.claim
    refine = migration_fixture.MigrationStateV2Tests.refine

    def refresh(self, diagnostic):
        current = self.read()
        plan = planner.plan_repairs(self.index, diagnostic, current["migration_plan"])
        state.refresh_migration_plan(self.forum, self.index, plan,
            expected_revision=current["revision"], expected_main_sha=current["formalization"]["main_sha"])
        return self.read()

    def diagnostic(self, *, changed_module=None, changed_message=False):
        value = fixture_diagnostics(self.index, compiled=["A"])
        value["source_sha256"] = "9" * 64
        value["target_imports"]["source_sha256"] = value["source_sha256"]
        value["target_imports"]["sha256"] = state.digest({key: row for key, row in value["target_imports"].items() if key != "sha256"})
        value["artifact_ref"] = {"artifact_id": "artifact-" + "9" * 12, "sha256": "8" * 64}
        for row in value["diagnostics"]:
            row["id"] = "diag-" + ("e" if row["path"] == "B.lean" else "f") * 64
            row["log_offset"] += 100
            if changed_message and row["path"] == "B.lean":
                row["content_sha256"] = "a" * 64
        if changed_module:
            value["module_source_hashes"][changed_module] = "7" * 64
        value["snapshot_sha256"] = state.digest({key: row for key, row in value.items() if key != "snapshot_sha256"})
        return value

    def candidate(self, current, task="B"):
        row = current["formal_tasks"][task]
        return {"candidate_id": "candidate-" + task, "task_id": task, "task_revision": row["revision"],
                "task_input_sha256": row.get("task_input_sha256"),
                "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"],
                "stage": "complete", "changed_paths": [task + ".lean"], "deleted_paths": [],
                "outputs": deepcopy(self.contract["bindings"][task]), "status": "submitted"}

    def test_unrelated_source_and_log_refresh_preserves_revision_strategy_candidate_and_budget(self):
        self.claim()
        state.begin_migration_attempt(self.forum, "B", "Worker")
        before = self.read()
        candidate = self.candidate(before)
        with state.transaction(self.forum) as current:
            current["formal_candidates"][candidate["candidate_id"]] = candidate
            current["formal_tasks"]["B"]["status"] = "candidate_pending"
        after = self.refresh(self.diagnostic(changed_module="D"))
        self.assertEqual(after["formal_tasks"]["B"]["revision"], before["formal_tasks"]["B"]["revision"])
        self.assertEqual(after["formal_tasks"]["B"]["status"], "candidate_pending")
        self.assertTrue(state.candidate_is_current(after, after["formal_candidates"][candidate["candidate_id"]]))
        self.assertTrue(state.migration_candidate_context_current(after, candidate))
        self.assertEqual(after["migration_budgets"], before["migration_budgets"])
        self.assertTrue(all(state.strategy_is_current(after, row) for row in after["strategies"].values()))

    def test_local_source_and_diagnostic_content_changes_supersede_candidate(self):
        for diagnostic in (self.diagnostic(changed_module="B"), self.diagnostic(changed_message=True)):
            before = self.read()
            candidate = self.candidate(before)
            with state.transaction(self.forum) as current:
                current["formal_candidates"][candidate["candidate_id"]] = candidate
                current["formal_tasks"]["B"]["status"] = "candidate_pending"
            after = self.refresh(diagnostic)
            self.assertGreater(after["formal_tasks"]["B"]["revision"], before["formal_tasks"]["B"]["revision"])
            self.assertEqual(after["formal_candidates"][candidate["candidate_id"]]["status"], "superseded")
            self.assertFalse(state.candidate_is_current(after, candidate))

    def test_legacy_candidate_missing_local_digest_is_not_current_for_new_task(self):
        current = self.read()
        candidate = self.candidate(current)
        candidate.pop("task_input_sha256")
        self.assertFalse(state.candidate_is_current(current, candidate))
        legacy = deepcopy(current)
        legacy["formal_tasks"]["B"].pop("task_input_sha256")
        self.assertTrue(state.candidate_is_current(legacy, candidate))

    def test_legacy_v7_v8_refresh_adds_conservative_identity_without_resetting_budget(self):
        for version in (7, 8):
            if version == 8:
                self.setUp()
            self.claim()
            state.begin_migration_attempt(self.forum, "B", "Worker")
            with state.transaction(self.forum) as current:
                task = current["formal_tasks"]["B"]
                candidate = self.candidate(current)
                candidate.pop("task_input_sha256")
                current["formal_candidates"][candidate["candidate_id"]] = candidate
                task.pop("task_input_sha256")
                task.pop("compile_input_sha256")
                task["status"] = "candidate_pending"
            # Simulate an on-disk old schema without the writer auto-upgrade.
            current = self.read()
            current["schema_version"] = version
            with patch.object(state, "SCHEMA_VERSION", version):
                with state.transaction(self.forum) as persisted:
                    persisted.update(current)
            old = self.read()
            self.assertEqual(old["schema_version"], version)
            before_budget = deepcopy(old["migration_budgets"])
            after = self.refresh(self.diagnostic(changed_module="D"))
            self.assertEqual(after["schema_version"], 9)
            self.assertEqual(after["migration_budgets"], before_budget)
            self.assertEqual(after["formal_candidates"][candidate["candidate_id"]]["status"], "superseded")
            self.assertFalse(state.candidate_is_current(after, candidate))

    def test_scoped_mapping_adoption_preserves_unrelated_candidate_and_attempts(self):
        self.claim()
        state.begin_migration_attempt(self.forum, "B", "Worker")
        before = self.read()
        candidate = self.candidate(before)
        mapping = deepcopy(self.mapping)
        key = self.index["modules"]["D"]["occurrence_ids"][0]
        row = next(row for row in mapping.values() if key in row["original_ids"])
        row.update(mode="declared", reason="document exact intended drift", relation="same local operation",
                   evidence_refs=[{"artifact_id": "artifact-" + "a" * 12, "sha256": "c" * 64}])
        row["targets"][0]["expected_meaning_sha256"] = "d" * 64
        proposed = checker.seal({**deepcopy(self.contract), "mapping": mapping,
                                  "mapping_sha256": checker.digest(mapping)})
        proposal_id = state.digest(mapping)
        with state.transaction(self.forum) as current:
            current["formal_candidates"][candidate["candidate_id"]] = candidate
            current["formal_tasks"]["B"]["status"] = "candidate_pending"
            current["migration_mapping_proposals"][proposal_id] = {
                "status": "proposed", "mapping": mapping, "contract_sha256": self.contract["sha256"],
                "main_sha": current["formalization"]["main_sha"],
                "source_sha256": current["migration_plan"]["source_sha256"]}
        current = self.read()
        self.assertTrue(state.resolve_migration_mapping(self.forum, proposal_id,
            expected_revision=current["revision"], proposed_contract=proposed))
        after = self.read()
        self.assertEqual(after["formal_tasks"]["B"]["revision"], before["formal_tasks"]["B"]["revision"])
        self.assertTrue(state.candidate_is_current(after, candidate))
        self.assertEqual(after["formal_candidates"][candidate["candidate_id"]]["status"], "submitted")
        self.assertEqual(after["migration_budgets"], before["migration_budgets"])
        self.assertTrue(after["migration_refresh_required"])
        self.assertFalse(state.migration_candidate_context_current(after, candidate))

    def test_mapping_artifact_alias_is_stable_but_evidence_content_change_is_not(self):
        mapped = deepcopy(self.contract)
        row = next(iter(mapped["mapping"].values()))
        module = row["targets"][0]["module"]
        row["evidence_refs"] = [{"artifact_id": "artifact-" + "a" * 12, "sha256": "c" * 64}]
        before = bump_contract.migration_group_mapping_content(mapped, module)
        row["evidence_refs"][0]["artifact_id"] = "artifact-" + "b" * 12
        self.assertEqual(bump_contract.migration_group_mapping_content(mapped, module), before)
        row["evidence_refs"][0]["sha256"] = "d" * 64
        self.assertNotEqual(bump_contract.migration_group_mapping_content(mapped, module), before)

    def test_refined_partition_and_private_chunk_survive_evidence_only_refresh(self):
        self.claim()
        changed = deepcopy(self.read()["formal_tasks"]["B"]["declaration_subtasks"])
        changed[0]["id"] = "my-explicit-partition"
        self.refine(changed)
        before = self.read()
        after = self.refresh(self.diagnostic(changed_module="D"))
        self.assertEqual(after["formal_tasks"]["B"]["revision"], before["formal_tasks"]["B"]["revision"])
        self.assertEqual(after["formal_tasks"]["B"]["declaration_subtasks"][0]["id"], "my-explicit-partition")
        self.assertEqual({key for row in after["formal_tasks"]["B"]["declaration_subtasks"] for key in row["diagnostic_ids"]},
                         set(after["formal_tasks"]["B"]["diagnostic_ids"]))

    def test_chunk_checkpoint_advances_private_focus_without_spending_an_attempt_or_accepting(self):
        self.claim()
        chunk = state.next_migration_chunk(self.read(), "B")
        self.assertIsNotNone(chunk)
        state.begin_migration_attempt(self.forum, "B", "Worker", subtask_id=chunk["id"])
        current = self.read()
        checkpoint = {"author": "Worker", "task_id": "B", "task_revision": current["formal_tasks"]["B"]["revision"],
                      "manifest_artifact": "artifact-" + "1" * 12, "ref": "refs/unity/private", "commit_sha": "0" * 40}
        result = state.checkpoint_migration_chunk(self.forum, "Worker", "B", chunk["id"],
            expected_revision=current["revision"], checkpoint=checkpoint, summary="private repair checkpoint")
        after = self.read()
        self.assertEqual(after["migration_budgets"], current["migration_budgets"])
        self.assertEqual(after["formal_tasks"]["B"]["status"], "pending")
        self.assertIsNone(after["formal_tasks"]["B"]["accepted_candidate"])
        self.assertTrue(result["not_an_acceptance_check"])
        self.assertEqual(result["next_action"], "verify_and_finalize_complete_module")

    def test_refinement_rebinds_live_chunk_and_rejects_obsolete_checkpoint_without_extra_attempt(self):
        self.claim()
        chunk = state.next_migration_chunk(self.read(), "B")
        state.begin_migration_attempt(self.forum, "B", "Worker", subtask_id=chunk["id"])
        current = self.read()
        changed = deepcopy(current["formal_tasks"]["B"]["declaration_subtasks"])
        changed[0]["id"] = "replacement-focus"
        result = self.refine(changed)
        after = self.read()
        self.assertEqual(after["migration_budgets"], current["migration_budgets"])
        self.assertEqual(result["next_chunk"]["id"], "replacement-focus")
        self.assertEqual(after["formal_tasks"]["B"]["migration_focus"]["subtask_id"], "replacement-focus")
        self.assertFalse(after["formal_tasks"]["B"].get("migration_chunks"))
        with self.assertRaisesRegex(ValueError, "stale or not owned"):
            state.checkpoint_migration_chunk(self.forum, "Worker", "B", chunk["id"],
                expected_revision=after["revision"], checkpoint={"author": "Worker", "task_id": "B",
                    "task_revision": current["formal_tasks"]["B"]["revision"]})

    def review_fixture(self, *, requirement="preserve:A", reopen="A"):
        current = self.read()
        snapshot = {"snapshot_id": "review-fixture", "main_sha": "0" * 40,
            "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"],
            "original_index_sha256": self.contract["original_index_sha256"], "migration_policy": 2,
            "formalization_revision": current["formalization"]["revision"],
            "spec_sha256": state.digest(self.contract["spec"]), "mapping_sha256": self.contract["mapping_sha256"],
            "declarations": {key: task for task, group in self.contract["task_bindings"].items() for key in group["obligation_ids"]},
            "module_receipts": {key: {} for key in self.contract["task_bindings"]}, "task_statuses": {}}
        ledger = next(row for row in self.contract["requirements"] if row["id"] == requirement)
        review = {"snapshot_id": snapshot["snapshot_id"], "scope_rationale": "Review all original coverage, including clean groups.",
            "requirements": [{"requirement_id": requirement, "status": "fail", "declarations": [],
                "checked_anchor_ids": ledger["anchor_ids"], "checked_prerequisite_ids": [],
                "rationale": "Original behavior was lost.", "argument_rationale": "Repair the provider's local meaning.",
                "repair_steps": ["Restore the original local behavior."]}], "repair_reviews": []}
        with state.transaction(self.forum) as value:
            value["phase"] = "critic"
            value["formalization"].update(status="review", review_snapshot=snapshot)
            value["review_snapshots"][snapshot["snapshot_id"]] = snapshot
        # Deterministic native snapshot production is out of this fixture's scope;
        # the real semantic verdict/reopening/feedback path is exercised below.
        with patch.object(state, "_current_snapshot", return_value=snapshot):
            state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "Repair original meaning.",
                review=review, reopen_tasks=[reopen])
        return self.read()

    def test_critic_reopen_clean_group_reads_list_ledger_and_all_original_snapshot(self):
        current = self.review_fixture()
        feedback = state.critic_feedback_for_task(current, "A")["direct"]
        self.assertEqual(feedback[0]["requirement_id"], "preserve:A")
        self.assertEqual(feedback[0]["repair_steps"], ["Restore the original local behavior."])
        self.assertNotIn("A", current["review_snapshots"]["review-fixture"]["task_statuses"])
        self.assertEqual(current["formal_tasks"]["A"]["faithfulness"]["status"], "changes_requested")

    def test_critic_provider_guidance_survives_coverage_only_consumer_and_global_epoch_change(self):
        current = self.review_fixture(requirement="preserve:B", reopen="A")
        current["retired_tasks"]["B"] = current["formal_tasks"].pop("B")
        current["formalization"]["revision"] += 1
        feedback = state.critic_feedback_for_task(current, "A")["direct"]
        self.assertEqual(feedback[0]["requirement_task_ids"], ["B"])
        self.assertEqual(feedback[0]["dependency_provider_task_ids"], ["A"])
        snapshot = current["review_snapshots"]["review-fixture"]
        snapshot["original_index_sha256"] = "f" * 64
        self.assertEqual(state.critic_feedback_for_task(current, "A"), {"direct": [], "upstream": []})


if __name__ == "__main__":
    unittest.main()
