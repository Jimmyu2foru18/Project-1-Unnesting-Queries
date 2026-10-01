"""Unit tests for the benchmark suite. No database required."""
import json

import benchmark_queries as bq
import pytest

SAMPLE = """\
-- Q14: Movies whose release year beat the previous year
-- The correlation counts rows with a strictly higher rating;
SELECT a
FROM t
WHERE a > 1;

-- Q15: Short one
SELECT b
FROM u;
"""


def catalog(tmp_path, text, name="catalog.sql"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def plan(ms, loops=1):
    return [{"Execution Time": ms, "Shared Hit Blocks": 300, "Shared Read Blocks": 40,
             "Plan": {"Actual Loops": loops}}]


class FakeConn:
    """Stands in for both a psycopg2 connection and its cursor."""

    def __init__(self, plans=(), fail_on=None):
        self.plans, self.fail_on, self.statements = list(plans), fail_on, []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement):
        self.statements.append(statement)
        if self.fail_on and self.fail_on in statement:
            raise RuntimeError(f'relation "{self.fail_on}" does not exist')

    def fetchone(self):
        return (self.plans.pop(0),)


def use_catalogs(monkeypatch, nested, unnested):
    monkeypatch.setattr(bq, "NESTED_SQL", "nested")
    monkeypatch.setattr(bq, "UNNESTED_SQL", "rewritten")
    monkeypatch.setattr(bq, "parse_catalog", lambda path: nested if path == "nested" else unnested)


def test_parse_catalog_keeps_sql_and_ignores_semicolons_in_comments(tmp_path):
    assert bq.parse_catalog(catalog(tmp_path, SAMPLE)) == {
        "Q14": "SELECT a\nFROM t\nWHERE a > 1",
        "Q15": "SELECT b\nFROM u",
    }
    text = "-- Q01 (unnested): A\nSELECT 1;"
    assert bq.parse_catalog(catalog(tmp_path, text, "gen.sql")) == {"Q01": "SELECT 1"}


@pytest.mark.parametrize("payload", [plan(12.5, 900), json.dumps(plan(12.5, 900))])
def test_summarize_plan_reads_time_buffers_and_rescans(payload):
    assert bq.summarize_plan(payload) == (12.5, 340, 900)


def test_summarize_plan_uses_the_largest_loop_count_and_defaults_blanks():
    assert bq.summarize_plan([{"Plan": {"Plans": [{"Actual Loops": 3}], "Actual Loops": 50}}]).rescan_loops == 50
    assert bq.summarize_plan([{"Plan": {}}]) == (0.0, 0, 1)


def test_measure_discards_the_warm_up_run_and_wraps_explain():
    conn = FakeConn([plan(100.0), plan(10.0), plan(12.0)])
    assert bq.measure(conn, "SELECT 1;  ", runs=3).median_ms == 11.0
    assert conn.statements == [bq.EXPLAIN + "SELECT 1"] * 3
    assert bq.measure(FakeConn([plan(5.0)]), "SELECT 1", runs=1).median_ms == 5.0


def test_benchmark_pairs_ids_and_records_a_database_error(monkeypatch):
    use_catalogs(monkeypatch, {"Q01": "a", "Q11": "b"}, {"Q01": "c", "Q11": "movie_directors"})
    conn = FakeConn([plan(1.0)] * 3, fail_on="movie_directors")
    results = bq.benchmark(conn, 1)
    assert [r[0] for r in results] == ["Q01", "Q11"]
    assert results[0][1] == (1.0, 340, 1)
    assert results[1][3] == 'relation "movie_directors" does not exist'


def test_render_reports_every_query_and_a_summary():
    rows = [("Q01", bq.Stats(100.0, 500, 9000), bq.Stats(10.0, 200, 1), None), ("Q11", None, None, "boom")]
    out = bq.render(rows, 5)
    assert "10.00x" in out and "boom" in out
    assert "2 queries | unnested faster: 1 | errors: 1" in out
