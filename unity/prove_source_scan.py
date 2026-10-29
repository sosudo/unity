"""Prove-only lexical guard for newly executable forbidden Lean constructs.

Compare full indexed/base source, including lexical state across unchanged lines.
The build and kernel/axiom reviews remain independent, authoritative checks.
"""
from __future__ import annotations

import bisect
import difflib
import re
import subprocess
from pathlib import Path


_FORBIDDEN = re.compile(r'(sorry|admit|axiom|native_decide)')
_RAW_START = re.compile(r'r(#+)?"')
_CHAR = re.compile(r"'(?:[^'\\\n]|\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.))'")
_DIRECT_INTERPOLATION = re.compile(
    r'\b(?:dbg_trace|throwNamedError(?:At)?|logNamed(?:Error|Warning)(?:At)?)\b|Macro\.trace\b'
)


class SourceScanError(ValueError):
    """Source cannot be safely classified; candidate review must fail closed."""


def _identifier_char(char: str) -> bool:
    r"""Lean 4.28 `isIdRest`; Python's Unicode `\w` is not equivalent."""
    if not char:
        return False
    cp = ord(char)
    return (
        'a' <= char <= 'z' or 'A' <= char <= 'Z' or '0' <= char <= '9'
        or char in "_'!?"
        or (0x3B1 <= cp <= 0x3C9 and cp != 0x3BB)
        or (0x391 <= cp <= 0x3A9 and cp not in (0x3A0, 0x3A3))
        or 0x3CA <= cp <= 0x3FB or 0x1F00 <= cp <= 0x1FFE
        or 0x2100 <= cp <= 0x214F or 0x1D49C <= cp <= 0x1D59F
        or (0xC0 <= cp <= 0xFF and cp not in (0xD7, 0xF7))
        or 0x100 <= cp <= 0x17F or 0x2080 <= cp <= 0x2089
        or 0x2090 <= cp <= 0x209C or 0x1D62 <= cp <= 0x1D6A or cp == 0x2C7C
    )


def _identifier_first(char: str) -> bool:
    if not char:
        return False
    cp = ord(char)
    return _identifier_char(char) and not (
        '0' <= char <= '9' or char in "'!?" or 0x2080 <= cp <= 0x2089
        or 0x2090 <= cp <= 0x209C or 0x1D62 <= cp <= 0x1D6A or cp == 0x2C7C
    )


def _forbidden_matches(text: str):
    for match in _FORBIDDEN.finditer(text):
        before = text[match.start() - 1] if match.start() else ''
        after = text[match.end()] if match.end() < len(text) else ''
        before_dot_ident = (before == '.' and match.start() >= 2
                            and (_identifier_char(text[match.start() - 2])
                                 or text[match.start() - 2] == '»'))
        after_dot_ident = (after == '.' and match.end() + 1 < len(text)
                           and (_identifier_first(text[match.end() + 1])
                                or text[match.end() + 1] == '«'))
        if not (_identifier_char(before) or _identifier_char(after) or before_dot_ident or after_dot_ident):
            yield match


def _code_mask(source: str) -> bytearray:
    """Return one for executable source characters and zero for lexical trivia.

    Interpolated string expressions and quotation code are recursively scanned.
    Ambiguous custom interpolated-string contexts containing a forbidden token
    fail closed rather than treating a possible expression as ordinary text.
    """
    mask = bytearray(b'\1') * len(source)
    n = len(source)

    def hide(start: int, end: int) -> None:
        mask[start:end] = b'\0' * (end - start)

    def block(i: int) -> int:
        start = i
        depth = 1
        i += 2
        while i < n and depth:
            pair = source[i:i + 2]
            if pair == '/-':
                depth += 1; i += 2
            elif pair == '-/':
                depth -= 1; i += 2
            else:
                i += 1
        if depth:
            raise SourceScanError('unterminated Lean block comment')
        hide(start, i)
        return i

    def interp_prefix(i: int) -> bool:
        # Bound lookback; an unknown long-distance custom interpolation with a
        # forbidden token is rejected by the ambiguous-string check below.
        prefix = source[max(0, i - 512):i].rstrip()
        if prefix.endswith('!'):
            return True
        # These built-in commands accept a bare interpolated string argument.
        return bool(_DIRECT_INTERPOLATION.search(prefix.rsplit('\n', 1)[-1]))

    def string(i: int, interpolated: bool) -> int:
        start = i
        i += 1
        chunk = i
        while i < n:
            if source[i] == '\\':
                if i + 1 >= n:
                    raise SourceScanError('unterminated Lean string escape')
                i += 2
            elif source[i] == '"':
                hide(chunk, i + 1)
                if not interpolated:
                    literal = source[start + 1:i]
                    # A parser extension can use even a bare string as an
                    # interpolated string. Its grammar is not available to this
                    # lexical pass, so ambiguous forbidden-token splices fail closed.
                    cursor = 0
                    while cursor < len(literal):
                        if literal[cursor] == '\\':
                            cursor += 2
                        elif literal[cursor] == '{':
                            if any(match.start() > cursor for match in _forbidden_matches(literal)):
                                raise SourceScanError('ambiguous interpolated string containing a forbidden token')
                            cursor += 1
                        else:
                            cursor += 1
                return i + 1
            elif source[i] == '{' and interpolated:
                hide(chunk, i + 1)
                i = code(i + 1, interpolation=True)
                chunk = i
            else:
                i += 1
        raise SourceScanError('unterminated Lean string')

    def code(i: int, interpolation: bool = False) -> int:
        braces = 0
        while i < n:
            pair = source[i:i + 2]
            ch = source[i]
            if pair == '/-':
                i = block(i)
            elif pair == '--':
                end = source.find('\n', i)
                end = n if end < 0 else end
                hide(i, end); i = end
            elif pair == '-/':
                raise SourceScanError('unmatched Lean block comment terminator')
            elif ch == '«':
                end = source.find('»', i + 1)
                if end < 0:
                    raise SourceScanError('unterminated escaped Lean identifier')
                hide(i, end + 1); i = end + 1
            elif ch == 'r' and (i == 0 or not _identifier_char(source[i - 1])):
                raw = _RAW_START.match(source, i)
                if raw:
                    terminator = '"' + (raw.group(1) or '')
                    end = source.find(terminator, raw.end())
                    if end < 0:
                        raise SourceScanError('unterminated Lean raw string')
                    end += len(terminator)
                    hide(i, end); i = end
                else:
                    i += 1
            elif ch == "'" and (i == 0 or not _identifier_char(source[i - 1])):
                char = _CHAR.match(source, i)
                if char:
                    hide(i, char.end()); i = char.end()
                elif i + 1 < n and (source[i + 1] == '\\' or source[i + 1].isalnum()):
                    raise SourceScanError('unsupported or malformed Lean character literal')
                else:
                    # Apostrophes also occur in syntax quotations and identifiers.
                    # Keep unclassified cases visible to the forbidden-token scan.
                    i += 1
            elif ch == '"':
                hide(i, i + 1)
                i = string(i, interp_prefix(i))
            elif interpolation and ch == '{':
                braces += 1; i += 1
            elif interpolation and ch == '}':
                if braces == 0:
                    hide(i, i + 1)
                    return i + 1
                braces -= 1; i += 1
            else:
                i += 1
        if interpolation:
            raise SourceScanError('unterminated Lean string interpolation')
        return i

    code(0)
    return mask


def newly_forbidden(old: str, new: str) -> set[str]:
    """Find candidate tokens not mapped to unchanged executable base spans."""
    old_mask = _code_mask(old)
    new_mask = _code_mask(new)
    matches = [match for match in _forbidden_matches(new)
               if all(new_mask[match.start():match.end()])]
    if not matches:
        return set()
    # Only whole unchanged lines qualify for reuse. This is conservative for
    # edited lines and detects comment-delimiter edits exposing later lines.
    # The default popular-line cutoff bounds repeated-line matching work.
    old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    old_offsets, new_offsets = [], []
    offset = 0
    for line in old_lines:
        old_offsets.append(offset); offset += len(line)
    offset = 0
    for line in new_lines:
        new_offsets.append(offset); offset += len(line)
    mapping = {}
    # A complete unchanged old file embedded in the new file is common when
    # headers/footers are added. Locate it in linear time even if every line is
    # identical, avoiding quadratic matching or popular-line false positives.
    embedded = new.find(old) if old else -1
    if embedded >= 0:
        first = bisect.bisect_left(new_offsets, embedded)
        if first < len(new_offsets) and new_offsets[first] == embedded:
            for index, line in enumerate(old_lines):
                if first + index >= len(new_lines) or new_lines[first + index] != line:
                    break
                mapping[first + index] = index
    prefix = 0
    while prefix < min(len(old_lines), len(new_lines)) and old_lines[prefix] == new_lines[prefix]:
        mapping[prefix] = prefix
        prefix += 1
    old_end, new_end = len(old_lines), len(new_lines)
    while old_end > prefix and new_end > prefix and old_lines[old_end - 1] == new_lines[new_end - 1]:
        old_end -= 1; new_end -= 1
        mapping[new_end] = old_end
    for kind, old_start, old_stop, new_start, _ in difflib.SequenceMatcher(
            None, old_lines[prefix:old_end], new_lines[prefix:new_end]).get_opcodes():
        if kind == 'equal':
            mapping.update((prefix + new_start + index, prefix + old_start + index)
                           for index in range(old_stop - old_start))
    found = set()
    for match in matches:
        start, end = match.span()
        line = bisect.bisect_right(new_offsets, start) - 1
        unchanged = False
        if line in mapping and end <= new_offsets[line] + len(new_lines[line]):
            prior = old_offsets[mapping[line]] + start - new_offsets[line]
            unchanged = all(old_mask[prior:prior + end - start])
        if not unchanged:
            found.add(match.group(1))
    return found


def _git_blobs(root: Path, specs: list[str]) -> list[str | None]:
    if any('\n' in spec or '\r' in spec for spec in specs):
        raise SourceScanError('unsupported newline in Lean file path')
    try:
        result = subprocess.run(['git', '-C', str(root), 'cat-file', '--batch'],
                                input=('\n'.join(specs) + '\n').encode(), capture_output=True, check=True)
        data = result.stdout
        pos = 0
        blobs = []
        for _ in specs:
            end = data.index(b'\n', pos)
            header = data[pos:end]; pos = end + 1
            if header.endswith(b' missing'):
                blobs.append(None); continue
            fields = header.split()
            if len(fields) != 3 or fields[1] != b'blob':
                raise SourceScanError('Lean path does not reference a Git blob')
            size = int(fields[2])
            content = data[pos:pos + size]
            if len(content) != size or data[pos + size:pos + size + 1] != b'\n':
                raise SourceScanError('invalid Git blob stream')
            blobs.append(content.decode('utf-8'))
            pos += size + 1
        return blobs
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, IndexError) as exc:
        if isinstance(exc, SourceScanError):
            raise
        raise SourceScanError(f'cannot read exact Lean source: {type(exc).__name__}') from exc


def scan_git_sources(root: Path, base: str, candidate: str | None = None) -> list[str]:
    """Scan staged Lean files, or an immutable candidate commit for audit tests."""
    command = ['git', '-C', str(root), 'diff', '--name-only', '-z', '--no-renames', '--diff-filter=AM']
    command += ['--cached', base] if candidate is None else [base, candidate]
    try:
        raw = subprocess.check_output(command)
        paths = [part.decode('utf-8') for part in raw.split(b'\0') if part and part.endswith(b'.lean')]
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise SourceScanError(f'cannot enumerate changed Lean source: {type(exc).__name__}') from exc
    if not paths:
        return []
    # A Git symlink blob contains a path, not the Lean bytes that the build
    # follows. Refuse such changed sources rather than inspecting the wrong text.
    try:
        base_tree = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-r', '-z', base])
        if candidate is None:
            candidate_tree = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--stage', '-z'])
        else:
            candidate_tree = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-r', '-z', candidate])
        def modes(data: bytes) -> dict[str, str]:
            result = {}
            for entry in data.split(b'\0'):
                if entry:
                    metadata, name = entry.split(b'\t', 1)
                    result[name.decode('utf-8')] = metadata.split()[0].decode('ascii')
            return result
        old_modes, new_modes = modes(base_tree), modes(candidate_tree)
        for path in paths:
            if new_modes.get(path) not in {'100644', '100755'} or old_modes.get(path, '100644') not in {'100644', '100755'}:
                raise SourceScanError(f'Lean source is not a regular Git file: {path}')
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        if isinstance(exc, SourceScanError):
            raise
        raise SourceScanError(f'cannot inspect Lean file modes: {type(exc).__name__}') from exc
    specs = []
    for path in paths:
        specs += [f'{base}:{path}', f'{candidate}:{path}' if candidate else f':{path}']
    blobs = _git_blobs(root, specs)
    forbidden = set()
    for index, path in enumerate(paths):
        old, new = blobs[index * 2:index * 2 + 2]
        if new is None:
            raise SourceScanError(f'changed Lean file is missing from candidate: {path}')
        try:
            forbidden.update(newly_forbidden(old or '', new))
        except SourceScanError as exc:
            raise SourceScanError(f'{path}: {exc}') from exc
    return sorted(forbidden)
