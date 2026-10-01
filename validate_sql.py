"""Check a rewritten query against the real schema before it is allowed near the database."""
import re
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

DIALECT = "postgres"
CREATE_TABLE = re.compile(r"CREATE TABLE\s+(\w+)\s*\((.*?)\)\s*;", re.IGNORECASE | re.DOTALL)
NOT_A_COLUMN = re.compile(r"^(PRIMARY|FOREIGN|UNIQUE|KEY|CONSTRAINT|INDEX|CHECK)", re.IGNORECASE)


def load_schema(ddl_path=None, dsn=None):
    """Table -> {column: type}, read from a SQL dump or from a live database."""
    if dsn:
        return _schema_from_database(dsn)
    schema = {}
    for table, body in CREATE_TABLE.findall(Path(ddl_path).read_text(encoding="utf-8")):
        columns = {}
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            parts = line.split()
            if len(parts) >= 2 and not NOT_A_COLUMN.match(parts[0]):
                columns[parts[0]] = parts[1]
        if columns:
            schema[table] = columns
    return schema


def _schema_from_database(dsn):
    import psycopg2

    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "select table_name, column_name, data_type from information_schema.columns "
            "where table_schema = 'public'"
        )
        schema = {}
        for table, column, dtype in cur.fetchall():
            schema.setdefault(table, {})[column] = dtype
    return schema


def _tables(tree):
    """Every real table the query reads, minus the CTEs it defines itself."""
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    return {table.name for table in tree.find_all(exp.Table)} - ctes


def validate(sql, schema, dialect=DIALECT):
    """Return a list of problems with the query. An empty list means it is safe to run."""
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.ParseError as err:
        return [f"syntax error: {str(err).splitlines()[0]}"]
    if tree is None:
        return ["the model returned no SQL"]

    problems = [f"table {name!r} does not exist" for name in sorted(_tables(tree) - set(schema))]
    if problems:
        return problems
    try:
        qualify(tree.copy(), schema=schema, dialect=dialect)
    except Exception as err:  # noqa: BLE001 - sqlglot raises OptimizeError and friends
        return [f"{type(err).__name__}: {str(err).splitlines()[0]}"]
    return []


def render_schema(schema):
    """A compact DDL block to paste into the prompt."""
    return "\n".join(
        f"CREATE TABLE {table} (" + ", ".join(f"{col} {kind}" for col, kind in cols.items()) + ");"
        for table, cols in sorted(schema.items())
    )
