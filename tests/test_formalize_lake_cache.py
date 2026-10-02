"""Worker Lake artifacts stay writable without granting toolchain writes.

The optional native test uses only an already-installed Lean and an OS sandbox;
it builds a trivial dependency-free fixture, never an evaluation or model call.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from unity.config import Paths
from unity import formalize_runtime


class FormalizeLakeCacheTests(unittest.TestCase):
    def test_cache_is_run_worker_scoped_without_changing_artifact_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.from_unity_dir(Path(directory).resolve() / ".unity")
            before = dict(os.environ)
            caches = []
            for run, worker in (("run-1", "Luna1"), ("run-1", "Luna2"), ("run-2", "Luna1")):
                env = formalize_runtime._agent_runtime_env(paths, {"run_id": run}, worker)
                cache = Path(env["LAKE_CACHE_DIR"])
                self.assertEqual(cache, paths.unity / "tmp" / run / worker / "lake-cache")
                self.assertTrue(cache.is_dir())
                self.assertNotIn("LAKE_ARTIFACT_CACHE", env)
                self.assertNotIn("LAKE_NO_CACHE", env)
                caches.append(cache)
            self.assertEqual(len(set(caches)), 3)
            self.assertEqual(dict(os.environ), before)

    def test_all_roles_pin_active_unity_without_following_python_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime_bin = root / "isolated-runtime" / "bin"
            runtime_bin.mkdir(parents=True)
            python = runtime_bin / "python"
            python.symlink_to(sys.executable)
            unity = runtime_bin / "unity"
            unity.write_text("#!/bin/sh\nexit 0\n")
            unity.chmod(0o700)
            global_bin = root / "other-runtime" / "bin"
            global_bin.mkdir(parents=True)
            other_unity = global_bin / "unity"
            other_unity.write_text("#!/bin/sh\nexit 1\n")
            other_unity.chmod(0o700)
            paths = Paths.from_unity_dir(root / "project" / ".unity")
            with patch("unity.formalize_runtime.sys.executable", str(python)), \
                    patch.dict(os.environ, {"PATH": str(global_bin)}):
                for phase in ("chunking", "critic", "retrospective", "formalizing"):
                    with self.subTest(phase=phase):
                        env = formalize_runtime._agent_runtime_env(paths, {"run_id": "path-test", "phase": phase}, "Luna1")
                        self.assertEqual(shutil.which("unity", path=env["PATH"]), str(unity))
                        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(runtime_bin))
                        self.assertEqual(env["UNITY_FORMALIZE_PROFILE"], phase)

    def test_formalizing_lake_guard_precedes_pinned_unity_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = Paths.from_unity_dir(root / ".unity")
            with patch("unity.formalize_runtime.shutil.which", return_value="/fixture/lake"):
                env = formalize_runtime._agent_runtime_env(paths, {"run_id": "path-test", "phase": "formalizing"}, "Luna1")
            entries = env["PATH"].split(os.pathsep)
            self.assertEqual(entries[0], str(paths.unity / "bin" / "formalize"))
            self.assertEqual(entries[1], str(Path(sys.executable).absolute().parent.resolve()))
            self.assertEqual(env["UNITY_REAL_LAKE"], "/fixture/lake")


class InstalledLeanCacheSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        explicit = os.environ.get("UNITY_TEST_LEAN_TOOLCHAIN")
        candidates = ([Path(explicit)] if explicit else [
            Path.home() / ".elan" / "toolchains" / ("leanprover--lean4---v" + version)
            for version in ("4.34.1", "4.34.0")
        ])
        cls.chain = next((p.resolve() for p in candidates if (p / "bin/lake").is_file()), None)
        if cls.chain is None:
            raise unittest.SkipTest("requires installed Lean4.34; never downloads")
        if sys.platform == "linux":
            cls.sandbox = shutil.which("bwrap")
        elif sys.platform == "darwin":
            cls.sandbox = shutil.which("sandbox-exec")
        else:
            cls.sandbox = None
        if cls.sandbox is None:
            raise unittest.SkipTest("requires bwrap or sandbox-exec")

    def test_real_guarded_build_redirects_artifacts_inside_sandbox(self):
        with tempfile.TemporaryDirectory(prefix="unity-lake-cache-test-") as directory:
            root = Path(directory).resolve()
            work = root / "work"
            work.mkdir()
            forbidden = root / "readonly-cache"
            forbidden.mkdir()
            marker = forbidden / "preserved"
            marker.write_text("unchanged\n")
            source = "def cacheProbe" + uuid.uuid4().hex + " : Nat := 1729\n"
            (work / "CacheFixture.lean").write_text(source)
            (work / "lakefile.toml").write_text(
                'name = "cache_fixture"\nversion = "0.1.0"\n'
                'defaultTargets = ["CacheFixture"]\nenableArtifactCache = true\n'
                '[[lean_lib]]\nname = "CacheFixture"\n')
            version = self.chain.name.rsplit("---v", 1)[-1]
            (work / "lean-toolchain").write_text("leanprover/lean4:v" + version + "\n")
            paths = Paths.from_unity_dir(work / ".unity")
            with patch("unity.formalize_runtime.shutil.which", return_value=str(self.chain / "bin/lake")):
                runtime = formalize_runtime._agent_runtime_env(
                    paths, {"run_id": "sandbox-test", "phase": "formalizing"}, "Luna1",
                )
            env = {**os.environ, **runtime, "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": str(Path(formalize_runtime.__file__).resolve().parents[1]),
                   "LEAN_SYSROOT": str(self.chain), "LEAN_PATH": "", "UNITY_AGENT_NAME": "Luna1"}
            if sys.platform == "linux":
                prefix = [self.sandbox, "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/",
                          "--bind", str(work), str(work), "--proc", "/proc", "--dev", "/dev",
                          "--chdir", str(work), "--"]
            else:
                policy = ('(version 1)(allow default)(deny network*)(deny file-write*)'
                          '(allow file-write* (subpath ' + json.dumps(str(work)) + '))'
                          '(allow file-write* (literal "/dev/null"))')
                prefix = [self.sandbox, "-p", policy]
            command = [*prefix, sys.executable, "-m", "unity.formalize_lake_guard", "build", "CacheFixture"]
            rejected = subprocess.run(command, cwd=work, env={**env, "LAKE_CACHE_DIR": str(forbidden)},
                                      capture_output=True, text=True, timeout=180)
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
            self.assertIn("failed to cache artifact", rejected.stdout + rejected.stderr)
            self.assertEqual(list(forbidden.iterdir()), [marker])
            self.assertEqual(marker.read_text(), "unchanged\n")
            accepted = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=180)
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            artifacts = list(Path(runtime["LAKE_CACHE_DIR"]).rglob("*.olean"))
            self.assertTrue(artifacts, "build must really write native artifact cache")
            self.assertEqual((work / "CacheFixture.lean").read_text(), source)
            self.assertEqual(list(forbidden.iterdir()), [marker])
            checked = subprocess.run([*prefix, sys.executable, "-m", "unity.formalize_lake_guard",
                                      "env", "lean", "CacheFixture.lean"],
                                     cwd=work, env=env, capture_output=True, text=True, timeout=180)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertEqual(list((work / ".unity/jobs/formalize").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
