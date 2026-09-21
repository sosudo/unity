"""Informal solving loop for ``unity solve``.

This follows :mod:`unity.prove_runtime`: the command owns phase order, while a
runtime launches cancellable workers, consumes authoritative Forum events, and
returns when its current phase becomes quiescent or advances.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
from pathlib import Path

from rich.console import Console

from . import artifacts, library, solve_jobs, solve_state
from .forum import solve_server
from .orchestrator import _preamble, load_prompt, stop_requested
from .spawn import spawn


_console = Console()


def configure_forum(paths, profile: str) -> None:
    solve_server.configure(paths.forum, paths.project_root, profile)


def forum_brief(paths, profile: str, author: str) -> str:
    if os.getenv("UNITY_FORUM_BRIEF", "on").lower() == "off":
        return ""
    configure_forum(paths, profile)
    try:
        return solve_server.solve_brief(author)
    except Exception:
        return ""


def _shared_prompt(paths, roster, agent, profile: str, base_prompt: str, tools_prompt: str) -> str:
    context = library.library_context()
    brief = forum_brief(paths, profile, agent.name)
    prompt = _preamble(agent, roster, icrl_enabled=False)
    if brief:
        prompt += f"\nSolve workspace brief (refresh with solve_brief):\n{brief}\n"
    prompt += base_prompt + "\n\n" + tools_prompt
    if context:
        prompt += "\n\n" + context
    return prompt


_CANCEL_GRACE_SECONDS = 20.0
_CANCEL_HARD_SECONDS = 10.0


async def _cancel(
    agent,
    task: asyncio.Task,
    interrupt: asyncio.Event,
    reason: str,
    project_root: Path | None = None,
) -> None:
    if task.done():
        return
    _console.print(f"[yellow]interrupting {agent.name}: {reason}[/yellow]")
    try:
        if agent.backend == "codex":
            interrupt.set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=_CANCEL_GRACE_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                pass
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=_CANCEL_HARD_SECONDS,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    finally:
        if project_root is not None:
            await asyncio.to_thread(
                solve_jobs.terminate, project_root, owner=agent.name,
            )


def _draft_path(paths, agent_name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", agent_name)
    path = paths.unity / "source" / "drafts" / safe / "PROOF.tex"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _agent_runtime_env(paths, state: dict, agent_name: str) -> dict[str, str]:
    """Give solve workers isolated, disposable temp space without affecting prove."""
    run_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(state.get("run_id") or "unknown-run"))
    safe_agent = re.sub(r"[^a-zA-Z0-9_-]", "_", agent_name)
    scratch = paths.unity / "tmp" / run_id / safe_agent
    scratch.mkdir(parents=True, exist_ok=True)
    value = str(scratch.resolve())
    result = {
        "TMPDIR": value,
        "TMP": value,
        "TEMP": value,
        "PIP_REQUIRE_VIRTUALENV": "true",
        "PIP_DISABLE_PIP_VERSION_CHECK": "true",
    }
    return result




def reset_solve_workspace(paths) -> None:
    """Clear only solve-owned transient documents on a fresh solve run."""
    drafts = paths.unity / "source" / "drafts"
    if drafts.exists():
        shutil.rmtree(drafts)
    scratch = paths.unity / "tmp"
    if scratch.exists():
        shutil.rmtree(scratch)
    (paths.unity / "source").mkdir(parents=True, exist_ok=True)
    (paths.unity / "source" / "PROOF.tex").unlink(missing_ok=True)
    (paths.unity / "dag.json").unlink(missing_ok=True)
    (paths.unity / "formalization-plan.json").unlink(missing_ok=True)
    for path in paths.forum.glob("solve-*.json"):
        if path.name != "solve-state.json":
            path.unlink(missing_ok=True)


def materialize_solution(paths, candidate: dict) -> Path:
    """Write the exact accepted solution artifact to the canonical PROOF.tex."""
    payload = artifacts.artifact_bytes(paths.artifacts, candidate["artifact_id"])
    digest = hashlib.sha256(payload).hexdigest()
    if digest != candidate["sha256"]:
        raise ValueError("accepted solution artifact hash does not match candidate state")
    target = paths.unity / "source" / "PROOF.tex"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


async def run_solving_runtime(roster, paths, mcp: dict, base_prompt: str) -> dict:
    """Swarm informal solving until a candidate interrupts the phase or workers quiesce."""
    if stop_requested(paths.project_root):
        return solve_state.load_state(paths.forum)
    configure_forum(paths, "solving")
    tools_prompt = load_prompt("SOLVE_SOLVING_TOOLS")
    subagents = library.library_subagents()
    agents = {agent.name: agent for agent in roster.agents}
    agent_names = {solve_state.author_key(name): name for name in agents}
    tasks: dict[str, asyncio.Task] = {}
    interrupts: dict[str, asyncio.Event] = {}
    worker_targets: dict[str, str] = {}
    reviewer_results: dict[str, str] = {}
    submission_nudges: set[str] = set()
    attempted_targets: set[str] = set()
    state = solve_state.load_state(paths.forum)
    seen = {event["event_id"] for event in state.get("events", [])}
    pending_reviews = {
        result_id for result_id, result in state.get("informal_results", {}).items()
        if result.get("status") == "submitted"
    }

    def launch(
        name: str,
        followup: str = "",
        *,
        target_task: str = "",
        review_result: str = "",
    ) -> None:
        if name in tasks and not tasks[name].done():
            return
        agent = agents[name]
        draft = _draft_path(paths, name)
        current = solve_state.load_state(paths.forum)
        task_context = ""
        if target_task:
            informal = current.get("informal_tasks", {}).get(target_task, {})
            worker_targets[name] = target_task
            if not review_result:
                attempted_targets.add(target_task)
            task_context = (
                f"Your current informal task is `{target_task}` ({informal.get('kind', 'task')}): "
                f"{informal.get('title', '')}. {informal.get('description', '')} "
                "Register and claim a distinct strategy targeting this task. "
            )
        if review_result:
            reviewer_results[name] = review_result
        draft_context = ""
        if draft.is_file() and draft.stat().st_size:
            payload = draft.read_bytes()
            draft_context = (
                f"You already have a nonempty draft at `{draft.relative_to(paths.project_root)}` "
                f"({len(payload)} bytes, SHA-256 {hashlib.sha256(payload).hexdigest()}). Read those "
                "exact bytes before doing new research. If they already form a complete rigorous "
                "solution, emit_solution_candidate immediately. "
            )
        task_prompt = draft_context + (followup or (task_context +
            "Collaboratively solve the original problem in natural language. Coordinate through "
            "the solve Forum, but develop your own exact candidate paper at "
            f"`{draft.relative_to(paths.project_root)}`. You may investigate privately before a "
            "direction is coherent; then refresh the brief, register and claim a distinct strategy, "
            "publish useful findings early, and ask for help on concrete blockers. Submit the exact "
            "draft immediately once it is a complete rigorous solution. Continue useful work until "
            "the candidate interrupt or genuine quiescence."
        ))
        event = asyncio.Event()
        interrupts[name] = event
        tasks[name] = asyncio.create_task(
            spawn(
                agent,
                _shared_prompt(paths, roster, agent, "solving", base_prompt, tools_prompt),
                task_prompt,
                paths.project_root,
                mcp,
                subagents=subagents,
                interrupt_event=event,
                log_context={
                    "command": "solve", "run_id": current.get("run_id"), "phase": "solving",
                    "task_id": target_task or None,
                    "role": "component_reviewer" if review_result else "solver",
                    "result_id": review_result or None,
                },
                env_overrides=_agent_runtime_env(paths, current, name),
                own_process_group=True,
                mcp_profile="solve",
            ),
            name=f"solve:solving:{name}",
        )

    def launch_idle_work(*, include_tasks: bool = True) -> None:
        current = solve_state.load_state(paths.forum)
        idle = [name for name in agents if name not in tasks]
        assigned_reviews = set(reviewer_results.values())
        for result_id in list(pending_reviews):
            if result_id in assigned_reviews:
                continue
            result = current.get("informal_results", {}).get(result_id, {})
            if result.get("status") != "submitted":
                pending_reviews.discard(result_id)
                continue
            reviewer = next((
                name for name in idle
                if solve_state.author_key(name) != solve_state.author_key(result.get("author"))
            ), None)
            if reviewer is None:
                break
            idle.remove(reviewer)
            launch(
                reviewer,
                f"Independently check informal component `{result_id}` for task "
                f"`{result.get('task_id')}` at immutable artifact `{result.get('artifact_id')}` "
                f"with SHA-256 `{result.get('sha256')}`. Call review_informal_result with `support` "
                "only if the exact argument is correct and reusable; otherwise call `object` with "
                "the concrete defect. Submit the review as soon as you have decisive support or one "
                "concrete blocking defect, then end the turn. Do not search for a stronger objection "
                "or an alternate proof after recording the verdict.",
                target_task=result.get("task_id", ""),
                review_result=result_id,
            )
            assigned_reviews.add(result_id)
        if not include_tasks:
            return
        ready = solve_state.ready_informal_tasks(current)
        if not ready:
            return
        claimed_targets = {
            strategy.get("target") for strategy in current.get("strategies", {}).values()
            if strategy.get("phase") == "solving" and strategy.get("status") == "claimed"
        }
        active_targets = {
            worker_targets[name] for name in tasks if name in worker_targets
        }
        preferred = [
            task for task in ready
            if task["task_id"] not in claimed_targets
            and task["task_id"] not in active_targets
            and task["task_id"] not in attempted_targets
        ]
        for name, task in zip(idle, preferred):
            launch(name, target_task=task["task_id"])

    launch_idle_work()
    if not state.get("informal_tasks"):
        for agent in roster.agents:
            if agent.name not in tasks:
                launch(agent.name)

    try:
        while not stop_requested(paths.project_root):
            await asyncio.sleep(0.5)
            state = solve_state.load_state(paths.forum)
            if state.get("phase") != "solving":
                await asyncio.gather(*(
                    _cancel(agents[name], task, interrupts[name], "solution candidate submitted",
                            paths.project_root)
                    for name, task in list(tasks.items())
                ))
                return state

            workers_finished = False
            for name, task in list(tasks.items()):
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _console.print(f"[red]informal solver {name} failed: {exc!r}[/red]")
                tasks.pop(name, None)
                interrupts.pop(name, None)
                target = worker_targets.get(name, "")
                current = solve_state.load_state(paths.forum)
                informal = current.get("informal_tasks", {}).get(target, {})
                draft = _draft_path(paths, name)
                can_nudge = (
                    informal.get("kind") == "synthesis"
                    and not current.get("solution", {}).get("current_candidate")
                    and draft.is_file()
                    and draft.stat().st_size > 0
                    and name not in submission_nudges
                    and name not in reviewer_results
                )
                if can_nudge:
                    submission_nudges.add(name)
                    launch(
                        name,
                        "Submission check only: read your existing draft before any new research. "
                        "Your first substantive action must be either emit_solution_candidate if "
                        "the draft is a complete rigorous solution, or publishing one precise "
                        "blocker explaining why it cannot yet be submitted. Do not begin another "
                        "research trajectory in this turn.",
                        target_task=target,
                    )
                    continue
                workers_finished = True
                worker_targets.pop(name, None)
                reviewer_results.pop(name, None)
                solve_state.release_author_claims(
                    paths.forum, name, "informal solver turn ended without a candidate"
                )
            for event in solve_state.events_after(state, seen):
                seen.add(event["event_id"])
                kind = event.get("kind")
                if kind in {"strategy_registered", "strategy_claimed", "strategy_assisted"}:
                    author = agent_names.get(solve_state.author_key(event.get("author")))
                    if event.get("phase") == "solving" and author:
                        worker_targets[author] = event.get("target", "")
                elif kind == "informal_task_created":
                    launch_idle_work()
                elif kind == "informal_result_submitted":
                    pending_reviews.add(event["result_id"])
                    target = event.get("task_id", "")
                    affected = [
                        name for name, running in tasks.items()
                        if not running.done() and worker_targets.get(name) == target
                    ]
                    await asyncio.gather(*(
                        _cancel(agents[name], tasks[name], interrupts[name],
                                f"informal component {event['result_id']} submitted for review",
                                paths.project_root)
                        for name in affected
                    ))
                    for name in affected:
                        tasks.pop(name, None)
                        interrupts.pop(name, None)
                        worker_targets.pop(name, None)
                        reviewer_results.pop(name, None)
                    launch_idle_work()
                elif kind == "informal_result_supported":
                    pending_reviews.discard(event["result_id"])
                    target = event.get("task_id", "")
                    affected = [
                        name for name, running in tasks.items()
                        if not running.done()
                        and worker_targets.get(name) == target
                        and reviewer_results.get(name) != event["result_id"]
                    ]
                    await asyncio.gather(*(
                        _cancel(agents[name], tasks[name], interrupts[name],
                                f"informal task {target} resolved", paths.project_root)
                        for name in affected
                    ))
                    for name in affected:
                        tasks.pop(name, None)
                        interrupts.pop(name, None)
                        worker_targets.pop(name, None)
                    launch_idle_work()
                elif kind in {"informal_task_superseded", "informal_task_invalidated"}:
                    target = event.get("task_id", "")
                    attempted_targets.discard(target)
                    affected = [
                        name for name, running in tasks.items()
                        if not running.done() and worker_targets.get(name) == target
                    ]
                    await asyncio.gather(*(
                        _cancel(
                            agents[name], tasks[name], interrupts[name],
                            event.get("reason") or f"informal task {target} superseded",
                            paths.project_root,
                        )
                        for name in affected
                    ))
                    for name in affected:
                        tasks.pop(name, None)
                        interrupts.pop(name, None)
                        worker_targets.pop(name, None)
                        reviewer_results.pop(name, None)
                    launch_idle_work()
                elif kind == "informal_result_objected":
                    pending_reviews.discard(event["result_id"])
                    result = state.get("informal_results", {}).get(event["result_id"], {})
                    author = agent_names.get(solve_state.author_key(result.get("author")))
                    if author in agents:
                        if author in tasks:
                            await _cancel(agents[author], tasks[author], interrupts[author],
                                          f"informal result {event['result_id']} objected",
                                          paths.project_root)
                            tasks.pop(author, None)
                            interrupts.pop(author, None)
                        launch(
                            author,
                            f"Your informal component `{event['result_id']}` was objected to: "
                            f"{event.get('review', '')}. Refresh solve_brief, repair the exact issue, "
                            "and submit a new immutable component with `supersedes` set.",
                            target_task=event.get("task_id", ""),
                        )
                    launch_idle_work()
                elif kind == "question_asked":
                    recipient = agent_names.get(solve_state.author_key(event.get("to")))
                    if recipient in agents and recipient not in tasks:
                        launch(
                            recipient,
                            f"Question `{event['question_id']}` is addressed to you. Refresh "
                            "solve_brief, answer it with concrete evidence, then continue useful work.",
                            target_task=event.get("target", ""),
                        )
                elif kind == "obstacle_reported":
                    helper = next((
                        name for name in agents
                        if solve_state.author_key(name) != solve_state.author_key(event.get("author"))
                        and name not in tasks
                    ), None)
                    if helper:
                        launch(
                            helper,
                            f"Help resolve obstacle `{event['obstacle_id']}`. Refresh solve_brief, "
                            "inspect what was tried, publish a concrete finding or answer, and assist "
                            "the existing strategy rather than duplicating it.",
                            target_task=event.get("target", ""),
                        )
                if event.get("kind") == "solution_candidate_submitted":
                    await asyncio.gather(*(
                        _cancel(agents[name], task, interrupts[name],
                                f"solution candidate {event['candidate_id']} submitted",
                                paths.project_root)
                        for name, task in list(tasks.items())
                    ))
                    return solve_state.load_state(paths.forum)

            if workers_finished:
                launch_idle_work()
            if not tasks:
                return state
        return solve_state.load_state(paths.forum)
    finally:
        await asyncio.gather(*(
            _cancel(agents[name], task, interrupts[name], "informal solving runtime ending",
                    paths.project_root)
            for name, task in list(tasks.items())
        ))
