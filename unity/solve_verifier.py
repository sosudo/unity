"""Deterministic integration checks for candidates produced by ``unity solve``.

Unlike prove candidates, solve formalization candidates may add new declarations, so
there is no pre-existing declaration signature for ``candidate_review`` to compare.
This module therefore checks the exact committed diff, forbidden constructs, and the
project build.  Semantic faithfulness to the accepted informal solution remains the
formalization critic's job and is bound to the resulting main commit.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import artifacts, worktree


_FORBIDDEN_ADDITION = re.compile(
    r"\b(?:sorry|admit|native_decide|axiom)\b", re.IGNORECASE
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CANDIDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_JOURNAL_SCHEMA_VERSION = 1


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True, check=False,
    )


def _nonignored_untracked(project: Path) -> tuple[list[str], str]:
    """Return untracked project inputs that are not excluded by Git.

    ``.unity`` is run-scoped coordination state rather than project source, and may
    be present even when a host project does not ignore it. Every other
    non-ignored untracked path can affect Lake/Lean without being represented by
    the commit under verification, so it makes the checkout inexact.
    """
    result = _git(project, "ls-files", "--others", "--exclude-standard", "-z")
    if result.returncode != 0:
        return [], (
            result.stderr.strip() or result.stdout.strip()
            or "could not inspect non-ignored untracked project files"
        )
    paths = [path for path in result.stdout.split("\0") if path]
    return [
        path for path in paths
        if path != ".unity" and not path.startswith(".unity/")
    ], ""


def main_checkout_issues(project_root: Path) -> list[str]:
    """Describe tracked dirt and non-ignored untracked inputs in main.

    Ignored build caches remain allowed. Non-ignored files outside ``.unity`` do
    not: a successful build that reads them would not be reproducible from the
    exact Git commit recorded by the solve gate.
    """
    project = Path(project_root).resolve()
    tracked = _git(project, "status", "--porcelain", "--untracked-files=no")
    if tracked.returncode != 0:
        return [
            tracked.stderr.strip() or tracked.stdout.strip()
            or "<git status failed>"
        ]
    untracked, error = _nonignored_untracked(project)
    if error:
        return [error]
    return [*tracked.stdout.splitlines(), *(f"?? {path}" for path in untracked)]


@contextmanager
def _solve_merge_lock(project: Path):
    # Stable across fresh-run cleanup of `.unity/solve/`.
    lock = project / ".unity" / "solve-merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _reset(project: Path, revision: str) -> None:
    """Restore HEAD, index, and tracked worktree bytes or fail loudly."""
    reset = _git(project, "reset", "--hard", revision)
    if reset.returncode != 0:
        raise RuntimeError(
            reset.stderr.strip() or reset.stdout.strip()
            or f"could not restore main to {revision}"
        )
    head = _git(project, "rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != revision:
        raise RuntimeError(f"main did not return to expected revision {revision}")
    # ``git reset --hard`` can only restore tracked state. Non-ignored untracked
    # inputs are reported by the verifier and deliberately left for the user
    # rather than deleted as part of rollback.
    status = _git(project, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0 or status.stdout.strip():
        raise RuntimeError(f"main remained dirty after restoring {revision}")


def _journal_path(project: Path, candidate_id: str) -> Path:
    digest = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
    return project / ".unity" / "solve" / "integrations" / f"{digest}.json"


def _write_journal(project: Path, candidate_id: str, record: dict) -> Path:
    """Atomically persist and fsync one integration journal revision."""
    path = _journal_path(project, candidate_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **record,
        "schema_version": _JOURNAL_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "updated_at": time.time(),
    }
    fd, temporary = tempfile.mkstemp(prefix=".integration-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def _read_journal(project: Path, candidate_id: str) -> dict | None:
    path = _journal_path(project, candidate_id)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"integration journal for {candidate_id} is unreadable") from exc
    if (record.get("schema_version") != _JOURNAL_SCHEMA_VERSION
            or record.get("candidate_id") != candidate_id):
        raise RuntimeError(f"integration journal for {candidate_id} has invalid identity")
    return record


def _journal_identity_issue(journal: dict, candidate: dict) -> str:
    expected = {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "author": str(candidate.get("author") or ""),
        "candidate_commit": str(candidate.get("commit_sha") or "").lower(),
        "candidate_base": str(candidate.get("base_main_sha") or "").lower(),
        "candidate_diff_sha256": str(candidate.get("diff_sha256") or "").lower(),
        "solution_sha256": str(candidate.get("solution_sha256") or "").lower(),
        "gate_revision": candidate.get("gate_revision"),
    }
    for key, value in expected.items():
        if value in ("", None) or journal.get(key) != value:
            return f"integration journal {key} does not match the exact candidate"
    return ""


def _commit_field(project: Path, revision: str, field: str) -> str:
    result = _git(project, "show", "-s", f"--format={field}", revision)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"cannot inspect integration commit {revision}")
    return result.stdout.strip()


def _recovered_result(project: Path, candidate: dict, journal: dict, main_sha: str) -> dict:
    verification = {
        "status": "passed",
        "stage": "integrated_build_recovered",
        "candidate_id": journal["candidate_id"],
        "candidate_commit": journal["candidate_commit"],
        "candidate_base": journal["candidate_base"],
        "base_main_sha": journal["previous_main_sha"],
        "main_sha": main_sha,
        "gate_revision": journal.get("gate_revision"),
        "solution_sha256": journal.get("solution_sha256", ""),
        "diff_sha256": journal.get("candidate_diff_sha256", ""),
        "diff_artifact_id": journal.get("diff_artifact_id", ""),
        "build_returncode": 0,
        "build_artifact_id": journal.get("build_artifact_id", ""),
        "integration_journal": str(_journal_path(project, journal["candidate_id"])),
        "recovered": True,
        "issues": [],
    }
    record = _store_output(
        project,
        json.dumps(verification, indent=2, sort_keys=True) + "\n",
        kind="solve_formalization_verification",
        metadata={
            "candidate_id": journal["candidate_id"],
            "status": "passed",
            "main_sha": main_sha,
            "recovered": True,
        },
    )
    if record:
        verification["artifact_id"] = record["artifact_id"]
    completed = {
        **journal,
        "status": "complete",
        "integrated_main_sha": main_sha,
        "verification_artifact_id": verification.get("artifact_id", ""),
        "recovered_at": time.time(),
    }
    _write_journal(project, journal["candidate_id"], completed)
    return {
        "ok": True,
        "recovered": True,
        "main_sha": main_sha,
        "previous_main_sha": journal["previous_main_sha"],
        "verification": verification,
        "build": {
            "returncode": 0,
            **({"artifact_id": journal["build_artifact_id"]}
               if journal.get("build_artifact_id") else {}),
        },
    }


def _store_output(project: Path, content: str, *, kind: str, metadata: dict) -> dict | None:
    if not content:
        return None
    return artifacts.store_text(
        project / ".unity" / "artifacts",
        content,
        kind=kind,
        source="unity solve deterministic verifier",
        metadata=metadata,
    )


def _failure(
    candidate: dict,
    stage: str,
    issue: str,
    *,
    base_main_sha: str = "",
    artifact: dict | None = None,
    blocked: bool = False,
) -> dict:
    verification = {
        "status": "blocked" if blocked else "failed",
        "stage": stage,
        "candidate_id": candidate.get("candidate_id", ""),
        "candidate_commit": candidate.get("commit_sha", ""),
        "base_main_sha": base_main_sha,
        "gate_revision": candidate.get("gate_revision"),
        "solution_sha256": candidate.get("solution_sha256", ""),
        "issues": [issue],
    }
    if artifact:
        verification["artifact_id"] = artifact["artifact_id"]
    return {"ok": False, "blocked": blocked, "error": issue, "verification": verification}


def _accepted_solution_issue(project: Path, candidate: dict) -> str:
    """Return an identity error if the canonical paper is not the accepted bytes."""
    expected = str(candidate.get("solution_sha256") or "").strip().lower()
    if not _SHA256.fullmatch(expected):
        return "candidate is not bound to a valid accepted-solution SHA-256"
    source = project / ".unity" / "source" / "PROOF.tex"
    try:
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError as exc:
        return f"accepted solution source is unavailable: {exc}"
    if actual != expected:
        return "canonical PROOF.tex does not match the candidate's accepted solution revision"
    return ""


def recover_formalization_integration(project_root: Path, candidate: dict) -> dict:
    """Recover an exact candidate committed by Unity before state acknowledgement.

    This is read-only with respect to Git.  It returns ``ok=True`` only when the
    journal identity, integration commit trailers, parent commit, and pre-commit
    tree hash all match.  A successful recovery recreates the immutable verification
    artifact and marks the journal complete.
    """
    project = Path(project_root).resolve()
    candidate_id = str(candidate.get("candidate_id") or "")
    if not _CANDIDATE_ID.fullmatch(candidate_id):
        return {
            "ok": False, "status": "conflict",
            "error": "candidate id is invalid for integration recovery",
        }
    with _solve_merge_lock(project):
        try:
            journal = _read_journal(project, candidate_id)
        except RuntimeError as exc:
            return {"ok": False, "status": "conflict", "error": str(exc)}
        if journal is None:
            return {"ok": False, "status": "not_found"}
        issue = _journal_identity_issue(journal, candidate)
        if issue:
            return {"ok": False, "status": "conflict", "error": issue}

        previous = str(journal.get("previous_main_sha") or "")
        integrated = str(journal.get("integrated_main_sha") or "")
        current = worktree.main_commit(project)
        if journal.get("status") == "rolled_back":
            if current == previous:
                return {"ok": False, "status": "not_committed", "main_sha": current}
            return {
                "ok": False,
                "status": "conflict",
                "error": "main changed after the journaled integration rollback",
                "main_sha": current,
            }
        if not integrated and current == previous:
            return {"ok": False, "status": "not_committed", "main_sha": current}
        if not integrated and journal.get("status") == "verified":
            # The only unjournaled post-commit window is between `git commit` and
            # the immediately following committed-journal fsync. The merge lock
            # ensures HEAD cannot advance through another Unity integration here.
            integrated = current
        if not integrated:
            return {
                "ok": False, "status": "conflict", "main_sha": current,
                "error": "main changed while a prepared integration journal was pending",
            }
        if _git(project, "cat-file", "-e", f"{integrated}^{{commit}}").returncode != 0:
            return {
                "ok": False, "status": "conflict",
                "error": "journaled integration commit is missing", "main_sha": current,
            }
        if _git(project, "merge-base", "--is-ancestor", integrated, current).returncode != 0:
            return {
                "ok": False, "status": "conflict",
                "error": "journaled integration commit is not present on current main",
                "main_sha": current,
            }
        parents = _commit_field(project, integrated, "%P").split()
        if parents != [previous]:
            return {
                "ok": False, "status": "conflict",
                "error": "journaled integration commit has an unexpected parent",
                "main_sha": current,
            }
        tree = _commit_field(project, integrated, "%T")
        if tree != journal.get("integrated_tree_sha"):
            return {
                "ok": False, "status": "conflict",
                "error": "journaled integration tree differs from the verified tree",
                "main_sha": current,
            }
        body = _commit_field(project, integrated, "%B")
        required_trailers = (
            f"Unity-Solve-Candidate: {candidate_id}",
            f"Unity-Candidate-Commit: {journal['candidate_commit']}",
            f"Unity-Candidate-Diff-SHA256: {journal['candidate_diff_sha256']}",
            f"Unity-Solution-SHA256: {journal['solution_sha256']}",
        )
        if any(trailer not in body.splitlines() for trailer in required_trailers):
            return {
                "ok": False, "status": "conflict",
                "error": "integration commit trailers do not match the journal",
                "main_sha": current,
            }
        checkout_issues = main_checkout_issues(project)
        if checkout_issues:
            return {
                "ok": False, "status": "conflict",
                "error": (
                    "main has tracked or non-ignored untracked project inputs: "
                    + "; ".join(checkout_issues[:20])
                ),
                "main_sha": current,
            }
        try:
            return {
                "status": "recovered",
                **_recovered_result(project, candidate, journal, integrated),
            }
        except Exception as exc:
            return {
                "ok": False, "status": "conflict",
                "error": f"could not persist recovered verification: {exc}",
                "main_sha": current,
            }


def integrate_formalization_candidate(project_root: Path, candidate: dict) -> dict:
    """Apply and verify one immutable formalization candidate under a merge lock.

    The complete diff from the candidate's recorded base to its exact commit is applied
    to the current clean main checkout.  A successful result is already committed on
    main; all unsuccessful paths restore the checkout to its original revision.
    """
    project = Path(project_root).resolve()
    author = str(candidate.get("author") or "")
    candidate_id = str(candidate.get("candidate_id") or "")
    commit_sha = str(candidate.get("commit_sha") or "")
    if not _CANDIDATE_ID.fullmatch(candidate_id) or not author or not commit_sha:
        return _failure(
            candidate, "identity",
            "a safe candidate id, author, and commit SHA are required",
        )

    recovery = recover_formalization_integration(project, candidate)
    if recovery.get("ok"):
        return recovery
    if recovery.get("status") == "conflict":
        return _failure(candidate, "recovery", recovery.get("error", "integration journal conflict"),
                        base_main_sha=recovery.get("main_sha", ""), blocked=True)

    try:
        resolved = worktree.verify_candidate_commit(project, author, commit_sha)
    except (ValueError, RuntimeError) as exc:
        return _failure(candidate, "identity", str(exc))

    with _solve_merge_lock(project):
        current = worktree.main_commit(project)
        checkout_issues = main_checkout_issues(project)
        if checkout_issues:
            record = _store_output(
                project,
                "\n".join(checkout_issues) + "\n",
                kind="solve_merge_blocker",
                metadata={"candidate_id": candidate_id, "kind": "dirty_main"},
            )
            return _failure(
                candidate,
                "main_status",
                "main has tracked or non-ignored untracked project inputs; "
                "refusing formalization merge",
                base_main_sha=current,
                artifact=record,
                blocked=True,
            )

        solution_issue = _accepted_solution_issue(project, candidate)
        if solution_issue:
            return _failure(
                candidate, "solution_identity", solution_issue, base_main_sha=current,
            )

        recorded_base = str(candidate.get("base_main_sha") or "").strip()
        base = recorded_base or _git(project, "merge-base", current, resolved).stdout.strip()
        if not base or _git(project, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
            return _failure(candidate, "identity", "candidate base commit is missing", base_main_sha=current)
        if _git(project, "merge-base", "--is-ancestor", base, resolved).returncode != 0:
            return _failure(
                candidate,
                "identity",
                "candidate base is not an ancestor of the submitted commit",
                base_main_sha=current,
            )

        diff_result = _git(project, "diff", "--binary", "--no-ext-diff", base, resolved)
        if diff_result.returncode != 0 or not diff_result.stdout:
            return _failure(candidate, "diff", "candidate contains no applicable source diff", base_main_sha=current)
        diff_text = diff_result.stdout
        diff_sha256 = hashlib.sha256(diff_text.encode()).hexdigest()
        expected_diff = str(candidate.get("diff_sha256") or "")
        if expected_diff and expected_diff != diff_sha256:
            return _failure(candidate, "identity", "candidate diff hash does not match its submitted identity", base_main_sha=current)

        diff_record = _store_output(
            project,
            diff_text,
            kind="solve_formalization_diff",
            metadata={
                "candidate_id": candidate_id,
                "commit_sha": resolved,
                "base_commit": base,
                "sha256": diff_sha256,
            },
        )
        forbidden = []
        for line in diff_text.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            match = _FORBIDDEN_ADDITION.search(line[1:])
            if match:
                forbidden.append(match.group(0))
        if forbidden:
            return _failure(
                candidate,
                "source_policy",
                "candidate adds forbidden constructs: " + ", ".join(sorted(set(forbidden))),
                base_main_sha=current,
                artifact=diff_record,
            )

        journal = {
            "status": "prepared",
            "author": author,
            "task_id": str(candidate.get("task_id") or ""),
            "candidate_commit": resolved,
            "candidate_base": base,
            "candidate_diff_sha256": diff_sha256,
            "solution_sha256": str(candidate.get("solution_sha256") or ""),
            "gate_revision": candidate.get("gate_revision"),
            "previous_main_sha": current,
            "diff_artifact_id": diff_record["artifact_id"] if diff_record else "",
            "prepared_at": time.time(),
        }
        # This fsync is deliberately the final operation before the first Git mutation.
        _write_journal(project, candidate_id, journal)
        mutation_started = False
        commit_completed = False
        build_record = None
        try:
            mutation_started = True
            applied = subprocess.run(
                ["git", "apply", "--index", "--3way", "-"],
                cwd=project,
                input=diff_text,
                capture_output=True,
                text=True,
                check=False,
            )
            if applied.returncode != 0:
                output = (applied.stdout + "\n" + applied.stderr).strip()
                record = _store_output(
                    project,
                    output,
                    kind="solve_merge_conflict",
                    metadata={"candidate_id": candidate_id, "main_sha": current},
                )
                return _failure(
                    candidate,
                    "integration",
                    "candidate conflicts with current main",
                    base_main_sha=current,
                    artifact=record,
                    blocked=True,
                )

            applied_tree_result = _git(project, "write-tree")
            if applied_tree_result.returncode != 0 or not applied_tree_result.stdout.strip():
                return _failure(
                    candidate,
                    "integration",
                    applied_tree_result.stderr.strip()
                    or "could not identify the exact applied candidate tree",
                    base_main_sha=current,
                )
            applied_tree = applied_tree_result.stdout.strip()

            try:
                build = subprocess.run(
                    ["lake", "build"], cwd=project, capture_output=True, text=True, check=False,
                )
            except OSError as exc:
                return _failure(candidate, "build", f"could not run lake build: {exc}", base_main_sha=current)
            build_output = "\n".join(
                part.rstrip("\n") for part in (build.stdout, build.stderr) if part
            )
            build_record = _store_output(
                project,
                build_output,
                kind="solve_formalization_build",
                metadata={
                    "candidate_id": candidate_id,
                    "commit_sha": resolved,
                    "returncode": build.returncode,
                },
            )
            if build.returncode != 0:
                result = _failure(
                    candidate,
                    "build",
                    "lake build failed for the integrated candidate",
                    base_main_sha=current,
                    artifact=build_record,
                )
                result["build"] = {
                    "returncode": build.returncode,
                    **({"artifact_id": build_record["artifact_id"]} if build_record else {}),
                }
                return result

            # The paper lives in shared run state rather than Git. Check it again after
            # the potentially long build so a concurrent accidental edit cannot silently
            # change what this candidate formalizes.
            solution_issue = _accepted_solution_issue(project, candidate)
            if solution_issue:
                return _failure(
                    candidate, "solution_identity", solution_issue, base_main_sha=current,
                    artifact=build_record,
                )

            if _git(project, "diff", "--quiet").returncode != 0:
                return _failure(
                    candidate,
                    "build",
                    "lake build changed tracked files outside the candidate index",
                    base_main_sha=current,
                    artifact=build_record,
                )
            untracked, untracked_error = _nonignored_untracked(project)
            if untracked_error:
                return _failure(
                    candidate,
                    "build",
                    untracked_error,
                    base_main_sha=current,
                    artifact=build_record,
                )
            if untracked:
                return _failure(
                    candidate,
                    "build",
                    "lake build produced non-ignored untracked project inputs absent "
                    "from the candidate commit: " + ", ".join(untracked[:20]),
                    base_main_sha=current,
                    artifact=build_record,
                )
            tree_result = _git(project, "write-tree")
            if tree_result.returncode != 0 or not tree_result.stdout.strip():
                return _failure(
                    candidate, "integration",
                    tree_result.stderr.strip() or "could not identify the verified integration tree",
                    base_main_sha=current,
                )
            integrated_tree = tree_result.stdout.strip()
            if integrated_tree != applied_tree:
                return _failure(
                    candidate,
                    "build",
                    "lake build changed the staged candidate tree",
                    base_main_sha=current,
                    artifact=build_record,
                )
            journal.update(
                status="verified",
                integrated_tree_sha=integrated_tree,
                build_artifact_id=build_record["artifact_id"] if build_record else "",
                verified_at=time.time(),
            )
            _write_journal(project, candidate_id, journal)

            message = (
                f"UNITY: formalize {candidate_id}\n\n"
                f"Unity-Solve-Candidate: {candidate_id}\n"
                f"Unity-Candidate-Commit: {resolved}\n"
                f"Unity-Candidate-Diff-SHA256: {diff_sha256}\n"
                f"Unity-Solution-SHA256: {journal['solution_sha256']}"
            )
            committed = _git(project, "commit", "--no-verify", "-m", message)
            if committed.returncode != 0:
                return _failure(
                    candidate,
                    "commit",
                    committed.stderr.strip() or "could not commit formalization candidate",
                    base_main_sha=current,
                )
            commit_completed = True
            main_sha = worktree.main_commit(project)
            journal.update(
                status="committed",
                integrated_main_sha=main_sha,
                committed_at=time.time(),
            )
            # Persist as soon as Git can report the new immutable commit identity.
            _write_journal(project, candidate_id, journal)
            committed_tree = _commit_field(project, main_sha, "%T")
            if committed_tree != integrated_tree:
                raise RuntimeError("integration commit tree differs from the verified index")

            verification = {
                "status": "passed",
                "stage": "integrated_build",
                "candidate_id": candidate_id,
                "candidate_commit": resolved,
                "candidate_base": base,
                "base_main_sha": current,
                "main_sha": main_sha,
                "gate_revision": candidate.get("gate_revision"),
                "solution_sha256": candidate.get("solution_sha256", ""),
                "diff_sha256": diff_sha256,
                "diff_artifact_id": diff_record["artifact_id"] if diff_record else "",
                "build_returncode": build.returncode,
                "build_artifact_id": build_record["artifact_id"] if build_record else "",
                "integration_journal": str(_journal_path(project, candidate_id)),
                "issues": [],
            }
            review_record = _store_output(
                project,
                json.dumps(verification, indent=2, sort_keys=True) + "\n",
                kind="solve_formalization_verification",
                metadata={"candidate_id": candidate_id, "status": "passed", "main_sha": main_sha},
            )
            if review_record:
                verification["artifact_id"] = review_record["artifact_id"]
            journal.update(
                status="complete",
                verification_artifact_id=verification.get("artifact_id", ""),
                completed_at=time.time(),
            )
            _write_journal(project, candidate_id, journal)
            return {
                "ok": True,
                "main_sha": main_sha,
                "previous_main_sha": current,
                "verification": verification,
                "build": {
                    "returncode": build.returncode,
                    **({"artifact_id": build_record["artifact_id"]} if build_record else {}),
                },
            }
        finally:
            if mutation_started and not commit_completed:
                _reset(project, current)


def rollback_formalization_integration(project_root: Path, result: dict) -> dict:
    """Undo exactly one just-integrated candidate if authoritative state invalidated it.

    Verification can take long enough for a source-fix event to reopen the solution
    gate.  The runtime detects that when it tries to record acceptance.  This helper
    only rewinds when main is still the exact commit produced by this verifier and is
    otherwise clean, so it can never discard a later integration or user edit.
    """
    project = Path(project_root).resolve()
    integrated = str(result.get("main_sha") or "")
    previous = str(result.get("previous_main_sha") or "")
    if not integrated or not previous:
        return {"ok": False, "error": "integration result lacks rollback identity"}
    with _solve_merge_lock(project):
        current = worktree.main_commit(project)
        if current != integrated:
            return {
                "ok": False,
                "error": "main advanced after the stale integration; refusing rollback",
                "main_sha": current,
            }
        checkout_issues = main_checkout_issues(project)
        if checkout_issues:
            return {
                "ok": False,
                "error": "main changed after the stale integration; refusing rollback",
                "main_sha": current,
            }
        if _git(project, "cat-file", "-e", f"{previous}^{{commit}}").returncode != 0:
            return {"ok": False, "error": "rollback base commit is missing", "main_sha": current}
        _reset(project, previous)
        candidate_id = str((result.get("verification") or {}).get("candidate_id") or "")
        if _CANDIDATE_ID.fullmatch(candidate_id):
            try:
                journal = _read_journal(project, candidate_id)
                if journal is not None:
                    journal.update(
                        status="rolled_back",
                        rolled_back_from=integrated,
                        rolled_back_to=previous,
                        rolled_back_at=time.time(),
                    )
                    _write_journal(project, candidate_id, journal)
            except RuntimeError as exc:
                return {
                    "ok": False,
                    "error": f"main was restored but rollback journal could not be updated: {exc}",
                    "main_sha": previous,
                    "rolled_back_sha": integrated,
                }
        return {"ok": True, "main_sha": previous, "rolled_back_sha": integrated}
