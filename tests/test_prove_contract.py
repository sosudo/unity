import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unity import blueprint, candidate_review, prove_contract, prove_state


class ProveDiscoveryTests(unittest.TestCase):
    def test_failed_build_does_not_inspect_stale_oleans(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(blueprint.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
             patch.object(prove_contract, 'inspect') as inspect:
            with self.assertRaisesRegex(ValueError, 'build failed'):
                blueprint.build_prove_dag(Path(tmp), Path(tmp) / '.unity')
            inspect.assert_not_called()


class ProveContractIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / 'lean-toolchain').write_text('leanprover/lean4:v4.28.0\n')
        (cls.root / 'lakefile.toml').write_text('name = "Fixture"\ndefaultTargets = ["Fixture"]\n[[lean_lib]]\nname = "Fixture"\n')
        (cls.root / 'Fixture.lean').write_text('''universe u
namespace Fixture
theorem target (α : Type u) (x : α) : x = x := by sorry
def program : Nat := by sorry
end Fixture
''')
        cls.unity = cls.root / '.unity'
        cls.unity.mkdir()
        cls.dag = blueprint.build_prove_dag(cls.root, cls.unity)
        cls.contracts = {c['lean_decl']: c['type_contract'] for c in cls.dag['chunks']}
        state = prove_state.initialize_from_dag(cls.unity / 'forum', cls.unity / 'dag.json', 'fixture', reset=True)
        assert state['declarations']['Fixture.target']['type_contract'] == cls.contracts['Fixture.target']
        (cls.root / 'Fixture.lean').write_text('''universe v
namespace Fixture
abbrev Alias (α : Type v) := α
theorem target (β : Type v) (y : Alias β) : y = y := by rfl
def program : Nat := 7
end Fixture
''')
        cls.build()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def build(cls):
        subprocess.run(['lake', 'build'], cwd=cls.root, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)

    def test_universe_binder_and_definitional_instance_changes_pass(self):
        kernel, check = prove_contract.inspect(self.root, decl='Fixture.target',
                                                contract=self.contracts['Fixture.target'])
        initial = next(c for c in self.dag['chunks'] if c['lean_decl'] == 'Fixture.target')
        self.assertNotEqual(initial['kernel_type_repr'], kernel['Fixture.target']['type_repr'])
        self.assertIs(check['passed'], True)
        self.assertTrue(prove_contract.check_signature(self.root, 'Fixture.program', self.contracts['Fixture.program'])['passed'])

    def test_missing_wrong_decl_and_tampered_digest_fail_closed(self):
        with self.assertRaises(ValueError):
            prove_contract.check_signature(self.root, 'Fixture.target', None)
        with self.assertRaises(ValueError):
            prove_contract.check_signature(self.root, 'Fixture.program', self.contracts['Fixture.target'])
        edited = copy.deepcopy(self.contracts['Fixture.target'])
        edited['type'] = ['sort', ['zero']]
        with self.assertRaises(ValueError):
            prove_contract.check_signature(self.root, 'Fixture.target', edited)
        declaration = {'decl': 'Fixture.target', 'title': 'target', 'file': 'Fixture.lean',
                       'statement': 'theorem target (β : Type v) (y : Alias β) : y = y',
                       'kernel_type_repr': 'same-looking source'}
        review = candidate_review.review_candidate(self.root, {'candidate_id': 'test'}, declaration,
                                                    base_main_sha='fixture', candidate_diff='')
        self.assertEqual(review['status'], 'failed')
        self.assertEqual(review['mode'], 'kernel_unavailable')
        self.assertFalse(review['signature_unchanged'])

    def test_malformed_and_metavariable_contracts_rejected_by_lean(self):
        for typ in (['mvar', ['anonymous']], ['fvar', ['anonymous']], ['bvar', 0], ['unknown']):
            payload = {'decl': 'Fixture.target', 'level_params': self.contracts['Fixture.target']['level_params'], 'type': typ}
            contract = prove_contract.freeze_contract(payload, 'Fixture.target')
            self.assertFalse(prove_contract.check_signature(self.root, 'Fixture.target', contract)['passed'])

    def test_changed_type_and_universe_arity_rejected(self):
        original = (self.root / 'Fixture.lean').read_text()
        try:
            (self.root / 'Fixture.lean').write_text('''universe v
namespace Fixture
theorem target (β : Type v) (y : β) : y = y ∧ True := by exact ⟨rfl, trivial⟩
def program : Nat := 7
end Fixture
''')
            self.build()
            self.assertFalse(prove_contract.check_signature(self.root, 'Fixture.target', self.contracts['Fixture.target'])['passed'])
            (self.root / 'Fixture.lean').write_text('''namespace Fixture
theorem target (n : Nat) : n = n := by rfl
def program : Nat := 7
end Fixture
''')
            self.build()
            self.assertFalse(prove_contract.check_signature(self.root, 'Fixture.target', self.contracts['Fixture.target'])['passed'])
        finally:
            (self.root / 'Fixture.lean').write_text(original)
            self.build()


if __name__ == '__main__':
    unittest.main()
