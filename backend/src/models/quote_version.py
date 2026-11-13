"""行情报价的机构版本流水：同一产品同一报价日可有多条来源版本。

机构每晚补发同日收益时，按「产品（机构口径产品）+ 报价日 + 更正时间」留存全部版本：
  - 同一来源版本重复投递（含采集任务重试）去重，不新增行；
  - 更正时间更晚的版本成为当前有效值（active），旧版本标 superseded 保留可查；
  - quotes 表始终只放每个 (product_id, quote_date) 的当前有效值，并回链 active 版本。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class QuoteVersion(Base):
    __tablename__ = "quote_versions"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "quote_date",
            "source_version",
            name="uq_quote_version_source",
        ),
        Index("ix_quote_version_lookup", "product_id", "quote_date", "corrected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    quote_date: Mapped[date] = mapped_column(Date, index=True)
    nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    seven_day_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    wanfen_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    year_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="import")
    source_version: Mapped[str] = mapped_column(String(80), default="", index=True)
    corrected_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    batch_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
