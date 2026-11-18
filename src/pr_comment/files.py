"""State and temporary directory safety (docs/interface.md: Files).

Everything here works on directory file descriptors: the state directory and its ``tmp/`` are
opened once with ``O_NOFOLLOW | O_DIRECTORY`` and checked with ``fstat`` on the descriptor (so a
swap after the check changes nothing), and every later operation is relative to those
descriptors (``dir_fd=``). Nothing below follows a symlink or leaves the directory it was given.

A refusal raises ``StateUnsafeError``; the caller turns it into the exit 12 ``state_unsafe``
envelope before any forgectl call. Failing to remove a run's own hook directory afterwards also
raises it; the caller should log a warning rather than change the command's result.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

STATE_DIR_NAME: Final = "pr-comment"
TMP_DIR_NAME: Final = "tmp"
HOOK_PREFIX: Final = "prc-hook-"
HOOK_NAME_RE: Final = re.compile(r"prc-hook-[0-9a-f]{32}")
STALE_AFTER_SECONDS: Final = 86400.0
MAX_REMOVE_DEPTH: Final = 64
MAX_NAME_BYTES: Final = 255
_HOOK_NAME_ATTEMPTS: Final = 8

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class StateUnsafeError(Exception):
    """The state or temporary directory is not safe to use (exit 12 ``state_unsafe``)."""

    def __init__(self, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = dict(details or {})


def _unsafe(message: str, path: object, exc: OSError | None = None) -> StateUnsafeError:
    details: dict[str, Any] = {"path": str(path)}
    if exc is not None and exc.errno is not None:
        details["errno"] = errno.errorcode.get(exc.errno, str(exc.errno))
    return StateUnsafeError(f"{message}: {path}", details)


def _check_name(name: str) -> None:
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\0" in name
        or len(os.fsencode(name)) > MAX_NAME_BYTES
    ):
        raise ValueError(f"not a single safe path component: {name!r}")


def _base_dir(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_STATE_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg)
    home = env.get("HOME", "")
    if not home:
        home = str(Path.home())
    if not os.path.isabs(home):
        raise StateUnsafeError(f"home directory is not absolute: {home!r}", {"path": home})
    return Path(home) / ".local" / "state"


def state_dir_path(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/pr-comment``, default ``~/.local/state/pr-comment``.

    An empty or relative ``XDG_STATE_HOME`` is ignored, as the XDG specification says.
    """
    return _base_dir(os.environ if env is None else env) / STATE_DIR_NAME


def _mkdir_exact(name: str, mode: int, dir_fd: int) -> None:
    """``mkdirat`` that the umask cannot make unusable (a umask may only add restrictions)."""
    old = os.umask(0)
    try:
        os.mkdir(name, mode, dir_fd=dir_fd)
    finally:
        os.umask(old)


def _open_checked(parent_fd: int, name: str, path: Path, uid: int, *, create: bool) -> int:
    """Open ``name`` below ``parent_fd`` without following links and check it; return the fd."""
    created = False
    try:
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                _mkdir_exact(name, 0o700, parent_fd)
                created = True
            except FileExistsError:
                pass
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _unsafe(
            "cannot open directory safely (a symlink, not a directory, or no access)", path, exc
        ) from exc
    try:
        if created:
            os.fchmod(fd, 0o700)  # the umask may have cut it; we made it, so make it exact
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise _unsafe("not a directory", path)
        if st.st_uid != uid:
            raise _unsafe(f"owner is uid {st.st_uid}, not the current user", path)
        mode = stat.S_IMODE(st.st_mode)
        if mode != 0o700:
            raise _unsafe(f"mode is {mode:04o}, not 0700", path)
    except BaseException as exc:
        os.close(fd)
        if isinstance(exc, OSError):
            raise _unsafe("cannot check directory", path, exc) from exc
        raise
    return fd


@dataclass
class StateDirs:
    """The opened state directory and its ``tmp/``; close it (or use ``with``) when done."""

    state_path: Path
    tmp_path: Path
    state_fd: int
    tmp_fd: int
    _closed: bool = field(default=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self.tmp_fd, self.state_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> StateDirs:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def sweep(self, *, now: float | None = None) -> list[str]:
        return sweep_stale_hook_dirs(self.tmp_fd, now=now)


def open_state_dirs(env: Mapping[str, str] | None = None, *, uid: int | None = None) -> StateDirs:
    """Open (creating with mode 0700 when missing) and check the state directory and ``tmp/``.

    ``uid`` is the expected owner (default: the effective user); tests pass another one to
    simulate a directory owned by someone else. Raises ``StateUnsafeError``.
    """
    expected_uid = os.geteuid() if uid is None else uid
    state_path = state_dir_path(env)
    base = state_path.parent
    old_umask = os.umask(0o077)  # created parents get 0700 whatever the caller's umask
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise _unsafe("cannot open the state base directory", base, exc) from exc
    finally:
        os.umask(old_umask)
    try:
        state_fd = _open_checked(base_fd, STATE_DIR_NAME, state_path, expected_uid, create=True)
    finally:
        os.close(base_fd)
    try:
        tmp_path = state_path / TMP_DIR_NAME
        tmp_fd = _open_checked(state_fd, TMP_DIR_NAME, tmp_path, expected_uid, create=True)
    except BaseException:
        os.close(state_fd)
        raise
    return StateDirs(state_path, tmp_path, state_fd, tmp_fd)


def create_file_exclusive(dir_fd: int, name: str, mode: int = 0o600) -> int:
    """Create a new file ``name`` below ``dir_fd`` (never an existing entry or a link).

    Returns a write-only descriptor; raises ``FileExistsError`` for anything already there,
    including a symlink (dangling or not).
    """
    _check_name(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, mode, dir_fd=dir_fd)
    try:
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def create_dir_exclusive(dir_fd: int, name: str, mode: int = 0o700) -> int:
    """Create a new directory ``name`` below ``dir_fd`` and return a descriptor for it."""
    _check_name(name)
    _mkdir_exact(name, mode, dir_fd)
    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


@dataclass
class HookDir:
    """A run's private hook working directory."""

    name: str
    path: Path
    fd: int
    _closed: bool = field(default=False, repr=False)

    def remove(self, tmp_fd: int) -> None:
        """Close the descriptor and delete the directory (``remove_tree_at``)."""
        if not self._closed:
            self._closed = True
            try:
                os.close(self.fd)
            except OSError:
                pass
        remove_tree_at(tmp_fd, self.name)


def create_hook_dir(tmp_fd: int, tmp_path: Path) -> HookDir:
    """Create ``prc-hook-<32 random hex>`` with mode 0700 below the opened ``tmp/``."""
    for _ in range(_HOOK_NAME_ATTEMPTS):
        name = HOOK_PREFIX + secrets.token_hex(16)
        try:
            fd = create_dir_exclusive(tmp_fd, name, 0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise _unsafe("cannot create a hook directory", tmp_path / name, exc) from exc
        return HookDir(name, tmp_path / name, fd)
    raise StateUnsafeError("cannot find an unused hook directory name", {"path": str(tmp_path)})


def _remove(parent_fd: int, name: str, depth: int) -> None:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)  # a file or a symlink: removes the link itself
        return
    if depth >= MAX_REMOVE_DEPTH:
        raise StateUnsafeError(
            f"directory tree is nested deeper than {MAX_REMOVE_DEPTH}; left in place",
            {"path": name},
        )
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            # Replaced by a link or file after the check: remove that entry, never follow it.
            os.unlink(name, dir_fd=parent_fd)
            return
        raise
    try:
        for entry in os.listdir(fd):
            _remove(fd, entry, depth + 1)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def remove_tree_at(parent_fd: int, name: str) -> None:
    """Delete ``name`` below ``parent_fd`` with an ``unlinkat`` walk that never follows links.

    Symlinks (and any non-directory) are unlinked themselves; directories are entered only
    through descriptors opened with ``O_NOFOLLOW``. A missing entry is fine. Raises
    ``ValueError`` for a name that is not one path component and ``StateUnsafeError`` when
    something cannot be removed.
    """
    _check_name(name)
    try:
        _remove(parent_fd, name, 0)
    except OSError as exc:
        raise _unsafe("cannot remove", name, exc) from exc


def sweep_stale_hook_dirs(
    tmp_fd: int, *, now: float | None = None, max_age: float = STALE_AFTER_SECONDS
) -> list[str]:
    """``rmdir`` the ``prc-hook-<32 hex>`` directories older than a day; return their names.

    Only real directories directly in ``tmp_fd`` with exactly that name are considered, and
    ``rmdir`` itself refuses a non-empty one, so nothing else can be removed.
    """
    current = time.time() if now is None else now
    removed: list[str] = []
    try:
        names = os.listdir(tmp_fd)
    except OSError:
        return removed
    for name in sorted(names):
        if not HOOK_NAME_RE.fullmatch(name):
            continue
        try:
            st = os.stat(name, dir_fd=tmp_fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or current - st.st_mtime <= max_age:
                continue
            os.rmdir(name, dir_fd=tmp_fd)
        except OSError:
            continue
        removed.append(name)
    return removed
