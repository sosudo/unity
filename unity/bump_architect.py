"""Optional, target-only LeanArchitect instrumentation before environment sealing.

This adapter does not call Prove's installer: that installer commits and resolves
all dependencies. Bump preserves its explicitly pinned dependency transition and
accepts at most one additional, exact toolchain-matched package.
"""

from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

from . import bump_migration_project as project
from .Architect import LEANARCHITECT_GIT, _set_requirement


def _reseal(value: dict) -> dict:
    value = {key: item for key, item in value.items() if key != "identity"}
    return {**value, "identity": project._digest(value)}


def prepare_optional_architect(target: Path, migration: dict, resolution: dict,
                               *, mode: str = "auto") -> tuple[dict, dict, dict]:
    """Try one matching release; a skip never changes accepted target bytes.

    No model, evaluation, original-source change or Git commit is performed.
    Failed optional setup retains its downloaded cache but restores the exact
    two configuration files it owns. Unexpected source/dependency effects fail
    closed rather than being presented as a harmless optional skip.
    """
    if mode not in {"auto", "off"}:
        raise ValueError("Bump LeanArchitect mode must be auto or off")
    if migration.get("target_sealed"):
        raise ValueError("Optional instrumentation must precede target sealing")
    if not resolution.get("passed") or resolution.get("baseline_identity") != migration.get("identity"):
        raise ValueError("Optional instrumentation requires the exact successful dependency resolution")
    target = Path(target).resolve()
    _, owned = project.resolve_paths(Path(migration["root"]), migration)
    if target != owned:
        raise ValueError("Optional instrumentation requires the owned target")
    if project.validate_target(target, migration, require_resolved=False, resolution=resolution):
        raise ValueError("Target configuration changed before optional instrumentation")
    receipt = {"version": 1, "mode": mode, "status": "skipped", "reason": "disabled"}
    if mode == "off":
        return migration, resolution, receipt
    existing = project._manifest(target)
    if any(row["name"] == "LeanArchitect" for row in existing["packages"]):
        return migration, resolution, {**receipt, "status": "existing", "reason": "existing dependency preserved"}
    version = migration["target_version"].split(":")[-1]
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\d+)?", version):
        return migration, resolution, {**receipt, "reason": "no release-matched optional package"}
    try:
        lookup = project._run(target, ["git", "ls-remote", "--tags", LEANARCHITECT_GIT,
                                      f"refs/tags/{version}", f"refs/tags/{version}^{{}}"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return migration, resolution, {**receipt, "reason": "optional release lookup unavailable",
                                      "error_type": type(exc).__name__}
    rows = [line.split() for line in lookup.stdout.splitlines()]
    commits = {name: sha for row in rows if len(row) == 2 for sha, name in [row]
               if re.fullmatch(r"[0-9a-f]{40}", sha)}
    revision = commits.get(f"refs/tags/{version}^{{}}", commits.get(f"refs/tags/{version}"))
    if lookup.returncode or not revision:
        return migration, resolution, {**receipt, "reason": "matching release unavailable"}
    configurations = [target / name for name in ("lakefile.toml", "lakefile.lean") if (target / name).is_file()]
    if len(configurations) != 1:
        raise ValueError("Optional instrumentation requires one lakefile")
    lakefile, manifest_path = configurations[0], target / "lake-manifest.json"
    originals = {path: path.read_bytes() for path in (lakefile, manifest_path)}
    before = project.source_files(target)
    expected_lakefile = _set_requirement(originals[lakefile].decode("utf-8"), lakefile, revision).encode()
    lakefile.write_bytes(expected_lakefile)
    accepted = False
    reason = "optional package setup failed"
    try:
        result = project._run(target, ["lake", "update", "LeanArchitect"])
        after = project.source_files(target)
        if {k: v for k, v in before.items() if k not in {lakefile.name, manifest_path.name}} != {
                k: v for k, v in after.items() if k not in {lakefile.name, manifest_path.name}}:
            raise ValueError("Optional dependency setup changed project source; evidence preserved")
        if lakefile.read_bytes() != expected_lakefile:
            raise ValueError("Optional dependency setup changed its declared configuration")
        if result.returncode:
            return migration, resolution, {**receipt, "reason": reason, "returncode": result.returncode}
        manifest = project._manifest(target)
        old_rows = {row["name"]: row for row in existing["packages"]}
        new_rows = {row["name"]: row for row in manifest["packages"]}
        if set(new_rows) != set(old_rows) | {"LeanArchitect"} or any(new_rows.get(name) != row for name, row in old_rows.items()):
            return migration, resolution, {**receipt, "reason": "optional package requires unrelated dependency changes"}
        auxiliary = new_rows["LeanArchitect"]
        if (auxiliary.get("type") != "git" or auxiliary.get("url") != LEANARCHITECT_GIT
                or auxiliary.get("rev") != revision or auxiliary.get("inputRev") != revision
                or auxiliary.get("inherited", False) is not False):
            raise ValueError("Optional package resolution does not match the exact approved source")
        built = project._run(target, ["lake", "build", "LeanArchitect"])
        if built.returncode:
            return migration, resolution, {**receipt, "reason": "matching optional package did not build"}
        if project.source_files(target) != after:
            raise ValueError("Optional package build changed project inputs")
        updated = copy.deepcopy(migration)
        updated["auxiliary_dependencies"] = {"LeanArchitect": auxiliary}
        updated["target_config"] = project._config_hashes(target)
        updated = _reseal(updated)
        result_resolution = {**resolution, "baseline_identity": updated["identity"],
                             "config": updated["target_config"], "manifest": manifest,
                             "optional_architect": {"release": version, "revision": revision}}
        accepted = True
        return updated, result_resolution, {**receipt, "status": "enabled", "reason": "matching package built",
                                            "release": version, "revision": revision}
    finally:
        if not accepted:
            for path, content in originals.items():
                path.write_bytes(content)
