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
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from threading import Event

from rich.console import Console

from . import artifacts, library, autoformalize_contract, autoformalize_jobs, autoformalize_state, worktree
from .autoformalize_input import require_source_matches
from .autoformalize_diagnostics import failure_excerpt
from .forum import autoformalize_server
from .autoformalize_orchestrator import _preamble, load_prompt, stop_requested
from .autoformalize_spawn import spawn
from .autoformalize_worktree_guard import WorkspaceGuard, WorkspaceContamination


_console = Console()
PIPELINE = "autoformalize"
_workspace_guard: ContextVar[WorkspaceGuard | None] = ContextVar("autoformalize_workspace_guard", default=None)


@contextmanager
def _guard_scope(guard: WorkspaceGuard | None):
    token = _workspace_guard.set(guard)
    try:
        yield
    finally:
        _workspace_guard.reset(token)


def _check_workspace() -> None:
    guard = _workspace_guard.get()
    if guard is not None:
        guard.assert_expected()


@contextmanager
def _main_write():
    """Only short controller mutations exclude scans, never an entire review."""
    guard = _workspace_guard.get()
    with guard.unity_write() if guard is not None else nullcontext():
        _check_workspace()
        yield guard


def configure_forum(paths, profile: str) -> None:
    autoformalize_server.configure(paths.forum, paths.project_root, profile)


def forum_brief(paths, profile: str, author: str, task_id: str = "") -> str:
    if os.getenv("UNITY_FORUM_BRIEF", "on").lower() == "off":
        return ""
    configure_forum(paths, profile)
    try:
        return autoformalize_server.autoformalize_brief(author, task_id=task_id)
    except Exception:
        return ""


_CANCEL_GRACE_SECONDS = 20.0
_CANCEL_HARD_SECONDS = 10.0
_WORKSPACE_EVIDENCE_GRACE_SECONDS = 2.0


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
        "UNITY_AUTOFORMALIZE_PROJECT_ROOT": str(paths.project_root.resolve()),
        "UNITY_AUTOFORMALIZE_TASK_ID": task_id,
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
    state = autoformalize_state.load_state(paths.forum)
    replan = state.get("replan") or {}
    if replan:
        previous = replan.get("previous_formalization") or {}
        plan["replan"] = {
            "request": replan.get("request"), "previous_tasks": replan.get("previous_tasks", {}),
            "previous_formalization": {"spec": previous.get("spec"),
                                       "requirements": previous.get("requirements", [])},
        }
    plan["source_issues"] = {
        key: {field: row.get(field) for field in (
            "issue_id", "anchor_ids", "task_ids", "description", "status", "repair_ids",
            "source_candidate", "source_sha256", "reason",
        )}
        for key, row in state.get("source_issues", {}).items()
    }
    plan["source_repairs"] = state.get("source_repairs", {})
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
    from .autoformalize_spec import normalize_requirements, normalize_spec, normalize_informal_nodes
    source = {"candidate_id": plan["solution_candidate"], "sha256": plan["solution_sha256"],
              "source_refs": plan["source_refs"]}
    dag["requirements"] = normalize_requirements(dag.get("requirements"), chunks, required_refs)
    dag["spec"] = normalize_spec(dag.get("spec"), source=source,
                                 requirements=dag["requirements"], tasks=chunks,
                                 allow_unresolved=True)
    nodes = normalize_informal_nodes(chunks, dag["requirements"], dag["spec"], source)
    dag["chunks"] = list(nodes.values())
    dag["solution_sha256"] = expected_solution_sha
    return dag


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return autoformalize_jobs.run(
        project, ["git", *args], cwd=project, owner="Unity", task_id="integration-git",
    )


class MainWorkspaceContaminationError(ValueError):
    """Unsafe integration state: stop the run instead of retrying proof workers."""


def _assert_main_inputs_tracked(root: Path, *, candidate_paths: list[str] | None = None) -> None:
    indexed = _git(root, "ls-files", "-z")
    if indexed.returncode:
        raise MainWorkspaceContaminationError("Cannot inspect main's index; reconcile main before resuming")
    tracked = set(indexed.stdout.split("\0"))
    try:
        unknown = {str(path.relative_to(root)) for path in autoformalize_contract.source_files(root)
                   if str(path.relative_to(root)) not in tracked}
    except (OSError, ValueError) as exc:
        raise MainWorkspaceContaminationError(
            "Cannot safely inspect main source inputs; reconcile main before resuming. Files were preserved."
        ) from exc
    if unknown:
        # Custom Lake build directories are disposable outputs, not source. Only
        # discover the layout on this exceptional path; clean merges pay no extra
        # Lean invocation. A broken layout cannot make unknown source safe.
        try:
            layout = autoformalize_contract.workspace_layout(root)
            build_dir = layout.get("build_dir")
            if build_dir:
                unknown = {str(path.relative_to(root))
                           for path in autoformalize_contract.source_files(root, build_dir=build_dir)
                           if str(path.relative_to(root)) not in tracked}
        except (OSError, ValueError):
            pass
    collisions = {name for name in candidate_paths or []
                  if name not in tracked and os.path.lexists(root / name)}
    if unknown or collisions:
        names = ", ".join(sorted(unknown | collisions)[:20])
        raise MainWorkspaceContaminationError(
            "Main has untracked build inputs or candidate-path collisions: " + names
            + ". Files were preserved. Inspect and reconcile main before resuming; "
              "agents must edit only their assigned worktrees."
        )


def candidate_retry_context(root: Path, contract: dict, *, layout: dict | None = None) -> dict:
    """Fingerprint actual inputs, not merely HEAD; called under the merge lock."""
    identity = autoformalize_contract.source_identity(root, layout=layout)
    return {"main_sha": identity["main_sha"], "contract_sha256": contract.get("sha256", ""),
            "source_sha256": identity["source_sha256"],
            "environment_sha256": autoformalize_contract.digest(identity["environment"])}


def _is_patch_conflict(result: subprocess.CompletedProcess) -> bool:
    """Conservative negative-cache classification, never an acceptance check."""
    if result.returncode != 1:
        return False
    lines = [line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()]
    conflict = r"(?:error: patch failed: .+:\d+|error: .+: patch does not apply|Applied patch to '.+' with conflicts\.)"
    progress = r"(?:Performing three-way merge\.\.\.|Falling back to direct application\.\.\.|Applied patch to '.+' cleanly\.|U .+)"
    return (any(re.fullmatch(conflict, line) for line in lines)
            and all(re.fullmatch(conflict, line) or re.fullmatch(progress, line) for line in lines))


def _rollback(root: Path, before: str) -> subprocess.CompletedProcess:
    with autoformalize_jobs.cancellation_disabled(), _main_write() as guard:
        result = _git(root, "reset", "--hard", before)
        if result.returncode:
            raise MainWorkspaceContaminationError("Main rollback failed; inspect and reconcile main before resuming: "
                             + (result.stderr.strip() or result.stdout.strip() or "git reset failed"))
        if guard is not None:
            guard.expect_git_index(expected_head=before)
        _assert_main_inputs_tracked(root)
    return result


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
        while True:
            autoformalize_jobs.check_cancelled()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _review_new_declaration(project_root: Path, task: dict, diff: str, *,
                            contract: dict | None = None,
                            formal_tasks: list[dict] | None = None,
                            layout: dict | None = None, environment: dict | None = None,
                            timings: dict | None = None, candidate: dict | None = None) -> dict:
    issues = []
    expected = task.get("lean_decl", "")
    tasks = formal_tasks or [task]
    stage = (candidate or {}).get("stage", "complete")
    completed = {item["task_id"] for item in tasks
                 if item.get("status") == "complete"
                 or (item["task_id"] == task["task_id"] and stage == "complete")}
    incremental = {}
    if (contract or {}).get("version") == 3:
        incremental = {"task_id": task["task_id"], "stage": stage,
                       "proposed_outputs": (candidate or {}).get("outputs"),
                       "final": len(completed) == len(tasks)}
    try:
        check = autoformalize_contract.check_formal_contract(project_root, contract or {}, tasks,
                                                     completed=completed, layout=layout,
                                                     environment=environment, timings=timings,
                                                     **incremental)
    except (OSError, ValueError) as exc:
        check = {"passed": False, "issues": [f"formal contract verification unavailable: {exc}"]}
    issues.extend(check["issues"])
    return {
        "status": "passed" if not issues else "failed",
        "expected_decl": expected,
        "source_components": list(task.get("source_components", [])),
        "mode": "formal_contract",
        "contract_sha256": check.get("proposed_contract", contract or {}).get("sha256"),
        "stage": stage,
        "verified_tasks": check.get("verified_tasks", sorted(completed)),
        **{key: check[key] for key in ("proposed_contract", "verified_targets", "final") if key in check},
        "issues": issues,
        # Empty inspection results also represent unavailable/crashed inspectors;
        # do not turn those failures into persistent negative cache entries.
        "deterministic_failure": bool(check.get("targets")),
    }


def _checked_tree(root: Path, revision: str | None = None) -> str:
    """Read a committed or index tree, failing closed on Git errors."""
    result = (_git(root, "rev-parse", f"{revision}^{{tree}}") if revision is not None
              else _git(root, "write-tree"))
    if result.returncode or not result.stdout.strip():
        raise ValueError(result.stderr.strip() or "could not read candidate source tree")
    return result.stdout.strip()


def _apply_formal_candidate(paths, candidate: dict, task: dict, *, timings: dict | None = None) -> dict:
    """Verify an immutable candidate; commit only if its integration changes main."""
    root = paths.project_root
    _check_workspace()
    current = autoformalize_state.load_state(paths.forum)
    require_source_matches(paths, current)
    contract = current["formalization"].get("contract", {})
    if not contract:
        return {"ok": False, "error": "missing formal contract; request re-chunking before proving"}
    if not autoformalize_state.candidate_is_current(current, candidate):
        return {"ok": False, "error": "candidate belongs to a superseded formal contract"}
    try:
        resolved = worktree.verify_candidate_commit(
            root, candidate["author"], candidate["commit_sha"], allow_unchanged=True,
        )
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
    changed_paths = _git(root, "diff", "--name-only", "-z", candidate["base_main_sha"], resolved)
    if changed_paths.returncode:
        return {"ok": False, "error": "could not inspect candidate paths"}
    _assert_main_inputs_tracked(root, candidate_paths=[name for name in changed_paths.stdout.split("\0") if name])
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        return {"ok": False, "error": "main has tracked changes; refusing candidate merge"}
    before = worktree.main_commit(root)
    before_tree = _checked_tree(root, before)
    if exact_diff:
        with _main_write() as guard:
            if guard is not None:
                # Conflicts must not leave an unmerged index whose conflict-marker
                # bytes cannot be distinguished from an unauthorized worker edit.
                checked = autoformalize_jobs.run(
                    root, ["git", "apply", "--check", "--3way", "--index", "-"],
                    cwd=root, input=exact_diff, owner="Unity", task_id=task["task_id"],
                )
                guard.assert_expected()
                # --check --3way can exit zero while predicting conflicts. Do
                # not perform that known-conflicting write against live main.
                predicted_conflict = bool(re.search(r"(?m)^Applied patch to '.+' with conflicts\.$", checked.stderr))
                if checked.returncode or predicted_conflict:
                    return {"ok": False, "error": checked.stderr.strip() or "candidate conflicts with main",
                            "failure_kind": "merge_conflict" if predicted_conflict or _is_patch_conflict(checked) else ""}
            # Drain the short mutation before honoring cancellation; otherwise a
            # half-applied patch cannot safely become a recovery baseline.
            with autoformalize_jobs.cancellation_disabled():
                applied = autoformalize_jobs.run(
                    root,
                    ["git", "apply", "--3way", "--index", "-"],
                    cwd=root, input=exact_diff, owner="Unity", task_id=task["task_id"],
                )
                if guard is not None:
                    guard.expect_git_index()
        if applied.returncode:
            _rollback(root, before)
            return {"ok": False, "error": applied.stderr.strip() or "candidate conflicts with main",
                    "failure_kind": "merge_conflict" if _is_patch_conflict(applied) else ""}
    elif _checked_tree(root, resolved) != before_tree:
        return {"ok": False, "error": "Main differs from this empty candidate; sync_from_main and resubmit."}
    with autoformalize_contract.measure(timings, "workspace_seconds"):
        layout = autoformalize_contract.workspace_layout(root)
    if (guard := _workspace_guard.get()) is not None:
        guard.set_build_dir(layout.get("build_dir"))
        guard.assert_expected()
    with autoformalize_contract.measure(timings, "initial_identity_seconds"):
        checked_source = autoformalize_contract.source_identity(root, layout=layout)
    checked_tree = _checked_tree(root)
    no_tree_change = checked_tree == before_tree
    if not exact_diff and not no_tree_change:
        raise ValueError("source tree changed before empty candidate verification")
    if checked_source["main_sha"] != before:
        raise ValueError("main changed before candidate verification")
    build_started = time.monotonic()
    try:
        build = autoformalize_contract.build_sources(
            root, full=True, layout=layout, task_id=task["task_id"], timings=timings,
        )
    except OSError as exc:
        build_seconds = time.monotonic() - build_started
        _rollback(root, before)
        return {
            "ok": False, "error": f"could not run lake build: {exc}",
            "build": {"returncode": None, "seconds": build_seconds},
        }
    build_seconds = time.monotonic() - build_started
    _check_workspace()
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
        _rollback(root, before)
        return {
            "ok": False,
            "error": "lake build failed: " + failure_excerpt(output, 3000),
            "build": build_record,
            # Cache actual Lean diagnostics, not exit-1 transport/tool failures.
            "failure_kind": "build_failed" if build["returncode"] == 1 and re.search(
                r"(?m)(?:^error: [^\n]*\.lean:\d+:\d+:|^[^\n]*\.lean:\d+:\d+: error:)", output,
            ) else "",
            "failure_environment_sha256": autoformalize_contract.digest(checked_source["environment"]),
        }
    if _git(root, "diff", "--quiet").returncode:
        _rollback(root, before)
        return {"ok": False, "error": "lake build changed tracked files", "build": build_record}
    staged = _git(root, "diff", "--cached", "--no-ext-diff", before).stdout
    verification_started = time.monotonic()
    verification = _review_new_declaration(
        root, task, staged, contract=contract,
        formal_tasks=list(current["formal_tasks"].values()),
        layout=layout, environment=checked_source["environment"], timings=timings,
        candidate=candidate,
    )
    _check_workspace()
    with autoformalize_contract.measure(timings, "postcheck_identity_seconds"):
        reviewed_source = autoformalize_contract.source_identity(root)
    if (reviewed_source != checked_source
            or _checked_tree(root) != checked_tree):
        raise ValueError("source changed during candidate build or kernel inspection")
    verification["seconds"] = time.monotonic() - verification_started
    record = artifacts.store_text(
        paths.artifacts, json.dumps(verification, indent=2, sort_keys=True) + "\n",
        kind="autoformalize_formal_verification", producer="Unity",
        source=f"formal task {task['task_id']}",
    )
    verification["artifact_id"] = record["artifact_id"]
    if verification["status"] != "passed":
        _rollback(root, before)
        return {
            "ok": False,
            "error": "; ".join(verification["issues"]),
            "build": build_record,
            "verification": verification,
            "failure_kind": "contract_failed" if verification.get("deterministic_failure") else "",
            "failure_environment_sha256": autoformalize_contract.digest(checked_source["environment"]),
        }
    require_source_matches(paths, current)
    if not no_tree_change:
        with autoformalize_jobs.cancellation_disabled(), _main_write() as guard:
            commit = _git(root, "commit", "-m", f"UNITY: merge {PIPELINE} task {task['task_id']}")
            if guard is not None:
                guard.expect_git_index(expected_head=worktree.main_commit(root) if not commit.returncode else before)
        if commit.returncode:
            _rollback(root, before)
            return {"ok": False, "error": commit.stderr.strip() or "could not commit candidate"}
    with autoformalize_contract.measure(timings, "postcommit_identity_seconds"):
        committed_source = autoformalize_contract.source_identity(root)
    expected_source = (checked_source if no_tree_change
                       else {**checked_source, "main_sha": committed_source["main_sha"]})
    if (committed_source != expected_source
            or _checked_tree(root, "HEAD") != checked_tree
            or _checked_tree(root) != checked_tree):
        raise ValueError("commit changed the verified candidate source")
    _check_workspace()
    verification["source_identity"] = committed_source
    return {
        "ok": True,
        "main_sha": committed_source["main_sha"],
        "build": build_record,
        "verification": verification,
    }


def _integrate_checked(paths, candidate: dict, task: dict) -> dict:
    root = paths.project_root
    _assert_main_inputs_tracked(root)
    # Nothing below may discard pre-existing tracked edits.
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        return {"ok": False, "error": "main has tracked changes; refusing candidate merge"}
    before = worktree.main_commit(root)
    timings = {}
    started = time.monotonic()
    result = {}
    try:
        current = autoformalize_state.load_state(paths.forum)
        contract = current["formalization"].get("contract") or {}
        lookup = {"task_id": candidate["task_id"], "base_main_sha": candidate["base_main_sha"],
                  "diff_sha256": candidate["diff_sha256"], "stage": candidate.get("stage", "complete"),
                  "outputs": candidate.get("outputs", [])}
        prior = autoformalize_state.matching_failed_candidate(current, **lookup)
        if prior:
            context = candidate_retry_context(root, contract)
            prior = autoformalize_state.matching_failed_candidate(current, **lookup, retry_context=context)
        if prior:
            result = {"ok": False, "error": prior.get("error", "unchanged failed candidate"),
                      "failure_kind": prior["failure_kind"], "failure_context": context,
                      **{key: dict(prior[key]) for key in ("build", "verification")
                         if isinstance(prior.get(key), dict)},
                      "unchanged_failed": prior["candidate_id"]}
        else:
            baseline_files = autoformalize_contract._file_hashes(root)
            result = _apply_formal_candidate(paths, candidate, task, timings=timings)
        autoformalize_jobs.check_cancelled()
        if result.get("failure_kind") and not result.get("failure_context"):
            # The failed integration has rolled back. Cache only the actual clean
            # baseline, with dependencies and non-Lean inputs included. A failed
            # fingerprint disables caching, never proof verification.
            _assert_main_inputs_tracked(root)
            try:
                context = candidate_retry_context(root, contract)
                if (baseline_files == autoformalize_contract._file_hashes(root)
                        and (not result.get("failure_environment_sha256")
                             or result["failure_environment_sha256"] == context["environment_sha256"])):
                    result["failure_context"] = context
            except autoformalize_jobs.JobCancelled:
                raise
            except (OSError, ValueError, KeyError):
                pass
    except MainWorkspaceContaminationError:
        raise
    except (OSError, ValueError, KeyError) as exc:
        _rollback(root, before)
        result = {"ok": False, "error": f"candidate verification failed: {exc}",
                  "cancelled": isinstance(exc, autoformalize_jobs.JobCancelled)}
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


def _integrate_formal_candidate(paths, candidate: dict, task: dict, *, guard: WorkspaceGuard | None = None) -> dict:
    """Apply one candidate under the merge lock (also useful for integration tests)."""
    with _merge_lock(paths.project_root), _guard_scope(guard):
        return _integrate_checked(paths, candidate, task)


def _integrate_and_record(paths, candidate: dict, task: dict, cancel_event: Event | None = None,
                          guard: WorkspaceGuard | None = None) -> dict:
    """Serialize Git integration AND state publication under the same lock."""
    def record(result: dict) -> dict:
        if result.get("cancelled") and autoformalize_state.pending_replan(autoformalize_state.load_state(paths.forum)):
            autoformalize_state.defer_formal_merge(paths.forum, candidate["candidate_id"], reason=result["error"])
            return {**result, "deferred": True}
        autoformalize_state.finish_formal_merge(
            paths.forum, candidate["candidate_id"], success=bool(result.get("ok")),
            main_sha=result.get("main_sha", ""), error=result.get("error", ""),
            build=result.get("build"), verification=result.get("verification"),
            failure_context=result.get("failure_context"), failure_kind=result.get("failure_kind", ""),
        )
        return result

    def cancelled_result(exc: autoformalize_jobs.JobCancelled, before: str | None = None) -> dict:
        # The workspace guard also signals cancellation. That interruption is
        # not a failed proof, even if it arrived before acquiring merge.lock.
        incident = guard.poll_due(force=True) if guard is not None else None
        if incident is not None:
            return {"ok": False, "workspace_recovery": True,
                    "incident": incident, "rollback_sha": before,
                    "candidate_id": candidate["candidate_id"], "error": incident.message}
        return record({"ok": False, "cancelled": True, "error": str(exc)})

    with autoformalize_jobs.cancellation_scope(cancel_event), _guard_scope(guard):
        try:
            with _merge_lock(paths.project_root):
                before = None
                try:
                    if guard is not None:
                        guard.assert_expected()
                        before = worktree.main_commit(paths.project_root)
                    result = _integrate_checked(paths, candidate, task)
                    _check_workspace()
                except WorkspaceContamination as exc:
                    # No rollback or proof-failure record until the writer is
                    # stopped and its exact bytes have been safely preserved.
                    return {"ok": False, "workspace_recovery": True,
                            "incident": exc.incident, "rollback_sha": before,
                            "candidate_id": candidate["candidate_id"], "error": str(exc)}
                except autoformalize_jobs.JobCancelled as exc:
                    return cancelled_result(exc, before)
                return record(result)
        except autoformalize_jobs.JobCancelled as exc:
            # Cancellation before acquiring the lock made no source mutation.
            return cancelled_result(exc)


def _recover_main_workspace(paths, guard: WorkspaceGuard, recovery: dict) -> None:
    """Called only after the offending worker and any integration have drained."""
    with autoformalize_jobs.cancellation_disabled(), _merge_lock(paths.project_root), _guard_scope(guard):
        guard.recover(recovery["incident"])
        if recovery.get("rollback_sha"):
            _rollback(paths.project_root, recovery["rollback_sha"])
        guard.assert_expected()
        if recovery.get("candidate_id"):
            # Leave it 'merging' until recovery AND rollback actually succeeded.
            autoformalize_state.defer_formal_merge(
                paths.forum, recovery["candidate_id"], reason="Workspace contamination recovered; retry verification",
            )


def recover_interrupted_formal_merges(paths) -> None:
    """Reopen clean interrupted merges; never discard ambiguous main changes."""
    with _merge_lock(paths.project_root):
        _assert_main_inputs_tracked(paths.project_root)
        state = autoformalize_state.load_state(paths.forum)
        formal = state["formalization"]
        interrupted = [
            candidate for candidate in state["formal_candidates"].values()
            if state["phase"] == "formalizing"
            and candidate.get("status") == "merging"
            and autoformalize_state.candidate_is_current(state, candidate)
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
    with _merge_lock(paths.project_root):
        _assert_main_inputs_tracked(paths.project_root)
    tools_prompt = load_prompt(f"{PIPELINE.upper()}_FORMALIZING_TOOLS")
    context = library.library_context()
    subagents = library.library_subagents()
    agents = {agent.name: agent for agent in roster.agents}
    agent_names = {autoformalize_state.author_key(name): name for name in agents}
    worktrees: dict[str, Path] = {}
    tasks: dict[str, asyncio.Task] = {}
    stopping: dict[str, asyncio.Task] = {}
    roles: dict[str, str] = {}
    repair_issues: dict[str, str] = {}
    repair_exhausted: dict[str, set[str]] = {}
    integration: asyncio.Task | None = None
    integration_candidate: dict = {}
    integration_cancel: Event | None = None
    recovery: dict | None = None
    recovery_notices: dict[str, str] = {}
    workspace_unsafe = False
    interrupts: dict[str, asyncio.Event] = {}
    worker_targets: dict[str, str] = {}
    worker_revisions: dict[str, tuple[str, int]] = {}
    blocked_launches: dict[str, str] = {}
    submission_nudges: set[tuple[str, str, str]] = set()
    state = autoformalize_state.load_state(paths.forum)
    # Target notifications may arrive while verification runs in another thread.
    # Consume them separately: refreshing assignments must not consume candidates.
    target_events_seen = {event["event_id"] for event in state.get("events", [])}

    for agent in roster.agents:
        tree = _formal_worktree(paths.project_root, agent.name)
        worktree.symlink_lake_cache(tree, paths.project_root)
        worktree.link_runtime_state(tree, paths.project_root)
        worktrees[agent.name] = tree

    guard = WorkspaceGuard(paths.project_root, paths.artifacts, worktrees=worktrees)
    with _merge_lock(paths.project_root):
        guard.capture_baseline()

    def participating_strategy(current: dict, name: str, task_id: str = "") -> dict | None:
        matches = [
            strategy for strategy in current.get("strategies", {}).values()
            if strategy.get("phase") == "formalizing"
            and autoformalize_state.strategy_is_current(current, strategy)
            and strategy.get("status") == "claimed"
            and autoformalize_state.participates(strategy, name)
            and (not task_id or strategy.get("target") == task_id)
        ]
        return next((strategy for strategy in matches
                     if autoformalize_state.author_key(strategy.get("owner")) == autoformalize_state.author_key(name)),
                    matches[0] if matches else None)

    def refresh_worker_targets(current: dict) -> None:
        for event in autoformalize_state.events_after(current, target_events_seen):
            target_events_seen.add(event["event_id"])
            if event.get("kind") not in {"strategy_registered", "strategy_claimed", "strategy_assisted"}:
                continue
            strategy = current.get("strategies", {}).get(event.get("strategy_id"), {})
            author = agent_names.get(autoformalize_state.author_key(event.get("author")))
            if (author and event.get("phase") == "formalizing"
                    and autoformalize_state.strategy_is_current(current, strategy)):
                worker_targets[author] = event.get("target", "")
        for name in agents:
            # Registering an alternative is not abandoning an owned strategy.
            # Paused participation also pins workers while a candidate is queued.
            unresolved = autoformalize_server.unresolved_formal_tasks(current, name)
            if unresolved and worker_targets.get(name) not in unresolved:
                strategy = participating_strategy(current, name)
                worker_targets[name] = strategy["target"] if strategy else unresolved[0]

    def request_stop(name: str, reason: str) -> None:
        running = tasks.get(name)
        if running is not None and not running.done() and name not in stopping:
            stopping[name] = asyncio.create_task(
                _cancel(agents[name], running, interrupts[name], reason, paths.project_root),
                name=f"autoformalize:stop:{name}",
            )

    def record_recovery(incident, details: dict | None = None) -> None:
        nonlocal recovery
        if recovery is None:
            # Filesystem writes can precede their native tool notifications.
            # This bounds notification delivery, not the worker's stop/drain.
            recovery = {"evidence_deadline": time.monotonic() + _WORKSPACE_EVIDENCE_GRACE_SECONDS}
        if details:
            recovery.update({key: value for key, value in details.items() if key != "incident"})
        recovery["incident"] = incident
        if integration_cancel is not None:
            integration_cancel.set()
        if incident.author in agents:
            request_stop(incident.author, incident.message)

    def retire_completed_task(task_id: str) -> None:
        for name, running in list(tasks.items()):
            current = autoformalize_state.load_state(paths.forum)
            refresh_worker_targets(current)
            if worker_targets.get(name) != task_id:
                continue
            if autoformalize_server.unresolved_formal_tasks(current, name):
                continue
            if roles.get(name) == "formalizing":
                request_stop(name, f"formal task {task_id} completed")
        # Retain assignments, claims, and source. Obsolete completed-task work
        # is reset only when a stopped worker is assigned its next task.

    def worktree_changes(name: str) -> tuple[str, str]:
        status = _git(
            worktrees[name], "status", "--porcelain", "--untracked-files=all"
        ).stdout.strip()
        diff = _git(worktrees[name], "diff", "HEAD", "--binary").stdout
        return status, hashlib.sha256((status + "\n" + diff).encode()).hexdigest()

    def launch(name: str, task_id: str, followup: str = "") -> None:
        if stop_requested(paths.project_root) or integration is not None or recovery is not None or name in stopping:
            return  # Worktree preparation takes merge.lock; never block this event loop on a review.
        if name in tasks and not tasks[name].done():
            return
        current = autoformalize_state.load_state(paths.forum)
        formal_task = current["formal_tasks"].get(task_id)
        if (not formal_task or not autoformalize_state.task_ready(current, formal_task)
                or autoformalize_server.has_pending_formal_candidate(current, name)
                or autoformalize_state.source_issues_blocking_task(current, task_id)):
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
        worker_revisions[name] = (task_id, formal_task.get("revision", 0))
        agent = agents[name]
        brief = forum_brief(paths, "formalizing", name, task_id=task_id)
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
        if prepared.get("sync_warning"):
            resume += prepared["sync_warning"] + " "
        strategy_instruction = (
            "Continue the claimed strategy for this task. "
            if strategy else
            "Claim a suitable existing unclaimed strategy, or register one only when your approach "
            "is materially different. You may investigate or edit before registering, but claim a "
            "strategy before finalizing. "
        )
        task_prompt = recovery_notices.pop(name, "") + resume + (followup or (
            f"Your current formalization target is task `{task_id}`: "
            f"{formal_task.get('description', '')}. Current adopted outputs: "
            f"{formal_task.get('outputs', [])}. Its formalization source references are "
            f"{formal_task.get('source_components', [])}. Statement prerequisites are available; "
            "proof-only dependencies may still be unfinished. "
            "Refresh autoformalize_brief. " + strategy_instruction +
            "Edit in your worktree using MCP tools while iterating: prefer compatible Axle tools "
            "when enabled over equivalent Lean LSP tools, and Lean LSP for local goals and diagnostics. "
            "Use direct shell checks only as a fallback or when compiled artifacts are needed. "
            "Choose Lean representations as needed. Submit explicit outputs with `finalize_formalization`; "
            "stage='representation' shares checked statements/definitions before proofs, while "
            "stage='complete' implements the whole node (and can adopt its outputs directly). Unity will commit the "
            "exact source and perform the sole authoritative full build in main. Publish useful Lean/API findings "
            "as you work. Supplied documents are read-only. Use report_source_issue for source defects, "
            "and submit_source_repair with evidence when you can repair the issue directly. "
            "Do not change the source or silently formalize a different result. "
            "Use refine_chunks for explicit graph/interpretation revisions; use source repair for source defects."
        ))
        event = asyncio.Event()
        interrupts[name] = event
        roles[name] = "formalizing"
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
                workspace_observer=guard.observe_tool,
            ),
            name=f"{PIPELINE}:formalizing:{name}:{task_id}",
        )

    def launch_idle() -> None:
        if stop_requested(paths.project_root) or integration is not None or recovery is not None:
            return
        current = autoformalize_state.load_state(paths.forum)
        if autoformalize_state.pending_replan(current):
            return
        refresh_worker_targets(current)
        from .autoformalize_repairs import repair_attempt_limit, source_repair_turn
        issues = autoformalize_state.ready_source_issues(current)
        assigned_issues = set(repair_issues.values())
        for name in agents:
            if name in tasks or name in stopping or autoformalize_server.has_pending_formal_candidate(current, name):
                continue
            issue = next((item for item in issues if item["issue_id"] not in assigned_issues
                          and name not in repair_exhausted.get(item["issue_id"], set())), None)
            if issue is None:
                continue
            issue_id = issue["issue_id"]
            assigned_issues.add(issue_id)
            repair_issues[name] = issue_id
            roles[name] = "source_repair"
            interrupts[name] = asyncio.Event()
            tasks[name] = asyncio.create_task(source_repair_turn(
                agents[name], roster, paths, issue_id, repair_attempt_limit(),
                interrupt_event=interrupts[name],
                workspace_observer=guard.observe_tool,
                workspace_notice=recovery_notices.pop(name, ""),
            ), name=f"autoformalize:source_repair:{name}:{issue_id}")
        ready = autoformalize_state.ready_formal_tasks(current)
        if not ready:
            return
        idle = [name for name in agents if name not in tasks and name not in stopping]
        ready_ids = {formal_task["task_id"] for formal_task in ready}
        unassigned = []
        for name in idle:
            if autoformalize_server.has_pending_formal_candidate(current, name):
                continue
            previous = worker_targets.get(name, "")
            successors = current.get("retired_tasks", {}).get(previous, {}).get("replaced_by", [])
            successor = next((key for key in successors if key in ready_ids), None)
            if successor:
                launch(name, successor, "Your prior informal node was replaced. Read autoformalize_task "
                       "for this successor and its lineage. Your worktree was preserved; reuse relevant "
                       "work, then claim a strategy for this node before finalizing.")
                continue
            # Unregistered edits are work too. Keep their target rather than
            # assigning the worker to a different ready task and resetting it.
            if current["formal_tasks"].get(previous, {}).get("status") == "pending":
                if previous in ready_ids:
                    launch(name, previous)
                elif not autoformalize_server.unresolved_formal_tasks(current, name):
                    prerequisites = autoformalize_server.ready_statement_prerequisites(current, previous)
                    active = [worker_targets.get(owner, "") for owner, running in tasks.items()
                              if not running.done()]
                    for _, prerequisite in _formal_task_assignments(prerequisites, [name], active):
                        # prepare_formal_worktree rechecks under locks and permits
                        # this move only with no private edits/commits to discard.
                        launch(name, prerequisite)
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

    def candidate_workers(candidate: dict) -> list[str]:
        return [
            name for name, running in tasks.items()
            if not running.done() and roles.get(name) == "formalizing" and (
                worker_targets.get(name) == candidate["task_id"]
                or autoformalize_state.author_key(name) == autoformalize_state.author_key(candidate["author"])
            )
        ]

    try:
        launch_idle()
        while not stop_requested(paths.project_root):
            await asyncio.sleep(0.1)
            incident = await asyncio.to_thread(guard.poll_due)
            if incident is not None:
                record_recovery(incident)
            state = autoformalize_state.load_state(paths.forum)
            refresh_worker_targets(state)
            require_source_matches(paths, state)
            for name in list(tasks):
                if roles.get(name) != "formalizing":
                    continue
                target, revision = worker_revisions.get(name, ("", 0))
                latest = state.get("formal_tasks", {}).get(target)
                if target and (latest is None or latest.get("revision", 0) != revision):
                    request_stop(name, f"informal task {target} was refined; refresh its current interpretation")
            replan = autoformalize_state.pending_replan(state)
            if replan or state.get("phase") != "formalizing":
                if integration_cancel is not None:
                    integration_cancel.set()
                for name in list(tasks):
                    request_stop(name, "formalization replanning" if replan else "formalization phase changed")

            # Cancelling one worker never delays observation of another candidate.
            for name, stopper in list(stopping.items()):
                if stopper.done():
                    stopper.result()
                    stopping.pop(name)
                    if name in tasks and not tasks[name].done():
                        raise ValueError(f"Worker {name} did not stop; source and pending candidates were preserved")

            for name, running in list(tasks.items()):
                if not running.done() or name in stopping:
                    continue
                result = None
                try:
                    result = running.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _console.print(f"[red]worker {name} failed: {exc!r}[/red]")
                tasks.pop(name, None)
                interrupts.pop(name, None)
                role = roles.pop(name, "")
                if role == "source_repair":
                    issue_id = repair_issues.pop(name, "")
                    if isinstance(result, dict) and result.get("status") == "exhausted":
                        repair_exhausted.setdefault(issue_id, set()).add(name)
                        if repair_exhausted[issue_id] == set(agents):
                            autoformalize_state.mark_source_issue_unresolved(
                                paths.forum, issue_id,
                                "Every configured agent exhausted its source-repair attempts",
                            )

            # Consume all submissions while a separate serial integration owns main.
            state = autoformalize_state.load_state(paths.forum)
            refresh_worker_targets(state)
            for candidate in state.get("formal_candidates", {}).values():
                if (candidate.get("status") in {"submitted", "merging"}
                        and autoformalize_state.candidate_is_current(state, candidate)):
                    for name in candidate_workers(candidate):
                        request_stop(name, f"formal candidate {candidate['candidate_id']} submitted for {candidate['task_id']}")

            if integration is not None and integration.done():
                result = integration.result()
                finished = integration_candidate
                integration = None
                integration_candidate = {}
                integration_cancel = None
                if result.get("workspace_recovery"):
                    # Integration may return an incident captured before later
                    # notifications arrived. Keep its rollback/candidate data,
                    # but only use the guard's current evidence for recovery.
                    incident = await asyncio.to_thread(guard.poll_due, force=True)
                    record_recovery(incident or result["incident"], result)
                elif result.get("ok"):
                    retire_completed_task(finished["task_id"])
                elif not result.get("deferred"):
                    require_source_matches(paths, autoformalize_state.load_state(paths.forum))
                    _console.print(f"[red]candidate {finished['candidate_id']} failed: {result.get('error', '')}[/red]")

            if recovery is not None:
                incident = await asyncio.to_thread(guard.poll_due, force=True)
                record_recovery(incident or recovery["incident"])
                incident = recovery["incident"]
                author = incident.author
                if integration is not None or author in tasks or author in stopping:
                    # The violator's cancellation can deliver the completion
                    # evidence needed to undo its write. Other workers remain
                    # active in their private worktrees throughout this drain.
                    continue
                if author not in agents or not incident.recoverable:
                    if time.monotonic() < recovery["evidence_deadline"]:
                        continue
                    # Never guess a shell writer or discard unsafe changes.
                    raise WorkspaceContamination(incident)
                # This short critical section must not be abandoned in a
                # background thread if the runtime itself is cancelled.
                _recover_main_workspace(paths, guard, recovery)
                recovery_notices[author] = (
                    f"Your previous turn modified {', '.join(incident.paths)} outside your worktree. "
                    f"Unity preserved the incident at {incident.artifact} and restored main. "
                    f"Continue in {worktrees[author]}; inspect the preserved work before repeating it. "
                )
                _console.print(f"[yellow]recovered misplaced work from {author}; continuing its existing task[/yellow]")
                recovery = None

            state = autoformalize_state.load_state(paths.forum)
            replan = autoformalize_state.pending_replan(state)
            if replan:
                if integration is None and not tasks and not stopping:
                    assignments = {
                        name: {"task_id": task_id,
                               "task_revision": state.get("formal_tasks", {}).get(task_id, {}).get("revision"),
                               "worktree": str(worktrees[name])}
                        for name, task_id in worker_targets.items() if task_id
                    }
                    roots = replan.get("task_ids")
                    affected = _affected_tasks(state, roots)
                    assignments = checkpoint_replan_worktrees(paths, assignments, affected)
                    with _merge_lock(paths.project_root):
                        return autoformalize_state.begin_replan(
                            paths.forum, replan["request_id"], assignments=assignments,
                        )
                continue
            if state.get("phase") != "formalizing":
                if integration is None and not tasks and not stopping:
                    return state
                continue

            if integration is None:
                candidates = sorted(
                    (item for item in state.get("formal_candidates", {}).values()
                     if item.get("status") == "submitted"
                     and autoformalize_state.candidate_is_current(state, item)),
                    key=lambda item: item.get("created_at", 0),
                )
                for candidate in candidates:
                    if candidate_workers(candidate):
                        continue
                    # An old worker's stop job can still be reaping owner jobs.
                    if any(worker_targets.get(name) == candidate["task_id"]
                           or autoformalize_state.author_key(name) == autoformalize_state.author_key(candidate["author"])
                           for name in stopping):
                        continue
                    started = autoformalize_state.begin_formal_merge(paths.forum, candidate["candidate_id"])
                    if started.get("idempotent") or started.get("conflict"):
                        continue
                    integration_candidate = started["candidate"]
                    integration_cancel = Event()
                    _console.print(f"[cyan]mechanically reviewing {candidate['candidate_id']} for {candidate['task_id']}[/cyan]")
                    integration = asyncio.create_task(asyncio.to_thread(
                        _integrate_and_record, paths, integration_candidate,
                        state["formal_tasks"][candidate["task_id"]], integration_cancel, guard,
                    ), name=f"autoformalize:integration:{candidate['candidate_id']}")
                    break

            if integration is None:
                # These source checks/preparations must never wait on a build's merge lock.
                for name in agents:
                    if name in tasks or name in stopping:
                        continue
                    current = autoformalize_state.load_state(paths.forum)
                    task_id = worker_targets.get(name, "")
                    formal_task = current.get("formal_tasks", {}).get(task_id, {})
                    if formal_task.get("status") != "pending" or autoformalize_server.has_pending_formal_candidate(current, name):
                        continue
                    dirty, source_digest = worktree_changes(name)
                    strategy = participating_strategy(current, name, task_id)
                    nudge_key = (name, task_id, source_digest)
                    if dirty and strategy and nudge_key not in submission_nudges:
                        submission_nudges.add(nudge_key)
                        launch(name, task_id,
                               "Submission check only: inspect the existing worktree diff before new research. "
                               "Finalize a completed target, or publish a precise blocker and continue the "
                               "claimed strategy. Do not register a replacement or repeat unchanged searches.")
                launch_idle()

            state = autoformalize_state.load_state(paths.forum)
            if (integration is None and not tasks and not stopping
                    and autoformalize_state.all_formal_tasks_complete(state)):
                if autoformalize_state.open_source_issues(state):
                    raise ValueError("Formal declarations are complete, but source issues remain unresolved; "
                                     "critic acceptance is blocked until a repair is adopted")
                return state
            if (integration is None and not tasks and not stopping
                    and not autoformalize_server.has_pending_formal_candidate(state)):
                if blocked_launches:
                    details = "; ".join(f"{name}: {reason}" for name, reason in blocked_launches.items())
                    raise ValueError("Autoformalize cannot launch workers without discarding preserved work. "
                                     "Reconcile these worktrees before resuming: " + details)
                return state
        return autoformalize_state.load_state(paths.forum)
    except WorkspaceContamination:
        workspace_unsafe = True
        raise
    finally:
        for name in list(tasks):
            request_stop(name, "formalization runtime ending")
        # Cancelling an asyncio wrapper would leave its thread mutating main.
        # Signal cooperative subprocess cancellation, then drain its rollback.
        interrupted_during_drain = False
        if integration is not None:
            if integration_cancel is not None:
                integration_cancel.set()
            while not integration.done():
                try:
                    await asyncio.shield(integration)
                except asyncio.CancelledError:
                    interrupted_during_drain = True
                except Exception:
                    break  # Retrieve/report below; still finish worker cleanup.
            try:
                result = integration.result()
                if result.get("workspace_recovery"):
                    workspace_unsafe = True
                    _console.print(f"[red]{result['error']}; main and worktrees preserved[/red]")
            except Exception as exc:
                _console.print(f"[red]integration ended with an error; worktrees preserved: {exc!r}[/red]")
        if stopping:
            await asyncio.gather(*stopping.values(), return_exceptions=True)
        await asyncio.to_thread(autoformalize_jobs.terminate, paths.project_root)
        final_state = autoformalize_state.load_state(paths.forum)
        for agent in roster.agents:
            running = tasks.get(agent.name)
            if running is not None and not running.done():
                _console.print(f"[red]worker {agent.name} has not stopped; preserving its worktree[/red]")
                continue
            if (workspace_unsafe or not autoformalize_state.all_formal_tasks_complete(final_state)
                    or final_state.get("phase") == "chunking"
                    or autoformalize_state.pending_replan(final_state)
                    or autoformalize_state.open_source_issues(final_state)):
                continue
            autoformalize_state.release_author_claims(
                paths.forum, agent.name, "formalization runtime ended",
            )
            tree = worktrees.get(agent.name)
            if tree is not None:
                worktree.cleanup_worktree(agent.name, tree, paths.project_root)
        if interrupted_during_drain:
            raise asyncio.CancelledError


def _affected_tasks(state: dict, requested: list[str] | None) -> set[str]:
    tasks = state.get("formal_tasks", {})
    affected = set(tasks) if not requested else set(requested)
    while True:
        expanded = affected | {task_id for task_id, task in tasks.items()
                               if affected.intersection(task.get("dependencies", []))}
        if expanded == affected:
            return affected
        affected = expanded


def checkpoint_replan_worktrees(paths, assignments: dict, affected: set[str]) -> dict:
    """Preserve known obsolete work by commit/ref before a controller may refresh it."""
    from copy import deepcopy
    import uuid

    saved = deepcopy(assignments)
    with _merge_lock(paths.project_root):
        for author, assignment in saved.items():
            if assignment.get("task_id") not in affected:
                continue
            with autoformalize_server._finalization_lock(author):
                tree = _formal_worktree(paths.project_root, author)
                if assignment.get("worktree") != str(tree):
                    raise ValueError(f"Unknown worktree ownership for {author}; source preserved")
                status = _git(tree, "status", "--porcelain")
                if status.returncode:
                    raise ValueError(f"Cannot checkpoint {author}'s worktree; source preserved")
                if status.stdout.strip():
                    added = _git(tree, "add", "-A")
                    if added.returncode:
                        raise ValueError(added.stderr or "could not stage replan checkpoint")
                    committed = _git(tree, "commit", "-m", "UNITY: preserve obsolete autoformalize work before replanning")
                    if committed.returncode:
                        raise ValueError(committed.stderr or "could not checkpoint replan work")
                head = _git(tree, "rev-parse", "HEAD").stdout.strip()
                safe_author = re.sub(r"[^a-zA-Z0-9_-]", "_", author)
                reference = f"refs/unity/autoformalize-checkpoints/{safe_author}/{uuid.uuid4().hex}"
                saved_ref = _git(tree, "update-ref", reference, head)
                if saved_ref.returncode:
                    raise ValueError(saved_ref.stderr or "could not retain replan checkpoint")
                assignment["checkpoint"] = {"ref": reference, "commit_sha": head}
    return saved


def refresh_replanned_worktrees(paths, assignments: dict, state: dict) -> dict:
    """Refresh only stopped, explicitly known assignments whose task revision changed."""
    affected = {
        assignment["task_id"] for assignment in assignments.values()
        if assignment.get("task_id") not in state.get("formal_tasks", {})
        or assignment.get("task_revision") != state["formal_tasks"][assignment["task_id"]].get("revision")
    }
    saved = checkpoint_replan_worktrees(paths, assignments, affected)
    with _merge_lock(paths.project_root):
        for author, assignment in saved.items():
            if assignment.get("task_id") not in affected:
                continue
            with autoformalize_server._finalization_lock(author):
                current = autoformalize_state.load_state(paths.forum)
                if autoformalize_server.has_pending_formal_candidate(current, author):
                    raise ValueError(f"Candidate review still protects {author}'s branch; source preserved")
                if worktree.main_commit(paths.project_root) != current["formalization"]["main_sha"]:
                    raise ValueError("Main changed before replanned worktree refresh; source preserved")
                result = worktree.force_sync_from_main(paths.project_root, author)
                if not result.get("ok"):
                    raise ValueError(result.get("error") or "replanned worktree refresh failed")
    return saved
