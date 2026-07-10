import asyncclick as click

from ..config import load_paths
from ..forum import web


@click.command(name="serve")
@click.option("--port", default=8080, type=int, help="Port for the dashboard.")
async def serve(port):
    """Serve the web control center (run from inside a unity-initialized project)."""
    paths = load_paths()  # errors with a clear message when no project .unity/ exists
    click.echo(f"Serving the unity control center on http://localhost:{port}  (project: {paths.project_root.name})")
    await web.run(paths.forum, paths.unity, port)


command = serve
