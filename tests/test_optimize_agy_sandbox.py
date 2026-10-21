"""Optimize-only AGY isolation regressions; no models, services, or builds.

Policy tests generate commands only. Native tests use an already usable local
Linux Bubblewrap and a fake AGY program; they never install or contact AGY.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity import optimize_agy_sandbox


_RUNTIME_CHILDREN = (
    "cache", "crashes", "scratch", "presence", "knowledge", "conversations",
    "implicit", "brain", "log",
)
_SHARED_CHILDREN = ("forum", "artifacts", "logs", "jobs", "tmp")


def _snapshot(paths):
    result = {}
    for root in paths:
        entries = [root, *sorted(root.rglob("*"))] if root.is_dir() else [root]
        for path in entries:
            if path.is_symlink():
                result[str(path)] = ("symlink", os.readlink(path))
            elif path.is_file():
                result[str(path)] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
            elif path.is_dir():
                result[str(path)] = ("directory",)
    return result


class _Fixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="optimize-agy-fixture-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.source = self.project / ".worktrees" / "readonly view"
        self.home = self.base / "home"
        self.home.mkdir()
        self.profile = self.home / ".gemini" / "antigravity-cli"
        self.profile.mkdir(parents=True)
        for name in _RUNTIME_CHILDREN:
            (self.profile / name).mkdir()
        (self.profile / "cli.log").write_text("retained log\n")
        (self.profile / "settings.json").write_text('{"fixture": "settings"}\n')
        (self.home / ".gemini" / "oauth_creds.json").write_text('{"fixture": true}\n')
        (self.home / ".gitconfig").write_text("# fixture settings\n")
        self.agy = self.base / "fake agy"
        self.agy.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "exec(compile(sys.argv[1], '<fake-agy>', 'exec'))\n"
        )
        self.agy.chmod(0o700)
        self.fake_bwrap = self.base / "fake bwrap"
        self.fake_bwrap.write_text("#!/bin/sh\nexit 0\n")
        self.fake_bwrap.chmod(0o700)
        self._git("init", "--quiet")
        (self.project / "Main.lean").write_text("theorem fixture : True := by trivial\n")
        (self.project / "nested").mkdir()
        (self.project / "nested" / "Child.lean").write_text("def fixtureValue := 1\n")
        self._git("add", "Main.lean", "nested/Child.lean")
        self._git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                  "commit", "--quiet", "-m", "fixture")
        self._git("worktree", "add", "--quiet", "--detach", str(self.source), "HEAD")
        self.git_common = self.project / ".git"
        source_unity = self.source / ".unity"
        source_unity.mkdir()
        for name in _SHARED_CHILDREN:
            shared = self.project / ".unity" / name
            shared.mkdir(parents=True)
            (source_unity / name).symlink_to(shared, target_is_directory=True)
        for name in ("agents.yaml", "config.yaml", "UNITY.md"):
            (self.project / ".unity" / name).write_text("fixture\n")
            (source_unity / name).write_text("fixture\n")
        (source_unity / "optimize-worktree.json").write_text(json.dumps({
            "schema": "optimize-worktree-v1", "project_root": str(self.project),
        }) + "\n")
        self.worker_tmp = self.project / ".unity" / "tmp" / "optimize" / "Fixture"
        for name in ("uv-tools", "uv-bin", "lake-cache"):
            (self.worker_tmp / name).mkdir(parents=True)
        self.env = {
            "HOME": str(self.home), "PATH": os.defpath,
            "UNITY_OPTIMIZE_PROJECT_ROOT": str(self.project),
            "UNITY_OPTIMIZE_WORKTREE": str(self.source),
            "UNITY_OPTIMIZE_PROFILE": "optimizing",
            "UNITY_OPTIMIZE_SCORE_REQUEST_ID": "fixture-score",
            "UNITY_OPTIMIZE_SCRATCH": "",
            "UV_TOOL_DIR": str(self.worker_tmp / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(self.worker_tmp / "uv-bin"),
            "LAKE_CACHE_DIR": str(self.worker_tmp / "lake-cache"),
            "FIXTURE_AUTH_SENTINEL": "not-a-real-credential",
        }
        self.protected = [
            self.source,
            self.project / "Main.lean", self.project / "nested", self.git_common,
            self.project / ".unity" / "agents.yaml", self.project / ".unity" / "config.yaml",
            self.profile / "settings.json", self.home / ".gemini" / "oauth_creds.json",
            self.home / ".gitconfig", self.agy,
        ]

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True, timeout=10)

    def prepare(self, *, env=None, native=False):
        if native:
            sandbox = optimize_agy_sandbox.prepare_sandbox(self.agy, self.source, env or self.env)
        else:
            original_which = shutil.which
            with patch.object(optimize_agy_sandbox.sys, "platform", "linux"), \
                    patch.object(optimize_agy_sandbox.shutil, "which", side_effect=lambda name, *a, **k:
                                 str(self.fake_bwrap) if name == "bwrap" else original_which(name, *a, **k)):
                sandbox = optimize_agy_sandbox.prepare_sandbox(self.agy, self.source, env or self.env)
        self.addCleanup(sandbox.close)
        return sandbox

    def scoring_fixture(self):
        from unity import optimize_metrics as metrics, optimize_tasks as tasks
        from unity import optimize_verification as verification

        directory = self.project / ".unity" / "forum" / "optimize"
        scope = {"targets": "Main.lean only", "files": ["Main.lean"]}
        metric_directory = self.project / ".unity" / "metrics"
        metric_directory.mkdir()
        definition = Path(metrics.__file__).parent / "defaults" / "metrics" / "length.md"
        (metric_directory / "length.md").write_bytes(definition.read_bytes())
        metric = metrics.load_metric(self.project, "length")
        method = {"description": "Offline tactic-count fixture", "direction": "lower"}
        tasks.initialize(directory, self.project, run_id="optimize-agy-fixture",
            base_sha=self._git("rev-parse", "HEAD").stdout.strip(), metric=metric,
            scope=scope, config={"primary": "Fixture", "agents": ["Fixture"]})
        tasks.install_inventory(directory, tasks.scan_sources(self.project, ["Main.lean"]))
        subject = next(iter(tasks.load(directory)["chunks"]))
        request = tasks.begin_scoring_request(directory, author="Fixture", worktree=self.source,
            source_identity=verification.file_identity(self.source, scope=scope),
            subjects=[subject], original_methods={subject: method})
        self.env.update(UNITY_AGENT_NAME="Fixture", UNITY_OPTIMIZE_AUTHOR="Fixture",
                        UNITY_OPTIMIZE_WORKFLOW="metric-optimization-v1",
                        UNITY_OPTIMIZE_SCORE_REQUEST_ID=request["request_id"])
        repository = str(Path(__file__).resolve().parents[1])
        server_bootstrap = (
            "import sys, runpy; "
            f"sys.path.insert(0, {repository!r}); "
            "runpy.run_module('unity.forum.optimize_server', run_name='__main__')"
        )
        server_arguments = ["-I", "-B", "-c", server_bootstrap,
            "--forum-dir", str(directory), "--project-root", str(self.project),
            "--profile", "optimizing", "--author", "Fixture"]
        arguments = {"author": "Fixture", "request_id": request["request_id"],
            "subject": subject, "raw_value": 1, "method": method,
            "evidence": {"reason": "Offline fixture has one explicit trivial tactic"}}
        # The shell bridge reads the real request; a real MCP stdio subprocess
        # records an advisory score. Neither path calls a compiler or provider.
        program = (
            "import asyncio, os, sys\n"
            f"sys.path.insert(0, {repository!r})\n"
            "from pathlib import Path\n"
            "Path('.gitconfig').write_text('private runner startup')\n"
            f"os.chdir({str(self.source)!r})\n"
            "from unity.optimize_mcp_bridge import main\n"
            "assert main(['mcp', 'unity-forum', 'scoring_request', '{}']) == 0\n"
            "from fastmcp import Client\n"
            "from fastmcp.client.transports import StdioTransport\n"
            "async def report():\n"
            f"    transport = StdioTransport(command={sys.executable!r}, args={server_arguments!r}, "
            "env=dict(os.environ), cwd=os.getcwd(), keep_alive=False)\n"
            "    async with Client(transport, timeout=20) as client:\n"
            "        inspected = await client.call_tool_mcp('scoring_request', {})\n"
            "        assert not inspected.isError, inspected.content\n"
            f"        result = await client.call_tool_mcp('submit_score_report', {arguments!r})\n"
            "        assert not result.isError, result.content\n"
            "asyncio.run(report())\n"
        )
        return directory, request["request_id"], subject, program

    def assert_advisory_score_only(self, directory, request_id, subject):
        from unity import optimize_tasks as tasks
        state = tasks.load(directory)
        report = state["scoring_requests"][request_id]["reports"][subject]
        self.assertEqual(report["raw_value"], 1)
        self.assertEqual(report["evidence_kind"], "agent_judged")
        self.assertEqual(state["baseline_measurements"], {})
        self.assertEqual(state["measurements"], {})
        self.assertFalse(state["baseline_sealed"])


class OptimizeAgySandboxPolicyTests(_Fixture, unittest.TestCase):
    def test_readonly_command_preserves_agy_arguments_network_auth_and_input(self):
        before = copy.deepcopy(self.env)
        sandbox = self.prepare()
        arguments = ["--print", "full prompt with spaces", "--model", "fixture-model",
                     "--output-format", "stream-json", "--sandbox"]
        command = sandbox.command(arguments)
        self.assertEqual(command[-len(arguments) - 1:], [str(self.agy), *arguments])
        for option in ("--unshare-user", "--unshare-pid", "--cap-drop", "--ro-bind",
                       "--proc", "--dev", "--die-with-parent", "--chdir"):
            self.assertIn(option, command)
        for option in ("--unshare-net", "--clearenv", "--new-session", "--disable-userns"):
            self.assertNotIn(option, command)
        self.assertEqual(self.env, before)
        self.assertNotIn("FIXTURE_AUTH_SENTINEL", command)
        self.assertNotIn("not-a-real-credential", command)

    def test_private_runner_and_control_executable_cannot_widen_source_writes(self):
        sandbox = self.prepare()
        roots = tuple(Path(path).resolve() for path in sandbox.writable_roots)
        for private in (sandbox.directory, sandbox.runner, sandbox.temporary):
            self.assertEqual(stat.S_IMODE(Path(private).stat().st_mode), 0o700)
            self.assertFalse(Path(private).is_relative_to(self.source))
            self.assertFalse(Path(private).is_relative_to(self.git_common))
        self.assertTrue(Path(sandbox.executable).is_file())
        for protected in (self.source, self.git_common, self.agy, sandbox.executable,
                          self.profile / "settings.json", self.home / ".gemini" / "oauth_creds.json"):
            self.assertFalse(any(Path(protected).resolve().is_relative_to(root) for root in roots))
        self.assertNotIn(self.home, roots)
        self.assertNotIn(self.project, roots)
        self.assertNotIn(self.project / ".unity", roots)
        self.assertNotIn(self.profile, roots)

    def test_only_existing_approved_agy_runtime_children_are_writable(self):
        (self.profile / "cache").rmdir()
        sandbox = self.prepare()
        roots = set(map(Path, sandbox.writable_roots))
        self.assertNotIn(self.profile / "cache", roots)
        self.assertFalse((self.profile / "cache").exists())
        for name in _RUNTIME_CHILDREN[1:]:
            self.assertIn(self.profile / name, roots)
        self.assertIn(self.profile / "cli.log", roots)
        self.assertNotIn(self.profile / "settings.json", roots)

    def test_missing_optional_profile_is_not_created(self):
        shutil.rmtree(self.profile)
        sandbox = self.prepare()
        self.assertFalse(self.profile.exists())
        self.assertFalse(any(Path(root).is_relative_to(self.profile) for root in sandbox.writable_roots))

    def test_exact_project_and_worktree_environment_bindings_are_required(self):
        for key in ("UNITY_OPTIMIZE_PROJECT_ROOT", "UNITY_OPTIMIZE_WORKTREE"):
            with self.subTest(key=key), self.assertRaises((ValueError, RuntimeError)):
                self.prepare(env={**self.env, key: str(self.base)})

    def test_missing_private_worktree_marker_is_rejected(self):
        (self.source / ".unity" / "optimize-worktree.json").unlink()
        with self.assertRaises((ValueError, RuntimeError)):
            self.prepare()

    def test_redirected_shared_runtime_to_source_is_rejected(self):
        runtime = self.project / ".unity" / "logs"
        runtime.rmdir()
        runtime.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises((ValueError, RuntimeError)):
            self.prepare()

    def test_redirected_agy_cache_to_git_is_rejected(self):
        cache = self.profile / "cache"
        cache.rmdir()
        cache.symlink_to(self.git_common, target_is_directory=True)
        with self.assertRaises((ValueError, RuntimeError)):
            self.prepare()

    def test_agy_home_inside_source_or_git_cannot_grant_writable_descendants(self):
        for root in (self.source, self.project, self.git_common):
            with self.subTest(root=root):
                home = root / "fixture-home"
                cache = home / ".gemini" / "antigravity-cli" / "cache"
                cache.mkdir(parents=True)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.prepare(env={**self.env, "HOME": str(home)})

    def test_runtime_environment_cannot_escape_project_tmp(self):
        for key in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "LAKE_CACHE_DIR"):
            for destination in (self.source, self.git_common, self.base):
                with self.subTest(key=key, destination=destination), self.assertRaises((ValueError, RuntimeError)):
                    self.prepare(env={**self.env, key: str(destination)})

    def test_runtime_hardlink_to_source_or_settings_is_rejected(self):
        for protected in (self.source / "Main.lean", self.profile / "settings.json", self.agy):
            alias = self.profile / "cache" / "protected-alias"
            with self.subTest(protected=protected):
                os.link(protected, alias)
                try:
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.prepare()
                finally:
                    alias.unlink()

    def test_unsupported_platform_and_missing_bwrap_fail_closed(self):
        with patch.object(optimize_agy_sandbox.sys, "platform", "darwin"):
            with self.assertRaises((ValueError, RuntimeError)):
                optimize_agy_sandbox.prepare_sandbox(self.agy, self.source, self.env)
        with patch.object(optimize_agy_sandbox.sys, "platform", "linux"), \
                patch.object(optimize_agy_sandbox.shutil, "which", return_value=None):
            with self.assertRaises((ValueError, RuntimeError)):
                optimize_agy_sandbox.prepare_sandbox(self.agy, self.source, self.env)

    def test_preflight_executes_only_true_and_fails_closed(self):
        sandbox = self.prepare()
        with patch.object(optimize_agy_sandbox.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1)) as run:
            with self.assertRaises(RuntimeError):
                sandbox.preflight()
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--", "/bin/true"])
        self.assertNotIn(str(self.agy), command)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_trampoline_is_isolated_before_any_python_startup_import(self):
        self.fake_bwrap.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        sandbox = self.prepare()
        untrusted = self.base / "untrusted-python"
        untrusted.mkdir()
        sentinel = self.base / "unexpected-startup-execution"
        (untrusted / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed outside sandbox')\n"
        )
        result = subprocess.run([str(sandbox.executable), "--print", "feedback with spaces"],
            cwd=sandbox.runner, env={**os.environ, **self.env, "PYTHONPATH": str(untrusted)},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists(), "Python startup code ran before the sandbox")
        self.assertEqual(result.stdout.splitlines()[-3:], [str(self.agy), "--print", "feedback with spaces"])

    def test_outer_owned_group_wrapper_is_isolated_before_python_startup(self):
        from unity.optimize_spawn import _process_group_wrapper
        self.fake_bwrap.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        sandbox = self.prepare()
        wrapper, pid_file = _process_group_wrapper(Path(sandbox.executable),
            Path(sandbox.directory), "fixture-agy", isolated=True)
        untrusted = self.base / "outer-untrusted-python"
        untrusted.mkdir()
        sentinel = self.base / "unexpected-outer-startup"
        (untrusted / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed before owned wrapper')\n"
        )
        environment = {**self.env, "PYTHONPATH": str(untrusted)}
        result = subprocess.run([str(wrapper), "--print", "feedback with spaces"],
            cwd=sandbox.runner, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists(), "Python startup ran before the owned process wrapper")
        self.assertGreater(int(pid_file.read_text()), 0)
        self.assertEqual(result.stdout.splitlines()[-3:], [str(self.agy), "--print", "feedback with spaces"])

    def test_local_forum_bridge_and_stdio_fixture_without_model_or_compiler(self):
        directory, request_id, subject, program = self.scoring_fixture()
        sandbox = self.prepare()
        before = _snapshot(self.protected)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program],
            cwd=sandbox.runner, env={**self.env, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(request_id, result.stdout)
        self.assert_advisory_score_only(directory, request_id, subject)
        self.assertEqual(_snapshot(self.protected), before)
        self.assertTrue((sandbox.runner / ".gitconfig").exists())
        self.assertFalse((self.source / ".gitconfig").exists())

    def test_command_revalidates_runtime_binding_before_launch(self):
        sandbox = self.prepare()
        original = self.profile / "cache"
        retained = self.profile / "retained-cache"
        original.rename(retained)
        original.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            sandbox.command(["--print", "fixture"])

    def test_vanished_runtime_entry_during_hardlink_scan_is_not_a_binding_failure(self):
        vanished = self.profile / "cache" / "already-removed-worker-file"
        with patch.object(optimize_agy_sandbox, "_files", return_value=iter([vanished])):
            optimize_agy_sandbox._check_hardlinks((self.profile / "cache",), (self.source,))
        optimize_agy_sandbox._walk_error(FileNotFoundError("worker removed scratch"))
        with self.assertRaises(PermissionError):
            optimize_agy_sandbox._walk_error(PermissionError("cannot inspect runtime"))
        sandbox = self.prepare()
        (self.profile / "cache").rmdir()
        with self.assertRaises((ValueError, OSError)):
            sandbox.command(["--print", "fixture"])

    def test_close_rejects_replaced_control_directory_without_deleting_it(self):
        sandbox = self.prepare()
        directory = Path(sandbox.directory)
        retained = directory.with_name(directory.name + "-retained")
        directory.rename(retained)
        directory.mkdir()
        sentinel = directory / "must-survive"
        sentinel.write_text("unrelated replacement")
        try:
            with self.assertRaises((ValueError, RuntimeError)):
                sandbox.close()
            self.assertEqual(sentinel.read_text(), "unrelated replacement")
        finally:
            sentinel.unlink()
            directory.rmdir()
            retained.rename(directory)

    def test_close_removes_only_its_private_root_and_is_repeatable(self):
        before = _snapshot(self.protected)
        sandbox = self.prepare()
        directory = Path(sandbox.directory)
        sandbox.close()
        sandbox.close()
        self.assertFalse(directory.exists())
        self.assertEqual(_snapshot(self.protected), before)
        self.assertTrue(self.worker_tmp.exists())


@unittest.skipUnless(sys.platform.startswith("linux"), "requires local Linux Bubblewrap; no VM is started")
class OptimizeAgyNativeSandboxTests(_Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            self.skipTest("requires already installed local Bubblewrap; never installs")
        # Determine environment support independently of production's plan.
        # Once this baseline works, a bad production mount or preflight fails
        # the test instead of being misclassified as an environment skip.
        try:
            available = subprocess.run([bwrap, "--unshare-user", "--unshare-pid",
                "--ro-bind", "/", "/", "--proc", "/proc", "--", "/bin/true"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest("local Bubblewrap namespaces unavailable: " + str(exc).splitlines()[0][:250])
        if available.returncode:
            self.skipTest("independent local Bubblewrap namespace baseline unavailable")
        self.sandbox = self.prepare(native=True)
        self.sandbox.preflight()
        self.before = _snapshot(self.protected)

    def run_fake(self, program):
        return subprocess.run(self.sandbox.command([program]), cwd=self.source,
                              env={**os.environ, **self.env}, capture_output=True,
                              text=True, timeout=15)

    def assert_unchanged(self):
        self.assertEqual(_snapshot(self.protected), self.before)

    def test_native_editor_and_shell_source_writes_are_denied(self):
        source = str(self.source)
        operations = (
            "(root / 'created.lean').write_text('changed')",
            "(root / 'Main.lean').write_text('changed')",
            "(root / 'Main.lean').unlink()",
            "(root / 'Main.lean').rename(root / 'renamed.lean')",
            "(root / 'nested' / 'Child.lean').write_text('changed')",
            "(root / 'new-alias').symlink_to(root / 'Main.lean')",
        )
        for operation in operations:
            with self.subTest(operation=operation):
                result = self.run_fake(f"from pathlib import Path; root = Path({source!r}); {operation}")
                self.assertNotEqual(result.returncode, 0)
                self.assert_unchanged()
        result = self.run_fake("import subprocess; raise SystemExit(subprocess.call(" + repr([
            "/bin/sh", "-c", 'printf changed > "$1/Main.lean"', "fixture", source]) + "))")
        self.assertNotEqual(result.returncode, 0)
        self.assert_unchanged()

    def test_main_git_configuration_and_launcher_writes_are_denied(self):
        for path in (self.project / "Main.lean", self.git_common / "config",
                     self.source / ".unity" / "agents.yaml", self.profile / "settings.json",
                     self.home / ".gemini" / "oauth_creds.json", self.sandbox.executable):
            with self.subTest(path=path):
                result = self.run_fake(f"from pathlib import Path; Path({str(path)!r}).write_text('changed')")
                self.assertNotEqual(result.returncode, 0)
                self.assert_unchanged()

    def test_absent_settings_file_cannot_be_created(self):
        settings = self.profile / "settings.json"
        settings.unlink()  # Establish the missing-settings fixture before entering the sandbox.
        self.before = _snapshot(self.protected)
        result = self.run_fake(f"from pathlib import Path; Path({str(settings)!r}).write_text('{{}}')")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(settings.exists())
        self.assert_unchanged()

    def test_real_forum_bridge_and_stdio_score_reporting_remain_usable(self):
        directory, request_id, subject, program = self.scoring_fixture()
        self.before = _snapshot(self.protected)
        result = self.run_fake(program)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(request_id, result.stdout)
        self.assert_advisory_score_only(directory, request_id, subject)
        self.assertTrue((self.sandbox.runner / ".gitconfig").exists())
        self.assertFalse((self.source / ".gitconfig").exists())
        self.assert_unchanged()

    def test_private_runner_startup_source_reads_and_runtime_writes_work(self):
        result = self.run_fake(
            "from pathlib import Path; import os; "
            f"assert Path.cwd() == Path({str(self.sandbox.runner)!r}); "
            "Path('.gitconfig').write_text('private startup'); "
            f"assert 'theorem fixture' in Path({str(self.source / 'Main.lean')!r}).read_text(); "
            f"Path({str(self.project / '.unity' / 'logs' / 'fixture.log')!r}).write_text('runtime'); "
            "assert os.environ['FIXTURE_AUTH_SENTINEL'] == 'not-a-real-credential'"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.source / ".gitconfig").exists())
        self.assert_unchanged()

    def test_writable_runtime_symlink_cannot_write_source(self):
        alias = self.project / ".unity" / "logs" / "source-alias"
        alias.symlink_to(self.source / "Main.lean")
        result = self.run_fake(f"from pathlib import Path; Path({str(alias)!r}).write_text('changed')")
        self.assertNotEqual(result.returncode, 0)
        self.assert_unchanged()

    def test_proc_is_private_and_self_root_does_not_escape_readonly_mount(self):
        result = self.run_fake(
            "from pathlib import Path; "
            f"assert not Path('/proc/{os.getpid()}/root').exists(); "
            f"Path('/proc/self/root{self.source}/Main.lean').write_text('changed')"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("AssertionError", result.stderr)
        self.assert_unchanged()

    def test_nested_bwrap_cannot_regrant_source_writes(self):
        nested = [shutil.which("bwrap"), "--unshare-user", "--ro-bind", "/", "/",
                  "--bind", str(self.source), str(self.source), "--", "/bin/sh", "-c",
                  'printf changed > "$1/Main.lean"', "fixture", str(self.source)]
        result = self.run_fake(f"import subprocess; raise SystemExit(subprocess.call({nested!r}))")
        self.assertNotEqual(result.returncode, 0)
        self.assert_unchanged()

    def test_nested_sandbox_can_read_source_and_write_private_runner(self):
        nested = [shutil.which("bwrap"), "--unshare-user", "--ro-bind", "/", "/",
                  "--bind", str(self.sandbox.runner), str(self.sandbox.runner),
                  "--chdir", str(self.sandbox.runner), "--", "/bin/sh", "-c",
                  'cat "$1/Main.lean" && printf nested > nested.log', "fixture", str(self.source)]
        result = self.run_fake(f"import subprocess; raise SystemExit(subprocess.call({nested!r}))")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("theorem fixture", result.stdout)
        self.assertEqual((self.sandbox.runner / "nested.log").read_text(), "nested")
        self.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
