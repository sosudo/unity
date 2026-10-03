"""Formalize-derived Bump runtime and snapshot-bound critic command."""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import tempfile

import asyncclick as click

from .. import library, bump_contract, bump_jobs, bump_state, bump_project, bump_bootstrap
from .. import bump_worktree as worktree
from ..config import load_paths
from ..bump_input import require_source_matches
from ..bump_orchestrator import (
    build_bump_mcp, dispatch, load_prompt, mark_done, mark_phase,
    resume_point, stop_requested,
)
from ..roster import load_roster
from ..bump_runtime import (
    configure_forum, forum_brief, recover_interrupted_formal_merges,
    run_formalizing_runtime, _merge_lock,
)
from ..bump_report import persist_report
from ..bump_provider import BumpTransportRetriesExhausted

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
        return 5
    try:
        value = int(raw)
    except ValueError as exc:
        raise click.ClickException("MAX_ATTEMPTS must be a positive integer (blank defaults to 5)") from exc
    if value < 1:
        raise click.ClickException("MAX_ATTEMPTS must be a positive integer (blank defaults to 5)")
    return value


def _brief_provider(paths, profile: str):
    return lambda author: forum_brief(paths, profile, author)


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
           "Audit the migrated project against frozen original sources/native inventories and the pinned version transition. ")
        + "Use the exact recorded "
        "machine snapshot for build, contract, and axiom status. Independently check requirement "
        "completeness and the mathematical meaning of statements and definitions against the source. "
        "Submit one structured verdict with submit_formalization_verdict and mandatory snapshot-bound "
        "per-requirement review evidence. Reopen only the exact Lean tasks that "
        "need repair. "
        + "Do not rewrite original project obligations or request paper/source repairs. Report migration discrepancies with evidence; "
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
    critics = [critic for critic in critics if getattr(critic, "backend", None) == "codex"
               and bump_state.author_key(critic.name) not in repair_authors]
    if not critics:
        raise click.ClickException("No independent Codex critic remains for the verified read-only review")

    _prepare_critic_snapshot(paths)
    transport_blocked = {}
    for critic in critics:
        while True:
            current = bump_state.load_state(paths.forum)
            binding = (current["formalization"].get("review_snapshot") or {}).get("snapshot_id")
            if _critic_attempt_count(current, binding, critic.name) >= max_attempts:
                break
            if stop_requested(paths.project_root):
                return
            current = bump_state.load_state(paths.forum)
            if bump_state.pending_replan(current) or bump_state.open_source_issues(current):
                return

            attempt = _begin_critic_attempt(paths, binding, critic.name)
            try:
                await _run_critic(
                    roster, paths, critic=critic, attempt=attempt,
                )
            except BumpTransportRetriesExhausted as exc:
                transport_blocked[critic.name] = str(exc)
                click.echo(f"critic {critic.name} is transport-blocked; trying the next eligible configured critic")
                break

            if stop_requested(paths.project_root):
                return
            after = bump_state.load_state(paths.forum)
            if (after["phase"] != "critic" or bump_state.pending_replan(after)
                    or bump_state.open_source_issues(after)):
                return

    if transport_blocked:
        raise click.ClickException(
            "critic review remains incomplete: transport retries exhausted for "
            + ", ".join(transport_blocked)
            + "; remaining eligible critics did not complete review. No verdict was invented."
        )
    raise click.ClickException(
        "every configured agent exhausted its critic attempts "
        "without completing the review"
    )



def _critic_attempt_count(state, binding, author):
    return sum(row.get("snapshot_id") == binding and row.get("author") == bump_state.author_key(author)
               for row in state.get("migration_critic_attempts", []))


def _begin_critic_attempt(paths, binding, author):
    if not binding:
        raise ValueError("Critic attempts require an exact machine snapshot")
    with bump_state.transaction(paths.forum) as state:
        if (state["formalization"].get("review_snapshot") or {}).get("snapshot_id") != binding:
            raise ValueError("Critic snapshot changed before dispatch")
        count = _critic_attempt_count(state, binding, author) + 1
        state.setdefault("migration_critic_attempts", []).append(
            {"snapshot_id": binding, "author": bump_state.author_key(author), "attempt": count})
    return count


def _charge_completed_round(paths):
    with bump_state.transaction(paths.forum) as state:
        seen = state.setdefault("migration_runtime_rounds", [])
        summary = state["formalization"].get("last_round") or {}
        if summary.get("round_id") not in seen and _formal_round_attempted(state, None):
            seen.append(summary["round_id"])
            state["migration_attempts"] = state.get("migration_attempts", 0) + 1
        return state.get("migration_attempts", 0)


@click.command(name="bump")
@click.argument("version", required=False)
@click.option("--dependency", "dependency_values", multiple=True, metavar="NAME=REV",
              help="Exact requested Git dependency revision; unrelated pins stay frozen.")
@click.option("--continue", "continue_", is_flag=True, default=False,
              help="Continue the preserved private migration and its attempt budgets.")
@click.option("--project-scope", type=click.Choice(["build", "all"]), default=None,
              help="Fresh runs default to the configured build module closure; all requires every local Lean file. Continuation preserves the sealed scope.")
@click.option("--architect", type=click.Choice(["auto", "off"]), default=None,
              help="Fresh runs try a target-version-matched optional LeanArchitect package; off disables it. Cannot change on continuation.")
async def bump(version=None, dependency_values=(), continue_=False, project_scope=None, architect=None):
    """Migrate an existing Lean project through the Formalize-derived runtime."""
    paths = load_paths()
    controller = paths.unity / "bump"
    controller.mkdir(parents=True, exist_ok=True)
    with (controller / "controller.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise click.ClickException("another Bump controller is already running") from exc
        try:
            if continue_ and architect is not None:
                raise click.ClickException("--continue cannot change optional instrumentation")
            await _run_bump(paths, continue_, version, bump_bootstrap.parse_dependency_pins(dependency_values),
                            project_scope=project_scope, architect=architect)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def _run_bump(source_paths, continue_, version=None, dependency_pins=None, *, project_scope=None, architect=None):
    max_attempts = _attempt_limit()
    roster = load_roster(source_paths.agents_yaml, use_learned_strength=False)
    names = [bump_state.author_key(agent.name) for agent in roster.agents]
    if len(names) != len(set(names)):
        raise click.ClickException("bump agent names must be unique ignoring case")
    if not any(getattr(agent, "backend", None) == "codex" for agent in roster.agents):
        raise click.ClickException("Bump requires a Codex agent for its verified read-only critic")
    if not continue_:
        (source_paths.unity / "stop-requested").unlink(missing_ok=True)
    prepare_options = {"project_scope": project_scope or "build"}
    if architect is not None:
        prepare_options["architect"] = architect
    paths = (bump_bootstrap.resume(source_paths, version, dependency_pins, project_scope=project_scope)
             if continue_ else bump_bootstrap.prepare(source_paths, version, dependency_pins or {}, **prepare_options))
    root = paths.project_root
    state = bump_state.load_state(paths.forum)
    if continue_:
        if state.get("migration_max_attempts") != max_attempts:
            raise click.ClickException("--continue cannot reset or change the saved Bump attempt policy")
        bump_jobs.terminate(root)
        recover_interrupted_formal_merges(paths)
    else:
        with bump_state.transaction(paths.forum) as saved:
            saved["migration_max_attempts"] = max_attempts
    if continue_:
        (paths.unity / "stop-requested").unlink(missing_ok=True)
        (source_paths.unity / "stop-requested").unlink(missing_ok=True)
    previous_cwd = Path.cwd()
    os.chdir(root)
    try:
        while not stop_requested(root):
            state = bump_state.load_state(paths.forum)
            require_source_matches(paths, state)
            if state["phase"] == "complete":
                break
            if bump_state.pending_replan(state) or bump_state.open_source_issues(state):
                raise click.ClickException(
                    "Bump cannot rewrite frozen original-project obligations. "
                    "The requested replan/source change is preserved; migration remains incomplete.")
            if state["phase"] == "formalizing":
                state = bump_bootstrap.check_ready_modules(paths)
                if bump_state.all_formal_tasks_complete(state):
                    _prepare_critic_snapshot(paths)
                    continue
                if _charge_completed_round(paths) >= max_attempts:
                    raise click.ClickException(f"bump exhausted MAX_ATTEMPTS={max_attempts} before migration acceptance")
                before = (state["formalization"].get("last_round") or {}).get("round_id")
                state = await run_formalizing_runtime(
                    roster, paths, build_bump_mcp(paths, "formalizing"), load_prompt("bump/FORMALIZING"))
                if (not stop_requested(root) and state.get("phase") == "formalizing"
                        and _formal_round_attempted(state, before)):
                    _charge_completed_round(paths)
                if (not stop_requested(root) and state.get("phase") == "formalizing"
                        and not bump_state.pending_replan(state) and not bump_state.open_source_issues(state)):
                    # A changed source can expose further compiler failures.
                    # Refresh the repair DAG before considering final review;
                    # a vanished error is not a preservation receipt.
                    if (state["formalization"].get("contract") or {}).get("migration_policy") == 2:
                        state = bump_bootstrap.check_ready_modules(paths)
                        if not bump_state.all_formal_tasks_complete(state):
                            continue
                    _prepare_critic_snapshot(paths)
            elif state["phase"] == "critic":
                await _run_critics(roster, paths, max_attempts)
            else:
                raise click.ClickException(
                    f"Unsupported Bump phase '{state['phase']}'; native module obligations cannot be replaced by paper chunking")
        if stop_requested(root):
            _save_incomplete_report(paths)
            click.echo(f"bump stopped safely; work retained at {root}; use --continue")
            return
        persist_report(paths, accepted=True)
        if _retrospective_enabled():
            await _run_retrospective(roster, paths)
            persist_report(paths, accepted=True)
        mark_done(paths, "bump")
        click.echo(f"bump complete: migration accepted in {root}; original checkout unchanged")
    except (OSError, ValueError, RuntimeError, click.ClickException) as exc:
        _save_incomplete_report(paths)
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc
    finally:
        os.chdir(previous_cwd)


command = bump


def _save_incomplete_report(paths):
    try:
        persist_report(paths, accepted=False)
    except (OSError, ValueError) as exc:
        click.echo(f"Warning: could not persist incomplete Bump report: {exc}")
