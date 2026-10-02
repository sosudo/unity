"""Lossless compact original-report freezing without large indentation payloads."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_bootstrap as bootstrap, bump_json, bump_runtime


class CompactBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_controller_json_roundtrips_all_nested_meaning_fields(self):
        record = {'module': 'Quoted.«a.b»', 'declaration_inventory': 'raw-module-constants-v1',
            'raw_declaration_count': 2,
            'declarations': {'same': {'axioms': ['custom axiom'], 'direct_sorry': False}},
            'meanings': {'same': {'meaning': {'name': ['str', ['str', ['anonymous'], 'a.b'], 'c'],
                'type': ['forall', 'x', ['const', 'Nat', []], ['bvar', 0], 'default'],
                'value': ['lit', 1729], 'kind': 'def', 'unsafe': False}}},
            'null': None, 'escaped': 'quote"\\\n\u0000', 'bool': True}
        destination = self.root / 'nested/report.json'
        bootstrap._json(destination, record)
        self.assertEqual(json.loads(destination.read_text()), record)
        self.assertEqual(destination.read_bytes(),
            (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode())
        self.assertEqual(destination.read_text().count('\n'), 1)

    def test_controller_writer_uses_shared_atomic_serializer(self):
        destination = self.root / 'report.json'
        value = {'sample': [1, 2, 3]}
        with patch.object(bump_json, 'atomic_dump') as writer:
            bootstrap._json(destination, value)
        writer.assert_called_once_with(destination, value)

    def test_deep_expression_has_no_indentation_amplification(self):
        expression = ['const', ['str', ['anonymous'], 'Nat'], []]
        for i in range(80):
            expression = ['app', expression, ['bvar', i]]
        value = {'meanings': {'fixture': {'meaning': expression}}}
        pretty = json.dumps(value, sort_keys=True, indent=2).encode()
        destination = self.root / 'report.json'
        bootstrap._json(destination, value)
        self.assertLess(destination.stat().st_size * 20, len(pretty))
        self.assertEqual(json.loads(destination.read_text()), value)

    def test_failed_serialization_preserves_existing_destination(self):
        destination = self.root / 'report.json'
        destination.write_bytes(b'preserved old receipt\n')
        before = destination.read_bytes()
        with self.assertRaises(TypeError):
            bootstrap._json(destination, {'first': [1, 2], 'unsupported': object()})
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_same_named_module_occurrences_and_trust_are_not_deduplicated(self):
        value = {'moduleA': {'declarations': {'shared': {'axioms': ['A.ax'], 'value': 1}}},
                 'moduleB': {'declarations': {'shared': {'axioms': ['B.ax'], 'value': 2}}}}
        destination = self.root / 'reports.json'
        bootstrap._json(destination, value)
        actual = json.loads(destination.read_text())
        self.assertEqual(actual, value)
        self.assertNotEqual(actual['moduleA'], actual['moduleB'])

    def test_compaction_preserves_existing_semantic_digest(self):
        value = {'declarations': {'x': {'axioms': ['propext'], 'meaning': ['bvar', 0]}},
                 'native_name': ['str', ['anonymous'], 'unicode_κ']}
        digest = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        destination = self.root / 'native.json'
        bootstrap._json(destination, value)
        self.assertEqual(digest(json.loads(destination.read_text())), digest(value))

    def test_verification_artifact_preserves_proposed_contract_and_all_receipts(self):
        meaning = ['app', ['const', ['str', ['anonymous'], 'Nat'], []], ['bvar', 0]]
        value = {'status': 'passed', 'proposed_contract': {'project_baseline': {
            'original_reports': {'A': {'meanings': {'same': {'meaning': meaning}},
                                     'declarations': {'same': {'axioms': ['A.ax']}}},
                                 'B': {'meanings': {'same': {'meaning': meaning}},
                                     'declarations': {'same': {'axioms': ['B.ax']}}}}}},
            'compiled_receipt': {'sha256': 'a' * 64}, 'issues': [],
            'project_declarations': {'same': {'type': meaning, 'proof_trust': ['propext']}},
            'unicode': 'κ'}
        payload = bump_runtime._verification_payload(value)
        self.assertEqual(json.loads(payload), value)
        self.assertEqual(payload, json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n')
        self.assertEqual(payload.count('\n'), 1)

    def test_candidate_verification_artifact_uses_compact_payload(self):
        import inspect
        source = inspect.getsource(bump_runtime._apply_formal_candidate)
        self.assertIn('paths.artifacts, _verification_payload(verification)', source)
        self.assertNotIn('json.dumps(verification, indent=', source)

    def test_verification_artifact_reference_is_attached_even_for_empty_module(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(bump_runtime._apply_formal_candidate))
        assignments = [node for node in tree.body[0].body if isinstance(node, ast.Assign)]
        references = [node for node in assignments if any(
            isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
            and target.value.id == 'verification' and isinstance(target.slice, ast.Constant)
            and target.slice.value == 'verification_artifact' for target in node.targets)]
        self.assertEqual(len(references), 1)
        self.assertEqual({key.value for key in references[0].value.keys}, {'artifact_id', 'sha256'})
        for declarations in ({}, {'one': {'kind': 'theorem'}}):
            namespace = {'verification': {'project_declarations': declarations},
                         'record': {'artifact_id': 'artifact-exact', 'sha256': 'f' * 64}}
            node = ast.Module(body=references, type_ignores=[])
            exec(compile(ast.fix_missing_locations(node), '<exact production assignment>', 'exec'), namespace)
            self.assertEqual(namespace['verification']['verification_artifact'],
                             {'artifact_id': 'artifact-exact', 'sha256': 'f' * 64})


if __name__ == '__main__':
    unittest.main()
