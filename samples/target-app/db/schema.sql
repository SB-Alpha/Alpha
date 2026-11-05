-- Business table for the migration target service.
-- Applied idempotently at app startup; also usable directly with psql.
-- PK is UUID (no sequence state to carry over during CDC migration).

CREATE TABLE IF NOT EXISTS work_items (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT        NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'todo'
                            CHECK (status IN ('todo', 'in_progress', 'done')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS work_items_created_at_idx ON work_items (created_at DESC);
