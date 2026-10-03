"""A sampled copy of the data, used to check that a rewrite is still correct.

Checking a rewrite by running it against the real table is not an option: the
original may take minutes, and both statements may produce more rows than fit in
memory. So correctness is judged on a small copy instead, and only a candidate
that survives there is allowed anywhere near the full table.

The sample is built to keep its foreign keys valid. Tables are visited in
dependency order, and a child table is not only sampled but also restricted to
the parents that were actually sampled. Without that restriction a sample is
full of dangling references, joins collapse to nothing, and a rewrite that
doubles every row can look correct because both queries return zero rows.

The sample lives in its own schema, so the original data is never touched.
"""
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from sqlrewriter.catalog import Catalog, ForeignKey
from sqlrewriter.dialects import Dialect

SAMPLE_SCHEMA = "sqlrw_sample"


@dataclass
class SampleStats:
    """What the sample ended up containing."""

    schema: str = SAMPLE_SCHEMA
    tables: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    percent: float = 0.0

    @property
    def rows(self) -> int:
        return sum(self.tables.values())

    def describe(self) -> str:
        parts = ", ".join(f"{t} {n:,}" for t, n in sorted(self.tables.items()))
        return f"{self.rows:,} rows across {len(self.tables)} tables ({parts})"

    def problems(self) -> str:
        """One line per table that could not be sampled, if any."""
        return "; ".join(f"{table}: {_short(err)}" for table, err in sorted(self.errors.items()))


def _short(err: Exception) -> str:
    return " ".join(str(err).split())[:160]


def build_sample(
    dialect: Dialect,
    conn,
    catalog: Catalog,
    *,
    percent: float = 1.0,
    max_rows: int = 200_000,
    schema: str = SAMPLE_SCHEMA,
    drop_existing: bool = True,
) -> SampleStats:
    """Create a sampled copy of ``schema`` in ``schema`` of sample tables.

    Returns the resulting :class:`SampleStats`. Tables that cannot be sampled
    are skipped rather than failing the run, because a missing sample table only
    costs coverage on the queries that touch it.
    """
    if drop_existing:
        _reset(dialect, conn, schema)

    stats = SampleStats(schema=schema, percent=percent)
    for table in catalog.order_for_sampling():
        try:
            source = dialect.sample_source(catalog.schema, table, percent)
            predicate = _parent_predicate(dialect, catalog, table, schema)
            sql = (
                f"CREATE TABLE {dialect.ref(schema, table)} AS "
                f"SELECT * FROM ({source}) AS {dialect.quote('src')}"
            )
            if predicate:
                sql += f" WHERE {predicate}"
            sql += f" LIMIT {int(max_rows)}"
            dialect.run(conn, sql)
            count = dialect.fetch(
                conn, f"SELECT count(*) FROM {dialect.ref(schema, table)}", 1
            ).rows
            stats.tables[table] = int(count[0][0]) if count else 0
        except Exception as err:  # noqa: BLE001 - an unsamplable table is skipped
            stats.errors[table] = _short(err)
            dialect.rollback(conn)
    dialect.commit(conn)
    return stats


def _parent_predicate(dialect: Dialect, catalog: Catalog, table: str, schema: str) -> str:
    """Require that every sampled foreign key still points at a sampled parent.

    A composite key is matched on all its columns at once, so the predicate
    mirrors the constraint rather than each column on its own.
    """
    clauses = []
    grouped: dict[tuple[str, str], list[str]] = {}
    for key in catalog.parents_of(table):
        if key.parent_table in catalog.columns:
            grouped.setdefault((key.parent_table, key.parent_column), []).append(key.child_column)
    for (parent, parent_column), children in grouped.items():
        ref = dialect.ref(schema, parent)
        src = dialect.quote("src")
        on = " AND ".join(
            f"{src}.{dialect.quote(child)} = {dialect.quote('p')}.{dialect.quote(parent_column)}"
            for child in sorted(children)
        )
        clauses.append(f"EXISTS (SELECT 1 FROM {ref} AS {dialect.quote('p')} WHERE {on})")
    return " AND ".join(clauses)


def _reset(dialect: Dialect, conn, schema: str) -> None:
    """Drop the sample schema if it is already there."""
    quoted = dialect.quote(schema)
    statements = {
        "duckdb": f"DROP SCHEMA IF EXISTS {quoted} CASCADE",
        "postgres": f"DROP SCHEMA IF EXISTS {quoted} CASCADE",
        "mysql": f"DROP DATABASE IF EXISTS {quoted}",
    }
    try:
        dialect.run(conn, statements[dialect.name])
    except Exception:  # noqa: BLE001 - a schema that is not there is fine
        dialect.rollback(conn)
    if dialect.name == "duckdb":
        dialect.run(conn, f"CREATE SCHEMA IF NOT EXISTS {quoted}")
    elif dialect.name == "postgres":
        dialect.run(conn, f"CREATE SCHEMA IF NOT EXISTS {quoted}")
    dialect.commit(conn)


def sample_predicate_sql(dialect: Dialect, sql: str, catalog: Catalog, sample_schema: str) -> str:
    """Rewrite a query so its tables resolve inside the sample schema.

    Only the schema qualifier changes; nothing about the query's shape is
    altered, so the candidate and the original are still comparable.
    """
    tree = sqlglot.parse_one(sql, dialect=dialect.sqlglot)
    if tree is None:
        return sql
    real = set(catalog.columns)
    for table in tree.find_all(exp.Table):
        if table.name in real and not table.db:
            table.set("db", exp.Identifier(this=sample_schema, quoted=True))
    return tree.sql(dialect=dialect.sqlglot, pretty=True)


def sample_table_names(catalog: Catalog, stats: SampleStats) -> list[str]:
    """Tables present in the sample."""
    return [t for t, n in stats.tables.items() if n]


def summarize(stats: SampleStats) -> str:
    return stats.describe()


def edge_hint(catalog: Catalog, table: str) -> str:
    """A short description of how a table relates to the rest of the schema."""
    parents = catalog.parents_of(table)
    children = catalog.children_of(table)
    bits = []
    if parents:
        bits.append("child of " + ", ".join(sorted(k.parent for k in parents)))
    if children:
        bits.append("parent of " + ", ".join(sorted(k.child for k in children)))
    return "; ".join(bits) or "no foreign keys"


def has_cycles(catalog: Catalog) -> list[str]:
    """Tables involved in a foreign-key cycle, which a sample cannot fully satisfy."""
    state: dict[str, int] = {}
    cycles: set[str] = set()

    def visit(table: str, trail: list[str]) -> None:
        if state.get(table) == 2:
            return
        if state.get(table) == 1:
            if table in trail:
                cycles.update(trail[trail.index(table):])
            return
        state[table] = 1
        for key in catalog.parents_of(table):
            if key.parent_table in catalog.columns:
                visit(key.parent_table, trail + [table])
        state[table] = 2

    for table in catalog.tables:
        visit(table, [])
    return sorted(cycles)


def unused_foreign_key_hint(key: ForeignKey) -> str:
    return f"{key.child} references {key.parent}"


__all__ = [
    "SAMPLE_SCHEMA",
    "SampleStats",
    "build_sample",
    "edge_hint",
    "has_cycles",
    "sample_predicate_sql",
    "sample_table_names",
    "summarize",
]