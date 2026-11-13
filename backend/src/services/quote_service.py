"""行情报价服务：机构版本化采集 + 当前有效值查询。

两套写入路径：
  1. 版本化（import / 带 corrected_at 的补录）：同一产品 + 报价日保留全部来源版本，
     按 corrected_at 选当前有效值；同来源版本重复投递（采集重试）去重不新增。
  2. 传统手工补录（不带 corrected_at）：维持自然键冲突 409 的既有契约。

quotes 表只保存每个 (product_id, quote_date) 的当前有效值并回链 quote_versions.active 行，
换算服务始终读 quotes，因此拿到的永远是「机构 + 报价日 + 更正时间」选出的有效值。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    QUOTE_SOURCE_LABELS,
    QUOTE_VERSION_STATUS_LABELS,
    QuoteVersionStatus,
)
from src.constants.limits import MAX_IMPORT_ROWS
from src.models.product import Product
from src.models.quote import Quote
from src.models.quote_version import QuoteVersion
from src.utils.errors import AppError
from src.utils.timeutil import iso_date, iso_utc

FIELDS = ("nav", "seven_day_pct", "wanfen_yield", "bench_low", "bench_high", "year_pct")
VERSION_FIELDS = (*FIELDS, "source_version", "corrected_at", "batch_key")


def _parse_corrected_at(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise AppError("VALIDATION", 422, {"corrected_at": str(value)}) from exc
    # 统一按 naive UTC 存储，与项目其他时间戳口径一致
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _default_source_version(source: str, corrected_at: datetime) -> str:
    return f"{source}:{corrected_at.strftime('%Y%m%d%H%M%S%f')}"


def _field_values(row: Quote | QuoteVersion) -> tuple:
    return tuple(getattr(row, field) for field in FIELDS)


def _payload_values(payload: dict) -> tuple:
    return tuple(payload.get(field) for field in FIELDS)


def _apply_fields(row: Quote | QuoteVersion, payload: dict) -> None:
    for field in FIELDS:
        if field in payload:
            setattr(row, field, payload[field])


def serialize(quote: Quote, *, product: Product | None = None) -> dict:
    return {
        "id": quote.id,
        "product_id": quote.product_id,
        "product_code": product.code if product else None,
        "product_name": product.name if product else None,
        "quote_date": iso_date(quote.quote_date),
        "nav": quote.nav,
        "seven_day_pct": quote.seven_day_pct,
        "wanfen_yield": quote.wanfen_yield,
        "bench_low": quote.bench_low,
        "bench_high": quote.bench_high,
        "year_pct": quote.year_pct,
        "source": quote.source,
        "source_label": QUOTE_SOURCE_LABELS.get(quote.source, quote.source),
        "source_version": getattr(quote, "source_version", None),
        "corrected_at": iso_utc(getattr(quote, "corrected_at", None)),
        "batch_key": getattr(quote, "batch_key", None),
        "active_version_id": getattr(quote, "active_version_id", None),
        "created_at": iso_utc(quote.created_at),
        "updated_at": iso_utc(getattr(quote, "updated_at", None)),
    }


def serialize_version(version: QuoteVersion, *, product: Product | None = None) -> dict:
    return {
        "id": version.id,
        "product_id": version.product_id,
        "product_code": product.code if product else None,
        "product_name": product.name if product else None,
        "quote_date": iso_date(version.quote_date),
        "nav": version.nav,
        "seven_day_pct": version.seven_day_pct,
        "wanfen_yield": version.wanfen_yield,
        "bench_low": version.bench_low,
        "bench_high": version.bench_high,
        "year_pct": version.year_pct,
        "source": version.source,
        "source_label": QUOTE_SOURCE_LABELS.get(version.source, version.source),
        "source_version": version.source_version,
        "corrected_at": iso_utc(version.corrected_at),
        "batch_key": version.batch_key,
        "status": version.status,
        "status_label": QUOTE_VERSION_STATUS_LABELS.get(version.status, version.status),
        "is_effective": version.status == QuoteVersionStatus.ACTIVE.value,
        "created_at": iso_utc(version.created_at),
    }


def list_quotes(
    db: Session,
    *,
    product_id: int | None = None,
    quote_date: str | None = None,
    method_missing: bool = False,
    limit: int = 60,
    offset: int = 0,
) -> dict:
    stmt = select(Quote)
    count_stmt = select(func.count()).select_from(Quote)
    if product_id:
        stmt = stmt.where(Quote.product_id == product_id)
        count_stmt = count_stmt.where(Quote.product_id == product_id)
    if quote_date:
        stmt = stmt.where(Quote.quote_date == date.fromisoformat(quote_date))
        count_stmt = count_stmt.where(Quote.quote_date == date.fromisoformat(quote_date))
    if method_missing:
        condition = Quote.seven_day_pct.is_(None) & Quote.wanfen_yield.is_(None) & Quote.nav.is_(None)
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(Quote.quote_date.desc(), Quote.product_id).limit(limit).offset(offset)
    ).all()
    products = {product.id: product for product in db.scalars(select(Product)).all()}
    return {
        "items": [serialize(row, product=products.get(row.product_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def list_versions(
    db: Session,
    *,
    product_id: int,
    quote_date: str | None = None,
    limit: int = 100,
) -> dict:
    """某只产品（可限定报价日）的全部来源版本，按报价日、更正时间倒序，当前有效排最前。"""
    stmt = select(QuoteVersion).where(QuoteVersion.product_id == product_id)
    if quote_date:
        stmt = stmt.where(QuoteVersion.quote_date == date.fromisoformat(quote_date))
    rows = db.scalars(
        stmt.order_by(
            QuoteVersion.quote_date.desc(),
            QuoteVersion.corrected_at.desc(),
            QuoteVersion.id.desc(),
        ).limit(limit)
    ).all()
    product = db.get(Product, product_id)
    return {
        "items": [serialize_version(row, product=product) for row in rows],
        "total": len(rows),
        "limit": limit,
    }


def _resolve_product(db: Session, payload: dict) -> Product:
    product = None
    if payload.get("product_id"):
        product = db.get(Product, int(payload["product_id"]))
    elif payload.get("product_code"):
        product = db.scalar(select(Product).where(Product.code == payload["product_code"]))
    if product is None:
        raise AppError("NOT_FOUND", 404, {"entity": "product", "id": payload.get("product_id") or payload.get("product_code")})
    return product


def _assign(quote: Quote, payload: dict, source: str) -> None:
    _apply_fields(quote, payload)
    quote.source = source


def create(db: Session, payload: dict, source: str = "manual") -> Quote:
    """单条写入：带 corrected_at 走版本化通道；否则维持自然键冲突 409 的原契约。"""
    if payload.get("corrected_at"):
        result = ingest_versions(db, [payload], source=source)
        product = _resolve_product(db, payload)
        quote_date = date.fromisoformat(str(payload["quote_date"])[:10])
        quote = db.scalar(select(Quote).where(Quote.product_id == product.id, Quote.quote_date == quote_date))
        if quote is None:  # pragma: no cover - ingest 必然落当前有效值
            raise AppError("VALIDATION", 422, {"ingest": result})
        return quote

    product = _resolve_product(db, payload)
    quote_date = date.fromisoformat(str(payload["quote_date"])[:10])
    exists = db.scalar(select(Quote).where(Quote.product_id == product.id, Quote.quote_date == quote_date))
    if exists is not None:
        raise AppError(
            "CONFLICT",
            409,
            {"product_id": product.id, "quote_date": quote_date.isoformat(), "quote_id": exists.id},
        )
    quote = Quote(product_id=product.id, quote_date=quote_date)
    _assign(quote, payload, source)
    db.add(quote)
    db.flush()
    return quote


def ingest_versions(db: Session, rows: list[dict], *, source: str = "import", default_batch_key: str | None = None) -> dict:
    """版本化批量写入。

    选择规则（机构 + 报价日 + 更正时间）：
      - 同 (product, quote_date, source_version) 已存在 → 采集重试，去重不新增；
      - 新版本 corrected_at 晚于当前 active（或当日尚无版本）→ 成为 active 并替换 quotes 有效值，
        旧 active 标 superseded 保留；
      - 更正时间更早的迟到版本 → 直接以 superseded 留痕，不影响当前有效值；
      - corrected_at 相同则后入库的版本（更大 id）生效，保证结果确定。
    """
    if len(rows) > MAX_IMPORT_ROWS:
        raise AppError("VALIDATION", 422, {"rows": len(rows), "max": MAX_IMPORT_ROWS})
    counters = {"created": 0, "updated": 0, "superseded": 0, "duplicate": 0, "archived": 0, "skipped": 0}
    affected_product_ids: set[int] = set()
    for payload in rows:
        try:
            product = _resolve_product(db, payload)
            quote_date = date.fromisoformat(str(payload["quote_date"])[:10])
            corrected_at = _parse_corrected_at(payload.get("corrected_at"))
            if corrected_at is None:
                raise ValueError("corrected_at required")
        except (AppError, KeyError, ValueError):
            counters["skipped"] += 1
            continue

        source_version = payload.get("source_version") or _default_source_version(source, corrected_at)
        batch_key = payload.get("batch_key") or default_batch_key

        duplicate = db.scalar(
            select(QuoteVersion).where(
                QuoteVersion.product_id == product.id,
                QuoteVersion.quote_date == quote_date,
                QuoteVersion.source_version == source_version,
            )
        )
        if duplicate is not None:
            # 同一采集任务重试：版本已在，不新增任何结果
            counters["duplicate"] += 1
            continue

        active = db.scalar(
            select(QuoteVersion).where(
                QuoteVersion.product_id == product.id,
                QuoteVersion.quote_date == quote_date,
                QuoteVersion.status == QuoteVersionStatus.ACTIVE.value,
            )
        )
        becomes_effective = active is None or corrected_at >= active.corrected_at

        version = QuoteVersion(
            product_id=product.id,
            quote_date=quote_date,
            source=source,
            source_version=source_version,
            corrected_at=corrected_at,
            batch_key=batch_key,
            status=QuoteVersionStatus.ACTIVE.value if becomes_effective else QuoteVersionStatus.SUPERSEDED.value,
        )
        _apply_fields(version, payload)
        db.add(version)
        db.flush()
        counters["created"] += 1

        if not becomes_effective:
            counters["archived"] += 1
            continue

        if active is not None:
            active.status = QuoteVersionStatus.SUPERSEDED.value
            counters["superseded"] += 1

        quote = db.scalar(select(Quote).where(Quote.product_id == product.id, Quote.quote_date == quote_date))
        content_changed = quote is None or _field_values(quote) != _payload_values(payload)
        if quote is None:
            quote = Quote(product_id=product.id, quote_date=quote_date)
            db.add(quote)
        _apply_fields(quote, payload)
        quote.source = source
        quote.source_version = source_version
        quote.corrected_at = corrected_at
        quote.batch_key = batch_key
        quote.active_version_id = version.id
        if content_changed:
            counters["updated"] += 1
            affected_product_ids.add(product.id)
    db.flush()
    counters["affected_product_ids"] = sorted(affected_product_ids)
    return counters


def import_rows(db: Session, rows: list[dict], source: str = "import", *, batch_key: str | None = None) -> dict:
    """批量导入：版本化 upsert，返回新建/替换/去重数量与受影响产品。"""
    return ingest_versions(db, rows, source=source, default_batch_key=batch_key)
