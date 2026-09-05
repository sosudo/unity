"""Event-driven worker loops for ``unity solve``.

This follows :mod:`unity.prove_runtime`: the command owns phase order, while a
runtime launches cancellable workers, consumes authoritative Forum events, and
returns when its current phase becomes quiescent or advances.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console

from . import artifacts, library, solve_contract, solve_jobs, solve_state, worktree
from .forum import solve_server
from .orchestrator import _preamble, load_prompt, stop_requested
from .spawn import spawn


_console = Console()
_FORBIDDEN_RE = re.compile(r"\b(sorry|admit|axiom|native_decide)\b")


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


def _agent_runtime_env(
    paths, state: dict, agent_name: str, *, task_id: str = "",
) -> dict[str, str]:
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
    if state.get("phase") == "formalizing":
        real_lake = shutil.which("lake")
        if real_lake:
            bin_dir = paths.unity / "bin" / "solve"
            bin_dir.mkdir(parents=True, exist_ok=True)
            wrapper = bin_dir / "lake"
            wrapper_source = (
                f"#!{sys.executable}\n"
                "from unity.solve_lake_guard import main\n"
                "raise SystemExit(main())\n"
            )
            if not wrapper.exists() or wrapper.read_text() != wrapper_source:
                temporary = wrapper.with_name(f".{wrapper.name}.{os.getpid()}.tmp")
                temporary.write_text(wrapper_source)
                temporary.chmod(0o700)
                os.replace(temporary, wrapper)
            result.update({
                "PATH": str(bin_dir.resolve()) + os.pathsep + os.environ.get("PATH", ""),
                "UNITY_REAL_LAKE": str(Path(real_lake).resolve()),
                "UNITY_SOLVE_PROJECT_ROOT": str(paths.project_root.resolve()),
                "UNITY_SOLVE_TASK_ID": task_id,
            })
    return result


def _formal_task_assignments(
    ready: list[dict],
    idle_workers: list[str],
    active_targets: list[str],
) -> list[tuple[str, str]]:
    """Cover independent ready tasks before assigning redundant formalizers."""
    if not ready:
        return []
    load = {formal_task["task_id"]: 0 for formal_task in ready}
    for target in active_targets:
        if target in load:
            load[target] += 1
    order = {
        formal_task["task_id"]: index
        for index, formal_task in enumerate(ready)
    }
    assignments = []
    for name in idle_workers:
        task_id = min(load, key=lambda target: (load[target], order[target]))
        assignments.append((name, task_id))
        load[task_id] += 1
    return assignments


def reset_solve_workspace(paths) -> None:
    """Clear only solve-owned transient documents on a fresh solve run."""
    drafts = paths.unity / "source" / "drafts"
    if drafts.exists():
        import shutil
        shutil.rmtree(drafts)
    scratch = paths.unity / "tmp"
    if scratch.exists():
        import shutil
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
    tasks: dict[str, asyncio.Task] = {}
    interrupts: dict[str, asyncio.Event] = {}
    worker_targets: dict[str, str] = {}
    reviewer_results: dict[str, str] = {}
    submission_nudges: set[str] = set()
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
            reviewer = next((name for name in idle if name != result.get("author")), None)
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
        preferred = [task for task in ready if task["task_id"] not in claimed_targets]
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
            if workers_finished and pending_reviews:
                launch_idle_work(include_tasks=False)

            for event in solve_state.events_after(state, seen):
                seen.add(event["event_id"])
                kind = event.get("kind")
                if kind in {"strategy_registered", "strategy_claimed", "strategy_assisted"}:
                    if event.get("phase") == "solving" and event.get("author") in agents:
                        worker_targets[event["author"]] = event.get("target", "")
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
                elif kind == "informal_task_superseded":
                    target = event.get("task_id", "")
                    affected = [
                        name for name, running in tasks.items()
                        if not running.done() and worker_targets.get(name) == target
                    ]
                    await asyncio.gather(*(
                        _cancel(
                            agents[name], tasks[name], interrupts[name],
                            f"informal task {target} superseded",
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
                    author = result.get("author")
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
                    recipient = event.get("to")
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
                        if name != event.get("author") and name not in tasks
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

            if not tasks:
                return state
        return solve_state.load_state(paths.forum)
    finally:
        await asyncio.gather(*(
            _cancel(agents[name], task, interrupts[name], "informal solving runtime ending",
                    paths.project_root)
            for name, task in list(tasks.items())
        ))


def write_formalization_plan(paths, candidate: dict) -> Path:
    """Mechanically scaffold the source identities the semantic DAG must cover."""
    components = list(candidate.get("components", []))
    source_refs = [{
        "ref_id": f"paper:{candidate['candidate_id']}",
        "kind": "complete_paper",
        "summary": "The complete accepted solution paper",
        "artifact_id": candidate["artifact_id"],
        "sha256": candidate["sha256"],
    }] + [
        {
            "ref_id": item["result_id"],
            "kind": item.get("kind", "component"),
            "summary": item.get("summary", ""),
            "artifact_id": item["artifact_id"],
            "sha256": item["sha256"],
        }
        for item in components
    ]
    plan = {
        "solution_candidate": candidate["candidate_id"],
        "solution_sha256": candidate["sha256"],
        "source_refs": source_refs,
    }
    path = paths.unity / "formalization-plan.json"
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return path


def validate_formalization_dag(paths, expected_solution_sha: str) -> dict:
    """Validate the semantic chunker's DAG and its binding to accepted paper bytes."""
    dag_path = paths.unity / "dag.json"
    try:
        dag = json.loads(dag_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("chunking did not produce a readable .unity/dag.json") from exc
    recorded = str(dag.get("solution_sha256") or dag.get("source_sha256") or "")
    if recorded != expected_solution_sha:
        raise ValueError("formalization DAG is not bound to the accepted PROOF.tex SHA-256")
    chunks = dag.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("formalization DAG requires a nonempty chunks list")
    ids = [str(item.get("id") or "").strip() for item in chunks]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("formalization chunks require unique nonempty ids")
    try:
        plan = json.loads((paths.unity / "formalization-plan.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("chunking requires a readable formalization-plan.json") from exc
    required_refs: set[str] = set()
    if plan is not None:
        if plan.get("solution_sha256") != expected_solution_sha:
            raise ValueError("formalization plan is not bound to the accepted paper SHA-256")
        if dag.get("solution_candidate") != plan.get("solution_candidate"):
            raise ValueError("formalization DAG names the wrong solution candidate")
        required_refs = {str(item.get("ref_id")) for item in plan.get("source_refs", [])}
        if not required_refs or "None" in required_refs:
            raise ValueError("formalization plan has invalid source references")
    known = set(ids)
    declarations = [str(item.get("lean_decl") or "").strip() for item in chunks]
    if any(not name for name in declarations) or len(set(declarations)) != len(declarations):
        raise ValueError("formalization declarations must be unique and nonempty")
    graph = {}
    covered_refs: set[str] = set()
    for chunk in chunks:
        task_id = str(chunk["id"])
        if not str(chunk.get("lean_decl") or "").strip():
            raise ValueError(f"chunk {task_id} must name its expected lean_decl")
        if not isinstance(chunk.get("lean_file"), str) or not chunk["lean_file"].strip():
            raise ValueError(f"chunk {task_id} must name its Lean scaffold file")
        deps = [str(item) for item in chunk.get("dependencies", [])]
        unknown = set(deps) - known
        if unknown:
            raise ValueError(f"chunk {task_id} has unknown dependencies: {sorted(unknown)}")
        source_refs = [str(item) for item in chunk.get("source_components", [])]
        if required_refs:
            unknown_refs = set(source_refs) - required_refs
            if unknown_refs:
                raise ValueError(f"chunk {task_id} has unknown source components: {sorted(unknown_refs)}")
            if not source_refs:
                raise ValueError(f"chunk {task_id} must name at least one source component")
            covered_refs.update(source_refs)
        graph[task_id] = set(deps)
    missing_refs = required_refs - covered_refs
    if missing_refs:
        raise ValueError("formalization DAG does not cover source components: " + ", ".join(sorted(missing_refs)))
    requirements = dag.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("formalization DAG requires explicit mathematical requirements")
    requirement_ids = set()
    requirement_refs = set()
    by_id = {chunk["id"]: chunk for chunk in chunks}
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise ValueError("each mathematical requirement must be an object")
        rid = requirement.get("id")
        if not isinstance(rid, str) or not rid.strip() or rid in requirement_ids:
            raise ValueError("mathematical requirement IDs must be unique and nonempty")
        requirement_ids.add(rid)
        if not isinstance(requirement.get("statement"), str) or not requirement["statement"].strip():
            raise ValueError(f"requirement {rid} needs a precise statement")
        refs, tasks = requirement.get("source_components"), requirement.get("tasks")
        if not isinstance(refs, list) or not refs or not set(refs) <= required_refs:
            raise ValueError(f"requirement {rid} has missing/unknown source references")
        if not isinstance(tasks, list) or not tasks or not set(tasks) <= known:
            raise ValueError(f"requirement {rid} has missing/unknown tasks")
        mapped_refs = {ref for task in tasks for ref in by_id[task]["source_components"]}
        if not set(refs) <= mapped_refs:
            raise ValueError(f"requirement {rid} references sources not covered by its tasks")
        requirement_refs.update(refs)
    if required_refs - requirement_refs:
        raise ValueError("mathematical requirements do not cover all accepted source components")
    pending = dict(graph)
    while pending:
        ready = [node for node, deps in pending.items() if not (deps & pending.keys())]
        if not ready:
            raise ValueError("formalization DAG contains a dependency cycle")
        for node in ready:
            pending.pop(node)
    return dag


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True, check=False,
    )


@contextmanager
def _merge_lock(project_root: Path):
    path = project_root / ".unity" / "forum" / "merge.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _review_new_declaration(project_root: Path, task: dict, diff: str, *,
                            contract: dict | None = None,
                            formal_tasks: list[dict] | None = None) -> dict:
    forbidden = sorted({
        match.group(1)
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
        for match in _FORBIDDEN_RE.finditer(line[1:].split("--", 1)[0])
    })
    issues = []
    if forbidden:
        issues.append("candidate adds forbidden construct(s): " + ", ".join(forbidden))
    expected = task.get("lean_decl", "")
    tasks = formal_tasks or [task]
    completed = {item["task_id"] for item in tasks
                 if item.get("status") == "complete" or item["task_id"] == task["task_id"]}
    try:
        check = solve_contract.check_formal_contract(project_root, contract or {}, tasks,
                                                     completed=completed)
    except (OSError, ValueError) as exc:
        check = {"passed": False, "issues": [f"formal contract verification unavailable: {exc}"]}
    issues.extend(check["issues"])
    return {
        "status": "passed" if not issues else "failed",
        "expected_decl": expected,
        "source_components": list(task.get("source_components", [])),
        "mode": "formal_contract",
        "contract_sha256": (contract or {}).get("sha256"),
        "verified_tasks": sorted(completed),
        "forbidden_constructs": forbidden,
        "issues": issues,
    }


def _apply_formal_candidate(paths, candidate: dict, task: dict) -> dict:
    """Apply, build, review, and commit one immutable formalization candidate."""
    root = paths.project_root
    current = solve_state.load_state(paths.forum)
    contract = current["formalization"].get("contract", {})
    if not contract:
        return {"ok": False, "error": "missing formal contract; request re-chunking before proving"}
    if candidate.get("formalization_revision") != current["formalization"].get("revision"):
        return {"ok": False, "error": "candidate belongs to a superseded formal contract"}
    try:
        resolved = worktree.verify_candidate_commit(root, candidate["author"], candidate["commit_sha"])
    except Exception as exc:
        return {"ok": False, "error": f"candidate identity failed: {exc}"}
    exact_diff = _git(root, "show", "--format=", "--binary", resolved).stdout
    if hashlib.sha256(exact_diff.encode()).hexdigest() != candidate["diff_sha256"]:
        return {"ok": False, "error": "candidate commit no longer matches its submitted diff hash"}
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        return {"ok": False, "error": "main has tracked changes; refusing candidate merge"}
    before = worktree.main_commit(root)
    applied = _git(root, "cherry-pick", "--no-commit", resolved)
    if applied.returncode:
        _git(root, "reset", "--hard", before)
        return {"ok": False, "error": applied.stderr.strip() or "candidate conflicts with main"}
    checked_source = solve_contract.source_identity(root)
    checked_tree = _git(root, "write-tree").stdout.strip()
    build_started = time.monotonic()
    try:
        build = solve_jobs.run(
            root,
            ["lake", "build"],
            cwd=root,
            owner="Unity",
            task_id=task["task_id"],
            serialize_build=True,
        )
    except OSError as exc:
        build_seconds = time.monotonic() - build_started
        _git(root, "reset", "--hard", before)
        return {
            "ok": False, "error": f"could not run lake build: {exc}",
            "build": {"returncode": None, "seconds": build_seconds},
        }
    build_seconds = time.monotonic() - build_started
    # Default Lake targets need not include the task's module. Explicitly
    # build all inspected source modules before loading their .olean files.
    if not build.returncode:
        modules_build = solve_contract.build_sources(root)
        if modules_build["returncode"]:
            _git(root, "reset", "--hard", before)
            return {"ok": False, "error": "target module build failed: " +
                    artifacts.preview_text(modules_build["output"], 3000)}
    output = "\n".join(part.rstrip() for part in (build.stdout, build.stderr) if part)
    build_record = {"returncode": build.returncode, "seconds": build_seconds}
    if output:
        record = artifacts.store_text(
            paths.artifacts, output, kind="solve_formal_build",
            source="lake build", producer="Unity",
            metadata={"candidate_id": candidate["candidate_id"], "task_id": task["task_id"]},
        )
        build_record.update({"artifact_id": record["artifact_id"], "sha256": record["sha256"]})
    if build.returncode:
        _git(root, "reset", "--hard", before)
        return {
            "ok": False,
            "error": "lake build failed: " + artifacts.preview_text(output, 3000),
            "build": build_record,
        }
    if _git(root, "diff", "--quiet").returncode:
        _git(root, "reset", "--hard", before)
        return {"ok": False, "error": "lake build changed tracked files", "build": build_record}
    staged = _git(root, "diff", "--cached", "--no-ext-diff", before).stdout
    verification_started = time.monotonic()
    verification = _review_new_declaration(
        root, task, staged, contract=contract,
        formal_tasks=list(current["formal_tasks"].values()),
    )
    if (solve_contract.source_identity(root) != checked_source
            or _git(root, "write-tree").stdout.strip() != checked_tree):
        raise ValueError("source changed during candidate build or kernel inspection")
    verification["seconds"] = time.monotonic() - verification_started
    record = artifacts.store_text(
        paths.artifacts, json.dumps(verification, indent=2, sort_keys=True) + "\n",
        kind="solve_formal_verification", producer="Unity",
        source=f"formal task {task['task_id']}",
    )
    verification["artifact_id"] = record["artifact_id"]
    if verification["status"] != "passed":
        _git(root, "reset", "--hard", before)
        return {
            "ok": False,
            "error": "; ".join(verification["issues"]),
            "build": build_record,
            "verification": verification,
        }
    commit = _git(root, "commit", "-m", f"UNITY: merge solve task {task['task_id']}")
    if commit.returncode:
        _git(root, "reset", "--hard", before)
        return {"ok": False, "error": commit.stderr.strip() or "could not commit candidate"}
    committed_source = solve_contract.source_identity(root)
    if (committed_source != {**checked_source, "main_sha": committed_source["main_sha"]}
            or _git(root, "rev-parse", "HEAD^{tree}").stdout.strip() != checked_tree):
        raise ValueError("commit changed the verified candidate source")
    verification["source_identity"] = committed_source
    return {
        "ok": True,
        "main_sha": worktree.main_commit(root),
        "build": build_record,
        "verification": verification,
    }


def _integrate_checked(paths, candidate: dict, task: dict) -> dict:
    root = paths.project_root
    # Nothing below may discard pre-existing tracked edits.
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        return {"ok": False, "error": "main has tracked changes; refusing candidate merge"}
    before = worktree.main_commit(root)
    try:
        return _apply_formal_candidate(paths, candidate, task)
    except (OSError, ValueError, KeyError) as exc:
        restored = _git(root, "reset", "--hard", before)
        suffix = "" if restored.returncode == 0 else "; main rollback also failed: " + restored.stderr
        return {"ok": False, "error": f"candidate verification failed: {exc}{suffix}"}


def _integrate_formal_candidate(paths, candidate: dict, task: dict) -> dict:
    """Apply one candidate under the merge lock (also useful for integration tests)."""
    with _merge_lock(paths.project_root):
        return _integrate_checked(paths, candidate, task)


def _integrate_and_record(paths, candidate: dict, task: dict) -> dict:
    """Serialize Git integration AND state publication under the same lock."""
    with _merge_lock(paths.project_root):
        result = _integrate_checked(paths, candidate, task)
        solve_state.finish_formal_merge(
            paths.forum, candidate["candidate_id"], success=bool(result.get("ok")),
            main_sha=result.get("main_sha", ""), error=result.get("error", ""),
            build=result.get("build"), verification=result.get("verification"),
        )
        return result


async def run_formalizing_runtime(roster, paths, mcp: dict, base_prompt: str) -> dict:
    """Swarm ready formal tasks and integrate candidates using prove-style events."""
    if stop_requested(paths.project_root):
        return solve_state.load_state(paths.forum)
    configure_forum(paths, "formalizing")
    tools_prompt = load_prompt("SOLVE_FORMALIZING_TOOLS")
    context = library.library_context()
    subagents = library.library_subagents()
    agents = {agent.name: agent for agent in roster.agents}
    worktrees: dict[str, Path] = {}
    tasks: dict[str, asyncio.Task] = {}
    interrupts: dict[str, asyncio.Event] = {}
    worker_targets: dict[str, str] = {}
    submission_nudges: set[tuple[str, str, str]] = set()
    state = solve_state.load_state(paths.forum)
    seen = {
        event["event_id"] for event in state.get("events", [])
        if not (
            event.get("kind") == "formal_candidate_submitted"
            and state.get("formal_candidates", {}).get(
                event.get("candidate_id"), {}
            ).get("status") == "submitted"
        )
    }

    for agent in roster.agents:
        tree = worktree.create_worktree(agent.name, paths.project_root)
        worktree.symlink_lake_cache(tree, paths.project_root)
        worktree.link_runtime_state(tree, paths.project_root)
        worktrees[agent.name] = tree

    def owned_strategy(current: dict, name: str, task_id: str = "") -> dict | None:
        return next((
            strategy for strategy in current.get("strategies", {}).values()
            if strategy.get("phase") == "formalizing"
            and strategy.get("status") == "claimed"
            and strategy.get("owner") == name
            and (not task_id or strategy.get("target") == task_id)
        ), None)

    def worktree_changes(name: str) -> tuple[str, str]:
        status = _git(
            worktrees[name], "status", "--porcelain", "--untracked-files=all"
        ).stdout.strip()
        diff = _git(worktrees[name], "diff", "HEAD", "--binary").stdout
        return status, hashlib.sha256((status + "\n" + diff).encode()).hexdigest()

    def launch(name: str, task_id: str, followup: str = "") -> None:
        if name in tasks and not tasks[name].done():
            return
        current = solve_state.load_state(paths.forum)
        formal_task = current["formal_tasks"].get(task_id)
        if not formal_task or formal_task.get("status") != "pending":
            return
        worker_targets[name] = task_id
        agent = agents[name]
        brief = forum_brief(paths, "formalizing", name)
        system = _preamble(agent, roster, icrl_enabled=False)
        if brief:
            system += f"\nSolve workspace brief (refresh with solve_brief):\n{brief}\n"
        system += base_prompt + "\n\n" + tools_prompt
        if context:
            system += "\n\n" + context
        strategy = owned_strategy(current, name, task_id)
        dirty, _ = worktree_changes(name)
        resume = ""
        if strategy:
            resume += (
                f"Resume your currently claimed strategy `{strategy['strategy_id']}`. Do not "
                "register or claim a replacement unless you explicitly abandon this strategy. "
            )
        if dirty:
            resume += (
                "Your worktree already has source changes. Inspect the current diff before any new "
                "search or edit. If the target is complete, call `finalize_formalization` immediately. "
            )
        strategy_instruction = (
            "Continue the claimed strategy for this task. "
            if strategy else
            "Claim a suitable existing unclaimed strategy, or register one only when your approach "
            "is materially different. You may investigate or edit before registering, but claim a "
            "strategy before finalizing. "
        )
        task_prompt = resume + (followup or (
            f"Your current formalization target is task `{task_id}`: "
            f"{formal_task.get('description', '')}. The required Lean declaration is "
            f"`{formal_task.get('lean_decl')}`. Its accepted-paper source references are "
            f"{formal_task.get('source_components', [])}. Dependencies have already been integrated. "
            "Refresh solve_brief. " + strategy_instruction +
            "Edit in your worktree using Lean diagnostics and targeted checks while iterating. "
            "When the implementation is ready, call `finalize_formalization`; Unity will commit the "
            "exact source and perform the sole authoritative full build in main. Publish useful Lean/API findings "
            "as you work. If the accepted paper is wrong, propose corrected paper bytes or explicitly "
            "reopen solving rather than silently formalizing a different result."
        ))
        event = asyncio.Event()
        interrupts[name] = event
        tasks[name] = asyncio.create_task(
            spawn(
                agent, system, task_prompt, worktrees[name], mcp,
                subagents=subagents, interrupt_event=event,
                log_context={
                    "command": "solve", "run_id": current.get("run_id"), "phase": "formalizing",
                    "task_id": task_id, "role": "formalizer",
                },
                env_overrides=_agent_runtime_env(paths, current, name, task_id=task_id),
                own_process_group=True,
                mcp_profile="solve",
            ),
            name=f"solve:formalizing:{name}:{task_id}",
        )

    def launch_idle() -> None:
        current = solve_state.load_state(paths.forum)
        ready = solve_state.ready_formal_tasks(current)
        if not ready:
            return
        idle = [name for name in agents if name not in tasks]
        ready_ids = {formal_task["task_id"] for formal_task in ready}
        unassigned = []
        for name in idle:
            strategy = owned_strategy(current, name)
            if strategy and strategy.get("target") in ready_ids:
                launch(name, strategy["target"])
            else:
                unassigned.append(name)
        active_targets = [
            worker_targets.get(name, "")
            for name, running in tasks.items()
            if not running.done()
        ]
        for name, task_id in _formal_task_assignments(ready, unassigned, active_targets):
            launch(name, task_id)

    launch_idle()
    try:
        while not stop_requested(paths.project_root):
            await asyncio.sleep(0.5)
            state = solve_state.load_state(paths.forum)
            if state.get("phase") != "formalizing":
                await asyncio.gather(*(
                    _cancel(agents[name], task, interrupts[name], "formalization phase changed",
                            paths.project_root)
                    for name, task in list(tasks.items())
                ))
                return state

            for name, task in list(tasks.items()):
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _console.print(f"[red]formalizer {name} failed: {exc!r}[/red]")
                tasks.pop(name, None)
                interrupts.pop(name, None)
                current = solve_state.load_state(paths.forum)
                task_id = worker_targets.get(name, "")
                formal_task = current.get("formal_tasks", {}).get(task_id, {})
                dirty, digest = worktree_changes(name)
                strategy = owned_strategy(current, name, task_id)
                nudge_key = (name, task_id, digest)
                if (
                    formal_task.get("status") == "pending"
                    and dirty
                    and strategy
                    and nudge_key not in submission_nudges
                ):
                    submission_nudges.add(nudge_key)
                    launch(
                        name,
                        task_id,
                        "Submission check only: inspect the existing worktree diff before doing "
                        "new research. Your first substantive action must be either calling "
                        "`finalize_formalization` if it completes the target, or publishing one "
                        "precise blocker and continuing the currently claimed strategy. Do not "
                        "register a new strategy or repeat unchanged searches in this turn.",
                    )

            state = solve_state.load_state(paths.forum)
            events = solve_state.events_after(state, seen)
            for event in events:
                seen.add(event["event_id"])
                kind = event.get("kind")
                if kind in {"strategy_registered", "strategy_claimed", "strategy_assisted"}:
                    if event.get("phase") == "formalizing" and event.get("author") in agents:
                        worker_targets[event["author"]] = event.get("target", "")
                if kind != "formal_candidate_submitted":
                    continue
                candidate_id = event["candidate_id"]
                current = solve_state.load_state(paths.forum)
                candidate = current["formal_candidates"].get(candidate_id, {})
                if candidate.get("status") != "submitted":
                    continue
                task_id = candidate["task_id"]
                affected = [
                    name for name, task in tasks.items()
                    if not task.done() and (worker_targets.get(name) == task_id or name == candidate["author"])
                ]
                await asyncio.gather(*(
                    _cancel(agents[name], tasks[name], interrupts[name],
                            f"formal candidate {candidate_id} submitted for {task_id}",
                            paths.project_root)
                    for name in affected
                ))
                for name in affected:
                    tasks.pop(name, None)
                    interrupts.pop(name, None)
                started = solve_state.begin_formal_merge(paths.forum, candidate_id)
                if started.get("idempotent") or started.get("conflict"):
                    continue
                _console.print(f"[cyan]mechanically reviewing {candidate_id} for {task_id}[/cyan]")
                result = await asyncio.to_thread(
                    _integrate_and_record,
                    paths,
                    started["candidate"],
                    current["formal_tasks"][task_id],
                )
                if result.get("ok"):
                    for name, running in list(tasks.items()):
                        await _cancel(agents[name], running, interrupts[name],
                                      f"task {task_id} merged; synchronizing main",
                                      paths.project_root)
                    tasks.clear()
                    interrupts.clear()
                    worker_targets.clear()
                    for name in agents:
                        solve_state.release_author_claims(
                            paths.forum, name, f"formal task {task_id} merged"
                        )
                        worktree.force_sync_from_main(paths.project_root, name)
                else:
                    _console.print(f"[red]candidate {candidate_id} failed: {result.get('error', '')}[/red]")

            state = solve_state.load_state(paths.forum)
            if solve_state.all_formal_tasks_complete(state):
                return state
            launch_idle()
            if not tasks:
                return state
        return solve_state.load_state(paths.forum)
    finally:
        await asyncio.gather(*(
            _cancel(agents[name], task, interrupts[name], "formalization runtime ending",
                    paths.project_root)
            for name, task in list(tasks.items())
        ))
        await asyncio.to_thread(solve_jobs.terminate, paths.project_root)
        for agent in roster.agents:
            solve_state.release_author_claims(
                paths.forum, agent.name, "formalization runtime ended"
            )
            tree = worktrees.get(agent.name)
            if tree is not None:
                worktree.cleanup_worktree(agent.name, tree, paths.project_root)
