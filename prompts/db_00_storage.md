# DB PROMPT 0: Limos storage layer (Aiven PostgreSQL)

You are building the **storage layer** of Limos, our Junction 2026 project. Mikael builds the rest of the backend (agent, Valio client, guardrails, routes) in parallel. You only touch the storage layer.

Before writing code, read:
1. `docs/PLAN.md`, especially section 7 (decision log, `Store` interface) and the Rules for every phase.
2. `docs/api_contract.md` v0.2: the shapes of `Quote`, `Order`, `Step`, approvals and requests.
3. `fixtures/api/*.json`: realistic examples of every shape. Use them as test data.
4. Whatever backend code already exists. If Mikael has written `schemas.py` (Pydantic models for the contract), use those models. If not, type against `dict[str, Any]` for now and keep the conversion in one place so it is easy to switch.

## Your zone
- Code: `gateway/store/` (where PLAN.md puts it; Mikael asked for "/backend/storage", and the backend lives in `gateway/`).
- Tests: `tests/test_store.py` (or the repo's `tests/storage/` if a tests folder exists).
- Migration: `gateway/store/migrations/001_init.sql`.
- **Do not touch anything else in the backend.** No routes, no agent, no policy code.

## Hard rules
1. The connection string comes only from env `DATABASE_URL`. Never hardcode it, print it, log it or commit it. Aiven requires TLS: keep `sslmode=require` from the URL.
2. `STORE=memory|postgres` picks the implementation (the plan also mentions `file`; implement it only if Mikael has not).
3. Storage is plain deterministic code. No LLM calls.
4. **Never commit `.env`**. Add `DATABASE_URL=` (empty) to `.env.example` if it is not there.
5. Python 3.12, `psycopg` 3 (with `psycopg_pool`), pytest. No ORM. Plain SQL, parameterised queries only.
6. The Aiven service (PostgreSQL 18, Developer plan) allows **only 15 connections** in total, shared by the whole team. Keep the pool small: `min_size=1`, `max_size` from env `DB_POOL_MAX` (default 3). Tests use one connection and close it.

## What to store

| Table | Key columns | Payload |
|---|---|---|
| `requests` | `request_id` PK, `buyer_id`, `status`, `created_at` | full request object as `jsonb` |
| `quotes` | `quote_id` PK, `request_id`, `buyer_id`, `status`, `content_hash`, `expires_at`, `created_at` | full `Quote` as `jsonb` |
| `approvals` | `approval_id` PK, `request_id`, `quote_id`, `status` (PENDING/APPROVED/REJECTED/EXPIRED), `decided_by`, `decided_at`, `approved_quote_id`, `approved_hash`, `comment`, `created_at` | option quote ids as `jsonb` |
| `orders` | `order_id` PK, `quote_id`, `request_id`, `idempotency_key` **UNIQUE**, `status`, `placed_at` | full `Order` as `jsonb` |
| `audit_steps` | (`request_id`, `seq`) PK, `at`, `actor`, `type`, `prev_hash`, `hash` **UNIQUE** | full `Step` as `jsonb` |

Indexes for the queries below (e.g. `approvals(status, created_at)`, `requests(buyer_id, created_at)`).

### Trust guarantees in the database itself (this is our Aiven story)
- `audit_steps` is **append-only**: a trigger rejects every `UPDATE` and `DELETE` on it.
- The first step of a request has `prev_hash IS NULL`; every later step's `prev_hash` must equal the previous step's `hash` (enforce in the append method inside one transaction with a row lock; a CHECK or trigger is a bonus).
- `orders.idempotency_key` is unique: the same key can never create a second order.
- An approval can be decided only once: deciding it is a single `UPDATE ... WHERE status = 'PENDING'`; zero rows updated means it was already decided or expired.
- One order per request: unique index on `orders(request_id)` (plan rule "one order per event").

## Interface

One abstract class (or `Protocol`) `Store` with two implementations, `MemoryStore` and `PostgresStore`, behaving identically. Proposed methods (align names with Mikael if his plan or code already defines them):

- `save_request(req)`, `update_request_status(request_id, status)`, `get_request(request_id)`, `list_requests(buyer_id, limit=20)`
- `save_quote(quote)`, `update_quote_status(quote_id, status)`, `get_quote(quote_id)`
- `create_approval(approval)`, `get_approval(approval_id)`, `list_approvals(status="PENDING")`
- `decide_approval(approval_id, status, decided_by, approved_quote_id=None, approved_hash=None, comment=None) -> bool` (False if not PENDING anymore)
- `create_order(order) -> Order`: if the `idempotency_key` exists, return the existing order instead of creating one
- `get_order(order_id)`
- `append_step(request_id, step) -> Step`: rejects a step whose `prev_hash` does not match the current last hash (raise a clear `ChainConflict`). **Hash calculation lives in Mikael's audit module**; storage only stores and checks the link.
- `get_steps(request_id)` in `seq` order, and `verify_chain(request_id) -> bool` (re-links prev/hash; Mikael's audit module can recompute the hashes on top)

Timestamps are timezone-aware (`timestamptz`), returned as ISO 8601 with offset as in the contract.

## Tests
- One shared test suite run against both `MemoryStore` and `PostgresStore` (pytest parametrised fixture). Postgres tests are skipped when `DATABASE_URL` is not set.
- Cover: round trip of every object using the fixtures, idempotent order, one order per request, approval decided only once, append-only (UPDATE/DELETE on `audit_steps` fails), broken chain rejected, `verify_chain` true for the fixture chain.
- Tests create their own schema (e.g. a random schema name per run) and drop it afterwards, so they never touch demo data.

## Also deliver
- `gateway/store/migrations/001_init.sql` (idempotent: `CREATE TABLE IF NOT EXISTS`, triggers with `CREATE OR REPLACE`).
- `gateway/store/README.md`: how to run the migration against Aiven (`psql "$DATABASE_URL" -f ...`), how to run tests, and the four trust guarantees above in plain language (this text goes into our Aiven submission).

## Your task for this prompt
1. Confirm in max 8 bullets what you will build and where, and list any mismatch with Mikael's existing code.
2. Build the migration, `Store`, `MemoryStore`, `PostgresStore`, tests and README.
3. Run the tests with `STORE=memory`. If `DATABASE_URL` is set in the environment, also run them against Postgres.

**Report back:** file tree, test output, and anything Mikael needs to know (method names, how to construct the store, env vars).
