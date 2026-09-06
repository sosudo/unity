## Axle — enabled

Prefer Axle over an equivalent Lean LSP tool when its environment supports
the project's Lean/Mathlib version. Use Lean LSP for local editor state or
when Axle has no suitable equivalent.

Useful tools:

- `check`, `verify_proof`: check Lean code/proofs.
- `repair_proofs`: repair failing proofs.
- `extract_decls`: inspect declarations and dependencies.
- `disprove`: investigate a potentially false statement.

Discover compatible environments and retrieve a tool's documentation when
needed; reuse relevant findings rather than repeatedly fetching them:

```sh
unity mcp axle list_environments '{}'
unity mcp axle read_docs '{"page":"verify_proof"}'
```

Use native MCP when exposed; otherwise:
`unity mcp axle <tool> '<json-args>'`.

External success does not replace local validation. Check returned code
under the project's actual toolchain, then use `finalize_formalization`;
Unity's main verification remains authoritative.

Respect phase boundaries: chunking creates scaffolds, not completed proofs;
critics remain read-only and reuse current verification records unless
there is a concrete reason for another check.
