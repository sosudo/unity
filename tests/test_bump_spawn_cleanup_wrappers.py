"""Bump cleanup faults propagate through wrappers; no model or service calls."""

import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from unity import artifacts, bump_orchestrator, bump_repairs, bump_representation
from unity import bump_runtime, bump_spawn, bump_worktree, config


class BumpCleanupWrapperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = SimpleNamespace(name="Ada")
        self.roster = SimpleNamespace(agents=[self.agent], primary=self.agent)
        self.paths = SimpleNamespace(project_root=Path("/unused"), forum=Path("/unused/forum"))

    def patch_common(self, stack):
        stack.enter_context(patch.object(bump_orchestrator, "stop_requested", return_value=False))
        stack.enter_context(patch.object(bump_orchestrator, "_preamble", return_value=""))
        stack.enter_context(patch.object(bump_orchestrator, "build_bump_mcp", return_value={}))
        stack.enter_context(patch.object(bump_runtime, "_formal_worktree", return_value=Path("/unused/tree")))
        stack.enter_context(patch.object(bump_runtime, "_agent_runtime_env", return_value={}))
        stack.enter_context(patch.object(bump_worktree, "link_runtime_state"))
        stack.enter_context(patch.object(bump_worktree, "symlink_lake_cache"))

    async def source_turn(self, failure=None):
        with ExitStack() as stack:
            self.patch_common(stack)
            stack.enter_context(patch.object(bump_repairs, "stop_requested", return_value=False))
            stack.enter_context(patch.object(bump_repairs, "_preamble", return_value=""))
            stack.enter_context(patch.object(bump_repairs, "build_bump_mcp", return_value={}))
            stack.enter_context(patch.object(bump_repairs, "load_prompt", return_value=""))
            stack.enter_context(patch.object(bump_repairs, "load_role_prompt", return_value=""))
            stack.enter_context(patch.object(bump_runtime, "forum_brief", return_value=""))
            stack.enter_context(patch.object(bump_repairs, "source_diagnosis_input", return_value={}))
            stack.enter_context(patch.object(bump_repairs.bump_state, "claim_source_issue", return_value={
                "status": "claimed", "issue": {"issue_id": "issue-1"}, "attempt_id": "attempt-1",
            }))
            stack.enter_context(patch.object(bump_repairs.bump_state, "load_state", return_value={
                "phase": "formalizing", "run_id": "fixture",
            }))
            self.finish = stack.enter_context(patch.object(
                bump_repairs.bump_state, "finish_source_repair_attempt", return_value={"status": "open"},
            ))
            self.spawn = stack.enter_context(patch.object(
                bump_repairs, "spawn", new_callable=AsyncMock, side_effect=failure, return_value="done",
            ))
            return await bump_repairs.source_repair_turn(self.agent, self.roster, self.paths, "issue-1", 5)

    async def representation_turn(self, failure=None):
        record = {"input_sha256": "input-1", "main_sha": "main-1",
                  "input": {"targets": ["Witness"], "repairs": []}}
        state = {"run_id": "fixture", "formalization": {"contract": {"targets": {"Witness": {}}}},
                 "source_repairs": {}}
        with ExitStack() as stack:
            self.patch_common(stack)
            stack.enter_context(patch.object(bump_representation, "claim_representation_review", return_value={
                "status": "claimed", "review": record,
            }))
            stack.enter_context(patch.object(bump_representation, "_state", return_value=SimpleNamespace(
                load_state=Mock(return_value=state),
            )))
            stack.enter_context(patch.object(bump_representation, "representation_review_input", return_value={
                "input_sha256": "input-1",
            }))
            stack.enter_context(patch.object(artifacts, "store_text", return_value={"artifact_id": "artifact-1"}))
            self.finish = stack.enter_context(patch.object(
                bump_representation, "finish_representation_attempt", return_value=record,
            ))
            self.spawn = stack.enter_context(patch.object(
                bump_spawn, "spawn", new_callable=AsyncMock, side_effect=failure, return_value="done",
            ))
            return await bump_representation.representation_review_turn(
                self.agent, self.roster, self.paths, "task-1",
            )

    async def dispatch(self, outcomes, *, worker=None):
        agents = [SimpleNamespace(name=name) for name in ("Ada", "Bert")[:len(outcomes)]]
        roster = SimpleNamespace(agents=agents, primary=agents[0])
        with ExitStack() as stack:
            self.patch_common(stack)
            stack.enter_context(patch.object(bump_orchestrator, "load_prompt", return_value=""))
            stack.enter_context(patch.object(bump_orchestrator.library, "library_context", return_value=""))
            stack.enter_context(patch.object(bump_orchestrator.library, "library_subagents", return_value=[]))
            stack.enter_context(patch.object(config, "find_unity_dir", return_value=None))
            self.spawn = stack.enter_context(patch.object(
                bump_orchestrator, "spawn", new_callable=AsyncMock,
                side_effect=worker if worker is not None else outcomes,
            ))
            return await bump_orchestrator.dispatch(
                agents, roster, "phase", "task", self.paths.project_root, {},
                brief_provider=lambda _name: "", log_context={"phase": "critic"},
                env_overrides={"UNITY_BUMP_DRAFT_PATH": "/fixture/draft"},
            )

    async def test_source_repair_cleanup_fault_propagates_after_attempt_finish(self):
        error = bump_spawn.SpawnCleanupError("fixture unresolved execution")
        with self.assertRaises(bump_spawn.SpawnCleanupError) as raised:
            await self.source_turn(error)
        self.assertIs(raised.exception, error)
        self.spawn.assert_awaited_once()
        self.finish.assert_called_once_with(
            self.paths.forum, "issue-1", "Ada", "attempt-1",
            error="SpawnCleanupError: fixture unresolved execution",
        )

    async def test_source_repair_ordinary_failure_keeps_existing_attempt_result(self):
        result = await self.source_turn(RuntimeError("fixture ordinary failure"))
        self.assertEqual(result, {"status": "attempted", "issue": {"status": "open"},
                                  "error": "RuntimeError: fixture ordinary failure"})
        self.spawn.assert_awaited_once()
        self.finish.assert_called_once()

    async def test_source_repair_cancellation_keeps_existing_interrupted_bookkeeping(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.source_turn(asyncio.CancelledError())
        self.finish.assert_called_once_with(
            self.paths.forum, "issue-1", "Ada", "attempt-1", error="source repair interrupted",
        )

    async def test_source_repair_success_keeps_existing_dispatch_arguments(self):
        result = await self.source_turn()
        self.assertEqual(result["status"], "attempted")
        self.assertEqual(result["error"], "")
        self.assertTrue(self.spawn.call_args.kwargs["own_process_group"])
        self.assertEqual(self.spawn.call_args.kwargs["env_overrides"]["UNITY_BUMP_PROFILE"], "source_repair")

    async def test_representation_cleanup_fault_propagates_after_attempt_finish(self):
        error = bump_spawn.SpawnCleanupError("fixture unresolved execution")
        with self.assertRaises(bump_spawn.SpawnCleanupError) as raised:
            await self.representation_turn(error)
        self.assertIs(raised.exception, error)
        self.spawn.assert_awaited_once()
        self.finish.assert_called_once_with(
            self.paths.forum, "input-1", "Ada", ["Ada"],
            "SpawnCleanupError: fixture unresolved execution", interrupted=False,
        )

    async def test_representation_ordinary_failure_keeps_existing_attempt_result(self):
        result = await self.representation_turn(RuntimeError("fixture ordinary failure"))
        self.assertEqual(result["status"], "attempted")
        self.assertEqual(result["error"], "RuntimeError: fixture ordinary failure")
        self.spawn.assert_awaited_once()
        self.finish.assert_called_once()

    async def test_representation_cancellation_keeps_existing_interrupted_bookkeeping(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.representation_turn(asyncio.CancelledError())
        self.finish.assert_called_once_with(
            self.paths.forum, "input-1", "Ada", ["Ada"],
            "representation review interrupted", interrupted=True,
        )

    async def test_representation_success_keeps_existing_dispatch_arguments(self):
        result = await self.representation_turn()
        self.assertEqual(result["status"], "attempted")
        self.assertEqual(result["error"], "")
        self.assertTrue(self.spawn.call_args.kwargs["own_process_group"])
        self.assertEqual(self.spawn.call_args.kwargs["env_overrides"]["UNITY_BUMP_PROFILE"], "representation_review")

    async def test_dispatch_cleanup_fault_propagates_after_existing_gather(self):
        error = bump_spawn.SpawnCleanupError("fixture unresolved execution")
        peer_started = asyncio.Event()

        async def worker(agent, *_args, **_kwargs):
            if agent.name == "Ada":
                await peer_started.wait()
                raise error
            peer_started.set()
            return "done"

        with self.assertRaises(bump_spawn.SpawnCleanupError) as raised:
            await self.dispatch([error, "done"], worker=worker)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.spawn.await_count, 2)

    async def test_dispatch_cleanup_fault_cancels_and_drains_only_its_blocked_peer(self):
        for failed_name in ("Ada", "Bert"):
            with self.subTest(failed_name=failed_name):
                error = bump_spawn.SpawnCleanupError("fixture unresolved execution")
                peer_started = asyncio.Event()
                peer_cancelled = asyncio.Event()
                peer_drained = asyncio.Event()
                unrelated = asyncio.create_task(asyncio.Event().wait())

                async def worker(agent, *_args, **_kwargs):
                    if agent.name == failed_name:
                        await peer_started.wait()
                        raise error
                    peer_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        peer_cancelled.set()
                        raise
                    finally:
                        await asyncio.sleep(0.01)
                        peer_drained.set()

                try:
                    with self.assertRaises(bump_spawn.SpawnCleanupError) as raised:
                        await asyncio.wait_for(self.dispatch([None, None], worker=worker), timeout=0.5)
                    self.assertIs(raised.exception, error)
                    self.assertTrue(peer_cancelled.is_set())
                    self.assertTrue(peer_drained.is_set(), "fatal failure escaped before peer cleanup drained")
                    self.assertFalse(unrelated.done(), "dispatch cancelled an unrelated task")
                    self.assertEqual(self.spawn.await_count, 2)
                finally:
                    unrelated.cancel()
                    await asyncio.gather(unrelated, return_exceptions=True)

    async def test_dispatch_ordinary_failure_still_waits_for_and_does_not_cancel_peer(self):
        error = RuntimeError("fixture ordinary failure")
        peer_started = asyncio.Event()
        peer_release = asyncio.Event()
        peer_cancelled = asyncio.Event()

        async def worker(agent, *_args, **_kwargs):
            if agent.name == "Ada":
                raise error
            peer_started.set()
            try:
                await peer_release.wait()
            except asyncio.CancelledError:
                peer_cancelled.set()
                raise
            return "done"

        dispatch = asyncio.create_task(self.dispatch([None, None], worker=worker))
        try:
            await asyncio.wait_for(peer_started.wait(), timeout=0.5)
            await asyncio.sleep(0)
            self.assertFalse(dispatch.done(), "ordinary failure no longer waits for its peers")
            self.assertFalse(peer_cancelled.is_set())
            peer_release.set()
            self.assertEqual(await asyncio.wait_for(dispatch, timeout=0.5), [error, "done"])
            self.assertFalse(peer_cancelled.is_set())
        finally:
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)

    async def test_dispatch_ordinary_error_and_order_are_unchanged(self):
        error = RuntimeError("fixture ordinary failure")
        self.assertEqual(await self.dispatch([error, "done"]), [error, "done"])
        self.assertEqual(self.spawn.await_count, 2)

    async def test_dispatch_success_does_not_expand_process_or_environment_policy(self):
        self.assertEqual(await self.dispatch(["done"]), ["done"])
        kwargs = self.spawn.call_args.kwargs
        self.assertNotIn("own_process_group", kwargs)
        self.assertEqual(kwargs["env_overrides"], {"UNITY_BUMP_DRAFT_PATH": "/fixture/draft"})
        self.assertEqual(kwargs["mcp_profile"], "bump")


if __name__ == "__main__":
    unittest.main()
