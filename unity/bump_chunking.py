"""Per-attempt chunking drafts, using the normal project and Forum connection.

Drafts are separate from the accepted plan, but this is not a security boundary.
The chunker reads a private snapshot of the existing project, not the user's checkout.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChunkingWorkspace:
    cwd: Path
    draft_path: Path
    env: dict[str, str] = field(repr=False)
    mcp: dict = field(repr=False)
    preserve: bool = False


@asynccontextmanager
async def chunking_workspace(paths, author: str, attempt: dict):
    """Keep a draft until acceptance/archival without starting a separate service."""
    from .bump_orchestrator import build_bump_mcp
    from .bump_worktree import role_view

    drafts = paths.forum / "chunking-drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="draft-", dir=drafts)).resolve()
    draft_path = directory / "dag.json"
    env = {
        "UNITY_BUMP_PROFILE": "chunking",
        "UNITY_BUMP_DRAFT_PATH": str(draft_path),
    }
    workspace = ChunkingWorkspace(role_view(paths.project_root, "chunking"), draft_path, env, {})
    try:
        forum = build_bump_mcp(paths, "chunking")["unity-forum"]
        forum["env"] = {**forum.get("env", {}), **env}
        workspace.mcp = {"unity-forum": forum}
        yield workspace
    finally:
        # The controller archives incomplete drafts before leaving this scope.
        # If archival failed, retain the only copy for manual recovery.
        if not workspace.preserve:
            shutil.rmtree(directory)
