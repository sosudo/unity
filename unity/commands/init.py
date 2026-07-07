import shutil
import asyncclick as click
import yaml
from pathlib import Path

from ..config import UNITY_DIRNAME, Paths
from .. import lake
from ..library import ensure_library

_DEFAULT_METRICS = Path(__file__).parent.parent / "defaults" / "metrics"

_ENV_DEFAULT = (
    "RECORDING=true\n"
    "SILENT=false\n"
    "FORUM_PORT=8080\n"
    "LEAN_LSP_PORT=8888\n"
    "MAX_ATTEMPTS=5\n"
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


async def _collect_agent(idx: int) -> dict:
    label = "primary agent" if idx == 0 else f"agent #{idx + 1}"
    click.echo(f"\n── {label} ──")
    names = [n.strip() for n in (await click.prompt("names (comma-separated)")).split(",") if n.strip()]
    backend = await click.prompt("backend", type=click.Choice(["claude_code", "codex"]), default="claude_code")
    model = await click.prompt("model")
    provider = await click.prompt("provider", default="")
    strength = await click.prompt("strength", type=int, default=5)
    budget = await click.prompt("budget USD per instance (blank = unlimited)", default="")
    base_url = await click.prompt("base_url", default="")
    api_key = await click.prompt("api_key", default="", hide_input=True, show_default=False)
    auth_token = await click.prompt("auth_token", default="", hide_input=True, show_default=False)

    agent = {"names": names, "model": model, "backend": backend}
    if provider:
        agent["provider"] = provider
    agent["strength"] = strength
    if budget:
        agent["budget"] = float(budget)
    if base_url:
        agent["base_url"] = base_url
    if api_key:
        agent["api_key"] = api_key
    if auth_token:
        agent["auth_token"] = auth_token
    return agent


def _write_agents_yaml(path: Path, agents: list[dict]) -> None:
    with path.open("w") as f:
        f.write("# First agent is the primary (preparation, critic, retrospective).\n")
        f.write("# strength: static capability tier for chunk allocation.\n")
        f.write("# budget: USD per instance (not shared across instances of a model).\n\n")
        yaml.safe_dump({"agents": agents}, f, sort_keys=False)


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

    agents = []
    while True:
        agents.append(await _collect_agent(len(agents)))
        if not click.confirm("Add another agent?", default=False):
            break

    goal = await click.prompt("\nOne-line goal (optional)", default="")

    paths.env.write_text(_ENV_DEFAULT)
    _write_agents_yaml(paths.agents_yaml, agents)
    paths.unity_md.write_text(
        f"# Goal\n\n{goal or '<what to formalize / prove / create>'}\n\n"
        "## State\n\n<maintained across runs by the primary agent>\n"
    )

    gi = root / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if ".unity/" not in existing.split():
        with gi.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(".unity/\n")

    click.echo(f"\nInitialized {unity} ({len(agents)} agent group(s))")
    click.echo("Fetching build cache...")
    lake.cache_get(root)
    click.echo("Building project...")
    lake.build(root)


@click.command(name="init")
async def init():
    """Interactively prepare .unity/ for an existing Lean project."""
    await run_init(Path.cwd())


command = init
