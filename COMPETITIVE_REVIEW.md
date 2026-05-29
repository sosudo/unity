# Competitive Review: Archon and OpenGauss vs Unity

Date: 2026-05-29. Scope: read-only analysis of two open-source Lean 4 autoformalization systems against Unity's design.

Local clones audited (gitignored under `external/`):
- **Archon** — `external/archon/` (frenzymath/Archon v0.2.0)
- **OpenGauss** — `external/opengauss/` (math-inc/OpenGauss v0.2.x)

---

## 1. TL;DR — top adoptable mechanisms

Ranked by leverage-to-implementation-cost:

1. **Archon's `progress-critic` subagent** — CONVERGING / CHURNING / STUCK / UNCLEAR verdicts driven by strict signal-only input. Closest fit for Unity's stagnation-detection gap (the place B16 currently just logs).
2. **Archon's blueprint/IR HARD GATE per-file dispatch precondition with same-iter fast path.** Cheap structural rule that prevents formalizer dispatch on under-specified IR.
3. **Archon's `task_results/<file>.md` per-attempt scratch + `proof-journal/.../milestones.jsonl` per-target structured log.** Lighter-weight complement to Unity's forum.
4. **Archon's multi-lane `lane-merge` agent for escalation reconciliation** (adopt the merge piece, not the always-on race).
5. **OpenGauss's prompt-cache breakpoints + middle-turn context compactor** for Unity's orchestrator-context-blowup ($90 / 100+ turn) failure mode.

OpenGauss contributes mostly *infrastructure* — Lean reasoning lives in upstream `cameronfreer/lean4-skills`, which Unity already integrates.

---

## 2. Archon (frenzymath/Archon)

### A. Architecture / orchestration
Multi-agent, multi-phase per iteration with strategic/executor separation. README: "A plan agent provides strategic guidance while prover agents write and verify proofs — separating analysis from execution to avoid context explosion" (`external/archon/README.md:12`). Entry `archon loop` (`external/archon/src/archon/commands/loop/entry.py:21-130`); phases under `commands/loop/phases/{plan,prover,review,blueprint_doctor,axiom_sweep,sync_leanok,finalize}.py`. Stages `autoformalize → prover → polish → COMPLETE` from `PROGRESS.md` (`README.md:144`). Inner git at `.archon/git-dir/` commits each phase independently of project git (`README.md:149`), enabling per-phase rollback.

vs Unity: Unity's pipeline is vertical per chunk; Archon iterates temporally over the whole project. The plan agent is forbidden from touching Lean (`prompts/plan.md:34`).

### B. Source intake and IR
No structured IR. Intake = LeanBlueprint (`README.md:262-266`). Per-file LaTeX chapters at `blueprint/src/chapters/<slug>.tex` with slug `Foo/Bar.lean → Foo_Bar.tex`. Consolidated chapters declare coverage via `% archon:covers RigidityKbar.lean Cotangent/ChartAlgebra.lean ...` (`prompts/plan.md:80-85`). Strict citation discipline: `% SOURCE:`, `% SOURCE QUOTE:`, `% SOURCE QUOTE PROOF:` LaTeX comments with verbatim original-language source text + visible `\textit{Source: ...}` (`prompts/plan.md:88-110`). `\lean{...}` cross-refs; `\leanok`/`\mathlibok` markers managed deterministically by `sync_leanok` and review (`prompts/plan.md:138`).

vs Unity: Unity's IR is machine-readable (`source_range`, `source_proof`, `is_assumption`); Archon's is human-readable LaTeX + tagged Lean.

### C. Proof search and retrieval
Identical tactic cascade (`external/archon/src/archon/.archon-src/skills/lean4/lib/scripts/solver_cascade.py:31-42`):
```python
SOLVERS = [("rfl", 1), ("simp", 2), ("ring", 2), ("linarith", 3),
           ("nlinarith", 4), ("omega", 3), ("exact?", 5), ("apply?", 5),
           ("grind", 8), ("aesop", 8)]
# "Inspired by APOLLO's solver-first strategy https://arxiv.org/abs/2505.05758"
```
Mathlib-search priority prescribed (`prompts/prover-prover.md:84-90`): `lean_local_search` → `lean_leansearch` → `lean_loogle` → never shell. Modified fork of `oOo0oOo/lean-lsp-mcp` bundled as `archon-lean-lsp` MCP. Blueprint handling far heavier than Unity's: dedicated `blueprint-doctor` phase, `blueprint-reviewer`/`blueprint-writer` subagents, `sync_leanok.py` script.

### D. Multi-agent coordination
File-based + descriptor-driven subagents. Per-prover scratch `task_results/<file>.md` (`prompts/prover-prover.md:99-121`); plan agent merges into `task_pending.md` + `task_done.md` each iter. Subagents are `.md` files with YAML frontmatter (`subagents/blueprint-reviewer.md:1-9`):
```yaml
name: blueprint-reviewer
description: Whole-blueprint audit...
write_domain: "task_results/**"
read_only: true
can_spawn: false
mandatory: [plan]
```
Dispatch: `python3 .claude/tools/archon-subagent.py --name <subagent> --slug <slug> --directive-file ... --write-domain '<glob>'` (`prompts/plan.md:243-251`). Concurrent dispatch via parallel Bash calls in one message (`prompts/plan.md:261`). `write_domain` glob is *enforced by the wrapper*; children's domains must be subset of parent's (`prompts/plan.md:259-260`) — structural guard Unity doesn't have. Council/ACCEPT-OBJECT analog: MANDATORY `progress-critic` returning per-route verdict; plan agent must obey or write explicit rebuttal (`subagents/progress-critic.md:228-242, 82-87`). No forum analog for cross-chunk gossip — hub-and-spoke through plan agent.

### E. Escalation / model tiering
Multi-lane parallelism replaces escalation tiers. `MultiLaneConfig` (`external/archon/src/archon/multilane/types.py:53-71`): N lanes (Anthropic, Moonshot/Kimi, DeepSeek) each in own worktree under `.archon/lanes/<lane>/`. "First lane to finish a file cleanly wins; other lanes get a 10-minute grace period, are then cancelled" (`README.md:169`). Per-file `lane-merge` agent picks best proof per declaration: complete > shorter > Mathlib-style > most progress > bare sorry (`prompts/lane-merge.md:23-46`). CLI `--model` for whole loop: opus/sonnet/haiku/kimi/deepseek (`commands/loop/entry.py:96-103`). No inner-loop stagnation fallback. No bandit/learned selection.

### F. Failure recovery
- `--resume` (`commands/loop/entry.py:115-130`) — detects last interrupted phase via `meta.json`, resumes Claude session id.
- `archon branch <name> --from <commit>` — fork any historical agent commit in `.archon/git-dir/` (`README.md:91, 149`).
- `blocked-deps filter` (`prompts/plan.md:370`) — drops objectives whose transitive imports failed previous `lake build`; itself-as-objective exempt.
- `dispatch cap` default 10 (`prompts/plan.md:367-370`); surplus deferred via `USER_HINTS.md`.
- Per-attempt log `task_results/<file>.md` (`prompts/prover-prover.md:99-123`): `Attempt N — Approach — Result (RESOLVED|FAILED|PARTIAL|IN PROGRESS) — Dead-end — Next step — Lemmas found`. Direct analog of Unity's `forum_log_attempt(outcome enum)` but file-scoped per chunk.

### G. Prompt engineering — unusually good patterns

1. **Hard structural rules with literal litmus tests** (`prompts/prover-prover.md:39-45`):
   > Litmus test: if you `unfold` your declaration, does it expose the named substantive content (Kähler differential module, the explicit iso, …) or does it stop at `Classical.choice` / `Iso.refl _` / nothing? If the latter, the body is structurally vacuous — ship the typed `sorry` instead.

   Three banned patterns (reflexive-iso placeholder, `Classical.choice`-as-body, empty `proof_wanted`) — exactly Unity's Invariant 2 failure modes, named upfront in the prover prompt.

2. **Plan-vs-prover boundary made literal** (`prompts/plan.md:56-63`):
   > Boundary: mathematical intent, not Lean syntax. Your output is mathematical intent. The prover's output is Lean syntax. Never cross this boundary. ... You MUST NOT use `lean_run_code` to validate proof bodies, search tactic sequences, or type-check expressions. If you find yourself writing or testing Lean tactic code, stop — that is the prover's job.

3. **Anti-fabrication rule** (`prompts/plan.md:293-305`):
   > When a hint or strategy step asks for verification against an external source ... and the named tool or path can't actually execute, you MUST NOT synthesize the verification output from your own context. The planner's context is the same context that produced the claims being verified; a planner-written cross-check is circular by construction and worse than skipping the verification, because it disguises absence of verification as presence of it.

4. **You decide; you never wait** (`prompts/plan.md:41`):
   > The loop is autonomous — it often runs unattended overnight ... every strategy-level choice ... is YOURS to make: pick the best option on the evidence, commit to it, and dispatch provers on it THIS iter. Never skip prover dispatch or idle an iter waiting for a human reply.

   USER_HINTS.md as asynchronous override; TO_USER.md as notice board, never question queue (`prompts/plan.md:43-47`).

5. **Tagged Mathlib lemma confidence** (`prompts/prover-prover.md:67-74`): `[verified]` / `[expected]` / `[gap]`. "Past iters' verification does NOT carry forward; Mathlib bumps rename and remove things."

6. **Mechanical-vs-deep partition** (`prompts/plan.md:358-363`): mechanical sorries (typeclass wiring, simp/ring territory) — load lane 3-6 at a time; deep sorries (load-bearing categorical/geometric argument) — one per lane. "Don't load a deep lane with three deep sorries; that just thrashes the prover's attempt budget."

7. **Progress-critic verdict rules verbatim** (`subagents/progress-critic.md:228-242`) — the most rigorous stagnation-detection spec in either repo. Bucket rules for CONVERGING/CHURNING/STUCK/UNCLEAR with "under-dispatch is churn" sub-rule and avoidance-pattern detection (off-critical-path reclassification, persistent deferral language across ≥2 iters).

8. **Lane-merge preference order** (`prompts/lane-merge.md:23-46`).

9. **HARD GATE per-file dispatch precondition** (`subagents/blueprint-reviewer.md:56-78`): F → `## Current Objectives` only if chapter C is `complete: true AND correct: true AND no must-fix-this-iter finding`. Same-iter fast path: re-dispatch reviewer scoped to one chapter after a writer fix.

10. **Bounded STRATEGY.md** (`prompts/plan.md:152-194`): "the whole file stays under ~250 lines / ~12 KB"; hard rules — no per-iter narrative, no accumulation, phases DELETED as completed. A living-arc that *shrinks*.

### H. Library / lessons
- `.archon/proof-journal/sessions/session_N/{summary.md, milestones.jsonl, recommendations.md}` (`prompts/review.md:49-80`). `milestones.jsonl` is one JSON per target with attempts, goals before/after, errors, lemmas used, insight.
- `task_results/<file>.md` short-term scratch.
- `STRATEGY.md` bounded living arc.
- `PROJECT_STATUS.md` rollup by review (`README.md:249`).
- No cross-run library analog — lessons stay project-local.

### I. What Unity does that Archon doesn't
- Machine-checkable IR with `source_range`/`source_proof`/`is_assumption`.
- Source Scan + Validation as first-class steps.
- MCP forum with `forum_log_attempt(outcome enum)` — cross-chunk visibility without plan-agent re-reading all scratch.
- Per-chunk topo-sorted DAG + worktree-per-chunk layer-parallel formalization. Archon's multilane is per-file × per-provider, not per-chunk × per-layer.
- Cross-run `~/.unity/library/`.
- ACCEPT/OBJECT council convergence — Archon critic verdicts are one-way, not multi-round.

---

## 3. OpenGauss (math-inc/OpenGauss)

### A. Architecture / orchestration
A project-scoped Lean workflow *orchestrator/launcher*, not a Lean reasoning system. Spawns child Claude Code or Codex sessions running `cameronfreer/lean4-skills` workflows directly (`README.md:9, 18-30`). CLI entry `cli.py`, `gauss_cli/main.py`; launcher `gauss_cli/autoformalize.py:36-89`. Lifted slash commands forward to lean4-skills (`README.md:21-30`):
```
/prove ... → /lean4:prove ...
/autoprove ... → /lean4:autoprove ...
/autoformalize ... → /lean4:autoformalize ...
```
Each invocation spawns a managed Claude Code/Codex subprocess with staged `~/.claude/` home, plugins, MCP/LSP wiring, and a startup-context markdown file. `swarm_manager.py` tracks concurrent child processes; `/swarm attach <id>` PTY-attaches, Ctrl-] detaches (`swarm_manager.py:1-12`). No analog of Unity's phases/IR/validation/council.

### B. Source intake and IR
None of its own. The launcher resolves the user's natural-language instruction, builds a startup-context file via `_write_startup_context` (`gauss_cli/autoformalize.py:1689-1701`), passes the instruction string verbatim. `plans/autoformalize-backend.md` describes a generic `handoff` transport + Claude-specific adapter that stages `lean4-skills/plugins/lean4`, writes `lean-lsp.mcp.json`. No Unity-style IR.

### C. Proof search and retrieval
Inherited from `lean4-skills`. Launcher stages `lean-lsp-mcp` for the child (`gauss_cli/autoformalize.py:36-39, 1654-1667`). OpenGauss writes none of its own Lean prompts.

### D. Multi-agent coordination
No multi-agent Lean coordination. The "swarm" is independent child agents the user manages. `SwarmTask` (`swarm_manager.py:59-80`): `task_id`, `theorem`, `working_dir`, `status`, `progress`, `result`, `error`. Each task is a separate `claude -p --output-format stream-json` subprocess. Status parsed from JSON stream; users `/swarm attach <task_id>` via PTY. Closer to a job tracker than to Unity's forum.

### E. Escalation / model tiering
Backend tiering only (`gauss_cli/autoformalize.py:46-59`):
```python
DEFAULT_AUTOFORMALIZE_BACKEND = "claude-code"
CODEX_AUTOFORMALIZE_BACKEND = "codex"
_AUTOFORMALIZE_BACKEND_ALIASES = {
    "claude": DEFAULT_AUTOFORMALIZE_BACKEND, "codex": CODEX_AUTOFORMALIZE_BACKEND,
    "openai-codex": CODEX_AUTOFORMALIZE_BACKEND, ...
}
```
Backend chosen at invocation; no fallback, no auto-escalation, no bandit, no stagnation detection.

### F. Failure recovery
SQLite session state via `gauss_state.py`. `/swarm` shows task statuses; failed tasks expose `result`/`error`. User re-launches manually. `plans/checkpoint-rollback.md` describes a planned design.

### G. Prompt engineering — platform-level, not Lean-level

1. **Skill-conditional activation** (`agent/prompt_builder.py:214-260`): SKILL.md frontmatter declares `fallback_for_toolsets` / `requires_toolsets` / `fallback_for_tools` / `requires_tools`. System prompt only shows compatible skills. Portable to Unity for gating prompt injections by available helper-scripts in `~/.unity/scripts/`.

2. **Mandatory skills index** (`agent/prompt_builder.py:344-358`):
   > Before replying, scan the skills below. If one clearly matches your task, load it with skill_view(name) and follow its instructions. If a skill has issues, fix it with skill_manage(action='patch'). After difficult/iterative tasks, offer to save as a skill.

   The "fix it with skill_manage(action='patch')" line is unusual — agents encouraged to patch their own skill library on the spot.

3. **Prompt-injection scanning of context files** (`agent/prompt_builder.py:20-57`): AGENTS.md / .cursorrules / SOUL.md scanned for invisible Unicode + regex threat patterns before injection; matches replaced with `[BLOCKED: ...]` marker.

4. **Context compactor** (`agent/context_compressor.py:31-100`): protect first N + last N turns, summarise the middle when prompt nears 50% of context. Summary uses cheaper auxiliary model (Gemini Flash by default). Marker block:
   > [CONTEXT COMPACTION] Earlier turns in this conversation were compacted to save context space. The summary below describes work that was already completed, and the current session state may still reflect that work (for example, files may already be changed). Use the summary and the current state to continue from where things left off, and avoid repeating work:

5. **Anthropic prompt-cache breakpoints `system_and_3`** (`agent/prompt_caching.py:40-70`): up to 4 `cache_control` markers — system + last 3 non-system messages. "Reduces input token costs by ~75% on multi-turn conversations."

6. **`trajectory_compressor.py`** (`trajectory_compressor.py:1-80`): post-processes completed trajectories to a token budget; protects first/last turns, summarises middle. Aimed at training data; reusable for library-storage compaction.

### H. Library / lessons
`~/.gauss/skills/<category>/<skill>/SKILL.md` indexed at startup (`agent/prompt_builder.py:263-358`). Categories include software-development, mlops, research, etc. `SOUL.md` from GAUSS_HOME injected into every conversation. `insights.py` (`agent/insights.py:62-100`) analyses SQLite session history (tokens/cost/duration/tool-use). Not Lean-specific; no per-project proof-journal equivalent of Archon's.

### I. What Unity does that OpenGauss doesn't
Everything Lean-specific: own IR + Validation + Critic + Retrospective; ACCEPT/OBJECT council; chunk-DAG with per-chunk worktrees; structured `forum_log_attempt(outcome=...)`. OpenGauss strengths are infrastructure.

---

## 4. Direct comparison table

| Dimension | Unity | Archon | OpenGauss |
|---|---|---|---|
| Orchestration | Multi-phase per chunk (Scan→IR→Val→Semi→Expl→Form→Critic→Retro→Esc) | Multi-phase per iter (Plan→Subagents→Provers→Review→Finalize) | Launcher; forwards to lean4-skills child |
| IR / intake | Structured (source_range, source_proof, is_assumption) | LaTeX blueprint + verbatim-source citation discipline | None of its own |
| Tactic cascade | rfl→simp→ring→linarith→nlinarith→omega→exact?→apply?→grind→aesop | Identical | Inherited |
| Mathlib search | local→leansearch→loogle | Same priority prescribed | Inherited |
| Multi-agent coord | MCP forum, per-chunk threads, attempt logs, ACCEPT/OBJECT council | YAML-frontmatter subagents w/ write_domain globs; file-scoped task_results; hub-and-spoke | None (swarm = independent jobs) |
| Cross-attempt logging | forum_log_attempt(outcome enum) | task_results/<file>.md + milestones.jsonl per target | SQLite session events |
| Escalation / tiering | Reactive secondary-model | Concurrent multi-lane providers + lane-merge | Backend selection at invocation |
| Stagnation detection | Logged via B16; no critic yet | progress-critic verdicts + correctives | None |
| Per-decl knowledge store | ~/.unity/library/ cross-run | proof-journal milestones.jsonl project-local | ~/.gauss/skills/ (not Lean) |
| Parallel work unit | Per-chunk worktree, layer-parallel | Per-file × per-provider worktree, lane-merged | Independent swarm tasks |
| Resume / rollback | Git-based project-level | --resume + per-phase inner-git + archon branch --from <commit> | Backend-managed only |
| Context-blowup defense | Open Unity weakness | Plan-vs-prover boundary; subagents w/ isolated context; mandatory progress-critic | Context compressor + prompt-cache breakpoints |
| Anti-fabrication discipline | Implicit in retros | Explicit cite-and-read rule in plan prompt | Prompt-injection scanner on context files |
| Blueprint integration | Context line in formalization prompt | First-class phase + reviewer/writer subagents + HARD GATE | None |

---

## 5. Adoption candidates

### A. Progress-Critic subagent with numeric verdicts (HIGH leverage, MEDIUM cost)
A new lightweight agent invoked between phases (e.g. before Escalation and as part of Retrospective). Directive contains ONLY signals + dispatch list — NOT strategy or IR (the discipline is the point, see `subagents/progress-critic.md:36-65`). Per-chunk verdict `CONVERGING|CHURNING|STUCK|UNCLEAR` + one primary corrective. Stance (`subagents/progress-critic.md:97-100`):
> The plan agent prefers CONVERGING verdicts because they let it continue. You should NOT give that bias the benefit of the doubt.

Unity files: new `unity_agent/SUBAGENTS/CRITIC/PROGRESS_CRITIC.md`; pipeline wiring near current escalation trigger (`unity_agent/pipeline.py` around `_run_escalation_phase`); forum helper to extract signals from `forum_log_attempt` history; pipeline calls into the per-chunk verdicts to drive escalation candidates. Cost ~3-5 commits. Payoff: principled escalation trigger; stops B16-style near-sequential-chain churn from going undetected; replaces current pure-stagnation-counter heuristic with structured signals.

### B. IR HARD GATE with same-iter fast path (HIGH leverage, SMALL cost)
Formalizer dispatch precondition: chunk C's IR must be `complete: true AND correct: true AND no must-fix retrospective finding`. If gate fails, dispatch Critic/Retrospective scoped to C *this iter*, re-check, then schedule Formalizer. Verbatim discipline at `external/archon/subagents/blueprint-reviewer.md:56-78`. Unity files: `unity_agent/pipeline.py` (gate before each formalization worktree dispatch); `unity_agent/PROMPTS/FORMALIZATION/*.md` encode gate; helper that reads the most-recent SEMIFORMAL_FIELD_DRIFT.md / VALIDATION_REPORT.md and per-chunk critic notes. Cost ~1-2 commits. Payoff: eliminates Formalizer work on under-specified IR — pairs naturally with Workstream A's S0.4 (raise → resolver) and Commit J's B14 promotion.

### C. Per-attempt `task_results/<chunk>.md` + lane-merge for escalation reconciliation (MEDIUM leverage, MEDIUM cost)
Two pieces together. (1) Each formalizer (primary and escalated) writes `task_results/<chunk_slug>.md` with `Attempt N — Approach — Result (RESOLVED|FAILED|PARTIAL|IN PROGRESS) — Dead-end — Next step — Lemmas found` (Archon `prompts/prover-prover.md:99-121`). Unity already has `forum_log_attempt`; the markdown file complements it for human review without requiring a forum_read. (2) After escalation, run a per-chunk merge agent (Archon `lane-merge.md`) against `{primary, escalated}` candidates instead of winner-take-all. Merge preference order verbatim: complete > shorter > Mathlib-style > most progress > bare sorry; never invents declarations; imports union'd (`prompts/lane-merge.md:23-46`). Unity files: new `unity_agent/SUBAGENTS/CRITIC/LANE_MERGE.md`; Formalization + Escalation prompts; pipeline wiring after escalation completes. Cost ~3-5 commits. Payoff: escalation no longer discards primary-tier partial progress.

### D. Prompt-cache breakpoints + middle-turn context compactor (HIGH leverage, SMALL cost)
Two mechanical changes:
1. **Cache breakpoints.** Apply Anthropic `cache_control: ephemeral` to (a) system prompt and (b) last 3 non-system messages of every sub-agent's API call. Lift from `external/opengauss/agent/prompt_caching.py:40-70`. ~75% input-token reduction on multi-turn conversations.
2. **Auxiliary-model compaction.** Invoke a cheaper model (Gemini Flash, Haiku) to summarise the middle of orchestrator message history when prompt exceeds ~50% of context. Pattern from `external/opengauss/agent/context_compressor.py:31-100`. Use the marker:
   > [CONTEXT COMPACTION] Earlier turns in this conversation were compacted to save context space. The summary below describes work that was already completed, and the current session state may still reflect that work (for example, files may already be changed). Use the summary and the current state to continue from where things left off, and avoid repeating work:

Unity files: Claude SDK call site wrapped with cache-marker helper; new `unity_agent/context_compactor.py`; wire into orchestrators known to blow context (formalization, exploration, strategy-formalization). Cost ~1-2 commits. Payoff: directly addresses AUDIT.md weakness "context exhaustion in single orchestrator agents (100+ turns / $90 observed)."

### E. Anti-fabrication and "you decide; you never wait" prompt directives (MEDIUM leverage, SMALL cost)
Two patterns into Unity's orchestrator + Critic + Retrospective prompts:
1. Anti-fabrication from `prompts/plan.md:293-305`: when external verification can't run, "you MUST NOT synthesise the verification output from your own context ... circular by construction." Three responses: substitute / partial / escalate.
2. "You decide; you never wait" from `prompts/plan.md:41`: pipeline is autonomous, every strategy choice is yours, surface via DECISIONS.md as FYI user can override next iter via USER_HINTS.md; do NOT idle waiting for a human reply.

Unity files: `unity_agent/PROMPTS/CRITIC.md`, `unity_agent/PROMPTS/RETROSPECTIVE.md`, FORMALIZATION orchestrator prompts; possibly add `DECISIONS.md`/`USER_HINTS.md` to project state (the project notes memory hints UNITY.md is already read). Cost ~1 commit. Payoff: fewer false verifications surfacing as real; fewer stall iterations on phantom user input.

---

## 6. What NOT to adopt

1. **Archon's hub-and-spoke plan-agent-as-everything.** Would lose Unity's main parallelism story. Plan-agent is Archon's bottleneck; Unity's forum is structurally superior for cross-chunk coordination. Adopt the verdict-style critic (5.A) without folding everything into one agent.
2. **OpenGauss's "wrap upstream skills CLI" approach.** Unity *is* the reasoning system; OpenGauss is a launcher *for* one. Adopting it means giving up Unity's IR/Validation/Critic — moving backwards.
3. **Archon's blanket multi-lane "race providers per file."** Every file gets N × cost; the 10-minute grace doesn't fully bound spend. Unity's reactive escalation is more cost-disciplined. Adopt the lane-merge *reconciliation* piece (5.C) without the always-on race.
4. **OpenGauss's PTY swarm + interactive attach.** Unity is autonomous; `/swarm attach` UX conflicts with the unattended-overnight assumption.
5. **Archon's `--dangerously-skip-permissions --permission-mode bypassPermissions`** (`README.md:16`). Acceptable for Archon only because they run as a dedicated low-priv user; Unity should keep permission scoping.
6. **Archon's hard-coded inner-git `.archon/git-dir/`.** Powerful but adds operational complexity; Unity's git-worktree-per-chunk already buys most of the same property.
7. **OpenGauss's `gauss` shell wrapper** that copies `claude-home` and stages plugins per session. Heavy machinery for a problem Unity does not have.

---

## 7. Open questions and explicit unknowns

- **Bandit model tiering in Archon**: not present in code I read. `multilane/types.py:53-71` confirms lanes are configured, not learned.
- **OpenGauss Lean reasoning quality**: cannot assess — it lives in `cameronfreer/lean4-skills`, which Unity already integrates. Novel surface area is launcher/swarm/UX.
- **Archon's `archon-informal-agent.py`**: referenced in prompts (`prompts/prover-prover.md:51`, `prompts/plan.md:288`) as a single-shot external-LLM call for proof sketches. README: "the bundled informal agent is a simplified demonstration ... Our internal implementation is more involved but not yet ready for open-sourcing" (`README.md:74-79`). Internal version unverifiable.
- **Whether Archon's plan agent actually obeys its anti-fabrication / "you decide; you never wait" rules at runtime**: prompts are exemplary, adherence unverifiable from static inspection.
- **OpenGauss `tinker-atropos`, `mini-swe-agent`, `acp_adapter`**: vendored agent stacks visible at repo root; not drilled into. Look like RL-training and agent-protocol scaffolding, not Lean-relevant. Out of scope.
