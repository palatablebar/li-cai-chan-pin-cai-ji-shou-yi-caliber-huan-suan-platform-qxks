"""收益口径换算服务：有效报价 → 算法 A 换算 → 算法 B 同类排名 → 版本化发布。

关键规则（对应机构晚间补发同日收益的场景）：
  1. 只从 quotes 表取数，该表每个产品+报价日只有一条当前有效值（由 quote_service 选出），
     换算结果记录报价版本快照（active_version_id），名次依据可追溯；
  2. 增量运行只重算「受影响产品所在同类组」：组外产品沿用上一版 YieldView（含人工复核结论），
     组内重算产品中已确认/已驳回的复核结论不被重开；
  3. 同一采集批次（batch_key）重试不新增运行，直接返回已有 run；
  4. 运行先落自己的 run + views（暂存），成功后一次性把发布指针切到本 run；
     失败不发布，正式榜保留上一版，客户侧看不到半成品。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CONFIDENCE_LABELS,
    PRODUCT_TYPE_LABELS,
    REVIEW_STATUS_LABELS,
    RISK_LEVEL_LABELS,
    RUN_STATUS_LABELS,
    TERM_BUCKET_LABELS,
    TRIGGER_LABELS,
    YIELD_METHOD_LABELS,
    ReviewStatus,
    RunStatus,
)
from src.models.product import Product
from src.models.quote import Quote
from src.models.yield_run import YieldRun
from src.models.yield_view import YieldView
from src.services import yield_math
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, local_now, utcnow


def next_run_no(db: Session) -> str:
    prefix = f"YR{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(YieldRun).where(YieldRun.run_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


# --------------------------------------------------------------------------
# 发布指针
# --------------------------------------------------------------------------
def latest_published_run(db: Session) -> YieldRun | None:
    return db.scalar(
        select(YieldRun).where(YieldRun.is_published.is_(True)).order_by(YieldRun.published_at.desc(), YieldRun.id.desc()).limit(1)
    )


def latest_run_any(db: Session) -> YieldRun | None:
    return db.scalar(select(YieldRun).order_by(YieldRun.id.desc()).limit(1))


def _resolve_run_id(db: Session, run_id: int | None) -> int | None:
    if run_id is not None:
        return run_id
    published = latest_published_run(db)
    return published.id if published else None


# --------------------------------------------------------------------------
# 计算：始终基于当前有效报价
# --------------------------------------------------------------------------
def compute_items(db: Session, product_ids: list[int] | None = None) -> list[dict]:
    """把指定产品（默认全部）+ 当前有效报价换算成可比年化并完成同类排名。"""
    product_stmt = select(Product).order_by(Product.id)
    if product_ids is not None:
        product_stmt = product_stmt.where(Product.id.in_(product_ids))
    products = db.scalars(product_stmt).all()
    quote_stmt = select(Quote).order_by(Quote.product_id, Quote.quote_date)
    if product_ids is not None:
        quote_stmt = quote_stmt.where(Quote.product_id.in_(product_ids))
    quotes = db.scalars(quote_stmt).all()
    by_product: dict[int, list[Quote]] = {}
    for quote in quotes:
        by_product.setdefault(quote.product_id, []).append(quote)

    items: list[dict] = []
    for product in products:
        rows = by_product.get(product.id, [])
        nav_series = [{"quote_date": row.quote_date, "nav": row.nav} for row in rows if row.nav is not None]
        latest = rows[-1] if rows else None
        quote_dict = (
            {
                "quote_date": latest.quote_date,
                "nav": latest.nav,
                "seven_day_pct": latest.seven_day_pct,
                "wanfen_yield": latest.wanfen_yield,
                "bench_low": latest.bench_low,
                "bench_high": latest.bench_high,
            }
            if latest
            else {}
        )
        normalized = yield_math.normalize_quote(quote_dict, nav_series)
        items.append(
            {
                "product_id": product.id,
                "product_name": product.name,
                "product_code": product.code,
                "product_type": product.type,
                "risk_level": product.risk_level,
                "term_days": product.term_days,
                "issuer": product.issuer,
                "group_key": yield_math.group_key(product.type, product.term_days),
                "quote_date": latest.quote_date if latest else None,
                "active_version_id": latest.active_version_id if latest else None,
                "comparable_pct": normalized["comparable_pct"],
                "method": normalized["method"],
                "confidence": normalized["confidence"],
                "caveat": normalized["caveat"],
                "source_value": normalized["source_value"],
                "vol_annual": yield_math.annualized_volatility(nav_series),
            }
        )
    return yield_math.rank_peers(items)


def _view_flags(item: dict) -> dict:
    return {
        "caveat": item["caveat"],
        "source_value": item.get("source_value"),
        "tags": list(item.get("flags") or []),
        "group_median_vol": item.get("group_median_vol"),
    }


def _prior_views(db: Session, run: YieldRun | None) -> dict[int, YieldView]:
    if run is None:
        return {}
    rows = db.scalars(select(YieldView).where(YieldView.run_id == run.id)).all()
    return {row.product_id: row for row in rows}


def _carry_view(new_run_id: int, previous: YieldView) -> YieldView:
    """未变化产品整行沿用上一版：算法字段与人工复核结论都不重开。"""
    return YieldView(
        run_id=new_run_id,
        product_id=previous.product_id,
        comparable_pct=previous.comparable_pct,
        method=previous.method,
        confidence=previous.confidence,
        peer_rank=previous.peer_rank,
        risk_adj=previous.risk_adj,
        group_key=previous.group_key,
        group_size=previous.group_size,
        vol_annual=previous.vol_annual,
        status=previous.status,
        flags=previous.flags,
        note=previous.note,
        reviewer=previous.reviewer,
        review_note=previous.review_note,
        quote_date_snapshot=previous.quote_date_snapshot,
        active_version_id=previous.active_version_id,
        carried_from_view_id=previous.id,
    )


def _recomputed_view(run_id: int, item: dict, previous: YieldView | None, *, reset_review: bool) -> YieldView:
    """受影响组内产品：刷新算法字段。

    reset_review=False（值未变化）时连人工复核结论一并沿用；
    值发生变化才回到待复核，已确认口径不会被静默重开成另一个数却仍挂「已确认」。
    """
    keep_review = previous is not None and not reset_review
    return YieldView(
        run_id=run_id,
        product_id=item["product_id"],
        comparable_pct=item["comparable_pct"],
        method=item["method"],
        confidence=item["confidence"],
        peer_rank=item["peer_rank"],
        risk_adj=item["risk_adj"],
        group_key=item["group_key"],
        group_size=item["group_size"],
        vol_annual=item["vol_annual"],
        status=previous.status if keep_review else ReviewStatus.PENDING.value,
        flags=_view_flags(item),
        note=previous.note if keep_review else None,
        reviewer=previous.reviewer if keep_review else None,
        review_note=previous.review_note if keep_review else None,
        quote_date_snapshot=item["quote_date"],
        active_version_id=item["active_version_id"],
        carried_from_view_id=None,
    )


def _diff_report(
    db: Session,
    previous_run: YieldRun | None,
    prior_views: dict[int, YieldView],
    recomputed: list[dict],
    carried: list[YieldView],
) -> dict:
    """发布前后差异：本批更新清单、名次变化、分组层面的发布前后对照。"""
    products = {product.id: product for product in db.scalars(select(Product)).all()}
    updates: list[dict] = []
    rank_changes: list[dict] = []
    for item in recomputed:
        previous = prior_views.get(item["product_id"])
        product = products.get(item["product_id"])
        before_pct = previous.comparable_pct if previous else None
        after_pct = item["comparable_pct"]
        before_rank = previous.peer_rank if previous else None
        after_rank = item["peer_rank"]
        changed = (
            previous is None
            or before_pct != after_pct
            or (previous.method != item["method"])
        )
        entry = {
            "product_id": item["product_id"],
            "product_code": product.code if product else None,
            "product_name": product.name if product else None,
            "group_key": item["group_key"],
            "before": {
                "comparable_pct": before_pct,
                "method": previous.method if previous else None,
                "peer_rank": before_rank,
                "active_version_id": previous.active_version_id if previous else None,
            },
            "after": {
                "comparable_pct": after_pct,
                "method": item["method"],
                "peer_rank": after_rank,
                "active_version_id": item["active_version_id"],
            },
            "changed": bool(changed),
        }
        if changed:
            updates.append(entry)
        if before_rank != after_rank:
            rank_changes.append(
                {
                    "product_id": item["product_id"],
                    "product_name": product.name if product else None,
                    "group_key": item["group_key"],
                    "rank_before": before_rank,
                    "rank_after": after_rank,
                    "comparable_before": before_pct,
                    "comparable_after": after_pct,
                }
            )

    affected_groups = sorted({item["group_key"] for item in recomputed})
    group_diff: list[dict] = []
    for key in affected_groups:
        group_type, _, bucket = key.partition(":")
        label = f"{PRODUCT_TYPE_LABELS.get(group_type, group_type)} · {TERM_BUCKET_LABELS.get(bucket, bucket)}"
        before_rows = sorted(
            (view for view in prior_views.values() if view.group_key == key and view.comparable_pct is not None),
            key=lambda view: (-float(view.comparable_pct), view.product_id),
        )
        after_rows = sorted(
            (item for item in recomputed if item["group_key"] == key and item["comparable_pct"] is not None),
            key=lambda item: (-float(item["comparable_pct"]), item["product_id"]),
        )
        group_diff.append(
            {
                "group_key": key,
                "group_label": label,
                "size_before": len(before_rows),
                "size_after": len(after_rows),
                "top_before": {
                    "product_id": before_rows[0].product_id,
                    "comparable_pct": before_rows[0].comparable_pct,
                }
                if before_rows
                else None,
                "top_after": {
                    "product_id": after_rows[0]["product_id"],
                    "product_name": products.get(after_rows[0]["product_id"]).name
                    if after_rows and products.get(after_rows[0]["product_id"])
                    else None,
                    "comparable_pct": after_rows[0]["comparable_pct"],
                }
                if after_rows
                else None,
            }
        )

    return {
        "previous_run_id": previous_run.id if previous_run else None,
        "previous_run_no": previous_run.run_no if previous_run else None,
        "recomputed_products": len(recomputed),
        "carried_products": len(carried),
        "changed_products": len(updates),
        "rank_changed_products": len(rank_changes),
        "updates": updates,
        "rank_changes": rank_changes,
        "group_diff": group_diff,
    }


def _log_text(metrics: dict, scope: str, diff: dict) -> str:
    counts = metrics.get("method_counts") or {}
    parts = [
        f"{YIELD_METHOD_LABELS.get(key, key)} {value}"
        for key, value in sorted(counts.items(), key=lambda pair: -pair[1])
    ]
    lines = [
        f"口径换算完成（{'增量：仅受影响同类组' if scope == 'incremental' else '全量'}）："
        f"重算 {diff['recomputed_products']} 只 / 沿用 {diff['carried_products']} 只 / 共 {metrics.get('views', 0)} 条结果",
        "方法分布：" + " · ".join(parts),
        f"同类分组 {len(metrics.get('groups') or [])} 个，可排名 {metrics.get('ranked_groups')} 个（样本≥{yield_math.PEER_MIN_SAMPLE}）",
        f"本批有效值变化 {diff['changed_products']} 条，名次变化 {diff['rank_changed_products']} 条",
        f"口径缺失 {metrics.get('unknown')} 条，标记 unknown 待人工补录，不做估算",
        "发布：一次性切换正式榜，失败保留上一版（本运行成功后发布指针已切到本批次）",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 运行编排
# --------------------------------------------------------------------------
def execute(
    db: Session,
    run: YieldRun,
    *,
    affected_product_ids: list[int] | None = None,
) -> YieldRun:
    run.status = RunStatus.RUNNING.value
    previous_run = latest_published_run(db)
    prior_views = _prior_views(db, previous_run)
    run.scope = "incremental" if (affected_product_ids and previous_run is not None) else "full"
    # 计算与暂存取 SAVEPOINT：失败只回滚本 run 的半成品，外层事务与上一版正式榜不受影响
    savepoint = db.begin_nested()
    try:
        incremental = run.scope == "incremental"

        if incremental:
            affected_products = db.scalars(select(Product).where(Product.id.in_(affected_product_ids))).all()
            affected_group_keys = {
                yield_math.group_key(product.type, product.term_days) for product in affected_products
            }
            target_products = db.scalars(
                select(Product).where(
                    Product.type.in_({key.partition(":")[0] for key in affected_group_keys})
                )
            ).all()
            recompute_ids = sorted(
                product.id
                for product in target_products
                if yield_math.group_key(product.type, product.term_days) in affected_group_keys
            )
            carry_ids = sorted(pid for pid in prior_views if pid not in set(recompute_ids))
        else:
            recompute_ids = None  # 全量
            carry_ids = []

        items = compute_items(db, recompute_ids)
        item_by_product = {item["product_id"]: item for item in items}

        new_views: list[YieldView] = []
        for product_id in carry_ids:
            new_views.append(_carry_view(run.id, prior_views[product_id]))
        for item in items:
            previous = prior_views.get(item["product_id"])
            value_changed = previous is None or (
                previous.comparable_pct != item["comparable_pct"] or previous.method != item["method"]
            )
            new_views.append(_recomputed_view(run.id, item, previous, reset_review=value_changed))

        # 暂存：先把本 run 的全部视图落库，发布前客户侧读取的仍是上一版正式榜
        db.add_all(new_views)
        db.flush()

        all_items = _items_with_carried(db, run, new_views, item_by_product)
        metrics = yield_math.summarize(all_items)
        metrics["products"] = len(all_items)
        metrics["scope"] = run.scope
        metrics["affected_products"] = sorted(affected_product_ids or [])
        diff = _diff_report(db, previous_run, prior_views, items, [v for v in new_views if v.carried_from_view_id])
        metrics["publish_diff"] = diff
        best_group = None
        for group in metrics["groups"]:
            if not group["ranked"]:
                continue
            if best_group is None or (group["top_comparable_pct"] or 0) > (best_group["top_comparable_pct"] or 0):
                best_group = group
        if best_group:
            group_type, _, bucket = best_group["group_key"].partition(":")
            label = f"{PRODUCT_TYPE_LABELS.get(group_type, group_type)} · {TERM_BUCKET_LABELS.get(bucket, bucket)}"
            metrics["top_group"] = {
                "group_key": best_group["group_key"],
                "group_label": label,
                "top_product_id": best_group["top_product_id"],
                "top_product_name": best_group["top_product_name"],
                "top_comparable_pct": best_group["top_comparable_pct"],
            }
        run.metrics = metrics
        run.product_count = len(all_items)
        run.view_count = len(new_views)
        run.supersedes_run_id = previous_run.id if previous_run else None
        run.status = RunStatus.PARTIAL.value if metrics.get("unknown") else RunStatus.SUCCESS.value
        run.log_text = _log_text(metrics, run.scope, diff)

        # 一次性发布：指针切换与视图写入在同一事务内，任何异常都不触碰发布指针
        _publish(db, run)
    except Exception as exc:  # noqa: BLE001 - 运行失败留痕，不抛 500，正式榜保留上一版
        savepoint.rollback()  # 只回滚本 run 的暂存视图，run 行本身仍由外层事务保留
        run.status = RunStatus.FAILED.value
        run.is_published = False
        run.published_at = None
        run.view_count = 0
        run.product_count = 0
        run.log_text = f"运行失败，正式榜保留上一版：{exc}"
        run.metrics = {"error": str(exc)[:300], "affected_products": sorted(affected_product_ids or [])}
    run.finished_at = utcnow()
    db.flush()
    return run


def _items_with_carried(db: Session, run: YieldRun, new_views: list[YieldView], item_by_product: dict) -> list[dict]:
    """汇总指标时把沿用视图转回 item 形状，保证 summarize 覆盖全量产品。"""
    products = {product.id: product for product in db.scalars(select(Product)).all()}
    combined: list[dict] = []
    for view in new_views:
        if view.product_id in item_by_product:
            combined.append(item_by_product[view.product_id])
            continue
        product = products.get(view.product_id)
        combined.append(
            {
                "product_id": view.product_id,
                "product_name": product.name if product else None,
                "product_type": product.type if product else "",
                "term_days": product.term_days if product else None,
                "risk_level": product.risk_level if product else None,
                "comparable_pct": view.comparable_pct,
                "method": view.method,
                "vol_annual": view.vol_annual,
                "group_key": view.group_key,
                "peer_rank": view.peer_rank,
                "group_size": view.group_size,
                "flags": list((view.flags or {}).get("tags") or []),
            }
        )
    return combined


def _publish(db: Session, run: YieldRun) -> None:
    run.is_published = True
    run.published_at = utcnow()


def create_and_execute(
    db: Session,
    *,
    trigger: str,
    operator_email: str,
    seed_key: str | None = None,
    batch_key: str | None = None,
    affected_product_ids: list[int] | None = None,
) -> YieldRun:
    if seed_key:
        existing = db.scalar(select(YieldRun).where(YieldRun.seed_key == seed_key))
        if existing is not None:
            return existing
    # 同一采集批次重试：已成功生成的运行直接复用；失败运行不阻塞同批次重试
    if batch_key:
        existing = db.scalar(
            select(YieldRun).where(
                YieldRun.batch_key == batch_key,
                YieldRun.status != RunStatus.FAILED.value,
            )
        )
        if existing is not None:
            return existing
    run = YieldRun(
        run_no=next_run_no(db),
        trigger=trigger,
        status=RunStatus.RUNNING.value,
        operator_email=operator_email,
        seed_key=seed_key,
        batch_key=batch_key,
    )
    db.add(run)
    db.flush()
    return execute(db, run, affected_product_ids=affected_product_ids)


def refresh_for_batch(db: Session, *, batch_key: str, affected_product_ids: list[int], operator_email: str) -> YieldRun:
    """采集批次落库后的增量换算入口：同批次重试幂等，无受影响产品则不重开正式榜。"""
    if not affected_product_ids:
        return create_and_execute(
            db,
            trigger="schedule",
            operator_email=operator_email,
            batch_key=batch_key,
            affected_product_ids=[],
        )
    return create_and_execute(
        db,
        trigger="schedule",
        operator_email=operator_email,
        batch_key=batch_key,
        affected_product_ids=sorted(set(affected_product_ids)),
    )


def serialize_run(run: YieldRun) -> dict:
    metrics = run.metrics or {}
    diff = metrics.get("publish_diff") or {}
    return {
        "id": run.id,
        "run_no": run.run_no,
        "trigger": run.trigger,
        "trigger_label": TRIGGER_LABELS.get(run.trigger, run.trigger),
        "status": run.status,
        "status_label": RUN_STATUS_LABELS.get(run.status, run.status),
        "operator_email": run.operator_email,
        "product_count": run.product_count,
        "view_count": run.view_count,
        "scope": run.scope,
        "is_published": run.is_published,
        "published_at": iso_utc(run.published_at),
        "supersedes_run_id": run.supersedes_run_id,
        "batch_key": run.batch_key,
        "metrics": metrics,
        "changed_products": diff.get("changed_products", 0),
        "rank_changed_products": diff.get("rank_changed_products", 0),
        "log_text": run.log_text,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
    }


def list_runs(db: Session, *, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(YieldRun)
    count_stmt = select(func.count()).select_from(YieldRun)
    if status:
        stmt = stmt.where(YieldRun.status == status)
        count_stmt = count_stmt.where(YieldRun.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(YieldRun.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [serialize_run(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_run(db: Session, run_id: int) -> YieldRun:
    run = db.get(YieldRun, run_id)
    if run is None:
        raise AppError("NOT_FOUND", 404, {"entity": "yield_run", "id": run_id})
    return run


def serialize_view(view: YieldView, *, product: Product | None = None, run_no: str | None = None, hide_fee: bool = False) -> dict:
    group_type, _, bucket = (view.group_key or "").partition(":")
    flags = view.flags or {}
    return {
        "id": view.id,
        "run_id": view.run_id,
        "run_no": run_no,
        "product_id": view.product_id,
        "product_code": product.code if product else None,
        "product_name": product.name if product else None,
        "issuer": product.issuer if product else None,
        "type": product.type if product else None,
        "type_label": PRODUCT_TYPE_LABELS.get(product.type, product.type) if product else None,
        "risk_level": product.risk_level if product else None,
        "risk_label": RISK_LEVEL_LABELS.get(product.risk_level, product.risk_level) if product else None,
        "term_days": product.term_days if product else None,
        "term_bucket_label": TERM_BUCKET_LABELS.get(bucket, bucket) if bucket else None,
        "group_key": view.group_key,
        "group_label": f"{PRODUCT_TYPE_LABELS.get(group_type, group_type)} · {TERM_BUCKET_LABELS.get(bucket, bucket)}"
        if bucket
        else view.group_key,
        "group_size": view.group_size,
        "comparables": view.comparable_pct,
        "comparable_pct": view.comparable_pct,
        "method": view.method,
        "method_label": YIELD_METHOD_LABELS.get(view.method, view.method),
        "confidence": view.confidence,
        "confidence_label": CONFIDENCE_LABELS.get(view.confidence, view.confidence),
        "peer_rank": view.peer_rank,
        "risk_adj": view.risk_adj,
        "vol_annual": view.vol_annual,
        "fee_rate": None if hide_fee else (round(float(product.fee_rate or 0), 4) if product else None),
        "status": view.status,
        "status_label": REVIEW_STATUS_LABELS.get(view.status, view.status),
        "caveat": flags.get("caveat"),
        "tags": flags.get("tags") or [],
        "source_value": flags.get("source_value"),
        "reviewer": view.reviewer,
        "review_note": view.review_note,
        "quote_date_snapshot": view.quote_date_snapshot.isoformat() if view.quote_date_snapshot else None,
        "active_version_id": view.active_version_id,
        "carried_from_view_id": view.carried_from_view_id,
        "is_carried": view.carried_from_view_id is not None,
        "created_at": iso_utc(view.created_at),
        "updated_at": iso_utc(view.updated_at),
    }


def _product_map(db: Session) -> dict[int, Product]:
    return {product.id: product for product in db.scalars(select(Product)).all()}


def _run_no_map(db: Session) -> dict[int, str]:
    return {run.id: run.run_no for run in db.scalars(select(YieldRun)).all()}


def list_views(
    db: Session,
    *,
    run_id: int | None = None,
    method: str | None = None,
    status: str | None = None,
    risk_level: str | None = None,
    group: str | None = None,
    q: str | None = None,
    ranked_only: bool = False,
    limit: int = 20,
    offset: int = 0,
    hide_fee: bool = False,
) -> dict:
    products = _product_map(db)
    run_nos = _run_no_map(db)
    target_run_id = _resolve_run_id(db, run_id)
    stmt = select(YieldView)
    count_stmt = select(func.count()).select_from(YieldView)
    conditions = []
    if target_run_id:
        conditions.append(YieldView.run_id == target_run_id)
    if method:
        conditions.append(YieldView.method == method)
    if status:
        conditions.append(YieldView.status == status)
    if group:
        conditions.append(YieldView.group_key == group)
    if ranked_only:
        conditions.append(YieldView.peer_rank.is_not(None))
    if risk_level or q:
        matching = [product.id for product in products.values() if _match_product(product, risk_level, q)]
        if not matching:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        conditions.append(YieldView.product_id.in_(matching))
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(YieldView.run_id.desc(), YieldView.peer_rank.is_(None), YieldView.peer_rank, YieldView.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "items": [
            serialize_view(
                row,
                product=products.get(row.product_id),
                run_no=run_nos.get(row.run_id),
                hide_fee=hide_fee,
            )
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "run_id": target_run_id,
    }


def _match_product(product: Product, risk_level: str | None, q: str | None) -> bool:
    if risk_level and product.risk_level != risk_level:
        return False
    if q:
        needle = q.strip().lower()
        haystack = f"{product.name} {product.code} {product.issuer}".lower()
        if needle and needle not in haystack:
            return False
    return True


def get_view(db: Session, view_id: int) -> dict:
    view = db.get(YieldView, view_id)
    if view is None:
        raise AppError("NOT_FOUND", 404, {"entity": "yield_view", "id": view_id})
    return serialize_view(
        view,
        product=db.get(Product, view.product_id),
        run_no=db.get(YieldRun, view.run_id).run_no if db.get(YieldRun, view.run_id) else None,
    )


def review(db: Session, view: YieldView, *, status: str, note: str | None, reviewer: str) -> YieldView:
    view.status = status
    view.review_note = note
    view.reviewer = reviewer
    view.updated_at = utcnow()
    db.flush()
    return view


def groups(db: Session, run_id: int | None = None, top_n: int = 10, hide_fee: bool = False) -> dict:
    """对比榜：默认读正式发布的最新一版；显式传 run_id 可回看历史批次。"""
    latest = _resolve_run_id(db, run_id)
    if latest is None:
        return {"run_id": None, "published_run_id": None, "items": []}
    published = latest_published_run(db)
    products = _product_map(db)
    run_nos = _run_no_map(db)
    rows = db.scalars(
        select(YieldView).where(YieldView.run_id == latest).order_by(YieldView.comparable_pct.is_(None), YieldView.comparable_pct.desc())
    ).all()
    buckets: dict[str, list[YieldView]] = {}
    for row in rows:
        buckets.setdefault(row.group_key, []).append(row)
    type_order = {"cash": 0, "fixed": 1, "mixed": 2, "equity": 3}
    bucket_order = {"le30": 0, "d31_180": 1, "d181_365": 2, "gt365": 3, "unknown": 4}

    def _order(key: str) -> tuple[int, int, str]:
        group_type, _, bucket = key.partition(":")
        return (type_order.get(group_type, 9), bucket_order.get(bucket, 9), key)

    items = []
    for key, members in sorted(buckets.items(), key=lambda pair: _order(pair[0])):
        valid = [row for row in members if row.comparable_pct is not None]
        valid.sort(key=lambda row: (-float(row.comparable_pct), row.product_id))
        group_type, _, bucket = key.partition(":")
        items.append(
            {
                "group_key": key,
                "group_label": f"{PRODUCT_TYPE_LABELS.get(group_type, group_type)} · {TERM_BUCKET_LABELS.get(bucket, bucket)}",
                "size": len(valid),
                "ranked": len(valid) >= yield_math.PEER_MIN_SAMPLE,
                "top": [
                    serialize_view(
                        row,
                        product=products.get(row.product_id),
                        run_no=run_nos.get(row.run_id),
                        hide_fee=hide_fee,
                    )
                    for row in valid[:top_n]
                ],
            }
        )
    return {
        "run_id": latest,
        "published_run_id": published.id if published else None,
        "is_published": published.id == latest if published else False,
        "items": items,
    }


def publish_diff(db: Session, run_id: int | None = None) -> dict:
    """发布前后差异报告：本批更新、名次变化、分组发布前后对照。"""
    target_id = _resolve_run_id(db, run_id)
    if target_id is None:
        return {"run_id": None, "publish_diff": None}
    run = get_run(db, target_id)
    return {"run_id": run.id, "run_no": run.run_no, "publish_diff": (run.metrics or {}).get("publish_diff")}


def refresh_daily(db: Session, operator_email: str) -> YieldRun:
    """worker 定时任务入口：按当前有效报价全量重算一次口径（兜底，仍受发布机制保护）。"""
    return create_and_execute(db, trigger="schedule", operator_email=operator_email)
