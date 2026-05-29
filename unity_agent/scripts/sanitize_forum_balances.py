#!/usr/bin/env python3
"""Sanitize a Unity forum's balances.json.

Drops obviously-non-agent ledger entries that accumulated due to two earlier
bugs in forum_mcp.py:

  1. The unbounded @-mention regex matched Lean code snippets like
     `@MeasureTheory.foo` and `@implicit`, creating phantom ledger rows.
  2. Author casing was not canonicalized, so `FORMALIZER`, `Formalizer`,
     and `Formalizer-Subagent` accumulated as three separate rows.

Both bugs are fixed in the live code; this script retroactively scrubs
existing archives that were captured before the fix.

Usage:
    python sanitize_forum_balances.py <forum-dir>          # in-place rewrite
    python sanitize_forum_balances.py <forum-dir> --dry-run  # report only

Safe to re-run; backs up to balances.json.bak before writing.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Obvious mathlib / Python noise that shows up as @-mention false-positives.
_PHANTOM_PREFIXES = {
    "MeasureTheory", "ContinuousLinearMap", "Finset", "Mathlib", "Real",
    "Set", "List", "Function", "Std", "Lean", "Classical", "OrderedField",
    "Nat", "Int", "Rat", "Complex", "Polynomial", "Topology", "Filter",
    "Submodule", "LinearMap", "Module", "Ring", "Field", "Group", "Monoid",
}
_PHANTOM_LOWER = {
    "implicit", "contextmanager", "staticmethod", "classmethod", "property",
    "dataclass", "abstractmethod", "override", "deprecated", "noinline",
    "inline", "ext", "simp", "norm_cast", "elab", "macro",
}

_AUTHOR_SUFFIX_RE = re.compile(r'-(subagent|agent|node|worker)$')


def canonical_author(name: str) -> str:
    n = (name or "").strip().lower()
    n = re.sub(r"[\s_-]+", "-", n)
    n = _AUTHOR_SUFFIX_RE.sub("", n)
    return n


def is_phantom(name: str, rec: dict) -> bool:
    """True if this row looks like noise rather than a real agent."""
    # Mathlib namespace lookalike (capital first letter, contained in our blocklist)
    if name in _PHANTOM_PREFIXES:
        return True
    # Python builtin / decorator lookalike
    if name.lower() in _PHANTOM_LOWER:
        return True
    # Zero balance and no credit history — was created by a mention only, never posted.
    if rec.get("balance", 0.0) == 0.0 and not rec.get("history"):
        return True
    return False


def collapse_case_variants(balances: dict) -> tuple[dict, list[tuple[str, list[str]]]]:
    """Group rows by canonical_author, sum balances and merge histories.

    Returns (merged_balances, [(canonical_key, [collapsed_raw_keys]), ...]).
    """
    groups: dict[str, list[str]] = {}
    for raw in balances:
        groups.setdefault(canonical_author(raw), []).append(raw)

    merged: dict = {}
    collapsed: list[tuple[str, list[str]]] = []
    for canon, raws in groups.items():
        if len(raws) == 1 and raws[0] == canon:
            merged[canon] = balances[raws[0]]
            continue
        # Pick the longest non-canonical form as display_name (preserves casing
        # like `DECLARATION-FORMALIZER` over canon `declaration-formalizer`).
        display = max(raws, key=len)
        combined = {
            "display_name": display,
            "balance": round(sum(balances[r].get("balance", 0.0) for r in raws), 2),
            "history": [],
            "notifications": [],
        }
        for r in raws:
            combined["history"].extend(balances[r].get("history", []))
            combined["notifications"].extend(balances[r].get("notifications", []))
        combined["history"].sort(key=lambda h: h.get("timestamp", 0))
        merged[canon] = combined
        collapsed.append((canon, raws))
    return merged, collapsed


def main() -> int:
    ap = argparse.ArgumentParser(description="Sanitize a Unity forum's balances.json")
    ap.add_argument("forum_dir", type=Path, help="Forum directory containing balances.json")
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = ap.parse_args()

    bp = args.forum_dir / "balances.json"
    if not bp.exists():
        print(f"No balances.json in {args.forum_dir}", file=sys.stderr)
        return 1

    balances = json.loads(bp.read_text())

    # Phase 1: drop phantom rows.
    phantoms = [name for name, rec in balances.items() if is_phantom(name, rec)]
    for name in phantoms:
        del balances[name]

    # Phase 2: collapse case variants.
    merged, collapsed = collapse_case_variants(balances)

    print(f"== {bp} ==")
    print(f"  dropped phantom rows: {len(phantoms)}")
    for name in phantoms:
        print(f"    - {name!r}")
    print(f"  collapsed casing groups: {len(collapsed)}")
    for canon, raws in collapsed:
        print(f"    - {canon!r} <- {raws}")
    print(f"  final row count: {len(merged)} (was {len(json.loads(bp.read_text()))})")

    if args.dry_run:
        print("  (dry-run — no files written)")
        return 0

    bak = bp.with_suffix(".json.bak")
    bak.write_text(bp.read_text())
    bp.write_text(json.dumps(merged, indent=2))
    print(f"  wrote {bp} (backup at {bak})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
