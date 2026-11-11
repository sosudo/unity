"""Prove transport and prompt boundaries without providers or Lean compilation."""
import copy
import json
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner

from unity import artifacts, orchestrator, prove_mcp, prove_state
from unity.commands import mcp
from unity.config import Paths
from unity.spawn import _codex_mcp_note


class ProofControlTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = Paths.from_unity_dir(self.root / '.unity')
        self.paths.unity.mkdir()
        (self.paths.unity / 'state.json').write_text(json.dumps(
            {'command': 'prove', 'phase': 'proving'}))
        (self.paths.unity / 'dag.json').write_text(json.dumps({'chunks': [
            {'id': 'target', 'lean_decl': 'target', 'lean_file': 'Main.lean'}]}))
        prove_state.initialize_from_dag(self.paths.forum, self.paths.unity / 'dag.json', 'base')

    async def invoke(self, server, tool, args='{}'):
        with chdir(self.root):
            return await CliRunner().invoke(mcp.command, [server, tool, args])

    async def test_discussion_aliases_are_denied_in_every_prove_phase(self):
        for phase in ('chunking', 'proving', 'critic', 'retrospective', 'done'):
            (self.paths.unity / 'state.json').write_text(json.dumps(
                {'command': 'prove', 'phase': phase}))
            for server in ('unity-forum', 'forum'):
                with self.subTest(phase=phase, server=server), patch('fastmcp.Client') as client:
                    result = await self.invoke(server, 'forum_post')
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertIn('disabled', result.output)
                    client.assert_not_called()

    async def test_control_status_is_available_but_discussion_tools_are_not(self):
        result = await self.invoke('unity-prove', 'prove_status')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('target', result.output)
        for tool in ('forum_post', 'forum_question', 'publish_finding', 'ledger_get', 'assist_strategy'):
            with self.subTest(tool=tool):
                result = await self.invoke('unity-prove', tool)
                self.assertNotEqual(result.exit_code, 0)
        self.assertFalse((self.paths.forum / 'threads').exists())

    async def test_primary_dispatch_uses_no_discussion_preamble_or_brief(self):
        agent = SimpleNamespace(name='Terra1', model='fixture', backend='codex')
        roster = SimpleNamespace(primary=agent, agents=[agent])
        launch = AsyncMock()
        with patch.object(orchestrator, 'spawn', launch), \
             patch.object(orchestrator.library, 'library_context', return_value=''), \
             patch.object(orchestrator.library, 'library_subagents', return_value=[]):
            await orchestrator.dispatch([agent], roster, orchestrator.load_prompt('prove/CRITIC'),
                'Review the proof.', self.root, {}, tools_prompt='PROVE_TOOLS', icrl_enabled=False,
                forum_enabled=False, brief_provider=lambda author: 'Proof state: target unresolved.',
                mcp_profile='prove-control')
        system = launch.await_args.args[1]
        self.assertNotRegex(system.lower(), r'forum|ledger_|publish_finding|assist_strategy')
        self.assertIn('Proof state: target unresolved.', system)
        self.assertEqual(launch.await_args.kwargs['mcp_profile'], 'prove-control')

    def test_mapping_replaces_both_discussion_aliases_without_mutating_inputs(self):
        original = {'unity-forum': {'command': 'old'}, 'forum': {'command': 'alias'},
                    'lean-lsp': {'command': 'lean', 'env': {'KEEP': 'value'}},
                    'axle': {'command': 'axle'}}
        before = copy.deepcopy(original)
        mapping = prove_mcp.for_run(original, self.paths)
        self.assertEqual(original, before)
        self.assertEqual(set(mapping), {'unity-prove', 'lean-lsp', 'axle'})
        self.assertIn('unity.prove_control', mapping['unity-prove']['args'])
        self.assertEqual(mapping['lean-lsp']['env']['KEEP'], 'value')
        self.assertEqual(mapping['lean-lsp']['env']['LEAN_PROJECT_PATH'], str(self.root))

    def test_prompts_and_backend_notes_have_no_discussion_contract(self):
        for name in ('PROVE_TOOLS', 'prove/PROVING', 'prove/CRITIC', 'prove/RETROSPECTIVE'):
            with self.subTest(name=name):
                self.assertNotRegex(orchestrator.load_prompt(name).lower(),
                                    r'forum|ledger_|publish_finding|assist_strategy|forum_')
        note = _codex_mcp_note('prove-control')
        self.assertIn('unity-prove', note)
        self.assertNotIn('forum', note.lower())
        self.assertIn('unity-forum', _codex_mcp_note('solve'))

    def test_diagnostic_output_points_to_enabled_control_server(self):
        content = {'artifact_id': 'artifact-fixture', 'sha256': 'hash',
                   'bytes': 9000, 'lines': 100, 'preview': 'error: fixture'}
        rendered = artifacts.format_compacted(content, server='unity-prove')
        self.assertIn('unity mcp unity-prove artifact_read', rendered)
        self.assertNotIn('forum', rendered)


if __name__ == '__main__':
    unittest.main()
