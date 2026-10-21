"""Frozen Optimize metric definitions and source-bound measurements.

Metric Markdown is a specification. Only an explicit Evaluator section opts
into helper execution; we never substitute line counts or a lexical tactic
counter for proof length.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _sealed(value: dict) -> dict:
    value["sha256"] = digest(value)
    return value


def _valid(value: dict) -> bool:
    return bool(isinstance(value, dict) and value and value.get("sha256") == digest({k: v for k, v in value.items() if k != "sha256"}))


def load_metric(root: Path, metric_name: str) -> dict:
    """Read existing metric Markdown and freeze referenced local helper bytes.

    The existing metric command accepts a filename in the Score/Metric function
    sections. Plain filenames, quoted filenames and Markdown links are accepted;
    prose definitions remain prose. Helpers are never executed by this loader.
    """
    root = Path(root).resolve()
    name = Path(metric_name).name.lower().removesuffix(".md")
    if not name or name != metric_name.lower().removesuffix(".md"):
        raise ValueError("metric name must name a project metric, not a path")
    directory = root / ".unity" / "metrics"
    path = directory / (name + ".md")
    if not path.is_file():
        raise ValueError(f"metric '{name}' not found in .unity/metrics")
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("metric definition escapes .unity/metrics")
    definition = path.read_text()
    sections = {}
    for match in re.finditer(r"(?m)^##\s+(.+)\s*$", definition):
        end = re.search(r"(?m)^##\s+", definition[match.end():])
        sections[match.group(1).strip().lower()] = definition[match.end():match.end() + end.start() if end else len(definition)].strip()
    helpers = {}
    for section in ("score function", "metric function"):
        value = sections.get(section, "").strip()
        link = re.fullmatch(r"\[[^\]]*\]\(([^)]+)\)", value)
        filename = link.group(1) if link else value.strip("`\"'")
        if filename in {"", "(none)", "none"}:
            continue
        # A prose formula is not interpreted as a filename or shell command.
        if not re.fullmatch(r"[\w./ -]+\.[A-Za-z0-9]+", filename):
            continue
        rel = Path(filename)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("metric helper must stay inside .unity/metrics")
        helper = directory / rel
        if not helper.is_file() or not helper.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"metric helper missing or escapes project metrics: {filename}")
        payload = helper.read_bytes()
        helpers[filename] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "section": section}
    default = Path(__file__).parent / "defaults" / "metrics" / (name + ".md")
    builtin = name if default.is_file() and default.read_text() == definition else None
    result = {"schema_version": 1, "name": name, "definition": definition,
                    "definition_sha256": hashlib.sha256(definition.encode()).hexdigest(),
                    "helpers": helpers, "builtin": builtin,
                    "direction": {"length": "lower", "modularity": "higher", "completion": "lower"}.get(builtin),
                    "units": {"length": "tactic invocations", "modularity": "typed-have / tactics ratio",
                              "completion": "compiler errors + sorry/axiom"}.get(builtin),
                    "qualification": "Markdown specification; helper presence alone is not execution evidence."}
    # Opt-in only. In particular, retain the exact old metric hash when this
    # section is absent; stopped runs must not acquire a new scoring method.
    if "evaluator" in sections:
        try:
            evaluator = json.loads(sections["evaluator"])
        except (TypeError, ValueError) as exc:
            raise ValueError("metric Evaluator must be a JSON object") from exc
        if (not isinstance(evaluator, dict)
                or set(evaluator) - {"protocol", "helper", "interpreter", "timeout_seconds"}
                or evaluator.get("protocol") != "unity-optimize-score-v1"
                or not isinstance(evaluator.get("helper"), str)
                or evaluator.get("helper") not in helpers
                or not isinstance(evaluator.get("interpreter", "executable"), str)
                or evaluator.get("interpreter", "executable") not in {"python", "executable"}):
            raise ValueError("unsupported metric evaluator or unfrozen helper")
        timeout = evaluator.get("timeout_seconds")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("metric evaluator timeout_seconds must be positive and finite")
        result["evaluator"] = evaluator
    return _sealed(result)


def parse_score_value(metric: dict, raw_value):
    """Normalize a fresh score using only the frozen builtin's numeric contract.

    Never call this on sealed measurements. Custom metrics have no declared
    result type, so preserve their numeric and qualitative inputs as supplied.
    The existing measurement validator still checks evidence, units and binding.
    """
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
        raise ValueError("raw metric must be a finite number or qualitative value")
    if isinstance(raw_value, str):
        if not raw_value.strip():
            raise ValueError("qualitative metric value cannot be empty")
    else:
        try:
            finite = math.isfinite(raw_value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("metric value must be finite")
    builtin = metric.get("builtin")
    if builtin not in {"length", "completion", "modularity"}:
        return raw_value
    if builtin == "modularity" and raw_value == "undefined":
        return raw_value  # record_measurement requires zero-denominator evidence.
    message = ("length/completion raw values are nonnegative integer counts"
               if builtin in {"length", "completion"}
               else "modularity raw value must be a ratio between zero and one")
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text):
            raise ValueError(message)
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(message) from exc
    else:
        number = Decimal(raw_value)
    if not number.is_finite():
        raise ValueError(message)
    if builtin in {"length", "completion"}:
        # Match the existing finite-number validator, without allocating an
        # enormous integer from hostile exponent text. Check exact integrality
        # before conversion: float("18.000000000000000001") would lose evidence.
        if number < 0 or number != number.to_integral_value():
            raise ValueError(message)
        # Use float only for the existing finite-range check, never to obtain
        # the integer value. This also retains accepted native integer bounds.
        if not math.isfinite(float(number)):
            raise ValueError(message)
        return int(number)
    # Decimal bounds must precede float conversion: out-of-range text must not
    # round into 0 or 1, and a positive ratio must not silently underflow to 0.
    if not 0 <= number <= 1:
        raise ValueError(message)
    if not isinstance(raw_value, str):
        return raw_value
    parsed = float(number)
    if parsed == 0 and number != 0:
        raise ValueError("modularity ratio is too small to represent without becoming zero")
    return parsed


def record_measurement(metric: dict, *, subject: str, raw_value, source_identity: dict,
                       method: dict, evidence, evidence_kind="agent_judged", units=None) -> dict:
    """Seal a raw measurement with explicit method, provenance and source.

    `measured` means the supplied evidence records a tool measurement; it is not
    inferred from numeric output. The caller must retain the actual tool output.
    """
    if not _valid(metric) or not _valid(source_identity):
        raise ValueError("measurement requires valid frozen metric and source identity")
    if not isinstance(subject, str) or not subject.strip() or not isinstance(method, dict) or not method.get("description"):
        raise ValueError("measurement requires subject and described method")
    if evidence_kind not in {"measured", "agent_judged"} or not evidence:
        raise ValueError("measurement requires explicit evidence and evidence kind")
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
        raise ValueError("raw metric must be a finite number or qualitative value")
    if isinstance(raw_value, str) and not raw_value.strip():
        raise ValueError("qualitative metric value cannot be empty")
    if isinstance(raw_value, (int, float)) and not math.isfinite(raw_value):
        raise ValueError("metric value must be finite")
    if metric.get("builtin") in {"length", "completion"} and (not isinstance(raw_value, int) or raw_value < 0):
        raise ValueError("length/completion raw values are nonnegative integer counts")
    undefined_ratio = (raw_value == "undefined" and isinstance(evidence, dict) and evidence.get("zero_denominator") is True)
    if metric.get("builtin") == "modularity" and not undefined_ratio and (not isinstance(raw_value, (int, float)) or not 0 <= raw_value <= 1):
        raise ValueError("modularity raw value must be a ratio between zero and one")
    if metric.get("builtin") and units is not None and units != metric.get("units"):
        raise ValueError("measurement units must match the builtin metric definition")
    direction = metric.get("direction") or method.get("direction")
    if isinstance(raw_value, (int, float)) and direction not in {"lower", "higher"}:
        raise ValueError("custom numeric metric needs an explicit direction in its method")
    if metric.get("direction") and method.get("direction", direction) != direction:
        raise ValueError("measurement method contradicts metric direction")
    return _sealed({"schema_version": 1, "subject": subject, "raw_value": raw_value,
                    "units": units or metric.get("units"), "direction": direction,
                    "metric_kind": metric.get("builtin"),
                    "metric_sha256": metric["sha256"], "source_sha256": source_identity["sha256"],
                    "method": deepcopy(method), "method_sha256": digest(method),
                    "evidence_kind": evidence_kind, "evidence": deepcopy(evidence)})


def validate_measurement_binding(measurement: dict, metric: dict, source_identity: dict, method=None) -> bool:
    try:
        bound = bool(_valid(metric) and _valid(source_identity) and _valid(measurement)
                    and measurement.get("metric_sha256") == metric["sha256"]
                    and measurement.get("source_sha256") == source_identity["sha256"]
                    and measurement.get("method_sha256") == digest(measurement.get("method"))
                    and measurement.get("evidence")
                    and measurement.get("evidence_kind") in {"measured", "agent_judged"}
                    and (method is None or measurement["method_sha256"] == digest(method)))
        if not bound:
            return False
        expected = record_measurement(metric, subject=measurement["subject"], raw_value=measurement["raw_value"],
                                      source_identity=source_identity, method=measurement["method"], evidence=measurement["evidence"],
                                      evidence_kind=measurement["evidence_kind"], units=measurement.get("units"))
        return all(measurement.get(key) == value for key, value in expected.items() if key != "sha256")
    except (TypeError, ValueError, KeyError):
        return False


def _intact(value) -> bool:
    try:
        return _valid(value)
    except (TypeError, ValueError):
        return False


def measurement_errors(measurement, metric: dict, source_identity: dict, *, subject=None) -> list[dict]:
    """Explain the existing sealed-record contract without changing evidence.

    These checks are pure and do not infer a score, provenance or improvement.
    Malformed payloads are distinct from records bound to different source bytes
    or Git identity. Callers supply the actual identity they intend to validate.
    """
    context = {"subject": subject} if subject is not None else {}
    if not isinstance(measurement, dict):
        return [{"code": "invalid_measurement_type", **context}]
    if not _intact(measurement):
        return [{"code": "invalid_measurement_digest", **context}]
    if not _intact(metric):
        return [{"code": "invalid_metric_digest", **context}]
    if not _intact(source_identity):
        return [{"code": "invalid_source_identity_digest", **context}]
    errors = []
    for code, field, expected in (
        ("measurement_subject_mismatch", "subject", subject),
        ("measurement_metric_mismatch", "metric_sha256", metric["sha256"]),
        ("measurement_source_mismatch", "source_sha256", source_identity["sha256"]),
    ):
        if (field != "subject" or subject is not None) and measurement.get(field) != expected:
            errors.append({"code": code, "fields": [field], **context})
    method = measurement.get("method")
    if not isinstance(method, dict) or not method.get("description"):
        errors.append({"code": "invalid_measurement_method", "fields": ["method"], **context})
    elif measurement.get("method_sha256") != digest(method):
        errors.append({"code": "invalid_measurement_method_digest", "fields": ["method_sha256"], **context})
    if errors:
        return errors
    try:
        expected = record_measurement(metric, subject=measurement["subject"], raw_value=measurement["raw_value"],
            source_identity=source_identity, method=method, evidence=measurement["evidence"],
            evidence_kind=measurement["evidence_kind"], units=measurement.get("units"))
    except (TypeError, ValueError, KeyError) as exc:
        return [{"code": "invalid_measurement_record", "reason": str(exc), **context}]
    fields = [key for key, value in expected.items() if key != "sha256" and measurement.get(key) != value]
    return [{"code": "invalid_measurement_record", "fields": fields, **context}] if fields else []


def comparison_errors(before, after, *, subject=None) -> list[dict]:
    """Check existing comparison metadata, not values or qualitative judgments."""
    context = {"subject": subject} if subject is not None else {}
    errors = []
    for side, measurement in (("before", before), ("after", after)):
        if not isinstance(measurement, dict):
            errors.append({"code": "invalid_measurement_type", "side": side, **context})
        elif not _intact(measurement):
            errors.append({"code": "invalid_measurement_digest", "side": side, **context})
    if errors:
        return errors
    fields = [key for key in ("subject", "metric_sha256", "method_sha256", "units", "direction", "metric_kind")
              if before.get(key) != after.get(key)]
    if subject is not None and (before.get("subject") != subject or after.get("subject") != subject) and "subject" not in fields:
        fields.insert(0, "subject")
    return [{"code": "measurement_comparison_mismatch", "fields": fields, **context}] if fields else []


def rebind_measurement(measurement: dict, metric: dict, old_identity: dict, new_identity: dict) -> dict:
    """Carry evidence across a commit-only change, never across changed bytes."""
    if not validate_measurement_binding(measurement, metric, old_identity) or not _valid(new_identity):
        raise ValueError("cannot rebind invalid measurement/source evidence")
    if any(old_identity.get(key) != new_identity.get(key) for key in ("files", "config", "scope", "lake_args")):
        raise ValueError("measurement rebinding requires identical source/configuration/scope bytes")
    if old_identity == new_identity:
        return deepcopy(measurement)
    rebound = {key: deepcopy(value) for key, value in measurement.items() if key != "sha256"}
    rebound["source_sha256"] = new_identity["sha256"]
    rebound.setdefault("origin", {"measurement_sha256": measurement["sha256"], "source_sha256": old_identity["sha256"],
                                  "qualification": "Rebound only after exact source/configuration/scope comparison; original measurement evidence retained."})
    return _sealed(rebound)


def compare_measurements(before: dict, after: dict, *, judgment=None) -> dict:
    if not _valid(before) or not _valid(after):
        raise ValueError("invalid measurement digest")
    for key in ("subject", "metric_sha256", "method_sha256", "units", "direction", "metric_kind"):
        if before.get(key) != after.get(key):
            raise ValueError("measurement methods/units/subjects are not comparable")
    old, new = before["raw_value"], after["raw_value"]
    numeric = isinstance(old, (int, float)) and isinstance(new, (int, float))
    if numeric:
        direction = before.get("direction")
        if direction not in {"lower", "higher"}:
            raise ValueError("numeric comparison needs metric direction")
        delta = (old - new) if direction == "lower" else (new - old)
        improved = delta > 0
        percent = delta / old * 100 if before.get("metric_kind") == "length" and old != 0 else None
    else:
        if not isinstance(judgment, dict) or not isinstance(judgment.get("improved"), bool) or not judgment.get("reason"):
            raise ValueError("qualitative comparison requires an explicit evidenced judgment")
        improved, delta, percent = judgment["improved"], None, None
    score = percent if before.get("metric_kind") == "length" else delta if before.get("metric_kind") in {"modularity", "completion"} else None
    qualification = "Comparison uses the recorded method and reported tool/agent evidence, not an independently executed metric scorer; no aggregate score is invented."
    if all(row.get("evidence_kind") == "measured" and isinstance(row.get("evidence"), dict)
           and row["evidence"].get("verifier_owned_execution") is True for row in (before, after)):
        qualification = ("Comparison uses controller-executed scorer output for the frozen method and exact sources; "
                         "this is not independent proof of metric validity or semantic equivalence. No aggregate score is invented.")
    return {"subject": before["subject"], "before": old, "after": new, "improved": improved,
            "delta": delta, "improvement_percent": percent, "zero_baseline": old == 0,
            "improvement_score": score,
            "regressed": delta < 0 if numeric else bool((judgment or {}).get("regressed", False)),
            "before_sha256": before["sha256"], "after_sha256": after["sha256"],
            "evidence_kind": "measured" if numeric and before["evidence_kind"] == after["evidence_kind"] == "measured" else "agent_judged",
            "judgment": deepcopy(judgment), "qualification": qualification}
