"""Workloads: sets of queries to rewrite.

A workload is a directory holding a ``queries.sql`` file and an optional
``workload.json``. The SQL file uses ``-- Q01: description`` headers, the same
shape the annotated SQL in this project already used, so an existing query set
becomes a workload by moving it into a folder and adding one small file.

``workload.json`` carries the two things a query set cannot express: the
database it is meant to run against, and extra guidance for the model.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# A header is an id that starts with letters and contains a digit, so ids like
# Q01 or J01 are recognised while ordinary annotation lines such as
# "-- Type: correlated subquery" are treated as part of the statement.
HEADER = re.compile(r"^--\s*([A-Za-z]+[0-9][A-Za-z0-9_]*)\s*:\s*(.*)$")
FENCE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)


@dataclass
class Query:
    """One query in a workload."""

    qid: str
    sql: str
    description: str = ""

    def __str__(self) -> str:
        return f"{self.qid}: {self.description}" if self.description else self.qid


@dataclass
class Workload:
    """A named set of queries plus how to run it."""

    name: str
    path: Path
    queries: list[Query] = field(default_factory=list)
    engine: str = ""
    schema: str = ""
    dsn: str = ""
    hints: str = ""
    tags: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.queries)

    def __iter__(self):
        return iter(self.queries)

    def limit(self, count: int) -> "Workload":
        if count and count < len(self.queries):
            trimmed = Workload(**{**self.__dict__, "queries": self.queries[:count]})
            return trimmed
        return self

    def only(self, qids: list[str] | None) -> "Workload":
        if not qids:
            return self
        wanted = {q.strip().upper() for q in qids}
        chosen = [q for q in self.queries if q.qid.upper() in wanted]
        return Workload(**{**self.__dict__, "queries": chosen})


def _clean_body(lines: list[str]) -> str:
    """Join statement lines, dropping comment-only metadata.

    Workload files annotate each query with extra comment lines describing its
    type and complexity. That text belongs in the description, not in the
    statement, and leaving it in bloats the prompt and the reports.
    """
    body = "\n".join(line for line in lines if not line.strip().startswith("--"))
    return FENCE.sub("", body).strip().rstrip(";").strip()


def parse_sql(text: str) -> list[Query]:
    """Read ``-- Q01: description`` annotated statements out of a SQL file."""
    queries: list[Query] = []
    qid: str | None = None
    description = ""
    lines: list[str] = []

    def flush() -> None:
        if qid and lines:
            body = _clean_body(lines)
            if body:
                queries.append(Query(qid=qid, sql=body, description=description))

    for line in text.splitlines():
        header = HEADER.match(line.strip())
        if header:
            flush()
            qid, description = header.group(1), header.group(2).strip()
            lines = []
            continue
        if qid:
            lines.append(line)
            # A comment line can end in a semicolon, so only a real statement
            # line terminates the block.
            if line.strip().endswith(";") and not line.strip().startswith("--"):
                flush()
                qid, description, lines = None, "", []
    flush()
    return queries


def load(path: str | Path) -> Workload:
    """Load a workload from a directory or a bare ``.sql`` file."""
    given = Path(path)
    directory = given if given.is_dir() else given.parent
    sql_file = given / "queries.sql" if given.is_dir() else given
    if not sql_file.exists():
        raise FileNotFoundError(f"no queries file at {sql_file}")

    workload = Workload(name=directory.name, path=directory, queries=parse_sql(sql_file.read_text(encoding="utf-8")))
    meta_file = directory / "workload.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        for key in ("engine", "schema", "dsn", "hints"):
            if meta.get(key):
                setattr(workload, key, str(meta[key]))
        workload.tags = list(meta.get("tags", []))
        if meta.get("name"):
            workload.name = str(meta["name"])
    return workload


def discover(root: str | Path = "workloads") -> list[Workload]:
    """Every workload under a directory."""
    base = Path(root)
    if not base.exists():
        return []
    found = []
    for candidate in sorted(base.rglob("queries.sql")):
        try:
            found.append(load(candidate.parent))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return found