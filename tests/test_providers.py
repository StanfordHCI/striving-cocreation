"""Provider classification and environment resolution tests."""

from types import SimpleNamespace

import pytest

from tempo.providers import (
    _resolve_litellm_model,
    VertexExpressProvider,
    create_provider,
    provider_name_for_model,
    resolve_provider_api_base,
    resolve_provider_api_key,
)


def test_provider_families_are_classified_from_model_names():
    assert provider_name_for_model("gemini-3.8-flash") == "gemini"
    assert provider_name_for_model("claude-sonnet-4-5") == "anthropic"
    assert provider_name_for_model("anthropic/claude-sonnet-4-5") == "anthropic"
    assert provider_name_for_model("gpt-4o-mini") == "openai-compatible"
    assert provider_name_for_model("Qwen/Qwen2-VL-7B-Instruct") == "openai-compatible"


def test_claude_passes_through_to_litellm_for_anthropic_routing():
    assert _resolve_litellm_model("claude-sonnet-4-5") == "claude-sonnet-4-5"


def test_provider_specific_keys_do_not_leak_across_providers():
    env = {
        "GOOGLE_API_KEY": "google-key",
        "OPENAI_API_KEY": "openai-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
    }

    assert resolve_provider_api_key("gemini-3.8-flash", environ=env) == "google-key"
    assert resolve_provider_api_key("gpt-4o-mini", environ=env) == "openai-key"
    assert resolve_provider_api_key("claude-sonnet-4-5", environ=env) == "anthropic-key"


def test_tempo_key_and_explicit_key_override_provider_specific_key():
    env = {
        "TEMPO_LM_API_KEY": "tempo-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
    }

    assert resolve_provider_api_key("claude-sonnet-4-5", environ=env) == "tempo-key"
    assert (
        resolve_provider_api_key("claude-sonnet-4-5", "explicit-key", environ=env)
        == "explicit-key"
    )


def test_anthropic_base_url_is_selected_only_for_claude():
    env = {
        "ANTHROPIC_API_BASE": "https://anthropic.example",
        "OPENAI_API_BASE": "https://openai.example/v1",
    }

    assert (
        resolve_provider_api_base("claude-sonnet-4-5", environ=env)
        == "https://anthropic.example"
    )
    assert (
        resolve_provider_api_base("gpt-4o-mini", environ=env)
        == "https://openai.example/v1"
    )
    assert resolve_provider_api_base("gemini-3.8-flash", environ=env) is None


@pytest.mark.asyncio
async def test_vertex_express_uses_google_api_key_client(monkeypatch):
    calls = []

    class FakeModels:
        async def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='{"ok": true}')

    class FakeAsyncClient:
        def __init__(self):
            self.models = FakeModels()
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeClient:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.aio = FakeAsyncClient()

    fake_client = FakeClient(vertexai=True, api_key="placeholder")
    monkeypatch.setattr(
        "google.genai.Client",
        lambda **kwargs: (setattr(fake_client, "init_kwargs", kwargs) or fake_client),
    )

    provider = create_provider(
        model="gemini-3.8-flash",
        api_key="vertex-express-secret",
        gemini_vertexai_express=True,
    )
    assert isinstance(provider, VertexExpressProvider)

    result = await provider.chat_completion(
        messages=[{"role": "user", "content": "Return JSON"}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    assert result == '{"ok": true}'
    assert fake_client.init_kwargs == {
        "vertexai": True,
        "api_key": "vertex-express-secret",
    }
    assert calls[0]["model"] == "gemini-3.8-flash"
    assert calls[0]["contents"][0].parts[0].text == "Return JSON"
    assert calls[0]["config"].response_mime_type == "application/json"
    assert calls[0]["config"].temperature == 0.2

    await provider.close()
    assert fake_client.aio.closed is True


@pytest.mark.asyncio
async def test_vertex_express_converts_screen_data_urls(monkeypatch):
    calls = []

    class FakeModels:
        async def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text="screen summary")

    fake_aio = SimpleNamespace(models=FakeModels(), aclose=lambda: None)
    fake_client = SimpleNamespace(aio=fake_aio)
    monkeypatch.setattr("google.genai.Client", lambda **_kwargs: fake_client)

    provider = VertexExpressProvider("gemini-3.8-flash", "key")
    result = await provider.vision_completion(
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/"}},
                {"type": "text", "text": "Describe this screen"},
            ],
        }],
    )

    assert result == "screen summary"
    parts = calls[0]["contents"][0].parts
    assert parts[0].inline_data.mime_type == "image/jpeg"
    assert parts[0].inline_data.data == b"\xff\xd8\xff"
    assert parts[1].text == "Describe this screen"


def test_vertex_express_rejects_missing_key_and_conflicting_vertex_modes():
    with pytest.raises(ValueError, match="VERTEX_EXPRESS_API_KEY"):
        create_provider(
            model="gemini-3.8-flash",
            gemini_vertexai_express=True,
        )
    with pytest.raises(ValueError, match="either standard Vertex AI or Vertex AI Express"):
        create_provider(
            model="gemini-3.8-flash",
            api_key="key",
            gemini_vertexai=True,
            gemini_vertexai_express=True,
        )


# ── Request timeout ──────────────────────────────────────────────────────────


def test_requests_carry_a_timeout_far_below_litellms_default():
    """A stalled call must fail fast enough for the retry to matter.

    LiteLLM defaults to 600s. These calls normally return in seconds, so
    without an explicit timeout one stalled connection blocks a pipeline stage
    for ten minutes before the caller — which already treats timeouts as
    transient and retries — ever gets to run.
    """
    from tempo.providers import DEFAULT_REQUEST_TIMEOUT_SECONDS, create_provider

    provider = create_provider("gemini-3.8-flash", api_key="k")
    assert provider._call_kwargs()["timeout"] == DEFAULT_REQUEST_TIMEOUT_SECONDS
    assert DEFAULT_REQUEST_TIMEOUT_SECONDS < 600


def test_request_timeout_is_configurable(monkeypatch):
    from tempo.providers import create_provider

    monkeypatch.setenv("TEMPO_LM_TIMEOUT", "45")
    assert create_provider("gemini-3.8-flash", api_key="k").timeout == 45.0

    # An explicit argument wins over the environment.
    assert create_provider("gemini-3.8-flash", api_key="k", timeout=12).timeout == 12

    # Nonsense values fall back to the default rather than disabling timeouts.
    monkeypatch.setenv("TEMPO_LM_TIMEOUT", "not-a-number")
    assert create_provider("gemini-3.8-flash", api_key="k").timeout == 120.0


def test_litellm_timeouts_are_retried_not_fatal():
    """The pipeline must classify a timeout as transient, or it gives up."""
    import litellm
    from tempo.providers import _is_transient_service_error

    exc = litellm.Timeout(
        "Connection timed out. Timeout passed=120.0",
        model="gemini-3.8-flash",
        llm_provider="vertex_ai",
    )
    assert _is_transient_service_error(exc) is True
