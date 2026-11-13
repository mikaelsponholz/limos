"""Store interface for Limos. Plain deterministic code, no LLM calls.

Objects are plain dicts shaped like docs/api_contract.md v0.2. The full object is
kept as-is (timestamps stay the ISO 8601 strings the gateway wrote), and a few key
columns are pulled out so the database can enforce the trust rules.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Optional

Obj = dict[str, Any]


class StorageError(Exception):
    """Base class for storage errors the gateway may want to catch."""


class NotFound(StorageError):
    pass


class ChainConflict(StorageError):
    """A decision-log step does not link to the current last step (wrong seq or prev_hash)."""


class OrderConflict(StorageError):
    """The request already has a live order (rule: one order per request)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def approval_keys(approval: Obj) -> dict[str, Any]:
    """Pull key columns out of an approval object (list or detail shape from the contract)."""
    quote = approval.get("quote") or {}
    request = approval.get("request") or {}
    quote_id = approval.get("quote_id") or quote.get("quote_id")
    if not quote_id:
        raise ValueError("approval needs quote_id (or a nested quote)")
    return {
        "approval_id": approval["approval_id"],
        "request_id": approval.get("request_id") or quote.get("request_id") or request.get("request_id"),
        "quote_id": quote_id,
        "buyer_id": approval.get("buyer_id") or quote.get("buyer_id") or request.get("buyer_id") or "unknown",
        "created_at": approval.get("created_at") or quote.get("created_at") or now_iso(),
        "expires_at": approval.get("expires_at") or quote.get("expires_at"),
    }


def check_link(prev: Optional[Obj], step: Obj, request_id: str) -> None:
    """Raise ChainConflict unless `step` is the correct next step after `prev`."""
    if prev is None:
        if step.get("seq") != 1 or step.get("prev_hash") is not None:
            raise ChainConflict(f"first step of {request_id} must have seq 1 and prev_hash null")
        return
    if step.get("seq") != prev["seq"] + 1 or step.get("prev_hash") != prev["hash"]:
        raise ChainConflict(
            f"step {step.get('seq')} of {request_id} does not link to step {prev['seq']}"
        )


def chain_is_valid(steps: list[Obj], recompute: Optional[Callable[[Obj], str]] = None) -> bool:
    """True if every step links to the one before. `recompute` (from the audit module)
    can also re-derive each hash from the step content."""
    prev = None
    for step in steps:
        try:
            check_link(prev, step, "chain")
        except ChainConflict:
            return False
        if recompute is not None and recompute(step) != step["hash"]:
            return False
        prev = step
    return True


class Store(ABC):
    # Requests
    @abstractmethod
    def save_request(self, req: Obj) -> Obj: ...
    @abstractmethod
    def update_request_status(self, request_id: str, status: str) -> None: ...
    @abstractmethod
    def get_request(self, request_id: str) -> Optional[Obj]: ...
    @abstractmethod
    def list_requests(self, buyer_id: Optional[str] = None, limit: int = 20) -> list[Obj]: ...

    # Quotes
    @abstractmethod
    def save_quote(self, quote: Obj) -> Obj: ...
    @abstractmethod
    def update_quote_status(self, quote_id: str, status: str) -> None: ...
    @abstractmethod
    def get_quote(self, quote_id: str) -> Optional[Obj]: ...

    # Approvals
    @abstractmethod
    def create_approval(self, approval: Obj) -> Obj: ...
    @abstractmethod
    def get_approval(self, approval_id: str) -> Optional[Obj]: ...
    @abstractmethod
    def list_approvals(self, status: Optional[str] = "PENDING", limit: int = 50) -> list[Obj]: ...
    @abstractmethod
    def decide_approval(
        self,
        approval_id: str,
        status: str,
        decided_by: str,
        approved_quote_id: Optional[str] = None,
        approved_hash: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> bool:
        """Decide a PENDING approval. Returns False if it was already decided or expired."""

    # Orders
    @abstractmethod
    def create_order(self, order: Obj) -> Obj:
        """Same idempotency_key returns the existing order. A second live order for the
        same request raises OrderConflict."""
    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Obj]: ...

    # Decision log
    @abstractmethod
    def append_step(self, request_id: str, step: Obj) -> Obj:
        """Append one step. Raises ChainConflict if seq/prev_hash do not link."""
    @abstractmethod
    def get_steps(self, request_id: str) -> list[Obj]: ...

    def verify_chain(self, request_id: str, recompute: Optional[Callable[[Obj], str]] = None) -> bool:
        return chain_is_valid(self.get_steps(request_id), recompute)

    def close(self) -> None:
        pass
