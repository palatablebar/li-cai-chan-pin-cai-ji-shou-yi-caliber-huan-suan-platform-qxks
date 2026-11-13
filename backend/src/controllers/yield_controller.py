"""收益口径控制器：发起换算、查看结果、同类分组、人工复核。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, yield_service


def _hide_fee(principal: dict) -> bool:
    return principal.get("role") == "viewer"


def list_runs(db: Session, **filters) -> dict:
    return yield_service.list_runs(db, **filters)


def get_run(db: Session, run_id: int) -> dict:
    return yield_service.serialize_run(yield_service.get_run(db, run_id))


def create_run(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    batch_key = payload.get("batch_key")
    if batch_key:
        # 增量换算：受影响产品由该采集批次实际改写到当前有效值的版本推导
        from src.models.quote_version import QuoteVersion
        from sqlalchemy import select

        affected = db.scalars(
            select(QuoteVersion.product_id)
            .where(QuoteVersion.batch_key == batch_key, QuoteVersion.status == "active")
            .distinct()
        ).all()
        run = yield_service.refresh_for_batch(
            db,
            batch_key=batch_key,
            affected_product_ids=list(affected),
            operator_email=principal["email"],
        )
    else:
        run = yield_service.create_and_execute(
            db,
            trigger=payload.get("trigger") or "manual",
            operator_email=principal["email"],
        )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="yield.run",
        entity="yield_run",
        entity_id=run.id,
        detail={
            "run_no": run.run_no,
            "status": run.status,
            "scope": run.scope,
            "views": run.view_count,
            "is_published": run.is_published,
            "batch_key": batch_key,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return yield_service.serialize_run(run)


def list_views(db: Session, principal: dict, **filters) -> dict:
    return yield_service.list_views(db, hide_fee=_hide_fee(principal), **filters)


def get_view(db: Session, view_id: int) -> dict:
    return yield_service.get_view(db, view_id)


def groups(db: Session, run_id: int | None, top_n: int, principal: dict) -> dict:
    return yield_service.groups(db, run_id, top_n, hide_fee=_hide_fee(principal))


def publish_diff(db: Session, run_id: int | None) -> dict:
    return yield_service.publish_diff(db, run_id)


def review(db: Session, view_id: int, payload: dict, principal: dict, request: Request) -> dict:
    from src.models.yield_view import YieldView

    view = db.get(YieldView, view_id)
    if view is None:
        from src.utils.errors import not_found

        raise not_found("yield_view", view_id)
    yield_service.review(db, view, status=payload["status"], note=payload.get("note"), reviewer=principal["email"])
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="yield.review",
        entity="yield_view",
        entity_id=view.id,
        detail={"status": view.status, "method": view.method, "comparable_pct": view.comparable_pct},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return yield_service.get_view(db, view_id)
