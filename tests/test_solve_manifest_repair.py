"""Solve-owned manifest repair contracts, without Lean, models, or remote calls."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from unity import solve_contract as contract, solve_state as state
from unity.solve_spec import digest
from solve_port_fixtures import informal_dag, machine_snapshot, semantic_evidence


def output(name, file=None):
    return {"declaration": "Example." + name, "file": file or f"Example/{name}.lean"}


class OutputManifestBlockerTests(unittest.TestCase):
    def setUp(self):
        self.contract = {
            "version": 3, "obligation_ids": ["alpha", "other"],
            "bindings": {"alpha": [output("alpha")], "other": [output("other")]},
            "targets": {"Example.alpha": {}, "Example.other": {}},
        }

    def check(self, outputs, *, value=None, task_id="alpha"):
        return contract.output_manifest_blockers(
            self.contract if value is None else value,
            task_ids={"alpha", "other"}, task_id=task_id, proposed_outputs=outputs,
        )

    def test_unchanged_manifest_is_order_independent_and_does_not_mutate(self):
        self.contract["bindings"]["alpha"].append(output("helper"))
        self.contract["targets"]["Example.helper"] = {}
        proposed = list(reversed(self.contract["bindings"]["alpha"]))
        before = deepcopy((self.contract, proposed))
        self.assertEqual(self.check(proposed), [])
        self.assertEqual((self.contract, proposed), before)

    def test_exact_output_error_codes_and_no_ownership_repair(self):
        variants = [
            ([], "output_manifest_invalid"),
            ([{"declaration": "Example.alpha"}], "output_manifest_invalid"),
            ([{**output("alpha"), "extra": "ignored?"}], "output_manifest_invalid"),
            ([{"declaration": " Example.alpha", "file": "Example/alpha.lean"}], "output_manifest_invalid"),
            ([{"declaration": "-alpha", "file": "Example/alpha.lean"}], "output_manifest_invalid"),
            ([output("alpha"), output("alpha")], "output_manifest_duplicate"),
            ([output("other")], "output_ownership_conflict"),
            ([output("renamed")], "output_manifest_changed"),
            ([output("alpha", "Moved.lean")], "output_manifest_changed"),
            ([output("alpha"), output("helper")], "output_manifest_changed"),
        ]
        for proposed, code in variants:
            with self.subTest(code=code, proposed=proposed):
                before = deepcopy(self.contract)
                rows = self.check(proposed)
                self.assertEqual([row["code"] for row in rows], [code])
                self.assertEqual(rows[0]["task_ids"], ["alpha"])
                self.assertIs(rows[0]["deterministic"], True)
                self.assertEqual(self.contract, before)

    def test_unknown_task_and_corrupt_adopted_bindings_fail_closed(self):
        self.assertEqual(self.check([output("alpha")], task_id="missing")[0]["code"],
                         "output_manifest_invalid")
        for change in ("targets", "targets_not_mapping", "duplicate_owner", "unknown_owner", "empty"):
            with self.subTest(change=change):
                value = deepcopy(self.contract)
                if change == "targets":
                    del value["targets"]["Example.alpha"]
                elif change == "targets_not_mapping":
                    value["targets"] = list(value["targets"])
                elif change == "duplicate_owner":
                    value["bindings"]["other"] = [output("alpha")]
                elif change == "unknown_owner":
                    value["bindings"]["missing"] = value["bindings"].pop("other")
                else:
                    value["bindings"]["alpha"] = []
                self.assertEqual(self.check([output("alpha")], value=value)[0]["code"],
                                 "contract_bindings_invalid")

    def test_unadopted_task_may_propose_new_outputs_without_adopting_them(self):
        value = deepcopy(self.contract)
        del value["bindings"]["alpha"]
        del value["targets"]["Example.alpha"]
        before = deepcopy(value)
        self.assertEqual(self.check([output("alpha"), output("helper")], value=value), [])
        self.assertEqual(value, before)


class ManifestStateFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="solve-manifest-state-")
        self.addCleanup(temporary.cleanup)
        self.forum = Path(temporary.name) / ".unity/forum"
        state.initialize(self.forum, "a" * 64, "b" * 40)
        self.paper = state.submit_solution_candidate(
            self.forum, "Writer", "paper", "c" * 64, "PROOF.tex",
        )["candidate"]
        state.review_solution_candidate(self.forum, self.paper["candidate_id"],
                                        "Reviewer", "approve", "Checked the mathematics.")
        state.accept_solution_candidate(self.forum, self.paper["candidate_id"], "Unity")
        ref = f"paper:{self.paper['candidate_id']}"
        chunks = [{"id": name, "lean_decl": "Example." + name,
                   "lean_file": f"Example/{name}.lean", "source_components": [ref],
                   "dependencies": []} for name in ("alpha", "beta", "other")]
        self.dag = informal_dag(self.paper, chunks)
        self.dag["chunks"][1]["statement_dependencies"] = ["alpha"]
        value = {
            "version": 3, "bindings": {}, "targets": {}, "external_declarations": {},
            "prerequisite_declarations": {}, "environment": {},
            "solution_candidate": self.paper["candidate_id"], "solution_sha256": self.paper["sha256"],
            "requirements": deepcopy(self.dag["requirements"]), "spec": deepcopy(self.dag["spec"]),
            "spec_sha256": digest(self.dag["spec"]), "obligation_ids": ["alpha", "beta", "other"],
        }
        value["sha256"] = state._contract_digest(value)
        state.initialize_informal_plan(self.forum, self.dag, main_sha="b" * 40, contract=value)
        self.accept("other")
        self.adopted = self.accept("alpha", stage="representation")
        self.strategy = self.claim("alpha")
        self.outputs = [output("alpha")]
        self.changed = [*self.outputs, output("helper")]

    def current(self):
        return state.load_state(self.forum)

    def claim(self, task_id, author="Ada"):
        strategy = state.register_strategy(self.forum, author, "Prove " + task_id, target=task_id)["strategy"]
        state.claim_strategy(self.forum, strategy["strategy_id"], author)
        return strategy["strategy_id"]

    def accept(self, task_id, *, stage="complete", outputs=None):
        author = "Ada" if task_id == "alpha" else task_id.capitalize()
        strategy = self.claim(task_id, author)
        candidate = state.submit_formal_candidate(
            self.forum, strategy, author, task_id, "d" * 40,
            self.current()["formalization"]["main_sha"], "e" * 64,
            stage=stage, outputs=outputs or [output(task_id)],
        )["candidate"]
        state.begin_formal_merge(self.forum, candidate["candidate_id"])
        proposed = deepcopy(self.current()["formalization"]["contract"])
        proposed["bindings"][task_id] = deepcopy(candidate["outputs"])
        for row in candidate["outputs"]:
            proposed["targets"][row["declaration"]] = {
                "fingerprint": digest(row["declaration"]),
                "meaning_dependencies": [row["declaration"]],
            }
        proposed["sha256"] = state._contract_digest(proposed)
        verification = {
            "status": "passed", "contract_sha256": proposed["sha256"],
            "policy_sha256": contract.policy_hash(),
            "verified_targets": {} if stage == "representation" else {
                row["declaration"]: digest(row["declaration"]) for row in candidate["outputs"]},
        }
        state.finish_formal_merge(self.forum, candidate["candidate_id"], success=True,
                                  main_sha="d" * 40, verification=verification, proposed_contract=proposed)
        return self.current()["formal_candidates"][candidate["candidate_id"]]

    def preflight(self, outputs=None, **kwargs):
        return state.preflight_formal_submission(
            self.forum, self.strategy, "Ada", "alpha",
            outputs=self.changed if outputs is None else outputs, **kwargs,
        )

    def submit(self, outputs=None, **kwargs):
        return state.submit_formal_candidate(
            self.forum, self.strategy, "Ada", "alpha", "f" * 40,
            self.current()["formalization"]["main_sha"], "1" * 64,
            outputs=self.changed if outputs is None else outputs, **kwargs,
        )

    def assert_atomic_rejection(self, operation):
        before = state.state_path(self.forum).read_bytes()
        with self.assertRaises((ValueError, ValidationError)):
            operation()
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def assert_only_diagnostics_changed(self, before):
        after = self.current()
        for key in set(before) | set(after):
            if key not in {"revision", "events", "manifest_repairs"}:
                self.assertEqual(after.get(key), before.get(key), key)


class ManifestRepairStateTests(ManifestStateFixture):
    def test_preflight_records_only_diagnostic_without_candidate_or_review_mutation(self):
        before = self.current()
        result = self.preflight()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual([row["code"] for row in result["blockers"]], ["output_manifest_changed"])
        self.assert_only_diagnostics_changed(before)
        self.assertEqual(len(state.current_manifest_repairs(self.current(), "alpha")), 1)
        self.assertEqual(self.current()["formal_tasks"]["other"]["status"], "complete")

    def test_repeated_preflight_and_submission_deduplicate_exact_state_bytes(self):
        first = self.preflight()
        before = state.state_path(self.forum).read_bytes()
        with patch.object(state, "_write_unlocked", wraps=state._write_unlocked) as write:
            second = self.preflight()
            submitted = self.submit()
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(submitted["status"], "blocked")
        self.assertEqual(first["repair"]["repair_id"], second["repair"]["repair_id"])
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)
        write.assert_not_called()

    def test_direct_submission_cannot_bypass_preflight(self):
        before = self.current()
        result = self.submit()
        self.assertEqual(result["status"], "blocked")
        self.assert_only_diagnostics_changed(before)
        self.assertEqual(len(state.current_manifest_repairs(self.current())), 1)

    def test_failed_valid_submission_reopens_same_cleared_repair_without_resetting_attempts(self):
        first = self.preflight()["repair"]
        state.begin_manifest_repair_attempt(self.forum, first["repair_id"], "Bert")
        state.finish_manifest_repair_attempt(self.forum, first["repair_id"], "Bert", "yielded")
        attempts = deepcopy(state.current_manifest_repairs(self.current())[0]["attempts"])
        candidate = self.submit(self.outputs)["candidate"]
        self.assertEqual(state.current_manifest_repairs(self.current()), [])
        state.begin_formal_merge(self.forum, candidate["candidate_id"])
        state.finish_formal_merge(self.forum, candidate["candidate_id"], success=False,
                                  error="Ordinary build failure with unchanged adopted state.")
        second = self.preflight()["repair"]
        self.assertEqual(second["repair_id"], first["repair_id"])
        self.assertEqual(second["input_sha256"], first["input_sha256"])
        self.assertEqual(second["status"], "open")
        self.assertEqual(second["attempts"], attempts)
        self.assertEqual(state.current_manifest_repairs(self.current()), [second])
        before = state.state_path(self.forum).read_bytes()
        self.preflight()
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_new_manifest_input_wakes_yielded_author_but_attempt_telemetry_does_not(self):
        state.yield_task(self.forum, "Bert", "alpha", "No untried ordinary proof approach.")
        self.assertFalse(state.task_available_to(self.current(), "Bert", "alpha"))
        repair = self.preflight()["repair"]
        self.assertTrue(state.task_available_to(self.current(), "Bert", "alpha"))
        state.yield_task(self.forum, "Bert", "alpha", "This exact repair needs another approach.")
        progress = state._attempt_progress(self.current(), "Bert", "alpha")
        self.assertFalse(state.task_available_to(self.current(), "Bert", "alpha"))
        state.begin_manifest_repair_attempt(self.forum, repair["repair_id"], "Ada")
        self.assertEqual(state._attempt_progress(self.current(), "Bert", "alpha"), progress)
        state.finish_manifest_repair_attempt(self.forum, repair["repair_id"], "Ada", "yielded")
        self.assertEqual(state._attempt_progress(self.current(), "Bert", "alpha"), progress)
        state.mark_manifest_repair_exhausted(self.forum, repair["repair_id"])
        self.assertEqual(state._attempt_progress(self.current(), "Bert", "alpha"), progress)
        self.assertFalse(state.task_available_to(self.current(), "Bert", "alpha"))
        self.preflight([*self.outputs, output("different")])
        self.assertTrue(state.task_available_to(self.current(), "Bert", "alpha"))

    def test_state_and_contract_manifest_blockers_agree_without_inspection(self):
        current = self.current()
        value = current["formalization"]["contract"]
        with patch.object(contract, "inspect_environment", side_effect=AssertionError("inspection")), \
             patch.object(contract, "workspace_layout", side_effect=AssertionError("layout discovery")):
            expected = contract.output_manifest_blockers(
                value, task_ids=set(current["formal_tasks"]), task_id="alpha", proposed_outputs=self.changed)
            actual = state.submission_blockers(current, "alpha", outputs=self.changed)
            report = contract.check_formal_contract(
                Path("/unused"), value, list(current["formal_tasks"].values()),
                completed={"other"}, task_id="alpha", proposed_outputs=self.changed,
            )
        self.assertEqual(actual, expected)
        self.assertFalse(report["passed"])
        self.assertEqual(report["blockers"], expected)

    def test_valid_preflight_is_read_only_and_context_allows_exact_submission(self):
        before = state.state_path(self.forum).read_bytes()
        result = self.preflight(self.outputs)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)
        submitted = self.submit(self.outputs, submission_context=result["context"])
        self.assertEqual(submitted["status"], "submitted")

    def test_changed_outputs_cannot_reuse_successful_submission_context(self):
        result = self.preflight(self.outputs)
        before = self.current()
        submitted = self.submit(submission_context=result["context"])
        self.assertIn(submitted["status"], {"retry", "blocked"})
        self.assertEqual(self.current()["formal_candidates"], before["formal_candidates"])

    def test_changed_main_after_preflight_returns_retry_without_publication(self):
        result = self.preflight(self.outputs)
        with state.transaction(self.forum) as current:
            current["formalization"]["main_sha"] = "2" * 40
        before = state.state_path(self.forum).read_bytes()
        submitted = self.submit(self.outputs, submission_context=result["context"])
        self.assertEqual(submitted["status"], "retry")
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_current_queued_candidate_has_priority_over_new_mismatch(self):
        candidate = self.submit(self.outputs)["candidate"]
        before = state.state_path(self.forum).read_bytes()
        preflight = self.preflight()
        submitted = self.submit()
        for result in (preflight, submitted):
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(result["candidate"]["candidate_id"], candidate["candidate_id"])
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)
        self.assertEqual(state.current_manifest_repairs(self.current()), [])

    def test_parallel_mismatches_publish_one_contextual_repair(self):
        before = self.current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.preflight(), range(2)))
        self.assertEqual([row["status"] for row in results], ["blocked", "blocked"])
        self.assertEqual(results[0]["repair"]["repair_id"], results[1]["repair"]["repair_id"])
        self.assertEqual(len(state.current_manifest_repairs(self.current())), 1)
        self.assert_only_diagnostics_changed(before)

    def test_repair_context_stales_on_each_bound_identity(self):
        self.preflight()
        original = self.current()
        mutations = {
            "run": lambda current: current.update(run_id="another-run"),
            "main": lambda current: current["formalization"].update(main_sha="2" * 40),
            "contract": lambda current: current["formalization"]["contract"].update(sha256="3" * 64),
            "task_revision": lambda current: current["formal_tasks"]["alpha"].update(revision=999),
            "source": lambda current: current["solution_candidates"][self.paper["candidate_id"]].update(sha256="4" * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = deepcopy(original)
                mutate(changed)
                self.assertEqual(state.current_manifest_repairs(changed), [])
                self.assertEqual(len(state.current_manifest_repairs(original)), 1)

    def test_changed_manifest_gets_distinct_diagnostic_identity(self):
        first = self.preflight()["repair"]
        second = self.preflight([*self.outputs, output("different")])["repair"]
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])

    def test_repair_attempt_claim_is_exclusive_and_stale_request_cannot_start(self):
        repair = self.preflight()["repair"]
        first = state.begin_manifest_repair_attempt(self.forum, repair["repair_id"], "Ada")
        second = state.begin_manifest_repair_attempt(self.forum, repair["repair_id"], "Bert")
        self.assertEqual(first["status"], "started")
        self.assertEqual(second["status"], "conflict")
        with state.transaction(self.forum) as current:
            current["formalization"]["main_sha"] = "2" * 40
        before = state.state_path(self.forum).read_bytes()
        stale = state.begin_manifest_repair_attempt(self.forum, repair["repair_id"], "Bert")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_finished_attempt_is_not_restarted_and_exhaustion_is_idempotent(self):
        repair = self.preflight()["repair"]
        repair_id = repair["repair_id"]
        self.assertEqual(state.begin_manifest_repair_attempt(self.forum, repair_id, "Ada")["status"], "started")
        before = state.state_path(self.forum).read_bytes()
        state.finish_manifest_repair_attempt(self.forum, repair_id, "Intruder", "yielded")
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)
        state.finish_manifest_repair_attempt(self.forum, repair_id, "Ada", "yielded")
        before = state.state_path(self.forum).read_bytes()
        state.finish_manifest_repair_attempt(self.forum, repair_id, "Ada", "yielded")
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)
        self.assertEqual(state.begin_manifest_repair_attempt(self.forum, repair_id, " ADA ")["status"], "attempted")
        state.mark_manifest_repair_exhausted(self.forum, repair_id)
        before = state.state_path(self.forum).read_bytes()
        state.mark_manifest_repair_exhausted(self.forum, repair_id)
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)
        self.assertEqual(state.current_manifest_repairs(self.current())[0]["status"], "exhausted")
        self.assertEqual(state.begin_manifest_repair_attempt(self.forum, repair_id, "Bert")["status"], "stale")

    def test_invalid_owner_and_malformed_outputs_are_atomic(self):
        self.assert_atomic_rejection(lambda: state.preflight_formal_submission(
            self.forum, self.strategy, "Intruder", "alpha", outputs=self.changed))
        for outputs, code in (([output("other")], "output_ownership_conflict"),
                              ([output("alpha"), output("alpha")], "output_manifest_duplicate")):
            with self.subTest(code=code):
                before = state.state_path(self.forum).read_bytes()
                result = self.preflight(outputs)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["blockers"][0]["code"], code)
                self.assertEqual(state.state_path(self.forum).read_bytes(), before)
        before = state.state_path(self.forum).read_bytes()
        result = self.submit([output("other")])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blockers"][0]["code"], "output_ownership_conflict")
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)
        self.assert_atomic_rejection(lambda: self.submit([output("alpha"), output("alpha")]))
        self.assertEqual(state.current_manifest_repairs(self.current()), [])

    def test_explicit_reopen_invalidates_dependents_but_preserves_unrelated_evidence(self):
        self.accept("beta")
        self.preflight()
        before = self.current()
        result = state.refine_chunks(self.forum, "Ada", before["revision"], {
            "reopen_representations": [{"task_id": "alpha", "reason": "Correct the output manifest."}],
        })
        after = self.current()
        self.assertEqual(result["affected_tasks"], ["alpha", "beta"])
        self.assertEqual(after["formal_tasks"]["other"], before["formal_tasks"]["other"])
        self.assertEqual(after["solution"], before["solution"])
        self.assertEqual(after["formal_candidates"][self.adopted["candidate_id"]], self.adopted)
        self.assertEqual(after["formal_tasks"]["alpha"]["informal_statement"],
                         before["formal_tasks"]["alpha"]["informal_statement"])
        self.assertNotIn("alpha", after["formalization"]["contract"]["bindings"])
        self.assertEqual(state.current_manifest_repairs(after), [])

    def test_stale_revision_reopen_is_atomic_and_old_candidate_cannot_merge(self):
        candidate = self.submit(self.outputs)["candidate"]
        before = self.current()
        changes = {"reopen_representations": [{"task_id": "alpha", "reason": "Correct manifest."}]}
        conflict = state.refine_chunks(self.forum, "Ada", before["revision"] - 1, changes)
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(self.current(), before)
        state.refine_chunks(self.forum, "Ada", before["revision"], changes)
        self.assertFalse(state.candidate_is_current(self.current(), candidate))
        before = state.state_path(self.forum).read_bytes()
        self.assertTrue(state.begin_formal_merge(self.forum, candidate["candidate_id"])["conflict"])
        self.assertEqual(state.state_path(self.forum).read_bytes(), before)

    def test_unchanged_complete_submission_still_requires_exact_kernel_receipts(self):
        self.preflight()
        candidate = self.submit(self.outputs)["candidate"]
        self.assertEqual(candidate["stage"], "complete")
        state.begin_formal_merge(self.forum, candidate["candidate_id"])
        current = self.current()
        receipt = {"status": "passed", "policy_sha256": contract.policy_hash(),
                   "contract_sha256": current["formalization"]["contract"]["sha256"]}
        self.assert_atomic_rejection(lambda: state.finish_formal_merge(
            self.forum, candidate["candidate_id"], success=True, main_sha="f" * 40,
            build={"returncode": 0}, verification=receipt))
        self.assertEqual(self.current()["formal_tasks"]["alpha"]["verification"]["status"], "pending")

    def test_reopened_manifest_is_readopted_only_with_fresh_task_local_receipts(self):
        self.preflight()
        before = self.current()
        state.refine_chunks(self.forum, "Ada", before["revision"], {
            "reopen_representations": [{"task_id": "alpha", "reason": "Declare the checked helper output."}],
        })
        repaired = self.accept("alpha", outputs=self.changed)
        after = self.current()
        self.assertNotEqual(repaired["task_revision"], self.adopted["task_revision"])
        self.assertEqual(after["formalization"]["contract"]["bindings"]["alpha"], self.changed)
        self.assertEqual(after["formal_tasks"]["alpha"]["verification"]["verified_targets"], {
            row["declaration"]: digest(row["declaration"]) for row in self.changed
        })
        self.assertEqual(after["formal_tasks"]["other"], before["formal_tasks"]["other"])
        self.assertEqual(after["formal_candidates"][self.adopted["candidate_id"]], self.adopted)
        self.assertEqual(state.current_manifest_repairs(after), [])
        self.assertEqual(after["phase"], "formalizing")
        self.assertNotEqual(after["formalization"]["status"], "accepted")


class ManifestRepairCriticTests(ManifestStateFixture):
    def setUp(self):
        super().setUp()
        self.accept("alpha")
        self.accept("beta")
        state.record_review_snapshot(self.forum, machine_snapshot(self.current()))
        state.begin_critic(self.forum)
        self.review = semantic_evidence(self.current())
        self.repairs = [{"task_id": "alpha", "kind": "output_manifest", "reason": "Include the helper output."}]

    def verdict(self, *, verdict="lean_reopen", repairs=None, reopen_tasks=None, review=None):
        return state.submit_critic_verdict(
            self.forum, "Reviewer", verdict, "The output inventory needs correction.",
            review=self.review if review is None else review,
            reopen_tasks=["alpha"] if reopen_tasks is None else reopen_tasks,
            representation_repairs=self.repairs if repairs is None else repairs,
        )

    def test_explicit_manifest_repair_is_recorded_without_acceptance_shortcut(self):
        before = self.current()
        result = self.verdict()
        after = self.current()
        self.assertEqual(result["verdict"]["representation_repairs"], self.repairs)
        self.assertEqual(after["phase"], "formalizing")
        self.assertNotEqual(after["formalization"]["status"], "accepted")
        self.assertEqual(after["solution"], before["solution"])
        self.assertEqual(after["formal_tasks"]["other"], before["formal_tasks"]["other"])
        self.assertIsNone(after["formalization"]["review_snapshot"])

    def test_repair_metadata_cannot_bypass_current_snapshot_gate(self):
        review = {**self.review, "snapshot_id": "stale-snapshot"}
        self.assert_atomic_rejection(lambda: self.verdict(review=review))

    def test_invalid_repair_rows_are_rejected_atomically(self):
        variants = [
            [self.repairs[0], self.repairs[0]],
            [{**self.repairs[0], "task_id": "other"}],
            [{**self.repairs[0], "task_id": "missing"}],
            [{**self.repairs[0], "kind": "proof"}],
            [{**self.repairs[0], "reason": " "}],
            [{**self.repairs[0], "accepted": True}],
        ]
        for repairs in variants:
            with self.subTest(repairs=repairs):
                self.assert_atomic_rejection(lambda: self.verdict(repairs=repairs))

    def test_repairs_require_lean_reopen_and_version_three(self):
        self.assert_atomic_rejection(lambda: self.verdict(verdict="approved", reopen_tasks=[]))
        self.assert_atomic_rejection(lambda: self.verdict(verdict="reopen_solving", reopen_tasks=[]))
        with state.transaction(self.forum) as current:
            current["formalization"]["contract"]["version"] = 2
        self.assert_atomic_rejection(self.verdict)


if __name__ == "__main__":
    unittest.main()
