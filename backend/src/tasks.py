"""Celery 任务：worker 心跳 + 每日口径换算 + 采集批次增量换算。

- daily_yield_refresh：每日 07:40 兜底全量重算，仍走「暂存 → 一次性发布」保护；
- refresh_batch：机构批次补发落库后调用，只重算受影响产品所在同类组；
  同一 batch_key 重试在 service 层幂等，不新增运行、不新增结果。
"""
from __future__ import annotations

import time

from src.config.celery_app import TASK_BATCH_REFRESH, TASK_DAILY, TASK_HEARTBEAT, celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat() -> str:
    try:
        _redis().set(f"{settings.app_name}:worker:heartbeat", time.time())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
        return "error"


@celery_app.task(name=TASK_DAILY)
def daily_yield_refresh() -> dict:
    """每日 07:40 由 beat 触发：按当前有效报价重算可比年化，写审计。"""
    from src.config.database import session_scope
    from src.services import audit_service, yield_service

    operator = "scheduler@" + settings.app_name + ".io"
    with session_scope() as db:
        run = yield_service.refresh_daily(db, operator)
        audit_service.record(
            db,
            actor_email=operator,
            actor_role="admin",
            action="yield.run",
            entity="yield_run",
            entity_id=run.id,
            detail={"trigger": "schedule", "run_no": run.run_no, "scope": run.scope, "views": run.view_count},
            layer="worker",
        )
    logger.info("daily yield refresh finished: %s", run.run_no)
    return {"run_no": run.run_no, "views": run.view_count}


@celery_app.task(name=TASK_BATCH_REFRESH)
def refresh_batch(batch_key: str) -> dict:
    """采集批次补发后的增量换算入口（可安全重试：同 batch_key 幂等）。"""
    from sqlalchemy import select

    from src.config.database import session_scope
    from src.models.quote_version import QuoteVersion
    from src.services import audit_service, yield_service
    operator = "scheduler@" + settings.app_name + ".io"
    with session_scope() as db:
        affected = db.scalars(
            select(QuoteVersion.product_id)
            .where(QuoteVersion.batch_key == batch_key, QuoteVersion.status == "active")
            .distinct()
        ).all()
        run = yield_service.refresh_for_batch(
            db,
            batch_key=batch_key,
            affected_product_ids=list(affected),
            operator_email=operator,
        )
        audit_service.record(
            db,
            actor_email=operator,
            actor_role="admin",
            action="yield.run",
            entity="yield_run",
            entity_id=run.id,
            detail={
                "trigger": "schedule",
                "run_no": run.run_no,
                "scope": run.scope,
                "batch_key": batch_key,
                "affected": len(affected),
                "views": run.view_count,
                "is_published": run.is_published,
            },
            layer="worker",
        )
    logger.info("batch refresh finished: %s -> %s", batch_key, run.run_no)
    return {"run_no": run.run_no, "batch_key": batch_key, "views": run.view_count, "published": run.is_published}
