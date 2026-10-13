"""Offline recovery/order regressions: temporary Git, mocked compiler and agents."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from unity import bump_setup, bump_tasks, bump_verification, bump_workflow


class WorkflowRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bump-workflow-recovery-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name).resolve()
        self.root = self.temp / "project"
        self.root.mkdir()
        self.git(self.root, "init", "-q")
        self.git(self.root, "config", "user.name", "Offline Test")
        self.git(self.root, "config", "user.email", "offline@example.invalid")
        for name, text in {".gitignore": ".unity\n.lake\n", "A.lean": "def a := missingA\n",
                           "B.lean": "def b := missingB\n", "Extra.lean": "def extra := 2\n",
                           "lean-toolchain": "leanprover/lean4:v4.34.1\n",
                           "lakefile.toml": 'name = "Offline"\n',
                           "lake-manifest.json": '{"packages": []}\n'}.items():
            (self.root / name).write_text(text)
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "baseline")
        self.base = self.git(self.root, "rev-parse", "HEAD")
        self.paths = bump_workflow.paths_for(self.root)
        self.scope = {"selected_modules": {"A": "A.lean", "B": "B.lean"},
                      "default_modules": {"A": "A.lean", "B": "B.lean"},
                      "excluded_files": {"Extra.lean": "outside selected current build scope"},
                      "build_dir": ".lake/build"}
        self.setup = {"phase": "ready", "scope": deepcopy(self.scope), "modules": [],
                      "lake_args": [], "project_scope": "build", "dependencies": {}, "pending_tasks": []}
        bump_tasks.initialize(self.paths.forum, self.root, run_id="offline-recovery", base_sha=self.base,
                              version="v4.34.1", config={"scope": self.scope, "modules": [],
                                                         "lake_args": [], "project_scope": "build"})
        with bump_tasks.transaction(self.paths.forum) as state:
            state["setup"] = deepcopy(self.setup)
        (self.paths.unity / "bump").mkdir(parents=True, exist_ok=True)
        bump_workflow.write_json(self.paths.unity / "bump" / "setup.json", self.setup)
        roster = SimpleNamespace(agents=[SimpleNamespace(name="Alice"), SimpleNamespace(name="Critic")])
        roster.primary = roster.agents[0]
        self.controller = bump_workflow.Controller(self.paths, roster)
        self.count = 0
        self.worktrees = patch.object(bump_workflow, "task_worktree", side_effect=self.worktree)
        self.worktrees.start()
        self.addCleanup(self.worktrees.stop)
        self.discovery = patch.object(bump_setup, "discover_scope", return_value=deepcopy(self.scope))
        self.discover = self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def git(self, root, *args):
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def worktree(self, paths, author, task_id):
        self.count += 1
        tree = self.temp / ("worktree-" + str(self.count))
        self.git(self.root, "worktree", "add", "-qb", "offline-" + str(self.count), str(tree), "HEAD")
        (tree / ".unity").symlink_to(self.paths.unity, target_is_directory=True)
        return tree

    def submit(self, name="A", *, author="Alice", kind="repair", text=None):
        tid, path = "repair-" + name, name + ".lean"
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid] = bump_tasks._task(tid, kind, [], [path])
        assignment = bump_tasks.claim(self.paths.forum, author, tid)
        tree = self.worktree(self.paths, author, tid)
        bump_tasks.configure_assignment(self.paths.forum, author, tid, tree, self.base)
        (tree / path).write_text(text or ("def " + name.lower() + " := 1\n"))
        self.git(tree, "add", path)
        self.git(tree, "commit", "-qm", "offline candidate")
        candidate = bump_tasks.submit_candidate(self.paths.forum, author, tid,
            commit_sha=self.git(tree, "rev-parse", "HEAD"), base_main_sha=self.base,
            task_revision=assignment["revision"], config_revision=assignment["config_revision"], files=[path])
        return tid, candidate

    def publish_journal(self, candidate, setup):
        self.git(self.root, "merge", "--ff-only", candidate["commit_sha"])
        bump_workflow.write_json(self.paths.forum / "integration.json", {
            "candidate_id": candidate["candidate_id"], "before": self.base,
            "after": candidate["commit_sha"], "config": deepcopy(setup)})

    def successful_build(self):
        return {"passed": True, "completed": True, "returncode": 0, "source_stable": True,
                "diagnostics": [], "errors": [], "unmapped": [], "checked_files": ["A.lean", "B.lean", "Extra.lean"]}

    async def test_configuration_integration_prepares_before_restarting_build(self):
        _, candidate = self.submit(kind="configuration")
        events = []
        def integrate(row):
            self.assertEqual(row["candidate_id"], candidate["candidate_id"])
            events.append("integrate")
            return {"accepted": True}
        async def prepare(): events.append("prepare")
        async def build():
            self.assertIn("prepare", events)
            events.append("build")
            return self.successful_build()
        def refresh(result, *, authoritative_full_build=False):
            self.assertTrue(authoritative_full_build)
            events.append("refresh")
        with patch.object(self.controller, "integrate", side_effect=integrate), \
             patch.object(self.controller, "reap_workers", new=AsyncMock()), \
             patch.object(self.controller, "prepare", side_effect=prepare), \
             patch.object(self.controller, "build", side_effect=build), \
             patch.object(self.controller, "refresh", side_effect=refresh):
            self.assertTrue(await self.controller.integrate_ready(reconcile=True))
        self.assertEqual(events, ["integrate", "prepare", "build", "refresh"])

    async def test_setup_loop_configuration_integration_does_not_recursively_prepare(self):
        self.submit(kind="configuration")
        with patch.object(self.controller, "integrate", return_value={"accepted": True}), \
             patch.object(self.controller, "reap_workers", new=AsyncMock()), \
             patch.object(self.controller, "prepare", new=AsyncMock()) as prepare, \
             patch.object(self.controller, "build", new=AsyncMock()) as build:
            self.assertTrue(await self.controller.integrate_ready(reconcile=False))
        prepare.assert_not_awaited()
        build.assert_not_awaited()

    async def test_ordinary_unchanged_scope_integration_needs_no_preparation(self):
        self.submit()
        with patch.object(self.controller, "integrate", return_value={"accepted": True}), \
             patch.object(self.controller, "reap_workers", new=AsyncMock()), \
             patch.object(self.controller, "prepare", new=AsyncMock()) as prepare, \
             patch.object(self.controller, "build", new=AsyncMock(return_value=self.successful_build())) as build, \
             patch.object(self.controller, "refresh", new=Mock()):
            self.assertTrue(await self.controller.integrate_ready())
        prepare.assert_not_awaited()
        build.assert_awaited_once()

    async def test_journal_publishes_setup_before_marking_complete(self):
        _, candidate = self.submit()
        updated = {**deepcopy(self.setup), "lake_args": ["-Kenv=dev"]}
        self.publish_journal(candidate, updated)
        writes, actual_write = [], bump_workflow.write_json
        def record(path, value):
            writes.append(Path(path))
            if Path(path) == self.paths.forum / "integration.json" and value.get("status") == "complete":
                self.assertEqual(json.loads((self.paths.unity / "bump" / "setup.json").read_text()), updated)
            return actual_write(path, value)
        with patch.object(bump_workflow, "write_json", side_effect=record):
            self.controller.recover_integration()
        self.assertLess(writes.index(self.paths.unity / "bump" / "setup.json"),
                        writes.index(self.paths.forum / "integration.json"))
        state = self.controller.state()
        self.assertEqual(state["config"]["revision"], 1)
        self.assertEqual(state["config"]["lake_args"], ["-Kenv=dev"])
        self.assertFalse(state["leases"])

    async def test_journal_restores_setup_when_state_commit_already_happened(self):
        tid, candidate = self.submit()
        updated = {**deepcopy(self.setup), "lake_args": ["-Kenv=dev"]}
        self.publish_journal(candidate, updated)
        with bump_tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = candidate["commit_sha"]
            state["config"].update(revision=1, lake_args=updated["lake_args"])
            state["setup"] = updated
            state["candidates"][candidate["candidate_id"]]["status"] = "merged"
            state["tasks"][tid]["status"] = "complete"
        self.controller.recover_integration()
        self.assertEqual(json.loads((self.paths.unity / "bump" / "setup.json").read_text()), updated)
        recovered = self.controller.state()
        self.assertEqual(recovered["config"]["revision"], 1)
        self.assertFalse(recovered["leases"])
        self.controller.recover_integration()
        self.assertEqual(self.controller.state(), recovered)

    async def test_setup_write_failure_keeps_journal_recoverable_without_double_revision(self):
        _, candidate = self.submit()
        updated = {**deepcopy(self.setup), "lake_args": ["-Kenv=dev"]}
        self.publish_journal(candidate, updated)
        actual_write = bump_workflow.write_json
        def fail_setup(path, value):
            if Path(path) == self.paths.unity / "bump" / "setup.json":
                raise OSError("offline simulated receipt write failure")
            return actual_write(path, value)
        with patch.object(bump_workflow, "write_json", side_effect=fail_setup):
            with self.assertRaisesRegex(OSError, "simulated receipt"):
                self.controller.recover_integration()
        self.assertNotEqual(json.loads((self.paths.forum / "integration.json").read_text()).get("status"), "complete")
        self.controller.recover_integration()
        self.assertEqual(self.controller.state()["config"]["revision"], 1)
        self.assertEqual(json.loads((self.paths.unity / "bump" / "setup.json").read_text()), updated)

    async def test_ordinary_import_repair_refreshes_scope_and_invalidates_old_candidates(self):
        _, candidate = self.submit(text="import Extra\ndef a := extra\n")
        _, stale = self.submit("B", author="Charlie")
        expanded = deepcopy(self.scope)
        expanded["selected_modules"]["Extra"] = "Extra.lean"
        expanded["excluded_files"] = {}
        self.discover.return_value = expanded
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
             patch.object(bump_verification, "build", return_value=self.successful_build()):
            result = self.controller.integrate(candidate)
        self.assertTrue(result["accepted"], result)
        self.assertTrue(self.discover.called)
        self.assertNotEqual(Path(self.discover.call_args.args[0]).resolve(), self.root)
        state = self.controller.state()
        self.assertEqual(state["config"]["revision"], 1)
        self.assertEqual(state["config"]["scope"], expanded)
        self.assertEqual(json.loads((self.paths.unity / "bump" / "setup.json").read_text())["scope"], expanded)
        head = self.git(self.root, "rev-parse", "HEAD")
        rejected = self.controller.integrate(stale)
        self.assertFalse(rejected["accepted"])
        self.assertIn("Configuration changed", rejected["reason"])
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), head)

    async def test_ordinary_repair_with_same_scope_keeps_configuration_revision(self):
        _, candidate = self.submit()
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
             patch.object(bump_verification, "build", return_value=self.successful_build()):
            self.assertTrue(self.controller.integrate(candidate)["accepted"])
        self.assertEqual(self.controller.state()["config"]["revision"], 0)

    async def test_path_bearing_final_audit_failure_returns_to_repair(self):
        issue = {"kind": "introduced_sorry", "path": "A.lean", "line": 1, "column": 1}
        snapshot = {"snapshot_id": "offline-failed-audit", "main_sha": self.base, "base_sha": self.base,
                    "passed": False, "config_revision": 0, "scope": deepcopy(self.scope),
                    "diagnostics": [], "issues": [issue], "hole_audit": {"passed": False, "issues": [issue]}}
        with patch.object(bump_verification, "verify_final", return_value=snapshot), \
             patch.object(self.controller, "worker", new=AsyncMock(side_effect=AssertionError("no critic for failed audit"))) as worker:
            self.assertFalse(await self.controller.review())
        worker.assert_not_awaited()
        state = self.controller.state()
        self.assertNotEqual(state["phase"], "complete")
        self.assertTrue(any("A.lean" in row["files"] and row["status"] == "pending"
                            for row in state["tasks"].values()))

    async def test_unrelated_existing_audit_issue_allows_partial_merge_but_not_final_acceptance(self):
        _, candidate = self.submit()
        issue = {"kind": "source_audit_parse_error", "path": "B.lean",
                 "reason": "source audit found unterminated comment, string or quoted identifier"}
        with patch.object(bump_verification, "audit_sources", return_value={"passed": False, "issues": [issue]}) as audit, \
             patch.object(bump_verification, "build", return_value=self.successful_build()):
            merged = self.controller.integrate(candidate)
        self.assertTrue(merged["accepted"], merged)
        self.assertEqual(audit.call_count, 2)
        self.assertNotEqual(Path(audit.call_args_list[0].args[0]).resolve(), self.root)
        self.assertEqual(Path(audit.call_args_list[1].args[0]).resolve(), self.root)
        state = self.controller.state()
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual((self.root / "A.lean").read_text(), "def a := 1\n")
        snapshot = {"snapshot_id": "offline-existing-audit", "main_sha": state["main_sha"],
                    "base_sha": self.base, "passed": False, "config_revision": state["config"]["revision"],
                    "scope": deepcopy(self.scope), "diagnostics": [], "issues": [issue],
                    "hole_audit": {"passed": False, "issues": [issue]}}
        with patch.object(bump_verification, "verify_final", return_value=snapshot), \
             patch.object(self.controller, "worker", new=AsyncMock()) as worker:
            self.assertFalse(await self.controller.review())
        worker.assert_not_awaited()
        state = self.controller.state()
        self.assertNotEqual(state["phase"], "complete")
        self.assertTrue(any("B.lean" in row["files"] and row["status"] == "pending"
                            for row in state["tasks"].values()))

    async def test_new_audit_issue_rejects_patch_despite_existing_unrelated_issue(self):
        _, candidate = self.submit()
        old = {"kind": "source_audit_parse_error", "path": "B.lean", "reason": "pre-existing malformed source"}
        new = {"kind": "introduced_sorry", "path": "A.lean", "line": 1, "column": 12}
        def audit(root, *args):
            return {"passed": False, "issues": [old] if Path(root).resolve() == self.root else [old, new]}
        with patch.object(bump_verification, "audit_sources", side_effect=audit), \
             patch.object(bump_verification, "build", side_effect=AssertionError("audit rejection must precede build")) as build:
            result = self.controller.integrate(candidate)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["introduced_issues"], [new])
        build.assert_not_called()
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.controller.state()["main_sha"], self.base)

    async def test_warning_only_final_diagnostics_do_not_hide_pending_audit_repair(self):
        issue = {"kind": "introduced_sorry", "path": "A.lean", "line": 1, "column": 1}
        warning = {"id": "offline-warning", "path": "B.lean", "line": 1, "column": 1,
                   "severity": "warning", "kind": "declaration", "message": "unused variable"}
        snapshot = {"snapshot_id": "offline-warning-audit", "main_sha": self.base, "base_sha": self.base,
                    "passed": False, "config_revision": 0, "scope": deepcopy(self.scope),
                    "diagnostics": [warning], "checked_files": ["A.lean", "B.lean"],
                    "issues": [issue], "hole_audit": {"passed": False, "issues": [issue]}}
        with patch.object(bump_verification, "verify_final", return_value=snapshot), \
             patch.object(self.controller, "refresh", side_effect=AssertionError("warning-only refresh would erase audit repair")) as refresh, \
             patch.object(self.controller, "worker", new=AsyncMock()) as worker:
            self.assertFalse(await self.controller.review())
        refresh.assert_not_called()
        worker.assert_not_awaited()
        state = self.controller.state()
        self.assertTrue(any("A.lean" in row["files"] and row["status"] == "pending"
                            for row in state["tasks"].values()))
        self.assertNotEqual(state["phase"], "complete")


if __name__ == "__main__":
    unittest.main()
