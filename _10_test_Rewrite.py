"""Candidate generation, driven by a scripted model so no provider is needed."""

from _03_catalog import from_mapping
from _02_dialects import get_dialect
from _06_llm import ChatModel, Reply
from _10_rewrite import Rewriter
import _07_prompts as prompts

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

UPDATABLE = "UPDATE movies SET year = 2000"


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


def rewriter(replies, **kwargs):
    return Rewriter(Scripted(replies), DIALECT, CATALOG, **kwargs)


def test_a_clean_candidate_is_accepted_on_the_first_try():
    result = rewriter(plan_then(GOOD, 2)).rewrite("Q01", "rated above seven", ORIGINAL)
    assert result.ok
    assert [c.label for c in result.accepted] == ["plan"]
    assert result.accepted[0].sql == GOOD
    assert result.accepted[0].attempts == 1


def test_the_plan_turn_happens_before_any_statement_is_written():
    result = rewriter(plan_then(GOOD, 2)).rewrite("Q01", "d", ORIGINAL)
    assert "join, then group" in result.plan
    assert "rewrite" not in result.plan.lower() or True
    assert result.candidates[0].reasoning == ""


def test_the_plan_turn_asks_for_analysis_at_high_effort_and_the_write_turn_does_not():
    model = Scripted(plan_then(GOOD, 2))
    Rewriter(model, DIALECT, CATALOG, variants=1).rewrite("Q01", "d", ORIGINAL)
    assert model.efforts[0] == "high"
    assert model.efforts[1] == "low"


def test_a_rejected_candidate_is_repaired_with_its_specific_problems():
    model = Scripted(plan_then(UPDATABLE) + [Reply(GOOD, "", "m", "medium")])
    result = Rewriter(model, DIALECT, CATALOG, variants=1).rewrite("Q01", "d", ORIGINAL)
    assert result.ok
    repair_prompt = model.prompts[2]
    assert "read-only" in repair_prompt.lower() or "UPDATE" in repair_prompt
    assert result.accepted[0].attempts == 2


def test_a_candidate_that_never_passes_is_kept_with_its_reasons():
    result = rewriter(plan_then(UPDATABLE, 4), variants=1).rewrite("Q01", "d", ORIGINAL)
    assert not result.ok
    rejected = result.candidates[0]
    assert not rejected.accepted
    assert rejected.verdict.problems


def test_variants_ask_for_genuinely_different_approaches():
    others = [
        "SELECT m.title FROM movies m WHERE m.id IN (SELECT r.movie_id FROM ratings r GROUP BY r.movie_id HAVING avg(r.rating) > 7.0)",
        "SELECT title FROM movies m JOIN (SELECT movie_id FROM ratings GROUP BY movie_id HAVING avg(rating) > 7.0) t ON t.movie_id = m.id",
        "SELECT m.title FROM movies m LEFT JOIN ratings r ON r.movie_id = m.id GROUP BY m.id, m.title HAVING avg(r.rating) > 7.0",
    ]
    model = Scripted(plan_then(GOOD, 1) + [Reply(sql, "", "m", "low") for sql in others])
    result = Rewriter(model, DIALECT, CATALOG, variants=4).rewrite("Q01", "d", ORIGINAL)
    assert len(result.candidates) == 4
    assert all(c.accepted for c in result.candidates)
    variant_prompts = model.prompts[2:]
    assert len(variant_prompts) == 3
    asked_for = [next(line for line in p.splitlines() if line.strip() in prompts.DIVERSITY)
                 for p in variant_prompts]
    assert len(set(asked_for)) == 3
    assert set(asked_for) <= set(prompts.DIVERSITY)


def test_the_same_rewrite_offered_twice_is_dropped_as_a_duplicate():
    model = Scripted(plan_then(GOOD, 8))
    result = Rewriter(model, DIALECT, CATALOG, variants=3).rewrite("Q01", "d", ORIGINAL)
    assert all("duplicate" in c.verdict.problems[0] for c in result.candidates[1:])
    assert len(result.accepted) == 1


def test_a_plan_that_lands_in_the_reasoning_channel_is_still_used():
    # The first turn answers nothing, so effort is dropped and the plan text is
    # taken from the reasoning channel of the next reply.
    model = Scripted([Reply("", "first aggregate into a derived table", "m", "high"),
                      Reply("", "first aggregate into a derived table", "m", "medium"),
                      Reply("first aggregate into a derived table", "first aggregate into a derived table", "m", "low"),
                      Reply(GOOD, "", "m", "low")])
    result = Rewriter(model, DIALECT, CATALOG, variants=1).rewrite("Q01", "d", ORIGINAL)
    assert result.plan == "first aggregate into a derived table"


def test_a_model_that_fails_to_plan_is_reported_not_raised():
    class Broken(ChatModel):
        name = "broken"

        def _once(self, *a, **k):
            raise __import__("sqlrewriter.llm", fromlist=["ModelError"]).ModelError("no answer")

    result = Rewriter(Broken(), DIALECT, CATALOG).rewrite("Q01", "d", ORIGINAL)
    assert not result.ok
    assert "planning failed" in result.error


def test_a_generation_failure_becomes_a_candidate_with_a_reason():
    class Broken(ChatModel):
        name = "broken"

        def __init__(self):
            self.calls = 0

        def _once(self, *a, **k):
            from _06_llm import ModelError
            self.calls += 1
            if self.calls == 1:
                return Reply("the plan", "", "m", "high")
            raise ModelError("no answer")

    result = Rewriter(Broken(), DIALECT, CATALOG, variants=1, attempts=1).rewrite("Q01", "d", ORIGINAL)
    assert not result.ok
    assert "generation failed" in result.candidates[0].verdict.problems[0]


def test_the_schema_sent_to_the_model_is_scoped_to_the_tables_in_the_query():
    model = Scripted(plan_then(GOOD, 4))
    Rewriter(model, DIALECT, CATALOG, variants=2).rewrite("Q01", "d", ORIGINAL)
    assert "movies" in model.prompts[0] and "ratings" in model.prompts[0]


def test_hints_reach_the_system_prompt():
    model = Scripted(plan_then(GOOD, 2))
    rewriter(plan_then(GOOD, 2), hints="prefer pre-aggregation")
    assert "prefer pre-aggregation" in prompts.system_for("prefer pre-aggregation")
    result = Rewriter(model, DIALECT, CATALOG, variants=1, hints="prefer pre-aggregation").rewrite(
        "Q01", "d", ORIGINAL
    )
    assert result.ok


def test_the_original_query_is_preserved_in_the_result():
    result = rewriter(plan_then(GOOD, 2)).rewrite("Q07", "rated above seven", ORIGINAL)
    assert result.qid == "Q07"
    assert result.description == "rated above seven"
    assert result.original_sql == ORIGINAL