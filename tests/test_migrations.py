"""Migration-source regression tests.

These read the .sql files as text rather than running them. That is deliberate:
the defects they guard against are shaped like "this still works, it is just
45x slower", which a functional test against a throwaway database with 1,200
rows will never catch — at that size the planner picks a sequential scan
whether or not the index is reachable.
"""
import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
CORE = (MIGRATIONS / "001_core.sql").read_text()
RETRIEVAL = (MIGRATIONS / "002_retrieval.sql").read_text()


def squash(sql: str) -> str:
    """Collapse whitespace so formatting differences do not register as drift."""
    return re.sub(r"\s+", " ", sql).strip()


def body_of(sql: str, function: str) -> str:
    """The $$-quoted body of a CREATE [OR REPLACE] FUNCTION block."""
    m = re.search(
        rf"CREATE (?:OR REPLACE )?FUNCTION {function}\b.*?\$\$(.*?)\$\$",
        sql, re.DOTALL | re.IGNORECASE)
    assert m, f"{function} not found in migration source"
    return m.group(1)


def searched_table() -> str:
    """The table hybrid_search ranks over, read from its own dense branch.

    Derived rather than hardcoded so these tests survive a table rename. There
    are two full-text GIN indexes in 001_core.sql (one on `event`, one on the
    aggregate unit); picking the wrong one is the mistake this avoids.
    """
    body = body_of(RETRIEVAL, "hybrid_search")
    m = re.search(r"dense AS \(.*?\bFROM (\w+)\b", body, re.DOTALL)
    assert m, "hybrid_search dense branch has no FROM <table>"
    return m.group(1)


def fts_index_expression(table: str) -> str:
    """The indexed to_tsvector() expression for `table`'s full-text GIN index."""
    for m in re.finditer(
            rf"CREATE INDEX \w+ ON {table} USING GIN \(\s*(.*?)\);",
            CORE, re.DOTALL | re.IGNORECASE):
        expr = squash(m.group(1))
        if expr.lower().startswith("to_tsvector"):
            return expr
    pytest.fail(f"no full-text GIN index on {table} in 001_core.sql")


def test_hybrid_search_lexical_branch_can_reach_its_index():
    """The lexical branch must read the base table, never a shared CTE.

    A CTE referenced more than once is materialized, and a CTE scan cannot use
    an index. Factoring the filter predicates into one `filtered` CTE therefore
    cost `episode_fts_idx` and made the lexical branch recompute to_tsvector()
    over every substantive episode: measured 5,506 ms against 3.2 ms with the
    index, and 5,694 ms against 120-255 ms for hybrid_search as a whole.

    If this test fails, someone has re-factored the duplicated predicates back
    into a shared CTE. The duplication is the optimization.
    """
    table = searched_table()
    body = body_of(RETRIEVAL, "hybrid_search")

    dense = re.search(r"dense AS \((.*?)\n\),", body, re.DOTALL)
    lexical = re.search(r"lexical AS \((.*?)\n\)", body, re.DOTALL)
    assert dense and lexical, "hybrid_search no longer has dense/lexical CTEs"

    for name, branch in (("dense", dense.group(1)), ("lexical", lexical.group(1))):
        assert re.search(rf"\bFROM {table}\b", branch), (
            f"hybrid_search {name} branch does not read {table} directly; "
            "a CTE scan cannot use an index")

    assert not re.search(r"\bfiltered AS \(", body), (
        "the shared `filtered` CTE is back — it is materialized because both "
        "branches reference it, which costs the lexical branch its index")


def test_lexical_predicate_matches_the_index_expression():
    """Predicate and index expression must be character-identical.

    PostgreSQL matches an expression index by expression equality. Changing
    'ru_unaccent' to another config, or reordering the coalesce() arguments,
    silently drops to a sequential scan with no error and no plan warning —
    the query keeps returning correct rows, ~45x slower.
    """
    table = searched_table()
    indexed_expr = fts_index_expression(table)
    body = body_of(RETRIEVAL, "hybrid_search")

    used = re.findall(
        r"to_tsvector\(\s*'[^']+'\s*,\s*coalesce\([^)]*\)\s*\)", body)
    assert used, "hybrid_search no longer builds a to_tsvector expression"

    # The predicate qualifies columns with the branch's table alias; the index
    # expression cannot. Strip aliases from both before comparing.
    strip_alias = lambda s: re.sub(r"\b\w+\.(?=\w)", "", s)  # noqa: E731
    normalized = {strip_alias(squash(u)) for u in used}
    expected = strip_alias(indexed_expr)
    assert normalized == {expected}, (
        f"lexical predicate {normalized} has drifted from the indexed "
        f"expression {expected!r}; the GIN index will not be used")


@pytest.mark.parametrize("function", ["hybrid_search", "stratified_search"])
def test_retrieval_functions_declare_no_recency_prior(function):
    """Decay is off by default — a global recency prior is an 8.6x penalty on
    2018 content across a 7.6-year span, and archival queries are the
    interesting ones."""
    body = body_of(RETRIEVAL, function)
    assert not re.search(r"\b(exp|recip)\s*\(\s*-?\s*(extract|age)", body,
                         re.IGNORECASE), (
        f"{function} has grown a recency decay term")
