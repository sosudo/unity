"""Offline critic-checklist policy and read-only, task-scoped feedback history."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from unity import autoformalize_report, autoformalize_state as state
from unity.autoformalize_review import SemanticReview
from unity.autoformalize_spec import digest
import test_autoformalize_manifest_repair as manifest_fixture
from test_autoformalize_manifest_repair import informal_dag, machine_snapshot, semantic_evidence


class FeedbackFixture(manifest_fixture.ManifestStateFixture):
    separate_requirements = False

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="autoformalize-critic-feedback-")
        self.addCleanup(temporary.cleanup)
        self.forum = Path(temporary.name) / ".unity/forum"
        self.source = self.paper = {
            "kind": "supplied_sources", "candidate_id": "source-" + "c" * 64,
            "sha256": "c" * 64,
            "source_refs": [{"ref_id": "document:main", "path": "SOURCE.md", "sha256": "a" * 64}],
        }
        state.initialize_source(self.forum, "a" * 64, "b" * 40, self.source)
        chunks = [{"id": name, "lean_decl": "Example." + name,
                   "lean_file": f"Example/{name}.lean", "source_components": ["document:main"],
                   "dependencies": ["alpha"] if name == "beta" else []}
                  for name in ("alpha", "beta", "other")]
        requirements = ([{"id": "R-" + name, "statement": "Source claim " + name,
                          "source_components": ["document:main"], "tasks": [name]}
                         for name in ("alpha", "beta", "other")]
                        if self.separate_requirements else None)
        self.dag = informal_dag(self.source, chunks, requirements=requirements)
        contract = {
            "version": 3, "representation_review_policy": 1,
            "bindings": {}, "targets": {}, "external_declarations": {},
            "prerequisite_declarations": {}, "environment": {},
            "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"],
            "requirements": deepcopy(self.dag["requirements"]), "spec": deepcopy(self.dag["spec"]),
            "spec_sha256": digest(self.dag["spec"]), "obligation_ids": ["alpha", "beta", "other"],
        }
        contract["sha256"] = state._contract_digest(contract)
        state.initialize_informal_plan(self.forum, self.dag, main_sha="b" * 40, contract=contract)
        for task_id in ("alpha", "beta", "other"):
            self.accept(task_id)
        self.enter_review()

    def enter_review(self):
        complete = state.all_formal_tasks_complete(self.current())
        state.record_review_snapshot(self.forum, machine_snapshot(self.current(), {"passed": complete}))
        state.begin_critic(self.forum, diagnostic=not complete)
        self.review = semantic_evidence(self.current())
        return self.review

    def fail(self, *, steps=None, reopen=None, requirement=None, review=None):
        review = deepcopy(self.review if review is None else review)
        requirement = requirement or ("R-alpha" if self.separate_requirements else "R1")
        for entry in review["requirements"]:
            if entry["requirement_id"] == requirement:
                entry.update(status="fail", repair_steps=["Prove the actual source counting step."] if steps is None else steps)
        return state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "The source argument is missing.",
            review=review, reopen_tasks=["alpha"] if reopen is None else reopen)["verdict"]

    def feedback(self, task_id="alpha", value=None):
        return state.critic_feedback_for_task(self.current() if value is None else value, task_id)


class CriticChecklistValidationTests(FeedbackFixture):
    def test_legacy_schema_defaults_without_new_submission_validation(self):
        for entry in self.review["requirements"]:
            entry["status"] = "fail"
        saved = deepcopy(self.review)
        self.assertEqual(SemanticReview.model_validate(saved).requirements[0].repair_steps, [])
        self.assertEqual(saved, self.review)
        self.assert_atomic_rejection(lambda: state.submit_critic_verdict(
            self.forum, "Critic", "lean_reopen", "Missing steps", review=self.review, reopen_tasks=["alpha"]))

    def test_invalid_failed_checklists_rejected_atomically(self):
        for steps in ([], [""], [" \n "], ["x" * 1001], ["step"] * 9, [1], "not a list"):
            with self.subTest(steps=steps):
                self.assert_atomic_rejection(lambda: self.fail(steps=steps))

    def test_nonfailed_requirements_cannot_supply_steps(self):
        for status in ("pass", "not_checked"):
            review = deepcopy(self.review)
            review["requirements"][0].update(status=status, repair_steps=["Do a repair"])
            self.assert_atomic_rejection(lambda: state.submit_critic_verdict(
                self.forum, "Critic", "lean_reopen", "Not failed", review=review, reopen_tasks=["alpha"]))

    def test_bounds_and_whitespace_normalization(self):
        verdict = self.fail(steps=["  Count the chains.  "] + ["x" * 1000] * 7)
        steps = verdict["review"]["requirements"][0]["repair_steps"]
        self.assertEqual(steps[0], "Count the chains.")
        self.assertEqual(len(steps), 8)

    def test_saved_legacy_approval_completes_and_reports_without_rewriting_review(self):
        verdict = state.submit_critic_verdict(self.forum, "Critic", "approved", "Faithful proof",
            review=self.review)["verdict"]
        with state.transaction(self.forum) as current:
            for entry in current["critic_verdicts"][-1]["review"]["requirements"]:
                entry.pop("repair_steps")
        raw_review = deepcopy(self.current()["critic_verdicts"][-1]["review"])
        state.complete_critic_review(self.forum, verdict["snapshot_id"], verdict["verdict_id"])
        accepted = self.current()
        self.assertEqual(autoformalize_report.completion_report(accepted)["status"], "accepted")
        self.assertEqual(accepted["critic_verdicts"][-1]["review"], raw_review)
        self.assertEqual(self.feedback(), {"direct": [], "upstream": []})


class CriticFeedbackHistoryTests(FeedbackFixture):
    def test_full_historical_review_evidence_lists_are_preserved_and_detached(self):
        review = deepcopy(self.review)
        review["requirements"][0]["declarations"] = ["Example.alpha"]
        verdict = self.fail(review=review)
        feedback = self.feedback()["direct"][0]
        entry = verdict["review"]["requirements"][0]
        for key in ("declarations", "checked_anchor_ids", "checked_prerequisite_ids"):
            self.assertEqual(feedback[key], entry[key])
            feedback[key].append("Only caller's returned copy")
            self.assertEqual(self.current()["critic_verdicts"][-1]["review"]["requirements"][0][key], entry[key])

    def test_shared_requirement_explicit_root_not_dependent_or_unrelated(self):
        verdict = self.fail()
        direct = self.feedback()["direct"]
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0]["task_ids"], ["alpha"])
        self.assertEqual(direct[0]["requirement_task_ids"], ["alpha", "beta", "other"])
        self.assertTrue(direct[0]["shared_guidance"])
        self.assertTrue(direct[0]["historical"])
        self.assertEqual(direct[0]["verdict_id"], verdict["verdict_id"])
        self.assertEqual(self.feedback("beta")["direct"], [])
        self.assertEqual(self.feedback("beta")["upstream"][0]["task_ids"], ["alpha"])
        self.assertEqual(self.feedback("other"), {"direct": [], "upstream": []})

    def test_pure_detached_result_does_not_change_state_or_attempt_counters(self):
        self.fail()
        value = self.current()
        before, disk_before = deepcopy(value), state.state_path(self.forum).read_bytes()
        feedback = self.feedback(value=value)
        feedback["direct"][0]["repair_steps"].append("Changed only by caller")
        self.assertEqual(value, before)
        self.assertEqual(state.state_path(self.forum).read_bytes(), disk_before)
        self.assertEqual(len(self.feedback()["direct"][0]["repair_steps"]), 1)
        self.assertEqual(self.current()["phase"], "formalizing")
        self.assertEqual(self.current()["formalization"]["status"], "active")
        with self.assertRaisesRegex(ValueError, "has not been accepted"):
            autoformalize_report.completion_report(self.current())

    def test_legacy_failed_review_still_exposes_rationales_without_inventing_steps(self):
        self.fail()
        value = self.current()
        value["critic_verdicts"][-1]["review"]["requirements"][0].pop("repair_steps")
        entry = self.feedback(value=value)["direct"][0]
        self.assertEqual(entry["repair_steps"], [])
        self.assertTrue(entry["rationale"])
        self.assertTrue(entry["argument_rationale"])

    def test_mechanical_merge_does_not_erase_advice_or_require_matching_main(self):
        verdict = self.fail()
        self.accept("alpha")
        value = self.current()
        self.assertEqual(value["formal_tasks"]["alpha"]["faithfulness"],
                         {"status": "unreviewed", "verdict_id": None})
        value["formalization"]["main_sha"] = "f" * 40
        entry = self.feedback(value=value)["direct"][0]
        self.assertEqual(entry["reviewed_main_sha"], verdict["main_sha"])

    def test_representation_and_proof_edge_refinements_preserve_guidance(self):
        self.fail()
        epoch = self.current()["formalization"]["revision"]
        state.refine_chunks(self.forum, "Ada", self.current()["revision"], {
            "reopen_representations": [{"task_id": "alpha", "reason": "Correct representation."}]})
        self.assertIsNone(self.current()["formal_tasks"]["alpha"]["faithfulness"]["verdict_id"])
        self.assertEqual(len(self.feedback()["direct"]), 1)
        state.refine_chunks(self.forum, "Ada", self.current()["revision"], {
            "upserts": [{"id": "beta", "proof_dependencies": ["alpha", "other"]}]})
        self.assertEqual(self.current()["formalization"]["revision"], epoch)
        self.assertEqual(len(self.feedback("beta")["upstream"]), 1)

    def test_new_pass_supersedes_failure_even_if_other_task_is_reopened(self):
        self.fail()
        self.enter_review()
        state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "Another task needs work",
            review=self.review, reopen_tasks=["other"])
        self.assertEqual(self.feedback(), {"direct": [], "upstream": []})

    def test_not_checked_keeps_last_failure_with_latest_status(self):
        old = self.fail()
        self.enter_review()
        review = deepcopy(self.review)
        review["requirements"][0]["status"] = "not_checked"
        state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "Not reviewed again",
            review=review, reopen_tasks=["alpha"])
        entry = self.feedback()["direct"][0]
        self.assertEqual(entry["verdict_id"], old["verdict_id"])
        self.assertEqual(entry["latest_review_status"], "not_checked")

    def test_omitted_partial_review_does_not_erase_old_failure(self):
        old = self.fail()
        self.enter_review()
        review = {**self.review, "requirements": []}
        state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "Other diagnosis",
            review=review, reopen_tasks=["other"])
        self.assertEqual(self.feedback()["direct"][0]["verdict_id"], old["verdict_id"])

    def test_pass_followed_by_not_checked_does_not_resurrect_older_failure(self):
        self.fail()
        self.enter_review()
        state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "Requirement now faithful",
            review=self.review, reopen_tasks=["other"])
        self.enter_review()
        self.review["requirements"][0]["status"] = "not_checked"
        state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "Not checked this time",
            review=self.review, reopen_tasks=["other"])
        self.assertEqual(self.feedback(), {"direct": [], "upstream": []})

    def test_new_failure_replaces_old_checklist(self):
        self.fail()
        self.enter_review()
        newer = self.fail(steps=["Prove the actual fiber cardinality."])
        entry = self.feedback()["direct"][0]
        self.assertEqual(entry["verdict_id"], newer["verdict_id"])
        self.assertEqual(entry["repair_steps"], ["Prove the actual fiber cardinality."])

    def test_stale_or_corrupt_bound_history_fails_closed(self):
        self.fail()
        for change in ("missing_snapshot", "snapshot_id", "snapshot_digest", "review_id", "main",
                       "source_candidate", "source_hash", "plan_epoch", "source_obligation"):
            with self.subTest(change=change):
                value = self.current()
                verdict = value["critic_verdicts"][-1]
                snapshot = value["review_snapshots"][verdict["snapshot_id"]]
                if change == "missing_snapshot":
                    value["review_snapshots"].clear()
                elif change == "snapshot_id":
                    snapshot["snapshot_id"] = "another-snapshot"
                    verdict["snapshot_sha256"] = state._report_digest(snapshot)
                elif change == "snapshot_digest":
                    snapshot["passed"] = not snapshot["passed"]
                elif change == "review_id":
                    verdict["review"]["snapshot_id"] = "another-snapshot"
                elif change == "main":
                    verdict["main_sha"] = "f" * 40
                elif change == "source_candidate":
                    value["input_source"]["candidate_id"] = "different-source"
                    value["formalization"]["solution_candidate"] = "different-source"
                elif change == "source_hash":
                    value["input_source"]["sha256"] = "f" * 64
                    value["formalization"]["solution_sha256"] = "f" * 64
                elif change == "plan_epoch":
                    value["formalization"]["revision"] += 1
                else:
                    value["formalization"]["requirements"][0]["statement"] = "Different obligation"
                self.assertEqual(self.feedback(value=value), {"direct": [], "upstream": []})

    def test_unknown_retired_and_pending_replan_are_not_actionable(self):
        self.fail()
        self.assertEqual(self.feedback("missing"), {"direct": [], "upstream": []})
        state.request_rechunk(self.forum, "Ada", "Reorganize argument", ["alpha"])
        self.assertEqual(self.feedback(), {"direct": [], "upstream": []})

    def test_completed_real_rechunk_starts_new_feedback_epoch(self):
        self.fail()
        request = state.request_rechunk(self.forum, "Ada", "Reconsider organization", ["alpha"])
        replanning = state.begin_replan(self.forum, request["request_id"])
        self.assertEqual(self.feedback(), {"direct": [], "upstream": []})
        state.initialize_informal_plan(self.forum, self.dag,
            main_sha=replanning["formalization"]["main_sha"],
            contract=deepcopy(replanning["formalization"]["contract"]))
        self.assertEqual(self.feedback(), {"direct": [], "upstream": []})

    def test_explicit_split_and_merge_lineage_inherits_shared_guidance(self):
        self.fail()
        original = next(row for row in self.dag["chunks"] if row["id"] == "alpha")
        state.refine_chunks(self.forum, "Ada", self.current()["revision"], {
            "upserts": [{**original, "id": name} for name in ("left", "right")],
            "replacements": [{"old_ids": ["alpha"], "new_ids": ["left", "right"], "reason": "Split counting work."}]})
        self.assertEqual(self.feedback("alpha"), {"direct": [], "upstream": []})
        for task_id in ("left", "right"):
            entry = self.feedback(task_id)["direct"][0]
            self.assertEqual(entry["task_ids"], ["left", "right"])
            self.assertTrue(entry["lineage_inherited"])
            self.assertTrue(entry["mapping_changed"])
            self.assertTrue(entry["shared_guidance"])
        self.assertEqual(self.feedback("other"), {"direct": [], "upstream": []})
        state.refine_chunks(self.forum, "Ada", self.current()["revision"], {
            "upserts": [{**original, "id": "combined"}],
            "replacements": [{"old_ids": ["left", "right"], "new_ids": ["combined"], "reason": "Integrate steps."}]})
        self.assertEqual(self.feedback("combined")["direct"][0]["task_ids"], ["combined"])
        self.assertEqual(self.feedback("beta")["upstream"][0]["task_ids"], ["combined"])
        value = self.current()
        value["retired_tasks"]["alpha"]["replaced_by"] = ["alpha"]
        self.assertEqual(self.feedback("combined", value), {"direct": [], "upstream": []})


class MultipleRequirementFeedbackTests(FeedbackFixture):
    separate_requirements = True

    def test_explicit_provider_repair_for_failed_consumer_requirement(self):
        self.fail(requirement="R-beta", reopen=["alpha"])
        alpha = self.feedback("alpha")
        self.assertEqual(alpha["upstream"], [])
        self.assertEqual(alpha["direct"][0]["task_ids"], ["alpha"])
        self.assertEqual(alpha["direct"][0]["requirement_id"], "R-beta")
        self.assertEqual(alpha["direct"][0]["requirement_task_ids"], ["beta"])
        self.assertEqual(alpha["direct"][0]["dependency_provider_task_ids"], ["alpha"])
        self.assertEqual(self.feedback("beta")["direct"], [])
        self.assertEqual(self.feedback("beta")["upstream"][0]["requirement_id"], "R-beta")
        self.assertEqual(self.feedback("other"), {"direct": [], "upstream": []})

    def test_explicit_transitive_provider_repair_follows_split_lineage(self):
        self.fail(requirement="R-other", reopen=["alpha"])
        state.refine_chunks(self.forum, "Ada", self.current()["revision"], {
            "upserts": [{"id": "other", "proof_dependencies": ["beta"]}]})
        self.enter_review()
        self.fail(requirement="R-other", reopen=["alpha"])
        self.assertEqual(self.feedback("alpha")["direct"][0]["requirement_id"], "R-other")
        self.assertEqual(self.feedback("other")["upstream"][0]["task_ids"], ["alpha"])
        original = next(row for row in self.dag["chunks"] if row["id"] == "alpha")
        state.refine_chunks(self.forum, "Ada", self.current()["revision"], {
            "upserts": [{**original, "id": name} for name in ("left", "right")],
            "replacements": [{"old_ids": ["alpha"], "new_ids": ["left", "right"], "reason": "Split provider proof."}]})
        for target in ("left", "right"):
            entry = self.feedback(target)["direct"][0]
            self.assertEqual(entry["task_ids"], ["left", "right"])
            self.assertEqual(entry["dependency_provider_task_ids"], ["left", "right"])
            self.assertTrue(entry["lineage_inherited"])
            self.assertTrue(entry["shared_guidance"])
        self.assertEqual(self.feedback("other")["upstream"][0]["task_ids"], ["left", "right"])

    def test_consumer_reopen_does_not_assign_repair_to_its_provider(self):
        self.fail(requirement="R-beta", reopen=["beta"])
        self.assertEqual(self.feedback("alpha"), {"direct": [], "upstream": []})
        entry = self.feedback("beta")["direct"][0]
        self.assertEqual(entry["task_ids"], ["beta"])
        self.assertEqual(entry["dependency_provider_task_ids"], [])
        self.assertEqual(self.feedback("other"), {"direct": [], "upstream": []})

    def test_multiple_failures_route_to_own_explicit_tasks_only(self):
        review = deepcopy(self.review)
        for entry in review["requirements"]:
            if entry["requirement_id"] in {"R-alpha", "R-other"}:
                entry.update(status="fail", repair_steps=["Fix " + entry["requirement_id"]])
        state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "Two independent repairs",
            review=review, reopen_tasks=["alpha", "other"])
        self.assertEqual([row["requirement_id"] for row in self.feedback()["direct"]], ["R-alpha"])
        self.assertEqual([row["requirement_id"] for row in self.feedback("other")["direct"]], ["R-other"])
        beta = self.feedback("beta")
        self.assertEqual(beta["direct"], [])
        self.assertEqual([row["requirement_id"] for row in beta["upstream"]], ["R-alpha"])

    def test_unknown_requirement_mapping_cannot_invent_repair_assignment(self):
        self.fail()
        value = self.current()
        value["critic_verdicts"][-1]["reopen_tasks"] = ["not-a-recorded-task"]
        self.assertEqual(self.feedback(value=value), {"direct": [], "upstream": []})


if __name__ == "__main__":
    unittest.main()
