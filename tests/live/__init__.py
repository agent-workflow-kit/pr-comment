"""The live suite (issue #39): tests marked ``live`` that talk to real forge hosts.

Nothing here runs by default. The host-facing tests need the opt-in variable, a target allowlist and
throwaway targets, all named in ``docs/live-checklist.md`` (generated from ``checklist.py``).
``settings.py`` reads the environment and decides what may run, ``runner.py`` drives pr-comment and
forgectl (or only describes the calls), ``scenarios.py`` holds the scenarios and ``checklist.py``
maps every still-to-verify row to a scenario or a manual step. The suite's own logic is tested
offline in ``tests/test_live_*.py`` against the pinned fake forgectl.
"""
