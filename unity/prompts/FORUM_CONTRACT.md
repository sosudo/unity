# Forum contract (typed workspace)

Coordination goes through **typed acts**, each with a guaranteed reader. Free-form `forum_post` is
for what no act covers — never for claims, results, or blockers.

- **Claim before you work**: `forum_claim(chunk, strategy)`. Check the brief first; agents sharing a
  chunk must use different strategies.
- **Report the moment a chunk builds (or fails terminally)**: `forum_result(chunk, status, build_ok,
  decl_names, error_sig)`. It auto-closes your claim.
- **Review teammates — this is part of the job, not a courtesy**: after each of your results, pick at
  least one open teammate result you didn't author, read its diff, and `forum_endorse(post_id)` or
  `forum_object(post_id, reason)`. **The merge gate is real**: a result merges only with ≥1
  endorsement and 0 open objections. Before merging, the primary calls `forum_consensus(chunk)` and
  merges only `mergeable` results; overriding the gate requires a `forum_decision`.
- **Blocked?** `forum_obstacle(chunk, goal_state, tried, hypothesis)` — it lands in every teammate's
  brief, and a later `forum_result(build_ok=true)` on the chunk auto-resolves it.
- **Obstacles are requests for help, not FYIs**: when your brief shows a teammate's open obstacle
  and you have a relevant idea, result, or counterexample, reply to it (`forum_post` with
  `reply_to`, or `forum_answer` if it was asked as a question) — a two-line reply that unblocks a
  teammate outranks an hour of solo work.
- **Ask and answer**: `forum_question(content, to?, chunk?)` for anything a teammate may know.
  Questions addressed to you appear in your brief — **answer them (`forum_answer`) before claiming
  new chunks**.
- **Distill verified knowledge**: `ledger_add(kind=lemma|tactic|failure, title, content, evidence)` —
  evidence (build output / error text / compiling snippet) is required. `ledger_get(query)` before
  fighting a goal a teammate may already have cracked.
- **Refresh**: your preamble carries a workspace-brief snapshot from dispatch time; call `forum_brief`
  for a live one whenever you finish a chunk or return from a long build.
- Binding choices are recorded with `forum_decision(topic, choice, rationale)`; close your phase
  participation with a `forum_handoff(phase, changed, open, commitments)`.
