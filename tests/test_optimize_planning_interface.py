"""Offline regression tests for Optimize's planning and phase-state contracts."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from unity import optimize_tasks as tasks


class OptimizePlanningInterfaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.forum = self.root / ".unity" / "forum" / "optimize"
        tasks.initialize(self.forum, self.root, run_id="optimize-interface", base_sha="original",
                         metric="length", scope={"files": ["A.lean", "B.lean"], "targets": "A.lean only"},
                         config={"primary": "Alice", "agents": ["Alice", "Bob", "Carol"]},
                         baseline={"source_identity": {"main_sha": "original"}, "build": {"passed": True}})
        self.inventory({"A.lean": "theorem one : True := by trivial\n",
                        "B.lean": "theorem two : True := by trivial\n"})
        self.phase("chunking")

    def inventory(self, sources):
        for path, source in sources.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(source)
        tasks.install_inventory(self.forum, tasks.scan_sources(self.root, sources))

    def phase(self, value):
        with tasks.transaction(self.forum) as state:
            state["phase"] = value

    def state(self):
        return tasks.load(self.forum)

    def selected(self, path="A.lean"):
        return tasks.inventory_page(self.state(), path=path)["chunks"]

    def propose(self, **changes):
        arguments = {"exclude_outside_paths": ["A.lean"], "exclusion_reason": "The request selects only A.lean",
                     "inventory_sha256": tasks.inventory_identity(self.state())}
        arguments.update(changes)
        return tasks.propose_plan(self.forum, "Alice", [{"chunk_ids": [row["chunk_id"]]} for row in self.selected()],
                                  **arguments)

    def assert_atomic_error(self, code, call):
        before = (self.forum / tasks.STATE_FILE).read_bytes()
        with self.assertRaises(tasks.PlanValidationError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual((self.forum / tasks.STATE_FILE).read_bytes(), before)
        return raised.exception

    def test_realistic_inventory_selector_produces_50_tasks_and_2970_exclusions(self):
        path = "Batteries/Data/List/Perm.lean"
        self.inventory({path: "".join(f"theorem perm{n} : True := by trivial\n" for n in range(50)),
                        "Batteries/Other.lean": "".join(f"theorem other{n} : True := by trivial\n" for n in range(2970))})
        before = self.state()
        page = tasks.inventory_page(before, path=path)
        self.assertEqual((page["total"], page["matched"], len(page["chunks"])), (3020, 50, 50))
        self.assertIsNone(page["next_offset"])
        proposal = tasks.propose_plan(self.forum, "Alice", [{"chunk_ids": [row["chunk_id"]]} for row in page["chunks"]],
                                     exclude_outside_paths=[path], exclusion_reason="Only the requested Perm.lean file",
                                     inventory_sha256=page["inventory_sha256"])
        self.assertEqual(len(proposal["tasks"]), 50)
        self.assertEqual(len(proposal["exclusions"]), 2970)
        self.assertEqual(proposal["exclusion_selector"]["exclude_outside_paths"], [path])
        accepted = tasks.accept_plan(self.forum, proposal["proposal_id"])
        self.assertEqual(accepted["scope"], before["scope"])
        self.assertEqual(accepted["baseline"], before["baseline"])
        self.assertEqual(accepted["scope_decisions"], proposal["exclusions"])
        self.assertEqual(len(accepted["tasks"]), 50)
        self.assertEqual(accepted["rounds"]["started"], 0)

    def test_inventory_pages_are_exact_deterministic_and_complete(self):
        self.inventory({"A.lean": "namespace N\n" + "".join(f"theorem n{i} : True := by trivial\n" for i in range(125)) + "end N\n",
                        "A/Other.lean": "theorem outside : True := by trivial\n"})
        state = self.state()
        first = tasks.inventory_page(state, path="A.lean")
        second = tasks.inventory_page(state, path="A.lean", offset=first["next_offset"], inventory_sha256=first["inventory_sha256"])
        third = tasks.inventory_page(state, path="A.lean", offset=second["next_offset"])
        all_rows = first["chunks"] + second["chunks"] + third["chunks"]
        self.assertEqual([row["name"] for row in all_rows], [f"N.n{i}" for i in range(125)])
        self.assertEqual(len({row["chunk_id"] for row in all_rows}), 125)
        self.assertTrue(all(row["source_sha256"] and row["start_line"] <= row["end_line"] for row in all_rows))
        self.assertIsNone(third["next_offset"])
        self.assertEqual(tasks.inventory_page(state, name="N.n1")["matched"], 1)
        self.assertEqual(tasks.inventory_page(state, name="n1")["matched"], 0)
        self.assertEqual(tasks.inventory_page(state, path="A")["matched"], 0)
        self.assertEqual(tasks.inventory_page(state, path="A.lean", offset=1000)["chunks"], [])
        self.assertEqual(first, tasks.inventory_page(state, path="A.lean"))

    def test_inventory_hash_ignores_events_phase_and_tasks_but_binds_content(self):
        state = self.state()
        identity = tasks.inventory_identity(state)
        changed = deepcopy(state)
        changed.update(revision=999, phase="exploration", events=[{"kind": "finding"}], tasks={"task": {"status": "complete"}})
        self.assertEqual(tasks.inventory_identity(changed), identity)
        chunk = next(iter(changed["chunks"].values()))
        chunk["source_sha256"] = "different"
        self.assertNotEqual(tasks.inventory_identity(changed), identity)
        with self.assertRaisesRegex(tasks.PlanValidationError, "stale inventory"):
            tasks.inventory_page(changed, inventory_sha256=identity)

    def test_inventory_rejects_noncanonical_paths_and_bad_paging(self):
        for arguments in ({"path": "../A.lean"}, {"path": "/A.lean"}, {"path": "./A.lean"},
                          {"path": ".unity/private"}, {"offset": -1}, {"offset": True},
                          {"limit": 0}, {"limit": 101}, {"limit": 1.5}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                tasks.inventory_page(self.state(), **arguments)

    def test_selector_missing_selected_declaration_is_not_implicitly_excluded(self):
        self.assert_atomic_error("incomplete_coverage", lambda: tasks.propose_plan(
            self.forum, "Alice", [], exclude_outside_paths=["A.lean"], exclusion_reason="Outside A",
            inventory_sha256=tasks.inventory_identity(self.state())))

    def test_selector_rejects_unknown_noncanonical_duplicate_and_empty_paths(self):
        for paths, code in ((["missing.lean"], "unknown_exclusion_path"), (["./A.lean"], "unknown_exclusion_path"),
                            (["../A.lean"], "unknown_exclusion_path"), (["A.lean", "A.lean"], "duplicate_exclusion_path"),
                            ([], "invalid_exclusion_selector"), ("A.lean", "invalid_exclusion_selector")):
            with self.subTest(paths=paths):
                self.assert_atomic_error(code, lambda: self.propose(exclude_outside_paths=paths))

    def test_selector_requires_current_hash_and_reason(self):
        for arguments, code in (({"inventory_sha256": ""}, "incomplete_exclusion_selector"),
                                ({"exclusion_reason": " "}, "incomplete_exclusion_selector"),
                                ({"inventory_sha256": "stale"}, "stale_inventory")):
            with self.subTest(arguments=arguments):
                self.assert_atomic_error(code, lambda: self.propose(**arguments))

    def test_selector_does_not_allow_universe_exclusions(self):
        with tasks.transaction(self.forum) as state:
            state["config"]["targets"] = "Universe"
        self.assert_atomic_error("universe_exclusion", self.propose)

    def test_explicit_exclusion_overlap_and_plan_conflict_are_rejected(self):
        excluded = self.selected("B.lean")[0]["chunk_id"]
        self.assert_atomic_error("conflicting_exclusions", lambda: self.propose(exclusions={excluded: "Different reason"}))
        proposal = self.propose(exclusions={excluded: "The request selects only A.lean"})
        self.assertEqual(proposal["exclusions"], {excluded: "The request selects only A.lean"})
        self.assert_atomic_error("unknown_or_excluded_chunks", lambda: tasks.propose_plan(
            self.forum, "Alice", [{"chunk_ids": [cid]} for cid in self.state()["chunks"]],
            exclude_outside_paths=["A.lean"], exclusion_reason="Outside A",
            inventory_sha256=tasks.inventory_identity(self.state())))

    def test_legacy_explicit_exclusion_proposal_and_checkpoint_remain_accepted(self):
        selected = self.selected()[0]["chunk_id"]
        excluded = self.selected("B.lean")[0]["chunk_id"]
        proposal = tasks.propose_plan(self.forum, "Bob", [{"chunk_ids": [selected]}], exclusions={excluded: "Outside requested A"})
        self.assertNotIn("exclusion_selector", proposal)
        state = tasks.accept_plan(self.forum, proposal["proposal_id"])
        self.assertEqual(list(state["tasks"]), [selected])

    def test_hash_bound_plan_rejects_inventory_changes_before_acceptance(self):
        proposal = self.propose()
        inventory = tasks.scan_sources(self.root, ["A.lean", "B.lean"])
        next(iter(inventory["chunks"].values()))["source_sha256"] = "changed-source"
        tasks.install_inventory(self.forum, inventory)
        self.assert_atomic_error("stale_inventory", lambda: tasks.accept_plan(self.forum, proposal["proposal_id"]))
        self.assertEqual(self.state()["tasks"], {})

    def test_hash_bound_plan_survives_unrelated_forum_events(self):
        proposal = self.propose()
        with tasks.transaction(self.forum) as state:
            tasks._event(state, "finding", "Bob", summary="Unrelated evidence")
        self.assertEqual(len(tasks.accept_plan(self.forum, proposal["proposal_id"])["tasks"]), 1)

    def test_malformed_plan_rows_get_structured_atomic_errors(self):
        cid = self.selected()[0]["chunk_id"]
        for row, code in (({"chunk_ids": "not-a-list"}, "invalid_chunk_ids"),
                          ({"chunk_ids": [cid], "dependencies": "not-a-list"}, "invalid_dependency"),
                          ({"chunk_ids": [cid], "files": ["../escape"]}, "invalid_task_files")):
            with self.subTest(code=code):
                self.assert_atomic_error(code, lambda: tasks.propose_plan(self.forum, "Alice", [row]))
        self.assert_atomic_error("invalid_plan_rows", lambda: tasks.propose_plan(self.forum, "Alice", ["not-a-row"]))

    def test_unknown_chunk_diagnostics_bound_individual_strings(self):
        error = self.assert_atomic_error("unknown_or_excluded_chunks", lambda: tasks.propose_plan(
            self.forum, "Alice", [{"chunk_ids": ["x" * 100000]}]))
        self.assertEqual(error.details["unknown_chunks_count"], 1)
        self.assertLessEqual(len(error.details["unknown_chunks_sample"][0]), 240)

    def test_missing_inventory_errors_have_bounded_samples_and_no_partial_publication(self):
        self.inventory({"A.lean": "".join(f"theorem n{i} : True := by trivial\n" for i in range(3020))})
        error = self.assert_atomic_error("incomplete_coverage", lambda: tasks.propose_plan(self.forum, "Alice", []))
        self.assertEqual(error.details["missing_chunks_count"], 3020)
        self.assertEqual(len(error.details["missing_chunks_sample"]), 10)
        self.assertEqual(error.details["inventory_tool"], "optimize_inventory")
        self.assertEqual(self.state()["plan_proposals"], {})

    def test_selector_preserves_mutual_group_and_dependency_cycle_rejections(self):
        self.inventory({"A.lean": "mutual\ndef even := odd\ndef odd := even\nend\n", "B.lean": "def outside := 0\n"})
        self.assert_atomic_error("split_mutual_group", self.propose)
        chunks = [row["chunk_id"] for row in self.selected()]
        grouped = tasks.propose_plan(self.forum, "Alice", [{"chunk_ids": chunks, "reason": "Mutually dependent declarations"}],
                                    exclude_outside_paths=["A.lean"], exclusion_reason="Outside A",
                                    inventory_sha256=tasks.inventory_identity(self.state()))
        self.assertEqual(len(grouped["tasks"]), 1)
        self.inventory({"A.lean": "def one := 0\ndef two := 1\n", "B.lean": "def outside := 0\n"})
        chunks = [row["chunk_id"] for row in self.selected()]
        rows = [{"chunk_ids": [chunks[0]], "dependencies": [chunks[1]]},
                {"chunk_ids": [chunks[1]], "dependencies": [chunks[0]]}]
        self.assert_atomic_error("cyclic_dependencies", lambda: tasks.propose_plan(
            self.forum, "Alice", rows, exclude_outside_paths=["A.lean"], exclusion_reason="Outside A",
            inventory_sha256=tasks.inventory_identity(self.state())))

    def test_phase_records_are_additive_and_do_not_change_budget_baseline_or_legacy_flags(self):
        with tasks.transaction(self.forum) as state:
            state.update(architect_done=True, exploration_done=True)
        before = self.state()
        dispatch = tasks.begin_phase_dispatch(self.forum, "chunking", ["Alice", "Bob"])
        tasks.record_phase_result(self.forum, "Alice", dispatch["dispatch_id"], "blocked", "Need canonical IDs", {"finding": "No plan yet"})
        tasks.record_phase_result(self.forum, "Bob", dispatch["dispatch_id"], "complete", "Plan supplied", ["plan-1"])
        closed = tasks.close_phase_dispatch(self.forum, dispatch["dispatch_id"], "complete", "Controller checked collective output")
        after = self.state()
        self.assertEqual(closed["results"]["Alice"]["status"], "blocked")
        self.assertEqual(closed["results"]["Bob"]["status"], "complete")
        for key in ("rounds", "baseline", "baseline_sealed", "architect_done", "exploration_done", "tasks", "phase"):
            self.assertEqual(after[key], before[key])

    def test_every_real_phase_profile_can_bind_non_task_turns(self):
        for profile in ("architect", "exploration", "chunking", "optimizing", "critic", "retrospective"):
            with self.subTest(profile=profile):
                self.phase(profile)
                dispatch = tasks.begin_phase_dispatch(self.forum, profile, ["Alice"])
                self.assertEqual(dispatch["profile"], profile)
                tasks.close_phase_dispatch(self.forum, dispatch["dispatch_id"], "interrupted")

    def test_phase_results_require_assigned_author_current_running_dispatch_and_phase(self):
        dispatch = tasks.begin_phase_dispatch(self.forum, "chunking", ["Alice"])
        did = dispatch["dispatch_id"]
        for author, dispatch_id in (("Bob", did), ("Alice", "missing")):
            before = self.state()
            with self.assertRaises(ValueError):
                tasks.record_phase_result(self.forum, author, dispatch_id, "complete", "Done", "evidence")
            self.assertEqual(self.state(), before)
        self.phase("exploration")
        with self.assertRaisesRegex(ValueError, "current phase"):
            tasks.record_phase_result(self.forum, "Alice", did, "complete", "Done", "evidence")
        tasks.close_phase_dispatch(self.forum, did, "interrupted")
        newer = tasks.begin_phase_dispatch(self.forum, "exploration", ["Alice"])
        with self.assertRaisesRegex(ValueError, "current running"):
            tasks.record_phase_result(self.forum, "Alice", did, "complete", "Old result", "evidence")
        self.assertEqual(self.state()["phase_dispatches"][newer["dispatch_id"]]["results"], {})

    def test_phase_result_rejects_changed_source_or_configuration(self):
        for key in ("main_sha", "config"):
            with self.subTest(key=key):
                dispatch = tasks.begin_phase_dispatch(self.forum, "chunking", ["Alice"])
                with tasks.transaction(self.forum) as state:
                    if key == "main_sha": state["main_sha"] += "-changed"
                    else: state["config"]["revision"] += 1
                with self.assertRaisesRegex(ValueError, "binding is stale"):
                    tasks.record_phase_result(self.forum, "Alice", dispatch["dispatch_id"], "complete", "Done", "evidence")
                tasks.close_phase_dispatch(self.forum, dispatch["dispatch_id"], "blocked")

    def test_result_status_evidence_and_skips_are_validated(self):
        dispatch = tasks.begin_phase_dispatch(self.forum, "chunking", ["Alice"])
        for status, summary, evidence in (("skipped", "Not doing chunking", "reason"), ("passed", "Done", "evidence"),
                                          ("complete", " ", "evidence"), ("complete", "Done", {}),
                                          ("complete", "Done", " "), ("complete", "Done", None)):
            with self.subTest(status=status, summary=summary, evidence=evidence), self.assertRaises(ValueError):
                tasks.record_phase_result(self.forum, "Alice", dispatch["dispatch_id"], status, summary, evidence)
        tasks.close_phase_dispatch(self.forum, dispatch["dispatch_id"], "blocked")
        self.phase("architect")
        dispatch = tasks.begin_phase_dispatch(self.forum, "architect", ["Alice"])
        result = tasks.record_phase_result(self.forum, "Alice", dispatch["dispatch_id"], "skipped", "No optional setup needed", "No dependency change")
        self.assertEqual(result["status"], "skipped")

    def test_preparation_result_cannot_be_used_for_review_or_optimization(self):
        self.phase("critic")
        dispatch = tasks.begin_phase_dispatch(self.forum, "critic", ["Alice"])
        with self.assertRaisesRegex(ValueError, "only reported during preparation"):
            tasks.record_phase_result(self.forum, "Alice", dispatch["dispatch_id"], "complete", "Approved", "evidence")

    def test_phase_dispatch_prevents_overlapping_or_mismatched_turns(self):
        for profile, authors in (("exploration", ["Alice"]), ("chunking", []),
                                 ("chunking", ["Alice", "Alice"]), ("chunking", ["Unknown"])):
            with self.subTest(profile=profile, authors=authors), self.assertRaises(ValueError):
                tasks.begin_phase_dispatch(self.forum, profile, authors)
        dispatch = tasks.begin_phase_dispatch(self.forum, "chunking", ["Alice"])
        with self.assertRaisesRegex(ValueError, "already running"):
            tasks.begin_phase_dispatch(self.forum, "chunking", ["Bob"])
        with self.assertRaisesRegex(ValueError, "invalid terminal"):
            tasks.close_phase_dispatch(self.forum, dispatch["dispatch_id"], "running")
        tasks.close_phase_dispatch(self.forum, dispatch["dispatch_id"], "blocked")
        with self.assertRaisesRegex(ValueError, "not running"):
            tasks.close_phase_dispatch(self.forum, dispatch["dispatch_id"], "complete")


if __name__ == "__main__":
    unittest.main()
