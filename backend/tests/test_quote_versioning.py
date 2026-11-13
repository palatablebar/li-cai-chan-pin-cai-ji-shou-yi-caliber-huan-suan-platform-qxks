"""版本化报价 + 增量换算 + 正式榜发布机制的服务级测试。

用内存 SQLite 建全量表跑完整服务链路（不 mock 算法），覆盖：
  1. 机构补发：按更正时间选有效值，旧版本 superseded 保留；
  2. 采集重试：同 source_version / 同 batch_key 幂等，不新增结果；
  3. 增量换算：只重算受影响产品所在同类组，组外沿用原复核结论；
  4. 正式榜一次性切换，失败保留上一版；
  5. 发布差异报告（本批更新 / 名次变化 / 分组前后对照）；
  6. 传统手工补录不带 corrected_at 仍走 409 契约。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.models import Base
from src.models.product import Product
from src.models.quote import Quote
from src.models.quote_version import QuoteVersion
from src.models.yield_run import YieldRun
from src.models.yield_view import YieldView
from src.services import quote_service, yield_service
from src.utils.errors import AppError


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, future=True)()
    # 6 只固定收益类（同一同类组 fixed:d31_180，样本≥5 可排名）+ 2 只现金类（另一组）
    specs = [
        ("p01", "固定一号", "fixed", 90, 2.10),
        ("p02", "固定二号", "fixed", 100, 2.20),
        ("p03", "固定三号", "fixed", 110, 2.30),
        ("p04", "固定四号", "fixed", 120, 2.40),
        ("p05", "固定五号", "fixed", 130, 2.50),
        ("p06", "固定六号", "fixed", 140, 2.60),
        ("c01", "现金一号", "cash", 1, 1.80),
        ("c02", "现金二号", "cash", 1, 1.90),
    ]
    for index, (code, name, ptype, term, pct) in enumerate(specs, start=1):
        session.add(
            Product(
                id=index,
                code=code,
                name=name,
                issuer="测试机构",
                type=ptype,
                risk_level="R2",
                min_amount=10000,
                term_days=term,
                fee_rate=0.2,
                active=True,
            )
        )
    session.flush()
    yield session
    session.close()
    engine.dispose()


D = date(2026, 11, 13)


def _row(product_id: int, pct: float, *, corrected: datetime, source_version: str, batch_key: str | None = None) -> dict:
    return {
        "product_id": product_id,
        "quote_date": D,
        "seven_day_pct": pct,
        "corrected_at": corrected,
        "source_version": source_version,
        "batch_key": batch_key,
    }


def _seed_first_edition(db) -> YieldRun:
    rows = [
        _row(pid, pct, corrected=datetime(2026, 11, 13, 18, 0), source_version=f"v1-p{pid}", batch_key="batch-1")
        for pid, pct in [
            (1, 2.10), (2, 2.20), (3, 2.30), (4, 2.40), (5, 2.50), (6, 2.60),
            (7, 1.80), (8, 1.90),
        ]
    ]
    result = quote_service.ingest_versions(db, rows, source="import", default_batch_key="batch-1")
    assert result["created"] == 8
    assert result["duplicate"] == 0
    run = yield_service.create_and_execute(db, trigger="manual", operator_email="admin@x.io", seed_key="seed-1")
    assert run.is_published is True
    return run


# --------------------------------------------------------------------------
# 1. 版本选择：晚到的更正成为有效值，旧版本保留
# --------------------------------------------------------------------------
def test_correction_supersedes_old_version_and_keeps_history(db):
    _seed_first_edition(db)

    # 机构晚间补发 p01 的同日更正：七日年化 2.10 -> 3.00
    result = quote_service.ingest_versions(
        db,
        [_row(1, 3.00, corrected=datetime(2026, 11, 13, 21, 30), source_version="v2-p1", batch_key="batch-2")],
        source="import",
        default_batch_key="batch-2",
    )
    assert result["created"] == 1
    assert result["superseded"] == 1
    assert result["updated"] == 1
    assert result["affected_product_ids"] == [1]

    quote = db.scalar(select(Quote).where(Quote.product_id == 1, Quote.quote_date == D))
    assert quote.seven_day_pct == 3.00
    assert quote.source_version == "v2-p1"

    versions = db.scalars(
        select(QuoteVersion).where(QuoteVersion.product_id == 1).order_by(QuoteVersion.corrected_at)
    ).all()
    assert [v.status for v in versions] == ["superseded", "active"]
    assert {v.source_version for v in versions} == {"v1-p1", "v2-p1"}
    # 当前有效值回链 active 版本，换算依据可追溯
    assert quote.active_version_id == next(v.id for v in versions if v.status == "active")


def test_late_older_correction_is_archived_without_touching_effective_value(db):
    _seed_first_edition(db)
    quote_service.ingest_versions(
        db,
        # 更正时间比当前 active（18:00）更早的迟到版本
        [_row(1, 1.50, corrected=datetime(2026, 11, 13, 12, 0), source_version="v0-p1", batch_key="batch-late")],
        source="import",
    )
    quote = db.scalar(select(Quote).where(Quote.product_id == 1, Quote.quote_date == D))
    assert quote.seven_day_pct == 2.10  # 有效值不变
    archived = db.scalar(select(QuoteVersion).where(QuoteVersion.source_version == "v0-p1"))
    assert archived.status == "superseded"


# --------------------------------------------------------------------------
# 2. 采集任务重试不新增结果
# --------------------------------------------------------------------------
def test_retry_same_source_version_is_deduplicated(db):
    _seed_first_edition(db)
    payload = [_row(1, 3.00, corrected=datetime(2026, 11, 13, 21, 30), source_version="v2-p1", batch_key="batch-2")]
    first = quote_service.ingest_versions(db, payload, source="import")
    second = quote_service.ingest_versions(db, payload, source="import")  # 同一采集任务重试
    assert first["created"] == 1 and second["created"] == 0
    assert second["duplicate"] == 1
    assert db.scalar(select(func.count()).select_from(QuoteVersion)) == 9  # 8 初始 + 1 更正


def test_same_batch_key_does_not_create_second_run(db):
    _seed_first_edition(db)
    quote_service.ingest_versions(
        db,
        [_row(1, 3.00, corrected=datetime(2026, 11, 13, 21, 30), source_version="v2-p1", batch_key="batch-2")],
        source="import",
    )
    run_one = yield_service.refresh_for_batch(
        db, batch_key="batch-2", affected_product_ids=[1], operator_email="scheduler@x.io"
    )
    run_two = yield_service.refresh_for_batch(
        db, batch_key="batch-2", affected_product_ids=[1], operator_email="scheduler@x.io"
    )
    assert run_one.id == run_two.id
    assert db.scalar(select(func.count()).select_from(YieldRun)) == 2  # 初始 + 增量，重试不新增


# --------------------------------------------------------------------------
# 3. 增量换算：只重算受影响同类组，组外沿用复核结论
# --------------------------------------------------------------------------
def test_incremental_run_recomputes_only_affected_group_and_carries_reviews(db):
    first = _seed_first_edition(db)
    # 复核现金组产品 c01（不在受影响组），确认其结论不应被重开
    cash_view = db.scalar(select(YieldView).where(YieldView.run_id == first.id, YieldView.product_id == 7))
    yield_service.review(db, cash_view, status="confirmed", note="现金类已核对", reviewer="analyst@x.io")

    quote_service.ingest_versions(
        db,
        [_row(1, 3.00, corrected=datetime(2026, 11, 13, 21, 30), source_version="v2-p1", batch_key="batch-2")],
        source="import",
    )
    second = yield_service.refresh_for_batch(
        db, batch_key="batch-2", affected_product_ids=[1], operator_email="scheduler@x.io"
    )

    assert second.scope == "incremental"
    assert second.is_published is True
    assert second.supersedes_run_id == first.id
    views = db.scalars(select(YieldView).where(YieldView.run_id == second.id)).all()
    assert len(views) == 8  # 全产品结果仍齐全

    fixed_views = [v for v in views if v.group_key == "fixed:d31_180"]
    cash_views = [v for v in views if v.group_key == "cash:le30"]
    assert all(v.carried_from_view_id is None for v in fixed_views)  # 受影响组全部重算
    assert all(v.carried_from_view_id is not None for v in cash_views)  # 组外整行沿用

    carried_cash = next(v for v in cash_views if v.product_id == 7)
    assert carried_cash.status == "confirmed"
    assert carried_cash.reviewer == "analyst@x.io"
    assert carried_cash.review_note == "现金类已核对"

    # p01 从 2.10 升到 3.00，成为 fixed 组第一
    new_p01 = next(v for v in fixed_views if v.product_id == 1)
    assert new_p01.comparable_pct == 3.00
    assert new_p01.peer_rank == 1
    assert new_p01.status == "pending"  # 值变了，重新进入待复核
    assert new_p01.active_version_id is not None  # 记录了换算依据版本

    diff = second.metrics["publish_diff"]
    assert diff["changed_products"] == 1
    assert diff["recomputed_products"] == 6
    assert diff["carried_products"] == 2
    rank_change = {row["product_id"]: row for row in diff["rank_changes"]}
    assert 1 in rank_change
    group = next(g for g in diff["group_diff"] if g["group_key"] == "fixed:d31_180")
    assert group["top_after"]["product_id"] == 1
    assert group["top_before"]["product_id"] == 6


def test_unchanged_value_in_affected_group_keeps_review_conclusion(db):
    first = _seed_first_edition(db)
    fixed_p02 = db.scalar(select(YieldView).where(YieldView.run_id == first.id, YieldView.product_id == 2))
    yield_service.review(db, fixed_p02, status="confirmed", note="维持", reviewer="analyst@x.io")

    quote_service.ingest_versions(
        db,
        [_row(1, 3.00, corrected=datetime(2026, 11, 13, 21, 30), source_version="v2-p1", batch_key="batch-2")],
        source="import",
    )
    second = yield_service.refresh_for_batch(
        db, batch_key="batch-2", affected_product_ids=[1], operator_email="scheduler@x.io"
    )
    p02 = db.scalar(select(YieldView).where(YieldView.run_id == second.id, YieldView.product_id == 2))
    # p02 数值本身没变（只是组内名次可能变），复核结论沿用
    assert p02.comparable_pct == 2.20
    assert p02.status == "confirmed"
    assert p02.review_note == "维持"


# --------------------------------------------------------------------------
# 4. 正式榜一次性切换：失败保留上一版
# --------------------------------------------------------------------------
def test_failed_run_does_not_change_published_board(db, monkeypatch):
    first = _seed_first_edition(db)

    def _boom(db_session, product_ids=None):
        raise RuntimeError("simulated ranking failure")

    monkeypatch.setattr(yield_service, "compute_items", _boom)
    failed = yield_service.create_and_execute(
        db, trigger="manual", operator_email="admin@x.io", affected_product_ids=[1]
    )
    assert failed.status == "failed"
    assert failed.is_published is False

    # 正式榜仍指向上一版
    published = yield_service.latest_published_run(db)
    assert published.id == first.id
    groups = yield_service.groups(db)
    assert groups["run_id"] == first.id
    assert groups["published_run_id"] == first.id
    # 失败 run 不留半成品视图
    assert db.scalar(select(func.count()).select_from(YieldView).where(YieldView.run_id == failed.id)) == 0


# --------------------------------------------------------------------------
# 5. 正式榜默认读发布版；历史批次可回看
# --------------------------------------------------------------------------
def test_groups_default_to_published_run_and_support_history(db):
    first = _seed_first_edition(db)
    quote_service.ingest_versions(
        db,
        [_row(1, 3.00, corrected=datetime(2026, 11, 13, 21, 30), source_version="v2-p1", batch_key="batch-2")],
        source="import",
    )
    second = yield_service.refresh_for_batch(
        db, batch_key="batch-2", affected_product_ids=[1], operator_email="scheduler@x.io"
    )
    default_groups = yield_service.groups(db)
    assert default_groups["run_id"] == second.id
    history = yield_service.groups(db, run_id=first.id)
    history_fixed_top = next(
        row for g in history["items"] if g["group_key"] == "fixed:d31_180" for row in g["top"] if row["peer_rank"] == 1
    )
    assert history_fixed_top["product_id"] == 6  # 旧版榜首不变


# --------------------------------------------------------------------------
# 6. 传统手工补录：不带 corrected_at 维持 409
# --------------------------------------------------------------------------
def test_legacy_manual_create_still_conflicts(db):
    _seed_first_edition(db)
    with pytest.raises(AppError) as exc:
        quote_service.create(
            db,
            {"product_id": 1, "quote_date": D, "seven_day_pct": 2.99},
            source="manual",
        )
    assert exc.value.status_code == 409


def test_manual_create_with_corrected_at_joins_version_path(db):
    _seed_first_edition(db)
    quote = quote_service.create(
        db,
        {
            "product_id": 1,
            "quote_date": D,
            "seven_day_pct": 3.05,
            "corrected_at": datetime(2026, 11, 13, 22, 0),
            "source_version": "manual-v3",
        },
        source="manual",
    )
    assert quote.seven_day_pct == 3.05
    versions = quote_service.list_versions(db, product_id=1)
    assert versions["total"] == 2
    assert versions["items"][0]["is_effective"] is True
    assert versions["items"][0]["source_version"] == "manual-v3"


def test_version_history_endpoint_shape(db):
    _seed_first_edition(db)
    quote_service.ingest_versions(
        db,
        [_row(1, 3.00, corrected=datetime(2026, 11, 13, 21, 30), source_version="v2-p1", batch_key="batch-2")],
        source="import",
    )
    payload = quote_service.list_versions(db, product_id=1, quote_date=D.isoformat())
    assert payload["total"] == 2
    assert {item["status"] for item in payload["items"]} == {"active", "superseded"}
    assert payload["items"][0]["status_label"] == "当前有效"


def test_batch_without_effective_change_runs_full_and_keeps_board(db):
    first = _seed_first_edition(db)
    # 没有任何有效值变化的批次：退化为全量运行，发布指针正常切换
    run = yield_service.refresh_for_batch(
        db, batch_key="empty-batch", affected_product_ids=[], operator_email="scheduler@x.io"
    )
    assert run.scope == "full"
    assert run.is_published is True
    assert run.view_count == 8
    assert yield_service.latest_published_run(db).id == run.id
    assert run.supersedes_run_id == first.id


def test_full_manual_rerun_resets_changed_but_keeps_unchanged_review(db):
    first = _seed_first_edition(db)
    p02 = db.scalar(select(YieldView).where(YieldView.run_id == first.id, YieldView.product_id == 2))
    yield_service.review(db, p02, status="confirmed", note="人工确认", reviewer="analyst@x.io")

    # 先让 p01 发生变化并增量发布
    quote_service.ingest_versions(
        db,
        [_row(1, 3.00, corrected=datetime(2026, 11, 13, 21, 30), source_version="v2-p1", batch_key="batch-2")],
        source="import",
    )
    yield_service.refresh_for_batch(db, batch_key="batch-2", affected_product_ids=[1], operator_email="s@x.io")

    # 再发起一次不带 batch_key 的全量换算
    full = yield_service.create_and_execute(db, trigger="manual", operator_email="admin@x.io")
    assert full.scope == "full" and full.is_published
    p02_view = db.scalar(select(YieldView).where(YieldView.run_id == full.id, YieldView.product_id == 2))
    # p02 的值在全量数据里没变，复核结论保留
    assert p02_view.comparable_pct == 2.20
    assert p02_view.status == "confirmed"
    p01_view = db.scalar(select(YieldView).where(YieldView.run_id == full.id, YieldView.product_id == 1))
    assert p01_view.comparable_pct == 3.00


def test_published_board_is_immutable_when_later_run_fails(db, monkeypatch):
    first = _seed_first_edition(db)
    good_published_id = yield_service.latest_published_run(db).id

    quote_service.ingest_versions(
        db,
        [_row(2, 4.00, corrected=datetime(2026, 11, 13, 22, 0), source_version="v2-p2", batch_key="batch-x")],
        source="import",
    )
    call_count = {"n": 0}
    real_compute = yield_service.compute_items

    def _flaky(db_session, product_ids=None):
        call_count["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(yield_service, "compute_items", _flaky)
    failed = yield_service.refresh_for_batch(
        db, batch_key="batch-x", affected_product_ids=[2], operator_email="s@x.io"
    )
    assert failed.status == "failed"
    # 正式榜仍是第一次发布的 run，且其视图数据完好
    assert yield_service.latest_published_run(db).id == good_published_id
    board = yield_service.groups(db)
    assert board["run_id"] == first.id
    monkeypatch.setattr(yield_service, "compute_items", real_compute)
    # 失败批次没有被标记为已发布，重试同批次应能重新执行
    retry = yield_service.refresh_for_batch(
        db, batch_key="batch-x", affected_product_ids=[2], operator_email="s@x.io"
    )
    assert retry.status != "failed"
    assert retry.is_published is True
