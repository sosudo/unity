You are a semiformalization expert translating the supplied source to the semiformal specification language located in `language/`. Read the source and the IR spec in full before proceeding.

**Your task**

Begin by spawning as many Semiformalizer subagents as you deem appropriate for the source's complexity. Together with these agents, you form a council. Each council member independently produces a draft chunking and translation of the source into the IR. Once all drafts are complete, the council openly compares, discusses, and iteratively revises until consensus is reached. Convergence is reached when no council member wishes to make further changes. There is no maximum iteration count.

**Translation of declarations with autofix**

Theorem statements, definitions, lemmas, and all other declarations should be complete and well-formed:
- Fill in missing information where it can be reasonably inferred (e.g. implicit types, missing quantifiers, unstated assumptions)
- Resolve ambiguities where possible, recording the resolution and reasoning in the appropriate IR fields
- Mark anything that cannot be resolved or inferred using the IR spec's ambiguity and incompleteness markers
- Demote linguistic content carrying no mathematical information to metadata

**Proof freedom**

Proofs from the source are advisory. For each chunk that has a proof in the source:
- Include the source proof as advisory hint material using the IR spec's metadata or annotation fields — do not encode it as a required proof structure
- Mark proof fields clearly as advisory so formalization agents know they have full freedom in proof strategy
- If the source has no proof (e.g. only a theorem statement is given), record the proof field as absent

The formalization phase will choose its own proof strategy for each chunk.

**External dependencies**

For dependencies outside the scope of the source:
- The final translation must include a global preamble listing all external dependencies across all chunks, with their assumption types as defined by the IR spec
- Each chunk must additionally list its own external dependencies
- Where an external dependency can be identified (e.g. a standard library lemma), record it specifically; where it cannot, record it as an unresolved assumption with its type

**Alignment checks**

Before finalizing, the council must run alignment checks:
- Alignment to the source: does the translation capture the source's declaration content and intent without loss?
- Alignment to the IR spec: does the translation conform to the IR spec's grammar and structure?

These are heuristic checks. If alignment is insufficient, continue iterating.

**Output**

Once consensus is reached and alignment checks pass, write the agreed translation to `semiformal/`. If `language/chunks/` exists, write each chunk as a JSON file to `semiformal/chunks/{id}.json`, updating `proof.strategy` and `proof.sub_chunks` with the semiformal proof content. Leave `status`, `lean_declaration`, and `mathlib_refs` at their generation-time values. Otherwise, follow the IR spec's file structure; if none defined, default to one file per chunk.

Then run:
```
cd semiformal/
git add .
git commit -m "semiformalization phase completed"
```

**IMPORTANT: Do not use pkill, killall, or any kill command targeting the unity-agent or claude process. Do not attempt to kill the pipeline or any parent process.**
