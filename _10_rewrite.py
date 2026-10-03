"""Generate and check candidate rewrites for one query."""

import time
from dataclasses import dataclass, field

from _02_dialects import DuckDB
from _03_catalog import Catalog
from _06_llm import ChatModel, ModelError, Reply
from _07_prompts import DIVERSITY
from _08_verify import Verdict, check
from _09_strategies import ExistingStrategy, PromptStrategy


@dataclass
class Candidate:
    """One proposed rewrite and what happened to it."""

    sql: str
    label: str = ""
    plan: str = ""
    reasoning: str = ""
    attempts: int = 1
    verdict: Verdict = field(default_factory=Verdict)
    equivalence: object | None = None
    strategy: str = ""
    generation_time_ms: float = 0.0
    tokens: int = 0

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
    strategy: str = ""

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
        dialect: DuckDB,
        catalog: Catalog,
        strategy: PromptStrategy | None = None,
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
        self.strategy = strategy or ExistingStrategy()
        self.variants = max(1, variants)
        self.attempts = max(1, attempts)
        self.schema_budget = schema_budget
        self.hints = hints
        self.plan_effort = plan_effort
        self.write_effort = write_effort
        self.repair_effort = repair_effort
        self.system = self.strategy.system_prompt(hints)

    def schema_block(self, sql: str) -> str:
        return self.catalog.render(sql, self.dialect.sqlglot, self.schema_budget)

    def plan_rewrite(self, original_sql: str) -> Reply:
        prompt = self.strategy.plan_prompt(self.schema_block(original_sql), original_sql, self.dialect.name)
        if prompt is None:
            return Reply("", "", self.model.name, self.plan_effort)
        return self.model.chat(self.system, prompt, effort=self.plan_effort, budget=8192)

    def emit(self, original_sql: str, plan: str, variant: str = "") -> Reply:
        schema = self.schema_block(original_sql)
        if variant:
            user = self.strategy.variant_prompt(schema, original_sql, self.dialect.name, variant)
            return self.model.chat(self.system, user, effort=self.write_effort)
        user = self.strategy.write_prompt(schema, original_sql, self.dialect.name, plan)
        return self.model.chat(self.system, user, effort=self.write_effort)

    def repair(self, original_sql: str, rejected: str, problems: list[str]) -> Reply:
        user = self.strategy.repair_prompt(
            self.schema_block(original_sql), original_sql, self.dialect.name, rejected, problems
        )
        return self.model.chat(self.system, user, effort=self.repair_effort, budget=8192)

    def rewrite(self, qid: str, description: str, original_sql: str) -> RewriteResult:
        result = RewriteResult(qid=qid, description=description, original_sql=original_sql, strategy=self.strategy.name)
        try:
            analysis = self.plan_rewrite(original_sql)
        except RuntimeError as err:
            result.error = f"planning failed: {err}"
            return result
        result.plan = _text(analysis)

        seen: set[str] = set()
        routes = [("", "plan")] + [
            (DIVERSITY[(i + len(qid)) % len(DIVERSITY)], f"variant-{i}")
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
        problems: list[str] = []
        rejected = ""
        generation_time_ms = 0.0
        total_tokens = 0
        try:
            started = time.perf_counter()
            reply = self.emit(original_sql, plan, variant)
            generation_time_ms += (time.perf_counter() - started) * 1000.0
            total_tokens += reply.tokens
        except ModelError as err:
            return Candidate(sql="", label=label, plan=plan, verdict=Verdict(
                False, [f"generation failed: {err}"]), strategy=self.strategy.name,
                generation_time_ms=generation_time_ms, tokens=total_tokens)

        for attempt in range(1, self.attempts + 1):
            sql = reply.sql
            if not sql:
                problems, rejected = ["the model returned no SQL"], ""
            elif sql in seen:
                problems, rejected = ["this is a duplicate of a candidate already tried"], sql
            else:
                verdict = check(sql, original_sql, self.dialect, self.catalog)
                if verdict.ok:
                    seen.add(sql)
                    return Candidate(sql, label, plan, reply.reasoning, attempt, verdict,
                                     strategy=self.strategy.name,
                                     generation_time_ms=generation_time_ms,
                                     tokens=total_tokens)
                problems, rejected = verdict.problems, sql

            if attempt == self.attempts:
                break
            try:
                started = time.perf_counter()
                reply = self.repair(original_sql, rejected, problems)
                generation_time_ms += (time.perf_counter() - started) * 1000.0
                total_tokens += reply.tokens
            except ModelError as err:
                problems = [f"repair failed: {err}"]
                break
        return Candidate(rejected, label, plan, reply.reasoning, self.attempts,
                         Verdict(False, problems), strategy=self.strategy.name,
                         generation_time_ms=generation_time_ms, tokens=total_tokens)


def _text(reply: Reply) -> str:
    return reply.sql.strip() or reply.reasoning.strip()