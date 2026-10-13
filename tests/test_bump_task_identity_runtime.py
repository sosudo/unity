"""Migration identity faults stop before workers, Git, or candidate caching.

These are controller-boundary tests using a valid compact fixture inventory,
not native migration acceptance, provider tests, or an evaluation invocation.
"""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import bump_migration as migration
from unity import bump_planner as planner
from unity import bump_runtime as runtime
from test_bump_migration_plan import migration_baseline


def fixture_state(*, missing_identity=False):
    baseline = migration_baseline()
    source = {"candidate_id": "source-" + "6" * 64, "sha256": "6" * 64,
              "source_refs": [{"ref_id": "source:project/" + path,
                               "path": ".unity/source/project/" + path, "sha256": sha}
                              for path, sha in baseline["migration"]["original_files"].items()]}
    plan = planner.plan_repairs(
        baseline["migration"]["original_index"],
        {"source_sha256": "7" * 64, "passed": True, "unmapped_error_count": 0, "diagnostics": []},
        source)
    tasks = {}
    if missing_identity:
        requirement = plan["requirements"][0]
        requirement["tasks"] = ["missing-identity"]
        tasks["missing-identity"] = {
            "id": "missing-identity", "task_id": "missing-identity", "status": "pending",
            "requirement_ids": [requirement["id"]], "anchor_ids": requirement["anchor_ids"],
            "source_components": requirement["source_components"], "lean_file": "", "outputs": [],
        }
    contract = migration.seal({
        "version": 3, "migration_policy": 1, "project_baseline": baseline,
        "requirements": deepcopy(plan["requirements"]), "spec": deepcopy(plan["spec"]),
        "bindings": {}, "targets": {}, "adopted_outputs": {},
    })
    return {
        "phase": "formalizing", "run_id": baseline["migration"]["run_id"], "project_baseline": baseline,
        "formalization": {"status": "active", "main_sha": "8" * 40, "revision": 1,
                          "requirements": plan["requirements"], "spec": plan["spec"], "contract": contract},
        "formal_tasks": tasks, "formal_candidates": {},
    }


class MigrationTaskIdentityRuntimeTests(unittest.TestCase):
    def test_empty_repair_plan_with_full_original_universe_has_valid_identity_invariant(self):
        current = fixture_state()
        before = deepcopy(current)
        self.assertEqual(current["formal_tasks"], {})
        self.assertGreater(len(current["formalization"]["requirements"]), 0)
        self.assertIsNone(migration.require_task_identities(current))
        self.assertEqual(current, before, "validation must never advance or reconstruct state")

    def test_identity_fault_precedes_git_candidate_cache_apply_and_rollback(self):
        current = fixture_state(missing_identity=True)
        task = current["formal_tasks"]["missing-identity"]
        candidate = {"candidate_id": "new-candidate", "task_id": task["task_id"]}
        current["formal_candidates"]["cached-failure"] = {
            "status": "failed", "task_id": task["task_id"], "failure_context": {"cacheable": True}}
        before = deepcopy(current)
        paths = SimpleNamespace(project_root=Path("/unused"), forum=Path("/unused/forum"),
                                artifacts=Path("/unused/artifacts"))
        with patch.object(runtime.bump_state, "load_state", return_value=current), \
             patch.object(runtime, "_git") as git, \
             patch.object(runtime, "_apply_formal_candidate") as apply, \
             patch.object(runtime, "_rollback") as rollback, \
             patch.object(runtime.bump_contract, "observe_failure_inputs") as cache_observation, \
             patch.object(runtime.bump_state, "transaction") as transaction, \
             patch.object(runtime.artifacts, "store_text") as artifact:
            with self.assertRaisesRegex(ValueError, "migration.*identity|identity.*migration"):
                runtime._integrate_checked(paths, candidate, task)
            for boundary in (git, apply, rollback, cache_observation, transaction, artifact):
                boundary.assert_not_called()
        self.assertEqual(current, before)

    def test_identity_fault_stops_before_forum_worktrees_models_or_reconciliation(self):
        current = fixture_state(missing_identity=True)
        before = deepcopy(current)
        paths = SimpleNamespace(project_root=Path("/unused"), forum=Path("/unused/forum"))
        roster = SimpleNamespace(agents=[])
        with patch.object(runtime, "stop_requested", return_value=False), \
             patch.object(runtime.bump_state, "load_state", return_value=current), \
             patch.object(runtime, "require_source_matches") as source_check, \
             patch.object(runtime, "configure_forum") as configure, \
             patch.object(runtime, "_formal_worktree") as worktree, \
             patch.object(runtime, "spawn") as spawn, \
             patch.object(runtime.bump_state, "reconcile_rejected_representations") as reconcile:
            with self.assertRaisesRegex(ValueError, "migration.*identity|identity.*migration"):
                asyncio.run(runtime.run_formalizing_runtime(roster, paths, {}, "offline fixture"))
            for boundary in (source_check, configure, worktree, spawn, reconcile):
                boundary.assert_not_called()
        self.assertEqual(current, before)


if __name__ == "__main__":
    unittest.main()
