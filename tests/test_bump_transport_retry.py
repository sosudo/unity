"""Canned Bump transport recovery; no real models, services, or network."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from unity import bump_orchestrator, bump_spawn, bump_state
from unity.bump_provider import (
    BumpProviderFailure, BumpTransportRetriesExhausted,
    is_transient_provider_failure, provider_failure,
)
from unity.commands import bump as command
from unity.roster import Agent


def agent(name="Fixture"):
    return Agent(name=name, model="fixture-model", provider="openai", backend="codex",
                 strength=1., base_url="https://freeinference.org/v1", api_key="never-real",
                 auth_token=None, budget=None, is_primary=name == "Fixture")


def note(method, **payload):
    return NS(method=method, payload=NS(**payload))


def complete():
    return [note("item/completed", item=NS(root=NS(type="agentMessage", text="done"))),
            note("turn/completed", turn=NS(status="completed", error=None))]


def rate_limit():
    return note("error", error=NS(httpStatusCode=429, message="private-provider-fixture"), will_retry=False)


def exhausted():
    return BumpTransportRetriesExhausted(
        BumpProviderFailure("rate_limited", provider="freeinference", status=429), 2)


class TransportClassificationTests(unittest.TestCase):
    def test_known_statuses_and_sanitized_codes_remain_retryable(self):
        for evidence in ({"httpStatusCode": 429}, {"httpStatusCode": 503}, {"httpStatusCode": 408},
                         {"code": "rate_limited"}, {"code": "provider_unavailable"},
                         {"codexErrorInfo": {"responseStreamDisconnected": {}}},
                         TimeoutError("private"), ConnectionError("private"),
                         httpx.ReadTimeout("private"), httpx.ConnectError("private")):
            with self.subTest(evidence=type(evidence).__name__):
                failure = provider_failure(agent(), evidence)
                self.assertTrue(is_transient_provider_failure(failure), failure)
                self.assertNotIn("private", str(failure))

    def test_auth_native_policy_protocol_unknown_and_nonretryable_5xx_stay_fatal(self):
        for evidence in ({"httpStatusCode": 401}, {"httpStatusCode": 403},
                         {"httpStatusCode": 501}, {"httpStatusCode": 505},
                         {"code": "adapter_transport_failure"},
                         {"code": "unknown_output_tool"}, ValueError("429 private"),
                         BumpProviderFailure("rate_limited", provider="native_mcp", status=429),
                         BumpProviderFailure("transport_timeout", provider="freeinference", status=401),
                         BumpProviderFailure("native_mcp_phase_catalog_mismatch", provider="native_mcp")):
            with self.subTest(evidence=type(evidence).__name__):
                self.assertFalse(is_transient_provider_failure(provider_failure(agent(), evidence)))

    def test_exhaustion_is_safe_and_cannot_wrap_permanent_failure(self):
        failure = exhausted()
        self.assertEqual((failure.category, failure.provider, failure.http_status, failure.attempts),
                         ("rate_limited", "freeinference", 429, 2))
        self.assertIs(provider_failure(agent(), failure), failure)
        self.assertNotIn("never-real", str(failure))
        with self.assertRaises(ValueError):
            BumpTransportRetriesExhausted(BumpProviderFailure("unknown", provider="native_mcp"), 1)

    def test_conflicting_http_statuses_cannot_authorize_retry(self):
        failure = provider_failure(agent(), {
            "httpStatusCode": 429, "code": "rate_limited",
            "error": {"httpStatusCode": 401},
        })
        self.assertEqual(failure.category, "ambiguous_http_status")
        self.assertIsNone(failure.http_status)
        self.assertFalse(is_transient_provider_failure(failure))

    def test_existing_cap_and_backoff_are_separate_from_proof_state(self):
        failure = provider_failure(agent(), {"httpStatusCode": 429})
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "3"}):
            self.assertFalse(bump_spawn._give_up(failure, 2))
            self.assertTrue(bump_spawn._give_up(failure, 3))
        with patch.dict(os.environ, {"MAX_ATTEMPTS": ""}):
            self.assertFalse(bump_spawn._give_up(failure, 100))
        self.assertEqual(bump_spawn._retry_sleep(failure), 60.)
        self.assertEqual(bump_spawn._retry_sleep(provider_failure(agent(), {"httpStatusCode": 503})), 600.)


class TransportSpawnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_client_construction_fault_never_starts_or_retries_model_session(self):
        fault = AttributeError("fixture HTTP client compatibility failure")
        with patch.object(httpx, "Timeout", side_effect=fault), \
                patch.object(bump_spawn, "_codex_spawner_impl", new_callable=AsyncMock) as start, \
                patch.object(bump_spawn, "_retry_pause", new_callable=AsyncMock) as pause:
            with self.assertRaises(AttributeError) as caught:
                await bump_spawn.codex_spawner(agent(), "", "", Path("/fixture"), {})
        self.assertIs(caught.exception, fault)
        start.assert_not_awaited()
        pause.assert_not_awaited()

    async def run_scenarios(self, scenarios, *, callback=None, start_error=None,
                            readiness_error=None, pause_result=True, max_attempts="3",
                            close_error=None):
        self.trace, self.clients = [], []
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            root = Path(directory)

            def make_client(**kwargs):
                index = len(self.clients)
                self.trace.append(("start", index))
                client = NS(thread_start=AsyncMock(), login_api_key=AsyncMock())
                thread = NS(id=f"thread-{index}", turn=AsyncMock(return_value=NS(index=index)))
                client.thread_start.return_value = thread
                client.thread_start.side_effect = start_error

                async def close():
                    self.trace.append(("close", index))
                    if close_error is not None:
                        raise close_error

                client.close = AsyncMock(side_effect=close)
                self.clients.append(client)
                return client

            sdk = NS(AsyncCodex=Mock(side_effect=make_client), CodexConfig=lambda **kw: NS(**kw),
                     Sandbox=NS(read_only="read-only", workspace_write="workspace-write"),
                     ApprovalMode=NS(deny_all="never"))

            async def stream(handle, *_):
                for item in scenarios[min(handle.index, len(scenarios) - 1)]:
                    yield item

            async def pause(delay, cwd, interrupt_event=None):
                self.trace.append(("pause", delay))
                self.assertEqual(self.trace[-2], ("close", len(self.clients) - 1))
                return pause_result

            config = bump_orchestrator.build_bump_mcp(NS(project_root=root, forum=root / ".unity/forum"), "critic")
            patches.enter_context(patch.dict(sys.modules, {"openai_codex": sdk}))
            patches.enter_context(patch.dict(os.environ, {"MAX_ATTEMPTS": max_attempts}))
            patches.enter_context(patch.object(bump_spawn, "_worktree_write_roots", return_value=(root,)))
            patches.enter_context(patch.object(bump_spawn.tempfile, "mkdtemp", return_value=str(root / "home")))
            patches.enter_context(patch.object(bump_spawn, "_stop_requested", return_value=False))
            patches.enter_context(patch.object(bump_spawn, "_codex_notifications", side_effect=stream))
            patches.enter_context(patch.object(bump_spawn, "_retry_pause", side_effect=pause))
            self.group_cleanup = patches.enter_context(patch.object(
                bump_spawn, "_terminate_process_group", new_callable=AsyncMock))
            patches.enter_context(patch("unity.bump_mcp_ready.wait_for_native_tools", new_callable=AsyncMock,
                                        return_value=True, side_effect=readiness_error))
            patches.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("no network")))
            patches.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("no DNS")))
            self.console = patches.enter_context(patch.object(bump_spawn, "_console"))
            return await bump_spawn._codex_spawner_impl(
                agent(), "fixture", "fixture", root, config,
                env_overrides={"UNITY_BUMP_PROFILE": "critic"}, on_normal_completion=callback)

    async def test_429_retries_after_cleanup_then_completes_one_spawn(self):
        self.assertEqual(await self.run_scenarios([[rate_limit()], complete()]), "done")
        self.assertEqual(self.trace, [("start", 0), ("close", 0), ("pause", 60.), ("start", 1), ("close", 1)])
        self.assertNotIn("private-provider-fixture", str(self.console.mock_calls))

    async def test_terminal_failed_turn_with_rate_status_uses_same_recovery(self):
        failed = note("turn/completed", turn=NS(status="failed", error=NS(httpStatusCode=429)))
        self.assertEqual(await self.run_scenarios([[failed], complete()]), "done")
        self.assertEqual(len(self.clients), 2)

    async def test_retryable_sdk_start_failure_also_honors_cap(self):
        with self.assertRaises(BumpTransportRetriesExhausted) as caught:
            await self.run_scenarios([[]], start_error=httpx.ConnectError("private"), max_attempts="2")
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual([row for row in self.trace if row[0] == "pause"], [("pause", 600.)])

    async def test_repeated_429_exhaustion_is_typed_not_successful(self):
        with self.assertRaises(BumpTransportRetriesExhausted) as caught:
            await self.run_scenarios([[rate_limit()]], max_attempts="2")
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.trace[-1], ("close", 1))

    async def test_uncertain_close_prevents_retry_and_exhaustion_rotation(self):
        for cap in ("1", "3"):
            for fault in (RuntimeError("private close failure"), TimeoutError("private close timeout")):
                with self.subTest(cap=cap, kind=type(fault).__name__), \
                        self.assertRaises(BumpProviderFailure) as caught:
                    await self.run_scenarios([[rate_limit()], complete()],
                                             close_error=fault, max_attempts=cap)
                self.assertEqual(caught.exception.category, "backend_cleanup_failed")
                self.assertNotIsInstance(caught.exception, BumpTransportRetriesExhausted)
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(self.trace, [("start", 0), ("close", 0)])
                self.group_cleanup.assert_awaited_once()

    async def test_close_cancellation_propagates_after_owned_group_cleanup(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_scenarios([[rate_limit()]], close_error=asyncio.CancelledError())
        self.assertEqual(self.trace, [("start", 0), ("close", 0)])
        self.group_cleanup.assert_awaited_once()

    async def test_codex_owned_retry_notification_does_not_start_second_session(self):
        retrying = note("error", error=NS(httpStatusCode=429), will_retry=True)
        self.assertEqual(await self.run_scenarios([[retrying] + complete()]), "done")
        self.assertEqual(len(self.clients), 1)

    async def test_native_readiness_failure_remains_fatal_without_retry(self):
        fault = BumpProviderFailure("native_mcp_required_tool_missing", provider="native_mcp")
        with self.assertRaises(BumpProviderFailure) as caught:
            await self.run_scenarios([complete()], readiness_error=fault)
        self.assertIs(caught.exception, fault)
        self.assertEqual(self.trace, [("start", 0), ("close", 0)])

    async def test_unknown_and_auth_failures_do_not_retry(self):
        for fault in (ValueError("private controller failure"),
                      BumpProviderFailure("authentication_or_access_denied", provider="freeinference", status=401)):
            with self.subTest(kind=type(fault).__name__), self.assertRaises(BumpProviderFailure):
                await self.run_scenarios([[]], start_error=fault)
            self.assertEqual(self.trace, [("start", 0), ("close", 0)])

    async def test_callback_controller_failure_is_not_reclassified_or_retried(self):
        fault = TimeoutError("controller-specific failure")
        with self.assertRaises(TimeoutError) as caught:
            await self.run_scenarios([complete()], callback=AsyncMock(side_effect=fault))
        self.assertIs(caught.exception, fault)
        self.assertEqual(self.trace, [("start", 0), ("close", 0)])

    async def test_callback_free_failed_turn_remains_failure(self):
        with self.assertRaises(BumpProviderFailure):
            await self.run_scenarios([[note("turn/completed", turn=NS(status="failed", error=None))]])
        self.assertEqual(len(self.clients), 1)

    async def test_stop_during_backoff_does_not_start_another_session(self):
        self.assertIsNone(await self.run_scenarios([[rate_limit()]], pause_result=False))
        self.assertEqual(len(self.clients), 1)


class RetryPauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_event_wakes_backoff(self):
        interrupt = asyncio.Event()
        with patch.object(bump_spawn, "_stop_requested", return_value=False):
            task = asyncio.create_task(bump_spawn._retry_pause(600., Path("/fixture"), interrupt))
            await asyncio.sleep(0)
            interrupt.set()
            self.assertFalse(await asyncio.wait_for(task, 1.))

    async def test_existing_stop_marker_skips_pause(self):
        with patch.object(bump_spawn, "_stop_requested", return_value=True):
            self.assertFalse(await bump_spawn._retry_pause(600., Path("/fixture")))

    async def test_cancellation_is_not_swallowed(self):
        with patch.object(bump_spawn, "_stop_requested", return_value=False):
            task = asyncio.create_task(bump_spawn._retry_pause(600., Path("/fixture")))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class CriticTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase_dispatch_preserves_typed_exhaustion(self):
        critic = agent()
        roster = NS(agents=[critic], primary=critic)
        fault = exhausted()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            root = Path(directory)
            config = bump_orchestrator.build_bump_mcp(NS(project_root=root, forum=root / ".unity/forum"), "critic")
            patches.enter_context(patch.object(bump_orchestrator, "spawn", new_callable=AsyncMock, side_effect=fault))
            patches.enter_context(patch.object(bump_orchestrator.library, "library_context", return_value=""))
            patches.enter_context(patch.object(bump_orchestrator.library, "library_subagents", return_value=()))
            patches.enter_context(patch.object(bump_orchestrator, "load_prompt", return_value="fixture"))
            with self.assertRaises(BumpTransportRetriesExhausted) as caught:
                await bump_orchestrator.dispatch([critic], roster, "", "", root, config,
                                                brief_provider=lambda _: "", log_context={"phase": "critic"})
            self.assertIs(caught.exception, fault)

    async def run_critics(self, outcomes):
        roster = NS(agents=[agent(), agent("Peer")], primary=agent())
        paths = NS(project_root=Path("/fixture"), forum=Path("/fixture/.unity/forum"))
        self.state = {"phase": "critic", "formalization": {"spec": {"arguments": []},
                      "review_snapshot": {"snapshot_id": "snapshot-current"}},
                      "source_repairs": {}, "migration_critic_attempts": [], "critic_verdicts": []}
        self.seen = []

        def begin(paths, binding, name):
            self.state["migration_critic_attempts"].append({"snapshot_id": binding, "author": name.lower()})
            return command._critic_attempt_count(self.state, binding, name)

        async def run(*_, critic, attempt):
            self.seen.append((critic.name, attempt))
            outcome = outcomes[critic.name]
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "verdict":
                self.state["critic_verdicts"].append({"author": critic.name})
                self.state["phase"] = "complete"

        with patch.object(command.bump_state, "load_state", side_effect=lambda _: deepcopy(self.state)), \
                patch.object(command.bump_state, "pending_replan", return_value=False), \
                patch.object(command.bump_state, "open_source_issues", return_value=[]), \
                patch.object(command, "_prepare_critic_snapshot"), \
                patch.object(command, "_begin_critic_attempt", side_effect=begin), \
                patch.object(command, "stop_requested", return_value=False), \
                patch.object(command, "_run_critic", side_effect=run), patch.object(command.click, "echo"):
            await command._run_critics(roster, paths, 5)

    async def test_exhausted_transport_rotates_once_without_a_fake_verdict(self):
        await self.run_critics({"Fixture": exhausted(), "Peer": "verdict"})
        self.assertEqual(self.seen, [("Fixture", 1), ("Peer", 1)])
        self.assertEqual(self.state["critic_verdicts"], [{"author": "Peer"}])

    async def test_all_transport_blocked_is_operational_not_missing_verdict_exhaustion(self):
        with self.assertRaisesRegex(Exception, "transport retries exhausted"):
            await self.run_critics({"Fixture": exhausted(), "Peer": exhausted()})
        self.assertEqual(self.seen, [("Fixture", 1), ("Peer", 1)])
        self.assertEqual(self.state["critic_verdicts"], [])

    async def test_unknown_critic_fault_remains_fatal(self):
        fault = ValueError("invalid machine snapshot")
        with self.assertRaises(ValueError) as caught:
            await self.run_critics({"Fixture": fault, "Peer": "verdict"})
        self.assertIs(caught.exception, fault)
        self.assertEqual(self.seen, [("Fixture", 1)])


if __name__ == "__main__":
    unittest.main()
