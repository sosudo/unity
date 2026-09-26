"""Cached-rejection scheduling regressions; real state predicates, no providers."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import unittest
from unittest.mock import patch

from unity import autoformalize_contract as contract, autoformalize_representation as review, autoformalize_state as state
from test_autoformalize_manifest_repair import ManifestStateFixture, output


class RejectedEncodingTests(ManifestStateFixture):
    auto_align = False

    def setUp(self):
        super().setUp()
        with state.transaction(self.forum) as current:
            value = current["formalization"]["contract"]
            value["representation_review_policy"] = 1
            value["sha256"] = state._contract_digest(value)
            review.queue_representation_review(current, "alpha")

    def reject(self):
        claimed = review.claim_representation_review(self.forum, "alpha", "Grace")
        self.assertEqual(claimed["status"], "claimed")
        payload = review.representation_review_input(self.current(), "alpha")
        verdict = {"input_sha256": payload["input_sha256"], "verdict": "encoding_error",
                   "checked_anchor_ids": [row["id"] for row in payload["anchors"]],
                   "rationale": "The exported theorem omits the exact cardinality condition.",
                   "evidence": "The source requires exactly h nonempty parts, not just a partition."}
        return review.submit_representation_review(self.forum, "Grace", "alpha", verdict)

    def readopt(self):
        rejected = self.reject()
        self.assertEqual(self.current()["formal_tasks"]["alpha"]["representation"]["status"], "stale")
        candidate = self.accept("alpha")
        return rejected, candidate, state.current_manifest_repairs(self.current(), "alpha")[0]

    def available(self, repair, author="Ada", current=None):
        return state.repair_available_to(current or self.current(), author, "alpha",
                                         repair["repair_id"], repair["input_sha256"])

    def test_same_statement_readoption_retains_rejection_not_acceptance(self):
        rejected, candidate, repair = self.readopt()
        current = self.current()
        task = current["formal_tasks"]["alpha"]
        self.assertEqual(review.current_representation_review(current, "alpha"), rejected)
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["accepted_candidate"])
        self.assertEqual(task["faithfulness"]["status"], "changes_requested")
        self.assertEqual(task["verification"]["status"], "verified")
        self.assertEqual(task["verification"]["candidate_id"], candidate["candidate_id"])
        self.assertEqual(current["formal_candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertNotEqual(current["strategies"][candidate["strategy_id"]]["status"], "succeeded")
        self.assertFalse(state.interface_available(current, "alpha"))
        self.assertFalse(state.task_ready(current, "alpha"))
        self.assertFalse(state.task_ready(current, "beta"))
        self.assertTrue(self.available(repair))

    def test_reconciliation_is_idempotent_and_preserves_proof_receipts(self):
        _, _, repair = self.readopt()
        before = self.current()
        after = state.reconcile_rejected_representations(self.forum)
        self.assertEqual(before, after)
        self.assertEqual(state.reconcile_rejected_representations(self.forum), after)
        self.assertEqual(state.current_manifest_repairs(after, "alpha"), [repair])

    def test_round_diagnostics_expose_rejected_repair_and_blocked_dependent(self):
        _, _, repair = self.readopt()
        current = state.record_round_end(self.forum, blocked_launches={}, activity={})
        summary = current["formalization"]["last_round"]
        self.assertEqual(summary["outcome"], "blocked")
        rows = {row["task_id"]: row for row in summary["pending_tasks"]}
        self.assertEqual(rows["alpha"]["representation_status"], "encoding_error")
        self.assertEqual(rows["alpha"]["repairs"], [{"repair_id": repair["repair_id"], "status": "open"}])
        self.assertEqual(rows["beta"]["unavailable_dependencies"], ["alpha"])
        self.assertFalse(state.interface_available(current, "alpha"))

    def test_kernel_receipt_and_repair_never_approve_rejected_encoding(self):
        self.readopt()
        self.assert_atomic_rejection(lambda: state.record_review_snapshot(self.forum, self.report()))
        self.assertIsNone(self.current()["formalization"]["review_snapshot"])
        self.assertNotEqual(self.current()["formalization"]["status"], "accepted")

    def test_adopted_source_repair_changes_make_old_encoding_permission_stale(self):
        _, _, repair = self.readopt()
        changed = self.current()
        changed["formalization"]["spec"]["arguments"][0]["repair_ids"] = ["repair-new"]
        changed["source_repairs"]["repair-new"] = {"sha256": "a" * 64}
        self.assertFalse(self.available(repair, current=changed))

    def test_persisted_bad_complete_state_is_recovered_without_new_review(self):
        rejected, candidate, _ = self.readopt()
        with state.transaction(self.forum) as current:
            current["manifest_repairs"] = {}
            current["formal_tasks"]["alpha"].update(status="complete", accepted_candidate=candidate["candidate_id"])
        recovered = state.reconcile_rejected_representations(self.forum)
        self.assertEqual(review.current_representation_review(recovered, "alpha"), rejected)
        self.assertEqual(recovered["formal_tasks"]["alpha"]["status"], "pending")
        self.assertIsNone(recovered["formal_tasks"]["alpha"]["accepted_candidate"])
        self.assertEqual(len(state.current_manifest_repairs(recovered, "alpha")), 1)

    def test_same_semantic_budget_survives_main_revision_and_feedback(self):
        _, _, repair = self.readopt()
        self.assertEqual(state.begin_manifest_repair_attempt(self.forum, repair["repair_id"], "Ada",
                         input_sha256=repair["input_sha256"])["status"], "started")
        state.finish_manifest_repair_attempt(self.forum, repair["repair_id"], "Ada", "yielded")
        with state.transaction(self.forum) as current:
            current["formalization"]["main_sha"] = "1" * 40
            current["formal_tasks"]["alpha"]["revision"] += 1
            state._record_manifest_repair(current, "alpha", [{"code": "critic_representation_repair",
                                         "message": "Another wording of the same rejection."}], origin="critic")
        refreshed = state.current_manifest_repairs(self.current(), "alpha")[0]
        self.assertEqual(refreshed["repair_id"], repair["repair_id"])
        self.assertEqual(refreshed["budget_sha256"], repair["budget_sha256"])
        self.assertNotEqual(refreshed["input_sha256"], repair["input_sha256"])
        self.assertFalse(self.available(repair, "Grace"))
        self.assertFalse(self.available(refreshed, "Ada"))
        self.assertTrue(self.available(refreshed, "Grace"))

    def test_exhausted_budget_is_not_reopened_by_unrelated_commit(self):
        _, _, repair = self.readopt()
        state.mark_manifest_repair_exhausted(self.forum, repair["repair_id"])
        with state.transaction(self.forum) as current:
            current["formalization"]["main_sha"] = "2" * 40
        state.reconcile_rejected_representations(self.forum)
        refreshed = state.current_manifest_repairs(self.current(), "alpha")[0]
        self.assertEqual(refreshed["status"], "exhausted")
        self.assertFalse(self.available(refreshed))

    def test_unrelated_merge_refreshes_permission_without_new_semantic_budget(self):
        _, _, repair = self.readopt()
        with state.transaction(self.forum) as current:
            current["formalization"]["main_sha"] = "9" * 40
        self.assertEqual(state.current_manifest_repairs(self.current(), "alpha"), [])
        refreshed_state = state.reconcile_rejected_representations(self.forum)
        refreshed = state.current_manifest_repairs(refreshed_state, "alpha")[0]
        self.assertEqual(refreshed["repair_id"], repair["repair_id"])
        self.assertEqual(refreshed["budget_sha256"], repair["budget_sha256"])
        self.assertNotEqual(refreshed["input_sha256"], repair["input_sha256"])
        self.assertTrue(self.available(refreshed))
        self.assertFalse(state.task_ready(refreshed_state, "beta"))

    def test_current_legacy_diagnostics_coalesce_without_erasing_attempts(self):
        _, _, repair = self.readopt()
        with state.transaction(self.forum) as current:
            rows = {}
            for number, author in enumerate(("Ada", "Grace")):
                row = deepcopy(repair)
                row.pop("budget_sha256")
                row.update(repair_id="legacy-" + str(number), input_sha256=state.digest(author),
                           kind="output_manifest", attempts=[{"author": author, "status": "yielded"}])
                rows[row["repair_id"]] = row
            current["manifest_repairs"] = rows
        current = state.reconcile_rejected_representations(self.forum)
        repairs = state.current_manifest_repairs(current, "alpha")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(len(repairs[0]["attempts"]), 2)
        self.assertFalse(self.available(repairs[0], "Ada"))
        self.assertFalse(self.available(repairs[0], "Grace"))
        self.assertTrue(self.available(repairs[0], "Blaise"))

    def test_initial_rejection_keeps_kernel_dependency_invalidation(self):
        with state.transaction(self.forum) as current:
            value = current["formalization"]["contract"]
            value["targets"]["Example.other"]["meaning_dependencies"] = ["Example.alpha"]
            value["sha256"] = state._contract_digest(value)
        self.reject()
        current = self.current()
        self.assertEqual(current["formal_tasks"]["other"]["representation"]["status"], "stale")
        self.assertNotIn("other", current["formalization"]["contract"]["bindings"])

    def test_claim_is_exclusive_under_state_lock(self):
        _, _, repair = self.readopt()
        def claim(author):
            return state.begin_manifest_repair_attempt(self.forum, repair["repair_id"], author,
                                                       input_sha256=repair["input_sha256"])["status"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(claim, ["Ada", "Grace"]))
        self.assertEqual(statuses.count("started"), 1)
        self.assertEqual(statuses.count("unavailable"), 1)

    def test_stale_permissions_fail_without_spending_attempt(self):
        _, _, repair = self.readopt()
        with state.transaction(self.forum) as current:
            current["formalization"]["main_sha"] = "3" * 40
        result = state.begin_manifest_repair_attempt(self.forum, repair["repair_id"], "Ada",
                                                     input_sha256=repair["input_sha256"])
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.current()["manifest_repairs"][repair["repair_id"]]["attempts"], [])

    def test_exact_repair_permission_invalidates_on_each_bound_identity(self):
        _, _, repair = self.readopt()
        original = self.current()
        mutations = {
            "run": lambda value: value.update(run_id="another-run"),
            "source": lambda value: value["input_source"].update(sha256="8" * 64),
            "revision": lambda value: value["formal_tasks"]["alpha"].update(revision=999),
            "interpretation": lambda value: value["formal_tasks"]["alpha"].update(interpretation_sha256="7" * 64),
            "outputs": lambda value: value["formal_tasks"]["alpha"].update(outputs=[output("replacement")]),
        }
        for name, mutate in mutations.items():
            with self.subTest(identity=name):
                changed = deepcopy(original)
                mutate(changed)
                self.assertFalse(self.available(repair, current=changed))
        with patch.object(review, "_policy_hash", return_value="new-review-policy"):
            self.assertFalse(self.available(repair, current=original))
        self.assertEqual(self.current(), original)

    def test_explicit_reopen_of_same_bad_statement_retains_attempt_budget(self):
        _, _, repair = self.readopt()
        state.begin_manifest_repair_attempt(self.forum, repair["repair_id"], "Ada",
                                            input_sha256=repair["input_sha256"])
        state.finish_manifest_repair_attempt(self.forum, repair["repair_id"], "Ada", "yielded")
        state.refine_chunks(self.forum, "Ada", self.current()["revision"], {
            "reopen_representations": [{"task_id": "alpha", "reason": "Revise the rejected encoding."}],
        })
        self.accept("alpha")
        refreshed = state.current_manifest_repairs(self.current(), "alpha")[0]
        self.assertEqual(refreshed["budget_sha256"], repair["budget_sha256"])
        self.assertFalse(self.available(refreshed, "Ada"))
        self.assertTrue(self.available(refreshed, "Grace"))
        self.assertFalse(state.task_ready(self.current(), "beta"))

    def test_upstream_rejection_and_source_issue_remain_hard_gates(self):
        _, _, repair = self.readopt()
        current = self.current()
        before = deepcopy(current)
        current["formal_tasks"]["alpha"]["proof_dependencies"] = ["other"]
        # Other has an adopted interface but no aligned policy-1 review.
        self.assertFalse(self.available(repair, current=current))
        self.assertEqual(before, self.current())
        current = self.current()
        current["source_issues"]["issue"] = {"status": "open", "task_ids": ["alpha"],
                "source_candidate": self.paper["candidate_id"], "source_sha256": self.paper["sha256"]}
        self.assertFalse(self.available(repair, current=current))

    def test_reported_supplied_source_issue_blocks_repair_without_spending_budget(self):
        _, _, repair = self.readopt()
        before = self.current()
        payload = review.representation_review_input(before, "alpha")
        issue = state.report_source_issue(self.forum, "Grace",
            [row["id"] for row in payload["anchors"]],
            "The supplied source needs independent diagnosis.", task_ids=["alpha"])
        current = self.current()
        self.assertEqual(issue["source_candidate"], self.source["candidate_id"])
        self.assertEqual(issue["source_sha256"], self.source["sha256"])
        self.assertFalse(self.available(repair, current=current))
        self.assertEqual(current["manifest_repairs"][repair["repair_id"]]["attempts"], [])
        self.assertEqual(current["input_source"], before["input_source"])
        self.assertEqual(current["formalization"]["source_obligations"],
                         before["formalization"]["source_obligations"])

    def test_pending_candidate_in_dependent_scope_blocks_repair(self):
        _, _, repair = self.readopt()
        current = self.current()
        current["formal_candidates"]["pending"] = {
            "task_id": "beta", "status": "submitted",
            "task_revision": current["formal_tasks"]["beta"]["revision"],
            "solution_candidate": self.paper["candidate_id"], "solution_sha256": self.paper["sha256"]}
        self.assertFalse(self.available(repair, current=current))

    def test_repair_permission_does_not_bypass_exact_kernel_receipts(self):
        _, _, repair = self.readopt()
        self.assertTrue(self.available(repair))
        strategy = self.claim("alpha")
        candidate = state.submit_formal_candidate(self.forum, strategy, "Ada", "alpha", "e" * 40,
            self.current()["formalization"]["main_sha"], "f" * 64, outputs=[output("alpha")])["candidate"]
        state.begin_formal_merge(self.forum, candidate["candidate_id"])
        value = self.current()["formalization"]["contract"]
        with self.assertRaisesRegex(ValueError, "exact task-local kernel receipts"):
            state.finish_formal_merge(self.forum, candidate["candidate_id"], success=True,
                main_sha="e" * 40, verification={"status": "passed", "contract_sha256": value["sha256"],
                "policy_sha256": contract.policy_hash(), "verified_targets": {}})

    def test_corrected_semantics_require_fresh_alignment_before_dependents_wake(self):
        _, _, repair = self.readopt()
        state.refine_chunks(self.forum, "Ada", self.current()["revision"], {
            "reopen_representations": [{"task_id": "alpha", "reason": "Export the missing cardinality statement."}],
        })
        self.accept("alpha", outputs=[output("alpha"), output("exactCardinality")])
        fresh = review.current_representation_review(self.current(), "alpha")
        self.assertEqual(fresh["status"], "pending")
        self.assertNotEqual(fresh["input_sha256"], repair["evidence"]["representation_input_sha256"])
        self.assertFalse(self.available(repair))
        self.assertFalse(state.task_ready(self.current(), "beta"))
        review.claim_representation_review(self.forum, "alpha", "Grace")
        payload = review.representation_review_input(self.current(), "alpha")
        review.submit_representation_review(self.forum, "Grace", "alpha", {
            "input_sha256": payload["input_sha256"], "verdict": "aligned",
            "checked_anchor_ids": [row["id"] for row in payload["anchors"]],
            "rationale": "The revised statement now has all required quantifiers.",
            "evidence": "Each source condition has a corresponding Lean hypothesis/conclusion."})
        self.assertTrue(state.task_ready(self.current(), "beta"))


if __name__ == "__main__":
    unittest.main()
