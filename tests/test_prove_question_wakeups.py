import asyncio
import fcntl
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unity import prove_runtime
from unity.forum import server
from unity.prove_questions import QuestionWakeups


def question(qid, to='Luna1', author='Terra1', chunk='goal', status='open'):
    return {'post_id': qid, 'act': 'question', 'author': author,
            'fields': {'to': to, 'chunk': chunk, 'status': status}}


class QuestionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.forum = Path(self.temp.name)
        self.agents = {'Luna1': object(), 'Terra1': object()}
        self.state = {'declarations': {'goal': {'status': 'unresolved', 'chunk_id': 'chunk-goal'}}}

    def write(self, posts):
        (self.forum / 'qa.json').write_text(json.dumps({'posts': posts}))

    def test_shared_lock_uses_a_readable_descriptor(self):
        self.write([question('q1')])
        original_flock = fcntl.flock
        def nfs_flock(handle, operation):
            if operation == fcntl.LOCK_SH:
                self.assertTrue(handle.readable(), 'NFS shared record locks need read access')
            return original_flock(handle, operation)
        with patch('unity.prove_questions.fcntl.flock', side_effect=nfs_flock):
            self.assertEqual(QuestionWakeups(self.forum).poll(self.state, self.agents, {}, {}),
                             [('Luna1', 'goal', 'q1', 'Terra1')])

    def test_active_waits_then_once_only_across_runtime_instances(self):
        self.write([question('q1', to='luna1')])
        reader = QuestionWakeups(self.forum)
        self.assertEqual(reader.poll(self.state, self.agents, {'Luna1': object()}, {}), [])
        self.assertEqual(reader.poll(self.state, self.agents, {}, {}), [('Luna1', 'goal', 'q1', 'Terra1')])
        self.assertEqual(reader.poll(self.state, self.agents, {}, {}), [])
        self.assertEqual(QuestionWakeups(self.forum).poll(self.state, self.agents, {}, {}), [])

    def test_ignored_and_ambiguous_questions(self):
        self.write([question('answered', status='answered'), question('self', author='Luna1'),
                    question('unknown', to='Luna'), question('empty', to=''),
                    question('missing', chunk='wrong'), question('solved', chunk='solved'),
                    question('pending', chunk='pending'), question('q2', chunk='chunk-goal')])
        self.state['declarations'].update({'solved': {'status': 'solved'},
                                           'pending': {'status': 'candidate_pending'}})
        reader = QuestionWakeups(self.forum)
        self.assertEqual(reader.poll(self.state, self.agents, {}, {}), [('Luna1', 'goal', 'q2', 'Terra1')])
        self.assertEqual(reader.poll(self.state, self.agents, {}, {}), [])

    def test_pending_answered_before_idle_never_wakes(self):
        row = question('q')
        self.write([row])
        reader = QuestionWakeups(self.forum)
        self.assertEqual(reader.poll(self.state, self.agents, {'Luna1': object()}, {}), [])
        row['fields']['status'] = 'answered'
        self.write([row])
        self.assertEqual(reader.poll(self.state, self.agents, {}, {}), [])
        self.assertEqual(QuestionWakeups(self.forum).poll(self.state, self.agents, {}, {}), [])

    def test_pending_question_for_candidate_or_solved_target_is_not_woken(self):
        for status in ('candidate_pending', 'solved'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                forum = Path(tmp)
                (forum / 'qa.json').write_text(json.dumps({'posts': [question('q')]}))
                reader = QuestionWakeups(forum)
                state = {'declarations': {'goal': {'status': 'unresolved'}}}
                self.assertEqual(reader.poll(state, self.agents, {'Luna1': object()}, {}), [])
                state['declarations']['goal']['status'] = status
                self.assertEqual(reader.poll(state, self.agents, {}, {}), [])
                self.assertEqual(QuestionWakeups(forum).poll(state, self.agents, {}, {}), [])

    def test_casefold_collision_is_not_routed(self):
        self.write([question('q', to='LUNA1')])
        reader = QuestionWakeups(self.forum)
        self.assertEqual(reader.poll(self.state, {'Luna1': object(), 'luna1': object()}, {}, {}), [])

    def test_fallback_only_for_unique_or_known_target(self):
        self.write([question('q', chunk='')])
        self.state['declarations']['other'] = {'status': 'unresolved'}
        self.assertEqual(QuestionWakeups(self.forum).poll(self.state, self.agents, {}, {}), [])
        self.write([question('q', chunk=''), question('q2', chunk='')])
        self.assertEqual(QuestionWakeups(self.forum).poll(self.state, self.agents, {}, {'Luna1': 'other'}),
                         [('Luna1', 'other', 'q2', 'Terra1')])


class RuntimeQuestionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_runtime_ignores_legacy_questions_across_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forum = root / '.unity' / 'forum'
            forum.mkdir(parents=True)
            unity = forum.parent
            (unity / 'dag.json').write_text('{}')
            paths = SimpleNamespace(project_root=root, unity=unity, forum=forum, unity_md=unity / 'UNITY.md')
            agents = [SimpleNamespace(name='Luna1', backend='codex'), SimpleNamespace(name='Terra1', backend='codex')]
            roster = SimpleNamespace(agents=agents)
            state = {'declarations': {'goal': {'status': 'unresolved', 'chunk_id': 'goal'}}, 'events': [], 'revision': 0}
            started = asyncio.Event()
            created = asyncio.Event()
            released = asyncio.Event()
            calls = []
            phase = [1]
            async def worker(agent, system, prompt, cwd, mcp, **kwargs):
                calls.append((phase[0], agent.name, prompt))
                if phase[0] == 1 and agent.name == 'Luna1' and len([x for x in calls if x[1] == 'Luna1']) == 1:
                    started.set()
                    await created.wait()
                    await asyncio.sleep(0.8)
                    released.set()
                elif phase[0] == 1 and agent.name == 'Terra1':
                    await started.wait()
                    server.forum_question('Terra1', 'Can you help?', to='luna1', chunk='goal')
                    created.set()
            with ExitStack() as stack:
                stack.enter_context(patch.object(server, 'FORUM_DIR', forum))
                stack.enter_context(patch.object(prove_runtime, '_prove_worktree', lambda root, unity, name, **k: root))
                stack.enter_context(patch.object(prove_runtime.worktree, 'main_commit', lambda *a: 'sha'))
                stack.enter_context(patch.object(prove_runtime.prove_state, 'initialize_from_dag', lambda *a, **k: state))
                stack.enter_context(patch.object(prove_runtime.prove_state, 'load_state', lambda *a: state))
                stack.enter_context(patch.object(prove_runtime.prove_state, 'release_author_claims', lambda *a: []))
                stack.enter_context(patch.object(prove_runtime, 'load_prompt', lambda *a: ''))
                stack.enter_context(patch.object(prove_runtime, '_preamble', lambda *a, **k: ''))
                stack.enter_context(patch.object(prove_runtime, '_workspace_brief', lambda *a: ''))
                stack.enter_context(patch.object(prove_runtime, '_prove_workspace_context', lambda *a: ''))
                stack.enter_context(patch.object(prove_runtime, '_target_context', lambda *a: ''))
                stack.enter_context(patch.object(prove_runtime.library, 'library_context', lambda: ''))
                stack.enter_context(patch.object(prove_runtime.library, 'library_subagents', lambda: []))
                stack.enter_context(patch.object(prove_runtime, 'spawn', side_effect=worker))
                stack.enter_context(patch.object(prove_runtime, 'stop_requested', lambda *a: False))
                await asyncio.wait_for(prove_runtime.run_prove_runtime(roster, paths, {}, ''), 8)
                phase[0] = 2
                await asyncio.wait_for(prove_runtime.run_prove_runtime(roster, paths, {}, ''), 8)
            wake = [row for row in calls if 'Forum question' in row[2]]
            self.assertEqual(wake, [])
            self.assertTrue(released.is_set())
            self.assertEqual([(phase, name) for phase, name, _ in calls],
                             [(1, 'Luna1'), (1, 'Terra1'), (2, 'Luna1'), (2, 'Terra1')])


if __name__ == '__main__':
    unittest.main()
