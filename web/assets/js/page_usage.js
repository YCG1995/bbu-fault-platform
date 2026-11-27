// 用量统计：按日模型调用、任务 Token 与技能执行消耗（管理员可按用户筛选）
import { api, bindActions, esc, fmtDur, fmtSize, icon, mount, state } from './core.js';

const RANGE_OPTIONS = [7, 30, 90];
const MODEL_COLORS = ['#2f6fed', '#12a594', '#f59e0b', '#8b5cf6', '#ef6a5b', '#64748b', '#0ea5e9', '#84cc16'];
const USER_COLORS = ['#0f766e', '#2563eb', '#d97706', '#7c3aed', '#dc2626', '#64748b', '#0891b2', '#65a30d'];
const SCOPE_LABELS = { admin: '共享', personal: '个人' };
const TASK_STATUS = {
  running: ['info', '运行中'], queued: ['muted', '排队中'], done: ['ok', '已完成'],
  success: ['ok', '成功'], failed: ['danger', '失败'], needs_data: ['warn', '需补充数据'],
  cancelled: ['muted', '已取消'], interrupted: ['danger', '已中断'],
};

let usageDays = 30;
let usageUser = '';
let usageMetric = 'total_tokens';
let usageUserMetric = 'total_tokens';
let usagePage = 1;
let usageDirectory = [];
let usageLoading = false;

function num(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n.toLocaleString('zh-CN') : '0';
}
function scopeBadge(scope) {
  const personal = String(scope) === 'personal';
  return `<span class="badge ${personal ? 'info' : 'muted'}">${personal ? '个人' : '共享'}</span>`;
}
function transportBadge(item) {
  const list = Array.isArray(item?.transports) ? item.transports : ['server'];
  const browser = list.includes('browser');
  const server = list.includes('server');
  if (browser && server) return '<span class="badge warn">混合执行</span>';
  if (browser) return '<span class="badge info">浏览器执行</span>';
  return '<span class="badge muted">服务端执行</span>';
}
function modelLabel(item) {
  return `${item.model_name || item.model_id || '未命名'}${item.scope === 'personal' ? '（个人）' : ''}`;
}
function statusBadge(status) {
  const meta = TASK_STATUS[status] || ['muted', status || '—'];
  return `<span class="badge ${meta[0]}">${esc(meta[1])}</span>`;
}
function userLabel(item) {
  return esc(item.user_name || item.display_name || item.username || `用户 #${item.user_id}`);
}
function queryString(withPage = false) {
  const params = new URLSearchParams({ days: String(usageDays) });
  if (state.user?.role === 'admin' && usageUser) params.set('user_id', usageUser);
  if (withPage) {
    params.set('page', String(usagePage));
    params.set('page_size', '20');
  }
  return params.toString();
}

function metricValue(row) {
  return usageMetric === 'calls' ? Number(row.calls || 0) : Number(row.total_tokens || 0);
}

function chartHtml(data) {
  const daily = data.daily || [];
  if (!daily.length) return '<div class="usage-empty">当前范围没有模型调用记录</div>';
  const models = (data.models || []).slice(0, 6);
  const topKeys = new Set(models.map((m) => m.key));
  const other = { key: '__other', model_name: '其他', scope: '' };
  const allModels = [...models.map((m) => ({
    key: m.key, model_name: m.model_name || m.model_id || '未命名',
    scope: m.scope, total_tokens: m.total_tokens, calls: m.calls,
  })), other];
  const colors = new Map(allModels.map((m, i) => [m.key, MODEL_COLORS[i % MODEL_COLORS.length]]));

  const series = daily.map((d) => {
    const byKey = new Map();
    (d.models || []).forEach((m) => {
      const key = topKeys.has(m.key) ? m.key : '__other';
      byKey.set(key, (byKey.get(key) || 0) + metricValue(m));
    });
    return byKey;
  });
  const max = Math.max(1, ...series.flatMap((row) => [...row.values()]));
  const W = 760, H = 286, PL = 58, PR = 14, PT = 18, PB = 38;
  const plotW = W - PL - PR, plotH = H - PT - PB;
  const slot = plotW / daily.length;
  const barW = Math.max(4, Math.min(20, slot * 0.64));
  const yTicks = 4;
  const grid = Array.from({ length: yTicks + 1 }, (_, i) => {
    const value = (max / yTicks) * i;
    const y = PT + plotH - (value / max) * plotH;
    return `<g><line x1="${PL}" y1="${y}" x2="${W - PR}" y2="${y}" stroke="var(--line)" stroke-width="1"/>
      <text x="${PL - 8}" y="${y + 4}" text-anchor="end" class="usage-axis">${usageMetric === 'calls' ? num(Math.round(value)) : num(value)}</text></g>`;
  }).join('');
  const labelEvery = daily.length <= 10 ? 1 : daily.length <= 30 ? 3 : 7;
  const bars = daily.map((d, i) => {
    let y = PT + plotH;
    const x = PL + slot * i + (slot - barW) / 2;
    const parts = allModels.map((m) => {
      const value = series[i].get(m.key) || 0;
      if (!value) return '';
      const h = (value / max) * plotH;
      y -= h;
      return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barW.toFixed(2)}" height="${h.toFixed(2)}"
        fill="${colors.get(m.key)}" rx="2"><title>${esc(d.date)} ${esc(m.model_name)}：${usageMetric === 'calls' ? `${num(value)} 次调用` : `${num(value)} Token`}</title></rect>`;
    }).join('');
    const label = i % labelEvery === 0 || i === daily.length - 1
      ? `<text x="${(x + barW / 2).toFixed(2)}" y="${H - 14}" text-anchor="middle" class="usage-axis">${esc(d.date.slice(5))}</text>`
      : '';
    return `<g>${parts}${label}</g>`;
  }).join('');
  const legend = allModels.filter((m) => series.some((row) => row.get(m.key))).map((m) => `
    <span class="usage-legend-item"><i style="background:${colors.get(m.key)}"></i>${esc(m.model_name)}
      ${m.scope ? `<span class="usage-scope">${SCOPE_LABELS[m.scope] || ''}</span>` : ''}</span>`).join('');
  return `<div class="usage-chart-wrap">
    <svg class="usage-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="每日模型使用统计">
      <title>每日各模型${usageMetric === 'calls' ? '调用次数' : 'Token 消耗'}</title>
      ${grid}${bars}
    </svg>
    <div class="usage-legend">${legend}</div>
  </div>`;
}

function userChartHtml(data) {
  const userDaily = data.user_daily || [];
  const labels = (data.daily || []).map((d) => d.date);
  if (!userDaily.length || !labels.length) return '<div class="usage-empty">当前范围没有用户调用记录</div>';
  const dayMap = new Map(userDaily.map((d) => [d.date, d]));
  const aggregate = new Map();
  userDaily.forEach((day) => (day.users || []).forEach((u) => {
    const item = aggregate.get(String(u.user_id)) || {
      key: String(u.user_id), name: u.user_name || `用户 #${u.user_id}`,
      calls: 0, total_tokens: 0,
    };
    item.name = u.user_name || item.name;
    item.calls += Number(u.calls || 0);
    item.total_tokens += Number(u.total_tokens || 0);
    aggregate.set(item.key, item);
  }));
  const userItems = [...aggregate.values()].sort((a, b) =>
    (b.total_tokens - a.total_tokens) || (b.calls - a.calls) || a.key.localeCompare(b.key));
  const top = userItems.slice(0, 6);
  const topKeys = new Set(top.map((u) => u.key));
  const other = { key: '__other', name: '其他用户', calls: 0, total_tokens: 0 };
  userItems.forEach((u) => {
    if (topKeys.has(u.key)) return;
    other.calls += u.calls;
    other.total_tokens += u.total_tokens;
  });
  const items = [...top, other];
  const colors = new Map(items.map((u, i) => [u.key, u.key === '__other'
    ? '#94a3b8' : USER_COLORS[i % USER_COLORS.length]]));
  const metric = (u) => usageUserMetric === 'calls' ? Number(u.calls || 0) : Number(u.total_tokens || 0);
  const series = labels.map((date) => {
    const byKey = new Map();
    const day = dayMap.get(date);
    (day?.users || []).forEach((u) => {
      const key = topKeys.has(String(u.user_id)) ? String(u.user_id) : '__other';
      byKey.set(key, (byKey.get(key) || 0) + metric(u));
    });
    return byKey;
  });
  const max = Math.max(1, ...series.flatMap((row) => [...row.values()]));
  const W = 760, H = 286, PL = 58, PR = 14, PT = 18, PB = 38;
  const plotW = W - PL - PR, plotH = H - PT - PB;
  const slot = plotW / labels.length;
  const barW = Math.max(4, Math.min(20, slot * 0.64));
  const yTicks = 4;
  const grid = Array.from({ length: yTicks + 1 }, (_, i) => {
    const value = (max / yTicks) * i;
    const y = PT + plotH - (value / max) * plotH;
    return `<g><line x1="${PL}" y1="${y}" x2="${W - PR}" y2="${y}" stroke="var(--line)" stroke-width="1"/>
      <text x="${PL - 8}" y="${y + 4}" text-anchor="end" class="usage-axis">${usageUserMetric === 'calls' ? num(Math.round(value)) : num(value)}</text></g>`;
  }).join('');
  const labelEvery = labels.length <= 10 ? 1 : labels.length <= 30 ? 3 : 7;
  const bars = labels.map((date, i) => {
    let y = PT + plotH;
    const x = PL + slot * i + (slot - barW) / 2;
    const parts = items.map((u) => {
      const value = series[i].get(u.key) || 0;
      if (!value) return '';
      const h = (value / max) * plotH;
      y -= h;
      return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barW.toFixed(2)}" height="${h.toFixed(2)}"
        fill="${colors.get(u.key)}" rx="2"><title>${esc(date)} ${esc(u.name)}：${usageUserMetric === 'calls' ? `${num(value)} 次调用` : `${num(value)} Token`}</title></rect>`;
    }).join('');
    const label = i % labelEvery === 0 || i === labels.length - 1
      ? `<text x="${(x + barW / 2).toFixed(2)}" y="${H - 14}" text-anchor="middle" class="usage-axis">${esc(date.slice(5))}</text>`
      : '';
    return `<g>${parts}${label}</g>`;
  }).join('');
  const legend = items.filter((u) => series.some((row) => row.get(u.key))).map((u) => `
    <span class="usage-legend-item"><i style="background:${colors.get(u.key)}"></i>${esc(u.name)}
      <span class="usage-scope">${num(metric(u))} ${usageUserMetric === 'calls' ? '次' : 'Token'}</span></span>`).join('');
  return `<div class="usage-chart-wrap usage-user-chart">
    <svg class="usage-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="用户每日用量统计">
      <title>各用户每日${usageUserMetric === 'calls' ? '调用次数' : 'Token 消耗'}</title>
      ${grid}${bars}
    </svg>
    <div class="usage-legend">${legend}</div>
  </div>`;
}

function shellHtml() {
  const admin = state.user?.role === 'admin';
  const directory = admin ? usageDirectory : [];
  return `
    <div class="usage-toolbar">
      <div class="usage-filters">
        <label>统计范围
          <select class="select" data-change="range">${RANGE_OPTIONS.map((n) =>
            `<option value="${n}" ${n === usageDays ? 'selected' : ''}>近 ${n} 天</option>`).join('')}</select>
        </label>
        ${admin ? `<label>用户
          <select class="select" data-change="user">
            <option value="">全部用户</option>
            ${directory.map((u) => `<option value="${u.user_id}" ${String(u.user_id) === usageUser ? 'selected' : ''}>${userLabel(u)}</option>`).join('')}
          </select></label>` : `<span class="usage-self">仅显示我的调用记录</span>`}
      </div>
      <button class="btn btn-sm" data-act="refresh">${icon('refresh')}刷新</button>
    </div>
    <div id="usage-body"><div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div></div>`;
}


function syncUserOptions() {
  const select = document.querySelector('.usage-toolbar select[data-change="user"]');
  if (!select) return;
  select.innerHTML = `<option value="">全部用户</option>${usageDirectory.map((u) =>
    `<option value="${u.user_id}" ${String(u.user_id) === usageUser ? 'selected' : ''}>${userLabel(u)}</option>`).join('')}`;
  select.value = usageUser;
}

async function load() {
  if (usageLoading) return;
  usageLoading = true;
  const view = document.getElementById('usage-body');
  if (view) view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  try {
    const [data, tasks] = await Promise.all([
      api(`/api/usage/overview?${queryString()}`),
      api(`/api/usage/tasks?${queryString(true)}`),
    ]);
    if (state.user?.role === 'admin' && Array.isArray(data.directory)) usageDirectory = data.directory;
    syncUserOptions();
    render(data, tasks);
  } catch (err) {
    if (view) view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
  } finally {
    usageLoading = false;
  }
}

function render(data, taskPage) {
  const view = document.getElementById('usage-body');
  if (!view) return;
  const totals = data.totals || {};
  const usersCount = data.directory?.length || totals.users || 0;
  const cards = `
    <div class="grid grid-4 usage-kpis">
      <div class="card stat"><div class="stat-icon info">${icon('activity')}</div><div><div class="stat-val">${num(totals.calls)}</div><div class="stat-label">模型调用</div><div class="stat-note">失败 ${num(totals.failed_calls)} 次 · 浏览器 ${num(totals.browser_calls)} 次</div></div></div>
      <div class="card stat"><div class="stat-icon">${icon('zap')}</div><div><div class="stat-val" title="${num(totals.total_tokens)}">${num(totals.total_tokens)}</div><div class="stat-label">Token 消耗</div><div class="stat-note">输入 ${num(totals.input_tokens)} / 输出 ${num(totals.output_tokens)}</div></div></div>
      <div class="card stat"><div class="stat-icon ok">${icon('list')}</div><div><div class="stat-val">${num(totals.tasks)}</div><div class="stat-label">覆盖任务</div><div class="stat-note">${usageDays} 天窗口</div></div></div>
      <div class="card stat"><div class="stat-icon warn">${state.user?.role === 'admin' ? icon('users') : icon('layers')}</div><div><div class="stat-val">${num(state.user?.role === 'admin' ? usersCount : totals.skills)}</div><div class="stat-label">${state.user?.role === 'admin' ? '活跃用户' : '执行技能'}</div><div class="stat-note">${state.user?.role === 'admin' ? '有模型调用的账号' : '有执行的技能数'}</div></div></div>
    </div>`;
  const modelRows = (data.models || []).map((m) => `<tr>
      <td><strong>${esc(m.model_name || m.model_id || '—')}</strong> ${scopeBadge(m.scope)} ${transportBadge(m)}<div class="sub">ID ${esc(m.model_id || '—')}${m.browser_calls ? ` · 浏览器 ${num(m.browser_calls)} 次` : ''}</div></td>
      <td class="num">${num(m.calls)}<div class="sub">失败 ${num(m.failed_calls)}</div></td>
      <td class="num">${num(m.input_tokens)}</td>
      <td class="num">${num(m.output_tokens)}</td>
      <td class="num"><strong>${num(m.total_tokens)}</strong></td>
      <td class="num">${num(m.users)}</td>
      <td class="num">${num(m.latency_ms_avg)} ms</td>
    </tr>`).join('') || `<tr><td colspan="7" class="usage-empty">暂无模型调用</td></tr>`;
  const modelTable = `<div class="card">
    <div class="card-head"><h3>模型明细</h3><span class="sub">共享模型与个人 API 分开展示</span></div>
    <div class="card-body table-wrap"><table class="data usage-table">
      <thead><tr><th>模型</th><th class="num">调用</th><th class="num">输入 Token</th><th class="num">输出 Token</th><th class="num">总 Token</th><th class="num">用户</th><th class="num">平均耗时</th></tr></thead>
      <tbody>${modelRows}</tbody></table></div></div>`;

  const dailyRows = (data.daily || []).slice().reverse().map((d) => `<tr>
      <td>${esc(d.date)}</td>
      <td class="num">${num(d.calls)}</td>
      <td class="num">${num(d.total_tokens)}</td>
      <td class="usage-daily-models">${(d.models || []).length ? d.models.map((m) => `${esc(modelLabel(m))} ${num(m.total_tokens)}`).join(' · ') : '—'}</td>
    </tr>`).join('') || `<tr><td colspan="4" class="usage-empty">暂无每日记录</td></tr>`;
  const dailyTable = `<details class="card usage-daily-table">
    <summary class="card-head"><h3>每日明细</h3><span class="sub">按本地时区切日 · 点击展开明细</span></summary>
    <div class="card-body table-wrap"><table class="data usage-table">
      <thead><tr><th>日期</th><th class="num">调用</th><th class="num">Token</th><th>模型分布（Token）</th></tr></thead>
      <tbody>${dailyRows}</tbody></table></div></details>`;

  const userDaily = data.user_daily || [];
  const userSummary = new Map();
  userDaily.forEach((day) => {
    (day.users || []).forEach((u) => {
      const item = userSummary.get(u.user_id) || {
        user_id: u.user_id, user_name: u.user_name, calls: 0, failed_calls: 0,
        input_tokens: 0, output_tokens: 0, total_tokens: 0, active_days: 0,
      };
      item.user_name = u.user_name || item.user_name;
      item.calls += Number(u.calls || 0);
      item.failed_calls += Number(u.failed_calls || 0);
      item.input_tokens += Number(u.input_tokens || 0);
      item.output_tokens += Number(u.output_tokens || 0);
      item.total_tokens += Number(u.total_tokens || 0);
      item.active_days += 1;
      userSummary.set(u.user_id, item);
    });
  });
  const userRows = [...userSummary.values()].sort((a, b) =>
    (b.total_tokens - a.total_tokens) || (b.calls - a.calls) || (a.user_id - b.user_id));
  const userSummaryRows = userRows.map((u) => `<tr>
      <td data-label="用户"><strong>${userLabel(u)}</strong><div class="sub">ID ${esc(String(u.user_id))}</div></td>
      <td class="num" data-label="活跃天数">${num(u.active_days)}</td>
      <td class="num" data-label="调用"><strong>${num(u.calls)}</strong><div class="sub">失败 ${num(u.failed_calls)}</div></td>
      <td class="num" data-label="输入 Token">${num(u.input_tokens)}</td>
      <td class="num" data-label="输出 Token">${num(u.output_tokens)}</td>
      <td class="num" data-label="总 Token"><strong>${num(u.total_tokens)}</strong></td>
      <td class="num" data-label="日均调用">${u.active_days ? (u.calls / u.active_days).toFixed(1) : '0'}</td>
      <td class="num" data-label="日均 Token">${u.active_days ? num(Math.round(u.total_tokens / u.active_days)) : '0'}</td>
    </tr>`).join('') || `<tr><td colspan="8" class="usage-empty">当前范围没有用户调用</td></tr>`;
  const userDailyRows = userDaily.slice().reverse().flatMap((day) => (day.users || []).map((u) => `<tr>
      <td data-label="日期">${esc(day.date)}</td>
      <td data-label="用户"><strong>${userLabel(u)}</strong><div class="sub">ID ${esc(String(u.user_id))}</div></td>
      <td class="num" data-label="调用">${num(u.calls)}</td>
      <td class="num" data-label="失败">${num(u.failed_calls)}</td>
      <td class="num" data-label="输入 Token">${num(u.input_tokens)}</td>
      <td class="num" data-label="输出 Token">${num(u.output_tokens)}</td>
      <td class="num" data-label="总 Token"><strong>${num(u.total_tokens)}</strong></td>
    </tr>`)).join('') || `<tr><td colspan="7" class="usage-empty">暂无每日用户记录</td></tr>`;
  const userTable = `<div class="card">
    <div class="card-head"><div><h3>用户每日用量</h3><span class="sub">按日堆叠展示每个用户的调用次数与 Token 总量</span></div>
      <div class="usage-metric-toggle">
        <button class="btn btn-sm ${usageUserMetric === 'total_tokens' ? 'btn-primary' : ''}" data-act="user-metric" data-metric="total_tokens">Token</button>
        <button class="btn btn-sm ${usageUserMetric === 'calls' ? 'btn-primary' : ''}" data-act="user-metric" data-metric="calls">调用次数</button>
      </div>
    </div>
    <div class="card-body">${userChartHtml(data)}</div>
    <div class="card-body table-wrap"><table class="data usage-table responsive">
      <thead><tr><th>用户</th><th class="num">活跃天数</th><th class="num">调用</th><th class="num">输入 Token</th><th class="num">输出 Token</th><th class="num">总 Token</th><th class="num">日均调用</th><th class="num">日均 Token</th></tr></thead>
      <tbody>${userSummaryRows}</tbody></table></div>
    <div class="card-body" style="padding-top:0">
      <details class="usage-subdetails">
        <summary>展开每日明细（${num(userDaily.length)} 天）</summary>
        <div class="table-wrap" style="margin-top:10px"><table class="data usage-table responsive">
          <thead><tr><th>日期</th><th>用户</th><th class="num">调用</th><th class="num">失败</th><th class="num">输入 Token</th><th class="num">输出 Token</th><th class="num">总 Token</th></tr></thead>
          <tbody>${userDailyRows}</tbody></table></div>
      </details>
    </div></div>`;

  const skillRows = (data.skills || []).map((s) => `<tr>
      <td><strong>${esc(s.skill_name)}</strong></td>
      <td class="num">${num(s.executions)}<div class="sub">失败 ${num(s.failed)}</div></td>
      <td class="num">${num(s.task_count)}</td>
      <td class="num"><strong>${num(s.tokens)}</strong></td>
      <td class="num">${num(s.tool_calls)}</td>
      <td class="num">${num(s.evidence_count)}</td>
      <td class="num">${fmtSize(s.input_bytes || 0)}</td>
      <td class="num">${s.executions ? fmtDur(Math.round((s.duration_ms || 0) / 1000 / s.executions)) : '—'}</td>
      <td>${esc((s.last_at || '').slice(0, 10) || '—')}</td>
    </tr>`).join('') || `<tr><td colspan="9" class="usage-empty">暂无技能执行记录</td></tr>`;
  const skillsTable = `<div class="card">
    <div class="card-head"><h3>技能消耗</h3><span class="sub">按技能执行统计；关联输入为技能所在任务输入文件大小</span></div>
    <div class="card-body table-wrap"><table class="data usage-table">
      <thead><tr><th>技能</th><th class="num">执行</th><th class="num">任务</th><th class="num">Token</th><th class="num">工具调用</th><th class="num">证据</th><th class="num">关联输入</th><th class="num">平均耗时</th><th>最近执行</th></tr></thead>
      <tbody>${skillRows}</tbody></table></div></div>`;

  const taskItems = taskPage.items || [];
  const taskRows = taskItems.map((t) => `<tr>
      <td><a href="#/tasks/${encodeURIComponent(t.task_id)}"><strong>${esc(t.title || `任务 ${t.task_id}`)}</strong></a>
        ${t.has_events ? '' : '<span class="badge muted" title="该任务创建于用量明细上线前，显示的是任务汇总值">历史汇总</span>'}
        <div class="sub">${esc(String(t.task_id))}</div></td>
      <td>${state.user?.role === 'admin' ? userLabel(t) : '—'}</td>
      <td>${statusBadge(t.status)}<div class="sub">${esc(t.mode || '')}</div></td>
      <td class="num">${num(t.calls)}</td>
      <td class="num">${num(t.input_tokens)}</td>
      <td class="num">${num(t.output_tokens)}</td>
      <td class="num"><strong>${num(t.total_tokens)}</strong></td>
      <td>${t.browser_calls ? `<span class="badge info">浏览器 ${num(t.browser_calls)} 次</span>` : '<span class="badge muted">服务端</span>'}
        <div class="sub">${(t.models || []).map((m) => `<span class="usage-model-chip">${esc(m)}</span>`).join('') || '—'}</div></td>
      <td>${esc((t.created_at || '').slice(0, 10) || '—')}</td>
    </tr>`).join('') || `<tr><td colspan="9" class="usage-empty">当前范围没有诊断任务</td></tr>`;
  const totalPages = Math.max(1, Math.ceil(Number(taskPage.total || 0) / Number(taskPage.page_size || 20)));
  const tasksTable = `<div class="card">
    <div class="card-head"><h3>任务 Token</h3><span class="sub">每次诊断任务的模型调用与 Token，按任务创建时间筛选</span></div>
    <div class="card-body table-wrap"><table class="data usage-table">
      <thead><tr><th>任务</th><th>用户</th><th>状态</th><th class="num">调用</th><th class="num">输入</th><th class="num">输出</th><th class="num">总 Token</th><th>模型</th><th>创建</th></tr></thead>
      <tbody>${taskRows}</tbody></table></div>
    <div class="card-foot usage-pager">
      <span class="sub">共 ${num(taskPage.total)} 个任务，第 ${num(taskPage.page)} / ${num(totalPages)} 页</span>
      <div class="inline">
        <button class="btn btn-sm" data-act="prev" ${Number(taskPage.page) <= 1 ? 'disabled' : ''}>上一页</button>
        <button class="btn btn-sm" data-act="next" ${Number(taskPage.page) >= totalPages ? 'disabled' : ''}>下一页</button>
      </div>
    </div></div>`;

  view.innerHTML = `${cards}
    <div class="card usage-chart-card">
      <div class="card-head"><h3>每日模型使用</h3>
        <div class="usage-metric-toggle">
          <button class="btn btn-sm ${usageMetric === 'total_tokens' ? 'btn-primary' : ''}" data-act="metric" data-metric="total_tokens">Token</button>
          <button class="btn btn-sm ${usageMetric === 'calls' ? 'btn-primary' : ''}" data-act="metric" data-metric="calls">调用次数</button>
        </div>
      </div>
      <div class="card-body">${chartHtml(data)}</div>
    </div>
    <div class="usage-stack">${modelTable}${userTable}${dailyTable}</div>
    ${skillsTable}
    ${tasksTable}`;
}

export async function renderUsage() {
  const view = mount('用量统计', state.user?.role === 'admin'
    ? '查看全部用户与个人 API 的模型调用、任务 Token 与技能消耗'
    : '查看自己的模型调用、任务 Token 与技能消耗', '');
  view.innerHTML = shellHtml();
  bindActions(view, {
    refresh: () => load(),
    prev: () => { usagePage = Math.max(1, usagePage - 1); load(); },
    next: () => { usagePage += 1; load(); },
    metric: (el) => { usageMetric = el.dataset.metric || 'total_tokens'; load(); },
    'user-metric': (el) => { usageUserMetric = el.dataset.metric || 'total_tokens'; load(); },
    range: (el) => { usageDays = Number(el.value) || 30; usagePage = 1; load(); },
    user: (el) => { usageUser = el.value || ''; usagePage = 1; usageDirectory = []; renderUsage(); },
  });
  await load();
  return view;
}
