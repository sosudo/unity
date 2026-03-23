"""Unity Forum MCP Server.

File-backed forum for formalization agents. Each thread is stored as
forum/<thread_id>.json. Run via:

    python -m unity_agent.forum_mcp --forum-dir <path>
"""

import argparse
import json
import math
import time
import uuid
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("unity-forum")
FORUM_DIR: Path = Path("forum")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _thread_path(thread_id: str) -> Path:
    return FORUM_DIR / f"{thread_id}.json"


def _load(thread_id: str) -> dict:
    path = _thread_path(thread_id)
    if not path.exists():
        raise ValueError(f"Thread '{thread_id}' does not exist. Call forum_create_thread first.")
    return json.loads(path.read_text())


def _save(data: dict) -> None:
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    _thread_path(data["thread_id"]).write_text(json.dumps(data, indent=2))


def _hot(post: dict) -> float:
    """Reddit hot-sort score: log10(max(|score|,1)) * sign(score) + timestamp/45000."""
    score = post["upvotes"] - post["downvotes"]
    sign = 1 if score > 0 else (-1 if score < 0 else 0)
    return math.log10(max(abs(score), 1)) * sign + post["timestamp"] / 45000


def _sorted_posts(posts: list[dict], sort: str) -> list[dict]:
    if sort == "hot":
        return sorted(posts, key=_hot, reverse=True)
    if sort == "new":
        return sorted(posts, key=lambda p: p["timestamp"], reverse=True)
    if sort == "top":
        return sorted(posts, key=lambda p: p["upvotes"] - p["downvotes"], reverse=True)
    raise ValueError("sort must be 'hot', 'new', or 'top'")


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def forum_create_thread(thread_id: str, title: str, description: str = "") -> str:
    """Create a new forum thread.

    Call once per chunk (thread_id='chunk-1', title=declaration name) and once
    for global cross-chunk discussion (thread_id='global', title='Global Discussion').
    Agents may create additional threads as needed. Returns a confirmation string.
    """
    if _thread_path(thread_id).exists():
        return f"Thread '{thread_id}' already exists."
    _save({
        "thread_id": thread_id,
        "title": title,
        "description": description,
        "created_at": int(time.time()),
        "posts": [],
    })
    return f"Thread '{thread_id}' created."


@mcp.tool()
def forum_post(
    thread_id: str,
    author: str,
    content: str,
    reply_to: str | None = None,
) -> dict:
    """Post a message to a forum thread.

    Returns the new post's metadata: post_id, author, timestamp, upvotes,
    downvotes, reply_to. Use post_id to vote on or redact this post later.
    """
    data = _load(thread_id)
    post = {
        "post_id": uuid.uuid4().hex[:8],
        "author": author,
        "content": content,
        "timestamp": int(time.time()),
        "upvotes": 0,
        "downvotes": 0,
        "reply_to": reply_to,
        "redacted": False,
    }
    data["posts"].append(post)
    _save(data)
    return {k: v for k, v in post.items()}


@mcp.tool()
def forum_vote(thread_id: str, post_id: str, vote: str) -> dict:
    """Vote on a post. vote must be 'up' or 'down'.

    Returns updated upvote and downvote counts for the post.
    """
    if vote not in ("up", "down"):
        raise ValueError("vote must be 'up' or 'down'")
    data = _load(thread_id)
    for post in data["posts"]:
        if post["post_id"] == post_id:
            if vote == "up":
                post["upvotes"] += 1
            else:
                post["downvotes"] += 1
            _save(data)
            return {"post_id": post_id, "upvotes": post["upvotes"], "downvotes": post["downvotes"]}
    raise ValueError(f"Post '{post_id}' not found in thread '{thread_id}'.")


@mcp.tool()
def forum_redact(thread_id: str, post_id: str) -> str:
    """Mark a post as [REDACTED]. Posts are never deleted — only their content is hidden.

    Use this when a post is outdated or wrong rather than letting stale
    information mislead other agents.
    """
    data = _load(thread_id)
    for post in data["posts"]:
        if post["post_id"] == post_id:
            post["redacted"] = True
            post["content"] = "[REDACTED]"
            _save(data)
            return f"Post '{post_id}' redacted."
    raise ValueError(f"Post '{post_id}' not found in thread '{thread_id}'.")


@mcp.tool()
def forum_read(thread_id: str, sort: str = "hot") -> dict:
    """Read a forum thread.

    sort can be:
    - 'hot'  (default): Reddit algorithm — log10(max(|score|,1)) * sign(score) + timestamp/45000
    - 'new':  newest posts first
    - 'top':  highest net score (upvotes − downvotes) first

    Returns thread metadata and the full list of posts in the requested order.
    """
    data = _load(thread_id)
    return {
        "thread_id": data["thread_id"],
        "title": data["title"],
        "description": data["description"],
        "created_at": data["created_at"],
        "post_count": len(data["posts"]),
        "posts": _sorted_posts(data["posts"], sort),
        "sort": sort,
    }


@mcp.tool()
def forum_list() -> list:
    """List all forum threads with summary metadata.

    Returns a list of {thread_id, title, description, post_count, last_activity}.
    """
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    threads = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            last_activity = max(
                (p["timestamp"] for p in data["posts"]),
                default=data["created_at"],
            )
            threads.append({
                "thread_id": data["thread_id"],
                "title": data["title"],
                "description": data["description"],
                "post_count": len(data["posts"]),
                "last_activity": last_activity,
            })
        except Exception:
            continue
    return threads


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global FORUM_DIR
    parser = argparse.ArgumentParser(description="Unity Forum MCP Server")
    parser.add_argument("--forum-dir", default="forum", help="Directory for forum thread files")
    args = parser.parse_args()
    FORUM_DIR = Path(args.forum_dir)
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
