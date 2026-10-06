from types import SimpleNamespace as NS

import anthropic
import httpx2 as httpx
import pytest

from agent.diagnose.provider import AnthropicProvider, ProviderError


def resp(text="{}", stop="end_turn"):
    return NS(stop_reason=stop, content=[NS(type="text", text=text)] if text is not None else [])


class Msgs:
    def __init__(self, result):
        self.result, self.kwargs = result, None

    def create(self, **kw):
        self.kwargs = kw
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def client(result):
    m = Msgs(result)
    return NS(messages=m, beta=NS(messages=m)), m


def test_returns_text_and_requests_json_schema():
    c, m = client(resp('{"a":1}'))
    assert AnthropicProvider(client=c, use_fallbacks=False).complete("sys", "p") == '{"a":1}'
    assert m.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert m.kwargs["model"] == "claude-opus-5-5" and "betas" not in m.kwargs


def test_fallbacks_enabled_uses_beta_endpoint():
    c, m = client(resp())
    AnthropicProvider(client=c).complete("s", "p")
    assert m.kwargs["fallbacks"] == "default" and m.kwargs["betas"] == ["server-side-fallback-2026-07-01"]


@pytest.mark.parametrize("r", [resp(stop="refusal"), resp(stop="max_tokens"), resp(text=None)])
def test_bad_stop_states_become_provider_errors(r):
    c, _ = client(r)
    with pytest.raises(ProviderError):
        AnthropicProvider(client=c, use_fallbacks=False).complete("s", "p")


def test_api_errors_wrapped():
    req = httpx.Request("POST", "https://x")
    err = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
    c, _ = client(err)
    with pytest.raises(ProviderError, match="429"):
        AnthropicProvider(client=c, use_fallbacks=False).complete("s", "p")


def test_missing_credentials_typeerror_becomes_provider_error():
    c, _ = client(TypeError("Could not resolve authentication method"))
    with pytest.raises(ProviderError, match="authentication"):
        AnthropicProvider(client=c, use_fallbacks=False).complete("s", "p")
