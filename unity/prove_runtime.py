"""Event-driven proving runtime built around authoritative Forum state."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from rich.console import Console

from . import library, prove_state, worktree
from .prove_questions import QuestionWakeups
from .forum import server as forum_server
from .orchestrator import _preamble, load_prompt, stop_requested
from .spawn import spawn


_console = Console()


def _prove_codex_reasoning_effort(agent_name: str, value: str | None) -> str | None:
    """Resolve a Prove effort setting from a scalar or an agent-name JSON map."""
    if value is None or not value.strip():
        return None
    from openai_codex.types import ReasoningEffort

    allowed = {effort.value for effort in ReasoningEffort}
    value = value.strip()
    if value.startswith("{"):
        try:
            mapping = json.loads(value)
        except ValueError as exc:
            raise ValueError("UNITY_PROVE_CODEX_REASONING_EFFORT must be an effort or JSON map") from exc
        if not isinstance(mapping, dict) or any(
            not isinstance(name, str) or not name or not isinstance(effort, str)
            or effort not in allowed for name, effort in mapping.items()
        ):
            raise ValueError("UNITY_PROVE_CODEX_REASONING_EFFORT contains an invalid agent or effort")
        return mapping.get(agent_name, mapping.get("*"))
    if value not in allowed:
        raise ValueError("UNITY_PROVE_CODEX_REASONING_EFFORT contains an invalid effort")
    return value


def _prove_worktree(root: Path, unity_dir: Path, name: str, *, fresh: bool) -> Path:
    """Create or safely reuse a Prove-owned worker worktree without deleting work."""
    root = Path(root).resolve()
    path = worktree.agent_worktree(root, name)
    branch = worktree.agent_branch(name)
    marker = Path(unity_dir) / "prove-workspaces" / (worktree._safe(name) + ".json")
    if marker.exists():
        if fresh:
            raise RuntimeError(f"existing Prove workspace for {name} requires recovery: {path}")
        try:
            record = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid Prove workspace record: {marker}") from exc
        expected = {"name": name, "root": str(root), "path": str(path), "branch": branch}
        if record != expected or path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"Prove workspace identity mismatch for {name}: {path}")
        listed = worktree._git(root, "worktree", "list", "--porcelain", check=True).stdout
        registered = any(
            f"worktree {path}\n" in block and f"branch refs/heads/{branch}\n" in block + "\n"
            for block in listed.split("\n\n")
        )
        current_branch = worktree._git(path, "symbolic-ref", "--short", "HEAD", check=True).stdout.strip()
        if not registered or current_branch != branch:
            raise RuntimeError(f"Prove workspace is not registered on its expected branch: {path}")
    else:
        # preserve_existing rejects both unowned directories and existing branch names.
        path = worktree.create_worktree(name, root, preserve_existing=True)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"name": name, "root": str(root), "path": str(path),
                                      "branch": branch}, indent=2) + "\n")
    worktree.symlink_lake_cache(path, root)
    worktree.link_runtime_state(path, root)
    return path


def _prove_workspace_context(root: Path, path: Path, name: str) -> str:
    worker_sha = worktree.main_commit(path)
    main_sha = worktree.main_commit(root)
    status = worktree._git(path, "status", "--porcelain", "--untracked-files=all", check=True).stdout
    return (
        f"Your preserved Prove workspace is {path} on branch {worktree.agent_branch(name)}. "
        f"Worker revision: {worker_sha}; main revision: {main_sha}.\n"
        f"Current workspace changes:\n{status[:4000] or '(none)'}\n"
        "Inspect your existing files, commits and Forum findings, and continue useful prior work. "
        "Preserve progress; if main has changed, reconcile deliberately instead of resetting your workspace.\n\n"
    )


def _target_context_limit() -> int:
    """Maximum launch-packet size; bounded independently of project size."""
    try:
        return max(2_000, int(os.getenv("UNITY_PROVE_TARGET_CONTEXT_CHARS", "12000")))
    except ValueError:
        return 12_000


def _bounded_text(path: Path, limit: int) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    if len(text) <= limit:
        return text.rstrip()
    return text[:limit].rstrip() + f"\n...[truncated at {limit} characters]"


def _target_context(paths, decl: str, state: dict | None = None) -> str:
    """Build a deterministic, bounded launch packet for one proof target."""
    state = state or prove_state.load_state(paths.forum)
    declaration = state.get("declarations", {}).get(decl, {})
    try:
        dag = json.loads((paths.unity / "dag.json").read_text())
    except (OSError, json.JSONDecodeError):
        dag = {}
    chunk = next((
        item for item in dag.get("chunks", [])
        if str(item.get("lean_decl") or item.get("id") or "") == decl
    ), {})

    lean_file = str(declaration.get("file") or chunk.get("lean_file") or "")
    source_path = paths.project_root / lean_file if lean_file else None
    source_lines: list[str] = []
    if source_path is not None:
        try:
            source_lines = source_path.read_text(errors="replace").splitlines()
        except OSError:
            pass

    imports = [line.strip() for line in source_lines if line.lstrip().startswith("import ")]
    imports_text = "\n".join(f"- {line}" for line in imports[:48]) or "- unavailable"
    if len(imports) > 48:
        imports_text += f"\n- ... {len(imports) - 48} more imports omitted"

    source_excerpt = ""
    line_range = chunk.get("lean_decl_lines")
    if source_lines and isinstance(line_range, list) and len(line_range) == 2:
        try:
            first, last = int(line_range[0]), int(line_range[1])
            start = max(1, first - 3)
            end = min(len(source_lines), last + 2)
            source_excerpt = "\n".join(
                f"{line_no:>5}: {source_lines[line_no - 1]}"
                for line_no in range(start, end + 1)
            )
        except (TypeError, ValueError):
            source_excerpt = ""
    if not source_excerpt:
        source_excerpt = str(declaration.get("statement") or chunk.get("statement") or "unavailable")

    type_dependencies = (
        declaration.get("type_dependencies")
        or chunk.get("type_dependencies")
        or []
    )
    dependency_text = "\n".join(f"- {name}" for name in type_dependencies[:48]) or "- unavailable"
    target_dependencies = declaration.get("dependencies") or chunk.get("dependencies") or []
    target_dependency_text = "\n".join(f"- {name}" for name in target_dependencies) or "- none"
    project_goal = _bounded_text(paths.unity_md, 2_500) or "unavailable"
    location = lean_file or "unknown file"
    if isinstance(line_range, list) and len(line_range) == 2:
        location += f":{line_range[0]}-{line_range[1]}"

    packet = (
        "TARGET CONTEXT (deterministic launch packet)\n"
        f"Declaration: {decl}\n"
        f"Kind: {declaration.get('declaration_kind') or chunk.get('type') or 'unknown'}\n"
        f"Location: {location}\n"
        f"Main revision: {state.get('main_sha', 'unknown')}\n\n"
        "Exact kernel type:\n"
        f"{declaration.get('kernel_type_repr') or chunk.get('kernel_type_repr') or 'unavailable'}\n\n"
        "Surface statement:\n"
        f"{declaration.get('statement') or chunk.get('statement') or 'unavailable'}\n\n"
        "Constants appearing in the target type:\n"
        f"{dependency_text}\n\n"
        "Unresolved target dependencies:\n"
        f"{target_dependency_text}\n\n"
        "Imports in the target file:\n"
        f"{imports_text}\n\n"
        "Target source excerpt:\n"
        f"{source_excerpt}\n\n"
        "Project prove goal/configuration:\n"
        f"{project_goal}"
    )
    limit = _target_context_limit()
    if len(packet) > limit:
        packet = packet[:limit].rstrip() + f"\n...[target context truncated at {limit} characters]"
    return packet


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
    effort_setting = os.getenv("UNITY_PROVE_CODEX_REASONING_EFFORT")
    efforts = {agent.name: _prove_codex_reasoning_effort(agent.name, effort_setting)
               for agent in roster.agents}
    forum_server.FORUM_DIR = paths.forum
    forum_server.PROJECT_ROOT = root

    worktrees = {}
    for agent in roster.agents:
        agent_tree = _prove_worktree(root, paths.unity, agent.name, fresh=reset_state)
        worktrees[agent.name] = agent_tree

    main_sha = worktree.main_commit(root)
    initial_state = prove_state.initialize_from_dag(
        paths.forum, paths.unity / "dag.json", main_sha, reset=reset_state
    )

    tools_ref = load_prompt("PROVE_TOOLS")
    context = library.library_context()
    shared_prompt = base_prompt + "\n\n" + tools_ref
    shared_prompt += (
        "\n\nEnabled MCP servers for this run: " + ", ".join(sorted(mcp)) + ". "
        "Use only these configured servers. Tool descriptions for an absent optional server "
        "do not enable that service. Continue with local proof work when an optional service is absent."
    )
    if context:
        shared_prompt += "\n\n" + context
    subagents = library.library_subagents()
    agents = {agent.name: agent for agent in roster.agents}
    tasks: dict[str, asyncio.Task] = {}
    interrupts: dict[str, asyncio.Event] = {}
    seen_events: set[str] = {
        event["event_id"] for event in initial_state.get("events", [])
    }
    pending_sync: set[str] = set()
    merge_followups: dict[str, tuple[str, str]] = {}
    worker_decls: dict[str, str] = {}
    question_wakeups = QuestionWakeups(paths.forum)

    normal_task = (
        "Coordinate proof work through the Forum. You may privately inspect the target and "
        "experiment first. On a later turn, inspect preserved files and findings, and reclaim a "
        "viable unowned strategy before extending it. Begin with a focused search for an existing project or Mathlib theorem "
        "or API before committing to a from-scratch derivation. Then refresh forum_brief and either "
        "register and claim a genuinely "
        "distinct direction, assist an existing strategy with a concrete contribution, or publish "
        "actionable findings while doing cross-cutting library/API search. Never invent a redundant "
        "strategy merely to appear active. "
        "Refresh before changing direction, after publishing a finding, and before editing the "
        "target. Publish a promising checked declaration immediately, before lengthy integration "
        "work, so another agent can assist with the conversion. Implement the claimed strategy in your "
        "worktree. Run lake build before committing "
        "and calling emit_candidate. Unity will mechanically build and review the exact declaration "
        "in main. If a candidate is merge-blocked, resolve the integration blocker and retry it; "
        "do not resume proof search. If a candidate is merged, call sync_from_main before claiming "
        "more work. Continue until no "
        "unresolved declaration has useful work for you. If the needed library theorem is absent, "
        "continue by stating and proving missing helper lemmas. Share checked partial progress and "
        "exact remaining goals; avoid ending a turn with only a repeated search blocker."
    )

    def launch(
        agent_name: str,
        task_prompt: str = normal_task,
        *,
        target_decl: str | None = None,
    ) -> None:
        existing = tasks.get(agent_name)
        if existing is not None and not existing.done():
            return
        task_prompt = _prove_workspace_context(root, worktrees[agent_name], agent_name) + task_prompt
        if target_decl:
            worker_decls[agent_name] = target_decl
        assigned_decl = worker_decls.get(agent_name)
        if assigned_decl:
            task_prompt = (
                _target_context(paths, assigned_decl)
                + "\n\n"
                + f"Your current target declaration is `{assigned_decl}`. Work on this declaration "
                "unless Forum state causes you to change direction. If you change declarations, "
                "register or claim a strategy for the new declaration.\n\n"
                + task_prompt
            )
        agent = agents[agent_name]
        system = (
            _preamble(agent, roster, icrl_enabled=False)
            + _workspace_brief(paths, agent_name)
            + shared_prompt
        )
        interrupt_event = asyncio.Event()
        interrupts[agent_name] = interrupt_event
        tasks[agent_name] = asyncio.create_task(
            spawn(
                agent,
                system,
                task_prompt,
                worktrees[agent_name],
                mcp,
                subagents=subagents,
                interrupt_event=interrupt_event,
                env_overrides=({"UNITY_PROVE_CODEX_REASONING_EFFORT": efforts[agent_name]}
                               if agent.backend == "codex" and efforts[agent_name] is not None
                               else None),
            ),
            name=f"prove:{agent_name}",
        )

    async def cancel(agent_name: str, reason: str) -> None:
        task = tasks.get(agent_name)
        if task is None or task.done():
            return
        _console.print(f"[yellow]interrupting {agent_name}: {reason}[/yellow]")
        if agents[agent_name].backend == "codex":
            interrupts[agent_name].set()
            try:
                # The Codex spawner requests turn/interrupt and drains the terminal
                # event. Its own forced-transport fallback completes within 45s.
                await asyncio.wait_for(asyncio.shield(task), timeout=50)
                return
            except asyncio.TimeoutError:
                _console.print(
                    f"[yellow]Codex interrupt timed out for {agent_name}; forcing task cancellation[/yellow]"
                )
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    unresolved_decls = [
        decl for decl, declaration in initial_state.get("declarations", {}).items()
        if declaration.get("status") != "solved"
    ]
    for index, agent in enumerate(roster.agents):
        target_decl = (
            unresolved_decls[index % len(unresolved_decls)]
            if unresolved_decls else None
        )
        launch(agent.name, target_decl=target_decl)

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
                interrupts.pop(name, None)
                merge_followup = merge_followups.pop(name, None)
                if merge_followup is not None:
                    candidate_id, followup_prompt = merge_followup
                    current = prove_state.load_state(paths.forum)
                    candidate = current.get("candidates", {}).get(candidate_id, {})
                    if candidate.get("status") == "merge_blocked":
                        launch(name, followup_prompt)
                        continue
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
                if kind in {"strategy_registered", "strategy_claimed", "strategy_assisted"}:
                    author = event.get("author", "")
                    decl = event.get("decl", "")
                    if author and decl:
                        worker_decls[author] = decl
                if kind == "candidate_submitted":
                    candidate = state["candidates"].get(event["candidate_id"], {})
                    if candidate.get("status") != "submitted":
                        continue
                    author = candidate.get("author")
                    target_decl = event["decl"]
                    active_workers = {
                        name for name, task in tasks.items()
                        if not task.done()
                        and (name == author or worker_decls.get(name) == target_decl)
                    }
                    await asyncio.gather(*(
                        cancel(
                            name,
                            f"candidate {event['candidate_id']} submitted for {target_decl}",
                        )
                        for name in active_workers
                    ))
                    _console.print(
                        f"[cyan]mechanically reviewing {event['candidate_id']} for {target_decl}[/cyan]"
                    )
                    try:
                        await asyncio.to_thread(
                            forum_server.sync_to_main,
                            event["candidate_id"],
                            "Unity",
                        )
                    except Exception as exc:
                        _console.print(
                            f"[red]mechanical review for {event['candidate_id']} failed: {exc!r}[/red]"
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
                elif kind in {"candidate_merge_failed", "candidate_verification_failed"}:
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
                    candidate = state["candidates"].get(event["candidate_id"], {})
                    if candidate.get("author"):
                        owners.add(candidate["author"])
                    for name in owners:
                        await cancel(name, f"candidate {event['candidate_id']} failed to merge")
                        author_instruction = (
                            " First call sync_from_main, then register and claim a repair strategy "
                            "before emitting a corrected candidate linked through supersedes."
                            if name == candidate.get("author") else ""
                        )
                        launch(name, retry_task + author_instruction, target_decl=event["decl"])
                elif kind == "candidate_merge_blocked":
                    candidate = state["candidates"].get(event["candidate_id"], {})
                    blocker = event.get("blocker", {})
                    merger = event.get("author") or candidate.get("last_merge_by")
                    if merger not in agents:
                        merger = candidate.get("author")
                    blocker_task = (
                        f"Mechanical review of candidate {event['candidate_id']} for "
                        f"{event['decl']} is blocked by {blocker.get('kind', 'an integration blocker')}: "
                        f"{blocker.get('message', '')[:800]}. Inspect the recorded git_status in "
                        "prove_status/forum_brief, determine who owns those changes, and preserve "
                        "legitimate work. Resolve the integration condition and call sync_to_main "
                        f"again for the same immutable candidate {event['candidate_id']}. Do not "
                        "start or resume a proof strategy unless this candidate is actually failed."
                    )
                    if merger in agents:
                        merge_followups[merger] = (event["candidate_id"], blocker_task)
                        if merger not in tasks:
                            merge_followups.pop(merger, None)
                            launch(merger, blocker_task)
                elif kind == "candidate_merged":
                    merge_followups.clear()
                    for name in list(tasks):
                        await cancel(name, f"{event['decl']} merged into main")
                    for name in agents:
                        released = prove_state.release_author_claims(
                            paths.forum, name, f"{event['decl']} merged"
                        )
                        synced = worktree.force_sync_from_main(root, name)
                        prove_state.record_sync(
                            paths.forum, name, synced["main_sha"], released
                        )
                        worker_decls.pop(name, None)
                    current = prove_state.load_state(paths.forum)
                    remaining = [
                        decl for decl, item in current.get("declarations", {}).items()
                        if item.get("status") != "solved"
                    ]
                    if remaining:
                        for index, name in enumerate(agents):
                            launch(
                                name,
                                normal_task,
                                target_decl=remaining[index % len(remaining)],
                            )
                elif kind == "worktree_synced":
                    pending_sync.discard(event.get("author"))
                elif kind == "strategy_registered":
                    idle = next((name for name in agents
                                 if name != event.get("author") and name not in tasks), None)
                    if idle:
                        launch(
                            idle,
                            normal_task,
                            target_decl=event.get("decl") or None,
                        )

            state = prove_state.load_state(paths.forum)
            for name, decl, question_id, author in question_wakeups.poll(
                state, agents, tasks, worker_decls
            ):
                launch(
                    name,
                    f"Forum question {question_id} from {author} is addressed to you. "
                    "Read the question in the Forum, respond using forum_answer if appropriate, "
                    "and coordinate any useful follow-up with the team.",
                    target_decl=decl,
                )

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
        # Keep worktrees and branches intact across critic rounds and stopped runs.
