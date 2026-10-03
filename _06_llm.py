"""Chat backends behind one interface."""
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from _01_core import ModelError, Reply

EFFORTS = ("low", "medium", "high")
DEFAULT_EFFORT = os.getenv("SQLRW_EFFORT", "medium")

FENCE = re.compile(r"^\s*```(?:sql|postgresql|mysql|duckdb)?\s*|\s*```\s*$", re.IGNORECASE)


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
    """A local Ollama model."""

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


class EchoModel(ChatModel):
    """Returns the source query unchanged."""

    name = "echo"

    def __init__(self, sql: str = "SELECT 1") -> None:
        self.sql = sql

    def _once(self, system, user, effort, temperature, budget) -> Reply:
        return Reply(self.sql, "echo model", "echo", effort, 0)


def parse_model(spec: str) -> ChatModel:
    """Build a backend from a provider/model string."""
    if "/" not in spec:
        raise ValueError(f"model must look like provider/model, got {spec!r}")
    provider, model = spec.split("/", 1)
    provider = provider.strip().lower()
    if provider == "ollama":
        return OllamaModel(model=model)
    if provider == "echo":
        return EchoModel(model)
    raise ValueError(f"unknown provider {provider!r}; try ollama or echo")


def available_models() -> list[str]:
    """Convenience list for help text."""
    return ["ollama/gpt-oss:20b", "echo/SELECT 1"]


__all__ = [
    "ChatModel",
    "EchoModel",
    "ModelError",
    "OllamaModel",
    "Reply",
    "available_models",
    "extract_sql",
    "parse_model",
]