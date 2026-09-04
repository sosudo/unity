"""``unity solve``: solve a problem in English, then formalize that solution."""

import hashlib
import os

import asyncclick as click

from ..Architect import architect
from .. import solve_state, worktree
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
)


def _retrospective_enabled() -> bool:
    return os.getenv("RETROSPECTIVE", "true").strip().lower() != "false"


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
                "one concrete Lean declaration per chunk, source-component coverage, and an acyclic graph. "
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
                solve_state.initialize_formal_tasks(
                    paths.forum,
                    dag["chunks"],
                    solution_candidate=candidate_id,
                    solution_sha256=candidate["sha256"],
                    main_sha=worktree.main_commit(paths.project_root),
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


async def _run_critic(roster, paths) -> None:
    """Run the final fidelity/correctness gate in a fresh primary-agent turn."""
    state = solve_state.load_state(paths.forum)
    if state["phase"] != "critic":
        solve_state.begin_critic(paths.forum)
    before = len(solve_state.load_state(paths.forum).get("critic_verdicts", []))
    await dispatch(
        [roster.primary],
        roster,
        load_prompt("solve/CRITIC"),
        "Audit the complete Lean project against the exact accepted PROOF.tex. Use the exact recorded "
        "machine verification for build and kernel status, then independently check theorem statements, "
        "dependencies, prohibited shortcuts, and mathematical fidelity. Submit one "
        "structured verdict with submit_formalization_verdict. Reopen only the exact Lean tasks that "
        "need repair, or reopen informal solving if the accepted mathematics is substantively wrong.",
        paths.project_root,
        build_solve_mcp(paths, "critic"),
        tools_prompt="SOLVE_CRITIC_TOOLS",
        icrl_enabled=False,
        brief_provider=_brief_provider(paths, "critic"),
        mcp_profile="solve",
        log_context={"command": "solve", "run_id": state.get("run_id"),
                     "phase": "critic", "role": "critic"},
    )
    after_state = solve_state.load_state(paths.forum)
    if len(after_state.get("critic_verdicts", [])) == before and after_state["phase"] == "critic":
        raise click.ClickException("critic ended without submitting a structured verdict")


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

    if resume is None and not continue_:
        mark_phase("solve", "architect")
        architect(root)

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

    # MAX_ATTEMPTS limits retries of each substantive worker loop. Mechanical
    # transitions (review and chunking) do not consume an attempt, so
    # MAX_ATTEMPTS=1 still permits one complete happy-path solve run.
    attempts = {"solving": 0, "formalizing": 0, "critic": 0}
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
                solve_state.begin_critic(paths.forum)
            attempts["formalizing"] += 1
            continue

        if phase == "critic":
            await _run_critic(roster, paths)
            attempts["critic"] += 1
            continue

        raise click.ClickException(f"unknown solve phase '{phase}'")

    if stop_requested(root):
        click.echo("solve stopped safely; rerun with --continue to resume")
        return

    if _retrospective_enabled():
        configure_forum(paths, "retrospective")
        await dispatch(
            [roster.primary],
            roster,
            load_prompt("solve/RETROSPECTIVE"),
            "Distill lessons from the complete informal-solving and Lean-formalization run into "
            "the reusable Unity library.",
            root,
            build_solve_mcp(paths, "retrospective"),
            tools_prompt="SOLVE_RETROSPECTIVE_TOOLS",
            icrl_enabled=False,
            brief_provider=_brief_provider(paths, "retrospective"),
            mcp_profile="solve",
            log_context={"command": "solve", "run_id": solve_state.load_state(paths.forum).get("run_id"),
                         "phase": "retrospective", "role": "retrospective"},
        )
    mark_done(paths, "solve")
    click.echo("solve complete: informal solution and Lean formalization accepted")


command = solve
