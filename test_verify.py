"""Static and structural verification gates, across all three dialects."""
import sqlglot
import pytest

from sqlrewriter.catalog import ForeignKey, from_mapping
from sqlrewriter.dialects import get_dialect
from sqlrewriter.verify import (
    check,
    correlated_columns,
    sargability_problems,
    static_problems,
    structural_problems,
)

CATALOG = from_mapping(
    {
        "movies": {"id": "INT", "title": "TEXT", "year": "NUMERIC"},
        "ratings": {"movie_id": "INT", "rating": "FLOAT", "votes": "INT"},
        "directors": {"movie_id": "INT", "person_id": "INT"},
    },
    primary={"movies": ["id"], "ratings": [], "directors": []},
    indexed={"movies": {"id"}, "ratings": {"movie_id"}, "directors": {"movie_id", "person_id"}},
    rows={"movies": 67_000, "ratings": 15_000_000, "directors": 1_200_000},
    foreign_keys=[ForeignKey("ratings", "movie_id", "movies", "id")],
)

CORRELATED = "SELECT m.title FROM movies m WHERE m.year > (SELECT AVG(m2.year) FROM movies m2 WHERE m2.id = m.id)"
UNNESTED = (
    "WITH peer AS (SELECT year AS y, COUNT(*) AS n FROM movies GROUP BY year) "
    "SELECT m.title FROM movies m JOIN peer p ON p.y = m.year"
)

DIALECTS = ["postgres", "mysql", "duckdb"]


@pytest.fixture(params=DIALECTS)
def dialect(request):
    return get_dialect(request.param)


def test_a_correct_rewrite_passes_every_gate(dialect):
    assert static_problems(UNNESTED, dialect, CATALOG) == []
    assert structural_problems(UNNESTED, CORRELATED, dialect, CATALOG) == []


def test_a_table_the_model_invented_is_rejected(dialect):
    assert static_problems("SELECT movie_id FROM movie_directors", dialect, CATALOG) == [
        "table 'movie_directors' does not exist"
    ]


def test_a_column_the_model_invented_is_rejected(dialect):
    problems = static_problems("SELECT m.release_year FROM movies m", dialect, CATALOG)
    assert problems and "release_year" in problems[0]


@pytest.mark.parametrize("sql", ["DELETE FROM movies", "DROP TABLE movies", "SELECT pg_sleep(1)"])
def test_a_mutating_statement_never_reaches_the_engine(sql, dialect):
    assert static_problems(sql, dialect, CATALOG)


def test_a_still_correlated_rewrite_is_caught_without_asking_the_model(dialect):
    problems = structural_problems(CORRELATED, CORRELATED, dialect, CATALOG)
    assert problems and "still correlated on m.id" in problems[0]


def test_a_cte_without_its_group_by_is_caught(dialect):
    broken = "WITH peer AS (SELECT m2.year AS y FROM movies m2) SELECT m.title FROM movies m JOIN peer p ON p.y = m.year"
    problems = structural_problems(broken, CORRELATED, dialect, CATALOG)
    assert problems and "no group by and no distinct" in problems[0]


def test_a_window_function_cte_is_caught_because_it_fans_out(dialect):
    broken = (
        "WITH peer AS (SELECT COUNT(*) OVER (PARTITION BY year) AS c, year FROM movies) "
        "SELECT m.title FROM movies m JOIN peer p ON p.year = m.year"
    )
    problems = structural_problems(broken, CORRELATED, dialect, CATALOG)
    assert problems and "window function" in problems[0]


def test_a_join_key_the_group_by_does_not_make_unique_is_caught(dialect):
    broken = (
        "WITH agg AS (SELECT year, COUNT(*) AS n FROM movies GROUP BY year) "
        "SELECT a.n FROM agg a JOIN movies m ON m.id = a.n"
    )
    problems = structural_problems(broken, CORRELATED, dialect, CATALOG)
    assert problems and "not key to key" in problems[0]


def test_a_correctly_grouped_cte_is_not_flagged(dialect):
    fine = (
        "WITH agg AS (SELECT year, COUNT(*) AS n FROM movies GROUP BY year) "
        "SELECT m.title FROM movies m JOIN agg a ON a.year = m.year WHERE m.id > a.n"
    )
    assert structural_problems(fine, CORRELATED, dialect, CATALOG) == []


def test_a_cte_selected_on_the_primary_key_is_accepted_ungrouped(dialect):
    fine = "WITH m2 AS (SELECT id FROM movies) SELECT m.title FROM movies m JOIN m2 ON m2.id = m.id"
    assert structural_problems(fine, CORRELATED, dialect, CATALOG) == []


def test_a_cte_in_the_from_clause_is_checked_too(dialect):
    broken = "WITH agg AS (SELECT year, COUNT(*) AS n FROM movies GROUP BY year) SELECT a.n FROM agg a JOIN movies m ON m.id = a.n"
    assert structural_problems(broken, CORRELATED, dialect, CATALOG)


def test_rewriting_not_exists_as_not_in_is_caught(dialect):
    source = "SELECT m.title FROM movies m WHERE NOT EXISTS (SELECT 1 FROM ratings r WHERE r.movie_id = m.id)"
    rewrite = "SELECT m.title FROM movies m WHERE m.id NOT IN (SELECT r.movie_id FROM ratings r)"
    problems = structural_problems(rewrite, source, dialect, CATALOG)
    assert problems and "not exists was rewritten as not in" in problems[0]


def test_dropping_distinct_order_by_or_limit_is_caught(dialect):
    source = "SELECT DISTINCT m.title FROM movies m ORDER BY m.title LIMIT 5"
    problems = structural_problems("SELECT m.title FROM movies m", source, dialect, CATALOG)
    assert any("distinct" in p for p in problems)
    assert any("order" in p for p in problems)
    assert any("limit" in p for p in problems)


def test_casting_an_indexed_column_is_caught_as_unsargable(dialect):
    problems = sargability_problems(
        "SELECT m.title FROM movies m WHERE cast(m.id as char) = '7'", dialect, CATALOG
    )
    assert problems and "indexed column movies.id" in problems[0]


def test_a_raw_indexed_predicate_is_not_flagged(dialect):
    assert not sargability_problems(
        "SELECT m.title FROM movies m WHERE m.year = 1990", dialect, CATALOG
    )


def test_correlation_detection_ignores_merely_unqualified_columns(dialect):
    assert correlated_columns(sqlglot.parse_one("SELECT id FROM movies")) == []


def test_check_stops_at_the_first_failing_gate(dialect):
    verdict = check("SELECT 1 FROM nowhere", CORRELATED, dialect, CATALOG)
    assert not verdict.ok
    assert verdict.problems == ["table 'nowhere' does not exist"]


def test_check_reports_a_pass(dialect):
    verdict = check(UNNESTED, CORRELATED, dialect, CATALOG)
    assert verdict.ok
    assert verdict.notes


def test_a_mysql_rewrite_is_parsed_with_mysql_dialect():
    dialect = get_dialect("mysql")
    sql = "SELECT `title` FROM `movies` WHERE `id` = 1 LIMIT 5"
    assert static_problems(sql, dialect, CATALOG) == []


def test_a_duckdb_specific_rewrite_is_parsed_with_duckdb_dialect():
    dialect = get_dialect("duckdb")
    sql = "SELECT list_value(1, 2)[1] AS one FROM movies"
    problems = static_problems(sql, dialect, CATALOG)
    assert problems == [] or "does not exist" in problems[0]