"""Deterministic review of one immutable prove candidate declaration."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import blueprint


_FORBIDDEN_ADDITION_RE = re.compile(r"\b(sorry|admit|axiom|native_decide)\b")
_DECL_HEAD_RE = re.compile(
    r"\b(?:theorem|lemma|def|abbrev|opaque|axiom)\s+[A-Za-z0-9_.'₀-₉]+\s*(.*)",
    re.DOTALL,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


def _statement_payload(statement: str) -> str:
    """Normalize a source declaration head while allowing axiom -> theorem conversion."""
    normalized = re.sub(r"\s+", " ", statement.strip())
    match = _DECL_HEAD_RE.search(normalized)
    return (match.group(1) if match else normalized).strip()


def _source_declaration(project_root: Path, declaration: dict) -> dict | None:
    expected_file = declaration.get("file", "")
    short_name = str(declaration.get("title") or declaration.get("decl") or "").rsplit(".", 1)[-1]
    matches = []
    for file, rows, bodies in blueprint.scan_blueprint(project_root):
        if expected_file and file != expected_file:
            continue
        for row in rows:
            if row["name"] == short_name or declaration.get("decl", "").endswith("." + row["name"]):
                matches.append({**row, "file": file, "body": bodies[row["name"]]})
    if len(matches) != 1:
        return None
    row = matches[0]
    row["statement"] = blueprint._signature(row["body"])
    return row


def _kernel_declaration(kernel: dict, declaration: dict) -> tuple[str, dict] | None:
    decl = declaration.get("decl", "")
    if decl in kernel:
        return decl, kernel[decl]
    short_name = str(declaration.get("title") or decl).rsplit(".", 1)[-1]
    matches = [(name, row) for name, row in kernel.items()
               if name == short_name or name.endswith("." + short_name)]
    return matches[0] if len(matches) == 1 else None


def _unsafe_dependency_closure(kernel: dict, target: str) -> list[dict]:
    """Return project-owned sorry/axiom dependencies reachable from the target."""
    unsafe = []
    seen = set()
    pending = list(kernel.get(target, {}).get("deps", []))
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        row = kernel.get(name)
        if not row:
            continue
        if row.get("sorried") or row.get("kind") == "axiom":
            unsafe.append({
                "name": name,
                "kind": row.get("kind", ""),
                "sorried": bool(row.get("sorried")),
            })
        pending.extend(row.get("deps", []))
    return sorted(unsafe, key=lambda item: item["name"])


def _forbidden_additions(candidate_diff: str) -> list[str]:
    found = set()
    for line in candidate_diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        code = line[1:].split("--", 1)[0]
        found.update(match.group(1) for match in _FORBIDDEN_ADDITION_RE.finditer(code))
    return sorted(found)


def review_candidate(
    project_root: Path,
    candidate: dict,
    declaration: dict,
    *,
    base_main_sha: str,
    candidate_diff: str,
) -> dict:
    """Review the built candidate currently applied to the main checkout.

    Kernel extraction is authoritative when available. Older toolchains that cannot run
    the bundled extractor use a conservative exact-source fallback.
    """
    project_root = Path(project_root)
    issues: list[str] = []
    forbidden = _forbidden_additions(candidate_diff)
    if forbidden:
        issues.append("candidate adds forbidden construct(s): " + ", ".join(forbidden))

    source = _source_declaration(project_root, declaration)
    target_exists = source is not None
    if source is None:
        issues.append("exact target declaration was not found in its recorded source file")

    expected_statement = declaration.get("statement", "")
    source_signature_unchanged = bool(
        source
        and expected_statement
        and _statement_payload(source["statement"]) == _statement_payload(expected_statement)
    )

    kernel = blueprint.kernel_extract(project_root)
    kernel_available = kernel is not None
    kernel_target = _kernel_declaration(kernel, declaration) if kernel_available else None
    if kernel_available and kernel_target is None:
        issues.append("exact target declaration was not found in the built kernel environment")
    expected_type = declaration.get("kernel_type_repr", "")
    actual_type = kernel_target[1].get("type_repr", "") if kernel_target else ""
    kernel_signature_unchanged = bool(expected_type and actual_type == expected_type)
    signature_unchanged = (
        kernel_signature_unchanged if expected_type and kernel_target
        else source_signature_unchanged
    )
    if not signature_unchanged:
        issues.append("target declaration type differs from the original prove target")

    mode = "kernel" if kernel_available else "source_fallback"
    target_kind = kernel_target[1].get("kind", "") if kernel_target else (source or {}).get("kind", "")
    target_sorried = bool(
        kernel_target[1].get("sorried") if kernel_target else (source or {}).get("status") == "sorry"
    )
    if target_kind == "axiom":
        issues.append("target declaration remains an axiom")
    if target_sorried:
        issues.append("target declaration still depends directly on sorryAx")

    unsafe_dependencies = (
        _unsafe_dependency_closure(kernel, kernel_target[0])
        if kernel_available and kernel_target else []
    )
    if unsafe_dependencies:
        issues.append("target depends on project-owned sorry/axiom declarations")

    status = "passed" if not issues else "failed"
    return {
        "status": status,
        "stage": "declaration_review",
        "mode": mode,
        "candidate_id": candidate.get("candidate_id", ""),
        "candidate_commit": candidate.get("commit_sha", ""),
        "base_main_commit": base_main_sha,
        "decl": declaration.get("decl", candidate.get("decl", "")),
        "target_exists": target_exists and (
            kernel_target is not None if kernel_available else True
        ),
        "signature_unchanged": signature_unchanged,
        "expected_type_sha256": _digest(expected_type),
        "actual_type_sha256": _digest(actual_type),
        "target_kind": target_kind,
        "target_sorried": target_sorried,
        "unsafe_dependencies": unsafe_dependencies,
        "forbidden_constructs": forbidden,
        "issues": issues,
    }
