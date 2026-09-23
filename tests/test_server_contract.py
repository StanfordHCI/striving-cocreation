"""Public server surface and loopback security contract."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import tempo.server as server
from tempo.observers.health import CaptureHealth


RETAINED_ROUTES = {
    ("POST", "/api/start"),
    ("POST", "/api/stop"),
    ("GET", "/api/apps/installed"),
    ("GET", "/api/settings/exclusions"),
    ("PUT", "/api/settings/exclusions"),
    ("GET", "/api/status"),
    ("GET", "/api/config"),
    ("GET", "/api/config/env"),
    ("POST", "/api/graph/entities"),
    ("GET", "/api/graph/entity/{entity_id}"),
    ("GET", "/api/graph/entity/{entity_id}/screenshot"),
    ("GET", "/api/graph/entity/{entity_id}/screenshots"),
    ("POST", "/api/graph/search"),
    ("GET", "/api/graph/timeline"),
    ("GET", "/api/graph/stats"),
    ("GET", "/api/goals"),
    ("PATCH", "/api/entity/{entity_id}"),
    ("POST", "/api/entity/{entity_id}/reparent"),
    ("POST", "/api/entity/merge"),
    ("POST", "/api/entity/{entity_id}/split"),
    ("DELETE", "/api/entity/{entity_id}"),
    ("POST", "/api/entity"),
    ("GET", "/api/hierarchy"),
    ("POST", "/api/hierarchy/compile"),
    ("POST", "/api/hierarchy/compile/{token}/accept"),
    ("POST", "/api/hierarchy/compile/{token}/revert"),
    ("GET", "/api/diagnostics/observer"),
    ("GET", "/api/onboarding"),
    ("PUT", "/api/onboarding"),
    ("GET", "/api/assistant/feed"),
    ("PUT", "/api/assistant/settings"),
    ("POST", "/api/assistant/refresh"),
    ("POST", "/api/assistant/refine"),
    ("POST", "/api/assistant/feedback"),
    ("POST", "/api/assistant/chat"),
    ("GET", "/api/assistant/chats/{session_id}"),
    ("GET", "/api/assistant/chats"),
    ("WEBSOCKET", "/ws"),
    ("GET", "/{spa_path:path}"),
}


def _runtime_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in server.app.routes:
        methods = getattr(route, "methods", None)
        if methods:
            routes.update((method, route.path) for method in methods if method != "HEAD")
        elif route.path == "/ws":
            routes.add(("WEBSOCKET", route.path))
    return routes


def test_server_exposes_only_the_phase7_contract():
    assert _runtime_routes() == RETAINED_ROUTES


def test_rejects_non_loopback_host_and_origin():
    with TestClient(server.app) as client:
        assert client.get("/api/status", headers={"host": "evil.example"}).status_code == 400

        hostile_headers = {
            "host": "127.0.0.1:8000",
            "origin": "https://evil.example",
        }
        assert client.get("/api/status", headers=hostile_headers).status_code == 403
        assert client.put(
            "/api/settings/exclusions",
            headers=hostile_headers,
            json={"apps": [], "domains": []},
        ).status_code == 403


def test_allows_loopback_and_electron_origins():
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        assert client.get(
            "/api/status", headers={"origin": "http://localhost:5173"}
        ).status_code == 200
        assert client.get("/api/status", headers={"origin": "null"}).status_code == 200

        preflight = client.options(
            "/api/settings/exclusions",
            headers={
                "origin": "http://127.0.0.1:5173",
                "access-control-request-method": "PUT",
                "access-control-request-headers": "content-type",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"

        edit_preflight = client.options(
            "/api/entity/1",
            headers={
                "origin": "http://localhost:5173",
                "access-control-request-method": "PATCH",
                "access-control-request-headers": "content-type",
            },
        )
        assert edit_preflight.status_code == 200
        assert edit_preflight.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_websocket_rejects_hostile_browser_origin():
    with TestClient(server.app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws", headers={"origin": "https://evil.example"}
            ):
                pass
    assert exc_info.value.code == 1008


def test_config_never_returns_api_keys(monkeypatch):
    secret = "sk-public-response-must-never-contain-this"
    monkeypatch.setattr(server, "status", {"running": True})
    monkeypatch.setattr(
        server,
        "current_config",
        {
            "model_name": "gpt-test",
            "platform": "macos",
            "api_key": secret,
            "api_key_source": "explicit",
            "api_base": "https://user:provider-secret@provider.example/v1",
            "debug": False,
        },
    )

    with TestClient(server.app) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert secret not in json.dumps(body)
    assert "provider-secret" not in json.dumps(body)
    assert body["api_key_configured"] is True
    assert "api_key" not in body


def test_env_config_reports_presence_without_values(monkeypatch):
    secrets = {
        "OPENAI_API_KEY": "openai-secret-value",
        "GOOGLE_API_KEY": "google-secret-value",
        "TEMPO_LM_API_KEY": "tempo-secret-value",
        "GOOGLE_APPLICATION_CREDENTIALS": "/secret/key.json",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)

    with TestClient(server.app) as client:
        body = client.get("/api/config/env").json()

    serialized = json.dumps(body)
    assert all(value not in serialized for value in secrets.values())
    assert body["api_key_configured"] is True
    assert body["vertex_credentials_configured"] is True


def test_env_config_recognizes_anthropic_provider_and_key(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "claude-sonnet-4-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    monkeypatch.delenv("TEMPO_LM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_VERTEXAI_EXPRESS", raising=False)

    with TestClient(server.app) as client:
        body = client.get("/api/config/env").json()

    assert body["provider"] == "anthropic"
    assert body["api_key_configured"] is True
    assert "anthropic-secret-value" not in json.dumps(body)


def test_env_config_recognizes_vertex_express_provider_and_key(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_VERTEXAI_EXPRESS", "true")
    monkeypatch.setenv("VERTEX_EXPRESS_API_KEY", "vertex-express-secret")

    with TestClient(server.app) as client:
        body = client.get("/api/config/env").json()

    assert body["provider"] == "vertex-express"
    assert body["api_key_configured"] is True
    assert "vertex-express-secret" not in json.dumps(body)


def test_provider_config_is_written_to_the_data_directory_not_the_package():
    """A pip/pipx install must not store the user's API key in site-packages.

    `_PROJECT_ROOT` is the installed package's parent, which for a wheel install
    is site-packages: not the user's to own, and wiped on upgrade.
    """
    env_path = Path(server.ENV_FILE_PATH).resolve()

    assert env_path.parent == server.DATA_DIRECTORY.resolve()
    assert Path(server._PROJECT_ROOT).resolve() not in env_path.parents
    assert Path(server.__file__).resolve().parent not in env_path.parents


def test_runtime_data_directory_configuration_moves_every_server_path(tmp_path, monkeypatch):
    """A CLI-selected data directory must also move the UI-written env file."""
    with monkeypatch.context() as patch:
        patch.setattr(server, "DATA_DIRECTORY", Path("/unused"))
        patch.setattr(server, "ENV_FILE_PATH", "/unused/.env")
        patch.setattr(server, "DEFAULT_SCREENSHOTS_DIR", "/unused/screenshots")
        patch.setattr(
            server,
            "exclusion_settings",
            server.ExclusionSettingsStore(Path("/unused/settings.json")),
        )

        resolved = server.configure_data_directory(tmp_path / "runtime-data")

        assert resolved == (tmp_path / "runtime-data").resolve()
        assert server.DATA_DIRECTORY == resolved
        assert Path(server.ENV_FILE_PATH) == resolved / ".env"
        assert Path(server.DEFAULT_SCREENSHOTS_DIR) == resolved / "screenshots"
        assert server.exclusion_settings.path == resolved / "settings.json"


def test_persisted_provider_config_creates_a_missing_data_directory(tmp_path, monkeypatch):
    """A first run has no data directory yet; saving a key must still work."""
    env_path = tmp_path / "fresh" / ".env"
    monkeypatch.setattr(server, "ENV_FILE_PATH", str(env_path))

    server._persist_env_config(
        model_name="gpt-test",
        model_name_set=True,
        api_key="secret",
        api_base=None,
    )

    assert env_path.exists()
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_persisted_provider_config_is_owner_only(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    monkeypatch.setattr(server, "ENV_FILE_PATH", str(env_path))

    server._persist_env_config(
        model_name="gpt-test",
        model_name_set=True,
        api_key="secret",
        api_base="http://127.0.0.1:9000/v1",
    )

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert "secret" in env_path.read_text()


def test_claude_key_is_persisted_as_anthropic_configuration(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    monkeypatch.setattr(server, "ENV_FILE_PATH", str(env_path))

    server._persist_env_config(
        model_name="claude-sonnet-4-5",
        model_name_set=True,
        api_key="anthropic-secret",
        api_base="https://api.anthropic.com",
    )

    contents = env_path.read_text()
    assert 'MODEL_NAME="claude-sonnet-4-5"' in contents
    assert 'ANTHROPIC_API_KEY="anthropic-secret"' in contents
    assert 'ANTHROPIC_API_BASE="https://api.anthropic.com"' in contents
    assert "TEMPO_LM_API_KEY" not in contents


def test_vertex_express_key_is_persisted_separately(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    monkeypatch.setattr(server, "ENV_FILE_PATH", str(env_path))

    server._persist_env_config(
        model_name="gemini-3.8-flash",
        model_name_set=True,
        api_key="vertex-express-secret",
        api_base=None,
        gemini_vertexai_express=True,
    )

    contents = env_path.read_text()
    assert 'GEMINI_VERTEXAI_EXPRESS="true"' in contents
    assert 'VERTEX_EXPRESS_API_KEY="vertex-express-secret"' in contents
    assert "GOOGLE_API_KEY" not in contents


def test_screenshot_paths_must_stay_in_the_screenshot_directory(tmp_path, monkeypatch):
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    allowed = screenshots / "capture.jpg"
    allowed.write_bytes(b"jpeg")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"private")
    monkeypatch.setattr(server, "DEFAULT_SCREENSHOTS_DIR", str(screenshots))

    assert server._safe_screenshot_path(allowed) == allowed.resolve()
    assert server._safe_screenshot_path(outside) is None


def test_packaged_ui_serves_assets_and_spa_routes(tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    assets = ui / "assets"
    assets.mkdir(parents=True)
    (ui / "index.html").write_text("<html><body>Tempo UI</body></html>")
    (assets / "app-123.js").write_text("console.log('tempo')")
    monkeypatch.setattr(server, "UI_DIRECTORY", ui)

    with TestClient(server.app) as client:
        root = client.get("/")
        nested = client.get("/hierarchy")
        asset = client.get("/assets/app-123.js")
        missing_asset = client.get("/assets/missing.js")
        missing_api = client.get("/api/not-real")

    assert root.status_code == 200 and "Tempo UI" in root.text
    assert nested.status_code == 200 and "Tempo UI" in nested.text
    assert root.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in root.headers["content-security-policy"]
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert missing_asset.status_code == 404
    assert missing_api.status_code == 404


def test_packaged_ui_reports_a_missing_build(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "UI_DIRECTORY", tmp_path / "missing")
    with TestClient(server.app) as client:
        response = client.get("/")
    assert response.status_code == 503
    assert "build-wheel.sh" in response.json()["error"]


def _fake_observer(*, seen, handled, dropped=0, listener_alive=True,
                   running=True, drop_reason=None, grabbed=0, attempts=0,
                   saved=0, low_change=0, no_frames=0, skips_visible=0,
                   skips_excluded=0, filter_blocks=0, persist_failure=None,
                   worker_alive=True, exclusion_paused=None):
    """Minimal stand-in shaped like the attributes diagnostics reads."""
    class _Thread:
        def is_alive(self): return listener_alive

    class _Backend:
        listener_task = _Thread() if listener_alive else None

    class _Task:
        def done(self): return not worker_alive

    class _Observer:
        os_backend = _Backend()
        event_loop = None
        _running = running
        _task = _Task()
        _mouse_events_seen = seen
        _mouse_events_handled = handled
        _mouse_events_dropped = dropped
        _last_drop_reason = drop_reason
        _last_mouse_event_time = 0.0
        _frames_grabbed = grabbed
        _persist_attempts = attempts
        _persist_succeeded = saved
        _persist_skipped_low_change = low_change
        _persist_skipped_no_frames = no_frames
        _capture_skips_visible_app = skips_visible
        _capture_skips_excluded = skips_excluded
        _exclusion_paused = skips_excluded > 0 if exclusion_paused is None else exclusion_paused
        ignore_visible_app_names = ["Electron", "Tempo"]
        _last_change_score = 0.0
        change_threshold = 0.04
        _content_filter_blocks = filter_blocks
        _last_persist_failure = persist_failure

    observer = _Observer()
    health = CaptureHealth(clock=lambda: 1000.0)
    health.set_mode("active")
    health.mode_since = 900.0
    health.last_input_seen = 1000.0 if seen else None
    health.last_input_handled = 1000.0 if handled else None
    health.last_frame = 1000.0 if grabbed else None
    if observer._exclusion_paused:
        health.set_mode("excluded")
    elif skips_visible:
        health.set_mode("ignored_app")
    if persist_failure or (attempts and not saved):
        health.result = "failed"
    elif saved:
        health.result = "saved"
    elif low_change:
        health.result = "unchanged"
    elif no_frames:
        health.result = "no_frames"
    observer.capture_health = health
    return observer


@pytest.mark.parametrize(
    "observer_kwargs, expected",
    [
        # Input silence alone cannot distinguish inactivity from permissions.
        (dict(seen=0, handled=0, grabbed=10), "No recent mouse input"),
        # Events arrive and are all discarded: a bug in Tempo, not permissions.
        (dict(seen=500, handled=0, dropped=500, grabbed=10,
              drop_reason="observer not running"), "bug in Tempo"),
        # Listener thread gone.
        (dict(seen=0, handled=0, listener_alive=False), "listener thread is not alive"),
        # Input is fine; the capture loop never grabs a frame.
        (dict(seen=500, handled=500), "not grabbing frames"),
        # Capture suppressed by the user's own exclusion settings.
        (dict(seen=500, handled=500, skips_excluded=90), "exclusion settings"),
        # Capture suppressed because one of Tempo's own windows is on screen.
        (dict(seen=500, handled=500, skips_visible=90), "ignored app is visible"),
        # Frames grabbed, but the screen genuinely has not changed — normal.
        (dict(seen=500, handled=500, grabbed=900, low_change=40), "discarded as unchanged"),
        # Captures attempted but nothing lands on disk.
        (dict(seen=500, handled=500, grabbed=900, attempts=12, saved=0), "latest capture was not saved"),
        # Every frame written then deleted by the content filter — the frames
        # leave no trace, so without this the failure is invisible.
        (dict(seen=500, handled=500, grabbed=900, attempts=12, saved=0,
              filter_blocks=12,
              persist_failure="content filter blocked the frame (password_or_secret)"),
         "content filter blocked"),
        # Fully healthy.
        (dict(seen=500, handled=500, grabbed=900, attempts=12, saved=12), "Capturing normally"),
        # Previous success must not conceal a stopped worker or current pause.
        (dict(seen=500, handled=500, grabbed=900, attempts=12, saved=12,
              running=False), "screen observer has stopped"),
        (dict(seen=500, handled=500, grabbed=900, attempts=12, saved=12,
              worker_alive=False), "capture worker is not running"),
        (dict(seen=500, handled=500, grabbed=900, attempts=12, saved=12,
              skips_excluded=90), "exclusion settings"),
        # Lifetime exclusion counts must not keep a recovered observer paused.
        (dict(seen=500, handled=500, grabbed=900, attempts=12, saved=12,
              skips_excluded=90, exclusion_paused=False), "Capturing normally"),
        (dict(seen=500, handled=500, grabbed=900, attempts=13, saved=12,
              filter_blocks=1,
              persist_failure="content filter blocked the frame (password_or_secret)"),
         "latest capture was not saved: content filter blocked"),
        (dict(seen=500, handled=500, grabbed=900, no_frames=3), "no buffered frames"),
        (dict(seen=500, handled=500, grabbed=900), "nothing has triggered a capture yet"),
    ],
)
def test_observer_diagnostics_attributes_the_right_failure(
    monkeypatch, observer_kwargs, expected
):
    """The verdict must distinguish a permission problem from a Tempo bug.

    These look identical from outside — nothing accumulates either way — and
    they need opposite fixes, so conflating them sends people to change OS
    settings that were never the cause.
    """
    class _System:
        observer = _fake_observer(**observer_kwargs)

        async def cleanup(self, *args, **kwargs):
            """Satisfy the lifespan shutdown hook."""

    monkeypatch.setattr(server, "system", _System())

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        body = client.get("/api/diagnostics/observer").json()

    assert expected in body["verdict"]
    assert body["mouse_events"]["seen"] == observer_kwargs["seen"]
    assert body["verdict"] == body["health"]["verdict"]


def test_observer_diagnostics_without_a_running_observer(monkeypatch):
    monkeypatch.setattr(server, "system", None)
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        body = client.get("/api/diagnostics/observer").json()
    assert body["observer"] is None
    assert "not recording" in body["detail"]


# ── Onboarding ───────────────────────────────────────────────────────────────


def test_onboarding_round_trips_and_drops_unknown_keys(tmp_path, monkeypatch):
    """Answers persist, and only the known questions are stored."""
    monkeypatch.setattr(server, "DATA_DIRECTORY", tmp_path)

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        empty = client.get("/api/onboarding").json()
        assert empty["completed"] is False
        assert len(empty["questions"]) == 12

        saved = client.put("/api/onboarding", json={
            "user_name": "  Shardul  ",
            "responses": {
                "roles": "  researcher, student  ",
                "health": "   ",              # blank answers are not stored
                "not_a_question": "ignored",  # unknown keys are dropped
            },
        }).json()
        assert saved["saved"] is True
        assert saved["answered"] == 1

        loaded = client.get("/api/onboarding").json()
        assert loaded["completed"] is True
        assert loaded["user_name"] == "Shardul"
        assert loaded["responses"] == {"roles": "researcher, student"}


def test_onboarding_answers_are_stored_in_the_data_directory(tmp_path, monkeypatch):
    """Answers describe the user's life — they stay beside their own data."""
    monkeypatch.setattr(server, "DATA_DIRECTORY", tmp_path)

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        client.put("/api/onboarding", json={"user_name": "A", "responses": {"roles": "x"}})

    stored = json.loads((tmp_path / "onboarding.json").read_text())
    assert stored["responses"] == {"roles": "x"}
    assert Path(server._PROJECT_ROOT).resolve() not in (tmp_path / "onboarding.json").parents


def test_missing_or_corrupt_onboarding_reads_as_unanswered(tmp_path, monkeypatch):
    """A damaged file must not stop the app from starting."""
    monkeypatch.setattr(server, "DATA_DIRECTORY", tmp_path)
    (tmp_path / "onboarding.json").write_text("{ not json")

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        body = client.get("/api/onboarding").json()

    assert body["completed"] is False
    assert body["responses"] == {}


def test_dismissing_the_prompt_is_remembered(tmp_path, monkeypatch):
    """Skipping must be recorded, or the prompt reappears before every session."""
    monkeypatch.setattr(server, "DATA_DIRECTORY", tmp_path)

    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/onboarding").json()["dismissed"] is False

        client.put("/api/onboarding", json={
            "user_name": "", "responses": {}, "dismissed": True,
        })
        body = client.get("/api/onboarding").json()
        assert body["dismissed"] is True
        assert body["completed"] is False   # dismissing is not answering

        # A later save keeps the dismissal rather than resurrecting the prompt.
        client.put("/api/onboarding", json={
            "user_name": "A", "responses": {"roles": "x"},
        })
        after = client.get("/api/onboarding").json()
        assert after["dismissed"] is True
        assert after["completed"] is True
