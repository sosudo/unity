"""Controller frontier evidence remains full in artifacts, bounded in state."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity import artifacts, bump_bootstrap as bootstrap, bump_contract as contract, bump_state as state
from tests import test_bump_json as json_fixture
from tests.test_bump_occurrence_contract import occurrence_contract, checked


class ControllerReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bump-controller-receipt-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = SimpleNamespace(project_root=self.root, forum=self.root / '.unity/forum/bump',
                                     artifacts=self.root / '.unity/artifacts')
        self.value = occurrence_contract()
        json_fixture.BumpVerificationProjectionTests().seed(self.paths.forum, self.value)

    def receipt(self, passed=False):
        value = checked(self.value)
        value.update(passed=passed, status='passed' if passed else 'failed',
            issues=[] if passed else ['ordinary compatibility failure'], source_sha256='f' * 64,
            extra={'preserved': ['all diagnostic fields']})
        return value

    def archive(self, receipt):
        return bootstrap._archive_controller_verification(self.paths, receipt)

    def payload(self, archived):
        payload = artifacts.artifact_bytes(self.root / '.unity/artifacts', archived['artifact_id'])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), archived['verification_artifact']['sha256'])
        return payload

    def test_full_immutable_compact_artifact_exactly_recovers_original_receipt(self):
        for passed in (False, True):
            with self.subTest(passed=passed):
                original = self.receipt(passed)
                before = deepcopy(original)
                archived = self.archive(original)
                payload = self.payload(archived)
                self.assertEqual(payload, json.dumps(original, sort_keys=True, separators=(',', ':')).encode())
                self.assertEqual(json.loads(payload)['proposed_contract'], self.value)
                self.assertEqual(original, before)

    def test_failed_diagnostic_projects_both_copies_only_after_cas(self):
        receipt = self.receipt()
        archived = self.archive(receipt)
        previous = state.load_state(self.paths.forum)
        self.assertTrue(state.record_migration_diagnostic(self.paths.forum, 'Fixture', archived,
            expected_revision=previous['revision']))
        current = state.load_state(self.paths.forum)
        task = current['formal_tasks']['Fixture']
        self.assertEqual(task['migration_diagnostics'], task['migration_check'])
        for key in ('migration_diagnostics', 'migration_check'):
            stored = task[key]
            self.assertNotIn('proposed_contract', stored)
            self.assertEqual(stored['source_sha256'], 'f' * 64)
            self.assertEqual(stored['issues'], receipt['issues'])
            self.assertEqual(stored['extra'], receipt['extra'])
            self.assertEqual(stored['proposed_contract_ref']['contract_sha256'], self.value['sha256'])
        self.assertEqual(current['formalization']['contract'], previous['formalization']['contract'])
        self.assertEqual(task['migration_attempts'], previous['formal_tasks']['Fixture']['migration_attempts'])
        self.assertEqual(json.loads(self.payload(archived)), receipt)

    def test_stale_diagnostic_cas_preserves_state_and_does_not_project(self):
        archived = self.archive(self.receipt())
        before = state.state_path(self.paths.forum).read_bytes()
        with patch.object(state, '_project_migration_verification') as projection:
            self.assertIs(state.record_migration_diagnostic(self.paths.forum, 'Fixture', archived,
                expected_revision=-1), False)
        projection.assert_not_called()
        self.assertEqual(state.state_path(self.paths.forum).read_bytes(), before)
        self.payload(archived)  # An orphaned immutable audit artifact is retained.

    def test_missing_malformed_or_different_proposal_is_not_hidden(self):
        for change in ('no_artifact', 'wrong_hash', 'wrong_id', 'changed_proposal', 'no_proposal'):
            with self.subTest(change=change):
                archived = self.archive(self.receipt())
                if change == 'no_artifact':
                    archived.pop('verification_artifact')
                elif change == 'wrong_hash':
                    archived['verification_artifact']['sha256'] = 'bad'
                elif change == 'wrong_id':
                    archived['verification_artifact']['artifact_id'] = 'artifact-' + '0' * 12
                elif change == 'changed_proposal':
                    archived['proposed_contract'] = {**self.value, 'inspection_policy': 3}
                else:
                    archived.pop('proposed_contract')
                state.record_migration_diagnostic(self.paths.forum, 'Fixture', archived)
                stored = state.load_state(self.paths.forum)['formal_tasks']['Fixture']['migration_diagnostics']
                self.assertEqual(stored, archived)
                self.assertNotIn('proposed_contract_ref', stored)

    def test_invalid_current_contract_digest_prevents_projection(self):
        with state.transaction(self.paths.forum) as current:
            current['formalization']['contract']['sha256'] = '0' * 64
        receipt = self.receipt()
        receipt['proposed_contract'] = state.load_state(self.paths.forum)['formalization']['contract']
        receipt['contract_sha256'] = '0' * 64
        archived = self.archive(receipt)
        state.record_migration_diagnostic(self.paths.forum, 'Fixture', archived)
        self.assertEqual(state.load_state(self.paths.forum)['formal_tasks']['Fixture']['migration_diagnostics'], archived)

    def test_null_contract_retains_full_diagnostic(self):
        archived = self.archive(self.receipt())
        with state.transaction(self.paths.forum) as current:
            current['formalization']['contract'] = None
        state.record_migration_diagnostic(self.paths.forum, 'Fixture', archived)
        self.assertEqual(state.load_state(self.paths.forum)['formal_tasks']['Fixture']['migration_diagnostics'], archived)

    def test_passed_controller_check_keeps_full_proposal_through_gate_then_projects(self):
        archived = self.archive(self.receipt(True))
        with patch.object(state, '_require_migration_receipt', wraps=state._require_migration_receipt) as gate:
            result = state.record_migration_module_check(self.paths.forum, 'Fixture', archived, main_sha='1' * 40)
        self.assertGreaterEqual(gate.call_count, 2)
        self.assertTrue(all(call.args[2]['proposed_contract'] == self.value for call in gate.call_args_list))
        self.assertNotIn('proposed_contract', result['candidate']['verification'])
        self.assertEqual(result['candidate']['status'], 'merged')
        self.assertEqual(json.loads(self.payload(archived))['proposed_contract'], self.value)

    def test_passed_stale_proposal_cannot_be_projected_to_pass(self):
        receipt = self.receipt(True)
        receipt['proposed_contract'] = contract._seal_contract({**self.value, 'inspection_policy': 3})
        archived = self.archive(receipt)
        with self.assertRaises(ValueError):
            state.record_migration_module_check(self.paths.forum, 'Fixture', archived, main_sha='1' * 40)
        current = state.load_state(self.paths.forum)
        self.assertEqual(current['formal_tasks']['Fixture']['status'], 'pending')
        self.assertFalse(any(row['status'] == 'merged' for row in current['formal_candidates'].values()))

    def test_archive_error_prevents_any_frontier_publication(self):
        with patch.object(bootstrap.bump_contract, 'source_identity', return_value={'source_sha256': 'f' * 64}), \
                patch.object(bootstrap.bump_worktree, 'main_commit', return_value='1' * 40), \
                patch.object(bootstrap.bump_contract, 'check_migration_module', return_value=self.receipt()), \
                patch.object(bootstrap.artifacts, 'store_text', side_effect=OSError('fixture disk failure')), \
                patch.object(state, 'record_migration_diagnostic') as diagnostic, \
                patch.object(state, 'record_migration_module_check') as accept:
            with self.assertRaisesRegex(OSError, 'fixture disk failure'):
                bootstrap._check_ready_modules_locked(self.paths)
        diagnostic.assert_not_called()
        accept.assert_not_called()

    def test_frontier_archives_before_both_publication_paths(self):
        for passed in (False, True):
            with self.subTest(passed=passed):
                def publication(forum, key, receipt, **kwargs):
                    self.assertEqual(json.loads(self.payload(receipt))['proposed_contract'], self.value)
                    self.assertIn('proposed_contract', receipt)
                    return False
                with patch.object(bootstrap.bump_contract, 'source_identity', return_value={'source_sha256': 'f' * 64}), \
                        patch.object(bootstrap.bump_worktree, 'main_commit', return_value='1' * 40), \
                        patch.object(bootstrap.bump_contract, 'check_migration_module', return_value=self.receipt(passed)), \
                        patch.object(state, 'record_migration_diagnostic', side_effect=publication) as diagnostic, \
                        patch.object(state, 'record_migration_module_check', side_effect=publication) as accept:
                    bootstrap._check_ready_modules_locked(self.paths)
                self.assertGreater((accept if passed else diagnostic).call_count, 0)

    def test_archive_rejects_inconsistent_record_hash(self):
        with patch.object(bootstrap.artifacts, 'store_text', return_value={
                'artifact_id': 'artifact-' + '1' * 12, 'sha256': '0' * 64}):
            with self.assertRaisesRegex(ValueError, 'inconsistent bytes'):
                self.archive(self.receipt())


class FrontierRoutingTests(unittest.TestCase):
    def setUp(self):
        self.current = {'revision': 7, 'formalization': {'contract': {'sha256': 'c' * 64}},
                        'formal_tasks': {}}
        self.paths = SimpleNamespace(forum=Path('unused'), project_root=Path('unused'))

    def task(self, dependencies=(), **fields):
        return {'status': 'pending', 'migration_module': 'Fixture',
                'dependencies': list(dependencies), **fields}

    def run_frontier(self, load=None, publish=None):
        def default_publish(forum, key, verification, **kwargs):
            self.current['formal_tasks'][key]['status'] = 'complete'
            self.current['revision'] += 1
            return {'candidate': {'status': 'merged'}}
        with patch.object(state, 'load_state', side_effect=load or (lambda _: deepcopy(self.current))) as reads, \
                patch.object(bootstrap.bump_contract, 'source_identity', return_value={'source_sha256': 'f' * 64}) as source, \
                patch.object(bootstrap.bump_worktree, 'main_commit', return_value='1' * 40), \
                patch.object(bootstrap.bump_contract, 'check_migration_module', return_value={'passed': True}) as check, \
                patch.object(bootstrap, '_archive_controller_verification', side_effect=lambda paths, value: value), \
                patch.object(state, 'record_migration_module_check', side_effect=publish or default_publish) as publication:
            result = bootstrap._check_ready_modules_locked(self.paths)
        return result, reads, source, check, publication

    def test_blocked_modules_do_not_each_reload_full_state(self):
        self.current['formal_tasks']['Root'] = self.task(migration_check={'source_sha256': 'f' * 64})
        for index in range(21):
            self.current['formal_tasks'][str(index)] = self.task(['Root'])
        _, reads, source, check, publication = self.run_frontier()
        self.assertEqual(reads.call_count, 3)  # routing, fresh eligible, final freshness
        source.assert_called_once()
        check.assert_not_called()
        publication.assert_not_called()

    def test_dependency_chain_becomes_eligible_after_own_success(self):
        self.current['formal_tasks'] = {'Root': self.task(), 'Child': self.task(['Root'])}
        result, _, _, check, publication = self.run_frontier()
        self.assertEqual([row.args[2] for row in check.call_args_list], ['Root', 'Child'])
        self.assertEqual([row.kwargs['expected_revision'] for row in publication.call_args_list], [7, 8])
        self.assertTrue(all(row['status'] == 'complete' for row in result['formal_tasks'].values()))

    def test_external_unblock_after_routing_is_not_missed(self):
        self.current['formal_tasks'] = {'Root': self.task(status='candidate_pending'),
                                        'Child': self.task(['Root'])}
        calls = 0
        def load(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.current['formal_tasks']['Root']['status'] = 'complete'
                self.current['revision'] += 1
            return deepcopy(self.current)
        _, _, _, check, publication = self.run_frontier(load=load)
        self.assertEqual([row.args[2] for row in check.call_args_list], ['Child'])
        self.assertEqual(publication.call_args.kwargs['expected_revision'], 8)

    def test_fresh_ineligible_state_blocks_native_inspection(self):
        self.current['formal_tasks'] = {'Root': self.task()}
        calls = 0
        def load(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.current['formal_tasks']['Root']['faithfulness'] = {'status': 'changes_requested'}
            return deepcopy(self.current)
        _, _, source, check, publication = self.run_frontier(load=load)
        source.assert_not_called()
        check.assert_not_called()
        publication.assert_not_called()


if __name__ == '__main__':
    unittest.main()
