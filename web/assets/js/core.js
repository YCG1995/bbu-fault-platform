// 平台前端核心：状态、API、布局、弹窗、提示、通用格式化。
export const state = {
  user: null,
  csrf: null,
  publicConfig: {},
  sessionExpires: null,
  page: '',
  onUnauthorized: null,
  onPasswordRequired: null,
};

const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
  layers: '<path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="m3 13 9 5 9-5"/><path d="m3 18 9 5 9-5"/>',
  flask: '<path d="M9 3h6M10 3v6l-5.5 9.2A2 2 0 0 0 6.2 21h11.6a2 2 0 0 0 1.7-2.8L14 9V3"/><path d="M7.5 15h9"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="10" y="10" width="4" height="4"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 20c.4-3.5 2.6-5.4 5.5-5.4s5.1 1.9 5.5 5.4"/><circle cx="17" cy="9.5" r="2.5"/><path d="M16 14.8c2.7 0 4.5 1.7 4.8 4.7"/>',
  shield: '<path d="M12 3 5 6v5.5c0 4.4 3 8 7 9.5 4-1.5 7-5.1 7-9.5V6l-7-3Z"/><path d="m9 12 2 2 4-4"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  chat: '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-5a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z"/><path d="M8 12h.01M12 12h.01M16 12h.01"/>',
  send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7Z"/>',
  key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m11 12 8-8M16 4l4 4M14 6l3 3"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
  logout: '<path d="M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h5"/><path d="M16 17l5-5-5-5M21 12H9"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  download: '<path d="M12 4v12M7 11l5 5 5-5"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  alert: '<path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 9v5M12 17h.01"/>',
  play: '<path d="M7 4l13 8-13 8V4Z"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  file: '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8l-5-5Z"/><path d="M14 3v5h5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  edit: '<path d="M4 20h4l10.5-10.5a2.1 2.1 0 0 0-3-3L5 17v3Z"/><path d="m13.5 6.5 3 3"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/><path d="M10 11v5M14 11v5"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  lock: '<rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  branch: '<circle cx="6" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="12" r="2.5"/><path d="M6 7.5v9M8.5 5H13a2 2 0 0 1 2 2v2.5M8.5 19H13a2 2 0 0 0 2-2v-2.5"/>',
  book: '<path d="M5 4h11a3 3 0 0 1 3 3v13H7a2 2 0 0 1-2-2V4Z"/><path d="M5 18a2 2 0 0 1 2-2h12"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 8v4l3 2"/>',
};

export function icon(name, cls = '') {
  const body = ICONS[name] || ICONS.info;
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

export function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function fmtDate(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString('zh-CN', { hour12: false, year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return iso; }
}

export function fmtShortDate(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

export function fmtSize(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtDur(sec) {
  if (sec == null) return '—';
  const s = Number(sec);
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  if (m < 60) return `${m}m${String(r).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`;
}

export function shortId(id) {
  return id && id.length > 18 ? `${id.slice(0, 12)}…${id.slice(-4)}` : (id || '');
}

const STATUS = {
  queued: ['muted', '排队'],
  running: ['info', '运行中'],
  done: ['ok', '已完成'],
  needs_data: ['warn', '待补充'],
  failed: ['danger', '失败'],
  cancelled: ['muted', '已取消'],
  matched: ['ok', '已匹配'],
  multiple_causes: ['warn', '多根因'],
  unknown_fault: ['warn', '未知故障'],
  unmatched: ['muted', '未匹配'],
  temporal_conflict: ['warn', '时序冲突'],
  insufficient: ['warn', '证据不足'],
  timeout: ['warn', '超时'],
  incompatible: ['danger', '不可用'],
  disabled: ['muted', '已停用'],
  ready: ['ok', '可用'],
  degraded: ['warn', '降级'],
  missing: ['danger', '缺失'],
  skipped: ['muted', '跳过'],
  published: ['ok', '已发布'],
  built: ['info', '已构建'],
  rolled_back: ['warn', '已回退'],
  editing: ['muted', '编辑中'],
  analyzing: ['info', '分析中'],
  analyzed: ['info', '已分析'],
  generating: ['info', '生成中'],
  generated: ['ok', '已生成'],
  needs_confirmation: ['warn', '待确认'],
  checked: ['info', '已校验'],
};

export function statusBadge(status, text) {
  const meta = STATUS[status] || ['muted', status || '未知'];
  return `<span class="badge ${meta[0]}"><i class="dot"></i>${esc(text || meta[1])}</span>`;
}

export function confidenceBadge(conf) {
  if (!conf) return '';
  const map = { high: ['ok', '高置信'], medium: ['warn', '中置信'], low: ['danger', '低置信'] };
  const meta = map[conf] || ['muted', conf];
  return `<span class="badge ${meta[0]}"><i class="dot"></i>${esc(meta[1])}</span>`;
}

export function providerName(p) {
  return { openai: 'OpenAI 兼容', anthropic: 'Anthropic', mock: 'Mock 演示' }[p] || p;
}

const requestGates = new Map();
const inflight = new Set();

export function withBusy(button, fn, { timeoutMs = 30000, minMs = 0 } = {}) {
  if (!button || button.tagName !== 'BUTTON') return Promise.resolve().then(fn);
  const started = Date.now();
  setBusy(button, true);
  let timer = null;
  let timedOut = false;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      timedOut = true;
      const err = new Error(`请求超时（${Math.round(timeoutMs / 1000)}s）`);
      err.name = 'AbortError';
      reject(err);
    }, timeoutMs);
  });
  return Promise.race([Promise.resolve().then(fn), timeout])
    .finally(async () => {
      clearTimeout(timer);
      const elapsed = Date.now() - started;
      if (minMs && elapsed < minMs && !timedOut) {
        await new Promise((resolve) => setTimeout(resolve, minMs - elapsed));
      }
      setBusy(button, false);
    });
}

export function requestGate(key, fn, windowMs = 300) {
  const now = Date.now();
  const gate = requestGates.get(key);
  if (gate) {
    if (gate.inFlight) return gate.inFlight;
    if (now - gate.last < windowMs) return Promise.resolve(null);
  }
  const promise = Promise.resolve().then(fn);
  requestGates.set(key, { last: now, inFlight: promise });
  promise.finally(() => {
    const current = requestGates.get(key);
    if (current && current.inFlight === promise) {
      current.inFlight = null;
      current.last = Date.now();
    }
  }).catch(() => {});
  return promise;
}

export function abortInflight(reason = '请求已取消') {
  for (const item of inflight) {
    try { item.controller.abort(reason); } catch { /* ignore */ }
  }
  inflight.clear();
}

export async function api(path, options = {}) {
  const opts = { credentials: 'same-origin', ...options };
  const headers = new Headers(opts.headers || {});
  let body = opts.body;
  if (body != null && !(body instanceof FormData) && typeof body !== 'string') {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(body);
  }
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes((opts.method || 'GET').toUpperCase()) && state.csrf) {
    headers.set('X-CSRF', state.csrf);
  }
  const method = (opts.method || 'GET').toUpperCase();
  const timeoutMs = Number(opts.timeoutMs || (body instanceof FormData ? 600000 : 30000));
  const actionButton = opts.button || state.activeButton || null;
  const actionKey = opts.gateKey || `${method} ${path} ${state.activeAction || ''}`;
  const controller = new AbortController();
  opts.headers = headers;
  opts.body = body;
  const exec = async () => {
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const marker = { controller, path };
    inflight.add(marker);
    try {
      const res = await fetch(path, {...opts, signal: controller.signal});
      return await handleResponse(res, path, opts.raw);
    } catch (err) {
      if (err && err.name === 'AbortError') {
        const error = new Error(`请求超时（${Math.round(timeoutMs / 1000)}s），请重试`);
        error.name = 'AbortError';
        throw error;
      }
      throw err;
    } finally {
      clearTimeout(timer);
      inflight.delete(marker);
    }
  };
  if (actionButton) {
    return requestGate(actionKey, () => withBusy(actionButton, exec, { timeoutMs }), 300);
  }
  return exec();
}

async function handleResponse(res, path, raw = false) {
  if (res.status === 401 && !path.includes('/api/auth/login')) {
    state.user = null;
    if (state.onUnauthorized) state.onUnauthorized();
    throw new Error('登录已过期，请重新登录');
  }
  const ctype = res.headers.get('Content-Type') || '';
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    let detail = null;
    if (ctype.includes('application/json')) {
      try {
        const data = await res.json();
        message = data.error || message;
        detail = data.detail || null;
      } catch { /* ignore */ }
    }
    const err = new Error(message);
    err.status = res.status;
    err.detail = detail;
    if (detail && detail.code === 'password_change_required' && state.onPasswordRequired) {
      state.onPasswordRequired();
    }
    throw err;
  }
  if (raw) return res;
  if (ctype.includes('application/json')) return res.json();
  if (ctype.includes('text/')) return res.text();
  return res.blob();
}

export function toast(message, type = 'info', timeout = 3800) {
  const stack = document.getElementById('toast-stack');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const iconName = type === 'ok' ? 'check' : type === 'error' ? 'alert' : type === 'warn' ? 'alert' : 'info';
  el.innerHTML = `${icon(iconName)}<div>${esc(message)}</div><button class="close" aria-label="关闭">${icon('x')}</button>`;
  el.querySelector('.close').addEventListener('click', () => el.remove());
  stack.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(-4px)'; }, timeout - 300);
  setTimeout(() => el.remove(), timeout);
}

export function mount(title, subtitle = '', actionsHtml = '') {
  if (state._topbarCleanup) { state._topbarCleanup(); state._topbarCleanup = null; }
  document.getElementById('page-title').textContent = title;
  document.getElementById('page-subtitle').textContent = subtitle || '';
  document.getElementById('page-actions').innerHTML = actionsHtml;
  const view = document.getElementById('view');
  view.innerHTML = '';
  return view;
}

export function bindTopbar(handlers) {
  if (state._topbarCleanup) { state._topbarCleanup(); state._topbarCleanup = null; }
  const root = document.getElementById('page-actions');
  const cleanup = bindActions(root, handlers);
  state._topbarCleanup = cleanup;
  return cleanup;
}

export function navigate(hash) {
  if (location.hash === hash) renderRoute();
  else location.hash = hash;
}

export function bindActions(root, handlers) {
  // 页面重复渲染时，#view 元素本身会被复用；先解绑上一轮监听，避免一次点击触发多次动作
  if (root._actionsCleanup) {
    root._actionsCleanup();
    root._actionsCleanup = null;
  }
  const click = (event) => {
    const el = event.target.closest('[data-act]');
    if (!el || !root.contains(el)) return;
    const action = el.dataset.act;
    if (handlers[action]) {
      event.preventDefault();
      state.activeButton = el;
      state.activeAction = action;
      let result;
      try {
        result = handlers[action](el, event);
      } finally {
        if (!result || typeof result.then !== 'function') {
          state.activeButton = null;
          state.activeAction = null;
        }
      }
      if (result && typeof result.then === 'function') {
        Promise.resolve(result).finally(() => {
          state.activeButton = null;
          state.activeAction = null;
        }).catch(() => {});
      }
    }
  };
  const change = (event) => {
    const el = event.target.closest('[data-change]');
    if (!el || !root.contains(el)) return;
    const action = el.dataset.change;
    if (handlers[action]) handlers[action](el, event);
  };
  root.addEventListener('click', click);
  root.addEventListener('change', change);
  const cleanup = () => {
    root.removeEventListener('click', click);
    root.removeEventListener('change', change);
  };
  root._actionsCleanup = cleanup;
  return cleanup;
}

let modalCloseHandler = null;

export function modal({ title, body = '', footer = '', wide = false, onClose = null }) {
  const root = document.getElementById('modal-root');
  const previousFocus = document.activeElement;
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `<div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">
    <div class="modal-head"><h2>${esc(title)}</h2>
      <button class="btn btn-ghost btn-sm" data-act="close-modal" aria-label="关闭">${icon('x')}</button>
    </div>
    <div class="modal-body"></div>
    <div class="modal-foot"></div>
  </div>`;
  root.appendChild(mask);
  const bodyEl = mask.querySelector('.modal-body');
  const footEl = mask.querySelector('.modal-foot');
  if (typeof body === 'string') bodyEl.innerHTML = body;
  else bodyEl.appendChild(body);
  if (footer) footEl.innerHTML = footer;
  else footEl.remove();
  const focusables = () => [...mask.querySelectorAll(
    'a[href],button:not([disabled]),textarea,input:not([type="hidden"]),select,[tabindex]:not([tabindex="-1"])'
  )].filter((el) => el.offsetParent !== null);
  const close = () => {
    mask.remove();
    if (modalCloseHandler) { document.removeEventListener('keydown', modalCloseHandler); modalCloseHandler = null; }
    if (previousFocus && previousFocus.isConnected) { try { previousFocus.focus(); } catch { /* ignore */ } }
    if (onClose) onClose();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { close(); return; }
    if (e.key !== 'Tab') return;
    const items = focusables();
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (!mask.contains(document.activeElement)) { e.preventDefault(); first.focus(); return; }
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  modalCloseHandler = onKey;
  document.addEventListener('keydown', onKey);
  requestAnimationFrame(() => {
    const preferred = mask.querySelector('[autofocus]') || focusables()[0];
    if (preferred) { try { preferred.focus(); } catch { /* ignore */ } }
  });
  // 只把“按下和抬起都在遮罩本身”视为关闭。输入框内拖选文字后在窗口外
  // 释放鼠标时，click 目标可能变成遮罩，不能因此销毁正在填写的表单。
  let backdropPressed = false;
  mask.addEventListener('pointerdown', (e) => {
    backdropPressed = e.target === mask;
  });
  mask.addEventListener('pointerup', (e) => {
    const shouldClose = backdropPressed && e.target === mask;
    backdropPressed = false;
    if (shouldClose) close();
  });
  mask.addEventListener('click', (e) => {
    if (e.target.closest('[data-act="close-modal"]')) close();
  });
  return { mask, body: bodyEl, foot: footEl, close };
}

export function confirmDialog(message, { title = '请确认', danger = false, okText = '确定' } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const m = modal({
      title,
      body: `<div class="alert ${danger ? 'danger' : 'info'}">${icon('alert')}<div>${esc(message)}</div></div>`,
      footer: `<button class="btn" data-act="cancel">取消</button>
               <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-act="ok">${esc(okText)}</button>`,
      onClose: () => finish(false),
    });
    m.mask.addEventListener('click', (e) => {
      if (e.target.closest('[data-act="cancel"]')) { finish(false); m.close(); }
      if (e.target.closest('[data-act="ok"]')) { finish(true); m.close(); }
    });
  });
}

export function emptyState(iconName, title, text, actionHtml = '') {
  return `<div class="empty">${icon(iconName)}<h3>${esc(title)}</h3><p>${esc(text)}</p>${actionHtml}</div>`;
}

export function loadingState(text = '加载中…') {
  return `<div class="empty"><div class="spin" style="border-color:#cbd5e1;border-top-color:#0f766e"></div><p>${esc(text)}</p></div>`;
}

export function setBusy(button, busy, busyText) {
  if (!button) return;
  button._busyDepth = Number(button._busyDepth || 0);
  if (busy) {
    button._busyDepth += 1;
    if (button._busyDepth === 1) {
      button.dataset.original = button.innerHTML;
      button.dataset.busyText = busyText || button.textContent || '处理中…';
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.classList.add('is-busy');
      button.innerHTML = `<span class="spin"></span>${esc(button.dataset.busyText)}`;
    }
  } else {
    button._busyDepth = Math.max(0, button._busyDepth - 1);
    if (button._busyDepth === 0) {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      button.classList.remove('is-busy');
      if (button.dataset.original) button.innerHTML = button.dataset.original;
      delete button.dataset.original;
      delete button.dataset.busyText;
    }
  }
}

export function formValues(form) {
  const data = {};
  new FormData(form).forEach((value, key) => {
    if (key.endsWith('[]')) {
      const k = key.slice(0, -2);
      (data[k] = data[k] || []).push(value);
    } else data[key] = value;
  });
  return data;
}

export function roleLabel(role) {
  return { admin: '管理员', reviewer: '专家', engineer: '工程师' }[role] || role || '用户';
}

export function renderRoute() {
  // 由 app.js 覆盖
  if (state._router) state._router();
}
