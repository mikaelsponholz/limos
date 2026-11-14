"""Single-process JSON persistence with atomic replacement on transaction commit."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from gateway.store.memory import MemoryStore
from gateway.store.models import StoreState


class FileStore(MemoryStore):
    def __init__(self, path: str | Path = Path(".runtime/store.json")) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            self._state = StoreState.model_validate_json(self.path.read_bytes())

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(self._state.model_dump_json())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
