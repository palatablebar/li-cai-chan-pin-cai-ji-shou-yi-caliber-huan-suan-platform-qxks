from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator


class QuoteIn(BaseModel):
    product_id: int | None = Field(default=None, ge=1)
    product_code: str | None = Field(default=None, min_length=3, max_length=40)
    quote_date: date
    nav: float | None = Field(default=None, ge=0, le=1000)
    seven_day_pct: float | None = Field(default=None, ge=-50, le=100)
    wanfen_yield: float | None = Field(default=None, ge=-100, le=1000)
    bench_low: float | None = Field(default=None, ge=-50, le=100)
    bench_high: float | None = Field(default=None, ge=-50, le=100)
    year_pct: float | None = Field(default=None, ge=-100, le=200)
    # 机构补发同日收益时的版本信息：带 corrected_at 即走版本化通道（保留被替换版本）
    source_version: str | None = Field(default=None, max_length=80)
    corrected_at: datetime | None = None
    batch_key: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _need_product(self) -> "QuoteIn":
        if not self.product_id and not self.product_code:
            raise ValueError("product_id 或 product_code 至少填一个")
        return self


class QuoteImportIn(BaseModel):
    rows: list[QuoteIn] = Field(min_length=1, max_length=5000)
    batch_key: str | None = Field(default=None, max_length=120)
