"""Deterministic LeanArchitect setup for the prove pipeline."""

from pathlib import Path
import re
import subprocess

import asyncclick as click


LEANARCHITECT_GIT = "https://github.com/hanwenzhu/LeanArchitect.git"


def _leanarchitect_requirement(version: str, lakefile: Path) -> str:
    if lakefile.name == "lakefile.toml":
        return (
            "[[require]]\n"
            'name = "LeanArchitect"\n'
            f'git = "{LEANARCHITECT_GIT}"\n'
            f'rev = "{version}"\n'
        )
    return f'require LeanArchitect from git "{LEANARCHITECT_GIT}" @ "{version}"\n'


def _set_toml_requirement(source: str, version: str) -> str:
    headers = list(re.finditer(r"(?m)^\s*\[\[?[^\]\n]+\]\]?\s*$", source))
    for index, match in enumerate(headers):
        if match.group().strip() != "[[require]]":
            continue
        end = headers[index + 1].start() if index + 1 < len(headers) else len(source)
        block = source[match.start():end]
        if not re.search(r'(?mi)^\s*name\s*=\s*["\']LeanArchitect["\']\s*$', block):
            continue

        values = {
            "git": LEANARCHITECT_GIT,
            "rev": version,
        }
        for key, value in values.items():
            pattern = re.compile(rf"(?m)^\s*{key}\s*=\s*[^\n]*$")
            line = f'{key} = "{value}"'
            if pattern.search(block):
                block = pattern.sub(line, block, count=1)
            else:
                block = block.rstrip("\n") + "\n" + line + "\n"
        return source[:match.start()] + block + source[end:]

    separator = "" if not source or source.endswith("\n\n") else ("\n" if source.endswith("\n") else "\n\n")
    return source + separator + _leanarchitect_requirement(version, Path("lakefile.toml"))


def _set_lean_requirement(source: str, version: str) -> str:
    requirement = _leanarchitect_requirement(version, Path("lakefile.lean"))
    pattern = re.compile(r"(?m)^\s*require\s+LeanArchitect\b[^\n]*(?:\n|$)")
    if pattern.search(source):
        return pattern.sub(requirement, source, count=1)
    separator = "" if not source or source.endswith("\n") else "\n"
    return source + separator + requirement


def _set_requirement(source: str, lakefile: Path, version: str) -> str:
    if lakefile.name == "lakefile.toml":
        return _set_toml_requirement(source, version)
    return _set_lean_requirement(source, version)


def architect(project_root: Path) -> bool:
    """Add the toolchain-matched LeanArchitect dependency and update Lake.

    If setup fails, restore the original lakefile and regenerate the manifest from it.
    """
    project_root = Path(project_root)
    lakefile = next(
        (project_root / name for name in ("lakefile.toml", "lakefile.lean")
         if (project_root / name).is_file()),
        None,
    )
    if lakefile is None:
        click.echo("LeanArchitect skipped: no lakefile.toml or lakefile.lean found.")
        return False

    manifest = project_root / "lake-manifest.json"
    git_repo = (project_root / ".git").exists()
    dependency_paths = [lakefile.name, manifest.name]
    if git_repo:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *dependency_paths],
            cwd=project_root, capture_output=True, text=True, check=False,
        )
        if dirty.returncode != 0 or dirty.stdout.strip():
            click.echo("LeanArchitect skipped: lakefile or lake manifest has existing changes.")
            return False

    try:
        version_result = subprocess.run(
            ["lake", "--version"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        click.echo(f"LeanArchitect skipped: could not run lake --version ({exc}).")
        return False

    version_output = version_result.stdout.strip()
    if version_result.returncode != 0 or " " not in version_output:
        click.echo("LeanArchitect skipped: lake --version failed or returned unexpected output.")
        return False
    version = "v" + version_output.rsplit(" ", 1)[-1].removesuffix(")")

    original = lakefile.read_bytes()
    updated = _set_requirement(original.decode(), lakefile, version).encode()
    lakefile.write_bytes(updated)

    try:
        update_result = subprocess.run(["lake", "update"], cwd=project_root, check=False)
    except OSError as exc:
        update_result = None
        failure = str(exc)
    else:
        failure = f"exit code {update_result.returncode}"

    if update_result is not None and update_result.returncode == 0:
        if git_repo:
            changed = subprocess.run(
                ["git", "status", "--porcelain", "--", *dependency_paths],
                cwd=project_root, capture_output=True, text=True, check=False,
            )
            if changed.stdout.strip():
                commit_paths = [lakefile.name] + ([manifest.name] if manifest.exists() else [])
                subprocess.run(["git", "add", "--", *commit_paths], cwd=project_root, check=False)
                committed = subprocess.run(
                    ["git", "commit", "-m", f"UNITY: add LeanArchitect {version}", "--", *commit_paths],
                    cwd=project_root, check=False,
                )
                if committed.returncode != 0:
                    lakefile.write_bytes(original)
                    subprocess.run(["lake", "update"], cwd=project_root, check=False)
                    subprocess.run(["git", "reset", "--", *commit_paths],
                                   cwd=project_root, check=False)
                    click.echo("LeanArchitect skipped: could not commit dependency baseline.")
                    return False
        click.echo(f"LeanArchitect enabled at {version}.")
        return True

    lakefile.write_bytes(original)
    try:
        subprocess.run(["lake", "update"], cwd=project_root, check=False)
    except OSError:
        pass
    click.echo(f"LeanArchitect skipped: lake update failed ({failure}); restored {lakefile.name}.")
    return False
