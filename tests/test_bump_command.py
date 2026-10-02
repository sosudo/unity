"""Ported Bump command lifecycle; all provider/native/acceptance gates mocked."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner

from unity import bump_state
from unity.commands import bump as command
from unity.config import Paths
from unity.bump_input import bump_paths


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="unity-bump-command-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.source = Paths.from_unity_dir(root / "original/.unity")
        self.paths = bump_paths(Paths.from_unity_dir(root / "target/.unity"))
        for paths in (self.source, self.paths):
            paths.unity.mkdir(parents=True)
            paths.forum.mkdir(parents=True, exist_ok=True)
        source = {"kind": "supplied_sources", "candidate_id": "source-" + "a" * 64,
                  "sha256": "a" * 64, "source_refs": [{"ref_id": "source:original",
                  "path": ".unity/source/original", "sha256": "b" * 64}]}
        bump_state.initialize_source(self.paths.forum, "c" * 64, "d" * 40, source,
                                     project_baseline={"policy": "migration-v1"}, reset=True)
        with bump_state.transaction(self.paths.forum) as state:
            state["phase"] = "formalizing"
            state["formal_tasks"] = {"Fixture": {"task_id": "Fixture", "status": "pending"}}
            state["migration_max_attempts"] = 1
        self.roster = SimpleNamespace(primary=SimpleNamespace(name="Luna1", backend="codex"))
        self.roster.agents = [self.roster.primary]
        self.real_run_critics = command._run_critics
        self.mocks = {}
        self.add_patch(command, "load_paths", return_value=self.source)
        self.add_patch(command, "load_roster", return_value=self.roster)
        self.add_patch(command, "stop_requested", return_value=False)
        self.add_patch(command, "require_source_matches")
        self.add_patch(command, "load_prompt", return_value="Offline migration instructions")
        self.add_patch(command, "build_bump_mcp", return_value={})
        self.add_patch(command, "persist_report", return_value={})
        self.add_patch(command, "mark_done")
        self.add_patch(command, "recover_interrupted_formal_merges")
        self.add_patch(command.bump_jobs, "terminate", return_value=0)
        self.add_patch(command.bump_bootstrap, "prepare", return_value=self.paths)
        self.add_patch(command.bump_bootstrap, "resume", return_value=self.paths)
        self.add_patch(command.bump_bootstrap, "check_ready_modules", side_effect=lambda paths: bump_state.load_state(paths.forum))
        self.add_patch(command, "run_formalizing_runtime", new_callable=AsyncMock, side_effect=self.run_round)
        self.add_patch(command, "_prepare_critic_snapshot", side_effect=self.prepare_critic)
        self.add_patch(command, "_run_critics", new_callable=AsyncMock, side_effect=self.finish_critic)
        self.add_patch(command, "dispatch", new_callable=AsyncMock, side_effect=AssertionError("no provider calls"))
        environment = patch.dict("os.environ", {"MAX_ATTEMPTS": "1", "RETROSPECTIVE": "false"})
        environment.start()
        self.addCleanup(environment.stop)

    def add_patch(self, module, name, **kwargs):
        patcher = patch.object(module, name, **kwargs)
        self.mocks[name] = patcher.start()
        self.addCleanup(patcher.stop)

    async def run_round(self, roster, paths, mcp, prompt):
        self.assertEqual(Path.cwd(), self.paths.project_root)
        with bump_state.transaction(paths.forum) as state:
            state["formalization"]["last_round"] = {
                "round_id": "round-one", "outcome": "completed", "activity": {"workers": 1}}
        return bump_state.load_state(paths.forum)

    def prepare_critic(self, paths):
        bump_state.set_phase(paths.forum, "critic")
        return True

    async def finish_critic(self, roster, paths, limit):
        with bump_state.transaction(paths.forum) as state:
            state["phase"] = "complete"
            state["formalization"]["status"] = "accepted"

    async def invoke(self, *args):
        return await CliRunner().invoke(command.command, list(args))

    async def test_fresh_uses_copied_runtime_critic_and_report(self):
        before = Path.cwd()
        result = await self.invoke("v4.34.1", "--dependency", "mathlib=v4.34.1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["prepare"].assert_called_once_with(self.source, "v4.34.1", {"mathlib": "v4.34.1"}, project_scope="build")
        self.mocks["run_formalizing_runtime"].assert_awaited_once()
        self.mocks["_run_critics"].assert_awaited_once()
        self.mocks["persist_report"].assert_called_once_with(self.paths, accepted=True)
        self.assertEqual(Path.cwd(), before)
        self.assertFalse(hasattr(command, "_chunk_source"))

    async def test_continue_uses_existing_workspace_without_fresh_setup(self):
        result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["resume"].assert_called_once_with(self.source, None, {}, project_scope=None)
        self.mocks["prepare"].assert_not_called()
        self.mocks["recover_interrupted_formal_merges"].assert_called_once_with(self.paths)

    async def test_explicit_all_scope_is_forwarded_without_downgrade(self):
        result = await self.invoke("v4.34.1", "--project-scope", "all")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["prepare"].assert_called_once_with(self.source, "v4.34.1", {}, project_scope="all")

    async def test_optional_architect_mode_is_forwarded_for_fresh_run(self):
        result = await self.invoke("v4.34.1", "--architect", "off")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["prepare"].assert_called_once_with(
            self.source, "v4.34.1", {}, project_scope="build", architect="off")

    async def test_continuation_cannot_change_optional_instrumentation(self):
        result = await self.invoke("--continue", "--architect", "auto")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot change optional instrumentation", result.output)
        self.mocks["resume"].assert_not_called()
        self.mocks["prepare"].assert_not_called()

    async def test_invalid_optional_instrumentation_rejected_before_setup(self):
        result = await self.invoke("v4.34.1", "--architect", "anything")
        self.assertNotEqual(result.exit_code, 0)
        self.mocks["prepare"].assert_not_called()

    async def test_continue_explicit_scope_is_checked_by_resume(self):
        self.mocks["resume"].side_effect = ValueError("--continue cannot change the sealed project scope")
        result = await self.invoke("--continue", "--project-scope", "all")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("sealed project scope", result.output)
        self.mocks["resume"].assert_called_once_with(self.source, None, {}, project_scope="all")
        self.mocks["run_formalizing_runtime"].assert_not_awaited()

    async def test_unknown_scope_rejected_before_setup(self):
        result = await self.invoke("v4.34.1", "--project-scope", "some")
        self.assertNotEqual(result.exit_code, 0)
        self.mocks["prepare"].assert_not_called()

    async def test_budget_exhaustion_is_incomplete_and_not_done(self):
        with bump_state.transaction(self.paths.forum) as state:
            state["migration_attempts"] = 1
        result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("exhausted", result.output)
        self.mocks["run_formalizing_runtime"].assert_not_awaited()
        self.mocks["mark_done"].assert_not_called()
        self.mocks["persist_report"].assert_called_once_with(self.paths, accepted=False)

    async def test_continue_cannot_change_attempt_policy(self):
        with patch.dict("os.environ", {"MAX_ATTEMPTS": "2"}):
            result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot reset or change", result.output)
        self.mocks["run_formalizing_runtime"].assert_not_awaited()

    async def test_no_dispatch_round_is_not_charged(self):
        async def blocked(*args):
            with bump_state.transaction(self.paths.forum) as state:
                state["formalization"]["last_round"] = {"round_id": "blocked", "outcome": "blocked",
                                                         "pending_tasks": [{"task_id": "Fixture"}]}
            return bump_state.load_state(self.paths.forum)
        self.mocks["run_formalizing_runtime"].side_effect = blocked
        result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        state = bump_state.load_state(self.paths.forum)
        self.assertEqual(state.get("migration_attempts", 0), 0)
        self.mocks["mark_done"].assert_not_called()

    async def test_source_replan_cannot_replace_original_obligations(self):
        with patch.object(bump_state, "pending_replan", return_value={"request_id": "change-original"}):
            result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("frozen original-project", result.output)
        self.mocks["run_formalizing_runtime"].assert_not_awaited()

    async def test_cancellation_never_marks_done(self):
        self.mocks["stop_requested"].return_value = True
        result = await self.invoke("v4.34.1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks["persist_report"].assert_called_once_with(self.paths, accepted=False)
        self.mocks["mark_done"].assert_not_called()

    async def test_report_failure_is_not_completion(self):
        self.mocks["persist_report"].side_effect = ValueError("stale evidence")
        result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        self.mocks["mark_done"].assert_not_called()

    async def test_operational_runtime_failure_persists_incomplete_report(self):
        self.mocks["run_formalizing_runtime"].side_effect = RuntimeError("required provider unavailable")
        result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("required provider unavailable", result.output)
        self.mocks["persist_report"].assert_called_once_with(self.paths, accepted=False)
        self.mocks["mark_done"].assert_not_called()

    async def test_duplicate_dependency_rejected_before_setup(self):
        result = await self.invoke("v4.34.1", "--dependency", "x=v1.0.0", "--dependency", "x=v2.0.0")
        self.assertNotEqual(result.exit_code, 0)
        self.mocks["prepare"].assert_not_called()

    async def test_missing_supported_critic_blocks_before_setup(self):
        self.roster.primary.backend = "claude_code"
        result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("requires a Codex agent", result.output)
        self.mocks["prepare"].assert_not_called()

    async def test_mixed_roster_critic_rotation_skips_unsupported_backends(self):
        self.roster.primary.backend = "claude_code"
        critic = SimpleNamespace(name="CodexCritic", backend="codex")
        self.roster.agents.append(critic)
        with bump_state.transaction(self.paths.forum) as state:
            state["phase"] = "critic"
            state["formalization"]["review_snapshot"] = {"snapshot_id": "current-snapshot"}

        async def approve(*args, **kwargs):
            await self.finish_critic(self.roster, self.paths, 1)

        with patch.object(command, "_run_critic", side_effect=approve) as run:
            await self.real_run_critics(self.roster, self.paths, 1)
        self.assertIs(run.call_args.kwargs["critic"], critic)

    def test_critic_attempts_are_snapshot_bound_and_persisted(self):
        with bump_state.transaction(self.paths.forum) as state:
            state["formalization"]["review_snapshot"] = {"snapshot_id": "snapshot-one"}
        self.assertEqual(command._begin_critic_attempt(self.paths, "snapshot-one", "Luna1"), 1)
        self.assertEqual(command._begin_critic_attempt(self.paths, "snapshot-one", "luna1"), 2)
        self.assertEqual(command._critic_attempt_count(bump_state.load_state(self.paths.forum), "snapshot-one", "Luna1"), 2)
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            command._begin_critic_attempt(self.paths, "different", "Luna1")

    def test_completed_round_charged_once_across_resume(self):
        with bump_state.transaction(self.paths.forum) as state:
            state["formalization"]["last_round"] = {"round_id": "r", "outcome": "completed", "activity": {"workers": 1}}
        self.assertEqual(command._charge_completed_round(self.paths), 1)
        self.assertEqual(command._charge_completed_round(self.paths), 1)

    def test_default_attempt_limit_is_finite(self):
        with patch.dict("os.environ", {"MAX_ATTEMPTS": ""}):
            self.assertEqual(command._attempt_limit(), 5)

    async def test_critic_exhaustion_cannot_be_reset_by_continuing(self):
        with bump_state.transaction(self.paths.forum) as state:
            state["phase"] = "critic"
            state["formalization"]["review_snapshot"] = {"snapshot_id": "current-snapshot"}
        with patch.object(command, "_run_critic", new_callable=AsyncMock) as run:
            for _ in range(2):
                with self.assertRaisesRegex(Exception, "exhausted its critic attempts"):
                    await self.real_run_critics(self.roster, self.paths, 1)
            run.assert_awaited_once()
        state = bump_state.load_state(self.paths.forum)
        self.assertEqual(command._critic_attempt_count(state, "current-snapshot", "Luna1"), 1)
