"""Event-driven proving runtime built around authoritative proof state."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from rich.console import Console

from . import artifacts, library, prove_engine, prove_mcp, prove_state, worktree
from .orchestrator import _preamble, load_prompt, stop_requested
from .spawn import spawn
from .prove_submissions import NoToolSubmissions


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
        "Inspect your existing files and commits, and continue useful prior work. "
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
    prove_engine.configure(paths.forum, paths.project_root)
    return prove_engine.build_brief(author)


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
    prove_engine.configure(paths.forum, root)
    mcp = prove_mcp.for_run(mcp, paths)

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
    submissions = NoToolSubmissions(paths, worktrees)
    worker_decls: dict[str, str] = {}
    merge_followups: dict[str, str] = {}
    retry_candidates = {
        key for key, candidate in initial_state.get("candidates", {}).items()
        if candidate.get("status") == "merge_blocked"
    }
    normal_task = (
        "Inspect the assigned target and experiment in your own worktree. Begin with a focused "
        "search for an existing project or Mathlib theorem or API. Independently choose a useful "
        "approach, implement it, and prove missing helper lemmas when necessary. Preserve checked "
        "partial progress and exact remaining goals in your worktree. Ordinary progress commits "
        "are not proof submissions. When the complete proof builds, put the entire proof patch "
        "in ONE commit based on the main revision in this packet, squashing intermediate progress "
        "commits if needed. Use the exact subject `prove-candidate: <declaration>` with the assigned "
        "declaration name substituted. The controller will verify that immutable commit and merge "
        "it only after the original build and declaration checks pass. Do not edit controller state "
        "or merge into main yourself. The controller handles synchronization and further targets."
    )

    def launch(agent_name: str, task_prompt: str = normal_task, *, target_decl: str,
               integration: bool = False) -> None:
        if stop_requested(root):
            return
        if not integration:
            submissions.prepare(agent_name, target_decl)
        worker_decls[agent_name] = target_decl
        task_prompt = (
            _target_context(paths, target_decl)
            + f"\n\nYour assigned declaration is `{target_decl}`. "
            + ("Resolve the integration condition described below.\n\n" if integration else
               f"Submit a complete proof with the exact commit subject `prove-candidate: {target_decl}`.\n\n")
            + _prove_workspace_context(root, worktrees[agent_name], agent_name)
            + task_prompt
        )
        agent = agents[agent_name]
        system = (
            _preamble(agent, roster, icrl_enabled=False, forum_enabled=False, proof_controls=False)
            + _workspace_brief(paths, agent_name) + shared_prompt
        )
        interrupt_event = asyncio.Event()
        interrupts[agent_name] = interrupt_event
        tasks[agent_name] = asyncio.create_task(
            spawn(agent, system, task_prompt, worktrees[agent_name],
                  prove_mcp.for_checkout(mcp, worktrees[agent_name]), subagents=subagents,
                  mcp_profile="prove-no-tools", interrupt_event=interrupt_event,
                  env_overrides=({"UNITY_PROVE_CODEX_REASONING_EFFORT": efforts[agent_name]}
                                 if agent.backend == "codex" and efforts[agent_name] is not None
                                 else None)),
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
                await asyncio.wait_for(asyncio.shield(task), timeout=50)
                return
            except asyncio.TimeoutError:
                _console.print(f"[yellow]Codex interrupt timed out for {agent_name}[/yellow]")
            except Exception:
                return  # Reported when the completed task is drained below.
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def drain_finished() -> None:
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
            candidate_id = merge_followups.pop(name, None)
            if candidate_id:
                retry_candidates.add(candidate_id)

    def pending(state: dict) -> list[dict]:
        return [candidate for candidate in state.get("candidates", {}).values()
                if candidate.get("status") in {"submitted", "acceptable"}
                or (candidate.get("status") == "merge_blocked"
                    and candidate["candidate_id"] in retry_candidates)]

    def start_ready(extra: dict[str, str] | None = None) -> None:
        state = prove_state.load_state(paths.forum)
        ready = [decl for decl in state["declarations"] if submissions._ready(state, decl)]
        if not ready:
            return
        for index, name in enumerate(agents):
            if name in tasks:
                continue
            # Reuse the same reservation to preserve dirty or committed work on a retry.
            owned = next((s["decl"] for s in state["strategies"].values()
                          if s.get("owner") == name and s.get("status") == "claimed"
                          and s.get("decl") in ready), None)
            target = owned or ready[index % len(ready)]
            launch(name, (extra or {}).get(name, "") + normal_task, target_decl=target)

    def failure_note(candidate: dict) -> str:
        note = (f"Your prior candidate failed mechanical review: {candidate.get('merge_error', '')[:1600]}. "
                "The controller synchronized your worktree to main. Inspect the immutable prior commit "
                f"{candidate['commit_sha']} and diagnostics, then create a complete corrected proof. ")
        for key in ("merge_build", "verification"):
            record = candidate.get(key) or {}
            if record.get("artifact_id"):
                info = artifacts.artifact_info(paths.unity / "artifacts", record["artifact_id"])
                note += f"Read full {key} output: {artifacts._blob_path(paths.unity / 'artifacts', info['sha256'])}. "
        return note + "\n\n"

    try:
        # A stopped turn may have committed its proof just before the last poll.
        # Recover it before scheduling a new model turn in the preserved worktree.
        worker_decls.update({s['owner']: s['decl']
                             for s in initial_state.get('strategies', {}).values()
                             if s.get('owner') in agents and s.get('status') == 'claimed'
                             and s.get('description', '').startswith('no-tool reservation ')})
        submissions.poll(worker_decls)
        if not pending(prove_state.load_state(paths.forum)):
            start_ready()
        while not stop_requested(root):
            # Poll before draining finished turns: their final commit can be a submission.
            submissions.poll(worker_decls)
            drain_finished()
            state = prove_state.load_state(paths.forum)
            candidates = pending(state)
            if candidates:
                candidate = candidates[0]
                candidate_id = candidate["candidate_id"]
                retry_candidates.discard(candidate_id)
                # No writer may race the mechanical merge or subsequent synchronization.
                await asyncio.gather(*(cancel(name, f"reviewing {candidate_id}") for name in list(tasks)))
                drain_finished()
                if stop_requested(root):
                    break
                _console.print(f"[cyan]mechanically reviewing {candidate_id} for {candidate['decl']}[/cyan]")
                await asyncio.to_thread(prove_engine.sync_to_main, candidate_id, "Unity")
                current = prove_state.load_state(paths.forum)
                candidate = current["candidates"][candidate_id]
                prove_engine.write_status(paths)
                if candidate["status"] == "merge_blocked":
                    author = candidate["author"]
                    if author not in agents:
                        raise RuntimeError(f"candidate {candidate_id} requires its original worker {author}")
                    blocker = candidate.get("merge_blocker") or {}
                    prompt = (
                        f"Mechanical review of your immutable candidate {candidate_id} is blocked: "
                        f"{blocker.get('message', '')[:1600]}. Main checkout: {root}. "
                        f"Recorded Git status: {json.dumps(blocker.get('git_status', []))}. "
                        "Inspect the integration condition and preserve legitimate changes. Resolve the "
                        "condition without changing the candidate or weakening the proof. When this "
                        "turn ends, the controller will retry the same candidate automatically. "
                        "Do not start a new proof attempt or merge the candidate yourself."
                    )
                    merge_followups[author] = candidate_id
                    launch(author, prompt, target_decl=candidate["decl"], integration=True)
                elif candidate["status"] == "merged":
                    # Keep branches holding other submitted commits until their reviews finish.
                    protected = {c["author"] for c in current["candidates"].values()
                                 if c.get("status") in {"submitted", "acceptable", "merge_blocked"}}
                    for name in agents:
                        if name not in protected:
                            prove_engine.sync_from_main(name, f"{candidate['decl']} merged")
                    if not pending(current):
                        start_ready()
                elif candidate["status"] == "failed":
                    author = candidate["author"]
                    note = failure_note(candidate)
                    if author in agents:
                        prove_engine.sync_from_main(author, f"{candidate_id} failed")
                    if not pending(current):
                        start_ready({author: note})
                else:
                    raise RuntimeError(f"mechanical review left {candidate_id} in {candidate['status']}")
                continue
            if prove_state.all_solved(state) or not tasks:
                return state
            await asyncio.sleep(0.5)
        return prove_state.load_state(paths.forum)
    finally:
        for name in list(tasks):
            await cancel(name, "prove runtime ending")
        drain_finished()
        prove_engine.write_status(paths)
        # Keep reservations, worktrees, and branches across critic rounds and stopped runs.
