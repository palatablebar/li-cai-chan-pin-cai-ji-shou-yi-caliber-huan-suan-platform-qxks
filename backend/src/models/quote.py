"""每日行情报价（当前有效值表）：每个产品 + 报价日仅一条，内容指向 quote_versions 的 active 版本。

机构补发同日收益时不改写历史：新版本先落 quote_versions，再由服务层把本表替换成新有效值，
旧版本在 quote_versions 中以 superseded 状态保留，换算依据可追溯。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (UniqueConstraint("product_id", "quote_date", name="uq_quotes_product_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    quote_date: Mapped[date] = mapped_column(Date, index=True)
    nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    seven_day_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    wanfen_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    year_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="seed")
    source_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    batch_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    active_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("quote_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
