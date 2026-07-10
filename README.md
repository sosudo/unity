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
unity new myproj --math
# ...or set up an existing Lean project (from inside it):
unity init
```

`init` interactively builds your agent roster (`.unity/agents.yaml`) — one entry per model, with
its backend (`claude_code` | `codex`), provider, credentials, and per-instance budget. Strength is
learned automatically per model across runs (autostrength); set `strength:` only to override. The
first agent is the **primary**. All Unity state lives in a gitignored `.unity/`.

### Roster examples

`.unity/agents.yaml` — one group per model; `names` spawns one agent instance per name. `${VAR}`
references resolve from the environment (or `.unity/.env`).

```yaml
agents:
# 1. Claude via your Claude subscription (claude_code backend, no credentials:
#    uses your existing `claude` CLI login). First group = the primary agent.
- names: [Ada]
  model: claude-opus-4-6
  backend: claude_code
  provider: anthropic
  budget: 10          # USD per agent instance

# 2. Claude via an Anthropic API key instead of the subscription
- names: [Grace]
  model: claude-sonnet-5
  backend: claude_code
  provider: anthropic
  api_key: ${ANTHROPIC_API_KEY}
  budget: 5

# 3. Your default Codex model (codex backend, official OpenAI auth via API key)
- names: [Kurt]
  model: gpt-5.5-codex
  backend: codex
  provider: openai
  api_key: ${OPENAI_API_KEY}

# 4. Any OpenAI-compatible provider through codex — OpenRouter
- names: [Emmy, Alan]                    # two instances of the same model
  model: qwen/qwen3-coder:free
  backend: codex
  provider: openrouter
  base_url: https://openrouter.ai/api/v1
  api_key: ${OPENROUTER_API_KEY}

# 5. FreeInference
- names: [Sophie]
  model: glm-5.1
  backend: codex
  provider: freeinference
  base_url: https://freeinference.org/v1
  api_key: ${FREEINFERENCE_API_KEY}

# 6. A self-hosted vLLM server (any key string vLLM was started with)
- names: [Henri]
  model: leanstral-24b
  backend: codex
  provider: vllm
  base_url: http://localhost:8004/v1
  api_key: unity
```

Notes: the `codex` backend always needs an `api_key` (for custom `base_url` providers it's sent as
the provider key; the endpoint must speak the OpenAI **Responses API**, which vLLM, OpenRouter, and
FreeInference all do). The `claude_code` backend accepts `api_key`, `auth_token`, and `base_url`
(`ANTHROPIC_*` equivalents) — omit all three to ride your subscription login. Mixed rosters are the
point: put your strongest model first (primary) and fill the swarm with cheap or free workers.

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
| `unity serve` | dashboard (default port 8080) |
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
