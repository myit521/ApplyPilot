CREATE TABLE workflow_approvals (
    run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id),
    draft_revision INTEGER NOT NULL CHECK (draft_revision > 0),
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    version_id BIGINT NOT NULL UNIQUE REFERENCES resume_versions(id),
    graph_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE resume_version_facts (
    version_id BIGINT NOT NULL REFERENCES resume_versions(id),
    fact_id TEXT NOT NULL,
    fact_revision INTEGER NOT NULL CHECK (fact_revision > 0),
    snapshot JSONB NOT NULL,
    PRIMARY KEY (version_id, fact_id),
    FOREIGN KEY (fact_id, fact_revision)
        REFERENCES fact_revisions (fact_id, revision)
);
