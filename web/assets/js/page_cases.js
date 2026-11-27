// 真实案例：复核分配、标签版本、证据时间窗、评测入口
import {
  api, bindActions, bindTopbar, confirmDialog, esc, fmtDate, icon, modal, mount, setBusy,
  state, toast,
} from './core.js';

const REVIEW_LABEL = { unassigned: '未分配', pending: '待复核', confirmed: '已复核', rejected: '已退回' };
const REVIEW_BADGE = { unassigned: '', pending: 'warn', confirmed: 'ok', rejected: 'danger' };
const CASE_BADGE = { unlabeled: '', labeled: 'info', golden: 'ok', deprecated: 'danger' };

function reviewBadge(s) {
  return `<span class="badge ${REVIEW_BADGE[s] || ''}">${esc(REVIEW_LABEL[s] || s || '未分配')}</span>`;
}

function caseBadge(s) {
  return `<span class="badge ${CASE_BADGE[s] || ''}">${esc(s || 'unlabeled')}</span>`;
}

function shortName(path) {
  return String(path || '').split(/[\\/]/).pop() || '—';
}

function basenameList(c) {
  return (c.input_paths || []).map(shortName).join('、') || '—';
}

function labelSummary(c) {
  const expected = (c.expected_roots || []).join('、') || '—';
  const excluded = (c.excluded_roots || []).join('、') || '—';
  return `<div class="kv">
    <div class="k">期望根因</div><div class="v">${esc(expected)}</div>
    <div class="k">显式排除</div><div class="v">${esc(excluded)}</div>
    <div class="k">原始输入</div><div class="v">${c.raw_inputs_available === false ? '<span class="danger-text">未随迁移包导出</span>' : '可用'}</div>
    <div class="k">证据时间窗</div><div class="v mono tiny">${esc(c.labels?.evidence_window?.start || '—')} ~ ${esc(c.labels?.evidence_window?.end || '—')}</div>
    <div class="k">恢复结果</div><div class="v">${esc(c.labels?.recovery_result || '—')}</div>
    <div class="k">复核备注</div><div class="v">${esc(c.labels?.decision_note || c.notes || '—')}</div>
  </div>`;
}

export async function renderCases() {
  const reviewer = state.user?.role === 'reviewer';
  const admin = state.user?.role === 'admin';
  const view = mount('真实案例', '未复核案例不参与准确率；标签修改保留版本历史',
    admin ? `<button class="btn" data-act="run-eval">${icon('activity')}运行评测</button>` : '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let data;
  try {
    data = await api('/api/cases?review_status=all');
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const items = data.items || [];
  const counts = data.review_status_counts || {};
  const filters = reviewer
    ? [['all', '全部'], ['pending', '待复核'], ['confirmed', '已复核'], ['rejected', '已退回']]
    : [['all', '全部'], ['unassigned', '未分配'], ['pending', '待复核'], ['confirmed', '已复核']];
  const activeFilter = state._caseFilter || 'all';
  const filtered = activeFilter === 'all' ? items : items.filter((c) => c.review_status === activeFilter);
  view.innerHTML = `
    <div class="grid grid-4" style="margin-bottom:14px">
      <div class="card stat"><div><div class="stat-val">${items.length}</div><div class="stat-label">可见案例</div>
        <div class="stat-note">真实/复核来源</div></div></div>
      <div class="card stat"><div><div class="stat-val">${counts.pending || 0}</div><div class="stat-label">待复核</div>
        <div class="stat-note">分配后进入个人队列</div></div></div>
      <div class="card stat"><div><div class="stat-val">${counts.confirmed || 0}</div><div class="stat-label">已复核</div>
        <div class="stat-note">可作为 golden 候选</div></div></div>
      <div class="card stat"><div><div class="stat-val">${items.filter((c) => c.status === 'golden').length}</div><div class="stat-label">golden</div>
        <div class="stat-note">需专家确认并切分 test</div></div></div>
    </div>
    <div class="card">
      <div class="card-head"><h2>案例队列</h2>
        <div class="tabs" style="margin-left:auto">
          ${filters.map(([id, label]) => `<button class="tab ${activeFilter === id ? 'active' : ''}" data-act="filter" data-filter="${id}">${label}</button>`).join('')}
        </div>
      </div>
      <div class="card-body tight table-wrap">
        ${filtered.length ? `<table class="data responsive"><thead><tr>
          <th>案例</th><th>现场</th><th>标签</th><th>复核</th><th>复核人</th><th>更新时间</th></tr></thead><tbody>
          ${filtered.map((c) => `<tr class="row-hover" data-href="#/cases/${encodeURIComponent(c.id)}">
            <td data-label="案例"><div style="font-weight:600">${esc(c.title)}</div>
              <div class="mono muted tiny">${esc(c.id)} · ${esc(c.name)}</div>
              <div class="tiny muted" style="margin-top:4px">${esc(basenameList(c))}${c.raw_inputs_available === false ? ' <span class="danger-text">· 原始输入未迁移</span>' : ''}</div></td>
            <td data-label="现场">${esc(c.site || '—')}<div class="tiny muted">${esc(c.device_type || '')} ${esc(c.software_version || '')}</div></td>
            <td data-label="标签">${caseBadge(c.status)}<div class="tiny muted" style="margin-top:3px">${esc(c.split || 'unlabeled')}</div></td>
            <td data-label="复核">${reviewBadge(c.review_status)}</td>
            <td data-label="复核人">${esc(c.reviewer_name || '—')}</td>
            <td data-label="更新时间" class="tiny">${esc(fmtDate(c.updated_at))}</td>
          </tr>`).join('')}</tbody></table>`
        : `<div class="empty">${icon('inbox')}<h3>当前筛选下没有案例</h3>
          <p>真实案例由管理员导入；专家只能看到分配给自己的案例。未标注案例不会进入准确率统计。</p></div>`}
      </div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>数据纪律：</strong>没有现场根因的案例保持 unlabeled；复核人必须填写期望/排除根因和证据时间窗后才能确认。
      golden 只能来自已复核真实案例，test/hidden 不参与规则调参。门禁未达标前只报告合成回归和真实样本覆盖，不宣称现网准确率。
    </div></div>`;

  const handlers = {
    filter: (el) => { state._caseFilter = el.dataset.filter; renderCases(); },
    'run-eval': () => openEvalModal(),
  };
  bindActions(view, handlers);
  bindTopbar(handlers);
  view.querySelectorAll('tr[data-href]').forEach((tr) => {
    tr.addEventListener('click', () => { location.hash = tr.dataset.href; });
  });
  return view;
}

export async function renderCaseDetail(caseId) {
  const view = mount('案例详情', `案例 ${caseId} · 标签版本与复核记录`, '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let data;
  try {
    data = await api(`/api/cases/${encodeURIComponent(caseId)}`);
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const c = data.case;
  const admin = state.user?.role === 'admin';
  const assigned = c.reviewer_id && String(c.reviewer_id) === String(state.user?.id);
  const canLabel = admin || assigned || c.created_by === state.user?.username;
  const canConfirm = admin || assigned;
  let history = [];
  try { history = (await api(`/api/cases/${encodeURIComponent(caseId)}/history`)).items || []; } catch { history = []; }
  const lw = c.labels?.evidence_window || {};
  view.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>${esc(c.title)}</h2>
        <div class="inline" style="margin-left:auto">${caseBadge(c.status)} ${reviewBadge(c.review_status)}
          <a class="btn btn-sm" href="#/cases">返回列表</a></div>
      </div>
      <div class="card-body">
        <div class="kv">
          <div class="k">案例 ID</div><div class="v mono">${esc(c.id)}</div>
          <div class="k">名称</div><div class="v">${esc(c.name)}</div>
          <div class="k">现场 / 设备</div><div class="v">${esc(c.site || '—')} · ${esc(c.device_type || '—')} · ${esc(c.software_version || '—')}</div>
          <div class="k">来源 / 切分</div><div class="v">${esc(c.origin)} · ${esc(c.split)}</div>
          <div class="k">复核人</div><div class="v">${esc(c.reviewer_name || '未分配')} ${admin ? `<button class="btn btn-sm" data-act="assign" style="margin-left:8px">分配/调整</button>` : ''}</div>
          <div class="k">输入文件</div><div class="v tiny mono" style="word-break:break-all">${esc(basenameList(c))}</div>
          <div class="k">数据指纹</div><div class="v mono tiny">${esc(c.dataset_sha256 || '—')}</div>
        </div>
        <div class="alert info" style="margin-top:12px">${icon('info')}<div>${esc(c.description || '该案例没有现场故障描述，需复核人根据日志与现场记录补充根因。')}</div></div>
      </div>
    </div>

    <div class="grid grid-2" style="margin-top:14px;grid-template-columns:minmax(0,1.25fr) minmax(0,.85fr)">
      <div class="card">
        <div class="card-head"><h2>根因标注</h2><span class="sub">确认后写入版本历史</span></div>
        <div class="card-body">
          <form id="case-label-form">
            <div class="form-row"><label>期望根因（逗号分隔）</label>
              <input class="input" name="expected_roots" value="${esc((c.expected_roots || []).join(', '))}" placeholder="多个根因用逗号分隔" ${canLabel ? '' : 'disabled'}></div>
            <div class="form-row"><label>显式排除根因</label>
              <input class="input" name="excluded_roots" value="${esc((c.excluded_roots || []).join(', '))}" placeholder="用于阴性样本，证明已排除" ${canLabel ? '' : 'disabled'}></div>
            <div class="form-row"><label>证据时间窗</label>
              <div class="inline"><input class="input" type="datetime-local" name="window_start" value="${esc(lw.start || '')}" ${canLabel ? '' : 'disabled'}>
              <input class="input" type="datetime-local" name="window_end" value="${esc(lw.end || '')}" ${canLabel ? '' : 'disabled'}></div>
              <div class="tiny muted">标注产生判断的证据区间；无时间窗时填日志覆盖时段。</div></div>
            <div class="form-row"><label>恢复结果</label>
              <select class="input" name="recovery_result" ${canLabel ? '' : 'disabled'}>
                ${['', '已恢复', '未恢复', '间歇恢复', '未验证'].map((x) => `<option value="${x}" ${x === (c.labels?.recovery_result || '') ? 'selected' : ''}>${x || '未填写'}</option>`).join('')}
              </select></div>
            <div class="form-row"><label>案例状态</label>
              <select class="input" name="status" ${canLabel ? '' : 'disabled'}>
                ${['unlabeled', 'labeled', 'golden', 'deprecated'].map((x) => `<option value="${x}" ${x === c.status ? 'selected' : ''}>${x}</option>`).join('')}
              </select></div>
            <div class="form-row"><label>数据集切分</label>
              <select class="input" name="split" ${canLabel ? '' : 'disabled'}>
                ${['unlabeled', 'dev', 'test', 'hidden', 'train'].map((x) => `<option value="${x}" ${x === c.split ? 'selected' : ''}>${x}</option>`).join('')}
              </select></div>
            <div class="form-row"><label>复核备注</label>
              <textarea class="input" name="decision_note" rows="3" placeholder="现场根因、证据位置、双故障说明或拒判理由" ${canLabel ? '' : 'disabled'}>${esc(c.labels?.decision_note || '')}</textarea></div>
            <div class="inline" style="margin-top:12px">
              <button class="btn btn-primary" type="submit" data-act="save-label" ${canLabel ? '' : 'disabled'}>保存标签</button>
              <button class="btn" type="button" data-act="approve" ${canConfirm ? '' : 'disabled'}>确认复核</button>
              <button class="btn btn-danger" type="button" data-act="reject" ${canConfirm ? '' : 'disabled'}>退回/拒判</button>
            </div>
            <div class="tiny muted" style="margin-top:8px">规则：未提供期望或排除根因不能标记 labeled/golden；golden 必须切到 dev/test/hidden；每次保存都写入历史。</div>
          </form>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>标签历史</h2><span class="sub">${history.length} 次变更</span></div>
        <div class="card-body" style="max-height:520px;overflow:auto">
          ${history.length ? `<div class="stack">${history.map((h) => `<div class="case-history-item">
            <div class="between"><strong>${esc(h.actor_name || 'system')} · ${esc(h.action)}</strong>
              <span class="tiny muted">${esc(fmtDate(h.created_at))}</span></div>
            <div class="tiny muted" style="margin-top:5px">${esc(h.note || '无备注')}</div>
            <div class="mono tiny muted" style="margin-top:4px">status ${esc(h.new?.status || '—')} / ${esc(h.new?.split || '—')} · review ${esc(h.new?.review_status || '—')}</div>
          </div>`).join('')}</div>` : `<div class="small muted">尚无标签变更。首次标注会生成第一条版本记录。</div>`}
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:14px"><div class="card-head"><h2>当前标签快照</h2></div>
      <div class="card-body">${labelSummary(c)}</div></div>`;

  const form = view.querySelector('#case-label-form');
  const payload = (extra = {}) => {
    const f = new FormData(form);
    const expected = String(f.get('expected_roots') || '').split(/[,，]/).map((x) => x.trim()).filter(Boolean);
    const excluded = String(f.get('excluded_roots') || '').split(/[,，]/).map((x) => x.trim()).filter(Boolean);
    const window = {
      start: String(f.get('window_start') || ''), end: String(f.get('window_end') || ''),
    };
    return {
      expected_roots: expected, excluded_roots: excluded,
      status: String(f.get('status') || 'unlabeled'), split: String(f.get('split') || 'unlabeled'),
      labels: { evidence_window: window, recovery_result: String(f.get('recovery_result') || ''),
                decision_note: String(f.get('decision_note') || '') },
      decision_note: String(f.get('decision_note') || ''),
      ...extra,
    };
  };
  const save = async (button, extra) => {
    setBusy(button, true, '保存中…');
    try {
      await api(`/api/cases/${encodeURIComponent(c.id)}/label`, { method: 'POST', body: payload(extra) });
      toast('标签已保存并写入历史', 'ok');
      renderCaseDetail(c.id);
    } catch (err) { toast(err.message, 'error', 6000); }
    finally { setBusy(button, false); }
  };
  view.querySelector('[data-act="save-label"]').addEventListener('click', (e) => {
    e.preventDefault(); save(e.currentTarget, { action: 'label' });
  });
  view.querySelector('[data-act="approve"]').addEventListener('click', async (e) => {
    e.preventDefault();
    if (!(await confirmDialog('确认该案例的根因与证据时间窗？确认后 review_status 变为已复核。', { title: '确认复核' }))) return;
    save(e.currentTarget, { action: 'approve' });
  });
  view.querySelector('[data-act="reject"]').addEventListener('click', (e) => {
    e.preventDefault(); save(e.currentTarget, { action: 'reject' });
  });
  view.querySelectorAll('[data-act="assign"]').forEach((btn) => {
    btn.addEventListener('click', () => openAssignModal(c));
  });
  return view;
}

function openAssignModal(c) {
  const m = modal({
    title: '分配复核专家',
    body: `<div class="form-row"><label>复核人</label><select class="input" id="assign-reviewer"><option value="0">不分配</option></select>
      <div class="tiny muted">只能分配给 active 状态的 reviewer 或 admin；分配后站内通知本人。</div></div>
      <div class="form-row"><label>备注</label><input class="input" id="assign-note" placeholder="可选"></div>`,
    footer: `<button class="btn" data-act="close">取消</button><button class="btn btn-primary" data-act="submit">确认分配</button>`,
  });
  const select = m.body.querySelector('#assign-reviewer');
  api('/api/users?page_size=100').then((data) => {
    (data.items || []).filter((u) => ['reviewer', 'admin'].includes(u.role) && u.status === 'active')
      .forEach((u) => {
        const opt = document.createElement('option');
        opt.value = u.id; opt.textContent = `${u.display_name || u.username}（${u.username} / ${u.role}）`;
        if (c.reviewer_id && String(c.reviewer_id) === String(u.id)) opt.selected = true;
        select.appendChild(opt);
      });
  }).catch((err) => toast(err.message, 'error'));
  bindActions(m.mask, {
    close: () => m.close(),
    submit: async (el) => {
      setBusy(el, true, '分配中…');
      try {
        await api(`/api/cases/${encodeURIComponent(c.id)}/assign-reviewer`, {
          method: 'POST',
          body: { reviewer_id: Number(select.value), note: m.body.querySelector('#assign-note').value },
        });
        toast('复核人已更新', 'ok'); m.close(); renderCaseDetail(c.id);
      } catch (err) { toast(err.message, 'error'); }
      finally { setBusy(el, false); }
    },
  });
}

function openEvalModal() {
  const m = modal({
    title: '运行真实案例评测',
    body: `<div class="alert info">${icon('info')}<div>只评测 labeled/golden 案例；确定性模式关闭 LLM 与自学习，用于版本可复现对比。样本不足时不宣称现网准确率。</div></div>
      <div class="form-row"><label>数据集切分</label><select class="input" id="eval-split">
        <option value="test">test（独立测试集）</option><option value="dev">dev（调参观察）</option><option value="hidden">hidden（隐藏集）</option></select></div>
      <div class="form-row"><label>最多案例数</label><input class="input" id="eval-limit" type="number" min="0" value="0"><div class="tiny muted">0 表示该切分全部案例。</div></div>
      <div id="eval-result"></div>`,
    footer: `<button class="btn" data-act="close">关闭</button><button class="btn btn-primary" data-act="run">开始评测</button>`,
  });
  bindActions(m.mask, {
    close: () => m.close(),
    run: async (el) => {
      setBusy(el, true, '评测中…');
      try {
        const data = await api('/api/evaluations/run', { method: 'POST', body: {
          split: m.body.querySelector('#eval-split').value,
          limit: Number(m.body.querySelector('#eval-limit').value || 0),
        } });
        const s = data.summary || {};
        m.body.querySelector('#eval-result').innerHTML = `<div class="alert ok" style="margin-top:12px">${icon('check')}
          <div>评测 ${esc(data.id)} 完成：${s.cases} 例，完全命中 ${((s.exact_rate || 0) * 100).toFixed(1)}%，
          误报率 ${s.false_positive_rate == null ? '—' : ((s.false_positive_rate || 0) * 100).toFixed(1) + '%'}，
          拒判率 ${((s.abstain_rate || 0) * 100).toFixed(1)}%。报告：<span class="mono tiny">${esc(data.report || '')}</span></div></div>`;
        toast('评测完成', 'ok');
      } catch (err) {
        m.body.querySelector('#eval-result').innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
      } finally { setBusy(el, false); }
    },
  });
}
