import re
import shutil
import asyncclick as click

from ..library import ensure_library

_FRONTMATTER = re.compile(r"^---\n.*?name:", re.DOTALL)


@click.command(name="clean")
async def clean():
    """Remove unused Unity subagents and skills (empty or malformed library entries)."""
    lib = ensure_library()
    removed = []

    for md in lib.rglob("*.md"):
        if md.stat().st_size == 0:
            md.unlink()
            removed.append(md)
    # subagents need valid frontmatter with a name to be loadable
    for md in (lib / "subagents").glob("*.md"):
        if not _FRONTMATTER.match(md.read_text(errors="replace")):
            md.unlink()
            removed.append(md)
    # skill dirs without a SKILL.md are dead weight
    for d in (lib / "skills").iterdir():
        if d.is_dir() and not (d / "SKILL.md").is_file():
            shutil.rmtree(d)
            removed.append(d)

    for p in removed:
        click.echo(f"removed {p.relative_to(lib)}")
    click.echo(f"Cleaned {len(removed)} entr{'y' if len(removed) == 1 else 'ies'} from {lib}")


command = clean
