"""Independent supplied-document autoformalization command and gate loops."""

import hashlib
import json
import os
from pathlib import Path
import tempfile

import asyncclick as click

from ..Architect import architect
from .. import lake, library, autoformalize_contract, autoformalize_jobs, autoformalize_state, worktree
from ..config import load_paths
from ..autoformalize_input import autoformalize_paths, require_source_matches, snapshot_sources
from ..autoformalize_orchestrator import (
    build_autoformalize_mcp, dispatch, load_prompt, mark_done, mark_phase,
    resume_point, stop_requested, toposort,
)
from ..roster import load_roster
from ..autoformalize_runtime import (
    configure_forum, forum_brief, recover_interrupted_formal_merges,
    run_formalizing_runtime, validate_formalization_dag, write_formalization_plan, _merge_lock,
    refresh_replanned_worktrees,
)
from ..autoformalize_repairs import run_source_repairs
from ..autoformalize_report import persist_report

PIPELINE = "autoformalize"


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
    run_id = autoformalize_state.load_state(paths.forum)["run_id"]
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
            [roster.primary], roster, load_prompt(f"{PIPELINE}/RETROSPECTIVE"),
            f"Distill reusable lessons from this completed {PIPELINE} run. Run ID: {run_id}. "
            f"Write library Markdown under {library_root}, using the existing tactics, lemmas, "
            "references, subagents, or skills directories. Preserve existing useful content. "
            f"Then write {report_path.resolve()} with exactly one of these JSON shapes:\n"
            + "\n".join(json.dumps(schema) for schema in schemas)
            + "\nEntry paths may be absolute or relative to the library root. Evidence must cite "
            "the actual checked run artifacts or source locations supporting each lesson. "
            "Do not compute file hashes; Unity records them after reading the saved files. "
            "Do not inspect Unity installation internals. Save the report and end the turn.",
            paths.project_root, build_autoformalize_mcp(paths, "retrospective"),
            tools_prompt=f"{PIPELINE.upper()}_RETROSPECTIVE_TOOLS", icrl_enabled=False,
            brief_provider=_brief_provider(paths, "retrospective"), mcp_profile="autoformalize",
            log_context={"command": PIPELINE, "run_id": run_id,
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


def _prepare_autoformalize_environment(
    root, *, run_architect: bool, validate_project: bool = True,
) -> None:
    """Refresh deterministic Lean state before any autoformalization worker is launched."""
    # Reap registered work left by an interrupted autoformalization before touching the
    # controller-owned shared package cache.
    autoformalize_jobs.terminate(root)
    if run_architect:
        architect(root)
    click.echo("Refreshing Mathlib build cache...")
    lake.cache_get(root)
    if validate_project:
        click.echo("Validating Lean project...")
        lake.build(root)


async def _chunk_source(
    roster, paths, max_attempts: int | float,
) -> None:
    """Create a valid semantic DAG, rotating chunkers after bounded failures."""
    state = autoformalize_state.load_state(paths.forum)
    candidate = autoformalize_state.formal_source(state)
    if not candidate:
        raise click.ClickException("chunking requires a bound formalization source")
    candidate_id = candidate["candidate_id"]
    require_source_matches(paths, state)
    source_instruction = "Read UNITY.md for scope and the exact supplied documents listed in "
    chunkers = [roster.primary] + [
        agent for agent in roster.agents if agent.name != roster.primary.name
    ]
    limit_label = "infinity" if max_attempts == float("inf") else str(int(max_attempts))
    failures: list[str] = []

    for chunker in chunkers:
        while not stop_requested(paths.project_root):
            current = autoformalize_state.load_state(paths.forum)
            used = autoformalize_state.chunking_attempt_count(current, candidate_id, chunker.name)
            if used >= max_attempts:
                break

            attempt = autoformalize_state.begin_chunking_attempt(
                paths.forum, candidate_id, chunker.name,
            )
            attempt_number = int(attempt["attempt"])
            plan = write_formalization_plan(paths, candidate)
            (paths.unity / "dag.json").unlink(missing_ok=True)
            prior = failures[-3:]
            prior_context = (
                " Previous failed attempts: " + " | ".join(prior)
                if prior else ""
            )
            results = await dispatch(
                [chunker],
                roster,
                load_prompt(f"{PIPELINE}/CHUNKING"),
                f"This is chunking attempt {attempt_number} of {limit_label} for `{chunker.name}`. "
                + source_instruction
                + f"the mechanical coverage scaffold at `{plan.relative_to(paths.project_root)}`. Its input SHA-256 is "
                f"`{candidate['sha256']}`. Produce `.unity/dag.json` bound to that candidate and hash, with "
                "explicit mathematical requirements, anchored scope/argument/prerequisite spec, and an acyclic graph. "
                "Create an elaboratable Lean scaffold for each chunk's exact declaration and complete "
                "meaning-bearing definitions. Only theorem proofs may remain as scaffold sorry holes. "
                "Use meaningful proof units, keeping tightly coupled steps together and splitting "
                "substantial independently useful work. Record genuine proof prerequisites, not "
                "paper order or shared-definition dependencies. Minimize scaffold imports using "
                "the installed min_imports tools. Read any repair proposals and replan context in the plan; "
                "preserve unchanged task IDs, signatures and completed proofs. Report source gaps with "
                "report_source_issue; propose justified local repairs without editing original source files."
                + prior_context,
                paths.project_root,
                build_autoformalize_mcp(paths, "chunking"),
                tools_prompt=f"{PIPELINE.upper()}_CHUNKING_TOOLS",
                icrl_enabled=False,
                brief_provider=_brief_provider(paths, "chunking"),
                mcp_profile="autoformalize",
                log_context={
                    "command": PIPELINE,
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
            if stop_requested(paths.project_root):
                return
            require_source_matches(paths, autoformalize_state.load_state(paths.forum))
            repaired = await run_source_repairs(roster, paths, max_attempts)
            if stop_requested(paths.project_root):
                return
            if any(issue.get("status") == "unresolved" for issue in autoformalize_state.open_source_issues(repaired)):
                autoformalize_state.finish_chunking_attempt(
                    paths.forum, attempt["attempt_id"], succeeded=False,
                    reason="source-repair attempts exhausted; original input and evidence preserved",
                )
                raise click.ClickException("Source-repair attempts exhausted; see source issues and repair evidence")
            try:
                dag = validate_formalization_dag(paths, candidate["sha256"])
                toposort(paths)
                with _merge_lock(paths.project_root):
                    current = autoformalize_state.load_state(paths.forum)
                    if (current["phase"] != "chunking"
                            or autoformalize_state.formal_source(current).get("candidate_id") != candidate_id):
                        return
                    contract = autoformalize_contract.freeze_formal_contract(paths, dag)
                    require_source_matches(paths, current)
                    revalidation = autoformalize_contract.carry_forward_revalidation(
                        paths, contract, current, dag["chunks"], dag["requirements"],
                    )
                    initialized = autoformalize_state.initialize_formal_tasks(
                        paths.forum,
                        dag["chunks"],
                        solution_candidate=candidate_id,
                        solution_sha256=candidate["sha256"],
                        main_sha=worktree.main_commit(paths.project_root),
                        requirements=dag["requirements"],
                        contract=contract,
                        revalidation=revalidation,
                    )
            except autoformalize_contract.ContractEnvironmentError as exc:
                autoformalize_state.finish_chunking_attempt(
                    paths.forum, attempt["attempt_id"], succeeded=False,
                    reason=f"environment failure: {exc}",
                )
                raise click.ClickException(
                    f"Contract environment check failed; not retrying chunking: {exc}"
                ) from exc
            except (OSError, ValueError) as exc:
                reason = str(exc)
                if dispatch_failure is not None:
                    reason = f"agent failure: {dispatch_failure!r}; {reason}"
                reason = reason[:2000]
                failures.append(f"{chunker.name} attempt {attempt_number}: {reason}")
                autoformalize_state.finish_chunking_attempt(
                    paths.forum, attempt["attempt_id"], succeeded=False, reason=reason,
                )
                (paths.unity / "dag.json").unlink(missing_ok=True)
                click.echo(
                    f"chunker {chunker.name} attempt {attempt_number}/{limit_label} failed: "
                    f"{reason[:500]}"
                )
                continue

            assignments = (current.get("replan") or {}).get("assignments", {})
            if assignments:
                refresh_replanned_worktrees(paths, assignments, initialized)
            autoformalize_state.finish_chunking_attempt(
                paths.forum,
                attempt["attempt_id"],
                succeeded=True,
                chunk_count=len(dag["chunks"]),
            )
            click.echo(
                f"created {len(dag['chunks'])} formalization task(s) from bound source "
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
        state = autoformalize_state.load_state(paths.forum)
        if not state["formalization"].get("contract"):
            raise click.ClickException(
                "this autoformalization run has no protected formal specification; request_rechunk before resuming review"
            )
        snapshot = state["formalization"].get("review_snapshot") or {}
        if not snapshot.get("passed") or not autoformalize_contract.snapshot_is_current(paths, state, snapshot):
            try:
                report = autoformalize_contract.verify_final_project(paths, state)
            except (OSError, ValueError) as exc:
                raise click.ClickException(f"mechanical critic gate could not verify this revision: {exc}") from exc
            if report["main_sha"] != state["formalization"]["main_sha"]:
                raise click.ClickException(
                    "main changed outside candidate integration; request_rechunk to establish a new reviewed specification"
                )
            autoformalize_state.record_review_snapshot(paths.forum, report)
            if not report["passed"]:
                autoformalize_state.reopen_after_machine_failure(paths.forum, report)
                click.echo("mechanical critic gate reopened formalization: " + "; ".join(report["issues"]))
                return False
        if state["phase"] != "critic":
            autoformalize_state.begin_critic(paths.forum)
    return True


def _accept_current_critic(paths) -> bool:
    """An LLM verdict is not authority to accept stale or edited sources."""
    if stop_requested(paths.project_root):
        return False
    with _merge_lock(paths.project_root):
        state = autoformalize_state.load_state(paths.forum)
        formal = state["formalization"]
        if (formal.get("status") != "approval_pending" or autoformalize_state.pending_replan(state)
                or autoformalize_state.open_source_issues(state)):
            return False
        snapshot = formal.get("review_snapshot") or {}
        if not autoformalize_contract.snapshot_is_current(paths, state, snapshot):
            # Recompute and require a new semantic review even when the new bytes
            # still pass machine checks. Never stamp old evidence with a new SHA.
            report = autoformalize_contract.verify_final_project(paths, state)
            if report["main_sha"] != formal["main_sha"]:
                raise click.ClickException(
                    "main changed during critic review; request_rechunk before acceptance"
                )
            autoformalize_state.record_review_snapshot(paths.forum, report)
            if not report["passed"]:
                autoformalize_state.reopen_after_machine_failure(paths.forum, report)
            click.echo("critic approval became stale; the changed revision needs a new review")
            return False
        autoformalize_state.complete_critic_review(
            paths.forum, snapshot["snapshot_id"], formal["pending_verdict_id"],
        )
        return True


async def _run_critic(roster, paths, *, critic, attempt: int = 1) -> None:
    """Run one attempt with the selected critic."""
    if not _prepare_critic_snapshot(paths):
        return
    if _accept_current_critic(paths):
        return
    state = autoformalize_state.load_state(paths.forum)
    before = len(autoformalize_state.load_state(paths.forum).get("critic_verdicts", []))
    retry_context = (
        f"This is critic attempt {attempt}. The gate is still open. Reading files or ending a turn "
        "without submitting a verdict does not complete the review. Refresh the current brief, "
        "check any remaining concerns, and submit the structured verdict before finishing. "
        if attempt > 1 else ""
    )
    await dispatch(
        [critic],
        roster,
        load_prompt(f"{PIPELINE}/CRITIC"),
        retry_context
        + "Audit the complete Lean project against the supplied source documents and UNITY.md scope. "
        + "Use the exact recorded "
        "machine snapshot for build, contract, and axiom status. Independently check requirement "
        "completeness and the mathematical meaning of statements and definitions against the source. "
        "Submit one structured verdict with submit_formalization_verdict and mandatory snapshot-bound "
        "per-requirement review evidence. Reopen only the exact Lean tasks that "
        "need repair. "
        + "Do not rewrite supplied documents or reopen solving. Report source defects with evidence; "
        "do not approve a changed or weakened result.",
        paths.project_root,
        build_autoformalize_mcp(paths, "critic"),
        tools_prompt=f"{PIPELINE.upper()}_CRITIC_TOOLS",
        icrl_enabled=False,
        brief_provider=_brief_provider(paths, "critic"),
        mcp_profile="autoformalize",
        log_context={"command": PIPELINE, "run_id": state.get("run_id"),
                     "phase": "critic", "role": "critic", "attempt": attempt},
    )
    after_state = autoformalize_state.load_state(paths.forum)
    if autoformalize_state.pending_replan(after_state) or autoformalize_state.open_source_issues(after_state):
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
    state = autoformalize_state.load_state(paths.forum)
    adopted = {key for row in (state["formalization"].get("spec") or {}).get("arguments", [])
               for key in row["repair_ids"]}
    repair_authors = {autoformalize_state.author_key(state["source_repairs"][key]["author"])
                      for key in adopted}
    critics = [critic for critic in critics if autoformalize_state.author_key(critic.name) not in repair_authors]
    if not critics:
        raise click.ClickException("No independent critic remains to review the adopted source repairs")

    for critic in critics:
        attempt = 0
        while attempt < max_attempts:
            if stop_requested(paths.project_root):
                return
            current = autoformalize_state.load_state(paths.forum)
            if autoformalize_state.pending_replan(current) or autoformalize_state.open_source_issues(current):
                return

            attempt += 1
            await _run_critic(
                roster, paths, critic=critic, attempt=attempt,
            )

            if stop_requested(paths.project_root):
                return
            after = autoformalize_state.load_state(paths.forum)
            if (after["phase"] != "critic" or autoformalize_state.pending_replan(after)
                    or autoformalize_state.open_source_issues(after)):
                return

    raise click.ClickException(
        "every configured agent exhausted its critic attempts "
        "without completing the review"
    )


@click.command(name="autoformalize")
@click.option("--continue", "continue_", is_flag=True, default=False,
              help="Resume the existing supplied-source formalization state.")
async def autoformalize(continue_):
    """Formalize documents added with `unity source add`; UNITY.md specifies scope."""
    paths = autoformalize_paths(load_paths())
    root = paths.project_root
    (paths.unity / "stop-requested").unlink(missing_ok=True)
    max_attempts = _attempt_limit()
    previous = autoformalize_state.load_state(paths.forum)
    resume = resume_point(paths, "autoformalize", continue_)
    if continue_ and (resume or autoformalize_state.state_path(paths.forum).exists()) and not previous.get("run_id"):
        raise click.ClickException(
            "legacy or unreadable autoformalize state cannot resume in the new runtime; rerun without --continue. "
            "Supplied source documents will be preserved."
        )
    fresh = not continue_ or not previous.get("run_id")
    if (not fresh and previous.get("phase") != "chunking"
            and (previous.get("formalization", {}).get("contract") or {}).get("version") != 2):
        raise click.ClickException(
            "This autoformalize run predates the source-evidence contract; rerun without --continue. "
            "Original sources and historical artifacts are preserved."
        )
    if not paths.unity_md.is_file():
        raise click.ClickException("Missing .unity/UNITY.md; initialize the project or restore its scope/instructions file")
    try:
        if fresh:
            source = snapshot_sources(paths)
        else:
            require_source_matches(paths, previous)
            source = previous["input_source"]
    except (OSError, ValueError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc

    roster = load_roster(paths.agents_yaml, use_learned_strength=False)
    names = [autoformalize_state.author_key(agent.name) for agent in roster.agents]
    if len(names) != len(set(names)):
        raise click.ClickException("autoformalize agent names must be unique ignoring case")
    if not fresh:
        autoformalize_jobs.terminate(root)
        try:
            recover_interrupted_formal_merges(paths)
            autoformalize_state.recover_source_repairs(paths.forum)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    if fresh:
        mark_phase("autoformalize", "architect")
    _prepare_autoformalize_environment(
        root, run_architect=fresh,
        validate_project=fresh or previous.get("phase") != "chunking",
    )
    scope = paths.unity_md.read_bytes() if paths.unity_md.exists() else b""
    try:
        autoformalize_state.initialize_source(
            paths.forum, hashlib.sha256(scope).hexdigest(), worktree.main_commit(root),
            source, reset=fresh,
        )
        if fresh:
            # Only generated planning files. In particular, never delete supplied
            # PROOF.tex, source/drafts, or other files imported by `source add`.
            for name in ("dag.json", "formalization-plan.json"):
                (paths.unity / name).unlink(missing_ok=True)
            for path in paths.forum.glob("autoformalize-*.json"):
                if path.name != "autoformalize-state.json":
                    path.unlink()

        attempts = 0
        while not stop_requested(root):
            state = autoformalize_state.load_state(paths.forum)
            require_source_matches(paths, state)
            phase = state["phase"]
            if phase == "complete":
                break
            # Critic tools only queue changes. No integration workers remain here,
            # so the command can safely dispatch repairs or enter replanning.
            if phase == "critic":
                if autoformalize_state.ready_source_issues(state):
                    state = await run_source_repairs(roster, paths, max_attempts)
                request = autoformalize_state.pending_replan(state)
                if request:
                    with _merge_lock(root):
                        autoformalize_state.begin_replan(paths.forum, request["request_id"])
                    continue
                if autoformalize_state.open_source_issues(state):
                    raise click.ClickException("Source issues remain unresolved after repair attempts; review is incomplete")
            if phase == "chunking":
                await _chunk_source(roster, paths, max_attempts)
            elif phase == "formalizing":
                if attempts >= max_attempts:
                    raise click.ClickException(
                        f"autoformalize exhausted MAX_ATTEMPTS={max_attempts} before formalization acceptance"
                    )
                state = await run_formalizing_runtime(
                    roster, paths, build_autoformalize_mcp(paths, "formalizing"),
                    load_prompt("autoformalize/FORMALIZING"),
                )
                attempts += 1
                if (state.get("phase") == "formalizing" and autoformalize_state.all_formal_tasks_complete(state)
                        and not autoformalize_state.open_source_issues(state)):
                    _prepare_critic_snapshot(paths)
            elif phase == "critic":
                await _run_critics(roster, paths, max_attempts)
            else:
                raise click.ClickException(f"unknown autoformalize phase '{phase}'")
    except (OSError, ValueError, click.ClickException) as exc:
        _save_incomplete_report(paths)
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc

    if stop_requested(root):
        _save_incomplete_report(paths)
        click.echo("autoformalize stopped safely; rerun with --continue to resume")
        return
    try:
        persist_report(paths, accepted=True)
    except (OSError, ValueError) as exc:
        _save_incomplete_report(paths)
        raise click.ClickException(f"Could not publish the accepted snapshot report: {exc}") from exc
    if _retrospective_enabled():
        await _run_retrospective(roster, paths)
    mark_done(paths, "autoformalize")
    click.echo("autoformalize complete: supplied-source Lean formalization accepted")
command = autoformalize


def _save_incomplete_report(paths) -> None:
    try:
        persist_report(paths, accepted=False)
    except (OSError, ValueError) as exc:
        click.echo(f"Warning: could not persist incomplete autoformalization report: {exc}")
