"""A retired rename obligation must not block repair of its live destination."""
import asyncio
import fcntl
from copy import deepcopy
from unittest.mock import AsyncMock, patch
import unittest

from unity import bump_tasks
from unity.forum import bump_workflow_server as forum
import test_bump_workflow_runtime as runtime


class RenamedDependencyTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime.WorkflowRuntimeTests.setUp
    git = runtime.WorkflowRuntimeTests.git
    worktree = runtime.WorkflowRuntimeTests.worktree
    error = runtime.WorkflowRuntimeTests.error
    build_result = runtime.WorkflowRuntimeTests.build_result
    renamed_command_fixture = runtime.WorkflowRuntimeTests.renamed_command_fixture
    renamed_target_build = runtime.WorkflowRuntimeTests.renamed_target_build
    authoritative_build_result = runtime.WorkflowRuntimeTests.authoritative_build_result

    async def blocked_fixture(self):
        old, fixture_current = self.renamed_command_fixture()
        # A dependent module imports the destination and must retain its edge.
        (self.root / 'B.lean').write_text('import New\ndef b := 0\n')
        self.git(self.root, 'add', 'B.lean')
        self.git(self.root, 'commit', '-qm', 'dependent module')
        head = self.git(self.root, 'rev-parse', 'HEAD')
        current = 'command-' + bump_tasks.digest(['New.lean', 'import'])[:16]
        root_task = 'command-' + bump_tasks.digest(['B.lean', 'import'])[:16]
        new_error = {'id': 'new-import', 'path': 'New.lean', 'kind': 'import', 'line': None,
                     'severity': 'error', 'message': "bad import 'Old'"}
        root_error = {'id': 'root-import', 'path': 'B.lean', 'kind': 'import', 'line': None,
                      'severity': 'error', 'message': "bad import 'New'"}
        with bump_tasks.transaction(self.paths.forum) as state:
            state['main_sha'] = head
            moved = state['tasks'].pop(fixture_current)
            moved['task_id'] = current
            state['tasks'][current] = moved
        self.controller.refresh(self.build_result([new_error, root_error]))
        bump_tasks.claim(self.paths.forum, 'Alice', current, max_attempts=5)
        tree = self.worktree(self.paths, 'Alice', current)
        bump_tasks.configure_assignment(self.paths.forum, 'Alice', current, tree, head)
        revision = self.controller.state()['tasks'][current]['revision']
        bump_tasks.request_scope(self.paths.forum, 'Alice', current, [], ['New.lean'],
                                 'group adjacent declarations', revision, include_file_chunks=True)
        (tree / 'New.lean').write_text('def a := 2\n')
        forum.configure(self.paths.forum, self.root)
        candidate = forum.submit_repair('Alice', current, 'partial repair')
        with patch.object(self.controller, 'integrate', return_value={'accepted': False, 'reason': 'not reached'}), \
             patch.object(self.controller, 'build', new=AsyncMock(return_value=self.build_result([new_error, root_error]))):
            self.assertTrue(await self.controller.integrate_ready())
        state = self.controller.state()
        self.assertEqual(state['candidates'][candidate['candidate_id']]['status'], 'rejected')
        self.assertIn(old, state['tasks'][current]['dependencies'])
        self.assertIn(old, state['tasks'][root_task]['dependencies'])
        self.assertIn(current, state['tasks'][root_task]['dependencies'])
        return old, current, root_task, candidate

    async def test_rejected_grouped_import_retries_without_retiring_legacy(self):
        old, current, root_task, _ = await self.blocked_fixture()
        before = deepcopy(self.controller.state()['tasks'][old])
        self.controller.reconcile_renamed_import_dependencies()
        state = self.controller.state()
        self.assertEqual(state['tasks'][old], before)
        self.assertNotIn(old, state['tasks'][current]['dependencies'])
        self.assertIn(old, state['tasks'][root_task]['dependencies'])
        self.assertIn(current, state['tasks'][root_task]['dependencies'])
        event = next(e for e in state['events'] if e['kind'] == 'renamed_import_dependency_reconciled')
        self.assertEqual(event['aliases']['A.lean']['candidate_id'], 'candidate-move')
        with patch.object(self.controller, 'worker', new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual([x['task_id'] for x in self.controller.workers.values()], [current])
            await asyncio.gather(*(x['task'] for x in self.controller.workers.values()))
            await self.controller.reap_workers()
        self.assertEqual(self.controller.state()['tasks'][old]['status'], 'pending')
        good = self.renamed_target_build(checked=['New.lean'])
        with patch.object(self.controller, 'build', new=AsyncMock(return_value=good)):
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
        self.assertEqual(self.controller.state()['tasks'][old]['status'], 'complete')
        self.assertTrue(any(e['kind'] == 'renamed_command_revalidated' for e in self.controller.state()['events']))

    async def test_partial_import_merge_then_destination_declaration_is_eligible(self):
        old, current, root_task, candidate = await self.blocked_fixture()
        # Represent a later accepted partial import repair: commit source and
        # expose a newly failing declaration in the renamed module.
        (self.root / 'New.lean').write_text('def a := 2\ndef later := missingLater\n')
        self.git(self.root, 'add', 'New.lean')
        self.git(self.root, 'commit', '-qm', 'partial import repair')
        head = self.git(self.root, 'rev-parse', 'HEAD')
        with bump_tasks.transaction(self.paths.forum) as state:
            state['main_sha'] = head
            state['tasks'][current]['status'] = 'complete'
            state['candidates'][candidate['candidate_id']].update(status='merged', merged_sha=head)
        later = {'id': 'later-error', 'path': 'New.lean', 'kind': 'declaration', 'line': 2,
                 'severity': 'error', 'message': 'unknown missingLater'}
        self.controller.refresh(self.build_result([later]))
        state = self.controller.state()
        repair = next(k for k, t in state['tasks'].items() if t['kind'] == 'repair'
                      and t['files'] == ['New.lean'] and t.get('diagnostics'))
        self.assertIn(old, state['tasks'][repair]['dependencies'])
        self.controller.reconcile_renamed_import_dependencies()
        state = self.controller.state()
        self.assertNotIn(old, state['tasks'][repair]['dependencies'])
        self.assertEqual(state['tasks'][old]['status'], 'pending')
        self.assertIn(old, state['tasks'][root_task]['dependencies'])
        self.assertIn(repair, state['tasks'][root_task]['dependencies'])
        with patch.object(self.controller, 'worker', new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual([x['task_id'] for x in self.controller.workers.values()], [repair])
            await asyncio.gather(*(x['task'] for x in self.controller.workers.values()))
            await self.controller.reap_workers()

    async def test_unproven_or_independent_obligations_keep_the_edge(self):
        old, current, _, _ = await self.blocked_fixture()
        original = self.controller.state()
        def unmerged(s): s['candidates']['candidate-move']['status'] = 'rejected'
        def wrong_revision(s): s['candidates']['candidate-move']['task_revision'] -= 1
        def wrong_mapping(s): s['tasks']['config']['requested_config']['module_renames'] = {'A': 'B'}
        def live_import(s): s['tasks'][old]['diagnostics'].append({'path': 'New.lean', 'kind': 'import', 'severity': 'error'})
        def extra_file(s): s['tasks'][old]['files'].append('B.lean')
        def owned(s): s['leases']['New.lean'] = {'task_id': 'other', 'author': 'Bob'}
        def submitted(s): s['candidates']['pending'] = {'task_id': current, 'status': 'submitted'}
        def missing_chunk(s): s['tasks'][current]['chunk_ids'].append('unknown')
        def manual_edge(s): s['tasks'][current]['dependency_basis'] = 'explicit user obligation'
        def unlocated(s): s['tasks'][current]['diagnostics'] = [{'path': 'New.lean', 'kind': 'declaration', 'line': None, 'severity': 'error'}]
        def outside_chunk(s): s['tasks'][current]['diagnostics'] = [{'path': 'New.lean', 'kind': 'declaration', 'line': 999, 'severity': 'error'}]
        for mutate in (unmerged, wrong_revision, wrong_mapping, live_import, extra_file, owned, submitted, missing_chunk, manual_edge, unlocated, outside_chunk):
            with self.subTest(case=mutate.__name__):
                with bump_tasks.transaction(self.paths.forum) as state: mutate(state)
                self.controller.reconcile_renamed_import_dependencies()
                self.assertIn(old, self.controller.state()['tasks'][current]['dependencies'])
                with bump_tasks.transaction(self.paths.forum) as state:
                    state.clear()
                    state.update(deepcopy(original))

    async def test_true_other_dependency_remains_and_blocks_dispatch(self):
        old, current, _, _ = await self.blocked_fixture()
        with bump_tasks.transaction(self.paths.forum) as state:
            state['tasks']['real-prerequisite'] = {**bump_tasks._task('real-prerequisite', 'command', [], ['B.lean']),
                                                   'status': 'pending'}
            state['tasks'][current]['dependencies'].append('real-prerequisite')
        self.controller.reconcile_renamed_import_dependencies()
        state = self.controller.state()
        self.assertNotIn(old, state['tasks'][current]['dependencies'])
        self.assertIn('real-prerequisite', state['tasks'][current]['dependencies'])
        with patch.object(self.controller, 'worker', new=AsyncMock()):
            self.controller.launch_ready()
            self.assertNotIn(current, [x['task_id'] for x in self.controller.workers.values()])
            await asyncio.gather(*(x['task'] for x in self.controller.workers.values()))
            await self.controller.reap_workers()

    async def test_dirty_or_moved_main_never_relaxes_the_edge(self):
        old, current, _, _ = await self.blocked_fixture()
        (self.root / 'New.lean').write_text('def a := 999\n')
        self.controller.reconcile_renamed_import_dependencies()
        self.assertIn(old, self.controller.state()['tasks'][current]['dependencies'])
        self.git(self.root, 'add', 'New.lean')
        self.git(self.root, 'commit', '-qm', 'unrecorded external change')
        self.controller.reconcile_renamed_import_dependencies()
        self.assertIn(old, self.controller.state()['tasks'][current]['dependencies'])

    async def test_git_evidence_and_edge_update_wait_for_integration_lock(self):
        old, current, _, _ = await self.blocked_fixture()
        lock_path = self.paths.forum / 'integration.lock'
        with lock_path.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            job = asyncio.create_task(asyncio.to_thread(self.controller.reconcile_renamed_import_dependencies))
            await asyncio.sleep(0.1)
            self.assertFalse(job.done())
            self.assertIn(old, self.controller.state()['tasks'][current]['dependencies'])
            fcntl.flock(lock, fcntl.LOCK_UN)
            await job
        self.assertNotIn(old, self.controller.state()['tasks'][current]['dependencies'])

    async def test_state_change_after_rename_proof_fails_closed(self):
        from unity import bump_workflow
        old, current, _, _ = await self.blocked_fixture()
        original = bump_workflow.accepted_rename_paths
        def changing(root, state, task):
            result = original(root, state, task)
            with bump_tasks.transaction(self.paths.forum) as fresh:
                fresh['tasks'][old]['diagnostics'].append({'path': 'New.lean', 'kind': 'import'})
            return result
        with patch.object(bump_workflow, 'accepted_rename_paths', side_effect=changing):
            self.controller.reconcile_renamed_import_dependencies()
        self.assertIn(old, self.controller.state()['tasks'][current]['dependencies'])
