-- T8 切片 1：持久化工作流任务。
-- workflow_runs 继续保存图状态；这里只放任务生命周期与幂等键。
CREATE TABLE workflow_tasks (
    run_id           TEXT PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    job_id           BIGINT NOT NULL REFERENCES jobs(id),
    idempotency_key  TEXT UNIQUE,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','retry_wait',
                                       'waiting_approval','completed','failed','cancelled')),
    attempt_count    INT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    error            TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 到期任务扫描索引：只包含等待被领取的状态。
CREATE INDEX workflow_tasks_due_idx ON workflow_tasks (status, next_attempt_at)
    WHERE status IN ('queued', 'retry_wait');
