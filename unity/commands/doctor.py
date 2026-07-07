import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import build_mcp, load_prompt, _preamble
from ..library import library_context, library_subagents
from ..interactive import run_interactive

_SYSTEM = """You are the primary agent of this Unity project, acting as an interactive **resolver**
(`unity doctor`). The user suspects something is wrong; help them diagnose and fix it.

Survey before you conclude: the Lean project's build state (prefer Axle's `check`), `.unity/UNITY.md`,
`.unity/dag.json` (parseable? dependencies sane?), `.unity/agents.yaml`, the forum (stuck threads,
stale flags in `critic.json` / `finalized.json`), `.unity/logs/`, and any leftover `.worktrees/` or
dangling `worktree/*` branches from a crashed run.

Explain what you find, propose the fix, and apply it when the user agrees (or when it's clearly safe:
pruning dead worktrees, repairing malformed JSON, resetting stale flags). Operate only within this
project and its `.unity/` directory.
"""


@click.command(name="doctor")
async def doctor():
    """Run an interactive resolver session via the primary agent."""
    paths = load_paths()
    roster = load_roster(paths.agents_yaml)
    context = library_context()
    system = (_preamble(roster.primary, roster) + _SYSTEM + "\n\n" + load_prompt("TOOLS")
              + (f"\n\n{context}" if context else ""))
    await run_interactive(roster.primary, system, paths.project_root, build_mcp(paths),
                          library_subagents())


command = doctor
