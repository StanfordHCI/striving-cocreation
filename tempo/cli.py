# cli.py

"""
Tempo CLI - Command Line Interface for Tempo.

This module handles:
- Command-line argument parsing
- Configuration setup
- System initialization
- Delegating execution to pipeline orchestrator
"""

import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
import webbrowser
from datetime import datetime
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

from pathlib import Path

from tempo.system import TempoSystem
from tempo.pipelines.orchestrator import PipelineOrchestrator
from tempo.db import Database
from tempo.store import Store
from tempo.queries import QueryInterface
from tempo.models import EntityType
from tempo.state import StateManager
from tempo.transcription_cache import TranscriptionCache
from tempo.providers import (
    create_provider,
    provider_name_for_model,
    resolve_provider_api_base,
    resolve_provider_api_key,
)
from tempo.prompts.screen import TRANSCRIPTION_PROMPT, SUMMARY_PROMPT


def get_data_dir() -> Path:
    """Return Tempo's exact local-data root."""
    return Path(os.environ.get("TEMPO_DATA_DIR", "~/.cache/tempo")).expanduser().resolve()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"true", "1", "yes"}


def _database() -> Database:
    return Database(data_directory=str(get_data_dir()))


def _redact_endpoint(endpoint: str) -> str:
    """Return a display-safe endpoint without URL credentials or query values."""
    try:
        parsed = urlsplit(endpoint)
        if not parsed.scheme or not parsed.hostname:
            return endpoint
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        query = "redacted" if parsed.query else ""
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except (ValueError, TypeError):
        return "invalid endpoint"


def _endpoint_scope(endpoint: str) -> str:
    """Classify an HTTP-style endpoint as local or external."""
    try:
        hostname = urlsplit(endpoint).hostname
        if not hostname:
            return "unknown"
        if hostname.lower() == "localhost":
            return "local"
        return "local" if ip_address(hostname).is_loopback else "external"
    except ValueError:
        return "external"


def build_doctor_report(env: dict[str, str] | None = None) -> str:
    """Describe configured network destinations without exposing secrets."""
    values = dict(os.environ if env is None else env)
    model = values.get("MODEL_NAME", "gemini-3.8-flash")
    provider = provider_name_for_model(model)
    lines = [
        "Tempo network doctor",
        "",
        "Application traffic:",
        "  [local] Tempo HTTP API: http://127.0.0.1:8756",
        "  [local] Tempo WebSocket: ws://127.0.0.1:8756/ws",
    ]

    custom_base = resolve_provider_api_base(model, environ=values)
    if provider == "gemini":
        if values.get("GEMINI_VERTEXAI_EXPRESS", "").lower() in {"true", "1", "yes"}:
            endpoint = "https://aiplatform.googleapis.com"
            source = "Vertex AI Express Mode API-key endpoint"
        elif values.get("GEMINI_VERTEXAI", "").lower() in {"true", "1", "yes"}:
            location = values.get("GOOGLE_CLOUD_LOCATION", "global")
            endpoint = (
                "https://aiplatform.googleapis.com"
                if location == "global"
                else f"https://{location}-aiplatform.googleapis.com"
            )
            source = "Vertex AI SDK endpoint"
        else:
            endpoint = "https://generativelanguage.googleapis.com"
            source = "Gemini SDK endpoint"
    elif provider == "anthropic":
        endpoint = custom_base or "https://api.anthropic.com/v1"
        source = (
            "TEMPO_LM_API_BASE/ANTHROPIC_API_BASE"
            if custom_base
            else "Anthropic API endpoint"
        )
    elif custom_base:
        endpoint = custom_base
        source = "TEMPO_LM_API_BASE/OPENAI_API_BASE"
    elif model.lower().startswith(("gpt-", "o1", "o3", "o4")):
        endpoint = "https://api.openai.com/v1"
        source = "OpenAI SDK endpoint"
    else:
        endpoint = "provider-managed"
        source = "LiteLLM resolves this model's endpoint"

    lines.extend([
        "",
        "LLM traffic:",
        f"  [{_endpoint_scope(endpoint)}] Main model ({model}): {_redact_endpoint(endpoint)}",
        f"             source: {source}",
    ])

    screen_base = values.get("SCREEN_LM_API_BASE")
    if screen_base and screen_base != custom_base:
        lines.append(
            f"  [{_endpoint_scope(screen_base)}] Screen model override: {_redact_endpoint(screen_base)}"
        )

    if values.get("ANTHROPIC_API_KEY"):
        anthropic_base = values.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        lines.append(
            f"  [{_endpoint_scope(anthropic_base)}] Widget chat: {_redact_endpoint(anthropic_base)}"
        )

    key_sources = [
        name for name in (
            "TEMPO_LM_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "ANTHROPIC_API_KEY",
            "VERTEX_EXPRESS_API_KEY",
        ) if values.get(name)
    ]
    lines.extend([
        "",
        "Credentials:",
        "  Configured variables: " + (", ".join(key_sources) if key_sources else "none"),
        "  Values are intentionally hidden.",
        "",
        "Telemetry:",
        "  Disabled. Tempo has no Firebase, Sentry, remote favicon, or remote font client.",
        "  Renderer HTTP/WebSocket requests are restricted to loopback hosts.",
    ])
    report = "\n".join(lines)
    for name, value in values.items():
        if value and any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            report = report.replace(value, "[hidden]")
    return report


def cmd_doctor(_args):
    """Print the local/external network configuration."""
    print(build_doctor_report())


def cmd_data_dir(_args) -> None:
    print(get_data_dir())


def _purge_contents(root: Path) -> None:
    for child in root.iterdir() if root.exists() else ():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def cmd_purge(args) -> None:
    data_dir = get_data_dir()
    home = Path.home().resolve()
    if data_dir in {Path("/").resolve(), home}:
        raise RuntimeError(f"Refusing unsafe data directory: {data_dir}")

    target = data_dir if args.all else data_dir / "screenshots"
    if not args.yes:
        answer = input(f"Delete {target}? Type 'purge' to confirm: ").strip().lower()
        if answer != "purge":
            print("Purge cancelled.")
            return

    if args.all:
        _purge_contents(data_dir)
    elif target.exists():
        shutil.rmtree(target)
    print(f"Purged {'all Tempo data' if args.all else 'Tempo screenshots'} from {target}.")


async def _open_ui_later(port: int) -> None:
    await asyncio.sleep(0.4)
    await asyncio.to_thread(webbrowser.open, f"http://127.0.0.1:{port}")


async def cmd_serve(args) -> None:
    """Serve existing local data and the packaged browser UI."""
    import uvicorn
    import tempo.server as api_server

    data_dir = get_data_dir()
    api_server.configure_data_directory(data_dir)
    server = uvicorn.Server(uvicorn.Config(
        api_server.app,
        host="127.0.0.1",
        port=args.port,
        log_level="info",
    ))
    opener = None if args.no_open else asyncio.create_task(_open_ui_later(args.port))
    try:
        await server.serve()
    finally:
        if opener is not None:
            if not opener.done():
                opener.cancel()
            await asyncio.gather(opener, return_exceptions=True)


def _flatten_csv(values: list[str] | None) -> list[str]:
    items: list[str] = []
    for value in values or []:
        for part in value.split(","):
            cleaned = part.strip()
            if cleaned:
                items.append(cleaned)
    return items


async def cmd_start(args):
    """Handle the 'start' command."""
    # Configure logging
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        # Quiet verbose client libraries even in debug
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("openai._base_client").setLevel(logging.WARNING)
    else:
        logging.basicConfig(
            level=logging.WARNING,
            format="%(asctime)s - %(levelname)s - %(message)s"
        )
    
    # Determine api_base and api_key based on model type
    vertex_express = getattr(args, "vertex_express", False)
    api_base = None if vertex_express else resolve_provider_api_base(args.model, args.api_base)
    api_key = (
        args.api_key or os.environ.get("VERTEX_EXPRESS_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if vertex_express
        else resolve_provider_api_key(args.model, args.api_key)
    )
    
    # Set up system
    system = TempoSystem(
        model_name=args.model,
        platform=args.platform,
        debug=args.debug,
        api_base=api_base,
        api_key=api_key,
        ignore_visible_app_names=_flatten_csv(args.ignore_app),
        vertex_project=getattr(args, "vertex_project", None),
        vertex_location=getattr(args, "vertex_location", None),
        gemini_vertexai_express=vertex_express,
        enable_content_filter=not args.no_content_filter,
        data_directory=str(get_data_dir()),
    )
    
    # Initialize components
    await system.setup()
    
    # Create orchestrator
    orchestrator = PipelineOrchestrator(system)
    await orchestrator.setup(main_db=system.db)

    api_server = None
    ui_server = None
    ui_task = None
    opener = None
    if not args.no_ui:
        import uvicorn
        import tempo.server as api_server_module

        api_server = api_server_module
        data_dir = get_data_dir()
        api_server.configure_data_directory(data_dir)
        api_server.system = system
        api_server.orchestrator = orchestrator
        api_server.status = {
            "running": True,
            "error": None,
            "message": "Recording",
            "started_at": datetime.now().isoformat(),
        }
        ui_server = uvicorn.Server(uvicorn.Config(
            api_server.app,
            host="127.0.0.1",
            port=args.port,
            log_level="debug" if args.debug else "warning",
        ))
        ui_task = asyncio.create_task(ui_server.serve(), name="tempo-ui-server")
        opener = asyncio.create_task(_open_ui_later(args.port), name="tempo-open-ui")
    
    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()
    
    stop_task = None

    def signal_handler():
        nonlocal stop_task
        # Fast stop on Ctrl+C (don't flush buffer; resume pending ops on next start)
        if stop_task is None or stop_task.done():
            stop_task = asyncio.create_task(
                orchestrator.stop(flush_buffer=False), name="tempo-signal-stop"
            )
        if ui_server is not None:
            ui_server.should_exit = True
    
    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)
    
    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        await orchestrator.stop(flush_buffer=False)
    finally:
        if stop_task is not None:
            await asyncio.gather(stop_task, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        if opener is not None and not opener.done():
            opener.cancel()
        if opener is not None:
            await asyncio.gather(opener, return_exceptions=True)
        if api_server is not None:
            api_server.system = None
            api_server.orchestrator = None
            api_server.status = {
                "running": False,
                "error": None,
                "message": "Stopped",
                "started_at": None,
            }
        if ui_server is not None:
            ui_server.should_exit = True
        if ui_task is not None:
            await asyncio.gather(ui_task, return_exceptions=True)
        await orchestrator.close()
        await system.cleanup(flush_buffer=False)


async def cmd_query(args):
    """Handle the 'query' command."""
    db = _database()
    await db.connect()
    
    try:
        async with db.session() as session:
            store = Store(session)
            queries = QueryInterface(store)
            
            results = await queries.search_entities(
                args.query_text,
                entity_type=args.type,
                top_k=args.limit,
            )
            
            if not results:
                print("No results found.")
                return
            
            print(f"\n Found {len(results)} results:\n")
            for r in results:
                score_str = f" (score: {r.score:.2f})" if r.score else ""
                print(f" [{r.entity.type}]{score_str}")
                print(f"   {r.entity.text[:100]}...")
                print(f"   {r.entity.timestamp_start.strftime('%Y-%m-%d %H:%M')}")
                print()
    finally:
        await db.close()


async def cmd_timeline(args):
    """Handle the 'timeline' command."""
    db = _database()
    await db.connect()
    
    try:
        async with db.session() as session:
            store = Store(session)
            queries = QueryInterface(store)
            
            activities = await queries.get_activity_timeline(days=args.days)
            
            if not activities:
                print(f"No activities found in the past {args.days} days.")
                return
            
            print(f"\n Activity Timeline (past {args.days} days):\n")
            for a in activities:
                print(f" {a.timestamp_start.strftime('%Y-%m-%d %H:%M')} - {a.text[:80]}")
    finally:
        await db.close()


async def cmd_stats(args):
    """Handle the 'stats' command."""
    db = _database()
    await db.connect()

    try:
        async with db.session() as session:
            store = Store(session)

            ops = await store.entities.get_by_type(EntityType.OPERATION, limit=10000)
            actions = await store.entities.get_by_type(EntityType.ACTION, limit=10000)
            activities = await store.entities.get_by_type(EntityType.ACTIVITY, limit=10000)

            print("\n Tempo Statistics:\n")
            print(f"   Operations: {len(ops)}")
            print(f"   Actions:    {len(actions)}")
            print(f"   Activities: {len(activities)}")

            # Show state info
            state_manager = StateManager(data_directory=str(get_data_dir()))
            state = state_manager.load()
            if state:
                print(f"\n State Info:")
                print(f"   Last active: {state.last_active}")
                print(f"   Buffered operations: {len(state.buffer.operations)}")
                if state.jobs.last_activity_inference:
                    print(f"   Last activity inference: {state.jobs.last_activity_inference}")
            print()
    finally:
        await db.close()


async def cmd_cache(args):
    """Handle the 'cache' command."""
    screenshots_dir = get_data_dir() / "screenshots"
    cache = TranscriptionCache(screenshots_dir=str(screenshots_dir))
    total = cache.count()

    # Count screenshots without transcriptions
    jpg_count = len(list(screenshots_dir.glob("*.jpg")))

    print("\n Transcription Cache Statistics:\n")
    print(f"   Screenshots: {jpg_count}")
    print(f"   Cached transcriptions: {total}")
    print(f"   Missing transcriptions: {jpg_count - total}")

    if total > 0:
        transcriptions = cache.list_all()
        oldest = transcriptions[0].timestamp
        newest = transcriptions[-1].timestamp
        print(f"   Date range: {oldest} to {newest}")
    print()


async def cmd_backfill(args):
    """Handle the 'backfill' command - generate transcriptions for screenshots missing them."""
    import base64
    import json
    import random
    from datetime import datetime

    cache = TranscriptionCache(screenshots_dir=str(get_data_dir() / "screenshots"))
    missing = cache.list_screenshots_without_cache()

    if not missing:
        print("\n All screenshots already have transcriptions cached.\n")
        return

    print(f"\n Found {len(missing)} screenshots without transcriptions.\n")

    if args.dry_run:
        print(" Dry run - showing first 10 screenshots that would be processed:\n")
        for path in missing[:10]:
            print(f"   {path}")
        if len(missing) > 10:
            print(f"   ... and {len(missing) - 10} more")
        print()
        return

    # Limit processing if --limit is specified
    to_process = missing
    if args.limit:
        to_process = missing[:args.limit]
        print(f" Processing {len(to_process)} of {len(missing)} (--limit {args.limit})\n")

    # Determine API key
    vertex_express = getattr(args, "vertex_express", False)
    api_key = (
        args.api_key or os.environ.get("VERTEX_EXPRESS_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if vertex_express
        else resolve_provider_api_key(args.model, args.api_key)
    )
    api_base = None if vertex_express else resolve_provider_api_base(args.model, args.api_base)

    # Create provider
    provider = create_provider(
        model=args.model,
        api_key=api_key,
        api_base=api_base,
        gemini_vertexai_express=vertex_express,
    )

    # Concurrency and retry settings
    max_concurrent = args.concurrency
    max_retries = 3
    base_backoff = 1.0
    max_backoff = 30.0

    semaphore = asyncio.Semaphore(max_concurrent)
    print_lock = asyncio.Lock()

    # Track progress
    completed = 0
    total = len(to_process)
    results = {"success": 0, "errors": 0}

    async def encode_image(path: str) -> str:
        """Encode image to base64."""
        def _read():
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        return await asyncio.to_thread(_read)

    async def call_vision_api_with_retry(prompt: str, image_path: str) -> str:
        """Call vision API with exponential backoff retry."""
        encoded = await encode_image(image_path)
        content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            },
            {"type": "text", "text": prompt},
        ]

        attempt = 0
        last_exc = None
        while attempt <= max_retries:
            try:
                return await provider.vision_completion(
                    messages=[{"role": "user", "content": content}],
                    response_format={"type": "text"},
                )
            except Exception as exc:
                last_exc = exc
                if attempt >= max_retries:
                    raise
                # Exponential backoff with jitter
                delay = min(max_backoff, base_backoff * (2 ** attempt))
                delay += random.uniform(0, delay * 0.1)
                if args.debug:
                    async with print_lock:
                        print(f"\n   Retry {attempt + 1}/{max_retries} after {delay:.1f}s: {exc}")
                await asyncio.sleep(delay)
                attempt += 1

        raise last_exc

    async def save_transcription(
        screenshot_path: str,
        transcription: str,
        summary: str,
        model_used: str,
    ) -> None:
        """Save transcription as sidecar JSON file."""
        json_path = screenshot_path.rsplit('.', 1)[0] + '.json'
        # Extract timestamp from filename (format: YYYY-MM-DD_HH-MM-SS.jpg)
        filename = os.path.basename(screenshot_path)
        try:
            ts_str = filename.rsplit('.', 1)[0]
            timestamp = datetime.strptime(ts_str, "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            timestamp = datetime.now()

        data = {
            "version": 1,
            "screenshot_path": os.path.basename(screenshot_path),
            "timestamp": timestamp.isoformat(),
            "transcription": transcription,
            "summary": summary,
            "app_context": None,  # Not available for backfill
            "model_used": model_used,
            "created_at": datetime.now().isoformat(),
        }
        await asyncio.to_thread(
            lambda: Path(json_path).write_text(json.dumps(data, indent=2))
        )

    async def process_screenshot(screenshot_path: str) -> bool:
        """Process a single screenshot with semaphore for concurrency control."""
        nonlocal completed

        async with semaphore:
            filename = os.path.basename(screenshot_path)

            try:
                # Get transcription and summary (with retries)
                transcription = await call_vision_api_with_retry(TRANSCRIPTION_PROMPT, screenshot_path)
                summary = await call_vision_api_with_retry(SUMMARY_PROMPT, screenshot_path)

                # Save
                await save_transcription(screenshot_path, transcription, summary, args.model)

                async with print_lock:
                    completed += 1
                    print(f" [{completed}/{total}] {filename} ✓")

                return True

            except Exception as exc:
                async with print_lock:
                    completed += 1
                    print(f" [{completed}/{total}] {filename} ✗ {exc}")
                    if args.debug:
                        import traceback
                        traceback.print_exc()

                return False

    # Process all screenshots concurrently (limited by semaphore)
    print(f" Processing with {max_concurrent} concurrent requests...\n")
    tasks = [process_screenshot(path) for path in to_process]
    task_results = await asyncio.gather(*tasks)

    results["success"] = sum(1 for r in task_results if r)
    results["errors"] = sum(1 for r in task_results if not r)

    print(f"\n Backfill complete: {results['success']} succeeded, {results['errors']} failed\n")


def _load_dotenv():
    """Load .env from the data directory, the working directory, or the checkout.

    The data-directory copy is where the UI writes provider configuration, so it
    must be readable no matter which directory `tempo` was invoked from. All
    locations are layered rather than exclusive: a key set by an earlier file
    wins, so a UI-written key is not shadowed by a stale checkout .env.
    """
    env_locations = [
        get_data_dir() / ".env",
        Path.cwd() / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for env_path in env_locations:
        if not env_path.exists():
            continue
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value


def _port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _run_async(command) -> None:
    """Run an async CLI command without printing a traceback on Ctrl-C."""
    try:
        asyncio.run(command)
    except KeyboardInterrupt:
        print("\nTempo stopped.")


def cli():
    """Main CLI entry point."""
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Tempo: Temporal Pattern Observer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  tempo start                              Start observing (macOS, configured model)
  tempo start --no-ui                      Start without the browser interface
  tempo serve                              Browse existing data without recording
  tempo start --platform gnome             Start observing (GNOME/Linux)
  tempo start --api-base http://localhost:8000/v1 --model Qwen/Qwen2-VL-7B-Instruct
                                           Use local vLLM server
  tempo query "email"                      Search for email-related activities
  tempo timeline --days 7                  Show past week's activities
  tempo stats                              Show database statistics
  tempo doctor                             Show configured network destinations
  tempo data-dir                           Print the local data directory
  tempo purge --screenshots                Delete captured screenshots after confirmation

Environment Variables:
  OPENAI_API_KEY      API key for OpenAI
  OPENAI_API_BASE     Base URL for OpenAI-compatible API (alternative to --api-base)
  GOOGLE_API_KEY      API key for Gemini
  VERTEX_EXPRESS_API_KEY API key for Vertex AI Express Mode
  ANTHROPIC_API_KEY   API key for Claude
  ANTHROPIC_API_BASE  Optional custom Anthropic API base for the main pipeline
  TEMPO_LM_API_KEY    API key override for Tempo (--api-key default)
  TEMPO_LM_API_BASE   API base override for Tempo (--api-base default)
  MODEL_NAME          Default model name for --model
        """
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Start command
    start_parser = subparsers.add_parser("start", help="Start the Tempo observer")
    start_parser.add_argument(
        "--model",
        default=os.environ.get("MODEL_NAME", "gemini-3.8-flash"),
        help="Model to use for analysis (default: gemini-3.8-flash, or $MODEL_NAME if set)",
    )
    start_parser.add_argument(
        "--platform",
        choices=["macos", "gnome"],
        default="macos",
        help="Platform backend (default: macos)",
    )
    start_parser.add_argument(
        "--api-base",
        dest="api_base",
        default=None,
        help="Optional provider API base. Defaults to the provider-specific environment variable or $TEMPO_LM_API_BASE.",
    )
    start_parser.add_argument(
        "--api-key",
        dest="api_key",
        default=None,
        help="Provider API key. Defaults to $TEMPO_LM_API_KEY or the selected provider's key variable.",
    )
    start_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    start_parser.add_argument(
        "--ignore-app",
        action="append",
        default=["Electron", "Tempo"],
        help="Skip capture when these app names are visible (repeat or comma-separate). Default: Electron,Tempo",
    )
    start_parser.add_argument(
        "--vertex-project",
        dest="vertex_project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        help="GCP project ID for Vertex AI (default: $GOOGLE_CLOUD_PROJECT)",
    )
    start_parser.add_argument(
        "--vertex-express",
        action="store_true",
        default=_env_flag("GEMINI_VERTEXAI_EXPRESS"),
        help="Use Vertex AI Express Mode with VERTEX_EXPRESS_API_KEY",
    )
    start_parser.add_argument(
        "--vertex-location",
        dest="vertex_location",
        default=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        help="Vertex AI region (default: global, or $GOOGLE_CLOUD_LOCATION)",
    )
    start_parser.add_argument(
        "--no-content-filter",
        dest="no_content_filter",
        action="store_true",
        help="Disable local OCR + PII content filter (screenshots sent to cloud unscreened)",
    )
    start_parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Run the observer and pipeline without serving or opening the browser UI",
    )
    start_parser.add_argument(
        "--port",
        type=_port_number,
        default=8756,
        help="Local UI/API port (default: 8756)",
    )

    serve_parser = subparsers.add_parser(
        "serve", help="Serve the UI over existing local Tempo data without recording"
    )
    serve_parser.add_argument(
        "--port", type=_port_number, default=8756, help="Local UI/API port (default: 8756)"
    )
    serve_parser.add_argument(
        "--no-open", action="store_true", help="Do not open the UI in the default browser"
    )

    # Query command
    query_parser = subparsers.add_parser("query", help="Search the behavioral graph")
    query_parser.add_argument(
        "query_text",
        help="Search query",
    )
    query_parser.add_argument(
        "--type",
        choices=["operation", "action", "activity", "goal", "proposition"],
        help="Filter by entity type",
    )
    query_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum results to return (default: 10)",
    )
    
    # Timeline command
    timeline_parser = subparsers.add_parser("timeline", help="Show activity timeline")
    timeline_parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to show (default: 7)",
    )
    
    # Stats command
    stats_parser = subparsers.add_parser("stats", help="Show database statistics")

    # Cache command
    cache_parser = subparsers.add_parser("cache", help="Show transcription cache statistics")

    # Doctor command
    doctor_parser = subparsers.add_parser(
        "doctor", help="Show configured local and outbound network destinations"
    )

    subparsers.add_parser("data-dir", help="Print Tempo's exact local data directory")

    purge_parser = subparsers.add_parser("purge", help="Delete local Tempo data")
    purge_targets = purge_parser.add_mutually_exclusive_group(required=True)
    purge_targets.add_argument("--screenshots", action="store_true", help="Delete screenshots only")
    purge_targets.add_argument("--all", action="store_true", help="Delete the database, settings, state, screenshots, and caches")
    purge_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")

    # Backfill command
    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Generate transcriptions for screenshots missing them"
    )
    backfill_parser.add_argument(
        "--model",
        default="gemini-3.8-flash",
        help="Model to use for transcription (default: gemini-3.8-flash)",
    )
    backfill_parser.add_argument(
        "--api-key",
        dest="api_key",
        help="API key (defaults to TEMPO_LM_API_KEY or the selected provider's key variable)",
    )
    backfill_parser.add_argument(
        "--api-base",
        dest="api_base",
        help="API base URL (for OpenAI-compatible servers)",
    )
    backfill_parser.add_argument(
        "--vertex-express",
        action="store_true",
        default=_env_flag("GEMINI_VERTEXAI_EXPRESS"),
        help="Use Vertex AI Express Mode with VERTEX_EXPRESS_API_KEY",
    )
    backfill_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of screenshots to process",
    )
    backfill_parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Number of concurrent API requests (default: 5)",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without making API calls",
    )
    backfill_parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed error information",
    )

    args = parser.parse_args()
    
    if args.command == "start":
        _run_async(cmd_start(args))
    elif args.command == "serve":
        _run_async(cmd_serve(args))
    elif args.command == "query":
        _run_async(cmd_query(args))
    elif args.command == "timeline":
        _run_async(cmd_timeline(args))
    elif args.command == "stats":
        _run_async(cmd_stats(args))
    elif args.command == "cache":
        _run_async(cmd_cache(args))
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "data-dir":
        cmd_data_dir(args)
    elif args.command == "purge":
        cmd_purge(args)
    elif args.command == "backfill":
        _run_async(cmd_backfill(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
