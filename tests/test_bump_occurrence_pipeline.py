"""Actual old/new Lean occurrence evidence through Bump's complete state path.

Only temporary, dependency-free fixtures are modified. The independent critic
verdict is scripted test data, not a model evaluation or Poly acceptance.
"""

from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from tests import test_bump_project as fixture_module
from unity import bump_bootstrap as bootstrap, bump_contract as contract, bump_state as state
from unity import bump_runtime, bump_report, bump_checker_v2 as checker
from unity.commands import bump as command
from unity.forum import bump_server
from unity.config import Paths


class NativeOccurrencePipelineTests(unittest.TestCase):
    def test_raw_sibling_occurrences_survive_candidate_merge_resume_and_final_review(self):
        if not shutil.which("elan"):
            self.skipTest("Native fixture requires installed toolchains; never downloads")
        available = subprocess.run(["elan", "toolchain", "list"], text=True,
                                   capture_output=True, timeout=20)
        if not {"leanprover/lean4:v4.28.0-rc1", "leanprover/lean4:v4.34.1"}.issubset(
                {line.split()[0] for line in available.stdout.splitlines() if line.strip()}):
            self.skipTest("Native fixture requires exact installed 4.28.0-rc1 and 4.34.1")
        fixture = fixture_module.ProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write("lean-toolchain", "leanprover/lean4:v4.28.0-rc1\n")
        fixture.write("Fixture/Foundation.lean", "axiom trustA : True\naxiom trustB : True\n")
        left = "import Fixture.Foundation\ntheorem shared : True := trustA\n"
        right = "import Fixture.Foundation\ntheorem shared : True := trustB\n"
        fixture.write("Fixture/Left.lean", left)
        fixture.write("Fixture/Right.lean", right)
        fixture.write("Fixture/Empty.lean", "import Fixture.Foundation\n")
        fixture.write("Fixture.lean", "import Fixture.Basic\nimport Fixture.Left\nimport Fixture.Right\n"
                      "import Fixture.Empty\ntheorem result : True := shared\n")
        fixture.write("Notes/IntentionalError.lean", "#check deliberatelyUnknown\n")
        fixture.commit()
        paths = Paths.from_unity_dir(fixture.root / ".unity")
        paths.unity.mkdir()
        paths.unity_md.write_text("# Goal\nPreserve the original module occurrences and their separate trust.\n")
        paths.agents_yaml.write_text("agents: []\n")
        paths.env.write_text("MAX_ATTEMPTS=5\nRETROSPECTIVE=false\n")
        original = bootstrap.bump_migration_project.snapshot(fixture.root)
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "5"}):
            target = bootstrap.prepare(paths, "v4.34.1", {}, architect="off")
        current = state.load_state(target.forum)
        value = current["formalization"]["contract"]
        baseline = current["project_baseline"]
        self.assertEqual(baseline["version"], 6)
        self.assertEqual(value["migration_policy"], 2)
        self.assertNotIn("project_baseline", value)
        left_id = contract.output_target_key(value, "Fixture.Left", "shared")
        right_id = contract.output_target_key(value, "Fixture.Right", "shared")
        self.assertNotEqual(left_id, right_id)
        self.assertNotIn("axioms", value["targets"][left_id])
        self.assertNotIn("axioms", value["targets"][right_id])
        self.assertIn("Notes/IntentionalError.lean", baseline["build_scope"]["excluded_files"])

        current = bootstrap.check_ready_modules(target)
        self.assertTrue(state.all_formal_tasks_complete(current), {
            key: row.get("migration_diagnostics") for key, row in current["formal_tasks"].items()
            if row["status"] != "complete"})
        self.assertEqual(current["formal_tasks"], {})
        self.assertEqual(current["formal_candidates"], {})
        self.assertEqual(bootstrap.resume(paths), target)
        self.assertEqual(bootstrap.bump_migration_project.snapshot(fixture.root), original)

        # A sibling's proof assumption is already present in the project, but
        # must never be permitted to spread to this original occurrence.
        fixture.write("Fixture/Right.lean", left, root=target.project_root)
        check = contract.check_migration_module(target, current, "Fixture.Right")
        self.assertFalse(check["passed"])
        self.assertIn("trusted assumptions expanded", " ".join(check["issues"]))
        self.assertEqual(check["failed_group_ids"], ["Fixture.Right"])
        self.assertFalse(check["global_blocker"])
        fixture.write("Fixture/Right.lean", "import Fixture.Foundation\n", root=target.project_root)
        check = contract.check_migration_module(target, current, "Fixture.Right")
        self.assertFalse(check["passed"])
        fixture.write("Fixture/Right.lean", right, root=target.project_root)

        left_check = contract.check_migration_module(target, current, "Fixture.Left")
        right_check = contract.check_migration_module(target, current, "Fixture.Right")
        self.assertTrue(left_check["passed"], left_check["issues"])
        self.assertTrue(right_check["passed"], right_check["issues"])
        altered = deepcopy(left_check)
        altered["verified_targets"] = right_check["verified_targets"]
        altered["module_receipt"]["verified_targets"] = right_check["verified_targets"]
        with self.assertRaises(ValueError):
            state._require_migration_receipt({"task_id": "Fixture.Left",
                "outputs": value["bindings"]["Fixture.Left"]}, value, altered)

        # Test-only target compiler failure creates a real error-driven worker
        # integration opportunity. No original evidence or contract is changed.
        fixture.write("Fixture/Left.lean", "import Fixture.Foundation\ntheorem shared : True := unknownFixtureProof\n",
                      root=target.project_root)
        fixture.git("add", "Fixture/Left.lean", root=target.project_root)
        fixture.git("commit", "-qm", "test-only target compiler failure", root=target.project_root)
        with state.transaction(target.forum) as mutable:
            mutable["formalization"]["main_sha"] = fixture.git("rev-parse", "HEAD", root=target.project_root)
            mutable["migration_refresh_required"] = True
        current = bootstrap.check_ready_modules(target)
        self.assertEqual(current["formal_tasks"]["Fixture.Left"]["diagnostic_status"], "repair")
        old_server = (bump_server.FORUM_DIR, bump_server.PROJECT_ROOT, bump_server.PROFILE)
        old_discussion = (bump_server.discussion.FORUM_DIR, bump_server.discussion.PROJECT_ROOT,
                          bump_server.discussion.ICRL_ENABLED)
        for name, old in zip(("FORUM_DIR", "PROJECT_ROOT", "PROFILE"), old_server):
            self.addCleanup(setattr, bump_server, name, old)
        for name, old in zip(("FORUM_DIR", "PROJECT_ROOT", "ICRL_ENABLED"), old_discussion):
            self.addCleanup(setattr, bump_server.discussion, name, old)
        bump_server.configure(target.forum, target.project_root, "formalizing")
        worker = "FixtureOccurrenceWorker"
        tree = bootstrap.bump_worktree.create_worktree(worker, target.project_root)
        task = state.load_state(target.forum)["formal_tasks"]["Fixture.Left"]
        fixture.write("Fixture/Left.lean", "import Fixture.Foundation\ntheorem shared : True := by exact trustA\n",
                      root=tree)
        strategy = state.register_strategy(target.forum, worker, "Preserve this fixture occurrence",
                                           target="Fixture.Left")["strategy"]
        state.claim_strategy(target.forum, strategy["strategy_id"], worker)
        with patch.dict(os.environ, {"UNITY_AGENT_NAME": worker}):
            submitted = bump_server.finalize_formalization(strategy["strategy_id"], worker, "Fixture.Left",
                changed_paths=["Fixture/Left.lean"], outputs=task["outputs"])
        self.assertEqual(submitted["status"], "submitted", submitted)
        candidate = state.begin_formal_merge(target.forum, submitted["candidate"]["candidate_id"])["candidate"]
        integrated = bump_runtime._integrate_and_record(target, candidate, task)
        self.assertTrue(integrated.get("ok"), integrated)
        current = bootstrap.check_ready_modules(target)
        self.assertTrue(state.all_formal_tasks_complete(current))
        self.assertEqual(current["formal_tasks"], {})
        self.assertEqual(bootstrap.resume(paths), target)
        self.assertEqual(current["formalization"]["contract"], value)

        report = contract.verify_final_project(target, current)
        self.assertTrue(report["passed"], report.get("issues"))
        self.assertEqual(set(report["declarations"]), set(value["targets"]))
        self.assertEqual(report["declarations"][left_id], "Fixture.Left")
        self.assertEqual(report["declarations"][right_id], "Fixture.Right")
        for key in ("declarations", "verified_targets", "declaration_occurrences"):
            altered = deepcopy(report)
            altered[key].pop(left_id)
            with self.assertRaises(ValueError):
                contract.validate_migration_snapshot(current, altered)
        altered = deepcopy(report)
        altered["module_receipts"]["Fixture.Left"]["original_index_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            contract.validate_migration_snapshot(current, altered)
        state.record_review_snapshot(target.forum, report)
        state.begin_critic(target.forum)
        current = state.load_state(target.forum)
        review = {"snapshot_id": report["snapshot_id"], "scope_rationale": "Canned native occurrence fixture only.",
                  "repair_reviews": [], "requirements": [{
                      "requirement_id": item["id"], "status": "pass",
                      "checked_anchor_ids": item["anchor_ids"], "checked_prerequisite_ids": [],
                      "declarations": [key for owner in item["tasks"]
                                       for key in contract.output_fingerprints(value, owner)],
                      "rationale": "This module's own original declarations and assumptions were checked.",
                      "argument_rationale": "Each raw module occurrence has separate native proof-trust evidence."}
                      for item in current["formalization"]["requirements"]]}
        missing = deepcopy(review)
        next(item for item in missing["requirements"] if item["requirement_id"] == "preserve:Fixture.Left")["declarations"] = []
        with self.assertRaises(ValueError):
            state._validate_semantic_review(current, missing, approved=True)
        state.submit_critic_verdict(target.forum, "FixtureCritic", "approved", "Canned native fixture, not model inference.",
                                    review=review)
        self.assertTrue(command._accept_current_critic(target))
        completed = state.load_state(target.forum)
        final_report = bump_report.completion_report(completed)
        self.assertEqual(final_report["status"], "accepted")
        self.assertTrue(contract.snapshot_is_current(target, completed, completed["formalization"]["review_snapshot"]))
        self.assertEqual(bootstrap.bump_migration_project.snapshot(fixture.root), original)
        self.assertEqual((target.project_root / "Notes/IntentionalError.lean").read_bytes(),
                         (fixture.root / "Notes/IntentionalError.lean").read_bytes())
        # Detailed native trust remains artifact-backed, separate for each raw
        # module occurrence, rather than duplicated into the compact contract.
        for module, expected in (("Fixture.Left", "trustA"), ("Fixture.Right", "trustB")):
            evidence = report["module_receipts"][module]["evidence_refs"][0]
            old = checker.read_artifact(target.artifacts, evidence["original"])
            declaration = next(row for row in old["declarations"] if row["display_name"] == "shared")
            self.assertEqual([row["display_name"] for row in declaration["axioms"]], [expected])
        self.assertEqual(report["module_receipts"]["Fixture.Empty"]["verified_targets"], {})
        self.assertFalse(final_report["semantic_equivalence_proved"])
