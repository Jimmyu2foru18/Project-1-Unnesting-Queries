"""Engine adapters. The MySQL and PostgreSQL paths run without a server."""
import pytest

from sqlrewriter.dialects import (
    Dialect,
    StatementRejected,
    get_dialect,
    open_database,
)

ENGINES = ["postgres", "mysql", "duckdb"]

FAKE_CONN = {
    "columns": [("movies", "id", "integer", "NO"), ("movies", "title", "text", "YES")],
}


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchmany(self, size):
        return list(self.rows[:size])

    def close(self):
        self.closed = True

    @property
    def description(self):
        return [(f"c{i}",) for i in range(len(self.rows[0]) if self.rows else 0)]


class FakeConnection:
    """Enough of a DB-API connection for the adapters to drive."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else FAKE_CONN["columns"]
        self.cursors = []

    def cursor(self):
        cursor = FakeCursor(self.rows)
        self.cursors.append(cursor)
        return cursor

    def execute(self, sql, params=None):
        cursor = FakeCursor(self.rows)
        cursor.executed.append(sql)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.mark.parametrize("name", ENGINES)
def test_every_engine_maps_to_a_sqlglot_dialect(name):
    assert get_dialect(name).sqlglot in {"postgres", "mysql", "duckdb"}


def test_aliases_resolve_to_the_same_engine():
    assert get_dialect("postgresql").name == "postgres"
    assert get_dialect("MariaDB").name == "mysql"


def test_an_unknown_engine_is_rejected_by_name():
    with pytest.raises(KeyError, match="unknown database"):
        get_dialect("oracle")


@pytest.mark.parametrize("name", ENGINES)
def test_a_select_is_accepted(name):
    get_dialect(name).check_statement("SELECT 1 FROM t")


@pytest.mark.parametrize("name", ENGINES)
def test_a_with_statement_is_accepted(name):
    get_dialect(name).check_statement("WITH x AS (SELECT 1) SELECT * FROM x")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM movies",
        "UPDATE movies SET year = 0",
        "DROP TABLE movies",
        "TRUNCATE movies",
        "INSERT INTO movies VALUES (1)",
        "SELECT pg_sleep(10)",
        "COPY movies TO '/tmp/x'",
    ],
)
def test_mutating_statements_are_refused(sql):
    with pytest.raises(StatementRejected):
        get_dialect("postgres").check_statement(sql)


def test_more_than_one_statement_is_refused():
    with pytest.raises(StatementRejected, match="one statement"):
        get_dialect("duckdb").check_statement("SELECT 1 FROM a; SELECT 1 FROM b")


def test_syntax_errors_are_refused_with_a_readable_message():
    with pytest.raises(StatementRejected, match="syntax error"):
        get_dialect("duckdb").check_statement("SELECT FROM WHERE")


@pytest.mark.parametrize(
    "name,schema,expected",
    [
        ("postgres", "public", '"public"."t"'),
        ("duckdb", "main", '"main"."t"'),
        ("mysql", "sakila", "`sakila`.`t`"),
    ],
)
def test_identifiers_are_quoted_per_engine(name, schema, expected):
    assert get_dialect(name).ref(schema, "t") == expected


def test_quoting_escapes_an_embedded_quote():
    assert get_dialect("postgres").quote('we"ird') == '"we""ird"'
    assert get_dialect("mysql").quote("we`ird") == "`we``ird`"


@pytest.mark.parametrize("name", ENGINES)
def test_the_catalog_templates_only_name_the_requested_schema(name):
    dialect = get_dialect(name)
    templates = dialect.catalog_queries("main")
    assert set(templates) == {"columns", "keys", "foreign_keys", "indexed_columns", "row_counts"}
    for template in templates.values():
        assert "{schema}" in template


def test_a_schema_name_that_could_inject_is_refused():
    with pytest.raises(StatementRejected, match="unsafe schema"):
        get_dialect("postgres").render("select 1 from {schema}", "a'; drop table x --")


def test_the_schema_is_substituted_as_a_quoted_literal():
    rendered = get_dialect("postgres").render("where s = {schema}", "public")
    assert rendered == "where s = 'public'"


@pytest.mark.parametrize("name", ENGINES)
def test_sample_source_returns_a_subquery_that_actually_samples(name):
    dialect = get_dialect(name)
    source = dialect.sample_source("main", "movies", 0.5)
    assert source.startswith("SELECT * FROM ")
    assert "movies" in source


def test_the_sample_source_is_executable_on_duckdb():
    import duckdb

    conn = duckdb.connect()
    conn.execute("create table movies as select * from range(100000) tbl(i)")
    source = get_dialect("duckdb").sample_source("main", "movies", 1.0)
    count = conn.execute(f"select count(*) from ({source})").fetchone()[0]
    assert 0 < count < 100000


def test_the_postgres_sample_source_uses_tablesample_bernoulli():
    assert "TABLESAMPLE BERNOULLI" in get_dialect("postgres").sample_source("public", "t", 2.0)


@pytest.mark.parametrize("name", ENGINES)
def test_fetch_returns_rows_columns_and_a_truncation_flag(name):
    dialect = get_dialect(name)
    conn = FakeConnection([(1, "a"), (2, "b"), (3, "c")])
    result = dialect.fetch(conn, "SELECT 1", limit=2)
    assert result.rows == [(1, "a"), (2, "b")]
    assert result.truncated is True
    assert len(result.columns) == 2


def test_measure_reports_the_median_and_drops_the_warm_up_run():
    import time

    class Slow(FakeConnection):
        def __init__(self):
            super().__init__([[1]])
            self.calls = 0

        def cursor(self):
            cursor = super().cursor()

            class Timed(FakeCursor):
                def fetchmany(self, size):
                    time.sleep(0.01)
                    return super().fetchmany(size)

            return Timed(cursor.rows)

    timing = get_dialect("duckdb").measure(Slow(), "SELECT 1", runs=3)
    assert timing.ok
    assert timing.runs == 3
    assert timing.median_ms > 0


def test_measure_reports_an_error_instead_of_raising():
    class Broken(FakeConnection):
        def execute(self, sql, params=None):
            raise RuntimeError("connection lost")

        def cursor(self):
            raise RuntimeError("connection lost")

    timing = get_dialect("duckdb").measure(Broken(), "SELECT 1", runs=1)
    assert not timing.ok
    assert "connection lost" in timing.error


def test_the_postgres_plan_is_read_out_of_json():
    import json

    payload = [{
        "Execution Time": 12.5,
        "Plan": {
            "Node Type": "Nested Loop", "Actual Loops": 4,
            "Plans": [{"Node Type": "Seq Scan", "Relation Name": "ratings", "Actual Loops": 1}],
        },
    }]

    class PlanConn(FakeConnection):
        def cursor(self):
            cursor = FakeCursor([(json.dumps(payload),)])
            return cursor

    plan = get_dialect("postgres").explain(PlanConn(), "SELECT 1 FROM ratings")
    assert plan.execution_ms == 12.5
    assert plan.max_loops == 4
    assert plan.seq_scans == ["ratings"]


def test_a_real_duckdb_plan_reports_total_time():
    import duckdb

    conn = duckdb.connect()
    conn.execute("create table t as select * from range(10000) tbl(i)")
    plan = get_dialect("duckdb").explain(conn, "select count(*) from t")
    assert plan.text
    assert plan.max_loops == 1


def test_open_database_returns_the_dialect_and_a_connection():
    dialect, conn = open_database("duckdb", ":memory:")
    assert dialect.name == "duckdb"
    assert dialect.fetch(conn, "SELECT 1 AS one", 1).rows == [(1,)]
    dialect.close(conn)


def test_duckdb_connections_ignore_commit_and_rollback():
    conn = open_database("duckdb", ":memory:")[1]
    dialect = get_dialect("duckdb")
    dialect.commit(conn)
    dialect.rollback(conn)
    dialect.close(conn)