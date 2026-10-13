"""Upgrade-first workspace preparation for the compiler-driven Bump workflow.

The source Git checkout is a starting snapshot, not a preservation contract.
Preparation creates a forum before running any compiler or dependency command;
recoverable configuration errors remain ordinary work in that owned workspace.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from . import artifacts, bump_dependencies, bump_jobs, bump_workspace
from .config import Paths

SCHEMA = bump_dependencies.SCHEMA
parse_dependency_pins = bump_dependencies.parse_dependency_pins
configuration_transaction = bump_dependencies.configuration_transaction
_CONFIG = ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json")


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    artifacts._atomic_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def _paths(root: Path) -> Paths:
    paths = Paths.from_unity_dir(root / ".unity")
    return replace(paths, forum=paths.unity / "forum" / "bump")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Bump Git operation failed: " + (result.stderr.strip()[-1000:] or args[0]))
    return result.stdout.strip()


def _safe_root(root: Path) -> Path:
    root = Path(root).absolute()
    if root.resolve() != root or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Bump source must be its exact canonical Git root")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Bump starts at the project Git root")
    return root


def _config_hashes(root: Path) -> dict:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in _CONFIG if (root / name).is_file() and not (root / name).is_symlink()}


def _regular_runtime_inputs(source: Paths, target: Paths) -> dict:
    hashes = {}
    for before, after, required in ((source.unity_md, target.unity_md, True),
                                    (source.agents_yaml, target.agents_yaml, True),
                                    (source.env, target.env, False)):
        if not required and not before.exists() and not before.is_symlink():
            continue
        if before.is_symlink() or not before.is_file() or after.exists() or after.is_symlink():
            raise ValueError("invalid or already existing Bump runtime input: " + before.name)
        data = before.read_bytes()
        fd = os.open(after, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        hashes[before.name] = hashlib.sha256(data).hexdigest()
    return hashes


def _actual_executable(name: str, version: str) -> Path:
    candidate = os.environ.get("UNITY_REAL_LAKE") if name == "lake" else None
    candidate = candidate or shutil.which(name)
    if not candidate:
        raise ValueError("requested Lean toolchain is unavailable; install it explicitly before continuing")
    path = Path(candidate)
    elan = shutil.which("elan")
    proxy = path.parent.name == "bin" and path.parent.parent.name == ".elan"
    if elan and path.exists():
        try:
            proxy = proxy or path.samefile(elan)
        except OSError:
            pass
    if proxy:
        # Avoid invoking an elan proxy: it may implicitly download/install into
        # the user's global toolchains. Resolve the existing requested compiler.
        elan_dir = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan")))
        directory = version.replace("/", "--").replace(":", "---")
        path = elan_dir / "toolchains" / directory / "bin" / name
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("requested Lean toolchain executable is missing or dangling: " + name)
    return path.resolve()


def verify_toolchain(root: Path, version: str) -> dict:
    """Execute both actual binaries, without an implicit elan installation."""
    version = bump_dependencies.normalize_version(version)
    lean, lake = (_actual_executable(name, version) for name in ("lean", "lake"))
    evidence = {"target_version": version}
    for name, executable in (("lean", lean), ("lake", lake)):
        result = bump_jobs.run(root, [str(executable), "--version"], cwd=root,
                               task_id="verify-" + name, serialize_build=False)
        if result.returncode:
            raise ValueError("requested toolchain executable failed its version check: " + name)
        output = result.stdout.strip()
        if not output or name == "lake" and not re.search(r"\bLake version\b", output, re.I):
            raise ValueError("toolchain executable returned an invalid version identity: " + name)
        evidence[name] = {"executable": str(executable), "version_output": output[:1000]}
    release = version.split(":", 1)[1]
    if release.startswith("v") and not re.search(r"(?<![\w.])" + re.escape(release[1:]) + r"(?![\w.-])",
                                                 evidence["lean"]["version_output"]):
        raise ValueError("actual Lean executable does not match the requested release")
    lean_commit = re.search(r"commit\s+([0-9a-f]{7,40})", evidence["lean"]["version_output"])
    lake_commit = re.search(r"src\+([0-9a-f]{7,40})", evidence["lake"]["version_output"])
    if lean_commit and lake_commit and not (lean_commit[1].startswith(lake_commit[1]) or lake_commit[1].startswith(lean_commit[1])):
        raise ValueError("actual Lean and Lake executables belong to different toolchains")
    return evidence


def current_source_files(root: Path, build_dir=".lake/build") -> list[str]:
    """Inventory current project Lean files, including nonignored additions."""
    root = Path(root).resolve()
    bump_dependencies._local_path(root, build_dir)
    files = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    runtime = {".git", ".unity", ".lake", ".worktrees", "__pycache__", "lake-packages"}
    return sorted({path for path in files if path.endswith(".lean") and path != "lakefile.lean"
                   and not any(part in runtime for part in Path(path).parts)
                   and not path.startswith(("vendor/", build_dir.rstrip("/") + "/"))
                   and (root / path).exists()})


def discover_scope(root: Path, *, modules=(), project_scope="build", lake_args=()) -> dict:
    """Discover current source coverage using Lake, without an old-version build."""
    if project_scope not in {"build", "all"}:
        raise ValueError("Bump scope must be build or all")
    lake_args = bump_dependencies.checked_lake_args(lake_args)
    layout = bump_workspace.discover(root, ["--layout-only"], lake_args=lake_args)
    defaults = layout.get("default_modules")
    if not isinstance(defaults, dict) or layout.get("unknown_default_targets"):
        raise ValueError("scope review needed: Lake has unknown default target coverage")
    build_dir = layout.get("build_dir")
    bump_dependencies._local_path(root, build_dir)
    lean_files = current_source_files(root, build_dir)
    owned = bump_workspace.discover(root, sorted(lean_files), lake_args=lake_args)
    known = {module: path for path, module in owned["modules"].items()}
    default_map = {module: path for path, module in defaults.items()}
    missing = set(modules) - known.keys()
    if missing:
        raise ValueError("unknown supplemental modules: " + ", ".join(sorted(missing)))
    selected = dict(known) if project_scope == "all" else dict(default_map)
    selected.update({module: known[module] for module in modules})
    if not selected or any(known.get(module) != path for module, path in selected.items()):
        raise ValueError("scope review needed: default modules do not map to current source")
    pending = set(selected)
    while pending:
        imports = bump_workspace.read_imports(root, [known[module] for module in sorted(pending)], lake_args=lake_args)
        reachable = {module for row in imports.values() for module in row if module in known}
        pending = reachable - selected.keys()
        selected.update({module: known[module] for module in pending})
    unmatched = sorted(owned.get("unmatched", []))
    if project_scope == "all" and unmatched:
        raise ValueError("all-source scope contains files without Lake module ownership")
    return {"mode": project_scope, "selected_modules": dict(sorted(selected.items())),
            "default_modules": default_map, "supplemental_modules": sorted(set(modules)),
            "excluded_files": {path: "outside selected current build scope" for path in lean_files if path not in selected.values()},
            "unmatched_files": unmatched, "build_dir": build_dir, "lake_args": list(lake_args)}


def prepare_environment(root: Path, version: str, pins: dict, *, modules=(), project_scope="build",
                        lake_args=(), resolve=True) -> dict:
    """Prepare or re-prepare one already-owned target; keep blockers inspectable."""
    root = Path(root).resolve()
    version = bump_dependencies.normalize_version(version)
    lake_args = bump_dependencies.checked_lake_args(lake_args)
    paths = _paths(root)
    for directory in (paths.forum, paths.logs, paths.artifacts, paths.unity / "bump"):
        directory.mkdir(parents=True, exist_ok=True)
    previous = read_setup(paths) if (paths.unity / "bump" / "setup.json").is_file() else {}
    setup = {"schema": SCHEMA, "phase": "preparing", "target_version": version, "dependency_pins": dict(pins),
             "project_scope": project_scope, "modules": list(modules), "lake_args": list(lake_args),
             "configuration_revision": previous.get("configuration_revision", 0) + 1,
             "pending_tasks": [], "created_at": datetime.now(timezone.utc).isoformat()}
    step = "toolchain"
    try:
        with configuration_transaction(root):
            toolchain = root / "lean-toolchain"
            if toolchain.is_symlink():
                raise ValueError("lean-toolchain must be a regular project file")
            toolchain.write_text(version + "\n")
            setup["toolchain"] = verify_toolchain(root, version)
            # Authoritative jobs resolve their exact Lean/Lake binaries from
            # this receipt rather than an unrelated PATH proxy or installation.
            _json(paths.unity / "bump" / "setup.json", setup)
            step = "dependencies"
            if resolve:
                setup["dependencies"] = bump_dependencies.resolve_dependencies(root, version, pins, lake_args=lake_args)
                if setup["dependencies"]["status"] != "resolved":
                    raise ValueError(setup["dependencies"]["reason"])
            # Bump's build lock is reentrant on this controller thread. Keep
            # scope checks inside the edit/update/check transaction as well.
            step = "scope"
            setup["scope"] = discover_scope(root, modules=modules, project_scope=project_scope, lake_args=lake_args)
            setup["phase"] = "ready"
    except bump_jobs.JobCancelled:
        raise
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        setup["phase"] = "blocked"
        setup["pending_tasks"].append({"task_id": "setup-" + step, "kind": "configuration" if step != "scope" else "scope",
            "status": "pending" if step != "toolchain" else "blocked", "step": step,
            "reason": str(exc)[:2000], "retryable": step != "toolchain"})
    setup["config_hashes"] = _config_hashes(root)
    _json(paths.unity / "bump" / "setup.json", setup)
    return setup


def read_setup(paths: Paths) -> dict:
    data = json.loads((paths.unity / "bump" / "setup.json").read_text())
    if data.get("schema") != SCHEMA:
        raise ValueError("saved workspace is not a compiler-driven Bump migration")
    return data


def prepare(source_paths: Paths, version: str, pins: dict, *, modules=(), project_scope="build", lake_args=()) -> Paths:
    source = _safe_root(source_paths.project_root)
    version = bump_dependencies.normalize_version(version)
    if project_scope not in {"build", "all"}:
        raise ValueError("Bump scope must be build or all")
    bump_dependencies.parse_dependency_pins([name + "=" + pin for name, pin in pins.items()])
    active = source_paths.unity / "bump" / "active.json"
    if active.exists() or active.is_symlink():
        raise ValueError("an existing Bump workspace is retained; use --continue")
    # Do not silently discard unrelated edits by starting from HEAD. No old
    # source build/index or full-project byte-preservation ledger is required.
    if _git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("commit or stash tracked edits before creating the migration worktree")
    untracked = _git(source, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    if any(path and not path.startswith((".unity/", ".lake/", ".worktrees/")) for path in untracked):
        raise ValueError("commit or move untracked project inputs before creating the migration worktree")
    for runtime_input in (source_paths.unity_md, source_paths.agents_yaml):
        if runtime_input.is_symlink() or not runtime_input.is_file():
            raise ValueError("initialize the existing project's runtime configuration before Bump")
    head = _git(source, "rev-parse", "HEAD")
    run_id = "bump-" + uuid.uuid4().hex[:12]
    target = source / ".unity" / "bump" / run_id / "target"
    if target.resolve() != target or any(p.is_symlink() for p in target.parents):
        raise ValueError("Bump target workspace has a redirected parent")
    target.parent.mkdir(parents=True, exist_ok=False)
    _git(source, "worktree", "add", "-b", "unity/" + run_id, str(target), head)
    paths = _paths(target)
    for directory in (paths.unity, paths.forum, paths.logs, paths.artifacts):
        directory.mkdir(parents=True, exist_ok=True)
    config = _regular_runtime_inputs(source_paths, paths)
    identity = {"schema": SCHEMA, "run_id": run_id, "source_root": str(source), "project_root": str(source),
                "target_path": str(target)}
    migration = {**identity, "target_root": str(target), "original_commit": head, "target_version": version,
                 "dependency_pins": dict(pins), "project_scope": project_scope, "modules": list(modules),
                 "lake_args": list(lake_args)}
    _json(paths.unity / "bump-origin.json", identity)
    _json(paths.unity / "bump" / "migration.json", migration)
    _json(active, {**identity, "status": "ready", "runtime_hashes": config})
    prepare_environment(target, version, pins, modules=modules, project_scope=project_scope, lake_args=lake_args)
    return paths


def resume(source_paths: Paths, version: str | None = None, pins: dict | None = None, *,
           modules=None, project_scope: str | None = None, lake_args=None) -> Paths:
    source = _safe_root(source_paths.project_root)
    pointer_path = source_paths.unity / "bump" / "active.json"
    if pointer_path.is_symlink():
        raise ValueError("Bump pointer cannot be a symlink")
    pointer = json.loads(pointer_path.read_text())
    run_id = pointer.get("run_id", "")
    if not isinstance(run_id, str) or not re.fullmatch(r"bump-[0-9a-f]{12}", run_id):
        raise ValueError("invalid Bump workspace identity")
    target = source / ".unity" / "bump" / run_id / "target"
    identity = {"schema": SCHEMA, "run_id": run_id, "source_root": str(source), "project_root": str(source),
                "target_path": str(target)}
    if (any(pointer.get(key) != value for key, value in identity.items()) or pointer.get("status") != "ready"
            or target.resolve() != target or any(p.is_symlink() for p in (target, *target.parents))):
        raise ValueError("saved Bump workspace is not the exact owned compiler migration")
    paths = _paths(target)
    origin_path = paths.unity / "bump-origin.json"
    if origin_path.is_symlink() or json.loads(origin_path.read_text()) != identity:
        raise ValueError("Bump origin disagrees with the source pointer")
    migration = json.loads((paths.unity / "bump" / "migration.json").read_text())
    if any(migration.get(key) != value for key, value in identity.items()) or migration.get("target_root") != str(target):
        raise ValueError("Bump migration disagrees with workspace ownership")
    if version and bump_dependencies.normalize_version(version) != migration["target_version"]:
        raise ValueError("--continue cannot change the saved Lean version")
    if pins is not None and pins and dict(pins) != migration["dependency_pins"]:
        raise ValueError("--continue cannot change explicit dependency pins")
    for key, value in (("modules", modules), ("project_scope", project_scope), ("lake_args", lake_args)):
        if value is not None and (list(value) if key != "project_scope" else value) != migration[key]:
            raise ValueError("--continue cannot change the saved " + key)
    if Path(_git(target, "rev-parse", "--show-toplevel")).resolve() != target:
        raise ValueError("saved Bump target is not its own Git worktree")
    # Runtime source edits are not silently copied over an active run. Unlike
    # old preservation gates this protects only private launch configuration.
    for name, digest in pointer.get("runtime_hashes", {}).items():
        if name not in {"UNITY.md", "agents.yaml", ".env"}:
            raise ValueError("unexpected saved runtime configuration name")
        path = paths.unity / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("saved Bump runtime configuration changed: " + name)
    read_setup(paths)
    return paths
