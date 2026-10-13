"""Offline Bump spawn cleanup regressions; fake SDK, real owned subprocesses.

No Codex binary, provider, model, MCP service, or Lean compiler is contacted.
The fake app-server retains a child after its SDK close returns, so assertions
test process closure, not merely that an AsyncMock close method was awaited.
"""

import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from unity import bump_spawn


_SERVER_SCRIPT = r'''import os, pathlib, subprocess, sys, time
directory = pathlib.Path(sys.argv[1])
child_code = r"""import os, pathlib, signal, sys, time
directory = pathlib.Path(sys.argv[1])
if sys.argv[2] == 'stubborn':
    def term(_sig, _frame):
        (directory / 'term-seen').write_text('received')
    signal.signal(signal.SIGTERM, term)
(directory / 'child.pid').write_text(str(os.getpid()))
(directory / 'child.pgid').write_text(str(os.getpgrp()))
while True:
    time.sleep(1)
"""
subprocess.Popen([sys.executable, '-c', child_code, str(directory), sys.argv[2]],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL)
while True:
    time.sleep(1)
'''


def _live(pid):
    """A killed orphan can briefly be a zombie before the host's init reaps it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path('/proc') / str(pid) / 'stat'
    if Path('/proc').is_dir():
        try:
            return stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z'
        except FileNotFoundError:
            return False
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                            capture_output=True, text=True, check=False)
    return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith('Z')


async def _until(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


class _Handle:
    def __init__(self, client):
        self.client = client
        self.queue = queue.Queue()
        self.registered = False
        self.waiter_started = threading.Event()
        self.waiter_finished = threading.Event()

    def blocking_next(self):
        self.waiter_started.set()
        try:
            return self.queue.get(timeout=3)
        finally:
            self.waiter_finished.set()

    async def interrupt(self):
        return SimpleNamespace()

    async def stream(self):
        if self.client.outcome == 'threaded_wait':
            self.registered = True
            try:
                item = await asyncio.to_thread(self.blocking_next)
                if isinstance(item, BaseException):
                    raise item
                yield item
            finally:
                self.registered = False
            return
        if self.client.outcome == 'stream_error':
            raise RuntimeError('fixture stream error')
        if self.client.outcome == 'completed':
            yield SimpleNamespace(method='item/completed', payload=SimpleNamespace(
                item=SimpleNamespace(root=SimpleNamespace(type='agentMessage', text='repaired'))))
        yield SimpleNamespace(method='turn/completed', payload=SimpleNamespace(
            turn=SimpleNamespace(status=self.client.outcome,
                                 error=SimpleNamespace(message=(None if self.client.outcome == 'completed'
                                                               else '429 fixture failure')))))


class _Client:
    def __init__(self, fixture, config, outcome):
        self.fixture, self.config, self.outcome = fixture, config, outcome
        self.proc = None
        self.child_pid = None
        self.handle = None
        self.reader_watcher = None
        self.close_calls = 0
        self.close_entered = asyncio.Event()
        self.close_gate = asyncio.Event()
        self.directory = fixture.root / ('attempt-' + str(len(fixture.clients)))
        self.directory.mkdir()

    async def thread_start(self, **_kwargs):
        self.proc = await asyncio.create_subprocess_exec(
            self.config.codex_bin, str(self.directory), self.fixture.child_mode,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Existence is not publication: write_text can briefly expose an empty
        # file, and the child writes its PGID after its PID.
        await _until(lambda: all((self.directory / name).exists()
                                and (self.directory / name).read_text().strip().isdigit()
                                for name in ('child.pid', 'child.pgid')))
        self.child_pid = int((self.directory / 'child.pid').read_text())
        # The wrapper starts a fresh session. The retained child is in its own
        # spawn's group, never the test runner's or the unrelated sentinel's.
        self.fixture.assertEqual(int((self.directory / 'child.pgid').read_text()), self.proc.pid)
        self.fixture.assertNotEqual(self.proc.pid, os.getpgrp())
        self.reader_watcher = asyncio.create_task(self.watch_transport())
        return SimpleNamespace(turn=self.turn)

    async def watch_transport(self):
        # SDK reader EOF/fail_all wakes a still-registered Queue.get waiter.
        # Do not wake an unregistered queue: reproduces the SDK's ordering risk.
        await self.proc.wait()
        if self.handle is not None and self.handle.registered:
            self.handle.queue.put(RuntimeError('fixture app-server closed'))

    async def turn(self, _prompt):
        self.handle = _Handle(self)
        return self.handle

    async def close(self):
        self.close_calls += 1
        self.close_entered.set()
        if self.fixture.close_mode == 'raises':
            raise RuntimeError('fixture SDK close failed')
        if self.fixture.close_mode in ('blocks', 'hangs'):
            await self.close_gate.wait()
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()
        # Deliberately do not terminate the retained MCP-like child.


@unittest.skipUnless(os.name == 'posix', 'owned process groups are POSIX-only')
class BumpSpawnCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Load the legacy runtime fixture before patch.dict(sys.modules).
        # Otherwise restoring that mapping unloads beartype modules but leaves
        # its global import hook installed, contaminating the following test.
        import test_bump_manifest_runtime
        self.real_mkdtemp = tempfile.mkdtemp
        self.temporary = tempfile.TemporaryDirectory(prefix='bump-spawn-cleanup-')
        self.root = Path(self.temporary.name)
        self.clients = []
        self.close_mode = 'normal'
        self.child_mode = 'normal'
        self.outcomes = ['completed']
        self.records = []
        self.script = self.root / 'fixture-app-server'
        self.script.write_text('#!' + sys.executable + '\n' + _SERVER_SCRIPT)
        self.script.chmod(0o700)
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        self.agent = SimpleNamespace(name='Ada', model='fixture-model', api_key=None,
                                     base_url=None, backend='codex')
        self.sentinel = await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; time.sleep(60)', start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.stack = ExitStack()
        sdk = SimpleNamespace(AsyncCodex=self.client_factory,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(workspace_write='workspace', full_access='full'))
        self.stack.enter_context(patch.dict(sys.modules, {'openai_codex': sdk}))
        self.stack.enter_context(patch.dict(os.environ, {'MAX_ATTEMPTS': '2'}))
        for name, value in (('_worktree_write_roots', ()), ('_write_codex_config', None),
                            ('_write_codex_agents', None), ('_agent_env', {}),
                            ('_stop_requested', False), ('_log', None)):
            self.stack.enter_context(patch.object(bump_spawn, name, return_value=value))
        self.stack.enter_context(patch.object(bump_spawn.tempfile, 'mkdtemp', return_value=str(self.home)))
        self.stack.enter_context(patch('shutil.which', return_value=str(self.script)))
        self.stack.enter_context(patch.object(bump_spawn, '_CODEX_CLOSE_TIMEOUT', 0.1))
        self.stack.enter_context(patch.object(bump_spawn, '_retry_sleep', return_value=0.01))
        self.tasks = []

    def client_factory(self, *, config):
        # An earlier attempt's cleanup must be complete before a replacement
        # app-server can start. Retry cap/backoff semantics otherwise stay intact.
        for previous in self.clients:
            self.assert_owned_stopped(previous)
        client = _Client(self, config, self.outcomes[len(self.clients)])
        self.clients.append(client)
        return client

    def assert_owned_stopped(self, client):
        if client.proc is not None:
            self.assertFalse(_live(client.proc.pid), 'owned app-server retained after spawn cleanup')
        if client.child_pid is not None:
            self.assertFalse(_live(client.child_pid), 'owned child retained after spawn cleanup')

    def assert_all_stopped(self):
        for client in self.clients:
            self.assert_owned_stopped(client)
        self.assertIsNone(self.sentinel.returncode, 'cleanup touched the unrelated sentinel')
        self.assertTrue(_live(self.sentinel.pid), 'cleanup touched the unrelated sentinel')

    def start(self, *, accounting=False, callback=None, interrupt_event=None,
              idle_timeout=600.0):
        if accounting:
            def record(*_args):
                self.assert_all_stopped()
                self.records.append('after-cleanup')
            self.stack.enter_context(patch.object(bump_spawn, '_write_run_log', side_effect=record))
            coroutine = bump_spawn.spawn(self.agent, 'system', 'task', self.root, {},
                own_process_group=True, mcp_profile='solve', on_normal_completion=callback,
                interrupt_event=interrupt_event, idle_timeout=idle_timeout)
        else:
            coroutine = bump_spawn.codex_spawner(self.agent, 'system', 'task', self.root, {},
                own_process_group=True, mcp_profile='solve', on_normal_completion=callback,
                interrupt_event=interrupt_event, idle_timeout=idle_timeout)
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def first_close(self):
        await _until(lambda: bool(self.clients))
        await self.clients[0].close_entered.wait()
        return self.clients[0]

    async def finish(self, task, *, cancelled=False):
        if cancelled:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        else:
            self.assertEqual(await asyncio.wait_for(task, timeout=2), 'repaired')
        self.assert_all_stopped()

    async def asyncTearDown(self):
        # Cleanup only processes launched by this fixture, even when a prepatch
        # regression fails. Never discover or signal arbitrary host processes.
        for client in self.clients:
            client.close_gate.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        for client in self.clients:
            if client.proc is not None:
                try:
                    os.killpg(client.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(client.proc.wait(), timeout=2)
            if client.reader_watcher is not None:
                await asyncio.wait_for(client.reader_watcher, timeout=2)
            if client.handle is not None and client.handle.waiter_started.is_set():
                await _until(lambda: client.handle.waiter_finished.is_set()
                             and not client.handle.registered)
        for task in self.tasks:
            try:
                await asyncio.wait_for(task, timeout=2)
            except (Exception, asyncio.CancelledError):
                pass
        if self.sentinel.returncode is None:
            self.sentinel.terminate()
        await asyncio.wait_for(self.sentinel.wait(), timeout=2)
        self.stack.close()
        self.temporary.cleanup()

    async def test_normal_completion_reaps_retained_server_child(self):
        await self.finish(self.start())
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.clients[0].close_calls, 1)

    async def test_close_error_does_not_skip_owned_group_cleanup(self):
        self.close_mode = 'raises'
        await self.finish(self.start())

    async def test_close_timeout_does_not_skip_owned_group_cleanup(self):
        self.close_mode = 'hangs'
        await self.finish(self.start())

    async def test_terminal_error_reaps_each_attempt_without_changing_cap(self):
        self.outcomes = ['failed', 'failed']
        task = self.start()
        with self.assertRaisesRegex(RuntimeError, '429 fixture failure'):
            await asyncio.wait_for(task, timeout=2)
        self.assertEqual(len(self.clients), 2)
        self.assert_all_stopped()

    async def test_retry_closes_old_attempt_before_starting_new_one(self):
        self.outcomes = ['failed', 'completed']
        await self.finish(self.start())
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(all(client.close_calls == 1 for client in self.clients))

    async def test_callback_error_is_preserved_after_owned_cleanup(self):
        async def callback(_final):
            raise ValueError('fixture controller error')
        with self.assertRaisesRegex(ValueError, 'fixture controller error'):
            await asyncio.wait_for(self.start(callback=callback), timeout=2)
        self.assertEqual(len(self.clients), 1)
        self.assert_all_stopped()

    async def test_stream_error_cancels_interrupt_task_before_owned_cleanup_returns(self):
        self.outcomes = ['stream_error']
        with patch.dict(os.environ, {'MAX_ATTEMPTS': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'fixture stream error'):
                await asyncio.wait_for(self.start(interrupt_event=asyncio.Event()), timeout=2)
        self.assert_all_stopped()
        self.assertFalse(any(task.get_name() == 'codex-interrupt:Ada'
                             and not task.done() for task in asyncio.all_tasks()))

    async def test_cancellation_during_close_drains_cleanup_before_raising(self):
        self.close_mode = 'blocks'
        task = self.start()
        client = await self.first_close()
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done(), 'cancellation escaped before owned cleanup drained')
        client.close_gate.set()
        await self.finish(task, cancelled=True)

    async def test_repeated_cancellation_during_close_still_drains_cleanup(self):
        self.close_mode = 'blocks'
        task = self.start()
        client = await self.first_close()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.005)
        self.assertFalse(task.done(), 'repeated cancellation abandoned cleanup')
        client.close_gate.set()
        await self.finish(task, cancelled=True)

    async def test_cancellation_during_term_kill_wait_reaps_stubborn_child(self):
        self.child_mode = 'stubborn'
        task = self.start()
        client = await self.first_close()
        await _until(lambda: (client.directory / 'term-seen').exists())
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.005)
        await self.finish(task, cancelled=True)

    async def test_run_accounting_occurs_after_owned_cleanup(self):
        await self.finish(self.start(accounting=True))
        self.assertEqual(self.records, ['after-cleanup'])

    async def test_cancelled_run_accounting_occurs_after_owned_cleanup(self):
        self.close_mode = 'blocks'
        task = self.start(accounting=True)
        client = await self.first_close()
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertEqual(self.records, [])
        client.close_gate.set()
        await self.finish(task, cancelled=True)
        self.assertEqual(self.records, ['after-cleanup'])

    async def test_cancelled_long_close_budget_cannot_outlive_scaled_hard_deadline(self):
        self.close_mode = 'hangs'
        with patch.object(bump_spawn, '_CODEX_CLOSE_TIMEOUT', 5.0):
            task = self.start()
            await self.first_close()
            started = time.monotonic()
            task.cancel()
            await self.finish(task, cancelled=True)
            self.assertLess(time.monotonic() - started, 1.0,
                            'cancelled cleanup used the longer normal close budget')

    async def test_cancel_during_interrupt_task_drain_still_reaps_owned_children(self):
        wrapper, pid_file = bump_spawn._process_group_wrapper(self.script, self.home, 'direct')
        client = self.client_factory(config=SimpleNamespace(codex_bin=str(wrapper)))
        await client.thread_start()
        interrupted = asyncio.Event()

        async def delayed_interrupter():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                interrupted.set()
                await asyncio.Event().wait()

        interrupt_task = asyncio.create_task(delayed_interrupter())
        self.tasks.append(interrupt_task)
        await asyncio.sleep(0)
        cleanup = asyncio.create_task(bump_spawn._cleanup_codex_attempt(client, interrupt_task, pid_file))
        self.tasks.append(cleanup)
        await interrupted.wait()
        for _ in range(3):
            cleanup.cancel()
            await asyncio.sleep(0.005)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(cleanup, timeout=2)
        self.assertTrue(interrupt_task.done())
        self.assert_all_stopped()

    async def test_repeated_raw_cancel_drains_threaded_notification_before_unregister(self):
        self.close_mode = 'hangs'
        self.outcomes = ['threaded_wait']
        with patch.object(bump_spawn, '_CODEX_CLOSE_TIMEOUT', 5.0):
            task = self.start()
            await _until(lambda: bool(self.clients) and self.clients[0].handle is not None
                         and self.clients[0].handle.waiter_started.is_set())
            client = self.clients[0]
            task.cancel()
            await client.close_entered.wait()
            started = time.monotonic()
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.005)
            await self.finish(task, cancelled=True)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(client.handle.waiter_finished.is_set(), 'SDK queue waiter was abandoned')
            self.assertFalse(client.handle.registered)

    async def test_group_cleanup_polls_until_live_members_are_observed_closed(self):
        wrapper, pid_file = bump_spawn._process_group_wrapper(self.script, self.home, 'poll')
        client = self.client_factory(config=SimpleNamespace(codex_bin=str(wrapper)))
        await client.thread_start()
        observations = []

        def group_alive(pgid):
            self.assertEqual(pgid, client.proc.pid)
            observations.append(pgid)
            return len(observations) < 3

        with patch.object(bump_spawn, '_process_group_alive', side_effect=group_alive):
            await asyncio.wait_for(bump_spawn._terminate_process_group(pid_file), timeout=2)
        self.assertGreaterEqual(len(observations), 3)
        self.assertFalse(pid_file.exists(), 'closed group breadcrumb was not retired')
        self.assert_all_stopped()

    async def test_group_closure_timeout_preserves_pid_breadcrumb_and_raises(self):
        wrapper, pid_file = bump_spawn._process_group_wrapper(self.script, self.home, 'timeout')
        client = self.client_factory(config=SimpleNamespace(codex_bin=str(wrapper)))
        await client.thread_start()
        started = time.monotonic()
        with patch.object(bump_spawn, '_process_group_alive', return_value=True), \
                patch.object(bump_spawn, '_PROCESS_GROUP_CLOSE_TIMEOUT', 0.03), \
                patch.object(bump_spawn, '_PROCESS_GROUP_POLL_SECONDS', 0.005):
            with self.assertRaises(bump_spawn.SpawnCleanupError) as raised:
                await asyncio.wait_for(bump_spawn._terminate_process_group(pid_file), timeout=2)
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        self.assertLess(time.monotonic() - started, 0.7, 'group closure poll was unbounded')
        self.assertTrue(pid_file.exists(), 'unverified closure lost its ownership breadcrumb')
        self.assertEqual(int(pid_file.read_text()), client.proc.pid)
        self.assert_all_stopped()

    async def test_group_cleanup_waits_for_delayed_kill_delivery_to_stubborn_child(self):
        self.child_mode = 'stubborn'
        wrapper, pid_file = bump_spawn._process_group_wrapper(self.script, self.home, 'delivery')
        client = self.client_factory(config=SimpleNamespace(codex_bin=str(wrapper)))
        await client.thread_start()
        real_killpg = os.killpg
        pending_signals = []

        def signal_group(pgid, sig):
            self.assertEqual(pgid, client.proc.pid)
            if sig == signal.SIGKILL:
                def deliver():
                    try:
                        real_killpg(pgid, sig)
                    except ProcessLookupError:
                        pass
                pending_signals.append(asyncio.get_running_loop().call_later(0.04, deliver))
            else:
                real_killpg(pgid, sig)

        try:
            with patch.object(os, 'killpg', side_effect=signal_group):
                await asyncio.wait_for(bump_spawn._terminate_process_group(pid_file), timeout=2)
            self.assert_all_stopped()
            self.assertFalse(pid_file.exists())
        finally:
            # The old implementation returns before delivery. A failing baseline
            # assertion must never leave a timer that could signal a reused PGID.
            for pending in pending_signals:
                pending.cancel()

    async def test_cleanup_failure_suppresses_accounting_and_replacement_attempt(self):
        self.outcomes = ['failed', 'completed']
        with patch.object(bump_spawn, '_process_group_alive', return_value=True), \
                patch.object(bump_spawn, '_PROCESS_GROUP_CLOSE_TIMEOUT', 0.03), \
                patch.object(bump_spawn, '_PROCESS_GROUP_POLL_SECONDS', 0.005):
            with self.assertRaises(bump_spawn.SpawnCleanupError):
                await asyncio.wait_for(self.start(accounting=True), timeout=2)
        self.assertEqual(len(self.clients), 1, 'unverified cleanup started a replacement attempt')
        self.assertEqual(self.records, [], 'unverified cleanup published completed run accounting')
        self.assertTrue((self.home / 'codex-group.pid').exists())
        self.assert_all_stopped()

    async def test_notification_cleanup_failure_is_not_a_transport_retry(self):
        self.outcomes = ['threaded_wait', 'completed']
        with patch.object(bump_spawn, '_process_group_alive', return_value=True), \
                patch.object(bump_spawn, '_PROCESS_GROUP_CLOSE_TIMEOUT', 0.03), \
                patch.object(bump_spawn, '_PROCESS_GROUP_POLL_SECONDS', 0.005):
            with self.assertRaises(bump_spawn.SpawnCleanupError):
                await asyncio.wait_for(self.start(accounting=True, idle_timeout=0.02), timeout=2)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.records, [])
        self.assertTrue(self.clients[0].handle.waiter_finished.is_set())
        self.assertFalse(self.clients[0].handle.registered)
        self.assert_all_stopped()

    async def test_cleanup_failure_takes_precedence_over_repeated_cancellation(self):
        self.close_mode = 'blocks'
        with patch.object(bump_spawn, '_process_group_alive', return_value=True), \
                patch.object(bump_spawn, '_PROCESS_GROUP_CLOSE_TIMEOUT', 0.03), \
                patch.object(bump_spawn, '_PROCESS_GROUP_POLL_SECONDS', 0.005):
            task = self.start(accounting=True)
            await self.first_close()
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.005)
            with self.assertRaises(bump_spawn.SpawnCleanupError):
                await asyncio.wait_for(task, timeout=2)
        self.assertEqual(self.records, [])
        self.assertEqual(len(self.clients), 1)
        self.assert_all_stopped()

    async def test_failed_transport_cleanup_bounds_reader_drain_without_unregistering_live_queue(self):
        self.close_mode = 'hangs'
        self.outcomes = ['threaded_wait', 'completed']
        failed_termination = AsyncMock(side_effect=bump_spawn.SpawnCleanupError('fixture unclosed group'))
        started = time.monotonic()
        with patch.object(bump_spawn, '_terminate_process_group', new=failed_termination), \
                patch.object(bump_spawn, '_CODEX_NOTIFICATION_DRAIN_TIMEOUT', 0.03):
            with self.assertRaises(bump_spawn.SpawnCleanupError):
                await asyncio.wait_for(self.start(accounting=True, idle_timeout=0.02), timeout=1)
        self.assertLess(time.monotonic() - started, 0.8, 'failed SDK queue cleanup waited unboundedly')
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.records, [])
        client = self.clients[0]
        self.assertTrue(client.handle.registered, 'a live blocking queue waiter was unregistered')
        self.assertFalse(client.handle.waiter_finished.is_set())
        self.assertTrue(_live(client.proc.pid))
        self.assertTrue(_live(client.child_pid))
        self.assertTrue(_live(self.sentinel.pid))
        # Teardown kills only the real fixture group, which wakes its transport
        # reader and allows the still-registered notification waiter to drain.

    async def test_changed_group_birth_refuses_delayed_kill_and_preserves_breadcrumb(self):
        wrapper, pid_file = bump_spawn._process_group_wrapper(self.script, self.home, 'birth')
        client = self.client_factory(config=SimpleNamespace(codex_bin=str(wrapper)))
        await client.thread_start()
        original = {(client.proc.pid, 100)}
        reused = {(client.proc.pid, 101)}
        with patch.object(bump_spawn, '_process_group_births',
                          side_effect=[original, original, reused]), \
                patch.object(bump_spawn, '_process_group_alive', return_value=True), \
                patch.object(os, 'killpg') as kill_group:
            with self.assertRaises(bump_spawn.SpawnCleanupError) as raised:
                await asyncio.wait_for(bump_spawn._terminate_process_group(pid_file), timeout=2)
        self.assertRegex(str(raised.exception.__cause__), 'birth identity changed')
        self.assertEqual(kill_group.call_args_list, [call(client.proc.pid, signal.SIGTERM)])
        self.assertTrue(pid_file.exists(), 'changed ownership lost the original breadcrumb')
        # Signals were intercepted, not delivered: retain the real owned fixture
        # for teardown, while proving no KILL was sent to the changed birth.
        self.assertTrue(_live(client.proc.pid))
        self.assertTrue(_live(client.child_pid))
        self.assertTrue(_live(self.sentinel.pid))

    async def test_group_inspection_failure_is_fatal_without_signalling_or_retiring_identity(self):
        pid_file = self.root / 'inspection-group.pid'
        pid_file.write_text('999999')
        with patch.object(bump_spawn, '_process_group_births', side_effect=PermissionError('fixture proc')), \
                patch.object(os, 'killpg') as kill_group:
            with self.assertRaises(bump_spawn.SpawnCleanupError) as raised:
                await bump_spawn._terminate_process_group(pid_file)
        self.assertIsInstance(raised.exception.__cause__, PermissionError)
        kill_group.assert_not_called()
        self.assertEqual(pid_file.read_text(), '999999')
        self.assert_all_stopped()

    async def test_runtime_cleanup_failure_propagates_without_worker_reschedule(self):
        from test_bump_manifest_runtime import ManifestRuntimeSchedulingTests
        fixture = ManifestRuntimeSchedulingTests()
        # Spawn's fake CODEX_HOME path must not replace the runtime fixture's
        # separate TemporaryDirectory allocation through shared tempfile.
        with patch.object(tempfile, 'mkdtemp', self.real_mkdtemp):
            fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        fixture.names = ['Ada']
        launched = []

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)
            raise bump_spawn.SpawnCleanupError('fixture unresolved process group')

        with self.assertRaisesRegex(bump_spawn.SpawnCleanupError, 'fixture unresolved process group'):
            await fixture.run_runtime(worker)
        self.assertEqual(launched, ['Ada'], 'cleanup failure was treated as ordinary eligible worker exit')
        self.assertEqual(fixture.yielded, set())
        self.assertEqual(fixture.state['formal_tasks']['root']['status'], 'pending')

    async def test_runtime_shutdown_preserves_worker_cleanup_failure(self):
        from test_bump_manifest_runtime import ManifestRuntimeSchedulingTests
        fixture = ManifestRuntimeSchedulingTests()
        with patch.object(tempfile, 'mkdtemp', self.real_mkdtemp):
            fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        fixture.names = ['Ada']
        launched = []

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)
            fixture.state['phase'] = 'chunking'
            raise bump_spawn.SpawnCleanupError('fixture shutdown closure failure')

        with self.assertRaisesRegex(bump_spawn.SpawnCleanupError, 'fixture shutdown closure failure'):
            await fixture.run_runtime(worker)
        self.assertEqual(launched, ['Ada'])
        self.assertEqual(fixture.yielded, set())


class ProcessGroupObservationTests(unittest.TestCase):
    def directory(self, pid, state, pgid, ticks, *, error=None):
        directory = MagicMock()
        directory.name = str(pid)
        fields = ['0'] * 20
        fields[0], fields[2], fields[19] = state, str(pgid), str(ticks)
        stat = directory.__truediv__.return_value
        if error is None:
            # Proc comm fields may contain spaces and closing parentheses.
            stat.read_text.return_value = f'{pid} (fixture ) app server) ' + ' '.join(fields)
        else:
            stat.read_text.side_effect = error
        return directory

    def proc(self, directories):
        proc = MagicMock()
        proc.is_dir.return_value = True
        proc.iterdir.side_effect = lambda: iter(directories)
        return proc

    def test_linux_proc_group_and_birth_observation_excludes_zombies_and_other_groups(self):
        for state, pgid, expected in [('S', 77, True), ('R', 77, True),
                                     ('Z', 77, False), ('X', 77, False), ('S', 88, False)]:
            with self.subTest(state=state, pgid=pgid):
                proc = self.proc([self.directory(101, state, pgid, 1234)])
                with patch.object(bump_spawn.sys, 'platform', 'linux'), \
                        patch.object(bump_spawn, 'Path', return_value=proc):
                    self.assertEqual(bump_spawn._process_group_alive(77), expected)
                    self.assertEqual(bump_spawn._process_group_births(77), {(101, 1234)} if expected else set())

    def test_linux_proc_missing_or_foreign_inaccessible_process_is_not_owned_work(self):
        proc = self.proc([self.directory(101, 'S', 77, 1234, error=FileNotFoundError()),
                          self.directory(102, 'S', 88, 2345, error=PermissionError())])
        with patch.object(bump_spawn.sys, 'platform', 'linux'), \
                patch.object(bump_spawn, 'Path', return_value=proc), \
                patch.object(os, 'getpgid', return_value=88):
            self.assertFalse(bump_spawn._process_group_alive(77))
            self.assertEqual(bump_spawn._process_group_births(77), set())
        with patch.object(bump_spawn.sys, 'platform', 'linux'), \
                patch.object(bump_spawn, 'Path', return_value=proc), \
                patch.object(os, 'getpgid', return_value=77):
            for observer in (bump_spawn._process_group_alive, bump_spawn._process_group_births):
                with self.subTest(observer=observer.__name__), self.assertRaises(PermissionError):
                    observer(77)

    def test_portable_ps_fallback_observes_non_zombie_members_with_bounded_call(self):
        with patch.object(bump_spawn.sys, 'platform', 'darwin'), \
                patch.object(subprocess, 'run') as run:
            self.assertIsNone(bump_spawn._process_group_births(77))
            run.return_value = SimpleNamespace(stdout='77 Z\n77 X\n88 Ss\n')
            self.assertFalse(bump_spawn._process_group_alive(77))
            run.return_value = SimpleNamespace(stdout='77 Z\n77 Sl\n88 Ss\n')
            self.assertTrue(bump_spawn._process_group_alive(77))
            run.assert_called_with(['ps', '-e', '-o', 'pgid=,stat='],
                                   capture_output=True, text=True, check=True, timeout=0.25)

    def test_portable_ps_timeout_or_error_is_not_false_closure(self):
        for error in (subprocess.TimeoutExpired('ps', 0.25), subprocess.CalledProcessError(1, 'ps')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(bump_spawn.sys, 'platform', 'darwin'), \
                    patch.object(subprocess, 'run', side_effect=error), \
                    self.assertRaises(type(error)):
                bump_spawn._process_group_alive(77)


if __name__ == '__main__':
    unittest.main()
