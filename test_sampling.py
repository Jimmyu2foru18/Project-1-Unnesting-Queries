"""Sampling and equivalence, exercised against a real in-memory DuckDB database."""
import duckdb
import pytest

from sqlrewriter.catalog import ForeignKey, from_mapping
from sqlrewriter.dialects import get_dialect
from sqlrewriter.equivalence import compare, multiset
from sqlrewriter.sampling import SAMPLE_SCHEMA, build_sample, sample_predicate_sql

DIALECT = get_dialect("duckdb")


@pytest.fixture
def conn():
    connection = duckdb.connect()
    connection.execute("create table movies as select i as id, 'title ' || i as title, 1990 + (i % 30) as year from range(2000) t(i)")
    connection.execute("create table ratings as select (i % 1500) as movie_id, (i % 10) as rating, i as votes from range(20000) t(i)")
    connection.execute("create table directors as select (i % 1500) as movie_id, (i % 400) as person_id from range(3000) t(i)")
    yield connection
    connection.close()


CATALOG = from_mapping(
    {
        "movies": {"id": "INT", "title": "TEXT", "year": "INT"},
        "ratings": {"movie_id": "INT", "rating": "INT", "votes": "INT"},
        "directors": {"movie_id": "INT", "person_id": "INT"},
    },
    schema="main",
    primary={"movies": ["id"], "ratings": [], "directors": []},
    rows={"movies": 2000, "ratings": 20_000, "directors": 3000},
    foreign_keys=[
        ForeignKey("ratings", "movie_id", "movies", "id"),
        ForeignKey("directors", "movie_id", "movies", "id"),
    ],
)


def test_build_sample_creates_every_table(conn):
    stats = build_sample(DIALECT, conn, CATALOG, percent=10.0)
    assert not stats.errors, stats.problems()
    assert set(stats.tables) == set(CATALOG.columns)
    assert all(count > 0 for count in stats.tables.values())
    tables = {r[0] for r in conn.execute(
        "select table_name from information_schema.tables where table_schema = ?", [SAMPLE_SCHEMA]
    ).fetchall()}
    assert tables == set(CATALOG.columns)


def test_the_sample_is_smaller_than_the_original(conn):
    stats = build_sample(DIALECT, conn, CATALOG, percent=10.0)
    for table, sampled in stats.tables.items():
        full = conn.execute(f"select count(*) from {table}").fetchone()[0]
        assert 0 < sampled < full


def test_the_sample_keeps_its_foreign_keys_valid(conn):
    build_sample(DIALECT, conn, CATALOG, percent=20.0)
    dangling = conn.execute(
        f"select count(*) from {SAMPLE_SCHEMA}.ratings r "
        f"left join {SAMPLE_SCHEMA}.movies m on m.id = r.movie_id where m.id is null"
    ).fetchone()[0]
    assert dangling == 0


def test_a_fully_sampled_child_table_keeps_its_keys_too(conn):
    build_sample(DIALECT, conn, CATALOG, percent=100.0)
    assert conn.execute(
        f"select count(*) from {SAMPLE_SCHEMA}.ratings r "
        f"left join {SAMPLE_SCHEMA}.movies m on m.id = r.movie_id where m.id is null"
    ).fetchone()[0] == 0


def test_the_row_cap_is_respected(conn):
    stats = build_sample(DIALECT, conn, CATALOG, percent=100.0, max_rows=50)
    assert all(count <= 50 for count in stats.tables.values())


def test_building_the_sample_twice_replaces_it(conn):
    first = build_sample(DIALECT, conn, CATALOG, percent=10.0)
    second = build_sample(DIALECT, conn, CATALOG, percent=50.0)
    assert second.rows >= first.rows


def test_an_unsamplable_table_is_skipped_not_fatal(conn):
    catalog = from_mapping(
        {**CATALOG.columns, "gone": {"id": "INT"}},
    )
    stats = build_sample(DIALECT, conn, CATALOG, percent=10.0)
    assert "gone" not in stats.tables


def test_sample_predicate_sql_retargets_only_real_tables():
    sql = "SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id"
    retargeted = sample_predicate_sql(DIALECT, sql, CATALOG, "sqlrw_sample")
    assert "sqlrw_sample" in retargeted
    assert retargeted.count("sqlrw_sample") >= 2


def test_sample_predicate_sql_retargets_tables_inside_a_cte_but_not_the_cte_itself():
    sql = "WITH x AS (SELECT id FROM movies) SELECT id FROM x"
    retargeted = sample_predicate_sql(DIALECT, sql, CATALOG, "sqlrw_sample")
    assert "sqlrw_sample" in retargeted
    assert "FROM x" in retargeted


def test_sample_predicate_sql_keeps_an_explicit_schema():
    sql = "SELECT id FROM other.movies"
    retargeted = sample_predicate_sql(DIALECT, sql, CATALOG, "sqlrw_sample")
    assert "sqlrw_sample" not in retargeted


SIMPLE = "SELECT title FROM movies WHERE year = 1990"
EQUIVALENT = "SELECT title FROM movies WHERE year = 1990 ORDER BY title"
DIFFERENT = "SELECT title FROM movies WHERE year = 1991"


def test_identical_results_are_equal(conn):
    outcome = compare(DIALECT, conn, SIMPLE, SIMPLE, catalog=CATALOG)
    assert outcome.equal is True
    assert outcome.status == "equal"
    assert "equal on" in outcome.describe()


def test_row_order_does_not_matter(conn):
    outcome = compare(DIALECT, conn, SIMPLE, EQUIVALENT, catalog=CATALOG)
    assert outcome.equal is True


def test_different_rows_are_caught(conn):
    outcome = compare(DIALECT, conn, SIMPLE, DIFFERENT, catalog=CATALOG)
    assert outcome.equal is False
    assert outcome.status == "different"
    assert "only the original returns" in outcome.reason


def test_duplicate_rows_are_not_collapsed(conn):
    original = "SELECT year FROM movies"
    candidate = "SELECT year FROM movies UNION ALL SELECT year FROM movies"
    outcome = compare(DIALECT, conn, original, candidate, catalog=CATALOG)
    assert outcome.equal is False


def test_a_different_column_count_is_caught(conn):
    outcome = compare(DIALECT, conn, SIMPLE, "SELECT title, year FROM movies WHERE year = 1990", catalog=CATALOG)
    assert outcome.equal is False
    assert "select list" in outcome.reason or "columns" in outcome.reason


def test_a_candidate_that_does_not_run_is_reported_not_raised(conn):
    outcome = compare(DIALECT, conn, SIMPLE, "SELECT missing FROM movies", catalog=CATALOG)
    assert outcome.equal is False
    assert "did not run" in outcome.reason


def test_results_over_the_limit_are_unknown_rather_than_equal(conn):
    outcome = compare(DIALECT, conn, "SELECT id FROM movies", "SELECT id FROM movies",
                      catalog=CATALOG, limit=10)
    assert outcome.equal is None
    assert outcome.status == "unknown"
    assert "limit" in outcome.reason


def test_comparison_runs_inside_the_sample_schema(conn):
    build_sample(DIALECT, conn, CATALOG, percent=10.0)
    outcome = compare(DIALECT, conn, SIMPLE, SIMPLE, catalog=CATALOG, schema=SAMPLE_SCHEMA)
    assert outcome.equal is True
    assert outcome.rows < 2000


def test_a_rewrite_that_only_matches_on_the_sample_is_still_caught(conn):
    build_sample(DIALECT, conn, CATALOG, percent=25.0)
    outcome = compare(DIALECT, conn, SIMPLE, DIFFERENT, catalog=CATALOG, schema=SAMPLE_SCHEMA)
    assert outcome.equal is False


def test_the_sample_is_what_makes_equivalence_affordable(conn):
    stats = build_sample(DIALECT, conn, CATALOG, percent=5.0)
    assert stats.rows < conn.execute("select count(*) from ratings").fetchone()[0]
    assert "rows across" in stats.describe()


def test_multiset_ignores_order_but_not_multiplicity():
    assert multiset([(1,), (1,), (2,)]) == multiset([(2,), (1,), (1,)])
    assert multiset([(1,), (2,)]) != multiset([(1,), (1,), (2,)])


def test_floats_are_compared_with_a_tolerance():
    assert multiset([(0.1 + 0.2,)]) == multiset([(0.3,)])