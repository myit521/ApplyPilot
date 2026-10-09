-- T9 切片：人工投递记录。
-- applications 保留全部旧行与既有唯一约束；旧行只会被标记为未经确认的来源，
-- 不会被改写成任何形式的网站确认。
ALTER TABLE applications ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy_unverified'
    CHECK (source IN ('legacy_unverified','user_reported'));
ALTER TABLE applications ADD COLUMN occurred_at TIMESTAMPTZ;
ALTER TABLE applications ADD COLUMN updated_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE applications ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);

-- 扩展状态枚举：新增 unknown，保留旧状态，仍然不引入 site_confirmed 一类来源。
ALTER TABLE applications DROP CONSTRAINT IF EXISTS applications_status_check;
ALTER TABLE applications ADD CONSTRAINT applications_status_check
    CHECK (status IN ('created','filling','waiting_submit','submitted',
                      'cancelled','failed','unknown'));
