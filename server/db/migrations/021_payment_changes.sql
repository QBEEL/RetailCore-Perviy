-- Commit-ordered, transactional change feed. The singleton is locked BEFORE
-- acquiring payment row locks; sequence allocation alone is not commit order.
CREATE TABLE payment_sync_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    epoch UUID NOT NULL DEFAULT gen_random_uuid(),
    revision BIGINT NOT NULL DEFAULT 0
);
INSERT INTO payment_sync_state(singleton) VALUES (TRUE);

CREATE TABLE payment_change (
    revision BIGINT PRIMARY KEY,
    payment_id BIGINT,
    metadata BOOLEAN NOT NULL DEFAULT FALSE,
    reset BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE payment ADD COLUMN sync_revision BIGINT NOT NULL DEFAULT 0;

CREATE FUNCTION payment_sync_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM payment_sync_state WHERE singleton FOR UPDATE;
    RETURN NULL;
END $$;

CREATE FUNCTION payment_sync_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE payment_sync_state SET revision = revision + 1 WHERE singleton
        RETURNING revision INTO NEW.sync_revision;
    RETURN NEW;
END $$;

CREATE FUNCTION payment_sync_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rev BIGINT; ident BIGINT; meta BOOLEAN := FALSE;
BEGIN
    IF TG_TABLE_NAME = 'payment' AND TG_OP IN ('INSERT', 'UPDATE') THEN
        rev := NEW.sync_revision;
        ident := NEW.id;
        IF TG_OP = 'INSERT' THEN
            meta := TRUE;
        ELSE
            meta := (NEW.recipient, NEW.recipient_key, NEW.responsible, NEW.operation,
                     NEW.supplier_id, NEW.dds_item) IS DISTINCT FROM
                    (OLD.recipient, OLD.recipient_key, OLD.responsible, OLD.operation,
                     OLD.supplier_id, OLD.dds_item);
        END IF;
    ELSE
        UPDATE payment_sync_state SET revision = revision + 1 WHERE singleton
            RETURNING revision INTO rev;
        IF TG_TABLE_NAME = 'payment' THEN
            ident := OLD.id;
            meta := TRUE;
        ELSIF TG_TABLE_NAME = 'payment_file' THEN
            ident := CASE WHEN TG_OP = 'DELETE' THEN OLD.payment_id ELSE NEW.payment_id END;
        ELSE
            meta := TRUE;
        END IF;
    END IF;
    INSERT INTO payment_change(revision, payment_id, metadata, reset)
        VALUES (rev, ident, meta, TG_TABLE_NAME IN (
            'direction', 'supplier_direction', 'supplier_assignment',
            'user_direction', 'user_responsible'));
    -- Moving an attachment between payments is unusual but still invalidates both.
    IF TG_TABLE_NAME = 'payment_file' THEN
        IF TG_OP = 'UPDATE' AND OLD.payment_id <> NEW.payment_id THEN
            UPDATE payment_sync_state SET revision = revision + 1 WHERE singleton
                RETURNING revision INTO rev;
            INSERT INTO payment_change(revision, payment_id) VALUES (rev, OLD.payment_id);
        END IF;
    END IF;
    RETURN NULL;
END $$;

CREATE FUNCTION payment_sync_prune() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rev BIGINT;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        UPDATE payment_sync_state SET revision = revision + 1 WHERE singleton
            RETURNING revision INTO rev;
        INSERT INTO payment_change(revision, metadata, reset) VALUES (rev, TRUE, TRUE);
    END IF;
    DELETE FROM payment_change
        WHERE revision <= (SELECT revision - 50000 FROM payment_sync_state WHERE singleton);
    RETURN NULL;
END $$;

CREATE TRIGGER payment_sync_version BEFORE INSERT OR UPDATE ON payment
    FOR EACH ROW EXECUTE FUNCTION payment_sync_version();
-- app_user is an FK parent of tracked rows. Lock it in the same order before
-- deletes/cascades as well; normal profile writes do not create feed events.
CREATE TRIGGER payment_sync_lock BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON app_user
    FOR EACH STATEMENT EXECUTE FUNCTION payment_sync_lock();

-- Metadata affects budgets, dropdowns and direction filters, but does not require
-- reloading every payment. A change in any of these tables is journaled as well.
DO $$
DECLARE tab TEXT;
BEGIN
    FOREACH tab IN ARRAY ARRAY['payment', 'payment_file', 'budget', 'dds_item', 'recipient_link', 'direction', 'supplier_direction', 'supplier_assignment', 'user_direction', 'user_responsible'] LOOP
        EXECUTE format('CREATE TRIGGER payment_sync_lock BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION payment_sync_lock()', tab);
        EXECUTE format('CREATE TRIGGER payment_sync_record AFTER INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION payment_sync_record()', tab);
        EXECUTE format('CREATE TRIGGER payment_sync_prune AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION payment_sync_prune()', tab);
    END LOOP;
END $$;
