"""Isolated snapshots and serialized, rollback-capable engine transactions."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from gateway.schemas import Order, PastOrder, Quote, RequestDetail, Step
from gateway.store.models import ApprovalRecord, StoreState


class MemoryStore:
    def __init__(self) -> None:
        self._state = StoreState()
        self._lock = RLock()
        self._depth = 0

    def _persist(self) -> None:
        pass

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            snapshot = self._state.model_copy(deep=True) if outer else None
            self._depth += 1
            try:
                yield
                if outer and self._state != snapshot:
                    self._persist()
            except BaseException:
                if snapshot is not None:
                    self._state = snapshot
                raise
            finally:
                self._depth -= 1

    def _claim_request(self, request_id: str, buyer_id: str) -> None:
        if request_id:
            owner = self._state.request_buyers.setdefault(request_id, buyer_id)
            if owner != buyer_id:
                raise ValueError("Request belongs to another buyer.")

    def request_buyer(self, request_id: str) -> str | None:
        with self._lock:
            return self._state.request_buyers.get(request_id)

    def save_request(self, request: RequestDetail) -> None:
        with self.transaction():
            self._claim_request(request.request_id, request.buyer_id)
            self._state.requests[request.request_id] = request.model_copy(deep=True)

    def get_request(self, request_id: str, buyer_id: str) -> RequestDetail | None:
        with self._lock:
            record = self._state.requests.get(request_id)
            return record.model_copy(deep=True) if record and record.buyer_id == buyer_id else None

    def list_requests(self, buyer_id: str) -> list[RequestDetail]:
        with self._lock:
            return [
                r.model_copy(deep=True)
                for r in self._state.requests.values()
                if r.buyer_id == buyer_id
            ]

    def save_quote(self, quote: Quote) -> None:
        with self.transaction():
            old = self._state.quotes.get(quote.quote_id)
            if old and old.buyer_id != quote.buyer_id:
                raise ValueError("Quote belongs to another buyer.")
            self._claim_request(quote.request_id, quote.buyer_id)
            self._state.quotes[quote.quote_id] = quote.model_copy(deep=True)

    def get_quote(self, quote_id: str, buyer_id: str) -> Quote | None:
        with self._lock:
            record = self._state.quotes.get(quote_id)
            return record.model_copy(deep=True) if record and record.buyer_id == buyer_id else None

    def list_quotes(self, request_id: str, buyer_id: str) -> list[Quote]:
        with self._lock:
            return [
                q.model_copy(deep=True)
                for q in self._state.quotes.values()
                if q.buyer_id == buyer_id and q.request_id == request_id
            ]

    def save_approval(self, approval: ApprovalRecord) -> None:
        with self.transaction():
            old = self._state.approvals.get(approval.approval_id)
            if old and old.buyer_id != approval.buyer_id:
                raise ValueError("Approval belongs to another buyer.")
            self._state.approvals[approval.approval_id] = approval.model_copy(deep=True)

    def get_approval(self, approval_id: str, buyer_id: str) -> ApprovalRecord | None:
        with self._lock:
            record = self._state.approvals.get(approval_id)
            return record.model_copy(deep=True) if record and record.buyer_id == buyer_id else None

    def list_approvals(self, buyer_id: str) -> list[ApprovalRecord]:
        with self._lock:
            return [
                a.model_copy(deep=True)
                for a in self._state.approvals.values()
                if a.buyer_id == buyer_id
            ]

    def save_order(self, order: Order, buyer_id: str) -> None:
        with self.transaction():
            quote = self.get_quote(order.quote_id, buyer_id)
            if quote is None or quote.request_id != order.request_id:
                raise ValueError("Order requires a quote of the same buyer and request.")
            if order.order_id in self._state.orders:
                raise ValueError("Orders are immutable.")
            index = self._state.idempotency.setdefault(buyer_id, {})
            if order.idempotency_key in index:
                raise ValueError("Idempotency key is already in use.")
            if order.request_id and self.order_for_request(order.request_id, buyer_id):
                raise ValueError("Request already has an order.")
            self._state.orders[order.order_id] = order.model_copy(deep=True)
            index[order.idempotency_key] = order.order_id

    def get_order(self, order_id: str, buyer_id: str) -> Order | None:
        with self._lock:
            record = self._state.orders.get(order_id)
            return (
                record.model_copy(deep=True)
                if record and self.get_quote(record.quote_id, buyer_id)
                else None
            )

    def order_for_request(self, request_id: str, buyer_id: str) -> Order | None:
        with self._lock:
            if not request_id or self.request_buyer(request_id) != buyer_id:
                return None
            return next(
                (
                    o.model_copy(deep=True)
                    for o in self._state.orders.values()
                    if o.request_id == request_id
                ),
                None,
            )

    def get_idempotent_order(self, key: str, buyer_id: str) -> Order | None:
        with self._lock:
            order_id = self._state.idempotency.get(buyer_id, {}).get(key)
            return self.get_order(order_id, buyer_id) if order_id else None

    def get_steps(self, request_id: str, buyer_id: str) -> list[Step]:
        with self._lock:
            if self.request_buyer(request_id) != buyer_id:
                return []
            return [s.model_copy(deep=True) for s in self._state.steps.get(request_id, [])]

    def append_step(self, request_id: str, buyer_id: str, step: Step) -> None:
        with self.transaction():
            self._claim_request(request_id, buyer_id)
            steps = self._state.steps.setdefault(request_id, [])
            if step.seq != len(steps) + 1 or step.prev_hash != (steps[-1].hash if steps else None):
                raise ValueError("Step must extend the decision log.")
            steps.append(step.model_copy(deep=True))

    def append_past_order(self, customer_id: str, buyer_id: str, order: PastOrder) -> None:
        with self.transaction():
            orders = self._state.past_orders.setdefault(buyer_id, {}).setdefault(customer_id, [])
            orders.append(order.model_copy(deep=True))

    def get_past_orders(self, customer_id: str, buyer_id: str) -> list[PastOrder]:
        with self._lock:
            return [
                o.model_copy(deep=True)
                for o in self._state.past_orders.get(buyer_id, {}).get(customer_id, [])
            ]
