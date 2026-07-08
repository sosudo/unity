# Rosters — which agents.yaml for which run

Copy the file to `<project>/.unity/agents.yaml` before each run. **Every comparative benchmark is
(at least) two comparison runs against its solo baseline:**
- **H1 run** (efficiency): a swarm of *weaker/free* models that should ≈ match the solo frontier
  baseline at a fraction of the cost.
- **H2 run** (capability): a swarm of *frontier* models that should beat the same solo baseline.

| Experiment | A: solo baseline | H1 run | H2 run |
|---|---|---|---|
| prove — FormalQualBench | `solo-opus-4-6.yaml` | `prove-fqb-h1.yaml` | `prove-fqb-h2.yaml` (opus-4-6 ×4) |
| solve — FirstProof / Formal Conjectures / Erdős | `solo-opus-4-8.yaml` | — (capability play) | `solve-h2.yaml` |
| autoformalize — ProofNet# / RLMEval / LeanMarathon suite / case studies | `solo-sonnet-5.yaml` | `autoformalize-h1.yaml` | `autoformalize-h2.yaml` |
| formalize — HoTTLean / miniCTX | `solo-opus-4-8.yaml` | — | `formalize-h2.yaml` |
| create — PraLean / tactic suite | `solo-opus-4-8.yaml` | `create-h1.yaml` | — |
| verify — binary_search / musl | `solo-opus-4-8.yaml` | — | `verify-h2.yaml` |
| bump — PraLean + community repo | `solo-sonnet-5.yaml` | `bump-h1.yaml` | — |
| optimize — vs ImProver | `solo-sonnet-5.yaml` | `optimize-h1.yaml` | — |

Where H1 or H2 is "—", that axis isn't the claim being tested for that command; add the missing
condition later if budget allows (the roster files compose freely).

## Environment variables (put real values in `<project>/.unity/.env`)

| Var | Meaning |
|---|---|
| `FREEINFERENCE_BASE_URL` / `FREEINFERENCE_API_KEY` | Harvard FreeInference: use `https://freeinference.org/v1` (OpenAI wire → codex backend; `/v1/models`, `/v1/responses` incl. SSE all verified live). Model ids as listed: `glm-5.1`, `glm-5-turbo`, `minimax-m2.5`, `minimax-m3`, `qwen3.6-35b`. ≤2 instances/model, $20/day. **Do not use the `/anthropic` gateway with claude_code yet** — as of 2026-07-07 it 404s every route/auth variant (server-side; the claude CLI reaches it but gets "issue with the selected model"). All FreeInference roster entries therefore use the codex backend. |
| `OPENROUTER_ANTHROPIC_URL` / `OPENROUTER_API_KEY` | OpenRouter free tier via claude_code — reuse the exact base_url from the working VerifiedProbabilisticAlgorithms run. |
| `BABEL_*_URL` | With the `babel-compute-node` tunnel up: `http://localhost:<port>/v1`, api key literally `unity`. vLLM serves `/v1/responses`, so the codex backend works against it (verified live 2026-07-07). See the babel model table below. |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | tentative; uncomment the stanzas in `solve-h2.yaml` if they land. |

## Babel models (`~/unity-models/*.sh` on babel; repo copies in `benchmarks/babel/models/`)

| Script | Model | Port | GPUs | Env var | Notes |
|---|---|---|---|---|---|
| `qwen3-32b.sh` | Qwen3-32B | 8000 | 2 | `BABEL_QWEN_URL` | verified live |
| `goedel-prover-v2-32b.sh` | Goedel-Prover-V2-32B | 8001 | 2 | `BABEL_GOEDEL_URL` | verified live |
| `hy3.sh` | Hunyuan 3 (295B MoE, FP8) | 8003 | 8 | `BABEL_HY3_URL` | **experimental** — arch may need a newer vLLM than the CUDA-12.9 driver allows; fallback: `tencent/hy3:free` on OpenRouter |
| `leanstral.sh` | **Leanstral-1.5-119B-A6B** (Mistral's Lean 4 prover; 587/672 PutnamBench) | 8004 | 4 (runtime FP8) | `BABEL_LEANSTRAL_URL` | the Goedel upgrade; co-fits with qwen+goedel or dsv4 |
| `deepseek-v4-flash.sh` | DeepSeek-V4-Flash (284B MoE, ~158GB) | 8005 | 4 | `BABEL_DSV4_URL` | strong self-hosted generalist |

**GLM-5.2 is deliberately absent**: it's 754B total — ~380GB at 4-bit, over the 8×48GB budget
before KV/overhead. You already get GLM-5.x hosted via FreeInference; use that.

Old-arch models (qwen/goedel) run in conda env `unity` (vllm 0.11.2); newer ones in `unity-new`
(newest cu128-compatible vLLM). The scripts pick their env themselves.

## Rerunning babel models

1. `ssh babel`, `cd ~/unity-models`, `sbatch <model>.sh` (any subset; ports never collide).
2. `./status.sh` — shows job state, ready URLs, and the exact `LocalForward` lines to paste.
3. Update the `babel-compute-node` block in local `~/.ssh/config`: set `HostName` to any one
   ready node and one `LocalForward <port> <node>:<port>` line per model (from `status.sh`).
4. `ssh -N -f babel-compute-node` → every model is `http://localhost:<port>/v1` locally.

Each roster's babel subset fits the 8-GPU cap on its own (one benchmark at a time):
prove-fqb-h1 / formalize-h2 = goedel(2)+qwen(2)+leanstral(4) = 8; babel-swarm-h1 =
dsv4(4)+leanstral(4) = 8; hy3(8) is standalone-only.

First run of a new model downloads weights to `/data/user_data/$USER/hf` (can take a while);
`status.sh` shows `boot` until the server logs ready. Jobs have a 2-day walltime.

Claude models authenticate through the logged-in Claude Code CLI (Claude Pro) — no keys in yaml.
`budget:` on FreeInference agents is documentation + claude-side enforcement only (codex has no
spend cap; token usage is logged to `.unity/logs/run.jsonl`).
