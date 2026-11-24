import {get} from '../api.js';
import {batchBadge, escapeHtml} from '../utils/constants.js';

// 逐条处理结果表格。claim_id 存在时渲染成可跳转的案件链接。
export function renderBatchItemsTable(items, navigate) {
  const rows = (items || []).map((it) => {
    const badge = batchBadge(it.status);
    const claim = it.claim_id
      ? `<button class="link-btn" data-receipt-claim="${it.claim_id}">${escapeHtml(it.claim_no || ('#' + it.claim_id))}</button>`
      : (it.claim_no ? escapeHtml(it.claim_no) : '<span class="muted">-</span>');
    const errors = (it.errors || []).length
      ? `<div class="subline" style="color:#c0392b">${it.errors.map(escapeHtml).join('；')}</div>`
      : '';
    return `<tr>
      <td>${it.item_index + 1}</td>
      <td><b>${escapeHtml(it.report_no || '-')}</b><div class="subline">修订号 ${it.revision_no ?? '-'}</div></td>
      <td><span class="badge ${badge.cls}">${escapeHtml(it.status_label || badge.label)}</span></td>
      <td style="max-width:340px">${escapeHtml(it.message || '')}${errors}</td>
      <td>${claim}</td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="table">
    <thead><tr><th>#</th><th>报案号 / 修订</th><th>处理结果</th><th>说明</th><th>案件</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="empty">暂无明细</td></tr>'}</tbody>
  </table></div>`;
}

export function renderReceipt(receipt, navigate) {
  if (!receipt) return '';
  const head = batchBadge(receipt.status);
  const counts = receipt.counts || {};
  const countChips = Object.entries(counts)
    .filter(([, v]) => v > 0)
    .map(([k, v]) => {
      const b = batchBadge(k);
      return `<span class="badge ${b.cls}">${escapeHtml(b.label)} ${v}</span>`;
    }).join(' ');
  const replay = receipt.replayed ? '<span class="badge badge-blue">重复请求 · 返回原回执</span>' : '';
  const reason = receipt.reject_reason
    ? `<div class="notice danger" style="margin:12px 0">${escapeHtml(receipt.reject_reason)}</div>`
    : '';
  const raw = escapeHtml(JSON.stringify(receipt.original_receipt || receipt.receipt || receipt, null, 2));
  return `<div class="receipt-card">
    <div class="split" style="align-items:center;gap:10px;flex-wrap:wrap">
      <div><b style="font-size:15px">批次 ${escapeHtml(receipt.batch_no)}</b>
        <div class="subline">合作方 ${escapeHtml(receipt.partner_code || '-')} · ${escapeHtml((receipt.created_at || '').replace('T', ' ').slice(0, 19))}</div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <span class="badge ${head.cls}">${escapeHtml(head.label)}</span>${replay}${countChips}
      </div>
    </div>
    ${reason}
    <div style="margin-top:12px">${renderBatchItemsTable(receipt.items, navigate)}</div>
    <details style="margin-top:10px">
      <summary class="muted" style="cursor:pointer">查看原回执 JSON</summary>
      <pre class="receipt-json">${raw}</pre>
    </details>
  </div>`;
}

// 在任意容器内挂载“按批次号查原回执”。
// root: 父节点；navigate: 路由函数；opts.initialBatchNo 自动查询；opts.batchChips: [{batch_no,status}]
export function mountReceiptLookup(root, navigate, opts = {}) {
  const scope = root || document;
  const form = scope.querySelector('[data-batch-lookup-form]');
  const input = scope.querySelector('[data-batch-lookup-input]');
  const result = scope.querySelector('[data-batch-receipt-result]');
  if (!form || !result) return;

  async function load(batchNo) {
    const no = String(batchNo || input.value || '').trim();
    if (!no) { result.innerHTML = '<div class="notice">请输入批次号</div>'; return; }
    if (input) input.value = no;
    result.innerHTML = '<div class="empty">回执加载中…</div>';
    try {
      const receipt = await get(`/report-batches/${encodeURIComponent(no)}`);
      result.innerHTML = renderReceipt(receipt, navigate);
    } catch (err) {
      result.innerHTML = `<div class="notice danger">${escapeHtml(err.message || '批次回执不存在')}</div>`;
    }
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); load(); });
  scope.querySelectorAll('[data-batch-chip]').forEach((chip) => {
    chip.addEventListener('click', () => load(chip.dataset.batchChip));
  });
  result.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-receipt-claim]');
    if (btn && navigate) navigate('detail', Number(btn.dataset.receiptClaim));
  });

  if (opts.initialBatchNo) load(opts.initialBatchNo);
  return {load};
}

// 案件详情页的“批次报案回执”整段。
export function renderDetailBatchSection(detail) {
  const c = detail.claim || {};
  const batches = detail.report_batches || [];
  const chips = batches.map((b) => {
    const badge = batchBadge(b.status);
    return `<button type="button" class="batch-chip" data-batch-chip="${escapeHtml(b.batch_no)}">
      ${escapeHtml(b.batch_no)} <span class="badge ${badge.cls}">${escapeHtml(badge.label)}</span>
    </button>`;
  }).join('');
  const report = detail.partner_report;
  const reportLine = report
    ? `<div class="kv" style="margin-bottom:12px">
        <div class="kv-item"><small>合作方报案号</small><b>${escapeHtml(report.report_no)}</b></div>
        <div class="kv-item"><small>当前修订号</small><b>第 ${report.current_revision} 版</b></div>
        <div class="kv-item"><small>报案批次</small><b>${escapeHtml(c.report_batch_no || batches[0]?.batch_no || '-')}</b></div>
        <div class="kv-item"><small>待确认修订</small><b>${detail.pending_revision_count || 0} 条</b></div>
      </div>`
    : '<div class="subline" style="margin-bottom:10px">本案由工作台直接登记，未关联合作方报案号；仍可按批次号查询回执。</div>';
  return `<section class="card section" id="batchReceiptSection">
    <div class="section-head"><div class="section-title">批次报案回执</div>
      <span class="muted">按批次查原回执与逐条处理结果</span>
    </div>
    ${reportLine}
    <form data-batch-lookup-form class="batch-lookup">
      <input data-batch-lookup-input class="batch-input" placeholder="输入合作方批次号，如 B-261124-0001"
             value="${escapeHtml(batches[0]?.batch_no || '')}">
      <button class="btn btn-primary" type="submit">查询回执</button>
    </form>
    ${chips ? `<div class="batch-chips"><span class="muted">本案相关批次：</span>${chips}</div>` : ''}
    <div data-batch-receipt-result style="margin-top:14px"></div>
  </section>`;
}
