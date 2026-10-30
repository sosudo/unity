import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import worktree


class ProveCandidateCleanlinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unity-prove-cleanliness-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'Main.lean'
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Prove Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.baseline_source = 'theorem target : True := by sorry\n'
        self.source.write_text(self.baseline_source)
        self.git('add', 'Main.lean')
        self.git('commit', '-m', 'baseline')
        self.baseline = self.git('rev-parse', 'HEAD')
        self.git('switch', '-c', 'candidate')
        self.candidate_source = 'theorem target : True := by trivial\n'
        self.source.write_text(self.candidate_source)
        self.git('add', 'Main.lean')
        self.git('commit', '-m', 'candidate')
        self.candidate_sha = self.git('rev-parse', 'HEAD')
        self.git('switch', 'main')

    def git(self, *args):
        return subprocess.check_output(
            ['git', *args], cwd=self.root, text=True, stderr=subprocess.DEVNULL,
        ).strip()

    def merge_with_review(self, mutation=None):
        original_run = subprocess.run

        def run(args, **kwargs):
            if args == ['lake', 'build']:
                return subprocess.CompletedProcess(args, 0, 'fixture build passed\n', '')
            return original_run(args, **kwargs)

        def review(*args, **kwargs):
            if mutation is not None:
                mutation()
            return {'status': 'passed', 'issues': [], 'mode': 'kernel'}

        with patch.object(worktree.subprocess, 'run', side_effect=run), \
             patch.object(worktree.candidate_review, 'review_candidate', side_effect=review):
            return worktree.sync_candidate_to_main(
                self.root,
                {'candidate_id': 'fixture', 'commit_sha': self.candidate_sha, 'decl': 'target'},
                {'decl': 'target'},
            )

    def assert_rolled_back(self, result):
        self.assertFalse(result['ok'])
        self.assertEqual(result['verification']['status'], 'failed')
        self.assertIn('Prove contract inspection changed tracked files or the candidate index',
                      result['verification']['issues'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.baseline)
        self.assertEqual(self.source.read_text(), self.baseline_source)
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=no'), '')

    def test_unstaged_tracked_change_during_review_rolls_back(self):
        result = self.merge_with_review(
            lambda: self.source.write_text(self.candidate_source + '-- build edit\n')
        )
        self.assert_rolled_back(result)

    def test_staged_change_during_review_rolls_back(self):
        def mutate():
            self.source.write_text(self.candidate_source + '-- staged build edit\n')
            self.git('add', 'Main.lean')

        self.assert_rolled_back(self.merge_with_review(mutate))

    def test_unchanged_review_commits_exact_candidate_tree(self):
        result = self.merge_with_review()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['verification']['status'], 'passed')
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}'),
                         self.git('rev-parse', self.candidate_sha + '^{tree}'))
        self.assertEqual(self.source.read_text(), self.candidate_source)
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=no'), '')


if __name__ == '__main__':
    unittest.main()
