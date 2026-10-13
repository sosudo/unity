"""Offline controller integration: real temporary Git, fake compiler and agents."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from unity import bump_setup, bump_tasks, bump_verification, bump_workflow
from unity.bump_spawn import SpawnCleanupError
from unity.forum import bump_workflow_server as forum


class WorkflowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temp = Path(directory.name).resolve()
        self.root = self.temp / "project"
        self.root.mkdir()
        self.git(self.root, "init", "-q")
        self.git(self.root, "config", "user.name", "Offline Test")
        self.git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / ".gitignore").write_text(".unity\n.lake\n")
        (self.root / "A.lean").write_text("def a := missingA\n")
        (self.root / "B.lean").write_text("def b := missingB\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "baseline")
        self.base = self.git(self.root, "rev-parse", "HEAD")
        self.paths = bump_workflow.paths_for(self.root)
        self.scope = {"selected_modules": {"A": "A.lean", "B": "B.lean"},
                      "default_modules": {"A": "A.lean", "B": "B.lean"}, "excluded_files": {}, "build_dir": ".lake/build"}
        scope_patcher = patch.object(bump_setup, "discover_scope", side_effect=lambda *a, **k: deepcopy(self.scope))
        scope_patcher.start()
        self.addCleanup(scope_patcher.stop)
        bump_tasks.initialize(self.paths.forum, self.root, run_id="test", base_sha=self.base,
                              version="v4.34.1", config={"scope": self.scope})
        self.roster = SimpleNamespace(agents=[SimpleNamespace(name="Alice"), SimpleNamespace(name="Bob")])
        self.roster.primary = self.roster.agents[0]
        self.controller = bump_workflow.Controller(self.paths, self.roster, max_attempts=2)
        self.counter = 0
        self.patcher = patch.object(bump_workflow, "task_worktree", side_effect=self.worktree)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.before = self.build_result([self.error("A.lean"), self.error("B.lean")])
        self.controller.refresh(self.before)

    def git(self, root, *args):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def worktree(self, paths, author, task_id):
        self.counter += 1
        tree = self.temp / ("worker-" + str(self.counter))
        self.git(self.root, "worktree", "add", "-qb", "offline-" + str(self.counter), str(tree), "HEAD")
        (tree / ".unity").symlink_to(self.paths.unity, target_is_directory=True)
        return tree

    def error(self, path):
        return {"id": path, "path": path, "line": 1, "column": 1, "severity": "error",
                "kind": "declaration", "message": "unknown identifier in " + path}

    def build_result(self, diagnostics, checked=()):
        return {"passed": not diagnostics, "completed": True, "returncode": int(bool(diagnostics)),
                "source_stable": True, "diagnostics": diagnostics, "errors": diagnostics,
                "checked_files": list(checked), "unmapped": []}

    def submit(self):
        state = self.controller.state()
        tid = next(tid for tid, task in state["tasks"].items() if task["files"] == ["A.lean"])
        bump_tasks.claim(self.paths.forum, "Alice", tid)
        worker = self.worktree(self.paths, "Alice", tid)
        bump_tasks.configure_assignment(self.paths.forum, "Alice", tid, worker, self.base)
        (worker / "A.lean").write_text("def a := 1\n")
        forum.configure(self.paths.forum, self.root)
        candidate = forum.submit_repair("Alice", tid, "offline fixture repair")
        return tid, candidate, worker

    def active_worker(self, tid, worker, *, cleanup_error=False):
        interrupt = asyncio.Event()
        finished = []
        async def session():
            await interrupt.wait()
            finished.append(True)
            if cleanup_error:
                raise SpawnCleanupError("fixture owned session remained live")
        task = asyncio.create_task(session())
        self.controller.workers["Alice"] = {"task": task, "task_id": tid, "interrupt": interrupt,
                                             "tree": worker, "agent": self.roster.primary}
        return finished

    async def test_useful_partial_candidate_merges_while_sibling_still_fails(self):
        tid, candidate, worker = self.submit()
        finished = self.active_worker(tid, worker)
        sibling = self.build_result([self.error("B.lean")], checked=["A.lean"])
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
                patch.object(bump_verification, "build", return_value=self.build_result([], checked=["A.lean"])), \
                patch.object(self.controller, "build", new=AsyncMock(return_value=sibling)):
            self.assertTrue(await self.controller.integrate_ready())
        self.assertTrue(finished)
        state = self.controller.state()
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual((self.root / "A.lean").read_text(), "def a := 1\n")
        self.assertNotEqual(state["main_sha"], self.base)
        self.assertFalse(state["leases"])
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        self.assertTrue(any(t["files"] == ["B.lean"] and t["status"] == "pending" for t in state["tasks"].values()))

    async def test_rejected_candidate_releases_only_after_worker_closed(self):
        tid, candidate, worker = self.submit()
        finished = self.active_worker(tid, worker)
        def rejected(_candidate):
            self.assertTrue(finished)
            self.assertIn("A.lean", self.controller.state()["leases"])
            return {"accepted": False, "reason": "fixture compiler rejected patch"}
        with patch.object(self.controller, "integrate", side_effect=rejected), \
                patch.object(self.controller, "build", new=AsyncMock(return_value=self.before)):
            await self.controller.integrate_ready()
        state = self.controller.state()
        self.assertFalse(state["leases"])
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "rejected")
        self.assertEqual(state["tasks"][tid]["attempts"], 1)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.base)

    async def test_cleanup_fatal_prevents_candidate_integration(self):
        tid, candidate, worker = self.submit()
        self.active_worker(tid, worker, cleanup_error=True)
        with patch.object(self.controller, "integrate") as integration:
            with self.assertRaises(SpawnCleanupError):
                await self.controller.integrate_ready()
        integration.assert_not_called()
        self.assertIn("A.lean", self.controller.state()["leases"])
        self.assertEqual(self.controller.state()["candidates"][candidate["candidate_id"]]["status"], "submitted")
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.base)

    async def test_restart_journal_reconciles_published_commit_without_reapplying(self):
        tid, candidate, worker = self.submit()
        self.git(self.root, "merge", "--ff-only", candidate["commit_sha"])
        bump_workflow.write_json(self.paths.forum / "integration.json", {
            "candidate_id": candidate["candidate_id"], "before": self.base,
            "after": candidate["commit_sha"], "config": None})
        self.controller.recover_integration()
        state = self.controller.state()
        self.assertEqual(state["main_sha"], candidate["commit_sha"])
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertFalse(state["leases"])
        self.controller.recover_integration()
        self.assertEqual(self.controller.state(), state)

    async def test_restart_journal_rejects_unrelated_head(self):
        _, candidate, _ = self.submit()
        (self.root / "Other").write_text("unrelated")
        self.git(self.root, "add", "Other")
        self.git(self.root, "commit", "-qm", "external change")
        head = self.git(self.root, "rev-parse", "HEAD")
        bump_workflow.write_json(self.paths.forum / "integration.json", {
            "candidate_id": candidate["candidate_id"], "before": self.base,
            "after": candidate["commit_sha"], "config": None})
        with self.assertRaisesRegex(ValueError, "neither side"):
            self.controller.recover_integration()
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), head)

    async def test_exact_snapshot_independent_review_and_unlimited_attempt_setting(self):
        self.controller.max_attempts = float("inf")
        with bump_tasks.transaction(self.paths.forum) as state:
            state["candidates"]["writer-history"] = {"author": "Alice", "status": "merged"}
        identity = {"main_sha": self.base, "scope": self.scope, "files": {}}
        identity["sha256"] = bump_verification.digest(identity)
        build = self.build_result([], checked=["A.lean", "B.lean"])
        build["fresh_project"] = {"fresh": True}
        snapshot = {"snapshot_id": "offline-snapshot", "main_sha": self.base, "base_sha": self.base,
            "config_revision": 0, "passed": True, "scope": self.scope, "source_identity": identity,
            "checked_files": ["A.lean", "B.lean"], "issues": [],
            "builds": [build],
            "hole_audit": {"passed": True, "issues": []}, "patch_replay": {"passed": True}}
        snapshot["sha256"] = bump_verification.digest(snapshot)
        seen = []
        async def critic(agent, task_id, tree, profile, interrupt):
            seen.append((agent.name, profile))
            forum.configure(self.paths.forum, self.root, "critic")
            forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], True, "offline fixture review")
        with patch.object(bump_verification, "verify_final", return_value=deepcopy(snapshot)), \
                patch.object(bump_verification, "source_identity", return_value=snapshot["source_identity"]), \
                patch.object(self.controller, "worker", side_effect=critic):
            self.assertTrue(await self.controller.review())
        self.assertEqual(seen, [("Bob", "critic")])
        state = self.controller.state()
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["review"]["verdict"]["snapshot_sha256"], snapshot["sha256"])
        self.assertTrue(state["final_report"])

    async def test_review_detects_source_change_after_critic(self):
        snapshot = {"snapshot_id": "offline-snapshot", "main_sha": self.base, "config_revision": 0,
                    "passed": True, "source_identity": {"snapshot": "before"}}
        snapshot["sha256"] = bump_verification.digest(snapshot)
        async def critic(agent, *args):
            forum.configure(self.paths.forum, self.root, "critic")
            forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], True, "offline fixture review")
        with patch.object(bump_verification, "verify_final", return_value=deepcopy(snapshot)), \
                patch.object(bump_verification, "source_identity", return_value={"snapshot": "changed"}), \
                patch.object(self.controller, "worker", side_effect=critic):
            with self.assertRaisesRegex(ValueError, "changed during critic"):
                await self.controller.review()
        self.assertNotEqual(self.controller.state()["phase"], "complete")


class CompilerFixtureTests(unittest.IsolatedAsyncioTestCase):
    """Real dependency-free Lake project; workers are deterministic test doubles.

    This is an offline integration test, not a model-backed evaluation. It never
    touches Poly, remote machines, credentials or installed Unity configuration.
    """
    async def test_upgrade_first_real_compiler_repair_and_final_review(self):
        elan_root = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan")))
        installed = elan_root / "toolchains" / "leanprover--lean4---v4.34.1" / "bin" / "lake"
        if not installed.is_file() or not shutil.which("elan"):
            self.skipTest("Lean v4.34.1 is not already installed; offline tests never install it")
        with tempfile.TemporaryDirectory() as temporary:
            root = (Path(temporary).resolve() / "fixture")
            root.mkdir()
            def git(*args):
                result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            git("init", "-q")
            git("config", "user.name", "Offline Test")
            git("config", "user.email", "test@example.invalid")
            (root / ".gitignore").write_text(".unity\n.lake\n")
            (root / "lean-toolchain").write_text("leanprover/lean4:v4.28.0-rc1\n")
            (root / "lakefile.toml").write_text('name = "BumpOfflineFixture"\nversion = "0.1.0"\ndefaultTargets = ["Fixture"]\n\n[[lean_lib]]\nname = "Fixture"\n')
            (root / "Fixture").mkdir()
            (root / "Fixture.lean").write_text("import Fixture.A\nimport Fixture.B\n")
            original = "def fixtureBad : Nat := missingName\n"
            (root / "Fixture" / "A.lean").write_text(original)
            (root / "Fixture" / "B.lean").write_text("theorem inheritedHole : True := by sorry\n")
            git("add", ".")
            git("commit", "-qm", "deliberately broken old-toolchain baseline")
            source = bump_workflow.paths_for(root)
            source.unity.mkdir()
            source.unity_md.write_text("Offline fixture only. No provider calls.\n")
            source.agents_yaml.write_text("agents: []\n")
            roster = SimpleNamespace(agents=[SimpleNamespace(name="Alice"), SimpleNamespace(name="Bob")])
            roster.primary = roster.agents[0]
            target = await asyncio.to_thread(bump_setup.prepare, source, "v4.34.1", {})
            setup = bump_setup.read_setup(target)
            self.assertEqual(setup["phase"], "ready", setup)
            called = []
            async def fixture_worker(controller, agent, task_id, tree, profile, interrupt):
                called.append((agent.name, profile))
                forum.configure(controller.paths.forum, controller.paths.project_root, profile)
                if profile == "critic":
                    snapshot = controller.state()["review"]["snapshot"]
                    return forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], True,
                        "Fixture review: replace missing natural-number term with 1, retain inherited hole.")
                self.assertEqual(profile, "repairing")
                task = controller.state()["tasks"][task_id]
                self.assertIn("Fixture/A.lean", task["files"])
                (tree / "Fixture" / "A.lean").write_text("def fixtureBad : Nat := 1\n")
                return forum.submit_repair(agent.name, task_id, "deterministic offline fixture repair")
            with patch.object(bump_workflow.Controller, "worker", new=fixture_worker), \
                    patch.object(bump_workflow, "spawn", new=AsyncMock(side_effect=AssertionError("real agent dispatch is prohibited"))) as forbidden:
                accepted = await asyncio.wait_for(bump_workflow.run(target, roster, max_attempts=2), timeout=120)
                forbidden.assert_not_called()
            self.assertTrue(accepted)
            state = bump_tasks.load(target.forum)
            self.assertEqual(state["phase"], "complete")
            self.assertFalse(state["leases"])
            self.assertEqual(len(state["chunks"]), 2)
            self.assertTrue(any(c["name"] == "inheritedHole" for c in state["chunks"].values()))
            self.assertEqual((root / "Fixture" / "A.lean").read_text(), original)
            self.assertEqual((root / "lean-toolchain").read_text(), "leanprover/lean4:v4.28.0-rc1\n")
            self.assertEqual((target.project_root / "Fixture" / "B.lean").read_text(), "theorem inheritedHole : True := by sorry\n")
            report = bump_verification.completion_report(state)
            self.assertEqual(report["status"], "accepted")
            self.assertEqual([role for _, role in called], ["repairing", "critic"])
