"""Describe the assistant's actual configured provider without exposing secrets."""

import hashlib
import json
import os
from urllib.parse import urlsplit

from tempo.providers import VertexExpressProvider, provider_name_for_model


def describe_connection(provider) -> dict:
    model = provider.model if provider is not None else os.environ.get("MODEL_NAME", "gemini-3.8-flash")
    family = provider_name_for_model(model)
    base = getattr(provider, "api_base", None)
    vertex = getattr(provider, "use_vertexai", False)
    location = getattr(provider, "vertex_location", "global")
    if isinstance(provider, VertexExpressProvider) or vertex:
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        label = "Google Vertex AI"
    elif base:
        host = urlsplit(base).netloc.rsplit("@", 1)[-1]
        label = "Configured model endpoint"
    elif family == "gemini":
        host, label = "generativelanguage.googleapis.com", "Google Gemini"
    elif family == "anthropic":
        host, label = "api.anthropic.com", "Anthropic"
    elif model.lower().startswith(("gpt-", "o1", "o3", "o4")):
        host, label = "api.openai.com", "OpenAI"
    else:
        host, label = "provider-managed", family
    identity = [model, host, base, vertex, getattr(provider, "vertex_project", None),
                getattr(provider, "api_key", None), os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") if vertex else None]
    return {"id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(), "model": model,
            "destination": host, "label": label, "configured": provider is not None}
