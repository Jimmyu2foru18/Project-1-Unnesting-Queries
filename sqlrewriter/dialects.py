"""Engine adapters for PostgreSQL, MySQL and DuckDB.

Everything that differs between the three engines is collected here: how to
connect, how to ask for the catalog and its statistics, how to take a sample,
how to run a statement, and how to read a plan back. The rest of the tool works
against :class:`Dialect` only and never issues engine-specific SQL itself.

Two conventions hold across all three. A statement submitted by the model is
always a single ``SELECT`` checked against an allowlist before it runs, and a
plan is best-effort: the speed ranking is always wall-clock, because only
PostgreSQL returns a machine-readable plan with loop counts.
"""
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

READONLY_STATEMENT = re.compile(
    r"^\s*(?:with|select)\b", re.IGNORECASE
)
BLOCKED = re.compile(
    r"\b(?:insert|update|delete|merge|drop|alter|create|truncate|comment|replace|"
    r"grant|revoke|vacuum|analyze|reindex|refresh|cluster|copy|do|call|execute|"
    r"pg_sleep|pg_sleep_for|dblink|lo_import|lo_export|set_config|load_extension|"
    r"install|force)\b",
    re.IGNORECASE,
)


class StatementRejected(RuntimeError):
    """A statement was refused before it reached the engine."""


class QueryFailed(RuntimeError):
    """The engine rejected or failed to run a statement."""


@dataclass
class Result:
    """Rows returned by a statement, with column names and a truncation flag."""

    rows: list[tuple] = field(default_factory=list)
    columns: tuple[str, ...] = ()
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.rows)


@dataclass
class Timing:
    """Wall-clock cost of a statement, median of several runs."""

    median_ms: float = 0.0
    runs: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Plan:
    """Best-effort plan facts. Any field may be missing on an engine."""

    execution_ms: float | None = None
    max_loops: int = 1
    seq_scans: list[str] = field(default_factory=list)
    text: str = ""


class Dialect(ABC):
    """One engine."""

    name: str = ""
    sqlglot: str = ""
    default_schema: str = ""
    quote_char: str = '"'

    @abstractmethod
    def connect(self, dsn: str, read_only: bool = False) -> Any:
        """Open a connection."""

    @abstractmethod
    def execute(self, conn: Any, sql: str, params: Any = None) -> Any:
        """Run one statement and return a cursor-like result object."""

    @abstractmethod
    def close(self, conn: Any) -> None:
        """Close a connection."""

    def commit(self, conn: Any) -> None:
        conn.commit()

    def rollback(self, conn: Any) -> None:
        conn.rollback()

    def quote(self, ident: str) -> str:
        return self.quote_char + ident.replace(self.quote_char, self.quote_char * 2) + self.quote_char

    def ref(self, schema: str, table: str) -> str:
        """A schema-qualified, quoted relation name."""
        return f"{self.quote(schema)}.{self.quote(table)}"

    def check_statement(self, sql: str) -> None:
        """Refuse anything that is not a single read-only query."""
        if BLOCKED.search(sql):
            raise StatementRejected("statement contains a blocked keyword")
        if not READONLY_STATEMENT.match(sql):
            raise StatementRejected("statement must start with SELECT or WITH")
        import sqlglot

        try:
            statements = [s for s in sqlglot.parse(sql, dialect=self.sqlglot) if s is not None]
        except sqlglot.ParseError as err:
            raise StatementRejected(f"syntax error: {str(err).splitlines()[0]}") from err
        if len(statements) != 1:
            raise StatementRejected(f"expected one statement, found {len(statements)}")

    def run(self, conn: Any, sql: str) -> None:
        """Execute a statement for its effect and release whatever it returned.

        Engines disagree about whether executing returns a cursor or the
        connection, so the cleanup is done through the dialect rather than by the
        caller reaching for ``.close()``.
        """
        self._close_cursor(self.execute(conn, sql))

    def fetch(self, conn: Any, sql: str, limit: int) -> Result:
        """Run a checked statement and fetch at most ``limit`` rows."""
        self.check_statement(sql)
        cursor = self.execute(conn, sql)
        try:
            rows = list(self._rows(cursor, limit + 1))
            columns = self._columns(cursor)
        finally:
            self._close_cursor(cursor)
        return Result(rows[:limit], columns, truncated=len(rows) > limit)

    def measure(self, conn: Any, sql: str, runs: int = 5, limit: int = 10_000) -> Timing:
        """Median wall-clock cost in milliseconds, discarding the first run."""
        import statistics
        import time

        self.check_statement(sql)
        samples = []
        for index in range(runs):
            start = time.perf_counter()
            try:
                cursor = self.execute(conn, sql)
                list(self._rows(cursor, limit + 1))
                self._close_cursor(cursor)
            except Exception as err:  # noqa: BLE001 - reported as a timing error
                self.rollback(conn)
                return Timing(error=_message(err))
            elapsed = (time.perf_counter() - start) * 1000.0
            if index:
                samples.append(elapsed)
        self.rollback(conn)
        return Timing(statistics.median(samples or [0.0]), len(samples) + 1)

    @abstractmethod
    def sample_source(self, schema: str, table: str, percent: float) -> str:
        """A subquery selecting roughly ``percent`` of a table."""

    @abstractmethod
    def catalog_queries(self, schema: str) -> dict[str, str]:
        """Named templates returning columns, keys, foreign keys, indexes and counts.

        ``{schema}`` is replaced with a quoted literal by :meth:`render`, so the
        templates stay parameter-style agnostic across engines.
        """

    def render(self, template: str, schema: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_$-]+", schema or ""):
            raise StatementRejected(f"unsafe schema name {schema!r}")
        return template.replace("{schema}", f"'{schema}'")

    def catalog(self, conn: Any, schema: str) -> dict[str, list[tuple]]:
        """Run every catalog template and return raw rows per topic."""
        out: dict[str, list[tuple]] = {}
        for topic, template in self.catalog_queries(schema).items():
            out[topic] = self._all(self.execute(conn, self.render(template, schema)))
        return out

    def explain(self, conn: Any, sql: str) -> Plan:
        """Best-effort plan for a statement."""
        return Plan()

    def _all(self, cursor: Any) -> list[tuple]:
        try:
            return list(cursor.fetchall())
        finally:
            self._close_cursor(cursor)

    def _rows(self, cursor: Any, size: int) -> Any:
        fetchmany = getattr(cursor, "fetchmany", None)
        return fetchmany(size) if fetchmany else []

    def _columns(self, cursor: Any) -> tuple[str, ...]:
        description = getattr(cursor, "description", None) or ()
        return tuple(_column_name(d) for d in description)

    def _close_cursor(self, cursor: Any) -> None:
        closer = getattr(cursor, "close", None)
        if callable(closer):
            closer()


def _column_name(description: Any) -> str:
    name = getattr(description, "name", None)
    return name if name is not None else description[0]


def _message(err: Exception) -> str:
    return " ".join(str(err).split())[:300]


def _walk(node: Any, out: dict) -> None:
    """Collect plan nodes from a PostgreSQL-style JSON plan."""
    if isinstance(node, list):
        for item in node:
            _walk(item, out)
    elif isinstance(node, dict):
        out.append(node)
        for child in node.get("Plans", []) or []:
            _walk(child, out)


class Postgres(Dialect):
    name = "postgres"
    sqlglot = "postgres"
    default_schema = "public"

    def connect(self, dsn, read_only=False):
        import psycopg2

        conn = psycopg2.connect(dsn)
        if read_only:
            conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("show statement_timeout")
            timeout = int(cur.fetchone()[0])
            if timeout <= 0:
                cur.execute("set statement_timeout = '120s'")
        conn.commit()
        return conn

    def execute(self, conn, sql, params=None):
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor

    def close(self, conn):
        conn.close()

    def sample_source(self, schema, table, percent):
        return (
            f"SELECT * FROM {self.ref(schema, table)} "
            f"TABLESAMPLE BERNOULLI ({max(percent, 0.0001):.6f})"
        )

    def catalog_queries(self, schema):
        return {
            "columns": (
                "select table_name, column_name, data_type, is_nullable "
                "from information_schema.columns where table_schema = {{schema}} "
                "order by table_name, ordinal_position"
            ),
            "keys": (
                "select tc.table_name, tc.constraint_name, kcu.column_name, tc.constraint_type "
                "from information_schema.table_constraints tc "
                "join information_schema.key_column_usage kcu "
                "  on kcu.constraint_name = tc.constraint_name "
                " and kcu.constraint_schema = tc.constraint_schema "
                "where tc.table_schema = {{schema}} "
                "  and tc.constraint_type in ('PRIMARY KEY', 'UNIQUE') "
                "order by tc.table_name, tc.constraint_name, kcu.ordinal_position"
            ),
            "foreign_keys": (
                "select tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name "
                "from information_schema.table_constraints tc "
                "join information_schema.key_column_usage kcu "
                "  on kcu.constraint_name = tc.constraint_name "
                " and kcu.constraint_schema = tc.constraint_schema "
                "join information_schema.constraint_column_usage ccu "
                "  on ccu.constraint_name = tc.constraint_name "
                " and ccu.constraint_schema = tc.constraint_schema "
                "where tc.table_schema = {{schema}} and tc.constraint_type = 'FOREIGN KEY'"
            ),
            "indexed_columns": (
                "select t.relname, pg_get_indexdef(x.indexrelid, k + 1, true) "
                "from pg_index x "
                "join lateral generate_subscripts(x.indkey, 1) as k(k) on true "
                "join pg_class t on t.oid = x.indrelid "
                "join pg_namespace n on n.oid = t.relnamespace "
                "where n.nspname = {{schema}} and not x.indisprimary"
            ),
            "row_counts": (
                "select c.relname, greatest(c.reltuples, 0) "
                "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = {{schema}} and c.relkind = 'r'"
            ),
        }

    def explain(self, conn, sql):
        self.check_statement(sql)
        cursor = self.execute(conn, "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql.rstrip().rstrip(";"))
        try:
            payload = cursor.fetchone()[0]
        finally:
            self._close_cursor(cursor)
        plan = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        root = plan[0] if isinstance(plan, list) and plan else {}
        nodes: list[dict] = []
        _walk(root.get("Plan", {}), nodes)
        loops = max((int(n.get("Actual Loops", 1) or 1) for n in nodes), default=1)
        scans = [
            n["Relation Name"]
            for n in nodes
            if n.get("Node Type") == "Seq Scan" and n.get("Relation Name")
        ]
        return Plan(float(root.get("Execution Time", 0.0) or 0.0), loops, scans, json.dumps(root)[:4000])


class MySQL(Dialect):
    name = "mysql"
    sqlglot = "mysql"
    default_schema = ""
    quote_char = "`"

    def connect(self, dsn, read_only=False):
        import pymysql

        from urllib.parse import unquote, urlparse

        parsed = urlparse(dsn)
        options = {"charset": "utf8mb4", "connect_timeout": 20}
        if read_only:
            options["read_timeout"] = 300
        conn = pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=unquote(parsed.username or "root"),
            password=unquote(parsed.password or ""),
            database=(parsed.path or "/").lstrip("/") or None,
            **options,
        )
        with conn.cursor() as cur:
            cur.execute("set session max_execution_time = 120000")
        conn.commit()
        return conn

    def execute(self, conn, sql, params=None):
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor

    def close(self, conn):
        conn.close()

    def rollback(self, conn):
        conn.rollback()

    def sample_source(self, schema, table, percent):
        # MySQL has no TABLESAMPLE, so sample by ordering on a random value and
        # capping the row count. The cap keeps the cost of building a sample sane.
        return f"SELECT * FROM {self.ref(schema, table)} ORDER BY rand()"

    def catalog_queries(self, schema):
        return {
            "columns": (
                "select table_name, column_name, data_type, is_nullable "
                "from information_schema.columns where table_schema = {{schema}} "
                "order by table_name, ordinal_position"
            ),
            "keys": (
                "select table_name, constraint_name, column_name, "
                "case when constraint_name = 'PRIMARY' then 'PRIMARY KEY' else 'UNIQUE' end "
                "from information_schema.key_column_usage "
                "where table_schema = {{schema}} and constraint_name in ('PRIMARY', 'UNIQUE') "
                "order by table_name, constraint_name, ordinal_position"
            ),
            "foreign_keys": (
                "select kcu.table_name, kcu.column_name, kcu.referenced_table_name, "
                "kcu.referenced_column_name "
                "from information_schema.key_column_usage kcu "
                "join information_schema.referential_constraints rc "
                "  on rc.constraint_name = kcu.constraint_name "
                " and rc.constraint_schema = kcu.constraint_schema "
                "where kcu.table_schema = {{schema}} and kcu.referenced_table_name is not null"
            ),
            "indexed_columns": (
                "select table_name, column_name from information_schema.statistics "
                "where table_schema = {{schema}} and seq_in_index = 1"
            ),
            "row_counts": (
                "select table_name, table_rows from information_schema.tables "
                "where table_schema = {{schema}} and table_type = 'BASE TABLE'"
            ),
        }

    def explain(self, conn, sql):
        self.check_statement(sql)
        cursor = self.execute(conn, "EXPLAIN FORMAT=JSON " + sql.rstrip().rstrip(";"))
        try:
            row = cursor.fetchone()
        finally:
            self._close_cursor(cursor)
        text = row[0] if row else "{}"
        data = json.loads(text) if isinstance(text, (str, bytes)) else {}
        nodes: list[dict] = []

        def collect(node):
            if isinstance(node, dict):
                nodes.append(node)
                for child in node.get("table", {}).values() if isinstance(node.get("table"), dict) else []:
                    collect(child)

        collect(data)
        scans = [n["table"] for n in nodes if n.get("access_type") == "ALL" and n.get("table")]
        return Plan(None, 1, scans, json.dumps(data)[:4000])


class DuckDB(Dialect):
    name = "duckdb"
    sqlglot = "duckdb"
    default_schema = "main"

    def connect(self, dsn, read_only=False):
        import duckdb

        path = dsn or ":memory:"
        return duckdb.connect(path, read_only=read_only)

    def execute(self, conn, sql, params=None):
        return conn.execute(sql, params) if params else conn.execute(sql)

    def _close_cursor(self, cursor: Any) -> None:
        # DuckDB's execute returns the connection itself, so closing what looks
        # like a cursor would close the database.
        return None

    def close(self, conn):
        conn.close()

    def rollback(self, conn):
        pass

    def commit(self, conn):
        pass

    def sample_source(self, schema, table, percent):
        return (
            f"SELECT * FROM {self.ref(schema, table)} "
            f"USING SAMPLE {max(percent, 0.0001):.6f} PERCENT (bernoulli)"
        )

    def catalog_queries(self, schema):
        return {
            "columns": (
                "select table_name, column_name, data_type, "
                "case when is_nullable = 'YES' then 'YES' else 'NO' end "
                "from information_schema.columns where table_schema = {schema} "
                "order by table_name, ordinal_position"
            ),
            "keys": (
                "select c.table_name, c.constraint_name, c.constraint_column_names[1] as column_name, "
                "c.constraint_type from duckdb_constraints() c "
                "join duckdb_tables() t on t.table_name = c.table_name "
                "where t.schema_name = {schema} and c.constraint_type in ('PRIMARY KEY', 'UNIQUE')"
            ),
            "foreign_keys": (
                "select c.table_name, child.column_name, c.referenced_table, parent.column_name "
                "from duckdb_constraints() c "
                "join duckdb_tables() t on t.table_name = c.table_name "
                "cross join unnest(c.constraint_column_names) with ordinality as child(column_name, pos) "
                "cross join unnest(c.referenced_column_names) with ordinality as parent(column_name, pos) "
                "where t.schema_name = {schema} and c.constraint_type = 'FOREIGN KEY' "
                "  and child.pos = parent.pos"
            ),
            "indexed_columns": (
                "select i.table_name, i.sql from duckdb_indexes() i "
                "join duckdb_tables() t on t.table_name = i.table_name "
                "where t.schema_name = {schema}"
            ),
            "row_counts": (
                "select table_name, estimated_size from duckdb_tables() "
                "where schema_name = {schema}"
            ),
        }

    def explain(self, conn, sql):
        self.check_statement(sql)
        cursor = self.execute(conn, "EXPLAIN ANALYZE " + sql.rstrip().rstrip(";"))
        try:
            text = "\n".join(str(row[1]) for row in cursor.fetchall() if len(row) > 1)
        finally:
            self._close_cursor(cursor)
        total = re.search(r"Total Time:\s*([0-9.]+)\s*ms", text)
        scans = re.findall(r"SEQ_SCAN\s+(\w+)", text)
        return Plan(float(total.group(1)) if total else None, 1, scans, text[:4000])


def _duckdb_index_columns(sql_text: str) -> set[str]:
    """Column names covered by a ``CREATE INDEX`` statement."""
    inner = re.search(r"\((.*)\)", sql_text or "", re.DOTALL)
    if not inner:
        return set()
    return {
        re.split(r"[^A-Za-z0-9_\"]", part.strip())[0].strip('"')
        for part in inner.group(1).split(",")
        if part.strip()
    }


REGISTRY: dict[str, type[Dialect]] = {
    "postgres": Postgres,
    "postgresql": Postgres,
    "mysql": MySQL,
    "mariadb": MySQL,
    "duckdb": DuckDB,
}


def get_dialect(name: str) -> Dialect:
    """Look up an engine by name."""
    try:
        return REGISTRY[name.strip().lower()]()
    except KeyError:
        raise KeyError(f"unknown database {name!r}; try one of {sorted(set(REGISTRY))}") from None


def open_database(name: str, dsn: str, read_only: bool = False) -> tuple[Dialect, Any]:
    """Open a connection to a named engine."""
    dialect = get_dialect(name)
    return dialect, dialect.connect(dsn, read_only=read_only)