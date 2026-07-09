"""Read the global Unity library (~/.unity/library/) for injection into agent prompts.

The library is the shared, cross-run knowledge store the retrospective phase writes to:
tactics/, lemmas/, references/, and subagents/. `library_context()` is injected into every
agent's system prompt; `library_subagents()` is registered per backend by the spawners.
Ported from unity_agent.pipeline (_load_library_context / _load_library_subagents)."""

import re
from pathlib import Path

_DEFAULT_TOOLS = "Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Agent,Skill"


SUBDIRS = ("tactics", "lemmas", "references", "subagents", "skills")


def library_dir() -> Path:
    return Path.home() / ".unity" / "library"


def ensure_library() -> Path:
    """Create the global library skeleton if missing; returns the library dir."""
    lib = library_dir()
    for sub in SUBDIRS:
        (lib / sub).mkdir(parents=True, exist_ok=True)
    return lib


# ── Autostrength ────────────────────────────────────────────────────────────────
# Per-model strengths learned across runs: an EMA over each run's end-of-phase forum
# credit (the same clamp used for dynamic re-ranking). Rosters no longer declare
# strength; an explicit strength in agents.yaml still overrides the learned value.

_STRENGTH_BASE = 5.0
_STRENGTH_EMA = 0.3  # weight of the newest observation


def strengths_path() -> Path:
    return library_dir() / "strengths.json"


def learned_strength(model: str) -> float:
    """The stored strength for a model (default 5.0)."""
    import json
    try:
        return float(json.loads(strengths_path().read_text()).get(model, _STRENGTH_BASE))
    except (OSError, ValueError, json.JSONDecodeError):
        return _STRENGTH_BASE


def update_strengths(roster, forum_dir: Path) -> None:
    """Fold this run's forum credit into the per-model strength store (best-effort)."""
    import json
    try:
        raw = json.loads((Path(forum_dir) / "balances.json").read_text())
        balances = {k.lower(): v.get("balance", 0.0) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, AttributeError):
        return
    observed: dict[str, list[float]] = {}
    for a in roster.agents:
        boost = min(3.0, max(-1.0, balances.get(a.name.lower(), 0.0) / 10.0))
        observed.setdefault(a.model, []).append(_STRENGTH_BASE + boost)
    try:
        stored = json.loads(strengths_path().read_text())
    except (OSError, json.JSONDecodeError):
        stored = {}
    for model, obs in observed.items():
        prev = float(stored.get(model, _STRENGTH_BASE))
        new = (1 - _STRENGTH_EMA) * prev + _STRENGTH_EMA * (sum(obs) / len(obs))
        stored[model] = round(new, 2)
    try:
        strengths_path().parent.mkdir(parents=True, exist_ok=True)
        strengths_path().write_text(json.dumps(stored, indent=2))
    except OSError:
        pass


def _first_heading(path: Path) -> str:
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#"):
                return re.sub(r"^#+\s*", "", line)
    except OSError:
        pass
    return path.stem.replace("-", " ").replace("_", " ").title()


def library_context() -> str:
    """A manifest of available library files, for injection into system prompts. Empty
    string if the library is absent/empty. Agents Read individual files on demand."""
    lib = library_dir()
    sections: list[str] = []
    for subdir in ("tactics", "lemmas", "references", "skills"):
        d = lib / subdir
        if not d.exists():
            continue
        candidates = list(d.glob("*.md")) + list(d.glob("*/SKILL.md"))
        files = sorted(f for f in candidates if f.stat().st_size > 0)
        if not files:
            continue
        lines = [f"*{subdir}/*"] + [f"- `{f}` — {_first_heading(f)}" for f in files]
        sections.append("\n".join(lines))
    if not sections:
        return ""
    return (
        "**Unity library** — `~/.unity/library/`\nUse `Read` to access any file listed below.\n\n"
        + "\n\n".join(sections)
    )


def library_subagents() -> list[dict]:
    """Parse ~/.unity/library/subagents/*.md frontmatter into backend-neutral dicts
    ({name, description, prompt, tools}). The spawners adapt these to each backend."""
    out: list[dict] = []
    d = library_dir() / "subagents"
    if not d.exists():
        return out
    for md in sorted(d.glob("*.md")):
        m = re.match(r"^---\n(.*?)\n---\n(.*)", md.read_text(), re.DOTALL)
        if not m:
            continue
        fm, body = m.group(1), m.group(2).strip()
        meta: dict = {}
        for line in fm.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        tools = [t.strip() for t in meta.get("tools", _DEFAULT_TOOLS).split(",")]
        out.append({
            "name": meta.get("name", md.stem),
            "description": meta.get("description", ""),
            "prompt": body,
            "tools": tools,
        })
    return out
