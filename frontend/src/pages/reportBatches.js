import {get, post} from '../api.js';
import {Layout, Section, openModal, showToast} from '../components/ui.js';
import {renderReceipt, mountReceiptLookup} from '../components/batchReceipt.js';
import {batchBadge, escapeHtml, money, dateText} from '../utils/constants.js';

const canEdit = (user) => user.role === 'intake_agent' || user.role === 'risk_manager';

function emptyRow(index, values = {}) {
  const v = values;
  return `<div class="batch-item-row card" data-row="${index}">
    <div class="batch-row-head">
      <b>第 <span data-row-index>${index + 1}</span> 条</b>
      <button type="button" class="btn btn-danger btn-sm" data-remove-row>删除</button>
    </div>
    <div class="batch-row-grid">
      <div class="field"><label>报案号 *</label><input data-f="report_no" value="${escapeHtml(v.report_no || '')}" placeholder="RPT-20261124-01"></div>
      <div class="field"><label>修订号 *</label><input data-f="revision_no" type="number" min="1" value="${escapeHtml(v.revision_no ?? 1)}"></div>
      <div class="field"><label>事故号（可选）</label><input data-f="accident_no" value="${escapeHtml(v.accident_no || '')}" placeholder="同一保单同日区分事故"></div>
      <div class="field"><label>保单号 *</label><input data-f="policy_no" value="${escapeHtml(v.policy_no || '')}"></div>
      <div class="field"><label>客户姓名 *</label><input data-f="customer_name" value="${escapeHtml(v.customer_name || '')}"></div>
      <div class="field"><label>出险类型 *</label>
        <select data-f="incident_type">
          ${['车损', '盗抢', '医疗', '水灾', '火灾'].map((t) => `<option ${v.incident_type === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
      </div>
      <div class="field"><label>出险日期 *</label><input data-f="incident_date" type="date" value="${escapeHtml(v.incident_date || '2026-11-24')}"></div>
      <div class="field"><label>损失金额 *</label><input data-f="loss_amount" type="number" min="0" value="${escapeHtml(v.loss_amount ?? 50000)}"></div>
      <div class="field"><label>出险地区 *</label>
        <select data-f="region">
          ${['都市', '沿海', '山区', '园区'].map((t) => `<option ${v.region === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
      </div>
      <div class="field"><label>联系电话 *</label><input data-f="claimant_phone" value="${escapeHtml(v.claimant_phone || '13800000000')}"></div>
    </div>
  </div>`;
}

function collectItems(scope) {
  return [...scope.querySelectorAll('.batch-item-row')].map((row) => {
    const val = (name) => row.querySelector(`[data-f="${name}"]`)?.value?.trim();
    return {
      report_no: val('report_no'),
      revision_no: Number(val('revision_no')),
      accident_no: val('accident_no') || null,
      policy_no: val('policy_no'),
      customer_name: val('customer_name'),
      incident_type: val('incident_type'),
      incident_date: val('incident_date'),
      loss_amount: Number(val('loss_amount')),
      region: val('region'),
      claimant_phone: val('claimant_phone'),
    };
  });
}

function pendingTable(items, editable = true) {
  if (!items || !items.length) return '<div class="empty">暂无等待人工确认的修订</div>';
  const rows = items.map((p) => {
    const proposed = p.proposed_payload || {};
    const diffs = [
      ['客户', p.customer_name, proposed.customer_name],
      ['估损', money(p.loss_amount), money(proposed.loss_amount)],
      ['电话', p.claimant_phone, proposed.claimant_phone],
      ['出险日期', dateText(p.incident_date), dateText(proposed.incident_date)],
    ].filter(([, oldV, newV]) => String(oldV ?? '') !== String(newV ?? ''));
    const diffHtml = diffs.map(([label, oldV, newV]) =>
      `<div class="diff-line"><span>${label}</span><span class="diff-old">${escapeHtml(oldV)}</span> → <span class="diff-new">${escapeHtml(newV)}</span></div>`).join('');
    return `<div class="pending-card" data-pending-row="${p.revision_id}">
      <div class="split">
        <div><b>${escapeHtml(p.claim_no)}</b> · 报案号 ${escapeHtml(p.report_no)}
          <div class="subline">第 ${p.revision_no} 版修订 · 批次 ${escapeHtml(p.batch_no || '-')} · ${escapeHtml((p.proposed_at || '').replace('T', ' ').slice(0, 16))}</div>
        </div>
        <span class="badge badge-amber">待人工确认</span>
      </div>
      <div class="diff-box">${diffHtml || '<span class="muted">内容字段有更新</span>'}</div>
      ${editable ? `<div class="pending-actions">
        <button class="btn btn-primary btn-sm" data-confirm="${p.revision_id}">采纳并更新案件</button>
        <button class="btn btn-line btn-sm" data-reject="${p.revision_id}">退回该修订</button>
      </div>` : '<div class="subline" style="margin-top:8px">仅受理员或风险主管可确认该修订</div>'}
    </div>`;
  }).join('');
  return `<div class="pending-list">${rows}</div>`;
}

function recentTable(items, navigate) {
  if (!items || !items.length) return '<div class="empty">还没有报送过批次</div>';
  const rows = items.map((b) => {
    const badge = batchBadge(b.status);
    return `<tr>
      <td><button class="link-btn" data-recent-batch="${escapeHtml(b.batch_no)}">${escapeHtml(b.batch_no)}</button></td>
      <td>${escapeHtml(b.partner_code || '-')}</td>
      <td><span class="badge ${badge.cls}">${escapeHtml(badge.label)}</span></td>
      <td>${b.total_count}</td>
      <td class="muted">立 ${b.created_count} · 更新 ${b.updated_count} · 重复 ${b.duplicate_count} · 旧版 ${b.stale_count} · 待确认 ${b.pending_count}</td>
      <td class="muted">${escapeHtml((b.created_at || '').replace('T', ' ').slice(0, 16))}</td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="table">
    <thead><tr><th>批次号</th><th>合作方</th><th>状态</th><th>条数</th><th>逐条结果汇总</th><th>时间</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

export async function renderReportBatches(user, navigate) {
  const [pendingData, batchesData] = await Promise.all([
    get('/report-revisions/pending').catch(() => ({items: []})),
    get('/report-batches?limit=20').catch(() => ({items: []})),
  ]);
  const editable = canEdit(user);

  const editor = editable ? Section('按批报案', `
    <p class="hint">合作方按批报送，每条含<b>报案号</b>与<b>修订号</b>。整批校验通过才受理；同一保单同一天的不同事故，请使用不同报案号分别立案。同报案号同修订号内容不一致时整批退回。</p>
    <form id="batchForm">
      <div class="form-grid">
        <div class="field"><label>合作方批次号（重试幂等键，可留空自动生成）</label><input data-f="batch_no" placeholder="例如 B-20261124-0009"></div>
        <div class="field"><label>合作方编码</label><input data-f="partner_code" value="HUADONG-PARTNER"></div>
      </div>
      <div id="batchRows" style="margin-top:12px"></div>
      <div class="batch-form-actions">
        <button type="button" class="btn btn-soft" id="addRow">+ 添加一条</button>
        <button type="button" class="btn btn-line" id="fillExample">填入示例：同保单同日两起事故</button>
        <button type="submit" class="btn btn-primary" style="margin-left:auto">整批校验并报送</button>
      </div>
    </form>
    <div id="submitResult" style="margin-top:8px"></div>
  `) : '<div class="notice">当前角色仅可查看批次回执与确认队列。</div>';

  const content = `<div class="page-head"><div>
      <h1 class="page-title">合作方批次报案</h1>
      <p class="page-subtitle">整批校验、幂等重试、修订顺序与分级后人工确认，批次原回执和逐条处理结果均可回查。</p>
    </div></div>
    ${editor}
    <div class="grid" style="margin-top:16px">
      ${Section('待人工确认的新修订', `<div data-pending>${pendingTable(pendingData.items, editable)}</div>`)}
      ${Section('批次回执查询', `
        <div data-lookup-scope>
          <form data-batch-lookup-form class="batch-lookup">
            <input data-batch-lookup-input class="batch-input" placeholder="输入批次号查原回执与逐条结果">
            <button class="btn btn-primary" type="submit">查询</button>
          </form>
          <div data-batch-receipt-result style="margin-top:12px"></div>
        </div>`)}
      ${Section('最近批次', `<div data-recent>${recentTable(batchesData.items, navigate)}</div>`)}
    </div>`;

  const page = Layout({user, active: 'reportBatches', onNavigate: navigate, content});
  setTimeout(() => bindPage(user, navigate, editable), 0);
  return page;
}

function bindPage(user, navigate, editable) {
  const rowsWrap = document.querySelector('#batchRows');
  let seq = 0;
  const addRow = (v) => {
    seq += 1;
    rowsWrap.insertAdjacentHTML('beforeend', emptyRow(seq, v));
    const row = rowsWrap.lastElementChild;
    row.querySelector('[data-remove-row]').onclick = () => {
      row.remove();
      [...rowsWrap.querySelectorAll('[data-row-index]')].forEach((el, i) => { el.textContent = i + 1; });
    };
  };
  if (editable && rowsWrap) {
    addRow({});
    document.querySelector('#addRow').onclick = () => addRow({});
    document.querySelector('#fillExample').onclick = () => {
      rowsWrap.innerHTML = '';
      const d = new Date();
      const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      addRow({report_no: `RPT-DEMO-${Date.now().toString().slice(-6)}-1`, revision_no: 1, accident_no: 'ACC-A', policy_no: 'PA-DEMO-SAMEDAY', customer_name: '张三（追尾）', incident_date: iso, loss_amount: 12000, claimant_phone: '13800001111'});
      addRow({report_no: `RPT-DEMO-${Date.now().toString().slice(-6)}-2`, revision_no: 1, accident_no: 'ACC-B', policy_no: 'PA-DEMO-SAMEDAY', customer_name: '李四（变道剐蹭）', incident_date: iso, loss_amount: 8000, claimant_phone: '13800002222'});
      const batchInput = document.querySelector('#batchForm [data-f="batch_no"]');
      if (batchInput && !batchInput.value) batchInput.value = `B-DEMO-${Date.now().toString().slice(-8)}`;
    };

    document.querySelector('#batchForm').onsubmit = async (e) => {
      e.preventDefault();
      const body = {
        batch_no: document.querySelector('#batchForm [data-f="batch_no"]').value.trim() || null,
        partner_code: document.querySelector('#batchForm [data-f="partner_code"]').value.trim() || null,
        items: collectItems(document.querySelector('#batchForm')),
      };
      const out = document.querySelector('#submitResult');
      out.innerHTML = '<div class="empty">正在整批校验…</div>';
      try {
        const receipt = await post('/report-batches', body);
        out.innerHTML = renderReceipt(receipt, navigate);
        showToast(`批次 ${receipt.batch_no} 已受理`);
        refreshRecent(navigate);
        bindReceiptClaimLinks(out, navigate);
      } catch (err) {
        const receipt = err.data;
        if (receipt && receipt.items) {
          out.innerHTML = renderReceipt(receipt, navigate);
          bindReceiptClaimLinks(out, navigate);
        } else {
          out.innerHTML = `<div class="notice danger">${escapeHtml(err.message)}</div>`;
        }
        showToast('整批退回，请查看逐条原因');
      }
    };
  }

  const lookupScope = document.querySelector('[data-lookup-scope]');
  if (lookupScope) mountReceiptLookup(lookupScope, navigate);

  document.querySelector('[data-recent]')?.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-recent-batch]');
    if (!btn) return;
    const input = document.querySelector('[data-lookup-scope] [data-batch-lookup-input]');
    if (input) {
      input.value = btn.dataset.recentBatch;
      input.closest('form').requestSubmit();
      document.querySelector('[data-lookup-scope]')?.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
  });

  bindPending(user, navigate, editable);
}

function bindReceiptClaimLinks(scope, navigate) {
  scope.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-receipt-claim]');
    if (btn && navigate) navigate('detail', Number(btn.dataset.receiptClaim));
  });
}

async function refreshRecent(navigate) {
  try {
    const data = await get('/report-batches?limit=20');
    const box = document.querySelector('[data-recent]');
    if (box) box.innerHTML = recentTable(data.items, navigate);
  } catch { /* 列表刷新失败不影响回执 */ }
}

function bindPending(user, navigate, editable = true) {
  const root = document.querySelector('[data-pending]');
  if (!root) return;
  root.addEventListener('click', (e) => {
    const confirmBtn = e.target.closest('[data-confirm]');
    const rejectBtn = e.target.closest('[data-reject]');
    if (!editable) return;
    if (confirmBtn) openReviewModal(confirmBtn.dataset.confirm, true, navigate);
    if (rejectBtn) openReviewModal(rejectBtn.dataset.reject, false, navigate);
  });
}

function openReviewModal(revisionId, isConfirm, navigate) {
  const m = openModal(isConfirm ? '采纳新修订' : '退回新修订', `<form id="reviewForm">
    <p class="hint">${isConfirm
      ? '采纳后新修订内容会写入案件。案件已有分级结果，可要求清空旧分级并重新分级。'
      : '退回后案件继续保留当前资料与分级结果。'}</p>
    <div class="field"><label>确认意见（可选）</label>
      <textarea name="note" rows="3" placeholder="例如：已与合作方/客户电话核对"></textarea></div>
    ${isConfirm ? '<label class="check-item" style="margin:10px 0"><input type="checkbox" name="regrade"> 采纳后要求重新风险分级</label>' : ''}
    <button class="btn btn-primary" type="submit" style="width:100%">${isConfirm ? '确认采纳' : '确认退回'}</button>
  </form>`);
  m.querySelector('#reviewForm').onsubmit = async (e) => {
    e.preventDefault();
    const body = {
      note: m.querySelector('[name=note]').value.trim() || null,
      regrade: isConfirm && m.querySelector('[name=regrade]')?.checked,
    };
    try {
      await post(`/report-revisions/${revisionId}/${isConfirm ? 'confirm' : 'reject'}`, body);
      m.remove();
      showToast(isConfirm ? '修订已采纳' : '修订已退回');
      const data = await get('/report-revisions/pending');
      const box = document.querySelector('[data-pending]');
      if (box) {
        box.innerHTML = pendingTable(data.items);
      }
    } catch (err) {
      showToast(err.message);
    }
  };
}
