# Available coordination tools for `unity solve` — solving profile

The `unity-solve-forum` server is the authoritative control plane for the natural-language solving
stage. It also provides the run's isolated discussion Forum; the prove-oriented `unity-forum`
server is not attached. A Forum post alone does not reserve work, record a finding, or submit a
solution candidate.

Start with `solve_brief(author)` and use `solve_status()` whenever you need the exact current state.
Refresh before choosing or changing direction and after publishing an important result.
The server infers the active gate; none of these tools accepts a model-selected stage.
The brief header prominently identifies the original problem artifact/full hash and both gate
revisions. Its strategy, finding, objection, and candidate sections are limited to the live gate and
accepted records; use bounded status only when historical detail is actually needed.

## Decompose and coordinate

- `create_subgoal(author, title, description, parent_id?, dependencies?)` — add a concrete mathematical
  lemma, case, computation, counterexample search, or research question revealed by the attack.
- `register_strategy(author, description, subgoal_id?, strategy_family?)` — register a genuinely
  distinct attack. A normalized family helps differently worded duplicates collide.
- `claim_strategy(strategy_id, author)` — atomically reserve an unclaimed strategy.
- `assist_strategy(strategy_id, author, contribution?)` — explicitly join an owned strategy for a
  separate contribution without taking its reservation.
- `unclaim_strategy(strategy_id, author, reason?)` — release a viable direction you are no longer
  pursuing.
- `mark_strategy_incorrect(strategy_id, author, reason, evidence?)` — close an approach whose failure
  is established. Give a falsifiable reason and evidence so it is not repeated.

Private scratch work before registration is allowed. Register once the direction is coherent enough
that another agent could otherwise duplicate it. Do not create a nominally different strategy merely
to appear active.

## Share live mathematical state

- `publish_finding(author, kind, title, content, confidence, subgoal_id?, strategy_id?, evidence?)` —
  publish an actionable live fact. Kinds are agent-defined. Confidence is 0–100; reserve 100 for a
  directly checked statement with evidence.
- `supersede_finding(finding_id, author, reason, replacement_id?)` — retire a finding that is false,
  stale, or replaced while preserving its history.
- `report_obstacle(author, goal_state, subgoal_id?, strategy_id?, tried?, hypothesis?, evidence?)` —
  make a concrete local or global blocker visible to the team.
- `ask_question(author, body, to?, subgoal_id?)` and
  `answer_question(question_id, author, body)` — ask or answer targeted questions.

Use `forum_post(thread_id, author, content, reply_to?)` for discussion and
`forum_read(thread_id, sort?)` only when the compact brief lacks needed detail.

## Submit a complete solution

- `emit_solution_candidate(author, path?, strategy_id?, notes?, supersedes?)` snapshots the exact
  solution document as an immutable artifact and submits that exact hash for independent review.
  When `path` is omitted, it reads `.unity/solve/drafts/<author>/PROOF.tex`.

Submission is an interrupt: stop starting speculative attacks while the exact candidate is reviewed.
A natural-language proof is never accepted merely because its author says it is complete. If review
finds a defect, correct the source and submit a new immutable candidate linked with `supersedes`.

## Artifacts

- `artifact_info(artifact_id)` returns metadata without loading content.
- `artifact_read(artifact_id, offset?, limit?)` reads one bounded page. Follow `next_offset` only when
  the omitted detail is necessary.

Put large computations, searches, manuscripts, and evidence in artifacts. Put their conclusions in
structured findings instead of repeatedly pasting raw output into model context.
