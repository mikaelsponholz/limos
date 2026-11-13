"""File-backed Store: MemoryStore that writes a JSON snapshot after every change.

For local dev without a database (STORE=file). Path from env STORE_PATH
(default .limos-store.json in the working directory, gitignored).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .memory import MemoryStore

_TABLES = ("_requests", "_quotes", "_approvals", "_orders", "_steps")
_WRITES = (
    "save_request", "update_request_status", "save_quote", "update_quote_status",
    "create_approval", "decide_approval", "create_order", "append_step",
)


class FileStore(MemoryStore):
    def __init__(self, path: Optional[str] = None) -> None:
        super().__init__()
        self.path = Path(path or os.environ.get("STORE_PATH", ".limos-store.json"))
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for t in _TABLES:
                setattr(self, t, data.get(t.lstrip("_"), {}))

    def _flush(self) -> None:
        data = {t.lstrip("_"): getattr(self, t) for t in _TABLES}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)  # atomic swap, a crash never leaves half a file


def _persisting(name: str):
    method = getattr(MemoryStore, name)

    def wrapper(self: FileStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            result = method(self, *args, **kwargs)
            self._flush()
            return result

    wrapper.__name__ = name
    wrapper.__doc__ = method.__doc__
    return wrapper


for _name in _WRITES:
    setattr(FileStore, _name, _persisting(_name))
