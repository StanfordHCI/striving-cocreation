"""Single-pipeline orchestration and lifecycle regression tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from tempo.context import UserContext
from tempo.db import Database
from tempo.pipelines.orchestrator import PipelineOrchestrator
from tempo.schemas import Update
from tempo.system import TempoSystem


def _make_mock_system(db: Database) -> TempoSystem:
    system = MagicMock(spec=TempoSystem)
    system.db = db
    system.provider = MagicMock()
    system.provider.chat_completion = AsyncMock(return_value="pong")
    system.stage1_provider = MagicMock()
    system.observer = MagicMock()
    system.observer._running = True
    system.observer.get_update = AsyncMock(return_value=None)
    system.observer.stop = AsyncMock()
    system.observer.restart = MagicMock()
    system.observer.set_user_context = MagicMock()
    system.debug = False
    return system


def _make_pipeline(default_db: Database, **kwargs):
    pipeline = MagicMock()
    pipeline.db = kwargs.get("db", default_db)
    pipeline.user_context = kwargs.get("user_context", "")
    pipeline.debug = False
    pipeline.debug_logger = MagicMock()
    pipeline.last_entity_produced_at = 0.0
    pipeline.get_status = AsyncMock(return_value={"observations": 0, "operations": 0})
    pipeline.save_state = MagicMock(
        return_value={"observation_count": 0, "buffer": {"items": []}}
    )
    pipeline.restore_state = MagicMock()
    pipeline.flush_buffers = AsyncMock()
    pipeline.pause = AsyncMock()
    pipeline.close = AsyncMock()
    pipeline.process_observation = AsyncMock()
    pipeline.check_periodic_jobs = AsyncMock()
    pipeline.trigger_goal_resynthesis = AsyncMock()
    return pipeline


@pytest_asyncio.fixture
async def orchestrator(db, tmp_path):
    system = _make_mock_system(db)
    orch = PipelineOrchestrator(
        system=system,
        state_path=str(tmp_path / "state.json"),
    )

    async def _noop_fts(session):
        return None

    with patch("tempo.pipelines.orchestrator.TempoPipeline") as pipeline_cls, patch(
        "tempo.pipelines.orchestrator.setup_fts", side_effect=_noop_fts
    ):
        pipeline_cls.side_effect = lambda **kwargs: _make_pipeline(db, **kwargs)
        await orch.setup(main_db=db)
    yield orch
    await orch.close()


@pytest_asyncio.fixture
async def context_orchestrator(db, tmp_path, request):
    """Use the real audit and operation pipeline with offline model responses."""
    context_path = tmp_path / "onboarding.json"
    context_path.write_text(json.dumps({"responses": {"work": "Preparing a field study"}}))
    system = _make_mock_system(db)
    system.provider.chat_completion.return_value = '{"transmit_data": true}'
    system.stage1_provider.chat_completion = AsyncMock(
        return_value='{"operations": [{"text": "Edited study notes"}]}'
    )
    orch = PipelineOrchestrator(
        system,
        user_context=UserContext(str(context_path)) if getattr(request, "param", True) else None,
        state_path=str(tmp_path / "state.json"),
    )
    await orch.setup(main_db=db)
    try:
        yield orch
    finally:
        await orch.close()


async def _process_one_update(orch, metadata):
    async def get_update():
        orch.running = False
        return Update(content="BASE_SCREEN_DESCRIPTION", content_type="input_text", metadata=metadata)

    orch.system.observer.get_update.side_effect = get_update
    await asyncio.wait_for(orch.start(), timeout=5)


class TestPersonalContext:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "metadata, expected",
        [
            ({"ctx_transcription": "CONTEXT_ENRICHED_DESCRIPTION"}, "CONTEXT_ENRICHED_DESCRIPTION"),
            ({}, "BASE_SCREEN_DESCRIPTION"),
            ({"ctx_transcription": ""}, "BASE_SCREEN_DESCRIPTION"),
            (None, "BASE_SCREEN_DESCRIPTION"),
        ],
        ids=["context-enriched", "older-capture", "empty-context-transcription", "no-metadata"],
    )
    async def test_audit_and_operations_use_context_observation(self, context_orchestrator, metadata, expected):
        orch = context_orchestrator
        orch.system.observer.set_user_context.assert_called_once_with(orch.user_context.render())

        await _process_one_update(orch, metadata)

        orch.system.provider.chat_completion.assert_awaited_once()
        audit_prompt = orch.system.provider.chat_completion.await_args.kwargs["messages"][0]["content"]
        orch.system.stage1_provider.chat_completion.assert_awaited_once()
        operation_prompt = orch.system.stage1_provider.chat_completion.await_args.kwargs["messages"][0]["content"]
        assert expected in audit_prompt
        assert expected in operation_prompt
        assert "Preparing a field study" in operation_prompt
        if expected == "CONTEXT_ENRICHED_DESCRIPTION":
            assert "BASE_SCREEN_DESCRIPTION" not in audit_prompt
            assert "BASE_SCREEN_DESCRIPTION" not in operation_prompt
        assert orch.pipeline.observation_count == 1
        assert orch.pipeline.error_count == 0
        assert orch.pipeline._op_buffer.size() == 1

    @pytest.mark.asyncio
    async def test_audit_can_block_context_enriched_observation(self, context_orchestrator):
        orch = context_orchestrator
        orch.system.provider.chat_completion.return_value = '{"transmit_data": false}'

        await _process_one_update(orch, {"ctx_transcription": "PRIVATE_CONTEXT_DESCRIPTION"})

        audit_prompt = orch.system.provider.chat_completion.await_args.kwargs["messages"][0]["content"]
        assert "PRIVATE_CONTEXT_DESCRIPTION" in audit_prompt
        assert orch.pipeline.audit_blocked_count == 1
        orch.system.stage1_provider.chat_completion.assert_not_awaited()
        assert orch.pipeline._op_buffer.is_empty()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context_orchestrator", [False], indirect=True)
    async def test_skipped_onboarding_uses_base_observation(self, context_orchestrator):
        orch = context_orchestrator
        orch.system.observer.set_user_context.assert_not_called()

        await _process_one_update(orch, {"ctx_transcription": "OLD_CONTEXT_DESCRIPTION"})

        for provider in (orch.system.provider, orch.system.stage1_provider):
            prompt = provider.chat_completion.await_args.kwargs["messages"][0]["content"]
            assert "BASE_SCREEN_DESCRIPTION" in prompt
            assert "OLD_CONTEXT_DESCRIPTION" not in prompt
            assert "Preparing a field study" not in prompt

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context_orchestrator", [False], indirect=True)
    async def test_adding_context_enables_personal_context_without_recreating_pipeline(self, context_orchestrator, tmp_path):
        orch = context_orchestrator
        pipeline = orch.pipeline
        context = UserContext(str(tmp_path / "onboarding.json"))
        orch.reload_user_context(context)
        orch.system.observer.set_user_context.assert_called_once_with(context.render())

        await _process_one_update(orch, {"ctx_transcription": "NEW_CONTEXT_DESCRIPTION"})

        assert orch.pipeline is pipeline
        audit_prompt = orch.system.provider.chat_completion.await_args.kwargs["messages"][0]["content"]
        operation_prompt = orch.system.stage1_provider.chat_completion.await_args.kwargs["messages"][0]["content"]
        assert "NEW_CONTEXT_DESCRIPTION" in audit_prompt
        assert "NEW_CONTEXT_DESCRIPTION" in operation_prompt
        assert "Preparing a field study" in operation_prompt


class TestSetup:
    @pytest.mark.asyncio
    async def test_setup_creates_one_pipeline_on_main_db(self, orchestrator, db):
        assert orchestrator.pipeline is not None
        assert orchestrator.pipeline.db is db

    @pytest.mark.asyncio
    async def test_setup_is_idempotent(self, orchestrator):
        original = orchestrator.pipeline
        await orchestrator.setup(main_db=orchestrator.system.db)
        assert orchestrator.pipeline is original

    @pytest.mark.asyncio
    async def test_start_requires_setup(self, db, tmp_path):
        orch = PipelineOrchestrator(
            _make_mock_system(db), state_path=str(tmp_path / "missing.json")
        )
        with pytest.raises(RuntimeError, match="setup"):
            await orch.start()


class TestStateSaveRestore:
    @pytest.mark.asyncio
    async def test_single_state_file_round_trip(self, orchestrator, tmp_path):
        orchestrator._started_at = datetime(2026, 1, 15, 10, 0, 0)
        orchestrator.save_state()
        state_path = tmp_path / "state.json"
        assert state_path.exists()
        assert state_path.stat().st_mode & 0o777 == 0o600
        assert json.loads(state_path.read_text())["_started_at"] == "2026-01-15T10:00:00"

        orchestrator._started_at = None
        orchestrator._restore_state()
        assert orchestrator._started_at == datetime(2026, 1, 15, 10, 0, 0)
        orchestrator.pipeline.restore_state.assert_called()

    @pytest.mark.asyncio
    async def test_missing_state_is_noop(self, db, tmp_path):
        orch = PipelineOrchestrator(
            _make_mock_system(db), state_path=str(tmp_path / "missing.json")
        )
        orch._restore_state()
        assert orch._started_at is None


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_runs_observation_loop(self, orchestrator):
        orchestrator._observation_loop = AsyncMock()
        await orchestrator.start()
        orchestrator._observation_loop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_pauses_pipeline_and_observer(self, orchestrator):
        orchestrator.running = True
        await orchestrator.stop()
        assert orchestrator.running is False
        orchestrator.pipeline.pause.assert_awaited_once()
        orchestrator.system.observer.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_cancels_awaits_and_drops_background_tasks(self, orchestrator):
        released = asyncio.Event()

        async def _forever():
            try:
                await asyncio.Future()
            finally:
                released.set()

        task = orchestrator._spawn_background(_forever(), "forever")
        await asyncio.sleep(0)
        await orchestrator.stop()

        assert task.cancelled()
        assert released.is_set()
        assert orchestrator._background_tasks == set()

    @pytest.mark.asyncio
    async def test_repeated_stop_does_not_retain_tasks(self, orchestrator):
        for _ in range(3):
            task = orchestrator._spawn_background(asyncio.sleep(60), "repeat")
            await asyncio.sleep(0)
            await orchestrator.stop()
            assert task.done()
            assert not orchestrator._background_tasks

    @pytest.mark.asyncio
    async def test_close_releases_pipeline_once(self, orchestrator):
        pipeline = orchestrator.pipeline
        await orchestrator.close()
        await orchestrator.close()
        pipeline.close.assert_awaited_once()
        assert orchestrator.pipeline is None


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_has_single_pipeline(self, orchestrator):
        orchestrator.running = True
        orchestrator._started_at = datetime.utcnow()
        status = await orchestrator.get_status()
        assert status["running"] is True
        assert status["pipeline"]["observations"] == 0
        assert "conditions" not in status

    @pytest.mark.asyncio
    async def test_status_handles_pipeline_error(self, orchestrator):
        orchestrator.pipeline.get_status = AsyncMock(side_effect=RuntimeError("db gone"))
        status = await orchestrator.get_status()
        assert status["pipeline"] == {"error": "db gone"}
