"""Aristotle MCP server — submit Lean proving jobs to Harmonic's Aristotle (v3) and poll them.

Aristotle runs asynchronously with no push notifications, so the flow is:
submit -> (keep working) -> poll aristotle_status (or bounded aristotle_wait) -> aristotle_result.

A Project is RUNNING while Aristotle works and IDLE when it has finished; the granular work is
its agent tasks. Auth is via the ARISTOTLE_API_KEY environment variable (read by aristotlelib)."""

import asyncio
import time

from fastmcp import FastMCP
from aristotlelib import Project, ProjectStatus, TaskStatus

mcp = FastMCP("aristotle")

_TASK_TERMINAL = {
    TaskStatus.COMPLETE, TaskStatus.COMPLETE_WITH_ERRORS,
    TaskStatus.OUT_OF_BUDGET, TaskStatus.FAILED, TaskStatus.CANCELED,
}


def _project_info(p: Project) -> dict:
    return {"project_id": p.project_id, "status": p.status.name, "description": p.description}


def _task_info(t) -> dict:
    return {
        "task_id": t.agent_task_id,
        "status": t.status.name,
        "percent_complete": t.percent_complete,
        "output_summary": t.output_summary,
    }


@mcp.tool()
async def aristotle_submit(prompt: str, project_dir: str | None = None) -> dict:
    """Submit a job to Aristotle. `prompt` is the instruction (e.g. "fill in all sorries");
    `project_dir` is an optional directory (e.g. your Lean project) uploaded as context.
    Returns the project_id to poll — the job runs asynchronously, so this returns immediately."""
    if project_dir:
        p = await Project.create_from_directory(prompt, project_dir)
    else:
        p = await Project.create(prompt)
    return _project_info(p)


@mcp.tool()
async def aristotle_status(project_id: str) -> dict:
    """Poll a job. Returns the project status (RUNNING = still working, IDLE = finished) and a
    summary of its agent tasks (each QUEUED / IN_PROGRESS / COMPLETE / COMPLETE_WITH_ERRORS /
    OUT_OF_BUDGET / FAILED / CANCELED, with percent_complete)."""
    p = await Project.from_id(project_id)
    await p.refresh()
    tasks, _ = await p.get_tasks()
    return {**_project_info(p), "tasks": [_task_info(t) for t in tasks]}


@mcp.tool()
async def aristotle_result(project_id: str, destination: str | None = None) -> dict:
    """Download a finished job's result files (a .tar.gz of the resulting Lean project) to
    `destination`. Returns the saved path, or reports that no result is available yet."""
    p = await Project.from_id(project_id)
    await p.refresh()
    # get_files() falls back to the *input* tar when no result exists yet; gate on has_files
    # so we never hand back the input as if it were a finished result.
    if not getattr(p, "has_files", False):
        return {**_project_info(p), "path": None, "message": "no result available yet — still running"}
    try:
        path = await p.get_files(destination)
        return {**_project_info(p), "path": str(path)}
    except Exception as e:  # noqa: BLE001 — surface any download error to the agent
        return {**_project_info(p), "path": None, "message": f"result download failed: {e}"}


@mcp.tool()
async def aristotle_wait(project_id: str, timeout_seconds: int = 600, poll_seconds: int = 15) -> dict:
    """Poll until the job finishes (project status IDLE) or `timeout_seconds` elapses (bounded, so
    it cannot block forever). Use for short jobs; for long ones, submit then poll aristotle_status."""
    p = await Project.from_id(project_id)
    await p.refresh()
    deadline = time.monotonic() + timeout_seconds
    while p.status == ProjectStatus.RUNNING:
        if time.monotonic() >= deadline:
            return {**_project_info(p), "timed_out": True}
        await asyncio.sleep(poll_seconds)
        await p.refresh()
    return {**_project_info(p), "timed_out": False}


@mcp.tool()
async def aristotle_cancel(project_id: str) -> dict:
    """Cancel a project's queued / in-progress agent tasks."""
    p = await Project.from_id(project_id)
    tasks, _ = await p.get_tasks()
    canceled = []
    for t in tasks:
        if t.status not in _TASK_TERMINAL:
            await t.cancel()
            canceled.append(t.agent_task_id)
    return {"project_id": project_id, "canceled_tasks": canceled}


@mcp.tool()
async def aristotle_list(limit: int = 10) -> dict:
    """List recent Aristotle projects (most recent first)."""
    try:
        projects, _ = await Project.list_projects(limit=limit)
        return {"projects": [_project_info(p) for p in projects]}
    except Exception as e:  # noqa: BLE001 — list endpoint can be unavailable; don't block the agent
        return {"projects": [], "error": str(e)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
