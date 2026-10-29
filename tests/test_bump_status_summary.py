"""Compact status is opt-in, bounded, and preserves the legacy result."""
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from asyncclick.testing import CliRunner
from fastmcp import Client
from unity import bump_tasks, bump_spawn
from unity.commands import mcp as command
from unity.config import Paths
from unity.forum import bump_workflow_server as forum
from test_bump_native_mcp_policy import servers_for


class StatusSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.paths = Paths.from_unity_dir(self.root / '.unity')
        bump_tasks.initialize(self.paths.forum / 'bump', self.root, run_id='test', base_sha='a' * 40,
                              version='v4.34.1', config={'revision': 4, 'scope': {
                                  'selected_modules': {f'M{i}': f'M{i}.lean' for i in range(300)},
                                  'default_modules': {'M0': 'M0.lean'}, 'excluded_files': {'Old.lean': 'reason'}}})
        self.dir = self.paths.forum / 'bump'
        with bump_tasks.transaction(self.dir) as state:
            for i in range(567):
                tid = f'task-{i:04}'
                state['tasks'][tid] = {'task_id': tid, 'kind': 'repair', 'status': 'pending' if i % 2 else 'complete',
                    'files': ['A.lean'], 'chunk_ids': [f'chunk-{j}' for j in range(60)], 'owner': None,
                    'revision': i, 'attempts': 1, 'diagnostics': ['large diagnostic ' * 100]}
            for i in range(323):
                cid = f'candidate-{i:04}'
                state['candidates'][cid] = {'candidate_id': cid, 'task_id': f'task-{i:04}', 'status': 'merged',
                    'author': 'Luna', 'task_revision': i, 'notes': 'large notes ' * 100}
            state['review'] = {'reviewer': 'Sol', 'snapshot': {'snapshot_id': 'snap-1', 'passed': True,
                'builds': ['large snapshot ' * 1000]}, 'verdict': {'verdict': 'approved', 'approved': True,
                'reasoning': 'large reasoning ' * 1000}}
        self.paths.unity.mkdir(exist_ok=True)
        (self.paths.unity / 'state.json').write_text(json.dumps({'command': 'bump', 'phase': 'repairing'}))
        forum.configure(self.dir, self.root, 'repairing')

    def test_legacy_summary_compact_and_no_mutation(self):
        state = bump_tasks.load(self.dir)
        before = (self.dir / bump_tasks.STATE_FILE).read_bytes()
        legacy = forum.bump_status()
        self.assertEqual(legacy, {k: state[k] for k in ('run_id', 'revision', 'phase', 'main_sha', 'target_version', 'config', 'review')} | {
            'task_counts': {'complete': 284, 'pending': 283}, 'tasks': list(state['tasks'].values()),
            'candidates': list(state['candidates'].values())})
        summary = forum.bump_status(summary=True)
        self.assertGreater(len(json.dumps(legacy)), 1000000)
        self.assertLess(len(json.dumps(summary)), 1500)
        self.assertEqual(summary['scope_counts']['selected_modules'], 300)
        self.assertEqual(summary['candidate_counts'], {'merged': 323})
        self.assertEqual(summary['review'], {'snapshot_id': 'snap-1', 'passed': True, 'reviewer': 'Sol',
                                             'verdict': 'approved', 'approved': True})
        self.assertNotIn('config', summary)
        self.assertEqual(before, (self.dir / bump_tasks.STATE_FILE).read_bytes())
        with self.assertRaises(ValueError): forum.bump_status(summary='true')

    def test_pages_are_bounded_sorted_and_read_only(self):
        before = (self.dir / bump_tasks.STATE_FILE).read_bytes()
        for kind, total in [('tasks', 567), ('candidates', 323)]:
            first = forum.bump_status_page(kind, limit=2)
            second = forum.bump_status_page(kind, offset=2, limit=2)
            key = 'task_id' if kind == 'tasks' else 'candidate_id'
            self.assertEqual([x[key] for x in first['items'] + second['items']],
                             [f'{kind[:-1]}-{i:04}' for i in range(4)])
            self.assertEqual(first['total'], total)
            self.assertEqual(first['next_offset'], 2)
            self.assertEqual(first['revision'], bump_tasks.load(self.dir)['revision'])
            self.assertIsNone(forum.bump_status_page(kind, offset=total)['next_offset'])
            self.assertLess(len(json.dumps(forum.bump_status_page(kind, limit=50))), 12000)
            self.assertNotIn('notes', json.dumps(first))
            self.assertNotIn('diagnostics', json.dumps(first))
            for offset, limit in [(-1, 1), (True, 1), (0, 0), (0, 51), (0, True)]:
                with self.assertRaises(ValueError): forum.bump_status_page(kind, offset, limit)
        with self.assertRaises(ValueError): forum.bump_status_page('other')
        self.assertEqual(before, (self.dir / bump_tasks.STATE_FILE).read_bytes())

    def test_pages_report_current_revision_when_state_changes(self):
        first = forum.bump_status_page('tasks', limit=2)
        with bump_tasks.transaction(self.dir) as state:
            state['tasks']['task-0000']['status'] = 'running'
        later = forum.bump_status_page('tasks', limit=2)
        self.assertGreater(later['revision'], first['revision'])
        self.assertEqual(later['items'][0]['status'], 'running')
        self.assertIn('may change', later['qualification'])

    async def test_native_catalog_and_actual_mcp_summary(self):
        for profile in ('preparing', 'repairing', 'critic', 'retrospective'):
            with self.subTest(profile=profile):
                tools = {t.name for t in await forum.build_server(profile).list_tools()}
                self.assertTrue({'artifact_read', 'bump_status', 'bump_status_page'} <= tools)
                policy = bump_spawn._bump_codex_tool_policy(servers_for(profile, workflow=True), profile)
                self.assertTrue({'artifact_read', 'bump_status', 'bump_status_page'} <= set(policy['unity-forum']))
        self.assertNotIn('bump_status', {t.name for t in await forum.build_server('assisting').list_tools()})
        async with Client(forum.build_server('repairing')) as client:
            result = await client.call_tool('bump_status', {'summary': True})
            self.assertLess(len(json.dumps(result.structured_content)), 1500)
            self.assertEqual(result.structured_content['task_total'], 567)

    async def test_cli_compacted_status_artifact_can_be_read_by_normal_profile(self):
        with patch('unity.config.load_paths', return_value=self.paths), \
             patch.dict('os.environ', {'UNITY_BUMP_PROFILE': 'repairing'}):
            runner = CliRunner()
            summary = await runner.invoke(command.command, ['unity-forum', 'bump_status', '{\"summary\": true}'])
            self.assertEqual(summary.exit_code, 0, summary.output)
            self.assertIn('task_total', summary.output)
            self.assertNotIn('artifact-', summary.output)
            status = await runner.invoke(command.command, ['unity-forum', 'bump_status', '{}'])
            self.assertEqual(status.exit_code, 0, status.output)
            match = re.search(r'artifact-[0-9a-f]{12}', status.output)
            self.assertIsNotNone(match, status.output)
            page = await runner.invoke(command.command, ['unity-forum', 'artifact_read',
                                       json.dumps({'artifact_id': match.group(), 'offset': 0, 'limit': 100})])
            self.assertEqual(page.exit_code, 0, page.output)
            self.assertIn('next_offset', page.output)
