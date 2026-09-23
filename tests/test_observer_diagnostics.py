"""A recovered capture must clear the failure shown in the Record view."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tempo.observers.screen import Screen
from tempo.observers.health import CaptureHealth


@pytest.fixture
def recent_capture():
    clock = SimpleNamespace(now=1000.0)
    health = CaptureHealth(clock=lambda: clock.now)
    health.set_mode("active")
    health.last_frame = health.last_input_seen = health.last_input_handled = clock.now
    health.result = "saved"
    return clock, health


def report(health, **kwargs):
    return health.report(running=True, worker_alive=True, listener_alive=True, **kwargs)


def test_frame_stall_after_success_and_recovery(recent_capture):
    clock, health = recent_capture
    assert report(health)["state"] == "healthy"
    clock.now += 31
    health.last_loop = health.last_input_seen = health.last_input_handled = clock.now
    assert report(health)["state"] == "stalled"
    assert "not grabbing frames" in report(health)["verdict"]

    health.last_frame = clock.now
    assert report(health)["state"] == "healthy"


def test_input_dropped_after_success_is_not_a_permission_diagnosis(recent_capture):
    clock, health = recent_capture
    clock.now += 31
    health.last_loop = health.last_frame = health.last_input_seen = clock.now
    diagnosis = report(health)
    assert diagnosis["state"] == "stalled"
    assert "bug in Tempo" in diagnosis["verdict"]


def test_quiet_mouse_is_not_evidence_of_missing_permission(recent_capture):
    clock, health = recent_capture
    clock.now += 121
    health.last_loop = health.last_frame = clock.now
    diagnosis = report(health)
    assert diagnosis["state"] == "waiting"
    assert "Move or click" in diagnosis["verdict"]
    assert "Grant Accessibility" not in diagnosis["verdict"]


def test_one_hung_capture_is_not_hidden_by_newer_successes(recent_capture):
    clock, health = recent_capture
    stuck = health.begin_capture()
    clock.now += 100
    newer = health.begin_capture()
    health.finish_capture(newer)
    health.result = "saved"
    clock.now += 21
    health.last_loop = health.last_frame = health.last_input_seen = health.last_input_handled = clock.now
    diagnosis = report(health)
    assert diagnosis["state"] == "stalled"
    assert diagnosis["oldest_pending_capture_seconds"] == 121
    health.finish_capture(stuck)
    assert report(health)["state"] == "healthy"


@pytest.mark.parametrize("mode", ["display_asleep", "excluded", "ignored_app"])
def test_pause_is_quiet_and_resume_gets_time_to_produce_frames(recent_capture, mode):
    clock, health = recent_capture
    pending = health.begin_capture()
    health.set_mode(mode)
    clock.now += 600
    health.last_loop = clock.now
    assert report(health)["state"] == "paused"

    health.set_mode("active")
    assert report(health)["state"] == "starting"
    health.last_frame = health.last_input_seen = health.last_input_handled = clock.now
    assert report(health)["oldest_pending_capture_seconds"] == 0
    health.finish_capture(pending)
    health.result = "saved"
    assert report(health)["state"] == "healthy"


def test_stale_pause_does_not_hide_a_hung_worker(recent_capture):
    clock, health = recent_capture
    health.set_mode("display_asleep")
    clock.now += 31
    assert report(health)["state"] == "stalled"


def test_startup_grace_and_no_input_prompt():
    clock = SimpleNamespace(now=1000.0)
    health = CaptureHealth(clock=lambda: clock.now)
    assert report(health)["state"] == "starting"
    health.set_mode("active")
    clock.now += 10
    health.last_loop = clock.now
    assert report(health)["state"] == "starting"
    health.last_frame = clock.now
    assert report(health)["state"] == "waiting"


def test_monotonic_progress_is_unaffected_by_wall_clock_changes(recent_capture, monkeypatch):
    _, health = recent_capture
    monkeypatch.setattr("time.time", lambda: -1000000.0)
    assert report(health)["state"] == "healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["display_asleep", "excluded", "ignored_app", "active"])
async def test_capture_worker_reports_current_mode_and_frame_progress(monkeypatch, mode):
    observer = object.__new__(Screen)
    observer.capture_health = CaptureHealth()
    observer._content_filter_enabled = False
    observer._transcription_task = SimpleNamespace(done=lambda: False)
    observer._running = True
    observer._poll_exclusions = AsyncMock()
    observer._exclusion_paused = mode == "excluded"
    observer.ignore_visible_app_names = ["Tempo"]
    observer._capture_skips_excluded = observer._capture_skips_visible_app = 0
    observer._frames_grabbed = 0
    observer.capture_fps = 5
    observer.log = MagicMock()
    observer.monitors = [{}]
    observer.frames = {}
    observer.k_history_frames = observer.k_future_frames = 1

    def stop_iteration(*_):
        raise asyncio.CancelledError

    async def stop_sleep(_):
        stop_iteration()

    observer.os_backend = SimpleNamespace(
        is_display_asleep=lambda: mode == "display_asleep",
        is_app_visible=lambda _: mode == "ignored_app",
        capture_screen=lambda _: object(),
    )
    observer.frame_taken_event = SimpleNamespace(set=stop_iteration)
    monkeypatch.setattr(asyncio, "sleep", stop_sleep)
    with pytest.raises(asyncio.CancelledError):
        await observer._worker()

    assert observer.capture_health.mode == mode
    if mode == "active":
        assert observer._frames_grabbed == 1
        assert observer.capture_health.last_frame is not None
    else:
        assert report(observer.capture_health)["state"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_pending_capture_is_removed_on_cancellation_or_error(fail):
    observer = object.__new__(Screen)
    observer.capture_health = CaptureHealth()
    observer._running = True
    observer.event_loop = asyncio.get_running_loop()
    observer.get_monitor_of_cursor = lambda *_: (0, {})
    observer._debounce_handles = {}
    observer._adaptive_debounce_seconds = 0
    observer._frame_semaphore = asyncio.Semaphore(1)
    observer._frame_tasks = set()
    observer.k_future_frames = 0
    observer.frames = {0: [object()]}
    observer._bundle_change_score = lambda *_: 1.0
    observer._frame_thumbnail = lambda _: object()
    observer.change_threshold = 0.04
    observer._persist_attempts = 0
    observer.log = MagicMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def persist(*_, **__):
        entered.set()
        await release.wait()
        raise OSError("disk unavailable")

    observer._persist_capture = persist
    await observer.mouse_event(0, 0)
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert len(observer.capture_health.pending) == 1
    task = next(iter(observer._frame_tasks))
    if fail:
        release.set()
        with pytest.raises(OSError):
            await task
        assert observer.capture_health.result == "failed"
        assert "OSError" in observer._last_persist_failure
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not observer.capture_health.pending


@pytest.mark.asyncio
async def test_successful_capture_clears_previous_failure():
    # Exercise persistence without starting OS listeners or an LLM worker.
    observer = object.__new__(Screen)
    observer.os_backend = SimpleNamespace(get_active_window=lambda: None)
    observer.ignore_visible_app_names = []
    observer.log = MagicMock()
    observer._save_frame = AsyncMock(return_value="recovered.jpg")
    observer._save_capture_metadata = AsyncMock()
    observer._content_filter_enabled = False
    observer._recent_process_times = []
    observer.debounce_seconds = 2.0
    observer._transcription_queue = asyncio.Queue()
    observer._last_persist_failure = "content filter blocked the frame (password_or_secret)"

    assert await observer._persist_capture([object()]) is True

    assert observer._last_persist_failure is None
    assert observer._transcription_queue.get_nowait() == "recovered.jpg"
