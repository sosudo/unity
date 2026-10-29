"""Explicit, bounded project checks for compiler-driven Bump."""
from __future__ import annotations

import re
from copy import deepcopy

_TARGET = re.compile(r"\+?[A-Za-z_][A-Za-z0-9_.-]*(?::[A-Za-z_][A-Za-z0-9_.-]*)?\Z")
_MAX_CHECKS = 32
_MAX_TARGETS = 24


def normalize(commands) -> list[list[str]]:
    """Validate exact argv without shell interpretation or destructive options."""
    if not isinstance(commands, list) or len(commands) > _MAX_CHECKS:
        raise ValueError("project checks must be a bounded list of argv lists")
    result = []
    for argv in commands:
        if (not isinstance(argv, list) or len(argv) < 2 or len(argv) > _MAX_TARGETS + 2
                or any(not isinstance(arg, str) or len(arg) > 200 for arg in argv) or argv[0] != "lake"):
            raise ValueError("project checks must use lake build or lake test")
        if argv[1] == "test":
            if len(argv) != 2: raise ValueError("lake test does not accept additional arguments")
        elif argv[1] == "build":
            if any(not _TARGET.fullmatch(arg) for arg in argv[2:]):
                raise ValueError("lake build targets must be simple target names")
        else:
            raise ValueError("project checks must use lake build or lake test")
        if argv not in result: result.append(list(argv))
    return result


def add_to_state(state: dict, commands, author: str, *, task_id=None, snapshot_id=None) -> list[list[str]]:
    from . import bump_tasks
    requested = normalize(commands)
    if not requested: raise ValueError("at least one project check is required")
    pending = list(state.get("config_requests", {}).values()) + list(state.get("tasks", {}).values())
    if (state.get("config", {}).get("lake_args") or any(
            row.get("status") in {"pending", "scheduled", "running", "submitted", "merging"}
            and row.get("requested_config", {}).get("lake_args") for row in pending)):
        raise ValueError("exact required project checks cannot be combined with configured or pending Lake options")
    existing = normalize(state.get("required_project_checks", []))
    added = [argv for argv in requested if argv not in existing]
    if len(existing) + len(added) > _MAX_CHECKS: raise ValueError("too many required project checks")
    if added:
        state["required_project_checks"] = existing + deepcopy(added)
        state["project_check_revision"] = state.get("project_check_revision", 0) + 1
        bump_tasks._event(state, "project_checks_requested", author, task_id=task_id,
                          snapshot_id=snapshot_id, commands=deepcopy(added),
                          check_revision=state["project_check_revision"])
    return requested
