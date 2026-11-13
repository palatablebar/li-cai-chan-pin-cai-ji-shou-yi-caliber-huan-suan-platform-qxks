"""报价控制器：版本化导入（来源版本留痕、重试去重）与版本历史查询。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, quote_service


def list_quotes(db: Session, **filters) -> dict:
    return quote_service.list_quotes(db, **filters)


def list_versions(db: Session, *, product_id: int, quote_date: str | None) -> dict:
    return quote_service.list_versions(db, product_id=product_id, quote_date=quote_date)


def create_quote(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    quote = quote_service.create(db, payload, source="manual")
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="quote.import",
        entity="quote",
        entity_id=quote.id,
        detail={
            "product_id": quote.product_id,
            "quote_date": quote.quote_date.isoformat(),
            "source_version": quote.source_version,
            "corrected_at": quote.corrected_at.isoformat() if quote.corrected_at else None,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return quote_service.serialize(quote)


def import_quotes(db: Session, rows: list[dict], principal: dict, request: Request, batch_key: str | None = None) -> dict:
    result = quote_service.import_rows(db, rows, source="import", batch_key=batch_key)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="quote.import",
        entity="quote",
        entity_id=None,
        detail=result,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result
