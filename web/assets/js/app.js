// 应用入口：登录、侧边导航、Hash 路由
import {
  api, bindActions, confirmDialog, esc, fmtDate, icon, mount, modal, roleLabel, setBusy, state, toast,
} from './core.js';
import { renderDashboard } from './page_dashboard.js';
import { renderTaskList, renderNewTask } from './page_tasks.js';
import { renderTaskDetail } from './page_task_detail.js';
import { renderSkills } from './page_skills.js';
import { renderWorkshop } from './page_workshop.js';
import { renderModels, renderUsers, renderAudit, renderSystem } from './page_admin.js';
import { renderCases, renderCaseDetail } from './page_cases.js';
import { openPersonalModelModal } from './personal_model.js';
import { mountAssistant, unmountAssistant } from './assistant.js';

const NAV = [
  { href: '#/dashboard', label: '总览', icon: 'dashboard' },
  { href: '#/tasks/new', label: '新建诊断', icon: 'plus' },
  { href: '#/tasks', label: '任务记录', icon: 'list' },
  { href: '#/cases', label: '真实案例', icon: 'inbox' },
  { href: '#/skills', label: '技能库', icon: 'layers' },
  { href: '#/workshop', label: '技能工坊', icon: 'flask' },
  { href: '#/system', label: '系统信息', icon: 'info' },
];
const ADMIN_NAV = [
  { href: '#/models', label: '模型接入', icon: 'cpu' },
  { href: '#/users', label: '用户管理', icon: 'users' },
  { href: '#/audit', label: '审计日志', icon: 'shield' },
];

let loginRoot = null;
let sessionTimer = null;
let reauthOpen = false;
let sessionWarningOpen = false;

function minPwdLength() {
  const value = Number(state.publicConfig?.password_min_length);
  return Number.isFinite(value) && value >= 8 ? value : 10;
}

function passwordPolicyHint() {
  return `至少 ${minPwdLength()} 位，且包含字母、数字、大写、符号中的两类，不能包含用户名`;
}

function rememberedUsername() {
  try { return localStorage.getItem('bbu.username') || ''; } catch { return ''; }
}

function rememberUsername(name, remember) {
  try {
    if (remember) localStorage.setItem('bbu.username', name);
    else localStorage.removeItem('bbu.username');
  } catch { /* ignore */ }
}

function bindPasswordToggles(root) {
  root.querySelectorAll('[data-toggle-pass]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const input = btn.closest('.input-affix')?.querySelector('input');
      if (!input) return;
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.textContent = show ? '隐藏' : '显示';
      btn.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    });
  });
  root.querySelectorAll('[data-caps-hint]').forEach((hint) => {
    const input = hint.closest('.form-row')?.querySelector('input');
    if (!input) return;
    const update = (event) => {
      try { hint.hidden = !event.getModifierState?.('CapsLock'); } catch { hint.hidden = true; }
    };
    input.addEventListener('keydown', update);
    input.addEventListener('keyup', update);
    input.addEventListener('blur', () => { hint.hidden = true; });
  });
}

function loginErrorMessage(err) {
  const retry = Number(err?.detail?.retry_after);
  if (Number.isFinite(retry) && retry > 0) {
    return `${err.message}（约 ${retry} 秒后可重试）`;
  }
  return err.message;
}

function stopSessionTimer() {
  if (sessionTimer) { clearTimeout(sessionTimer); sessionTimer = null; }
}

function scheduleSessionWarning() {
  stopSessionTimer();
  if (!state.user || !state.sessionExpires) return;
  const expires = Date.parse(state.sessionExpires);
  if (Number.isNaN(expires)) return;
  const warnMs = Math.max(30, Number(state.publicConfig?.session_warn_sec) || 120) * 1000;
  const delay = expires - Date.now() - warnMs;
  if (delay <= 0) {
    if (expires <= Date.now()) openReauth('会话已过期，请重新登录');
    else setTimeout(openSessionWarning, 0);
    return;
  }
  sessionTimer = setTimeout(() => {
    if (Date.parse(state.sessionExpires) <= Date.now()) openReauth('会话已过期，请重新登录');
    else openSessionWarning();
  }, delay);
}

function currentPath() {
  return location.hash || '#/dashboard';
}

function updateNav() {
  const path = currentPath();
  document.querySelectorAll('#nav a').forEach((a) => {
    const href = a.getAttribute('href');
    const active = href === path || (href === '#/tasks' && /^#\/tasks\/(?!new$)/.test(path));
    a.classList.toggle('active', active);
  });
}

function renderShell() {
  document.body.classList.remove('login-mode');
  if (loginRoot) { loginRoot.remove(); loginRoot = null; }
  const admin = state.user?.role === 'admin';
  const reviewer = state.user?.role === 'reviewer';
  const baseNav = NAV;
  const nav = document.getElementById('nav');
  nav.innerHTML = `<div class="nav-section">${reviewer ? '诊断与复核' : '诊断'}</div>
    ${baseNav.map((n) => `<a href="${n.href}">${icon(n.icon)}${n.label}</a>`).join('')}
    ${admin ? `<div class="nav-section">管理</div>
    ${ADMIN_NAV.map((n) => `<a href="${n.href}">${icon(n.icon)}${n.label}</a>`).join('')}` : ''}`;
  const display = state.user?.display_name || state.user?.username || '用户';
  document.getElementById('user-menu').innerHTML = `
    <div class="avatar">${esc(display.slice(0, 1).toUpperCase())}</div>
    <div class="user-meta"><strong>${esc(display)}</strong><span>${roleLabel(state.user?.role)}</span></div>
    <button class="btn btn-ghost btn-sm" data-act="search" title="全局搜索">${icon('search')}</button>
    <button class="btn btn-ghost btn-sm" data-act="notifications" title="站内通知" id="notif-btn" style="position:relative">${icon('bell')}<span id="notif-badge" class="badge danger" hidden style="position:absolute;top:-2px;right:-2px;padding:1px 5px;font-size:10px"></span></button>
    <button class="btn btn-ghost btn-sm" data-act="profile" title="个人中心">${icon('users')}</button>
    <button class="btn btn-ghost btn-sm" data-act="password" title="修改密码">${icon('lock')}</button>
    <button class="btn btn-ghost btn-sm" data-act="logout" title="退出登录">${icon('logout')}</button>`;
  document.getElementById('worker-pill').innerHTML = '<span class="dot"></span>调度器在线';
  refreshNotifications();
  startNotificationPoll();
  document.getElementById('version-pill').textContent =
    `${state.publicConfig.brand || 'BBU FaultScope'} · v${state.publicConfig.version || '1.0.0'}`;
  bindTopbar();
  updateNav();
  mountAssistant();
}

function bindTopbar() {
  const topbar = document.getElementById('topbar');
  if (topbar.dataset.bound) return;
  topbar.dataset.bound = '1';
  topbar.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'search') openSearch();
    if (btn.dataset.act === 'notifications') openNotifications();
    if (btn.dataset.act === 'profile') openProfile();
    if (btn.dataset.act === 'password') openChangePassword(false);
    if (btn.dataset.act === 'logout') {
      try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* 已过期也按退出处理 */ }
      state.user = null;
      state.sessionExpires = null;
      unmountAssistant();
      stopSessionTimer();
      toast('已退出登录', 'ok');
      renderLogin();
    }
  });
}

function renderSearchResults(data) {
  const groups = [];
  if (data.tasks?.length) groups.push(`<div class="search-group"><div class="search-group-title">任务</div>
    ${data.tasks.map((r) => `<a class="search-hit" href="${esc(r.link)}"><strong>${esc(r.title)}</strong><span class="tiny muted">${esc(r.id)} · ${esc(r.status)}</span></a>`).join('')}</div>`);
  if (data.cases?.length) groups.push(`<div class="search-group"><div class="search-group-title">真实案例</div>
    ${data.cases.map((r) => `<div class="search-hit static"><strong>${esc(r.title)}</strong><span class="tiny muted">${esc(r.id)} · ${esc(r.status)} · ${esc(r.split)}</span></div>`).join('')}</div>`);
  if (data.skills?.length) groups.push(`<div class="search-group"><div class="search-group-title">技能</div>
    ${data.skills.map((r) => `<a class="search-hit" href="${esc(r.link)}"><strong>${esc(r.title || r.name)}</strong><span class="tiny muted">${esc(r.name)} · v${esc(r.version || '-')} · ${esc(r.status)}</span></a>`).join('')}</div>`);
  if (data.users?.length) groups.push(`<div class="search-group"><div class="search-group-title">用户</div>
    ${data.users.map((r) => `<a class="search-hit" href="${esc(r.link)}"><strong>${esc(r.display_name || r.username)}</strong><span class="tiny muted">${esc(r.username)} · ${esc(roleLabel(r.role))} · ${esc(r.status)}</span></a>`).join('')}</div>`);
  return groups.length ? groups.join('') : `<div class="empty" style="padding:30px">${icon('search')}<h3>没有匹配结果</h3><p>试试任务 ID、标题、案例名、技能名或用户名。</p></div>`;
}

function openSearch() {
  const m = modal({
    title: '全局搜索',
    wide: true,
    body: `<div class="search">${icon('search')}<input class="input" id="global-search" placeholder="任务 ID / 标题 / 案例 / 技能 / 用户" autofocus autocomplete="off"></div>
      <div id="global-results" style="margin-top:14px"><div class="small muted">输入至少 2 个字符开始搜索。结果会按当前账号权限过滤。</div></div>`,
    footer: `<button class="btn" data-act="close">关闭</button>`,
  });
  const input = m.body.querySelector('#global-search');
  const results = m.body.querySelector('#global-results');
  let timer = null;
  let seq = 0;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { results.innerHTML = '<div class="small muted">输入至少 2 个字符开始搜索。</div>'; return; }
    results.innerHTML = '<div class="small muted">搜索中…</div>';
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
        if (mine === seq) results.innerHTML = renderSearchResults(data);
      } catch (err) {
        if (mine === seq) results.innerHTML = `<div class="alert danger">${esc(err.message)}</div>`;
      }
    }, 250);
  });
  m.mask.addEventListener('click', (e) => {
    if (e.target.closest('[data-act="close"]')) m.close();
    if (e.target.closest('.search-hit[href]')) m.close();
  });
  input.focus();
}

let notificationItems = [];
let notificationTimer = null;

function stopNotificationPoll() {
  if (notificationTimer) { clearInterval(notificationTimer); notificationTimer = null; }
}

function startNotificationPoll() {
  stopNotificationPoll();
  if (!state.user) return;
  notificationTimer = setInterval(() => { refreshNotifications().catch(() => {}); }, 30000);
}

async function refreshNotifications() {
  if (!state.user) return;
  try {
    const data = await api('/api/notifications?limit=20');
    notificationItems = data.items || [];
    const badge = document.getElementById('notif-badge');
    if (badge) {
      badge.hidden = !data.unread;
      badge.textContent = data.unread > 99 ? '99+' : String(data.unread || '');
    }
  } catch { /* 通知失败不影响主流程 */ }
}

async function openNotifications() {
  await refreshNotifications().catch(() => {});
  const m = modal({
    title: '站内通知',
    wide: true,
    body: `<div class="between" style="margin-bottom:10px"><span class="small muted">任务完成/失败、复核分配、升级结果与账号状态</span>
      <button class="btn btn-sm" data-act="read-all">全部标为已读</button></div>
      <div class="stack" style="gap:8px" id="notif-list">${notificationItems.length
        ? notificationItems.map((n) => `<div class="notif-item ${n.read ? '' : 'unread'}" data-id="${esc(n.id)}" data-link="${esc(n.link || '')}">
            <div class="between"><strong>${esc(n.title)}</strong><span class="tiny muted">${fmtDate(n.created_at)}</span></div>
            <div class="small muted" style="margin-top:3px">${esc(n.body || '')}</div>
          </div>`).join('')
        : `<div class="empty">${icon('bell')}<h3>暂无通知</h3><p>任务完成、失败或收到复核任务时会在这里提醒。</p></div>`}</div>`,
    footer: `<button class="btn" data-act="close">关闭</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="close"]')) m.close();
    if (e.target.closest('[data-act="read-all"]')) {
      try { await api('/api/notifications/read-all', { method: 'POST' }); toast('已全部标为已读', 'ok'); m.close(); refreshNotifications(); }
      catch (err) { toast(err.message, 'error'); }
    }
    const item = e.target.closest('.notif-item');
    if (item) {
      if (item.classList.contains('unread')) {
        try { await api(`/api/notifications/${encodeURIComponent(item.dataset.id)}/read`, { method: 'POST' }); } catch { /* ignore */ }
      }
      const link = item.dataset.link || '';
      m.close();
      if (link) location.hash = link.replace(/^\/#/, '#');
      refreshNotifications();
    }
  });
}

function renderLogin(message = '') {
  stopSessionTimer();
  stopNotificationPoll();
  reauthOpen = false;
  sessionWarningOpen = false;
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } state.cleanup = null; }
  document.body.classList.add('login-mode');
  document.getElementById('modal-root').innerHTML = '';
  const view = document.getElementById('view');
  view.innerHTML = '';
  if (loginRoot) loginRoot.remove();
  const remembered = rememberedUsername();
  const minLen = minPwdLength();
  const allowSelfRegister = Boolean(state.publicConfig?.allow_self_register);
  loginRoot = document.createElement('div');
  loginRoot.className = 'login-wrap';
  loginRoot.innerHTML = `
    <div class="login-card">
      <div class="login-brand">
        <div class="brand-mark">${icon('activity')}</div>
        <div><strong>BBU FaultScope</strong><span>内网智能故障诊断平台</span></div>
      </div>
      <h2 id="login-title">登录</h2>
      <p class="sub" id="login-sub">使用本地账号登录；所有操作会写入审计日志。</p>
      <form id="login-form">
        <div class="form-row"><label class="label" for="login-user">用户名</label>
          <input class="input" id="login-user" name="username" autocomplete="username" autofocus required value="${esc(remembered)}"></div>
        <div class="form-row"><label class="label" for="login-pass">密码</label>
          <div class="input-affix">
            <input class="input" id="login-pass" name="password" type="password" autocomplete="current-password" required>
            <button type="button" class="affix-btn" data-toggle-pass aria-label="显示密码">显示</button>
          </div>
          <div class="help capslock-hint" data-caps-hint hidden>大写锁定已开启</div>
        </div>
        <div class="form-row"><label class="check"><input type="checkbox" name="remember" ${remembered ? 'checked' : ''}> 记住用户名</label></div>
        <button class="btn btn-primary" type="submit" style="width:100%;margin-top:4px" id="login-btn">${icon('logout')}登录</button>
        ${message ? `<div class="alert danger login-error">${icon('alert')}<div>${esc(message)}</div></div>` : ''}
        <div class="help" style="margin-top:10px">密码策略：${esc(passwordPolicyHint())}（现有账号不受影响）</div>
        <button type="button" class="btn btn-ghost" id="to-activate" style="width:100%;margin-top:10px">${icon('lock')}使用激活码开户</button>
        ${allowSelfRegister ? `<button type="button" class="btn btn-ghost" id="to-register" style="width:100%;margin-top:8px">${icon('plus')}自助注册（无需邀请码）</button>` : ''}
      </form>
      <form id="activate-form" hidden>
        <div class="alert info" style="margin-bottom:12px">${icon('info')}<div>输入管理员发放的一次性激活码，自行设置密码。激活码只能使用一次。</div></div>
        <div class="form-row"><label class="label" for="activate-code">激活码</label>
          <input class="input mono" id="activate-code" name="code" autocomplete="one-time-code" placeholder="XXXX-XXXX" required></div>
        <div class="form-row"><label class="label" for="activate-pass">设置密码</label>
          <div class="input-affix"><input class="input" id="activate-pass" name="password" type="password" minlength="${minLen}" autocomplete="new-password" required>
            <button type="button" class="affix-btn" data-toggle-pass aria-label="显示密码">显示</button></div></div>
        <div class="form-row"><label class="label" for="activate-pass2">确认密码</label>
          <input class="input" id="activate-pass2" name="confirm" type="password" minlength="${minLen}" autocomplete="new-password" required></div>
        <button class="btn btn-primary" type="submit" style="width:100%;margin-top:4px" id="activate-btn">${icon('check')}激活并返回登录</button>
        <button type="button" class="btn btn-ghost" id="back-login" style="width:100%;margin-top:8px">返回登录</button>
        <div class="help" style="margin-top:10px">密码策略：${esc(passwordPolicyHint())}</div>
      </form>
      <form id="register-form" hidden>
        <div class="alert info" style="margin-bottom:12px">${icon('info')}<div>管理员已开启自助注册。注册账号为普通工程师，可自行创建诊断任务。</div></div>
        <div class="form-row"><label class="label" for="register-user">用户名</label>
          <input class="input mono" id="register-user" name="username" autocomplete="username" pattern="[A-Za-z0-9._-]{3,32}" required placeholder="zhangsan"></div>
        <div class="form-row"><label class="label" for="register-display">显示名</label>
          <input class="input" id="register-display" name="display_name" placeholder="可选，默认使用用户名"></div>
        <div class="form-row"><label class="label" for="register-domain">领域</label>
          <input class="input" id="register-domain" name="domain" placeholder="可选，如：时钟同步"></div>
        <div class="form-row"><label class="label" for="register-pass">设置密码</label>
          <div class="input-affix"><input class="input" id="register-pass" name="password" type="password" minlength="${minLen}" autocomplete="new-password" required>
            <button type="button" class="affix-btn" data-toggle-pass aria-label="显示密码">显示</button></div></div>
        <div class="form-row"><label class="label" for="register-pass2">确认密码</label>
          <input class="input" id="register-pass2" name="confirm" type="password" minlength="${minLen}" autocomplete="new-password" required></div>
        <button class="btn btn-primary" type="submit" style="width:100%;margin-top:4px" id="register-btn">${icon('check')}注册并返回登录</button>
        <button type="button" class="btn btn-ghost" id="register-back" style="width:100%;margin-top:8px">返回登录</button>
        <div class="help" style="margin-top:10px">密码策略：${esc(passwordPolicyHint())}</div>
      </form>
      <div class="login-note">完全离线部署 · 数据与密钥保存在本机<br>管理员首次登录需修改初始密码</div>
    </div>`;
  document.body.appendChild(loginRoot);
  bindPasswordToggles(loginRoot);
  const form = loginRoot.querySelector('#login-form');
  const activateForm = loginRoot.querySelector('#activate-form');
  const registerForm = loginRoot.querySelector('#register-form');
  const toggleActivate = (show) => {
    loginRoot.querySelector('#login-title').textContent = show ? '激活账号' : '登录';
    loginRoot.querySelector('#login-sub').textContent = show
      ? '使用一次性激活码设置密码；激活后用该密码登录。'
      : '使用本地账号登录；所有操作会写入审计日志。';
    form.hidden = show;
    activateForm.hidden = !show;
    if (registerForm) registerForm.hidden = true;
    loginRoot.querySelector('#to-activate').hidden = show;
    const registerBtn = loginRoot.querySelector('#to-register');
    if (registerBtn) registerBtn.hidden = show;
    if (show) activateForm.elements.code.focus();
    else form.elements.username.focus();
  };
  const toggleRegister = (show) => {
    if (!allowSelfRegister || !registerForm) return;
    loginRoot.querySelector('#login-title').textContent = show ? '注册账号' : '登录';
    loginRoot.querySelector('#login-sub').textContent = show
      ? '管理员已开启自助注册，填写账号信息后即可使用。'
      : '使用本地账号登录；所有操作会写入审计日志。';
    form.hidden = show;
    activateForm.hidden = true;
    registerForm.hidden = !show;
    loginRoot.querySelector('#to-activate').hidden = show;
    const registerBtn = loginRoot.querySelector('#to-register');
    if (registerBtn) registerBtn.hidden = show;
    if (show) registerForm.elements.username.focus();
    else form.elements.username.focus();
  };
  loginRoot.querySelector('#to-activate').addEventListener('click', () => toggleActivate(true));
  loginRoot.querySelector('#back-login').addEventListener('click', () => toggleActivate(false));
  loginRoot.querySelector('#to-register')?.addEventListener('click', () => toggleRegister(true));
  loginRoot.querySelector('#register-back')?.addEventListener('click', () => toggleRegister(false));
  activateForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = loginRoot.querySelector('#activate-btn');
    const code = activateForm.elements.code.value.trim().toUpperCase();
    const password = activateForm.elements.password.value;
    if (password !== activateForm.elements.confirm.value) {
      toast('两次输入的密码不一致', 'warn');
      return;
    }
    setBusy(btn, true, '激活中…');
    try {
      const data = await api('/api/auth/activate', { method: 'POST', body: { code, password } });
      toast('账号已激活，请使用新密码登录', 'ok');
      form.elements.username.value = data.username || '';
      activateForm.reset();
      toggleActivate(false);
      form.elements.password.focus();
    } catch (err) {
      toast(err.message || '激活失败，请检查激活码是否过期或已使用', 'error', 6000);
    } finally {
      setBusy(btn, false);
    }
  });
  registerForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = loginRoot.querySelector('#register-btn');
    const password = registerForm.elements.password.value;
    if (password !== registerForm.elements.confirm.value) {
      toast('两次输入的密码不一致', 'warn');
      return;
    }
    setBusy(btn, true, '注册中…');
    try {
      const data = await api('/api/auth/register', {
        method: 'POST',
        body: {
          username: registerForm.elements.username.value.trim(),
          display_name: registerForm.elements.display_name.value.trim(),
          domain: registerForm.elements.domain.value.trim(),
          password,
        },
      });
      toast('注册成功，请使用新账号登录', 'ok');
      form.elements.username.value = data.username || '';
      registerForm.reset();
      toggleRegister(false);
      form.elements.password.focus();
    } catch (err) {
      toast(err.message || '注册失败，请检查用户名或密码策略', 'error', 6000);
    } finally {
      setBusy(btn, false);
    }
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = loginRoot.querySelector('#login-btn');
    setBusy(btn, true, '登录中…');
    const username = form.elements.username.value.trim();
    const password = form.elements.password.value;
    rememberUsername(username, form.elements.remember.checked);
    try {
      const data = await api('/api/auth/login', { method: 'POST', body: { username, password } });
      state.user = data.user;
      state.csrf = data.csrf;
      state.sessionExpires = data.expires_at;
      scheduleSessionWarning();
      toast(`欢迎回来，${data.user.display_name || data.user.username}`, 'ok');
      renderShell();
      route();
    } catch (err) {
      const box = form.querySelector('.login-error');
      if (box) box.remove();
      const alert = document.createElement('div');
      alert.className = 'alert danger login-error';
      alert.innerHTML = `${icon('alert')}<div>${esc(loginErrorMessage(err))}</div>`;
      form.appendChild(alert);
      setBusy(btn, false);
    }
  });
}

function showPasswordSetup(oldPassword) {
  if (loginRoot) loginRoot.remove();
  document.body.classList.add('login-mode');
  loginRoot = document.createElement('div');
  loginRoot.className = 'login-wrap';
  const minLen = minPwdLength();
  loginRoot.innerHTML = `
    <div class="login-card">
      <div class="login-brand">
        <div class="brand-mark">${icon('lock')}</div>
        <div><strong>设置新密码</strong><span>首次登录必须修改初始密码</span></div>
      </div>
      <p class="sub">${esc(passwordPolicyHint())}。修改完成后进入平台。</p>
      <form id="setup-form">
        <div class="form-row"><label class="label">新密码</label>
          <div class="input-affix">
            <input class="input" name="new_password" type="password" minlength="${minLen}" required autocomplete="new-password">
            <button type="button" class="affix-btn" data-toggle-pass aria-label="显示密码">显示</button>
          </div>
          <div class="help capslock-hint" data-caps-hint hidden>大写锁定已开启</div>
        </div>
        <div class="form-row"><label class="label">确认新密码</label>
          <div class="input-affix">
            <input class="input" name="confirm" type="password" minlength="${minLen}" required autocomplete="new-password">
            <button type="button" class="affix-btn" data-toggle-pass aria-label="显示密码">显示</button>
          </div>
        </div>
        <button class="btn btn-primary" type="submit" style="width:100%" id="setup-btn">${icon('check')}保存并进入</button>
      </form>
      <div class="login-note">初始密码来自部署时生成的 data/初始管理员密码.txt；修改成功后该文件会自动删除。</div>
    </div>`;
  document.body.appendChild(loginRoot);
  bindPasswordToggles(loginRoot);
  const form = loginRoot.querySelector('#setup-form');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const pwd = form.elements.new_password.value;
    if (pwd !== form.elements.confirm.value) { toast('两次输入的新密码不一致', 'warn'); return; }
    const btn = loginRoot.querySelector('#setup-btn');
    setBusy(btn, true, '保存中…');
    try {
      const data = await api('/api/auth/password', { method: 'POST',
        body: { old_password: oldPassword, new_password: pwd } });
      state.csrf = data.csrf || state.csrf;
      state.user.must_change = 0;
      scheduleSessionWarning();
      toast('密码已更新，其他登录会话已退出', 'ok');
      renderShell();
      route();
    } catch (err) {
      toast(err.message, 'error');
      setBusy(btn, false);
    }
  });
}

function openChangePassword() {
  const minLen = minPwdLength();
  const m = modal({
    title: '修改密码',
    body: `<form id="pwd-form">
      <div class="form-row"><label class="label">当前密码</label><input class="input" type="password" name="old_password" required autocomplete="current-password"></div>
      <div class="form-row"><label class="label">新密码</label>
        <div class="input-affix">
          <input class="input" type="password" name="new_password" minlength="${minLen}" required autocomplete="new-password">
          <button type="button" class="affix-btn" data-toggle-pass aria-label="显示密码">显示</button>
        </div>
        <div class="help">${esc(passwordPolicyHint())}</div>
        <div class="help capslock-hint" data-caps-hint hidden>大写锁定已开启</div>
      </div>
      <div class="form-row"><label class="label">确认新密码</label>
        <div class="input-affix">
          <input class="input" type="password" name="confirm" minlength="${minLen}" required autocomplete="new-password">
          <button type="button" class="affix-btn" data-toggle-pass aria-label="显示密码">显示</button>
        </div>
      </div>
      <div class="alert info" style="margin-top:4px">${icon('info')}<div>保存后，除当前会话外的其他登录会话会立即退出。</div></div>
    </form>`,
    footer: `<button class="btn" data-act="cancel">取消</button>
      <button class="btn btn-primary" data-act="save">保存</button>`,
  });
  bindPasswordToggles(m.body);
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    if (e.target.closest('[data-act="save"]')) {
      const form = m.body.querySelector('#pwd-form');
      if (form.elements.new_password.value !== form.elements.confirm.value) { toast('两次输入的新密码不一致', 'warn'); return; }
      const btn = e.target.closest('[data-act="save"]');
      setBusy(btn, true, '保存中…');
      try {
        const data = await api('/api/auth/password', {
          method: 'POST',
          body: { old_password: form.elements.old_password.value,
                  new_password: form.elements.new_password.value },
        });
        state.csrf = data.csrf || state.csrf;
        state.user.must_change = 0;
        const note = data.other_sessions_revoked
          ? `，已退出 ${data.other_sessions_revoked} 个其他会话` : '';
        toast(`密码已更新${note}`, 'ok');
        m.close();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

async function openProfile() {
  let profile;
  let sessions;
  let modelConfig;
  try {
    [profile, sessions] = await Promise.all([api('/api/profile'), api('/api/auth/sessions')]);
  } catch (err) { toast(err.message, 'error'); return; }
  try {
    modelConfig = await api('/api/profile/model-key');
  } catch {
    // 个人模型接口异常不应阻断个人中心其他功能。
    modelConfig = { configured: false, model: '', base_url: '', maas_url: '', status: {} };
  }
  const u = profile.user;
  const prefs = u.notification_prefs || {};
  const prefDefs = [
    ['task_done', '任务完成'], ['task_failed', '任务失败'], ['review_assigned', '待复核任务'],
    ['upgrade_result', '升级结果'], ['account_status', '账号状态变化'],
  ];
  const m = modal({
    title: '个人中心',
    wide: true,
    body: `<div class="form-grid">
      <div class="form-row"><label class="label">用户名</label><input class="input mono" value="${esc(u.username)}" disabled></div>
      <div class="form-row"><label class="label">显示名</label><input class="input" name="display_name" value="${esc(u.display_name || '')}"></div>
      <div class="form-row"><label class="label">角色</label><input class="input" value="${esc(roleLabel(u.role))}" disabled></div>
      <div class="form-row"><label class="label">领域</label><input class="input" value="${esc(u.domain || '—')}" disabled>
        <div class="help">由管理员在用户管理中维护；个人中心只读。</div></div>
    </div>
    <div class="card" style="margin-top:14px;box-shadow:none;border-color:#dbe4ee">
      <div class="card-body">
        <div class="between">
          <div><strong>公司内网模型（个人 Key）</strong>
            <div class="small muted">${modelConfig.configured
              ? `已配置 ${esc(modelConfig.model)} · ${modelConfig.status?.personal?.available ? '当前可用' : '暂不可用，自动回退默认模型'}`
              : '未配置，当前使用管理员默认模型'}</div></div>
          <button class="btn btn-sm" data-act="model-key">${modelConfig.configured ? '更新个人 Key' : '配置个人 Key'}</button>
        </div>
        ${!modelConfig.configured && modelConfig.status?.notice
          ? `<div class="alert warn" style="margin-top:10px">${icon('alert')}<div>${esc(modelConfig.status.notice.message)}
              <div style="margin-top:6px"><a href="${esc(modelConfig.maas_url)}" target="_blank" rel="noopener">打开个人 API Key 页面 →</a></div></div></div>`
          : ''}
        <div class="tiny muted" style="margin-top:8px">${esc(modelConfig.base_url || '')}</div>
      </div>
    </div>
    <h3 style="margin:14px 0 6px;font-size:14px">通知偏好（站内）</h3>
    <div class="inline" style="flex-wrap:wrap;gap:12px 18px">${prefDefs.map(([key, label]) => `<label class="check"><input type="checkbox" name="pref_${key}" ${prefs[key] !== false ? 'checked' : ''}> ${label}</label>`).join('')}</div>
    <div class="between" style="margin:20px 0 8px"><h3 style="margin:0;font-size:14px">我的活跃会话（${sessions.items.length}）</h3>
      <button class="btn btn-sm" data-act="revoke-others">退出其他设备</button></div>
    <div class="table-wrap"><table class="data"><thead><tr><th>创建</th><th>到期</th><th>IP</th><th>UA</th><th>操作</th></tr></thead><tbody>
      ${sessions.items.map((sess) => `<tr><td class="tiny muted">${fmtDate(sess.created_at)}</td><td class="tiny muted">${fmtDate(sess.expires_at)}</td><td class="mono tiny">${esc(sess.ip || '—')}</td><td class="tiny muted" style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(sess.ua || '—')}</td>
        <td>${sess.current ? '<span class="badge brand">当前</span>' : `<button class="btn btn-sm btn-danger" data-act="revoke-session" data-id="${sess.id}">撤销</button>`}</td></tr>`).join('')}
    </tbody></table></div>`,
    footer: `<button class="btn" data-act="close">关闭</button><button class="btn" data-act="password">修改密码</button><button class="btn btn-primary" data-act="save">保存个人资料</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="close"]')) m.close();
    if (e.target.closest('[data-act="password"]')) { m.close(); openChangePassword(); }
    if (e.target.closest('[data-act="model-key"]')) {
      m.close();
      openPersonalModelModal(() => openProfile());
    }
    if (e.target.closest('[data-act="save"]')) {
      const display_name = m.body.querySelector('[name="display_name"]').value.trim();
      const notification_prefs = {};
      prefDefs.forEach(([key]) => { notification_prefs[key] = !!m.body.querySelector(`[name="pref_${key}"]`).checked; });
      const btn = e.target.closest('[data-act="save"]');
      setBusy(btn, true, '保存中…');
      try {
        const data = await api('/api/profile', { method: 'PUT', body: { display_name, notification_prefs } });
        state.user.display_name = data.user.display_name;
        toast('个人资料已保存', 'ok');
        m.close();
        renderShell();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
    if (e.target.closest('[data-act="revoke-others"]')) {
      if (!await confirmDialog('立即撤销除当前会话外的全部登录会话？', { danger: true, okText: '退出其他设备' })) return;
      try { const res = await api('/api/auth/sessions/revoke-others', { method: 'POST' }); toast(`已撤销 ${res.revoked} 个会话`, 'ok'); m.close(); openProfile(); }
      catch (err) { toast(err.message, 'error'); }
    }
    const revokeBtn = e.target.closest('[data-act="revoke-session"]');
    if (revokeBtn) {
      if (!await confirmDialog('撤销该登录会话？', { danger: true, okText: '撤销' })) return;
      try { await api(`/api/auth/sessions/${revokeBtn.dataset.id}`, { method: 'DELETE' }); toast('会话已撤销', 'ok'); m.close(); openProfile(); }
      catch (err) { toast(err.message, 'error'); }
    }
  });
}

function openReauth(message = '登录已过期，请重新登录') {
  if (reauthOpen) return;
  if (loginRoot) { renderLogin(message); return; }
  reauthOpen = true;
  const remembered = state.user?.username || rememberedUsername();
  const m = modal({
    title: '需要重新登录',
    body: `<div class="alert warn">${icon('alert')}<div>${esc(message)}。当前页面内容会保留，登录后可继续操作。</div></div>
      <form id="reauth-form" style="margin-top:14px">
        <div class="form-row"><label class="label">用户名</label>
          <input class="input" name="username" value="${esc(remembered)}" autocomplete="username" required></div>
        <div class="form-row"><label class="label">密码</label>
          <div class="input-affix">
            <input class="input" name="password" type="password" autocomplete="current-password" required autofocus>
            <button type="button" class="affix-btn" data-toggle-pass aria-label="显示密码">显示</button>
          </div>
          <div class="help capslock-hint" data-caps-hint hidden>大写锁定已开启</div>
        </div>
      </form>`,
    footer: `<button class="btn" data-act="cancel">稍后再登录</button>
      <button class="btn btn-primary" data-act="login">登录</button>`,
    onClose: () => { reauthOpen = false; },
  });
  bindPasswordToggles(m.body);
  const form = m.body.querySelector('#reauth-form');
  const submit = async (btn) => {
    const username = form.elements.username.value.trim();
    const password = form.elements.password.value;
    if (!username || !password) { toast('请输入用户名和密码', 'warn'); return; }
    setBusy(btn, true, '登录中…');
    try {
      const data = await api('/api/auth/login', { method: 'POST', body: { username, password } });
      state.user = data.user;
      state.csrf = data.csrf;
      state.sessionExpires = data.expires_at;
      reauthOpen = false;
      m.close();
      rememberUsername(username, true);
      scheduleSessionWarning();
      renderShell();
      toast('已恢复登录，页面内容保留', 'ok');
    } catch (err) {
      toast(loginErrorMessage(err), 'error');
      setBusy(btn, false);
    }
  };
  m.mask.addEventListener('click', (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    const btn = e.target.closest('[data-act="login"]');
    if (btn) submit(btn);
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const btn = m.foot.querySelector('[data-act="login"]');
    if (btn) submit(btn);
  });
}

function openSessionWarning() {
  if (sessionWarningOpen || reauthOpen || !state.user) return;
  sessionWarningOpen = true;
  const expires = Date.parse(state.sessionExpires || '');
  const minutes = Number.isNaN(expires) ? 2 : Math.max(1, Math.round((expires - Date.now()) / 60000));
  const m = modal({
    title: '会话即将到期',
    body: `<div class="alert warn">${icon('clock')}<div>当前登录会话约 ${minutes} 分钟后到期。继续在线会延长会话，未保存的表单内容不会丢失。</div></div>`,
    footer: `<button class="btn" data-act="relogin">重新登录</button>
      <button class="btn btn-primary" data-act="renew">继续在线</button>`,
    onClose: () => { sessionWarningOpen = false; },
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="relogin"]')) { m.close(); openReauth('请重新登录'); return; }
    const btn = e.target.closest('[data-act="renew"]');
    if (!btn) return;
    setBusy(btn, true, '续期中…');
    try {
      const data = await api('/api/auth/refresh', { method: 'POST' });
      state.sessionExpires = data.expires_at;
      state.csrf = data.csrf || state.csrf;
      toast('会话已延长', 'ok');
      m.close();
      scheduleSessionWarning();
    } catch (err) {
      toast(err.message, 'error');
      setBusy(btn, false);
    }
  });
}

function route() {
  reauthOpen = false;
  sessionWarningOpen = false;
  if (!state.user) { renderLogin(); return; }
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } state.cleanup = null; }
  // 路由切换时关闭所有弹窗/抽屉，避免上一页的浮层遮挡新页面
  document.querySelectorAll('.modal-mask').forEach((el) => el.remove());
  const path = currentPath();
  updateNav();
  if (path === '#/dashboard' || path === '#/') return renderDashboard();
  if (path === '#/tasks') return renderTaskList();
  if (path === '#/tasks/new') return renderNewTask();
  if (path === '#/cases') return renderCases();
  const caseMatch = path.match(/^#\/cases\/([^/]+)$/);
  if (caseMatch) return renderCaseDetail(decodeURIComponent(caseMatch[1]));
  const taskMatch = path.match(/^#\/tasks\/([^/]+)$/);
  if (taskMatch) return renderTaskDetail(decodeURIComponent(taskMatch[1]));
  if (path === '#/skills') return renderSkills();
  if (path === '#/workshop') return renderWorkshop();
  if (path === '#/models') return state.user.role === 'admin' ? renderModels() : denied();
  if (path === '#/users') return state.user.role === 'admin' ? renderUsers() : denied();
  if (path === '#/audit') return state.user.role === 'admin' ? renderAudit() : denied();
  if (path === '#/system') return renderSystem();
  location.hash = '#/dashboard';
}

function denied() {
  const view = mount('无权访问', '当前账号没有该页面权限', '');
  view.innerHTML = `<div class="card"><div class="card-body"><div class="alert warn">${icon('lock')}
    <div>当前账号没有该页面权限。专家账号仅可查看被分配任务并提交复核。</div></div>
    <a class="btn" style="margin-top:12px" href="#/dashboard">返回总览</a></div></div>`;
}

async function boot() {
  state.onUnauthorized = () => {
    unmountAssistant();
    stopSessionTimer();
    if (loginRoot) renderLogin('登录已过期，请重新登录');
    else openReauth('登录已过期，请重新登录');
  };
  state.onPasswordRequired = () => {
    if (!loginRoot) {
      state.user = null;
      stopSessionTimer();
      renderLogin('请先修改初始密码');
    }
  };
  try {
    state.publicConfig = await api('/api/public/config');
  } catch { state.publicConfig = {}; }
  try {
    const data = await api('/api/auth/me');
    state.user = data.user;
    state.csrf = data.csrf;
    state.sessionExpires = data.session_expires;
    scheduleSessionWarning();
    renderShell();
    route();
  } catch {
    renderLogin();
  }
  window.addEventListener('hashchange', route);
}

boot();
