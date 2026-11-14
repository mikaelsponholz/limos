-- Limos storage schema for gateway.store.PostgresStore, migration 001.
-- Idempotent: safe to run more than once. Runs in the current search_path schema.
-- Needs PostgreSQL 14+ (CREATE OR REPLACE TRIGGER). Aiven runs PostgreSQL 18.
--
-- Each record is stored whole as jsonb (the contract model), next to the columns
-- the store filters on. `pos` keeps insertion order, like the memory store.

-- Which buyer owns a request id. Claimed once, never moved to another buyer.
CREATE TABLE IF NOT EXISTS limos_request_owners (
    request_id  text PRIMARY KEY,
    buyer_id    text NOT NULL
);

CREATE TABLE IF NOT EXISTS limos_requests (
    request_id  text      PRIMARY KEY,
    buyer_id    text      NOT NULL,
    pos         bigserial NOT NULL,
    payload     jsonb     NOT NULL
);
CREATE INDEX IF NOT EXISTS limos_requests_buyer ON limos_requests (buyer_id, pos);

-- Quotes change status (PENDING_APPROVAL, ORDERED...), so they are upserted.
CREATE TABLE IF NOT EXISTS limos_quotes (
    quote_id    text      PRIMARY KEY,
    buyer_id    text      NOT NULL,
    request_id  text      NOT NULL,
    pos         bigserial NOT NULL,
    payload     jsonb     NOT NULL
);
CREATE INDEX IF NOT EXISTS limos_quotes_request ON limos_quotes (buyer_id, request_id, pos);

CREATE TABLE IF NOT EXISTS limos_approvals (
    approval_id  text      PRIMARY KEY,
    buyer_id     text      NOT NULL,
    status       text      NOT NULL
                 CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
    pos          bigserial NOT NULL,
    payload      jsonb     NOT NULL
);
CREATE INDEX IF NOT EXISTS limos_approvals_buyer ON limos_approvals (buyer_id, pos);

-- Guarantee: a decided approval is final. Its decision can never be changed afterwards.
CREATE OR REPLACE FUNCTION limos_approval_decided_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'PENDING' AND NEW.payload IS DISTINCT FROM OLD.payload THEN
        RAISE EXCEPTION 'approval % is already %', OLD.approval_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER limos_approvals_decided_once
    BEFORE UPDATE ON limos_approvals
    FOR EACH ROW EXECUTE FUNCTION limos_approval_decided_once();

-- Orders: immutable, one per request, and an idempotency key is used once per buyer.
CREATE TABLE IF NOT EXISTS limos_orders (
    order_id         text      PRIMARY KEY,
    buyer_id         text      NOT NULL,
    quote_id         text      NOT NULL REFERENCES limos_quotes (quote_id),
    request_id       text      NOT NULL UNIQUE,
    idempotency_key  text      NOT NULL,
    pos              bigserial NOT NULL,
    payload          jsonb     NOT NULL,
    UNIQUE (buyer_id, idempotency_key)
);

-- Decision log: append-only, hash-chained steps per request.
CREATE TABLE IF NOT EXISTS limos_steps (
    request_id  text    NOT NULL,
    seq         integer NOT NULL CHECK (seq >= 1),
    prev_hash   text,
    hash        text    NOT NULL,
    payload     jsonb   NOT NULL,
    PRIMARY KEY (request_id, seq),
    -- Only the first step of a chain may lack a previous hash.
    CHECK ((seq = 1) = (prev_hash IS NULL))
);

-- Past orders that Limos appends to a customer's history.
CREATE TABLE IF NOT EXISTS limos_past_orders (
    buyer_id     text      NOT NULL,
    customer_id  text      NOT NULL,
    pos          bigserial PRIMARY KEY,
    payload      jsonb     NOT NULL
);
CREATE INDEX IF NOT EXISTS limos_past_orders_customer ON limos_past_orders (buyer_id, customer_id, pos);

-- Guarantee: orders, the decision log and order history can never be edited or deleted.
CREATE OR REPLACE FUNCTION limos_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE TRIGGER limos_orders_no_update_delete
    BEFORE UPDATE OR DELETE ON limos_orders
    FOR EACH ROW EXECUTE FUNCTION limos_append_only();
CREATE OR REPLACE TRIGGER limos_orders_no_truncate
    BEFORE TRUNCATE ON limos_orders
    FOR EACH STATEMENT EXECUTE FUNCTION limos_append_only();
CREATE OR REPLACE TRIGGER limos_steps_no_update_delete
    BEFORE UPDATE OR DELETE ON limos_steps
    FOR EACH ROW EXECUTE FUNCTION limos_append_only();
CREATE OR REPLACE TRIGGER limos_steps_no_truncate
    BEFORE TRUNCATE ON limos_steps
    FOR EACH STATEMENT EXECUTE FUNCTION limos_append_only();
CREATE OR REPLACE TRIGGER limos_past_orders_no_update_delete
    BEFORE UPDATE OR DELETE ON limos_past_orders
    FOR EACH ROW EXECUTE FUNCTION limos_append_only();
CREATE OR REPLACE TRIGGER limos_past_orders_no_truncate
    BEFORE TRUNCATE ON limos_past_orders
    FOR EACH STATEMENT EXECUTE FUNCTION limos_append_only();

-- Guarantee: a new step must link to the previous one (seq + 1 and prev_hash = previous hash),
-- checked by the database itself, so no client can write a broken chain.
CREATE OR REPLACE FUNCTION limos_steps_check_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    last_seq  integer;
    last_hash text;
BEGIN
    SELECT s.seq, s.hash INTO last_seq, last_hash
      FROM limos_steps s
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
CREATE OR REPLACE TRIGGER limos_steps_check_chain
    BEFORE INSERT ON limos_steps
    FOR EACH ROW EXECUTE FUNCTION limos_steps_check_chain();
