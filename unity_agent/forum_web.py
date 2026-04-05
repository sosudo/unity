"""Unity Forum Web UI.

A view-only web interface for the unity forum and dependency DAG.
Reads forum/*.json and dag.json directly. Started automatically by the pipeline.

    python -m unity_agent.forum_web --forum-dir ./forum --root-dir . --port 8080
"""

import argparse
import asyncio
import json
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


_DEFAULT_DIMENSIONS = [
    "correctness", "faithfulness", "style_alignment",
    "priority", "confidence", "feasibility",
]


def _load_config() -> dict:
    path = FORUM_DIR / "config.json"
    if not path.exists():
        return {"dimensions": {"active": list(_DEFAULT_DIMENSIONS), "pending": {}}, "tags": {}}
    try:
        cfg = json.loads(path.read_text())
        if not cfg.get("dimensions", {}).get("active"):
            cfg.setdefault("dimensions", {})["active"] = list(_DEFAULT_DIMENSIONS)
        return cfg
    except Exception:
        return {"dimensions": {"active": list(_DEFAULT_DIMENSIONS), "pending": {}}, "tags": {}}


_SORRY_RE = re.compile(r'\bsorry\b')
_ONLY_SORRY_RE = re.compile(r':=\s*sorry\s*$|by\s*\n?\s*sorry\s*$', re.MULTILINE)


def _chunk_status(chunk: dict) -> str:
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
    thread_path = FORUM_DIR / f"{chunk.get('id', '')}.json"
    if thread_path.exists():
        try:
            data = json.loads(thread_path.read_text())
            if any(time.time() - p["timestamp"] < 300 for p in data["posts"]):
                return "yellow"
        except Exception:
            pass
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
    chunks = [{**c, "status": _chunk_status(c)} for c in dag.get("chunks", [])]
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

FORUM_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>unity forum</title>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; font-size: 13px; background: #fff; color: #000; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 8px 16px; border-bottom: 1px solid #000; flex-shrink: 0; }
header h1 { font-size: 13px; font-weight: bold; letter-spacing: 0.08em; }
nav { display: flex; gap: 12px; }
nav a { font-size: 12px; color: #000; text-decoration: none; border-bottom: 1px solid #000; }
nav a:hover { opacity: 0.6; }
.controls { display: flex; align-items: center; gap: 16px; }
#status { font-size: 11px; color: #999; }
.sort-tabs { display: flex; }
.sort-tabs button { background: none; border: 1px solid #ccc; cursor: pointer; font: inherit; font-size: 12px; padding: 2px 10px; margin-left: -1px; }
.sort-tabs button:first-child { margin-left: 0; }
.sort-tabs button.active { background: #000; color: #fff; border-color: #000; }
main { display: flex; flex: 1; overflow: hidden; }
#sidebar { width: 190px; border-right: 1px solid #000; overflow-y: auto; flex-shrink: 0; }
.sidebar-section { font-size: 10px; letter-spacing: 0.08em; color: #999; padding: 8px 12px 4px; text-transform: uppercase; border-bottom: 1px solid #f0f0f0; }
.thread-item { display: flex; justify-content: space-between; align-items: baseline; padding: 7px 12px; cursor: pointer; border-bottom: 1px solid #f0f0f0; gap: 8px; }
.thread-item:hover { background: #f8f8f8; }
.thread-item.active { background: #000; color: #fff; }
.thread-item.active .count { color: #aaa; }
.thread-item.pinned { background: #f0f4ff; border-left: 3px solid #2255cc; }
.thread-item.pinned.active { background: #2255cc; }
.thread-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.count { font-size: 11px; color: #999; flex-shrink: 0; }
.tag-sidebar-item { display: flex; justify-content: space-between; padding: 5px 12px; cursor: pointer; border-bottom: 1px solid #f8f8f8; font-size: 11px; color: #555; }
.tag-sidebar-item:hover { background: #f8f8f8; }
#panel { flex: 1; overflow-y: auto; padding: 20px 24px; }
.thread-title { font-weight: bold; margin-bottom: 4px; }
.thread-desc { color: #666; font-size: 12px; margin-bottom: 8px; }
.dim-bar { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
.dim-chip { font-size: 10px; background: #f0f4ff; color: #2255cc; padding: 1px 6px; border-radius: 10px; }
.post { margin-bottom: 18px; padding-bottom: 18px; border-bottom: 1px solid #ebebeb; }
.post-meta { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 5px; font-size: 11px; color: #888; }
.post-author { font-weight: bold; color: #000; font-size: 12px; }
.post-content { line-height: 1.6; font-size: 13px; }
.post-content p { margin-bottom: 0.6em; }
.post-content p:last-child { margin-bottom: 0; }
.post-content pre { background: #f6f6f6; padding: 8px 10px; overflow-x: auto; margin: 0.5em 0; }
.post-content code { background: #f0f0f0; padding: 1px 4px; font-size: 12px; }
.post-content pre code { background: none; padding: 0; }
.post-content ul, .post-content ol { padding-left: 1.4em; margin-bottom: 0.5em; }
.post-content blockquote { border-left: 3px solid #ddd; margin: 0 0 0.5em; padding-left: 10px; color: #666; }
.post.redacted .post-content { color: #bbb; font-style: italic; white-space: pre-wrap; }
.mention { background: #f0f4ff; color: #2255cc; padding: 1px 3px; border-radius: 2px; font-weight: bold; }
.post-id-link { color: #ccc; font-size: 11px; text-decoration: none; }
.post-id-link:hover { color: #888; }
.reply-to-link { color: #aaa; font-size: 11px; text-decoration: none; }
.reply-to-link:hover { color: #555; }
.post-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.tag-chip { font-size: 10px; background: #fff8e6; color: #a06000; border: 1px solid #e8d090; padding: 1px 6px; border-radius: 10px; cursor: pointer; }
.tag-chip:hover { background: #ffe8a0; }
.dim-inline { font-size: 10px; color: #aaa; white-space: nowrap; }
.dim-inline-name { color: #bbb; margin-right: 1px; }
.dim-inline .up { color: #2d7a2d; }
.dim-inline .down { color: #c02020; }
.reply { margin-left: 20px; border-left: 2px solid #e8e8e8; padding-left: 14px; border-bottom: none; margin-bottom: 10px; padding-bottom: 0; }
#placeholder { color: #aaa; padding: 20px 0; }
</style>
</head>
<body>
<header>
  <h1>unity forum</h1>
  <div class="controls">
    <nav>
      <a href="/graph">graph &rarr;</a>
      <a href="/dag">dag &rarr;</a>
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
<title>unity graph</title>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; font-size: 13px; background: #fff; color: #000; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 8px 16px; border-bottom: 1px solid #000; flex-shrink: 0; }
header h1 { font-size: 13px; font-weight: bold; letter-spacing: 0.08em; }
nav { display: flex; gap: 12px; }
nav a { font-size: 12px; color: #000; text-decoration: none; border-bottom: 1px solid #000; }
nav a:hover { opacity: 0.6; }
.controls { display: flex; align-items: center; gap: 12px; }
#status { font-size: 11px; color: #999; }
.color-tabs { display: flex; }
.color-tabs button { background: none; border: 1px solid #ccc; cursor: pointer; font: inherit; font-size: 12px; padding: 2px 10px; margin-left: -1px; }
.color-tabs button:first-child { margin-left: 0; }
.color-tabs button.active { background: #000; color: #fff; border-color: #000; }
main { display: flex; flex: 1; overflow: hidden; position: relative; }
#cy { flex: 1; }
#info-panel { width: 300px; border-left: 1px solid #000; overflow-y: auto; padding: 16px; flex-shrink: 0; display: none; }
#info-panel.visible { display: block; }
#info-close { float: right; cursor: pointer; font-size: 16px; line-height: 1; }
#info-close:hover { opacity: 0.5; }
.info-field { margin-bottom: 10px; }
.info-label { font-size: 10px; color: #999; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 3px; }
.info-value { line-height: 1.5; }
.info-value.md { font-size: 12px; }
.info-value.md p { margin-bottom: 0.4em; }
.info-value.md code { background: #f0f0f0; padding: 1px 3px; font-size: 11px; }
.info-value.md pre { background: #f6f6f6; padding: 6px 8px; overflow-x: auto; font-size: 11px; margin: 0.3em 0; }
.info-value.md pre code { background: none; padding: 0; }
.info-link { color: #000; text-decoration: underline; }
.tag-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.tag-chip { font-size: 10px; background: #fff8e6; color: #a06000; border: 1px solid #e8d090; padding: 1px 6px; border-radius: 10px; }
.dim-row { display: flex; gap: 8px; font-size: 11px; margin-bottom: 2px; }
.dim-name { color: #555; min-width: 110px; }
.dim-up { color: #2d7a2d; }
.dim-down { color: #c02020; }
#legend { position: absolute; bottom: 12px; left: 12px; background: rgba(255,255,255,0.95); border: 1px solid #ddd; padding: 8px 12px; font-size: 11px; max-width: 180px; pointer-events: none; }
#legend-title { font-weight: bold; margin-bottom: 4px; color: #555; }
.legend-row { display: flex; align-items: center; gap: 6px; margin-bottom: 2px; overflow: hidden; }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.legend-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
</head>
<body>
<header>
  <h1>unity graph</h1>
  <div class="controls">
    <nav>
      <a href="/">&larr; forum</a>
      <a href="/dag">dag &rarr;</a>
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
        'border-color': '#00000044',
        'border-width': 1,
        'label': 'data(label)',
        'font-family': 'monospace',
        'font-size': 9,
        'text-valign': 'center',
        'text-halign': 'center',
        'text-wrap': 'ellipsis',
        'text-max-width': 68,
        'width': 76,
        'height': 24,
        'shape': 'roundrectangle',
        'color': '#000',
        'min-zoomed-font-size': 6,
      }},
      { selector: 'node:selected', style: { 'border-width': 2.5, 'border-color': '#000', 'border-opacity': 1 }},
      { selector: 'node:active', style: { 'overlay-opacity': 0.1 }},
      { selector: 'node[?redacted]', style: { 'opacity': 0.4 }},
      { selector: 'edge', style: {
        'curve-style': 'bezier',
        'target-arrow-shape': 'triangle',
        'target-arrow-color': '#bbb',
        'line-color': '#bbb',
        'arrow-scale': 0.7,
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
<title>unity dag</title>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; font-size: 13px; background: #fff; color: #000; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 8px 16px; border-bottom: 1px solid #000; flex-shrink: 0; }
header h1 { font-size: 13px; font-weight: bold; letter-spacing: 0.08em; }
nav { display: flex; gap: 12px; }
nav a { font-size: 12px; color: #000; text-decoration: none; border-bottom: 1px solid #000; }
nav a:hover { opacity: 0.6; }
.controls { display: flex; align-items: center; gap: 16px; }
#status { font-size: 11px; color: #999; }
main { display: flex; flex: 1; overflow: hidden; position: relative; }
#cy { flex: 1; }
#info-panel { width: 280px; border-left: 1px solid #000; overflow-y: auto; padding: 16px; flex-shrink: 0; display: none; }
#info-panel.visible { display: block; }
#info-close { float: right; cursor: pointer; font-size: 16px; line-height: 1; }
#info-close:hover { opacity: 0.5; }
.info-field { margin-bottom: 12px; }
.info-label { font-size: 11px; color: #888; margin-bottom: 2px; }
.info-value { white-space: pre-wrap; line-height: 1.5; }
.info-link { color: #000; }
.status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
#legend { position: absolute; bottom: 12px; left: 12px; background: #fff; border: 1px solid #ddd; padding: 8px 12px; font-size: 11px; line-height: 2; pointer-events: none; }
.legend-row { display: flex; align-items: center; gap: 6px; }
#waiting { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); color: #aaa; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>unity dag</h1>
  <div class="controls">
    <nav>
      <a href="/">&larr; forum</a>
      <a href="/graph">graph &rarr;</a>
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
    <div class="legend-row"><span class="status-dot" style="background:#e8e8e8;border:1px solid #999"></span>not started</div>
    <div class="legend-row"><span class="status-dot" style="background:#fff3a0;border:1px solid #c8a000"></span>in progress</div>
    <div class="legend-row"><span class="status-dot" style="background:#c8f0c8;border:1px solid #2d7a2d"></span>fully formalized</div>
    <div class="legend-row"><span class="status-dot" style="background:#c8e0f8;border:1px solid #1a5fa0"></span>partially formalized</div>
    <div class="legend-row"><span class="status-dot" style="background:#f8c8c8;border:1px solid #c02020"></span>by sorry</div>
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

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function buildGraph(data) {
  chunks = {};
  data.chunks.forEach(c => { chunks[c.id] = c; });
  const elements = [];
  data.chunks.forEach(c => {
    const col = STATUS_COLOR[c.status] || STATUS_COLOR.grey;
    elements.push({ data: {
      id: c.id,
      label: c.id + (c.title && c.title !== c.id ? '\\n' + c.title.substring(0,24) : ''),
      status: c.status,
      bgColor: col.bg,
      borderColor: col.border,
    }});
    (c.dependencies || []).forEach(dep => {
      elements.push({ data: { id: dep+'->'+c.id, source: dep, target: c.id }});
    });
  });
  if (cy) cy.destroy();
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements,
    style: [
      { selector: 'node', style: {
        'background-color': 'data(bgColor)', 'border-color': 'data(borderColor)', 'border-width': 1.5,
        'label': 'data(label)', 'font-family': 'monospace', 'font-size': '11px',
        'text-valign': 'center', 'text-halign': 'center', 'text-wrap': 'wrap',
        'width': 'label', 'height': 'label', 'padding': '10px', 'shape': 'roundrectangle', 'color': '#000',
      }},
      { selector: 'node:selected', style: { 'border-width': 2.5, 'border-color': '#000' }},
      { selector: 'edge', style: {
        'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
        'target-arrow-color': '#bbb', 'line-color': '#bbb', 'arrow-scale': 0.8, 'width': 1,
      }},
    ],
    layout: { name: 'dagre', rankDir: 'TB', nodeSep: 40, rankSep: 70, padding: 30 },
  });
  cy.on('tap', 'node', e => showPanel(e.target.id()));
  cy.on('tap', e => { if (e.target === cy) closePanel(); });
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
    +'<div class="info-field"><div class="info-label">dependencies</div><div class="info-value">'+esc((c.dependencies||[]).join(', ')||'none')+'</div></div>'
    +'<div class="info-field"><div class="info-label">forum</div><div class="info-value"><a class="info-link" href="/#'+esc(c.id)+'" target="_blank">'+esc(c.id)+' &rarr;</a></div></div>';
  document.getElementById('info-panel').classList.add('visible');
}

function closePanel() {
  openId = null;
  document.getElementById('info-panel').classList.remove('visible');
  if (cy) cy.$(':selected').unselect();
}

let initialized = false;
async function loadDag(forceRebuild) {
  const res = await fetch('/api/dag');
  const waiting = document.getElementById('waiting');
  if (!res.ok) { waiting.style.display='block'; return; }
  waiting.style.display = 'none';
  const data = await res.json();
  if (!initialized || forceRebuild) { buildGraph(data); initialized = true; }
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

@app.get("/", response_class=HTMLResponse)
def forum():
    return FORUM_HTML


@app.get("/graph", response_class=HTMLResponse)
def graph():
    return GRAPH_HTML


@app.get("/dag", response_class=HTMLResponse)
def dag():
    return DAG_HTML


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global FORUM_DIR, ROOT_DIR
    parser = argparse.ArgumentParser(description="Unity Forum Web UI")
    parser.add_argument("--forum-dir", default="forum")
    parser.add_argument("--root-dir", default=".", help="Root directory where dag.json and Lean files live")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    FORUM_DIR = Path(args.forum_dir)
    ROOT_DIR = Path(args.root_dir)
    FORUM_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="error")


if __name__ == "__main__":
    main()
