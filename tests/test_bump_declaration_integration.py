"""Real Forum -> owned Git worktree -> controller -> native acceptance fixture.

Only the worker edits and final semantic critic are scripted. The original and
target use an already installed Lean; two target-only type errors represent
compiler incompatibilities. No model, provider, external service, or production
verification stub is used. A scripted critic tests the acceptance protocol, not
independent human/model semantic judgment or a real cross-version migration.
"""

from copy import deepcopy
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from unity import artifacts
from unity import bump_bootstrap as bootstrap
from unity import bump_contract as contract
from unity import bump_report as report
from unity import bump_runtime as runtime
from unity import bump_state as state
from unity.commands.bump import _accept_current_critic, _prepare_critic_snapshot
from unity.forum import bump_server as server
import test_bump_declaration_native as native_fixture


class InstalledDeclarationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native_fixture.InstalledDeclarationMigrationTests.setUpClass()

    def setUp(self):
        # Reuse only native fixture setup, not its manual integration helper.
        self.fixture = native_fixture.InstalledDeclarationMigrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.paths = self.fixture.paths
        self.target = self.fixture.target
        for name, value in (("PROJECT_ROOT", self.target), ("FORUM_DIR", self.paths.forum),
                            ("PROFILE", "formalizing")):
            self.enterContext(patch.object(server, name, value))
        for name, value in (("PROJECT_ROOT", self.target), ("FORUM_DIR", self.paths.forum),
                            ("ICRL_ENABLED", False)):
            self.enterContext(patch.object(server.discussion, name, value))
        self.enterContext(patch.dict(os.environ, {"UNITY_AGENT_NAME": "", "UNITY_BUMP_TASK_ID": ""}))

    def install_target_errors(self):
        broken = self.fixture.original.replace(": Nat := 1", ": Nat := True").replace(": Nat := 2", ": Nat := False")
        (self.target / "Fixture.lean").write_text(broken)
        broken_sha = self.fixture.commit("fixture: target compiler incompatibilities")
        # The sole fixture setup mutation models a compiler-breaking upgrade.
        # All candidate merges, reviews and acceptance below use production APIs.
        with state.transaction(self.paths.forum) as live:
            live["formalization"]["main_sha"] = broken_sha
        planned = bootstrap.refresh(self.paths)
        self.assertEqual({row["lean_decl"] for row in planned["formal_tasks"].values()}, {"first", "second"})
        self.assertEqual(len(planned["formalization"]["requirements"]), 3)
        return broken, planned

    def submit(self, author, declaration, updated, *, task_id=None):
        current = state.load_state(self.paths.forum)
        task = (current["formal_tasks"][task_id] if task_id else
                next(row for row in current["formal_tasks"].values() if row["lean_decl"] == declaration))
        tree = server.worktree.create_worktree(author, self.target)
        prepared = server.prepare_formal_worktree(author, next_task=task["task_id"],
                                                  expected_revision=current["formalization"]["revision"])
        self.assertTrue(prepared.get("ok"), prepared)
        registered = server.register_strategy(author, "Restore the original " + declaration + " declaration.",
                                               target=task["task_id"], strategy_family="exact_original_term")
        strategy_id = registered["strategy"]["strategy_id"]
        self.assertEqual(server.claim_strategy(strategy_id, author)["status"], "claimed")
        (tree / "Fixture.lean").write_text(updated)
        submitted = server.finalize_formalization(
            strategy_id, author, task["task_id"], changed_paths=["Fixture.lean"],
            outputs=[{"declaration": declaration, "file": "Fixture.lean"}],
            notes="Scripted fixture worker, restoring the exact original term.",
        )
        self.assertEqual(submitted["status"], "submitted", submitted)
        self.assertTrue(submitted["committed"])
        candidate = submitted["candidate"]
        self.assertEqual(candidate["changed_paths"], ["Fixture.lean"])
        self.assertEqual(candidate["base_main_sha"], current["formalization"]["main_sha"])
        return candidate

    def integrate(self, candidate):
        begun = state.begin_formal_merge(self.paths.forum, candidate["candidate_id"])
        self.assertNotIn("conflict", begun)
        current = state.load_state(self.paths.forum)
        result = runtime._integrate_and_record(
            self.paths, begun["candidate"], current["formal_tasks"][candidate["task_id"]],
        )
        self.assertTrue(result.get("ok"), result)
        current = state.load_state(self.paths.forum)
        recorded = current["formal_candidates"][candidate["candidate_id"]]
        self.assertEqual(recorded["status"], "merged")
        self.assertEqual(recorded["main_sha"], current["formalization"]["main_sha"])
        self.assertEqual(recorded["main_sha"], self.fixture.command(self.target, ["git", "rev-parse", "HEAD"]).stdout.strip())
        self.assertEqual(recorded["verification"]["mode"], "diagnostic_repair")
        self.assertTrue(recorded["verification"]["native_pending"])
        self.assertEqual(current["formal_tasks"][candidate["task_id"]]["verification"]["status"], "provisional")
        return result, current

    def semantic_review(self, current):
        formal = current["formalization"]
        requirements = formal["requirements"]
        bindings = formal["review_snapshot"]["migration_review"]["occurrence_declarations"]
        return {"snapshot_id": formal["review_snapshot"]["snapshot_id"],
                "scope_rationale": "Scripted fixture checks all three original declarations, including unchanged third.",
                "requirements": [{
                    "requirement_id": row["id"], "status": "pass",
                    "declarations": [bindings[row["id"].removeprefix("requirement-")]],
                    "checked_anchor_ids": row["anchor_ids"], "checked_prerequisite_ids": [],
                    "rationale": "The exact original type and definition were restored in this fixture.",
                    "argument_rationale": "The original computation and dependency are unchanged.",
                    "repair_steps": [],
                } for row in requirements], "repair_reviews": []}

    def test_migration_plan_writer_rechunks_after_partial_merge_without_losing_progress(self):
        broken, _ = self.install_target_errors()
        candidate = self.submit("Ada", "first", broken.replace(": Nat := True", ": Nat := 1"))
        result, partial = self.integrate(candidate)
        self.assertNotEqual(result["build"]["returncode"], 0)
        completed_id = candidate["task_id"]
        pending_id = next(key for key, row in partial["formal_tasks"].items()
                          if row["lean_decl"] == "second")
        self.assertEqual(partial["formal_tasks"][pending_id]["status"], "pending")
        original_baseline = deepcopy(partial["project_baseline"])
        original_obligations = deepcopy(partial["formalization"]["source_obligations"])
        original_correspondences = deepcopy(
            partial["formalization"]["contract"]["migration_correspondences"])
        original_source = deepcopy(state.formal_source(partial))
        original_tasks = deepcopy(partial["formal_tasks"])
        original_candidate = deepcopy(partial["formal_candidates"][candidate["candidate_id"]])
        main_sha = partial["formalization"]["main_sha"]

        request = state.request_rechunk(self.paths.forum, "Ada", "Replan the remaining repair.", [pending_id])
        state.begin_replan(self.paths.forum, request["request_id"])
        interrupted = state.begin_chunking_attempt(self.paths.forum, original_source["candidate_id"], "Ada")
        # A prior controller exit can leave an allocated attempt unfinished.
        # Continuing must retain it as failed, not reset the attempt budget.
        attempt = state.begin_chunking_attempt(self.paths.forum, original_source["candidate_id"], "Ada")
        chunking = state.load_state(self.paths.forum)
        self.assertEqual(attempt["attempt"], interrupted["attempt"] + 1)
        recovered = next(row for row in chunking["chunking_attempts"]
                         if row["attempt_id"] == interrupted["attempt_id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["reason"], "chunking process ended before recording an outcome")
        plan_path = runtime.write_formalization_plan(self.paths, state.formal_source(chunking))
        plan = json.loads(plan_path.read_text())
        self.assertEqual(plan["project_baseline"]["migration"]["original_index"],
                         original_baseline["migration"]["original_index"])
        self.assertEqual(plan["replan"]["previous_tasks"], original_tasks)
        self.assertEqual(plan["solution_sha256"], original_source["sha256"])
        self.assertEqual(state.load_state(self.paths.forum), chunking)

        draft = runtime.seed_chunking_draft(chunking)
        # Invalid edits still fail read-only validation; the writer does not
        # relax the existing source-binding or frozen-obligation checks.
        stale = deepcopy(draft)
        stale["base_revision"] -= 1
        wrong_source = deepcopy(draft)
        wrong_source["solution_sha256"] = "0" * 64
        rewritten = deepcopy(draft)
        rewritten["requirements"] = []
        for invalid in (stale, wrong_source, rewritten):
            with self.subTest(invalid_fields=list(invalid)):
                with self.assertRaises(ValueError):
                    runtime.prepare_chunking_draft(self.paths, chunking, json.dumps(invalid).encode())
                self.assertEqual(state.load_state(self.paths.forum), chunking)

        dag, source_contract = runtime.prepare_chunking_draft(
            self.paths, chunking, json.dumps(draft).encode())
        self.assertEqual(state.load_state(self.paths.forum), chunking)
        published = state.initialize_informal_plan(
            self.paths.forum, dag, main_sha=main_sha, contract=source_contract,
            attempt_id=attempt["attempt_id"], expected_revision=chunking["revision"])

        self.assertEqual(published["phase"], "formalizing")
        self.assertEqual(published["formalization"]["status"], "active")
        self.assertEqual(published["project_baseline"], original_baseline)
        self.assertEqual(published["formalization"]["contract"]["project_baseline"], original_baseline)
        self.assertEqual(published["formalization"]["source_obligations"], original_obligations)
        self.assertEqual(len(original_obligations["requirements"]), 3,
                         "unchanged third declaration remains an original obligation")
        self.assertEqual(published["formalization"]["contract"]["migration_correspondences"],
                         original_correspondences)
        self.assertEqual(state.formal_source(published), original_source)
        self.assertEqual(published["formal_candidates"][candidate["candidate_id"]], original_candidate)
        for task_id, original in original_tasks.items():
            for field in ("migration", "migration_native_status", "status", "accepted_candidate",
                          "outputs", "lean_decl", "lean_file", "verification", "revision"):
                self.assertEqual(published["formal_tasks"][task_id].get(field), original.get(field),
                                 (task_id, field))
        self.assertEqual(published["replan"]["carried_tasks"], [completed_id])
        self.assertEqual(published["replan"]["affected_tasks"], [])
        self.assertEqual(published["replan_requests"][request["request_id"]]["status"], "complete")
        recorded_attempt = next(row for row in published["chunking_attempts"]
                                if row["attempt_id"] == attempt["attempt_id"])
        self.assertEqual(recorded_attempt["status"], "succeeded")
        self.assertEqual(next(row for row in published["chunking_attempts"]
                              if row["attempt_id"] == interrupted["attempt_id"]), recovered)
        self.assertEqual(published["formalization"]["main_sha"], main_sha)
        self.assertEqual(self.fixture.command(self.target, ["git", "rev-parse", "HEAD"]).stdout.strip(), main_sha)
        self.assertEqual((self.target / "Fixture.lean").read_text(), broken.replace(": Nat := True", ": Nat := 1"))
        self.assertEqual((self.fixture.source / "Fixture.lean").read_text(), self.fixture.original)
        self.assertIsNone(published["formalization"]["review_snapshot"])
        self.assertTrue(published["formal_tasks"][completed_id]["verification"]["native_pending"])

    def test_replan_new_original_task_binds_before_dispatch_merge_and_refresh(self):
        broken, _ = self.install_target_errors()
        first = self.submit("Ada", "first", broken.replace(": Nat := True", ": Nat := 1"))
        _, partial = self.integrate(first)
        original_baseline = deepcopy(partial["project_baseline"])
        original_obligations = deepcopy(partial["formalization"]["source_obligations"])
        original_source = deepcopy(state.formal_source(partial))
        original_candidates = deepcopy(partial["formal_candidates"])
        original_tasks = deepcopy(partial["formal_tasks"])
        original_attempts = deepcopy(partial["chunking_attempts"])
        main_sha = partial["formalization"]["main_sha"]
        second_id = next(key for key, row in partial["formal_tasks"].items()
                         if row["lean_decl"] == "second")
        index = original_baseline["migration"]["original_index"]["occurrences"]
        third_original_id = next(key for key, row in index.items() if row["display_name"] == "third")
        requirement_id = "requirement-" + third_original_id
        original_requirement = next(row for row in partial["formalization"]["requirements"]
                                    if row["id"] == requirement_id)
        self.assertEqual(original_requirement["tasks"], [], "third has no compiler-created repair task")

        request = state.request_rechunk(self.paths.forum, "Ada", "Include the remaining original obligation.", [second_id])
        state.begin_replan(self.paths.forum, request["request_id"])
        attempt = state.begin_chunking_attempt(self.paths.forum, original_source["candidate_id"], "Ada")
        chunking = state.load_state(self.paths.forum)
        draft = runtime.seed_chunking_draft(chunking)
        new_task_id = "replanned-third"
        draft["requirement_tasks"][requirement_id] = [new_task_id]
        draft["chunks"].append({
            "id": new_task_id, "title": "Preserve third", "predicted_kind": index[third_original_id]["kind"],
            "informal_statement": "Preserve the original third declaration, its computation and inherited trust.",
            "informal_proof": None, "statement_dependencies": [], "proof_dependencies": [first["task_id"]],
            "proposed_formal_statement": None, "proposed_formal_strategy": None,
            "source_components": deepcopy(original_requirement["source_components"]),
            "anchor_ids": deepcopy(original_requirement["anchor_ids"]), "requirement_ids": [requirement_id],
        })
        # Draft syntax has no implementation metadata. The controller must bind
        # the new identity from sealed source obligations before publication.
        self.assertNotIn("migration", draft["chunks"][-1])
        dag, source_contract = runtime.prepare_chunking_draft(
            self.paths, chunking, json.dumps(draft).encode())
        self.assertEqual(state.load_state(self.paths.forum), chunking, "validation is read-only")
        published = state.initialize_informal_plan(
            self.paths.forum, dag, main_sha=main_sha, contract=source_contract,
            attempt_id=attempt["attempt_id"], expected_revision=chunking["revision"])

        new_task = published["formal_tasks"][new_task_id]
        self.assertEqual(new_task["migration"]["original_ids"], [third_original_id])
        self.assertEqual(new_task["migration"]["kind"], "declaration")
        self.assertEqual(new_task["migration"]["module"], "Fixture")
        self.assertEqual(new_task["migration"]["path"], "Fixture.lean")
        self.assertEqual(new_task["migration"]["original_ranges"], [index[third_original_id]["range"]])
        self.assertEqual(new_task["migration"]["diagnostic_ids"], [])
        self.assertEqual(new_task["lean_file"], "Fixture.lean")
        self.assertEqual(new_task["status"], "pending")
        self.assertEqual(new_task["migration_native_status"], "pending")
        self.assertEqual(new_task["outputs"], [], "identity binding must not adopt an output")
        reservation = published["file_reservations"]["Fixture.lean"]
        self.assertEqual({reservation["owner_task"], *reservation["shared_with"]}, set(published["formal_tasks"]))
        self.assertEqual(reservation["owner_task"], partial["file_reservations"]["Fixture.lean"]["owner_task"])
        self.assertEqual(published["project_baseline"], original_baseline)
        self.assertEqual(published["formalization"]["source_obligations"], original_obligations)
        self.assertEqual(state.formal_source(published), original_source)
        self.assertEqual(published["formal_candidates"], original_candidates)
        self.assertEqual(published["chunking_attempts"][:len(original_attempts)], original_attempts)
        for task_id, previous in original_tasks.items():
            for field in ("migration", "migration_native_status", "status", "accepted_candidate",
                          "outputs", "lean_decl", "lean_file", "verification", "revision"):
                self.assertEqual(published["formal_tasks"][task_id].get(field), previous.get(field),
                                 (task_id, field))

        # The scripted worker changes only syntax, not the original meaning.
        # Both insertions are strictly inside the native declaration span:
        # wrapping the whole RHS would put ')' at its exclusive end boundary,
        # correctly rejected as a possible adjacent source-command edit.
        # The other declaration remains broken, so this exercises the real
        # provisional merge-recording path rather than final acceptance.
        updated = (self.target / "Fixture.lean").read_text().replace("first + 1", "(first) + 1")
        third = self.submit("Cora", "third", updated, task_id=new_task_id)
        self.assertEqual(third["base_main_sha"], main_sha)
        result, refreshed = self.integrate(third)
        self.assertNotEqual(result["build"]["returncode"], 0)
        self.assertIn(new_task_id, refreshed["formal_tasks"], "compiler refresh must retain the replanned task")
        self.assertEqual(refreshed["formal_tasks"][new_task_id]["migration"], new_task["migration"])
        self.assertEqual(refreshed["formal_tasks"][new_task_id]["status"], "complete")
        self.assertTrue(refreshed["formal_tasks"][new_task_id]["verification"]["native_pending"])
        self.assertEqual(refreshed["formal_tasks"][second_id]["status"], "pending")
        self.assertEqual(refreshed["formal_candidates"][first["candidate_id"]], original_candidates[first["candidate_id"]])
        self.assertEqual(refreshed["formalization"]["source_obligations"], original_obligations)
        self.assertEqual({row["id"] for row in refreshed["formalization"]["requirements"]},
                         {row["id"] for row in partial["formalization"]["requirements"]})
        self.assertEqual((self.fixture.source / "Fixture.lean").read_text(), self.fixture.original)
        self.assertEqual((self.target / "Fixture.lean").read_text(), updated)
        self.assertIsNone(refreshed["formalization"]["review_snapshot"])
        self.assertNotEqual(refreshed["formalization"]["status"], "accepted")

    def test_unrecorded_real_integration_commit_is_preserved_and_blocks_automatic_resume(self):
        broken, _ = self.install_target_errors()
        candidate = self.submit("Ada", "first", broken.replace(": Nat := True", ": Nat := 1"))
        begun = state.begin_formal_merge(self.paths.forum, candidate["candidate_id"])
        self.assertNotIn("conflict", begun)
        merging = state.load_state(self.paths.forum)
        recorded_main = merging["formalization"]["main_sha"]
        with patch.object(state, "finish_formal_merge", side_effect=RuntimeError("scripted interruption after Git commit")), \
             patch.object(bootstrap, "refresh", wraps=bootstrap.refresh) as refresh:
            with self.assertRaisesRegex(RuntimeError, "scripted interruption after Git commit"):
                runtime._integrate_and_record(
                    self.paths, begun["candidate"], merging["formal_tasks"][candidate["task_id"]])
            refresh.assert_not_called()
        unrecorded_main = self.fixture.command(self.target, ["git", "rev-parse", "HEAD"]).stdout.strip()
        self.assertNotEqual(unrecorded_main, recorded_main)
        self.assertEqual(self.fixture.command(self.target, ["git", "rev-parse", "HEAD^1"]).stdout.strip(), recorded_main)
        self.assertEqual(self.fixture.command(self.target, ["git", "status", "--porcelain", "--untracked-files=no"]).stdout.strip(), "")
        self.assertEqual(state.load_state(self.paths.forum), merging, "failed recording must not publish partial state")
        self.assertEqual((self.target / "Fixture.lean").read_text(), broken.replace(": Nat := True", ": Nat := 1"))
        worker_commit = candidate["commit_sha"]
        self.assertEqual(self.fixture.command(self.target, ["git", "cat-file", "-t", worker_commit]).stdout.strip(), "commit")
        with patch.object(state, "finish_formal_merge", wraps=state.finish_formal_merge) as record:
            with self.assertRaisesRegex(ValueError, "Inspect and reconcile main before resuming.*No changes were discarded"):
                runtime.recover_interrupted_formal_merges(self.paths)
            record.assert_not_called()
        self.assertEqual(state.load_state(self.paths.forum), merging)
        self.assertEqual(self.fixture.command(self.target, ["git", "rev-parse", "HEAD"]).stdout.strip(), unrecorded_main)
        self.assertEqual(self.fixture.command(self.target, ["git", "cat-file", "-t", worker_commit]).stdout.strip(), "commit")
        self.assertEqual((self.fixture.source / "Fixture.lean").read_text(), self.fixture.original)

    def test_same_file_provisional_candidates_reach_actual_native_and_critic_acceptance(self):
        broken, planned = self.install_target_errors()
        before = planned["formalization"]["main_sha"]
        first = self.submit("Ada", "first", broken.replace(": Nat := True", ": Nat := 1"))
        second = self.submit("Bert", "second", broken.replace(": Nat := False", ": Nat := 2"))
        # Both are real owned worktree candidates based on the same old main.
        # No same-file dispatch exclusion or manual candidate/state completion.
        self.assertEqual(first["base_main_sha"], second["base_main_sha"])
        self.assertEqual(self.fixture.command(self.target, ["git", "rev-parse", "HEAD"]).stdout.strip(), before)
        result, partial = self.integrate(first)
        self.assertNotEqual(result["build"]["returncode"], 0, "other declaration must still fail")
        self.assertEqual({row["lean_decl"]: row["status"] for row in partial["formal_tasks"].values()},
                         {"first": "complete", "second": "candidate_pending"})
        self.assertEqual(partial["formal_candidates"][second["candidate_id"]]["status"], "submitted")
        self.assertTrue(state.candidate_is_current(partial, partial["formal_candidates"][second["candidate_id"]]))
        self.assertNotEqual(partial["formalization"]["main_sha"], before)
        self.assertNotEqual(partial["formalization"]["status"], "accepted")

        result, repaired = self.integrate(second)
        self.assertEqual(result["build"]["returncode"], 0)
        self.assertTrue(state.all_formal_tasks_complete(repaired))
        self.assertEqual((self.target / "Fixture.lean").read_text(), self.fixture.original)
        self.assertEqual((self.fixture.source / "Fixture.lean").read_text(), self.fixture.original)
        self.assertTrue(_prepare_critic_snapshot(self.paths))
        reviewed = state.load_state(self.paths.forum)
        snapshot = reviewed["formalization"]["review_snapshot"]
        self.assertTrue(snapshot["passed"], snapshot["issues"])
        self.assertTrue(snapshot["migration_review"]["native_complete"])
        original_ids = set(reviewed["project_baseline"]["migration"]["original_index"]["occurrences"])
        self.assertEqual(set(snapshot["migration_review"]["verified_occurrences"]), original_ids)
        self.assertEqual(len(original_ids), 3)
        self.assertTrue(contract.snapshot_is_current(self.paths, reviewed, snapshot))

        semantic = self.semantic_review(reviewed)
        omitted = deepcopy(semantic)
        omitted["requirements"].pop()
        with self.assertRaisesRegex(ValueError, "exact coverage"):
            server.submit_formalization_verdict("IndependentCritic", "approved", "Incomplete fixture review.", omitted)
        self.assertEqual(state.load_state(self.paths.forum), reviewed)
        verdict = server.submit_formalization_verdict("IndependentCritic", "approved", "All original fixture declarations preserved.", semantic)
        self.assertEqual(verdict["state"]["formalization"]["status"], "approval_pending")
        self.assertTrue(_accept_current_critic(self.paths))
        accepted = state.load_state(self.paths.forum)
        self.assertEqual(accepted["phase"], "complete")
        self.assertEqual(accepted["formalization"]["status"], "accepted")

        reference = report.persist_report(self.paths)
        payload = artifacts.artifact_bytes(self.paths.artifacts, reference["artifact_id"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), reference["sha256"])
        expected = (json.dumps(report.completion_report(state.load_state(self.paths.forum)),
                               indent=2, sort_keys=True) + "\n").encode()
        self.assertEqual(payload, expected)
        saved = json.loads(payload)
        self.assertEqual(saved["status"], "accepted")
        self.assertEqual(saved["main_sha"], accepted["formalization"]["main_sha"])
        self.assertEqual(saved["machine_review"]["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(saved["critic_verdict"]["verdict_id"], verdict["verdict"]["verdict_id"])
        self.assertEqual(len(saved["coverage"]), 3)
        # Publication is exact and idempotent; later source edits cannot reuse it.
        self.assertEqual(report.persist_report(self.paths), reference)
        (self.target / "Fixture.lean").write_text(self.fixture.original.replace("Nat := 2", "Nat := 3"))
        with self.assertRaisesRegex(ValueError, "stale source revision"):
            report.persist_report(self.paths)
        self.assertEqual(state.load_state(self.paths.forum)["final_report"], reference)


if __name__ == "__main__":
    unittest.main()
