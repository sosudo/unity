# completion

## Prompt
Rewrite and complete the Lean code so that it is fully correct — eliminate every error, including
any remaining `sorry` or `axiom`. A complete proof has zero errors. Do not change the statement
being proved.

## Examples
(none)

## Score function
Improvement = reduction in the number of errors; a fully complete proof reaches zero. Lower is better.

## Metric function
completion = the number of errors present in the code (compiler errors, plus remaining `sorry` /
`axiom`). Lower is better; zero means complete.
