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

    def submit(self, author, declaration, updated):
        current = state.load_state(self.paths.forum)
        task = next(row for row in current["formal_tasks"].values() if row["lean_decl"] == declaration)
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
