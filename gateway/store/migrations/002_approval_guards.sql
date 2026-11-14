-- Limos storage schema for gateway.store.PostgresStore, migration 002.
-- Idempotent: safe to run more than once. Runs in the current search_path schema.

-- Guarantee: neither the payload nor the status of a decided approval can change.
CREATE OR REPLACE FUNCTION limos_approval_decided_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'PENDING' AND (
        NEW.payload IS DISTINCT FROM OLD.payload OR NEW.status IS DISTINCT FROM OLD.status
    ) THEN
        RAISE EXCEPTION 'approval % is already %', OLD.approval_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

-- Pending approvals may be deleted; decided approvals must remain.
CREATE OR REPLACE FUNCTION limos_approval_no_decided_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'PENDING' THEN
        RAISE EXCEPTION 'approval % is already %', OLD.approval_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
END;
$$;
CREATE OR REPLACE TRIGGER limos_approvals_no_decided_delete
    BEFORE DELETE ON limos_approvals
    FOR EACH ROW EXECUTE FUNCTION limos_approval_no_decided_delete();

-- Truncation bypasses row triggers and could remove decided approvals.
CREATE OR REPLACE TRIGGER limos_approvals_no_truncate
    BEFORE TRUNCATE ON limos_approvals
    FOR EACH STATEMENT EXECUTE FUNCTION limos_append_only();
