"""Native Claude restrictions for autoformalize's private worktrees only.

The Bash sandbox permits the controller-supplied runtime/cache paths; native
file edits stay in the private source tree. MCP servers remain trusted Unity
components, not children confined by Claude's Bash sandbox.
"""

import json
from pathlib import Path


def claude_worktree_options(cwd: Path, writable_roots: tuple[Path, ...]) -> dict:
    """Return SDK options without changing user settings or launching a model."""
    from claude_agent_sdk import HookMatcher

    source_root = cwd.resolve()
    shared = tuple(dict.fromkeys(path.resolve() for path in writable_roots))
    if any(path != source_root and source_root.is_relative_to(path) for path in shared):
        raise ValueError("Claude worktree access cannot include an ancestor of its source tree")

    async def restrict_tool(data, tool_use_id, context):
        name = data.get("tool_name", "")
        arguments = data.get("tool_input") or {}
        reason = ""
        if name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
            value = arguments.get("notebook_path" if name == "NotebookEdit" else "file_path")
            try:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("missing file path")
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = Path(data.get("cwd") or source_root) / path
                if not path.resolve().is_relative_to(source_root):
                    reason = f"Edit only your private worktree: {source_root}. Use Unity MCP for shared state."
            except (OSError, ValueError, RuntimeError):
                reason = "Cannot resolve this write safely; provide a path inside your private worktree."
        elif name == "Bash" and arguments.get("dangerouslyDisableSandbox"):
            reason = "Autoformalize shell commands must stay inside the native sandbox."
        elif name in {"ExitPlanMode", "EnterWorktree", "ExitWorktree"}:
            reason = "Unity manages permissions and worktrees; continue in your assigned worktree."
        else:
            return {}
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if reason else "allow",
            **({"permissionDecisionReason": reason} if reason else {}),
        }}

    return {
        "permission_mode": "dontAsk",
        "allowed_tools": [
            "Bash", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "Agent", "Task", "TodoWrite",
            "mcp__unity-forum__*", "mcp__lean-lsp__*", "mcp__axle__*", "mcp__aristotle__*",
        ],
        # SDK 0.1.59 omits --setting-sources when setting_sources=[]. Pass the
        # empty CLI value explicitly so inherited settings cannot widen writes.
        "extra_args": {"setting-sources": ""},
        "settings": json.dumps({"sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "excludedCommands": [],
            "filesystem": {"allowWrite": [str(path) for path in shared]},
            # Claude 2.1.105 rejects '*'. Keep package downloads available;
            # other research URLs use WebFetch, and MCP networking is separate.
            "network": {"allowedDomains": [
                "github.com", "*.github.com", "*.githubusercontent.com",
                "pypi.org", "files.pythonhosted.org", "releases.lean-lang.org",
                "lakecache.blob.core.windows.net", "mathlib4.lean-cache.cloud", "cache.mathlib.org",
            ]},
        }}),
        "hooks": {"PreToolUse": [HookMatcher(
            matcher="^(Write|Edit|MultiEdit|NotebookEdit|Bash|ExitPlanMode|EnterWorktree|ExitWorktree)$",
            hooks=[restrict_tool],
        )]},
    }
