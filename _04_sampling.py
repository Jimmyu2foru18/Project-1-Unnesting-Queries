"""A sampled copy of the data, used to check that a rewrite is still correct."""
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from _03_catalog import Catalog

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
    dialect: Any,
    conn: Any,
    catalog: Catalog,
    *,
    percent: float = 1.0,
    max_rows: int = 200_000,
    schema: str = SAMPLE_SCHEMA,
    drop_existing: bool = True,
) -> SampleStats:
    """Create a sampled copy of schema in schema of sample tables."""
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
            dialect.execute(conn, sql)
            count = dialect.fetch(
                conn, f"SELECT count(*) FROM {dialect.ref(schema, table)}", 1
            ).rows
            stats.tables[table] = int(count[0][0]) if count else 0
        except Exception as err:
            stats.errors[table] = _short(err)
            dialect.rollback(conn)
    dialect.commit(conn)
    return stats


def _parent_predicate(dialect: Any, catalog: Catalog, table: str, schema: str) -> str:
    """Require that every sampled foreign key still points at a sampled parent."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for key in catalog.parents_of(table):
        if key.parent_table in catalog.columns:
            grouped.setdefault((key.parent_table, key.parent_column), []).append(key.child_column)

    clauses = []
    for (parent, parent_column), children in grouped.items():
        ref = dialect.ref(schema, parent)
        src = dialect.quote("src")
        on = " AND ".join(
            f"{src}.{dialect.quote(child)} = {dialect.quote('p')}.{dialect.quote(parent_column)}"
            for child in sorted(children)
        )
        clauses.append(f"EXISTS (SELECT 1 FROM {ref} AS {dialect.quote('p')} WHERE {on})")
    return " AND ".join(clauses)


def _reset(dialect: Any, conn: Any, schema: str) -> None:
    """Drop the sample schema if it is already there."""
    quoted = dialect.quote(schema)
    statements = {
        "duckdb": f"DROP SCHEMA IF EXISTS {quoted} CASCADE",
        "postgres": f"DROP SCHEMA IF EXISTS {quoted} CASCADE",
        "mysql": f"DROP DATABASE IF EXISTS {quoted}",
    }
    try:
        dialect.execute(conn, statements[dialect.name])
    except Exception:
        dialect.rollback(conn)
    if dialect.name in ("duckdb", "postgres"):
        dialect.execute(conn, f"CREATE SCHEMA IF NOT EXISTS {quoted}")
    dialect.commit(conn)


def sample_predicate_sql(dialect: Any, sql: str, catalog: Catalog, sample_schema: str) -> str:
    """Rewrite a query so its tables resolve inside the sample schema."""
    tree = sqlglot.parse_one(sql, dialect=dialect.sqlglot)
    if tree is None:
        return sql
    real = set(catalog.columns)
    for table in tree.find_all(exp.Table):
        if table.name in real and not table.db:
            table.set("db", exp.Identifier(this=sample_schema, quoted=True))
    return tree.sql(dialect=dialect.sqlglot, pretty=True)


__all__ = [
    "SAMPLE_SCHEMA",
    "SampleStats",
    "build_sample",
    "sample_predicate_sql",
]