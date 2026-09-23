# server.py

"""
FastAPI server for Tempo Electron app.

Provides REST API and WebSocket endpoints for controlling Tempo
and querying the behavioral graph.
"""

from __future__ import annotations
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime
from pathlib import Path
import asyncio
import io
import json
import os
import logging
import re
import time
from uuid import UUID

from tempo.system import TempoSystem
from tempo.pipelines.orchestrator import PipelineOrchestrator
from tempo.db import Database
from tempo.store import Store
from tempo.queries import QueryInterface
from tempo.models import EntityType, Entity
from tempo.hierarchy import HierarchyError, HierarchyService, SplitPart
from tempo.hierarchy_compile import CompileError, HierarchyCompiler, HierarchyEdits
from tempo.exclusions import ExclusionSettingsStore, discover_installed_apps
from tempo.providers import (
    create_provider,
    provider_name_for_model,
    resolve_provider_api_base,
    resolve_provider_api_key,
)
from tempo.assistant_chat import ChatSendRequest
from tempo.assistant_connection import describe_connection
from tempo.assistant_service import (
    AssistantError, AssistantRefineRequest, AssistantService,
    AssistantSettingsRequest, FeedbackRequest,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ENV_DEFAULT_PATH = os.path.join(_PROJECT_ROOT, ".env.default")
DATA_DIRECTORY = Path(os.environ.get("TEMPO_DATA_DIR", "~/.cache/tempo")).expanduser()

# Configuration written from the UI belongs in the user's data directory, not
# beside the installed package: for a pip/pipx install `_PROJECT_ROOT` is
# `site-packages`, which is wiped on upgrade and is not the user's to own.
# The project-root copy is still *read* so a development checkout keeps working.
ENV_FILE_PATH = str(DATA_DIRECTORY / ".env")
ENV_PROJECT_PATH = os.path.join(_PROJECT_ROOT, ".env")
DEFAULT_SCREENSHOTS_DIR = str(DATA_DIRECTORY / "screenshots")
UI_DIRECTORY = Path(__file__).with_name("ui")
exclusion_settings = ExclusionSettingsStore(DATA_DIRECTORY / "settings.json")


def configure_data_directory(data_directory: str | Path) -> Path:
    """Point every server-owned runtime path at one Tempo data directory."""
    global DATA_DIRECTORY, ENV_FILE_PATH, DEFAULT_SCREENSHOTS_DIR, exclusion_settings

    resolved = Path(data_directory).expanduser().resolve()
    DATA_DIRECTORY = resolved
    ENV_FILE_PATH = str(resolved / ".env")
    DEFAULT_SCREENSHOTS_DIR = str(resolved / "screenshots")
    exclusion_settings = ExclusionSettingsStore(resolved / "settings.json")
    return resolved


def _load_env_from_file(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key or key in os.environ:
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                value = value.replace("\\n", "\n")
                os.environ[key] = value
    except OSError as exc:
        logger.warning("Failed to load env file %s: %s", path, exc)


def _utc_iso(dt: datetime | None) -> str | None:
    """Format a naive-UTC datetime as ISO 8601 with ``Z`` suffix.

    All datetimes stored in the database are UTC but lack timezone info.
    Appending ``Z`` tells the frontend (JavaScript ``new Date()``) to
    interpret the value as UTC so it can convert to the user's local
    timezone for display.
    """
    if dt is None:
        return None
    return dt.isoformat() + "Z"


def _escape_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _safe_local_path(path_value: object, allowed_suffixes: set[str]) -> Optional[Path]:
    """Resolve a path only when it stays inside Tempo's screenshot directory."""
    if not path_value:
        return None
    try:
        root = Path(DEFAULT_SCREENSHOTS_DIR).expanduser().resolve()
        candidate = Path(str(path_value)).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved.suffix.lower() not in allowed_suffixes or not resolved.is_file():
        return None
    return resolved


def _safe_screenshot_path(path_value: object) -> Optional[Path]:
    return _safe_local_path(path_value, {".jpg", ".jpeg", ".png"})


def _resolve_screenshot_path_from_transcription(json_path_str: Optional[str]) -> Optional[str]:
    if not json_path_str:
        return None
    try:
        json_path = _safe_local_path(json_path_str, {".json"})
    except (TypeError, ValueError):
        return None
    if json_path is None:
        return None
    try:
        data = json.loads(json_path.read_text())
    except Exception:
        return None
    screenshot_name = data.get("screenshot_path")
    if screenshot_name:
        candidate = _safe_screenshot_path(json_path.parent / screenshot_name)
        if candidate:
            return str(candidate)
    candidate = _safe_screenshot_path(json_path.with_suffix(".jpg"))
    if candidate:
        return str(candidate)
    return None


def _resolve_screenshot_path_from_metadata(metadata: Optional[dict]) -> Optional[str]:
    if not metadata or not isinstance(metadata, dict):
        return None
    screenshot_path = metadata.get("screenshot_path") or metadata.get("screenshot")
    source_transcription = metadata.get("source_transcription")
    if screenshot_path:
        try:
            candidate = Path(screenshot_path).expanduser()
        except Exception:
            candidate = None
        if candidate:
            if not candidate.is_absolute():
                if source_transcription:
                    try:
                        base_dir = Path(source_transcription).expanduser().parent
                        relative_candidate = _safe_screenshot_path(base_dir / screenshot_path)
                        if relative_candidate:
                            return str(relative_candidate)
                    except Exception:
                        pass
                fallback_dir = Path("~/.cache/tempo/screenshots").expanduser()
                relative_candidate = _safe_screenshot_path(fallback_dir / screenshot_path)
                if relative_candidate:
                    return str(relative_candidate)
            else:
                safe_candidate = _safe_screenshot_path(candidate)
                if safe_candidate:
                    return str(safe_candidate)
    return _resolve_screenshot_path_from_transcription(source_transcription)


def _extract_epoch_from_screenshot_name(path: Path) -> Optional[float]:
    try:
        stem = path.stem
        epoch_str = stem.split("_")[0]
        return float(epoch_str)
    except (ValueError, IndexError, AttributeError):
        return None


def _find_nearest_screenshot_for_timestamp(
    timestamp: Optional[datetime],
    max_seconds: int = 180,
) -> Optional[str]:
    if not timestamp:
        return None
    screenshots_dir = Path(DEFAULT_SCREENSHOTS_DIR).expanduser()
    if not screenshots_dir.exists():
        return None
    target = timestamp.timestamp()
    best_path = None
    best_diff = None
    for path in screenshots_dir.glob("*.jpg"):
        epoch = _extract_epoch_from_screenshot_name(path)
        if epoch is None:
            continue
        diff = abs(epoch - target)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_path = path
    if best_path and best_diff is not None and best_diff <= max_seconds:
        safe_path = _safe_screenshot_path(best_path)
        if safe_path:
            return str(safe_path)
    return None


def _load_env_lines(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readlines()
    except OSError as exc:
        logger.warning("Failed to read env file %s: %s", path, exc)
        return []


def _set_env_line(lines: List[str], key: str, value: str) -> List[str]:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    rendered = f"{key}={_escape_env_value(value)}\n"
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = rendered
            return lines
    lines.append(rendered)
    return lines


def _persist_env_config(
    *,
    model_name: str,
    model_name_set: bool,
    api_key: Optional[str],
    api_base: Optional[str],
    gemini_vertexai_express: bool = False,
) -> None:
    updates: Dict[str, str] = {}
    if model_name_set:
        updates["MODEL_NAME"] = model_name

    provider = provider_name_for_model(model_name)

    if gemini_vertexai_express:
        updates["GEMINI_VERTEXAI_EXPRESS"] = "true"

    if api_key:
        if gemini_vertexai_express:
            updates["VERTEX_EXPRESS_API_KEY"] = api_key
        elif provider == "gemini":
            updates["GOOGLE_API_KEY"] = api_key
        elif provider == "anthropic":
            updates["ANTHROPIC_API_KEY"] = api_key
        else:
            updates["TEMPO_LM_API_KEY"] = api_key

    if api_base and provider != "gemini":
        base_variable = "ANTHROPIC_API_BASE" if provider == "anthropic" else "TEMPO_LM_API_BASE"
        updates[base_variable] = api_base

    if not updates:
        return

    # Update os.environ so subsequent reads within this process use the new values
    for key, value in updates.items():
        os.environ[key] = value

    lines = _load_env_lines(ENV_FILE_PATH)
    for key, value in updates.items():
        lines = _set_env_line(lines, key, value)

    temp_path = f"{ENV_FILE_PATH}.tmp"
    try:
        os.makedirs(os.path.dirname(ENV_FILE_PATH) or ".", exist_ok=True)
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, ENV_FILE_PATH)
        os.chmod(ENV_FILE_PATH, 0o600)
    except OSError as exc:
        logger.warning("Failed to write env file %s: %s", ENV_FILE_PATH, exc)


# _load_env_from_file skips keys already in os.environ, so the first file to
# define a key wins. Order: user data directory (written by the UI), then a
# development checkout's .env, then the committed defaults.
_load_env_from_file(ENV_FILE_PATH)
_load_env_from_file(ENV_PROJECT_PATH)
_load_env_from_file(ENV_DEFAULT_PATH)

# Resolve relative GOOGLE_APPLICATION_CREDENTIALS to absolute (SDK needs it).
_gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
if _gac and not os.path.isabs(_gac):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.join(_PROJECT_ROOT, _gac)

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    yield
    await shutdown_resources()


app = FastAPI(
    title="Tempo API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=app_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)(:\d+)?",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
)


def _origin_allowed(origin: Optional[str]) -> bool:
    if origin is None or origin == "null":
        return True
    return bool(
        re.fullmatch(r"https?://(127\.0\.0\.1|localhost)(:\d+)?", origin)
    )


@app.middleware("http")
async def enforce_local_origin(request: Request, call_next):
    """Reject browser requests from non-loopback origins before route handling."""
    if not _origin_allowed(request.headers.get("origin")):
        return JSONResponse({"error": "Origin not allowed"}, status_code=403)
    return await call_next(request)

# Global state
system: Optional[TempoSystem] = None
orchestrator: Optional[PipelineOrchestrator] = None
orchestrator_task: Optional[asyncio.Task] = None
status = {"running": False, "error": None, "message": "Not started"}
websocket_clients: List[WebSocket] = []
current_db_name: str = "tempo.db"
current_config: Optional[dict] = None  # Store current running configuration
assistant_service: AssistantService | None = None
_assistant_init_lock = asyncio.Lock()


async def shutdown_resources() -> None:
    """Cancel background work and close long-lived application resources."""
    global system, orchestrator, orchestrator_task, assistant_service, _assistant_init_lock

    if assistant_service is not None:
        await assistant_service.close()
        assistant_service = None
    _assistant_init_lock = asyncio.Lock()

    if orchestrator is not None:
        await orchestrator.close()
        orchestrator = None
    if orchestrator_task is not None:
        if not orchestrator_task.done():
            orchestrator_task.cancel()
        await asyncio.gather(orchestrator_task, return_exceptions=True)
        orchestrator_task = None

    if system is not None:
        await system.cleanup(flush_buffer=False)
        system = None


def _log_task_failure(task: asyncio.Task, name: str) -> None:
    """Log exceptions from background tasks instead of silently losing them."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Background task '%s' failed: %s", name, exc, exc_info=exc)



class StartRequest(BaseModel):
    model_name: Optional[str] = None
    platform: str = "macos"
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    gemini_vertexai: Optional[bool] = None
    gemini_vertexai_express: Optional[bool] = None
    vertex_project: Optional[str] = None
    vertex_location: Optional[str] = None
    debug: bool = False
    buffer_time_threshold: float = 300.0
    buffer_max_size: int = 50
    db_name: str = "tempo.db"
    user_name: str = "the user"


class GraphQuery(BaseModel):
    entity_type: Optional[str] = None  # 'operation', 'action', 'activity'
    since: Optional[str] = None  # ISO datetime
    until: Optional[str] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    include_relations: bool = True


class EntityUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    text: Optional[str] = Field(default=None, max_length=10_000)
    locked: Optional[bool] = None


class EntityReparentRequest(BaseModel):
    new_parent_id: int
    expected_revision: int = Field(ge=0)


class EntityMergeRequest(BaseModel):
    ids: List[int] = Field(min_length=2)
    text: Optional[str] = Field(default=None, max_length=10_000)
    expected_revisions: Dict[int, int]


class EntitySplitRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    into: List[SplitPart] = Field(min_length=2)


class EntityDeleteRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    reparent_children_to: Optional[int] = None


class EntityCreateRequest(BaseModel):
    type: str
    text: str = Field(min_length=1, max_length=10_000)
    parent_id: Optional[int] = None


class ExclusionSettingsRequest(BaseModel):
    apps: List[str] = Field(default_factory=list, max_length=10_000)
    domains: List[str] = Field(default_factory=list, max_length=10_000)



async def broadcast_status():
    """Broadcast status to all connected WebSocket clients."""
    message = {"type": "status", "data": status}
    disconnected = []
    for client in websocket_clients:
        try:
            await client.send_json(message)
        except Exception:
            disconnected.append(client)

    # Remove disconnected clients
    for client in disconnected:
        if client in websocket_clients:
            websocket_clients.remove(client)


async def broadcast_hierarchy_update(action: str, data: dict) -> None:
    """Notify connected clients after a hierarchy transaction commits."""
    if assistant_service is not None:
        assistant_service.notify_context_change()
    disconnected = []
    message = {"type": "hierarchy", "action": action, "data": data}
    for client in list(websocket_clients):
        try:
            await client.send_json(message)
        except Exception:
            disconnected.append(client)
    for client in disconnected:
        if client in websocket_clients:
            websocket_clients.remove(client)


async def _init_system(
    model_name: Optional[str] = None,
    platform: str = "macos",
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    gemini_vertexai: Optional[bool] = None,
    gemini_vertexai_express: Optional[bool] = None,
    vertex_project: Optional[str] = None,
    vertex_location: Optional[str] = None,
    debug: bool = False,
    buffer_time_threshold: float = 300.0,
    buffer_max_size: int = 50,
    db_name: str = "tempo.db",
) -> None:
    """Initialize the TempoSystem."""
    global system, current_db_name, current_config

    current_db_name = db_name

    # Auto-detect platform
    import platform as platform_module
    if not platform or platform == "macos":
        system_platform = platform_module.system().lower()
        if system_platform == "darwin":
            detected_platform = "macos"
        elif system_platform == "linux":
            detected_platform = "gnome"
        else:
            detected_platform = platform or "macos"
    else:
        detected_platform = platform

    # Resolve Vertex modes and debug from env if not explicitly set.
    if gemini_vertexai is None:
        gemini_vertexai = os.environ.get("GEMINI_VERTEXAI", "").lower() in ("true", "1", "yes")
    if gemini_vertexai_express is None:
        gemini_vertexai_express = os.environ.get("GEMINI_VERTEXAI_EXPRESS", "").lower() in ("true", "1", "yes")
    if gemini_vertexai and gemini_vertexai_express:
        raise ValueError("GEMINI_VERTEXAI and GEMINI_VERTEXAI_EXPRESS cannot both be enabled")
    if not debug:
        debug = os.environ.get("TEMPO_DEBUG", "").lower() in ("true", "1", "yes")

    # Vertex AI requires a real service-account keyfile. If GEMINI_VERTEXAI
    # is set but GOOGLE_APPLICATION_CREDENTIALS points at a missing file, the
    # consumer Gemini path (with GOOGLE_API_KEY) is the safe fallback —
    # otherwise every LLM call hangs in google-auth retries.
    if gemini_vertexai:
        gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if gac and not os.path.exists(gac):
            logger.warning(
                "GEMINI_VERTEXAI=true but GOOGLE_APPLICATION_CREDENTIALS file "
                "not found: %s. Falling back to consumer Gemini API "
                "(set GOOGLE_API_KEY).",
                gac,
            )
            gemini_vertexai = False
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

    # Model name
    resolved_model = model_name or os.environ.get("MODEL_NAME", "gemini-3.8-flash")

    # API configuration
    if gemini_vertexai_express:
        resolved_api_base = None
        resolved_api_key = (
            api_key
            or os.environ.get("VERTEX_EXPRESS_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not resolved_api_key:
            raise ValueError("VERTEX_EXPRESS_API_KEY is required for Vertex AI Express Mode")
    else:
        resolved_api_base = resolve_provider_api_base(resolved_model, api_base)
        resolved_api_key = resolve_provider_api_key(resolved_model, api_key)

    key_source = "explicit" if api_key else "env"
    print(f"  _init_system: model={resolved_model}, api_key_source={key_source}")

    # Store config metadata
    api_key_source = "none"
    if api_key and api_key.strip():
        api_key_source = "explicit"
    elif resolved_api_key:
        stored_api_key = None
        api_key_source = "env"

    current_config = {
        "model_name": resolved_model,
        "platform": detected_platform,
        "api_key_configured": bool(resolved_api_key),
        "api_base": resolved_api_base,
        "gemini_vertexai": gemini_vertexai,
        "gemini_vertexai_express": gemini_vertexai_express,
        "vertex_project": vertex_project,
        "vertex_location": vertex_location,
        "debug": debug,
        "buffer_time_threshold": buffer_time_threshold,
        "buffer_max_size": buffer_max_size,
        "db_name": db_name,
        "api_key_source": api_key_source,
        "api_base_source": "explicit" if api_base else ("env" if resolved_api_base else "none"),
        "model_name_source": "explicit" if model_name else ("env" if os.environ.get("MODEL_NAME") else "default"),
    }

    model_name_set = bool(model_name and model_name.strip())
    api_key_set = api_key.strip() if api_key else None
    api_base_set = api_base.strip() if api_base else None

    _persist_env_config(
        model_name=resolved_model,
        model_name_set=model_name_set,
        api_key=api_key_set,
        api_base=api_base_set,
        gemini_vertexai_express=gemini_vertexai_express,
    )

    system = TempoSystem(
        model_name=resolved_model,
        platform=detected_platform,
        debug=debug,
        api_key=resolved_api_key,
        api_base=resolved_api_base,
        gemini_vertexai=gemini_vertexai,
        gemini_vertexai_express=gemini_vertexai_express,
        vertex_project=vertex_project or os.environ.get("GOOGLE_CLOUD_PROJECT"),
        vertex_location=vertex_location or os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        buffer_time_threshold=buffer_time_threshold,
        buffer_max_size=buffer_max_size,
        db_name=db_name,
        data_directory=str(DATA_DIRECTORY),
        ignore_visible_app_names=["Electron", "Tempo"],
    )

    await system.setup()


@app.post("/api/start")
async def start_tempo(request: StartRequest):
    """Start the Tempo production pipeline."""
    global system, orchestrator, orchestrator_task, status

    if status["running"]:
        return JSONResponse({"error": "Tempo is already running"}, status_code=400)

    try:
        status = {"running": False, "error": None, "message": "Starting..."}
        await broadcast_status()

        # Check if provider config changed (api_key, api_base, model)
        # If so, force a full reinit instead of resuming with stale provider
        provider_config_changed = False
        if system is not None:
            if request.api_key and request.api_key.strip() and request.api_key.strip() != system.api_key:
                provider_config_changed = True
            if request.api_base and request.api_base.strip() and request.api_base.strip() != system.api_base:
                provider_config_changed = True
            if request.model_name and request.model_name.strip() and request.model_name.strip() != system.model_name:
                provider_config_changed = True
            if request.gemini_vertexai is not None and request.gemini_vertexai != getattr(system, 'gemini_vertexai', False):
                provider_config_changed = True
            if request.gemini_vertexai_express is not None and request.gemini_vertexai_express != getattr(system, 'gemini_vertexai_express', False):
                provider_config_changed = True
            if request.vertex_project and request.vertex_project != getattr(system, 'vertex_project', None):
                provider_config_changed = True
            if request.vertex_location and request.vertex_location != getattr(system, 'vertex_location', None):
                provider_config_changed = True

        if provider_config_changed:
            print(f"  Provider config changed — forcing full reinit (model={request.model_name or system.model_name})")
            if orchestrator is not None:
                try:
                    await orchestrator.close()
                except Exception:
                    pass
                orchestrator = None
            if orchestrator_task is not None:
                orchestrator_task.cancel()
                await asyncio.gather(orchestrator_task, return_exceptions=True)
                orchestrator_task = None
            if system is not None:
                try:
                    await system.cleanup(flush_buffer=False)
                except Exception:
                    pass
            system = None

        # Resume a paused production pipeline.
        if (
            orchestrator is not None
            and not orchestrator.running
        ):
            logger.info("Resuming paused production pipeline")

            # Update debug flag on resume (user may have toggled it)
            system.debug = request.debug
            if orchestrator.pipeline is not None:
                orchestrator.pipeline.debug = request.debug
                if orchestrator.pipeline.debug_logger:
                    orchestrator.pipeline.debug_logger.enabled = request.debug

            async def _resume():
                try:
                    await orchestrator.start()
                except Exception:
                    logger.exception("Orchestrator error")
                    status["running"] = False
                    status["error"] = "orchestrator_failed"
                    status["message"] = "Tempo stopped after an internal error"
                    await broadcast_status()

            orchestrator_task = asyncio.create_task(_resume(), name="orchestrator")
            orchestrator_task.add_done_callback(lambda t: _log_task_failure(t, "orchestrator"))

            status = {
                "running": True,
                "error": None,
                "message": "Running",
                "started_at": datetime.utcnow().isoformat() + "Z",
            }
            await broadcast_status()
            return {"status": "resumed", "message": "Tempo resumed"}

        # Fresh start: initialize system if needed
        if system is None:
            await _init_system(
                model_name=request.model_name,
                platform=request.platform,
                api_key=request.api_key,
                api_base=request.api_base,
                gemini_vertexai=request.gemini_vertexai,
                gemini_vertexai_express=request.gemini_vertexai_express,
                vertex_project=request.vertex_project,
                vertex_location=request.vertex_location,
                debug=request.debug,
                buffer_time_threshold=request.buffer_time_threshold,
                buffer_max_size=request.buffer_max_size,
                db_name=request.db_name,
            )
        else:
            # System already exists (from previous run) — update debug flag
            system.debug = request.debug

        user_name = request.user_name

        # Close any previous orchestrator before creating a fresh pipeline.
        if orchestrator is not None:
            try:
                await orchestrator.close()
            except Exception:
                pass
            orchestrator = None

        # Create unified orchestrator. Onboarding answers, when the user has
        # given them, are rendered into a context block that every inference
        # stage sees — the difference between guessing from screen activity
        # alone and knowing what the person is actually trying to do.
        stored_onboarding = _load_onboarding()
        if stored_onboarding.get("responses"):
            user_name = stored_onboarding.get("user_name") or user_name
        orchestrator = PipelineOrchestrator(
            system=system,
            user_context=_build_user_context(stored_onboarding, user_name),
        )
        await orchestrator.setup(
            user_name=user_name,
            main_db=system.db,
        )

        async def _run():
            try:
                await orchestrator.start()
            except Exception:
                logger.exception("Orchestrator error")
                status["running"] = False
                status["error"] = "orchestrator_failed"
                status["message"] = "Tempo stopped after an internal error"
                await broadcast_status()

        orchestrator_task = asyncio.create_task(_run(), name="orchestrator")
        orchestrator_task.add_done_callback(lambda t: _log_task_failure(t, "orchestrator"))

        status = {
            "running": True,
            "error": None,
            "message": "Running",
            "started_at": datetime.utcnow().isoformat() + "Z",
        }
        await broadcast_status()

        return {"status": "started", "message": "Tempo is now running"}
    except Exception as e:
        logger.exception("Failed to start Tempo")
        status = {"running": False, "error": "startup_failed", "message": "Unable to start Tempo"}
        await broadcast_status()
        return JSONResponse({"error": "Unable to start Tempo"}, status_code=500)


@app.post("/api/stop")
async def stop_tempo():
    """Pause the production pipeline while keeping it available for resume."""
    global orchestrator, orchestrator_task, status, current_config

    if not status["running"] and not (orchestrator and orchestrator.running):
        return JSONResponse({"error": "Tempo is not running"}, status_code=400)

    try:
        status = {**status, "message": "Stopping..."}
        await broadcast_status()

        if orchestrator and orchestrator.running:
            await orchestrator.stop()
        if orchestrator_task is not None:
            if not orchestrator_task.done():
                orchestrator_task.cancel()
            await asyncio.gather(orchestrator_task, return_exceptions=True)
            orchestrator_task = None

        status = {"running": False, "error": None, "message": "Stopped", "started_at": None}
        current_config = None
        await broadcast_status()

        return {"status": "stopped", "message": "Tempo has been paused"}
    except Exception as e:
        logger.exception("Failed to stop Tempo")
        status = {"running": False, "error": "shutdown_failed", "message": "Unable to stop Tempo"}
        await broadcast_status()
        return JSONResponse({"error": "Unable to stop Tempo"}, status_code=500)


@app.get("/api/apps/installed")
async def get_installed_apps():
    """List launchable desktop applications from platform-native sources."""
    return await asyncio.to_thread(discover_installed_apps)


@app.get("/api/settings/exclusions")
async def get_exclusion_settings():
    settings = await asyncio.to_thread(exclusion_settings.load)
    return settings.to_dict()


@app.put("/api/settings/exclusions")
async def put_exclusion_settings(request: ExclusionSettingsRequest):
    settings = await asyncio.to_thread(
        exclusion_settings.save, request.apps, request.domains
    )
    return settings.to_dict()


@app.get("/api/status")
async def get_status():
    """Get current Tempo status."""
    return status


@app.get("/api/config")
async def get_config():
    """Return non-secret metadata for the running configuration."""
    global current_config
    if not status["running"] or not current_config:
        return JSONResponse({"error": "Tempo is not running"}, status_code=400)
    
    safe_config = {
        key: value
        for key, value in current_config.items()
        if key
        in {
            "model_name",
            "model_name_source",
            "platform",
            "api_key_configured",
            "api_key_source",
            "api_base_source",
            "gemini_vertexai",
            "gemini_vertexai_express",
            "debug",
            "buffer_time_threshold",
            "buffer_max_size",
            "db_name",
        }
    }
    safe_config["api_base_configured"] = bool(current_config.get("api_base"))
    safe_config["api_key_configured"] = bool(
        current_config.get("api_key_configured") or current_config.get("api_key")
    )
    return safe_config


@app.get("/api/config/env")
async def get_env_config():
    """Report provider configuration presence without returning values."""
    model_name = os.environ.get("MODEL_NAME", "gemini-3.8-flash")
    express_mode = os.environ.get("GEMINI_VERTEXAI_EXPRESS", "").lower() in ("true", "1", "yes")
    provider = "vertex-express" if express_mode else provider_name_for_model(model_name)
    return {
        "model_name": model_name,
        "provider": provider,
        "api_key_configured": bool(
            os.environ.get("TEMPO_LM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("VERTEX_EXPRESS_API_KEY")
        ),
        "api_base_configured": bool(
            os.environ.get("TEMPO_LM_API_BASE")
            or os.environ.get("OPENAI_API_BASE")
            or os.environ.get("ANTHROPIC_API_BASE")
            or os.environ.get("ANTHROPIC_BASE_URL")
        ),
        "vertex_credentials_configured": bool(
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Database Discovery & Selection Endpoints (Multi-DB Support)
# ─────────────────────────────────────────────────────────────────────────────


def get_query_database() -> Database:
    """Open the single production database used by all read endpoints."""
    return Database(db_name=current_db_name, data_directory=str(DATA_DIRECTORY))


@app.post("/api/graph/entities")
async def get_entities(query: GraphQuery):
    """Get entities with optional filtering."""
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            store = Store(session)
            since = datetime.fromisoformat(query.since) if query.since else None
            until = datetime.fromisoformat(query.until) if query.until else None
            
            total = None
            if query.entity_type:
                entities = await store.entities.get_by_type(
                    query.entity_type,
                    since=since,
                    until=until,
                    limit=query.limit,
                    offset=query.offset,
                )
                if query.limit:
                    total = await store.entities.count_by_type(
                        query.entity_type, since=since, until=until
                    )
            else:
                # Get all types
                entities = []
                for etype in [EntityType.OPERATION, EntityType.ACTION, EntityType.ACTIVITY, EntityType.GOAL, EntityType.PROPOSITION]:
                    ents = await store.entities.get_by_type(
                        etype, since=since, until=until, limit=query.limit or 100
                    )
                    entities.extend(ents)
            
            # Serialize entities
            result = []
            for entity in entities:
                meta = entity.metadata_dict or {}
                confidence = meta.get("goal_confidence") or meta.get("confidence") or meta.get("score")
                entity_data = {
                    "id": entity.id,
                    "type": entity.type,
                    "text": entity.text,
                    "confidence": confidence,
                    "timestamp_start": _utc_iso(entity.timestamp_start),
                    "timestamp_end": _utc_iso(entity.timestamp_end),
                    "created_at": _utc_iso(entity.created_at),
                    "metadata": meta,
                }

                if query.include_relations:
                    # Get relations
                    outgoing = await store.relations.get_by_source(entity.id)
                    incoming = await store.relations.get_by_target(entity.id)
                    entity_data["relations"] = {
                        "outgoing": [
                            {
                                "id": r.id,
                                "target_id": r.target_id,
                                "type": r.relation_type,
                                "subtype": r.relation_subtype,
                                "confidence": r.confidence,
                                "metadata": r.metadata_dict,
                            }
                            for r in outgoing
                        ],
                        "incoming": [
                            {
                                "id": r.id,
                                "source_id": r.source_id,
                                "type": r.relation_type,
                                "subtype": r.relation_subtype,
                                "confidence": r.confidence,
                                "metadata": r.metadata_dict,
                            }
                            for r in incoming
                        ],
                    }
                
                result.append(entity_data)
            
            response = {"entities": result, "count": len(result)}
            if total is not None:
                response["total"] = total
            return response
    except Exception:
        logger.exception("Failed to get entities")
        return JSONResponse({"error": "Failed to get entities"}, status_code=500)
    finally:
        await db.close()


@app.get("/api/graph/entity/{entity_id}")
async def get_entity(entity_id: int):
    """Get a single entity with full relations."""
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            store = Store(session)
            entity = await store.entities.get_with_relations(entity_id)
            if not entity:
                return JSONResponse({"error": "Entity not found"}, status_code=404)
            
            # Collect all related entity IDs to fetch their text
            related_ids = set()
            for r in entity.outgoing_relations:
                related_ids.add(r.target_id)
            for r in entity.incoming_relations:
                related_ids.add(r.source_id)
            
            # Fetch text for all related entities
            related_texts = {}
            related_types = {}
            for rid in related_ids:
                try:
                    related_entity = await store.entities.get(rid)
                    if related_entity:
                        related_texts[rid] = related_entity.text
                        related_types[rid] = related_entity.type
                except Exception:
                    pass
            
            # Get confidence from metadata if available
            metadata = entity.metadata_dict or {}
            confidence = metadata.get("goal_confidence") or metadata.get("confidence") or metadata.get("score")
            
            return {
                "id": entity.id,
                "type": entity.type,
                "text": entity.text,
                "confidence": confidence,
                "timestamp_start": _utc_iso(entity.timestamp_start),
                "timestamp_end": _utc_iso(entity.timestamp_end),
                "created_at": _utc_iso(entity.created_at),
                "metadata": metadata,
                "relations": {
                    "outgoing": [
                        {
                            "id": r.id,
                            "target_id": r.target_id,
                            "target_text": related_texts.get(r.target_id, f"Entity #{r.target_id}"),
                            "target_type": related_types.get(r.target_id),
                            "type": r.relation_type,
                            "subtype": r.relation_subtype,
                            "confidence": r.confidence,
                            "metadata": r.metadata_dict,
                        }
                        for r in entity.outgoing_relations
                    ],
                    "incoming": [
                        {
                            "id": r.id,
                            "source_id": r.source_id,
                            "source_text": related_texts.get(r.source_id, f"Entity #{r.source_id}"),
                            "source_type": related_types.get(r.source_id),
                            "type": r.relation_type,
                            "subtype": r.relation_subtype,
                            "confidence": r.confidence,
                            "metadata": r.metadata_dict,
                        }
                        for r in entity.incoming_relations
                    ],
                },
            }
    except Exception:
        logger.exception("Failed to get entity")
        return JSONResponse({"error": "Failed to get entity"}, status_code=500)
    finally:
        await db.close()


@app.get("/api/graph/entity/{entity_id}/screenshot")
async def get_entity_screenshot(
    entity_id: int,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
    index: int = 0,
):
    """Return a screenshot associated with an entity (if available).

    For operations: resolves from metadata screenshot_path.
    For propositions/goals: uses observation_screenshots from metadata.
    For actions/activities: traverses down to constituent operations.
    The ``index`` param selects which screenshot (0 = first/most recent).
    """
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            store = Store(session)
            entity = await store.entities.get_with_relations(entity_id)
            if not entity:
                raise HTTPException(status_code=404, detail="Entity not found")

            screenshot_path = None

            if entity.type == EntityType.OPERATION:
                meta = entity.metadata_dict or {}
                screenshot_path = _resolve_screenshot_path_from_metadata(meta)
                if not screenshot_path:
                    screenshot_path = _find_nearest_screenshot_for_timestamp(
                        entity.timestamp_start
                    )
            else:
                # Propositions, goals, actions, activities: check observation_screenshots
                meta = entity.metadata_dict or {}
                screenshots = meta.get("observation_screenshots") or meta.get("screenshot_paths") or []
                if screenshots and index < len(screenshots):
                    entry = screenshots[index]
                    # Support both basenames and full paths
                    candidate = _safe_screenshot_path(entry)
                    if candidate:
                        screenshot_path = str(candidate)
                # Fallback: try resolving like an operation
                if not screenshot_path:
                    screenshot_path = _resolve_screenshot_path_from_metadata(meta)
                if not screenshot_path:
                    screenshot_path = _find_nearest_screenshot_for_timestamp(
                        entity.timestamp_start
                    )

            if not screenshot_path:
                raise HTTPException(status_code=404, detail="Screenshot not found")
    finally:
        await db.close()

    path = _safe_screenshot_path(screenshot_path)
    if path is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    safe_width = max_width if max_width and max_width > 0 else None
    safe_height = max_height if max_height and max_height > 0 else None
    if safe_width or safe_height:
        try:
            from PIL import Image
            with Image.open(path) as img:
                img = img.convert("RGB")
                target = (safe_width or img.width, safe_height or img.height)
                img.thumbnail(target)
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=85, optimize=True)
            buffer.seek(0)
            return StreamingResponse(
                buffer,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            logger.warning("Failed to resize screenshot for %s: %s", path, exc)

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/graph/entity/{entity_id}/screenshots")
async def get_entity_screenshots(entity_id: int):
    """Return all screenshots associated with an entity.

    For operations: returns the single screenshot.
    For propositions/goals: returns all observation_screenshots from metadata.
    For actions/activities: traverses relations down to operations.
    """
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            store = Store(session)
            entity = await store.entities.get_with_relations(entity_id)
            if not entity:
                raise HTTPException(status_code=404, detail="Entity not found")

            results = []

            if entity.type == EntityType.OPERATION:
                meta = entity.metadata_dict or {}
                path = _resolve_screenshot_path_from_metadata(meta)
                if not path:
                    path = _find_nearest_screenshot_for_timestamp(
                        entity.timestamp_start
                    )
                if path and Path(path).exists():
                    results.append({
                        "path": Path(path).name,
                        "url": f"/api/graph/entity/{entity_id}/screenshot",
                        "timestamp": _utc_iso(entity.timestamp_start),
                        "entity_id": entity.id,
                        "entity_text": entity.text[:80],
                    })
            else:
                # Check observation_screenshots in metadata
                meta = entity.metadata_dict or {}
                obs_screenshots = meta.get("observation_screenshots") or meta.get("screenshot_paths") or []
                for idx, entry in enumerate(obs_screenshots):
                    # Support both basenames and full paths
                    candidate = _safe_screenshot_path(entry)
                    if candidate:
                        # Extract timestamp from screenshot filename
                        epoch = _extract_epoch_from_screenshot_name(candidate)
                        ts = None
                        if epoch:
                            ts = datetime.utcfromtimestamp(epoch).isoformat() + "Z"
                        results.append({
                            "path": candidate.name,
                            "url": f"/api/graph/entity/{entity_id}/screenshot?index={idx}",
                            "timestamp": ts,
                            "entity_id": entity.id,
                            "entity_text": entity.text[:80],
                        })

                # If no observation_screenshots, try traversing relations
                # (for hierarchical entities like actions/activities/goals)
                if not results and entity.type in (
                    EntityType.ACTION, EntityType.ACTIVITY, EntityType.GOAL
                ):
                    results = await _collect_screenshots_from_hierarchy(store, entity)

            return {"entity_id": entity_id, "screenshots": results}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to get entity screenshots")
        return JSONResponse({"error": "Failed to get entity screenshots"}, status_code=500)
    finally:
        await db.close()


async def _collect_screenshots_from_hierarchy(
    store: Store, entity: Entity,
    max_depth: int = 4, max_results: int = 50,
) -> list:
    """Traverse structural relations down to operations and collect screenshots."""
    from tempo.models import RelationType, RelationSubtype

    results = []
    visited = set()

    async def _traverse(eid: int, depth: int):
        if depth > max_depth or eid in visited or len(results) >= max_results:
            return
        visited.add(eid)

        e = await store.entities.get_with_relations(eid)
        if not e:
            return

        if e.type == EntityType.OPERATION:
            meta = e.metadata_dict or {}
            path = _resolve_screenshot_path_from_metadata(meta)
            if not path:
                path = _find_nearest_screenshot_for_timestamp(e.timestamp_start)
            if path and Path(path).exists():
                results.append({
                    "path": Path(path).name,
                    "url": f"/api/graph/entity/{e.id}/screenshot",
                    "timestamp": _utc_iso(e.timestamp_start),
                    "entity_id": e.id,
                    "entity_text": e.text[:80],
                })
            return

        # Traverse children (part_of relations where child.source → this.target)
        children = await store.relations.get_by_target(
            eid,
            relation_type=RelationType.STRUCTURAL,
            relation_subtype=RelationSubtype.PART_OF,
        )
        if children:
            for rel in children:
                await _traverse(rel.source_id, depth + 1)

    await _traverse(entity.id, 0)
    results.sort(key=lambda r: r.get("timestamp") or "")
    return results[:max_results]



@app.post("/api/graph/search")
async def search_entities(query: str, entity_type: Optional[str] = None, limit: int = 10):
    """Semantic search for entities."""
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            store = Store(session)
            queries = QueryInterface(store)
            
            results = await queries.search_entities(
                query,
                entity_type=entity_type,
                top_k=limit,
            )
            
            return {
                "results": [
                    {
                        "entity": {
                            "id": r.entity.id,
                            "type": r.entity.type,
                            "text": r.entity.text,
                            "timestamp_start": _utc_iso(r.entity.timestamp_start),
                            "timestamp_end": _utc_iso(r.entity.timestamp_end),
                        },
                        "score": r.score,
                    }
                    for r in results
                ],
                "count": len(results),
            }
    except Exception:
        logger.exception("Failed to search entities")
        return JSONResponse({"error": "Failed to search entities"}, status_code=500)
    finally:
        await db.close()


@app.get("/api/graph/timeline")
async def get_timeline(days: int = 7):
    """Get activity timeline."""
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            store = Store(session)
            queries = QueryInterface(store)
            
            activities = await queries.get_activity_timeline(days=days)
            
            return {
                "activities": [
                    {
                        "id": a.id,
                        "text": a.text,
                        "timestamp_start": _utc_iso(a.timestamp_start),
                        "timestamp_end": _utc_iso(a.timestamp_end),
                        "metadata": a.metadata_dict,
                    }
                    for a in activities
                ],
                "count": len(activities),
            }
    except Exception:
        logger.exception("Failed to get timeline")
        return JSONResponse({"error": "Failed to get timeline"}, status_code=500)
    finally:
        await db.close()


@app.get("/api/graph/stats")
async def get_stats():
    """Get database statistics."""
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            store = Store(session)
            
            ops = await store.entities.get_by_type(EntityType.OPERATION, limit=10000)
            actions = await store.entities.get_by_type(EntityType.ACTION, limit=10000)
            activities = await store.entities.get_by_type(EntityType.ACTIVITY, limit=10000)
            goals = await store.entities.get_by_type(EntityType.GOAL, limit=10000)
            propositions = await store.entities.get_by_type(EntityType.PROPOSITION, limit=10000)

            return {
                "operations": len(ops),
                "actions": len(actions),
                "activities": len(activities),
                "goals": len(goals),
                "propositions": len(propositions),
            }
    except Exception:
        logger.exception("Failed to get stats")
        return JSONResponse({"error": "Failed to get stats"}, status_code=500)
    finally:
        await db.close()



# ─── Goal endpoints ─────────────────────────────────────────────────────────


@app.get("/api/goals")
async def get_goals():
    """Return all goals with constituent activities."""
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            store = Store(session)
            goals = await store.get_goals()
            result = []
            for g in goals:
                gd = await store.get_goal_with_activities(g.id)
                activities = []
                if gd:
                    activities = [
                        {"id": a.id, "text": a.text, "metadata": a.metadata_dict}
                        for a in gd["activities"]
                    ]
                result.append({
                    "id": g.id,
                    "text": g.text,
                    "metadata": g.metadata_dict,
                    "activities": activities,
                })
            return {"goals": result, "count": len(result)}
    except Exception:
        logger.exception("Failed to get goals")
        return JSONResponse({"error": "Failed to get goals"}, status_code=500)
    finally:
        await db.close()



async def _run_hierarchy_mutation(action: str, mutation):
    """Run one edit atomically, then publish its committed canonical result."""
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            result = await mutation(HierarchyService(session))
    except HierarchyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Hierarchy %s failed", action)
        raise HTTPException(status_code=500, detail="Hierarchy mutation failed") from exc
    finally:
        await db.close()

    await broadcast_hierarchy_update(action, result)
    return result


@app.patch("/api/entity/{entity_id}")
async def update_hierarchy_entity(entity_id: int, request: EntityUpdateRequest):
    return await _run_hierarchy_mutation(
        "update",
        lambda service: service.update_entity(
            entity_id,
            expected_revision=request.expected_revision,
            text=request.text,
            locked=request.locked,
        ),
    )


@app.post("/api/entity/{entity_id}/reparent")
async def reparent_hierarchy_entity(entity_id: int, request: EntityReparentRequest):
    return await _run_hierarchy_mutation(
        "reparent",
        lambda service: service.reparent_entity(
            entity_id,
            new_parent_id=request.new_parent_id,
            expected_revision=request.expected_revision,
        ),
    )


@app.post("/api/entity/merge")
async def merge_hierarchy_entities(request: EntityMergeRequest):
    return await _run_hierarchy_mutation(
        "merge",
        lambda service: service.merge_entities(
            ids=request.ids,
            text=request.text,
            expected_revisions=request.expected_revisions,
        ),
    )


@app.post("/api/entity/{entity_id}/split")
async def split_hierarchy_entity(entity_id: int, request: EntitySplitRequest):
    return await _run_hierarchy_mutation(
        "split",
        lambda service: service.split_entity(
            entity_id,
            expected_revision=request.expected_revision,
            into=request.into,
        ),
    )


@app.delete("/api/entity/{entity_id}")
async def delete_hierarchy_entity(entity_id: int, request: EntityDeleteRequest):
    return await _run_hierarchy_mutation(
        "delete",
        lambda service: service.delete_entity(
            entity_id,
            expected_revision=request.expected_revision,
            reparent_children_to=request.reparent_children_to,
        ),
    )


@app.post("/api/entity")
async def create_hierarchy_entity(request: EntityCreateRequest):
    return await _run_hierarchy_mutation(
        "create",
        lambda service: service.create_entity(
            entity_type=request.type,
            text=request.text,
            parent_id=request.parent_id,
        ),
    )


@app.get("/api/hierarchy")
async def get_hierarchy(max_depth: int = Query(default=4, ge=1, le=4)):
    db = get_query_database()
    await db.connect()
    try:
        async with db.session() as session:
            return await HierarchyService(session).get_hierarchy(max_depth=max_depth)
    except HierarchyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Failed to get hierarchy")
        raise HTTPException(status_code=500, detail="Failed to get hierarchy") from exc
    finally:
        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Hierarchy compile — batch edits, re-synthesis, preview, accept/revert
# ─────────────────────────────────────────────────────────────────────────────


def get_compiler() -> HierarchyCompiler:
    """Compiler bound to whichever database the read endpoints are serving."""
    return HierarchyCompiler(data_directory=DATA_DIRECTORY, db_name=current_db_name)


def _compile_provider():
    """Resolve an LLM provider for a compile, or None if none is configured.

    Prefers the provider the running pipeline already built; otherwise creates
    one from the same environment resolution ``/api/start`` uses, so compiling
    works while recording is stopped.
    """
    if system is not None and getattr(system, "provider", None) is not None:
        return system.provider

    model_name = os.environ.get("MODEL_NAME", "gemini-3.8-flash")
    express = os.environ.get("GEMINI_VERTEXAI_EXPRESS", "").lower() in ("true", "1", "yes")
    vertexai = os.environ.get("GEMINI_VERTEXAI", "").lower() in ("true", "1", "yes")

    if express:
        api_key = (
            os.environ.get("VERTEX_EXPRESS_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        api_base = None
    else:
        api_key = resolve_provider_api_key(model_name, None)
        api_base = resolve_provider_api_base(model_name, None)

    # Vertex authenticates with a service-account file rather than a key.
    if not api_key and not (vertexai and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")):
        return None

    try:
        return create_provider(
            model_name,
            api_key=api_key,
            api_base=api_base,
            gemini_vertexai=vertexai and not express,
            gemini_vertexai_express=express,
            vertex_project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            vertex_location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
    except Exception:
        logger.exception("Could not build an LLM provider for compile")
        return None


@app.post("/api/hierarchy/compile")
async def compile_hierarchy(request: Request):
    """Compile staged edits against a working copy, streaming SSE progress.

    The live database is untouched: the result carries a draft token the client
    then accepts or reverts.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    edits = HierarchyEdits.from_payload(payload)
    if edits.is_empty():
        raise HTTPException(status_code=400, detail="No edits to compile")

    compiler = get_compiler()
    provider = _compile_provider()

    async def stream():
        try:
            async for event in compiler.compile(
                edits,
                provider=provider,
                user_name=os.environ.get("USER_NAME"),
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # a generator failure must not hang the client
            logger.exception("Hierarchy compile stream failed")
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/hierarchy/compile/{token}/accept")
async def accept_hierarchy_compile(token: str):
    """Promote a compile draft to be the live database."""
    # The swap moves the database file, so nothing may hold it open. Recording
    # keeps a long-lived connection, so it has to be stopped first.
    if status["running"]:
        raise HTTPException(
            status_code=409,
            detail="Stop recording before accepting a compile.",
        )

    async def quiesce(phase: str) -> None:
        if phase == "before" and system is not None and system.db is not None:
            # Sessions reconnect lazily, so this handle comes back on next use.
            await system.db.force_close()

    try:
        result = await get_compiler().accept(token, on_quiesce=quiesce)
    except CompileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Failed to accept compile %s", token)
        raise HTTPException(status_code=500, detail="Failed to accept compile") from exc

    await broadcast_hierarchy_update("compile:accept", result)
    return result


@app.post("/api/hierarchy/compile/{token}/revert")
async def revert_hierarchy_compile(token: str):
    """Discard a compile draft. The live database was never modified."""
    try:
        return await get_compiler().revert(token)
    except CompileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Failed to revert compile %s", token)
        raise HTTPException(status_code=500, detail="Failed to revert compile") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding — optional self-description that grounds every inference stage
# ─────────────────────────────────────────────────────────────────────────────

#: The questions asked on first run. Answering is optional and partial answers
#: are fine — each one that is filled in becomes a line the model can reason
#: from instead of inferring a whole life from screenshots.
ONBOARDING_QUESTIONS: List[Dict[str, str]] = [
    {"key": "roles", "label": "Roles",
     "prompt": "What are the main roles you play in your life right now?",
     "placeholder": "e.g. software engineer, parent, student, caretaker…"},
    {"key": "typical_day", "label": "Typical day",
     "prompt": "Walk through what a typical weekday looks like for you.",
     "placeholder": "e.g. Wake at 7, commute, meetings in the morning…"},
    {"key": "main_concerns", "label": "Time & energy",
     "prompt": "What takes up most of your time and energy right now — by choice or obligation?",
     "placeholder": "e.g. A big project deadline, caring for family…"},
    {"key": "stressors", "label": "Stressors",
     "prompt": "What are the main sources of stress or pressure in your life right now?",
     "placeholder": "e.g. Financial concerns, health issues, deadlines…"},
    {"key": "recent_changes", "label": "Recent changes",
     "prompt": "Has anything significant changed recently, or is anything about to?",
     "placeholder": "e.g. New job, moving, relationship changes…"},
    {"key": "work", "label": "Work & career",
     "prompt": "What are you working toward in your work or career?",
     "placeholder": "e.g. Getting promoted, finding meaningful work, managing a team…"},
    {"key": "relationships", "label": "Relationships",
     "prompt": "What matters to you in your relationships right now?",
     "placeholder": "e.g. Maintaining friendships, family obligations, dating…"},
    {"key": "personal_growth", "label": "Personal growth",
     "prompt": "What are you trying to develop in yourself?",
     "placeholder": "e.g. Learning new skills, self-improvement, creative pursuits…"},
    {"key": "health", "label": "Health",
     "prompt": "What are you working on health-wise, if anything?",
     "placeholder": "e.g. Exercise routine, managing a condition, mental health…"},
    {"key": "finances", "label": "Finances",
     "prompt": "What are your financial priorities right now?",
     "placeholder": "e.g. Saving for a goal, managing debt, budgeting…"},
    {"key": "education", "label": "Education",
     "prompt": "Are you studying or learning anything at the moment?",
     "placeholder": "e.g. Coursework, research, learning on the job…"},
    {"key": "additional_context", "label": "Anything else",
     "prompt": "Anything else that would help Tempo read your activity correctly?",
     "placeholder": "e.g. you share this computer, or it is work-only and you never browse personally…"},
]

_ONBOARDING_KEYS = {question["key"] for question in ONBOARDING_QUESTIONS}


def _onboarding_path() -> Path:
    return DATA_DIRECTORY / "onboarding.json"


def _load_onboarding() -> dict:
    """Read stored answers, treating any problem as 'not answered yet'."""
    try:
        return json.loads(_onboarding_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _build_user_context(stored: dict, user_name: str):
    """Render stored answers into the prompt block, or None when unanswered."""
    if not stored.get("responses"):
        return None
    try:
        from tempo.context import UserContext

        return UserContext(
            onboarding_path=str(_onboarding_path()),
            user_name=stored.get("user_name") or user_name,
        )
    except Exception:
        logger.exception("Could not build user context from onboarding answers")
        return None


class OnboardingRequest(BaseModel):
    user_name: str = Field(default="", max_length=200)
    responses: Dict[str, str] = Field(default_factory=dict)
    #: Set when the user closed the prompt without answering. Recorded so they
    #: are asked once rather than before every recording session.
    dismissed: bool = False


@app.get("/api/onboarding")
async def get_onboarding():
    """Return the onboarding questions and any answers already given."""
    stored = _load_onboarding()
    return {
        "questions": ONBOARDING_QUESTIONS,
        "user_name": stored.get("user_name", ""),
        "responses": stored.get("responses", {}),
        "completed": bool(stored.get("responses")),
        "dismissed": bool(stored.get("dismissed")),
    }


@app.put("/api/onboarding")
async def save_onboarding(request: OnboardingRequest):
    """Store answers and apply them to a running pipeline immediately."""
    responses = {
        key: value.strip()
        for key, value in request.responses.items()
        if key in _ONBOARDING_KEYS and value and value.strip()
    }
    previous = _load_onboarding()
    payload = {
        "user_name": request.user_name.strip(),
        "responses": responses,
        # Once asked, stays asked — dismissing is not undone by a later save.
        "dismissed": bool(request.dismissed or previous.get("dismissed")),
        "updated_at": datetime.utcnow().isoformat(),
    }
    try:
        _onboarding_path().parent.mkdir(parents=True, exist_ok=True)
        _onboarding_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.exception("Failed to save onboarding answers")
        raise HTTPException(status_code=500, detail="Could not save your answers") from exc

    # Apply without a restart when the pipeline is already running.
    applied = False
    if orchestrator is not None and responses:
        context = _build_user_context(payload, payload["user_name"] or "the user")
        if context is not None:
            try:
                orchestrator.reload_user_context(context)
                applied = True
            except Exception:
                logger.exception("Could not apply onboarding context to the pipeline")

    return {
        "saved": True,
        "applied_to_running_pipeline": applied,
        "answered": len(responses),
        "dismissed": payload["dismissed"],
    }


@app.get("/api/diagnostics/observer")
async def observer_diagnostics():
    """Report where the capture path stands, stage by stage.

    Capture failing is close to invisible from outside: the server still says
    it is running, and the only symptom is that nothing accumulates. This
    endpoint answers the question the logs cannot — is the OS delivering input
    events, and if so what happens to them.
    """
    observer = getattr(system, "observer", None) if system is not None else None
    if observer is None:
        return {"observer": None, "detail": "No observer — Tempo is not recording."}

    backend = getattr(observer, "os_backend", None)
    listener = getattr(backend, "listener_task", None)
    loop = getattr(observer, "event_loop", None)
    now = time.time()

    seen = getattr(observer, "_mouse_events_seen", 0)
    handled = getattr(observer, "_mouse_events_handled", 0)

    grabbed = getattr(observer, "_frames_grabbed", 0)
    attempts = getattr(observer, "_persist_attempts", 0)
    saved = getattr(observer, "_persist_succeeded", 0)
    low_change = getattr(observer, "_persist_skipped_low_change", 0)
    no_frames = getattr(observer, "_persist_skipped_no_frames", 0)
    skips_visible = getattr(observer, "_capture_skips_visible_app", 0)
    skips_excluded = getattr(observer, "_capture_skips_excluded", 0)
    running = bool(getattr(observer, "_running", False))
    worker = getattr(observer, "_task", None)
    worker_alive = worker is not None and not worker.done()
    exclusion_paused = bool(getattr(observer, "_exclusion_paused", False))
    persist_failure = getattr(observer, "_last_persist_failure", None)

    health = observer.capture_health.report(
        running=running,
        worker_alive=worker_alive,
        listener_alive=bool(listener is not None and listener.is_alive()),
        failure_reason=persist_failure,
    )

    return {
        "running": running,
        "worker_task_alive": worker_alive,
        "listener_thread_alive": bool(listener is not None and listener.is_alive()),
        "event_loop_is_running_loop": loop is asyncio.get_running_loop(),
        "event_loop_closed": bool(loop.is_closed()) if loop is not None else None,
        "mouse_events": {
            "seen": seen,
            "handled": handled,
            "dropped": getattr(observer, "_mouse_events_dropped", 0),
            "last_drop_reason": getattr(observer, "_last_drop_reason", None),
        },
        "seconds_since_handled_event": round(
            now - getattr(observer, "_last_mouse_event_time", now), 1
        ),
        "capture": {
            "frames_grabbed": grabbed,
            "skipped_ignored_app_visible": skips_visible,
            "skipped_excluded": skips_excluded,
            "exclusion_paused": exclusion_paused,
            "ignored_app_names": list(getattr(observer, "ignore_visible_app_names", [])),
        },
        "persist": {
            "attempts": attempts,
            "saved": saved,
            "skipped_low_change": low_change,
            "skipped_no_frames": no_frames,
            "last_change_score": getattr(observer, "_last_change_score", None),
            "change_threshold": getattr(observer, "change_threshold", None),
            "last_failure_reason": persist_failure,
            "blocked_by_content_filter": getattr(observer, "_content_filter_blocks", 0),
        },
        "screenshots_captured": len(
            list(Path(DEFAULT_SCREENSHOTS_DIR).glob("*.jpg"))
        ) if os.path.isdir(DEFAULT_SCREENSHOTS_DIR) else 0,
        "verdict": health["verdict"],
        "health": health,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008, reason="Origin not allowed")
        return
    await websocket.accept()
    websocket_clients.append(websocket)

    try:
        # Send current status immediately
        await websocket.send_json({"type": "status", "data": status})

        # Keep connection alive and wait for messages
        while True:
            try:
                # Wait for ping or timeout
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # Echo back or handle message
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # Send periodic status updates
                await websocket.send_json({"type": "status", "data": status})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WebSocket error")
    finally:
        if websocket in websocket_clients:
            websocket_clients.remove(websocket)


_UI_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


async def get_assistant_service() -> AssistantService:
    global assistant_service
    async with _assistant_init_lock:
        if assistant_service is not None and (
            Path(assistant_service.db.data_directory) != DATA_DIRECTORY or assistant_service.db.db_name != current_db_name
        ):
            await assistant_service.close()
            assistant_service = None
        if assistant_service is None:
            runtime = AssistantService(get_query_database(), _compile_provider, lambda: describe_connection(_compile_provider()))
            await runtime.start()
            assistant_service = runtime
        return assistant_service


async def _assistant_request(method: str, *args):
    try:
        runtime = await get_assistant_service()
        return await getattr(runtime, method)(*args)
    except AssistantError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except ValueError as exc:
        detail = "Choose goals that are still available." if method == "refine" else "The assistant returned an invalid response. Please retry."
        raise HTTPException(status_code=422 if method == "refine" else 502, detail=detail) from exc
    except Exception as exc:
        # Provider exceptions can contain prompts or credentials. Keep them out
        # of HTTP responses and routine logs.
        logger.warning("Assistant %s failed (%s)", method, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Assistant request failed. Check your model settings and retry.") from exc


@app.get("/api/assistant/feed")
async def assistant_feed():
    return await _assistant_request("feed")


@app.put("/api/assistant/settings")
async def assistant_settings(request: AssistantSettingsRequest):
    return await _assistant_request("settings", request)


@app.post("/api/assistant/refresh")
async def assistant_refresh():
    return await _assistant_request("refresh")


@app.post("/api/assistant/refine")
async def assistant_refine(request: AssistantRefineRequest):
    return await _assistant_request("refine", request)


@app.post("/api/assistant/feedback")
async def assistant_feedback(request: FeedbackRequest):
    return await _assistant_request("feedback", request)


@app.post("/api/assistant/chat")
async def assistant_chat(request: ChatSendRequest):
    return await _assistant_request("chat", request)


@app.get("/api/assistant/chats")
async def assistant_threads():
    return await _assistant_request("threads")


@app.get("/api/assistant/chats/{session_id}")
async def assistant_history(session_id: UUID):
    return await _assistant_request("history", session_id)


@app.get("/{spa_path:path}", include_in_schema=False)
async def serve_packaged_ui(spa_path: str):
    """Serve packaged Vite assets and fall back to the SPA entry document."""
    if spa_path == "ws" or spa_path == "api" or spa_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")

    root = UI_DIRECTORY.resolve()
    requested = (root / spa_path).resolve()
    try:
        requested.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc

    if spa_path and requested.is_file():
        headers = dict(_UI_SECURITY_HEADERS)
        if requested.parent.name == "assets":
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return FileResponse(requested, headers=headers)

    if spa_path and Path(spa_path).suffix:
        raise HTTPException(status_code=404, detail="Asset not found")

    index = root / "index.html"
    if not index.is_file():
        return JSONResponse(
            {"error": "Tempo UI is not installed; rebuild the package with scripts/build-wheel.sh"},
            status_code=503,
        )
    return FileResponse(
        index,
        headers={**_UI_SECURITY_HEADERS, "Cache-Control": "no-store"},
    )


def _start_parent_watchdog():
    """Exit if the parent process (Electron) dies, preventing zombie servers."""
    import threading, signal
    parent_pid = os.getppid()
    def _watch():
        while True:
            time.sleep(5)
            try:
                os.kill(parent_pid, 0)  # check if parent is alive
            except OSError:
                print("Parent process died — shutting down server")
                os.kill(os.getpid(), signal.SIGTERM)
                return
    t = threading.Thread(target=_watch, daemon=True)
    t.start()


if __name__ == "__main__":
    import uvicorn
    _start_parent_watchdog()
    uvicorn.run(app, host="127.0.0.1", port=8756, log_level="info")
