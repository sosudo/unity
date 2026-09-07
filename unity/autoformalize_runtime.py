"""Event-driven formalization workers for ``unity autoformalize``.

The command owns phase order. This runtime launches cancellable workers, consumes
authoritative Forum events, and returns when formalization is quiescent or advances.
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

from . import artifacts, library, autoformalize_contract, autoformalize_jobs, autoformalize_state, worktree
from .autoformalize_input import require_source_matches
from .forum import autoformalize_server
from .autoformalize_orchestrator import _preamble, load_prompt, stop_requested
from .autoformalize_spawn import spawn


_console = Console()
PIPELINE = "autoformalize"


def configure_forum(paths, profile: str) -> None:
    autoformalize_server.configure(paths.forum, paths.project_root, profile)


def forum_brief(paths, profile: str, author: str) -> str:
    if os.getenv("UNITY_FORUM_BRIEF", "on").lower() == "off":
        return ""
    configure_forum(paths, profile)
    try:
        return autoformalize_server.autoformalize_brief(author)
    except Exception:
        return ""


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
                autoformalize_jobs.terminate, project_root, owner=agent.name,
            )


def _agent_runtime_env(
    paths, state: dict, agent_name: str, *, task_id: str = "",
) -> dict[str, str]:
    """Give autoformalization workers isolated, disposable temporary space."""
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
            bin_dir = paths.unity / "bin" / "autoformalize"
            bin_dir.mkdir(parents=True, exist_ok=True)
            wrapper = bin_dir / "lake"
            wrapper_source = (
                f"#!{sys.executable}\n"
                "from unity.autoformalize_lake_guard import main\n"
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
                "UNITY_AUTOFORMALIZE_PROJECT_ROOT": str(paths.project_root.resolve()),
                "UNITY_AUTOFORMALIZE_TASK_ID": task_id,
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


def write_formalization_plan(paths, candidate: dict) -> Path:
    """Mechanically scaffold the source identities the semantic DAG must cover."""
    source_refs = candidate["source_refs"]
    plan = {
        "solution_candidate": candidate["candidate_id"],
        "solution_sha256": candidate["sha256"],
        "source_refs": source_refs,
    }
    path = paths.unity / "formalization-plan.json"
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return path


def validate_formalization_dag(paths, expected_solution_sha: str) -> dict:
    """Validate the semantic chunker's DAG and its binding to supplied source bytes."""
    dag_path = paths.unity / "dag.json"
    try:
        dag = json.loads(dag_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("chunking did not produce a readable .unity/dag.json") from exc
    if not isinstance(dag, dict):
        raise ValueError("formalization DAG must be a JSON object")
    recorded = str(dag.get("solution_sha256") or dag.get("source_sha256") or "")
    if recorded != expected_solution_sha:
        raise ValueError("formalization DAG is not bound to the formalization input SHA-256")
    chunks = dag.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("formalization DAG requires a nonempty chunks list")
    if any(not isinstance(chunk, dict) for chunk in chunks):
        raise ValueError("each formalization chunk must be a JSON object")
    ids = [str(item.get("id") or "").strip() for item in chunks]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("formalization chunks require unique nonempty ids")
    try:
        plan = json.loads((paths.unity / "formalization-plan.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("chunking requires a readable formalization-plan.json") from exc
    if not isinstance(plan, dict):
        raise ValueError("formalization plan must be a JSON object")
    required_refs: set[str] = set()
    if plan is not None:
        if plan.get("solution_sha256") != expected_solution_sha:
            raise ValueError("formalization plan is not bound to the formalization input SHA-256")
        if dag.get("solution_candidate") != plan.get("solution_candidate"):
            raise ValueError("formalization DAG names the wrong supplied-source snapshot")
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


def _formal_worktree(project_root: Path, author: str) -> Path:
    """Reuse an owned registered tree; never let generic creation erase old work."""
    tree = worktree.agent_worktree(project_root, author)
    branch = worktree.agent_branch(author)
    if not tree.exists() and not tree.is_symlink():
        if _git(project_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
            raise ValueError(f"Preserved branch {branch} has no worktree; reconcile it before resuming autoformalize")
        return worktree.create_worktree(author, project_root)

    registered = _git(project_root, "worktree", "list", "--porcelain")
    top = _git(tree, "rev-parse", "--show-toplevel") if tree.is_dir() else None
    head = _git(tree, "symbolic-ref", "--short", "HEAD") if tree.is_dir() else None
    expected = f"worktree {tree.resolve()}\n"
    matching = any(record.startswith(expected) and f"branch refs/heads/{branch}" in record.splitlines()
                   for record in registered.stdout.split("\n\n"))
    root_common = _git(project_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    tree_common = _git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir") if tree.is_dir() else None
    if (tree.is_symlink() or registered.returncode or not matching or top is None or top.returncode
            or Path(top.stdout.strip()).resolve() != tree.resolve()
            or head is None or head.returncode or head.stdout.strip() != branch
            or root_common.returncode or tree_common is None or tree_common.returncode
            or root_common.stdout.strip() != tree_common.stdout.strip()):
        raise ValueError(f"Existing path {tree} is not the expected registered worktree for {author}; "
                         "preserved unchanged. Reconcile it before resuming autoformalize")
    return tree


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
                            formal_tasks: list[dict] | None = None,
                            layout: dict | None = None, environment: dict | None = None,
                            timings: dict | None = None) -> dict:
    issues = []
    expected = task.get("lean_decl", "")
    tasks = formal_tasks or [task]
    completed = {item["task_id"] for item in tasks
                 if item.get("status") == "complete" or item["task_id"] == task["task_id"]}
    try:
        check = autoformalize_contract.check_formal_contract(project_root, contract or {}, tasks,
                                                     completed=completed, layout=layout,
                                                     environment=environment, timings=timings)
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
        "issues": issues,
    }


def _apply_formal_candidate(paths, candidate: dict, task: dict, *, timings: dict | None = None) -> dict:
    """Apply, build, review, and commit one immutable formalization candidate."""
    root = paths.project_root
    current = autoformalize_state.load_state(paths.forum)
    require_source_matches(paths, current)
    contract = current["formalization"].get("contract", {})
    if not contract:
        return {"ok": False, "error": "missing formal contract; request re-chunking before proving"}
    if candidate.get("formalization_revision") != current["formalization"].get("revision"):
        return {"ok": False, "error": "candidate belongs to a superseded formal contract"}
    try:
        resolved = worktree.verify_candidate_commit(root, candidate["author"], candidate["commit_sha"])
    except Exception as exc:
        return {"ok": False, "error": f"candidate identity failed: {exc}"}
    diff_result = _git(
        root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index",
        candidate["base_main_sha"], resolved,
    )
    if diff_result.returncode:
        return {"ok": False, "error": "could not read cumulative candidate diff"}
    exact_diff = diff_result.stdout
    if hashlib.sha256(exact_diff.encode()).hexdigest() != candidate["diff_sha256"]:
        return {"ok": False, "error": "candidate commit no longer matches its submitted diff hash"}
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        return {"ok": False, "error": "main has tracked changes; refusing candidate merge"}
    before = worktree.main_commit(root)
    applied = subprocess.run(
        ["git", "apply", "--3way", "--index", "-"],
        cwd=root, input=exact_diff, capture_output=True, text=True, check=False,
    )
    if applied.returncode:
        _git(root, "reset", "--hard", before)
        return {"ok": False, "error": applied.stderr.strip() or "candidate conflicts with main"}
    with autoformalize_contract.measure(timings, "workspace_seconds"):
        layout = autoformalize_contract.workspace_layout(root)
    with autoformalize_contract.measure(timings, "initial_identity_seconds"):
        checked_source = autoformalize_contract.source_identity(root, layout=layout)
    checked_tree = _git(root, "write-tree").stdout.strip()
    build_started = time.monotonic()
    try:
        build = autoformalize_contract.build_sources(
            root, full=True, layout=layout, task_id=task["task_id"], timings=timings,
        )
    except OSError as exc:
        build_seconds = time.monotonic() - build_started
        _git(root, "reset", "--hard", before)
        return {
            "ok": False, "error": f"could not run lake build: {exc}",
            "build": {"returncode": None, "seconds": build_seconds},
        }
    build_seconds = time.monotonic() - build_started
    # Invalidation precedes BOTH build passes, whose complete duration/output is
    # recorded. Default targets alone need not include every inspected module.
    output = build["output"]
    build_record = {"returncode": build["returncode"], "seconds": build_seconds}
    if output:
        record = artifacts.store_text(
            paths.artifacts, output, kind="autoformalize_formal_build",
            source="lake build + explicit source modules", producer="Unity",
            metadata={"candidate_id": candidate["candidate_id"], "task_id": task["task_id"]},
        )
        build_record.update({"artifact_id": record["artifact_id"], "sha256": record["sha256"]})
    if build["returncode"]:
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
        layout=layout, environment=checked_source["environment"], timings=timings,
    )
    with autoformalize_contract.measure(timings, "postcheck_identity_seconds"):
        reviewed_source = autoformalize_contract.source_identity(root)
    if (reviewed_source != checked_source
            or _git(root, "write-tree").stdout.strip() != checked_tree):
        raise ValueError("source changed during candidate build or kernel inspection")
    verification["seconds"] = time.monotonic() - verification_started
    record = artifacts.store_text(
        paths.artifacts, json.dumps(verification, indent=2, sort_keys=True) + "\n",
        kind="autoformalize_formal_verification", producer="Unity",
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
    require_source_matches(paths, current)
    commit = _git(root, "commit", "-m", f"UNITY: merge {PIPELINE} task {task['task_id']}")
    if commit.returncode:
        _git(root, "reset", "--hard", before)
        return {"ok": False, "error": commit.stderr.strip() or "could not commit candidate"}
    with autoformalize_contract.measure(timings, "postcommit_identity_seconds"):
        committed_source = autoformalize_contract.source_identity(root)
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
    timings = {}
    started = time.monotonic()
    result = {}
    try:
        result = _apply_formal_candidate(paths, candidate, task, timings=timings)
    except (OSError, ValueError, KeyError) as exc:
        restored = _git(root, "reset", "--hard", before)
        suffix = "" if restored.returncode == 0 else "; main rollback also failed: " + restored.stderr
        result = {"ok": False, "error": f"candidate verification failed: {exc}{suffix}"}
    finally:
        timings["total_seconds"] = time.monotonic() - started
        # Detailed profiling is telemetry, never prompt memory or acceptance
        # evidence. Failure to write it must not undo a verified commit.
        try:
            artifact = artifacts.store_text(
                paths.artifacts, json.dumps(timings, sort_keys=True) + "\n",
                kind="autoformalize_formal_timings", producer="Unity",
                metadata={"candidate_id": candidate["candidate_id"], "task_id": task["task_id"]},
            )
            record = result.get("verification", result.get("build"))
            if isinstance(record, dict):
                record["timing_artifact_id"] = artifact["artifact_id"]
        except (OSError, ValueError):
            pass
    return result


def _integrate_formal_candidate(paths, candidate: dict, task: dict) -> dict:
    """Apply one candidate under the merge lock (also useful for integration tests)."""
    with _merge_lock(paths.project_root):
        return _integrate_checked(paths, candidate, task)


def _integrate_and_record(paths, candidate: dict, task: dict) -> dict:
    """Serialize Git integration AND state publication under the same lock."""
    with _merge_lock(paths.project_root):
        result = _integrate_checked(paths, candidate, task)
        autoformalize_state.finish_formal_merge(
            paths.forum, candidate["candidate_id"], success=bool(result.get("ok")),
            main_sha=result.get("main_sha", ""), error=result.get("error", ""),
            build=result.get("build"), verification=result.get("verification"),
        )
        return result


def recover_interrupted_formal_merges(paths) -> None:
    """Reopen clean interrupted merges; never discard ambiguous main changes."""
    with _merge_lock(paths.project_root):
        state = autoformalize_state.load_state(paths.forum)
        formal = state["formalization"]
        interrupted = [
            candidate for candidate in state["formal_candidates"].values()
            if state["phase"] == "formalizing"
            and candidate.get("status") == "merging"
            and candidate.get("formalization_revision") == formal.get("revision")
            and candidate.get("solution_candidate") == formal.get("solution_candidate")
        ]
        if not interrupted:
            return
        status = _git(
            paths.project_root, "status", "--porcelain", "--untracked-files=no",
        )
        if (
            status.returncode
            or status.stdout.strip()
            or worktree.main_commit(paths.project_root) != formal["main_sha"]
        ):
            raise ValueError(
                "Interrupted formal merge: main is dirty or differs from "
                f"the last accepted commit {formal['main_sha']}. "
                "Inspect and reconcile main before resuming. No changes were discarded."
            )
        for candidate in interrupted:
            autoformalize_state.finish_formal_merge(
                paths.forum, candidate["candidate_id"], success=False,
                error="Merge interrupted; task reopened for resubmission.",
            )


async def run_formalizing_runtime(roster, paths, mcp: dict, base_prompt: str) -> dict:
    """Swarm ready formal tasks and integrate immutable candidates using Forum events."""
    if stop_requested(paths.project_root):
        return autoformalize_state.load_state(paths.forum)
    configure_forum(paths, "formalizing")
    state = autoformalize_state.load_state(paths.forum)
    require_source_matches(paths, state)
    tools_prompt = load_prompt(f"{PIPELINE.upper()}_FORMALIZING_TOOLS")
    context = library.library_context()
    subagents = library.library_subagents()
    agents = {agent.name: agent for agent in roster.agents}
    agent_names = {autoformalize_state.author_key(name): name for name in agents}
    worktrees: dict[str, Path] = {}
    tasks: dict[str, asyncio.Task] = {}
    interrupts: dict[str, asyncio.Event] = {}
    worker_targets: dict[str, str] = {}
    blocked_launches: dict[str, str] = {}
    submission_nudges: set[tuple[str, str, str]] = set()
    state = autoformalize_state.load_state(paths.forum)
    seen = {
        event["event_id"] for event in state.get("events", [])
        if not (
            event.get("kind") == "formal_candidate_submitted"
            and state.get("formal_candidates", {}).get(
                event.get("candidate_id"), {}
            ).get("status") == "submitted"
        )
    }
    # Target notifications may arrive while verification runs in another thread.
    # Consume them separately: refreshing assignments must not consume candidates.
    target_events_seen = {event["event_id"] for event in state.get("events", [])}

    for agent in roster.agents:
        tree = _formal_worktree(paths.project_root, agent.name)
        worktree.symlink_lake_cache(tree, paths.project_root)
        worktree.link_runtime_state(tree, paths.project_root)
        worktrees[agent.name] = tree

    def participating_strategy(current: dict, name: str, task_id: str = "") -> dict | None:
        matches = [
            strategy for strategy in current.get("strategies", {}).values()
            if strategy.get("phase") == "formalizing"
            and strategy.get("phase_revision") == current["formalization"]["revision"]
            and strategy.get("status") == "claimed"
            and autoformalize_state.participates(strategy, name)
            and (not task_id or strategy.get("target") == task_id)
        ]
        return next((strategy for strategy in matches
                     if autoformalize_state.author_key(strategy.get("owner")) == autoformalize_state.author_key(name)),
                    matches[0] if matches else None)

    def refresh_worker_targets(current: dict) -> None:
        changed = set()
        for event in autoformalize_state.events_after(current, target_events_seen):
            target_events_seen.add(event["event_id"])
            if event.get("kind") not in {"strategy_registered", "strategy_claimed", "strategy_assisted"}:
                continue
            strategy = current.get("strategies", {}).get(event.get("strategy_id"), {})
            author = agent_names.get(autoformalize_state.author_key(event.get("author")))
            if (author and event.get("phase") == "formalizing"
                    and strategy.get("phase_revision") == current["formalization"]["revision"]):
                worker_targets[author] = event.get("target", "")
                changed.add(author)
        for name in changed:
            # Registering an alternative is not abandoning an owned strategy.
            # Paused participation also pins workers while a candidate is queued.
            unresolved = autoformalize_server.unresolved_formal_tasks(current, name)
            if unresolved and worker_targets.get(name) not in unresolved:
                strategy = participating_strategy(current, name)
                worker_targets[name] = strategy["target"] if strategy else unresolved[0]

    async def retire_completed_task(task_id: str) -> None:
        for name, running in list(tasks.items()):
            current = autoformalize_state.load_state(paths.forum)
            refresh_worker_targets(current)
            if worker_targets.get(name) != task_id:
                continue
            if autoformalize_server.unresolved_formal_tasks(current, name):
                continue
            await _cancel(
                agents[name], running, interrupts[name],
                f"formal task {task_id} completed", paths.project_root,
            )
            if running.done():
                tasks.pop(name, None)
                interrupts.pop(name, None)
        # Retain assignments, claims, and source. Obsolete completed-task work
        # is reset only when a stopped worker is assigned its next task.

    def worktree_changes(name: str) -> tuple[str, str]:
        status = _git(
            worktrees[name], "status", "--porcelain", "--untracked-files=all"
        ).stdout.strip()
        diff = _git(worktrees[name], "diff", "HEAD", "--binary").stdout
        return status, hashlib.sha256((status + "\n" + diff).encode()).hexdigest()

    def launch(name: str, task_id: str, followup: str = "") -> None:
        if name in tasks and not tasks[name].done():
            return
        current = autoformalize_state.load_state(paths.forum)
        formal_task = current["formal_tasks"].get(task_id)
        if (not formal_task or formal_task.get("status") != "pending"
                or autoformalize_server.has_pending_formal_candidate(current, name)):
            return
        prepared = autoformalize_server.prepare_formal_worktree(
            name, previous_task=worker_targets.get(name, ""), next_task=task_id,
            expected_revision=current["formalization"]["revision"],
        )
        if not prepared["ok"]:
            blocked_launches[name] = prepared.get("error", prepared.get("reason", "worktree unavailable"))
            _console.print(f"[yellow]preserving {name}'s worktree: {prepared.get('reason', '')}[/yellow]")
            return
        blocked_launches.pop(name, None)
        worker_targets[name] = task_id
        agent = agents[name]
        brief = forum_brief(paths, "formalizing", name)
        system = _preamble(agent, roster, icrl_enabled=False)
        if brief:
            system += f"\n{PIPELINE.capitalize()} workspace brief (refresh with autoformalize_brief):\n{brief}\n"
        system += base_prompt + "\n\n" + tools_prompt
        if context:
            system += "\n\n" + context
        strategy = participating_strategy(current, name, task_id)
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
            f"`{formal_task.get('lean_decl')}`. Its formalization source references are "
            f"{formal_task.get('source_components', [])}. Dependencies have already been integrated. "
            "Refresh autoformalize_brief. " + strategy_instruction +
            "Edit in your worktree using MCP tools while iterating: prefer compatible Axle tools "
            "when enabled over equivalent Lean LSP tools, and Lean LSP for local goals and diagnostics. "
            "Use direct shell checks only as a fallback or when compiled artifacts are needed. "
            "When the implementation is ready, call `finalize_formalization`; Unity will commit the "
            "exact source and perform the sole authoritative full build in main. Publish useful Lean/API findings "
            "as you work. Supplied documents are read-only. Report source defects as obstacles; "
            "do not change the source or silently formalize a different result. "
            "Request re-chunking for encoding errors."
        ))
        event = asyncio.Event()
        interrupts[name] = event
        tasks[name] = asyncio.create_task(
            spawn(
                agent, system, task_prompt, worktrees[name], mcp,
                subagents=subagents, interrupt_event=event,
                log_context={
                    "command": PIPELINE, "run_id": current.get("run_id"), "phase": "formalizing",
                    "task_id": task_id, "role": "formalizer",
                },
                env_overrides=_agent_runtime_env(paths, current, name, task_id=task_id),
                own_process_group=True,
                mcp_profile="autoformalize",
            ),
            name=f"{PIPELINE}:formalizing:{name}:{task_id}",
        )

    def launch_idle() -> None:
        current = autoformalize_state.load_state(paths.forum)
        refresh_worker_targets(current)
        ready = autoformalize_state.ready_formal_tasks(current)
        if not ready:
            return
        idle = [name for name in agents if name not in tasks]
        ready_ids = {formal_task["task_id"] for formal_task in ready}
        unassigned = []
        for name in idle:
            if autoformalize_server.has_pending_formal_candidate(current, name):
                continue
            previous = worker_targets.get(name, "")
            # Unregistered edits are work too. Keep their target rather than
            # assigning the worker to a different ready task and resetting it.
            if current["formal_tasks"].get(previous, {}).get("status") == "pending":
                if previous in ready_ids:
                    launch(name, previous)
                continue
            strategy = participating_strategy(current, name)
            if strategy and strategy.get("target") in ready_ids:
                if not previous:
                    # Existing claims/assistance on runtime entry are resumes,
                    # not a request to abandon unresolved work for a new task.
                    worker_targets[name] = strategy["target"]
                launch(name, strategy["target"])
                continue
            if autoformalize_server.unresolved_formal_tasks(current, name):
                continue  # Includes assistants paused for somebody else's candidate.
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
            state = autoformalize_state.load_state(paths.forum)
            refresh_worker_targets(state)
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
                current = autoformalize_state.load_state(paths.forum)
                task_id = worker_targets.get(name, "")
                formal_task = current.get("formal_tasks", {}).get(task_id, {})
                dirty, digest = worktree_changes(name)
                strategy = participating_strategy(current, name, task_id)
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

            state = autoformalize_state.load_state(paths.forum)
            events = autoformalize_state.events_after(state, seen)
            for event in events:
                seen.add(event["event_id"])
                kind = event.get("kind")
                if kind != "formal_candidate_submitted":
                    continue
                candidate_id = event["candidate_id"]
                current = autoformalize_state.load_state(paths.forum)
                refresh_worker_targets(current)
                candidate = current["formal_candidates"].get(candidate_id, {})
                if candidate.get("status") != "submitted":
                    continue
                task_id = candidate["task_id"]
                affected = [
                    name for name, task in tasks.items()
                    if not task.done() and (
                        worker_targets.get(name) == task_id
                        or autoformalize_state.author_key(name) == autoformalize_state.author_key(candidate["author"])
                    )
                ]
                await asyncio.gather(*(
                    _cancel(agents[name], tasks[name], interrupts[name],
                            f"formal candidate {candidate_id} submitted for {task_id}",
                            paths.project_root)
                    for name in affected
                ))
                for name in affected:
                    if tasks[name].done():
                        tasks.pop(name, None)
                        interrupts.pop(name, None)
                started = autoformalize_state.begin_formal_merge(paths.forum, candidate_id)
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
                    await retire_completed_task(task_id)
                else:
                    # A changed supplied input cannot be repaired by retrying
                    # Lean candidates against the now-obsolete source binding.
                    require_source_matches(paths, autoformalize_state.load_state(paths.forum))
                    _console.print(f"[red]candidate {candidate_id} failed: {result.get('error', '')}[/red]")

            state = autoformalize_state.load_state(paths.forum)
            if autoformalize_state.all_formal_tasks_complete(state):
                return state
            launch_idle()
            state = autoformalize_state.load_state(paths.forum)
            if not tasks and not autoformalize_server.has_pending_formal_candidate(state):
                if blocked_launches:
                    details = "; ".join(f"{name}: {reason}" for name, reason in blocked_launches.items())
                    raise ValueError("Autoformalize cannot launch workers without discarding preserved work. "
                                     "Reconcile these worktrees before resuming: " + details)
                return state
        return autoformalize_state.load_state(paths.forum)
    finally:
        await asyncio.gather(*(
            _cancel(agents[name], task, interrupts[name], "formalization runtime ending",
                    paths.project_root)
            for name, task in list(tasks.items())
        ))
        await asyncio.to_thread(autoformalize_jobs.terminate, paths.project_root)
        final_state = autoformalize_state.load_state(paths.forum)
        for agent in roster.agents:
            running = tasks.get(agent.name)
            if running is not None and not running.done():
                _console.print(f"[red]worker {agent.name} has not stopped; preserving its worktree[/red]")
                continue
            if not autoformalize_state.all_formal_tasks_complete(final_state):
                continue  # Retain unfinished source, claims and candidate ancestry for --continue.
            autoformalize_state.release_author_claims(
                paths.forum, agent.name, "formalization runtime ended"
            )
            tree = worktrees.get(agent.name)
            if tree is not None:
                worktree.cleanup_worktree(agent.name, tree, paths.project_root)
