import shutil
import asyncclick as click
import yaml
from pathlib import Path

from ..config import UNITY_DIRNAME, Paths
from .. import lake
from ..library import ensure_library

_DEFAULT_METRICS = Path(__file__).parent.parent / "defaults" / "metrics"

_ENV_DEFAULT = (
    "# Critic-loop cap per run\n"
    "MAX_ATTEMPTS=5\n"
    "# Set to off to disable workspace-brief injection (ablation)\n"
    "UNITY_FORUM_BRIEF=on\n"
    "# Optional service keys — unlock extra agent tools when set\n"
    "AXLE_API_KEY=\n"
    "ARISTOTLE_API_KEY=\n"
)


def _is_lean_project(root: Path) -> bool:
    return (root / "lakefile.toml").exists() or (root / "lakefile.lean").exists()


def _seed_default_metrics(metrics_dir: Path) -> None:
    """Seed the default ImProver metrics; never overwrite a user's existing metric."""
    metrics_dir.mkdir(parents=True, exist_ok=True)
    for src in _DEFAULT_METRICS.glob("*.md"):
        dest = metrics_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)


_AGENTS_TEMPLATE = """\
# Unity roster — add agents here or (easier) in the web UI: `unity serve` -> agents tab.
# One group per model; `names` spawns one agent instance per name.
# The PRIMARY agent (mark a group with `primary: true`; default: the first group) runs the
# solo phases: preparation, architect bootstrap, the critic, and the retrospective — make it
# your strongest model. Strength is learned automatically per model (autostrength); set
# `strength:` only to override. `budget`: USD per agent instance.
#
# agents:
# - names: [Ada]
#   model: claude-opus-4-6
#   backend: claude_code        # claude_code | codex
#   provider: anthropic
#   primary: true
#   budget: 10
agents: []
"""


def _write_agents_yaml(path: Path) -> None:
    path.write_text(_AGENTS_TEMPLATE)


async def run_init(root: Path) -> None:
    """Prepare .unity/ for the Lean project at `root`, then warm the build."""
    if not _is_lean_project(root):
        raise click.ClickException(
            "not a Lean project (no lakefile.toml/lakefile.lean here) — cd into your project first"
        )

    unity = root / UNITY_DIRNAME
    if unity.exists() and not click.confirm(f"{unity} exists — overwrite its config?", default=False):
        raise click.Abort()

    unity.mkdir(exist_ok=True)
    paths = Paths.from_unity_dir(unity)
    paths.forum.mkdir(exist_ok=True)
    paths.logs.mkdir(exist_ok=True)
    _seed_default_metrics(unity / "metrics")
    ensure_library()

    paths.env.write_text(_ENV_DEFAULT)
    _write_agents_yaml(paths.agents_yaml)
    paths.unity_md.write_text(
        "# Goal\n\n<what to formalize / prove / create>\n\n"
        "## State\n\n<maintained across runs by the primary agent>\n"
    )

    gi = root / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if ".unity/" not in existing.split():
        with gi.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(".unity/\n")

    click.echo(f"\nInitialized {unity}")
    click.echo("Next: `unity serve` -> agents tab to set up your roster (presets included),")
    click.echo("edit the goal under the prompt tab, then hit run.")
    click.echo("Fetching build cache...")
    lake.cache_get(root)
    click.echo("Building project...")
    lake.build(root)


@click.command(name="init")
async def init():
    """Prepare .unity/ for an existing Lean project (roster is configured in `unity serve`)."""
    await run_init(Path.cwd())


command = init
