// 平台悬浮小助手：文档问答、消息检索、管理员运维确认卡片。
import { api, esc, icon, modal, setBusy, state, toast } from './core.js';

let mounted = false;
let panel = null;
let fab = null;
let log = null;
let input = null;
let sendBtn = null;
let history = [];
let statusData = null;
let pendingActions = new Map();
let loadingEl = null;

function roleLabel(role) {
  return { admin: '管理员', reviewer: '专家', engineer: '工程师' }[role] || role || '用户';
}

function shortTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

function renderText(text) {
  return esc(String(text || '')).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>');
}

function scrollBottom() {
  if (log) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
}

function setStatusLine() {
  if (!panel) return;
  const badge = panel.querySelector('#assistant-mode');
  if (!badge) return;
  const usable = !!statusData?.model_usable;
  badge.className = `badge ${usable ? 'ok' : 'warn'}`;
  badge.textContent = usable ? '模型在线' : '本地检索';
  badge.title = usable ? `模型来源：${statusData?.model_source || '平台配置'}`
    : '当前没有可用模型，助手仍可检索平台文档和本机消息';
}

function addBubble(role, content, extras = []) {
  if (!log) return null;
  const wrap = document.createElement('div');
  wrap.className = `assistant-msg ${role}`;
  const bubble = document.createElement('div');
  bubble.className = 'assistant-bubble';
  const who = role === 'user' ? '你' : '助手';
  bubble.innerHTML = `<div class="assistant-who">${who}<span>${shortTime(new Date().toISOString())}</span></div>
    <div class="assistant-text">${renderText(content)}</div>`;
  for (const node of extras) if (node) bubble.appendChild(node);
  wrap.appendChild(bubble);
  log.appendChild(wrap);
  requestAnimationFrame(() => {
    // 长回答优先让用户看到结论开头；短回答仍然贴底。
    const rect = wrap.getBoundingClientRect();
    const logRect = log.getBoundingClientRect();
    if (role === 'assistant' && rect.height > log.clientHeight - 24) {
      log.scrollTop = rect.top - logRect.top + log.scrollTop - 6;
    } else {
      scrollBottom();
    }
  });
  return bubble;
}

function makeLinks(links) {
  if (!links?.length) return null;
  const box = document.createElement('div');
  box.className = 'assistant-links';
  for (const link of links.slice(0, 6)) {
    const a = document.createElement('a');
    a.href = link.href;
    a.className = 'assistant-link';
    a.textContent = link.label;
    box.appendChild(a);
  }
  return box;
}

function makeSources(sources) {
  if (!sources?.length) return null;
  const box = document.createElement('div');
  box.className = 'assistant-sources';
  const label = document.createElement('span');
  label.textContent = '来源：';
  box.appendChild(label);
  for (const source of sources.slice(0, 5)) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'assistant-source';
    btn.textContent = source.heading || source.title || source.uri || '文档';
    btn.addEventListener('click', () => modal({
      title: source.title || source.heading || '文档片段',
      body: `<div class="small muted" style="margin-bottom:8px">${esc(source.uri || '')}</div>
        <div class="assistant-source-text">${renderText(source.excerpt || '暂无片段')}</div>`,
      wide: true,
    }));
    box.appendChild(btn);
  }
  return box;
}

function makeTools(toolCalls) {
  if (!toolCalls?.length) return null;
  const box = document.createElement('div');
  box.className = 'assistant-tools';
  for (const call of toolCalls.slice(0, 4)) {
    const details = document.createElement('details');
    details.className = `assistant-tool ${call.ok === false ? 'failed' : ''}`;
    const summary = document.createElement('summary');
    summary.innerHTML = `<span>${esc(call.title || call.tool || '工具')}</span>
      <span class="badge ${call.ok === false ? 'danger' : 'ok'}">${call.ok === false ? '失败' : '完成'}</span>`;
    details.appendChild(summary);
    const body = document.createElement('div');
    body.className = 'assistant-tool-body';
    body.innerHTML = `<div>${esc(call.summary || call.error || '已完成')}</div>`;
    const items = call.items;
    if (Array.isArray(items) && items.length) {
      const list = document.createElement('div');
      list.className = 'assistant-tool-items';
      for (const row of items.slice(0, 4)) {
        if (!row || typeof row !== 'object') continue;
        const title = row.title || row.task_title || row.username || row.id || '';
        const detail = row.body || row.message || row.content || row.action || '';
        const when = row.ts || row.created_at || '';
        const line = document.createElement('div');
        line.innerHTML = `<strong>${esc(title)}</strong>${detail ? ` ${esc(String(detail).slice(0, 160))}` : ''}
          ${when ? `<span class="muted tiny"> · ${esc(when)}</span>` : ''}`;
        list.appendChild(line);
      }
      body.appendChild(list);
    }
    details.appendChild(body);
    box.appendChild(details);
  }
  return box;
}

function makePending(action) {
  const card = document.createElement('div');
  card.className = 'assistant-action';
  card.dataset.actionId = action.id;
  const risk = action.requires_password ? '高风险' : (action.risk_label || '写操作');
  card.innerHTML = `<div class="assistant-action-head">
      <strong>${esc(action.title)}</strong>
      <span class="badge ${action.requires_password ? 'danger' : 'warn'}">${esc(risk)}</span>
    </div>
    <div class="assistant-action-summary">${esc(action.summary)}</div>
    ${action.requires_password ? `<label class="assistant-password"><span>管理员密码</span>
      <input type="password" autocomplete="current-password" placeholder="确认执行前重新输入密码"></label>` : ''}
    <div class="assistant-action-btns">
      <button type="button" class="btn btn-primary btn-sm" data-confirm-action>${icon('check')}确认执行</button>
      <button type="button" class="btn btn-ghost btn-sm" data-cancel-action>取消</button>
    </div>`;
  pendingActions.set(action.id, action);
  return card;
}

function addResponse(response) {
  const extras = [];
  const tools = makeTools(response.tool_calls);
  if (tools) extras.push(tools);
  for (const action of response.pending_actions || []) extras.push(makePending(action));
  const links = makeLinks(response.links);
  if (links) extras.push(links);
  const sources = makeSources(response.sources);
  if (sources) extras.push(sources);
  addBubble('assistant', response.message || '（没有返回内容）', extras);
  if (response.message) history.push({ role: 'assistant', content: response.message });
}

function showLoading() {
  hideLoading();
  if (!log) return;
  loadingEl = document.createElement('div');
  loadingEl.className = 'assistant-msg assistant';
  loadingEl.innerHTML = `<div class="assistant-bubble assistant-loading">
    <span class="spin"></span>正在查平台数据…</div>`;
  log.appendChild(loadingEl);
  scrollBottom();
}

function hideLoading() {
  if (loadingEl) { loadingEl.remove(); loadingEl = null; }
}

async function send() {
  if (!input || !sendBtn) return;
  const text = input.value.trim();
  if (!text) return;
  if (text.length > 2000) { toast('问题最多 2000 字', 'warn'); return; }
  input.value = '';
  input.style.height = 'auto';
  history.push({ role: 'user', content: text });
  if (history.length > 10) history = history.slice(-10);
  addBubble('user', text);
  setBusy(sendBtn, true, '发送中');
  showLoading();
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    const data = await api('/api/assistant/chat', {
      method: 'POST',
      body: { messages: history.slice(-10) },
      signal: controller.signal,
    });
    clearTimeout(timer);
    hideLoading();
    addResponse(data);
  } catch (err) {
    hideLoading();
    const message = err.name === 'AbortError' ? '助手响应超时，请稍后重试' : err.message;
    addBubble('assistant', `抱歉，${message}`);
    toast(message, 'error');
  } finally {
    setBusy(sendBtn, false);
    input.focus();
  }
}

async function confirmAction(card) {
  const action = pendingActions.get(card.dataset.actionId);
  if (!action) return;
  const passwordInput = card.querySelector('input[type="password"]');
  const password = passwordInput ? passwordInput.value : '';
  if (action.requires_password && !password) {
    toast('请输入管理员密码', 'warn');
    if (passwordInput) passwordInput.focus();
    return;
  }
  const btn = card.querySelector('[data-confirm-action]');
  setBusy(btn, true, '执行中');
  try {
    const data = await api(`/api/assistant/actions/${encodeURIComponent(action.id)}/confirm`, {
      method: 'POST',
      body: action.requires_password ? { password } : {},
    });
    pendingActions.delete(action.id);
    card.classList.add('done');
    card.innerHTML = `<div class="assistant-action-head"><strong>${esc(action.title)}</strong>
      <span class="badge ok">已执行</span></div>
      <div class="assistant-action-summary">${esc(data.message || '操作已完成')}</div>`;
    if (data.message) {
      history.push({ role: 'assistant', content: data.message });
      if (history.length > 10) history = history.slice(-10);
    }
    const links = makeLinks(data.links);
    if (links) card.appendChild(links);
    toast('操作已执行', 'ok');
  } catch (err) {
    setBusy(btn, false);
    toast(err.message, 'error');
  }
}

async function cancelAction(card) {
  const action = pendingActions.get(card.dataset.actionId);
  if (!action) return;
  try {
    await api(`/api/assistant/actions/${encodeURIComponent(action.id)}/cancel`, {
      method: 'POST', body: {},
    });
  } catch { /* 已过期也按取消失败处理，不阻断界面 */ }
  pendingActions.delete(action.id);
  card.innerHTML = `<div class="assistant-action-head"><strong>${esc(action.title)}</strong>
    <span class="badge">已取消</span></div>`;
}

function renderWelcome() {
  if (!log) return;
  log.innerHTML = '';
  const card = document.createElement('div');
  card.className = 'assistant-welcome';
  card.innerHTML = `<div class="assistant-welcome-title">${icon('chat')} 平台小助手</div>
    <p>可以问我平台怎么用，也可以查任务、通知、审计和外部 IM 消息。管理员还能做系统运维。</p>
    <div class="assistant-suggestions" id="assistant-suggestions"></div>`;
  log.appendChild(card);
  const box = card.querySelector('#assistant-suggestions');
  for (const item of statusData?.suggestions || []) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'assistant-suggestion';
    btn.textContent = item.label;
    btn.addEventListener('click', () => {
      if (!input) return;
      input.value = item.prompt;
      input.dispatchEvent(new Event('input'));
      input.focus();
    });
    box.appendChild(btn);
  }
}

async function loadStatus() {
  try {
    statusData = await api('/api/assistant/status');
    if (!statusData?.status?.enabled) {
      unmountAssistant();
      return;
    }
    statusData = { ...statusData.status, suggestions: statusData.suggestions || [] };
    setStatusLine();
    const sub = panel?.querySelector('#assistant-sub');
    if (sub) {
      sub.textContent = statusData.model_usable
        ? `已连接模型 · ${statusData.tools} 个工具`
        : `本地检索模式 · ${statusData.tools} 个工具`;
    }
    if (log && !log.querySelector('.assistant-msg')) renderWelcome();
  } catch (err) {
    if (panel) panel.querySelector('#assistant-sub').textContent = err.message;
  }
}

function buildPanel() {
  fab = document.createElement('button');
  fab.type = 'button';
  fab.id = 'assistant-fab';
  fab.className = 'assistant-fab';
  fab.title = '平台小助手';
  fab.setAttribute('aria-label', '打开平台小助手');
  fab.innerHTML = `${icon('chat')}<span>助手</span>`;

  panel = document.createElement('section');
  panel.id = 'assistant-panel';
  panel.className = 'assistant-panel';
  panel.hidden = true;
  panel.innerHTML = `<header class="assistant-head">
      <div class="assistant-head-main">
        <div class="assistant-title">${icon('chat')} 平台小助手</div>
        <div class="assistant-sub" id="assistant-sub">正在读取状态…</div>
      </div>
      <span class="badge" id="assistant-mode">…</span>
      <button type="button" class="btn btn-ghost btn-sm" id="assistant-clear" title="清空对话">${icon('trash')}</button>
      <button type="button" class="btn btn-ghost btn-sm" id="assistant-close" aria-label="收起">${icon('x')}</button>
    </header>
    <div class="assistant-log" id="assistant-log" role="log" aria-live="polite"></div>
    <form class="assistant-compose" id="assistant-form">
      <textarea id="assistant-input" rows="1" maxlength="2000"
        placeholder="问平台用法，或查任务、通知、审计…"></textarea>
      <button type="submit" class="btn btn-primary assistant-send" id="assistant-send" aria-label="发送">${icon('send')}</button>
    </form>`;
  document.body.appendChild(fab);
  document.body.appendChild(panel);
  log = panel.querySelector('#assistant-log');
  input = panel.querySelector('#assistant-input');
  sendBtn = panel.querySelector('#assistant-send');

  const open = () => {
    panel.hidden = false;
    fab.classList.add('open');
    scrollBottom();
    setTimeout(() => input?.focus(), 30);
  };
  const close = () => {
    panel.hidden = true;
    fab.classList.remove('open');
    fab.focus();
  };
  fab.addEventListener('click', () => (panel.hidden ? open() : close()));
  panel.querySelector('#assistant-close').addEventListener('click', close);
  panel.querySelector('#assistant-clear').addEventListener('click', () => {
    history = [];
    pendingActions.clear();
    log.innerHTML = '';
    renderWelcome();
    toast('对话已清空', 'ok');
  });
  panel.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close();
  });

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(120, input.scrollHeight)}px`;
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
  panel.querySelector('#assistant-form').addEventListener('submit', (event) => {
    event.preventDefault();
    send();
  });

  log.addEventListener('click', (event) => {
    const card = event.target.closest('.assistant-action');
    if (!card) return;
    if (event.target.closest('[data-confirm-action]')) confirmAction(card);
    if (event.target.closest('[data-cancel-action]')) cancelAction(card);
  });
  renderWelcome();
}

export async function mountAssistant() {
  if (!state.user) return;
  if (mounted) {
    await loadStatus();
    return;
  }
  mounted = true;
  buildPanel();
  await loadStatus();
}

export function unmountAssistant() {
  if (fab) { fab.remove(); fab = null; }
  if (panel) { panel.remove(); panel = null; }
  mounted = false;
  history = [];
  pendingActions.clear();
  loadingEl = null;
  log = null;
  input = null;
  sendBtn = null;
  statusData = null;
}
