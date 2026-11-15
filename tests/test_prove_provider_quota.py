import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openai_codex import ApprovalMode
from unity import orchestrator, spawn
from unity.prove_provider import ProveProviderQuotaError, quota_exhausted


class ProveQuotaClassificationTests(unittest.TestCase):
    def test_billing_exhaustion_is_distinct_from_transient_rate_limits(self):
        for message, code in [
            ('Quota exceeded. Check your plan and billing details.', ''),
            ('The project has reached its limit.', 'project_spend_limit_exceeded'),
            ('You exceeded your current quota; check billing.', 'insufficient_quota'),
        ]:
            with self.subTest(message=message):
                self.assertTrue(quota_exhausted(message, code))
        for message, code in [
            ('Rate limit reached for tokens per minute; retry later.', 'rate_limit_exceeded'),
            ('Quota exceeded for requests per minute.', 'rate_limit_exceeded'),
            ('Context window exceeded.', ''),
        ]:
            with self.subTest(message=message):
                self.assertFalse(quota_exhausted(message, code))


class ProveQuotaTransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_note(self, note, profile, expect_quota):
        async def notifications(*args):
            yield note
        with tempfile.TemporaryDirectory() as temp:
            agent = SimpleNamespace(name='Terra1', model='fixture', api_key=None, base_url=None)
            thread = SimpleNamespace(turn=AsyncMock(return_value=SimpleNamespace()))
            client = SimpleNamespace(thread_start=AsyncMock(return_value=thread), close=AsyncMock())
            sdk = SimpleNamespace(
                AsyncCodex=Mock(return_value=client),
                CodexConfig=Mock(side_effect=lambda **kw: SimpleNamespace(**kw)),
                Sandbox=SimpleNamespace(full_access='full', workspace_write='write'),
                ApprovalMode=ApprovalMode,
            )
            with patch.dict(sys.modules, {'openai_codex': sdk}), \
                 patch.dict(spawn._last_run_stats, {'Terra1': {'usage': {'old': 1}}}, clear=True), \
                 patch.object(spawn, '_write_codex_config', return_value=None), \
                 patch.object(spawn, '_write_codex_agents'), \
                 patch.object(spawn, '_agent_env', return_value={'PATH': ''}), \
                 patch.object(spawn, '_codex_notifications', notifications), \
                 patch.object(spawn, '_stop_requested', return_value=False), \
                 patch.object(spawn, '_log'), \
                 patch.object(spawn, '_terminate_process_group', new_callable=AsyncMock), \
                 patch.object(spawn, '_give_up', return_value=True) as retry:
                call = spawn.codex_spawner(agent, 'system', 'task', Path(temp), {},
                                            mcp_profile=profile)
                if expect_quota:
                    with self.assertRaises(ProveProviderQuotaError):
                        await asyncio.wait_for(call, 2)
                    self.assertIsNone(spawn._last_run_stats['Terra1']['usage'])
                else:
                    self.assertIsNone(await asyncio.wait_for(call, 2))
                retry.assert_not_called()
            self.assertEqual(thread.turn.await_count, 1)
            client.close.assert_awaited_once()

    async def test_prove_quota_notifications_are_fatal_without_transport_retries(self):
        error = SimpleNamespace(message='Quota exceeded. Check your plan and billing details.',
                                code='insufficient_quota')
        for method, payload in [
            ('error', SimpleNamespace(error=error)),
            ('turn/completed', SimpleNamespace(turn=SimpleNamespace(status='failed', error=error))),
        ]:
            for profile in ('prove', 'prove-no-tools'):
                with self.subTest(method=method, profile=profile):
                    await self.run_note(SimpleNamespace(method=method, payload=payload), profile, True)

    async def test_other_profiles_and_transient_limits_keep_their_existing_behavior(self):
        quota = SimpleNamespace(method='error', payload=SimpleNamespace(error=SimpleNamespace(
            message='Quota exceeded. Check your plan and billing details.')))
        await self.run_note(quota, 'solve', False)
        rate = SimpleNamespace(method='error', payload=SimpleNamespace(error=SimpleNamespace(
            message='Rate limit reached for tokens per minute.', code='rate_limit_exceeded')))
        await self.run_note(rate, 'prove-no-tools', False)

    async def test_critic_dispatch_propagates_quota_failure(self):
        agent = SimpleNamespace(name='critic')
        roster = SimpleNamespace(agents=[agent], primary=agent)
        with patch.object(orchestrator, 'stop_requested', return_value=False), \
             patch.object(orchestrator, 'load_prompt', return_value=''), \
             patch.object(orchestrator.library, 'library_context', return_value=''), \
             patch.object(orchestrator.library, 'library_subagents', return_value=[]), \
             patch('unity.config.find_unity_dir', return_value=None), \
             patch.object(orchestrator, '_preamble', return_value=''), \
             patch.object(orchestrator, 'spawn', new_callable=AsyncMock,
                          side_effect=ProveProviderQuotaError()) as launch:
            with self.assertRaises(ProveProviderQuotaError):
                await orchestrator.dispatch([agent], roster, '', '', Path('/unused'), {},
                                            icrl_enabled=False, forum_enabled=False,
                                            proof_controls=False, mcp_profile='prove-no-tools')
        launch.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
