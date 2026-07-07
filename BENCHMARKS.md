# Unity — Benchmark & Case-Study Plan (for the paper)

## The two claims every experiment serves

- **H1 (efficiency):** a roster of *small/free* models in Unity ≈ one frontier model working solo,
  at a fraction of the cost. ("10 sonnets ≈ 1 opus, cheaper")
- **H2 (capability):** a roster of *frontier* models in Unity > one of those models working solo.
  ("10 opuses > 1 opus")

Every benchmark below runs (up to budget) three conditions:
**(A)** single frontier model solo — the baseline; **(B)** Unity small-swarm (H1); **(C)** Unity
frontier-swarm (H2). Condition A should run *through Unity with a 1-agent roster* so the harness is
controlled and only multi-agency varies. **Each comparative benchmark is therefore (at least) two
comparison runs against its baseline: the H1 run (weaker models matching the solo frontier for less
money) and the H2 run (a bunch of the frontier model beating itself solo).** Per-run cost/wall-time
now lands in `.unity/logs/run.jsonl` (see `changes2.md`) — cite those numbers.

**Ready-made rosters: `benchmarks/rosters/`** — one agents.yaml per condition per benchmark, with a
README mapping every experiment to its files and the env vars they expect. Chunk allocation uses
**dynamic capability re-ranking** (static `strength` + live forum-credit boost; see `changes3.md`),
so swarm standings adjust to who is actually delivering during a run.

## Compute inventory → roles

| Source | Models | Role |
|---|---|---|
| Claude Pro (subscription; usage-window limited, no $ meter — report tokens + API-list-price estimates) | opus-4-8, opus-4-7, **opus-4-6** (FormalQualBench comparability), sonnet-5, sonnet-4-6, haiku-4-5, opus-3 | frontier swarm (H2), baselines, primaries |
| FreeInference ($20/day, ≤2 instances each) | glm-5.1, glm-5-turbo, minimax-m2.5, minimax-m3, qwen3.6-35b | small swarm (H1). *Excluded:* bge-m3 (embeddings), diffusiongemma (non-agentic) |
| OpenRouter free tier | any `:free` (nemotron-550b, hunyuan, poolside-laguna, …) | swarm filler / heterogeneity |
| Babel vLLM (scripts in `benchmarks/babel/`) | qwen3-32b, goedel-prover-v2-32b (proof-search grunt), llama33-70b | self-hosted swarm members via **codex backend** (OpenAI-compatible) |
| Tentative (advisor) | OpenAI + Gemini keys | drop into H2 rosters for cross-provider heterogeneity if they materialize |

**Standard rosters** (tune per benchmark):
- `SOLO-FRONTIER`: 1× opus-4-8 (or opus-4-6 where comparability demands).
- `SWARM-SMALL` (H1): primary haiku-4-5 (str 6) + glm-5.1 + minimax-m2.5 ×2 + qwen3.6-35b ×2 +
  babel qwen3-32b + 1–2 OR-free (str 3–5). ~8 agents, ~$0/tok + $20/day cap.
- `SWARM-FRONTIER` (H2): primary opus-4-8 (str 9) + opus-4-7 + sonnet-5 + opus-4-6 (+ gpt/gemini
  if keys). 4–6 agents.

Babel `agents.yaml` snippet (after `sbatch ~/unity-models/qwen3-32b.sh`, note the node's hostname):
```yaml
  - names: [qwen-1]
    model: qwen3-32b
    backend: codex
    provider: vllm
    base_url: http://<node>:8000/v1
    api_key: unity
    strength: 4
```

## Per-command plan

### `prove` — **FormalQualBench** (Math Inc; 23 problems)
- **Why:** published Opus-4.6 baselines from Math Inc; MerLean-Prover solved 10/23 — external
  reference points for free.
- **Conditions:** A = 1× opus-4-6 (matches Math Inc), B = SWARM-SMALL (+ goedel-prover-v2-32b as a
  dedicated proof grunt), C = opus-4-6 ×3 + sonnet-5 (keep 4.6-centric so C vs A isolates
  multi-agency, not model generation).
- **Metrics:** problems solved (builds, sorry/axiom-free, `lean_verify` clean), cost/solve, wall
  time. Full 23 for A & C; if Pro limits bite, ablate B on a fixed 10-problem subset.

### `solve` — **FirstProof + Formal Conjectures** (benchmarks) + **Erdős problems** (case studies)
- **FirstProof:** 10 research-level problems; the *Second Batch community round is live right now
  (Community Week #2 started July 1, 2026)* — run Unity and submit; Aletheia's 6/10 is the bar.
- **Formal Conjectures** (google-deepmind): open, evolving benchmark of *formally stated* open
  conjectures — statements are already Lean, so faithfulness is free and every outcome is
  kernel-checkable. Select a fixed slice (e.g., 10–15 Erdős-tagged + graph-theory entries at
  mixed difficulty), report proved / refuted / verified-partial-progress per condition.
- **Erdős:** pick 3–5 from erdosproblems.com / the formal-conjectures repo
  (formalized statements = free faithfulness): 2 recently-resolved (sanity, ground truth exists) +
  2–3 open-but-approachable (any progress is publishable; a rigorous "here's why X fails" also
  counts per Unity's determination design).
- **Conditions:** capability play → C only (SWARM-FRONTIER, max heterogeneity: add glm-5.1 +
  minimax-m3 — diverse strategy pools are the point), with A = 1× opus-4-8 for contrast.
- **Metrics:** FirstProof expert protocol; for Erdős, Lean-verified proof or verified partial
  progress (merged sorry-free lemmas).

### `autoformalize` — **ProofNet# + RLMEval** (benchmarks) + a whole-paper case study
- **ProofNet#** (corrected Lean-4 ProofNet): statement-level autoformalization, standard in the
  literature. **RLMEval**: research-level statements from real Lean projects — closer to Unity's
  whole-paper ambition.
- **Case study** (no whole-paper benchmark exists — this is the gap Unity fills): two sources:
  1. *Niven's "A simple proof that π is irrational"* (1 page) — result exists in Mathlib, so
     faithfulness is checkable against a reference;
  2. *sards.pdf* (Sard's theorem) — not in Mathlib, the honest hard case, and directly comparable
     to Unity v1's attempt (a built-in ablation vs the old architecture).
- **Head-to-head vs LeanMarathon** (arXiv 2606.05400, June 2026): the closest competing system — a
  multi-agent, blueprint-DAG, paper-level Lean autoformalization harness. Run `unity autoformalize`
  on **its published evaluation suite** (two 2026 papers spanning four Erdős problems, seven target
  theorems, which LeanMarathon formalizes sorry-free while a commercial-agent baseline fails).
  Matching or beating a purpose-built competitor on its own suite is the strongest autoformalize
  result available; also adopt its adversarial **target-fidelity audit** as an additional
  faithfulness measure alongside the metrics below.
- **Faithfulness measurement** (required): typecheck + **BEq/BEq+** where a reference
  formalization exists; **FormalAlign**-style alignment score + round-trip informalization with an
  LLM judge (use a model *not* in the roster, e.g. gemini if the key lands, else opus-3) where
  none does; LeanMarathon-style target-fidelity review; plus a human audit of a 20-statement
  sample. Report all of them.
- **Conditions:** A/B/C as standard (A = 1× sonnet-5 to keep Pro budget for `prove`/`solve`).

### `formalize` — **HoTTLean case study** + **miniCTX-style benchmark**
- **Case study:** finish the HoTTLean axioms (`EqTp.inv_pi/inv_sigma/inv_Id`), source =
  logrel-coq (the Coq proofs to translate). This resisted ~33 runs of Unity v1 — v2 closing even
  one axiom is a headline result; v1 logs are the baseline.
- **Benchmark:** **miniCTX** (theorem completion with real-project context) — the closest public
  fit to "formalize into an existing project." Statements are fixed → faithfulness is *automatic*:
  statement-preservation diff + no-new-axioms audit (`lean_verify`).
- **Conditions:** C-focused (hard targets): opus-4-8 primary + sonnet-5 + goedel-prover-v2-32b +
  qwen3-32b; A = 1× opus-4-8.

### `create` — **PraLean case study** + tactic-generation task suite
- **PraLean:** recreate from its theory document with `unity create`; compare v1's PraLean run
  (completed chunks, sorry count, faithfulness of the monad-law statements) — same-source ablation
  of v2 vs v1.
- **Tactic suite** (5 tasks, each judged by a held-out Lean test file of goals the tactic must
  close): sampling-correctness tactic (à la VPA), a `positivity` extension, an assumption-search
  tactic, a parity/omega-lite decision procedure, a rewrite-search tactic.
- **Conditions:** B-focused (breadth-heavy, cheap): SWARM-SMALL w/ sonnet-5 primary; A = 1×
  opus-4-8.

### `verify` — case study: **the Rust `binary_search` overflow story** (+ musl string functions)
- Verify the *current* Rust std `binary_search` correct in Lean, then feed the pre-2016 version
  with the classic `(lo+hi)/2` overflow — Unity must **find and prove the bug** (showcases the
  "prove why there is no proof" determination; great narrative, well-known ground truth).
- Secondary: musl libc `memchr`/`strlen` (small, famous, self-contained C).
- **Conditions:** C roster (opus-4-8 primary + sonnet-5 + qwen3-32b + minimax-m3); A = 1× opus-4-8.
- **Metrics:** properties proven / bug exhibited with kernel-checked counterexample derivation,
  faithfulness of the code model (human-audited against the source).

### `bump` — case study: PraLean + one community repo
- PraLean (its old toolchain → current stable) + one Reservoir project untouched for ≥6 months,
  <5k LOC (pick at run time).
- **Metrics:** builds at target, zero statement diffs (automatic), no new sorries/axioms, cost.
- **Conditions:** B-focused (mechanical work is the cheap-swarm sweet spot): SWARM-SMALL; A = 1×
  sonnet-5.

### `optimize` — benchmark **against ImProver / ImProver2**
- Their metrics are already Unity's shipped defaults (length, readability/modularity); run on the
  ImProver evaluation subsets (Mathlib/Compfiles selections from the papers) and compare score
  deltas + correctness preservation against the published numbers; rerun ImProver itself if the
  harness is easily revivable.
- **Conditions:** B (qwen3.6-35b ×2 + glm-5-turbo + haiku primary) vs A (1× sonnet-5) — optimize
  is where "free tokens, many tries" should shine.

## Run protocol (all experiments)
1. Fresh project per run; roster in `.unity/agents.yaml`; seed/scope in `UNITY.md`; sources via
   `unity source add`.
2. Fixed `MAX_ATTEMPTS=5`; record `.unity/logs/run.jsonl`, the forum, and `dag.json` per run
   (archive `.unity/` as the run artifact).
3. One repetition minimum for expensive conditions; 3 for cheap swarm conditions (report variance).
4. Claude Pro has no dollar meter: report measured tokens/turns and *estimated* cost at public API
   prices, clearly labeled; FreeInference is capped $20/day — schedule B-runs across days.

## Risks / notes
- Pro rate windows are the binding constraint on A/C conditions → order of spend: `solve` (live
  FirstProof round) > `prove` > `formalize` > the rest.
- FormalQualBench/FirstProof/miniCTX/ProofNet#/RLMEval/formal-conjectures: verify exact problem
  counts + harnesses when wiring runs (links in paper notes).
- tate-babel setup is pending a working password (`benchmarks/babel/setup-tate.sh` is ready to
  run); babel + riyaz-babel are set up with `~/unity-models/{qwen3-32b,goedel-prover-v2-32b,llama33-70b}.sh`.
