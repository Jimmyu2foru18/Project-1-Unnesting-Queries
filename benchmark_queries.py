"""Benchmark nested SQL against the LLM-rewritten unnested version."""
import argparse
import json
import os
import re
import statistics
from contextlib import closing
from pathlib import Path
from typing import Any, NamedTuple

import psycopg2

DIR = Path(__file__).resolve().parent
NESTED_SQL, UNNESTED_SQL = (DIR / name for name in ("nested_queries.sql", "unnested_queries.sql"))
EXPLAIN = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
BLOCK = re.compile(r"^--\s*(Q\d+)\b[^:]*:[^\n]*\n((?:.*?\n)?(?!--)[^\n]*);[ \t]*(?=\n|\Z)", re.MULTILINE | re.DOTALL)


class Stats(NamedTuple):
    median_ms: float
    buffers: int
    rescan_loops: int


def parse_catalog(path: Path) -> dict[str, str]:
    """Map query id -> SQL from a ``-- Qnn: ...`` annotated SQL file.

    A block ends at the first ``;`` closing a line of SQL, never one inside a comment.
    """
    catalog = {}
    for qid, body in BLOCK.findall(path.read_text(encoding="utf-8")):
        sql = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("--")).strip()
        if sql:
            catalog[qid] = sql
    return catalog


def max_loops(plan: dict[str, Any]) -> int:
    """Largest Actual Loops in the plan; above 1 means something was rescanned."""
    return max([int(plan.get("Actual Loops", 1))] + [max_loops(c) for c in plan.get("Plans", [])])


def summarize_plan(payload: Any) -> Stats:
    root = (json.loads(payload) if isinstance(payload, (str, bytes)) else payload)[0]
    hit, read = int(root.get("Shared Hit Blocks", 0)), int(root.get("Shared Read Blocks", 0))
    return Stats(float(root.get("Execution Time", 0.0)), hit + read, max_loops(root["Plan"]))


def measure(conn: Any, sql: str, runs: int = 5) -> Stats:
    """Run EXPLAIN ANALYZE ``runs`` times; report the median, dropping the warm-up."""
    samples, statement = [], EXPLAIN + sql.rstrip().rstrip(";")
    with conn.cursor() as cur:
        for _ in range(runs):
            cur.execute(statement)
            samples.append(summarize_plan(cur.fetchone()[0]))
    timed = samples[1:] if len(samples) > 1 else samples
    return Stats(statistics.median(s.median_ms for s in timed),
                 statistics.median(s.buffers for s in samples),
                 max(s.rescan_loops for s in samples))


def benchmark(conn: Any, runs: int = 5) -> list[tuple[str, Stats | None, Stats | None, str | None]]:
    """Benchmark every id present in both catalogs; a database error becomes the result."""
    nested_all, unnested_all = parse_catalog(NESTED_SQL), parse_catalog(UNNESTED_SQL)
    results = []
    for qid in [q for q in nested_all if q in unnested_all]:
        try:
            nested = measure(conn, nested_all[qid], runs)
            results.append((qid, nested, measure(conn, unnested_all[qid], runs), None))
        except Exception as err:  # noqa: BLE001 - a bad rewrite must not stop the run
            results.append((qid, None, None, str(err).strip()))
    return results


def render(results: list[Any], runs: int) -> str:
    lines = [f"{'QID':<5}{'nested ms':>11}{'unnested ms':>13}{'speedup':>9}{'buffers n/u':>14}{'u.loops':>9}"]
    for qid, n, u, err in results:
        speed = n.median_ms / u.median_ms if n and u and n.median_ms else None
        nested, unnested = (f"{n.median_ms:.1f}" if n else "-"), (f"{u.median_ms:.1f}" if u else "-")
        buffers, loops = (f"{n.buffers:,}/{u.buffers:,}" if n and u else "-", f"{u.rescan_loops:,}" if u else "-")
        lines.append(f"{qid:<5}{nested:>11}{unnested:>13}{f'{speed:.2f}x' if speed else '-':>9}{buffers:>14}{loops:>9}  {err or ''}")
    wins = sum(1 for _, n, u, _ in results if n and u and n.median_ms / u.median_ms > 1.05)
    lines.append(f"\n{len(results)} queries | unnested faster: {wins} | errors: {sum(1 for r in results if r[3])} | {runs} runs each")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare nested queries against their unnested rewrites.")
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="Postgres DSN (or set DATABASE_URL)")
    parser.add_argument("--runs", type=int, default=5, help="executions per query (first is warm-up)")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("no database given: pass --dsn or set DATABASE_URL")

    conn = psycopg2.connect(args.dsn)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 60000")
    with closing(conn):
        print(render(benchmark(conn, args.runs), args.runs))


if __name__ == "__main__":
    main()
