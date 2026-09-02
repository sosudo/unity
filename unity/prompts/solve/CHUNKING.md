You are decomposing one accepted informal solution into a semantic formalization DAG.

Read `solve_status()` first. Work only from the exact accepted `.unity/source/PROOF.tex` identified
by its solving-gate revision and SHA-256. If the file hash differs, report the mismatch and stop.

Write `.unity/dag.json` with:

```json
{
  "solution_gate_revision": 1,
  "solution_sha256": "...",
  "chunks": [
    {
      "id": "lemma-example",
      "title": "...",
      "summary": "...",
      "source_refs": ["section/equation identifiers"],
      "source_sha256": "hash of the source material represented by this chunk",
      "dependencies": [],
      "status": "pending",
      "statement": "intended Lean-level statement",
      "type": "theorem"
    }
  ]
}
```

Each definition, lemma, proposition, theorem, or corollary should be a separate task when it can be
implemented independently. Dependencies must reflect the actual mathematical dependency graph.
Summaries and statements must preserve all hypotheses and conclusions from the accepted solution.

Preserve an existing chunk ID and `source_sha256` when a later source revision leaves that chunk's
mathematical content unchanged. Changed content receives a changed hash and is reopened. Do not
write Lean during this task and do not invent content absent from the accepted solution.
