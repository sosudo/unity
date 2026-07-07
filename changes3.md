# changes3 — H1/H2 readiness + dynamic capability re-ranking (2026-07-07)

## Do we need code changes to hit H1 and H2?

Mostly no — the architecture already carries both claims: per-agent env/provider isolation lets
mixed rosters run concurrently, the sign-up cap + strength guidance spreads weak swarms across the
frontier (H1), consensus voting + primary tie-break is the mechanism by which N frontier agents
should beat one (H2), and `run.jsonl` (changes2) supplies the cost evidence. Two things *were*
worth changing:

## 1. Dynamic capability re-ranking for allocation (`unity/orchestrator.py`) ✅
Static `strength` can't tell a swarm that a nominally-weak agent is outperforming (or that a
frontier agent is flailing) mid-run — exactly the allocation signal H1 swarms need. Now:

- `_effective_ranking(roster, forum_dir)`: **effective capability = base strength + a bounded
  boost from forum ICRL credit** (`balance / 10`, clamped to [−1, +3]). Credit is earned by posts
  and upvotes on an agent's contributions — i.e., by delivering — so the ranking tracks *realized*
  performance, not the initial guess. Recomputed at **every dispatch** from
  `.unity/forum/balances.json`.
- `_preamble(...)` now shows the team **sorted by live standing**, each agent annotated
  `standing #N, effective capability X.X (base strength S)`, plus an instruction that sign-ups
  should follow *current* standings, which re-rank from forum credit as the run progresses.
- Bounded on purpose: credit can promote a small model by at most +3 and demote by at most −1, so
  a chatty-but-wrong agent can't vote itself to the top, and the primary's role never changes.
- Degrades gracefully: no forum / no balances → static strengths, unchanged behavior.
- Verified: boost + penalty clamping, live ordering in the preamble, fallback without `.unity/`.

## 2. Per-benchmark rosters (`benchmarks/rosters/`) ✅
13 ready-made `agents.yaml` files — one per condition per benchmark — plus a README mapping every
experiment to (A: solo baseline, H1 run, H2 run) and documenting the env vars
(`FREEINFERENCE_*`, `OPENROUTER_*`, `BABEL_*_URL`, tentative `OPENAI/GEMINI`). All 13 parse
through `load_roster`. Conventions: Claude Pro models ride the CLI login (no keys in yaml);
FreeInference + babel vLLM go through the codex backend (OpenAI wire); OpenRouter free reuses the
claude_code wiring from the working VerifiedProbabilisticAlgorithms run.

## Benchmark-plan additions (BENCHMARKS.md, additive)
- **solve**: + **Formal Conjectures** (google-deepmind) as a second benchmark — formally-stated
  open conjectures, faithfulness free, kernel-checkable outcomes.
- **autoformalize**: + **LeanMarathon** (arXiv 2606.05400) — the closest competing system
  (multi-agent blueprint-DAG paper-level autoformalization). Run Unity on its published eval suite
  (2 papers / 4 Erdős problems / 7 theorems, sorry-free) as a head-to-head, and adopt its
  adversarial target-fidelity audit as an extra faithfulness measure. (This is the "faithfulness
  benchmark from LeanMarathon or another paper" — it turned out to be a rival harness with a
  fidelity protocol rather than a metric suite; kept everything else as-is, purely additive.)
- Explicit **two-run protocol** language: every comparative benchmark = H1 run + H2 run against
  the same solo baseline.
