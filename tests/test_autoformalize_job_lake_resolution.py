"""Internal Lake jobs bypass the worker guard without bypassing serialization."""

import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from unity import autoformalize_jobs as jobs, autoformalize_native


class JobLakeResolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = dict(os.environ)
        for key in ("UNITY_REAL_LAKE", "UNITY_AUTOFORMALIZE_WORKER_STATE", "UNITY_AGENT_NAME"):
            environment.pop(key, None)
        environment["UNITY_AUTOFORMALIZE_PROJECT_ROOT"] = str(self.root)
        environment["PYTHONPATH"] = str(Path(jobs.__file__).resolve().parents[1])
        binding = patch.dict(os.environ, environment, clear=True)
        binding.start()
        self.addCleanup(binding.stop)

    def executable(self, name, source):
        executable = self.root / name
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o700)
        return executable

    def guarded_job(self, command, *, native=False):
        entered = self.root / "guard-entered"
        guard = self.executable("guard/lake", (
            "from pathlib import Path\n"
            f"Path({str(entered)!r}).touch()\n"
            "from unity.autoformalize_lake_guard import main\n"
            "raise SystemExit(main())\n"
        ))
        lock = self.root / ".unity/forum/autoformalize-build.lock"
        real_lake = self.executable("real-lake", (
            "import fcntl, json, os, sys, time\n"
            "from pathlib import Path\n"
            f"with open({str(lock)!r}, 'a+') as handle:\n"
            "    try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError: pass\n"
            "    else: raise RuntimeError('authoritative build lock was bypassed')\n"
            # Registration happens just after Popen, so wait for this child's
            # record rather than making its creation race with inspection.
            "deadline = time.monotonic() + 2\n"
            f"directory = Path({str(self.root / '.unity/jobs/autoformalize')!r})\n"
            "while time.monotonic() < deadline:\n"
            "    records = [json.loads(p.read_text()) for p in directory.glob('*.json')]\n"
            "    own = [r for r in records if r['pid'] == os.getpid()]\n"
            "    if own: break\n"
            "    time.sleep(0.01)\n"
            "else: raise RuntimeError('missing owned-job record')\n"
            "print(json.dumps({'args': sys.argv, 'record': own[0]}))\n"
        ))
        cancelled = threading.Event()

        def run():
            with jobs.cancellation_scope(cancelled):
                if native:
                    return autoformalize_native._run(self.root, command, name="workspace")
                return jobs.run(self.root, command, serialize_build=True)

        original = list(command)
        with patch.dict(os.environ, {
            "PATH": str(guard.parent) + os.pathsep + os.environ.get("PATH", ""),
            "UNITY_REAL_LAKE": str(real_lake),
        }), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run)
            try:
                result = future.result(timeout=4)
            finally:
                # The unfixed guard waits on its parent's lock; cancellation
                # reaps it before executor shutdown instead of hanging the suite.
                cancelled.set()

        self.assertEqual(result.returncode, 0, result.stderr)
        actual = [str(real_lake), *original[1:]]
        observed = json.loads(result.stdout)
        self.assertEqual(observed["args"], actual)
        self.assertEqual(observed["record"]["command"], actual)
        self.assertEqual(observed["record"]["owner"], "Unity")
        self.assertEqual(observed["record"]["cwd"], str(self.root))
        if native:
            self.assertEqual(observed["record"]["task_id"], "workspace")
        self.assertEqual(result.args, actual)
        self.assertEqual(command, original)
        self.assertFalse(entered.exists())
        self.assertEqual(list((self.root / ".unity/jobs/autoformalize").iterdir()), [])
        with lock.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def test_native_version_check_bypasses_guard_with_outer_lock_held(self):
        self.guarded_job(["lake", "env", "lean", "--version"], native=True)

    def test_targeted_build_bypasses_guard_with_outer_lock_held(self):
        self.guarded_job(["lake", "build", "Example.Target"])

    def test_resolved_lake_cancellation_reaps_child_and_releases_lock(self):
        lake = self.executable("real-lake", "import time\ntime.sleep(30)\n")
        cancelled = threading.Event()

        def run():
            with jobs.cancellation_scope(cancelled):
                return jobs.run(self.root, ["lake", "build", "Example.Target"],
                                owner="Orca", serialize_build=True)

        with patch.dict(os.environ, {"UNITY_REAL_LAKE": str(lake)}), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run)
            try:
                directory = self.root / ".unity/jobs/autoformalize"
                deadline = time.monotonic() + 2
                records = []
                while time.monotonic() < deadline:
                    records = list(directory.glob("*.json"))
                    if records:
                        break
                    time.sleep(0.01)
                self.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text())
                self.assertEqual(record["owner"], "Orca")
                self.assertEqual(record["command"], [str(lake), "build", "Example.Target"])
            finally:
                cancelled.set()
            with self.assertRaises(jobs.JobCancelled):
                future.result(timeout=3)

        with self.assertRaises(ProcessLookupError):
            os.kill(record["pid"], 0)
        self.assertEqual(list(directory.iterdir()), [])
        with (self.root / ".unity/forum/autoformalize-build.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def test_unbound_and_blank_binding_keep_path_resolution(self):
        lake = self.executable("path/lake", "print('PATH Lake')\n")
        for binding in (None, "", " \t "):
            with self.subTest(binding=binding), patch.dict(os.environ, {"PATH": str(lake.parent)}):
                if binding is not None:
                    os.environ["UNITY_REAL_LAKE"] = binding
                command = ["lake", "--version"]
                result = jobs.run(self.root, command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "PATH Lake\n")
                self.assertEqual(result.args, command)

    def test_invalid_binding_fails_before_lock_launch_or_registration(self):
        nonexecutable = self.root / "nonexecutable"
        nonexecutable.write_text("not executable")
        nonexecutable.chmod(0o600)
        for binding in ("relative-lake", str(self.root / "missing"), str(self.root), str(nonexecutable)):
            with self.subTest(binding=binding), patch.dict(os.environ, {"UNITY_REAL_LAKE": binding}):
                with patch.object(jobs, "_build_lock") as lock, patch.object(jobs.subprocess, "Popen") as spawn:
                    with self.assertRaisesRegex(ValueError, "Invalid UNITY_REAL_LAKE executable"):
                        jobs.run(self.root, ["lake", "env", "lean", "--version"], serialize_build=True)
                    lock.assert_not_called()
                    spawn.assert_not_called()
                self.assertFalse((self.root / ".unity").exists())

    def test_explicit_lake_and_non_lake_commands_ignore_binding(self):
        explicit = self.executable("explicit/lake", "print('explicit Lake')\n")
        cases = [
            ([str(explicit), "--version"], "explicit Lake\n"),
            ([sys.executable, "-c", "print('not Lake')"], "not Lake\n"),
        ]
        with patch.dict(os.environ, {"UNITY_REAL_LAKE": str(self.root / "missing")}):
            for command, stdout in cases:
                with self.subTest(command=command):
                    original = list(command)
                    result = jobs.run(self.root, command)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, stdout)
                    self.assertEqual(result.args, original)
                    self.assertEqual(command, original)

    def test_binding_preserves_symlink_name_arguments_streams_and_status(self):
        executable = self.executable("elan", (
            "import json, sys\n"
            "print(json.dumps(sys.argv))\n"
            "print('diagnostic', file=sys.stderr)\n"
            "raise SystemExit(7)\n"
        ))
        lake = self.root / "lake"
        lake.symlink_to(executable)
        command = ["lake", "env", "lean", "file with spaces.lean"]
        original = list(command)
        with patch.dict(os.environ, {"UNITY_REAL_LAKE": f" {lake} "}):
            result = jobs.run(self.root, command)
        actual = [str(lake), *original[1:]]
        self.assertEqual(result.args, actual)
        self.assertEqual(json.loads(result.stdout), actual)
        self.assertEqual(result.stderr, "diagnostic\n")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(command, original)
        self.assertEqual(list((self.root / ".unity/jobs/autoformalize").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
