"""Main autoformalization pipeline for Unity Agent."""

import asyncio
import atexit
import hashlib
import os
import re
import shutil
import sys
import time
import json
import logging
import subprocess
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, AgentDefinition, TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage, HookMatcher

_console: Console | None = None

# Package-relative directory constants (fixed at import time)
_PKG = Path(__file__).parent
_PROMPTS_DIR = _PKG / "PROMPTS"
_TEAMS_DIR = _PKG / "TEAMS"
_SUBAGENTS_DIR = _PKG / "SUBAGENTS"
_DEFAULT_PROMPTS_DIR = _PKG / "DEFAULT_PROMPTS"
_DEFAULT_SUBAGENTS_DIR = _PKG / "DEFAULT_SUBAGENTS"

_ALL_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill"]


def _log_agent_message(message) -> None:
    """Log an agent SDK message with rich formatting."""
    if isinstance(message, TaskStartedMessage):
        _console.print(f"[bold green]↳ Agent spawned[/bold green]  [dim]{message.task_id}[/dim] — {message.description!r}")
    elif isinstance(message, TaskProgressMessage):
        last = getattr(message, "last_tool_name", None)
        tool = f"  [cyan]{last}[/cyan]" if last else ""
        _console.print(f"[blue]→[/blue] Progress  [dim]{message.task_id}[/dim]{tool}")
    elif isinstance(message, TaskNotificationMessage):
        color = {"completed": "green", "failed": "red", "stopped": "yellow"}.get(message.status, "white")
        summary = getattr(message, "summary", None)
        suffix = f" — {summary!r}" if summary else ""
        _console.print(f"[bold {color}]✓ Agent {message.status}[/bold {color}]  [dim]{message.task_id}[/dim]{suffix}")
    elif isinstance(message, AssistantMessage):
        agent = f"[dim]\\[{message.parent_tool_use_id[:8]}][/dim] " if message.parent_tool_use_id else ""
        for block in message.content:
            if hasattr(block, "thinking"):
                _console.print(f"{agent}[dim italic]💭 {block.thinking[:300]}[/dim italic]")
            elif hasattr(block, "text") and block.text.strip():
                _console.print(f"{agent}{block.text[:500]}")
            elif hasattr(block, "name") and hasattr(block, "input"):
                _console.print(f"{agent}[cyan]⚙  {block.name}[/cyan]({str(block.input)[:200]})")
    elif isinstance(message, ResultMessage):
        _console.print(f"[bold green]✓ Complete[/bold green] — {message.num_turns} turns, [yellow]${message.total_cost_usd:.4f}[/yellow], stop={message.stop_reason}")


def _run(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(
            f"Command failed (rc={result.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"cwd: {cwd}\nstdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr,
        )
    return result


_RATE_LIMIT_PATTERN = re.compile(
    r"rate.?limit|429|too many requests|overloaded|retry.after",
    re.IGNORECASE,
)


def _commit_phase(phase_name: str, metadata: dict | None = None) -> None:
    """Commit current state with a parseable phase boundary message."""
    meta_str = " ".join(f"{k}={v}" for k, v in (metadata or {}).items())
    msg = f"PHASE:{phase_name} status=complete"
    if meta_str:
        msg += f" {meta_str}"
    try:
        subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", msg, "--allow-empty"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip() if isinstance(e.stderr, bytes) else (e.stderr or "").strip()
        logging.warning(
            f"_commit_phase('{phase_name}') failed (rc={e.returncode}): "
            f"{stderr or 'no stderr'} — resolver will see stale last-checkpoint"
        )


_JSON_ESCAPE_OR_STRAY = re.compile(r'\\(?:["\\/bfnrtu]|u[0-9a-fA-F]{4})|\\')


def _load_chunk_json(path: Path):
    """Parse chunk JSON, repairing stray LaTeX backslashes that weaker models leave unescaped."""
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        def _fix(m: re.Match) -> str:
            s = m.group(0)
            return s if len(s) > 1 else '\\\\'
        repaired = _JSON_ESCAPE_OR_STRAY.sub(_fix, text)
        return json.loads(repaired)


def _is_lean_repo(path: Path) -> bool:
    return (path / "lean-toolchain").exists() and (
        (path / "lakefile.lean").exists() or (path / "lakefile.toml").exists()
    )


def _get_library_dir() -> Path:
    return Path.home() / ".unity" / "library"


def _get_project_notes_dir() -> Path:
    return Path.cwd() / ".unity"


def _init_library() -> None:
    """Create global library and project notes directories if they don't exist."""
    lib = _get_library_dir()
    for subdir in ("tactics", "lemmas", "ir-patterns", "subagents", "references"):
        (lib / subdir).mkdir(parents=True, exist_ok=True)
    _get_project_notes_dir().mkdir(exist_ok=True)
    _seed_default_library()


def _seed_default_library() -> None:
    """Copy bundled DEFAULT_LIBRARY and scripts to ~/.unity/ if not already present."""
    package_root = Path(__file__).parent

    # Seed reference docs and any other DEFAULT_LIBRARY content
    default_lib = package_root / "DEFAULT_LIBRARY"
    if default_lib.exists():
        lib = _get_library_dir()
        for src in sorted(default_lib.rglob("*.md")):
            rel = src.relative_to(default_lib)
            dst = lib / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)

    # Seed helper scripts
    scripts_src = package_root / "scripts"
    if scripts_src.exists():
        scripts_dst = Path.home() / ".unity" / "scripts"
        scripts_dst.mkdir(parents=True, exist_ok=True)
        for src in sorted(scripts_src.iterdir()):
            dst = scripts_dst / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
                # Make shell/python scripts executable
                if src.suffix in (".sh", ".py"):
                    dst.chmod(dst.stat().st_mode | 0o111)


def _load_library_context() -> str:
    """Build a compact manifest of available library files for injection into prompts.

    Returns an empty string if no library or project note files exist.
    Agents use the Read tool to access individual files on demand.
    """
    def first_heading(path: Path) -> str:
        """Return the first markdown heading in a file as a one-line description."""
        try:
            for line in path.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith("#"):
                    return re.sub(r"^#+\s*", "", line)
        except OSError:
            pass
        return path.stem.replace("-", " ").replace("_", " ").title()

    sections: list[str] = []
    lib = _get_library_dir()

    for subdir in ("tactics", "lemmas", "ir-patterns", "references"):
        subdir_path = lib / subdir
        if not subdir_path.exists():
            continue
        files = sorted(f for f in subdir_path.glob("*.md") if f.stat().st_size > 0)
        if not files:
            continue
        lines = [f"*{subdir}/*"]
        for f in files:
            lines.append(f"- `{f}` — {first_heading(f)}")
        sections.append("\n".join(lines))

    notes_dir = _get_project_notes_dir()
    note_files = [f for f in sorted(notes_dir.glob("*.md")) if f.exists() and f.stat().st_size > 0]
    if note_files:
        lines = ["*project notes — `.unity/`*"]
        for f in note_files:
            lines.append(f"- `{f}` — {first_heading(f)}")
        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = "**Library** — `~/.unity/library/`\n\nUse `Read` to access any file listed below.\n\n"
    return header + "\n\n".join(sections)


# Populated at run_pipeline startup; available to all query() agents= dicts.
LIBRARY_SUBAGENTS: dict = {}


def _load_library_subagents() -> dict:
    """Parse ~/.unity/library/subagents/*.md into AgentDefinition objects."""
    result = {}
    subagents_dir = _get_library_dir() / "subagents"
    for md_file in sorted(subagents_dir.glob("*.md")):
        raw = md_file.read_text()
        # Parse YAML-lite frontmatter between --- delimiters
        fm_match = re.match(r"^---\n(.*?)\n---\n(.*)", raw, re.DOTALL)
        if not fm_match:
            continue
        fm_text, body = fm_match.group(1), fm_match.group(2).strip()
        meta: dict = {}
        for line in fm_text.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        name = meta.get("name", md_file.stem)
        description = meta.get("description", "")
        tools_raw = meta.get("tools", "Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Agent,Skill")
        tools = [t.strip() for t in tools_raw.split(",")]
        result[name] = AgentDefinition(description=description, prompt=body, tools=tools)
    return result


def _toposort_chunks(language_dir: Path) -> None:
    """Read chunk JSONs from language/chunks/, run Kahn's toposort, write dag.json."""
    chunks_dir = language_dir / "chunks"
    if not chunks_dir.exists():
        logging.info("No language/chunks/ directory — skipping toposort.")
        return

    chunks = []
    for f in sorted(chunks_dir.glob("*.json")):
        try:
            chunks.append(_load_chunk_json(f))
        except Exception as e:
            logging.warning(f"Could not parse chunk file {f}: {e}")

    if not chunks:
        logging.info("No chunk JSON files found — skipping toposort.")
        return

    chunk_ids = {c["id"] for c in chunks}
    in_degree: dict[str, int] = {c["id"]: 0 for c in chunks}
    dependents: dict[str, list[str]] = {c["id"]: [] for c in chunks}

    for c in chunks:
        for dep in c.get("dependencies", []):
            dep_id = dep["chunk_id"] if isinstance(dep, dict) else dep
            if dep_id in chunk_ids:
                in_degree[c["id"]] += 1
                dependents[dep_id].append(c["id"])

    layers: list[list[str]] = []
    ready = sorted(cid for cid in chunk_ids if in_degree[cid] == 0)

    while ready:
        layers.append(ready)
        nxt: list[str] = []
        for cid in ready:
            for child in dependents[cid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    nxt.append(child)
        ready = sorted(nxt)

    remaining = {cid for cid, deg in in_degree.items() if deg > 0}
    if remaining:
        logging.warning(f"Cycle detected in chunk dependency graph involving: {remaining}. Appending as final layer.")
        layers.append(sorted(remaining))

    layer_of = {cid: i for i, layer in enumerate(layers) for cid in layer}
    chunk_index = {c["id"]: c for c in chunks}

    dag_chunks = []
    for layer_idx, layer in enumerate(layers):
        for cid in layer:
            c = chunk_index[cid]
            dag_chunks.append({
                "id": cid,
                "layer": layer_idx,
                "type": c.get("type", "other"),
                "title": c.get("title", cid),
                "summary": c.get("summary", ""),
                "dependencies": [
                    d["chunk_id"] if isinstance(d, dict) else d
                    for d in c.get("dependencies", [])
                ],
                "lean_file": None,
                "lean_decl_lines": None,
                "status": "pending",
            })

    dag = {"layers": layers, "chunks": dag_chunks}
    Path("dag.json").write_text(json.dumps(dag, indent=2))
    logging.info(f"dag.json written: {len(chunks)} chunks across {len(layers)} layers.")


def _create_worktree(chunk_id: str, project_path: Path) -> Path:
    """Create a git worktree for chunk_id inside the Lean project; return its path."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", chunk_id)
    worktree_path = project_path / ".worktrees" / safe_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    gitignore = project_path / ".gitignore"
    ignore_entry = ".worktrees/"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if ignore_entry not in existing.splitlines():
        with gitignore.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"{ignore_entry}\n")
    _run(["git", "worktree", "add", "-b", f"worktree/{safe_id}", str(worktree_path)], cwd=project_path)
    return worktree_path


def _write_worktrees_manifest(worktree_assignments: dict[str, str]) -> None:
    """Write worktrees.json at CWD (next to dag.json) mapping chunk_id → worktree info."""
    manifest = {
        cid: {
            "worktree_path": wt,
            "branch": f"worktree/{re.sub(r'[^a-zA-Z0-9_-]', '_', cid)}",
            "status": "pending",
        }
        for cid, wt in worktree_assignments.items()
    }
    Path("worktrees.json").write_text(json.dumps(manifest, indent=2))


def _delete_worktrees_manifest() -> None:
    Path("worktrees.json").unlink(missing_ok=True)


def _symlink_lake_cache(worktree_path: Path, project_path: Path) -> None:
    """Symlink .lake/packages/ from main project into worktree to share the Mathlib cache."""
    packages_src = project_path / ".lake" / "packages"
    if not packages_src.exists():
        return
    lake_dir = worktree_path / ".lake"
    lake_dir.mkdir(exist_ok=True)
    packages_link = lake_dir / "packages"
    if not packages_link.exists():
        packages_link.symlink_to(packages_src.resolve())


def _detect_main_branch(project_path: Path) -> str:
    """Detect the default branch of the Lean project at startup."""
    res = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=project_path, capture_output=True, text=True,
    )
    return res.stdout.strip() or "main"


def _audit_worktree_commits(worktree_assignments: dict[str, str], project_path: Path, main_branch: str = "main") -> dict:
    """Audit post-orchestrator state per chunk. Returns {chunk_id: {committed, merged, dirty}}.

    - committed: subagent committed at least one chunk commit on the worktree branch
    - merged:    main branch shows a "UNITY: merge chunk <chunk_id>" commit after the run
    - dirty:     worktree still has uncommitted changes (subagent worked but never committed)
    Issues a WARNING log per chunk that failed any expectation so silent work loss is surfaced.
    """
    report: dict = {}
    for chunk_id, wt in worktree_assignments.items():
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", chunk_id)
        branch = f"worktree/{safe_id}"
        wt_path = Path(wt)

        dirty = False
        committed = False
        merged = False

        if wt_path.exists():
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=wt_path, capture_output=True, text=True,
            )
            dirty = status.returncode == 0 and bool(status.stdout.strip())

        branch_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=project_path,
        ).returncode == 0
        if branch_exists:
            log_res = subprocess.run(
                ["git", "log", f"{main_branch}..{branch}", "--oneline"],
                cwd=project_path, capture_output=True, text=True,
            )
            committed = log_res.returncode == 0 and bool(log_res.stdout.strip())

        merge_grep = subprocess.run(
            ["git", "log", main_branch, "--grep", f"UNITY: merge chunk {chunk_id}", "--oneline"],
            cwd=project_path, capture_output=True, text=True,
        )
        merged = merge_grep.returncode == 0 and bool(merge_grep.stdout.strip())

        if dirty:
            # Rescue: auto-commit the dirty changes on the worktree branch so work
            # survives the upcoming `git worktree remove --force`. Broken code in
            # git history is strictly better than deleted code.
            add_res = subprocess.run(
                ["git", "-C", str(wt_path), "add", "-A"], capture_output=True, text=True,
            )
            if add_res.returncode == 0:
                commit_res = subprocess.run(
                    ["git", "-C", str(wt_path), "commit", "-m",
                     f"EMERGENCY: auto-commit dirty worktree for chunk {chunk_id}"],
                    capture_output=True, text=True,
                )
                if commit_res.returncode == 0:
                    logging.error(
                        f"[audit] chunk {chunk_id}: RESCUED {wt_path} — subagent failed to commit; "
                        f"work preserved via EMERGENCY commit on branch '{branch}'."
                    )
                    committed = True
                    dirty = False
                else:
                    logging.warning(
                        f"[audit] chunk {chunk_id}: rescue commit FAILED ({commit_res.stderr.strip()}); "
                        f"work at {wt_path} will be LOST at cleanup."
                    )
            else:
                logging.warning(
                    f"[audit] chunk {chunk_id}: rescue `git add -A` FAILED ({add_res.stderr.strip()}); "
                    f"work at {wt_path} will be LOST at cleanup."
                )

        report[chunk_id] = {"committed": committed, "merged": merged, "dirty": dirty}
        if not committed and not merged:
            logging.warning(
                f"[audit] chunk {chunk_id}: no commits on branch '{branch}' beyond main and no "
                f"merge commit on main — subagent likely returned without doing work."
            )
        elif committed and not merged:
            logging.warning(
                f"[audit] chunk {chunk_id}: branch '{branch}' has commits but orchestrator did not "
                f"squash-merge them into main — merge step was skipped."
            )
        else:
            logging.info(
                f"[audit] chunk {chunk_id}: ok (committed={committed}, merged={merged}, dirty={dirty})"
            )
    return report


def _cleanup_worktree(worktree_path: Path, project_path: Path, chunk_id: str) -> None:
    """Remove the git worktree and its branch. Tolerant of missing branch. Raises on other failures."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", chunk_id)
    _run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=project_path)
    branch = f"worktree/{safe_id}"
    ref_check = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=project_path,
    )
    if ref_check.returncode == 0:
        _run(["git", "branch", "-D", branch], cwd=project_path)
    else:
        logging.info(f"Branch {branch} already gone — skipping delete.")


_TOP_LEVEL_DECL = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?"
    r"(?:noncomputable\s+|private\s+|protected\s+)*"
    r"(theorem|lemma|def|example|instance|structure|inductive|class|abbrev|opaque|axiom)\b"
)


def _strip_lean_comments(src: str) -> str:
    src = re.sub(r"/-.*?-/", "", src, flags=re.DOTALL)
    src = re.sub(r"--[^\n]*", "", src)
    return src


def _strip_lean_comments_preserve_lines(src: str) -> str:
    """Strip Lean block and line comments, replacing interior chars with spaces so line numbers stay stable."""
    src = re.sub(r"/-.*?-/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), src, flags=re.DOTALL)
    src = re.sub(r"--[^\n]*", lambda m: " " * len(m.group(0)), src)
    return src


def _audit_illegitimate_sorries(run_dir: Path, project_path: Path) -> list[dict]:
    """Scan the project for any `sorry` not inside an `is_assumption: true` chunk body.

    Returns a list of violation records and writes ILLEGITIMATE_SORRIES.md. A sorry is
    *legitimate* iff it lies within the declaration body of a chunk whose `is_assumption`
    is True. Every other `\\bsorry\\b` — including those in formalizer-introduced helper
    lemmas (cascade sorries) — is a violation.
    """
    chunk_dirs = [run_dir / "semiformal" / "chunks", run_dir / "language" / "chunks"]
    chunk_files: dict[str, Path] = {}
    for cd in chunk_dirs:
        if cd.is_dir():
            for p in sorted(cd.glob("*.json")):
                chunk_files.setdefault(p.stem, p)

    intervals_by_file: dict[Path, list[tuple[int, int, str, bool]]] = {}
    for chunk_id, chunk_path in chunk_files.items():
        try:
            chunk = _load_chunk_json(chunk_path)
        except Exception as e:
            logging.warning(f"audit: failed to read {chunk_path}: {e}")
            continue
        loc = _resolve_chunk_decl_loc(chunk_id, chunk, run_dir)
        if loc is None:
            continue
        file_rel, line = loc
        lean_file = (project_path / file_rel).resolve()
        intervals_by_file.setdefault(lean_file, []).append(
            (line, -1, chunk_id, bool(chunk.get("is_assumption")))
        )

    resolved_intervals: dict[Path, list[tuple[int, int, str, bool]]] = {}
    for lean_file, entries in intervals_by_file.items():
        try:
            text = lean_file.read_text()
        except Exception:
            continue
        lines_count = len(text.splitlines())
        decl_starts = sorted({
            i + 1 for i, ln in enumerate(text.splitlines()) if _TOP_LEVEL_DECL.match(ln)
        })
        resolved = []
        for start, _, cid, is_asm in sorted(entries):
            end = lines_count
            for s in decl_starts:
                if s > start:
                    end = s - 1
                    break
            resolved.append((start, end, cid, is_asm))
        resolved_intervals[lean_file] = resolved

    violations: list[dict] = []
    for lean_file in project_path.rglob("*.lean"):
        if any(p in (".lake", "lake-packages", "build") for p in lean_file.parts):
            continue
        try:
            text = lean_file.read_text()
        except Exception:
            continue
        stripped = _strip_lean_comments_preserve_lines(text)
        intervals = resolved_intervals.get(lean_file.resolve(), [])
        for idx, ln in enumerate(stripped.splitlines(), start=1):
            if not re.search(r"\bsorry\b", ln):
                continue
            containing = next(
                ((cid, is_asm) for (s, e, cid, is_asm) in intervals if s <= idx <= e),
                None,
            )
            if containing is not None and containing[1]:
                continue  # legitimate: inside an is_assumption chunk body
            rel = str(lean_file.relative_to(project_path)) if lean_file.is_relative_to(project_path) else str(lean_file)
            violations.append({
                "chunk_id": containing[0] if containing else None,
                "file": rel,
                "line": idx,
            })

    for v in violations:
        cid = v["chunk_id"] or "<outside any tracked chunk>"
        logging.error(f"ILLEGITIMATE sorry at {v['file']}:{v['line']} (chunk: {cid})")

    report_path = run_dir / "ILLEGITIMATE_SORRIES.md"
    if violations:
        lines_out = [
            "# Illegitimate Sorries", "",
            f"Found {len(violations)} illegitimate `sorry` occurrence(s). "
            f"A sorry is legitimate only inside an `is_assumption: true` chunk body; "
            f"helper-lemma (\"cascade\") sorries and sorries in non-assumption chunks are violations.", "",
        ]
        for v in violations:
            cid = v["chunk_id"] or "outside any tracked chunk"
            lines_out.append(f"- {v['file']}:{v['line']} — {cid}")
        report_path.write_text("\n".join(lines_out) + "\n")
    else:
        report_path.write_text("# Illegitimate Sorries\n\nNone detected.\n")

    return violations


def _resolve_chunk_decl_loc(chunk_id: str, chunk: dict, run_dir: Path) -> tuple[str, int] | None:
    """Resolve (file_rel, line) for a chunk's Lean declaration.
    Prefers chunk JSON's `lean_declaration`; falls back to dag.json's `lean_file`/`lean_decl_lines`."""
    decl = chunk.get("lean_declaration") or {}
    file_rel = decl.get("file")
    line = decl.get("line")
    if file_rel and isinstance(line, int):
        return file_rel, line
    try:
        dag = json.loads((run_dir / "dag.json").read_text())
    except Exception:
        return None
    for node in dag.get("chunks") or []:
        if node.get("id") != chunk_id:
            continue
        f = node.get("lean_file")
        lines = node.get("lean_decl_lines")
        if f and isinstance(lines, list) and lines and isinstance(lines[0], int):
            return f, lines[0]
        break
    return None


def _collect_chunk_sorry_set(run_dir: Path, project_path: Path) -> frozenset[str]:
    """Return the set of chunk IDs currently carrying a sorry (any chunk, assumption or not)."""
    out: set[str] = set()
    chunk_dirs = [run_dir / "semiformal" / "chunks", run_dir / "language" / "chunks"]
    seen: dict[str, Path] = {}
    for cd in chunk_dirs:
        if cd.is_dir():
            for p in sorted(cd.glob("*.json")):
                seen.setdefault(p.stem, p)
    by_file: dict[Path, list[dict]] = {}
    for chunk_id, chunk_path in seen.items():
        try:
            chunk = _load_chunk_json(chunk_path)
        except Exception:
            continue
        loc = _resolve_chunk_decl_loc(chunk_id, chunk, run_dir)
        if loc is None:
            continue
        file_rel, line = loc
        by_file.setdefault((project_path / file_rel).resolve(), []).append({"id": chunk_id, "line": line})
    for lean_file, entries in by_file.items():
        try:
            text = lean_file.read_text()
        except Exception:
            continue
        lines = text.splitlines()
        decl_starts = sorted({i + 1 for i, ln in enumerate(lines) if _TOP_LEVEL_DECL.match(ln)})
        entries.sort(key=lambda e: e["line"])
        for entry in entries:
            start = entry["line"]
            end = len(lines)
            for s in decl_starts:
                if s > start:
                    end = s - 1
                    break
            if start < 1 or start > len(lines):
                continue
            body = "\n".join(lines[start - 1:end])
            stripped = _strip_lean_comments(body)
            if re.search(r"\bsorry\b", stripped):
                out.add(entry["id"])
    return frozenset(out)


def _chunk_body_signatures(run_dir: Path, project_path: Path) -> dict[str, tuple[str, bool]]:
    """Per-chunk (body_hash, has_sorry). Mirrors _collect_chunk_sorry_set's body-extraction logic."""
    out: dict[str, tuple[str, bool]] = {}
    chunk_dirs = [run_dir / "semiformal" / "chunks", run_dir / "language" / "chunks"]
    seen: dict[str, Path] = {}
    for cd in chunk_dirs:
        if cd.is_dir():
            for p in sorted(cd.glob("*.json")):
                seen.setdefault(p.stem, p)
    by_file: dict[Path, list[dict]] = {}
    for chunk_id, chunk_path in seen.items():
        try:
            chunk = _load_chunk_json(chunk_path)
        except Exception:
            continue
        loc = _resolve_chunk_decl_loc(chunk_id, chunk, run_dir)
        if loc is None:
            continue
        file_rel, line = loc
        by_file.setdefault((project_path / file_rel).resolve(), []).append({"id": chunk_id, "line": line})
    for lean_file, entries in by_file.items():
        try:
            text = lean_file.read_text()
        except Exception:
            continue
        lines = text.splitlines()
        decl_starts = sorted({i + 1 for i, ln in enumerate(lines) if _TOP_LEVEL_DECL.match(ln)})
        entries.sort(key=lambda e: e["line"])
        for entry in entries:
            start = entry["line"]
            end = len(lines)
            for s in decl_starts:
                if s > start:
                    end = s - 1
                    break
            if start < 1 or start > len(lines):
                continue
            body = "\n".join(lines[start - 1:end])
            stripped = _strip_lean_comments(body)
            has_sorry = bool(re.search(r"\bsorry\b", stripped))
            h = hashlib.sha256(stripped.encode("utf-8", "replace")).hexdigest()[:16]
            out[entry["id"]] = (h, has_sorry)
    return out


def _default_escalation_state() -> dict:
    return {"chunks": {}, "secondary_spend": 0.0}


def _load_escalation_state(path: Path) -> dict:
    if path.exists():
        try:
            state = json.loads(path.read_text())
            state.setdefault("chunks", {})
            state.setdefault("secondary_spend", 0.0)
            return state
        except Exception:
            pass
    return _default_escalation_state()


def _save_escalation_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def _update_stagnation(state: dict, current_sigs: dict[str, tuple[str, bool]]) -> None:
    """Bump stagnation counter for any chunk that still carries a sorry, regardless of body edits."""
    for cid, (h, s) in current_sigs.items():
        entry = state["chunks"].setdefault(cid, {"prev_sig": None, "stagnation": 0, "last_escalation": None})
        if not s:
            entry["stagnation"] = 0
        else:
            entry["stagnation"] = int(entry.get("stagnation", 0)) + 1
        entry["prev_sig"] = [h, s]


def _stagnant_chunks(state: dict, threshold: int = 2) -> list[str]:
    return sorted(
        cid for cid, e in state["chunks"].items()
        if int(e.get("stagnation", 0)) >= threshold
        and e.get("prev_sig") and e["prev_sig"][1]
    )


def _append_escalated_log(run_dir: Path, iteration: int, chunks: list[str], cost: float, t_sec: float, secondary_spend_total: float) -> None:
    log_path = run_dir / "ESCALATED.md"
    header = not log_path.exists()
    with log_path.open("a") as f:
        if header:
            f.write("# Escalation Log\n")
        f.write(f"\n## Iteration {iteration}\n")
        f.write(f"- chunks: {', '.join(chunks)}\n")
        f.write(f"- cost: ${cost:.4f}\n")
        f.write(f"- wall_time: {t_sec:.1f}s\n")
        f.write(f"- secondary_spend_total: ${secondary_spend_total:.4f}\n")


def _assert_semiformal_field_propagation(run_dir: Path) -> list[dict]:
    """Verify each semiformal chunk carries is_assumption/source_range/source_proof matching its language chunk.

    Writes SEMIFORMAL_FIELD_DRIFT.md and logs one error per drift. Non-halting:
    drift is surfaced so the next iteration can correct it.
    """
    language_dir = run_dir / "language" / "chunks"
    semiformal_dir = run_dir / "semiformal" / "chunks"
    if not language_dir.is_dir() or not semiformal_dir.is_dir():
        return []

    drifts: list[dict] = []
    for semi_path in sorted(semiformal_dir.glob("*.json")):
        chunk_id = semi_path.stem
        lang_path = language_dir / semi_path.name
        if not lang_path.exists():
            drifts.append({"chunk_id": chunk_id, "field": "(missing language chunk)", "language": None, "semiformal": None})
            continue
        try:
            lang = _load_chunk_json(lang_path)
            semi = _load_chunk_json(semi_path)
        except Exception as e:
            drifts.append({"chunk_id": chunk_id, "field": f"(parse error: {e})", "language": None, "semiformal": None})
            continue
        for field in ("is_assumption", "source_range", "source_proof"):
            lv, sv = lang.get(field), semi.get(field)
            if lv != sv:
                drifts.append({"chunk_id": chunk_id, "field": field, "language": lv, "semiformal": sv})

    for d in drifts:
        logging.error(
            f"SEMIFORMAL FIELD DRIFT in chunk {d['chunk_id']} "
            f"field={d['field']}: language={d['language']!r} semiformal={d['semiformal']!r}"
        )

    report_path = run_dir / "SEMIFORMAL_FIELD_DRIFT.md"
    if drifts:
        lines_out = ["# Semiformal Field Drift", "",
                     f"Found {len(drifts)} drift(s) — `is_assumption`/`source_range`/`source_proof` must be copied verbatim from `language/chunks/<id>.json`.", ""]
        for d in drifts:
            lines_out.append(f"- `{d['chunk_id']}` field `{d['field']}`: language=`{d['language']}` semiformal=`{d['semiformal']}`")
        report_path.write_text("\n".join(lines_out) + "\n")
    else:
        report_path.write_text("# Semiformal Field Drift\n\nNone detected.\n")

    return drifts


async def _warm_lean_lsp(project_path: Path) -> None:
    """Pre-warm the Lean LSP by opening a trivial file, loading OLEANs into the OS page cache."""
    def _do_warmup():
        try:
            from leanclient import LeanLSPClient
        except ImportError:
            return
        warmup_file = project_path / ".unity_lsp_warmup.lean"
        try:
            warmup_file.write_text("import Mathlib\n\n#check Nat.add_comm\n")
            client = LeanLSPClient(str(project_path), initial_build=False)
            try:
                client.get_diagnostics(".unity_lsp_warmup.lean", inactivity_timeout=120.0)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        finally:
            warmup_file.unlink(missing_ok=True)

    await asyncio.to_thread(_do_warmup)


async def _infer_flags() -> tuple[str | None, str | None, bool]:
    """Run a lightweight inference agent to detect source, project, and prove from CWD."""
    cwd_env = Path.cwd() / ".env"
    package_env = Path(__file__).parent.parent / ".env"
    load_dotenv(dotenv_path=cwd_env)
    load_dotenv(dotenv_path=package_env, override=False)

    infer_file = Path(".unity_infer.json")
    infer_file.unlink(missing_ok=True)

    with open(_PROMPTS_DIR / "INFERENCE.md") as f:
        infer_prompt = f.read()

    async for _ in query(
        prompt="Infer the source, project, and prove flag for the current working directory.",
        options=ClaudeAgentOptions(
            tools=["Read", "Glob", "Grep", "Write", "Bash"],
            allowed_tools=["Read", "Glob", "Grep", "Write", "Bash"],
            agents={},
            system_prompt=infer_prompt,
            permission_mode="bypassPermissions",

            model="sonnet",
            fallback_model="haiku",
            env={k: v for k, v in {
                "ANTHROPIC_BASE_URL": os.getenv("PRIMARY_BASE_URL"),
                "ANTHROPIC_API_KEY": os.getenv("PRIMARY_API_KEY"),
                "ANTHROPIC_AUTH_TOKEN": os.getenv("PRIMARY_AUTH_TOKEN"),
                "ANTHROPIC_DEFAULT_OPUS_MODEL": os.getenv("PRIMARY_MODEL"),
                "ANTHROPIC_DEFAULT_SONNET_MODEL": os.getenv("PRIMARY_MODEL"),
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": os.getenv("PRIMARY_MODEL"),
            }.items() if v},

        ),
    ):
        pass

    if not infer_file.exists():
        return None, None, False
    try:
        data = json.loads(infer_file.read_text())
        infer_file.unlink(missing_ok=True)
        return data.get("source"), data.get("project"), bool(data.get("prove", False))
    except Exception:
        infer_file.unlink(missing_ok=True)
        return None, None, False


async def run_pipeline(source: str | None, project_dir: str, context: bool, prove: bool = False, depth: int = 1, output_dir: str | None = None):
    """Run the full autoformalization pipeline."""
    global _console
    PROJECT_DIR = project_dir

    # Helper functions for environment variable parsing
    def parse_bool(val: str | None) -> bool:
        """Parse string env var to boolean."""
        return val is not None and val.lower() in ("true", "1", "yes")

    def parse_float(val: str | None) -> float | None:
        """Parse string env var to float, or None if not set/empty."""
        if not val or val.lower() == "none":
            return None
        return float(val)

    def parse_int(val: str | None) -> int | None:
        """Parse string env var to int, or None if not set/empty."""
        if not val or val.lower() == "none":
            return None
        return int(val)

    # Load environment
    try:
        cwd_env = Path.cwd() / ".env"
        package_env = Path(__file__).parent.parent / ".env"
        load_dotenv(dotenv_path=cwd_env)
        load_dotenv(dotenv_path=package_env, override=False)
        silent = parse_bool(os.getenv("SILENT"))
        recording = parse_bool(os.getenv("RECORDING"))

        # Reroute output if needed
        if silent:
            sys.stdout = open("unity.out", 'w')
            sys.stderr = open("unity.err", 'w')
        elif recording:
            class _Tee:
                def __init__(self, stream, path):
                    self._file = open(path, 'w')
                    self._stream = stream
                def write(self, data):
                    self._stream.write(data)
                    self._file.write(data)
                def flush(self):
                    self._stream.flush()
                    self._file.flush()
                def isatty(self):
                    return hasattr(self._stream, "isatty") and self._stream.isatty()
            sys.stdout = _Tee(sys.stdout, "unity.out")
            sys.stderr = _Tee(sys.stderr, "unity.err")

        _console = Console(file=sys.stdout)
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(console=_console, rich_tracebacks=True, show_path=False)],
            force=True,
        )

        if output_dir is not None:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            os.chdir(output_dir)

        logging.info(f"DEPTH: {depth}")
        logging.info("Loading environment...")

        # Set environment
        save_spec = parse_bool(os.getenv("SAVE_SPECIFICATION"))
        no_bypass = parse_bool(os.getenv("NO_BYPASS"))
        source_scan_budget = parse_float(os.getenv("SOURCE_SCAN_BUDGET"))
        generation_budget = parse_float(os.getenv("GENERATION_BUDGET"))
        validation_budget = parse_float(os.getenv("VALIDATION_BUDGET"))
        semiformalization_budget = parse_float(os.getenv("SEMIFORMALIZATION_BUDGET"))
        exploration_budget = parse_float(os.getenv("EXPLORATION_BUDGET"))
        formalization_budget = parse_float(os.getenv("FORMALIZATION_BUDGET"))
        critic_budget = parse_float(os.getenv("CRITIC_BUDGET"))
        secondary_budget = parse_float(os.getenv("SECONDARY_BUDGET"))
        save_semiformalization = parse_bool(os.getenv("SAVE_SEMIFORMALIZATION"))
        autofix = parse_bool(os.getenv("AUTOFIX"))
        exploration = parse_bool(os.getenv("EXPLORATION"))
        recurse = parse_bool(os.getenv("RECURSE"))
        max_critic_iterations = parse_int(os.getenv("MAX_CRITIC_ITERATIONS")) or 3
        max_validation_iterations = parse_int(os.getenv("MAX_VALIDATION_ITERATIONS"))
        forum_port = parse_int(os.getenv("FORUM_PORT")) or 6367
        lean_lsp_port = parse_int(os.getenv("LEAN_LSP_PORT")) or 6368
        claude_code_stream_close_timeout = parse_int(os.getenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT")) or 180000
        os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = str(claude_code_stream_close_timeout)
        sdk_idle_timeout = float(parse_int(os.getenv("SDK_MESSAGE_IDLE_TIMEOUT")) or 600)
        max_lsp_restarts = parse_int(os.getenv("MAX_LSP_RESTARTS_BEFORE_DEGRADE"))
        if max_lsp_restarts is None:
            max_lsp_restarts = 2
        primary_base_url = os.getenv("PRIMARY_BASE_URL")
        primary_api_key = os.getenv("PRIMARY_API_KEY")
        primary_auth_token = os.getenv("PRIMARY_AUTH_TOKEN")
        primary_model = os.getenv("PRIMARY_MODEL")
        secondary_base_url = os.getenv("SECONDARY_BASE_URL")
        secondary_api_key = os.getenv("SECONDARY_API_KEY")
        secondary_auth_token = os.getenv("SECONDARY_AUTH_TOKEN")
        secondary_model = os.getenv("SECONDARY_MODEL")
        claude_code_experimental_agent_teams = os.getenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")

        # Build per-tier env dicts for agent query() calls. Unset values are omitted
        # so the SDK falls back to its own credential / model resolution.
        # Primary-tier queries use model="opus" (with fallback_model="sonnet") for most phases; we pin
        # all three DEFAULT_*_MODEL slots to PRIMARY_MODEL so stray routing never crosses
        # tiers. Secondary mirrors the pattern with SECONDARY_MODEL and the secondary
        # provider's credentials; the escalation phase uses _secondary_env.
        _primary_env = {k: v for k, v in {
            "ANTHROPIC_BASE_URL": primary_base_url,
            "ANTHROPIC_API_KEY": primary_api_key,
            "ANTHROPIC_AUTH_TOKEN": primary_auth_token,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": primary_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": primary_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": primary_model,
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": claude_code_experimental_agent_teams,
            "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT": str(claude_code_stream_close_timeout),
        }.items() if v}
        _secondary_env = {k: v for k, v in {
            "ANTHROPIC_BASE_URL": secondary_base_url,
            "ANTHROPIC_API_KEY": secondary_api_key,
            "ANTHROPIC_AUTH_TOKEN": secondary_auth_token,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": secondary_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": secondary_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": secondary_model,
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": claude_code_experimental_agent_teams,
            "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT": str(claude_code_stream_close_timeout),
        }.items() if v}

        # Print environment
        logging.info("Environment:")
        logging.info(f"SAVE_SPECIFICATION: {save_spec}")
        logging.info(f"NO_BYPASS: {no_bypass}")
        logging.info(f"SOURCE_SCAN_BUDGET: {source_scan_budget}")
        logging.info(f"GENERATION_BUDGET: {generation_budget}")
        logging.info(f"VALIDATION_BUDGET: {validation_budget}")
        logging.info(f"SEMIFORMALIZATION_BUDGET: {semiformalization_budget}")
        logging.info(f"EXPLORATION_BUDGET: {exploration_budget}")
        logging.info(f"FORMALIZATION_BUDGET: {formalization_budget}")
        logging.info(f"CRITIC_BUDGET: {critic_budget}")
        logging.info(f"SECONDARY_BUDGET: {secondary_budget}")
        logging.info(f"MAX_VALIDATION_ITERATIONS: {max_validation_iterations}")
        logging.info(f"SILENT: {silent}")
        logging.info(f"RECORDING: {recording}")
        logging.info(f"SAVE_SEMIFORMALIZATION: {save_semiformalization}")
        logging.info(f"AUTOFIX: {autofix}")
        logging.info(f"EXPLORATION: {exploration}")
        logging.info(f"RECURSE: {recurse}")
        logging.info(f"FORUM_PORT: {forum_port}")
        logging.info(f"LEAN_LSP_PORT: {lean_lsp_port}")
        logging.info(f"CLAUDE_CODE_STREAM_CLOSE_TIMEOUT: {claude_code_stream_close_timeout}")
        logging.info(f"SDK_MESSAGE_IDLE_TIMEOUT: {sdk_idle_timeout}")
        logging.info(f"MAX_LSP_RESTARTS_BEFORE_DEGRADE: {max_lsp_restarts}")
        logging.info(f"PRIMARY_BASE_URL: {primary_base_url}")
        logging.info(f"PRIMARY_API_KEY: {primary_api_key}")
        logging.info(f"PRIMARY_AUTH_TOKEN: {primary_auth_token}")
        logging.info(f"PRIMARY_MODEL: {primary_model}")
        logging.info(f"SECONDARY_BASE_URL: {secondary_base_url}")
        logging.info(f"SECONDARY_API_KEY: {secondary_api_key}")
        logging.info(f"SECONDARY_AUTH_TOKEN: {secondary_auth_token}")
        logging.info(f"SECONDARY_MODEL: {secondary_model}")
        logging.info(f"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: {claude_code_experimental_agent_teams}")

        # Check for conflicts
        if not autofix and context:
            logging.critical("CRITICAL: cannot have context without autofix enabled")
            exit(1)

        if not exploration and recurse:
            logging.critical("CRITICAL: cannot have recurse without exploration enabled")
            exit(1)

        if recording and silent:
            logging.critical("CRITICAL: cannot have recording enabled with silent enabled")
            exit(1)

        # Select prompts directory
        _teams = parse_bool(claude_code_experimental_agent_teams)
        PROMPTS_DIR = _TEAMS_DIR if _teams else _PROMPTS_DIR
        PROVE_PROMPTS_DIR = PROMPTS_DIR / "PROVE"
        PROVE_SUBAGENTS_DIR = _SUBAGENTS_DIR / "PROVE"
        # Active dirs: prove mode swaps generation/semiformalization/formalization/critic
        ACTIVE_PROMPTS_DIR = PROVE_PROMPTS_DIR if prove else PROMPTS_DIR
        ACTIVE_SUBAGENTS_DIR = PROVE_SUBAGENTS_DIR if prove else _SUBAGENTS_DIR
        logging.info(f"Prompts directory: {PROMPTS_DIR}")

        # Prove-mode conflict checks
        if prove and source is None and not context:
            logging.critical("CRITICAL: --prove without --source requires --context/-c")
            exit(1)

        # --prove without --source: EXPLORATION=true runs the chunked pipeline; EXPLORATION=false
        # runs the unchunked strategy-parallel mode below.

        # Set permissions

        if no_bypass:
            PERMISSIONS="acceptEdits"
        else:
            PERMISSIONS="bypassPermissions"

        logging.info("Environment loaded successfully!")
    except Exception as e:
        logging.critical(f"CRITICAL (environment loading): {e}")
        exit(1)

    # Lean project initialization

    logging.info("Initializing Lean project...")
    try:
        # Create Lean project (if needed), get cache, and update
        project_path = Path(PROJECT_DIR).expanduser().resolve()

        if not project_path.exists():
            project_path.parent.mkdir(parents=True, exist_ok=True)
            _run(["lake", "new", project_path.name, "math"], cwd=project_path.parent)
        elif not _is_lean_repo(project_path):
            _run(["lake", "init", project_path.name, "math"], cwd=project_path)

        logging.info("Lean project initialized successfully!")
    except Exception as e:
        logging.critical(f"CRITICAL (project initialization): {e}")
        exit(1)

    async def _lake_init():
        """Fetch Mathlib cache and update dependencies (runs in background thread)."""
        await asyncio.to_thread(_run, ["lake", "exe", "cache", "get"], project_path)
        await asyncio.to_thread(_run, ["lake", "update"], project_path)

    _lake_init_task = asyncio.create_task(_lake_init())
    await asyncio.sleep(0)  # yield so the lake init thread starts before synchronous setup
    logging.info("lake cache + update running in background...")

    # Detect the Lean project's default branch once; thread through worktree audit.
    _main_branch = _detect_main_branch(project_path)
    logging.info(f"Detected Lean project main branch: {_main_branch}")

    def _read_report_md() -> str:
        """Read REPORT.md from CWD; if missing, recover from <project_path> (critic may have misplaced it)."""
        cwd_report = Path("REPORT.md")
        if cwd_report.exists():
            return cwd_report.read_text()
        misplaced = project_path / "REPORT.md"
        if misplaced.exists():
            logging.warning(
                f"REPORT.md found at {misplaced} (Lean project) instead of unity run dir — "
                f"moving to {cwd_report.resolve()}. Critic wrote to the wrong directory."
            )
            shutil.move(str(misplaced), str(cwd_report))
            return cwd_report.read_text()
        raise FileNotFoundError("REPORT.md")

    # lean-lsp-mcp launch is deferred until AFTER `await _lake_init_task` so the
    # lakefile / .lake/packages/ manifest is stable before `lake serve` reads it.
    # See launch block further below.
    _lean_lsp_proc = None  # set after lake init completes
    _lean_lsp_stderr_path = Path.cwd() / "lean-lsp.stderr.log"
    _lean_lsp_stderr_file = None  # set when lean-lsp-mcp is (re)spawned

    def _assert_lsp_alive(phase: str) -> None:
        """Fail loud if the LSP subprocess died since last check."""
        if _lean_lsp_proc is None:
            return
        if _lean_lsp_proc.poll() is not None:
            tail = ""
            try:
                tail = _lean_lsp_stderr_path.read_text()[-2000:]
            except Exception:
                pass
            logging.critical(
                f"CRITICAL: lean-lsp-mcp died before phase '{phase}' "
                f"(exit={_lean_lsp_proc.returncode}). stderr tail:\n{tail}"
            )
            exit(1)

    # Configure MCP servers for all agents
    LEAN_MCP_SERVER = {
        "lean-lsp": {
            "type": "http",
            "url": f"http://127.0.0.1:{lean_lsp_port}/mcp/",
        },
        "unity-forum": {
            "command": sys.executable,
            "args": ["-m", "unity_agent.forum_mcp", "--forum-dir", str(Path.cwd() / "forum")],
        },
    }

    # Watchdog state: consecutive SDK-idle stalls since the last successful query
    # turn. Reset on any clean StopAsyncIteration. Used to escalate from
    # "kill claude CLI + retry" → "restart lean-lsp-mcp" → "drop lean-lsp from
    # MCP server list and run LSP-less for remaining retries".
    _stall_count = [0]

    def _restart_lean_lsp_mcp() -> None:
        """Terminate and respawn the lean-lsp-mcp subprocess and wait for the port."""
        nonlocal _lean_lsp_proc, _lean_lsp_stderr_file
        import socket
        logging.warning("Restarting lean-lsp-mcp subprocess after stall...")
        if _lean_lsp_proc is not None:
            try:
                _lean_lsp_proc.terminate()
                _lean_lsp_proc.wait(timeout=5)
            except Exception as term_err:
                logging.warning(
                    f"_restart_lean_lsp_mcp: terminate failed ({term_err}); attempting SIGKILL"
                )
                try:
                    _lean_lsp_proc.kill()
                    _lean_lsp_proc.wait(timeout=5)
                except Exception as kill_err:
                    logging.error(
                        f"_restart_lean_lsp_mcp: kill also failed ({kill_err}); "
                        f"zombie lean-lsp-mcp may hold port {lean_lsp_port}"
                    )
        _lean_lsp_stderr_file = open(_lean_lsp_stderr_path, "a")
        _lean_lsp_proc = subprocess.Popen(
            ["uvx", "lean-lsp-mcp",
             "--transport", "streamable-http",
             "--host", "127.0.0.1",
             "--port", str(lean_lsp_port),
             "--lean-project-path", str(project_path)],
            cwd=str(project_path),
            stdout=subprocess.DEVNULL,
            stderr=_lean_lsp_stderr_file,
        )
        for _ in range(60):
            try:
                with socket.create_connection(("127.0.0.1", lean_lsp_port), timeout=0.5):
                    logging.info("lean-lsp-mcp restarted and listening.")
                    return
            except OSError:
                time.sleep(0.5)
        logging.error(f"lean-lsp-mcp restart failed to bind 127.0.0.1:{lean_lsp_port}")

    async def _query_with_idle_timeout(*, prompt, options):
        """Wrap claude_agent_sdk.query() with a per-message idle timeout.

        Yields messages one at a time. If no message arrives within
        SDK_MESSAGE_IDLE_TIMEOUT seconds, raises asyncio.TimeoutError so the
        existing phase try/except routes to _invoke_resolver. Before re-raising:
          - increments _stall_count
          - on stall #1..MAX_LSP_RESTARTS_BEFORE_DEGRADE: restarts lean-lsp-mcp
          - on stall > MAX_LSP_RESTARTS_BEFORE_DEGRADE: clears LEAN_MCP_SERVER's
            "lean-lsp" entry so subsequent ClaudeAgentOptions(mcp_servers=...)
            constructions omit the lean LSP MCP for the rest of the run.
        """
        it = query(prompt=prompt, options=options).__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(it.__anext__(), timeout=sdk_idle_timeout)
            except StopAsyncIteration:
                _stall_count[0] = 0
                return
            except asyncio.TimeoutError:
                _stall_count[0] += 1
                logging.warning(
                    f"SDK idle timeout ({sdk_idle_timeout:.0f}s) — stall #{_stall_count[0]}. "
                    "Forcing phase failure so _invoke_resolver can retry."
                )
                if _stall_count[0] <= max_lsp_restarts:
                    try:
                        _restart_lean_lsp_mcp()
                    except Exception as restart_err:
                        logging.error(f"LSP MCP restart raised: {restart_err}")
                else:
                    if "lean-lsp" in LEAN_MCP_SERVER:
                        logging.warning(
                            f"Stall count {_stall_count[0]} exceeded "
                            f"MAX_LSP_RESTARTS_BEFORE_DEGRADE={max_lsp_restarts}. "
                            "Degrading: removing lean-lsp from MCP server list for the rest of this run."
                        )
                        LEAN_MCP_SERVER.pop("lean-lsp", None)
                raise
            yield msg

    # ICRL hook: reward agents for forum participation and surface vote feedback
    _balances_path = Path.cwd() / "forum" / "balances.json"

    async def _forum_reward_hook(hook_input: dict, _tool_use_id: str | None, context: object) -> dict:
        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input", {})
        if tool_name == "forum_post":
            actor = tool_input.get("author", "unknown")
            action = "forum_post +0.5"
        elif tool_name == "forum_vote":
            actor = tool_input.get("voter", "unknown")
            action = "forum_vote +0.5"
        else:
            return {"continue_": True}
        try:
            balances = json.loads(_balances_path.read_text())
            balance = balances.get(actor, {}).get("balance", 0.0)
        except Exception:
            balance = 0.0
        return {
            "continue_": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"[ICRL] {action} — {actor} balance: {balance:.1f}",
            },
        }

    import re as _re

    _KILL_PATTERN = _re.compile(
        r"\b(pkill|killall|kill\s+-[0-9]+|kill\s+-SIG\w+)\b.*\b(claude|unity.agent|unity_agent)\b"
        r"|\b(pkill|killall)\b.*\b(claude|unity.agent|unity_agent)\b",
        _re.IGNORECASE,
    )

    async def _self_kill_guard_hook(hook_input: dict, _tool_use_id: str | None, _context: object) -> dict:
        command = hook_input.get("tool_input", {}).get("command", "")
        if _KILL_PATTERN.search(command):
            return {
                "continue_": False,
                "stopReason": (
                    "[BLOCKED] Agents may not kill the Unity pipeline process. "
                    "To stop a background agent, post to the forum instead."
                ),
            }
        return {"continue_": True}

    FORUM_HOOKS = {
        "PostToolUse": [
            HookMatcher(matcher="forum_post|forum_vote", hooks=[_forum_reward_hook])
        ],
    }

    # Start forum web UI
    forum_dir = Path.cwd() / "forum"
    forum_dir.mkdir(exist_ok=True)
    _forum_web = subprocess.Popen(
        [sys.executable, "-m", "unity_agent.forum_web",
         "--forum-dir", str(forum_dir),
         "--root-dir", str(Path.cwd()),
         "--port", str(forum_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(_forum_web.terminate)
    logging.info(f"Forum web UI: http://localhost:{forum_port}")

    # Library initialization and context loading
    _init_library()
    library_context = _load_library_context()
    global LIBRARY_SUBAGENTS
    LIBRARY_SUBAGENTS = _load_library_subagents()
    if library_context:
        logging.info("Library context loaded.")
    if LIBRARY_SUBAGENTS:
        logging.info(f"Loaded {len(LIBRARY_SUBAGENTS)} library subagent(s): {', '.join(LIBRARY_SUBAGENTS)}")

    # Register recursive-unity subagent when depth allows further recursion
    if depth > 0:
        child_depth = depth - 1
        with open(_SUBAGENTS_DIR / "RECURSIVE/UNITY.md") as f:
            recursive_prompt = f.read().format(depth=depth, child_depth=child_depth)
        LIBRARY_SUBAGENTS["recursive-unity"] = AgentDefinition(
            description=f"Spawns a child unity pipeline run for a self-contained subtask too large or complex for a single-context pass. Child runs at --depth {child_depth}.",
            prompt=recursive_prompt,
            tools=["Bash", "Read", "Glob", "Grep", "Write"],
        )
        logging.info(f"Recursive unity subagent registered (child depth: {child_depth})")

    def with_library(prompt: str) -> str:
        """Append library context and tool-naming reminder to a prompt."""
        tool_note = (
            "\n\n---\n\n"
            "**Tool naming:** MCP forum tools are named with a HYPHEN: "
            "`mcp__unity-forum__forum_post`, `mcp__unity-forum__forum_create_thread`, etc. "
            "Not `mcp__unity_forum__*` (underscore) and not bare `forum_post` via Skill. "
            "Any other spelling will silently no-op."
        )
        out = prompt + tool_note
        if library_context:
            out = out + "\n\n---\n\n" + library_context
        return out

    # Resolver infrastructure
    _retries: dict[str, int] = {}

    async def _invoke_resolver(phase_name: str, error: Exception, ctx: dict | None = None) -> None:
        """Classify error and either sleep (rate limit) or spawn resolver agent, then return for retry."""
        max_retries = parse_int(os.getenv("RESOLVER_MAX_RETRIES"))
        _retries[phase_name] = _retries.get(phase_name, 0) + 1
        if max_retries is not None and _retries[phase_name] > max_retries:
            logging.critical(
                f"CRITICAL: resolver retry budget exhausted for phase '{phase_name}' "
                f"after {max_retries} attempt(s)."
            )
            exit(1)

        err_str = str(error)
        logging.warning(
            f"Resolver invoked for phase '{phase_name}' "
            f"(attempt {_retries[phase_name]}): {err_str[:300]}"
        )

        if _RATE_LIMIT_PATTERN.search(err_str):
            wait = 60
            m = re.search(r"retry.after\s+(\d+)", err_str, re.IGNORECASE)
            if not m:
                m = re.search(r"reset.in\s+(\d+)", err_str, re.IGNORECASE)
            if m:
                wait = int(m.group(1))
            logging.warning(f"Rate limit detected — sleeping {wait}s before retry.")
            await asyncio.sleep(wait)
            return

        # Non-rate-limit: spawn resolver agent
        last_checkpoint = "unknown"
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--grep=PHASE:.*status=complete", "-1"],
                capture_output=True, text=True,
            )
            if result.stdout.strip():
                last_checkpoint = result.stdout.strip().split()[0]
        except Exception:
            pass

        chunk_summary = "(dag.json not found or unreadable)"
        try:
            dag = json.loads(Path("dag.json").read_text())
            chunks = dag.get("chunks", [])
            if chunks:
                chunk_summary = json.dumps(
                    [{"id": c["id"], "status": c.get("status", "unknown")} for c in chunks],
                    indent=2,
                )
        except Exception:
            pass

        with open(_PROMPTS_DIR / "RESOLVER.md") as f:
            resolver_prompt = f.read()

        resolver_input = (
            f"## Error Report\n\n"
            f"**Phase:** {phase_name}\n"
            f"**Error:** {err_str}\n"
            f"**Last clean checkpoint:** {last_checkpoint}\n\n"
            f"## Chunk Statuses\n\n```json\n{chunk_summary}\n```\n"
        )
        if ctx:
            resolver_input += (
                f"\n## Additional Context\n\n"
                f"```json\n{json.dumps(ctx, indent=2, default=str)}\n```\n"
            )

        async for message in _query_with_idle_timeout(
            prompt=resolver_input,
            options=ClaudeAgentOptions(
                tools=_ALL_TOOLS,
                allowed_tools=_ALL_TOOLS,
                system_prompt=resolver_prompt,
                mcp_servers=LEAN_MCP_SERVER,
                hooks=FORUM_HOOKS,
                permission_mode="bypassPermissions",
                model="sonnet",
                fallback_model="haiku",
                env=_primary_env,
            ),
        ):
            _log_agent_message(message)

        logging.info(f"Resolver completed for phase '{phase_name}' — retrying.")

    async def _run_escalation_phase(iteration: int, source_label: str | None) -> None:
        """Escalation phase: re-run formalization on the secondary provider for stagnant sorry-carrying chunks.

        Soft give-up when cumulative secondary spend exceeds SECONDARY_BUDGET. (There is no
        primary/secondary bandit selection — all candidates always run on the secondary tier.
        See PROPOSED_FIXES.md S0.3(b) for a real bandit if/when that's implemented.)
        """
        state_path = Path.cwd() / "escalation_state.json"
        state = _load_escalation_state(state_path)

        current_sigs = _chunk_body_signatures(Path.cwd(), project_path)
        if not current_sigs:
            try:
                has_any_sorry = any(
                    re.search(r"\bsorry\b", _strip_lean_comments(p.read_text()))
                    for p in project_path.rglob("*.lean")
                )
            except Exception:
                has_any_sorry = False
            if has_any_sorry:
                logging.warning(
                    "[escalation] no chunk signatures resolved (lean_declaration/dag.json both empty) "
                    "but project tree contains `sorry` — stagnation tracker is blind, escalation will not fire"
                )
        _update_stagnation(state, current_sigs)

        candidates = _stagnant_chunks(state, threshold=2)
        if not candidates:
            _save_escalation_state(state_path, state)
            return

        logging.info(f"[escalation] iteration={iteration} stagnant chunks: {candidates}")

        if secondary_budget is not None and float(state.get("secondary_spend", 0.0)) >= float(secondary_budget):
            logging.warning(
                f"[escalation] secondary budget exhausted "
                f"(${state['secondary_spend']:.4f} / ${secondary_budget:.4f}) — "
                f"{len(candidates)} chunk(s) unresolved: {candidates}"
            )
            _save_escalation_state(state_path, state)
            return

        logging.info("[escalation] running on secondary provider")

        _console.rule("[bold magenta]Escalation Phase[/bold magenta]")
        _assert_lsp_alive("escalation")

        worktree_assignments: dict[str, str] = {}
        run_cost = 0.0
        t_start = time.monotonic()
        try:
            with open(ACTIVE_PROMPTS_DIR / "FORMALIZATION/ESCALATION.md", "r") as f:
                FORMALIZATION_PROMPT = with_library(f.read())
            with open(_SUBAGENTS_DIR / "FORMALIZATION/DECLARATIONFORMALIZER/T.md", "r") as f:
                DECLARATIONFORMALIZER_SUBAGENT = f.read()
            with open(ACTIVE_SUBAGENTS_DIR / "FORMALIZATION/PROOFFORMALIZER/T.md", "r") as f:
                PROOFFORMALIZER_SUBAGENT = f.read()

            for cid in candidates:
                wt = _create_worktree(cid, project_path)
                _symlink_lake_cache(wt, project_path)
                worktree_assignments[cid] = str(wt)
            _write_worktrees_manifest(worktree_assignments)
            logging.info(f"[escalation] worktrees.json written with {len(worktree_assignments)} assignment(s)")

            unity_run_dir = Path.cwd()
            source_line = f"SOURCE_PATH: {source_label}\n" if source_label else ""
            escalation_prompt = (
                f"Escalation pass (iteration {iteration}, tier {tier}).\n\n"
                f"UNITY_RUN_DIR: {unity_run_dir}\n"
                f"PROJECT_PATH: {project_path}\n"
                f"{source_line}"
                f"WORKTREES_MANIFEST: {unity_run_dir}/worktrees.json\n"
                f"CANDIDATE_CHUNKS: {candidates}\n\n"
                f"Resolve the sorries in each candidate chunk using the source material "
                f"(read from SOURCE_PATH if provided) and the semiformal translation at "
                f"{unity_run_dir}/semiformal/chunks/<id>.json. Each candidate has a pre-created "
                f"worktree — read WORKTREES_MANIFEST for its worktree_path and branch, cd to "
                f"the worktree, produce the proof, commit inside the worktree, then merge "
                f"--squash into {_main_branch} from PROJECT_PATH with commit message "
                f"'UNITY: merge chunk <id>' (audit greps for this exact prefix — do not vary it)."
            )

            async for message in _query_with_idle_timeout(
                prompt=escalation_prompt,
                options=ClaudeAgentOptions(
                    tools=_ALL_TOOLS,
                    allowed_tools=_ALL_TOOLS,
                    agents={
                        "declaration-formalizer": AgentDefinition(
                            description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                            prompt=DECLARATIONFORMALIZER_SUBAGENT,
                            tools=_ALL_TOOLS,
                        ),
                        "proof-formalizer": AgentDefinition(
                            description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                            prompt=PROOFFORMALIZER_SUBAGENT,
                            tools=_ALL_TOOLS,
                        ),
                        **LIBRARY_SUBAGENTS,
                    },
                    system_prompt=FORMALIZATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    hooks=FORUM_HOOKS,
                    permission_mode=PERMISSIONS,
                    max_budget_usd=formalization_budget,
                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env=_secondary_env,
                ),
            ):
                _log_agent_message(message)
                if isinstance(message, ResultMessage) and getattr(message, "total_cost_usd", None) is not None:
                    run_cost = float(message.total_cost_usd)

            _audit_worktree_commits(worktree_assignments, project_path, _main_branch)
        except Exception as e:
            logging.error(f"ERROR (escalation phase): {e}")
        finally:
            for cid, wt in list(worktree_assignments.items()):
                try:
                    _cleanup_worktree(Path(wt), project_path, cid)
                except Exception as cleanup_err:
                    logging.warning(f"Cleanup failed for {cid} during escalation: {cleanup_err}")
            _delete_worktrees_manifest()

        t_sec = time.monotonic() - t_start
        state["secondary_spend"] = float(state.get("secondary_spend", 0.0)) + run_cost

        for cid in candidates:
            entry = state["chunks"].setdefault(cid, {"prev_sig": None, "stagnation": 0, "last_escalation": None})
            entry["last_escalation"] = {
                "iteration": iteration,
                "t_sec": t_sec,
                "cost": run_cost,
            }
            entry["stagnation"] = 0

        _save_escalation_state(state_path, state)
        _append_escalated_log(
            Path.cwd(), iteration, candidates, run_cost, t_sec,
            float(state.get("secondary_spend", 0.0)),
        )
        _commit_phase("escalation", {"iteration": iteration, "cost": f"{run_cost:.4f}"})

    # Ensure lake cache + update finished before any agent phase starts
    try:
        await _lake_init_task
        logging.info("lake cache + update completed.")
    except Exception as e:
        logging.critical(f"CRITICAL (lake init): {e}")
        exit(1)

    # Launch lean-lsp-mcp now that the lakefile / .lake/packages/ manifest is stable.
    # Long-lived streamable-http server; every phase's query() attaches to this
    # instance instead of re-spawning uvx lean-lsp-mcp. stderr is captured to a
    # log file so startup / mid-run crashes are diagnosable (see _assert_lsp_alive).
    import socket
    _lean_lsp_stderr_file = open(_lean_lsp_stderr_path, "w")
    _lean_lsp_proc = subprocess.Popen(
        ["uvx", "lean-lsp-mcp",
         "--transport", "streamable-http",
         "--host", "127.0.0.1",
         "--port", str(lean_lsp_port),
         "--lean-project-path", str(project_path)],
        cwd=str(project_path),
        stdout=subprocess.DEVNULL,
        stderr=_lean_lsp_stderr_file,
    )
    atexit.register(_lean_lsp_proc.terminate)

    # Wait for the port to accept connections (up to 60s).
    _lsp_ready = False
    for _ in range(120):
        if _lean_lsp_proc.poll() is not None:
            try:
                tail = _lean_lsp_stderr_path.read_text()[-2000:]
            except Exception:
                tail = ""
            logging.critical(
                f"CRITICAL: lean-lsp-mcp exited during startup "
                f"(exit={_lean_lsp_proc.returncode}). stderr tail:\n{tail}"
            )
            exit(1)
        try:
            with socket.create_connection(("127.0.0.1", lean_lsp_port), timeout=0.5):
                _lsp_ready = True
                break
        except OSError:
            await asyncio.sleep(0.5)
    if not _lsp_ready:
        logging.critical(f"CRITICAL: lean-lsp-mcp failed to bind 127.0.0.1:{lean_lsp_port}")
        exit(1)
    logging.info(f"lean-lsp-mcp listening on http://127.0.0.1:{lean_lsp_port}/mcp/")

    _lsp_warmup_task = asyncio.create_task(_warm_lean_lsp(project_path))
    await asyncio.sleep(0)  # yield so the warmup thread starts immediately
    logging.info("Lean LSP warming up in background...")

    # Await LSP warmup before any agent phase touches the LSP
    try:
        await _lsp_warmup_task
        logging.info("Lean LSP warmup completed.")
    except Exception as e:
        logging.warning(f"Lean LSP warmup failed (non-fatal): {e}")

    # ── Path 3: prove mode, no source, EXPLORATION=false ─────────────────────
    # Strategy-parallel mode: formalization orchestrator brainstorms strategies,
    # spawns ≤K subagents (one per strategy, each in its own worktree), uses forum
    # for coordination, merges winning proofs into main. Critic checks sorry-free +
    # metaprogramming-free. Loop until COMPLETE or max iterations.
    if prove and source is None and not exploration:

        iteration = 0
        while True:
            # Formalization phase
            _console.rule(f"[bold blue]Strategy Formalization Phase[/bold blue] (iteration {iteration})")
            _assert_lsp_alive("strategy-formalization")
            while True:
                try:
                    with open(ACTIVE_PROMPTS_DIR / "FORMALIZATION/STRATEGY.md", "r") as f:
                        FORMALIZATION_PROMPT = with_library(f.read())
                    with open(ACTIVE_SUBAGENTS_DIR / "EXPLORATION/EXPLORER.md", "r") as f:
                        EXPLORER_SUBAGENT = f.read()

                    formalization_prompt = (
                        f"Iteration {iteration}: fill outstanding `sorry`s in the Lean project at {project_path}. "
                        f"Brainstorm proof strategies, decide how many parallel attempts to spawn, create worktrees, "
                        f"dispatch subagents, coordinate via forum, and merge winning proofs into the main branch. "
                        f"PROJECT_PATH: {project_path}"
                    )
                    if iteration > 0:
                        formalization_prompt += " REPORT.md contains the critic's feedback from the previous iteration; address the unresolved items listed there."

                    async for message in _query_with_idle_timeout(
                        prompt=formalization_prompt,
                        options=ClaudeAgentOptions(
                            tools=_ALL_TOOLS,
                            allowed_tools=_ALL_TOOLS,
                            agents={
                                "explorer": AgentDefinition(
                                    description="Explorer subagent. Searches Mathlib and the web for relevant lemmas, definitions, references.",
                                    prompt=EXPLORER_SUBAGENT,
                                    tools=_ALL_TOOLS,
                                ),
                            },
                            system_prompt=FORMALIZATION_PROMPT,
                            mcp_servers=LEAN_MCP_SERVER,
                            hooks=FORUM_HOOKS,
                            permission_mode=PERMISSIONS,
                            max_budget_usd=formalization_budget,
                            enable_file_checkpointing=True,
                            env=_primary_env,
                        ),
                    ):
                        _log_agent_message(message)

                    logging.info("Strategy formalization phase completed successfully!")
                    _commit_phase("strategy-formalization", {"iteration": iteration})
                    break
                except Exception as e:
                    await _invoke_resolver("strategy-formalization", e)

            # Critic phase: sorry-free + metaprogramming-free check
            _console.rule(f"[bold blue]Strategy Critic Phase[/bold blue] (iteration {iteration})")
            _assert_lsp_alive("strategy-critic")
            while True:
                try:
                    with open(ACTIVE_PROMPTS_DIR / "CRITIC_STRATEGY.md", "r") as f:
                        CRITIC_PROMPT = with_library(f.read())

                    async for message in _query_with_idle_timeout(
                        prompt=(
                            f"Audit the Lean project at {project_path}: confirm zero `sorry` and zero metaprogramming "
                            f"escape hatches in the main branch. Write REPORT.md with status COMPLETE or NEEDS_REVISION. "
                            f"PROJECT_PATH: {project_path}"
                        ),
                        options=ClaudeAgentOptions(
                            tools=_ALL_TOOLS,
                            allowed_tools=_ALL_TOOLS,
                            agents={},
                            system_prompt=CRITIC_PROMPT,
                            mcp_servers=LEAN_MCP_SERVER,
                            hooks=FORUM_HOOKS,
                            permission_mode=PERMISSIONS,
                            max_budget_usd=critic_budget,
                            enable_file_checkpointing=True,
                            env=_primary_env,
                        ),
                    ):
                        _log_agent_message(message)

                    logging.info("Strategy critic phase completed successfully!")
                    _commit_phase("strategy-critic", {"iteration": iteration})
                    break
                except Exception as e:
                    await _invoke_resolver("strategy-critic", e)

            # Loop status
            try:
                report_text = _read_report_md()
                if re.search(r"\*\*Status:\*\*\s+COMPLETE", report_text, re.IGNORECASE):
                    logging.info("Critic declared formalization complete.")
                    break
                elif max_critic_iterations is not None and iteration + 1 >= max_critic_iterations:
                    logging.warning(f"Reached maximum iterations ({max_critic_iterations}) — stopping loop.")
                    break
                else:
                    iteration += 1
                    logging.info(f"Critic requested revision — starting iteration {iteration + 1}...")
            except FileNotFoundError:
                logging.warning("No REPORT.md found after critic phase — stopping loop.")
                break

        # Cleanup worktrees the orchestrator created
        wt_root = project_path / ".worktrees"
        if wt_root.exists():
            for wt in sorted(wt_root.iterdir()):
                if not wt.is_dir():
                    continue
                try:
                    _cleanup_worktree(wt, project_path, wt.name)
                except Exception as cleanup_err:
                    logging.warning(f"Cleanup failed for {wt.name}: {cleanup_err}")
        _delete_worktrees_manifest()

        logging.info("Unity has completed!")
        return 0

    # ── Path 2: prove mode, no source ─────────────────────────────────────────
    # Flow: exploration → generation → semiformalization (TT) → critic loop (formalization T → critic → retro → escalation)
    if prove and source is None:

        # Exploration phase
        _console.rule("[bold blue]Exploration Phase[/bold blue]")
        _assert_lsp_alive("exploration")
        while True:
            try:
                with open(ACTIVE_PROMPTS_DIR / "EXPLORATION.md", "r") as f:
                    EXPLORATION_PROMPT = with_library(f.read())
                with open(ACTIVE_SUBAGENTS_DIR / "EXPLORATION/EXPLORER.md", "r") as f:
                    EXPLORER_SUBAGENT = f.read()

                async for message in _query_with_idle_timeout(
                    prompt=f"Survey the Lean project at {project_path} for declarations needing proofs, then gather mathematical content for each.",
                    options=ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "explorer": AgentDefinition(
                                description="Explorer subagent. Capable of searching the web and gathering mathematical content for a specific Lean declaration needing a proof.",
                                prompt=EXPLORER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=EXPLORATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=exploration_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,

                    ),
                ):
                    _log_agent_message(message)

                logging.info("Exploration phase completed successfully!")
                _commit_phase("exploration")
                break
            except Exception as e:
                await _invoke_resolver("exploration", e)

        # Generation + Validation loop
        validation_iteration = 0
        while True:
            # Generation phase
            _console.rule("[bold blue]Generation Phase[/bold blue]")
            _assert_lsp_alive("generation")
            while True:
                try:
                    with open(ACTIVE_PROMPTS_DIR / "GENERATION.md", "r") as f:
                        GENERATION_PROMPT = with_library(f.read())
                    with open(_SUBAGENTS_DIR / "GENERATION/GENERATOR.md", "r") as f:
                        GENERATOR_SUBAGENT = f.read()

                    generation_prompt = "Generate the specification language for the gathered mathematical content in `gathered/`."
                    if validation_iteration > 0:
                        generation_prompt += " VALIDATION_REPORT.md contains feedback from the previous validation attempt — use it to refine the specification."

                    _gen_opts = ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "generator": AgentDefinition(
                                description="Generator subagent. Capable of assisting in the design of a semiformal specification language for a given source.",
                                prompt=GENERATOR_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=GENERATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=generation_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,
                    )
                    async for message in _query_with_idle_timeout(prompt=generation_prompt, options=_gen_opts):
                        _log_agent_message(message)

                    logging.info("Generation phase completed successfully!")
                    _chunks_dir = Path("language") / "chunks"
                    if not _chunks_dir.exists() or not any(_chunks_dir.glob("*.json")):
                        raise FileNotFoundError(
                            "contract breach: language/chunks/ is empty after generation phase ended; "
                            "routing through resolver for fresh-session retry"
                        )
                    _commit_phase("generation")
                    break
                except Exception as e:
                    await _invoke_resolver("generation", e)

            # Validation phase
            _console.rule("[bold blue]Validation Phase[/bold blue]")
            _assert_lsp_alive("validation")
            while True:
                try:
                    with open(PROMPTS_DIR / "VALIDATION.md", "r") as f:
                        VALIDATION_PROMPT = with_library(f.read())

                    _val_opts = ClaudeAgentOptions(
                        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent", "Skill"],
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent", "Skill"],
                        agents={**LIBRARY_SUBAGENTS},
                        system_prompt=VALIDATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=validation_budget,

                        enable_file_checkpointing=True,
                        model="sonnet",
                        fallback_model="haiku",
                        env=_primary_env,
                    )
                    async for message in _query_with_idle_timeout(
                        prompt=f"Validate the IR specification generated for the gathered content in `gathered/`.",
                        options=_val_opts,
                    ):
                        _log_agent_message(message)

                    logging.info("Validation phase completed successfully!")
                    if not Path("VALIDATION_REPORT.md").exists():
                        raise FileNotFoundError(
                            "contract breach: VALIDATION_REPORT.md missing after validation phase ended; "
                            "routing through resolver for fresh-session retry"
                        )
                    _commit_phase("validation")
                    break
                except SystemExit:
                    raise
                except Exception as e:
                    await _invoke_resolver("validation", e)

            # Validation loop status check
            try:
                report_text = Path("VALIDATION_REPORT.md").read_text()
                if not re.search(r"\*\*Status:\*\*\s+INVALID", report_text, re.IGNORECASE):
                    logging.info("Validation loop: report does not contain INVALID marker — proceeding to semiformalization.")
                    break
                elif max_validation_iterations is not None and validation_iteration + 1 >= max_validation_iterations:
                    logging.warning(f"IR validation failed after {max_validation_iterations} iteration(s) — proceeding with semiformalization anyway. See VALIDATION_REPORT.md for details.")
                    break
                else:
                    validation_iteration += 1
                    logging.info(f"Validator rejected specification — rerunning generator with feedback (iteration {validation_iteration + 1})...")
            except FileNotFoundError:
                logging.warning("No VALIDATION_REPORT.md found — proceeding anyway.")
                break

        # Build DAG from chunk JSON files before semiformalization
        try:
            _toposort_chunks(Path("language"))
        except Exception as e:
            logging.critical(f"CRITICAL (toposort): {e}")
            exit(1)

        # Semiformalization phase (always TT: autofix + context, required for Path 2)
        _console.rule("[bold blue]Semiformalization Phase[/bold blue]")
        _assert_lsp_alive("semiformalization")
        while True:
            try:
                with open(ACTIVE_PROMPTS_DIR / "SEMIFORMALIZATION/TT.md", "r") as f:
                    SEMIFORMALIZATION_PROMPT = with_library(f.read())
                with open(ACTIVE_SUBAGENTS_DIR / "SEMIFORMALIZATION/TT.md", "r") as f:
                    SEMIFORMALIZER_SUBAGENT = f.read()

                async for message in _query_with_idle_timeout(
                    prompt=f"Semiformalize the gathered content in `gathered/` using the specification language in `language/`. The Lean project is {project_path}.",
                    options=ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "semiformalizer": AgentDefinition(
                                description="Semiformalizer subagent. Capable of producing faithful semiformal translations of gathered mathematical content into the IR specification language located in `language/`.",
                                prompt=SEMIFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=SEMIFORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=semiformalization_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,

                    ),
                ):
                    _log_agent_message(message)

                logging.info("Semiformalization phase completed successfully!")
                try:
                    _assert_semiformal_field_propagation(Path.cwd())
                except Exception as _drift_err:
                    logging.error(f"ERROR (semiformal field drift check): {_drift_err}")
                _commit_phase("semiformalization")
                break
            except Exception as e:
                await _invoke_resolver("semiformalization", e)

        iteration = 0
        previous_sorry_chunks: frozenset[str] | None = None
        while True:

            # Formalization phase (always T variant: existing project always present)
            _console.rule("[bold blue]Formalization Phase[/bold blue]")
            _assert_lsp_alive("formalization")
            worktree_assignments: dict[str, str] = {}
            while True:
                for cid, wt in list(worktree_assignments.items()):
                    try:
                        _cleanup_worktree(Path(wt), project_path, cid)
                    except Exception as cleanup_err:
                        logging.warning(f"Pre-loop cleanup failed for {cid}: {cleanup_err}")
                worktree_assignments = {}
                try:
                    with open(ACTIVE_PROMPTS_DIR / "FORMALIZATION/T.md", "r") as f:
                        FORMALIZATION_PROMPT = with_library(f.read())
                    with open(_SUBAGENTS_DIR / "FORMALIZATION/DECLARATIONFORMALIZER/T.md", "r") as f:
                        DECLARATIONFORMALIZER_SUBAGENT = f.read()
                    with open(ACTIVE_SUBAGENTS_DIR / "FORMALIZATION/PROOFFORMALIZER/T.md", "r") as f:
                        PROOFFORMALIZER_SUBAGENT = f.read()

                    dag_data = json.loads(Path("dag.json").read_text()) if Path("dag.json").exists() else {"layers": [], "chunks": []}
                    dag_layers = dag_data.get("layers", [])
                    _total_chunks = sum(len(layer) for layer in dag_layers)
                    logging.info(
                        f"[prove-formalization] iteration={iteration}: creating worktrees for "
                        f"{_total_chunks} chunk(s) across {len(dag_layers)} layer(s) under {project_path}/.worktrees/"
                    )
                    for layer_idx, layer in enumerate(dag_layers):
                        for cid in layer:
                            wt = _create_worktree(cid, project_path)
                            _symlink_lake_cache(wt, project_path)
                            worktree_assignments[cid] = str(wt)
                            logging.info(f"[prove-formalization] layer {layer_idx}: worktree ready for chunk '{cid}' at {wt}")
                    _write_worktrees_manifest(worktree_assignments)
                    logging.info(f"[prove-formalization] worktrees.json written with {len(worktree_assignments)} assignment(s)")

                    _formalization_agents = {
                        "declaration-formalizer": AgentDefinition(
                            description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                            prompt=DECLARATIONFORMALIZER_SUBAGENT,
                            tools=_ALL_TOOLS
                        ),
                        "proof-formalizer": AgentDefinition(
                            description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                            prompt=PROOFFORMALIZER_SUBAGENT,
                            tools=_ALL_TOOLS
                        ),
                        **LIBRARY_SUBAGENTS
                    }
                    _formalization_kwargs = dict(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents=_formalization_agents,
                        system_prompt=FORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=formalization_budget,
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,
                    )

                    logging.info("[prove-formalization] invoking orchestrator query — agent will spawn per-chunk subagents, merge, and build")
                    async for message in _query_with_idle_timeout(
                        prompt=f"Formalize the declarations in {project_path}. Worktree assignments are in worktrees.json at the repository root.",
                        options=ClaudeAgentOptions(**_formalization_kwargs),
                    ):
                        _log_agent_message(message)
                    logging.info("[prove-formalization] orchestrator query returned — running post-run audit")

                    _audit_worktree_commits(worktree_assignments, project_path, _main_branch)

                    logging.info(f"[prove-formalization] cleaning up {len(worktree_assignments)} worktree(s)")
                    for cid, wt in worktree_assignments.items():
                        _cleanup_worktree(Path(wt), project_path, cid)
                    worktree_assignments = {}
                    _delete_worktrees_manifest()

                    logging.info("Formalization phase completed successfully!")
                    _commit_phase("formalization", {"iteration": iteration})
                    break
                except Exception as e:
                    for cid, wt in list(worktree_assignments.items()):
                        try:
                            _cleanup_worktree(Path(wt), project_path, cid)
                        except Exception as cleanup_err:
                            logging.warning(f"Cleanup failed for {cid} during error recovery: {cleanup_err}")
                    worktree_assignments = {}
                    _delete_worktrees_manifest()
                    await _invoke_resolver("formalization", e)

            # Surface illegitimate sorries (incl. helper-lemma cascades) so the critic can react this iteration
            try:
                _audit_illegitimate_sorries(Path.cwd(), project_path)
            except Exception as e:
                logging.error(f"ERROR (illegitimate-sorry audit): {e}")

            # Critic phase (always T variant)
            _console.rule("[bold blue]Critic Phase[/bold blue]")
            _assert_lsp_alive("critic")
            while True:
                try:
                    with open(ACTIVE_PROMPTS_DIR / "CRITIC.md", "r") as f:
                        CRITIC_PROMPT = with_library(f.read())
                    with open(_SUBAGENTS_DIR / "CRITIC/DECLARATIONFORMALIZER/T.md", "r") as f:
                        DECLARATIONFORMALIZER_SUBAGENT = f.read()
                    with open(_SUBAGENTS_DIR / "CRITIC/PROOFFORMALIZER/T.md", "r") as f:
                        PROOFFORMALIZER_SUBAGENT = f.read()

                    _crit_opts = ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=CRITIC_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=critic_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,
                    )
                    async for message in _query_with_idle_timeout(
                        prompt=f"Critique {project_path} given semiformalization `semiformal/` and specification language `language/`.",
                        options=_crit_opts,
                    ):
                        _log_agent_message(message)
                    logging.info("Critic phase completed successfully!")
                    if not Path("REPORT.md").exists():
                        raise FileNotFoundError(
                            "contract breach: REPORT.md missing after critic phase ended; "
                            "routing through resolver for fresh-session retry"
                        )
                    _commit_phase("critic", {"iteration": iteration})
                    break
                except Exception as e:
                    await _invoke_resolver("critic", e)

            # Retrospective phase (after critic — integrates critic feedback into library)
            _console.rule("[bold blue]Retrospective Phase[/bold blue]")
            _assert_lsp_alive("retrospective")
            try:
                with open(PROMPTS_DIR / "RETROSPECTIVE.md", "r") as f:
                    RETROSPECTIVE_PROMPT = with_library(f.read().format(
                        SOURCE_PATH="(no source — proof completion mode)",
                        LIBRARY_DIR=str(_get_library_dir()),
                        PROJECT_NOTES_DIR=str(_get_project_notes_dir()),
                        SUBAGENTS_DIR=str(_SUBAGENTS_DIR),
                        DEFAULT_SUBAGENTS_DIR=str(_DEFAULT_SUBAGENTS_DIR),
                    ))
                async for message in _query_with_idle_timeout(
                    prompt=f"Run the retrospective for the unity proof formalization of {project_path}.",
                    options=ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={**LIBRARY_SUBAGENTS},
                        system_prompt=RETROSPECTIVE_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,

                    ),
                ):
                    _log_agent_message(message)
                logging.info("Retrospective phase completed successfully!")
            except Exception as e:
                logging.error(f"ERROR (retrospective phase): {e}")

            library_context = _load_library_context()

            # Stagnation check: compare sorry-carrying chunks across iterations
            try:
                current_sorry_chunks = _collect_chunk_sorry_set(Path.cwd(), project_path)
                if previous_sorry_chunks is not None and current_sorry_chunks and current_sorry_chunks == previous_sorry_chunks:
                    logging.warning(
                        f"Critic iteration {iteration}: sorry set unchanged from previous iteration "
                        f"({len(current_sorry_chunks)} chunk(s)): {sorted(current_sorry_chunks)}"
                    )
                previous_sorry_chunks = current_sorry_chunks
            except Exception as e:
                logging.warning(f"stagnation check failed: {e}")

            # Escalation phase (stagnant chunks only; no-op if none)
            try:
                await _run_escalation_phase(iteration, None)
            except Exception as e:
                logging.error(f"ERROR (escalation phase): {e}")

            # Loop status check
            try:
                report_text = _read_report_md()
                if re.search(r"\*\*Status:\*\*\s+COMPLETE", report_text, re.IGNORECASE):
                    logging.info("Critic declared formalization complete.")
                    break
                elif max_critic_iterations is not None and iteration + 1 >= max_critic_iterations:
                    logging.warning(f"Reached maximum iterations ({max_critic_iterations}) — stopping loop.")
                    break
                else:
                    iteration += 1
                    logging.info(f"Critic requested revision — starting iteration {iteration + 1}...")
            except FileNotFoundError:
                logging.warning("No REPORT.md found after critic phase — stopping loop.")
                break

        _console.rule("[bold blue]Summary[/bold blue]")
        try:

            _console.print(Markdown(_read_report_md()))
        except FileNotFoundError:
            logging.warning("No REPORT.md found — critic may not have completed.")
        except Exception as e:
            logging.error(f"ERROR (summarization): {e}")

        try:
            if not save_spec:
                spec_dir = Path("language")
                if spec_dir.exists():
                    shutil.rmtree(spec_dir)
            if not save_semiformalization:
                semiformal_dir = Path("semiformal")
                if semiformal_dir.exists():
                    shutil.rmtree(semiformal_dir)
        except Exception as e:
            logging.error(f"ERROR (clean up): {e}")

        logging.info("Unity has completed!")
        return 0

    # ── Path 1 / normal mode ──────────────────────────────────────────────────

    # Source scan phase
    if source is not None:
        _console.rule("[bold blue]Source Scan Phase[/bold blue]")
        _assert_lsp_alive("source scan")
        while True:
            try:
                with open(_PROMPTS_DIR / "SOURCE_SCAN.md", "r") as f:
                    SOURCE_SCAN_PROMPT = with_library(f.read())
                with open(_SUBAGENTS_DIR / "SOURCE_SCAN/SCANNER.md", "r") as f:
                    SCANNER_SUBAGENT = f.read()

                scan_prompt = f"Scan {source} for mathematical claims and search Mathlib for each."
                if context:
                    scan_prompt += f" An existing Lean project is present at {project_path} — also inventory its current Mathlib imports."

                async for message in _query_with_idle_timeout(
                    prompt=scan_prompt,
                    options=ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "scanner": AgentDefinition(
                                description="Scanner subagent. Searches Mathlib for declarations relevant to a given mathematical claim.",
                                prompt=SCANNER_SUBAGENT,
                                tools=["Read", "WebSearch", "WebFetch"],
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=SOURCE_SCAN_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=source_scan_budget,

                        enable_file_checkpointing=True,
                        model="sonnet",
                        fallback_model="haiku",
                        env=_primary_env,

                    ),
                ):
                    _log_agent_message(message)

                logging.info("Source scan phase completed successfully!")
                _commit_phase("source-scan")
                break
            except Exception as e:
                await _invoke_resolver("source-scan", e)

    # Generation + Validation loop

    validation_iteration = 0
    while True:

        # Generation phase
        _console.rule("[bold blue]Generation Phase[/bold blue]")
        _assert_lsp_alive("generation")
        while True:
            try:
                # Load generation phase system prompt and generator subagent prompt
                with open(ACTIVE_PROMPTS_DIR / "GENERATION.md", "r") as f:
                    GENERATION_PROMPT = with_library(f.read())
                with open(_SUBAGENTS_DIR / "GENERATION/GENERATOR.md", "r") as f:
                    GENERATOR_SUBAGENT = f.read()

                generation_prompt = f"Generate the specification language for {source}."
                if validation_iteration > 0:
                    generation_prompt += " VALIDATION_REPORT.md contains feedback from the previous validation attempt — use it to refine the specification."

                _gen_opts = ClaudeAgentOptions(
                    tools=_ALL_TOOLS,
                    allowed_tools=_ALL_TOOLS,
                    agents={
                        "generator": AgentDefinition(
                            description="Generator subagent. Capable of assisting in the design of a semiformal specification language for a given source.",
                            prompt=GENERATOR_SUBAGENT,
                            tools=_ALL_TOOLS
                        ),
                        **LIBRARY_SUBAGENTS
                    },
                    system_prompt=GENERATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    hooks=FORUM_HOOKS,
                    permission_mode=PERMISSIONS,
                    max_budget_usd=generation_budget,

                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env=_primary_env,
                )
                async for message in _query_with_idle_timeout(prompt=generation_prompt, options=_gen_opts):
                    _log_agent_message(message)

                logging.info("Generation phase completed successfully!")
                _chunks_dir = Path("language") / "chunks"
                if not _chunks_dir.exists() or not any(_chunks_dir.glob("*.json")):
                    raise FileNotFoundError(
                        "contract breach: language/chunks/ is empty after generation phase ended; "
                        "routing through resolver for fresh-session retry"
                    )
                _commit_phase("generation")
                break
            except Exception as e:
                await _invoke_resolver("generation", e)

        # Validation phase
        _console.rule("[bold blue]Validation Phase[/bold blue]")
        _assert_lsp_alive("validation")
        while True:
            try:
                with open(PROMPTS_DIR / "VALIDATION.md", "r") as f:
                    VALIDATION_PROMPT = with_library(f.read())

                _val_opts = ClaudeAgentOptions(
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent", "Skill"],
                    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent", "Skill"],
                    agents={**LIBRARY_SUBAGENTS},
                    system_prompt=VALIDATION_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    hooks=FORUM_HOOKS,
                    permission_mode=PERMISSIONS,
                    max_budget_usd=validation_budget,

                    enable_file_checkpointing=True,
                    model="sonnet",
                    fallback_model="haiku",
                    env=_primary_env,
                )
                async for message in _query_with_idle_timeout(
                    prompt=f"Validate the IR specification generated for {source}.",
                    options=_val_opts,
                ):
                    _log_agent_message(message)

                logging.info("Validation phase completed successfully!")
                if not Path("VALIDATION_REPORT.md").exists():
                    raise FileNotFoundError(
                        "contract breach: VALIDATION_REPORT.md missing after validation phase ended; "
                        "routing through resolver for fresh-session retry"
                    )
                _commit_phase("validation")
                break
            except SystemExit:
                raise
            except Exception as e:
                await _invoke_resolver("validation", e)

        # Validation loop status check
        try:
            report_text = Path("VALIDATION_REPORT.md").read_text()
            if not re.search(r"\*\*Status:\*\*\s+INVALID", report_text, re.IGNORECASE):
                logging.info("Validation loop: report does not contain INVALID marker — proceeding to semiformalization.")
                break
            elif max_validation_iterations is not None and validation_iteration + 1 >= max_validation_iterations:
                logging.warning(f"IR validation failed after {max_validation_iterations} iteration(s) — proceeding with semiformalization anyway. See VALIDATION_REPORT.md for details.")
                break
            else:
                validation_iteration += 1
                logging.info(f"Validator rejected specification — rerunning generator with feedback (iteration {validation_iteration + 1})...")
        except FileNotFoundError:
            logging.warning("No VALIDATION_REPORT.md found — proceeding anyway.")
            break

    # Build DAG from chunk JSON files before semiformalization
    try:
        _toposort_chunks(Path("language"))
    except Exception as e:
        logging.critical(f"CRITICAL (toposort): {e}")
        exit(1)

    # Semiformalization phase

    _console.rule("[bold blue]Semiformalization Phase[/bold blue]")

    _assert_lsp_alive("semiformalization")
    if not autofix and not context:
        while True:
            try:
                # Load semiformalization phase system prompt and semiformalizer subagent prompt
                with open(ACTIVE_PROMPTS_DIR / "SEMIFORMALIZATION/FF.md", "r") as f:
                    SEMIFORMALIZATION_PROMPT = with_library(f.read())
                with open(ACTIVE_SUBAGENTS_DIR / "SEMIFORMALIZATION/FF.md", "r") as f:
                    SEMIFORMALIZER_SUBAGENT = f.read()

                async for message in _query_with_idle_timeout(
                    prompt=f"Semiformalize {source} as specified by the language.",
                    options=ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "semiformalizer": AgentDefinition(
                                description="Semiformalizer subagent. Capable of producing faithful semiformal translations of a source into the IR specification language located in `language/`.",
                                prompt=SEMIFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=SEMIFORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=semiformalization_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,

                    ),
                ):
                    _log_agent_message(message)

                logging.info("Semiformalization phase completed successfully!")
                try:
                    _assert_semiformal_field_propagation(Path.cwd())
                except Exception as _drift_err:
                    logging.error(f"ERROR (semiformal field drift check): {_drift_err}")
                _commit_phase("semiformalization")
                break
            except Exception as e:
                await _invoke_resolver("semiformalization", e)
    elif autofix and not context:
        while True:
            try:
                # Load semiformalization phase system prompt and semiformalizer subagent prompt
                with open(ACTIVE_PROMPTS_DIR / "SEMIFORMALIZATION/TF.md", "r") as f:
                    SEMIFORMALIZATION_PROMPT = with_library(f.read())
                with open(ACTIVE_SUBAGENTS_DIR / "SEMIFORMALIZATION/TF.md", "r") as f:
                    SEMIFORMALIZER_SUBAGENT = f.read()

                async for message in _query_with_idle_timeout(
                    prompt=f"Semiformalize {source} as specified by the language.",
                    options=ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "semiformalizer": AgentDefinition(
                                description="Semiformalizer subagent. Capable of producing faithful semiformal translations of a source into the IR specification language located in `language/`.",
                                prompt=SEMIFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=SEMIFORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=semiformalization_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,

                    ),
                ):
                    _log_agent_message(message)

                logging.info("Semiformalization phase completed successfully!")
                try:
                    _assert_semiformal_field_propagation(Path.cwd())
                except Exception as _drift_err:
                    logging.error(f"ERROR (semiformal field drift check): {_drift_err}")
                _commit_phase("semiformalization")
                break
            except Exception as e:
                await _invoke_resolver("semiformalization", e)
    elif autofix and context:
        while True:
            try:
                # Load semiformalization phase system prompt and semiformalizer subagent prompt
                with open(ACTIVE_PROMPTS_DIR / "SEMIFORMALIZATION/TT.md", "r") as f:
                    SEMIFORMALIZATION_PROMPT = with_library(f.read())
                with open(ACTIVE_SUBAGENTS_DIR / "SEMIFORMALIZATION/TT.md", "r") as f:
                    SEMIFORMALIZER_SUBAGENT = f.read()

                async for message in _query_with_idle_timeout(
                    prompt=f"Semiformalize {source} as specified by the language. The Lean project is {project_path}.",
                    options=ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "semiformalizer": AgentDefinition(
                                description="Semiformalizer subagent. Capable of producing faithful semiformal translations of a source into the IR specification language located in `language/`.",
                                prompt=SEMIFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=SEMIFORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=semiformalization_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,

                    ),
                ):
                    _log_agent_message(message)

                logging.info("Semiformalization phase completed successfully!")
                try:
                    _assert_semiformal_field_propagation(Path.cwd())
                except Exception as _drift_err:
                    logging.error(f"ERROR (semiformal field drift check): {_drift_err}")
                _commit_phase("semiformalization")
                break
            except Exception as e:
                await _invoke_resolver("semiformalization", e)
    else:
        logging.critical("CRITICAL (semiformalization phase): cannot have context without autofix enabled")
        exit(1)

    iteration = 0
    previous_sorry_chunks: frozenset[str] | None = None
    while True:

        # Exploration phase

        if exploration:
            _console.rule("[bold blue]Exploration Phase[/bold blue]")
            _assert_lsp_alive("exploration")
            if not recurse and not context:
                while True:
                    try:
                        # Load exploration phase system prompt and explorer, semiformalizer, and exploration-generator subagent prompts
                        with open(PROMPTS_DIR / "EXPLORATION/FF.md", "r") as f:
                            EXPLORATION_PROMPT = with_library(f.read())
                        with open(_SUBAGENTS_DIR / "EXPLORATION/EXPLORER/F.md", "r") as f:
                            EXPLORER_SUBAGENT = f.read()
                        with open(_SUBAGENTS_DIR / "EXPLORATION/SEMIFORMALIZER/F.md", "r") as f:
                            SEMIFORMALIZER_SUBAGENT = f.read()
                        with open(_SUBAGENTS_DIR / "EXPLORATION/EXPLORATIONGENERATOR.md", "r") as f:
                            EXPLORATIONGENERATOR_SUBAGENT = f.read()

                        async for message in _query_with_idle_timeout(
                            prompt=f"Explore `semiformal/` given specification language `language/` and source {source}.",
                            options=ClaudeAgentOptions(
                                tools=_ALL_TOOLS,
                                allowed_tools=_ALL_TOOLS,
                                agents={
                                    "explorer": AgentDefinition(
                                        description="Explorer subagent. Capable of searching the web and gathering sources for a specific assumption type.",
                                        prompt=EXPLORER_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    "semiformalizer": AgentDefinition(
                                        description="Semiformalizer subagent. Capable of semiformalizing gathered sources for specific assumption types into the existing semiformal translation.",
                                        prompt=SEMIFORMALIZER_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    "exploration-generator": AgentDefinition(
                                        description="ExplorationGenerator subagent. Capable of extending the IR specification language to accomodate new sources gathered during exploration.",
                                        prompt=EXPLORATIONGENERATOR_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    **LIBRARY_SUBAGENTS
                                },
                                system_prompt=EXPLORATION_PROMPT,
                                mcp_servers=LEAN_MCP_SERVER,
                                hooks=FORUM_HOOKS,
                                permission_mode=PERMISSIONS,
                                max_budget_usd=exploration_budget,

                                enable_file_checkpointing=True,
                                model="opus",
                                fallback_model="sonnet",
                                env=_primary_env,

                            ),
                        ):
                            _log_agent_message(message)

                        logging.info("Exploration phase completed successfully!")
                        _commit_phase("exploration", {"iteration": iteration})
                        break
                    except Exception as e:
                        await _invoke_resolver("exploration", e)
            elif not recurse and context:
                while True:
                    try:
                        # Load exploration phase system prompt and explorer, semiformalizer, and exploration-generator subagent prompts
                        with open(PROMPTS_DIR / "EXPLORATION/FT.md", "r") as f:
                            EXPLORATION_PROMPT = with_library(f.read())
                        with open(_SUBAGENTS_DIR / "EXPLORATION/EXPLORER/T.md", "r") as f:
                            EXPLORER_SUBAGENT = f.read()
                        with open(_SUBAGENTS_DIR / "EXPLORATION/SEMIFORMALIZER/T.md", "r") as f:
                            SEMIFORMALIZER_SUBAGENT = f.read()
                        with open(_SUBAGENTS_DIR / "EXPLORATION/EXPLORATIONGENERATOR.md", "r") as f:
                            EXPLORATIONGENERATOR_SUBAGENT = f.read()

                        async for message in _query_with_idle_timeout(
                            prompt=f"Explore `semiformal/` given specification language `language/` and source {source}. The Lean project is {project_path}.",
                            options=ClaudeAgentOptions(
                                tools=_ALL_TOOLS,
                                allowed_tools=_ALL_TOOLS,
                                agents={
                                    "explorer": AgentDefinition(
                                        description="Explorer subagent. Capable of searching the web and gathering sources for a specific assumption type.",
                                        prompt=EXPLORER_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    "semiformalizer": AgentDefinition(
                                        description="Semiformalizer subagent. Capable of semiformalizing gathered sources for specific assumption types into the existing semiformal translation.",
                                        prompt=SEMIFORMALIZER_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    "exploration-generator": AgentDefinition(
                                        description="ExplorationGenerator subagent. Capable of extending the IR specification language to accomodate new sources gathered during exploration.",
                                        prompt=EXPLORATIONGENERATOR_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    **LIBRARY_SUBAGENTS
                                },
                                system_prompt=EXPLORATION_PROMPT,
                                mcp_servers=LEAN_MCP_SERVER,
                                hooks=FORUM_HOOKS,
                                permission_mode=PERMISSIONS,
                                max_budget_usd=exploration_budget,

                                enable_file_checkpointing=True,
                                model="opus",
                                fallback_model="sonnet",
                                env=_primary_env,

                            ),
                        ):
                            _log_agent_message(message)

                        logging.info("Exploration phase completed successfully!")
                        _commit_phase("exploration", {"iteration": iteration})
                        break
                    except Exception as e:
                        await _invoke_resolver("exploration", e)
            elif recurse and not context:
                while True:
                    try:
                        # Load exploration phase system prompt and explorer, semiformalizer, and exploration-generator subagent prompts
                        with open(PROMPTS_DIR / "EXPLORATION/TF.md", "r") as f:
                            EXPLORATION_PROMPT = with_library(f.read())
                        with open(_SUBAGENTS_DIR / "EXPLORATION/EXPLORER/F.md", "r") as f:
                            EXPLORER_SUBAGENT = f.read()
                        with open(_SUBAGENTS_DIR / "EXPLORATION/SEMIFORMALIZER/F.md", "r") as f:
                            SEMIFORMALIZER_SUBAGENT = f.read()
                        with open(_SUBAGENTS_DIR / "EXPLORATION/EXPLORATIONGENERATOR.md", "r") as f:
                            EXPLORATIONGENERATOR_SUBAGENT = f.read()

                        async for message in _query_with_idle_timeout(
                            prompt=f"Explore `semiformal/` given specification language `language/` and source {source}.",
                            options=ClaudeAgentOptions(
                                tools=_ALL_TOOLS,
                                allowed_tools=_ALL_TOOLS,
                                agents={
                                    "explorer": AgentDefinition(
                                        description="Explorer subagent. Capable of searching the web and gathering sources for a specific assumption type.",
                                        prompt=EXPLORER_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    "semiformalizer": AgentDefinition(
                                        description="Semiformalizer subagent. Capable of semiformalizing gathered sources for specific assumption types into the existing semiformal translation.",
                                        prompt=SEMIFORMALIZER_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    "exploration-generator": AgentDefinition(
                                        description="ExplorationGenerator subagent. Capable of extending the IR specification language to accomodate new sources gathered during exploration.",
                                        prompt=EXPLORATIONGENERATOR_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    **LIBRARY_SUBAGENTS
                                },
                                system_prompt=EXPLORATION_PROMPT,
                                mcp_servers=LEAN_MCP_SERVER,
                                hooks=FORUM_HOOKS,
                                permission_mode=PERMISSIONS,
                                max_budget_usd=exploration_budget,

                                enable_file_checkpointing=True,
                                model="opus",
                                fallback_model="sonnet",
                                env=_primary_env,

                            ),
                        ):
                            _log_agent_message(message)

                        logging.info("Exploration phase completed successfully!")
                        _commit_phase("exploration", {"iteration": iteration})
                        break
                    except Exception as e:
                        await _invoke_resolver("exploration", e)
            elif recurse and context:
                while True:
                    try:
                        # Load exploration phase system prompt and explorer, semiformalizer, and exploration-generator subagent prompts
                        with open(PROMPTS_DIR / "EXPLORATION/TT.md", "r") as f:
                            EXPLORATION_PROMPT = with_library(f.read())
                        with open(_SUBAGENTS_DIR / "EXPLORATION/EXPLORER/T.md", "r") as f:
                            EXPLORER_SUBAGENT = f.read()
                        with open(_SUBAGENTS_DIR / "EXPLORATION/SEMIFORMALIZER/T.md", "r") as f:
                            SEMIFORMALIZER_SUBAGENT = f.read()
                        with open(_SUBAGENTS_DIR / "EXPLORATION/EXPLORATIONGENERATOR.md", "r") as f:
                            EXPLORATIONGENERATOR_SUBAGENT = f.read()

                        async for message in _query_with_idle_timeout(
                            prompt=f"Explore `semiformal/` given specification language `language/` and source {source}. The Lean project is {project_path}.",
                            options=ClaudeAgentOptions(
                                tools=_ALL_TOOLS,
                                allowed_tools=_ALL_TOOLS,
                                agents={
                                    "explorer": AgentDefinition(
                                        description="Explorer subagent. Capable of searching the web and gathering sources for a specific assumption type.",
                                        prompt=EXPLORER_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    "semiformalizer": AgentDefinition(
                                        description="Semiformalizer subagent. Capable of semiformalizing gathered sources for specific assumption types into the existing semiformal translation.",
                                        prompt=SEMIFORMALIZER_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    "exploration-generator": AgentDefinition(
                                        description="ExplorationGenerator subagent. Capable of extending the IR specification language to accomodate new sources gathered during exploration.",
                                        prompt=EXPLORATIONGENERATOR_SUBAGENT,
                                        tools=_ALL_TOOLS
                                    ),
                                    **LIBRARY_SUBAGENTS
                                },
                                system_prompt=EXPLORATION_PROMPT,
                                mcp_servers=LEAN_MCP_SERVER,
                                hooks=FORUM_HOOKS,
                                permission_mode=PERMISSIONS,
                                max_budget_usd=exploration_budget,

                                enable_file_checkpointing=True,
                                model="opus",
                                fallback_model="sonnet",
                                env=_primary_env,

                            ),
                        ):
                            _log_agent_message(message)

                        logging.info("Exploration phase completed successfully!")
                        _commit_phase("exploration", {"iteration": iteration})
                        break
                    except Exception as e:
                        await _invoke_resolver("exploration", e)
            else:
                logging.critical("CRITICAL (exploration phase): reached unreachable code")
                exit(1)
        else:
            logging.info("Exploration phase skipped.")

        # Formalization phase

        _console.rule("[bold blue]Formalization Phase[/bold blue]")


        _assert_lsp_alive("formalization")
        if not context and iteration == 0:
            worktree_assignments: dict[str, str] = {}
            while True:
                for cid, wt in list(worktree_assignments.items()):
                    try:
                        _cleanup_worktree(Path(wt), project_path, cid)
                    except Exception as cleanup_err:
                        logging.warning(f"Pre-loop cleanup failed for {cid}: {cleanup_err}")
                worktree_assignments = {}
                try:
                    # Load formalization phase system prompt and declaration-formalizer and proof-formalizer subagent prompts
                    with open(ACTIVE_PROMPTS_DIR / "FORMALIZATION/F.md", "r") as f:
                        FORMALIZATION_PROMPT = with_library(f.read())
                    with open(_SUBAGENTS_DIR / "FORMALIZATION/DECLARATIONFORMALIZER/F.md", "r") as f:
                        DECLARATIONFORMALIZER_SUBAGENT = f.read()
                    with open(ACTIVE_SUBAGENTS_DIR / "FORMALIZATION/PROOFFORMALIZER/F.md", "r") as f:
                        PROOFFORMALIZER_SUBAGENT = f.read()

                    dag_data = json.loads(Path("dag.json").read_text()) if Path("dag.json").exists() else {"layers": [], "chunks": []}
                    dag_layers = dag_data.get("layers", [])
                    _total_chunks = sum(len(layer) for layer in dag_layers)
                    logging.info(
                        f"[formalization F] iteration={iteration}: creating worktrees for "
                        f"{_total_chunks} chunk(s) across {len(dag_layers)} layer(s) under {project_path}/.worktrees/"
                    )
                    for layer_idx, layer in enumerate(dag_layers):
                        for cid in layer:
                            wt = _create_worktree(cid, project_path)
                            _symlink_lake_cache(wt, project_path)
                            worktree_assignments[cid] = str(wt)
                            logging.info(f"[formalization F] layer {layer_idx}: worktree ready for chunk '{cid}' at {wt}")
                    _write_worktrees_manifest(worktree_assignments)
                    logging.info(f"[formalization F] worktrees.json written with {len(worktree_assignments)} assignment(s)")

                    _formalization_prompt = f"Formalize {source} into {project_path}."
                    if worktree_assignments:
                        _formalization_prompt += " Worktree assignments are in worktrees.json at the repository root."

                    logging.info("[formalization F] invoking orchestrator query — agent will spawn per-chunk subagents, merge, and build")
                    async for message in _query_with_idle_timeout(
                        prompt=_formalization_prompt,
                        options=ClaudeAgentOptions(
                            tools=_ALL_TOOLS,
                            allowed_tools=_ALL_TOOLS,
                            agents={
                                "declaration-formalizer": AgentDefinition(
                                    description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                    prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                    tools=_ALL_TOOLS
                                ),
                                "proof-formalizer": AgentDefinition(
                                    description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                    prompt=PROOFFORMALIZER_SUBAGENT,
                                    tools=_ALL_TOOLS
                                ),
                                **LIBRARY_SUBAGENTS
                            },
                            system_prompt=FORMALIZATION_PROMPT,
                            mcp_servers=LEAN_MCP_SERVER,
                            hooks=FORUM_HOOKS,
                            permission_mode=PERMISSIONS,
                            max_budget_usd=formalization_budget,

                            enable_file_checkpointing=True,
                            model="opus",
                            fallback_model="sonnet",
                            env=_primary_env,

                        ),
                    ):
                        _log_agent_message(message)
                    logging.info("[formalization F] orchestrator query returned — running post-run audit")

                    _audit_worktree_commits(worktree_assignments, project_path, _main_branch)

                    logging.info(f"[formalization F] cleaning up {len(worktree_assignments)} worktree(s)")
                    for cid, wt in worktree_assignments.items():
                        _cleanup_worktree(Path(wt), project_path, cid)
                    worktree_assignments = {}
                    _delete_worktrees_manifest()

                    logging.info("Formalization phase completed successfully!")
                    _commit_phase("formalization", {"iteration": iteration})
                    break
                except Exception as e:
                    for cid, wt in list(worktree_assignments.items()):
                        try:
                            _cleanup_worktree(Path(wt), project_path, cid)
                        except Exception as cleanup_err:
                            logging.warning(f"Cleanup failed for {cid} during error recovery: {cleanup_err}")
                    worktree_assignments = {}
                    _delete_worktrees_manifest()
                    await _invoke_resolver("formalization", e)
        elif context or iteration > 0:
            worktree_assignments: dict[str, str] = {}
            while True:
                for cid, wt in list(worktree_assignments.items()):
                    try:
                        _cleanup_worktree(Path(wt), project_path, cid)
                    except Exception as cleanup_err:
                        logging.warning(f"Pre-loop cleanup failed for {cid}: {cleanup_err}")
                worktree_assignments = {}
                try:
                    # Load formalization phase system prompt and declaration-formalizer and proof-formalizer subagent prompts
                    with open(ACTIVE_PROMPTS_DIR / "FORMALIZATION/T.md", "r") as f:
                        FORMALIZATION_PROMPT = with_library(f.read())
                    with open(_SUBAGENTS_DIR / "FORMALIZATION/DECLARATIONFORMALIZER/T.md", "r") as f:
                        DECLARATIONFORMALIZER_SUBAGENT = f.read()
                    with open(ACTIVE_SUBAGENTS_DIR / "FORMALIZATION/PROOFFORMALIZER/T.md", "r") as f:
                        PROOFFORMALIZER_SUBAGENT = f.read()

                    dag_data = json.loads(Path("dag.json").read_text()) if Path("dag.json").exists() else {"layers": [], "chunks": []}
                    dag_layers = dag_data.get("layers", [])
                    _total_chunks = sum(len(layer) for layer in dag_layers)
                    logging.info(
                        f"[formalization T] iteration={iteration}: creating worktrees for "
                        f"{_total_chunks} chunk(s) across {len(dag_layers)} layer(s) under {project_path}/.worktrees/"
                    )
                    for layer_idx, layer in enumerate(dag_layers):
                        for cid in layer:
                            wt = _create_worktree(cid, project_path)
                            _symlink_lake_cache(wt, project_path)
                            worktree_assignments[cid] = str(wt)
                            logging.info(f"[formalization T] layer {layer_idx}: worktree ready for chunk '{cid}' at {wt}")
                    _write_worktrees_manifest(worktree_assignments)
                    logging.info(f"[formalization T] worktrees.json written with {len(worktree_assignments)} assignment(s)")

                    _formalization_agents = {
                        "declaration-formalizer": AgentDefinition(
                            description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                            prompt=DECLARATIONFORMALIZER_SUBAGENT,
                            tools=_ALL_TOOLS
                        ),
                        "proof-formalizer": AgentDefinition(
                            description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                            prompt=PROOFFORMALIZER_SUBAGENT,
                            tools=_ALL_TOOLS
                        ),
                        **LIBRARY_SUBAGENTS
                    }
                    _formalization_kwargs = dict(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents=_formalization_agents,
                        system_prompt=FORMALIZATION_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=formalization_budget,
                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,
                    )

                    _prompt_prefix = f"Formalize {source} into {project_path}."
                    if worktree_assignments:
                        _prompt_prefix += " Worktree assignments are in worktrees.json at the repository root."
                    logging.info("[formalization T] invoking orchestrator query — agent will spawn per-chunk subagents, merge, and build")
                    async for message in _query_with_idle_timeout(prompt=_prompt_prefix, options=ClaudeAgentOptions(**_formalization_kwargs)):
                        _log_agent_message(message)
                    logging.info("[formalization T] orchestrator query returned — running post-run audit")

                    _audit_worktree_commits(worktree_assignments, project_path, _main_branch)

                    logging.info(f"[formalization T] cleaning up {len(worktree_assignments)} worktree(s)")
                    for cid, wt in worktree_assignments.items():
                        _cleanup_worktree(Path(wt), project_path, cid)
                    worktree_assignments = {}
                    _delete_worktrees_manifest()

                    logging.info("Formalization phase completed successfully!")
                    _commit_phase("formalization", {"iteration": iteration})
                    break
                except Exception as e:
                    for cid, wt in list(worktree_assignments.items()):
                        try:
                            _cleanup_worktree(Path(wt), project_path, cid)
                        except Exception as cleanup_err:
                            logging.warning(f"Cleanup failed for {cid} during error recovery: {cleanup_err}")
                    worktree_assignments = {}
                    _delete_worktrees_manifest()
                    await _invoke_resolver("formalization", e)
        else:
            logging.critical("CRITICAL (formalization phase): reached unreachable code")
            exit(1)

        # Surface illegitimate sorries (incl. helper-lemma cascades) so the critic can react this iteration
        try:
            _audit_illegitimate_sorries(Path.cwd(), project_path)
        except Exception as e:
            logging.error(f"ERROR (illegitimate-sorry audit): {e}")

        # Critic phase

        _console.rule("[bold blue]Critic Phase[/bold blue]")


        _assert_lsp_alive("critic")
        if not context and iteration == 0:
            while True:
                try:
                    # Load critic phase system prompt and declaration-formalizer and proof-formalizer subagent prompts
                    with open(ACTIVE_PROMPTS_DIR / "CRITIC.md", "r") as f:
                        CRITIC_PROMPT = with_library(f.read())
                    with open(_SUBAGENTS_DIR / "CRITIC/DECLARATIONFORMALIZER/F.md", "r") as f:
                        DECLARATIONFORMALIZER_SUBAGENT = f.read()
                    with open(_SUBAGENTS_DIR / "CRITIC/PROOFFORMALIZER/F.md", "r") as f:
                        PROOFFORMALIZER_SUBAGENT = f.read()

                    _crit_opts = ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=CRITIC_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=critic_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,
                    )
                    async for message in _query_with_idle_timeout(
                        prompt=f"Critique {project_path} given source {source}, semiformalization `semiformal/`, and specification language `language/`.",
                        options=_crit_opts,
                    ):
                        _log_agent_message(message)

                    logging.info("Critic phase completed successfully!")
                    if not Path("REPORT.md").exists():
                        raise FileNotFoundError(
                            "contract breach: REPORT.md missing after critic phase ended; "
                            "routing through resolver for fresh-session retry"
                        )
                    _commit_phase("critic", {"iteration": iteration})
                    break
                except Exception as e:
                    await _invoke_resolver("critic", e)
        elif context or iteration > 0:
            while True:
                try:
                    # Load critic phase system prompt and declaration-formalizer and proof-formalizer subagent prompts
                    with open(ACTIVE_PROMPTS_DIR / "CRITIC.md", "r") as f:
                        CRITIC_PROMPT = with_library(f.read())
                    with open(_SUBAGENTS_DIR / "CRITIC/DECLARATIONFORMALIZER/T.md", "r") as f:
                        DECLARATIONFORMALIZER_SUBAGENT = f.read()
                    with open(_SUBAGENTS_DIR / "CRITIC/PROOFFORMALIZER/T.md", "r") as f:
                        PROOFFORMALIZER_SUBAGENT = f.read()

                    _crit_opts = ClaudeAgentOptions(
                        tools=_ALL_TOOLS,
                        allowed_tools=_ALL_TOOLS,
                        agents={
                            "declaration-formalizer": AgentDefinition(
                                description="DeclarationFormalizer subagent. Capable of formalizing a declaration or statement of a specific chunk into Lean4.",
                                prompt=DECLARATIONFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            "proof-formalizer": AgentDefinition(
                                description="ProofFormalizer subagent. Capable of formalizing the proof of a specific chunk into Lean4.",
                                prompt=PROOFFORMALIZER_SUBAGENT,
                                tools=_ALL_TOOLS
                            ),
                            **LIBRARY_SUBAGENTS
                        },
                        system_prompt=CRITIC_PROMPT,
                        mcp_servers=LEAN_MCP_SERVER,
                        hooks=FORUM_HOOKS,
                        permission_mode=PERMISSIONS,
                        max_budget_usd=critic_budget,

                        enable_file_checkpointing=True,
                        model="opus",
                        fallback_model="sonnet",
                        env=_primary_env,
                    )
                    async for message in _query_with_idle_timeout(
                        prompt=f"Critique {project_path} given source {source}, semiformalization `semiformal/`, and specification language `language/`.",
                        options=_crit_opts,
                    ):
                        _log_agent_message(message)

                    logging.info("Critic phase completed successfully!")
                    if not Path("REPORT.md").exists():
                        raise FileNotFoundError(
                            "contract breach: REPORT.md missing after critic phase ended; "
                            "routing through resolver for fresh-session retry"
                        )
                    _commit_phase("critic", {"iteration": iteration})
                    break
                except Exception as e:
                    await _invoke_resolver("critic", e)
        else:
            logging.critical("CRITICAL (critic phase): reached unreachable code")
            exit(1)

        # Retrospective phase (inside loop — updates .unity/ before next iteration)

        _console.rule("[bold blue]Retrospective Phase[/bold blue]")

        _assert_lsp_alive("retrospective")
        try:
            with open(PROMPTS_DIR / "RETROSPECTIVE.md", "r") as f:
                RETROSPECTIVE_PROMPT = with_library(f.read().format(
                    SOURCE_PATH=source,
                    LIBRARY_DIR=str(_get_library_dir()),
                    PROJECT_NOTES_DIR=str(_get_project_notes_dir()),
                    SUBAGENTS_DIR=str(_SUBAGENTS_DIR),
                    DEFAULT_SUBAGENTS_DIR=str(_DEFAULT_SUBAGENTS_DIR),
                ))

            async for message in _query_with_idle_timeout(
                prompt=f"Run the retrospective for the unity formalization of {source}.",
                options=ClaudeAgentOptions(
                    tools=_ALL_TOOLS,
                    allowed_tools=_ALL_TOOLS,
                    agents={**LIBRARY_SUBAGENTS},
                    system_prompt=RETROSPECTIVE_PROMPT,
                    mcp_servers=LEAN_MCP_SERVER,
                    hooks=FORUM_HOOKS,
                    permission_mode=PERMISSIONS,

                    enable_file_checkpointing=True,
                    model="opus",
                    fallback_model="sonnet",
                    env=_primary_env,

                ),
            ):
                _log_agent_message(message)

            logging.info("Retrospective phase completed successfully!")
        except Exception as e:
            logging.error(f"ERROR (retrospective phase): {e}")

        # Reload library context so next iteration picks up .unity/ additions
        library_context = _load_library_context()

        # Stagnation check: compare sorry-carrying chunks across iterations
        try:
            current_sorry_chunks = _collect_chunk_sorry_set(Path.cwd(), project_path)
            if previous_sorry_chunks is not None and current_sorry_chunks and current_sorry_chunks == previous_sorry_chunks:
                logging.warning(
                    f"Critic iteration {iteration}: sorry set unchanged from previous iteration "
                    f"({len(current_sorry_chunks)} chunk(s)): {sorted(current_sorry_chunks)}"
                )
            previous_sorry_chunks = current_sorry_chunks
        except Exception as e:
            logging.warning(f"stagnation check failed: {e}")

        # Escalation phase (stagnant chunks only; no-op if none)
        try:
            await _run_escalation_phase(iteration, str(source) if source else None)
        except Exception as e:
            logging.error(f"ERROR (escalation phase): {e}")

        # Critic loop status check
        try:
            report_text = _read_report_md()
            if re.search(r"\*\*Status:\*\*\s+COMPLETE", report_text, re.IGNORECASE):
                logging.info("Critic declared formalization complete.")
                break
            elif max_critic_iterations is not None and iteration + 1 >= max_critic_iterations:
                logging.warning(f"Reached maximum iterations ({max_critic_iterations}) — stopping loop.")
                break
            else:
                iteration += 1
                logging.info(f"Critic requested revision — starting iteration {iteration + 1}...")
        except FileNotFoundError:
            logging.warning("No REPORT.md found after critic phase — stopping loop.")
            break

    # Cleanup

    logging.info("Cleaning up...")

    try:
        if not save_spec:
            spec_dir = Path("language")
            if spec_dir.exists():
                shutil.rmtree(spec_dir)

        if not save_semiformalization:
            semiformal_dir = Path("semiformal")
            if semiformal_dir.exists():
                shutil.rmtree(semiformal_dir)
    except Exception as e:
        logging.error(f"ERROR (clean up): {e}")

    logging.info("Clean up completed successfully!")

    _console.rule("[bold blue]Summary[/bold blue]")
    try:
        _console.print(Markdown(_read_report_md()))
    except FileNotFoundError:
        logging.warning("No REPORT.md found — critic may not have completed.")
    except Exception as e:
        logging.error(f"ERROR (summarization): {e}")

    logging.info("Unity has completed!")
    return 0
