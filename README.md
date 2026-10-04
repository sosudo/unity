# Unity

A multi-agentic harness for Lean 4.

Unity uses a roster of heterogeneous agents (different models) to work on Lean projects collaboratively.

If you have any issues/concerns or contributions, please make a GitHub issue or reach out to me at [shivansg@andrew.cmu.edu](mailto:shivansg@andrew.cmu.edu). A technical writeup detailing exactly how Unity works will be put out soon!

## License and citation

Unity is distributed under the [Unity Citation License 1.0](LICENSE). Any use of
Unity, including derivative works and outputs materially produced with Unity,
must cite the project. Ready-to-use citation metadata is provided in
[`CITATION.cff`](CITATION.cff).

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

From the CLI, add files or folders with `unity source add <path>`, put the scope and instructions in `.unity/UNITY.md`, then run `unity autoformalize`. The pipeline snapshots the supplied documents, prepares Lean, chunks and scaffolds the formalization, runs collaborative formalizers, verifies and merges candidates, and runs the final critic. `unity autoformalize --continue` resumes that state; changed source documents or scope require a fresh run. `RETROSPECTIVE=false` skips the optional retrospective.

Autoformalize has its own runtime, agent launcher, verifier, prompts, and Forum interface. Its discussions and structured state live under `.unity/forum/autoformalize/`, separate from solve and prove. It does not run the English-solving phase or generate or rewrite a source paper. The existing project setup, roster, dependency cache, and generic worktree utilities are unchanged.

Autoformalize workers use Codex's workspace-write sandbox or Claude's required Bash sandbox plus editor path checks. Source edits are confined to the assigned worktree; shared Git metadata, `.unity`, dependency packages, and the existing uv cache remain accessible to Unity tools. These are source-write restrictions, not isolation from trusted MCP servers or shared runtime state. Claude requires native sandbox support (including its platform prerequisites) and refuses unsandboxed fallback; arbitrary research URLs should use WebFetch rather than shell networking. Antigravity enables its terminal sandbox and receives the same worktree policy, but editor isolation is best effort. Solve formalization has its own copy of these restrictions; informal solving and prove keep their existing launchers.

### Formalize

First, go to the sources tab and add the documents you want formalized.

Next, go to the agents tab and set up your agent roster. There are some presets you can use to quickly add common agents (e.g. Claude via your Claude Code subscription, GPT via your Codex subscription, OpenRouter API models); if you want to use a model without a preset, you can press the `new` button and fill the fields in yourself. Check [Roster Configuration](#roster-configuration) for more information on how to fill them in yourself.
Make sure to save your agents before continuing!

Then, go to the prompt tab, and type in any specialized instructions you want (such as telling specific agents of stuff they may not be allowed to do or if you have a specific file structure in mind). No need to tell the agents that they're formalizing anything, Unity's pipeline will handle that for you. Again, remember to save before continuing!

Finally, press the settings icon in the top right, and set your max attempts (how many iterations of the formalization loop are allowed, the default of 5 typically works well), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Your Unity agents can call out to both Axle and Aristotle Agent using tool calls to help with formalization.

When you're ready, hover over the `run` button, press `formalize`, and press the `start` button. In the `targets` box, put specific Lean declarations to complete or theorems/lemmas/definitions/sections from your sources to formalize. Leaving it blank requests the supplied source within the existing project's instructions, not repair of every unrelated project hole.

From the CLI, add the supplied documents with `unity source add <path>`, put your
scope and instructions in `.unity/UNITY.md`, and run
`unity formalize --targets "<existing declaration names or target description>"`. The
`--targets` flag is optional; it narrows the source obligations and existing
project gaps to address. `unity formalize --continue` resumes the same saved
source, scope, and project baseline. To change sources, instructions, or targets,
start in a separate project copy with fresh Unity run state, keeping the original
run and its evidence intact; existing Formalize history is not overwritten.

`--project-scope changes` is the fresh-run default. Unity records the original
files, configuration and dependencies and checks the project's normal build;
it does not import every library into one Lean environment. Candidate checks
then validate the agent's complete diff, inspect submitted outputs in their real
module/import contexts, and compare affected original declarations in the normal
build or submitted import closure against the immutable snapshot on demand.
Unrelated original files remain frozen, including broken optional tools.
New results must not depend on old proof holes or forbidden axioms. Final review
rechecks the merged changes and normal build, with exact coverage in the report.
Changes mode currently requires default Lake targets whose Lean modules can be
identified (libraries/executables); an opaque custom default target is reported
as unsupported, not silently treated as checked. Original commands are protected
with conservative source-edit checks plus native declaration comparison; unusual
edits may be rejected rather than guessed safe. This is a preservation check,
not a sandbox for hostile Lean metaprograms.

`--project-scope all` retains the legacy whole-project audit policy.
`--project-scope libraries` checks the project's declared Lean library roots and
their project import closure; auxiliary modules outside that closure stay frozen
byte-for-byte, without claiming those modules were compiled or kernel-verified.
This is independent of `--targets`, which selects the mathematical work.
Library-scoped workers cannot edit frozen auxiliary modules or add imports of
previously excluded project modules. The saved report names the exact coverage.
Auxiliary modules already imported by a library are verified but remain read-only.
On `--continue`, omitting `--project-scope` reuses its saved value; changing it is
rejected before any Lean build. Use a fresh separate project copy for a different
verification scope.

Start from an existing project whose normal build (or explicit legacy verification scope) builds with its
pinned toolchain and dependencies, on a clean named Git branch. Project source/configuration inputs
must be tracked so private worktrees can preserve them; Unity will not stash,
discard, or commit dirty user work to make the baseline pass. Supplied documents
under `.unity/source/` are snapshotted separately. On resume, source documents,
targets, and instructions remain frozen. Only the exact `## State` section in
`.unity/UNITY.md` (outside a code fence, ending before the next level-one or
level-two heading) is mutable progress text and excluded from the instruction identity.

Formalize preserves the existing project's declarations and interfaces while
filling the selected gaps or adding the requested missing source material. It
does not run the Architect/bootstrap phase or automatically change dependencies,
the toolchain, or project configuration. Out-of-scope holes remain untouched,
but completed targets and their project-owned dependency closure must be clean:
an unrelated existing `sorry` is not a reason to rewrite the whole project, and
a target that depends on one is not complete.

For change-focused runs, use bounded existing targets or a source description;
the old `--targets All` whole-project hole enumeration is not the default and
requires an explicit legacy audit mode. Selected holes in any mode cannot include
compiler-generated/internal auxiliary declarations, such as a
structure-field proof moved into `foo._proof_1`. Explicit auxiliary targets and
ordinary targets depending on such holes also stop because reliable source
ownership is not yet available. Explicit ordinary targets independent of those
holes remain supported; unrelated original holes stay protected.

Formalize has its own source-bound chunking, private-worktree formalizers,
representation and machine checks, independent faithfulness critic, repair
loop, and final report. Its Forum, state, DAG, and telemetry live under
`.unity/forum/formalize/`, separate from Autoformalize, Solve, and Prove. The
dashboard shows this workspace and its separate verification/faithfulness
statuses. Safe stop requests preserve work and stop Formalize's registered
verification jobs; compilation alone does not mean the requested source has
been faithfully formalized.

Current validation covers offline regressions and native Lean fixtures; a full
provider-backed Formalize run has not yet been validated.

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

When you're ready, hover over the `run` button, press `solve`, and press the `start` button. During
informal solving the whole roster coordinates through a solve-specific Forum and submits immutable
argument and paper components. Dependency-aware informal tasks let agents divide lemmas, checks,
sections, and synthesis while preserving direct full-solution attempts. Paper candidates record the
exact component revisions they incorporate and receive independent review. The accepted paper is then
chunked into a source-linked informal DAG, without writing Lean scaffolds. Chunkers correct ordinary
validation feedback in the same session; only failed executions consume their attempt budget.
Solve-owned formalizers refine this graph and implement ready Lean tasks in private worktrees,
with separate statement/proof dependencies, immutable candidates, targeted representation review,
and cached verification of exact source and dependency revisions. The same solve Forum spans both
loops; standalone autoformalize and prove remain independent. A final critic can reopen individual
Lean tasks or the informal solution. Paper corrections require new independent paper review and
checkpoint old private work before reassignment. Runs finish only after both gates are accepted.
Legacy solve runs with a version-1 formal contract cannot reuse their old verification evidence;
their files remain preserved, but continuing requires a fresh solve run.

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

Finally, press the settings icon in the top right, and set your max attempts (the inherited Formalize retry and round limits; default 5), the port for your Lean LSP MCP (default 8888), your [Axle](https://axle.axiommath.ai/) API Key (optional), and your [Aristotle Agent](https://aristotle.harmonic.fun/) API key (also optional). Workers use the configured tools through Bump's own Forum and tool profiles.

When you're ready, hover over the `run` button, press `bump`, put in your exact target Lean version, and press the `start` button. Bump is an independent copy of Formalize's worker, candidate, merge, retry, and critic lifecycle, adapted to compiler-driven migration. Its discussions, tasks, and state live in `.unity/forum/bump/`; its workers, jobs, native helpers, and tool catalogs use their own Bump namespace. Backend API retries follow Formalize's existing behavior, and an exhausted worker returns through the ordinary scheduling path without a separate transport blacklist.

Bump builds the original project and records its declarations and dependencies, updates an isolated target's toolchain and explicitly requested dependencies, then builds the target. Compiler errors become **declaration-level assignments**, ordered by the declaration dependency graph. Declaration refinement mappings record compatibility changes and preserve each original obligation. Several tasks may belong to the same file; the compiler still checks complete files, while integration serializes changes to the shared target. A module with a failing import is not automatically a declaration repair assignment: fixing its prerequisite and rebuilding reveals whether its own declarations need changes.

Located import or syntax errors outside a declaration use a bounded source-command task, including in import-only files with no declarations. This does not grant whole-file editing permission or invent a declaration. Actual mutually dependent declarations and exact source-declaration families can share an assignment; unrelated declarations cannot be grouped just because they share a file.

Repair integration is provisional. A useful declaration patch can advance the target while unrelated declarations still fail; it is not reported as an accepted migration. Refreshing compiler diagnostics discovers newly exposed repairs. The final selected-scope build, native declaration and trust checks, and independent semantic critic must cover all original obligations, including declarations that never needed editing. Compatible upgraded imports are an explicit assumption, rather than a recursive comparison of every upstream expression. Existing holes remain recorded assumptions; compilation alone does not establish semantic equivalence or permit new trusted assumptions.

By default, `--project-scope build` checks the local modules in the configured default build and their local import closure, as reported by Lake and Lean. Other files, including unbuilt examples or notes, are preserved byte-for-byte; they are not claimed to be migrated or newly kernel-verified. The boundary is sealed before migration and cannot expand or shrink during agent repairs. Use `--project-scope all` explicitly to require inspection of every local Lean module instead; an unbuildable file then blocks the run.

For explicit dependency changes, use `unity bump v4.34.1 --dependency mathlib=v4.34.1` (repeat `--dependency NAME=REV` as needed). Unspecified dependencies retain their pins; Bump does not guess a Mathlib upgrade. Fresh runs also try an optional, exact target-version-matched LeanArchitect dependency before sealing the target; `--architect off` disables this. An unavailable or incompatible optional package is skipped. Start from a clean project with a working original build. `unity bump --continue` resumes the saved target and configuration; it cannot replace the target version, dependency pins, or verification scope. Previous Bump implementations' saved attempts cannot be continued by this replacement.

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
| `unity bump [version]` | `--dependency NAME=REV`, `--project-scope build\|all`, `--architect auto\|off`, `--continue` | migrate to an exact Lean version with explicitly selected dependency changes; version is required for a fresh run |
| `unity optimize <metric>` | `--targets <scope>`, `--continue` | improve Lean code w.r.t. a metric (`length`, `modularity`, ...) |
| `unity agent` / `unity doctor` | — | interactive session / interactive resolver with the primary agent |
| `unity serve` | `--port <n>` (default 8080) | **the control center** (see Quick start) |
| `unity mcp <server> <tool> [json]` | — | call any agent MCP tool from the shell (e.g. `unity mcp unity-forum forum_stats '{}'`) |
| `unity source add <path>` / `remove <name>` / `list` | — | manage source material in `.unity/source/` |
| `unity metric add\|modify\|remove <name>` / `move <file>` / `list` | — | manage optimization metrics in `.unity/metrics/` |
| `unity reset` / `unity clean` | — | wipe / prune the global library (`~/.unity/library/`) |
| `unity complete` | — | remove Unity artifacts from a finished project |
| `unity update` / `unity uninstall` | — | manage the installation |

`--targets` narrows a run's scope (default: everything in scope). For `prove`, pass exact unresolved declaration names or Lean file paths, separated by commas or newlines; its target DAG is extracted mechanically rather than interpreted by a model. `--continue` re-orients from the previous run's state before continuing — the web UI sets it automatically when prior state exists. Except for `formalize`, fresh (non-`--continue`) runs start with a bootstrap step that adds LeanArchitect when a toolchain-matching release exists. Formalize keeps the existing project's dependencies and configuration unchanged.

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

- name: Nova                   # Google Antigravity subscription (`agy` login;
  model: gemini-3.1-pro-high   #  Gemini pool and Claude/GPT pool — see `agy models`)
  backend: antigravity

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
  model: deepseek-v4-flash
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

- `.unity/.env` — run flags: `MAX_ATTEMPTS` (cap on solving, formalization, and critic loop retries;
  blank/unset = indefinite),
  `RETROSPECTIVE=false` (skip prove/solve retrospectives), `UNITY_SOLVE_REVIEW_QUORUM` (independent
  approvals required for an informal solution), `UNITY_FORUM_BRIEF=off` (disable workspace-brief
  injection), and optional service keys (`AXLE_API_KEY`,
  `ARISTOTLE_API_KEY`) that unlock extra agent tools.
- `.unity/agents.yaml` — the roster (see [Roster Configuration](#roster-configuration)). Per-agent
  credentials (`api_key` / `auth_token` / `base_url`) live here, not in `.env`; `${VAR}` references
  are resolved from the environment. Only `openai` agents with a custom `base_url` require an
  `api_key` — with no credentials, an agent rides your subscription login.
- `~/.unity/library/` — the global library (tactics, lemmas, references, skills, subagents) that
  every agent sees and the retrospective phase grows across runs.
