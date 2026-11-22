"""Exercises the real AnthropicProvider + SDK against a mock HTTP transport (no network, no spend)."""
import json

import anthropic
import httpx2 as httpx
import pytest

from wwos.llm import AnthropicProvider, PermanentLLMError, TransientLLMError


def provider(handler):
    client = anthropic.Anthropic(api_key="test-key", max_retries=0,
                                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return AnthropicProvider(client=client)


def message(content, stop_reason="end_turn", model="claude-opus-5-5"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 45}}


def test_request_shape_and_tool_use_parsing():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx.Response(200, json=message(
            [{"type": "tool_use", "id": "toolu_1", "name": "crm__find_contact", "input": {"email": "a@b.com"}}],
            stop_reason="tool_use"))

    tools = [{"name": "crm__find_contact", "description": "d",
              "input_schema": {"type": "object", "properties": {"email": {"type": "string"}}}}]
    r = provider(handler).complete(model="claude-opus-5-5", system="sys",
                                   messages=[{"role": "user", "content": "hi"}], tools=tools, max_tokens=1000)
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5" and body["system"] == "sys" and body["max_tokens"] == 1000
    assert body["output_config"] == {"effort": "medium"}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["tools"][0]["name"] == "crm__find_contact"
    assert "thinking" not in body and "temperature" not in body
    assert r.stop_reason == "tool_use" and r.tool_calls[0]["input"] == {"email": "a@b.com"}
    assert (r.input_tokens, r.output_tokens, r.model) == (120, 45, "claude-opus-5-5")


def test_text_response():
    r = provider(lambda req: httpx.Response(200, json=message([{"type": "text", "text": "Hello"}]))).complete(
        model="claude-haiku-5-5", system="s", messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=10)
    assert r.text == "Hello" and r.tool_calls == []


@pytest.mark.parametrize("status,exc", [(429, TransientLLMError), (529, TransientLLMError),
                                        (500, TransientLLMError), (400, PermanentLLMError),
                                        (401, PermanentLLMError)])
def test_error_classification(status, exc):
    err = {"type": "error", "error": {"type": "x", "message": "boom"}}
    p = provider(lambda req: httpx.Response(status, json=err))
    with pytest.raises(exc):
        p.complete(model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "x"}], tools=[],
                   max_tokens=10)


def test_refusal_is_permanent():
    p = provider(lambda req: httpx.Response(200, json=message([], stop_reason="refusal")))
    with pytest.raises(PermanentLLMError, match="declined"):
        p.complete(model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "x"}], tools=[],
                   max_tokens=10)
