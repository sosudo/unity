"""Event-driven runtime for the two loops inside :command:`unity solve`.

The natural-language solution and its Lean formalization share one authoritative
``solve_state``.  Model turns are disposable workers: state, immutable artifacts,
and exact Git candidates survive interruptions and stage changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from rich.console import Console

from . import artifacts, library, solve_state, worktree
from .forum import solve_server
from .orchestrator import build_solve_mcp, load_prompt, mark_phase, stop_requested, toposort
from .solve_verifier import (
    integrate_formalization_candidate,
    main_checkout_issues,
    rollback_formalization_integration,
)
from .spawn import spawn


_console = Console()
_TERMINAL = {"complete", "stopped", "exhausted", "failed"}
_POLL_SECONDS = 0.25


def _safe_agent(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def _reset_run_memory(paths) -> None:
    """Remove only solve-owned live memory before initializing a fresh run."""
    # State itself is reset atomically by `solve_state.initialize(reset=True)`. Do
    # not delete the containing directory: state and merge locks must retain stable
    # inodes while a fresh run clears only disposable solve-owned children.
    solve_root = paths.unity / "solve"
    solve_root.mkdir(parents=True, exist_ok=True)
    for child in ("forum", "drafts", "integrations"):
        shutil.rmtree(solve_root / child, ignore_errors=True)
    for path in (
        paths.unity / "dag.json",
        paths.unity / "VERDICT.md",
        paths.unity / "critic.json",
        paths.unity / "finalized.json",
        paths.unity / "source" / "PROOF.tex",
    ):
        try:
            path.chmod(0o644)
        except OSError:
            pass
        path.unlink(missing_ok=True)


def _review_quorum() -> int:
    try:
        return max(1, int(os.getenv("UNITY_SOLVE_REVIEW_QUORUM", "1") or "1"))
    except ValueError:
        return 1


def _main_identity(project_root: Path) -> tuple[str, list[str]]:
    """Return exact HEAD and project inputs absent from that Git identity."""
    sha = worktree.main_commit(project_root)
    return sha, main_checkout_issues(project_root)


def _solve_preamble(agent, roster) -> str:
    team = "\n".join(
        f"- {item.name}: {item.model} ({item.backend})"
        f"{' [primary]' if item.is_primary else ''}"
        for item in roster.agents
    )
    return (
        f"You are agent '{agent.name}', running model '{agent.model}' "
        f"(backend: {agent.backend}).\n"
        "You are collaborating through one authoritative Forum for the complete unity solve "
        "run. Its state spans informal solving, solution review, and Lean formalization.\n"
        f"Team:\n{team}\n"
        f"Primary agent: {roster.primary.name}.\n"
        "Finish your assigned work or publish a concrete blocker and release it. Do not infer "
        "current shared state from old transcript text; refresh the solve brief.\n\n"
    )


def _configure_forum(paths) -> None:
    solve_server.configure(
        paths.unity / "solve",
        paths.project_root,
        paths.unity / "solve" / "forum",
    )


def _brief(paths, author: str) -> str:
    if os.getenv("UNITY_FORUM_BRIEF", "on").lower() == "off":
        return ""
    _configure_forum(paths)
    try:
        text = solve_server.solve_brief(author)
    except Exception:
        return ""
    return f"\nAuthoritative solve brief (refresh with solve_brief):\n{text}\n" if text else ""


def _system_prompt(paths, roster, agent, profile: str, phase_prompt: str, tools_prompt: str) -> str:
    context = library.library_context()
    prompt = (
        _solve_preamble(agent, roster)
        + _brief(paths, agent.name)
        + phase_prompt
        + "\n\n"
        + tools_prompt
    )
    if context:
        prompt += "\n\n" + context
    return prompt


async def _turn(
    paths,
    roster,
    agent,
    *,
    profile: str,
    phase_prompt: str,
    tools_prompt: str,
    task_prompt: str,
    cwd: Path,
    interrupt_event: asyncio.Event | None = None,
):
    return await spawn(
        agent,
        _system_prompt(paths, roster, agent, profile, phase_prompt, tools_prompt),
        task_prompt,
        cwd,
        build_solve_mcp(paths, profile, agent_name=agent.name),
        subagents=library.library_subagents(),
        interrupt_event=interrupt_event,
    )


async def _cancel_one(agent, task: asyncio.Task, event: asyncio.Event, reason: str) -> None:
    if task.done():
        return
    _console.print(f"[yellow]interrupting {agent.name}: {reason}[/yellow]")
    if agent.backend == "codex":
        event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=50)
            return
        except asyncio.TimeoutError:
            pass
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _cancel_workers(
    roster,
    tasks: dict[str, asyncio.Task],
    interrupts: dict[str, asyncio.Event],
    reason: str,
    names: set[str] | None = None,
) -> None:
    agents = {agent.name: agent for agent in roster.agents}
    selected = [name for name, task in tasks.items()
                if (names is None or name in names) and not task.done()]
    # Signal all Codex turns first so their drains happen concurrently.
    for name in selected:
        if agents[name].backend == "codex":
            interrupts[name].set()
    await asyncio.gather(*(
        _cancel_one(agents[name], tasks[name], interrupts[name], reason)
        for name in selected
    ), return_exceptions=True)


def _prepare_drafts(paths, roster) -> dict[str, Path]:
    root = paths.unity / "solve" / "drafts"
    root.mkdir(parents=True, exist_ok=True)
    canonical = paths.unity / "source" / "PROOF.tex"
    drafts: dict[str, Path] = {}
    for agent in roster.agents:
        directory = root / _safe_agent(agent.name)
        directory.mkdir(parents=True, exist_ok=True)
        draft = directory / "PROOF.tex"
        if not draft.exists() and canonical.exists():
            shutil.copyfile(canonical, draft)
        drafts[agent.name] = draft
    return drafts


def _materialize_solution(paths, candidate: dict) -> Path:
    payload = artifacts.artifact_bytes(paths.artifacts, candidate["artifact_id"])
    digest = hashlib.sha256(payload).hexdigest()
    if digest != candidate["sha256"]:
        raise RuntimeError("accepted solution artifact hash no longer matches solve state")
    destination = paths.unity / "source" / "PROOF.tex"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".PROOF-", suffix=".tex", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        # Formalizers submit corrections through private drafts. Making the accepted
        # source read-only prevents accidental in-place edits from changing the bytes
        # that the formalization gate names (the runtime still re-hashes it).
        destination.chmod(0o444)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def _ensure_materialized_solution(paths, state: dict) -> dict:
    """Restore and return the exact solution artifact admitted by the current gate."""
    gate = state.get("gates", {}).get("solution", {})
    candidate_id = gate.get("accepted_candidate_id")
    candidate = state.get("solution_candidates", {}).get(candidate_id or "")
    if gate.get("status") != "accepted" or not candidate:
        raise RuntimeError("formalization has no accepted immutable solution candidate")
    if candidate.get("sha256") != gate.get("sha256"):
        raise RuntimeError("accepted solution candidate identity disagrees with its gate")
    payload = artifacts.artifact_bytes(paths.artifacts, candidate["artifact_id"])
    if hashlib.sha256(payload).hexdigest() != gate["sha256"]:
        raise RuntimeError("accepted solution artifact bytes fail their recorded SHA-256")
    canonical = paths.unity / "source" / "PROOF.tex"
    try:
        current_sha = hashlib.sha256(canonical.read_bytes()).hexdigest()
    except OSError:
        current_sha = ""
    if current_sha != gate["sha256"]:
        _materialize_solution(paths, candidate)
    return candidate


async def _run_solving(paths, roster) -> None:
    mark_phase("solve", "solving")
    drafts = _prepare_drafts(paths, roster)
    state = solve_state.load_state(paths.unity)
    solution_gate = state.get("gates", {}).get("solution", {})
    source_fix_id = solution_gate.get("source_fix_id") or ""
    source_fix = state.get("source_fixes", {}).get(source_fix_id)
    targeted_repair = solution_gate.get("repair_mode") == "targeted_source_fix"
    active_agents = list(roster.agents)
    if targeted_repair and source_fix:
        # One focused repair worker is useful work; a full fresh mathematical
        # swarm is reserved for an explicit `reopen_solving` verdict.
        repair_agent = max(
            (agent for agent in roster.agents if agent.name != source_fix.get("author")),
            key=lambda agent: getattr(agent, "strength", 0),
            default=roster.primary,
        )
        active_agents = [repair_agent]
        current_revision = solution_gate.get("revision")
        iterative = [
            candidate for candidate in state.get("solution_candidates", {}).values()
            if candidate.get("gate_revision") == current_revision
            and candidate.get("status") == "rejected"
        ]
        base_candidate = max(
            iterative,
            key=lambda item: item.get("updated_at", item.get("created_at", 0)),
            default=state.get("solution_candidates", {}).get(
                source_fix.get("candidate_id", "")
            ),
        )
        if base_candidate:
            payload = artifacts.artifact_bytes(paths.artifacts, base_candidate["artifact_id"])
            drafts[repair_agent.name].write_bytes(payload)

    reopen_context = ""
    reopen_reason = solution_gate.get("reopen_reason") or (
        source_fix.get("reason", "") if source_fix else ""
    )
    if reopen_reason or source_fix:
        evidence_parts = []
        if source_fix and source_fix.get("evidence"):
            evidence_parts.append(f"inline evidence: {source_fix['evidence']}")
        if source_fix and source_fix.get("artifact_id"):
            evidence_parts.append(
                f"evidence artifact `{source_fix['artifact_id']}`"
                + (f" at SHA-256 `{source_fix['artifact_sha256']}`"
                   if source_fix.get("artifact_sha256") else "")
            )
        evidence_reference = "; ".join(evidence_parts) or "no separate evidence artifact recorded"
        fix_reference = f"source-fix `{source_fix_id}`" if source_fix_id else "gate reopen"
        reopen_context = (
            f" This is a full solving reopen triggered by {fix_reference}. "
            f"Exact reopen reason: {reopen_reason}. Evidence reference: {evidence_reference}."
        )
    phase_prompt = load_prompt("solve/SOLVING")
    tools_prompt = load_prompt("SOLVE_SOLVING_TOOLS")
    tasks: dict[str, asyncio.Task] = {}
    interrupts: dict[str, asyncio.Event] = {}
    failures = 0

    for agent in active_agents:
        event = asyncio.Event()
        interrupts[agent.name] = event
        if targeted_repair and source_fix:
            task_prompt = (
                f"Perform targeted source repair `{source_fix['source_fix_id']}` on the previously "
                "accepted solution. Do not restart the full mathematical search. The critic's issue "
                f"is: {source_fix.get('reason', '')} Suggested correction: "
                f"{source_fix.get('suggested_fix') or source_fix.get('summary', '')}. "
                f"Edit `{drafts[agent.name].relative_to(paths.project_root)}`, preserve unaffected "
                "arguments, validate the exact concern, and submit the corrected file immediately."
            )
        else:
            task_prompt = (
                "Attack the original mathematical problem collaboratively. Your private candidate "
                f"document is `{drafts[agent.name].relative_to(paths.project_root)}`. Research, test, "
                "publish findings, and coordinate strategies continuously. Submit that exact file as "
                f"soon as it is a complete rigorous solution.{reopen_context}"
            )
        tasks[agent.name] = asyncio.create_task(
            _turn(
                paths, roster, agent, profile="solving", phase_prompt=phase_prompt,
                tools_prompt=tools_prompt, task_prompt=task_prompt,
                cwd=paths.project_root, interrupt_event=event,
            ),
            name=f"solve:solving:{agent.name}",
        )

    try:
        while True:
            if stop_requested(paths.project_root):
                await _cancel_workers(roster, tasks, interrupts, "safe stop requested")
                for name in list(tasks):
                    _release_agent_strategies(
                        paths, name, "solving", "controller stopped the owning model turn",
                    )
                solve_state.set_outcome(paths.unity, "stopped", "Unity", "safe stop requested")
                return
            state = solve_state.load_state(paths.unity)
            if state["stage"] != "solving":
                await _cancel_workers(
                    roster, tasks, interrupts,
                    "solution candidate submitted; reallocating to exact-candidate review",
                )
                return
            for name, task in list(tasks.items()):
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    failures += 1
                    _console.print(f"[red]solve worker {name} failed: {exc!r}[/red]")
                _release_agent_strategies(
                    paths, name, "solving", "worker ended without an accepted solution",
                )
                del tasks[name]
                interrupts.pop(name, None)
            if not tasks:
                outcome = "failed" if failures >= len(active_agents) else "exhausted"
                solve_state.set_outcome(
                    paths.unity, outcome, "Unity",
                    "all informal solvers ended without an accepted candidate",
                )
                return
            await asyncio.sleep(_POLL_SECONDS)
    finally:
        await _cancel_workers(roster, tasks, interrupts, "leaving informal solving")


def _current_review_candidate(state: dict) -> dict | None:
    revision = state["gates"]["solution"]["revision"]
    candidates = [
        candidate for candidate in state.get("solution_candidates", {}).values()
        if candidate.get("gate_revision") == revision
        and candidate.get("status") in {"submitted", "reviewable"}
    ]
    return max(candidates, key=lambda item: item.get("created_at", 0), default=None)


async def _run_solution_review(paths, roster) -> None:
    mark_phase("solve", "solution_review")
    state = solve_state.load_state(paths.unity)
    candidate = _current_review_candidate(state)
    if candidate is None:
        solve_state.set_outcome(
            paths.unity, "failed", "Unity", "solution-review stage has no reviewable candidate"
        )
        return
    quorum = _review_quorum()
    existing = {review["author"] for review in candidate.get("reviews", [])
                if review.get("verdict") == "approve"}
    reviewers = sorted(
        (agent for agent in roster.agents
         if agent.name != candidate["author"] and agent.name not in existing),
        key=lambda item: -getattr(item, "strength", 0),
    )
    needed = max(0, quorum - len(existing))
    if len(reviewers) < needed:
        solve_state.reject_solution_candidate(
            paths.unity, candidate["candidate_id"], "Unity",
            f"independent review quorum {quorum} cannot be satisfied by this roster",
        )
        return

    phase_prompt = load_prompt("solve/SOLUTION_REVIEW")
    tools_prompt = load_prompt("SOLVE_REVIEW_TOOLS")
    tasks: dict[str, asyncio.Task] = {}
    interrupts: dict[str, asyncio.Event] = {}
    for reviewer in reviewers[:needed]:
        event = asyncio.Event()
        interrupts[reviewer.name] = event
        task_prompt = (
            f"Independently review solution candidate `{candidate['candidate_id']}` at artifact "
            f"`{candidate['artifact_id']}` with SHA-256 `{candidate['sha256']}`. Submit exactly one "
            "approve or object review through the solve Forum."
        )
        tasks[reviewer.name] = asyncio.create_task(
            _turn(
                paths, roster, reviewer, profile="solution_review",
                phase_prompt=phase_prompt, tools_prompt=tools_prompt,
                task_prompt=task_prompt, cwd=paths.project_root,
                interrupt_event=event,
            ),
            name=f"solve:review:{reviewer.name}",
        )

    try:
        while tasks:
            if stop_requested(paths.project_root):
                await _cancel_workers(roster, tasks, interrupts, "safe stop requested")
                solve_state.set_outcome(paths.unity, "stopped", "Unity", "safe stop requested")
                return
            state = solve_state.load_state(paths.unity)
            if state["stage"] != "solution_review":
                await _cancel_workers(roster, tasks, interrupts, "candidate review state changed")
                return
            for name, task in list(tasks.items()):
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _console.print(f"[red]solution reviewer {name} failed: {exc!r}[/red]")
                del tasks[name]
                interrupts.pop(name, None)
            await asyncio.sleep(_POLL_SECONDS)
    finally:
        await _cancel_workers(roster, tasks, interrupts, "solution review complete")

    state = solve_state.load_state(paths.unity)
    if state["stage"] != "solution_review":
        return
    candidate = state["solution_candidates"].get(candidate["candidate_id"], candidate)
    approvals = {review["author"] for review in candidate.get("reviews", [])
                 if review.get("verdict") == "approve"}
    open_objections = [
        item for item in state.get("objections", {}).values()
        if item.get("target_id") == candidate["candidate_id"] and item.get("status") == "open"
    ]
    if len(approvals) >= quorum and not open_objections:
        accepted = solve_state.accept_solution_candidate(
            paths.unity,
            candidate["candidate_id"],
            "Unity",
            f"{len(approvals)} independent approval(s), zero open objections",
        )["candidate"]
        _materialize_solution(paths, accepted)
    else:
        solve_state.reject_solution_candidate(
            paths.unity,
            candidate["candidate_id"],
            "Unity",
            "independent review did not satisfy the acceptance policy",
        )


def _valid_dag(paths, state: dict) -> tuple[bool, dict]:
    dag_path = paths.unity / "dag.json"
    try:
        dag = json.loads(dag_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False, {}
    solution = state["gates"]["solution"]
    valid = (
        isinstance(dag.get("chunks"), list)
        and bool(dag["chunks"])
        and int(dag.get("solution_gate_revision", -1)) == int(solution["revision"])
        and dag.get("solution_sha256") == solution["sha256"]
    )
    if not valid:
        return False, dag
    chunks = dag["chunks"]
    if any(not isinstance(chunk, dict) or not str(chunk.get("id") or "").strip()
           for chunk in chunks):
        return False, dag
    ids = [str(chunk["id"]).strip() for chunk in chunks]
    if len(ids) != len(set(ids)):
        return False, dag
    dependencies: dict[str, list[str]] = {}
    for chunk, chunk_id in zip(chunks, ids):
        raw = chunk.get("dependencies") or []
        if not isinstance(raw, list):
            return False, dag
        deps = [
            str(item.get("chunk_id") or "").strip() if isinstance(item, dict)
            else str(item).strip()
            for item in raw
        ]
        if any(not dep or dep not in set(ids) or dep == chunk_id for dep in deps):
            return False, dag
        dependencies[chunk_id] = list(dict.fromkeys(deps))
    resolved: set[str] = set()
    while len(resolved) < len(ids):
        newly_ready = {
            chunk_id for chunk_id, deps in dependencies.items()
            if chunk_id not in resolved and all(dep in resolved for dep in deps)
        }
        if not newly_ready:
            return False, dag
        resolved.update(newly_ready)
    return valid, dag


async def _run_chunking(paths, roster) -> None:
    mark_phase("solve", "chunking")
    state = solve_state.load_state(paths.unity)
    solution = state["gates"]["solution"]
    canonical = paths.unity / "source" / "PROOF.tex"
    try:
        digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
    except OSError as exc:
        solve_state.set_outcome(paths.unity, "failed", "Unity", f"accepted solution missing: {exc}")
        return
    if digest != solution.get("sha256"):
        solve_state.set_outcome(
            paths.unity, "failed", "Unity", "canonical PROOF.tex differs from the accepted artifact"
        )
        return

    valid, dag = _valid_dag(paths, state)
    if not valid:
        phase_prompt = load_prompt("solve/CHUNKING")
        tools_prompt = load_prompt("SOLVE_CHUNKING_TOOLS")
        ordered = [roster.primary] + [agent for agent in roster.agents if not agent.is_primary]
        for agent in ordered:
            task_prompt = (
                f"Create the semantic formalization DAG for solution gate revision "
                f"{solution['revision']} and SHA-256 {solution['sha256']}. Write `.unity/dag.json` "
                "and do not begin Lean implementation."
            )
            try:
                await _turn(
                    paths, roster, agent, profile="chunking",
                    phase_prompt=phase_prompt, tools_prompt=tools_prompt,
                    task_prompt=task_prompt, cwd=paths.project_root,
                )
            except Exception as exc:
                _console.print(f"[red]chunker {agent.name} failed: {exc!r}[/red]")
            state = solve_state.load_state(paths.unity)
            if state.get("stage") != "chunking":
                return
            valid, dag = _valid_dag(paths, state)
            if valid:
                break
    if not valid:
        solve_state.set_outcome(
            paths.unity, "failed", "Unity",
            "no chunker produced a non-empty DAG bound to the accepted solution revision",
        )
        return
    if solve_state.load_state(paths.unity).get("stage") != "chunking":
        return
    toposort(paths)
    solve_state.initialize_formalization_tasks(paths.unity, paths.unity / "dag.json", "Unity")


def _ready_formal_tasks(state: dict) -> list[dict]:
    gate_revision = state["gates"]["formalization"]["revision"]
    tasks = state.get("formal_tasks", {})
    ready = []
    for task in tasks.values():
        if task.get("gate_revision") != gate_revision or task.get("status") not in {"pending", "failed"}:
            continue
        if all(tasks.get(dep, {}).get("status") == "complete" for dep in task.get("dependencies", [])):
            ready.append(task)
    return sorted(ready, key=lambda item: item.get("created_at", 0))


def _verification_artifact_sha(paths, result: dict) -> tuple[str, str]:
    artifact_id = str((result.get("verification") or {}).get("artifact_id") or "")
    if not artifact_id:
        return "", ""
    try:
        record = artifacts.artifact_info(paths.artifacts, artifact_id)
    except ValueError:
        return artifact_id, ""
    return artifact_id, record.get("sha256", "")


async def _integrate_quiescent(project_root: Path, candidate: dict) -> dict:
    """Do not let controller cancellation abandon a Git-mutating verifier thread.

    Python cannot stop a function already running in ``to_thread``. Shielding and
    draining it before cancellation propagates guarantees that runtime cleanup never
    races a verifier which is still applying, building, committing, or rolling back.
    """
    task = asyncio.create_task(
        asyncio.to_thread(integrate_formalization_candidate, project_root, candidate),
        name=f"solve:verify:{candidate.get('candidate_id', 'unknown')}",
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        _console.print("[yellow]waiting for deterministic integration to become quiescent[/yellow]")
        try:
            await asyncio.shield(task)
        except Exception as exc:
            _console.print(f"[red]deterministic integration failed during shutdown: {exc!r}[/red]")
        raise


def _release_formal_assignments(
    paths,
    assignments: dict[str, str],
    reason: str,
    *,
    agent_names: set[str] | None = None,
) -> None:
    """Release controller-owned live claims when their model turns are gone."""
    names = set(assignments) | set(agent_names or ())
    for name in names:
        state = solve_state.load_state(paths.unity)
        owned = [
            task for task in state.get("formal_tasks", {}).values()
            if task.get("status") == "claimed" and task.get("owner") == name
        ]
        for formal_task in owned:
            try:
                solve_state.release_formal_task(
                    paths.unity, formal_task["task_id"], name, reason,
                )
            except ValueError:
                pass


def _reconcile_formal_assignments(
    paths, active_names: set[str], assignments: dict[str, str]
) -> list[str]:
    """Repair legacy duplicate claims while retaining each live turn's assignment."""
    released: list[str] = []
    for name in active_names:
        state = solve_state.load_state(paths.unity)
        gate_revision = state.get("gates", {}).get("formalization", {}).get("revision")
        owned = sorted(
            (task for task in state.get("formal_tasks", {}).values()
             if task.get("status") == "claimed" and task.get("owner") == name
             and task.get("gate_revision") == gate_revision),
            key=lambda task: (task.get("created_at", 0), task["task_id"]),
        )
        preferred = assignments.get(name)
        keep = next((task for task in owned if task["task_id"] == preferred), None)
        if keep is None and owned:
            keep = owned[0]
        for extra in owned:
            if keep is not None and extra["task_id"] == keep["task_id"]:
                continue
            try:
                solve_state.release_formal_task(
                    paths.unity,
                    extra["task_id"],
                    name,
                    "released duplicate residual claim during controller reconciliation",
                )
                released.append(extra["task_id"])
            except ValueError:
                pass
        if keep is None:
            assignments.pop(name, None)
        else:
            assignments[name] = keep["task_id"]
    return released


def _release_agent_strategies(paths, author: str, stage: str, reason: str) -> None:
    """Release exclusive strategy claims when the owning model turn no longer exists."""
    state = solve_state.load_state(paths.unity)
    for strategy in state.get("strategies", {}).values():
        if (strategy.get("owner") != author or strategy.get("stage") != stage
                or strategy.get("status") != "claimed"):
            continue
        try:
            solve_state.release_strategy(
                paths.unity, strategy["strategy_id"], author, reason,
            )
        except ValueError:
            pass


def _recover_orphaned_formal_claims(paths) -> None:
    """A newly entered controller has no live turns, so persisted claims are orphaned."""
    state = solve_state.load_state(paths.unity)
    gate_revision = state.get("gates", {}).get("formalization", {}).get("revision")
    for formal_task in state.get("formal_tasks", {}).values():
        if (formal_task.get("gate_revision") != gate_revision
                or formal_task.get("status") != "claimed"
                or not formal_task.get("owner")):
            continue
        try:
            solve_state.release_formal_task(
                paths.unity, formal_task["task_id"], formal_task["owner"],
                "reclaimed when the solve controller entered formalization",
            )
        except ValueError:
            pass


async def _run_formalizing(paths, roster) -> None:
    mark_phase("solve", "formalizing")
    root = paths.project_root
    _recover_orphaned_formal_claims(paths)
    try:
        _ensure_materialized_solution(paths, solve_state.load_state(paths.unity))
    except (OSError, ValueError, RuntimeError) as exc:
        solve_state.set_outcome(
            paths.unity, "failed", "Unity", f"accepted solution identity failed: {exc}",
        )
        return
    phase_prompt = load_prompt("solve/FORMALIZING")
    tools_prompt = load_prompt("SOLVE_FORMALIZING_TOOLS")
    worktrees: dict[str, Path] = {}
    tasks: dict[str, asyncio.Task] = {}
    interrupts: dict[str, asyncio.Event] = {}
    assignments: dict[str, str] = {}
    attempted: set[tuple[str, str]] = set()
    seen_candidates: set[str] = set()
    agents = {agent.name: agent for agent in roster.agents}

    try:
        async def launch(agent_name: str, formal_task: dict) -> None:
            agent = agents[agent_name]
            task_id = formal_task["task_id"]
            tree = worktrees.get(agent_name)
            try:
                if tree is None:
                    existing = worktree.agent_worktree(root, agent_name)
                    if existing.is_dir():
                        tree = existing
                        worktree.link_runtime_state(tree, root)
                        worktree.symlink_lake_cache(tree, root)
                    else:
                        tree = worktree.create_worktree(agent_name, root)
                        worktree.symlink_lake_cache(tree, root)
                        worktree.link_runtime_state(tree, root)
                    worktrees[agent_name] = tree
                worktree.force_sync_from_main(root, agent_name)
            except Exception as exc:
                _console.print(
                    f"[red]could not prepare {agent_name}'s worktree for {task_id}: {exc!r}[/red]"
                )
                attempted.add((agent_name, task_id))
                return
            claimed = solve_state.claim_formal_task(paths.unity, task_id, agent_name)
            if claimed.get("status") != "claimed":
                return
            event = asyncio.Event()
            interrupts[agent_name] = event
            assignments[agent_name] = task_id
            attempted.add((agent_name, task_id))
            task_prompt = (
                f"Your assigned formalization task is `{task_id}` for chunk "
                f"`{formal_task.get('chunk_id') or formal_task.get('task_key')}`. "
                f"Description: {formal_task.get('description', '')}\n"
                "Dependencies are integrated. Register a concrete implementation strategy, work "
                "in this worktree, run lake build, commit, and emit the exact candidate SHA."
            )
            tasks[agent_name] = asyncio.create_task(
                _turn(
                    paths, roster, agent, profile="formalizing",
                    phase_prompt=phase_prompt, tools_prompt=tools_prompt,
                    task_prompt=task_prompt, cwd=worktrees[agent_name],
                    interrupt_event=event,
                ),
                name=f"solve:formalizing:{agent_name}:{task_id}",
            )

        while True:
            if stop_requested(root):
                await _cancel_workers(roster, tasks, interrupts, "safe stop requested")
                _release_formal_assignments(
                    paths, assignments, "controller stopped; model turn is no longer active",
                    agent_names=set(tasks) | set(worktrees),
                )
                solve_state.set_outcome(paths.unity, "stopped", "Unity", "safe stop requested")
                return
            state = solve_state.load_state(paths.unity)
            if state["stage"] != "formalizing":
                await _cancel_workers(roster, tasks, interrupts, "formalization state changed")
                return

            # Agent tools are authoritative. Reconcile the controller's assignment
            # cache in case a worker switched tasks. State now prevents multiple
            # current claims; this also repairs residual claims from older state.
            _reconcile_formal_assignments(paths, set(tasks), assignments)

            # Candidate discovery is an interrupt for the exact formal task.
            fresh = [candidate for candidate in state.get("formal_candidates", {}).values()
                     if candidate["candidate_id"] not in seen_candidates
                     and candidate.get("status") in {"submitted", "verifying", "verified"}]
            for candidate in sorted(fresh, key=lambda item: item.get("created_at", 0)):
                seen_candidates.add(candidate["candidate_id"])
                same_task = {
                    name for name, task_id in assignments.items()
                    if task_id == candidate["task_id"]
                }
                same_task.add(candidate.get("author", ""))
                same_task.discard("")
                await _cancel_workers(
                    roster, tasks, interrupts,
                    f"candidate {candidate['candidate_id']} submitted for {candidate['task_id']}",
                    same_task,
                )
                for name in same_task:
                    tasks.pop(name, None)
                    interrupts.pop(name, None)
                    assignments.pop(name, None)

                current = solve_state.load_state(paths.unity)
                gate = current["gates"]["formalization"]
                solution = current["gates"]["solution"]
                if (candidate.get("gate_revision") != gate["revision"]
                        or candidate.get("accepted_solution_sha256") != solution["sha256"]):
                    _release_formal_assignments(
                        paths,
                        {candidate.get("author", ""): candidate.get("task_id", "")},
                        "candidate no longer belongs to the active formalization gate",
                    )
                    continue
                reservation = solve_state.begin_formal_candidate_verification(
                    paths.unity, candidate["candidate_id"], "Unity",
                )
                reservation_status = reservation.get("status")
                if reservation_status not in {"verifying", "verified"}:
                    if reservation_status != "conflict":
                        _release_formal_assignments(
                            paths,
                            {candidate.get("author", ""): candidate.get("task_id", "")},
                            "candidate could not enter deterministic verification",
                        )
                    continue
                candidate = reservation["candidate"]
                verification_reserved = reservation_status == "verifying"
                try:
                    _ensure_materialized_solution(paths, solve_state.load_state(paths.unity))
                except (OSError, ValueError, RuntimeError) as exc:
                    solve_state.finish_formal_candidate_verification(
                        paths.unity, candidate["candidate_id"], "Unity", "failed",
                        issues=[f"accepted solution identity failed: {exc}"],
                    )
                    with solve_state.transaction(paths.unity) as mutable:
                        formal_task = mutable["formal_tasks"].get(candidate["task_id"])
                        if formal_task and formal_task.get("status") == "claimed":
                            formal_task.update(
                                status="failed", owner=None,
                                status_reason=f"accepted solution identity failed: {exc}",
                            )
                    continue
                verifier_candidate = {
                    **candidate,
                    "solution_sha256": candidate.get("accepted_solution_sha256", ""),
                }
                result = await _integrate_quiescent(root, verifier_candidate)
                artifact_id, artifact_sha = _verification_artifact_sha(paths, result)
                issues = list((result.get("verification") or {}).get("issues") or [])
                verification_status = str(
                    (result.get("verification") or {}).get("status")
                    or ("passed" if result.get("ok") else "failed")
                )
                if verification_status not in {"passed", "failed", "blocked"}:
                    verification_status = "failed"
                try:
                    if verification_reserved:
                        solve_state.finish_formal_candidate_verification(
                            paths.unity,
                            candidate["candidate_id"],
                            "Unity",
                            verification_status,
                            artifact_id=artifact_id,
                            artifact_sha256=artifact_sha,
                            issues=issues or (
                                [] if result.get("ok")
                                else [result.get("error", "verification failed")]
                            ),
                            details=result.get("verification") or {},
                        )
                    elif not result.get("ok"):
                        # A prior process may have persisted `verified` immediately
                        # before dying. If its exact integration cannot be recovered,
                        # make that failure explicit rather than stranding the task.
                        with solve_state.transaction(paths.unity) as mutable:
                            item = mutable["formal_candidates"].get(candidate["candidate_id"])
                            if item and item.get("status") == "verified":
                                item.update(
                                    status=verification_status,
                                    integration_recovery_error=result.get(
                                        "error", "verification recovery failed"
                                    ),
                                )
                    if not result.get("ok"):
                        raise RuntimeError(result.get("error", "verification failed"))
                    with solve_state.transaction(paths.unity) as mutable:
                        item = mutable["formal_candidates"][candidate["candidate_id"]]
                        if item.get("status") != "verified":
                            raise ValueError("candidate was invalidated during deterministic verification")
                        item["integrated_main_sha"] = result["main_sha"]
                        item["integration_verification"] = result.get("verification", {})
                    solve_state.accept_formal_candidate(
                        paths.unity, candidate["candidate_id"], "Unity",
                        "deterministic exact-commit build passed",
                    )
                except (ValueError, RuntimeError) as exc:
                    if result.get("ok"):
                        rollback = await asyncio.to_thread(
                            rollback_formalization_integration, root, result,
                        )
                        if not rollback.get("ok"):
                            solve_state.set_outcome(
                                paths.unity, "failed", "Unity",
                                "a stale formalization integration could not be rolled back safely: "
                                + rollback.get("error", str(exc)),
                            )
                            return
                    with solve_state.transaction(paths.unity) as mutable:
                        item = mutable["formal_candidates"].get(candidate["candidate_id"])
                        formal_task = mutable["formal_tasks"].get(candidate["task_id"])
                        current_gate = mutable["gates"]["formalization"]["revision"]
                        if (item and item.get("gate_revision") == current_gate
                                and mutable.get("stage") == "formalizing"):
                            item["acceptance_error"] = str(exc)
                            if item.get("status") not in {"blocked", "failed"}:
                                item["status"] = "failed"
                        if (formal_task and formal_task.get("gate_revision") == current_gate
                                and formal_task.get("status") == "claimed"):
                            formal_task.update(status="failed", owner=None,
                                               status_reason=result.get("error", "verification failed"))
                    _release_agent_strategies(
                        paths, candidate.get("author", ""), "formalizing",
                        "candidate was not accepted",
                    )

            state = solve_state.load_state(paths.unity)
            if state["stage"] != "formalizing":
                continue
            if solve_state.all_formalization_tasks_complete(paths.unity):
                await _cancel_workers(roster, tasks, interrupts, "all formalization tasks integrated")
                main_sha, dirty = _main_identity(root)
                if dirty:
                    solve_state.set_outcome(
                        paths.unity, "failed", "Unity",
                        "formalization completed but main has tracked changes: "
                        + "; ".join(dirty[:20]),
                    )
                    return
                try:
                    _ensure_materialized_solution(paths, solve_state.load_state(paths.unity))
                except (OSError, ValueError, RuntimeError) as exc:
                    solve_state.set_outcome(
                        paths.unity, "failed", "Unity",
                        f"formalization completed against invalid solution bytes: {exc}",
                    )
                    return
                solve_state.close_formalization_gate(
                    paths.unity, "Unity", "all current-gate tasks have verified integrations",
                    integrated_main_sha=main_sha,
                )
                return

            # Reclaim finished turns that produced no candidate.
            for name, task in list(tasks.items()):
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _console.print(f"[red]formalization worker {name} failed: {exc!r}[/red]")
                task_id = assignments.pop(name, "")
                tasks.pop(name, None)
                interrupts.pop(name, None)
                current = solve_state.load_state(paths.unity)
                formal_task = current.get("formal_tasks", {}).get(task_id)
                if formal_task and formal_task.get("owner") == name and formal_task.get("status") == "claimed":
                    try:
                        solve_state.release_formal_task(
                            paths.unity, task_id, name,
                            "worker ended without submitting a candidate",
                        )
                    except ValueError:
                        pass
                _release_agent_strategies(
                    paths, name, "formalizing", "worker ended without an accepted candidate",
                )

            state = solve_state.load_state(paths.unity)
            ready = _ready_formal_tasks(state)
            idle = [agent.name for agent in roster.agents if agent.name not in tasks]
            for formal_task in ready:
                available = next((name for name in idle
                                  if (name, formal_task["task_id"]) not in attempted), None)
                if available is None:
                    continue
                idle.remove(available)
                await launch(available, formal_task)

            if not tasks:
                state = solve_state.load_state(paths.unity)
                remaining = [task for task in state.get("formal_tasks", {}).values()
                             if task.get("gate_revision") == state["gates"]["formalization"]["revision"]
                             and task.get("status") not in {"complete", "cancelled", "superseded"}]
                if remaining:
                    solve_state.set_outcome(
                        paths.unity, "exhausted", "Unity",
                        "configured formalization workers exhausted the current ready tasks",
                    )
                    return
            await asyncio.sleep(_POLL_SECONDS)
    finally:
        await _cancel_workers(roster, tasks, interrupts, "leaving formalization")
        _release_formal_assignments(
            paths, assignments, "controller left formalization; model turn is no longer active",
            agent_names=set(tasks) | set(worktrees),
        )
        for name in list(tasks):
            _release_agent_strategies(
                paths, name, "formalizing", "controller left the owning model turn",
            )
        final_state = solve_state.load_state(paths.unity)
        preserve_authors = {
            candidate.get("author")
            for candidate in final_state.get("formal_candidates", {}).values()
            if candidate.get("status") in {"submitted", "verifying"}
        }
        for agent in roster.agents:
            tree = worktrees.get(agent.name)
            if tree is not None and agent.name not in preserve_authors:
                worktree.cleanup_worktree(agent.name, tree, root)


async def _run_critic(paths, roster) -> None:
    mark_phase("solve", "critic")
    state = solve_state.load_state(paths.unity)
    solution = state["gates"]["solution"]
    formal = state["gates"]["formalization"]
    main_sha, dirty = _main_identity(paths.project_root)
    expected_main_sha = formal.get("integrated_main_sha", "")
    if dirty or main_sha != expected_main_sha:
        reason = (
            "main has tracked changes" if dirty
            else f"main HEAD {main_sha} differs from closed gate {expected_main_sha}"
        )
        solve_state.set_outcome(
            paths.unity, "failed", "Unity", f"critic cannot review an inexact revision: {reason}",
        )
        return
    try:
        solution_candidate = _ensure_materialized_solution(paths, state)
    except (OSError, ValueError, RuntimeError) as exc:
        solve_state.set_outcome(
            paths.unity, "failed", "Unity", f"critic cannot load accepted solution: {exc}",
        )
        return
    phase_prompt = load_prompt("solve/CRITIC")
    tools_prompt = load_prompt("SOLVE_CRITIC_TOOLS")
    task_prompt = (
        f"Audit solution candidate `{solution.get('accepted_candidate_id')}` at SHA-256 "
        f"`{solution.get('sha256')}` (artifact `{solution_candidate.get('artifact_id')}`), "
        f"solution gate revision {solution.get('revision')}, "
        f"formalization gate revision {formal.get('revision')}, and exact main commit `{main_sha}`. "
        f"Submit one structured formalization verdict with reviewed_main_sha=`{main_sha}`."
    )
    try:
        await _turn(
            paths, roster, roster.primary, profile="critic",
            phase_prompt=phase_prompt, tools_prompt=tools_prompt,
            task_prompt=task_prompt, cwd=paths.project_root,
        )
    except Exception as exc:
        _console.print(f"[red]solve critic failed: {exc!r}[/red]")
    state = solve_state.load_state(paths.unity)
    if state["stage"] == "complete":
        actual_sha, dirty = _main_identity(paths.project_root)
        source_ok = True
        try:
            _ensure_materialized_solution(paths, state)
        except (OSError, ValueError, RuntimeError):
            source_ok = False
        if actual_sha != main_sha or dirty or not source_ok:
            solve_state.invalidate_critic_approval(
                paths.unity, "Unity", main_sha,
                "main or accepted solution bytes changed during critic review",
            )
            solve_state.set_outcome(
                paths.unity, "failed", "Unity",
                "critic approval was invalidated because the reviewed revision changed",
            )
            return
    if state["stage"] == "critic":
        solve_state.set_outcome(
            paths.unity, "failed", "Unity", "critic ended without a structured verdict"
        )


async def run_solve_runtime(roster, paths, *, reset_state: bool = False) -> dict:
    """Run solving, review, chunking, formalization, and critic transitions to quiescence."""
    if reset_state:
        _reset_run_memory(paths)
        problem = paths.unity_md.read_text(errors="replace") if paths.unity_md.exists() else ""
        record = artifacts.store_text(
            paths.artifacts,
            problem,
            kind="solve_problem",
            source=str(paths.unity_md),
        )
        solve_state.initialize(
            paths.unity,
            record["artifact_id"],
            record["sha256"],
            reset=True,
            problem_path=paths.unity_md if paths.unity_md.exists() else None,
        )
    else:
        state = solve_state.load_state(paths.unity)
        if not state.get("run_id"):
            problem = paths.unity_md.read_text(errors="replace") if paths.unity_md.exists() else ""
            record = artifacts.store_text(
                paths.artifacts, problem, kind="solve_problem", source=str(paths.unity_md),
            )
            solve_state.initialize(
                paths.unity, record["artifact_id"], record["sha256"],
                problem_path=paths.unity_md if paths.unity_md.exists() else None,
            )

    while True:
        state = solve_state.load_state(paths.unity)
        stage = state.get("stage", "uninitialized")
        if stage in _TERMINAL:
            return state
        if stop_requested(paths.project_root):
            solve_state.set_outcome(paths.unity, "stopped", "Unity", "safe stop requested")
            return solve_state.load_state(paths.unity)
        if stage == "solving":
            await _run_solving(paths, roster)
        elif stage == "solution_review":
            await _run_solution_review(paths, roster)
        elif stage == "chunking":
            await _run_chunking(paths, roster)
        elif stage == "formalizing":
            await _run_formalizing(paths, roster)
        elif stage == "critic":
            await _run_critic(paths, roster)
        else:
            solve_state.set_outcome(
                paths.unity, "failed", "Unity", f"unsupported solve stage: {stage}"
            )


async def run_solve_retrospective(roster, paths) -> None:
    """Run the optional post-run retrospective against bounded solve state."""
    mark_phase("solve", "retrospective")
    await _turn(
        paths,
        roster,
        roster.primary,
        profile="retrospective",
        phase_prompt=load_prompt("solve/RETROSPECTIVE"),
        tools_prompt=load_prompt("SOLVE_RETROSPECTIVE_TOOLS"),
        task_prompt="Distill reusable lessons from the completed solve run without replaying raw logs.",
        cwd=paths.project_root,
    )
