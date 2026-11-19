"""The host-facing live suite. Needs ``PR_COMMENT_LIVE=1`` and the targets of
``docs/live-checklist.md``; run by the maintainer only, never in CI.

    PR_COMMENT_LIVE=1 PR_COMMENT_LIVE_MODE=plan  pytest -m live tests/live/test_live_hosts.py -rs
    PR_COMMENT_LIVE=1 PR_COMMENT_LIVE_MODE=preflight ... (reads only; every write is a dry run)
    PR_COMMENT_LIVE=1 PR_COMMENT_LIVE_MODE=run ...       (writes to the throwaway targets)

Without the opt-in every test here is skipped with the reason. With it, a setting the suite refuses
(no allowlist, a target outside it, a missing report directory) fails the run before any call, and
the failure names variables, never values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live.driver import cases, run_scenario
from tests.live.runner import LiveRefusal
from tests.live.scenarios import Scenario, skip_reason
from tests.live.settings import Target, from_env

pytestmark = pytest.mark.live

SETTINGS = from_env()  # the environment as it was at collection, before the autouse fixtures
CASES = cases(SETTINGS)


def _id(case: tuple[Scenario, Target]) -> str:
    return f"{case[0].id}-{case[1].name}"


@pytest.mark.parametrize("case", CASES or [None], ids=[_id(c) for c in CASES] or ["no-target"])
def test_live_scenario(
    case: tuple[Scenario, Target] | None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reason = SETTINGS.skip_reason()
    if reason:
        pytest.skip(reason)
    refusals = SETTINGS.refusals()
    if refusals:
        pytest.fail("the live suite refused to run: " + "; ".join(refusals), pytrace=False)
    if case is None:
        pytest.skip("no target configured for this mode")
    scenario, target = case
    why = skip_reason(scenario, SETTINGS, target)
    if why:
        pytest.skip(why)
    try:
        runner = run_scenario(SETTINGS, scenario, target, tmp_path / "work")
    except LiveRefusal as exc:
        pytest.fail(f"the live suite refused a step: {exc}", pytrace=False)
    with capsys.disabled():
        print(f"\n--- {scenario.id} on {target.name} ({SETTINGS.mode}, run {SETTINGS.run_id}) ---")
        for line in runner.describe():
            print(line)
        for note in runner.notes:
            print(f"NOTE: {note}")
