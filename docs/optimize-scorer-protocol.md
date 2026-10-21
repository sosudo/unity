# Optimize scorer protocol

Optimize builds and scores an exact source snapshot during verification. The
controller supplies its source identity and seals the returned records. Worker
score claims are not verification results.

## Prose metrics

Without an `Evaluator` section, the metric remains prose. The existing roster
assesses the frozen method against the requested source. Its result is labeled
`agent_judged`, including numeric values. The built-in metrics remain prose;
this implementation does not replace tactic invocations with lines or tokens.

## Explicit executable evaluator

A custom metric can explicitly opt in with these Markdown sections:

```markdown
## Metric function
measure.py

## Evaluator
{"protocol":"unity-optimize-score-v1","helper":"measure.py","interpreter":"python"}
```

The helper must be inside `.unity/metrics`, appear in the metric's `Metric
function` or `Score function` section, and match its frozen hash. `interpreter`
is either `python` (Unity's Python interpreter) or `executable` (an executable
helper with its own interpreter declaration). The controller does not invoke a
shell or infer execution from a filename alone. Optional `timeout_seconds` must
be positive and finite. There is no added default time budget.

The owned job receives `--request-file PATH`. The path identifies an immutable
JSON artifact containing:

- `protocol`, `root`, and the requested `subjects`;
- the frozen `metric`, `source_identity`, and controller `build_result`;
- `original_methods`, which must be retained exactly on continuation;
- `comparison_records`, when this score will be compared with before-scores.

The helper writes exactly one JSON object to stdout:

```json
{
  "protocol": "unity-optimize-score-v1",
  "measurements": {
    "subject-id": {
      "raw_value": 3,
      "method": {"description": "The configured method", "direction": "lower"},
      "units": "tactic invocations",
      "evidence": {"reason": "The retained tool result and explanation"}
    }
  },
  "judgments": {}
}
```

There must be one record per requested subject, and no extra subjects. An
original method must be returned unchanged. Qualitative comparisons require a
judgment with `improved`, optional `regressed`, and an evidence-based `reason`.
Undefined modularity requires `evidence.zero_denominator: true`.

## Score input types

Fresh score inputs use the frozen metric definition:

- Built-in `length` and `completion` accept nonnegative whole numbers or numeric
  text. For example, `18`, `18.0`, `"18"`, and `"18.0"` become integer `18`.
  Fractional counts are rejected. They are never rounded or truncated.
- Built-in `modularity` accepts finite numbers or numeric text in `[0, 1]`.
  Fractional ratios remain floats. The existing `"undefined"` evidence rule
  still applies.
- Custom metrics retain their numeric or qualitative input type. For example,
  `0.75` remains a float and `"0.75"` remains text. A metric name or comparison
  direction does not declare an output type.

Booleans, non-finite numbers, empty text, and containers are not scores.
Numeric text is parsed only for the built-in numeric metrics. Count text is
checked exactly before integer conversion. Ratio bounds are checked before
float conversion; a nonzero ratio that would become zero is rejected.
Normal finite float precision applies to accepted ratio text.

Parsing applies only to new input. Sealed records, baseline hashes, source
bindings, methods, and provenance rules remain unchanged. Verifier response
artifacts retain the original evaluator output before parsing.

## Execution evidence

Combined stdout and stderr are limited to 2 MiB. The owned job is cancelled if
that limit is exceeded. Cancellation and an explicitly configured deadline use
the existing checked-job shutdown path. Successful exact output is retained as
an artifact. Nonzero exit, malformed output, missing records, changed source,
or changed methods make verification fail; they never mean `no_improvement`.

Executable output is labeled `measured`. This establishes actual helper
execution, not independent mathematical correctness of the metric. Primary
review still assesses the metric and semantic preservation. No executable
failure falls back to a model assessment.

An old metric without this section retains its exact frozen identity. An old
baseline is never replaced or relabeled when the implementation changes.
