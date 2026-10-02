"""Owned local subprocess checks; no models, network, or project migrations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Timer
import unittest
from unittest.mock import patch

from unity import bump_jobs as jobs


class JobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n")

    def records(self):
        return list((self.root / ".unity/jobs/bump").glob("*.json"))

    def test_controller_pins_toolchain_and_caches_without_mutating_parent(self):
        keys = ["ELAN_TOOLCHAIN", "LEAN_PATH", "LAKE_HOME", "LAKE_CACHE_DIR", "XDG_CACHE_HOME"]
        code = "import json,os;print(json.dumps({k:os.environ.get(k) for k in " + repr(keys) + "}))"
        with patch.dict(os.environ, {key: "foreign" for key in keys}):
            result = jobs.run(self.root, [sys.executable, "-c", code], timeout=5)
            self.assertTrue(all(os.environ[key] == "foreign" for key in keys))
        actual = json.loads(result.stdout)
        self.assertEqual(actual["ELAN_TOOLCHAIN"], "leanprover/lean4:v4.34.1")
        self.assertIsNone(actual["LEAN_PATH"])
        self.assertIsNone(actual["LAKE_HOME"])
        self.assertEqual(actual["LAKE_CACHE_DIR"], str(self.root / ".unity/bump-cache/lake"))
        self.assertFalse(self.records())

    def test_explicit_worker_cache_is_preserved_and_input_is_delivered(self):
        env = {**os.environ, "LAKE_CACHE_DIR": str(self.root / "worker-cache")}
        result = jobs.run(self.root, [sys.executable, "-c",
            "import os,sys; print(os.environ['LAKE_CACHE_DIR']); print(sys.stdin.read())"],
            env=env, input="scoped input", timeout=5)
        self.assertEqual(result.stdout.splitlines(), [str(self.root / "worker-cache"), "scoped input"])
        self.assertFalse(self.records())

    def test_timeout_reaps_child_and_removes_registration(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            jobs.run(self.root, [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.15)
        self.assertFalse(self.records())

    def test_cancellation_interrupts_owned_job(self):
        cancelled = Event()
        timer = Timer(0.15, cancelled.set)
        timer.start()
        try:
            with jobs.cancellation_scope(cancelled), self.assertRaises(jobs.JobCancelled):
                jobs.run(self.root, [sys.executable, "-c", "import time; time.sleep(30)"])
        finally:
            timer.cancel()
            timer.join()
        self.assertFalse(self.records())

    def test_invalid_timeout_does_not_start_job(self):
        with self.assertRaises(ValueError):
            jobs.run(self.root, ["not-an-executable"], timeout=0)
