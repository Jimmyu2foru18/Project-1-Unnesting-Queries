"""Prompting strategies for SQL query rewriting."""

from _07_prompts import CONTRACT, DIVERSITY, PLAN, REPAIR, VARIANTS, WRITE, system_for


class PromptStrategy:
    name: str = ""

    def system_prompt(self, hints: str = "") -> str:
        raise NotImplementedError

    def plan_prompt(self, schema: str, sql: str, dialect: str) -> str | None:
        return None

    def write_prompt(self, schema: str, sql: str, dialect: str, plan: str = "") -> str:
        raise NotImplementedError

    def variant_prompt(self, schema: str, sql: str, dialect: str, variant: str) -> str:
        return self.write_prompt(schema, sql, dialect, "")

    def repair_prompt(
        self, schema: str, sql: str, dialect: str, rejected: str, problems: list[str]
    ) -> str:
        raise NotImplementedError


class ExistingStrategy(PromptStrategy):
    name = "existing"

    def system_prompt(self, hints: str = "") -> str:
        return system_for(hints)

    def plan_prompt(self, schema: str, sql: str, dialect: str) -> str:
        return PLAN.format(schema=schema, sql=sql, dialect=dialect)

    def write_prompt(self, schema: str, sql: str, dialect: str, plan: str = "") -> str:
        return WRITE.format(plan=plan, schema=schema, sql=sql, dialect=dialect)

    def variant_prompt(self, schema: str, sql: str, dialect: str, variant: str) -> str:
        return VARIANTS.format(variant=variant, schema=schema, sql=sql, dialect=dialect)

    def repair_prompt(
        self, schema: str, sql: str, dialect: str, rejected: str, problems: list[str]
    ) -> str:
        return REPAIR.format(
            problems="\n".join(f"- {p}" for p in problems),
            rejected=rejected,
            schema=schema,
            sql=sql,
            dialect=dialect,
        )


class ZeroShotStrategy(PromptStrategy):
    name = "zero-shot"

    def system_prompt(self, hints: str = "") -> str:
        base = CONTRACT
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
        base = CONTRACT
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
    name = "reasoning"

    def system_prompt(self, hints: str = "") -> str:
        base = (
            CONTRACT
            + "\n\n"
            + "When rewriting, always start your response with an ANALYSIS section "
            + "followed by a SQL section. Do not expose private chain-of-thought. "
            + "Keep the analysis concise and structured.\n"
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


def get_strategy(name: str):
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
    "StructuredReasoningStrategy",
    "ZeroShotStrategy",
    "get_strategy",
]