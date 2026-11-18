"""An append-only line file under the state directory (``audit.jsonl`` and ``log.jsonl``).

``AppendFile.open`` opens (creating, mode 0600) one file by name relative to the checked state
directory descriptor, with ``O_APPEND | O_NOFOLLOW``. It refuses anything that is not a regular
file owned by the user, with one hard link, and not writable by group or others. The descriptor
is held: every append first checks that the name still leads to the same inode, so a file
renamed away, replaced or swapped for a symlink after the open is noticed instead of silently
written through.
(Between that check and the write a swap is still possible for a process of the same user; the
state directory is 0700, so only the user's own processes can do it.)

One record is one line, written with a single ``write(2)`` of the whole line. On a local file
system ``O_APPEND`` makes the seek-and-write atomic with respect to other appenders, so lines of
concurrent runs never interleave. Unlike a pipe there is no ``PIPE_BUF`` limit that applies, but a
short write or an error in the middle can still leave a torn line: a short write is completed, and
when that fails too the torn line is ended with a newline (best effort) so the next record starts
on its own line and a reader that skips malformed lines loses only the torn one. When even that
fails, the next append starts with a newline inside its own single write. After the write (and the
fsync) the same-file check runs again, so a rename in between is an error, not a success. Lines
are capped at ``MAX_LINE_BYTES`` so one record cannot fill the disk.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any, Final

MAX_LINE_BYTES: Final = 1 << 20  # 1 MiB, newline included
_MAX_SHORT_WRITES: Final = 64


class AppendError(Exception):
    """The file cannot be opened, is unsafe, or an append failed.

    ``details`` has ``path`` and, for an OS error, ``errno`` (its name). ``partial`` is true when
    some but not all bytes of a line reached the file.
    """

    def __init__(
        self,
        message: str,
        path: object,
        exc: OSError | None = None,
        *,
        partial: bool = False,
        written: bool | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = {"path": str(path)}
        if exc is not None and exc.errno is not None:
            self.details["errno"] = errno.errorcode.get(exc.errno, str(exc.errno))
        self.details.update(extra)
        self.partial = partial
        #: Some or all of the line may be in the file (a partial write, or a complete write whose
        #: fsync or final same-file check failed): the record must not be appended again.
        self.written = partial if written is None else written


def refuse_unsafe(fd: int, path: object) -> None:
    """Raise ``AppendError`` unless ``fd`` is a regular file owned by the user with one hard link
    that group and others cannot write. Readers (``export``) use it too."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise AppendError("not a regular file", path)
    if st.st_uid != os.geteuid():
        raise AppendError(f"owner is uid {st.st_uid}, not the current user", path)
    if stat.S_IMODE(st.st_mode) & 0o022:
        raise AppendError("writable by group or others", path)
    if st.st_nlink != 1:
        raise AppendError("the file has more than one hard link", path)


class AppendFile:
    """An open append-only file; close it when done."""

    def __init__(self, fd: int, dir_fd: int, name: str, path: Path) -> None:
        self._fd = fd
        self._dir_fd = dir_fd
        self._name = name
        self._path = path
        self._closed = False
        self._torn = False  # the file may end in a fragment without a newline

    @property
    def fd(self) -> int:
        return self._fd

    @classmethod
    def open(cls, state_fd: int, name: str, path: Path) -> AppendFile:
        """Open ``name`` below ``state_fd`` for append, creating it 0600. Raises ``AppendError``."""
        if not name or "/" in name or name in (".", ".."):
            raise ValueError(f"not a single path component: {name!r}")
        base = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        fd = -1
        created = False
        try:
            for _ in range(3):  # the file may be removed or created between the two opens
                try:
                    fd = os.open(name, base | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=state_fd)
                    created = True
                    break
                except FileExistsError:
                    pass
                try:
                    fd = os.open(name, base, dir_fd=state_fd)
                    break
                except FileNotFoundError:
                    continue
            if fd < 0:
                raise AppendError("the file keeps changing while it is opened", path)
        except OSError as exc:
            raise AppendError(
                "cannot open for append (a symlink, or no access)", path, exc
            ) from exc
        try:
            if created:
                os.fchmod(fd, 0o600)  # a umask can only narrow the mode; make it exact
            refuse_unsafe(fd, path)
            dir_fd = os.dup(state_fd)
        except BaseException as exc:
            os.close(fd)
            if isinstance(exc, OSError):
                raise AppendError("cannot check the file", path, exc) from exc
            raise
        return cls(fd, dir_fd, name, path)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self._fd, self._dir_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _check_same_file(self) -> None:
        try:
            mine = os.fstat(self._fd)
            there = os.stat(self._name, dir_fd=self._dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise AppendError("the file is gone or cannot be checked", self._path, exc) from exc
        if (mine.st_dev, mine.st_ino) != (there.st_dev, there.st_ino) or not stat.S_ISREG(
            there.st_mode
        ):
            raise AppendError("the file was replaced or moved after it was opened", self._path)

    def append(self, line: bytes, *, sync: bool = False) -> None:
        """Append one complete line (exactly one trailing newline, no other) and optionally fsync.

        Raises ``AppendError``; ``partial`` says whether any of the line may be in the file.
        """
        if not isinstance(line, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"line must be bytes, got {type(line).__name__}")
        if self._closed:
            raise AppendError("the file is closed", self._path)
        if line.count(b"\n") != 1 or not line.endswith(b"\n") or len(line) < 2:
            raise AppendError("a record is exactly one non-empty line", self._path)
        if len(line) > MAX_LINE_BYTES:
            raise AppendError(
                f"record is larger than {MAX_LINE_BYTES} bytes", self._path, too_large=True
            )
        prefix = b"\n" if self._torn else b""  # ends a torn fragment in the same single write
        data = prefix + line
        if len(data) > MAX_LINE_BYTES:
            raise AppendError(
                f"record is larger than {MAX_LINE_BYTES} bytes", self._path, too_large=True
            )
        self._check_same_file()
        written = 0
        shorts = 0
        while written < len(data):
            try:
                count = os.write(self._fd, data[written:])
            except OSError as exc:
                self._after_failure(written, len(prefix))
                raise AppendError(
                    "append failed", self._path, exc, partial=written > len(prefix)
                ) from exc
            if count <= 0:
                self._after_failure(written, len(prefix))
                raise AppendError("append wrote nothing", self._path, partial=written > len(prefix))
            written += count
            if written < len(data):
                shorts += 1
                if shorts > _MAX_SHORT_WRITES:
                    self._after_failure(written, len(prefix))
                    raise AppendError("too many short writes", self._path, partial=True)
        self._torn = False
        if sync:
            try:
                os.fsync(self._fd)
            except OSError as exc:
                raise AppendError("fsync failed", self._path, exc, written=True) from exc
        try:
            self._check_same_file()
        except AppendError as exc:
            # The line was written; it may sit in the moved or replaced file. Never retry it.
            raise AppendError(
                "the file was replaced or moved while the record was written",
                self._path,
                partial=True,
                **{k: v for k, v in exc.details.items() if k != "path"},
            ) from exc

    def _after_failure(self, written: int, prefix_len: int) -> None:
        """Bookkeeping after a failed write of ``written`` bytes (the prefix counts first)."""
        if written == 0:
            return
        if written == prefix_len:  # only the leading newline landed: the file ends on a line
            self._torn = False
            return
        self._torn = not self._end_torn_line()

    def _end_torn_line(self) -> bool:
        try:
            os.write(self._fd, b"\n")
        except OSError:
            return False
        return True
