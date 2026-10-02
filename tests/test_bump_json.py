"""Lossless storage codec and unchanged transaction semantics, without services."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import artifacts, bump_json, bump_state
from tests.test_bump_occurrence_contract import occurrence_contract, checked
from unity import bump_contract


class BumpJsonTests(unittest.TestCase):
    def encoded(self, value):
        return "".join(bump_json.iterencode(value))

    def test_exact_standard_compact_bytes_across_chunk_budgets(self):
        values = [None, True, False, 0, -0.0, 1.0, 1, 1e30, float('inf'), float('-inf'), float('nan'),
                  '\u03bb\n"\\\ud800', {'b': [1, {'x': 'λ'}], 'a': (False, None)},
                  {1: 'int', 2: 'another'}, {False: 'bool', True: 'other'}, {None: 'null'},
                  {1.5: ['float-key']}, {'same': [[['app', ['bvar', x]]] for x in range(5000)]}]
        for budget in (1, 7, 65_536):
            with patch.object(bump_json, '_CHUNK_CHARACTERS', budget):
                for value in values:
                    with self.subTest(budget=budget, value_type=type(value).__name__):
                        expected = json.dumps(value, sort_keys=True, separators=(',', ':'))
                        self.assertEqual(self.encoded(value), expected)
                        self.assertEqual(bump_json.mutation_digest(value), hashlib.sha256(expected.encode()).digest())

    def test_unsupported_and_mixed_key_errors_preserved(self):
        for value in ({'a': 1, 2: 3}, {object(): None}, [object()], {('tuple',): 1}):
            with patch.object(bump_json, '_CHUNK_CHARACTERS', 1):
                with self.assertRaises((TypeError, ValueError)):
                    self.encoded(value)

    def test_cycles_fail_without_suppressing_shared_values(self):
        repeated = ['leaf']
        self.assertEqual(self.encoded([repeated, repeated]), '[["leaf"],["leaf"]]')
        cycle = []
        cycle.append(cycle)
        dictionary = {}
        dictionary['loop'] = dictionary
        for value in (cycle, dictionary):
            with self.assertRaisesRegex(ValueError, 'Circular reference'):
                self.encoded(value)

    def test_large_compact_text_is_yielded_in_bounded_slices(self):
        value = {'reports': [{'meaning': ['app', ['bvar', i], ['bvar', i + 1]]} for i in range(10000)]}
        chunks = list(bump_json.iterencode(value))
        self.assertEqual(''.join(chunks), json.dumps(value, sort_keys=True, separators=(',', ':')))
        self.assertLess(max(map(len, chunks)), 100000)
        self.assertGreater(len(chunks), 1)

    def test_atomic_dump_roundtrip_and_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested/state.json'
            value = {'lambda': 'λ', 'deep': [['x'] * 200 for _ in range(100)]}
            bump_json.atomic_dump(path, value)
            self.assertEqual(path.read_text(), json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n')
            self.assertEqual(json.loads(path.read_bytes()), value)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_serialization_failure_preserves_destination_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_bytes(b'preserved')
            with self.assertRaises(TypeError):
                bump_json.atomic_dump(path, {'bad': object()})
            self.assertEqual(path.read_bytes(), b'preserved')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_replace_failure_preserves_destination_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_bytes(b'preserved')
            with patch.object(Path, 'replace', side_effect=OSError('isolated failure')):
                with self.assertRaises(OSError):
                    bump_json.atomic_dump(path, {'valid': True})
            self.assertEqual(path.read_bytes(), b'preserved')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_transactions_preserve_revision_and_json_visible_numeric_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            forum = Path(directory)
            with bump_state.transaction(forum) as state:
                state['fixture'] = {'a': 1, 'b': ['λ']}
            first = bump_state.load_state(forum)
            self.assertEqual(first['revision'], 1)
            data = bump_state.state_path(forum).read_bytes()
            with bump_state.transaction(forum) as state:
                state['fixture'] = {'b': ['λ'], 'a': 1}
            self.assertEqual(bump_state.state_path(forum).read_bytes(), data)
            with bump_state.transaction(forum) as state:
                state['fixture']['a'] = 1.0
            self.assertEqual(bump_state.load_state(forum)['revision'], 2)
            with bump_state.transaction(forum) as state:
                state['fixture']['b'][0] = 'changed'
            self.assertEqual(bump_state.load_state(forum)['revision'], 3)

    def test_transaction_exception_preserves_original_bytes_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            forum = Path(directory)
            with bump_state.transaction(forum) as state:
                state['fixture'] = 1
            before = bump_state.state_path(forum).read_bytes()
            with self.assertRaises(RuntimeError):
                with bump_state.transaction(forum) as state:
                    state['fixture'] = 2
                    raise RuntimeError('do not publish')
            self.assertEqual(bump_state.state_path(forum).read_bytes(), before)
            with bump_state.transaction(forum) as state:
                state['fixture'] = 3
            self.assertEqual(bump_state.load_state(forum)['fixture'], 3)

    def test_legacy_pretty_state_reads_without_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            forum = Path(directory)
            state = bump_state._default_state()
            state['fixture'] = {'legacy': True}
            path = bump_state.state_path(forum)
            path.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
            before = path.read_bytes()
            with bump_state.transaction(forum):
                pass
            self.assertEqual(path.read_bytes(), before)


class BumpVerificationProjectionTests(unittest.TestCase):
    def artifact_verification(self, directory, value):
        verification = {**checked(value), 'proposed_contract': deepcopy(value), 'extra': ['retained']}
        payload = json.dumps(verification, sort_keys=True, separators=(',', ':'))
        record = artifacts.store_text(Path(directory) / 'artifacts', payload,
                                      kind='bump_formal_verification', producer='Unity')
        verification['artifact_id'] = record['artifact_id']
        verification['verification_artifact'] = {
            'artifact_id': record['artifact_id'], 'sha256': record['sha256']}
        return verification

    def seed(self, forum, value):
        with bump_state.transaction(forum) as current:
            current.update(phase='formalizing', run_id='bump-storage-fixture',
                input_source={'kind': 'supplied_sources', 'candidate_id': 'original', 'sha256': 'c' * 64},
                project_baseline=value['project_baseline'])
            current['formalization'].update(contract=value, revision=1, main_sha='1' * 40,
                solution_candidate='original', solution_sha256='c' * 64)
            current['formal_tasks'] = {key: {'task_id': key, 'revision': 1,
                'status': 'pending', 'outputs': [], 'dependencies': row['imports']}
                for key, row in value['project_baseline']['compiler_modules'].items()}
        bump_state.seed_migration_tasks(forum, value['project_baseline']['compiler_modules'],
                                        value['project_baseline']['original_reports'])

    def test_merge_projects_exact_proposal_but_full_artifact_recovers_it(self):
        value = occurrence_contract()
        with tempfile.TemporaryDirectory() as directory:
            forum = Path(directory) / 'forum'
            self.seed(forum, value)
            verification = self.artifact_verification(directory, value)
            result = bump_state.record_migration_module_check(forum, 'Fixture', verification,
                                                              main_sha='1' * 40)
            stored = result['candidate']['verification']
            self.assertNotIn('proposed_contract', stored)
            self.assertEqual(stored['extra'], ['retained'])
            self.assertEqual(stored['proposed_contract_ref'], {
                'contract_sha256': value['sha256'], 'artifact_id': verification['artifact_id'],
                'artifact_sha256': verification['verification_artifact']['sha256']})
            payload = artifacts.artifact_bytes(Path(directory) / 'artifacts', verification['artifact_id'])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), stored['proposed_contract_ref']['artifact_sha256'])
            self.assertEqual(json.loads(payload)['proposed_contract'], value)
            self.assertEqual(verification['proposed_contract'], value)  # Caller is not mutated.
            verification['proposed_contract']['targets'].clear()
            self.assertEqual(bump_state.load_state(forum)['formalization']['contract'], value)
            bump_contract.validate_migration_state(bump_state.load_state(forum))

    def test_stale_or_injected_proposal_rejected_before_projection(self):
        value = occurrence_contract()
        with tempfile.TemporaryDirectory() as directory:
            forum = Path(directory) / 'forum'
            self.seed(forum, value)
            for mutate in (lambda c: c.update(solution_sha256='d' * 64),
                           lambda c: c['targets'].clear()):
                verification = self.artifact_verification(directory, value)
                mutate(verification['proposed_contract'])
                verification['proposed_contract'] = bump_contract._seal_contract(verification['proposed_contract'])
                with patch.object(bump_state, '_project_migration_verification') as projection:
                    with self.assertRaises(ValueError):
                        bump_state.record_migration_module_check(forum, 'Fixture', verification,
                                                                 main_sha='1' * 40)
                    projection.assert_not_called()
                self.assertEqual(bump_state.load_state(forum)['formalization']['contract'], value)

    def test_missing_invalid_or_mismatched_artifact_keeps_original_evidence(self):
        value = occurrence_contract()
        with tempfile.TemporaryDirectory() as directory:
            original = self.artifact_verification(directory, value)
            changes = [lambda v: v.pop('verification_artifact'),
                       lambda v: v['verification_artifact'].update(sha256='bad'),
                       lambda v: v['verification_artifact'].update(artifact_id='artifact-' + 'a' * 12),
                       lambda v: v['verification_artifact'].update(artifact_id='../unsafe'),
                       lambda v: v['verification_artifact'].update(unexpected=True),
                       lambda v: v.update(proposed_contract_ref={'unexpected': 'preserve'}),
                       lambda v: v.update(contract_sha256='f' * 64)]
            for mutate in changes:
                verification = deepcopy(original)
                mutate(verification)
                self.assertIs(bump_state._project_migration_verification(verification, value), verification)
            legacy = deepcopy(value)
            legacy.pop('migration_policy')
            self.assertIs(bump_state._project_migration_verification(original, legacy), original)

    def test_failed_verification_projects_only_exact_sealed_contract(self):
        value = occurrence_contract()
        with tempfile.TemporaryDirectory() as directory:
            verification = self.artifact_verification(directory, value)
            verification.update(status='failed', issues=['preserved failed proof'])
            projected = bump_state._project_migration_verification(verification, value)
            self.assertNotIn('proposed_contract', projected)
            self.assertEqual(projected['issues'], ['preserved failed proof'])
            changed = deepcopy(verification)
            changed['proposed_contract']['targets'].clear()
            self.assertIs(bump_state._project_migration_verification(changed, value), changed)
            unsealed = deepcopy(value)
            unsealed['unsealed'] = True
            changed = deepcopy(verification)
            changed['proposed_contract'] = unsealed
            self.assertIs(bump_state._project_migration_verification(changed, unsealed), changed)


if __name__ == '__main__':
    unittest.main()
