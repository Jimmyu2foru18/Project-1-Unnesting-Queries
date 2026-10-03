"""The catalog: columns, keys, relationships, indexes and row counts.

This is the only thing the model is told about the database, so it is built to
answer the three questions that decide whether a rewrite is fast: which column
pairs are actually joinable, how big each side of that join is, and which
predicates can still use an index.

Rendering is pruned per query. A database with dozens of tables and hundreds of
foreign keys does not fit in a useful prompt, so tables are walked outward from
the ones the query already names and emitted nearest-first. If the budget
truncates the list, the tables that survive are the relevant ones.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, NamedTuple

import sqlglot
from sqlglot import exp

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TYPE_ALIASES = {
    "character varying": "TEXT",
    "character": "CHAR",
    "integer": "INT",
    "int": "INT",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
    "boolean": "BOOLEAN",
    "double precision": "DOUBLE PRECISION",
    "double": "DOUBLE",
    "real": "FLOAT",
    "float": "FLOAT",
    "timestamp without time zone": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMPTZ",
    "time without time zone": "TIME",
    "time with time zone": "TIMETZ",
    "decimal": "DECIMAL",
    "user-defined": "",
}


class ForeignKey(NamedTuple):
    """A directed key edge: ``child.column`` points at ``parent.column``."""

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str

    @property
    def child(self) -> str:
        return f"{self.child_table}.{self.child_column}"

    @property
    def parent(self) -> str:
        return f"{self.parent_table}.{self.parent_column}"


def as_key(value: object) -> ForeignKey:
    """Accept a key edge written either as a tuple or as a ``ForeignKey``."""
    if isinstance(value, ForeignKey):
        return value
    if isinstance(value, dict):
        return ForeignKey(**value)
    parts = tuple(value)  # type: ignore[arg-type]
    if len(parts) != 4:
        raise ValueError(f"a foreign key needs 4 parts, got {value!r}")
    return ForeignKey(*parts)


@dataclass
class Catalog:
    """Everything the model is allowed to know about a database."""

    schema: str = "public"
    columns: dict[str, dict[str, str]] = field(default_factory=dict)
    not_null: dict[str, set[str]] = field(default_factory=dict)
    primary: dict[str, list[str]] = field(default_factory=dict)
    unique: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexed: dict[str, set[str]] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)

    @property
    def tables(self) -> list[str]:
        return sorted(self.columns)

    def known(self, name: str) -> bool:
        return name in self.columns

    def neighbours(self, table: str) -> set[str]:
        found: set[str] = set()
        for key in self.foreign_keys:
            if key.child_table == table:
                found.add(key.parent_table)
            if key.parent_table == table:
                found.add(key.child_table)
        return found

    def parents_of(self, table: str) -> list[ForeignKey]:
        return [k for k in self.foreign_keys if k.child_table == table]

    def children_of(self, table: str) -> list[ForeignKey]:
        return [k for k in self.foreign_keys if k.parent_table == table]

    def fan_in(self, child: str, parent: str) -> float:
        """Estimated child rows per parent row."""
        child_rows, parent_rows = self.rows.get(child, 0), self.rows.get(parent, 1)
        return child_rows / parent_rows if parent_rows else float(child_rows)

    def fan_in_label(self, key: ForeignKey) -> str:
        if key.child_table not in self.rows or key.parent_table not in self.rows:
            return (
                "row counts unavailable, so treat this child side as the larger one and "
                "aggregate or filter it before joining"
            )
        ratio = self.fan_in(key.child_table, key.parent_table)
        if ratio >= 20:
            return f"{ratio:.1f} rows per parent row, many-to-one; the join multiplies rows"
        if ratio >= 1.5:
            return f"{ratio:.1f} rows per parent row, one-to-many"
        if ratio < 1:
            return (
                f"{ratio:.3f} rows per parent row, so this child key is close to unique "
                "and the join will not multiply the parent"
            )
        return f"{ratio:.1f} rows per parent row, roughly one-to-one"

    def order_for_sampling(self) -> list[str]:
        """Tables with parents before children, so a sample keeps its keys valid."""
        ordered: list[str] = []
        state: dict[str, int] = {}

        def visit(table: str) -> None:
            if state.get(table) == 2:
                return
            if state.get(table) == 1:
                return  # a cycle; the remaining edge is broken on purpose
            state[table] = 1
            for key in self.parents_of(table):
                if key.parent_table in self.columns:
                    visit(key.parent_table)
            state[table] = 2
            ordered.append(table)

        for table in self.tables:
            visit(table)
        return ordered

    def scope(self, sql: str, dialect: str, budget: int = 8) -> list[str]:
        """Tables worth showing for ``sql``, nearest to the query first."""
        seeds = [t for t in tables_in(sql, dialect) if self.known(t)]
        if not seeds:
            return self.tables[:budget]

        ordered: list[str] = []
        seen: set[str] = set()
        frontier = list(seeds)
        while frontier and len(ordered) < budget:
            layer: list[str] = []
            for table in sorted(frontier):
                if table in seen or not self.known(table):
                    continue
                seen.add(table)
                ordered.append(table)
                layer.extend(self.neighbours(table) - seen)
                if len(ordered) >= budget:
                    break
            frontier = sorted(layer)
        return ordered[:budget]

    def ddl(self, tables: Iterable[str] | None = None) -> str:
        """Compact DDL annotated with keys, indexes and nullability."""
        wanted = set(tables) if tables is not None else set(self.tables)
        blocks = []
        for table in sorted(wanted & set(self.columns)):
            lines = []
            for column, kind in self.columns[table].items():
                marks = []
                if column in self.primary.get(table, []):
                    marks.append("PRIMARY KEY")
                if (column,) in self.unique.get(table, []):
                    marks.append("UNIQUE")
                if column in self.indexed.get(table, ()):
                    marks.append("indexed")
                if column not in self.not_null.get(table, ()):
                    marks.append("nullable")
                suffix = ("  -- " + ", ".join(marks)) if marks else ""
                lines.append(f"  {column} {kind or 'UNKNOWN'}{suffix}")
            blocks.append(f"CREATE TABLE {table} (\n" + ",\n".join(lines) + "\n);")
        return "\n".join(blocks)

    def edges(self, tables: Iterable[str] | None = None) -> str:
        """The joinable key pairs, with fan-out and index status."""
        wanted = set(tables) if tables is not None else set(self.tables)
        lines = []
        for key in self.foreign_keys:
            if key.child_table in wanted and key.parent_table in wanted:
                status = "indexed" if key.child_column in self.indexed.get(key.child_table, ()) else "NO INDEX"
                lines.append(f"  {key.child} -> {key.parent}  [{status}] {self.fan_in_label(key)}")
        return "\n".join(lines)

    def render(self, sql: str, dialect: str, budget: int = 8) -> str:
        """The full schema-awareness block for one query."""
        scope = self.scope(sql, dialect, budget)
        parts = [self.ddl(scope)]
        if len(scope) < len(self.tables):
            parts.append(
                f"Only these {len(scope)} tables are in scope. Any other table is off limits "
                "even if it looks like a better fit."
            )
        counts = ", ".join(f"{t} {self.rows[t]:,}" for t in scope if t in self.rows)
        if counts:
            parts.append(f"Approximate row counts: {counts}")
        edges = self.edges(scope)
        if edges:
            parts.append("Joinable key pairs (the only joinable pairs that keep an index):\n" + edges)
        return "\n\n".join(parts)


def tables_in(sql: str, dialect: str = "postgres") -> set[str]:
    """Real tables a query reads, minus the CTEs it defines itself."""
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except (sqlglot.ParseError, ValueError):
        return set()
    if tree is None:
        return set()
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    return {table.name for table in tree.find_all(exp.Table)} - ctes


def short_type(data_type: str) -> str:
    return TYPE_ALIASES.get(str(data_type).lower(), str(data_type).upper())


def from_rows(topics: dict[str, list[tuple]], schema: str = "public", engine: str = "postgres") -> Catalog:
    """Build a catalog from raw introspection rows."""
    catalog = Catalog(schema=schema)
    for row in topics.get("columns", []):
        table, column, dtype = row[0], row[1], row[2]
        nullable = row[3] if len(row) > 3 else "YES"
        catalog.columns.setdefault(table, {})[column] = short_type(dtype)
        if str(nullable).upper() == "NO":
            catalog.not_null.setdefault(table, set()).add(column)

    slots: dict[str, dict[str, list[str]]] = {}
    for table, constraint, column, kind in topics.get("keys", []):
        slot = "PRIMARY KEY" if str(kind).upper().startswith("PRIMARY") else f"UNIQUE:{constraint}"
        slots.setdefault(table, {}).setdefault(slot, []).append(column)
    for table, kinds in slots.items():
        if "PRIMARY KEY" in kinds:
            catalog.primary[table] = kinds["PRIMARY KEY"]
        composites = [tuple(cols) for name, cols in kinds.items() if name.startswith("UNIQUE:")]
        if composites:
            catalog.unique[table] = composites

    catalog.foreign_keys = [ForeignKey(*row) for row in topics.get("foreign_keys", []) if row[0]]

    for row in topics.get("indexed_columns", []):
        table, column = row[0], row[1]
        if engine == "duckdb" or not IDENT.match(str(column)):
            for name in _index_column_names(column):
                catalog.indexed.setdefault(table, set()).add(name)
        else:
            catalog.indexed.setdefault(table, set()).add(str(column))

    catalog.rows = {r[0]: int(r[1]) for r in topics.get("row_counts", []) if r[0] and r[1]}

    for table, columns in catalog.primary.items():
        catalog.not_null.setdefault(table, set()).update(columns)
    return catalog


def _index_column_names(value: str) -> set[str]:
    """Column names from an index definition, or from a bare column name."""
    text = str(value)
    inner = re.search(r"\((.*)\)", text, re.DOTALL)
    if not inner:
        return {text.strip('"')}
    names = set()
    for part in inner.group(1).split(","):
        token = re.split(r"[^A-Za-z0-9_\"]", part.strip())[0]
        if token:
            names.add(token.strip('"'))
    return names


def introspect(dialect, conn, schema: str, exact_counts: bool = False) -> Catalog:
    """Read the catalog and statistics from a live database."""
    catalog = from_rows(dialect.catalog(conn, schema), schema, engine=dialect.name)
    if exact_counts:
        missing = [t for t in catalog.columns if t not in catalog.rows]
        for table in missing:
            quoted = dialect.ref(schema, table)
            try:
                catalog.rows[table] = int(dialect.fetch(conn, f"SELECT count(*) FROM {quoted}", 1).rows[0][0])
            except Exception:  # noqa: BLE001 - a missing count is not fatal
                pass
    return catalog


CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;", re.IGNORECASE | re.DOTALL)
NOT_A_COLUMN = re.compile(r"^(PRIMARY|FOREIGN|UNIQUE|KEY|CONSTRAINT|INDEX|CHECK)", re.IGNORECASE)
PRIMARY_KEY = re.compile(r"PRIMARY\s+KEY\s*\(([^)]*)\)", re.IGNORECASE)
UNIQUE_KEY = re.compile(r"UNIQUE\s*(?:KEY|INDEX)?\s*\(([^)]*)\)", re.IGNORECASE)
FOREIGN_KEY = re.compile(r"FOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE)
INLINE_REFERENCE = re.compile(r"REFERENCES\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE)
TYPE_NAMES = (
    r"integer|bigint|smallint|numeric|decimal|real|double precision|float|text|"
    r"character varying|character|varchar|char|boolean|date|time|timestamp|uuid|json|jsonb|serial|bigserial"
)


def _clean(columns: str) -> list[str]:
    return [c.strip().strip('"').strip("`") for c in columns.split(",") if c.strip()]


def from_dump(dump_path, schema: str = "public") -> Catalog:
    """Build a catalog from a SQL dump, with no database involved.

    Keys and foreign keys are read out of the dump and every key column is
    treated as indexed, because a database restored from a dump will have those
    indexes. Row counts are unknown, and the prompt says so rather than
    guessing.
    """
    text = Path(dump_path).read_text(encoding="utf-8", errors="replace")
    catalog = Catalog(schema=schema)
    for table, body in CREATE_TABLE.findall(text):
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or NOT_A_COLUMN.match(line):
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            name, rest = parts[0].strip('"'), parts[1]
            kind = re.match(rf"({TYPE_NAMES})\b", rest, re.IGNORECASE)
            catalog.columns.setdefault(table, {})[name] = short_type(kind.group(1)) if kind else rest.split()[0].upper()
            if "NOT NULL" in rest.upper():
                catalog.not_null.setdefault(table, set()).add(name)
            inline = INLINE_REFERENCE.search(rest)
            if inline:
                catalog.foreign_keys.append(
                    ForeignKey(table, name, inline.group(1), inline.group(2).strip().strip('"'))
                )

        primary = PRIMARY_KEY.search(body)
        if primary:
            catalog.primary[table] = _clean(primary.group(1))
        composites = [tuple(_clean(c)) for c in UNIQUE_KEY.findall(body) if not PRIMARY_KEY.search(c)]
        if composites:
            catalog.unique[table] = composites
        for child, parent, parent_cols in FOREIGN_KEY.findall(body):
            for column, referenced in zip(_clean(child), _clean(parent_cols)):
                catalog.foreign_keys.append(ForeignKey(table, column, parent, referenced))

    for key in catalog.foreign_keys:
        catalog.indexed.setdefault(key.child_table, set()).add(key.child_column)
    for table, columns in catalog.primary.items():
        catalog.indexed.setdefault(table, set()).update(columns)
        catalog.not_null.setdefault(table, set()).update(columns)
    for table, composites in catalog.unique.items():
        catalog.indexed.setdefault(table, set()).update(c for group in composites for c in group)
    return catalog


def from_mapping(columns: dict[str, dict[str, str]], **kwargs) -> Catalog:
    """Build a catalog from a plain ``{table: {column: type}}`` mapping."""
    schema = kwargs.pop("schema", "public")
    if "foreign_keys" in kwargs:
        kwargs["foreign_keys"] = [as_key(key) for key in kwargs["foreign_keys"]]
    return Catalog(schema=schema, columns=columns, **kwargs)