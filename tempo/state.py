# state.py

"""
Persistent state management for Tempo.

Handles saving/loading state across program restarts, sleep, and crashes.
"""

from __future__ import annotations
import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class BufferState:
    """Persistent buffer state."""
    operations: List[Dict[str, Any]]  # List of {entity_id, text, timestamp}
    centroid_b64: Optional[str] = None  # Deprecated: unused, kept for backward-compat with old state files
    last_operation_time: Optional[str] = None  # ISO format


@dataclass
class JobState:
    """Persistent job scheduling state."""
    last_activity_inference: Optional[str]  # ISO format timestamp
    last_action_build: Optional[str]  # ISO format timestamp


@dataclass 
class TempoState:
    """Complete Tempo state for persistence."""
    buffer: BufferState
    jobs: JobState
    last_active: str  # When Tempo was last running


class StateManager:
    """
    Manages persistent state for Tempo.
    
    State is saved to ~/.cache/tempo/state.json
    """
    
    def __init__(self, data_directory: str = "~/.cache/tempo"):
        self.data_directory = os.path.expanduser(data_directory)
        self.state_file = os.path.join(self.data_directory, "state.json")
        os.makedirs(self.data_directory, exist_ok=True)
    
    def save(self, state: TempoState) -> None:
        """Save state to disk."""
        state_dict = {
            "buffer": asdict(state.buffer),
            "jobs": asdict(state.jobs),
            "last_active": state.last_active,
        }
        
        # Write atomically using temp file
        temp_file = self.state_file + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(state_dict, f, indent=2)
        os.rename(temp_file, self.state_file)
    
    def load(self) -> Optional[TempoState]:
        """Load state from disk."""
        if not os.path.exists(self.state_file):
            return None
        
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            
            return TempoState(
                buffer=BufferState(
                    operations=data.get("buffer", {}).get("operations", []),
                    centroid_b64=data.get("buffer", {}).get("centroid_b64"),
                    last_operation_time=data.get("buffer", {}).get("last_operation_time"),
                ),
                jobs=JobState(
                    last_activity_inference=data.get("jobs", {}).get("last_activity_inference"),
                    last_action_build=data.get("jobs", {}).get("last_action_build"),
                ),
                last_active=data.get("last_active", datetime.utcnow().isoformat()),
            )
        except (json.JSONDecodeError, KeyError) as e:
            print(f"⚠️ Could not load state file: {e}")
            return None
    
    def clear(self) -> None:
        """Clear persisted state."""
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
    
    def get_inactive_duration(self) -> Optional[float]:
        """
        Get how long Tempo has been inactive (in seconds).
        
        Returns None if no previous state exists.
        """
        state = self.load()
        if not state:
            return None
        
        try:
            last_active = datetime.fromisoformat(state.last_active)
            return (datetime.utcnow() - last_active).total_seconds()
        except (ValueError, TypeError):
            return None


def encode_embedding(embedding: bytes) -> str:
    """Encode embedding bytes to base64 string."""
    import base64
    return base64.b64encode(embedding).decode("utf-8")


def decode_embedding(b64_string: str) -> bytes:
    """Decode base64 string to embedding bytes."""
    import base64
    return base64.b64decode(b64_string.encode("utf-8"))

