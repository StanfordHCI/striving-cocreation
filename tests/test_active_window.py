"""Frontmost-app detection must not depend on who is asking.

Tempo's capture process runs as a non-GUI child of the Electron shell.
``NSWorkspace.frontmostApplication()`` answers from the *calling* process's GUI
context, so from there it reports Electron regardless of which app the user is
actually in — and because Tempo declines to record its own windows, that reading
rejected every single capture while looking perfectly healthy from outside.
"""

from __future__ import annotations

import sys
import types

import pytest

from tempo.os_backends import MacOSBackend


LAYER_OVERLAY = 3      # always-on-top, e.g. Tempo's floating pill
LAYER_NORMAL = 0       # ordinary application windows


def _install_fake_quartz(monkeypatch, window_list, frontmost_name="Electron"):
    """Stub the two macOS frameworks `get_active_window` imports."""
    quartz = types.ModuleType("Quartz")
    quartz.CGWindowListCopyWindowInfo = lambda *_: window_list
    quartz.kCGWindowListOptionOnScreenOnly = 1
    quartz.kCGNullWindowID = 0
    quartz.kCGWindowListExcludeDesktopElements = 2

    class _App:
        def localizedName(self): return frontmost_name
        def bundleIdentifier(self): return "com.github.Electron"
        def processIdentifier(self): return 999

    class _Workspace:
        @staticmethod
        def sharedWorkspace(): return _Workspace()
        def frontmostApplication(self): return _App()

    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = _Workspace
    # No mapping available for the fake pids; bundle id simply stays unset.
    appkit.NSRunningApplication = types.SimpleNamespace(
        runningApplicationWithProcessIdentifier_=staticmethod(lambda pid: None)
    )

    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)


def test_frontmost_app_comes_from_the_window_server_not_the_caller(monkeypatch):
    """The user is in Chrome; NSWorkspace wrongly claims Electron."""
    _install_fake_quartz(monkeypatch, [
        {"kCGWindowLayer": LAYER_NORMAL, "kCGWindowOwnerName": "Google Chrome",
         "kCGWindowOwnerPID": 100, "kCGWindowName": "Inbox"},
        {"kCGWindowLayer": LAYER_NORMAL, "kCGWindowOwnerName": "Code",
         "kCGWindowOwnerPID": 101, "kCGWindowName": "main.py"},
    ], frontmost_name="Electron")

    window = MacOSBackend().get_active_window()

    assert window.app_name == "Google Chrome"
    assert window.window_title == "Inbox"
    assert window.pid == 100


def test_always_on_top_overlays_are_not_the_active_window(monkeypatch):
    """Tempo's own floating pill sits above everything and must be skipped.

    It is the frontmost window on screen at all times; treating it as the active
    window would make Tempo believe the user is always looking at Tempo.
    """
    _install_fake_quartz(monkeypatch, [
        {"kCGWindowLayer": LAYER_OVERLAY, "kCGWindowOwnerName": "Electron",
         "kCGWindowOwnerPID": 55, "kCGWindowName": "Recording"},
        {"kCGWindowLayer": LAYER_NORMAL, "kCGWindowOwnerName": "Google Chrome",
         "kCGWindowOwnerPID": 100, "kCGWindowName": "Inbox"},
    ])

    window = MacOSBackend().get_active_window()

    assert window.app_name == "Google Chrome"
    assert window.pid == 100


def test_tempo_is_still_detected_when_it_genuinely_is_in_front(monkeypatch):
    """The ignore rule must keep working — this is not a blanket bypass."""
    _install_fake_quartz(monkeypatch, [
        {"kCGWindowLayer": LAYER_NORMAL, "kCGWindowOwnerName": "Electron",
         "kCGWindowOwnerPID": 55, "kCGWindowName": "Tempo"},
        {"kCGWindowLayer": LAYER_NORMAL, "kCGWindowOwnerName": "Google Chrome",
         "kCGWindowOwnerPID": 100, "kCGWindowName": "Inbox"},
    ])

    window = MacOSBackend().get_active_window()

    assert window.app_name == "Electron"
    assert window.window_title == "Tempo"


def test_falls_back_to_nsworkspace_when_no_ordinary_window_is_on_screen(monkeypatch):
    """Every window is an overlay (displays asleep, full-screen takeover)."""
    _install_fake_quartz(monkeypatch, [
        {"kCGWindowLayer": LAYER_OVERLAY, "kCGWindowOwnerName": "Window Server",
         "kCGWindowOwnerPID": 1, "kCGWindowName": "StatusIndicator"},
    ], frontmost_name="Finder")

    window = MacOSBackend().get_active_window()

    assert window.app_name == "Finder"


def test_returns_none_rather_than_raising_when_the_window_list_fails(monkeypatch):
    quartz = types.ModuleType("Quartz")
    quartz.CGWindowListCopyWindowInfo = lambda *_: (_ for _ in ()).throw(OSError("boom"))
    quartz.kCGWindowListOptionOnScreenOnly = 1
    quartz.kCGNullWindowID = 0
    quartz.kCGWindowListExcludeDesktopElements = 2

    class _Workspace:
        @staticmethod
        def sharedWorkspace(): return _Workspace()
        def frontmostApplication(self): return None

    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = _Workspace
    appkit.NSRunningApplication = types.SimpleNamespace(
        runningApplicationWithProcessIdentifier_=staticmethod(lambda pid: None)
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    assert MacOSBackend().get_active_window() is None
