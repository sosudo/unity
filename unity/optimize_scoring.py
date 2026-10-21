"""Verifier-owned scoring of one exact Optimize source snapshot.

Metric prose remains prose. Only an explicit Evaluator protocol executes a
helper. An existing-roster assessment remains agent_judged, even if numeric.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

from . import artifacts, optimize_jobs as jobs, optimize_metrics as metrics

PROTOCOL = "unity-optimize-score-v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ScoringError(ValueError):
    """A score could not be established; this is not no_improvement."""


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ScoringError("scorer response is not finite JSON") from exc


def _subjects(subjects):
    result = list(subjects)
    if (not result or any(not isinstance(subject, str) or not subject for subject in result)
            or len(set(result)) != len(result)):
        raise ScoringError("scoring needs distinct nonempty subjects")
    return sorted(result)


def _stable(root, metric, identity):
    from .optimize_verification import file_identity
    if (file_identity(root, scope=identity.get("scope"), lake_args=identity.get("lake_args", ())) != identity
            or metrics.load_metric(root, metric["name"]) != metric):
        raise ScoringError("source or metric changed during scoring")


class ExecutableEvaluator:
    """Explicit frozen helper, owned by the normal Optimize checked-job path."""

    def __init__(self, metric, *, run_checked, project_root):
        self.metric = deepcopy(metric)
        self.run_checked = run_checked
        self.project_root = Path(project_root).resolve()

    async def __call__(self, request):
        config = self.metric["evaluator"]
        root = Path(request["root"]).resolve()
        _stable(root, self.metric, request["source_identity"])
        directory = root / ".unity" / "metrics"
        helper = directory / config["helper"]
        if (not helper.is_file() or not helper.resolve().is_relative_to(directory.resolve())
                or hashlib.sha256(helper.read_bytes()).hexdigest()
                != self.metric["helpers"][config["helper"]]["sha256"]):
            raise ScoringError("configured scorer helper changed")
        command = [sys.executable, str(helper)] if config.get("interpreter") == "python" else [str(helper)]
        if config.get("interpreter", "executable") == "executable" and not os.access(helper, os.X_OK):
            raise ScoringError("configured scorer helper is not executable")
        folder = self.project_root / ".unity" / "artifacts"
        input_record = artifacts.store_text(folder, _json(request), kind="optimize_score_request", producer="Unity")
        command += ["--request-file", str(folder / "blobs" / input_record["sha256"])]
        output_bytes = 0

        def bounded_output(_stream, text):
            nonlocal output_bytes
            output_bytes += len(text.encode("utf-8"))
            if output_bytes > MAX_RESPONSE_BYTES:
                raise ScoringError("scorer output exceeds the protocol size limit")

        pending = self.run_checked(jobs.run, self.project_root, command, cwd=root,
                                   owner="Unity", task_id="scoring", on_output=bounded_output)
        timeout = config.get("timeout_seconds")
        if timeout is None:
            result = await pending
        else:
            job = asyncio.ensure_future(pending)
            try:
                done, _ = await asyncio.wait({job}, timeout=timeout)
                if not done:
                    job.cancel()
                    try:
                        await job
                    except (asyncio.CancelledError, jobs.JobCancelled):
                        pass
                    # Checked-job cleanup can report JobCancelled instead of
                    # CancelledError. The configured deadline is still the cause.
                    raise ScoringError("configured scorer deadline exceeded")
                result = await job
            except asyncio.CancelledError:
                job.cancel()
                try:
                    await asyncio.shield(job)
                except (asyncio.CancelledError, jobs.JobCancelled):
                    pass
                raise
        # Check fixture/custom job runners as well as the streaming production path.
        if len((result.stdout + result.stderr).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ScoringError("scorer output exceeds the protocol size limit")
        output_record = artifacts.store_text(folder, _json({"stdout": result.stdout, "stderr": result.stderr,
            "returncode": result.returncode}), kind="optimize_score_execution", producer="Unity",
            metadata={"request_sha256": input_record["sha256"], "helper_sha256": self.metric["helpers"][config["helper"]]["sha256"]})
        if result.returncode:
            raise ScoringError("configured scorer failed; evidence " + output_record["artifact_id"])
        try:
            response = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise ScoringError("configured scorer returned invalid JSON; evidence " + output_record["artifact_id"]) from exc
        if not isinstance(response, dict) or response.get("protocol") != PROTOCOL:
            raise ScoringError("configured scorer returned an unsupported protocol")
        response = deepcopy(response)
        response["execution_evidence"] = output_record
        return response


def executable_evaluator(metric, *, run_checked, project_root):
    """Return the explicitly configured evaluator, or None for metric prose."""
    if "evaluator" not in metric:
        return None
    if not metrics._valid(metric) or metric["evaluator"].get("protocol") != PROTOCOL:
        raise ScoringError("unsupported configured scorer")
    return ExecutableEvaluator(metric, run_checked=run_checked, project_root=project_root)


async def score_snapshot(root, *, metric, subjects, source_identity, build_result,
                         original_methods, evaluator, comparison_records=None,
                         project_root=None):
    """Score after build and seal bindings here, never from worker claims.

    evaluator receives one immutable-by-copy request. It returns raw records;
    source hashes, provenance and artifact bindings are assigned by this owner.
    """
    root = Path(root).resolve()
    owner = Path(project_root or root).resolve()
    subjects = _subjects(subjects)
    if not metrics._valid(metric) or not metrics._valid(source_identity):
        raise ScoringError("scoring needs an intact metric and source identity")
    if (not build_result.get("source_stable") or build_result.get("source_identity") != source_identity):
        raise ScoringError("scoring needs build evidence for this exact source")
    if not callable(evaluator):
        raise ScoringError("no evaluator is available for this metric")
    methods = deepcopy(original_methods or {})
    if set(methods) - set(subjects):
        raise ScoringError("scoring methods contain an unrequested subject")
    _stable(root, metric, source_identity)
    request = {"protocol": PROTOCOL, "root": str(root), "subjects": subjects,
        "metric": deepcopy(metric), "source_identity": deepcopy(source_identity),
        "build_result": deepcopy(build_result), "original_methods": methods,
        "comparison_records": deepcopy(comparison_records or {})}
    response = await evaluator(deepcopy(request))
    _stable(root, metric, source_identity)
    encoded = _json(response)
    if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ScoringError("scorer response exceeds the protocol size limit")
    if not isinstance(response, dict) or not isinstance(response.get("measurements"), dict):
        raise ScoringError("scorer measurements must be an object")
    if set(response["measurements"]) != set(subjects):
        raise ScoringError("scorer response does not cover the requested subjects")
    rows = response["measurements"]
    judgments = response.get("judgments", {})
    if not isinstance(judgments, dict) or set(judgments) - set(subjects):
        raise ScoringError("scorer judgments contain an unrequested subject")
    for judgment in judgments.values():
        if (not isinstance(judgment, dict) or not isinstance(judgment.get("improved"), bool)
                or not judgment.get("reason")
                or ("regressed" in judgment and not isinstance(judgment["regressed"], bool))):
            raise ScoringError("scorer comparison judgment is invalid")
    kind = "measured" if isinstance(evaluator, ExecutableEvaluator) else "agent_judged"
    evidence = artifacts.store_text(owner / ".unity" / "artifacts", encoded,
        kind="optimize_score_response", producer="Unity", metadata={
            "source_sha256": source_identity["sha256"], "metric_sha256": metric["sha256"],
            "evidence_kind": kind})
    measurements = {}
    for subject in subjects:
        row = rows[subject]
        if not isinstance(row, dict) or not row.get("evidence"):
            raise ScoringError("scorer record needs described evidence")
        method = row.get("method")
        if subject in methods and method != methods[subject]:
            raise ScoringError("scorer changed the original measurement method")
        # Store full evaluator output once, not once per task/state copy.
        bound_evidence = {"artifact": evidence, "subject": subject}
        if kind == "measured":
            bound_evidence["verifier_owned_execution"] = True
        if isinstance(row["evidence"], dict) and row["evidence"].get("zero_denominator") is True:
            bound_evidence["zero_denominator"] = True
        try:
            measurements[subject] = metrics.record_measurement(metric, subject=subject,
                raw_value=metrics.parse_score_value(metric, row["raw_value"]),
                source_identity=source_identity, method=method,
                evidence=bound_evidence, evidence_kind=kind, units=row.get("units"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ScoringError("scorer returned an invalid measurement record") from exc
    _stable(root, metric, source_identity)
    return metrics._sealed({"schema_version": 1, "source_identity": deepcopy(source_identity),
        "metric_sha256": metric["sha256"], "measurements": measurements,
        "judgments": deepcopy(judgments), "evidence": evidence, "evaluator_kind": kind})
