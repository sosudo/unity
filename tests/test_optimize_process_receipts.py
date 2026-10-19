"""Birth/closure receipt checks with local dummy subprocesses, never providers."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from unity import optimize_spawn


@unittest.skipUnless(os.name == "posix", "owned sessions require POSIX")
class ProcessReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="optimize-process-receipt-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / "receipts"
        self.wrapper, self.pid_file = optimize_spawn._process_group_wrapper(Path(sys.executable), self.root, "fixture")
        self.procs = []

    async def asyncTearDown(self):
        for proc in self.procs:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await proc.wait()

    async def launch(self, code="import time; time.sleep(30)"):
        proc = await asyncio.create_subprocess_exec(str(self.wrapper), "-c", code,
            cwd=self.root, env={**os.environ, "UNITY_OPTIMIZE_PROCESS_RECEIPTS": str(self.directory),
                                "PRIVATE_PROVIDER_KEY": "fixture-sensitive-value"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        async with asyncio.timeout(3):
            while True:
                if self.pid_file.exists() and self.pid_file.with_suffix(".receipt").exists():
                    _, receipt = self.receipt()
                    if receipt["pid"] == proc.pid and self.pid_file.read_text() == str(proc.pid):
                        break
                if proc.returncode is not None:
                    self.fail("fixture wrapper exited before recording birth")
                await asyncio.sleep(0.01)
        return proc

    def receipt(self):
        path = Path(self.pid_file.with_suffix(".receipt").read_text())
        return path, json.loads(path.read_text())

    async def test_birth_receipt_records_owned_identity_not_env_or_argv(self):
        proc = await self.launch()
        path, record = self.receipt()
        self.assertEqual(record["pid"], proc.pid)
        self.assertEqual(record["pgid"], proc.pid)
        self.assertEqual(record["sid"], proc.pid)
        self.assertEqual(record["cwd"], str(self.root))
        self.assertEqual(record["executable"], sys.executable)
        self.assertNotIn("argv", record)
        self.assertNotIn("env", record)
        self.assertNotIn("fixture-sensitive-value", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(record["start_ticks"] or record["birth"])
        await optimize_spawn._terminate_process_group(self.pid_file)

    async def test_verified_closure_retains_original_birth(self):
        await self.launch()
        path, _ = self.receipt()
        original = path.read_bytes()
        await optimize_spawn._terminate_process_group(self.pid_file)
        self.assertEqual(path.read_bytes(), original)
        closed = json.loads(path.with_suffix(".closed.json").read_text())
        self.assertTrue(closed["group_empty"])
        self.assertEqual(closed["receipt_sha256"], hashlib.sha256(original).hexdigest())
        self.assertFalse(self.pid_file.exists())

    async def test_failure_to_verify_closure_never_certifies_receipt(self):
        proc = await self.launch()
        path, _ = self.receipt()
        with patch.object(optimize_spawn, "_process_group_births", return_value=None), \
             patch.object(optimize_spawn, "_process_group_alive", return_value=True), \
             patch.object(optimize_spawn.os, "killpg"), \
             patch.object(optimize_spawn, "_PROCESS_GROUP_CLOSE_TIMEOUT", 0.01):
            with self.assertRaises(optimize_spawn.SpawnCleanupError):
                await optimize_spawn._terminate_process_group(self.pid_file)
        self.assertFalse(path.with_suffix(".closed.json").exists())
        self.assertTrue(self.pid_file.exists())
        self.assertIsNone(proc.returncode)
        await optimize_spawn._terminate_process_group(self.pid_file)

    async def test_normal_child_exit_still_gets_observed_closure(self):
        proc = await self.launch("pass")
        path, _ = self.receipt()
        await proc.wait()
        await optimize_spawn._terminate_process_group(self.pid_file)
        self.assertTrue(path.with_suffix(".closed.json").is_file())

    async def test_retry_gets_distinct_immutable_birth_record(self):
        await self.launch()
        first, _ = self.receipt()
        await optimize_spawn._terminate_process_group(self.pid_file)
        await self.launch()
        second, _ = self.receipt()
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        await optimize_spawn._terminate_process_group(self.pid_file)

    async def test_mismatched_receipt_not_certified(self):
        await self.launch()
        path, record = self.receipt()
        original = path.read_bytes()
        path.write_text(json.dumps({**record, "pid": record["pid"] + 1}))
        with self.assertRaises(optimize_spawn.SpawnCleanupError):
            await optimize_spawn._terminate_process_group(self.pid_file)
        self.assertFalse(path.with_suffix(".closed.json").exists())
        path.write_bytes(original)
        await optimize_spawn._terminate_process_group(self.pid_file)

    def test_mcp_children_keep_workflow_and_lake_arguments_not_credentials(self):
        servers = {"lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
                   "unity-forum": {"command": sys.executable, "args": []}}
        rebound = optimize_spawn.optimize_mcp_with_runtime_env(servers, {
            "UNITY_OPTIMIZE_WORKFLOW": "metric-optimization-v1", "UNITY_OPTIMIZE_LAKE_ARGS": '["-Kenv=dev"]',
            "PRIVATE_PROVIDER_KEY": "must-not-forward"})
        for name in servers:
            self.assertEqual(rebound[name]["env"]["UNITY_OPTIMIZE_WORKFLOW"], "metric-optimization-v1")
            self.assertEqual(rebound[name]["env"]["UNITY_OPTIMIZE_LAKE_ARGS"], '["-Kenv=dev"]')
            self.assertNotIn("PRIVATE_PROVIDER_KEY", rebound[name]["env"])


if __name__ == "__main__":
    unittest.main()
