"""Shared multi-agent engine: dispatch the roster per phase via the spawners."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from rich.console import Console

from .spawn import spawn
from . import worktree, library

_console = Console()
_PROMPTS = Path(__file__).parent / "prompts"


def mark_phase(command: str, phase: str) -> None:
    """Record the running phase (.unity/state.json, for the web navbar) and print a
    timestamped delimiter banner (lands in the web run log)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    _console.print(f"\n[bold]════════ {stamp} · {command} · {phase} ════════[/bold]")
    try:
        from .config import find_unity_dir
        u = find_unity_dir(Path.cwd())
        if u is not None:
            (u / "state.json").write_text(json.dumps(
                {"command": command, "phase": phase, "ts": time.time()}))
    except OSError:
        pass


def load_prompt(name: str) -> str:
    # A "command/PHASE" prompt load is exactly a phase transition — mark it here so
    # every command gets phase tracking without per-command bookkeeping.
    if "/" in name:
        cmd, phase = name.split("/", 1)
        mark_phase(cmd, phase.lower())
    return (_PROMPTS / f"{name}.md").read_text()


def stop_requested(cwd) -> bool:
    """True when a safe stop was requested (.unity/stop-requested exists): finish the
    current agent turns, then skip all remaining phases."""
    from .config import find_unity_dir
    u = find_unity_dir(Path(cwd))
    return u is not None and (u / "stop-requested").exists()


def build_mcp(paths) -> dict:
    """MCP servers every agent attaches to (stdio; forum is file-backed + flock-safe)."""
    servers = {
        "lean-lsp": {"command": "uvx", "args": ["lean-lsp-mcp"]},
        "unity-forum": {
            "command": sys.executable,
            "args": ["-m", "unity.forum.server", "--forum-dir", str(paths.forum)],
        },
    }
    axle_key = os.getenv("AXLE_API_KEY")
    if axle_key:
        servers["axle"] = {
            "command": "uvx",
            "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"],
            "env": {"AXLE_API_KEY": axle_key},
        }
    aristotle_key = os.getenv("ARISTOTLE_API_KEY")
    if aristotle_key:
        servers["aristotle"] = {
            "command": sys.executable,
            "args": ["-m", "unity.aristotle"],
            "env": {"ARISTOTLE_API_KEY": aristotle_key},
        }
    return servers


def _effective_ranking(roster, forum_dir) -> dict:
    """Dynamic capability ranking: static strength + a bounded boost from forum ICRL
    credit (earned by posts and upvotes on an agent's contributions). Re-computed at
    every dispatch, so standings shift as the run progresses."""
    balances = {}
    try:
        raw = json.loads((Path(forum_dir) / "balances.json").read_text())
        balances = {k.lower(): v.get("balance", 0.0) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return {
        a.name: a.strength + min(3.0, max(-1.0, balances.get(a.name.lower(), 0.0) / 10.0))
        for a in roster.agents
    }


def _preamble(agent, roster, ranking: dict | None = None) -> str:
    ranking = ranking or {a.name: float(a.strength) for a in roster.agents}
    order = sorted(roster.agents, key=lambda a: -ranking[a.name])
    standing = {a.name: i + 1 for i, a in enumerate(order)}
    team = "\n".join(
        f"- {a.name}: {a.model} ({a.backend}) — standing #{standing[a.name]}, "
        f"effective capability {ranking[a.name]:.1f} (base strength {a.strength})"
        f"{' [primary]' if a.is_primary else ''}"
        for a in order
    )
    return (
        f"You are agent '{agent.name}', running model '{agent.model}' (backend: {agent.backend}).\n"
        f"You are collaborating with this team via the forum:\n{team}\n"
        f"Standings are dynamic: effective capability re-ranks from forum credit as the run "
        f"progresses — strong contributions raise your standing, and chunk sign-ups should follow "
        f"current standings, not the initial ordering.\n"
        f"The primary agent is '{roster.primary.name}'.\n"
        f"Persistence norm: end your turn only when your phase's deliverable is complete or you are "
        f"hard-blocked (post the forum_obstacle first). Running low on ideas is not completion — post "
        f"your state, pick the next approach, and continue; an early quit wastes the whole team's "
        f"work.\n\n"
    )


async def dispatch(agents, roster, base_prompt, task, cwd, mcp):
    """Spawn `agents` concurrently with per-agent prompts; await all, log failures.

    `cwd` is a single Path (shared) or a dict {agent.name: Path} (per-agent worktrees)."""
    def _cwd(a):
        return cwd[a.name] if isinstance(cwd, dict) else cwd

    any_cwd = next(iter(cwd.values())) if isinstance(cwd, dict) else cwd
    if stop_requested(any_cwd):
        _console.print("[yellow]stop requested — skipping phase[/yellow]")
        return []

    tools_ref = load_prompt("TOOLS")
    context = library.library_context()
    full = base_prompt + f"\n\n{tools_ref}" + (f"\n\n{context}" if context else "")
    subagents = library.library_subagents()

    # Dynamic capability re-ranking: standings from forum credit, refreshed per dispatch.
    from .config import find_unity_dir
    unity_dir = find_unity_dir(Path(any_cwd))
    ranking = _effective_ranking(roster, unity_dir / "forum") if unity_dir else None

    def _brief(a) -> str:
        """Workspace digest injected per agent: binding decisions, latest handoff, open
        obstacles/questions, ledger highlights. Empty on a fresh run (nothing binding yet);
        it earns its keep at phase boundaries, critic-loop iterations, and --continue runs.
        Intra-phase freshness comes from the forum_brief tool instead."""
        if unity_dir is None or os.getenv("UNITY_FORUM_BRIEF", "on").lower() == "off":
            return ""  # UNITY_FORUM_BRIEF=off -> H3 substrate ablation
        try:
            from .forum import server as forum_server
            forum_server.FORUM_DIR = unity_dir / "forum"
            text = forum_server.build_brief(a.name)
            return f"\nWorkspace brief (live state — refresh anytime with forum_brief):\n{text}\n" if text else ""
        except Exception:
            return ""

    results = await asyncio.gather(
        *[spawn(a, _preamble(a, roster, ranking) + _brief(a) + full, task, _cwd(a), mcp,
                subagents=subagents)
          for a in agents],
        return_exceptions=True,
    )
    for a, r in zip(agents, results):
        if isinstance(r, Exception):
            _console.print(f"[red]agent {a.name} failed: {r!r}[/red]")
    return results


def read_approved(paths) -> bool:
    """Read the critic's approval flag from .unity/critic.json (False if absent/invalid)."""
    f = paths.unity / "critic.json"
    if not f.exists():
        return False
    try:
        return bool(json.loads(f.read_text()).get("approved", False))
    except (json.JSONDecodeError, OSError):
        return False
    
def read_finalized(paths) -> bool:
    """Read the finalized flag from .unity/finalized.json (True if absent/invalid)."""
    f = paths.unity / "finalized.json"
    if not f.exists():
        return True
    try:
        return bool(json.loads(f.read_text()).get("finalized", True))
    except (json.JSONDecodeError, OSError):
        return True


def toposort(paths) -> None:
    """Toposort the chunk dependency graph in .unity/dag.json into layers (Kahn's),
    writing `layers` back. For the forum viewer + the agents' own traversal — never used
    to gate dispatch. Re-run whenever chunks change. Ported from unity_agent._toposort_chunks."""
    dag_path = paths.unity / "dag.json"
    if not dag_path.exists():
        _console.print("[red]no .unity/dag.json to toposort[/red]")
        return
    data = json.loads(dag_path.read_text())
    chunks = data.get("chunks", [])
    if not chunks:
        return

    chunk_ids = {c["id"] for c in chunks}
    in_degree = {c["id"]: 0 for c in chunks}
    dependents = {c["id"]: [] for c in chunks}
    for c in chunks:
        for dep in c.get("dependencies", []):
            dep_id = dep["chunk_id"] if isinstance(dep, dict) else dep
            if dep_id in chunk_ids:
                in_degree[c["id"]] += 1
                dependents[dep_id].append(c["id"])

    layers = []
    ready = sorted(cid for cid in chunk_ids if in_degree[cid] == 0)
    while ready:
        layers.append(ready)
        nxt = []
        for cid in ready:
            for child in dependents[cid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    nxt.append(child)
        ready = sorted(nxt)

    remaining = {cid for cid, deg in in_degree.items() if deg > 0}
    if remaining:
        _console.print(f"[yellow]cycle in DAG involving {sorted(remaining)}; appending as final layer[/yellow]")
        layers.append(sorted(remaining))

    data["layers"] = layers
    dag_path.write_text(json.dumps(data, indent=2))


async def run_worktree_phase(roster, paths, mcp, base_prompt, verb):
    """Set up per-agent worktrees, dispatch once, tear down. Shared by the formalization /
    proving / optimization phases — `verb` is the action word for the task ("Formalize",
    "Prove", "Optimize"). Agents traverse the (dynamic) DAG, sign up for chunks, and reach
    consensus + merge entirely via the forum + prompt. Python does NOT traverse dag.json."""
    root = paths.project_root
    if stop_requested(root):
        _console.print("[yellow]stop requested — skipping worktree phase[/yellow]")
        return
    worktrees = {}
    for a in roster.agents:
        wt = worktree.create_worktree(a.name, root)
        worktree.symlink_lake_cache(wt, root)
        worktrees[a.name] = wt

    main_branch = worktree.detect_main_branch(root)
    task = (
        f"{verb} the project by working through .unity/dag.json (it is dynamic — re-read it as "
        f"you go). Sign up for chunks with forum_claim, work in your worktree and commit, post "
        f"each finished chunk with forum_result, and review + forum_endorse (or forum_object) "
        f"teammates' results. The primary checks forum_consensus and squash-merges each mergeable "
        f"chunk into '{main_branch}' as 'UNITY: merge chunk <id>'."
    )
    contract = load_prompt("FORUM_CONTRACT")
    try:
        await dispatch(roster.agents, roster, base_prompt + "\n\n" + contract, task, worktrees, mcp)
    finally:
        for a in roster.agents:
            worktree.cleanup_worktree(a.name, worktrees[a.name], root)
        library.update_strengths(roster, paths.unity / "forum")
