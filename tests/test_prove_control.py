"""Core proof controls preserve candidate checks without discussion posts."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import prove_engine as control, prove_state, worktree


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
        strategy = prove_state.register_strategy(self.state_dir, 'Alice', 'direct proof', 'target')['strategy']
        self.assertEqual(prove_state.claim_strategy(self.state_dir, strategy['strategy_id'], 'Alice', worktree.main_commit(self.root))['status'], 'claimed')
        (self.tree / 'Main.lean').write_text('theorem target : True := by trivial\n')
        self.git('add', 'Main.lean', cwd=self.tree)
        self.git('commit', '-m', 'proof', cwd=self.tree)
        sha = self.git('rev-parse', 'HEAD', cwd=self.tree)
        result = prove_state.emit_candidate(self.state_dir, strategy['strategy_id'], 'Alice', worktree.verify_candidate_commit(self.root, 'Alice', sha), 'target')
        self.assertNotIn('forum_post_id', result)
        self.assertEqual(result['candidate']['commit_sha'], sha)
        return result['candidate']

    def test_candidate_uses_original_mechanical_verifier_before_becoming_solved(self):
        candidate = self.candidate()
        self.assertNotEqual(prove_state.load_state(self.state_dir)['declarations']['target']['status'], 'solved')
        original_run = subprocess.run
        def fake_build(args, **kwargs):
            if args == ['lake', 'build']:
                return subprocess.CompletedProcess(args, 0, 'build passed', '')
            return original_run(args, **kwargs)
        with patch.object(worktree.subprocess, 'run', side_effect=fake_build), \
             patch.object(worktree.candidate_review, 'review_candidate', return_value={
                 'status': 'passed', 'issues': [], 'mode': 'kernel'}):
            result = control.sync_to_main(candidate['candidate_id'], 'Unity')
            self.assertTrue(result['sync']['ok'], result)
            self.assertEqual(control.sync_to_main(candidate['candidate_id'], 'Unity')['idempotent'], True)
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}'),
                         self.git('rev-parse', candidate['commit_sha'] + '^{tree}'))
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

    def test_foreign_candidate_is_rejected(self):
        with self.assertRaises(ValueError):
            worktree.verify_candidate_commit(self.root, 'Bob', worktree.main_commit(self.root))

    def test_snapshot_and_brief_do_not_expose_legacy_findings(self):
        with prove_state.transaction(self.state_dir) as state:
            state['findings'] = {'hidden': {'content': 'private discussion payload'}}
            state['events'].append({'event_id': 'old', 'kind': 'finding_published',
                                    'content': 'private discussion payload'})
        self.assertNotIn('private discussion payload', json.dumps(control.status_snapshot()))
        self.assertNotIn('private discussion payload', control.build_brief('Alice'))


if __name__ == '__main__':
    unittest.main()
