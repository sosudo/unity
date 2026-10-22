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
import re
import shlex
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
_TRANSCRIPT_BYTES = 2 * 1024 * 1024
_TRANSCRIPT_ROWS = 4096


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _transcript_paths(brain):
    """List only the known transcript location, with a bounded directory scan."""
    paths = set()
    with os.scandir(brain) as entries:
        for count, entry in enumerate(entries, 1):
            _require(count <= _TRANSCRIPT_ROWS, "AGY transcript directory limit exceeded")
            if entry.is_dir(follow_symlinks=False):
                path = Path(entry.path) / ".system_generated/logs/transcript_full.jsonl"
                if path.exists():
                    paths.add(path)
    return paths


def _read_transcript(path):
    """Read exact stable bytes. Never turn missing or partial evidence into a pass."""
    first = path.lstat()
    _require(stat.S_ISREG(first.st_mode), "AGY transcript must be a regular file")
    _require(first.st_size <= _TRANSCRIPT_BYTES, "AGY transcript byte limit exceeded")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        payload = handle.read(_TRANSCRIPT_BYTES + 1)
        final_open = os.fstat(handle.fileno())
    last = path.lstat()
    identities = [(item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
                  for item in (first, opened, final_open, last)]
    _require(all(item == identities[0] for item in identities), "AGY transcript changed during read")
    _require(len(payload) <= _TRANSCRIPT_BYTES, "AGY transcript byte limit exceeded")
    _require(payload.endswith(b"\n"), "AGY transcript has an incomplete final row")
    lines = payload.splitlines()
    _require(0 < len(lines) <= _TRANSCRIPT_ROWS, "AGY transcript row limit exceeded")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "AGY transcript has duplicate JSON keys")
            result[key] = value
        return result

    rows = [json.loads(line, object_pairs_hook=unique_object) for line in lines]
    _require(all(isinstance(row, dict) for row in rows), "AGY transcript row must be an object")
    return payload, rows


def _no_sandbox_bypass(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().replace("_", "") == "bypasssandbox":
                _require(item is False, "AGY requested a sandbox bypass")
            _no_sandbox_bypass(item)
    elif isinstance(value, list):
        for item in value:
            _no_sandbox_bypass(item)


def _denial_command(source):
    program = ("import errno,json\nfrom pathlib import Path\n"
        f"target=Path({str(source / 'Main.lean')!r})\n"
        "try:\n    with target.open('a') as handle:\n        handle.write('UNAUTHORIZED STARTUP TEST WRITE\\n')\n"
        "except OSError as exc:\n    assert exc.errno in (errno.EACCES,errno.EPERM,errno.EROFS)\n"
        "    print(json.dumps({'fixture_source_write_denied':True}))\n"
        "else:\n    raise SystemExit('source write was permitted')\n")
    return shlex.join([sys.executable, "-I", "-c", program])


def _bridge_command(command, source):
    """Accept a direct bridge call, not a pipeline whose exit code hides failure."""
    # JSON evidence can contain literal dollar signs and Markdown backticks.
    # They are data inside single quotes, but can expand in other shell contexts.
    quote, escaped = None, False
    for character in command:
        _require(character not in "\n\r", "AGY bridge command must not use shell expansion")
        if quote == "'":
            if character == "'":
                quote = None
            continue
        _require(character not in "$`", "AGY bridge command must not use shell expansion")
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words = list(lexer)
    _require(len(words) == 9 and words[:7] == ["cd", "--", str(source), "&&", "unity", "mcp", "unity-forum"],
             "AGY bridge command must be direct, without pipes, redirection, or shell wrappers")
    arguments = json.loads(words[8])
    _require(isinstance(arguments, dict), "AGY bridge arguments must be an object")
    return words[7], arguments


def _audit_transcript(rows, *, full_prompt, source, project, request_id, subject, denial_command):
    """Audit actual native tool calls; the model's final answer is not evidence."""
    _require([row.get("step_index") for row in rows] == list(range(len(rows))),
             "AGY transcript has missing, duplicate, or unordered steps")
    inputs = [row for row in rows if row.get("type") == "USER_INPUT"]
    _require(len(inputs) == 1 and inputs[0].get("source") == "USER_EXPLICIT"
             and full_prompt in inputs[0].get("content", ""), "AGY transcript does not bind this exact test turn")
    seen, pending, artifacts_seen = set(), None, set()
    for row in rows:
        if "tool_calls" in row:
            calls = row["tool_calls"]
            _require(isinstance(calls, list), "AGY tool call evidence is malformed")
            for call in calls:
                _require(isinstance(call, dict) and isinstance(call.get("args"), dict),
                         "AGY tool call arguments are missing or malformed")
                _no_sandbox_bypass(call)
            _require(not calls or pending is None,
                     "AGY started another tool without a completed tool result")
            commands = [call for call in calls if call.get("name") == "run_command"]
            _require(len(commands) <= 1, "AGY test commands must be serial for exact result attribution")
            if commands:
                _require(len(calls) == 1,
                         "AGY command must be the only tool call for exact result attribution")
                command = commands[0]["args"].get("CommandLine")
                _require(isinstance(command, str), "AGY command text is missing")
                if command == denial_command:
                    pending = ("source_write_denial", {})
                elif "unity-forum" in command:
                    pending = _bridge_command(command, source)
                else:
                    raise AssertionError("AGY test used an unrequested shell command")
            elif calls:
                _require(len(calls) == 1 and calls[0].get("name") == "read_file",
                         "AGY test used an unrequested or parallel native tool")
                _require(calls[0]["args"].get("Path") == str(source / "Main.lean"),
                         "AGY source read does not bind this test source")
                pending = ("native_read_file", {})
        if row.get("type") != "GENERIC" or pending is None:
            continue
        content = row.get("content", "")
        _require(isinstance(content, str), "AGY command result is malformed")
        if pending[0] == "native_read_file":
            _require(row.get("status") == "DONE" and
                     "Showing lines 1-1 of 1\n1: theorem fixture : True := by trivial" in content,
                     "AGY native source read did not complete successfully")
            pending = None
            continue
        match = re.search(r"(?:^|\n)Command exited with code (-?\d+)\.\n", content)
        _require(match is not None, "AGY command result is incomplete or asynchronous; attribution is unverified")
        _require(row.get("status") == "DONE" and match.group(1) == "0", "AGY normal sandbox command failed")
        _require("Command output:\n" in content, "AGY command output is missing")
        output = json.loads(content.split("Command output:\n", 1)[1].strip())
        _require(isinstance(output, dict), "AGY bridge result must be a JSON object")
        tool, arguments = pending
        if tool == "source_write_denial":
            _require(output == {"fixture_source_write_denied": True}, "source write denial was not proved")
        elif tool == "optimize_brief":
            _require(arguments == {} and output.get("root") == str(project)
                     and output.get("scoring_request_id") == request_id, "brief does not bind this test request")
        elif tool == "scoring_request":
            _require(arguments == {} and output.get("root") == str(source)
                     and output.get("request_id") == request_id
                     and output.get("subjects") == [subject], "scoring request does not bind this test source")
        elif tool == "artifact_read":
            _require(arguments.get("artifact_id") in artifacts_seen
                     and output.get("artifact_id") == arguments["artifact_id"]
                     and output.get("offset") == 0 and output.get("next_offset") is None,
                     "artifact read did not retrieve the complete bound scoring request")
            artifact = json.loads(output["content"])
            _require(artifact.get("request_id") == request_id and artifact.get("root") == str(source)
                     and artifact.get("subjects") == [subject], "artifact does not bind this scoring request")
        elif tool == "submit_score_report":
            _require({"optimize_brief", "scoring_request", "artifact_read", "source_write_denial"} <= seen,
                     "AGY submitted a score before the required normal-path checks")
            _require(arguments.get("request_id") == request_id and arguments.get("subject") == subject
                     and output.get("reported") is True and output.get("request_id") == request_id
                     and output.get("subject") == subject, "score submission does not bind this test request")
        else:
            raise AssertionError("AGY test used an unrequested bridge tool")
        if tool in {"optimize_brief", "scoring_request"}:
            records = output.get("records", {})
            key = "scoring_request" if tool == "optimize_brief" else "request"
            artifact_id = records.get(key, {}).get("artifact_id")
            _require(isinstance(artifact_id, str) and artifact_id, "bound scoring artifact is missing")
            artifacts_seen.add(artifact_id)
        seen.add(tool)
        pending = None
    _require(pending is None, "AGY command has no completed result")
    _require({"optimize_brief", "scoring_request", "artifact_read", "submit_score_report", "source_write_denial"} <= seen,
             "AGY transcript lacks required successful normal sandbox calls")
    _require(rows[-1].get("type") == "PLANNER_RESPONSE" and rows[-1].get("status") == "DONE"
             and rows[-1].get("content") and not rows[-1].get("tool_calls"),
             "AGY transcript lacks its completed final response")
    return sorted(seen)


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
            assert '--dangerously-skip-permissions' not in sys.argv
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

            brief = call('optimize_brief', {})
            assert brief['scoring_request_id'] == os.environ['UNITY_OPTIMIZE_SCORE_REQUEST_ID']
            request = call('scoring_request', {})
            assert request['request_id'] == os.environ['UNITY_OPTIMIZE_SCORE_REQUEST_ID']
            assert request['root'] == str(source)
            artifact = call('artifact_read', {'artifact_id': request['records']['request']['artifact_id']})
            assert json.loads(artifact['content'])['request_id'] == request['request_id']
            assert artifact['next_offset'] is None
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


class OptimizeAgyTranscriptAuditTests(unittest.TestCase):
    """Offline negative cases for the real-test oracle; never starts AGY."""

    def setUp(self):
        self.source = Path("/fixture/source with spaces")
        self.project = Path("/fixture/project")
        self.bound = {"full_prompt": "exact unique fixture prompt", "source": self.source,
            "project": self.project, "request_id": "score-request-fixture", "subject": "fixture-subject",
            "denial_command": _denial_command(self.source)}
        request = {"request_id": self.bound["request_id"], "root": str(self.source),
                   "subjects": [self.bound["subject"]]}
        artifact = {"artifact_id": "artifact-fixture", "offset": 0, "next_offset": None,
                    "content": json.dumps(request)}
        record = {"artifact_id": "artifact-fixture"}
        self.rows = [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                      "content": "<USER_REQUEST>" + self.bound["full_prompt"] + "</USER_REQUEST>"}]
        self.add_call(self.bound["denial_command"], {"fixture_source_write_denied": True})
        self.add_bridge("optimize_brief", {}, {"root": str(self.project),
            "scoring_request_id": self.bound["request_id"], "records": {"scoring_request": record}})
        self.add_bridge("scoring_request", {}, {**request, "records": {"request": record}})
        self.add_bridge("artifact_read", {"artifact_id": "artifact-fixture"}, artifact)
        self.add_bridge("submit_score_report", {"request_id": self.bound["request_id"],
            "subject": self.bound["subject"], "raw_value": 1},
            {"request_id": self.bound["request_id"], "subject": self.bound["subject"], "reported": True})
        self.rows.append({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "Fixture report submitted."})
        self.renumber()

    def add_call(self, command, output):
        self.rows.extend([{"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [
            {"name": "run_command", "args": {"CommandLine": command, "WaitMsBeforeAsync": 10000}}]},
            {"type": "GENERIC", "status": "DONE", "content":
                "Created At: 2026-10-21T19:34:58-04:00\nCompleted At: 2026-10-21T19:34:59-04:00\n"
                "Command exited with code 0.\n\nCommand output:\n" + json.dumps(output) + "\r\n"}])

    def add_bridge(self, tool, arguments, output):
        command = "cd -- " + shlex.quote(str(self.source)) + " && " + shlex.join(
            ["unity", "mcp", "unity-forum", tool, json.dumps(arguments)])
        self.add_call(command, output)

    def renumber(self):
        for index, row in enumerate(self.rows):
            row["step_index"] = index

    def audit(self):
        return _audit_transcript(self.rows, **self.bound)

    def test_accepts_complete_normal_path_and_source_write_denial(self):
        self.assertEqual(self.audit(), ["artifact_read", "optimize_brief", "scoring_request",
                                       "source_write_denial", "submit_score_report"])

    def test_rejects_bypass_even_if_score_report_was_successful(self):
        self.rows[9]["tool_calls"][0]["args"]["BypassSandbox"] = True
        with self.assertRaisesRegex(AssertionError, "sandbox bypass"):
            self.audit()

    def test_rejects_bypass_on_any_tool_even_after_success(self):
        self.rows.append({"type": "PLANNER_RESPONSE", "tool_calls": [
            {"name": "other_tool", "args": {"nested": {"BypassSandbox": True}}}]})
        self.renumber()
        with self.assertRaisesRegex(AssertionError, "sandbox bypass"):
            self.audit()

    def test_rejects_malformed_bypass_values(self):
        for value in ("false", "true", 0, 1, None):
            with self.subTest(value=value):
                self.rows[1]["tool_calls"][0]["args"]["BypassSandbox"] = value
                with self.assertRaisesRegex(AssertionError, "sandbox bypass"):
                    self.audit()

    def test_rejects_previous_pipeline_masked_artifact_failure(self):
        self.rows[5]["tool_calls"][0]["args"]["CommandLine"] += " 2>&1 | tail -40"
        self.rows[6]["content"] = "Command exited with code 0.\n\nCommand output:\nTraceback: Read-only file system"
        with self.assertRaisesRegex(AssertionError, "without pipes"):
            self.audit()

    def test_rejects_shell_expansion_in_bridge_command(self):
        args = self.rows[9]["tool_calls"][0]["args"]
        original = args["CommandLine"]
        for suffix in (' $(true)', ' `true`', ' "$HOME"', ' "`true`"', ' "$(true)"'):
            with self.subTest(suffix=suffix):
                args["CommandLine"] = original + suffix
                with self.assertRaisesRegex(AssertionError, "shell expansion"):
                    self.audit()

    def test_accepts_single_quoted_literal_evidence_without_shell_expansion(self):
        arguments = {"request_id": self.bound["request_id"], "subject": self.bound["subject"],
                     "raw_value": 1, "evidence": "The proof's `trivial` tactic; $x and $(true) are data."}
        self.rows[9]["tool_calls"][0]["args"]["CommandLine"] = (
            "cd -- " + shlex.quote(str(self.source)) + " && " + shlex.join(
                ["unity", "mcp", "unity-forum", "submit_score_report", json.dumps(arguments)]))
        self.assertIn("submit_score_report", self.audit())

    def test_rejects_failed_normal_command_before_later_success(self):
        self.rows[4]["content"] = self.rows[4]["content"].replace("code 0", "code 1")
        with self.assertRaisesRegex(AssertionError, "normal sandbox command failed"):
            self.audit()

    def test_rejects_missing_or_async_result_attribution(self):
        self.rows[4]["content"] = "Command is running in background."
        with self.assertRaisesRegex(AssertionError, "incomplete or asynchronous"):
            self.audit()

    def test_rejects_missing_required_call_despite_successful_report(self):
        del self.rows[7:9]
        self.renumber()
        with self.assertRaisesRegex(AssertionError, "before the required"):
            self.audit()

    def test_rejects_missing_final_response(self):
        self.rows.pop()
        with self.assertRaisesRegex(AssertionError, "completed final response"):
            self.audit()

    def test_rejects_parallel_bridge_calls_with_ambiguous_results(self):
        self.rows[3]["tool_calls"].append(self.rows[5]["tool_calls"][0])
        with self.assertRaisesRegex(AssertionError, "serial"):
            self.audit()

    def test_rejects_mixed_tool_output_that_masks_failed_command(self):
        self.rows[5]["tool_calls"].insert(0, {
            "name": "read_file", "args": {"AbsolutePath": "/fixture/prior-output.txt"}})
        self.rows.insert(7, {"type": "GENERIC", "status": "DONE", "content":
            "Command exited with code 1.\n\nCommand output:\nrequired scoring_request failed"})
        self.renumber()
        with self.assertRaisesRegex(AssertionError, "only tool call"):
            self.audit()

    def test_rejects_new_noncommand_tool_before_command_result(self):
        self.rows.insert(6, {"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [
            {"name": "read_file", "args": {"AbsolutePath": "/fixture/prior-output.txt"}}]})
        self.renumber()
        with self.assertRaisesRegex(AssertionError, "another tool"):
            self.audit()

    def test_rejects_command_before_prior_native_read_result(self):
        self.rows.insert(5, {"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [
            {"name": "read_file", "args": {"Path": str(self.source / "Main.lean")}}]})
        self.renumber()
        with self.assertRaisesRegex(AssertionError, "another tool"):
            self.audit()

    def test_accepts_completed_native_read_before_next_command(self):
        self.rows[5:5] = [
            {"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [
                {"name": "read_file", "args": {"Path": str(self.source / "Main.lean")}}]},
            {"type": "GENERIC", "status": "DONE", "content":
                "Showing lines 1-1 of 1\n1: theorem fixture : True := by trivial"}]
        self.renumber()
        self.assertIn("submit_score_report", self.audit())

    def test_rejects_missing_malformed_or_wrong_turn_evidence(self):
        changes = [(0, "content", "unrelated test"), (1, "tool_calls", [{}]),
                   (1, "step_index", 123), (4, "content", "Command exited with code 0.\n")]
        for index, key, value in changes:
            with self.subTest(key=key, value=value):
                old = self.rows[index][key]
                self.rows[index][key] = value
                with self.assertRaises((AssertionError, ValueError)):
                    self.audit()
                self.rows[index][key] = old

    def test_rejects_unbound_artifact(self):
        self.rows[8]["content"] = self.rows[8]["content"].replace("artifact-fixture", "artifact-other")
        with self.assertRaisesRegex(AssertionError, "bound scoring request"):
            self.audit()

    def test_bounded_reader_rejects_partial_oversized_or_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript_full.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in self.rows) + "\n")
            payload, rows = _read_transcript(path)
            self.assertTrue(payload)
            self.assertEqual(_audit_transcript(rows, **self.bound), self.audit())
            path.write_text('{}')
            with self.assertRaisesRegex(AssertionError, "incomplete final row"):
                _read_transcript(path)
            path.write_text('{"BypassSandbox":true,"BypassSandbox":false}\n')
            with self.assertRaisesRegex(AssertionError, "duplicate JSON keys"):
                _read_transcript(path)
            path.write_bytes(b"x" * (_TRANSCRIPT_BYTES + 1))
            with self.assertRaisesRegex(AssertionError, "byte limit"):
                _read_transcript(path)


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
        old_transcripts = _transcript_paths(profile / "brain")
        bound = {}
        prepare = optimize_spawn._antigravity_options

        def capture_options(agent, agy, cwd, system_prompt, prompt, env_overrides,
                            own_process_group, mcp_profile, **kwargs):
            self.assertFalse(bound, "this startup test requires exactly one AGY turn")
            source = Path(cwd).resolve()
            denial = _denial_command(source)
            instructions = (
                "\n\nNATIVE STARTUP TEST REQUIREMENTS (this disposable fixture only):\n"
                "Do not request BypassSandbox, change settings, or use a shell outside the sandbox.\n"
                "Run exactly one native run_command call at a time. Wait for its completed output.\n"
                "Use WaitMsBeforeAsync=10000. Do not use pipes, redirection, wrappers, or parallel calls.\n"
                "First run this exact harmless source-write-denial test command:\n" + denial + "\n"
                "Then use direct commands in this form: cd -- " + shlex.quote(str(source)) +
                " && unity mcp unity-forum TOOL 'JSON_ARGUMENTS'.\n"
                "Call optimize_brief with {}, then scoring_request with {}.\n"
                "Call artifact_read with the returned records.request.artifact_id to read the complete request.\n"
                "Use native read_file with an absolute path to read Main.lean.\n"
                "Then call submit_score_report for the requested subject, with author=Fixture, raw_value=1, the exact "
                "original method, and evidence from Main.lean. Do not run any other shell command.\n"
                "If any required command fails, stop and report the failure. Do not bypass a sandbox.\n")
            options = prepare(agent, agy, cwd, system_prompt, prompt + instructions, env_overrides,
                              own_process_group, mcp_profile, **kwargs)
            self.assertIn("--sandbox", options["cmd"], "the real test must retain AGY's terminal sandbox")
            self.assertNotIn("--dangerously-skip-permissions", options["cmd"],
                             "the real test must not auto-approve sandbox bypass")
            bound.update(full_prompt=options["full"], source=source, project=self.project,
                         request_id=env_overrides["UNITY_OPTIMIZE_SCORE_REQUEST_ID"],
                         subject=self.subject, denial_command=denial)
            return options

        with patch.dict(os.environ, env, clear=True), patch.object(
                optimize_spawn, "_antigravity_options", side_effect=capture_options):
            await asyncio.wait_for(self.controller(model).judge_snapshot(self.request), 180)
        self.assert_closed(before)
        self.assertTrue(bound, "the real test must capture its actual AGY turn")
        matches = []
        for path in sorted(_transcript_paths(profile / "brain") - old_transcripts):
            payload, rows = _read_transcript(path)
            if any(row.get("type") == "USER_INPUT" and bound["full_prompt"] in row.get("content", "")
                   for row in rows):
                matches.append((path, payload, rows))
        self.assertEqual(len(matches), 1, "exactly one new native transcript must bind this exact test turn")
        path, payload, rows = matches[0]
        checks = _audit_transcript(rows, **bound)
        (self.base / "native-transcript-audit.json").write_text(json.dumps({
            "transcript_path": str(path), "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "request_id": bound["request_id"],
            "no_sandbox_bypass": True, "successful_normal_calls": checks}, indent=2) + "\n")
        self._safe_to_remove = True  # Real-AGY evidence is retained even on success.


if __name__ == "__main__":
    unittest.main()
