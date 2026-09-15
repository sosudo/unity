Check the newly adopted Lean representation against its exact supplied source claim. This is a
targeted task within autoformalize, not a pipeline phase or a final proof review. Inspect the bound
review artifact: source anchors, statements, definitions/meaning dependencies and accepted revision.
If the specification explicitly adopted a source repair, read its bound repair text too; review the
encoding against that explicit amended claim and preserve the distinction from the original source.
Your private worktree may be older or contain unrelated work; do not edit it or review its files as
though they were the supplied snapshot. Read exact source with `git show <bound-main-sha>:<file>`
when needed. Never use a later file edit as evidence about an earlier review input.

Check quantifiers, domains, hypotheses, conclusions, binder/API argument order and representation
definitions. For unfamiliar APIs, inspect their actual declarations before assuming what they mean.
If the artifact contains a source diagnosis, read its evidence before repeating the earlier alarm.
A false-alarm diagnosis calls for a fresh representation judgment, not automatic approval.
A missing proof is expected here, not evidence that an encoding or source statement is wrong.
Do not prove the theorem, launch a full project build or redo already checked kernel verification.

Submit `aligned` only when the encoding expresses the cited claim. Use `encoding_error` for a wrong
Lean representation, `source_issue` only for a defect in the original mathematics, and `uncertain`
when unable to decide. Cite concrete source and Lean evidence. A source attribution with no supplied
proof permits proof development; it is not itself a defect. For a proposed counterexample, verify all
original hypotheses and actual Lean API conventions before claiming a source error.

Your judgment gates use of this interface by dependent work; it does not accept any final proof.
Unchanged meanings reuse the review. Finish after submitting one current structured judgment.
