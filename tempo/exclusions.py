"""Local application discovery and recording-exclusion policy."""

from __future__ import annotations

import base64
import configparser
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit


DEFAULT_SETTINGS_PATH = Path("~/.cache/tempo/settings.json").expanduser()
DEFAULT_ICON_CACHE_DIR = Path("~/.cache/tempo/app-icons").expanduser()
MACOS_APP_ROOTS = (
    (Path("/Applications"), "applications"),
    (Path("/System/Applications"), "system"),
    (Path("/System/Applications/Utilities"), "utilities"),
    (Path("~/Applications").expanduser(), "user"),
)
LINUX_APP_ROOTS = (
    (Path("~/.local/share/applications").expanduser(), "user"),
    (Path("/usr/share/applications"), "system"),
)


@dataclass(frozen=True)
class InstalledApp:
    bundle_id: str
    name: str
    icon_data_url: Optional[str]
    source: str

    def to_dict(self) -> dict[str, Optional[str]]:
        return {
            "bundle_id": self.bundle_id,
            "name": self.name,
            "icon_data_url": self.icon_data_url,
            "source": self.source,
        }


@dataclass(frozen=True)
class ExclusionSettings:
    apps: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {"apps": list(self.apps), "domains": list(self.domains)}


def _data_url(path: Path) -> Optional[str]:
    try:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def _mac_icon_data_url(
    app_path: Path,
    bundle_id: str,
    icon_name: object,
    cache_dir: Path,
) -> Optional[str]:
    if not isinstance(icon_name, str) or not icon_name.strip():
        return None
    icon_file = Path(icon_name.strip()).name
    if not icon_file.lower().endswith(".icns"):
        icon_file += ".icns"
    source = app_path / "Contents" / "Resources" / icon_file
    if not source.is_file():
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_name = hashlib.sha256(bundle_id.encode("utf-8")).hexdigest()[:24] + ".png"
    cached = cache_dir / cache_name
    if not cached.is_file():
        try:
            result = subprocess.run(
                ["sips", "-s", "format", "png", "-z", "32", "32", str(source), "--out", str(cached)],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not cached.is_file():
            cached.unlink(missing_ok=True)
            return None
        try:
            os.chmod(cached, 0o600)
        except OSError:
            pass
    return _data_url(cached)


def _discover_macos(
    roots: Iterable[tuple[Path, str]], icon_cache_dir: Path
) -> list[InstalledApp]:
    apps: dict[str, InstalledApp] = {}
    for root, source in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for app_path in entries:
            if app_path.suffix.lower() != ".app" or not app_path.is_dir():
                continue
            try:
                with (app_path / "Contents" / "Info.plist").open("rb") as handle:
                    info = plistlib.load(handle)
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            bundle_id = str(info.get("CFBundleIdentifier") or "").strip()
            if not bundle_id or bundle_id in apps:
                continue
            name = str(
                info.get("CFBundleDisplayName")
                or info.get("CFBundleName")
                or app_path.stem
            ).strip()
            icon = _mac_icon_data_url(
                app_path, bundle_id, info.get("CFBundleIconFile"), icon_cache_dir
            )
            apps[bundle_id] = InstalledApp(bundle_id, name or app_path.stem, icon, source)
    return sorted(apps.values(), key=lambda app: (app.name.casefold(), app.bundle_id))


def _desktop_bool(section: configparser.SectionProxy, key: str) -> bool:
    return section.get(key, "false").strip().lower() in {"1", "true", "yes"}


def _discover_linux(roots: Iterable[tuple[Path, str]]) -> list[InstalledApp]:
    apps: dict[str, InstalledApp] = {}
    for root, source in roots:
        if not root.is_dir():
            continue
        for desktop_path in sorted(root.glob("*.desktop")):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(desktop_path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (OSError, KeyError, configparser.Error):
                continue
            if entry.get("Type", "Application") != "Application":
                continue
            if _desktop_bool(entry, "Hidden") or _desktop_bool(entry, "NoDisplay"):
                continue
            name = entry.get("Name", "").strip()
            if not name:
                continue
            app_id = desktop_path.name
            if app_id in apps:
                continue
            icon_value = entry.get("Icon", "").strip()
            icon_path = Path(icon_value).expanduser() if icon_value else None
            icon = _data_url(icon_path) if icon_path and icon_path.suffix.lower() == ".png" else None
            apps[app_id] = InstalledApp(app_id, name, icon, source)
    return sorted(apps.values(), key=lambda app: (app.name.casefold(), app.bundle_id))


def discover_installed_apps(
    *,
    platform: Optional[str] = None,
    roots: Optional[Iterable[tuple[Path, str]]] = None,
    icon_cache_dir: Path = DEFAULT_ICON_CACHE_DIR,
) -> list[dict[str, Optional[str]]]:
    """Discover launchable desktop apps without requiring Electron."""
    selected = platform or sys.platform
    if selected == "darwin":
        found = _discover_macos(roots or MACOS_APP_ROOTS, icon_cache_dir)
    elif selected.startswith("linux"):
        found = _discover_linux(roots or LINUX_APP_ROOTS)
    else:
        found = []
    return [app.to_dict() for app in found]


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_domain(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname or parsed.username or parsed.password:
            return None
        hostname = hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    if hostname.startswith("www."):
        hostname = hostname[4:]
    labels = hostname.split(".")
    if len(hostname) > 253 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        return None
    return hostname


def domain_matches(hostname: str, excluded_domain: str) -> bool:
    return hostname == excluded_domain or hostname.endswith("." + excluded_domain)


class ExclusionSettingsStore:
    """Atomic, owner-only storage with an mtime-backed read cache."""

    def __init__(self, path: Path = DEFAULT_SETTINGS_PATH) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._cached: Optional[ExclusionSettings] = None
        self._cached_mtime_ns: Optional[int] = None

    def load(self) -> ExclusionSettings:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if self._cached is not None and mtime_ns == self._cached_mtime_ns:
            return self._cached
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        settings = self.normalize(payload.get("apps", []), payload.get("domains", []))
        self._cached = settings
        self._cached_mtime_ns = mtime_ns
        return settings

    @staticmethod
    def normalize(apps: object, domains: object) -> ExclusionSettings:
        normalized_apps = tuple(dict.fromkeys(
            str(item).strip() for item in (apps if isinstance(apps, list) else [])
            if str(item).strip()
        ))
        normalized_domains = tuple(dict.fromkeys(
            domain for item in (domains if isinstance(domains, list) else [])
            if (domain := normalize_domain(item)) is not None
        ))
        return ExclusionSettings(normalized_apps, normalized_domains)

    def save(self, apps: object, domains: object) -> ExclusionSettings:
        settings = self.normalize(apps, domains)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=self.path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(settings.to_dict(), handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                self._cached_mtime_ns = self.path.stat().st_mtime_ns
                self._cached = settings
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                Path(temporary).unlink(missing_ok=True)
                raise
        return settings


_BROWSER_SCRIPTS = {
    "com.apple.Safari": 'tell application "Safari" to get URL of front document',
    "com.google.Chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
}
_BROWSER_AUTOMATION_DENIED: set[str] = set()


def get_active_browser_url(bundle_id: Optional[str]) -> Optional[str]:
    script = _BROWSER_SCRIPTS.get(bundle_id or "")
    if (
        script is None
        or sys.platform != "darwin"
        or bundle_id in _BROWSER_AUTOMATION_DENIED
    ):
        return None
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        error = (result.stderr or "").lower()
        if "-1743" in error or "not allowed" in error or "denied" in error:
            _BROWSER_AUTOMATION_DENIED.add(bundle_id)
        return None
    return result.stdout.strip() or None


class ExclusionController:
    def __init__(self, store: Optional[ExclusionSettingsStore] = None) -> None:
        self.store = store or ExclusionSettingsStore()

    def should_pause(self, active_window: object) -> bool:
        settings = self.store.load()
        bundle_id = getattr(active_window, "bundle_id", None)
        if bundle_id and bundle_id in settings.apps:
            return True
        if not settings.domains or bundle_id not in _BROWSER_SCRIPTS:
            return False
        url = get_active_browser_url(bundle_id)
        hostname = normalize_domain(url) if url else None
        return bool(hostname and any(domain_matches(hostname, domain) for domain in settings.domains))
