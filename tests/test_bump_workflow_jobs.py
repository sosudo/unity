"""Offline subprocess/lock tests; no model, service, or project evaluation."""
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from unity import bump_jobs, bump_lake_guard


class WorkflowJobsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_streamed_stdout_and_stderr_are_retained(self):
        chunks = []
        result = bump_jobs.run(self.root, [sys.executable, "-c",
            "import sys; print('α', flush=True); print('diagnostic', file=sys.stderr, flush=True)"],
            on_output=lambda channel, text: chunks.append((channel, text)))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "α\n")
        self.assertEqual(result.stderr, "diagnostic\n")
        self.assertEqual("".join(text for channel, text in chunks if channel == "stdout"), result.stdout)
        self.assertFalse(list((self.root / ".unity/jobs/bump").glob("*.json")))

    def test_stream_callback_precedes_process_exit_and_cancel_reaps(self):
        event = threading.Event()
        def output(channel, text):
            if "ready" in text:
                event.set()
        with bump_jobs.cancellation_scope(event):
            with self.assertRaises(bump_jobs.JobCancelled):
                bump_jobs.run(self.root, [sys.executable, "-c",
                    "import time; print('ready', flush=True); time.sleep(30)"], on_output=output)
        self.assertFalse(list((self.root / ".unity/jobs/bump").glob("*.json")))

    def test_build_lock_reentrant_on_same_thread(self):
        with bump_jobs._build_lock(self.root):
            with bump_jobs._build_lock(self.root):
                result = bump_jobs.run(self.root, [sys.executable, "-c", "print('ok')"], serialize_build=True)
        self.assertEqual(result.stdout, "ok\n")

    def test_other_thread_cannot_inherit_build_lock(self):
        acquired = threading.Event()
        def acquire():
            with bump_jobs._build_lock(self.root):
                acquired.set()
        with bump_jobs._build_lock(self.root):
            thread = threading.Thread(target=acquire)
            thread.start()
            self.assertFalse(acquired.wait(0.15))
        thread.join(2)
        self.assertTrue(acquired.is_set())

    def test_build_lock_reentrant_through_shared_runtime_link(self):
        worker = self.root / "worker"
        worker.mkdir()
        (self.root / ".unity").mkdir()
        (worker / ".unity").symlink_to(self.root / ".unity", target_is_directory=True)
        with bump_jobs._build_lock(self.root):
            with bump_jobs._build_lock(worker):
                self.assertEqual(len(bump_jobs._held_build_locks.get()), 1)

    def test_configured_lake_flags_are_consistent(self):
        env = {"UNITY_REAL_LAKE": sys.executable, "UNITY_BUMP_PROJECT_ROOT": str(self.root),
               "UNITY_BUMP_LAKE_ARGS": '["-Kenv=dev"]'}
        with patch.object(bump_jobs, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(bump_lake_guard.run(["build", "+A"], environ=env, cwd=self.root), 0)
            self.assertEqual(run.call_args.args[1], [sys.executable, "-Kenv=dev", "build", "+A"])
            self.assertEqual(bump_lake_guard.run(["-Kenv=prod", "build", "+A"], environ=env, cwd=self.root), 2)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
