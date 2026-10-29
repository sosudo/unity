"""Prove-only structural target contracts and kernel-checked candidate assignment."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def freeze_contract(value: dict, decl: str) -> dict:
    if not isinstance(value, dict) or set(value) != {'decl', 'level_params', 'type'} or value['decl'] != decl:
        raise ValueError('invalid structural prove target contract')
    payload = {'decl': decl, 'level_params': value['level_params'], 'type': value['type']}
    return {**payload, 'sha256': _digest(payload)}


def _validate(contract: dict, decl: str) -> dict:
    if not isinstance(contract, dict) or set(contract) != {'decl', 'level_params', 'type', 'sha256'}:
        raise ValueError('missing or malformed structural prove target contract')
    payload = {key: contract[key] for key in ('decl', 'level_params', 'type')}
    if payload['decl'] != decl or not isinstance(payload['level_params'], list) or not isinstance(payload['type'], list):
        raise ValueError('structural prove target contract does not match declaration')
    if not isinstance(contract['sha256'], str) or contract['sha256'] != _digest(payload):
        raise ValueError('structural prove target contract digest mismatch')
    return payload


def inspect(project_root: Path, *, decl: str | None = None, contract: dict | None = None) -> tuple[dict, dict | None]:
    """Extract project kernel metadata, optionally checking a frozen exact target contract.

    Returns (declarations, check). check is None for discovery, otherwise a dict
    with boolean ``passed``. Failures to inspect raise ValueError and never fall
    back to source or printed types.
    """
    from . import blueprint
    root = Path(project_root)
    modules = ['.'.join(path.relative_to(root).with_suffix('').parts)
               for path in blueprint._project_lean_files(root)]
    if not modules:
        raise ValueError('no Lean modules found for prove contract inspection')
    if (decl is None) != (contract is None):
        raise ValueError('declaration and contract must be provided together')
    payload = _validate(contract, decl) if decl is not None else None
    script = Path(__file__).with_suffix('.lean')
    environment = dict(os.environ)
    environment['PATH'] = environment.get('PATH', '') + os.pathsep + str(Path.home() / '.elan' / 'bin')
    try:
        with tempfile.TemporaryDirectory(prefix='unity-prove-contract-') as temp:
            args = ['lake', 'env', 'lean', '--run', str(script), *modules]
            if payload is not None:
                input_path = Path(temp) / 'contract.json'
                input_path.write_text(json.dumps(payload, ensure_ascii=False))
                args += ['--', str(input_path)]
            result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=300, env=environment)
        if result.returncode != 0:
            raise ValueError('Lean prove contract inspector failed: ' + result.stderr[-2000:])
        data = json.loads(result.stdout.strip().splitlines()[-1])
        rows = data['declarations']
        if not isinstance(rows, list):
            raise ValueError('invalid declaration metadata')
        kernel = {row['name']: row for row in rows}
        check = data.get('signature_check')
        if decl is not None and (not isinstance(check, dict) or not isinstance(check.get('passed'), bool)):
            raise ValueError('invalid kernel signature check result')
        return kernel, check
    except (OSError, subprocess.TimeoutExpired, IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Lean prove contract inspection failed: {type(exc).__name__}') from exc


def check_signature(project_root: Path, decl: str, contract: dict) -> dict:
    """Return a kernel assignment check for an exact target and frozen contract."""
    _, check = inspect(project_root, decl=decl, contract=contract)
    return check
