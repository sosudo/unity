"""Command adapter controls; no compiler, model or service calls."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner
from unity import bump_setup, bump_tasks, bump_workflow
from unity.commands import bump as command
from unity.config import Paths
from unity.bump_input import bump_paths


class BumpCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = bump_paths(Paths.from_unity_dir(self.root / ".unity"))
        target_root = self.root / "owned-target"
        target_root.mkdir()
        self.target = bump_paths(Paths.from_unity_dir(target_root / ".unity"))
        self.target.unity.mkdir()
        self.source.unity.mkdir()
        self.roster = SimpleNamespace(agents=[SimpleNamespace(name="Ada"), SimpleNamespace(name="Bert")])
        self.roster.primary = self.roster.agents[0]
        for module, name, kwargs in (
            (command, "load_paths", {"return_value": self.source}),
            (command, "load_roster", {"return_value": self.roster}),
            (bump_workflow, "run", {"new_callable": AsyncMock}),
            (bump_tasks, "load", {"return_value": {"phase": "complete"}}),
            (command.bump_jobs, "terminate", {}),
            (command, "recover_interrupted_formal_merges", {}),
            (command.bump_state, "recover_source_repairs", {}),
            (bump_setup, "prepare", {"return_value": self.target}),
            (bump_setup, "resume", {"return_value": self.target}),
        ):
            patcher = patch.object(module, name, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    async def invoke(self, *args):
        return await CliRunner().invoke(command.command, list(args))

    async def test_fresh_arguments_enter_owned_target_runtime(self):
        cwd = Path.cwd()
        result = await self.invoke("v4.34.1", "--dependency", "mathlib=" + "a" * 40, "--architect", "off")
        self.assertEqual(result.exit_code, 0, result.output)
        self.prepare.assert_called_once_with(self.source, "v4.34.1", {"mathlib": "a" * 40},
                                            project_scope="build", modules=(), lake_args=())
        self.resume.assert_not_called()
        self.run.assert_awaited_once()
        self.assertEqual(self.run.await_args.args[0], self.target)
        self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(json.loads((self.source.unity / "state.json").read_text())["phase"], "done")

    async def test_resume_does_not_repeat_preparation(self):
        result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.prepare.assert_not_called()
        self.resume.assert_called_once_with(self.source, None, {}, project_scope=None, modules=None, lake_args=None)
        self.terminate.assert_not_called()
        self.recover_interrupted_formal_merges.assert_not_called()

    async def test_missing_version_cannot_start_fresh_migration(self):
        result = await self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.prepare.assert_not_called()
        self.run.assert_not_awaited()

    async def test_duplicate_dependency_rejected_before_preparation(self):
        result = await self.invoke("v4.34.1", "--dependency", "mathlib=" + "a" * 40,
                                   "--dependency", "mathlib=" + "b" * 40)
        self.assertNotEqual(result.exit_code, 0)
        self.prepare.assert_not_called()

    async def test_failure_restores_cwd_and_marks_source_stopped(self):
        cwd = Path.cwd()
        self.run.side_effect = ValueError("migration failure")
        result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(json.loads((self.source.unity / "state.json").read_text())["phase"], "stopped")

    async def test_supplemental_modules_and_consistent_lake_options(self):
        result = await self.invoke("v4.34.1", "--module", "Poly.UPFan", "--module", "Poly.CommDiag",
                                   "--lake-option=-Kenv=dev")
        self.assertEqual(result.exit_code, 0, result.output)
        self.prepare.assert_called_once_with(self.source, "v4.34.1", {}, project_scope="build",
                                              modules=("Poly.UPFan", "Poly.CommDiag"), lake_args=["-Kenv=dev"])

    async def test_no_automatic_architect_installation(self):
        result = await self.invoke("v4.34.1", "--architect", "auto")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no longer automatically installs", result.output)
        self.prepare.assert_not_called()

    async def test_lake_command_injection_option_rejected(self):
        result = await self.invoke("v4.34.1", "--lake-option=update")
        self.assertNotEqual(result.exit_code, 0)
        self.prepare.assert_not_called()

    async def test_team_loaded_before_preparation(self):
        order = []
        self.load_roster.side_effect = lambda *args, **kwargs: (order.append("roster"), self.roster)[1]
        self.prepare.side_effect = lambda *args, **kwargs: (order.append("prepare"), self.target)[1]
        result = await self.invoke("v4.34.1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(order, ["roster", "prepare"])

    async def test_duplicate_authors_fail_before_prepare(self):
        self.roster.agents = [SimpleNamespace(name="Ada"), SimpleNamespace(name="ada")]
        result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        self.prepare.assert_not_called()

    async def test_single_primary_can_enter_environment_preparation_and_runtime(self):
        self.roster.agents = [SimpleNamespace(name="Ada")]
        self.roster.primary = self.roster.agents[0]
        result = await self.invoke("v4.34.1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.prepare.assert_called_once()
        self.run.assert_awaited_once_with(self.target, self.roster, max_attempts=command._attempt_limit())
        self.assertIn("primary migration review passed", result.output)

    async def test_empty_roster_still_fails_before_preparation(self):
        self.roster.agents = []
        result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        self.prepare.assert_not_called()
        self.run.assert_not_awaited()

    async def test_current_live_job_blocks_resume_without_termination(self):
        directory = self.target.unity / "jobs" / "bump"
        directory.mkdir(parents=True)
        record = directory / "job.json"
        record.write_text(json.dumps({"pid": 12345, "pgid": 12345}))
        with patch.object(command.os, "kill") as kill, patch.object(command.os, "killpg") as killpg:
            result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        kill.assert_called_once_with(12345, 0)
        killpg.assert_not_called()
        self.terminate.assert_not_called()
        self.run.assert_not_awaited()
        self.assertTrue(record.exists())

    async def test_stale_jobs_preserved_without_signals_on_resume(self):
        directory = self.target.unity / "jobs" / "bump"
        directory.mkdir(parents=True)
        record = directory / "job.json"
        record.write_text(json.dumps({"pid": 12345, "pgid": 12345}))
        with patch.object(command.os, "kill", side_effect=ProcessLookupError), \
                patch.object(command.os, "killpg", side_effect=ProcessLookupError):
            result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.terminate.assert_not_called()
        self.assertTrue(record.exists())

    async def test_worker_session_receipt_blocks_resume_without_valid_closure(self):
        directory = self.target.forum / "processes"
        directory.mkdir(parents=True)
        (directory / "worker.json").write_text(json.dumps({"pid": 12345, "pgid": 12345, "sid": 12345, "start_ticks": 99}))
        with patch.object(command.os, "kill") as kill:
            result = await self.invoke("--continue")
        self.assertNotEqual(result.exit_code, 0)
        kill.assert_called_once_with(12345, 0)
        self.run.assert_not_awaited()

    async def test_verified_closed_worker_receipt_is_not_historical_pid_authority(self):
        directory = self.target.forum / "processes"
        directory.mkdir(parents=True)
        record = directory / "worker.json"
        record.write_text(json.dumps({"pid": 12345, "pgid": 12345, "sid": 12345, "start_ticks": 99}))
        closure = {"receipt_sha256": command.hashlib.sha256(record.read_bytes()).hexdigest(), "pgid": 12345, "group_empty": True}
        record.with_suffix(".closed.json").write_text(json.dumps(closure))
        with patch.object(command.os, "kill") as kill, patch.object(command.os, "killpg") as killpg:
            result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        kill.assert_not_called()
        killpg.assert_not_called()

    def test_empty_scheduler_pass_is_not_charged_as_proof_attempt(self):
        with self.assertRaisesRegex(Exception, "no proof attempt was charged"):
            command._formal_round_attempted({"formalization": {"last_round": {
                "round_id": "round", "outcome": "blocked", "pending_tasks": [],
                "blocked_launches": {}, "activity": {}}}}, None)
