"""Select a store without opening any file until explicitly constructed."""

from pathlib import Path

from gateway.settings import Settings
from gateway.store.base import Store
from gateway.store.file import FileStore
from gateway.store.memory import MemoryStore
from gateway.store.models import ApprovalRecord

__all__ = ["ApprovalRecord", "FileStore", "MemoryStore", "Store", "create_store"]


def create_store(settings: Settings, *, path: str | Path = Path(".runtime/store.json")) -> Store:
    match settings.store:
        case "memory":
            return MemoryStore()
        case "file":
            return FileStore(path)
        case "postgres":
            raise NotImplementedError("Postgres store is not available yet.")
        case _:
            raise ValueError(f"Unknown store implementation: {settings.store}.")
