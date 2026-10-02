"""Safe native-startup observations; fake status RPC only, no inference/services."""
import asyncio
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from unity.bump_mcp_ready import wait_for_native_tools
from unity.bump_provider import BumpProviderFailure


class StartupDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    policy = {"unity-forum": ["bump_status"], "lean-lsp": ["lean_goal"]}

    def row(self, name, status="connected", tools=None):
        return {"name": name, "runtimeStatus": status,
                "tools": {key: {"description": "private-fixture"} for key in
                          (tools if tools is not None else self.policy[name])},
                "error": "private-fixture", "authStatus": "private-fixture"}

    async def run_gate(self, bodies, **kwargs):
        observations = []
        client = NS(_client=NS(request=AsyncMock(side_effect=[NS(root=body) for body in bodies])))
        try:
            result = await wait_for_native_tools(client, "fixture", self.policy, stopped=lambda: False,
                interval=.001, timeout=.03, observe=observations.append, **kwargs)
        except BumpProviderFailure as error:
            return error, observations
        return result, observations

    async def test_records_per_service_progress_without_raw_auth_errors(self):
        result, seen = await self.run_gate([
            {"data": [self.row("unity-forum"), self.row("lean-lsp", "connecting")]},
            {"data": [self.row("unity-forum"), self.row("lean-lsp")]},
        ])
        self.assertIs(result, True)
        self.assertEqual(seen[-1]["polls"], 2)
        self.assertEqual(seen[-1]["outcome"], "ready")
        self.assertEqual(seen[1]["services"]["lean-lsp"]["status"], "connecting")
        self.assertNotIn("private-fixture", json.dumps(seen))
        self.assertGreaterEqual(seen[-1]["elapsed_seconds"], seen[0]["elapsed_seconds"])

    async def test_missing_required_tools_are_exact_policy_names_only(self):
        error, seen = await self.run_gate([{"data": [self.row("unity-forum"),
            self.row("lean-lsp", tools=["private-fixture"])]}])
        self.assertEqual(error.category, "native_mcp_required_tool_missing")
        self.assertEqual(seen[-1]["services"]["lean-lsp"]["missing_tools"], ["lean_goal"])
        self.assertNotIn("private-fixture", json.dumps(seen))
        self.assertEqual(error.startup_diagnostics, seen[-1])

    async def test_unknown_server_identity_is_not_logged(self):
        error, seen = await self.run_gate([{"data": [{"name": "private-fixture"}]}])
        self.assertEqual(error.category, "native_mcp_unexpected_server")
        self.assertNotIn("private-fixture", json.dumps(seen))

    async def test_unclassified_runtime_state_never_leaks_raw_status(self):
        error, seen = await self.run_gate([{"data": [self.row("unity-forum", "private-fixture")]}])
        self.assertEqual(seen[1]["services"]["unity-forum"]["status"], "unclassified")
        self.assertNotIn("private-fixture", json.dumps(seen))

    async def test_status_timeout_remains_bounded_and_classified(self):
        async def hung(*args, **kwargs):
            await asyncio.Event().wait()
        seen = []
        with self.assertRaises(BumpProviderFailure) as caught:
            await wait_for_native_tools(NS(_client=NS(request=hung)), "fixture", self.policy,
                stopped=lambda: False, timeout=.01, observe=seen.append)
        self.assertEqual(caught.exception.category, "native_mcp_startup_timeout")
        self.assertEqual(seen[-1]["rpc_timeouts"], 1)
        self.assertEqual(seen[-1]["services"]["lean-lsp"]["status"], "not_reported")

    async def test_telemetry_failure_cannot_skip_catalog_gate(self):
        def broken(_):
            raise ValueError("private-fixture")
        client = NS(_client=NS(request=AsyncMock(return_value=NS(root={"data": [
            self.row("unity-forum", tools=[]), self.row("lean-lsp")]}))))
        with self.assertRaises(BumpProviderFailure) as caught:
            await wait_for_native_tools(client, "fixture", self.policy, stopped=lambda: False, observe=broken)
        self.assertEqual(caught.exception.category, "native_mcp_phase_catalog_mismatch")

    async def test_cancel_reports_category_and_rethrows(self):
        client = NS(_client=NS(request=AsyncMock(side_effect=asyncio.CancelledError())))
        seen = []
        with self.assertRaises(asyncio.CancelledError):
            await wait_for_native_tools(client, "fixture", self.policy, stopped=lambda: False, observe=seen.append)
        self.assertEqual(seen[-1]["outcome"], "cancelled")

    async def test_six_concurrent_worker_gates_are_independent_and_redacted(self):
        histories = [[] for _ in range(6)]
        requests = []
        clients = []
        for worker in range(6):
            async def request(method, params, *, response_model, worker=worker):
                requests.append((worker, params["threadId"]))
                await asyncio.sleep(.001 * (6 - worker))
                return NS(root={"data": [self.row("unity-forum"), self.row("lean-lsp",
                    tools=["private-fixture"] if worker == 2 else ["lean_goal"])]})
            clients.append(NS(_client=NS(request=request)))
        results = await asyncio.wait_for(asyncio.gather(*[
            wait_for_native_tools(clients[worker], "worker-" + str(worker), self.policy,
                stopped=lambda: False, timeout=.1, observe=histories[worker].append)
            for worker in range(6)], return_exceptions=True), .5)
        self.assertEqual(sum(value is True for value in results), 5)
        self.assertIsInstance(results[2], BumpProviderFailure)
        self.assertEqual(results[2].category, "native_mcp_required_tool_missing")
        self.assertEqual(set(requests), {(worker, "worker-" + str(worker)) for worker in range(6)})
        for worker, history in enumerate(histories):
            self.assertEqual(history[-1]["polls"], 1)
            self.assertEqual(history[-1]["outcome"], "native_mcp_required_tool_missing" if worker == 2 else "ready")
        self.assertNotIn("private-fixture", json.dumps(histories))

    async def test_successful_turn_accounting_retains_native_startup_evidence(self):
        from tests.test_bump_provider import ProviderTurnTests, agent, note
        from unity import bump_spawn
        bump_spawn._last_run_stats.pop(agent().name, None)
        self.addCleanup(bump_spawn._last_run_stats.pop, agent().name, None)
        await ProviderTurnTests.run_turn(self, [
            note("turn/completed", turn=NS(status="completed", error=None))])
        stats = bump_spawn._last_run_stats[agent().name]
        self.assertEqual(stats["native_mcp_startup"]["outcome"], "ready")
        self.assertEqual(stats["native_mcp_startup"]["polls"], 1)
        self.assertIn("usage", stats)


class MigrationCatalogTests(unittest.TestCase):
    def test_refinement_is_formalizing_only_coverage_is_readable_by_critic(self):
        from unity.forum.bump_server import PROFILE_TOOLS
        names = {phase: {tool.__name__ for tool in tools} for phase, tools in PROFILE_TOOLS.items()}
        self.assertIn("refine_migration", names["formalizing"])
        for phase in ("critic", "retrospective"):
            self.assertNotIn("refine_migration", names[phase])
            self.assertIn("bump_migration_plan", names[phase])
