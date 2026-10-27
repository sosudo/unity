"""Namespace-aware Bump process safety, without model or network calls."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_jobs, bump_processes


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux procfs")
class ProcessIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / ".unity/jobs/bump"
        self.jobs.mkdir(parents=True)
        self.receipts = self.root / ".unity/forum/bump/processes"
        self.receipts.mkdir(parents=True)
        self.identity = bump_jobs.process_identity(os.getpid())
        self.assertIsNotNone(self.identity)

    def write_job(self, **updates):
        row = {"job_id": "fixture", "pid": os.getpid(), "pgid": os.getpgrp(),
               "owner": "Alice", **self.identity, **updates}
        path = self.jobs / "fixture.json"
        path.write_text(json.dumps(row, sort_keys=True))
        return path, row

    def worker_receipt(self, token, closed=True):
        path = self.receipts / "worker.json"
        row = {"pid": 101, "pgid": 101, "sid": 101, "start_ticks": 5, "worker_session": token,
               "pid_namespace": self.identity["pid_namespace"], "boot_id": self.identity["boot_id"]}
        path.write_text(json.dumps(row, sort_keys=True))
        if closed:
            path.with_suffix(".closed.json").write_text(json.dumps({
                "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "pgid": 101, "group_empty": True}))
        return path

    def job_closure(self, path, token):
        directory = self.jobs / "closed"
        directory.mkdir(exist_ok=True)
        (directory / "fixture.json").write_text(json.dumps({"record_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "job_id": "fixture", "worker_session": token, "group_empty": True}))

    def test_foreign_and_legacy_records_never_signal_host_pids(self):
        for change in ({"pid_namespace": self.identity["pid_namespace"] + 1}, {"boot_id": "different"},
                       {"start_ticks": self.identity["start_ticks"] + 1}):
            with self.subTest(change=change):
                path, _ = self.write_job(**change)
                with patch.object(signal, "pidfd_send_signal", create=True) as send, patch.object(os, "kill") as kill, \
                     patch.object(os, "killpg") as killpg:
                    self.assertEqual(bump_jobs.terminate(self.root), 0)
                    send.assert_not_called(); kill.assert_not_called(); killpg.assert_not_called()
                self.assertTrue(path.exists())
        path.write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
        with patch.object(os, "kill") as kill, patch.object(os, "killpg") as killpg:
            with self.assertRaisesRegex(ValueError, "legacy"):
                bump_processes.assert_closed(self.root)
            kill.assert_not_called(); killpg.assert_not_called()

    def test_foreign_job_is_not_cleared_by_group_closure_alone(self):
        path, _ = self.write_job(pid_namespace=self.identity["pid_namespace"] + 1, worker_session="session-one")
        with patch.object(os, "kill") as kill, patch.object(os, "killpg") as killpg, \
             patch.object(bump_processes, "_no_nested_pid_namespaces", return_value=False):
            with self.assertRaisesRegex(ValueError, "foreign"):
                bump_processes.assert_closed(self.root)
            self.worker_receipt("session-one")
            with self.assertRaisesRegex(ValueError, "foreign"):
                bump_processes.assert_closed(self.root)
            self.job_closure(path, "session-one")
            # Group-empty receipts do not prove escaped descendants are gone.
            with self.assertRaisesRegex(ValueError, "foreign"):
                bump_processes.assert_closed(self.root)
            path.write_text(path.read_text() + " ")
            with self.assertRaisesRegex(ValueError, "foreign"):
                bump_processes.assert_closed(self.root)
            kill.assert_not_called(); killpg.assert_not_called()

    def test_foreign_job_reconciles_only_with_bound_worker_and_no_nested_namespaces(self):
        self.write_job(pid_namespace=self.identity["pid_namespace"] + 1, worker_session="session-one")
        worker = self.worker_receipt("session-one")
        with patch.object(bump_processes, "_no_nested_pid_namespaces", return_value=True):
            bump_processes.assert_closed(self.root)
            worker.write_text(worker.read_text() + " ")  # Invalidates the exact closure hash.
            with self.assertRaises(ValueError):
                bump_processes.assert_closed(self.root)

    def test_namespace_absence_requires_full_valid_proc_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            pid = str(os.getpid())
            (proc / pid).mkdir()
            (proc / "self").symlink_to(pid, target_is_directory=True)
            (proc / pid / "status").write_text(f"Name:\tself\nNSpid:\t{pid}\n")
            mount = "22 70 0:21 / /proc rw,nosuid - proc proc rw\n"
            (proc / pid / "mountinfo").write_text(mount)
            self.assertTrue(bump_processes._no_nested_pid_namespaces(proc))
            (proc / "300").mkdir()
            status = proc / "300/status"
            status.write_text("NSpid:\t300 1\n")
            self.assertFalse(bump_processes._no_nested_pid_namespaces(proc))
            status.write_text("Name:\tunknown\n")
            self.assertFalse(bump_processes._no_nested_pid_namespaces(proc))
            status.write_text("NSpid:\tgarbage\n")
            self.assertFalse(bump_processes._no_nested_pid_namespaces(proc))
            status.unlink()
            self.assertFalse(bump_processes._no_nested_pid_namespaces(proc))
            status.write_text("NSpid:\t300\n")
            (proc / pid / "mountinfo").write_text(mount.replace("proc rw", "proc rw,hidepid=2"))
            self.assertFalse(bump_processes._no_nested_pid_namespaces(proc))
            (proc / pid / "mountinfo").write_text(mount)
            with patch.object(Path, "read_text", side_effect=PermissionError):
                self.assertFalse(bump_processes._no_nested_pid_namespaces(proc))

    def test_unclosed_sibling_worker_invalidates_same_session(self):
        path, _ = self.write_job(pid_namespace=self.identity["pid_namespace"] + 1, worker_session="session-one")
        self.worker_receipt("session-one")
        self.job_closure(path, "session-one")
        (self.receipts / "second.json").write_text(json.dumps({"worker_session": "session-one", "pid": 102,
            "pgid": 102, "sid": 102, "start_ticks": 8}))
        with self.assertRaisesRegex(ValueError, "foreign"):
            bump_processes.assert_closed(self.root)

    def test_pidfd_delivery_revalidates_birth_and_never_targets_group(self):
        self.write_job()
        with patch.object(os, "pidfd_open", return_value=123, create=True) as opened, \
             patch.object(signal, "pidfd_send_signal", create=True) as send, \
             patch.object(os, "close") as close, patch.object(os, "killpg") as killpg, \
             patch.object(bump_jobs.time, "sleep"):
            self.assertEqual(bump_jobs.terminate(self.root), 1)
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(close.call_count, 2)
            killpg.assert_not_called()
        # A reused PID discovered after opening must never receive a signal.
        with patch.object(os, "pidfd_open", return_value=123, create=True), \
             patch.object(signal, "pidfd_send_signal", create=True) as send, \
             patch.object(os, "close"), \
             patch.object(bump_jobs, "_live_owned_record", side_effect=[True, True, False, False]):
            self.assertEqual(bump_jobs.terminate(self.root), 0)
            send.assert_not_called()

    @unittest.skipUnless(hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"), "requires pidfd signals")
    def test_live_matching_job_signals_only_pidfd(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        identity = bump_jobs.process_identity(proc.pid)
        self.assertIsNotNone(identity)
        path, _ = self.write_job(pid=proc.pid, pgid=proc.pid, **identity)
        with patch.object(os, "killpg", side_effect=AssertionError("saved groups must not be signalled")):
            self.assertEqual(bump_jobs.terminate(self.root), 1)
        proc.wait(timeout=3)
        self.assertTrue(path.exists())

    def test_runtime_job_records_birth_and_session_then_closure(self):
        observed = []
        def on_output(channel, text):
            if text.strip():
                observed.extend(json.loads(path.read_text()) for path in self.jobs.glob("*.json"))
        with patch.dict(os.environ, {"UNITY_BUMP_WORKER_SESSION": "test-session"}):
            bump_jobs.run(self.root, [sys.executable, "-c", "print('ready', flush=True)"], on_output=on_output)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["worker_session"], "test-session")
        self.assertEqual(observed[0]["pid_namespace"], self.identity["pid_namespace"])
        self.assertTrue(observed[0]["start_ticks"])
        self.assertTrue((self.jobs / "closed" / (observed[0]["job_id"] + ".json")).is_file())


if __name__ == "__main__":
    unittest.main()
