"""Core types used project wide."""
from dataclasses import dataclass, field
from typing import NamedTuple


class StatementRejected(RuntimeError):
	"""A statement was refused before it reached the engine."""


class QueryFailed(RuntimeError):
	"""The engine rejected or failed to run a statement."""


class ModelError(RuntimeError):
	"""The model never produced an answer, or could not be reached."""


class ForeignKey(NamedTuple):
	"""A directed key edge: child.column points at parent.column."""

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


@dataclass(frozen=True)
class Result:
	"""Rows returned by a statement, with column names and a truncation flag."""

	rows: tuple[tuple, ...] = ()
	columns: tuple[str, ...] = ()
	truncated: bool = False

	def __len__(self) -> int:
		return len(self.rows)


@dataclass(frozen=True)
class Timing:
	"""Wall clock cost of a statement, median of several runs."""

	median_ms: float = 0.0
	runs: int = 0
	error: str | None = None

	@property
	def ok(self) -> bool:
		return self.error is None


@dataclass(frozen=True)
class Plan:
	"""Best effort plan facts."""

	execution_ms: float | None = None
	max_loops: int = 1
	seq_scans: tuple[str, ...] = ()
	text: str = ""


@dataclass(frozen=True)
class Reply:
	"""One model turn."""

	sql: str = ""
	reasoning: str = ""
	model: str = ""
	effort: str = ""
	tokens: int = 0
	raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Verdict:
	"""Whether a candidate may proceed, and why not."""

	ok: bool = True
	problems: tuple[str, ...] = ()
	notes: tuple[str, ...] = ()

	def fail(self, problem: str) -> "Verdict":
		return Verdict(ok=False, problems=self.problems + (problem,), notes=self.notes)

	def note(self, note: str) -> "Verdict":
		return Verdict(ok=self.ok, problems=self.problems, notes=self.notes + (note,))

	def absorb(self, problems: list[str]) -> "Verdict":
		new_problems = self.problems + tuple(problems)
		return Verdict(ok=False if new_problems else self.ok, problems=new_problems, notes=self.notes)