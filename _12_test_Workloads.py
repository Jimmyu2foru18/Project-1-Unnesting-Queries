"""Workload loading, including the annotation comment trap."""

import sys
import pytest

from _12_workloads import Workload, discover, load, parse_sql

workloads = sys.modules[__name__]
workloads.parse_sql = parse_sql
workloads.load = load
workloads.discover = discover
workloads.Workload = Workload


SIMPLE = """-- Workload: demo
-- a free comment line before anything

-- D01: first query
-- Type: correlated subquery (scalar, AVG)
-- Complexity: low
SELECT a FROM t1;

-- D02: second query
SELECT b
FROM t2
WHERE c = 1;
"""

ANNOTATION_WITH_SEMICOLON = """-- W01: has an annotation ending in a semicolon
-- Description: the subquery counts directors for each movie;
-- Complexity: high
SELECT a
FROM t1
WHERE b IN (SELECT c FROM t2);

-- W02: second
SELECT 1 FROM t3;
"""


def test_queries_are_read_with_ids_and_descriptions():
    parsed = workloads.parse_sql(SIMPLE)
    assert [q.qid for q in parsed] == ["D01", "D02"]
    assert parsed[0].description == "first query"
    assert parsed[1].description == "second query"


def test_annotation_comments_are_kept_out_of_the_statement():
    statement = workloads.parse_sql(SIMPLE)[0].sql
    assert statement == "SELECT a FROM t1"
    assert "--" not in statement


def test_an_annotation_line_ending_in_a_semicolon_does_not_end_the_query():
    parsed = workloads.parse_sql(ANNOTATION_WITH_SEMICOLON)
    assert [q.qid for q in parsed] == ["W01", "W02"]
    assert "SELECT c FROM t2" in parsed[0].sql
    assert "Description" not in parsed[0].sql


def test_a_trailing_semicolon_is_stripped():
    assert workloads.parse_sql(SIMPLE)[0].sql.endswith("FROM t1")


def test_a_query_without_a_trailing_semicolon_is_still_captured():
    text = "-- Z1: no terminator\nSELECT 1 FROM t"
    assert workloads.parse_sql(text)[0].sql == "SELECT 1 FROM t"


def test_a_code_fence_is_stripped():
    text = "-- Z2: fenced\n```sql\nSELECT 1 FROM t\n```"
    assert workloads.parse_sql(text)[0].sql == "SELECT 1 FROM t"


def test_a_header_with_no_description_still_parses():
    parsed = workloads.parse_sql("-- Z3:\nSELECT 1 FROM t")
    assert parsed[0].qid == "Z3"
    assert parsed[0].description == ""


def test_a_file_with_no_headers_yields_nothing():
    assert workloads.parse_sql("SELECT 1 FROM t;") == []


@pytest.fixture
def workload_dir(tmp_path):
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "queries.sql").write_text(SIMPLE, encoding="utf-8")
    (directory / "workload.json").write_text(
        '{"name": "renamed", "engine": "duckdb", "schema": "main", '
        '"dsn": "data/x.duckdb", "hints": "be careful", "tags": ["a"]}',
        encoding="utf-8",
    )
    return directory


def test_metadata_is_read_from_workload_json(workload_dir):
    workload = workloads.load(workload_dir)
    assert workload.name == "renamed"
    assert workload.engine == "duckdb"
    assert workload.schema == "main"
    assert workload.dsn == "data/x.duckdb"
    assert workload.hints == "be careful"
    assert workload.tags == ["a"]


def test_a_workload_without_metadata_still_loads(workload_dir):
    (workload_dir / "workload.json").unlink()
    workload = workloads.load(workload_dir)
    assert workload.engine == ""
    assert len(workload) == 2


def test_a_bare_sql_file_can_be_loaded_directly(tmp_path):
    path = tmp_path / "queries.sql"
    path.write_text(SIMPLE, encoding="utf-8")
    assert len(workloads.load(path)) == 2


def test_limit_and_only_narrow_the_query_set(workload_dir):
    workload = workloads.load(workload_dir)
    assert [q.qid for q in workload.limit(1)] == ["D01"]
    assert [q.qid for q in workload.only(["D02"])] == ["D02"]
    assert [q.qid for q in workload.only([])] == ["D01", "D02"]
    assert len(workload.limit(99)) == 2


def test_discover_finds_every_workload_under_a_root(tmp_path):
    for name in ("one", "two"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "queries.sql").write_text(SIMPLE, encoding="utf-8")
    assert {w.name for w in workloads.discover(tmp_path)} == {"one", "two"}


def test_discover_returns_nothing_for_a_missing_root(tmp_path):
    assert workloads.discover(tmp_path / "absent") == []


def test_the_bundled_workloads_all_parse():
    found = workloads.discover("workloads")
    assert found, "no workloads found under workloads/"
    for workload in found:
        assert len(workload) > 0, f"{workload.name} parsed to no queries"
        for query in workload:
            assert query.sql.strip().lower().startswith(("select", "with")), (
                workload.name, query.qid, query.sql[:60]
            )
            assert query.description, f"{workload.name}/{query.qid} has no description"


def test_every_bundled_query_only_uses_tables_in_its_own_schema():
    from _03_catalog import tables_in

    known = {"movies", "ratings", "people", "stars", "directors"}
    for workload in workloads.discover("workloads"):
        for query in workload:
            assert tables_in(query.sql, "duckdb") <= known, (workload.name, query.qid)