// 故障知识库：检索问答、引用案例、文档库、导入任务与审核队列
import {
  api, bindActions, confirmDialog, emptyState, esc, fmtDate, icon, mount, modal,
  setBusy, state, toast,
} from './core.js';

const STATUS_LABEL = { active: '生效', reviewing: '待审核', disabled: '已停用', deleted: '已删除' };
const STATUS_BADGE = { active: 'ok', reviewing: 'warn', disabled: '', deleted: 'danger' };
const JOB_LABEL = {
  queued: '排队中', processing: '解析中', completed: '已完成', partial: '部分完成',
  failed: '失败', cancelled: '已取消', cancelling: '取消中', rolled_back: '已回退',
};

const KB_PROGRESS_STEPS = {
  search: [['parse', '解析问题与过滤条件'], ['retrieve', '检索索引与候选案例'], ['rank', '融合排序并生成结果']],
  chat: [['parse', '解析问题与过滤条件'], ['retrieve', '检索索引与候选案例'], ['rank', '融合排序'], ['answer', '整理回答与引用']],
};

function kbProgressHtml(endpoint, query, filters) {
  const steps = KB_PROGRESS_STEPS[endpoint] || KB_PROGRESS_STEPS.search;
  return `<div class="card kb-progress" id="kb-progress" data-endpoint="${endpoint}">
    <div class="card-head"><h2>检索进度</h2><span class="sub">后台操作会逐步显示；完成前请不要重复提交</span>
      <span class="mono tiny" id="kb-progress-clock" style="margin-left:auto">0.0s</span></div>
    <div class="card-body">
      <div class="kb-progress-bar"><span id="kb-progress-fill" style="width:4%"></span></div>
      <div class="kb-progress-steps">${steps.map(([key, label], i) => `<div class="kb-progress-step ${i === 0 ? 'active' : ''}" data-step="${key}">
        <span class="kb-progress-dot"></span><strong>${label}</strong><span class="tiny muted kb-progress-state">${i === 0 ? '进行中' : '等待'}</span>
      </div>`).join('')}</div>
      <details class="kb-progress-detail"><summary>查看后台操作</summary>
        <div class="tiny muted">请求：${endpoint === 'chat' ? 'POST /api/kb/chat' : 'GET /api/kb/search'}；
        查询：${esc(query.slice(0, 160))}${query.length > 160 ? '…' : ''}；
        过滤：${esc(JSON.stringify(filters || {}))}</div>
        <div class="tiny muted">本次检索完成后会写入知识库查询日志，并可在「任务记录 → 知识库操作」查看。</div>
      </details>
      <div id="kb-progress-note" class="tiny muted">正在准备检索…</div>
    </div></div>`;
}

function startKbProgress(view, endpoint, query, filters) {
  const target = view.querySelector('#kb-result');
  if (!target) return { finish() {}, fail() {} };
  target.innerHTML = kbProgressHtml(endpoint, query, filters);
  const started = performance.now();
  const steps = KB_PROGRESS_STEPS[endpoint] || KB_PROGRESS_STEPS.search;
  let index = 0;
  const stepEls = target.querySelectorAll('.kb-progress-step');
  const fill = target.querySelector('#kb-progress-fill');
  const clock = target.querySelector('#kb-progress-clock');
  const note = target.querySelector('#kb-progress-note');
  const advance = () => {
    if (!target.isConnected) return;
    index = Math.min(index + 1, steps.length - 1);
    stepEls.forEach((el, i) => {
      el.classList.toggle('active', i === index);
      el.classList.toggle('done', i < index);
      const state = el.querySelector('.kb-progress-state');
      if (state) state.textContent = i < index ? '完成' : i === index ? '进行中' : '等待';
    });
    if (fill) fill.style.width = `${Math.max(8, Math.round(((index + 1) / steps.length) * 88))}%`;
    const label = steps[index]?.[1] || '';
    if (note) note.textContent = `后台操作：${label}…`;
  };
  const timer = setInterval(advance, endpoint === 'chat' ? 1800 : 450);
  const clockTimer = setInterval(() => {
    if (clock && target.isConnected) clock.textContent = `${((performance.now() - started) / 1000).toFixed(1)}s`;
  }, 200);
  const clear = () => { clearInterval(timer); clearInterval(clockTimer); };
  return {
    finish() {
      clear();
      if (fill) fill.style.width = '100%';
      stepEls.forEach((el) => { el.classList.remove('active'); el.classList.add('done');
        const state = el.querySelector('.kb-progress-state'); if (state) state.textContent = '完成'; });
    },
    fail() {
      clear();
      const active = target.querySelector('.kb-progress-step.active');
      if (active) { active.classList.add('failed'); const state = active.querySelector('.kb-progress-state');
        if (state) state.textContent = '失败'; }
    },
  };
}


function statusBadge(status) {
  return `<span class="badge ${STATUS_BADGE[status] || ''}">${esc(STATUS_LABEL[status] || status || '—')}</span>`;
}

function jobBadge(status) {
  const cls = status === 'completed' ? 'ok' : status === 'failed' ? 'danger'
    : status === 'partial' ? 'warn' : '';
  return `<span class="badge ${cls}">${esc(JOB_LABEL[status] || status || '—')}</span>`;
}

function scoreText(value) {
  const n = Number(value || 0);
  return n > 1 ? `${Math.round(n)} 分` : `${Math.round(n * 100)}%`;
}

function versionLabel(value) {
  if (typeof value === 'string') return value;
  return value?.version ? `v${value.version}` : '';
}

function citationChips(citations, caseMap) {
  return (citations || []).map((citation) => {
    const item = caseMap[citation];
    const href = item ? `#/kb/docs/${encodeURIComponent(item.doc_id)}` : '';
    return href
      ? `<a class="kb-cite" href="${href}" title="${esc(item.title)}">${esc(citation)}</a>`
      : `<span class="kb-cite muted">${esc(citation)}</span>`;
  }).join(' ');
}

function renderResult(data) {
  const cases = data.cases || [];
  const caseMap = Object.fromEntries(cases.map((c) => [c.citation, c]));
  const libNames = data.library_names || {};
  const multiLibrary = (data.kb_ids || []).length > 1 || Number(data.libraries || 0) > 1;
  const noAnswer = data.no_answer;
  const parts = [];
  if (noAnswer) {
    parts.push(`<div class="alert warn">${icon('alert')}<div><strong>未找到足够相关的历史案例。</strong>
      <div class="tiny" style="margin-top:4px">${esc(data.answer || '')}</div></div></div>`);
  } else {
    parts.push(`<div class="card kb-answer">
      <div class="card-head"><h2>知识库回答</h2>
        <span class="badge ${data.degraded ? 'warn' : 'ok'}">${data.degraded ? '本地检索摘要' : '模型综合'}</span>
        <span class="tiny muted" style="margin-left:auto">${data.model ? `模型 ${esc(data.model)}` : '未调用模型'} · 置信度 ${scoreText(data.confidence)}</span>
      </div>
      <div class="card-body">
        <div class="kb-answer-text">${esc(data.answer || '').replace(/\n/g, '<br>')}</div>
        ${(data.conclusions || []).length ? `<div class="kb-subhead">结论</div>
          <ul class="kb-points">${data.conclusions.map((item) => `<li>${esc(item.text)} ${citationChips(item.citations, caseMap)}</li>`).join('')}</ul>` : ''}
        ${(data.steps || []).length ? `<div class="kb-subhead">排查/处理步骤</div>
          <ol class="kb-points">${data.steps.map((item) => `<li>${esc(item)}</li>`).join('')}</ol>` : ''}
        ${(data.cautions || []).length ? `<div class="kb-subhead">风险提示</div>
          <ul class="kb-points">${data.cautions.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}
      </div>
    </div>`);
  }
  if (cases.length) {
    parts.push(`<div class="card" style="margin-top:14px">
      <div class="card-head"><h2>引用案例</h2><span class="sub">共 ${cases.length} 个；点击打开完整记录</span></div>
      <div class="card-body kb-case-list">
        ${cases.map((c) => `<a class="kb-case-card" href="#/kb/docs/${encodeURIComponent(c.doc_id)}">
          <div class="inline"><span class="kb-cite">${esc(c.citation)}</span>
            <strong>${esc(c.title)}</strong>${multiLibrary ? `<span class="badge">${esc(libNames[c.kb_id] || `库 ${c.kb_id || '—'}`)}</span>` : ''}<span class="badge info" style="margin-left:auto">${scoreText(c.score)}</span></div>
          <div class="tiny muted" style="margin-top:6px">${esc((c.boards || []).join('、') || '未识别板卡')} · ${esc((c.domains || []).join('、') || '未识别领域')} ${c.versions?.length ? '· ' + esc(c.versions.join('、')) : ''}</div>
          <div class="kb-snippet"><span>现象</span>${esc(c.symptom || '—')}</div>
          <div class="kb-snippet"><span>原因</span>${esc(c.cause || '—')}</div>
          <div class="kb-snippet"><span>方案</span>${esc(c.solution || '—')}</div>
        </a>`).join('')}
      </div>
    </div>`);
  }
  if (data.analysis) {
    const a = data.analysis;
    parts.push(`<details class="card kb-analysis"><summary>本次检索如何理解问题</summary>
      <div class="card-body tiny muted">
        意图：${esc(a.intent || '未识别')}；板卡：${esc((a.boards || []).join('、') || '—')}；
        领域：${esc((a.domains || []).join('、') || '—')}；版本：${esc((a.versions || []).join('、') || '—')}；
        扩展词：${esc((a.expansions || []).join('、') || '—')}
      </div></details>`);
  }
  return parts.join('') + kbRecordFooter(data);
}

function kbRecordFooter(data) {
  if (!data?.query_id) return '';
  return `<div class="kb-record-note tiny muted">${icon('list')} 本次检索已写入任务记录
    <span class="mono">${esc(data.query_id)}</span>
    <a href="#/tasks?tab=kb">查看知识库操作 →</a></div>`;
}

async function loadFacets(kbId = 1) {
  try { return await api(`/api/kb/facets?kb=${encodeURIComponent(kbId)}`); }
  catch { return { boards: [], domains: [] }; }
}

function boardOptions(facets) {
  return `<option value="">全部板卡</option>${(facets.boards || []).map(
    (x) => `<option value="${esc(x.value)}">${esc(x.value)}（${x.count}）</option>`).join('')}`;
}

function domainOptions(facets) {
  return `<option value="">全部领域</option>${(facets.domains || []).map(
    (x) => `<option value="${esc(x.value)}">${esc(x.value)}（${x.count}）</option>`).join('')}`;
}

const KB_CHAT_STATUS = {
  pending: '排队中', running: '检索中', done: '已完成', error: '失败',
};
const KB_MODEL_STATE = {
  timeout: '模型调用超时', no_model: '未配置可用模型', error: '模型调用失败',
  no_answer: '未命中案例',
};
const KB_REVIEW_STATUS_LABEL = {
  pass: '审核通过', reject: '审核驳回', unreviewed: '待审核', pending: '待审核',
};
const KB_REV_ACTION_LABEL = {
  import: '导入', update: '编辑', status: '状态变更', restore: '恢复', delete: '删除',
  review: '审核', split: '拆分', merge: '合并', derive: '派生',
};

function kbChatProgressHtml(progress, status = 'running') {
  const p = progress || {};
  const failed = status === 'error' || p.failed;
  const steps = p.steps || [];
  return `<div class="kb-chat-progress ${failed ? 'failed' : ''}">
    <div class="inline"><strong>${failed ? '检索中断' : '正在检索'}</strong>
      <span class="badge ${failed ? 'danger' : 'warn'}">${esc(p.label || '准备中')}</span>
      <span class="badge info" style="margin-left:auto">${Number(p.percent || 0)}%</span></div>
    <div class="kb-meter-track" style="margin-top:8px"><div class="kb-meter-fill" style="width:${Math.max(4, Math.min(100, Number(p.percent || 0)))}%"></div></div>
    <div class="kb-progress-steps">${steps.map((s) => `<span class="kb-step ${esc(s.status || 'waiting')}">
      ${s.status === 'done' ? '✓' : s.status === 'failed' ? '!' : '·'} ${esc(s.label)}</span>`).join('')}</div>
    ${p.detail?.error ? `<div class="tiny muted" style="margin-top:6px">${esc(String(p.detail.error).slice(0, 300))}</div>` : ''}
    ${p.elapsed_ms ? `<div class="tiny muted" style="margin-top:4px">已用时 ${(Number(p.elapsed_ms) / 1000).toFixed(1)}s；刷新页面后会继续显示服务端进度。</div>` : ''}
  </div>`;
}

function kbModelNotice(data) {
  const status = data.model_status || {};
  if (!status.state || status.state === 'ok' || status.state === 'no_answer') return '';
  const label = KB_MODEL_STATE[status.state] || '模型降级';
  const raw = String(status.message || '').trim();
  // 兼容 1.34 早期写入的历史消息：message 本身重复了标题和降级说明。
  const detail = raw && !raw.startsWith(label) ? `：${esc(raw.slice(0, 300))}` : '';
  const cost = status.latency_ms ? `，耗时 ${(Number(status.latency_ms) / 1000).toFixed(1)}s` : '';
  const trace = data.trace_id && state.user?.role === 'admin'
    ? ` <a href="#/traces/${encodeURIComponent(data.trace_id)}">查看执行轨迹 →</a>` : '';
  return `<div class="alert warn kb-model-note">${icon('alert')}<div><strong>${label}</strong>${detail}。
    已降级为本地检索摘要${cost}。${trace}</div></div>`;
}

function kbChatMessageHtml(m) {
  const status = m.status || 'done';
  if (m.role === 'user') {
    return `<div class="kb-msg user"><div class="kb-msg-bubble">${esc(m.query || m.content || '').replace(/\n/g, '<br>')}</div>
      <div class="tiny muted">${esc(m.created_at ? fmtDate(m.created_at) : '')}</div></div>`;
  }
  if (status === 'pending' || status === 'running') {
    return `<div class="kb-msg assistant" data-mid="${esc(m.id)}"><div class="card">
      <div class="card-body">${kbChatProgressHtml(m.progress, status)}</div></div></div>`;
  }
  if (status === 'error') {
    return `<div class="kb-msg assistant" data-mid="${esc(m.id)}"><div class="alert danger">${icon('alert')}<div>
      <strong>检索失败：${esc(m.error_message || '未知错误')}</strong>
      <div class="tiny muted">错误码 ${esc(m.error_code || '—')}${m.latency_ms ? ` · 耗时 ${(Number(m.latency_ms) / 1000).toFixed(1)}s` : ''}</div>
      <div class="inline" style="margin-top:8px">
        <button class="btn btn-sm" data-act="chat-retry" data-query="${esc(m.query || '')}">重新检索</button>
        ${state.user?.role === 'admin' ? `<a class="btn btn-sm" href="#/traces">查看执行轨迹</a>` : ''}
      </div></div></div></div>`;
  }
  const payload = m.payload && Object.keys(m.payload).length
    ? m.payload : { answer: m.content, query_id: m.query_id, degraded: true, cases: [] };
  const meta = [m.created_at ? fmtDate(m.created_at) : '', m.model, m.latency_ms ? `${(Number(m.latency_ms) / 1000).toFixed(1)}s` : '']
    .filter(Boolean).join(' · ');
  return `<div class="kb-msg assistant" data-mid="${esc(m.id)}">
    <div class="kb-msg-meta tiny muted">${esc(meta)}</div>
    ${kbModelNotice(payload)}${renderResult(payload)}</div>`;
}

function kbChatListHtml(chats, activeId) {
  if (!chats.length) return '<div class="empty small">还没有历史对话。提问一次就会自动保存。</div>';
  return chats.map((c) => `<div class="kb-chat-item ${c.id === activeId ? 'active' : ''}" data-session="${esc(c.id)}">
    <div class="kb-chat-title">${esc(c.title || '新对话')}</div>
    <div class="inline" style="gap:6px"><span class="tiny muted">${esc(c.updated_at ? fmtDate(c.updated_at) : '')}</span>
      ${c.running_message_id ? '<span class="badge warn">进行中</span>' : ''}
      <span class="tiny muted" style="margin-left:auto">${Number(c.message_count || 0)} 条</span></div>
    ${c.last_message?.content ? `<div class="tiny muted kb-chat-preview">${esc(c.last_message.content)}</div>` : ''}
    <button class="btn btn-sm btn-ghost kb-chat-del" data-act="del-chat" data-id="${esc(c.id)}" title="删除对话">删除</button>
  </div>`).join('');
}

export async function renderKb() {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  let kbId = Math.max(0, Number(params.get('kb') || 0));
  const view = mount('故障问答', '历史对话与检索进度保存在服务端；刷新后可继续查看', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let status;
  let facets;
  let recent = [];
  let chats = [];
  try {
    const statusResp = await api(`/api/kb/status${kbId ? `?kb=${kbId}` : ''}`);
    status = statusResp.status;
    kbId = kbId || Number(status.kb_id || 1);
    const [facetData, recentResp, chatResp] = await Promise.all([
      loadFacets(kbId),
      api(`/api/kb/docs?size=6&status=active&kb=${kbId}`).catch(() => ({ items: [] })),
      api(`/api/kb/chats?limit=30&kb=${kbId}`).catch(() => ({ items: [] })),
    ]);
    facets = facetData;
    recent = recentResp.items || [];
    chats = chatResp.items || [];
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  if (!status.enabled) {
    view.innerHTML = `<div class="alert warn">${icon('alert')}<div>故障知识库已在运行时配置中关闭。</div></div>`;
    return;
  }
  const empty = Number(status.documents || 0) === 0;
  const activeLibraries = (status.libraries || []).filter((x) => x.status === 'active');
  let savedCrossIds = [];
  try { savedCrossIds = JSON.parse(localStorage.getItem('kb.cross.ids') || '[]'); } catch {}
  if (!Array.isArray(savedCrossIds)) savedCrossIds = [];
  const paramCrossIds = String(params.get('kb_ids') || '').split(',').map((x) => Number(x)).filter((x) => x > 0);
  const initialCrossIds = [...new Set((paramCrossIds.length ? paramCrossIds : savedCrossIds)
    .filter((x) => activeLibraries.some((lib) => Number(lib.id) === Number(x))))].slice(0, 5);
  if (!initialCrossIds.length && activeLibraries.length) initialCrossIds.push(Number(status.kb_id || 1));
  const crossChoices = activeLibraries.map((lib) => `<label class="check tiny"><input type="checkbox" class="kb-cross-option" value="${esc(lib.id)}" ${initialCrossIds.includes(Number(lib.id)) ? 'checked' : ''}> ${esc(lib.name)}</label>`).join('');
  const libraryOptions = activeLibraries
    .map((x) => `<option value="${esc(x.id)}" ${Number(x.id) === Number(status.kb_id || 1) ? 'selected' : ''}>${esc(x.name)} · ${x.documents || 0} 文档</option>`).join('')
    || '<option value="1">故障知识库</option>';

  let sessionId = String(params.get('session') || '');
  let session = chats.find((x) => x.id === sessionId) || null;
  let messages = [];
  if (!sessionId && chats.length) sessionId = chats[0].id;
  if (sessionId) {
    try {
      const detail = await api(`/api/kb/chats/${encodeURIComponent(sessionId)}`);
      session = detail.session;
      messages = detail.messages || [];
    } catch {
      sessionId = '';
      session = null;
      messages = [];
    }
  }

  view.innerHTML = `
    <div class="card"><div class="card-body inline" style="gap:12px;flex-wrap:wrap">
      <label class="small" for="kb-library-select">知识库</label>
      <select class="input" id="kb-library-select" style="max-width:260px">${libraryOptions}</select>
      <label class="check"><input type="checkbox" id="kb-cross" ${params.get('cross') === '1' ? 'checked' : ''}> 跨库检索（融合排序）</label>
      <span class="tiny muted">专家库、语料库与故障库逻辑隔离；跨库检索只合并结果，不合并数据。</span>
      <div id="kb-cross-scope" class="kb-cross-scope" style="display:${params.get('cross') === '1' ? 'flex' : 'none'};flex-basis:100%;gap:10px;flex-wrap:wrap;align-items:center">
        <span class="tiny muted">检索范围</span>${crossChoices || '<span class="tiny muted">没有可见知识库</span>'}
        <span class="tiny muted" id="kb-cross-count">最多 5 个库</span>
      </div>
    </div></div>
    <div class="grid grid-4 kb-stats">
      <div class="card stat"><div><div class="stat-val">${status.documents || 0}</div>
        <div class="stat-label">生效文档</div><div class="stat-note">${status.cases || 0} 个可引用案例</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.index?.terms || 0}</div>
        <div class="stat-label">索引词项</div><div class="stat-note">重建 ${status.index?.build_ms || 0} ms · ${esc(status.index?.status || 'empty')}</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.drafts || 0}</div>
        <div class="stat-label">待审核</div><div class="stat-note">工程师导入后进入审核队列</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.top_k || 6}</div>
        <div class="stat-label">默认引用上限</div><div class="stat-note">${status.index?.bytes ? `${Math.round(status.index.bytes / 1024)} KB 索引` : '无模型也可检索'}</div></div></div>
    </div>

    <div class="kb-chat-layout">
      <aside class="card kb-chat-side">
        <div class="card-head"><h2>历史对话</h2>
          <span class="sub" id="kb-chat-count">${chats.length} 条</span></div>
        <div class="kb-chat-list" id="kb-chat-list">${kbChatListHtml(chats, sessionId)}</div>
        <div class="card-body tiny muted kb-chat-tip">对话、进度和回答都保存在服务端。刷新页面或换设备后继续查看，不会丢失。</div>
      </aside>
      <section class="kb-chat-main">
        <div class="card kb-chat-top"><div class="card-body inline" style="gap:10px">
          <div style="min-width:0"><strong id="kb-chat-title">${esc(session?.title || '新对话')}</strong>
            <div class="tiny muted" id="kb-chat-meta">${messages.length ? `${messages.length} 条消息` : '提问后自动保存'}</div></div>
          <span style="margin-left:auto"></span>
          <button class="btn btn-sm" type="button" data-act="new-chat">＋ 新对话</button>
        </div></div>
        <div class="kb-chat-messages" id="kb-chat-messages">${messages.length
          ? messages.map(kbChatMessageHtml).join('')
          : emptyState('inbox', '还没有对话', '描述一次现场故障，回答、引用案例和检索进度都会保存在这里。')}</div>
        <div id="kb-search-result"></div>
        <div class="card kb-ask-card">
          <div class="card-head"><h2>描述故障</h2><span class="sub">Enter 发送 · Shift+Enter 换行</span></div>
          <div class="card-body">
            ${empty ? `<div class="alert info">${icon('info')}<div>知识库还没有数据。管理员可以到
              <a href="#/kb/docs?tab=import">导入页</a> 导入 CSV/TXT/MD，或先载入示例数据。</div></div>` : ''}
            <div class="form-row"><label for="kb-query">故障现象 / 问题</label>
              <textarea class="input kb-query" id="kb-query" rows="3" maxlength="2000"
                placeholder="例如：VBPE2 单板每 2~3 分钟复位一次，V4.30.20 升级后出现，网管有看门狗告警"></textarea>
              <div class="tiny muted">建议写：板卡型号 + 软件/逻辑版本 + 告警码 + 现象 + 已做过的操作。</div></div>
            <div class="form-row"><label>可选过滤</label>
              <div class="inline kb-filters">
                <select class="input" id="kb-board">${boardOptions(facets)}</select>
                <select class="input" id="kb-domain">${domainOptions(facets)}</select>
                <button class="btn btn-primary" type="button" data-act="ask">${icon('search')} 检索并回答</button>
                <button class="btn" type="button" data-act="search">只检索案例</button>
              </div>
            </div>
            <div class="inline kb-examples">
              <span class="tiny muted">示例：</span>
              ${['看门狗复位', '1588 时钟失锁', 'SSD 只读', 'SFP 收光低', 'EPLD 升级失败'].map(
                (x) => `<button class="btn btn-sm btn-ghost" type="button" data-act="example" data-text="${esc(x)}">${esc(x)}</button>`).join('')}
            </div>
          </div>
        </div>
      </section>
    </div>

    ${recent.length ? `<div class="card" style="margin-top:14px">
      <div class="card-head"><h2>最近入库</h2><a class="tiny" href="#/kb/docs?tab=docs" style="margin-left:auto">查看全部</a></div>
      <div class="card-body kb-recent">${recent.map((d) => `<a class="kb-recent-row" href="#/kb/docs/${encodeURIComponent(d.id)}">
        <span>${esc(d.title)}</span><span class="tiny muted">${esc((d.boards || []).join('、') || '—')} · ${d.case_count || 0} 案例</span>
      </a>`).join('')}</div></div>` : ''}
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>回答边界：</strong>知识库回答只基于检索到的历史案例；无模型或模型超时会明确标注并显示本地摘要。
      历史案例不是厂商正式公告，执行前需结合当前版本、现场日志和变更记录复核。
    </div></div>`;

  const crossToggle = view.querySelector('#kb-cross');
  const crossScope = view.querySelector('#kb-cross-scope');
  const crossCount = view.querySelector('#kb-cross-count');
  const listEl = view.querySelector('#kb-chat-list');
  const msgEl = view.querySelector('#kb-chat-messages');
  const titleEl = view.querySelector('#kb-chat-title');
  const metaEl = view.querySelector('#kb-chat-meta');
  let pollTimer = null;

  const selectedCross = () => [...view.querySelectorAll('.kb-cross-option:checked')]
    .map((x) => Number(x.value)).filter((x) => x > 0);
  const currentKb = () => Number(view.querySelector('#kb-library-select')?.value || kbId);
  const syncCross = () => {
    if (crossCount) crossCount.textContent = `已选 ${selectedCross().length} / 最多 5 个`;
    try { localStorage.setItem('kb.cross.ids', JSON.stringify(selectedCross())); } catch {}
  };
  const stopPoll = () => {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  };
  const renderChatList = () => {
    if (!listEl) return;
    listEl.innerHTML = kbChatListHtml(chats, sessionId);
    const countEl = view.querySelector('#kb-chat-count');
    if (countEl) countEl.textContent = `${chats.length} 条`;
  };
  const renderChatHeader = () => {
    if (titleEl) titleEl.textContent = session?.title || '新对话';
    if (metaEl) metaEl.textContent = messages.length
      ? `${messages.length} 条消息 · ${session?.updated_at ? fmtDate(session.updated_at) : ''}`
      : '提问后自动保存';
  };
  const renderChatMessages = (scroll = false) => {
    if (!msgEl) return;
    msgEl.innerHTML = messages.length
      ? messages.map(kbChatMessageHtml).join('')
      : emptyState('inbox', '还没有对话', '描述一次现场故障，回答、引用案例和检索进度都会保存在这里。');
    if (scroll) msgEl.scrollTop = msgEl.scrollHeight;
  };
  const syncHash = () => {
    const parts = [`kb=${currentKb()}`];
    if (sessionId) parts.push(`session=${encodeURIComponent(sessionId)}`);
    if (crossToggle?.checked) {
      parts.push('cross=1', `kb_ids=${selectedCross().join(',')}`);
    }
    history.replaceState(null, '', `#/kb?${parts.join('&')}`);
  };
  const refreshChats = async () => {
    try {
      const r = await api(`/api/kb/chats?limit=30&kb=${currentKb()}`);
      chats = r.items || [];
      renderChatList();
    } catch {}
  };
  const pollMessage = (mid) => {
    stopPoll();
    const tick = async () => {
      if (!view.isConnected || !sessionId) return;
      try {
        const message = (await api(`/api/kb/chats/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(mid)}`)).message;
        const index = messages.findIndex((x) => x.id === mid);
        if (index >= 0) messages[index] = message;
        renderChatMessages(true);
        if (['pending', 'running'].includes(message.status)) {
          pollTimer = setTimeout(tick, 1200);
        } else {
          pollTimer = null;
          refreshChats();
        }
      } catch {
        if (view.isConnected) pollTimer = setTimeout(tick, 2000);
      }
    };
    tick();
  };
  const loadSession = async (sid) => {
    stopPoll();
    try {
      const detail = await api(`/api/kb/chats/${encodeURIComponent(sid)}`);
      sessionId = sid;
      session = detail.session;
      messages = detail.messages || [];
      renderChatList();
      renderChatHeader();
      renderChatMessages(true);
      syncHash();
      if (detail.running_message_id) pollMessage(detail.running_message_id);
    } catch (err) {
      toast(err.message, 'error');
    }
  };
  const newChat = async () => {
    if (sessionId && !messages.length) {
      view.querySelector('#kb-query')?.focus();
      return;
    }
    try {
      const r = await api('/api/kb/chats', { method: 'POST', body: { kb_id: currentKb() } });
      chats.unshift(r.session);
      sessionId = r.session.id;
      session = r.session;
      messages = [];
      stopPoll();
      renderChatList();
      renderChatHeader();
      renderChatMessages();
      syncHash();
      view.querySelector('#kb-query')?.focus();
    } catch (err) {
      toast(err.message, 'error');
    }
  };
  const chatPayload = () => {
    const filters = {};
    const board = view.querySelector('#kb-board')?.value || '';
    const domain = view.querySelector('#kb-domain')?.value || '';
    if (board) filters.board = board;
    if (domain) filters.domain = domain;
    let kbIds = [currentKb()];
    if (crossToggle?.checked) {
      const checked = selectedCross();
      if (checked.length > 5) throw new Error('跨库检索最多选择 5 个知识库');
      kbIds = checked.length ? checked : [currentKb()];
    }
    return { filters, kbIds };
  };
  const ensureSession = async () => {
    if (sessionId) return;
    const r = await api('/api/kb/chats', { method: 'POST', body: { kb_id: currentKb() } });
    chats.unshift(r.session);
    sessionId = r.session.id;
    session = r.session;
    messages = [];
    renderChatList();
    renderChatHeader();
    syncHash();
  };
  const ask = async (button) => {
    const query = view.querySelector('#kb-query')?.value.trim() || '';
    if (!query) { toast('请先描述故障现象', 'error'); return; }
    if (pollTimer) { toast('当前检索仍在进行，进度会继续显示', 'warn'); return; }
    let payload;
    try { payload = chatPayload(); } catch (err) { toast(err.message, 'error'); return; }
    setBusy(button, true, '提交中…');
    const field = view.querySelector('#kb-query');
    try {
      await ensureSession();
      const res = await api(`/api/kb/chats/${encodeURIComponent(sessionId)}/messages`, {
        method: 'POST', body: { query, filters: payload.filters, kb_ids: payload.kbIds },
      });
      messages.push(res.user_message, res.assistant_message);
      session = res.session || session;
      const index = chats.findIndex((x) => x.id === sessionId);
      if (index >= 0) {
        chats[index] = { ...chats[index], ...session, message_count: messages.length, running_message_id: res.assistant_message.id };
      }
      renderChatList();
      renderChatHeader();
      renderChatMessages(true);
      if (field) field.value = '';
      syncHash();
      pollMessage(res.assistant_message.id);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(button, false);
    }
  };
  const searchOnly = async (button) => {
    const query = view.querySelector('#kb-query')?.value.trim() || '';
    if (!query) { toast('请先描述故障现象', 'error'); return; }
    let payload;
    try { payload = chatPayload(); } catch (err) { toast(err.message, 'error'); return; }
    const board = payload.filters.board || '';
    const domain = payload.filters.domain || '';
    setBusy(button, true, '检索中…');
    try {
      const data = await api(`/api/kb/search?q=${encodeURIComponent(query)}&board=${encodeURIComponent(board)}&domain=${encodeURIComponent(domain)}&limit=10&kb_ids=${payload.kbIds.join(',')}`);
      const items = data.items || [];
      const multi = payload.kbIds.length > 1;
      const box = view.querySelector('#kb-search-result');
      box.innerHTML = items.length ? `<div class="card" style="margin-top:12px">
        <div class="card-head"><h2>只检索结果</h2><span class="sub">${data.no_answer ? '低于拒答阈值，仅供参考' : `${items.length} 个案例`}${multi ? ` · ${payload.kbIds.length} 个库` : ''} · 未写入对话</span></div>
        <div class="card-body kb-case-list">${items.map((c, i) => `<a class="kb-case-card" href="#/kb/docs/${encodeURIComponent(c.doc_id)}">
          <div class="inline"><span class="kb-cite">K${i + 1}</span><strong>${esc(c.title)}</strong>
            <span class="badge info" style="margin-left:auto">${scoreText(c.score)}</span></div>
          <div class="kb-snippet"><span>现象</span>${esc(c.symptom || '—')}</div>
          <div class="kb-snippet"><span>原因</span>${esc(c.cause || '—')}</div>
          <div class="kb-snippet"><span>方案</span>${esc(c.solution || '—')}</div></a>`).join('')}</div></div>`
        : `<div class="alert warn" style="margin-top:12px">${icon('alert')}<div>${esc(data.reason || '没有命中案例')}</div></div>`;
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(button, false);
    }
  };

  crossToggle?.addEventListener('change', () => {
    if (crossScope) crossScope.style.display = crossToggle.checked ? 'flex' : 'none';
    if (crossToggle.checked && !selectedCross().length) {
      const current = String(currentKb());
      const cb = [...view.querySelectorAll('.kb-cross-option')].find((x) => x.value === current);
      if (cb) cb.checked = true;
    }
    syncCross();
    syncHash();
  });
  view.querySelectorAll('.kb-cross-option').forEach((cb) => cb.addEventListener('change', () => {
    if (selectedCross().length > 5) {
      cb.checked = false;
      toast('跨库检索最多选择 5 个知识库', 'error');
    }
    syncCross();
    syncHash();
  }));
  syncCross();

  listEl?.addEventListener('click', (event) => {
    if (event.target.closest('[data-act="del-chat"]')) return;
    const item = event.target.closest('[data-session]');
    if (item) loadSession(item.dataset.session);
  });
  view.querySelector('#kb-library-select')?.addEventListener('change', (e) => {
    location.hash = `#/kb?kb=${encodeURIComponent(e.target.value)}`;
  });
  view.querySelector('#kb-query')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      view.querySelector('[data-act="ask"]')?.click();
    }
  });

  bindActions(view, {
    ask: (el) => ask(el),
    search: (el) => searchOnly(el),
    example: (el) => { view.querySelector('#kb-query').value = el.dataset.text; view.querySelector('#kb-query').focus(); },
    'new-chat': () => newChat(),
    'chat-retry': (el) => {
      const field = view.querySelector('#kb-query');
      if (field) field.value = el.dataset.query || '';
      ask(view.querySelector('[data-act="ask"]'));
    },
    'del-chat': async (el) => {
      const id = el.dataset.id;
      const chat = chats.find((x) => x.id === id);
      if (!(await confirmDialog(`删除对话“${chat?.title || id}”？消息记录会一并隐藏，无法恢复。`, { danger: true, okText: '删除' }))) return;
      setBusy(el, true, '删除中…');
      try {
        await api(`/api/kb/chats/${encodeURIComponent(id)}`, { method: 'DELETE' });
        chats = chats.filter((x) => x.id !== id);
        if (sessionId === id) {
          stopPoll();
          sessionId = '';
          session = null;
          messages = [];
          renderChatHeader();
          renderChatMessages();
          syncHash();
        }
        renderChatList();
        toast('对话已删除', 'ok');
      } catch (err) {
        toast(err.message, 'error');
        setBusy(el, false);
      }
    },
    sample: async (el) => {
      if (!(await confirmDialog('载入 20 条脱敏示例案例？示例只用于演示检索，不代表现场结论。'))) return;
      setBusy(el, true, '载入中…');
      try {
        const r = await api('/api/kb/sample', { method: 'POST', body: { kb_id: kbId } });
        toast(`已载入 ${r.documents || 0} 个文档`, 'ok');
        renderKb();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });

  renderChatList();
  renderChatHeader();
  const running = messages.find((m) => m.role === 'assistant'
    && ['pending', 'running'].includes(m.status));
  if (running) pollMessage(running.id);
  return view;
}

const KB_TABS = [
  ['overview', '概览'], ['stats', '统计'], ['docs', '文档'], ['import', '导入'],
  ['review', '审核'], ['audit', '变更与审计'], ['terms', '词典'],
  ['libraries', '知识库'], ['dedupe', '近重复'], ['eval', '评测'], ['settings', '设置'],
];

function kbLibraryBar(status, kbId = 1) {
  const items = (status.libraries || []).filter((x) => x.status === 'active');
  if (!items.length) return '';
  return `<div class="card" style="margin-top:10px"><div class="card-body inline" style="gap:10px;flex-wrap:wrap">
    <span class="small muted">当前知识库</span>
    <select class="input" id="kb-library-switch" style="max-width:280px">
      ${items.map((x) => `<option value="${esc(x.id)}" ${Number(x.id) === Number(kbId) ? 'selected' : ''}>${esc(x.name)} · ${x.documents || 0} 文档 / ${x.cases || 0} 案例</option>`).join('')}
    </select>
    <a class="btn btn-sm" href="#/kb/docs?tab=libraries">${icon('layers')} 管理知识库</a>
    <span class="tiny muted">逻辑隔离；每库独立索引与导入任务。</span>
  </div></div>`;
}

const REVIEW_STATUS = {
  queued: '排队中', running: '审核中', done: '已完成', manual: '转人工',
  failed: '失败', decided: '已终审',
};
const REVIEW_STATUS_BADGE = { queued: 'warn', running: 'warn', done: 'ok', manual: 'warn',
  failed: 'danger', decided: 'ok' };

function kbTabNav(active, kbId = 1) {
  const suffix = Number(kbId) && Number(kbId) !== 1 ? `&kb=${Number(kbId)}` : '';
  return `<div class="kb-tabbar">${KB_TABS.map(([key, label]) =>
    `<a class="kb-tab ${key === active ? 'active' : ''}" href="#/kb/docs?tab=${key}${suffix}">${label}</a>`).join('')}</div>`;
}

function reviewBadge(status) {
  return `<span class="badge ${REVIEW_STATUS_BADGE[status] || ''}">${esc(REVIEW_STATUS[status] || status || '—')}</span>`;
}

function trendBars(trend, days = 30) {
  const rows = (trend || []).slice(-Math.max(1, Number(days || 30)));
  const max = Math.max(1, ...rows.map((x) => Number(x.queries || 0)));
  return `<div class="kb-trend">${rows.map((x) => `<div class="kb-trend-col" title="${esc(x.day)} 查询 ${x.queries}">
    <div class="kb-trend-bar" style="height:${Math.max(2, Math.round((Number(x.queries || 0) / max) * 100))}%"></div>
    <span class="tiny muted">${esc(String(x.day).slice(5))}</span></div>`).join('')}</div>`;
}

function miniBars(items, key = 'n') {
  const rows = items || [];
  const max = Math.max(1, ...rows.map((x) => Number(x[key] || 0)));
  return rows.slice(0, 10).map((x) => `<div class="kb-meter"><span class="tiny">${esc(x.query || x.title || x.day || '')}</span>
    <div class="kb-meter-track"><div class="kb-meter-fill" style="width:${Math.round((Number(x[key] || 0) / max) * 100)}%"></div></div>
    <span class="mono tiny">${x[key] || 0}</span></div>`).join('');
}

function kbReloadTab(tab) {
  location.hash = `#/kb/docs?tab=${encodeURIComponent(tab)}&_=${Date.now()}`;
}

function kbDiffText(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map((x) => String(x)).join('、') || '—';
  return String(value);
}

function kbPreviewField(label, value) {
  if (!value) return '';
  const text = String(value);
  return `<div class="kb-snippet"><span>${label}</span>${esc(text.length > 300 ? `${text.slice(0, 300)}…` : text)}</div>`;
}

async function openDuplicateWizard(source, target, kbId) {
  let preview;
  try { preview = await api(`/api/kb/duplicates/preview?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}&kb=${kbId}`); }
  catch (err) { toast(err.message, 'error'); return; }
  const fields = preview.fields || [];
  const m = modal({
    title: '近重复合并预览',
    wide: true,
    body: `<div class="alert info">${icon('info')}<div>相似度 ${(preview.similarity * 100).toFixed(1)}%；建议 canonical：
      <strong>${esc(preview.suggested_canonical === preview.source.doc_id ? preview.source.title : preview.target.title)}</strong>。
      合并只把源案例并入目标，源文档标记 merged，不物理删除。</div></div>
      <div class="grid grid-2"><div class="card"><div class="card-body"><strong>源：${esc(preview.source.title)}</strong>
        <div class="tiny muted">${preview.source.case_count} 案例 · ${esc(preview.source.doc_id)}</div></div></div>
        <div class="card"><div class="card-body"><strong>目标：${esc(preview.target.title)}</strong>
        <div class="tiny muted">${preview.target.case_count} 案例 · ${esc(preview.target.doc_id)}</div></div></div></div>
      <div class="kb-subhead">逐字段对照（相同字段也保留，便于判断合并是否安全）</div>
      <div class="table-wrap kb-diff-table"><table class="data"><thead><tr><th>字段</th><th>源文档</th><th>目标文档</th><th>结论</th></tr></thead><tbody>
      ${fields.map((f) => `<tr><td><strong>${esc(f.field)}</strong></td>
        <td><pre class="kb-diff-text">${esc(kbDiffText(f.source))}</pre></td>
        <td><pre class="kb-diff-text">${esc(kbDiffText(f.target))}</pre></td>
        <td><span class="badge ${f.same ? 'ok' : 'warn'}">${f.same ? '一致' : '不同'}</span></td></tr>`).join('')
        || '<tr><td colspan="4" class="empty small">没有可对照字段</td></tr>'}
      </tbody></table></div>
      ${(preview.source_case_list || []).length ? `<div class="kb-subhead">源文档案例内容</div>
        <div class="kb-case-list">${preview.source_case_list.map((c) => `<div class="kb-case-card" style="cursor:default">
          <div class="inline"><strong>${esc(c.title)}</strong><span class="badge ${c.status === 'active' ? 'ok' : 'warn'}">${esc(c.status)}</span></div>
          ${kbPreviewField('现象', c.symptom)}${kbPreviewField('原因', c.cause)}${kbPreviewField('方案', c.solution)}${kbPreviewField('预防', c.prevention)}
        </div>`).join('')}</div>` : ''}
      ${(preview.target_case_list || []).length ? `<div class="kb-subhead">目标文档案例内容（合并后保留）</div>
        <div class="kb-case-list">${preview.target_case_list.map((c) => `<div class="kb-case-card" style="cursor:default">
          <div class="inline"><strong>${esc(c.title)}</strong><span class="badge ${c.status === 'active' ? 'ok' : 'warn'}">${esc(c.status)}</span></div>
          ${kbPreviewField('现象', c.symptom)}${kbPreviewField('原因', c.cause)}${kbPreviewField('方案', c.solution)}${kbPreviewField('预防', c.prevention)}
        </div>`).join('')}</div>` : ''}
      <div class="alert warn" style="margin-top:12px">${icon('alert')}<div>合并会迁移 ${preview.source.case_count} 个案例并追加 merge 修订；目标文档已有案例不会被覆盖。</div></div>`,
    footer: `<button class="btn" data-act="close">取消</button>
      <button class="btn btn-primary" data-act="merge">${icon('check')} 确认合并</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'close') { m.close(); return; }
    if (btn.dataset.act === 'merge') {
      const reason = prompt('合并原因（写入审计，可留空）') ?? null;
      if (reason === null) return;
      const token = `merge-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
      setBusy(btn, true, '合并中…');
      try {
        const r = await api('/api/kb/duplicates/merge', { method: 'POST', body: {
          source_doc_id: source, target_doc_id: target, kb_id: kbId, merge_token: token,
          reason: reason || '近重复合并向导' } });
        toast(`已合并 ${r.moved_cases || 0} 个案例`, 'ok'); m.close(); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

async function openReviewModal(id) {
  let review;
  try { review = (await api(`/api/kb/reviews/${encodeURIComponent(id)}`)).review; }
  catch (err) { toast(err.message, 'error'); return; }
  const dims = review.dimensions || [];
  const issues = review.issues || [];
  const patchText = Object.keys(review.suggested_patch || {}).length
    ? JSON.stringify(review.suggested_patch, null, 2) : '';
  const m = modal({
    title: `审核：${review.target_title || review.target_id}`,
    wide: true,
    body: `<div class="inline"><span class="badge ${review.status === 'done' ? 'ok' : 'warn'}">${esc(REVIEW_STATUS[review.status] || review.status)}</span>
      <span class="badge ${review.decision === 'pass' ? 'ok' : review.decision === 'reject' ? 'danger' : 'warn'}">${esc(review.decision || '待模型')}</span>
      <span class="mono tiny">${esc(review.model || '')}</span><span class="tiny muted" style="margin-left:auto">${review.score ? Math.round(review.score) + ' 分' : ''} · 置信度 ${Math.round((review.confidence || 0) * 100)}%</span></div>
      <div class="kb-subhead">六维评分</div>
      ${dims.map((d) => `<div class="kb-meter"><span class="tiny">${esc(d.label || d.key)}</span>
        <div class="kb-meter-track"><div class="kb-meter-fill" style="width:${Math.round((Number(d.score || 0) / Number(d.max || 1)) * 100)}%"></div></div>
        <span class="mono tiny">${d.score}/${d.max}</span></div>
        <div class="tiny muted">${esc(d.comment || '')}</div>`).join('') || '<div class="muted small">模型未返回评分</div>'}
      <div class="kb-subhead">问题</div>
      ${issues.length ? `<div class="kb-issues">${issues.map((i) => `<div class="kb-issue ${i.severity === 'blocker' ? 'danger' : ''}">
        <span class="badge ${i.severity === 'blocker' ? 'danger' : i.severity === 'warn' ? 'warn' : 'info'}">${esc(i.severity)}</span>
        <strong>${esc(i.code)}</strong> ${esc(i.field || '')}<div class="tiny muted">${esc(i.message)}${i.suggestion ? ` · 建议：${esc(i.suggestion)}` : ''}</div></div>`).join('')}</div>` : '<div class="muted small">未发现问题</div>'}
      ${patchText ? `<div class="kb-subhead">建议 patch（人工采纳后才写入）</div><pre class="kb-raw">${esc(patchText)}</pre>` : ''}
      ${review.error ? `<div class="alert warn" style="margin-top:10px">${esc(review.error)}</div>` : ''}`,
    footer: `<button class="btn" data-act="close">关闭</button>
      ${review.status !== 'decided' ? `<button class="btn" data-act="retry">${icon('refresh')} 重新审核</button>
      <button class="btn btn-danger" data-act="reject">驳回</button>
      ${patchText ? '<button class="btn" data-act="adopt">采纳 patch</button>' : ''}
      <button class="btn btn-primary" data-act="approve">通过并生效</button>` : ''}`,
  });
  m.mask.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn || btn.dataset.act === 'close') { if (btn) m.close(); return; }
    const act = btn.dataset.act;
    if (act === 'retry') {
      setBusy(btn, true, '审核中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/retry`, { method: 'POST', body: {} });
        toast('审核已重试', 'ok'); m.close(); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
      return;
    }
    if (act === 'reject') {
      const note = prompt('驳回原因（会写入审计）') ?? null;
      if (note === null) return;
      setBusy(btn, true, '驳回中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/decision`,
        { method: 'POST', body: { decision: 'reject', note } });
        toast('已驳回', 'ok'); m.close(); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
      return;
    }
    if (act === 'adopt') {
      if (!(await confirmDialog('采纳建议 patch？会先写入目标案例/草稿，再等待人工发布。'))) return;
      setBusy(btn, true, '应用中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/decision`,
        { method: 'POST', body: { decision: 'adopt_patch', apply_patch: true } });
        toast('patch 已采纳', 'ok'); m.close(); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
      return;
    }
    if (act === 'approve') {
      const note = prompt('终审备注（可留空）') ?? '';
      setBusy(btn, true, '发布中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/decision`,
        { method: 'POST', body: { decision: 'approve', note } });
        toast('已通过', 'ok'); m.close(); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

async function openDraft(id) {
  try {
    const data = (await api(`/api/kb/drafts/${encodeURIComponent(id)}`)).draft;
    const payload = data.payload || {};
    const doc = payload.document || {};
    const cases = payload.cases || [];
    const m = modal({
      title: `审核：${doc.title || data.title}`,
      wide: true,
      body: `<div class="alert info">${icon('info')}<div>批准后将写入知识库并重建索引。当前 ${cases.length} 个案例；原始文本请确认无敏感信息。</div></div>
        <div class="form-row" style="margin-top:12px"><label>标题</label><input class="input" id="kb-draft-title" value="${esc(doc.title || data.title)}"></div>
        <div class="form-row"><label>案例预览</label><div class="kb-case-list">${cases.map((c, i) => `<div class="kb-case-card"><strong>K${i + 1} ${esc(c.title)}</strong>
          <div class="kb-snippet"><span>原因</span>${esc(c.cause || '—')}</div><div class="kb-snippet"><span>方案</span>${esc(c.solution || '—')}</div></div>`).join('')}</div></div>
        <details><summary class="tiny muted">查看脱敏原文</summary><pre class="kb-raw">${esc(doc.masked_text || doc.raw_text || '')}</pre></details>`,
      footer: `<button class="btn" data-act="cancel">关闭</button>
        <button class="btn btn-primary" data-act="approve">批准入库</button>`,
    });
    m.mask.addEventListener('click', async (e) => {
      if (e.target.closest('[data-act="cancel"]')) m.close();
      const btn = e.target.closest('[data-act="approve"]');
      if (!btn) return;
      setBusy(btn, true, '批准中…');
      try {
        const title = m.mask.querySelector('#kb-draft-title').value.trim();
        if (title && title !== doc.title) await api(`/api/kb/drafts/${encodeURIComponent(id)}`, { method: 'PUT', body: { title } });
        await api(`/api/kb/drafts/${encodeURIComponent(id)}/approve`, { method: 'POST', body: {} });
        toast('已批准', 'ok'); m.close(); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    });
  } catch (err) { toast(err.message, 'error'); }
}

function selectedJobIds(view) {
  return [...view.querySelectorAll('.kb-job-check:checked')].map((el) => el.value).filter(Boolean);
}

async function batchJobAction(view, el, path, confirmText, busyText) {
  const ids = selectedJobIds(view);
  if (!ids.length) { toast('请先勾选导入任务', 'error'); return; }
  if (!(await confirmDialog(confirmText.replace('{n}', ids.length)))) return;
  setBusy(el, true, busyText);
  try {
    const r = await api(path, { method: 'POST', body: { job_ids: ids } });
    toast(`已处理 ${ids.length} 个任务`, 'ok');
    if (r.items?.some((x) => !x.ok)) {
      toast(r.items.filter((x) => !x.ok).map((x) => x.error).join('；'), 'error');
    }
    renderKbDocs();
  } catch (err) { toast(err.message, 'error'); }
  finally { setBusy(el, false); }
}

function renderImportPreview(boxes, preview, sheetName = "") {
  if (!boxes || !preview) return;
  const targets = [['', '— 忽略 —'], ['title', '标题'], ['symptom', '故障现象'], ['cause', '原因'],
    ['solution', '解决方案'], ['answer', '整段答案'], ['board', '板卡'], ['domain', '领域'],
    ['version', '版本'], ['tags', '标签']];
  const sheets = preview.sheets || [];
  const sheet = sheets.find((x) => x.sheet === sheetName) || sheets[0] || null;
  const headers = sheet ? sheet.headers || [] : preview.headers || [];
  const guessed = sheet ? sheet.guessed_mapping || {} : preview.guessed_mapping || {};
  const rows = sheet ? sheet.rows || [] : preview.rows || [];
  const sheetSelect = sheets.length > 1
    ? `<div class="form-row"><label>工作表</label><select class="input" id="kb-import-sheet" data-change="preview-sheet">
        ${sheets.map((x) => `<option value="${esc(x.sheet)}" ${x.sheet === (sheet?.sheet || '') ? 'selected' : ''}>${esc(x.sheet)}</option>`).join('')}
      </select><div class="tiny muted">切换工作表会重新猜测列映射；确认后再开始导入。</div></div>`
    : '';
  boxes.innerHTML = `${sheetSelect}
    <div class="kb-subhead">列映射</div>
    <div class="kb-map-grid">${headers.map((h) => `<label class="kb-map-field"><span class="kb-map-label" title="${esc(h)}">${esc(h)}</span>
      <select class="input kb-map-select" data-header="${esc(h)}">${targets.map(([v, label]) => `<option value="${v}" ${guessed[h] === v ? 'selected' : ''}>${label}</option>`).join('')}</select></label>`).join('')}</div>
    <div class="kb-subhead">前 20 行脱敏预览</div>
    <div class="table-wrap"><table class="data"><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>
      ${rows.slice(0, 5).map((row) => `<tr>${headers.map((h) => `<td class="tiny">${esc(row[h] || '')}</td>`).join('')}</tr>`).join('') || '<tr><td colspan="99" class="empty small">没有样本行</td></tr>'}
    </tbody></table></div><div class="tiny muted">编码 ${esc(preview.encoding || preview.format)} · 分隔符 ${esc(preview.delimiter || 'sheet')}</div>`;
}

function bindKbTabActions(view, kbId = 1) {
  bindActions(view, {
    'review-view': (el) => openReviewModal(el.dataset.id),
    'review-decision': (el) => openReviewModal(el.dataset.id),
    'review-batch': async (el) => {
      setBusy(el, true, '批量审核中…');
      try { const r = await api('/api/kb/reviews/run', { method: 'POST', body: { limit: 20 } });
        toast(`处理 ${r.processed} 条`, 'ok'); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'audit-filter': () => {
      const actor = view.querySelector('#kb-audit-actor')?.value.trim() || '';
      const action = view.querySelector('#kb-audit-action')?.value.trim() || '';
      location.hash = `#/kb/docs?tab=audit&actor=${encodeURIComponent(actor)}&action=${encodeURIComponent(action)}`;
    },
    'library-create': async (el) => {
      const name = view.querySelector('#kb-lib-name')?.value.trim();
      if (!name) { toast('请填写知识库名称', 'error'); return; }
      setBusy(el, true, '创建中…');
      try {
        await api('/api/kb/libraries', { method: 'POST', body: {
          name, kind: view.querySelector('#kb-lib-kind')?.value || 'fault',
          description: view.querySelector('#kb-lib-desc')?.value.trim() || '',
        } });
        toast('已创建知识库', 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'library-rename': async (el) => {
      const name = window.prompt('知识库新名称', el.dataset.name || '');
      if (name === null) return;
      const next = name.trim();
      if (!next) { toast('名称不能为空', 'error'); return; }
      setBusy(el, true, '保存中…');
      try {
        await api(`/api/kb/libraries/${encodeURIComponent(el.dataset.id)}`, { method: 'PUT', body: { name: next } });
        toast('已重命名', 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'library-visibility': async (el) => {
      const next = el.dataset.visibility === 'admin' ? 'all' : 'admin';
      if (!(await confirmDialog(next === 'admin'
        ? '设为仅管理员可见？工程师将不会在问答页看到该库。'
        : '设为全员可见？所有登录用户可在问答页选择该库。'))) return;
      setBusy(el, true, '保存中…');
      try {
        await api(`/api/kb/libraries/${encodeURIComponent(el.dataset.id)}`, { method: 'PUT', body: { visibility: next } });
        toast('已更新可见性', 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'library-default': async (el) => {
      if (!(await confirmDialog('设为默认知识库？新会话和未指定库的问答将优先使用该库。'))) return;
      setBusy(el, true, '保存中…');
      try {
        await api(`/api/kb/libraries/${encodeURIComponent(el.dataset.id)}`, { method: 'PUT', body: { is_default: true } });
        toast('已设为默认', 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'library-archive': async (el) => {
      if (!(await confirmDialog('归档该知识库？归档后不再出现在问答库列表；数据保留，可由管理员离线清理。'))) return;
      setBusy(el, true, '归档中…');
      try {
        await api(`/api/kb/libraries/${encodeURIComponent(el.dataset.id)}/archive`, { method: 'POST', body: {} });
        toast('已归档', 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'library-unarchive': async (el) => {
      if (!(await confirmDialog('恢复该知识库？恢复后会重新出现在问答库列表。'))) return;
      setBusy(el, true, '恢复中…');
      try {
        await api(`/api/kb/libraries/${encodeURIComponent(el.dataset.id)}`, { method: 'PUT', body: { status: 'active' } });
        toast('已恢复', 'ok'); renderKbDocs();
      } finally { setBusy(el, false); }
    },
    'library-restore': async (el) => {
      const file = view.querySelector('#kb-lib-file')?.files?.[0];
      if (!file) { toast('请选择备份包', 'error'); return; }
      const fd = new FormData();
      fd.append('file', file, file.name);
      const name = view.querySelector('#kb-lib-restore-name')?.value.trim() || '';
      if (name) fd.append('name', name);
      const target = view.querySelector('#kb-lib-restore-target')?.value || '';
      if (target) fd.append('target_kb_id', target);
      setBusy(el, true, '恢复中…');
      try {
        const r = await api('/api/kb/libraries/restore', { method: 'POST', body: fd, timeoutMs: 300000 });
        toast(`${r.merged ? '已合并' : '已恢复到新库'}：${r.documents || 0} 个文档`, 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'job-rollback': (el) => batchJobAction(view, el, '/api/kb/import/jobs/rollback',
      '回退所选 {n} 个导入任务？本批新增文档会被停用，未审草稿会退回；版本与修订链保留。', '回退中…'),
    'job-retry': (el) => batchJobAction(view, el, '/api/kb/import/jobs/retry',
      '重试所选 {n} 个任务中的失败文件？', '重试中…'),
    'job-delete': (el) => batchJobAction(view, el, '/api/kb/import/jobs/delete',
      '删除所选 {n} 个导入任务记录？已入库文档不会删除，仅删除任务和暂存文件。', '删除中…'),
    'draft-view': (el) => openDraft(el.dataset.id),
    'draft-approve': async (el) => {
      if (!(await confirmDialog('批准该草稿并立即进入检索？'))) return;
      setBusy(el, true, '批准中…');
      try { await api(`/api/kb/drafts/${encodeURIComponent(el.dataset.id)}/approve`, { method: 'POST', body: {} }); toast('已批准', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'draft-bulk-approve': async (el) => {
      const ids = [...view.querySelectorAll('.kb-draft-check:checked')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选草稿', 'error'); return; }
      if (!(await confirmDialog(`批量批准 ${ids.length} 个草稿并写入知识库？`))) return;
      setBusy(el, true, '批量批准中…');
      let ok = 0; let failed = 0;
      for (const id of ids) {
        try { await api(`/api/kb/drafts/${encodeURIComponent(id)}/approve`, { method: 'POST', body: {} }); ok += 1; }
        catch { failed += 1; }
      }
      toast(`已批准 ${ok} 个${failed ? `，${failed} 个失败` : ''}`, failed ? 'error' : 'ok');
      renderKbDocs();
    },
    'draft-bulk-reject': async (el) => {
      const ids = [...view.querySelectorAll('.kb-draft-check:checked')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选草稿', 'error'); return; }
      const reason = prompt('批量退回原因（可留空）') ?? null;
      if (reason === null) return;
      setBusy(el, true, '批量退回中…');
      let ok = 0; let failed = 0;
      for (const id of ids) {
        try { await api(`/api/kb/drafts/${encodeURIComponent(id)}/reject`, { method: 'POST', body: { reason } }); ok += 1; }
        catch { failed += 1; }
      }
      toast(`已退回 ${ok} 个${failed ? `，${failed} 个失败` : ''}`, failed ? 'error' : 'ok');
      renderKbDocs();
    },
    'review-bulk-reject': async (el) => {
      const ids = [...view.querySelectorAll('.kb-review-check:checked:not(:disabled)')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选审核记录', 'error'); return; }
      if (ids.length > 50) { toast('单批最多处理 50 条审核', 'error'); return; }
      const note = prompt('批量驳回原因（可留空）');
      if (note === null) return;
      setBusy(el, true, '批量驳回中…');
      try {
        const res = await api('/api/kb/reviews/batch-decision', { method: 'POST', body: { ids, decision: 'reject', note } });
        toast(`已驳回 ${res.done} 条${res.failed ? `，${res.failed} 条失败` : ''}`, res.failed ? 'error' : 'ok');
      } catch (err) { toast(err.message, 'error'); }
      setBusy(el, false);
      renderKbDocs();
    },
    'review-bulk-retry': async (el) => {
      const ids = [...view.querySelectorAll('.kb-review-check:checked:not(:disabled)')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选审核记录', 'error'); return; }
      if (ids.length > 50) { toast('单批最多处理 50 条审核', 'error'); return; }
      setBusy(el, true, '批量重试中…');
      try {
        const res = await api('/api/kb/reviews/batch-retry', { method: 'POST', body: { ids } });
        toast(`已重试 ${res.done} 条${res.failed ? `，${res.failed} 条失败` : ''}`, res.failed ? 'error' : 'ok');
      } catch (err) { toast(err.message, 'error'); }
      setBusy(el, false);
      renderKbDocs();
    },
    'review-bulk-approve': async (el) => {
      const ids = [...view.querySelectorAll('.kb-review-check:checked:not(:disabled)')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选审核记录', 'error'); return; }
      if (ids.length > 50) { toast('单批最多处理 50 条审核', 'error'); return; }
      if (!(await confirmDialog(`批量通过 ${ids.length} 条审核？存在 blocker 的记录会被拒绝。`))) return;
      setBusy(el, true, '批量通过中…');
      try {
        const res = await api('/api/kb/reviews/batch-decision', { method: 'POST', body: { ids, decision: 'approve', note: '批量通过' } });
        toast(`已通过 ${res.done} 条${res.failed ? `，${res.failed} 条被拦截或失败` : ''}`, res.failed ? 'error' : 'ok');
      } catch (err) { toast(err.message, 'error'); }
      setBusy(el, false);
      renderKbDocs();
    },
    'draft-reject': async (el) => {
      const reason = prompt('退回原因（可留空）') ?? null;
      if (reason === null) return;
      setBusy(el, true, '退回中…');
      try { await api(`/api/kb/drafts/${encodeURIComponent(el.dataset.id)}/reject`, { method: 'POST', body: { reason } }); toast('已退回', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'term-save': async (el) => {
      const term = view.querySelector('#kb-term-term')?.value.trim() || '';
      if (!term) { toast('请填写词条', 'error'); return; }
      const id = view.querySelector('#kb-term-id')?.value || '';
      const payload = {
        term,
        aliases: (view.querySelector('#kb-term-aliases')?.value || '').split(',').map((x) => x.trim()).filter(Boolean),
        kind: view.querySelector('#kb-term-kind')?.value || 'synonym',
        weight: Number(view.querySelector('#kb-term-weight')?.value || 1),
        enabled: !!view.querySelector('#kb-term-enabled')?.checked,
        kb_id: kbId,
      };
      setBusy(el, true, '保存中…');
      try {
        await api(id ? `/api/kb/terms/${encodeURIComponent(id)}` : '/api/kb/terms', { method: id ? 'PUT' : 'POST', body: payload });
        toast('词条已保存，检索立即生效', 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'term-reset': () => {
      for (const id of ['#kb-term-id', '#kb-term-term', '#kb-term-aliases']) { const el = view.querySelector(id); if (el) el.value = ''; }
      const w = view.querySelector('#kb-term-weight'); if (w) w.value = '1';
    },
    'term-edit': (el) => {
      const setv = (id, value) => { const x = view.querySelector(id); if (x) x.value = value; };
      setv('#kb-term-id', el.dataset.id || ''); setv('#kb-term-term', el.dataset.term || '');
      setv('#kb-term-aliases', el.dataset.aliases || ''); setv('#kb-term-weight', el.dataset.weight || '1');
      setv('#kb-term-kind', el.dataset.kind || 'synonym');
      const enabled = view.querySelector('#kb-term-enabled'); if (enabled) enabled.checked = el.dataset.enabled === '1';
      view.querySelector('#kb-term-term')?.focus();
    },
    'term-delete': async (el) => {
      if (!(await confirmDialog('删除该词条？删除后不会再参与查询扩展。'))) return;
      setBusy(el, true, '删除中…');
      try { await api(`/api/kb/terms/${encodeURIComponent(el.dataset.id)}?kb=${kbId}`, { method: 'DELETE' }); toast('词条已删除', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'term-import': async (el) => {
      const file = view.querySelector('#kb-term-file')?.files?.[0];
      if (!file) { toast('请选择词典 CSV', 'error'); return; }
      const fd = new FormData(); fd.append('file', file, file.name); fd.append('kb_id', String(kbId));
      setBusy(el, true, '导入中…');
      try { const r = await api('/api/kb/terms/import', { method: 'POST', body: fd }); toast(`已导入 ${r.imported || 0} 条`, 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'eval-import': async (el) => {
      const file = view.querySelector('#kb-eval-file')?.files?.[0];
      const name = view.querySelector('#kb-eval-name')?.value.trim() || '';
      if (!file) { toast('请选择评测集文件', 'error'); return; }
      if (!name) { toast('请填写评测集名称', 'error'); return; }
      const fd = new FormData(); fd.append('file', file, file.name); fd.append('name', name);
      setBusy(el, true, '导入中…');
      try { const r = await api('/api/kb/eval/sets', { method: 'POST', body: fd }); toast(`已导入 ${r.imported || 0} 条查询`, 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'eval-run': async (el) => {
      setBusy(el, true, '运行评测中…');
      try {
        const r = await api(`/api/kb/eval/sets/${encodeURIComponent(el.dataset.id)}/run`, { method: 'POST', body: { kb_id: kbId, top_k: 10 }, timeoutMs: 300000 });
        toast(`评测完成：R@5 ${(Number(r.summary?.['recall@5'] || 0) * 100).toFixed(1)}%`, 'ok');
        location.hash = `#/kb/docs?tab=eval&run=${encodeURIComponent(r.id)}&kb=${kbId}`;
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'eval-view': async (el) => {
      try {
        const set = await api(`/api/kb/eval/sets/${encodeURIComponent(el.dataset.id)}`);
        const run = (set.runs || [])[0];
        if (!run) { toast('该评测集还没有运行记录', 'warn'); return; }
        location.hash = `#/kb/docs?tab=eval&run=${encodeURIComponent(run.id)}&kb=${kbId}`;
      } catch (err) { toast(err.message, 'error'); }
    },
    'eval-delete': async (el) => {
      if (!(await confirmDialog('删除该评测集及其运行记录？只删除评测数据，不影响知识库。'))) return;
      setBusy(el, true, '删除中…');
      try { await api(`/api/kb/eval/sets/${encodeURIComponent(el.dataset.id)}`, { method: 'DELETE' }); toast('已删除', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'dup-preview': (el) => openDuplicateWizard(el.dataset.source, el.dataset.target, kbId),
    'auto-publish': async (el) => {
      if (!(await confirmDialog('立即自动发布该草稿？将写入 kb.auto_publish 审计。'))) return;
      setBusy(el, true, '发布中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(el.dataset.id)}/auto-publish`, { method: 'POST', body: {} }); toast('已自动发布', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function importJobProgress(job) {
  const total = Math.max(0, Number(job.total_files || 0));
  const done = Math.max(0, Number(job.done_files || 0));
  const status = String(job.status || 'queued');
  let pct = 4;
  if (status === 'completed') pct = 100;
  else if (status === 'partial') pct = 92;
  else if (status === 'failed') pct = 88;
  else if (status === 'processing') pct = total ? Math.max(12, Math.min(88, Math.round((done / total) * 88))) : 18;
  else if (status === 'queued') pct = 5;
  const files = job.files || [];
  return `<div class="kb-job-progress">
    <div class="kb-job-progress-head"><span>${jobBadge(status)}</span>
      <span class="tiny muted">${done}/${total || '—'} 个文件 · ${esc(job.id)}</span>
      <span class="mono tiny" style="margin-left:auto">${pct}%</span></div>
    <div class="kb-progress-bar"><span style="width:${pct}%"></span></div>
    <div class="tiny muted" style="margin-top:6px">${esc(job.error || (status === 'processing' ? '正在解析文件并写入案例；页面会持续刷新进度。' : '等待后台任务开始…'))}</div>
    ${files.length ? `<div class="kb-job-files">${files.map((f) => `<div class="kb-job-file">
      <span class="mono tiny">${esc(f.name || f.rel_path || '')}</span>${f.status === 'done' ? '<span class="badge ok">完成</span>' : f.status === 'skipped' ? '<span class="badge warn">跳过</span>' : f.status === 'failed' ? '<span class="badge danger">失败</span>' : '<span class="badge warn">处理中</span>'}
      ${f.error ? `<span class="tiny danger-text">${esc(f.error)}</span>` : ''}</div>`).join('')}</div>` : ''}
  </div>`;
}

function pollImportJob(view, jobId) {
  let tries = 0;
  const timer = setInterval(async () => {
    tries += 1;
    try {
      const job = (await api(`/api/kb/import/jobs/${encodeURIComponent(jobId)}`)).job;
      const box = view.querySelector('#kb-job-status');
      if (box) box.innerHTML = importJobProgress(job);
      const cancel = view.querySelector('[data-act="cancel-job"]');
      if (cancel) cancel.disabled = ['completed', 'partial', 'failed', 'cancelled'].includes(job.status);
      if (['completed', 'partial', 'failed', 'cancelled'].includes(job.status) || tries > 180) {
        clearInterval(timer);
        if (job.status === 'completed' || job.status === 'partial') {
          toast(job.status === 'partial' ? '部分文件失败，请查看任务详情' : '导入完成', job.status === 'partial' ? 'warn' : 'ok');
          setTimeout(() => {
            const params = new URLSearchParams(location.hash.split('?')[1] || '');
            if (location.hash.startsWith('#/kb/docs') && params.get('tab') === 'import') renderKbDocs();
          }, 600);
        }
      }
    } catch (err) {
      if (tries > 8) { clearInterval(timer); toast(err.message, 'error'); }
    }
  }, 1100);
  state.cleanup = () => clearInterval(timer);
}

async function renderKbImport(view, header, status, kbId = 1) {
  let jobs = { items: [] };
  try { jobs = await api(`/api/kb/import/jobs?kb=${kbId}`); } catch { /* optional */ }
  view.innerHTML = `${header}
    <div class="grid kb-admin-grid">
      <div class="card">
        <div class="card-head"><h2>导入文档</h2><span class="sub">CSV / TXT / MD / HTML / DOCX / XLSX / PDF / 图片，单文件 ≤ ${status.config?.max_file_mb || 50} MB</span></div>
        <div class="card-body">
          <div class="kb-import-steps"><span>1 选择文件</span><span>2 解析与拆案例</span><span>3 写库并重建索引</span></div>
          <form id="kb-import-form">
            <div class="form-row"><label>选择文件（可多选）</label>
              <input class="input" type="file" name="files" multiple accept=".csv,.tsv,.txt,.md,.markdown,.log,.html,.htm,.docx,.xlsx,.pdf,.png,.jpg,.jpeg,.webp" required>
              <div class="tiny muted">CSV/XLSX 先预览表头再映射列；DOCX/PDF 抽取文本；图片/扫描 PDF 进入人工转写队列。同一条答案里的多个【原因分析】会拆成多个派生案例。</div>
              <div class="inline" style="margin-top:8px"><button class="btn btn-sm" type="button" data-act="preview-mapping">${icon('search')} 解析表头 / 列映射</button>
                <span class="tiny muted">仅 CSV / XLSX；多文件时对第一个文件生效</span></div>
              <div id="kb-import-preview" style="margin-top:10px"></div></div>
            ${state.user?.role === 'admin' ? `<div class="form-row"><label>入库方式</label>
              <select class="input" name="mode"><option value="direct">直接生效（管理员）</option>
              <option value="review">先进入审核队列</option></select></div>` : `<input type="hidden" name="mode" value="review">`}
            <div class="form-row"><label>目标知识库</label><select class="input" name="kb_id">
              ${(status.libraries || []).filter((x) => x.status === 'active').map((x) => `<option value="${esc(x.id)}" ${Number(x.id) === Number(status.kb_id || 1) ? 'selected' : ''}>${esc(x.name)}</option>`).join('') || '<option value="1">故障知识库</option>'}
            </select></div>
            <div class="inline"><button class="btn btn-primary" type="button" data-act="import">${icon('upload')} 开始导入</button>
              <button class="btn" type="button" data-act="sample" ${(status.documents || 0) ? 'disabled' : ''}>载入示例</button></div>
          </form>
          <div id="kb-job-status" class="kb-job-status" style="margin-top:12px"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>最近导入任务</h2><span class="sub">勾选后可批量回退、重试或删除记录</span></div>
        <div class="card-body tight">
          ${(jobs.items || []).length ? `<div class="kb-job-list">${jobs.items.slice(0, 10).map((j) => `<div class="kb-job-row">
            <label class="check"><input type="checkbox" class="kb-job-check" value="${esc(j.id)}"></label>
            <span style="font-weight:600">${esc(j.display_name || j.id)}</span>${jobBadge(j.status)}
            <span class="tiny muted">${j.done_files || 0}/${j.total_files || 0} 文件</span>
            ${j.stats?.skipped ? `<span class="tiny muted">跳过 ${j.stats.skipped}</span>` : ''}
            <span class="tiny muted">${esc(j.created_at ? fmtDate(j.created_at) : '')}</span>
            ${j.rollback_supported === false ? '<span class="badge warn">旧任务</span>' : ''}
          </div>`).join('')}</div>
          <div class="inline" style="margin-top:10px;flex-wrap:wrap">
            <button class="btn btn-sm" data-act="job-rollback">${icon('history')} 回退所选</button>
            <button class="btn btn-sm" data-act="job-retry">${icon('refresh')} 重试失败</button>
            <button class="btn btn-sm btn-danger" data-act="job-delete">${icon('trash')} 删除记录</button>
            <span class="tiny muted">回退会停用本批新增文档、退回未审草稿；版本与修订链保留。</span>
          </div>` : '<div class="empty small">还没有导入任务</div>'}
        </div>
      </div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>导入说明：</strong>导入是后台任务，文件完成后才进入索引；原始文本仅管理员可见。
      大批量导入时可以先完成上传，再离开本页，任务会在后台继续。
    </div></div>`;
  bindActions(view, {
    import: async (el) => {
      const form = view.querySelector('#kb-import-form');
      const files = form.querySelector('input[type="file"]').files;
      if (!files || !files.length) { toast('请选择文件', 'error'); return; }
      const fd = new FormData();
      for (const f of files) fd.append('files', f, f.name);
      // 把预览页的列映射随任务保存；没有映射则沿用旧自动猜测。
      const map = {};
      view.querySelectorAll('.kb-map-select').forEach((sel) => {
        if (sel.value) map[sel.dataset.header] = sel.value;
      });
      if (Object.keys(map).length) fd.append('mapping', JSON.stringify(map));
      const sheet = view.querySelector('#kb-import-sheet')?.value || '';
      if (sheet) fd.append('sheet', sheet);
      fd.append('mode', form.querySelector('[name="mode"]')?.value || 'review');
      fd.append('kb_id', form.querySelector('[name="kb_id"]')?.value || '1');
      setBusy(el, true, '上传中…');
      try {
        const r = await api('/api/kb/import', { method: 'POST', body: fd, timeoutMs: 600000 });
        toast('已创建导入任务，后台解析中', 'ok');
        const box = view.querySelector('#kb-job-status');
        if (box) box.innerHTML = importJobProgress({ ...r.job, files: [], error: '' });
        pollImportJob(view, r.job.id);
      } catch (err) { toast(err.message, 'error'); }
      finally { setBusy(el, false); }
    },
    'preview-mapping': async (el) => {
      const form = view.querySelector('#kb-import-form');
      const file = form.querySelector('input[type="file"]')?.files?.[0];
      if (!file) { toast('请选择一个 CSV 或 XLSX 文件', 'error'); return; }
      if (!/\.(csv|tsv|xlsx)$/i.test(file.name)) { toast('列映射预览仅支持 CSV / XLSX', 'warn'); return; }
      const fd = new FormData(); fd.append('file', file, file.name);
      setBusy(el, true, '解析中…');
      try {
        const { preview } = await api('/api/kb/import/preview', { method: 'POST', body: fd });
        view._kbImportPreview = preview;
        renderImportPreview(view.querySelector('#kb-import-preview'), preview);
        toast('表头已解析；确认映射后开始导入', 'ok');
      } catch (err) { toast(err.message, 'error'); }
      finally { setBusy(el, false); }
    },
    'preview-sheet': (el) => {
      renderImportPreview(view.querySelector('#kb-import-preview'), view._kbImportPreview, el.value);
    },
    sample: async (el) => {
      if (!(await confirmDialog('载入 20 条脱敏示例案例？'))) return;
      setBusy(el, true, '载入中…');
      try { await api('/api/kb/sample', { method: 'POST', body: {} }); toast('已载入示例', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
}

async function renderKbTab(tab, view, status, facets, kbId = 1) {
  const admin = state.user?.role === 'admin';
  bindKbTabActions(view, kbId);
  view.addEventListener('change', (e) => {
    if (e.target.id === 'kb-library-switch') {
      location.hash = `#/kb/docs?tab=overview&kb=${encodeURIComponent(e.target.value)}`;
    }
  });
  const tabs = kbTabNav(tab, kbId);
  const kbSuffix = Number(kbId) && Number(kbId) !== 1 ? `&kb=${Number(kbId)}` : '';
  const header = `${tabs}${kbLibraryBar(status, kbId)}`;
  if (!admin) {
    view.innerHTML = `${header}<div class="alert warn">${icon('alert')}<div>仅管理员可进入知识库管理。</div></div>`;
    return;
  }
  if (tab === 'import') {
    await renderKbImport(view, header, status, kbId);
    return;
  }
  const statsDays = Math.max(1, Math.min(365, Number(
    new URLSearchParams(location.hash.split('?')[1] || '').get('days') || 30)));
  let stats = null;
  if (['overview', 'stats', 'eval'].includes(tab)) {
    try { stats = await api(`/api/kb/stats?days=${statsDays}`); }
    catch (err) { view.innerHTML = `${header}<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`; return; }
  }
  if (tab === 'libraries') {
    let libraries = { items: [] };
    try { libraries = await api('/api/kb/libraries?include_archived=1'); }
    catch (err) { view.innerHTML = `${header}<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`; return; }
    const kindLabel = { fault: '故障库', expert: '专家库', corpus: '语料库' };
    view.innerHTML = `${header}
      <div class="grid grid-2">
        <div class="card"><div class="card-head"><h2>知识库列表</h2><span class="sub">故障库 / 专家库 / 语料库同构；默认库不可归档</span></div>
          <div class="card-body tight"><div class="kb-job-list">
            ${(libraries.items || []).map((x) => `<div class="kb-job-row" style="flex-wrap:wrap">
              <span style="font-weight:600">${esc(x.name)}</span>
              <span class="badge ${x.status === 'active' ? 'ok' : 'warn'}">${x.status === 'active' ? '生效' : '已归档'}</span>
              <span class="badge info">${esc(kindLabel[x.kind] || x.kind)}</span>
              <span class="badge ${x.visibility === 'admin' ? 'warn' : ''}">${x.visibility === 'admin' ? '仅管理员' : '全员可见'}</span>
              <span class="tiny muted">${x.documents || 0} 文档 / ${x.cases || 0} 案例</span>
              ${x.is_default ? '<span class="badge">默认</span>' : ''}
              <span style="margin-left:auto" class="inline">
                <a class="btn btn-sm" href="/api/kb/libraries/${encodeURIComponent(x.id)}/backup">${icon('download')} 备份</a>
                ${x.status === 'active' ? `<button class="btn btn-sm" data-act="library-rename" data-id="${esc(x.id)}" data-name="${esc(x.name)}">重命名</button>
                <button class="btn btn-sm" data-act="library-visibility" data-id="${esc(x.id)}" data-visibility="${esc(x.visibility || 'all')}">${x.visibility === 'admin' ? '设为全员' : '设为仅管理员'}</button>
                ${!x.is_default ? `<button class="btn btn-sm" data-act="library-default" data-id="${esc(x.id)}">设为默认</button>` : ''}` : ''}
                ${x.status === 'archived' && !x.is_default ? `<button class="btn btn-sm" data-act="library-unarchive" data-id="${esc(x.id)}">恢复</button>` : ''}
                ${x.status === 'active' && !x.is_default ? `<button class="btn btn-sm btn-danger" data-act="library-archive" data-id="${esc(x.id)}">归档</button>` : ''}
              </span>
              ${x.description ? `<div class="tiny muted" style="flex-basis:100%">${esc(x.description)}</div>` : ''}
            </div>`).join('') || '<div class="empty small">还没有知识库</div>'}
          </div></div></div>
        <div class="card"><div class="card-head"><h2>新建 / 恢复</h2><span class="sub">备份格式 *.kbbackup.zip（v2），含文档/案例/草稿/审核/修订/版本</span></div>
          <div class="card-body">
            <div class="form-row"><label>名称</label><input class="input" id="kb-lib-name" maxlength="80" placeholder="例如：专家经验库"></div>
            <div class="form-row"><label>类型</label><select class="input" id="kb-lib-kind">
              <option value="fault">故障库</option><option value="expert">专家库</option><option value="corpus">语料库</option></select></div>
            <div class="form-row"><label>说明</label><input class="input" id="kb-lib-desc" maxlength="500" placeholder="可选"></div>
            <button class="btn btn-primary" data-act="library-create">${icon('plus')} 创建知识库</button>
            <hr style="margin:16px 0;border:0;border-top:1px solid var(--line)">
            <div class="form-row"><label>从备份恢复</label><input class="input" id="kb-lib-file" type="file" accept=".zip,.kbbackup.zip">
              <input class="input" id="kb-lib-restore-name" placeholder="恢复后的名称（新建库时可选）" style="margin-top:6px">
              <select class="input" id="kb-lib-restore-target" style="margin-top:6px">
                <option value="">恢复为新知识库</option>
                ${(libraries.items || []).filter((x) => x.status === 'active').map((x) => `<option value="${esc(x.id)}">合并到：${esc(x.name)}</option>`).join('')}
              </select></div>
            <button class="btn" data-act="library-restore">${icon('upload')} 上传并恢复</button>
          </div></div>
      </div>`;
    return;
  }
  if (tab === 'overview') {
    let reviews = { items: [] };
    let jobs = { items: [] };
    try { reviews = await api(`/api/kb/reviews?status=pending&limit=6&kb=${kbId}`); } catch { /* optional */ }
    try { jobs = await api(`/api/kb/import/jobs?kb=${kbId}`); } catch { /* optional */ }
    const attention = [];
    if (reviews.items?.length) attention.push(`<div class="kb-attention"><strong>${reviews.items.length} 条审核待处理</strong>
      <a href="#/kb/docs?tab=review${kbSuffix}">去审核</a></div>`);
    if (Number(stats.review?.pending || 0)) attention.push(`<div class="kb-attention"><strong>${stats.review.pending} 条 AI/人工审核未完成</strong>
      <a href="#/kb/docs?tab=review${kbSuffix}">查看</a></div>`);
    if (Number(stats.inventory?.drafts || 0)) attention.push(`<div class="kb-attention"><strong>${stats.inventory.drafts} 个草稿待入库</strong>
      <a href="#/kb/docs?tab=review${kbSuffix}">处理</a></div>`);
    if (!attention.length) attention.push(`<div class="kb-attention ok">当前没有待处理事项</div>`);
    view.innerHTML = `${header}
      <div class="grid grid-4 kb-stats">
        <div class="card stat"><div><div class="stat-val">${stats.cards.documents}</div><div class="stat-label">生效文档</div><div class="stat-note">${stats.cards.cases} 个可引用案例</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.cards.queries}</div><div class="stat-label">30 天查询</div><div class="stat-note">零结果 ${stats.cards.zero_result_rate}%</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.cards.pending_reviews}</div><div class="stat-label">待审核</div><div class="stat-note">AI 预审 + 人工终审</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.cards.revisions}</div><div class="stat-label">30 天修订</div><div class="stat-note">哈希链可校验</div></div></div>
      </div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><div class="card-head"><h2>需要关注</h2></div><div class="card-body">${attention.join('')}</div></div>
        <div class="card"><div class="card-head"><h2>最近导入</h2><a class="tiny" href="#/kb/docs?tab=import${kbSuffix}" style="margin-left:auto">全部</a></div>
          <div class="card-body tight">${(jobs.items || []).slice(0, 5).map((j) => `<div class="kb-job-row">
            <span>${esc(j.display_name || j.id)}</span>${jobBadge(j.status)}<span class="tiny muted">${j.done_files || 0}/${j.total_files || 0} 文件</span></div>`).join('') || emptyState('inbox', '还没有导入', '导入 CSV/文档或载入示例。')}</div></div>
      </div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><div class="card-head"><h2>索引状态</h2></div><div class="card-body small">
          <div class="kv"><div class="k">状态</div><div class="v">${esc(stats.index.status)}</div>
          <div class="k">文档 / 案例</div><div class="v">${stats.index.documents} / ${stats.index.cases}</div>
          <div class="k">词项 / 体积</div><div class="v">${stats.index.terms} / ${Math.round((stats.index.bytes || 0) / 1024)} KB</div>
          <div class="k">最近构建</div><div class="v tiny">${esc(stats.index.built_at || '—')} · ${stats.index.build_ms} ms</div></div></div></div>
        <div class="card"><div class="card-head"><h2>最近变更</h2><a class="tiny" href="#/kb/docs?tab=audit${kbSuffix}" style="margin-left:auto">审计</a></div>
          <div class="card-body tight">${stats.changes.top_cases.slice(0, 6).map((x) => `<div class="kb-job-row">
            <span>${esc(x.title || x.case_id)}</span><span class="tiny muted">${x.revisions} 次修订</span></div>`).join('') || '<div class="empty small">暂无修订</div>'}</div></div>
      </div>`;
    return;
  }
  if (tab === 'stats') {
    view.innerHTML = `${header}
      <div class="inline" style="margin-bottom:10px"><span class="tiny muted">统计范围：最近 ${stats.days} 天；库存与索引为当前真值</span>
        <span class="kb-range">${[7, 30, 90].map((d) => `<a class="btn btn-sm ${stats.days === d ? 'btn-primary' : ''}"
          href="#/kb/docs?tab=stats&days=${d}${kbSuffix}">${d} 天</a>`).join('')}</span>
        <a class="btn btn-sm" href="/api/kb/stats?format=csv&days=${stats.days}" style="margin-left:auto">${icon('download')} 导出 CSV</a></div>
      <div class="grid grid-4 kb-stats">
        <div class="card stat"><div><div class="stat-val">${stats.inventory.active_documents}</div><div class="stat-label">生效文档</div><div class="stat-note">${stats.inventory.active_cases} 案例 · ${stats.inventory.drafts} 待审草稿</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.usage.queries}</div><div class="stat-label">查询次数</div><div class="stat-note">零结果率 ${stats.usage.zero_result_rate}% · P95 ${stats.usage.p95_latency_ms} ms</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.usage.feedback.helpful_rate}%</div><div class="stat-label">有帮助率</div><div class="stat-note">${stats.usage.feedback.helpful} 有帮助 / ${stats.usage.feedback.unhelpful} 无帮助</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.review.pending}</div><div class="stat-label">审核待处理</div><div class="stat-note">AI 覆盖 ${stats.review.ai_coverage}% · 人工通过 ${stats.review.human_approval_rate}%</div></div></div>
      </div>
      <div class="card" style="margin-top:14px"><div class="card-head"><h2>${stats.days} 天查询趋势</h2><span class="sub">柱高按当日查询量归一化</span></div><div class="card-body">${trendBars(stats.trend, stats.days)}</div></div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><div class="card-head"><h2>高频问题</h2></div><div class="card-body">${miniBars(stats.usage.top_queries) || '<div class="empty small">暂无查询</div>'}</div></div>
        <div class="card"><div class="card-head"><h2>被引用案例</h2></div><div class="card-body">${miniBars(stats.usage.cited_cases, 'citation_count') || '<div class="empty small">暂无引用</div>'}</div></div>
      </div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><div class="card-head"><h2>覆盖</h2></div><div class="card-body small">
          <div class="kv"><div class="k">原因覆盖</div><div class="v">${stats.coverage.cause_coverage}%</div>
          <div class="k">方案覆盖</div><div class="v">${stats.coverage.solution_coverage}%</div>
          <div class="k">低质量</div><div class="v">${stats.coverage.low_quality}</div></div>
          <div class="kb-subhead">板卡 × 领域</div>
          <div class="kb-matrix">${(stats.coverage.board_domain_matrix || []).slice(0, 12).map((x) =>
            `<span class="badge info">${esc(x.board)} / ${esc(x.domain)} · ${x.count}</span>`).join('') || '<span class="muted tiny">暂无标签</span>'}</div></div></div>
        <div class="card"><div class="card-head"><h2>审核与变更</h2></div><div class="card-body small">
          <div class="kv"><div class="k">AI 覆盖</div><div class="v">${stats.review.ai_coverage}%</div>
          <div class="k">AI 结论</div><div class="v">通过 ${stats.review.ai_pass} · 转人工 ${stats.review.ai_needs_review + stats.review.fallback_manual} · 驳回 ${stats.review.ai_reject}</div>
          <div class="k">人工推翻 AI</div><div class="v">${stats.review.human_overrides}</div>
          <div class="k">修订 / 恢复</div><div class="v">${stats.changes.revisions} / ${stats.changes.restores}</div>
          <div class="k">导入成功率</div><div class="v">${stats.imports.success_rate}%</div></div>
          <div class="kb-subhead">最近变更人</div>
          ${(stats.changes.actors || []).slice(0, 5).map((x) => `<div class="kb-meter"><span class="tiny">${esc(x.actor)}</span><span class="mono tiny">${x.count}</span></div>`).join('') || '<span class="muted tiny">暂无</span>'}
        </div></div>
      </div>`;
    return;
  }
  if (tab === 'review') {
    const params = new URLSearchParams(location.hash.split('?')[1] || '');
    const activeView = params.get('view') === 'reviews' ? 'reviews' : 'drafts';
    let drafts = { items: [] };
    let reviews = { items: [] };
    try { drafts = await api(`/api/kb/drafts?kb=${kbId}`); } catch { /* optional */ }
    try { reviews = await api(`/api/kb/reviews?status=pending&limit=100&kb=${kbId}`); } catch { /* optional */ }
    let autoPublish = { enabled: false, dry_run: true, items: [], would_publish: 0 };
    try { autoPublish = await api('/api/kb/reviews/auto-publish/dry-run?limit=40'); } catch { /* optional */ }
    const reviewTabs = [
      ['drafts', `待入库草稿 ${drafts.total ?? drafts.items?.length ?? 0}`],
      ['reviews', `AI / 人工审核 ${reviews.total ?? reviews.items?.length ?? 0}`],
    ];
    const panels = {
      drafts: `${(drafts.items || []).length ? `<table class="data responsive"><thead><tr>
        <th><input type="checkbox" id="kb-draft-all" aria-label="全选草稿"></th><th>标题</th><th>案例</th><th>操作</th></tr></thead><tbody>
        ${drafts.items.map((d) => `<tr><td data-label="选择"><input type="checkbox" class="kb-draft-check" value="${esc(d.id)}"></td>
        <td data-label="标题"><strong>${esc(d.title)}</strong><div class="tiny muted">${esc((d.warnings || []).join('；') || '无警告')}</div></td>
        <td data-label="案例">${d.case_count || 0}</td>
        <td data-label="操作"><button class="btn btn-sm" data-act="draft-view" data-id="${esc(d.id)}">查看</button>
        <button class="btn btn-sm btn-primary" data-act="draft-approve" data-id="${esc(d.id)}">批准</button>
        <button class="btn btn-sm btn-danger" data-act="draft-reject" data-id="${esc(d.id)}">退回</button></td></tr>`).join('')}
        </tbody></table>` : emptyState('check', '没有待审草稿', '工程师导入的文档会先进入这里。')}
        <div class="inline" style="margin-top:10px"><button class="btn btn-sm btn-primary" data-act="draft-bulk-approve">批量批准</button>
        <button class="btn btn-sm btn-danger" data-act="draft-bulk-reject">批量退回</button>
        <span class="tiny muted">勾选后批量处理；每条仍会走 blocker 检查与修订链。</span></div>`,
      reviews: `${(reviews.items || []).length ? `<table class="data responsive"><thead><tr>
        <th><input type="checkbox" id="kb-review-all" aria-label="全选审核"></th><th>对象</th><th>结论</th><th>状态</th><th>操作</th></tr></thead><tbody>
        ${reviews.items.map((r) => `<tr><td data-label="选择"><input type="checkbox" class="kb-review-check" value="${esc(r.id)}" ${r.status === 'decided' ? 'disabled' : ''}></td>
        <td data-label="对象"><strong>${esc(r.target_title || r.target_id)}</strong><div class="tiny muted">${esc(r.target_type)} · ${esc(r.target_id)}</div></td>
        <td data-label="结论">${r.decision ? `<span class="badge ${r.decision === 'pass' ? 'ok' : r.decision === 'reject' ? 'danger' : 'warn'}">${esc(r.decision)}</span> <span class="tiny">${r.score ? Math.round(r.score) + ' 分' : ''}</span>` : '—'}</td>
        <td data-label="状态">${reviewBadge(r.status)}</td>
        <td data-label="操作"><button class="btn btn-sm" data-act="review-view" data-id="${esc(r.id)}">详情</button>
        ${r.status !== 'decided' ? `<button class="btn btn-sm" data-act="review-decision" data-id="${esc(r.id)}">终审</button>` : ''}</td></tr>`).join('')}
        </tbody></table>` : emptyState('check', '没有待处理审核', '导入或编辑后会生成审核记录。')}
        <div class="inline" style="margin-top:10px"><button class="btn btn-sm btn-primary" data-act="review-bulk-approve">批量通过</button>
        <button class="btn btn-sm btn-danger" data-act="review-bulk-reject">批量驳回</button>
        <button class="btn btn-sm" data-act="review-bulk-retry">批量重试</button>
        <button class="btn btn-sm" data-act="review-batch">${icon('refresh')} 处理排队中的 AI 预审</button>
        <span class="tiny muted">批量通过仍会拒绝 blocker；批量驳回/重试只处理勾选的未决记录。</span></div>`,
    };
    view.innerHTML = `${header}
      <div class="card"><div class="card-head"><h2>审核队列</h2><span class="sub">草稿与 AI 预审分开展示；处理后再发布</span></div>
        <div class="card-body">
          <div class="kb-tabbar" role="tablist">${reviewTabs.map(([key, label]) =>
            `<a class="kb-tab ${key === activeView ? 'active' : ''}" href="#/kb/docs?tab=review&view=${key}${kbSuffix}" role="tab">${label}</a>`).join('')}</div>
          <div class="table-wrap" style="margin-top:10px">${panels[activeView]}</div>
        </div></div>
      <div class="card" style="margin-top:14px"><div class="card-head"><h2>AI 自动发布 dry-run</h2>
        <span class="sub">总开关 ${autoPublish.enabled ? '已开启' : '关闭'}；dry-run ${autoPublish.dry_run ? '开启' : '关闭'}；策略 ${esc(autoPublish.policy_id || 'review-auto-v1')}</span>
        <span class="badge ${autoPublish.would_publish ? 'warn' : 'info'}" style="margin-left:auto">会发布 ${autoPublish.would_publish || 0} / 评估 ${autoPublish.evaluated || 0}</span></div>
        <div class="card-body"><div class="table-wrap"><table class="data"><thead><tr><th>草稿</th><th>分数</th><th>置信度</th><th>结论</th><th>原因</th><th></th></tr></thead><tbody>
          ${(autoPublish.items || []).slice(0, 30).map((x) => `<tr><td class="mono tiny">${esc(x.draft_id || '')}</td><td>${Math.round(Number(x.score || 0))}</td><td>${(Number(x.confidence || 0) * 100).toFixed(0)}%</td>
            <td><span class="badge ${x.eligible ? 'ok' : 'warn'}">${x.eligible ? '会发布' : '不发布'}</span></td><td class="tiny">${esc((x.reasons || []).join('；') || '满足全部策略')}</td>
            <td>${x.eligible && autoPublish.enabled && !autoPublish.dry_run ? `<button class="btn btn-sm" data-act="auto-publish" data-id="${esc(x.review_id)}">立即自动发布</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="6" class="empty small">没有可评估的已完成审核</td></tr>'}
        </tbody></table></div><div class="tiny muted">自动发布只针对 draft；上线前建议保持 dry-run 一周，观察误放率。关闭总开关后全部人工终审。</div></div></div>`;
    const checks = view.querySelectorAll('.kb-draft-check,.kb-review-check');
    view.querySelector('#kb-draft-all')?.addEventListener('change', (e) => {
      view.querySelectorAll('.kb-draft-check').forEach((x) => { x.checked = e.target.checked; });
    });
    view.querySelector('#kb-review-all')?.addEventListener('change', (e) => {
      view.querySelectorAll('.kb-review-check:not(:disabled)').forEach((x) => { x.checked = e.target.checked; });
    });
    return;
  }
  if (tab === 'audit') {
    const params = new URLSearchParams(location.hash.split('?')[1] || '');
    const actor = params.get('actor') || '';
    const action = params.get('action') || '';
    let audit = { items: [] };
    try { audit = await api(`/api/kb/audit?limit=100&actor=${encodeURIComponent(actor)}&action=${encodeURIComponent(action)}&kb=${kbId}`); }
    catch (err) { view.innerHTML = `${header}<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`; return; }
    view.innerHTML = `${header}
      <div class="card"><div class="card-head"><h2>变更与审计</h2><span class="sub">案例字段级 diff、文档操作与审核决策</span>
        <a class="btn btn-sm" style="margin-left:auto" href="/api/kb/audit?format=csv&limit=500&kb=${kbId}">${icon('download')} 导出 CSV</a></div>
        <div class="card-body">
          <div class="inline"><input class="input" id="kb-audit-actor" placeholder="操作人" value="${esc(actor)}" style="max-width:160px">
            <input class="input" id="kb-audit-action" placeholder="动作，如 case.update" value="${esc(action)}" style="max-width:220px">
            <button class="btn btn-sm" data-act="audit-filter">筛选</button></div>
          <div class="table-wrap" style="margin-top:10px"><table class="data responsive"><thead><tr><th>时间</th><th>动作</th><th>对象</th><th>操作人</th><th>原因</th></tr></thead><tbody>
            ${(audit.items || []).map((x) => `<tr><td data-label="时间" class="tiny">${esc(x.created_at ? fmtDate(x.created_at) : '—')}</td>
              <td data-label="动作"><span class="mono tiny">${esc(x.action)}</span></td>
              <td data-label="对象"><div>${x.href ? `<a href="${esc(x.href)}">${esc(x.target_title || x.target || '')}</a>` : esc(x.target_title || x.target || '')}</div><div class="tiny muted mono">${esc(x.target || '')}</div></td>
              <td data-label="操作人">${esc(x.actor || '—')}</td>
              <td data-label="原因" class="tiny">${esc(x.message || (Array.isArray(x.detail) ? x.detail.map((d) => d.field).join('、') : ''))}</td></tr>`).join('') || '<tr><td colspan="5" class="empty small">暂无审计记录</td></tr>'}
          </tbody></table></div>
        </div></div>`;
    return;
  }
  if (tab === 'terms') {
    let terms = { items: [], effective: 0 };
    try { terms = await api(`/api/kb/terms?kb=${kbId}`); }
    catch (err) { view.innerHTML = `${header}<div class="alert danger">${esc(err.message)}</div>`; return; }
    view.innerHTML = `${header}
      <div class="grid grid-2">
        <div class="card"><div class="card-head"><h2>同义词词典</h2><span class="sub">保存后立即参与检索；不重建倒排。${terms.effective} 条生效</span>
          <a class="btn btn-sm" style="margin-left:auto" href="/api/kb/terms/export?kb=${kbId}">${icon('download')} 导出 CSV</a></div>
          <div class="card-body tight table-wrap"><table class="data"><thead><tr><th>词条</th><th>同义词</th><th>权重</th><th>状态</th><th></th></tr></thead><tbody>
            ${(terms.items || []).map((x) => `<tr><td><strong>${esc(x.term)}</strong><div class="tiny muted">${esc(x.kind)}</div></td>
              <td class="tiny">${esc((x.aliases || []).join('、') || '—')}</td><td>${Number(x.weight || 1).toFixed(1)}</td>
              <td><span class="badge ${x.enabled ? 'ok' : 'warn'}">${x.enabled ? '启用' : '停用'}</span></td>
              <td><button class="btn btn-sm" data-act="term-edit" data-id="${x.id}" data-term="${esc(x.term)}" data-aliases="${esc((x.aliases || []).join(','))}" data-weight="${x.weight}" data-enabled="${x.enabled ? 1 : 0}" data-kind="${esc(x.kind)}">编辑</button>
                <button class="btn btn-sm btn-danger" data-act="term-delete" data-id="${x.id}">删除</button></td></tr>`).join('') || '<tr><td colspan="5" class="empty small">暂无自定义词条</td></tr>'}
          </tbody></table></div>
          <div class="card-body" style="border-top:1px solid var(--line)">
            <div class="form-row"><label>导入词典 CSV（term,aliases,kind,weight,enabled）</label><input class="input" type="file" id="kb-term-file" accept=".csv"></div>
            <button class="btn btn-sm" data-act="term-import">${icon('upload')} 导入</button>
          </div>
        </div>
        <div class="card"><div class="card-head"><h2>新增 / 编辑词条</h2><span class="sub">管理员可写；保存时拒绝成环、空别名和同名别名</span></div>
          <div class="card-body"><input type="hidden" id="kb-term-id">
            <div class="form-row"><label>词条</label><input class="input" id="kb-term-term" placeholder="如 看门狗"></div>
            <div class="form-row"><label>同义词（逗号分隔，最多 10 个）</label><input class="input" id="kb-term-aliases" placeholder="watchdog, wdg, 狗复位"></div>
            <div class="inline"><div class="form-row" style="flex:1"><label>类型</label><select class="input" id="kb-term-kind"><option value="synonym">同义词</option><option value="stopword">停用词</option><option value="domain">领域词</option></select></div>
              <div class="form-row" style="flex:0 0 110px"><label>权重</label><input class="input" id="kb-term-weight" type="number" min="0.1" max="10" step="0.1" value="1"></div>
              <label class="check" style="align-self:end;margin-bottom:10px"><input type="checkbox" id="kb-term-enabled" checked> 启用</label></div>
            <div class="inline" style="margin-top:10px"><button class="btn btn-primary" data-act="term-save">${icon('check')} 保存词条</button>
              <button class="btn" data-act="term-reset">清空</button></div>
            <div class="kb-subhead">入库标签</div><div class="kb-matrix">${(facets.boards || []).map((x) => `<span class="badge info">${esc(x.value)} · ${x.count}</span>`).join('') || '<span class="muted tiny">暂无</span>'}</div>
            <div class="kb-subhead">领域</div><div class="kb-matrix">${(facets.domains || []).map((x) => `<span class="badge">${esc(x.value)} · ${x.count}</span>`).join('') || '<span class="muted tiny">暂无</span>'}</div>
          </div>
        </div>
      </div>`;
    return;
  }
  if (tab === 'eval') {
    const params = new URLSearchParams(location.hash.split('?')[1] || '');
    const runId = params.get('run') || '';
    let sets = { items: [] };
    let run = null;
    try { sets = await api('/api/kb/eval/sets'); } catch (err) { view.innerHTML = `${header}<div class="alert danger">${esc(err.message)}</div>`; return; }
    if (runId) { try { run = await api(`/api/kb/eval/runs/${encodeURIComponent(runId)}`); } catch (err) { toast(err.message, 'error'); } }
    const metric = (key) => run ? Number(run.metrics?.summary?.[key] || 0) : 0;
    const pct = (key) => `${(metric(key) * 100).toFixed(1)}%`;
    const f = (key, digits = 3) => Number(metric(key)).toFixed(digits);
    const catRows = run ? Object.entries(run.metrics?.by_category || {}) : [];
    const failures = run?.failures || [];
    view.innerHTML = `${header}
      <div class="grid grid-2">
        <div class="card"><div class="card-head"><h2>评测集</h2><span class="sub">300 条金标准须专家标注；系统不生成 qrels</span></div>
          <div class="card-body tight"><div class="kb-job-list">
            ${(sets.items || []).map((x) => `<div class="kb-job-row" style="flex-wrap:wrap">
              <span style="font-weight:600">${esc(x.name)}</span>
              <span class="badge info">${x.query_count || 0} 条</span>
              <span class="tiny muted">${esc(x.description || '')}</span>
              ${x.last_run ? `<span class="badge ${x.last_run.status === 'success' ? 'ok' : 'warn'}">${esc(x.last_run.status)}</span>` : ''}
              <span style="margin-left:auto" class="inline">
                <button class="btn btn-sm btn-primary" data-act="eval-run" data-id="${esc(x.id)}">${icon('play')} 运行</button>
                <button class="btn btn-sm" data-act="eval-view" data-id="${esc(x.id)}">查看</button>
                <button class="btn btn-sm btn-danger" data-act="eval-delete" data-id="${esc(x.id)}">删除</button>
              </span>
              ${x.last_summary ? `<div class="tiny muted" style="flex-basis:100%">R@5 ${(Number(x.last_summary['recall@5'] || 0) * 100).toFixed(1)}% · MRR ${Number(x.last_summary['mrr@10'] || 0).toFixed(3)} · nDCG ${Number(x.last_summary['ndcg@10'] || 0).toFixed(3)}</div>` : ''}
            </div>`).join('') || '<div class="empty small">还没有评测集；先导入专家标注 JSONL/CSV。</div>'}
          </div>
          <div class="card-body" style="border-top:1px solid var(--line)">
            <div class="form-row"><label>导入评测集（JSONL / CSV）</label><input class="input" type="file" id="kb-eval-file" accept=".jsonl,.json,.csv"></div>
            <div class="inline"><input class="input" id="kb-eval-name" placeholder="评测集名称，如 300条金标准-v1" style="max-width:280px">
              <button class="btn btn-sm" data-act="eval-import">${icon('upload')} 导入</button></div>
            <div class="tiny muted" style="margin-top:8px">CSV 列：query,qrels,category,split；qrels 用逗号分隔案例 ID。四类各 75 条。</div>
          </div>
        </div>
        <div class="card"><div class="card-head"><h2>运行指标</h2><span class="sub">${run ? esc(run.metrics?.set_name || run.set_id) : '选择评测集运行'}</span></div>
          <div class="card-body">
            ${run ? `<div class="grid grid-4 kb-stats">
              <div class="card stat"><div><div class="stat-val">${pct('recall@5')}</div><div class="stat-label">Recall@5</div><div class="stat-note">CI ${(Number((run.metrics?.summary?.['recall@5_ci95'] || {}).lower || 0) * 100).toFixed(1)}–${(Number((run.metrics?.summary?.['recall@5_ci95'] || {}).upper || 0) * 100).toFixed(1)}%</div></div></div>
              <div class="card stat"><div><div class="stat-val">${f('mrr@10')}</div><div class="stat-label">MRR@10</div><div class="stat-note">${metric('graded_queries')} 条有标注</div></div></div>
              <div class="card stat"><div><div class="stat-val">${f('ndcg@10')}</div><div class="stat-label">nDCG@10</div><div class="stat-note">二值相关</div></div></div>
              <div class="card stat"><div><div class="stat-val">${pct('no_answer_accuracy')}</div><div class="stat-label">库外拒答</div><div class="stat-note">${metric('no_answer_queries')} 条无答案</div></div></div>
            </div>
            <div class="inline" style="margin:10px 0"><span class="badge ${run.metrics?.comparable === false ? 'warn' : 'ok'}">${run.metrics?.comparable === false ? '索引已变化，不可比' : '索引冻结可比'}</span>
              <span class="tiny muted mono">checksum ${esc((run.metrics?.index_checksum || '').slice(0, 12))}</span>
              <a class="btn btn-sm" style="margin-left:auto" href="/api/kb/eval/runs/${encodeURIComponent(run.id)}/export?format=csv">CSV</a>
              <a class="btn btn-sm" href="/api/kb/eval/runs/${encodeURIComponent(run.id)}/export?format=md">Markdown</a></div>
            ${run.metrics?.warnings?.length ? `<div class="alert warn">${run.metrics.warnings.map(esc).join('；')}</div>` : ''}
            <div class="table-wrap"><table class="data"><thead><tr><th>类别</th><th>查询</th><th>R@1</th><th>R@5</th><th>MRR</th><th>nDCG</th></tr></thead><tbody>
              ${catRows.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.queries}</td>${Number(v.graded_queries) ? `<td>${(v['recall@1'] * 100).toFixed(1)}%</td><td>${(v['recall@5'] * 100).toFixed(1)}%</td><td>${Number(v['mrr@10']).toFixed(3)}</td><td>${Number(v['ndcg@10']).toFixed(3)}</td>` : '<td>—</td><td>—</td><td>—</td><td>—</td>'}</tr>`).join('') || '<tr><td colspan="6" class="empty small">暂无分类指标</td></tr>'}
            </tbody></table></div>
            <div class="kb-subhead">失败样例（${failures.length}）</div>
            <div class="table-wrap"><table class="data"><thead><tr><th>查询</th><th>类别</th><th>原因</th><th>Top10</th></tr></thead><tbody>
              ${failures.slice(0, 30).map((x) => `<tr><td>${esc(x.query)}</td><td>${esc(x.category || '—')}</td><td class="tiny">${esc(x.reason)}</td><td class="tiny mono">${esc((x.top_case_ids || []).join('、'))}</td></tr>`).join('') || '<tr><td colspan="4" class="empty small">没有失败样例</td></tr>'}
            </tbody></table></div>` : `<div class="empty small">导入评测集后点击“运行”，这里会显示 Recall/MRR/nDCG、分类别指标和失败样例。空索引会安全跳过，不会除零。</div>`}
          </div>
        </div>
      </div>`;
    return;
  }
  if (tab === 'dedupe') {
    let dup = { items: [], total: 0, threshold: 0.82 };
    try { dup = await api(`/api/kb/duplicates?kb=${kbId}&limit=80`); }
    catch (err) { view.innerHTML = `${header}<div class="alert danger">${esc(err.message)}</div>`; return; }
    view.innerHTML = `${header}<div class="card"><div class="card-head"><h2>近重复合并向导</h2><span class="sub">字符 3-gram + Jaccard；只“并入”不物理删除，先预览再执行</span>
      <span class="badge info" style="margin-left:auto">阈值 ${(dup.threshold * 100).toFixed(0)}% · ${dup.total} 对 · ${dup.documents || 0} 文档</span></div>
      <div class="card-body tight"><div class="kb-job-list">
        ${(dup.items || []).map((x) => `<div class="kb-job-row" style="flex-wrap:wrap">
          <div style="flex-basis:100%"><strong>${esc(x.source.title)}</strong> <span class="badge warn">相似 ${(x.similarity * 100).toFixed(1)}%</span> <span class="tiny muted">→ ${esc(x.target.title)}</span></div>
          <div class="tiny muted" style="flex-basis:100%">源 ${x.source.case_count} 案例 · 目标 ${x.target.case_count} 案例 · 建议 canonical ${esc(x.suggested_target)}</div>
          <div class="inline" style="margin-top:6px">
            <button class="btn btn-sm" data-act="dup-preview" data-source="${esc(x.suggested_source)}" data-target="${esc(x.suggested_target)}">${icon('search')} 预览逐字段差异</button>
          </div>
        </div>`).join('') || '<div class="empty small">当前没有超过阈值的近重复候选。</div>'}
      </div></div></div>`;
    return;
  }
  if (tab === 'settings') {
    view.innerHTML = `${header}<div class="card"><div class="card-head"><h2>知识库设置</h2><span class="sub">运行时配置在“系统管理 → 运行时配置”修改</span></div>
      <div class="card-body"><div class="kv">
        <div class="k">审核开关</div><div class="v">${status.config.review_enabled ? '开启' : '关闭'}</div>
        <div class="k">导入自动预审</div><div class="v">${status.config.review_auto_on_import ? '开启' : '关闭'}</div>
        <div class="k">AI 自动发布</div><div class="v">${status.config.review_auto_publish ? '开启' : '关闭'}（默认关闭）</div>
        <div class="k">PII 脱敏</div><div class="v">${status.config.mask_pii ? '开启' : '关闭'}</div>
        <div class="k">单文件 / 单批</div><div class="v">${status.config.max_file_mb} MB / ${status.config.max_batch_mb} MB</div>
        <div class="k">问答引用上限</div><div class="v">${status.top_k}</div></div>
        <div class="inline" style="margin-top:12px"><a class="btn" href="#/admin">${icon('settings')} 去系统管理</a></div></div></div>`;
    return;
  }
  // docs 之外的兜底
  view.innerHTML = `${header}<div class="alert warn">未知分区</div>`;
}


export async function renderKbDocs() {
  const admin = state.user?.role === 'admin';
  const params = new URLSearchParams((location.hash.split('?')[1] || ''));
  const tab = params.get('tab') || 'docs';
  const docStatus = params.get('status') || 'all';
  const docQ = params.get('q') || '';
  const page = Math.max(1, Number(params.get('page') || 1));
  let kbId = Math.max(0, Number(params.get('kb') || 0));
  const view = mount('知识库管理', '文档库、导入、审核、变更与审计；原始文本仅管理员可见', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  if (tab !== 'docs') {
    try {
      const statusResp = await api(`/api/kb/status${kbId ? `?kb=${kbId}` : ''}`);
      kbId = kbId || Number(statusResp.status.kb_id || 1);
      const facets = await loadFacets(kbId);
      await renderKbTab(tab, view, statusResp.status, facets, kbId);
    } catch (err) {
      view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    }
    return view;
  }
  let status;
  let docs;
  try {
    status = (await api(`/api/kb/status${kbId ? `?kb=${kbId}` : ''}`)).status;
    kbId = kbId || Number(status.kb_id || 1);
    docs = await api(`/api/kb/docs?size=50&page=${page}&status=${encodeURIComponent(docStatus)}&q=${encodeURIComponent(docQ)}&kb=${kbId}`);
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return view;
  }
  const items = docs.items || [];
  const pages = Math.max(1, Math.ceil(Number(docs.total || items.length) / 50));
  const kbParam = Number(kbId) !== 1 ? `&kb=${Number(kbId)}` : '';
  view.innerHTML = `
    ${kbTabNav('docs', kbId)}
    <div class="grid grid-4 kb-stats">
      <div class="card stat"><div><div class="stat-val">${status.documents || 0}</div>
        <div class="stat-label">生效文档</div><div class="stat-note">${status.cases || 0} 个案例</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.drafts || 0}</div>
        <div class="stat-label">待审核</div><div class="stat-note">在“审核”标签处理</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.index?.terms || 0}</div>
        <div class="stat-label">索引词项</div><div class="stat-note">${esc(status.index?.built_at || '尚未构建')}</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.index?.build_ms || 0} ms</div>
        <div class="stat-label">最近重建</div><div class="stat-note">${esc(status.index?.status || 'empty')}</div></div></div>
    </div>
    <div class="card" style="margin-top:14px">
      <div class="card-head"><h2>文档库</h2><span class="sub">共 ${docs.total || items.length} 个；点击行查看案例；本页只展示文档，不在此导入</span>
        <div class="inline" style="margin-left:auto">
          <select class="input" id="kb-doc-status"><option value="all">全部状态</option>
            ${Object.entries(STATUS_LABEL).map(([k, v]) => `<option value="${k}" ${docStatus === k ? 'selected' : ''}>${v}</option>`).join('')}</select>
          <input class="input" id="kb-doc-q" placeholder="搜索标题/正文" value="${esc(docQ)}" style="max-width:200px">
          <button class="btn btn-sm" data-act="filter-docs">筛选</button>
          <button class="btn btn-sm" data-act="export-report">${icon('download')} 导出报告</button></div>
      </div>
      <div class="card-body tight table-wrap">
        ${items.length ? `<table class="data responsive"><thead><tr><th style="width:36px"><input type="checkbox" id="kb-doc-all" title="全选本页"></th><th>标题</th><th>来源</th><th>板卡/领域</th>
          <th>案例</th><th>状态</th><th>更新时间</th></tr></thead><tbody>
          ${items.map((d) => `<tr class="row-hover" data-href="#/kb/docs/${encodeURIComponent(d.id)}">
            <td><input type="checkbox" class="kb-doc-check" value="${esc(d.id)}" title="选中导出"></td>
            <td data-label="标题"><div style="font-weight:600">${esc(d.title)}</div>
              <div class="tiny muted mono">${esc(d.id)}</div></td>
            <td data-label="来源">${esc(d.source_type || '—')}<div class="tiny muted">${esc(d.source_name || '')}</div></td>
            <td data-label="板卡/领域">${esc((d.boards || []).join('、') || '—')}<div class="tiny muted">${esc((d.domains || []).join('、') || '')}</div></td>
            <td data-label="案例">${d.case_count || 0}</td>
            <td data-label="状态">${statusBadge(d.status)}</td>
            <td data-label="更新时间" class="tiny">${esc(d.updated_at ? fmtDate(d.updated_at) : '—')}</td>
          </tr>`).join('')}</tbody></table>`
          : emptyState('inbox', '暂无文档', '管理员可在“导入”标签导入 CSV/TXT/MD，或载入示例数据。',
            admin ? `<a class="btn btn-sm" href="#/kb/docs?tab=import${kbParam}">去导入</a>` : '')}
      </div>
      ${pages > 1 ? `<div class="pager"><span class="small muted">第 ${page} / ${pages} 页</span>
        <button class="btn btn-sm" data-act="page-docs" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
        <button class="btn btn-sm" data-act="page-docs" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>下一页</button></div>` : ''}
    </div>
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>数据纪律：</strong>原始文本只对管理员开放；普通用户和模型证据只使用脱敏文本。删除是软删除，版本仍保留用于审计。
    </div></div>`;

  const goDocs = (nextPage = page) => {
    location.hash = `#/kb/docs?tab=docs&status=${encodeURIComponent(docStatus)}&q=${encodeURIComponent(docQ)}&page=${nextPage}${kbParam}`;
  };
  bindActions(view, {
    'filter-docs': () => {
      const statusVal = view.querySelector('#kb-doc-status').value;
      const q = view.querySelector('#kb-doc-q').value.trim();
      location.hash = `#/kb/docs?tab=docs&status=${encodeURIComponent(statusVal)}&q=${encodeURIComponent(q)}&page=1&kb=${Number(kbId)}`;
    },
    'page-docs': (el) => goDocs(Number(el.dataset.page || 1)),
    'export-report': async (el) => {
      const ids = [...view.querySelectorAll('.kb-doc-check:checked')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选要导出的文档（可全选本页）', 'error'); return; }
      setBusy(el, true, '生成报告中…');
      try {
        const blob = await api('/api/kb/export/report', { method: 'POST', timeoutMs: 300000,
          body: { kb_id: kbId, doc_ids: ids, formats: ['html', 'md', 'csv', 'docx'], reveal: false,
                  title: `知识库导出-${ids.length}个文档` } });
        saveBlob(blob, `kb-report-${Date.now()}.zip`);
        toast('报告已生成（含 HTML/Word/CSV/Markdown）', 'ok');
      } catch (err) { toast(err.message, 'error'); }
      finally { setBusy(el, false); }
    },
    'doc-select-all': () => {},
  });
  view.querySelector('#kb-doc-all')?.addEventListener('change', (e) => {
    view.querySelectorAll('.kb-doc-check').forEach((x) => { x.checked = e.target.checked; });
  });
  view.querySelectorAll('tr[data-href]').forEach((tr) => {
    tr.addEventListener('click', (event) => {
      if (event.target.closest('button,a,input')) return;
      location.hash = tr.dataset.href;
    });
  });
  return view;
}

async function openCaseRevisions(caseId, docId, caseTitle) {
  let data;
  let verify;
  try {
    data = await api(`/api/kb/cases/${encodeURIComponent(caseId)}/revisions?size=50`);
    verify = await api(`/api/kb/cases/${encodeURIComponent(caseId)}/revisions/verify`);
  } catch (err) { toast(err.message, 'error'); return; }
  const items = data.items || [];
  const diffText = (diff) => (diff || []).map((d) => `${d.field}: ${JSON.stringify(d.before)} → ${JSON.stringify(d.after)}`).join('\n') || '字段无变化';
  const m = modal({
    title: `修订链：${caseTitle || caseId}`,
    wide: true,
    body: `<div class="alert ${verify.ok ? 'info' : 'danger'}">${icon(verify.ok ? 'check' : 'alert')}
      <div>${verify.ok ? `哈希链校验通过（${verify.checked} 条）` : `校验失败：r${verify.broken?.revision} ${verify.broken?.reason}`}</div></div>
      <div class="tiny muted" style="margin:8px 0">每次人工变更都追加一条修订；快照和哈希不可改写。恢复会生成新修订，不删除历史。</div>
      <div class="kb-rev-list">${items.map((r) => `<div class="kb-rev-row">
        <div class="inline"><strong>r${r.revision}</strong><span class="badge info">${esc(r.action)}</span>
          <span class="tiny muted">${esc(r.actor_name || '')} · ${esc(r.created_at ? fmtDate(r.created_at) : '')}</span>
          <span class="mono tiny" style="margin-left:auto">${esc((r.entry_hash || '').slice(0, 12))}</span></div>
        <div class="tiny">${esc(r.reason || '')}</div>
        <details><summary class="tiny muted">查看 diff</summary><pre class="kb-raw">${esc(diffText(r.diff))}</pre></details>
        <div class="inline"><button class="btn btn-sm" data-act="case-restore" data-id="${esc(r.revision)}">恢复此修订</button></div>
      </div>`).join('') || '<div class="empty small">暂无修订</div>'}</div>`,
    footer: `<button class="btn" data-act="close">关闭</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'close') { m.close(); return; }
    if (btn.dataset.act === 'case-restore') {
      const reason = prompt('恢复原因（写入审计）') ?? null;
      if (reason === null) return;
      setBusy(btn, true, '恢复中…');
      try {
        await api(`/api/kb/cases/${encodeURIComponent(caseId)}/revisions/${encodeURIComponent(btn.dataset.id)}/restore`,
          { method: 'POST', body: { reason } });
        toast('已恢复该修订', 'ok'); m.close(); renderKbDoc(docId);
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

const KB_CASE_EDIT_TABS = [
  ['basic', '标题与现象'],
  ['cause', '原因与方案'],
  ['more', '预防与变更'],
];

function openCaseEdit(caseData, docId) {
  let suggestion = null;
  const m = modal({
    title: `编辑案例 · ${caseData.title}`,
    wide: true,
    body: `<div class="alert warn">${icon('alert')}<div>保存会新增一条修订、重建索引，并出现在“修订记录”标签页；必须填写变更原因。</div></div>
      <details class="kb-guide"><summary>字段填写指引</summary>
        <ul class="tiny muted">
          <li><strong>现象</strong>：触发条件 + 可观测表现 + 影响范围；不要重复标题。</li>
          <li><strong>原因</strong>：直接根因和证据（日志/告警/版本差异），不确定就写“待确认”。</li>
          <li><strong>处理方案</strong>：按步骤编号，写清版本、命令和验证方法。</li>
          <li><strong>预防</strong>：如何避免复发，可关联到巡检或升级规范。</li>
        </ul>
      </details>
      <nav class="tabs detail-tabs" id="kb-case-tabs">${KB_CASE_EDIT_TABS.map(
        ([key, label], index) => `<button type="button" class="${index === 0 ? 'active' : ''}" data-case-tab="${key}">${label}</button>`).join('')}</nav>
      <div class="kb-ai-panel" id="kb-ai-panel" hidden></div>
      <div class="kb-case-pane" data-pane="basic">
        <div class="form-row"><label>标题</label><input class="input" id="kb-case-title" value="${esc(caseData.title)}">
          <div class="tiny muted">标题用于检索命中，保留板卡/告警码等关键词。</div></div>
        <div class="form-row"><label>现象</label><textarea class="input" id="kb-case-symptom" rows="6">${esc(caseData.symptom || '')}</textarea>
          <div class="tiny muted">已自动去掉开头重复的标题；如果仍重复可手动删除。</div></div>
      </div>
      <div class="kb-case-pane" data-pane="cause" hidden>
        <div class="form-row"><label>原因</label><textarea class="input" id="kb-case-cause" rows="6">${esc(caseData.cause_detail || caseData.cause || '')}</textarea>
          <div class="tiny muted">写直接根因与证据；多条独立原因用换行分条。</div></div>
        <div class="form-row"><label>处理方案</label><textarea class="input" id="kb-case-solution" rows="7">${esc(caseData.solution || '')}</textarea>
          <div class="tiny muted">建议按 1/2/3 步骤组织，并写明验证方式。</div></div>
      </div>
      <div class="kb-case-pane" data-pane="more" hidden>
        <div class="form-row"><label>预防</label><textarea class="input" id="kb-case-prevention" rows="4">${esc(caseData.prevention || '')}</textarea></div>
        <div class="form-row"><label>变更原因 <span class="badge warn">必填</span></label>
          <input class="input" id="kb-case-reason" placeholder="例如：补充现场验证结果 / 修正根因描述"></div>
        <div class="alert info tiny">${icon('info')}<div>AI 优化只生成建议稿，不会直接写入；应用后仍需核对事实、补全变更原因再保存。</div></div>
      </div>`,
    footer: `<button class="btn" data-act="cancel">取消</button>
      <button class="btn" data-act="ai-polish">✦ AI 优化</button>
      <button class="btn btn-primary" data-act="save">保存并生成修订</button>`,
  });

  const mask = m.mask;
  const switchTab = (key) => {
    mask.querySelectorAll('[data-case-tab]').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.caseTab === key);
    });
    mask.querySelectorAll('.kb-case-pane').forEach((pane) => {
      pane.hidden = pane.dataset.pane !== key;
    });
  };
  mask.querySelectorAll('[data-case-tab]').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.caseTab));
  });

  const renderAiPanel = () => {
    const panel = mask.querySelector('#kb-ai-panel');
    if (!panel || !suggestion) return;
    panel.hidden = false;
    const rows = [
      ['symptom', '现象'], ['cause', '原因'], ['solution', '处理方案'], ['prevention', '预防'],
    ].filter(([key]) => suggestion.suggestions[key]);
    panel.innerHTML = `<div class="inline"><strong>AI 优化建议</strong>
      <span class="badge ok">${esc(suggestion.model || '模型')}</span>
      <span class="tiny muted">${Number(suggestion.latency_ms || 0)} ms · 仅建议，未写入</span></div>
      ${suggestion.suggestions.notes ? `<div class="tiny muted" style="margin-top:6px">${esc(suggestion.suggestions.notes)}</div>` : ''}
      <div class="kb-ai-suggest">${rows.map(([key, label]) => `<details ${suggestion.changed[key] ? 'open' : ''}>
        <summary><strong>${label}</strong> <span class="badge ${suggestion.changed[key] ? 'warn' : 'info'}">${suggestion.changed[key] ? '有改动' : '与原文一致'}</span></summary>
        <div class="kb-ai-text">${esc(suggestion.suggestions[key])}</div></details>`).join('')}</div>
      <div class="inline" style="margin-top:10px"><button class="btn btn-sm btn-primary" data-act="ai-apply">应用建议到表单</button>
        <button class="btn btn-sm" data-act="ai-ignore">忽略</button></div>`;
  };

  mask.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === 'cancel') { m.close(); return; }
    if (act === 'ai-ignore') {
      suggestion = null;
      const panel = mask.querySelector('#kb-ai-panel');
      if (panel) panel.hidden = true;
      return;
    }
    if (act === 'ai-apply') {
      if (!suggestion) return;
      const s = suggestion.suggestions;
      const changed = suggestion.changed || {};
      const fields = [['kb-case-symptom', 'symptom'], ['kb-case-cause', 'cause'],
        ['kb-case-solution', 'solution'], ['kb-case-prevention', 'prevention']];
      for (const [id, key] of fields) {
        if (changed[key] && s[key]) mask.querySelector(`#${id}`).value = s[key];
      }
      switchTab('basic');
      toast('已填入有改动的字段，请核对事实后保存', 'ok');
      return;
    }
    if (act === 'ai-polish') {
      setBusy(btn, true, 'AI 优化中…');
      try {
        suggestion = await api(`/api/kb/cases/${encodeURIComponent(caseData.id)}/ai-polish`, {
          method: 'POST', body: {},
        });
        renderAiPanel();
        toast('AI 已生成建议，请检查后再保存', 'ok');
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        setBusy(btn, false);
      }
      return;
    }
    if (act !== 'save') return;
    const body = {
      title: mask.querySelector('#kb-case-title').value.trim(),
      symptom: mask.querySelector('#kb-case-symptom').value.trim(),
      cause_detail: mask.querySelector('#kb-case-cause').value.trim(),
      solution: mask.querySelector('#kb-case-solution').value.trim(),
      prevention: mask.querySelector('#kb-case-prevention').value.trim(),
      reason: mask.querySelector('#kb-case-reason').value.trim(),
    };
    if (!body.reason) {
      switchTab('more');
      mask.querySelector('#kb-case-reason').focus();
      toast('请填写变更原因', 'error');
      return;
    }
    setBusy(btn, true, '保存中…');
    try {
      const saved = await api(`/api/kb/cases/${encodeURIComponent(caseData.id)}`, { method: 'PUT', body });
      toast(`已保存，新增修订 r${saved.case?.revision || ''}`, 'ok');
      m.close();
      const target = `#/kb/docs/${encodeURIComponent(docId)}?tab=revisions&case=${encodeURIComponent(caseData.id)}`;
      // 从“修订记录”页再次保存时 hash 完全相同，浏览器不会触发 hashchange，
      // 会导致表格和计数停留在旧数据；这里主动重新走一次路由。
      if (location.hash === target) window.dispatchEvent(new Event('hashchange'));
      else location.hash = target;
    } catch (err) {
      toast(err.message, 'error');
      setBusy(btn, false);
    }
  });
}

export async function renderKbDoc(docId) {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const validTabs = ['overview', 'cases', 'edit', 'versions', 'revisions'];
  const activeTab = validTabs.includes(params.get('tab')) ? params.get('tab') : 'cases';
  const focusCase = params.get('case') || '';
  const view = mount('知识库文档详情', '案例、原文与修订记录；引用可回溯到这里', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let data;
  try { data = (await api(`/api/kb/docs/${encodeURIComponent(docId)}`)).doc; }
  catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const admin = state.user?.role === 'admin';
  const doc = data;
  let versionItems = [];
  let revisionItems = [];
  if (admin) {
    const [versions, revisions] = await Promise.all([
      api(`/api/kb/docs/${encodeURIComponent(docId)}/versions`).catch(() => ({ items: [] })),
      api(`/api/kb/docs/${encodeURIComponent(docId)}/revisions?limit=200`).catch(() => ({ items: [] })),
    ]);
    versionItems = versions.items || [];
    revisionItems = revisions.items || [];
  }
  const versionsText = (doc.versions || []).map(versionLabel).filter(Boolean).join('、') || '—';
  const activeCases = (doc.cases || []).filter((c) => c.status === 'active');
  const disabledCases = (doc.cases || []).filter((c) => c.status !== 'active');
  const tabs = [
    ['overview', '概览'], ['cases', `案例 ${doc.cases?.length || 0}`],
    ['edit', '编辑原文'], ['versions', `版本 ${versionItems.length}`],
    ['revisions', `修订记录 ${revisionItems.length}`],
  ];
  view.innerHTML = `
    <div class="inline" style="margin-bottom:12px"><a class="btn btn-sm" href="#/kb/docs">← 返回文档库</a>
      <a class="btn btn-sm" href="#/kb">${icon('search')} 去提问</a>
      ${admin ? `<span class="badge ${STATUS_BADGE[doc.status] || ''}">${esc(STATUS_LABEL[doc.status] || doc.status)}</span>` : ''}
    </div>
    <div class="card">
      <div class="card-head"><h2>${esc(doc.title)}</h2>
        <span class="sub">${esc(doc.id)} · ${esc(doc.source_type)} · ${esc(doc.source_name || '')}</span></div>
      <div class="card-body">
        <div class="inline" style="gap:8px;flex-wrap:wrap">
          <span class="badge info">${activeCases.length} 个生效案例</span>
          ${disabledCases.length ? `<span class="badge warn">${disabledCases.length} 个已停用（可在案例页恢复）</span>` : ''}
          <span class="badge">${scoreText(doc.quality)} 质量</span>
          <span class="tiny muted" style="margin-left:auto">更新 ${esc(doc.updated_at ? fmtDate(doc.updated_at) : '—')}</span>
        </div>
        <details class="kb-guide" style="margin-top:10px"><summary>这个页面怎么用</summary>
          <div class="tiny muted" style="margin-top:6px">① 先看“案例”核对现象/原因/方案；② 需要改动就编辑单条案例（支持 AI 优化），保存后到“修订记录”核对；③ 停用的案例不会消失，可在案例页重新启用；④ 原文改动会生成新版本，旧版本仍可恢复；⑤ “变更与审计”里的对象可直接点进本文档。</div>
        </details>
      </div>
    </div>
    <nav class="tabs detail-tabs" id="kb-doc-tabs" style="margin-top:14px">${tabs.map(
      ([key, label]) => `<button type="button" class="${activeTab === key ? 'active' : ''}" data-doc-tab="${key}">${label}</button>`).join('')}</nav>

    <div class="kb-doc-pane" data-pane="overview" ${activeTab === 'overview' ? '' : 'hidden'}>
      <div class="card" style="margin-top:10px"><div class="card-body">
        <div class="kv">
          <div class="k">板卡</div><div class="v">${esc((doc.boards || []).join('、') || '未识别')}</div>
          <div class="k">领域</div><div class="v">${esc((doc.domains || []).join('、') || '未识别')}</div>
          <div class="k">版本</div><div class="v mono tiny">${esc(versionsText)}</div>
          <div class="k">质量 / 置信度</div><div class="v">${scoreText(doc.quality)} / ${scoreText(doc.confidence)}</div>
          <div class="k">PII</div><div class="v">${doc.pii_masked ? '已脱敏（原文仅管理员可读）' : '未脱敏'}</div>
          <div class="k">创建 / 更新</div><div class="v tiny">${esc(doc.created_at || '')} / ${esc(doc.updated_at || '')}</div>
        </div>
        ${admin ? `<div class="inline" style="margin-top:12px;flex-wrap:wrap">
          <button class="btn btn-sm" data-act="toggle-status">${doc.status === 'active' ? '停用文档' : '启用文档'}</button>
          <button class="btn btn-sm" data-act="reindex">重建索引</button>
          <button class="btn btn-sm" data-act="repair-symptoms">修复现象重复标题</button>
          <button class="btn btn-sm btn-danger" data-act="delete">删除文档</button></div>
          <div class="tiny muted" style="margin-top:6px">停用只影响检索，不删除数据；“修复现象重复标题”会批量清理历史导入造成的标题重复，并写审计。</div>` : ''}
      </div></div>
    </div>

    <div class="kb-doc-pane" data-pane="cases" ${activeTab === 'cases' ? '' : 'hidden'}>
      ${(doc.cases || []).length ? `<div class="card" style="margin-top:10px"><div class="card-head"><h2>案例（${doc.cases.length}）</h2>
        <span class="sub">${doc.cases.some((c) => c.is_derived) ? '含多原因派生案例' : '单案例'}${disabledCases.length ? ` · 停用 ${disabledCases.length} 个可恢复` : ''}</span></div>
        <div class="card-body kb-doc-cases">${doc.cases.map((c, i) => `<div class="kb-doc-case ${c.status !== 'active' ? 'disabled' : ''} ${focusCase === c.id ? 'focus' : ''}" id="case-${esc(c.id)}" data-case-id="${esc(c.id)}">
          <div class="inline" style="flex-wrap:wrap"><span class="kb-cite">K${i + 1}</span><strong>${esc(c.title)}</strong>
            <span class="badge ${c.status === 'active' ? 'ok' : 'warn'}">${c.status === 'active' ? '生效' : c.status === 'disabled' ? '已停用' : esc(c.status)}</span>
            <span class="badge ${c.review_status === 'pass' ? 'ok' : c.review_status === 'reject' ? 'danger' : 'info'}">${esc(KB_REVIEW_STATUS_LABEL[c.review_status] || c.review_status || '待审核')}</span>
            <span class="badge info">r${c.revision || 1}</span>
            ${admin ? `<span style="margin-left:auto"></span><button class="btn btn-sm" data-act="case-rev" data-id="${esc(c.id)}" data-title="${esc(c.title)}">修订</button>
              <button class="btn btn-sm" data-act="case-edit" data-id="${esc(c.id)}">编辑</button>
              <button class="btn btn-sm" data-act="case-status" data-id="${esc(c.id)}" data-status="${esc(c.status || 'active')}">${c.status === 'active' ? '停用' : '恢复启用'}</button>` : ''}
            <span class="badge info" style="margin-left:${admin ? '0' : 'auto'}">${scoreText(c.quality)}</span></div>
          <div class="kv" style="margin-top:10px">
            <div class="k">现象</div><div class="v">${esc(c.symptom || '—')}</div>
            <div class="k">环境/复现</div><div class="v">${esc(c.environment || '—')} ${esc(c.repro || '')}</div>
            <div class="k">原因</div><div class="v">${esc(c.cause_detail || c.cause || '—')}</div>
            <div class="k">处理方案</div><div class="v">${esc(c.solution || '—')}</div>
            <div class="k">预防</div><div class="v">${esc(c.prevention || '—')}</div>
          </div>
          ${(c.sections || []).length ? `<details><summary class="tiny muted">查看分节原文（${c.sections.length}）</summary>
            ${c.sections.map((s) => `<div class="kb-section"><div class="k">${esc(s.label || s.kind)}</div><div>${esc(s.text)}</div></div>`).join('')}</details>` : ''}
        </div>`).join('')}</div></div>` : `<div class="card" style="margin-top:10px"><div class="card-body small muted">还没有案例。可以编辑原文重新解析。</div></div>`}
    </div>

    <div class="kb-doc-pane" data-pane="edit" ${activeTab === 'edit' ? '' : 'hidden'}>
      ${admin ? `<div class="card" style="margin-top:10px"><div class="card-head"><h2>编辑标题与原文</h2>
        <span class="sub">保存后按【标签】重新解析案例并生成新版本；建议单条修正走“案例”页</span></div>
        <div class="card-body"><form id="kb-doc-edit">
          <div class="form-row"><label>标题</label><input class="input" name="title" value="${esc(doc.title)}"></div>
          <div class="form-row"><label>正文</label><textarea class="input" name="raw_text" rows="14">${esc(doc.raw_text || '')}</textarea>
            <div class="tiny muted">模板标签示例：【详细描述】【原因分析】【处理方案】【故障建议】。标题不要再写进【详细描述】。</div></div>
          <div class="inline"><button class="btn btn-primary" type="button" data-act="save-doc">保存并重建</button>
            <a class="btn btn-sm" href="#/kb/docs?tab=import">去导入页批量更新</a></div>
        </form></div></div>` : `<div class="alert info">仅管理员可编辑原文。</div>`}
    </div>

    <div class="kb-doc-pane" data-pane="versions" ${activeTab === 'versions' ? '' : 'hidden'}>
      ${admin && versionItems.length ? `<div class="card" style="margin-top:10px">
        <div class="card-head"><h2>版本历史（${versionItems.length}）</h2><span class="sub">恢复旧版本会重建案例；当前版本不会丢失</span></div>
        <div class="card-body tight table-wrap"><table class="data responsive"><thead><tr>
          <th>版本</th><th>时间</th><th>SHA256</th><th>操作</th></tr></thead><tbody>
          ${versionItems.map((v) => `<tr><td data-label="版本">v${esc(String(v.version))}</td>
            <td data-label="时间" class="tiny">${esc(v.created_at ? fmtDate(v.created_at) : '—')}</td>
            <td data-label="SHA256" class="mono tiny">${esc((v.sha256 || '').slice(0, 12) || '—')}</td>
            <td data-label="操作"><button class="btn btn-sm" data-act="restore-version" data-id="${esc(v.id)}">恢复</button></td></tr>`).join('')}
        </tbody></table></div></div>` : `<div class="card" style="margin-top:10px"><div class="card-body small muted">暂无版本记录。</div></div>`}
    </div>

    <div class="kb-doc-pane" data-pane="revisions" ${activeTab === 'revisions' ? '' : 'hidden'}>
      ${revisionItems.length ? `<div class="card" style="margin-top:10px">
        <div class="card-head"><h2>修订记录（${revisionItems.length}）</h2><span class="sub">每次案例编辑都会追加一条，最新在上；字段 diff 可展开</span></div>
        <div class="card-body tight table-wrap"><table class="data responsive"><thead><tr>
          <th>时间</th><th>案例</th><th>动作</th><th>操作人</th><th>原因</th><th>字段变化</th></tr></thead><tbody>
          ${revisionItems.map((r) => `<tr class="${focusCase && r.case_id === focusCase ? 'focus-row' : ''}">
            <td data-label="时间" class="tiny">${esc(r.created_at ? fmtDate(r.created_at) : '—')}</td>
            <td data-label="案例"><a href="#/kb/docs/${encodeURIComponent(doc.id)}?tab=cases&case=${encodeURIComponent(r.case_id || '')}">${esc(r.case_title || r.case_id || '—')}</a>
              <div class="tiny muted">r${esc(String(r.revision || ''))}</div></td>
            <td data-label="动作"><span class="badge ${r.action === 'status' ? 'warn' : 'info'}">${esc(KB_REV_ACTION_LABEL[r.action] || r.action || '')}</span></td>
            <td data-label="操作人" class="tiny">${esc(r.actor_name || '—')}</td>
            <td data-label="原因" class="tiny">${esc(r.reason || '—')}</td>
            <td data-label="字段变化" class="tiny">${(r.differences || []).length
              ? `<details><summary>${(r.differences || []).map((d) => esc(d.field)).slice(0, 4).join('、')}${(r.differences || []).length > 4 ? ' …' : ''}</summary><pre class="kb-raw">${esc(JSON.stringify(r.differences, null, 2))}</pre></details>`
              : '—'}</td></tr>`).join('')}
        </tbody></table></div></div>` : `<div class="card" style="margin-top:10px"><div class="card-body small muted">暂无案例修订记录。</div></div>`}
    </div>

    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>引用：</strong><span class="mono">faultkb://case/&lt;case_id&gt;</span>；页面链接为 <span class="mono">#/kb/docs/${esc(doc.id)}</span>。
      普通用户看不到原始文本，回答和工具也只使用脱敏文本。
    </div></div>`;

  const switchDocTab = (key) => {
    view.querySelectorAll('[data-doc-tab]').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.docTab === key);
    });
    view.querySelectorAll('.kb-doc-pane').forEach((pane) => {
      pane.hidden = pane.dataset.pane !== key;
    });
    const url = new URL(location.href);
    url.hash = `#/kb/docs/${encodeURIComponent(doc.id)}?tab=${key}${focusCase ? `&case=${encodeURIComponent(focusCase)}` : ''}`;
    history.replaceState(null, '', url.hash);
  };
  view.querySelectorAll('[data-doc-tab]').forEach((btn) => {
    btn.addEventListener('click', () => switchDocTab(btn.dataset.docTab));
  });
  if (focusCase) {
    const target = view.querySelector(`#case-${CSS.escape(focusCase)}`);
    if (target) setTimeout(() => target.scrollIntoView({ behavior: 'smooth', block: 'center' }), 60);
  }

  bindActions(view, {
    'toggle-status': async (el) => {
      const next = doc.status === 'active' ? 'disabled' : 'active';
      setBusy(el, true, '处理中…');
      try { await api(`/api/kb/docs/${encodeURIComponent(doc.id)}/status`, { method: 'POST', body: { status: next } }); toast('已更新', 'ok'); renderKbDoc(docId); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    reindex: async (el) => {
      setBusy(el, true, '重建中…');
      try { await api('/api/kb/reindex', { method: 'POST', body: {} }); toast('索引已重建', 'ok'); }
      catch (err) { toast(err.message, 'error'); }
      finally { setBusy(el, false); }
    },
    'repair-symptoms': async (el) => {
      if (!(await confirmDialog('批量清理全库“现象以标题开头”的历史数据？只改现象字段并写审计。'))) return;
      setBusy(el, true, '修复中…');
      try {
        const r = await api('/api/kb/maintenance/repair-symptoms', { method: 'POST', body: {} });
        toast(`扫描 ${r.scanned || 0} 条，修复 ${r.fixed || 0} 条`, 'ok');
        renderKbDoc(docId);
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'restore-version': async (el) => {
      if (!(await confirmDialog('恢复该历史版本？当前版本会保留，可再次恢复。'))) return;
      setBusy(el, true, '恢复中…');
      try {
        await api(`/api/kb/docs/${encodeURIComponent(docId)}/versions/${encodeURIComponent(el.dataset.id)}/restore`,
          { method: 'POST', body: {} });
        toast('已恢复历史版本', 'ok'); renderKbDoc(docId);
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    delete: async (el) => {
      if (!(await confirmDialog('删除后该文档不再参与检索，但版本记录保留。确认删除？', { danger: true, okText: '删除' }))) return;
      setBusy(el, true, '删除中…');
      try { await api(`/api/kb/docs/${encodeURIComponent(doc.id)}`, { method: 'DELETE' }); toast('已删除', 'ok'); location.hash = '#/kb/docs'; }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'case-rev': (el) => openCaseRevisions(el.dataset.id, doc.id, el.dataset.title),
    'case-edit': (el) => {
      const c = (doc.cases || []).find((x) => x.id === el.dataset.id);
      if (c) openCaseEdit(c, doc.id);
    },
    'case-status': async (el) => {
      const next = el.dataset.status === 'active' ? 'disabled' : 'active';
      setBusy(el, true, '处理中…');
      try {
        await api(`/api/kb/cases/${encodeURIComponent(el.dataset.id)}/status`,
          { method: 'POST', body: { status: next, reason: next === 'active' ? '管理员恢复启用' : '管理员停用' } });
        toast(next === 'active' ? '案例已恢复启用' : '案例已停用，可在列表中恢复', 'ok');
        history.replaceState(null, '', `#/kb/docs/${encodeURIComponent(doc.id)}?tab=cases`);
        renderKbDoc(docId);
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'save-doc': async (el) => {
      const form = view.querySelector('#kb-doc-edit');
      const title = form.querySelector('[name="title"]').value.trim();
      const raw = form.querySelector('[name="raw_text"]').value;
      setBusy(el, true, '保存中…');
      try {
        await api(`/api/kb/docs/${encodeURIComponent(doc.id)}`, { method: 'PUT', body: { title, raw_text: raw } });
        toast('已保存并重建索引，可在“版本”页查看新版本', 'ok');
        history.replaceState(null, '', `#/kb/docs/${encodeURIComponent(doc.id)}?tab=versions`);
        renderKbDoc(docId);
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
  return view;
}
