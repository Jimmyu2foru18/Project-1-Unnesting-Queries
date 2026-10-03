"""Chat backends behind one interface.

Every backend returns the same pair: the answer, and the reasoning that produced
it. Reasoning models keep the two apart and so does this client, because the
pipeline wants the deliberation on one turn and nothing but SQL on the next.

One behaviour is worth knowing about. On a reasoning model the thinking budget
and the answer budget come out of the same allowance, so a long deliberation can
consume the whole thing and leave the final message empty. Rather than handing
an empty string to the verifier, a response with no answer is retried at a
cheaper reasoning effort.
"""
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

EFFORTS = ("low", "medium", "high")
DEFAULT_EFFORT = os.getenv("SQLRW_EFFORT", "medium")

FENCE = re.compile(r"^\s*```(?:sql|postgresql|mysql|duckdb)?\s*|\s*```\s*$", re.IGNORECASE)


class ModelError(RuntimeError):
    """The model never produced an answer, or could not be reached."""


@dataclass(frozen=True)
class Reply:
    """One model turn."""

    sql: str = ""
    reasoning: str = ""
    model: str = ""
    effort: str = ""
    tokens: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    def __bool__(self) -> bool:
        return bool(self.sql.strip())


def strip_fences(text: str) -> str:
    return FENCE.sub("", text or "").strip()


def extract_sql(content: str) -> str:
    """Pull a statement out of a final message that may still be chatty."""
    text = strip_fences(content)
    if not text:
        return ""
    blocks = re.findall(r"(?is)(?:^|\n)\s*((?:with|select)\b.*?;)", text)
    if blocks:
        return max(blocks, key=len).strip()
    return text if re.match(r"(?is)^\s*(?:with|select)\b", text) else ""


def ladder(effort: str) -> tuple[str, ...]:
    """Efforts to try, starting at the requested one and getting cheaper."""
    start = EFFORTS.index(effort) if effort in EFFORTS else EFFORTS.index(DEFAULT_EFFORT)
    return EFFORTS[: start + 1][::-1]


class ChatModel:
    """A provider that can be asked for SQL."""

    name = ""

    def chat(self, system: str, user: str, *, effort: str = DEFAULT_EFFORT,
             temperature: float = 0.0, budget: int = 4096) -> Reply:
        """Return the final answer, downshifting effort while the answer is empty."""
        for level in ladder(effort):
            reply = self._once(system, user, level, temperature, budget)
            if reply.sql:
                return reply
        raise ModelError(f"{self.name} returned no answer in the final channel at any effort")

    def _once(self, system, user, effort, temperature, budget) -> Reply:
        raise NotImplementedError


class OllamaModel(ChatModel):
    """A local Ollama model. gpt-oss is the default target."""

    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None,
                 budget: int = 4096, seed: int = 7, timeout: int = 1800, retries: int = 3) -> None:
        self.model = model or os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
        self.host = (host or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.budget = budget
        self.seed = seed
        self.timeout = timeout
        self.retries = retries
        self.name = f"ollama/{self.model}"

    def _once(self, system, user, effort, temperature, budget) -> Reply:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": effort,
            "options": {"temperature": temperature, "seed": self.seed,
                        "num_predict": budget, "num_ctx": 32768},
        }
        data = self._post(payload)
        message = data.get("message") or {}
        return Reply(
            extract_sql(message.get("content") or ""),
            message.get("thinking") or "",
            self.model, effort, int(data.get("eval_count") or 0), data,
        )

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        last: Exception | None = None
        import time

        for attempt in range(self.retries):
            request = urllib.request.Request(
                f"{self.host}/api/chat", data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as err:
                detail = err.read().decode("utf-8", "replace")[:200]
                if err.code < 500:
                    raise ModelError(f"ollama rejected the request ({err.code}): {detail}") from err
                last = RuntimeError(f"ollama {err.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as err:
                last = err
            time.sleep(2 ** attempt)
        raise ModelError(f"could not reach ollama at {self.host}: {last}")


class OpenAIModel(ChatModel):
    """OpenAI chat completions, with reasoning effort when the model has it."""

    name = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None, budget: int = 4096, seed: int = 7) -> None:
        from openai import OpenAI

        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.budget = budget
        self.seed = seed
        self.name = f"openai/{self.model}"
        options: dict[str, Any] = {"api_key": self.api_key or "missing"}
        if base_url or os.getenv("OPENAI_BASE_URL"):
            options["base_url"] = base_url or os.environ["OPENAI_BASE_URL"]
        self.client = OpenAI(**options)

    def _once(self, system, user, effort, temperature, budget) -> Reply:
        options: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_completion_tokens": budget,
            "seed": self.seed,
        }
        try:
            completion = self.client.chat.completions.create(reasoning_effort=effort, **options)
        except Exception:
            completion = self.client.chat.completions.create(**options)
        message = completion.choices[0].message
        reasoning = getattr(message, "reasoning", None) or getattr(message, "reasoning_content", None) or ""
        usage = getattr(completion, "usage", None)
        return Reply(
            extract_sql(message.content or ""),
            reasoning,
            self.model, effort,
            int(getattr(usage, "completion_tokens", 0) or 0),
            completion.model_dump() if hasattr(completion, "model_dump") else {},
        )


class GeminiModel(ChatModel):
    """Google Gemini via the google-genai SDK."""

    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None, budget: int = 4096) -> None:
        from google import genai

        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
        self.budget = budget
        self.name = f"gemini/{self.model}"
        self.client = genai.Client(api_key=api_key or os.getenv("GEMINI_API_KEY") or None)

    def _once(self, system, user, effort, temperature, budget) -> Reply:
        from google.genai import types

        thinking = None
        if effort in EFFORTS:
            budget_for_thinking = {"low": 2048, "medium": 8192, "high": 16384}[effort]
            thinking = types.ThinkingConfig(thinking_budget=budget_for_thinking, include_thoughts=True)
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max(budget, 8192),
            thinking_config=thinking,
        )
        response = self.client.models.generate_content(model=self.model, contents=user, config=config)
        text, reasoning = "", ""
        for part in getattr(response, "candidates", [None])[0].content.parts or []:
            if getattr(part, "thought", False):
                reasoning += part.text or ""
            else:
                text += part.text or ""
        usage = getattr(response, "usage_metadata", None)
        return Reply(
            extract_sql(text), reasoning, self.model, effort,
            int(getattr(usage, "candidates_token_count", 0) or 0),
        )


class EchoModel(ChatModel):
    """Returns the source query unchanged. Used to exercise the pipeline offline."""

    name = "echo"

    def __init__(self, sql: str = "SELECT 1") -> None:
        self.sql = sql

    def _once(self, system, user, effort, temperature, budget) -> Reply:
        return Reply(self.sql, "echo model", "echo", effort, 0)


def parse_model(spec: str) -> ChatModel:
    """Build a backend from a ``provider/model`` string."""
    if "/" not in spec:
        raise ValueError(f"model must look like provider/model, got {spec!r}")
    provider, model = spec.split("/", 1)
    provider = provider.strip().lower()
    if provider == "ollama":
        return OllamaModel(model=model)
    if provider == "openai":
        return OpenAIModel(model=model)
    if provider in ("gemini", "google"):
        return GeminiModel(model=model)
    if provider == "echo":
        return EchoModel(model)
    raise ValueError(f"unknown provider {provider!r}; try ollama, openai or gemini")


def available_models() -> list[str]:
    """Convenience list for help text."""
    return ["ollama/gpt-oss:20b", "openai/gpt-4.1", "gemini/gemini-2.5-pro"]