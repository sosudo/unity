import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openai_codex.types import ReasoningEffort
from unity import prove_runtime, spawn


class ProveEffortTests(unittest.IsolatedAsyncioTestCase):
    def test_resolution_and_validation(self):
        resolve = prove_runtime._prove_codex_reasoning_effort
        self.assertIsNone(resolve('Terra1', None))
        self.assertEqual(resolve('Terra1', '{"Terra1":"high","*":"medium"}'), 'high')
        self.assertEqual(resolve('Luna1', '{"Terra1":"high","*":"medium"}'), 'medium')
        self.assertIsNone(resolve('Luna1', '{"Terra1":"high"}'))
        for effort in ReasoningEffort:
            self.assertEqual(resolve('Terra1', effort.value), effort.value)
        for value in ('bogus', '["high"]', '{', '{"x":"high","y":"bogus"}', '{"x":null}', '{"":"high"}'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'UNITY_PROVE_CODEX_REASONING_EFFORT'):
                resolve('Terra1', value)

    async def test_codex_config_only_uses_explicit_prove_override(self):
        async def notifications(*args):
            if False:
                yield None
        for profile, explicit, ambient, expected in [
            ('prove', None, 'high', None), ('prove', {'UNITY_PROVE_CODEX_REASONING_EFFORT': 'high'}, 'low', 'high'),
            ('solve', {'UNITY_PROVE_CODEX_REASONING_EFFORT': 'high'}, 'high', None),
            ('prove', {}, 'high', None),
        ]:
            with self.subTest(profile=profile, explicit=explicit), tempfile.TemporaryDirectory() as temp:
                agent = SimpleNamespace(name='Terra1', model='gpt-test', api_key=None, base_url=None)
                handle = SimpleNamespace()
                thread = SimpleNamespace(turn=AsyncMock(return_value=handle))
                client = SimpleNamespace(thread_start=AsyncMock(return_value=thread), close=AsyncMock())
                config_ctor = Mock(side_effect=lambda **kw: SimpleNamespace(**kw))
                sdk = SimpleNamespace(AsyncCodex=Mock(return_value=client), CodexConfig=config_ctor,
                                      Sandbox=SimpleNamespace(full_access='full', workspace_write='write'))
                with patch.dict(os.environ, {'UNITY_PROVE_CODEX_REASONING_EFFORT': ambient}), \
                     patch.dict(sys.modules, {'openai_codex': sdk}), \
                     patch.object(spawn, '_write_codex_config', return_value=None), \
                     patch.object(spawn, '_write_codex_agents'), \
                     patch.object(spawn, '_agent_env', return_value={'PATH': ''}), \
                     patch.object(spawn, '_codex_notifications', notifications), \
                     patch.object(spawn, '_stop_requested', return_value=False):
                    await spawn.codex_spawner(agent, 'system', 'task', Path(temp), {},
                                              env_overrides=explicit, mcp_profile=profile)
                config = tomllib.loads('\n'.join(config_ctor.call_args.kwargs.get('config_overrides', ())))
                self.assertEqual(config.get('model_reasoning_effort'), expected)

    async def test_runtime_validates_before_creating_workspaces(self):
        agent = SimpleNamespace(name='Terra1', backend='codex')
        roster = SimpleNamespace(agents=[agent])
        paths = SimpleNamespace(project_root=Path('/unused'), forum=Path('/unused/forum'))
        with patch.dict(os.environ, {'UNITY_PROVE_CODEX_REASONING_EFFORT': '{"unused":"invalid"}'}), \
             patch.object(prove_runtime, 'stop_requested', return_value=False), \
             patch.object(prove_runtime, '_prove_worktree') as create:
            with self.assertRaisesRegex(ValueError, 'UNITY_PROVE_CODEX_REASONING_EFFORT'):
                await prove_runtime.run_prove_runtime(roster, paths, {}, 'prompt', reset_state=True)
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
