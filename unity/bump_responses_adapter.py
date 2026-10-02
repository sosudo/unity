"""Bump-local Responses namespace serialization for FreeInference.

This adapter never executes a tool. It makes the exact Codex-authorized tool
catalog visible as ordinary functions, then restores the original native names
before Codex dispatch. The native registry, approval rules and sandbox still
decide every tool call. There is no retry, fallback provider or shell bridge.
"""

from __future__ import annotations

import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import socket
import threading

from .bump_provider import is_freeinference, provider_failure

_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_BODY = 32 * 1024 * 1024
_MAX_EVENT = 4 * 1024 * 1024


class AdapterProtocolError(ValueError):
    """Bounded error categories only; never retain provider/request text."""


def strict_json_loads(data):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AdapterProtocolError("duplicate_json_key")
            result[key] = value
        return result

    def constant(_):
        raise AdapterProtocolError("invalid_json_constant")

    try:
        return json.loads(data, object_pairs_hook=object_pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterProtocolError("invalid_json") from None


def _name(value):
    if not isinstance(value, str) or not value or not _NAME.fullmatch(value):
        raise AdapterProtocolError("invalid_tool_name")
    return value


def _read_bounded(response, limit):
    chunks, size = [], 0
    for chunk in response.iter_bytes(chunk_size=65536):
        size += len(chunk)
        if size > limit:
            raise AdapterProtocolError("response_size")
        chunks.append(chunk)
    return b"".join(chunks)


def _event_lines(response):
    """Bound memory before searching for line breaks in an upstream stream."""
    pending = b""
    for chunk in response.iter_bytes():
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if len(line) > _MAX_EVENT:
                raise AdapterProtocolError("response_event_size")
            yield line.rstrip(b"\r").decode("utf-8", errors="strict")
        if len(pending) > _MAX_EVENT:
            raise AdapterProtocolError("response_event_size")
    if pending:
        yield pending.rstrip(b"\r").decode("utf-8", errors="strict")


def _safe_provider_event(agent, event):
    for container in (event, event.get("response")):
        if not isinstance(container, dict):
            continue
        direct = container.get("type") == "error"
        error = container if direct else container.get("error")
        if error:
            failure = provider_failure(agent, error)
            code = "insufficient_credits" if failure.category == "freeinference_credit_exhausted" else failure.category
            if direct:
                container.clear()
                container.update(type="error", code=code, message=code)
            else:
                container["error"] = {"code": code, "message": code}
    return event


class ToolMap:
    """One request's bijection; duplicate/unknown identities fail closed."""

    def __init__(self, body, *, history=None):
        if not isinstance(body, dict) or not isinstance(body.get("tools", []), list):
            raise AdapterProtocolError("invalid_request_tools")
        self.request_body = copy.deepcopy(body)
        self._by_flat = {}
        self._by_identity = {}
        self.history_map = dict(history or {})
        historical_flats = set()
        for identity, value in self.history_map.items():
            if (not isinstance(identity, tuple) or len(identity) != 2
                    or not isinstance(value, tuple) or len(value) != 2
                    or value[1] not in {"function", "custom"}):
                raise AdapterProtocolError("invalid_history_map")
            if identity[0] is not None:
                _name(identity[0])
            _name(identity[1])
            _name(value[0])
            if value[0] in historical_flats:
                raise AdapterProtocolError("history_identity_collision")
            historical_flats.add(value[0])
        flattened = []
        for spec in body.get("tools", []):
            if not isinstance(spec, dict):
                raise AdapterProtocolError("invalid_tool_spec")
            if spec.get("type") == "namespace":
                namespace = _name(spec.get("name"))
                nested = spec.get("tools")
                if not isinstance(nested, list) or not nested:
                    raise AdapterProtocolError("invalid_namespace_tools")
                for tool in nested:
                    if not isinstance(tool, dict) or tool.get("type") not in {"function", "custom"}:
                        raise AdapterProtocolError("unsupported_namespace_tool")
                    flattened.append(self._register(tool, namespace))
            elif spec.get("type") in {"function", "custom"}:
                flattened.append(self._register(spec, None))
            else:
                # Hosted tools retain their exact representation. They cannot
                # register a callable alias or authorize a native tool call.
                if "name" in spec:
                    raise AdapterProtocolError("unsupported_named_tool")
                flattened.append(copy.deepcopy(spec))
        self.request_body["tools"] = flattened
        inputs = self.request_body.get("input", [])
        if not isinstance(inputs, (list, str)):
            raise AdapterProtocolError("invalid_input")
        if isinstance(inputs, list):
            for item in inputs:
                self._flatten_call(item)
        choice = self.request_body.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") in {"function", "custom"}:
            self._flatten_call(choice, choice=True)
        elif isinstance(choice, dict):
            if choice.get("type") != "allowed_tools" or not isinstance(choice.get("tools"), list):
                raise AdapterProtocolError("unsupported_tool_choice")
            for item in choice["tools"]:
                if not isinstance(item, dict) or item.get("type") not in {"function", "custom"}:
                    raise AdapterProtocolError("unsupported_tool_choice")
                self._flatten_call(item, choice=True)

    def _register(self, spec, namespace):
        name = _name(spec.get("name"))
        identity = (namespace, name)
        flat = name if namespace is None else namespace + "__" + name
        if len(flat) > 64:
            flat = flat[:47] + "_" + hashlib.sha256(flat.encode()).hexdigest()[:16]
        if identity in self._by_identity or flat in self._by_flat:
            raise AdapterProtocolError("tool_identity_collision")
        if (identity in self.history_map and self.history_map[identity] != (flat, spec["type"])):
            raise AdapterProtocolError("history_identity_changed")
        if any(old != identity and value[0] == flat for old, value in self.history_map.items()):
            raise AdapterProtocolError("history_identity_collision")
        self._by_identity[identity] = flat
        self._by_flat[flat] = (namespace, name, spec["type"])
        self.history_map[identity] = (flat, spec["type"])
        result = copy.deepcopy(spec)
        result["name"] = flat
        return result

    def _flatten_call(self, item, *, choice=False):
        if not isinstance(item, dict) or (not choice and item.get("type") not in {"function_call", "custom_tool_call"}):
            return
        identity = (item.get("namespace"), item.get("name"))
        try:
            if choice:
                flat = self._by_identity[identity]
                kind = self._by_flat[flat][2]
            else:
                flat, kind = self.history_map[identity]
        except (KeyError, TypeError):
            raise AdapterProtocolError("unknown_input_tool") from None
        expected = kind if choice else ("function_call" if kind == "function" else "custom_tool_call")
        if item.get("type") != expected:
            raise AdapterProtocolError("wrong_input_tool_kind")
        item["name"] = flat
        item.pop("namespace", None)

    def _restore_call(self, item):
        if not isinstance(item, dict) or item.get("type") not in {"function_call", "custom_tool_call"}:
            return
        name = item.get("name")
        namespace = item.get("namespace")
        if namespace is not None:
            # A provider may already understand namespaces; accept only the
            # exact catalog identity rather than guessing from display text.
            try:
                name = self._by_identity[(namespace, name)]
            except (KeyError, TypeError):
                raise AdapterProtocolError("unknown_output_tool") from None
        try:
            original_namespace, original_name, kind = self._by_flat[name]
        except (KeyError, TypeError):
            raise AdapterProtocolError("unknown_output_tool") from None
        expected = "function_call" if kind == "function" else "custom_tool_call"
        if item["type"] != expected:
            raise AdapterProtocolError("wrong_output_tool_kind")
        item["name"] = original_name
        if original_namespace is not None:
            item["namespace"] = original_namespace
        else:
            item.pop("namespace", None)

    def restore_event(self, event):
        if not isinstance(event, dict):
            raise AdapterProtocolError("invalid_response_event")
        result = copy.deepcopy(event)
        self._restore_call(result)
        self._restore_call(result.get("item"))
        for response in (result, result.get("response")):
            if isinstance(response, dict) and "output" in response:
                if not isinstance(response["output"], list):
                    raise AdapterProtocolError("invalid_response_output")
                for item in response["output"]:
                    self._restore_call(item)
        return result


class NamespaceResponsesAdapter:
    """Authenticated loopback serialization only, exact upstream/model pin."""

    def __init__(self, agent, *, _client=None):
        if not is_freeinference(agent) or not agent.api_key:
            raise ValueError("Bump namespace adapter requires configured FreeInference")
        self._agent = agent
        self.api_key = secrets.token_urlsafe(32)
        self._client = _client
        self._server = self._thread = None
        self._lock = threading.Lock()
        self._responses = set()
        self._connections = set()
        # This is history serialization, never a permission cache. Tool-less
        # local compaction sends prior calls but authorizes no new tool calls.
        self._history = {}
        self._closed = threading.Event()
        self.request_count = 0

    def __enter__(self):
        import httpx
        if self._client is None:
            self._client = httpx.Client(follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(connect=20, read=600, write=60, pool=30))
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _error(self, status, category):
                data = json.dumps({"error": {"code": category, "message": category}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def do_GET(self):
                self._error(405, "adapter_method_not_allowed")

            def do_POST(self):
                streaming = False
                with owner._lock:
                    owner._connections.add(self.connection)
                try:
                    if owner._closed.is_set():
                        self._error(503, "adapter_closed")
                        return
                    if (self.client_address[0] != "127.0.0.1" or self.headers.get("Origin")
                            or self.headers.get("Host") != f"127.0.0.1:{owner._server.server_port}"
                            or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.api_key)):
                        self._error(403, "adapter_access_denied")
                        return
                    if self.path != "/v1/responses":
                        self._error(404, "adapter_unsupported_endpoint")
                        return
                    if self.headers.get("Transfer-Encoding"):
                        self._error(400, "adapter_transfer_encoding_rejected")
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        length = 0
                    if not 0 < length <= _MAX_BODY:
                        self._error(413, "adapter_request_size")
                        return
                    body = strict_json_loads(self.rfile.read(length))
                    if not isinstance(body, dict) or body.get("model") != owner._agent.model:
                        self._error(400, "adapter_model_mismatch")
                        return
                    with owner._lock:
                        mapping = ToolMap(body, history=owner._history)
                        owner._history = mapping.history_map
                        owner.request_count += 1
                    # Never forward client auth/headers, redirects, query strings
                    # or arbitrary URLs. The real key remains in this process.
                    with owner._client.stream("POST", owner._agent.base_url.rstrip("/") + "/responses",
                            headers={"Authorization": "Bearer " + owner._agent.api_key},
                            json=mapping.request_body) as upstream:
                        with owner._lock:
                            owner._responses.add(upstream)
                        try:
                            if upstream.status_code != 200:
                                raw = _read_bounded(upstream, _MAX_EVENT)
                                try:
                                    error = strict_json_loads(raw)
                                except AdapterProtocolError:
                                    error = {}
                                failure = provider_failure(owner._agent, {"httpStatusCode": upstream.status_code, "error": error})
                                code = "insufficient_credits" if failure.category == "freeinference_credit_exhausted" else failure.category
                                self._error(upstream.status_code, code)
                                return
                            content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].strip()
                            if body.get("stream") is True:
                                if content_type != "text/event-stream":
                                    raise AdapterProtocolError("expected_event_stream")
                                self.send_response(200)
                                self.send_header("Content-Type", "text/event-stream")
                                self.send_header("Connection", "close")
                                self.end_headers()
                                streaming = True
                                event_lines = []
                                size = 0
                                for line in _event_lines(upstream):
                                    if owner._closed.is_set():
                                        return
                                    size += len(line.encode())
                                    if size > _MAX_EVENT:
                                        raise AdapterProtocolError("response_event_size")
                                    if line:
                                        event_lines.append(line)
                                    else:
                                        self._event(event_lines, mapping)
                                        event_lines, size = [], 0
                                if event_lines:
                                    self._event(event_lines, mapping)
                            else:
                                raw = _read_bounded(upstream, _MAX_BODY)
                                event = _safe_provider_event(owner._agent, mapping.restore_event(strict_json_loads(raw)))
                                data = json.dumps(event).encode()
                                self.send_response(200)
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Content-Length", str(len(data)))
                                self.send_header("Connection", "close")
                                self.end_headers()
                                self.wfile.write(data)
                        finally:
                            with owner._lock:
                                owner._responses.discard(upstream)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    category = str(exc) if isinstance(exc, AdapterProtocolError) else "adapter_transport_failure"
                    try:
                        if streaming:
                            data = {"type": "error", "error": {"code": category, "message": category}}
                            self.wfile.write(("event: error\ndata: " + json.dumps(data) + "\n\n").encode())
                            self.wfile.flush()
                        else:
                            self._error(502, category)
                    except (OSError, ValueError):
                        pass
                finally:
                    self.close_connection = True
                    with owner._lock:
                        owner._connections.discard(self.connection)

            def _event(self, lines, mapping):
                data = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
                if data and data != "[DONE]":
                    event = _safe_provider_event(owner._agent, mapping.restore_event(strict_json_loads(data)))
                    lines = [line for line in lines if not line.startswith("data:")]
                    lines.append("data: " + json.dumps(event))
                self.wfile.write(("\n".join(lines) + "\n\n").encode())
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._closed.set()
        with self._lock:
            responses, connections = tuple(self._responses), tuple(self._connections)
        for response in responses:
            try:
                response.close()
            except Exception:
                pass
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
            except OSError:
                pass
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._client is not None:
            self._client.close()
