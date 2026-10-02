"""Bump custom-provider boundaries; canned notifications, no network/models."""

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import tomllib
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from unity import bump_orchestrator, bump_spawn
from unity.bump_provider import BumpProviderFailure, is_freeinference, provider_failure
from unity.roster import Agent


def agent(**overrides):
    return replace(Agent(name="Gableton", model="glm-5.3", provider="openai",
        backend="codex", strength=5., base_url="https://freeinference.org/v1",
        api_key="fixture-only-never-real", auth_token=None, budget=None, is_primary=True),
        **overrides)


def note(method, **payload):
    return NS(method=method, payload=NS(**payload))


class ProviderClassificationTests(unittest.TestCase):
    def test_exact_provider_identity_not_text_or_suffix_match(self):
        self.assertTrue(is_freeinference(agent()))
        self.assertTrue(is_freeinference(agent(base_url="https://freeinference.org:443/v1/")))
        for url in (None, "http://freeinference.org/v1", "https://freeinference.org.example/v1",
                    "https://freeinference.org/v1?key=fixture", "https://user@freeinference.org/v1",
                    "https://freeinference.org:123/v1", "https://elsewhere.invalid/freeinference.org/v1"):
            with self.subTest(url=url):
                self.assertFalse(is_freeinference(agent(base_url=url)))

    def test_explicit_credit_exhaustion_is_typed_without_raw_body(self):
        error = {"error": {"code": "insufficient_credits", "message": "secret fixture token value"},
                 "httpStatusCode": 402}
        failure = provider_failure(agent(), error)
        self.assertEqual(failure.category, "freeinference_credit_exhausted")
        self.assertEqual(failure.http_status, 402)
        self.assertNotIn("secret", str(failure))
        self.assertNotIn("fixture token", repr(failure.__dict__))

    def test_credit_message_in_trusted_transport_error_is_recognized(self):
        for message in ("Insufficient credits", "Your credits are exhausted", "Not enough credits",
                        "Insufficient account balance"):
            with self.subTest(message=message):
                self.assertEqual(provider_failure(agent(), NS(message=message)).category,
                                 "freeinference_credit_exhausted")

    def test_auth_outage_rate_and_generic_quota_never_mean_exhausted_credits(self):
        for status, message, category in (
                (429, "Too many requests", "rate_limited"),
                (429, "insufficient_quota", "rate_limited"),
                (401, "Authentication failed", "authentication_or_access_denied"),
                (503, "Unavailable", "provider_unavailable"),
                (402, "Payment required", "unsuccessful_turn")):
            with self.subTest(status=status, message=message):
                self.assertEqual(provider_failure(agent(), {"httpStatusCode": status, "message": message}).category,
                                 category)

    def test_credit_error_from_other_provider_does_not_enable_fi_fallback(self):
        failure = provider_failure(agent(base_url=None), {"code": "insufficient_credits"})
        self.assertNotEqual(failure.category, "freeinference_credit_exhausted")

    def test_nested_sdk_error_preserves_status_not_raw_headers(self):
        error = NS(message="header-secret", codex_error_info={
            "responseTooManyFailedAttempts": {"httpStatusCode": 503}})
        failure = provider_failure(agent(), error)
        self.assertEqual(failure.category, "provider_unavailable")
        self.assertNotIn("header-secret", str(failure))

    def test_model_prose_or_unknown_attributes_are_not_provider_evidence(self):
        failure = provider_failure(agent(), NS(text="Insufficient credits", response="insufficient_credits"))
        self.assertEqual(failure.category, "unsuccessful_turn")


class ProviderConfigTests(unittest.TestCase):
    def test_custom_provider_keeps_native_critic_policy_and_no_subscription_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            config = bump_orchestrator.build_bump_mcp(NS(project_root=root, forum=root / ".unity/forum"), "critic")
            with patch.object(Path, "home", return_value=root):
                self.assertEqual(bump_spawn._write_codex_config(private, agent(), config, bump_phase="critic"), "unity")
            actual = tomllib.loads((private / "config.toml").read_text())
            provider = actual["model_providers"]["unity"]
            self.assertEqual(provider["wire_api"], "responses")
            self.assertEqual(provider["env_key"], "CODEX_API_KEY")
            self.assertFalse(provider["requires_openai_auth"])
            self.assertNotIn(agent().api_key, (private / "config.toml").read_text())
            self.assertFalse((private / "auth.json").exists())
            self.assertEqual(set(actual["mcp_servers"]), {"unity-forum"})
            tools = actual["mcp_servers"]["unity-forum"]["enabled_tools"]
            self.assertIn("submit_formalization_verdict", tools)
            self.assertNotIn("finalize_formalization", tools)

    def test_custom_provider_url_is_toml_encoded_not_configuration_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            url = 'https://example.invalid/v1"\n[other]\nkey="injected'
            bump_spawn._write_codex_config(home, agent(base_url=url), {})
            actual = tomllib.loads((home / "config.toml").read_text())
            self.assertEqual(actual["model_providers"]["unity"]["base_url"], url)
            self.assertNotIn("other", actual)


class ProviderTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_records_safe_failure_category_without_changing_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".unity").mkdir()
            config = bump_orchestrator.build_bump_mcp(NS(project_root=root, forum=root / ".unity/forum"), "critic")
            original = agent()
            failure = BumpProviderFailure("freeinference_credit_exhausted", provider="freeinference", status=402)
            with patch.object(bump_spawn, "codex_spawner", new_callable=AsyncMock, side_effect=failure):
                with self.assertRaises(BumpProviderFailure):
                    await bump_spawn.spawn(original, "fixture", "fixture", root, config)
            import json
            record = json.loads((root / ".unity/logs/run.jsonl").read_text())
            self.assertEqual(record["provider_failure"]["category"], "freeinference_credit_exhausted")
            self.assertEqual(record["model"], original.model)
            self.assertNotIn(original.api_key, json.dumps(record))

    async def run_turn(self, notifications, *, callback=None, start_error=None, stopped=False, readiness_error=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = NS(thread_start=AsyncMock(), close=AsyncMock(), login_api_key=AsyncMock())
            thread = NS(id="fixture-thread", turn=AsyncMock(return_value=NS()))
            client.thread_start.return_value = thread
            client.thread_start.side_effect = start_error
            config_ctor = Mock(side_effect=lambda **kw: NS(**kw))
            sdk = NS(AsyncCodex=Mock(return_value=client), CodexConfig=config_ctor,
                     Sandbox=NS(read_only="read-only", workspace_write="workspace-write"),
                     ApprovalMode=NS(deny_all="never"))
            config = bump_orchestrator.build_bump_mcp(NS(project_root=root, forum=root / ".unity/forum"), "critic")
            policy = bump_spawn._bump_codex_tool_policy(config, "critic")
            client._client = NS(request=AsyncMock(return_value=NS(root={"data":[
                {"name":name,"runtimeStatus":"connected","tools":{tool:{} for tool in names}}
                for name,names in policy.items()]})))
            client._client.request.side_effect = readiness_error

            async def stream(*args):
                for item in notifications:
                    yield item

            self.client, self.thread, self.config_ctor = client, thread, config_ctor
            with patch.dict(sys.modules, {"openai_codex": sdk}), \
                    patch.object(bump_spawn.tempfile, "mkdtemp", return_value=str(root / "home")), \
                    patch.object(bump_spawn, "_worktree_write_roots", return_value=(root,)), \
                    patch.object(bump_spawn, "_stop_requested", return_value=stopped), \
                    patch.object(bump_spawn, "_codex_notifications", side_effect=stream), \
                    patch.object(bump_spawn, "_console") as console:
                self.console = console
                return await bump_spawn.codex_spawner(agent(), "review", "fixture", root, config,
                    env_overrides={"UNITY_BUMP_PROFILE": "critic"}, on_normal_completion=callback)

    async def test_failed_callback_free_critic_turn_raises_instead_of_returning_none(self):
        with self.assertRaises(BumpProviderFailure):
            await self.run_turn([note("turn/completed", turn=NS(status="failed", error=None))])
        self.client.close.assert_awaited_once()
        self.client.login_api_key.assert_not_awaited()
        self.thread.turn.assert_awaited_once()

    async def test_native_startup_failure_never_dispatches_and_closes(self):
        with self.assertRaises(BumpProviderFailure) as caught:
            await self.run_turn([],readiness_error=RuntimeError('fixture-private-error'))
        self.assertEqual(caught.exception.category,'native_mcp_status_failed')
        self.thread.turn.assert_not_awaited()
        self.client.close.assert_awaited_once()
        self.assertNotIn('fixture-private-error',str(caught.exception))

    async def test_truncated_stream_without_completed_status_fails_closed(self):
        with self.assertRaises(BumpProviderFailure):
            await self.run_turn([])
        self.client.close.assert_awaited_once()

    async def test_failed_final_turn_preserves_explicit_credit_exhaustion(self):
        with self.assertRaises(BumpProviderFailure) as caught:
            await self.run_turn([note("turn/completed", turn=NS(status="failed",
                error=NS(message="insufficient credits")))])
        self.assertEqual(caught.exception.category, "freeinference_credit_exhausted")

    async def test_explicit_credit_error_stops_even_if_backend_proposes_retry(self):
        with self.assertRaises(BumpProviderFailure) as caught:
            await self.run_turn([note("error", error=NS(message="credits exhausted: secret-fixture"), will_retry=True)])
        self.assertEqual(caught.exception.category, "freeinference_credit_exhausted")
        self.assertNotIn("secret-fixture", str(self.console.mock_calls))
        self.client.close.assert_awaited_once()

    async def test_recovered_bounded_reconnect_can_complete(self):
        result = await self.run_turn([
            note("error", error=NS(message="reconnecting"), will_retry=True),
            note("item/completed", item=NS(root=NS(type="agentMessage", text="done"))),
            note("turn/completed", turn=NS(status="completed", error=None)),
        ])
        self.assertEqual(result, "done")
        self.assertEqual(self.client.thread_start.call_args.kwargs["sandbox"], "read-only")
        self.assertEqual(self.client.thread_start.call_args.kwargs["model_provider"], "unity")

    async def test_nonretryable_transport_error_is_not_silently_accepted(self):
        with self.assertRaises(BumpProviderFailure):
            await self.run_turn([note("error", error=NS(message="unavailable"), will_retry=False)])

    async def test_rpc_credit_error_before_turn_has_same_safe_category(self):
        class RpcError(RuntimeError):
            message = "insufficient credits secret-fixture"

        with self.assertRaises(BumpProviderFailure) as caught:
            await self.run_turn([], start_error=RpcError())
        self.assertEqual(caught.exception.category, "freeinference_credit_exhausted")
        self.assertNotIn("secret-fixture", str(caught.exception))
        self.client.close.assert_awaited_once()

    async def test_controller_completion_failure_is_not_reclassified(self):
        expected = ValueError("controller fixture")
        callback = AsyncMock(side_effect=expected)
        with self.assertRaises(ValueError) as caught:
            await self.run_turn([note("turn/completed", turn=NS(status="completed", error=None))], callback=callback)
        self.assertIs(caught.exception, expected)

    async def test_intentional_stop_does_not_start_a_model_turn(self):
        self.assertIsNone(await self.run_turn([], stopped=True))
        self.thread.turn.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
