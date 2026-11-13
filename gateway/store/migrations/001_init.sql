-- Limos storage schema, migration 001.
-- Idempotent: safe to run more than once. Runs in the current search_path schema.
-- Needs PostgreSQL 14+ (CREATE OR REPLACE TRIGGER). Aiven runs PostgreSQL 18.

-- Requests: one row per purchase request (preset, event, item list or free text).
CREATE TABLE IF NOT EXISTS requests (
    request_id  text        PRIMARY KEY,
    buyer_id    text        NOT NULL,
    status      text        NOT NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    payload     jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_buyer_created ON requests (buyer_id, created_at DESC);

-- Quotes: a locked price for a set of lines. The content hash is what a human approves.
CREATE TABLE IF NOT EXISTS quotes (
    quote_id      text        PRIMARY KEY,
    request_id    text,
    buyer_id      text        NOT NULL,
    status        text        NOT NULL,
    content_hash  text        NOT NULL,
    expires_at    timestamptz NOT NULL,
    created_at    timestamptz NOT NULL,
    payload       jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS quotes_request ON quotes (request_id);

-- Approvals: a human decision about one or more quote options. Decided at most once.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id        text        PRIMARY KEY,
    request_id         text,
    quote_id           text        NOT NULL REFERENCES quotes (quote_id),
    buyer_id           text        NOT NULL,
    status             text        NOT NULL DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
    created_at         timestamptz NOT NULL,
    expires_at         timestamptz,
    decided_by         text,
    decided_at         timestamptz,
    approved_quote_id  text        REFERENCES quotes (quote_id),
    approved_hash      text,
    comment            text,
    payload            jsonb       NOT NULL,
    -- A decided approval always says who decided and when.
    CHECK (status = 'PENDING' OR (decided_at IS NOT NULL)),
    -- An approved approval always names the exact quote and hash that was approved.
    CHECK (status <> 'APPROVED' OR (approved_quote_id IS NOT NULL AND approved_hash IS NOT NULL AND decided_by IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS approvals_status_created ON approvals (status, created_at DESC);

-- Orders: simulated orders placed from a quote.
CREATE TABLE IF NOT EXISTS orders (
    order_id         text        PRIMARY KEY,
    quote_id         text        NOT NULL REFERENCES quotes (quote_id),
    request_id       text,
    idempotency_key  text        NOT NULL UNIQUE,   -- same key can never create a second order
    status           text        NOT NULL,
    placed_at        timestamptz NOT NULL,
    payload          jsonb       NOT NULL
);
-- One live order per request ("one order per event"). Failed attempts do not count.
CREATE UNIQUE INDEX IF NOT EXISTS orders_one_per_request
    ON orders (request_id) WHERE request_id IS NOT NULL AND status <> 'FAILED';

-- Decision log: append-only, hash-chained steps per request.
CREATE TABLE IF NOT EXISTS audit_steps (
    request_id  text        NOT NULL,
    seq         integer     NOT NULL CHECK (seq >= 1),
    at          timestamptz NOT NULL,
    actor       text        NOT NULL,
    type        text        NOT NULL,
    prev_hash   text,
    hash        text        NOT NULL UNIQUE,
    payload     jsonb       NOT NULL,
    PRIMARY KEY (request_id, seq),
    -- Only the first step of a chain may lack a previous hash.
    CHECK ((seq = 1) = (prev_hash IS NULL))
);

-- Guarantee 1: the decision log can never be edited or deleted, only appended to.
CREATE OR REPLACE FUNCTION limos_audit_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_steps is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE TRIGGER audit_steps_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_steps
    FOR EACH ROW EXECUTE FUNCTION limos_audit_append_only();

CREATE OR REPLACE TRIGGER audit_steps_no_truncate
    BEFORE TRUNCATE ON audit_steps
    FOR EACH STATEMENT EXECUTE FUNCTION limos_audit_append_only();

-- Guarantee 2: a new step must link to the previous one (seq + 1 and prev_hash = previous hash).
CREATE OR REPLACE FUNCTION limos_audit_check_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    last_seq  integer;
    last_hash text;
BEGIN
    SELECT s.seq, s.hash INTO last_seq, last_hash
      FROM audit_steps s
     WHERE s.request_id = NEW.request_id
     ORDER BY s.seq DESC
     LIMIT 1;

    IF last_seq IS NULL THEN
        IF NEW.seq <> 1 OR NEW.prev_hash IS NOT NULL THEN
            RAISE EXCEPTION 'chain conflict: first step of % must have seq 1 and no prev_hash', NEW.request_id
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.seq <> last_seq + 1 OR NEW.prev_hash IS DISTINCT FROM last_hash THEN
        RAISE EXCEPTION 'chain conflict: step % of % does not link to step %', NEW.seq, NEW.request_id, last_seq
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER audit_steps_check_chain
    BEFORE INSERT ON audit_steps
    FOR EACH ROW EXECUTE FUNCTION limos_audit_check_chain();
