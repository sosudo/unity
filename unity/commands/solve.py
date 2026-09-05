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
    toposort,
)
from ..roster import load_roster
from ..solve_runtime import (
    configure_forum,
    forum_brief,
    materialize_solution,
    reset_solve_workspace,
    run_formalizing_runtime,
    run_solving_runtime,
    validate_formalization_dag,
    write_formalization_plan,
    _merge_lock,
)


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


def _prepare_solve_environment(root, *, run_architect: bool) -> None:
    """Refresh deterministic Lean state before any solve worker is launched."""
    # Reap registered work left by an interrupted solve before touching the
    # controller-owned shared package cache.
    solve_jobs.terminate(root)
    if run_architect:
        architect(root)
    click.echo("Refreshing Mathlib build cache...")
    lake.cache_get(root)
    click.echo("Validating Lean project...")
    lake.build(root)


async def _review_current_solution(roster, paths) -> bool:
    """Run the independent semantic gate for the exact submitted paper."""
    state = solve_state.load_state(paths.forum)
    candidate_id = state["solution"].get("current_candidate")
    candidate = state["solution_candidates"].get(candidate_id or "")
    if not candidate or candidate.get("status") != "review":
        raise click.ClickException("solution-review phase has no reviewable candidate")

    quorum = _review_quorum()
    existing = {review["author"] for review in candidate.get("reviews", [])}
    approvals = sum(review.get("verdict") == "approve" for review in candidate.get("reviews", []))
    reviewers = sorted(
        (
            agent for agent in roster.agents
            if agent.name != candidate["author"] and agent.name not in existing
        ),
        key=lambda agent: -agent.strength,
    )
    needed = max(0, quorum - approvals)
    if needed and len(reviewers) < needed:
        raise click.ClickException(
            f"solution candidate needs {quorum} independent review(s), but the roster has "
            f"only {len(reviewers) + approvals} available"
        )

    if needed:
        await dispatch(
            reviewers[:needed],
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
                         "phase": "solution_review", "role": "reviewer"},
        )

    state = solve_state.load_state(paths.forum)
    candidate = state["solution_candidates"][candidate_id]
    objections = [review for review in candidate.get("reviews", []) if review.get("verdict") == "object"]
    approvals = [review for review in candidate.get("reviews", []) if review.get("verdict") == "approve"]
    if objections:
        reason = " | ".join(review.get("review", "") for review in objections)
        solve_state.reject_solution_candidate(paths.forum, candidate_id, "Unity", reason)
        click.echo(f"solution candidate {candidate_id} rejected: {reason[:500]}")
        return False
    if len(approvals) < quorum:
        click.echo(f"solution candidate {candidate_id} did not receive a review; retrying review")
        return False

    accepted = solve_state.accept_solution_candidate(paths.forum, candidate_id, "Unity")
    materialize_solution(paths, accepted)
    click.echo(f"accepted informal solution {candidate_id} ({accepted['sha256']})")
    return True


async def _chunk_accepted_solution(roster, paths, max_attempts: int | float) -> None:
    """Create a valid semantic DAG, rotating chunkers after bounded failures."""
    state = solve_state.load_state(paths.forum)
    candidate_id = state["solution"].get("accepted_candidate")
    candidate = state["solution_candidates"].get(candidate_id or "")
    if not candidate:
        raise click.ClickException("chunking requires an accepted solution candidate")
    proof = materialize_solution(paths, candidate)
    plan = write_formalization_plan(paths, candidate)
    chunkers = [roster.primary] + [
        agent for agent in roster.agents if agent.name != roster.primary.name
    ]
    limit_label = "infinity" if max_attempts == float("inf") else str(int(max_attempts))
    failures: list[str] = []

    for chunker in chunkers:
        while not stop_requested(paths.project_root):
            current = solve_state.load_state(paths.forum)
            used = solve_state.chunking_attempt_count(current, candidate_id, chunker.name)
            if used >= max_attempts:
                break

            attempt = solve_state.begin_chunking_attempt(
                paths.forum, candidate_id, chunker.name,
            )
            attempt_number = int(attempt["attempt"])
            (paths.unity / "dag.json").unlink(missing_ok=True)
            prior = failures[-3:]
            prior_context = (
                " Previous failed attempts: " + " | ".join(prior)
                if prior else ""
            )
            results = await dispatch(
                [chunker],
                roster,
                load_prompt("solve/CHUNKING"),
                f"This is chunking attempt {attempt_number} of {limit_label} for `{chunker.name}`. "
                f"Read the exact accepted paper at `{proof.relative_to(paths.project_root)}` and the "
                f"mechanical coverage scaffold at `{plan.relative_to(paths.project_root)}`. Its SHA-256 is "
                f"`{candidate['sha256']}`. Produce `.unity/dag.json` bound to that candidate and hash, with "
                "explicit mathematical requirements, source-component coverage, and an acyclic graph. "
                "Create an elaboratable Lean scaffold for each chunk's exact declaration and complete "
                "meaning-bearing definitions. Only theorem proofs may remain as scaffold sorry holes. "
                "Prefer one final-theorem chunk for a short single-result proof; introduce helpers only when "
                "Lean implementation or useful parallelism actually requires them."
                + prior_context,
                paths.project_root,
                build_solve_mcp(paths, "chunking"),
                tools_prompt="SOLVE_CHUNKING_TOOLS",
                icrl_enabled=False,
                brief_provider=_brief_provider(paths, "chunking"),
                mcp_profile="solve",
                log_context={
                    "command": "solve",
                    "run_id": state.get("run_id"),
                    "phase": "chunking",
                    "role": "chunker",
                    "candidate_id": candidate_id,
                    "attempt": attempt_number,
                },
            )
            dispatch_failure = next(
                (result for result in results if isinstance(result, Exception)), None,
            )
            try:
                dag = validate_formalization_dag(paths, candidate["sha256"])
                toposort(paths)
                with _merge_lock(paths.project_root):
                    current = solve_state.load_state(paths.forum)
                    if (current["phase"] != "chunking"
                            or current["solution"].get("accepted_candidate") != candidate_id):
                        return
                    contract = solve_contract.freeze_formal_contract(paths, dag)
                    solve_state.initialize_formal_tasks(
                        paths.forum,
                        dag["chunks"],
                        solution_candidate=candidate_id,
                        solution_sha256=candidate["sha256"],
                        main_sha=worktree.main_commit(paths.project_root),
                        requirements=dag["requirements"],
                        contract=contract,
                    )
            except (OSError, ValueError) as exc:
                reason = str(exc)
                if dispatch_failure is not None:
                    reason = f"agent failure: {dispatch_failure!r}; {reason}"
                reason = reason[:2000]
                failures.append(f"{chunker.name} attempt {attempt_number}: {reason}")
                solve_state.finish_chunking_attempt(
                    paths.forum, attempt["attempt_id"], succeeded=False, reason=reason,
                )
                (paths.unity / "dag.json").unlink(missing_ok=True)
                click.echo(
                    f"chunker {chunker.name} attempt {attempt_number}/{limit_label} failed: "
                    f"{reason[:500]}"
                )
                continue

            solve_state.finish_chunking_attempt(
                paths.forum,
                attempt["attempt_id"],
                succeeded=True,
                chunk_count=len(dag["chunks"]),
            )
            click.echo(
                f"created {len(dag['chunks'])} formalization task(s) from accepted solution "
                f"using {chunker.name} on attempt {attempt_number}"
            )
            return

    if stop_requested(paths.project_root):
        return
    summary = " | ".join(failures[-10:]) or "attempt limits were already exhausted"
    raise click.ClickException(
        "every configured agent exhausted its chunking attempts without a valid DAG: "
        + summary
    )


def _prepare_critic_snapshot(paths) -> bool:
    """Controller-only mechanical gate, cached for unchanged critic retries."""
    with _merge_lock(paths.project_root):
        state = solve_state.load_state(paths.forum)
        if not state["formalization"].get("contract"):
            raise click.ClickException(
                "this solve run has no protected formal specification; request_rechunk before resuming review"
            )
        snapshot = state["formalization"].get("review_snapshot") or {}
        if not snapshot.get("passed") or not solve_contract.snapshot_is_current(paths, state, snapshot):
            try:
                report = solve_contract.verify_final_project(paths, state)
            except (OSError, ValueError) as exc:
                raise click.ClickException(f"mechanical critic gate could not verify this revision: {exc}") from exc
            if report["main_sha"] != state["formalization"]["main_sha"]:
                raise click.ClickException(
                    "main changed outside candidate integration; request_rechunk to establish a new reviewed specification"
                )
            solve_state.record_review_snapshot(paths.forum, report)
            if not report["passed"]:
                solve_state.reopen_after_machine_failure(paths.forum, report)
                click.echo("mechanical critic gate reopened formalization: " + "; ".join(report["issues"]))
                return False
        if state["phase"] != "critic":
            solve_state.begin_critic(paths.forum)
    return True


def _accept_current_critic(paths) -> bool:
    """An LLM verdict is not authority to accept stale or edited sources."""
    if stop_requested(paths.project_root):
        return False
    with _merge_lock(paths.project_root):
        state = solve_state.load_state(paths.forum)
        formal = state["formalization"]
        if formal.get("status") != "approval_pending":
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
            if not report["passed"]:
                solve_state.reopen_after_machine_failure(paths.forum, report)
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
    before = len(solve_state.load_state(paths.forum).get("critic_verdicts", []))
    retry_context = (
        f"This is critic attempt {attempt}. The gate is still open. Reading files or ending a turn "
        "without submitting a verdict does not complete the review. Refresh the current brief, "
        "check any remaining concerns, and submit the structured verdict before finishing. "
        if attempt > 1 else ""
    )
    await dispatch(
        [critic],
        roster,
        load_prompt("solve/CRITIC"),
        retry_context
        + "Audit the complete Lean project against the exact accepted PROOF.tex. Use the exact recorded "
        "machine snapshot for build, contract, and axiom status. Independently check requirement "
        "completeness and the mathematical meaning of statements and definitions against the source. "
        "Submit one structured verdict with submit_formalization_verdict and mandatory snapshot-bound "
        "per-requirement review evidence. Reopen only the exact Lean tasks that "
        "need repair, or reopen informal solving if the accepted mathematics is substantively wrong.",
        paths.project_root,
        build_solve_mcp(paths, "critic"),
        tools_prompt="SOLVE_CRITIC_TOOLS",
        icrl_enabled=False,
        brief_provider=_brief_provider(paths, "critic"),
        mcp_profile="solve",
        log_context={"command": "solve", "run_id": state.get("run_id"),
                     "phase": "critic", "role": "critic", "attempt": attempt},
    )
    after_state = solve_state.load_state(paths.forum)
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

            attempt += 1
            await _run_critic(
                roster, paths, critic=critic, attempt=attempt,
            )

            if stop_requested(paths.project_root):
                return
            if solve_state.load_state(paths.forum)["phase"] != "critic":
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
    resume = resume_point(paths, "solve", continue_)
    if resume:
        click.echo(f"resuming from phase: {resume}")
    root = paths.project_root
    max_attempts = _attempt_limit()

    fresh = resume is None and not continue_
    if fresh:
        mark_phase("solve", "architect")
    _prepare_solve_environment(root, run_architect=fresh)

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
    while not stop_requested(root):
        state = solve_state.load_state(paths.forum)
        phase = state.get("phase", "solving")
        if phase == "complete":
            break
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
            await _review_current_solution(roster, paths)
            continue

        if phase == "chunking":
            await _chunk_accepted_solution(roster, paths, max_attempts)
            continue

        if phase == "formalizing":
            state = await run_formalizing_runtime(
                roster,
                paths,
                build_solve_mcp(paths, "formalizing"),
                load_prompt("solve/FORMALIZING"),
            )
            if state.get("phase") == "formalizing" and solve_state.all_formal_tasks_complete(state):
                _prepare_critic_snapshot(paths)
            attempts["formalizing"] += 1
            continue

        if phase == "critic":
            await _run_critics(roster, paths, max_attempts)
            continue

        raise click.ClickException(f"unknown solve phase '{phase}'")

    if stop_requested(root):
        click.echo("solve stopped safely; rerun with --continue to resume")
        return

    if _retrospective_enabled():
        await _run_retrospective(roster, paths)
    mark_done(paths, "solve")
    click.echo("solve complete: informal solution and Lean formalization accepted")


command = solve
