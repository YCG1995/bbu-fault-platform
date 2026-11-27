// 浏览器代理模型常驻循环：只执行服务端序列化好的请求，并回传原始响应。
// 这里不构造 prompt、不解析模型输出、不做 Schema 校验，也不记录 Key。
import { state } from './core.js';

const WINDOW_KEY = 'bbu.relay.window_id';
const MAX_BACKOFF = 30000;

let running = false;
let ready = false;
let relayEnabled = true;
let allowHttp = false;
let models = [];
let maxParallel = 1;
let active = 0;
let heartbeatSeq = 0;
let permissionError = false;
let lastBadge = ['muted', '浏览器代理 未连接'];
let statusAbort = null;

function getWindowId() {
  try {
    const stored = sessionStorage.getItem(WINDOW_KEY);
    if (stored) return stored;
    const id = (crypto.randomUUID ? crypto.randomUUID() : `w-${Date.now()}-${Math.random().toString(16).slice(2)}`);
    sessionStorage.setItem(WINDOW_KEY, id);
    return id;
  } catch {
    return `w-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
}

function clearWindowId() {
  try { sessionStorage.removeItem(WINDOW_KEY); } catch { /* ignore */ }
}

function badge(text, tone = 'muted', title = '') {
  lastBadge = [tone, text];
  const el = document.getElementById('relay-pill');
  if (!el) return;
  el.className = `badge ${tone} relay-pill`;
  el.innerHTML = `<i class="dot"></i>${text}`;
  el.title = title || text;
  el.dataset.tone = tone;
}

function restoreBadge() {
  badge(lastBadge[1], lastBadge[0]);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}

async function readError(resp) {
  try {
    const data = await resp.json();
    const err = new Error(data.error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.code = data.detail?.code || '';
    return err;
  } catch {
    const err = new Error(`HTTP ${resp.status}`);
    err.status = resp.status;
    return err;
  }
}

function stopLoops() {
  running = false;
  ready = false;
  heartbeatSeq += 1;
  if (statusAbort) { try { statusAbort.abort(); } catch { /* ignore */ } statusAbort = null; }
}

function handleWindowLost() {
  stopLoops();
  permissionError = true;
  clearWindowId();
  badge('浏览器代理 已被接管', 'warn', '另一个标签页接管了浏览器代理；刷新本页可重新连接');
}

function handleUnauthorized() {
  stopLoops();
  badge('浏览器代理 未连接', 'muted', '登录会话已失效');
}

function statusBackoff(attempt) {
  return Math.min(MAX_BACKOFF, 2000 * (2 ** Math.max(0, attempt - 1)));
}

async function heartbeatLoop(token) {
  let attempt = 0;
  while (running && token === heartbeatSeq) {
    if (document.hidden) {
      // 后台标签页仍保持租约，太长的定时器会被浏览器节流，这里用较短间隔。
    }
    try {
      statusAbort = new AbortController();
      const timer = setTimeout(() => statusAbort?.abort(), 15000);
      const resp = await fetch(`/api/relay/status?window_id=${encodeURIComponent(getWindowId())}`,
        { credentials: 'same-origin', cache: 'no-store', signal: statusAbort.signal });
      clearTimeout(timer);
      statusAbort = null;
      if (!running || token !== heartbeatSeq) return;
      if (resp.status === 401) { handleUnauthorized(); return; }
      if (resp.status === 409) { handleWindowLost(); return; }
      if (resp.status === 403) {
        const err = await readError(resp);
        stopLoops();
        badge('浏览器代理 已关闭', 'muted', err.message);
        return;
      }
      if (!resp.ok) throw await readError(resp);
      const data = await resp.json();
      if (!running || token !== heartbeatSeq) return;
      attempt = 0;
      ready = true;
      relayEnabled = !!data.enabled;
      allowHttp = !!data.allow_http;
      models = Array.isArray(data.models) ? data.models : [];
      maxParallel = Math.max(1, Math.min(4,
        ...models.map((m) => Number(m.max_concurrent) || 2)));
      if (!relayEnabled) {
        badge('浏览器代理 已关闭', 'muted', '管理员已在运行时配置关闭浏览器代理');
        ready = false;
        await delay(15000);
        continue;
      }
      if (!models.length) {
        badge('浏览器代理 空闲', 'muted', '本账号当前没有可执行的浏览器代理模型');
      } else {
        badge('浏览器代理 运行中', 'ok',
          `当前窗口可承接 ${models.length} 个浏览器代理模型；请保持本页打开`);
      }
      permissionError = false;
      const poll = Math.max(1, Math.min(60, Number(data.poll_sec) || 5));
      await delay(poll * 1000);
    } catch (err) {
      if (!running || token !== heartbeatSeq) return;
      if (err?.name === 'AbortError') { await delay(1000); continue; }
      attempt += 1;
      ready = false;
      if (err?.status === 401) { handleUnauthorized(); return; }
      if (err?.status === 409) { handleWindowLost(); return; }
      badge('浏览器代理 未连接', 'danger',
        err?.message ? `连接失败：${err.message}` : '无法连接平台中继接口');
      await delay(statusBackoff(attempt));
    }
  }
}

async function claimLoop(token) {
  let attempt = 0;
  while (running && token === heartbeatSeq) {
    if (!ready || !relayEnabled || !models.length || permissionError) {
      await delay(800);
      continue;
    }
    if (active >= maxParallel) {
      await delay(400);
      continue;
    }
    try {
      const resp = await fetch(`/api/relay/calls?window_id=${encodeURIComponent(getWindowId())}&wait=10`,
        { credentials: 'same-origin', cache: 'no-store' });
      if (!running || token !== heartbeatSeq) return;
      if (resp.status === 204) { attempt = 0; await delay(300); continue; }
      if (resp.status === 401) { handleUnauthorized(); return; }
      if (resp.status === 409) { handleWindowLost(); return; }
      if (resp.status === 403) {
        const err = await readError(resp);
        permissionError = true;
        badge('浏览器代理 不可用', 'warn', err.message);
        await delay(3000);
        continue;
      }
      if (!resp.ok) throw await readError(resp);
      const call = await resp.json();
      attempt = 0;
      active += 1;
      // 不 await：在 maxParallel 以内继续领取下一条，浏览器可并行 fetch。
      executeCall(call).catch(() => {}).finally(() => { active = Math.max(0, active - 1); });
    } catch (err) {
      if (!running || token !== heartbeatSeq) return;
      attempt += 1;
      if (err?.status === 401) { handleUnauthorized(); return; }
      if (err?.status === 409) { handleWindowLost(); return; }
      badge('浏览器代理 未连接', 'danger',
        err?.message ? `连接失败：${err.message}` : '无法领取模型调用');
      await delay(statusBackoff(attempt));
    }
  }
}

function isForbiddenHost(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/^\[]|\]$/g, '');
  if (!host) return true;
  if (host === 'localhost' || host === 'localhost.localdomain' || host.endsWith('.localhost')) return true;
  if (host === '0.0.0.0' || host === '::' || host === '::1') return true;
  if (host.startsWith('127.')) return true;
  if (host.startsWith('169.254.')) return true;
  // fe80::/10：fe80–febf
  const v6 = host.match(/^([0-9a-f]{1,4}):/);
  if (v6) {
    const value = parseInt(v6[1], 16);
    if (value >= 0xfe80 && value <= 0xfebf) return true;
  }
  return false;
}

function safeTarget(rawUrl) {
  let target;
  try { target = new URL(rawUrl, location.href); } catch { throw new Error('模型地址格式非法'); }
  if (!['https:', 'http:'].includes(target.protocol) ||
      (target.protocol === 'http:' && !allowHttp)) {
    throw new Error('浏览器代理只允许 HTTPS 模型地址');
  }
  if (target.username || target.password || isForbiddenHost(target.hostname)) {
    throw new Error('模型地址被浏览器安全策略拒绝');
  }
  return target.toString();
}

function byteLength(text) {
  try { return new Blob([text]).size; } catch { return new TextEncoder().encode(text).length; }
}

async function postResult(callId, payload) {
  let lastError = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const resp = await fetch(`/api/relay/calls/${encodeURIComponent(callId)}/result`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF': state.csrf || '' },
        body: JSON.stringify(payload),
      });
      if (resp.status === 409 || resp.status === 410) return;
      if (!resp.ok) throw await readError(resp);
      return;
    } catch (err) {
      lastError = err;
      if (err?.status === 401) { handleUnauthorized(); return; }
      if (err?.status === 409 || err?.status === 410) return;
      if (attempt === 0) await delay(600);
    }
  }
  // 失败已由服务端执行超时兜底；不能把 Key 或响应正文写进 console。
  void lastError;
}

async function executeCall(call) {
  const started = performance.now();
  let status = 0;
  let bodyText = '';
  let errorKind = '';
  let truncated = false;
  let controller = null;
  let timer = null;
  try {
    if (!call || !call.call_id || !call.url) throw new Error('调用 payload 不完整');
    const target = safeTarget(call.url);
    const method = String(call.method || 'POST').toUpperCase();
    if (method !== 'GET' && method !== 'POST') throw new Error('不支持的 HTTP 方法');
    const headers = {};
    Object.entries(call.headers || {}).forEach(([key, value]) => {
      if (key && value != null) headers[key] = String(value);
    });
    let authHeader = '';
    if (call.auth && call.auth.header && call.auth.value != null) {
      authHeader = String(call.auth.header);
      headers[authHeader] = String(call.auth.value);
    }
    // 这里是 Key 在浏览器内存中的最后一处；fetch 调用后立刻从 payload 擦除。
    const init = {
      method, headers, credentials: 'omit', mode: 'cors', cache: 'no-store', redirect: 'follow',
    };
    if (method !== 'GET' && call.body) init.body = String(call.body);
    controller = new AbortController();
    const timeoutMs = Math.max(1000, Math.min(180000, Number(call.timeout_sec || 90) * 1000));
    timer = setTimeout(() => controller.abort('relay-timeout'), timeoutMs);
    const fetchPromise = fetch(target, { ...init, signal: controller.signal });
    call.auth = null;
    const resp = await fetchPromise;
    status = resp.status;
    const maxBytes = Math.max(1024, Number(call.max_bytes) || 1048576);
    const declared = Number(resp.headers.get('Content-Length') || 0);
    if (declared && declared > maxBytes) {
      truncated = true;
      try { await resp.body?.cancel(); } catch { /* ignore */ }
    } else {
      bodyText = await resp.text();
      if (byteLength(bodyText) > maxBytes) {
        truncated = true;
        bodyText = '';
      }
    }
  } catch (err) {
    status = 0;
    truncated = false;
    if (err?.name === 'AbortError') {
      errorKind = controller?.signal?.reason === 'relay-timeout' ? 'timeout' : 'abort';
    } else if (err?.name === 'TimeoutError') {
      errorKind = 'timeout';
    } else {
      errorKind = 'network';
    }
    bodyText = '';
  } finally {
    if (timer) clearTimeout(timer);
  }
  // 经服务端仍会把 body_text 当不可信输入做 Schema/错误映射；这里只回传原始事实。
  await postResult(call.call_id, {
    status,
    body_text: bodyText,
    latency_ms: Math.max(0, Math.round(performance.now() - started)),
    error_kind: errorKind,
    truncated,
  });
}

export function startRelay() {
  if (!state.user) return;
  if (running && !permissionError) { restoreBadge(); return; }
  stopLoops();
  running = true;
  ready = false;
  permissionError = false;
  const token = ++heartbeatSeq;
  badge('浏览器代理 连接中', 'muted', '正在与平台建立浏览器代理租约');
  heartbeatLoop(token).catch(() => {});
  claimLoop(token).catch(() => {});
}

export function stopRelay() {
  stopLoops();
  badge('浏览器代理 未连接', 'muted');
}

function releaseOnPageHide() {
  if (!running) return;
  try {
    const payload = JSON.stringify({ window_id: getWindowId() });
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/relay/release',
        new Blob([payload], { type: 'application/json' }));
    }
  } catch { /* 未送达时由服务端 TTL 兜底 */ }
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', releaseOnPageHide);
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && state.user) startRelay();
  });
}
