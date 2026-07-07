# changes2 — benchmark-motivated changes (2026-07-07)

Changes made to Unity specifically in service of the benchmark goals (H1: small-swarm ≈ big-solo,
cheaper; H2: big-swarm > big-solo). Kept deliberately small — the architecture didn't need changes.

## 1. Per-run cost/usage logging (`unity/spawn.py`) ✅
The paper's cost claims need per-agent accounting. Every `spawn()` now appends a line to
`<project>/.unity/logs/run.jsonl` (found via the parent-walk, so worktree agents log too):

```json
{"ts": "...", "agent": "scout-1", "model": "...", "backend": "claude_code",
 "seconds": 512.3, "cost_usd": 0.42, "num_turns": 31}
```

- claude_code: `cost_usd` + `num_turns` from the SDK's ResultMessage (subscription runs report 0 —
  use tokens/turns + API list prices, as noted in BENCHMARKS.md).
- codex: no spend figure in the SDK; token counts from `TurnResult.usage` are logged instead.
- Best-effort: logging never fails a run (silently skipped outside a `.unity` project).

## 2. Babel model serving (`benchmarks/babel/`) ✅ (babel, riyaz-babel) / ⏳ (tate)
- `setup-babel.sh` (ran on **babel** and **riyaz-babel**): creates conda env `unity`
  (python 3.12 + uv + vllm; existing env overridden as permitted) and writes
  `~/unity-models/{qwen3-32b,goedel-prover-v2-32b,llama33-70b}.sh` — sbatch scripts that serve
  vLLM OpenAI-compatible on port 8000 (api key `unity`), HF cache on `/data/user_data/$USER/hf`.
  Env installs were launched in the background — check `~/unity-env-setup.log` for
  `UNITY_ENV_READY` before first sbatch. (The babel login node dropped SSH right at my final
  status check; the installs run server-side regardless — if the log shows a failure, just rerun
  `benchmarks/babel/setup-babel.sh` over ssh.)
- **tate-babel: NOT set up** — the password `zermellian` was rejected by
  `trowney@login.babel.cs.cmu.edu` (3 attempts; I stopped to avoid a lockout). When you have the
  right password: `ssh tate-babel 'bash -s' < benchmarks/babel/setup-tate.sh`.
- Wiring: babel models plug into Unity via the **codex backend** (vLLM speaks OpenAI wire format):
  see the agents.yaml snippet in BENCHMARKS.md. GPU sizing: 32B ⇒ `gpu:2` (TP2), 70B ⇒ `gpu:4`
  (TP4) on 48GB L40S/A6000. Your usual debug-partition line still works for interactive tests
  (`srun --partition=debug --time=12:00:00 --cpus-per-task=12 --gres=gpu:2 --mem=256G --pty bash`).

## Considered, not done (didn't need code)
- Dynamic capability re-ranking for allocation: static `strength` suffices for the planned rosters.
- Codex budget enforcement: no SDK knob; token counts are now logged, so overspend is at least
  visible. Revisit if codex agents become budget-critical.
