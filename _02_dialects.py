"""Engine adapters for PostgreSQL, MySQL, and DuckDB."""
import json
import re
import statistics
import time
from typing import Any

from _01_core import Plan, QueryFailed, StatementRejected, Result, Timing

READONLY = re.compile(r"^\s*(?:with|select)\b", re.IGNORECASE)
BLOCKED = re.compile(
    r"\b(?:insert|update|delete|merge|drop|alter|create|truncate|comment|replace|"
    r"grant|revoke|vacuum|analyze|reindex|refresh|cluster|copy|do|call|execute|"
    r"pg_sleep|pg_sleep_for|dblink|lo_import|lo_export|set_config|load_extension|"
    r"install|force)\b",
    re.IGNORECASE,
)


def _check_statement(sql: str, sqlglot_name: str) -> None:
    if BLOCKED.search(sql):
        raise StatementRejected("blocked keyword")
    if not READONLY.match(sql):
        raise StatementRejected("must start with SELECT or WITH")
    import sqlglot

    try:
        stmts = [s for s in sqlglot.parse(sql, dialect=sqlglot_name) if s is not None]
    except sqlglot.ParseError as err:
        raise StatementRejected(f"syntax error: {str(err).splitlines()[0]}") from err
    if len(stmts) != 1:
        raise StatementRejected(f"expected 1 statement, got {len(stmts)}")


def _column_name(desc: Any) -> str:
    return getattr(desc, "name", None) or desc[0]


def _message(err: Exception) -> str:
    return " ".join(str(err).split())[:300]


def _walk(node: Any, out: list) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, out)
    elif isinstance(node, dict):
        out.append(node)
        for child in node.get("Plans", []) or []:
            _walk(child, out)


class BaseEngine:
    def quote(self, ident: str) -> str:
        return self.quote_char + ident.replace(self.quote_char, self.quote_char * 2) + self.quote_char

    def ref(self, schema: str, table: str) -> str:
        return f"{self.quote(schema)}.{self.quote(table)}"

    def check_statement(self, sql: str) -> None:
        _check_statement(sql, self.sqlglot)

    def execute(self, conn: Any, sql: str) -> None:
        """Run one statement that returns no rows. Used for schema setup."""
        conn.cursor().execute(sql)

    def close(self, conn: Any) -> None:
        conn.close()

    def commit(self, conn: Any) -> None:
        pass

    def rollback(self, conn: Any) -> None:
        pass

    def measure(self, conn: Any, sql: str, runs: int = 5, limit: int = 10_000) -> Timing:
        self.check_statement(sql)
        samples = []
        for i in range(runs):
            start = time.perf_counter()
            try:
                self._run_query(conn, sql, limit)
            except Exception as err:
                return Timing(error=_message(err))
            if i or runs == 1:
                samples.append((time.perf_counter() - start) * 1000.0)
        return Timing(statistics.median(samples or [0.0]), runs)


class Postgres(BaseEngine):
    name = "postgres"
    sqlglot = "postgres"
    default_schema = "public"
    quote_char = '"'

    def connect(self, dsn: str, read_only: bool = False):
        import psycopg2
        return psycopg2.connect(dsn)

    def commit(self, conn) -> None:
        conn.commit()

    def rollback(self, conn) -> None:
        conn.rollback()

    def _run_query(self, conn, sql, limit):
        cur = conn.cursor()
        cur.execute(sql)
        cur.fetchmany(limit + 1)

    def fetch(self, conn, sql, limit):
        self.check_statement(sql)
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchmany(limit + 1)
        cols = tuple(_column_name(d) for d in (cur.description or ()))
        return Result(tuple(rows[:limit]), cols, truncated=len(rows) > limit)

    def sample_source(self, schema, table, percent):
        return f"SELECT * FROM {self.ref(schema, table)} TABLESAMPLE BERNOULLI ({max(percent, 0.0001):.6f})"

    def catalog_queries(self, schema):
        return {
            "columns": f"select table_name, column_name, data_type, is_nullable from information_schema.columns where table_schema = '{schema}' order by table_name, ordinal_position",
            "keys": f"select tc.table_name, tc.constraint_name, kcu.column_name, tc.constraint_type from information_schema.table_constraints tc join information_schema.key_column_usage kcu on kcu.constraint_name = tc.constraint_name where tc.table_schema = '{schema}' and tc.constraint_type in ('PRIMARY KEY', 'UNIQUE') order by tc.table_name, tc.constraint_name, kcu.ordinal_position",
            "foreign_keys": f"select tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name from information_schema.table_constraints tc join information_schema.key_column_usage kcu on kcu.constraint_name = tc.constraint_name join information_schema.constraint_column_usage ccu on ccu.constraint_name = tc.constraint_name where tc.table_schema = '{schema}' and tc.constraint_type = 'FOREIGN KEY'",
            "indexed_columns": f"select t.relname, pg_get_indexdef(x.indexrelid, k + 1, true) from pg_index x join lateral generate_subscripts(x.indkey, 1) as k(k) on true join pg_class t on t.oid = x.indrelid join pg_namespace n on n.oid = t.relnamespace where n.nspname = '{schema}' and not x.indisprimary",
            "row_counts": f"select c.relname, greatest(c.reltuples, 0) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = '{schema}' and c.relkind = 'r'",
        }

    def explain(self, conn, sql):
        self.check_statement(sql)
        cur = conn.cursor()
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql.rstrip().rstrip(";"))
        payload = cur.fetchone()[0]
        plan = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        root = plan[0] if isinstance(plan, list) and plan else {}
        nodes = []
        _walk(root.get("Plan", {}), nodes)
        loops = max((int(n.get("Actual Loops", 1) or 1) for n in nodes), default=1)
        scans = [n["Relation Name"] for n in nodes if n.get("Node Type") == "Seq Scan" and n.get("Relation Name")]
        return Plan(float(root.get("Execution Time", 0.0) or 0.0), loops, tuple(scans), json.dumps(root)[:4000])


class MySQL(BaseEngine):
    name = "mysql"
    sqlglot = "mysql"
    default_schema = ""
    quote_char = "`"

    def connect(self, dsn: str, read_only: bool = False):
        import pymysql
        from urllib.parse import unquote, urlparse
        parsed = urlparse(dsn)
        opts = {"charset": "utf8mb4", "connect_timeout": 20}
        if read_only:
            opts["read_timeout"] = 300
        return pymysql.connect(
            host=parsed.hostname or "localhost", port=parsed.port or 3306,
            user=unquote(parsed.username or "root"), password=unquote(parsed.password or ""),
            database=(parsed.path or "/").lstrip("/") or None, **opts,
        )

    def commit(self, conn) -> None:
        conn.commit()

    def rollback(self, conn) -> None:
        conn.rollback()

    def _run_query(self, conn, sql, limit):
        cur = conn.cursor()
        cur.execute(sql)
        cur.fetchmany(limit + 1)

    def fetch(self, conn, sql, limit):
        self.check_statement(sql)
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchmany(limit + 1)
        cols = tuple(_column_name(d) for d in (cur.description or ()))
        return Result(tuple(rows[:limit]), cols, truncated=len(rows) > limit)

    def sample_source(self, schema, table, percent):
        return f"SELECT * FROM {self.ref(schema, table)} ORDER BY rand()"

    def catalog_queries(self, schema):
        return {
            "columns": f"select table_name, column_name, data_type, is_nullable from information_schema.columns where table_schema = '{schema}' order by table_name, ordinal_position",
            "keys": f"select table_name, constraint_name, column_name, case when constraint_name = 'PRIMARY' then 'PRIMARY KEY' else 'UNIQUE' end from information_schema.key_column_usage where table_schema = '{schema}' and constraint_name in ('PRIMARY', 'UNIQUE') order by table_name, constraint_name, ordinal_position",
            "foreign_keys": f"select kcu.table_name, kcu.column_name, kcu.referenced_table_name, kcu.referenced_column_name from information_schema.key_column_usage kcu join information_schema.referential_constraints rc on rc.constraint_name = kcu.constraint_name where kcu.table_schema = '{schema}' and kcu.referenced_table_name is not null",
            "indexed_columns": f"select table_name, column_name from information_schema.statistics where table_schema = '{schema}' and seq_in_index = 1",
            "row_counts": f"select table_name, table_rows from information_schema.tables where table_schema = '{schema}' and table_type = 'BASE TABLE'",
        }

    def explain(self, conn, sql):
        self.check_statement(sql)
        cur = conn.cursor()
        cur.execute("EXPLAIN FORMAT=JSON " + sql.rstrip().rstrip(";"))
        text = cur.fetchone()[0] if cur.rowcount else "{}"
        data = json.loads(text) if isinstance(text, (str, bytes)) else {}
        nodes = []

        def collect(node):
            if isinstance(node, dict):
                nodes.append(node)
                for child in node.get("table", {}).values() if isinstance(node.get("table"), dict) else []:
                    collect(child)

        collect(data)
        scans = [n["table"] for n in nodes if n.get("access_type") == "ALL" and n.get("table")]
        return Plan(None, 1, tuple(scans), json.dumps(data)[:4000])


class DuckDB(BaseEngine):
    name = "duckdb"
    sqlglot = "duckdb"
    default_schema = "main"
    quote_char = '"'

    def connect(self, dsn: str, read_only: bool = False):
        import duckdb
        return duckdb.connect(dsn or ":memory:", read_only=read_only)

    def _run_query(self, conn, sql, limit):
        cur = conn.execute(sql)
        cur.fetchmany(limit + 1)

    def fetch(self, conn, sql, limit):
        self.check_statement(sql)
        cur = conn.execute(sql)
        rows = cur.fetchmany(limit + 1)
        cols = tuple(_column_name(d) for d in (getattr(cur, "description", None) or ()))
        return Result(tuple(rows[:limit]), cols, truncated=len(rows) > limit)

    def sample_source(self, schema, table, percent):
        return f"SELECT * FROM {self.ref(schema, table)} USING SAMPLE {max(percent, 0.0001):.6f} PERCENT (bernoulli)"

    def catalog_queries(self, schema):
        return {
            "columns": f"select table_name, column_name, data_type, case when is_nullable = 'YES' then 'YES' else 'NO' end from information_schema.columns where table_schema = '{schema}' order by table_name, ordinal_position",
            "keys": f"select c.table_name, c.constraint_name, c.constraint_column_names[1] as column_name, c.constraint_type from duckdb_constraints() c join duckdb_tables() t on t.table_name = c.table_name where t.schema_name = '{schema}' and c.constraint_type in ('PRIMARY KEY', 'UNIQUE')",
            "foreign_keys": f"select c.table_name, child.column_name, c.referenced_table, parent.column_name from duckdb_constraints() c join duckdb_tables() t on t.table_name = c.table_name cross join unnest(c.constraint_column_names) with ordinality as child(column_name, pos) cross join unnest(c.referenced_column_names) with ordinality as parent(column_name, pos) where t.schema_name = '{schema}' and c.constraint_type = 'FOREIGN KEY' and child.pos = parent.pos",
            "indexed_columns": f"select i.table_name, i.sql from duckdb_indexes() i join duckdb_tables() t on t.table_name = i.table_name where t.schema_name = '{schema}'",
            "row_counts": f"select table_name, estimated_size from duckdb_tables() where schema_name = '{schema}'",
        }

    def explain(self, conn, sql):
        self.check_statement(sql)
        cur = conn.execute("EXPLAIN ANALYZE " + sql.rstrip().rstrip(";"))
        text = "\n".join(str(row[1]) for row in cur.fetchall() if len(row) > 1)
        total = re.search(r"Total Time:\s*([0-9.]+)\s*ms", text)
        scans = re.findall(r"SEQ_SCAN\s+(\w+)", text)
        return Plan(float(total.group(1)) if total else None, 1, tuple(scans), text[:4000])


REGISTRY = {"postgres": Postgres, "postgresql": Postgres, "mysql": MySQL, "mariadb": MySQL, "duckdb": DuckDB}


def get_dialect(name: str):
    try:
        return REGISTRY[name.strip().lower()]()
    except KeyError:
        raise KeyError(f"unknown database {name!r}; try one of {sorted(REGISTRY)}") from None


def open_database(name: str, dsn: str, read_only: bool = False):
    dialect = get_dialect(name)
    return dialect, dialect.connect(dsn, read_only=read_only)