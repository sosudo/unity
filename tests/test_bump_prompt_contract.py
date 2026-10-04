"""Bump prompt/tool repair contracts, without model or Lean calls."""

import asyncio
import inspect
from pathlib import Path
import re
import unittest

from unity.forum import bump_server as server


PROMPTS = Path(__file__).resolve().parents[1] / "unity/prompts"


class BumpPromptContractTests(unittest.TestCase):
    def phase(self, name):
        return (PROMPTS / "bump" / f"{name}.md").read_text()

    def tools(self, name):
        return (PROMPTS / f"BUMP_{name}_TOOLS.md").read_text()

    def test_critic_catalog_matches_actual_optional_repair_signature(self):
        text = self.tools("CRITIC")
        match = re.search(r"`submit_formalization_verdict\(([^)]*)\)`", text)
        self.assertIsNotNone(match)
        documented = [part.strip() for part in match.group(1).split(",")]
        parameters = inspect.signature(server.submit_formalization_verdict).parameters
        self.assertEqual([part.rstrip("?") for part in documented], list(parameters))
        for part in documented:
            parameter = parameters[part.rstrip("?")]
            self.assertEqual(part.endswith("?"), parameter.default is not inspect.Parameter.empty)
        self.assertIsNone(parameters["representation_repairs"].default)

    def test_controller_repair_permission_is_not_an_agent_tool(self):
        for profile in ("formalizing", "critic"):
            with self.subTest(profile=profile):
                names = {tool.name for tool in asyncio.run(server.build_server(profile).list_tools())}
                self.assertNotIn("prepare_formal_worktree", names)
                self.assertNotIn("begin_manifest_repair_attempt", names)
                self.assertNotIn("repair_available_to", names)
                self.assertTrue({"bump_task", "bump_brief"}.issubset(names))
                for unavailable in ("submit_solution_candidate", "reopen_solving", "propose_source_fix"):
                    self.assertNotIn(unavailable, names)

    def test_focused_repair_prompts_preserve_source_and_acceptance_boundaries(self):
        formal = self.phase("FORMALIZING")
        catalog = self.tools("FORMALIZING")
        for text in (formal, catalog):
            for required in ("manifest_repairs", "output_manifest", "reopen_representations",
                             "cleared", "independent", "source"):
                self.assertIn(required, text)
        self.assertIn("Finalization checks current submission blockers before staging or committing", formal)
        self.assertIn("not a submitted candidate", formal)
        self.assertIn("There is no generated solution paper or informal-solving phase", formal)
        self.assertIn("Do not edit the supplied source", formal)
        self.assertIn("submit_source_repair", formal)
        self.assertIn("Original source bytes remain unchanged", formal)
        self.assertIn("Submission preflight runs before staging/committing", catalog)
        self.assertIn("not an accepted proof or a semantic approval", catalog)

    def test_critic_requests_do_not_replace_independent_source_repair_review(self):
        critic = self.phase("CRITIC")
        catalog = self.tools("CRITIC")
        for text in (critic, catalog):
            for required in ("representation_repairs", "reopen_tasks", "lean_reopen",
                             "output_manifest", "representation", "reason", "independent"):
                self.assertIn(required, text)
        self.assertIn("every adopted", catalog)
        self.assertIn("repair_reviews", catalog)
        self.assertIn("Do not edit Lean/source during review", critic)
        self.assertIn("or review a correction you just", critic)
        self.assertNotIn("Leave `repair_reviews` empty", critic)
        self.assertNotIn("empty `repair_reviews`", catalog)

    def test_no_solve_only_tool_in_changed_prompt_catalogs(self):
        for role in ("FORMALIZING", "CRITIC"):
            for text in (self.phase(role), self.tools(role)):
                with self.subTest(role=role, prefix=text[:60]):
                    for unavailable in ("solve_task(", "solve_brief(", "solve_status(",
                                        "reopen_solving", "propose_source_fix", "submit_solution_candidate"):
                        self.assertNotIn(unavailable, text)
                    self.assertNotIn("accepted paper candidate", text)

    def test_critic_documents_nested_repair_step_contract(self):
        critic = self.phase("CRITIC")
        catalog = self.tools("CRITIC")
        for text in (critic, catalog):
            with self.subTest(prefix=text[:60]):
                self.assertIn("repair_steps", text)
                self.assertRegex(text, r"1(?:--|–)8 nonblank")
                self.assertIn("1000 characters", text)
                self.assertIn("not_checked", text)
                self.assertIn("invent", text.lower())
                self.assertIn("source obligation", text)
        self.assertIn("Legacy reviews that predate this field", critic)
        self.assertIn('"repair_steps": []', critic)
        self.assertRegex(critic, r'"status": "fail"[\s\S]*?"repair_steps": \[')
        self.assertIn("concrete missing\nconstruction or proof obligation", critic)
        self.assertIn("defaults to `[]` only for legacy reviews", catalog)
        self.assertRegex(catalog, r'"status": "fail"[\s\S]*?"repair_steps": \[')

    def test_bumpr_consumes_critic_feedback_without_self_approval(self):
        formal = self.phase("FORMALIZING")
        catalog = self.tools("FORMALIZING")
        for text in (formal, catalog):
            with self.subTest(prefix=text[:60]):
                self.assertIn("critic_feedback", text)
                self.assertIn("direct", text)
                self.assertIn("upstream", text)
                self.assertIn("repair_steps", text)
                self.assertIn("provenance", text)
                self.assertIn("revalidate", text)
                self.assertIn("Compilation", text)
                self.assertIn("independent critic", text)
                self.assertIn("notes", text)
        self.assertIn("map every applicable repair", formal)
        self.assertIn("actual declaration(s), dependency resolution(s)", formal)
        self.assertIn("candidate-rejection blocker", formal)
        self.assertIn("do not wait indefinitely", formal)
        self.assertIn("do not resolve the critic's semantic finding", formal)
        self.assertIn("map every\n  applicable `repair_steps` item", catalog)
        self.assertIn("verdict/snapshot/reviewed-main provenance", catalog)
        self.assertIn("concrete candidate-rejection blocker", catalog)
        self.assertIn("resubmit promptly", catalog)


if __name__ == "__main__":
    unittest.main()
