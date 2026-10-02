"""Bump namespace transport boundaries; isolated canned data, never model calls."""

import copy
import json
import os
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import httpx

from unity import bump_responses_adapter as adapter_module
from unity.bump_responses_adapter import NamespaceResponsesAdapter, ToolMap, strict_json_loads


def function(name="bump_status", **extra):
    return {"type": "function", "name": name, "description": "Read bounded status",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False}, "strict": True, **extra}


def namespace(name="mcp__unity_forum", *tools):
    return {"type": "namespace", "name": name, "description": "Fixture namespace",
            "tools": list(tools) or [function()]}


def request(tools=None, **extra):
    return {"model": "glm-5.3", "stream": True, "store": False,
            "input": [{"role": "user", "content": "Read status only"}],
            "tools": tools if tools is not None else [namespace()], **extra}


def call(name, *, namespace_name=None, kind="function_call", **extra):
    result = {"type": kind, "id": "fc-fixture", "call_id": "call-fixture",
              "name": name, "arguments": "{}", **extra}
    if namespace_name is not None:
        result["namespace"] = namespace_name
    return result


def event(item, kind="response.output_item.done"):
    return {"type": kind, "output_index": 0, "item": item}


class NamespaceToolMapTests(unittest.TestCase):
    def test_flattening_preserves_exact_function_schema_and_input(self):
        source = request()
        original = copy.deepcopy(source)
        mapping = ToolMap(source)
        self.assertEqual(source, original)
        body = mapping.request_body
        self.assertEqual(body["model"], source["model"])
        self.assertEqual(body["input"], source["input"])
        self.assertEqual(len(body["tools"]), 1)
        flat = body["tools"][0]
        self.assertEqual(flat["type"], "function")
        self.assertNotIn("namespace", flat)
        self.assertEqual(flat["parameters"], function()["parameters"])
        self.assertTrue(flat["strict"])
        self.assertEqual(mapping.restore_event(event(call(flat["name"])))
                         ["item"]["namespace"], "mcp__unity_forum")
        self.assertEqual(mapping.restore_event(event(call(flat["name"])))
                         ["item"]["name"], "bump_status")

    def test_same_display_name_in_two_namespaces_is_not_aliased(self):
        mapping = ToolMap(request([namespace("first"), namespace("second")]))
        names = [tool["name"] for tool in mapping.request_body["tools"]]
        self.assertEqual(len(set(names)), 2)
        self.assertEqual([mapping.restore_event(event(call(name)))["item"]["namespace"]
                          for name in names], ["first", "second"])

    def test_mapping_is_stable_across_request_tool_reordering(self):
        tools = [namespace("first"), namespace("second", function("inspect"))]
        one = ToolMap(request(tools))
        two = ToolMap(request(list(reversed(tools))))
        for tool in one.request_body["tools"]:
            restored = two.restore_event(event(call(tool["name"])))["item"]
            self.assertIn((restored["namespace"], restored["name"]),
                          {("first", "bump_status"), ("second", "inspect")})

    def test_long_names_remain_bounded_and_do_not_collapse(self):
        # A single namespace may contain distinct long tool names.
        tools = [namespace("long_" + "a" * 50,
                           function("tool_" + "b" * 50 + "a"),
                           function("tool_" + "b" * 50 + "b"))]
        mapping = ToolMap(request(tools))
        names = [tool["name"] for tool in mapping.request_body["tools"]]
        self.assertTrue(all(len(name) <= 64 for name in names))
        self.assertEqual(len(set(names)), 2)
        for name in names:
            self.assertEqual(mapping.restore_event(event(call(name)))["item"]["namespace"],
                             "long_" + "a" * 50)

    def test_duplicate_namespace_or_tool_identity_rejected(self):
        for tools in ([namespace(), namespace()],
                      [namespace("n", function(), function())],
                      [function(), function()]):
            with self.subTest(tools=tools), self.assertRaises(ValueError):
                ToolMap(request(tools))

    def test_flat_and_namespaced_wire_name_collision_rejected(self):
        flat = ToolMap(request()).request_body["tools"][0]["name"]
        with self.assertRaises(ValueError):
            ToolMap(request([namespace(), function(flat)]))

    def test_unknown_function_never_reaches_native_dispatch(self):
        mapping = ToolMap(request())
        for name in ("fixture_unknown_tool", "register_strategy", "bump_status"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                mapping.restore_event(event(call(name)))

    def test_response_cannot_inject_a_namespace_or_change_mapped_kind(self):
        mapping = ToolMap(request())
        flat = mapping.request_body["tools"][0]["name"]
        for item in (call(flat, namespace_name="unauthorized"),
                     call(flat, kind="custom_tool_call")):
            with self.subTest(item=item), self.assertRaises(ValueError):
                mapping.restore_event(event(item))

    def test_plain_function_is_preserved_and_cannot_be_namespaced(self):
        mapping = ToolMap(request([function("exec_command")]))
        self.assertEqual(mapping.request_body["tools"], [function("exec_command")])
        restored = mapping.restore_event(event(call("exec_command")))["item"]
        self.assertEqual(restored["name"], "exec_command")
        self.assertFalse(restored.get("namespace"))
        with self.assertRaises(ValueError):
            mapping.restore_event(event(call("exec_command", namespace_name="evil")))

    def test_namespaced_function_history_is_flattened_without_touching_arguments(self):
        arguments = json.dumps({"name": "bump_status", "namespace": "mcp__unity_forum"})
        item = call("bump_status", namespace_name="mcp__unity_forum", arguments=arguments)
        output = {"type": "function_call_output", "call_id": item["call_id"],
                  "output": "native namespace=mcp__unity_forum name=bump_status"}
        mapping = ToolMap(request(input=[item, output]))
        history = mapping.request_body["input"]
        self.assertEqual(history[0]["name"], mapping.request_body["tools"][0]["name"])
        self.assertNotIn("namespace", history[0])
        self.assertEqual(history[0]["arguments"], arguments)
        self.assertEqual(history[1], output)

    def test_unknown_or_ambiguous_history_name_rejected(self):
        for item in (call("register_strategy", namespace_name="mcp__unity_forum"),
                     call("bump_status", namespace_name="other"), call("bump_status")):
            with self.subTest(item=item), self.assertRaises(ValueError):
                ToolMap(request(input=[item]))

    def test_input_history_cannot_change_registered_tool_kind(self):
        with self.assertRaises(ValueError):
            ToolMap(request(input=[call("bump_status", namespace_name="mcp__unity_forum",
                                        kind="custom_tool_call")]))

    def test_forced_function_choice_uses_same_exact_mapping(self):
        choice = {"type": "function", "name": "bump_status", "namespace": "mcp__unity_forum"}
        mapping = ToolMap(request(tool_choice=choice))
        self.assertEqual(mapping.request_body["tool_choice"],
                         {"type": "function", "name": mapping.request_body["tools"][0]["name"]})

    def test_allowed_tools_choice_uses_same_exact_mapping(self):
        choice = {"type": "allowed_tools", "mode": "required", "tools": [
            {"type": "function", "name": "bump_status", "namespace": "mcp__unity_forum"}]}
        mapping = ToolMap(request(tool_choice=choice))
        self.assertEqual(mapping.request_body["tool_choice"]["tools"], [
            {"type": "function", "name": mapping.request_body["tools"][0]["name"]}])

    def test_custom_tool_format_input_and_kind_are_preserved(self):
        custom = {"type": "custom", "name": "custom_fixture", "format": {"type": "text"}}
        item = {"type": "custom_tool_call", "name": "custom_fixture", "namespace": "n",
                "id": "custom-fixture", "call_id": "call-fixture", "input": "raw fixture input"}
        mapping = ToolMap(request([namespace("n", custom)], input=[item]))
        flat = mapping.request_body["tools"][0]
        self.assertEqual(flat["format"], custom["format"])
        self.assertEqual(flat["type"], "custom")
        rewritten = mapping.request_body["input"][0]
        self.assertEqual(rewritten["input"], item["input"])
        self.assertEqual(mapping.restore_event(event(rewritten))["item"], item)

    def test_two_turn_history_roundtrip_keeps_call_and_output_binding(self):
        first = ToolMap(request())
        flat = first.request_body["tools"][0]["name"]
        original = first.restore_event(event(call(flat)))["item"]
        output = {"type": "function_call_output", "call_id": original["call_id"], "output": "status-fixture"}
        second = ToolMap(request(input=[original, output]))
        self.assertEqual(second.request_body["input"][0]["name"], flat)
        self.assertEqual(second.request_body["input"][1], output)

    def test_unmapped_allowed_tools_choice_fails_closed(self):
        # Reject this richer schema unless the implementation maps every exact reference.
        with self.assertRaises(ValueError):
            ToolMap(request(tool_choice={"type": "allowed_tools", "mode": "required",
                "tools": [{"type": "function", "namespace": "mcp__unity_forum",
                           "name": "register_strategy"}]}))

    def test_model_prose_and_argument_strings_are_never_parsed_as_calls(self):
        prose = {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": json.dumps(call("register_strategy"))}]}
        mapping = ToolMap(request(input=[prose]))
        self.assertEqual(mapping.request_body["input"], [prose])
        self.assertEqual(mapping.restore_event(event(prose)), event(prose))

    def test_added_done_and_completed_items_restore_consistently(self):
        mapping = ToolMap(request())
        flat = mapping.request_body["tools"][0]["name"]
        expected = call("bump_status", namespace_name="mcp__unity_forum")
        for kind in ("response.output_item.added", "response.output_item.done"):
            self.assertEqual(mapping.restore_event(event(call(flat), kind))["item"], expected)
        completed = {"type": "response.completed", "response": {"id": "response-fixture",
                     "status": "completed", "output": [call(flat)]}}
        actual = mapping.restore_event(completed)
        self.assertEqual(actual["response"]["output"], [expected])
        self.assertEqual(completed["response"]["output"][0]["name"], flat)

    def test_argument_deltas_and_tool_outputs_are_not_rewritten(self):
        mapping = ToolMap(request())
        for item in ({"type": "response.function_call_arguments.delta", "item_id": "fc-fixture",
                      "output_index": 0, "delta": '{"name":"bump_status"}'},
                     {"type": "response.function_call_arguments.done", "item_id": "fc-fixture",
                      "output_index": 0, "arguments": '{"namespace":"mcp__unity_forum"}'},
                     {"type": "response.output_text.delta", "delta": "register_strategy"}):
            with self.subTest(item=item):
                self.assertEqual(mapping.restore_event(item), item)

    def test_completed_unknown_call_is_rejected_even_without_added_event(self):
        mapping = ToolMap(request())
        with self.assertRaises(ValueError):
            mapping.restore_event({"type": "response.completed",
                                   "response": {"output": [call("register_strategy")]}})

    def test_empty_tools_do_not_create_native_capabilities(self):
        mapping = ToolMap(request([]))
        self.assertEqual(mapping.request_body["tools"], [])
        with self.assertRaises(ValueError):
            mapping.restore_event(event(call("bump_status")))

    def test_toolless_compaction_rewrites_only_previously_advertised_history(self):
        prior = ToolMap(request())
        flat = prior.request_body["tools"][0]["name"]
        original = prior.restore_event(event(call(flat)))["item"]
        output = {"type": "function_call_output", "call_id": original["call_id"], "output": "status"}
        compact = ToolMap(request([], input=[original, output]), history=prior.history_map)
        self.assertEqual(compact.request_body["tools"], [])
        self.assertEqual(compact.request_body["input"][0]["name"], flat)
        self.assertNotIn("namespace", compact.request_body["input"][0])
        self.assertEqual(compact.request_body["input"][1], output)
        for item in (call(flat), original):
            with self.subTest(item=item), self.assertRaises(ValueError):
                compact.restore_event(event(item))

    def test_history_never_authorizes_tool_choice_or_allowed_tools(self):
        prior = ToolMap(request())
        choice = {"type": "function", "name": "bump_status", "namespace": "mcp__unity_forum"}
        for selection in (choice, {"type": "allowed_tools", "mode": "required", "tools": [choice]}):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                ToolMap(request([], tool_choice=selection), history=prior.history_map)

    def test_compaction_history_unknown_identity_and_wrong_kind_rejected(self):
        prior = ToolMap(request())
        for item in (call("register_strategy", namespace_name="mcp__unity_forum"),
                     call("bump_status", namespace_name="other"),
                     call("bump_status", namespace_name="mcp__unity_forum", kind="custom_tool_call")):
            with self.subTest(item=item), self.assertRaises(ValueError):
                ToolMap(request([], input=[item]), history=prior.history_map)

    def test_current_catalog_remains_only_output_authority_when_tools_change(self):
        prior = ToolMap(request())
        old_flat = prior.request_body["tools"][0]["name"]
        original = prior.restore_event(event(call(old_flat)))["item"]
        current = ToolMap(request([namespace("mcp__unity_forum", function("bump_inspect"))],
                                  input=[original]), history=prior.history_map)
        self.assertEqual(current.request_body["input"][0]["name"], old_flat)
        with self.assertRaises(ValueError):
            current.restore_event(event(call(old_flat)))
        current_flat = current.request_body["tools"][0]["name"]
        self.assertEqual(current.restore_event(event(call(current_flat)))["item"]["name"], "bump_inspect")

    def test_changed_kind_or_alias_collision_with_history_is_rejected(self):
        prior = ToolMap(request())
        flat = prior.request_body["tools"][0]["name"]
        for tools in ([function(flat)], [namespace("mcp__unity_forum", {
                "type": "custom", "name": "bump_status", "format": {"type": "text"}})]):
            with self.subTest(tools=tools), self.assertRaises(ValueError):
                ToolMap(request(tools), history=prior.history_map)

    def test_history_map_copy_keeps_prior_state_immutable(self):
        prior = ToolMap(request())
        saved = copy.deepcopy(prior.history_map)
        current = ToolMap(request([namespace("other", function("inspect"))]), history=prior.history_map)
        self.assertEqual(prior.history_map, saved)
        self.assertIsNot(current.history_map, prior.history_map)
        restored = ToolMap(request(), history=current.history_map)
        self.assertEqual(restored.request_body["tools"], prior.request_body["tools"])
        with self.assertRaises(ValueError):
            ToolMap(request([], input=[call("bump_status", namespace_name="mcp__unity_forum")]))

    def test_malformed_history_maps_fail_closed(self):
        for history in ({"bad": ("flat", "function")},
                        {("n", "a"): ("flat", "unknown")},
                        {("n", "a"): ("flat", "function"), ("n", "b"): ("flat", "function")}):
            with self.subTest(history=history), self.assertRaises(ValueError):
                ToolMap(request([]), history=history)


class AdapterJsonTests(unittest.TestCase):
    def test_strict_json_preserves_normal_unicode_data(self):
        value = {"name": "Fixture", "text": "∀ κ, ψ κ", "tools": []}
        self.assertEqual(strict_json_loads(json.dumps(value).encode()), value)

    def test_duplicate_keys_rejected_at_every_depth(self):
        for data in ('{"model":"allowed","model":"other"}',
                     '{"tools":[{"name":"one","name":"two"}]}',
                     '{"input":[{"arguments":{"a":1,"a":2}}]}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                strict_json_loads(data.encode())

    def test_nonfinite_values_and_invalid_encoding_rejected(self):
        for data in (b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
                     b'{"x":"\xff"}', b'{"model":', b'{}{}'):
            with self.subTest(data=data), self.assertRaises((ValueError, UnicodeError)):
                strict_json_loads(data)


def configured_agent(**overrides):
    values = {"backend": "codex", "model": "glm-5.3", "provider": "openai",
              "base_url": "https://freeinference.org/v1", "api_key": "upstream-fixture-secret"}
    values.update(overrides)
    return NS(**values)


def response_body(item=None):
    return {"id": "resp-fixture", "object": "response", "status": "completed",
            "output": [] if item is None else [item]}


class NamespaceAdapterHttpTests(unittest.TestCase):
    """HTTP runs only on loopback; httpx MockTransport blocks real upstream traffic."""

    def run_http(self, body=None, *, handler=None, headers=None, path="/responses", method="POST", raw=None):
        seen = []

        def upstream(req):
            seen.append(req)
            if handler is not None:
                return handler(req)
            return httpx.Response(200, json=response_body())

        upstream_client = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
        with NamespaceResponsesAdapter(configured_agent(), _client=upstream_client) as adapter:
            auth = {"Authorization": "Bearer " + adapter.api_key}
            if headers is not None:
                auth.update(headers)
            with httpx.Client(trust_env=False, timeout=5) as client:
                kwargs = {"headers": auth}
                if raw is not None:
                    kwargs["content"] = raw
                else:
                    kwargs["json"] = request(stream=False) if body is None else body
                response = client.request(method, adapter.base_url + path, **kwargs)
            self.assertNotEqual(adapter.api_key, configured_agent().api_key)
            self.assertTrue(adapter.base_url.startswith("http://127.0.0.1:"))
        return response, seen

    def test_exact_upstream_auth_model_and_no_forwarded_client_headers(self):
        response, seen = self.run_http(headers={"X-Private-Client": "do-not-forward"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url), "https://freeinference.org/v1/responses")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer upstream-fixture-secret")
        self.assertNotIn("X-Private-Client", seen[0].headers)
        self.assertEqual(json.loads(seen[0].content)["model"], "glm-5.3")
        self.assertNotIn("upstream-fixture-secret", response.text)

    def test_http_compaction_retains_history_without_authorizing_output_or_other_adapter(self):
        seen = []
        def upstream(req):
            body = json.loads(req.content)
            seen.append(body)
            flat = "mcp__unity_forum__bump_status"
            # The third request attempts an unadvertised call during compaction.
            return httpx.Response(200, json=response_body(call(flat) if len(seen) in (1, 3) else None))
        client = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
        with NamespaceResponsesAdapter(configured_agent(), _client=client) as adapter:
            auth = {"Authorization": "Bearer " + adapter.api_key}
            with httpx.Client(trust_env=False, timeout=5) as local:
                first = local.post(adapter.base_url + "/responses", headers=auth, json=request(stream=False))
                self.assertEqual(first.status_code, 200)
                prior = first.json()["output"][0]
                compact = request([], stream=False, input=[prior, {
                    "type": "function_call_output", "call_id": prior["call_id"], "output": "status"}])
                second = local.post(adapter.base_url + "/responses", headers=auth, json=compact)
                self.assertEqual(second.status_code, 200)
                self.assertEqual(seen[1]["tools"], [])
                self.assertEqual(seen[1]["input"][0]["name"], "mcp__unity_forum__bump_status")
                self.assertNotIn("namespace", seen[1]["input"][0])
                third = local.post(adapter.base_url + "/responses", headers=auth, json=compact)
                self.assertEqual(third.status_code, 502)
                self.assertIn("unknown_output_tool", third.text)
        other_client = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
        with NamespaceResponsesAdapter(configured_agent(), _client=other_client) as other:
            with httpx.Client(trust_env=False, timeout=5) as local:
                rejected = local.post(other.base_url + "/responses",
                    headers={"Authorization": "Bearer " + other.api_key}, json=compact)
                self.assertEqual(rejected.status_code, 502)
                self.assertIn("unknown_input_tool", rejected.text)
        self.assertEqual(len(seen), 3)

    def test_non_freeinference_identity_or_missing_key_rejected(self):
        for changes in ({"base_url": "http://freeinference.org/v1"},
                        {"base_url": "https://freeinference.org.evil/v1"},
                        {"base_url": "https://freeinference.org/v1?key=value"},
                        {"api_key": None}, {"backend": "claude_code"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                NamespaceResponsesAdapter(configured_agent(**changes))

    def test_bad_auth_host_origin_path_or_model_never_reaches_upstream(self):
        for kwargs in ({"headers": {"Authorization": "Bearer wrong"}},
                       {"headers": {"Host": "evil.invalid"}},
                       {"headers": {"Origin": "http://evil.invalid"}},
                       {"path": "/responses?api_key=fixture"},
                       {"path": "/responses/compact"},
                       {"body": request(model="other-model", stream=False)}):
            with self.subTest(kwargs=kwargs):
                response, seen = self.run_http(**kwargs)
                self.assertGreaterEqual(response.status_code, 400)
                self.assertEqual(seen, [])

    def test_non_post_never_reaches_upstream(self):
        response, seen = self.run_http(method="GET")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(seen, [])

    def test_duplicate_json_request_and_invalid_json_fail_before_upstream(self):
        for raw in (b'{"model":"glm-5.3","model":"other"}', b'{"model":NaN}', b'{'):
            with self.subTest(raw=raw):
                response, seen = self.run_http(raw=raw)
                self.assertGreaterEqual(response.status_code, 400)
                self.assertEqual(seen, [])
                self.assertNotIn("upstream-fixture-secret", response.text)

    def test_upstream_redirect_is_not_followed_or_leaked(self):
        response, seen = self.run_http(handler=lambda req: httpx.Response(302,
            headers={"Location": "https://evil.invalid/?secret=fixture"},
            json={"error": {"message": "private-header-fixture"}}))
        self.assertEqual(len(seen), 1)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("Location", response.headers)
        self.assertNotIn("private-header-fixture", response.text)

    def test_credit_http_error_is_classified_without_raw_diagnostics(self):
        response, seen = self.run_http(handler=lambda req: httpx.Response(402, json={
            "error": {"code": "insufficient_credits", "message": "private-header-fixture"}}))
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"]["code"], "insufficient_credits")
        self.assertNotIn("private-header-fixture", response.text)

    def test_non_json_upstream_auth_failure_retains_only_status_classification(self):
        response, _ = self.run_http(handler=lambda req: httpx.Response(401,
            content="<html>private-auth-fixture</html>"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_or_access_denied")
        self.assertNotIn("private-auth-fixture", response.text)

    def test_plain_json_function_call_restores_catalog_identity(self):
        def upstream(req):
            flat = json.loads(req.content)["tools"][0]["name"]
            return httpx.Response(200, json=response_body(call(flat)))
        response, _ = self.run_http(handler=upstream)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"][0]["name"], "bump_status")
        self.assertEqual(response.json()["output"][0]["namespace"], "mcp__unity_forum")

    def test_unknown_plain_json_call_fails_closed(self):
        response, _ = self.run_http(handler=lambda req: httpx.Response(200,
            json=response_body(call("register_strategy"))))
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("register_strategy", response.text)

    def test_stream_restores_added_done_and_completed_without_argument_mutation(self):
        def upstream(req):
            flat = json.loads(req.content)["tools"][0]["name"]
            items = [event(call(flat), "response.output_item.added"),
                     {"type": "response.function_call_arguments.delta", "item_id": "fc-fixture",
                      "output_index": 0, "delta": '{"name":"literal"}'},
                     event(call(flat)), {"type": "response.completed", "response": response_body(call(flat))}]
            content = "".join("event: " + item["type"] + "\ndata: " + json.dumps(item) + "\n\n" for item in items)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)
        response, _ = self.run_http(body=request(), handler=upstream)
        self.assertEqual(response.status_code, 200)
        data = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(data[0]["item"]["namespace"], "mcp__unity_forum")
        self.assertEqual(data[1]["delta"], '{"name":"literal"}')
        self.assertEqual(data[2]["item"]["name"], "bump_status")
        self.assertEqual(data[3]["response"]["output"][0]["namespace"], "mcp__unity_forum")

    def test_stream_unknown_native_call_emits_only_safe_error(self):
        response, _ = self.run_http(body=request(), handler=lambda req: httpx.Response(200,
            headers={"Content-Type": "text/event-stream"},
            content="data: " + json.dumps(event(call("register_strategy"))) + "\n\n"))
        self.assertIn("unknown_output_tool", response.text)
        self.assertNotIn("register_strategy", response.text)

    def test_sse_multiline_data_and_fragmented_unicode_restore_without_loss(self):
        class Fragments(httpx.SyncByteStream):
            def __iter__(self):
                payload = ('data: {"type":"response.output_text.delta",\r\n'
                           'data: "delta":"∀κ"}\r\n\r\n').encode()
                for byte in payload:
                    yield bytes([byte])
        response, _ = self.run_http(body=request(), handler=lambda req: httpx.Response(200,
            headers={"Content-Type": "text/event-stream"}, stream=Fragments()))
        data = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(data, [{"type": "response.output_text.delta", "delta": "∀κ"}])

    def test_unbounded_sse_line_and_json_response_rejected(self):
        with patch.object(adapter_module, "_MAX_EVENT", 1024):
            response, _ = self.run_http(body=request(), handler=lambda req: httpx.Response(200,
                headers={"Content-Type": "text/event-stream"}, content=b"x" * 2048))
        self.assertIn("response_event_size", response.text)
        self.assertNotIn("x" * 30, response.text)
        with patch.object(adapter_module, "_MAX_BODY", 1024):
            response, _ = self.run_http(handler=lambda req: httpx.Response(200,
                json={"output": [], "fixture": "x" * 2048}))
        self.assertEqual(response.status_code, 502)
        self.assertIn("response_size", response.text)

    def test_small_sse_event_is_forwarded_before_eof_and_exit_closes_active_stream(self):
        class BlockingStream(httpx.SyncByteStream):
            def __init__(self):
                self.started, self.closed = threading.Event(), threading.Event()

            def __iter__(self):
                self.started.set()
                yield b'data: {"type":"response.created","response":{"output":[]}}\n\n'
                self.closed.wait(5)

            def close(self):
                self.closed.set()

        stream = BlockingStream()
        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200,
            headers={"Content-Type": "text/event-stream"}, stream=stream)), trust_env=False)
        observed = threading.Event()
        with NamespaceResponsesAdapter(configured_agent(), _client=client) as adapter:
            def consume():
                try:
                    with httpx.Client(trust_env=False, timeout=5) as local_client:
                        with local_client.stream("POST", adapter.base_url + "/responses",
                                headers={"Authorization": "Bearer " + adapter.api_key}, json=request()) as response:
                            for line in response.iter_lines():
                                if line.startswith("data:"):
                                    observed.set()
                except httpx.TransportError:
                    # The adapter's intentional shutdown may truncate the HTTP stream.
                    pass
            consumer = threading.Thread(target=consume, daemon=True)
            consumer.start()
            started = stream.started.wait(2)
            forwarded = observed.wait(1)
        consumer.join(timeout=2)
        self.assertTrue(started)
        self.assertTrue(forwarded, "a complete small SSE event was buffered until EOF")
        self.assertTrue(stream.closed.is_set())
        self.assertFalse(consumer.is_alive())

    def test_sse_top_level_error_does_not_leak_provider_message_or_param(self):
        response, _ = self.run_http(body=request(), handler=lambda req: httpx.Response(200,
            headers={"Content-Type": "text/event-stream"}, content="data: " + json.dumps({
                "type": "error", "code": "insufficient_credits", "message": "private-header-fixture",
                "param": "secret-param-fixture"}) + "\n\n"))
        self.assertIn("insufficient_credits", response.text)
        self.assertNotIn("private-header-fixture", response.text)
        self.assertNotIn("secret-param-fixture", response.text)

    def test_nonstream_failed_response_does_not_leak_provider_error(self):
        response, _ = self.run_http(handler=lambda req: httpx.Response(200, json={
            "id": "fixture", "status": "failed", "output": [],
            "error": {"code": "provider_error", "message": "private-header-fixture"}}))
        self.assertNotIn("private-header-fixture", response.text)

    def test_adapter_exit_closes_listener_and_upstream_client(self):
        upstream_client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
        with NamespaceResponsesAdapter(configured_agent(), _client=upstream_client) as adapter:
            url, token = adapter.base_url, adapter.api_key
        self.assertTrue(upstream_client.is_closed)
        with httpx.Client(trust_env=False, timeout=1) as client, self.assertRaises(httpx.TransportError):
            client.post(url + "/responses", headers={"Authorization": "Bearer " + token}, json=request())


class NamespaceSpawnBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_keeps_critic_native_authorization_and_hides_upstream_key_from_child(self):
        from tests.test_bump_provider import ProviderTurnTests, agent, note
        # The helper temporarily patches sys.modules for its SDK double. Load
        # native-backed Forum dependencies first so that restoration does not
        # unload Python wrappers while cryptography retains native type state.
        from unity.forum import bump_server  # noqa: F401

        probe = ProviderTurnTests("runTest")
        with patch.dict(os.environ, {"FREEINFERENCE_API_KEY": "inherited-fixture-secret",
                                     "FREEINFERENCE_SESSION_TOKEN": "inherited-fixture-session"}):
            await probe.run_turn([note("turn/completed", turn=NS(status="completed", error=None))])
        config = probe.config_ctor.call_args.kwargs
        self.assertNotEqual(config["env"]["CODEX_API_KEY"], agent().api_key)
        self.assertNotIn("FREEINFERENCE_API_KEY", config["env"])
        self.assertNotIn("FREEINFERENCE_SESSION_TOKEN", config["env"])
        overrides = config["config_overrides"]
        self.assertIn('sandbox_mode="read-only"', overrides)
        self.assertIn('approval_policy="never"', overrides)
        enabled = next(value.split("=", 1)[1] for value in overrides
                       if value.startswith("mcp_servers.unity-forum.enabled_tools="))
        tools = json.loads(enabled)
        self.assertIn("bump_status", tools)
        self.assertIn("submit_formalization_verdict", tools)
        self.assertNotIn("register_strategy", tools)
        self.assertNotIn("finalize_formalization", tools)
        self.assertNotIn("fixture_unknown_tool", tools)
        self.assertEqual(probe.client.thread_start.call_args.kwargs["sandbox"], "read-only")
        self.assertEqual(probe.client.thread_start.call_args.kwargs["model"], "glm-5.3")
        probe.client.login_api_key.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
