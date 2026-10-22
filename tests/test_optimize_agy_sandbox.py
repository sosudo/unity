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
        self.shell_helper = self.profile / "bin" / "agentapi"
        self.shell_helper.parent.mkdir()
        self.shell_helper.write_text("#!/bin/sh\nexit 0\n")
        self.shell_helper.chmod(0o700)
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
            self.home / ".gitconfig", self.agy, self.shell_helper,
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

    def test_missing_profile_fails_closed_without_host_placeholders(self):
        shutil.rmtree(self.profile)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.prepare()
        self.assertFalse(self.profile.exists())

    def test_native_settings_overlay_keeps_exact_runtime_rules_and_host_bytes(self):
        path = self.profile / "settings.json"
        original = {"fixture": "preserved", "permissions": {
            "allow": ["read_file(/existing)"], "deny": ["write_file(/restricted)"],
            "ask": ["read_url(example.invalid)"]}, "toolPermission": "always-proceed"}
        path.write_text(json.dumps(original))
        before = path.read_bytes()
        sandbox = self.prepare()
        private = sandbox.directory / "agy-settings.json"
        settings = json.loads(private.read_text())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(settings["fixture"], "preserved")
        self.assertEqual(settings["toolPermission"], "proceed-in-sandbox")
        self.assertIs(settings["enableTerminalSandbox"], True)
        self.assertEqual(settings["permissions"]["allow"], ["read_file(/existing)",
            *(f"write_file({root})" for root in sandbox.writable_roots),
            *(f"read_file({root})" for root in dict.fromkeys((Path(sys.prefix).resolve(),
                Path(sys.base_prefix).resolve(), Path(optimize_agy_sandbox.__file__).resolve().parent.parent)))])
        self.assertEqual(settings["permissions"]["deny"],
                         ["write_file(/restricted)", "unsandboxed(*)"])
        self.assertEqual(settings["permissions"]["ask"], ["read_url(example.invalid)"])
        prefix = sandbox.plan["prefix"]
        self.assertIn(["--ro-bind", str(private), str(path)],
                      [prefix[index:index + 3] for index in range(len(prefix))])
        self.assertFalse(any(private.is_relative_to(root) for root in sandbox.writable_roots))
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)

    def test_native_settings_reject_malformed_or_mutated_permissions(self):
        path = self.profile / "settings.json"
        for data in (b"bad", b"[]", b'{"permissions":[]}', b'{"permissions":{"deny":"no"}}',
                     b'{"permissions":{"allow":[1]}}', b" " * (1024 * 1024 + 1)):
            with self.subTest(size=len(data)):
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.prepare()
        path.write_text('{}')
        sandbox = self.prepare()
        path.write_text('{"changed": true}')
        with self.assertRaisesRegex(ValueError, "settings binding changed"):
            sandbox.command(["--print", "fixture"])

    def test_private_settings_cannot_change_after_plan_creation(self):
        sandbox = self.prepare()
        (sandbox.directory / "agy-settings.json").write_text('{}')
        with self.assertRaisesRegex(ValueError, "settings binding changed"):
            sandbox.command(["--print", "fixture"])

    def test_native_rule_cannot_be_injected_through_a_runtime_path(self):
        for path in ("/tmp/with*glob", "/tmp/with?glob", "/tmp/with[glob]", "/tmp/with)paren",
                     "/tmp/line\nbreak", "relative"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "permission rule"):
                optimize_agy_sandbox._readonly_settings(b'{}', (Path(path),))

    def test_cli_log_link_uses_only_the_existing_log_directory_grant(self):
        log = self.profile / "cli.log"
        log.unlink()
        target = self.profile / "log" / "cli-current.log"
        target.write_text("retained log\n")
        log.symlink_to(Path("log") / target.name)
        sandbox = self.prepare()
        self.assertIn(target.parent, sandbox.writable_roots)
        self.assertNotIn(log, sandbox.writable_roots)
        self.assertNotIn(target, sandbox.writable_roots)
        self.assertNotIn(self.profile, sandbox.writable_roots)
        sandbox.command(["--print", "fixture"])

    def test_cli_log_link_cannot_grant_access_outside_the_log_directory(self):
        log = self.profile / "cli.log"
        log.unlink()
        for target in (self.source / "Main.lean", self.profile / "settings.json",
                       self.agy, self.profile / "log", self.profile / "log" / "missing"):
            with self.subTest(target=target):
                log.symlink_to(target)
                try:
                    with self.assertRaises(ValueError):
                        self.prepare()
                finally:
                    log.unlink()

    def test_cli_log_link_retarget_after_preparation_is_rejected(self):
        log = self.profile / "cli.log"
        log.unlink()
        target = self.profile / "log" / "cli-current.log"
        target.write_text("retained log\n")
        log.symlink_to(target)
        sandbox = self.prepare()
        log.unlink()
        log.symlink_to(self.source / "Main.lean")
        with self.assertRaisesRegex(ValueError, "binding changed"):
            sandbox.command(["--print", "fixture"])

    def test_cli_log_target_cannot_escape_through_another_link_or_hardlink(self):
        log = self.profile / "cli.log"
        log.unlink()
        target = self.profile / "log" / "cli-current.log"
        log.symlink_to(target)
        target.symlink_to(self.profile / "settings.json")
        with self.assertRaises(ValueError):
            self.prepare()
        target.unlink()
        os.link(self.profile / "settings.json", target)
        with self.assertRaisesRegex(ValueError, "hardlink"):
            self.prepare()

    def test_cli_log_link_and_target_births_are_rechecked(self):
        log = self.profile / "cli.log"
        log.unlink()
        target = self.profile / "log" / "cli-current.log"
        target.write_text("retained log\n")
        log.symlink_to(target)
        sandbox = self.prepare()
        retained_link = self.profile / "retained-link"
        log.rename(retained_link)
        log.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "binding changed"):
            sandbox.command(["--print", "fixture"])
        log.unlink()
        retained_link.rename(log)
        target.rename(target.with_name("retained-target"))
        target.write_text("replacement log\n")
        with self.assertRaisesRegex(ValueError, "binding changed"):
            sandbox.command(["--print", "fixture"])

    def test_shell_helper_overlay_has_a_private_inode_and_no_bin_write_grant(self):
        sandbox = self.prepare()
        private = sandbox.runner / "agentapi"
        self.assertEqual(private.read_bytes(), self.shell_helper.read_bytes())
        self.assertFalse(private.samefile(self.shell_helper))
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertNotIn(self.shell_helper, sandbox.writable_roots)
        self.assertNotIn(self.shell_helper.parent, sandbox.writable_roots)
        command = sandbox.command(["--print", "fixture"])
        self.assertIn(["--bind", str(private), str(self.shell_helper)],
                      [command[i:i + 3] for i in range(len(command) - 2)])

    def test_shell_helper_alias_hardlink_and_non_script_are_rejected(self):
        self.shell_helper.unlink()
        self.shell_helper.symlink_to(self.source / "Main.lean")
        with self.assertRaises(ValueError):
            self.prepare()
        self.shell_helper.unlink()
        os.link(self.source / "Main.lean", self.shell_helper)
        with self.assertRaises(ValueError):
            self.prepare()
        self.shell_helper.unlink()
        self.shell_helper.write_bytes(b"not a generated shell script")
        with self.assertRaises(ValueError):
            self.prepare()

    def test_shell_helper_host_and_private_inodes_are_rechecked(self):
        sandbox = self.prepare()
        private = sandbox.runner / "agentapi"
        for path in (self.shell_helper, private):
            with self.subTest(path=path):
                retained = path.with_name(path.name + "-retained")
                path.rename(retained)
                path.write_text("#!/bin/sh\nexit 1\n")
                try:
                    with self.assertRaisesRegex(ValueError, "binding changed"):
                        sandbox.command(["--print", "fixture"])
                finally:
                    path.unlink()
                    retained.rename(path)

    def test_missing_shell_helper_is_not_created_on_the_host(self):
        self.shell_helper.unlink()
        sandbox = self.prepare()
        self.assertFalse(self.shell_helper.exists())
        self.assertFalse((sandbox.runner / "agentapi").exists())

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
        with self.assertRaises((OSError, ValueError)):
            self.run_fake(f"from pathlib import Path; Path({str(settings)!r}).write_text('{{}}')")
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

    def test_cli_log_alias_is_writable_but_profile_and_source_remain_readonly(self):
        log = self.profile / "cli.log"
        log.unlink()
        target = self.profile / "log" / "cli-current.log"
        target.write_text("retained log\n")
        log.symlink_to(target)
        self.sandbox = self.prepare(native=True)
        self.sandbox.preflight()
        result = self.run_fake(
            "from pathlib import Path; "
            f"Path({str(log)!r}).write_text('runtime log'); "
            f"assert Path({str(target)!r}).read_text() == 'runtime log'"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for operation in (
            f"Path({str(log)!r}).unlink()",
            f"Path({str(self.profile / 'settings.json')!r}).write_text('changed')",
            f"Path({str(self.source / 'Main.lean')!r}).write_text('changed')",
        ):
            result = self.run_fake("from pathlib import Path; " + operation)
            self.assertNotEqual(result.returncode, 0)
            self.assert_unchanged()

    def test_generated_shell_helper_writes_affect_only_the_private_copy(self):
        original_mode = stat.S_IMODE(self.shell_helper.stat().st_mode)
        result = self.run_fake(
            "from pathlib import Path; import os; "
            f"helper = Path({str(self.shell_helper)!r}); "
            "helper.write_text('#!/bin/sh\\nexit 7\\n'); helper.chmod(0o600); "
            "assert helper.read_text() == '#!/bin/sh\\nexit 7\\n'"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(self.shell_helper.stat().st_mode), original_mode)
        self.assert_unchanged()
        for path in (self.shell_helper.parent / "new-helper", self.source / "Main.lean"):
            result = self.run_fake(f"from pathlib import Path; Path({str(path)!r}).write_text('changed')")
            self.assertNotEqual(result.returncode, 0)
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
