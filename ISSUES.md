# Unity Pipeline — Issue Inventory

Audit scope: trace every computation path; surface stalls, loops, premature end_turns, broken mechanisms, and source-collapse risks. No code changes; recording only. Citations are `pipeline.py:LINE` unless prefixed with another path.

Last run (logs file, ~3h39m, scancel'd):
- source-scan ok (28 turns, $6.37)
- generation: **1 turn end_turn, $0.0013, no chunks written**
- validation: **1 turn end_turn, $0.0016, no VALIDATION_REPORT.md**
- pipeline logged "proceeding anyway" → empty toposort → vacuous formalization → critic discovered the cascade
- exploration phase (later): **1 turn end_turn, $0.0019**

The cascade is a collision of (a) the SDK regression that caused short turns *and* (b) several fail-open paths in the pipeline that turn "agent ended without producing output" into "phase succeeded; downstream gets empty input."

---

## Severity 1 — Cascading silent failures

### S1.1 Validation loop fails open on missing report
**Location:** `pipeline.py:2148-2150` (Path 1), `pipeline.py:1689-1691` (Path 2)
```python
except FileNotFoundError:
    logging.warning("No VALIDATION_REPORT.md found — proceeding anyway.")
    break
```
**Effect:** When the validation agent ends_turn without writing the report (the exact failure mode in last run), the loop breaks and downstream proceeds with whatever (possibly empty) `language/` exists.
**Should be:** Treat missing report as INVALID and continue the loop, capped by `max_validation_iterations`.

### S1.2 Critic loop fails open on missing report
**Location:** `pipeline.py:2918-2920` (Path 1)
```python
except FileNotFoundError:
    logging.warning("No REPORT.md found after critic phase — stopping loop.")
    break
```
**Effect:** Iteration loop terminates if critic ended without writing REPORT.md. Symmetrical to S1.1 — same fail-open class.
**Should be:** Treat missing report as NEEDS_REVISION and continue the iteration loop, capped by `max_critic_iterations`.

### S1.3 Toposort silently no-ops on empty `language/chunks/`
**Location:** `pipeline.py:220-225` and call site `pipeline.py:2152-2157`
```python
if not chunks_dir.exists():
    logging.info("No language/chunks/ directory — skipping toposort.")
    return
```
**Effect:** When generation produced no chunks, toposort writes no `dag.json`, formalization then logs "creating worktrees for 0 chunk(s)" and the iteration completes as a no-op.
**Should be:** In source mode, missing/empty `language/chunks/` after the validation loop is a loud failure → re-trigger generation via resolver, not skip.
**Severity note:** Logged as INFO, should be ERROR/CRITICAL — log level masks the cascade.

### S1.4 Validation inner-loop "success" suppresses resolver
**Location:** `pipeline.py:2106-2134` (the inner `while True: try: query; break; except: invoke_resolver`)
**Effect:** When the agent ends_turn cleanly (no exception) but writes nothing, the inner loop breaks normally; resolver is never invoked. The outer loop then hits S1.1 and proceeds. Resolver mechanism exists but doesn't fire on "empty success."

### S1.5 Same pattern on every other phase's inner loop
Generation `:2085-2102`, semiformalization `:2173-2208`, exploration variants `:2358-2530`, formalization F/T branches `:2580-2733`, critic `:2755-2841`. All have the same shape: "if query() returned without raising → success; otherwise → resolver." None check that the expected output file/dir exists.

---

## Severity 2 — Closing-gate coverage

### S2.1 Only SOURCE_SCAN.md has an end_turn closing gate
The recently-added gate ("Verify `mathlib-context.md` exists in your CWD and is non-empty. ... this is mandatory. Do not end_turn until this file is present.") appears only in `unity_agent/PROMPTS/SOURCE_SCAN.md:57`. Every other phase prompt lacks an analogous gate.

Vulnerable prompts (each is the exact failure surface for an SDK short-turn or model confusion incident):
- `PROMPTS/GENERATION.md` — must verify `language/chunks/*.json` non-empty + language repo committed
- `PROMPTS/VALIDATION.md` — must verify `VALIDATION_REPORT.md` exists + `**Status:**` line present
- `PROMPTS/SEMIFORMALIZATION/{FF,TF,TT}.md` — must verify `semiformal/chunks/` ID-matches `language/chunks/`
- `PROMPTS/EXPLORATION/{FF,FT,TF,TT}.md` — must verify `semiformal/` mutated OR record explicit no-op rationale
- `PROMPTS/CRITIC.md` — must verify `REPORT.md` at unity run dir (NOT project_path) with `**Status:**` line
- `PROMPTS/RETROSPECTIVE.md` — lower priority (best-effort), but library writes should be verified
- `PROMPTS/EXPLORATION.md` (Path 2) — must verify `gathered/` populated
- `PROMPTS/FORMALIZATION/{F,T}.md` — must verify each worktree had a commit OR the orchestrator merged via `UNITY: merge chunk <id>` for every chunk in `worktrees.json`
- `PROMPTS/FORMALIZATION/ESCALATION.md` — same as above for candidate chunks

---

## Severity 2 — Escalation tier configuration

### S2.2 Escalation primary tier uses `model="sonnet"` against the user's "everything opus" rule
**Location:** `pipeline.py:1371-1372`
```python
model_for_tier = "opus" if tier == "B" else "sonnet"
fallback_for_tier = "sonnet" if tier == "B" else "haiku"
```
Tier A (primary, research-model on aicohort) gets `model="sonnet" / fallback="haiku"`. Tier B (secondary OpenRouter) gets `model="opus" / fallback="sonnet"`. Conflicts with the rule applied to every other phase: `model="opus", fallback_model="sonnet"` (or `"opus"`).

---

## Severity 2 — Resolver retry semantics

### S2.3 Resolver retry cap defaults to None → unlimited respin
**Location:** `pipeline.py:1256` reads `RESOLVER_MAX_RETRIES`; `.env` does not set it.
**Effect:** A phase that keeps raising exceptions will respin the resolver indefinitely until phase budget exhausts (most are `None` per `.env` → effectively unbounded). Combined with the rate-limit detector that just sleeps and retries, a sticky API failure becomes an infinite loop.

### S2.4 Resolver counter never resets on success
**Location:** `pipeline.py:1252` (`_retries: dict[str, int]`), used at `:1257`
**Effect:** Counter accumulates across the entire pipeline run. A flaky-but-eventually-successful phase exhausts the (currently disabled) cap earlier than the user might expect.

### S2.5 Resolver doesn't trigger on "empty success" — see S1.4
The inner-loop pattern means resolver only fires on raised exceptions, not on agents that ended_turn without producing artifacts.

---

## Severity 3 — Looping & termination edges

### S3.1 `max_critic_iterations` default is 3 if env unset
**Location:** `pipeline.py:956`: `parse_int(...) or 3`. `.env` sets it to 5; safe currently.

### S3.2 `max_validation_iterations` default is None (unbounded)
**Location:** `pipeline.py:957`: no `or N` fallback. `.env` is also None. So validation can loop forever when (a) validator keeps emitting INVALID and (b) the report file does exist (otherwise S1.1 short-circuits).

### S3.3 No loop guard on the iteration loop other than critic-status
**Location:** `pipeline.py:2306` (Path 1) — `while True:` with `break` only on `**Status:** COMPLETE` or `iteration+1 >= max_critic_iterations`. If critic keeps writing NEEDS_REVISION and `max_critic_iterations` is unset, this loops forever.

### S3.4 `_invoke_resolver` rate-limit branch sleeps then `return` without bounded retry
**Location:** `pipeline.py:1271-1280`
```python
if _RATE_LIMIT_PATTERN.search(err_str):
    wait = 60
    ...
    await asyncio.sleep(wait)
    return
```
Inner loop will retry the query. If the rate limit persists, this is an unbounded sleep+retry cycle (the retry counter still increments but only triggers exit if `RESOLVER_MAX_RETRIES` is set — see S2.3).

---

## Severity 3 — Phase-level error swallowing

### S3.5 Retrospective errors are logged-only
**Location:** `pipeline.py:2882-2883`
```python
except Exception as e:
    logging.error(f"ERROR (retrospective phase): {e}")
```
Acceptable since retrospective is best-effort, but documented for completeness.

### S3.6 Escalation errors are logged-only
**Location:** `pipeline.py:2901-2904`
Same pattern; same acceptability rationale.

### S3.7 `_commit_phase` swallows commit failures
**Location:** `pipeline.py:80-90`
```python
except subprocess.CalledProcessError:
    pass
```
**Effect:** If the commit fails (e.g., CWD not a git repo, hooks reject, etc.), the phase appears to complete but no `PHASE:* status=complete` checkpoint exists. Downstream resolver reads "Last clean checkpoint: unknown" and operates without context.

### S3.8 `_commit_phase` uses `git add -A` at unity run dir
**Location:** `pipeline.py:87`
**Effect:** Stages every untracked file in the run dir (logs, scratch, etc.) into checkpoint commits. Pollutes history; not destructive.

---

## Severity 3 — Worktree lifecycle

### S3.9 EMERGENCY auto-commit can land broken proofs / junk on main
**Location:** `pipeline.py:389-418` (`_audit_worktree_commits` rescue path)
```
git -C <worktree> add -A
git -C <worktree> commit -m "EMERGENCY: auto-commit dirty worktree for chunk <id>"
```
**Effect:** If a subagent left the worktree dirty (didn't commit), this stages all untracked files and commits, then the orchestrator (or the audit's downstream merge logic) squash-merges into main. Risks:
1. Junk files (test scratch, .swp, etc.) get squashed into main.
2. Broken Lean code lands; next iteration's `lake build` catches it but iteration is wasted.

### S3.10 Stranded branches detected but never merged automatically
**Location:** `pipeline.py:426-431, 437-447`
**Effect:** If a worktree has commits but no `UNITY: merge chunk <id>` commit on main, audit logs ERROR + writes `MERGE_SKIPPED.md`, but does not auto-squash-merge. Cleanup then deletes the branch (`-D`). Work survives in `MERGE_SKIPPED.md` log only; the actual commits are lost when `git branch -D` runs.
**Cross-check:** `_cleanup_worktree:451-463` calls `git branch -D` unconditionally if the branch exists.

### S3.11 Worktree creation mutates user's Lean project `.gitignore`
**Location:** `pipeline.py:299-306`
**Effect:** Appends `.worktrees/` to `<project_path>/.gitignore`. Side effect on user's project repo. Probably benign but worth noting.

---

## Severity 3 — Toposort & DAG

### S3.12 Cycle in dependency graph silently appended as final layer
**Location:** `pipeline.py:262-265`
```python
remaining = {cid for cid, deg in in_degree.items() if deg > 0}
if remaining:
    logging.warning(f"Cycle detected in chunk dependency graph involving: {remaining}. Appending as final layer.")
    layers.append(sorted(remaining))
```
**Effect:** A circular dependency in IR is silently absorbed into a single layer. Formalization may try to build a chunk whose (cyclic) deps aren't in main yet → spurious build failures, no clear error trail back to the IR bug.

### S3.13 `_toposort_chunks` raises CRITICAL inside a try in pipeline body
**Location:** `pipeline.py:2152-2157`
```python
try:
    _toposort_chunks(Path("language"))
except Exception as e:
    logging.critical(f"CRITICAL (toposort): {e}")
    exit(1)
```
But `_toposort_chunks` itself never raises on missing dir — it just returns. So the `exit(1)` only fires on parse errors. Combined with S1.3, missing chunks pass through silently.

---

## Severity 3 — Path/prompt routing inconsistencies

### S3.14 Path 2 validation reads from `PROMPTS_DIR`, not `ACTIVE_PROMPTS_DIR`
**Location:** `pipeline.py:1645`
```python
with open(PROMPTS_DIR / "VALIDATION.md", "r") as f:
```
While other Path 2 phases (exploration, generation, semiformalization) use `ACTIVE_PROMPTS_DIR`. Means PROVE-mode prompt overrides for VALIDATION.md would never load. Probably intentional, but inconsistent.

### S3.15 Critic subagents (CRITIC/DECLARATIONFORMALIZER, CRITIC/PROOFFORMALIZER) always load from `_SUBAGENTS_DIR`, never `ACTIVE_SUBAGENTS_DIR`
**Location:** `pipeline.py:2750-2753, 2799-2802`
**Effect:** PROVE-mode overrides for critic subagents (if they exist) never load. Compare formalization at `:2657-2660`, where DECLARATIONFORMALIZER uses `_SUBAGENTS_DIR` (universal) but PROOFFORMALIZER uses `ACTIVE_SUBAGENTS_DIR` (overrideable). Inconsistent.

### S3.16 Retrospective reads from `PROMPTS_DIR`, not `ACTIVE_PROMPTS_DIR`
**Location:** `pipeline.py:2852`
Same pattern as S3.14. Probably intentional.

### S3.17 Path 2 validation tools omit WebSearch / WebFetch
**Location:** `pipeline.py:1651-1652`
```python
tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent", "Skill"],
```
Path 1 validation includes all tools. Path 2 validator can't WebSearch. May be deliberate (validation should be local) but inconsistent.

---

## Severity 3 — Field-propagation drift

### S3.18 `_assert_semiformal_field_propagation` is non-halting
**Location:** `pipeline.py:753-798`, called at `:2203-2205` etc.
**Effect:** Detects drift in `is_assumption / source_range / source_proof` between language and semiformal chunks but only logs ERROR and writes `SEMIFORMAL_FIELD_DRIFT.md`. The bad data flows downstream. Drift in `is_assumption` later surfaces as an `_audit_illegitimate_sorries` violation, but `source_range` / `source_proof` drift goes unnoticed.

---

## Severity 3 — Budget / cumulative spend

### S3.19 `formalization_budget` is per-call, not cumulative across iterations
**Location:** `pipeline.py:2612, 2699, 1436` (escalation)
**Effect:** Each iteration of the critic loop runs formalization with `max_budget_usd=formalization_budget`. With N iterations + escalation passes, total spend can be N × budget. Currently `None` per `.env`, so no practical impact.

### S3.20 Escalation cost attribution drift
**Location:** `pipeline.py:1444-1445, 1459-1460`
```python
if isinstance(message, ResultMessage) and getattr(message, "total_cost_usd", None) is not None:
    run_cost = float(message.total_cost_usd)
...
if tier == "B":
    state["secondary_spend"] = float(state.get("secondary_spend", 0.0)) + run_cost
```
`run_cost` is overwritten by each ResultMessage; only the **last** ResultMessage's total is added to secondary_spend. If the agent emits multiple ResultMessages (e.g. per subagent invocation), only the final one counts. Verify SDK behavior — if multiple ResultMessages are emitted in a single query, secondary_spend is undercounted.

### S3.21 SECONDARY_BUDGET check uses `>=`, not strict `>` — last allowable run can exceed cap
**Location:** `pipeline.py:1360-1367`
The check fires *before* a run, so if `secondary_spend == SECONDARY_BUDGET - epsilon`, one more run can push it well over. Mostly cosmetic.

---

## Severity 3 — REPORT.md path drift

### S3.22 `_read_report_md` auto-recovers misplaced REPORT.md
**Location:** `pipeline.py:1107-1120`
```python
misplaced = project_path / "REPORT.md"
if misplaced.exists():
    logging.warning(...)
    shutil.move(...)
```
**Effect:** Critic frequently writes REPORT.md to `project_path` instead of unity run dir; this code rescues it. Defensive, but masks a recurring agent bug. CRITIC.md prompt warns about location but the bug keeps happening — gate (S2.1) would catch it loudly.

---

## Severity 4 — Code smell / DRY

### S4.1 `_chunk_body_signatures` and `_collect_chunk_sorry_set` duplicate body-extraction logic
**Location:** `pipeline.py:556-598` vs `:601-644`
Same Lean-decl-boundary scan implemented twice; a refactor to share `_extract_chunk_body(...)` would reduce drift risk.

### S4.2 `_TOP_LEVEL_DECL` regex is permissive but doesn't handle `mutual` / `section` / `namespace`
**Location:** `pipeline.py:466-470`
**Effect:** A chunk inside a `namespace Foo` block has its body extracted up to the next `theorem|lemma|def|...`, ignoring `end Foo`. Sorry detection is still correct (it's just a substring scan), but body-hash signatures may include unrelated trailing content, breaking stagnation detection in unusual layouts.

### S4.3 `_KILL_PATTERN` only catches kill-by-name patterns
**Location:** `pipeline.py:1185-1189`
Doesn't catch `kill <PID>` with a pid the agent could have read from `/proc` or `ps`. Defense in depth issue, not a known exploit.

---

## No-finding items (verified safe)

### V1 No source path collapse
- Pipeline reads `source` as a path; never writes to it.
- Cleanup tail (`pipeline.py:2929-2944`) only removes `language/` and `semiformal/` in CWD, never `source`.
- All prompts (SOURCE_SCAN, GENERATION, SEMIFORMALIZATION, EXPLORATION, CRITIC, RETROSPECTIVE) say "read source", never "write to source".
- Source at `/Users/shivanshgour/Downloads/unity-agent/source` is safe.

### V2 LSP startup is bounded and fail-loud
- `pipeline.py:1506-1527`: 60-second wait with explicit CRITICAL exit on failure.

### V3 Lake init is bounded and fail-loud
- `pipeline.py:1480-1486`: `await _lake_init_task` with CRITICAL exit on failure.

### V4 SDK pin enforced
- `pyproject.toml:9`: `claude-agent-sdk==0.1.59`. Memory note documents the regression bisect.

### V5 All phase models are `model="opus"` (per latest restore)
- Verified: every `query(...)` call in `pipeline.py` for source-scan, generation, validation, semiformalization, exploration, formalization, critic, retrospective uses `model="opus"`. Fallbacks are mostly `"opus"` or `"sonnet"`. Resolver and infer use `model="opus"` with `fallback_model="sonnet"`. **Exception:** escalation tier A — see S2.2.

### V6 Filesystem-scope guardrails present in every prompt
- Every PROMPTS/*.md and SUBAGENTS/**.md ends with the "Filesystem scope (mandatory)" block enumerating forbidden NFS-stalling commands.

### V7 Self-kill guard hook present
- `pipeline.py:1191-1207` blocks `pkill|killall|kill -SIG... claude|unity-agent` patterns.

---

## Summary table — proposed fix priority

| # | Issue | Severity | Why it matters |
|---|---|---|---|
| S1.1 | Validation fail-open | HIGH | Root cause of last run cascade |
| S1.2 | Critic fail-open | HIGH | Symmetric to S1.1, same class |
| S1.3 | Toposort silent no-op | HIGH | Final breaker in cascade |
| S1.4 | Resolver doesn't fire on empty success | HIGH | Same root cause for S1.1–S1.3 |
| S2.1 | Closing gates only on SOURCE_SCAN | HIGH | Prevents recurrence at every phase |
| S2.2 | Escalation tier A model="sonnet" | MEDIUM | Violates "everything opus" rule |
| S2.3 | RESOLVER_MAX_RETRIES unset → infinite | MEDIUM | Stall risk on persistent failure |
| S3.2 | max_validation_iterations unbounded | MEDIUM | Stall risk on persistent INVALID |
| S3.7 | _commit_phase swallows commit errors | MEDIUM | Silent checkpoint loss |
| S3.9 | EMERGENCY commit can land junk on main | MEDIUM | Correctness regression on dirty worktree |
| S3.10 | Stranded branches not auto-merged | MEDIUM | Work loss on `git branch -D` |
| S3.12 | Cycle in DAG silently absorbed | MEDIUM | Masks IR bugs |
| S3.13 | Toposort try/except dead branch | LOW | Combined with S1.3 |
| S3.18 | Field drift non-halting | MEDIUM | Bad metadata flows downstream |
| S3.19 | formalization_budget per-call | LOW | No impact at current `None` |
| S3.20 | Escalation cost attribution | LOW | Possible undercount |
| S3.22 | REPORT.md auto-recovery | LOW | Masks recurring bug; gate (S2.1) handles |
| S3.14-17 | ACTIVE vs PROMPTS dir inconsistency | LOW | Latent footgun, no current impact |
| S4.* | Code smell | INFO | Refactor opportunities |
