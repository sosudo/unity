"""Prove-only structural target contracts and kernel-checked candidate assignment."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


_LEGACY_FIELDS = {'decl', 'level_params', 'type'}
_MODULE_FIELDS = _LEGACY_FIELDS | {'module', 'origins'}


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _valid_name(value: object) -> bool:
    """Validate the existing Lean Name encoding without interpreting a type."""
    pending = [value]
    while pending:
        node = pending.pop()
        if not isinstance(node, list) or not node:
            return False
        if node[0] == 'anonymous':
            if len(node) != 1:
                return False
        elif node[0] == 'str':
            if len(node) != 3 or not isinstance(node[2], str):
                return False
            pending.append(node[1])
        elif node[0] == 'num':
            if len(node) != 3 or type(node[2]) is not int or node[2] < 0:
                return False
            pending.append(node[1])
        else:
            return False
    return True


def _payload(value: dict, decl: str) -> dict:
    if not isinstance(value, dict) or set(value) not in (_LEGACY_FIELDS, _MODULE_FIELDS):
        raise ValueError('invalid structural prove target contract')
    if (not isinstance(decl, str) or not decl or value['decl'] != decl
            or not isinstance(value['level_params'], list) or not isinstance(value['type'], list)):
        raise ValueError('structural prove target contract does not match declaration')
    if 'module' in value:
        if not isinstance(value['module'], str) or not value['module'].strip():
            raise ValueError('invalid owning module in structural prove target contract')
        if not isinstance(value['origins'], list):
            raise ValueError('invalid constant origins in structural prove target contract')
        seen = set()
        for origin in value['origins']:
            if (not isinstance(origin, dict) or set(origin) != {'name', 'module'}
                    or not _valid_name(origin['name']) or origin['name'] == ['anonymous']
                    or (origin['module'] is not None and (
                        not isinstance(origin['module'], str) or not origin['module'].strip()))):
                raise ValueError('invalid constant origin in structural prove target contract')
            identity = json.dumps(origin['name'], separators=(',', ':'), ensure_ascii=False)
            if identity in seen:
                raise ValueError('duplicate constant origin in structural prove target contract')
            seen.add(identity)
    return dict(value)


def freeze_contract(value: dict, decl: str) -> dict:
    payload = _payload(value, decl)
    return {**payload, 'sha256': _digest(payload)}


def _validate(contract: dict, decl: str) -> dict:
    if (not isinstance(contract, dict)
            or set(contract) not in (_LEGACY_FIELDS | {'sha256'}, _MODULE_FIELDS | {'sha256'})):
        raise ValueError('missing or malformed structural prove target contract')
    payload = _payload({key: value for key, value in contract.items() if key != 'sha256'}, decl)
    if not isinstance(contract['sha256'], str) or contract['sha256'] != _digest(payload):
        raise ValueError('structural prove target contract digest mismatch')
    return payload


def _merge_rows(rows: list, modules: list[str]) -> tuple[dict, dict[str, list[str]]]:
    """Keep owning-module identities and resolve edges in their original environments."""
    inventory = set(modules)
    owned = {}
    owners: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid declaration metadata row')
        name, module = row.get('name'), row.get('module')
        if not isinstance(name, str) or not name or not isinstance(module, str) or module not in inventory:
            raise ValueError('invalid declaration name or owning module')
        identity = (module, name)
        if identity in owned:
            raise ValueError(f'duplicate declaration metadata for {module}:{name}')
        deps, dependency_modules = row.get('deps'), row.get('dependency_modules')
        if (not isinstance(deps, list) or any(not isinstance(dep, str) or not dep for dep in deps)
                or len(deps) != len(set(deps)) or not isinstance(dependency_modules, dict)
                or set(dependency_modules) != set(deps)):
            raise ValueError(f'invalid dependency metadata for {module}:{name}')
        if 'contract' in row:
            contract = _payload(row['contract'], name)
            if contract.get('module', module) != module:
                raise ValueError(f'contract owner does not match declaration {module}:{name}')
        owned[identity] = row
        owners.setdefault(name, []).append(module)
    duplicates = {name: sorted(values) for name, values in owners.items() if len(values) > 1}
    identities = {identity: (f'{identity[0]}:{identity[1]}' if identity[1] in duplicates else identity[1])
                  for identity in owned}
    if len(set(identities.values())) != len(identities):
        raise ValueError('module-qualified declaration key collision')
    kernel = {}
    for identity, row in owned.items():
        deps = []
        for name in row['deps']:
            module = row['dependency_modules'][name]
            if not isinstance(module, str) or (module, name) not in identities:
                raise ValueError(f'unresolved dependency {module}:{name} from {identity[0]}:{identity[1]}')
            deps.append(identities[(module, name)])
        kernel[identities[identity]] = {**row, 'deps': deps}
    return kernel, duplicates


def _validate_owned_names(value: object, modules: list[str], kernel: dict) -> dict[str, list[str]]:
    """Validate all owned constants, including names omitted from target metadata."""
    if not isinstance(value, list):
        raise ValueError('invalid complete owned-name inventory')
    inventory = set(modules)
    pairs = set()
    owners: dict[str, list[str]] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {'name', 'module'}:
            raise ValueError('invalid complete owned-name inventory entry')
        name, module = item['name'], item['module']
        if not isinstance(name, str) or not name or not isinstance(module, str) or module not in inventory:
            raise ValueError('invalid constant name or module in complete owned-name inventory')
        pair = (module, name)
        if pair in pairs:
            raise ValueError(f'duplicate owned-name inventory entry for {module}:{name}')
        pairs.add(pair)
        owners.setdefault(name, []).append(module)
    for row in kernel.values():
        if (row['module'], row['name']) not in pairs:
            raise ValueError(f'declaration is missing from owned-name inventory: {row["module"]}:{row["name"]}')
    return {name: sorted(values) for name, values in owners.items() if len(values) > 1}


def inspect(project_root: Path, *, decl: str | None = None, contract: dict | None = None) -> tuple[dict, dict | None]:
    """Extract project kernel metadata, optionally checking a frozen exact target contract.

    Returns (declarations, check). check is None for discovery, otherwise a dict
    with boolean ``passed``. Failures to inspect raise ValueError and never fall
    back to source or printed types. Build exactly the complete module inventory
    that the inspector imports, including modules outside default Lake targets.
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
        # A plain `lake build` only covers default targets and their imports.
        # Keep nondefault/unreferenced project modules in the inspection scope,
        # and refresh their oleans instead of importing missing or stale output.
        build = subprocess.run(
            ['lake', 'build', *(f'+{module}:olean' for module in modules)],
            cwd=root, capture_output=True, text=True, env=environment,
        )
        if build.returncode != 0:
            output = '\n'.join(part for part in (build.stdout, build.stderr) if part)
            raise ValueError('Lean prove inspection module build failed: ' + output[-4000:])
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
        kernel, _ = _merge_rows(rows, modules)
        duplicates = _validate_owned_names(data['owned_names'], modules, kernel)
        if payload is not None and 'module' not in payload and duplicates:
            detail = '; '.join(f'{name} ({", ".join(owners)})' for name, owners in sorted(duplicates.items()))
            raise ValueError('legacy prove contract requires a collision-free project: ' + detail)
        check = data.get('signature_check')
        if decl is not None and (not isinstance(check, dict) or not isinstance(check.get('passed'), bool)):
            raise ValueError('invalid kernel signature check result')
        if decl is not None and check.get('passed') is True:
            matches = [row for row in kernel.values() if row['name'] == decl
                       and ('module' not in payload or row['module'] == payload['module'])]
            if len(matches) != 1 or (check.get('module') is not None and check['module'] != matches[0]['module']):
                raise ValueError('kernel signature check does not identify the exact owning module')
        return kernel, check
    except (OSError, subprocess.TimeoutExpired, IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Lean prove contract inspection failed: {type(exc).__name__}') from exc


def check_signature(project_root: Path, decl: str, contract: dict) -> dict:
    """Return a kernel assignment check for an exact target and frozen contract."""
    _, check = inspect(project_root, decl=decl, contract=contract)
    return check
