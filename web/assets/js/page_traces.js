// 管理员执行轨迹：诊断 / 技能工坊每一步操作、模型原文、工具输出与失败详情
import { api, bindActions, esc, fmtDate, fmtDur, fmtSize, icon, mount, state, statusBadge, toast } from './core.js';

const KIND_LABELS = { diagnosis: '故障诊断', workshop: '技能工坊' };
const EVENT_LABELS = {
  run_start: '开始', stage: '阶段', step_start: '步骤开始', step_end: '步骤结束',
  model_request: '模型请求', model_response: '模型回复', model_error: '模型失败',
  tool_call: '工具调用', tool_result: '工具结果', validation: '校验', error: '错误',
  run_end: '结束',
};
const STATUS_LABELS = {
  running: '运行中', success: '成功', failed: '失败', needs_data: '需补充数据',
  cancelled: '已取消', interrupted: '已中断', disabled: '未启用', queued: '排队中',
};
const FAILED_EVENT_TYPES = ['error', 'model_error'];

function kindLabel(kind) { return KIND_LABELS[kind] || kind || '—'; }
function eventLabel(type) { return EVENT_LABELS[type] || type || '事件'; }
function statusText(status) { return STATUS_LABELS[status] || status || '—'; }
function short(value, limit = 160) {
  const text = String(value == null ? '' : value);
  return text.length > limit ? text.slice(0, limit) + '…' : text;
}
function durationText(run) {
  if (!run?.started_at) return '—';
  const start = new Date(run.started_at).getTime();
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return '—';
  return fmtDur(Math.max(0, (end - start) / 1000));
}
function traceStatus(run) {
  const status = run?.status || '';
  const cls = status === 'success' ? 'ok' : status === 'failed' || status === 'interrupted' ? 'danger'
    : status === 'running' ? 'info' : 'muted';
  return `<span class="badge ${cls}">${esc(statusText(status))}</span>`;
}
function jsonBlock(label, value, open = false) {
  if (value == null || value === '') return '';
  let text;
  try { text = typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
  catch { text = String(value); }
  if (!text) return '';
  return `<details class="trace-json" ${open ? 'open' : ''}><summary>${esc(label)}</summary><pre class="trace-pre">${esc(text)}</pre></details>`;
}

function eventTone(event) {
  if (event.type === 'error' || event.type === 'model_error' || event.status === 'failed') return 'danger';
  if (event.type === 'validation' && !event.ok) return 'danger';
  if (event.type === 'validation') return 'ok';
  if (event.type === 'model_response' || event.type === 'tool_result') return 'ok';
  if (event.type === 'model_request' || event.type === 'tool_call') return 'info';
  return 'muted';
}

function eventMeta(event) {
  const bits = [];
  if (event.phase) bits.push(`阶段 ${event.phase}`);
  if (event.role) bits.push(`角色 ${event.role}`);
  if (event.model?.name) bits.push(`模型 ${event.model.name}`);
  if (event.purpose) bits.push(`用途 ${event.purpose}`);
  if (event.attempt) bits.push(`第 ${event.attempt} 次`);
  if (event.step_id) bits.push(`步骤 ${event.step_id}`);
  if (event.tool) bits.push(`工具 ${event.tool}`);
  if (event.tool_call_id) bits.push(`调用 ${event.tool_call_id}`);
  if (event.name) bits.push(`名称 ${event.name}`);
  return bits.map((x) => `<span class="trace-meta">${esc(x)}</span>`).join('');
}

function eventBody(event) {
  const type = event.type;
  if (type === 'model_request') {
    const req = event.request || {};
    return `
      <div class="small muted">模型请求 · ${esc(event.model?.provider || '')} ${esc(event.model?.model || '')} · stream=${req.stream ? '是' : '否'} · max_tokens=${esc(req.max_tokens ?? '—')}</div>
      ${jsonBlock('System 提示词', req.system)}
      ${jsonBlock('User 完整输入', req.user, true)}
      ${jsonBlock('JSON Schema', req.json_schema)}
      ${jsonBlock('请求元数据', { purpose: req.purpose, stage: req.stage, attempt: req.attempt, repair: req.repair, temperature: req.temperature, stream: req.stream })}`;
  }
  if (type === 'model_response') {
    const res = event.response || {};
    return `
      <div class="small muted">模型回复 · ${esc(res.latency_ms ?? '—')}ms · ${esc(res.usage?.total_tokens ?? res.usage?.input_tokens ?? 0)} tokens · HTTP ${esc(res.http_status ?? 200)}</div>
      ${jsonBlock('原始回复全文', res.text, true)}
      ${jsonBlock('解析后的 JSON', res.parsed)}
      ${jsonBlock('Token 用量', res.usage)}`;
  }
  if (type === 'model_error') {
    const err = event.error || {};
    return `
      <div class="alert danger" style="margin-bottom:10px">${icon('alert')}<div><strong>${esc(err.type || '模型调用失败')}</strong>
        ${err.http_status ? ` · HTTP ${esc(err.http_status)}` : ''}${err.retryable ? ' · 可重试' : ' · 不重试'}
        <div class="small" style="margin-top:4px;white-space:pre-wrap">${esc(err.message || '')}</div></div></div>
      ${jsonBlock('服务端响应体（未截断前）', err.response_body, true)}`;
  }
  if (type === 'tool_call') {
    return `${jsonBlock('工具参数', event.arguments, true)}
      ${jsonBlock('决策与审批', event.decision)}
      ${jsonBlock('沙箱配置', event.sandbox)}`;
  }
  if (type === 'tool_result') {
    return `
      <div class="small muted">状态 ${esc(event.status || '—')} · 退出码 ${esc(event.exit_code ?? '—')} · ${esc(event.duration_ms ?? 0)}ms</div>
      ${event.error ? `<div class="alert warn" style="margin-top:8px">${icon('alert')}<div>${esc(event.error)}</div></div>` : ''}
      ${jsonBlock('stdout（末尾 4KB）', event.stdout, true)}
      ${jsonBlock('stderr（末尾 4KB）', event.stderr)}
      ${jsonBlock('结果摘要', event.result)}
      ${jsonBlock('产物路径', event.artifacts)}`;
  }
  if (type === 'validation') {
    return `<div class="small ${event.ok ? 'muted' : ''}">${event.ok ? '校验通过' : '校验未通过'}</div>
      ${jsonBlock('错误列表', event.errors, !event.ok)}
      ${jsonBlock('校验详情', event.details)}`;
  }
  if (type === 'error') {
    return `<div class="small" style="white-space:pre-wrap">${esc(event.message || '')}</div>
      ${jsonBlock('异常堆栈', event.stack, true)}
      ${jsonBlock('附加详情', event.details)}`;
  }
  if (type === 'stage') {
    return `<div class="small">${esc(event.message || '')} · 进度 ${esc(event.progress ?? '—')}%</div>`;
  }
  if (type === 'run_start' || type === 'run_end') {
    return `${jsonBlock('运行信息', event.meta)}
      ${event.error_summary ? `<div class="alert ${event.status === 'failed' ? 'danger' : 'warn'}" style="margin-top:8px">${icon('alert')}<div>${esc(event.error_summary)}</div></div>` : ''}`;
  }
  return jsonBlock('事件详情', event);
}

function eventCard(event) {
  const failed = event.type === 'error' || event.type === 'model_error' || event.status === 'failed'
    || (event.type === 'validation' && !event.ok);
  return `<div class="card trace-event ${failed ? 'trace-event-failed' : ''}">
    <div class="card-body">
      <div class="between trace-event-head">
        <div class="inline" style="gap:8px;flex-wrap:wrap">
          <span class="badge ${eventTone(event)}">${esc(eventLabel(event.type))}</span>
          ${event.status ? `<span class="tiny muted">#${esc(event.seq)} · ${esc(event.status)}</span>` : `<span class="tiny muted">#${esc(event.seq)}</span>`}
          ${eventMeta(event)}
        </div>
        <span class="tiny muted">${esc(event.ts || '')}</span>
      </div>
      <div class="trace-event-body">${eventBody(event)}</div>
      ${jsonBlock('单条原始 JSON', event)}
    </div></div>`;
}

function failureBanner(summary) {
  const first = summary?.first_error;
  if (!first) return '';
  const type = first.type === 'model_error' ? '模型调用失败' : first.type === 'validation' ? '校验未通过' : '执行错误';
  const msg = first.error?.message || first.message
    || (first.errors || []).map((x) => typeof x === 'string' ? x : JSON.stringify(x)).join('；')
    || '未知错误';
  const http = first.error?.http_status ? ` · HTTP ${first.error.http_status}` : '';
  return `<div class="alert danger" id="trace-failure" style="margin-bottom:14px">${icon('alert')}<div>
    <strong>首个失败：${esc(type)}${esc(http)}</strong>
    <div class="small" style="margin-top:4px;white-space:pre-wrap">${esc(short(msg, 800))}</div>
    <div class="tiny muted" style="margin-top:4px">阶段：${esc(first.phase || '—')} · 事件 #${esc(first.seq || '—')} · ${esc(eventLabel(first.type))}</div>
  </div></div>`;
}

export async function renderTraceDetail(runId) {
  if (state.user?.role !== 'admin') {
    const view = mount('无权访问', '执行轨迹仅管理员可见', '');
    view.innerHTML = `<div class="card"><div class="card-body"><div class="alert warn">${icon('lock')}<div>执行轨迹包含模型完整输入输出与工具日志，仅管理员账号可查看。</div></div>
      <a class="btn" style="margin-top:12px" href="#/dashboard">返回总览</a></div></div>`;
    return view;
  }
  const view = mount('执行轨迹', '逐条查看模型输入、模型回复、工具调用、校验与失败详情', '');
  view.innerHTML = `<div class="skeleton" style="height:160px;margin-bottom:14px"></div><div class="skeleton" style="height:420px"></div>`;
  let summary = null;
  let events = [];
  let total = 0;
  let next = 0;
  let typeFilter = '';
  let onlyFailed = false;
  let query = '';

  async function load() {
    try {
      const [s, e] = await Promise.all([
        api(`/api/traces/${encodeURIComponent(runId)}/summary`),
        api(`/api/traces/${encodeURIComponent(runId)}/events?limit=1000`),
      ]);
      summary = s;
      events = e.items || [];
      total = e.total || 0;
      next = e.next || 0;
      render();
    } catch (err) {
      view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div>
        <a class="btn" style="margin-top:10px" href="#/traces">返回轨迹列表</a></div>`;
    }
  }

  function filteredEvents() {
    return events.filter((event) => {
      if (typeFilter && event.type !== typeFilter) return false;
      if (onlyFailed) {
        const failed = FAILED_EVENT_TYPES.includes(event.type) || event.status === 'failed'
          || (event.type === 'validation' && !event.ok);
        if (!failed) return false;
      }
      if (query) {
        let hay = '';
        try { hay = JSON.stringify(event); } catch { hay = String(event); }
        if (!hay.toLowerCase().includes(query.toLowerCase())) return false;
      }
      return true;
    });
  }

  function render() {
    const run = summary?.run || {};
    const visible = filteredEvents();
    view.innerHTML = `
      <div class="inline" style="margin-bottom:12px;flex-wrap:wrap">
        <a class="btn btn-sm" href="#/traces">${icon('history')}轨迹列表</a>
        <a class="btn btn-sm" href="${esc(run.ref_url || '#/dashboard')}">${icon('eye')}查看关联对象</a>
        <a class="btn btn-sm" href="/api/traces/${encodeURIComponent(runId)}/download">${icon('download')}下载原始 JSONL</a>
        <button class="btn btn-sm" data-act="refresh">${icon('refresh')}刷新</button>
      </div>
      <div class="card"><div class="card-body">
        <div class="between" style="gap:12px;flex-wrap:wrap">
          <div>
            <div class="inline" style="gap:8px;flex-wrap:wrap">
              ${traceStatus(run)}
              <span class="badge info">${esc(kindLabel(run.kind))}</span>
              <span class="id mono">${esc(run.id || runId)}</span>
              <span class="tiny muted">第 ${esc(run.attempt_no || 1)} 次执行</span>
            </div>
            <h2 style="margin:8px 0 4px">${esc(run.ref_title || run.ref_id || '—')}</h2>
          </div>
          <div class="tiny muted" style="text-align:right">
            <div>开始 ${fmtDate(run.started_at)}</div>
            <div>结束 ${run.finished_at ? fmtDate(run.finished_at) : '—'}</div>
            <div>耗时 ${durationText(run)} · ${fmtSize(run.size_bytes)}</div>
          </div>
        </div>
        <div class="grid grid-4" style="margin-top:14px">
          <div class="kv small"><div class="k">事件</div><div class="v">${esc(summary?.event_count ?? run.event_count ?? 0)}</div></div>
          <div class="kv small"><div class="k">模型调用</div><div class="v">${esc(run.model_calls ?? 0)}</div></div>
          <div class="kv small"><div class="k">工具调用</div><div class="v">${esc(run.tool_calls ?? 0)}</div></div>
          <div class="kv small"><div class="k">失败事件</div><div class="v">${esc((summary?.errors || []).length)}</div></div>
        </div>
        ${run.meta && Object.keys(run.meta).length ? jsonBlock('运行元数据', run.meta) : ''}
      </div></div>
      ${failureBanner(summary)}
      <div class="card" style="margin-top:14px"><div class="card-body">
        <div class="toolbar" style="align-items:center">
          <select class="select" id="trace-type" aria-label="按事件类型筛选" style="width:170px">
            <option value="">全部事件</option>
            ${Object.entries(EVENT_LABELS).map(([k, v]) => `<option value="${k}" ${typeFilter === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}
          </select>
          <input class="input" id="trace-query" value="${esc(query)}" placeholder="搜索输入、回复、工具输出或错误" style="max-width:360px">
          <label class="check"><input type="checkbox" id="trace-failed" ${onlyFailed ? 'checked' : ''}> 只看失败</label>
          <button class="btn btn-sm" data-act="apply">筛选</button>
          <button class="btn btn-sm btn-ghost" data-act="reset">重置</button>
          <span class="tiny muted" style="margin-left:auto">显示 ${visible.length} / 已加载 ${events.length} · 文件共 ${total} 条</span>
        </div>
      </div></div>
      <div class="trace-list" style="margin-top:14px">
        ${visible.length ? visible.map(eventCard).join('') : `<div class="card"><div class="card-body"><div class="empty" style="padding:28px">${icon('list')}<h3>没有匹配的事件</h3><p>调整筛选条件，或下载原始 JSONL 查看全部记录。</p></div></div></div>`}
      </div>
      ${total > events.length ? `<div class="alert info" style="margin-top:14px">${icon('info')}<div>页面已加载前 ${events.length} 条事件，共 ${total} 条；请下载原始 JSONL 查看完整轨迹。</div></div>` : ''}
      ${summary?.last_model_response ? `<div class="card" style="margin-top:14px"><div class="card-head"><h2>最近一次模型回复</h2><span class="sub">用于快速确认模型最后说了什么</span></div>
        <div class="card-body">${jsonBlock('回复全文', summary.last_model_response.response?.text, true)}</div></div>` : ''}`;
    bindActions(view, {
      refresh: () => load(),
      apply: () => {
        typeFilter = view.querySelector('#trace-type')?.value || '';
        query = view.querySelector('#trace-query')?.value.trim() || '';
        onlyFailed = !!view.querySelector('#trace-failed')?.checked;
        render();
      },
      reset: () => { typeFilter = ''; query = ''; onlyFailed = false; render(); },
    });
    view.querySelectorAll('.toolbar input').forEach((el) => el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); view.querySelector('[data-act="apply"]')?.click(); }
    }));
  }

  await load();
  return view;
}

export async function renderTraces() {
  if (state.user?.role !== 'admin') {
    const view = mount('无权访问', '执行轨迹仅管理员可见', '');
    view.innerHTML = `<div class="card"><div class="card-body"><div class="alert warn">${icon('lock')}<div>执行轨迹仅管理员账号可查看。</div></div>
      <a class="btn" style="margin-top:12px" href="#/dashboard">返回总览</a></div></div>`;
    return view;
  }
  const view = mount('执行轨迹', '诊断与技能工坊的管理员运行审计', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  async function load() {
    try {
      const data = await api('/api/traces?limit=100');
      render(data.items || []);
    } catch (err) {
      view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    }
  }
  function render(items) {
    view.innerHTML = `
      <div class="inline" style="margin-bottom:12px">
        <button class="btn btn-sm" data-act="refresh">${icon('refresh')}刷新</button>
        <span class="tiny muted">轨迹包含完整模型输入输出，属于管理员审计数据，请按需下载。</span>
      </div>
      <div class="card"><div class="card-body tight table-wrap"><table class="data responsive"><thead><tr>
        <th>时间</th><th>类型</th><th>关联对象</th><th>状态</th><th>事件</th><th>模型</th><th>工具</th><th>耗时</th><th>失败摘要</th></tr></thead><tbody>
        ${items.length ? items.map((item) => `<tr>
          <td data-label="时间" class="tiny muted nowrap">${fmtDate(item.started_at)}</td>
          <td data-label="类型"><span class="badge info">${esc(kindLabel(item.kind))}</span></td>
          <td data-label="关联对象"><a href="#/traces/${encodeURIComponent(item.id)}"><strong>${esc(item.ref_title || item.ref_id)}</strong></a>
            <div class="mono tiny muted">${esc(item.ref_id)}</div></td>
          <td data-label="状态">${traceStatus(item)}</td>
          <td data-label="事件">${esc(item.event_count || 0)}</td>
          <td data-label="模型">${esc(item.model_calls || 0)}</td>
          <td data-label="工具">${esc(item.tool_calls || 0)}</td>
          <td data-label="耗时">${durationText(item)}</td>
          <td data-label="失败摘要" class="small" style="max-width:320px">${item.error_summary ? esc(short(item.error_summary, 180)) : '<span class="muted">—</span>'}</td>
        </tr>`).join('') : '<tr><td colspan="9" class="muted" style="text-align:center;padding:30px">还没有轨迹。诊断任务或工坊任务运行后会自动生成。</td></tr>'}
      </tbody></table></div></div>`;
    bindActions(view, { refresh: () => load() });
  }
  await load();
  return view;
}

export async function renderTraceListPanel(panel) {
  if (!panel) return;
  panel.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 轨迹索引加载中…</div></div>`;
  try {
    const data = await api('/api/traces?limit=20');
    const items = data.items || [];
    panel.innerHTML = `<div class="card"><div class="card-head"><h2>执行轨迹</h2>
      <div class="inline" style="margin-left:auto"><a class="btn btn-sm" href="#/traces">查看全部</a></div></div>
      <div class="card-body tight table-wrap"><table class="data"><thead><tr><th>时间</th><th>类型</th><th>对象</th><th>状态</th><th>失败摘要</th></tr></thead><tbody>
        ${items.length ? items.map((item) => `<tr>
          <td class="tiny muted nowrap">${fmtDate(item.started_at)}</td>
          <td>${esc(kindLabel(item.kind))}</td>
          <td><a href="#/traces/${encodeURIComponent(item.id)}">${esc(item.ref_title || item.ref_id)}</a></td>
          <td>${traceStatus(item)}</td>
          <td class="small">${item.error_summary ? esc(short(item.error_summary, 120)) : '<span class="muted">—</span>'}</td>
        </tr>`).join('') : '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">还没有执行轨迹</td></tr>'}
      </tbody></table></div></div>`;
  } catch (err) {
    panel.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
  }
}
