"""The command line, run against a real DuckDB file."""

import json

import duckdb
import pytest

from _12_workloads import parse_sql
import _13_cli as cli

QUERIES = """-- C01: correlated average
-- Description: movies rated above eight
SELECT m.title FROM movies m
WHERE (SELECT avg(r.rating) FROM ratings r WHERE r.movie_id = m.id) > 8.0;

-- C02: join then group
SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id
GROUP BY m.id, m.title HAVING avg(r.rating) > 8.0;
"""


@pytest.fixture
def dsn(tmp_path):
    path = tmp_path / "cli.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("create table movies (id BIGINT PRIMARY KEY, title VARCHAR, year BIGINT)")
    conn.execute("create table ratings (movie_id BIGINT REFERENCES movies(id), rating BIGINT)")
    conn.execute("insert into movies select i, 'title ' || i, 1990 + (i % 30) from range(3000) t(i)")
    conn.execute("insert into ratings select (i % 3000), (i % 10) from range(60000) t(i)")
    conn.close()
    return str(path)


@pytest.fixture
def workload(tmp_path):
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "queries.sql").write_text(QUERIES, encoding="utf-8")
    (directory / "workload.json").write_text(
        json.dumps({"engine": "duckdb", "schema": "main"}), encoding="utf-8"
    )
    return directory


def test_the_parser_exposes_the_four_subcommands():
    parser = cli.build_parser()
    for command in ("show", "catalog", "verify", "rewrite", "compare"):
        assert parser.parse_args([command] + (
            ["--original", "a", "--candidate", "b"] if command == "verify" else (
                ["a.json", "b.json"] if command == "compare" else []
            )
        )).command == command


def test_a_missing_subcommand_is_an_error():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_show_prints_the_schema_block(dsn, capsys):
    code = cli.main(["show", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
                     "--sql", "SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id"])
    assert code == 0
    out = capsys.readouterr().out
    assert "movies" in out and "ratings" in out


def test_show_reports_how_much_it_introspected(dsn, capsys):
    cli.main(["show", "--engine", "duckdb", "--dsn", dsn, "--schema", "main"])
    out = capsys.readouterr().out
    assert "tables: 2" in out
    assert "measured: 2" in out
    assert "PRIMARY KEY" in out
    assert "ratings 60,000" in out


def test_show_renders_every_query_in_a_workload(dsn, workload, capsys):
    code = cli.main(["show", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
                     "--workload", str(workload)])
    assert code == 0
    out = capsys.readouterr().out
    assert "===== C01" in out and "===== C02" in out


def test_catalog_emits_valid_json(dsn, capsys):
    assert cli.main(["catalog", "--engine", "duckdb", "--dsn", dsn, "--schema", "main"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["columns"]) == {"movies", "ratings"}
    assert payload["foreign_keys"] == [["ratings", "movie_id", "movies", "id"]]


def test_verify_returns_zero_when_the_results_match(dsn, capsys):
    code = cli.main(["verify", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
                     "--original", "SELECT title FROM movies WHERE year = 1995",
                     "--candidate", "SELECT title FROM movies WHERE year = 1995 ORDER BY title"])
    assert code == 0
    assert "equal" in capsys.readouterr().out


def test_verify_returns_one_when_the_results_differ(dsn, capsys):
    code = cli.main(["verify", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
                     "--original", "SELECT title FROM movies WHERE year = 1995",
                     "--candidate", "SELECT title FROM movies WHERE year = 1996"])
    assert code == 1
    assert "different" in capsys.readouterr().out


def test_verify_can_compare_inside_a_sample(dsn, capsys):
    code = cli.main(["verify", "--engine", "duckdb", "--dsn", dsn, "--schema", "main", "--sample",
                     "--original", "SELECT title FROM movies WHERE year = 1995",
                     "--candidate", "SELECT title FROM movies WHERE year = 1995 ORDER BY title"])
    assert code == 0
    assert "sample" in capsys.readouterr().out


def test_a_missing_connection_string_is_refused(capsys):
    with pytest.raises(SystemExit) as err:
        cli.main(["catalog", "--engine", "duckdb", "--dsn", ""])
    assert "no connection" in str(err.value)


def test_rewrite_runs_the_whole_pipeline_with_the_echo_model(dsn, workload, capsys):
    code = cli.main([
        "rewrite", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
        "--workload", str(workload), "--model", "echo/SELECT 1",
        "--variants", "1", "--attempts", "1", "--runs", "1",
        "--no-sample", "--no-write", "--limit", "1",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "engine duckdb" in out
    assert "C01" in out
    assert "queries |" in out


def test_rewrite_builds_a_sample_by_default(dsn, workload, capsys):
    cli.main([
        "rewrite", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
        "--workload", str(workload), "--model", "echo/SELECT 1",
        "--variants", "1", "--attempts", "1", "--runs", "1", "--sample-percent", "50",
        "--no-write", "--limit", "1",
    ])
    assert "sample built" in capsys.readouterr().out


def test_rewrite_writes_a_json_report(dsn, workload, tmp_path, capsys):
    out_file = tmp_path / "results" / "run.json"
    cli.main([
        "rewrite", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
        "--workload", str(workload), "--model", "echo/SELECT 1",
        "--variants", "1", "--attempts", "1", "--runs", "1", "--no-sample", "--no-write",
        "--limit", "1", "--out", str(out_file),
    ])
    assert out_file.exists()
    assert json.loads(out_file.read_text(encoding="utf-8"))["summary"]["queries"] == 1


def test_rewrite_reports_a_query_whose_generation_failed(dsn, workload, capsys):
    class Broken:
        name = "broken"

        def chat(self, *a, **k):
            from _06_llm import ModelError
            raise ModelError("no answer")

    original = cli.parse_model
    cli.parse_model = lambda spec: Broken()
    try:
        cli.main([
            "rewrite", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
            "--workload", str(workload), "--variants", "1", "--no-sample", "--no-write",
        ])
    finally:
        cli.parse_model = original
    assert "planning failed" in capsys.readouterr().out


def test_an_empty_workload_is_refused(dsn, tmp_path, capsys):
    directory = tmp_path / "empty"
    directory.mkdir()
    (directory / "queries.sql").write_text("-- nothing here\n", encoding="utf-8")
    with pytest.raises(SystemExit) as err:
        cli.main(["rewrite", "--engine", "duckdb", "--dsn", dsn, "--schema", "main",
                  "--workload", str(directory), "--no-write"])
    assert "no queries" in str(err.value)


def test_rewrites_are_rendered_in_the_workload_sql_shape():
    from _11_rank import Measurement, QueryReport

    report = QueryReport("C01", "rated above eight", "SELECT 1",
                         baseline=Measurement("original", "SELECT 1", median_ms=100.0),
                         candidates=[Measurement("plan", "SELECT 2", median_ms=20.0, equivalent=True)])
    text = cli.render_rewrites([report])
    assert text.startswith("-- Verified rewrites")
    assert "-- C01: rated above eight" in text
    assert "-- original: 100.0ms | rewritten: 20.0ms | speedup: 5.00x" in text
    assert text.rstrip().endswith("SELECT 2;")


def test_a_failed_query_is_marked_rather_than_omitted():
    from _11_rank import QueryReport

    text = cli.render_rewrites([QueryReport("C09", "d", "SELECT 1")])
    assert "-- C09 FAILED" in text


def test_a_query_with_no_baseline_still_renders():
    from _11_rank import Measurement, QueryReport

    report = QueryReport("C01", "d", "SELECT 1", baseline=Measurement("original", "SELECT 1"),
                         candidates=[Measurement("plan", "SELECT 2", median_ms=5.0, equivalent=None)])
    assert "SELECT 2;" in cli.render_rewrites([report])


def test_a_rewrite_is_only_written_when_it_verified():
    from _11_rank import Measurement, QueryReport

    report = QueryReport("C01", "d", "SELECT 1",
                         baseline=Measurement("original", "SELECT 1", median_ms=1.0),
                         candidates=[Measurement("plan", "SELECT 2", median_ms=1.0, equivalent=False)])
    assert "FAILED" in cli.render_rewrites([report])


def test_the_bundled_workload_ids_are_unique():
    for workload_dir in ("workloads/imdb_nested", "workloads/imdb_join"):
        ids = [q.qid for q in parse_sql(open(f"{workload_dir}/queries.sql", encoding="utf-8").read())]
        assert len(ids) == len(set(ids))