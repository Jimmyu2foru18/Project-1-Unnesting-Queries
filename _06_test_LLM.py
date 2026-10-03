"""The model layer, exercised without touching a provider."""
import pytest

import _06_llm as llm


def test_a_plain_statement_survives():
    assert llm.extract_sql("SELECT 1 FROM t") == "SELECT 1 FROM t"


def test_a_fenced_statement_is_unwrapped():
    assert llm.extract_sql("```sql\nSELECT 1 FROM t\n```") == "SELECT 1 FROM t"


def test_a_bare_fence_without_a_language_tag_is_unwrapped():
    assert llm.extract_sql("```\nSELECT 1 FROM t\n```") == "SELECT 1 FROM t"


def test_a_dialect_tagged_fence_is_unwrapped():
    assert llm.extract_sql("```postgresql\nSELECT 1 FROM t\n```") == "SELECT 1 FROM t"


def test_a_chatty_answer_yields_just_the_statement():
    answer = "Sure! Here is the rewrite:\n\n```sql\nSELECT a FROM t1 WHERE b = 1;\n```\nLet me know."
    assert llm.extract_sql(answer) == "SELECT a FROM t1 WHERE b = 1;"


def test_the_longest_statement_wins_when_several_are_offered():
    answer = "First:\nSELECT 1 FROM a;\nActually, better:\nSELECT a, b FROM t1 JOIN t2 ON t1.id = t2.id;"
    assert "t2" in llm.extract_sql(answer)


def test_prose_with_no_statement_yields_nothing():
    assert llm.extract_sql("I am not able to answer that.") == ""
    assert llm.extract_sql("") == ""
    assert llm.extract_sql("   ") == ""


def test_a_leading_statement_is_found_after_a_comment():
    assert llm.extract_sql("Some notes\n\nSELECT 1 FROM t;") == "SELECT 1 FROM t;"


def test_strip_fences_leaves_ordinary_text_alone():
    assert llm.strip_fences("SELECT 1") == "SELECT 1"


def test_a_reply_is_false_when_it_carries_no_statement():
    assert llm.Reply(sql="").sql == ""
    assert llm.Reply(sql="   ").sql == "   "
    assert llm.Reply(sql="SELECT 1").sql == "SELECT 1"


def test_the_effort_ladder_starts_where_asked_and_gets_cheaper():
    assert llm.ladder("high") == ("high", "medium", "low")
    assert llm.ladder("medium") == ("medium", "low")
    assert llm.ladder("low") == ("low",)


def test_an_unknown_effort_falls_back_to_the_default():
    assert llm.ladder("nonsense") == llm.ladder(llm.DEFAULT_EFFORT)


class Scripted(llm.ChatModel):
    """Returns whatever it is told to, one reply per effort level."""

    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def _once(self, system, user, effort, temperature, budget):
        self.seen.append(effort)
        return self.replies.pop(0) if self.replies else llm.Reply("", "", "scripted", effort)


def test_an_answer_on_the_first_try_costs_one_call():
    model = Scripted([llm.Reply("SELECT 1", "", "m", "high")])
    assert model.chat("s", "u", effort="high").sql == "SELECT 1"
    assert model.seen == ["high"]


def test_an_empty_answer_is_retried_at_a_cheaper_effort():
    model = Scripted([llm.Reply("", "thought a lot", "m", "high"),
                      llm.Reply("SELECT 1", "", "m", "medium")])
    reply = model.chat("s", "u", effort="high")
    assert reply.sql == "SELECT 1"
    assert model.seen == ["high", "medium"]


def test_effort_is_only_dropped_while_the_answer_is_empty():
    model = Scripted([llm.Reply("", "", "m", "high"), llm.Reply("", "", "m", "medium"),
                      llm.Reply("SELECT 1", "", "m", "low")])
    assert model.chat("s", "u", effort="high").sql == "SELECT 1"
    assert model.seen == ["high", "medium", "low"]


def test_a_model_that_never_answers_raises_rather_than_returning_nothing():
    model = Scripted([])
    with pytest.raises(llm.ModelError) as err:
        model.chat("s", "u", effort="high")
    assert "no answer" in str(err.value)


def test_the_reply_records_what_was_spent():
    reply = llm.Reply("SELECT 1", "because", "gpt-oss:20b", "medium", 42)
    assert reply.reasoning == "because"
    assert reply.tokens == 42
    assert reply.model == "gpt-oss:20b"
    assert reply.effort == "medium"


def test_the_echo_model_returns_the_query_it_was_given():
    model = llm.EchoModel("SELECT 1 FROM t")
    assert model.chat("s", "u").sql == "SELECT 1 FROM t"
    assert model.name == "echo"


def test_parse_model_builds_the_echo_backend():
    assert isinstance(llm.parse_model("echo/SELECT 1"), llm.EchoModel)


def test_parse_model_rejects_a_spec_without_a_provider():
    with pytest.raises(ValueError, match="provider/model"):
        llm.parse_model("gpt-4.1")


def test_parse_model_rejects_an_unknown_provider():
    with pytest.raises(ValueError, match="unknown provider"):
        llm.parse_model("bedrock/anthropic.claude")


def test_the_ollama_request_carries_the_think_field():
    seen = {}

    class Recording(llm.OllamaModel):
        def _post(self, payload):
            seen.update(payload)
            return {"message": {"content": "SELECT 1", "thinking": "hmm"}, "eval_count": 5}

    model = Recording(model="gpt-oss:20b", host="http://localhost:1")
    reply = model.chat("s", "u", effort="high", budget=1234, temperature=0.5)
    assert seen["think"] == "high"
    assert seen["options"]["num_predict"] == 1234
    assert seen["options"]["temperature"] == 0.5
    assert seen["stream"] is False
    assert reply.sql == "SELECT 1"
    assert reply.reasoning == "hmm"
    assert reply.tokens == 5


def test_a_chatty_ollama_reply_still_yields_the_statement():
    class Chatty(llm.OllamaModel):
        def _post(self, payload):
            return {"message": {"content": "```sql\nSELECT 1 FROM t\n```"}}

    assert Chatty(model="m").chat("s", "u").sql == "SELECT 1 FROM t"


def test_an_unreachable_ollama_raises_a_clear_error(monkeypatch):
    model = llm.OllamaModel(model="m", host="http://localhost:1", retries=1, timeout=1)
    with pytest.raises(llm.ModelError, match="could not reach ollama"):
        model.chat("s", "u")


def test_the_default_backend_targets_gpt_oss():
    assert llm.available_models()[0].startswith("ollama/")