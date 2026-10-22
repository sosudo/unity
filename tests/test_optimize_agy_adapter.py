"""Offline Optimize AGY adapter contracts; all native execution is mocked."""

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from unity import optimize_agy_sandbox, optimize_spawn, spawn
from unity.roster import Agent


class OptimizeAgyAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="optimize-agy-adapter-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source with spaces"
        self.source.mkdir()
        self.control = self.base / "sandbox-control"
        self.control.mkdir()
        self.shared_runtime = self.base / "shared-runtime"
        self.shared_runtime.mkdir()
        self.agy = str(self.base / "fake-agy")
        self.wrapper = self.control / "host-wrapper"
        self.receipt = self.control / "owned.pid"
        self.agent = Agent("fixture", "fixture-model", "fixture-provider", "antigravity", 1,
                           None, None, None, None, True)
        self.environment = {
            "UNITY_OPTIMIZE_PROFILE": "optimizing", "UNITY_OPTIMIZE_SCORE_REQUEST_ID": "score-fixture",
            "UNITY_OPTIMIZE_PROJECT_ROOT": str(self.base), "UNITY_OPTIMIZE_WORKTREE": str(self.source),
            "UNITY_AGENT_NAME": "fixture", "PATH": "/fixture/bin",
        }
        self.prepare = self.enterContext(patch.object(optimize_agy_sandbox, "prepare_sandbox"))
        self.roots = self.enterContext(patch.object(optimize_spawn, "_worktree_write_roots",
                                                    return_value=(self.source, self.shared_runtime)))
        self.group = self.enterContext(patch.object(optimize_spawn, "_process_group_wrapper",
                                                    return_value=(self.wrapper, self.receipt)))
        self.enterContext(patch.object(optimize_spawn, "_agent_env",
            side_effect=lambda agent, env_overrides=None: dict(env_overrides or {})))
        self.enterContext(patch.object(optimize_spawn, "_stop_requested", return_value=False))
        self.enterContext(patch.object(optimize_spawn, "_write_run_log"))
        self.enterContext(patch.object(optimize_spawn, "_log"))
        self.enterContext(patch.object(optimize_spawn._console, "print"))
        self.enterContext(patch.object(spawn._console, "print"))
        self.enterContext(patch("shutil.which", return_value=self.agy))
        self.sandbox = self.new_sandbox()

    def new_sandbox(self, events=None):
        # Use the real carrier type, but never prepare or execute a native sandbox.
        sandbox = optimize_agy_sandbox.Sandbox(self.control, self.control / "runner",
            self.control / "tmp", self.control / "agy-readonly", (), {})
        sandbox.preflight = Mock()
        sandbox.close = Mock(side_effect=(lambda: events.append("sandbox closed")) if events is not None else None)
        self.prepare.return_value = sandbox
        return sandbox

    def options(self, environment=None, *, own_process_group=True, sandboxes=None):
        return optimize_spawn._antigravity_options(self.agent, self.agy, self.source, "system", "task",
            self.environment if environment is None else environment, own_process_group, "optimize",
            sandboxes=[] if sandboxes is None else sandboxes)

    async def invoke(self, name, *, callback=None):
        return await getattr(optimize_spawn, name)(self.agent, "system", "task", self.source, {},
            env_overrides=self.environment, own_process_group=True, on_normal_completion=callback)

    def process(self, *, status="SUCCESS", response="done"):
        line = json.dumps({"event": "result", "result": {"response": response, "status": status}}).encode() + b"\n"
        return SimpleNamespace(stdout=SimpleNamespace(readline=AsyncMock(side_effect=[line, b""])),
            returncode=0, pid=123456, wait=AsyncMock(return_value=0), kill=Mock(), terminate=Mock())

    def test_scoring_and_critic_use_outer_owned_wrapper_and_source_context(self):
        for profile, request in (("optimizing", "score-fixture"), ("critic", "")):
            with self.subTest(profile=profile):
                self.prepare.reset_mock()
                self.group.reset_mock()
                sandbox = self.new_sandbox()
                env = {**self.environment, "UNITY_OPTIMIZE_PROFILE": profile,
                       "UNITY_OPTIMIZE_SCORE_REQUEST_ID": request}
                before = dict(env)
                sandboxes = []
                result = self.options(env, sandboxes=sandboxes)
                self.prepare.assert_called_once_with(self.agy, self.source, env)
                sandbox.preflight.assert_called_once_with()
                sandbox.close.assert_not_called()
                self.assertEqual(sandboxes, [sandbox])
                self.group.assert_called_once_with(sandbox.executable, sandbox.directory, "antigravity", isolated=True)
                self.assertEqual(result["cmd"][0], str(self.wrapper))
                self.assertEqual(result["cmd"][1], "--print")
                self.assertEqual(result["cmd"][2], result["full"])
                self.assertEqual(result["pid_file"], self.receipt)
                self.assertNotIn("--dangerously-skip-permissions", result["cmd"])
                self.assertEqual(result["cmd"][-5:],
                    ["--sandbox", "--add-dir", str(self.source), "--add-dir", str(self.shared_runtime)])
                self.assertIn("private runtime directory outside source", result["full"])
                self.assertIn(str(self.source), result["full"])
                self.assertNotIn("plan.json", result["full"])
                self.assertEqual(env, before)

    def test_readonly_requires_owned_cleanup_and_lifetime_tracking(self):
        for owned, sandboxes in ((False, []), (True, None)):
            with self.subTest(owned=owned, sandboxes=sandboxes):
                with self.assertRaisesRegex(ValueError, "require owned process cleanup"):
                    optimize_spawn._antigravity_options(self.agent, self.agy, self.source, "system", "task",
                        self.environment, owned, "optimize", sandboxes=sandboxes)
        self.prepare.assert_not_called()
        self.group.assert_not_called()

    def test_ordinary_optimize_writer_command_and_defaults_are_unchanged(self):
        self.roots.return_value = ()
        env = {**self.environment, "UNITY_OPTIMIZE_SCORE_REQUEST_ID": ""}
        result = self.options(env, own_process_group=False)
        full = "system" + optimize_spawn._codex_mcp_note("optimize", "optimizing") + "\n\n---\n\nTASK:\ntask"
        self.assertEqual(result, {"full": full, "cmd": [self.agy, "--print", full,
            "--model", "fixture-model", "--output-format", "stream-json", "--dangerously-skip-permissions",
            "--print-timeout", "72h"], "pid_file": None})
        self.prepare.assert_not_called()
        self.group.assert_not_called()

    def test_non_scoring_profiles_do_not_prepare_readonly_sandbox(self):
        for profile in ("architect", "preparation", "exploration", "chunking", "optimizing", "retrospective"):
            with self.subTest(profile=profile):
                env = {**self.environment, "UNITY_OPTIMIZE_PROFILE": profile}
                env.pop("UNITY_OPTIMIZE_SCORE_REQUEST_ID")
                result = self.options(env, own_process_group=False)
                self.assertIn("--sandbox", result["cmd"])
                self.assertEqual(result["cmd"][-2:], ["--add-dir", str(self.shared_runtime)])
                self.assertNotIn("READ-ONLY SOURCE:", result["full"])
        self.prepare.assert_not_called()

    def test_owned_writer_keeps_original_executable_and_separate_receipt_directory(self):
        env = {**self.environment, "UNITY_OPTIMIZE_SCORE_REQUEST_ID": ""}
        group_dir = self.base / "writer-group"
        with patch.object(optimize_spawn.tempfile, "mkdtemp", return_value=str(group_dir)) as temporary:
            result = self.options(env)
        temporary.assert_called_once_with(prefix="unity-antigravity-group-")
        self.group.assert_called_once_with(Path(self.agy), group_dir, "antigravity")
        self.assertEqual(result["cmd"][0], str(self.wrapper))
        self.prepare.assert_not_called()

    async def test_both_entrypoints_close_sandbox_after_owned_success(self):
        for name in ("spawn", "antigravity_spawner"):
            with self.subTest(entrypoint=name):
                events = []
                sandbox = self.new_sandbox(events)
                proc = self.process()
                close = AsyncMock(side_effect=lambda _: events.append("owned group closed"))
                with patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as launch, \
                        patch.object(optimize_spawn, "_terminate_process_group", close):
                    self.assertEqual(await self.invoke(name), "done")
                self.assertEqual(events, ["owned group closed", "sandbox closed"])
                sandbox.close.assert_called_once_with()
                close.assert_awaited_once_with(self.receipt)
                proc.wait.assert_awaited()
                self.assertEqual(launch.call_args.args[0:2], (str(self.wrapper), "--print"))
                self.assertEqual(launch.call_args.kwargs["cwd"], str(self.source))
                self.assertEqual(launch.call_args.kwargs["env"]["UNITY_OPTIMIZE_WORKTREE"], str(self.source))
                self.assertFalse(launch.call_args.kwargs["start_new_session"])

    async def test_both_entrypoints_close_sandbox_after_ordinary_turn_failure(self):
        for name in ("spawn", "antigravity_spawner"):
            with self.subTest(entrypoint=name):
                events = []
                sandbox = self.new_sandbox(events)
                proc = self.process(status="FAILED")
                callback = AsyncMock()
                with patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
                        patch.object(optimize_spawn, "_terminate_process_group",
                                     new_callable=AsyncMock, side_effect=lambda _: events.append("owned group closed")):
                    with self.assertRaisesRegex(optimize_spawn._UnsuccessfulTurnError, "agy turn failed"):
                        await self.invoke(name, callback=callback)
                self.assertEqual(events, ["owned group closed", "sandbox closed"])
                sandbox.close.assert_called_once_with()
                callback.assert_not_awaited()

    async def test_both_entrypoints_retain_sandbox_on_owned_cleanup_failure(self):
        for name in ("spawn", "antigravity_spawner"):
            with self.subTest(entrypoint=name):
                sandbox = self.new_sandbox()
                proc = self.process()
                with patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
                        patch.object(optimize_spawn, "_terminate_process_group", new_callable=AsyncMock,
                                     side_effect=optimize_spawn.SpawnCleanupError("fixture closure failed")):
                    with self.assertRaisesRegex(optimize_spawn.SpawnCleanupError, "fixture closure failed"):
                        await self.invoke(name)
                sandbox.close.assert_not_called()
                proc.wait.assert_awaited()

    async def test_both_entrypoints_close_failed_preflight_without_starting_process(self):
        for name in ("spawn", "antigravity_spawner"):
            with self.subTest(entrypoint=name):
                sandbox = self.new_sandbox()
                sandbox.preflight.side_effect = RuntimeError("fixture preflight failed")
                with patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock) as launch:
                    with self.assertRaisesRegex(RuntimeError, "fixture preflight failed"):
                        await self.invoke(name)
                launch.assert_not_awaited()
                sandbox.close.assert_called_once_with()
        self.group.assert_not_called()

    async def test_shared_feedback_replaces_prompt_not_wrapper_arguments(self):
        events = []
        sandbox = self.new_sandbox(events)
        processes = [self.process(response="first"), self.process(response="second")]
        callback = AsyncMock(side_effect=["correct the score report", None])
        close = AsyncMock(side_effect=lambda _: events.append("owned group closed"))
        with patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock, side_effect=processes) as launch, \
                patch.object(optimize_spawn, "_terminate_process_group", close):
            self.assertEqual(await self.invoke("antigravity_spawner", callback=callback), "second")
        self.assertEqual(launch.await_count, 2)
        first, second = (call.args for call in launch.await_args_list)
        self.assertEqual(first[0:2], (str(self.wrapper), "--print"))
        self.assertEqual(second[0:2], first[0:2])
        self.assertEqual(second[3:], first[3:])
        self.assertEqual(second[2], first[2] + "\n\nCONTROLLER VALIDATION FEEDBACK:\ncorrect the score report")
        self.assertEqual(events, ["owned group closed", "owned group closed", "sandbox closed"])
        self.prepare.assert_called_once()
        sandbox.preflight.assert_called_once_with()
        sandbox.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
