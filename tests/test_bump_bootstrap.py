"""Deterministic Bump setup and module-plan tests, without model evaluation."""

import json
import os
from copy import deepcopy
from pathlib import Path
import stat
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_bump_project as project_fixture
from unity import bump_bootstrap as bootstrap, bump_state, bump_spec, bump_runtime, bump_report
from unity import artifacts, bump_inventory, bump_diagnostics, bump_architect
from unity.commands import bump as command
from unity.forum import bump_server
from unity.config import Paths


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        fixture = project_fixture.ProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        self.paths = Paths.from_unity_dir(self.root / ".unity")
        self.paths.unity.mkdir()
        self.paths.unity_md.write_text("# Goal\nPreserve this project during the pinned migration.\n")
        self.paths.agents_yaml.write_text("agents: []\n")
        self.paths.env.write_text("MAX_ATTEMPTS=5\n")
        self.graph = {
            "Fixture": {"path": "Fixture.lean", "imports": ["Fixture.Basic"], "compiler_derived": True},
            "Fixture.Basic": {"path": "Fixture/Basic.lean", "imports": [], "compiler_derived": True}}
        self.reports = {name: {"module": name, "verified": True, "complete_inventory": True,
                               "declarations": {}, "evidence_sha256": "a" * 64} for name in self.graph}
        self.mocks = {}
        self.add_patch(bootstrap.bump_migration_project, "build", return_value={"passed": True, "diagnostics": "", "returncode": 0})
        self.add_patch(bootstrap.bump_migration_project, "validate_dependencies", return_value=[])
        self.add_patch(bootstrap.bump_migration_project, "compiler_modules", return_value=self.graph)
        self.add_patch(bump_inventory, "capture_original_index", side_effect=self.capture_index)
        self.add_patch(bootstrap.bump_project, "capture_baseline_v2", return_value={"sha256": "b" * 64, "policy": "migration-v2"})
        self.add_patch(bootstrap.bump_project, "require_original_branch")
        self.add_patch(bootstrap.bump_project, "_require_clean")
        self.add_patch(bootstrap.bump_contract, "prepare_migration_contract_v2", return_value={"migration_policy": 2})
        self.add_patch(bootstrap.bump_contract, "validate_migration_state")
        self.add_patch(bootstrap.bump_state, "initialize_migration_plan", side_effect=self.install)
        self.add_patch(bump_diagnostics, "collect_build_diagnostics", side_effect=self.diagnostics)
        self.add_patch(bump_architect, "prepare_optional_architect",
                       side_effect=lambda target, migration, resolution, mode: (migration, resolution, {"status": "off"}))

    def add_patch(self, module, name, **kwargs):
        patcher = patch.object(module, name, **kwargs)
        self.mocks[name] = patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, forum, index, plan, **kwargs):
        with bump_state.transaction(forum) as state:
            state["phase"] = "formalizing"
        return bump_state.load_state(forum)

    def capture_index(self, root, scope, *, artifact_dir):
        reports = {module: {"mode": "index", "module": module, "declaration_inventory": "raw-module-constants-v1",
                           "raw_declaration_count": 0, "declarations": []} for module in self.graph}
        index = bump_inventory.assemble_index(reports, self.graph, bootstrap.bump_migration_project.source_files(root),
                                             scope_sha256=scope["sha256"], environment={})
        record = artifacts.store_text(artifact_dir, json.dumps(index), kind="bump_original_index")
        return {"index_ref": {key: record[key] for key in ("artifact_id", "sha256")}, "index_sha256": index["index_sha256"]}

    def diagnostics(self, target, index, **kwargs):
        from tests.test_bump_diagnostics import fixture_diagnostics
        return fixture_diagnostics(index, compiled=index["modules"], errors=())

    def legacy_source(self, source):
        # Retain legacy pure-plan regression coverage separately from v2 setup.
        refs = [row for row in source["source_refs"] if row["ref_id"] != "source:original-index.json"]
        refs += [{"ref_id": "source:native/" + module + ".json"} for module in self.graph]
        return {**source, "source_refs": refs}

    def prepare(self):
        return bootstrap.prepare(self.paths, "v4.34.1", {}, project_scope="all", architect="off")

    def test_fresh_setup_installs_copied_runtime_plan_in_private_named_branch(self):
        before = bootstrap.bump_migration_project.snapshot(self.root)
        paths = self.prepare()
        record = json.loads(bootstrap.active_path(self.paths).read_text())
        self.assertEqual(record["status"], "ready")
        self.assertEqual(bootstrap.bump_migration_project.snapshot(self.root), before)
        self.assertNotEqual(paths.project_root, self.root)
        self.assertEqual(self.fixture.git("branch", "--show-current", root=paths.project_root), record["branch"])
        self.assertTrue(record["branch"].startswith("unity/bump-"))
        self.assertFalse(paths.unity.is_symlink())
        self.assertEqual(stat.S_IMODE(paths.env.stat().st_mode), 0o600)
        self.mocks["initialize_migration_plan"].assert_called_once()
        self.mocks["capture_original_index"].assert_called_once()
        source = bump_state.load_state(paths.forum)["input_source"]
        self.assertEqual(len(source["source_refs"]), 4)
        self.assertTrue(all(row["kind"] == "original_project_input" for row in source["source_refs"]))

    def test_original_scope_and_roster_are_securely_copied_without_paper(self):
        paths = self.prepare()
        self.assertEqual(paths.unity_md.read_bytes(), self.paths.unity_md.read_bytes())
        self.assertEqual(paths.agents_yaml.read_bytes(), self.paths.agents_yaml.read_bytes())
        self.assertFalse((self.paths.unity / "source").exists())
        self.assertTrue((paths.unity / "source/project/Fixture.lean").is_file())
        self.assertTrue((paths.unity / "source/original-index.json").is_file())
        self.assertFalse((paths.unity / "source/native").exists())
        origin = json.loads((paths.unity / "bump-origin.json").read_text())
        self.assertEqual(origin["project_root"], str(self.root))
        self.assertEqual(origin["target_path"], str(paths.project_root))

    def test_native_preparation_progress_records_every_context_without_becoming_acceptance(self):
        paths = self.prepare()
        events = [json.loads(line) for line in (paths.unity / "bump-preparation.jsonl").read_text().splitlines()]
        names = [item["event"] for item in events]
        self.assertEqual(names, ["original_build_started", "original_build_finished", "original_index_started",
                                "original_index_finished", "target_build_started", "target_build_finished", "repair_plan_ready"])
        self.assertEqual(events[3]["occurrence_count"], 0)
        self.assertTrue(all(item.get("elapsed_seconds", 0) >= 0 for item in events))
        self.assertTrue(all("accepted" not in item for item in events))
        self.assertTrue(all("bump-preparation.jsonl" not in row["ref_id"]
            for row in bump_state.load_state(paths.forum)["input_source"]["source_refs"]))

    def test_native_preparation_failure_preserves_prior_progress_without_raw_error_text(self):
        self.mocks["capture_original_index"].side_effect = ValueError("private diagnostic that must not be copied")
        with self.assertRaisesRegex(ValueError, "private diagnostic"):
            self.prepare()
        pointer = json.loads(bootstrap.active_path(self.paths).read_text())
        text = (Path(pointer["target_path"]) / ".unity/bump-preparation.jsonl").read_text()
        events = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(events[-1]["event"], "original_index_failed")
        self.assertEqual(events[-1]["error_type"], "ValueError")
        self.assertNotIn("private diagnostic", text)
        self.assertEqual(events[1]["event"], "original_build_finished")
        self.assertEqual(pointer["status"], "preparing")
        self.mocks["capture_baseline_v2"].assert_not_called()

    def test_scope_is_captured_after_build_and_passed_to_native_graph(self):
        events = []
        capture = bootstrap.bump_migration_project.capture_build_scope
        self.mocks["build"].side_effect = lambda _root: (events.append("build") or {"passed": True})
        def scoped(original, migration):
            self.assertEqual(events, ["build"])
            events.append("scope")
            return capture(original, migration)
        def graph(_root, *, scope):
            self.assertEqual(events, ["build", "scope"])
            self.assertEqual(scope["selected_modules"], {name: row["path"] for name, row in self.graph.items()})
            events.append("graph")
            return self.graph
        self.mocks["compiler_modules"].side_effect = graph
        with patch.object(bootstrap.bump_migration_project, "capture_build_scope", side_effect=scoped):
            self.prepare()
        self.assertEqual(events, ["build", "scope", "graph"])

    def test_fresh_attempt_never_overwrites_existing_evidence(self):
        paths = self.prepare()
        old = bootstrap.active_path(self.paths).read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()
        self.assertEqual(bootstrap.active_path(self.paths).read_bytes(), old)
        self.assertTrue(paths.project_root.is_dir())

    def test_preparation_failure_is_retained_and_not_blindly_resumed(self):
        self.mocks["build"].return_value = {"passed": False, "diagnostics": "old compiler failure"}
        with patch.object(bootstrap.bump_migration_project, "capture_build_scope") as capture:
            with self.assertRaisesRegex(ValueError, "must build"):
                self.prepare()
            capture.assert_not_called()
        record = json.loads(bootstrap.active_path(self.paths).read_text())
        self.assertEqual(record["status"], "preparing")
        self.assertTrue(Path(record["target_path"]).is_dir())
        with self.assertRaisesRegex(ValueError, "did not finish"):
            bootstrap.resume(self.paths)

    def test_continue_reuses_exact_initialized_state(self):
        paths = self.prepare()
        state = bump_state.state_path(paths.forum).read_bytes()
        self.assertEqual(bootstrap.resume(self.paths), paths)
        self.assertEqual(bump_state.state_path(paths.forum).read_bytes(), state)
        self.mocks["build"].assert_called_once()

    def test_continue_rejects_version_pin_or_runtime_changes(self):
        paths = self.prepare()
        with self.assertRaisesRegex(ValueError, "sealed project scope"):
            bootstrap.resume(self.paths, project_scope="build")
        with self.assertRaisesRegex(ValueError, "target Lean version"):
            bootstrap.resume(self.paths, "v4.35.0")
        with self.assertRaisesRegex(ValueError, "dependency pins"):
            bootstrap.resume(self.paths, dependency_pins={"mathlib": "v4.34.1"})
        paths.agents_yaml.write_text("agents: [different]\n")
        with self.assertRaisesRegex(ValueError, "runtime configuration changed"):
            bootstrap.resume(self.paths)

    def test_legacy_pointer_refused_before_loading_old_large_state(self):
        pointer = bootstrap.active_path(self.paths)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"version": 1, "status": "ready", "migration_policy": 1,
                                       "project_root": str(self.root)}))
        with patch.object(bootstrap.bump_state, "load_state", side_effect=AssertionError("old state must not be read")) as load:
            with self.assertRaisesRegex(ValueError, "Old-policy"):
                bootstrap.resume(self.paths)
        load.assert_not_called()

    def test_unchanged_diagnostic_refresh_does_not_build_or_load_inventory(self):
        state = {"formalization": {"contract": {"migration_policy": 2}, "main_sha": "a" * 40},
                 "project_baseline": {}, "migration_plan": {"source_sha256": "b" * 64},
                 "migration_refresh_required": False}
        with patch.object(bootstrap.bump_project, "require_pinned_inputs"), \
             patch.object(bootstrap.bump_worktree, "main_commit", return_value="a" * 40), \
             patch.object(bootstrap.bump_contract, "source_identity", return_value={"source_sha256": "b" * 64}), \
             patch.object(bump_inventory, "load_original_index", side_effect=AssertionError("unchanged inventory must not reload")) as load:
            result = bootstrap.refresh_target_diagnostics(self.paths, state=state)
        self.assertEqual(result, state)
        self.mocks["collect_build_diagnostics"].assert_not_called()
        load.assert_not_called()

    def test_deterministic_module_dag_is_valid_source_spec_and_covers_empty_modules(self):
        paths = self.prepare()
        source = self.legacy_source(bump_state.load_state(paths.forum)["input_source"])
        dag = bootstrap.migration_dag(self.graph, self.reports, source)
        requirements = bump_spec.normalize_requirements(dag["requirements"], dag["chunks"],
                                                        {row["ref_id"] for row in source["source_refs"]})
        spec = bump_spec.normalize_spec(dag["spec"], source=source, requirements=requirements, tasks=dag["chunks"])
        nodes = bump_spec.normalize_informal_nodes(dag["chunks"], requirements, spec, source)
        self.assertEqual(set(nodes), set(self.graph))
        self.assertEqual(nodes["Fixture"]["dependencies"], ["Fixture.Basic"])
        self.assertEqual(nodes["Fixture.Basic"]["predicted_kind"], "module")
        self.assertEqual(dag, bootstrap.migration_dag(self.graph, self.reports, source))

    def test_lexical_graph_cannot_replace_compiler_graph(self):
        paths = self.prepare()
        source = self.legacy_source(bump_state.load_state(paths.forum)["input_source"])
        bad = {key: {**entry, "compiler_derived": False} for key, entry in self.graph.items()}
        with self.assertRaisesRegex(ValueError, "original Lean compiler"):
            bootstrap.migration_dag(bad, self.reports, source)

    def test_missing_native_module_inventory_blocks_plan(self):
        paths = self.prepare()
        source = self.legacy_source(bump_state.load_state(paths.forum)["input_source"])
        with self.assertRaisesRegex(ValueError, "same modules"):
            bootstrap.migration_dag(self.graph, {}, source)

    def test_no_version_or_duplicate_pins_block_before_worktree_creation(self):
        with self.assertRaisesRegex(ValueError, "exact Lean version"):
            bootstrap.prepare(self.paths, None, {})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            bootstrap.parse_dependency_pins(["mathlib=v4.34.1", "mathlib=v4.35.0"])
        self.assertFalse(bootstrap.active_path(self.paths).exists())

    def test_controller_precheck_does_not_erase_critic_repair_request(self):
        state = {"formal_tasks": {"Fixture": {"task_id": "Fixture", "migration_module": "Fixture",
                 "status": "pending", "faithfulness": {"status": "changes_requested"}, "dependencies": []}}}
        with patch.object(bootstrap.bump_state, "load_state", return_value=state), \
                patch.object(bootstrap.bump_contract, "source_identity") as identity:
            result = bootstrap.check_ready_modules(SimpleNamespace(forum=Path("unused"), project_root=self.root))
        self.assertEqual(result, state)
        identity.assert_not_called()

    def test_controller_precheck_skips_pending_candidates_and_merging_tasks(self):
        for status in ("candidate_pending", "merging", "complete"):
            with self.subTest(status=status):
                state = {"formal_tasks": {"Fixture": {"task_id": "Fixture", "migration_module": "Fixture",
                         "status": status, "dependencies": []}}}
                with patch.object(bootstrap.bump_state, "load_state", return_value=state), \
                        patch.object(bootstrap.bump_contract, "source_identity") as identity:
                    bootstrap.check_ready_modules(SimpleNamespace(forum=Path("unused"), project_root=self.root))
                identity.assert_not_called()

    def test_inspection_race_discards_check_without_overwriting_candidate(self):
        for passed in (True, False):
            with self.subTest(passed=passed):
                state = {"revision": 7, "formalization": {"contract": {"sha256": "contract"}},
                         "formal_tasks": {"Fixture": {"task_id": "Fixture", "migration_module": "Fixture",
                         "status": "pending", "dependencies": []}}}

                def inspect(*args):
                    state["revision"] = 8
                    state["formal_tasks"]["Fixture"]["status"] = "candidate_pending"
                    return {"passed": passed, "issues": []}

                with patch.object(bootstrap.bump_state, "load_state", side_effect=lambda forum: deepcopy(state)), \
                        patch.object(bootstrap.bump_contract, "source_identity", return_value={"source_sha256": "source"}), \
                        patch.object(bootstrap.bump_contract, "check_migration_module", side_effect=inspect) as check, \
                        patch.object(bootstrap.bump_state, "record_migration_module_check", return_value=False) as accept, \
                        patch.object(bootstrap.bump_state, "record_migration_diagnostic", return_value=False) as diagnostic:
                    result = bootstrap.check_ready_modules(SimpleNamespace(
                        forum=Path("unused"), project_root=self.root, artifacts=self.root / ".unity/artifacts"))
                check.assert_called_once()
                publication = accept if passed else diagnostic
                self.assertEqual(publication.call_args.kwargs["expected_revision"], 7)
                self.assertEqual(result["formal_tasks"]["Fixture"]["status"], "candidate_pending")


class NativeBootstrapTests(unittest.TestCase):
    def test_native_original_to_target_frontier_including_import_only_module(self):
        if not shutil.which("elan"):
            self.skipTest("Native fixture requires already-installed Lean toolchains")
        result = subprocess.run(["elan", "toolchain", "list"], text=True, capture_output=True, timeout=20)
        available = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
        original_toolchain = os.getenv("UNITY_TEST_BUMP_ORIGINAL_TOOLCHAIN", "leanprover/lean4:v4.33.0")
        if not {original_toolchain, "leanprover/lean4:v4.34.1"}.issubset(available):
            self.skipTest("Native fixture does not install missing toolchains")
        fixture = project_fixture.ProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write("lean-toolchain", original_toolchain + "\n")
        fixture.write("Fixture/Imports.lean", "import Fixture.Basic\n")
        fixture.write("Fixture/Unrelated.lean", "theorem Fixture.unrelated : True := by trivial\n")
        fixture.write("Fixture.lean", "import Fixture.Imports\nimport Fixture.Unrelated\n"
                      "theorem result : True := Fixture.basic\n")
        fixture.write("Notes/IntentionalError.lean", "#check deliberatelyUnknown\n")
        fixture.commit()
        paths = Paths.from_unity_dir(fixture.root / ".unity")
        paths.unity.mkdir()
        paths.unity_md.write_text("# Goal\nPreserve all original declarations and trust assumptions.\n")
        paths.agents_yaml.write_text("agents: []\n")
        paths.env.write_text("MAX_ATTEMPTS=5\nRETROSPECTIVE=false\n")
        before = bootstrap.bump_migration_project.snapshot(fixture.root)
        with patch.dict("os.environ", {"MAX_ATTEMPTS": "5"}):
            target = bootstrap.prepare(paths, "v4.34.1", {}, architect="off")
        state = bump_state.load_state(target.forum)
        modules = {"Fixture", "Fixture.Basic", "Fixture.Imports", "Fixture.Unrelated"}
        self.assertEqual(state["formal_tasks"], {})
        self.assertEqual(set(state["formalization"]["contract"]["task_bindings"]), modules)
        scope = state["project_baseline"]["build_scope"]
        self.assertEqual(scope["mode"], "build")
        self.assertIn("Notes/IntentionalError.lean", scope["excluded_files"])
        self.assertEqual((target.project_root / "Notes/IntentionalError.lean").read_bytes(),
                         (fixture.root / "Notes/IntentionalError.lean").read_bytes())
        self.assertEqual(state["formalization"]["contract"]["bindings"]["Fixture.Imports"], [])
        state = bootstrap.check_ready_modules(target)
        self.assertTrue(bump_state.all_formal_tasks_complete(state), {
            key: {"status": task["status"], "diagnostics": task.get("migration_diagnostics")}
            for key, task in state["formal_tasks"].items()})
        self.assertEqual(state["phase"], "formalizing")
        self.assertEqual(state["formalization"]["status"], "active")
        self.assertEqual(bootstrap.bump_migration_project.snapshot(fixture.root), before)
        self.assertEqual(state["formal_candidates"], {})

        # A test-only state fixture models target compiler failures in
        # two independent modules. It is not a model, a migration bypass or an
        # accepted receipt: both changed modules must traverse real candidate
        # submission/integration, then the unchanged final gates below.
        fixture.write("Fixture/Unrelated.lean", "theorem Fixture.unrelated : True := unknownFixtureProof\n",
                      root=target.project_root)
        fixture.write("Fixture/Basic.lean", "theorem Fixture.basic : True := unknownBasicProof\n",
                      root=target.project_root)
        fixture.git("add", "Fixture/Unrelated.lean", "Fixture/Basic.lean", root=target.project_root)
        fixture.git("commit", "-qm", "test-only target compiler failure", root=target.project_root)
        with bump_state.transaction(target.forum) as current:
            current["formalization"]["main_sha"] = fixture.git("rev-parse", "HEAD", root=target.project_root)
            current["migration_refresh_required"] = True
        state = bootstrap.check_ready_modules(target)
        self.assertEqual(set(state["formal_tasks"]), modules)
        self.assertEqual(state["formal_tasks"]["Fixture.Basic"]["diagnostic_status"], "repair")
        self.assertEqual(state["formal_tasks"]["Fixture.Unrelated"]["diagnostic_status"], "repair")
        self.assertEqual(state["formal_tasks"]["Fixture.Imports"]["diagnostic_status"], "blocked")
        self.assertFalse(bootstrap.bump_migration_project.build(target.project_root, sorted(modules))["passed"])

        old_server = (bump_server.FORUM_DIR, bump_server.PROJECT_ROOT, bump_server.PROFILE)
        old_discussion = (bump_server.discussion.FORUM_DIR, bump_server.discussion.PROJECT_ROOT,
                          bump_server.discussion.ICRL_ENABLED)
        self.addCleanup(lambda: setattr(bump_server, "FORUM_DIR", old_server[0]))
        self.addCleanup(lambda: setattr(bump_server, "PROJECT_ROOT", old_server[1]))
        self.addCleanup(lambda: setattr(bump_server, "PROFILE", old_server[2]))
        self.addCleanup(lambda: setattr(bump_server.discussion, "FORUM_DIR", old_discussion[0]))
        self.addCleanup(lambda: setattr(bump_server.discussion, "PROJECT_ROOT", old_discussion[1]))
        self.addCleanup(lambda: setattr(bump_server.discussion, "ICRL_ENABLED", old_discussion[2]))
        bump_server.configure(target.forum, target.project_root, "formalizing")

        def submit_and_integrate(module, author, content):
            tree = bootstrap.bump_worktree.create_worktree(author, target.project_root)
            state = bump_state.load_state(target.forum)
            task = state["formal_tasks"][module]
            fixture.write(task["lean_file"], content, root=tree)
            strategy = bump_state.register_strategy(target.forum, author, "Preserve the original theorem using a target-compatible proof",
                                                    target=module)["strategy"]
            bump_state.claim_strategy(target.forum, strategy["strategy_id"], author)
            with patch.dict("os.environ", {"UNITY_AGENT_NAME": author}):
                submitted = bump_server.finalize_formalization(strategy["strategy_id"], author, module,
                    changed_paths=[task["lean_file"]], outputs=task["outputs"])
            self.assertEqual(submitted["status"], "submitted", submitted)
            candidate = bump_state.begin_formal_merge(target.forum, submitted["candidate"]["candidate_id"])["candidate"]
            result = bump_runtime._integrate_and_record(target, candidate, task)
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(bump_state.load_state(target.forum)["formal_tasks"][module]["status"], "complete")

        submit_and_integrate("Fixture.Basic", "FixtureWorkerA", "theorem Fixture.basic : True := by exact True.intro\n")
        # The copied candidate path must build/check this module's native import
        # closure, not make unrelated compiler failures an all-project barrier.
        self.assertFalse(bootstrap.bump_migration_project.build(target.project_root, sorted(modules))["passed"])
        self.assertTrue(bump_state.load_state(target.forum)["migration_refresh_required"])
        state = bootstrap.check_ready_modules(target)
        self.assertFalse(state["migration_refresh_required"])
        self.assertEqual(state["formal_tasks"]["Fixture.Unrelated"]["diagnostic_status"], "repair")
        submit_and_integrate("Fixture.Unrelated", "FixtureWorkerB", "theorem Fixture.unrelated : True := by exact True.intro\n")
        state = bootstrap.check_ready_modules(target)
        self.assertTrue(bump_state.all_formal_tasks_complete(state))
        self.assertEqual(state["formal_tasks"], {})
        self.assertEqual(state["formalization"]["contract"]["bindings"]["Fixture.Imports"], [])
        report = bootstrap.bump_contract.verify_final_project(target, state)
        self.assertTrue(report["passed"], report.get("issues"))
        self.assertNotEqual(bump_state.load_state(target.forum)["phase"], "complete")
        bump_state.record_review_snapshot(target.forum, report)
        bump_state.begin_critic(target.forum)
        state = bump_state.load_state(target.forum)
        review = {"snapshot_id": report["snapshot_id"],
                  "scope_rationale": "Canned offline fixture verdict; every original module and declaration is explicitly covered.",
                  "repair_reviews": [], "requirements": [
                      {"requirement_id": row["id"], "status": "pass",
                       "checked_anchor_ids": row["anchor_ids"], "checked_prerequisite_ids": [],
                       "declarations": [output["declaration"] for key in row["tasks"]
                                        for output in state["formalization"]["contract"]["bindings"][key]],
                       "rationale": "Scripted fixture compares original and migrated module obligations.",
                       "argument_rationale": "Only proof terms changed; the import-only module has no declarations."}
                      for row in state["formalization"]["requirements"]]}
        bump_state.submit_critic_verdict(target.forum, "FixtureCritic", "approved", "Canned fixture approval, not a model evaluation.", review=review)
        self.assertTrue(command._accept_current_critic(target))
        accepted = bump_state.load_state(target.forum)
        self.assertEqual(accepted["phase"], "complete")
        self.assertEqual(bump_report.completion_report(accepted)["status"], "accepted")
        corrupt = deepcopy(accepted)
        corrupt["critic_verdicts"][-1]["snapshot_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "accepted semantic verdict does not match the migration snapshot"):
            bump_report.completion_report(corrupt)
        bump_report.persist_report(target, accepted=True)
        stale = target.project_root / "Fixture/Basic.lean"
        original = stale.read_bytes()
        try:
            stale.write_bytes(original + b"\n-- stale post-review source\n")
            with self.assertRaisesRegex(ValueError, "stale"):
                bump_report.persist_report(target, accepted=True)
        finally:
            stale.write_bytes(original)
        self.assertEqual(bootstrap.bump_migration_project.snapshot(fixture.root), before)


if __name__ == "__main__":
    unittest.main()
