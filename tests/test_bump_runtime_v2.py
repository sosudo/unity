"""Policy-2 routing boundaries, with no models, compilers or service calls."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from unity import bump_bootstrap, bump_runtime as runtime, bump_state as state
from tests.test_bump_scheduler_snapshot import state_fixture


class MigrationFrontierTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_refresh_cas_retries_next_pass_before_return_or_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = state_fixture()
            value["formalization"]["contract"] = {"version": 4, "migration_policy": 2}
            value["formal_tasks"] = {}
            value["migration_plan"] = {"generation": 1, "unmapped_diagnostic_ids": []}
            value["migration_plan_main_sha"] = "previous"
            value["migration_refresh_required"] = True
            state._write_unlocked(root, value)
            paths = SimpleNamespace(forum=root, project_root=root, unity=root)
            attempts, source_checks = [], []
            def refresh(actual_paths):
                attempts.append(actual_paths)
                if len(attempts) == 1:
                    # This is the controller-publication CAS-conflict outcome:
                    # no state change, never a successful frontier observation.
                    return state.load_state(root)
                with state.transaction(root) as current:
                    current["migration_plan_main_sha"] = current["formalization"]["main_sha"]
                    current["migration_refresh_required"] = False
                return state.load_state(root)
            with ExitStack() as stack:
                def mock(obj, name, **kwargs):
                    return stack.enter_context(patch.object(obj, name, **kwargs))
                mock(runtime, "configure_forum")
                mock(runtime, "stop_requested", return_value=False)
                mock(runtime, "load_prompt", return_value="")
                mock(runtime.library, "library_context", return_value="")
                mock(runtime.library, "library_subagents", return_value=[])
                mock(runtime.bump_representation, "recover_representation_reviews")
                mock(runtime.bump_representation, "pending_representation_reviews", return_value=[])
                mock(runtime.bump_jobs, "terminate")
                mock(runtime, "require_source_matches", side_effect=lambda *args: source_checks.append(True))
                gate = mock(bump_bootstrap, "check_ready_modules", side_effect=refresh)
                mock(state, "record_round_end", side_effect=lambda *args, **kwargs: state.load_state(root))
                result = await asyncio.wait_for(runtime.run_formalizing_runtime(
                    SimpleNamespace(agents=[]), paths, {}, ""), 3)
            self.assertEqual(gate.call_count, 2)
            self.assertGreaterEqual(len(source_checks), 3)
            self.assertTrue(runtime._migration_frontier_is_current(result))
            self.assertIsNone(result["formalization"].get("accepted_verdict_id"))

    def test_pending_mapping_only_blocks_until_resolution(self):
        value = {"formalization": {"main_sha": "main", "contract": {"migration_policy": 2}},
                 "migration_plan_main_sha": "main", "migration_plan": {"generation": 1},
                 "migration_mapping_proposals": {"p": {"status": "proposed"}}}
        self.assertFalse(runtime._migration_frontier_is_current(value))
        for status in ("adopted", "rejected"):
            value["migration_mapping_proposals"]["p"]["status"] = status
            self.assertTrue(runtime._migration_frontier_is_current(value))

    def test_compact_projection_keeps_v2_references_but_never_hydrates_originals(self):
        value = {"formalization": {"contract": {"migration_policy": 2,
            "original_index_ref": {"artifact_id": "index", "sha256": "hash"},
            "baseline_ref": {"artifact_id": "baseline", "sha256": "hash"}}},
            "formal_tasks": {"M": {"declaration_subtasks": []}}, "project_baseline": {"version": 6}}
        projected = runtime._scheduler_projection(value)
        self.assertNotIn("project_baseline", projected)
        self.assertEqual(projected["formalization"]["contract"], value["formalization"]["contract"])
        projected["formalization"]["contract"]["original_index_ref"]["sha256"] = "changed"
        self.assertEqual(value["formalization"]["contract"]["original_index_ref"]["sha256"], "hash")
