"""存量库启动时幂等补列（002 版本化改造）的回归测试。

模拟只含旧表结构的库，验证 _ensure_runtime_columns：
  1. 补齐 quote_versions 新表与 quotes / yield_runs / yield_views 新列；
  2. 存量成功运行被视为已发布，避免升级后正式榜为空；
  3. 重复执行幂等，不报错、不重复加列。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import app as app_module
from src.config import database as db_module
from src.models import Base
from src.models.yield_run import YieldRun


LEGACY_DDL = [
    """CREATE TABLE products (id INTEGER PRIMARY KEY, code VARCHAR(40) UNIQUE, name VARCHAR(120),
       issuer VARCHAR(80), type VARCHAR(20), risk_level VARCHAR(4), min_amount FLOAT DEFAULT 10000,
       term_days INTEGER DEFAULT 0, fee_rate FLOAT DEFAULT 0, active BOOLEAN DEFAULT 1, note TEXT,
       created_at TIMESTAMP, updated_at TIMESTAMP)""",
    """CREATE TABLE quotes (id INTEGER PRIMARY KEY, product_id INTEGER, quote_date DATE, nav FLOAT,
       seven_day_pct FLOAT, wanfen_yield FLOAT, bench_low FLOAT, bench_high FLOAT, year_pct FLOAT,
       source VARCHAR(20) DEFAULT 'seed', created_at TIMESTAMP, UNIQUE(product_id, quote_date))""",
    """CREATE TABLE yield_runs (id INTEGER PRIMARY KEY, run_no VARCHAR(40) UNIQUE, trigger VARCHAR(20),
       status VARCHAR(20), operator_email VARCHAR(160), product_count INTEGER DEFAULT 0,
       view_count INTEGER DEFAULT 0, metrics JSON, log_text TEXT, seed_key VARCHAR(120) UNIQUE,
       started_at TIMESTAMP, finished_at TIMESTAMP)""",
    """CREATE TABLE yield_views (id INTEGER PRIMARY KEY, run_id INTEGER, product_id INTEGER,
       comparable_pct FLOAT, method VARCHAR(16), confidence VARCHAR(10), peer_rank INTEGER,
       risk_adj FLOAT, group_key VARCHAR(60), group_size INTEGER DEFAULT 0, vol_annual FLOAT,
       status VARCHAR(16) DEFAULT 'pending', flags JSON, note TEXT, reviewer VARCHAR(160),
       review_note TEXT, created_at TIMESTAMP, updated_at TIMESTAMP, UNIQUE(run_id, product_id))""",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE watches (id INTEGER PRIMARY KEY)",
    "CREATE TABLE audit_logs (id INTEGER PRIMARY KEY)",
]


@pytest.fixture()
def legacy_engine(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        for ddl in LEGACY_DDL:
            conn.exec_driver_sql(ddl)
        conn.exec_driver_sql(
            "INSERT INTO yield_runs (id, run_no, trigger, status, operator_email, started_at, finished_at) "
            "VALUES (1, 'YR-LEGACY', 'seed', 'success', 's@x', '2026-11-13 00:00:00', '2026-11-13 00:01:00')"
        )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(app_module, "engine", engine)
    yield engine
    engine.dispose()


def test_runtime_backfill_adds_columns_and_publishes_legacy_runs(legacy_engine):
    Base.metadata.create_all(legacy_engine)
    app_module._ensure_runtime_columns()

    inspector = inspect(legacy_engine)
    quote_cols = {c["name"] for c in inspector.get_columns("quotes")}
    assert {"source_version", "corrected_at", "batch_key", "active_version_id", "updated_at"} <= quote_cols
    run_cols = {c["name"] for c in inspector.get_columns("yield_runs")}
    assert {"is_published", "published_at", "scope", "batch_key", "supersedes_run_id"} <= run_cols
    view_cols = {c["name"] for c in inspector.get_columns("yield_views")}
    assert {"quote_date_snapshot", "active_version_id", "carried_from_view_id"} <= view_cols
    assert "quote_versions" in inspector.get_table_names()

    session = sessionmaker(bind=legacy_engine, future=True)()
    try:
        legacy_run = session.get(YieldRun, 1)
        assert legacy_run.is_published is True
        assert legacy_run.published_at is not None
    finally:
        session.close()


def test_runtime_backfill_is_idempotent(legacy_engine):
    Base.metadata.create_all(legacy_engine)
    app_module._ensure_runtime_columns()
    inspector_first = inspect(legacy_engine)
    columns_first = {
        table: sorted(c["name"] for c in inspector_first.get_columns(table))
        for table in ("quotes", "yield_runs", "yield_views")
    }
    # 第二次调用不应抛错，列集合保持不变
    app_module._ensure_runtime_columns()
    inspector_second = inspect(legacy_engine)
    columns_second = {
        table: sorted(c["name"] for c in inspector_second.get_columns(table))
        for table in ("quotes", "yield_runs", "yield_views")
    }
    assert columns_first == columns_second
