from tempo.cli import _endpoint_scope, _redact_endpoint, build_doctor_report


def test_endpoint_scope_recognizes_loopback_hosts():
    assert _endpoint_scope("http://localhost:8000/v1") == "local"
    assert _endpoint_scope("http://127.0.0.1:8000/v1") == "local"
    assert _endpoint_scope("http://[::1]:8000/v1") == "local"
    assert _endpoint_scope("https://api.openai.com/v1") == "external"


def test_doctor_redacts_credentials_and_query_values():
    report = build_doctor_report({
        "MODEL_NAME": "gpt-4o-mini",
        "TEMPO_LM_API_BASE": "https://user:password@example.test/secret-in-path?token=secret",
        "TEMPO_LM_API_KEY": "secret-in-path",
        "ANTHROPIC_API_KEY": "another-secret-key",
        "ANTHROPIC_BASE_URL": "http://localhost:9000/v1",
    })

    assert "https://example.test/[hidden]?redacted" in report
    assert "secret-in-path" not in report
    assert "another-secret-key" not in report
    assert "password" not in report
    assert "token=secret" not in report
    assert "[external] Main model" in report
    assert "[local] Widget chat" in report


def test_doctor_reports_default_gemini_destination():
    report = build_doctor_report({"MODEL_NAME": "gemini-3.8-flash"})

    assert "https://generativelanguage.googleapis.com" in report
    assert "Telemetry:\n  Disabled." in report


def test_doctor_reports_anthropic_as_the_main_model_destination():
    report = build_doctor_report({
        "MODEL_NAME": "claude-sonnet-4-5",
        "ANTHROPIC_API_KEY": "anthropic-secret",
    })

    assert "[external] Main model (claude-sonnet-4-5): https://api.anthropic.com/v1" in report
    assert "source: Anthropic API endpoint" in report
    assert "anthropic-secret" not in report


def test_doctor_reports_vertex_express_without_revealing_key():
    report = build_doctor_report({
        "MODEL_NAME": "gemini-3.8-flash",
        "GEMINI_VERTEXAI_EXPRESS": "true",
        "VERTEX_EXPRESS_API_KEY": "vertex-express-secret",
    })

    assert "https://aiplatform.googleapis.com" in report
    assert "Vertex AI Express Mode API-key endpoint" in report
    assert "vertex-express-secret" not in report
