# Unity 0.5.0 — finalization changes (2026-07-07)

Overview of everything changed in the autonomous finalization session. Verified claims are marked ✅;
things I could not test live are flagged under **Known gaps**.

## 1. Packaging — root causes found and fixed ✅
- **`--prerelease=allow` mystery**: `openai-codex` has *only* beta releases (latest `0.1.0b2`).
  Fixed by pinning `openai-codex>=0.1.0b2` — an explicit pre-release specifier lets uv resolve it
  normally. `uv tool install .` now works with no flags (verified).
- **`unity.forum` missing from installs**: the repo `.gitignore` had `forum/` (from the old
  architecture) and **hatchling excludes VCS-ignored files by default**, so `unity/forum/` was
  silently dropped from every wheel. Fixed twice over: `.gitignore` rewritten without `forum/`,
  and `[tool.hatch.build] ignore-vcs = true`. Verified: the installed tool imports
  `unity.forum.server` / `unity.forum.web`, and prompts + default metrics ship in the wheel.

## 2. Repo restructure — only the new Unity remains ✅
- Deleted: `unity_agent/` (entire old version), all runtime forum dirs (`forum/`, `a_forum/`,
  `forum2/3/_c/_sards/`), audit/notes files (`AUDIT.md`, `ISSUES.md`, `OBSERVATIONS.md`,
  `PROPOSED_FIXES.md`, `FORUM_*`, `PHASE_RUNNER_REFACTOR.md`, `COMPETITIVE_REVIEW.md`, old
  `UNITY.md`, `TODO`), logs/outputs (`logs`, `unity-*.out`), LaTeX + PDFs (`main.tex`, `poster.*`,
  papers), old `pyproject.toml`/`uv.lock`/`.env`/`.env.example`/`README.md`, and the stray
  `unity/forum` runtime dir.
- The package was hoisted from `unity/unity/` to `unity/` at repo root; new root `pyproject.toml`
  (version **0.5.0**), fresh `README.md` (install + usage), minimal `.gitignore`, `.mcp.json`
  updated to `unity.forum.server`.
- Verified end-to-end: `uv tool install .` → `unity version` = 0.5.0, all 23 commands registered.

## 3. Codex backend — rewritten against the real SDK ✅
Installed `openai-codex 0.1.0b2` and inspected its actual API; the spawner had been written from
docs and didn't match. Now uses `AsyncCodex` (native async, no thread-bridging):
`login_api_key` → `thread_start(model, model_provider, sandbox, base_instructions, cwd)` →
`thread.turn(prompt)` → `handle.stream()` (logged + idle-guarded) → `handle.run().final_response`,
with `codex.close()` in a finally. Sandbox mapping fixed: `bypassPermissions → full_access`,
anything else → `workspace_write` (was wrongly `read_only` for acceptEdits).
- **agents.yaml requirement change (recorded)**: the `codex` backend now **requires `api_key`**
  (that's what `login_api_key`/`CODEX_API_KEY` consume; `auth_token` is not used by codex).
  Validated in `roster.py` with a clear error.

## 4. Global library — agents can actually use it ✅
- `library.ensure_library()` creates `~/.unity/library/{tactics,lemmas,references,subagents,skills}`;
  `unity init`/`new` seed it.
- The context manifest now also surfaces `skills/` including `skills/<name>/SKILL.md` layouts.
- Verified: manifest injection into every dispatched prompt, subagent registration on claude
  (`AgentDefinition`) and codex (`CODEX_HOME/agents/*.toml`).

## 5. Interactive commands ✅ (mock-tested)
- New `unity/interactive.py`: REPL with the **primary** agent — claude via `ClaudeSDKClient`
  (true multi-turn), codex via one thread with repeated turns. `exit`/`quit`/Ctrl-D to leave.
- `unity agent`: general Hermes-style session (system prompt + TOOLS catalog + library context).
- `unity doctor`: resolver session — surveys build state, `.unity/` integrity, stale flags,
  dead worktrees; proposes and applies fixes.

## 6. Housekeeping commands ✅
- `unity reset`: confirm-gated wipe of `~/.unity/library/` back to the empty skeleton.
- `unity clean`: prunes empty `.md`s, subagents without valid frontmatter, and skill dirs without
  `SKILL.md`.
- `unity new` **no longer crashes on non-Mathlib projects**: `lake exe cache get` only runs when
  the lakefile/manifest actually mentions Mathlib (`lake.has_mathlib`).

## 7. Engine fixes & optimizations ✅
- **Per-agent budgets are now enforced (claude)**: `agents.yaml` `budget` flows into
  `ClaudeAgentOptions.max_budget_usd`. (Codex has no equivalent knob — see gaps.)
- **Retry caps**: both spawners kept your 10-minute retry-on-API-error, now capped at 6 attempts so
  a permanently-broken agent (bad key/dead provider) can't hang a phase forever.
- **Worktree crash-resilience**: `create_worktree` prunes stale worktrees/branches from a crashed
  run before re-adding; `cleanup_worktree` is best-effort (a half-removed worktree no longer aborts
  the other agents' cleanup). Verified with a simulated crash + double-cleanup.
- **`MAX_ATTEMPTS` unified**: `init` now writes `MAX_ATTEMPTS=5` (was `MAX_FORMALIZATION_ATTEMPTS`,
  which none of the pipelines read anymore).

## Known gaps / notes for later
- **Codex budget**: not enforced (SDK exposes no spend cap). Option: track usage from
  `TurnResult.usage` and stop the agent when exceeded.
- **Codex subagents**: written as custom-agent TOMLs (`name`/`description`/`developer_instructions`)
  per the docs; not exercised against a live Codex account.
- **Codex approval mode**: left at the SDK default (`auto_review`); if a live run ever blocks on
  approvals, pass `approval_mode` explicitly in `thread_start`.
- **Interactive commands**: REPL flow verified with a mocked client; not yet exercised against the
  live CLI.
- The dev `.venv` still contains old-package deps; `uv sync`/recreate at leisure — the installed
  tool has its own environment.
