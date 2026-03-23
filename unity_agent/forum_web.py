"""Unity Forum Web UI.

A view-only web interface for the unity forum and dependency DAG.
Reads forum/*.json and dag.json directly. Started automatically by the pipeline.

    python -m unity_agent.forum_web --forum-dir ./forum --root-dir . --port 8080
"""

import argparse
import json
import math
import re
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from watchfiles import awatch

app = FastAPI(title="unity-forum")
FORUM_DIR: Path = Path("forum")
ROOT_DIR: Path = Path(".")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _hot(post: dict) -> float:
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
    return posts


def _load_thread(thread_id: str) -> dict | None:
    path = FORUM_DIR / f"{thread_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


_SORRY_RE = re.compile(r'\bsorry\b')
_ONLY_SORRY_RE = re.compile(r':=\s*sorry\s*$|by\s*\n?\s*sorry\s*$', re.MULTILINE)


def _chunk_status(chunk: dict) -> str:
    """Derive chunk status from the filesystem."""
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

    # Declaration not yet in Lean file — check for recent forum activity (yellow)
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
    threads = []
    for path in sorted(FORUM_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            last = max((p["timestamp"] for p in data["posts"]), default=data["created_at"])
            threads.append({
                "thread_id": data["thread_id"],
                "title": data["title"],
                "description": data["description"],
                "post_count": len(data["posts"]),
                "last_activity": last,
            })
        except Exception:
            continue
    return JSONResponse(threads)


@app.get("/api/threads/{thread_id}")
def get_thread(thread_id: str, sort: str = "hot"):
    data = _load_thread(thread_id)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "thread_id": data["thread_id"],
        "title": data["title"],
        "description": data["description"],
        "post_count": len(data["posts"]),
        "posts": _sorted_posts(data["posts"], sort),
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
    chunks = [
        {**c, "status": _chunk_status(c)}
        for c in dag.get("chunks", [])
    ]
    return JSONResponse({"chunks": chunks})


@app.get("/api/events")
async def events():
    async def generate():
        yield "data: connected\n\n"
        watch_paths = [str(FORUM_DIR)]
        dag_file = ROOT_DIR / "dag.json"
        if dag_file.exists():
            watch_paths.append(str(dag_file.parent))
        async for _ in awatch(*watch_paths):
            yield "data: update\n\n"
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
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; font-size: 13px; background: #fff; color: #000; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; padding: 8px 16px; border-bottom: 1px solid #000; flex-shrink: 0; }
header h1 { font-size: 13px; font-weight: bold; letter-spacing: 0.08em; }
nav a { font-size: 12px; color: #000; text-decoration: none; border-bottom: 1px solid #000; }
nav a:hover { opacity: 0.6; }
.controls { display: flex; align-items: center; gap: 16px; }
#status { font-size: 11px; color: #999; }
.sort-tabs { display: flex; }
.sort-tabs button { background: none; border: 1px solid #ccc; cursor: pointer; font: inherit; font-size: 12px; padding: 2px 10px; margin-left: -1px; }
.sort-tabs button:first-child { margin-left: 0; }
.sort-tabs button.active { background: #000; color: #fff; border-color: #000; }
main { display: flex; flex: 1; overflow: hidden; }
#sidebar { width: 180px; border-right: 1px solid #000; overflow-y: auto; flex-shrink: 0; }
.thread-item { display: flex; justify-content: space-between; align-items: baseline; padding: 7px 12px; cursor: pointer; border-bottom: 1px solid #f0f0f0; gap: 8px; }
.thread-item:hover { background: #f8f8f8; }
.thread-item.active { background: #000; color: #fff; }
.thread-item.active .count { color: #aaa; }
.thread-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.count { font-size: 11px; color: #999; flex-shrink: 0; }
#panel { flex: 1; overflow-y: auto; padding: 20px 24px; }
.thread-title { font-weight: bold; margin-bottom: 4px; }
.thread-desc { color: #666; font-size: 12px; margin-bottom: 20px; }
.post { margin-bottom: 18px; padding-bottom: 18px; border-bottom: 1px solid #ebebeb; }
.post-meta { display: flex; align-items: baseline; gap: 12px; margin-bottom: 5px; font-size: 11px; color: #888; }
.post-author { font-weight: bold; color: #000; font-size: 12px; }
.post-content { white-space: pre-wrap; line-height: 1.6; font-size: 13px; }
.post.redacted .post-content { color: #bbb; font-style: italic; }
.reply { margin-left: 20px; border-left: 2px solid #e8e8e8; padding-left: 14px; border-bottom: none; margin-bottom: 10px; padding-bottom: 0; }
#placeholder { color: #aaa; padding: 20px 0; }
</style>
</head>
<body>
<header>
  <h1>unity forum</h1>
  <div class="controls">
    <nav><a href="/dag">dag &rarr;</a></nav>
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
let currentThread = null, currentSort = 'hot';

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
  const threads = await res.json();
  const el = document.getElementById('sidebar'); el.innerHTML = '';
  threads.forEach(t => {
    const div = document.createElement('div');
    div.className = 'thread-item' + (t.thread_id === currentThread ? ' active' : '');
    div.dataset.id = t.thread_id;
    div.innerHTML = '<span class="thread-name">'+esc(t.thread_id)+'</span><span class="count">'+t.post_count+'</span>';
    div.addEventListener('click', () => loadThread(t.thread_id));
    el.appendChild(div);
  });
  if (!currentThread && threads.length > 0) loadThread(threads[0].thread_id);
}

function renderPost(p, depth) {
  const score = p.upvotes - p.downvotes;
  return '<div class="post'+(depth>0?' reply':'')+(p.redacted?' redacted':'')+'">'
    +'<div class="post-meta"><span class="post-author">'+esc(p.author)+'</span>'
    +'<span>&#8593;'+p.upvotes+' &#8595;'+p.downvotes+' ('+(score>=0?'+':'')+score+')</span>'
    +'<span>'+reltime(p.timestamp)+' ago</span>'
    +'<span style="color:#ccc;font-size:11px">#'+p.post_id+'</span></div>'
    +'<div class="post-content">'+esc(p.content)+'</div>'
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
  data.posts.forEach(p => { if (p.reply_to && byId[p.reply_to]) byId[p.reply_to]._replies.push(p); else roots.push(p); });
  const panel = document.getElementById('panel');
  panel.innerHTML = '<div class="thread-title">'+esc(data.title)+'</div>'
    +(data.description?'<div class="thread-desc">'+esc(data.description)+'</div>':'')
    +(roots.length===0?'<div id="placeholder">no posts yet</div>':roots.map(p=>renderPost(p,0)).join(''));
}

function connect() {
  const es = new EventSource('/api/events');
  es.onopen = () => { document.getElementById('status').textContent = 'live'; };
  es.onmessage = () => { loadSidebar(); if (currentThread) loadThread(currentThread); };
  es.onerror = () => { document.getElementById('status').textContent = 'reconnecting...'; es.close(); setTimeout(connect, 3000); };
}

const hash = location.hash.slice(1);
if (hash) { currentThread = hash; }
loadSidebar(); connect();
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
nav a { font-size: 12px; color: #000; text-decoration: none; border-bottom: 1px solid #000; }
nav a:hover { opacity: 0.6; }
.controls { display: flex; align-items: center; gap: 16px; }
#status { font-size: 11px; color: #999; }
main { display: flex; flex: 1; overflow: hidden; position: relative; }
#cy { flex: 1; }
#info-panel {
  width: 280px;
  border-left: 1px solid #000;
  overflow-y: auto;
  padding: 16px;
  flex-shrink: 0;
  display: none;
}
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
    <nav><a href="/">&larr; forum</a></nav>
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

let cy = null;
let chunks = {};

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
      {
        selector: 'node',
        style: {
          'background-color': 'data(bgColor)',
          'border-color': 'data(borderColor)',
          'border-width': 1.5,
          'label': 'data(label)',
          'font-family': 'monospace',
          'font-size': '11px',
          'text-valign': 'center',
          'text-halign': 'center',
          'text-wrap': 'wrap',
          'width': 'label',
          'height': 'label',
          'padding': '10px',
          'shape': 'roundrectangle',
          'color': '#000',
        }
      },
      {
        selector: 'node:selected',
        style: { 'border-width': 2.5, 'border-color': '#000' }
      },
      {
        selector: 'edge',
        style: {
          'curve-style': 'bezier',
          'target-arrow-shape': 'triangle',
          'target-arrow-color': '#bbb',
          'line-color': '#bbb',
          'arrow-scale': 0.8,
          'width': 1,
        }
      },
    ],
    layout: { name: 'dagre', rankDir: 'TB', nodeSep: 40, rankSep: 70, padding: 30 },
  });

  cy.on('tap', 'node', e => showPanel(e.target.id()));
  cy.on('tap', e => { if (e.target === cy) closePanel(); });
}

function updateColors(data) {
  if (!cy) return;
  data.chunks.forEach(c => {
    const node = cy.getElementById(c.id);
    if (!node.length) return;
    const col = STATUS_COLOR[c.status] || STATUS_COLOR.grey;
    node.style({ 'background-color': col.bg, 'border-color': col.border });
    node.data({ status: c.status, bgColor: col.bg, borderColor: col.border });
    chunks[c.id] = c;
  });
  // refresh open panel if needed
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
