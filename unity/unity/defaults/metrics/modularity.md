# modularity

## Prompt
Rewrite the Lean proof to be more modular, readable, and declarative — decompose it into
explicitly-typed `have` steps that name reusable, independent subproofs, while keeping it correct.
Do not change the statement being proved.

## Examples
(none)

## Score function
Improvement = modularity(new) − modularity(original) (absolute increase in the declarativity ratio).
Counts only when the rewrite compiles correctly; otherwise 0. Higher is better.

## Metric function
modularity (declarativity) = ratio of explicitly-typed `have` tactics to total tactic invocations.
Higher is better.
