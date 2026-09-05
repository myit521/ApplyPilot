-- ApplyPilot 数据库 schema，对应 docs/design.md 第 6 节。
-- 通过 docker-compose 挂载为初始化脚本，也可由测试容器直接执行。

CREATE EXTENSION IF NOT EXISTS vector;

-- 6.1 个人事实库
CREATE TABLE IF NOT EXISTS facts (
    id            TEXT PRIMARY KEY,
    fact_type     TEXT NOT NULL CHECK (fact_type IN ('internship','project','skill','award','education')),
    source_name   TEXT NOT NULL,
    content       TEXT NOT NULL,
    start_date    DATE,
    end_date      DATE,
    skills        TEXT[] NOT NULL DEFAULT '{}',
    metrics       TEXT[] NOT NULL DEFAULT '{}',
    evidence_type TEXT NOT NULL DEFAULT 'self_report'
                  CHECK (evidence_type IN ('commit','document','test','self_report')),
    evidence_ref  TEXT NOT NULL DEFAULT '',
    embedding     vector(512),
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6.2 职位
CREATE TABLE IF NOT EXISTS jobs (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL DEFAULT 'paste',
    url           TEXT,
    company       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    raw_text      TEXT NOT NULL,
    parsed        JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 简历版本：批准后冻结，不再修改
CREATE TABLE IF NOT EXISTS resume_versions (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT NOT NULL REFERENCES jobs(id),
    content       JSONB NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','approved','rejected')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 简历单条表述与事实引用
CREATE TABLE IF NOT EXISTS resume_claims (
    id            BIGSERIAL PRIMARY KEY,
    version_id    BIGINT NOT NULL REFERENCES resume_versions(id),
    text          TEXT NOT NULL,
    fact_ids      TEXT[] NOT NULL,
    matched_requirements TEXT[] NOT NULL DEFAULT '{}'
);

-- 工作流运行：节点级持久化，支持断点恢复
CREATE TABLE IF NOT EXISTS workflow_runs (
    id            TEXT PRIMARY KEY,
    current_node  TEXT NOT NULL,
    status        TEXT NOT NULL,
    retry_count   INT NOT NULL DEFAULT 0,
    input_summary TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    state         JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 投递记录：幂等键唯一约束防止重复投递
CREATE TABLE IF NOT EXISTS applications (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT NOT NULL REFERENCES jobs(id),
    version_id    BIGINT NOT NULL REFERENCES resume_versions(id),
    channel       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'created'
                  CHECK (status IN ('created','filling','waiting_submit','submitted','cancelled','failed')),
    result        TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, version_id, channel),
    UNIQUE (idempotency_key)
);

-- 审计事件
CREATE TABLE IF NOT EXISTS audit_events (
    id            BIGSERIAL PRIMARY KEY,
    event_type    TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 全文检索与向量索引
CREATE INDEX IF NOT EXISTS facts_content_fts ON facts
    USING gin (to_tsvector('simple', content || ' ' || source_name));
CREATE INDEX IF NOT EXISTS facts_embedding_idx ON facts
    USING hnsw (embedding vector_cosine_ops);
