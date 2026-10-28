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

    def stale_import_command_fixture(self):
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"] = {}
        old = {"id": "old-import", "path": "A.lean", "line": None, "kind": "import",
               "severity": "error", "message": "bad import 'Old'"}
        self.controller.refresh(self.build_result([old]))
        return "command-" + bump_tasks.digest(["A.lean", "import"])[:16], old

    async def test_authoritative_body_reach_retires_import_command_keeps_repair(self):
        tid, old = self.stale_import_command_fixture()
        error = {**self.error("A.lean"), "id": "current-declaration"}
        result = self.authoritative_build_result([error], checked=["B.lean"])
        result["log_ref"] = {"artifact_id": "full-build"}
        self.controller.refresh(result, authoritative_full_build=True)
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        self.assertEqual(state["tasks"][tid]["diagnostics"], [old])
        event = next(e for e in state["events"] if e["kind"] == "import_command_resolved")
        self.assertEqual(event["diagnostics"], [old])
        self.assertEqual(event["evidence"]["error_id"], "current-declaration")
        repair = next(t for t in state["tasks"].values() if t["kind"] == "repair" and t["files"] == ["A.lean"])
        self.assertEqual(repair["status"], "pending")
        self.assertEqual(repair["diagnostics"], [error])
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertIn(repair["task_id"], [w["task_id"] for w in self.controller.workers.values()])
            await asyncio.gather(*(w["task"] for w in self.controller.workers.values()))
            await self.controller.reap_workers()

    async def test_import_retirement_rejects_uncertain_or_active_obligations(self):
        tid, old = self.stale_import_command_fixture()
        original = deepcopy(self.controller.state())
        error = {**self.error("A.lean"), "id": "current-declaration"}
        good = self.authoritative_build_result([error])
        cases = ("no_error", "stale_head", "stale_config", "stale_source", "partial_command", "unmapped",
                 "pathless", "excluded", "mixed", "manual", "active_owner", "lease", "candidate", "merging", "unmatched")
        for case in cases:
            with self.subTest(case=case):
                with bump_tasks.transaction(self.paths.forum) as state:
                    for key in ("tasks", "chunks", "files", "leases", "candidates", "config"):
                        state[key] = deepcopy(original[key])
                    if case == "mixed": state["tasks"][tid]["diagnostics"].append(error)
                    if case == "manual": state["tasks"][tid]["reason"] = "additional requested work"
                    if case == "active_owner": state["tasks"][tid]["owner"] = "Alice"
                    if case == "lease": state["leases"]["A.lean"] = {"task_id": "other", "author": "Bob"}
                    if case == "candidate": state["candidates"]["c"] = {"task_id": tid, "status": "submitted"}
                    if case == "merging": state["candidates"]["c"] = {"task_id": tid, "status": "merging"}
                    if case == "excluded": state["config"]["scope"]["excluded_files"]["A.lean"] = "excluded"
                result = deepcopy(good)
                if case == "no_error": result["diagnostics"] = result["errors"] = []
                if case == "stale_head": result["source_identity"]["main_sha"] = "old"
                if case == "stale_config": result["config_revision"] += 1
                if case == "stale_source": result["source_identity"]["files"]["A.lean"] = "old"
                if case == "partial_command": result["command"] = ["lake", "--rehash", "build", "+A"]
                if case == "unmapped": result["unmapped"] = [{"path": None}]
                if case == "pathless": result["diagnostics"].append({"path": None, "severity": "error", "kind": "environment"})
                if case == "unmatched": result["diagnostics"][0]["line"] = 99
                if case == "excluded": result["source_identity"]["scope"] = deepcopy(self.controller.state()["config"]["scope"])
                self.controller.refresh(result, authoritative_full_build=True)
                state = self.controller.state()
                self.assertEqual(state["tasks"][tid]["status"], "pending")
                self.assertEqual(state["tasks"][tid]["diagnostics"][0], old)

    async def test_import_retirement_never_uses_new_bad_import_or_ambiguous_chunk(self):
        tid, _ = self.stale_import_command_fixture()
        before = deepcopy(self.controller.state())
        old = {"path": "A.lean", "line": None, "kind": "import", "severity": "error", "message": "bad import"}
        result = self.authoritative_build_result([old, self.error("A.lean")])
        self.controller.refresh(result, authoritative_full_build=True)
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "pending")
        with bump_tasks.transaction(self.paths.forum) as state:
            for key in ("tasks", "chunks", "files", "leases", "candidates", "config"):
                state[key] = deepcopy(before[key])
        inventory = self.controller.inventory()
        chunk = next(row for row in inventory["chunks"].values() if row["path"] == "A.lean")
        inventory["chunks"]["ambiguous"] = {**chunk, "chunk_id": "ambiguous"}
        with patch.object(self.controller, "inventory", return_value=inventory):
            self.controller.refresh(self.authoritative_build_result([self.error("A.lean")]), authoritative_full_build=True)
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "pending")

    async def test_import_retirement_rejects_qualified_unparsed_declaration(self):
        tid, _ = self.stale_import_command_fixture()
        inventory = self.controller.inventory()
        chunk = next(row for row in inventory["chunks"].values() if row["path"] == "A.lean")
        chunk["name"] = "Some.Namespace.<unparsed-def>"
        build = self.authoritative_build_result([self.error("A.lean")])
        self.assertEqual(inventory["files"]["A.lean"]["sha256"], build["source_identity"]["files"]["A.lean"])
        with patch.object(self.controller, "inventory", return_value=inventory):
            self.controller.refresh(build, authoritative_full_build=True)
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "pending")

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

    async def test_configuration_handoff_resumes_and_coalesces_saved_children(self):
        tid = self.exhausted_configuration_task()
        with bump_tasks.transaction(self.paths.forum) as state:
            parent = state["tasks"][tid]
            parent.update(kind="configuration", worktree="/retained/worktree", attempts=5)
            state["config_requests"][tid] = {"status": "scheduled"}
            for n in (3, 4):
                key = "configuration-" + str(n)
                state["tasks"][key] = bump_tasks._task(key, "configuration", [], ["lakefile.lean"])
                state["config_requests"][key] = {"request_id": key, "task_id": tid, "status": "scheduled",
                    "files": ["lakefile.lean", "A.lean", "New.lean"], "reason": "rename",
                    "proposed_changes": "move source", "requested_config": {"modules": ["A", "New"]}}
        await self.controller.handle_requests()
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["attempts"], 0)
        self.assertTrue({"A.lean", "New.lean"} <= set(state["tasks"][tid]["files"]))
        self.assertEqual(state["tasks"]["configuration-3"]["grouped_into"], tid)
        self.assertEqual(state["tasks"]["configuration-4"]["status"], "grouped")
        self.controller.reconcile_configuration_handoffs()
        self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 0)
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            record = next(iter(self.controller.workers.values()))
            self.assertEqual(record["task_id"], tid)
            self.assertIn("/retained/worktree", self.controller.state()["tasks"][tid]["previous_worktrees"])
            await record["task"]
            await self.controller.reap_workers()

    async def test_owned_configuration_request_does_not_interrupt_worker(self):
        tid = self.exhausted_configuration_task()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["attempts"] = 0
        bump_tasks.claim(self.paths.forum, "Alice", tid, max_attempts=2)
        forum.configure(self.paths.forum, self.root)
        interrupt = asyncio.Event()
        worker = asyncio.create_task(interrupt.wait())
        self.controller.workers["Alice"] = {"task": worker, "task_id": tid, "interrupt": interrupt,
                                             "tree": self.root, "agent": self.roster.primary}
        response = forum.request_configuration_change("Alice", tid, ["lakefile.lean"],
                                                       "update dependency", "edit lakefile")
        self.assertEqual(response["status"], "already_assigned")
        await self.controller.handle_requests()
        self.assertFalse(interrupt.is_set())
        self.assertFalse(worker.done())
        self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 1)
        interrupt.set()
        await worker
        await self.controller.reap_workers()

    async def test_startup_dispatch_waits_for_saved_configuration_request(self):
        tid = self.exhausted_configuration_task()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["attempts"] = 1
            state["config_requests"]["configuration-10"] = {
                "request_id": "configuration-10", "task_id": tid, "status": "pending",
                "files": [], "reason": "repeat", "proposed_changes": "continue", "requested_config": {}}
        with patch.object(self.controller, "worker", new=AsyncMock()) as worker:
            self.controller.launch_ready()  # The initial build may stream before requests are handled.
            self.assertFalse(self.controller.workers)
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 1)
            await self.controller.handle_requests()
            self.controller.launch_ready()
            record = next(iter(self.controller.workers.values()))
            self.assertEqual(record["task_id"], tid)
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 2)
            await record["task"]
            worker.assert_awaited_once()
            await self.controller.reap_workers()

    async def test_handoff_budget_only_resets_for_new_scope(self):
        tid = self.exhausted_configuration_task()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["config"]["modules"] = ["A"]
            state["tasks"][tid]["requested_config"] = {"modules": ["A"], "lake_args": []}
        def add_request(key, options, files=()):
            with bump_tasks.transaction(self.paths.forum) as state:
                state["config_requests"][key] = {"request_id": key, "task_id": tid, "status": "pending",
                    "files": list(files), "reason": "continue", "proposed_changes": "update", "requested_config": options}
        for key, options in (("configuration-10", {"modules": ["A"], "lake_args": []}),
                             ("configuration-11", {"lake_args": ["-Kmode=dev"]}),
                             ("configuration-12", {"lake_args": []})):
            add_request(key, options)
            await self.controller.handle_requests()
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 5)
        add_request("configuration-13", {"modules": ["A", "New"]}, ["New.lean"])
        await self.controller.handle_requests()
        self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 0)
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["attempts"] = 5
        add_request("configuration-14", {"modules": ["A", "New"]}, ["New.lean"])
        await self.controller.handle_requests()
        self.controller.reconcile_configuration_handoffs()
        self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 5)

    async def test_handoff_rejects_conflicting_and_chained_renames(self):
        tid = self.exhausted_configuration_task()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["config"].update(modules=["A", "B", "C"], scope={"selected_modules": {x: x + ".lean" for x in ("A", "B", "C")}})
            state["tasks"][tid]["requested_config"] = {"module_renames": {"A": "B"}}
        for number, mapping in enumerate(({"A": "C"}, {"C": "B"}, {"B": "C"}), 10):
            key = "configuration-" + str(number)
            with bump_tasks.transaction(self.paths.forum) as state:
                state["config_requests"][key] = {"request_id": key, "task_id": tid, "status": "pending",
                    "files": ["A.lean"], "reason": "rename", "proposed_changes": "move",
                    "requested_config": {"module_renames": mapping}}
            await self.controller.handle_requests()
            state = self.controller.state()
            self.assertEqual(state["config_requests"][key]["status"], "rejected")
            self.assertEqual(state["config_requests"][key]["requested_config"]["module_renames"], mapping)
            self.assertEqual(state["tasks"][tid]["requested_config"]["module_renames"], {"A": "B"})
            self.assertEqual(state["tasks"][tid]["attempts"], 5)

    async def test_saved_conflicting_child_is_rejected_without_stranding_dependents(self):
        tid = self.exhausted_configuration_task()
        key = "configuration-10"
        with bump_tasks.transaction(self.paths.forum) as state:
            state["config_requests"][tid] = {"status": "scheduled"}
            state["config"].update(modules=["A"], scope={"selected_modules": {"A": "A.lean"}})
            state["tasks"][tid]["requested_config"] = {"module_renames": {"A": "B"}}
            state["tasks"][key] = bump_tasks._task(key, "configuration", [], ["A.lean"])
            state["tasks"]["dependent"] = bump_tasks._task("dependent", "repair", [], [])
            state["tasks"]["dependent"]["dependencies"] = [key]
            state["config_requests"][key] = {"request_id": key, "task_id": tid, "status": "scheduled",
                "files": ["A.lean"], "reason": "rename", "proposed_changes": "move",
                "requested_config": {"module_renames": {"A": "C"}}}
        await self.controller.handle_requests()
        state = self.controller.state()
        self.assertEqual(state["config_requests"][key]["status"], "rejected")
        self.assertEqual(state["tasks"][key]["grouped_into"], tid)
        self.assertEqual(state["tasks"]["dependent"]["dependencies"], [tid])
        self.assertEqual(state["tasks"][tid]["attempts"], 5)

    async def test_queued_correction_removes_invalid_rename_and_keeps_new_coverage(self):
        tid = self.exhausted_configuration_task()
        original = {"Root": "NewRoot", "A": "NewA"}
        with bump_tasks.transaction(self.paths.forum) as state:
            state["config"].update(modules=["A", "B"], scope={"selected_modules": {"A": "A.lean", "B": "B.lean"}})
            state["tasks"][tid]["requested_config"] = {"module_renames": original}
            state["config_requests"]["configuration-10"] = {"request_id": "configuration-10", "task_id": tid,
                "status": "pending", "files": ["A.lean"], "reason": "correct", "proposed_changes": "extend",
                "requested_config": {"module_renames": {"A": "NewA", "B": "NewB"}}}
        await self.controller.handle_requests()
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["requested_config"]["module_renames"], {"A": "NewA", "B": "NewB"})
        self.assertEqual(state["tasks"][tid]["attempts"], 0)
        event = next(e for e in state["events"] if e["kind"] == "configuration_rename_corrected")
        self.assertEqual(event["original_renames"], original)
        self.assertEqual(event["corrected_renames"], {"A": "NewA", "B": "NewB"})
        self.assertEqual(state["config_requests"]["configuration-10"]["status"], "scheduled")

    async def test_handoff_waits_for_owner_and_submitted_candidate(self):
        tid = self.exhausted_configuration_task()
        key = "configuration-10"
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid].update(owner="Alice", status="running")
            state["config_requests"][key] = {"request_id": key, "task_id": tid, "status": "pending",
                "files": ["A.lean"], "reason": "rename", "proposed_changes": "move", "requested_config": {}}
        await self.controller.handle_requests()
        state = self.controller.state()
        self.assertEqual(state["config_requests"][key]["status"], "pending")
        self.assertNotIn(key, state["tasks"])
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid].update(owner=None, status="pending")
            state["candidates"]["candidate-1"] = {"task_id": tid, "status": "submitted"}
        await self.controller.handle_requests()
        state = self.controller.state()
        self.assertEqual(state["config_requests"][key]["status"], "pending")
        self.assertNotIn(key, state["tasks"])
        with bump_tasks.transaction(self.paths.forum) as state:
            state["candidates"]["candidate-1"]["status"] = "rejected"
        await self.controller.handle_requests()
        state = self.controller.state()
        self.assertEqual(state["config_requests"][key]["status"], "scheduled")
        self.assertIn("A.lean", state["tasks"][tid]["files"])

    async def test_configuration_candidate_transfers_renamed_module_coverage(self):
        (self.root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n")
        (self.root / "lake-manifest.json").write_text("{}\n")
        (self.root / "A.lean").write_text("theorem inherited : True := by sorry\n")
        self.git(self.root, "add", "lean-toolchain", "lake-manifest.json", "A.lean")
        self.git(self.root, "commit", "-qm", "configuration fixture")
        base = self.git(self.root, "rev-parse", "HEAD")
        tid = "configuration-rename"
        (self.paths.unity / "bump").mkdir(parents=True, exist_ok=True)
        with bump_tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = base
            state["base_sha"] = base
            state["config"].update(modules=["A"], scope=self.scope)
            state["tasks"][tid] = {**bump_tasks._task(tid, "configuration", [], ["A.lean", "New.lean"]),
                "requested_config": {"modules": ["A", "New"], "module_renames": {"A": "New"}}}
        task = bump_tasks.claim(self.paths.forum, "Alice", tid)
        tree = self.worktree(self.paths, "Alice", tid)
        self.git(tree, "mv", "A.lean", "New.lean")
        self.git(tree, "commit", "-qm", "rename source")
        commit = self.git(tree, "rev-parse", "HEAD")
        candidate = {"candidate_id": "candidate-rename", "task_id": tid, "author": "Alice",
            "base_main_sha": base, "commit_sha": commit, "config_revision": task["config_revision"],
            "task_revision": task["revision"], "status": "submitted"}
        with bump_tasks.transaction(self.paths.forum) as state:
            state["candidates"][candidate["candidate_id"]] = candidate
            state["tasks"][tid]["status"] = "submitted"
        scope = {**self.scope, "selected_modules": {"New": "New.lean", "B": "B.lean"}}
        setup = {"phase": "ready", "scope": scope, "modules": ["New"], "lake_args": [], "project_scope": "build"}
        missing_source = self.root / "Missing/Module.lean"
        imports = bump_verification._parse_diagnostics(
            f"error: no such file or directory (error code: 2)\n  file: {missing_source}\n"
            "error: B.lean: bad import 'Missing.Module'\n", self.root)
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0]["kind"], "import")
        self.assertEqual(imports[0]["related_missing_files"], [str(missing_source)])
        with patch.object(bump_setup, "prepare_environment", return_value=setup) as prepare, \
             patch.object(bump_verification, "build", return_value=self.build_result(imports)) as build:
            with bump_tasks.transaction(self.paths.forum) as state:
                state["tasks"][tid]["requested_config"]["module_renames"] = {"A": "B"}
            rejected = self.controller.integrate(candidate)
            self.assertFalse(rejected["accepted"])
            self.assertIn("move", rejected["reason"])
            with bump_tasks.transaction(self.paths.forum) as state:
                state["tasks"][tid]["requested_config"]["module_renames"] = {"A": "New"}
            io_error = {"path": None, "severity": "error", "kind": "environment", "message": "disk I/O failure"}
            build.return_value = {**self.build_result([*imports, io_error]), "unmapped": [io_error]}
            self.assertFalse(self.controller.integrate(candidate)["accepted"])
            build.return_value = self.build_result(imports)
            result = self.controller.integrate(candidate)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(prepare.call_args.kwargs["modules"], ["New"])
        self.assertEqual(self.controller.state()["config"]["modules"], ["New"])
        self.assertFalse((self.root / "A.lean").exists())
        self.assertTrue((self.root / "New.lean").is_file())
        audit = bump_verification.audit_sources(self.root, base, scope)
        self.assertTrue(audit["passed"], audit["issues"])
        self.assertEqual(audit["inherited"][0]["baseline_path"], "A.lean")

    async def test_streamed_environment_errors_wait_for_completed_build(self):
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"] = {}
        observations = []
        source = self.error("A.lean")
        environment = {"path": None, "line": None, "column": None, "severity": "error",
                       "kind": "environment", "message": "error: unrelated environment failure"}
        async def streamed_build(*, stream=False):
            self.assertTrue(stream)
            await self.controller.diagnostics_queue.put(("stdout", "error: no such file or directory (error code: 2)\n"))
            await asyncio.sleep(0.25)
            self.assertEqual(observations, [])
            await self.controller.diagnostics_queue.put(("stdout", "error: A.lean:1:1: unknown identifier\n"))
            await asyncio.sleep(0.25)
            self.assertTrue(observations)
            return self.build_result([source, environment])
        def observe_launch():
            if self.controller.initial_build and not self.controller.initial_build.done():
                observations.append(self.controller.state()["tasks"])
        with patch.object(self.controller, "prepare", new=AsyncMock()), \
             patch.object(self.controller, "build", side_effect=streamed_build), \
             patch.object(self.controller, "launch_ready", side_effect=observe_launch):
            with self.assertRaisesRegex(ValueError, "blocked or exhausted"):
                await self.controller.run()
        self.assertTrue(any(task["kind"] == "repair" for state in observations for task in state.values()))
        self.assertFalse(any(task["kind"] == "configuration" for state in observations for task in state.values()))
        self.assertTrue(any(task["kind"] == "configuration" for task in self.controller.state()["tasks"].values()))

    async def test_resumed_stream_waits_for_full_refresh_before_dispatching_dependencies(self):
        (self.root / "B.lean").write_text("import A\ndef b := missingB\n")
        (self.root / "C.lean").write_text("def c := missingC\n")
        self.git(self.root, "add", "B.lean", "C.lean")
        self.git(self.root, "commit", "-qm", "resumed dependency fixture")
        head = self.git(self.root, "rev-parse", "HEAD")
        self.scope["selected_modules"]["C"] = "C.lean"
        self.scope["default_modules"]["C"] = "C.lean"
        with bump_tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = head
            state["config"]["scope"] = deepcopy(self.scope)
            state["tasks"] = {}
        errors = [self.error("A.lean"), {**self.error("B.lean"), "line": 2}, self.error("C.lean")]
        self.controller.refresh(self.build_result(errors))
        state = self.controller.state()
        ids = {t["files"][0]: tid for tid, t in state["tasks"].items() if len(t["files"]) == 1}
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][ids["A.lean"]]["status"] = "complete"
        self.controller.resume_checkpoint = True
        gate = asyncio.Event()
        saw_stream = asyncio.Event()
        stop = {"requested": False}
        dispatches = []
        async def streamed_build(*, stream=False):
            await self.controller.diagnostics_queue.put(("stdout", "error: B.lean:2:1: unknown identifier\n"))
            saw_stream.set()
            await gate.wait()
            return self.authoritative_build_result(errors)
        launch = self.controller.launch_ready
        def capture_launch():
            launch()
            dispatches.append({name: row["task_id"] for name, row in self.controller.workers.items()})
            if self.controller.initial_build is None:
                stop["requested"] = True
        with patch.object(self.controller, "prepare", new=AsyncMock()), \
             patch.object(self.controller, "build", side_effect=streamed_build), \
             patch.object(self.controller, "worker", new=AsyncMock()), \
             patch.object(self.controller, "launch_ready", side_effect=capture_launch), \
             patch.object(bump_workflow, "stopped", side_effect=lambda _: stop["requested"]):
            running = asyncio.create_task(self.controller.run())
            await saw_stream.wait()
            await asyncio.sleep(0.25)
            self.assertEqual(dispatches, [])
            self.assertEqual(self.controller.state()["tasks"][ids["B.lean"]]["attempts"], 0)
            gate.set()
            self.assertFalse(await running)
        self.assertEqual(set(dispatches[0].values()), {ids["A.lean"], ids["C.lean"]})
        self.assertEqual(self.controller.state()["tasks"][ids["B.lean"]]["attempts"], 0)
        self.assertIn(ids["A.lean"], self.controller.state()["tasks"][ids["B.lean"]]["dependencies"])

    async def test_resumed_stale_initial_build_fails_closed_before_dispatch(self):
        self.controller.resume_checkpoint = True
        bad = self.authoritative_build_result()
        bad["source_identity"]["main_sha"] = "stale"
        with patch.object(self.controller, "prepare", new=AsyncMock()), \
             patch.object(self.controller, "build", new=AsyncMock(return_value=bad)), \
             patch.object(self.controller, "launch_ready") as launch:
            with self.assertRaisesRegex(ValueError, "current authoritative initial build"):
                await self.controller.run()
            launch.assert_not_called()

    async def test_existing_checkpoint_enables_initial_dispatch_barrier(self):
        from unity import bump_processes
        directory = self.paths.unity / "bump"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "migration.json").write_text(json.dumps({"run_id": "test", "target_version": "v4.34.1"}))
        controller = SimpleNamespace(run=AsyncMock(return_value=False))
        with patch.object(bump_processes, "assert_closed"), \
             patch.object(bump_workflow, "Controller", return_value=controller) as factory, \
             patch.object(bump_workflow, "phase"), \
             patch.object(bump_verification, "persist_report"):
            self.assertFalse(await bump_workflow.run(self.paths, self.roster, setup={"phase": "ready"}))
        self.assertIs(factory.call_args.kwargs["resume_checkpoint"], True)

    async def test_empty_configuration_assignment_is_never_dispatched(self):
        tid = "configuration-empty"
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"] = {tid: bump_tasks._task(tid, "configuration", [], [])}
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertFalse(self.controller.workers)
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 0)
            with bump_tasks.transaction(self.paths.forum) as state:
                state["tasks"][tid]["files"] = ["lakefile.lean"]
            self.controller.launch_ready()
            record = next(iter(self.controller.workers.values()))
            self.assertEqual(record["task_id"], tid)
            await record["task"]
            await self.controller.reap_workers()

    async def test_saved_empty_configuration_waits_for_authoritative_reconciliation(self):
        tid = self.exhausted_configuration_task()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid].update(files=[], attempts=2)
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertFalse(self.controller.workers)
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 2)
            self.controller.refresh(self.authoritative_build_result(), authoritative_full_build=True)
            state = self.controller.state()
            self.assertEqual(state["tasks"][tid]["status"], "complete")
            self.assertEqual(state["tasks"][tid]["attempts"], 2)
            self.controller.launch_ready()
            records = list(self.controller.workers.values())
            self.assertTrue(records)
            self.assertTrue(all(self.controller.state()["tasks"][row["task_id"]]["kind"] == "repair" for row in records))
            await asyncio.gather(*(row["task"] for row in records))
            await self.controller.reap_workers()

    async def test_saved_empty_configuration_receives_files_when_error_persists(self):
        tid = self.exhausted_configuration_task()
        (self.root / "lakefile.toml").write_text('name = "Fixture"\n')
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid].update(files=[], attempts=1)
        environment = {"path": None, "line": None, "severity": "error", "kind": "environment",
                       "message": "error: A.lean: bad import 'Old.Module'"}
        self.controller.refresh(self.authoritative_build_result([environment]), authoritative_full_build=True)
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["status"], "pending")
        self.assertEqual(state["tasks"][tid]["files"], ["lakefile.toml"])
        self.assertEqual(state["tasks"][tid]["attempts"], 1)
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            record = next(iter(self.controller.workers.values()))
            self.assertEqual(record["task_id"], tid)
            await record["task"]
            await self.controller.reap_workers()

    async def test_scheduler_waits_for_failed_import_through_clean_bridge(self):
        (self.root / "Bridge.lean").write_text("import A\ndef bridge := a\n")
        (self.root / "B.lean").write_text("import Bridge\ndef b := bridge\n")
        self.git(self.root, "add", "Bridge.lean", "B.lean")
        self.git(self.root, "commit", "-qm", "transitive imports fixture")
        head = self.git(self.root, "rev-parse", "HEAD")
        self.scope["selected_modules"]["Bridge"] = "Bridge.lean"
        with bump_tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = head
            state["tasks"] = {}
            state["config"]["scope"] = deepcopy(self.scope)
        errors = [self.error("A.lean"), {**self.error("B.lean"), "line": 2}]
        self.controller.refresh(self.build_result(errors))
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            records = list(self.controller.workers.values())
            self.assertEqual(len(records), 1)
            state = self.controller.state()
            self.assertEqual(state["tasks"][records[0]["task_id"]]["files"], ["A.lean"])
            await records[0]["task"]
            await self.controller.reap_workers()
            with bump_tasks.transaction(self.paths.forum) as state:
                state["tasks"][records[0]["task_id"]]["status"] = "complete"
            self.controller.refresh(self.build_result([errors[1]], checked=["A.lean"]))
            self.controller.launch_ready()
            next_record = next(iter(self.controller.workers.values()))
            self.assertEqual(self.controller.state()["tasks"][next_record["task_id"]]["files"], ["B.lean"])
            await next_record["task"]
            await self.controller.reap_workers()

    def orphaned_repair_fixture(self):
        state = self.controller.state()
        tid = next(tid for tid, task in state["tasks"].items() if task["files"] == ["A.lean"])
        old_chunk = state["tasks"][tid]["chunk_ids"][0]
        (self.root / "A.lean").write_text("def replacement := missingA\n")
        self.git(self.root, "add", "A.lean")
        self.git(self.root, "commit", "-qm", "replace old declaration")
        head = self.git(self.root, "rev-parse", "HEAD")
        with bump_tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = head
            state["tasks"][tid]["attempts"] = 3
        self.controller.refresh(self.build_result([self.error("B.lean")]))
        self.assertNotIn(old_chunk, self.controller.state()["chunks"])
        return tid

    def targeted_orphan_build(self, diagnostics=(), checked=()):
        result = self.authoritative_build_result(list(diagnostics), checked=checked)
        result["command"] = ["lake", "--rehash", "build", "+A"]
        result["log_ref"] = {"artifact_id": "targeted-orphan-log"}
        return result

    async def test_authoritative_failed_build_skips_only_matching_orphan_recheck(self):
        tid = self.orphaned_repair_fixture()
        error = self.error("A.lean")
        full = self.authoritative_build_result([error])
        full.update(attempted_files=["A.lean"], log_ref={"artifact_id": "full-failure"})
        self.controller.refresh(full, authoritative_full_build=True)
        state = self.controller.state()
        receipt = deepcopy(state["orphan_failure_receipt"])
        self.assertEqual(receipt["log_ref"]["artifact_id"], "full-failure")
        before = deepcopy(state["tasks"][tid])
        with patch.object(self.controller, "build", new=AsyncMock()) as build:
            self.assertFalse(await self.controller.recheck_orphaned_repairs())
            build.assert_not_awaited()
        self.assertEqual(self.controller.state()["tasks"][tid], before)

        invalid = {}
        for name in ("sha", "config", "file_hash", "commands", "attempts", "no_error", "unmapped", "pathless", "missing_file"):
            invalid[name] = deepcopy(receipt)
        invalid["sha"]["source_identity"]["main_sha"] = "old"
        invalid["config"]["config_revision"] += 1
        invalid["file_hash"]["source_identity"]["files"]["A.lean"] = "old"
        invalid["commands"]["commands"] = [["lake", "build", "+A"]]
        invalid["attempts"]["attempted_files"] = []
        invalid["no_error"]["errors"] = []
        invalid["unmapped"]["unmapped"] = [{"path": None}]
        invalid["pathless"]["errors"].append({"path": None})
        del invalid["missing_file"]["source_identity"]["files"]["A.lean"]
        stale_target = self.targeted_orphan_build()
        stale_target["source_identity"]["main_sha"] = "stale"
        for name, bad in invalid.items():
            with self.subTest(name=name):
                with bump_tasks.transaction(self.paths.forum) as state:
                    state["orphan_failure_receipt"] = bad
                self.controller.orphan_checks.clear()
                with patch.object(self.controller, "build", new=AsyncMock(return_value=stale_target)) as build:
                    self.assertTrue(await self.controller.recheck_orphaned_repairs())
                    build.assert_awaited_once_with(modules=["A"])
                self.assertEqual(self.controller.state()["tasks"][tid], before)

    async def test_non_authoritative_or_partial_build_never_creates_failure_receipt(self):
        tid = self.orphaned_repair_fixture()
        full = self.authoritative_build_result([self.error("A.lean")])
        full["attempted_files"] = ["A.lean"]
        for result, authoritative in ((full, False), ({**full, "command": ["lake", "build", "+A"]}, True)):
            self.controller.refresh(result, authoritative_full_build=authoritative)
            self.assertIsNone(self.controller.state().get("orphan_failure_receipt"))
            self.controller.orphan_checks.clear()
            with patch.object(self.controller, "build", new=AsyncMock(return_value=self.targeted_orphan_build())) as build:
                self.assertTrue(await self.controller.recheck_orphaned_repairs())
                build.assert_awaited_once_with(modules=["A"])
            self.assertEqual(self.controller.state()["tasks"][tid]["status"], "pending")

    async def test_each_orphan_file_must_have_its_own_mapped_failure(self):
        tid = self.orphaned_repair_fixture()
        full = self.authoritative_build_result([self.error("A.lean")])
        full["attempted_files"] = ["A.lean", "B.lean"]
        self.controller.refresh(full, authoritative_full_build=True)
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["files"] = ["A.lean", "B.lean"]
            state["tasks"][tid]["dependencies"] = []
        stale = self.targeted_orphan_build()
        stale["source_identity"]["main_sha"] = "old"
        with patch.object(self.controller, "build", new=AsyncMock(return_value=stale)) as build:
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            build.assert_awaited_once_with(modules=["A", "B"])
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "pending")

    async def test_rename_failure_receipt_uses_current_destination(self):
        tid, current = self.renamed_command_fixture()
        error = {**self.error("New.lean"), "kind": "import", "line": None}
        full = self.authoritative_build_result([error])
        full["attempted_files"] = ["New.lean"]
        self.controller.refresh(full, authoritative_full_build=True)
        before = deepcopy(self.controller.state()["tasks"][tid])
        with patch.object(self.controller, "build", new=AsyncMock()) as build:
            self.assertFalse(await self.controller.recheck_orphaned_repairs())
            build.assert_not_awaited()
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid], before)
        self.assertEqual(state["tasks"][current]["status"], "pending")

    def renamed_command_fixture(self):
        self.git(self.root, "mv", "A.lean", "New.lean")
        self.git(self.root, "commit", "-qm", "accepted module rename")
        merged = self.git(self.root, "rev-parse", "HEAD")
        self.scope["selected_modules"] = {"New": "New.lean", "B": "B.lean"}
        self.scope["default_modules"] = {"New": "New.lean", "B": "B.lean"}
        old = "command-old-import"
        current = "command-new-import"
        diagnostic = {"id": "old-import", "path": "A.lean", "kind": "import", "line": None,
                      "severity": "error", "message": "bad import 'Old'"}
        with bump_tasks.transaction(self.paths.forum) as state:
            state["main_sha"] = merged
            state["config"].update(scope=deepcopy(self.scope), revision=4, modules=["New"])
            state["tasks"] = {
                old: {**bump_tasks._task(old, "command", [], ["A.lean", "New.lean"]),
                      "attempts": 3, "diagnostics": [diagnostic]},
                current: {**bump_tasks._task(current, "command", [], ["New.lean"]),
                          "diagnostics": [{**diagnostic, "id": "new-import", "path": "New.lean"}]},
                "config": {**bump_tasks._task("config", "configuration", [], ["A.lean", "New.lean"]),
                           "status": "complete", "revision": 11,
                           "requested_config": {"module_renames": {"A": "New"}}},
            }
            state["candidates"] = {"candidate-move": {"candidate_id": "candidate-move", "task_id": "config",
                "task_revision": 11, "status": "merged", "merged_sha": merged}}
        return old, current

    def renamed_target_build(self, diagnostics=(), checked=()):
        result = self.authoritative_build_result(list(diagnostics), checked=checked)
        result["command"] = ["lake", "--rehash", "build", "+New"]
        result["log_ref"] = {"artifact_id": "renamed-target-log"}
        return result

    async def test_accepted_move_rechecks_destination_and_preserves_other_task(self):
        old, current = self.renamed_command_fixture()
        good = self.renamed_target_build(checked=["New.lean"])
        with patch.object(self.controller, "build", new=AsyncMock(return_value=good)) as build, \
                patch.object(self.controller, "worker", new=AsyncMock()):
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            build.assert_awaited_once_with(modules=["New"])
            state = self.controller.state()
            self.assertEqual(state["tasks"][old]["status"], "complete")
            self.assertEqual(state["tasks"][old]["attempts"], 3)
            self.assertEqual(state["tasks"][current]["status"], "pending")
            self.assertEqual(state["tasks"][current]["diagnostics"][0]["id"], "new-import")
            event = next(e for e in state["events"] if e["kind"] == "renamed_command_revalidated")
            self.assertEqual(event["diagnostics"][0]["id"], "old-import")
            self.assertEqual(event["aliases"]["A.lean"]["candidate_id"], "candidate-move")
            self.assertEqual(event["log_ref"]["artifact_id"], "renamed-target-log")

    async def test_rename_rechecks_do_not_share_cached_incomplete_orphan_check(self):
        old, _ = self.renamed_command_fixture()
        other = "command-old-import-2"
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][other] = {**deepcopy(state["tasks"][old]), "task_id": other, "attempts": 4}
            state["tasks"]["a-orphan"] = {
                **bump_tasks._task("a-orphan", "repair", ["missing-chunk"], ["New.lean"]),
                "diagnostics": [{**self.error("New.lean"), "id": "orphan-error"}],
            }
        unchecked = self.renamed_target_build()
        good = self.renamed_target_build(checked=["New.lean"])
        with patch.object(self.controller, "build", new=AsyncMock(side_effect=[unchecked, good, good])) as build:
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            self.assertEqual(build.await_count, 3)
        state = self.controller.state()
        self.assertEqual(state["tasks"]["a-orphan"]["status"], "complete")
        for tid, attempts in ((old, 3), (other, 4)):
            self.assertEqual(state["tasks"][tid]["status"], "complete")
            self.assertEqual(state["tasks"][tid]["attempts"], attempts)
        self.assertEqual(len([e for e in state["events"] if e["kind"] == "renamed_command_revalidated"]), 2)

    async def test_failed_renamed_target_does_not_close_or_dispatch_old_task(self):
        old, _ = self.renamed_command_fixture()
        error = {**self.error("New.lean"), "kind": "import"}
        result = self.renamed_target_build([error])
        with patch.object(self.controller, "build", new=AsyncMock(return_value=result)), \
                patch.object(self.controller, "worker", new=AsyncMock()):
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            self.assertFalse(await self.controller.recheck_orphaned_repairs())
            self.controller.launch_ready()
            self.assertNotIn(old, [r["task_id"] for r in self.controller.workers.values()])
            self.assertEqual(self.controller.state()["tasks"][old]["status"], "pending")
            self.assertEqual(self.controller.state()["tasks"][old]["attempts"], 3)
            await asyncio.gather(*(r["task"] for r in self.controller.workers.values()))
            await self.controller.reap_workers()

    async def test_renamed_target_requires_checked_file_and_current_source_identity(self):
        old, _ = self.renamed_command_fixture()
        unchecked = self.renamed_target_build()
        stale = self.renamed_target_build(checked=["New.lean"])
        stale["source_identity"]["main_sha"] = "old"
        for result in (unchecked, stale):
            self.controller.orphan_checks.clear()
            with patch.object(self.controller, "build", new=AsyncMock(return_value=result)):
                self.assertTrue(await self.controller.recheck_orphaned_repairs())
            self.assertEqual(self.controller.state()["tasks"][old]["status"], "pending")
            self.assertEqual(self.controller.state()["tasks"][old]["attempts"], 3)

    async def test_renamed_target_rejects_changed_provenance_during_build(self):
        old, _ = self.renamed_command_fixture()
        result = self.renamed_target_build(checked=["New.lean"])
        async def build(**kwargs):
            with bump_tasks.transaction(self.paths.forum) as state:
                state["candidates"]["candidate-move"]["status"] = "rejected"
            return result
        with patch.object(self.controller, "build", new=build):
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
        self.assertEqual(self.controller.state()["tasks"][old]["status"], "pending")

    async def test_missing_path_without_accepted_move_never_rechecked_or_dispatched(self):
        for invalid in ("unmerged", "different_revision", "wrong_mapping", "no_mapping"):
            with self.subTest(invalid=invalid):
                old, _ = self.renamed_command_fixture() if invalid == "unmerged" else ("command-old-import", "command-new-import")
                with bump_tasks.transaction(self.paths.forum) as state:
                    c, t = state["candidates"]["candidate-move"], state["tasks"]["config"]
                    c["status"], c["task_revision"] = "merged", 11
                    t["requested_config"]["module_renames"] = {"A": "New"}
                    if invalid == "unmerged": c["status"] = "rejected"
                    if invalid == "different_revision": c["task_revision"] = 10
                    if invalid == "wrong_mapping": t["requested_config"]["module_renames"] = {"A": "B"}
                    if invalid == "no_mapping": t["requested_config"]["module_renames"] = {}
                with patch.object(self.controller, "build", new=AsyncMock()) as build, \
                        patch.object(self.controller, "worker", new=AsyncMock()):
                    self.assertFalse(await self.controller.recheck_orphaned_repairs())
                    build.assert_not_awaited()
                    self.controller.launch_ready()
                    self.assertNotIn(old, [r["task_id"] for r in self.controller.workers.values()])
                    self.assertEqual(self.controller.state()["tasks"][old]["status"], "pending")
                    await asyncio.gather(*(r["task"] for r in self.controller.workers.values()))
                    await self.controller.reap_workers()

    async def test_missing_rename_destination_never_rechecked_or_dispatched(self):
        old, _ = self.renamed_command_fixture()
        (self.root / "New.lean").unlink()
        with patch.object(self.controller, "build", new=AsyncMock()) as build, \
                patch.object(self.controller, "worker", new=AsyncMock()):
            self.assertFalse(await self.controller.recheck_orphaned_repairs())
            build.assert_not_awaited()
            self.controller.launch_ready()
            self.assertNotIn(old, [r["task_id"] for r in self.controller.workers.values()])
            self.assertEqual(self.controller.state()["tasks"][old]["status"], "pending")
            self.assertEqual(self.controller.state()["tasks"][old]["attempts"], 3)
            await asyncio.gather(*(r["task"] for r in self.controller.workers.values()))
            await self.controller.reap_workers()

    async def test_orphaned_chunk_rechecked_by_controller_without_worker_attempt(self):
        tid = self.orphaned_repair_fixture()
        good = self.targeted_orphan_build(checked=["A.lean"])
        with patch.object(self.controller, "build", new=AsyncMock(return_value=good)) as build, \
             patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertNotIn(tid, [row["task_id"] for row in self.controller.workers.values()])
            records = list(self.controller.workers.values())
            await asyncio.gather(*(row["task"] for row in records))
            await self.controller.reap_workers()
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            build.assert_awaited_once_with(modules=["A"])
            state = self.controller.state()
            self.assertEqual(state["tasks"][tid]["status"], "complete")
            self.assertEqual(state["tasks"][tid]["attempts"], 3)
            event = next(row for row in state["events"] if row["kind"] == "orphan_revalidated")
            self.assertEqual(event["task_id"], tid)
            self.assertEqual(event["log_ref"], {"artifact_id": "targeted-orphan-log"})
            self.assertNotIn("A.lean", state["last_build"].get("checked_files", []))

    async def test_one_targeted_build_revalidates_multiple_orphans_in_same_file(self):
        tid = self.orphaned_repair_fixture()
        second = "repair-old-second"
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][second] = {**bump_tasks._task(second, "repair", ["missing-old-chunk"], ["A.lean"]),
                "attempts": 4, "diagnostics": [{**self.error("A.lean"), "id": "old-second"}]}
        good = self.targeted_orphan_build(checked=["A.lean"])
        with patch.object(self.controller, "build", new=AsyncMock(return_value=good)) as build:
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            self.assertFalse(await self.controller.recheck_orphaned_repairs())
            build.assert_awaited_once_with(modules=["A"])
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        self.assertEqual(state["tasks"][second]["status"], "complete")
        self.assertEqual(state["tasks"][second]["attempts"], 4)
        self.assertEqual(len([e for e in state["events"] if e["kind"] == "orphan_revalidated"]), 2)

    async def test_command_orphan_with_import_diagnostic_requires_positive_target_check(self):
        tid = self.orphaned_repair_fixture()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["kind"] = "command"
            state["tasks"][tid]["diagnostics"] = [{"id": "old-import", "path": "A.lean", "kind": "import",
                                                    "line": None, "severity": "error", "message": "bad import"}]
        good = self.targeted_orphan_build(checked=["A.lean"])
        with patch.object(self.controller, "build", new=AsyncMock(return_value=good)):
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        event = next(e for e in state["events"] if e["kind"] == "orphan_revalidated" and e["task_id"] == tid)
        self.assertEqual(event["resolved_diagnostic_ids"], ["old-import"])

    async def test_failed_orphan_recheck_schedules_new_diagnostic_and_does_not_loop(self):
        tid = self.orphaned_repair_fixture()
        error = {**self.error("A.lean"), "message": "replacement still fails"}
        failed = self.targeted_orphan_build([error])
        with patch.object(self.controller, "build", new=AsyncMock(return_value=failed)) as build, \
             patch.object(self.controller, "worker", new=AsyncMock()):
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
            self.assertFalse(await self.controller.recheck_orphaned_repairs())
            build.assert_awaited_once()
            state = self.controller.state()
            self.assertEqual(state["tasks"][tid]["status"], "pending")
            self.assertEqual(state["tasks"][tid]["attempts"], 3)
            event = next(row for row in state["events"] if row["kind"] == "orphan_recheck_incomplete")
            self.assertEqual(event["log_ref"], {"artifact_id": "targeted-orphan-log"})
            repair = next(task for key, task in state["tasks"].items()
                          if key != tid and task["files"] == ["A.lean"] and task["kind"] == "repair")
            self.assertNotIn(tid, repair["dependencies"])
            self.assertEqual(repair["diagnostics"][0]["message"], "replacement still fails")
            self.controller.launch_ready()
            self.assertIn(repair["task_id"], [row["task_id"] for row in self.controller.workers.values()])
            records = list(self.controller.workers.values())
            await asyncio.gather(*(row["task"] for row in records))
            await self.controller.reap_workers()

    async def test_orphan_recheck_rejects_stale_build_identity(self):
        tid = self.orphaned_repair_fixture()
        good = self.targeted_orphan_build(checked=["A.lean"])
        stale = {**good, "source_identity": {**good["source_identity"], "main_sha": "old"}}
        with patch.object(self.controller, "build", new=AsyncMock(return_value=stale)):
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "pending")
        self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 3)
        self.assertFalse(any(e["kind"] == "orphan_revalidated" for e in self.controller.state()["events"]))

    async def test_missing_inventory_chunk_without_source_change_is_not_revalidated(self):
        state = self.controller.state()
        tid = next(tid for tid, task in state["tasks"].items() if task["files"] == ["A.lean"])
        cid = state["tasks"][tid]["chunk_ids"][0]
        with bump_tasks.transaction(self.paths.forum) as state:
            del state["chunks"][cid]
        with patch.object(self.controller, "build", new=AsyncMock()) as build:
            self.assertFalse(await self.controller.recheck_orphaned_repairs())
            build.assert_not_awaited()
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "pending")

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

    async def test_identical_later_error_merges_owned_fix_with_hash_bound_candidate(self):
        (self.root / "A.lean").write_text("def a := missingA\ndef later := missingLater\n")
        self.git(self.root, "add", "A.lean")
        self.git(self.root, "commit", "-qm", "same-message fixture")
        self.base = self.git(self.root, "rev-parse", "HEAD")
        with bump_tasks.transaction(self.paths.forum) as state:
            state.update(base_sha=self.base, main_sha=self.base, tasks={})
        old = {**self.error("A.lean"), "id": "old-owned", "line": 1, "message": "unsolved goals"}
        later = {**self.error("A.lean"), "id": "later", "line": 2, "message": "unsolved goals"}
        self.controller.refresh(self.build_result([old, later]))
        state = self.controller.state()
        tid = next(key for key, task in state["tasks"].items() if old in task.get("diagnostics", []))
        bump_tasks.claim(self.paths.forum, "Alice", tid)
        worker = self.worktree(self.paths, "Alice", tid)
        bump_tasks.configure_assignment(self.paths.forum, "Alice", tid, worker, self.base)
        (worker / "A.lean").write_text("def a := 1\ndef later := missingLater\n")
        forum.configure(self.paths.forum, self.root)
        candidate = forum.submit_repair("Alice", tid, "repair first declaration")
        self.active_worker(tid, worker)
        import hashlib
        build = {**self.build_result([later]), "source_identity": {"files": {
            "A.lean": hashlib.sha256((worker / "A.lean").read_bytes()).hexdigest()}}}
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
             patch.object(bump_verification, "build", return_value=build) as compiler, \
             patch.object(self.controller, "build", new=AsyncMock(return_value=build)):
            compiler.return_value = {**build, "source_identity": {"files": {"A.lean": "stale"}}}
            self.assertFalse(self.controller.integrate(candidate)["accepted"])
            compiler.return_value = build
            self.assertTrue(await self.controller.integrate_ready())
        state = self.controller.state()
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        remaining = [task for key, task in state["tasks"].items() if key != tid and later in task.get("diagnostics", [])]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["status"], "pending")
        self.assertFalse(state["leases"])

    async def test_mixed_partial_merge_leaves_later_error_and_missing_obligation(self):
        (self.root / "A.lean").write_text("def a := missingA\ndef later := missingLater\n")
        self.git(self.root, "add", "A.lean")
        self.git(self.root, "commit", "-qm", "same-message fixture")
        self.base = self.git(self.root, "rev-parse", "HEAD")
        with bump_tasks.transaction(self.paths.forum) as state:
            state.update(base_sha=self.base, main_sha=self.base, tasks={})
        old = {**self.error("A.lean"), "id": "old-owned", "line": 1, "message": "unsolved goals"}
        later = {**self.error("A.lean"), "id": "later", "line": 2, "message": "unsolved goals"}
        self.controller.refresh(self.build_result([old, later]))
        state = self.controller.state()
        tid = next(key for key, task in state["tasks"].items() if old in task.get("diagnostics", []))
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["chunk_ids"].append("inherited-missing")
        bump_tasks.claim(self.paths.forum, "Alice", tid)
        worker = self.worktree(self.paths, "Alice", tid)
        bump_tasks.configure_assignment(self.paths.forum, "Alice", tid, worker, self.base)
        (worker / "A.lean").write_text("def a := 1\ndef later := missingLater\n")
        forum.configure(self.paths.forum, self.root)
        candidate = forum.submit_repair("Alice", tid, "repair first declaration")
        self.active_worker(tid, worker)
        import hashlib
        build = {**self.build_result([later]), "source_identity": {"files": {
            "A.lean": hashlib.sha256((worker / "A.lean").read_bytes()).hexdigest()}}}
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
             patch.object(bump_verification, "build", return_value=build) as compiler, \
             patch.object(self.controller, "build", new=AsyncMock(return_value=build)):
            compiler.return_value = {**build, "source_identity": {"files": {"A.lean": "stale"}}}
            self.assertFalse(self.controller.integrate(candidate)["accepted"])
            compiler.return_value = build
            self.assertTrue(await self.controller.integrate_ready())
        state = self.controller.state()
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        remaining = [task for key, task in state["tasks"].items() if key != tid and later in task.get("diagnostics", [])]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["status"], "pending")
        orphan = next(task for task in state["tasks"].values() if task.get("orphaned_from") == tid)
        self.assertEqual(orphan["chunk_ids"], ["inherited-missing"])
        self.assertEqual(orphan["status"], "pending")
        self.assertNotIn("inherited-missing", state["tasks"][tid]["chunk_ids"])
        self.assertFalse(state["leases"])

    async def test_resolved_import_merges_and_exposed_declaration_remains_open(self):
        (self.root / "A.lean").write_text("import Old.Missing\ndef a := missingA\n")
        self.git(self.root, "add", "A.lean")
        self.git(self.root, "commit", "-qm", "import blocker fixture")
        self.base = self.git(self.root, "rev-parse", "HEAD")
        with bump_tasks.transaction(self.paths.forum) as state:
            state.update(base_sha=self.base, main_sha=self.base, tasks={})
        old = {"id": "old-import", "path": "A.lean", "line": None, "column": None,
               "severity": "error", "kind": "import", "message": "bad import 'Old.Missing'"}
        self.controller.refresh(self.build_result([old]))
        state = self.controller.state()
        tid = next(key for key, task in state["tasks"].items() if task["files"] == ["A.lean"])
        self.assertEqual(state["tasks"][tid]["kind"], "command")
        self.assertEqual(state["tasks"][tid]["chunk_ids"], [])
        bump_tasks.claim(self.paths.forum, "Alice", tid)
        worker = self.worktree(self.paths, "Alice", tid)
        bump_tasks.configure_assignment(self.paths.forum, "Alice", tid, worker, self.base)
        (worker / "A.lean").write_text("-- fixed import\ndef a := missingA\n")
        forum.configure(self.paths.forum, self.root)
        candidate = forum.submit_repair("Alice", tid, "fixed import")
        self.active_worker(tid, worker)
        error = {**self.error("A.lean"), "line": 2, "message": "declaration still fails"}
        import hashlib
        result = {**self.build_result([error]), "source_identity": {"files": {
            "A.lean": hashlib.sha256((worker / "A.lean").read_bytes()).hexdigest()}}}
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
             patch.object(bump_verification, "build", return_value=result) as compiler, \
             patch.object(self.controller, "build", new=AsyncMock(return_value=result)):
            compiler.return_value = {**result, "source_identity": {"files": {"A.lean": "stale"}}}
            self.assertFalse(self.controller.integrate(candidate)["accepted"])
            compiler.return_value = result
            self.assertTrue(await self.controller.integrate_ready())
        state = self.controller.state()
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        repairs = [task for task in state["tasks"].values() if task["kind"] == "repair" and task["files"] == ["A.lean"]]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["status"], "pending")
        self.assertEqual(repairs[0]["diagnostics"][0]["message"], "declaration still fails")
        self.assertTrue(all(state["chunks"][cid]["status"] == "needs_repair" for cid in repairs[0]["chunk_ids"]))
        self.assertFalse(state["leases"])

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

    async def test_mixed_candidate_merge_retains_missing_chunks_as_recheckable_orphan(self):
        state = self.controller.state()
        tid = next(key for key, task in state["tasks"].items() if task["files"] == ["A.lean"])
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["chunk_ids"].append("inherited-missing")
            state["chunk_attempts"]["inherited-missing"] = 2
        tid, candidate, worker = self.submit()
        self.active_worker(tid, worker)
        import hashlib
        result = {**self.build_result([], checked=["A.lean"]), "source_identity": {"files": {
            "A.lean": hashlib.sha256((worker / "A.lean").read_bytes()).hexdigest()}}}
        with patch.object(bump_verification, "audit_sources", return_value={"issues": []}), \
             patch.object(bump_verification, "build", return_value=result), \
             patch.object(self.controller, "build", new=AsyncMock(return_value=result)):
            self.assertTrue(await self.controller.integrate_ready())
        state = self.controller.state()
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(state["tasks"][tid]["status"], "complete")
        self.assertNotIn("inherited-missing", state["tasks"][tid]["chunk_ids"])
        orphan = next(t for t in state["tasks"].values() if t.get("orphaned_from") == tid)
        self.assertEqual(orphan["chunk_ids"], ["inherited-missing"])
        self.assertEqual(orphan["status"], "pending")
        self.assertEqual(orphan["attempts"], 1)
        self.assertEqual(state["chunk_attempts"]["inherited-missing"], 3)
        self.assertTrue(bump_tasks.orphaned_repair(orphan, state["chunks"]))
        target = self.authoritative_build_result([], checked=["A.lean"])
        target["command"] = ["lake", "--rehash", "build", "+A"]
        with patch.object(self.controller, "build", new=AsyncMock(return_value=target)):
            self.assertTrue(await self.controller.recheck_orphaned_repairs())
        self.assertEqual(self.controller.state()["tasks"][orphan["task_id"]]["status"], "complete")

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

    def submission_followup_fixture(self):
        state = self.controller.state()
        tid = next(key for key, task in state["tasks"].items() if task["files"] == ["A.lean"])
        bump_tasks.claim(self.paths.forum, "Alice", tid)
        tree = self.worktree(self.paths, "Alice", tid)
        bump_tasks.configure_assignment(self.paths.forum, "Alice", tid, tree, self.base)
        forum.configure(self.paths.forum, self.root)
        return tid, tree

    async def test_one_submission_followup_keeps_attempt_tree_and_real_receipt(self):
        tid, tree = self.submission_followup_fixture()
        calls = []
        async def fake_spawn(agent, system, instruction, cwd, servers, **kwargs):
            calls.append((instruction, cwd, kwargs["env_overrides"]["UNITY_BUMP_WORKER_SESSION"]))
            if len(calls) == 1:
                (tree / "A.lean").write_text("def a := 1\n")
                return "Submitted the repair"  # No tool receipt was actually created.
            self.assertIn("submit_repair", instruction)
            self.assertIn("candidate_id", instruction)
            receipt = forum.submit_repair("Alice", tid, "follow-up submission")
            return receipt["candidate_id"]
        with patch.object(bump_workflow, "spawn", side_effect=fake_spawn):
            answer = await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", asyncio.Event())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertNotEqual(calls[0][2], calls[1][2])
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["attempts"], 1)
        self.assertEqual(state["candidates"][answer]["status"], "submitted")
        self.assertEqual(sum(e["kind"] == "submission_followup" for e in state["events"]), 1)

    async def test_followup_once_only_even_if_second_turn_does_not_submit(self):
        tid, tree = self.submission_followup_fixture()
        async def fake_spawn(*args, **kwargs):
            (tree / "A.lean").write_text("def a := 1\n")
            return "no receipt"
        with patch.object(bump_workflow, "spawn", side_effect=fake_spawn) as spawn:
            await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", asyncio.Event())
        self.assertEqual(spawn.await_count, 2)
        self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 1)
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "running")

    async def test_followup_accepts_explicit_scope_revision_with_matching_leases(self):
        tid, tree = self.submission_followup_fixture()
        calls = []
        async def fake_spawn(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                state = self.controller.state()
                bump_tasks.request_scope(self.paths.forum, "Alice", tid, [], ["New.lean"],
                                         "expand source", state["tasks"][tid]["revision"])
                (tree / "New.lean").write_text("def new := 1\n")
            return "finished"
        with patch.object(bump_workflow, "spawn", side_effect=fake_spawn):
            await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", asyncio.Event())
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 1)

    async def test_failed_or_cancelled_spawn_never_triggers_followup(self):
        tid, tree = self.submission_followup_fixture()
        (tree / "A.lean").write_text("def a := 1\n")
        for failure in (asyncio.CancelledError(), SpawnCleanupError("not closed")):
            with self.subTest(error=type(failure).__name__):
                with patch.object(bump_workflow, "spawn", side_effect=failure) as spawn:
                    with self.assertRaises(type(failure)):
                        await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", asyncio.Event())
                    self.assertEqual(spawn.await_count, 1)
        self.assertFalse(any(e["kind"] == "submission_followup" for e in self.controller.state()["events"]))

    async def test_no_submission_followup_for_unrelated_untracked_file(self):
        tid, tree = self.submission_followup_fixture()
        async def fake_spawn(*args, **kwargs):
            (tree / "Scratch.lean").write_text("def scratch := 1\n")
            return "finished"
        with patch.object(bump_workflow, "spawn", side_effect=fake_spawn) as spawn:
            await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", asyncio.Event())
        self.assertEqual(spawn.await_count, 1)
        self.assertFalse(any(e["kind"] == "submission_followup" for e in self.controller.state()["events"]))

    async def test_no_submission_followup_after_yield_or_submission(self):
        tid, tree = self.submission_followup_fixture()
        async def fake_spawn(*args, **kwargs):
            (tree / "A.lean").write_text("def a := 1\n")
            forum.yield_task("Alice", tid, "needs input")
            return "yielded"
        with patch.object(bump_workflow, "spawn", side_effect=fake_spawn) as spawn:
            await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", asyncio.Event())
        self.assertEqual(spawn.await_count, 1)
        self.assertIsNotNone(self.controller.state()["tasks"][tid].get("yield_requested"))

    async def test_no_submission_followup_after_real_receipt(self):
        tid, tree = self.submission_followup_fixture()
        async def fake_spawn(*args, **kwargs):
            (tree / "A.lean").write_text("def a := 1\n")
            return forum.submit_repair("Alice", tid, "submitted")["candidate_id"]
        with patch.object(bump_workflow, "spawn", side_effect=fake_spawn) as spawn:
            await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", asyncio.Event())
        self.assertEqual(spawn.await_count, 1)
        self.assertEqual(self.controller.state()["tasks"][tid]["status"], "submitted")

    async def test_no_submission_followup_after_stop_interrupt_or_stale_assignment(self):
        for mode in ("stop", "interrupt", "stale", "pending_request"):
            with self.subTest(mode=mode):
                tid, tree = self.submission_followup_fixture()
                interrupt = asyncio.Event()
                async def fake_spawn(*args, **kwargs):
                    (tree / "A.lean").write_text("def a := 1\n")
                    if mode == "stop":
                        (self.paths.unity / "stop-requested").touch()
                    elif mode == "interrupt":
                        interrupt.set()
                    elif mode == "stale":
                        with bump_tasks.transaction(self.paths.forum) as state:
                            state["tasks"][tid]["revision"] += 1
                    else:
                        with bump_tasks.transaction(self.paths.forum) as state:
                            state["config_requests"]["configuration-test"] = {"status": "pending"}
                    return "finished"
                with patch.object(bump_workflow, "spawn", side_effect=fake_spawn) as spawn:
                    await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", interrupt)
                self.assertEqual(spawn.await_count, 1)
                (self.paths.unity / "stop-requested").unlink(missing_ok=True)
                if mode == "pending_request":
                    with bump_tasks.transaction(self.paths.forum) as state:
                        state["config_requests"].pop("configuration-test")
                bump_tasks.release(self.paths.forum, "Alice", tid, "test cleanup")

    async def test_followup_detects_local_commit_and_staged_or_untracked_leased_source(self):
        for mode in ("commit", "staged", "untracked"):
            with self.subTest(mode=mode):
                tid, tree = self.submission_followup_fixture()
                if mode == "untracked":
                    state = self.controller.state()
                    bump_tasks.request_scope(self.paths.forum, "Alice", tid, [], ["New.lean"], "new source", state["tasks"][tid]["revision"])
                calls = []
                async def fake_spawn(*args, **kwargs):
                    calls.append(True)
                    if len(calls) == 1:
                        if mode == "untracked":
                            (tree / "New.lean").write_text("def new := 1\n")
                        else:
                            (tree / "A.lean").write_text("def a := 1\n")
                            self.git(tree, "add", "A.lean")
                            if mode == "commit": self.git(tree, "commit", "-qm", "local partial")
                    return "finished"
                with patch.object(bump_workflow, "spawn", side_effect=fake_spawn) as spawn:
                    await self.controller.worker(self.roster.agents[0], tid, tree, "repairing", asyncio.Event())
                self.assertEqual(spawn.await_count, 2)
                bump_tasks.release(self.paths.forum, "Alice", tid, "test cleanup")

    def yielded_assignment(self, *, yielded=True):
        state = self.controller.state()
        tid = next(tid for tid, task in state["tasks"].items() if task["files"] == ["A.lean"])
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"] = {tid: state["tasks"][tid]}
        self.controller.max_attempts = 5
        bump_tasks.claim(self.paths.forum, "Alice", tid, max_attempts=5)
        if yielded:
            forum.configure(self.paths.forum, self.root)
            forum.yield_task("Alice", tid, "need another perspective")
        bump_tasks.release(self.paths.forum, "Alice", tid, "confirmed closed")
        return tid

    def continuation_fixture(self, *, keep_other=False):
        state = self.controller.state()
        tid = next(tid for tid, task in state["tasks"].items() if task["files"] == ["A.lean"])
        with bump_tasks.transaction(self.paths.forum) as state:
            if not keep_other:
                state["tasks"] = {tid: state["tasks"][tid]}
            state["tasks"][tid]["attempts"] = 1
            state["candidates"]["merged"] = {"candidate_id": "merged", "task_id": tid,
                "status": "merged", "author": "Bob", "submitted": "2026-01-02"}
            bump_tasks._event(state, "candidate_submitted", "Bob", task_id=tid, candidate_id="merged")
            bump_tasks._event(state, "release", "Bob", task_id=tid, reason="Recovered integrated repair")
        self.controller.max_attempts = 5
        return tid

    async def finish_continuation_workers(self):
        await asyncio.gather(*(row["task"] for row in self.controller.workers.values()))
        await self.controller.reap_workers()

    async def test_merged_candidate_author_gets_exact_pending_task_without_budget_reset(self):
        tid = self.continuation_fixture()
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Bob"})
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 2)
            self.assertEqual(self.controller.workers["Bob"]["task_id"], tid)
            await self.finish_continuation_workers()

    async def test_preferred_writer_busy_or_absent_does_not_block_other_writer(self):
        tid = self.continuation_fixture()
        for mode in ("locked", "busy", "absent"):
            with self.subTest(mode=mode):
                if mode == "locked": await self.controller.agent_locks["Bob"].acquire()
                if mode == "absent": self.controller.writers = [self.roster.agents[0]]
                if mode == "busy":
                    gate = asyncio.Event()
                    busy = asyncio.create_task(gate.wait())
                    self.controller.workers["Bob"] = {"task": busy}
                with patch.object(self.controller, "worker", new=AsyncMock()):
                    self.controller.launch_ready()
                    self.assertEqual(self.controller.workers["Alice"]["task_id"], tid)
                    if mode == "busy":
                        gate.set()
                        await busy
                        self.controller.workers.pop("Bob")
                    await self.finish_continuation_workers()
                if mode == "locked": self.controller.agent_locks["Bob"].release()
                self.controller.writers = self.roster.agents
                with bump_tasks.transaction(self.paths.forum) as state:
                    # Restore the fixture's prior candidate/attempt boundary for each case.
                    state["events"] = [e for e in state["events"]
                                       if not (e.get("task_id") == tid and e.get("author") == "Alice")]
                    state["tasks"][tid]["attempts"] = 1

    async def test_later_rejection_or_worker_exit_disables_old_preference(self):
        for mode in ("rejected", "submitted", "claim", "failed_release"):
            with self.subTest(mode=mode):
                tid = self.continuation_fixture() if mode == "rejected" else tid
                with bump_tasks.transaction(self.paths.forum) as state:
                    state["candidates"] = {"merged": state["candidates"]["merged"]}
                    state["events"] = [e for e in state["events"] if e.get("task_id") != tid]
                    bump_tasks._event(state, "candidate_submitted", "Bob", task_id=tid, candidate_id="merged")
                    bump_tasks._event(state, "release", "Bob", task_id=tid, reason="Recovered integrated repair")
                    if mode in {"rejected", "submitted"}:
                        state["candidates"]["later"] = {"candidate_id": "later", "task_id": tid,
                            "status": mode, "author": "Bob", "submitted": "2026-01-03"}
                    elif mode == "claim": bump_tasks._event(state, "claim", "Bob", task_id=tid)
                    else: bump_tasks._event(state, "release", "Bob", task_id=tid, reason="Worker failed: Error")
                self.assertIsNone(self.controller.continuation_author(self.controller.state(), self.controller.state()["tasks"][tid]))
                with patch.object(self.controller, "worker", new=AsyncMock()):
                    self.controller.launch_ready()
                    self.assertIn("Alice", self.controller.workers)
                    if mode == "submitted":
                        with bump_tasks.transaction(self.paths.forum) as state:
                            state["candidates"]["later"]["status"] = "rejected"
                    await self.finish_continuation_workers()
                with bump_tasks.transaction(self.paths.forum) as state:
                    state["tasks"][tid]["attempts"] = 1

    async def test_yield_takes_precedence_over_previously_merged_author(self):
        tid = self.continuation_fixture()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["yield_handoff_from"] = "Bob"
            bump_tasks._event(state, "claim", "Bob", task_id=tid)
            bump_tasks._event(state, "yield", "Bob", task_id=tid)
            bump_tasks._event(state, "release", "Bob", task_id=tid, reason="Worker ended without a submitted repair",
                              previous_yield={"reason": "need help"})
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice"})
            await self.finish_continuation_workers()

    async def test_preference_does_not_block_other_task_or_transfer_to_it(self):
        tid = self.continuation_fixture(keep_other=True)
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice", "Bob"})
            self.assertEqual(self.controller.workers["Bob"]["task_id"], tid)
            self.assertNotEqual(self.controller.workers["Alice"]["task_id"], tid)
            await self.finish_continuation_workers()

    async def test_continuation_does_not_override_exhaustion(self):
        tid = self.continuation_fixture()
        self.controller.max_attempts = 1
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
        self.assertFalse(self.controller.workers)
        self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 1)

    def test_continuation_is_exact_task_recent_candidate_and_repair_kind_only(self):
        tid = self.continuation_fixture()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["candidates"]["z"] = {"candidate_id": "z", "task_id": tid, "status": "rejected",
                "author": "Alice", "submitted": "2026-01-02"}
            state["candidates"]["new-other"] = {"candidate_id": "new-other", "task_id": "other",
                "status": "merged", "author": "Alice", "submitted": "2027-01-01"}
        state = self.controller.state()
        self.assertIsNone(self.controller.continuation_author(state, state["tasks"][tid]))
        del state["candidates"]["z"]
        self.assertEqual(self.controller.continuation_author(state, state["tasks"][tid]), "Bob")
        state["tasks"][tid]["kind"] = "configuration"
        self.assertIsNone(self.controller.continuation_author(state, state["tasks"][tid]))

    async def test_explicit_yield_prefers_other_available_writer_and_keeps_attempts(self):
        tid = self.yielded_assignment()
        self.assertEqual(self.controller.state()["tasks"][tid]["yield_handoff_from"], "Alice")
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Bob"})
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 2)
            await self.controller.workers["Bob"]["task"]
            await self.controller.reap_workers()
        self.assertTrue(any(event.get("kind") == "yield" and event.get("task_id") == tid
                            for event in self.controller.state()["events"]))

    async def test_capacity_releases_lease_and_hands_task_to_other_writer(self):
        tid = self.continuation_fixture()
        # Force Alice first; the prior merged Bob preference is unrelated here.
        with bump_tasks.transaction(self.paths.forum) as state:
            state["candidates"] = {}
        async def fail_capacity(agent, *args):
            if agent.name == "Alice":
                raise bump_workflow.BumpProviderCapacityError("Selected model is at capacity")
        with patch.object(self.controller, "worker", side_effect=fail_capacity):
            self.controller.launch_ready()
            record = self.controller.workers["Alice"]
            await asyncio.gather(record["task"], return_exceptions=True)
            old_tree = str(record["tree"])
            await self.controller.reap_workers()
            state = self.controller.state()
            self.assertIsNone(state["tasks"][tid]["owner"])
            self.assertFalse(state["leases"])
            self.assertTrue(self.controller.capacity_cooling("Alice"))
            self.assertTrue(any(e["kind"] == "provider_capacity" and e["worker"] == "Alice" for e in state["events"]))
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Bob"})
            state = self.controller.state()
            self.assertEqual(state["tasks"][tid]["attempts"], 3)
            self.assertIn(old_tree, state["tasks"][tid]["previous_worktrees"])
            await self.controller.workers["Bob"]["task"]
            await self.controller.reap_workers()

    async def test_cooldown_does_not_block_yield_or_successful_continuation_fallback(self):
        tid = self.yielded_assignment()
        self.controller.capacity_cooldowns["Bob"] = bump_workflow.time.monotonic() + 60
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice"})
            await self.controller.workers["Alice"]["task"]
            await self.controller.reap_workers()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["tasks"][tid]["yield_handoff_from"] = None
            state["events"] = [e for e in state["events"] if e.get("task_id") != tid]
            state["candidates"]["m"] = {"candidate_id": "m", "task_id": tid, "author": "Bob",
                                        "status": "merged", "submitted": "2026-01-01"}
            bump_tasks._event(state, "candidate_submitted", "Bob", task_id=tid, candidate_id="m")
            bump_tasks._event(state, "release", "Bob", task_id=tid, reason="Recovered integrated repair")
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice"})
            await self.controller.workers["Alice"]["task"]
            await self.controller.reap_workers()

    async def test_all_cooling_writers_wait_without_declaring_exhaustion(self):
        self.controller.capacity_cooldowns = {agent.name: bump_workflow.time.monotonic() + 30
                                              for agent in self.roster.agents}
        stop = {"requested": False}
        with patch.object(self.controller, "prepare", new=AsyncMock()), \
             patch.object(self.controller, "build", new=AsyncMock(return_value=self.build_result([]))), \
             patch.object(bump_workflow, "stopped", side_effect=lambda _: stop["requested"]):
            running = asyncio.create_task(self.controller.run())
            await asyncio.sleep(0.3)
            self.assertFalse(running.done())
            self.assertFalse(self.controller.workers)
            stop["requested"] = True
            self.assertFalse(await asyncio.wait_for(running, 2))

    async def test_lone_writer_resumes_only_after_cooldown(self):
        tid = self.continuation_fixture()
        self.controller.writers = [self.roster.agents[0]]
        self.controller.capacity_cooldowns["Alice"] = bump_workflow.time.monotonic() + 60
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertFalse(self.controller.workers)
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 1)
            self.controller.capacity_cooldowns["Alice"] = bump_workflow.time.monotonic() - 1
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice"})
            await self.controller.workers["Alice"]["task"]
            await self.controller.reap_workers()

    async def test_cleanup_failure_still_retains_owned_lease(self):
        tid = self.continuation_fixture()
        with bump_tasks.transaction(self.paths.forum) as state:
            state["candidates"] = {}
        with patch.object(self.controller, "worker", new=AsyncMock(side_effect=SpawnCleanupError("not closed"))):
            self.controller.launch_ready()
            await asyncio.gather(self.controller.workers["Alice"]["task"], return_exceptions=True)
            with self.assertRaises(SpawnCleanupError):
                await self.controller.reap_workers()
        state = self.controller.state()
        self.assertEqual(state["tasks"][tid]["owner"], "Alice")
        self.assertEqual(state["leases"]["A.lean"]["task_id"], tid)
        self.assertFalse(self.controller.capacity_cooldowns)
        self.controller.workers.clear()

    async def test_capacity_review_waits_and_retries_only_primary(self):
        snapshot = self.passed_snapshot()
        seen = []
        async def critic(agent, task_id, tree, profile, interrupt):
            seen.append(agent.name)
            if len(seen) == 1:
                raise bump_workflow.BumpProviderCapacityError("selected model is at capacity")
            forum.configure(self.paths.forum, self.root, "critic")
            forum.submit_migration_verdict(agent.name, snapshot["snapshot_id"], True, "offline fixture review")
        with patch.object(bump_workflow, "_CAPACITY_COOLDOWN_SECONDS", 0.05), \
             patch.object(bump_verification, "verify_final", return_value=deepcopy(snapshot)), \
             patch.object(bump_verification, "source_identity", return_value=snapshot["source_identity"]), \
             patch.object(self.controller, "worker", side_effect=critic):
            self.assertTrue(await asyncio.wait_for(self.controller.review(), 3))
        self.assertEqual(seen, ["Alice", "Alice"])
        self.assertTrue(any(e["kind"] == "provider_capacity" and e["task_id"] == "critic"
                            for e in self.controller.state()["events"]))

    async def test_saved_legacy_yield_still_rotates_after_same_writer_was_retried(self):
        tid = self.yielded_assignment()
        # Simulate the old dispatcher immediately reclaiming and stopping the
        # same writer, leaving only the original release/yield history.
        bump_tasks.claim(self.paths.forum, "Alice", tid, max_attempts=5)
        bump_tasks.release(self.paths.forum, "Alice", tid, "interrupted without yield")
        self.assertNotIn("yield_handoff_from", self.controller.state()["tasks"][tid])
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Bob"})
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 3)
            await self.controller.workers["Bob"]["task"]
            await self.controller.reap_workers()

    async def test_stop_without_prior_yield_does_not_change_writer_preference(self):
        tid = self.yielded_assignment(yielded=False)
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice"})
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 2)
            await self.controller.workers["Alice"]["task"]
            await self.controller.reap_workers()

    async def test_single_writer_can_resume_explicitly_yielded_task(self):
        tid = self.yielded_assignment()
        self.controller.writers = [self.roster.agents[0]]
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertEqual(set(self.controller.workers), {"Alice"})
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 2)
            await self.controller.workers["Alice"]["task"]
            await self.controller.reap_workers()

    async def test_yielded_writer_waits_for_busy_teammate(self):
        tid = self.yielded_assignment()
        done = asyncio.Event()
        busy = asyncio.create_task(done.wait())
        self.controller.workers["Bob"] = {"task": busy, "task_id": "unrelated", "interrupt": asyncio.Event(),
                                           "tree": self.root, "agent": self.roster.agents[1]}
        with patch.object(self.controller, "worker", new=AsyncMock()):
            self.controller.launch_ready()
            self.assertNotIn("Alice", self.controller.workers)
            self.assertEqual(self.controller.state()["tasks"][tid]["attempts"], 1)
        done.set()
        await busy
        self.controller.workers.pop("Bob")

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
