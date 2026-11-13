# gateway/store: Limos storage layer

Stores requests, quotes, approvals, orders and the decision log. Plain deterministic code, no AI.

```python
from gateway.store import get_store, ChainConflict, OrderConflict
store = get_store()          # STORE=file (default) | memory | postgres
```

| `STORE` | What it is |
|---|---|
| `file` | MemoryStore that writes `.limos-store.json` after every change (path from `STORE_PATH`). Default, matches `gateway.settings`. |
| `memory` | In-process dicts. Fast, gone on restart. |
| `postgres` | Aiven PostgreSQL 18. Reads `DATABASE_URL`, runs the migration on start. |

Methods: `save_request`, `update_request_status`, `get_request`, `list_requests`, `save_quote`, `update_quote_status`, `get_quote`, `create_approval`, `get_approval`, `list_approvals`, `decide_approval -> bool`, `create_order`, `get_order`, `append_step`, `get_steps`, `verify_chain(request_id, recompute=None)`.

Objects are dicts shaped like `docs/api_contract.md` v0.2 (convert with `gateway.schemas` models via `.model_dump(mode="json")`). The full object is stored as `jsonb`, and the key columns the rules need are pulled out next to it. Hashes are computed by the audit module; the store only stores them and checks the links. Pass the audit module's hash function as `recompute` to `verify_chain` to also re-derive every hash.

## Setup

Dependencies for Postgres (not in `pyproject.toml` yet): `psycopg[binary]` and `psycopg-pool`.

```bash
uv add "psycopg[binary]" psycopg-pool
```

`DATABASE_URL` goes only in `.env`, never in code, logs or commits. Aiven needs `sslmode=require`, which is already in the URL from the Aiven console.

The Aiven Developer plan allows **15 connections for the whole team**. The pool uses `min_size=1` and `max_size=DB_POOL_MAX` (default 3). Tests use one connection.

Migration (idempotent, also run by `get_store()`):

```bash
psql "$DATABASE_URL" -f gateway/store/migrations/001_init.sql
```

Tests (memory and file always, Postgres when `DATABASE_URL` is set in env or `.env`; each run uses its own random schema and drops it):

```bash
uv run pytest tests/test_store.py
```

## Trust guarantees in the database (Aiven)

These are enforced by PostgreSQL itself, so they hold even if application code has a bug or someone writes SQL by hand.

1. **The decision log cannot be rewritten.** `audit_steps` is append-only: a trigger rejects every `UPDATE`, `DELETE` and `TRUNCATE`. What the agent, the rules and the human did stays as it was written.
2. **Every step is chained to the one before.** The first step of a request has no previous hash, and every later step must carry the exact hash of the step before it, with no gaps in numbering. A database trigger checks this on every insert, and the store locks the request while appending so two writers cannot fork the chain. Editing any step after the fact breaks the chain, and `verify_chain` shows it.
3. **The same order can never be placed twice.** Every order has an idempotency key with a unique constraint. A retry or a double click returns the order that already exists instead of creating a new one. On top of that, a request can have only one live order.
4. **An approval is decided exactly once.** Deciding is a single `UPDATE ... WHERE status = 'PENDING'`. If two people click at the same time, or the quote already expired, only one decision lands and the other gets "already decided". An approval always records who decided, when, and the exact quote and content hash that was approved.
