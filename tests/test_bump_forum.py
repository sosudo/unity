import os
import unittest
from unittest.mock import patch
from unity.forum import bump_server as forum
from unity import bump_spawn


class ForumTests(unittest.TestCase):
    def test_only_migration_profiles_and_no_agent_replanning(self):
        self.assertEqual(forum.PROFILES, {"formalizing", "critic", "retrospective"})
        tools = {tool.__name__ for tool in forum.PROFILE_TOOLS["formalizing"]}
        self.assertIn("finalize_formalization", tools)
        self.assertIn("sync_from_main", tools)
        self.assertFalse(tools & {"refine_chunks", "request_rechunk", "reserve_files", "report_source_issue", "submit_source_repair"})

    def test_critic_cannot_submit_source(self):
        tools = {tool.__name__ for tool in forum.PROFILE_TOOLS["critic"]}
        self.assertIn("submit_formalization_verdict", tools)
        self.assertFalse(tools & {"finalize_formalization", "emit_formalization_candidate", "sync_from_main", "request_rechunk"})

    def test_author_identity_is_bound(self):
        with patch.dict(os.environ, {"UNITY_AGENT_NAME": "Luna1"}):
            self.assertEqual(forum._author("luna1"), "Luna1")
            with self.assertRaises(ValueError):
                forum._author("Luna2")

    def test_operational_errors_are_not_invisible_restarts(self):
        for message in ("timeout", "connection closed", "quota exhausted", "approval unavailable"):
            self.assertTrue(bump_spawn._give_up(RuntimeError(message), 1))
