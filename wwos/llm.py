"""LLM provider abstraction.

``AnthropicProvider`` calls the Claude API through the official SDK.
``OfflineProvider`` is deterministic and free; it lets the whole system
(including the tool-use loop) run in tests, CI and environments without
credentials. It follows a tiny script convention: every line of the latest
user message of the form ``TOOL: <name> <json-args>`` becomes a tool call on
the first turn; once tool results arrive it returns a plain-text summary.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import get_settings


class AIDisabled(RuntimeError):
    """No language model is configured (WWOS_LLM_PROVIDER=none, the zero-spend mode). Nothing is
    called and nothing is spent; callers fall back to rules or mark the work as skipped."""


class TransientLLMError(RuntimeError):
    """Retryable failure (rate limit, overload, network)."""


class PermanentLLMError(RuntimeError):
    """Non-retryable failure (bad request, auth, refusal)."""


@dataclass
class LLMResult:
    content: list[dict[str, Any]]  # assistant content blocks, replayable as-is
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Prompt-cache usage (billed at 1.25x / 0.1x of the input price); input_tokens excludes these.
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()


class LLMProvider(Protocol):
    name: str

    def complete(self, *, model: str, system: str, messages: list[dict], tools: list[dict],
                 max_tokens: int) -> LLMResult: ...


_TOOL_LINE = re.compile(r"^TOOL:\s*([\w.]+)\s*(\{.*\})?\s*$", re.MULTILINE)


class OfflineProvider:
    name = "offline"

    def complete(self, *, model: str, system: str, messages: list[dict], tools: list[dict],
                 max_tokens: int) -> LLMResult:
        last = messages[-1]
        content = last["content"]
        prompt_chars = len(system) + sum(len(json.dumps(m["content"], default=str)) for m in messages)
        in_tokens = prompt_chars // 4 + 1
        if isinstance(content, str):
            calls = []
            for i, m in enumerate(_TOOL_LINE.finditer(content)):
                args = json.loads(m.group(2)) if m.group(2) else {}
                calls.append({"type": "tool_use", "id": f"toolu_offline_{len(messages)}_{i}",
                              "name": m.group(1), "input": args})
            if calls:
                return LLMResult(content=calls, stop_reason="tool_use", input_tokens=in_tokens,
                                 output_tokens=20 * len(calls), model="offline", tool_calls=calls)
            text = f"[offline] Acknowledged: {content[:200]}"
        else:
            results = [b for b in content if b.get("type") == "tool_result"]
            errors = sum(1 for b in results if b.get("is_error"))
            text = f"[offline] Completed {len(results)} tool call(s); {errors} error(s)."
        return LLMResult(content=[{"type": "text", "text": text}], stop_reason="end_turn",
                         input_tokens=in_tokens, output_tokens=len(text) // 4 + 1, model="offline")


# Thinking effort per model: the economy model does routine work at low effort; everything else medium.
EFFORT_BY_MODEL = {"claude-haiku-5-5": "low"}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client=None) -> None:
        import anthropic  # imported lazily so offline mode needs no credentials

        self._anthropic = anthropic
        self._client = client or anthropic.Anthropic(max_retries=2)

    def complete(self, *, model: str, system: str, messages: list[dict], tools: list[dict],
                 max_tokens: int) -> LLMResult:
        a = self._anthropic
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": EFFORT_BY_MODEL.get(model, "medium")},
            # Automatic prompt caching: each turn of an agent run re-reads the system prompt, tool
            # definitions and earlier turns from cache at 0.1x the input price instead of paying full price.
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            kwargs["tools"] = tools
        # Server-side refusal fallback for current-generation models.
        if model in {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"}:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            create = self._client.beta.messages.create
        else:
            create = self._client.messages.create
        try:
            resp = create(**kwargs)
        except a.RateLimitError as e:
            raise TransientLLMError(f"rate limited: {e}") from e
        except a.APIConnectionError as e:
            raise TransientLLMError(f"connection error: {e}") from e
        except a.APIStatusError as e:
            if e.status_code >= 500 or e.status_code in (408, 409, 429, 529):
                raise TransientLLMError(f"API {e.status_code}: {e.message}") from e
            raise PermanentLLMError(f"API {e.status_code}: {e.message}") from e
        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            raise PermanentLLMError(f"model declined the request (category={category})")
        blocks = [b.to_dict() for b in resp.content]
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        usage = resp.usage
        return LLMResult(content=blocks, stop_reason=resp.stop_reason or "end_turn",
                         input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                         model=resp.model, tool_calls=calls,
                         cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
                         cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0)


_provider_override: LLMProvider | None = None


def set_provider(provider: LLMProvider | None) -> None:
    """Test hook / dependency injection."""
    global _provider_override
    _provider_override = provider


def ai_enabled() -> bool:
    """False in zero-spend mode. A test provider override always counts as enabled."""
    return _provider_override is not None or get_settings().llm_provider != "none"


def get_provider() -> LLMProvider:
    if _provider_override is not None:
        return _provider_override
    provider = get_settings().llm_provider
    if provider == "anthropic":
        return AnthropicProvider()
    if provider == "none":
        raise AIDisabled("AI is off (WWOS_LLM_PROVIDER=none): no model is called and nothing is spent")
    return OfflineProvider()
