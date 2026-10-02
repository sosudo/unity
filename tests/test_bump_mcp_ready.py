"""Native startup gate tests; no models or real service connections."""
import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from unity.bump_mcp_ready import wait_for_native_tools
from unity.bump_provider import BumpProviderFailure
from unity.bump_spawn import _bump_codex_feature_overrides


POLICY = {"unity-forum": ("bump_status", "submit_formalization_verdict")}


def connected(policy=POLICY):
    return NS(root={"data":[{"name":name,"runtimeStatus":"connected",
                            "tools":{tool:{} for tool in names}} for name,names in policy.items()]})


class ReadyTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, responses, *, policy=POLICY, stopped=lambda:False, timeout=.1):
        self.rpc=AsyncMock(side_effect=responses)
        return await wait_for_native_tools(NS(_client=NS(request=self.rpc)),"thread",policy,
                                           stopped=stopped,timeout=timeout,interval=.001)

    async def failure(self,responses,category,**kwargs):
        with self.assertRaises(BumpProviderFailure) as result:
            await self.check(responses,**kwargs)
        self.assertEqual(result.exception.category,category)
        self.assertEqual(result.exception.provider,"native_mcp")
        return result.exception

    async def test_initial_empty_and_connecting_waits_before_success(self):
        result=await self.check([NS(root={"data":[]}),NS(root={"data":[{
            "name":"unity-forum","runtimeStatus":"connecting","tools":{}}]}),connected()])
        self.assertTrue(result)
        self.assertEqual(self.rpc.await_count,3)
        self.assertEqual(self.rpc.call_args.args[0],"mcpServerStatus/list")
        self.assertEqual(self.rpc.call_args.args[1]["threadId"],"thread")

    async def test_ready_catalog_does_not_probe_or_infer(self):
        self.assertTrue(await self.check([connected()]))
        self.rpc.assert_awaited_once()

    async def test_wrong_phase_or_missing_forum_tool_fails_closed(self):
        for names in (("bump_status",),("bump_status","submit_formalization_verdict","finalize_formalization")):
            with self.subTest(names=names):
                await self.failure([connected({"unity-forum":names})],"native_mcp_phase_catalog_mismatch")

    async def test_failed_service_does_not_wait_or_retry(self):
        for status in ("failed","error","errored","disabled","disconnected"):
            with self.subTest(status=status):
                await self.failure([NS(root={"data":[{"name":"unity-forum","runtimeStatus":status}]})],
                                   "native_mcp_service_unavailable")

    async def test_unknown_or_duplicate_server_fails_closed(self):
        row=connected().root['data'][0]
        for rows in ([row,row],[row,{**row,'name':'untrusted'}]):
            with self.subTest(rows=rows):
                await self.failure([NS(root={'data':rows})],"native_mcp_unexpected_server")

    async def test_missing_server_reaches_bounded_timeout(self):
        client=NS(_client=NS(request=AsyncMock(return_value=NS(root={'data':[]}))))
        with self.assertRaises(BumpProviderFailure) as result:
            await wait_for_native_tools(client,'thread',POLICY,stopped=lambda:False,timeout=.02,interval=.001)
        self.assertEqual(result.exception.category,'native_mcp_startup_timeout')

    async def test_hung_status_request_is_bounded(self):
        async def hung(*args,**kwargs):
            await asyncio.Event().wait()
        client=NS(_client=NS(request=hung))
        with self.assertRaises(BumpProviderFailure) as result:
            await wait_for_native_tools(client,'thread',POLICY,stopped=lambda:False,timeout=.01)
        self.assertEqual(result.exception.category,'native_mcp_startup_timeout')

    async def test_stop_before_status_query(self):
        self.assertFalse(await self.check([],stopped=lambda:True))
        self.rpc.assert_not_awaited()

    async def test_stop_during_startup_returns_without_dispatch(self):
        status=[False,False,True]
        self.assertFalse(await self.check([NS(root={'data':[]})],stopped=lambda:status.pop(0)))

    async def test_task_cancellation_is_not_reclassified(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.check([asyncio.CancelledError()])

    async def test_error_body_is_not_retained(self):
        exc=await self.failure([RuntimeError('private-header-secret')],'native_mcp_status_failed')
        self.assertNotIn('private-header-secret',str(exc))

    async def test_invalid_status_shapes_fail_closed(self):
        for body in ({}, {'data':{}}, {'data':[1]}, {'data':[{}]}, {'data':[],'nextCursor':'next'}):
            with self.subTest(body=body):
                await self.failure([NS(root=body)],'native_mcp_invalid_status')

    async def test_invalid_catalog_is_not_empty_success(self):
        await self.failure([NS(root={'data':[{'name':'unity-forum','runtimeStatus':'connected','tools':[]} ]})],
                           'native_mcp_invalid_catalog')

    async def test_optional_service_names_may_be_absent(self):
        policy={**POLICY,'lean-lsp':('lean_goal','lean_state_search'),'axle':('check','highlight')}
        actual={**POLICY,'lean-lsp':('lean_goal',),'axle':('check',)}
        self.assertTrue(await self.check([connected(actual)],policy=policy))

    async def test_required_service_tool_absence_rejects(self):
        policy={**POLICY,'lean-lsp':('lean_goal','lean_state_search')}
        await self.failure([connected({**POLICY,'lean-lsp':('lean_state_search',)})],
                           'native_mcp_required_tool_missing',policy=policy)

    async def test_raw_service_extras_do_not_grant_permissions(self):
        policy={**POLICY,'lean-lsp':('lean_goal',)}
        self.assertTrue(await self.check([connected({**POLICY,'lean-lsp':('lean_goal','unapproved_extra')})],policy=policy))
        self.assertEqual(policy['lean-lsp'],('lean_goal',))

    async def test_missing_forum_policy_is_never_ready(self):
        await self.failure([],'native_mcp_invalid_policy',policy={})


class FeatureTests(unittest.TestCase):
    def test_exact_roster_precludes_native_untracked_fanout(self):
        for phase in ('retrospective','formalizing','critic'):
            self.assertIn('features.multi_agent=false',_bump_codex_feature_overrides(phase))

    def test_critic_only_restricts_native_features_no_removed_flag(self):
        flags=_bump_codex_feature_overrides('critic')
        for value in ('features.shell_tool=false','features.goals=false','features.view_image=false',
                      'features.apps=false','features.plugins=false','features.browser_use=false',
                      'features.computer_use=false','features.image_generation=false','web_search="disabled"'):
            self.assertIn(value,flags)
        self.assertFalse(any('apply_patch_freeform' in value for value in flags))
        for phase in ('retrospective','formalizing'):
            self.assertEqual(_bump_codex_feature_overrides(phase),('features.multi_agent=false',))
