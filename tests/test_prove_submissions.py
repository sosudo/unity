import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import tempfile
import unittest

from unity import prove_state, worktree
from unity.prove_submissions import NoToolSubmissions


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def make_setup(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.email', 'test@example.com')
    git(root, 'config', 'user.name', 'Test')
    (root / 'Main.lean').write_text('theorem foo : True := by sorry\n')
    git(root, 'add', 'Main.lean')
    git(root, 'commit', '-m', 'initial')
    worktree.ensure_git_excludes(root, ('.worktrees/', '.unity/'))
    trees = {a: worktree.create_worktree(a, root) for a in ('alice', 'bob')}
    paths = SimpleNamespace(project_root=root, unity=root / '.unity', forum=root / '.unity' / 'forum')
    paths.unity.mkdir(exist_ok=True)
    dag = paths.unity / 'dag.json'
    dag.write_text(json.dumps({'chunks': [{'id': 'foo', 'lean_decl': 'Foo'}, {'id': 'bar', 'lean_decl': 'Bar'}]}))
    prove_state.initialize_from_dag(paths.forum, dag, worktree.main_commit(root))
    return paths, trees


def commit(tree, subject, content):
    (tree / 'Main.lean').write_text(content)
    git(tree, 'add', 'Main.lean')
    git(tree, 'commit', '-m', subject)
    return git(tree, 'rev-parse', 'HEAD')


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths, self.trees = make_setup(Path(self.temp.name))

    def test_marker_and_restart(self):
        paths, trees = self.paths, self.trees
        adapter = NoToolSubmissions(paths, trees)
        adapter.prepare('alice', 'Foo')
        commit(trees['alice'], 'partial progress', 'partial\n')
        assert adapter.poll({'alice': 'Foo'}) == []
        commit(trees['alice'], 'prove-candidate: Foo', 'proof\n')
        # The merger applies a single commit: an incremental last commit is incomplete.
        self.assertEqual(adapter.poll({'alice': 'Foo'}), [])
        git(trees['alice'], 'reset', '--soft', worktree.main_commit(paths.project_root))
        git(trees['alice'], 'commit', '-m', 'prove-candidate: Foo')
        sha = git(trees['alice'], 'rev-parse', 'HEAD')
        result = adapter.poll({'alice': 'Foo'})
        assert len(result) == 1 and result[0]['commit_sha'] == sha
        assert result[0]['status'] == 'submitted'
        assert adapter.poll({'alice': 'Foo'}) == []
        assert NoToolSubmissions(paths, trees).poll({'alice': 'Foo'}) == []
        assert len(prove_state.load_state(paths.forum)['candidates']) == 1


    def test_wrong_target_and_stale_ownership(self):
        paths, trees = self.paths, self.trees
        adapter = NoToolSubmissions(paths, trees)
        reservation = adapter.prepare('alice', 'Foo')
        commit(trees['alice'], 'prove-candidate: Bar', 'wrong\n')
        assert adapter.poll({'alice': 'Foo', 'bob': 'Bar'}) == []
        commit(trees['alice'], 'prove-candidate: Foo', 'proof\n')
        git(trees['alice'], 'reset', '--soft', worktree.main_commit(paths.project_root))
        git(trees['alice'], 'commit', '-m', 'prove-candidate: Foo')
        with prove_state.transaction(paths.forum) as state:
            state['strategies'][reservation['strategy_id']]['owner'] = 'bob'
        assert adapter.poll({'alice': 'Foo'}) == []


    def test_resume_preserves_progress_and_fresh_dirty_is_rejected(self):
        paths, trees = self.paths, self.trees
        adapter = NoToolSubmissions(paths, trees)
        reservation = adapter.prepare('alice', 'Foo')
        sha = commit(trees['alice'], 'partial', 'committed\n')
        (trees['alice'] / 'Main.lean').write_text('dirty\n')
        resumed = NoToolSubmissions(paths, trees).prepare('alice', 'Foo')
        assert resumed['strategy_id'] == reservation['strategy_id']
        assert git(trees['alice'], 'rev-parse', 'HEAD') == sha
        assert (trees['alice'] / 'Main.lean').read_text() == 'dirty\n'
        (trees['bob'] / 'Main.lean').write_text('dirty\n')
        with self.assertRaisesRegex(ValueError, 'clean'):
            adapter.prepare('bob', 'Foo')


    def test_independent_reservations_and_dependencies(self):
        paths, trees = self.paths, self.trees
        adapter = NoToolSubmissions(paths, trees)
        with prove_state.transaction(paths.forum) as state:
            state['declarations']['Bar']['dependencies'] = ['foo']
        with self.assertRaisesRegex(ValueError, 'not ready'):
            adapter.prepare('alice', 'Bar')
        alice = adapter.prepare('alice', 'Foo')
        bob = adapter.prepare('bob', 'Foo')
        assert alice['strategy_id'] != bob['strategy_id']
        with prove_state.transaction(paths.forum) as state:
            state['declarations']['Foo']['status'] = 'solved'
        assert adapter.poll({'alice': 'Foo'}) == []
        assert adapter.prepare('alice', 'Bar')['decl'] == 'Bar'

    def test_main_move_rejects_resume_and_submission(self):
        paths, trees = self.paths, self.trees
        adapter = NoToolSubmissions(paths, trees)
        adapter.prepare('alice', 'Foo')
        commit(trees['alice'], 'prove-candidate: Foo', 'candidate\n')
        (trees['alice'] / 'Main.lean').write_text('preserved dirty work\n')
        commit(paths.project_root, 'external change', 'new main\n')
        with prove_state.transaction(paths.forum) as state:
            state['main_sha'] = worktree.main_commit(paths.project_root)
        with self.assertRaisesRegex(ValueError, 'clean'):
            adapter.prepare('alice', 'Foo')
        self.assertEqual(adapter.poll({'alice': 'Foo'}), [])
        self.assertEqual((trees['alice'] / 'Main.lean').read_text(), 'preserved dirty work\n')

if __name__ == '__main__':
    unittest.main()
