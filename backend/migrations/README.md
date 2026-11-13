# 数据库迁移说明

本项目用 SQLAlchemy `Base.metadata.create_all()` 在 backend 启动时建表（见 `src/config/app.py`），
因此首次 `docker compose up` 会自动建好表，不需要额外执行迁移。

- `001_core_tables.sql`：核心表的人类可读 DDL 参考（PostgreSQL 语法），用于 reviewer 对照字段；
  运行时不执行它。
- `002_quote_versioning.sql`：版本化报价 + 正式榜发布机制的存量库迁移（幂等，可重复执行）：
  - 新建 `quote_versions`（来源版本流水，active/superseded）；
  - `quotes` 补 `source_version / corrected_at / batch_key / active_version_id / updated_at`，
    并把存量报价回填成一条 active 版本；
  - `yield_runs` 补 `batch_key / scope / is_published / published_at / supersedes_run_id`，
    存量成功/部分成功运行视为已发布；
  - `yield_views` 补 `quote_date_snapshot / active_version_id / carried_from_view_id`。
- 即使没有手动执行 002，应用启动时 `_ensure_runtime_columns()` 也会对存量库幂等补列
  （全新库 `create_all` 已含最终结构，该步骤为空操作）。
- 后续如果要做更复杂的字段变更，建议引入 Alembic，并在本目录按 `00x_<描述>.py` 递增。
- seed 数据只走 `backend/scripts/seed_demo.py`，不要写进 SQL。
