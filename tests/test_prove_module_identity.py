"""Keep independently compiled Lean declarations bound to their owning modules."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import blueprint, candidate_review, prove_contract, prove_state


def lean_name(name):
    """The structural Name representation used in frozen kernel contracts."""
    result = ['anonymous']
    for component in name.split('.'):
        result = ['str', result, component]
    return result


class ProveModuleIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.toolchain_bin = Path.home() / '.elan/toolchains/leanprover--lean4---v4.28.0/bin'
        if not (cls.toolchain_bin / 'lake').is_file():
            raise unittest.SkipTest('Lean 4.28 toolchain is not installed')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unity-prove-module-identity-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unity = self.root / '.unity'
        environment = patch.dict(os.environ, {
            'PATH': str(self.toolchain_bin) + os.pathsep + os.environ.get('PATH', ''),
        })
        environment.start()
        self.addCleanup(environment.stop)
        (self.root / 'lean-toolchain').write_text('leanprover/lean4:v4.28.0\n')
        (self.root / 'lakefile.toml').write_text(
            'name = "ModuleIdentityFixture"\n'
            'defaultTargets = ["Main"]\n'
            '[[lean_lib]]\nname = "Main"\n'
            '[[lean_lib]]\nname = "Left"\n'
            '[[lean_lib]]\nname = "Right"\n'
        )
        for module in ('Main', 'Left', 'Right'):
            self.write(module, '-- Independent fixture module.\n')

    def write(self, module, source):
        (self.root / (module + '.lean')).write_text(source)

    def declaration(self, kernel, name, module):
        matches = [(key, row) for key, row in kernel.items()
                   if row.get('name') == name and row.get('module') == module]
        self.assertEqual(len(matches), 1, (name, module, matches))
        return matches[0]

    def contract(self, kernel, name='unique_target', module='Main'):
        _, row = self.declaration(kernel, name, module)
        contract = prove_contract.freeze_contract(row['contract'], name)
        self.assertEqual(contract['module'], module)
        self.assertIsInstance(contract['origins'], list)
        for origin in contract['origins']:
            self.assertEqual(set(origin), {'name', 'module'})
            self.assertIsInstance(origin['name'], list)
            self.assertTrue(origin['module'] is None or isinstance(origin['module'], str))
        return contract

    def assert_signature_rejected(self, name, contract):
        try:
            check = prove_contract.check_signature(self.root, name, contract)
        except ValueError:
            return
        self.assertIs(check['passed'], False)

    def test_completed_duplicate_names_survive_and_unique_target_initializes(self):
        for module in ('Left', 'Right'):
            self.write(module, 'theorem shared_helper : True := by trivial\n')
        self.write('Main', 'theorem unique_target : True := by sorry\n')

        kernel, _ = prove_contract.inspect(self.root)
        left_key, left = self.declaration(kernel, 'shared_helper', 'Left')
        right_key, right = self.declaration(kernel, 'shared_helper', 'Right')
        self.assertNotEqual(left_key, right_key)
        self.assertNotEqual(left_key, 'shared_helper')
        self.assertNotEqual(right_key, 'shared_helper')
        self.assertFalse(left['sorried'])
        self.assertFalse(right['sorried'])

        dag = blueprint.build_prove_dag(self.root, self.unity)
        self.assertEqual([chunk['lean_decl'] for chunk in dag['chunks']], ['unique_target'])
        self.assertEqual(dag['chunks'][0]['lean_file'], 'Main.lean')
        self.assertEqual(dag['chunks'][0]['type_contract']['module'], 'Main')
        state = prove_state.initialize_from_dag(
            self.unity / 'forum', self.unity / 'dag.json', 'fixture', reset=True)
        self.assertEqual(set(state['declarations']), {'unique_target'})
        self.assertEqual(state['declarations']['unique_target']['type_contract'],
                         dag['chunks'][0]['type_contract'])

    def test_duplicate_helpers_cannot_mask_actual_axiom_dependencies(self):
        for unsafe_module, safe_module in [('Left', 'Right'), ('Right', 'Left')]:
            with self.subTest(unsafe_module=unsafe_module):
                self.write(unsafe_module,
                           'axiom unique_assumption : True\n'
                           'theorem shared_helper : True := unique_assumption\n')
                self.write(safe_module, 'theorem shared_helper : True := by trivial\n')
                self.write('Main', f'import {unsafe_module}\n'
                           'theorem unique_target : True := shared_helper\n')
                kernel, _ = prove_contract.inspect(self.root)
                target_key, target = self.declaration(kernel, 'unique_target', 'Main')
                unsafe_key, unsafe_helper = self.declaration(kernel, 'shared_helper', unsafe_module)
                safe_key, _ = self.declaration(kernel, 'shared_helper', safe_module)
                assumption_key, _ = self.declaration(kernel, 'unique_assumption', unsafe_module)
                self.assertIn(unsafe_key, target['deps'])
                self.assertNotIn(safe_key, target['deps'])
                self.assertIn(assumption_key, unsafe_helper['deps'])
                unsafe = candidate_review._unsafe_dependency_closure(kernel, target_key)
                self.assertEqual({row['name'] for row in unsafe}, {'unique_assumption'})

                self.write('Main', f'import {safe_module}\n'
                           'theorem unique_target : True := shared_helper\n')
                kernel, _ = prove_contract.inspect(self.root)
                target_key, target = self.declaration(kernel, 'unique_target', 'Main')
                selected_key, _ = self.declaration(kernel, 'shared_helper', safe_module)
                self.assertIn(selected_key, target['deps'])
                self.assertEqual(candidate_review._unsafe_dependency_closure(kernel, target_key), [])

    def test_duplicate_unresolved_fullname_has_both_module_names_in_error(self):
        for module in ('Left', 'Right'):
            self.write(module, 'theorem ambiguous_target : True := by sorry\n')
        with self.assertRaises(ValueError) as caught:
            blueprint.build_prove_dag(self.root, self.unity)
        message = str(caught.exception)
        self.assertIn('ambiguous_target', message)
        self.assertIn('Left', message)
        self.assertIn('Right', message)

    def test_frozen_contract_rejects_target_moved_to_another_module(self):
        target = 'theorem unique_target : True := by trivial\n'
        self.write('Main', target)
        kernel, _ = prove_contract.inspect(self.root)
        contract = self.contract(kernel)
        self.write('Main', '-- The declaration moved out of this module.\n')
        self.write('Right', target)
        kernel, _ = prove_contract.inspect(self.root)
        self.declaration(kernel, 'unique_target', 'Right')
        self.assert_signature_rejected('unique_target', contract)

    def test_import_switch_rejects_direct_and_indirect_constant_origin_changes(self):
        self.write('Left', 'def SharedPredicate : Prop := True\n')
        self.write('Right', 'def SharedPredicate : Prop := (1 : Nat) = 1\n')
        for indirect in (False, True):
            with self.subTest(indirect_wrapper=indirect):
                body = ('def LocalWrapper : Prop := SharedPredicate\n'
                        'theorem unique_target : LocalWrapper := by unfold LocalWrapper SharedPredicate; decide\n') if indirect else (
                        'theorem unique_target : SharedPredicate := by unfold SharedPredicate; decide\n')
                self.write('Main', 'import Left\n' + body)
                kernel, _ = prove_contract.inspect(self.root)
                contract = self.contract(kernel)
                self.assertIn({'name': lean_name('SharedPredicate'), 'module': 'Left'},
                              contract['origins'])
                if indirect:
                    self.assertIn({'name': lean_name('LocalWrapper'), 'module': 'Main'},
                                  contract['origins'])
                    missing_indirect = {key: value for key, value in contract.items() if key != 'sha256'}
                    missing_indirect['origins'] = [entry for entry in contract['origins']
                                                   if entry['name'] != lean_name('SharedPredicate')]
                    incomplete = prove_contract.freeze_contract(missing_indirect, 'unique_target')
                    self.assertIs(prove_contract.check_signature(
                        self.root, 'unique_target', incomplete)['passed'], False)
                else:
                    self.assertIs(prove_contract.check_signature(
                        self.root, 'unique_target', contract)['passed'], True)
                # Only the import changes; the target and any local wrapper stay identical.
                self.write('Main', 'import Right\n' + body)
                check = prove_contract.check_signature(self.root, 'unique_target', contract)
                self.assertIs(check['passed'], False)

    def test_legacy_contract_is_allowed_only_without_project_name_collisions(self):
        self.write('Main', 'theorem unique_target : True := by trivial\n')
        kernel, _ = prove_contract.inspect(self.root)
        current = self.contract(kernel)
        old_payload = {key: current[key] for key in ('decl', 'level_params', 'type')}
        legacy = prove_contract.freeze_contract(old_payload, 'unique_target')
        self.assertIs(prove_contract.check_signature(
            self.root, 'unique_target', legacy)['passed'], True)
        for module in ('Left', 'Right'):
            self.write(module, 'theorem shared_helper : True := by trivial\n')
        # The target itself is untouched; ambiguity anywhere prevents safe legacy binding.
        self.assert_signature_rejected('unique_target', legacy)

        # Noise-filtered public names must still count in the full collision inventory.
        for module in ('Left', 'Right'):
            self.write(module, 'def _SharedPredicate : Prop := True\n')
        self.write('Main', 'import Left\n'
                   'theorem unique_target : _SharedPredicate := by unfold _SharedPredicate; trivial\n')
        kernel, _ = prove_contract.inspect(self.root)
        current = self.contract(kernel)
        legacy = prove_contract.freeze_contract(
            {key: current[key] for key in ('decl', 'level_params', 'type')}, 'unique_target')
        self.assert_signature_rejected('unique_target', legacy)


class ProveModuleContractDigestTests(unittest.TestCase):
    def test_module_and_origin_tampering_invalidates_the_frozen_digest(self):
        payload = {
            'decl': 'unique_target', 'level_params': [],
            'type': ['const', lean_name('True'), []], 'module': 'Main',
            'origins': [{'name': lean_name('True'), 'module': 'Init.Prelude'}],
        }
        contract = prove_contract.freeze_contract(payload, 'unique_target')
        self.assertEqual(prove_contract._validate(contract, 'unique_target'), payload)
        for field in ('module', 'origins'):
            with self.subTest(tampered_field=field):
                changed = copy.deepcopy(contract)
                if field == 'module':
                    changed['module'] = 'Right'
                else:
                    changed['origins'][0]['module'] = 'DifferentOrigin'
                with self.assertRaises(ValueError):
                    prove_contract._validate(changed, 'unique_target')


if __name__ == '__main__':
    unittest.main()
