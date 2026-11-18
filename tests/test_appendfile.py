"""The append-only file under the state directory (issue #11): no-follow, regular file, 0600."""

from __future__ import annotations

import errno
import multiprocessing
import os
import stat
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from pr_comment import appendfile, files
from pr_comment.appendfile import AppendError, AppendFile


@pytest.fixture
def dirs(tmp_path: Path) -> Iterator[files.StateDirs]:
    env = {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "xdg")}
    (tmp_path / "home").mkdir()
    with files.open_state_dirs(env) as opened:
        yield opened


def target(dirs: files.StateDirs, name: str = "audit.jsonl") -> Path:
    return dirs.state_path / name


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def os_proxy(**overrides: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(**{**vars(os), **overrides})


def open_file(dirs: files.StateDirs, name: str = "audit.jsonl") -> AppendFile:
    return AppendFile.open(dirs.state_fd, name, dirs.state_path / name)


def test_creates_the_file_with_mode_0600_even_under_a_loose_umask(dirs: files.StateDirs) -> None:
    old = os.umask(0)
    try:
        f = open_file(dirs)
    finally:
        os.umask(old)
    f.close()
    assert mode_of(target(dirs)) == 0o600


def test_creates_the_file_0600_under_a_restrictive_umask(dirs: files.StateDirs) -> None:
    old = os.umask(0o277)
    try:
        f = open_file(dirs)
    finally:
        os.umask(old)
    f.close()
    assert mode_of(target(dirs)) == 0o600


def test_appends_lines_in_order_and_keeps_existing_content(dirs: files.StateDirs) -> None:
    target(dirs).write_bytes(b'{"old":1}\n')
    os.chmod(target(dirs), 0o600)
    f = open_file(dirs)
    f.append(b'{"a":1}\n')
    f.append(b'{"b":2}\n')
    f.close()
    assert target(dirs).read_bytes() == b'{"old":1}\n{"a":1}\n{"b":2}\n'


def test_an_existing_file_keeps_its_mode(dirs: files.StateDirs) -> None:
    target(dirs).write_bytes(b"")
    os.chmod(target(dirs), 0o640)
    open_file(dirs).close()
    assert mode_of(target(dirs)) == 0o640


def test_a_symlink_is_refused_and_not_followed(dirs: files.StateDirs, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep\n")
    target(dirs).symlink_to(victim)
    with pytest.raises(AppendError) as info:
        open_file(dirs)
    assert info.value.details["path"] == str(target(dirs))
    assert victim.read_bytes() == b"keep\n"


def test_a_dangling_symlink_is_refused_and_nothing_is_created(
    dirs: files.StateDirs, tmp_path: Path
) -> None:
    target(dirs).symlink_to(tmp_path / "missing")
    with pytest.raises(AppendError):
        open_file(dirs)
    assert not (tmp_path / "missing").exists()


def test_a_directory_is_refused(dirs: files.StateDirs) -> None:
    target(dirs).mkdir()
    with pytest.raises(AppendError):
        open_file(dirs)


def test_a_fifo_is_refused_without_blocking(dirs: files.StateDirs) -> None:
    os.mkfifo(target(dirs), 0o600)
    reader = os.open(target(dirs), os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(AppendError):
            open_file(dirs)
    finally:
        os.close(reader)


def test_a_fifo_without_a_reader_is_refused_without_blocking(dirs: files.StateDirs) -> None:
    os.mkfifo(target(dirs), 0o600)
    with pytest.raises(AppendError):
        open_file(dirs)


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666, 0o660, 0o606])
def test_a_group_or_world_writable_file_is_refused(dirs: files.StateDirs, mode: int) -> None:
    target(dirs).write_bytes(b"")
    os.chmod(target(dirs), mode)
    with pytest.raises(AppendError):
        open_file(dirs)


def test_a_file_owned_by_someone_else_is_refused(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    target(dirs).write_bytes(b"")
    os.chmod(target(dirs), 0o600)
    monkeypatch.setattr(appendfile, "os", os_proxy(geteuid=lambda: os.geteuid() + 1))
    with pytest.raises(AppendError):
        open_file(dirs)


def test_an_unwritable_file_is_refused(dirs: files.StateDirs) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    target(dirs).write_bytes(b"")
    os.chmod(target(dirs), 0o400)
    with pytest.raises(AppendError) as info:
        open_file(dirs)
    assert info.value.details["errno"] == "EACCES"


def test_a_file_swapped_for_a_symlink_after_the_open_is_noticed(
    dirs: files.StateDirs, tmp_path: Path
) -> None:
    f = open_file(dirs)
    f.append(b'{"a":1}\n')
    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    target(dirs).unlink()
    target(dirs).symlink_to(victim)
    with pytest.raises(AppendError):
        f.append(b'{"b":2}\n')
    assert victim.read_bytes() == b""


def test_a_file_renamed_away_after_the_open_is_noticed(dirs: files.StateDirs) -> None:
    f = open_file(dirs)
    os.rename(target(dirs), dirs.state_path / "audit.old")
    with pytest.raises(AppendError):
        f.append(b'{"a":1}\n')
    assert (dirs.state_path / "audit.old").read_bytes() == b""


def test_a_file_replaced_by_another_regular_file_is_noticed(dirs: files.StateDirs) -> None:
    f = open_file(dirs)
    target(dirs).unlink()
    target(dirs).write_bytes(b"")
    with pytest.raises(AppendError):
        f.append(b'{"a":1}\n')
    assert target(dirs).read_bytes() == b""


@pytest.mark.parametrize(
    "line", [b"", b"no newline", b"two\nlines\n", b"\n\n", b'{"a":1}\r\n\n', "text\n"]
)
def test_only_one_complete_line_is_accepted(dirs: files.StateDirs, line: bytes) -> None:
    f = open_file(dirs)
    with pytest.raises((AppendError, TypeError)):
        f.append(line)
    assert target(dirs).read_bytes() == b""


def test_a_huge_line_is_refused_before_anything_is_written(dirs: files.StateDirs) -> None:
    f = open_file(dirs)
    line = b'{"x":"' + b"a" * appendfile.MAX_LINE_BYTES + b'"}\n'
    with pytest.raises(AppendError) as info:
        f.append(line)
    assert info.value.details.get("too_large") is True
    assert target(dirs).read_bytes() == b""


def test_the_largest_allowed_line_is_written_whole(dirs: files.StateDirs) -> None:
    f = open_file(dirs)
    line = b"a" * (appendfile.MAX_LINE_BYTES - 1) + b"\n"
    f.append(line)
    assert target(dirs).read_bytes() == line


def test_a_full_disk_before_any_byte_leaves_the_file_untouched(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = open_file(dirs)
    f.append(b'{"a":1}\n')

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(appendfile, "os", os_proxy(write=full))
    with pytest.raises(AppendError) as info:
        f.append(b'{"b":2}\n')
    assert info.value.details["errno"] == "ENOSPC"
    assert info.value.partial is False
    assert target(dirs).read_bytes() == b'{"a":1}\n'


def test_a_short_write_is_completed(dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    f = open_file(dirs)
    real = os.write

    def short(fd: int, data: bytes) -> int:
        return real(fd, bytes(data)[:3])

    monkeypatch.setattr(appendfile, "os", os_proxy(write=short))
    f.append(b'{"a":1}\n')
    assert target(dirs).read_bytes() == b'{"a":1}\n'


def test_a_torn_write_is_terminated_so_the_next_record_starts_a_new_line(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = open_file(dirs)
    real = os.write
    calls = {"n": 0}

    def torn(fd: int, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real(fd, bytes(data)[:4])
        if calls["n"] == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(appendfile, "os", os_proxy(write=torn))
    with pytest.raises(AppendError) as info:
        f.append(b'{"a":1}\n')
    assert info.value.partial is True
    f.append(b'{"b":2}\n')
    assert target(dirs).read_bytes() == b'{"a"\n{"b":2}\n'


def test_a_failed_fsync_is_an_error(dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    f = open_file(dirs)

    def bad(fd: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(appendfile, "os", os_proxy(fsync=bad))
    with pytest.raises(AppendError) as info:
        f.append(b'{"a":1}\n', sync=True)
    assert info.value.details["errno"] == "EIO"
    assert info.value.partial is False


def test_sync_calls_fsync_and_no_sync_does_not(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(appendfile, "os", os_proxy(fsync=calls.append))
    f = open_file(dirs)
    f.append(b'{"a":1}\n')
    assert calls == []
    f.append(b'{"b":2}\n', sync=True)
    assert len(calls) == 1


def test_the_descriptor_is_not_inherited_and_close_is_idempotent(dirs: files.StateDirs) -> None:
    f = open_file(dirs)
    fd = f.fd
    assert not os.get_inheritable(fd)
    f.close()
    f.close()
    with pytest.raises(OSError):
        os.fstat(fd)


def test_appending_after_close_is_an_error(dirs: files.StateDirs) -> None:
    f = open_file(dirs)
    f.close()
    with pytest.raises(AppendError):
        f.append(b'{"a":1}\n')


def _worker(state_fd_path: str, name: str, tag: int, count: int, size: int) -> None:
    fd = os.open(state_fd_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        f = AppendFile.open(fd, name, Path(state_fd_path) / name)
        for i in range(count):
            f.append(f'{{"tag":{tag},"i":{i},"pad":"{"x" * size}"}}\n'.encode())
        f.close()
    finally:
        os.close(fd)


def test_concurrent_processes_never_interleave_lines(dirs: files.StateDirs) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_worker, args=(str(dirs.state_path), "audit.jsonl", tag, 40, 20000))
        for tag in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    lines = target(dirs).read_bytes().split(b"\n")
    assert lines.pop() == b""
    assert len(lines) == 160
    import json

    seen = {(d["tag"], d["i"]) for d in map(json.loads, lines)}
    assert len(seen) == 160


def test_a_torn_line_that_cannot_be_ended_gets_a_newline_before_the_next_record(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = open_file(dirs)
    real = os.write
    state = {"n": 0, "broken": True}

    def torn(fd: int, data: bytes) -> int:
        state["n"] += 1
        if state["n"] == 1:
            return real(fd, bytes(data)[:4])
        if state["broken"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(appendfile, "os", os_proxy(write=torn))
    with pytest.raises(AppendError) as info:
        f.append(b'{"a":1}\n')
    assert info.value.partial is True
    assert target(dirs).read_bytes() == b'{"a"'
    state["broken"] = False
    f.append(b'{"b":2}\n')
    f.append(b'{"c":3}\n')
    assert target(dirs).read_bytes() == b'{"a"\n{"b":2}\n{"c":3}\n'


def test_the_repair_newline_counts_in_the_size_cap(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = open_file(dirs)
    f._torn = True  # pyright: ignore[reportPrivateUsage]
    line = b"a" * (appendfile.MAX_LINE_BYTES - 1) + b"\n"
    with pytest.raises(AppendError) as info:
        f.append(line)
    assert info.value.details.get("too_large") is True
    assert target(dirs).read_bytes() == b""


def test_a_rename_after_the_write_is_reported_as_a_landed_partial(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = open_file(dirs)
    real = os.write

    def write_then_rename(fd: int, data: bytes) -> int:
        count = real(fd, data)
        os.rename(target(dirs), dirs.state_path / "audit.old")
        return count

    monkeypatch.setattr(appendfile, "os", os_proxy(write=write_then_rename))
    with pytest.raises(AppendError) as info:
        f.append(b'{"a":1}\n')
    assert info.value.partial is True
    assert (dirs.state_path / "audit.old").read_bytes() == b'{"a":1}\n'


def test_a_failed_fsync_marks_the_line_as_possibly_written(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = open_file(dirs)

    def bad(fd: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(appendfile, "os", os_proxy(fsync=bad))
    with pytest.raises(AppendError) as info:
        f.append(b'{"a":1}\n', sync=True)
    assert info.value.written is True


def test_a_file_with_a_second_hard_link_is_refused(dirs: files.StateDirs, tmp_path: Path) -> None:
    target(dirs).write_bytes(b"")
    os.chmod(target(dirs), 0o600)
    os.link(target(dirs), tmp_path / "other-name")
    with pytest.raises(AppendError, match="hard link"):
        open_file(dirs)
