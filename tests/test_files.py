"""State and temporary directory safety (docs/interface.md: Files).

Every test runs against a private directory under ``tmp_path`` through an explicit environment
mapping, so nothing here can touch the real state directory.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from pr_comment import files
from pr_comment.files import StateUnsafeError


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home), "XDG_STATE_HOME": str(tmp_path / "xdg")}


@pytest.fixture
def state_path(env: dict[str, str]) -> Path:
    return Path(env["XDG_STATE_HOME"]) / "pr-comment"


@pytest.fixture
def umask0() -> Iterator[None]:
    old = os.umask(0)
    try:
        yield
    finally:
        os.umask(old)


def os_proxy(**overrides: object) -> types.SimpleNamespace:
    """A stand-in for ``os`` seen only by ``pr_comment.files`` (patch ``files.os``)."""
    return types.SimpleNamespace(**{**vars(os), **overrides})


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def open_dirs(env: dict[str, str], **kw: object) -> files.StateDirs:
    return files.open_state_dirs(env, **kw)  # type: ignore[arg-type]


def make_safe(state_path: Path) -> Path:
    """A state directory with a 0700 tmp/, as a previous run would leave it."""
    tmp = state_path / "tmp"
    tmp.mkdir(parents=True)
    os.chmod(state_path, 0o700)
    os.chmod(tmp, 0o700)
    return tmp


# --- locating the directory -------------------------------------------------------------------


def test_state_path_uses_xdg_state_home(env: dict[str, str]) -> None:
    assert files.state_dir_path(env) == Path(env["XDG_STATE_HOME"]) / "pr-comment"


def test_state_path_defaults_under_home(env: dict[str, str]) -> None:
    del env["XDG_STATE_HOME"]
    assert files.state_dir_path(env) == Path(env["HOME"]) / ".local" / "state" / "pr-comment"


@pytest.mark.parametrize("value", ["", "relative/dir", "."])
def test_relative_or_empty_xdg_state_home_is_ignored(env: dict[str, str], value: str) -> None:
    env["XDG_STATE_HOME"] = value
    assert files.state_dir_path(env) == Path(env["HOME"]) / ".local" / "state" / "pr-comment"


# --- opening and creating ---------------------------------------------------------------------


def test_first_run_creates_both_directories_with_mode_0700(
    env: dict[str, str], state_path: Path, umask0: None
) -> None:
    with open_dirs(env) as dirs:
        assert dirs.state_path == state_path
        assert dirs.tmp_path == state_path / "tmp"
    assert mode_of(state_path) == 0o700
    assert mode_of(state_path / "tmp") == 0o700


def test_creation_is_0700_even_under_a_restrictive_umask(
    env: dict[str, str], state_path: Path
) -> None:
    old = os.umask(0o277)
    try:
        with open_dirs(env):
            pass
    finally:
        os.umask(old)
    assert mode_of(state_path) == 0o700
    assert mode_of(state_path / "tmp") == 0o700


def test_creation_is_0700_under_umask_077_and_the_process_umask_is_untouched(
    env: dict[str, str], state_path: Path
) -> None:
    old = os.umask(0o077)
    try:
        with open_dirs(env):
            pass
        assert os.umask(0o077) == 0o077
    finally:
        os.umask(old)
    assert mode_of(state_path) == 0o700
    assert mode_of(state_path.parent) == 0o700


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open mode 0 directories")
def test_umask_777_surfaces_as_state_unsafe_not_a_hang(env: dict[str, str]) -> None:
    old = os.umask(0o777)
    try:
        with pytest.raises(StateUnsafeError):
            open_dirs(env)
        assert os.umask(old) == 0o777
    finally:
        os.umask(old)


def test_files_module_never_calls_umask() -> None:
    assert "umask(" not in Path(files.__file__).read_text()


def test_missing_parents_are_created_one_by_one_0700(env: dict[str, str]) -> None:
    env["XDG_STATE_HOME"] = str(Path(env["HOME"]) / "a" / "b" / "c")
    old = os.umask(0o022)
    try:
        with open_dirs(env):
            pass
    finally:
        os.umask(old)
    for sub in ("a", "a/b", "a/b/c"):
        assert mode_of(Path(env["HOME"]) / sub) == 0o700


def test_second_run_reopens_the_same_directories(env: dict[str, str], state_path: Path) -> None:
    with open_dirs(env):
        pass
    with open_dirs(env) as dirs:
        assert os.fstat(dirs.tmp_fd).st_ino == os.lstat(state_path / "tmp").st_ino


def test_default_home_chain_is_created(env: dict[str, str]) -> None:
    del env["XDG_STATE_HOME"]
    with open_dirs(env) as dirs:
        assert dirs.state_path == Path(env["HOME"]) / ".local" / "state" / "pr-comment"
        assert dirs.state_path.is_dir()


def test_descriptors_are_closed_on_exit(env: dict[str, str]) -> None:
    with open_dirs(env) as dirs:
        fds = (dirs.state_fd, dirs.tmp_fd)
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_descriptors_are_not_inherited_by_children(env: dict[str, str]) -> None:
    with open_dirs(env) as dirs:
        assert not os.get_inheritable(dirs.state_fd)
        assert not os.get_inheritable(dirs.tmp_fd)


def test_stale_other_files_in_state_dir_are_left_alone(
    env: dict[str, str], state_path: Path
) -> None:
    make_safe(state_path)
    (state_path / "audit.jsonl").write_text("x")
    with open_dirs(env):
        pass
    assert (state_path / "audit.jsonl").read_text() == "x"


# --- unsafe layouts ---------------------------------------------------------------------------


def expect_unsafe(env: dict[str, str], **kw: object) -> StateUnsafeError:
    with pytest.raises(StateUnsafeError) as info:
        open_dirs(env, **kw)
    return info.value


def test_tmp_as_symlink_is_state_unsafe_and_not_followed(
    env: dict[str, str], state_path: Path, tmp_path: Path
) -> None:
    make_safe(state_path)
    (state_path / "tmp").rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    os.symlink(elsewhere, state_path / "tmp")
    expect_unsafe(env)
    assert os.listdir(elsewhere) == []
    assert (state_path / "tmp").is_symlink()


def test_dangling_symlink_tmp_is_state_unsafe_and_not_created_through(
    env: dict[str, str], state_path: Path, tmp_path: Path
) -> None:
    make_safe(state_path)
    (state_path / "tmp").rmdir()
    os.symlink(tmp_path / "nowhere", state_path / "tmp")
    expect_unsafe(env)
    assert not (tmp_path / "nowhere").exists()


def test_state_dir_as_symlink_is_state_unsafe(
    env: dict[str, str], state_path: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "tmp").mkdir(parents=True)
    os.chmod(elsewhere, 0o700)
    os.chmod(elsewhere / "tmp", 0o700)
    state_path.parent.mkdir(parents=True)
    os.symlink(elsewhere, state_path)
    expect_unsafe(env)
    assert os.listdir(elsewhere / "tmp") == []


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o701, 0o777, 0o770, 0o500, 0o600])
def test_tmp_with_wrong_mode_is_state_unsafe(
    env: dict[str, str], state_path: Path, mode: int
) -> None:
    tmp = make_safe(state_path)
    os.chmod(tmp, mode)
    err = expect_unsafe(env)
    assert err.details["path"] == str(tmp)
    assert mode_of(tmp) == mode  # not silently repaired
    os.chmod(tmp, 0o700)


@pytest.mark.parametrize("mode", [0o755, 0o710, 0o777])
def test_state_dir_with_wrong_mode_is_state_unsafe(
    env: dict[str, str], state_path: Path, mode: int
) -> None:
    make_safe(state_path)
    os.chmod(state_path, mode)
    expect_unsafe(env)
    assert mode_of(state_path) == mode
    os.chmod(state_path, 0o700)


def test_setuid_style_extra_mode_bits_are_state_unsafe(
    env: dict[str, str], state_path: Path
) -> None:
    tmp = make_safe(state_path)
    os.chmod(tmp, 0o700 | stat.S_ISGID)
    expect_unsafe(env)
    os.chmod(tmp, 0o700)


def test_tmp_owned_by_another_user_is_state_unsafe(env: dict[str, str], state_path: Path) -> None:
    make_safe(state_path)
    err = expect_unsafe(env, uid=os.geteuid() + 1)
    assert "owner" in str(err)


def test_state_dir_owned_by_another_user_is_state_unsafe(
    env: dict[str, str], state_path: Path
) -> None:
    make_safe(state_path)
    expect_unsafe(env, uid=os.geteuid() + 1)


def test_tmp_that_is_a_regular_file_is_state_unsafe(env: dict[str, str], state_path: Path) -> None:
    make_safe(state_path)
    (state_path / "tmp").rmdir()
    (state_path / "tmp").write_text("x")
    expect_unsafe(env)


def test_tmp_that_is_a_fifo_is_state_unsafe_without_blocking(
    env: dict[str, str], state_path: Path
) -> None:
    make_safe(state_path)
    (state_path / "tmp").rmdir()
    os.mkfifo(state_path / "tmp")
    expect_unsafe(env)


def test_unreadable_tmp_is_state_unsafe(env: dict[str, str], state_path: Path) -> None:
    tmp = make_safe(state_path)
    os.chmod(tmp, 0o000)
    try:
        expect_unsafe(env)
    finally:
        os.chmod(tmp, 0o700)


def test_unsafe_state_leaves_no_descriptor_open(env: dict[str, str], state_path: Path) -> None:
    tmp = make_safe(state_path)
    os.chmod(tmp, 0o755)
    before = set(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    for _ in range(5):
        expect_unsafe(env)
    if before is not None:
        assert set(os.listdir("/proc/self/fd")) == before
    os.chmod(tmp, 0o700)


def test_state_unsafe_maps_to_the_exit_12_envelope(env: dict[str, str], state_path: Path) -> None:
    from pr_comment.output import error_outcome

    tmp = make_safe(state_path)
    os.chmod(tmp, 0o755)
    err = expect_unsafe(env)
    outcome = error_outcome("state_unsafe", str(err), details=err.details)
    assert outcome.exit_code == 12
    assert outcome.document is not None
    error = outcome.document["error"]
    assert error["code"] == "state_unsafe"
    assert (error["landed"], error["retry"]) == (False, "same_command")
    os.chmod(tmp, 0o700)


def test_state_unsafe_details_are_an_absolute_path_and_a_symbolic_errno(
    env: dict[str, str], state_path: Path
) -> None:
    tmp = make_safe(state_path)
    os.chmod(tmp, 0o000)
    try:
        err = expect_unsafe(env)
    finally:
        os.chmod(tmp, 0o700)
    assert set(err.details) <= {"path", "errno"}
    assert os.path.isabs(err.details["path"])
    assert err.details["errno"] == "EACCES"


def test_base_directory_that_is_a_file_is_state_unsafe(env: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "xdg").write_text("x")
    expect_unsafe(env)


# --- exclusive creation -----------------------------------------------------------------------


@pytest.fixture
def tmp_fd(env: dict[str, str]) -> Iterator[int]:
    with open_dirs(env) as dirs:
        yield dirs.tmp_fd


def test_create_file_exclusive_is_0600_under_umask_0(
    tmp_fd: int, state_path: Path, umask0: None
) -> None:
    fd = files.create_file_exclusive(tmp_fd, "a.txt")
    try:
        os.write(fd, b"hi")
    finally:
        os.close(fd)
    assert mode_of(state_path / "tmp" / "a.txt") == 0o600
    assert (state_path / "tmp" / "a.txt").read_bytes() == b"hi"


def test_create_file_exclusive_refuses_existing_file(tmp_fd: int, state_path: Path) -> None:
    os.close(files.create_file_exclusive(tmp_fd, "a"))
    with pytest.raises(FileExistsError):
        files.create_file_exclusive(tmp_fd, "a")


def test_create_file_exclusive_does_not_follow_a_planted_symlink(
    tmp_fd: int, state_path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "victim"
    target.write_text("keep")
    os.symlink(target, state_path / "tmp" / "a")
    with pytest.raises(FileExistsError):
        files.create_file_exclusive(tmp_fd, "a")
    assert target.read_text() == "keep"


def test_create_file_exclusive_does_not_create_through_a_dangling_symlink(
    tmp_fd: int, state_path: Path, tmp_path: Path
) -> None:
    os.symlink(tmp_path / "new-target", state_path / "tmp" / "a")
    with pytest.raises(FileExistsError):
        files.create_file_exclusive(tmp_fd, "a")
    assert not (tmp_path / "new-target").exists()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "/abs", "a\0b", "x" * 300])
def test_create_helpers_reject_hostile_names(tmp_fd: int, name: str) -> None:
    with pytest.raises(ValueError):
        files.create_file_exclusive(tmp_fd, name)
    with pytest.raises(ValueError):
        files.create_dir_exclusive(tmp_fd, name)


def test_create_dir_exclusive_is_0700_under_umask_0(
    tmp_fd: int, state_path: Path, umask0: None
) -> None:
    fd = files.create_dir_exclusive(tmp_fd, "d")
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    assert mode_of(state_path / "tmp" / "d") == 0o700


def test_create_dir_exclusive_is_0700_under_restrictive_umask(
    tmp_fd: int, state_path: Path
) -> None:
    old = os.umask(0o277)
    try:
        os.close(files.create_dir_exclusive(tmp_fd, "d"))
    finally:
        os.umask(old)
    assert mode_of(state_path / "tmp" / "d") == 0o700


def test_create_dir_exclusive_refuses_symlink_and_existing(
    tmp_fd: int, state_path: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "e"
    elsewhere.mkdir()
    os.symlink(elsewhere, state_path / "tmp" / "d")
    with pytest.raises(FileExistsError):
        files.create_dir_exclusive(tmp_fd, "d")
    assert os.listdir(elsewhere) == []


# --- hook directories -------------------------------------------------------------------------

HOOK_NAME = re.compile(r"prc-hook-[0-9a-f]{32}")


def test_hook_dir_has_random_name_and_mode_0700(
    tmp_fd: int, state_path: Path, umask0: None
) -> None:
    a = files.create_hook_dir(tmp_fd, state_path / "tmp")
    b = files.create_hook_dir(tmp_fd, state_path / "tmp")
    try:
        assert HOOK_NAME.fullmatch(a.name)
        assert a.name != b.name
        assert a.path == state_path / "tmp" / a.name
        assert mode_of(a.path) == 0o700
        assert os.listdir(a.path) == []
    finally:
        a.remove(tmp_fd)
        b.remove(tmp_fd)
    assert os.listdir(state_path / "tmp") == []


def test_hook_dir_name_collision_is_retried(
    tmp_fd: int, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = iter(["a" * 32, "b" * 32])
    (state_path / "tmp" / f"prc-hook-{'a' * 32}").mkdir()
    monkeypatch.setattr(files.secrets, "token_hex", lambda n: next(names))
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    assert hook.name == f"prc-hook-{'b' * 32}"
    hook.remove(tmp_fd)


def test_hook_dir_collision_gives_up_after_bounded_attempts(
    tmp_fd: int, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (state_path / "tmp" / f"prc-hook-{'a' * 32}").mkdir()
    monkeypatch.setattr(files.secrets, "token_hex", lambda n: "a" * 32)
    with pytest.raises(StateUnsafeError):
        files.create_hook_dir(tmp_fd, state_path / "tmp")


def test_remove_deletes_a_populated_tree(tmp_fd: int, state_path: Path) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    (hook.path / "f").write_text("x")
    (hook.path / "sub" / "deeper").mkdir(parents=True)
    (hook.path / "sub" / "deeper" / "g").write_text("y")
    (hook.path / "sub" / "-rf").write_text("z")
    (hook.path / "new\nline").write_text("z")
    (hook.path / "‮evil").write_text("z")
    (hook.path / ("n" * 255)).write_text("z")
    hook.remove(tmp_fd)
    assert not hook.path.exists()


def test_symlink_in_hook_dir_is_removed_as_a_link_and_target_survives(
    tmp_fd: int, state_path: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    file_target = tmp_path / "victim.txt"
    file_target.write_text("keep")
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    os.symlink(outside, hook.path / "dirlink")
    os.symlink(file_target, hook.path / "filelink")
    os.symlink(tmp_path / "missing", hook.path / "dangling")
    os.symlink("..", hook.path / "parent")
    os.symlink("../..", hook.path / "grandparent")
    os.symlink(state_path / "tmp", hook.path / "tmplink")
    sub = hook.path / "sub"
    sub.mkdir()
    os.symlink(outside, sub / "nested")
    hook.remove(tmp_fd)
    assert not hook.path.exists()
    assert (outside / "keep.txt").read_text() == "keep"
    assert file_target.read_text() == "keep"
    assert state_path.is_dir() and (state_path / "tmp").is_dir()


def test_hardlink_to_outside_file_only_loses_the_link(
    tmp_fd: int, state_path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "victim.txt"
    target.write_text("keep")
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    os.link(target, hook.path / "hard")
    hook.remove(tmp_fd)
    assert target.read_text() == "keep"


def test_hook_dir_replaced_by_a_symlink_is_unlinked_not_followed(
    tmp_fd: int, state_path: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    os.rmdir(hook.path)
    os.symlink(outside, hook.path)
    hook.remove(tmp_fd)
    assert not os.path.lexists(hook.path)
    assert (outside / "keep.txt").read_text() == "keep"


def test_remove_tree_at_refuses_hostile_names(tmp_fd: int, tmp_path: Path) -> None:
    victim = tmp_path / "v"
    victim.mkdir()
    for name in ("..", ".", "", "a/b", "../v"):
        with pytest.raises(ValueError):
            files.remove_tree_at(tmp_fd, name)
    assert victim.is_dir()


def test_remove_of_a_missing_entry_is_a_no_op(tmp_fd: int) -> None:
    files.remove_tree_at(tmp_fd, "prc-hook-" + "0" * 32)


def test_removal_never_leaves_the_directory_when_a_subdir_is_swapped_for_a_symlink(
    tmp_fd: int, state_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap a listed real directory for a symlink between listing and opening."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    (hook.path / "sub").mkdir()
    real_open = os.open
    swapped = []

    def evil_open(path: object, flags: int, *a: object, **kw: object) -> int:
        if path == "sub" and not swapped:
            swapped.append(True)
            os.rmdir(hook.path / "sub")
            os.symlink(outside, hook.path / "sub")
        return real_open(path, flags, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(files, "os", os_proxy(open=evil_open))
    try:
        hook.remove(tmp_fd)
    except StateUnsafeError:
        pass
    monkeypatch.undo()
    assert swapped
    assert (outside / "keep.txt").read_text() == "keep"
    hook.remove(tmp_fd)
    assert not hook.path.exists()


def nest(hook: files.HookDir, levels: int) -> None:
    fd = os.dup(hook.fd)
    try:
        for _ in range(levels):
            os.mkdir("d", 0o700, dir_fd=fd)
            nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
    finally:
        os.close(fd)


def test_tree_nested_64_levels_below_the_hook_dir_is_removed(tmp_fd: int, state_path: Path) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    nest(hook, files.MAX_REMOVE_DEPTH)
    hook.remove(tmp_fd)
    assert not hook.path.exists()


def test_tree_nested_65_levels_is_refused_and_left_in_place(tmp_fd: int, state_path: Path) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    nest(hook, files.MAX_REMOVE_DEPTH + 1)
    with pytest.raises(StateUnsafeError) as info:
        hook.remove(tmp_fd)
    assert hook.path.is_dir()  # left in place; the sweep never touches non-empty directories
    deepest = hook.path.joinpath(*(["d"] * (files.MAX_REMOVE_DEPTH + 1)))
    assert info.value.details["path"] == str(deepest)


def test_very_deep_tree_is_refused_not_crashed(tmp_fd: int, state_path: Path) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    nest(hook, files.MAX_REMOVE_DEPTH + 20)
    with pytest.raises(StateUnsafeError):
        hook.remove(tmp_fd)
    assert hook.path.is_dir()


@pytest.mark.parametrize("mode", [0o555, 0o500, 0o300, 0o000])
def test_unusable_own_subdirs_are_made_removable(tmp_fd: int, state_path: Path, mode: int) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    sub = hook.path / "sub"
    (sub / "inner").mkdir(parents=True)
    (sub / "f").write_text("x")
    (sub / "inner" / "g").write_text("y")
    os.chmod(sub / "inner", mode)
    os.chmod(sub, mode)
    hook.remove(tmp_fd)
    assert not hook.path.exists()


def test_a_directory_not_owned_by_us_is_not_chmodded_and_removal_refuses(
    tmp_fd: int, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    sub = hook.path / "sub"
    sub.mkdir()
    os.chmod(sub, 0o500)
    other = os.geteuid() + 1
    monkeypatch.setattr(files, "os", os_proxy(geteuid=lambda: other))
    try:
        with pytest.raises(StateUnsafeError):
            hook.remove(tmp_fd)
    finally:
        monkeypatch.undo()
    assert mode_of(sub) == 0o500
    os.chmod(sub, 0o700)
    hook.remove(tmp_fd)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open mode 0 directories")
def test_an_unreadable_directory_not_owned_by_us_is_never_chmodded(
    tmp_fd: int, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    sub = hook.path / "sub"
    sub.mkdir()
    os.chmod(sub, 0o000)
    real = os.geteuid()
    chmods: list[object] = []
    calls = 0

    def euid() -> int:
        # The hook directory itself is ours; only the nested directory looks foreign.
        nonlocal calls
        calls += 1
        return real if calls == 1 else real + 1

    def spy_chmod(path: object, *a: object, **kw: object) -> None:
        chmods.append(path)

    monkeypatch.setattr(files, "os", os_proxy(geteuid=euid, chmod=spy_chmod))
    try:
        with pytest.raises(StateUnsafeError) as info:
            hook.remove(tmp_fd)
    finally:
        monkeypatch.undo()
    assert info.value.details["path"] == str(sub)
    assert chmods == []
    assert mode_of(sub) == 0o000
    os.chmod(sub, 0o700)
    hook.remove(tmp_fd)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open mode 0 directories")
def test_a_missing_proc_is_reported_not_treated_as_a_vanished_entry(
    tmp_fd: int, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    sub = hook.path / "sub"
    sub.mkdir()
    os.chmod(sub, 0o000)

    def no_proc_chmod(path: object, *a: object, **kw: object) -> None:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))

    monkeypatch.setattr(files, "os", os_proxy(chmod=no_proc_chmod))
    try:
        with pytest.raises(StateUnsafeError) as info:
            hook.remove(tmp_fd)
    finally:
        monkeypatch.undo()
    assert info.value.details["path"] == str(sub)
    assert info.value.details["errno"] == "ENOENT"
    assert sub.is_dir()
    os.chmod(sub, 0o700)
    hook.remove(tmp_fd)


def test_removal_does_not_cross_into_another_filesystem(
    tmp_fd: int, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    (hook.path / "mnt").mkdir()
    (hook.path / "mnt" / "keep").write_text("x")
    real_stat = os.stat

    def fake_stat(path: object, *a: object, **kw: object) -> os.stat_result:
        st = real_stat(path, *a, **kw)  # type: ignore[arg-type]
        if path == "mnt":
            values = list(st)
            values[2] += 1  # st_dev
            return os.stat_result(values)
        return st

    monkeypatch.setattr(files, "os", os_proxy(stat=fake_stat))
    try:
        with pytest.raises(StateUnsafeError) as info:
            hook.remove(tmp_fd)
    finally:
        monkeypatch.undo()
    assert info.value.details["path"] == str(hook.path / "mnt")
    assert (hook.path / "mnt" / "keep").read_text() == "x"


def test_removal_failure_reports_the_absolute_path_of_the_failing_entry(
    tmp_fd: int, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    (hook.path / "sub").mkdir()
    (hook.path / "sub" / "f").write_text("x")
    real_unlink = os.unlink

    def failing_unlink(path: object, *a: object, **kw: object) -> None:
        if path == "f":
            raise PermissionError(errno_eperm(), "no")
        real_unlink(path, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(files, "os", os_proxy(unlink=failing_unlink))
    try:
        with pytest.raises(StateUnsafeError) as info:
            hook.remove(tmp_fd)
    finally:
        monkeypatch.undo()
    assert info.value.details["path"] == str(hook.path / "sub" / "f")
    assert info.value.details["errno"] == "EPERM"
    hook.remove(tmp_fd)


def errno_eperm() -> int:
    import errno

    return errno.EPERM


# --- sweeping ---------------------------------------------------------------------------------

DAY = 86400.0


def hook_dir_named(parent: Path, char: str, age: float, *, now: float) -> Path:
    path = parent / f"prc-hook-{char * 32}"
    path.mkdir(mode=0o700)
    os.utime(path, (now - age, now - age))
    return path


def test_sweep_removes_only_old_empty_hook_dirs(tmp_fd: int, state_path: Path) -> None:
    tmp = state_path / "tmp"
    now = 2_000_000_000.0
    old_empty = hook_dir_named(tmp, "a", DAY + 60, now=now)
    young_empty = hook_dir_named(tmp, "b", DAY - 60, now=now)
    exactly_day = hook_dir_named(tmp, "c", DAY, now=now)
    old_full = hook_dir_named(tmp, "d", DAY * 5, now=now)
    (old_full / "f").write_text("x")
    os.utime(old_full, (now - DAY * 5, now - DAY * 5))
    old_other = tmp / "something-else"
    old_other.mkdir()
    os.utime(old_other, (now - DAY * 9, now - DAY * 9))
    old_file = tmp / f"prc-hook-{'e' * 32}"
    old_file.write_text("x")
    os.utime(old_file, (now - DAY * 9, now - DAY * 9))
    removed = files.sweep_stale_hook_dirs(tmp_fd, now=now)
    assert removed == [old_empty.name]
    assert not old_empty.exists()
    for kept in (young_empty, exactly_day, old_full, old_other, old_file):
        assert os.path.lexists(kept), kept


@pytest.mark.parametrize(
    "name",
    [
        "prc-hook-" + "A" * 32,
        "prc-hook-" + "a" * 31,
        "prc-hook-" + "a" * 33,
        "prc-hook-" + "a" * 32 + "x",
        "prc-hook-" + "a" * 32 + "\n",
        "xprc-hook-" + "a" * 32,
        "prc-hook-",
        "prc-hook-" + "g" * 32,
        "prc-" + "a" * 32,
        "prc-hook-" + "ａ" * 32,
    ],
)
def test_sweep_ignores_lookalike_names(tmp_fd: int, state_path: Path, name: str) -> None:
    now = 2_000_000_000.0
    path = state_path / "tmp" / name
    path.mkdir()
    os.utime(path, (now - DAY * 9, now - DAY * 9))
    assert files.sweep_stale_hook_dirs(tmp_fd, now=now) == []
    assert path.is_dir()


def test_sweep_does_not_follow_or_remove_symlinks_named_like_hook_dirs(
    tmp_fd: int, state_path: Path, tmp_path: Path
) -> None:
    now = 2_000_000_000.0
    outside = tmp_path / "outside-empty"
    outside.mkdir()
    os.utime(outside, (now - DAY * 9, now - DAY * 9))
    link = state_path / "tmp" / f"prc-hook-{'a' * 32}"
    os.symlink(outside, link)
    assert files.sweep_stale_hook_dirs(tmp_fd, now=now) == []
    assert outside.is_dir()
    assert link.is_symlink()


def test_sweep_leaves_a_dir_that_gains_content_between_check_and_rmdir(
    tmp_fd: int, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000.0
    path = hook_dir_named(state_path / "tmp", "a", DAY * 3, now=now)
    real_rmdir = os.rmdir

    def racing_rmdir(p: str, *a: object, **kw: object) -> None:
        (path / "late").write_text("x")
        real_rmdir(p, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(files, "os", os_proxy(rmdir=racing_rmdir))
    assert files.sweep_stale_hook_dirs(tmp_fd, now=now) == []
    assert (path / "late").read_text() == "x"


def test_sweep_of_an_empty_directory_is_quiet(tmp_fd: int) -> None:
    assert files.sweep_stale_hook_dirs(tmp_fd) == []


def test_sweep_never_removes_a_live_hook_dir_of_a_concurrent_run(
    tmp_fd: int, state_path: Path
) -> None:
    hook = files.create_hook_dir(tmp_fd, state_path / "tmp")
    assert files.sweep_stale_hook_dirs(tmp_fd) == []
    assert hook.path.is_dir()
    hook.remove(tmp_fd)


def test_sweep_works_through_state_dirs_helper(env: dict[str, str], state_path: Path) -> None:
    with open_dirs(env) as dirs:
        now = 2_000_000_000.0
        hook_dir_named(dirs.tmp_path, "a", DAY * 2, now=now)
        assert dirs.sweep(now=now) == [f"prc-hook-{'a' * 32}"]
