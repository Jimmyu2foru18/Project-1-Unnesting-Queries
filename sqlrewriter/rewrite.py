"""Generating candidates for one query.

A rewrite is not a single answer. One plan is produced, then several statements
are attempted from it, each pushed in a different direction so the options are
genuinely different rather than re-samplings of the same idea. Every candidate
is checked, and a rejected one is repaired with the specific reasons attached
rather than a request to try again.

Nothing here decides which candidate is best. Verification says whether one is
allowed to run; :mod:`sqlrewriter.rank` decides which is worth running.
"""
from dataclasses import dataclass, field

from sqlrewriter import prompts, verify
from sqlrewriter.catalog import Catalog
from sqlrewriter.dialects import Dialect
from sqlrewriter.llm import ChatModel, ModelError, Reply


@dataclass
class Candidate:
    """One proposed rewrite and what happened to it."""

    sql: str
    label: str = ""
    plan: str = ""
    reasoning: str = ""
    attempts: int = 1
    verdict: verify.Verdict = field(default_factory=verify.Verdict)
    equivalence: object | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict.ok


@dataclass
class RewriteResult:
    """Everything attempted for one query."""

    qid: str
    description: str
    original_sql: str
    plan: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    error: str | None = None

    @property
    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates if c.accepted]

    @property
    def ok(self) -> bool:
        return bool(self.accepted)


class Rewriter:
    """Drives plan, emit, diversify and repair for a single query."""

    def __init__(
        self,
        model: ChatModel,
        dialect: Dialect,
        catalog: Catalog,
        *,
        variants: int = 3,
        attempts: int = 2,
        schema_budget: int = 8,
        hints: str = "",
        plan_effort: str = "high",
        write_effort: str = "low",
        repair_effort: str = "medium",
    ) -> None:
        self.model = model
        self.dialect = dialect
        self.catalog = catalog
        self.variants = max(1, variants)
        self.attempts = max(1, attempts)
        self.schema_budget = schema_budget
        self.hints = hints
        self.plan_effort = plan_effort
        self.write_effort = write_effort
        self.repair_effort = repair_effort
        self.system = prompts.system_for(hints)

    def schema_block(self, sql: str) -> str:
        return self.catalog.render(sql, self.dialect.sqlglot, self.schema_budget)

    def plan_rewrite(self, original_sql: str) -> Reply:
        """The analysis turn: no SQL, only the shape of the better query."""
        user = prompts.PLAN.format(
            schema=self.schema_block(original_sql),
            sql=original_sql,
            dialect=self.dialect.name,
        )
        return self.model.chat(self.system, user, effort=self.plan_effort, budget=8192)

    def emit(self, original_sql: str, plan: str, variant: str = "") -> Reply:
        """One candidate, from the plan or along a named alternative route."""
        schema = self.schema_block(original_sql)
        if variant:
            user = prompts.VARIANTS.format(
                variant=variant, schema=schema, sql=original_sql, dialect=self.dialect.name
            )
            return self.model.chat(self.system, user, effort=self.write_effort)
        user = prompts.WRITE.format(
            plan=plan, schema=schema, sql=original_sql, dialect=self.dialect.name
        )
        return self.model.chat(self.system, user, effort=self.write_effort)

    def repair(self, original_sql: str, rejected: str, problems: list[str]) -> Reply:
        user = prompts.REPAIR.format(
            problems="\n".join(f"- {p}" for p in problems),
            rejected=rejected,
            schema=self.schema_block(original_sql),
            sql=original_sql,
            dialect=self.dialect.name,
        )
        return self.model.chat(self.system, user, effort=self.repair_effort, budget=8192)

    def rewrite(self, qid: str, description: str, original_sql: str) -> RewriteResult:
        """Produce and check every candidate for one query."""
        result = RewriteResult(qid=qid, description=description, original_sql=original_sql)
        try:
            analysis = self.plan_rewrite(original_sql)
        except ModelError as err:
            result.error = f"planning failed: {err}"
            return result
        result.plan = _text(analysis)

        seen: set[str] = set()
        routes = [("", "plan")] + [
            (prompts.DIVERSITY[(i + len(qid)) % len(prompts.DIVERSITY)], f"variant-{i}")
            for i in range(self.variants - 1)
        ]
        for variant, label in routes:
            candidate = self._attempt(original_sql, result.plan, variant, label, seen)
            if candidate is not None:
                result.candidates.append(candidate)
        return result

    def _attempt(
        self, original_sql: str, plan: str, variant: str, label: str, seen: set[str]
    ) -> Candidate | None:
        """Emit one candidate and repair it until the gates accept it."""
        problems: list[str] = []
        rejected = ""
        try:
            reply = self.emit(original_sql, plan, variant)
        except ModelError as err:
            return Candidate(sql="", label=label, plan=plan, verdict=verify.Verdict(
                False, [f"generation failed: {err}"]))

        for attempt in range(1, self.attempts + 1):
            if not reply.sql:
                problems, rejected = ["the model returned no SQL"], ""
            elif reply.sql in seen:
                problems, rejected = ["this is a duplicate of a candidate already tried"], reply.sql
            else:
                verdict = verify.check(reply.sql, original_sql, self.dialect, self.catalog)
                if verdict.ok:
                    seen.add(reply.sql)
                    return Candidate(reply.sql, label, plan, reply.reasoning, attempt, verdict)
                problems, rejected = verdict.problems, reply.sql

            if attempt == self.attempts:
                break
            try:
                reply = self.repair(original_sql, rejected, problems)
            except ModelError as err:
                problems = [f"repair failed: {err}"]
                break
        return Candidate(rejected, label, plan, reply.reasoning, self.attempts,
                         verify.Verdict(False, problems))


def _text(reply: Reply) -> str:
    """The plan: the final message when there is one, otherwise the reasoning.

    A high-effort plan turn can end inside the reasoning channel when it hits its
    budget. That text is still a plan, so it is used rather than discarded.
    """
    return reply.sql.strip() or reply.reasoning.strip()