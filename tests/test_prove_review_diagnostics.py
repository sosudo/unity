import unittest
from pathlib import Path
from unittest.mock import patch

from unity import candidate_review, prove_contract, prove_source_scan


class ProveReviewDiagnosticTests(unittest.TestCase):
    def review(self, *, result=None, error=None):
        declaration = {
            'decl': 'Fixture.target', 'file': 'Fixture.lean', 'title': 'target',
            'kernel_type_repr': 'True', 'type_contract': {'sha256': 'frozen-contract'},
        }
        source = {'kind': 'theorem', 'status': 'proven'}
        with patch.object(prove_source_scan, 'scan_git_sources', return_value=[]), \
             patch.object(candidate_review, '_source_declaration', return_value=source), \
             patch.object(prove_contract, 'inspect', return_value=result, side_effect=error):
            return candidate_review.review_candidate(
                Path('/unused-diagnostic-fixture'), {'candidate_id': 'candidate'},
                declaration, base_main_sha='baseline', candidate_diff='',
            )

    def kernel(self):
        return {'Fixture.target': {
            'kind': 'theorem', 'sorried': False, 'deps': [], 'type_repr': 'True',
        }}

    def test_missing_inspection_is_not_reported_as_a_type_mismatch(self):
        review = self.review(error=ValueError(
            "Lean prove contract inspector failed: object file 'Extra/Orphan.olean' does not exist"
        ))
        self.assertEqual(review['status'], 'failed')
        self.assertEqual(review['mode'], 'kernel_unavailable')
        self.assertFalse(review['signature_check_available'])
        self.assertFalse(review['signature_unchanged'])
        self.assertEqual(review['actual_type_sha256'], '')
        self.assertTrue(any('inspection failed' in issue for issue in review['issues']))
        self.assertFalse(any('type differs' in issue for issue in review['issues']))

    def test_completed_negative_kernel_check_reports_a_type_mismatch(self):
        review = self.review(result=(self.kernel(), {'passed': False}))
        self.assertEqual(review['status'], 'failed')
        self.assertEqual(review['mode'], 'kernel')
        self.assertTrue(review['signature_check_available'])
        self.assertFalse(review['signature_unchanged'])
        self.assertNotEqual(review['actual_type_sha256'], '')
        self.assertIn('target declaration type differs from the original prove target',
                      review['issues'])

    def test_contract_decode_error_is_not_reported_as_a_type_mismatch(self):
        review = self.review(result=(self.kernel(), {
            'passed': False, 'error': 'malformed frozen expression',
        }))
        self.assertEqual(review['status'], 'failed')
        self.assertFalse(review['signature_check_available'])
        self.assertFalse(review['signature_unchanged'])
        self.assertIn('target declaration contract check failed: malformed frozen expression',
                      review['issues'])
        self.assertFalse(any('type differs' in issue for issue in review['issues']))


if __name__ == '__main__':
    unittest.main()
