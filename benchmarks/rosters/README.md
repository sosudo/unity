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
| `BABEL_QWEN_URL` / `BABEL_GOEDEL_URL` | With the `babel-compute-node` tunnel up (`ssh -N -f babel-compute-node`; its config carries both forwards): `http://localhost:8000/v1` (qwen3-32b) and `http://localhost:8001/v1` (goedel-prover-v2-32b); api key is literally `unity`. After resubmitting jobs, update the node names in `~/.ssh/config` (`HostName` + the goedel `LocalForward`) to the new `squeue` nodes. vLLM serves `/v1/responses`, so the codex backend works against it (verified live 2026-07-07). |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | tentative; uncomment the stanzas in `solve-h2.yaml` if they land. |

Claude models authenticate through the logged-in Claude Code CLI (Claude Pro) — no keys in yaml.
`budget:` on FreeInference agents is documentation + claude-side enforcement only (codex has no
spend cap; token usage is logged to `.unity/logs/run.jsonl`).
