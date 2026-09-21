"""``unity solve``: solve a problem in English, then formalize that solution."""

import hashlib
import json
import os
from pathlib import Path
import tempfile

import asyncclick as click

from ..Architect import architect
from .. import lake, library, solve_contract, solve_jobs, solve_state, worktree
from ..config import load_paths
from ..orchestrator import (
    build_solve_mcp,
    dispatch,
    load_prompt,
    mark_done,
    mark_phase,
    resume_point,
    stop_requested,
)
from ..solve_formal_orchestrator import (
    dispatch as formal_dispatch, build_solve_formal_mcp,
)
from ..solve_input import require_source_matches
from ..solve_repairs import run_source_repairs
from ..solve_report import persist_report
from ..roster import load_roster
from ..solve_runtime import (
    configure_forum,
    forum_brief,
    materialize_solution,
    reset_solve_workspace,
    run_solving_runtime,
)
from ..solve_formal_runtime import (
    recover_interrupted_formal_merges, run_formalizing_runtime,
    write_formalization_plan, _merge_lock, refresh_replanned_worktrees,
)

PIPELINE = "solve"


def _retrospective_enabled() -> bool:
    return os.getenv("RETROSPECTIVE", "true").strip().lower() != "false"


def _validate_retrospective_result(report_path: Path, run_id: str, library_root: Path) -> dict:
    """Check the saved outcome, not a model's claim that it wrote useful lessons."""
    report = json.loads(report_path.read_text())
    if not isinstance(report, dict) or report.get("run_id") != run_id:
        raise ValueError("retrospective report has a missing or stale run_id")
    status = report.get("status")
    if status == "no_changes":
        reason = report.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("no_changes requires a concrete reason")
        return {"run_id": run_id, "status": status, "reason": reason.strip()}
    if status != "written" or not isinstance(report.get("entries"), list) or not report["entries"]:
        raise ValueError("retrospective must report written entries or no_changes with a reason")

    root = library_root.resolve(strict=True)
    entries = []
    for entry in report["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("each retrospective entry requires a library Markdown path")
        path = Path(entry["path"]).expanduser()
        path = (path if path.is_absolute() else root / path).resolve(strict=True)
        if not path.is_relative_to(root) or path.suffix.lower() != ".md" or not path.is_file():
            raise ValueError("retrospective entries must be Markdown files inside the library")
        content = path.read_bytes()
        if not content.decode("utf-8").strip():
            raise ValueError("retrospective library entries must not be empty")
        evidence = entry.get("evidence")
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(item, str) or not item.strip() for item in evidence)):
            raise ValueError("each retrospective entry requires nonempty evidence references")
        entries.append({
            "path": str(path),
            "evidence": [item.strip() for item in evidence],
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    return {"run_id": run_id, "status": status, "entries": entries}


def _save_retrospective_result(path: Path, result: dict) -> None:
    """Replace the run report atomically, including when an agent left a symlink."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as file:
            temporary = Path(file.name)
            json.dump(result, file, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def _run_retrospective(roster, paths) -> dict:
    """Run one retrospective and report its outcome independently of proof acceptance."""
    run_id = solve_state.load_state(paths.forum)["run_id"]
    report_path = paths.unity / "retrospective.json"
    result = {"run_id": run_id, "status": "incomplete", "reason": "no new retrospective result saved"}
    try:
        # Reset even on --continue: an earlier result from this run is not evidence
        # that the newly dispatched retrospective produced anything.
        _save_retrospective_result(report_path, result)
        library_root = library.ensure_library().resolve()
        configure_forum(paths, "retrospective")
        schemas = (
            {"run_id": run_id, "status": "written", "entries": [
                {"path": "tactics/example.md", "evidence": ["artifact-id or checked source reference"]}
            ]},
            {"run_id": run_id, "status": "no_changes", "reason": "why no reusable lesson is justified"},
        )
        results = await dispatch(
            [roster.primary], roster, load_prompt("solve/RETROSPECTIVE"),
            f"Distill reusable lessons from this completed solve run. Run ID: {run_id}. "
            f"Write library Markdown under {library_root}, using the existing tactics, lemmas, "
            "references, subagents, or skills directories. Preserve existing useful content. "
            f"Then write {report_path.resolve()} with exactly one of these JSON shapes:\n"
            + "\n".join(json.dumps(schema) for schema in schemas)
            + "\nEntry paths may be absolute or relative to the library root. Evidence must cite "
            "the actual checked run artifacts or source locations supporting each lesson. "
            "Do not compute file hashes; Unity records them after reading the saved files. "
            "Do not inspect Unity installation internals. Save the report and end the turn.",
            paths.project_root, build_solve_mcp(paths, "retrospective"),
            tools_prompt="SOLVE_RETROSPECTIVE_TOOLS", icrl_enabled=False,
            brief_provider=_brief_provider(paths, "retrospective"), mcp_profile="solve",
            log_context={"command": "solve", "run_id": run_id,
                         "phase": "retrospective", "role": "retrospective"},
        )
        failures = [type(item).__name__ for item in results if isinstance(item, BaseException)]
        if failures:
            raise ValueError("retrospective agent failed: " + ", ".join(failures))
        result = _validate_retrospective_result(report_path, run_id, library_root)
    except Exception as exc:
        result = {"run_id": run_id, "status": "incomplete",
                  "reason": f"{type(exc).__name__}: {exc}"}
    try:
        _save_retrospective_result(report_path, result)
    except OSError as exc:
        result = {"run_id": run_id, "status": "incomplete",
                  "reason": f"could not save retrospective outcome: {exc}"}
    if result["status"] == "incomplete":
        click.echo("Warning: proof accepted, but retrospective is incomplete: " + result["reason"])
    else:
        click.echo(f"retrospective {result['status']}: {report_path}")
    return result


def _review_quorum() -> int:
    try:
        return max(1, int(os.getenv("UNITY_SOLVE_REVIEW_QUORUM", "1")))
    except ValueError:
        return 1


def _attempt_limit() -> int | float:
    raw = os.getenv("MAX_ATTEMPTS", "").strip()
    if not raw:
        return float("inf")
    try:
        value = int(raw)
    except ValueError as exc:
        raise click.ClickException("MAX_ATTEMPTS must be a positive integer or blank") from exc
    if value < 1:
        raise click.ClickException("MAX_ATTEMPTS must be a positive integer or blank")
    return value


def _brief_provider(paths, profile: str):
    return lambda author: forum_brief(paths, profile, author)


def _prepare_solve_environment(
    root, *, run_architect: bool, validate_project: bool = True,
) -> None:
    """Refresh deterministic Lean state before any solve worker is launched."""
    # Reap registered work left by an interrupted solve before touching the
    # controller-owned shared package cache.
    solve_jobs.terminate(root)
    if run_architect:
        architect(root)
    click.echo("Refreshing Mathlib build cache...")
    lake.cache_get(root)
    if validate_project:
        click.echo("Validating Lean project...")
        lake.build(root)


async def _review_current_solution(roster, paths, max_attempts: int | float) -> bool:
    """Run the independent semantic gate for the exact submitted paper."""
    state = solve_state.load_state(paths.forum)
    candidate_id = state["solution"].get("current_candidate")
    candidate = state["solution_candidates"].get(candidate_id or "")
    if not candidate or candidate.get("status") != "review":
        raise click.ClickException("solution-review phase has no reviewable candidate")

    quorum = _review_quorum()
    existing = {solve_state.author_key(review["author"])
                for review in candidate.get("reviews", [])}
    approvals = len(solve_state.independent_reviewers(candidate, "approve"))
    reviewers = sorted(
        (
            agent for agent in roster.agents
            if solve_state.author_key(agent.name) != solve_state.author_key(candidate["author"])
            and solve_state.author_key(agent.name) not in existing
        ),
        key=lambda agent: -agent.strength,
    )
    needed = max(0, quorum - approvals)
    objected = any(review.get("verdict") == "object" for review in candidate.get("reviews", []))
    if needed and not objected and len(reviewers) < needed:
        raise click.ClickException(
            f"solution candidate needs {quorum} independent review(s), but the roster has "
            f"only {len(reviewers) + approvals} available"
        )

    for reviewer in reviewers:
        attempt = 0
        while attempt < max_attempts:
            if stop_requested(paths.project_root):
                return False
            state = solve_state.load_state(paths.forum)
            if (state["phase"] != "solution_review"
                    or state["solution"].get("current_candidate") != candidate_id):
                return False
            candidate = state["solution_candidates"][candidate_id]
            reviews = candidate.get("reviews", [])
            if (any(review.get("verdict") == "object" for review in reviews)
                    or len(solve_state.independent_reviewers(candidate, "approve")) >= quorum
                    or any(solve_state.author_key(review["author"])
                           == solve_state.author_key(reviewer.name) for review in reviews)):
                break
            attempt += 1
            await dispatch(
                [reviewer],
                roster,
                load_prompt("solve/SOLUTION_REVIEW"),
                f"Independently review solution candidate `{candidate_id}` at immutable artifact "
                f"`{candidate['artifact_id']}` with SHA-256 `{candidate['sha256']}` against the original "
                "problem. Submit exactly one approve or object verdict through review_solution_candidate. "
                "Do not edit the paper.",
                paths.project_root,
                build_solve_mcp(paths, "solution_review"),
                tools_prompt="SOLVE_REVIEW_TOOLS",
                icrl_enabled=False,
                brief_provider=_brief_provider(paths, "solution_review"),
                mcp_profile="solve",
                log_context={"command": "solve", "run_id": state.get("run_id"),
                             "phase": "solution_review", "role": "reviewer", "attempt": attempt},
            )

    if stop_requested(paths.project_root):
        return False
    state = solve_state.load_state(paths.forum)
    if (state["phase"] != "solution_review"
            or state["solution"].get("current_candidate") != candidate_id):
        return False
    candidate = state["solution_candidates"][candidate_id]
    objections = [review for review in candidate.get("reviews", []) if review.get("verdict") == "object"]
    approvals = solve_state.independent_reviewers(candidate, "approve")
    if objections:
        reason = " | ".join(review.get("review", "") for review in objections)
        solve_state.reject_solution_candidate(paths.forum, candidate_id, "Unity", reason)
        click.echo(f"solution candidate {candidate_id} rejected: {reason[:500]}")
        return False
    if len(approvals) < quorum:
        raise click.ClickException(
            "every eligible reviewer exhausted its solution-review attempts "
            "before the required verdicts were submitted"
        )

    accepted = solve_state.accept_solution_candidate(paths.forum, candidate_id, "Unity")
    materialize_solution(paths, accepted)
    click.echo(f"accepted informal solution {candidate_id} ({accepted['sha256']})")
    return True


async def _chunk_accepted_solution(roster, paths, max_attempts: int | float) -> None:
    """Rotate failed executions; correct proposals inside their existing session."""
    from .. import artifacts
    from ..solve_chunking import chunking_workspace
    from ..solve_input import store_bytes
    from ..solve_formal_runtime import (
        read_chunking_draft, prepare_chunking_draft, seed_chunking_draft, chunking_diagnostic,
    )

    state = solve_state.load_state(paths.forum)
    candidate = solve_state.formal_source(state)
    if not candidate:
        raise click.ClickException("chunking requires a bound formalization source")
    require_source_matches(paths, state)
    chunkers = [roster.primary] + [a for a in roster.agents if a.name != roster.primary.name]
    failures = []
    # The last submitted bytes survive genuine session failures without requiring
    # another agent to recreate the proposal. Interrupted runs also retain this artifact.
    resume_draft = None
    for row in reversed(state["chunking_attempts"]):
        if not row.get("obsolete") and row.get("draft_artifact"):
            try:
                resume_draft = artifacts.artifact_bytes(paths.artifacts, row["draft_artifact"])
            except (OSError, ValueError):
                continue
            break

    for chunker in chunkers:
        while not stop_requested(paths.project_root):
            current = solve_state.load_state(paths.forum)
            if (current.get("phase") != "chunking"
                    or solve_state.formal_source(current).get("candidate_id") != candidate["candidate_id"]):
                return
            if solve_state.chunking_attempt_count(current, candidate["candidate_id"], chunker.name) >= max_attempts:
                break
            attempt = solve_state.begin_chunking_attempt(paths.forum, candidate["candidate_id"], chunker.name)
            plan_path = write_formalization_plan(paths, candidate)
            installed = None
            assignments = {}
            last_feedback = None
            execution_started = False
            try:
                async with chunking_workspace(paths, chunker.name, attempt) as workspace:
                    seed = seed_chunking_draft(current)
                    if resume_draft is not None:
                        workspace.draft_path.write_bytes(resume_draft)
                    elif seed is not None:
                        workspace.draft_path.write_text(json.dumps(seed, indent=2) + "\n")

                    async def complete_draft(_final):
                        nonlocal installed, assignments, resume_draft, last_feedback
                        if stop_requested(paths.project_root):
                            return None
                        current = solve_state.load_state(paths.forum)
                        if current.get("phase") != "chunking":
                            return None
                        try:
                            require_source_matches(paths, current)
                        except ValueError as exc:
                            raise click.ClickException(str(exc)) from exc
                        repaired = await run_source_repairs(roster, paths, max_attempts)
                        if repaired.get("phase") != "chunking":
                            return None
                        if stop_requested(paths.project_root):
                            return None
                        if any(row.get("status") == "unresolved"
                               for row in solve_state.open_source_issues(repaired)):
                            raise click.ClickException("Source-repair attempts exhausted; original input and evidence preserved")
                        write_formalization_plan(paths, candidate)
                        current = solve_state.load_state(paths.forum)
                        payload = None
                        try:
                            payload = read_chunking_draft(workspace.draft_path)
                            resume_draft = payload
                            dag, _ = prepare_chunking_draft(paths, current, payload)
                        except (ValueError, RecursionError) as exc:
                            diagnostic = chunking_diagnostic(exc)
                            signature = (hashlib.sha256(payload).hexdigest() if payload is not None else None,
                                         json.dumps(diagnostic, sort_keys=True))
                            artifact_id = None
                            if payload is not None and signature != last_feedback:
                                record = store_bytes(paths.artifacts, payload, kind="solve_chunking_draft",
                                                     producer=chunker.name, source=attempt["attempt_id"])
                                artifact_id = record["artifact_id"]
                            solve_state.record_chunking_feedback(
                                paths.forum, attempt["attempt_id"], diagnostic, artifact_id=artifact_id,
                            )
                            last_feedback = signature
                            return ("Unity rejected this draft, not this execution. Correct the fields below in "
                                    "the same draft; call validate_chunks() before finishing. Do not rewrite frozen "
                                    "source obligations or call Unity internals.\n"
                                    + json.dumps(diagnostic, ensure_ascii=False)
                                    + "\nRead updated source-repair context at " + str(plan_path))

                        # A plan preflight never approves itself. Refresh live environment
                        # once here, outside the model/API retry machinery.
                        try:
                            contract = solve_contract.prepare_source_contract(paths, dag, state=current)
                        except (OSError, ValueError) as exc:
                            raise click.ClickException("Contract environment check failed: " + str(exc)) from exc
                        old_environment = (current["formalization"].get("contract") or {}).get("environment")
                        if old_environment is not None and old_environment != contract["environment"]:
                            raise click.ClickException("Protected Lean environment changed; not retrying chunking")
                        with _merge_lock(paths.project_root):
                            latest = solve_state.load_state(paths.forum)
                            if (latest.get("phase") != "chunking"
                                    or solve_state.formal_source(latest).get("candidate_id") != candidate["candidate_id"]):
                                return None
                            try:
                                unchanged = read_chunking_draft(workspace.draft_path) == payload
                            except ValueError as exc:
                                return json.dumps(chunking_diagnostic(exc), ensure_ascii=False)
                            if latest["revision"] != current["revision"] or not unchanged:
                                return "The draft or shared state changed during acceptance. Refresh validate_chunks() and finish again."
                            require_source_matches(paths, latest)
                            main_sha = worktree.main_commit(paths.project_root)
                            if main_sha != contract["source_main_sha"]:
                                return "Accepted main changed during validation. Refresh validate_chunks() and finish again."
                            record = artifacts.store_text(
                                paths.artifacts, json.dumps(dag, sort_keys=True, ensure_ascii=False),
                                kind="solve_accepted_plan", producer="Unity", source=attempt["attempt_id"],
                            )
                            try:
                                installed = solve_state.initialize_informal_plan(
                                    paths.forum, dag, main_sha=main_sha, contract=contract,
                                    attempt_id=attempt["attempt_id"], expected_revision=latest["revision"],
                                    plan_artifact=record["artifact_id"],
                                )
                            except ValueError as exc:
                                return ("Publication did not change state. Refresh validate_chunks() and correct: "
                                        + json.dumps(chunking_diagnostic(exc), ensure_ascii=False))
                            assignments = (latest.get("replan") or {}).get("assignments", {})
                            # State and its immutable artifact are authoritative if the process
                            # exits before this convenience JSON mirror is replaced.
                            artifacts._atomic_write(paths.unity / "dag.json",
                                                    (json.dumps(dag, indent=2) + "\n").encode())
                        return None

                    async def completed(final):
                        try:
                            return await complete_draft(final)
                        except click.ClickException:
                            raise
                        except Exception as exc:
                            # Unexpected controller/IO failures are not agent/API
                            # failures: stop rather than spending the whole roster.
                            raise click.ClickException("Chunking controller failed: " + str(exc)) from exc

                    try:
                        execution_started = True
                        results = await formal_dispatch(
                            [chunker], roster, load_prompt(f"{PIPELINE}/CHUNKING"),
                            f"You are the chunker for execution {attempt['attempt']} ({chunker.name}). "
                            f"Read scope at {paths.unity_md} and the source/repair plan at {plan_path}. "
                            f"Write only the draft at {workspace.draft_path}. "
                            + ("This is a replan: edit the seeded mutable-only draft; Unity supplies frozen obligations. "
                               if seed is not None else
                               "This is initial chunking: use the full informal DAG schema in your instructions. ")
                            + "Keep one initial node per source definition/result, with its statement and supplied proof. "
                              "Use separate statement/proof dependencies. Do not write Lean or build anything. "
                              "Correct validation feedback in this session; ordinary corrections do not consume attempts. "
                            + ("Previous execution failures: " + " | ".join(failures[-3:]) if failures else ""),
                            workspace.cwd, workspace.mcp,
                            tools_prompt=f"{PIPELINE.upper()}_CHUNKING_TOOLS", icrl_enabled=False,
                            brief_provider=_brief_provider(paths, "chunking"),
                            log_context={"command": PIPELINE, "run_id": state["run_id"], "phase": "chunking",
                                         "role": "chunker", "candidate_id": candidate["candidate_id"],
                                         "attempt": attempt["attempt"], "attempt_id": attempt["attempt_id"]},
                            on_normal_completion=completed, env_overrides=workspace.env,
                        )
                    finally:
                        # Archive before the scratch workspace is removed, including
                        # a transport failure/cancellation before a completed turn.
                        if installed is None:
                            try:
                                resume_draft = read_chunking_draft(workspace.draft_path)
                            except ValueError:
                                pass
                            except OSError as exc:
                                workspace.preserve = True
                                raise click.ClickException(
                                    f"Cannot read chunking draft; retained at {workspace.draft_path}: {exc}"
                                ) from exc
                            else:
                                try:
                                    record = store_bytes(paths.artifacts, resume_draft,
                                        kind="solve_chunking_draft", producer=chunker.name,
                                        source=attempt["attempt_id"])
                                    if solve_state.load_state(paths.forum).get("phase") == "chunking":
                                        solve_state.save_chunking_draft(
                                            paths.forum, attempt["attempt_id"], record["artifact_id"],
                                        )
                                except Exception as exc:
                                    workspace.preserve = True
                                    raise click.ClickException(
                                        f"Cannot archive chunking draft; retained at {workspace.draft_path}: {exc}"
                                    ) from exc
                    if stop_requested(paths.project_root):
                        return
                    if installed is None and solve_state.load_state(paths.forum).get("phase") != "chunking":
                        return
                    error = next((result for result in results if isinstance(result, Exception)), None)
                    if error is not None:
                        raise error
                    if installed is None:
                        raise RuntimeError("chunker execution ended without controller plan publication")
            except click.ClickException:
                if installed is None and solve_state.load_state(paths.forum).get("phase") == "chunking":
                    solve_state.finish_chunking_attempt(paths.forum, attempt["attempt_id"],
                        succeeded=False, reason="controller/source/environment failure; see terminal diagnostic")
                raise
            except Exception as exc:
                if installed is not None:
                    raise click.ClickException("Plan accepted but chunker cleanup failed: " + str(exc)) from exc
                if not execution_started:
                    raise click.ClickException("Cannot start chunker workspace: " + str(exc)) from exc
                # Only genuinely failed executions reach here, never schema corrections.
                reason = f"{type(exc).__name__}: {exc}"[:2000]
                solve_state.finish_chunking_attempt(paths.forum, attempt["attempt_id"],
                                                            succeeded=False, reason=reason)
                failures.append(f"{chunker.name} attempt {attempt['attempt']}: {reason}")
                click.echo(f"chunker {failures[-1]}")
                continue
            if assignments:
                refresh_replanned_worktrees(paths, assignments, installed)
            click.echo(f"created {len(installed['formal_tasks'])} formalization task(s) "
                       f"using {chunker.name} on execution {attempt['attempt']}")
            return

    if stop_requested(paths.project_root):
        return
    summary = " | ".join(failures[-10:]) or "attempt limits were already exhausted"
    solve_state.record_chunking_exhausted(paths.forum, summary)
    raise click.ClickException("every configured agent exhausted its chunking executions: " + summary)


def _prepare_critic_snapshot(paths) -> bool:
    """Cache exact mechanical evidence for final or diagnostic critic retries."""
    with _merge_lock(paths.project_root):
        state = solve_state.load_state(paths.forum)
        if not state["formalization"].get("contract"):
            raise click.ClickException(
                "this solve run has no protected formal specification; request_rechunk before resuming review"
            )
        snapshot = state["formalization"].get("review_snapshot") or {}
        if not solve_contract.snapshot_is_current(paths, state, snapshot, require_complete=False):
            try:
                report = solve_contract.verify_final_project(paths, state)
            except (OSError, ValueError) as exc:
                raise click.ClickException(f"mechanical critic gate could not verify this revision: {exc}") from exc
            if report["main_sha"] != state["formalization"]["main_sha"]:
                raise click.ClickException(
                    "main changed outside candidate integration; request_rechunk to establish a new reviewed specification"
                )
            solve_state.record_review_snapshot(paths.forum, report)
            snapshot = report
        if state["phase"] != "critic":
            solve_state.begin_critic(paths.forum, diagnostic=not snapshot["passed"])
    return True


def _accept_current_critic(paths) -> bool:
    """An LLM verdict is not authority to accept stale or edited sources."""
    if stop_requested(paths.project_root):
        return False
    with _merge_lock(paths.project_root):
        state = solve_state.load_state(paths.forum)
        formal = state["formalization"]
        if (formal.get("status") != "approval_pending" or solve_state.pending_replan(state)
                or solve_state.open_source_issues(state)):
            return False
        snapshot = formal.get("review_snapshot") or {}
        if not solve_contract.snapshot_is_current(paths, state, snapshot):
            # Recompute and require a new semantic review even when the new bytes
            # still pass machine checks. Never stamp old evidence with a new SHA.
            report = solve_contract.verify_final_project(paths, state)
            if report["main_sha"] != formal["main_sha"]:
                raise click.ClickException(
                    "main changed during critic review; request_rechunk before acceptance"
                )
            solve_state.record_review_snapshot(paths.forum, report)
            click.echo("critic approval became stale; the changed revision needs a new review")
            return False
        solve_state.complete_critic_review(
            paths.forum, snapshot["snapshot_id"], formal["pending_verdict_id"],
        )
        return True


async def _run_critic(roster, paths, *, critic, attempt: int = 1) -> None:
    """Run one attempt with the selected critic."""
    if not _prepare_critic_snapshot(paths):
        return
    if _accept_current_critic(paths):
        return
    state = solve_state.load_state(paths.forum)
    diagnostic = not state["formalization"]["review_snapshot"]["passed"]
    before = len(solve_state.load_state(paths.forum).get("critic_verdicts", []))
    retry_context = (
        f"This is critic attempt {attempt}. The gate is still open. Reading files or ending a turn "
        "without submitting a verdict does not complete the review. Refresh the current brief, "
        "check any remaining concerns, and submit the structured verdict before finishing. "
        if attempt > 1 else ""
    )
    await formal_dispatch(
        [critic],
        roster,
        load_prompt(f"{PIPELINE}/CRITIC"),
        retry_context
        + ("DIAGNOSTIC REVIEW: this formalization round ended without passing final checks. "
           "Review completed and pending tasks, yielded attempts, last-round launch blockers, "
           "and preserved work. Give exact task IDs and concrete next steps in a lean_reopen verdict, "
           "or request a justified replan/source repair. Mark unchecked requirements not_checked. "
           "Do not approve incomplete proofs or reopen unaffected work. "
           if diagnostic else
           "Audit the complete Lean project against the supplied source documents and UNITY.md scope. ")
        + "Use the exact recorded "
        "machine snapshot for build, contract, and axiom status. Independently check requirement "
        "completeness and the mathematical meaning of statements and definitions against the source. "
        "Submit one structured verdict with submit_formalization_verdict and mandatory snapshot-bound "
        "per-requirement review evidence. Reopen only the exact Lean tasks that "
        "need repair. "
        + "Do not edit accepted PROOF.tex directly. Use lean_reopen for encoding/proof defects, "
        "propose_source_fix for a corrected paper requiring independent paper review, or reopen_solving "
        "when the accepted mathematics needs further informal work. UNITY.md remains the original problem; "
        "do not approve a changed or weakened result.",
        paths.project_root,
        build_solve_formal_mcp(paths, "critic"),
        tools_prompt=f"{PIPELINE.upper()}_CRITIC_TOOLS",
        icrl_enabled=False,
        brief_provider=_brief_provider(paths, "critic"),
        mcp_profile="solve",
        log_context={"command": PIPELINE, "run_id": state.get("run_id"),
                     "phase": "critic", "role": "critic", "attempt": attempt},
    )
    after_state = solve_state.load_state(paths.forum)
    if solve_state.pending_replan(after_state) or solve_state.open_source_issues(after_state):
        return
    if after_state["formalization"].get("status") == "approval_pending":
        _accept_current_critic(paths)
    if len(after_state.get("critic_verdicts", [])) == before and after_state["phase"] == "critic":
        # Leave the gate open. Returning lets the existing outer loop count this
        # attempt, honor stop requests, and retry only while its budget remains.
        click.echo(
            f"critic attempt {attempt} ended without submitting a structured verdict; "
            "review remains incomplete"
        )


async def _run_critics(roster, paths, max_attempts: int | float) -> None:
    """Rotate critics, each with its own attempt budget for the current gate."""
    critics = [roster.primary] + [
        agent for agent in roster.agents
        if agent.name != roster.primary.name
    ]
    for critic in critics:
        attempt = 0
        while attempt < max_attempts:
            if stop_requested(paths.project_root):
                return
            current = solve_state.load_state(paths.forum)
            if solve_state.pending_replan(current) or solve_state.open_source_issues(current):
                return

            attempt += 1
            await _run_critic(
                roster, paths, critic=critic, attempt=attempt,
            )

            if stop_requested(paths.project_root):
                return
            after = solve_state.load_state(paths.forum)
            if (after["phase"] != "critic" or solve_state.pending_replan(after)
                    or solve_state.open_source_issues(after)):
                return

    raise click.ClickException(
        "every configured agent exhausted its critic attempts "
        "without completing the review"
    )


@click.command(name="solve")
@click.option("--continue", "continue_", is_flag=True, default=False,
              help="Resume the existing solving/formalization state.")
async def solve(continue_):
    """Solve a problem in natural language and verify the solution in Lean."""
    paths = load_paths()
    (paths.unity / "stop-requested").unlink(missing_ok=True)
    roster = load_roster(paths.agents_yaml, use_learned_strength=False)
    names = [solve_state.author_key(agent.name) for agent in roster.agents]
    if len(names) != len(set(names)):
        raise click.ClickException("solve agent names must be unique ignoring case")
    resume = resume_point(paths, "solve", continue_)
    if resume:
        click.echo(f"resuming from phase: {resume}")
    root = paths.project_root
    max_attempts = _attempt_limit()

    fresh = resume is None and not continue_
    if not fresh:
        solve_jobs.terminate(root)
        try:
            recover_interrupted_formal_merges(paths)
            solve_state.recover_source_repairs(paths.forum)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    if fresh:
        mark_phase("solve", "architect")
    persisted = solve_state.load_state(paths.forum) if not fresh else {}
    persisted_phase = persisted.get("phase")
    if (persisted_phase in {"formalizing", "critic", "complete"}
            and (persisted.get("formalization", {}).get("contract") or {}).get("version") not in {2, 3}):
        raise click.ClickException(
            "This older solve formalization has no source-bound specification for the new verifier. "
            "Its paper, proofs and state were preserved. Start a fresh solve run without --continue; "
            "legacy verification cannot be reused as new acceptance evidence."
        )
    _prepare_solve_environment(
        root, run_architect=fresh, validate_project=persisted_phase != "chunking",
    )

    problem = paths.unity_md.read_bytes() if paths.unity_md.exists() else b""
    problem_sha = hashlib.sha256(problem).hexdigest()
    reset = resume is None and not continue_
    if reset:
        reset_solve_workspace(paths)
    try:
        solve_state.initialize(
            paths.forum,
            problem_sha,
            worktree.main_commit(root),
            reset=reset,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    # Count solving/formalizing outer loops here. Chunking and critic helpers
    # enforce their own per-agent retry budgets, so MAX_ATTEMPTS=1 still permits
    # one complete happy-path solve run.
    attempts = {"solving": 0, "formalizing": 0}
    try:
        while not stop_requested(root):
            state = solve_state.load_state(paths.forum)
            phase = state.get("phase", "solving")
            if phase == "complete":
                break
            if phase in {"chunking", "formalizing", "critic"}:
                require_source_matches(paths, state)
            if phase in attempts and attempts[phase] >= max_attempts:
                raise click.ClickException(
                    f"solve exhausted MAX_ATTEMPTS={max_attempts} in the {phase} loop "
                    "before both gates were accepted"
                )

            if phase == "solving":
                await run_solving_runtime(
                    roster,
                    paths,
                    build_solve_mcp(paths, "solving"),
                    load_prompt("solve/SOLVING"),
                )
                attempts["solving"] += 1
                continue

            if phase == "solution_review":
                await _review_current_solution(roster, paths, max_attempts)
                continue

            if phase == "chunking":
                await _chunk_accepted_solution(roster, paths, max_attempts)
                continue

            if phase == "formalizing":
                before_round = (state["formalization"].get("last_round") or {}).get("round_id")
                state = await run_formalizing_runtime(
                    roster,
                    paths,
                    build_solve_formal_mcp(paths, "formalizing"),
                    load_prompt("solve/FORMALIZING"),
                )
                after_round = (state["formalization"].get("last_round") or {}).get("round_id")
                if after_round and after_round != before_round:
                    attempts["formalizing"] += 1
                if (not stop_requested(root) and state.get("phase") == "formalizing"
                        and not solve_state.pending_replan(state)
                        and not solve_state.open_source_issues(state)):
                    _prepare_critic_snapshot(paths)
                continue

            if phase == "critic":
                if solve_state.ready_source_issues(state):
                    state = await run_source_repairs(roster, paths, max_attempts)
                    if state.get("phase") != "critic":
                        continue
                request = solve_state.pending_replan(state)
                if request:
                    with _merge_lock(root):
                        solve_state.begin_replan(paths.forum, request["request_id"])
                    continue
                if solve_state.open_source_issues(state):
                    raise click.ClickException("Source issues remain unresolved; repair or reopen the accepted paper")
                await _run_critics(roster, paths, max_attempts)
                continue

            raise click.ClickException(f"unknown solve phase '{phase}'")

    except Exception:
        _save_incomplete_report(paths)
        raise

    if stop_requested(root):
        _save_incomplete_report(paths)
        click.echo("solve stopped safely; rerun with --continue to resume")
        return

    persist_report(paths, accepted=True)
    if _retrospective_enabled():
        await _run_retrospective(roster, paths)
    mark_done(paths, "solve")
    click.echo("solve complete: informal solution and Lean formalization accepted")


command = solve


def _save_incomplete_report(paths) -> None:
    try:
        persist_report(paths, accepted=False)
    except (OSError, ValueError) as exc:
        click.echo(f"Warning: could not persist incomplete solve formalization report: {exc}")
