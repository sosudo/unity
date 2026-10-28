"""Assistance is advisory, assignment-bound, and independent of writer leases."""
import asyncio
import os
import time
from unittest.mock import AsyncMock, patch
import unittest

from unity import bump_tasks, bump_workflow
from unity.forum import bump_workflow_server as forum
import test_bump_workflow_runtime as runtime


class AssistanceTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime.WorkflowRuntimeTests.setUp
    git = runtime.WorkflowRuntimeTests.git
    worktree = runtime.WorkflowRuntimeTests.worktree
    error = runtime.WorkflowRuntimeTests.error
    build_result = runtime.WorkflowRuntimeTests.build_result

    def assignment(self):
        state = self.controller.state()
        tid = next(k for k, t in state['tasks'].items() if t['files'] == ['A.lean'])
        bump_tasks.claim(self.paths.forum, 'Alice', tid, max_attempts=2)
        bump_tasks.configure_assignment(self.paths.forum, 'Alice', tid, self.root, self.base)
        forum.configure(self.paths.forum, self.root, 'repairing')
        return tid

    def request(self, tid):
        with patch.dict(os.environ, {'UNITY_AGENT_NAME': 'Alice', 'UNITY_BUMP_TASK_ID': tid}, clear=False):
            return forum.request_assistance('Alice', tid, 'Please inspect the exact committed base.')

    async def test_bound_answer_and_stale_assignment(self):
        tid = self.assignment()
        request = self.request(tid)
        with self.assertRaises(ValueError): self.request(tid)
        with bump_tasks.transaction(self.paths.forum) as state:
            bump_tasks._event(state, 'assistance_started', 'controller', task_id=tid,
                              request_id=request['event_id'], helper='Bob', deadline_at=time.time() + 300)
        forum.configure(self.paths.forum, self.root, 'assisting')
        env = {'UNITY_AGENT_NAME': 'Bob', 'UNITY_BUMP_TASK_ID': tid,
               'UNITY_BUMP_ASSISTANCE_REQUEST': request['event_id']}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(ValueError): forum.answer_assistance('Bob', 'wrong', 'advice')
            with self.assertRaises(ValueError): forum.answer_assistance('Mallory', request['event_id'], 'spoof')
            answer = forum.answer_assistance('Bob', request['event_id'], 'Check the typed transport.')
            self.assertEqual(answer['question_id'], request['event_id'])
            with self.assertRaises(ValueError): forum.answer_assistance('Bob', request['event_id'], 'again')
            context = forum.bump_task_context(tid)
            self.assertIn(answer['event_id'], [x['event_id'] for x in context['items']])
            with self.assertRaises(ValueError): forum.bump_task('other')
        self.assertNotIn(forum.submit_repair, forum.PROFILE_TOOLS['assisting'])
        self.assertNotIn(forum.request_configuration_change, forum.PROFILE_TOOLS['assisting'])
        self.assertNotIn(forum.claim_strategy, forum.PROFILE_TOOLS['assisting'])

    async def test_stale_request_refuses_answer(self):
        tid = self.assignment()
        request = self.request(tid)
        with bump_tasks.transaction(self.paths.forum) as state:
            bump_tasks._event(state, 'assistance_started', 'controller', task_id=tid,
                              request_id=request['event_id'], helper='Bob', deadline_at=time.time() + 300)
            state['tasks'][tid]['revision'] += 1
        forum.configure(self.paths.forum, self.root, 'assisting')
        with patch.dict(os.environ, {'UNITY_AGENT_NAME': 'Bob', 'UNITY_BUMP_TASK_ID': tid,
                                    'UNITY_BUMP_ASSISTANCE_REQUEST': request['event_id']}, clear=False):
            with self.assertRaises(ValueError): forum.answer_assistance('Bob', request['event_id'], 'stale')

    async def test_helper_reap_never_releases_writer_and_no_retry(self):
        tid = self.assignment()
        request = self.request(tid)
        self.roster.agents[0].model = 'model-a'
        self.roster.agents[1].model = 'model-b'
        async def helper(agent, request, tree, interrupt):
            await interrupt.wait()
        with patch.object(bump_workflow.bump_worktree, 'create_worktree', return_value=self.root) as create, \
             patch.object(bump_workflow.bump_worktree, 'symlink_lake_cache'), \
             patch.object(self.controller, 'assistance_worker', side_effect=helper):
            self.controller.launch_assistance()
            self.assertEqual(create.call_args.kwargs['base'], self.base)
            self.assertIn('Bob', self.controller.helpers)
            await asyncio.sleep(0)
            await self.controller.reconcile_helpers(drain=True)
            state = self.controller.state()
            self.assertEqual(state['tasks'][tid]['owner'], 'Alice')
            self.assertEqual(state['leases']['A.lean']['author'], 'Alice')
            self.assertEqual(state['tasks'][tid]['attempts'], 1)
            self.controller.launch_assistance()
            self.assertFalse(self.controller.helpers)
            self.assertEqual(sum(e.get('request_id') == request['event_id'] and e['kind'] == 'assistance_started'
                                 for e in self.controller.state()['events']), 1)

    async def test_preempts_for_ready_writer_and_does_not_dispatch_old_questions(self):
        tid = self.assignment()
        with patch.dict(os.environ, {'UNITY_AGENT_NAME': 'Alice', 'UNITY_BUMP_TASK_ID': tid}, clear=False):
            forum.ask_question('Alice', tid, 'Old ordinary question')
        self.controller.launch_assistance()
        self.assertFalse(self.controller.helpers)
        self.request(tid)
        async def helper(agent, request, tree, interrupt):
            await interrupt.wait()
        with patch.object(bump_workflow.bump_worktree, 'create_worktree', return_value=self.root), \
             patch.object(bump_workflow.bump_worktree, 'symlink_lake_cache'), \
             patch.object(self.controller, 'assistance_worker', side_effect=helper):
            self.controller.launch_assistance()
            self.assertIn('Bob', self.controller.helpers)
            await asyncio.sleep(0)
            await self.controller.reconcile_helpers(preempt=True)
            self.assertFalse(self.controller.helpers)
            self.assertEqual(self.controller.state()['leases']['A.lean']['author'], 'Alice')

    async def test_config_change_cancels_helper_and_preserves_lease(self):
        tid = self.assignment()
        self.request(tid)
        async def helper(agent, request, tree, interrupt):
            await interrupt.wait()
        with patch.object(bump_workflow.bump_worktree, 'create_worktree', return_value=self.root), \
             patch.object(bump_workflow.bump_worktree, 'symlink_lake_cache'), \
             patch.object(self.controller, 'assistance_worker', side_effect=helper):
            self.controller.launch_assistance()
            await asyncio.sleep(0)
            with bump_tasks.transaction(self.paths.forum) as state:
                state['config_requests']['x'] = {'status': 'pending'}
            await self.controller.reconcile_helpers()
            self.assertFalse(self.controller.helpers)
            self.assertEqual(self.controller.state()['leases']['A.lean']['author'], 'Alice')

    async def test_cleanup_failure_propagates_without_releasing_writer(self):
        tid = self.assignment()
        self.request(tid)
        async def helper(agent, request, tree, interrupt):
            raise bump_workflow.SpawnCleanupError('cleanup failed')
        with patch.object(bump_workflow.bump_worktree, 'create_worktree', return_value=self.root), \
             patch.object(bump_workflow.bump_worktree, 'symlink_lake_cache'), \
             patch.object(self.controller, 'assistance_worker', side_effect=helper):
            self.controller.launch_assistance()
            await asyncio.sleep(0)
            with self.assertRaises(bump_workflow.SpawnCleanupError):
                await self.controller.reconcile_helpers(drain=True)
            self.assertEqual(self.controller.state()['leases']['A.lean']['author'], 'Alice')

    async def test_capacity_is_cooled_and_request_not_retried(self):
        tid = self.assignment()
        self.request(tid)
        async def helper(agent, request, tree, interrupt):
            raise bump_workflow.BumpProviderCapacityError('busy')
        with patch.object(bump_workflow.bump_worktree, 'create_worktree', return_value=self.root), \
             patch.object(bump_workflow.bump_worktree, 'symlink_lake_cache'), \
             patch.object(self.controller, 'assistance_worker', side_effect=helper):
            self.controller.launch_assistance()
            await asyncio.sleep(0)
            await self.controller.reconcile_helpers()
            self.assertTrue(self.controller.capacity_cooling('Bob'))
            self.controller.launch_assistance()
            self.assertFalse(self.controller.helpers)

    async def test_expired_helper_is_stopped(self):
        tid = self.assignment()
        self.request(tid)
        async def helper(agent, request, tree, interrupt):
            await interrupt.wait()
        with patch.object(bump_workflow.bump_worktree, 'create_worktree', return_value=self.root), \
             patch.object(bump_workflow.bump_worktree, 'symlink_lake_cache'), \
             patch.object(self.controller, 'assistance_worker', side_effect=helper):
            self.controller.launch_assistance()
            await asyncio.sleep(0)
            self.controller.helpers['Bob']['deadline'] = 0
            await self.controller.reconcile_helpers()
            self.assertFalse(self.controller.helpers)
            self.assertEqual(self.controller.state()['leases']['A.lean']['author'], 'Alice')

    async def test_helper_checkout_uses_exact_base_even_after_main_moves(self):
        (self.root / 'A.lean').write_text('def a := 1\n')
        self.git(self.root, 'add', 'A.lean')
        self.git(self.root, 'commit', '-qm', 'later')
        tree = bump_workflow.bump_worktree.create_worktree('help-test', self.root, base=self.base)
        self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual((tree / 'A.lean').read_text(), 'def a := missingA\n')

    async def test_answer_rejected_after_terminal_event_or_deadline_across_reconfigure(self):
        tid = self.assignment()
        request = self.request(tid)
        with bump_tasks.transaction(self.paths.forum) as state:
            bump_tasks._event(state, 'assistance_started', 'controller', task_id=tid,
                              request_id=request['event_id'], helper='Bob', deadline_at=time.time() + 300)
            bump_tasks._event(state, 'assistance_finished', 'controller', task_id=tid,
                              request_id=request['event_id'], helper='Bob')
        # Reconfiguring is equivalent to a new server process reading persisted state.
        forum.configure(self.paths.forum, self.root, 'assisting')
        with patch.dict(os.environ, {'UNITY_AGENT_NAME': 'Bob', 'UNITY_BUMP_TASK_ID': tid,
                                    'UNITY_BUMP_ASSISTANCE_REQUEST': request['event_id']}, clear=False):
            with self.assertRaises(ValueError): forum.answer_assistance('Bob', request['event_id'], 'late')
        with bump_tasks.transaction(self.paths.forum) as state:
            state['events'].pop()
            next(e for e in state['events'] if e['kind'] == 'assistance_started')['deadline_at'] = time.time() - 1
        with patch.dict(os.environ, {'UNITY_AGENT_NAME': 'Bob', 'UNITY_BUMP_TASK_ID': tid,
                                    'UNITY_BUMP_ASSISTANCE_REQUEST': request['event_id']}, clear=False):
            with self.assertRaises(ValueError): forum.answer_assistance('Bob', request['event_id'], 'expired')

    async def test_answer_rejected_after_yield_submission_base_config_or_lease_change(self):
        tid = self.assignment()
        request = self.request(tid)
        with bump_tasks.transaction(self.paths.forum) as state:
            bump_tasks._event(state, 'assistance_started', 'controller', task_id=tid,
                              request_id=request['event_id'], helper='Bob', deadline_at=time.time() + 300)
        forum.configure(self.paths.forum, self.root, 'assisting')
        env = {'UNITY_AGENT_NAME': 'Bob', 'UNITY_BUMP_TASK_ID': tid,
               'UNITY_BUMP_ASSISTANCE_REQUEST': request['event_id']}
        mutations = [lambda s: s['tasks'][tid].update(yield_requested={'reason': 'handoff'}),
                     lambda s: s['tasks'][tid].update(status='submitted'),
                     lambda s: s['tasks'][tid].update(base_main_sha='f' * 40),
                     lambda s: s['config'].update(revision=123),
                     lambda s: s['leases']['A.lean'].update(author='Bob'),
                     lambda s: s['tasks'][tid].update(attempts=123)]
        import copy
        original = self.controller.state()
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                with bump_tasks.transaction(self.paths.forum) as state:
                    mutate(state)
                with patch.dict(os.environ, env, clear=False):
                    with self.assertRaises(ValueError): forum.answer_assistance('Bob', request['event_id'], 'stale')
                with bump_tasks.transaction(self.paths.forum) as state:
                    state.clear()
                    state.update(copy.deepcopy(original))

    async def test_helper_reads_grouped_predecessors_and_compacted_artifacts(self):
        from unity import artifacts
        tid = self.assignment()
        with bump_tasks.transaction(self.paths.forum) as state:
            state['tasks']['child'] = {'task_id': 'child', 'status': 'grouped', 'grouped_into': tid}
            state['tasks']['other'] = {'task_id': 'other', 'status': 'grouped', 'grouped_into': 'nowhere'}
            bump_tasks._event(state, 'finding', 'Alice', task_id='child', content='prior finding')
        forum.configure(self.paths.forum, self.root, 'assisting')
        artifact = artifacts.compact_text(self.paths.artifacts, 'large context content', kind='mcp_output', threshold=1)
        with patch.dict(os.environ, {'UNITY_AGENT_NAME': 'Bob', 'UNITY_BUMP_TASK_ID': tid}, clear=False):
            self.assertEqual(forum.bump_task_context('child')['items'][0]['content'], 'prior finding')
            with self.assertRaises(ValueError): forum.bump_task_context('other')
            first = forum.artifact_read(artifact['artifact_id'], limit=5)
            second = forum.artifact_read(artifact['artifact_id'], offset=first['next_offset'])
            self.assertEqual(first['content'] + second['content'], 'large context content')

    async def test_critic_sees_assistance_request_context(self):
        tid = self.assignment()
        request = self.request(tid)
        with bump_tasks.transaction(self.paths.forum) as state:
            state['phase'] = 'critic'
            state['review'] = {'snapshot': {'snapshot_id': 'r', 'main_sha': state['main_sha'],
                                             'config_revision': state['config']['revision']}}
        forum.configure(self.paths.forum, self.root, 'critic')
        items = forum.bump_review_context('r')['items']
        self.assertTrue(any(e['event_id'] == request['event_id'] and e['content'] == request['content'] for e in items))

    async def test_pending_configuration_does_not_preempt_helper_for_blocked_writer(self):
        tid = self.assignment()
        self.request(tid)
        async def helper(agent, request, tree, interrupt): await interrupt.wait()
        with patch.object(bump_workflow.bump_worktree, 'create_worktree', return_value=self.root), \
             patch.object(bump_workflow.bump_worktree, 'symlink_lake_cache'), \
             patch.object(self.controller, 'assistance_worker', side_effect=helper):
            self.controller.launch_assistance()
            await asyncio.sleep(0)
            self.controller.workers['Alice'] = {}
            with bump_tasks.transaction(self.paths.forum) as state:
                state['tasks']['config'] = bump_tasks._task('config', 'configuration', [], ['lakefile.lean'])
            await self.controller.reconcile_helpers(preempt=True)
            self.assertIn('Bob', self.controller.helpers)
            await self.controller.reconcile_helpers(drain=True)

    async def test_helper_receives_bound_identity_and_project_instructions(self):
        tid = self.assignment()
        request = self.request(tid)
        self.paths.unity_md.parent.mkdir(parents=True, exist_ok=True)
        self.paths.unity_md.write_text('Project-specific test instruction')
        with patch.object(bump_workflow, 'spawn', new=AsyncMock(return_value='done')) as spawn:
            await self.controller.assistance_worker(self.roster.agents[1], request, self.root, asyncio.Event())
        system = spawn.call_args.args[1]
        self.assertIn('You are Bob.', system)
        self.assertIn('Team: Alice, Bob.', system)
        self.assertIn('Primary: Alice.', system)
        self.assertIn('Project-specific test instruction', system)
        self.assertEqual(set(spawn.call_args.args[4]), {'unity-forum', 'lean-lsp'})

    async def test_assisting_facade_exposes_artifact_reader_for_compacted_cli_output(self):
        from unity.forum import bump_server
        bump_server.configure(self.paths.forum, self.root, 'assisting')
        names = {tool.name for tool in await bump_server.build_server('assisting').list_tools()}
        self.assertIn('artifact_read', names)
        self.assertIn('answer_assistance', names)
        self.assertNotIn('submit_repair', names)
