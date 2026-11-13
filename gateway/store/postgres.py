"""PostgreSQL Store (Aiven). The connection string comes only from env DATABASE_URL.

Aiven Developer plan allows 15 connections for the whole team, so the pool is small:
min_size=1, max_size from env DB_POOL_MAX (default 3).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from psycopg import errors, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .base import (
    ChainConflict, NotFound, Obj, OrderConflict, Store, approval_keys, check_link,
)

MIGRATIONS = Path(__file__).parent / "migrations"
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresStore(Store):
    def __init__(self, dsn: Optional[str] = None, schema: Optional[str] = None,
                 max_size: Optional[int] = None) -> None:
        dsn = dsn or os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        if schema is not None and not _SCHEMA_RE.match(schema):
            raise ValueError("schema must be a plain lowercase identifier")
        self.schema = schema
        size = max_size or int(os.environ.get("DB_POOL_MAX", "3"))

        def configure(conn):
            if schema:
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                conn.commit()

        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=size, open=True, configure=configure,
            kwargs={"row_factory": dict_row, "application_name": "limos-storage"},
        )

    def close(self) -> None:
        self.pool.close()

    def migrate(self) -> None:
        """Run every migration file in order. They are idempotent."""
        with self.pool.connection() as conn:
            for f in sorted(MIGRATIONS.glob("*.sql")):
                conn.execute(f.read_text(encoding="utf-8"))

    # Helpers
    def _one(self, query: str, params: tuple) -> Optional[Obj]:
        with self.pool.connection() as conn:
            row = conn.execute(query, params).fetchone()
            return row["payload"] if row else None

    # Requests
    def save_request(self, req: Obj) -> Obj:
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO requests (request_id, buyer_id, status, created_at, payload)
                   VALUES (%s, %s, %s, %s::timestamptz, %s)
                   ON CONFLICT (request_id) DO UPDATE
                   SET buyer_id = EXCLUDED.buyer_id, status = EXCLUDED.status,
                       payload = EXCLUDED.payload, updated_at = now()""",
                (req["request_id"], req["buyer_id"], req["status"], req["created_at"], Jsonb(req)),
            )
        return req

    def update_request_status(self, request_id: str, status: str) -> None:
        with self.pool.connection() as conn:
            cur = conn.execute(
                """UPDATE requests SET status = %s, updated_at = now(),
                          payload = jsonb_set(payload, '{status}', to_jsonb(%s::text))
                   WHERE request_id = %s""",
                (status, status, request_id),
            )
            if cur.rowcount == 0:
                raise NotFound(request_id)

    def get_request(self, request_id: str) -> Optional[Obj]:
        return self._one("SELECT payload FROM requests WHERE request_id = %s", (request_id,))

    def list_requests(self, buyer_id: Optional[str] = None, limit: int = 20) -> list[Obj]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT payload FROM requests WHERE %s::text IS NULL OR buyer_id = %s
                   ORDER BY created_at DESC LIMIT %s""",
                (buyer_id, buyer_id, limit),
            ).fetchall()
        return [r["payload"] for r in rows]

    # Quotes
    def save_quote(self, quote: Obj) -> Obj:
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO quotes (quote_id, request_id, buyer_id, status, content_hash,
                                       expires_at, created_at, payload)
                   VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s)
                   ON CONFLICT (quote_id) DO UPDATE
                   SET status = EXCLUDED.status, content_hash = EXCLUDED.content_hash,
                       expires_at = EXCLUDED.expires_at, payload = EXCLUDED.payload""",
                (quote["quote_id"], quote.get("request_id"), quote["buyer_id"], quote["status"],
                 quote["content_hash"], quote["expires_at"], quote["created_at"], Jsonb(quote)),
            )
        return quote

    def update_quote_status(self, quote_id: str, status: str) -> None:
        with self.pool.connection() as conn:
            cur = conn.execute(
                """UPDATE quotes SET status = %s,
                          payload = jsonb_set(payload, '{status}', to_jsonb(%s::text))
                   WHERE quote_id = %s""",
                (status, status, quote_id),
            )
            if cur.rowcount == 0:
                raise NotFound(quote_id)

    def get_quote(self, quote_id: str) -> Optional[Obj]:
        return self._one("SELECT payload FROM quotes WHERE quote_id = %s", (quote_id,))

    # Approvals
    _APPROVAL_COLS = """payload || jsonb_build_object(
        'status', status, 'decided_by', decided_by,
        'decided_at', to_char(decided_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'),
        'comment', comment, 'approved_quote_id', approved_quote_id, 'approved_hash', approved_hash
    ) AS payload"""

    def create_approval(self, approval: Obj) -> Obj:
        k = approval_keys(approval)
        try:
            with self.pool.connection() as conn:
                conn.execute(
                    """INSERT INTO approvals (approval_id, request_id, quote_id, buyer_id, status,
                                              created_at, expires_at, payload)
                       VALUES (%s, %s, %s, %s, 'PENDING', %s::timestamptz, %s::timestamptz, %s)""",
                    (k["approval_id"], k["request_id"], k["quote_id"], k["buyer_id"],
                     k["created_at"], k["expires_at"], Jsonb(approval)),
                )
        except errors.ForeignKeyViolation as e:
            raise NotFound(f"quote {k['quote_id']} must be saved before its approval") from e
        return self.get_approval(k["approval_id"])

    def get_approval(self, approval_id: str) -> Optional[Obj]:
        return self._approval_payload(
            self._raw(f"SELECT {self._APPROVAL_COLS} FROM approvals WHERE approval_id = %s", (approval_id,))
        )

    def _raw(self, query: str, params: tuple):
        with self.pool.connection() as conn:
            return conn.execute(query, params).fetchone()

    @staticmethod
    def _approval_payload(row) -> Optional[Obj]:
        if not row:
            return None
        p = row["payload"]
        # Keys only present after a decision; keep the contract shape tidy before that.
        for k in ("approved_quote_id", "approved_hash"):
            if p.get(k) is None:
                p.pop(k, None)
        return p

    def list_approvals(self, status: Optional[str] = "PENDING", limit: int = 50) -> list[Obj]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {self._APPROVAL_COLS} FROM approvals
                    WHERE %s::text IS NULL OR status = %s
                    ORDER BY created_at DESC LIMIT %s""",
                (status, status, limit),
            ).fetchall()
        return [self._approval_payload(r) for r in rows]

    def decide_approval(self, approval_id, status, decided_by, approved_quote_id=None,
                        approved_hash=None, comment=None) -> bool:
        if status not in ("APPROVED", "REJECTED", "EXPIRED"):
            raise ValueError(f"bad decision status {status}")
        if status == "APPROVED" and not (approved_quote_id and approved_hash and decided_by):
            raise ValueError("APPROVED needs decided_by, approved_quote_id and approved_hash")
        with self.pool.connection() as conn:
            # One statement: only a PENDING approval can change. Zero rows = already decided.
            cur = conn.execute(
                """UPDATE approvals
                   SET status = %s, decided_by = %s, decided_at = now(),
                       approved_quote_id = %s, approved_hash = %s, comment = %s
                   WHERE approval_id = %s AND status = 'PENDING'""",
                (status, decided_by, approved_quote_id, approved_hash, comment, approval_id),
            )
            return cur.rowcount == 1

    # Orders
    def create_order(self, order: Obj) -> Obj:
        try:
            with self.pool.connection() as conn:
                row = conn.execute(
                    """INSERT INTO orders (order_id, quote_id, request_id, idempotency_key,
                                           status, placed_at, payload)
                       VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s)
                       ON CONFLICT (idempotency_key) DO NOTHING
                       RETURNING payload""",
                    (order["order_id"], order["quote_id"], order.get("request_id"),
                     order["idempotency_key"], order["status"], order["placed_at"], Jsonb(order)),
                ).fetchone()
                if row:
                    return row["payload"]
                existing = conn.execute(
                    "SELECT payload FROM orders WHERE idempotency_key = %s", (order["idempotency_key"],)
                ).fetchone()
                return existing["payload"]
        except errors.UniqueViolation as e:
            raise OrderConflict(f"request {order.get('request_id')} already has an order") from e
        except errors.ForeignKeyViolation as e:
            raise NotFound(f"quote {order['quote_id']} must be saved before its order") from e

    def get_order(self, order_id: str) -> Optional[Obj]:
        return self._one("SELECT payload FROM orders WHERE order_id = %s", (order_id,))

    # Decision log
    def append_step(self, request_id: str, step: Obj) -> Obj:
        try:
            with self.pool.connection() as conn, conn.transaction():
                # Serialise appends per request (works for the first step too, unlike a row lock).
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (request_id,))
                last = conn.execute(
                    "SELECT seq, hash FROM audit_steps WHERE request_id = %s ORDER BY seq DESC LIMIT 1",
                    (request_id,),
                ).fetchone()
                check_link(last, step, request_id)
                conn.execute(
                    """INSERT INTO audit_steps (request_id, seq, at, actor, type, prev_hash, hash, payload)
                       VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, %s)""",
                    (request_id, step["seq"], step["at"], step["actor"], step["type"],
                     step.get("prev_hash"), step["hash"], Jsonb(step)),
                )
        except (errors.UniqueViolation, errors.CheckViolation) as e:
            raise ChainConflict(str(e).splitlines()[0]) from e
        return step

    def get_steps(self, request_id: str) -> list[Obj]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM audit_steps WHERE request_id = %s ORDER BY seq", (request_id,)
            ).fetchall()
        return [r["payload"] for r in rows]
