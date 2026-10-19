"""Offline Optimize coordination contracts: no model, Lean, Lake or evaluation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import optimize_metrics as metrics, optimize_tasks as tasks
from unity.forum import optimize_server as forum


def seal(value):
    return {**value, "sha256": tasks.digest(value)}


class OptimizeStateForumTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.root = self.temp / "project"
        self.root.mkdir()
        self.directory = self.root / ".unity" / "forum" / "optimize"
        self.metric = seal({"name": "length", "builtin": "length", "direction": "lower",
                            "units": "tactic invocations", "definition": "Count tactics"})
        self.identity = seal({"main_sha": "base", "files": {"A.lean": "initial"}, "config": {}})
        self.baseline = seal({"source_identity": self.identity, "metric": self.metric,
                              "measurements": {}, "build": {"passed": True}})
        tasks.initialize(self.directory, self.root, run_id="optimize-test", base_sha="base",
                         metric=self.metric, scope={"targets": "Universe"},
                         config={"primary": "Alice", "agents": ["Alice", "Bob", "Carol"]})
        (self.root / "A.lean").write_text("theorem one : True := by trivial\ntheorem two : True := by trivial\n")
        tasks.install_inventory(self.directory, tasks.scan_sources(self.root, ["A.lean"]))
        tasks.set_baseline(self.directory, self.baseline)
        with tasks.transaction(self.directory) as state: state["phase"] = "chunking"
        self.chunks = list(tasks.load(self.directory)["chunks"])
        self.method = {"description": "Explicit tactic invocation inventory"}
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        forum.configure(self.directory, self.root, "chunking")

    def plan(self, dependencies=False):
        rows = [{"chunk_ids": [cid]} for cid in self.chunks]
        if dependencies: rows[1]["dependencies"] = [self.chunks[0]]
        proposal = tasks.propose_plan(self.directory, "Alice", rows, "Declaration-first scope")
        tasks.accept_plan(self.directory, proposal["proposal_id"])
        return tasks.load(self.directory)

    def baseline_and_round(self):
        self.plan()
        for cid in self.chunks:
            measurement = metrics.record_measurement(self.metric, subject=cid, raw_value=5,
                source_identity=self.identity, method=self.method, evidence={"tactics": 5})
            tasks.record_baseline_measurement(self.directory, "Bob", cid, measurement)
        tasks.seal_baseline(self.directory)
        tasks.begin_round(self.directory, max_rounds=2)
        forum.configure(self.directory, self.root, "optimizing")

    def assignment(self, author="Alice", chunk=None, strategy="replace long tactic chain"):
        chunk = chunk or self.chunks[0]
        row = tasks.register_strategy(self.directory, author, chunk, strategy, strategy)
        tasks.claim_strategy(self.directory, author, row["strategy_id"])
        tree = self.temp / (author + "-" + row["strategy_id"])
        tree.mkdir()
        return tasks.configure_assignment(self.directory, author, row["strategy_id"], tree, "base")

    def candidate(self, author="Alice", strategy="simplify"):
        assignment = self.assignment(author, strategy=strategy)
        return tasks.submit_candidate(self.directory, author, assignment["assignment_id"], "Retained statement; improved tactics", {})

    def test_initialization_rejects_old_or_duplicate_state(self):
        with self.assertRaisesRegex(ValueError, "already initialized"):
            tasks.initialize(self.directory, self.root, run_id="second", base_sha="other", metric="length")
        path = self.directory / tasks.STATE_FILE
        state = json.loads(path.read_text())
        state["schema"] = "legacy"
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "legacy checkpoints"):
            tasks.load(self.directory)

    def test_transaction_rollback_and_revision_guard(self):
        before = tasks.load(self.directory)
        with self.assertRaises(RuntimeError):
            with tasks.transaction(self.directory) as state:
                state["phase"] = "complete"
                raise RuntimeError("do not publish")
        self.assertEqual(tasks.load(self.directory), before)
        with self.assertRaisesRegex(ValueError, "stale state"):
            with tasks.transaction(self.directory, expected_revision=-1): pass

    def test_transactions_serialize_concurrent_events(self):
        def post(index):
            with tasks.transaction(self.directory) as state:
                tasks._event(state, "finding", "Bob", index=index)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(post, range(16)))
        state = tasks.load(self.directory)
        self.assertEqual(len(state["events"]), 16)
        self.assertEqual(len({row["event_id"] for row in state["events"]}), 16)

    def test_baseline_immutable_and_current_measurements_separate(self):
        self.baseline_and_round()
        sealed = tasks.load(self.directory)["baseline"]
        with self.assertRaisesRegex(ValueError, "baseline is immutable"):
            with tasks.transaction(self.directory) as state:
                state["baseline"]["measurements"][self.chunks[0]]["raw_value"] = 1
        with self.assertRaisesRegex(ValueError, "baseline is immutable"):
            with tasks.transaction(self.directory) as state:
                state["baseline"]["source_identity"] = {"main_sha": "new"}
        with tasks.transaction(self.directory) as state:
            state["measurements"] = {self.chunks[0]: {"raw_value": 1}}
        self.assertEqual(tasks.load(self.directory)["baseline"], sealed)
        self.assertEqual(sealed["sha256"], tasks.digest({k: v for k, v in sealed.items() if k != "sha256"}))

    def test_baseline_rejects_wrong_source_and_later_writes(self):
        measurement = metrics.record_measurement(self.metric, subject=self.chunks[0], raw_value=5,
            source_identity=seal({"main_sha": "wrong"}), method=self.method, evidence="counted five")
        with self.assertRaisesRegex(ValueError, "does not bind baseline"):
            tasks.record_baseline_measurement(self.directory, "Bob", self.chunks[0], measurement)
        self.baseline_and_round()
        with self.assertRaisesRegex(ValueError, "only collected"):
            tasks.record_baseline_measurement(self.directory, "Bob", self.chunks[0], measurement)

    def test_preseal_baseline_corrections_retain_evidence_history(self):
        self.plan()
        cid = self.chunks[0]
        first = metrics.record_measurement(self.metric, subject=cid, raw_value=7,
            source_identity=self.identity, method=self.method, evidence="Initial seven-tactic count")
        corrected = metrics.record_measurement(self.metric, subject=cid, raw_value=5,
            source_identity=self.identity, method=self.method,
            evidence="Recount: two earlier entries were term syntax rather than tactic invocations")
        forum.submit_baseline_measurement("Alice", cid, first)
        forum.submit_baseline_measurement("Bob", cid, corrected)
        state = tasks.load(self.directory)
        self.assertEqual(state["baseline_measurements"][cid], corrected)
        history = state["baseline_measurement_history"][cid]
        self.assertEqual([row["author"] for row in history], ["Alice", "Bob"])
        self.assertEqual([row["measurement"]["raw_value"] for row in history], [7, 5])
        tasks.seal_baseline(self.directory)
        with self.assertRaisesRegex(ValueError, "only collected"):
            forum.submit_baseline_measurement("Alice", cid, first)
        self.assertEqual(tasks.load(self.directory)["baseline"]["measurements"][cid], corrected)

    def test_clean_declarations_are_tasks_and_unknown_omissions_rejected(self):
        self.assertEqual(len(self.plan()["tasks"]), 2)
        with self.assertRaisesRegex(ValueError, "every in-scope"):
            tasks.propose_plan(self.directory, "Alice", [{"chunk_ids": [self.chunks[0]]}])
        with self.assertRaisesRegex(ValueError, "Universe scope"):
            tasks.propose_plan(self.directory, "Alice", [{"chunk_ids": [self.chunks[0]]}],
                               exclusions={self.chunks[1]: "compiles already"})

    def test_narrow_scope_exclusions_are_explicit(self):
        with tasks.transaction(self.directory) as state: state["config"]["targets"] = "one"
        proposal = tasks.propose_plan(self.directory, "Bob", [{"chunk_ids": [self.chunks[0]]}],
                                     exclusions={self.chunks[1]: "declaration two is outside requested one"})
        state = tasks.accept_plan(self.directory, proposal["proposal_id"])
        self.assertEqual(state["scope_decisions"], proposal["exclusions"])

    def test_cycle_rejected_instead_of_silently_appended(self):
        rows = [{"chunk_ids": [self.chunks[0]], "dependencies": [self.chunks[1]]},
                {"chunk_ids": [self.chunks[1]], "dependencies": [self.chunks[0]]}]
        with self.assertRaisesRegex(ValueError, "cyclic task graph"):
            tasks.propose_plan(self.directory, "Alice", rows)
        grouped = tasks.propose_plan(self.directory, "Alice", [
            {"chunk_ids": self.chunks, "reason": "Mutually dependent declarations; graph contains cycle"}])
        self.assertEqual(len(grouped["tasks"]), 1)

    def test_dependency_precedes_claim_but_compilation_not_filter(self):
        self.plan(dependencies=True)
        strategy = tasks.register_strategy(self.directory, "Bob", self.chunks[1], "one", "shorter proof")
        with self.assertRaisesRegex(ValueError, "unfinished dependencies"):
            tasks.claim_strategy(self.directory, "Bob", strategy["strategy_id"])
        with tasks.transaction(self.directory) as state:
            state["tasks"][self.chunks[0]]["status"] = "no_improvement"
        tasks.claim_strategy(self.directory, "Bob", strategy["strategy_id"])

    def test_mutual_scanner_and_comments_do_not_invent_declarations(self):
        (self.root / "A.lean").write_text('''/- theorem fake : True := by trivial /- nested -/ -/
mutual
def even := odd
def odd := even
end
def text := "theorem falseDecl : True"
#exit
def draft := sorry
''')
        inventory = tasks.scan_sources(self.root, ["A.lean"])
        rows = list(inventory["chunks"].values())
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["group_id"], rows[1]["group_id"])
        self.assertTrue(rows[-1]["after_exit"])
        tasks.install_inventory(self.directory, inventory)
        with self.assertRaisesRegex(ValueError, "mutual declarations"):
            tasks.propose_plan(self.directory, "Alice", [{"chunk_ids": [row["chunk_id"]]} for row in rows[:-1]])

    def test_competing_same_file_strategies_allowed_in_private_trees(self):
        self.baseline_and_round()
        first = self.assignment("Alice", strategy="shorten with simp")
        second = self.assignment("Bob", strategy="direct exact proof")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertNotEqual(first["worktree"], second["worktree"])
        self.assertNotIn("leases", tasks.load(self.directory))
        with self.assertRaisesRegex(ValueError, "already exists"):
            tasks.register_strategy(self.directory, "Carol", self.chunks[0], "same", "shorten with simp")

    def test_author_task_and_tree_binding_and_profile_catalogs(self):
        self.baseline_and_round()
        forum.configure(self.directory, self.root, "optimizing", author="Bob")
        with self.assertRaisesRegex(ValueError, "bound worker"):
            forum.register_strategy("Alice", self.chunks[0], "x", "y")
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_TASK_ID": self.chunks[1]}):
            with self.assertRaisesRegex(ValueError, "task does not match"):
                forum.register_strategy("Bob", self.chunks[0], "x", "y")
        critic = {function.__name__ for function in forum.PROFILE_TOOLS["critic"]}
        self.assertIn("submit_optimization_verdict", critic)
        self.assertNotIn("submit_candidate", critic)
        self.assertNotIn("select_candidate", critic)
        for profile in forum.PROFILES: forum.build_server(profile)

    def test_forum_claim_requires_bound_private_tree(self):
        self.baseline_and_round()
        strategy = forum.register_strategy("Alice", self.chunks[0], "one", "specific approach")
        with self.assertRaisesRegex(ValueError, "controller-bound private worktree"):
            forum.claim_strategy("Alice", strategy["strategy_id"])
        tree = self.temp / "private"
        tree.mkdir()
        with patch.dict(os.environ, {"UNITY_OPTIMIZE_WORKTREE": str(tree), "UNITY_OPTIMIZE_BASE_SHA": "base", "UNITY_OPTIMIZE_TASK_ID": self.chunks[0]}):
            row = forum.claim_strategy("Alice", strategy["strategy_id"])
        self.assertEqual(row["worktree"], str(tree.resolve()))

    def test_round_budget_persists_across_resume_without_reset(self):
        self.baseline_and_round()
        first = tasks.begin_round(self.directory, max_rounds=2)
        self.assertEqual(first["number"], 1)
        tasks.record_dispatch(self.directory, "Alice", self.chunks[0])
        with self.assertRaisesRegex(ValueError, "already dispatched"):
            tasks.record_dispatch(self.directory, "Alice", self.chunks[0])
        self.assertEqual(tasks.load(self.directory)["rounds"]["started"], 1)
        tasks.finish_round(self.directory, outcome="review_rejected")
        tasks.begin_round(self.directory, max_rounds=2)
        tasks.finish_round(self.directory, outcome="no_improvement")
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            tasks.begin_round(self.directory, max_rounds=2)
        state = tasks.load(self.directory)
        self.assertEqual(state["rounds"]["started"], state["rounds"]["completed"])
        self.assertNotEqual(state["phase"], "complete")

    def test_affected_task_can_redispatch_same_round_at_new_revision(self):
        self.baseline_and_round()
        first = tasks.record_dispatch(self.directory, "Alice", self.chunks[0])
        tasks.refresh_affected(self.directory, ["A.lean"], main_sha="changed")
        second = tasks.record_dispatch(self.directory, "Alice", self.chunks[0])
        self.assertEqual(first["task_revision"], 0)
        self.assertEqual(second["task_revision"], 1)
        self.assertEqual(tasks.load(self.directory)["rounds"]["started"], 1)

    def test_unrelated_merge_can_refresh_stale_candidate_in_same_round(self):
        (self.root / "A.lean").write_text("theorem one : True := by trivial\n")
        (self.root / "B.lean").write_text("theorem two : True := by trivial\n")
        tasks.install_inventory(self.directory, tasks.scan_sources(self.root, ["A.lean", "B.lean"]))
        self.chunks = list(tasks.load(self.directory)["chunks"])
        self.baseline_and_round()
        task_a, task_b = self.chunks
        first = tasks.record_dispatch(self.directory, "Bob", task_b)
        assignment = self.assignment("Bob", chunk=task_b, strategy="independent B proof")
        candidate_b = tasks.submit_candidate(self.directory, "Bob", assignment["assignment_id"], "B proposal")
        candidate_a = self.candidate("Alice", strategy="independent A proof")
        tasks.refresh_affected(self.directory, ["A.lean"], main_sha="merged-A",
            integrated_task_id=task_a, integrated_candidate_id=candidate_a["candidate_id"], verification={"passed": True})
        state = tasks.load(self.directory)
        self.assertEqual(state["tasks"][task_b]["revision"], 0)
        self.assertEqual(state["candidates"][candidate_b["candidate_id"]]["status"], "stale")
        second = tasks.record_dispatch(self.directory, "Bob", task_b)
        self.assertEqual(first["task_revision"], second["task_revision"])
        self.assertNotEqual(first["base_main_sha"], second["base_main_sha"])
        self.assertEqual(tasks.load(self.directory)["rounds"]["started"], 1)
        with self.assertRaisesRegex(ValueError, "already dispatched"):
            tasks.record_dispatch(self.directory, "Bob", task_b)

    def test_primary_can_author_but_other_agents_cannot_select(self):
        self.baseline_and_round()
        candidate = self.candidate("Alice")
        cid = candidate["candidate_id"]
        with self.assertRaisesRegex(ValueError, "controller-checked"):
            tasks.select_candidate(self.directory, "Alice", cid, "best")
        with tasks.transaction(self.directory) as state:
            state["candidates"][cid].update(status="checked", verification={"passed": True})
        with self.assertRaisesRegex(ValueError, "configured primary"):
            tasks.select_candidate(self.directory, "Bob", cid, "best")
        selected = tasks.select_candidate(self.directory, "Alice", cid, "best valid score from the candidates")
        self.assertEqual(selected["author"], selected["selection"]["author"])

    def test_no_improvement_is_evidenced_not_accepted_completion(self):
        self.baseline_and_round()
        row = self.assignment()
        with self.assertRaisesRegex(ValueError, "needs attempted approach"):
            tasks.record_no_improvement(self.directory, "Alice", row["assignment_id"], "already best", "")
        tasks.record_no_improvement(self.directory, "Alice", row["assignment_id"], "simp did not shorten", "5 tactics before and after")
        state = tasks.load(self.directory)
        self.assertEqual(state["assignments"][row["assignment_id"]]["status"], "no_improvement")
        self.assertNotEqual(state["phase"], "complete")

    def test_refresh_invalidates_dependent_revisions_scores_and_verdict(self):
        self.plan(dependencies=True)
        tasks.seal_baseline(self.directory)
        tasks.begin_round(self.directory, max_rounds=1)
        candidate = self.candidate()
        with tasks.transaction(self.directory) as state:
            state["review"] = {"verdict": {"approved": True}}
        refreshed = tasks.refresh_affected(self.directory, ["A.lean"], main_sha="new",
                                          integrated_task_id=self.chunks[0])
        self.assertEqual(refreshed["tasks"][self.chunks[1]]["revision"], 1)
        self.assertEqual(refreshed["candidates"][candidate["candidate_id"]]["status"], "stale")
        self.assertEqual(refreshed["review"], {})
        self.assertEqual(refreshed["rounds"]["started"], 1)

    def test_integrated_candidate_and_refresh_are_atomic_and_replay_idempotent(self):
        self.baseline_and_round()
        candidate = self.candidate()
        kwargs = {"main_sha": "merged", "integrated_task_id": self.chunks[0],
                  "integrated_candidate_id": candidate["candidate_id"], "verification": {"passed": True}}
        state = tasks.refresh_affected(self.directory, ["A.lean"], **kwargs)
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "merged")
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["integrated_sha"], state["main_sha"])
        revisions = {tid: row["revision"] for tid, row in state["tasks"].items()}
        tasks.refresh_affected(self.directory, ["A.lean"], **kwargs)
        self.assertEqual(revisions, {tid: row["revision"] for tid, row in tasks.load(self.directory)["tasks"].items()})
        with self.assertRaisesRegex(ValueError, "conflicts with journal"):
            tasks.refresh_affected(self.directory, ["A.lean"], **{**kwargs, "main_sha": "different"})

    def test_group_request_preserves_history_and_requires_worker_closure(self):
        self.baseline_and_round()
        assignment = self.assignment()
        request = tasks.request_scope(self.directory, "Alice", self.chunks[0], [self.chunks[1]], [],
                                      "shared proof helper", "Both transformations touch the same helper")
        with self.assertRaisesRegex(ValueError, "close affected workers"):
            tasks.apply_scope_request(self.directory, request["request_id"])
        candidate = tasks.submit_candidate(self.directory, "Alice", assignment["assignment_id"], "partial proposal")
        tasks.apply_scope_request(self.directory, request["request_id"])
        state = tasks.load(self.directory)
        self.assertEqual(state["tasks"][self.chunks[1]]["status"], "grouped")
        self.assertEqual(state["tasks"][self.chunks[0]]["chunk_ids"], sorted(self.chunks))
        self.assertEqual(state["candidates"][candidate["candidate_id"]]["status"], "stale")
        self.assertEqual(state["rounds"]["started"], 1)
        tasks.refresh_affected(self.directory, ["A.lean"], main_sha="new")
        self.assertEqual(tasks.load(self.directory)["tasks"][self.chunks[1]]["status"], "grouped")

    def test_group_request_cannot_expand_selected_scope(self):
        self.baseline_and_round()
        request = tasks.request_scope(self.directory, "Alice", self.chunks[0], [], ["Unselected.lean"],
                                      "need edits", "requested outside known selected files")
        with self.assertRaisesRegex(ValueError, "widen the selected file scope"):
            tasks.apply_scope_request(self.directory, request["request_id"])

    def review_snapshot(self):
        snapshot = seal({"snapshot_id": "review-1", "main_sha": "base", "metric_sha256": self.metric["sha256"],
                         "passed": True, "config_revision": 0})
        with tasks.transaction(self.directory) as state:
            state["review"] = {"reviewer": "Alice", "snapshot": snapshot}
        forum.configure(self.directory, self.root, "critic")
        return snapshot

    def test_only_configured_primary_reviews_exact_snapshot(self):
        self.baseline_and_round()
        self.candidate("Alice")
        snapshot = self.review_snapshot()
        with self.assertRaisesRegex(ValueError, "configured primary"):
            forum.submit_optimization_verdict("Bob", snapshot["snapshot_id"], True, "looks good")
        result = forum.submit_optimization_verdict("Alice", snapshot["snapshot_id"], True, "same statement, current checked improvement")
        self.assertEqual(result["snapshot_sha256"], snapshot["sha256"])
        with tasks.transaction(self.directory) as state: state["main_sha"] = "different"
        with self.assertRaisesRegex(ValueError, "current source"):
            forum.submit_optimization_verdict("Alice", snapshot["snapshot_id"], True, "old review")

    def test_review_cannot_approve_after_requesting_fixes(self):
        self.baseline_and_round()
        snapshot = self.review_snapshot()
        forum.request_review_repair("Alice", snapshot["snapshot_id"], [self.chunks[0]], "statement explanation insufficient")
        with self.assertRaisesRegex(ValueError, "requested repairs"):
            forum.submit_optimization_verdict("Alice", snapshot["snapshot_id"], True, "ignore finding")
        verdict = forum.submit_optimization_verdict("Alice", snapshot["snapshot_id"], False,
                                                    "repair through normal path", [self.chunks[0]])
        self.assertEqual(verdict["reopen_tasks"], [self.chunks[0]])

    def test_tampered_or_failed_snapshot_cannot_be_approved(self):
        self.baseline_and_round()
        snapshot = self.review_snapshot()
        with tasks.transaction(self.directory) as state:
            state["review"]["snapshot"]["passed"] = False
        with self.assertRaisesRegex(ValueError, "not intact"):
            forum.submit_optimization_verdict("Alice", snapshot["snapshot_id"], True, "ignore tamper")

    def test_no_improvement_review_is_not_success(self):
        self.baseline_and_round()
        snapshot = self.review_snapshot()
        snapshot.update(passed=False, issues=[{"kind": "no_improvement_found"}],
                        comparisons={cid: {"improved": False, "regressed": False} for cid in self.chunks})
        snapshot["sha256"] = tasks.digest({k: v for k, v in snapshot.items() if k != "sha256"})
        with tasks.transaction(self.directory) as state:
            state["review"]["snapshot"] = snapshot
            for task in state["tasks"].values(): task["status"] = "no_improvement"
        result = forum.submit_optimization_verdict("Alice", snapshot["snapshot_id"], False,
            "Finite attempted strategies did not improve the recorded metric; not global optimality",
            no_improvement_found=True)
        self.assertFalse(result["approved"])
        self.assertEqual(result["verdict"], "no_improvement")
        self.assertNotEqual(tasks.load(self.directory)["phase"], "complete")

    def test_no_improvement_verdict_rejects_other_unresolved_failures(self):
        self.baseline_and_round()
        snapshot = self.review_snapshot()
        snapshot.update(passed=False, issues=[{"kind": "no_improvement_found"}, {"kind": "required_build_failed"}])
        snapshot["sha256"] = tasks.digest({k: v for k, v in snapshot.items() if k != "sha256"})
        with tasks.transaction(self.directory) as state:
            state["review"]["snapshot"] = snapshot
            for task in state["tasks"].values(): task["status"] = "no_improvement"
        with self.assertRaisesRegex(ValueError, "non-regressing evidence"):
            forum.submit_optimization_verdict("Alice", snapshot["snapshot_id"], False, "does not compile",
                                              no_improvement_found=True)

    def test_measurements_preserve_provenance_and_method(self):
        self.baseline_and_round()
        identity = seal({"main_sha": "base", "files": {"A.lean": "current"}, "config": {}})
        with patch("unity.optimize_verification.file_identity", return_value=identity):
            row = forum.record_current_measurement("Bob", self.chunks[0], 3, self.method, "three actual tactics")
            self.assertEqual(row["evidence_kind"], "agent_judged")
            self.assertEqual(row["source_sha256"], identity["sha256"])
            with self.assertRaisesRegex(ValueError, "original measurement method"):
                forum.record_current_measurement("Bob", self.chunks[0], 3,
                                                 {"description": "count lines instead"}, "three lines")

    def test_comparison_judgments_retained_separately_from_measurements(self):
        self.baseline_and_round()
        identity = seal({"main_sha": "base", "files": {"A.lean": "current"}, "config": {}})
        judgment = {"improved": True, "reason": "The user-defined qualitative rubric prefers direct structure"}
        with patch("unity.optimize_verification.file_identity", return_value=identity):
            forum.record_current_measurement("Bob", self.chunks[0], 3, self.method, "three actual tactics",
                                               comparison_judgment=judgment)
            with self.assertRaisesRegex(ValueError, "comparison judgment"):
                forum.record_current_measurement("Bob", self.chunks[0], 3, self.method, "evidence",
                                                   comparison_judgment={"improved": True})
        self.assertEqual(tasks.load(self.directory)["judgments"][self.chunks[0]], judgment)


if __name__ == "__main__":
    unittest.main()
