"""Canned scheduler transport failures: no model, service, or compiler calls."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import bump_runtime as runtime, bump_state as state
from unity.bump_provider import BumpProviderFailure, BumpTransportRetriesExhausted
from tests.test_bump_scheduler_snapshot import state_fixture


class RuntimeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_workers(self, *, complete_all=False, fatal=False, queued=False, focused=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = state_fixture()
            if queued or focused:
                value["formalization"]["contract"]["migration_policy"] = 2
            value["formal_tasks"] = {
                key: {"task_id": key, "revision": 1, "status": "pending",
                      "dependencies": [], "migration_module": key,
                      "lean_file": key + ".lean", "migration_attempts": 0,
                      "migration_max_attempts": 5, "representation": {"status": "adopted"}}
                for key in ("A", "B")
            }
            state._write_unlocked(root, value)
            paths = SimpleNamespace(forum=root, project_root=root, unity=root)
            roster = SimpleNamespace(agents=[SimpleNamespace(name="Blocked"), SimpleNamespace(name="Healthy")])
            launched, charged, yielded, rounds = [], [], [], []
            failed = BumpProviderFailure("rate_limited", provider="freeinference", status=429)
            exhausted = BumpTransportRetriesExhausted(failed, 5)

            async def spawn(agent, *args, **kwargs):
                launched.append((agent.name, kwargs["log_context"]["task_id"]))
                if agent.name == "Blocked":
                    if fatal:
                        raise ValueError("controller fixture fault")
                    raise exhausted
                # Remain live across consumption of the other worker's failure.
                await asyncio.sleep(0.25)
                with state.transaction(root) as current:
                    current["formal_tasks"]["B"]["status"] = "complete"
                    if queued:
                        current["formal_candidates"]["queued"] = {
                            "candidate_id": "queued", "task_id": "A", "author": "Queue",
                            "status": "submitted", "task_revision": 1,
                        }
                    if complete_all:
                        current["formal_tasks"]["A"]["status"] = "complete"

            def charge(forum, key, author, **kwargs):
                charged.append((author, key))
                with state.transaction(forum) as current:
                    current["formal_tasks"][key]["migration_attempts"] += 1
                return state.load_state(forum)["formal_tasks"][key]

            def record(forum, **kwargs):
                rounds.append(deepcopy(kwargs))
                return state.load_state(forum)

            with ExitStack() as stack:
                def mock(obj, name, **kwargs):
                    return stack.enter_context(patch.object(obj, name, **kwargs))
                mock(runtime, "configure_forum")
                mock(runtime, "stop_requested", return_value=False)
                mock(runtime, "load_prompt", return_value="")
                mock(runtime, "require_source_matches")
                mock(runtime, "forum_brief", return_value="")
                mock(runtime, "_preamble", return_value="")
                mock(runtime, "_agent_runtime_env", return_value={})
                mock(runtime, "_formal_worktree", side_effect=lambda project, name: root / name)
                mock(runtime, "_formal_launch_retry_key", return_value="fixture")
                mock(runtime, "_git", return_value=SimpleNamespace(stdout="", returncode=0))
                mock(runtime, "_migration_frontier_is_current", return_value=True)
                mock(runtime, "spawn", side_effect=spawn)
                mock(runtime, "_cancel", new=lambda *args: asyncio.sleep(0))
                mock(runtime.library, "library_context", return_value="")
                mock(runtime.library, "library_subagents", return_value=[])
                mock(runtime.worktree, "symlink_lake_cache")
                mock(runtime.worktree, "link_runtime_state")
                mock(runtime.worktree, "cleanup_worktree")
                mock(runtime.bump_jobs, "terminate")
                mock(runtime.bump_representation, "recover_representation_reviews")
                mock(runtime.bump_representation, "pending_representation_reviews", return_value=[])
                mock(runtime.bump_server, "unresolved_formal_tasks", return_value=[])
                mock(runtime.bump_server, "has_pending_formal_candidate", side_effect=lambda current, *args:
                     bool(current.get("formal_candidates")) if not args else False)
                mock(runtime.bump_server, "verification_blockers", return_value=[])
                mock(runtime.bump_server, "prepare_formal_worktree", return_value={"ok": True})
                focus_assignment = mock(runtime.bump_server, "record_migration_focus_assignment")
                mock(state, "task_available_to", side_effect=lambda current, author, key:
                     current.get("formal_tasks", {}).get(key, {}).get("status") == "pending"
                     and not (author == "Healthy" and key == "A"))
                mock(state, "snapshot_attempt", side_effect=lambda current, author, key:
                     {"task_id": key, "task_revision": 1})
                mock(state, "begin_migration_attempt", side_effect=charge)
                mock(state, "next_migration_chunk", side_effect=lambda current, key:
                     {"id": "chunk-" + key, "kind": "declaration", "original_ids": [],
                      "diagnostic_ids": []} if focused else None)
                mock(state, "candidate_is_current", return_value=True)
                mock(state, "migration_candidate_context_current", return_value=False)
                mock(state, "critic_feedback_for_task", return_value={"direct": [], "upstream": []})
                mock(state, "record_worker_yield", side_effect=lambda *args, **kwargs: yielded.append(args))
                mock(state, "record_round_end", side_effect=record)
                if fatal:
                    with self.assertRaisesRegex(RuntimeError, "migration preserved"):
                        await asyncio.wait_for(runtime.run_formalizing_runtime(roster, paths, {}, ""), 3)
                elif complete_all:
                    result = await asyncio.wait_for(runtime.run_formalizing_runtime(roster, paths, {}, ""), 3)
                    self.assertTrue(state.all_formal_tasks_complete(result))
                else:
                    with self.assertRaises(BumpTransportRetriesExhausted) as caught:
                        await asyncio.wait_for(runtime.run_formalizing_runtime(roster, paths, {}, ""), 3)
                    self.assertIs(caught.exception, exhausted)
                    self.assertEqual(state.load_state(root)["formal_tasks"]["B"]["status"], "complete")
                    if queued:
                        self.assertEqual(state.load_state(root)["formal_candidates"]["queued"]["status"],
                                         "submitted")
                if focused:
                    self.assertEqual(focus_assignment.call_count, 2)
                    self.assertEqual([call.args for call in focus_assignment.call_args_list],
                                     [("Blocked", "A", 1), ("Healthy", "B", 1)])
            return launched, charged, yielded, rounds

    async def test_exhausted_worker_preserves_healthy_peer_and_does_not_redispatch(self):
        launched, charged, yielded, rounds = await self.run_workers()
        self.assertEqual(launched.count(("Blocked", "A")), 1)
        self.assertEqual(launched.count(("Healthy", "B")), 1)
        self.assertEqual(charged, [("Blocked", "A"), ("Healthy", "B")])
        self.assertFalse(any(args[1] == "Blocked" for args in yielded))
        self.assertIn("Blocked", rounds[-1]["blocked_launches"])

    async def test_peer_can_finish_all_obligations_despite_exhausted_transport(self):
        launched, charged, yielded, rounds = await self.run_workers(complete_all=True)
        self.assertEqual(len(launched), 2)
        self.assertEqual(len(charged), 2)
        self.assertIn("Blocked", rounds[-1]["blocked_launches"])

    async def test_blocked_queued_candidate_does_not_hide_transport_exhaustion(self):
        launched, charged, yielded, rounds = await self.run_workers(queued=True)
        self.assertEqual(len(launched), 2)
        self.assertEqual(len(charged), 2)
        self.assertIn("Blocked", rounds[-1]["blocked_launches"])

    async def test_initial_chunk_focus_is_durable_without_an_extra_attempt(self):
        launched, charged, yielded, rounds = await self.run_workers(complete_all=True, focused=True)
        self.assertEqual(len(launched), 2)
        self.assertEqual(charged, [("Blocked", "A"), ("Healthy", "B")])

    async def test_unknown_controller_failure_is_still_fatal(self):
        await self.run_workers(fatal=True)


class CandidateFrontierTests(unittest.TestCase):
    def test_deferred_frontier_remains_a_queued_merge_not_a_rejection(self):
        paths = SimpleNamespace(project_root=Path("fixture"), forum=Path("fixture"))
        candidate = {"candidate_id": "candidate", "task_id": "A"}
        deferred = {"ok": False, "deferred": True, "error": "refresh required"}
        from contextlib import nullcontext
        with patch.object(runtime, "_merge_lock", return_value=nullcontext()), \
                patch.object(runtime, "_integrate_checked", return_value=deferred), \
                patch.object(state, "defer_formal_merge") as postpone, \
                patch.object(state, "finish_formal_merge") as finish:
            result = runtime._integrate_and_record(paths, candidate, {})
        self.assertTrue(result["deferred"])
        postpone.assert_called_once_with(paths.forum, "candidate", reason="refresh required")
        finish.assert_not_called()
