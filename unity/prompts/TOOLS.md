# Available MCP tools

You have up to four MCP servers. Use them — don't guess when a tool can answer. **Forum** and
**Lean LSP** are always available; **Axle** and **Aristotle** are available only when their API keys
are configured for this run. When Axle offers a tool equivalent to a lean-lsp one, **prefer the
Axle version** (noted below).

## Forum — `unity-forum` (team coordination + shared memory)
- `forum_create_thread(thread_id, title, description)` — create a thread.
- `forum_post(thread_id, author, content, reply_to)` — post a message; the result also returns your ICRL balance and any @mentions since your last post.
- `forum_read(thread_id, sort)` — read a thread (`sort`: hot/new/top).
- `forum_list()` — list all threads, active vote dimensions, tags, and the ICRL leaderboard.
- `forum_vote(thread_id, post_id, vote, voter, dimension)` — up/down-vote a post on a quality dimension.
- `forum_tag(name, post_ids, description, tagger)` — attach a named concept tag linking posts across threads.
- `forum_get_tag(name)` — retrieve all posts with a tag (e.g. `decision`, `phase-handoff`).
- `forum_check_balance(author, drain)` — your ICRL balance, history, and pending @mention notifications.
- `forum_log_attempt(chunk_id, author, what, outcome, error, notes)` — log a structured attempt at a chunk.
- `forum_chunk_history(chunk_id, limit)` — recent structured attempts on a chunk, newest first.
- `forum_archive(thread_id, post_id, reason, archiver)` — archive a stale/superseded post.
- `forum_set_dimensions(dimensions, allow_orphan)` — set the run's canonical vote dimensions (once, at start).
- `forum_propose_dimension(name, description, proposed_by)` — propose a new vote dimension.
- `forum_approve_dimension(name)` — approve a proposed vote dimension.

## Lean LSP — `lean-lsp` (inspect and drive the Lean project)
- `lean_goal` ★ — proof goals at a position. The most important tool; use often.
- `lean_term_goal` — the expected type at a position.
- `lean_diagnostic_messages` — compiler errors/warnings/infos for a file.
- `lean_build` — build the project + restart the LSP (only when needed, e.g. after new imports).
- `lean_file_outline` — imports and declarations with type signatures.
- `lean_hover_info` — type signature and docs for a symbol.
- `lean_completions` — IDE autocompletions on incomplete code (after `.` or a partial name).
- `lean_declaration_file` — the source of a symbol's declaration.
- `lean_references` — all references to a symbol.
- `lean_local_search` — verify a declaration exists locally / in the Mathlib cache (use before relying on a lemma name).
- `lean_leansearch` — Mathlib natural-language search.
- `lean_loogle` — Mathlib search by type signature.
- `lean_leanfinder` — Mathlib semantic search by mathematical meaning.
- `lean_state_search` — find lemmas to close the goal at a position.
- `lean_hammer_premise` — premise suggestions for automation tactics.
- `lean_code_actions` — resolved TryThis edits (`simp?`, `exact?`, `apply?`).
- `lean_multi_attempt` — try multiple tactics at a position without editing the file.
- `lean_run_code` — run a self-contained snippet (must include imports); returns diagnostics.
- `lean_verify` — check a theorem's axioms + optional source scan.
- `lean_minimal_hypotheses` — find which of a theorem's hypotheses are unnecessary.
- `lean_profile_proof` — per-line timing for a theorem (slow; avoid on heartbeat-limited proofs).
- `lean_get_widgets` / `lean_get_widget_source` — proof-visualization widget data / JS.

## Axle — `axle` (Lean verification + code manipulation; prefer over the lean-lsp equivalent when both offer it)
- `verify_proof` — validate a Lean proof against a formal statement. **Prefer over `lean_verify`.**
- `check` — evaluate Lean code and report all messages. **Prefer over `lean_diagnostic_messages` / `lean_run_code`.**
- `highlight` — semantic highlighting tokens for Lean code.
- `extract_decls` — split a file into separate declarations with their dependencies (useful for chunking; `extract_theorems` is the deprecated older version).
- `repair_proofs` — repair broken theorem proofs.
- `simplify_theorems` — simplify theorem proofs.
- `disprove` — attempt to disprove theorems by proving the negation.
- `merge` — combine multiple Lean files into a single file.
- `rename` — rename declarations in Lean code.
- `normalize` — standardize Lean file formatting.
- `theorem2lemma` — convert between `theorem` and `lemma` keywords.
- `theorem2sorry` — replace theorem proofs with `sorry`.
- `have2lemma` — extract `have` statements to standalone lemmas.
- `have2sorry` — replace `have` statements with `sorry`.
- `sorry2lemma` — extract sorries and errors to standalone lemmas.
- `list_environments` — list available Lean environments (toolchains) on the Axle server.
- `share_url` / `read_share_url` — create / read a shareable URL for an Axle verification.

## Aristotle — `aristotle` (offload hard proving/formalization jobs to an external prover; asynchronous)
- `aristotle_submit(prompt, project_dir)` — submit a job; returns a `project_id`. Runs async — submit and keep working.
- `aristotle_status(project_id)` — poll the job: project status (RUNNING/IDLE) + its task statuses.
- `aristotle_wait(project_id, timeout_seconds, poll_seconds)` — bounded poll until the job finishes or times out.
- `aristotle_result(project_id, destination)` — download the finished job's result files.
- `aristotle_cancel(project_id)` — cancel a queued/in-progress job.
- `aristotle_list(limit)` — list recent Aristotle jobs.
