## Axle — enabled

Use Axle as the default for supported proof-development checks when its environment matches the
project's Lean/Mathlib version and the supplied code includes the needed context. Prefer it over
equivalent Lean LSP checks and direct local scratch compilation. Use Lean LSP for local goals,
editor state, project-only dependencies, or when no compatible Axle environment is available.

Use the tool's exposed MCP schema; do not guess argument names. Capabilities:

- `check` — check supplied Lean code and inspect messages; prefer over `lean_run_code` and
  equivalent `lean_diagnostic_messages` checks when the necessary source context is available.
- `verify_proof` — validate a proof against its statement; prefer over `lean_verify` where the
  requested check is equivalent. Unity's local contract/axiom checks remain authoritative.
- `highlight` — retrieve semantic highlighting.
- `extract_decls` — inspect declarations and dependencies in a file.
- `repair_proofs` and `simplify_theorems` — repair or simplify proofs.
- `disprove` — attempt to prove a negation.
- `merge`, `rename`, and `normalize` — manipulate Lean source.
- `theorem2lemma`, `theorem2sorry`, `have2lemma`, `have2sorry`, and `sorry2lemma` — declaration
  transformations for scratch work and diagnosis. Never finalize generated `sorry` placeholders.
- `list_environments` — inspect available external toolchains.
- `share_url` and `read_share_url` — exchange Axle artifacts.

Before the first external check, reuse a teammate's compatible-environment finding or inspect
`list_environments`. When schemas are not exposed, retrieve the needed tool documentation:

```sh
unity mcp axle list_environments '{}'
unity mcp axle read_docs '{"page":"check"}'
unity mcp axle read_docs '{"page":"verify_proof"}'
```

Use native MCP when exposed; otherwise use `unity mcp axle <tool> '<json-args>'` with those documented
arguments. Reuse environment/documentation findings; do not rediscover them for every check.

External success does not replace local validation. Check returned proof/import edits in the actual
local project with targeted diagnostics, then use `finalize_formalization`; do not run a worker
project-wide build or duplicate a successful unchanged check. Unity's main verification remains
authoritative. Apply only relevant source edits and preserve protected statements and definitions.

Respect phase boundaries: chunking creates scaffolds, not completed proofs;
critics remain read-only and reuse current verification records unless
there is a concrete reason for another check.
