"""Compiler-driven declaration migration using Bump's own copied gate loops."""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import tempfile

import asyncclick as click

from .. import library, bump_contract, bump_jobs, bump_state, bump_project, bump_scope
from .. import bump_worktree as worktree
from ..config import load_paths
from ..bump_input import bump_paths, require_source_matches, snapshot_sources, scope_bytes
from ..bump_orchestrator import (
    build_bump_mcp, dispatch, load_prompt, mark_done, mark_phase,
    resume_point, stop_requested,
)
from ..roster import load_roster
from ..bump_runtime import (
    configure_forum, forum_brief, recover_interrupted_formal_merges,
    run_formalizing_runtime, write_formalization_plan, _merge_lock,
    refresh_replanned_worktrees,
)
from ..bump_repairs import run_source_repairs
from ..bump_report import persist_report

PIPELINE = "bump"


def _formal_round_attempted(state: dict, previous_round: str | None) -> bool:
    """An empty scheduler pass is a blocker, not a spent proof attempt."""
    summary = state.get("formalization", {}).get("last_round") or {}
    if not summary.get("round_id") or summary["round_id"] == previous_round:
        return False
    if summary.get("outcome") == "blocked":
        pending = ", ".join(row["task_id"] for row in summary.get("pending_tasks", [])) or "unknown"
        reasons = "; ".join(summary.get("blocked_launches", {}).values())
        raise click.ClickException(
            f"bump formalization blocked: no worker, review, repair or integration was dispatched; "
            f"pending tasks: {pending}. "
            + (reasons + ". " if reasons else "")
            + "State and proof work were preserved. Inspect formalization.last_round in bump-state.json; "
              "no proof attempt was charged and no unchanged critic snapshot was requested."
        )
    # Older recorded rounds lack activity metadata; retain their prior accounting.
    return "activity" not in summary or any(summary["activity"].values())


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
    run_id = bump_state.load_state(paths.forum)["run_id"]
    report_path = paths.forum / "retrospective.json"
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
            worktree.role_view(paths.project_root, "retrospective"),
            build_bump_mcp(paths, "retrospective"),
            tools_prompt=f"{PIPELINE.upper()}_RETROSPECTIVE_TOOLS", icrl_enabled=False,
            brief_provider=_brief_provider(paths, "retrospective"), mcp_profile="bump",
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


def _prepare_bump_environment(
    root, *, validate_project: bool = True, baseline: dict | None = None,
) -> None:
    """Build the existing pinned project; never bootstrap or update its packages."""
    bump_jobs.terminate(root)
    if validate_project:
        click.echo("Validating Lean project...")
        scope_options = {"baseline": baseline} if baseline is not None else {}
        result = bump_contract.build_sources(
            root, full=True, task_id="resume-preflight", **scope_options)
        if result["returncode"]:
            raise ValueError("existing project failed validation: " + result["output"][-3000:])


async def _chunk_source(roster, paths, max_attempts: int | float) -> None:
    """Rotate failed executions; correct proposals inside their existing session."""
    from .. import artifacts
    from ..bump_chunking import chunking_workspace
    from ..bump_input import store_bytes
    from ..bump_runtime import (
        read_chunking_draft, prepare_chunking_draft, seed_chunking_draft, chunking_diagnostic,
    )

    state = bump_state.load_state(paths.forum)
    candidate = bump_state.formal_source(state)
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
            current = bump_state.load_state(paths.forum)
            if bump_state.chunking_attempt_count(current, candidate["candidate_id"], chunker.name) >= max_attempts:
                break
            attempt = bump_state.begin_chunking_attempt(paths.forum, candidate["candidate_id"], chunker.name)
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
                        current = bump_state.load_state(paths.forum)
                        try:
                            require_source_matches(paths, current)
                        except ValueError as exc:
                            raise click.ClickException(str(exc)) from exc
                        repaired = await run_source_repairs(roster, paths, max_attempts)
                        if stop_requested(paths.project_root):
                            return None
                        if any(row.get("status") == "unresolved"
                               for row in bump_state.open_source_issues(repaired)):
                            raise click.ClickException("Source-repair attempts exhausted; original input and evidence preserved")
                        write_formalization_plan(paths, candidate)
                        current = bump_state.load_state(paths.forum)
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
                                record = store_bytes(paths.artifacts, payload, kind="bump_chunking_draft",
                                                     producer=chunker.name, source=attempt["attempt_id"])
                                artifact_id = record["artifact_id"]
                            bump_state.record_chunking_feedback(
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
                            contract = bump_contract.prepare_source_contract(paths, dag, state=current)
                        except (OSError, ValueError) as exc:
                            raise click.ClickException("Contract environment check failed: " + str(exc)) from exc
                        old_environment = (current["formalization"].get("contract") or {}).get("environment")
                        if old_environment is not None and old_environment != contract["environment"]:
                            raise click.ClickException("Protected Lean environment changed; not retrying chunking")
                        with _merge_lock(paths.project_root):
                            latest = bump_state.load_state(paths.forum)
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
                                kind="bump_accepted_plan", producer="Unity", source=attempt["attempt_id"],
                            )
                            try:
                                installed = bump_state.initialize_informal_plan(
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
                            artifacts._atomic_write(paths.forum / "dag.json",
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
                        results = await dispatch(
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
                                        kind="bump_chunking_draft", producer=chunker.name,
                                        source=attempt["attempt_id"])
                                    bump_state.save_chunking_draft(
                                        paths.forum, attempt["attempt_id"], record["artifact_id"],
                                    )
                                except Exception as exc:
                                    workspace.preserve = True
                                    raise click.ClickException(
                                        f"Cannot archive chunking draft; retained at {workspace.draft_path}: {exc}"
                                    ) from exc
                    if stop_requested(paths.project_root):
                        return
                    error = next((result for result in results if isinstance(result, Exception)), None)
                    if error is not None:
                        raise error
                    if installed is None:
                        raise RuntimeError("chunker execution ended without controller plan publication")
            except click.ClickException:
                if installed is None:
                    bump_state.finish_chunking_attempt(paths.forum, attempt["attempt_id"],
                        succeeded=False, reason="controller/source/environment failure; see terminal diagnostic")
                raise
            except Exception as exc:
                if installed is not None:
                    raise click.ClickException("Plan accepted but chunker cleanup failed: " + str(exc)) from exc
                if not execution_started:
                    raise click.ClickException("Cannot start chunker workspace: " + str(exc)) from exc
                # Only genuinely failed executions reach here, never schema corrections.
                reason = f"{type(exc).__name__}: {exc}"[:2000]
                bump_state.finish_chunking_attempt(paths.forum, attempt["attempt_id"],
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
    bump_state.record_chunking_exhausted(paths.forum, summary)
    raise click.ClickException("every configured agent exhausted its chunking executions: " + summary)


def _prepare_critic_snapshot(paths) -> bool:
    """Cache exact mechanical evidence for final or diagnostic critic retries."""
    with _merge_lock(paths.project_root):
        state = bump_state.load_state(paths.forum)
        if not state["formalization"].get("contract"):
            raise click.ClickException(
                "this formalization run has no protected formal specification; request_rechunk before resuming review"
            )
        snapshot = state["formalization"].get("review_snapshot") or {}
        if not bump_contract.snapshot_is_current(paths, state, snapshot, require_complete=False):
            try:
                report = bump_contract.verify_final_project(paths, state)
            except (OSError, ValueError) as exc:
                raise click.ClickException(f"mechanical critic gate could not verify this revision: {exc}") from exc
            if report["main_sha"] != state["formalization"]["main_sha"]:
                raise click.ClickException(
                    "main changed outside candidate integration; request_rechunk to establish a new reviewed specification"
                )
            bump_state.record_review_snapshot(paths.forum, report)
            snapshot = report
        if state["phase"] != "critic":
            bump_state.begin_critic(paths.forum, diagnostic=not snapshot["passed"])
    return True


def _accept_current_critic(paths) -> bool:
    """An LLM verdict is not authority to accept stale or edited sources."""
    if stop_requested(paths.project_root):
        return False
    with _merge_lock(paths.project_root):
        state = bump_state.load_state(paths.forum)
        formal = state["formalization"]
        if (formal.get("status") != "approval_pending" or bump_state.pending_replan(state)
                or bump_state.open_source_issues(state)):
            return False
        snapshot = formal.get("review_snapshot") or {}
        if not bump_contract.snapshot_is_current(paths, state, snapshot):
            # Recompute and require a new semantic review even when the new bytes
            # still pass machine checks. Never stamp old evidence with a new SHA.
            report = bump_contract.verify_final_project(paths, state)
            if report["main_sha"] != formal["main_sha"]:
                raise click.ClickException(
                    "main changed during critic review; request_rechunk before acceptance"
                )
            bump_state.record_review_snapshot(paths.forum, report)
            click.echo("critic approval became stale; the changed revision needs a new review")
            return False
        bump_state.complete_critic_review(
            paths.forum, snapshot["snapshot_id"], formal["pending_verdict_id"],
        )
        return True


async def _run_critic(roster, paths, *, critic, attempt: int = 1) -> None:
    """Run one attempt with the selected critic."""
    if not _prepare_critic_snapshot(paths):
        return
    if _accept_current_critic(paths):
        return
    state = bump_state.load_state(paths.forum)
    diagnostic = not state["formalization"]["review_snapshot"]["passed"]
    before = len(bump_state.load_state(paths.forum).get("critic_verdicts", []))
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
        + "Do not rewrite supplied documents or reopen solving. Report source defects with evidence; "
        "do not approve a changed or weakened result.",
        worktree.role_view(paths.project_root, "critic"),
        build_bump_mcp(paths, "critic"),
        tools_prompt=f"{PIPELINE.upper()}_CRITIC_TOOLS",
        icrl_enabled=False,
        brief_provider=_brief_provider(paths, "critic"),
        mcp_profile="bump",
        log_context={"command": PIPELINE, "run_id": state.get("run_id"),
                     "phase": "critic", "role": "critic", "attempt": attempt},
    )
    after_state = bump_state.load_state(paths.forum)
    if bump_state.pending_replan(after_state) or bump_state.open_source_issues(after_state):
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
    state = bump_state.load_state(paths.forum)
    adopted = {key for row in (state["formalization"].get("spec") or {}).get("arguments", [])
               for key in row["repair_ids"]}
    repair_authors = {bump_state.author_key(state["source_repairs"][key]["author"])
                      for key in adopted}
    critics = [critic for critic in critics if bump_state.author_key(critic.name) not in repair_authors]
    if not critics:
        raise click.ClickException("No independent critic remains to review the adopted source repairs")

    for critic in critics:
        attempt = 0
        while attempt < max_attempts:
            if stop_requested(paths.project_root):
                return
            current = bump_state.load_state(paths.forum)
            if bump_state.pending_replan(current) or bump_state.open_source_issues(current):
                return

            attempt += 1
            await _run_critic(
                roster, paths, critic=critic, attempt=attempt,
            )

            if stop_requested(paths.project_root):
                return
            after = bump_state.load_state(paths.forum)
            if (after["phase"] != "critic" or bump_state.pending_replan(after)
                    or bump_state.open_source_issues(after)):
                return

    raise click.ClickException(
        "every configured agent exhausted its critic attempts "
        "without completing the review"
    )


@click.command(name="bump")
@click.argument("version", required=False)
@click.option("--dependency", "dependency_values", multiple=True, help="Exact target dependency NAME=COMMIT or NAME=VERSION.")
@click.option("--continue", "continue_", is_flag=True, default=False, help="Resume this saved declaration migration.")
@click.option("--project-scope", type=click.Choice(["build", "all"]), default=None,
              help="Original native default targets/import closure (build), or all original project modules.")
@click.option("--architect", type=click.Choice(["auto", "off"]), default=None,
              help="Optionally install the exact target-version LeanArchitect release.")
async def bump(version=None, dependency_values=(), continue_=False, project_scope=None, architect=None):
    """Migrate an existing Lean project using declaration-level compiler repairs."""
    from .. import bump_bootstrap
    paths = bump_paths(load_paths())
    paths.forum.mkdir(parents=True, exist_ok=True)
    with (paths.forum / "controller.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise click.ClickException("another Bump controller is already running in this project") from exc
        try:
            pins = bump_bootstrap.parse_dependency_pins(dependency_values)
            await _run_bump(paths, continue_, version, pins, project_scope=project_scope, architect=architect)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def _run_bump(source_paths, continue_, version=None, dependency_pins=None, *, project_scope=None, architect=None):
    from .. import bump_bootstrap
    max_attempts = _attempt_limit()
    saved_cwd = Path.cwd()
    paths = None
    try:
        if continue_:
            paths = bump_bootstrap.resume(source_paths, version, dependency_pins,
                                           project_scope=project_scope, architect=architect)
        else:
            if not version:
                raise click.ClickException("Fresh Bump requires an exact target Lean version.")
            bump_bootstrap._json(source_paths.unity / "state.json",
                                 {"command": "bump", "phase": "preparing"})
            (source_paths.unity / "stop-requested").unlink(missing_ok=True)
            paths = bump_bootstrap.prepare(source_paths, version, dependency_pins or {},
                                           project_scope=project_scope or "build", architect=architect or "auto")
        root = paths.project_root
        os.chdir(root)
        roster = load_roster(paths.agents_yaml, use_learned_strength=False)
        names = [bump_state.author_key(agent.name) for agent in roster.agents]
        if len(names) != len(set(names)):
            raise click.ClickException("bump agent names must be unique ignoring case")
        if continue_:
            bump_jobs.terminate(root)
            recover_interrupted_formal_merges(paths)
            bump_state.recover_source_repairs(paths.forum)
        if continue_:
            (paths.unity / "stop-requested").unlink(missing_ok=True)
        elif (source_paths.unity / "stop-requested").exists():
            (paths.unity / "stop-requested").touch()
        bump_bootstrap._json(source_paths.unity / "state.json",
                             {"command": "bump", "phase": bump_state.load_state(paths.forum)["phase"]})
        await _run_migration_loop(roster, paths, max_attempts)
        final_state = bump_state.load_state(paths.forum)
        bump_bootstrap._json(source_paths.unity / "state.json",
                             {"command": "bump", "phase": "done" if final_state["phase"] == "complete" else final_state["phase"]})
    except BaseException:
        bump_bootstrap._json(source_paths.unity / "state.json", {"command": "bump", "phase": "stopped"})
        raise
    finally:
        os.chdir(saved_cwd)


async def _run_migration_loop(roster, paths, max_attempts):
    """Keep the copied Formalize phase/retry loop after compiler task discovery."""
    root = paths.project_root
    try:
        attempts = 0
        while not stop_requested(root):
            state = bump_state.load_state(paths.forum)
            require_source_matches(paths, state)
            phase = state["phase"]
            if phase == "complete":
                break
            # Critic tools only queue changes. No integration workers remain here,
            # so the command can safely dispatch repairs or enter replanning.
            if phase == "critic":
                if bump_state.ready_source_issues(state):
                    state = await run_source_repairs(roster, paths, max_attempts)
                request = bump_state.pending_replan(state)
                if request:
                    with _merge_lock(root):
                        bump_state.begin_replan(paths.forum, request["request_id"])
                    continue
                if bump_state.open_source_issues(state):
                    raise click.ClickException("Source issues remain unresolved after repair attempts; review is incomplete")
            if phase == "chunking":
                await _chunk_source(roster, paths, max_attempts)
            elif phase == "formalizing":
                if attempts >= max_attempts:
                    raise click.ClickException(
                        f"bump exhausted MAX_ATTEMPTS={max_attempts} before formalization acceptance"
                    )
                before_round = (state["formalization"].get("last_round") or {}).get("round_id")
                state = await run_formalizing_runtime(
                    roster, paths, build_bump_mcp(paths, "formalizing"),
                    load_prompt("bump/FORMALIZING"),
                )
                # Replans/cancellation interrupt a round; they do not complete
                # one. The drained runtime is the authority on round endings.
                if (not stop_requested(root) and state.get("phase") == "formalizing"
                        and _formal_round_attempted(state, before_round)):
                    attempts += 1
                if (not stop_requested(root) and state.get("phase") == "formalizing"
                        and not bump_state.pending_replan(state)
                        and not bump_state.open_source_issues(state)):
                    _prepare_critic_snapshot(paths)
            elif phase == "critic":
                await _run_critics(roster, paths, max_attempts)
            else:
                raise click.ClickException(f"unknown bump phase '{phase}'")
    except (OSError, ValueError, click.ClickException) as exc:
        _save_incomplete_report(paths)
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc

    if stop_requested(root):
        _save_incomplete_report(paths)
        click.echo("bump stopped safely; rerun with --continue to resume")
        return
    try:
        persist_report(paths, accepted=True)
    except (OSError, ValueError) as exc:
        _save_incomplete_report(paths)
        raise click.ClickException(f"Could not publish the accepted snapshot report: {exc}") from exc
    if _retrospective_enabled():
        await _run_retrospective(roster, paths)
        # A retrospective is not permission to change the verified project.
        # Rebind the final handoff after that optional worker has finished.
        try:
            persist_report(paths, accepted=True)
        except (OSError, ValueError) as exc:
            _save_incomplete_report(paths)
            raise click.ClickException(f"Project changed after acceptance; evidence preserved: {exc}") from exc
    mark_done(paths, "bump")
    click.echo("bump complete: selected original Lean declarations preserved and migration accepted")
command = bump


def _save_incomplete_report(paths) -> None:
    try:
        persist_report(paths, accepted=False)
    except (OSError, ValueError) as exc:
        click.echo(f"Warning: could not persist incomplete formalization report: {exc}")
