"""Prove checkout bindings across real command/runtime boundaries, without providers."""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, chdir
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from asyncclick.testing import CliRunner

from unity import prove_mcp, prove_runtime, prove_state
from unity.commands import mcp as mcp_command
from unity.commands import prove as prove_command


def server_specs():
    return {
        'lean-lsp': {'command': 'uvx', 'args': ['lean-lsp-mcp'],
                     'env': {'LEAN_PROJECT_PATH': '/stale/main', 'LEAN_SETTING': 'keep'}},
        'unity-forum': {'command': 'python', 'args': ['shared-forum']},
        'axle': {'command': 'axle', 'env': {'AXLE_API_KEY': 'fixture-only'}},
    }


class CheckoutMappingTests(unittest.TestCase):
    def test_bindings_are_independent_and_preserve_other_server_configuration(self):
        original = server_specs()
        snapshot = copy.deepcopy(original)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {'LEAN_PROJECT_PATH': '/ambient/wrong', 'UNRELATED_SECRET': 'do-not-copy'}
        ):
            root = Path(directory)
            main = prove_mcp.for_checkout(original, root)
            worker = prove_mcp.for_checkout(main, root / '.worktrees' / 'Luna1')
        self.assertEqual(original, snapshot)
        self.assertEqual(main['lean-lsp']['env'], {
            'LEAN_PROJECT_PATH': str(root.resolve()), 'LEAN_SETTING': 'keep'})
        self.assertEqual(worker['lean-lsp']['env'], {
            'LEAN_PROJECT_PATH': str((root / '.worktrees' / 'Luna1').resolve()),
            'LEAN_SETTING': 'keep'})
        self.assertIsNot(main['lean-lsp']['env'], worker['lean-lsp']['env'])
        self.assertIs(main['unity-forum'], original['unity-forum'])
        self.assertIs(worker['axle'], original['axle'])
        self.assertEqual(worker['lean-lsp']['args'], original['lean-lsp']['args'])

    def test_absent_lean_server_is_not_added(self):
        original = {'unity-forum': {'command': 'shared-forum'}}
        self.assertIs(prove_mcp.for_checkout(original, Path('/unused')), original)


class ProveLaunchBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_checkouts_and_primary_phases_receive_separate_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args],
                                               stderr=subprocess.PIPE, text=True).strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (root / 'Main.lean').write_text('-- disposable scheduler fixture\n')
            git('add', 'Main.lean')
            git('commit', '-m', 'fixture')
            unity = root / '.unity'
            unity.mkdir()
            paths = SimpleNamespace(project_root=root, unity=unity, forum=unity / 'forum',
                                    unity_md=unity / 'UNITY.md', agents_yaml=unity / 'agents.yaml')
            agents = [SimpleNamespace(name='Terra1', backend='codex'),
                      SimpleNamespace(name='Luna1', backend='codex')]
            roster = SimpleNamespace(agents=agents, primary=agents[0])
            mcp = server_specs()
            original = copy.deepcopy(mcp)
            launches = []
            primary_phases = []
            dag = {'source': 'kernel', 'chunks': [
                {'id': 'target', 'lean_decl': 'target', 'lean_file': 'Main.lean'}]}
            def build(*args):
                (unity / 'dag.json').write_text(json.dumps(dag))
                return dag
            async def worker(agent, system, task, cwd, mapping, **kwargs):
                launches.append((agent.name, cwd, mapping))
                if len(launches) == len(agents):
                    with prove_state.transaction(paths.forum) as state:
                        state['declarations']['target']['status'] = 'solved'
            async def primary(*args, **kwargs):
                primary_phases.append((args[2], args[4], args[5]))
                if args[2] == 'prove/CRITIC':
                    (unity / 'critic.json').write_text(json.dumps({
                        'approved': True, 'verdict': 'proven', 'reopen': []}))
            with ExitStack() as stack:
                replacements = {
                    'load_paths': lambda: paths, 'load_roster': lambda *a, **k: roster,
                    'build_mcp': lambda *a, **k: mcp, 'resume_point': lambda *a: None,
                    'architect': lambda *a: None, 'build_prove_dag': build,
                    'toposort': lambda *a: None, 'mark_phase': lambda *a: None,
                    'mark_done': lambda *a: None, 'load_prompt': lambda n: n,
                    '_retrospective_enabled': lambda: True,
                }
                for name, replacement in replacements.items():
                    stack.enter_context(patch.object(prove_command, name, replacement))
                stack.enter_context(patch.object(prove_command, 'dispatch', new=AsyncMock(side_effect=primary)))
                stack.enter_context(patch.object(prove_runtime, 'spawn', side_effect=worker))
                stack.enter_context(patch.object(prove_runtime, 'load_prompt', lambda n: n))
                stack.enter_context(patch.object(prove_runtime, '_preamble', lambda *a, **k: ''))
                stack.enter_context(patch.object(prove_runtime, '_workspace_brief', lambda *a: ''))
                stack.enter_context(patch.object(prove_runtime.library, 'library_context', lambda: ''))
                stack.enter_context(patch.object(prove_runtime.library, 'library_subagents', lambda: []))
                stack.enter_context(patch.dict(os.environ, {
                    'MAX_ATTEMPTS': '1',
                    'UNITY_PROVE_CODEX_REASONING_EFFORT': '{"Terra1":"high","Luna1":"medium"}',
                }))
                await prove_command.prove.callback(targets='All', continue_=False)
            self.assertEqual({name for name, _, _ in launches}, {'Terra1', 'Luna1'})
            for name, cwd, mapping in launches:
                self.assertEqual(cwd, root / '.worktrees' / name)
                self.assertEqual((cwd / '.unity').resolve(), unity.resolve())
                self.assertEqual(mapping['lean-lsp']['env']['LEAN_PROJECT_PATH'], str(cwd.resolve()))
                self.assertNotIn('unity-forum', mapping)
                self.assertIn('unity-prove', mapping)
            self.assertEqual([phase for phase, _, _ in primary_phases],
                             ['prove/CRITIC', 'prove/RETROSPECTIVE'])
            for _, cwd, mapping in primary_phases:
                self.assertEqual(cwd, root)
                self.assertEqual(mapping['lean-lsp']['env']['LEAN_PROJECT_PATH'], str(root.resolve()))
            self.assertEqual(mcp, original)


class ShellBridgeBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unity = self.root / '.unity'
        self.unity.mkdir()
        self.worker = self.root / '.worktrees' / 'Luna1'
        self.nested = self.worker / 'src' / 'nested'
        self.nested.mkdir(parents=True)
        (self.worker / '.unity').symlink_to(self.unity, target_is_directory=True)
        self.specs = server_specs()
        self.snapshot = copy.deepcopy(self.specs)

    async def invoke(self, cwd, state, server='lean-lsp'):
        state_file = self.unity / 'state.json'
        if state is None:
            state_file.unlink(missing_ok=True)
        else:
            state_file.write_text(json.dumps(state))
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.call_tool.return_value = SimpleNamespace(content=[])
        factory = Mock(return_value=client)
        stdio = AsyncMock(return_value=(SimpleNamespace(content=[]), ''))
        with chdir(cwd), ExitStack() as stack:
            for module, name in [
                ('unity.orchestrator', 'build_mcp'),
                ('unity.orchestrator', 'build_solve_mcp'),
                ('unity.bump_orchestrator', 'build_bump_mcp'),
                ('unity.formalize_orchestrator', 'build_formalize_mcp'),
                ('unity.autoformalize_orchestrator', 'build_autoformalize_mcp'),
            ]:
                stack.enter_context(patch(module + '.' + name, return_value=self.specs))
            stack.enter_context(patch('fastmcp.Client', factory))
            stack.enter_context(patch.object(mcp_command, '_call_solve_stdio', stdio))
            stack.enter_context(patch.object(mcp_command, '_call_autoformalize_stdio', stdio))
            result = await CliRunner().invoke(mcp_command.command,
                [server, 'lean_local_search', '{"query":"fixture_name"}'],
                catch_exceptions=False)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.specs, self.snapshot)
        if factory.called:
            self.assertEqual(factory.call_count, 1)
            client.call_tool.assert_awaited_once_with('lean_local_search', {'query': 'fixture_name'})
            return factory.call_args.args[0]['mcpServers'][server]
        self.assertEqual(stdio.await_count, 1)
        return stdio.await_args.args[2]

    async def test_each_fresh_call_binds_main_or_nested_linked_worktree(self):
        for cwd, expected in [(self.root, self.root), (self.nested, self.worker),
                              (self.nested, self.worker), (self.root, self.root)]:
            with self.subTest(cwd=cwd):
                spec = await self.invoke(cwd, {'command': 'prove', 'phase': 'proving'})
                self.assertEqual(spec['env']['LEAN_PROJECT_PATH'], str(expected.resolve()))
                self.assertEqual(spec['env']['LEAN_SETTING'], 'keep')

    async def test_other_commands_and_finished_prove_keep_existing_server_spec(self):
        states = [None, {'command': 'prove', 'phase': 'done'},
                  {'command': 'optimize', 'phase': 'optimizing'},
                  {'command': 'solve', 'phase': 'solving'},
                  {'command': 'bump', 'phase': 'chunking'},
                  {'command': 'formalize', 'phase': 'chunking'},
                  {'command': 'autoformalize', 'phase': 'chunking'}]
        for state in states:
            with self.subTest(state=state):
                spec = await self.invoke(self.nested, state)
                self.assertIs(spec, self.specs['lean-lsp'])
                self.assertEqual(spec['env']['LEAN_PROJECT_PATH'], '/stale/main')

    async def test_active_prove_does_not_rebind_an_unrelated_server(self):
        spec = await self.invoke(self.nested, {'command': 'prove', 'phase': 'critic'}, server='axle')
        self.assertIs(spec, self.specs['axle'])
        self.assertEqual(spec['env'], {'AXLE_API_KEY': 'fixture-only'})


if __name__ == '__main__':
    unittest.main()
