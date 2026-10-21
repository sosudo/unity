"""Offline Claude socket-scratch regressions: real SDK options, no model calls.

Only the SDK query/client and process-close boundary are mocked. The socket
tests bind local AF_UNIX sockets using the paths actually passed to Claude.
"""

import asyncio
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import claude_agent_sdk

from unity import optimize_spawn


class ResultMessage:
    result = "fixture result"
    is_error = False
    total_cost_usd = 0.0
    num_turns = 1


class _Stream:
    def __init__(self, fixture, options, *, error=None, close_error=None):
        self.fixture = fixture
        self.options = options
        self.error = error
        self.close_error = close_error
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        if self.fixture.stream_gate is not None:
            await self.fixture.stream_gate.wait()
        if self.error is not None:
            raise self.error
        return ResultMessage()

    async def aclose(self):
        scratch = Path(self.options.env["TMPDIR"])
        self.fixture.assertTrue(scratch.is_dir())
        self.fixture.events.append(("sdk-close", scratch))
        if self.fixture.close_entered is not None:
            self.fixture.close_entered.set()
        if self.fixture.close_gate is not None:
            await self.fixture.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error


@unittest.skipUnless(os.name == "posix", "Claude's socket scratch is POSIX-only")
class OptimizeClaudeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="optimize-claude-test-")
        self.root = Path(self.temporary.name)
        self.cwd = self.root / ("nested-source-" * 8) / "worktree"
        self.cwd.mkdir(parents=True)
        self.shared = self.root / "shared-runtime"
        self.shared.mkdir()
        self.persistent_tmp = self.shared / ("persistent-runtime-" * 6)
        self.persistent_tmp.mkdir()
        self.planning_scratch = self.cwd / ".unity" / "scratch" / "optimize"
        self.planning_scratch.mkdir(parents=True)
        self.runtime = {
            **dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(self.persistent_tmp)),
            "UV_TOOL_DIR": str(self.shared / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(self.shared / "uv-bin"),
            "LAKE_CACHE_DIR": str(self.shared / "lake-cache"),
            "UNITY_OPTIMIZE_PROFILE": "optimizing",
            "UNITY_OPTIMIZE_SCRATCH": str(self.planning_scratch),
        }
        self.mcp = {"lean-lsp": {
            "command": "fixture-no-execute", "args": [],
            "env": {"TMPDIR": str(self.persistent_tmp), "FIXTURE": "unchanged"},
        }}
        self.agent = SimpleNamespace(name="Primary", model="fixture-haiku", budget=None,
                                     backend="claude_code", base_url=None, api_key=None,
                                     auth_token=None)
        self.options = []
        self.created = []
        self.events = []
        self.outcomes = [{}]
        self.stream_gate = None
        self.close_entered = None
        self.close_gate = None
        self.group_error = None
        self.group_entered = None
        self.group_gate = None
        self.tasks = []
        self.stack = ExitStack()
        original_mkdtemp = tempfile.mkdtemp

        def record_mkdtemp(*args, **kwargs):
            value = original_mkdtemp(*args, **kwargs)
            self.created.append(Path(value))
            return value

        self.stack.enter_context(patch.object(optimize_spawn.tempfile, "mkdtemp", side_effect=record_mkdtemp))
        self.stack.enter_context(patch.object(optimize_spawn, "_worktree_write_roots",
                                             return_value=(self.cwd, self.shared)))
        self.stack.enter_context(patch.object(optimize_spawn, "_stop_requested", return_value=False))
        self.stack.enter_context(patch.object(optimize_spawn, "_log"))
        self.stack.enter_context(patch.object(optimize_spawn, "_write_run_log"))
        self.stack.enter_context(patch.object(optimize_spawn, "_terminate_process_group", side_effect=self.close_group))
        self.stack.enter_context(patch.object(optimize_spawn, "_retry_sleep", return_value=0))
        self.stack.enter_context(patch.dict(os.environ, {"MAX_ATTEMPTS": "1"}))
        self.stack.enter_context(patch.object(claude_agent_sdk, "query", side_effect=self.query))

    def query(self, *, prompt, options):
        self.assertIsInstance(options, claude_agent_sdk.ClaudeAgentOptions)
        outcome = self.outcomes[min(len(self.options), len(self.outcomes) - 1)]
        self.options.append(options)
        return _Stream(self, options, **outcome)

    async def close_group(self, pid_file):
        if self.options:
            self.assertTrue(Path(self.options[-1].env["TMPDIR"]).is_dir(),
                            "scratch removed before owned process closure")
        self.events.append(("group-close", pid_file))
        if self.group_entered is not None:
            self.group_entered.set()
        if self.group_gate is not None:
            await self.group_gate.wait()
        if self.group_error is not None:
            raise self.group_error

    async def run_worker(self, **kwargs):
        return await optimize_spawn.claude_spawner(
            self.agent, "fixture system", "fixture task", self.cwd, self.mcp,
            env_overrides=self.runtime, **kwargs)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        for task in self.tasks:
            try:
                await task
            except BaseException:
                pass
        self.stack.close()
        # Every path here was freshly created by this fixture. Production
        # intentionally retains scratch when cleanup could not be certified.
        for path in self.created:
            if path.exists():
                shutil.rmtree(path)
        self.temporary.cleanup()

    async def test_long_worker_paths_use_short_private_bindable_socket_paths(self):
        original_query = self.query

        def bind_query(*, prompt, options):
            stream = original_query(prompt=prompt, options=options)
            scratch = Path(options.env["TMPDIR"])
            self.assertEqual(scratch.parent, Path("/tmp"))
            self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o700)
            for bridge in ("http", "socks"):
                name = f"claude-{bridge}-{'a' * 16}.sock"
                self.assertGreaterEqual(len(os.fsencode(self.persistent_tmp / name)), 108)
                for root in (scratch, scratch.resolve()):
                    path = root / name
                    self.assertLess(len(os.fsencode(path)), 104)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                        listener.bind(str(path))
                    path.unlink()
            return stream

        with patch.object(claude_agent_sdk, "query", side_effect=bind_query):
            self.assertEqual(await self.run_worker(), "fixture result")
        self.assertFalse(Path(self.options[0].env["TMPDIR"]).exists())
        self.assertEqual([event[0] for event in self.events], ["sdk-close", "group-close"])

    async def test_child_only_override_keeps_caches_mcp_inputs_and_global_environment(self):
        before_env = dict(os.environ)
        before_runtime, before_mcp = copy.deepcopy((self.runtime, self.mcp))
        await self.run_worker()
        options = self.options[0]
        for key in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(options.env[key], options.env["TMPDIR"])
            self.assertNotEqual(options.env[key], self.runtime[key])
        for key in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "LAKE_CACHE_DIR", "UNITY_OPTIMIZE_SCRATCH"):
            self.assertEqual(options.env[key], self.runtime[key])
        self.assertEqual(options.mcp_servers, before_mcp)
        self.assertEqual(self.runtime, before_runtime)
        self.assertEqual(self.mcp, before_mcp)
        self.assertEqual(dict(os.environ), before_env)

    async def test_only_exact_scratch_is_added_to_unchanged_sandbox_policy(self):
        await self.run_worker()
        options = self.options[0]
        sandbox = json.loads(options.settings)["sandbox"]
        self.assertEqual(sandbox["filesystem"]["allowWrite"], [
            str(self.cwd.resolve()), str(self.shared.resolve()),
            str(Path(options.env["TMPDIR"]).resolve()),
        ])
        self.assertNotIn("/tmp", sandbox["filesystem"]["allowWrite"])
        self.assertNotIn(str(Path("/tmp").resolve()), sandbox["filesystem"]["allowWrite"])
        self.assertEqual(options.permission_mode, "dontAsk")
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        self.assertEqual(sandbox["excludedCommands"], [])
        self.assertEqual(options.extra_args, {"setting-sources": ""})
        hook = options.hooks["PreToolUse"][0].hooks[0]
        answer = await hook({"tool_name": "Bash", "tool_input": {
            "dangerouslyDisableSandbox": True}}, None, None)
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_concurrent_workers_get_distinct_scratch(self):
        self.stream_gate = asyncio.Event()
        self.tasks = [asyncio.create_task(self.run_worker()) for _ in range(2)]
        async with asyncio.timeout(2):
            while len(self.options) < 2:
                await asyncio.sleep(0)
        paths = [Path(options.env["TMPDIR"]) for options in self.options]
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(path.is_dir() for path in paths))
        # Each invocation may finish independently; stop checking the other
        # invocation's already-cleaned scratch at this mocked group boundary.
        with patch.object(optimize_spawn, "_terminate_process_group", return_value=None):
            self.stream_gate.set()
            self.assertEqual(await asyncio.gather(*self.tasks), ["fixture result"] * 2)
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_same_scratch_survives_retries_until_all_cleanup_finishes(self):
        self.outcomes = [{"error": RuntimeError("fixture transient")}, {}]
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "2"}):
            await self.run_worker()
        self.assertEqual(len(self.options), 2)
        self.assertEqual(self.options[0].env["TMPDIR"], self.options[1].env["TMPDIR"])
        self.assertEqual(len([path for path in self.created if path.name.startswith("uo-")]), 1)
        self.assertFalse(Path(self.options[0].env["TMPDIR"]).exists())

    async def test_sdk_close_failure_retains_scratch(self):
        self.outcomes = [{"close_error": RuntimeError("fixture close failed")}]
        with self.assertRaisesRegex(RuntimeError, "fixture close failed"):
            await self.run_worker()
        self.assertTrue(Path(self.options[0].env["TMPDIR"]).is_dir())

    async def test_sdk_close_failure_retention_is_sticky_across_existing_retries(self):
        self.outcomes = [{"close_error": RuntimeError("fixture close failed")}, {}]
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "2"}):
            self.assertEqual(await self.run_worker(), "fixture result")
        self.assertEqual(len(self.options), 2)
        self.assertTrue(Path(self.options[0].env["TMPDIR"]).is_dir())

    async def test_owned_group_cleanup_failure_retains_scratch_and_exact_exception(self):
        self.group_error = optimize_spawn.SpawnCleanupError("fixture unclosed group")
        with self.assertRaises(optimize_spawn.SpawnCleanupError) as error:
            await self.run_worker()
        self.assertIs(error.exception, self.group_error)
        self.assertTrue(Path(self.options[0].env["TMPDIR"]).is_dir())

    async def test_owned_group_wrapper_is_closed_before_scratch_is_removed(self):
        def wrapper(real_cli, directory, label):
            self.assertEqual(label, "claude")
            return str(directory / "fixture-cli"), directory / "fixture-pgid"

        with patch.object(optimize_spawn, "_process_group_wrapper", side_effect=wrapper) as make_wrapper:
            await self.run_worker(own_process_group=True)
        make_wrapper.assert_called_once()
        self.assertEqual([event[0] for event in self.events], ["sdk-close", "group-close"])
        self.assertEqual(self.events[-1][1].name, "fixture-pgid")
        self.assertFalse(Path(self.options[0].env["TMPDIR"]).exists())

    async def test_cancelled_stream_removes_scratch_after_successful_cleanup(self):
        self.stream_gate = asyncio.Event()
        task = asyncio.create_task(self.run_worker())
        self.tasks.append(task)
        async with asyncio.timeout(2):
            while not self.options:
                await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([event[0] for event in self.events], ["sdk-close", "group-close"])
        self.assertFalse(Path(self.options[0].env["TMPDIR"]).exists())

    async def test_interrupted_sdk_cleanup_retains_scratch(self):
        self.close_entered, self.close_gate = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.run_worker())
        self.tasks.append(task)
        await asyncio.wait_for(self.close_entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(Path(self.options[0].env["TMPDIR"]).is_dir())

    async def test_interrupted_group_cleanup_retains_scratch(self):
        self.group_entered, self.group_gate = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.run_worker())
        self.tasks.append(task)
        await asyncio.wait_for(self.group_entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(Path(self.options[0].env["TMPDIR"]).is_dir())

    async def test_repair_and_review_keep_configured_mcp_runtime_overrides(self):
        for phase in ("optimizing", "critic"):
            with self.subTest(phase=phase):
                runtime = {**self.runtime, "UNITY_OPTIMIZE_PROFILE": phase}
                if phase == "critic":
                    runtime["UNITY_OPTIMIZE_SCRATCH"] = ""
                await optimize_spawn.spawn(self.agent, "system", "task", self.cwd,
                                       self.mcp, env_overrides=runtime)
                options = self.options[-1]
                self.assertEqual(options.env["UNITY_OPTIMIZE_PROFILE"], phase)
                mcp_env = options.mcp_servers["lean-lsp"]["env"]
                self.assertEqual(mcp_env["TMPDIR"], self.runtime["TMPDIR"])
                self.assertEqual(mcp_env["UV_TOOL_DIR"], self.runtime["UV_TOOL_DIR"])
                self.assertNotEqual(mcp_env["TMPDIR"], options.env["TMPDIR"])
                for env in (options.env, mcp_env):
                    self.assertEqual(env["UNITY_OPTIMIZE_SCRATCH"], runtime["UNITY_OPTIMIZE_SCRATCH"])
                if phase == "critic":
                    self.assertIn("Review is read-only", options.system_prompt)
                    self.assertNotIn('--args-file "$UNITY_OPTIMIZE_SCRATCH', options.system_prompt)
                    sandbox = json.loads(options.settings)["sandbox"]
                    self.assertIn(str(self.cwd.resolve()), sandbox["filesystem"]["denyWrite"])
                    hook = options.hooks["PreToolUse"][0].hooks[0]
                    answer = await hook({"tool_name": "Write", "tool_input": {
                        "file_path": str(self.planning_scratch / "plan.json"), "content": "{}"}}, None, None)
                    self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")
                else:
                    self.assertNotEqual(options.env["TMPDIR"], str(self.planning_scratch))
                    self.assertIn('--args-file "$UNITY_OPTIMIZE_SCRATCH', options.system_prompt)
        self.assertTrue(self.planning_scratch.is_dir())

    async def test_non_sandboxed_claude_keeps_existing_environment(self):
        with patch.object(optimize_spawn, "_worktree_write_roots", return_value=()):
            await self.run_worker()
        self.assertEqual(self.options[0].env["TMPDIR"], self.runtime["TMPDIR"])
        self.assertEqual(self.options[0].permission_mode, "bypassPermissions")
        self.assertEqual(self.created, [])

    async def test_score_sdk_starts_in_runner_and_preserves_source_and_mcp_bindings(self):
        self.runtime.update(UNITY_OPTIMIZE_SCORE_REQUEST_ID="score-1",
                            UNITY_OPTIMIZE_WORKTREE=str(self.cwd.resolve()),
                            UNITY_OPTIMIZE_SCRATCH="")
        original_query = self.query

        def check_query(*, prompt, options):
            runner = Path(options.cwd)
            self.assertEqual(runner, Path(options.env["TMPDIR"]).resolve() / "runner")
            self.assertTrue(runner.is_dir())
            self.assertFalse(runner.is_relative_to(self.cwd.resolve()))
            self.assertEqual(options.env["UNITY_OPTIMIZE_WORKTREE"], str(self.cwd.resolve()))
            self.assertEqual(options.env["PWD"], str(runner))
            self.assertEqual(options.add_dirs, [])
            self.assertIn(str(self.cwd.resolve()), options.mcp_servers["lean-lsp"]["args"])
            return original_query(prompt=prompt, options=options)

        with patch.object(claude_agent_sdk, "query", side_effect=check_query):
            self.assertEqual(await self.run_worker(), "fixture result")
        self.assertFalse(Path(self.options[0].cwd).exists())
        self.assertFalse((self.cwd / ".gitconfig").exists())
        self.assertEqual([event[0] for event in self.events], ["sdk-close", "group-close"])

    async def test_score_cancellation_closes_owned_processes_before_removing_runner(self):
        self.runtime["UNITY_OPTIMIZE_SCORE_REQUEST_ID"] = "score-1"
        self.stream_gate = asyncio.Event()
        task = asyncio.create_task(self.run_worker())
        self.tasks.append(task)
        async with asyncio.timeout(2):
            while not self.options:
                await asyncio.sleep(0)
        runner = Path(self.options[0].cwd)
        self.assertTrue(runner.is_dir())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(runner.exists())
        self.assertEqual([event[0] for event in self.events], ["sdk-close", "group-close"])

    async def test_score_cleanup_failure_retains_runner_for_owned_processes(self):
        self.runtime["UNITY_OPTIMIZE_SCORE_REQUEST_ID"] = "score-1"
        self.group_error = optimize_spawn.SpawnCleanupError("fixture unclosed group")
        with self.assertRaises(optimize_spawn.SpawnCleanupError):
            await self.run_worker()
        self.assertTrue(Path(self.options[0].cwd).is_dir())

    async def test_score_sdk_options_failure_removes_unstarted_runner(self):
        self.runtime["UNITY_OPTIMIZE_SCORE_REQUEST_ID"] = "score-1"
        with patch.object(claude_agent_sdk, "ClaudeAgentOptions", side_effect=ValueError("fixture options")):
            with self.assertRaisesRegex(ValueError, "fixture options"):
                await self.run_worker()
        self.assertEqual(self.options, [])
        self.assertTrue(all(not path.exists() for path in self.created))

    async def test_unsafe_encoded_socket_path_is_rejected_before_query(self):
        short_root = Path(tempfile.mkdtemp(prefix="ut-", dir="/tmp"))
        long_path = short_root / ("é" * 32)
        long_path.mkdir()
        socket_path = long_path / ("claude-socks-" + "a" * 16 + ".sock")
        self.assertLess(len(str(socket_path)), 104)
        self.assertGreaterEqual(len(os.fsencode(socket_path)), 104)
        with patch.object(optimize_spawn.tempfile, "mkdtemp", return_value=str(long_path)):
            with self.assertRaisesRegex(ValueError, "socket path"):
                await self.run_worker()
        self.assertEqual(self.options, [])
        self.assertFalse(long_path.exists())

    async def test_options_construction_failure_removes_unused_scratch(self):
        with patch.object(claude_agent_sdk, "ClaudeAgentOptions", side_effect=ValueError("fixture options")):
            with self.assertRaisesRegex(ValueError, "fixture options"):
                await self.run_worker()
        self.assertEqual(self.options, [])
        self.assertTrue(all(not path.exists() for path in self.created))

    async def test_continuation_keeps_scratch_until_disconnect_and_group_close(self):
        fixture = self

        class Client:
            def __init__(self, *, options):
                self.options = options
                fixture.options.append(options)

            async def connect(self):
                fixture.events.append(("connect", None))

            async def query(self, prompt):
                fixture.events.append(("query", prompt))

            def receive_response(self):
                return _Stream(fixture, self.options)

            async def disconnect(self):
                fixture.assertTrue(Path(self.options.env["TMPDIR"]).is_dir())
                fixture.events.append(("disconnect", None))

        turns = 0

        async def feedback(final):
            nonlocal turns
            turns += 1
            return "fixture correction" if turns == 1 else None

        with patch.object(claude_agent_sdk, "ClaudeSDKClient", Client):
            self.assertEqual(await self.run_worker(on_normal_completion=feedback), "fixture result")
        self.assertEqual(turns, 2)
        self.assertEqual([event[0] for event in self.events], [
            "connect", "query", "sdk-close", "query", "sdk-close", "disconnect", "group-close"])
        self.assertFalse(Path(self.options[0].env["TMPDIR"]).exists())

    async def test_continuation_disconnect_failure_retains_scratch(self):
        fixture = self

        class Client:
            def __init__(self, *, options):
                self.options = options
                fixture.options.append(options)

            async def connect(self):
                pass

            async def query(self, prompt):
                pass

            def receive_response(self):
                return _Stream(fixture, self.options)

            async def disconnect(self):
                raise RuntimeError("fixture disconnect failed")

        async def feedback(final):
            return None

        with patch.object(claude_agent_sdk, "ClaudeSDKClient", Client):
            with self.assertRaisesRegex(RuntimeError, "fixture disconnect failed"):
                await self.run_worker(on_normal_completion=feedback)
        self.assertTrue(Path(self.options[0].env["TMPDIR"]).is_dir())

    async def test_continuation_response_close_failure_retains_scratch(self):
        fixture = self

        class Client:
            def __init__(self, *, options):
                self.options = options
                fixture.options.append(options)

            async def connect(self):
                pass

            async def query(self, prompt):
                pass

            def receive_response(self):
                return _Stream(fixture, self.options, close_error=RuntimeError("fixture response close failed"))

            async def disconnect(self):
                fixture.events.append(("disconnect", None))

        async def feedback(final):
            return None

        with patch.object(claude_agent_sdk, "ClaudeSDKClient", Client):
            with self.assertRaisesRegex(RuntimeError, "fixture response close failed"):
                await self.run_worker(on_normal_completion=feedback)
        self.assertEqual([event[0] for event in self.events], ["sdk-close", "disconnect", "group-close"])
        self.assertTrue(Path(self.options[0].env["TMPDIR"]).is_dir())


if __name__ == "__main__":
    unittest.main()
