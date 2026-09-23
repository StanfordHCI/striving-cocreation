from __future__ import annotations
import base64
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
import asyncio
import mss
import numpy as np
from PIL import Image
from mss.screenshot import ScreenShot

from tempo.observers.observer import Observer
from tempo.observers.health import CaptureHealth
from tempo.observers import ocr, content_filter
from tempo.providers import (
    _is_transient_service_error,
    resolve_provider_api_base,
    resolve_provider_api_key,
)
from tempo.exclusions import ExclusionController

if TYPE_CHECKING:
    from tempo.os_backends import OSBackend, ActiveWindowInfo
from tempo.prompts.screen import SUMMARY_PROMPT, TRANSCRIPTION_PROMPT
from tempo.providers import create_provider
from tempo.schemas import Update
from tempo.utils import get_debug_logger, report_pipeline_error


class Screen(Observer):
    """Class representing a screen observer in the Tempo system.
    The observer captures screenshots of the user's screen and monitors mouse events to provide context-aware updates.
    It takes screenshots at a specified frame rate and processes them based on mouse interactions.
    If a mouse event is detected, it debounces the event and processes the relevant screenshots to generate a summary and transcription using a language model.
    To provide the necessary context, it keeps a history of screenshots before the event and waits for a few screenshots after the event.
    The amount of historical and future frames to consider can be configured using the `k_history_frames` and `k_future_frames` parameters.

    Args:
        name (Optional[str]): A custom name for the observer. If not provided, the class name will be used.
        backend (Optional[ScreenBackend]): The screen backend to use for capturing screen content and monitoring user interactions.
        screenshots_dir (str): Directory to store screenshots. Defaults to "~/.cache/tempo/screenshots".
        transcription_prompt (Optional[str]): Prompt to use for transcribing screen content. If not provided, a default prompt will be used.
        summary_prompt (Optional[str]): Prompt to use for summarizing screen activity. If not provided, a default prompt will be used.
        capture_fps (int): Frames per second to capture screenshots. Defaults to 10.
        debounce_seconds (float): Seconds to debounce mouse events. Defaults to 1.0.
        k_history_frames (int): Number of historical frames to keep for context. Defaults to 1.
        k_future_frames (int): Number of future frames to wait for after a mouse event. Defaults to 1.
        debug (bool): Whether to enable debug logging. Defaults to False.
        ignore_visible_app_names (Optional[List[str]]): App names to skip capture when visible.
        api_key (Optional[str]): API key for the language model provider. If not provided, environment variables will be checked.
        api_base (Optional[str]): API base URL for the language model provider. If not provided, environment variables will be checked.

    Attributes:
        update_queue (asyncio.Queue): Queue for sending updates to the main Tempo system.
        _name (str): The name of the observer.
        _running (bool): Flag indicating if the observer is currently running.
        _task (Optional[asyncio.Task]): Background task handle for the observer's worker.
    """

    def __init__(
        self,
        model_name: str = "gemini-3.8-flash",
        name: Optional[str] = None,
        os_backend: Optional["OSBackend"] = None,
        screenshots_dir: str = "~/.cache/tempo/screenshots",
        transcription_prompt: Optional[str] = None,
        summary_prompt: Optional[str] = None,
        capture_fps: int = 5,
        debounce_seconds: float = 2.0,
        k_history_frames: int = 2,
        k_future_frames: int = 2,
        change_threshold: float = 0.04,
        thumbnail_size: Tuple[int, int] = (96, 54),
        idle_threshold_seconds: float = 60.0,
        ignore_visible_app_names: Optional[List[str]] = None,
        debug: bool = False,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        gemini_vertexai: bool = False,
        gemini_vertexai_express: bool = False,
        vertex_project: Optional[str] = None,
        vertex_location: Optional[str] = None,
        enable_content_filter: bool = True,
        exclusion_controller: Optional[ExclusionController] = None,
    ) -> None:
        self.model_name = model_name
        self.screenshots_dir = os.path.abspath(os.path.expanduser(screenshots_dir))
        self.transcription_prompt = transcription_prompt or TRANSCRIPTION_PROMPT
        self.summary_prompt = summary_prompt or SUMMARY_PROMPT
        self.capture_fps = capture_fps
        self.debounce_seconds = debounce_seconds
        self._debounce_handles: Dict[int, asyncio.Handle] = {}
        self.k_history_frames = k_history_frames
        self.k_future_frames = k_future_frames
        self.change_threshold = change_threshold
        self.thumbnail_size = thumbnail_size
        self.idle_threshold_seconds = idle_threshold_seconds
        self.ignore_visible_app_names = [
            name for name in (ignore_visible_app_names or []) if name
        ]
        self.debug = debug
        self.api_key = api_key
        self.api_base = api_base
        self.user_context: str = ""

        self.frame_taken_event = asyncio.Event()
        self._frame_tasks: set[asyncio.Task] = set()
        self._mouse_event_tasks: set[asyncio.Task] = set()
        self._frame_semaphore = asyncio.Semaphore(5)  # max concurrent persist tasks
        self._last_emit_time: float = time.time()
        self._last_persist_time: float = time.time()
        self._last_mouse_event_time: float = time.time()
        self._watchdog_last_alert_time: float = 0.0
        self._persist_watchdog_last_alert_time: float = 0.0

        # Counted at each stage of the mouse path so a stall can be attributed
        # rather than guessed at. `_mouse_events_seen` increments in the
        # listener thread before any gate, so "the tap is silent" and "events
        # arrive but are dropped" are distinguishable — they need opposite fixes
        # (an OS permission vs. a bug in here) and look identical from outside.
        self._mouse_events_seen: int = 0
        self._mouse_events_dropped: int = 0
        self._mouse_events_handled: int = 0
        self._last_drop_reason: Optional[str] = None

        # The same idea applied to the capture path: a frame can be discarded
        # at several points for entirely legitimate reasons, and from outside
        # they are indistinguishable from a broken capture loop.
        self._frames_grabbed: int = 0
        self._capture_skips_visible_app: int = 0
        self._capture_skips_excluded: int = 0
        self._persist_attempts: int = 0
        self._persist_succeeded: int = 0
        self._persist_skipped_no_frames: int = 0
        self._persist_skipped_low_change: int = 0
        self._last_change_score: Optional[float] = None
        self._last_persist_failure: Optional[str] = None
        self.capture_health = CaptureHealth()
        # Counted separately from other rejections: this one deletes the frames
        # it just wrote, so a filter matching something permanently on screen
        # blocks every capture while leaving no trace on disk to explain why.
        self._content_filter_blocks: int = 0

        # Transcription worker: decoupled from capture
        self._transcription_queue: asyncio.Queue = asyncio.Queue()
        self._transcription_task: Optional[asyncio.Task] = None
        self._permanent_retry_counts: Dict[str, int] = {}  # jpeg_path → retry count
        self._last_transcription_activity: float = time.time()
        self._transcription_worker_started_once: bool = False

        self.provider = create_provider(
            model=model_name,
            api_key=api_key
            or os.getenv("SCREEN_LM_API_KEY")
            or resolve_provider_api_key(model_name),
            api_base=api_base
            or os.getenv("SCREEN_LM_API_BASE")
            or resolve_provider_api_base(model_name),
            gemini_vertexai=gemini_vertexai,
            gemini_vertexai_express=gemini_vertexai_express,
            vertex_project=vertex_project,
            vertex_location=vertex_location,
        )
        self._content_filter_enabled = enable_content_filter
        self.event_loop = asyncio.get_event_loop()

        os.makedirs(self.screenshots_dir, exist_ok=True)

        self.log = get_debug_logger(self, debug=debug)

        self.frames: Dict[int, ScreenShot] = {}
        self._last_triggered_thumb: Dict[int, np.ndarray] = {}
        self._last_screen_change_time = time.time()
        self._idle_reported = False
        self._exclusion_paused = False
        self._exclusion_controller = exclusion_controller or ExclusionController()
        self._next_exclusion_poll_at = 0.0
        # Adaptive throttling: track recent processing to detect high-change
        # scenarios (e.g. video calls) and back off automatically.
        self._recent_process_times: List[float] = []  # timestamps of recent _persist_capture calls
        self._adaptive_debounce_seconds = debounce_seconds  # dynamically adjusted

        self.monitors = []
        with mss.mss() as sct:
            self.monitors = sct.monitors[1:]

        # instantiate the backend from the provided class
        self.os_backend = os_backend
        self.os_backend.set_mouse_callbacks(
            on_mouse_move=lambda x, y: self._schedule_mouse_event(x, y),
            on_mouse_click=lambda x, y, btn, prsd: self._schedule_mouse_event(x, y, btn, prsd),
            on_mouse_scroll=lambda x, y, dx, dy: self._schedule_mouse_event(x, y, dx, dy),
        )
        super().__init__(name=name)

    def _schedule_mouse_event(self, x: int, y: int, *args, **kwargs) -> None:
        """Transfer a listener-thread event to an explicitly owned async task."""
        # Counted before anything can reject it: this is the ground truth that
        # the OS is delivering events to us at all.
        self._mouse_events_seen += 1
        self.capture_health.last_input_seen = time.monotonic()
        try:
            self.event_loop.call_soon_threadsafe(
                self._spawn_mouse_event, x, y, args, kwargs
            )
        except RuntimeError:
            # The event loop can already be closed during interpreter shutdown.
            self._mouse_events_dropped += 1
            self._last_drop_reason = "event loop closed"
            return

    def _spawn_mouse_event(self, x: int, y: int, args: tuple, kwargs: dict) -> None:
        if not getattr(self, "_running", False):
            self._mouse_events_dropped += 1
            self._last_drop_reason = "observer not running"
            return
        if self.event_loop.is_closed():
            self._mouse_events_dropped += 1
            self._last_drop_reason = "event loop closed"
            return
        self._mouse_events_handled += 1
        self.capture_health.last_input_handled = time.monotonic()
        task = self.event_loop.create_task(self.mouse_event(x, y, *args, **kwargs))
        self._mouse_event_tasks.add(task)
        task.add_done_callback(self._on_mouse_event_done)

    def _on_mouse_event_done(self, task: asyncio.Task) -> None:
        self._mouse_event_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self.log.error("Mouse event task failed: %s", exc, exc_info=exc)

    def set_user_context(self, ctx: str) -> None:
        """Set or update user context for context-enriched transcription.

        When non-empty, the transcription worker will produce a second
        transcription with the context injected and save it as a separate
        sidecar JSON (``*_ctx.json``).
        """
        self.user_context = ctx

    def get_monitor_of_cursor(self, x: int, y: int) -> Tuple[int, Optional[Dict]]:
        """Get the monitor dictionary that contains the given cursor position.

        Args:
            x (int): The x-coordinate of the cursor.
            y (int): The y-coordinate of the cursor.

        Returns:
            Optional[Dict]: The monitor dictionary if found, otherwise None.
        """
        for i, monitor in enumerate(self.monitors):
            if (
                monitor["left"] <= x < monitor["left"] + monitor["width"]
                and monitor["top"] <= y < monitor["top"] + monitor["height"]
            ):
                return i, monitor
        return -1, None

    async def _save_frame(self, frame: ScreenShot, tag: str) -> str:
        """Save a ScreenShot to a file.
        Args:
            frame (ScreenShot): The screenshot to save.
            tag (str): A tag to include in the filename.
        Returns:
            str: The path to the saved screenshot.
        """
        ts = f"{time.time():.5f}"
        path = os.path.join(self.screenshots_dir, f"{ts}_{tag}.jpg")
        await asyncio.to_thread(
            Image.frombytes("RGB", (frame.width, frame.height), frame.rgb).save,
            path,
            "JPEG",
            quality=70,
        )
        return path

    def _frame_thumbnail(self, frame: ScreenShot) -> np.ndarray:
        """Convert a frame to a grayscale thumbnail for change detection."""
        img = Image.frombytes("RGB", (frame.width, frame.height), frame.rgb)
        if self.thumbnail_size:
            img = img.resize(self.thumbnail_size, Image.BILINEAR)
        img = img.convert("L")
        return np.array(img, dtype=np.uint8)

    @staticmethod
    def _change_score(prev_thumb: np.ndarray, curr_thumb: np.ndarray) -> float:
        """Return mean absolute difference between two thumbnails.
        
        This matches the computation in screenshot_change_calibration.ipynb:
        mean_abs_diff uses np.float32 for the subtraction.
        """
        return float(np.mean(np.abs(prev_thumb.astype(np.float32) - curr_thumb.astype(np.float32))))

    def _bundle_change_score(self, monitor_idx: int, frames: List[ScreenShot]) -> float:
        """Compute the max change score for a bundle of frames."""
        if not frames:
            return 0.0
        thumbs = [self._frame_thumbnail(frame) for frame in frames]
        last_thumb = self._last_triggered_thumb.get(monitor_idx)
        if last_thumb is None:
            # Force the first batch through.
            return self.change_threshold + 1.0
        scores = [self._change_score(last_thumb, thumb) for thumb in thumbs]
        return max(scores) if scores else 0.0

    @staticmethod
    def _serialize(path: str) -> str:
        """Encode a ScreenShot to a base64 string.

        Args:
            frame (ScreenShot): The screenshot to encode.

        Returns:
            str: Base64 encoded string of the screenshot.
        """

        with open(path, "rb") as bff:
            return base64.b64encode(bff.read()).decode()

    async def _call_vision_api(self, prompt: str, image_paths: List[str]) -> str:
        """Call Vision API to analyze images.

        Args:
            prompt (str): Prompt to guide the analysis.
            image_paths (List[str]): List of image paths to analyze.

        Returns:
            str: Model's analysis of the images.
        """
        content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
            for encoded in (
                await asyncio.gather(
                    *[asyncio.to_thread(self._serialize, path) for path in image_paths]
                )
            )
        ]
        content.append({"type": "text", "text": prompt})
        rsp = await self.provider.vision_completion(
            messages=[{"role": "user", "content": content}],
            response_format={"type": "text"},
        )
        return rsp

    async def _save_transcription(
        self,
        screenshot_path: str,
        transcription: str,
        summary: str,
        app_context: Optional[dict],
        model_used: str,
        json_path_override: Optional[str] = None,
        ocr_text: Optional[str] = None,
    ) -> str:
        """Save transcription as sidecar JSON file next to screenshot.

        Args:
            json_path_override: If provided, use this path for the JSON
                file instead of deriving it from *screenshot_path*.
        """
        json_path = json_path_override or (screenshot_path.rsplit('.', 1)[0] + '.json')
        data = {
            "version": 1,
            "screenshot_path": os.path.basename(screenshot_path),
            "timestamp": datetime.now().isoformat(),
            "transcription": transcription,
            "summary": summary,
            "app_context": app_context,
            "model_used": model_used,
            "created_at": datetime.now().isoformat(),
            "ocr_text": ocr_text,
        }
        await asyncio.to_thread(
            lambda: Path(json_path).write_text(json.dumps(data, indent=2))
        )
        return json_path

    def _build_app_context_header(self, active_window: Optional["ActiveWindowInfo"]) -> str:
        """Build a context header with active window information."""
        if not active_window:
            return ""

        parts = []
        if active_window.app_name:
            parts.append(f"Application: {active_window.app_name}")
        if active_window.bundle_id:
            parts.append(f"Bundle ID: {active_window.bundle_id}")
        if active_window.window_title:
            parts.append(f"Window Title: {active_window.window_title}")

        if not parts:
            return ""

        return "Active Window Context (system-wide, may not be on this monitor):\n" + "\n".join(parts) + "\n\n"

    def _build_app_context_header_from_meta(self, meta: dict) -> str:
        """Build context header from saved .meta.json active_window dict."""
        aw = meta.get("active_window")
        if not aw:
            return ""
        parts = []
        if aw.get("app_name"):
            parts.append(f"Application: {aw['app_name']}")
        if aw.get("bundle_id"):
            parts.append(f"Bundle ID: {aw['bundle_id']}")
        if aw.get("window_title"):
            parts.append(f"Window Title: {aw['window_title']}")
        if not parts:
            return ""
        return "Active Window Context (system-wide, may not be on this monitor):\n" + "\n".join(parts) + "\n\n"

    async def _save_capture_metadata(
        self,
        screenshot_path: str,
        active_window: Optional["ActiveWindowInfo"],
        ocr_text: Optional[str],
        note: Optional[str],
    ) -> str:
        """Write a lightweight .meta.json sidecar with capture-time context."""
        active_window_dict = None
        if active_window:
            active_window_dict = {
                "app_name": active_window.app_name,
                "bundle_id": active_window.bundle_id,
                "window_title": active_window.window_title,
            }
        meta_path = screenshot_path.rsplit('.', 1)[0] + '.meta.json'
        data = {
            "version": 1,
            "timestamp": time.time(),
            "active_window": active_window_dict,
            "ocr_text": ocr_text,
            "user_context": self.user_context or "",
            "note": note,
        }
        await asyncio.to_thread(
            lambda: Path(meta_path).write_text(json.dumps(data, indent=2))
        )
        return meta_path

    async def _persist_capture(self, frames: List[ScreenShot], note: Optional[str] = None) -> bool:
        """Phase 1: Save frames to disk and queue for transcription. No LLM calls.

        Returns True if screenshots were persisted, False if skipped/filtered.
        """
        if not frames:
            self.log.debug("_persist_capture called with empty frames, skipping")
            self._last_persist_failure = "no frames passed to persist"
            return False

        # Get active window context
        active_window = await asyncio.to_thread(self.os_backend.get_active_window)

        # Skip if active window is ignored
        if active_window and self.ignore_visible_app_names:
            active_name = (active_window.app_name or "").lower()
            for ignored in self.ignore_visible_app_names:
                if ignored.lower() == active_name and active_window.window_title:
                    self.log.debug(
                        "Skipping _persist_capture — active window %s is ignored.",
                        active_window.app_name,
                    )
                    self._last_persist_failure = (
                        f"active window is an ignored app ({active_window.app_name})"
                    )
                    return False
        if self.ignore_visible_app_names:
            for ignored in self.ignore_visible_app_names:
                if await asyncio.to_thread(self.os_backend.is_app_visible, ignored):
                    self.log.debug(
                        "Skipping _persist_capture — %s became visible after frame capture.",
                        ignored,
                    )
                    self._last_persist_failure = (
                        f"ignored app became visible after capture ({ignored})"
                    )
                    return False

        self.log.debug(
            "Persisting screenshots. Active window: %s (%s)",
            active_window.app_name if active_window else "unknown",
            active_window.bundle_id if active_window else "unknown",
        )

        # Save frames as JPEGs, releasing each ScreenShot immediately after
        # encoding so raw pixel buffers (~12 MB each) don't linger in memory.
        image_paths = []
        for i in range(len(frames)):
            path = await self._save_frame(frames[i], str(i))
            image_paths.append(path)
            frames[i] = None  # release pixel data eagerly
        frames.clear()
        del frames

        # Content filter: OCR + PII detection before sending to cloud
        ocr_text = None
        if self._content_filter_enabled:
            ocr_text = await ocr.extract_text(image_paths[-1])
            if ocr_text:
                sensitive = content_filter.check_sensitive(ocr_text)
                if sensitive:
                    self.log.warning(
                        "Blocked screenshot — sensitive content detected: %s (matched: %s)",
                        sensitive["reason"],
                        sensitive["matched"][:40],
                    )
                    for p in image_paths:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    self._content_filter_blocks += 1
                    self._last_persist_failure = (
                        f"content filter blocked the frame ({sensitive['reason']})"
                    )
                    return False

        # Write .meta.json sidecar with capture-time context
        await self._save_capture_metadata(image_paths[-1], active_window, ocr_text, note)

        # Adaptive throttling
        now = time.time()
        self._recent_process_times.append(now)
        self._recent_process_times = [
            t for t in self._recent_process_times if now - t < 30.0
        ]
        recent_count = len(self._recent_process_times)
        if recent_count >= 6:
            self._adaptive_debounce_seconds = min(10.0, self.debounce_seconds * 5)
            self.log.debug(
                "High screen-change rate (%d/30s) — debounce increased to %.1fs",
                recent_count, self._adaptive_debounce_seconds,
            )
        elif recent_count >= 3:
            self._adaptive_debounce_seconds = min(6.0, self.debounce_seconds * 2)
        else:
            self._adaptive_debounce_seconds = self.debounce_seconds

        # Push to transcription queue (path string only — lightweight)
        await self._transcription_queue.put(image_paths[-1])
        self._last_persist_time = time.time()
        self._last_persist_failure = None
        return True

    def _scan_orphaned_captures(self) -> List[str]:
        """One-time startup scan: find .meta.json files without .json or .failed.

        Returns JPEG paths sorted by filename (timestamp order).
        """
        orphaned = []
        screenshots_dir = Path(self.screenshots_dir)
        for meta_path in sorted(screenshots_dir.glob("*.meta.json")):
            stem = meta_path.name.rsplit('.meta.json', 1)[0]
            jpeg_path = screenshots_dir / f"{stem}.jpg"
            json_path = screenshots_dir / f"{stem}.json"
            failed_path = screenshots_dir / f"{stem}.failed"
            if jpeg_path.exists() and not json_path.exists() and not failed_path.exists():
                orphaned.append(str(jpeg_path))
        return orphaned

    async def _mark_failed(self, jpeg_path: str, error_msg: str) -> None:
        """Write a .failed marker for a permanently broken screenshot."""
        failed_path = jpeg_path.rsplit('.', 1)[0] + '.failed'
        data = {
            "error": error_msg,
            "timestamp": datetime.now().isoformat(),
        }
        await asyncio.to_thread(
            lambda: Path(failed_path).write_text(json.dumps(data, indent=2))
        )
        self.log.warning("Marked screenshot as permanently failed: %s", jpeg_path)

    async def _transcription_worker(self) -> None:
        """Phase 2: Sequential transcription worker. Pops from queue, transcribes, emits.

        Transient errors (rate limit, quota, server down) → retry indefinitely with backoff.
        Permanent errors → retry up to _MAX_PERMANENT_RETRIES then write .failed marker.
        """
        # One-time startup: scan for orphaned .meta.json from previous crash
        # Skip on watchdog restarts — orphans are already in the queue.
        if not self._transcription_worker_started_once:
            self._transcription_worker_started_once = True
            orphaned = await asyncio.to_thread(self._scan_orphaned_captures)
            if orphaned:
                self.log.info("Found %d orphaned captures from previous run, re-queuing", len(orphaned))
                for path in orphaned:
                    await self._transcription_queue.put(path)

        backoff_seconds = 1.0

        while self._running:
            try:
                jpeg_path = await asyncio.wait_for(
                    self._transcription_queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            meta_path = jpeg_path.rsplit('.', 1)[0] + '.meta.json'
            try:
                meta = json.loads(Path(meta_path).read_text())
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                self.log.warning("Cannot read meta for %s: %s — skipping", jpeg_path, exc)
                continue

            # Check JPEG still exists (could have been cleaned up)
            if not os.path.exists(jpeg_path):
                self.log.debug("JPEG gone, skipping: %s", jpeg_path)
                # Clean up orphaned meta
                try:
                    os.remove(meta_path)
                except OSError:
                    pass
                continue

            try:
                app_context = self._build_app_context_header_from_meta(meta)
                saved_user_context = meta.get("user_context", "")
                ocr_text = meta.get("ocr_text")
                note = meta.get("note")

                # Build prompts
                base_prompt = app_context + self.transcription_prompt.replace("{user_context}", "")
                summary_prompt = app_context + self.summary_prompt

                # Base transcription
                transcription = await self._call_vision_api(base_prompt, [jpeg_path])

                # Summary
                summary = await self._call_vision_api(summary_prompt, [jpeg_path])

                # Context-enriched transcription (if user_context was set at capture time)
                ctx_transcription = None
                if saved_user_context:
                    ctx_prompt = app_context + self.transcription_prompt.replace(
                        "{user_context}", saved_user_context
                    )
                    ctx_transcription = await self._call_vision_api(ctx_prompt, [jpeg_path])

                # Save sidecar .json
                active_window_dict = meta.get("active_window")
                json_path = await self._save_transcription(
                    screenshot_path=jpeg_path,
                    transcription=transcription,
                    summary=summary,
                    app_context=active_window_dict,
                    model_used=self.model_name,
                    ocr_text=ocr_text,
                )

                # Save context-enriched sidecar
                ctx_json_path = None
                if ctx_transcription:
                    ctx_json_out = jpeg_path.rsplit('.', 1)[0] + '_ctx.json'
                    ctx_json_path = await self._save_transcription(
                        screenshot_path=jpeg_path,
                        transcription=ctx_transcription,
                        summary=summary,
                        app_context=active_window_dict,
                        model_used=self.model_name,
                        json_path_override=ctx_json_out,
                    )

                # Emit update
                if transcription or summary:
                    txt = ((transcription or "") + (summary or "")).strip()
                    if note:
                        txt = f"{txt}\n\n{note}".strip()
                    update_meta = {
                        "screenshot_path": jpeg_path,
                        "source_transcription": json_path,
                    }
                    if ctx_transcription:
                        ctx_txt = ((ctx_transcription or "") + (summary or "")).strip()
                        if note:
                            ctx_txt = f"{ctx_txt}\n\n{note}".strip()
                        update_meta["ctx_transcription"] = ctx_txt
                        update_meta["ctx_source_transcription"] = ctx_json_path
                    await self.update_queue.put(
                        Update(content=txt, content_type="input_text", metadata=update_meta)
                    )
                    self._last_emit_time = time.time()

                # Success — delete .meta.json and reset backoff
                try:
                    os.remove(meta_path)
                except OSError:
                    pass
                self._permanent_retry_counts.pop(jpeg_path, None)
                backoff_seconds = 1.0
                self._last_transcription_activity = time.time()

            except asyncio.CancelledError:
                # Re-queue so it's not lost (meta.json on disk is the safety net)
                try:
                    self._transcription_queue.put_nowait(jpeg_path)
                except asyncio.QueueFull:
                    pass  # .meta.json on disk → _scan_orphaned_captures recovers it
                break
            except Exception as exc:
                try:
                    if _is_transient_service_error(exc):
                        # Transient — retry indefinitely with backoff
                        self.log.warning(
                            "Transient error transcribing %s: %s — retrying in %.0fs",
                            os.path.basename(jpeg_path), exc, backoff_seconds,
                        )
                        await asyncio.sleep(backoff_seconds)
                        backoff_seconds = min(120.0, backoff_seconds * 2)
                        await self._transcription_queue.put(jpeg_path)
                    else:
                        # Permanent error — retry with cap
                        count = self._permanent_retry_counts.get(jpeg_path, 0) + 1
                        self._permanent_retry_counts[jpeg_path] = count
                        if count > self._MAX_PERMANENT_RETRIES:
                            self.log.error(
                                "Permanent failure transcribing %s after %d attempts: %s",
                                os.path.basename(jpeg_path), count, exc,
                            )
                            await self._mark_failed(jpeg_path, str(exc))
                            self._permanent_retry_counts.pop(jpeg_path, None)
                        else:
                            self.log.warning(
                                "Error transcribing %s (attempt %d/%d): %s — retrying",
                                os.path.basename(jpeg_path), count,
                                self._MAX_PERMANENT_RETRIES, exc,
                            )
                            await asyncio.sleep(2.0)
                            await self._transcription_queue.put(jpeg_path)
                except asyncio.CancelledError:
                    # Shutdown during error handling — re-queue synchronously
                    try:
                        self._transcription_queue.put_nowait(jpeg_path)
                    except asyncio.QueueFull:
                        pass  # .meta.json on disk → recovered on next startup
                    break

    async def mouse_event(self, x: int, y: int, *args, **kwargs) -> None:
        """Handle mouse events using the backend's event loop."""
        if not self._running:
            return
        self._last_mouse_event_time = time.time()

        idx, _ = self.get_monitor_of_cursor(x, y)
        if idx < 0:
            return

        def debounce_callback():
            if not self._running:
                return

            async def process_frames():
                # Semaphore only guards the fast frame-reading section.
                # _persist_capture (JPEG save, OCR, disk I/O) runs outside
                # so slots free up quickly and don't bottleneck on I/O.
                async with self._frame_semaphore:
                    for _ in range(self.k_future_frames):
                        await self.frame_taken_event.wait()
                        self.frame_taken_event.clear()
                    if idx not in self.frames or len(self.frames[idx]) == 0:
                        self._persist_skipped_no_frames += 1
                        self.capture_health.result = "no_frames"
                        return
                    frames = list(self.frames[idx])
                    score = self._bundle_change_score(idx, frames)
                    self._last_change_score = round(float(score), 4)
                    last_thumb = self._frame_thumbnail(frames[-1])
                    if score < self.change_threshold:
                        idle_seconds = time.time() - self._last_screen_change_time
                        if idle_seconds >= self.idle_threshold_seconds and not self._idle_reported:
                            note = (
                                f"[Idle] Screen on monitor {idx} unchanged for "
                                f"{int(idle_seconds)} seconds. Capturing context."
                            )
                        else:
                            self.log.debug(
                                "Skipping vision call for monitor %s (change score %.2f < %.2f)",
                                idx,
                                score,
                                self.change_threshold,
                            )
                            self._persist_skipped_low_change += 1
                            self.capture_health.result = "unchanged"
                            self.frames[idx].clear()
                            return
                    else:
                        note = None
                    self.frames[idx].clear()
                self._persist_attempts += 1

                # Slow I/O runs outside the semaphore.
                # frames list still holds raw ScreenShot references here;
                # _persist_capture releases them eagerly after JPEG encoding.
                persisted = await self._persist_capture(frames, note=note)
                self.capture_health.result = "saved" if persisted else "failed"
                del frames  # ensure closure doesn't pin pixel data after persist
                if persisted:
                    self._persist_succeeded += 1
                    self._last_triggered_thumb[idx] = last_thumb
                    self._last_screen_change_time = time.time()
                    if note:  # was idle capture
                        self._idle_reported = True
                    else:
                        self._idle_reported = False

            async def wait_for_future_frames_persist_capture():
                token = self.capture_health.begin_capture()
                try:
                    await process_frames()
                except Exception as exc:
                    self._last_persist_failure = f"capture processing failed ({type(exc).__name__})"
                    self.capture_health.result = "failed"
                    raise
                finally:
                    self.capture_health.finish_capture(token)

            task = asyncio.create_task(wait_for_future_frames_persist_capture())
            self._frame_tasks.add(task)
            task.add_done_callback(lambda t: self._on_frame_task_done(t))

        prev = self._debounce_handles.get(idx)
        if prev is not None:
            prev.cancel()
        self._debounce_handles[idx] = self.event_loop.call_later(
            self._adaptive_debounce_seconds, debounce_callback
        )

    def _on_frame_task_done(self, task: asyncio.Task) -> None:
        self._frame_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self.log.error("Frame processing task failed: %s", exc, exc_info=exc)

    async def stop(self) -> None:
        """Stop the screen observer, cancelling all in-flight work."""
        # Set flag FIRST so straggling listener-thread callbacks become no-ops.
        self._running = False
        self._exclusion_paused = False

        mouse_tasks = list(self._mouse_event_tasks)
        for task in mouse_tasks:
            task.cancel()
        if mouse_tasks:
            await asyncio.gather(*mouse_tasks, return_exceptions=True)
        self._mouse_event_tasks.clear()

        # Cancel all per-monitor debounce callbacks
        for handle in self._debounce_handles.values():
            handle.cancel()
        self._debounce_handles.clear()

        # Cancel all in-flight frame processing tasks
        frame_tasks = list(self._frame_tasks)
        for task in frame_tasks:
            task.cancel()
        if frame_tasks:
            await asyncio.gather(*frame_tasks, return_exceptions=True)
        self._frame_tasks.clear()

        # Cancel transcription worker (un-transcribed JPEGs survive on disk)
        if self._transcription_task is not None:
            self._transcription_task.cancel()
            try:
                await self._transcription_task
            except asyncio.CancelledError:
                pass
            self._transcription_task = None

        # Stop OS-level mouse listener thread
        if self.os_backend is not None:
            self.os_backend.stop_listeners()

        # Cancel the screenshot capture worker (base class)
        await super().stop()

        # Raw screenshots and queued updates can be large; release them on pause.
        self.frames.clear()
        self._last_triggered_thumb.clear()
        self._recent_process_times.clear()

    async def close(self) -> None:
        """Stop capture and release providers and OS capture resources."""
        await self.stop()
        try:
            await self.provider.close()
        finally:
            if self.os_backend is not None:
                self.os_backend.close()

    def _restart_mouse_listener(self) -> None:
        """Stop and recreate the pynput mouse listener thread.

        Safe to call from the worker or from restart().  Blocks briefly
        while the old listener thread joins (up to 2 s).
        """
        self.os_backend.stop_listeners()

        self.os_backend.set_mouse_callbacks(
            on_mouse_move=lambda x, y: self._schedule_mouse_event(x, y),
            on_mouse_click=lambda x, y, btn, prsd: self._schedule_mouse_event(x, y, btn, prsd),
            on_mouse_scroll=lambda x, y, dx, dy: self._schedule_mouse_event(x, y, dx, dy),
        )

    def restart(self) -> None:
        """Restart the screen observer after a stop (for resume)."""
        self.event_loop = asyncio.get_event_loop()

        # Reset watchdog timers so we don't false-alarm on resume
        self._last_emit_time = time.time()
        self._last_persist_time = time.time()
        self._last_mouse_event_time = time.time()
        self._last_transcription_activity = time.time()
        self._watchdog_last_alert_time = 0.0
        self._next_exclusion_poll_at = 0.0

        # Re-register mouse callbacks to start a new listener thread
        self._restart_mouse_listener()

        # Restart the screenshot capture worker (base class)
        super().restart()

    # Screen capture resilience constants
    _CAPTURE_RETRY_BASE_SECONDS = 0.5
    _CAPTURE_RETRY_MAX_SECONDS = 60.0  # back off up to 60s
    _CAPTURE_FAILURE_PATIENCE_SECONDS = 300  # crash after 5 min of continuous failure
    _DISPLAY_SLEEP_POLL_SECONDS = 5.0
    _EXCLUSION_POLL_SECONDS = 2.0
    # Watchdog: alert if no screenshots emitted while worker is capturing
    _WATCHDOG_NO_EMIT_SECONDS = 120  # 2 minutes with zero emits triggers alert
    _WATCHDOG_REPEAT_SECONDS = 300   # re-alert every 5 minutes if still stuck
    # Watchdog: alert if no screenshots persisted to disk for a long time
    _PERSIST_WATCHDOG_SECONDS = 600   # 10 min with no screenshots saved → local alert
    _PERSIST_WATCHDOG_REPEAT_SECONDS = 600  # re-alert every 10 min
    # Transcription worker resilience
    _MAX_PERMANENT_RETRIES = 5  # give up on non-transient errors after this many attempts
    _TRANSCRIPTION_WATCHDOG_SECONDS = 300  # 5 min; must exceed max backoff (120s) + API call time

    async def _poll_exclusions(self) -> None:
        """Refresh capture policy from the active bundle ID and browser URL."""
        now = time.monotonic()
        if now < self._next_exclusion_poll_at:
            return
        self._next_exclusion_poll_at = now + self._EXCLUSION_POLL_SECONDS
        try:
            active_window = await asyncio.to_thread(self.os_backend.get_active_window)
            paused = await asyncio.to_thread(
                self._exclusion_controller.should_pause, active_window
            )
        except Exception as exc:
            # Keep the last decision on a transient platform/settings failure.
            self.log.warning("Unable to refresh exclusion state: %s", exc)
            return
        if paused != self._exclusion_paused:
            bundle_id = getattr(active_window, "bundle_id", None)
            self.log.info(
                "%s capture for exclusion policy (bundle=%s)",
                "Pausing" if paused else "Resuming",
                bundle_id or "unknown",
            )
        self._exclusion_paused = paused

    async def _worker(self) -> None:
        """Background worker to continuously capture screenshots."""
        # Bind the loop that is actually running us. The constructor captures a
        # loop too, but `asyncio.get_event_loop()` there returns whatever loop
        # happens to be current at construction — which is not necessarily the
        # one the worker ends up on. Posting to the wrong loop fails silently:
        # `call_soon_threadsafe` succeeds, the wakeup is written, and the
        # callback simply never runs because nobody is driving that loop.
        self.event_loop = asyncio.get_running_loop()
        self.capture_health.set_mode("starting")
        self.capture_health.last_loop = time.monotonic()

        # Compile OCR helper on first run (non-blocking, fail-open)
        if self._content_filter_enabled:
            try:
                ok = await ocr.init()
                if ok:
                    self.log.info("Content filter enabled (OCR available)")
                else:
                    self.log.info("Content filter: OCR unavailable, filter disabled")
                    self._content_filter_enabled = False
            except Exception as exc:
                self.log.warning("Content filter init failed: %s", exc)
                self._content_filter_enabled = False

        # Start the decoupled transcription worker only when one is not
        # already alive (the base observer may restart this capture worker).
        if self._transcription_task is None or self._transcription_task.done():
            if self._transcription_task is not None and not self._transcription_task.cancelled():
                self._transcription_task.exception()
            self._transcription_task = asyncio.create_task(self._transcription_worker())

        consecutive_failures = 0  # only incremented for non-sleep failures
        failure_started_at: float | None = None  # when continuous failures began
        display_was_asleep = False

        while self._running:
            self.capture_health.last_loop = time.monotonic()
            await self._poll_exclusions()

            # ── If display is asleep/locked, wait patiently (no retry limit) ──
            if await asyncio.to_thread(self.os_backend.is_display_asleep):
                self.capture_health.set_mode("display_asleep")
                if not display_was_asleep:
                    self.log.info("Display is asleep, pausing capture until wake")
                    print("  [SLEEP] Display asleep — pausing screen capture")
                    display_was_asleep = True
                await asyncio.sleep(self._DISPLAY_SLEEP_POLL_SECONDS)
                continue

            if display_was_asleep:
                self.log.info("Display woke up, resuming capture")
                print("  [WAKE] Display awake — resuming screen capture")
                display_was_asleep = False
                # Reset failure timer so sleep duration doesn't count against patience
                consecutive_failures = 0
                failure_started_at = None

            # Skip capture when the active bundle or browser domain is excluded.
            if self._exclusion_paused:
                self.capture_health.set_mode("excluded")
                self._capture_skips_excluded += 1
                await asyncio.sleep(0.5)
                continue

            ts_start = time.time()

            if self.ignore_visible_app_names:
                skip_capture = False
                for app_name in self.ignore_visible_app_names:
                    if await asyncio.to_thread(self.os_backend.is_app_visible, app_name):
                        self.log.debug("Skipping capture because %s is visible.", app_name)
                        skip_capture = True
                        break
                if skip_capture:
                    self.capture_health.set_mode("ignored_app")
                    self._capture_skips_visible_app += 1
                    await asyncio.sleep(max(0, (1 / self.capture_fps)))
                    continue

            self.capture_health.set_mode("active")
            capture_ok = True
            for i, monitor in enumerate(self.monitors):
                try:
                    frame = await asyncio.to_thread(self.os_backend.capture_screen, monitor)
                except Exception as exc:
                    # Check if the display went to sleep between our check and the capture
                    if await asyncio.to_thread(self.os_backend.is_display_asleep):
                        self.capture_health.set_mode("display_asleep")
                        self.log.info("Display went to sleep during capture, pausing")
                        print("  [SLEEP] Display asleep — pausing screen capture")
                        # Reset failure timer so sleep duration doesn't count
                        consecutive_failures = 0
                        failure_started_at = None
                        capture_ok = False
                        break

                    # Real error (display is awake but capture failed)
                    consecutive_failures += 1
                    now = time.time()
                    if failure_started_at is None:
                        failure_started_at = now
                    if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                        self.log.warning(
                            "Screen capture failed (attempt %d, %.0fs): %s",
                            consecutive_failures,
                            now - failure_started_at,
                            exc,
                        )
                    # Hybrid: back off for 5 min, then crash to get fresh mss handle
                    if now - failure_started_at >= self._CAPTURE_FAILURE_PATIENCE_SECONDS:
                        self.log.error(
                            "Screen capture failed for %.0fs (%d attempts), crashing for fresh handle",
                            now - failure_started_at,
                            consecutive_failures,
                        )
                        raise
                    delay = min(
                        self._CAPTURE_RETRY_MAX_SECONDS,
                        self._CAPTURE_RETRY_BASE_SECONDS * (2 ** min(consecutive_failures - 1, 7)),
                    )
                    await asyncio.sleep(delay)
                    capture_ok = False
                    break  # skip remaining monitors this iteration

                if i not in self.frames:
                    self.frames[i] = []
                self.frames[i].append(frame)
                self._frames_grabbed += 1
                self.capture_health.last_frame = time.monotonic()
                if len(self.frames[i]) > self.k_history_frames + self.k_future_frames:
                    self.frames[i].pop(0)
                self.frame_taken_event.set()

            if capture_ok:
                if consecutive_failures > 0:
                    elapsed = time.time() - failure_started_at if failure_started_at is not None else 0.0
                    self.log.info(
                        "Screen capture recovered after %d failures (%.0fs)",
                        consecutive_failures, elapsed,
                    )
                    print("  [WAKE] Screen capture resumed")
                consecutive_failures = 0
                failure_started_at = None

                # ── Watchdog: detect silent failures (e.g. pynput not delivering events) ──
                # Check whether the mouse listener thread is alive and whether
                # mouse events are actually being received.  Previous approach
                # used _last_persist_time which false-alarmed when the screen
                # was static (change score below threshold → no persists).
                now_ts = time.time()
                listener_alive = (
                    self.os_backend.listener_task is not None
                    and self.os_backend.listener_task.is_alive()
                )
                since_mouse = now_ts - self._last_mouse_event_time
                since_alert = now_ts - self._watchdog_last_alert_time
                watchdog_triggered = (
                    (not listener_alive or since_mouse >= self._WATCHDOG_NO_EMIT_SECONDS)
                    and since_alert >= self._WATCHDOG_REPEAT_SECONDS
                )
                if watchdog_triggered:
                    # Report where the path actually broke. The stage counters
                    # separate "the OS is not delivering events" (a permission
                    # problem, outside this process) from "events arrive and we
                    # drop them" (a bug in here) — previously both were blamed
                    # on Accessibility, which sends people to fix the wrong
                    # thing when the tap was working all along.
                    if not listener_alive:
                        reason = "listener thread is dead"
                    elif self._mouse_events_seen == 0:
                        reason = (
                            f"no mouse events reached the listener in "
                            f"{int(since_mouse)}s — the OS is not delivering "
                            f"them (check Accessibility for the app that "
                            f"launched Tempo)"
                        )
                    else:
                        reason = (
                            f"listener received {self._mouse_events_seen} events "
                            f"but none were handled for {int(since_mouse)}s "
                            f"(dropped={self._mouse_events_dropped}, "
                            f"last drop reason: {self._last_drop_reason or 'unknown'})"
                        )
                    msg = f"Observer watchdog: {reason}"
                    print(f"  [WARNING] {msg}")
                    report_pipeline_error(
                        "observer_watchdog",
                        RuntimeError(msg),
                        {"seconds_since_mouse_event": int(since_mouse),
                         "listener_alive": listener_alive},
                    )
                    self._watchdog_last_alert_time = now_ts

                    # Auto-recovery: restart the pynput mouse listener
                    try:
                        print("  [RECOVERY] Restarting mouse listener...")
                        await asyncio.to_thread(self._restart_mouse_listener)
                        self._last_mouse_event_time = now_ts
                        self._last_persist_time = now_ts
                        print("  [RECOVERY] Mouse listener restarted")
                    except Exception as exc:
                        self.log.error("Mouse listener restart failed: %s", exc)

                # ── Watchdog: detect stuck/crashed transcription worker ──
                if self._transcription_task is not None:
                    txn_task_done = self._transcription_task.done()
                    txn_stale = (
                        time.time() - self._last_transcription_activity
                        > self._TRANSCRIPTION_WATCHDOG_SECONDS
                    )
                    txn_stuck = (
                        not txn_task_done
                        and not self._transcription_queue.empty()
                        and txn_stale
                    )

                    if txn_task_done or txn_stuck:
                        # Diagnose
                        if txn_task_done:
                            txn_exc = (
                                self._transcription_task.exception()
                                if not self._transcription_task.cancelled()
                                else None
                            )
                            reason = (
                                f"transcription worker crashed: {txn_exc}"
                                if txn_exc
                                else "transcription worker exited unexpectedly"
                            )
                        else:
                            reason = (
                                f"transcription worker stuck: no activity for "
                                f"{int(time.time() - self._last_transcription_activity)}s "
                                f"with {self._transcription_queue.qsize()} items queued"
                            )

                        msg = f"Transcription watchdog: {reason}"
                        self.log.warning(msg)
                        print(f"  [WARNING] {msg}")
                        report_pipeline_error(
                            "transcription_watchdog",
                            RuntimeError(msg),
                            {
                                "task_done": txn_task_done,
                                "queue_size": self._transcription_queue.qsize(),
                                "seconds_since_activity": int(
                                    time.time() - self._last_transcription_activity
                                ),
                            },
                        )

                        # Recovery never overlaps two workers. A stuck task may
                        # hold an HTTP client or image batch, so keep its handle
                        # and wait for cancellation to complete on a later pass.
                        old_task = self._transcription_task
                        if not old_task.done():
                            old_task.cancel()
                            self._last_transcription_activity = time.time()
                            print("  [RECOVERY] Waiting for old transcription worker to stop")
                        else:
                            # Retrieve any exception before replacing the handle.
                            if not old_task.cancelled():
                                old_task.exception()
                            self._last_transcription_activity = time.time()
                            self._transcription_worker_started_once = False
                            self._transcription_task = asyncio.create_task(
                                self._transcription_worker()
                            )
                            print("  [RECOVERY] Transcription worker restarted")
                            report_pipeline_error(
                                "transcription_watchdog_recovery",
                                RuntimeError("Transcription worker restarted successfully"),
                                {
                                    "previous_reason": reason,
                                    "queue_size": self._transcription_queue.qsize(),
                                },
                            )

                # ── Watchdog: no screenshots persisted to disk ──
                # Fires when display is awake and not exclusion-paused but
                # no screenshots have been saved for _PERSIST_WATCHDOG_SECONDS.
                if not display_was_asleep and not self._exclusion_paused:
                    since_persist = now_ts - self._last_persist_time
                    since_persist_alert = now_ts - self._persist_watchdog_last_alert_time
                    if (
                        since_persist >= self._PERSIST_WATCHDOG_SECONDS
                        and since_persist_alert >= self._PERSIST_WATCHDOG_REPEAT_SECONDS
                    ):
                        msg = (
                            f"Screenshot liveness watchdog: no screenshots persisted "
                            f"for {int(since_persist)}s while display is awake"
                        )
                        self.log.error(msg)
                        print(f"  [WARNING] {msg}")
                        report_pipeline_error(
                            "screenshot_liveness_watchdog",
                            RuntimeError(msg),
                            {
                                "seconds_since_persist": int(since_persist),
                                "display_asleep": display_was_asleep,
                                "exclusion_paused": self._exclusion_paused,
                            },
                        )
                        self._persist_watchdog_last_alert_time = now_ts

                ts_end = time.time()
                dt = ts_end - ts_start
                await asyncio.sleep(max(0, (1 / self.capture_fps) - dt))
