"""Read-only real-home sentinel and path rebasing for the pytest harness.

Keep this module independent of application imports: isolation must be installed
before collection imports application modules and captures their state paths.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

HASH_LIMIT = 1024 * 1024
# These files change during normal bot polling / diagnostic publishing. Persistent
# registries, cookies, env files, queues and analytics state are NOT exceptions.
RUNTIME_FILES = frozenset({
    "runtime_status.json", "telegram-bot-runtime.json", "telegram-bot-state.json",
    "run_history.jsonl", "analytics_events.jsonl", "telegram-bot-debug.jsonl",
})


def _scope_parts(relative: Path) -> tuple[str, ...]:
    parts = relative.parts
    return parts[2:] if len(parts) >= 3 and parts[0] == "profiles" else parts


def excluded(relative: Path) -> bool:
    """Only root/profile runtime artifacts; unknown state stays protected."""
    if relative.name.endswith(".env"):
        return False
    parts = _scope_parts(relative)
    if parts and parts[0] in {"state", "traces"}:
        return True
    if len(parts) != 1:
        return False
    name = parts[0]
    return (name in RUNTIME_FILES or name.endswith((".pid", ".lock", ".log"))
            or ".log." in name)


def fingerprint_tree(root: Path) -> dict[str, tuple]:
    """Never create directories, lock files, backups or journals in this tree.

    Large non-log files still get size + nanosecond mtime protection. Small
    files also get SHA256, catching same-size edits with a restored mtime.
    Directory metadata is deliberately ignored (live bots create runtime files).
    Linked profile directories are visited too; inode tracking stops cycles.
    """
    result = {}
    if not root.is_dir():
        return result
    visited = set()
    for directory, dirs, files in os.walk(root, followlinks=True):
        parent = Path(directory)
        identity = (parent.stat().st_dev, parent.stat().st_ino)
        if identity in visited:
            dirs.clear()
            continue
        visited.add(identity)
        dirs[:] = sorted(name for name in dirs if not excluded((parent / name).relative_to(root)))
        for name in files:
            path = parent / name
            relative = path.relative_to(root)
            if excluded(relative):
                continue
            try:
                stat = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.st_size <= HASH_LIMIT else None
                link = os.readlink(path) if path.is_symlink() else None
                result[str(relative)] = (stat.st_size, stat.st_mtime_ns, digest, link)
            except FileNotFoundError:
                # Atomic replacement/removal by a live process: a missing file
                # must compare differently from its initial fingerprint.
                result[str(relative)] = ("missing",)
    return result


def changed_files(before: dict, after: dict) -> list[str]:
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def absolute_path(value) -> Path | None:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        return None
    path = Path(value).expanduser()
    return Path(os.path.abspath(path)) if path.is_absolute() else None


class PathRebaser:
    """Resolve root aliases once, rather than statting them for every constant."""

    def __init__(self, roots: tuple[Path, ...], destination: Path):
        self.roots = sorted((*roots, destination), key=lambda item: len(item.parts), reverse=True)
        self.resolved_roots = sorted({root.resolve() for root in self.roots},
                                     key=lambda item: len(item.parts), reverse=True)
        self.destination = destination

    def __call__(self, value):
        path = absolute_path(value)
        if path is None:
            return value
        # Lexical comparison first preserves relative layout and avoids needless
        # filesystem reads. Resolve only for a potential symlink alias.
        for root in self.roots:
            if path.is_relative_to(root):
                target = self.destination / path.relative_to(root)
                return str(target) if isinstance(value, str) else target
        candidate = path.resolve()
        for root in self.resolved_roots:
            if candidate.is_relative_to(root):
                target = self.destination / candidate.relative_to(root)
                return str(target) if isinstance(value, str) else target
        return value


def rebase_path(value, roots: tuple[Path, ...], destination: Path):
    """Component-aware prefix comparison, including resolved symlink aliases.

    A sibling such as ~/.job-hunter-backup is never treated as a child.
    """
    return PathRebaser(roots, destination)(value)


def isolate_paths(module, monkeypatch, roots: tuple[Path, ...], home: Path, *, rebase=None) -> None:
    """Discover paths by VALUE, including future config constants / aliases."""
    rebase = rebase or PathRebaser(roots, home)
    for name, value in list(vars(module).items()):
        if name.startswith("__") or not isinstance(value, (str, os.PathLike)):
            continue
        rebased = rebase(value)
        if rebased != value:
            monkeypatch.setattr(module, name, rebased)


def isolate_environment(monkeypatch, roots: tuple[Path, ...], home: Path, *, rebase=None) -> dict[str, str]:
    """Rebase inherited overrides too, so reloads/subprocesses stay isolated.

    An explicitly configured file outside either state root is isolated as well;
    return its mapping so config attributes with that same value follow it.
    JOB_HUNTER_HOME is removed intentionally: profile tests set config directly,
    and profile._profiles_root otherwise lets the inherited env take precedence.
    HOME supplies a safe ~/.job-hunter fallback, including after config reload.
    """
    overrides = {}
    rebase = rebase or PathRebaser(roots, home)
    for name, value in list(os.environ.items()):
        if not name.startswith("JOB_HUNTER_") or name == "JOB_HUNTER_HOME":
            continue
        rebased = rebase(value)
        if name.endswith(("_FILE", "_DIR")) and value.strip():
            if rebased == value:
                rebased = str(home / "env-overrides" / name / Path(value).name)
            overrides[value] = rebased
        if rebased != value:
            monkeypatch.setenv(name, str(rebased))
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.delenv("JOB_HUNTER_HOME", raising=False)
    return overrides
