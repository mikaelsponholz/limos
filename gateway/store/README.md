# gateway/store: Limos storage layer

Stores requests, quotes, approvals, orders, customer history and the decision log. Plain deterministic code, no AI. Every store implements the `Store` protocol in `base.py`, and every read is scoped to a buyer.

```python
from gateway.store import create_store

store = create_store(settings)  # STORE=file (default) | memory | postgres
with store.transaction():  # all writes inside commit or roll back together
    ...
```

| `STORE` | What it is |
|---|---|
| `file` | MemoryStore that writes `.runtime/store.json` atomically on commit. Default. |
| `memory` | In-process state. Fast, gone on restart. Used by most tests. |
| `postgres` | Aiven PostgreSQL 18 (`postgres.py`). Reads `DATABASE_URL`; migrations require explicit opt-in. |

## Postgres setup

The store imports psycopg only when `STORE=postgres`.

`DATABASE_URL` goes only in `.env`, never in code, logs or commits. Aiven needs `sslmode=require`, which is already in the URL from the Aiven console. Start the gateway with the env file, for example `uvicorn gateway.main:app --env-file .env`.

The Aiven Developer plan allows **15 connections for the whole team**. The pool uses `min_size=1` and `max_size=DB_POOL_MAX` (default 3).

The gateway does not migrate on start unless `MIGRATE_ON_START=true`. Migrations are
idempotent and run in file name order under one advisory lock and transaction.
Locally, set `DATABASE_URL` in the environment and run:

```bash
uv run python -m scripts.migrate_db
```

Tests run memory and file always, and Postgres too when `DATABASE_URL` is set in the environment or `.env`. Each test gets its own random schema, dropped afterwards.

```bash
uv run pytest tests/test_engine_store_audit.py tests/test_store_postgres.py
```

## How it stays consistent

Each record is stored whole as `jsonb` (the contract model), with the columns the store filters on next to it. Every `transaction()` is one database transaction that first takes one advisory lock, so engine operations are serialized across gateway processes exactly like the memory store serializes them across threads. Nested transactions join the outer one.

## Trust guarantees in the database (Aiven)

These are enforced by PostgreSQL itself, so they hold even if application code has a bug or someone writes SQL by hand. `tests/test_store_postgres.py` checks each one with a raw connection that bypasses the store.

1. **The decision log cannot be rewritten.** `limos_steps` is append-only: a trigger rejects every `UPDATE`, `DELETE` and `TRUNCATE`. What the agent, the rules and the human did stays as it was written.
2. **Every step is chained to the one before.** The first step of a request has no previous hash, and every later step must carry the exact hash of the step before it, with no gaps in numbering. A database trigger checks this on every insert. Editing a step after the fact is impossible, and `verify_chain` in `gateway/audit.py` recomputes every hash on read.
3. **The same order can never be placed twice.** Orders are immutable. An idempotency key is unique per buyer, so a retry or a double click returns the existing order, and a request can have only one order.
4. **A decided approval stays decided.** Once an approval is APPROVED, REJECTED or EXPIRED, triggers reject changes to its payload or status and reject its deletion. Truncating approvals is always rejected. Pending approvals may be deleted.
5. **Order history is append-only** (`limos_past_orders`), like the log.

## Known limitations

Quote deletes stay allowed, subject to existing foreign keys.

## Production steps

The triggers protect against clients, not the table owner, who can disable them.
In production, the gateway connects as a separate application role without owner
rights, with only SELECT, INSERT, UPDATE and DELETE as the guarantees allow.
Migrations run as the owner role using `uv run python -m scripts.migrate_db`.
On a host such as Aiven Runtime, `MIGRATE_ON_START=true` explicitly enables startup
migrations; this requires an owner role for those migrations.
