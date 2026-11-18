"""Runs one scenario on one target: the code the live tests and the offline tests share."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .ctx import Ctx
from .report import write_report
from .runner import Runner
from .scenarios import SCENARIOS, Scenario
from .settings import PLACEHOLDER_REF, TARGET_KEYS, LiveSettings, Target


def targets_for(settings: LiveSettings) -> list[Target]:
    """The configured targets; in plan mode with none, one placeholder per backend so the plan
    still shows every step."""
    if settings.targets:
        return list(settings.targets.values())
    if settings.mode == "plan":
        return [
            Target(key, backend, PLACEHOLDER_REF[backend])
            for key, backend in TARGET_KEYS.items()
            if key in ("GITHUB", "GITLAB")
        ]
    return []


def cases(settings: LiveSettings) -> list[tuple[Scenario, Target]]:
    return [(sc, target) for sc in SCENARIOS.values() for target in targets_for(settings)]


def run_scenario(
    settings: LiveSettings,
    scenario: Scenario,
    target: Target,
    workdir: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Runner:
    """Run ``scenario`` on ``target``. Teardown and the report happen whatever the outcome; the
    scenario's own failure is raised afterwards. ``runner.notes`` is what is left for a human."""
    runner = Runner(settings, workdir, env=env, scenario=scenario.id)
    ctx = Ctx(runner, target, scenario.id)
    status = "passed"
    try:
        runner.verify_target(target.ref)
        scenario.fn(ctx)
    except BaseException as exc:
        status = f"failed: {exc.__class__.__name__}"
        raise
    finally:
        runner.notes = runner.teardown()
        write_report(runner, scenario.id, target.name, status, runner.notes)
    return runner
