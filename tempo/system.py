# system.py

"""
Tempo System - Sets up and manages all Tempo components.

This module handles initialization, state management, and component setup.
Pipeline execution logic is handled by the pipeline modules themselves.
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from tempo.db import Database
from tempo.providers import create_provider
from tempo.buffer import OperationBuffer
from tempo.state import StateManager, TempoState, BufferState, JobState
from tempo.buffer import BufferedOperation


logger = logging.getLogger(__name__)


class TempoSystem:
    """
    System setup and component management.
    
    Handles:
    - Database initialization
    - Component creation (provider, buffer, observer)
    - State restoration and saving
    - Component lifecycle
    """
    
    def __init__(
        self,
        model_name: str = "gemini-3.8-flash",
        platform: str = "macos",
        debug: bool = False,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        gemini_vertexai: bool = False,
        gemini_vertexai_express: bool = False,
        vertex_project: Optional[str] = None,
        vertex_location: Optional[str] = None,
        buffer_time_threshold: float = 300.0,
        buffer_max_size: int = 50,
        db_name: str = "tempo.db",
        data_directory: str = "~/.cache/tempo",
        ignore_visible_app_names: Optional[list[str]] = None,
        enable_content_filter: bool = True,
    ):
        """
        Initialize Tempo system configuration.

        Args:
            model_name: LLM model to use
            platform: Platform backend ('macos' or 'gnome')
            debug: Enable debug logging
            api_base: API base URL (for vLLM)
            api_key: API key
            buffer_time_threshold: Buffer inactivity threshold in seconds
            buffer_max_size: Maximum buffer size
            db_name: Database file name (default: "tempo.db")
            ignore_visible_app_names: App names to skip capture when visible
            enable_content_filter: Enable local OCR + PII content filter
        """
        self.model_name = model_name
        self.platform = platform
        self.debug = debug
        self.api_base = api_base
        self.api_key = api_key
        self.gemini_vertexai = gemini_vertexai
        self.gemini_vertexai_express = gemini_vertexai_express
        self.vertex_project = vertex_project
        self.vertex_location = vertex_location
        self.buffer_time_threshold = buffer_time_threshold
        self.buffer_max_size = buffer_max_size
        self.db_name = db_name
        self.data_directory = os.path.expanduser(data_directory)
        self.ignore_visible_app_names = ignore_visible_app_names or []
        self.enable_content_filter = enable_content_filter

        # Components (initialized in setup)
        self.db: Optional[Database] = None
        self.provider = None
        self.stage1_provider = None  # Optional cheaper/faster model just for Stage 1
        self.buffer: Optional[OperationBuffer] = None
        self.observer = None
        self.backend = None
        
        # State management
        self.state_manager = StateManager(data_directory=self.data_directory)
        self.last_activity_job: Optional[datetime] = None
        self.last_state_save: Optional[datetime] = None
    
    async def setup(self):
        """Set up all Tempo components."""
        print(" Starting Tempo...")
        
        # Initialize database
        print(f"  Initializing database: {self.db_name}...")
        self.db = Database(db_name=self.db_name, data_directory=self.data_directory)
        await self.db.connect()
        
        # Create LLM provider
        print(f"  Using model: {self.model_name}")
        model_lower = self.model_name.lower()
        if self.gemini_vertexai_express:
            print("  Provider: Vertex AI Express Mode")
        elif "gemini" in model_lower:
            print("  Provider: Google Gemini API")
        elif self.api_base:
            print(f"  API base: {self.api_base}")
        self.provider = create_provider(
            self.model_name,
            api_key=self.api_key,
            api_base=self.api_base,
            gemini_vertexai=self.gemini_vertexai,
            gemini_vertexai_express=self.gemini_vertexai_express,
            vertex_project=self.vertex_project,
            vertex_location=self.vertex_location,
        )

        # Optional Stage 1 override: a cheaper/faster model just for atomic
        # operation extraction. Set MODEL_NAME_STAGE1 env var to enable.
        # Falls through to self.provider when unset.
        stage1_model = os.environ.get("MODEL_NAME_STAGE1", "").strip()
        if stage1_model and stage1_model != self.model_name:
            print(f"  Stage 1 model override: {stage1_model}")
            self.stage1_provider = create_provider(
                stage1_model,
                api_key=self.api_key,
                api_base=self.api_base,
                gemini_vertexai=self.gemini_vertexai,
                gemini_vertexai_express=self.gemini_vertexai_express,
                vertex_project=self.vertex_project,
                vertex_location=self.vertex_location,
            )

        # Create OS backend
        print(f"  Platform: {self.platform}")
        if self.platform == "macos":
            from tempo.os_backends import MacOSBackend
            self.backend = MacOSBackend(debug=self.debug)
        elif self.platform == "gnome":
            from tempo.os_backends import GnomeBackend
            self.backend = GnomeBackend(debug=self.debug)
        else:
            raise ValueError(f"Unknown platform: {self.platform}")
        
        # Create screen observer
        print("  Starting screen observer...")
        from tempo.observers.screen import Screen
        from tempo.exclusions import ExclusionController, ExclusionSettingsStore
        self.observer = Screen(
            model_name=self.model_name,
            os_backend=self.backend,
            debug=self.debug,
            api_key=self.api_key,
            api_base=self.api_base,
            gemini_vertexai=self.gemini_vertexai,
            gemini_vertexai_express=self.gemini_vertexai_express,
            vertex_project=self.vertex_project,
            vertex_location=self.vertex_location,
            ignore_visible_app_names=self.ignore_visible_app_names,
            enable_content_filter=self.enable_content_filter,
            screenshots_dir=os.path.join(self.data_directory, "screenshots"),
            exclusion_controller=ExclusionController(
                ExclusionSettingsStore(Path(self.data_directory) / "settings.json")
            ),
        )
        
        # Buffer will be created by pipeline orchestrator after state restoration
        self.last_state_save = datetime.utcnow()
    
    async def restore_state(self) -> tuple[Optional[OperationBuffer], Optional[datetime]]:
        """
        Restore state from disk.
        
        Returns:
            Tuple of (buffer, last_activity_job_time)
        """
        state = self.state_manager.load()
        
        if not state:
            print("  No previous state found, starting fresh")
            self.last_activity_job = datetime.utcnow()
            return None, self.last_activity_job
        
        print("  Restoring previous state...")
        
        # Restore job timestamps
        if state.jobs.last_activity_inference:
            try:
                self.last_activity_job = datetime.fromisoformat(state.jobs.last_activity_inference)
            except ValueError:
                self.last_activity_job = datetime.utcnow()
        else:
            self.last_activity_job = datetime.utcnow()
        
        # Create buffer with restored operations
        buffer = OperationBuffer(
            time_threshold_seconds=self.buffer_time_threshold,
            max_buffer_size=self.buffer_max_size,
            debug=self.debug,
        )
        
        # Restore buffer operations
        if state.buffer.operations:
            print(f"     Restoring {len(state.buffer.operations)} buffered operations")
            for op_data in state.buffer.operations:
                try:
                    timestamp = datetime.fromisoformat(op_data["timestamp"])
                except (ValueError, KeyError):
                    timestamp = datetime.utcnow()

                buffer.operations.append(BufferedOperation(
                    entity_id=op_data.get("entity_id"),
                    text=op_data.get("text", ""),
                    timestamp=timestamp,
                ))
        
        # Restore last operation time
        if state.buffer.last_operation_time:
            try:
                buffer.last_operation_time = datetime.fromisoformat(state.buffer.last_operation_time)
            except ValueError:
                pass
        
        # Start buffer timer if we have restored operations
        if buffer.operations:
            print(f"  Starting timer for {len(buffer.operations)} buffered operations")
            buffer.start_timer_if_needed()
        
        return buffer, self.last_activity_job
    
    async def save_state(self, buffer: OperationBuffer):
        """Save current state to disk."""
        # Serialize buffer operations
        buffer_ops = []
        for op in buffer.operations:
            op_data = {
                "entity_id": op.entity_id,
                "text": op.text,
                "timestamp": op.timestamp.isoformat() if op.timestamp else None,
            }
            buffer_ops.append(op_data)
        
        # Build state
        state = TempoState(
            buffer=BufferState(
                operations=buffer_ops,
                centroid_b64=None,  # No longer used - segmentation handled by LLM
                last_operation_time=buffer.last_operation_time.isoformat() if buffer.last_operation_time else None,
            ),
            jobs=JobState(
                last_activity_inference=self.last_activity_job.isoformat() if self.last_activity_job else None,
                last_action_build=None,
            ),
            last_active=datetime.utcnow().isoformat(),
        )
        
        self.state_manager.save(state)
    
    def get_inactive_duration(self) -> Optional[float]:
        """Get how long Tempo has been inactive (in seconds)."""
        return self.state_manager.get_inactive_duration()
    
    async def cleanup(self, buffer: Optional[OperationBuffer] = None, *, flush_buffer: bool = False):
        """Clean up all components.

        Notes:
        - `buffer.flush()` can be slow because it triggers downstream pipelines (LLM calls).
          For interactive stop (UI Ctrl+C), we prefer to *not* flush and instead persist state,
          so stop feels instant and pending ops are resumed on next start.
        """
        print("\n Stopping Tempo...")

        # Flush remaining buffer
        if flush_buffer and buffer and buffer.operations:
            print("  Flushing buffer...")
            await buffer.flush()

        # Save final state
        if buffer:
            print("  Saving state...")
            await self.save_state(buffer)

        if self.observer:
            await self.observer.close()

        # Checkpoint WAL before closing DB so data is fully merged
        if self.db:
            try:
                await self.db.checkpoint()
            except Exception as e:
                logger.warning("WAL checkpoint on shutdown failed: %s", e)
            await self.db.close()

        # Close provider HTTP client
        if self.provider:
            try:
                await self.provider.close()
            except Exception as e:
                logger.warning("Provider close failed: %s", e)

        if self.stage1_provider and self.stage1_provider is not self.provider:
            try:
                await self.stage1_provider.close()
            except Exception as e:
                logger.warning("Stage 1 provider close failed: %s", e)

        self.observer = None
        self.backend = None
        self.provider = None
        self.stage1_provider = None
        self.db = None

        print(" Goodbye!")
