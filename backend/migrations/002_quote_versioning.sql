-- 002 版本化报价与正式榜发布机制（幂等：重复执行不报错、不重复加列）
-- 适用：已经由 001 建表的存量 PostgreSQL 库；全新库由 SQLAlchemy create_all 直接建出最终结构。
-- 运行：psql "$DATABASE_URL" -f backend/migrations/002_quote_versioning.sql

-- 1) 报价版本流水表 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quote_versions (
    id              SERIAL PRIMARY KEY,
    product_id      INTEGER      NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    quote_date      DATE         NOT NULL,
    nav             DOUBLE PRECISION,
    seven_day_pct   DOUBLE PRECISION,
    wanfen_yield    DOUBLE PRECISION,
    bench_low       DOUBLE PRECISION,
    bench_high      DOUBLE PRECISION,
    year_pct        DOUBLE PRECISION,
    source          VARCHAR(20)  NOT NULL DEFAULT 'import',
    source_version  VARCHAR(80)  NOT NULL DEFAULT '',
    corrected_at    TIMESTAMP    NOT NULL,
    batch_key       VARCHAR(120),
    status          VARCHAR(16)  NOT NULL DEFAULT 'active',
    created_at      TIMESTAMP    NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_quote_version_source') THEN
        ALTER TABLE quote_versions
            ADD CONSTRAINT uq_quote_version_source UNIQUE (product_id, quote_date, source_version);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_quote_version_lookup ON quote_versions (product_id, quote_date, corrected_at);

-- 2) quotes 当前有效值表补列 -------------------------------------------------
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS source_version    VARCHAR(80);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS corrected_at      TIMESTAMP;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS batch_key         VARCHAR(120);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS active_version_id INTEGER REFERENCES quote_versions(id) ON DELETE SET NULL;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS updated_at        TIMESTAMP NOT NULL DEFAULT now();

-- 3) 存量报价回填一条 active 版本，保证换算依据不悬空 ---------------------------
INSERT INTO quote_versions (
    product_id, quote_date, nav, seven_day_pct, wanfen_yield, bench_low, bench_high,
    year_pct, source, source_version, corrected_at, batch_key, status, created_at
)
SELECT
    q.product_id, q.quote_date, q.nav, q.seven_day_pct, q.wanfen_yield, q.bench_low, q.bench_high,
    q.year_pct, q.source,
    COALESCE(q.source_version, 'legacy:' || q.product_id || ':' || to_char(q.quote_date, 'YYYYMMDD')),
    COALESCE(q.corrected_at, q.created_at),
    COALESCE(q.batch_key, 'legacy-backfill'),
    'active',
    q.created_at
FROM quotes q
WHERE q.active_version_id IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM quote_versions v
      WHERE v.product_id = q.product_id AND v.quote_date = q.quote_date
  );

UPDATE quotes q
SET active_version_id = v.id,
    source_version   = COALESCE(q.source_version, v.source_version),
    corrected_at     = COALESCE(q.corrected_at, v.corrected_at),
    batch_key        = COALESCE(q.batch_key, v.batch_key)
FROM quote_versions v
WHERE q.active_version_id IS NULL
  AND v.product_id = q.product_id
  AND v.quote_date = q.quote_date
  AND v.status = 'active';

-- 4) 换算运行：批次/范围/发布指针 --------------------------------------------
ALTER TABLE yield_runs ADD COLUMN IF NOT EXISTS batch_key         VARCHAR(120);
ALTER TABLE yield_runs ADD COLUMN IF NOT EXISTS scope             VARCHAR(16) NOT NULL DEFAULT 'full';
ALTER TABLE yield_runs ADD COLUMN IF NOT EXISTS is_published      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE yield_runs ADD COLUMN IF NOT EXISTS published_at      TIMESTAMP;
ALTER TABLE yield_runs ADD COLUMN IF NOT EXISTS supersedes_run_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_yield_runs_published ON yield_runs (is_published);
CREATE INDEX IF NOT EXISTS ix_yield_runs_batch_key ON yield_runs (batch_key);

-- 存量已完成的运行视为已发布，避免升级后正式榜为空
UPDATE yield_runs
SET is_published = TRUE,
    published_at = COALESCE(published_at, finished_at, started_at)
WHERE status IN ('success', 'partial') AND is_published = FALSE;

-- 5) 换算结果：报价依据快照与沿用标记 -----------------------------------------
ALTER TABLE yield_views ADD COLUMN IF NOT EXISTS quote_date_snapshot  DATE;
ALTER TABLE yield_views ADD COLUMN IF NOT EXISTS active_version_id     INTEGER;
ALTER TABLE yield_views ADD COLUMN IF NOT EXISTS carried_from_view_id  INTEGER;
