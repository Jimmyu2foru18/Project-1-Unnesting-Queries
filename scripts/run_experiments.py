"""Run the experiments and record the results.

    python scripts/run_experiments.py                      # every workload on duckdb
    python scripts/run_experiments.py --workload workloads/imdb_join --model ollama/gpt-oss:20b
    python scripts/run_experiments.py --engine postgres --dsn postgresql://...

One run per workload, each writing a JSON report under ``benchmarks/results``.
Each run is also appended to ``benchmarks/results/index.json`` with everything it
depends on: the model, the engine, the schema, the settings and the sample that was
built. A result that cannot be reproduced from its own record is not worth keeping.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DIR))

RESULTS = DIR / "benchmarks" / "results"


def available_databases(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def run_one(args, workload, model: str) -> dict:
    from sqlrewriter import rank, sampling, workloads as workload_mod
    from sqlrewriter.catalog import introspect
    from sqlrewriter.dialects import get_dialect
    from sqlrewriter.llm import parse_model
    from sqlrewriter.rewrite import Rewriter

    dsn = args.dsn or workload.dsn
    dialect = get_dialect(args.engine)
    schema = args.schema or workload.schema or dialect.default_schema
    if not dsn:
        return {"workload": workload.name, "error": "no dsn"}

    started = time.perf_counter()
    conn = dialect.connect(dsn)
    catalog = introspect(dialect, conn, schema)

    sample_schema = None
    sample_info = {"used": False, "reason": "sampling disabled"}
    if not args.no_sample:
        stats = sampling.build_sample(
            dialect, conn, catalog, percent=args.sample_percent,
            max_rows=args.sample_max_rows,
        )
        if stats.tables:
            sample_schema = stats.schema
            sample_info = {
                "used": True,
                "schema": stats.schema,
                "percent": args.sample_percent,
                "max_rows": args.sample_max_rows,
                "rows": stats.rows,
                "tables": len(stats.tables),
                "problems": stats.problems(),
            }
        else:
            sample_info = {"used": False, "reason": "sample was empty; compared on the full data",
                           "problems": stats.problems()}

    rewriter = Rewriter(
        parse_model(model), dialect, catalog,
        variants=args.variants, attempts=args.attempts,
        schema_budget=args.schema_budget, hints=args.hints or workload.hints,
    )

    reports = []
    for query in workload:
        result = rewriter.rewrite(query.qid, query.description, query.sql)
        report = rank.measure_query(
            dialect, conn, result, catalog,
            runs=args.runs, sample_schema=sample_schema,
            compare_limit=args.compare_limit,
        )
        reports.append(report)
    dialect.close(conn)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"{workload.name}-{args.engine}-{stamp}.json"
    payload = rank.to_json(reports)
    out.write_text(payload, encoding="utf-8")

    print(rank.render_table(reports))
    print(f"report: {out.relative_to(DIR)}")
    return {
        "workload": workload.name,
        "engine": args.engine,
        "model": model,
        "schema": schema,
        "queries": len(workload),
        "settings": {
            "variants": args.variants,
            "attempts": args.attempts,
            "runs": args.runs,
            "schema_budget": args.schema_budget,
            "compare_limit": args.compare_limit,
        },
        "sample": sample_info,
        "summary": json.loads(payload)["summary"],
        "report": out.relative_to(DIR).as_posix(),
        "started": stamp,
        "seconds": round(time.perf_counter() - started, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workload", default="", help="a workload folder; default is every one found")
    parser.add_argument("--engine", default="duckdb", choices=["duckdb", "mysql", "postgres"])
    parser.add_argument("--dsn", default=os.getenv("SQLRW_DSN", ""))
    parser.add_argument("--schema", default="")
    parser.add_argument("--model", default="ollama/gpt-oss:20b")
    parser.add_argument("--variants", type=int, default=3)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--schema-budget", type=int, default=8)
    parser.add_argument("--sample-percent", type=float, default=1.0)
    parser.add_argument("--sample-max-rows", type=int, default=200_000)
    parser.add_argument("--compare-limit", type=int, default=200_000)
    parser.add_argument("--no-sample", action="store_true")
    parser.add_argument("--hints", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(DIR / ".env")
    except ImportError:
        pass

    from sqlrewriter import workloads as workload_mod

    if args.workload:
        selected = [workload_mod.load(args.workload)]
    else:
        selected = workload_mod.discover(DIR / "workloads")
    if not selected:
        print("Error: no workloads found")
        return 1

    if not args.dsn:
        args.dsn = os.getenv("SQLRW_DSN", "")
    if not args.dsn and args.engine == "duckdb":
        args.dsn = str(DIR / "data" / "imdb.duckdb")
        if not Path(args.dsn).exists():
            print(f"Error: {args.dsn} not found. Run: python scripts/load_data.py")
            return 1

    driver = {"duckdb": "duckdb", "mysql": "pymysql", "postgres": "psycopg2"}[args.engine]
    if not available_databases(driver):
        print(f"Error: the {args.engine} driver is not installed. Run: python scripts/setup.py --install")
        return 1

    RESULTS.mkdir(parents=True, exist_ok=True)
    index = []
    for workload in selected:
        queries = workload.limit(args.limit) if args.limit else workload
        print(f"\n{'=' * 70}\n{workload.name}: {len(queries)} queries | {args.engine} | {args.model}\n{'=' * 70}")
        try:
            index.append(run_one(args, queries, args.model))
        except KeyboardInterrupt:
            print("\ninterrupted")
            break
        except Exception as err:  # noqa: BLE001 - one workload failing is not fatal
            print(f"Error running {workload.name}: {type(err).__name__}: {err}")
            index.append({"workload": workload.name, "error": str(err)[:200]})

    summary = RESULTS / "index.json"
    existing = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else []
    summary.write_text(json.dumps(existing + index, indent=2), encoding="utf-8")
    print(f"\nrecorded {len(index)} run(s) in {summary.relative_to(DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())