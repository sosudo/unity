"""Bump migration review boundaries: offline mocks and temporary Git only."""

import asyncio
from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from unity import bump_orchestrator as orchestrator
from unity import bump_permissions, bump_report, bump_review, bump_spawn, bump_worktree
from unity import bump_migration_project


def paths(root):
    return SimpleNamespace(project_root=root, forum=root / ".unity/forum/bump")


def fixture_agent(backend="codex"):
    return SimpleNamespace(name="Ada", backend=backend, model="fixture", is_primary=True,
                           provider="openai", base_url=None, api_key=None, auth_token=None,
                           budget=None, strength=1.)


def report_fixture():
    baseline = {
        "version": 5, "policy": "migration-v1", "scope_policy": 1, "occurrence_policy": 1,
        "sha256": "b" * 64, "declarations": {},
        "migration": {"source_commit": "a" * 40, "source_hash": "c" * 64,
                      "target_version": "v4.34.0"},
        "compiler_modules": {"Empty": {"path": "Empty.lean", "imports": []},
                             "Proof": {"path": "Proof.lean", "imports": ["Empty"]}},
        "original_reports": {
            "Empty": {"declarations": {}},
            "Proof": {"declarations": {
                "oldHole": {"kind": "theorem", "direct_sorry": True, "axioms": ["sorryAx"]},
                "oldAxiom": {"kind": "axiom", "direct_sorry": False, "axioms": ["oldAxiom"]},
                "proved": {"kind": "theorem", "direct_sorry": False, "axioms": []},
            }},
        },
    }
    scope = {"version": 1, "mode": "all", "kind": "all_project_modules", "default_build_required": True,
             "selected_modules": {"Empty": "Empty.lean", "Proof": "Proof.lean"},
             "excluded_modules": {}, "excluded_files": {}, "native_default_modules": {}, "native_metadata": {}}
    scope["sha256"] = bump_migration_project._digest(scope)
    baseline["build_scope"] = scope
    baseline["migration"]["scope"] = deepcopy(scope)
    baseline["migration"]["source_files"] = {"Empty.lean": "e" * 64, "Proof.lean": "f" * 64}
    coverage = {
        "mode": "migration", "policy": "migration-v1", "inspection_policy": 4, "occurrence_policy": 1,
        "declaration_occurrences": {},
        "scope_policy": 1, "scope_sha256": scope["sha256"], "project_scope": "all",
        "baseline_sha256": baseline["sha256"],
        "original_source_commit": baseline["migration"]["source_commit"],
        "original_source_sha256": baseline["migration"]["source_hash"],
        "target_version": "v4.34.0", "verification_modules": {"Empty.lean": "Empty", "Proof.lean": "Proof"},
        "contexts": ["Empty", "Proof"], "byte_only_modules": {}, "byte_preserved_files": {},
        "normal_default_build": True,
    }
    snapshot = {"snapshot_id": "snapshot-current", "passed": True, "project_verification": coverage}
    state = {
        "run_id": "bump-fixture", "phase": "complete", "project_baseline": baseline,
        "formalization": {"status": "accepted", "main_sha": "d" * 40, "requirements": [],
                          "accepted_verdict_id": "approved-current", "review_snapshot": snapshot,
                          "contract": {"project_baseline": baseline, "migration_policy": 1,
                                       "migration_scope_policy": 1, "migration_occurrence_policy": 1,
                                       "inspection_policy": 4}},
        "critic_verdicts": [{
            "verdict_id": "approved-current", "verdict": "approved", "author": "Ada",
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_sha256": bump_report.bump_state._report_digest(snapshot),
            "requirements_sha256": bump_report.bump_state._report_digest([]),
            "review": {"snapshot_id": snapshot["snapshot_id"], "scope_rationale": "Original modules preserved",
                       "requirements": [], "repair_reviews": []},
        }],
    }
    return state, snapshot


class BumpCriticBoundaryTests(unittest.TestCase):
    def test_critic_operational_failure_propagates_instead_of_becoming_missing_verdict(self):
        agent = fixture_agent()
        roster = SimpleNamespace(agents=[agent], primary=agent)
        config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "critic")
        fault = OSError("fixture service unavailable")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(orchestrator, "spawn", new_callable=AsyncMock, side_effect=fault) as spawn, \
                patch.object(orchestrator.library, "library_context", return_value=""), \
                patch.object(orchestrator.library, "library_subagents", return_value=()), \
                patch.object(orchestrator, "load_prompt", return_value="fixture"):
            with self.assertRaisesRegex(RuntimeError, "critic agent Ada failed") as raised:
                asyncio.run(orchestrator.dispatch(
                    [agent], roster, "review", "record evidence", Path(directory), config,
                    brief_provider=lambda _: "", log_context={"phase": "critic"}))
        self.assertIs(raised.exception.__cause__, fault)
        spawn.assert_awaited_once()

    def test_critic_has_only_forum_despite_enabled_external_services(self):
        with patch.dict(os.environ, {"AXLE_API_KEY": "test-only", "ARISTOTLE_API_KEY": "test-only"}):
            config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "critic")
        self.assertEqual(set(config), {"unity-forum"})
        self.assertEqual(config["unity-forum"]["args"][-2:], ["--profile", "critic"])
        self.assertNotIn("AXLE_API_KEY", config["unity-forum"].get("env", {}))

    def test_workers_retain_their_existing_services(self):
        with patch.dict(os.environ, {"AXLE_API_KEY": "test-only", "ARISTOTLE_API_KEY": "test-only"}):
            config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "formalizing")
        self.assertEqual(set(config), {"unity-forum", "lean-lsp", "axle", "aristotle"})

    def test_unsupported_critic_backend_rejected_before_dispatch(self):
        for backend in ("claude_code", "antigravity", "openai"):
            with self.subTest(backend=backend), patch.object(orchestrator, "spawn", new_callable=AsyncMock) as spawn:
                agent = fixture_agent(backend)
                config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "critic")
                with self.assertRaisesRegex(ValueError, "read-only Codex"):
                    asyncio.run(orchestrator.dispatch([agent], None, "", "", Path("/fixture"), config))
                spawn.assert_not_called()

    def test_critic_rejects_injected_external_service_before_dispatch(self):
        config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "critic")
        config["lean-lsp"] = {"command": "uvx", "args": ["lean-lsp-mcp"]}
        with patch.object(orchestrator, "spawn", new_callable=AsyncMock) as spawn:
            with self.assertRaisesRegex(ValueError, "only the snapshot-bound Forum"):
                asyncio.run(orchestrator.dispatch([fixture_agent()], None, "", "", Path("/fixture"), config))
            spawn.assert_not_called()

    def test_critic_log_context_also_blocks_external_services(self):
        config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "formalizing")
        with self.assertRaises(ValueError):
            asyncio.run(orchestrator.dispatch([fixture_agent()], None, "", "", Path("/fixture"), config,
                                             log_context={"phase": "critic"}))

    def test_critic_cannot_drop_or_disagree_with_native_phase_policy(self):
        config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "critic")
        for options in ({"mcp_profile": "other"}, {"log_context": {"phase": "formalizing"}},
                        {"env_overrides": {"UNITY_BUMP_PROFILE": "formalizing"}}):
            with self.subTest(options=options), patch.object(orchestrator, "spawn", new_callable=AsyncMock) as spawn:
                with self.assertRaisesRegex(ValueError, "consistent snapshot-bound critic profile"):
                    asyncio.run(orchestrator.dispatch([fixture_agent()], None, "", "", Path("/fixture"), config, **options))
                spawn.assert_not_called()

    def test_codex_critic_dispatch_preserves_phase_and_forum_only(self):
        agent = fixture_agent()
        roster = SimpleNamespace(agents=[agent], primary=agent)
        config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "critic")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(orchestrator, "spawn", new_callable=AsyncMock, return_value="reviewed") as spawn, \
                patch.object(orchestrator.library, "library_context", return_value=""), \
                patch.object(orchestrator.library, "library_subagents", return_value=()), \
                patch.object(orchestrator, "load_prompt", return_value="fixture"):
            result = asyncio.run(orchestrator.dispatch(
                [agent], roster, "review", "record evidence", Path(directory), config,
                brief_provider=lambda _: "", log_context={"phase": "critic"},
            ))
        self.assertEqual(result, ["reviewed"])
        self.assertEqual(set(spawn.call_args.args[4]), {"unity-forum"})
        self.assertEqual(spawn.call_args.kwargs["log_context"]["phase"], "critic")

    def test_direct_spawn_rejects_unsupported_backend_from_transport_phase(self):
        config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "critic")
        with patch.object(bump_spawn, "claude_spawner", new_callable=AsyncMock) as backend, \
                patch.object(bump_spawn, "_stop_requested", return_value=False), \
                patch.object(bump_spawn, "_write_run_log"):
            with self.assertRaisesRegex(ValueError, "read-only Codex"):
                asyncio.run(bump_spawn.spawn(fixture_agent("claude_code"), "", "", Path("/fixture"), config))
            backend.assert_not_called()

    def test_direct_spawn_rejects_profile_bypass_and_phase_disagreement(self):
        config = orchestrator.build_bump_mcp(paths(Path("/fixture")), "critic")
        for options in ({"mcp_profile": "other"}, {"log_context": {"phase": "formalizing"}},
                        {"env_overrides": {"UNITY_BUMP_PROFILE": "formalizing"}}):
            with self.subTest(options=options), patch.object(bump_spawn, "codex_spawner", new_callable=AsyncMock) as backend, \
                    patch.object(bump_spawn, "_stop_requested", return_value=False), patch.object(bump_spawn, "_write_run_log"):
                with self.assertRaises(ValueError):
                    asyncio.run(bump_spawn.spawn(fixture_agent(), "", "", Path("/fixture"), config, **options))
                backend.assert_not_called()

    def test_codex_critic_uses_read_only_sdk_and_cli_policy_without_model_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker, home = root / "worker", root / "isolated"
            thread = SimpleNamespace(turn=AsyncMock())
            client = SimpleNamespace(thread_start=AsyncMock(return_value=thread), close=AsyncMock())
            config_ctor = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
            sdk = SimpleNamespace(AsyncCodex=Mock(return_value=client), CodexConfig=config_ctor,
                                  Sandbox=SimpleNamespace(read_only="read-only", workspace_write="workspace-write"),
                                  ApprovalMode=SimpleNamespace(deny_all="never"))
            config = orchestrator.build_bump_mcp(paths(worker), "critic")
            with patch.dict(sys.modules, {"openai_codex": sdk}), \
                    patch.object(bump_spawn.tempfile, "mkdtemp", return_value=str(home)), \
                    patch.object(bump_spawn, "_worktree_write_roots", return_value=(worker,)), \
                    patch.object(bump_spawn, "_stop_requested", side_effect=[False, True]), \
                    patch.object(Path, "home", return_value=root):
                result = asyncio.run(bump_spawn.codex_spawner(
                    fixture_agent(), "review", "record evidence", worker, config,
                    env_overrides={"UNITY_BUMP_PROFILE": "critic"}))
            self.assertIsNone(result)
            self.assertEqual(client.thread_start.call_args.kwargs["sandbox"], "read-only")
            self.assertEqual(client.thread_start.call_args.kwargs["approval_mode"], "never")
            overrides = tomllib.loads("\n".join(config_ctor.call_args.kwargs["config_overrides"]))
            self.assertEqual(overrides["sandbox_mode"], "read-only")
            self.assertEqual(overrides["approval_policy"], "never")
            self.assertNotIn("sandbox_workspace_write", overrides)
            self.assertEqual(set(overrides["mcp_servers"]), {"unity-forum"})
            thread.turn.assert_not_awaited()
            client.close.assert_awaited_once()


class BumpStopBridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.original = Path(temp.name).resolve() / "original"
        self.target = self.original / ".unity/bump/bump-fixture/target"
        (self.target / ".unity").mkdir(parents=True)
        self.record = {"project_root": str(self.original), "run_id": "bump-fixture", "target_path": str(self.target)}
        self.origin = self.target / ".unity/bump-origin.json"
        self.active = self.original / ".unity/bump/active.json"
        self.origin.write_text(json.dumps(self.record))
        self.active.write_text(json.dumps({**self.record, "status": "ready"}))

    def test_valid_bridge_is_idle_then_observes_original_stop(self):
        self.assertFalse(orchestrator.stop_requested(self.target))
        (self.original / ".unity/stop-requested").touch()
        self.assertTrue(orchestrator.stop_requested(self.target))

    def test_private_worker_runtime_link_observes_original_stop(self):
        worker = self.target / ".worktrees/bump-Ada"
        worker.mkdir(parents=True)
        (worker / ".unity").symlink_to(self.target / ".unity", target_is_directory=True)
        self.assertFalse(orchestrator.stop_requested(worker))
        (self.original / ".unity/stop-requested").touch()
        self.assertTrue(orchestrator.stop_requested(worker))

    def test_target_local_stop_does_not_need_origin(self):
        self.origin.unlink()
        (self.target / ".unity/stop-requested").touch()
        self.assertTrue(orchestrator.stop_requested(self.target))

    def test_missing_origin_uses_local_marker_only(self):
        self.origin.unlink()
        (self.original / ".unity/stop-requested").touch()
        self.assertFalse(orchestrator.stop_requested(self.target))

    def test_mismatched_active_identity_stops_fail_closed(self):
        for key, value in (("run_id", "bump-other"), ("target_path", str(self.original)),
                           ("project_root", str(self.target)), ("status", "superseded")):
            with self.subTest(key=key):
                self.active.write_text(json.dumps({**self.record, "status": "ready", key: value}))
                self.assertTrue(orchestrator.stop_requested(self.target))

    def test_malformed_or_path_escaping_origin_stops_fail_closed(self):
        cases = [[], {**self.record, "target_path": str(self.original)},
                 {**self.record, "run_id": "../outside"}, {**self.record, "project_root": "."},
                 {**self.record, "extra": "not trusted"}]
        for record in cases:
            with self.subTest(record=record):
                self.origin.write_text(json.dumps(record))
                self.assertTrue(orchestrator.stop_requested(self.target))

    def test_symlinked_origin_or_active_record_is_not_followed(self):
        data = self.target / "foreign.json"
        data.write_text(json.dumps(self.record))
        self.origin.unlink()
        self.origin.symlink_to(data)
        self.assertTrue(orchestrator.stop_requested(self.target))
        self.origin.unlink()
        self.origin.write_text(json.dumps(self.record))
        self.active.unlink()
        self.active.symlink_to(data)
        self.assertTrue(orchestrator.stop_requested(self.target))


class BumpReportBoundaryTests(unittest.TestCase):
    def test_migration_coverage_includes_empty_module_and_inherited_holes(self):
        state, snapshot = report_fixture()
        result = bump_report._project_verification(state, snapshot, accepted=True)
        self.assertEqual(result["original_verification_modules"]["Empty.lean"], "Empty")
        self.assertEqual(set(result["inherited_assumptions_and_holes"]["Proof"]), {"oldHole", "oldAxiom"})
        self.assertEqual(result["inherited_assumptions_and_holes"]["Empty"], {})

    def test_accepted_report_rejects_coverage_version_identity_and_module_drift(self):
        replacements = {"inspection_policy": 2, "target_version": "v4.99.0", "original_source_commit": "e" * 40,
                        "original_source_sha256": "f" * 64, "baseline_sha256": "0" * 64,
                        "normal_default_build": False, "contexts": ["Proof"],
                        "verification_modules": {"Proof.lean": "Proof"}, "byte_only_modules": {"Empty.lean": "Empty"}}
        for key, value in replacements.items():
            with self.subTest(key=key):
                state, snapshot = report_fixture()
                snapshot["project_verification"][key] = value
                with self.assertRaisesRegex(ValueError, "two-version coverage"):
                    bump_report._project_verification(state, snapshot, accepted=True)

    def test_incomplete_report_is_explicitly_historical_and_does_not_require_paper(self):
        state, _ = report_fixture()
        state["formalization"]["review_snapshot"]["project_verification"] = None
        result = bump_report.completion_report(state, accepted=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["machine_review_scope"], "historical recorded evidence only")
        self.assertEqual(result["project_scope"], "migration")
        self.assertIn("No source paper is required", result["qualification"])

    def test_accepted_report_cannot_use_copied_formalize_contract_policy(self):
        state, _ = report_fixture()
        del state["formalization"]["contract"]["migration_policy"]
        with self.assertRaisesRegex(ValueError, "migration contract policy"):
            bump_report.completion_report(state)

    def test_matching_approval_still_invokes_independent_state_validation(self):
        state, _ = report_fixture()
        with patch.object(bump_report.bump_contract, "_baseline_matches", return_value=True), \
                patch.object(bump_report.bump_state, "_validate_snapshot_binding") as machine, \
                patch.object(bump_report.bump_state, "_validate_semantic_review") as critic:
            result = bump_report.completion_report(state)
        machine.assert_called_once_with(state, state["formalization"]["review_snapshot"], require_passed=True)
        critic.assert_called_once()
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["critic_verdict"]["verdict_id"], "approved-current")

    def test_stale_critic_approval_cannot_be_reported_as_accepted(self):
        for key in ("snapshot_id", "snapshot_sha256", "requirements_sha256"):
            with self.subTest(key=key):
                state, _ = report_fixture()
                state["critic_verdicts"][0][key] = "stale"
                with patch.object(bump_report.bump_contract, "_baseline_matches", return_value=True), \
                        patch.object(bump_report.bump_state, "_validate_snapshot_binding"), \
                        self.assertRaisesRegex(ValueError, "critic evidence"):
                    bump_report.completion_report(state)

    def test_typed_review_does_not_accept_unknown_authority_fields(self):
        review = report_fixture()[0]["critic_verdicts"][0]["review"]
        with self.assertRaises(ValidationError):
            bump_review.SemanticReview.model_validate({**review, "override_machine_failure": True})

    def test_publication_rejects_stale_snapshot_before_writing_artifact(self):
        state, _ = report_fixture()
        locations = SimpleNamespace(project_root=Path("/fixture"), forum=Path("/fixture/forum"), artifacts=Path("/fixture/artifacts"))
        with patch.object(bump_report, "_merge_lock", return_value=nullcontext()), \
                patch.object(bump_report.bump_state, "transaction", return_value=nullcontext(state)), \
                patch.object(bump_report, "completion_report", return_value={"status": "accepted", "main_sha": "d" * 40}), \
                patch.object(bump_report, "require_source_matches") as source, \
                patch.object(bump_report.bump_contract, "snapshot_is_current", return_value=False), \
                patch.object(bump_report.artifacts, "store_text") as store:
            with self.assertRaisesRegex(ValueError, "stale source revision"):
                bump_report.persist_report(locations)
        source.assert_called_once()
        store.assert_not_called()
        self.assertNotIn("final_report", state)

    def test_publication_rechecks_snapshot_after_immutable_artifact_write(self):
        state, _ = report_fixture()
        report = {"status": "accepted", "main_sha": "d" * 40}
        data = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
        locations = SimpleNamespace(project_root=Path("/fixture"), forum=Path("/fixture/forum"), artifacts=Path("/fixture/artifacts"))
        with patch.object(bump_report, "_merge_lock", return_value=nullcontext()), \
                patch.object(bump_report.bump_state, "transaction", return_value=nullcontext(state)), \
                patch.object(bump_report, "completion_report", return_value=report), \
                patch.object(bump_report, "require_source_matches"), \
                patch.object(bump_report.bump_contract, "snapshot_is_current", side_effect=[True, False]) as current, \
                patch.object(bump_report.artifacts, "store_text", return_value={"artifact_id": "report", "sha256": hashlib.sha256(data).hexdigest()}), \
                patch.object(bump_report.artifacts, "artifact_bytes", return_value=data):
            with self.assertRaisesRegex(ValueError, "stale source revision"):
                bump_report.persist_report(locations)
        self.assertEqual(current.call_count, 2)
        self.assertNotIn("final_report", state)


class BumpPrivateWorktreeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Unity Test")
        self.git("config", "user.email", "unity@example.test")
        (self.root / "Proof.lean").write_text("theorem old : True := by sorry\n")
        self.git("add", "Proof.lean")
        self.git("commit", "-qm", "original")
        (self.root / ".unity").mkdir()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout.strip()

    def test_role_view_is_separate_but_not_itself_a_permission_sandbox(self):
        view = bump_worktree.role_view(self.root, "critic")
        self.assertEqual((view / ".unity").resolve(), self.root / ".unity")
        original = (self.root / "Proof.lean").read_bytes()
        (view / "Proof.lean").write_text("only the private view changed")
        self.assertEqual((self.root / "Proof.lean").read_bytes(), original)
        self.assertNotEqual(view, self.root)

    def test_completed_work_is_retained_and_cannot_be_replaced(self):
        tree = bump_worktree.create_worktree("Ada", self.root)
        (tree / "draft.txt").write_text("unsubmitted work")
        bump_worktree.cleanup_worktree("Ada", tree, self.root)
        self.assertEqual((tree / "draft.txt").read_text(), "unsubmitted work")
        with self.assertRaisesRegex(ValueError, "recovered, not overwritten"):
            bump_worktree.create_worktree("Ada", self.root)
        with self.assertRaisesRegex(ValueError, "not a Bump-owned"):
            bump_worktree.cleanup_worktree("Ada", self.root, self.root)

    def test_shared_git_migrations_use_distinct_branches_and_keep_prior_work(self):
        first = self.root / ".unity/bump/bump-first/target"
        second = self.root / ".unity/bump/bump-second/target"
        self.git("worktree", "add", "-b", "unity/bump-first", str(first))
        self.git("worktree", "add", "-b", "unity/bump-second", str(second))
        for target in (first, second):
            (target / ".unity").mkdir()
        one = bump_worktree.create_worktree("Ada", first)
        (one / "Proof.lean").write_text("theorem old : True := by trivial\n")
        subprocess.run(["git", "add", "Proof.lean"], cwd=one, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "first private progress"], cwd=one, check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=one, check=True,
                             capture_output=True, text=True).stdout.strip()
        two = bump_worktree.create_worktree("Ada", second)
        self.assertNotEqual(bump_worktree.agent_branch("Ada", first), bump_worktree.agent_branch("Ada", second))
        self.assertEqual(bump_worktree.verify_candidate_commit(first, "Ada", sha), sha)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            bump_worktree.verify_candidate_commit(second, "Ada", sha)
        self.assertEqual((one / "Proof.lean").read_text(), "theorem old : True := by trivial\n")
        self.assertEqual((two / "Proof.lean").read_text(), "theorem old : True := by sorry\n")
        self.assertEqual((self.root / "Proof.lean").read_text(), "theorem old : True := by sorry\n")

    def test_claude_worker_cannot_gain_source_ancestor_write_access(self):
        tree = bump_worktree.create_worktree("Ada", self.root)
        with self.assertRaisesRegex(ValueError, "ancestor"):
            bump_permissions.claude_worktree_options(tree, (self.root,))

    def test_claude_native_edits_cannot_escape_via_runtime_symlink(self):
        tree = bump_worktree.create_worktree("Ada", self.root)
        options = bump_permissions.claude_worktree_options(tree, (tree, self.root / ".unity/scratch"))
        hook = options["hooks"]["PreToolUse"][0].hooks[0]
        for filename in ("../outside.lean", ".unity/forged-state.json"):
            with self.subTest(filename=filename):
                result = asyncio.run(hook({"tool_name": "Write", "cwd": str(tree),
                                          "tool_input": {"file_path": filename}}, "id", None))
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        result = asyncio.run(hook({"tool_name": "Write", "cwd": str(tree),
                                  "tool_input": {"file_path": "Proof.lean"}}, "id", None))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "allow")


if __name__ == "__main__":
    unittest.main()
