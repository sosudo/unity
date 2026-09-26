"""Manifest repair scheduling regressions; no models, Lean, or remote services."""

import asyncio
import hashlib
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from unity import autoformalize_runtime as runtime


def repair_record():
    return {"repair_id": "repair-1", "input_sha256": "input-1", "task_id": "root",
            "kind": "output_manifest_changed", "context": {}, "attempts": [], "status": "open",
            "blockers": [{"code": "output_manifest_changed", "task_ids": ["root"],
                          "message": "The adopted manifest differs.",
                          "required_action": "Explicitly reopen the representation."}]}


class ManifestRuntimeUnitTests(unittest.TestCase):
    def test_candidate_preflight_receives_exact_manifest(self):
        outputs = [{"declaration": "Witness", "file": "Witness.lean"}]
        candidate = {"task_id": "root", "stage": "complete", "outputs": outputs}
        with patch.object(runtime.autoformalize_state, "submission_blockers", return_value=[]) as check:
            self.assertEqual(runtime._candidate_preflight({}, candidate), [])
        check.assert_called_once_with({}, "root", "complete", outputs=outputs)

    def test_integration_preflight_precedes_git_and_build(self):
        state = {"formalization": {"contract": {"version": 3}}}
        blocker = {"message": "The adopted manifest differs."}
        with patch.object(runtime.autoformalize_state, "load_state", return_value=state), \
             patch.object(runtime, "require_source_matches"), \
             patch.object(runtime.autoformalize_state, "candidate_is_current", return_value=True), \
             patch.object(runtime, "_candidate_preflight", return_value=[blocker]), \
             patch.object(runtime.worktree, "verify_candidate_commit") as git, \
             patch.object(runtime.autoformalize_contract, "build_sources") as build:
            result = runtime._apply_formal_candidate(
                SimpleNamespace(project_root=Path("/unused"), forum=Path("/unused")), {}, {},
            )
        self.assertEqual(result["verification"]["mode"], "preflight")
        git.assert_not_called()
        build.assert_not_called()

    def test_repair_survives_rejection_while_opportunistic_nudge_is_suppressed(self):
        prompt = runtime._compose_formal_task_prompt(
            recovery="RECOVER REJECTED CANDIDATE", resume="resume", followup="Submission check only",
            normal="normal", representation="representation instructions", repair=repair_record(),
        )
        for text in ("MANIFEST REPAIR", "RECOVER REJECTED CANDIDATE", "normal",
                     "reopen_representations", "Preserve existing Lean", "representation instructions"):
            self.assertIn(text, prompt)
        self.assertNotIn("Submission check only", prompt)

    def test_nudge_without_rejection_keeps_dedicated_repair_instructions(self):
        prompt = runtime._compose_formal_task_prompt(
            recovery="", resume="resume", followup="Submission check only",
            normal="normal", representation="representation instructions", repair=repair_record(),
        )
        self.assertIn("MANIFEST REPAIR", prompt)
        self.assertIn("Submission check only", prompt)
        self.assertIn("resume", prompt)

    def test_scope_includes_transitive_dependents_not_unrelated_work(self):
        state = {"formal_tasks": {"root": {}, "dependent": {"dependencies": ["root"]},
                                  "last": {"dependencies": ["dependent"]}, "unrelated": {}}}
        self.assertEqual(runtime._manifest_repair_scope(state, repair_record()), {"root", "dependent", "last"})

    def test_pending_current_candidate_wins_over_repair(self):
        state = {"formal_tasks": {"root": {}, "dependent": {"dependencies": ["root"]}},
                 "formal_candidates": {"candidate": {"task_id": "dependent", "status": "submitted"}}}
        with patch.object(runtime.autoformalize_state, "candidate_is_current", return_value=True):
            self.assertTrue(runtime._manifest_repair_pending_candidate(state, repair_record()))
        with patch.object(runtime.autoformalize_state, "candidate_is_current", return_value=False):
            self.assertFalse(runtime._manifest_repair_pending_candidate(state, repair_record()))

    def test_overlapping_diagnostics_do_not_cancel_current_owner(self):
        first, second = repair_record(), repair_record()
        second.update(repair_id="repair-2", input_sha256="input-2")
        state = {"formal_tasks": {"root": {}, "dependent": {"dependencies": ["root"]}}}
        self.assertEqual(runtime._select_manifest_repairs(
            state, [first, second], {("repair-2", "input-2")},
        ), [second])
        first["status"] = "exhausted"
        self.assertEqual(runtime._select_manifest_repairs(state, [first, second], set()), [second])


class ManifestRuntimeSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = SimpleNamespace(project_root=self.root, forum=self.root, unity=self.root)
        self.state = {"phase": "formalizing", "run_id": "test", "events": [], "worker_tasks": {},
                      "formalization": {"main_sha": "main", "revision": 1}, "formal_candidates": {},
                      "formal_tasks": {"root": {"task_id": "root", "status": "pending", "revision": 1}},
                      "strategies": {}, "retired_tasks": {}, "manifest_repairs": {}}
        self.yielded = set()
        self.names = ["Ada", "Bert"]
        self.finishes = []
        self.rounds = []

    def current_repairs(self, state, task_id=""):
        return [deepcopy(row) for row in state["manifest_repairs"].values()
                if row["status"] in {"open", "exhausted"} and (not task_id or row["task_id"] == task_id)]

    def begin(self, _forum, repair_id, author, *, input_sha256=None):
        repair = self.state["manifest_repairs"][repair_id]
        if any(row["author"] == author.casefold() for row in repair["attempts"]):
            return {"status": "attempted", "repair": deepcopy(repair)}
        if any(row["status"] == "started" for row in repair["attempts"]):
            return {"status": "conflict", "repair": deepcopy(repair)}
        repair["attempts"].append({"author": author.casefold(), "status": "started"})
        return {"status": "started", "repair": deepcopy(repair)}

    def finish(self, _forum, repair_id, author, outcome):
        self.finishes.append((author.casefold(), outcome))
        for row in self.state["manifest_repairs"][repair_id]["attempts"]:
            if row["author"] == author.casefold() and row["status"] == "started":
                row["status"] = outcome

    async def run_runtime(self, worker, *, prepare=None, real_eligibility=False, real_repair_state=False,
                          source_issues=None, source_worker=None):
        agents = [SimpleNamespace(name=name, backend="claude") for name in self.names]
        roster = SimpleNamespace(agents=agents)
        ss = runtime.autoformalize_state
        fs = runtime.autoformalize_server
        with ExitStack() as stack:
            def mock(obj, name, **kwargs):
                return stack.enter_context(patch.object(obj, name, create=True, **kwargs))
            for name in ("configure_forum", "require_source_matches"):
                mock(runtime, name)
            mock(runtime, "stop_requested", return_value=False)
            mock(runtime, "_formal_worktree", return_value=self.root)
            mock(runtime, "_git", return_value=SimpleNamespace(stdout="", returncode=0))
            mock(runtime, "_agent_runtime_env", return_value={})
            mock(runtime, "_formal_launch_retry_key", return_value="retry")
            mock(runtime, "_preamble", return_value="")
            mock(runtime, "load_prompt", return_value="")
            mock(runtime, "forum_brief", return_value="")
            mock(runtime, "_rejection_recovery_prompt", return_value="RECOVER REJECTED CANDIDATE")
            mock(runtime, "spawn", side_effect=worker)
            mock(runtime.library, "library_context", return_value="")
            mock(runtime.library, "library_subagents", return_value=[])
            for name in ("symlink_lake_cache", "link_runtime_state", "cleanup_worktree"):
                mock(runtime.worktree, name)
            mock(runtime.autoformalize_jobs, "terminate")
            mock(runtime.autoformalize_representation, "recover_representation_reviews")
            mock(runtime.autoformalize_representation, "pending_representation_reviews", return_value=[])
            if not real_repair_state:
                mock(ss, "load_state", side_effect=lambda *_: deepcopy(self.state))
                mock(ss, "reconcile_rejected_representations", side_effect=lambda *_: deepcopy(self.state))
                mock(ss, "current_manifest_repairs", side_effect=self.current_repairs)
                mock(ss, "begin_manifest_repair_attempt", side_effect=self.begin)
                mock(ss, "finish_manifest_repair_attempt", side_effect=self.finish)
                mock(ss, "mark_manifest_repair_exhausted", side_effect=lambda _, key:
                     self.state["manifest_repairs"][key].update(status="exhausted"))
                mock(ss, "repair_available_to", side_effect=lambda state, name, task, *_:
                     ss.task_available_to(state, name, task))
            if not real_eligibility:
                mock(ss, "ready_formal_tasks", side_effect=lambda state:
                     [row for row in state["formal_tasks"].values() if row["status"] == "pending"])
                mock(ss, "task_available_to", side_effect=lambda state, name, task:
                     task in state["formal_tasks"] and state["formal_tasks"][task]["status"] == "pending"
                     and (name, task) not in self.yielded)
            if not real_repair_state:
                mock(ss, "source_issues_blocking_task", return_value=[])
            mock(ss, "ready_source_issues", return_value=source_issues or [])
            if source_worker is not None:
                from unity import autoformalize_repairs
                mock(autoformalize_repairs, "source_repair_turn", side_effect=source_worker)
            mock(ss, "open_source_issues", return_value=[])
            mock(ss, "pending_replan", return_value=None)
            if real_repair_state:
                pass  # Real persisted attempt/yield/currentness gates in the regression below.
            elif real_eligibility:
                mock(ss, "record_worker_yield", side_effect=lambda _, name, task, reason, *, snapshot:
                     ss._record_yield(self.state, name, task, reason, [], snapshot))
            else:
                mock(ss, "has_yielded", side_effect=lambda _, name, task: (name, task) in self.yielded)
                mock(ss, "snapshot_attempt", side_effect=lambda _, name, task: {"task_id": task})
                mock(ss, "record_worker_yield", side_effect=lambda _, name, task, *args, **kwargs:
                     self.yielded.add((name, task)))
            def end_round(*args, **kwargs):
                self.rounds.append(deepcopy(kwargs))
                return deepcopy(self.state)
            mock(ss, "record_round_end", side_effect=end_round)
            mock(ss, "all_formal_tasks_complete", return_value=False)
            mock(fs, "has_pending_formal_candidate", return_value=False)
            if not real_eligibility:
                mock(fs, "unresolved_formal_tasks", return_value=[])
            mock(fs, "verification_blockers", return_value=[])
            mock(fs, "prepare_formal_worktree", side_effect=prepare or (lambda *args, **kwargs: {"ok": True}))
            return await asyncio.wait_for(runtime.run_formalizing_runtime(roster, self.paths, {}, "formalize"), 4)

    async def test_cached_rejected_interface_launches_only_root_owner_with_real_gates(self):
        from test_autoformalize_manifest_repair import ManifestStateFixture
        fixture = ManifestStateFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.paths.forum = fixture.forum
        ss = runtime.autoformalize_state
        with ss.transaction(fixture.forum) as state:
            contract = state["formalization"]["contract"]
            contract["representation_review_policy"] = 1
            contract["bindings"]["beta"] = [{"declaration": "Example.beta", "file": "Example/beta.lean"}]
            contract["targets"]["Example.beta"] = {"fingerprint": ss.digest("Example.beta")}
            contract["sha256"] = ss._contract_digest(contract)
            state["worker_tasks"] = {"ada": "beta", "bert": "alpha"}
            # Proof-only dependencies must stay blocked, too.
            state["formal_tasks"]["beta"]["statement_dependencies"] = []
            state["formal_tasks"]["beta"]["proof_dependencies"] = ["alpha"]
            state["formal_tasks"]["beta"]["representation"] = {"status": "adopted"}
            state["formal_tasks"]["beta"]["outputs"] = contract["bindings"]["beta"]
            for strategy in state["strategies"].values():
                strategy["status"] = "cancelled"
            row = runtime.autoformalize_representation.queue_representation_review(state, "alpha")
            state["representation_reviews"][row["input_sha256"]]["status"] = "encoding_error"
            dependent_review = runtime.autoformalize_representation.queue_representation_review(state, "beta")
            state["representation_reviews"][dependent_review["input_sha256"]]["status"] = "aligned"
        before = ss.load_state(fixture.forum)
        self.assertFalse(ss.task_ready(before, before["formal_tasks"]["alpha"]))
        self.assertFalse(ss.task_ready(before, before["formal_tasks"]["beta"]))
        aligned = deepcopy(before)
        aligned["representation_reviews"][row["input_sha256"]]["status"] = "aligned"
        self.assertTrue(ss.task_ready(aligned, aligned["formal_tasks"]["beta"]))
        self.assertEqual(ss.current_manifest_repairs(before), [])
        launched, prepared, budgets = [], [], []

        def prepare(author, **kwargs):
            current = ss.load_state(fixture.forum)
            self.assertTrue(ss.repair_available_to(
                current, author, kwargs["next_task"], kwargs["repair_id"], kwargs["repair_input_sha256"],
            ))
            prepared.append(author)
            repair = ss.current_manifest_repairs(current, "alpha")[0]
            budgets.append(repair["budget_sha256"])
            if len(prepared) == 1:
                # Simulate an unrelated accepted merge during preparation.
                # The exact permission must fail post-prepare, then refresh
                # without losing the rejection or resetting its retry budget.
                with ss.transaction(fixture.forum) as state:
                    state["formalization"]["main_sha"] = "f" * 40
            return {"ok": True}

        async def worker(agent, _system, prompt, *_args, **kwargs):
            launched.append((agent.name, kwargs["log_context"]["task_id"]))
            self.assertIn("MANIFEST REPAIR", prompt)
            self.assertIn("Supplied documents are read-only", prompt)
            self.assertIn("submit_source_repair with evidence", prompt)
            self.assertNotIn("propose_source_fix", prompt)
            self.assertNotIn("reopen_solving", prompt)
            current = ss.load_state(fixture.forum)
            repair = ss.current_manifest_repairs(current, "alpha")[0]
            self.assertFalse(ss.repair_available_to(
                current, "Ada", "alpha", repair["repair_id"], repair["input_sha256"],
            ))
            self.assertFalse(ss.interface_available(current, current["formal_tasks"]["alpha"]))
            self.assertFalse(ss.task_ready(current, current["formal_tasks"]["beta"]))
            # End the offline worker stub after observing its exclusive lease;
            # no acceptance or proof success is synthesized.
            with ss.transaction(fixture.forum) as state:
                state["phase"] = "critic"

        await self.run_runtime(worker, prepare=prepare, real_eligibility=True, real_repair_state=True)
        self.assertEqual(launched, [("Bert", "alpha")])
        self.assertEqual(prepared, ["Bert", "Bert"])
        self.assertEqual(len(set(budgets)), 1)

    async def test_one_author_per_input_then_exhaustion_without_generic_relaunch(self):
        self.state["manifest_repairs"]["repair-1"] = repair_record()
        launched = []
        active = 0
        maximum = 0

        async def worker(agent, _system, prompt, *_args, **_kwargs):
            nonlocal active, maximum
            self.assertIn("MANIFEST REPAIR", prompt)
            self.assertIn("RECOVER REJECTED CANDIDATE", prompt)
            launched.append(agent.name)
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1

        await self.run_runtime(worker)
        self.assertEqual(launched, ["Ada", "Bert"])
        self.assertEqual(maximum, 1)
        self.assertEqual(self.state["manifest_repairs"]["repair-1"]["status"], "exhausted")
        self.assertEqual(self.rounds[-1]["activity"], {
            "worker_launches": 2, "integrations": 0, "review_launches": 0, "source_repairs": 0,
        })

    async def test_restart_consumes_interrupted_attempt_and_uses_next_author(self):
        repair = repair_record()
        repair["attempts"] = [{"author": "ada", "status": "started"}]
        self.state["manifest_repairs"]["repair-1"] = repair
        launched = []

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)

        await self.run_runtime(worker)
        self.assertEqual(launched, ["Bert"])
        self.assertIn(("ada", "interrupted"), self.finishes)

    async def test_real_claim_and_yield_gates_allow_next_repair_author(self):
        self.state["manifest_repairs"]["repair-1"] = repair_record()
        self.state["strategies"]["strategy"] = {
            "strategy_id": "strategy", "phase": "formalizing", "target": "root",
            "task_revision": 1, "status": "claimed", "owner": "Ada", "assistants": [],
            "description_key": "repair-approach",
        }
        launched = []

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)
            if agent.name == "Bert":
                self.assertEqual(self.state["strategies"]["strategy"]["status"], "registered")
                self.assertEqual(runtime.autoformalize_server.unresolved_formal_tasks(self.state, "Ada"), [])

        await self.run_runtime(worker, real_eligibility=True)
        self.assertEqual(launched, ["Ada", "Bert"])
        self.assertEqual(self.state["manifest_repairs"]["repair-1"]["status"], "exhausted")

    async def test_preparation_race_does_not_launch_stale_repair(self):
        self.state["manifest_repairs"]["repair-1"] = repair_record()
        launched = []

        def prepare(*_args, **_kwargs):
            self.state["manifest_repairs"]["repair-1"]["status"] = "cleared"
            self.state["formal_tasks"]["root"]["status"] = "complete"
            return {"ok": True}

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)

        await self.run_runtime(worker, prepare=prepare)
        self.assertEqual(launched, [])
        self.assertEqual(self.state["manifest_repairs"]["repair-1"]["attempts"], [])
        self.assertFalse(any(self.rounds[-1]["activity"].values()))

    async def test_blocked_worktree_is_not_recorded_as_a_dispatched_attempt(self):
        self.state["manifest_repairs"]["repair-1"] = repair_record()
        launched = []

        async def worker(agent, *_args, **_kwargs):
            launched.append(agent.name)

        await self.run_runtime(worker, prepare=lambda *_args, **_kwargs: {
            "ok": False, "error": "unresolved_work", "reason": "Private unrelated work remains.",
        })
        self.assertEqual(launched, [])
        self.assertEqual(self.state["manifest_repairs"]["repair-1"]["attempts"], [])
        self.assertEqual(set(self.rounds[-1]["blocked_launches"]), {"Ada", "Bert"})
        self.assertFalse(any(self.rounds[-1]["activity"].values()))

    async def test_source_repair_dispatch_preserves_exhaustion_flow_and_counts_activity(self):
        self.names = ["Ada"]
        self.yielded.add(("Ada", "root"))
        repaired, formalized = [], []

        async def source_worker(agent, _roster, _paths, issue_id, _limit, **_kwargs):
            repaired.append((agent.name, issue_id))
            return {"status": "exhausted"}

        async def formal_worker(agent, *_args, **_kwargs):
            formalized.append(agent.name)

        with patch.object(runtime.autoformalize_state, "mark_source_issue_unresolved") as unresolved:
            await self.run_runtime(formal_worker, source_issues=[{"issue_id": "source-gap"}],
                                   source_worker=source_worker)
        self.assertEqual(repaired, [("Ada", "source-gap")])
        self.assertEqual(formalized, [])
        unresolved.assert_called_once_with(
            self.paths.forum, "source-gap", "Every configured agent exhausted its source-repair attempts",
        )
        self.assertEqual(self.rounds[-1]["activity"], {
            "worker_launches": 0, "integrations": 0, "review_launches": 0, "source_repairs": 1,
        })

    async def test_new_repair_drains_dependents_preserving_unrelated_worker_and_source(self):
        self.names.append("Cleo")
        self.state["formal_tasks"].update({
            "dependent": {"task_id": "dependent", "status": "pending", "revision": 1, "dependencies": ["root"]},
            "other": {"task_id": "other", "status": "pending", "revision": 1},
        })
        private = self.root / "private.lean"
        private.write_text("retained private proof")
        active = set()
        cancelled = set()
        repaired = asyncio.Event()

        async def worker(agent, _system, prompt, *_args, **kwargs):
            target = kwargs["log_context"]["task_id"]
            if "MANIFEST REPAIR" in prompt:
                self.assertEqual(active, {"other"})
                self.assertEqual(private.read_text(), "retained private proof")
                self.state["manifest_repairs"]["repair-1"]["status"] = "cleared"
                for task in ("root", "dependent"):
                    self.state["formal_tasks"][task]["status"] = "complete"
                repaired.set()
                return
            active.add(target)
            if len(active) == 3:
                self.state["manifest_repairs"]["repair-1"] = repair_record()
            try:
                if target == "other":
                    await repaired.wait()
                    self.state["formal_tasks"][target]["status"] = "complete"
                else:
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(target)
                raise
            finally:
                active.discard(target)

        await self.run_runtime(worker)
        self.assertEqual(cancelled, {"root", "dependent"})
        self.assertTrue(repaired.is_set())
        self.assertEqual(private.read_text(), "retained private proof")


class ManifestRuntimePersistedIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Real Git, source snapshot, scheduler, review/state and merge publication.

    Only provider dispatch and the expensive compiler-check boundary are stubbed;
    no acceptance is manufactured by the runtime cases below.
    """

    def setUp(self):
        from unity.autoformalize_input import autoformalize_paths, snapshot_sources
        from unity.config import Paths
        from test_autoformalize_manifest_repair import informal_dag

        temporary = tempfile.TemporaryDirectory(prefix="autoformalize-runtime-state-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = autoformalize_paths(Paths.from_unity_dir(self.root / ".unity"))
        (self.paths.unity / "source").mkdir(parents=True)
        (self.root / ".lake/packages").mkdir(parents=True)
        (self.root / ".gitignore").write_text(".unity/\n.lake/\n")
        for task in ("alpha", "beta"):
            (self.root / f"{task.title()}.lean").write_text(f"theorem {task} : True := by sorry\n")
        self.paths.unity_md.write_text("Faithfully formalize the supplied claims.\n")
        self.source_file = self.paths.unity / "source/claims.md"
        self.source_file.write_text("Alpha holds. Beta follows from Alpha.\n")
        self.source_bytes = self.source_file.read_bytes()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Unity Test")
        self.git("config", "user.email", "unity@example.test")
        self.git("add", ".")
        self.git("commit", "-qm", "initial test scaffold")
        self.base = self.git("rev-parse", "HEAD")
        self.source = snapshot_sources(self.paths)
        ss = runtime.autoformalize_state
        ss.initialize_source(self.paths.forum, hashlib.sha256(self.paths.unity_md.read_bytes()).hexdigest(),
                             self.base, self.source)
        chunks = [{"id": task, "lean_decl": task, "lean_file": f"{task.title()}.lean",
                   "source_components": [self.source["source_refs"][0]["ref_id"]],
                   "dependencies": [] if task == "alpha" else ["alpha"]}
                  for task in ("alpha", "beta")]
        dag = informal_dag(self.source, chunks)
        dag["chunks"][1]["statement_dependencies"] = ["alpha"]
        contract = {"version": 3, "representation_review_policy": 1,
                    "bindings": {}, "targets": {}, "external_declarations": {},
                    "prerequisite_declarations": {}, "environment": {},
                    "solution_candidate": self.source["candidate_id"], "solution_sha256": self.source["sha256"],
                    "requirements": deepcopy(dag["requirements"]), "spec": deepcopy(dag["spec"]),
                    "spec_sha256": ss.digest(dag["spec"]), "obligation_ids": ["alpha", "beta"]}
        contract["sha256"] = ss._contract_digest(contract)
        ss.initialize_informal_plan(self.paths.forum, dag, main_sha=self.base, contract=contract)
        agents = [SimpleNamespace(name=name, backend="claude", model="offline-test", is_primary=name == "Ada")
                  for name in ("Ada", "Bert")]
        self.roster = SimpleNamespace(agents=agents, primary=agents[0])

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True, capture_output=True,
                              check=True).stdout.strip()

    def current(self):
        return runtime.autoformalize_state.load_state(self.paths.forum)

    def candidate(self, task_id, author="Ada"):
        ss = runtime.autoformalize_state
        strategy = ss.register_strategy(self.paths.forum, author, "formalize " + task_id,
                                        target=task_id)["strategy"]
        ss.claim_strategy(self.paths.forum, strategy["strategy_id"], author)
        return ss.submit_formal_candidate(
            self.paths.forum, strategy["strategy_id"], author, task_id, self.base,
            self.base, hashlib.sha256(b"").hexdigest(), stage="representation",
            outputs=[{"declaration": task_id, "file": f"{task_id.title()}.lean"}],
        )["candidate"]

    def adopt_alpha_fixture(self, *, align=False):
        """Seed a checked representation fixture, never a completed proof."""
        ss = runtime.autoformalize_state
        candidate = self.candidate("alpha")
        ss.begin_formal_merge(self.paths.forum, candidate["candidate_id"])
        proposed = deepcopy(self.current()["formalization"]["contract"])
        proposed["bindings"]["alpha"] = candidate["outputs"]
        proposed["targets"]["alpha"] = {"fingerprint": ss.digest("alpha : True"),
                                        "meaning_dependencies": ["alpha"]}
        proposed["sha256"] = ss._contract_digest(proposed)
        verification = {"status": "passed", "contract_sha256": proposed["sha256"],
                        "policy_sha256": runtime.autoformalize_contract.policy_hash(), "verified_targets": {}}
        ss.finish_formal_merge(self.paths.forum, candidate["candidate_id"], success=True,
                               main_sha=self.base, verification=verification, proposed_contract=proposed)
        if align:
            representation = runtime.autoformalize_representation
            self.assertEqual(representation.claim_representation_review(
                self.paths.forum, "alpha", "FixtureReviewer")["status"], "claimed")
            self.submit_review("FixtureReviewer", "alpha", "aligned")

    def submit_review(self, author, task_id, verdict):
        representation = runtime.autoformalize_representation
        payload = representation.representation_review_input(self.current(), task_id)
        return representation.submit_representation_review(self.paths.forum, author, task_id, {
            "input_sha256": payload["input_sha256"], "verdict": verdict,
            "checked_anchor_ids": [row["id"] for row in payload["anchors"]],
            "rationale": "Offline fixture review of the exact supplied claim.",
            "evidence": "The named declaration is compared with the frozen source anchor.",
        })

    async def run_persisted(self, provider, *, compiler_result=None):
        from unity import autoformalize_spawn
        with ExitStack() as stack:
            # Providers and optional library prompt context are external inputs;
            # all eligibility, source checks, Git preparation and state remain real.
            stack.enter_context(patch.object(runtime, "spawn", side_effect=provider))
            stack.enter_context(patch.object(autoformalize_spawn, "spawn", side_effect=provider))
            stack.enter_context(patch.object(runtime.library, "library_context", return_value=""))
            stack.enter_context(patch.object(runtime.library, "library_subagents", return_value=[]))
            if compiler_result:
                stack.enter_context(patch.object(runtime, "_integrate_checked", side_effect=compiler_result))
            return await asyncio.wait_for(
                runtime.run_formalizing_runtime(self.roster, self.paths, {}, "formalize supplied source"), 8,
            )

    async def test_representation_reviewer_dispatch_is_counted_with_real_claim_and_review(self):
        self.adopt_alpha_fixture()
        calls = []

        async def provider(agent, _system, _prompt, *_args, **kwargs):
            context = kwargs["log_context"]
            calls.append((agent.name, context["role"], context["task_id"]))
            self.assertEqual(context["role"], "representation_review")
            self.assertEqual(runtime.autoformalize_representation.current_representation_review(
                self.current(), "alpha")["owner"], agent.name)
            self.submit_review(agent.name, "alpha", "uncertain")

        result = await self.run_persisted(provider)
        self.assertEqual(calls, [("Bert", "representation_review", "alpha")])
        self.assertEqual(result["formalization"]["last_round"]["activity"], {
            "worker_launches": 0, "integrations": 0, "review_launches": 1, "source_repairs": 0,
        })
        self.assertEqual(result["formalization"]["last_round"]["outcome"], "attempted")
        self.assertEqual(result["phase"], "formalizing")
        self.assertIsNone(result["formal_tasks"]["alpha"]["accepted_candidate"])
        self.assertFalse(runtime.autoformalize_state.task_ready(result, "beta"))
        self.assertEqual(self.source_file.read_bytes(), self.source_bytes)

    async def test_queued_dependent_integrates_before_repair_and_records_actual_dispatch(self):
        self.adopt_alpha_fixture(align=True)
        ss = runtime.autoformalize_state
        candidate = self.candidate("beta", "Bert")
        with ss.transaction(self.paths.forum) as state:
            ss._record_manifest_repair(state, "alpha", [{
                "code": "output_manifest_changed", "task_ids": ["alpha"],
                "message": "The adopted output manifest must be revised explicitly.",
                "required_action": "Preserve existing work and refine the representation.",
            }], origin="preflight")
        repair = ss.current_manifest_repairs(self.current(), "alpha")[0]
        self.assertTrue(runtime._manifest_repair_pending_candidate(self.current(), repair))
        calls = []

        def compiler_result(paths, submitted, task):
            current = self.current()
            self.assertEqual(current["formal_candidates"][candidate["candidate_id"]]["status"], "merging")
            self.assertEqual(current["manifest_repairs"][repair["repair_id"]]["attempts"], [])
            self.assertEqual(calls, [])
            calls.append(("integration", submitted["task_id"]))
            # A controlled compiler failure exercises the real merge-result
            # publication without faking accepted proof or semantic evidence.
            return {"ok": False, "error": "offline compiler fixture rejected beta"}

        async def provider(agent, _system, prompt, *_args, **kwargs):
            current = self.current()
            self.assertEqual(kwargs["log_context"]["role"], "formalizer")
            self.assertEqual(kwargs["log_context"]["task_id"], "alpha")
            self.assertIn("MANIFEST REPAIR", prompt)
            self.assertEqual(current["formal_candidates"][candidate["candidate_id"]]["status"], "failed")
            self.assertEqual(calls[0], ("integration", "beta"))
            calls.append(("repair", agent.name))

        result = await self.run_persisted(provider, compiler_result=compiler_result)
        self.assertEqual(calls, [("integration", "beta"), ("repair", "Ada"), ("repair", "Bert")])
        self.assertEqual(result["formalization"]["last_round"]["activity"], {
            "worker_launches": 2, "integrations": 1, "review_launches": 0, "source_repairs": 0,
        })
        self.assertEqual(result["manifest_repairs"][repair["repair_id"]]["status"], "exhausted")
        self.assertEqual(result["formal_candidates"][candidate["candidate_id"]]["status"], "failed")
        self.assertEqual(result["phase"], "formalizing")
        self.assertIsNone(result["formal_tasks"]["alpha"]["accepted_candidate"])
        self.assertEqual(self.source_file.read_bytes(), self.source_bytes)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)


if __name__ == "__main__":
    unittest.main()
