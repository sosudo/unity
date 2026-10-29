"""Candidate progress requires positive reach past each resolved declaration."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from unity import bump_tasks, bump_verification

_TEST_LAKE = os.environ.get("UNITY_BUMP_TEST_LAKE", "")


class ProgressReachTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='bump-progress-reach-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.file = self.root / 'A.lean'

    def scan(self, text):
        self.file.write_text(text)
        return bump_tasks.scan_sources(self.root, ['A.lean'])

    def diagnostic(self, line, message):
        return {'id': message, 'path': 'A.lean', 'line': line, 'column': 1,
                'kind': 'declaration', 'severity': 'error', 'message': message}

    def run_progress(self, baseline, candidate, old, after, *, checked=()):
        before = self.scan(baseline)
        current = self.scan(candidate)
        chunks = list(before['chunks'])
        result = bump_verification.candidate_progress(
            {'diagnostics': old, 'checked_files': []},
            {'passed': False, 'source_stable': True, 'errors': after, 'checked_files': list(checked), 'unmapped': []},
            {'kind': 'repair', 'files': ['A.lean'], 'chunk_ids': chunks}, before,
            candidate_chunks=current['chunks'])
        return result, before, current

    def test_single_line_changed_message_is_not_progress(self):
        text = 'theorem a : False := by missing\n'
        result, before, _ = self.run_progress(text, text.replace('missing', 'other'),
                                             [self.diagnostic(1, 'old error')], [self.diagnostic(1, 'new error')])
        chunk = next(iter(before['chunks'].values()))
        self.assertEqual((chunk['start_line'], chunk['end_line']), (1, 1))
        self.assertFalse(result['accepted'])
        self.assertEqual(result['resolved_diagnostic_ids'], [])

    def test_later_error_does_not_hide_changed_error_inside_candidate(self):
        text = 'theorem a : False := by\n  missing\ntheorem b : False := by\n  broken\n'
        old = [self.diagnostic(2, 'old a'), self.diagnostic(4, 'old b')]
        after = [self.diagnostic(2, 'new a'), self.diagnostic(4, 'old b')]
        result, _, _ = self.run_progress(text, text.replace('missing', 'other'), old, after)
        self.assertFalse(result['accepted'])

    def test_expanded_candidate_range_with_changed_error_fails_closed(self):
        baseline = 'theorem a : False := by\n  missing\ntheorem b : False := by broken\n'
        candidate = 'theorem a : False := by\n  have x : True := True.intro\n  other\ntheorem b : False := by broken\n'
        old = [self.diagnostic(2, 'old a'), self.diagnostic(3, 'old b')]
        after = [self.diagnostic(3, 'new a'), self.diagnostic(4, 'old b')]
        result, before, current = self.run_progress(baseline, candidate, old, after)
        a_id = next(k for k, row in before['chunks'].items() if row['name'] == 'a')
        self.assertEqual(before['chunks'][a_id]['end_line'], 2)
        self.assertEqual(current['chunks'][a_id]['end_line'], 3)
        self.assertFalse(result['accepted'])

    def test_error_on_same_line_as_declaration_end_does_not_prove_completion(self):
        text = 'theorem a : False := by\n  missing\ntheorem b : False := by broken\n'
        old = [self.diagnostic(2, 'old a')]
        # A changed error at the final line remains within the owned declaration.
        result, _, _ = self.run_progress(text, text, old, [self.diagnostic(2, 'new a')])
        self.assertFalse(result['accepted'])

    def test_later_declaration_error_still_proves_partial_progress(self):
        text = 'theorem a : True := by missing\ntheorem b : False := by broken\n'
        old = [self.diagnostic(1, 'old a'), self.diagnostic(2, 'old b')]
        result, _, _ = self.run_progress(text, text.replace('missing', 'trivial'), old,
                                         [self.diagnostic(2, 'old b')])
        self.assertTrue(result['accepted'])
        self.assertEqual(result['resolved_diagnostic_ids'], ['old a'])

    def test_checked_file_with_same_file_error_cannot_prove_reach(self):
        baseline = 'def prefix := 1\n\n\n\ntheorem a : True := by\n  have x : True := True.intro\n  trivial\n  done\n'
        old = [self.diagnostic(6, 'old a')]
        # The candidate range is lines 5–8; a contradictory checked_files
        # entry cannot make an earlier error prove the later declaration ran.
        earlier, _, current = self.run_progress(baseline, baseline, old,
            [self.diagnostic(1, 'earlier failure')], checked=['A.lean'])
        chunk = next(c for c in current['chunks'].values() if c['name'] == 'a')
        self.assertEqual((chunk['start_line'], chunk['end_line']), (5, 8))
        self.assertFalse(earlier['accepted'])
        interior, _, _ = self.run_progress(baseline, baseline, old,
            [self.diagnostic(6, 'new a')], checked=['A.lean'])
        self.assertFalse(interior['accepted'])
        complete, _, _ = self.run_progress(baseline, baseline, old, [], checked=['A.lean'])
        self.assertTrue(complete['accepted'])
        later, _, _ = self.run_progress(baseline, baseline, old,
            [self.diagnostic(9, 'later failure')], checked=['A.lean'])
        self.assertTrue(later['accepted'])

    def test_overlapping_nested_or_same_line_ranges_fail_closed(self):
        text = 'def a := 0\n'
        old = self.diagnostic(1, 'old')
        chunks = {'outer': {'path': 'A.lean', 'name': 'outer', 'kind': 'def', 'start_line': 1, 'end_line': 2},
                  'inner': {'path': 'A.lean', 'name': 'inner', 'kind': 'def', 'start_line': 1, 'end_line': 1}}
        result = bump_verification.candidate_progress([old],
            {'passed': False, 'source_stable': True, 'errors': [self.diagnostic(3, 'later')], 'checked_files': [], 'unmapped': []},
            {'kind': 'repair', 'files': ['A.lean'], 'chunk_ids': ['outer', 'inner']},
            {'files': {'A.lean': {'imports': []}}, 'chunks': chunks}, candidate_chunks=chunks)
        self.assertFalse(result['accepted'])

    @unittest.skipUnless(bool(_TEST_LAKE) and Path(_TEST_LAKE).is_file(),
                         'set UNITY_BUMP_TEST_LAKE to a local Lean 4.34.1 Lake executable')
    def test_real_lean_changed_error_remains_rejected(self):
        lake = _TEST_LAKE
        (self.root / 'lakefile.toml').write_text('name = "Fixture"\n[[lean_lib]]\nname = "Fixture"\n')
        (self.root / 'lean-toolchain').write_text('leanprover/lean4:v4.34.1\n')
        (self.root / 'Fixture').mkdir()
        (self.root / 'Fixture/Base.lean').write_text('def fixtureBase : Nat := 40\n')
        target = self.root / 'Fixture/Target.lean'
        def compile(proof):
            target.write_text('import Fixture.Base\ntheorem fixtureClaim : fixtureBase = 0 := by ' + proof + '\n')
            inventory = bump_tasks.scan_sources(self.root, ['Fixture/Target.lean'])
            result = subprocess.run([lake, '--rehash', 'build', '+Fixture.Target'], cwd=self.root,
                                    text=True, capture_output=True, check=False)
            diagnostics = bump_verification._parse_diagnostics(result.stdout + '\n' + result.stderr, self.root)
            return result.returncode, inventory, [d for d in diagnostics if d['severity'] == 'error']
        before_code, before, old = compile('exact rfl')
        after_code, current, errors = compile('decide')
        self.assertEqual((before_code, after_code), (1, 1))
        self.assertNotEqual(old[0]['message'], errors[0]['message'])
        cid = next(iter(before['chunks']))
        self.assertEqual((before['chunks'][cid]['start_line'], before['chunks'][cid]['end_line']), (2, 2))
        result = bump_verification.candidate_progress({'diagnostics': old, 'checked_files': []},
            {'passed': False, 'source_stable': True, 'errors': errors, 'checked_files': [], 'unmapped': []},
            {'kind': 'repair', 'files': ['Fixture/Target.lean'], 'chunk_ids': [cid]}, before,
            candidate_chunks=current['chunks'])
        self.assertFalse(result['accepted'], json.dumps(result))
