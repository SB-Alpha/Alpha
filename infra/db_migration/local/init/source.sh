#!/bin/bash
# 원본(RDS 역할): 앱 계정 alpha가 테이블 소유자이자 RDS 마스터 역할.
# 복제 계정·marker·publication은 에이전트 source_prepare 단계가 alpha로 만든다.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<SQL
CREATE ROLE alpha LOGIN CREATEROLE REPLICATION PASSWORD '${APP_PASSWORD}';
GRANT CREATE ON SCHEMA public TO alpha;
GRANT CREATE ON DATABASE postgres TO alpha;
SET ROLE alpha;

CREATE TABLE work_items (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT        NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'todo'
                            CHECK (status IN ('todo', 'in_progress', 'done')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX work_items_created_at_idx ON work_items (created_at DESC);
INSERT INTO work_items (title, status)
SELECT 'seed item ' || g, (ARRAY['todo','in_progress','done'])[1 + g % 3]
FROM generate_series(1, 1000) g;
RESET ROLE;
SQL
