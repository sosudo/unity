import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from unity import prove_runtime, prove_state
from unity.commands import prove as command


class ProveWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'Main.lean').write_text('initial\n')
        self.git('add', 'Main.lean')
        self.git('commit', '-m', 'initial')
        self.unity = self.root / '.unity'
        self.unity.mkdir()
        self.paths = SimpleNamespace(project_root=self.root, unity=self.unity, forum=self.unity / 'forum',
                                     unity_md=self.unity / 'UNITY.md', agents_yaml=self.unity / 'agents.yaml')
        self.agent = SimpleNamespace(name='worker', backend='codex')
        self.roster = SimpleNamespace(agents=[self.agent], primary=self.agent)

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.root, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()

    def test_fresh_collision_preserves_unrelated_directory(self):
        unrelated = self.root / '.worktrees' / 'worker'
        unrelated.mkdir(parents=True)
        (unrelated / 'keep').write_text('important')
        with self.assertRaisesRegex(ValueError, 'existing work'):
            prove_runtime._prove_worktree(self.root, self.unity, 'worker', fresh=True)
        self.assertEqual((unrelated / 'keep').read_text(), 'important')

    async def test_critic_retry_preserves_dirty_and_committed_progress(self):
        dag = {'source': 'kernel', 'chunks': [{'id': 'target', 'lean_decl': 'target', 'lean_file': 'Main.lean'}]}
        invocations = []
        decisions = []
        async def spawn(agent, system, task, cwd, mcp, **kwargs):
            self.assertEqual(kwargs['env_overrides'], {'UNITY_PROVE_CODEX_REASONING_EFFORT': 'high'})
            invocations.append(task)
            if len(invocations) == 1:
                (cwd / 'Main.lean').write_text('committed progress\n')
                self.git('add', 'Main.lean', cwd=cwd)
                self.git('commit', '-m', 'progress', cwd=cwd)
                (cwd / 'Scratch.lean').write_text('dirty progress\n')
            else:
                self.assertEqual((cwd / 'Main.lean').read_text(), 'committed progress\n')
                self.assertEqual((cwd / 'Scratch.lean').read_text(), 'dirty progress\n')
                self.assertEqual(self.git('log', '-1', '--format=%s', cwd=cwd), 'progress')
                self.assertIn('preserved Prove workspace', task)
                self.assertIn('Scratch.lean', task)
                with prove_state.transaction(self.paths.forum) as state:
                    state['declarations']['target']['status'] = 'solved'
        async def critic(*args, **kwargs):
            approved = len(invocations) == 2
            decisions.append(approved)
            (self.unity / 'critic.json').write_text(json.dumps({
                'approved': approved, 'verdict': 'proven' if approved else 'stalled', 'reopen': []}))
        def build(*args):
            (self.unity / 'dag.json').write_text(json.dumps(dag))
            return dag
        with ExitStack() as stack:
            for name, value in {
                'load_paths': lambda: self.paths, 'load_roster': lambda *a, **k: self.roster,
                'build_mcp': lambda *a, **k: {}, 'resume_point': lambda *a: None,
                'architect': lambda *a: None, 'build_prove_dag': build, 'toposort': lambda *a: None,
                'mark_phase': lambda *a: None, 'mark_done': lambda *a: None,
                'load_prompt': lambda n: n, '_retrospective_enabled': lambda: False,
            }.items():
                stack.enter_context(patch.object(command, name, value))
            stack.enter_context(patch.object(command, 'dispatch', new=AsyncMock(side_effect=critic)))
            stack.enter_context(patch.object(prove_runtime, 'spawn', side_effect=spawn))
            stack.enter_context(patch.object(prove_runtime, 'load_prompt', lambda n: n))
            stack.enter_context(patch.object(prove_runtime, '_preamble', lambda *a, **k: ''))
            stack.enter_context(patch.object(prove_runtime, '_workspace_brief', lambda *a: ''))
            stack.enter_context(patch.object(prove_runtime.library, 'library_context', lambda: ''))
            stack.enter_context(patch.object(prove_runtime.library, 'library_subagents', lambda: []))
            stack.enter_context(patch.dict('os.environ', {
                'MAX_ATTEMPTS': '2', 'UNITY_PROVE_CODEX_REASONING_EFFORT': '{"worker":"high"}',
            }))
            await command.prove.callback(targets='All', continue_=False)
        self.assertEqual(decisions, [False, True])
        self.assertEqual(len(invocations), 2)
        tree = self.root / '.worktrees' / 'worker'
        self.assertEqual((tree / 'Scratch.lean').read_text(), 'dirty progress\n')
        self.assertEqual(self.git('branch', '--show-current', cwd=tree), 'worktree/worker')


if __name__ == '__main__':
    unittest.main()
