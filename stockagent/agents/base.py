"""OpenRouter client + structured-output helpers.

Why OpenRouter: routes one API to many providers (Gemini Flash, DeepSeek, GLM,
MiniMax). We pick per-agent in stockagent.config.

If `OPENROUTER_API_KEY` is unset, `LLMUnavailableError` is raised on first call.
The coordinator handles that by falling back to deterministic conviction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from loguru import logger
from openai import OpenAI

from stockagent.config import settings


class LLMUnavailableError(RuntimeError):
    """Raised when no API key is configured, so callers can fallback gracefully."""


_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is not None:
        return _client
    if not settings.openrouter_api_key:
        raise LLMUnavailableError("OPENROUTER_API_KEY not set in .env")
    # OpenRouter convention headers — improve rate-limit pooling & give them attribution.
    _client = OpenAI(
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        default_headers={
            "HTTP-Referer": "https://github.com/local/stockagent",
            "X-Title": "stockagent",
        },
    )
    return _client


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: dict | None = None


def call_llm(
    *,
    model: str,
    system: str,
    user: str,
    response_format: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1000,
    image_data_url: str | None = None,
) -> LLMResponse:
    """Single-turn chat completion.

    `response_format='json_object'` only works with text-only models that support it.
    Multimodal models often don't — leave it None and rely on prompt-instructed JSON.

    When `image_data_url` is provided we fold the system prompt INTO the user message,
    because some multimodal providers (kimi-k2.5 via Chutes) silently return empty when
    they receive role=system + multimodal user content. Single-message multimodal works
    everywhere we tested.
    """
    client = get_client()
    messages: list[dict[str, Any]]
    if image_data_url:
        combined_text = f"{system}\n\n---\n\n{user}"
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": combined_text},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        }]
    else:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format == "json_object":
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    if not resp.choices:
        raise RuntimeError(f"model {model!r} returned no choices (gated, out of quota, or provider error)")
    content = resp.choices[0].message.content or ""
    usage = resp.usage.model_dump() if resp.usage else None
    return LLMResponse(content=content, model=model, usage=usage)


def parse_json_safely(content: str) -> dict:
    """Best-effort JSON parse — strips code fences if the model added them."""
    s = content.strip()
    if s.startswith("```"):
        # ```json ... ``` or ``` ... ```
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3].rstrip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        logger.warning(f"LLM returned non-JSON, raw: {content[:200]}")
        return {}
