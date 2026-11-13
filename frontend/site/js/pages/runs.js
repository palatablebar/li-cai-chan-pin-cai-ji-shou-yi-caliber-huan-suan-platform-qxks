import { api } from '../api.js';
import { ENUMS, can, runTone } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, fmtNum, fmtPct, fmtTime, kv, table, toast } from '../ui.js';

function runDuration(run) {
  if (!run.started_at || !run.finished_at) return '—';
  const ms = new Date(run.finished_at) - new Date(run.started_at);
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function diffRows(diff) {
  if (!diff) return '<p class="muted">该批次没有发布差异数据。</p>';
  const updateRows = (diff.updates || [])
    .map(
      (row) => `<tr>
        <td>${esc(row.product_name || row.product_code)}</td>
        <td class="num">${row.before.comparable_pct === null ? '缺失' : fmtPct(row.before.comparable_pct, 4)}</td>
        <td class="num">${row.after.comparable_pct === null ? '缺失' : fmtPct(row.after.comparable_pct, 4)}</td>
        <td>${esc(ENUMS.method[row.before.method] || row.before.method || '—')} → ${esc(
          ENUMS.method[row.after.method] || row.after.method,
        )}</td>
        <td class="num">${row.before.peer_rank ?? '—'} → ${row.after.peer_rank ?? '—'}</td>
      </tr>`,
    )
    .join('');
  const rankRows = (diff.rank_changes || [])
    .map(
      (row) => `<tr>
        <td>${esc(row.product_name || `#${row.product_id}`)}</td>
        <td>${esc(row.group_key)}</td>
        <td class="num">${row.rank_before ?? '未排名'} → ${row.rank_after ?? '未排名'}</td>
        <td class="num">${row.comparable_before === null ? '缺失' : fmtPct(row.comparable_before, 4)} → ${
          row.comparable_after === null ? '缺失' : fmtPct(row.comparable_after, 4)
        }</td>
      </tr>`,
    )
    .join('');
  const groupRows = (diff.group_diff || [])
    .map((group) => {
      const before = group.top_before ? `#${group.top_before.product_id} ${fmtPct(group.top_before.comparable_pct, 4)}` : '—';
      const after = group.top_after
        ? `${esc(group.top_after.product_name || `#${group.top_after.product_id}`)} ${fmtPct(group.top_after.comparable_pct, 4)}`
        : '—';
      return `<tr><td>${esc(group.group_label)}</td><td class="num">${group.size_before}</td><td class="num">${group.size_after}</td><td>${before}</td><td>${after}</td></tr>`;
    })
    .join('');
  return `
    <p class="note-line" style="margin-top:0">
      上一版 ${esc(diff.previous_run_no || '—')}：重算 <b>${diff.recomputed_products}</b> 只 · 沿用 <b>${diff.carried_products}</b> 只 ·
      有效值变化 <b>${diff.changed_products}</b> 条 · 名次变化 <b>${diff.rank_changed_products}</b> 条
    </p>
    <h3 style="margin:14px 0 8px;font-size:1rem;color:var(--navy)">本批更新（发布前后）</h3>
    ${
      updateRows
        ? `<div class="table-wrap"><table class="data"><thead><tr><th>产品</th><th>发布前可比</th><th>发布后可比</th><th>口径</th><th>名次</th></tr></thead><tbody>${updateRows}</tbody></table></div>`
        : '<p class="muted">本批没有有效值发生变化。</p>'
    }
    <h3 style="margin:14px 0 8px;font-size:1rem;color:var(--navy)">名次变化</h3>
    ${
      rankRows
        ? `<div class="table-wrap"><table class="data"><thead><tr><th>产品</th><th>同类组</th><th>名次</th><th>可比年化</th></tr></thead><tbody>${rankRows}</tbody></table></div>`
        : '<p class="muted">同类名次没有变化。</p>'
    }
    <h3 style="margin:14px 0 8px;font-size:1rem;color:var(--navy)">分组发布前后差异</h3>
    ${
      groupRows
        ? `<div class="table-wrap"><table class="data"><thead><tr><th>同类组</th><th>发布前样本</th><th>发布后样本</th><th>发布前榜首</th><th>发布后榜首</th></tr></thead><tbody>${groupRows}</tbody></table></div>`
        : '<p class="muted">无分组差异。</p>'
    }`;
}

function openRun(run) {
  const metrics = run.metrics || {};
  const diff = metrics.publish_diff;
  const { root } = drawer({
    title: `换算批次 ${run.run_no}`,
    subtitle: `${run.trigger_label} · ${run.operator_email}`,
    body: `${kv([
      ['状态', `<span class="badge ${runTone(run.status)}">${esc(run.status_label)}</span>`],
      [
        '发布',
        run.is_published
          ? `<span class="badge ok">已发布 · ${fmtTime(run.published_at)}</span>`
          : '<span class="badge pending warn">未发布（正式榜保留上一版）</span>',
      ],
      ['换算范围', esc(ENUMS.runScope[run.scope] || run.scope || '全量')],
      ['采集批次', esc(run.batch_key || '—')],
      ['产品数', `${run.product_count}`],
      ['结果数', `${run.view_count}`],
      ['本批变化', `${run.changed_products ?? 0} 条 · 名次变化 ${run.rank_changed_products ?? 0} 条`],
      ['开始', fmtTime(run.started_at)],
      ['结束', fmtTime(run.finished_at)],
      ['耗时', runDuration(run)],
      ['平均可比', fmtPct(metrics.avg_comparable_pct, 4)],
    ])}
    <h3 style="margin:14px 0 8px;font-size:1rem;color:var(--navy)">发布前后差异</h3>
    ${diffRows(diff)}
    <h3 style="margin:14px 0 8px;font-size:1rem;color:var(--navy)">运行指标</h3>
    <pre class="code">${esc(JSON.stringify(metrics, null, 2))}</pre>
    <h3 style="margin:14px 0 8px;font-size:1rem;color:var(--navy)">运行日志</h3>
    <pre class="code">${esc(run.log_text || '（无日志）')}</pre>`,
    footer: `<button class="btn accent" data-views>查看这批对比结果</button>`,
  });
  root.querySelector('[data-views]')?.addEventListener('click', () => {
    location.hash = '#/compare';
  });
}

function openImport(ctx, products, rerender) {
  const today = new Date().toISOString().slice(0, 10);
  const { root, close } = drawer({
    title: '导入机构报价（支持晚间补发更正）',
    subtitle: '同一产品 + 报价日按更正时间选有效值；被替换版本保留可查，同批次重试不新增',
    body: `<form id="quote-form" style="display:grid;gap:12px">
      ${field({
        label: '产品',
        name: 'product_id',
        options: products.map((product) => ({ value: product.id, label: `${product.name}（${product.code}）` })),
      })}
      ${field({ label: '报价日', name: 'quote_date', type: 'date', value: today })}
      ${field({ label: '单位净值', name: 'nav', type: 'number', placeholder: '1.0523，可留空' })}
      ${field({ label: '七日年化（%）', name: 'seven_day_pct', type: 'number', placeholder: '2.31，可留空' })}
      ${field({ label: '万份收益', name: 'wanfen_yield', type: 'number', placeholder: '0.62，可留空' })}
      ${field({ label: '业绩基准下限（%）', name: 'bench_low', type: 'number', placeholder: '2.2' })}
      ${field({ label: '业绩基准上限（%）', name: 'bench_high', type: 'number', placeholder: '3.4' })}
      <hr style="border:none;border-top:1px solid var(--line);margin:4px 0" />
      ${field({
        label: '更正时间（填了即按版本化处理）',
        name: 'corrected_at',
        type: 'datetime-local',
        placeholder: '2026-11-13T21:30',
      })}
      ${field({ label: '来源版本号（留空按更正时间生成）', name: 'source_version', placeholder: 'org-night-v2' })}
      ${field({ label: '采集批次号（同批次重试幂等）', name: 'batch_key', placeholder: 'night-batch-20261113' })}
      <div class="form-error" id="quote-error"></div>
      <button class="btn accent" type="submit">写入报价</button>
    </form>`,
  });
  root.querySelector('#quote-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const numeric = (name) => {
      const raw = form[name].value.trim();
      return raw === '' ? null : Number(raw);
    };
    const correctedAt = form.corrected_at.value ? `${form.corrected_at.value}:00` : null;
    try {
      const payload = {
        product_id: Number(form.product_id.value),
        quote_date: form.quote_date.value,
        nav: numeric('nav'),
        seven_day_pct: numeric('seven_day_pct'),
        wanfen_yield: numeric('wanfen_yield'),
        bench_low: numeric('bench_low'),
        bench_high: numeric('bench_high'),
      };
      if (correctedAt) {
        payload.corrected_at = correctedAt;
        payload.source_version = form.source_version.value.trim() || null;
        payload.batch_key = form.batch_key.value.trim() || null;
      }
      const data = await api.post('/api/quotes', payload);
      toast(
        correctedAt
          ? `报价版本已写入（当前有效版本 #${data.active_version_id ?? ''}），可发起批次换算`
          : '报价已写入，可再发起一次换算',
      );
      close();
      rerender();
    } catch (err) {
      root.querySelector('#quote-error').textContent =
        err.code === 'CONFLICT' ? '这只产品当天已有报价（409，不覆盖）；补发请填写更正时间走版本化' : errorMessage(err);
    }
  });
}

function openBatchRun(ctx, rerender) {
  const { root, close } = drawer({
    title: '按采集批次发起增量换算',
    subtitle: '只重算该批次受影响产品所在同类组；同批次号重试不新增运行',
    body: `<form id="batch-form" style="display:grid;gap:12px">
      ${field({ label: '采集批次号', name: 'batch_key', placeholder: 'night-batch-20261113' })}
      <div class="form-error" id="batch-error"></div>
      <button class="btn accent" type="submit">发起增量换算并发布</button>
    </form>`,
  });
  root.querySelector('#batch-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const batchKey = event.target.batch_key.value.trim();
    if (!batchKey) {
      root.querySelector('#batch-error').textContent = '请填写采集批次号';
      return;
    }
    try {
      const run = await api.post('/api/yield-runs', { trigger: 'manual', batch_key: batchKey });
      toast(`换算完成（${ENUMS.runScope[run.scope] || run.scope}）：${run.run_no}，变化 ${run.changed_products} 条，已发布`);
      close();
      rerender();
    } catch (err) {
      root.querySelector('#batch-error').textContent = errorMessage(err);
    }
  });
}

export const runsPage = {
  async render(view, ctx) {
    let runs;
    let products;
    let quotes;
    try {
      [runs, products, quotes] = await Promise.all([
        api.get('/api/yield-runs', { limit: 20 }),
        api.get('/api/products', { limit: 200 }),
        api.get('/api/quotes', { limit: 8 }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => runsPage.render(view, ctx));
      return;
    }
    const rerender = () => runsPage.render(view, ctx);
    const canRun = can(ctx.user.role, 'yield:run');
    const canImport = can(ctx.user.role, 'quote:import');
    const canExport = can(ctx.user.role, 'export:read');

    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>口径换算批次</h2>
          <div class="toolbar">
            <span class="meta">${runs.total} 次</span>
            ${canRun ? '<button class="btn sm accent" id="new-run">全量换算</button>' : ''}
            ${canRun ? '<button class="btn sm ghost" id="batch-run">批次增量换算</button>' : ''}
            ${
              canExport
                ? `<button class="btn sm ghost" data-export="views">对比榜 CSV</button>
                   <button class="btn sm ghost" data-export="quotes">报价流水 CSV</button>
                   <button class="btn sm ghost" data-export="runs">批次 CSV</button>`
                : ''
            }
          </div>
        </div>
        ${table({
          rows: runs.items,
          columns: [
            { key: 'run_no', label: '运行编号', className: 'mono' },
            {
              key: 'status',
              label: '状态',
              render: (row) => `<span class="badge ${runTone(row.status)}">${esc(row.status_label)}</span>`,
            },
            {
              key: 'is_published',
              label: '发布',
              render: (row) =>
                row.is_published
                  ? `<span class="badge ok">已发布</span>`
                  : '<span class="badge pending warn">未发布</span>',
            },
            { key: 'scope', label: '范围', render: (row) => esc(ENUMS.runScope[row.scope] || row.scope || '全量') },
            { key: 'trigger_label', label: '触发' },
            { key: 'product_count', label: '产品', className: 'num' },
            { key: 'view_count', label: '结果', className: 'num' },
            {
              key: 'changed',
              label: '本批变化',
              className: 'num',
              render: (row) => `${row.changed_products ?? 0} / ${row.rank_changed_products ?? 0}`,
            },
            {
              key: 'avg_comparable',
              label: '平均可比',
              className: 'num',
              render: (row) => fmtPct((row.metrics || {}).avg_comparable_pct, 4),
            },
            {
              key: 'unknown',
              label: '缺失',
              className: 'num',
              render: (row) => `${(row.metrics || {}).unknown ?? 0} 条`,
            },
            { key: 'started_at', label: '开始时间', render: (row) => fmtTime(row.started_at) },
            { key: 'duration', label: '耗时', className: 'num', render: (row) => runDuration(row) },
          ],
          empty: { title: '还没有换算批次', hint: '口径专员可以发起第一次换算。' },
        })}
      </section>

      <div class="grid-2">
        <section class="panel">
          <div class="panel-head">
            <h2>报价导入</h2>
            <span class="meta">quotes · 版本化</span>
          </div>
          <div class="panel-body">
            ${
              canImport
                ? `<p class="note-line" style="margin-top:0">机构晚间补发同日收益时，填写「更正时间」导入：系统按产品+报价日+更正时间选有效值，旧版本标「已替换」保留；同一来源版本重复投递自动去重。</p>
                   <button class="btn accent" id="import-quote">补录/补发一条报价</button>`
                : '<p class="muted">客户经理角色只能查看报价，不能导入。</p>'
            }
            <h3 style="margin:16px 0 8px;font-size:1rem;color:var(--navy)">最近有效报价</h3>
            ${table({
              rows: quotes.items,
              columns: [
                { key: 'quote_date', label: '报价日', className: 'mono' },
                { key: 'product_name', label: '产品' },
                {
                  key: 'seven_day_pct',
                  label: '七日年化',
                  className: 'num',
                  render: (row) => (row.seven_day_pct === null ? '—' : `${fmtNum(row.seven_day_pct, 2)}%`),
                },
                {
                  key: 'wanfen_yield',
                  label: '万份收益',
                  className: 'num',
                  render: (row) => (row.wanfen_yield === null ? '—' : fmtNum(row.wanfen_yield, 4)),
                },
                { key: 'source_label', label: '来源' },
                {
                  key: 'corrected_at',
                  label: '更正时间',
                  render: (row) => (row.corrected_at ? fmtTime(row.corrected_at) : '<span class="muted">—</span>'),
                },
              ],
              empty: { title: '还没有报价', hint: '补录或等待每日采集任务。' },
            })}
          </div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>口径与发布说明</h2>
            <span class="meta">notes</span>
          </div>
          <div class="panel-body">
            <pre class="code">版本化报价（机构补发同日收益）
  1. 每个来源版本都落 quote_versions，不覆盖旧值
  2. 按 产品 + 报价日 + 更正时间 选当前有效值（active）
  3. 旧版本标 superseded 保留，换算结果回链依据版本
  4. 同 source_version / 同采集批次重试 → 去重不新增

增量换算与正式榜
  · 只重算受影响产品所在同类组，组外沿用原复核结论
  · 运行先暂存，成功后一次性切换发布指针
  · 运行失败不发布，客户侧正式榜保留上一版
  · 批次抽屉列出本批更新、名次变化、发布前后差异

算法 A｜direct &gt; wanfen &gt; nav &gt; bench &gt; unknown（缺失不猜）
算法 B｜类型+期限档分组，样本 &lt;5 不排名</pre>
          </div>
        </section>
      </div>`;

    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      const run = runs.items.find((item) => String(item.id) === row.dataset.id);
      if (run) row.addEventListener('click', () => openRun(run));
    });
    view.querySelector('#new-run')?.addEventListener('click', async () => {
      try {
        const run = await api.post('/api/yield-runs', { trigger: 'manual' });
        toast(`换算完成：${run.run_no}，${run.view_count} 条结果，已发布`);
        rerender();
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelector('#batch-run')?.addEventListener('click', () => openBatchRun(ctx, rerender));
    view.querySelector('#import-quote')?.addEventListener('click', () => openImport(ctx, products.items, rerender));
    view.querySelectorAll('[data-export]').forEach((button) => {
      button.addEventListener('click', async () => {
        const kind = button.dataset.export;
        try {
          await api.download(`/api/exports/${kind}.csv`, `${kind}.csv`);
          toast('已开始下载');
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
  },
};

export default runsPage;
