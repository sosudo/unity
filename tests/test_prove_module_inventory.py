import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import blueprint, candidate_review, prove_contract


class ProveModuleInventoryTests(unittest.TestCase):
    """Inspection must cover project modules omitted by Lake's default target."""

    @classmethod
    def setUpClass(cls):
        cls.toolchain_bin = (
            Path.home() / '.elan/toolchains/leanprover--lean4---v4.28.0/bin'
        )
        if not (cls.toolchain_bin / 'lake').is_file():
            raise unittest.SkipTest('Lean 4.28 toolchain is not installed')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unity-prove-inventory-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path_patch = patch.dict(os.environ, {
            'PATH': str(self.toolchain_bin) + os.pathsep + os.environ.get('PATH', ''),
        })
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        (self.root / 'lean-toolchain').write_text('leanprover/lean4:v4.28.0\n')
        (self.root / 'lakefile.toml').write_text(
            'name = "InventoryFixture"\n'
            'defaultTargets = ["Main"]\n'
            '[[lean_lib]]\nname = "Main"\n'
            '[[lean_lib]]\nname = "Extra"\n'
        )
        (self.root / 'Main.lean').write_text('theorem main_target : True := by trivial\n')
        # The root of this nondefault library deliberately does not import Orphan.
        (self.root / 'Extra.lean').write_text('namespace Extra\nend Extra\n')
        (self.root / 'Extra').mkdir()
        self.extra_source = self.root / 'Extra/Orphan.lean'
        self.extra_source.write_text(
            'namespace Extra\ntheorem orphan : True := by trivial\nend Extra\n'
        )
        self.extra_olean = self.root / '.lake/build/lib/lean/Extra/Orphan.olean'

    def build(self, *targets):
        result = subprocess.run(
            ['lake', 'build', *targets], cwd=self.root, capture_output=True,
            text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inspect_builds_and_imports_nondefault_unimported_module(self):
        self.build()
        self.assertFalse(self.extra_olean.exists())
        self.assertFalse((self.root / '.lake/build/lib/lean/Extra.olean').exists())

        kernel, check = prove_contract.inspect(self.root)

        self.assertIsNone(check)
        self.assertTrue(self.extra_olean.is_file())
        self.assertIn('main_target', kernel)
        self.assertEqual(kernel['Extra.orphan']['module'], 'Extra.Orphan')
        self.assertFalse(kernel['Extra.orphan']['sorried'])

    def test_broken_nondefault_module_rejects_existing_stale_olean(self):
        self.build()
        self.build('+Extra:olean', '+Extra.Orphan:olean')
        stale_olean = self.extra_olean.read_bytes()
        self.extra_source.write_text(
            'namespace Extra\n'
            'theorem orphan : True := missing_inventory_test_proof\n'
            'end Extra\n'
        )
        # The ordinary project build still succeeds and leaves the old module.
        self.build()
        self.assertEqual(self.extra_olean.read_bytes(), stale_olean)

        with self.assertRaises(ValueError):
            prove_contract.inspect(self.root)

    def test_unreferenced_extra_holes_remain_in_metadata_and_discovery(self):
        self.extra_source.write_text(
            'namespace Extra\n'
            'axiom unreferenced_axiom : True\n'
            'theorem unreferenced_sorry : True := by sorry\n'
            'end Extra\n'
        )
        self.build()
        self.assertFalse(self.extra_olean.exists())

        kernel, _ = prove_contract.inspect(self.root)
        self.assertEqual(kernel['Extra.unreferenced_axiom']['kind'], 'axiom')
        self.assertTrue(kernel['Extra.unreferenced_sorry']['sorried'])
        self.assertEqual(kernel['main_target']['deps'], [])

        dag = blueprint.build_prove_dag(self.root, self.root / '.unity')
        self.assertEqual(dag['source'], 'kernel')
        chunks = {chunk['lean_decl']: chunk for chunk in dag['chunks']}
        self.assertEqual(set(chunks), {'Extra.unreferenced_axiom', 'Extra.unreferenced_sorry'})
        for name, chunk in chunks.items():
            self.assertEqual(chunk['lean_file'], 'Extra/Orphan.lean')
            self.assertEqual(chunk['type_contract']['decl'], name)

    def test_reachable_extra_helpers_remain_in_unsafe_dependency_closure(self):
        self.extra_source.write_text(
            'namespace Extra\n'
            'axiom assumed : True\n'
            'theorem unfinished : True := by sorry\n'
            'theorem bridge : True := assumed\n'
            'end Extra\n'
        )
        (self.root / 'Main.lean').write_text(
            'import Extra.Orphan\n'
            'theorem main_target : True ∧ True := ⟨Extra.bridge, Extra.unfinished⟩\n'
        )
        self.build()

        kernel, _ = prove_contract.inspect(self.root)
        unsafe = candidate_review._unsafe_dependency_closure(kernel, 'main_target')

        self.assertEqual({row['name'] for row in unsafe}, {'Extra.assumed', 'Extra.unfinished'})
        self.assertEqual(kernel['Extra.assumed']['module'], 'Extra.Orphan')
        self.assertIn('Extra.assumed', kernel['Extra.bridge']['deps'])
        self.assertTrue(next(row for row in unsafe if row['name'] == 'Extra.unfinished')['sorried'])


if __name__ == '__main__':
    unittest.main()
