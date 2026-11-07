"""Mocked backends for the LLM client tests: no network, no Ollama."""

import json
from collections.abc import Callable
from typing import Any

import httpx

from wikipulse.llm.client import ChatRequest, LLMClient
from wikipulse.llm.ollama_backend import OllamaClient
from wikipulse.llm.types import LLMResponse, Usage


class ScriptedClient(LLMClient):
    """A backend that answers with prepared texts and remembers every request."""

    backend = "fake"

    def __init__(self, answers: list[str], model: str = "m") -> None:
        super().__init__(model)
        self.answers = list(answers)
        self.requests: list[ChatRequest] = []

    def _send(self, request: ChatRequest) -> LLMResponse:
        self.requests.append(request)
        usage = Usage(input_tokens=10, output_tokens=5)
        return LLMResponse(model=self.setting, text=self.answers.pop(0), usage=usage)


def ollama_reply(content: str = "", **extra: Any) -> dict[str, Any]:
    """A non-streaming /api/chat response body, as Ollama sends it."""
    message = {"role": "assistant", "content": content, **extra.pop("message", {})}
    return {
        "model": "qwen3:4b",
        "created_at": "2026-11-07T12:00:00Z",
        "message": message,
        "done": True,
        "done_reason": "stop",
        "total_duration": 2_000_000_000,
        "load_duration": 500_000_000,
        "prompt_eval_count": 42,
        "eval_count": 7,
        **extra,
    }


def mock_ollama(
    replies: list[dict[str, Any]] | Callable[[dict[str, Any]], httpx.Response],
    model: str = "qwen3:4b",
) -> tuple[OllamaClient, list[dict[str, Any]]]:
    """An OllamaClient whose HTTP requests go to a mock; returns it and the request bodies."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append(body)
        if callable(replies):
            return replies(body)
        return httpx.Response(200, json=replies.pop(0))

    client = OllamaClient(model, host="http://ollama", transport=httpx.MockTransport(handler))
    return client, seen
