"""Project-scope CLI/report regressions without Lake, providers or remote calls."""
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest

from unity import formalize_project, formalize_report, formalize_scope, formalize_state
from unity.commands import formalize as command
from unity.formalize_input import snapshot_sources, scope_bytes
import test_formalize_command as existing


def library_baseline(base: dict) -> dict:
    result = deepcopy(base)
    modules = {'Existing.lean': 'Existing', 'Imported.lean': 'Imported', 'Aux.lean': 'Aux'}
    owners = {
        'Existing.lean': {'libraries': ['Library'], 'executables': []},
        'Imported.lean': {'libraries': [], 'executables': ['tool']},
        'Aux.lean': {'libraries': [], 'executables': ['tool']},
    }
    verified = {path: modules[path] for path in ('Existing.lean', 'Imported.lean')}
    editable = {'Existing.lean': 'Existing'}
    policy = formalize_scope._seal({
        'version': 1, 'mode': 'libraries', 'selected_libraries': ['Library'],
        'original_modules': modules, 'original_owners': owners,
        'verification_modules': verified, 'editable_modules': editable,
        'imports': {'Existing.lean': ['Imported'], 'Imported.lean': []},
    })
    result['verification_scope'] = policy
    result['layout'].update(modules=modules, module_owners=owners, libraries=['Library'],
                            verification_modules=verified, editable_modules=editable,
                            project_scope='libraries', scope_sha256=policy['sha256'])
    return formalize_project._seal(result)


class ProjectScopeCommandTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only the external-boundary fixture, not its test methods.
    setUp = existing.FormalizeCommandTests.setUp
    add_patch = existing.FormalizeCommandTests.add_patch
    baseline = existing.FormalizeCommandTests.baseline
    chunk = existing.FormalizeCommandTests.chunk
    finish = existing.FormalizeCommandTests.finish
    invoke = existing.FormalizeCommandTests.invoke

    def capture(self, root, target_scope='All', *, project_scope='all'):
        self.assertEqual(root, self.root)
        result = self.baseline(target_scope)
        if project_scope == 'changes':
            return existing.changes_baseline(result)
        return library_baseline(result) if project_scope == 'libraries' else result

    def bind(self, project_scope='libraries'):
        baseline = self.capture(self.root, 'target', project_scope=project_scope)
        source = snapshot_sources(self.paths)
        result = formalize_state.initialize_source(
            self.paths.forum, hashlib.sha256(scope_bytes(self.paths)).hexdigest(), self.head,
            source, reset=True, project_baseline=baseline)
        formalize_state.set_phase(self.paths.forum, 'formalizing')
        return result

    async def test_fresh_omitted_mode_selects_changes(self):
        result = await self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks['capture_baseline'].assert_called_once_with(
            self.root, target_scope='Formalize the supplied sources within this existing project.',
            project_scope='changes')
        state = formalize_state.load_state(self.paths.forum)
        self.assertEqual(formalize_scope.mode(state['project_baseline']), 'changes')

    async def test_continue_reuses_changes_policy(self):
        original = self.bind(project_scope='changes')
        result = await self.invoke('--continue')
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks['capture_baseline'].assert_not_called()
        self.mocks['build_sources'].assert_called_once_with(
            self.root, full=True, task_id='resume-preflight', baseline=original['project_baseline'])

    async def test_continue_cannot_downgrade_changes_to_all(self):
        self.bind(project_scope='changes')
        result = await self.invoke('--continue', '--project-scope', 'all')
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('cannot change the original --project-scope', result.output)
        self.mocks['build_sources'].assert_not_called()

    async def test_fresh_explicit_all_keeps_legacy_capture_call(self):
        result = await self.invoke('--project-scope', 'all')
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks['capture_baseline'].assert_called_once_with(self.root, target_scope='All')

    async def test_fresh_libraries_propagates_both_independent_scopes(self):
        result = await self.invoke('--project-scope', 'libraries', '--targets', 'target')
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks['capture_baseline'].assert_called_once_with(
            self.root, target_scope='target', project_scope='libraries')
        state = formalize_state.load_state(self.paths.forum)
        self.assertEqual(formalize_scope.mode(state['project_baseline']), 'libraries')
        self.assertEqual(state['project_baseline']['target_scope'], 'target')

    async def test_continue_omitted_mode_reuses_library_baseline_in_build(self):
        original = self.bind()
        result = await self.invoke('--continue')
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks['capture_baseline'].assert_not_called()
        self.mocks['build_sources'].assert_called_once_with(
            self.root, full=True, task_id='resume-preflight', baseline=original['project_baseline'])
        self.assertEqual(formalize_state.load_state(self.paths.forum)['project_baseline'],
                         original['project_baseline'])

    async def test_continue_explicit_same_mode_preserves_library_build(self):
        original = self.bind()
        result = await self.invoke('--continue', '--project-scope', 'libraries')
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks['build_sources'].assert_called_once_with(
            self.root, full=True, task_id='resume-preflight', baseline=original['project_baseline'])

    async def test_continue_cannot_expand_libraries_to_all_before_lake_or_recovery(self):
        self.bind()
        before = formalize_state.state_path(self.paths.forum).read_bytes()
        result = await self.invoke('--continue', '--project-scope', 'all')
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('cannot change the original --project-scope', result.output)
        self.mocks['build_sources'].assert_not_called()
        self.mocks['terminate'].assert_not_called()
        self.mocks['recover_interrupted_formal_merges'].assert_not_called()
        self.mocks['_chunk_source'].assert_not_awaited()
        self.assertEqual(formalize_state.state_path(self.paths.forum).read_bytes(), before)

    async def test_legacy_all_cannot_downgrade_to_libraries_on_continue(self):
        self.bind(project_scope='all')
        result = await self.invoke('--continue', '--project-scope', 'libraries')
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('cannot change the original --project-scope', result.output)
        self.mocks['build_sources'].assert_not_called()
        self.mocks['terminate'].assert_not_called()

    async def test_legacy_all_continue_accepts_explicit_all(self):
        self.bind(project_scope='all')
        result = await self.invoke('--continue', '--project-scope', 'all')
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks['build_sources'].assert_called_once_with(
            self.root, full=True, task_id='resume-preflight')

    async def test_continue_rejects_downgraded_bound_baseline_before_execution(self):
        original = self.bind()['project_baseline']
        changed = deepcopy(original)
        changed.pop('verification_scope')
        for key in ('verification_modules', 'editable_modules', 'project_scope', 'scope_sha256'):
            changed['layout'].pop(key)
        changed['origin_sha256'] = original['sha256']
        changed = formalize_project._seal(changed)
        self.assertTrue(formalize_project.baseline_is_valid(changed))
        with formalize_state.transaction(self.paths.forum) as state:
            state['formalization']['contract'] = {'project_baseline': changed}
        before = formalize_state.state_path(self.paths.forum).read_bytes()
        result = await self.invoke('--continue')
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('does not match the original project baseline', result.output)
        for name in ('load_roster', 'terminate', 'require_pinned_inputs', 'build_sources',
                     'recover_interrupted_formal_merges'):
            self.mocks[name].assert_not_called()
        self.assertEqual(formalize_state.state_path(self.paths.forum).read_bytes(), before)

    async def test_continue_preserves_legitimate_one_time_natural_scope_binding(self):
        original = self.bind()['project_baseline']
        original['target_scope'] = 'Fill the intended library result'
        original['scope'] = {'mode': 'natural', 'existing_targets': [], 'bound': False}
        original = formalize_project._seal(original)
        bound = formalize_project.bind_scope(original, {'existing_targets': ['target'], 'chunks': []})
        with formalize_state.transaction(self.paths.forum) as state:
            state['project_baseline'] = original
            state['formalization']['contract'] = {'project_baseline': bound}
        result = await self.invoke('--continue')
        self.assertEqual(result.exit_code, 0, result.output)
        self.mocks['build_sources'].assert_called_once_with(
            self.root, full=True, task_id='resume-preflight', baseline=bound)

    async def test_unknown_project_scope_is_rejected_before_loading_project(self):
        result = await self.invoke('--project-scope', 'executables')
        self.assertNotEqual(result.exit_code, 0)
        self.mocks['load_paths'].assert_not_called()
        self.mocks['build_sources'].assert_not_called()

    def test_preparation_forwards_supplied_library_baseline(self):
        baseline = library_baseline(self.baseline())
        command._prepare_formalize_environment(self.root, baseline=baseline)
        self.mocks['build_sources'].assert_called_once_with(
            self.root, full=True, task_id='resume-preflight', baseline=baseline)


class ProjectScopeReportTests(unittest.TestCase):
    def baseline(self):
        from test_formalize_manifest_repair import project_baseline
        result = library_baseline(project_baseline())
        result['files'] = {'Aux.lean': 'a' * 64, 'unowned-script.lean': 'b' * 64}
        return formalize_project._seal(result)

    def state(self, baseline):
        return {'project_baseline': baseline, 'formalization': {'contract': {'project_baseline': baseline}},
                'phase': 'formalizing', 'formal_tasks': {}}

    def test_incomplete_report_distinguishes_verified_readonly_and_byte_only_sources(self):
        baseline = self.baseline()
        report = formalize_report.completion_report(self.state(baseline), accepted=False)
        coverage = report['project_verification']
        self.assertEqual(coverage['mode'], 'libraries')
        self.assertEqual(coverage['selected_libraries'], ['Library'])
        self.assertEqual(coverage['original_readonly_verified_modules'], {'Imported.lean': 'Imported'})
        self.assertEqual(coverage['original_byte_only_auxiliary_modules'], {'Aux.lean': 'Aux'})
        self.assertEqual(coverage['original_byte_only_auxiliary_files'], ['Aux.lean', 'unowned-script.lean'])
        self.assertIsNone(coverage['current_snapshot_coverage'])
        self.assertIn('not whole-project verification', coverage['qualification'])
        self.assertIn('not claimed compiled or kernel-verified', coverage['qualification'])

    def test_legacy_report_remains_all_scope_without_library_caveat(self):
        from test_formalize_manifest_repair import project_baseline
        coverage = formalize_report.completion_report(self.state(project_baseline()), accepted=False)['project_verification']
        self.assertEqual(coverage['mode'], 'all')
        self.assertEqual(coverage['original_byte_only_auxiliary_modules'], {})
        self.assertIn('All-project verification scope', coverage['qualification'])

    def test_changes_report_does_not_claim_a_whole_project_inventory(self):
        from test_formalize_manifest_repair import project_baseline
        baseline = existing.changes_baseline(project_baseline())
        coverage = formalize_report.completion_report(self.state(baseline), accepted=False)['project_verification']
        self.assertEqual(coverage['mode'], 'changes')
        self.assertIsNone(coverage['current_snapshot_coverage'])
        self.assertIn('not whole-project', coverage['qualification'])

    def test_changes_report_requires_current_policy_bound_coverage(self):
        from test_formalize_manifest_repair import project_baseline
        baseline = existing.changes_baseline(project_baseline())
        state = self.state(baseline)
        coverage = {'mode': 'changes', 'policy': 'changes-v1', 'inspection_policy': 2,
                    'baseline_sha256': baseline['sha256'], 'normal_default_build': True,
                    'verification_modules': {'New.lean': 'New'}, 'byte_only_modules': {},
                    'contexts': ['New']}
        for key, wrong in (('mode', 'all'), ('policy', 'old'), ('inspection_policy', 1),
                           ('baseline_sha256', 'bad'), ('normal_default_build', False)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                formalize_report._project_verification(
                    state, {'project_verification': {**coverage, key: wrong}}, accepted=True)
        result = formalize_report._project_verification(state, {'project_verification': coverage}, accepted=True)
        self.assertEqual(result['current_snapshot_coverage'], coverage)

    def test_changes_baseline_cannot_be_silently_downgraded(self):
        from test_formalize_manifest_repair import project_baseline
        baseline = existing.changes_baseline(project_baseline())
        for key in ('version', 'policy', 'project_scope'):
            malformed = deepcopy(baseline)
            malformed.pop(key)
            malformed = formalize_project._seal(malformed)
            with self.subTest(key=key), self.assertRaises(ValueError):
                formalize_scope.mode(malformed)

    def test_accepted_library_coverage_requires_matching_current_snapshot(self):
        baseline = self.baseline()
        state = self.state(baseline)
        for snapshot in ({}, {'project_verification': {'mode': 'all'}},
                         {'project_verification': {'mode': 'libraries', 'scope_sha256': 'wrong',
                                                   'selected_libraries': ['Library']}}):
            with self.subTest(snapshot=snapshot), self.assertRaisesRegex(ValueError, 'matching current'):
                formalize_report._project_verification(state, snapshot, accepted=True)

    def test_current_coverage_retains_new_helpers_without_claiming_excluded_modules(self):
        baseline = self.baseline()
        current = {
            'mode': 'libraries', 'scope_sha256': baseline['verification_scope']['sha256'],
            'selected_libraries': ['Library'],
            'verification_modules': {'Existing.lean': 'Existing', 'Imported.lean': 'Imported',
                                     'NewHelper.lean': 'NewHelper'},
            'editable_modules': {'Existing.lean': 'Existing', 'NewHelper.lean': 'NewHelper'},
            'readonly_imported_modules': {'Imported.lean': 'Imported'},
            'byte_only_modules': {'Aux.lean': 'Aux'},
        }
        result = formalize_report._project_verification(self.state(baseline),
                                                       {'project_verification': current}, accepted=True)
        self.assertEqual(result['current_snapshot_coverage'], current)
        self.assertNotIn('NewHelper.lean', result['original_verification_modules'])

    def test_role_prompts_explain_frozen_auxiliaries_and_no_whole_project_claim(self):
        prompts = Path(__file__).parents[1] / 'unity/prompts/formalize'
        for role in ('CHUNKING', 'FORMALIZING', 'CRITIC'):
            text = (prompts / f'{role}.md').read_text()
            with self.subTest(role=role):
                self.assertIn('verification_scope', text)
                self.assertIn('read-only', text)
                self.assertIn('excluded project module', text)


if __name__ == '__main__':
    unittest.main()
