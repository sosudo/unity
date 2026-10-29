import json
from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncclick as click

from unity.commands import prove as command


class ProveCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        unity = root / '.unity'
        unity.mkdir()
        self.paths = SimpleNamespace(project_root=root, unity=unity, forum=unity / 'forum',
                                     agents_yaml=unity / 'agents.yaml')
        self.roster = SimpleNamespace(primary='worker')
        self.state = {'declarations': {'target': {'status': 'unresolved'}}}
        self.stopped = False
        self.decision = {'approved': False, 'verdict': 'stalled', 'reopen': []}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            'load_paths': lambda: self.paths,
            'load_roster': lambda *a, **kw: self.roster,
            'build_mcp': lambda *a, **kw: {},
            'resume_point': lambda *a: None,
            'architect': lambda *a: None,
            'build_prove_dag': lambda *a: {'chunks': [{'id': 'target'}], 'source': 'kernel'},
            'toposort': lambda *a: None,
            'mark_phase': lambda *a: None,
            'load_prompt': lambda x: x,
            'stop_requested': lambda *a: self.stopped,
            '_retrospective_enabled': lambda: True,
        }.items():
            self.stack.enter_context(patch.object(command, name, value))
        self.stack.enter_context(patch.object(command.prove_state, 'load_state', lambda *a: self.state))
        self.stack.enter_context(patch.object(command.prove_state, 'apply_critic_reopens',
                                              lambda *a: {'reopened': [], 'rejected': []}))
        self.done = self.stack.enter_context(patch.object(command, 'mark_done'))
        self.runtime = self.stack.enter_context(patch.object(command, 'run_prove_runtime', new=AsyncMock()))
        self.dispatch = self.stack.enter_context(patch.object(command, 'dispatch', new=AsyncMock(side_effect=self._dispatch)))
        self.stack.enter_context(patch.dict('os.environ', {'MAX_ATTEMPTS': '1'}))

    async def _dispatch(self, *args, **kwargs):
        if args[2] == 'prove/CRITIC':
            (self.paths.unity / 'critic.json').write_text(json.dumps(self.decision))

    async def run_command(self):
        await command.prove.callback(targets='All', continue_=False)

    async def test_stop_after_runtime_skips_critic_and_completion(self):
        async def stop(*args, **kwargs):
            self.stopped = True
        self.runtime.side_effect = stop
        with self.assertRaisesRegex(click.ClickException, 'stopped'):
            await self.run_command()
        self.dispatch.assert_not_awaited()
        self.done.assert_not_called()

    async def test_exhausted_attempts_are_not_completion(self):
        with self.assertRaisesRegex(click.ClickException, 'approved proven'):
            await self.run_command()
        self.assertEqual(self.dispatch.await_count, 1)
        self.done.assert_not_called()

    async def test_approved_solved_result_completes(self):
        self.state['declarations']['target']['status'] = 'solved'
        self.decision = {'approved': True, 'verdict': 'proven', 'reopen': []}
        await self.run_command()
        self.assertEqual(self.dispatch.await_count, 2)
        self.done.assert_called_once_with(self.paths, 'prove')

    async def test_zero_targets_exits_before_runtime(self):
        with patch.object(command, 'build_prove_dag', return_value={'chunks': [], 'source': 'kernel'}):
            with self.assertRaisesRegex(click.ClickException, 'no unresolved prove targets'):
                await self.run_command()
        self.runtime.assert_not_awaited()
        self.done.assert_not_called()

    async def test_stop_during_critic_skips_retrospective(self):
        async def stop(*args, **kwargs):
            self.stopped = True
        self.dispatch.side_effect = stop
        with self.assertRaisesRegex(click.ClickException, 'stopped'):
            await self.run_command()
        self.assertEqual(self.dispatch.await_count, 1)
        self.done.assert_not_called()

    async def test_resume_retrospective_requires_prior_approval(self):
        with patch.object(command, 'resume_point', return_value='retrospective'):
            with self.assertRaisesRegex(click.ClickException, 'approved proven'):
                await command.prove.callback(targets='All', continue_=True)
        self.dispatch.assert_not_awaited()
        self.done.assert_not_called()
