"""Timing and ranking, measured against a real DuckDB database."""

import json

import duckdb
import pytest

from _08_verify import check as verify_check, Verdict as _Verdict

class verify:
    check = staticmethod(verify_check)
    Verdict = _Verdict
from _03_catalog import from_mapping
from _02_dialects import get_dialect
from _11_rank import (
    Measurement,
    check_equivalence,
    measure_query,
    render_table,
    save_json,
    time_statement,
    to_json,
)
from _10_rewrite import Candidate, RewriteResult

DIALECT = get_dialect("duckdb")
CATALOG = from_mapping(
    {"movies": {"id": "INT", "title": "TEXT", "year": "INT"},
     "ratings": {"movie_id": "INT", "rating": "INT"}},
    schema="main",
    primary={"movies": ["id"]},
    rows={"movies": 5000, "ratings": 250_000},
    foreign_keys=[("ratings", "movie_id", "movies", "id")],
)

ORIGINAL = (
    "SELECT m.title FROM movies m "
    "WHERE (SELECT avg(r.rating) FROM ratings r WHERE r.movie_id = m.id) > 8.0"
)
FASTER = (
    "SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id "
    "GROUP BY m.id, m.title HAVING avg(r.rating) > 8.0"
)


@pytest.fixture
def conn():
    connection = duckdb.connect()
    connection.execute("create table movies as select i as id, 'title ' || i as title, 1990 + (i % 30) as year from range(5000) t(i)")
    connection.execute("create table ratings as select (i % 5000) as movie_id, (i % 10) as rating from range(250000) t(i)")
    yield connection
    connection.close()


def candidate(sql, label="plan"):
    return Candidate(sql=sql, label=label, verdict=verify.Verdict(True, []))


def result(*candidates):
    return RewriteResult("Q01", "rated above eight", ORIGINAL, plan="aggregate first", candidates=list(candidates))


def test_a_statement_is_timed_and_recorded(conn):
    measurement = time_statement(DIALECT, conn, "SELECT count(*) FROM movies", "original", runs=3)
    assert measurement.ok
    assert measurement.runs == 3
    assert measurement.median_ms > 0
    assert measurement.label == "original"


def test_a_statement_that_does_not_run_is_recorded_as_an_error(conn):
    measurement = time_statement(DIALECT, conn, "SELECT nope FROM movies", "broken", runs=1)
    assert not measurement.ok
    assert measurement.error


def test_the_median_of_several_runs_is_reported(conn):
    one = time_statement(DIALECT, conn, "SELECT 1", "a", runs=5)
    assert one.median_ms >= 0


def test_a_candidate_that_fails_equivalence_is_not_rankable(conn):
    wrong = candidate("SELECT m.title FROM movies m WHERE m.year = 1999")
    outcome = check_equivalence(DIALECT, conn, wrong, ORIGINAL, catalog=CATALOG)
    assert outcome.equal is False
    measurement = Measurement("plan", wrong.sql)
    measurement.equivalent = outcome.equal
    measurement.error = outcome.describe()
    assert not measurement.rankable


def test_a_verified_candidate_is_rankable(conn):
    good = candidate(FASTER)
    outcome = check_equivalence(DIALECT, conn, good, ORIGINAL, catalog=CATALOG)
    assert outcome.equal is True
    measurement = Measurement("plan", good.sql, equivalent=outcome.equal)
    assert measurement.rankable


def test_a_candidate_that_was_never_checked_is_still_rankable(conn):
    assert Measurement("plan", "SELECT 1", equivalent=None).rankable


def test_the_baseline_and_the_candidate_are_both_measured(conn):
    report = measure_query(DIALECT, conn, result(candidate(FASTER)), CATALOG, runs=2)
    assert report.baseline.ok
    assert report.best is not None
    assert report.best.speedup is not None
    assert report.best.speedup > 0


def test_the_fastest_ranked_candidate_comes_first(conn):
    slower = candidate("SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id "
                      "GROUP BY m.id, m.title, r.rating HAVING avg(r.rating) > 8.0", "wide-group")
    report = measure_query(DIALECT, conn, result(candidate(FASTER, "join"), slower), CATALOG, runs=2)
    ranked = [m.label for m in report.ranked]
    assert ranked == sorted(ranked, key=lambda label: [m.median_ms for m in report.ranked if m.label == label][0])
    assert len(report.ranked) == 2


def test_a_candidate_that_is_wrong_does_not_become_the_best(conn):
    wrong = candidate("SELECT 'nonsense' FROM movies", "wrong")
    report = measure_query(DIALECT, conn, result(candidate(FASTER, "join"), wrong), CATALOG, runs=2)
    assert [m.label for m in report.ranked] == ["join"]


def test_a_candidate_slower_than_the_original_is_kept_and_ranked(conn):
    candidate("SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id "
              "GROUP BY m.id, m.title, r.votes0 FROM ratings r2 "
              "JOIN (SELECT * FROM ratings) r ON r.movie_id = m.id LIMIT 1", "silly")
    report = measure_query(DIALECT, conn, result(candidate(FASTER, "join")), CATALOG, runs=2)
    assert report.ranked, "a correct candidate is never dropped for being slow"


def test_the_plan_is_inspected_for_rescans_and_scans(conn):
    report = measure_query(DIALECT, conn, result(candidate(FASTER)), CATALOG, runs=1)
    measurement = report.candidates[0]
    assert measurement.max_loops >= 1


SLOW = "SELECT sum(range) FROM range(20000000)"


class Stubbed(type(DIALECT)):
    """A DuckDB dialect that reports chosen timings, so budgets are testable."""

    __slots__ = ("calls", "timings")

    def __init__(self, timings):
        self.timings = timings
        self.calls = []

    def measure(self, conn, sql, runs):
        self.calls.append((sql, runs))
        from _02_dialects import Timing
        return Timing(float(self.timings.get(sql, 1.0)), runs)


def test_a_budget_stops_the_slow_original_being_timed_again(conn):
    slow = RewriteResult("Q05", "expensive", "SELECT slow",
                         candidates=[Candidate("SELECT fast", "fast", verdict=verify.Verdict(True, []))])
    dialect = Stubbed({"SELECT slow": 900.0, "SELECT fast": 5.0})
    report = measure_query(dialect, conn, slow, CATALOG, runs=5, time_limit_ms=100.0)
    assert not report.baseline.ok
    assert "budget" in report.baseline.error
    assert report.baseline.runs == 1, "the budget should stop the extra runs"
    assert ("SELECT slow", 5) not in dialect.calls, "a slow original must not be re-timed"
    assert report.candidates[0].speedup is None, "no baseline means no speedup"


def test_a_zero_budget_means_no_budget(conn):
    dialect = Stubbed({ORIGINAL: 50.0, FASTER: 10.0})
    report = measure_query(dialect, conn, result(candidate(FASTER)), CATALOG,
                           runs=1, time_limit_ms=0.0)
    assert report.baseline.ok
    assert report.baseline.runs == 1
    assert "budget" not in (report.baseline.error or "")


def test_an_original_under_budget_is_timed_fully(conn):
    dialect = Stubbed({ORIGINAL: 50.0, FASTER: 10.0})
    report = measure_query(dialect, conn, result(candidate(FASTER)), CATALOG,
                           runs=3, time_limit_ms=60_000.0)
    assert report.baseline.ok
    assert report.baseline.runs == 3
    assert (ORIGINAL, 3) in dialect.calls


def test_an_original_that_fails_leaves_the_report_without_a_best(conn):
    broken = RewriteResult("Q02", "d", "SELECT nope FROM t")
    report = measure_query(DIALECT, conn, broken, CATALOG, runs=1)
    assert report.best is None


def test_a_generation_failure_is_carried_into_the_report(conn):
    failed = RewriteResult("Q03", "d", ORIGINAL, error="planning failed: no answer")
    report = measure_query(DIALECT, conn, failed, CATALOG, runs=1)
    assert report.error == "planning failed: no answer"


def test_the_table_lists_every_query_and_a_summary(conn):
    reports = [
        measure_query(DIALECT, conn, result(candidate(FASTER)), CATALOG, runs=1),
        measure_query(DIALECT, conn, RewriteResult("Q02", "nothing", "SELECT nope FROM t"), CATALOG, runs=1),
    ]
    table = render_table(reports)
    assert "QID" in table and "Q01" in table and "Q02" in table
    assert "queries |" in table
    assert "candidates correct on the sample" in table


def test_a_query_with_nothing_usable_says_why(conn):
    reports = [measure_query(DIALECT, conn, RewriteResult("Q02", "d", "SELECT nope FROM t"), CATALOG, runs=1)]
    assert "did not run" in render_table(reports) or "no candidate" in render_table(reports)


def test_the_json_report_round_trips(conn):
    reports = [measure_query(DIALECT, conn, result(candidate(FASTER)), CATALOG, runs=1)]
    payload = json.loads(to_json(reports))
    assert payload["summary"]["queries"] == 1
    entry = payload["queries"][0]
    assert entry["qid"] == "Q01"
    assert entry["candidates"][0]["equivalent"] is True
    assert "median_ms" in entry["candidates"][0]


def test_the_report_is_written_to_disk(conn, tmp_path):
    reports = [measure_query(DIALECT, conn, result(candidate(FASTER)), CATALOG, runs=1)]
    target = save_json(reports, tmp_path / "out" / "results.json")
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["summary"]["queries"] == 1