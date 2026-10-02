"""Persisted Bump cancellation never trusts a numeric PID/PGID alone."""

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from unity import bump_jobs as jobs


class PersistedJobIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bump-job-identity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.pid = 987654
        self.birth = {"platform": "fixture", "pid": self.pid, "birth": 10}
        self.record = {"job_id": "fixture", "pid": self.pid, "pgid": self.pid,
                       "owner": "Worker", "process_identity": self.birth, "group_identity": self.birth}
        self.path = jobs._jobs_dir(self.root) / "fixture.json"

    def save(self, record=None):
        self.path.write_text(json.dumps(self.record if record is None else record))

    def test_reused_pid_fails_closed_without_signal(self):
        self.save()
        with patch.object(jobs, "_process_identity", return_value={**self.birth, "birth": 11}), \
             patch.object(jobs.os, "kill") as kill, patch.object(jobs.os, "killpg") as group:
            with self.assertRaisesRegex(jobs.JobIdentityError, "birth identity"):
                jobs.terminate(self.root)
        kill.assert_not_called()
        group.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_legacy_live_record_is_a_blocker(self):
        self.save({k: v for k, v in self.record.items() if not k.endswith("identity")})
        with patch.object(jobs, "_process_identity", return_value=self.birth), \
             patch.object(jobs.os, "killpg") as group:
            with self.assertRaises(jobs.JobIdentityError):
                jobs.terminate(self.root)
        group.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_unreadable_birth_but_live_pid_is_a_blocker(self):
        self.save()
        with patch.object(jobs, "_process_identity", return_value=None), \
             patch.object(jobs, "_pid_present", return_value=True), patch.object(jobs.os, "killpg") as group:
            with self.assertRaises(jobs.JobIdentityError):
                jobs.terminate(self.root)
        group.assert_not_called()

    def test_dead_record_pruned_without_real_signal(self):
        self.save()
        with patch.object(jobs, "_process_identity", return_value=None), \
             patch.object(jobs, "_pid_present", return_value=False), \
             patch.object(jobs.os, "killpg", side_effect=ProcessLookupError) as group:
            self.assertEqual(jobs.terminate(self.root), 0)
        group.assert_called_once_with(self.pid, 0)
        self.assertFalse(self.path.exists())

    def test_dead_child_with_surviving_group_is_not_signaled(self):
        self.save()
        with patch.object(jobs, "_process_identity", return_value=None), \
             patch.object(jobs, "_pid_present", return_value=False), \
             patch.object(jobs.subprocess, "run", return_value=type("Result", (), {"returncode": 0, "stdout": "123 987654 S\n"})()), \
             patch.object(jobs.os, "killpg") as group:
            with self.assertRaisesRegex(jobs.JobIdentityError, "remains live"):
                jobs.terminate(self.root)
        group.assert_called_once_with(self.pid, 0)
        self.assertTrue(self.path.exists())

    def test_changed_group_identity_is_rejected(self):
        self.record["passthrough_stdio"] = True
        self.record["pgid"] = self.pid + 1
        self.record["group_identity"] = {"pid": self.pid + 1, "birth": 1}
        self.save()
        with patch.object(jobs, "_process_identity", side_effect=[self.birth, {"pid": self.pid + 1, "birth": 2}]), \
             patch.object(jobs.os, "getpgid", return_value=self.pid + 1), patch.object(jobs.os, "killpg") as group:
            with self.assertRaisesRegex(jobs.JobIdentityError, "leader birth"):
                jobs.terminate(self.root)
        group.assert_not_called()

    def test_pid_reused_between_term_and_kill_is_rechecked(self):
        self.save()
        identities = [self.birth, self.birth, self.birth, self.birth, {**self.birth, "birth": 11}]
        with patch.object(jobs, "_process_identity", side_effect=identities), \
             patch.object(jobs.os, "getpgid", return_value=self.pid), \
             patch.object(jobs.os, "killpg") as group, patch.object(jobs.time, "sleep"):
            with self.assertRaises(jobs.JobIdentityError):
                jobs.terminate(self.root)
        group.assert_called_once_with(self.pid, signal.SIGTERM)
        self.assertTrue(self.path.exists())

    def test_owner_filter_never_inspects_unrelated_jobs(self):
        self.save()
        with patch.object(jobs, "_process_identity") as identity:
            self.assertEqual(jobs.terminate(self.root, owner="Other"), 0)
        identity.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_kill_without_confirmed_exit_is_a_blocker(self):
        self.save()
        with patch.object(jobs, "_process_identity", return_value=self.birth), \
             patch.object(jobs.os, "getpgid", return_value=self.pid), \
             patch.object(jobs.os, "killpg") as group, patch.object(jobs, "_group_running", return_value=True), \
             patch.object(jobs.time, "monotonic", side_effect=[0.0, 2.0]), patch.object(jobs.time, "sleep"):
            with self.assertRaisesRegex(jobs.JobIdentityError, "could not confirm"):
                jobs.terminate(self.root)
        self.assertEqual([call.args[1] for call in group.call_args_list], [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(self.path.exists())

    def test_zombie_only_group_is_not_running(self):
        result = type("Result", (), {"returncode": 0, "stdout": "123 987654 Z\n456 1 S\n"})()
        with patch.object(jobs.os, "killpg"), patch.object(jobs.subprocess, "run", return_value=result):
            self.assertFalse(jobs._group_running(self.pid))

    def test_linux_start_ticks_are_bound_to_boot_identity(self):
        fields = ["S"] + ["0"] * 18 + ["987654321"] + ["0"] * 4
        stat = "123 (a command (with) spaces) " + " ".join(fields)
        with patch.object(jobs.sys, "platform", "linux"), \
             patch.object(Path, "read_text", side_effect=[stat, "boot-a\n"]):
            self.assertEqual(jobs._process_identity(123), {
                "platform": "linux", "pid": 123, "startticks": 987654321, "boot_id": "boot-a"})

    def test_unsupported_platform_has_no_coarse_time_fallback(self):
        with patch.object(jobs.sys, "platform", "unsupported"):
            self.assertIsNone(jobs._process_identity(os.getpid()))

    def test_corrupt_registration_is_preserved(self):
        self.path.write_text("not json")
        with patch.object(jobs.os, "killpg") as group:
            with self.assertRaisesRegex(jobs.JobIdentityError, "unreadable"):
                jobs.terminate(self.root)
        group.assert_not_called()
        self.assertTrue(self.path.exists())

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "native birth API unavailable")
    def test_native_registered_job_is_birth_verified_then_stopped(self):
        # The local child is created for this test, never discovered by name.
        outcomes, failures = [], []
        cancelled = Event()

        def run():
            try:
                with jobs.cancellation_scope(cancelled):
                    outcomes.append(jobs.run(self.root, [sys.executable, "-c", "import time; time.sleep(30)"],
                                             owner="NativeFixture", timeout=5))
            except BaseException as exc:
                failures.append(exc)

        worker = Thread(target=run)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            saved = None
            while time.monotonic() < deadline:
                entries = list(jobs._jobs_dir(self.root).glob("*.json"))
                if entries:
                    saved = json.loads(entries[0].read_text())
                    break
                time.sleep(0.01)
            self.assertIsNotNone(saved, failures)
            self.assertIsNotNone(saved["process_identity"])
            self.assertEqual(jobs._process_identity(saved["pid"]), saved["process_identity"])
            self.assertTrue(jobs._registered_job_live(saved))
            self.assertEqual(jobs.terminate(self.root, owner="NativeFixture"), 1)
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(failures, failures)
            self.assertLess(outcomes[0].returncode, 0)
            self.assertFalse(list(jobs._jobs_dir(self.root).glob("*.json")))
        finally:
            cancelled.set()
            worker.join(timeout=6)


if __name__ == "__main__":
    unittest.main()
