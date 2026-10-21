"""Production Optimize scoring startup, without a campaign or compiler.

The native regression replaces only the AGY executable with a local transport
double. Worktree creation, worker environment, shared dispatch, Optimize policy,
Bubblewrap, Forum bridge, and process ownership are real.

The separate real-AGY test is disabled unless UNITY_TEST_REAL_AGY=1 and
UNITY_TEST_REAL_AGY_MODEL names an explicitly approved model. It uses existing
CLI authentication and a disposable project, never an evaluation checkpoint.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from unity import optimize_metrics as metrics, optimize_tasks as tasks
from unity import optimize_verification as verification, optimize_workflow as workflow
from unity import optimize_spawn
from unity.roster import Agent, Roster


_REPOSITORY = Path(__file__).resolve().parents[1]
_METHOD = {"description": "Count explicit tactic invocations in the declaration body.",
           "direction": "lower"}
_RUNTIME = ("cache", "crashes", "scratch", "presence", "knowledge",
            "conversations", "implicit", "brain", "log")


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _protected_snapshot(paths):
    return {str(path): {"sha256": _digest(path), "mode": stat.S_IMODE(path.stat().st_mode)}
            if path.exists() or path.is_symlink() else None for path in paths}


def _fake_agy_program():
    # This consumes the actual generated CLI command and worker environment.
    # It does not call a task API directly or invent the scoring request ID.
    return f"#!{sys.executable}\n" + textwrap.dedent(r'''
        import json, os, shutil, subprocess, sys
        from pathlib import Path

        def main():
            assert '--print' in sys.argv and '--sandbox' in sys.argv
            assert sys.argv[sys.argv.index('--output-format') + 1] == 'stream-json'
            source = Path(os.environ['UNITY_OPTIMIZE_WORKTREE'])
            project = Path(os.environ['UNITY_OPTIMIZE_PROJECT_ROOT'])
            runner = Path.cwd()
            assert not runner.is_relative_to(source)
            assert not runner.is_relative_to(project)
            assert not os.environ.get('UNITY_OPTIMIZE_SCRATCH')
            Path('.gitconfig').write_text('# private startup file\n')
            profile = Path(os.environ['HOME']) / '.gemini/antigravity-cli'
            log = profile / 'cli.log'
            assert log.is_symlink()
            with log.open('a') as handle:
                handle.write('native scoring startup fixture\n')
            agentapi = profile / 'bin/agentapi'
            replacement = '#!/bin/sh\n# private scoring startup fixture\nexit 0\n'
            agentapi.write_text(replacement)
            agentapi.chmod(0o600)
            assert agentapi.stat().st_mode & 0o777 == 0o600
            agentapi.chmod(0o700)
            assert agentapi.read_text() == replacement
            assert agentapi.stat().st_mode & 0o777 == 0o700

            denied = []
            for target in (source / 'Main.lean', project / 'Main.lean',
                           project / '.git/config', source / '.unity/config.yaml',
                           profile / 'settings.json',
                           Path(os.environ['HOME']) / '.gemini/oauth_creds.json',
                           profile / 'bin/.unity-scoring-startup-sentinel'):
                try:
                    with target.open('a') as handle:
                        handle.write('UNAUTHORIZED FIXTURE WRITE\n')
                except OSError:
                    denied.append(str(target))
                else:
                    raise AssertionError('protected write was permitted')
            try:
                log.unlink()
            except OSError:
                pass
            else:
                raise AssertionError('profile log alias could be replaced')
            assert (source / 'Main.lean').read_text() == 'theorem fixture : True := by trivial\n'
            os.chdir(source)
            bridge = shutil.which('unity')
            assert Path(bridge).parent == project / '.unity/bin/optimize-workflow'

            def call(tool, arguments):
                result = subprocess.run([bridge, 'mcp', 'unity-forum', tool,
                    json.dumps(arguments)], capture_output=True, text=True, timeout=45)
                assert result.returncode == 0, (tool, result.returncode, result.stderr[-2000:])
                return json.loads(result.stdout)

            request = call('scoring_request', {})
            assert request['request_id'] == os.environ['UNITY_OPTIMIZE_SCORE_REQUEST_ID']
            assert request['root'] == str(source)
            for subject in request['subjects']:
                report = call('submit_score_report', {
                    'author': os.environ['UNITY_AGENT_NAME'],
                    'request_id': request['request_id'], 'subject': subject, 'raw_value': 1,
                    'method': json.loads(os.environ['UNITY_TEST_SCORE_METHOD']),
                    'evidence': {'fixture': 'Read Main.lean; its body has one trivial tactic.',
                                 'source_writes_denied': len(denied),
                                 'private_runner': str(runner), 'bridge': bridge}})
                assert report['reported'] is True
            (project / '.unity/tmp/scoring-startup-complete.json').write_text(json.dumps({
                'request_id': request['request_id'], 'subjects': request['subjects'],
                'source_writes_denied': len(denied)}))
            print(json.dumps({'event': 'result', 'result': {
                'status': 'SUCCESS', 'response': 'Fixture scoring report submitted.'}}), flush=True)

        try:
            main()
        except Exception as exc:
            # Exit normally so the controller reports missing evidence rather
            # than retrying a deterministic test-double assertion.
            print(json.dumps({'event': 'result', 'result': {
                'status': 'SUCCESS', 'response': 'Fixture assertion: ' + repr(exc)}}), flush=True)
    ''')


class _ScoringFixture:
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="optimize-agy-startup-")).resolve()
        self._safe_to_remove = False
        self._retain_success = self._testMethodName == "test_opt_in_real_agy_scoring_startup"
        self.addCleanup(self.cleanup_fixture)
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.project / "Main.lean").write_text("theorem fixture : True := by trivial\n")
        (self.project / ".gitignore").write_text(".unity/\n.worktrees/\n.lake/\n")
        self.git("add", "Main.lean", ".gitignore")
        self.git("commit", "--quiet", "-m", "scoring startup fixture")
        self.paths = workflow.paths_for(self.project)
        self.paths.unity.mkdir()
        (self.paths.unity / "agents.yaml").write_text("agents: []\n")
        (self.paths.unity / "config.yaml").write_text("{}\n")
        self.paths.unity_md.write_text(
            "This is a disposable scoring startup test, not an optimization evaluation.\n"
            "Read Main.lean and submit its requested tactic count through unity-forum.\n"
            "Do not run a compiler, build, provider probe, or external service.\n"
            "Use the supplied original method without changes.\n"
            "Original method: " + json.dumps(_METHOD, sort_keys=True) + "\n")
        metric_path = self.paths.unity / "metrics/length.md"
        metric_path.parent.mkdir()
        metric_path.write_bytes((Path(metrics.__file__).parent / "defaults/metrics/length.md").read_bytes())
        metric = metrics.load_metric(self.project, "length")
        self.scope = {"targets": "Main.lean only", "files": ["Main.lean"], "modules": ["Main"]}
        tasks.initialize(self.paths.forum, self.project, run_id="optimize-agy-startup-fixture",
            base_sha=self.git("rev-parse", "HEAD"), metric=metric, scope=self.scope,
            config={"primary": "Fixture", "agents": ["Fixture"], "max_rounds": 1,
                    "lake_args": [], "real_lake": sys.executable})
        tasks.install_inventory(self.paths.forum, tasks.scan_sources(self.project, ["Main.lean"]))
        self.subject = next(iter(tasks.load(self.paths.forum)["chunks"]))
        self.request = {"source_identity": verification.file_identity(self.project, scope=self.scope),
            "subjects": [self.subject], "original_methods": {self.subject: _METHOD},
            "metric": metric, "build_result": {"fixture_only": True, "build_was_not_run": True}}
        self.protected = [self.project / "Main.lean", self.project / ".git/config",
                          self.paths.unity / "config.yaml", self.paths.unity / "agents.yaml",
                          self.paths.unity_md, metric_path]

    def cleanup_fixture(self):
        result = getattr(getattr(self, "_outcome", None), "result", None)
        failures = getattr(result, "failures", []) + getattr(result, "errors", [])
        failed = any(case is self or getattr(case, "test_case", None) is self for case, _ in failures)
        if self._safe_to_remove and not self._retain_success and not failed:
            shutil.rmtree(self.base)
        else:
            print("Retained Optimize AGY test fixture: " + str(self.base), flush=True)

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], cwd=self.project, capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()

    def make_profile(self):
        home = self.base / "home"
        profile = home / ".gemini/antigravity-cli"
        profile.mkdir(parents=True)
        for name in _RUNTIME:
            (profile / name).mkdir()
        (profile / "settings.json").write_text('{"fixture": true}\n')
        (home / ".gemini/oauth_creds.json").write_text('{"not_real_auth": true}\n')
        (home / ".gitconfig").write_text("# fixture\n")
        (profile / "log/cli-fixture.log").write_text("retained log\n")
        (profile / "cli.log").symlink_to("log/cli-fixture.log")
        (profile / "bin").mkdir()
        (profile / "bin/agentapi").write_text("#!/bin/sh\nexit 0\n")
        (profile / "bin/agentapi").chmod(0o700)
        self.protected.extend((profile / "settings.json", home / ".gemini/oauth_creds.json",
                               home / ".gitconfig", profile / "bin/agentapi",
                               profile / "bin/.unity-scoring-startup-sentinel"))
        return home, profile

    def controller(self, model):
        agent = Agent("Fixture", model, "antigravity", "antigravity", 1,
                      None, None, None, None, True)
        return workflow.Controller(self.paths, Roster([agent]), max_attempts=1)

    def environment(self):
        env = dict(os.environ)
        # Do not expose optional service credentials to this isolated test.
        for name in ("AXLE_API_KEY", "ARISTOTLE_API_KEY"):
            env.pop(name, None)
        env["PYTHONPATH"] = str(_REPOSITORY)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def assert_closed(self, before):
        self.assertEqual(_protected_snapshot(self.protected), before)
        self.assertEqual(verification.file_identity(self.project, scope=self.scope),
                         self.request["source_identity"])
        state = tasks.load(self.paths.forum)
        self.assertEqual(len(state["scoring_requests"]), 1)
        request = next(iter(state["scoring_requests"].values()))
        self.assertEqual(request["status"], "complete")
        self.assertEqual(request["source_identity"], self.request["source_identity"])
        self.assertEqual(request["reports"][self.subject]["raw_value"], 1)
        self.assertEqual(request["reports"][self.subject]["evidence_kind"], "agent_judged")
        self.assertEqual(state["baseline_measurements"], {})
        self.assertEqual(state["measurements"], {})
        self.assertFalse(state["baseline_sealed"])
        self.assertEqual(state["rounds"]["started"], 0)
        self.assertEqual(state["candidates"], {})
        self.assertFalse((self.paths.unity / "stop-requested").exists())
        receipts = [path for path in (self.paths.forum / "processes").glob("*.json")
                    if not path.name.endswith(".closed.json")]
        self.assertTrue(receipts, "real shared dispatch must create owned-process receipts")
        for path in receipts:
            receipt = json.loads(path.read_text())
            closed = json.loads(path.with_suffix(".closed.json").read_text())
            self.assertTrue(closed["group_empty"])
            self.assertEqual(closed["receipt_sha256"], _digest(path))
            self.assertFalse(optimize_spawn._process_group_alive(receipt["pgid"]))
            self.assertFalse(Path(receipt["executable"]).parent.exists(), "sandbox controls must close")
        return request


class OptimizeAgyStartupFixtureTests(_ScoringFixture, unittest.TestCase):
    def test_fixture_uses_production_worktree_and_runtime_environment(self):
        home, profile = self.make_profile()
        tree = workflow.task_worktree(self.paths, "Fixture-score", "verification")
        env = workflow.runtime_env(self.paths, "Fixture", None, "optimizing", tree,
                                   tasks.load(self.paths.forum), score_request_id="fixture-only")
        self.assertEqual(Path(env["UNITY_OPTIMIZE_PROJECT_ROOT"]), self.project)
        self.assertEqual(Path(env["UNITY_OPTIMIZE_WORKTREE"]), tree)
        self.assertEqual(env["UNITY_OPTIMIZE_SCRATCH"], "")
        self.assertEqual((tree / ".unity/forum").resolve(), self.paths.unity / "forum")
        self.assertEqual((profile / "cli.log").resolve(), profile / "log/cli-fixture.log")
        self.assertTrue((profile / "cli.log").is_symlink())
        compile(_fake_agy_program(), "fake-agy-startup", "exec")
        self._safe_to_remove = True  # This fixture-only test starts no owned worker.


@unittest.skipUnless(sys.platform.startswith("linux"), "requires local Linux Bubblewrap; no VM is started")
class OptimizeAgyNativeStartupTests(_ScoringFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        bwrap = shutil.which("bwrap")
        if not bwrap:
            self.skipTest("requires installed Bubblewrap; never installs")
        try:
            available = subprocess.run([bwrap, "--unshare-user", "--unshare-pid",
                "--ro-bind", "/", "/", "--proc", "/proc", "--", "/bin/true"],
                capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("independent Bubblewrap namespace baseline unavailable")
        if available.returncode:
            self.skipTest("independent Bubblewrap namespace baseline unavailable")

    async def test_real_scoring_dispatch_with_log_alias_and_native_readonly_checks(self):
        home, profile = self.make_profile()
        binary = self.base / "bin"
        binary.mkdir()
        executable = binary / "agy"
        executable.write_text(_fake_agy_program())
        executable.chmod(0o700)
        env = self.environment()
        env.update(HOME=str(home), PATH=str(binary) + os.pathsep + env.get("PATH", os.defpath),
                   UNITY_TEST_SCORE_METHOD=json.dumps(_METHOD))
        before = _protected_snapshot(self.protected)
        with patch.dict(os.environ, env, clear=True):
            result = await asyncio.wait_for(self.controller("fixture-model").judge_snapshot(self.request), 120)
        request = self.assert_closed(before)
        self.assertEqual(set(result["measurements"]), {self.subject})
        self.assertEqual(request["reports"][self.subject]["evidence"]["source_writes_denied"], 7)
        self.assertTrue((profile / "cli.log").is_symlink())
        self.assertIn("native scoring startup fixture", (profile / "cli.log").read_text())
        marker = json.loads((self.paths.unity / "tmp/scoring-startup-complete.json").read_text())
        self.assertEqual(marker, {"request_id": request["request_id"],
            "subjects": [self.subject], "source_writes_denied": 7})
        self._safe_to_remove = True  # All assertions and owned closure passed.

    @unittest.skipUnless(os.getenv("UNITY_TEST_REAL_AGY") == "1",
                         "real AGY and model calls require explicit UNITY_TEST_REAL_AGY=1")
    async def test_opt_in_real_agy_scoring_startup(self):
        model = os.getenv("UNITY_TEST_REAL_AGY_MODEL", "").strip()
        self.assertTrue(model, "set the explicitly approved UNITY_TEST_REAL_AGY_MODEL")
        self.assertIsNotNone(shutil.which("agy"), "real AGY must already be installed")
        env = self.environment()
        home = Path(env.get("HOME", str(Path.home()))).resolve()
        profile = home / ".gemini/antigravity-cli"
        self.assertTrue((profile / "cli.log").is_symlink(), "exercise the actual AGY log alias")
        credentials = [profile / "settings.json", home / ".gemini/oauth_creds.json", home / ".gitconfig",
                       profile / "bin/agentapi", profile / "bin/.unity-scoring-startup-sentinel"]
        self.protected.extend(credentials)
        before = _protected_snapshot(self.protected)
        with patch.dict(os.environ, env, clear=True):
            await asyncio.wait_for(self.controller(model).judge_snapshot(self.request), 180)
        self.assert_closed(before)
        self._safe_to_remove = True  # Real-AGY evidence is retained even on success.


if __name__ == "__main__":
    unittest.main()
