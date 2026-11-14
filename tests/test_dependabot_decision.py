"""The automerge decision (update type x check states -> merge or skip).

Pure logic, no workflow: see scripts/dependabot_decision.py for why no
.github/workflows/ change ships with it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from dependabot_decision import decide

PATCH = "version-update:semver-patch"
MINOR = "version-update:semver-minor"
MAJOR = "version-update:semver-major"
GREEN = ["success", "neutral", "skipped"]

CASES = [
    (PATCH, GREEN,                                  "merge"),
    (MINOR, GREEN,                                  "merge"),
    (MINOR, ["success"],                            "merge"),
    (MAJOR, GREEN,                                  "skip"),
    ("version-update:semver-indirect", GREEN,       "skip"),
    (PATCH, [],                                     "skip"),
    (PATCH, ["success", None],                      "skip"),
    (PATCH, ["success", "in_progress"],              "skip"),
    (PATCH, ["success", "failure"],                  "skip"),
    (PATCH, ["cancelled"],                           "skip"),
    (MINOR, ["success", "timed_out"],                "skip"),
]


@pytest.mark.parametrize("update_type,conclusions,expected", CASES)
def test_decide(update_type, conclusions, expected):
    assert decide(update_type, conclusions).action == expected


def test_no_checks_names_the_reason_explicitly():
    assert "no checks" in decide(PATCH, []).reason
