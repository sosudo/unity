"""Whole-process read-only isolation for Optimize AGY scoring and review.

This is not a launcher for other pipelines or ordinary Optimize writers. The
outer Optimize process-group wrapper remains responsible for host birth/closure
receipts. This wrapper changes only the child filesystem view and physical cwd.
Network and authentication configuration are not changed. It does not claim to
isolate mutually distrustful workers' shared Forum state or external services.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


_SHARED = ("forum", "artifacts", "logs", "jobs", "tmp")
# Existing CLI runtime children, not a grant to the profile directory itself.
# Settings, auth storage, updater, binaries and absent siblings stay read-only.
# The generated agentapi shell script uses a private file overlay, never a host
# write grant. Unsupported native storage arrangements fail closed.
_AGY_RUNTIME = ("cache", "crashes", "scratch", "presence", "knowledge",
                "conversations", "implicit", "brain", "log")
_SETTINGS_LIMIT = 1024 * 1024


def _settings_bytes(path: Path) -> bytes:
    """Read a bounded, stable settings file without publishing private values."""
    _exact(path, directory=False)
    before = path.stat()
    if before.st_nlink != 1:
        raise ValueError("Optimize AGY settings has an unsupported hardlink")
    if before.st_size > _SETTINGS_LIMIT:
        raise ValueError("Optimize AGY settings file is unsupported")
    with path.open("rb") as handle:
        data = handle.read(_SETTINGS_LIMIT + 1)
    after = path.stat()
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in stable) or len(data) != before.st_size:
        raise ValueError("Optimize AGY settings changed during startup")
    return data


def _readonly_settings(data: bytes, writable: tuple[Path, ...], readable: tuple[Path, ...] = ()) -> dict:
    """Grant the inner terminal sandbox only the outer sandbox's runtime roots.

    This is a turn-local overlay, not an edit to the user's saved AGY settings.
    Preserve existing restrictions. Never auto-approve an unsandboxed command.
    """
    try:
        settings = json.loads(data)
    except (ValueError, UnicodeError):
        raise ValueError("Optimize AGY settings are not valid JSON") from None
    if not isinstance(settings, dict):
        raise ValueError("Optimize AGY settings must be an object")
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("Optimize AGY permissions must be an object")
    for key in ("allow", "deny", "ask"):
        rules = permissions.setdefault(key, [])
        if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in rules):
            raise ValueError("Optimize AGY permission rules must be string lists")
    for action, root in [("write_file", root) for root in writable] + [("read_file", root) for root in readable]:
        # AGY rules use action(path), with '*' reserved for global matching.
        # Do not let a pathname inject a rule or expand the exact write grant.
        if not root.is_absolute() or any(c in str(root) for c in "()*?[]\\\r\n\x00"):
            raise ValueError("Optimize AGY runtime path cannot be a permission rule")
        rule = f"{action}({root})"
        if rule not in permissions["allow"]:
            permissions["allow"].append(rule)
    if "unsandboxed(*)" not in permissions["deny"]:
        permissions["deny"].append("unsandboxed(*)")
    settings["enableTerminalSandbox"] = True
    settings["toolPermission"] = "proceed-in-sandbox"
    return settings


def _exact(path: Path, *, directory=True) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Optimize AGY sandbox requires exact absolute paths")
    if (directory and not path.is_dir()) or (not directory and not path.is_file()):
        raise ValueError("Optimize AGY sandbox path is unavailable")
    return path


def _identity(path: Path) -> tuple[int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino


def _files(root: Path, excluded=()):
    """Walk without following symlinks; mounted destinations are checked apart."""
    if root.is_file():
        yield root
        return
    for parent, directories, files in os.walk(root, followlinks=False, onerror=_walk_error):
        directories[:] = [name for name in directories
                          if Path(parent, name) not in excluded
                          and not Path(parent, name).is_symlink()]
        for name in files:
            path = Path(parent, name)
            if path not in excluded and not path.is_symlink():
                yield path


def _walk_error(error):
    # Another worker can remove its own runtime scratch during this read. A
    # vanished pathname grants no write access. Binding roots are checked apart.
    if not isinstance(error, FileNotFoundError):
        raise error


def _check_hardlinks(writable: tuple[Path, ...], protected: tuple[Path, ...]):
    # A read-only pathname does not protect an inode which already has a writable
    # hardlink elsewhere. Only walk protected trees if such aliases are possible.
    aliases = set()
    for root in writable:
        for path in _files(root):
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                aliases.add((info.st_dev, info.st_ino))
    if not aliases:
        return
    for root in protected:
        for path in _files(root, excluded=writable):
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if (info.st_dev, info.st_ino) in aliases:
                raise ValueError("Optimize AGY runtime has a hardlink to protected files")


def _validate_plan(plan):
    for raw, device, inode in plan["bindings"]:
        path = Path(raw)
        if path.resolve() != path or _identity(path) != (device, inode):
            raise ValueError("Optimize AGY sandbox binding changed")
    for raw, digest in plan.get("settings_snapshots", ()):
        if hashlib.sha256(_settings_bytes(Path(raw))).hexdigest() != digest:
            raise ValueError("Optimize AGY settings binding changed")
    for raw, target, device, inode, link_device, link_inode in plan.get("log_aliases", ()):
        path = Path(raw)
        link = path.lstat()
        if (not path.is_symlink() or str(path.resolve()) != target
                or _identity(path) != (device, inode)
                or (link.st_dev, link.st_ino) != (link_device, link_inode)):
            raise ValueError("Optimize AGY log binding changed")
    _check_hardlinks(tuple(map(Path, plan["writable"])), tuple(map(Path, plan["protected"])))


@dataclass
class Sandbox:
    directory: Path
    runner: Path
    temporary: Path
    executable: Path
    writable_roots: tuple[Path, ...]
    plan: dict

    def command(self, argv: list[str]) -> list[str]:
        _validate_plan(self.plan)
        return [*self.plan["prefix"], "--", self.plan["agy"], *argv]

    def preflight(self):
        """Check local mounts/namespaces, without launching AGY or any service."""
        _validate_plan(self.plan)
        try:
            result = subprocess.run([*self.plan["prefix"], "--", "/bin/true"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Optimize AGY read-only sandbox is unavailable") from exc
        if result.returncode:
            raise RuntimeError("Optimize AGY read-only sandbox could not start")

    def close(self):
        # Caller may use this only before process start or after owned closure.
        # A cleanup failure must retain this exact directory for diagnosis.
        if self.directory.exists():
            _exact(self.directory)
            if _identity(self.directory) != tuple(self.plan["control_identity"]):
                raise RuntimeError("Optimize AGY control directory changed")
            shutil.rmtree(self.directory)


def prepare_sandbox(agy: str | Path, cwd: Path, env: dict[str, str]) -> Sandbox:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Optimize AGY read-only turns require Linux Bubblewrap")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("Optimize AGY read-only turns require installed Bubblewrap")
    bwrap = _exact(Path(bwrap).resolve(), directory=False)
    agy = _exact(Path(agy).resolve(), directory=False)
    source = _exact(Path(cwd).resolve())
    if env.get("UNITY_OPTIMIZE_WORKTREE") != str(source):
        raise ValueError("Optimize AGY source binding is missing or changed")
    project = _exact(Path(env.get("UNITY_OPTIMIZE_PROJECT_ROOT", "")))
    unity = _exact(source / ".unity")
    marker = unity / "optimize-worktree.json"
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 16384:
        raise ValueError("Optimize AGY needs a private worktree binding")
    binding = json.loads(marker.read_text())
    if (binding.get("schema") != "optimize-worktree-v1"
            or binding.get("project_root") != str(project) or project == source):
        raise ValueError("Optimize AGY private worktree binding changed")

    writable = []
    log_aliases = []
    for name in _SHARED:
        target = _exact(project / ".unity" / name)
        entry = unity / name
        if not entry.is_symlink() or entry.resolve() != target:
            raise ValueError("Optimize AGY shared runtime binding changed: " + name)
        writable.append(target)
    # Keep existing per-worker service tool/cache locations, not global installs.
    for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "LAKE_CACHE_DIR"):
        if env.get(name):
            path = _exact(Path(env[name]))
            if not path.is_relative_to(project / ".unity" / "tmp"):
                raise ValueError("Optimize AGY service scratch is outside its runtime")

    home = _exact(Path(env.get("HOME", str(Path.home()))).resolve())
    agy_root = home / ".gemini" / "antigravity-cli"
    # The native terminal sandbox has its own filesystem policy. --add-dir is
    # workspace context, not a substitute for its explicit runtime write rules.
    # Require an existing regular settings file; do not create host placeholders.
    settings_path = _exact(agy_root / "settings.json", directory=False)
    settings_data = _settings_bytes(settings_path)
    shell_helper = None
    if agy_root.exists() or agy_root.is_symlink():
        _exact(agy_root)
        for name in _AGY_RUNTIME:
            entry = agy_root / name
            if entry.exists() or entry.is_symlink():
                writable.append(_exact(entry))
        log = agy_root / "cli.log"
        if log.is_symlink():
            # AGY keeps cli.log as a pointer into its existing log directory.
            # That directory is already writable. Do not mount the alias or
            # grant writes to the profile parent (settings and credentials).
            log_directory = _exact(agy_root / "log")
            target = _exact(log.resolve(), directory=False)
            if not target.is_relative_to(log_directory):
                raise ValueError("Optimize AGY log link is outside its log directory")
            link = log.lstat()
            log_aliases.append([str(log), str(target), *_identity(target),
                                link.st_dev, link.st_ino])
        elif log.exists():
            writable.append(_exact(log, directory=False))
        helper = agy_root / "bin" / "agentapi"
        if helper.exists() or helper.is_symlink():
            _exact(helper.parent)
            shell_helper = _exact(helper, directory=False)
            info = shell_helper.stat()
            if info.st_nlink != 1 or info.st_size > 65536:
                raise ValueError("Optimize AGY shell helper is not a private runtime script")
            if not shell_helper.read_bytes().startswith(b"#!"):
                raise ValueError("Optimize AGY shell helper must be a runtime script")

    # Never place control executables in TMPDIR: the worker's normal TMPDIR is
    # a shared writable runtime directory. Bind only runner/tmp, not this parent.
    directory = Path(tempfile.mkdtemp(prefix="uo-agy-", dir="/tmp")).resolve()
    try:
        runner, temporary = directory / "runner", directory / "tmp"
        runner.mkdir(mode=0o700)
        temporary.mkdir(mode=0o700)
        writable.extend((runner, temporary))
        writable = tuple(dict.fromkeys(writable))
        module = Path(__file__).resolve()
        # The bridge's absolute shebang and imports must exist in the inner
        # sandbox as well. These are reads, never runtime/source write grants.
        readable = tuple(dict.fromkeys(_exact(path.resolve()) for path in
                         (Path(sys.prefix), Path(sys.base_prefix), module.parent.parent)))
        private_settings = directory / "agy-settings.json"
        private_settings.write_text(json.dumps(_readonly_settings(settings_data, writable, readable)))
        private_settings.chmod(0o600)
        git = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            cwd=source, capture_output=True, text=True, timeout=10, check=True)
        metadata = tuple(_exact(Path(line)) for line in git.stdout.splitlines())
        if len(metadata) != 2:
            raise ValueError("Optimize AGY Git metadata binding is unavailable")
        packages = source / ".lake" / "packages"
        source_roots = (source, *metadata, *((packages.resolve(),) if packages.exists() else ()))
        overlays = []
        if shell_helper is not None:
            if (any(shell_helper.is_relative_to(root) for root in (project, *source_roots))
                    or any(shell_helper.samefile(path) for path in
                           (agy, bwrap, module, Path(sys.executable).resolve()))):
                raise ValueError("Optimize AGY shell helper overlaps protected files")
            # AGY rewrites this generated script before each run_command. Give
            # it a turn-local inode at that pathname, not the host file or bin.
            private_helper = runner / "agentapi"
            shutil.copyfile(shell_helper, private_helper)
            private_helper.chmod(0o700)
            overlays.append((private_helper, shell_helper))
        protected = tuple(dict.fromkeys((source, project, agy, bwrap, module,
                                         Path(sys.executable).resolve(), home / ".gemini",
                                         home / ".gitconfig", *metadata,
                                         *((packages.resolve(),) if packages.exists() else ()))))
        for root in writable:
            if any(path == root or path.is_relative_to(root) for path in protected):
                raise ValueError("Optimize AGY write grant includes protected paths")
            if any(root.is_relative_to(path) for path in source_roots):
                raise ValueError("Optimize AGY write grant includes source or Git files")
            if root.is_relative_to(project) and root not in tuple(project / ".unity" / name for name in _SHARED):
                raise ValueError("Optimize AGY write grant includes canonical source files")
        prefix = [str(bwrap), "--die-with-parent", "--unshare-user",
                  "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL",
                  "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev"]
        for root in writable:
            prefix.extend(("--bind", str(root), str(root)))
        for private, destination in overlays:
            prefix.extend(("--bind", str(private), str(destination)))
        prefix.extend(("--ro-bind", str(private_settings), str(settings_path)))
        prefix.extend(("--chdir", str(runner), "--setenv", "PWD", str(runner)))
        for key in ("TMPDIR", "TMP", "TEMP"):
            prefix.extend(("--setenv", key, str(temporary)))
        # uvx can retain its installed tools, but a new cache write stays private.
        prefix.extend(("--setenv", "UV_CACHE_DIR", str(temporary / "uv-cache")))
        plan = {"prefix": prefix, "agy": str(agy),
                "settings_snapshots": [[str(settings_path), hashlib.sha256(settings_data).hexdigest()],
                                       [str(private_settings), hashlib.sha256(private_settings.read_bytes()).hexdigest()]],
                "log_aliases": log_aliases,
                "writable": list(map(str, writable)),
                "protected": [str(p) for p in protected if p.exists()],
                "control_identity": list(_identity(directory)),
                "bindings": [[str(p), *_identity(p)] for p in
                             dict.fromkeys((source, project, unity, marker, agy, bwrap, module,
                                            directory, settings_path, private_settings, *writable,
                                            *readable,
                                            *(path for pair in overlays for path in pair),
                                            *(target.parent for _, target in overlays)))]}
        plan_path = directory / "sandbox.json"
        plan_path.write_text(json.dumps(plan))
        plan_path.chmod(0o600)
        executable = directory / "agy-readonly"
        executable.write_text(
            f"#!{sys.executable} -I\nimport os, sys\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, '-I', {str(module)!r}, "
            f"'--launch', {str(plan_path)!r}, *sys.argv[1:]])\n")
        executable.chmod(0o700)
        _validate_plan(plan)
        return Sandbox(directory, runner, temporary, executable, writable, plan)
    except BaseException:
        shutil.rmtree(directory)
        raise


def _main():
    if len(sys.argv) < 3 or sys.argv[1] != "--launch":
        raise SystemExit("Optimize AGY sandbox expects an owned launch plan")
    plan = json.loads(Path(sys.argv[2]).read_text())
    try:
        _validate_plan(plan)
        command = [*plan["prefix"], "--", plan["agy"], *sys.argv[3:]]
        os.execv(command[0], command)
    except (OSError, ValueError):
        # Do not echo paths, provider environment, or native credential diagnostics.
        raise SystemExit("Optimize AGY read-only sandbox binding or startup failed") from None


if __name__ == "__main__":
    _main()
