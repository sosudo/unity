"""On-demand source-issue repair turns, without a mandatory pipeline phase."""

from __future__ import annotations

import asyncio
import json
import os

from . import solve_state, worktree
from .solve_formal_orchestrator import _preamble, build_solve_formal_mcp, load_prompt, load_role_prompt, stop_requested
from .solve_formal_spawn import spawn
from .solve_representation import source_diagnosis_input, source_diagnosis_current


def repair_attempt_limit() -> int | float:
    value = os.getenv("MAX_ATTEMPTS", "").strip()
    return int(value) if value else float("inf")


async def source_repair_turn(
    agent, roster, paths, issue_id: str, max_attempts: int | float,
    *, interrupt_event: asyncio.Event | None = None,
) -> dict:
    """Atomically claim one issue and run exactly one outer repair attempt."""
    from .solve_formal_runtime import _agent_runtime_env, _formal_worktree, forum_brief

    if stop_requested(paths.project_root):
        return {"status": "stopped"}
    claim = solve_state.claim_source_issue(paths.forum, issue_id, agent.name, max_attempts)
    if claim["status"] != "claimed":
        return claim
    error = ""
    try:
        state = solve_state.load_state(paths.forum)
        diagnosis = source_diagnosis_input(state, issue_id)
        tree = _formal_worktree(paths.project_root, agent.name)
        worktree.link_runtime_state(tree, paths.project_root)
        worktree.symlink_lake_cache(tree, paths.project_root)
        env = _agent_runtime_env(paths, state, agent.name)
        env["UNITY_SOLVE_PROFILE"] = "source_repair"
        system = (
            _preamble(agent, roster, icrl_enabled=False)
            + "\n" + load_role_prompt("SOURCE_REPAIR")
            + "\n" + load_prompt("SOLVE_SOURCE_REPAIR_TOOLS")
            + "\n" + forum_brief(paths, "source_repair", agent.name)
        )
        result = await spawn(
            agent, system,
            "Diagnose this exact reported issue before assuming the source is defective:\n"
            + json.dumps(claim["issue"], sort_keys=True)
            + "\nExact diagnosis input: " + json.dumps(diagnosis, sort_keys=True)
            + "\nSubmit source diagnosis first: false alarm, encoding error, actual source defect, or uncertain. "
              "For a confirmed paper defect, write a corrected complete paper to a private scratch "
              "artifact and call propose_source_fix, or call reopen_solving if more informal work is needed. "
              "Corrections require independent solution review. Never edit accepted PROOF.tex or Lean "
              "project files during this repair turn. Preserve unfinished formalization work.",
            tree, build_solve_formal_mcp(paths, "source_repair"),
            interrupt_event=interrupt_event, env_overrides=env, own_process_group=True,
            mcp_profile="solve",
            log_context={"command": "solve", "run_id": state.get("run_id"),
                         "phase": state["phase"], "role": "source_repair", "issue_id": issue_id,
                         "attempt_id": claim["attempt_id"]},
        )
        if isinstance(result, BaseException):
            error = f"{type(result).__name__}: {result}"
    except asyncio.CancelledError:
        error = "source repair interrupted"
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        current = solve_state.load_state(paths.forum)
        if issue_id in current.get("source_issues", {}):
            issue = solve_state.finish_source_repair_attempt(
                paths.forum, issue_id, agent.name, claim["attempt_id"], error=error,
            )
        else:
            # Reopening the paper archives old formalization issues atomically.
            issue = {**claim["issue"], "status": "superseded"}
    current = solve_state.load_state(paths.forum)
    if (current.get("phase") in {"chunking", "formalizing", "critic"}
            and issue_id in current.get("source_issues", {})
            and (source_diagnosis_current(current, issue_id) or {}).get("verdict") == "source_defect"):
        # The worker had an opportunity to submit a complete local paper fix.
        # If it only diagnosed/proposed a repair, return that evidence to the
        # existing informal loop rather than adopting different mathematics.
        def reopen():
            from .solve_formal_runtime import _merge_lock
            with _merge_lock(paths.project_root):
                latest = solve_state.load_state(paths.forum)
                if (latest.get("phase") in {"chunking", "formalizing", "critic"}
                        and issue_id in latest.get("source_issues", {})
                        and (source_diagnosis_current(latest, issue_id) or {}).get("verdict") == "source_defect"):
                    solve_state.reopen_solution(
                        paths.forum, agent.name,
                        f"Confirmed source issue {issue_id}: {issue.get('description', '')}. "
                        "Review the archived diagnosis/repair evidence and submit a corrected paper.",
                    )
        await asyncio.to_thread(reopen)
        issue = {**issue, "status": "superseded"}
    return {"status": "attempted", "issue": issue, "error": error}


async def run_source_repairs(roster, paths, max_attempts, issue_ids=None) -> dict:
    """Rotate the existing roster over requested open issues; never run model rounds."""
    allowed = set(issue_ids) if issue_ids is not None else None
    agents = [roster.primary] + [a for a in roster.agents if a.name != roster.primary.name]
    processed = set()
    while not stop_requested(paths.project_root):
        state = solve_state.load_state(paths.forum)
        if state.get("phase") not in {"chunking", "formalizing", "critic"}:
            break
        issue = next((issue for issue in solve_state.ready_source_issues(state)
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
            state = solve_state.load_state(paths.forum)
            if not any(item["issue_id"] == issue_id for item in solve_state.ready_source_issues(state)):
                break
        if len(exhausted) == len(agents):
            solve_state.mark_source_issue_unresolved(
                paths.forum, issue_id, "Every configured agent exhausted its source-repair attempts",
            )
        if stop_requested(paths.project_root):
            break
    return solve_state.load_state(paths.forum)
