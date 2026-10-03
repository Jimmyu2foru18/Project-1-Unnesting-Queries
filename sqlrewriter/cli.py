"""The command line interface.

    python -m sqlrewriter show   --engine duckdb --dsn data/imdb.duckdb --schema main
    python -m sqlrewriter rewrite --engine postgres --dsn postgresql://... \\
                                 --model ollama/gpt-oss:20b --workload workloads/imdb_nested
    python -m sqlrewriter compare benchmarks/results/*.json

``rewrite`` runs the full experiment: sample the data, generate candidates,
check them against the sample, time the survivors, and rank them. ``show``
prints the schema block sent to the model, which is the quickest way to check
that introspection is working.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from sqlrewriter import catalog as catalog_mod
from sqlrewriter import rank, sampling, workloads
from sqlrewriter.dialects import get_dialect
from sqlrewriter.llm import parse_model
from sqlrewriter.rewrite import Rewriter
from sqlrewriter.strategies import get_strategy

DIR = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlrewriter", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def connection_args(p):
        p.add_argument("--engine", default="duckdb", help="postgres, mysql or duckdb")
        p.add_argument("--dsn", default=os.getenv("SQLRW_DSN", ""), help="connection string")
        p.add_argument("--schema", default="", help="schema to introspect")

    show = sub.add_parser("show", help="print the schema block sent to the model")
    connection_args(show)
    show.add_argument("--sql", default="", help="render the block for this query")
    show.add_argument("--budget", type=int, default=8, help="how many tables to show")
    show.add_argument("--workload", default="", help="render the block for each query in a workload")
    show.add_argument("--dump", default="", help="read the schema from a SQL dump instead")

    rewrite = sub.add_parser("rewrite", help="rewrite, verify and rank a workload")
    connection_args(rewrite)
    rewrite.add_argument("--workload", default="workloads/imdb_nested")
    rewrite.add_argument("--model", default="ollama/gpt-oss:20b")
    rewrite.add_argument("--strategy", default="existing", choices=["existing", "zero-shot", "one-shot", "reasoning"])
    rewrite.add_argument("--variants", type=int, default=3, help="candidates per query")
    rewrite.add_argument("--attempts", type=int, default=2, help="repair attempts per candidate")
    rewrite.add_argument("--schema-budget", type=int, default=8)
    rewrite.add_argument("--runs", type=int, default=5, help="timing runs per statement")
    rewrite.add_argument("--sample-percent", type=float, default=1.0)
    rewrite.add_argument("--sample-max-rows", type=int, default=200_000)
    rewrite.add_argument("--sample-schema", default=sampling.SAMPLE_SCHEMA)
    rewrite.add_argument("--no-sample", action="store_true", help="skip sampling and check on the full data")
    rewrite.add_argument("--compare-limit", type=int, default=200_000)
    rewrite.add_argument("--baseline-budget-ms", type=float, default=0.0,
                         help="skip timing the original above this many milliseconds")
    rewrite.add_argument("--limit", type=int, default=0)
    rewrite.add_argument("--only", default="", help="comma separated query ids")
    rewrite.add_argument("--hints", default="", help="extra guidance for the model")
    rewrite.add_argument("--out", default="", help="write a JSON report here")
    rewrite.add_argument("--no-write", action="store_true", help="do not write the best rewrites to SQL")

    verify_cmd = sub.add_parser("verify", help="check one candidate query against the original")
    connection_args(verify_cmd)
    verify_cmd.add_argument("--original", required=True)
    verify_cmd.add_argument("--candidate", required=True)
    verify_cmd.add_argument("--sample", action="store_true", help="compare inside the sample schema")

    catalog_cmd = sub.add_parser("catalog", help="dump the catalog as JSON")
    connection_args(catalog_cmd)

    compare_cmd = sub.add_parser("compare", help="compare strategy results from JSON files")
    compare_cmd.add_argument("files", nargs="+", help="JSON result files produced by rewrite")
    return parser


def _resolve_schema(args, dialect) -> str:
    return args.schema or dialect.default_schema


def _connect(args):
    dialect = get_dialect(args.engine)
    if not args.dsn:
        sys.exit("Error: no connection given; pass --dsn (for duckdb the default is ':memory:')")
    conn = dialect.connect(args.dsn)
    return dialect, conn


def cmd_show(args) -> int:
    if args.dump:
        cat = catalog_mod.from_dump(args.dump)
        schema = args.schema or cat.schema
    else:
        dialect, conn = _connect(args)
        schema = _resolve_schema(args, dialect)
        cat = catalog_mod.introspect(dialect, conn, schema)
        dialect.close(conn)

    if args.workload:
        for query in workloads.load(args.workload):
            print(f"===== {query.qid}: {query.description}")
            print(cat.render(query.sql, dialect.sqlglot, args.budget))
            print()
        return 0
    print(f"schema: {schema} | tables: {len(cat.columns)} | measured: {len(cat.rows)}")
    print()
    print(cat.render(args.sql or "SELECT 1", dialect.sqlglot, args.budget))
    return 0


def cmd_catalog(args) -> int:
    dialect, conn = _connect(args)
    schema = _resolve_schema(args, dialect)
    cat = catalog_mod.introspect(dialect, conn, schema)
    dialect.close(conn)
    print(json.dumps({
        "schema": cat.schema,
        "columns": cat.columns,
        "primary": cat.primary,
        "unique": {k: [list(g) for g in v] for k, v in cat.unique.items()},
        "foreign_keys": [list(k) for k in cat.foreign_keys],
        "indexed": {k: sorted(v) for k, v in cat.indexed.items()},
        "rows": cat.rows,
    }, indent=2))
    return 0


def cmd_verify(args) -> int:
    from sqlrewriter.equivalence import compare

    dialect, conn = _connect(args)
    schema = _resolve_schema(args, dialect)
    cat = catalog_mod.introspect(dialect, conn, schema)
    target = None
    if args.sample:
        print("building sample...", flush=True)
        stats = sampling.build_sample(dialect, conn, cat, percent=1.0)
        print(f"sample: {stats.describe()}")
        target = stats.schema
    outcome = compare(dialect, conn, args.original, args.candidate,
                      catalog=cat, schema=target)
    dialect.close(conn)
    print(f"{outcome.status}: {outcome.describe()}")
    return 0 if outcome.equal else 1


def cmd_rewrite(args) -> int:
    workload = workloads.load(args.workload)
    if args.only:
        workload = workload.only(args.only.split(","))
    if args.limit:
        workload = workload.limit(args.limit)
    if not workload.queries:
        sys.exit(f"Error: no queries in {args.workload}")

    dsn = args.dsn or workload.dsn
    if not dsn and args.engine == "duckdb":
        dsn = ":memory:"
    dialect = get_dialect(args.engine)
    schema = args.schema or workload.schema or dialect.default_schema
    if not dsn:
        sys.exit("Error: pass --dsn or set dsn in workload.json")

    conn = dialect.connect(dsn)
    cat = catalog_mod.introspect(dialect, conn, schema)
    print(f"engine {dialect.name} | schema {schema} | {len(cat.columns)} tables | workload {workload.name} | strategy {args.strategy}")

    sample_schema = None
    if not args.no_sample:
        print(f"building a {args.sample_percent:g}% sample...", flush=True)
        started = time.perf_counter()
        stats = sampling.build_sample(
            dialect, conn, cat,
            percent=args.sample_percent, max_rows=args.sample_max_rows, schema=args.sample_schema,
        )
        print(f"sample built in {time.perf_counter() - started:.1f}s: {stats.describe()}")
        if not stats.tables:
            print("Warning: the sample is empty, falling back to comparing on the full data")
        else:
            sample_schema = stats.schema

    model = parse_model(args.model)
    strategy = get_strategy(args.strategy)
    rewriter = Rewriter(
        model, dialect, cat, strategy,
        variants=args.variants, attempts=args.attempts,
        schema_budget=args.schema_budget,
        hints=args.hints or workload.hints,
    )

    reports = []
    for query in workload:
        print(f"\n{query.qid}: {query.description}", flush=True)
        result = rewriter.rewrite(query.qid, query.description, query.sql)
        if result.error:
            print(f"  {result.error}")
            reports.append(rank.QueryReport(query.qid, query.description, query.sql, error=result.error, strategy=result.strategy))
            continue
        print(f"  {len(result.accepted)}/{len(result.candidates)} candidates passed the static gates")
        report = rank.measure_query(
            dialect, conn, result, cat,
            runs=args.runs, sample_schema=sample_schema,
            compare_limit=args.compare_limit, time_limit_ms=args.baseline_budget_ms,
        )
        for candidate, measurement in zip(result.accepted, report.candidates):
            outcome = candidate.equivalence
            state = "unknown" if outcome is None else outcome.status
            note = f"correct={state}"
            if measurement.speedup:
                note += f" {measurement.speedup:.2f}x"
            if measurement.error:
                note += f" ({measurement.error[:60]})"
            print(f"    {measurement.label:<12} {note}")
        reports.append(report)

    dialect.close(conn)
    print("\n" + rank.render_table(reports))
    if args.out:
        path = rank.save_json(reports, args.out)
        print(f"report written to {path}")
    if not args.no_write:
        target = DIR / "workloads" / workload.name / "rewrites.sql"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_rewrites(reports), encoding="utf-8")
        print(f"rewrites written to {target}")
    return 0


def render_rewrites(reports) -> str:
    """Write the best verified rewrite for each query in the workload's SQL shape."""
    lines = ["-- Verified rewrites: the fastest candidate that matched the original on the sample.", ""]
    for report in reports:
        best = report.best
        if best is None or best.equivalent is False:
            lines.append(f"-- {report.qid} FAILED: no verified candidate\n")
            continue
        lines.append(f"-- {report.qid} (rewrite): {report.description}")
        lines.append(f"-- {best.median_ms:.1f}ms vs {report.baseline.median_ms:.1f}ms original"
                     if report.baseline and report.baseline.ok else "")
        lines.append(best.sql + ";")
        lines.append("")
    return "\n".join(line for line in lines if line is not None)


def cmd_compare(args) -> int:
    print(rank.compare_strategies(args.files))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "show": cmd_show, "catalog": cmd_catalog,
        "verify": cmd_verify, "rewrite": cmd_rewrite,
        "compare": cmd_compare,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())