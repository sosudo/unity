"""Offline runtime integration through real Git and the mechanical merge path."""
import asyncio
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unity import candidate_review, prove_runtime, prove_state, worktree
from unity.commands import prove as command


class NoToolsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / 'Main.lean').write_text('theorem foo : True := by sorry\ntheorem bar : True := by sorry\n')
        self.git('add', 'Main.lean')
        self.git('commit', '-m', 'initial')
        self.unity = self.root / '.unity'
        self.unity.mkdir()
        self.paths = SimpleNamespace(project_root=self.root, unity=self.unity,
                                     forum=self.unity / 'forum', unity_md=self.unity / 'UNITY.md')
        self.agent = SimpleNamespace(name='worker', backend='other', model='fixture')
        self.roster = SimpleNamespace(agents=[self.agent], primary=self.agent)
        self.chunks = [{'id': 'foo', 'lean_decl': 'foo', 'lean_file': 'Main.lean'}]
        self.reviews = []
        self.builds = []

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.root,
                                       text=True, stderr=subprocess.PIPE).strip()

    def commit_proof(self, cwd, decl, text):
        (cwd / 'Main.lean').write_text(text)
        self.git('add', 'Main.lean', cwd=cwd)
        self.git('commit', '-m', f'prove-candidate: {decl}', cwd=cwd)
        return self.git('rev-parse', 'HEAD', cwd=cwd)

    async def run_runtime(self, spawn, review=None):
        (self.unity / 'dag.json').write_text(json.dumps({'chunks': self.chunks}))
        real_run = subprocess.run
        def run(args, *a, **kw):
            if args == ['lake', 'build']:
                self.builds.append(args)
                return subprocess.CompletedProcess(args, 0, '', '')
            return real_run(args, *a, **kw)
        def check(root, candidate, declaration, **kwargs):
            self.reviews.append((candidate['commit_sha'], declaration['decl']))
            return review(root, candidate, declaration, **kwargs) if review else {'status': 'passed', 'issues': []}
        assertions = []
        async def checked_spawn(agent, system, task, cwd, mcp, **kwargs):
            try:
                self.assertTrue({'unity-forum', 'forum', 'unity-prove'}.isdisjoint(mcp))
                self.assertEqual(kwargs['mcp_profile'], 'prove-no-tools')
                self.assertNotRegex(system + task, r'unity-prove|forum_|emit_candidate|claim_strategy|prove_status')
                return await spawn(agent, system, task, cwd, mcp, **kwargs)
            except AssertionError as exc:
                assertions.append(exc)
                raise
        with ExitStack() as stack:
            stack.enter_context(patch.object(prove_runtime, 'spawn', side_effect=checked_spawn))
            stack.enter_context(patch.object(prove_runtime, 'load_prompt', return_value=''))
            stack.enter_context(patch.object(prove_runtime, '_preamble', return_value=''))
            stack.enter_context(patch.object(prove_runtime.library, 'library_context', return_value=''))
            stack.enter_context(patch.object(prove_runtime.library, 'library_subagents', return_value=[]))
            stack.enter_context(patch.object(worktree.subprocess, 'run', side_effect=run))
            stack.enter_context(patch.object(candidate_review, 'review_candidate', side_effect=check))
            result = await asyncio.wait_for(prove_runtime.run_prove_runtime(self.roster, self.paths, {}, ''), 15)
        if assertions:
            raise assertions[0]  # The runtime logs worker errors; do not hide test assertions.
        return result

    async def test_candidate_passes_actual_git_merge_and_mechanical_gates(self):
        shas = []
        async def spawn(agent, system, task, cwd, mcp, **kwargs):
            shas.append(self.commit_proof(cwd, 'foo', 'theorem foo : True := by trivial\ntheorem bar : True := by sorry\n'))
        state = await self.run_runtime(spawn)
        self.assertEqual(state['declarations']['foo']['status'], 'solved')
        candidate = next(iter(state['candidates'].values()))
        self.assertEqual(candidate['commit_sha'], shas[0])
        self.assertEqual(candidate['verification']['status'], 'passed')
        self.assertEqual(self.reviews, [(shas[0], 'foo')])
        self.assertEqual(len(self.builds), 1)
        self.assertIn('by trivial', (self.root / 'Main.lean').read_text())

    async def test_failed_candidate_restarts_from_main_and_repairs(self):
        shas = []
        async def spawn(agent, system, task, cwd, mcp, **kwargs):
            if shas:
                self.assertIn('prior candidate failed mechanical review', task)
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=cwd), self.git('rev-parse', 'HEAD'))
            proof = 'first' if not shas else 'corrected'
            shas.append(self.commit_proof(cwd, 'foo', f'-- {proof}\ntheorem foo : True := by trivial\n'))
        def review(root, candidate, declaration, **kwargs):
            return {'status': 'failed', 'issues': ['bad proof']} if len(self.reviews) == 1 else {'status': 'passed', 'issues': []}
        state = await self.run_runtime(spawn, review)
        self.assertEqual(state['declarations']['foo']['status'], 'solved')
        self.assertEqual({c['status'] for c in state['candidates'].values()}, {'failed', 'merged'})
        self.assertEqual([sha for sha, _ in self.reviews], shas)
        self.assertIn('corrected', (self.root / 'Main.lean').read_text())

    async def test_blocked_candidate_retries_same_commit(self):
        shas = []
        async def spawn(agent, system, task, cwd, mcp, **kwargs):
            if not shas:
                shas.append(self.commit_proof(cwd, 'foo', 'theorem foo : True := by trivial\n'))
                (self.root / 'Main.lean').write_text('temporary integration edit\n')
            else:
                self.assertIn('integration condition', task)
                self.git('checkout', '--', 'Main.lean')
        state = await self.run_runtime(spawn)
        candidate = next(iter(state['candidates'].values()))
        self.assertEqual(candidate['status'], 'merged')
        self.assertEqual(candidate['commit_sha'], shas[0])
        self.assertEqual(candidate['merge_attempts'], 2)
        self.assertEqual(self.reviews, [(shas[0], 'foo')])

    async def test_dependency_and_preserved_reservation_across_rounds(self):
        self.chunks.append({'id': 'bar', 'lean_decl': 'bar', 'lean_file': 'Main.lean', 'dependencies': ['foo']})
        calls = []
        async def first(agent, system, task, cwd, mcp, **kwargs):
            calls.append('partial')
            self.assertIn('assigned declaration is `foo`', task)
            (cwd / 'Scratch.lean').write_text('preserved')
        first_state = await self.run_runtime(first)
        self.assertEqual(first_state['declarations']['bar']['status'], 'unresolved')
        async def second(agent, system, task, cwd, mcp, **kwargs):
            if 'assigned declaration is `foo`' in task:
                self.assertEqual((cwd / 'Scratch.lean').read_text(), 'preserved')
                (cwd / 'Scratch.lean').unlink()
                self.commit_proof(cwd, 'foo', 'theorem foo : True := by trivial\ntheorem bar : True := by sorry\n')
                calls.append('foo')
            else:
                self.assertIn('assigned declaration is `bar`', task)
                self.commit_proof(cwd, 'bar', 'theorem foo : True := by trivial\ntheorem bar : True := by trivial\n')
                calls.append('bar')
        state = await self.run_runtime(second)
        self.assertEqual(calls, ['partial', 'foo', 'bar'])
        self.assertEqual(state['declarations']['bar']['status'], 'solved')

    async def test_stop_preserves_submission_and_resume_reviews_without_another_turn(self):
        shas = []
        async def first(agent, system, task, cwd, mcp, **kwargs):
            shas.append(self.commit_proof(cwd, 'foo', 'theorem foo : True := by trivial\n'))
            (self.unity / 'stop-requested').touch()
        stopped = await self.run_runtime(first)
        self.assertFalse(prove_state.all_solved(stopped))
        tree = worktree.agent_worktree(self.root, 'worker')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=tree), shas[0])
        (self.unity / 'stop-requested').unlink()
        async def unexpected(*args, **kwargs):
            self.fail('A saved complete submission should be reviewed before another agent turn')
        resumed = await self.run_runtime(unexpected)
        self.assertTrue(prove_state.all_solved(resumed))
        self.assertEqual(self.reviews, [(shas[0], 'foo')])

    async def test_command_reaches_critic_approval_from_a_git_submission(self):
        self.paths.agents_yaml = self.unity / 'agents.yaml'
        async def spawn(agent, system, task, cwd, mcp, **kwargs):
            self.commit_proof(cwd, 'foo', 'theorem foo : True := by trivial\n')
        async def runtime(*args, **kwargs):
            return await self.run_runtime(spawn)
        async def critic(*args, **kwargs):
            snapshot = json.loads((self.unity / 'prove-status.json').read_text())
            self.assertEqual(snapshot['declarations']['foo']['status'], 'solved')
            self.assertTrue(all(c['verification']['status'] == 'passed'
                                for c in snapshot['candidates'].values()))
            self.assertFalse(kwargs['forum_enabled'])
            self.assertFalse(kwargs['proof_controls'])
            (self.unity / 'critic.json').write_text(json.dumps(
                {'approved': True, 'verdict': 'proven', 'reopen': []}))
        with ExitStack() as stack:
            for name, replacement in {
                'load_paths': lambda: self.paths, 'load_roster': lambda *a, **k: self.roster,
                'build_mcp': lambda *a, **k: {}, 'resume_point': lambda *a: 'proving',
                'load_prompt': lambda *a: '', '_retrospective_enabled': lambda: False,
                'run_prove_runtime': runtime, 'dispatch': critic,
            }.items():
                stack.enter_context(patch.object(command, name, replacement))
            completed = stack.enter_context(patch.object(command, 'mark_done'))
            await command.prove.callback(targets='All', continue_=True)
        completed.assert_called_once_with(self.paths, 'prove')


if __name__ == '__main__':
    unittest.main()
