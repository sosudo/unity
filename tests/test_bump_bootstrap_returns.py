"""Unused bootstrap returns skip only the final detached state read; no services."""

import ast
from copy import deepcopy
import fcntl
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_bootstrap as bootstrap, bump_contract as contract, bump_state as state
from tests.test_bump_occurrence_contract import occurrence_contract


class BootstrapReturnTests(unittest.TestCase):
    stages = ('source', 'plan', 'seed')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bump-return-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.value = occurrence_contract()
        self.baseline = self.value['project_baseline']
        self.graph = self.baseline['compiler_modules']
        self.reports = self.baseline['original_reports']
        refs = ['transition.json']
        refs += ['native/' + module + '.json' for module in self.graph]
        refs += ['project/' + row['path'] for row in self.graph.values()]
        self.source = {'kind': 'supplied_sources', 'candidate_id': 'source-' + 'c' * 64,
            'sha256': 'c' * 64, 'source_refs': [
                {'ref_id': 'source:' + name, 'path': '.unity/source/' + name,
                 'sha256': 'd' * 64} for name in refs]}
        self.dag = bootstrap.migration_dag(self.graph, self.reports, self.source)
        requirements = contract.normalize_requirements(self.dag['requirements'], self.dag['chunks'],
            {row['ref_id'] for row in self.source['source_refs']})
        spec = contract.normalize_spec(self.dag['spec'], source=self.source,
            requirements=requirements, tasks=self.dag['chunks'], allow_unresolved=True)
        self.value.update(solution_candidate=self.source['candidate_id'],
            requirements=requirements, spec=spec, spec_sha256=contract.digest(spec))
        self.value = contract._seal_contract(self.value)
        clock = patch.object(state.time, 'time', return_value=1234.5)
        identifiers = patch.object(state, '_id', side_effect=lambda prefix: prefix + '-fixture')
        clock.start()
        identifiers.start()
        self.addCleanup(clock.stop)
        self.addCleanup(identifiers.stop)

    def invoke(self, stage, forum, option=None, **kwargs):
        if option is not None:
            kwargs['return_state'] = option
        if stage == 'source':
            return state.initialize_source(forum, 'a' * 64, 'b' * 40, self.source,
                project_baseline=self.baseline, **kwargs)
        if stage == 'plan':
            return state.initialize_informal_plan(forum, self.dag, main_sha='b' * 40,
                contract=self.value, plan_artifact='artifact-' + '1' * 12, **kwargs)
        return state.seed_migration_tasks(forum, self.graph, self.reports, **kwargs)

    def prepared(self, stage, suffix=''):
        forum = self.root / (stage + suffix)
        forum.mkdir()
        for preceding in self.stages[:self.stages.index(stage)]:
            self.invoke(preceding, forum, False)
        return forum

    def test_default_and_no_return_have_identical_persisted_bytes_and_revisions(self):
        for stage in self.stages:
            with self.subTest(stage=stage):
                first, second = self.prepared(stage, '-default'), self.prepared(stage, '-none')
                result = self.invoke(stage, first)
                self.assertIsNone(self.invoke(stage, second, False))
                self.assertIsInstance(result, dict)
                self.assertEqual(state.state_path(first).read_bytes(), state.state_path(second).read_bytes())
                self.assertEqual(result['revision'], self.stages.index(stage) + 1)

    def test_only_unused_post_transaction_read_is_removed(self):
        for stage in self.stages:
            for option in (None, True, False):
                with self.subTest(stage=stage, option=option):
                    forum = self.prepared(stage, '-' + str(option))
                    with patch.object(state, 'load_state', wraps=state.load_state) as load, \
                            patch.object(state, '_read_unlocked', wraps=state._read_unlocked) as read:
                        self.invoke(stage, forum, option)
                    self.assertEqual(load.call_count, 0 if option is False else 1)
                    self.assertEqual(read.call_count, 1 if option is False else 2)

    def test_default_returns_are_detached_from_disk_and_caller_inputs(self):
        for stage in self.stages:
            with self.subTest(stage=stage):
                forum = self.prepared(stage)
                result = self.invoke(stage, forum)
                before = state.state_path(forum).read_bytes()
                inputs = deepcopy((self.source, self.baseline, self.value))
                result['project_baseline']['original_reports'].clear()
                result['input_source']['source_refs'].clear()
                self.assertEqual(state.state_path(forum).read_bytes(), before)
                self.assertEqual((self.source, self.baseline, self.value), inputs)

    def test_both_modes_retain_transaction_lock_and_unlock(self):
        for stage in self.stages:
            for option in (True, False):
                with self.subTest(stage=stage, option=option):
                    forum = self.prepared(stage, '-' + str(option))
                    with patch.object(state.fcntl, 'flock', wraps=fcntl.flock) as flock:
                        self.invoke(stage, forum, option)
                    self.assertEqual([call.args[1] for call in flock.call_args_list],
                                     [fcntl.LOCK_EX, fcntl.LOCK_UN])

    def test_default_return_still_observes_a_post_commit_update(self):
        for stage in self.stages:
            with self.subTest(stage=stage):
                forum = self.prepared(stage)
                actual_load = state.load_state

                def concurrent_update_then_load(directory):
                    with state.transaction(directory) as current:
                        current['post_publication'] = 'fresh'
                    return actual_load(directory)

                with patch.object(state, 'load_state', side_effect=concurrent_update_then_load):
                    result = self.invoke(stage, forum)
                self.assertEqual(result['post_publication'], 'fresh')
                self.assertEqual(result, actual_load(forum))

    def test_no_return_plan_still_rejects_stale_revision_without_publication(self):
        forum = self.prepared('plan')
        before = state.state_path(forum).read_bytes()
        with self.assertRaisesRegex(ValueError, 'chunking state changed'):
            self.invoke('plan', forum, False, expected_revision=0)
        self.assertEqual(state.state_path(forum).read_bytes(), before)

    def test_no_return_source_still_rejects_changed_original_baseline(self):
        forum = self.prepared('plan')
        before = state.state_path(forum).read_bytes()
        self.baseline = {**self.baseline, 'sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'original project baseline'):
            self.invoke('source', forum, False)
        self.assertEqual(state.state_path(forum).read_bytes(), before)

    def test_no_return_seed_still_reads_and_rejects_invalid_saved_baseline(self):
        forum = self.prepared('seed')
        with state.transaction(forum) as current:
            current['formalization']['contract']['project_baseline']['sha256'] = '0' * 64
        before = state.state_path(forum).read_bytes()
        with self.assertRaisesRegex(ValueError, 'sealed scope'):
            self.invoke('seed', forum, False)
        self.assertEqual(state.state_path(forum).read_bytes(), before)

    def test_no_return_seed_rejects_module_inventory_change(self):
        forum = self.prepared('seed')
        before = state.state_path(forum).read_bytes()
        self.graph = {key: value for key, value in self.graph.items() if key != 'Empty'}
        with self.assertRaisesRegex(ValueError, 'fixed module inventory'):
            self.invoke('seed', forum, False)
        self.assertEqual(state.state_path(forum).read_bytes(), before)

    def test_v2_bootstrap_opts_out_at_the_two_discarded_return_calls(self):
        tree = ast.parse(inspect.getsource(bootstrap.prepare))
        expected = {'initialize_source', 'initialize_migration_plan'}
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if isinstance(call.func, ast.Attribute) and call.func.attr in expected:
                self.assertEqual([keyword.value.value for keyword in call.keywords
                    if keyword.arg == 'return_state' and isinstance(keyword.value, ast.Constant)], [False])
                found.add(call.func.attr)
        self.assertEqual(found, expected)


if __name__ == '__main__':
    unittest.main()
