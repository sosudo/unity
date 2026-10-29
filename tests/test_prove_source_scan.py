import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import candidate_review, prove_source_scan as scan


class LeanLexicalTests(unittest.TestCase):
    def test_50_documentation_hits_are_not_code(self):
        words = ['sorry'] * 21 + ['axiom'] * 18 + ['admit'] * 11
        new = '/-!\n' + '\n'.join(f'No `{word}` is introduced.' for word in words) + '\n-/\n'
        self.assertEqual(len(re.findall(r'\b(?:sorry|axiom|admit)\b', new)), 50)
        self.assertEqual(scan.newly_forbidden('', new), set())

    def test_real_forbidden_tokens_and_quotation_are_visible(self):
        for token in ('sorry', 'admit', 'axiom', 'native_decide'):
            with self.subTest(token=token):
                self.assertEqual(scan.newly_forbidden('', f'example : True := by {token}\n'), {token})
        self.assertEqual(scan.newly_forbidden('', 'def q := `(term| by sorry)\n'), {'sorry'})

    def test_lean_identifier_suffixes_do_not_match_keywords(self):
        source = "def sorry' := 1\ndef native_decide' := 1\ndef sorry! := 1\ndef sorry? := 1\ndef sorry₂ := 1\ndef sorry.foo := 1\ndef Foo.native_decide := 1\n"
        self.assertEqual(scan.newly_forbidden('', source), set())
        # Lambda is not Lean identifier-rest, despite being a Python Unicode word character.
        self.assertEqual(scan.newly_forbidden('', 'example : True := by sorryλ\n'), {'sorry'})
        self.assertEqual(scan.newly_forbidden('', 'example : True := by sorry.1\n'), {'sorry'})

    def test_line_comment_endings_match_lean_lexer(self):
        self.assertEqual(scan.newly_forbidden('', '-- comment\rtheorem T : True := by sorry\n'), set())
        self.assertEqual(scan.newly_forbidden('', '-- comment\r\ntheorem T : True := by sorry\n'), {'sorry'})
        self.assertEqual(scan.newly_forbidden('', '-- comment\ntheorem T : True := by sorry\n'), {'sorry'})

    def test_line_endings_and_identifiers_against_installed_lean(self):
        lean = Path.home() / '.elan/toolchains/leanprover--lean4---v4.28.0/bin/lean'
        if not lean.is_file():
            self.skipTest('Lean 4.28 toolchain is not installed')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'Main.lean'
            for ending, expected in ((b'\r', False), (b'\r\n', True), (b'\n', True)):
                path.write_bytes(b'-- comment' + ending + b'theorem T : True := by sorry\n')
                result = subprocess.run([str(lean), '--root=' + temp, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual('declaration uses `sorry`' in result.stdout, expected)
            path.write_text("def sorry' : Nat := 1\ndef native_decide' : Nat := 1\ndef sorry! : Nat := 1\ndef sorry? : Nat := 1\ndef sorry₂ : Nat := 1\ndef sorry.foo : Nat := 1\ndef Foo.native_decide : Nat := 1\n")
            result = subprocess.run([str(lean), '--root=' + temp, str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nested_comments_strings_chars_raw_and_escaped_identifiers(self):
        text = '''/- sorry /- nested axiom -/ admit -/
-- sorry is a line comment
 def a := "the -- is text and sorry is text"
 def b := r###"axiom /- -- native_decide"###
 def c := '\"'
 def «sorry» := 1
'''
        self.assertEqual(scan.newly_forbidden('', text), set())
        self.assertEqual(scan.newly_forbidden('', 'def a := "-- not a comment"; example : True := by sorry\n'), {'sorry'})

    def test_interpolated_expressions_are_code_and_literal_text_is_not(self):
        self.assertEqual(scan.newly_forbidden('', 'def a := s!"literal sorry {by admit}"\n'), {'admit'})
        self.assertEqual(scan.newly_forbidden('', 'def a := m!"{by native_decide}"\n'), {'native_decide'})
        self.assertEqual(scan.newly_forbidden('', 'def a := println! "value {by sorry}"\n'), {'sorry'})
        self.assertEqual(scan.newly_forbidden('', 'dbg_trace "value {by admit}"\n'), {'admit'})
        self.assertEqual(scan.newly_forbidden('', 'def a := s!"outer {s!\"inner {by sorry}\"}"\n'), {'sorry'})
        self.assertEqual(scan.newly_forbidden('', 'def a := s!"escaped \\{sorry} literal"\n'), set())
        with self.assertRaisesRegex(scan.SourceScanError, 'ambiguous interpolated'):
            scan.newly_forbidden('', 'customParser "{by sorry}"\n')
        with self.assertRaisesRegex(scan.SourceScanError, 'ambiguous interpolated'):
            scan.newly_forbidden('', 'def possibly_plain := "{sorry}"\n')
        with self.assertRaisesRegex(scan.SourceScanError, 'ambiguous interpolated'):
            scan.newly_forbidden('', 'customParser "\\\\{x} {by sorry}"\n')
        with self.assertRaisesRegex(scan.SourceScanError, 'ambiguous interpolated'):
            scan.newly_forbidden('', 'customParser "{foo {x := 1} (by sorry)}"\n')

    def test_comment_boundary_edits_expose_unchanged_lines(self):
        old = '/-\nintro\nsorry\n-/\n'
        for new in ('-- removed start\nintro\nsorry\n-- removed end\n',
                    '/- closed early -/\nintro\nsorry\n-- removed end\n'):
            with self.subTest(new=new):
                self.assertEqual(scan.newly_forbidden(old, new), {'sorry'})
        self.assertEqual(scan.newly_forbidden('-- existing\nsorry\n', '/- hidden\nsorry\n-/\n'), set())

    def test_existing_holes_unchanged_are_not_new(self):
        old = 'theorem A : True := by sorry\n' + ''.join(f'-- note {i}\n' for i in range(2000))
        new = '-- new header\n' + old + '-- new footer\n'
        self.assertEqual(scan.newly_forbidden(old, new), set())
        self.assertEqual(scan.newly_forbidden(old, new + 'theorem B : True := by admit\n'), {'admit'})
        repeated = 'example : True := by sorry\n' * 10000
        self.assertEqual(scan.newly_forbidden(repeated, '-- header\n' + repeated + '-- footer\n'), set())

    def test_unterminated_and_unsupported_input_fails_closed(self):
        for source in ('/- unterminated', 'def a := "unterminated', 'def «name := 1',
                       'def a := r##"unterminated', 'def a := s!"{by sorry"', "def a := '\\uZZZZ'"):
            with self.subTest(source=source), self.assertRaises(scan.SourceScanError):
                scan.newly_forbidden('', source)


class IndexedGitTests(unittest.TestCase):
    def test_staged_source_only_and_nonlean_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()
            git('init', '-b', 'main'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
            path = root / 'Main.lean'; path.write_text('theorem Old : True := by sorry\n')
            git('add', 'Main.lean'); git('commit', '-m', 'initial'); base = git('rev-parse', 'HEAD')
            path.write_text('/- no axiom or admit here -/\ntheorem Old : True := by sorry\n')
            (root / 'LICENSE').write_text('sorry admit axiom native_decide\n')
            git('add', 'Main.lean', 'LICENSE')
            self.assertEqual(scan.scan_git_sources(root, base), [])
            path.write_text(path.read_text() + 'theorem New : True := by native_decide\n')
            git('add', 'Main.lean')
            self.assertEqual(scan.scan_git_sources(root, base), ['native_decide'])

    def test_symlinked_lean_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args): return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()
            git('init', '-b', 'main'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
            (root / 'README').write_text('fixture'); git('add', 'README'); git('commit', '-m', 'initial')
            base = git('rev-parse', 'HEAD')
            (root / 'Alias.lean').symlink_to('README'); git('add', 'Alias.lean')
            with self.assertRaisesRegex(scan.SourceScanError, 'not a regular Git file'):
                scan.scan_git_sources(root, base)

    def test_candidate_review_rejects_executable_and_unclassifiable_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args): return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()
            git('init', '-b', 'main'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
            path = root / 'Main.lean'; path.write_text('theorem T : True := by trivial\n')
            git('add', 'Main.lean'); git('commit', '-m', 'initial'); base = git('rev-parse', 'HEAD')
            declaration = {'decl': 'T', 'file': 'Main.lean', 'type_contract': {}, 'kernel_type_repr': 'True'}
            kernel = {'T': {'kind': 'theorem', 'sorried': False, 'deps': [], 'type_repr': 'True'}}
            with patch.object(candidate_review, '_source_declaration', return_value={'kind': 'theorem'}), \
                 patch.object(candidate_review.prove_contract, 'inspect', return_value=(kernel, {'passed': True})):
                path.write_text('theorem T : True := by sorry\n'); git('add', 'Main.lean')
                result = candidate_review.review_candidate(root, {}, declaration, base_main_sha=base, candidate_diff='')
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['forbidden_constructs'], ['sorry'])
                path.write_text('/- unterminated\n'); git('add', 'Main.lean')
                result = candidate_review.review_candidate(root, {}, declaration, base_main_sha=base, candidate_diff='')
                self.assertEqual(result['status'], 'failed')
                self.assertTrue(any('source safety scan failed' in issue for issue in result['issues']))


if __name__ == '__main__':
    unittest.main()
