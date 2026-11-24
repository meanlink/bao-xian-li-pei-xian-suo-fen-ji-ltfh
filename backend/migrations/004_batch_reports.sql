-- 合作方按批报案：批次回执、报案号/修订号台账、人工确认队列
-- 幂等执行，已有数据库卷可直接升级。

-- 案件侧补充合作方报案标识。一个报案号始终对应同一个案件，
-- 同一保单同一天的不同事故使用不同报案号分别立案，因此这里不建 (policy_no, incident_date) 唯一约束。
ALTER TABLE claims ADD COLUMN IF NOT EXISTS report_no TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS revision_no INTEGER;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS accident_no TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS accident_desc TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS report_batch_no TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS claims_report_no_unique
    ON claims (report_no) WHERE report_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS claims_report_batch_idx
    ON claims (report_batch_no) WHERE report_batch_no IS NOT NULL;

-- 批次主表：batch_no 是合作方批次重试的幂等键，receipt 保存原始回执快照。
CREATE SEQUENCE IF NOT EXISTS report_batch_no_seq;

CREATE TABLE IF NOT EXISTS report_batches (
    id SERIAL PRIMARY KEY,
    batch_no TEXT UNIQUE NOT NULL,
    partner_code TEXT,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected')),
    total_count INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    stale_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    request_hash TEXT NOT NULL,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    reject_reason TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 每个批次内每一条报案的逐条处理结果（原回执的明细来源）。
CREATE TABLE IF NOT EXISTS report_batch_items (
    id SERIAL PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES report_batches(id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL,
    report_no TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN (
        'created', 'updated', 'duplicate', 'stale_revision',
        'pending_confirmation', 'conflict', 'invalid', 'not_processed'
    )),
    message TEXT NOT NULL DEFAULT '',
    claim_id INTEGER REFERENCES claims(id) ON DELETE SET NULL,
    claim_no TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, item_index)
);

CREATE INDEX IF NOT EXISTS report_batch_items_report_idx
    ON report_batch_items (report_no, revision_no);

-- 报案号台账：每个 (报案号, 修订号) 第一次出现时的内容指纹。
-- 同号同修订但内容不同会违反业务一致性，整批退回；同号同修订同内容按重复报送幂等处理。
CREATE TABLE IF NOT EXISTS partner_reports (
    id SERIAL PRIMARY KEY,
    report_no TEXT UNIQUE NOT NULL,
    claim_id INTEGER REFERENCES claims(id) ON DELETE SET NULL,
    current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    policy_no TEXT,
    incident_date DATE,
    accident_no TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS report_revisions (
    id SERIAL PRIMARY KEY,
    report_no TEXT NOT NULL,
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    content_hash TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- applied：已写入案件；pending：案件已分级，留待人工确认；
    -- confirmed / rejected：人工确认结论；superseded：被更高修订确认后自动失效。
    status TEXT NOT NULL CHECK (status IN ('applied', 'pending', 'confirmed', 'rejected', 'superseded')),
    claim_id INTEGER REFERENCES claims(id) ON DELETE SET NULL,
    batch_no TEXT,
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (report_no, revision_no)
);

CREATE INDEX IF NOT EXISTS report_revisions_pending_idx
    ON report_revisions (status, created_at) WHERE status = 'pending';
