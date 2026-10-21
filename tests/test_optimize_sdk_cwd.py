"""Offline contracts for Optimize's opt-in Claude startup directory.

Use real SDK options, but never start Claude or contact a provider.
"""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import claude_agent_sdk

from unity import spawn
from unity.execution_policy import ExecutionPolicy


class ResultMessage:
    result = "fixture result"
    is_error = False
    total_cost_usd = 0.0
    num_turns = 1


class CallbackFailure(Exception):
    pass


class TurnFailure(Exception):
    pass


class Stream:
    def __init__(self):
        self.sent = False
        self.aclose = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return ResultMessage()


class ClaudeStartupDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = Path("/fixture/source")
        self.runner = Path("/fixture/runtime/runner")
        self.scratch = Path("/fixture/runtime")
        self.agent = SimpleNamespace(name="fixture", model="fixture-model", budget=None)
        self.servers = {"fixture": {"command": "never-run"}}
        self.environment = {"FIXTURE": "unchanged"}

    def policy(self, *, sdk_cwd=None, callback=None):
        hooks = SimpleNamespace(
            _agent_env=Mock(return_value=self.environment),
            _stop_requested=Mock(return_value=False),
            _log=Mock(),
            _idle_guard=lambda stream, timeout: stream,
            _terminate_process_group=AsyncMock(),
            _last_run_stats={},
            _give_up=Mock(return_value=True),
        )

        async def completion_feedback(function, final, cwd):
            return await function(final)

        hooks._completion_feedback = AsyncMock(side_effect=completion_feedback)
        prepared = {
            "system_prompt": "fixture system", "mcp_servers": self.servers,
            "env_overrides": self.environment, "options": {}, "scratch": self.scratch,
        }
        if sdk_cwd is not None:
            prepared["sdk_cwd"] = sdk_cwd
        return ExecutionPolicy(
            hooks=hooks, claude_options=Mock(return_value=prepared), finish_claude=Mock(),
            codex_options=Mock(), antigravity_options=Mock(),
            callback_error=CallbackFailure, unsuccessful_error=TurnFailure,
            cleanup_error=RuntimeError, on_normal_completion=callback,
        )

    async def invoke(self, policy=None):
        return await spawn.claude_spawner(
            self.agent, "fixture system", "fixture task", self.source, self.servers,
            env_overrides=self.environment, execution_policy=policy,
        )

    def assert_logical_paths(self, policy):
        self.assertTrue(policy.hooks._stop_requested.called)
        self.assertTrue(all(call.args == (self.source,)
                            for call in policy.hooks._stop_requested.call_args_list))
        self.assertTrue(policy.hooks._log.called)
        self.assertTrue(all(call.args[2] == self.source
                            for call in policy.hooks._log.call_args_list))

    async def test_without_policy_sdk_and_lifecycle_keep_original_cwd(self):
        stream = Stream()
        with patch.object(claude_agent_sdk, "query", return_value=stream) as query, \
                patch.object(spawn, "_agent_env", return_value=self.environment), \
                patch.object(spawn, "_stop_requested", return_value=False) as stop, \
                patch.object(spawn, "_log") as log, \
                patch.object(spawn, "_terminate_process_group", new_callable=AsyncMock):
            self.assertEqual(await self.invoke(), "fixture result")
        options = query.call_args.kwargs["options"]
        self.assertEqual(options.cwd, str(self.source))
        self.assertEqual(options.mcp_servers, self.servers)
        self.assertEqual(options.env, self.environment)
        self.assertEqual(options.permission_mode, "bypassPermissions")
        self.assertIsNone(options.settings)
        self.assertIsNone(options.hooks)
        stop.assert_called_once_with(self.source)
        self.assertEqual(log.call_args.args[2], self.source)
        stream.aclose.assert_awaited_once()

    async def test_existing_policy_without_sdk_cwd_keeps_original_cwd(self):
        policy = self.policy()
        with patch.object(claude_agent_sdk, "query", return_value=Stream()) as query:
            self.assertEqual(await self.invoke(policy), "fixture result")
        self.assertEqual(query.call_args.kwargs["options"].cwd, str(self.source))
        self.assert_logical_paths(policy)
        policy.finish_claude.assert_called_once_with(self.scratch, True, False)

    async def test_sdk_cwd_changes_only_transport_not_lifecycle(self):
        policy = self.policy(sdk_cwd=self.runner)
        with patch.object(claude_agent_sdk, "query", return_value=Stream()) as query:
            self.assertEqual(await self.invoke(policy), "fixture result")
        options = query.call_args.kwargs["options"]
        self.assertEqual(options.cwd, str(self.runner))
        self.assertEqual(options.system_prompt, "fixture system")
        self.assertEqual(options.mcp_servers, self.servers)
        self.assertEqual(options.env, self.environment)
        self.assertEqual(options.add_dirs, [])
        policy.claude_options.assert_called_once_with(
            self.source, "fixture system", self.servers, "bypassPermissions", self.environment)
        self.assert_logical_paths(policy)
        policy.finish_claude.assert_called_once_with(self.scratch, True, False)
        policy.codex_options.assert_not_called()
        policy.antigravity_options.assert_not_called()

    async def test_continuation_corrections_keep_source_callback_and_stop_bindings(self):
        callback = AsyncMock(side_effect=["fixture correction", None])
        policy = self.policy(sdk_cwd=self.runner, callback=callback)
        streams = [Stream(), Stream()]
        client = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(), query=AsyncMock(),
            receive_response=Mock(side_effect=streams),
        )
        with patch.object(claude_agent_sdk, "ClaudeSDKClient", return_value=client) as sdk, \
                patch.object(claude_agent_sdk, "query") as direct_query:
            self.assertEqual(await self.invoke(policy), "fixture result")
        self.assertEqual(sdk.call_args.kwargs["options"].cwd, str(self.runner))
        direct_query.assert_not_called()
        self.assertEqual([call.args for call in client.query.await_args_list],
                         [("fixture task",), ("fixture correction",)])
        self.assertEqual(callback.await_count, 2)
        self.assertTrue(all(call.args == (callback, "fixture result", self.source)
                            for call in policy.hooks._completion_feedback.await_args_list))
        self.assert_logical_paths(policy)
        for stream in streams:
            stream.aclose.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        policy.finish_claude.assert_called_once_with(self.scratch, True, False)

    async def test_stop_marker_is_checked_at_source_not_runner(self):
        policy = self.policy(sdk_cwd=self.runner)
        policy.hooks._stop_requested.side_effect = lambda cwd: cwd == self.source
        stream = Stream()
        with patch.object(claude_agent_sdk, "query", return_value=stream) as query:
            self.assertEqual(await self.invoke(policy), "fixture result")
        self.assertEqual(query.call_args.kwargs["options"].cwd, str(self.runner))
        policy.hooks._stop_requested.assert_called_once_with(self.source)
        stream.aclose.assert_awaited_once()
        policy.finish_claude.assert_called_once_with(self.scratch, True, False)


if __name__ == "__main__":
    unittest.main()
