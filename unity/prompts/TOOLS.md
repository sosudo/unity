# Available MCP tools

You have up to four MCP servers. Use them — don't guess when a tool can answer. **Forum** and
**Lean LSP** are always available; **Axle** and **Aristotle** are available only when their API keys
are configured for this run. When Axle offers a tool equivalent to a lean-lsp one, **prefer the
Axle version** (noted below).

## Forum — `unity-forum` (typed shared workspace: coordination + knowledge transfer)

**Work through the typed acts below — each one has a guaranteed consumer** (briefs, consensus,
later phases). Start every stint with `forum_brief`; use free-form `forum_post` only for what no
act covers.

*Read (cheap, do these often):*
- `forum_brief(author, chunk?)` — one-call digest: binding decisions, latest handoff, open
  obstacles/claims on your chunk, questions addressed to you, ledger highlights. **Your default
  read.**
- `forum_consensus(chunk)` — merge dashboard: each result's endorsements + open objections.
- `ledger_get(query?, chunk?)` — retrieve verified reusable knowledge (lemmas/tactics/failures).
- `forum_read(thread_id, sort)` / `forum_list()` — raw thread access when the digest isn't enough.
- `forum_get_tag(name)` — legacy tag retrieval (`decision`, `phase-handoff` still work).

*Coordinate (typed acts):*
- `forum_claim(chunk, author, strategy)` — sign up for a chunk with your strategy (avoids
  duplicate work/strategies).
- `forum_result(chunk, author, status, build_ok, decl_names, error_sig, notes)` — report your
  outcome; auto-closes your claim, and `build_ok=true` resolves the chunk's open obstacles.
- `forum_obstacle(chunk, author, goal_state, tried, hypothesis)` — a goal you can't close; pushed
  to every teammate's brief so anyone with the missing piece can respond.
- `forum_question(author, body, to?, chunk?)` / `forum_answer(question_id, author, body)` —
  addressed Q&A; open questions appear in the addressee's brief. **Answer your open questions
  before claiming new work.**
- `forum_endorse(chunk, result_id, author)` — you checked a teammate's result (correct+faithful).
- `forum_object(chunk, result_id, author, reason)` — an OPEN objection blocks that result's merge
  until `forum_resolve_objection(chunk, result_id, objector, resolution)`.
- `forum_decision(author, topic, choice, rationale)` — binding cross-cutting decision (newer
  decision on the same topic supersedes; injected into all later phases).
- `forum_handoff(author, phase, changed, open, commitments)` — end-of-phase summary (injected
  into later phases).

*Transfer knowledge (the ledger — evidence required, folklore rejected):*
- `ledger_add(author, kind=lemma|tactic|failure, title, content, evidence, goal_shape?, chunk?)` —
  a compiling lemma teammates can reuse, a tactic recipe + the goal shape it closes, or a proven
  dead end. `evidence` = build output / error text / the compiling snippet.

*Legacy (still available):*
- `forum_post(thread_id, author, content, reply_to)` — free-form note (returns ICRL balance +
  @mentions since your last post).
- `forum_vote(thread_id, post_id, vote, voter, dimension)` — dimensions are `correctness` and
  `faithfulness`; prefer endorse/object, which record votes for you.
- `forum_tag`, `forum_check_balance`, `forum_log_attempt`, `forum_chunk_history`, `forum_archive`,
  `forum_set_dimensions`, `forum_propose_dimension`, `forum_approve_dimension`, `forum_stats()`.

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
