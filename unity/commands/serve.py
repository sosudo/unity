import asyncclick as click

from ..config import load_paths
from ..forum import web


@click.command(name="serve")
@click.option("--port", default=8080, type=int, help="Port for the dashboard.")
async def serve(port):
    """Serve the webview dashboard."""
    paths = load_paths()
    click.echo(f"Serving forum dashboard on http://localhost:{port}  (forum: {paths.forum})")
    await web.run(paths.forum, paths.unity, port)


command = serve
