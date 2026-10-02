"""Detached scheduler observations; no models, services, or native proof calls."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import patch

from unity import bump_runtime as runtime
from unity import bump_state


def state_fixture():
    baseline = {"version": 5, "original_reports": {"Module": {"deep": [1, 2, 3]}}}
    return {"run_id": "bump-fixture", "revision": 1, "phase": "formalizing",
            "problem_sha256": "scope", "input_source": {"kind": "supplied_sources",
                "candidate_id": "source-fixture", "sha256": "source", "source_refs": []},
            "project_baseline": baseline, "formalization": {"main_sha": "main", "revision": 1,
                "contract": {"migration_policy": 1, "project_baseline": deepcopy(baseline)}},
            "formal_tasks": {"Module": {"task_id": "Module", "revision": 1,
                "status": "pending", "dependencies": []}}, "formal_candidates": {},
            "worker_tasks": {}, "strategies": {}, "events": [], "manifest_repairs": {}}


class SchedulerObservationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.forum = Path(self.temporary.name)
        self.state = state_fixture()
        self.write()
        self.path = bump_state.state_path(self.forum)
        self.observations = runtime._SchedulerObservations(self.forum)

    def write(self):
        bump_state._write_unlocked(self.forum, self.state)

    def test_unchanged_reads_parse_once_and_return_detached_mutable_observations(self):
        with patch.object(bump_state, "load_state", wraps=bump_state.load_state) as load:
            first = self.observations.observe()
            first["formal_tasks"]["Module"]["status"] = "complete"
            first["events"].append({"forged": True})
            second = self.observations.observe()
            third = self.observations.observe()
        self.assertEqual(load.call_count, 1)
        self.assertEqual(second["formal_tasks"]["Module"]["status"], "pending")
        self.assertEqual(second["events"], [])
        self.assertIsNot(second, third)
        self.assertIsNot(second["formal_tasks"], third["formal_tasks"])
        self.assertNotIn("project_baseline", second)
        self.assertNotIn("project_baseline", second["formalization"]["contract"])
        self.assertEqual(second["input_source"], self.state["input_source"])
        self.assertEqual(second["problem_sha256"], "scope")

    def test_baselines_are_not_copied_or_exposed_by_projection(self):
        class DoNotCopy:
            def __deepcopy__(self, memo):
                raise AssertionError("native baseline was copied")
        value = state_fixture()
        value["project_baseline"] = DoNotCopy()
        value["formalization"]["contract"]["project_baseline"] = DoNotCopy()
        result = runtime._scheduler_projection(value)
        self.assertEqual(result["formal_tasks"], value["formal_tasks"])
        self.assertIsNot(result["formal_tasks"], value["formal_tasks"])
        self.assertIn("project_baseline", value)

    def test_explicit_migration_representation_policy_keeps_required_baseline_detached(self):
        state = state_fixture()
        state["formalization"]["contract"]["representation_review_policy"] = 1
        result = runtime._scheduler_projection(state)
        self.assertEqual(result, state)
        self.assertIsNot(result["project_baseline"], state["project_baseline"])
        self.assertIsNot(result["formalization"]["contract"]["project_baseline"],
                         state["formalization"]["contract"]["project_baseline"])

    def test_fresh_boundary_bypasses_identical_metadata_and_retains_full_baselines(self):
        with patch.object(self.observations, "_file_identity", return_value=(1, 2, 3, 4, 5)), \
                patch.object(bump_state, "load_state", wraps=bump_state.load_state) as load:
            self.observations.observe()
            self.state["revision"] = 2
            self.write()
            fresh = self.observations.fresh()
            self.assertEqual(load.call_count, 2)
            self.assertEqual(fresh["revision"], 2)
            self.assertEqual(fresh["project_baseline"], self.state["project_baseline"])
            fresh["formal_tasks"]["Module"]["status"] = "forged"
            self.assertEqual(self.observations.observe()["formal_tasks"]["Module"]["status"], "pending")

    def test_new_iteration_does_not_reuse_prior_projection_even_with_equal_metadata(self):
        with patch.object(bump_state, "load_state", wraps=bump_state.load_state) as load:
            self.observations.observe()
            runtime._SchedulerObservations(self.forum).observe()
        self.assertEqual(load.call_count, 2)

    def test_same_size_atomic_replacement_with_restored_mtime_is_observed(self):
        self.observations.observe()
        before = self.path.stat()
        self.state["revision"] = 2
        self.write()
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, before.st_size)
        self.assertEqual(self.observations.observe()["revision"], 2)

    def test_inplace_same_size_edit_with_restored_mtime_is_observed(self):
        self.observations.observe()
        before = self.path.stat()
        payload = self.path.read_bytes().replace(b'"revision":1', b'"revision":2')
        with self.path.open("r+b") as stream:
            stream.write(payload)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = self.path.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
        self.assertEqual(self.observations.observe()["revision"], 2)

    def test_replacement_during_read_retries_before_publishing_observation(self):
        loader = bump_state.load_state
        calls = []
        def racing_load(forum):
            value = loader(forum)
            calls.append(value["revision"])
            if len(calls) == 1:
                self.state["revision"] = 2
                self.write()
            return value
        with patch.object(bump_state, "load_state", side_effect=racing_load):
            value = self.observations.observe()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(value["revision"], 2)

    def test_replacement_during_cached_copy_is_observed(self):
        self.observations.observe()
        changed = False
        def racing_copy(value):
            nonlocal changed
            result = deepcopy(value)
            if not changed:
                changed = True
                self.state["revision"] = 2
                self.write()
            return result
        with patch.object(runtime, "deepcopy", side_effect=racing_copy):
            self.assertEqual(self.observations.observe()["revision"], 2)

    def test_continuous_read_races_fail_closed_without_cache(self):
        loader = bump_state.load_state
        def racing_load(forum):
            value = loader(forum)
            self.state["revision"] += 1
            self.write()
            return value
        with patch.object(bump_state, "load_state", side_effect=racing_load) as load:
            with self.assertRaisesRegex(ValueError, "changed repeatedly"):
                self.observations.observe()
            self.assertEqual(load.call_count, 3)
        self.assertIsNone(self.observations._projection)

    def test_malformed_replacement_never_reuses_previous_state(self):
        self.observations.observe()
        self.path.write_text('{"incomplete":')
        with self.assertRaisesRegex(ValueError, "state is unreadable"):
            self.observations.observe()
        self.assertIsNone(self.observations._projection)

    def test_deletion_retains_loader_default_not_previous_snapshot_and_creation_invalidates(self):
        self.observations.observe()
        self.path.unlink()
        self.assertEqual(self.observations.observe()["run_id"], bump_state.load_state(self.forum)["run_id"])
        self.assertIsNone(self.observations._projection)
        self.state["revision"] = 3
        self.write()
        self.assertEqual(self.observations.observe()["revision"], 3)

    def test_permission_error_is_not_a_cache_hit(self):
        self.observations.observe()
        with patch.object(self.observations, "_file_identity", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                self.observations.observe()


class SchedulerReconciliationTests(unittest.TestCase):
    def test_native_migration_without_review_policy_does_no_transaction_or_digest(self):
        state = state_fixture()
        with patch.object(bump_state, "load_state", return_value=state) as load, \
                patch.object(bump_state, "transaction", side_effect=AssertionError("transaction")), \
                patch.object(bump_state.bump_json, "mutation_digest", side_effect=AssertionError("digest")), \
                patch.object(bump_state, "reconcile_rejected_representations", side_effect=AssertionError("repair")):
            self.assertIs(runtime._reconcile_for_scheduler(Path("unused")), state)
        load.assert_called_once()
        self.assertIsNone(runtime.bump_representation.representation_review_input(state, "Module"))
        self.assertIsNone(runtime.bump_representation.current_representation_review(state, "Module"))

    def test_enabled_policy_and_every_nonmigration_or_explicit_policy_use_original_reconciliation(self):
        for migration, policy in ((1, 1), (1, 0), (1, None), (None, "absent")):
            with self.subTest(migration=migration, policy=policy):
                state = state_fixture()
                contract = state["formalization"]["contract"]
                if migration is None:
                    contract.pop("migration_policy")
                if policy != "absent":
                    contract["representation_review_policy"] = policy
                repaired = {"fresh_reconciliation": True}
                with patch.object(bump_state, "load_state", return_value=state), \
                        patch.object(bump_state, "reconcile_rejected_representations", return_value=repaired) as repair:
                    self.assertIs(runtime._reconcile_for_scheduler(Path("unused")), repaired)
                repair.assert_called_once_with(Path("unused"))

    def test_guard_reads_policy_fresh_after_a_previous_policy_free_observation(self):
        state = state_fixture()
        enabled = deepcopy(state)
        enabled["formalization"]["contract"]["representation_review_policy"] = 1
        with patch.object(bump_state, "load_state", side_effect=[state, enabled]) as load, \
                patch.object(bump_state, "reconcile_rejected_representations", return_value=enabled) as repair:
            self.assertIs(runtime._reconcile_for_scheduler(Path("unused")), state)
            self.assertIs(runtime._reconcile_for_scheduler(Path("unused")), enabled)
        self.assertEqual(load.call_count, 2)
        repair.assert_called_once()


class SchedulerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_decision_receives_full_fresh_state_and_source_gate_still_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = state_fixture()
            state["formal_tasks"] = {}
            bump_state._write_unlocked(root, state)
            paths = SimpleNamespace(forum=root, project_root=root, unity=root)
            checked = []
            ended = []
            source_started, source_release = Event(), Event()
            heartbeat_ticks = []
            async def heartbeat():
                for _ in range(100):
                    await asyncio.sleep(.01)
                    if source_started.is_set():
                        heartbeat_ticks.append(True)
                        source_release.set()
                        return
                source_release.set()
                self.fail("top source check never started")
            def source_check(paths, value):
                checked.append((deepcopy(value["input_source"]), value["problem_sha256"]))
                if len(checked) == 2:
                    source_started.set()
                    self.assertTrue(source_release.wait(timeout=1), "source check blocked the event loop")
            with ExitStack() as stack:
                def mock(obj, name, **kwargs):
                    return stack.enter_context(patch.object(obj, name, **kwargs))
                mock(runtime, "configure_forum")
                mock(runtime, "stop_requested", return_value=False)
                mock(runtime, "load_prompt", return_value="")
                mock(runtime.library, "library_context", return_value="")
                mock(runtime.library, "library_subagents", return_value=[])
                mock(runtime.bump_representation, "recover_representation_reviews")
                mock(runtime.bump_representation, "pending_representation_reviews", return_value=[])
                mock(runtime.bump_jobs, "terminate")
                mock(runtime, "require_source_matches", side_effect=source_check)
                def complete(value):
                    self.assertTrue(source_release.is_set(), "routing ran before source check completed")
                    self.assertEqual(value["project_baseline"], state["project_baseline"])
                    self.assertEqual(value["formalization"]["contract"]["project_baseline"], state["project_baseline"])
                    ended.append(value["revision"])
                    return True
                mock(bump_state, "all_formal_tasks_complete", side_effect=complete)
                mock(bump_state, "record_round_end", side_effect=lambda *args, **kwargs: bump_state.load_state(root))
                ticker = asyncio.create_task(heartbeat())
                try:
                    result = await asyncio.wait_for(runtime.run_formalizing_runtime(
                        SimpleNamespace(agents=[]), paths, {}, ""), 3)
                finally:
                    source_release.set()
                    await ticker
            self.assertEqual(len(checked), 2)  # Entry plus the scheduler pass; no source-check cache.
            self.assertTrue(all(row == (state["input_source"], "scope") for row in checked))
            self.assertEqual(ended, [1])
            self.assertEqual(heartbeat_ticks, [True])
            self.assertEqual(result["project_baseline"], state["project_baseline"])

    async def test_offloaded_source_failure_propagates_before_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bump_state._write_unlocked(root, state_fixture())
            observations = runtime._SchedulerObservations(root)
            paths = SimpleNamespace(forum=root, project_root=root, unity=root)
            routed = []
            with patch.object(runtime, "require_source_matches", side_effect=ValueError("source changed")) as check:
                with self.assertRaisesRegex(ValueError, "source changed"):
                    value = await asyncio.to_thread(runtime._observe_scheduler_sources, paths, observations)
                    routed.append(value)
            check.assert_called_once()
            self.assertEqual(routed, [])


if __name__ == "__main__":
    unittest.main()
