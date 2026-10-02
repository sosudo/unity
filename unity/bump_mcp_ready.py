"""Bump's bounded native MCP startup gate; no model turns or permission changes."""

from __future__ import annotations

import asyncio
import hashlib
import json

from .bump_provider import BumpProviderFailure

# These optional names are absent in the pinned service catalogs. Their absence
# cannot make a required configured service unavailable; unknown tools still
# receive no permission from the existing per-tool Codex policy.
_OPTIONAL = {"lean-lsp": {"lean_state_search"}, "axle": {"highlight"}}


def _failure(category):
    return BumpProviderFailure(category, provider="native_mcp")


async def wait_for_native_tools(codex, thread_id, policy, *, stopped,
                                timeout=60.0, interval=.1, observe=None):
    """Wait for the exact required services before any inference is dispatched.

    SDK 0.157 has no public status wrapper. Use the documented app-server RPC
    through its existing client, without creating another MCP connection. The
    return value is false only for an intentional controller stop.
    """
    from pydantic import RootModel

    if (not isinstance(policy, dict) or not policy.get("unity-forum")
            or len(policy) > 32 or any(not isinstance(name, str)
                or not isinstance(names, (tuple, list)) or len(names) > 512
                or any(not isinstance(tool, str) for tool in names)
                for name, names in policy.items())):
        raise _failure("native_mcp_invalid_policy")
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + timeout
    polls = 0
    rpc_timeouts = 0
    last_rpc_seconds = 0.0
    services = {name: {"status": "not_reported", "missing_tools": sorted(names),
                       "tool_count": 0, "catalog_sha256": None}
                for name, names in sorted(policy.items())}

    def publish(outcome):
        # Only trusted policy names and fixed categories leave this gate. Never
        # retain server errors, authentication fields or unknown catalog names.
        value = {"outcome": outcome, "elapsed_seconds": round(loop.time() - started, 6),
                 "polls": polls, "rpc_timeouts": rpc_timeouts,
                 "last_rpc_seconds": round(last_rpc_seconds, 6),
                 "services": {name: dict(row) for name, row in services.items()}}
        if observe is not None:
            try:
                observe(value)
            except Exception:
                pass  # Optional telemetry cannot relax or obstruct the gate.
        return value

    def fail(category):
        error = _failure(category)
        error.startup_diagnostics = publish(category)
        return error

    publish("starting")
    while True:
        if stopped():
            publish("stopped")
            return False
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise fail("native_mcp_startup_timeout")
        rpc_started = loop.time()
        polls += 1
        try:
            result = await asyncio.wait_for(codex._client.request(
                "mcpServerStatus/list", {"threadId": thread_id, "detail": "toolsAndAuthOnly"},
                response_model=RootModel[dict]), min(remaining, 5.0))
        except asyncio.CancelledError:
            last_rpc_seconds = loop.time() - rpc_started
            publish("cancelled")
            raise
        except asyncio.TimeoutError:
            last_rpc_seconds = loop.time() - rpc_started
            rpc_timeouts += 1
            publish("status_rpc_timeout")
            continue
        except Exception:
            last_rpc_seconds = loop.time() - rpc_started
            raise fail("native_mcp_status_failed") from None
        last_rpc_seconds = loop.time() - rpc_started
        body = getattr(result, "root", None)
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or body.get("nextCursor"):
            raise fail("native_mcp_invalid_status")
        actual = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise fail("native_mcp_invalid_status")
            name = row["name"]
            if name in actual or name not in policy:
                raise fail("native_mcp_unexpected_server")
            actual[name] = row
        ready = set(actual) == set(policy)
        for name in set(policy) - set(actual):
            services[name] = {"status": "not_reported", "missing_tools": sorted(policy[name]),
                              "tool_count": 0, "catalog_sha256": None}
        for name, row in actual.items():
            status = row.get("runtimeStatus")
            services[name]["status"] = (status if status in {
                "connected", "connecting", "starting", "failed", "errored", "error",
                "disabled", "disconnected"} else "unclassified")
            if status in {"failed", "errored", "error", "disabled", "disconnected"}:
                raise fail("native_mcp_service_unavailable")
            if status != "connected":
                ready = False
                continue
            tools = row.get("tools")
            if not isinstance(tools, dict):
                raise fail("native_mcp_invalid_catalog")
            expected = set(policy[name])
            if any(not isinstance(tool, str) for tool in tools):
                raise fail("native_mcp_invalid_catalog")
            services[name].update(tool_count=len(tools),
                catalog_sha256=hashlib.sha256(json.dumps(sorted(tools),
                    separators=(",", ":")).encode()).hexdigest(),
                missing_tools=sorted((expected - _OPTIONAL.get(name, set())) - set(tools)))
            if name == "unity-forum":
                if set(tools) != expected:
                    raise fail("native_mcp_phase_catalog_mismatch")
            elif (expected - _OPTIONAL.get(name, set())) - set(tools):
                raise fail("native_mcp_required_tool_missing")
        if stopped():
            publish("stopped")
            return False
        if ready:
            publish("ready")
            return True
        publish("waiting")
        await asyncio.sleep(min(interval, max(0, deadline - loop.time())))
