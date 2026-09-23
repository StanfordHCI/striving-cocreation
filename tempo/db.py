# db.py

from __future__ import annotations
import asyncio
import os
import weakref
from typing import Optional, Tuple
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
    AsyncEngine,
)
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from tempo.models import Base


_DATABASE_WRITE_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _write_lock_for(db_name: str, data_directory: str) -> asyncio.Lock:
    """Share one write lock across every manager for the same SQLite file."""
    key = os.path.abspath(os.path.join(os.path.expanduser(data_directory), db_name))
    lock = _DATABASE_WRITE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DATABASE_WRITE_LOCKS[key] = lock
    return lock


# Enable SQLite foreign key support, WAL mode, and timeout
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Configure SQLite for better concurrency."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for better concurrency
    cursor.execute("PRAGMA busy_timeout=5000")  # Wait up to 5 seconds for locks
    cursor.close()


async def init_db(
    db_name: str = "tempo.db",
    data_directory: str = "~/.cache/tempo",
) -> Tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """
    Initialize the database and return the engine and session maker.
    
    Args:
        db_name: Name of the SQLite database file.
        data_directory: Directory to store the database file.
        
    Returns:
        Tuple of (engine, session_maker).
    """
    # Expand and create data directory
    data_directory = os.path.expanduser(data_directory)
    os.makedirs(data_directory, exist_ok=True)
    
    # Build database URL
    db_path = os.path.join(data_directory, db_name)
    db_url = f"sqlite+aiosqlite:///{db_path}"
    
    # Create async engine
    # Note: WAL mode and busy_timeout are set via PRAGMA in set_sqlite_pragma
    engine = create_async_engine(
        db_url,
        echo=False,
        future=True,
        pool_pre_ping=True,  # Verify connections before using
    )
    
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Create session maker
    Session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    return engine, Session


class Database:
    """
    Database manager class for Tempo.
    
    Uses a global write lock to serialize all database sessions, preventing
    SQLite "database is locked" errors in concurrent async code.
    
    Usage:
        db = Database()
        await db.connect()
        
        async with db.session() as session:
            # Use session... (automatically serialized)
            
        await db.close()
    """
    
    def __init__(
        self,
        db_name: str = "tempo.db",
        data_directory: str = "~/.cache/tempo",
        shared: bool = False,
    ):
        self.db_name = db_name
        self.data_directory = os.path.expanduser(data_directory)
        self.engine: Optional[AsyncEngine] = None
        self.Session: Optional[async_sessionmaker[AsyncSession]] = None
        # All managers for this file share the lock, including the long-lived
        # pipeline database and short-lived API query managers.
        self._write_lock = _write_lock_for(db_name, self.data_directory)
        self._init_lock = asyncio.Lock()
        self._shared = shared

    async def connect(self) -> None:
        """Initialize the database connection."""
        if self.engine is not None:
            return
        async with self._init_lock:
            if self.engine is None:
                self.engine, self.Session = await init_db(
                    db_name=self.db_name,
                    data_directory=self.data_directory,
                )

    async def close(self) -> None:
        """Close the database connection.

        No-op for shared instances — use force_close() instead.
        """
        if self._shared:
            return
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.Session = None

    async def force_close(self) -> None:
        """Close the database connection even if shared."""
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.Session = None
    
    @asynccontextmanager
    async def session(self):
        """
        Get a database session as an async context manager.
        
        Sessions are serialized via a global write lock to prevent
        SQLite concurrency errors. Only one session can be active at a time.
        
        Usage:
            async with db.session() as session:
                # Use session...
        """
        if self.Session is None:
            await self.connect()
        if self.Session is None:
            raise RuntimeError("Database not connected. Call connect() first.")

        # Acquire write lock - only one session at a time
        async with self._write_lock:
            # Re-check after acquiring lock — force_close() may have
            # nullified Session while we were waiting.
            if self.Session is None:
                await self.connect()
            if self.Session is None:
                raise RuntimeError("Database closed while waiting for lock.")
            async with self.Session() as session:
                async with session.begin():
                    yield session
    
    async def checkpoint(self) -> None:
        """Merge WAL back into main DB file and truncate.

        Should be called periodically (e.g. hourly) in long-running
        processes to prevent the ``-wal`` sidecar from growing unbounded.
        """
        if self.Session is None:
            return
        async with self._write_lock:
            async with self.Session() as session:
                await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                await session.commit()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


# Global database instance (can be overridden)
_db: Optional[Database] = None


def get_database() -> Database:
    """Get the global database instance."""
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_database(db: Database) -> None:
    """Set the global database instance."""
    global _db
    _db = db

