"""Unity Agent CLI - Command line interface for autoformalization pipeline."""

import sys
import os
import shutil
import asyncclick as click
from pathlib import Path

from .setup_cmd import run_setup
from .pipeline import run_pipeline, _get_library_dir, _infer_flags


@click.group(invoke_without_command=True)
@click.option(
    "--source", "-s",
    default=None,
    type=click.Path(dir_okay=True, readable=True),
    help="Source material to autoformalize (file or directory)"
)
@click.option(
    "--project", "-p",
    default=None,
    type=click.Path(exists=False, dir_okay=True, readable=True),
    help="Target Lean project directory (required when --prove is set)"
)
@click.option(
    "--context", "-c",
    is_flag=True,
    default=False,
    help="Use existing Lean files in project as context"
)
@click.option(
    "--prove", is_flag=True, default=False,
    help=(
        "Proof-completion mode. "
        "With --source: formalize declarations faithfully; proofs may use any strategy. "
        "Without --source: fill in sorrys in an existing project (requires --context)."
    )
)
@click.option(
    "--depth",
    default=1,
    type=int,
    help="Maximum recursion depth available to agents for child unity calls (0 = no recursion allowed)"
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(dir_okay=True),
    help="Output directory for this unity run; unity will chdir here before running (used when spawned as a child)"
)
@click.pass_context
async def main(ctx, source, project, context, prove, depth, output_dir):
    """Unity Agent - Autoformalization pipeline for Lean theorem proving.

    Run the pipeline:

        unity --source paper.tex --project ./lean_proj

    Or use subcommands:

        unity setup    Generate .env configuration file
        unity reset    Restore PROMPTS/SUBAGENTS/TEAMS from defaults
        unity clean    Clear global library cache and local project notes
    """
    if ctx.invoked_subcommand is None:
        # Inference: when no flags are supplied at all, detect from CWD
        if source is None and project is None and not prove:
            click.echo("No flags supplied — running inference to detect source, project, and prove...")
            source, project, prove = await _infer_flags()
            if source or project or prove:
                click.echo(f"Inferred: source={source!r}  project={project!r}  prove={prove}")
            else:
                click.echo("Inference inconclusive — using defaults.")

        if prove:
            # --prove requires --project to be explicitly specified
            if project is None:
                raise click.UsageError("--prove requires --project/-p to be specified")
            # Path 2: no source — requires --context
            if source is None and not context:
                raise click.UsageError(
                    "--prove without --source requires --context/-c "
                    "(the project must already contain declarations to complete)"
                )
            # Path 1: source provided — validate it exists
            if source is not None and not os.path.exists(source):
                raise click.BadParameter(
                    f"Source '{source}' does not exist.",
                    param_hint="'--source' / '-s'"
                )
        else:
            # Normal mode: default project to cwd, source to source.tex
            if project is None:
                project = "."
            if source is None:
                source = "source.tex"
            if not os.path.exists(source):
                raise click.BadParameter(
                    f"Source '{source}' does not exist.",
                    param_hint="'--source' / '-s'"
                )

        exit_code = await run_pipeline(source, project, context, prove, depth, output_dir)
        sys.exit(exit_code if exit_code else 0)


@main.command()
@click.option(
    "--output", "-o",
    default=".env",
    type=click.Path(dir_okay=False),
    help="Output path for .env file"
)
def setup(output):
    """Generate .env configuration file interactively."""
    run_setup(output)


@main.command()
def reset():
    """Restore PROMPTS, SUBAGENTS, and TEAMS from their DEFAULT counterparts."""
    pkg = Path(__file__).parent
    pairs = [
        (pkg / "DEFAULT_PROMPTS",   pkg / "PROMPTS"),
        (pkg / "DEFAULT_SUBAGENTS", pkg / "SUBAGENTS"),
        (pkg / "DEFAULT_TEAMS",     pkg / "TEAMS"),
    ]
    for src, dst in pairs:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        click.echo(f"Restored {dst.name}/ from {src.name}/")
    click.echo("Reset complete.")


@main.command()
def clean():
    """Clear the global library cache (~/.unity/library/) and local project notes (.unity/) if present."""
    lib = _get_library_dir()
    if lib.exists():
        shutil.rmtree(lib)
    for subdir in ("tactics", "lemmas", "ir-patterns", "subagents"):
        (lib / subdir).mkdir(parents=True, exist_ok=True)
    click.echo(f"Library cleared: {lib}")

    project_notes = Path.cwd() / ".unity"
    if project_notes.exists():
        shutil.rmtree(project_notes)
        click.echo(f"Project notes cleared: {project_notes}")


def cli():
    """Entry point for the CLI."""
    main(_anyio_backend="asyncio")


if __name__ == "__main__":
    cli()
