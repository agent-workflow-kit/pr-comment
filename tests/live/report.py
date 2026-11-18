"""What a live run leaves for the user: one JSON file per scenario and target, holding the observed
host facts (the input of design.md's Live verification table), the steps taken and what is left on
the targets. Never an environment variable, a token or a body."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runner import Runner
from .settings import redact


def write_report(
    runner: Runner, scenario: str, target: str, status: str, notes: list[str]
) -> Path | None:
    directory = runner.settings.report_dir
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "run_id": runner.settings.run_id,
        "mode": runner.settings.mode,
        "scenario": scenario,
        "target": target,
        "status": status,
        "observations": runner.observations,
        "steps": runner.describe(),
        "left_for_a_human": notes,
    }
    path = directory / f"results-{runner.settings.run_id}-{scenario}-{target}.json"
    path.write_text(
        redact(json.dumps(document, indent=2, default=str), runner.env) + "\n", encoding="utf-8"
    )
    return path
