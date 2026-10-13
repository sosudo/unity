"""Offline coordination tests: no providers, Lean, Lake or evaluation calls."""
import json
from pathlib import Path
import subprocess

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from unity import bump_tasks as tasks
from unity.forum import bump_workflow_server as forum


def setup_state(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    directory = tmp_path / "forum"
    tasks.initialize(directory, root, run_id="bump-test", base_sha="base", version="v4.34.1")
    return root, directory


def write_source(root, path, source):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(source)


def diagnostic(path="A.lean", line=1):
    return {"path": path, "line": line, "severity": "error", "message": "unknown identifier"}


def prepare_task(tmp_path):
    root, directory = setup_state(tmp_path)
    write_source(root, "A.lean", "def foo := missing\ndef bar := 1\n")
    inventory = tasks.scan_sources(root, ["A.lean"])
    state = tasks.refresh(directory, inventory, [diagnostic()], main_sha="base", config_revision=0)
    tid = next(iter(state["tasks"]))
    return root, directory, tid


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def setup_git_worker(tmp_path):
    root, directory, tid = prepare_task(tmp_path)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Offline Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "add", "A.lean")
    git(root, "commit", "-qm", "initial")
    sha = git(root, "rev-parse", "HEAD")
    with tasks.transaction(directory) as state: state["main_sha"] = sha
    worker = tmp_path / "worker"
    git(root, "worktree", "add", "-qb", "worker", str(worker), sha)
    tasks.claim(directory, "Alice", tid)
    tasks.configure_assignment(directory, "Alice", tid, worker, sha)
    forum.configure(directory, root)
    return root, directory, tid, worker, sha

class WorkflowTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)
        environment = patch.dict(os.environ, {})
        environment.start()
        self.addCleanup(environment.stop)

    def raises(self, exception, match=None):
        return self.assertRaisesRegex(exception, match) if match else self.assertRaises(exception)

    def test_scanner_all_declarations_comments_strings_quotes_and_anonymous(self):
        tmp_path = self.tmp_path
        root, _ = setup_state(tmp_path)
        write_source(root, "A.lean", '''/- nested /- theorem ghost : True := by trivial -/ ignored -/
    namespace «Space Name»
    def «strange name» := "theorem ghost := false"
    instance : Inhabited Nat := ⟨0⟩
    -- theorem commented := true
    theorem actual : True := by trivial
    end «Space Name»
    #exit
    def draft := sorry
    ''')
        inventory = tasks.scan_sources(root, ["A.lean"])
        rows = list(inventory["chunks"].values())
        assert len(rows) == 4
        assert [row["name"] for row in rows] == ["«Space Name».«strange name»", "«Space Name».<anonymous-instance>",
                                                 "«Space Name».actual", "draft"]
        assert rows[-1]["after_exit"] and rows[-1]["status"] == "skipped"
        assert rows[1]["end_line"] < rows[2]["start_line"]

    def test_mutual_group_is_atomic_repair_assignment(self):
        tmp_path = self.tmp_path
        root, directory = setup_state(tmp_path)
        write_source(root, "A.lean", "mutual\ndef even := odd\ndef odd := missing\nend\n")
        inventory = tasks.scan_sources(root, ["A.lean"])
        state = tasks.refresh(directory, inventory, [diagnostic(line=3)], main_sha="base", config_revision=0)
        task = next(iter(state["tasks"].values()))
        assert set(task["chunk_ids"]) == set(inventory["chunks"])
        assert len(state["tasks"]) == 1

    def test_every_chunk_inventory_but_only_error_dispatch_and_no_false_clean(self):
        tmp_path = self.tmp_path
        root, directory, tid = prepare_task(tmp_path)
        initial = tasks.load(directory)
        assert len(initial["chunks"]) == 2 and len(initial["tasks"]) == 1
        inventory = tasks.scan_sources(root, ["A.lean"])
        state = tasks.refresh(directory, inventory, [], main_sha="base", config_revision=0, checked_files=[])
        assert state["tasks"][tid]["status"] == "pending"
        assert not all(c["status"] == "clean" for c in state["chunks"].values())
        state = tasks.refresh(directory, inventory, [], main_sha="base", config_revision=0, checked_files=["A.lean"], complete=True)
        assert all(c["status"] == "clean" for c in state["chunks"].values())
        assert state["tasks"][tid]["status"] == "complete"

    def test_import_dependencies_block_downstream_claim(self):
        tmp_path = self.tmp_path
        root, directory = setup_state(tmp_path)
        write_source(root, "A.lean", "def a := missing\n")
        write_source(root, "B.lean", "import A\ndef b := a\n")
        state = tasks.refresh(directory, tasks.scan_sources(root, ["A.lean", "B.lean"]),
                              [diagnostic(), diagnostic("B.lean", 2)], main_sha="base", config_revision=0)
        by_file = {t["files"][0]: tid for tid, t in state["tasks"].items()}
        assert state["tasks"][by_file["B.lean"]]["dependencies"] == [by_file["A.lean"]]
        with self.raises(ValueError, match="unfinished dependencies"):
            tasks.claim(directory, "Bob", by_file["B.lean"])
        tasks.claim(directory, "Alice", by_file["A.lean"])

    def test_unknown_compiler_location_becomes_explicit_command_work(self):
        tmp_path = self.tmp_path
        root, directory = setup_state(tmp_path)
        write_source(root, "A.lean", "import Missing\ndef a := 1\n")
        state = tasks.refresh(directory, tasks.scan_sources(root, ["A.lean"]), [diagnostic()], main_sha="base", config_revision=0)
        task = next(iter(state["tasks"].values()))
        assert task["kind"] == "command" and task["files"] == ["A.lean"]
        assert not task["chunk_ids"]

    def test_transactions_rollback_and_optimistic_revision(self):
        tmp_path = self.tmp_path
        _, directory = setup_state(tmp_path)
        before = tasks.load(directory)
        with self.raises(RuntimeError):
            with tasks.transaction(directory) as state:
                state["phase"] = "complete"
                raise RuntimeError("do not publish")
        assert tasks.load(directory) == before
        with tasks.transaction(directory, expected_revision=0) as state: state["phase"] = "repairing"
        with self.raises(ValueError, match="stale"):
            with tasks.transaction(directory, expected_revision=0): pass
        assert tasks.load(directory)["revision"] == 1

    def test_exclusive_writers_and_release_requires_closure(self):
        tmp_path = self.tmp_path
        root, directory, first = prepare_task(tmp_path)
        inventory = tasks.scan_sources(root, ["A.lean"])
        state = tasks.refresh(directory, inventory, [diagnostic(), diagnostic(line=2)], main_sha="base", config_revision=0)
        second = next(t for t in state["tasks"] if t != first)
        tasks.claim(directory, "Alice", first)
        with self.raises(ValueError, match="active writer|unfinished dependencies"): tasks.claim(directory, "Bob", second)
        with self.raises(ValueError, match="closure"): tasks.release(directory, "Alice", first, "yield", closed=False)
        tasks.release(directory, "Alice", first, "closed")
        with tasks.transaction(directory) as state: state["tasks"][first]["status"] = "complete"
        tasks.claim(directory, "Bob", second)

    def test_scope_groups_module_without_resetting_budgets(self):
        tmp_path = self.tmp_path
        root, directory, first = prepare_task(tmp_path)
        state = tasks.refresh(directory, tasks.scan_sources(root, ["A.lean"]),
                              [diagnostic(), diagnostic(line=2)], main_sha="base", config_revision=0)
        second = next(t for t in state["tasks"] if t != first)
        with tasks.transaction(directory) as saved: saved["tasks"][first]["status"] = "complete"
        tasks.claim(directory, "Bob", second)
        tasks.release(directory, "Bob", second, "investigated")
        with tasks.transaction(directory) as saved: saved["tasks"][first]["status"] = "pending"
        assigned = tasks.claim(directory, "Alice", first)
        grouped = tasks.request_scope(directory, "Alice", first, state["tasks"][second]["chunk_ids"],
                                      ["A.lean"], "shared API repair", assigned["revision"])
        state = tasks.load(directory)
        assert set(grouped["chunk_ids"]) == set(state["chunks"])
        assert state["tasks"][second]["status"] == "grouped"
        assert state["chunk_attempts"][state["tasks"][second]["chunk_ids"][0]] == 1
        with self.raises(ValueError, match="stale"):
            tasks.request_scope(directory, "Alice", first, [], [], "again", assigned["revision"])

    def test_candidate_rejects_unleased_stale_configuration_and_retains_leases(self):
        tmp_path = self.tmp_path
        _, directory, tid = prepare_task(tmp_path)
        assigned = tasks.claim(directory, "Alice", tid)
        arguments = dict(commit_sha="commit", base_main_sha="base", task_revision=assigned["revision"], config_revision=0)
        with self.raises(ValueError, match="unassigned"):
            tasks.submit_candidate(directory, "Alice", tid, files=["Other.lean"], **arguments)
        with self.raises(ValueError, match="configuration"):
            tasks.submit_candidate(directory, "Alice", tid, files=["A.lean"], **(arguments | {"config_revision": 1}))
        candidate = tasks.submit_candidate(directory, "Alice", tid, files=["A.lean"], **arguments)
        assert candidate["status"] == "submitted"
        assert tasks.load(directory)["leases"]["A.lean"]["author"] == "Alice"
        with self.raises(ValueError, match="awaits integration"):
            tasks.release(directory, "Alice", tid, "closed")

    def test_budget_survives_diagnostic_refresh(self):
        tmp_path = self.tmp_path
        root, directory, tid = prepare_task(tmp_path)
        tasks.claim(directory, "Alice", tid, max_attempts=1)
        tasks.release(directory, "Alice", tid, "tried")
        tasks.refresh(directory, tasks.scan_sources(root, ["A.lean"]), [diagnostic()], main_sha="base", config_revision=0)
        with self.raises(ValueError, match="budget"): tasks.claim(directory, "Bob", tid, max_attempts=1)

    def test_reject_path_escape_and_internal_paths(self):
        tmp_path = self.tmp_path
        root, _ = setup_state(tmp_path)
        for value in ("../A.lean", "/A.lean", ".unity/state.lean", ".lake/packages/A.lean"):
            with self.raises(ValueError): tasks.scan_sources(root, [value])
        outside = tmp_path / "outside.lean"
        outside.write_text("def x := 1")
        (root / "Escape.lean").symlink_to(outside)
        with self.raises(ValueError, match="symlink"): tasks.scan_sources(root, ["Escape.lean"])

    def test_forum_identity_scope_and_yield_keeps_lease(self):
        tmp_path = self.tmp_path
        monkeypatch = SimpleNamespace(setenv=os.environ.__setitem__)
        root, directory, tid = prepare_task(tmp_path)
        tasks.claim(directory, "Alice", tid)
        forum.configure(directory, root)
        monkeypatch.setenv("UNITY_AGENT_NAME", "Alice")
        monkeypatch.setenv("UNITY_BUMP_TASK_ID", tid)
        with self.raises(ValueError, match="author"): forum.yield_task("Bob", tid, "no")
        with self.raises(ValueError, match="assignment"): forum.yield_task("Alice", "wrong", "no")
        forum.yield_task("Alice", tid, "needs API research")
        state = tasks.load(directory)
        assert state["tasks"][tid]["yield_requested"]
        assert state["leases"]["A.lean"]["author"] == "Alice"

    def test_forum_workflow_record_requires_evidence(self):
        tmp_path = self.tmp_path
        root, directory = setup_state(tmp_path)
        forum.configure(directory, root)
        with self.raises(ValueError): forum.record_workflow_change("Alice", "old", "problem", "", "new", "result")
        row = forum.record_workflow_change("Alice", "single declaration", "shared API", "build log", "group module", "build passed")
        assert tasks.load(directory)["workflow_changes"] == [row]

    def test_critic_is_current_independent_and_precise(self):
        tmp_path = self.tmp_path
        root, directory, tid = prepare_task(tmp_path)
        forum.configure(directory, root, "critic")
        with tasks.transaction(directory) as state:
            state["review"] = {"snapshot": {"snapshot_id": "snapshot", "main_sha": "base", "config_revision": 0}}
            state["candidates"]["c"] = {"author": "Alice", "status": "merged"}
        with self.raises(ValueError, match="independent"):
            forum.submit_migration_verdict("Alice", "snapshot", True, "looks good")
        with self.raises(ValueError, match="stale"):
            forum.submit_migration_verdict("Bob", "old", True, "looks good")
        with self.raises(ValueError, match="precise"):
            forum.submit_migration_verdict("Bob", "snapshot", False, "needs work")
        row = forum.submit_migration_verdict("Bob", "snapshot", False, "fix the reported declaration", [tid])
        assert row["reopen_task_ids"] == [tid]

    def test_submission_commits_only_worker_and_preserves_main(self):
        tmp_path = self.tmp_path
        root, directory, tid, worker, base = setup_git_worker(tmp_path)
        (worker / "A.lean").write_text("def foo := 1\ndef bar := 1\n")
        candidate = forum.submit_repair("Alice", tid, "fixed unknown identifier")
        assert git(root, "rev-parse", "HEAD") == base
        assert candidate["commit_sha"] == git(worker, "rev-parse", "HEAD") != base
        assert candidate["files"] == ["A.lean"] and candidate["artifact"]["artifact_id"]
        assert tasks.load(directory)["leases"]["A.lean"]["task_id"] == tid

    def test_submission_rejects_before_git_and_artifact_side_effects(self):
        tmp_path = self.tmp_path
        root, directory, tid, worker, base = setup_git_worker(tmp_path)
        (worker / "Other.lean").write_text("def other := 2\n")
        with self.raises(ValueError, match="outside leased"):
            forum.submit_repair("Alice", tid)
        assert git(worker, "rev-parse", "HEAD") == base
        assert not (root / ".unity" / "artifacts").exists()
        assert tasks.load(directory)["candidates"] == {}

    def test_sync_never_discards_local_changes(self):
        tmp_path = self.tmp_path
        _, _, tid, worker, base = setup_git_worker(tmp_path)
        original = "def foo := 7\n"
        (worker / "A.lean").write_text(original)
        with self.raises(ValueError, match="no reset"):
            forum.sync_from_main("Alice", tid)
        assert (worker / "A.lean").read_text() == original
        assert git(worker, "rev-parse", "HEAD") == base

    def test_phase_tool_catalogs_and_bump_facade(self):
        tmp_path = self.tmp_path
        root, directory = setup_state(tmp_path)
        from unity.forum import bump_server
        bump_server.configure(directory, root, "repairing")
        assert bump_server._COMPILER_WORKFLOW
        assert bump_server.build_server("repairing").name == "unity-bump-forum-repairing"
        assert forum.submit_repair not in forum.PROFILE_TOOLS["critic"]
        assert forum.submit_migration_verdict not in forum.PROFILE_TOOLS["repairing"]
        assert forum.submit_migration_verdict in forum.PROFILE_TOOLS["critic"]

    def test_no_original_preservation_contract_in_new_schema(self):
        tmp_path = self.tmp_path
        _, directory = setup_state(tmp_path)
        state = tasks.load(directory)
        assert state["schema"] == "compiler-migration-v2"
        assert not {"project_baseline", "original_inventory", "representation", "formalization"} & state.keys()

    def test_legacy_state_is_not_implicitly_imported(self):
        tmp_path = self.tmp_path
        directory = tmp_path / "forum"
        directory.mkdir()
        (directory / tasks.STATE_FILE).write_text(json.dumps({"schema": "legacy"}))
        with self.raises(ValueError, match="explicit migration"): tasks.load(directory)

    def test_configuration_request_records_environment_changes_without_applying_them(self):
        root, directory, tid = prepare_task(self.tmp_path)
        forum.configure(directory, root)
        with tasks.transaction(directory) as state:
            state["config"].update(modules=["Existing"], project_scope="build")
        result = forum.request_configuration_change("Alice", tid, [], "additional current module needs dev dependencies",
            "build supplemental module with dev config", modules=["Extra"], lake_args=["-Kenv=dev"], project_scope="build")
        self.assertEqual(result["requested_config"], {"modules": ["Existing", "Extra"], "lake_args": ["-Kenv=dev"], "project_scope": "build"})
        self.assertEqual(tasks.load(directory)["config"]["lake_args"], [])
        self.assertEqual(result["status"], "pending")

    def test_configuration_request_rejects_command_override_and_scope_drop(self):
        root, directory, tid = prepare_task(self.tmp_path)
        forum.configure(directory, root)
        with self.raises(ValueError):
            forum.request_configuration_change("Alice", tid, [], "bad command", "update", lake_args=["update"])
        with tasks.transaction(directory) as state: state["config"]["project_scope"] = "all"
        with self.raises(ValueError, match="drop"):
            forum.request_configuration_change("Alice", tid, [], "hide failure", "default only", project_scope="build")

    def test_scope_request_cannot_bypass_configuration_assignment(self):
        _, directory, tid = prepare_task(self.tmp_path)
        assignment = tasks.claim(directory, "Alice", tid)
        for filename in ["lean-toolchain", "lakefile.toml", "lake-manifest.json", "vendor/Dep/Foo.lean"]:
            with self.raises(ValueError, match="configuration assignment"):
                tasks.request_scope(directory, "Alice", tid, [], [filename], "need API changes", assignment["revision"])

    def test_source_order_is_explicit_conservative_dependency(self):
        root, directory, first = prepare_task(self.tmp_path)
        state = tasks.refresh(directory, tasks.scan_sources(root, ["A.lean"]),
            [diagnostic(), diagnostic(line=2)], main_sha="base", config_revision=0)
        second = next(t for t in state["tasks"] if t != first)
        self.assertEqual(state["tasks"][second]["dependencies"], [first])
        self.assertIn("conservative", state["tasks"][second]["dependency_basis"])
        assignment = tasks.claim(directory, "Alice", first)
        grouped = tasks.request_scope(directory, "Alice", first, state["tasks"][second]["chunk_ids"], [], "shared API", assignment["revision"])
        self.assertNotIn(first, grouped["dependencies"])
        self.assertNotIn(second, grouped["dependencies"])

    def test_unchecked_changed_snapshot_cannot_inherit_clean_status(self):
        root, directory = setup_state(self.tmp_path)
        write_source(root, "A.lean", "def a := 1\n")
        inventory = tasks.scan_sources(root, ["A.lean"])
        tasks.refresh(directory, inventory, [], main_sha="base", config_revision=0, checked_files=["A.lean"])
        state = tasks.refresh(directory, inventory, [], main_sha="different", config_revision=0, checked_files=[])
        self.assertEqual(next(iter(state["chunks"].values()))["status"], "unobserved")

    def test_excluded_declarations_inventory_but_not_repair_work(self):
        root, directory = setup_state(self.tmp_path)
        write_source(root, "Old.lean", "def archived := missing\n")
        inventory = tasks.scan_sources(root, ["Old.lean"])
        for chunk in inventory["chunks"].values(): chunk.update(status="excluded", scope_excluded=True)
        with tasks.transaction(directory) as state:
            state["config"]["scope"] = {"excluded_files": {"Old.lean": "historical module"}}
        state = tasks.refresh(directory, inventory, [diagnostic("Old.lean")], main_sha="base", config_revision=0)
        self.assertEqual(next(iter(state["chunks"].values()))["status"], "excluded")
        self.assertEqual(next(iter(state["tasks"].values()))["kind"], "scope_review")

    def test_concurrent_transactions_publish_every_update(self):
        _, directory = setup_state(self.tmp_path)
        def update(index):
            with tasks.transaction(directory) as state: state["events"].append({"index": index})
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(update, range(30)))
        state = tasks.load(directory)
        self.assertEqual(state["revision"], 30)
        self.assertEqual(sorted(e["index"] for e in state["events"]), list(range(30)))

    def test_critic_can_queue_new_issue_in_previously_clean_chunk(self):
        root, directory = setup_state(self.tmp_path)
        write_source(root, "A.lean", "def a := 1\n")
        tasks.refresh(directory, tasks.scan_sources(root, ["A.lean"]), [], main_sha="base", config_revision=0,
                       checked_files=["A.lean"])
        with tasks.transaction(directory) as state:
            state["review"] = {"snapshot": {"snapshot_id": "review", "main_sha": "base", "config_revision": 0}}
        before = tasks.load(directory)["review"]["snapshot"]
        forum.configure(directory, root, "critic")
        chunk = next(iter(tasks.load(directory)["chunks"]))
        result = forum.request_review_repair("Bob", "review", [chunk], [], "The migration changed the intended API.")
        state = tasks.load(directory)
        self.assertFalse(state["leases"])
        self.assertEqual(state["review"]["snapshot"], before)
        self.assertEqual(result["task"]["chunk_ids"], [chunk])
        with self.raises(ValueError, match="unresolved review"):
            forum.submit_migration_verdict("Bob", "review", True, "ignore my own issue")
        verdict = forum.submit_migration_verdict("Bob", "review", False, "repair the identified API", result["task_ids"])
        self.assertEqual(verdict["reopen_task_ids"], result["task_ids"])

    def test_review_repair_preserves_existing_attempt_budget(self):
        root, directory, tid = prepare_task(self.tmp_path)
        tasks.claim(directory, "Alice", tid)
        tasks.release(directory, "Alice", tid, "closed")
        with tasks.transaction(directory) as state:
            state["tasks"][tid]["status"] = "complete"
            state["review"] = {"snapshot": {"snapshot_id": "review", "main_sha": "base", "config_revision": 0}}
        forum.configure(directory, root, "critic")
        chunk_ids = tasks.load(directory)["tasks"][tid]["chunk_ids"]
        result = forum.request_review_repair("Bob", "review", chunk_ids, [], "Review found an incomplete repair")
        self.assertEqual(result["task_ids"], [tid])
        self.assertEqual(result["task"]["attempts"], 1)
        self.assertEqual(tasks.load(directory)["chunk_attempts"][chunk_ids[0]], 1)

    def test_yield_cleared_only_after_closure_before_new_assignment(self):
        root, directory, tid = prepare_task(self.tmp_path)
        tasks.claim(directory, "Alice", tid)
        forum.configure(directory, root, "repairing")
        forum.yield_task("Alice", tid, "Need a different approach")
        with self.raises(ValueError, match="closure"):
            tasks.release(directory, "Alice", tid, "not stopped yet", closed=False)
        self.assertIn("yield_requested", tasks.load(directory)["tasks"][tid])
        tasks.release(directory, "Alice", tid, "writer closed", closed=True)
        state = tasks.load(directory)
        self.assertNotIn("yield_requested", state["tasks"][tid])
        self.assertEqual(state["events"][-1]["previous_yield"]["reason"], "Need a different approach")
        fresh = tasks.claim(directory, "Bob", tid)
        self.assertNotIn("yield_requested", fresh)
        self.assertEqual(fresh["attempts"], 2)

    def test_compiler_refresh_does_not_retire_pending_audit_or_critic_repair(self):
        root, directory, tid = prepare_task(self.tmp_path)
        inventory = tasks.scan_sources(root, ["A.lean"])
        for key in ("audit_feedback", "critic_feedback", "review_snapshot_id"):
            with self.subTest(key=key):
                with tasks.transaction(directory) as state:
                    task = state["tasks"][tid]
                    for marker in ("audit_feedback", "critic_feedback", "review_snapshot_id"):
                        task.pop(marker, None)
                    task.update(status="pending", **{key: "repair required"})
                state = tasks.refresh(directory, inventory, [], main_sha="base", config_revision=0,
                                      checked_files=["A.lean"], complete=True)
                self.assertEqual(state["tasks"][tid]["status"], "pending")
        with tasks.transaction(directory) as state:
            state["tasks"][tid]["status"] = "complete"  # controller integrated its reviewed repair
        state = tasks.refresh(directory, inventory, [], main_sha="base", config_revision=0,
                              checked_files=["A.lean"], complete=True)
        self.assertEqual(state["tasks"][tid]["status"], "complete")

    def test_scope_review_assignment_can_request_configuration_files(self):
        root, directory, tid = prepare_task(self.tmp_path)
        with tasks.transaction(directory) as state:
            state["tasks"][tid]["kind"] = "scope_review"
        assignment = tasks.claim(directory, "Alice", tid)
        enlarged = tasks.request_scope(directory, "Alice", tid, [], ["lakefile.toml"],
                                       "Explicit controller scope review", assignment["revision"])
        self.assertIn("lakefile.toml", enlarged["files"])

    def test_grouping_retains_review_findings_and_pending_repair_requirement(self):
        root, directory, first = prepare_task(self.tmp_path)
        inventory = tasks.scan_sources(root, ["A.lean"])
        state = tasks.refresh(directory, inventory, [diagnostic(), diagnostic(line=2)],
                              main_sha="base", config_revision=0)
        second = next(key for key in state["tasks"] if key != first)
        feedback = {"audit_feedback": {"kind": "new_hole", "path": "A.lean"},
                    "critic_feedback": {"reasoning": "Repair API change"}, "review_snapshot_id": "review-1"}
        with tasks.transaction(directory) as state:
            state["tasks"][second].update(feedback)
        assignment = tasks.claim(directory, "Alice", first)
        grouped = tasks.request_scope(directory, "Alice", first, state["tasks"][second]["chunk_ids"], [],
                                       "These declarations need one shared repair", assignment["revision"])
        self.assertEqual(grouped["grouped_review_feedback"], [{"task_id": second, **feedback}])
        tasks.release(directory, "Alice", first, "worker closed without patch", closed=True)
        state = tasks.refresh(directory, inventory, [], main_sha="base", config_revision=0,
                              checked_files=["A.lean"], complete=True)
        self.assertEqual(state["tasks"][first]["status"], "pending")
        self.assertEqual(state["tasks"][second]["status"], "grouped")
        self.assertEqual(state["tasks"][first]["grouped_review_feedback"], [{"task_id": second, **feedback}])

