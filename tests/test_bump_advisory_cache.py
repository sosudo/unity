"""Advisory cache copies never become controller validation evidence."""
import asyncio
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import time
from threading import Event
import unittest
from unittest.mock import AsyncMock, patch

from unity import bump_advisory_cache as cache, bump_tasks, bump_workflow, bump_worktree
import test_bump_workflow_runtime as runtime


class AdvisoryCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='advisory-cache-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / 'project'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / '.gitignore').write_text('.lake/\n.unity/\n.worktrees/\n')
        (self.root / 'A.lean').write_text('def a := 1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.paths = bump_workflow.paths_for(self.root)
        self.config = {'revision': 0, 'scope': {'build_dir': '.lake/build'}, 'lake_args': []}
        bump_tasks.initialize(self.paths.forum, self.root, run_id='fixture', base_sha=self.base,
                              version='fixture', config=self.config)
        (self.root / '.lake/packages').mkdir(parents=True)
        (self.root / '.lake/build/lib').mkdir(parents=True)
        (self.root / '.lake/build/lib/A.olean').write_bytes(b'cached fixture')
        self.tree = bump_worktree.create_worktree('fixture', self.root)
        bump_worktree.symlink_lake_cache(self.tree, self.root)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def snapshot(self, base=None, config=None):
        return cache._snapshot(self.paths, self.tree, base or self.base, config or self.config, Event())

    def assert_cold_clean(self):
        self.assertFalse((self.tree / '.lake/build').exists())
        self.assertEqual(list((self.tree / '.lake').glob('.advisory-seed-*')), [])

    def test_independent_copy_preserves_main_and_is_cold_when_destination_exists(self):
        self.assertTrue(self.snapshot())
        src = self.root / '.lake/build/lib/A.olean'
        dst = self.tree / '.lake/build/lib/A.olean'
        self.assertEqual(dst.read_bytes(), src.read_bytes())
        self.assertNotEqual(dst.stat().st_ino, src.stat().st_ino)
        dst.write_bytes(b'changed private')
        self.assertEqual(src.read_bytes(), b'cached fixture')
        self.assertFalse(self.snapshot())
        self.assertEqual(dst.read_bytes(), b'changed private')

    def test_stale_dirty_source_or_config_and_helper_base_stay_cold(self):
        self.assertFalse(self.snapshot(base='f' * 40))
        self.assertFalse(self.snapshot(config={**self.config, 'revision': 1}))
        (self.root / 'A.lean').write_text('def a := 2\n')
        self.assertFalse(self.snapshot())
        self.assert_cold_clean()

    def test_symlink_hardlink_size_timeout_and_unsupported_layout_roll_back(self):
        root_build = self.root / '.lake/build'
        (root_build / 'unsafe').symlink_to('/etc/passwd')
        self.assertFalse(self.snapshot())
        (root_build / 'unsafe').unlink()
        os.link(root_build / 'lib/A.olean', root_build / 'hardlink')
        self.assertFalse(self.snapshot())
        (root_build / 'hardlink').unlink()
        with patch.object(cache, '_MAX_BYTES', 1): self.assertFalse(self.snapshot())
        with patch.object(cache, '_MAX_ENTRIES', 1): self.assertFalse(self.snapshot())
        with patch.object(cache, '_SECONDS', 0): self.assertFalse(self.snapshot())
        self.assertFalse(self.snapshot(config={**self.config, 'scope': {'build_dir': '../unsafe'}}))
        self.assert_cold_clean()

    def test_busy_lock_and_preexisting_destination_stay_cold(self):
        lock_path = self.paths.unity / 'forum/bump-build.lock'
        with lock_path.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(self.snapshot())
        self.assert_cold_clean()
        (self.tree / '.lake/build').mkdir()
        (self.tree / '.lake/build/owned').write_text('preserve')
        self.assertFalse(self.snapshot())
        self.assertEqual((self.tree / '.lake/build/owned').read_text(), 'preserve')

    def test_changed_identity_after_copy_rolls_back(self):
        with patch.object(cache, '_state_identity', side_effect=[True, False]):
            self.assertFalse(self.snapshot())
        self.assert_cold_clean()

    def test_mid_copy_cancel_and_cooperative_timeout_roll_back(self):
        original_read = cache.os.read
        for mode in ('cancel', 'deadline'):
            cancellation = Event()
            def slow_read(fd, length):
                result = original_read(fd, length)
                if mode == 'cancel': cancellation.set()
                else: time.sleep(0.03)
                return result
            def fake_git(root, *args):
                return self.base if args[0] == 'rev-parse' else ''
            with self.subTest(mode=mode), patch.object(cache, '_state_identity', return_value=True), \
                 patch.object(cache, '_git', side_effect=fake_git), \
                 patch.object(cache.os, 'read', side_effect=slow_read), \
                 patch.object(cache, '_SECONDS', 0.01 if mode == 'deadline' else 3):
                self.assertFalse(cache._snapshot(self.paths, self.tree, self.base, self.config, cancellation))
            self.assert_cold_clean()

    def test_source_lake_symlink_and_outside_destination_rejected(self):
        self.assertFalse(cache._snapshot(self.paths, self.root, self.base, self.config, Event()))
        (self.root / '.lake/build').rename(self.root / '.lake/old-build')
        (self.root / '.lake/build').symlink_to(self.root / '.lake/old-build')
        self.assertFalse(self.snapshot())
        self.assert_cold_clean()

    def test_symlinked_destination_cannot_escape_private_tree(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (self.tree / '.lake').rename(self.tree / '.lake-original')
        (self.tree / '.lake').symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.snapshot())
        self.assertEqual(list(outside.iterdir()), [])

    def test_copy_error_and_cleanup_failure_are_not_silent(self):
        with patch.object(cache, '_CHUNK', -1):
            self.assertFalse(self.snapshot())
        self.assert_cold_clean()
        with patch.object(cache, '_CHUNK', -1), \
             patch.object(cache.shutil, 'rmtree', side_effect=OSError('cleanup')):
            with self.assertRaises(cache.SpawnCleanupError): self.snapshot()

    def test_destination_reservation_cleanup_failure_is_fatal(self):
        original_rmdir = cache.os.rmdir
        def fail_build(name, *args, **kwargs):
            if name == 'build': raise OSError('fixture reservation cleanup')
            return original_rmdir(name, *args, **kwargs)
        with patch.object(cache.os, 'rename', side_effect=OSError('fixture rename')), \
             patch.object(cache.os, 'rmdir', side_effect=fail_build):
            with self.assertRaises(cache.SpawnCleanupError): self.snapshot()
        self.assertEqual(list((self.tree / '.lake').glob('.advisory-seed-*')), [])

    async def test_interrupt_joins_copy_before_return(self):
        started, release, finished = Event(), Event(), Event()
        def copy(paths, tree, base, config, cancel):
            started.set()
            release.wait(5)
            finished.set()
            return False
        interrupt = asyncio.Event()
        with patch.object(cache, '_snapshot', side_effect=copy):
            task = asyncio.create_task(cache.seed(self.paths, self.tree, self.base, self.config, interrupt))
            await asyncio.to_thread(started.wait, 2)
            interrupt.set()
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            release.set()
            self.assertFalse(await task)
            self.assertTrue(finished.is_set())

    async def test_cancellation_joins_copy_before_return(self):
        started, release, finished = Event(), Event(), Event()
        def copy(paths, tree, base, config, cancel):
            started.set()
            release.wait(5)
            finished.set()
            return False
        with patch.object(cache, '_snapshot', side_effect=copy):
            task = asyncio.create_task(cache.seed(self.paths, self.tree, self.base, self.config, asyncio.Event()))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(finished.is_set())


class AdvisoryRoleTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime.WorkflowRuntimeTests.setUp
    git = runtime.WorkflowRuntimeTests.git
    worktree = runtime.WorkflowRuntimeTests.worktree
    error = runtime.WorkflowRuntimeTests.error
    build_result = runtime.WorkflowRuntimeTests.build_result

    def owned_request(self):
        tid = next(iter(self.controller.state()['tasks']))
        bump_tasks.claim(self.paths.forum, 'Alice', tid)
        bump_tasks.configure_assignment(self.paths.forum, 'Alice', tid, self.root, self.base)
        assignment = self.controller.state()['tasks'][tid]
        request = {'task_id': tid, 'event_id': 'event-fixture', 'content': 'help', 'author': 'Alice',
                   'base_main_sha': self.base, 'task_revision': assignment['revision'],
                   'attempt': assignment['attempts'], 'config_revision': assignment['config_revision']}
        return tid, request

    async def test_only_repair_and_helper_worker_launches_seed(self):
        tid, request = self.owned_request()
        with patch.object(cache, 'seed', new=AsyncMock(return_value=False)) as seed, \
             patch.object(bump_workflow, 'spawn', new=AsyncMock(return_value=None)) as spawn, \
             patch.object(self.controller, '_submission_followup_needed', return_value=False):
            await self.controller.worker(self.roster.agents[0], tid, self.root, 'preparing', asyncio.Event())
            await self.controller.worker(self.roster.agents[0], 'critic', self.root, 'critic', asyncio.Event())
            self.assertEqual(seed.await_count, 0)
            await self.controller.worker(self.roster.agents[0], tid, self.root, 'repairing', asyncio.Event())
            await self.controller.assistance_worker(self.roster.agents[1], request, self.root, asyncio.Event())
            self.assertEqual(seed.await_count, 2)
            self.assertEqual(spawn.await_count, 4)

    async def test_helper_does_not_spawn_after_request_invalidates_during_copy(self):
        tid, request = self.owned_request()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
            return False
        with patch.object(cache, 'seed', side_effect=delayed), \
             patch.object(bump_workflow, 'spawn', new=AsyncMock()) as spawn:
            task = asyncio.create_task(self.controller.assistance_worker(
                self.roster.agents[1], request, self.root, asyncio.Event()))
            await entered.wait()
            with bump_tasks.transaction(self.paths.forum) as state:
                state['tasks'][tid]['yield_requested'] = {'reason': 'handoff'}
            release.set()
            self.assertIsNone(await task)
            spawn.assert_not_awaited()

    async def test_writer_does_not_spawn_after_assignment_invalidates_during_copy(self):
        tid, _ = self.owned_request()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
            return False
        with patch.object(cache, 'seed', side_effect=delayed), \
             patch.object(bump_workflow, 'spawn', new=AsyncMock()) as spawn:
            task = asyncio.create_task(self.controller.worker(
                self.roster.agents[0], tid, self.root, 'repairing', asyncio.Event()))
            await entered.wait()
            with bump_tasks.transaction(self.paths.forum) as state:
                state['config']['revision'] += 1
            release.set()
            self.assertIsNone(await task)
            spawn.assert_not_awaited()

    async def test_writer_does_not_spawn_after_lease_changes_during_copy(self):
        tid, _ = self.owned_request()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
            return False
        with patch.object(cache, 'seed', side_effect=delayed), \
             patch.object(bump_workflow, 'spawn', new=AsyncMock()) as spawn:
            task = asyncio.create_task(self.controller.worker(
                self.roster.agents[0], tid, self.root, 'repairing', asyncio.Event()))
            await entered.wait()
            with bump_tasks.transaction(self.paths.forum) as state:
                path = state['tasks'][tid]['files'][0]
                state['leases'][path]['author'] = 'Bob'
            release.set()
            self.assertIsNone(await task)
            spawn.assert_not_awaited()


@unittest.skipUnless(os.environ.get('UNITY_BUMP_TEST_LAKE') and Path(os.environ.get('UNITY_BUMP_TEST_LAKE', '')).is_file(),
                     'set UNITY_BUMP_TEST_LAKE to a local Lean 4.34.1 Lake executable')
class AdvisoryColdIntegrationTests(unittest.TestCase):
    def test_stale_worker_success_is_rejected_by_cold_controller(self):
        from types import SimpleNamespace
        from unity import bump_verification
        from unity.forum import bump_workflow_server as forum
        lake = Path(os.environ['UNITY_BUMP_TEST_LAKE']).resolve()
        with tempfile.TemporaryDirectory(prefix='advisory-real-lean-') as directory:
            root = Path(directory).resolve() / 'project'
            root.mkdir()
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (root / '.gitignore').write_text('.lake/\n.unity/\n.worktrees/\nlake-manifest.json\n')
            (root / 'lakefile.toml').write_text('name = "Fixture"\n[[lean_lib]]\nname = "Fixture"\n')
            (root / 'lean-toolchain').write_text('leanprover/lean4:v4.34.1\n')
            (root / 'Fixture').mkdir()
            (root / 'Fixture/Base.lean').write_text('def fixtureBase : Nat := 40\n')
            (root / 'Fixture/Target.lean').write_text(
                'import Fixture.Base\ntheorem fixtureClaim : fixtureBase = 0 := by exact rfl\n')
            git('add', '.')
            git('commit', '-qm', 'fixture')
            base = git('rev-parse', 'HEAD')
            paths = bump_workflow.paths_for(root)
            scope = {'selected_modules': {'Fixture.Base': 'Fixture/Base.lean', 'Fixture.Target': 'Fixture/Target.lean'},
                     'default_modules': {'Fixture.Target': 'Fixture/Target.lean'},
                     'excluded_files': {}, 'build_dir': '.lake/build'}
            bump_tasks.initialize(paths.forum, root, run_id='real', base_sha=base,
                                  version='leanprover/lean4:v4.34.1', config={'scope': scope})
            roster = SimpleNamespace(agents=[SimpleNamespace(name='Alice')])
            roster.primary = roster.agents[0]
            controller = bump_workflow.Controller(paths, roster)
            environment = {'UNITY_REAL_LAKE': str(lake), 'PATH': str(lake.parent) + os.pathsep + os.environ.get('PATH', ''),
                           'UNITY_AGENT_NAME': '', 'UNITY_BUMP_TASK_ID': ''}
            with patch.dict(os.environ, environment, clear=False):
                baseline = bump_verification.build(root, modules=['Fixture.Target'], scope=scope)
                self.assertFalse(baseline['passed'])
                controller.refresh(baseline)
                state = controller.state()
                tid = next(k for k, t in state['tasks'].items() if t.get('kind') == 'repair')
                bump_tasks.claim(paths.forum, 'Alice', tid)
                worker = bump_workflow.task_worktree(paths, 'Alice', tid)
                bump_tasks.configure_assignment(paths.forum, 'Alice', tid, worker, base)
                # Build a valid but wrong prerequisite, then restore tracked source.
                (worker / 'Fixture/Base.lean').write_text('def fixtureBase : Nat := 0\n')
                built = subprocess.run([str(lake), '--rehash', 'build', '+Fixture.Target'], cwd=worker,
                                       capture_output=True, text=True, check=False)
                self.assertEqual(built.returncode, 0, built.stderr)
                (worker / 'Fixture/Base.lean').write_text('def fixtureBase : Nat := 40\n')
                (worker / 'Fixture/Target.lean').write_text(
                    'import Fixture.Base\ntheorem fixtureClaim : fixtureBase = 0 := by decide\n')
                false_positive = subprocess.run([str(lake), 'env', 'lean', 'Fixture/Target.lean'], cwd=worker,
                                                capture_output=True, text=True, check=False)
                self.assertEqual(false_positive.returncode, 0, false_positive.stderr)
                forum.configure(paths.forum, root, 'repairing')
                candidate = forum.submit_repair('Alice', tid, 'worker saw a stale success')
                original_build = bump_verification.build
                def cold_controller_build(root, **kwargs):
                    self.assertFalse((root / '.lake/build').exists())
                    return original_build(root, **kwargs)
                with patch.object(controller, 'discover_scope', return_value=scope), \
                     patch.object(cache, 'seed', new=AsyncMock()) as seeded, \
                     patch.object(bump_verification, 'build', side_effect=cold_controller_build):
                    result = controller.integrate(candidate)
                seeded.assert_not_awaited()
                self.assertFalse(result['accepted'], str(result))
                self.assertFalse(result['build']['passed'])
                self.assertEqual(git('rev-parse', 'HEAD'), base)
