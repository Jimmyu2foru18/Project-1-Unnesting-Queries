"""Prompting strategies."""

import pytest

from _03_catalog import from_mapping
from _02_dialects import get_dialect
from _06_llm import ChatModel, Reply
from _10_rewrite import Rewriter
from _09_strategies import (
    ExistingStrategy,
    OneShotStrategy,
    StructuredReasoningStrategy,
    ZeroShotStrategy,
    get_strategy,
)
import _13_cli as cli

DIALECT = get_dialect("duckdb")
CATALOG = from_mapping(
    {"movies": {"id": "INT", "title": "TEXT", "year": "INT"},
     "ratings": {"movie_id": "INT", "rating": "INT"}},
    schema="main",
    primary={"movies": ["id"]},
    rows={"movies": 1000, "ratings": 50_000},
    foreign_keys=[("ratings", "movie_id", "movies", "id")],
)

ORIGINAL = (
    "SELECT m.title FROM movies m "
    "WHERE (SELECT avg(r.rating) FROM ratings r WHERE r.movie_id = m.id) > 7.0"
)

GOOD = "SELECT m.title FROM movies m JOIN ratings r ON r.movie_id = m.id GROUP BY m.id, m.title HAVING avg(r.rating) > 7.0"


class Scripted(ChatModel):
    """Answers each prompt in turn, then repeats whatever it last said."""

    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.efforts = []
        self.last = None

    def _once(self, system, user, effort, temperature, budget):
        self.prompts.append(user)
        self.efforts.append(effort)
        if self.replies:
            self.last = self.replies.pop(0)
        return self.last


def plan_then(sql, count=1):
    return [Reply("join, then group and filter on the average", "", "m", "high")] + [
        Reply(sql, "", "m", "low") for _ in range(count)
    ]


def rewriter(strategy, replies, **kwargs):
    return Rewriter(Scripted(replies), DIALECT, CATALOG, strategy, **kwargs)


class TestGetStrategy:
    def test_existing_is_returned_by_name(self):
        assert isinstance(get_strategy("existing"), ExistingStrategy)

    def test_zero_shot_is_returned_by_name(self):
        assert isinstance(get_strategy("zero-shot"), ZeroShotStrategy)

    def test_one_shot_is_returned_by_name(self):
        assert isinstance(get_strategy("one-shot"), OneShotStrategy)

    def test_reasoning_is_returned_by_name(self):
        assert isinstance(get_strategy("reasoning"), StructuredReasoningStrategy)

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError, match="unknown strategy"):
            get_strategy("nonsense")

    def test_strategy_names_are_lowercase(self):
        assert get_strategy("EXISTING").name == "existing"


class TestExistingStrategy:
    def test_uses_plan_then_write(self):
        result = rewriter(ExistingStrategy(), plan_then(GOOD, 2)).rewrite("Q01", "d", ORIGINAL)
        assert result.ok
        assert "join, then group" in result.plan

    def test_repair_prompt_contains_rejected_sql(self):
        UPDATABLE = "UPDATE movies SET year = 2000"
        model = Scripted(plan_then(UPDATABLE) + [Reply(GOOD, "", "m", "medium")])
        result = Rewriter(model, DIALECT, CATALOG, ExistingStrategy(), variants=1).rewrite("Q01", "d", ORIGINAL)
        assert result.ok
        repair_prompt = model.prompts[2]
        assert "read-only" in repair_prompt.lower() or "UPDATE" in repair_prompt
        assert result.accepted[0].attempts == 2


class TestZeroShotStrategy:
    def test_no_plan_turn(self):
        result = rewriter(ZeroShotStrategy(), plan_then(GOOD, 2)).rewrite("Q01", "d", ORIGINAL)
        assert result.plan == ""

    def test_write_prompt_has_no_example(self):
        strat = ZeroShotStrategy()
        prompt = strat.write_prompt("schema", "SELECT 1", "duckdb")
        assert "example" not in prompt.lower()
        assert "SELECT 1" in prompt

    def test_system_prompt_has_no_example(self):
        strat = ZeroShotStrategy()
        assert "example" not in strat.system_prompt().lower()

    def test_accepts_clean_candidate(self):
        result = rewriter(ZeroShotStrategy(), [Reply("join, then group", "", "m", "high"), Reply(GOOD, "", "m", "low")]).rewrite("Q01", "d", ORIGINAL)
        assert result.ok

    def test_strategy_name_is_recorded(self):
        result = rewriter(ZeroShotStrategy(), plan_then(GOOD, 2)).rewrite("Q01", "d", ORIGINAL)
        assert result.strategy == "zero-shot"
        assert result.candidates[0].strategy == "zero-shot"


class TestOneShotStrategy:
    def test_contains_exactly_one_example(self):
        strat = OneShotStrategy()
        prompt = strat.write_prompt("schema", ORIGINAL, "duckdb")
        assert "example" in prompt.lower() or "Input query" in prompt
        assert "SELECT m.title FROM movies m" in prompt

    def test_write_prompt_contains_target_query(self):
        strat = OneShotStrategy()
        prompt = strat.write_prompt("schema", ORIGINAL, "duckdb")
        assert ORIGINAL in prompt

    def test_system_prompt_has_no_example(self):
        strat = OneShotStrategy()
        assert "example" not in strat.system_prompt().lower()

    def test_accepts_clean_candidate(self):
        result = rewriter(OneShotStrategy(), [Reply("join, then group", "", "m", "high"), Reply(GOOD, "", "m", "low")]).rewrite("Q01", "d", ORIGINAL)
        assert result.ok

    def test_strategy_name_is_recorded(self):
        result = rewriter(OneShotStrategy(), plan_then(GOOD, 2)).rewrite("Q01", "d", ORIGINAL)
        assert result.strategy == "one-shot"


class TestStructuredReasoningStrategy:
    def test_prompts_for_structured_output(self):
        strat = StructuredReasoningStrategy()
        prompt = strat.write_prompt("schema", ORIGINAL, "duckdb")
        assert "ANALYSIS:" in prompt
        assert "SQL:" in prompt
        assert "chain-of-thought" not in prompt.lower()

    def test_no_plan_turn(self):
        result = rewriter(StructuredReasoningStrategy(), plan_then(GOOD, 2)).rewrite("Q01", "d", ORIGINAL)
        assert result.plan == ""

    def test_system_prompt_forbids_private_chain_of_thought(self):
        strat = StructuredReasoningStrategy()
        prompt = strat.system_prompt()
        assert "chain-of-thought" in prompt.lower() or "private chain" in prompt.lower()

    def test_accepts_candidate_with_sql_in_reply(self):
        result = rewriter(StructuredReasoningStrategy(), [Reply("analysis first", "", "m", "high"), Reply(GOOD, "", "m", "low")]).rewrite("Q01", "d", ORIGINAL)
        assert result.ok

    def test_strategy_name_is_recorded(self):
        result = rewriter(StructuredReasoningStrategy(), plan_then(GOOD, 2)).rewrite("Q01", "d", ORIGINAL)
        assert result.strategy == "reasoning"


class TestStrategySelection:
    def test_cli_parser_accepts_all_strategies(self):
        parser = cli.build_parser()
        for name in ("existing", "zero-shot", "one-shot", "reasoning"):
            args = parser.parse_args(["rewrite", "--strategy", name])
            assert args.strategy == name

    def test_cli_compare_command_exists(self):
        parser = cli.build_parser()
        args = parser.parse_args(["compare", "a.json", "b.json"])
        assert args.command == "compare"
        assert args.files == ["a.json", "b.json"]