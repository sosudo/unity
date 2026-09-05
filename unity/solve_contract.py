"""Solve-only formal contracts and exact-source mechanical review snapshots.

These checks preserve a formal specification. They do not establish that its
English interpretation is correct. The semantic critic remains responsible for
that judgment. No printed-expression or textual-discovery fallback is allowed.

This is not an adversarial Lean sandbox or an external proof checker. It assumes
a trusted toolchain/dependency installation; arbitrary elaborator I/O outside the
recorded project inputs is not isolated. Structural identity is intentionally
conservative and may reject harmless refactors, which require re-chunking.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

from . import artifacts, solve_jobs, solve_workspace


AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})
_EXCLUDED = {".git", ".lake", ".unity", ".worktrees", "lake-packages", "build", "__pycache__"}
_CONFIGS = {"lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git inspection failed")
    return result.stdout.strip()


def source_files(root: Path) -> list[Path]:
    """Include untracked Lean sources too; Git HEAD alone is not a build identity."""
    result = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED)
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("project source directories must not be symlinks")
        for name in sorted(names):
            if name in _EXCLUDED:
                continue
            path = Path(directory) / name
            if path.suffix == ".lean" or (path.parent == root and name in _CONFIGS):
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"contract source must not be a symlink: {path}")
                result.append(path)
    return sorted(result)


def _file_hashes(root: Path, *, build_dir: str | None = None) -> dict:
    # Lean elaboration can read non-Lean inputs (e.g. include_str). Include all
    # local non-runtime inputs, even when untracked or ignored by Git.
    result = {}
    output = (root / build_dir).resolve() if build_dir else None
    if output is not None and (output == root.resolve() or not output.is_relative_to(root.resolve())):
        raise ValueError("project build directory must be strictly inside the project")
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED
                         and (output is None or (Path(directory) / d).resolve() != output))
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("contract input directories must not be symlinks")
        for name in sorted(names):
            if name in _EXCLUDED:
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"contract input must be a regular file: {path}")
            hashed = hashlib.sha256()
            with path.open("rb") as file:
                for block in iter(lambda: file.read(1024 * 1024), b""):
                    hashed.update(block)
            result[str(path.relative_to(root))] = hashed.hexdigest()
    return result


def _dependencies(root: Path) -> dict:
    """Pin actual dependency source bytes, including local/path dependencies.

    Build output is excluded. Do not trust the manifest revision alone when a
    shared package checkout can have local edits.
    """
    manifest_path = root / "lake-manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("cannot inspect Lake dependency manifest") from exc
    package_dir = root / manifest.get("packagesDir", ".lake/packages")
    result = {}
    for package in manifest.get("packages", []):
        name = package["name"]
        if package.get("type") == "path":
            directory = root / package["dir"]
        else:
            directory = package_dir / name
        directory = directory.resolve()
        if not directory.is_dir():
            raise ValueError(f"missing dependency source: {name}")
        result[name] = {"path": str(directory), "sources": digest(_file_hashes(directory))}
    return result


def environment_identity(root: Path) -> dict:
    version = solve_jobs.run(root, ["lake", "env", "lean", "--version"], cwd=root,
                             owner="Unity", task_id="contract", serialize_build=True)
    if version.returncode:
        raise ValueError("cannot determine Lean toolchain identity")
    return {
        "lean_version": version.stdout.strip(),
        "config": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in sorted(_CONFIGS) if (root / name).exists()},
        "dependencies": _dependencies(root),
    }


def source_identity(root: Path) -> dict:
    layout = workspace_layout(root)
    return {"main_sha": _git(root, "rev-parse", "HEAD"),
            "source_sha256": digest(_file_hashes(root, build_dir=layout["build_dir"])),
            "environment": environment_identity(root)}


def workspace_layout(root: Path) -> dict:
    files = [str(path.relative_to(root)) for path in source_files(root)
             if path.suffix == ".lean" and path.name != "lakefile.lean"]
    data = solve_workspace.discover(root, files)
    if data.get("issues") or not isinstance(data.get("modules"), dict):
        raise ValueError("Lake module inspection returned errors")
    return data


def workspace_modules(root: Path) -> dict[str, str]:
    return workspace_layout(root)["modules"]


def module_for_file(root: Path, filename: str, modules: dict | None = None) -> str:
    path = Path(filename)
    if (path.is_absolute() or path.suffix != ".lean" or ".." in path.parts
            or any(part in _EXCLUDED for part in path.parts)
            or any(part.startswith("-") for part in path.parts)
            or path.name == "lakefile.lean"):
        raise ValueError(f"invalid formalization source file: {filename}")
    if not (root / path).is_file():
        raise ValueError(f"missing formalization scaffold: {filename}")
    modules = workspace_modules(root) if modules is None else modules
    if filename not in modules:
        raise ValueError(f"source file is not owned by a configured Lake library/executable: {filename}")
    return modules[filename]


def inspect_environment(root: Path, tasks: list[dict]) -> dict:
    # Import every project module: generated/private dependencies are inspected by
    # the Lean helper, not filtered through the web blueprint presentation model.
    modules = sorted(set(workspace_modules(root).values()))
    names = [task["lean_decl"] for task in tasks]
    if not names or not modules:
        raise ValueError("formal contract has no declarations/modules")
    result = solve_jobs.run(
        root, ["lake", "env", "lean", "--run", str(Path(__file__).with_suffix(".lean")),
               *modules, "--", *names], cwd=root, owner="Unity", task_id="contract",
        serialize_build=True,
    )
    if result.returncode:
        raise ValueError("formal contract inspection failed: " +
                         artifacts.preview_text(result.stderr or result.stdout, 2000))
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ValueError("formal contract inspector did not return valid JSON") from exc
    if data.get("issues") or set(data.get("targets", {})) != set(names):
        raise ValueError("formal contract inspection incomplete: " + str(data.get("issues", [])))
    if not isinstance(data.get("project_axioms"), list) or not isinstance(data.get("project_sorries"), list):
        raise ValueError("formal contract inspector omitted project-wide axiom/placeholder audit")
    return data


def inspect_declarations(root: Path, tasks: list[dict]) -> dict:
    return inspect_environment(root, tasks)["targets"]


def _semantic_record(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "axioms"}


def build_sources(root: Path, *, full: bool = False) -> dict:
    layout = workspace_layout(root)
    modules = sorted(set(layout["modules"].values()))
    auxiliary = digest({name: value for name, value in _file_hashes(root, build_dir=layout["build_dir"]).items()
                        if Path(name).suffix != ".lean"})
    receipt_path = root / ".unity" / "solve-build-inputs.json"
    receipt = {"auxiliary": auxiliary, "modules": layout["modules"]}
    try:
        previous = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        previous = None
    if previous != receipt:
        # include_str/custom elaboration inputs are not necessarily Lake trace
        # dependencies. Invalidate only the exact root-project module traces;
        # never clean shared Mathlib/dependency build directories.
        directory = (root / layout["build_dir"]).resolve()
        if directory == root.resolve() or not directory.is_relative_to(root.resolve()):
            raise ValueError("project build directory must be strictly inside the project")
        for filename in layout["traces"].values():
            trace = root / filename
            if trace.suffix != ".trace" or not trace.resolve().is_relative_to(directory):
                raise ValueError("unsafe project module trace path")
            trace.unlink(missing_ok=True)
    # Named module facets guarantee freshness even if a lakefile's default target
    # does not include the theorem module. The full default build is optional.
    commands = ([["lake", "build"]] if full else []) + (
        [["lake", "--rehash", "build", *[f"+{module}" for module in modules]]] if modules else [])
    outputs = []
    for command in commands:
        result = solve_jobs.run(root, command, cwd=root, owner="Unity", task_id="contract",
                                serialize_build=True)
        outputs.append(" ".join(command) + "\n" + result.stdout + "\n" + result.stderr)
        if result.returncode:
            return {"returncode": result.returncode, "output": "\n".join(outputs)}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt_path.with_name(f".solve-build-inputs-{uuid.uuid4().hex}.json")
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    os.replace(temporary, receipt_path)
    return {"returncode": 0, "output": "\n".join(outputs)}


def freeze_formal_contract(paths, dag: dict) -> dict:
    """Build the chunker's scaffold and freeze its meaning before proof search."""
    root = paths.project_root
    chunks = dag["chunks"]
    modules = workspace_modules(root)
    for chunk in chunks:
        module_for_file(root, chunk.get("lean_file", ""), modules)
    before = source_identity(root)
    build = build_sources(root)
    if build["returncode"]:
        raise ValueError("Lean specification scaffold failed to build: " +
                         artifacts.preview_text(build["output"], 3000))
    targets = inspect_declarations(root, chunks)
    if source_identity(root) != before:
        raise ValueError("source or dependencies changed while freezing the formal contract")
    for chunk in chunks:
        row = targets[chunk["lean_decl"]]
        if row["module"] != module_for_file(root, chunk["lean_file"], modules):
            raise ValueError(f"scaffold declaration is not in {chunk['lean_file']}")
        if row["target_kind"] == "axiom":
            raise ValueError("use theorem proof holes in the scaffold, not axiom declarations")
        semantic = _semantic_record(row)
        if '"sorryAx"' in json.dumps(semantic):
            raise ValueError("specification types and meaning-bearing definitions must not contain sorry")
        if set(row["axioms"]) - AXIOMS - {"sorryAx"}:
            raise ValueError("specification uses an unexpected axiom")
    body = {
        "version": 1,
        "solution_candidate": dag["solution_candidate"],
        "solution_sha256": dag["solution_sha256"],
        "requirements": dag["requirements"],
        "environment": before["environment"],
        "scaffold_source_sha256": before["source_sha256"],
        "targets": {name: {"target_kind": row["target_kind"], "module": row["module"],
                           "fingerprint": digest(_semantic_record(row))}
                    for name, row in targets.items()},
    }
    # Commit only source/build configuration, never run state or arbitrary files.
    filenames = [str(path.relative_to(root)) for path in source_files(root)]
    unrelated_staged = set(_git(root, "diff", "--cached", "--name-only").splitlines()) - set(filenames)
    if unrelated_staged:
        raise ValueError("cannot checkpoint scaffold with unrelated staged files: " +
                         ", ".join(sorted(unrelated_staged)))
    unrelated_dirty = set(_git(root, "diff", "--name-only").splitlines()) - set(filenames)
    if unrelated_dirty:
        raise ValueError("cannot checkpoint scaffold with unrelated tracked changes: " +
                         ", ".join(sorted(unrelated_dirty)))
    _git(root, "add", "--", *filenames)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode:
        _git(root, "commit", "-m", "UNITY: freeze solve formal specification")
    after = source_identity(root)
    if after != {**before, "main_sha": after["main_sha"]}:
        raise ValueError("source changed while committing the formal specification")
    body["scaffold_main_sha"] = after["main_sha"]
    contract = {**body, "sha256": digest(body)}
    record = artifacts.store_text(paths.artifacts, json.dumps({"contract": contract,
                                  "declarations": targets}, sort_keys=True) + "\n",
                                  kind="solve_formal_contract", producer="Unity")
    contract["artifact_id"] = record["artifact_id"]
    return contract


def check_formal_contract(root: Path, contract: dict, tasks: list[dict],
                          *, completed: set[str]) -> dict:
    """Identity is conservative: harmless signature refactors require re-chunking."""
    body = {key: value for key, value in contract.items() if key not in {"sha256", "artifact_id"}}
    if not contract or digest(body) != contract.get("sha256"):
        return {"passed": False, "issues": ["formal contract is missing or corrupt"], "targets": {}}
    issues = []
    if environment_identity(root) != contract["environment"]:
        issues.append("toolchain or dependency environment changed from the formal contract")
    try:
        inspection = inspect_environment(root, tasks)
        targets = inspection["targets"]
    except ValueError as exc:
        return {"passed": False, "issues": [*issues, str(exc)], "targets": {}}
    if completed == {task.get("task_id", task.get("id")) for task in tasks}:
        if inspection["project_axioms"]:
            issues.append("project retains custom axioms: " + ", ".join(inspection["project_axioms"]))
        if inspection["project_sorries"]:
            issues.append("project retains proof holes: " + ", ".join(inspection["project_sorries"]))
    for task in tasks:
        name = task["lean_decl"]
        row = targets[name]
        if digest(_semantic_record(row)) != contract["targets"].get(name, {}).get("fingerprint"):
            issues.append(f"contract changed for {name}: type, definition, or declaration identity differs")
        if task.get("task_id", task.get("id")) in completed:
            unexpected = set(row["axioms"]) - AXIOMS
            if unexpected:
                issues.append(f"{name} depends on forbidden axioms: {', '.join(sorted(unexpected))}")
    return {"passed": not issues, "issues": issues, "targets": targets}


def snapshot_is_current(paths, state: dict, snapshot: dict) -> bool:
    if not snapshot:
        return False
    current = source_identity(paths.project_root)
    formal = state["formalization"]
    return (
        current["main_sha"] == snapshot.get("main_sha") == formal.get("main_sha")
        and current["source_sha256"] == snapshot.get("source_sha256")
        and current["environment"] == snapshot.get("environment")
        and formal.get("contract", {}).get("sha256") == snapshot.get("contract_sha256")
        and formal.get("revision") == snapshot.get("formalization_revision")
        and formal.get("solution_candidate") == snapshot.get("solution_candidate")
        and formal.get("solution_sha256") == snapshot.get("solution_sha256")
        and {key: task.get("accepted_candidate") for key, task in state["formal_tasks"].items()}
        == snapshot.get("accepted_candidates")
        and hashlib.sha256(paths.unity_md.read_bytes()).hexdigest() == state["problem_sha256"]
        and hashlib.sha256((paths.unity / "source" / "PROOF.tex").read_bytes()).hexdigest()
        == formal.get("solution_sha256")
    )


def verify_final_project(paths, state: dict) -> dict:
    root = paths.project_root
    formal = state["formalization"]
    before = source_identity(root)
    tasks = list(state["formal_tasks"].values())
    last = next((candidate for candidate in reversed(list(state["formal_candidates"].values()))
                 if candidate.get("status") == "merged" and candidate.get("main_sha") == before["main_sha"]), {})
    verification = last.get("verification") or {}
    complete_ids = set(state["formal_tasks"])
    reusable = (verification.get("status") == "passed"
                and verification.get("source_identity") == before
                and verification.get("contract_sha256") == formal.get("contract", {}).get("sha256")
                and set(verification.get("verified_tasks", [])) == complete_ids
                and last.get("build", {}).get("returncode") == 0)
    if reusable:
        build = {"returncode": 0, "reused_candidate": last["candidate_id"]}
        check = {"passed": True, "issues": [], "targets": {},
                 "reused_verification": verification.get("artifact_id")}
    else:
        build = build_sources(root, full=True)
        check = (check_formal_contract(root, formal.get("contract", {}), tasks, completed=complete_ids)
                 if not build["returncode"] else
                 {"passed": False, "issues": ["final project build failed"], "targets": {}})
    issues = list(check["issues"])
    if any(task.get("status") != "complete" for task in tasks) or not tasks:
        issues.append("formal tasks are incomplete")
    if source_identity(root) != before:
        issues.append("source changed during final mechanical verification")
    if before["main_sha"] != formal.get("main_sha"):
        issues.append("main differs from the recorded accepted candidate revision")
    if hashlib.sha256(paths.unity_md.read_bytes()).hexdigest() != state["problem_sha256"]:
        issues.append("original problem changed; start a new solve run")
    if hashlib.sha256((paths.unity / "source" / "PROOF.tex").read_bytes()).hexdigest() != formal["solution_sha256"]:
        issues.append("accepted paper bytes changed")
    report = {
        **before,
        "passed": not issues,
        "issues": issues,
        "solution_candidate": formal["solution_candidate"],
        "solution_sha256": formal["solution_sha256"],
        "formalization_revision": formal["revision"],
        "contract_sha256": formal.get("contract", {}).get("sha256"),
        "accepted_candidates": {task["task_id"]: task.get("accepted_candidate") for task in tasks},
        "declarations": {task["lean_decl"]: task["task_id"] for task in tasks},
        "build": build,
        "targets": check["targets"],
    }
    report["snapshot_id"] = "review-" + uuid.uuid4().hex
    artifact = artifacts.store_text(paths.artifacts, json.dumps(report, sort_keys=True) + "\n",
                                    kind="solve_machine_review", producer="Unity")
    # Keep telemetry out of shared prompt memory. Detail remains in the artifact.
    return {key: value for key, value in report.items() if key not in {"build", "targets"}} | {
        "artifact_id": artifact["artifact_id"],
    }
