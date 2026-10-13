"""Migration publication ownership and fail-closed merge ordering; no models."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_state


class MigrationPublicationTests(unittest.TestCase):
    def tasks(self):
        old = {
            "task_id": "z-existing", "status": "complete", "accepted_candidate": "accepted",
            "migration": {"path": "Fixture.lean"}, "lean_file": "Protected.lean",
            "migration_native_status": "verified", "outputs": [{"file": "Support.lean", "declaration": "old"}],
            "verification": {"status": "provisional", "native_pending": True},
            "history": [],
        }
        new = {"task_id": "a-new", "status": "pending", "accepted_candidate": None,
               "migration": {"path": "Fixture.lean"}, "lean_file": "", "outputs": [],
               "verification": {"status": "pending"}}
        return old, new

    def test_source_sharing_keeps_existing_owner_fields_and_supporting_files(self):
        old, new = self.tasks()
        previous = {old["task_id"]: deepcopy(old)}
        source_reservation = {"owner_task": old["task_id"], "shared_with": [],
                              "inherited_from": ["retired-parent"], "receipt": "preserved"}
        support_reservation = {"owner_task": old["task_id"], "shared_with": [], "receipt": "support"}
        state = {"formal_tasks": {old["task_id"]: old, new["task_id"]: new}, "retired_tasks": {},
                 "file_reservations": {"Fixture.lean": deepcopy(source_reservation),
                                       "Support.lean": deepcopy(support_reservation)}}
        bump_state._migration_task_hints_and_reservations(state, previous)
        self.assertEqual(state["file_reservations"]["Fixture.lean"],
                         {**source_reservation, "shared_with": [new["task_id"]]})
        self.assertEqual(state["file_reservations"]["Support.lean"], support_reservation)
        self.assertEqual(old, previous[old["task_id"]])
        self.assertEqual(new["lean_file"], "Fixture.lean")
        self.assertEqual(new["migration_native_status"], "pending")
        self.assertEqual(new["outputs"], [])
        self.assertEqual(new["status"], "pending")
        self.assertEqual(new["verification"], {"status": "pending"})

    def test_source_overlay_does_not_normalize_unrelated_retired_supporting_owners(self):
        old, new = self.tasks()
        previous = {old["task_id"]: deepcopy(old)}
        source_reservation = {"owner_task": "source-parent", "shared_with": [], "receipt": "source"}
        support_reservation = {"owner_task": "support-parent", "shared_with": ["source-parent"],
                               "inherited_from": ["older-support"], "receipt": "support"}
        state = {"formal_tasks": {old["task_id"]: old, new["task_id"]: new},
                 "retired_tasks": {"source-parent": {"replaced_by": [old["task_id"]]},
                                   "support-parent": {"replaced_by": [new["task_id"]]}},
                 "file_reservations": {"Fixture.lean": deepcopy(source_reservation),
                                       "Support.lean": deepcopy(support_reservation)}}
        bump_state._migration_task_hints_and_reservations(state, previous)
        self.assertEqual(state["file_reservations"]["Fixture.lean"],
                         {"owner_task": old["task_id"], "shared_with": [new["task_id"]],
                          "inherited_from": ["source-parent"], "receipt": "source"})
        self.assertEqual(state["file_reservations"]["Support.lean"], support_reservation)

    def test_binding_overlays_metadata_without_replacing_task_table_references(self):
        old, new = self.tasks()
        tasks = {old["task_id"]: old, new["task_id"]: new}
        baseline, requirements = {"sealed": "baseline"}, [{"id": "original-requirement"}]
        state = {"formal_tasks": tasks, "formalization": {"contract": {"project_baseline": baseline},
                                                         "requirements": requirements},
                 "retired_tasks": {}, "file_reservations": {}}
        identities = {key: {"path": "Fixture.lean", "original_ids": [key]} for key in tasks}
        previous = {old["task_id"]: deepcopy(old)}
        compiler = {"controller": "metadata"}
        with patch("unity.bump_migration.bind_task_identities", return_value=identities) as bind:
            bump_state._bind_migration_tasks(state, previous_tasks=previous, compiler_metadata=compiler)
        bind.assert_called_once_with(baseline, requirements, tasks,
                                     previous_tasks=previous, compiler_metadata=compiler)
        self.assertIs(state["formal_tasks"], tasks)
        self.assertIs(tasks[old["task_id"]], old)
        self.assertIs(tasks[new["task_id"]], new)
        self.assertEqual(old["status"], "complete")
        self.assertEqual(old["accepted_candidate"], "accepted")
        self.assertEqual(old["outputs"], previous[old["task_id"]]["outputs"])
        self.assertEqual(new["outputs"], [])

    def test_merge_identity_guard_precedes_all_persisted_candidate_mutations(self):
        for operation in ("begin", "finish"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                forum = Path(directory)
                with bump_state.transaction(forum) as state:
                    state["phase"] = "formalizing"
                    state["formal_candidates"]["candidate"] = {
                        "candidate_id": "candidate", "status": "submitted" if operation == "begin" else "merging"}
                before = bump_state.state_path(forum).read_bytes()
                with patch("unity.bump_migration.require_task_identities",
                           side_effect=ValueError("missing migration identity")) as guard:
                    with self.assertRaisesRegex(ValueError, "missing migration identity"):
                        if operation == "begin":
                            bump_state.begin_formal_merge(forum, "candidate")
                        else:
                            bump_state.finish_formal_merge(forum, "candidate", success=False, error="rejected")
                guard.assert_called_once()
                self.assertEqual(bump_state.state_path(forum).read_bytes(), before)
