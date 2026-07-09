# Forum 2.0 — from social forum to typed shared workspace

Design rationale + implementation record for the redesign of Unity's inter-agent communication
substrate (2026-07-08). Written for the paper's design section.

## 1. The evidence that forced the redesign

Autopsy of the VerifiedProbabilisticAlgorithms run (`unity create`, 8 heterogeneous agents):

| Feature | Designed for | Observed usage |
|---|---|---|
| Multi-parent DAG replies | nuanced synthesis across threads | **0 / 86 posts** |
| 6 vote dimensions | high-dimensional evaluation | **17 votes; only 2 dims ever used** (correctness, faithfulness) |
| Free-form posts | discussion | 86 posts, pure broadcast, never read back conversationally |
| Tags | cross-thread hyperedges | **thrived: 23 tags, agents invented their own typed taxonomy** (`decision-pmf-representation`, `correction-local-search-unreliable`, `reference-sampcert`, …) |

The pattern has one explanation: **a communication structure gets used iff something downstream is
contracted to consume it.** The `decision`/`phase-handoff` tags were the only features whose
prompts promised a reader (`forum_get_tag("decision")` is called by later phases) — and they were
not only used but organically *extended* into a key-value store. Replies and vote dimensions had
no reader in any agent's loop, so rational, task-pressured agents never wrote them. Communication
value = (what a reader can do with it) − (write cost + read cost); the old design maximized
*expressible* nuance while leaving *consumed* nuance near zero.

## 2. The high-dimensional hypothesis, assessed

The original bet: since human forums are linear due to human limitations, a higher-dimensional
substrate (multi-parent replies, many vote axes) would let LLM agents communicate superhumanly.

Verdict from the evidence and literature: **the direction is right, the mechanism was wrong.**
- Right: single artifacts should carry more structured, machine-consumable content than a chat
  message. The agents *demonstrated demand* for this by building a typed taxonomy out of tag names.
- Wrong: open-ended dimensionality with no contracted consumer is entropy, not nuance (dead vote
  dims, dead reply DAG). And genuinely latent/high-bandwidth channels (KV-cache or hidden-state
  exchange) are architecturally unavailable for heterogeneous black-box API models.
- The reframe: **put the dimensionality in the schema, not the social features.** A RESULT with
  eight typed fields is an eight-dimensional message where every dimension has a guaranteed reader.

## 3. Design principles

1. **Consumption contracts** — nothing is writable without a guaranteed reader somewhere in the
   control loop (briefs, consensus, later-phase injection, sign-up retrieval).
2. **Typed speech acts over free text** — coordination happens through structured artifacts;
   free-form posts remain for what no act covers.
3. **Verified knowledge, not folklore** — knowledge transfer flows through a ledger whose entries
   require evidence (build output / error text / compiling snippet). Lean gives us a kernel-grade
   verifier; use it.
4. **Reading must be ~free** — one digest call (or automatic injection) renders the workspace into
   a few hundred tokens; agents never parse raw JSON threads.
5. **Auto-thread, never ask agents to be forum citizens** — replies emerge from typed references
   (RESULT→CLAIM, ANSWER→QUESTION, OBJECT→RESULT), not manual etiquette.

## 4. What was built (all live in `unity/forum/server.py`, backward compatible)

**Typed acts** (each line: act → its contracted consumer):
- `forum_claim(chunk, strategy)` → teammates' briefs (duplicate-work/strategy avoidance).
- `forum_result(chunk, status, build_ok, decl_names, error_sig)` → `forum_consensus` + the merge
  gate; auto-links & closes the author's claim; `build_ok` auto-resolves the chunk's obstacles.
- `forum_obstacle(chunk, goal_state, tried, hypothesis)` → pushed into every teammate's brief.
- `forum_question(to?, chunk?)` / `forum_answer(question_id)` → addressee's brief; prompts require
  answering before new claims.
- `forum_endorse` / `forum_object(reason)` / `forum_resolve_objection` → merge quorum: a result
  merges only with ≥1 endorsement and 0 open objections (primary override must be a DECISION).
  Both also record legacy correctness votes, preserving ICRL credit + dynamic re-ranking.
- `forum_decision(topic, choice, rationale)` → injected into all later phases; newest per topic
  supersedes; carries the legacy `decision` tag (old retrieval keeps working).
- `forum_handoff(phase, changed, open, commitments)` → later phases; legacy `phase-handoff` tag.

**The Ledger** — the knowledge-transfer organ:
- `ledger_add(kind = lemma | tactic | failure, title, content, evidence, goal_shape?, chunk?)`,
  evidence **required** (rejected otherwise); `ledger_get(query?, chunk?)` for retrieval.
- Run-scoped Voyager-style skill library, verification-gated ("evidence over plans"); its best
  entries graduate to `~/.unity/library/` at retrospective.

**Digest layer**:
- `forum_brief(author, chunk?)` — one call: binding decisions, latest handoff, open obstacles &
  claims on your chunk, questions addressed to you, ledger highlights.
- **Dispatch injection**: `orchestrator.dispatch` prepends the same digest to every agent's
  preamble. Empty (and omitted) on fresh runs — it earns its keep at phase boundaries,
  critic-loop iterations, and `--continue`; *intra-phase* freshness comes from the tool.
- `forum_stats()` — post counts by act/author (telemetry for the H3 ablation).

**Cuts**: default vote dimensions reduced to the two that were ever used (correctness,
faithfulness); manual multi-parent threading demoted (auto-links only); the dimension-proposal
machinery remains but is no longer advertised.

**Verification**: 24-case functional suite (act semantics, auto-linking, auto-resolution,
supersession, quorum/objection blocking, evidence gate, ledger retrieval, brief composition,
legacy compatibility) + MCP wire test (29 tools registered over the protocol, round-trip verified)
+ dispatch-injection test (present with state, absent on fresh runs). One real bug found and
fixed along the way: module entrypoint preceded the new tool registrations, so `python -m
unity.forum.server` served only the legacy tools; entrypoint moved to EOF.

## 5. Radical alternatives considered and rejected (for now)

- **Latent / KV-cache communication** (LatentMAS, DroidSpeak, KVComm, HyLaT): requires activation
  access and (mostly) aligned latent spaces — unavailable for heterogeneous black-box APIs.
  A contained same-model experiment on babel-served vLLM pairs is a possible paper ablation, not
  infrastructure.
- **Induced compact dialects** (EcoLANG-style): unverifiable by humans, brittle across model
  families; Unity's faithfulness story depends on auditable communication. Rejected.
- **Debate rounds / conversational mesh**: literature shows modest gains at exploding cost; the
  phase-gated consensus already captures the value. Rejected.

## 6. Sources

- VPA forum autopsy (this repo's run artifact) — primary evidence.
- [LLM-based Multi-Agent Blackboard System](https://arxiv.org/pdf/2510.01285) — shared-state beats
  message passing for long-horizon coordination.
- [What Should Agents Say? Action-state Communication](https://arxiv.org/pdf/2606.05304) —
  communicate verified state, not chatter.
- AgentPrune / [TodyComm](https://arxiv.org/pdf/2602.03688) / [MOC](https://arxiv.org/html/2606.02359v1) /
  [CONCAT](https://arxiv.org/pdf/2605.29612) — message-graph redundancy pruning; more messaging ≠
  more coordination.
- Voyager (arXiv 2305.16291) + [SkillOps](https://arxiv.org/pdf/2605.13716) /
  [EvoSkill](https://arxiv.org/pdf/2603.02766) / [SkillBrew](https://arxiv.org/pdf/2605.29440) —
  verified reusable artifacts as the strongest cross-agent knowledge transfer.
- [Evidence Over Plans: Online Trajectory Verification for Skill Distillation](https://arxiv.org/pdf/2605.09192)
  — verify before distilling (our ledger evidence gate).
- [LatentMAS](https://www.emergentmind.com/papers/2511.20639),
  [DroidSpeak](https://arxiv.org/abs/2411.02820), [KVComm](https://arxiv.org/html/2510.03346),
  [HyLaT](https://arxiv.org/pdf/2605.25421),
  [unified latent-communication survey](https://arxiv.org/pdf/2606.05711),
  [LCGuard](https://arxiv.org/abs/2605.22786) — the latent-channel line and why it doesn't apply
  to heterogeneous API rosters yet.
- [EcoLANG](https://arxiv.org/pdf/2505.06904) — induced agent dialects (rejected: auditability).

## 7. Second autopsy: the VPA continuation run (2026-07-09)

With Forum 2.0 live, a `--continue` run on VerifiedProbabilisticAlgorithms still under-used the
substrate: 141 posts, **0 replies, 0 endorsements, 0 objections, 0 votes on any dimension**; only
43/141 posts used typed acts (claims 15, results 11, handoffs 5, decisions 4, ledger 8) — the other
98 were free-form `forum_post` prose that *content-wise* were claims and merge reports. Merges
happened without the consensus gate ever being exercised, and the config still displayed the
legacy six vote dimensions.

**Diagnosis.** Not an API issue — the agents communicated constantly; they just used the untyped
channel. The typed tools were documented only in the TOOLS.md appendix, while every *phase prompt*
(PROVING, FORMALIZING, …) still spoke Forum 1.0: "sign up via the forum", "the team votes on the
forum", "post to the forum". Instructions at the point where work is assigned dominate a tool
catalog appended afterwards. Same lesson as §1, one level up: **the consumption contract must be
stated in the prompt that assigns the work, not in the reference manual.**

**Changes (2026-07-09):**
- `prompts/FORUM_CONTRACT.md` — a single authoritative typed-coordination contract
  (claim → result → endorse/object → consensus-gated merge; obstacle; question/answer;
  evidence-gated ledger; brief), now injected into **every worktree phase** by
  `run_worktree_phase`, whose task string also names the acts explicitly.
- All 44 phase prompts migrated off Forum 1.0 language: `forum_claim` for sign-ups,
  `forum_result` + `forum_endorse`/`forum_object` for consensus (vote language removed),
  `forum_obstacle` for blockers, `forum_question` for uncertainty, `forum_brief` for reading.
- Reciprocity norm in the contract: after each of your results, review ≥1 open teammate result and
  endorse/object; answer questions addressed to you before claiming new chunks; the primary must
  check `forum_consensus` before merging.
- Retrospectives now graduate `ledger_get()` entries into `~/.unity/library/`.
- Legacy six-dimension configs (untouched defaults) migrate to the current two on load, in both the
  MCP server and the web viewer — fixes the stale dimension display on continued projects.
