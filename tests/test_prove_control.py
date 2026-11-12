"""Core proof controls preserve candidate checks without discussion posts."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastmcp import Client

from unity import prove_control as control, prove_state, worktree


class ProofControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / 'Main.lean').write_text('theorem target : True := by sorry\n')
        self.git('add', 'Main.lean')
        self.git('commit', '-m', 'baseline')
        worktree.ensure_git_excludes(self.root, ('.unity/', '.worktrees/'))
        self.tree = worktree.create_worktree('Alice', self.root)
        self.state_dir = self.root / '.unity' / 'forum'
        self.state_dir.mkdir(parents=True)
        dag = self.state_dir.parent / 'dag.json'
        dag.write_text(json.dumps({'chunks': [
            {'id': 'target', 'lean_decl': 'target', 'lean_file': 'Main.lean'}]}))
        prove_state.initialize_from_dag(self.state_dir, dag, worktree.main_commit(self.root))
        control.configure(self.state_dir, self.root)

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.root,
                                       text=True, stderr=subprocess.PIPE).strip()

    def candidate(self):
        strategy = control.register_strategy('Alice', 'direct proof', 'target')['strategy']
        self.assertEqual(control.claim_strategy(strategy['strategy_id'], 'Alice')['status'], 'claimed')
        (self.tree / 'Main.lean').write_text('theorem target : True := by trivial\n')
        self.git('add', 'Main.lean', cwd=self.tree)
        self.git('commit', '-m', 'proof', cwd=self.tree)
        sha = self.git('rev-parse', 'HEAD', cwd=self.tree)
        result = control.emit_candidate(strategy['strategy_id'], 'Alice', sha, 'target')
        self.assertNotIn('forum_post_id', result)
        self.assertEqual(result['candidate']['commit_sha'], sha)
        return result['candidate']

    async def test_only_proof_and_diagnostic_tools_are_exposed(self):
        async with Client(control.mcp) as client:
            tools = await client.list_tools()
        self.assertEqual({tool.name for tool in tools}, {
            'artifact_info', 'artifact_read', 'register_strategy', 'claim_strategy',
            'unclaim_strategy', 'mark_strategy_incorrect', 'emit_candidate',
            'sync_to_main', 'sync_from_main', 'prove_status',
        })

    def test_candidate_uses_original_mechanical_verifier_before_becoming_solved(self):
        candidate = self.candidate()
        self.assertNotEqual(prove_state.load_state(self.state_dir)['declarations']['target']['status'], 'solved')
        with patch.object(worktree, 'sync_candidate_to_main', return_value={
            'ok': True, 'main_sha': 'accepted', 'build': {'passed': True},
            'verification': {'passed': True},
        }) as verify:
            result = control.sync_to_main(candidate['candidate_id'], 'Unity')
            self.assertTrue(result['sync']['ok'])
            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[1]['commit_sha'], candidate['commit_sha'])
            self.assertEqual(control.sync_to_main(candidate['candidate_id'], 'Unity')['idempotent'], True)
            verify.assert_called_once()
        state = prove_state.load_state(self.state_dir)
        self.assertEqual(state['declarations']['target']['status'], 'solved')
        self.assertFalse((self.state_dir / 'threads').exists())

    def test_blocked_candidate_can_retry_without_new_submission(self):
        candidate = self.candidate()
        with patch.object(worktree, 'sync_candidate_to_main', return_value={
            'ok': False, 'blocked': True, 'error': 'main has local edits',
            'blocker_kind': 'dirty_main', 'git_status': [' M Main.lean'],
        }):
            result = control.sync_to_main(candidate['candidate_id'], 'Unity')
        self.assertIn('retry', result)
        self.assertEqual(prove_state.load_state(self.state_dir)['candidates'][candidate['candidate_id']]['status'], 'merge_blocked')
        with patch.object(worktree, 'sync_candidate_to_main', return_value={'ok': True, 'main_sha': 'accepted'}):
            control.sync_to_main(candidate['candidate_id'], 'Unity')
        self.assertEqual(prove_state.load_state(self.state_dir)['declarations']['target']['status'], 'solved')

    def test_failed_verification_never_solves_target(self):
        candidate = self.candidate()
        with patch.object(worktree, 'sync_candidate_to_main', return_value={
            'ok': False, 'error': 'changed statement', 'verification': {'passed': False},
        }):
            control.sync_to_main(candidate['candidate_id'], 'Unity')
        state = prove_state.load_state(self.state_dir)
        self.assertNotEqual(state['declarations']['target']['status'], 'solved')
        self.assertEqual(state['candidates'][candidate['candidate_id']]['status'], 'failed')

    def test_dirty_claim_and_foreign_candidate_are_rejected(self):
        strategy = control.register_strategy('Alice', 'direct proof', 'target')['strategy']
        (self.tree / 'scratch').write_text('preserve this')
        self.assertEqual(control.claim_strategy(strategy['strategy_id'], 'Alice')['status'], 'stale_worktree')
        self.assertEqual((self.tree / 'scratch').read_text(), 'preserve this')
        with self.assertRaises(ValueError):
            control.emit_candidate(strategy['strategy_id'], 'Bob', worktree.main_commit(self.root), 'target')

    def test_status_and_brief_do_not_expose_legacy_findings(self):
        with prove_state.transaction(self.state_dir) as state:
            state['findings'] = {'hidden': {'content': 'private discussion payload'}}
            state['events'].append({'event_id': 'old', 'kind': 'finding_published',
                                    'content': 'private discussion payload'})
        self.assertNotIn('private discussion payload', json.dumps(control.prove_status()))
        self.assertNotIn('private discussion payload', control.build_brief('Alice'))


    def test_status_redacts_discussion_without_mutating_state(self):
        strategy = control.register_strategy('Alice', 'allocation description', 'target')['strategy']
        sid = strategy['strategy_id']
        with prove_state.transaction(self.state_dir) as state:
            strategy = state['strategies'][sid]
            strategy.update(attempts=[{'reason': 'hidden attempt'}], assistants={'Bob': 'hidden assist'},
                            incorrect_reason='hidden reason', evidence='hidden evidence',
                            evidence_artifact_id='hidden artifact')
            state['candidates']['c1'] = {
                'candidate_id': 'c1', 'decl': 'target', 'commit_sha': 'abcdef012345',
                'status': 'merge_blocked', 'notes': 'hidden notes',
                'endorsements': ['hidden endorsement'], 'objections': ['hidden objection'],
                'merge_blocker': {'message': 'mechanical blocker'},
                'verification': {'reason': 'mechanical review reason'},
            }
            state['events'].extend([
                {'event_id': 'e1', 'kind': 'strategy_incorrect', 'decl': 'target', 'reason': 'hidden event'},
                {'event_id': 'e2', 'kind': 'candidate_objected', 'decl': 'target', 'content': 'hidden objection event'},
            ])
        before = prove_state.load_state(self.state_dir)
        results = [control.prove_status(), control.prove_status('target'), control.build_brief('Bob'),
                   control.register_strategy('Bob', 'allocation description', 'target'),
                   control.claim_strategy(sid, 'Alice')]
        # Claim mutates the state; compare read-only calls separately below.
        for value in results:
            rendered = json.dumps(value)
            for secret in ('hidden attempt', 'hidden assist', 'hidden reason', 'hidden evidence',
                           'hidden artifact', 'hidden notes', 'hidden endorsement', 'hidden objection',
                           'hidden event'):
                self.assertNotIn(secret, rendered)
        self.assertIn('allocation description', json.dumps(results[0]))
        self.assertIn('mechanical blocker', json.dumps(results[0]))
        self.assertIn('mechanical review reason', json.dumps(results[0]))
        after = prove_state.load_state(self.state_dir)
        self.assertEqual(before['candidates'], after['candidates'])
        self.assertEqual(before['strategies'][sid]['attempts'], after['strategies'][sid]['attempts'])
        snapshot = prove_state.load_state(self.state_dir)
        control.prove_status()
        control.prove_status('target')
        control.build_brief('Alice')
        self.assertEqual(snapshot, prove_state.load_state(self.state_dir))


if __name__ == '__main__':
    unittest.main()
