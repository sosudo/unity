# length

## Prompt
Rewrite the Lean proof to be as short as possible while keeping it correct — reduce the number
of tactic invocations (proof steps). Prefer the shorter proof whenever both compile. Do not
change the statement being proved.

## Examples
(none)

## Score function
Improvement = percentage reduction in length: (length(original) − length(new)) / length(original) × 100.
Counts only when the rewrite compiles correctly; otherwise 0. Higher is better.

## Metric function
length = the number of tactic invocations in the tactic proof. Lower is better.
