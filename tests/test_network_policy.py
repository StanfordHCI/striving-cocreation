import socket
from ipaddress import ip_address
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tempo.server as server
from tempo.cli import _endpoint_scope


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOTS = (REPO_ROOT / "tempo", REPO_ROOT / "electron-app", REPO_ROOT / "docs" / "src")
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html"}
IGNORED_PARTS = {"node_modules", "dist", "dist-main"}


def _runtime_sources():
    for root in RUNTIME_ROOTS:
        for path in root.rglob("*"):
            if path.suffix in TEXT_SUFFIXES and not IGNORED_PARTS.intersection(path.parts):
                yield path


def test_runtime_has_no_removed_telemetry_clients():
    combined = "\n".join(path.read_text(errors="ignore").lower() for path in _runtime_sources())

    assert "firebase-admin" not in combined
    assert "firebase-logger" not in combined
    assert "sentry_sdk" not in combined
    assert "@sentry/" not in combined


def test_renderer_has_no_implicit_remote_assets():
    renderer = REPO_ROOT / "electron-app" / "src"
    combined = "\n".join(
        path.read_text(errors="ignore").lower()
        for path in renderer.rglob("*")
        if path.suffix in TEXT_SUFFIXES
    )

    assert "fonts.googleapis.com" not in combined
    assert "google.com/s2/favicons" not in combined


class OutboundConnection(AssertionError):
    """Raised when runtime code opens a socket to a non-loopback address."""


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture
def block_external_sockets(monkeypatch):
    """Fail the test if anything tries to connect off-machine.

    A static grep cannot catch a destination introduced through a new client
    library, so this asserts at the socket layer instead.
    """
    attempted: list[str] = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if not _is_loopback(str(host)):
            attempted.append(str(host))
            raise OutboundConnection(f"connect() to non-loopback host {host!r}")
        return real_connect(self, address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback(str(host)):
            attempted.append(str(host))
            raise OutboundConnection(f"DNS lookup for non-loopback host {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    return attempted


def test_socket_guard_actually_catches_an_external_connection(block_external_sockets):
    """The guard must fail on a real external attempt, or the next test is vacuous."""
    with pytest.raises(OutboundConnection):
        socket.create_connection(("example.com", 80), timeout=0.1)
    assert block_external_sockets == ["example.com"]


def test_serving_the_api_never_leaves_loopback(block_external_sockets, tmp_path, monkeypatch):
    """Exercising the read surface must not contact anything off-machine."""
    monkeypatch.setattr(server, "DATA_DIRECTORY", tmp_path)
    monkeypatch.setattr(server, "DEFAULT_SCREENSHOTS_DIR", str(tmp_path / "screenshots"))

    with TestClient(server.app) as client:
        for path in (
            "/api/status",
            "/api/config",
            "/api/config/env",
            "/api/settings/exclusions",
        ):
            response = client.get(path)
            assert response.status_code < 500, (path, response.status_code)

    assert block_external_sockets == []


def test_outbound_policy_classifies_only_configured_llm_as_external():
    for local in (
        "http://127.0.0.1:8756",
        "ws://127.0.0.1:8756/ws",
        "http://localhost:8000/v1",
        "http://[::1]:8756",
    ):
        assert _endpoint_scope(local) == "local", local

    for external in (
        "https://generativelanguage.googleapis.com",
        "https://api.openai.com/v1",
        "https://www.google.com/s2/favicons",
        "https://sentry.io",
    ):
        assert _endpoint_scope(external) == "external", external


def test_doctor_reports_exactly_one_external_destination_by_default():
    """The default configuration may reach the LLM provider and nothing else."""
    report = server_doctor_report()
    external = [line for line in report.splitlines() if "[external]" in line]
    assert len(external) == 1, report
    assert "generativelanguage.googleapis.com" in external[0]


def server_doctor_report() -> str:
    from tempo.cli import build_doctor_report

    return build_doctor_report(env={"MODEL_NAME": "gemini-3.8-flash"})
