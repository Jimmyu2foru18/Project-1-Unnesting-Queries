"""Prompting strategies for SQL query rewriting.

Each strategy controls the prompts sent to the model. Verification, timing,
ranking, and reporting are strategy-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from sqlrewriter import prompts


class PromptStrategy(ABC):
    """Abstract base for prompting strategies."""

    name: str = ""

    @abstractmethod
    def system_prompt(self, hints: str = "") -> str:
        """The system prompt, including any workload hints."""
        raise NotImplementedError

    def plan_prompt(self, schema: str, sql: str, dialect: str) -> str | None:
        """Return a planning prompt, or ``None`` to skip the planning turn."""
        return None

    @abstractmethod
    def write_prompt(self, schema: str, sql: str, dialect: str, plan: str = "") -> str:
        """The prompt that asks the model to produce SQL."""
        raise NotImplementedError

    def variant_prompt(self, schema: str, sql: str, dialect: str, variant: str) -> str:
        """The prompt for an alternative rewrite route."""
        return self.write_prompt(schema, sql, dialect, "")

    @abstractmethod
    def repair_prompt(
        self, schema: str, sql: str, dialect: str, rejected: str, problems: list[str]
    ) -> str:
        """The prompt that asks the model to fix a rejected rewrite."""
        raise NotImplementedError


class ExistingStrategy(PromptStrategy):
    """The original plan-then-write pipeline with repair."""

    name = "existing"

    def system_prompt(self, hints: str = "") -> str:
        return prompts.system_for(hints)

    def plan_prompt(self, schema: str, sql: str, dialect: str) -> str:
        return prompts.PLAN.format(schema=schema, sql=sql, dialect=dialect)

    def write_prompt(self, schema: str, sql: str, dialect: str, plan: str = "") -> str:
        return prompts.WRITE.format(plan=plan, schema=schema, sql=sql, dialect=dialect)

    def variant_prompt(self, schema: str, sql: str, dialect: str, variant: str) -> str:
        return prompts.VARIANTS.format(
            variant=variant, schema=schema, sql=sql, dialect=dialect
        )

    def repair_prompt(
        self, schema: str, sql: str, dialect: str, rejected: str, problems: list[str]
    ) -> str:
        return prompts.REPAIR.format(
            problems="\n".join(f"- {p}" for p in problems),
            rejected=rejected,
            schema=schema,
            sql=sql,
            dialect=dialect,
        )


class ZeroShotStrategy(PromptStrategy):
    """A single direct rewrite request, no examples, no planning turn."""

    name = "zero-shot"

    def system_prompt(self, hints: str = "") -> str:
        base = prompts.CONTRACT
        if hints.strip():
            base += "\n" + hints.strip() + "\n"
        return base

    def write_prompt(self, schema: str, sql: str, dialect: str, plan: str = "") -> str:
        return (
            "Rewrite the following SQL query so it runs faster while returning exactly the same rows.\n\n"
            f"Schema:\n{schema}\n\n"
            f"Original query:\n{sql}\n\n"
            f"Target database: {dialect}\n\n"
            "Return only the rewritten SQL statement. No preamble, no commentary, no markdown fences.\n"
        )

    def repair_prompt(
        self, schema: str, sql: str, dialect: str, rejected: str, problems: list[str]
    ) -> str:
        return (
            "This rewrite was rejected. Fix it and return the whole statement again.\n\n"
            "Reasons:\n"
            + "\n".join(f"- {p}" for p in problems)
            + f"\n\nRejected rewrite:\n{rejected}\n\n"
            f"Schema:\n{schema}\n\n"
            f"Original query:\n{sql}\n\n"
            f"Target database: {dialect}\n\n"
            "Return only the corrected SQL statement. No preamble, no commentary, no markdown fences.\n"
        )


class OneShotStrategy(PromptStrategy):
    """One worked example followed by the target query."""

    name = "one-shot"

    EXAMPLE_INPUT = (
        "SELECT m.title FROM movies m "
        "WHERE (SELECT avg(r.rating) FROM ratings r WHERE r.movie_id = m.id) > 7.0"
    )
    EXAMPLE_OUTPUT = (
        "SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id "
        "GROUP BY m.id, m.title HAVING avg(r.rating) > 7.0"
    )

    def system_prompt(self, hints: str = "") -> str:
        base = prompts.CONTRACT
        if hints.strip():
            base += "\n" + hints.strip() + "\n"
        return base

    def write_prompt(self, schema: str, sql: str, dialect: str, plan: str = "") -> str:
        return (
            "Here is an example of unnesting a nested SQL query:\n\n"
            "Input query:\n"
            f"{self.EXAMPLE_INPUT}\n\n"
            "Rewritten query:\n"
            f"{self.EXAMPLE_OUTPUT}\n\n"
            "Now rewrite this query using the same approach:\n\n"
            f"Schema:\n{schema}\n\n"
            f"Input query:\n{sql}\n\n"
            f"Target database: {dialect}\n\n"
            "Return only the rewritten SQL statement. No preamble, no commentary, no markdown fences.\n"
        )

    def repair_prompt(
        self, schema: str, sql: str, dialect: str, rejected: str, problems: list[str]
    ) -> str:
        return (
            "This rewrite was rejected. Fix it and return the whole statement again.\n\n"
            "Reasons:\n"
            + "\n".join(f"- {p}" for p in problems)
            + f"\n\nRejected rewrite:\n{rejected}\n\n"
            f"Schema:\n{schema}\n\n"
            f"Original query:\n{sql}\n\n"
            f"Target database: {dialect}\n\n"
            "Return only the corrected SQL statement. No preamble, no commentary, no markdown fences.\n"
        )


class StructuredReasoningStrategy(PromptStrategy):
    """Ask the model for a structured analysis block before the SQL."""

    name = "reasoning"

    def system_prompt(self, hints: str = "") -> str:
        base = (
            prompts.CONTRACT
            + "\n\n"
            + "When rewriting, always start your response with an ANALYSIS section "
            "followed by a SQL section. Do not expose private chain-of-thought. "
            "Keep the analysis concise and structured.\n"
        )
        if hints.strip():
            base += hints.strip() + "\n"
        return base

    def write_prompt(self, schema: str, sql: str, dialect: str, plan: str = "") -> str:
        return (
            "Rewrite the following SQL query so it runs faster while returning exactly the same rows.\n\n"
            f"Schema:\n{schema}\n\n"
            f"Original query:\n{sql}\n\n"
            f"Target database: {dialect}\n\n"
            "Format your response as:\n\n"
            "ANALYSIS:\n"
            "Nested operation: ...\n"
            "Transformation: ...\n"
            "Semantic considerations: ...\n"
            "Expected result: ...\n\n"
            "SQL:\n"
            "<the rewritten SQL>\n"
        )

    def repair_prompt(
        self, schema: str, sql: str, dialect: str, rejected: str, problems: list[str]
    ) -> str:
        return (
            "This rewrite was rejected. Fix it and return the whole response again.\n\n"
            "Reasons:\n"
            + "\n".join(f"- {p}" for p in problems)
            + f"\n\nRejected rewrite:\n{rejected}\n\n"
            f"Schema:\n{schema}\n\n"
            f"Original query:\n{sql}\n\n"
            f"Target database: {dialect}\n\n"
            "Format your response as:\n\n"
            "ANALYSIS:\n"
            "Nested operation: ...\n"
            "Transformation: ...\n"
            "Semantic considerations: ...\n"
            "Expected result: ...\n\n"
            "SQL:\n"
            "<the corrected SQL>\n"
        )


def get_strategy(name: str) -> PromptStrategy:
    """Build a strategy by name."""
    normalised = name.strip().lower()
    registry = {
        ExistingStrategy.name: ExistingStrategy,
        ZeroShotStrategy.name: ZeroShotStrategy,
        OneShotStrategy.name: OneShotStrategy,
        StructuredReasoningStrategy.name: StructuredReasoningStrategy,
    }
    try:
        return registry[normalised]()
    except KeyError:
        raise ValueError(
            f"unknown strategy {name!r}; try {sorted(registry)}"
        ) from None


__all__ = [
    "ExistingStrategy",
    "OneShotStrategy",
    "PromptStrategy",
    "StructuredReasoningStrategy",
    "ZeroShotStrategy",
    "get_strategy",
]
