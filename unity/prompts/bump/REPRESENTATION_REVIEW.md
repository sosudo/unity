Check the adopted migration correspondence against its exact immutable original Lean declaration. This is a
targeted task within bump, not a pipeline phase or a final proof review. Inspect the bound
review artifact: source anchors, statements, definitions/meaning dependencies and accepted revision.
Read any bound repair proposal, but never let it replace the frozen original type, definition behavior,
selected coverage or per-declaration trust. An equivalent rename/refinement must retain those obligations.
Your private worktree may be older or contain unrelated work; do not edit it or review its files as
though they were the supplied snapshot. Read exact source with `git show <bound-main-sha>:<file>`
when needed. Never use a later file edit as evidence about an earlier review input.

Check quantifiers, domains, hypotheses, conclusions, binder/API argument order and representation
definitions. For unfamiliar APIs, inspect their actual declarations before assuming what they mean.
If the artifact contains a source diagnosis, read its evidence before repeating the earlier alarm.
A false-alarm diagnosis calls for a fresh representation judgment, not automatic approval.
Provisional compiler repair is not complete native verification. No new proof holes or trust expansion
are allowed in Bump representation candidates; distinguish inherited original trust from new holes.
Do not prove the theorem, launch a full project build or redo already checked kernel verification.

Submit `aligned` only when the encoding expresses the cited claim. Use `encoding_error` for a wrong
Lean representation, `source_issue` only for a defect in the original mathematics, and `uncertain`
when unable to decide. Cite concrete original and target Lean evidence. A version-dependent API change
is not itself a source defect. For a proposed counterexample, verify all
original hypotheses and actual Lean API conventions before claiming a source error.

Your judgment gates use of this interface by dependent work; it does not accept any final proof.
Unchanged meanings reuse the review. Finish after submitting one current structured judgment.
