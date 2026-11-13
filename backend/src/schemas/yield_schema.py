from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class YieldRunIn(BaseModel):
    trigger: Literal["manual", "schedule"] = "manual"
    note: str | None = Field(default=None, max_length=200)
    # 采集批次号：传入后按该批次受影响产品做增量换算；同批次重试幂等不新增运行
    batch_key: str | None = Field(default=None, max_length=120)


class ViewReviewIn(BaseModel):
    status: Literal["pending", "confirmed", "dismissed"]
    note: str | None = Field(default=None, max_length=1000)
