"""Prepare the benchmark dataset.

    python scripts/load_data.py --dump imdb.7z --engine duckdb --out data/imdb.duckdb

DuckDB is the default target because it requires no server. The same dump loads
into MySQL and PostgreSQL with their respective clients, and --emit-sql writes
a cleaned file for either.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DIR))

MYSQL_ONLY = re.compile(
    r"^\s*(?:DROP\s+DATABASE|CREATE\s+DATABASE|USE\s+|SET\s+|START\s+TRANSACTION|"
    r"BEGIN;|COMMIT;|ROLLBACK;|LOCK\s+TABLES|UNLOCK\s+TABLES|\(\d+\)\s*ENGINE|"
    r"ALTER\s+DATABASE)\b",
    re.IGNORECASE,
)
SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"


def resolve_dump(source: Path) -> Path:
    if source.suffix.lower() == ".7z":
        target = Path(tempfile.gettempdir()) / "sqlrw_imdb_dump"
        target.mkdir(parents=True, exist_ok=True)
        marker = target / ".extracted"
        if not marker.exists() or marker.read_text().strip() != str(source):
            binary = Path(SEVEN_ZIP) if Path(SEVEN_ZIP).exists() else Path("7z")
            if not Path(binary).exists() and not _on_path("7z"):
                sys.exit("Error: 7-Zip is needed to read a .7z dump; pass an extracted .sql instead")
            subprocess.run([str(binary), "x", str(source), f"-o{target}", "-y"], check=True)
            marker.write_text(str(source), encoding="utf-8")
        sql = target / (source.stem + ".sql")
        if not sql.exists():
            matches = sorted(target.glob("*.sql"))
            if not matches:
                sys.exit(f"Error: no .sql found inside {source}")
            return matches[0]
        return sql
    return source


def _on_path(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def clean_dump(lines: list[str]) -> list[str]:
    return [line for line in lines if not MYSQL_ONLY.match(line)]


INSERT_INTO = re.compile(r"^(\s*)INSERT\s+INTO\s+", re.IGNORECASE)


def split_statements(lines: list[str], ignore_duplicates: bool = True) -> tuple[list[str], dict[str, list[str]]]:
    ddl: list[str] = []
    inserts: dict[str, list[str]] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("INSERT INTO "):
            table = stripped.split()[2].strip('`"')
            line = INSERT_INTO.sub(
                r"\1INSERT OR IGNORE INTO " if ignore_duplicates else r"\1", line
            )
            inserts.setdefault(table, []).append(line)
        elif not inserts:
            ddl.append(line)
    return ddl, inserts


def load_order(ddl: list[str], inserts: dict[str, list[str]]) -> list[str]:
    blocks = re.findall(
        r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\)\s*;", "\n".join(ddl), re.IGNORECASE | re.DOTALL
    )
    edges = {
        table: [p for p in re.findall(r"REFERENCES\s+(\w+)\s*\(", body, re.IGNORECASE) if p in inserts]
        for table, body in blocks
    }

    ordered: list[str] = []
    state: dict[str, int] = {}

    def visit(table: str) -> None:
        if state.get(table):
            return
        state[table] = 1
        for parent in edges.get(table, []):
            visit(parent)
        state[table] = 2
        ordered.append(table)

    for table in inserts:
        visit(table)
    return ordered


def _run_group(conn, group: list[str], strict: bool) -> tuple[int, list[str]]:
    try:
        conn.execute("\n".join(group))
        return 0, []
    except Exception:
        if strict:
            raise
    duplicates, failures = 0, []
    for line in group:
        if not line.strip():
            continue
        try:
            conn.execute(line)
        except Exception as err:
            message = str(err)
            if "Duplicate key" in message or "violates primary key" in message:
                duplicates += 1
                continue
            failures.append(f"{message[:110]} :: {line.strip()[:110]}")
    return duplicates, failures


def load_duckdb(dump: Path, out: Path, indexes: bool, rebuild: bool,
                batch: int = 2000, strict: bool = False) -> None:
    import duckdb

    if out.exists() and not rebuild:
        print(f"{out} already exists; pass --rebuild to reload")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    if rebuild and out.exists():
        out.unlink()

    print(f"reading {dump} ({dump.stat().st_size / 1e6:.0f} MB)")
    started = time.perf_counter()
    lines = clean_dump(dump.read_text(encoding="utf-8", errors="replace").splitlines())
    ddl, inserts = split_statements(lines)
    order = load_order(ddl, inserts)
    print(
        f"prepared {sum(len(v) for v in inserts.values()):,} inserts for "
        f"{len(inserts)} tables in {time.perf_counter() - started:.1f}s"
    )
    print(f"load order: {' -> '.join(order)}")

    conn = duckdb.connect(str(out))
    started = time.perf_counter()
    conn.execute("\n".join(ddl))
    duplicates, failures = 0, []
    for table in order:
        rows = inserts[table]
        for start in range(0, len(rows), batch):
            group_duplicates, group_failures = _run_group(conn, rows[start:start + batch], strict)
            duplicates += group_duplicates
            failures.extend(group_failures)
        print(f"  {table:<12} {len(rows):>10,} statements")

    print(f"loaded in {time.perf_counter() - started:.1f}s")
    if duplicates:
        print(f"skipped {duplicates:,} duplicate primary keys")
    if failures:
        print(f"{len(failures)} statements failed to load:")
        for note in failures[:10]:
            print(f"  {note}")

    for table, count in conn.execute(
        "select table_name, estimated_size from duckdb_tables() order by table_name"
    ).fetchall():
        print(f"  {table:<12} {count:>12,} rows loaded")

    if indexes:
        started = time.perf_counter()
        print("creating indexes on foreign key columns...")
        constraints = conn.execute(
            "select c.table_name, c.constraint_column_names[1] as column_name "
            "from duckdb_constraints() c where c.constraint_type = 'FOREIGN KEY'"
        ).fetchall()
        for table, column in constraints:
            try:
                conn.execute(
                    f'create index if not exists "idx_{table}_{column}" '
                    f'on "{table}" ("{column}")'
                )
            except Exception as err:
                print(f"  skipped {table}.{column}: {str(err)[:60]}")
        print(f"indexes built in {time.perf_counter() - started:.1f}s")

    conn.execute("checkpoint")
    conn.close()
    print(f"wrote {out}")


def emit_sql(dump: Path, out: Path) -> None:
    print(f"reading {dump}")
    kept = clean_dump(dump.read_text(encoding="utf-8", errors="replace").splitlines())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.0f} MB)")
    print()
    print("Load it with:")
    print(f"  mysql  -u root -p <database> < {out}")
    print(f"  psql -d <database> -f {out}")


def load_server(engine: str, dump: Path) -> None:
    if engine == "mysql":
        import pymysql
        from urllib.parse import unquote, urlparse

        parsed = urlparse(os.getenv("SQLRW_DSN", ""))
        conn = pymysql.connect(
            host=parsed.hostname or "localhost", port=parsed.port or 3306,
            user=unquote(parsed.username or "root"), password=unquote(parsed.password or ""),
            database=(parsed.path or "/").lstrip("/") or None, charset="utf8mb4",
        )
        with conn.cursor() as cur:
            cur.execute("show databases")
            print("server reachable, databases:", [row[0] for row in cur.fetchall()])
        conn.close()
        print(f"load the dump with: mysql -u <user> -p <database> < {dump}")
    else:
        print(f"load the dump with: psql -d <database> -f {dump}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dump", default="imdb.7z", help="dataset archive or .sql dump")
    parser.add_argument("--engine", default="duckdb", choices=["duckdb", "mysql", "postgres"])
    parser.add_argument("--out", default="data/imdb.duckdb", help="database file for duckdb, or .sql for --emit-sql")
    parser.add_argument("--indexes", action="store_true", help="build indexes on foreign key columns")
    parser.add_argument("--rebuild", action="store_true", help="reload even if output exists")
    parser.add_argument("--batch", type=int, default=2000, help="inserts per batch")
    parser.add_argument("--strict", action="store_true", help="fail on first bad statement")
    parser.add_argument("--emit-sql", default="", help="write a cleaned .sql for MySQL or PostgreSQL and stop")
    args = parser.parse_args(argv)

    source = Path(args.dump)
    if not source.exists():
        sys.exit(f"Error: {source} not found")
    dump = resolve_dump(source)

    if args.emit_sql:
        emit_sql(dump, Path(args.emit_sql))
        return 0
    if args.engine == "duckdb":
        load_duckdb(dump, Path(args.out), args.indexes, args.rebuild, args.batch, args.strict)
    else:
        load_server(args.engine, dump)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())