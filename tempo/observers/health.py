"""Recent capture progress, independent of polling and lifetime counters."""

import time
from collections.abc import Callable
from typing import Literal


CaptureMode = Literal["starting", "active", "display_asleep", "excluded", "ignored_app"]
CaptureResult = Literal["saved", "unchanged", "no_frames", "failed"]


class CaptureHealth:
    FRAME_STALL_SECONDS = 30.0
    INPUT_IDLE_SECONDS = 120.0
    PROCESSING_STALL_SECONDS = 120.0

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.mode: CaptureMode = "starting"
        self.mode_since = self.last_loop = clock()
        self.last_input_seen: float | None = None
        self.last_input_handled: float | None = None
        self.last_frame: float | None = None
        self.result: CaptureResult | None = None
        self.pending: dict[int, float] = {}
        self._sequence = 0

    def set_mode(self, mode: CaptureMode) -> None:
        if mode != self.mode:
            self.mode = mode
            self.mode_since = self.clock()
            if mode == "active":
                # Give capture a fresh grace period after sleep or exclusions.
                self.result = None

    def begin_capture(self) -> int:
        self._sequence += 1
        self.pending[self._sequence] = self.clock()
        return self._sequence

    def finish_capture(self, token: int) -> None:
        self.pending.pop(token, None)

    def report(self, *, running: bool, worker_alive: bool, listener_alive: bool,
               failure_reason: str | None = None) -> dict:
        now = self.clock()

        def age(timestamp: float | None) -> float | None:
            return round(max(0.0, now - timestamp), 1) if timestamp is not None else None

        handled_at = self.last_input_handled if self.last_input_handled is not None else self.mode_since
        frame_at = self.last_frame if self.last_frame is not None else self.mode_since
        frame_age = now - max(frame_at, self.mode_since)
        handled_age = now - max(handled_at, self.mode_since)
        pending_age = (
            now - max(min(self.pending.values()), self.mode_since) if self.pending else None
        )
        state, verdict = "healthy", "Capturing normally."
        if not running:
            state, verdict = "error", "The screen observer has stopped. Stop and restart recording."
        elif not worker_alive:
            state, verdict = "error", "The capture worker is not running. Stop and restart recording."
        elif not listener_alive:
            state, verdict = "error", "The input listener thread is not alive."
        elif now - self.last_loop >= self.FRAME_STALL_SECONDS:
            state, verdict = "stalled", "The capture loop has stopped responding. Stop and restart recording."
        elif self.mode == "display_asleep":
            state, verdict = "paused", "Capture is paused while the display is asleep or locked."
        elif self.mode == "excluded":
            state, verdict = "paused", (
                "Capture is paused by your app/domain exclusion settings — the "
                "active window is on the exclusion list."
            )
        elif self.mode == "ignored_app":
            state, verdict = "paused", (
                "Capture is paused because an ignored app is visible. Tempo's own "
                "windows are ignored; switch to another app to resume capture."
            )
        elif self.mode == "starting":
            state, verdict = "starting", "Starting screen capture…"
        elif frame_age >= self.FRAME_STALL_SECONDS:
            state, verdict = "stalled", (
                "The capture loop is not grabbing frames from the display. "
                "Stop and restart recording."
            )
        elif self.last_frame is None or self.last_frame < self.mode_since:
            state, verdict = "starting", "Waiting for the first frame after capture resumes…"
        elif (self.last_input_seen is not None
              and self.last_input_seen > max(handled_at, self.mode_since)
              and handled_age >= self.FRAME_STALL_SECONDS):
            state, verdict = "stalled", (
                "Input events are arriving but are no longer being handled — "
                "this is a bug in Tempo, not a permission problem. Stop and restart recording."
            )
        elif pending_age is not None and pending_age >= self.PROCESSING_STALL_SECONDS:
            state, verdict = "stalled", (
                "Frames are arriving, but a capture has not finished processing "
                "for two minutes. Stop and restart recording."
            )
        elif self.result == "failed":
            state, verdict = "error", (
                f"The latest capture was not saved: {failure_reason}." if failure_reason
                else "The latest capture was not saved."
            )
            if failure_reason and "content filter blocked" in failure_reason:
                verdict += " Close the sensitive content on screen, or exclude that app from recording."
        elif (self.last_input_seen is None
              or now - self.last_input_seen >= self.INPUT_IDLE_SECONDS):
            state, verdict = "waiting", (
                "No recent mouse input. Move or click in another app to check capture. "
                "If this message remains, check Accessibility for the application "
                "that launched Tempo, then fully quit and relaunch it."
            )
        elif self.result == "unchanged":
            state, verdict = "idle", (
                "Frames are being grabbed but discarded as unchanged — the screen "
                "has not changed enough to be worth recording. This is normal on "
                "an idle display; use another app for a minute and re-check."
            )
        elif self.result == "no_frames":
            state, verdict = "error", (
                "The persist path is running but finds no buffered frames — the "
                "capture loop and the mouse trigger are out of step."
            )
        elif self.result is None:
            state, verdict = "waiting", "Frames are grabbed, but nothing has triggered a capture yet."

        return {
            "state": state,
            "verdict": verdict,
            "seconds_since_frame": age(self.last_frame),
            "seconds_since_input": age(self.last_input_seen),
            "oldest_pending_capture_seconds": round(max(0.0, pending_age), 1) if pending_age is not None else None,
        }
