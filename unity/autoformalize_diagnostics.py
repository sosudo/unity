"""Small deterministic failure excerpts; complete output remains an artifact."""

from __future__ import annotations

import re

from .artifacts import preview_text


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ERROR = re.compile(
    r"(?:^|\s)(?:error(?:\[[^\]]+\])?|fatal|panic):|"
    r"^Traceback \(most recent call last\):|^\w*(?:Error|Exception):",
    re.IGNORECASE,
)
_BUILD_SUMMARY = re.compile(r"^error:\s*(?:build failed|some required targets logged failures)", re.IGNORECASE)


def failure_excerpt(output: str, limit: int = 2000) -> str:
    """Prioritize error windows over warnings, bounded in UTF-8 bytes.

    Call on the original output, not an already truncated head/tail preview.
    This only selects diagnostics: it never decides whether verification passes.
    """
    clean = _ANSI.sub("", str(output))
    lines = clean.splitlines()
    errors = [i for i, line in enumerate(lines) if _ERROR.search(line)]
    if not errors:
        return preview_text(clean, limit)
    errors.sort(key=lambda i: bool(_BUILD_SUMMARY.search(lines[i])))
    selected: list[str] = []
    covered: set[int] = set()
    for index in errors:
        if index in covered:
            continue
        # Put the actual error before adjacent chatter. Several small windows
        # expose independent errors instead of filling the brief with the first.
        end = min(len(lines), index + 9)
        for following in range(index + 1, end):
            if _ERROR.search(lines[following]):
                end = following
                break
        selected.append(preview_text("\n".join(lines[index:end]), min(limit, 900)))
        covered.update(range(index, end))
        if sum(len(part.encode("utf-8")) + 5 for part in selected) >= limit:
            break
    return preview_text("\n...\n".join(selected), limit)
