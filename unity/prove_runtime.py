"""Event-driven proving runtime built around authoritative Forum state."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from rich.console import Console

from . import library, prove_state, worktree
from .forum import server as forum_server
from .orchestrator import _preamble, load_prompt, stop_requested
from .spawn import spawn


_console = Console()


def _workspace_brief(paths, author: str) -> str:
    if os.getenv("UNITY_FORUM_BRIEF", "on").lower() == "off":
        return ""
    forum_server.FORUM_DIR = paths.forum
    forum_server.PROJECT_ROOT = paths.project_root
    text = forum_server.build_brief(author)
    return f"\nWorkspace brief (refresh with forum_brief):\n{text}\n" if text else ""


async def run_prove_runtime(
    roster,
    paths,
    mcp: dict,
    base_prompt: str,
    *,
    reset_state: bool = False,
) -> dict:
    """Run cancellable agent turns until the prove workspace becomes quiescent."""
    root = paths.project_root
    if stop_requested(root):
        return prove_state.load_state(paths.forum)

    main_sha = worktree.main_commit(root)
    initial_state = prove_state.initialize_from_dag(
        paths.forum, paths.unity / "dag.json", main_sha, reset=reset_state
    )

    worktrees = {}
    for agent in roster.agents:
        agent_tree = worktree.create_worktree(agent.name, root)
        worktree.symlink_lake_cache(agent_tree, root)
        worktree.link_runtime_state(agent_tree, root)
        worktrees[agent.name] = agent_tree

    tools_ref = load_prompt("PROVE_TOOLS")
    context = library.library_context()
    shared_prompt = base_prompt + "\n\n" + tools_ref
    if context:
        shared_prompt += "\n\n" + context
    subagents = library.library_subagents()
    agents = {agent.name: agent for agent in roster.agents}
    tasks: dict[str, asyncio.Task] = {}
    seen_events: set[str] = {
        event["event_id"] for event in initial_state.get("events", [])
    }
    pending_sync: set[str] = set()

    normal_task = (
        "Coordinate proof strategies through the Forum. Discuss useful approaches, register them, "
        "atomically claim one, and implement it in your worktree. Run lake build before committing "
        "and calling emit_candidate. Review submitted candidates immediately. If an acceptable "
        "candidate is merged, call sync_from_main before claiming more work. Continue until no "
        "unresolved declaration has useful work for you."
    )

    def launch(agent_name: str, task_prompt: str = normal_task) -> None:
        existing = tasks.get(agent_name)
        if existing is not None and not existing.done():
            return
        agent = agents[agent_name]
        system = (
            _preamble(agent, roster, icrl_enabled=False)
            + _workspace_brief(paths, agent_name)
            + shared_prompt
        )
        tasks[agent_name] = asyncio.create_task(
            spawn(agent, system, task_prompt, worktrees[agent_name], mcp, subagents=subagents),
            name=f"prove:{agent_name}",
        )

    async def cancel(agent_name: str, reason: str) -> None:
        task = tasks.get(agent_name)
        if task is None or task.done():
            return
        _console.print(f"[yellow]interrupting {agent_name}: {reason}[/yellow]")
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    for agent in roster.agents:
        launch(agent.name)

    quiescent_revision = None
    try:
        while not stop_requested(root):
            await asyncio.sleep(0.5)
            for name, task in list(tasks.items()):
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _console.print(f"[red]agent {name} failed: {exc!r}[/red]")
                del tasks[name]
                if name in pending_sync:
                    current_main = worktree.main_commit(root)
                    current = prove_state.load_state(paths.forum)
                    already_synced = any(
                        event.get("kind") == "worktree_synced"
                        and event.get("author") == name
                        and event.get("main_sha") == current_main
                        for event in current.get("events", [])
                    )
                    if not already_synced:
                        # The MCP tool is the normal agent path. This deterministic fallback
                        # prevents a short/failed model turn from leaving obsolete source behind.
                        released = prove_state.release_author_claims(
                            paths.forum, name, "merge synchronization fallback"
                        )
                        synced = worktree.force_sync_from_main(root, name)
                        prove_state.record_sync(paths.forum, name, synced["main_sha"], released)
                    pending_sync.discard(name)
                    current = prove_state.load_state(paths.forum)
                    owns_work = any(
                        strategy.get("owner") == name and strategy.get("status") == "claimed"
                        for strategy in current.get("strategies", {}).values()
                    )
                    if not prove_state.all_solved(current) and owns_work:
                        launch(name)

            state = prove_state.load_state(paths.forum)
            fresh_events = [event for event in state.get("events", [])
                            if event["event_id"] not in seen_events]
            for event in fresh_events:
                seen_events.add(event["event_id"])
                kind = event.get("kind")
                if kind == "candidate_submitted":
                    candidate = state["candidates"].get(event["candidate_id"], {})
                    if candidate.get("status") != "submitted":
                        continue
                    author = candidate.get("author")
                    paused_owners = {
                        state["strategies"].get(strategy_id, {}).get("owner")
                        for strategy_id in event.get("paused_strategies", [])
                    } - {None, author}
                    reviewers = set(paused_owners)
                    if not reviewers:
                        idle = [name for name in agents if name != author and name not in tasks]
                        active = [name for name in agents if name != author and name in tasks]
                        if idle or active:
                            reviewers.add((idle or active)[0])
                    review_task = (
                        f"Candidate {event['candidate_id']} for {event['decl']} was submitted at "
                        f"commit {candidate.get('commit_sha')}. Treat this as an interrupt: stop "
                        "speculative proving for that declaration, inspect the exact commit and diff, "
                        "then call endorse_candidate or object_candidate with concrete reasoning. If it "
                        "becomes acceptable, call sync_to_main."
                    )
                    for reviewer in reviewers:
                        await cancel(reviewer, f"candidate {event['candidate_id']} needs review")
                        launch(reviewer, review_task)
                elif kind in {"candidate_endorsed", "candidate_objection_resolved"}:
                    candidate = state["candidates"].get(event["candidate_id"], {})
                    if candidate.get("status") != "acceptable":
                        continue
                    merger = event.get("author")
                    if merger == candidate.get("author"):
                        merger = next(
                            (name for name in agents if name != candidate.get("author")), None
                        )
                    if merger in agents:
                        await cancel(merger, f"candidate {event['candidate_id']} is acceptable")
                        launch(
                            merger,
                            f"Candidate {event['candidate_id']} for {candidate.get('decl')} is now "
                            "acceptable: it has an independent endorsement and no open objections. "
                            "Call sync_to_main now. If synchronization fails, report the exact error "
                            "and resume an unblocked strategy.",
                        )
                elif kind == "candidate_objected":
                    candidate = state["candidates"].get(event["candidate_id"], {})
                    if candidate.get("status") != "blocked":
                        continue
                    wake = {candidate.get("author")}
                    wake.update(
                        state["strategies"].get(strategy_id, {}).get("owner")
                        for strategy_id in event.get("unpaused_strategies", [])
                    )
                    repair_task = (
                        f"Candidate {event['candidate_id']} for {event['decl']} has objection "
                        f"{event['objection_id']}. Refresh prove_status/forum_brief, address the "
                        "objection or resume a distinct proof strategy, and submit a new immutable "
                        "candidate if source bytes change."
                    )
                    for name in wake - {None}:
                        await cancel(name, f"candidate {event['candidate_id']} was objected")
                        author_instruction = (
                            " Your prior candidate commit is not a clean base for a new one. First "
                            "call sync_from_main to discard it, then register and claim a repair "
                            "strategy and commit the complete corrected patch with supersedes set."
                            if name == candidate.get("author") else ""
                        )
                        launch(name, repair_task + author_instruction)
                elif kind == "candidate_merge_failed":
                    artifact_note = (
                        f" Full build output is {event['build_artifact_id']}."
                        if event.get("build_artifact_id") else ""
                    )
                    retry_task = (
                        f"Candidate {event['candidate_id']} for {event['decl']} failed to merge: "
                        f"{event.get('error', '')[:1000]}.{artifact_note} Refresh prove_status, "
                        "inspect the failure, and "
                        "continue your claimed strategy or register a corrected strategy."
                    )
                    owners = {
                        state["strategies"].get(strategy_id, {}).get("owner")
                        for strategy_id in event.get("unpaused_strategies", [])
                    } - {None}
                    for name in owners:
                        await cancel(name, f"candidate {event['candidate_id']} failed to merge")
                        launch(name, retry_task)
                elif kind == "candidate_merged":
                    for name in list(tasks):
                        await cancel(name, f"{event['decl']} merged into main")
                    sync_task = (
                        f"Main advanced to {event['main_sha']} after candidate "
                        f"{event['candidate_id']} solved {event['decl']}. Immediately call "
                        "sync_from_main; your current tracked and untracked attempt work will be "
                        "discarded. Then refresh prove_status and claim a registered strategy for an "
                        "unresolved declaration, or help register/review one."
                    )
                    pending_sync.update(agents)
                    for name in agents:
                        launch(name, sync_task)
                elif kind == "worktree_synced":
                    pending_sync.discard(event.get("author"))
                elif kind == "strategy_registered":
                    idle = next((name for name in agents
                                 if name != event.get("author") and name not in tasks), None)
                    if idle:
                        launch(idle, normal_task)

            if prove_state.all_solved(state) and not pending_sync:
                return state

            if tasks:
                quiescent_revision = None
                continue
            if quiescent_revision is None:
                quiescent_revision = state.get("revision", 0)
                continue
            if state.get("revision", 0) == quiescent_revision:
                return state
            quiescent_revision = state.get("revision", 0)
        return prove_state.load_state(paths.forum)
    finally:
        for name in list(tasks):
            await cancel(name, "prove runtime ending")
        for agent in roster.agents:
            prove_state.release_author_claims(paths.forum, agent.name, "prove runtime ended")
        for agent in roster.agents:
            worktree.cleanup_worktree(agent.name, worktrees[agent.name], root)
