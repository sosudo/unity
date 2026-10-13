"""Compiler-driven Bump prompt contracts; no model, provider or Lean calls."""

import asyncio
import inspect
from pathlib import Path
import re
import unittest

from unity.forum import bump_workflow_server as server


PROMPTS = Path(__file__).resolve().parents[1] / "unity/prompts"
ROLES = ("PREPARING", "REPAIRING", "CRITIC", "RETROSPECTIVE")


class BumpPromptContractTests(unittest.TestCase):
    def phase(self, name):
        return (PROMPTS / "bump" / f"{name}.md").read_text()

    def tools(self):
        return (PROMPTS / "BUMP_WORKFLOW_TOOLS.md").read_text()

    def test_every_current_phase_has_prompt_and_exact_registered_catalog(self):
        self.assertEqual(set(server.PROFILES), {name.lower() for name in ROLES})
        for role in ROLES:
            self.assertTrue(self.phase(role).strip())
            actual = {tool.name for tool in asyncio.run(server.build_server(role.lower()).list_tools())}
            expected = {tool.__name__ for tool in server.PROFILE_TOOLS[role.lower()]}
            self.assertEqual(actual, expected)

    def test_tools_document_all_advertised_names_and_no_nonexistent_calls(self):
        names = {tool.__name__ for tools in server.PROFILE_TOOLS.values() for tool in tools}
        for name in names:
            self.assertRegex(self.tools(), r"\b" + re.escape(name) + r"\b")
        for text in (self.tools(), *(self.phase(role) for role in ROLES)):
            for name in re.findall(r"`([a-z_]+)\(", text):
                self.assertIn(name, names)

    def test_documented_full_signatures_follow_current_forum(self):
        for name in ("request_repair_scope", "request_configuration_change", "submit_repair",
                     "record_workflow_change", "request_review_repair", "submit_migration_verdict"):
            match = re.search(r"`" + name + r"\(([^)]*)\)`", self.tools())
            self.assertIsNotNone(match)
            documented = [part.strip().rstrip("?") for part in match.group(1).split(",")]
            self.assertEqual(documented, list(inspect.signature(getattr(server, name)).parameters))

    def test_controller_actions_are_not_worker_tools(self):
        forbidden = {"prepare_formal_worktree", "persist_report", "runtime_advance",
                     "finalize_formalization", "submit_formalization_verdict", "validate_chunks",
                     "submit_solution_candidate", "reopen_solving", "propose_source_fix"}
        for tools in server.PROFILE_TOOLS.values():
            self.assertFalse(forbidden.intersection(tool.__name__ for tool in tools))

    def test_new_prompts_do_not_instruct_legacy_ledger_or_another_pipeline(self):
        forbidden = ("output_manifest", "manifest_repairs", "reopen_representations", "repair_reviews",
                     "submit_source_repair", "submit_formalization_verdict", "solve_task(", "solve_brief(",
                     "Original source bytes remain unchanged", "Do not edit the supplied source")
        for text in (self.tools(), *(self.phase(role) for role in ROLES)):
            for value in forbidden:
                self.assertNotIn(value, text)

    def test_repair_prompt_tracks_all_declarations_and_incremental_progress(self):
        text = self.phase("REPAIRING")
        for value in ("Every source declaration", "including declarations that already compile",
                      "Only chunks needing work", "leased files", "one active production writer",
                      "multi-declaration", "submit_repair", "unrelated declaration", "failed import"):
            self.assertIn(value, text)

    def test_configuration_and_dependency_resolution_are_controller_owned(self):
        for role in ("PREPARING", "REPAIRING"):
            text = self.phase(role)
            self.assertIn("request_configuration_change", text)
            self.assertIn("lake update", text)
        preparing = self.phase("PREPARING")
        for value in ("actual Lean/Lake executables", "recursively migrate dependencies", "vendor/",
                      "license and provenance", "need not be recursively changed", "supplemental modules"):
            self.assertIn(value, preparing)
        self.assertIn("scope-only request may use `files=[]`", self.tools())

    def test_primary_critic_can_request_precise_new_tasks_and_must_check_current_evidence(self):
        text = self.phase("CRITIC")
        for value in ("Do not edit sources", "configured primary", "actual source/configuration/dependency diff",
                      "request_review_repair", "returned task IDs", "reopen_task_ids",
                      "identical current passed snapshot", "Never approve with queued review issues"):
            self.assertIn(value, text)
        for role, tools in server.PROFILE_TOOLS.items():
            names = {tool.__name__ for tool in tools}
            self.assertEqual("request_review_repair" in names, role == "critic")
            self.assertEqual("submit_migration_verdict" in names, role == "critic")

    def test_roster_has_no_reserved_reviewer_or_contributor_exclusion(self):
        for role in ("REPAIRING", "FORMALIZING", "SOURCE_REPAIR"):
            self.assertIn("All configured agents participate in repairs, including the primary", self.phase(role))
        for role in ("CRITIC", "REPRESENTATION_REVIEW"):
            text = self.phase(role)
            for value in ("configured primary", "temporary read-only phase, not a separate roster role",
                          "Prior contributions do not disqualify you", "including your own"):
                self.assertIn(value, text)
        self.assertIn("Only the configured primary during the critic phase", self.tools())
        self.assertIn("prior contributions do not disqualify the primary", self.tools())
        readme = (PROMPTS.parents[1] / "README.md").read_text()
        bump_readme = readme.split("### Bump\n", 1)[1].split("### Optimize\n", 1)[0]
        self.assertIn("A single-agent roster is supported", bump_readme)
        for text in (self.tools(), bump_readme, *(path.read_text() for path in (PROMPTS / "bump").glob("*.md"))):
            for value in ("reserved for independent review", "Configure at least two agents",
                          "independent critic", "independent review", "independent diff review",
                          "independent of contributors", "contributor self-review is prohibited",
                          "never approve your own migration edits"):
                self.assertNotIn(value, text)

    def test_source_holes_and_semantic_review_not_claimed_as_kernel_equivalence(self):
        critic = self.phase("CRITIC")
        for value in ("do not mechanically certify cross-version semantic equivalence",
                      "Existing holes and axioms", "new holes", "failed modules",
                      "warnings", "unperformed checks"):
            self.assertIn(value, critic)
        self.assertIn("not a fresh formalization", self.phase("REPAIRING"))

    def test_separate_project_and_workflow_records_remain_evidence_backed(self):
        for role in ROLES:
            text = self.phase(role)
            self.assertTrue("record_workflow_change" in text or "workflow-change" in text)
        retrospective = self.phase("RETROSPECTIVE")
        for value in ("two records separate", "concrete problem/evidence", "observed result",
                      "Do not reopen", "do not invent success"):
            self.assertIn(value, retrospective)


if __name__ == "__main__":
    unittest.main()
