import importlib.metadata

import pytest

from pr_comment.cli import main


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    version = importlib.metadata.version("pr-comment")
    assert capsys.readouterr().out.strip() == f"pr-comment {version}"


def test_no_arguments_returns_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err
