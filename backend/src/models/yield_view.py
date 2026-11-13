"""换算结果：一只产品在一次运行里的可比年化、口径、置信度、同类排名与风险调整。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class YieldView(Base):
    __tablename__ = "yield_views"
    __table_args__ = (UniqueConstraint("run_id", "product_id", name="uq_views_run_product"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("yield_runs.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    comparable_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    method: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    confidence: Mapped[str] = mapped_column(String(10), default="none")
    peer_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_adj: Mapped[float | None] = mapped_column(Float, nullable=True)
    group_key: Mapped[str] = mapped_column(String(60), default="", index=True)
    group_size: Mapped[int] = mapped_column(Integer, default=0)
    vol_annual: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    flags: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本条结果换算时实际使用的报价依据（对应 quote_versions.active 行），保证名次可追溯
    quote_date_snapshot: Mapped[object] = mapped_column(Date, nullable=True)
    active_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 未变化产品沿用上一版复核结论时记录来源视图；本批重算的产品为 None
    carried_from_view_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
