"""Timing candidates and ranking them.

Speed is wall-clock, median of several runs, because that is the only measure
available on all three engines and it is what a user actually waits for. Plans
are consulted afterwards for the two structural facts timing cannot show: a node
that is rescanned, and a sequential scan of a large table that has an index.

The original is measured on the same connection with the same number of runs, so
a speedup is always a like-for-like comparison. A candidate that is correct but
slower than the original is kept and ranked last rather than dropped: on some
queries the honest answer is that the database was already doing better.
"""
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlrewriter.catalog import Catalog
from sqlrewriter.dialects import Dialect, Timing
from sqlrewriter.equivalence import Equivalence
from sqlrewriter.rewrite import Candidate, RewriteResult

SEQSCAN_ROWS = 50_000


@dataclass
class Measurement:
    """How one statement performed, and whether it may be ranked."""

    label: str
    sql: str
    median_ms: float = 0.0
    runs: int = 0
    speedup: float | None = None
    equivalent: bool | None = None
    error: str | None = None
    max_loops: int = 1
    seq_scans: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def rankable(self) -> bool:
        """Correct enough to rank: provably equal, or not checked against a sample."""
        return self.ok and self.equivalent is not False


@dataclass
class QueryReport:
    """The full story for one query."""

    qid: str
    description: str
    original_sql: str
    baseline: Measurement | None = None
    candidates: list[Measurement] = field(default_factory=list)
    plan: str = ""
    error: str | None = None

    @property
    def ranked(self) -> list[Measurement]:
        return sorted(
            (c for c in self.candidates if c.rankable), key=lambda m: m.median_ms
        )

    @property
    def best(self) -> Measurement | None:
        return self.ranked[0] if self.ranked else None


def time_statement(
    dialect: Dialect, conn, sql: str, label: str, runs: int = 5
) -> Measurement:
    timing: Timing = dialect.measure(conn, sql, runs)
    return Measurement(label, sql, timing.median_ms, timing.runs, error=timing.error)


def inspect_plan(dialect: Dialect, conn, sql: str, catalog: Catalog, measurement: Measurement) -> None:
    """Fill in loop counts and suspicious scans from the plan, if available."""
    try:
        plan = dialect.explain(conn, sql)
    except Exception:  # noqa: BLE001 - a plan is a bonus, not a requirement
        return
    measurement.max_loops = plan.max_loops
    measurement.seq_scans = [
        f"{name} ({catalog.rows[name]:,} rows)"
        for name in plan.seq_scans
        if catalog.rows.get(name, 0) >= SEQSCAN_ROWS
    ]


def check_equivalence(
    dialect: Dialect,
    conn,
    candidate: Candidate,
    original_sql: str,
    *,
    catalog: Catalog | None = None,
    sample_schema: str | None = None,
    limit: int = 200_000,
) -> Equivalence | None:
    """Run the candidate against the original, on the sample when there is one."""
    from sqlrewriter.equivalence import compare

    if not candidate.accepted or not candidate.sql:
        return None
    return compare(
        dialect, conn, original_sql, candidate.sql,
        catalog=catalog, schema=sample_schema, limit=limit,
    )


def measure_query(
    dialect: Dialect,
    conn,
    result: RewriteResult,
    catalog: Catalog,
    *,
    runs: int = 5,
    sample_schema: str | None = None,
    compare_limit: int = 200_000,
    time_limit_ms: float | None = None,
) -> QueryReport:
    """Verify every accepted candidate, then time the survivors and the original.

    ``time_limit_ms`` is a budget on the original. It is probed with a single run
    first: a query that turns out to take an hour does not need to be timed five
    more times, and its speedup is simply unknown. Zero or ``None`` means no
    budget, and the original is timed like any other statement.
    """
    report = QueryReport(result.qid, result.description, result.original_sql, plan=result.plan)
    if result.error:
        report.error = result.error
        return report

    budget = time_limit_ms if time_limit_ms and time_limit_ms > 0 else None
    if budget is None:
        baseline = time_statement(dialect, conn, result.original_sql, "original", runs)
    else:
        baseline = time_statement(dialect, conn, result.original_sql, "original", 1)
        if baseline.ok and baseline.median_ms >= budget:
            baseline.error = (
                f"original takes {baseline.median_ms:.0f}ms, at or above the {budget:.0f}ms budget"
            )
        elif baseline.ok:
            baseline = time_statement(dialect, conn, result.original_sql, "original", runs)
    report.baseline = baseline

    for candidate in result.accepted:
        outcome = check_equivalence(
            dialect, conn, candidate, result.original_sql,
            catalog=catalog, sample_schema=sample_schema, limit=compare_limit,
        )
        measurement = time_statement(dialect, conn, candidate.sql, candidate.label, runs)
        if outcome is not None:
            candidate.equivalence = outcome
            measurement.equivalent = outcome.equal
            if outcome.equal is False:
                measurement.error = outcome.describe()
        if baseline and baseline.ok and measurement.ok and baseline.median_ms:
            measurement.speedup = baseline.median_ms / measurement.median_ms if measurement.median_ms else None
        if measurement.ok:
            inspect_plan(dialect, conn, candidate.sql, catalog, measurement)
        report.candidates.append(measurement)
    return report


def _row(reports: list[QueryReport]) -> dict[str, Any]:
    verified = sum(
        1 for r in reports for m in r.candidates if m.rankable and m.equivalent
    )
    attempted = sum(1 for r in reports for m in r.candidates if m.ok)
    speedups = [m.speedup for r in reports for m in r.candidates if m.speedup and m.rankable]
    return {
        "queries": len(reports),
        "candidates_timed": attempted,
        "candidates_correct": verified,
        "faster_than_original": sum(1 for s in speedups if s > 1.05),
        "median_speedup": round(statistics.median(speedups), 3) if speedups else None,
        "best_speedup": round(max(speedups), 3) if speedups else None,
    }


def render_table(reports: list[QueryReport]) -> str:
    """One line per query, fastest surviving candidate first."""
    lines = [
        f"{'QID':<6}{'original ms':>12}{'best ms':>10}{'speedup':>9}  {'best candidate':<16}{'notes'}"
    ]
    for report in reports:
        base = report.baseline.median_ms if report.baseline and report.baseline.ok else None
        best = report.best
        if best is None:
            reason = report.error or (report.candidates[0].error if report.candidates else "no candidate passed")
            lines.append(f"{report.qid:<6}{_ms(base):>12}{'-':>10}{'-':>9}  {'-':<16}{reason[:60]}")
            continue
        notes = ", ".join(filter(None, [f"{len(report.candidates)} tried", best.error or ""]))
        lines.append(
            f"{report.qid:<6}{_ms(base):>12}{_ms(best.median_ms):>10}"
            f"{_speed(best.speedup):>9}  {best.label:<16}{notes[:60]}"
        )
    summary = _row(reports)
    lines.append(
        f"\n{summary['queries']} queries | {summary['candidates_correct']}/{summary['candidates_timed']} "
        f"candidates correct on the sample | {summary['faster_than_original']} faster than the original"
        + (f" | median speedup {summary['median_speedup']}x" if summary["median_speedup"] else "")
    )
    return "\n".join(lines)


def _ms(value: float | None) -> str:
    return f"{value:,.1f}" if value else "-"


def _speed(value: float | None) -> str:
    return f"{value:.2f}x" if value else "-"


def to_json(reports: list[QueryReport]) -> str:
    payload = {
        "summary": _row(reports),
        "queries": [
            {
                "qid": r.qid,
                "description": r.description,
                "original_sql": r.original_sql,
                "plan": r.plan,
                "baseline_ms": r.baseline.median_ms if r.baseline else None,
                "baseline_error": r.baseline.error if r.baseline else None,
                "candidates": [
                    {
                        "label": m.label,
                        "sql": m.sql,
                        "median_ms": m.median_ms,
                        "runs": m.runs,
                        "speedup": m.speedup,
                        "equivalent": m.equivalent,
                        "max_loops": m.max_loops,
                        "seq_scans": m.seq_scans,
                        "error": m.error,
                    }
                    for m in r.candidates
                ],
            }
            for r in reports
        ],
    }
    return json.dumps(payload, indent=2)


def save_json(reports: list[QueryReport], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_json(reports), encoding="utf-8")
    return target