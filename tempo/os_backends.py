from abc import ABC, abstractmethod
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional
import mss
from mss.screenshot import ScreenShot
import pynput
from pyparsing import Dict

from tempo.utils import get_debug_logger


@dataclass
class ActiveWindowInfo:
    """Information about the currently active window."""

    app_name: Optional[str] = None
    bundle_id: Optional[str] = None
    window_title: Optional[str] = None
    pid: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "app_name": self.app_name,
            "bundle_id": self.bundle_id,
            "window_title": self.window_title,
            "pid": self.pid,
        }


class OSBackend(ABC):
    """Abstract base class for screen backends used by the Screen observer.

    This class defines the interface for different screen backends that can be used
    to capture screen content and monitor user interactions.
    The backend is responsible for capturing screenshots and handling mouse events.
    Therefore, it starts a separate thread to listen for mouse events. The specific calls to the provided callback functions must be implemented in `event_loop`.
    To take screenshots, the backend must implement the `capture_screen` method.

    Args:
        on_mouse_move (Callable[[int, int], None]): Callback function to handle mouse move events.
        on_mouse_click (Callable[[int, int, str, bool], None]): Callback function to handle mouse click events.
        on_mouse_scroll (Callable[[int, int], None]): Callback function to handle mouse scroll events.
        debug (bool): Whether to enable debug logging. Defaults to True.
    """

    def __init__(
        self,
        debug: bool = True,
    ) -> None:
        self.on_mouse_move = None
        self.on_mouse_click = None
        self.on_mouse_scroll = None
        self.listener_task = None
        self._mouse_listener = None  # pynput / evdev listener handle
        self.log = get_debug_logger(self, debug=debug)

    def set_mouse_callbacks(
        self,
        on_mouse_move: Callable[[int, int], None],
        on_mouse_click: Callable[[int, int, str, bool], None],
        on_mouse_scroll: Callable[[int, int], None],
    ) -> None:
        self.on_mouse_move = on_mouse_move
        self.on_mouse_click = on_mouse_click
        self.on_mouse_scroll = on_mouse_scroll

        self.listener_task = threading.Thread(target=self.event_loop, daemon=True)
        self.listener_task.start()

    def stop_listeners(self):
        # Stop the underlying mouse listener (pynput/evdev)
        if self._mouse_listener is not None:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None
        if self.listener_task is not None and self.listener_task.is_alive():
            self.listener_task.join(timeout=2)
        self.listener_task = None
        self.on_mouse_move = None
        self.on_mouse_click = None
        self.on_mouse_scroll = None

    def close(self):
        """Release listener and cached capture resources deterministically."""
        self.stop_listeners()
        if hasattr(self, "_mss") and self._mss is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None

    def __del__(self):
        self.close()

    @abstractmethod
    def capture_screen(self, monitor: Dict) -> ScreenShot:
        """Capture the screen content for the given monitor.

        Args:
            monitor (Dict): A dictionary representing the monitor to capture with bbox coordinates as keys (left, top, width, height).

        Returns:
            ScreenShot: A screenshot object containing the captured screen content.
        """
        pass

    @abstractmethod
    def send_notification(self, title: str, message: str) -> None:
        """Send a desktop notification.

        Args:
            title (str): The title of the notification.
            message (str): The message content of the notification.
        """
        pass

    @abstractmethod
    def event_loop(self) -> None:
        """Start the event loop to monitor user interactions. Should call the appropriate self.on_mouse_* callbacks."""
        pass

    def get_active_window(self) -> Optional[ActiveWindowInfo]:
        """Get information about the currently active window.

        Returns:
            ActiveWindowInfo if available, None otherwise.
        """
        return None

    def is_app_visible(self, app_name: str) -> bool:
        """Return True if the app has any visible window on screen."""
        return False

    def is_display_asleep(self) -> bool:
        """Return True if the display is asleep or the screen is locked."""
        return False


class MacOSBackend(OSBackend):
    """Screen backend implementation for macOS."""

    @staticmethod
    def _get_global_display_bounds() -> tuple[float, float, float, float]:
        from Quartz import CGGetActiveDisplayList, CGDisplayBounds, kCGErrorSuccess

        err, display_ids, count = CGGetActiveDisplayList(16, None, None)
        if err != kCGErrorSuccess:
            raise OSError(f"CGGetActiveDisplayList failed with error {err}")

        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")

        for display_id in display_ids[:count]:
            rect = CGDisplayBounds(display_id)
            x0 = rect.origin.x
            y0 = rect.origin.y
            x1 = x0 + rect.size.width
            y1 = y0 + rect.size.height

            if x0 < min_x:
                min_x = x0
            if y0 < min_y:
                min_y = y0
            if x1 > max_x:
                max_x = x1
            if y1 > max_y:
                max_y = y1

        return (min_x, min_y, max_x, max_y)

    def _get_visible_windows(self) -> list[tuple[dict, float]]:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGWindowListOptionIncludingWindow,
            kCGNullWindowID,
        )
        from shapely.geometry import box
        from shapely.ops import unary_union

        (_min_x, _min_y, global_max_x, global_max_y) = self._get_global_display_bounds()
        options = kCGWindowListOptionOnScreenOnly | kCGWindowListOptionIncludingWindow
        window_list = CGWindowListCopyWindowInfo(options, kCGNullWindowID)

        occupied_region = None
        visible_windows: list[tuple[dict, float]] = []

        for window_info in window_list:
            owner = window_info.get("kCGWindowOwnerName", "")
            if owner in ("Dock", "WindowServer", "Window Server"):
                continue

            bounds = window_info.get("kCGWindowBounds", {})
            x = bounds.get("X", 0)
            y = bounds.get("Y", 0)
            w = bounds.get("Width", 0)
            h = bounds.get("Height", 0)
            if w <= 0 or h <= 0:
                continue

            inverted_y = global_max_y - y - h
            window_poly = box(x, inverted_y, x + w, inverted_y + h)
            if window_poly.is_empty:
                continue

            if occupied_region is None:
                visible_part = window_poly
            else:
                visible_part = window_poly.difference(occupied_region)

            if not visible_part.is_empty:
                ratio = visible_part.area / window_poly.area
                visible_windows.append((window_info, ratio))

                if occupied_region is None:
                    occupied_region = window_poly
                else:
                    occupied_region = unary_union([occupied_region, window_poly])

        return visible_windows

    def capture_screen(self, monitor: Dict) -> ScreenShot:
        if not hasattr(self, "_mss") or self._mss is None:
            self._mss = mss.mss()
        try:
            return self._mss.grab(monitor)
        except Exception:
            # If the cached instance is stale (e.g. after sleep/wake),
            # close it and create a fresh one for a single retry.
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = mss.mss()
            return self._mss.grab(monitor)

    def send_notification(self, title: str, message: str) -> None:
        pass

    def get_active_window(self) -> Optional[ActiveWindowInfo]:
        """Get the currently active window on macOS.

        The frontmost app is read from the window server's z-order rather than
        from ``NSWorkspace.frontmostApplication()``. NSWorkspace answers from
        the *calling* process's GUI context, so a non-GUI helper — Tempo's
        capture process, spawned by the Electron shell — gets told that Electron
        is frontmost no matter which app the user is actually in. Since Tempo
        declines to record its own windows, that reading silently rejected every
        capture. The window list is owned by the window server and gives the
        same answer regardless of who asks.
        """
        try:
            from AppKit import NSRunningApplication, NSWorkspace
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
                kCGWindowListExcludeDesktopElements,
            )

            app_name = bundle_id = window_title = None
            pid = None

            try:
                options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
                window_list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []
                # Front-to-back order. Layer 0 is the ordinary window layer;
                # anything above it is an overlay (menu-bar extras, Tempo's own
                # always-on-top pill) that is not what the user is working in.
                for window in window_list:
                    if window.get("kCGWindowLayer", 0) != 0:
                        continue
                    owner = window.get("kCGWindowOwnerName")
                    if not owner:
                        continue
                    app_name = owner
                    pid = window.get("kCGWindowOwnerPID")
                    window_title = window.get("kCGWindowName") or None
                    break
            except Exception:
                pass

            if pid is not None:
                running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if running is not None:
                    bundle_id = running.bundleIdentifier()
                    app_name = running.localizedName() or app_name

            if app_name is None:
                # No usable window (all displays asleep, a full-screen overlay).
                # NSWorkspace is the weaker source, but better than nothing.
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                if app is None:
                    return None
                app_name = app.localizedName()
                bundle_id = app.bundleIdentifier()
                pid = app.processIdentifier()

            return ActiveWindowInfo(
                app_name=app_name,
                bundle_id=bundle_id,
                window_title=window_title,
                pid=pid,
            )
        except ImportError:
            return None
        except Exception:
            return None

    def is_app_visible(self, app_name: str) -> bool:
        try:
            visible_windows = self._get_visible_windows()
        except Exception:
            return False

        app_name_lower = app_name.lower()
        for info, _ratio in visible_windows:
            if info.get("kCGWindowOwnerName", "").lower() != app_name_lower:
                continue
            # Ignore menu-bar / tray icons (layer != 0, tiny size)
            if info.get("kCGWindowLayer", 0) != 0:
                continue
            bounds = info.get("kCGWindowBounds", {})
            if bounds.get("Width", 0) < 200 or bounds.get("Height", 0) < 200:
                continue
            return True
        return False

    def is_display_asleep(self) -> bool:
        """Check if the display is asleep, screen is locked, or session is inactive."""
        try:
            from Quartz import (
                CGMainDisplayID, CGDisplayIsAsleep,
                CGSessionCopyCurrentDictionary,
            )
            if CGDisplayIsAsleep(CGMainDisplayID()):
                return True
            session = CGSessionCopyCurrentDictionary()
            if session:
                if session.get("CGSSessionScreenIsLocked", False):
                    return True
                # Fast user switching: another user is on the console
                if not session.get("kCGSSessionOnConsoleKey", True):
                    return True
            return False
        except Exception:
            return False

    def event_loop(self) -> None:

        listener = pynput.mouse.Listener(
            on_move=lambda x, y: self.on_mouse_move(x, y),
            on_click=lambda x, y, btn, prsd: (
                self.on_mouse_click(x, y, btn, prsd) if prsd else None
            ),
            on_scroll=lambda x, y, dx, dy: self.on_mouse_scroll(x, y, dx, dy),
        )
        self._mouse_listener = listener
        listener.start()
        try:
            listener.join()
        except KeyboardInterrupt:
            listener.stop()


class GnomeBackend(OSBackend):
    """Screen backend implementation for Gnome."""

    def __init__(self, mouse_device=None, debug=True):
        # Import Linux-specific modules only when needed
        try:
            import dbus
            import evdev
            import pyscreenshot as ImageGrab
        except ImportError as e:
            raise RuntimeError(
                "The GNOME backend requires optional dependencies (dbus, evdev, pyscreenshot). "
                "Install them or choose --platform macos."
            ) from e
        
        self.mouse_device = mouse_device or self._get_likely_mouse_device()
        self.gnome_session_dbus = None
        self.get_global_pointer_position = None
        self.send_notification = None
        self.ImageGrab = ImageGrab
        try:
            bus_name = os.getenv("GNOME_SESSION_BUS_NAME", "org.yba.GnomeSession")
            object_path = os.getenv("GNOME_SESSION_OBJECT_PATH", "/org/yba/GnomeSession")
            self.gnome_session_dbus = dbus.SessionBus().get_object(bus_name, object_path)
            self.get_global_pointer_position = self.gnome_session_dbus.get_dbus_method(
                "GetGlobalPointerPosition", bus_name
            )
            self.send_notification = self.gnome_session_dbus.get_dbus_method(
                "SendNotification", bus_name
            )
        except dbus.DBusException as e:
            raise RuntimeError(
                "Failed to get mouse position, make sure the gnome extension is running."
            )
        super().__init__(debug=debug)
        self.log.info(f"Using mouse device: {self.mouse_device.name} at {self.mouse_device.path}")

    def _get_likely_mouse_device(self):
        import evdev
        devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
        for device in devices:
            if "mouse" in device.name.lower():
                return device
            if "touch" in device.name.lower():
                return device
        raise RuntimeError("No mouse device found. Mouse device can be explicitly provided.")

    def capture_screen(self, monitor: Dict) -> ScreenShot:
        im = self.ImageGrab.grab(
            bbox=(
                monitor["left"],
                monitor["top"],
                monitor["left"] + monitor["width"],
                monitor["top"] + monitor["height"],
            )
        )
        return ScreenShot(im.tobytes(), monitor)

    def send_notification(self, title: str, message: str) -> None:
        """Send a desktop notification.

        Args:
            title (str): The title of the notification.
            message (str): The message content of the notification.
        """
        if self.send_notification:
            self.send_notification(title, message)

    def event_loop(self) -> None:
        import evdev
        for event in self.mouse_device.read_loop():
            x, y = self.get_global_pointer_position()
            if event.type == evdev.ecodes.EV_REL or event.type == evdev.ecodes.EV_ABS:
                if event.code in (
                    evdev.ecodes.REL_X,
                    evdev.ecodes.REL_Y,
                    evdev.ecodes.ABS_X,
                    evdev.ecodes.ABS_Y,
                ):
                    self.on_mouse_move(x, y)
                elif event.code in (
                    evdev.ecodes.REL_WHEEL,
                    evdev.ecodes.REL_WHEEL_HI_RES,
                    evdev.ecodes.ABS_WHEEL,
                ):
                    self.on_mouse_scroll(x, y, 0, event.value)
            elif event.type == evdev.ecodes.EV_KEY:
                if (
                    event.code in (evdev.ecodes.BTN_LEFT, evdev.ecodes.BTN_RIGHT)
                    and event.value == 1
                ):
                    self.on_mouse_click(x, y, event.code, event.value == 1)
