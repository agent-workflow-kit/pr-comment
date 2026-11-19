"""The two ways the live suite stops: a check that failed, and a step it refused to take."""

from __future__ import annotations


class LiveFailure(AssertionError):
    """A check of the live suite failed (a host behaved unexpectedly)."""


class LiveRefusal(Exception):
    """The suite refused to do something unsafe (a write to a target that is not verified)."""
