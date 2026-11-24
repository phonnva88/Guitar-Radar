CREATE TABLE IF NOT EXISTS post_engagement (
    post_id bigint PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
    feedback_id text NOT NULL,
    comment_count integer CHECK (comment_count >= 0),
    reaction_count integer CHECK (reaction_count >= 0),
    comment_count_at timestamptz,
    reaction_count_at timestamptz
);

CREATE TABLE IF NOT EXISTS post_comment_pages (
    post_id bigint NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    account_id text NOT NULL,
    page integer NOT NULL CHECK (page >= 1),
    thread_id uuid NOT NULL,
    items jsonb NOT NULL,
    next_cursor text,
    preview_only boolean NOT NULL DEFAULT false,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, account_id, page)
);

CREATE TABLE IF NOT EXISTS facebook_engagement_actions (
    id uuid PRIMARY KEY,
    post_id bigint NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    account_id text NOT NULL,
    request_hash text NOT NULL,
    state text NOT NULL CHECK (state IN ('sending', 'confirmed', 'rejected', 'unknown')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Legacy integer counts cannot distinguish unavailable from zero.
INSERT INTO post_engagement (post_id, feedback_id)
SELECT DISTINCT ON (p.id) p.id, raw_post->>'feedbackId'
FROM collector_run_snapshots snapshot
JOIN collection_runs run ON run.id = snapshot.run_id
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(snapshot.raw->'posts') = 'array'
         THEN snapshot.raw->'posts' ELSE '[]'::jsonb END
) raw_post
JOIN posts p ON p.group_id = run.group_id AND p.post_id = raw_post->>'id'
WHERE NOT EXISTS (SELECT 1 FROM radar_schema_version WHERE version = 5)
  AND p.platform = 'facebook' AND run.source = 'live' AND snapshot.raw->>'collector' = 'facebook-go'
  AND char_length(raw_post->>'feedbackId') BETWEEN 1 AND 256
  AND raw_post->>'feedbackId' ~ '^[A-Za-z0-9_:/+=-]+$'
ORDER BY p.id, snapshot.run_id DESC
ON CONFLICT (post_id) DO NOTHING;

INSERT INTO radar_schema_version(version) VALUES (5) ON CONFLICT DO NOTHING;
