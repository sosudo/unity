"""Persistent dependency-project dispatch for the ordinary Bump controller.

The caller provides the same child workflow used for root projects. This module
does not invent a second agent loop or call a model; it coordinates source
identity, recursion, runtime scaffolding, and reviewed vendoring only.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from . import bump_dependencies as dependencies, bump_setup as setup
from .config import Paths


def _registry_path(paths: Paths) -> Path:
    return paths.unity / "bump" / "dependencies.json"


def _load_registry(paths: Paths, provided: dict | None) -> dict:
    disk = json.loads(_registry_path(paths).read_text()) if _registry_path(paths).is_file() else {}
    registry = provided if provided is not None else {}
    if disk.get("schema", dependencies.SCHEMA) != dependencies.SCHEMA:
        raise dependencies.DependencyError("incompatible saved dependency coordination state")
    if not isinstance(disk.get("tasks", {}), dict):
        raise dependencies.DependencyError("invalid saved dependency coordination state")
    if registry.get("schema", dependencies.SCHEMA) != dependencies.SCHEMA:
        raise dependencies.DependencyError("incompatible dependency coordination state")
    registry.setdefault("schema", dependencies.SCHEMA)
    registry.setdefault("tasks", {})
    if not isinstance(registry["tasks"], dict):
        raise dependencies.DependencyError("invalid dependency coordination state")
    for task_id, saved in disk.get("tasks", {}).items():
        if task_id not in registry["tasks"]:
            registry["tasks"][task_id] = saved
        else:
            current = registry["tasks"][task_id]
            if any(saved.get(key) != current.get(key) for key in ("repository", "revision", "version")):
                raise dependencies.DependencyError("conflicting saved dependency source identity")
            for key, value in saved.get("integrations", {}).items():
                current.setdefault("integrations", {}).setdefault(key, value)
    return registry


def _scaffold(parent_paths: Paths, child: Path, task: dict, parent_setup: dict) -> tuple[Paths, dict]:
    paths = setup._paths(child)
    for directory in (paths.unity, paths.forum, paths.logs, paths.artifacts, paths.unity / "bump"):
        directory.mkdir(parents=True, exist_ok=True)
    marker = paths.unity / "bump" / "migration.json"
    if marker.is_file():
        saved = json.loads(marker.read_text())
        if (saved.get("schema") != dependencies.SCHEMA or saved.get("dependency_task_id") != task["task_id"]
                or saved.get("target_root") != str(child)):
            raise dependencies.DependencyError("child workspace belongs to another workflow")
        if (paths.unity / "bump/setup.json").is_file():
            previous = setup.read_setup(paths)
            if previous.get("phase") == "ready":
                return paths, previous
        # An interrupted setup or newly installed compiler is rechecked on an
        # explicit continuation, without resetting task/attempt state.
        return paths, setup.prepare_environment(child, task["version"], {}, project_scope="build",
                                                lake_args=parent_setup.get("lake_args", ()))
    setup._regular_runtime_inputs(parent_paths, paths)
    migration = {"schema": dependencies.SCHEMA, "dependency_task_id": task["task_id"],
                 "run_id": task["task_id"], "source_root": str(child), "target_root": str(child),
                 "target_path": str(child), "original_commit": task["revision"], "target_version": task["version"],
                 "dependency_pins": {}, "project_scope": "build", "modules": [],
                 "lake_args": list(parent_setup.get("lake_args", []))}
    dependencies._write_json(marker, migration)
    child_setup = setup.prepare_environment(child, task["version"], {}, project_scope="build",
                                            lake_args=parent_setup.get("lake_args", ()))
    return paths, child_setup


async def run_children(parent_paths: Paths, environment: dict, *, migrate_child, registry: dict | None = None,
                       ancestors: tuple = (), run_job=None) -> bool:
    """Build/review child projects before parent work, then integrate vendored source.

    ``await migrate_child(child_paths, child_setup, ancestors=next_ancestors)``
    returns ``build_passed``, ``review_approved`` and ``verified_commit``. Both
    boolean claims must be true, and the commit must still be the child's clean
    HEAD. Callback evidence is retained, and failures preserve the child for
    ordinary continuation rather than resetting it. Return whether any parent
    requirement changed; the caller then reruns resolution/scope discovery.
    """
    root = parent_paths.project_root.resolve()
    run_job = run_job or asyncio.to_thread
    registry = _load_registry(parent_paths, registry)
    changed = False
    candidates = environment.get("dependencies", {}).get("dependency_candidates", [])
    for candidate in candidates:
        repository = dependencies._repository(candidate["repository"]).rstrip("/")
        version = dependencies.normalize_version(candidate["version"])
        identity = (repository, version)
        if identity in ancestors:
            raise dependencies.DependencyError("recursive dependency migration cycle")
        task = dependencies.dependency_task(registry, candidate["name"], repository, candidate["revision"], version)
        task.setdefault("integrations", {})
        name = candidate["name"]
        integration_key = json.dumps([str(root), name], separators=(",", ":"))
        if integration_key in task["integrations"]:
            recorded = task["integrations"][integration_key]
            await run_job(dependencies.validate_vendor_publication, root, recorded)
            requirements = dependencies.direct_requirements(root)
            if not any(row.get("name") == name and row.get("path") == recorded["path"] for row in requirements):
                raise dependencies.DependencyError("recorded dependency integration needs reconciliation")
            continue
        dependencies._write_json(_registry_path(parent_paths), registry)
        try:
            child = await run_job(dependencies.materialize_dependency, root, task)
            task["workspace"] = str(child)
            if task.get("status") != "verified":
                child_paths, child_setup = await run_job(_scaffold, parent_paths, child, task, environment)
                if any(item.get("step") == "toolchain" and item.get("retryable") is False
                       for item in child_setup.get("pending_tasks", [])):
                    task["status"] = "blocked"
                    raise dependencies.DependencyError("dependency toolchain unavailable; explicit installation required")
                task["status"] = "running"
                dependencies._write_json(_registry_path(parent_paths), registry)
                result = await migrate_child(child_paths, child_setup, ancestors=(*ancestors, identity))
                if (not isinstance(result, dict) or result.get("build_passed") is not True
                        or result.get("review_approved") is not True or not result.get("verified_commit")):
                    task["status"] = "blocked"
                    raise dependencies.DependencyError("dependency migration lacks a successful build and primary review")
                task["verification"] = result
                task["status"] = "verified"
                dependencies._write_json(_registry_path(parent_paths), registry)
            result = await run_job(dependencies.vendor_dependency, root, child, name, provenance={
                **task["verification"], "repository": repository, "original_revision": task["revision"],
                "target_version": version, "dependency_task_id": task["task_id"]})
            task["integrations"][integration_key] = result
            dependencies._write_json(_registry_path(parent_paths), registry)
            changed = True
        except asyncio.CancelledError:
            task["status"] = "interrupted"
            dependencies._write_json(_registry_path(parent_paths), registry)
            raise
        except Exception:
            if task.get("status") != "verified":
                task["status"] = "blocked"
            dependencies._write_json(_registry_path(parent_paths), registry)
            raise
    return changed
