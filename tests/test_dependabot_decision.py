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
    (PATCH, GREEN, None,     "merge"),
    (MINOR, GREEN, None,     "merge"),
    (MINOR, ["success"], "1.2.0",    "merge"),
    (MAJOR, GREEN, None,     "skip"),
    ("version-update:semver-indirect", GREEN, None,       "skip"),
    (PATCH, [], None,                                     "skip"),
    (PATCH, ["success", None], None,                      "skip"),
    (PATCH, ["success", "in_progress"], None,              "skip"),
    (PATCH, ["success", "failure"], None,                  "skip"),
    (PATCH, ["cancelled"], None,                           "skip"),
    (MINOR, ["success", "timed_out"], None,                "skip"),
    (MINOR, GREEN, "0.4.0",                                "skip"),
    (PATCH, GREEN, "0.4.0",                                "merge"),
]


@pytest.mark.parametrize("update_type,conclusions,new_version,expected", CASES)
def test_decide(update_type, conclusions, new_version, expected):
    assert decide(update_type, conclusions, new_version).action == expected


def test_no_checks_names_the_reason_explicitly():
    assert "no checks" in decide(PATCH, []).reason


def test_0x_minor_names_the_reason_explicitly():
    assert "0.x minor" in decide(MINOR, GREEN, "0.9.0").reason
