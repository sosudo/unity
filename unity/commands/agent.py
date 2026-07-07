import asyncclick as click

from ..config import load_paths
from ..roster import load_roster
from ..orchestrator import build_mcp, load_prompt, _preamble
from ..library import library_context, library_subagents
from ..interactive import run_interactive

_SYSTEM = """You are the primary agent of this Unity project, in an interactive session with the user.

Help them with whatever they ask about the Lean project: explore or explain the code, prove or sketch
things, search Mathlib, inspect the forum, run builds, or make edits they request. Use your tools; don't
guess when a tool can tell you. Operate only within this project and its `.unity/` directory.
"""


@click.command(name="agent")
async def agent():
    """Run an interactive session with the primary agent."""
    paths = load_paths()
    roster = load_roster(paths.agents_yaml)
    context = library_context()
    system = (_preamble(roster.primary, roster) + _SYSTEM + "\n\n" + load_prompt("TOOLS")
              + (f"\n\n{context}" if context else ""))
    await run_interactive(roster.primary, system, paths.project_root, build_mcp(paths),
                          library_subagents())


command = agent
