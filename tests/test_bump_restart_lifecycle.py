"""Declaration scheduling and provisional state; no models or Lean compiler."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from unity import bump_contract, bump_spawn, bump_state
from test_bump_manifest_repair import informal_dag, machine_snapshot
from test_bump_manifest_runtime import ManifestRuntimeSchedulingTests


class RestartSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = ManifestRuntimeSchedulingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    async def test_exhausted_worker_is_rescheduled_without_cancelling_peer(self):
        fixture = self.fixture
        calls = {name: 0 for name in fixture.names}
        retry_started = asyncio.Event()
        peer_cancelled = []

        async def worker(agent, *_args, **_kwargs):
            calls[agent.name] += 1
            if agent.name == "Ada":
                if calls[agent.name] == 1:
                    raise RuntimeError("429 after the backend retry session exhausted")
                retry_started.set()
                return
            try:
                await retry_started.wait()
            except asyncio.CancelledError:
                peer_cancelled.append(agent.name)
                raise

        await fixture.run_runtime(worker)
        self.assertEqual(calls, {"Ada": 2, "Bert": 1})
        self.assertEqual(peer_cancelled, [])
        self.assertEqual(fixture.rounds[-1]["blocked_launches"], {})
        self.assertEqual(fixture.rounds[-1]["activity"]["worker_launches"], 3)

    async def test_independent_declarations_in_one_file_run_concurrently(self):
        fixture = self.fixture
        fixture.state["formal_tasks"] = {
            key: {"task_id": key, "status": "pending", "revision": 1,
                  "lean_file": "Example.lean", "outputs": [{"declaration": key, "file": "Example.lean"}],
                  "migration": {"kind": "declaration", "original_ids": [key],
                                "path": "Example.lean", "module": "Example"}}
            for key in ("alpha", "beta")
        }
        fixture.state["worker_tasks"] = {"ada": "alpha", "bert": "beta"}
        active, launched = set(), []
        both_active = asyncio.Event()

        async def worker(agent, _system, prompt, *_args, **kwargs):
            task_id = kwargs["log_context"]["task_id"]
            launched.append((agent.name, task_id))
            active.add(task_id)
            if active == {"alpha", "beta"}:
                both_active.set()
            await both_active.wait()
            fixture.state["formal_tasks"][task_id]["status"] = "complete"
            active.remove(task_id)

        await fixture.run_runtime(worker)
        self.assertEqual(set(launched), {("Ada", "alpha"), ("Bert", "beta")})
        self.assertTrue(both_active.is_set())


class CodexTerminalRetryTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, outcomes, *, callback=None):
        clients = []
        def client_factory(**_kwargs):
            status, message = outcomes[len(clients)]
            async def stream():
                if status == "completed":
                    yield SimpleNamespace(method="item/completed", payload=SimpleNamespace(
                        item=SimpleNamespace(root=SimpleNamespace(type="agentMessage", text="repaired"))))
                yield SimpleNamespace(method="turn/completed", payload=SimpleNamespace(
                    turn=SimpleNamespace(status=status, error=SimpleNamespace(message=message))))
            handle = SimpleNamespace(stream=stream)
            thread = SimpleNamespace(turn=AsyncMock(return_value=handle))
            client = SimpleNamespace(thread_start=AsyncMock(return_value=thread), close=AsyncMock())
            clients.append(client)
            return client
        sdk = SimpleNamespace(AsyncCodex=client_factory, CodexConfig=lambda **kwargs: kwargs,
                              Sandbox=SimpleNamespace(workspace_write="workspace", full_access="full"))
        agent = SimpleNamespace(name="Ada", model="deepseek-v4-flash", api_key="", base_url="")
        with tempfile.TemporaryDirectory() as directory, ExitStack() as mocks:
            mocks.enter_context(patch.dict(sys.modules, {"openai_codex": sdk}))
            mocks.enter_context(patch.dict(os.environ, {"MAX_ATTEMPTS": "2"}))
            for name, value in (("_worktree_write_roots", ()), ("_write_codex_config", "openai"),
                                ("_write_codex_agents", None), ("_agent_env", {}),
                                ("_stop_requested", False), ("_log", None)):
                mocks.enter_context(patch.object(bump_spawn, name, return_value=value))
            mocks.enter_context(patch.object(bump_spawn.tempfile, "mkdtemp", return_value=directory))
            mocks.enter_context(patch("shutil.which", return_value=None))
            mocks.enter_context(patch.object(bump_spawn, "_terminate_process_group", new=AsyncMock()))
            sleep = mocks.enter_context(patch.object(bump_spawn.asyncio, "sleep", new=AsyncMock()))
            try:
                result = await bump_spawn.codex_spawner(
                    agent, "system", "task", Path(directory), {}, mcp_profile="solve",
                    on_normal_completion=callback,
                )
            finally:
                self.clients = clients
                self.backoffs = [call.args[0] for call in sleep.await_args_list if call.args[0] > 0]
        return result

    async def test_terminal_429_retries_without_a_completion_callback(self):
        result = await self.invoke([("failed", "429 Too Many Requests"), ("completed", None)])
        self.assertEqual(result, "repaired")
        self.assertEqual(self.backoffs, [60.0])
        self.assertEqual(len(self.clients), 2)
        for client in self.clients:
            client.close.assert_awaited_once()

    async def test_terminal_429_retries_then_calls_only_successful_completion(self):
        completion = AsyncMock(return_value=None)
        result = await self.invoke([("failed", "429 Too Many Requests"), ("completed", None)], callback=completion)
        self.assertEqual(result, "repaired")
        completion.assert_awaited_once_with("repaired")
        self.assertEqual(self.backoffs, [60.0])

    async def test_terminal_failure_exhausts_existing_cap_and_closes_every_client(self):
        completion = AsyncMock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, "429"):
            await self.invoke([("failed", "429 Too Many Requests")] * 2, callback=completion)
        completion.assert_not_awaited()
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.backoffs, [60.0])
        for client in self.clients:
            client.close.assert_awaited_once()

    async def test_controller_callback_error_is_not_a_transport_retry(self):
        completion = AsyncMock(side_effect=ValueError("controller state changed"))
        with self.assertRaisesRegex(ValueError, "controller state changed"):
            await self.invoke([("completed", None)], callback=completion)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.backoffs, [])
        self.clients[0].close.assert_awaited_once()


class MigrationPlanStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bump-declaration-state-")
        self.addCleanup(temporary.cleanup)
        self.forum = Path(temporary.name) / "forum"
        self.source = {"kind": "supplied_sources", "migration": True,
                       "candidate_id": "source-" + "a" * 64, "sha256": "a" * 64,
                       "source_refs": [{"ref_id": "source:Example.lean", "path": "Example.lean",
                                        "sha256": "b" * 64}]}
        bump_state.initialize_source(self.forum, "c" * 64, "d" * 40, self.source)
        chunks = [{"id": key, "lean_decl": key, "source_components": ["source:Example.lean"]}
                  for key in ("alpha", "beta")]
        self.dag = informal_dag(self.source, chunks)
        for row in self.dag["chunks"]:
            key = row["id"]
            row.update(lean_file="Example.lean", lean_decl=key,
                       outputs=[{"declaration": key, "file": "Example.lean"}],
                       migration={"kind": "declaration", "original_ids": [key],
                                  "path": "Example.lean", "module": "Example",
                                  "original_ranges": [], "diagnostic_ids": [key + "-error"]})
        self.dag["compiler_tasks"] = ["alpha", "beta"]
        self.contract = {"version": 3, "migration_policy": 1,
                         "environment": {}, "bindings": {}, "targets": {}, "external_declarations": {},
                         "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"],
                         "requirements": deepcopy(self.dag["requirements"]), "spec": deepcopy(self.dag["spec"]),
                         "spec_sha256": bump_state.digest(self.dag["spec"]), "obligation_ids": ["alpha", "beta"]}
        self.contract["sha256"] = bump_state._contract_digest(self.contract)

    def initialize(self):
        return bump_state.initialize_migration_plan(
            self.forum, self.dag, main_sha="d" * 40, contract=self.contract,
        )

    def test_top_level_declaration_tasks_share_file_without_module_owner(self):
        state = self.initialize()
        self.assertEqual(set(state["formal_tasks"]), {"alpha", "beta"})
        self.assertEqual(state["file_reservations"]["Example.lean"],
                         {"owner_task": "alpha", "shared_with": ["beta"]})
        self.assertEqual({row["task_id"] for row in bump_state.ready_formal_tasks(state)}, {"alpha", "beta"})
        for task in state["formal_tasks"].values():
            self.assertNotIn("execution_unit", task)
            self.assertNotIn("migration_attempts", task)
            self.assertNotIn("single_writer", task)

    def test_original_proof_dependencies_wait_for_repair_integration(self):
        self.dag["chunks"][1]["proof_dependencies"] = ["alpha"]
        state = self.initialize()
        self.assertFalse(bump_state.task_ready(state, "beta"))
        state["formal_tasks"]["alpha"]["representation"] = {"status": "adopted"}
        self.assertFalse(bump_state.task_ready(state, "beta"))
        state["formal_tasks"]["alpha"]["status"] = "complete"
        state["formal_tasks"]["alpha"]["verification"] = {"status": "provisional", "native_pending": True}
        self.assertTrue(bump_state.task_ready(state, "beta"))
        from unity.forum.bump_server import task_readiness
        evidence = task_readiness(state, "beta")["proof_dependencies"][0]
        self.assertTrue(evidence["repair_integrated"])
        self.assertFalse(evidence["native_preservation_accepted"])
        self.assertNotIn("proof_complete", evidence)

    def test_command_repair_can_submit_without_inventing_a_declaration(self):
        state = self.initialize()
        state["formal_tasks"]["alpha"]["migration"].update(kind="command", original_ids=[])
        state["formal_tasks"]["alpha"]["outputs"] = []
        self.assertEqual(bump_state.submission_blockers(state, "alpha", outputs=[]), [])
        self.assertIn("output_manifest_invalid", {
            row["code"] for row in bump_state.submission_blockers(state, "beta", outputs=[])
        })

    def test_refresh_preserves_candidate_identity_for_unrelated_declaration(self):
        initial = self.initialize()
        task = initial["formal_tasks"]["beta"]
        candidate = {"task_id": "beta", "task_revision": task["revision"],
                     "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"]}
        self.dag["compiler_tasks"] = ["beta"]
        state = bump_state.refresh_migration_plan(self.forum, self.dag, main_sha="d" * 40,
                                                  contract=initial["formalization"]["contract"])
        self.assertTrue(bump_state.candidate_is_current(state, candidate))
        cleared = state["formal_tasks"]["alpha"]
        self.assertEqual(cleared["status"], "complete")
        self.assertIsNone(cleared["accepted_candidate"])
        self.assertEqual(cleared["verification"]["status"], "provisional")
        self.assertTrue(cleared["verification"]["native_pending"])
        self.assertEqual(state["formalization"]["source_obligations"], initial["formalization"]["source_obligations"])

    def test_refresh_supersedes_only_resolved_sibling_candidate_and_claim(self):
        initial = self.initialize()
        with bump_state.transaction(self.forum) as state:
            state["worker_tasks"] = {"ada": "alpha", "bert": "beta"}
            for key in ("alpha", "beta"):
                state["formal_candidates"]["candidate-" + key] = {
                    "candidate_id": "candidate-" + key, "task_id": key, "status": "submitted",
                    "task_revision": initial["formal_tasks"][key]["revision"],
                    "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"]}
                state["strategies"]["strategy-" + key] = {
                    "strategy_id": "strategy-" + key, "target": key, "phase": "formalizing", "status": "claimed"}
        self.dag["compiler_tasks"] = ["beta"]
        state = bump_state.refresh_migration_plan(self.forum, self.dag, main_sha="d" * 40,
                                                  contract=initial["formalization"]["contract"])
        self.assertEqual(state["formal_candidates"]["candidate-alpha"]["status"], "superseded")
        self.assertEqual(state["formal_candidates"]["candidate-beta"]["status"], "submitted")
        self.assertTrue(bump_state.candidate_is_current(state, state["formal_candidates"]["candidate-beta"]))
        self.assertEqual(state["formal_tasks"]["beta"]["revision"], initial["formal_tasks"]["beta"]["revision"])
        self.assertEqual(state["strategies"]["strategy-alpha"]["status"], "cancelled")
        self.assertEqual(state["strategies"]["strategy-beta"]["status"], "claimed")
        self.assertEqual(state["worker_tasks"], {"ada": "alpha", "bert": "beta"})

    def test_reappearing_error_reopens_only_its_declaration_revision(self):
        initial = self.initialize()
        self.dag["compiler_tasks"] = ["beta"]
        cleared = bump_state.refresh_migration_plan(self.forum, self.dag, main_sha="d" * 40,
                                                    contract=initial["formalization"]["contract"])
        self.dag["compiler_tasks"] = ["alpha", "beta"]
        current = bump_state.refresh_migration_plan(self.forum, self.dag, main_sha="d" * 40,
                                                    contract=cleared["formalization"]["contract"])
        self.assertEqual(current["formal_tasks"]["alpha"]["status"], "pending")
        self.assertEqual(current["formal_tasks"]["alpha"]["revision"],
                         initial["formal_tasks"]["alpha"]["revision"] + 1)
        self.assertEqual(current["formal_tasks"]["beta"]["revision"], initial["formal_tasks"]["beta"]["revision"])

    def test_refresh_rejects_rebinding_existing_original_identity(self):
        initial = self.initialize()
        self.dag["chunks"][0]["migration"]["original_ids"] = ["other-original"]
        with self.assertRaisesRegex(ValueError, "original identity"):
            bump_state.refresh_migration_plan(self.forum, self.dag, main_sha="d" * 40,
                                              contract=initial["formalization"]["contract"])
        self.assertEqual(bump_state.load_state(self.forum), initial)

    def test_clean_empty_queue_preserves_original_requirements(self):
        self.dag["chunks"] = []
        self.dag["compiler_tasks"] = []
        for row in self.dag["requirements"]:
            row["tasks"] = []
        self.contract["requirements"] = deepcopy(self.dag["requirements"])
        self.contract["obligation_ids"] = []
        self.contract["sha256"] = bump_state._contract_digest(self.contract)
        state = self.initialize()
        self.assertEqual(state["formal_tasks"], {})
        self.assertTrue(bump_state.all_formal_tasks_complete(state))
        self.assertTrue(state["formalization"]["requirements"])
        self.assertIsNone(state["formalization"]["review_snapshot"])
        self.assertNotEqual(state["formalization"]["status"], "accepted")

    def merging_candidate(self):
        state = self.initialize()
        candidate = {"candidate_id": "candidate-alpha", "task_id": "alpha", "status": "merging",
                     "task_revision": state["formal_tasks"]["alpha"]["revision"],
                     "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"],
                     "stage": "complete", "outputs": deepcopy(state["formal_tasks"]["alpha"]["outputs"]),
                     "strategy_id": "strategy-alpha", "author": "Ada"}
        with bump_state.transaction(self.forum) as current:
            current["formal_candidates"][candidate["candidate_id"]] = candidate
        proposed = deepcopy(state["formalization"]["contract"])
        proposed["bindings"]["alpha"] = candidate["outputs"]
        proposed["targets"]["alpha"] = {"fingerprint": "e" * 64}
        proposed["sha256"] = bump_state._contract_digest(proposed)
        verification = {"status": "passed", "mode": "diagnostic_repair", "native_pending": True,
                        "contract_sha256": proposed["sha256"], "policy_sha256": bump_contract.policy_hash(),
                        "proposed_contract": proposed, "issues": []}
        return candidate, verification

    def test_declaration_integration_does_not_claim_native_verification(self):
        candidate, verification = self.merging_candidate()
        bump_state.finish_formal_merge(self.forum, candidate["candidate_id"], success=True,
                                      main_sha="f" * 40, verification=verification)
        state = bump_state.load_state(self.forum)
        task = state["formal_tasks"]["alpha"]
        self.assertEqual(task["status"], "complete")
        self.assertEqual(task["verification"]["status"], "provisional")
        self.assertTrue(task["verification"]["native_pending"])
        self.assertEqual(task["migration_native_status"], "pending")
        self.assertEqual(state["formal_tasks"]["beta"]["status"], "pending")
        self.assertIsNone(state["formalization"]["review_snapshot"])
        self.assertNotEqual(state["formalization"]["status"], "accepted")

    def test_provisional_receipt_cannot_omit_pending_native_marker(self):
        candidate, verification = self.merging_candidate()
        del verification["native_pending"]
        with self.assertRaisesRegex(ValueError, "pending native verification"):
            bump_state.finish_formal_merge(self.forum, candidate["candidate_id"], success=True,
                                          main_sha="f" * 40, verification=verification)
        self.assertEqual(bump_state.load_state(self.forum)["formal_candidates"][candidate["candidate_id"]]["status"],
                         "merging")

    def test_final_acceptance_requires_original_universe_native_validator(self):
        # Controller plumbing test only: the native validator is deliberately
        # failing. Provisional repair bookkeeping must not avoid its gate.
        state = self.initialize()
        for task in state["formal_tasks"].values():
            task.update(status="complete", accepted_candidate=None,
                        verification={"status": "provisional", "native_pending": True})
        contract = state["formalization"]["contract"]
        contract["project_baseline"] = {"sha256": "e" * 64, "policy": "migration-v1"}
        contract["sha256"] = bump_state._contract_digest(contract)
        snapshot = machine_snapshot(state)
        with patch.object(bump_contract, "_baseline_matches", return_value=True), \
             patch("unity.bump_migration.validate_native_snapshot", side_effect=ValueError("native review incomplete")) as native:
            with self.assertRaisesRegex(ValueError, "native review incomplete"):
                bump_state._validate_snapshot_binding(state, snapshot, require_passed=True)
        native.assert_called_once_with(state, snapshot)


class OriginalUniverseCriticTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MigrationPlanStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        requirements = [{"id": "requirement-" + key, "statement": "Preserve original " + key,
                         "source_components": ["source:Example.lean"], "tasks": [],
                         "anchor_ids": ["anchor-" + key]} for key in ("alpha", "beta")]
        fixture.dag = informal_dag(fixture.source, [], requirements=requirements)
        fixture.dag["compiler_tasks"] = []
        occurrences = {key: {"display_name": "Example." + key, "kind": "theorem",
                             "module": "Example", "path": "Example.lean", "dependencies": [], "range": None}
                       for key in ("alpha", "beta")}
        fixture.contract.update(requirements=deepcopy(fixture.dag["requirements"]),
            spec=deepcopy(fixture.dag["spec"]), spec_sha256=bump_state.digest(fixture.dag["spec"]),
            project_baseline={"policy": "migration-v1", "sha256": "e" * 64,
                              "migration": {"original_index": {"occurrences": occurrences,
                                  "modules": {"Example": {"path": "Example.lean", "source_sha256": "b" * 64,
                                                          "occurrence_ids": sorted(occurrences)}}}},
                              "layout": {"modules": {"Example.lean": "Example"}}},
            migration_correspondences={key: {"module": "Example", "declaration": "Example." + key}
                                       for key in occurrences}, obligation_ids=[])
        fixture.contract["sha256"] = bump_state._contract_digest(fixture.contract)
        current = fixture.initialize()
        self.forum = fixture.forum
        self.snapshot = machine_snapshot(current, {"migration_review": {
            "original_occurrences": sorted(occurrences),
            "occurrence_declarations": {key: "Example." + key for key in occurrences}}})
        with bump_state.transaction(self.forum) as state:
            state["phase"] = "critic"
            state["formalization"].update(status="review", review_snapshot=deepcopy(self.snapshot))
            state["review_snapshots"][self.snapshot["snapshot_id"]] = deepcopy(self.snapshot)
        self.review = {"snapshot_id": self.snapshot["snapshot_id"], "scope_rationale": "All original occurrences.",
                       "requirements": [{"requirement_id": row["id"], "status": "pass",
                            "declarations": ["Example." + row["id"].removeprefix("requirement-")],
                            "checked_anchor_ids": row["anchor_ids"], "checked_prerequisite_ids": [],
                            "rationale": "Matches original statement.", "argument_rationale": "Matches original behavior.",
                            "repair_steps": []} for row in requirements], "repair_reviews": []}

    def reject_alpha(self, *, original="alpha"):
        review = deepcopy(self.review)
        review["requirements"][0].update(status="fail", repair_steps=["Restore the original alpha statement."])
        return bump_state.submit_critic_verdict(self.forum, "Critic", "lean_reopen", "Alpha differs.",
                                               review=review, reopen_tasks=[original])

    def test_critic_materializes_original_declaration_absent_from_repair_queue(self):
        original = bump_state.load_state(self.forum)["formalization"]["source_obligations"]
        result = self.reject_alpha()
        state = result["state"]
        self.assertEqual(set(state["formal_tasks"]), {"alpha"})
        task = state["formal_tasks"]["alpha"]
        self.assertEqual(task["migration"]["original_ids"], ["alpha"])
        self.assertEqual(task["migration"]["kind"], "declaration")
        self.assertEqual(task["status"], "pending")
        self.assertTrue(task["migration"]["critic_reopen"])
        self.assertEqual(state["formalization"]["source_obligations"], original)
        self.assertEqual(state["review_snapshots"][self.snapshot["snapshot_id"]], self.snapshot)
        self.assertEqual(result["verdict"]["original_reopen_occurrences"], ["alpha"])
        self.assertEqual(bump_state.critic_feedback_for_task(state, "alpha")["direct"][0]["repair_steps"],
                         ["Restore the original alpha statement."])

    def test_diagnostic_refresh_cannot_discard_outstanding_semantic_repair(self):
        state = self.reject_alpha()["state"]
        dag = {"chunks": list(deepcopy(state["formal_tasks"]).values()), "compiler_tasks": [],
               "requirements": state["formalization"]["requirements"], "spec": state["formalization"]["spec"]}
        current = bump_state.refresh_migration_plan(self.forum, dag,
            main_sha=state["formalization"]["main_sha"], contract=state["formalization"]["contract"])
        self.assertEqual(current["formal_tasks"]["alpha"]["status"], "pending")
        self.assertEqual(current["formalization"]["compiler_tasks"], ["alpha"])

    def test_unknown_original_reopen_is_rejected_atomically(self):
        before = bump_state.load_state(self.forum)
        with self.assertRaisesRegex(ValueError, "unknown reopen"):
            self.reject_alpha(original="not-original")
        self.assertEqual(bump_state.load_state(self.forum), before)

    def test_original_approval_coverage_is_not_limited_to_repair_tasks(self):
        # Canned controller bindings isolate semantic coverage, not native proof.
        with patch.object(bump_contract, "_baseline_matches", return_value=True), \
             patch("unity.bump_migration.validate_native_snapshot"):
            result = bump_state.submit_critic_verdict(self.forum, "Critic", "approved", "Every original checked.",
                                                      review=self.review)
        self.assertEqual(result["state"]["formalization"]["status"], "approval_pending")
        self.assertEqual(result["state"]["formal_tasks"], {})

    def test_empty_repair_queue_does_not_allow_omitted_original_review(self):
        review = deepcopy(self.review)
        review["requirements"].pop()
        with patch.object(bump_contract, "_baseline_matches", return_value=True), \
             patch("unity.bump_migration.validate_native_snapshot"):
            with self.assertRaisesRegex(ValueError, "exact coverage"):
                bump_state.submit_critic_verdict(self.forum, "Critic", "approved", "Incomplete review.", review=review)

    def test_original_declaration_still_requires_nonempty_review_references(self):
        review = deepcopy(self.review)
        review["requirements"][0]["declarations"] = []
        with patch.object(bump_contract, "_baseline_matches", return_value=True), \
             patch("unity.bump_migration.validate_native_snapshot"):
            with self.assertRaisesRegex(ValueError, "declaration references"):
                bump_state.submit_critic_verdict(self.forum, "Critic", "approved", "Missing declaration.", review=review)


class EmptyModuleCriticTests(unittest.TestCase):
    """Saved controller-evidence gate tests; canned receipts are not native proof."""

    def setUp(self):
        from unity.bump_migration import POLICY, coverage
        from unity.bump_planner import empty_module_commands
        self.index = {"occurrences": {}, "modules": {"Root": {"path": "Root.lean", "source_sha256": "b" * 64,
                     "occurrence_ids": [], "line_lengths": [14]}}}
        commands = empty_module_commands(self.index)
        self.key = next(iter(commands))
        ref = "source:project/Root.lean"
        requirement = {"id": "requirement-" + self.key, "statement": "Preserve the original import-only module.",
                       "source_components": [ref], "tasks": [], "anchor_ids": ["anchor-" + self.key]}
        spec = {"anchors": [{"id": "anchor-" + self.key, "source_ref": ref,
                             "location": "Root.lean", "excerpt": "import Fixture"}],
                "scope": {"targets": ["anchor-" + self.key], "references": [], "excluded": []},
                "arguments": [{"requirement_id": requirement["id"], "prerequisites": [], "repair_ids": []}],
                "prerequisites": []}
        baseline = {"sha256": "e" * 64, "policy": "migration-v1", "project_scope": "build",
                    "migration": {"scope": {"sha256": "f" * 64}, "original_index": self.index,
                                  "selected_modules": {"Root": "Root.lean"}, "excluded_files": []}}
        frozen = {"version": 3, "migration_policy": 1, "project_baseline": baseline,
                  "requirements": [requirement], "migration_correspondences": {}}
        native = {"policy": POLICY, "native_complete": True, "original_occurrences": [], "verified_occurrences": [],
                  "occurrence_declarations": {}, "empty_module_commands": commands, "scope_sha256": "f" * 64,
                  "correspondences_sha256": bump_state.digest({}), "helper_modules": [],
                  "native_reports": [{"module": "Root", "side": side, "artifact_id": "canned-" + side,
                                      "sha256": "c" * 64} for side in ("original", "target")]}
        self.state = {"input_source": {"kind": "supplied_sources", "candidate_id": "source-" + "a" * 64,
                       "sha256": "a" * 64, "source_refs": [{"ref_id": ref, "path": ".unity/source/project/Root.lean",
                                                           "sha256": "b" * 64}]},
                      "formal_tasks": {}, "source_repairs": {},
                      "formalization": {"contract": frozen, "requirements": [requirement], "spec": spec,
                           "review_snapshot": {"passed": True, "declarations": {}, "compiled_receipt": {"canned": True},
                                               "project_verification": coverage(baseline), "migration_review": native}}}
        self.review = {"scope_rationale": "Import-only original module has no native declarations.",
                       "requirements": [{"requirement_id": requirement["id"], "status": "pass", "declarations": [],
                            "checked_anchor_ids": requirement["anchor_ids"], "checked_prerequisite_ids": [],
                            "rationale": "Original command source and imports preserved.",
                            "argument_rationale": "Both native module inspections have empty local inventory."}],
                       "repair_reviews": []}

    def test_native_empty_module_command_requires_no_fabricated_declaration_reference(self):
        bump_state._validate_semantic_review(self.state, self.review, approved=True, author="Critic")

    def test_empty_command_still_requires_exact_review_coverage(self):
        self.review["requirements"] = []
        with self.assertRaisesRegex(ValueError, "exact coverage"):
            bump_state._validate_semantic_review(self.state, self.review, approved=True, author="Critic")

    def test_command_exception_rejects_a_nonempty_original_declaration_inventory(self):
        self.index["occurrences"]["original-declaration"] = {"module": "Root"}
        with self.assertRaisesRegex(ValueError, "contains original declarations"):
            bump_state._validate_semantic_review(self.state, self.review, approved=True, author="Critic")

    def test_command_exception_requires_paired_native_module_evidence(self):
        self.state["formalization"]["review_snapshot"]["migration_review"]["native_reports"].pop()
        with self.assertRaisesRegex(ValueError, "native migration acceptance"):
            bump_state._validate_semantic_review(self.state, self.review, approved=True, author="Critic")

    def test_command_exception_rejects_stale_native_map_or_source_anchor(self):
        for defect in ("native_map", "source_hash", "anchor"):
            with self.subTest(defect=defect):
                current = deepcopy(self.state)
                if defect == "native_map":
                    current["formalization"]["review_snapshot"]["migration_review"]["empty_module_commands"] = {}
                elif defect == "source_hash":
                    current["input_source"]["source_refs"][0]["sha256"] = "d" * 64
                else:
                    current["formalization"]["spec"]["anchors"][0]["source_ref"] = "different-source"
                with self.assertRaises(ValueError):
                    bump_state._validate_semantic_review(current, self.review, approved=True, author="Critic")

    def test_critic_cannot_create_whole_empty_module_task_without_command_location(self):
        before = deepcopy(self.state)
        with self.assertRaisesRegex(ValueError, "existing bounded command task"):
            bump_state._materialize_migration_reopen(self.state, [self.key])
        self.assertEqual(self.state, before)

    def test_critic_command_obligation_reopens_only_its_existing_bounded_task(self):
        task = {"task_id": "bounded-command", "status": "complete",
                "migration": {"kind": "command", "original_ids": [], "command_obligation": self.key,
                              "module": "Root", "path": "Root.lean", "command_line": 1}}
        self.state["formal_tasks"]["bounded-command"] = task
        self.state["formalization"]["requirements"][0]["tasks"] = ["bounded-command"]
        mapped, routes = bump_state._materialize_migration_reopen(self.state, [self.key])
        self.assertEqual(mapped, ["bounded-command"])
        self.assertEqual(routes, {self.key: ["bounded-command"]})
        self.assertEqual(set(self.state["formal_tasks"]), {"bounded-command"})
        task["migration"]["command_line"] = 2
        with self.assertRaisesRegex(ValueError, "existing bounded command task"):
            bump_state._materialize_migration_reopen(self.state, [self.key])


if __name__ == "__main__":
    unittest.main()
