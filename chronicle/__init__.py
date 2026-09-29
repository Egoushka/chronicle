"""chronicle — self-hosted memory for one person's digital life."""

#: Kept equal to pyproject.toml's `version` by tests/test_version.py. Served by
#: the api's /health, so a running deployment says which release it is — the
#: reference box once ran code that matched no commit, and nothing could tell.
__version__ = "0.3.0"
