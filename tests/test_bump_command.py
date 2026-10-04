"""Command adapter controls; no compiler, model or service calls."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner
from unity import bump_bootstrap
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
        self.roster = SimpleNamespace(agents=[SimpleNamespace(name="Ada")])
        self.roster.primary = self.roster.agents[0]
        for module, name, kwargs in (
            (command, "load_paths", {"return_value": self.source}),
            (command, "load_roster", {"return_value": self.roster}),
            (command, "_run_migration_loop", {"new_callable": AsyncMock}),
            (command.bump_state, "load_state", {"return_value": {"phase": "complete"}}),
            (command.bump_jobs, "terminate", {}),
            (command, "recover_interrupted_formal_merges", {}),
            (command.bump_state, "recover_source_repairs", {}),
            (bump_bootstrap, "prepare", {"return_value": self.target}),
            (bump_bootstrap, "resume", {"return_value": self.target}),
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
                                            project_scope="build", architect="off")
        self.resume.assert_not_called()
        self._run_migration_loop.assert_awaited_once()
        self.assertEqual(self._run_migration_loop.await_args.args[1], self.target)
        self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(json.loads((self.source.unity / "state.json").read_text())["phase"], "done")

    async def test_resume_does_not_repeat_preparation(self):
        result = await self.invoke("--continue")
        self.assertEqual(result.exit_code, 0, result.output)
        self.prepare.assert_not_called()
        self.resume.assert_called_once_with(self.source, None, {}, project_scope=None, architect=None)

    async def test_missing_version_cannot_start_fresh_migration(self):
        result = await self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.prepare.assert_not_called()
        self._run_migration_loop.assert_not_awaited()

    async def test_duplicate_dependency_rejected_before_preparation(self):
        result = await self.invoke("v4.34.1", "--dependency", "mathlib=" + "a" * 40,
                                   "--dependency", "mathlib=" + "b" * 40)
        self.assertNotEqual(result.exit_code, 0)
        self.prepare.assert_not_called()

    async def test_failure_restores_cwd_and_marks_source_stopped(self):
        cwd = Path.cwd()
        self._run_migration_loop.side_effect = ValueError("migration failure")
        result = await self.invoke("v4.34.1")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(json.loads((self.source.unity / "state.json").read_text())["phase"], "stopped")

    def test_empty_scheduler_pass_is_not_charged_as_proof_attempt(self):
        with self.assertRaisesRegex(Exception, "no proof attempt was charged"):
            command._formal_round_attempted({"formalization": {"last_round": {
                "round_id": "round", "outcome": "blocked", "pending_tasks": [],
                "blocked_launches": {}, "activity": {}}}}, None)
