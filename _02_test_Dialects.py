"""DuckDB adapter tests."""
import pytest

from _02_dialects import (
    StatementRejected,
    get_dialect,
    open_database,
)

DIALECT = get_dialect("duckdb")


def test_registry_lookups():
    assert get_dialect("duckdb").name == "duckdb"
    with pytest.raises(KeyError, match="unknown database"):
        get_dialect("oracle")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM t",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ],
)
def test_valid_statements_are_accepted(sql):
    DIALECT.check_statement(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM movies",
        "UPDATE movies SET year = 0",
        "DROP TABLE movies",
        "TRUNCATE movies",
        "INSERT INTO movies VALUES (1)",
        "SELECT pg_sleep(10)",
        "SELECT 1 FROM a; SELECT 1 FROM b",
        "SELECT FROM WHERE",
    ],
)
def test_invalid_statements_are_refused(sql):
    with pytest.raises(StatementRejected):
        DIALECT.check_statement(sql)


def test_formatting_and_catalog():
    assert DIALECT.ref("main", "t") == '"main"."t"'
    assert DIALECT.quote('we"ird') == '"we""ird"'
    
    templates = DIALECT.catalog_queries("main")
    assert set(templates) == {"columns", "keys", "foreign_keys", "indexed_columns", "row_counts"}
    for template in templates.values():
        assert "'main'" in template


def test_fetch_handles_truncation():
    dialect, conn = open_database("duckdb", ":memory:")
    conn.execute("create table items as select range as id from range(5)")
    result = dialect.fetch(conn, "SELECT id FROM items", limit=2)
    assert result.rows == ((0,), (1,))
    assert result.truncated is True
    assert len(result.columns) == 1
    dialect.close(conn)


def test_measure_reports_timing_median():
    dialect, conn = open_database("duckdb", ":memory:")
    timing = dialect.measure(conn, "SELECT 42", runs=3)
    assert timing.ok
    assert timing.runs == 3
    assert timing.median_ms >= 0
    dialect.close(conn)


def test_measure_reports_error_on_failure():
    dialect, conn = open_database("duckdb", ":memory:")
    timing = dialect.measure(conn, "SELECT * FROM nonexistent_table", runs=1)
    assert not timing.ok
    assert timing.error is not None
    dialect.close(conn)


def test_explain_plan():
    dialect, conn = open_database("duckdb", ":memory:")
    conn.execute("create table t as select * from range(100) tbl(i)")
    plan = dialect.explain(conn, "select count(*) from t")
    assert plan.text
    assert plan.max_loops == 1
    dialect.close(conn)


def test_lifecycle_methods():
    dialect, conn = open_database("duckdb", ":memory:")
    dialect.commit(conn)
    dialect.rollback(conn)
    dialect.close(conn)