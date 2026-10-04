"""Protect the inherited retry and worker lifecycle during Bump adaptation.

These checks compare the behavioral control-flow source with the independent
Formalize implementation. The migration planner and compiler checks are allowed
to differ; transport exhaustion must still take Formalize's worker-exit path.
No providers, subprocesses, native tools, or evaluation runs are used.
"""

import ast
import inspect
import math
import os
from pathlib import Path
import textwrap
import unittest
from unittest.mock import patch

from unity import bump_runtime, bump_spawn, formalize_runtime, formalize_spawn


def normalized_tree(source):
    source = source.replace("BUMP", "FORMALIZE").replace("Bump", "Formalize").replace("bump", "formalize")
    return ast.parse(textwrap.dedent(source))


def semantic_source(function):
    return ast.dump(normalized_tree(inspect.getsource(function)), include_attributes=False)


def worker_exit_blocks(module):
    tree = normalized_tree(inspect.getsource(module.run_formalizing_runtime))
    expected = ast.dump(ast.parse("for name, running in list(tasks.items()):\n    pass").body[0].target)
    blocks = [node for node in ast.walk(tree) if isinstance(node, ast.For)
              and ast.dump(node.target) == expected
              and ast.unparse(node.iter) == "list(tasks.items())"]
    if not blocks:
        raise AssertionError("expected worker lifecycle loops")
    return [ast.dump(node, include_attributes=False) for node in blocks]


class BumpRestartParityTests(unittest.TestCase):
    def test_migration_role_prompts_preserve_declaration_strategy_and_final_gate(self):
        root = Path(bump_spawn.__file__).parent / "prompts"
        worker = (root / "bump/FORMALIZING.md").read_text()
        planner = (root / "bump/CHUNKING.md").read_text()
        critic = (root / "bump/CRITIC.md").read_text()
        tools = (root / "BUMP_FORMALIZING_TOOLS.md").read_text()
        for text in (worker, planner, critic, tools):
            with self.subTest(prefix=text[:60]):
                self.assertIn("original", text)
                self.assertIn("declaration", text)
                self.assertIn("selected", text)
                self.assertIn("provisional", text.lower())
                self.assertIn("independent", text)
                self.assertNotIn("project_scope=changes", text)
                self.assertNotIn("Prefer one module per independent task", text)
                self.assertNotIn("proof holes are allowed", text)
                self.assertNotIn("may contain theorem proof holes", text)
        self.assertIn("immutable original Lean", " ".join(worker.split()))
        self.assertIn("A failing file is not a whole-module assignment", worker)
        self.assertIn("Another declaration in the same", worker)
        self.assertIn("No new holes", tools)
        self.assertIn("compiler-driven", planner)
        self.assertIn("no need\nfor a repair assignment", planner)
        self.assertIn("compiled unchanged", critic)
        self.assertIn("migration_review.native_complete", critic)

    def test_retry_decisions_and_backoff_remain_copied_from_formalize(self):
        self.assertEqual(bump_spawn._PERMANENT, formalize_spawn._PERMANENT)
        for name in ("_max_retries", "_retry_sleep", "_give_up", "_idle_guard",
                     "_terminate_process_group"):
            with self.subTest(function=name):
                self.assertEqual(semantic_source(getattr(bump_spawn, name)),
                                 semantic_source(getattr(formalize_spawn, name)))

    def test_worker_cleanup_and_failed_launch_recovery_remain_copied(self):
        for name in ("_cancel", "_formal_launch_retry_key"):
            with self.subTest(function=name):
                self.assertEqual(semantic_source(getattr(bump_runtime, name)),
                                 semantic_source(getattr(formalize_runtime, name)))

    def test_transport_exception_uses_the_same_worker_completion_path(self):
        # Inherited exception handling logs the failure, removes the finished
        # worker and lets ordinary eligibility reschedule it. A successful empty
        # turn still records a yield through the unchanged Formalize path.
        self.assertEqual(worker_exit_blocks(bump_runtime), worker_exit_blocks(formalize_runtime))

    def test_removed_transport_quarantine_cannot_reappear(self):
        source = Path(bump_runtime.__file__).read_text()
        for legacy in ("transport_blocked", "BumpTransportRetriesExhausted",
                       "transport_retries_exhausted"):
            self.assertNotIn(legacy, source)

    def test_rate_limits_keep_the_inherited_bounded_backend_retry_behavior(self):
        failure = RuntimeError("429 Too Many Requests")
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "5"}):
            for module in (bump_spawn, formalize_spawn):
                with self.subTest(module=module.__name__):
                    self.assertEqual(module._retry_sleep(failure), 60.0)
                    self.assertFalse(module._give_up(failure, 4))
                    self.assertTrue(module._give_up(failure, 5))
        with patch.dict(os.environ, {"MAX_ATTEMPTS": ""}):
            self.assertTrue(math.isinf(bump_spawn._max_retries()))
            self.assertFalse(bump_spawn._give_up(failure, 1000))

    def test_non_rate_limit_retry_behavior_also_matches(self):
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "5"}):
            transient = RuntimeError("server temporarily unavailable")
            permanent = RuntimeError("authentication failed")
            self.assertEqual(bump_spawn._retry_sleep(transient), 600.0)
            self.assertFalse(bump_spawn._give_up(transient, 4))
            self.assertTrue(bump_spawn._give_up(transient, 5))
            self.assertFalse(bump_spawn._give_up(permanent, 1))
            self.assertTrue(bump_spawn._give_up(permanent, 2))


if __name__ == "__main__":
    unittest.main()
