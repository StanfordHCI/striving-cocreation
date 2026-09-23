"""LLM provider abstraction using LiteLLM + Instructor."""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


# ── Error classification (imported by observers/screen.py, utils.py) ─────────


def _is_transient_service_error(exc: Exception) -> bool:
    """Detect whether an exception is transient (rate-limit, server error, network).

    Transient errors are expected to resolve on their own (rate-limit cooldown,
    server recovery, network reconnection, quota reset).
    """
    # Python built-in transient types
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    # OpenAI SDK transient types (also raised by LiteLLM)
    try:
        from openai import (
            RateLimitError,
            InternalServerError,
            APIConnectionError,
            APITimeoutError,
        )
        if isinstance(exc, (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError)):
            return True
    except ImportError:
        pass

    # Gemini SDK transient types
    try:
        from google.genai.errors import ClientError, ServerError
        if isinstance(exc, ServerError):
            return True
        if isinstance(exc, ClientError):
            if getattr(exc, "code", None) in (429, 500, 503):
                return True
            status = getattr(exc, "status", "")
            if status in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL"):
                return True
    except ImportError:
        pass

    # String fallback for wrapped / unknown exception types
    msg = str(exc).lower()
    transient_markers = (
        "429", "rate limit", "resource exhausted",
        "quota", "billing",
        "server error", "service unavailable", "503", "500",
        "connection", "timeout", "network",
    )
    return any(marker in msg for marker in transient_markers)


def _is_rate_limited(exc: Exception) -> bool:
    """Backwards-compatible alias for _is_transient_service_error."""
    return _is_transient_service_error(exc)


# ── LiteLLM model name resolution ───────────────────────────────────────────


def _resolve_litellm_model(model: str, use_vertexai: bool = False) -> str:
    """Convert a model name to LiteLLM's model format.

    Examples:
        gemini-3.8-flash + vertexai=True  -> vertex_ai/gemini-3.8-flash
        gemini-3.8-flash + vertexai=False -> gemini/gemini-3.8-flash
        gpt-4o-mini                       -> gpt-4o-mini (unchanged)
    """
    model_lower = model.lower()
    if "gemini" in model_lower:
        if use_vertexai:
            return f"vertex_ai/{model}"
        return f"gemini/{model}"
    return model  # OpenAI models pass through unchanged


#: Long enough for a slow vision call on a large batch, short enough that a
#: stalled connection is retried in the same minute rather than the same hour.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0


def _default_request_timeout() -> float:
    """Per-request timeout, overridable with ``TEMPO_LM_TIMEOUT`` seconds."""
    raw = os.environ.get("TEMPO_LM_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_REQUEST_TIMEOUT_SECONDS


def provider_name_for_model(model: str) -> str:
    """Return Tempo's user-facing provider family for a model name."""
    model_lower = model.strip().lower()
    if "gemini" in model_lower:
        return "gemini"
    if model_lower.startswith(("claude", "anthropic/")):
        return "anthropic"
    return "openai-compatible"


def resolve_provider_api_key(
    model: str,
    explicit: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Resolve a provider key without borrowing a key from another provider."""
    if explicit and explicit.strip():
        return explicit.strip()

    values = os.environ if environ is None else environ
    universal_key = values.get("TEMPO_LM_API_KEY")
    if universal_key:
        return universal_key

    provider = provider_name_for_model(model)
    if provider == "gemini":
        return values.get("GOOGLE_API_KEY")
    if provider == "anthropic":
        return values.get("ANTHROPIC_API_KEY")
    return values.get("OPENAI_API_KEY")


def resolve_provider_api_base(
    model: str,
    explicit: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Resolve a custom API base for the selected provider family."""
    if provider_name_for_model(model) == "gemini":
        return None
    if explicit and explicit.strip():
        return explicit.strip()

    values = os.environ if environ is None else environ
    universal_base = values.get("TEMPO_LM_API_BASE")
    if universal_base:
        return universal_base
    if provider_name_for_model(model) == "anthropic":
        return values.get("ANTHROPIC_API_BASE") or values.get("ANTHROPIC_BASE_URL")
    return values.get("OPENAI_API_BASE")


# ── Provider ─────────────────────────────────────────────────────────────────


class ModelProvider:
    """LLM provider using LiteLLM for multi-provider support."""

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        use_vertexai: bool = False,
        vertex_project: Optional[str] = None,
        vertex_location: Optional[str] = None,
        debug: bool = False,
        timeout: Optional[float] = None,
    ):
        self.model = model
        self.litellm_model = _resolve_litellm_model(model, use_vertexai)
        self.api_key = api_key
        self.api_base = api_base
        self.use_vertexai = use_vertexai
        self.vertex_project = vertex_project
        self.vertex_location = vertex_location or "global"
        self.debug = debug
        # LiteLLM defaults to a 600s timeout. These calls normally return in a
        # few seconds, so a stalled connection would otherwise block a pipeline
        # stage for ten minutes before the retry — which the caller already
        # classifies as transient — gets a chance to run. Fail fast instead.
        self.timeout = timeout if timeout is not None else _default_request_timeout()

    def _call_kwargs(self) -> dict:
        """Build kwargs for litellm.acompletion."""
        kwargs: Dict[str, Any] = {"model": self.litellm_model}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.timeout:
            kwargs["timeout"] = self.timeout
        if self.use_vertexai:
            if self.vertex_project:
                kwargs["vertex_project"] = self.vertex_project
            if self.vertex_location:
                kwargs["vertex_location"] = self.vertex_location
        return kwargs

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        **kwargs,
    ) -> str:
        """Generate a chat completion. Returns raw string response."""
        import litellm

        call_kwargs = self._call_kwargs()
        call_kwargs["messages"] = messages
        if temperature is not None:
            call_kwargs["temperature"] = temperature
        if response_format:
            call_kwargs["response_format"] = response_format
        call_kwargs.update(kwargs)

        response = await litellm.acompletion(**call_kwargs)
        return response.choices[0].message.content

    async def vision_completion(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        **kwargs,
    ) -> str:
        """Generate a vision completion. Same as chat_completion for LiteLLM."""
        return await self.chat_completion(
            messages,
            response_format=response_format,
            temperature=temperature,
            **kwargs,
        )

    async def close(self) -> None:
        """No-op — LiteLLM manages its own connections."""
        pass


class VertexExpressProvider(ModelProvider):
    """Vertex AI Express Mode provider using Google's API-key SDK path."""

    def __init__(self, model: str, api_key: str, debug: bool = False):
        if "gemini" not in model.lower():
            raise ValueError("Vertex AI Express Mode currently supports Gemini models only")
        if not api_key.strip():
            raise ValueError("VERTEX_EXPRESS_API_KEY is required for Vertex AI Express Mode")
        super().__init__(model=model, api_key=api_key.strip(), use_vertexai=False, debug=debug)
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(vertexai=True, api_key=self.api_key)
        return self._client

    @staticmethod
    def _part_from_data_url(url: str):
        from google.genai import types

        if not url.startswith("data:") or ";base64," not in url:
            raise ValueError("Vertex Express vision input must be a base64 data URL")
        metadata, encoded = url.split(",", 1)
        mime_type = metadata[5:].split(";", 1)[0]
        return types.Part.from_bytes(
            data=base64.b64decode(encoded, validate=True),
            mime_type=mime_type,
        )

    @classmethod
    def _convert_messages(cls, messages: List[Dict[str, Any]]):
        from google.genai import types

        contents = []
        system_parts = []
        for message in messages:
            role = str(message.get("role", "user"))
            raw_content = message.get("content", "")
            parts = []
            if isinstance(raw_content, str):
                parts.append(types.Part.from_text(text=raw_content))
            elif isinstance(raw_content, list):
                for item in raw_content:
                    if not isinstance(item, dict):
                        parts.append(types.Part.from_text(text=str(item)))
                        continue
                    if item.get("type") == "text":
                        parts.append(types.Part.from_text(text=str(item.get("text", ""))))
                    elif item.get("type") == "image_url":
                        image = item.get("image_url") or {}
                        url = image.get("url") if isinstance(image, dict) else image
                        parts.append(cls._part_from_data_url(str(url)))
            else:
                parts.append(types.Part.from_text(text=str(raw_content)))

            if role == "system":
                system_parts.extend(parts)
            else:
                contents.append(
                    types.Content(role="model" if role == "assistant" else "user", parts=parts)
                )
        return contents, system_parts

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        **kwargs,
    ) -> str:
        from google.genai import types

        contents, system_parts = self._convert_messages(messages)
        config: Dict[str, Any] = {}
        if system_parts:
            config["system_instruction"] = types.Content(parts=system_parts)
        if temperature is not None:
            config["temperature"] = temperature
        if response_format and response_format.get("type") in {"json_object", "json_schema"}:
            config["response_mime_type"] = "application/json"
            schema = response_format.get("json_schema")
            if isinstance(schema, dict):
                config["response_json_schema"] = schema.get("schema", schema)

        if "max_tokens" in kwargs:
            config["max_output_tokens"] = kwargs.pop("max_tokens")
        for name in ("max_output_tokens", "top_p", "top_k"):
            if name in kwargs:
                config[name] = kwargs.pop(name)
        if "stop" in kwargs:
            config["stop_sequences"] = kwargs.pop("stop")
        if kwargs:
            logger.debug("Ignoring unsupported Vertex Express options: %s", sorted(kwargs))

        response = await self._get_client().aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(**config),
        )
        return response.text or ""

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None


# ── Factory ──────────────────────────────────────────────────────────────────


def create_provider(
    model: str,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    gemini_vertexai: bool = False,
    gemini_vertexai_express: bool = False,
    vertex_project: Optional[str] = None,
    vertex_location: Optional[str] = None,
    debug: bool = False,
    timeout: Optional[float] = None,
) -> ModelProvider:
    """Create a ModelProvider instance.

    Args:
        model: The model name (e.g., 'gemini-3.8-flash', 'gpt-4o-mini').
        api_key: API key for authentication.
        api_base: Base URL for the API.
        gemini_vertexai: Use Vertex AI instead of consumer Gemini API.
        vertex_project: GCP project ID (for Vertex AI).
        vertex_location: Vertex AI region (default: "global").
        debug: Enable debug logging.

    Returns:
        ModelProvider: A configured provider instance.
    """
    if gemini_vertexai and gemini_vertexai_express:
        raise ValueError("Choose either standard Vertex AI or Vertex AI Express Mode, not both")
    if gemini_vertexai_express:
        return VertexExpressProvider(model=model, api_key=api_key or "", debug=debug)

    return ModelProvider(
        model=model,
        api_key=api_key,
        api_base=api_base,
        use_vertexai=gemini_vertexai,
        vertex_project=vertex_project,
        vertex_location=vertex_location,
        debug=debug,
        timeout=timeout,
    )
