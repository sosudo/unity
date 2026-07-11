# Unity

Multi-agent proving, solving, autoformalization, formalization, and optimization harness for Lean 4.

A roster of heterogeneous agents (different models, providers, and backends — Claude Agent SDK or
Codex) collaborates through a typed shared workspace: agents claim chunks of work, attack them in
per-agent git worktrees, endorse or object to each other's results, and consensus-merge winners
into your project.

## Prerequisites

- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- [Lean 4](https://lean-lang.org/) with `lake` (and `elan`)
- API credentials for the models you want to run (Anthropic, OpenRouter, OpenAI, ...)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/sosudo/unity/main/install.sh | sh
```

or manually:

```bash
git clone https://github.com/sosudo/unity && cd unity
uv tool install .
```

## Quick start

```bash
# Create a new Lean project (with Mathlib) and set it up for Unity:
unity new myproj --math    # --version <toolchain> to pin a Lean version
# ...or set up an existing Lean project (from inside it):
unity init

# Then, FROM INSIDE THE PROJECT, open the control center:
cd myproj
unity serve        # → http://localhost:8080
```

**`unity serve` is the intended way to use Unity.** It opens a full control center for the
project:

- **blueprint** (home) — the project's actual Lean structure: every declaration with its proof
  status (kernel-verified when the project builds), dependencies, list and graph views, click any
  declaration for its signature, source, and chunk.
- **run ▾** — launch any pipeline (`prove`, `solve`, `autoformalize`, …) with target/metric/version
  pickers and automatic `--continue` detection; the button becomes a **stop** button while running
  (safe stop: agents finish their current turn, then the run winds down).
- **overview / forum / chunks** — the live typed workspace (decisions, consensus, obstacles,
  ledger, tool usage), the discussion threads (with an in-place graph view), and the chunk DAG,
  auto-refreshing as agents work.
- **agents / prompt / sources / metrics / logs** — edit `agents.yaml` (form or raw, kept in sync),
  `UNITY.md`, source material, and optimization metrics; tail timestamped run logs live. The ⚙
  settings button edits `.unity/.env` (run flags, Axle/Aristotle keys).

Everything below is also available as plain CLI commands if you prefer the terminal.

`init` scaffolds a gitignored `.unity/` (empty roster, prompt, metrics, env) and warms the build.
Configure your roster in the **agents tab** of `unity serve` — one-click presets cover the common
setups (Claude subscription, OpenAI/Codex, OpenRouter, FreeInference, local vLLM), or edit the
YAML directly. Mark your strongest model with **set as primary**: the primary agent leads the run —
it prepares context, acts as the critic, merges consensus results, and writes the retrospective
(default: the first group). Strength is learned automatically per model across runs (autostrength);
set `strength:` only to override.

### Roster examples

`.unity/agents.yaml` — one entry per agent. `${VAR}` references resolve from the environment (or
`.unity/.env`). The agents tab in `unity serve` builds this for you (with presets).

```yaml
agents:
# 1. Claude via your Claude subscription (no credentials: uses your `claude` CLI
#    login). primary: true = leads the run (default: the first agent).
- name: Ada
  model: claude-opus-4-6
  backend: anthropic       # anthropic | openai (which API the agent speaks)
  primary: true
  budget: 10               # USD for this agent

# 2. Claude via an Anthropic API key instead of the subscription
- name: Grace
  model: claude-sonnet-5
  backend: anthropic
  api_key: ${ANTHROPIC_API_KEY}
  budget: 5

# 3. Codex via your ChatGPT/Codex subscription (no credentials: uses `codex login`)
- name: Kurt
  model: gpt-5.5-codex
  backend: openai

# 4. Codex via an OpenAI API key
- name: Karl
  model: gpt-5.5-codex
  backend: openai
  api_key: ${OPENAI_API_KEY}

# 5. Claude through OpenRouter (Anthropic wire; the OpenRouter key rides as auth_token)
- name: Emmy
  model: anthropic/claude-sonnet-5
  backend: anthropic
  base_url: https://openrouter.ai/api
  auth_token: ${OPENROUTER_API_KEY}

# 6. Any non-Claude OpenRouter model (OpenAI wire)
- name: Alan
  model: qwen/qwen3-coder:free
  backend: openai
  base_url: https://openrouter.ai/api/v1
  api_key: ${OPENROUTER_API_KEY}

# 7. FreeInference
- name: Sophie
  model: glm-5.1
  backend: openai
  base_url: https://freeinference.org/v1
  api_key: ${FREEINFERENCE_API_KEY}

# 8. A self-hosted vLLM server (any key string vLLM was started with)
- name: Henri
  model: leanstral-24b
  backend: openai
  base_url: http://localhost:8004/v1
  api_key: unity
```

Notes: `backend` says which API the agent speaks — `anthropic` (Claude Code runtime) or `openai`
(Codex runtime); the legacy values `claude_code`/`codex` still work. With no credentials, an agent
rides your local subscription login (`claude` or `codex login`). `openai` agents with a custom
`base_url` need an `api_key`, and the endpoint must speak the OpenAI **Responses API** (vLLM,
OpenRouter, and FreeInference all do); `anthropic` agents take `api_key`/`auth_token`/`base_url`
(`ANTHROPIC_*` equivalents). Mixed rosters are the point: mark your strongest model
`primary: true` and fill the swarm with cheap or free workers.

Then, from inside the project:

| Command | Flags | What it does |
|---|---|---|
| `unity autoformalize` | `--continue` | whole paper/book (in `.unity/source/`) → Lean, faithfully |
| `unity formalize` | `--targets <scope>`, `--continue` | formalize source material into an existing project's gaps |
| `unity prove` | `--targets <scope>`, `--continue` | fill in the project's `sorry`s and `axiom`s |
| `unity solve` | `--continue` | solve a natural-language problem from `UNITY.md`, then formalize the proof |
| `unity create` | `--continue` | build a Lean library from a natural-language description in `UNITY.md` |
| `unity verify` | `--targets <scope>`, `--continue` | program verification: model code from `.unity/source/`, prove properties |
| `unity bump [version]` | `--continue` | migrate to a Lean/Mathlib version (e.g. `v4.31.0`; omit to use `UNITY.md`'s target) |
| `unity optimize <metric>` | `--targets <scope>`, `--continue` | improve Lean code w.r.t. a metric (`length`, `modularity`, ...) |
| `unity agent` / `unity doctor` | — | interactive session / interactive resolver with the primary agent |
| `unity serve` | `--port <n>` (default 8080) | **the control center** (see Quick start) |
| `unity mcp <server> <tool> [json]` | — | call any agent MCP tool from the shell (e.g. `unity mcp unity-forum forum_stats '{}'`) |
| `unity source add <path>` / `remove <name>` / `list` | — | manage source material in `.unity/source/` |
| `unity metric add\|modify\|remove <name>` / `move <file>` / `list` | — | manage optimization metrics in `.unity/metrics/` |
| `unity reset` / `unity clean` | — | wipe / prune the global library (`~/.unity/library/`) |
| `unity complete` | — | remove Unity artifacts from a finished project |
| `unity update` / `unity uninstall` | — | manage the installation |

`--targets` narrows a run's scope (declaration/chunk names or a description; default: everything in scope). `--continue` re-orients from the previous run's state before continuing — the web UI sets it automatically when prior state exists. Fresh (non-`--continue`) runs start with a bootstrap step that adds LeanArchitect when a toolchain-matching release exists.

## Common workflows

Everything below happens in `unity serve` — set up once (agents tab → your roster; prompt tab →
your goal in `UNITY.md`), then:

- **Autoformalize a paper/book from scratch** — upload the source (PDF/tex/markdown) in the
  **sources** tab, add any special instructions in **prompt**, then **run → autoformalize**. The
  swarm semiformalizes it into a chunk DAG and formalizes chunk by chunk, faithfully.
- **Fill in missing proofs (`sorry`s / `axiom`s)** — optionally add reference material under
  **sources** and guidance in **prompt**, then **run → prove** (narrow with targets if you only
  want specific declarations).
- **Formalize parts of a paper into an existing project** — upload the paper in **sources**, say
  which parts go where in **prompt**, then **run → formalize**.
- **Solve an open problem end-to-end** — state the problem in **prompt**, **run → solve**: the
  team writes a rigorous LaTeX proof first, then formalizes it.
- **Build a new Lean library from a description** — describe the library in **prompt**,
  **run → create**: spec first, then implementation.
- **Verify a program** — put the code in **sources**, the properties in **prompt**,
  **run → verify**.
- **Migrate Lean/Mathlib versions** — **run → bump** with the target version.
- **Make the code better** — pick/edit a metric in **metrics** (length, modularity, …), set it
  active, **run → optimize**.

Watch progress live in **blueprint** (proof status per declaration), **overview** (decisions,
consensus, obstacles), and **logs**; stop safely anytime with the run button.

## Configuration

- `.unity/.env` — run flags: `MAX_ATTEMPTS` (critic-loop cap, default 5), `UNITY_FORUM_BRIEF=off`
  (disable workspace-brief injection), and optional service keys (`AXLE_API_KEY`,
  `ARISTOTLE_API_KEY`) that unlock extra agent tools.
- `.unity/agents.yaml` — the roster. Per-agent credentials (`api_key` / `auth_token` / `base_url`)
  live here, not in `.env`; `${VAR}` references are resolved from the environment. The `codex`
  backend requires `api_key`.
- `~/.unity/library/` — the global library (tactics, lemmas, references, skills, subagents) that
  every agent sees and the retrospective phase grows across runs.
