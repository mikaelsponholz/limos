"""PostgreSQL persistence (Aiven) with the same contract as MemoryStore.

Every engine transaction runs in one database transaction and takes one advisory
lock first, so engine operations are serialized across processes the same way the
memory store serializes them across threads. The database also enforces the trust
rules itself (see migrations/001_init.sql): orders, steps and history are append-only,
the step chain must link, and a decided approval stays decided.

psycopg is imported here only, so memory and file stores work without it.
The connection URL comes from DATABASE_URL and is never logged.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock, get_ident
from typing import Any

from psycopg import Connection, errors, sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from gateway.schemas import Order, PastOrder, Quote, RequestDetail, Step
from gateway.store.models import ApprovalRecord

MIGRATIONS = Path(__file__).parent / "migrations"
# Any fixed 64-bit number; every Limos process uses the same one.
_ENGINE_LOCK = 0x4C494D4F53  # "LIMOS"


def _payload(model: BaseModel) -> Jsonb:
    return Jsonb(model.model_dump(mode="json"))


def _load[M: BaseModel](model: type[M], text: str) -> M:
    # JSON mode, because the contract models are strict and stored as JSON text.
    return model.model_validate_json(text)


class PostgresStore:
    def __init__(
        self,
        database_url: str,
        *,
        schema: str | None = None,
        max_size: int | None = None,
        migrate: bool = True,
    ) -> None:
        if not database_url:
            raise ValueError("Postgres store needs DATABASE_URL.")
        kwargs: dict[str, Any] = {"autocommit": True}
        if schema is not None:
            if not schema.replace("_", "").isalnum():
                raise ValueError("Schema name must be letters, digits and underscores.")
            kwargs["options"] = f"-c search_path={schema}"
        size = max_size if max_size is not None else int(os.getenv("DB_POOL_MAX", "3"))
        self._pool: ConnectionPool[Connection[Any]] = ConnectionPool(
            database_url, min_size=1, max_size=max(1, size), kwargs=kwargs, open=True
        )
        self._tx: ContextVar[tuple[Connection[Any], int, int] | None] = ContextVar(
            f"limos_pg_tx_{id(self)}", default=None
        )
        # Writers in this process queue here first, holding no connection while they wait.
        # Without it, threads waiting for the advisory lock each hold a pool connection,
        # and the writer that holds the lock could starve for one under heavy polling.
        self._lock = RLock()
        if migrate:
            self.migrate()

    def close(self) -> None:
        self._pool.close()

    def migrate(self) -> list[str]:
        scripts = sorted(MIGRATIONS.glob("*.sql"))
        with self._pool.connection() as conn, conn.transaction():
            # Two processes starting at once must not run the DDL at the same time.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ENGINE_LOCK + 1,))
            for script in scripts:
                conn.execute(sql.SQL(script.read_text(encoding="utf-8")))
        return [script.name for script in scripts]

    # ---- transactions ----

    def _current_transaction(self) -> tuple[Connection[Any], int, int] | None:
        current = self._tx.get()
        # to_thread() and copy_context() copy ContextVars, not connection ownership.
        if current is not None and current[2] != get_ident():
            raise RuntimeError("Postgres transaction belongs to another thread.")
        return current

    @contextmanager
    def transaction(self) -> Iterator[None]:
        current = self._current_transaction()
        if current is not None:
            conn, depth, owner = current
            token = self._tx.set((conn, depth + 1, owner))
            try:
                yield
            finally:
                self._tx.reset(token)
            return
        with self._lock, self._pool.connection() as conn:
            try:
                with conn.transaction():
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ENGINE_LOCK,))
                    token = self._tx.set((conn, 1, get_ident()))
                    try:
                        yield
                    finally:
                        self._tx.reset(token)
            except errors.IntegrityError as error:
                # A database guarantee caught what the Python checks did not.
                raise ValueError(f"Store rule violated: {error.diag.message_primary}") from error

    @contextmanager
    def _conn(self) -> Iterator[Connection[Any]]:
        current = self._current_transaction()
        if current is not None:
            yield current[0]
        else:
            with self._pool.connection() as conn:
                yield conn

    def _write(self) -> Connection[Any]:
        current = self._current_transaction()
        assert current is not None, "writes run inside transaction()"
        return current[0]

    def _rows(self, query: str, params: tuple[Any, ...]) -> list[str]:
        with self._conn() as conn:
            return [row[0] for row in conn.execute(query, params).fetchall()]

    def _one(self, query: str, params: tuple[Any, ...]) -> str | None:
        rows = self._rows(query, params)
        return rows[0] if rows else None

    # ---- ownership ----

    def _claim_request(self, request_id: str, buyer_id: str) -> None:
        if not request_id:
            return
        conn = self._write()
        conn.execute(
            "INSERT INTO limos_request_owners (request_id, buyer_id) VALUES (%s, %s) "
            "ON CONFLICT (request_id) DO NOTHING",
            (request_id, buyer_id),
        )
        if self.request_buyer(request_id) != buyer_id:
            raise ValueError("Request belongs to another buyer.")

    def request_buyer(self, request_id: str) -> str | None:
        return self._one(
            "SELECT buyer_id FROM limos_request_owners WHERE request_id = %s", (request_id,)
        )

    # ---- requests ----

    def save_request(self, request: RequestDetail) -> None:
        with self.transaction():
            self._claim_request(request.request_id, request.buyer_id)
            self._write().execute(
                "INSERT INTO limos_requests (request_id, buyer_id, payload) VALUES (%s, %s, %s) "
                "ON CONFLICT (request_id) DO UPDATE SET payload = EXCLUDED.payload",
                (request.request_id, request.buyer_id, _payload(request)),
            )

    def get_request(self, request_id: str, buyer_id: str) -> RequestDetail | None:
        text = self._one(
            "SELECT payload::text FROM limos_requests WHERE request_id = %s AND buyer_id = %s",
            (request_id, buyer_id),
        )
        return _load(RequestDetail, text) if text else None

    def list_requests(self, buyer_id: str) -> list[RequestDetail]:
        rows = self._rows(
            "SELECT payload::text FROM limos_requests WHERE buyer_id = %s ORDER BY pos",
            (buyer_id,),
        )
        return [_load(RequestDetail, t) for t in rows]

    # ---- quotes ----

    def save_quote(self, quote: Quote) -> None:
        with self.transaction():
            owner = self._one(
                "SELECT buyer_id FROM limos_quotes WHERE quote_id = %s", (quote.quote_id,)
            )
            if owner is not None and owner != quote.buyer_id:
                raise ValueError("Quote belongs to another buyer.")
            self._claim_request(quote.request_id, quote.buyer_id)
            self._write().execute(
                "INSERT INTO limos_quotes (quote_id, buyer_id, request_id, payload) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (quote_id) DO UPDATE SET payload = EXCLUDED.payload",
                (quote.quote_id, quote.buyer_id, quote.request_id, _payload(quote)),
            )

    def get_quote(self, quote_id: str, buyer_id: str) -> Quote | None:
        text = self._one(
            "SELECT payload::text FROM limos_quotes WHERE quote_id = %s AND buyer_id = %s",
            (quote_id, buyer_id),
        )
        return _load(Quote, text) if text else None

    def list_quotes(self, request_id: str, buyer_id: str) -> list[Quote]:
        rows = self._rows(
            "SELECT payload::text FROM limos_quotes WHERE buyer_id = %s AND request_id = %s "
            "ORDER BY pos",
            (buyer_id, request_id),
        )
        return [_load(Quote, t) for t in rows]

    # ---- approvals ----

    def save_approval(self, approval: ApprovalRecord) -> None:
        with self.transaction():
            owner = self._one(
                "SELECT buyer_id FROM limos_approvals WHERE approval_id = %s",
                (approval.approval_id,),
            )
            if owner is not None and owner != approval.buyer_id:
                raise ValueError("Approval belongs to another buyer.")
            self._write().execute(
                "INSERT INTO limos_approvals (approval_id, buyer_id, status, payload) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (approval_id) "
                "DO UPDATE SET status = EXCLUDED.status, payload = EXCLUDED.payload",
                (approval.approval_id, approval.buyer_id, approval.status, _payload(approval)),
            )

    def get_approval(self, approval_id: str, buyer_id: str) -> ApprovalRecord | None:
        text = self._one(
            "SELECT payload::text FROM limos_approvals WHERE approval_id = %s AND buyer_id = %s",
            (approval_id, buyer_id),
        )
        return _load(ApprovalRecord, text) if text else None

    def list_approvals(self, buyer_id: str) -> list[ApprovalRecord]:
        rows = self._rows(
            "SELECT payload::text FROM limos_approvals WHERE buyer_id = %s ORDER BY pos",
            (buyer_id,),
        )
        return [_load(ApprovalRecord, t) for t in rows]

    # ---- orders ----

    def save_order(self, order: Order, buyer_id: str) -> None:
        with self.transaction():
            quote = self.get_quote(order.quote_id, buyer_id)
            if quote is None or quote.request_id != order.request_id:
                raise ValueError("Order requires a quote of the same buyer and request.")
            if self._one(
                "SELECT order_id FROM limos_orders WHERE order_id = %s", (order.order_id,)
            ):
                raise ValueError("Orders are immutable.")
            if self._one(
                "SELECT order_id FROM limos_orders WHERE buyer_id = %s AND idempotency_key = %s",
                (buyer_id, order.idempotency_key),
            ):
                raise ValueError("Idempotency key is already in use.")
            if order.request_id and self.order_for_request(order.request_id, buyer_id):
                raise ValueError("Request already has an order.")
            self._write().execute(
                "INSERT INTO limos_orders "
                "(order_id, buyer_id, quote_id, request_id, idempotency_key, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    order.order_id,
                    buyer_id,
                    order.quote_id,
                    order.request_id,
                    order.idempotency_key,
                    _payload(order),
                ),
            )

    def get_order(self, order_id: str, buyer_id: str) -> Order | None:
        text = self._one(
            "SELECT o.payload::text FROM limos_orders o "
            "JOIN limos_quotes q ON q.quote_id = o.quote_id "
            "WHERE o.order_id = %s AND q.buyer_id = %s",
            (order_id, buyer_id),
        )
        return _load(Order, text) if text else None

    def order_for_request(self, request_id: str, buyer_id: str) -> Order | None:
        if not request_id or self.request_buyer(request_id) != buyer_id:
            return None
        text = self._one(
            "SELECT payload::text FROM limos_orders WHERE request_id = %s", (request_id,)
        )
        return _load(Order, text) if text else None

    def get_idempotent_order(self, key: str, buyer_id: str) -> Order | None:
        order_id = self._one(
            "SELECT order_id FROM limos_orders WHERE buyer_id = %s AND idempotency_key = %s",
            (buyer_id, key),
        )
        return self.get_order(order_id, buyer_id) if order_id else None

    # ---- decision log ----

    def get_steps(self, request_id: str, buyer_id: str) -> list[Step]:
        if self.request_buyer(request_id) != buyer_id:
            return []
        rows = self._rows(
            "SELECT payload::text FROM limos_steps WHERE request_id = %s ORDER BY seq",
            (request_id,),
        )
        return [_load(Step, t) for t in rows]

    def append_step(self, request_id: str, buyer_id: str, step: Step) -> None:
        with self.transaction():
            self._claim_request(request_id, buyer_id)
            with self._conn() as conn:
                last = conn.execute(
                    "SELECT seq, hash FROM limos_steps WHERE request_id = %s "
                    "ORDER BY seq DESC LIMIT 1",
                    (request_id,),
                ).fetchone()
            seq, prev = (last[0], last[1]) if last else (0, None)
            if step.seq != seq + 1 or step.prev_hash != prev:
                raise ValueError("Step must extend the decision log.")
            self._write().execute(
                "INSERT INTO limos_steps (request_id, seq, prev_hash, hash, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                (request_id, step.seq, step.prev_hash, step.hash, _payload(step)),
            )

    # ---- customer history ----

    def append_past_order(self, customer_id: str, buyer_id: str, order: PastOrder) -> None:
        with self.transaction():
            self._write().execute(
                "INSERT INTO limos_past_orders (buyer_id, customer_id, payload) "
                "VALUES (%s, %s, %s)",
                (buyer_id, customer_id, _payload(order)),
            )

    def get_past_orders(self, customer_id: str, buyer_id: str) -> list[PastOrder]:
        rows = self._rows(
            "SELECT payload::text FROM limos_past_orders "
            "WHERE buyer_id = %s AND customer_id = %s ORDER BY pos",
            (buyer_id, customer_id),
        )
        return [_load(PastOrder, t) for t in rows]
