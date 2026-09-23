import base64
import json
import os
import plistlib
import stat
import asyncio
from pathlib import Path
from types import SimpleNamespace

import tempo.exclusions as exclusions
import tempo.server as server
from tempo.observers.screen import Screen
from fastapi.testclient import TestClient


def _mac_app(root: Path, folder: str, bundle_id: str, name: str, *, icon: bool = False) -> None:
    contents = root / folder / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True)
    info = {"CFBundleIdentifier": bundle_id, "CFBundleName": name}
    if icon:
        info["CFBundleIconFile"] = "AppIcon"
        (resources / "AppIcon.icns").write_bytes(b"icns")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)


def test_macos_discovery_covers_roots_and_deduplicates_bundle_ids(tmp_path, monkeypatch):
    applications = tmp_path / "Applications"
    system = tmp_path / "System Applications"
    applications.mkdir()
    system.mkdir()
    _mac_app(applications, "Safari.app", "com.apple.Safari", "Safari", icon=True)
    _mac_app(system, "Safari.app", "com.apple.Safari", "Duplicate Safari")
    _mac_app(system, "Notes.app", "com.apple.Notes", "Notes")

    def fake_sips(args, **_kwargs):
        Path(args[-1]).write_bytes(b"png")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(exclusions.subprocess, "run", fake_sips)
    apps = exclusions.discover_installed_apps(
        platform="darwin",
        roots=((applications, "applications"), (system, "system")),
        icon_cache_dir=tmp_path / "icons",
    )

    assert [app["bundle_id"] for app in apps] == ["com.apple.Notes", "com.apple.Safari"]
    safari = next(app for app in apps if app["bundle_id"] == "com.apple.Safari")
    assert safari["name"] == "Safari"
    assert safari["source"] == "applications"
    assert safari["icon_data_url"] == "data:image/png;base64," + base64.b64encode(b"png").decode()


def test_linux_discovery_parses_visible_desktop_entries(tmp_path):
    root = tmp_path / "applications"
    root.mkdir()
    (root / "editor.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Editor\n", encoding="utf-8"
    )
    (root / "hidden.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\n", encoding="utf-8"
    )
    apps = exclusions.discover_installed_apps(
        platform="linux", roots=((root, "system"),)
    )
    assert apps == [{
        "bundle_id": "editor.desktop",
        "name": "Editor",
        "icon_data_url": None,
        "source": "system",
    }]


def test_settings_are_normalized_cached_and_owner_only(tmp_path):
    path = tmp_path / "tempo" / "settings.json"
    store = exclusions.ExclusionSettingsStore(path)
    saved = store.save(
        ["com.apple.Notes", "com.apple.Notes", ""],
        ["HTTPS://WWW.Example.com/path", "sub.example.org", "bad domain"],
    )
    assert saved.to_dict() == {
        "apps": ["com.apple.Notes"],
        "domains": ["example.com", "sub.example.org"],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load() == saved
    assert json.loads(path.read_text()) == saved.to_dict()


def test_exclusion_controller_matches_bundle_and_domain_boundaries(tmp_path, monkeypatch):
    store = exclusions.ExclusionSettingsStore(tmp_path / "settings.json")
    store.save(["com.apple.Notes"], ["example.com"])
    controller = exclusions.ExclusionController(store)
    assert controller.should_pause(SimpleNamespace(bundle_id="com.apple.Notes")) is True

    monkeypatch.setattr(exclusions, "get_active_browser_url", lambda _bundle: "https://sub.example.com/page")
    assert controller.should_pause(SimpleNamespace(bundle_id="com.apple.Safari")) is True
    monkeypatch.setattr(exclusions, "get_active_browser_url", lambda _bundle: "https://notexample.com")
    assert controller.should_pause(SimpleNamespace(bundle_id="com.apple.Safari")) is False
    assert controller.should_pause(SimpleNamespace(bundle_id="org.mozilla.firefox")) is False


def test_browser_automation_denial_is_not_retried(monkeypatch):
    calls = 0

    def denied(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=1, stdout="", stderr="Not allowed (-1743)")

    exclusions._BROWSER_AUTOMATION_DENIED.clear()
    monkeypatch.setattr(exclusions.sys, "platform", "darwin")
    monkeypatch.setattr(exclusions.subprocess, "run", denied)
    assert exclusions.get_active_browser_url("com.apple.Safari") is None
    assert exclusions.get_active_browser_url("com.apple.Safari") is None
    assert calls == 1


def test_exclusion_settings_api_round_trip(tmp_path, monkeypatch):
    store = exclusions.ExclusionSettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(server, "exclusion_settings", store)
    monkeypatch.setattr(
        server,
        "discover_installed_apps",
        lambda: [{"bundle_id": "com.example.App", "name": "App", "icon_data_url": None, "source": "user"}],
    )
    with TestClient(server.app) as client:
        assert client.get("/api/apps/installed").json()[0]["bundle_id"] == "com.example.App"
        response = client.put(
            "/api/settings/exclusions",
            json={"apps": ["com.example.App"], "domains": ["www.Example.com"]},
        )
        assert response.status_code == 200
        assert response.json() == {"apps": ["com.example.App"], "domains": ["example.com"]}
        assert client.get("/api/settings/exclusions").json() == response.json()


def test_screen_observer_owns_exclusion_polling(monkeypatch):
    screen = Screen.__new__(Screen)
    screen.os_backend = SimpleNamespace(
        get_active_window=lambda: SimpleNamespace(bundle_id="com.apple.Notes")
    )
    screen._exclusion_controller = SimpleNamespace(should_pause=lambda _window: True)
    screen._exclusion_paused = False
    screen._next_exclusion_poll_at = 0.0
    screen.log = SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)

    asyncio.run(screen._poll_exclusions())
    assert screen._exclusion_paused is True

    screen._exclusion_controller.should_pause = lambda _window: False
    asyncio.run(screen._poll_exclusions())
    assert screen._exclusion_paused is True  # cadence prevents a duplicate platform poll

    screen._next_exclusion_poll_at = 0.0
    asyncio.run(screen._poll_exclusions())
    assert screen._exclusion_paused is False


def test_electron_has_no_active_app_timer_or_pause_round_trip():
    root = Path(__file__).resolve().parents[1]
    exclusion_source = (root / "electron-app" / "app-exclusion.ts").read_text()
    server_source = (root / "tempo" / "server.py").read_text()
    assert "setInterval" not in exclusion_source
    assert "/api/pause" not in exclusion_source + server_source
    assert "/api/resume" not in exclusion_source + server_source
