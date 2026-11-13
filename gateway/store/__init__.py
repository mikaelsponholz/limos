"""Limos storage layer. Use get_store() to pick the implementation from env STORE."""
from __future__ import annotations

import os

from .base import ChainConflict, NotFound, OrderConflict, StorageError, Store
from .file import FileStore
from .memory import MemoryStore

__all__ = [
    "Store", "MemoryStore", "FileStore", "get_store",
    "StorageError", "ChainConflict", "NotFound", "OrderConflict",
]


def get_store() -> Store:
    """STORE=file (default, same as gateway.settings), memory, or postgres (needs DATABASE_URL)."""
    kind = os.environ.get("STORE", "file").lower()
    if kind == "memory":
        return MemoryStore()
    if kind == "file":
        return FileStore()
    if kind == "postgres":
        from .postgres import PostgresStore  # imported lazily so memory mode needs no psycopg
        store = PostgresStore()
        store.migrate()
        return store
    raise ValueError(f"unknown STORE={kind!r}, use memory, file or postgres")
