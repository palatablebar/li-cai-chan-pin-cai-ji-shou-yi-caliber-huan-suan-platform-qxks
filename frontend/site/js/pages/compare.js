import { api } from '../api.js';
import { ENUMS, can, methodTone, reviewTone, riskTone, runTone } from '../config.js';
import { drawer, emptyState, errorBox, errorMessage, esc, field, fmtPct, fmtNum, kv, rankBars, toast } from '../ui.js';

const state = { runId: '', topN: 10 };

function openView(ctx, viewRow, rerender) {
  api
    .get(`/api/yield-views/${viewRow.id}`)
    .then((item) => {
      const canReview = can(ctx.user.role, 'yield:review');
      const { root, close } = drawer({
        title: item.product_name || item.product_code,
        subtitle: `${item.group_label} · ${item.run_no}`,
        body: `${kv([
          [
            '可比年化',
            `<b style="font-family:var(--font-display);font-size:1.2rem;color:var(--navy)">${fmtPct(item.comparable_pct, 4)}</b>`,
          ],
          ['换算口径', `<span class="badge ${methodTone(item.method)}">${esc(item.method_label)}</span>`],
          ['置信度', `${esc(item.confidence_label)}`],
          ['同类排名', item.peer_rank ? `第 ${item.peer_rank} / ${item.group_size}` : `未排名（样本 ${item.group_size} < 5）`],
          ['年化波动', item.vol_annual === null ? '<span class="muted">净值点不足</span>' : `${fmtNum(item.vol_annual, 4)}%`],
          ['风险调整', item.risk_adj === null ? '<span class="muted">不可计算</span>' : fmtNum(item.risk_adj, 3)],
          ['风险等级', `<span class="badge ${riskTone(item.risk_level)}">${esc(item.risk_label)}</span>`],
          ['期限', `${item.term_days} 天 · ${esc(item.term_bucket_label || '')}`],
          ['销售费率', item.fee_rate === null ? '<span class="muted">当前角色不展示</span>' : `${fmtNum(item.fee_rate, 2)}%`],
          ['复核状态', `<span class="badge ${reviewTone(item.status)}">${esc(item.status_label)}</span>`],
          ['复核人', esc(item.reviewer || '—')],
          ['复核备注', esc(item.review_note || '—')],
          [
            '结论来源',
            item.is_carried
              ? `<span class="badge info">沿用上一版复核结论（视图 #${item.carried_from_view_id}）</span>`
              : '本批重新换算',
          ],
          [
            '报价依据',
            item.active_version_id
              ? `<span class="mono">报价日 ${esc(item.quote_date_snapshot || '—')} · 版本 #${item.active_version_id}</span>`
              : '<span class="muted">—</span>',
          ],
        ])}
        <h3 style="margin:14px 0 8px;font-size:1rem;color:var(--navy)">口径说明</h3>
        <pre class="code">${esc(item.caveat || '（无）')}</pre>
        <p class="note-line">可比年化由后端按当前有效报价版本算出并落库；前端不重算、不改写，依据版本可在报价版本历史中追溯。</p>`,
        footer: canReview
          ? `<form id="review-form" style="display:grid;gap:10px">
              ${field({
                label: '复核结论',
                name: 'status',
                value: item.status,
                options: Object.entries(ENUMS.review).map(([value, label]) => ({ value, label })),
              })}
              ${field({ label: '复核备注', name: 'note', value: item.review_note || '', placeholder: '写一句判定依据' })}
              <button class="btn accent" type="submit">提交复核</button>
            </form>`
          : '<span class="mono muted">客户经理角色：可查看榜单，不能复核口径</span>',
      });
      root.querySelector('#review-form')?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.target;
        try {
          await api.patch(`/api/yield-views/${item.id}/review`, {
            status: form.status.value,
            note: form.note.value.trim() || null,
          });
          toast('复核结论已保存');
          close();
          rerender();
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    })
    .catch((err) => toast(errorMessage(err), 'err'));
}

function diffBanner(run, groups) {
  const diff = run && run.metrics ? run.metrics.publish_diff : null;
  if (!diff) return '';
  return `<div class="panel" style="margin-bottom:14px">
    <div class="panel-head"><h2>本批发布差异 · ${esc(run.run_no)}</h2>
      <span class="meta">${groups.is_published ? '正式榜已切换到本批次' : '本批次未发布，正式榜仍为上一版'}</span></div>
    <div class="panel-body">
      <p class="note-line" style="margin-top:0">
        重算 <b>${diff.recomputed_products}</b> 只 · 沿用原结论 <b>${diff.carried_products}</b> 只 ·
        有效值变化 <b>${diff.changed_products}</b> 条 · 名次变化 <b>${diff.rank_changed_products}</b> 条
        （上一版：${esc(diff.previous_run_no || '—')}）
      </p>
      ${
        diff.rank_changes && diff.rank_changes.length
          ? `<div class="table-wrap"><table class="data"><thead><tr><th>产品</th><th>同类组</th><th>名次变化</th><th>可比年化</th></tr></thead><tbody>
              ${diff.rank_changes
                .map(
                  (row) => `<tr><td>${esc(row.product_name || `#${row.product_id}`)}</td><td>${esc(row.group_key)}</td>
                  <td class="num">${row.rank_before ?? '未排名'} → ${row.rank_after ?? '未排名'}</td>
                  <td class="num">${row.comparable_before === null ? '缺失' : fmtPct(row.comparable_before, 4)} → ${
                    row.comparable_after === null ? '缺失' : fmtPct(row.comparable_after, 4)
                  }</td></tr>`,
                )
                .join('')}
            </tbody></table></div>`
          : '<p class="muted">本批没有同类名次变化。</p>'
      }
    </div>
  </div>`;
}

export const comparePage = {
  async render(view, ctx) {
    let runs;
    let groups;
    try {
      runs = await api.get('/api/yield-runs', { limit: 20 });
      groups = await api.get('/api/yield-views/groups', {
        run_id: state.runId || undefined,
        top_n: state.topN,
      });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => comparePage.render(view, ctx));
      return;
    }
    const currentRun = runs.items.find((run) => String(run.id) === String(groups.run_id)) || runs.items[0];
    const rerender = () => comparePage.render(view, ctx);

    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>同类对比榜</h2>
          <div class="toolbar">
            <span class="meta">${groups.items.length} 个分组 · top ${state.topN}${
              groups.is_published ? ' · 正式发布版' : ' · 历史批次（非正式榜）'
            }</span>
            ${can(ctx.user.role, 'export:read') ? '<button class="btn sm ghost" id="export-views">导出 CSV</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          <div class="filter-bar" id="compare-filters">
            ${field({
              label: '运行批次',
              name: 'run_id',
              value: groups.run_id || '',
              options: runs.items.map((run) => ({
                value: run.id,
                label: `${run.run_no} · ${run.is_published ? '已发布' : run.status_label} · ${run.view_count} 条`,
              })),
            })}
            ${field({
              label: '每组展示',
              name: 'top_n',
              value: state.topN,
              options: [
                { value: 5, label: '前 5' },
                { value: 10, label: '前 10' },
              ],
            })}
          </div>
          <p class="note-line">
            分组 = 产品类型 + 期限档；样本 &lt;5 只不排名；风险调整 = 可比年化 ÷ 年化波动（近 30 日净值日收益标准差 × √252）。
            榜单默认展示最新正式发布版，切换批次可回看历史；失败运行不会发布，正式榜始终保留上一版。
            ${
              currentRun
                ? `当前批次 ${esc(currentRun.run_no)}：${
                    currentRun.metrics && currentRun.metrics.avg_comparable_pct
                      ? `平均可比 ${fmtPct(currentRun.metrics.avg_comparable_pct, 4)}`
                      : ''
                  } ${currentRun.metrics && currentRun.metrics.unknown ? `· 缺失 ${currentRun.metrics.unknown} 条` : ''}`
                : ''
            }
          </p>
        </div>
      </section>
      ${currentRun ? diffBanner(currentRun, groups) : ''}
      ${
        groups.items.length
          ? groups.items
              .map(
                (group) => `<section class="panel group-panel">
            <div class="panel-head">
              <h2>${esc(group.group_label)}</h2>
              <span class="meta">${group.ranked ? `样本 ${group.size} · 已排名` : `样本 ${group.size} · 不足 5 不排名`}</span>
            </div>
            <div class="panel-body">
              ${rankBars(group.top)}
              <p class="note-line">${
                group.ranked
                  ? '排名按可比年化降序，同值按产品编号升序。'
                  : '样本不足 5 只，仅展示数值不给出排名。'
              }</p>
            </div>
          </section>`,
              )
              .join('')
          : errorBox('还没有换算批次，先去「换算与导入」发起一次运行。')
      }`;

    const filters = view.querySelector('#compare-filters');
    filters?.addEventListener('change', () => {
      state.runId = filters.querySelector('[name=run_id]').value;
      state.topN = Number(filters.querySelector('[name=top_n]').value || 10);
      rerender();
    });
    view.querySelectorAll('.bar-row[data-view-id]').forEach((row) => {
      row.style.cursor = 'pointer';
      row.addEventListener('click', () => {
        let target = null;
        groups.items.forEach((group) => {
          group.top.forEach((item) => {
            if (String(item.id) === row.dataset.viewId) target = item;
          });
        });
        if (target) openView(ctx, target, rerender);
      });
    });
    view.querySelector('#export-views')?.addEventListener('click', async () => {
      try {
        await api.download(
          `/api/exports/views.csv${groups.run_id ? `?run_id=${groups.run_id}` : ''}`,
          'views.csv',
        );
        toast('已开始下载对比榜 CSV');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
  },
};

export default comparePage;
