"""One version, in three places that must agree."""

import re
from pathlib import Path

from chronicle import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_package_and_pyproject_agree():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == __version__


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_changelog_has_a_section_for_it():
    """A release without notes is a tag nobody can read. CHANGELOG.md gets
    its `## [X.Y.Z]` heading in the same commit that bumps the version."""
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{__version__}]" in changelog
    assert "## [Unreleased]" in changelog
