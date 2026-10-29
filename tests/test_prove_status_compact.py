import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity.forum import server


class ProveStatusCompactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.forum = Path(self.temp.name)
        self.state = {
            'schema_version': 5, 'revision': 7, 'main_sha': 'main',
            'declarations': {
                'A': {'decl': 'A', 'status': 'unresolved', 'kernel_type_repr': 'original type',
                      'type_contract': {'decl': 'A', 'type': ['forallE', ['large', 'payload']],
                                        'level_params': [], 'sha256': 'abc123'}},
                'B': {'decl': 'B', 'status': 'solved', 'kernel_type_repr': 'other type',
                      'type_contract': {'decl': 'B', 'type': ['sort', ['zero']],
                                        'level_params': [], 'sha256': 'def456'}},
            },
            'strategies': {'s': {'decl': 'A', 'owner': 'worker'}},
            'findings': {'f': {'decl': 'A', 'content': 'evidence'}},
            'candidates': {'c': {'decl': 'A', 'status': 'failed'}},
            'events': [{'event_id': 'e', 'decl': 'A', 'kind': 'fixture'}],
        }
        self.path = self.forum / 'prove-state.json'
        self.path.write_text(json.dumps(self.state))
        self.original = self.path.read_bytes()
        self.patch = patch.object(server, 'FORUM_DIR', self.forum)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_full_default_is_compact_without_mutating_authoritative_state(self):
        result = server.prove_status()
        self.assertEqual(result['revision'], 7)
        self.assertEqual(result['strategies'], self.state['strategies'])
        self.assertEqual(result['events'], self.state['events'])
        for name, sha in [('A', 'abc123'), ('B', 'def456')]:
            self.assertNotIn('type_contract', result['declarations'][name])
            self.assertEqual(result['declarations'][name]['type_contract_sha256'], sha)
            self.assertEqual(result['declarations'][name]['kernel_type_repr'],
                             self.state['declarations'][name]['kernel_type_repr'])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_single_declaration_and_explicit_full_contract(self):
        compact = server.prove_status('A')
        self.assertNotIn('type_contract', compact['declaration'])
        self.assertEqual(compact['declaration']['type_contract_sha256'], 'abc123')
        self.assertEqual(compact['strategies'], list(self.state['strategies'].values()))
        self.assertEqual(compact['findings'], list(self.state['findings'].values()))
        self.assertEqual(compact['candidates'], list(self.state['candidates'].values()))
        expanded = server.prove_status('A', include_type_contract=True)
        self.assertEqual(expanded['declaration']['type_contract'], self.state['declarations']['A']['type_contract'])
        self.assertEqual(expanded['declaration']['type_contract_sha256'], 'abc123')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_full_opt_in_returns_all_contracts_and_unknown_decl_still_errors(self):
        expanded = server.prove_status(include_type_contract=True)
        for name in ('A', 'B'):
            self.assertEqual(expanded['declarations'][name]['type_contract'], self.state['declarations'][name]['type_contract'])
        with self.assertRaisesRegex(ValueError, 'unknown prove declaration'):
            server.prove_status('missing')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_compaction_does_not_mutate_loaded_state_object(self):
        state = copy.deepcopy(self.state)
        with patch.object(server.prove_store, 'load_state', return_value=state):
            server.prove_status()
            server.prove_status('A')
        self.assertEqual(state, self.state)


if __name__ == '__main__':
    unittest.main()
