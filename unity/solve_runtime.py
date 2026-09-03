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
import subprocess
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console

from . import artifacts, blueprint, library, solve_state, worktree
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


async def _cancel(agent, task: asyncio.Task, interrupt: asyncio.Event, reason: str) -> None:
    if task.done():
        return
    _console.print(f"[yellow]interrupting {agent.name}: {reason}[/yellow]")
    if agent.backend == "codex":
        interrupt.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=50)
            return
        except asyncio.TimeoutError:
            pass
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _draft_path(paths, agent_name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", agent_name)
    path = paths.unity / "source" / "drafts" / safe / "PROOF.tex"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def reset_solve_workspace(paths) -> None:
    """Clear only solve-owned transient documents on a fresh solve run."""
    drafts = paths.unity / "source" / "drafts"
    if drafts.exists():
        import shutil
        shutil.rmtree(drafts)
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
        task_prompt = followup or (task_context +
            "Collaboratively solve the original problem in natural language. Coordinate through "
            "the solve Forum, but develop your own exact candidate paper at "
            f"`{draft.relative_to(paths.project_root)}`. You may investigate privately before a "
            "direction is coherent; then refresh the brief, register and claim a distinct strategy, "
            "publish useful findings early, and ask for help on concrete blockers. Submit the exact "
            "draft immediately once it is a complete rigorous solution. Continue useful work until "
            "the candidate interrupt or genuine quiescence."
        )
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
                "the concrete defect. Then refresh solve_brief and continue useful work.",
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
        preferred = [task for task in ready if task["task_id"] not in claimed_targets] or ready
        for index, name in enumerate(idle):
            launch(name, target_task=preferred[index % len(preferred)]["task_id"])

    launch_idle_work()
    for agent in roster.agents:
        if agent.name not in tasks:
            launch(agent.name)

    try:
        while not stop_requested(paths.project_root):
            await asyncio.sleep(0.5)
            state = solve_state.load_state(paths.forum)
            if state.get("phase") != "solving":
                await asyncio.gather(*(
                    _cancel(agents[name], task, interrupts[name], "solution candidate submitted")
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
                workers_finished = True
                interrupts.pop(name, None)
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
                                f"informal component {event['result_id']} submitted for review")
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
                                f"informal task {target} resolved")
                        for name in affected
                    ))
                    for name in affected:
                        tasks.pop(name, None)
                        interrupts.pop(name, None)
                        worker_targets.pop(name, None)
                    launch_idle_work()
                elif kind == "informal_result_objected":
                    pending_reviews.discard(event["result_id"])
                    result = state.get("informal_results", {}).get(event["result_id"], {})
                    author = result.get("author")
                    if author in agents:
                        if author in tasks:
                            await _cancel(agents[author], tasks[author], interrupts[author],
                                          f"informal result {event['result_id']} objected")
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
                                f"solution candidate {event['candidate_id']} submitted")
                        for name, task in list(tasks.items())
                    ))
                    return solve_state.load_state(paths.forum)

            if not tasks:
                return state
        return solve_state.load_state(paths.forum)
    finally:
        await asyncio.gather(*(
            _cancel(agents[name], task, interrupts[name], "informal solving runtime ending")
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
    except (OSError, json.JSONDecodeError):
        plan = None
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
    graph = {}
    covered_refs: set[str] = set()
    for chunk in chunks:
        task_id = str(chunk["id"])
        if not str(chunk.get("lean_decl") or "").strip():
            raise ValueError(f"chunk {task_id} must name its expected lean_decl")
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


def _kernel_target(kernel: dict, expected: str) -> tuple[str, dict] | None:
    if expected in kernel:
        return expected, kernel[expected]
    tail = expected.rsplit(".", 1)[-1]
    matches = [(name, row) for name, row in kernel.items()
               if name == tail or name.endswith("." + tail)]
    return matches[0] if len(matches) == 1 else None


def _unsafe_dependencies(kernel: dict, target: str) -> list[str]:
    unsafe: list[str] = []
    seen: set[str] = set()
    pending = list(kernel.get(target, {}).get("deps", []))
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        row = kernel.get(name)
        if not row:
            continue
        if row.get("sorried") or row.get("kind") == "axiom":
            unsafe.append(name)
        pending.extend(row.get("deps", []))
    return sorted(unsafe)


def _review_new_declaration(project_root: Path, task: dict, diff: str) -> dict:
    forbidden = sorted({
        match.group(1)
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
        for match in _FORBIDDEN_RE.finditer(line[1:].split("--", 1)[0])
    })
    issues = []
    if forbidden:
        issues.append("candidate adds forbidden construct(s): " + ", ".join(forbidden))
    kernel = blueprint.kernel_extract(project_root)
    expected = task.get("lean_decl", "")
    target = _kernel_target(kernel, expected) if kernel else None
    if kernel is None:
        matches = []
        for file, rows, _ in blueprint.scan_blueprint(project_root):
            for row in rows:
                if row["name"] == expected.rsplit(".", 1)[-1]:
                    matches.append((file, row))
        if len(matches) != 1:
            issues.append(f"expected declaration {expected} was not found uniquely")
        elif matches[0][1].get("status") in {"sorry", "axiom"}:
            issues.append(f"expected declaration {expected} remains unresolved")
    elif target is None:
        issues.append(f"expected declaration {expected} was not found in the built kernel")
    else:
        name, row = target
        if row.get("kind") == "axiom":
            issues.append(f"expected declaration {name} remains an axiom")
        if row.get("sorried"):
            issues.append(f"expected declaration {name} still uses sorryAx")
        unsafe = _unsafe_dependencies(kernel, name)
        if unsafe:
            issues.append("target depends on unresolved project declarations: " + ", ".join(unsafe))
    return {
        "status": "passed" if not issues else "failed",
        "expected_decl": expected,
        "source_components": list(task.get("source_components", [])),
        "mode": "kernel" if kernel is not None else "source_fallback",
        "forbidden_constructs": forbidden,
        "issues": issues,
    }


def _integrate_formal_candidate(paths, candidate: dict, task: dict) -> dict:
    """Apply, build, review, and commit one immutable formalization candidate."""
    root = paths.project_root
    with _merge_lock(root):
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
        try:
            build = subprocess.run(
                ["lake", "build"], cwd=root, capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            _git(root, "reset", "--hard", before)
            return {"ok": False, "error": f"could not run lake build: {exc}"}
        output = "\n".join(part.rstrip() for part in (build.stdout, build.stderr) if part)
        build_record = {"returncode": build.returncode}
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
        verification = _review_new_declaration(root, task, staged)
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
        return {
            "ok": True,
            "main_sha": worktree.main_commit(root),
            "build": build_record,
            "verification": verification,
        }


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
        task_prompt = followup or (
            f"Your current formalization target is task `{task_id}`: "
            f"{formal_task.get('description', '')}. The required Lean declaration is "
            f"`{formal_task.get('lean_decl')}`. Its accepted-paper source references are "
            f"{formal_task.get('source_components', [])}. Dependencies have already been integrated. "
            "Refresh solve_brief, register and claim a distinct implementation strategy for this "
            "task, edit in your worktree, run lake build, commit the complete change, and submit "
            "the exact commit with emit_formalization_candidate. Publish useful Lean/API findings "
            "as you work. If the accepted paper is wrong, propose corrected paper bytes or explicitly "
            "reopen solving rather than silently formalizing a different result."
        )
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
            ),
            name=f"solve:formalizing:{name}:{task_id}",
        )

    def launch_idle() -> None:
        current = solve_state.load_state(paths.forum)
        ready = solve_state.ready_formal_tasks(current)
        if not ready:
            return
        idle = [name for name in agents if name not in tasks]
        for index, name in enumerate(idle):
            launch(name, ready[index % len(ready)]["task_id"])

    launch_idle()
    try:
        while not stop_requested(paths.project_root):
            await asyncio.sleep(0.5)
            state = solve_state.load_state(paths.forum)
            if state.get("phase") != "formalizing":
                await asyncio.gather(*(
                    _cancel(agents[name], task, interrupts[name], "formalization phase changed")
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
                solve_state.release_author_claims(
                    paths.forum, name, "formalizer turn ended without an accepted candidate"
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
                            f"formal candidate {candidate_id} submitted for {task_id}")
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
                    _integrate_formal_candidate,
                    paths,
                    started["candidate"],
                    current["formal_tasks"][task_id],
                )
                solve_state.finish_formal_merge(
                    paths.forum, candidate_id,
                    success=bool(result.get("ok")),
                    main_sha=result.get("main_sha", ""),
                    error=result.get("error", ""),
                    build=result.get("build"),
                    verification=result.get("verification"),
                )
                if result.get("ok"):
                    for name, running in list(tasks.items()):
                        await _cancel(agents[name], running, interrupts[name],
                                      f"task {task_id} merged; synchronizing main")
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
            _cancel(agents[name], task, interrupts[name], "formalization runtime ending")
            for name, task in list(tasks.items())
        ))
        for agent in roster.agents:
            solve_state.release_author_claims(
                paths.forum, agent.name, "formalization runtime ended"
            )
            tree = worktrees.get(agent.name)
            if tree is not None:
                worktree.cleanup_worktree(agent.name, tree, paths.project_root)
