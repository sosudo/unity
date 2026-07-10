"""Unity Forum Web UI.

A view-only web interface for the unity forum and dependency DAG.
Reads <forum-dir>/*.json and <root-dir>/dag.json directly. Served by `unity serve`.

    python -m unity.forum.web --forum-dir ./forum --root-dir . --port 8080
or programmatically via run(forum_dir, root_dir, port).
"""

import argparse
import asyncio
import json
import subprocess
import math
import re
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI(title="unity-forum")
FORUM_DIR: Path = Path("forum")
ROOT_DIR: Path = Path(".")

_GRAPH_PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _hot(post: dict) -> float:
    score = post.get("upvotes", 0) - post.get("downvotes", 0)
    sign = 1 if score > 0 else (-1 if score < 0 else 0)
    return math.log10(max(abs(score), 1)) * sign + post["timestamp"] / 45000


def _sorted_posts(posts: list[dict], sort: str) -> list[dict]:
    if sort == "hot":
        return sorted(posts, key=_hot, reverse=True)
    if sort == "new":
        return sorted(posts, key=lambda p: p["timestamp"], reverse=True)
    if sort == "top":
        return sorted(posts, key=lambda p: p.get("upvotes", 0) - p.get("downvotes", 0), reverse=True)
    return posts


def _load_thread(thread_id: str) -> dict | None:
    path = FORUM_DIR / f"{thread_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


_DEFAULT_DIMENSIONS = ["correctness", "faithfulness"]
# Pre-Forum-2.0 default set; configs still carrying exactly these six display as the
# current two (mirrors the migration in forum/server.py).
_LEGACY_DIMENSIONS = ["correctness", "faithfulness", "style_alignment",
                      "priority", "confidence", "feasibility"]


def _load_config() -> dict:
    path = FORUM_DIR / "config.json"
    if not path.exists():
        return {"dimensions": {"active": list(_DEFAULT_DIMENSIONS), "pending": {}}, "tags": {}}
    try:
        cfg = json.loads(path.read_text())
        if not cfg.get("dimensions", {}).get("active"):
            cfg.setdefault("dimensions", {})["active"] = list(_DEFAULT_DIMENSIONS)
        if sorted(cfg["dimensions"]["active"]) == sorted(_LEGACY_DIMENSIONS):
            cfg["dimensions"]["active"] = list(_DEFAULT_DIMENSIONS)
        return cfg
    except Exception:
        return {"dimensions": {"active": list(_DEFAULT_DIMENSIONS), "pending": {}}, "tags": {}}


_SORRY_RE = re.compile(r'\bsorry\b')
_ONLY_SORRY_RE = re.compile(r':=\s*sorry\s*$|by\s*\n?\s*sorry\s*$', re.MULTILINE)


def _merged_chunk_ids() -> set:
    """Chunk ids with a 'UNITY: merge chunk <id>' commit on the project repo (cached 10s).
    ROOT_DIR is .unity, so the repo is its parent."""
    now = time.time()
    if now - _merged_cache["ts"] < 10:
        return _merged_cache["ids"]
    ids: set = set()
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT_DIR.parent), "log", "--grep=UNITY: merge chunk", "--pretty=%s"],
            capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            m = re.search(r"UNITY: merge chunk (\S+)", line)
            if m:
                ids.add(m.group(1))
    except Exception:
        pass
    _merged_cache.update(ts=now, ids=ids)
    return ids


_merged_cache: dict = {"ts": 0.0, "ids": set()}


def _chunk_status(chunk: dict, merged: set | None = None) -> str:
    """Auto-color a DAG node from every signal available, in fidelity order:
    Lean source inspection > merge commits / dag status > typed forum acts > recency."""
    cid = str(chunk.get("id", ""))
    # 1) highest fidelity: inspect the Lean declaration itself (v1-style fields, if present)
    lean_file = chunk.get("lean_file")
    lines_range = chunk.get("lean_decl_lines")
    if lean_file and lines_range:
        path = ROOT_DIR / lean_file
        if path.exists():
            try:
                lines = path.read_text().splitlines()
                start = max(0, lines_range[0] - 1)
                end = min(len(lines), lines_range[1])
                block = "\n".join(lines[start:end])
                if block.strip():
                    if not _SORRY_RE.search(block):
                        return "green"
                    if _ONLY_SORRY_RE.search(block):
                        return "red"
                    return "blue"
            except Exception:
                pass
    # 2) authoritative completion: merge commits or the chunk's own status field
    st = str(chunk.get("status") or "").lower()
    if (merged and cid in merged) or st in ("merged", "done", "complete", "completed"):
        return "green"
    if st in ("blocked", "failed"):
        return "red"
    if st in ("in_progress", "in-progress", "claimed", "active"):
        return "yellow"
    # 3) typed forum acts on the chunk's thread
    for tid in dict.fromkeys([cid, f"chunk-{cid}"]):
        thread_path = FORUM_DIR / f"{tid}.json"
        if not thread_path.exists():
            continue
        try:
            posts = json.loads(thread_path.read_text()).get("posts", [])
        except Exception:
            continue
        acts = [(p.get("act"), p.get("fields") or {}, p.get("timestamp", 0)) for p in posts]
        if any(a == "result" and f.get("build_ok") for a, f, _ in acts):
            return "green"
        if any(a == "obstacle" and f.get("status") == "open" for a, f, _ in acts):
            return "red"
        if any(a == "result" and f.get("status") == "partial" for a, f, _ in acts):
            return "blue"
        if any(a == "claim" and f.get("status") == "open" for a, f, _ in acts):
            return "yellow"
        if any(time.time() - ts < 300 for _, _, ts in acts):
            return "yellow"
    return "grey"


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/threads")
def list_threads():
    """Returns threads, active dimensions, pending proposals, tags, and leaderboard."""
    threads = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
            last = max((p["timestamp"] for p in data["posts"]), default=data["created_at"])
            threads.append({
                "thread_id": data["thread_id"],
                "title": data["title"],
                "description": data.get("description", ""),
                "post_count": len(data["posts"]),
                "last_activity": last,
                "pinned": data["thread_id"] == "_dimensions",
            })
        except Exception:
            continue
    # Pinned first, then by last_activity
    threads.sort(key=lambda t: (not t["pinned"], -t["last_activity"]))
    config = _load_config()
    balances_path = FORUM_DIR / "balances.json"
    leaderboard = []
    if balances_path.exists():
        try:
            balances = json.loads(balances_path.read_text())
            leaderboard = sorted(
                [{"author": a, "balance": r["balance"]} for a, r in balances.items()],
                key=lambda x: x["balance"], reverse=True,
            )
        except Exception:
            pass
    return JSONResponse({
        "threads": threads,
        "active_dimensions": config["dimensions"]["active"],
        "pending_dimensions": {
            name: {"description": p["description"], "proposed_by": p["proposed_by"]}
            for name, p in config["dimensions"]["pending"].items()
        },
        "tags": {
            name: {"description": t["description"], "post_count": len(t["post_ids"])}
            for name, t in config.get("tags", {}).items()
        },
        "leaderboard": leaderboard,
    })


@app.get("/api/threads/{thread_id}")
def get_thread(thread_id: str, sort: str = "hot"):
    data = _load_thread(thread_id)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    posts = data["posts"]
    for p in posts:
        p.setdefault("upvotes", 0)
        p.setdefault("downvotes", 0)
        p.setdefault("votes_by_dimension", {})
        p.setdefault("tags", [])
        if not isinstance(p.get("reply_to"), list):
            p["reply_to"] = [p["reply_to"]] if p.get("reply_to") else []
    config = _load_config()
    return JSONResponse({
        "thread_id": data["thread_id"],
        "title": data["title"],
        "description": data.get("description", ""),
        "post_count": len(posts),
        "active_dimensions": config["dimensions"]["active"],
        "posts": _sorted_posts(posts, sort),
    })


@app.get("/api/graph")
def get_graph():
    """All posts as nodes + reply edges. Used by the graph view."""
    thread_colors: dict[str, str] = {}
    nodes = []
    edges = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
            tid = data["thread_id"]
            if tid not in thread_colors:
                thread_colors[tid] = _GRAPH_PALETTE[len(thread_colors) % len(_GRAPH_PALETTE)]
            color = thread_colors[tid]
            for post in data["posts"]:
                reply_to = post.get("reply_to") or []
                if not isinstance(reply_to, list):
                    reply_to = [reply_to] if reply_to else []
                nodes.append({
                    "id": post["post_id"],
                    "author": post.get("author", "?"),
                    "content_preview": post.get("content", "")[:200],
                    "thread_id": tid,
                    "thread_title": data["title"],
                    "color": color,
                    "upvotes": post.get("upvotes", 0),
                    "downvotes": post.get("downvotes", 0),
                    "votes_by_dimension": post.get("votes_by_dimension", {}),
                    "tags": post.get("tags", []),
                    "timestamp": post["timestamp"],
                    "redacted": post.get("redacted", False),
                })
                for parent_id in reply_to:
                    edges.append({"id": post["post_id"] + "__" + parent_id,
                                  "source": post["post_id"], "target": parent_id})
        except Exception:
            continue
    return JSONResponse({"nodes": nodes, "edges": edges, "thread_colors": thread_colors})


@app.get("/api/tags/{name}")
def get_tag(name: str):
    config = _load_config()
    tags = config.get("tags", {})
    if name not in tags:
        return JSONResponse({"error": "tag not found"}, status_code=404)
    tag = tags[name]
    post_ids = set(tag["post_ids"])
    posts = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        if path.name in ("balances.json", "config.json"):
            continue
        try:
            data = json.loads(path.read_text())
            for post in data["posts"]:
                if post["post_id"] in post_ids:
                    posts.append({**post, "thread_id": data["thread_id"], "thread_title": data["title"]})
        except Exception:
            continue
    return JSONResponse({
        "tag": name,
        "description": tag["description"],
        "created_by": tag["created_by"],
        "posts": sorted(posts, key=_hot, reverse=True),
    })


@app.get("/api/dag")
def get_dag():
    dag_file = ROOT_DIR / "dag.json"
    if not dag_file.exists():
        return JSONResponse({"error": "dag.json not found"}, status_code=404)
    try:
        dag = json.loads(dag_file.read_text())
    except Exception:
        return JSONResponse({"error": "failed to parse dag.json"}, status_code=500)
    merged = _merged_chunk_ids()
    chunks = [{**c, "status": _chunk_status(c, merged)} for c in dag.get("chunks", [])]
    return JSONResponse({"chunks": chunks})


@app.get("/api/events")
async def events():
    async def generate():
        yield "data: connected\n\n"
        last_mtime = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                mtime = max(
                    (p.stat().st_mtime for p in FORUM_DIR.glob("*.json")),
                    default=0.0,
                )
                dag_file = ROOT_DIR / "dag.json"
                if dag_file.exists():
                    mtime = max(mtime, dag_file.stat().st_mtime)
                if mtime > last_mtime:
                    last_mtime = mtime
                    yield "data: update\n\n"
            except Exception:
                pass
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Forum HTML ────────────────────────────────────────────────────────────────


@app.get("/api/workspace")
def get_workspace():
    """Typed-workspace state: decisions, handoffs, per-chunk consensus, obstacles,
    questions, ledger, and act telemetry (Forum 2.0 view)."""
    decisions: dict = {}
    handoffs: list = []
    chunks: dict = {}
    questions: list = []
    ledger: list = []
    by_act: dict = {}
    for tp in sorted(FORUM_DIR.glob("*.json")):
        if tp.name.startswith("_") or tp.name in ("config.json", "balances.json"):
            continue
        try:
            data = json.loads(tp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        tid = data.get("thread_id", tp.stem)
        for post in data.get("posts", []):
            act = post.get("act")
            by_act[act or "note"] = by_act.get(act or "note", 0) + 1
            f = post.get("fields") or {}
            if act == "decision":
                decisions[f.get("topic", "?")] = {"topic": f.get("topic"), "choice": f.get("choice"),
                                                  "rationale": f.get("rationale", ""), "author": post["author"],
                                                  "ts": post["timestamp"]}
            elif act == "handoff":
                handoffs.append({"phase": f.get("phase"), "content": post["content"],
                                 "author": post["author"], "ts": post["timestamp"]})
            elif act in ("claim", "result", "obstacle") and tid.startswith("chunk-"):
                c = chunks.setdefault(tid, {"chunk": tid, "claims": [], "results": [], "obstacles": []})
                if act == "claim" and f.get("status") == "open":
                    c["claims"].append({"author": post["author"], "strategy": f.get("strategy", "")})
                elif act == "result":
                    open_obj = [o for o in f.get("objections", []) if o.get("status") == "open"]
                    c["results"].append({"id": post["post_id"], "author": post["author"],
                                         "status": f.get("status"), "build_ok": f.get("build_ok"),
                                         "endorsements": f.get("endorsements", []),
                                         "open_objections": open_obj,
                                         "mergeable": bool(f.get("endorsements")) and not open_obj})
                elif act == "obstacle" and f.get("status") == "open":
                    c["obstacles"].append({"author": post["author"], "content": post["content"]})
            elif act == "question" and f.get("status") == "open":
                questions.append({"id": post["post_id"], "author": post["author"], "to": f.get("to", ""),
                                  "chunk": f.get("chunk", ""), "content": post["content"]})
            elif act == "ledger":
                ledger.append({"author": post["author"], "kind": f.get("kind"), "title": f.get("title"),
                               "goal_shape": f.get("goal_shape", ""), "content": post["content"],
                               "ts": post["timestamp"]})
    return {"decisions": sorted(decisions.values(), key=lambda d: -d["ts"]),
            "handoffs": handoffs[-3:][::-1],
            "chunks": sorted(chunks.values(), key=lambda c: c["chunk"]),
            "questions": questions, "ledger": ledger[::-1][:20], "by_act": by_act}


FORUM_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>union</title>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-monospace, 'Cascadia Code', 'Fira Code', 'Menlo', monospace; font-size: 13px; background: #fafafa; color: #111; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 20px; border-bottom: 1px solid #e4e4e4; flex-shrink: 0; background: #fafafa; }
header h1 { font-size: 14px; font-weight: 600; letter-spacing: 0.14em; color: #111; }
nav { display: flex; gap: 16px; }
nav a { font-size: 12px; color: #888; text-decoration: none; transition: color 0.12s; }
nav a:hover { color: #111; }
.controls { display: flex; align-items: center; gap: 16px; }
#status { font-size: 11px; color: #bbb; }
.sort-tabs { display: flex; border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden; }
.sort-tabs button { background: none; border: none; border-left: 1px solid #e0e0e0; cursor: pointer; font: inherit; font-size: 12px; padding: 3px 12px; color: #666; transition: all 0.12s; }
.sort-tabs button:first-child { border-left: none; }
.sort-tabs button.active { background: #111; color: #fff; }
main { display: flex; flex: 1; overflow: hidden; }
#sidebar { width: 200px; border-right: 1px solid #e4e4e4; overflow-y: auto; flex-shrink: 0; background: #fafafa; }
.sidebar-section { font-size: 10px; letter-spacing: 0.1em; color: #bbb; padding: 10px 14px 5px; text-transform: uppercase; }
.thread-item { display: flex; justify-content: space-between; align-items: baseline; padding: 8px 14px; cursor: pointer; border-bottom: 1px solid #f0f0f0; gap: 8px; transition: background 0.1s; }
.thread-item:hover { background: #f2f2f2; }
.thread-item.active { background: #111; color: #fff; }
.thread-item.active .count { color: #888; }
.thread-item.pinned { background: #f0f4ff; border-left: 3px solid #4070e8; }
.thread-item.pinned.active { background: #4070e8; }
.thread-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.count { font-size: 11px; color: #bbb; flex-shrink: 0; }
.tag-sidebar-item { display: flex; justify-content: space-between; padding: 6px 14px; cursor: pointer; border-bottom: 1px solid #f4f4f4; font-size: 11px; color: #666; transition: background 0.1s; }
.tag-sidebar-item:hover { background: #f2f2f2; }
#panel { flex: 1; overflow-y: auto; padding: 22px 28px; background: #fff; }
.thread-title { font-weight: 600; margin-bottom: 4px; font-size: 14px; }
.thread-desc { color: #777; font-size: 12px; margin-bottom: 10px; }
.dim-bar { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
.dim-chip { font-size: 10px; background: #eef2ff; color: #4070e8; padding: 2px 8px; border-radius: 10px; }
.post { margin-bottom: 20px; padding-bottom: 20px; border-bottom: 1px solid #efefef; }
.post-meta { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; font-size: 11px; color: #999; }
.post-author { font-weight: 600; color: #111; font-size: 12px; }
.post-content { line-height: 1.65; font-size: 13px; }
.post-content p { margin-bottom: 0.6em; }
.post-content p:last-child { margin-bottom: 0; }
.post-content pre { background: #f4f4f6; padding: 9px 12px; overflow-x: auto; margin: 0.5em 0; border-radius: 4px; }
.post-content code { background: #f0f0f2; padding: 1px 5px; font-size: 12px; border-radius: 3px; }
.post-content pre code { background: none; padding: 0; }
.post-content ul, .post-content ol { padding-left: 1.4em; margin-bottom: 0.5em; }
.post-content blockquote { border-left: 2px solid #e0e0e0; margin: 0 0 0.5em; padding-left: 12px; color: #777; }
.post.redacted .post-content { color: #ccc; font-style: italic; white-space: pre-wrap; }
.mention { background: #eef2ff; color: #4070e8; padding: 1px 4px; border-radius: 3px; font-weight: 600; }
.post-id-link { color: #ddd; font-size: 11px; text-decoration: none; }
.post-id-link:hover { color: #999; }
.reply-to-link { color: #bbb; font-size: 11px; text-decoration: none; }
.reply-to-link:hover { color: #555; }
.post-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
.tag-chip { font-size: 10px; background: #fff8e8; color: #a06000; border: 1px solid #f0dfa0; padding: 2px 7px; border-radius: 10px; cursor: pointer; transition: background 0.1s; }
.tag-chip:hover { background: #ffeebb; }
.dim-inline { font-size: 10px; color: #bbb; white-space: nowrap; }
.dim-inline-name { color: #ccc; margin-right: 1px; }
.dim-inline .up { color: #16a34a; }
.dim-inline .down { color: #dc2626; }
.reply { margin-left: 20px; border-left: 2px solid #eeeeee; padding-left: 14px; border-bottom: none; margin-bottom: 10px; padding-bottom: 0; }
#placeholder { color: #ccc; padding: 24px 0; }
</style>
</head>
<body>
<header>
  <h1>union</h1>
  <div class="controls">
    <nav>
      <a href="/">← app</a>
      <a href="/workspace">workspace →</a>
      <a href="/graph">graph →</a>
      <a href="/dag">dag →</a>
    </nav>
    <span id="status">connecting...</span>
    <div class="sort-tabs">
      <button class="active" data-sort="hot">hot</button>
      <button data-sort="new">new</button>
      <button data-sort="top">top</button>
    </div>
  </div>
</header>
<main>
  <div id="sidebar"></div>
  <div id="panel"><div id="placeholder">select a thread</div></div>
</main>
<script>
marked.use({ breaks: true, gfm: true });

function renderContent(text) {
  const html = marked.parse(text);
  return html.replace(/@([\\w][\\w-]*)/g, '<span class="mention">@$1</span>');
}

let currentThread = null, currentSort = 'hot', activeDimensions = [];

document.querySelectorAll('.sort-tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    currentSort = btn.dataset.sort;
    document.querySelectorAll('.sort-tabs button').forEach(b => b.classList.toggle('active', b === btn));
    if (currentThread) loadThread(currentThread);
  });
});

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function reltime(ts) {
  const d = Math.floor(Date.now()/1000 - ts);
  if (d < 60) return d+'s'; if (d < 3600) return Math.floor(d/60)+'m';
  if (d < 86400) return Math.floor(d/3600)+'h'; return Math.floor(d/86400)+'d';
}

async function loadSidebar() {
  const res = await fetch('/api/threads'); if (!res.ok) return;
  const data = await res.json();
  activeDimensions = data.active_dimensions || [];
  const el = document.getElementById('sidebar'); el.innerHTML = '';

  if (data.threads.length) {
    el.innerHTML += '<div class="sidebar-section">threads</div>';
    data.threads.forEach(t => {
      const div = document.createElement('div');
      const cls = ['thread-item', t.thread_id === currentThread ? 'active' : '', t.pinned ? 'pinned' : ''].filter(Boolean).join(' ');
      div.className = cls;
      div.dataset.id = t.thread_id;
      div.innerHTML = '<span class="thread-name">'+(t.pinned ? '&#9650; ' : '')+esc(t.thread_id)+'</span><span class="count">'+t.post_count+'</span>';
      div.addEventListener('click', () => loadThread(t.thread_id));
      el.appendChild(div);
    });
  }

  const tagNames = Object.keys(data.tags || {});
  if (tagNames.length) {
    const sec = document.createElement('div');
    sec.className = 'sidebar-section'; sec.textContent = 'tags';
    el.appendChild(sec);
    tagNames.forEach(name => {
      const div = document.createElement('div');
      div.className = 'tag-sidebar-item';
      div.innerHTML = '<span>#'+esc(name)+'</span><span>'+data.tags[name].post_count+'</span>';
      div.addEventListener('click', () => loadTagView(name));
      el.appendChild(div);
    });
  }

  if (!currentThread && data.threads.length) loadThread(data.threads[0].thread_id);
}

function renderDimBreakdown(vbd) {
  if (!activeDimensions.length) return '';
  return activeDimensions.map(dim => {
    const counts = (vbd||{})[dim] || {up:0, down:0};
    const net = (counts.up||0) - (counts.down||0);
    return '<span class="dim-inline"><span class="dim-inline-name">'+esc(dim)+'</span>'
      +' <span class="up">&#8593;'+(counts.up||0)+'</span>'
      +'<span class="down">&#8595;'+(counts.down||0)+'</span>'
      +'</span>';
  }).join('');
}

function renderPost(p, depth) {
  const score = (p.upvotes||0) - (p.downvotes||0);
  const replyLinks = (p.reply_to||[]).map(id =>
    '<a class="reply-to-link" href="#post-'+id+'">&#8629; #'+id+'</a>'
  ).join(' ');
  const tags = (p.tags||[]).map(t =>
    '<span class="tag-chip" onclick="loadTagView(&#39;'+esc(t)+'&#39;)">'+esc(t)+'</span>'
  ).join('');
  const content = p.redacted
    ? '<div class="post-content">'+esc(p.content)+'</div>'
    : '<div class="post-content">'+renderContent(p.content)+'</div>';
  const dimBreak = renderDimBreakdown(p.votes_by_dimension);
  const tagsHtml = tags ? '<div class="post-tags">'+tags+'</div>' : '';
  return '<div class="post'+(depth>0?' reply':'')+(p.redacted?' redacted':'')+'" id="post-'+p.post_id+'">'
    +'<div class="post-meta"><span class="post-author">'+esc(p.author)+'</span>'
    +replyLinks
    +'<span>&#8593;'+(p.upvotes||0)+' &#8595;'+(p.downvotes||0)+' ('+(score>=0?'+':'')+score+')</span>'
    +dimBreak
    +'<span>'+reltime(p.timestamp)+' ago</span>'
    +'<a class="post-id-link" href="#post-'+p.post_id+'">#'+p.post_id+'</a></div>'
    +content
    +tagsHtml
    +(p._replies||[]).map(r=>renderPost(r,depth+1)).join('')+'</div>';
}

async function loadThread(id) {
  currentThread = id;
  document.querySelectorAll('.thread-item').forEach(el => el.classList.toggle('active', el.dataset.id === id));
  const res = await fetch('/api/threads/'+encodeURIComponent(id)+'?sort='+currentSort);
  if (!res.ok) return;
  const data = await res.json();
  const byId = {}, roots = [];
  data.posts.forEach(p => { byId[p.post_id] = p; p._replies = []; });
  data.posts.forEach(p => {
    const parents = p.reply_to || [];
    const firstKnown = parents.find(pid => byId[pid]);
    if (firstKnown) byId[firstKnown]._replies.push(p);
    else roots.push(p);
  });
  activeDimensions = data.active_dimensions || [];
  const panel = document.getElementById('panel');
  panel.innerHTML = '<div class="thread-title">'+esc(data.title)+'</div>'
    +(data.description?'<div class="thread-desc">'+esc(data.description)+'</div>':'')
    +(roots.length===0?'<div id="placeholder">no posts yet</div>':roots.map(p=>renderPost(p,0)).join(''));
  if (location.hash) {
    const el = document.querySelector(location.hash);
    if (el) el.scrollIntoView({ behavior: 'smooth' });
  }
}

async function loadTagView(name) {
  currentThread = null;
  document.querySelectorAll('.thread-item').forEach(el => el.classList.remove('active'));
  const res = await fetch('/api/tags/'+encodeURIComponent(name));
  if (!res.ok) return;
  const data = await res.json();
  const panel = document.getElementById('panel');
  panel.innerHTML = '<div class="thread-title">#'+esc(data.tag)+'</div>'
    +(data.description?'<div class="thread-desc">'+esc(data.description)+'</div>':'')
    +(data.posts.length===0?'<div id="placeholder">no posts tagged</div>'
      : data.posts.map(p => renderPost({...p, reply_to: p.reply_to||[]}, 0)).join(''));
}

const hash = location.hash.slice(1);
if (hash) { currentThread = hash; }
loadSidebar();
document.getElementById('status').textContent = 'live';
setInterval(() => { loadSidebar(); if (currentThread) loadThread(currentThread); }, 2000);
</script>
</body>
</html>
"""


# ── Graph HTML ────────────────────────────────────────────────────────────────

GRAPH_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>union · graph</title>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-monospace, 'Cascadia Code', 'Fira Code', 'Menlo', monospace; font-size: 13px; background: #fafafa; color: #111; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 20px; border-bottom: 1px solid #e4e4e4; flex-shrink: 0; background: #fafafa; }
header h1 { font-size: 14px; font-weight: 600; letter-spacing: 0.14em; }
nav { display: flex; gap: 16px; }
nav a { font-size: 12px; color: #888; text-decoration: none; transition: color 0.12s; }
nav a:hover { color: #111; }
.controls { display: flex; align-items: center; gap: 14px; }
#status { font-size: 11px; color: #bbb; }
.color-tabs { display: flex; border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden; }
.color-tabs button { background: none; border: none; border-left: 1px solid #e0e0e0; cursor: pointer; font: inherit; font-size: 12px; padding: 3px 12px; color: #666; transition: all 0.12s; }
.color-tabs button:first-child { border-left: none; }
.color-tabs button.active { background: #111; color: #fff; }
main { display: flex; flex: 1; overflow: hidden; position: relative; }
#cy { flex: 1; }
#info-panel { position: absolute; right: 0; top: 0; bottom: 0; width: 300px; border-left: 1px solid #e4e4e4; overflow-y: auto; padding: 18px; background: #fff; display: none; z-index: 10; }
#info-panel.visible { display: block; }
#info-close { float: right; cursor: pointer; font-size: 15px; line-height: 1; color: #bbb; transition: color 0.1s; }
#info-close:hover { color: #111; }
.info-field { margin-bottom: 12px; }
.info-label { font-size: 10px; color: #bbb; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 3px; }
.info-value { line-height: 1.5; color: #111; }
.info-value.md { font-size: 12px; }
.info-value.md p { margin-bottom: 0.4em; }
.info-value.md code { background: #f0f0f2; padding: 1px 4px; font-size: 11px; border-radius: 3px; }
.info-value.md pre { background: #f4f4f6; padding: 7px 10px; overflow-x: auto; font-size: 11px; margin: 0.3em 0; border-radius: 4px; }
.info-value.md pre code { background: none; padding: 0; }
.info-link { color: #4070e8; text-decoration: none; }
.info-link:hover { text-decoration: underline; }
.tag-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.tag-chip { font-size: 10px; background: #fff8e8; color: #a06000; border: 1px solid #f0dfa0; padding: 2px 7px; border-radius: 10px; }
.dim-row { display: flex; gap: 8px; font-size: 11px; margin-bottom: 2px; }
.dim-name { color: #888; min-width: 110px; }
.dim-up { color: #16a34a; }
.dim-down { color: #dc2626; }
#legend { position: absolute; bottom: 14px; left: 14px; background: rgba(255,255,255,0.97); border: 1px solid #e4e4e4; padding: 9px 14px; font-size: 11px; max-width: 190px; pointer-events: none; border-radius: 6px; }
#legend-title { font-weight: 600; margin-bottom: 5px; color: #999; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
.legend-row { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; overflow: hidden; }
.legend-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.legend-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #555; }
</style>
</head>
<body>
<header>
  <h1>union</h1>
  <div class="controls">
    <nav>
      <a href="/">← app</a>
      <a href="/forum">forum →</a>
      <a href="/workspace">workspace →</a>
      <a href="/dag">dag →</a>
    </nav>
    <span id="status">loading...</span>
    <div class="color-tabs">
      <button class="active" data-mode="thread">by thread</button>
      <button data-mode="tag">by tag</button>
    </div>
  </div>
</header>
<main>
  <div id="cy"></div>
  <div id="info-panel">
    <span id="info-close" onclick="closePanel()">&#x2715;</span>
    <div id="info-content"></div>
  </div>
  <div id="legend"><div id="legend-title">threads</div><div id="legend-rows"></div></div>
</main>
<script>
marked.use({ breaks: true, gfm: true });
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function reltime(ts) {
  const d = Math.floor(Date.now()/1000 - ts);
  if (d < 60) return d+'s'; if (d < 3600) return Math.floor(d/60)+'m';
  if (d < 86400) return Math.floor(d/3600)+'h'; return Math.floor(d/86400)+'d';
}

const TAG_PALETTE = ['#e07b39','#7b61c4','#2d9e6b','#c4416a','#3d7fc4','#9e882d','#4ab0b0','#c46e2d'];
let cy = null, colorMode = 'thread', graphData = null, tagColors = {};
let nodePositions = {};  // id -> {x,y}, persisted across reloads
let lastDataSig = '';    // "nodeCount,edgeCount" — skip rebuild when unchanged

document.querySelectorAll('.color-tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    colorMode = btn.dataset.mode;
    document.querySelectorAll('.color-tabs button').forEach(b => b.classList.toggle('active', b === btn));
    if (graphData) recolor();
  });
});

function buildTagColors(nodes) {
  tagColors = {};
  nodes.forEach(n => (n.tags||[]).forEach(t => {
    if (!(t in tagColors)) tagColors[t] = TAG_PALETTE[Object.keys(tagColors).length % TAG_PALETTE.length];
  }));
}

function nodeColor(n) {
  if (colorMode === 'tag') {
    const firstTag = (n.tags||[])[0];
    return firstTag ? (tagColors[firstTag]||'#cccccc') : '#dddddd';
  }
  return n.color || '#cccccc';
}

function buildLegend() {
  const title = document.getElementById('legend-title');
  const rows = document.getElementById('legend-rows');
  rows.innerHTML = '';
  if (colorMode === 'thread') {
    title.textContent = 'threads';
    Object.entries(graphData.thread_colors).forEach(([tid, col]) => {
      rows.innerHTML += '<div class="legend-row"><span class="legend-dot" style="background:'+col+'"></span><span class="legend-label">'+esc(tid)+'</span></div>';
    });
  } else {
    title.textContent = 'tags';
    if (!Object.keys(tagColors).length) {
      rows.innerHTML = '<div style="color:#aaa">no tags yet</div>';
    } else {
      Object.entries(tagColors).forEach(([tag, col]) => {
        rows.innerHTML += '<div class="legend-row"><span class="legend-dot" style="background:'+col+'"></span><span class="legend-label">#'+esc(tag)+'</span></div>';
      });
      rows.innerHTML += '<div class="legend-row"><span class="legend-dot" style="background:#dddddd"></span><span class="legend-label">untagged</span></div>';
    }
  }
}

function recolor() {
  if (!cy) return;
  const nodeMap = {};
  graphData.nodes.forEach(n => { nodeMap[n.id] = n; });
  cy.nodes().forEach(node => {
    const n = nodeMap[node.id()];
    if (!n) return;
    const col = nodeColor(n);
    node.style('background-color', col);
  });
  buildLegend();
}

// Spring simulation on the thread-level graph.
// Inter-thread edge count -> attraction; ideal distance ∝ 1/√count so heavily
// connected threads end up closer. Unconnected threads only repel each other.
// Returns { threadId: {x, y} } for every thread present in data.
function computeThreadLayout(data) {
  const allThreadIds = [...new Set(data.nodes.map(n => n.thread_id))];
  if (allThreadIds.length === 0) return {};

  // Count cross-thread edges
  const nodeThread = {};
  data.nodes.forEach(n => { nodeThread[n.id] = n.thread_id; });
  const interCount = {};
  data.edges.forEach(e => {
    const t1 = nodeThread[e.source], t2 = nodeThread[e.target];
    if (t1 && t2 && t1 !== t2) {
      const key = [t1, t2].sort().join('\\x00');
      interCount[key] = (interCount[key] || 0) + 1;
    }
  });

  // Initialise on a circle
  const pos = {};
  allThreadIds.forEach((tid, i) => {
    const a = (2 * Math.PI * i) / allThreadIds.length;
    pos[tid] = { x: Math.cos(a) * 400 + 400, y: Math.sin(a) * 400 + 300 };
  });

  // 300 relaxation steps
  const BASE_DIST = 450;
  for (let iter = 0; iter < 300; iter++) {
    const f = {};
    allThreadIds.forEach(t => { f[t] = { x: 0, y: 0 }; });

    // Attraction: inter-thread edges pull threads together
    Object.entries(interCount).forEach(([key, cnt]) => {
      const sep = key.split('\\x00');
      const t1 = sep[0], t2 = sep[1];
      const dx = pos[t2].x - pos[t1].x, dy = pos[t2].y - pos[t1].y;
      const dist = Math.sqrt(dx*dx + dy*dy) || 1;
      const ideal = BASE_DIST / Math.sqrt(cnt);   // more edges → shorter ideal
      const k = 0.02 * (dist - ideal) / dist;
      f[t1].x += k*dx; f[t1].y += k*dy;
      f[t2].x -= k*dx; f[t2].y -= k*dy;
    });

    // Repulsion: all thread pairs push apart
    for (let i = 0; i < allThreadIds.length; i++) {
      for (let j = i + 1; j < allThreadIds.length; j++) {
        const t1 = allThreadIds[i], t2 = allThreadIds[j];
        const dx = pos[t2].x - pos[t1].x, dy = pos[t2].y - pos[t1].y;
        const d2 = dx*dx + dy*dy || 1, d = Math.sqrt(d2);
        const rep = 25000 / (d2 * d);
        f[t1].x -= rep*dx; f[t1].y -= rep*dy;
        f[t2].x += rep*dx; f[t2].y += rep*dy;
      }
    }

    allThreadIds.forEach(t => { pos[t].x += f[t].x; pos[t].y += f[t].y; });
  }

  return pos;
}

function buildGraph(data) {
  graphData = data;
  buildTagColors(data.nodes);
  const nodeIds = new Set(data.nodes.map(n => n.id));
  const elements = [];

  // Centroid of already-placed nodes per thread (for existing threads)
  const threadCentroids = {};
  data.nodes.forEach(n => {
    if (nodePositions[n.id]) {
      const c = threadCentroids[n.thread_id] || (threadCentroids[n.thread_id] = { x: 0, y: 0, count: 0 });
      c.x += nodePositions[n.id].x; c.y += nodePositions[n.id].y; c.count++;
    }
  });
  Object.values(threadCentroids).forEach(c => { c.x /= c.count; c.y /= c.count; });

  // For brand-new threads (no saved nodes), use the inter-thread spring layout
  // so heavily cross-linked threads start near each other
  const allThreadIds = [...new Set(data.nodes.map(n => n.thread_id))];
  const hasNewThreads = allThreadIds.some(t => !threadCentroids[t]);
  if (hasNewThreads) {
    const springPos = computeThreadLayout(data);
    allThreadIds.forEach(tid => {
      if (!threadCentroids[tid] && springPos[tid]) threadCentroids[tid] = springPos[tid];
    });
  }

  data.nodes.forEach(n => {
    const elem = { data: {
      id: n.id,
      label: (n.redacted ? '[REDACTED]' : n.content_preview.substring(0,20)).replace(/\\n/g,' '),
      author: n.author,
      thread_id: n.thread_id,
      bgColor: nodeColor(n),
    }};
    if (nodePositions[n.id]) {
      // Existing node: restore exact position so it doesn't move
      elem.position = nodePositions[n.id];
    } else {
      // New node: start near its thread centroid so cose keeps it in the cluster
      const c = threadCentroids[n.thread_id];
      elem.position = { x: c.x + (Math.random() - 0.5) * 80, y: c.y + (Math.random() - 0.5) * 80 };
    }
    elements.push(elem);
  });

  data.edges.forEach(e => {
    if (nodeIds.has(e.source) && nodeIds.has(e.target)) {
      elements.push({ data: { id: e.id, source: e.source, target: e.target }});
    }
  });

  if (cy) cy.destroy();
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements,
    style: [
      { selector: 'node', style: {
        'background-color': 'data(bgColor)',
        'border-color': 'rgba(0,0,0,0.13)',
        'border-width': 1,
        'label': 'data(label)',
        'font-family': 'ui-monospace, "Cascadia Code", "Fira Code", "Menlo", monospace',
        'font-size': 10,
        'text-valign': 'center',
        'text-halign': 'center',
        'text-wrap': 'ellipsis',
        'text-max-width': 74,
        'width': 88,
        'height': 30,
        'shape': 'roundrectangle',
        'color': '#111',
        'min-zoomed-font-size': 7,
      }},
      { selector: 'node:selected', style: { 'border-width': 2, 'border-color': 'rgba(51,51,51,0.5)' }},
      { selector: 'node:active', style: { 'overlay-opacity': 0.07 }},
      { selector: 'node[?redacted]', style: { 'opacity': 0.35 }},
      { selector: 'edge', style: {
        'curve-style': 'bezier',
        'target-arrow-shape': 'triangle',
        'target-arrow-color': '#ccc',
        'line-color': '#ccc',
        'arrow-scale': 0.65,
        'width': 1,
      }},
    ],
    // randomize: false + pre-set positions = existing nodes barely move, new ones land near their thread
    layout: { name: 'cose', animate: false, nodeRepulsion: 4096, idealEdgeLength: 60, gravity: 1, padding: 40, randomize: false },
  });

  // Persist final positions so next rebuild preserves them
  cy.nodes().forEach(node => {
    nodePositions[node.id()] = { x: node.position('x'), y: node.position('y') };
  });

  cy.on('tap', 'node', e => { e.stopPropagation(); showPanel(e.target.id()); });
  cy.on('tap', e => { if (e.target === cy) closePanel(); });
  cy.on('mouseover', 'node', e => { e.target.style('border-width', 2); });
  cy.on('mouseout', 'node', e => { e.target.style('border-width', e.target.selected() ? 2.5 : 1); });
  buildLegend();
  document.getElementById('status').textContent = data.nodes.length+' posts';
}

const nodeDataMap = {};
function showPanel(id) {
  if (!graphData) return;
  const n = graphData.nodes.find(x => x.id === id);
  if (!n) return;
  nodeDataMap[id] = n;
  const score = n.upvotes - n.downvotes;
  const dimRows = Object.entries(n.votes_by_dimension||{}).map(([dim, c]) => {
    const net = (c.up||0)-(c.down||0);
    return '<div class="dim-row"><span class="dim-name">'+esc(dim)+'</span>'
      +'<span class="dim-up">&#8593;'+(c.up||0)+'</span> '
      +'<span class="dim-down">&#8595;'+(c.down||0)+'</span> '
      +'('+(net>=0?'+':'')+net+')</div>';
  }).join('');
  const tags = (n.tags||[]).map(t=>'<span class="tag-chip">#'+esc(t)+'</span>').join('');
  const contentHtml = n.redacted ? '<em style="color:#bbb">[redacted]</em>' : marked.parse(n.content_preview+(n.content_preview.length>=200?'…':''));
  document.getElementById('info-content').innerHTML =
    '<div class="info-field"><div class="info-label">author</div><div class="info-value">'+esc(n.author)+'</div></div>'
    +'<div class="info-field"><div class="info-label">thread</div><div class="info-value">'+esc(n.thread_title)+' <span style="color:#aaa">('+esc(n.thread_id)+')</span></div></div>'
    +'<div class="info-field"><div class="info-label">'+reltime(n.timestamp)+' ago &middot; &#8593;'+n.upvotes+' &#8595;'+n.downvotes+' ('+(score>=0?'+':'')+score+')</div></div>'
    +(dimRows?'<div class="info-field"><div class="info-label">by dimension</div>'+dimRows+'</div>':'')
    +(tags?'<div class="info-field"><div class="info-label">tags</div><div class="tag-chips">'+tags+'</div></div>':'')
    +'<div class="info-field"><div class="info-label">content</div><div class="info-value md">'+contentHtml+'</div></div>'
    +'<div class="info-field"><a class="info-link" href="/#post-'+esc(n.id)+'" target="_blank">view in forum &rarr;</a></div>';
  document.getElementById('info-panel').classList.add('visible');
}

function closePanel() {
  document.getElementById('info-panel').classList.remove('visible');
  if (cy) cy.$(':selected').unselect();
}

async function load() {
  const res = await fetch('/api/graph');
  if (!res.ok) { document.getElementById('status').textContent = 'error'; return; }
  const data = await res.json();
  const sig = data.nodes.length + ',' + data.edges.length;
  if (sig === lastDataSig) return;  // nothing changed, don't rebuild
  lastDataSig = sig;
  buildGraph(data);
}

load();
setInterval(load, 2000);
</script>
</body>
</html>
"""


# ── DAG HTML ──────────────────────────────────────────────────────────────────

DAG_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>union · dag</title>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-monospace, 'Cascadia Code', 'Fira Code', 'Menlo', monospace; font-size: 13px; background: #fafafa; color: #111; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 20px; border-bottom: 1px solid #e4e4e4; flex-shrink: 0; background: #fafafa; }
header h1 { font-size: 14px; font-weight: 600; letter-spacing: 0.14em; }
nav { display: flex; gap: 16px; }
nav a { font-size: 12px; color: #888; text-decoration: none; transition: color 0.12s; }
nav a:hover { color: #111; }
.controls { display: flex; align-items: center; gap: 16px; }
#status { font-size: 11px; color: #bbb; }
main { display: flex; flex: 1; overflow: hidden; position: relative; }
#cy { flex: 1; }
#info-panel { position: absolute; right: 0; top: 0; bottom: 0; width: 280px; border-left: 1px solid #e4e4e4; overflow-y: auto; padding: 18px; background: #fff; display: none; z-index: 10; }
#info-panel.visible { display: block; }
#info-close { float: right; cursor: pointer; font-size: 15px; line-height: 1; color: #bbb; transition: color 0.1s; }
#info-close:hover { color: #111; }
.info-field { margin-bottom: 12px; }
.info-label { font-size: 10px; color: #bbb; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 3px; }
.info-value { white-space: pre-wrap; line-height: 1.5; color: #111; }
.info-link { color: #4070e8; text-decoration: none; }
.info-link:hover { text-decoration: underline; }
.status-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
#legend { position: absolute; bottom: 14px; left: 14px; background: rgba(255,255,255,0.97); border: 1px solid #e4e4e4; padding: 9px 14px; font-size: 11px; line-height: 2; pointer-events: none; border-radius: 6px; }
.legend-row { display: flex; align-items: center; gap: 6px; color: #555; }
#waiting { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); color: #bbb; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>union</h1>
  <div class="controls">
    <nav>
      <a href="/">← app</a>
      <a href="/forum">forum →</a>
      <a href="/workspace">workspace →</a>
      <a href="/graph">graph →</a>
    </nav>
    <span id="status">connecting...</span>
  </div>
</header>
<main>
  <div id="cy"></div>
  <div id="info-panel">
    <span id="info-close" onclick="closePanel()">&#x2715;</span>
    <div id="info-content"></div>
  </div>
  <div id="legend">
    <div class="legend-row"><span class="status-dot" style="background:#e8e8e8;border:1px solid #999"></span>pending</div>
    <div class="legend-row"><span class="status-dot" style="background:#fff3a0;border:1px solid #c8a000"></span>claimed / active</div>
    <div class="legend-row"><span class="status-dot" style="background:#c8f0c8;border:1px solid #2d7a2d"></span>merged / builds sorry-free</div>
    <div class="legend-row"><span class="status-dot" style="background:#c8e0f8;border:1px solid #1a5fa0"></span>partial result</div>
    <div class="legend-row"><span class="status-dot" style="background:#f8c8c8;border:1px solid #c02020"></span>blocked / open obstacle</div>
  </div>
  <div id="waiting" style="display:none">waiting for dag.json...</div>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<script>
cytoscape.use(cytoscapeDagre);

const STATUS_COLOR = {
  grey:   { bg: '#e8e8e8', border: '#999999' },
  yellow: { bg: '#fff3a0', border: '#c8a000' },
  green:  { bg: '#c8f0c8', border: '#2d7a2d' },
  blue:   { bg: '#c8e0f8', border: '#1a5fa0' },
  red:    { bg: '#f8c8c8', border: '#c02020' },
};

let cy = null, chunks = {};
let nodePositions = {};  // id -> {x,y}, persisted across rebuilds
let lastSig = '';        // sorted chunk-ids, skip rebuild when unchanged

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function buildGraph(data) {
  if (cy) {
    cy.nodes().forEach(n => { nodePositions[n.id()] = { x: n.position('x'), y: n.position('y') }; });
    cy.destroy();
  }
  chunks = {};
  data.chunks.forEach(c => { chunks[c.id] = c; });
  const elements = [];
  const nodeIds = new Set(data.chunks.map(c => c.id));
  data.chunks.forEach(c => {
    const col = STATUS_COLOR[c.status] || STATUS_COLOR.grey;
    const elem = { data: {
      id: c.id,
      label: c.id + (c.title && c.title !== c.id ? '\\n' + c.title.substring(0,24) : ''),
      status: c.status,
      bgColor: col.bg,
      borderColor: col.border,
    }};
    if (nodePositions[c.id]) elem.position = nodePositions[c.id];
    elements.push(elem);
    (Array.isArray(c.dependencies) ? c.dependencies : (c.dependencies?.local || [])).forEach(dep => {
      if (nodeIds.has(dep)) {
        elements.push({ data: { id: dep+'->'+c.id, source: dep, target: c.id }});
      }
    });
  });
  const allPositionsKnown = data.chunks.length > 0 && data.chunks.every(c => nodePositions[c.id]);
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements,
    style: [
      { selector: 'node', style: {
        'background-color': 'data(bgColor)', 'border-color': 'data(borderColor)', 'border-width': 1.5,
        'label': 'data(label)', 'font-family': 'ui-monospace, "Cascadia Code", "Fira Code", "Menlo", monospace', 'font-size': '11px',
        'text-valign': 'center', 'text-halign': 'center', 'text-wrap': 'wrap',
        'shape': 'roundrectangle', 'color': '#111', 'padding': '10px 14px',
        'min-zoomed-font-size': 7,
      }},
      { selector: 'node:selected', style: { 'border-width': 2.5, 'border-color': 'rgba(51,51,51,0.6)' }},
      { selector: 'edge', style: {
        'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
        'target-arrow-color': '#ccc', 'line-color': '#ccc', 'arrow-scale': 0.75, 'width': 1,
      }},
    ],
    layout: allPositionsKnown
      ? { name: 'preset' }
      : { name: 'dagre', rankDir: 'TB', nodeSep: 40, rankSep: 70, padding: 30 },
  });
  cy.nodes().forEach(n => { nodePositions[n.id()] = { x: n.position('x'), y: n.position('y') }; });
  cy.on('tap', 'node', e => { e.stopPropagation(); showPanel(e.target.id()); });
  cy.on('tap', e => { if (e.target === cy) closePanel(); });
  cy.on('mouseover', 'node', e => { e.target.style('border-width', 2.5); });
  cy.on('mouseout', 'node', e => { e.target.style('border-width', e.target.selected() ? 2.5 : 1.5); });
}

function updateColors(data) {
  if (!cy) return;
  data.chunks.forEach(c => {
    const node = cy.getElementById(c.id); if (!node.length) return;
    const col = STATUS_COLOR[c.status] || STATUS_COLOR.grey;
    node.style({ 'background-color': col.bg, 'border-color': col.border });
    node.data({ status: c.status, bgColor: col.bg, borderColor: col.border });
    chunks[c.id] = c;
  });
  if (openId) showPanel(openId);
}

let openId = null;
function showPanel(id) {
  openId = id;
  const c = chunks[id]; if (!c) return;
  const col = STATUS_COLOR[c.status] || STATUS_COLOR.grey;
  const statusLabel = { grey:'not started', yellow:'in progress', green:'fully formalized', blue:'partially formalized', red:'by sorry' }[c.status] || c.status;
  document.getElementById('info-content').innerHTML =
    '<div class="info-field"><div class="info-label">chunk</div><div class="info-value">'+esc(c.id)+'</div></div>'
    +'<div class="info-field"><div class="info-label">title</div><div class="info-value">'+esc(c.title||'—')+'</div></div>'
    +'<div class="info-field"><div class="info-label">type</div><div class="info-value">'+esc(c.type||'—')+'</div></div>'
    +'<div class="info-field"><div class="info-label">declarations</div><div class="info-value">'+esc((c.declarations||[]).join(', ')||'—')+'</div></div>'
    +'<div class="info-field"><div class="info-label">summary</div><div class="info-value">'+esc(c.summary||'—')+'</div></div>'
    +'<div class="info-field"><div class="info-label">status</div><div class="info-value"><span class="status-dot" style="background:'+col.bg+';border:1px solid '+col.border+'"></span>'+esc(statusLabel)+'</div></div>'
    +'<div class="info-field"><div class="info-label">lean file</div><div class="info-value">'+esc(c.lean_file||'—')+(c.lean_decl_lines?' : '+c.lean_decl_lines[0]+'–'+c.lean_decl_lines[1]:'')+'</div></div>'
    +'<div class="info-field"><div class="info-label">dependencies</div><div class="info-value">'+esc(((Array.isArray(c.dependencies)?c.dependencies:(c.dependencies?.local||[]))).join(', ')||'none')+'</div></div>'
    +'<div class="info-field"><div class="info-label">forum</div><div class="info-value"><a class="info-link" href="/#'+esc(c.id)+'" target="_blank">'+esc(c.id)+' &rarr;</a></div></div>';
  document.getElementById('info-panel').classList.add('visible');
}

function closePanel() {
  openId = null;
  document.getElementById('info-panel').classList.remove('visible');
  if (cy) cy.$(':selected').unselect();
}

async function loadDag(forceRebuild) {
  const res = await fetch('/api/dag');
  const waiting = document.getElementById('waiting');
  if (!res.ok) { waiting.style.display='block'; return; }
  waiting.style.display = 'none';
  const data = await res.json();
  const sig = (data.chunks||[]).map(c=>c.id).sort().join(',');
  if (forceRebuild || sig !== lastSig) { buildGraph(data); lastSig = sig; }
  else updateColors(data);
}

function connect() {
  const es = new EventSource('/api/events');
  es.onopen = () => { document.getElementById('status').textContent = 'live'; };
  es.onmessage = () => { loadDag(false); };
  es.onerror = () => { document.getElementById('status').textContent = 'reconnecting...'; es.close(); setTimeout(connect, 3000); };
}

loadDag(true); connect();
</script>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────────────────────



@app.get("/graph", response_class=HTMLResponse)
def graph():
    return _embeddable(GRAPH_HTML)


@app.get("/dag", response_class=HTMLResponse)
def dag():
    return _embeddable(DAG_HTML)


WORKSPACE_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>union — workspace</title>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-monospace, 'Cascadia Code', 'Fira Code', 'Menlo', monospace; font-size: 13px; background: #fafafa; color: #111; }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 20px; border-bottom: 1px solid #e4e4e4; background: #fafafa; position: sticky; top: 0; }
header h1 { font-size: 14px; font-weight: 600; letter-spacing: 0.14em; }
nav { display: flex; gap: 16px; }
nav a { font-size: 12px; color: #888; text-decoration: none; }
nav a:hover { color: #111; }
#status { font-size: 11px; color: #bbb; }
main { padding: 18px 20px; display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; align-items: start; }
section { background: #fff; border: 1px solid #e4e4e4; border-radius: 6px; padding: 12px 14px; min-width: 0; overflow-wrap: anywhere; }
section h2 { font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: #bbb; margin-bottom: 8px; }
.item { padding: 6px 0; border-top: 1px solid #f2f2f2; line-height: 1.45; overflow-wrap: anywhere; word-break: break-word; }
.item:first-of-type { border-top: none; }
.who { color: #999; font-size: 11px; }
.chunk-card { margin-bottom: 10px; }
.chunk-name { font-weight: 600; font-size: 12px; }
.badge { display: inline-block; font-size: 10px; border-radius: 4px; padding: 1px 6px; margin-left: 6px; }
.ok { background: #e8f5e9; color: #1b5e20; }
.blocked { background: #ffebee; color: #b71c1c; }
.pending { background: #f5f5f5; color: #888; }
.kind { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: #888; margin-right: 6px; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; }
.stat { border: 1px solid #eee; border-radius: 4px; padding: 2px 8px; font-size: 11px; color: #666; }
.empty { color: #ccc; padding: 8px 0; }
</style>
</head>
<body>
<header>
  <h1>union — workspace</h1>
  <div style="display:flex;gap:16px;align-items:center">
    <nav>
      <a href="/">← app</a>
      <a href="/forum">forum →</a>
      <a href="/graph">graph →</a>
      <a href="/dag">dag →</a>
    </nav>
    <span id="status">connecting...</span>
  </div>
</header>
<main id="main"></main>
<script>
const esc = t => (t || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function badge(r) {
  if (r.mergeable) return '<span class="badge ok">mergeable</span>';
  if (r.open_objections.length) return '<span class="badge blocked">' + r.open_objections.length + ' objection(s)</span>';
  return '<span class="badge pending">needs endorsement</span>';
}
async function refresh() {
  try {
    const d = await (await fetch('/api/workspace')).json();
    document.getElementById('status').textContent = 'live';
    let h = '';
    h += '<section><h2>Binding decisions</h2>' + (d.decisions.length ? d.decisions.map(x =>
      '<div class="item"><b>' + esc(x.topic) + '</b>: ' + esc(x.choice) +
      (x.rationale ? ' — <span class="who">' + esc(x.rationale) + '</span>' : '') +
      ' <span class="who">(' + esc(x.author) + ')</span></div>').join('') : '<div class="empty">none yet</div>') + '</section>';
    h += '<section><h2>Chunk consensus</h2>' + (d.chunks.length ? d.chunks.map(c =>
      '<div class="chunk-card"><span class="chunk-name">' + esc(c.chunk) + '</span>' +
      c.results.map(r => '<div class="item">' + esc(r.author) + ': ' + esc(r.status) +
        (r.build_ok ? ' ✓build' : '') + badge(r) +
        (r.endorsements.length ? ' <span class="who">endorsed by ' + esc(r.endorsements.join(', ')) + '</span>' : '') +
        r.open_objections.map(o => '<div class="who">⛔ ' + esc(o.by) + ': ' + esc(o.reason) + '</div>').join('') +
        '</div>').join('') +
      (c.claims.length ? '<div class="item who">open claims: ' + c.claims.map(cl => esc(cl.author) +
        (cl.strategy ? ' (' + esc(cl.strategy) + ')' : '')).join(', ') + '</div>' : '') +
      '</div>').join('') : '<div class="empty">no typed chunk activity yet</div>') + '</section>';
    h += '<section><h2>Open obstacles</h2>' + (d.chunks.some(c => c.obstacles.length) ?
      d.chunks.flatMap(c => c.obstacles.map(o => '<div class="item">' + esc(o.content) +
      ' <span class="who">(' + esc(o.author) + ')</span></div>')).join('') : '<div class="empty">none open</div>') + '</section>';
    h += '<section><h2>Open questions</h2>' + (d.questions.length ? d.questions.map(q =>
      '<div class="item">' + esc(q.content) + ' <span class="who">(' + esc(q.author) + ')</span></div>').join('')
      : '<div class="empty">none open</div>') + '</section>';
    h += '<section><h2>Ledger</h2>' + (d.ledger.length ? d.ledger.map(l =>
      '<div class="item"><span class="kind">' + esc(l.kind) + '</span>' + esc(l.content) +
      (l.goal_shape ? ' <span class="who">[' + esc(l.goal_shape) + ']</span>' : '') + '</div>').join('')
      : '<div class="empty">no verified knowledge yet</div>') + '</section>';
    h += '<section><h2>Latest handoffs</h2>' + (d.handoffs.length ? d.handoffs.map(x =>
      '<div class="item">' + esc(x.content) + '</div>').join('') : '<div class="empty">none yet</div>') + '</section>';
    h += '<section><h2>Telemetry (posts by act)</h2><div class="stats">' +
      Object.entries(d.by_act).map(([k, v]) => '<span class="stat">' + esc(k) + ': ' + v + '</span>').join('') +
      '</div></section>';
    document.getElementById('main').innerHTML = h;
  } catch (e) {
    document.getElementById('status').textContent = 'disconnected';
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


@app.get("/workspace", response_class=HTMLResponse)
def workspace():
    return _embeddable(WORKSPACE_HTML)





# ── App APIs (project control center) ─────────────────────────────────────────

import base64
import os
import sys

_run_state: dict = {"proc": None}

_COMMANDS: dict = {
    # command -> which extra inputs it takes
    "autoformalize": {"targets": True, "metric": False, "version": False},
    "formalize":     {"targets": True, "metric": False, "version": False},
    "prove":         {"targets": True, "metric": False, "version": False},
    "solve":         {"targets": False, "metric": False, "version": False},
    "create":        {"targets": False, "metric": False, "version": False},
    "verify":        {"targets": True, "metric": False, "version": False},
    "bump":          {"targets": False, "metric": False, "version": True},
    "optimize":      {"targets": True, "metric": True, "version": False},
}


from fastapi import Request
from fastapi.responses import JSONResponse as _JR


@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    return _JR({"error": str(exc)}, status_code=400)


def _project_root() -> Path:
    return ROOT_DIR.parent


def _safe_unity_path(rel: str) -> Path:
    q = (ROOT_DIR / rel).resolve()
    if not str(q).startswith(str(ROOT_DIR.resolve())):
        raise ValueError("path escapes .unity")
    return q


def _forum_nonempty() -> bool:
    for tp in FORUM_DIR.glob("*.json"):
        if tp.name.startswith("_") or tp.name in ("config.json", "balances.json"):
            continue
        try:
            if json.loads(tp.read_text()).get("posts"):
                return True
        except Exception:
            continue
    return False


def _active_metric() -> str:
    f = ROOT_DIR / "metrics" / ".active"
    return f.read_text().strip() if f.exists() else ""


# Run state is persisted to .unity/logs/webrun.json so the status pill and the
# stop button keep working across `unity serve` restarts (pid-based, process-group kill).

def _run_state_path() -> Path:
    return ROOT_DIR / "logs" / "webrun.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _current_run() -> dict:
    """The last-launched run, reconstructed from disk: works even if serve restarted."""
    try:
        st = json.loads(_run_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"running": False, "pid": None, "command": None, "started": 0.0,
                "log": None, "exit_code": None}
    proc = _run_state["proc"]
    if proc is not None and proc.pid == st.get("pid"):
        code = proc.poll()
        st.update(running=code is None, exit_code=code)
    else:  # serve was restarted: we only know liveness, not the exit code
        st.update(running=_pid_alive(st.get("pid")), exit_code=None)
    return st


@app.get("/api/project")
def api_project():
    dag = ROOT_DIR / "dag.json"
    chunk_ids = []
    if dag.exists():
        try:
            chunk_ids = [c.get("id") for c in json.loads(dag.read_text()).get("chunks", [])]
        except Exception:
            pass
    return {"name": _project_root().name,
            "continue": _forum_nonempty(),
            "has_dag": dag.exists(), "chunks": chunk_ids,
            "active_metric": _active_metric(),
            "commands": _COMMANDS,
            "metrics": sorted(p.name for p in (ROOT_DIR / "metrics").glob("*.md")) if (ROOT_DIR / "metrics").exists() else []}


@app.get("/api/unityfile")
def api_file_get(name: str):
    if name not in ("UNITY.md", "agents.yaml"):
        return JSONResponse({"error": "file not editable"}, status_code=400)
    q = _safe_unity_path(name)
    return {"name": name, "content": q.read_text() if q.exists() else ""}


from fastapi import Body


@app.put("/api/unityfile")
def api_file_put(payload: dict = Body(...)):
    name = payload.get("name", "")
    if name not in ("UNITY.md", "agents.yaml"):
        return JSONResponse({"error": "file not editable"}, status_code=400)
    q = _safe_unity_path(name)
    q.write_text(payload.get("content", ""))
    return {"ok": True}


@app.get("/api/agents")
def api_agents_get():
    q = ROOT_DIR / "agents.yaml"
    raw = q.read_text() if q.exists() else ""
    groups = []
    try:
        import yaml
        doc = yaml.safe_load(raw) or {}
        groups = doc.get("agents") or []
    except Exception:
        pass
    return {"raw": raw, "groups": groups}


def _serialize_groups(groups: list) -> str:
    import yaml
    clean = []
    for g in groups:
        entry = {}
        for k in ("names", "model", "backend", "provider", "strength", "budget",
                  "base_url", "api_key", "auth_token"):
            v = g.get(k)
            if v in (None, "", []):
                continue
            entry[k] = v
        clean.append(entry)
    header = ("# First agent is the primary (preparation, critic, retrospective).\n"
              "# strength: static capability tier; budget: USD per instance.\n\n")
    return header + yaml.safe_dump({"agents": clean}, sort_keys=False)


@app.put("/api/agents")
def api_agents_put(payload: dict = Body(...)):
    q = ROOT_DIR / "agents.yaml"
    if "raw" in payload:
        q.write_text(payload["raw"])
        return {"ok": True}
    q.write_text(_serialize_groups(payload.get("groups") or []))
    return {"ok": True}


@app.post("/api/agents/convert")
def api_agents_convert(payload: dict = Body(...)):
    """Stateless converter for the two-way editor sync: groups -> raw, or raw -> groups.
    Writes nothing."""
    if "groups" in payload:
        return {"raw": _serialize_groups(payload.get("groups") or [])}
    import yaml
    try:
        doc = yaml.safe_load(payload.get("raw", "")) or {}
        groups = doc.get("agents") or []
        if not isinstance(groups, list):
            raise ValueError("'agents' must be a list")
        return {"groups": groups}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sources")
def api_sources():
    d = ROOT_DIR / "source"
    d.mkdir(exist_ok=True)
    out = []
    for p in sorted(d.rglob("*")):
        if p.is_file():
            out.append({"name": str(p.relative_to(d)), "size": p.stat().st_size})
    return {"files": out}


@app.get("/api/sources/file")
def api_source_get(name: str):
    q = _safe_unity_path(f"source/{name}")
    if not q.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    data = q.read_bytes()
    try:
        return {"name": name, "text": data.decode("utf-8"), "binary": False}
    except UnicodeDecodeError:
        return {"name": name, "text": f"(binary file, {len(data)} bytes)", "binary": True}


@app.post("/api/sources/file")
def api_source_add(payload: dict = Body(...)):
    name = Path(payload.get("name", "")).name
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    q = _safe_unity_path(f"source/{name}")
    q.parent.mkdir(parents=True, exist_ok=True)
    if "content_b64" in payload:
        q.write_bytes(base64.b64decode(payload["content_b64"]))
    else:
        q.write_text(payload.get("content", ""))
    return {"ok": True, "name": name}


@app.put("/api/sources/file")
def api_source_put(payload: dict = Body(...)):
    name = payload.get("name", "")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    q = _safe_unity_path(f"source/{name}")
    if not q.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    q.write_text(payload.get("content", ""))
    return {"ok": True}


@app.delete("/api/sources/file")
def api_source_del(name: str):
    q = _safe_unity_path(f"source/{name}")
    if q.exists():
        q.unlink()
    return {"ok": True}


@app.get("/api/metrics")
def api_metrics():
    d = ROOT_DIR / "metrics"
    d.mkdir(exist_ok=True)
    return {"files": sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.startswith(".")),
            "active": _active_metric()}


@app.get("/api/metrics/file")
def api_metric_get(name: str):
    q = _safe_unity_path(f"metrics/{Path(name).name}")
    if not q.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"name": Path(name).name, "content": q.read_text(errors="replace")}


@app.put("/api/metrics/file")
def api_metric_put(payload: dict = Body(...)):
    name = Path(payload.get("name", "")).name
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    q = _safe_unity_path(f"metrics/{name}")
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(payload.get("content", ""))
    return {"ok": True}


@app.delete("/api/metrics/file")
def api_metric_del(name: str):
    q = _safe_unity_path(f"metrics/{Path(name).name}")
    if q.exists():
        q.unlink()
    if _active_metric() == Path(name).stem:
        (ROOT_DIR / "metrics" / ".active").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/metrics/active")
def api_metric_active(payload: dict = Body(...)):
    d = ROOT_DIR / "metrics"
    d.mkdir(exist_ok=True)
    name = Path(payload.get("name") or "").stem
    if not name or payload.get("clear"):
        (d / ".active").unlink(missing_ok=True)
        return {"ok": True, "active": ""}
    (d / ".active").write_text(name)
    return {"ok": True, "active": name}


_METRIC_TEMPLATE = """# {name}

## Prompt
<what to optimize for, and how to judge an improvement>

## Examples
(none)

## Score function
(none)

## Metric function
(none)
"""


@app.post("/api/metrics/new")
def api_metric_new(payload: dict = Body(...)):
    name = Path(payload.get("name", "")).stem.lower()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    q = _safe_unity_path(f"metrics/{name}.md")
    if q.exists():
        return JSONResponse({"error": "metric exists"}, status_code=400)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(_METRIC_TEMPLATE.format(name=name))
    return {"ok": True, "name": f"{name}.md"}


import re as _re

_DECL_RE = _re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+|partial\s+|unsafe\s+|scoped\s+)*"
    r"(theorem|lemma|def|abbrev|structure|inductive|class|instance|opaque|axiom)\s+([A-Za-z0-9_.'₀-₉]+)",
    _re.M)


_IDENT_RE = _re.compile(r"[A-Za-z_][A-Za-z0-9_.'₀-₉]*")


def _blueprint_trees() -> list[str]:
    """Selectable sources for the blueprint: the main checkout + live agent worktrees."""
    trees = ["main"]
    wd = _project_root() / ".worktrees"
    if wd.is_dir():
        trees += sorted(p.name for p in wd.iterdir() if p.is_dir())
    return trees


def _blueprint_base(tree: str) -> Path:
    if tree in ("", "main"):
        return _project_root()
    base = (_project_root() / ".worktrees" / tree).resolve()
    if base.parent != (_project_root() / ".worktrees").resolve() or not base.is_dir():
        raise ValueError(f"unknown tree '{tree}'")
    return base


def _scan_blueprint(base: Path) -> list[tuple[str, list[dict], dict]]:
    """Parse `base`'s .lean files into (relpath, decls, bodies-by-name) triples."""
    parsed = []
    for p in sorted(base.rglob("*.lean")):
        if any(part in (".lake", ".unity", ".worktrees", "lake-packages", "build") for part in p.parts):
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        matches = list(_DECL_RE.finditer(text))
        decls, bodies = [], {}
        for i, m in enumerate(matches):
            body = text[m.start():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
            kind, name = m.group(1), m.group(2)
            status = ("axiom" if kind == "axiom"
                      else "sorry" if _re.search(r"\bsorry\b|\badmit\b", body) else "complete")
            decls.append({"name": name, "kind": kind, "status": status,
                          "line": text.count("\n", 0, m.start()) + 1})
            bodies[name] = body
        if decls:
            parsed.append((str(p.relative_to(base)), decls, bodies))
    # naive in-project deps: identifiers in a decl's body that name another project decl
    all_names = {d["name"] for _, decls, _ in parsed for d in decls}
    used_by: dict = {}
    for _, decls, bodies in parsed:
        for d in decls:
            deps = sorted((set(_IDENT_RE.findall(bodies[d["name"]])) & all_names) - {d["name"]})
            d["deps"] = deps[:12]
            for n in deps:
                used_by.setdefault(n, []).append(d["name"])
    for _, decls, _ in parsed:
        for d in decls:
            d["used_by"] = len(used_by.get(d["name"], []))
    return parsed


# ── Kernel-exact blueprint (LeanArchitect's mechanism without its annotations) ────
# unity/blueprint_extract.lean loads the project's compiled modules and dumps every
# project decl with kernel-derived kind / sorry usage / used-constant deps. Slow-ish
# and needs a built project, so it runs in a background thread, is cached against the
# newest .lean mtime, and the regex scan serves whenever it's stale or unavailable.

import threading

_KERNEL: dict = {"running": False}
_KERNEL_LOCK = threading.Lock()


def _kernel_cache_path() -> Path:
    return ROOT_DIR / "blueprint-kernel.json"


def _src_stamp(base: Path) -> float:
    stamp = 0.0
    for p in base.rglob("*.lean"):
        if any(part in (".lake", ".unity", ".worktrees", "lake-packages", "build") for part in p.parts):
            continue
        try:
            stamp = max(stamp, p.stat().st_mtime)
        except OSError:
            pass
    return stamp


def _kernel_extract(base: Path) -> dict | None:
    """Run the bundled extractor; None when the project isn't built, the toolchain is
    too old for the script, or anything else fails (caller keeps the regex scan)."""
    mods = []
    for p in base.rglob("*.lean"):
        if any(part in (".lake", ".unity", ".worktrees", "lake-packages", "build") for part in p.parts):
            continue
        mods.append(".".join(p.relative_to(base).with_suffix("").parts))
    if not mods:
        return None
    script = Path(__file__).parent.parent / "blueprint_extract.lean"
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + str(Path.home() / ".elan" / "bin")
    try:
        r = subprocess.run(["lake", "env", "lean", "--run", str(script)] + sorted(mods),
                           cwd=base, capture_output=True, text=True, timeout=300, env=env)
        if r.returncode != 0:
            return None
        rows = json.loads(r.stdout.strip().splitlines()[-1])
        return {row["name"]: row for row in rows}
    except Exception:
        return None


def _kernel_refresh(stamp: float) -> None:
    data = _kernel_extract(_project_root())
    with _KERNEL_LOCK:
        _KERNEL.update(running=False, stamp=stamp, data=data)
    try:
        _kernel_cache_path().write_text(json.dumps({"stamp": stamp, "decls": data}))
    except OSError:
        pass


def _kernel_data() -> tuple[dict | None, bool]:
    """(fresh kernel data for the main tree | None, refresh-in-flight?). A stale cache
    kicks off a background re-extract; a recorded failed attempt is not retried until
    the sources change again."""
    stamp = _src_stamp(_project_root())
    with _KERNEL_LOCK:
        if "stamp" not in _KERNEL:  # first call after serve start: load persisted cache
            try:
                saved = json.loads(_kernel_cache_path().read_text())
                _KERNEL.update(stamp=saved.get("stamp", 0.0), data=saved.get("decls"))
            except (OSError, json.JSONDecodeError):
                _KERNEL.update(stamp=0.0, data=None)
        if _KERNEL["stamp"] == stamp:
            return _KERNEL["data"], _KERNEL["running"]
        if not _KERNEL["running"]:
            _KERNEL["running"] = True
            threading.Thread(target=_kernel_refresh, args=(stamp,), daemon=True).start()
        return None, True


def _apply_kernel(files: list, kernel: dict) -> None:
    """Overlay kernel-exact deps + status onto the regex scan. Kernel names are fully
    qualified; regex names are as written — match exactly or by unique '.name' suffix.
    Adds the 'tainted' status: proof complete but transitively resting on a sorry/axiom."""
    regex_names = {d["name"] for f in files for d in f["decls"]}
    to_regex: dict = {}
    for kname in kernel:
        if kname in regex_names:
            to_regex[kname] = kname
        else:
            tails = [r for r in regex_names if kname.endswith("." + r)]
            if len(tails) == 1:
                to_regex[kname] = tails[0]
    to_kernel = {v: k for k, v in to_regex.items()}

    def direct_bad(k: str) -> bool:
        return kernel[k]["sorried"] or kernel[k]["kind"] == "axiom"

    memo: dict = {}

    def tainted(k: str, seen: tuple = ()) -> bool:
        if k in memo:
            return memo[k]
        if k in seen:
            return False
        bad = any(direct_bad(d) or tainted(d, seen + (k,))
                  for d in kernel[k]["deps"] if d in kernel)
        memo[k] = bad
        return bad

    for f in files:
        for d in f["decls"]:
            k = to_kernel.get(d["name"])
            if not k:
                continue
            row = kernel[k]
            d["deps"] = sorted({to_regex[x] for x in row["deps"] if x in to_regex})
            if row["sorried"]:
                d["status"] = "sorry"
            elif row["kind"] == "axiom":
                d["status"] = "axiom"
            elif tainted(k):
                d["status"] = "tainted"
            else:
                d["status"] = "complete"
    counts: dict = {}
    for f in files:
        for d in f["decls"]:
            for x in d["deps"]:
                counts[x] = counts.get(x, 0) + 1
    for f in files:
        for d in f["decls"]:
            d["used_by"] = counts.get(d["name"], 0)


def _blueprint_files(tree: str):
    """(files, bodies-by-(path,name), source, refreshing) — regex scan with the kernel
    overlay applied when fresh exact data exists (main tree only)."""
    parsed = _scan_blueprint(_blueprint_base(tree))
    files = [{"path": path, "decls": decls} for path, decls, _ in parsed]
    bodies = {(path, d["name"]): bs[d["name"]] for path, decls, bs in parsed for d in decls}
    source, refreshing = "regex", False
    if tree in ("", "main"):
        kernel, refreshing = _kernel_data()
        if kernel:
            _apply_kernel(files, kernel)
            source = "kernel"
    return files, bodies, source, refreshing


@app.get("/api/blueprint")
def api_blueprint(tree: str = "main"):
    """The Lean blueprint: every declaration in the selected tree's .lean files, with
    proof status (complete / tainted / sorry / axiom) and in-project dependency links —
    kernel-exact when the project builds, regex-approximate otherwise. This is the
    project's actual Lean structure, distinct from the run's chunk DAG."""
    files, _, source, refreshing = _blueprint_files(tree)
    total = sum(len(f["decls"]) for f in files)
    counts = {s: sum(1 for f in files for d in f["decls"] if d["status"] == s)
              for s in ("sorry", "axiom", "tainted")}
    return {"files": files, "total": total, "sorries": counts["sorry"],
            "axioms": counts["axiom"], "tainted": counts["tainted"],
            "source": source, "refreshing": refreshing,
            "tree": tree or "main", "trees": _blueprint_trees()}


def _chunk_for_decl(name: str) -> dict | None:
    """Find the chunk in dag.json that covers a declaration, if any."""
    try:
        chunks = json.loads((ROOT_DIR / "dag.json").read_text()).get("chunks", [])
    except (OSError, json.JSONDecodeError):
        return None
    short = name.split(".")[-1]
    for c in chunks:
        decls = c.get("declarations") or []
        if (name in decls or short in decls or c.get("lean_decl") in (name, short)
                or c.get("title") in (name, short)):
            return {"id": c.get("id"), "title": c.get("title", ""), "status": c.get("status", "")}
    for c in chunks:  # weaker match: the decl name inside the chunk id or statement
        if short and (short.lower() in str(c.get("id", "")).lower()
                      or _re.search(r"\b" + _re.escape(short) + r"\b", str(c.get("statement", "")))):
            return {"id": c.get("id"), "title": c.get("title", ""), "status": c.get("status", "")}
    return None


@app.get("/api/blueprint/decl")
def api_blueprint_decl(name: str, file: str, tree: str = "main"):
    """Detail for one declaration: signature, full source, deps both ways (kernel-exact
    when available), chunk link."""
    files, bodies, source, _ = _blueprint_files(tree)
    for f in files:
        if f["path"] != file:
            continue
        for d in f["decls"]:
            if d["name"] != name:
                continue
            body = bodies[(file, name)]
            # signature: decl head up to the proof/definition body
            sig_end = _re.search(r":=|\bby\b|\bwhere\b", body)
            signature = body[:sig_end.start()].rstrip() if sig_end else body.splitlines()[0]
            used_by = sorted({x["name"] for f2 in files for x in f2["decls"]
                              if name in x["deps"]})
            return {**{k: d[k] for k in ("name", "kind", "status", "line", "deps")},
                    "file": file, "signature": signature[:2000], "source_kind": source,
                    "source": body.rstrip()[:6000], "truncated": len(body) > 6000,
                    "used_by": used_by[:20], "chunk": _chunk_for_decl(name)}
    return JSONResponse({"error": f"declaration '{name}' not found in {file}"}, status_code=404)


@app.get("/api/tools")
def api_tools():
    """Aggregated tool-call telemetry from .unity/logs/tools.jsonl: agent × tool counts."""
    f = ROOT_DIR / "logs" / "tools.jsonl"
    counts: dict = {}
    last_ts = None
    if f.exists():
        for line in f.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (e.get("agent", "?"), e.get("tool", "?"))
            counts[key] = counts.get(key, 0) + 1
            last_ts = e.get("ts", last_ts)
    rows = [{"agent": a, "tool": t, "count": c} for (a, t), c in counts.items()]
    rows.sort(key=lambda r: -r["count"])
    return {"rows": rows, "last_ts": last_ts}


@app.get("/api/logs")
def api_logs():
    d = ROOT_DIR / "logs"
    if not d.exists():
        return {"files": []}
    files = [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
             for p in d.iterdir() if p.is_file() and p.suffix in (".log", ".jsonl")]
    return {"files": sorted(files, key=lambda f: -f["mtime"])}


_ANSI_RE = None


def _strip_ansi(text: str) -> str:
    global _ANSI_RE
    if _ANSI_RE is None:
        import re
        _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    return _ANSI_RE.sub("", text)


@app.get("/api/logs/file")
def api_log_get(name: str):
    q = _safe_unity_path(f"logs/{Path(name).name}")
    if not q.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    data = q.read_bytes()
    clipped = len(data) > 200_000
    return {"name": Path(name).name, "clipped": clipped,
            "text": _strip_ansi(data[-200_000:].decode("utf-8", errors="replace"))}


@app.get("/api/run")
def api_run_status():
    st = _current_run()
    tail = ""
    if st.get("log") and Path(st["log"]).exists():
        data = Path(st["log"]).read_bytes()
        tail = _strip_ansi(data[-4000:].decode("utf-8", errors="replace"))
    phase = None
    if st["running"]:
        try:
            phase = json.loads((ROOT_DIR / "state.json").read_text()).get("phase")
        except (OSError, json.JSONDecodeError):
            pass
    return {"running": st["running"], "command": st.get("command"),
            "started": st.get("started", 0.0), "exit_code": st.get("exit_code"),
            "phase": phase, "stopping": st["running"] and (ROOT_DIR / "stop-requested").exists(),
            "log": Path(st["log"]).name if st.get("log") else None,
            "log_tail": tail}


@app.post("/api/run")
def api_run_start(payload: dict = Body(...)):
    command = payload.get("command", "")
    if command not in _COMMANDS:
        return JSONResponse({"error": f"unknown command '{command}'"}, status_code=400)
    if _current_run()["running"]:
        return JSONResponse({"error": "a run is already active"}, status_code=409)

    argv = [command]
    if command == "optimize":
        metric = payload.get("metric") or _active_metric()
        if not metric:
            return JSONResponse({"error": "optimize needs a metric (set one active or pass it)"},
                                status_code=400)
        argv.append(Path(metric).stem)
    if _COMMANDS[command].get("version"):
        ver = (payload.get("version") or "").strip()
        if ver:
            argv.append(ver)
    targets = (payload.get("targets") or "").strip()
    if targets and _COMMANDS[command]["targets"]:
        argv += ["--targets", ", ".join(t.strip() for t in targets.splitlines() if t.strip())]
    cont = payload.get("continue")
    if cont is None:
        cont = _forum_nonempty()
    if cont:
        argv.append("--continue")

    if payload.get("dry"):
        return {"ok": True, "dry": True, "argv": argv}

    logs = ROOT_DIR / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"webrun-{int(time.time())}-{command}.log"
    code = ("import sys, json, os; sys.argv = ['unity'] + json.loads(os.environ['UNITY_WEB_ARGS']); "
            "from unity.cli import main; main()")
    env = {**os.environ, "UNITY_WEB_ARGS": json.dumps(argv)}
    (ROOT_DIR / "stop-requested").unlink(missing_ok=True)  # clear any stale safe-stop flag
    lf = open(log_path, "ab")
    # own process group so stop can kill the whole agent tree, not just the wrapper
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(_project_root()),
                            stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=env, start_new_session=True)
    _run_state["proc"] = proc
    _run_state_path().write_text(json.dumps(
        {"pid": proc.pid, "command": command, "started": time.time(), "log": str(log_path)}))
    return {"ok": True, "argv": argv, "log": str(log_path)}


@app.post("/api/run/stop")
def api_run_stop(payload: dict = Body(default={})):
    import signal
    st = _current_run()
    if not st["running"]:
        return {"ok": True, "stopped": False}
    if payload.get("mode", "safe") == "safe":
        # Safe stop: agents end at their next stream item, remaining phases are skipped,
        # worktrees get cleaned up. Force mode (second click) kills the process group.
        (ROOT_DIR / "stop-requested").write_text(str(time.time()))
        return {"ok": True, "stopping": True}
    pid = st["pid"]
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
        for _ in range(30):  # up to 3s of grace, then hard-kill the group
            time.sleep(0.1)
            if not _pid_alive(pid):
                break
        else:
            os.killpg(pgid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return {"ok": True, "stopped": True}




APP_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>unity</title>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-monospace, 'Cascadia Code', 'Fira Code', 'Menlo', monospace; font-size: 13px; background: #fafafa; color: #111; }
header { display: flex; align-items: center; justify-content: space-between; padding: 10px 20px; border-bottom: 1px solid #e4e4e4; background: #fafafa; position: sticky; top: 0; z-index: 5; }
header h1 { font-size: 14px; font-weight: 600; letter-spacing: 0.14em; }
.tabs { display: flex; gap: 2px; border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden; }
.tabs button { background: none; border: none; border-left: 1px solid #e0e0e0; cursor: pointer; font: inherit; font-size: 12px; padding: 4px 12px; color: #666; }
.tabs button:first-child { border-left: none; }
.tabs button.active { background: #111; color: #fff; }
nav { display: flex; gap: 14px; align-items: center; }
nav a { font-size: 12px; color: #888; text-decoration: none; }
nav a:hover { color: #111; }
.runwrap { position: relative; }
#runbtn { background: #111; color: #fff; border: none; border-radius: 5px; font: inherit; font-size: 12px; padding: 5px 14px; cursor: pointer; }
#runbtn.running { background: #b71c1c; }
#runbtn.stopping { background: #b7791f; }
.runmenu { display: none; position: absolute; right: 0; top: 110%; background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; min-width: 150px; box-shadow: 0 4px 14px rgba(0,0,0,0.08); z-index: 20; }
.runwrap:hover .runmenu { display: block; }
.runwrap.running .runmenu { display: none; }
.runmenu div { padding: 7px 14px; cursor: pointer; font-size: 12px; color: #444; }
.runmenu div:hover { background: #f2f2f2; color: #111; }
#status { font-size: 11px; color: #bbb; }
main { padding: 0; }
.pane { padding: 18px 20px; }
.framewrap iframe { display: block; width: 100%; height: calc(100vh - 54px); border: none; background: #fff; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; align-items: start; }
section { background: #fff; border: 1px solid #e4e4e4; border-radius: 6px; padding: 12px 14px; min-width: 0; overflow-wrap: anywhere; }
section h2 { font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: #bbb; margin-bottom: 8px; }
.item { padding: 6px 0; border-top: 1px solid #f2f2f2; line-height: 1.45; overflow-wrap: anywhere; word-break: break-word; }
.item:first-of-type { border-top: none; }
.who { color: #999; font-size: 11px; }
.badge { display: inline-block; font-size: 10px; border-radius: 4px; padding: 1px 6px; margin-left: 6px; }
.ok { background: #e8f5e9; color: #1b5e20; } .blocked { background: #ffebee; color: #b71c1c; } .pending { background: #f5f5f5; color: #888; }
.kind { font-size: 10px; text-transform: uppercase; color: #888; margin-right: 6px; }
textarea { width: 100%; min-height: 320px; font: inherit; font-size: 12.5px; border: 1px solid #e0e0e0; border-radius: 6px; padding: 10px; background: #fff; resize: vertical; }
input, select { font: inherit; font-size: 12px; border: 1px solid #e0e0e0; border-radius: 5px; padding: 4px 8px; background: #fff; }
button.act { font: inherit; font-size: 12px; border: 1px solid #d8d8d8; background: #fff; border-radius: 5px; padding: 4px 12px; cursor: pointer; color: #333; }
button.act:hover { border-color: #999; }
button.primary { background: #111; color: #fff; border-color: #111; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0; }
.agent-card { border: 1px solid #e8e8e8; border-radius: 6px; padding: 10px 12px; margin-bottom: 10px; }
.agent-card .fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; }
.agent-card label { font-size: 10px; text-transform: uppercase; color: #aaa; display: block; margin-bottom: 2px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.chip { font-size: 11px; border: 1px solid #ddd; border-radius: 10px; padding: 1px 9px; cursor: pointer; color: #555; }
.chip.on { background: #111; color: #fff; border-color: #111; }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,0.28); display: none; align-items: center; justify-content: center; z-index: 40; }
.modal.open { display: flex; }
.modal .box { background: #fff; border-radius: 8px; padding: 18px 20px; width: min(680px, 92vw); max-height: 84vh; overflow-y: auto; }
.modal h3 { font-size: 13px; margin-bottom: 10px; }
pre.log { background: #111; color: #ddd; font-size: 11px; padding: 12px; border-radius: 6px; max-height: 50vh; overflow: auto; white-space: pre-wrap; }
.filelist td { padding: 5px 10px 5px 0; border-top: 1px solid #f2f2f2; font-size: 12px; }
.empty { color: #ccc; padding: 8px 0; }
.savemsg { font-size: 11px; color: #2d7a2d; margin-left: 8px; }
.bp-decl { display: flex; gap: 8px; align-items: baseline; padding: 4px 0; border-top: 1px solid #f4f4f4; font-size: 12px; }
.bp-decl:first-of-type { border-top: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; align-self: center; }
.dot.complete { background: #43a047; } .dot.sorry { background: #e53935; } .dot.axiom { background: #fb8c00; } .dot.tainted { background: #fbc02d; }
.bp-kind { color: #aaa; font-size: 10px; text-transform: uppercase; width: 68px; flex: none; }
.bp-deps { color: #bbb; font-size: 11px; margin-left: auto; text-align: right; }
pre.tail { background: #111; color: #ddd; font-size: 11px; padding: 12px; border-radius: 6px; height: 58vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
</head>
<body>
<header>
  <h1 id="title">unity</h1>
  <div class="tabs" id="tabs">
    <button data-tab="blueprint" class="active">blueprint</button>
    <button data-tab="overview">overview</button>
    <button data-tab="forum">forum</button>
    <button data-tab="graph">graph</button>
    <button data-tab="chunks">chunks</button>
    <button data-tab="agents">agents</button>
    <button data-tab="prompt">prompt</button>
    <button data-tab="sources">sources</button>
    <button data-tab="metrics">metrics</button>
    <button data-tab="logs">logs</button>
  </div>
  <nav>
    <span id="status">idle</span>
    <div class="runwrap" id="runwrap">
      <button id="runbtn">run ▾</button>
      <div class="runmenu" id="runmenu"></div>
    </div>
  </nav>
</header>
<main>
  <div id="tab-blueprint" class="pane"></div>
  <div id="tab-overview" class="pane grid" style="display:none"></div>
  <div id="tab-forum" class="framewrap" style="display:none"><iframe data-src="/forum"></iframe></div>
  <div id="tab-graph" class="framewrap" style="display:none"><iframe data-src="/graph"></iframe></div>
  <div id="tab-chunks" class="framewrap" style="display:none"><iframe data-src="/dag"></iframe></div>
  <div id="tab-agents" class="pane" style="display:none"></div>
  <div id="tab-prompt" class="pane" style="display:none"></div>
  <div id="tab-sources" class="pane" style="display:none"></div>
  <div id="tab-metrics" class="pane" style="display:none"></div>
  <div id="tab-logs" class="pane" style="display:none"></div>
</main>

<div class="modal" id="runmodal"><div class="box">
  <h3 id="rm-title">run</h3>
  <div class="row"><label><input type="checkbox" id="rm-continue"> --continue</label>
    <span class="who" id="rm-continue-note"></span></div>
  <div id="rm-metric-row" class="row" style="display:none">metric:
    <select id="rm-metric"></select></div>
  <div id="rm-version-row" class="row" style="display:none">version:
    <input id="rm-version" placeholder="e.g. v4.16.0 (blank = from UNITY.md)" style="flex:1"></div>
  <div id="rm-targets-row" style="display:none">
    <div class="who">targets — one per line (empty = all)</div>
    <textarea id="rm-targets" style="min-height:90px"></textarea>
    <div class="chips" id="rm-chips"></div>
  </div>
  <div class="row"><button class="act primary" id="rm-start">start</button>
    <button class="act" onclick="document.getElementById('runmodal').classList.remove('open')">cancel</button>
    <span class="who" id="rm-err"></span></div>
</div></div>

<div class="modal" id="viewmodal"><div class="box">
  <h3 id="view-name"></h3>
  <pre class="log" id="view-body" style="background:#fafafa;color:#111;border:1px solid #eee"></pre>
  <div class="row"><button class="act" onclick="document.getElementById('viewmodal').classList.remove('open')">close</button></div>
</div></div>

<div class="modal" id="declmodal"><div class="box">
  <h3 id="dm-title"></h3>
  <div class="who" id="dm-meta" style="margin-bottom:10px; line-height:1.6"></div>
  <pre class="log" id="dm-source" style="background:#fafafa;color:#111;border:1px solid #eee; max-height:46vh"></pre>
  <div class="row"><button class="act" onclick="document.getElementById('declmodal').classList.remove('open')">close</button></div>
</div></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<script>
const esc = t => (t || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $ = id => document.getElementById(id);
let PROJECT = {commands:{}, chunks:[], metrics:[]};
const J = (url, opts) => fetch(url, opts).then(r => r.json());
const put = (url, body) => J(url, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
const post = (url, body) => J(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});

const TABS = ['blueprint','overview','forum','graph','chunks','agents','prompt','sources','metrics','logs'];
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('#tabs button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  TABS.forEach(t => $('tab-'+t).style.display = 'none');
  const t = b.dataset.tab; $('tab-'+t).style.display = t === 'overview' ? 'grid' : 'block';
  const fr = document.querySelector('#tab-' + t + ' iframe');
  if (fr && !fr.getAttribute('src')) fr.src = fr.dataset.src;
  if (loaders[t]) loaders[t]();
});
function reltime(ts) {
  if (!ts) return '';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 129600) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal.open').forEach(m => m.classList.remove('open'));
});

function badge(r) {
  if (r.mergeable) return '<span class="badge ok">mergeable</span>';
  if (r.open_objections.length) return '<span class="badge blocked">' + r.open_objections.length + ' objection(s)</span>';
  return '<span class="badge pending">needs endorsement</span>';
}
async function loadOverview() {
  try {
    const d = await J('/api/workspace');
    const when = x => x.ts ? ' <span class="who">· ' + reltime(x.ts) + '</span>' : '';
    let h = '';
    h += '<section><h2>Binding decisions</h2>' + (d.decisions.length ? d.decisions.map(x =>
      '<div class="item"><b>' + esc(x.topic) + '</b>: ' + esc(x.choice) + ' <span class="who">(' + esc(x.author) + ')</span>' + when(x) + '</div>').join('') : '<div class="empty">none yet</div>') + '</section>';
    h += '<section><h2>Chunk consensus</h2>' + (d.chunks.length ? d.chunks.map(c =>
      '<div class="item"><b>' + esc(c.chunk) + '</b>' +
      c.results.map(r => '<div class="item">' + esc(r.author) + ': ' + esc(r.status) + (r.build_ok ? ' ✓build' : '') + badge(r) +
        r.open_objections.map(o => '<div class="who">⛔ ' + esc(o.by) + ': ' + esc(o.reason) + '</div>').join('') + '</div>').join('') +
      (c.claims.length ? '<div class="who">claims: ' + c.claims.map(cl => esc(cl.author)).join(', ') + '</div>' : '') +
      '</div>').join('') : '<div class="empty">no chunk activity yet</div>') + '</section>';
    h += '<section><h2>Open obstacles</h2>' + (d.chunks.some(c => c.obstacles.length) ?
      d.chunks.flatMap(c => c.obstacles.map(o => '<div class="item">' + esc(o.content) + '</div>')).join('') : '<div class="empty">none open</div>') + '</section>';
    h += '<section><h2>Open questions</h2>' + (d.questions.length ? d.questions.map(q =>
      '<div class="item">' + esc(q.content) + '</div>').join('') : '<div class="empty">none open</div>') + '</section>';
    h += '<section><h2>Ledger</h2>' + (d.ledger.length ? d.ledger.map(l =>
      '<div class="item"><span class="kind">' + esc(l.kind) + '</span>' + esc(l.content) + when(l) + '</div>').join('') : '<div class="empty">no verified knowledge yet</div>') + '</section>';
    h += '<section><h2>Latest handoffs</h2>' + (d.handoffs.length ? d.handoffs.map(x =>
      '<div class="item">' + esc(x.content) + when(x) + '</div>').join('') : '<div class="empty">none yet</div>') + '</section>';
    const t = await J('/api/tools');
    h += '<section><h2>Tool usage</h2>' + (t.rows.length ?
      '<table class="filelist">' + t.rows.slice(0, 30).map(r =>
        '<tr><td>' + esc(r.agent) + '</td><td>' + esc(r.tool) + '</td><td class="who">' + r.count + '×</td></tr>').join('') + '</table>'
      : '<div class="empty">no tool calls recorded yet</div>') + '</section>';
    $('tab-overview').innerHTML = h;
  } catch (e) { $('tab-overview').innerHTML = '<section><div class="empty">overview unavailable</div></section>'; }
}

let BP = {tree: 'main', view: 'list', cy: null, dagreWired: false};
async function loadBlueprint() {
  const d = await J('/api/blueprint');
  if (d.error) return;
  const counts = ' — ' + d.total + ' declarations' +
    (d.sorries ? ' · <span style="color:#e53935">' + d.sorries + ' sorry</span>' : '') +
    (d.axioms ? ' · <span style="color:#fb8c00">' + d.axioms + ' axiom</span>' : '') +
    (d.tainted ? ' · <span style="color:#c8a000" title="proof complete but transitively rests on a sorry/axiom">' + d.tainted + ' tainted</span>' : '') +
    (d.total && !d.sorries && !d.axioms && !d.tainted ? ' · all complete ✓' : '');
  const src = d.source === 'kernel' ? '<span class="badge ok" title="statuses and dependencies read from the compiled Lean environment">kernel-verified</span>'
    : '<span class="badge pending" title="textual approximation — kernel data appears once the project builds">regex approx.</span>';
  const refr = d.refreshing ? ' <span class="who">kernel extraction running…</span>' : '';
  if (d.refreshing) setTimeout(() => { if ($('tab-blueprint').style.display !== 'none') loadBlueprint(); }, 8000);
  let h = '<section><div class="row" style="margin:0">' +
    '<h2 style="margin:0">Lean blueprint' + counts + ' ' + src + refr + '</h2><span style="flex:1"></span>' +
    '<button class="act" id="bp-toggle">' + (BP.view === 'list' ? 'graph view' : 'list view') + '</button></div></section>';
  if (!d.files.length) {
    h += '<section style="margin-top:12px"><div class="empty">no Lean declarations found in this tree</div></section>';
    $('tab-blueprint').innerHTML = h;
  } else if (BP.view === 'list') {
    h += d.files.map(f =>
      '<section style="margin-top:12px"><h2>' + esc(f.path) + '</h2>' +
      f.decls.map(x =>
        '<div class="bp-decl" style="cursor:pointer" data-file="' + esc(f.path) + '" data-name="' + esc(x.name) + '">' +
        '<span class="dot ' + x.status + '"></span>' +
        '<span class="bp-kind">' + esc(x.kind) + '</span>' +
        '<span title="line ' + x.line + (x.deps.length ? ' — uses: ' + esc(x.deps.join(', ')) : '') + '">' + esc(x.name) + '</span>' +
        '<span class="bp-deps">' + (x.deps.length ? '→ ' + x.deps.length : '') + (x.used_by ? ' · used by ' + x.used_by : '') + '</span></div>'
      ).join('') + '</section>').join('');
    $('tab-blueprint').innerHTML = h;
    document.querySelectorAll('#tab-blueprint .bp-decl').forEach(el =>
      el.onclick = () => showDecl(el.dataset.file, el.dataset.name));
  } else {
    h += '<section style="margin-top:12px; padding:0"><div id="bp-cy" style="height:74vh"></div></section>';
    $('tab-blueprint').innerHTML = h;
    buildBpGraph(d);
  }
  $('bp-toggle').onclick = () => { BP.view = BP.view === 'list' ? 'graph' : 'list'; loadBlueprint(); };
}
function buildBpGraph(d) {
  if (typeof cytoscape === 'undefined' || typeof cytoscapeDagre === 'undefined') {
    $('bp-cy').innerHTML = '<div class="empty" style="padding:20px">graph libraries unavailable (offline?) — use list view</div>';
    return;
  }
  if (!BP.dagreWired) { cytoscape.use(cytoscapeDagre); BP.dagreWired = true; }
  const COLORS = {complete: {bg: '#c8f0c8', border: '#2d7a2d'}, sorry: {bg: '#f8c8c8', border: '#c02020'},
                  axiom: {bg: '#ffe2b8', border: '#c87800'}, tainted: {bg: '#fff3a0', border: '#c8a000'}};
  const names = new Set(); d.files.forEach(f => f.decls.forEach(x => names.add(x.name)));
  const elements = [];
  d.files.forEach(f => f.decls.forEach(x => {
    const col = COLORS[x.status] || COLORS.complete;
    elements.push({data: {id: x.name, label: x.name.split('.').pop(), file: f.path,
                          bgColor: col.bg, borderColor: col.border}});
    x.deps.forEach(dep => { if (names.has(dep)) elements.push({data: {id: dep + '->' + x.name, source: dep, target: x.name}}); });
  }));
  BP.cy = cytoscape({
    container: $('bp-cy'), elements,
    style: [
      {selector: 'node', style: {'background-color': 'data(bgColor)', 'border-color': 'data(borderColor)',
        'border-width': 1.5, 'label': 'data(label)', 'font-size': '11px',
        'font-family': 'ui-monospace, "Cascadia Code", "Fira Code", "Menlo", monospace',
        'text-valign': 'center', 'text-halign': 'center', 'shape': 'roundrectangle',
        'color': '#111', 'padding': '8px 12px', 'min-zoomed-font-size': 7}},
      {selector: 'edge', style: {'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
        'target-arrow-color': '#ccc', 'line-color': '#ccc', 'arrow-scale': 0.75, 'width': 1}},
    ],
    layout: {name: 'dagre', rankDir: 'TB', nodeSep: 30, rankSep: 60, padding: 24},
  });
  BP.cy.on('tap', 'node', e => showDecl(e.target.data('file'), e.target.id()));
}
async function showDecl(file, name) {
  const d = await J('/api/blueprint/decl?tree=' + encodeURIComponent(BP.tree) +
                    '&file=' + encodeURIComponent(file) + '&name=' + encodeURIComponent(name));
  if (d.error) return;
  $('dm-title').textContent = d.kind + ' ' + d.name;
  const chunk = d.chunk ? 'chunk: <b>' + esc(d.chunk.id) + '</b>' + (d.chunk.status ? ' (' + esc(d.chunk.status) + ')' : '') : 'no matching chunk';
  $('dm-meta').innerHTML = [
    'status: <b>' + esc(d.status) + '</b>', esc(d.file) + ':' + d.line, chunk,
    d.deps.length ? 'uses: ' + esc(d.deps.join(', ')) : 'uses: nothing in-project',
    d.used_by.length ? 'used by: ' + esc(d.used_by.join(', ')) : 'used by: nothing in-project',
  ].join('<br>');
  $('dm-source').textContent = d.source + (d.truncated ? '\n… (truncated)' : '');
  $('declmodal').classList.add('open');
}

const AG_F = ['model','backend','provider','budget','base_url','api_key','auth_token'];
let agTimer = null, agLastEdited = 'cards';
function agCollect() {
  return [...document.querySelectorAll('.agent-card')].map(c => {
    const g = {names: c.querySelector('[data-f=names]').value.split(',').map(x => x.trim()).filter(Boolean)};
    AG_F.forEach(f => { const v = c.querySelector('[data-f=' + f + ']').value.trim(); if (v) g[f] = (f === 'budget' ? parseFloat(v) : v); });
    return g;
  });
}
function agRenderCards(groups) {
  $('agent-cards').innerHTML = (groups || []).map((g, i) => agentCard(g, i, AG_F)).join('');
  document.querySelectorAll('.ag-del').forEach(b => b.onclick = () => { b.closest('.agent-card').remove(); agSyncRaw(); });
}
async function agSyncRaw() {  // cards -> raw mirror
  agLastEdited = 'cards';
  const r = await post('/api/agents/convert', {groups: agCollect()});
  if (document.activeElement !== $('ag-raw-text')) { $('ag-raw-text').value = r.raw; $('ag-err').textContent = ''; }
}
async function agSyncCards() {  // raw -> cards mirror
  agLastEdited = 'raw';
  const r = await post('/api/agents/convert', {raw: $('ag-raw-text').value});
  if (r.error) { $('ag-err').textContent = 'yaml: ' + r.error; return; }
  $('ag-err').textContent = '';
  agRenderCards(r.groups);
}
async function loadAgents() {
  const d = await J('/api/agents');
  $('tab-agents').innerHTML =
    '<section><h2>agents</h2>' +
    '<div id="agent-cards"></div>' +
    '<div class="row"><button class="act" id="ag-add">+ group</button>' +
    '<button class="act primary" id="ag-save">save</button>' +
    '<button class="act" id="ag-raw-toggle">raw</button>' +
    '<span class="savemsg" id="ag-msg"></span><span class="who" id="ag-err" style="color:#b71c1c"></span></div>' +
    '<div id="ag-raw" style="display:none"><textarea id="ag-raw-text"></textarea></div></section>';
  agRenderCards(d.groups);
  $('ag-raw-text').value = d.raw;
  $('agent-cards').addEventListener('input', () => { clearTimeout(agTimer); agTimer = setTimeout(agSyncRaw, 350); });
  $('ag-raw-text').addEventListener('input', () => { clearTimeout(agTimer); agTimer = setTimeout(agSyncCards, 500); });
  $('ag-add').onclick = () => {
    $('agent-cards').insertAdjacentHTML('beforeend', agentCard({names:['agent']}, Date.now(), AG_F));
    document.querySelectorAll('.ag-del').forEach(b => b.onclick = () => { b.closest('.agent-card').remove(); agSyncRaw(); });
    agSyncRaw();
  };
  $('ag-raw-toggle').onclick = () => { const r = $('ag-raw'); r.style.display = r.style.display === 'none' ? 'block' : 'none'; };
  $('ag-save').onclick = async () => {
    clearTimeout(agTimer);
    if (agLastEdited === 'raw') await put('/api/agents', {raw: $('ag-raw-text').value});
    else await put('/api/agents', {groups: agCollect()});
    $('ag-msg').textContent = 'saved'; setTimeout(() => { $('ag-msg').textContent = ''; }, 1500);
    const wasOpen = $('ag-raw').style.display !== 'none';
    await loadAgents();
    if (wasOpen) $('ag-raw').style.display = 'block';
  };
}
function agentCard(g, i, F) {
  const inp = (f, v, type) => '<div><label>' + f + '</label><input data-f="' + f + '" value="' + esc(String(v ?? '')) + '"' + (type ? ' type="' + type + '"' : '') + ' style="width:100%"></div>';
  let h = '<div class="agent-card"><div class="fields">';
  h += inp('names', (g.names || []).join(', '));
  h += inp('model', g.model); h += inp('backend', g.backend || 'claude_code'); h += inp('provider', g.provider);
  h += inp('budget', g.budget); h += inp('base_url', g.base_url);
  h += inp('api_key', g.api_key, 'password'); h += inp('auth_token', g.auth_token, 'password');
  h += '</div><div class="row"><button class="act ag-del">remove</button></div></div>';
  return h;
}

async function loadPrompt() {
  const d = await J('/api/unityfile?name=UNITY.md');
  $('tab-prompt').innerHTML = '<section><h2>unity.md</h2>' +
    '<textarea id="um-text"></textarea><div class="row">' +
    '<button class="act primary" id="um-save">save</button><span class="savemsg" id="um-msg"></span></div></section>';
  $('um-text').value = d.content;
  $('um-save').onclick = async () => { await put('/api/unityfile', {name: 'UNITY.md', content: $('um-text').value});
    $('um-msg').textContent = 'saved'; setTimeout(() => $('um-msg').textContent = '', 1500); };
}

async function loadSources() {
  const d = await J('/api/sources');
  let h = '<section><h2>.unity/source/</h2><table class="filelist">';
  h += (d.files.length ? d.files.map(f => '<tr><td>' + esc(f.name) + '</td><td class="who">' + f.size + ' B</td>' +
    '<td><button class="act" onclick="editSource(\'' + esc(f.name) + '\')">edit</button></td>' +
    '<td><button class="act" onclick="delSource(\'' + esc(f.name) + '\')">remove</button></td></tr>').join('')
    : '<tr><td class="empty">no sources yet</td></tr>') + '</table>';
  h += '<div class="row" style="margin-top:12px"><input type="file" id="src-upload" multiple>' +
       '<span class="who">files are copied into .unity/source/</span></div>' +
       '<div id="src-editor" style="display:none;margin-top:10px"><div class="who" id="src-edit-name"></div>' +
       '<textarea id="src-text"></textarea><div class="row"><button class="act primary" id="src-save">save</button>' +
       '<span class="savemsg" id="src-msg"></span></div></div></section>';
  $('tab-sources').innerHTML = h;
  $('src-upload').onchange = async (e) => {
    for (const file of e.target.files) {
      const b64 = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result.split(',')[1]); r.readAsDataURL(file); });
      await post('/api/sources/file', {name: file.name, content_b64: b64});
    }
    loadSources();
  };
  $('src-save').onclick = async () => {
    await put('/api/sources/file', {name: $('src-edit-name').textContent, content: $('src-text').value});
    $('src-msg').textContent = 'saved'; setTimeout(() => $('src-msg').textContent = '', 1500);
  };
}
async function editSource(name) {
  const d = await J('/api/sources/file?name=' + encodeURIComponent(name));
  if (d.binary) { $('view-name').textContent = name; $('view-body').textContent = d.text; $('viewmodal').classList.add('open'); return; }
  $('src-editor').style.display = 'block'; $('src-edit-name').textContent = name; $('src-text').value = d.text;
}
async function delSource(name) {
  await fetch('/api/sources/file?name=' + encodeURIComponent(name), {method: 'DELETE'});
  loadSources();
}

async function loadMetrics() {
  const d = await J('/api/metrics');
  let h = '<section><h2>.unity/metrics/ ' + (d.active ? '<span class="badge ok">active: ' + esc(d.active) + '</span>' : '') + '</h2><table class="filelist">';
  h += (d.files.length ? d.files.map(f => { const on = f.replace(/[.]md$/, '') === d.active; return '<tr><td>' + esc(f) + (on ? ' *' : '') + '</td>' +
    '<td><button class="act" onclick="editMetric(\'' + esc(f) + '\')">edit</button></td>' +
    '<td><button class="act" onclick="' + (on ? 'unsetActive()' : 'setActive(\'' + esc(f) + '\')') + '">' + (on ? 'unset' : 'set active') + '</button></td>' +
    '<td><button class="act" onclick="delMetric(\'' + esc(f) + '\')">remove</button></td></tr>'; }).join('')
    : '<tr><td class="empty">no metrics</td></tr>') + '</table>';
  h += '<div class="row" style="margin-top:12px"><input id="mt-new-name" placeholder="new metric name">' +
       '<button class="act" id="mt-new">create</button></div>' +
       '<div id="mt-editor" style="display:none;margin-top:10px"><div class="who" id="mt-edit-name"></div>' +
       '<textarea id="mt-text"></textarea><div class="row"><button class="act primary" id="mt-save">save</button>' +
       '<span class="savemsg" id="mt-msg"></span></div></div></section>';
  $('tab-metrics').innerHTML = h;
  $('mt-new').onclick = async () => { const n = $('mt-new-name').value.trim(); if (!n) return;
    await post('/api/metrics/new', {name: n}); loadMetrics(); };
  $('mt-save').onclick = async () => {
    await put('/api/metrics/file', {name: $('mt-edit-name').textContent, content: $('mt-text').value});
    $('mt-msg').textContent = 'saved'; setTimeout(() => $('mt-msg').textContent = '', 1500);
  };
}
async function editMetric(name) {
  const d = await J('/api/metrics/file?name=' + encodeURIComponent(name));
  $('mt-editor').style.display = 'block'; $('mt-edit-name').textContent = name; $('mt-text').value = d.content;
}
async function setActive(name) { await post('/api/metrics/active', {name}); loadMetrics(); }
async function unsetActive() { await post('/api/metrics/active', {clear: true}); loadMetrics(); }

let LOG_OPEN = null;
async function loadLogs() {
  const d = await J('/api/logs');
  const fmt = t => new Date(t * 1000).toLocaleString();
  $('tab-logs').innerHTML = '<section><h2>.unity/logs/</h2><table class="filelist">' +
    (d.files.length ? d.files.map(f => '<tr><td>' + esc(f.name) + (f.name === RUN.log && RUN.running ? ' <span class="badge ok">live</span>' : '') + '</td>' +
      '<td class="who">' + f.size + ' B</td>' +
      '<td class="who">' + fmt(f.mtime) + ' (' + reltime(f.mtime) + ')</td>' +
      '<td><button class="act" onclick="openLog(\'' + esc(f.name) + '\')">view</button></td></tr>').join('')
      : '<tr><td class="empty">no logs yet</td></tr>') + '</table>' +
    '<div id="log-viewer" style="display:none;margin-top:12px"><div class="who" id="log-viewer-name"></div>' +
    '<pre class="tail" id="log-viewer-body"></pre></div></section>';
  if (LOG_OPEN) openLog(LOG_OPEN);
}
async function openLog(name) {
  LOG_OPEN = name;
  const d = await J('/api/logs/file?name=' + encodeURIComponent(name));
  const v = $('log-viewer'); if (!v) return;
  v.style.display = 'block';
  $('log-viewer-name').textContent = name + (d.clipped ? ' (last 200 KB)' : '') +
    (name === RUN.log && RUN.running ? ' — live, auto-refreshing' : '');
  const el = $('log-viewer-body'), stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 12 || !el.textContent;
  el.textContent = d.text || '(empty)';
  if (stick) el.scrollTop = el.scrollHeight;
}
async function delMetric(name) { await fetch('/api/metrics/file?name=' + encodeURIComponent(name), {method: 'DELETE'}); loadMetrics(); }

function buildRunMenu() {
  $('runmenu').innerHTML = Object.keys(PROJECT.commands).map(c => '<div data-c="' + c + '">' + c + '</div>').join('');
  document.querySelectorAll('#runmenu div').forEach(el => el.onclick = () => openRunModal(el.dataset.c));
}
let RUN_CMD = null;
function openRunModal(cmd) {
  RUN_CMD = cmd;
  $('rm-title').textContent = 'run: unity ' + cmd;
  $('rm-continue').checked = PROJECT['continue'];
  $('rm-continue-note').textContent = PROJECT['continue'] ? '(auto-detected: forum has prior state)' : '(fresh run: forum empty)';
  const spec = PROJECT.commands[cmd];
  $('rm-metric-row').style.display = spec.metric ? 'flex' : 'none';
  if (spec.metric) {
    $('rm-metric').innerHTML = PROJECT.metrics.map(m => '<option' + (m.replace(/[.]md$/, '') === PROJECT.active_metric ? ' selected' : '') + '>' + esc(m) + '</option>').join('');
  }
  $('rm-version-row').style.display = spec.version ? 'flex' : 'none';
  $('rm-version').value = '';
  $('rm-targets-row').style.display = spec.targets ? 'block' : 'none';
  $('rm-targets').value = '';
  $('rm-chips').innerHTML = (PROJECT.chunks || []).map(c => '<span class="chip" data-c="' + esc(c) + '">' + esc(c) + '</span>').join('');
  document.querySelectorAll('#rm-chips .chip').forEach(ch => ch.onclick = () => {
    ch.classList.toggle('on');
    const t = $('rm-targets'); const lines = new Set(t.value.split('\n').filter(Boolean));
    if (ch.classList.contains('on')) lines.add(ch.dataset.c); else lines.delete(ch.dataset.c);
    t.value = [...lines].join('\n');
  });
  $('rm-err').textContent = '';
  $('runmodal').classList.add('open');
}
$('rm-start').onclick = async () => {
  const body = {command: RUN_CMD, 'continue': $('rm-continue').checked, targets: $('rm-targets').value};
  if (PROJECT.commands[RUN_CMD].metric) body.metric = $('rm-metric').value;
  if (PROJECT.commands[RUN_CMD].version) body.version = $('rm-version').value;
  const r = await post('/api/run', body);
  if (r.error) { $('rm-err').textContent = r.error; return; }
  $('runmodal').classList.remove('open');
  pollRun();
};
let RUN = {running: false, stopping: false, log: null};
$('runbtn').onclick = async () => {
  if (!RUN.running) return;  // idle: the hover menu launches runs
  if (!RUN.stopping) {
    await post('/api/run/stop', {mode: 'safe'});  // agents end their current turn, run winds down
  } else if (confirm('Force stop? This kills the whole run immediately.')) {
    await post('/api/run/stop', {mode: 'force'});
  }
  pollRun();
};
async function pollRun() {
  const d = await J('/api/run');
  RUN = {running: d.running, stopping: d.stopping, log: d.log};
  if (d.running) {
    $('runwrap').classList.add('running');
    const mins = Math.floor((Date.now() / 1000 - d.started) / 60);
    $('runbtn').classList.toggle('stopping', d.stopping);
    $('runbtn').classList.toggle('running', !d.stopping);
    $('runbtn').textContent = d.stopping ? 'force stop' : 'stop';
    $('status').textContent = d.command + (d.phase ? ' · ' + d.phase : '') + ' · ' + mins + 'm' +
      (d.stopping ? ' · stopping after current turns…' : '');
  } else {
    $('runwrap').classList.remove('running');
    $('runbtn').classList.remove('running', 'stopping'); $('runbtn').textContent = 'run ▾';
    $('status').textContent = d.command ? ('last: ' + d.command + (d.exit_code === null ? '' : ' (exit ' + d.exit_code + ')')) : 'idle';
  }
}

const loaders = {blueprint: loadBlueprint, overview: loadOverview, agents: loadAgents,
                 prompt: loadPrompt, sources: loadSources, metrics: loadMetrics, logs: loadLogs};
async function boot() {
  PROJECT = await J('/api/project');
  $('title').textContent = 'unity — ' + PROJECT.name;
  buildRunMenu();
  loadBlueprint();
  pollRun();
  setInterval(() => {
    pollRun();
    if ($('tab-overview').style.display !== 'none') loadOverview();
    if ($('tab-logs').style.display !== 'none' && LOG_OPEN && LOG_OPEN === RUN.log && RUN.running) openLog(LOG_OPEN);
  }, 4000);
}
boot();
</script>
</body>
</html>
"""


# When a page is embedded as an app-shell tab, drop its own header so navbars don't
# nest (and can't recurse via the "← app" link).
_EMBED_SNIPPET = ("<script>if(self!==top){var _h=document.querySelector('header');"
                  "if(_h)_h.remove();}</script>")


def _embeddable(html: str) -> str:
    return html.replace("</body>", _EMBED_SNIPPET + "\n</body>")


@app.get("/", response_class=HTMLResponse)
def app_shell():
    return APP_HTML


@app.get("/forum", response_class=HTMLResponse)
def forum_page():
    return _embeddable(FORUM_HTML)


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(forum_dir: Path, root_dir: Path = Path("."), port: int = 8080) -> None:
    """Serve the dashboard against `forum_dir`/`root_dir` (runs in the caller's event loop)."""
    global FORUM_DIR, ROOT_DIR
    FORUM_DIR = Path(forum_dir)
    ROOT_DIR = Path(root_dir)
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error")
    await uvicorn.Server(config).serve()


def main() -> None:
    import asyncio
    parser = argparse.ArgumentParser(description="Unity Forum Web UI")
    parser.add_argument("--forum-dir", default="forum")
    parser.add_argument("--root-dir", default=".", help="Root directory where dag.json and Lean files live")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    asyncio.run(run(args.forum_dir, args.root_dir, args.port))


if __name__ == "__main__":
    main()
