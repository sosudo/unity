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
        return {"passed": not diagnostics, "completed": not diagnostics, "returncode": int(bool(diagnostics)),
                "source_stable": True, "diagnostics": diagnostics, "errors": diagnostics,
                "checked_files": list(checked), "unmapped": []}

    def exhausted_configuration_task(self):
        error = {"path": None, "line": None, "severity": "error", "kind": "environment",
                 "message": "error: A.lean: bad import 'Old.Module'"}
        self.controller.refresh(self.build_result([error]))
        state = self.controller.state()
        tid = next(tid for tid, task in state["tasks"].items() if task["kind"] == "configuration")
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid].update(attempts=5, files=["lean-toolchain", "lakefile.lean", "lake-manifest.json"])
        return tid

    def authoritative_build_result(self, diagnostics=None, checked=()):
        state = self.controller.state()
        if diagnostics is None:
            diagnostics = [self.error("A.lean"), self.error("B.lean")]
        result = self.build_result(diagnostics, checked=checked)
        result.update(config_revision=state["config"]["revision"],
                      command=["lake", *state["config"].get("lake_args", ()), "--rehash", "build"],
                      source_identity={"main_sha": self.git(self.root, "rev-parse", "HEAD"),
                                       "scope": deepcopy(state["config"]["scope"]),
                                       "lake_args": list(state["config"].get("lake_args", ())),
                                       "files": bump_verification._files(self.root, self.scope["build_dir"])})
        result["source_identity"]["sha256"] = bump_verification.digest(result["source_identity"])
        return result

    def same_file_declaration_tasks(self, *, import_b=False):
        """A helper failure also breaks its wrapper, before unrelated later work."""
        prefix = "import B\n" if import_b else ""
        (self.root / "A.lean").write_text(
            prefix + "def a := missingA\ndef wrapper := a\ndef later := missingLater\n")
        self.git(self.root, "add", "A.lean")
        self.git(self.root, "commit", "-qm", "same-file declaration regression fixture")
        self.base = self.git(self.root, "rev-parse", "HEAD")
        with bump_tasks.transaction(self.paths.forum) as state:
            state.update(base_sha=self.base, main_sha=self.base)
        diagnostics = [{**self.error("A.lean"), "id": name, "line": line,
                        "message": "fixture compiler error in " + name}
                       for line, name in enumerate(("a", "wrapper", "later"), start=2 if import_b else 1)]
        self.controller.refresh(self.build_result(diagnostics, checked=["B.lean"]))
        state = self.controller.state()
        tasks = {state["chunks"][task["chunk_ids"][0]]["name"]: tid
                 for tid, task in state["tasks"].items() if task["files"] == ["A.lean"]}
        return tasks, diagnostics[-1]

    async def test_finished_failing_build_clears_exhausted_configuration_and_dispatches_repairs(self):
        tid = self.exhausted_configuration_task()
        self.controller.max_attempts = 5
        result = self.authoritative_build_result()
        self.assertFalse(result["passed"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["returncode"], 1)
        self.controller.refresh(result, authoritative_full_build=True)
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        self.assertEqual(state["tasks"][tid]["attempts"], 5)
        self.assertEqual(state["phase"], "preparing")  # no evaluation-success promotion
        self.assertTrue(any(t["kind"] == "repair" and t["status"] == "pending"
                            for t in state["tasks"].values()))
        with patch.object(self.controller, "worker", new=AsyncMock()) as worker:
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice", "Bob"})
            records = list(self.controller.workers.values())
            for record in records:
                self.assertEqual(self.controller.state()["tasks"][record["task_id"]]["kind"], "repair")
            await asyncio.gather(*(record["task"] for record in records))
            self.assertEqual(worker.await_count, 2)
            await self.controller.reap_workers()

    async def test_environment_reconciliation_rejects_partial_stale_or_unstable_builds(self):
        tid = self.exhausted_configuration_task()
        good = self.authoritative_build_result()
        cases = {
            "unmarked_stream_or_review": (good, False),
            "unstable": ({**good, "source_stable": False}, True),
            "missing_stability": ({k: v for k, v in good.items() if k != "source_stable"}, True),
            "missing_identity": ({k: v for k, v in good.items() if k != "source_identity"}, True),
            "missing_revision": ({k: v for k, v in good.items() if k != "config_revision"}, True),
            "stale_revision": ({**good, "config_revision": -1}, True),
            "stale_head": ({**good, "source_identity": {**good["source_identity"], "main_sha": "old"}}, True),
            "stale_scope": ({**good, "source_identity": {**good["source_identity"], "scope": {}}}, True),
            "stale_lake_args": ({**good, "source_identity": {**good["source_identity"], "lake_args": ["-Kold=true"]}}, True),
            "partial_command": ({**good, "command": [*good["command"], "+A"]}, True),
            "missing_command": ({k: v for k, v in good.items() if k != "command"}, True),
            "interrupted": ({**good, "returncode": -15}, True),
            "missing_returncode": ({k: v for k, v in good.items() if k != "returncode"}, True),
            "invalid_returncode": ({**good, "returncode": True}, True),
        }
        for label, (result, authoritative) in cases.items():
            with self.subTest(label=label):
                self.controller.refresh(result, authoritative_full_build=authoritative)
                state = self.controller.state()
                self.assertEqual(state["tasks"][tid]["status"], "pending")
                self.assertEqual(state["tasks"][tid]["attempts"], 5)

    async def test_build_captures_configuration_revision_before_await(self):
        result = self.authoritative_build_result()
        result.pop("config_revision")
        started_revision = self.controller.state()["config"]["revision"]
        async def finish_build(_execute):
            with bump_tasks.transaction(self.paths.forum) as state:
                state["config"]["revision"] += 1
            return result
        with patch.object(self.controller, "checked_job", side_effect=finish_build):
            actual = await self.controller.build()
        self.assertEqual(actual["config_revision"], started_revision)
        self.assertNotEqual(actual["config_revision"], self.controller.state()["config"]["revision"])

    async def test_initial_full_build_reconciles_exhausted_configuration_before_dispatch(self):
        tid = self.exhausted_configuration_task()
        result = self.authoritative_build_result()
        async def worker(*args):
            state = self.controller.state()
            self.assertEqual(state["tasks"][tid]["status"], "complete")
            self.assertEqual(state["tasks"][tid]["attempts"], 5)
            (self.paths.unity / "stop-requested").touch()
        with patch.object(self.controller, "prepare", new=AsyncMock()), \
                patch.object(self.controller, "build", new=AsyncMock(return_value=result)), \
                patch.object(self.controller, "worker", side_effect=worker) as launched:
            self.assertFalse(await self.controller.run())
        self.assertEqual({call.args[0].name for call in launched.await_args_list}, {"Alice", "Bob"})

    async def test_scope_change_full_build_reconciles_exhausted_configuration(self):
        tid = self.exhausted_configuration_task()
        (self.paths.unity / "bump").mkdir(parents=True, exist_ok=True)
        changed_scope = {**self.scope, "default_modules": {"A": "A.lean"}}
        calls = []
        with patch.object(self.controller, "discover_scope", return_value=changed_scope), \
                patch.object(bump_workflow.bump_jobs, "run", side_effect=self.scope_compiler(calls)):
            self.assertFalse(await self.controller.review())
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "complete")
        self.assertEqual([command for root, command in calls if root == self.root], [
            ["lake", "--rehash", "build"], ["lake", "--rehash", "build", "+B"]])

    async def test_post_integration_full_build_reconciles_exhausted_configuration(self):
        tid, candidate, worker = self.submit()
        config_tid = self.exhausted_configuration_task()
        self.active_worker(tid, worker)
        async def rebuilt():
            return self.authoritative_build_result()
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
                patch.object(bump_verification, "build", return_value=self.build_result([], checked=["A.lean"])), \
                patch.object(self.controller, "build", side_effect=rebuilt):
            self.assertTrue(await self.controller.integrate_ready())
        self.assertEqual(self.controller.state()["tasks"][config_tid]["status"], "complete")

    async def test_helper_merge_retires_indirectly_fixed_wrapper_before_dispatch(self):
        tasks, later_error = self.same_file_declaration_tasks()
        helper, wrapper, later = (tasks[name] for name in ("a", "wrapper", "later"))
        self.assertIn(wrapper, self.controller.state()["tasks"][later]["dependencies"])
        bump_tasks.claim(self.paths.forum, "Alice", helper)
        tree = self.worktree(self.paths, "Alice", helper)
        bump_tasks.configure_assignment(self.paths.forum, "Alice", helper, tree, self.base)
        (tree / "A.lean").write_text("def a := 1\ndef wrapper := a\ndef later := missingLater\n")
        forum.configure(self.paths.forum, self.root)
        candidate = forum.submit_repair("Alice", helper, "helper repair also resolves wrapper error")
        closed = self.active_worker(helper, tree)

        async def rebuilt():
            return self.authoritative_build_result([later_error], checked=["B.lean"])

        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
                patch.object(bump_verification, "build", return_value=self.build_result([later_error])), \
                patch.object(self.controller, "build", side_effect=rebuilt):
            self.assertTrue(await self.controller.integrate_ready())
        self.assertTrue(closed)
        state = self.controller.state()
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(state["tasks"][helper]["status"], "complete")
        self.assertEqual(state["tasks"][wrapper]["status"], "complete")
        self.assertEqual(state["tasks"][wrapper]["attempts"], 0)
        self.assertEqual(state["tasks"][wrapper]["diagnostics"], [])
        self.assertEqual(state["tasks"][later]["status"], "pending")
        self.assertEqual(state["tasks"][later]["dependencies"], [])
        self.assertEqual(state["last_build"]["returncode"], 1)
        self.assertNotIn("A.lean", state["last_build"]["checked_files"])
        self.assertNotEqual(state["phase"], "complete")
        with patch.object(self.controller, "worker", new=AsyncMock()) as worker:
            self.controller.launch_ready()
            records = list(self.controller.workers.values())
            self.assertEqual([record["task_id"] for record in records], [later])
            await asyncio.gather(*(record["task"] for record in records))
            self.assertEqual(worker.await_count, 1)
            await self.controller.reap_workers()
        state = self.controller.state()
        self.assertEqual(state["tasks"][wrapper]["attempts"], 0)
        self.assertEqual(state["tasks"][later]["attempts"], 1)
        self.assertFalse(any(event["kind"] == "claim" and event.get("task_id") == wrapper
                             for event in state["events"]))

    def supplemental_tasks(self):
        (self.root / "B.lean").write_text("import A\ndef b := a\n")
        (self.root / "Tests.lean").write_text("import A\n#eval a\n")
        (self.root / "Later.lean").write_text("import B\nimport Tests\ndef later := missingLater\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "supplemental declaration and command fixture")
        self.base = self.git(self.root, "rev-parse", "HEAD")
        self.scope.update(default_modules={"A": "A.lean"}, selected_modules={
            "A": "A.lean", "B": "B.lean", "Tests": "Tests.lean", "Later": "Later.lean"})
        with bump_tasks.transaction(self.paths.forum) as state:
            state.update(base_sha=self.base, main_sha=self.base)
            state["config"].update(scope=deepcopy(self.scope), modules=["Tests", "B", "Later"])
        diagnostics = [self.error("A.lean"), {**self.error("B.lean"), "line": 2},
                       {**self.error("Tests.lean"), "line": 2, "kind": "command"},
                       {**self.error("Later.lean"), "line": 3}]
        self.controller.refresh(self.build_result(diagnostics))
        tasks = {task["files"][0]: tid for tid, task in self.controller.state()["tasks"].items()}
        with bump_tasks.transaction(self.paths.forum) as state:
            for name in ("B.lean", "Tests.lean"):
                task = state["tasks"][tasks[name]]
                task["attempts"] = 5
                for cid in task["chunk_ids"]:
                    state["chunk_attempts"][cid] = 5
        self.assertEqual(self.controller.state()["tasks"][tasks["Tests.lean"]]["kind"], "command")
        self.assertEqual(self.controller.state()["tasks"][tasks["B.lean"]]["kind"], "repair")
        return tasks

    def scope_compiler(self, calls, *, remaining_error=False):
        real_run = bump_workflow.bump_jobs.run
        def execute(root, args, **kwargs):
            if args[0] != "lake":
                return real_run(root, args, **kwargs)
            if args[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, "offline fixture version\n", "")
            calls.append((Path(kwargs.get("cwd", root)).resolve(), list(args)))
            modules = [arg[1:] for arg in args if arg.startswith("+")] or ["A"]
            lines = ["✔ Built " + module for module in modules
                     if module != "Later" or not remaining_error]
            failed = remaining_error and "Later" in modules
            if failed:
                lines.append("Later.lean:3:1: error: unknown identifier 'missingLater'")
            return subprocess.CompletedProcess(args, int(failed), "\n".join(lines) + "\n", "")
        return execute

    async def supplemental_merge_regression(self, *, remaining_error):
        tasks = self.supplemental_tasks()
        tid, candidate, worker = self.submit()
        finished = self.active_worker(tid, worker)
        calls = []
        # Real integration, build aggregation and refresh; only compiler execution
        # is replaced. Mocking Controller.build would hide the original bug.
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
                patch.object(bump_workflow.bump_jobs, "run", side_effect=self.scope_compiler(
                    calls, remaining_error=remaining_error)):
            self.assertTrue(await self.controller.integrate_ready())
        self.assertTrue(finished)
        self.assertEqual([command for root, command in calls if root == self.root], [
            ["lake", "--rehash", "build"],
            ["lake", "--rehash", "build", "+B", "+Later", "+Tests"]])
        state = self.controller.state()
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        for name in ("B.lean", "Tests.lean"):
            task = state["tasks"][tasks[name]]
            self.assertEqual(task["status"], "complete")
            self.assertEqual(task["attempts"], 5)
            self.assertTrue(all(state["chunk_attempts"][cid] == 5 for cid in task["chunk_ids"]))
        self.assertEqual(state["tasks"][tasks["Later.lean"]]["dependencies"], [])
        self.assertEqual(state["tasks"][tasks["Later.lean"]]["status"],
                         "pending" if remaining_error else "complete")
        self.assertEqual(state["last_build"]["returncode"], int(remaining_error))
        self.assertEqual(len(state["last_build"]["builds"]), 2)
        self.assertTrue(all("source_identity" not in item for item in state["last_build"]["builds"]))
        self.assertNotEqual(state["phase"], "complete")
        resolved = next(event for event in state["events"]
                        if event["kind"] == "declaration_resolved" and event["task_id"] == tasks["B.lean"])
        self.assertEqual(resolved["log_ref"], state["last_build"]["log_ref"])
        with patch.object(self.controller, "worker", new=AsyncMock()) as launched:
            self.controller.launch_ready()
            records = list(self.controller.workers.values())
            self.assertEqual([record["task_id"] for record in records],
                             [tasks["Later.lean"]] if remaining_error else [])
            await asyncio.gather(*(record["task"] for record in records))
            self.assertEqual(launched.await_count, int(remaining_error))
            await self.controller.reap_workers()
        self.assertFalse(any(event["kind"] == "claim" and event.get("task_id") in
                             {tasks["B.lean"], tasks["Tests.lean"]}
                             for event in self.controller.state()["events"]))

    async def test_postmerge_scope_build_retires_supplemental_declaration_and_command(self):
        await self.supplemental_merge_regression(remaining_error=False)

    async def test_postmerge_scope_build_preserves_unrelated_supplemental_error(self):
        await self.supplemental_merge_regression(remaining_error=True)

    async def test_scope_reconciliation_requires_every_current_component(self):
        tasks = self.supplemental_tasks()
        calls = []
        with patch.object(bump_workflow.bump_jobs, "run", side_effect=self.scope_compiler(calls)):
            good = await self.controller.build()
        config_tid = self.exhausted_configuration_task()
        cases = {}
        for index in range(2):
            for name, change in {
                "missing_exit": lambda item: item.pop("returncode"),
                "interrupted": lambda item: item.update(returncode=-15),
                "boolean_exit": lambda item: item.update(returncode=True),
                "unstable": lambda item: item.update(source_stable=False),
                "old_head": lambda item: item["source_identity"].update(main_sha="old"),
                "old_files": lambda item: item["source_identity"]["files"].update({"A.lean": "old"}),
                "wrong_command": lambda item: item["command"].append("+NotSelected"),
            }.items():
                changed = deepcopy(good)
                # Do not let mutation of one component also mutate the aggregate
                # identity through the in-memory reference returned by build_scope.
                changed["builds"][index] = deepcopy(changed["builds"][index])
                change(changed["builds"][index])
                cases[f"{index}_{name}"] = changed
        for index in range(2):
            changed = deepcopy(good)
            changed["builds"].pop(index)
            cases[f"missing_component_{index}"] = changed
        bare = deepcopy(good["builds"][0])
        bare["config_revision"] = good["config_revision"]
        cases["default_only"] = bare
        cases["no_components"] = {**good, "builds": None}
        cases["wrong_plan"] = {**good, "commands": [good["commands"][0]]}
        for label, result in cases.items():
            with self.subTest(label=label):
                self.controller.refresh(result, authoritative_full_build=True)
                state = self.controller.state()
                for name in ("B.lean", "Tests.lean"):
                    self.assertEqual(state["tasks"][tasks[name]]["status"], "pending")
                    self.assertEqual(state["tasks"][tasks[name]]["attempts"], 5)
                self.assertEqual(state["tasks"][config_tid]["status"], "pending")
        with patch.object(bump_verification, "source_identity", side_effect=AssertionError("no probes in refresh")):
            self.controller.refresh(good, authoritative_full_build=True)
        state = self.controller.state()
        self.assertEqual(state["tasks"][tasks["B.lean"]]["status"], "complete")
        self.assertEqual(state["tasks"][tasks["Tests.lean"]]["status"], "complete")
        self.assertEqual(state["tasks"][config_tid]["status"], "complete")

    async def test_initial_scope_build_covers_supplemental_modules(self):
        tasks = self.supplemental_tasks()
        calls = []
        async def review():
            state = self.controller.state()
            self.assertEqual(state["tasks"][tasks["B.lean"]]["status"], "complete")
            self.assertEqual(state["tasks"][tasks["Tests.lean"]]["status"], "complete")
            return True
        with patch.object(self.controller, "prepare", new=AsyncMock()), \
                patch.object(self.controller, "review", side_effect=review), \
                patch.object(bump_workflow.bump_jobs, "run", side_effect=self.scope_compiler(calls)), \
                patch.object(self.controller, "worker", new=AsyncMock()) as worker:
            self.assertTrue(await self.controller.run())
        self.assertEqual([command for _, command in calls], [
            ["lake", "--rehash", "build"],
            ["lake", "--rehash", "build", "+B", "+Later", "+Tests"]])
        worker.assert_not_awaited()

    async def test_declaration_reconciliation_requires_current_authoritative_build_and_file_bytes(self):
        tasks, later_error = self.same_file_declaration_tasks()
        wrapper = tasks["wrapper"]
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][wrapper]["attempts"] = 3
            for cid in state["tasks"][wrapper]["chunk_ids"]:
                state["chunk_attempts"][cid] = 3
        good = self.authoritative_build_result([later_error], checked=["B.lean"])
        identity = good["source_identity"]
        cases = {
            "unmarked_stream_or_review": (good, False),
            "unstable": ({**good, "source_stable": False}, True),
            "missing_stability": ({k: v for k, v in good.items() if k != "source_stable"}, True),
            "missing_identity": ({k: v for k, v in good.items() if k != "source_identity"}, True),
            "missing_revision": ({k: v for k, v in good.items() if k != "config_revision"}, True),
            "stale_revision": ({**good, "config_revision": -1}, True),
            "stale_head": ({**good, "source_identity": {**identity, "main_sha": "old"}}, True),
            "stale_scope": ({**good, "source_identity": {**identity, "scope": {}}}, True),
            "stale_lake_args": ({**good, "source_identity": {**identity, "lake_args": ["-Kold=true"]}}, True),
            "partial_command": ({**good, "command": [*good["command"], "+A"]}, True),
            "missing_command": ({k: v for k, v in good.items() if k != "command"}, True),
            "interrupted": ({**good, "returncode": -15}, True),
            "missing_returncode": ({k: v for k, v in good.items() if k != "returncode"}, True),
            "invalid_returncode": ({**good, "returncode": True}, True),
            "missing_file_hashes": ({**good, "source_identity": {**identity, "files": {}}}, True),
            "mismatched_file_hash": ({**good, "source_identity": {
                **identity, "files": {**identity["files"], "A.lean": "old"}}}, True),
        }
        for label, (result, authoritative) in cases.items():
            claimed_checked = {**result, "diagnostics": [], "errors": [],
                               "checked_files": ["A.lean", "B.lean"], "passed": True, "completed": True}
            if type(result.get("returncode")) is int and result["returncode"] >= 0:
                claimed_checked["returncode"] = 0
            for evidence, build in (("later_error", result), ("all_checked", claimed_checked)):
                with self.subTest(label=label, evidence=evidence):
                    self.controller.refresh(build, authoritative_full_build=authoritative)
                    state = self.controller.state()
                    self.assertEqual(state["tasks"][wrapper]["status"], "pending")
                    self.assertEqual(state["tasks"][wrapper]["attempts"], 3)
                    self.assertTrue(state["tasks"][wrapper]["diagnostics"])
                    self.assertTrue(all(state["chunks"][cid]["status"] != "clean"
                                        for cid in state["tasks"][wrapper]["chunk_ids"]))
                    self.assertIn(wrapper, state["tasks"][tasks["later"]]["dependencies"])
                    self.assertFalse(any(event["kind"] == "declaration_resolved"
                                         and event.get("task_id") == wrapper for event in state["events"]))
        self.controller.refresh(good, authoritative_full_build=True)
        state = self.controller.state()
        self.assertEqual(state["tasks"][wrapper]["status"], "complete")
        self.assertEqual(state["tasks"][wrapper]["attempts"], 3)
        self.assertTrue(all(state["chunk_attempts"][cid] == 3 for cid in state["tasks"][wrapper]["chunk_ids"]))
        self.assertEqual(state["tasks"][tasks["later"]]["dependencies"], [])

    async def test_declaration_reconciliation_rejects_same_head_import_or_config_edits_without_probes(self):
        tasks, later_error = self.same_file_declaration_tasks(import_b=True)
        wrapper = tasks["wrapper"]
        (self.root / "lakefile.toml").write_text('name = "offline_fixture"\n')
        (self.root / "B.lean").write_text("def b := 1\n")
        self.git(self.root, "add", "B.lean", "lakefile.toml")
        self.git(self.root, "commit", "-qm", "import and configuration freshness fixture")
        head = self.git(self.root, "rev-parse", "HEAD")
        with bump_tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = head
        good = self.authoritative_build_result([later_error], checked=["B.lean"])
        checked_good = self.authoritative_build_result([], checked=["A.lean", "B.lean"])
        self.assertIn("lakefile.toml", good["source_identity"]["files"])
        original_a = (self.root / "A.lean").read_bytes()
        mutations = {"B.lean": "def b := missingNewImportDependency\n",
                     "lakefile.toml": 'name = "changed_configuration"\n'}
        with patch.object(bump_verification, "source_identity", side_effect=AssertionError("no source-identity probe")) as source_probe, \
                patch.object(bump_verification, "_tool_identity", side_effect=AssertionError("no Lean/Lake version probe")) as tool_probe:
            for path, changed in mutations.items():
                with self.subTest(path=path):
                    target = self.root / path
                    original = target.read_text()
                    try:
                        target.write_text(changed)
                        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), head)
                        self.assertEqual((self.root / "A.lean").read_bytes(), original_a)
                        self.assertTrue(good["source_stable"])
                        for evidence, build in (("later_error", good), ("all_checked", checked_good)):
                            with self.subTest(evidence=evidence):
                                self.controller.refresh(build, authoritative_full_build=True)
                                state = self.controller.state()
                                self.assertEqual(state["tasks"][wrapper]["status"], "pending")
                                self.assertEqual(state["tasks"][wrapper]["attempts"], 0)
                                self.assertTrue(all(state["chunks"][cid]["status"] != "clean"
                                                    for cid in state["tasks"][wrapper]["chunk_ids"]))
                                self.assertIn(wrapper, state["tasks"][tasks["later"]]["dependencies"])
                                self.assertFalse(any(event["kind"] == "declaration_resolved"
                                                     and event.get("task_id") == wrapper for event in state["events"]))
                    finally:
                        target.write_text(original)
            self.controller.refresh(checked_good, authoritative_full_build=True)
            self.assertEqual(self.controller.state()["tasks"][wrapper]["status"], "complete")
            source_probe.assert_not_called()
            tool_probe.assert_not_called()

    async def test_checked_build_does_not_override_declaration_owner_or_current_ranges(self):
        tasks, _ = self.same_file_declaration_tasks()
        wrapper = tasks["wrapper"]
        good = self.authoritative_build_result([], checked=["A.lean", "B.lean"])
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][wrapper]["owner"] = "Alice"
        self.controller.refresh(good, authoritative_full_build=True)
        state = self.controller.state()
        self.assertEqual(state["tasks"][wrapper]["status"], "pending")
        self.assertEqual(state["tasks"][wrapper]["owner"], "Alice")
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][wrapper]["owner"] = None
        inventory = self.controller.inventory()
        cid = self.controller.state()["tasks"][wrapper]["chunk_ids"][0]
        missing = deepcopy(inventory)
        del missing["chunks"][cid]
        after_exit = deepcopy(inventory)
        after_exit["chunks"][cid]["after_exit"] = True
        for label, unavailable in (("missing_range", missing), ("after_exit", after_exit)):
            with self.subTest(label=label), patch.object(self.controller, "inventory", return_value=unavailable):
                self.controller.refresh(good, authoritative_full_build=True)
                self.assertEqual(self.controller.state()["tasks"][wrapper]["status"], "pending")
        self.controller.refresh(good, authoritative_full_build=True)
        state = self.controller.state()
        self.assertEqual(state["tasks"][wrapper]["status"], "complete")
        self.assertEqual(state["tasks"][wrapper]["attempts"], 0)

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

    def passed_snapshot(self):
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
        return snapshot

    async def test_every_roster_agent_including_last_can_receive_repairs(self):
        with patch.object(self.controller, "worker", new=AsyncMock()) as worker:
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice", "Bob"})
            await asyncio.gather(*(record["task"] for record in self.controller.workers.values()))
            self.assertEqual({call.args[0].name for call in worker.await_args_list}, {"Alice", "Bob"})
            self.assertEqual({call.args[3] for call in worker.await_args_list}, {"repairing"})
            await self.controller.reap_workers()
        self.assertFalse(self.controller.state()["leases"])

    async def test_exact_snapshot_primary_review_allows_contributors_and_unlimited_attempt_setting(self):
        self.controller.max_attempts = float("inf")
        with bump_tasks.transaction(self.paths.forum) as state:
            for author in ("Alice", "Bob"):
                state["candidates"][author + "-history"] = {"author": author, "status": "merged"}
        snapshot = self.passed_snapshot()
        seen = []
        async def critic(agent, task_id, tree, profile, interrupt):
            seen.append((agent.name, profile))
            forum.configure(self.paths.forum, self.root, "critic")
            forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], True, "offline fixture review")
        with patch.object(bump_verification, "verify_final", return_value=deepcopy(snapshot)), \
                patch.object(bump_verification, "source_identity", return_value=snapshot["source_identity"]), \
                patch.object(self.controller, "worker", side_effect=critic):
            self.assertTrue(await self.controller.review())
        self.assertEqual(seen, [("Alice", "critic")])
        state = self.controller.state()
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["review"]["reviewer"], "Alice")
        self.assertEqual(state["review"]["verdict"]["snapshot_sha256"], snapshot["sha256"])
        self.assertTrue(state["final_report"])

    async def test_configured_primary_reviews_regardless_of_roster_order(self):
        alice, bob = self.roster.agents
        charlie = SimpleNamespace(name="Charlie")
        snapshot = self.passed_snapshot()
        for agents in ([alice, bob], [bob, alice], [alice, bob, charlie]):
            with self.subTest(order=[agent.name for agent in agents]):
                roster = SimpleNamespace(agents=agents, primary=bob)
                controller = bump_workflow.Controller(self.paths, roster, max_attempts=2)
                seen = []
                async def critic(agent, task_id, tree, profile, interrupt):
                    seen.append(agent.name)
                    forum.configure(self.paths.forum, self.root, "critic")
                    forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], True,
                                                   "offline configured-primary review")
                with patch.object(bump_verification, "verify_final", return_value=deepcopy(snapshot)), \
                        patch.object(bump_verification, "source_identity", return_value=snapshot["source_identity"]), \
                        patch.object(controller, "worker", side_effect=critic):
                    self.assertTrue(await controller.review())
                self.assertEqual(seen, ["Bob"])
                self.assertEqual(controller.state()["review"]["reviewer"], "Bob")

    async def test_child_recursion_keeps_same_roster_and_primary_review_contract(self):
        from unity import bump_recursive
        child_root = self.temp / "child"
        child_root.mkdir()
        child_paths = bump_workflow.paths_for(child_root)
        bump_tasks.initialize(child_paths.forum, child_root, run_id="child-test", base_sha=self.base,
                              version="v4.34.1", config={"scope": self.scope})
        with bump_tasks.transaction(child_paths.forum) as state:
            state["candidates"]["primary-contribution"] = {"author": "Alice", "status": "merged"}
        setup = {"phase": "ready", "pending_tasks": [], "dependencies": {}}
        with bump_tasks.transaction(self.paths.forum) as state:
            state["setup"] = setup
        snapshot = self.passed_snapshot()
        ancestors = (("offline-child", "v4.34.1"),)
        receipts, seen = [], []
        async def child_run(paths, roster, **options):
            self.assertIs(paths, child_paths)
            self.assertIs(roster, self.roster)
            self.assertIs(roster.primary, self.roster.primary)
            self.assertIs(options["agent_locks"], self.controller.agent_locks)
            self.assertEqual(options["ancestors"], ancestors)
            self.assertEqual(options["max_attempts"], self.controller.max_attempts)
            self.assertIs(options["setup"], setup)
            child_controller = bump_workflow.Controller(paths, roster,
                max_attempts=options["max_attempts"], agent_locks=options["agent_locks"])
            return await child_controller.review()
        async def dispatch(paths, environment, *, migrate_child, **options):
            self.assertIs(paths, self.paths)
            receipts.append(await migrate_child(child_paths, setup, ancestors=ancestors))
            return False
        async def critic(controller, agent, task_id, tree, profile, interrupt):
            seen.append((agent.name, profile))
            forum.configure(controller.paths.forum, controller.paths.project_root, profile)
            forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], True,
                                           "offline child primary review")
        with patch.object(bump_recursive, "run_children", side_effect=dispatch), \
                patch.object(bump_workflow, "run", side_effect=child_run), \
                patch.object(self.controller, "commit_setup"), \
                patch.object(bump_workflow, "task_worktree", return_value=child_root), \
                patch.object(bump_workflow.Controller, "worker", new=critic), \
                patch.object(bump_verification, "verify_final", return_value=snapshot), \
                patch.object(bump_verification, "source_identity", return_value=snapshot["source_identity"]):
            await asyncio.wait_for(self.controller.prepare(), timeout=5)
        self.assertEqual(seen, [("Alice", "critic")])
        self.assertEqual(receipts, [{"build_passed": True, "review_approved": True,
                                    "verified_commit": self.base, "snapshot_id": snapshot["snapshot_id"]}])
        child = bump_tasks.load(child_paths.forum)
        self.assertEqual(child["phase"], "complete")
        self.assertEqual(child["review"]["reviewer"], self.roster.primary.name)

    async def test_primary_review_exhaustion_never_falls_back_to_another_agent(self):
        snapshot = self.passed_snapshot()
        with patch.object(bump_verification, "verify_final", return_value=snapshot), \
                patch.object(self.controller, "worker", new=AsyncMock()) as worker:
            with self.assertRaisesRegex(ValueError, "attempts ended"):
                await self.controller.review()
        self.assertEqual([call.args[0].name for call in worker.await_args_list], ["Alice", "Alice"])
        self.assertNotEqual(self.controller.state()["phase"], "complete")
        self.assertNotIn("final_report", self.controller.state())

    async def test_singleton_primary_can_repair_and_review(self):
        alice = self.roster.primary
        roster = SimpleNamespace(agents=[alice], primary=alice)
        controller = bump_workflow.Controller(self.paths, roster, max_attempts=2)
        snapshot = self.passed_snapshot()
        seen = []
        async def worker(agent, task_id, tree, profile, interrupt):
            seen.append((agent.name, profile))
            if profile == "critic":
                forum.configure(self.paths.forum, self.root, "critic")
                forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], True,
                                               "offline singleton-primary review")
        with patch.object(controller, "worker", side_effect=worker), \
                patch.object(bump_verification, "verify_final", return_value=snapshot), \
                patch.object(bump_verification, "source_identity", return_value=snapshot["source_identity"]):
            controller.launch_ready()
            self.assertEqual(set(controller.workers), {"Alice"})
            await controller.workers["Alice"]["task"]
            await controller.reap_workers()
            self.assertTrue(await controller.review())
        self.assertEqual(seen, [("Alice", "repairing"), ("Alice", "critic")])

    async def test_primary_review_rejection_reopens_only_named_task(self):
        snapshot = self.passed_snapshot()
        tasks = self.controller.state()["tasks"]
        target = next(tid for tid, task in tasks.items() if task["files"] == ["A.lean"])
        sibling = next(tid for tid, task in tasks.items() if task["files"] == ["B.lean"])
        with bump_tasks.transaction(self.paths.forum) as state:
            for task in state["tasks"].values():
                task["status"] = "complete"
            state["candidates"]["primary-contribution"] = {"author": "Alice", "status": "merged"}
        async def critic(agent, *args):
            self.assertEqual(agent.name, "Alice")
            forum.configure(self.paths.forum, self.root, "critic")
            forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], False,
                                           "offline targeted repair finding", [target])
        with patch.object(bump_verification, "verify_final", return_value=snapshot), \
                patch.object(self.controller, "worker", side_effect=critic):
            self.assertFalse(await self.controller.review())
        state = self.controller.state()
        self.assertEqual(state["phase"], "repairing")
        self.assertEqual(state["tasks"][target]["status"], "pending")
        self.assertEqual(state["tasks"][sibling]["status"], "complete")
        self.assertNotIn("final_report", state)

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
            self.assertEqual([author for author, _ in called], [roster.primary.name, roster.primary.name])
