# Unity

Multi-agent proving, solving, autoformalization, formalization, and optimization harness for Lean 4.

A roster of heterogeneous agents (different models, providers, and backends — Claude Agent SDK or
Codex) collaborates through a persistent forum: agents sign up for chunks of work, attack them in
per-agent git worktrees, vote on candidate solutions, and consensus-merge winners into your project.

## Prerequisites

- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- [Lean 4](https://lean-lang.org/) with `lake` (and `elan`)
- API credentials for the models you want to run (Anthropic, OpenRouter, OpenAI, ...)

## Install

```bash
git clone <repository-url> unity && cd unity
uv tool install .
```

## Quick start

```bash
# Create a new Lean project (with Mathlib) and set it up for Unity:
unity new myproj --math
# ...or set up an existing Lean project (from inside it):
unity init
```

`init` interactively builds your agent roster (`.unity/agents.yaml`) — one entry per model, with
its backend (`claude_code` | `codex`), provider, credentials, `strength` tier, and per-instance
budget. The first agent is the **primary**. All Unity state lives in a gitignored `.unity/`.

Then, from inside the project:

| Command | What it does |
|---|---|
| `unity autoformalize` | whole paper/book (in `.unity/source/`) → Lean, faithfully |
| `unity formalize` | formalize source material into an existing project's gaps |
| `unity prove` | fill in the project's `sorry`s and `axiom`s |
| `unity solve` | solve a natural-language problem from `UNITY.md`, then formalize the proof |
| `unity create` | build a Lean library from a natural-language description in `UNITY.md` |
| `unity verify` | program verification: model code from `.unity/source/`, prove properties |
| `unity bump` | migrate the project to a target Lean/Mathlib version |
| `unity optimize <metric>` | improve Lean code w.r.t. a metric (`length`, `modularity`, ...) |
| `unity agent` / `unity doctor` | interactive session / interactive resolver with the primary agent |
| `unity serve` | forum + DAG dashboard (default port 8080) |
| `unity source add\|remove\|list` | manage source material in `.unity/source/` |
| `unity metric add\|modify\|move\|list` | manage optimization metrics in `.unity/metrics/` |
| `unity reset` / `unity clean` | wipe / prune the global library (`~/.unity/library/`) |
| `unity complete` | remove Unity artifacts from a finished project |
| `unity update` / `unity uninstall` | manage the installation |

Most pipeline commands accept `--continue` (re-orient from the previous run before continuing).

## Configuration

- `.unity/.env` — run flags (`RECORDING`, `SILENT`, `FORUM_PORT`, `LEAN_LSP_PORT`, `MAX_ATTEMPTS`)
  and optional service keys (`AXLE_API_KEY`, `ARISTOTLE_API_KEY`) that unlock extra agent tools.
- `.unity/agents.yaml` — the roster. Per-agent credentials (`api_key` / `auth_token` / `base_url`)
  live here, not in `.env`; `${VAR}` references are resolved from the environment. The `codex`
  backend requires `api_key`.
- `~/.unity/library/` — the global library (tactics, lemmas, references, skills, subagents) that
  every agent sees and the retrospective phase grows across runs.
