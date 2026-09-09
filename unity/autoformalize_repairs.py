"""On-demand source-issue repair turns, without a mandatory pipeline phase."""

from __future__ import annotations

import asyncio
import json
import os

from . import artifacts, autoformalize_state, worktree
from .autoformalize_orchestrator import _preamble, build_autoformalize_mcp, load_prompt, load_role_prompt, stop_requested
from .autoformalize_spawn import spawn


def repair_attempt_limit() -> int | float:
    value = os.getenv("MAX_ATTEMPTS", "").strip()
    return int(value) if value else float("inf")


async def source_repair_turn(
    agent, roster, paths, issue_id: str, max_attempts: int | float,
    *, interrupt_event: asyncio.Event | None = None,
) -> dict:
    """Atomically claim one issue and run exactly one outer repair attempt."""
    from .autoformalize_runtime import _agent_runtime_env, _formal_worktree, forum_brief

    if stop_requested(paths.project_root):
        return {"status": "stopped"}
    claim = autoformalize_state.claim_source_issue(paths.forum, issue_id, agent.name, max_attempts)
    if claim["status"] != "claimed":
        return claim
    error = ""
    output_artifact = ""
    try:
        state = autoformalize_state.load_state(paths.forum)
        tree = _formal_worktree(paths.project_root, agent.name)
        worktree.link_runtime_state(tree, paths.project_root)
        worktree.symlink_lake_cache(tree, paths.project_root)
        env = _agent_runtime_env(paths, state, agent.name)
        env["UNITY_AUTOFORMALIZE_PROFILE"] = "source_repair"
        system = (
            _preamble(agent, roster, icrl_enabled=False)
            + "\n" + load_role_prompt("SOURCE_REPAIR")
            + "\n" + load_prompt("AUTOFORMALIZE_SOURCE_REPAIR_TOOLS")
            + "\n" + forum_brief(paths, "source_repair", agent.name)
        )
        result = await spawn(
            agent, system,
            "Repair this exact source issue, or report why it cannot be repaired faithfully:\n"
            + json.dumps(claim["issue"], sort_keys=True)
            + "\nSubmit an evidence-backed source-repair proposal through the Forum. "
              "Never edit supplied source files or Lean project files during this repair turn. "
              "Your worktree may contain an unfinished formalization; preserve it unchanged.",
            tree, build_autoformalize_mcp(paths, "source_repair"),
            interrupt_event=interrupt_event, env_overrides=env, own_process_group=True,
            mcp_profile="autoformalize",
            log_context={"command": "autoformalize", "run_id": state.get("run_id"),
                         "phase": state["phase"], "role": "source_repair", "issue_id": issue_id,
                         "attempt_id": claim["attempt_id"]},
        )
        if isinstance(result, BaseException):
            error = f"{type(result).__name__}: {result}"
        elif isinstance(result, str) and result.strip():
            output_artifact = artifacts.store_text(
                paths.artifacts, result,
                kind="source_repair_output", producer=agent.name, source=issue_id,
                metadata={"attempt_id": claim["attempt_id"]},
            )["artifact_id"]
    except asyncio.CancelledError:
        error = "source repair interrupted"
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        issue = autoformalize_state.finish_source_repair_attempt(
            paths.forum, issue_id, agent.name, claim["attempt_id"], error=error,
            output_artifact=output_artifact,
        )
    attempt = next(row for row in issue["attempts"] if row["attempt_id"] == claim["attempt_id"])
    return {"status": "attempted", "issue": issue, "error": attempt["error"]}


async def run_source_repairs(roster, paths, max_attempts, issue_ids=None) -> dict:
    """Rotate the existing roster over requested open issues; never run model rounds."""
    allowed = set(issue_ids) if issue_ids is not None else None
    agents = [roster.primary] + [a for a in roster.agents if a.name != roster.primary.name]
    processed = set()
    while not stop_requested(paths.project_root):
        state = autoformalize_state.load_state(paths.forum)
        issue = next((issue for issue in autoformalize_state.ready_source_issues(state)
                      if issue["issue_id"] not in processed
                      and (allowed is None or issue["issue_id"] in allowed)), None)
        if issue is None:
            break
        issue_id = issue["issue_id"]
        processed.add(issue_id)
        exhausted = set()
        for agent in agents:
            while not stop_requested(paths.project_root):
                result = await source_repair_turn(agent, roster, paths, issue_id, max_attempts)
                status = result["status"]
                if status == "exhausted":
                    exhausted.add(agent.name)
                    break
                if status != "attempted" or result["issue"].get("status") != "open":
                    break
            state = autoformalize_state.load_state(paths.forum)
            if not any(item["issue_id"] == issue_id for item in autoformalize_state.ready_source_issues(state)):
                break
        if len(exhausted) == len(agents):
            autoformalize_state.mark_source_issue_unresolved(
                paths.forum, issue_id, "Every configured agent exhausted its source-repair attempts",
            )
        if stop_requested(paths.project_root):
            break
    return autoformalize_state.load_state(paths.forum)
