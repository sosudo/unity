# Unity

A multi-agentic harness for Lean 4.

Unity uses a roster of heterogeneous agents (different models) to work on Lean projects collaboratively.

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
# Create and enter a new Lean project (with Mathlib) with Unity setup:
unity new [project] --math    # --version <toolchain> to pin a Lean version
cd [project]
# ...or set up an existing Lean project to work with Unity (from inside it):
cd [project]
unity init

# Then, from inside the project, open the control center:
unity serve        # → http://localhost:8080
```

`unity serve` will launch the dashboard, where you will be able to do anything you'd want to do with Unity! Alternatively, there is a `unity` cli tool, the commands of which are described in the table in the [CLI Commands](#cli-commands) section.

Unity has 8 core commands: 
- [`autoformalize`](#autoformalize) to automatically formalize a whole paper/book into a newly created or empty Lean project
- [`formalize`](#formalize) to formalize natural language content into missing sections of an existing Lean project
- [`prove`](#prove) to fill in target `sorry`s and `axiom`s automatically
- [`solve`](#solve) to solve a natural language problem first and then formalize the solution in Lean
- [`create`](#create) to build a Lean library from a natural language description
- [`verify`](#verify) for program verification of some source code
- [`bump`](#bump) to migrate a Lean project to a different version
- [`optimize`](#optimize) to improve Lean code with respect to some metric (re: [ImProver](https://github.com/riyazahuja/ImProver))

The below subsections will get you quickly started with using any of the commands, so you can hit the ground running with Unity! (You can also click on the command you want to run above...)

### Autoformalize

First, go to the sources tab and add the documents you want formalized.

Next, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Make sure to save your agents before continuing!

Then, go to the prompt tab, and type in any specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). No need to tell the agents that they're autoformalizing anything, Unity's pipeline will handle that for you. Again, remember to save before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the autoformalization loop are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with autoformalization.

Once you're ready, hover over the `run` button, press `autoformalize`, and press the `start` button!

### Formalize

First, go to the sources tab and add the documents you want formalized.

Next, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Make sure to save your agents before continuing!

Then, go to the prompt tab, and type in any specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). No need to tell the agents that they're formalizing anything, Unity's pipeline will handle that for you. Again, remember to save before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the formalization loop are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with formalization.

When you're ready, hover over the `run` button, press `formalize`, and press the `start` button. In the `targets` box, you can put in any specific Lean declarations you want fixed or specific theorems/lemmas/definitions/section from your sources you want formalized (you can also leave it blank and the agents will treat everything as a target).

### Prove

First, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Make sure to save your agents before continuing!

Then, go to the prompt tab, and type in any specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). No need to tell the agents that they're proving anything, Unity's pipeline will handle that for you. Again, remember to save before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the proving loop are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with proving.

When you're ready, hover over the `run` button, press `prove`, and press the `start` button. In the `targets` box, you can put in any specific Lean declarations you want proven (you can also leave it blank and the agents will treat every `sorry` and `axiom` as a target).

### Solve

First, go to the sources tab and add any documents with the problem you're trying to solve or any auxiliary information. If you don't have any documents, it's ok to skip this step!

Then, go to the prompt tab, and type in an overview of your problem and any specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). Also, if you added sources in the previous step, make sure you add something in the prompt saying there are resources in the sources for the agents to use. No need to tell the agents that they're solving anything, Unity's pipeline will handle that for you. Remember to save before continuing!

Next, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Again, make sure to save your agents before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the solving and formalization loops are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with formalization.

When you're ready, hover over the `run` button, press `solve`, and press the `start` button.

### Create

First, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Make sure to save your agents before continuing!

Then, go to the prompt tab, and type in a description of the library you want created and any other specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). No need to tell the agents that they're creating anything, Unity's pipeline will handle that for you. Again, remember to save before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the creating loop are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with creating.

When you're ready, hover over the `run` button, press `create`, and press the `start` button.

### Verify

First, go to the sources tab and add the code sources you want verified.

Next, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Make sure to save your agents before continuing!

Then, go to the prompt tab, and type in any specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). No need to tell the agents that they're verifying anything, Unity's pipeline will handle that for you. Again, remember to save before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the verification loop are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with verifying.

Once you're ready, hover over the `run` button, press `verify`, and press the `start` button! In the `targets` box, you can put in specific functions/files from your source code you want verified (you can also leave it blank and the agents will treat everything as a target).

### Bump

First, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Make sure to save your agents before continuing!

Then, go to the prompt tab, and type in any other specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). No need to tell the agents that they're bumping anything, Unity's pipeline will handle that for you. Again, remember to save before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the bumping loop are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with bumping.

When you're ready, hover over the `run` button, press `bump`, put in your target Lean version, and press the `start` button.

### Optimize

First, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Make sure to save your agents before continuing!

Then, go to the prompt tab, and type in any specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). No need to tell the agents that they're optimizing anything, Unity's pipeline will handle that for you. Again, remember to save before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the optimizing loop are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with optimizing.

When you're ready, hover over the `run` button, press `optimize`, set the metric you want to optimize for, and press the `start` button. In the `targets` box, you can put in any specific Lean declarations you want optimized (you can also leave it blank and the agents will treat all declarations as targets). If you want to edit the existing metrics or add new metrics, go to the metrics tab!

## CLI Commands

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

## Roster Configuration

Your roster lives in `.unity/agents.yaml` — one entry per agent. The easiest way to build it is the agents tab in the webview (presets + a form), but here's what the fields mean if you're filling them in yourself:

| Field | What it is |
|---|---|
| `name` | the agent's name (each agent needs a unique one) |
| `model` | the model this agent runs (e.g. `claude-opus-4-6`, `gpt-5.5-codex`, `qwen/qwen3-coder:free`) |
| `backend` | which API the agent speaks: `anthropic` (Claude Code runtime) or `openai` (Codex runtime) |
| `primary` | `true` marks this agent as the primary (defaults to the first agent) |
| `budget` | USD cap for this agent per run (optional; only enforced on the `anthropic` backend) |
| `base_url` | a custom endpoint, for providers like OpenRouter, FreeInference, or a self-hosted vLLM server (optional) |
| `api_key` / `auth_token` | credentials for the model (optional — see below); `${VAR}` references resolve from your environment or `.unity/.env` |

The **primary** agent leads the run: it prepares context on continuations, acts as the critic, merges consensus results, and writes the retrospective — so make it your strongest model.

A few rules of thumb for credentials:
- **No credentials at all?** The agent rides your local subscription login — `claude` login for `anthropic` agents, `codex login` for `openai` agents. This is the cheapest way to get started!
- **`openai` agents with a custom `base_url`** (OpenRouter, FreeInference, vLLM, ...) need an `api_key`, and the endpoint must speak the OpenAI Responses API (all three of those do).
- **`anthropic` agents** take `api_key`/`auth_token`/`base_url` (they map to the `ANTHROPIC_*` env vars) — notably, Claude models through OpenRouter go on the `anthropic` backend with your OpenRouter key as the `auth_token`.

You may also set `strength` (a capability tier used for chunk allocation) on any agent, but you usually shouldn't: Unity learns per-model strengths automatically across runs (autostrength) and an explicit value just overrides the learned one.

Here's a full example showing every setup the presets cover:

```yaml
agents:
- name: Ada                    # Claude via your Claude Code subscription
  model: claude-opus-4-6
  backend: anthropic
  primary: true
  budget: 10

- name: Grace                  # Claude via an Anthropic API key
  model: claude-sonnet-5
  backend: anthropic
  api_key: ${ANTHROPIC_API_KEY}
  budget: 5

- name: Kurt                   # Codex via your ChatGPT/Codex subscription
  model: gpt-5.5-codex
  backend: openai

- name: Karl                   # Codex via an OpenAI API key
  model: gpt-5.5-codex
  backend: openai
  api_key: ${OPENAI_API_KEY}

- name: Emmy                   # Claude through OpenRouter
  model: anthropic/claude-sonnet-5
  backend: anthropic
  base_url: https://openrouter.ai/api
  auth_token: ${OPENROUTER_API_KEY}

- name: Alan                   # any non-Claude OpenRouter model
  model: qwen/qwen3-coder:free
  backend: openai
  base_url: https://openrouter.ai/api/v1
  api_key: ${OPENROUTER_API_KEY}

- name: Sophie                 # FreeInference
  model: glm-5.1
  backend: openai
  base_url: https://freeinference.org/v1
  api_key: ${FREEINFERENCE_API_KEY}

- name: Henri                  # a self-hosted vLLM server
  model: leanstral-24b
  backend: openai
  base_url: http://localhost:8004/v1
  api_key: unity
```

Mixed rosters are the point: mark your strongest model as the primary and fill the swarm out with cheap or free workers!

## Webview Page Descriptions

- **overview** — the home page: the current run status (idle, or the running command and its phase), your agents with what each one is working on right now, open obstacles & questions, and recent decisions. It auto-refreshes while a run is going, so this is the page to sit on.
- **blueprint** — the actual Lean structure of your project: every declaration with its proof status (green = verified, yellow = complete but resting on a `sorry`, red = `sorry`, orange = `axiom`), filterable, with a list view and a dependency-graph view. Click any declaration to see its signature, source, and the chunk it belongs to. Statuses are kernel-verified when the project builds (you'll see a `kernel-verified` chip) and fall back to a textual approximation when it doesn't.
- **forum** — the agents' shared workspace, as threads: claims, results, obstacles, questions, decisions, endorsements. The `graph view` button shows the same posts as a reply graph.
- **chunks** — the run's chunk DAG (how the agents split up the work), colored by status: merged, active, pending, blocked. Click a node for its details.
- **agents** — your roster (see [Roster Configuration](#roster-configuration)). Add agents from presets or the `new` button, set one as primary, and edit the raw yaml directly if you prefer — the form and the yaml stay in sync.
- **prompt** — `UNITY.md`, the specialized instructions that go to every agent. State your goal or any constraints here.
- **sources** — the documents your agents work from (papers to formalize, code to verify, reference material). Upload, edit, or remove them here; they land in `.unity/source/`.
- **metrics** — the optimization metrics for `optimize` runs. Edit the built-ins, create your own, and set one as active.
- **logs** — every run's timestamped log, with phase delimiters. The live run's log tails automatically.
- **⚙ (settings)** — max attempts, the Lean LSP port, and your Axle / Aristotle Agent API keys.
- **run** — hover to pick a command, fill in the options (targets, metric, version — whatever that command takes), and start. While a run is going the button becomes a `stop` button: one press asks the agents to finish their current turn and wind down safely; a second press force-kills the run.

## Configuration

- `.unity/.env` — run flags: `MAX_ATTEMPTS` (critic-loop cap, default 5), `UNITY_FORUM_BRIEF=off`
  (disable workspace-brief injection), and optional service keys (`AXLE_API_KEY`,
  `ARISTOTLE_API_KEY`) that unlock extra agent tools.
- `.unity/agents.yaml` — the roster (see [Roster Configuration](#roster-configuration)). Per-agent
  credentials (`api_key` / `auth_token` / `base_url`) live here, not in `.env`; `${VAR}` references
  are resolved from the environment. Only `openai` agents with a custom `base_url` require an
  `api_key` — with no credentials, an agent rides your subscription login.
- `~/.unity/library/` — the global library (tactics, lemmas, references, skills, subagents) that
  every agent sees and the retrospective phase grows across runs.
