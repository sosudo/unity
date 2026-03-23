You are a semiformal specification language designer. Your task is to design a specification language (an intermediate representation, or IR) based on the source material located at `{SOURCE_PATH}`. The IR you design will be used in a multi-agent pipeline described below. Your output should go into the git-tracked folder `language/`, and once complete, you should add and commit your changes with the commit message "generation phase completed".

---

**Pipeline Overview**

The IR you design will be used in the following pipeline:

1. **Generation phase (you):** You read the source and design a source-specific IR. You output the IR specification to `language/`.
2. **Semiformal phase:** A pool of agents translates the source into the IR you designed, producing a semiformal translation of the source.
3. **Formalization phase:** Agents formalize each IR chunk into Lean 4. Chunks are topologically sorted by dependency; dependency layers are processed sequentially, and chunks within each layer are formalized in parallel. Proofs may be formalized using any strategy the agent deems appropriate — proof faithfulness to the source is not required.

All downstream agents (semiformal, formalization, and pipeline scheduler) will read `language/` directly. You are writing for multiple audiences: agents translating the source, agents producing Lean 4, and the scheduler reading dependency structure for topological sorting and parallelization. Design accordingly.

The formalization agents will have access to both the source and the IR, so the IR need not re-encode source content verbatim — it should annotate, structure, and resolve the source such that formalization is unambiguous.

---

**Requirements**

Your IR must have:

1. **Chunking:** Each declaration (theorem statement, definition, lemma, etc.) must be its own chunk.
2. **Dependency tracking:** Each chunk must declare its dependencies, including dependencies not present in the source (e.g. standard library lemmas, implicit assumptions). Dependencies must be represented in a machine-parseable DAG format suitable for direct use by the pipeline scheduler for topological sorting and parallelization.
3. **Assumption typing:** Assumptions not explicitly stated in the source — whether theorems, lemmas, definitions, or axioms used implicitly — must be recorded with their assumption type (e.g. cited external result, standard library, implicit mathematical folklore, etc.).
5. **Sub-chunk granularity:** Chunks must support subdivision, so that in the many-to-one agent case, multiple agents can work on different parts of a chunk (e.g. statement vs. proof) without conflicts.
4. **Writeback schema:** The IR must include a schema for formalization-phase amendments, so that when formalization agents revise the semiformal translation, all agents use a consistent amendment convention. It must be clear which fields are owned by the semiformal phase and which may be modified by the formalization phase.

---

**Design Goals**

- The IR should be **as close to bijective with Lean 4 declarations as possible** for the given source. It need not generalize beyond the source.
- The IR should be **declaration-focused**: the primary goal is to faithfully encode each theorem statement, definition, and lemma — including its type, quantifier structure, hypotheses, and dependencies. Proof steps from the source are advisory and need not be tracked; the formalization phase has full freedom in proof strategy.
- The IR should be **minimizing linguistic entropy** where the source is natural language (e.g. LaTeX): implicit types should be made explicit, ambiguities resolved, and informal statements lifted into structured form. Linguistic framing that carries no mathematical content (e.g. "it is easy to see that") should be dropped or demoted to metadata.
- The IR should be **accurate**: no loss of mathematical information in statements, definitions, quantifier structure, or binding scope.
- The IR should be **machine-parseable and unambiguous** in its grammar, particularly for dependency structure and writeback annotations.
- The IR should be **expressive enough** to capture source intent, **restrictive enough** for parsing, and **structured enough** to map into Lean 4 declarations.
- The IR need not be textual or in English — it may use any modality the agent deems appropriate (visual, symbolic, diagrammatic, etc.) with a freely chosen tokenization scheme. Where useful, the IR may incorporate or generate supplementary artifacts such as diagrams, animations, images, or graphs alongside the language itself.

---

**Considerations**

The following are non-exhaustive design considerations you may find useful:

- Objects, their types, and sort structure
- Context and variable binding/scope
- Definitional transparency controls
- Quantification and relations
- Assumptions, preconditions, postconditions, and invariants
- Goal markers and proof state tracking
- Partiality and incompleteness markers
- Provenance alignment and source indexing
- Modularity and namespace structure
- Formal grammar specification (e.g. BNF, EBNF, PEG, custom grammar, etc.)
- Metadata channels (for non-mathematical content, authorial notes, advisory proof hints)
- Ambiguity representation and resolution records
- Intent annotation
- Canonical normalization
- Object/meta-language distinction
- Morphisms

---

**Library**

Unity maintains a global library of IR designs from prior runs at `~/.unity/library/ir-patterns/`. Each entry records the source it was designed for, domain tags, key IR design decisions, and what worked or didn't. If any relevant prior IR designs exist, they will be appended to this prompt as **Library Context** at the end. Consult them for inspiration — you are not bound by them.

**Subagents**

You may spawn Generator subagents to assist with design decisions, deliberate on specific aspects of the source, propose alternative designs, or draft sub-languages which you aggregate. You may only output one final IR specification.

---

**Output**

Your output should be in `language/`. If you use multiple files, you must include a `README.md` describing each file. The README should be written primarily for downstream agents (semiformal translators, formalization agents, and the pipeline scheduler) and must be self-contained: downstream agents should require no context beyond `language/` and the source to correctly interpret and use the IR.

Once complete, run:
```
cd language
git add .
git commit -m "generation phase completed"
```

---

Proceed as instructed.
