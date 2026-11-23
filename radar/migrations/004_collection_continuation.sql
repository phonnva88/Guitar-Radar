ALTER TABLE collection_runs ADD COLUMN IF NOT EXISTS continued_from bigint
    REFERENCES collection_runs(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS collector_checkpoints (
    run_id bigint PRIMARY KEY REFERENCES collection_runs(id) ON DELETE CASCADE,
    state jsonb NOT NULL
);

INSERT INTO radar_schema_version(version) VALUES (4) ON CONFLICT DO NOTHING;
