"""In-memory Store. Same behaviour as PostgresStore, for local dev and tests."""
from __future__ import annotations

import copy
import threading
from typing import Optional

from .base import (
    ChainConflict, NotFound, Obj, OrderConflict, Store, approval_keys, check_link, now_iso,
)


class MemoryStore(Store):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requests: dict[str, Obj] = {}
        self._quotes: dict[str, Obj] = {}
        self._approvals: dict[str, Obj] = {}
        self._orders: dict[str, Obj] = {}
        self._steps: dict[str, list[Obj]] = {}

    # Requests
    def save_request(self, req: Obj) -> Obj:
        with self._lock:
            self._requests[req["request_id"]] = copy.deepcopy(req)
            return copy.deepcopy(req)

    def update_request_status(self, request_id: str, status: str) -> None:
        with self._lock:
            if request_id not in self._requests:
                raise NotFound(request_id)
            self._requests[request_id]["status"] = status

    def get_request(self, request_id: str) -> Optional[Obj]:
        with self._lock:
            r = self._requests.get(request_id)
            return copy.deepcopy(r) if r else None

    def list_requests(self, buyer_id: Optional[str] = None, limit: int = 20) -> list[Obj]:
        with self._lock:
            rows = [r for r in self._requests.values() if buyer_id is None or r.get("buyer_id") == buyer_id]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return copy.deepcopy(rows[:limit])

    # Quotes
    def save_quote(self, quote: Obj) -> Obj:
        with self._lock:
            self._quotes[quote["quote_id"]] = copy.deepcopy(quote)
            return copy.deepcopy(quote)

    def update_quote_status(self, quote_id: str, status: str) -> None:
        with self._lock:
            if quote_id not in self._quotes:
                raise NotFound(quote_id)
            self._quotes[quote_id]["status"] = status

    def get_quote(self, quote_id: str) -> Optional[Obj]:
        with self._lock:
            q = self._quotes.get(quote_id)
            return copy.deepcopy(q) if q else None

    # Approvals
    def create_approval(self, approval: Obj) -> Obj:
        with self._lock:
            keys = approval_keys(approval)
            if keys["quote_id"] not in self._quotes:
                raise NotFound(f"quote {keys['quote_id']} must be saved before its approval")
            a = copy.deepcopy(approval)
            a.setdefault("status", "PENDING")
            a.setdefault("decided_by", None)
            a.setdefault("decided_at", None)
            a.setdefault("comment", None)
            a["_keys"] = keys
            self._approvals[a["approval_id"]] = a
            return self._public(a)

    @staticmethod
    def _public(a: Obj) -> Obj:
        out = copy.deepcopy(a)
        out.pop("_keys", None)
        return out

    def get_approval(self, approval_id: str) -> Optional[Obj]:
        with self._lock:
            a = self._approvals.get(approval_id)
            return self._public(a) if a else None

    def list_approvals(self, status: Optional[str] = "PENDING", limit: int = 50) -> list[Obj]:
        with self._lock:
            rows = [a for a in self._approvals.values() if status is None or a["status"] == status]
            rows.sort(key=lambda a: a["_keys"]["created_at"], reverse=True)
            return [self._public(a) for a in rows[:limit]]

    def decide_approval(self, approval_id, status, decided_by, approved_quote_id=None,
                        approved_hash=None, comment=None) -> bool:
        if status not in ("APPROVED", "REJECTED", "EXPIRED"):
            raise ValueError(f"bad decision status {status}")
        if status == "APPROVED" and not (approved_quote_id and approved_hash and decided_by):
            raise ValueError("APPROVED needs decided_by, approved_quote_id and approved_hash")
        with self._lock:
            a = self._approvals.get(approval_id)
            if a is None or a["status"] != "PENDING":
                return False
            a.update(status=status, decided_by=decided_by, decided_at=now_iso(), comment=comment,
                     approved_quote_id=approved_quote_id, approved_hash=approved_hash)
            return True

    # Orders
    def create_order(self, order: Obj) -> Obj:
        with self._lock:
            key = order["idempotency_key"]
            for o in self._orders.values():
                if o["idempotency_key"] == key:
                    return copy.deepcopy(o)
            if order["quote_id"] not in self._quotes:
                raise NotFound(f"quote {order['quote_id']} must be saved before its order")
            rid = order.get("request_id")
            if rid is not None and order.get("status") != "FAILED":
                for o in self._orders.values():
                    if o.get("request_id") == rid and o.get("status") != "FAILED":
                        raise OrderConflict(f"request {rid} already has order {o['order_id']}")
            self._orders[order["order_id"]] = copy.deepcopy(order)
            return copy.deepcopy(order)

    def get_order(self, order_id: str) -> Optional[Obj]:
        with self._lock:
            o = self._orders.get(order_id)
            return copy.deepcopy(o) if o else None

    # Decision log
    def append_step(self, request_id: str, step: Obj) -> Obj:
        with self._lock:
            chain = self._steps.setdefault(request_id, [])
            check_link(chain[-1] if chain else None, step, request_id)
            if any(s["hash"] == step["hash"] for c in self._steps.values() for s in c):
                raise ChainConflict(f"hash {step['hash']} already exists")
            chain.append(copy.deepcopy(step))
            return copy.deepcopy(step)

    def get_steps(self, request_id: str) -> list[Obj]:
        with self._lock:
            return copy.deepcopy(self._steps.get(request_id, []))
