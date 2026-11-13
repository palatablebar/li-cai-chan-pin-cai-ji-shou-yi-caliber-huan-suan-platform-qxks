"""FastAPI 装配：中间件顺序、异常处理、路由挂载、启动钩子。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config.database import engine, wait_for_db
from src.config.settings import settings
from src.middlewares.audit_logger import AuditMiddleware
from src.middlewares.auth import AuthMiddleware
from src.middlewares.error_handler import register_exception_handlers
from src.middlewares.rate_limit import RateLimitMiddleware
from src.routes import api_router

logger = logging.getLogger(f"{settings.app_name}.app")


def _bootstrap_database() -> None:
    from src.models import Base  # noqa: WPS433 - 延迟导入避免循环

    wait_for_db()
    Base.metadata.create_all(bind=engine)
    _ensure_runtime_columns()


def _ensure_runtime_columns() -> None:
    """对存量库幂等补齐 002 版本化改造的新增列（全新库 create_all 已含，检查为空操作）。"""
    from sqlalchemy import inspect, text

    # 每次调用即时反射，避免持有模块级 inspector 在 create_all 之后看到过期结构
    inspector = inspect(engine)

    def _columns(table: str) -> set[str]:
        return {column["name"] for column in inspector.get_columns(table)} if inspector.has_table(table) else set()

    quote_cols = _columns("quotes")
    run_cols = _columns("yield_runs")
    view_cols = _columns("yield_views")
    has_quotes = inspector.has_table("quotes")
    has_runs = inspector.has_table("yield_runs")
    has_views = inspector.has_table("yield_views")
    statements: list[str] = []
    if has_quotes:
        if "source_version" not in quote_cols:
            statements.append("ALTER TABLE quotes ADD COLUMN source_version VARCHAR(80)")
        if "corrected_at" not in quote_cols:
            statements.append("ALTER TABLE quotes ADD COLUMN corrected_at TIMESTAMP")
        if "batch_key" not in quote_cols:
            statements.append("ALTER TABLE quotes ADD COLUMN batch_key VARCHAR(120)")
        if "active_version_id" not in quote_cols:
            statements.append("ALTER TABLE quotes ADD COLUMN active_version_id INTEGER")
        if "updated_at" not in quote_cols:
            statements.append("ALTER TABLE quotes ADD COLUMN updated_at TIMESTAMP")
    if has_runs:
        if "batch_key" not in run_cols:
            statements.append("ALTER TABLE yield_runs ADD COLUMN batch_key VARCHAR(120)")
        if "scope" not in run_cols:
            statements.append("ALTER TABLE yield_runs ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT 'full'")
        if "is_published" not in run_cols:
            statements.append("ALTER TABLE yield_runs ADD COLUMN is_published BOOLEAN NOT NULL DEFAULT FALSE")
        if "published_at" not in run_cols:
            statements.append("ALTER TABLE yield_runs ADD COLUMN published_at TIMESTAMP")
        if "supersedes_run_id" not in run_cols:
            statements.append("ALTER TABLE yield_runs ADD COLUMN supersedes_run_id INTEGER")
        statements.append(
            "UPDATE yield_runs SET is_published = TRUE, published_at = COALESCE(published_at, finished_at, started_at) "
            "WHERE status IN ('success', 'partial') AND is_published = FALSE"
        )
    if has_views:
        if "quote_date_snapshot" not in view_cols:
            statements.append("ALTER TABLE yield_views ADD COLUMN quote_date_snapshot DATE")
        if "active_version_id" not in view_cols:
            statements.append("ALTER TABLE yield_views ADD COLUMN active_version_id INTEGER")
        if "carried_from_view_id" not in view_cols:
            statements.append("ALTER TABLE yield_views ADD COLUMN carried_from_view_id INTEGER")
    if not statements:
        return
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    logger.info("runtime schema backfill applied: %s statements", len(statements))


def _run_seed_once() -> None:
    if not settings.seed_demo:
        return
    try:
        from scripts.seed_demo import seed_all

        seed_all()
    except Exception as exc:  # noqa: BLE001 - 启动阶段不阻塞
        logger.warning("[SEED_DEMO] seed failed (ignored): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_database()
    _run_seed_once()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_title, version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AuthMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RateLimitMiddleware)
    register_exception_handlers(app)

    app.include_router(api_router, prefix="/api")
    app.include_router(api_router, include_in_schema=False)
    return app
