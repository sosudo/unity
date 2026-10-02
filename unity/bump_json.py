"""Lossless compact JSON for Bump's large, immutable native evidence.

The standard C encoder creates one compact text value; bounded slices avoid a
second whole-value UTF-8 allocation while writing or hashing. This deliberately
trades that one compact allocation for much lower CPU cost than a Python walk.
This is a storage codec, not a semantic hash or acceptance policy.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

_CHUNK_CHARACTERS = 65_536
_ENCODER = json.JSONEncoder(sort_keys=True, separators=(",", ":"))


def iterencode(value):
    """Emit bounded slices of exactly compact sorted ``json.dumps`` text.

The standard encoder retains its key coercions, float/NaN/Unicode behavior,
unsupported-value errors and circular-reference policy. No data is omitted.
The complete compact text is allocated once; this is not an incremental parser
or a bounded-memory encoder for arbitrarily large values.
"""
    text = _ENCODER.encode(value)
    for start in range(0, len(text), _CHUNK_CHARACTERS):
        yield text[start:start + _CHUNK_CHARACTERS]


def mutation_digest(value) -> bytes:
    """Detect exactly the JSON-visible changes previously compared as strings."""
    hashed = hashlib.sha256()
    for chunk in iterencode(value):
        hashed.update(chunk.encode("utf-8"))
    return hashed.digest()


def atomic_dump(path: Path, value) -> None:
    """Write compact sorted JSON plus newline; preserve old bytes on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".bump-json-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            for chunk in iterencode(value):
                output.write(chunk)
            output.write("\n")
            output.flush()
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
