"""Bounded Bump status transport does not change authoritative evidence."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from unity import artifacts, bump_state
from unity.forum import bump_server as forum


class BumpStatusPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = bump_state._default_state()
        self.state.update(run_id="bump-status-test", revision=7, phase="critic")
        baseline = {"sha256": "b" * 64, "version": 5, "policy": "migration-v1",
            "scope_policy": 1, "occurrence_policy": 1, "compiler_modules": {"Poly": {}},
            "declarations": {"occurrence-a": {}}, "original_reports": {"Poly": {}},
            "migration": {"target_version": "leanprover/lean4:v4.34.1"}}
        self.state["project_baseline"] = baseline
        self.state["input_source"] = {"kind": "supplied_sources", "candidate_id": "source-test",
            "sha256": "s" * 64, "source_refs": [{"ref_id": "source:native/Poly.json",
                "artifact_id": "artifact-123456789abc", "sha256": "n" * 64, "bytes": 900000000,
                "path": ".unity/source/native/Poly.json"}]}
        self.state["formalization"].update(status="approval_pending", main_sha="a" * 40,
            requirements=[{"id": "requirement-a", "tasks": ["Poly"]}],
            contract={"sha256": "c" * 64, "version": 3, "migration_policy": 1,
                "migration_scope_policy": 1, "migration_occurrence_policy": 1,
                "inspection_policy": 4, "project_baseline": baseline,
                "bindings": {"Poly": [{"declaration": "Poly.a", "file": "Poly.lean"}]}},
            review_snapshot={"snapshot_id": "review-current", "artifact_id": "artifact-abcdef123456",
                "passed": True, "main_sha": "a" * 40, "contract_sha256": "c" * 64},
            pending_verdict_id="verdict-current")
        self.state["formal_tasks"]["Poly"] = {"status": "complete", "revision": 2,
            "migration_module": "Poly", "migration_attempts": 2, "migration_max_attempts": 5,
            "accepted_candidate": "candidate-a", "outputs": [{"declaration": "Poly.a", "file": "Poly.lean"}],
            "verification": {"status": "verified", "candidate_id": "candidate-a"}}
        self.state["formal_candidates"]["candidate-a"] = {"task_id": "Poly", "status": "merged",
            "author": "Sol1", "stage": "complete", "commit_sha": "d" * 40,
            "verification": {"status": "passed", "artifact_id": "artifact-1234abcd5678",
                "proposed_contract": self.state["formalization"]["contract"],
                "verification_artifact": {"artifact_id": "artifact-1234abcd5678", "sha256": "v" * 64}}}
        self.state["critic_verdicts"] = [{"verdict_id": "verdict-current", "verdict": "approved",
            "snapshot_id": "review-current", "snapshot_sha256": "r" * 64,
            "requirements_sha256": "q" * 64}]

    def status(self, **kwargs):
        with patch.object(forum.bump_state, "load_state", return_value=self.state), \
             patch.object(forum, "PROJECT_ROOT", self.root):
            return forum.bump_status(**kwargs)

    def test_zero_argument_call_preserves_current_binding_metadata(self):
        result = self.status()
        self.assertEqual(result["run_id"], self.state["run_id"])
        self.assertEqual(result["revision"], 7)
        formal = result["formalization"]
        self.assertEqual(formal["review_snapshot"]["snapshot_id"], "review-current")
        self.assertEqual(formal["review_snapshot"]["snapshot_sha256"],
                         bump_state._report_digest(self.state["formalization"]["review_snapshot"]))
        self.assertEqual(formal["requirements_sha256"],
                         bump_state._report_digest(self.state["formalization"]["requirements"]))
        self.assertEqual(formal["pending_verdict_reference"]["verdict_id"], "verdict-current")
        self.assertEqual(formal["contract"]["sha256"], "c" * 64)
        task = result["formal_tasks"]["Poly"]
        self.assertEqual((task["migration_attempts"], task["migration_max_attempts"]), (2, 5))
        self.assertEqual(task["binding_count"], 1)
        self.assertEqual(task["binding_sha256"], bump_state.digest(
            self.state["formalization"]["contract"]["bindings"]["Poly"]))

    def test_payload_independent_of_logically_gigabyte_native_baseline(self):
        expected = self.status()
        # Shared strings keep this offline fixture small, while a mistaken full
        # JSON serialization would materialize over 8 GiB per baseline copy.
        marker = "NATIVE_AST_NOT_AGENT_PAYLOAD_" + "x" * (1024 * 1024)
        self.state["project_baseline"]["original_reports"]["Poly"]["meaning"] = [marker] * 8192
        start = time.monotonic()
        observed = self.status()
        elapsed = time.monotonic() - start
        self.assertEqual(observed, expected)
        encoded = json.dumps(observed)
        self.assertLess(len(encoded.encode()), forum._STATUS_INLINE_BYTES)
        self.assertNotIn("NATIVE_AST_NOT_AGENT_PAYLOAD", encoded)
        print(json.dumps({"diagnostic": "status_projection_only", "seconds": elapsed,
            "payload_bytes": len(encoded.encode()), "logical_native_bytes_per_baseline": len(marker) * 8192}))

    def test_no_recursive_copy_or_iteration_of_native_reports(self):
        class Forbidden(dict):
            def __iter__(self):
                raise AssertionError("native evidence iterated")
            def items(self):
                raise AssertionError("native evidence traversed")
            def __deepcopy__(self, memo):
                raise AssertionError("native evidence copied")
        self.state["project_baseline"]["original_reports"] = Forbidden({"Poly": Forbidden()})
        self.assertEqual(self.status()["project_baseline"]["original_report_count"], 1)

    def test_status_does_not_mutate_authoritative_state(self):
        original = copy.deepcopy(self.state)
        self.status()
        self.assertEqual(self.state, original)
        self.assertIs(self.state["formal_candidates"]["candidate-a"]["verification"]["proposed_contract"],
                      self.state["formalization"]["contract"])

    def test_collection_pages_are_complete_and_explicit(self):
        self.state["formal_tasks"] = {f"Module{i:03}": {"status": "pending"} for i in range(43)}
        self.state["formal_candidates"] = {f"candidate-{i:03}": {"status": "submitted"} for i in range(43)}
        self.state["strategies"] = {f"strategy-{i:03}": {"status": "registered"} for i in range(43)}
        seen = {field: [] for field in ("formal_tasks", "formal_candidates", "strategies")}
        offset = 0
        while offset is not None:
            result = self.status(offset=offset, limit=10)
            for field in seen:
                seen[field].extend(result[field])
                self.assertEqual(result["pages"][field]["total"], 43)
                self.assertEqual(result["pages"][field]["returned"], len(result[field]))
            offset = result["pages"]["formal_tasks"]["next_offset"]
        for field, names in seen.items():
            self.assertEqual(names, sorted(self.state[field]))

    def test_source_refs_reach_exact_native_artifact_without_ast(self):
        result = self.status()
        self.assertEqual(result["input_source"]["source_refs"][0]["artifact_id"], "artifact-123456789abc")
        self.assertNotIn("original_reports", result["project_baseline"])
        self.assertNotIn("project_baseline", result["formalization"]["contract"])
        self.assertNotIn("proposed_contract", result["formal_candidates"]["candidate-a"]["verification"])

    def test_oversized_metadata_uses_exact_existing_artifact_mechanism(self):
        self.state["formal_tasks"]["Poly"]["lean_file"] = "exact-" + "p" * 50000
        result = self.status()
        self.assertLess(len(json.dumps(result).encode()), forum._STATUS_INLINE_BYTES)
        reference = result["status_page_artifact"]
        stored = artifacts.artifact_bytes(self.root / ".unity/artifacts", reference["artifact_id"])
        payload = json.loads(stored)
        self.assertEqual(payload["formal_tasks"]["Poly"]["lean_file"],
                         self.state["formal_tasks"]["Poly"]["lean_file"])
        self.assertEqual(payload["formalization"]["review_snapshot"]["snapshot_id"], "review-current")

    def test_invalid_pages_fail_before_loading_state(self):
        for kwargs in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 101}, {"limit": True}):
            with self.subTest(kwargs=kwargs), patch.object(forum.bump_state, "load_state") as load:
                with self.assertRaises(ValueError):
                    forum.bump_status(**kwargs)
                load.assert_not_called()

    def test_accepted_verdict_reference_not_lost_outside_current_page(self):
        self.state["formalization"]["accepted_verdict_id"] = "verdict-current"
        self.state["critic_verdicts"] = [{"verdict_id": f"old-{n}"} for n in range(30)] + self.state["critic_verdicts"]
        result = self.status(limit=2)
        self.assertEqual(result["pages"]["critic_verdicts"]["next_offset"], 2)
        self.assertEqual(result["formalization"]["accepted_verdict_reference"]["snapshot_id"], "review-current")


if __name__ == "__main__":
    unittest.main()
