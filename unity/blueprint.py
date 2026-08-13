"""Shared Lean declaration extraction and deterministic prove-DAG generation."""

import json
import os
import re
import subprocess
from pathlib import Path


_IGNORED_DIRS = {".lake", ".unity", ".worktrees", "lake-packages", "build"}
_DECL_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+|partial\s+|unsafe\s+|scoped\s+)*"
    r"(theorem|lemma|def|abbrev|structure|inductive|class|instance|opaque|axiom)\s+"
    r"([A-Za-z0-9_.'₀-₉]+)",
    re.MULTILINE,
)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.'₀-₉]*")


def _project_lean_files(base: Path) -> list[Path]:
    return [
        path for path in sorted(base.rglob("*.lean"))
        if path.name != "lakefile.lean"
        and not any(part in _IGNORED_DIRS for part in path.relative_to(base).parts)
    ]


def scan_blueprint(base: Path) -> list[tuple[str, list[dict], dict]]:
    """Textually extract declarations and approximate in-project dependencies."""
    parsed = []
    for path in _project_lean_files(base):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        matches = list(_DECL_RE.finditer(text))
        declarations, bodies = [], {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.start():end]
            kind, name = match.group(1), match.group(2)
            status = (
                "axiom" if kind == "axiom"
                else "sorry" if re.search(r"\bsorry\b|\badmit\b", body)
                else "complete"
            )
            line = text.count("\n", 0, match.start()) + 1
            declarations.append({
                "name": name,
                "kind": kind,
                "status": status,
                "line": line,
                "end_line": line + body.rstrip().count("\n"),
            })
            bodies[name] = body
        if declarations:
            parsed.append((str(path.relative_to(base)), declarations, bodies))

    all_names = {declaration["name"] for _, declarations, _ in parsed for declaration in declarations}
    used_by: dict[str, list[str]] = {}
    for _, declarations, bodies in parsed:
        for declaration in declarations:
            dependencies = sorted(
                (set(_IDENT_RE.findall(bodies[declaration["name"]])) & all_names)
                - {declaration["name"]}
            )
            declaration["deps"] = dependencies[:12]
            for dependency in dependencies:
                used_by.setdefault(dependency, []).append(declaration["name"])
    for _, declarations, _ in parsed:
        for declaration in declarations:
            declaration["used_by"] = len(used_by.get(declaration["name"], []))
    return parsed


def source_stamp(base: Path) -> float:
    """Newest relevant source/configuration modification time."""
    stamp = 0.0
    extra = [base / name for name in (
        "lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json"
    )]
    for path in _project_lean_files(base) + extra:
        try:
            stamp = max(stamp, path.stat().st_mtime)
        except OSError:
            pass
    return stamp


def kernel_extract(base: Path) -> dict | None:
    """Return kernel-exact project declarations, or ``None`` if extraction fails."""
    modules = [".".join(path.relative_to(base).with_suffix("").parts)
               for path in _project_lean_files(base)]
    if not modules:
        return None
    script = Path(__file__).parent / "blueprint_extract.lean"
    environment = dict(os.environ)
    environment["PATH"] = environment.get("PATH", "") + os.pathsep + str(Path.home() / ".elan" / "bin")
    try:
        result = subprocess.run(
            ["lake", "env", "lean", "--run", str(script), *modules],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=300,
            env=environment,
        )
        if result.returncode != 0:
            return None
        rows = json.loads(result.stdout.strip().splitlines()[-1])
        return {row["name"]: row for row in rows}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError, KeyError):
        return None


def apply_kernel(files: list, kernel: dict) -> None:
    """Overlay exact kernel status and dependencies onto textually scanned files."""
    regex_names = {declaration["name"] for file in files for declaration in file["decls"]}
    to_regex: dict[str, str] = {}
    for kernel_name in kernel:
        if kernel_name in regex_names:
            to_regex[kernel_name] = kernel_name
        else:
            tails = [name for name in regex_names if kernel_name.endswith("." + name)]
            if len(tails) == 1:
                to_regex[kernel_name] = tails[0]
    to_kernel = {regex_name: kernel_name for kernel_name, regex_name in to_regex.items()}

    def directly_unresolved(kernel_name: str) -> bool:
        return kernel[kernel_name]["sorried"] or kernel[kernel_name]["kind"] == "axiom"

    memo: dict[str, bool] = {}

    def tainted(kernel_name: str, seen: tuple[str, ...] = ()) -> bool:
        if kernel_name in memo:
            return memo[kernel_name]
        if kernel_name in seen:
            return False
        bad = any(
            directly_unresolved(dependency) or tainted(dependency, seen + (kernel_name,))
            for dependency in kernel[kernel_name]["deps"] if dependency in kernel
        )
        memo[kernel_name] = bad
        return bad

    for file in files:
        for declaration in file["decls"]:
            kernel_name = to_kernel.get(declaration["name"])
            if not kernel_name:
                continue
            row = kernel[kernel_name]
            declaration["full_name"] = kernel_name
            declaration["deps"] = sorted({
                to_regex[dependency] for dependency in row["deps"] if dependency in to_regex
            })
            if row["sorried"]:
                declaration["status"] = "sorry"
            elif row["kind"] == "axiom":
                declaration["status"] = "axiom"
            elif tainted(kernel_name):
                declaration["status"] = "tainted"
            else:
                declaration["status"] = "complete"
    counts: dict[str, int] = {}
    for file in files:
        for declaration in file["decls"]:
            for dependency in declaration["deps"]:
                counts[dependency] = counts.get(dependency, 0) + 1
    for file in files:
        for declaration in file["decls"]:
            declaration["used_by"] = counts.get(declaration["name"], 0)


def _signature(body: str) -> str:
    end = re.search(r":=|\bby\b|\bwhere\b", body)
    if end:
        return body[:end.start()].rstrip()
    return body.splitlines()[0].rstrip() if body.splitlines() else ""


def _source_metadata(base: Path) -> tuple[dict[str, str], list[dict]]:
    module_files: dict[str, str] = {}
    declarations: list[dict] = []
    for path, rows, bodies in scan_blueprint(base):
        module_files[".".join(Path(path).with_suffix("").parts)] = path
        for row in rows:
            declarations.append({
                **row,
                "file": path,
                "body": bodies[row["name"]],
                "statement": _signature(bodies[row["name"]]),
            })
    return module_files, declarations


def _kernel_targets(base: Path, kernel: dict) -> list[dict]:
    module_files, source_declarations = _source_metadata(base)
    targets = []
    for name, row in kernel.items():
        if not (row.get("sorried") or row.get("kind") == "axiom"):
            continue
        path = module_files.get(row.get("module", ""), "")
        candidates = [
            declaration for declaration in source_declarations
            if (not path or declaration["file"] == path)
            and (name == declaration["name"] or name.endswith("." + declaration["name"]))
        ]
        source = candidates[0] if len(candidates) == 1 else {}
        targets.append({
            "name": name,
            "display_name": name,
            "kind": row.get("kind", source.get("kind", "theorem")),
            "file": path or source.get("file", ""),
            "line": source.get("line"),
            "end_line": source.get("end_line"),
            "statement": source.get("statement", ""),
            "deps": list(row.get("deps", [])),
        })
    return targets


def _regex_targets(base: Path) -> list[dict]:
    _, declarations = _source_metadata(base)
    unresolved = [row for row in declarations if row["status"] in ("sorry", "axiom")]
    counts: dict[str, int] = {}
    for row in unresolved:
        counts[row["name"]] = counts.get(row["name"], 0) + 1
    ids = {
        (row["file"], row["name"]): (
            row["name"] if counts[row["name"]] == 1 else f'{row["file"]}:{row["name"]}'
        )
        for row in unresolved
    }
    unique_ids = {
        row["name"]: ids[(row["file"], row["name"])]
        for row in unresolved if counts[row["name"]] == 1
    }
    return [{
        "name": ids[(row["file"], row["name"])],
        "display_name": row["name"],
        "kind": row["kind"],
        "file": row["file"],
        "line": row["line"],
        "end_line": row["end_line"],
        "statement": row["statement"],
        "deps": [unique_ids[dependency] for dependency in row["deps"] if dependency in unique_ids],
    } for row in unresolved]


def _select_targets(candidates: list[dict], targets: str) -> list[dict]:
    requested = targets.strip()
    if not requested or requested.casefold() == "all":
        return candidates
    selectors = [part.strip() for part in re.split(r"[,\n]", requested) if part.strip()]
    selected: dict[str, dict] = {}
    for selector in selectors:
        normalized_file = selector.removeprefix("./")
        exact = [row for row in candidates if selector == row["name"]]
        file_matches = [row for row in candidates if normalized_file == row["file"]]
        if exact:
            matches = exact
        elif file_matches:
            matches = file_matches
        else:
            short_selector = selector.rsplit(".", 1)[-1]
            matches = [row for row in candidates if short_selector == row["display_name"]
                       or row["name"].endswith("." + selector)]
        if not matches:
            raise ValueError(f"prove target '{selector}' is not an unresolved declaration or Lean file")
        if len(matches) > 1 and not file_matches:
            names = ", ".join(sorted(row["name"] for row in matches))
            raise ValueError(f"prove target '{selector}' is ambiguous; use one of: {names}")
        selected.update((row["name"], row) for row in matches)
    return [row for row in candidates if row["name"] in selected]


def build_prove_dag(project_root: Path, unity_dir: Path, targets: str = "All") -> dict:
    """Mechanically create one prove chunk per unresolved target declaration."""
    project_root = Path(project_root)
    unity_dir = Path(unity_dir)

    # Bring oleans up to date before kernel inspection. A broken project simply uses
    # the textual fallback; proof holes and axioms themselves do not make Lake fail.
    try:
        subprocess.run(["lake", "build"], cwd=project_root, check=False)
    except OSError:
        pass
    kernel = kernel_extract(project_root)
    candidates = _kernel_targets(project_root, kernel) if kernel else _regex_targets(project_root)
    selected = _select_targets(candidates, targets)
    selected_ids = {row["name"] for row in selected}

    chunks = []
    for row in selected:
        chunk = {
            "id": row["name"],
            "title": row["display_name"],
            "summary": f'Prove {row["kind"]} {row["display_name"]}.',
            "dependencies": sorted(set(row["deps"]) & selected_ids),
            "status": "pending",
            "statement": row["statement"],
            "type": row["kind"],
            "declarations": [row["name"]],
            "lean_decl": row["name"],
            "lean_file": row["file"],
        }
        if row["line"] is not None and row["end_line"] is not None:
            chunk["lean_decl_lines"] = [row["line"], row["end_line"]]
        chunks.append(chunk)

    dag = {"source": "kernel" if kernel else "regex", "chunks": chunks}
    unity_dir.mkdir(parents=True, exist_ok=True)
    (unity_dir / "dag.json").write_text(json.dumps(dag, indent=2) + "\n")
    return dag
