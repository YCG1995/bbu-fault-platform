// 技能库：健康检查、详情抽屉、导入导出与启停
import {
  api, bindActions, bindTopbar, confirmDialog, esc, fmtDate, icon, modal, mount, state, statusBadge, toast,
} from './core.js';

let filter = '';

export async function renderSkills() {
  const isAdmin = state.user?.role === 'admin';
  const view = mount('技能库', '子技能的健康状态、规则覆盖与版本信息',
    `${isAdmin ? `<button class="btn" data-act="scan">${icon('refresh')}重新扫描</button>
     <button class="btn btn-primary" data-act="import">${icon('upload')}导入技能</button>` : ''}`);
  view.innerHTML = `<div class="skeleton" style="height:140px"></div>`;
  let items = [];
  try {
    const data = await api('/api/skills');
    items = data.items || [];
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const counts = { all: items.length, ready: 0, degraded: 0, incompatible: 0, disabled: 0 };
  items.forEach((s) => { counts[s.status] = (counts[s.status] || 0) + 1; });

  view.innerHTML = `
    <div class="grid grid-4" style="margin-bottom:16px">
      <div class="card stat"><div class="stat-icon ok">${icon('check')}</div><div><div class="stat-val">${counts.ready}</div><div class="stat-label">可用技能</div></div></div>
      <div class="card stat"><div class="stat-icon warn">${icon('alert')}</div><div><div class="stat-val">${counts.degraded}</div><div class="stat-label">降级（缺规则/根因）</div></div></div>
      <div class="card stat"><div class="stat-icon" style="background:#fef2f2;color:#b91c1c">${icon('x')}</div><div><div class="stat-val">${counts.incompatible}</div><div class="stat-label">不可用</div></div></div>
      <div class="card stat"><div class="stat-icon info">${icon('layers')}</div><div><div class="stat-val">${items.filter((s) => s.verified).length}</div><div class="stat-label">已通过案例回归</div></div></div>
    </div>
    <div class="toolbar" style="margin-bottom:12px">
      <div class="tabs" id="skill-tabs" style="border:0;padding:0">
        ${[['', '全部'], ['ready', '可用'], ['degraded', '降级'], ['incompatible', '不可用'], ['disabled', '已停用']]
          .map(([k, l]) => `<button data-filter="${k}" class="${filter === k ? 'active' : ''}">${l}</button>`).join('')}
      </div>
      <span class="small muted" style="margin-left:auto">共 ${items.length} 个技能</span>
    </div>
    <div class="skill-grid" id="skill-grid"></div>
    <div class="card" style="margin-top:16px"><div class="card-body small muted">
      <strong>判定口径：</strong>可用 = 脚本与 profile 完整；降级 = 缺证据规则或根因但仍可运行；不可用 = 缺 run_analysis.py / profile 等关键组件。
      「已通过案例回归」仅表示构建时跑过案例，示例技能默认未验证。
    </div></div>`;

  function renderGrid() {
    const filtered = filter ? items.filter((s) => s.status === filter) : items;
    const grid = view.querySelector('#skill-grid');
    if (!filtered.length) {
      grid.innerHTML = `<div class="card" style="grid-column:1/-1">${icon('layers')}<div class="empty"><h3>没有符合条件的技能</h3>
        <p>用元 Skill 或技能工坊构建一个子技能。</p></div></div>`;
      return;
    }
    grid.innerHTML = filtered.map((s) => {
      const reasons = s.health?.reasons || [];
      const example = /示例|demo/i.test(s.title) || /demo-/i.test(s.name);
      return `<div class="card skill-tile">
        <div class="title"><div style="flex:1;min-width:0">
          <strong>${esc(s.title || s.name)}</strong>
          <div class="name">${esc(s.name)} · v${esc(s.version || '0.1.0')}</div>
        </div>${statusBadge(s.status)}</div>
        <div class="inline" style="gap:5px;flex-wrap:wrap">
          ${s.verified ? `<span class="badge ok"><i class="dot"></i>已回归</span>` : `<span class="badge warn"><i class="dot"></i>未验证</span>`}
          ${example ? `<span class="badge muted"><i class="dot"></i>示例</span>` : ''}
          ${s.channel === 'canary' ? `<span class="badge warn"><i class="dot"></i>灰度</span>` : `<span class="badge muted"><i class="dot"></i>稳定</span>`}
          <span class="chip">${esc(s.category || '未分类')}</span>
        </div>
        <div class="desc">${esc(s.description || '（无描述）')}</div>
        <div class="metrics">
          <span>${icon('layers')} 规则 ${s.rules_count ?? 0}</span>
          <span>${icon('branch')} 根因 ${s.root_causes_count ?? 0}</span>
          <span>${icon('history')} v${esc(s.version || '-')}</span>
        </div>
        ${reasons.length ? `<div class="alert ${s.status === 'incompatible' ? 'danger' : 'warn'}" style="padding:8px 10px;font-size:11px">${icon('alert')}<div>${esc(reasons[0])}</div></div>` : ''}
        <div class="actions">
          <button class="btn btn-sm" data-act="detail" data-name="${esc(s.name)}">${icon('eye')}详情</button>
          ${s.status !== 'incompatible' && isAdmin ? `<button class="btn btn-sm ${s.status === 'disabled' ? '' : 'btn-danger'}" data-act="toggle" data-name="${esc(s.name)}" data-status="${s.status}">${s.status === 'disabled' ? '启用' : '停用'}</button>` : ''}
          ${isAdmin ? `<button class="btn btn-sm" data-act="channel" data-name="${esc(s.name)}" data-channel="${s.channel || 'stable'}">${s.channel === 'canary' ? '转稳定' : '灰度'}</button>` : ''}
          <a class="btn btn-sm" href="/api/skills/${encodeURIComponent(s.name)}/export">${icon('download')}导出</a>
        </div>
      </div>`;
    }).join('');
  }
  renderGrid();

  view.querySelector('#skill-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-filter]');
    if (!btn) return;
    filter = btn.dataset.filter;
    view.querySelectorAll('#skill-tabs button').forEach((b) => b.classList.toggle('active', b === btn));
    renderGrid();
  });

  const handlers = {
    scan: async (btn) => {
      btn.disabled = true;
      try {
        const data = await api('/api/skills/scan', { method: 'POST' });
        toast(`扫描完成，共 ${data.count} 个技能`, 'ok');
        renderSkills();
      } catch (err) { toast(err.message, 'error'); btn.disabled = false; }
    },
    import: () => openImportModal(() => renderSkills()),
    detail: (el) => openSkillDrawer(el.dataset.name, () => renderSkills()),
    channel: async (el) => {
      const name = el.dataset.name;
      const next = el.dataset.channel === 'canary' ? 'stable' : 'canary';
      const ok = await confirmDialog(next === 'canary'
        ? `把 ${name} 设为灰度？普通任务不会选中它，只有管理员显式勾选“包含灰度技能”才会参与，适合先做真实案例对照。`
        : `把 ${name} 转回稳定？之后普通任务会重新选中该技能。`,
        { danger: next === 'canary', title: '调整发布通道' });
      if (!ok) return;
      try {
        await api(`/api/skills/${encodeURIComponent(name)}/channel`, { method: 'POST', body: { channel: next } });
        toast(next === 'canary' ? '已设为灰度' : '已转为稳定', 'ok');
        renderSkills();
      } catch (err) { toast(err.message, 'error'); }
    },
    toggle: async (el) => {
      const name = el.dataset.name;
      const disabled = el.dataset.status === 'disabled';
      const ok = await confirmDialog(disabled ? `启用技能 ${name}？` : `停用后该技能不会再被规划选中，确定停用 ${name}？`,
        { danger: !disabled, title: disabled ? '启用技能' : '停用技能' });
      if (!ok) return;
      try {
        await api(`/api/skills/${encodeURIComponent(name)}/${disabled ? 'enable' : 'disable'}`, { method: 'POST' });
        toast(disabled ? '技能已启用' : '技能已停用', 'ok');
        renderSkills();
      } catch (err) { toast(err.message, 'error'); }
    },
  };
  bindActions(view, handlers);
  bindTopbar(handlers);
  return view;
}

function openImportModal(onDone) {
  const m = modal({
    title: '导入技能包',
    wide: true,
    body: `
    <div class="form-row">
      <label class="label">技能 zip 包 <span class="hint">包内需包含 SKILL.md 与 scripts/run_analysis.py</span></label>
      <input class="input" type="file" id="import-file" accept=".zip,application/zip" required>
    </div>
    <label class="check"><input type="checkbox" id="import-overwrite"> 允许覆盖同名技能（旧版本会备份到 data/skill_versions）</label>
    <div class="alert info" style="margin-top:14px">${icon('info')}<div>导入后会重新扫描并做健康检查；缺组件的旧技能会显示为不可用，不会强行运行。</div></div>`,
    footer: `<button class="btn" data-act="cancel">取消</button>
     <button class="btn btn-primary" data-act="do-import">${icon('upload')}导入</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    if (e.target.closest('[data-act="do-import"]')) {
      const file = m.body.querySelector('#import-file').files[0];
      if (!file) { toast('请选择 zip 文件', 'warn'); return; }
      const btn = e.target.closest('[data-act="do-import"]');
      btn.disabled = true;
      const fd = new FormData();
      fd.append('files', file, file.name);
      if (m.body.querySelector('#import-overwrite').checked) fd.append('overwrite', '1');
      try {
        const data = await api('/api/skills/import', { method: 'POST', body: fd });
        toast(`技能 ${data.skill.name} 导入成功`, 'ok');
        m.close();
        onDone && onDone();
      } catch (err) { toast(err.message, 'error'); btn.disabled = false; }
    }
  });
}

async function openSkillDrawer(name, onChanged) {
  let data;
  try {
    data = await api(`/api/skills/${encodeURIComponent(name)}`);
  } catch (err) { toast(err.message, 'error'); return; }
  const drawer = document.createElement('div');
  drawer.className = 'modal-mask';
  const isAdmin = state.user?.role === 'admin';
  const profile = data.profile || {};
  const fault = profile.fault || {};
  const steps = profile.steps || [];
  const rootCauses = profile.root_causes || [];
  const reasons = data.health?.reasons || [];
  const components = data.components || {};
  drawer.innerHTML = `<div class="drawer">
    <div class="drawer-head">
      <div style="flex:1;min-width:0">
        <h2>${esc(data.title || data.name)}</h2>
        <div class="mono muted tiny">${esc(data.name)} · v${esc(data.version)}</div>
      </div>
      ${statusBadge(data.status)}
      <button class="btn btn-ghost btn-sm" data-act="close">${icon('x')}</button>
    </div>
    <div class="drawer-body">
      ${reasons.length ? `<div class="alert ${data.status === 'incompatible' ? 'danger' : 'warn'}" style="margin-bottom:16px">${icon('alert')}<div><strong>健康检查提示：</strong><ul>${reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div></div>` : ''}
      <div class="kv" style="margin-bottom:18px">
        <div class="k">故障名称</div><div class="v">${esc(fault.name || data.fault?.name || '—')}</div>
        <div class="k">别名</div><div class="v">${(data.fault?.aliases || []).map((a) => `<span class="chip alias">${esc(a)}</span>`).join(' ') || '—'}</div>
        <div class="k">告警码</div><div class="v">${(data.fault?.alarm_codes || []).map((a) => `<span class="chip">${esc(a)}</span>`).join(' ') || '—'}</div>
        <div class="k">设备形态</div><div class="v">${(data.fault?.device_types || []).join('、') || '—'}</div>
        <div class="k">分类 / 来源</div><div class="v">${esc(data.category || '—')} / ${esc(data.source || '—')}</div>
        <div class="k">执行模式</div><div class="v">${esc(data.execution_mode || 'script')}</div>
        <div class="k">版本 / 验证</div><div class="v">v${esc(data.version)} · ${data.verified ? '已通过案例回归' : '未验证（草稿或示例）'}</div>
        <div class="k">导入 / 更新</div><div class="v">${fmtDate(data.imported_at)} / ${fmtDate(data.updated_at)}</div>
      </div>
      <h3 style="font-size:14px;margin:0 0 8px">组件状态</h3>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px">
        ${Object.entries(components).map(([k, v]) => {
          const ok = v === true || v === 'ok' || (v && v.exists !== false);
          return `<span class="badge ${ok ? 'ok' : 'danger'}"><i class="dot"></i>${esc(k)}</span>`;
        }).join('') || '<span class="muted small">无组件明细</span>'}
      </div>
      <h3 style="font-size:14px;margin:0 0 8px">根因定义 <span class="muted small">(${rootCauses.length})</span></h3>
      <div class="tree-list" style="margin-bottom:18px">
        ${rootCauses.length ? rootCauses.map((rc) => `<div class="rule-item">
          <div class="id">${esc(rc.id)} · ${esc(rc.name)}</div>
          <div class="desc">${esc(rc.mechanism || rc.description || '')}</div>
          <div class="globs">置信度 ${esc(rc.confidence || '—')}${rc.action ? ` · 处置：${esc(rc.action)}` : ''}</div>
        </div>`).join('') : '<div class="muted small">没有根因定义</div>'}
      </div>
      <h3 style="font-size:14px;margin:0 0 8px">分析步骤</h3>
      <div class="tree-list" style="margin-bottom:18px">
        ${steps.length ? steps.map((s, i) => `<div class="rule-item">
          <div class="id">${esc(s.id || `step-${i + 1}`)}</div>
          <div class="desc">${esc(s.title || s.description || '')}</div>
          ${s.script ? `<div class="globs">${esc(s.script)}</div>` : ''}
        </div>`).join('') : '<div class="muted small">没有步骤定义</div>'}
      </div>
      <h3 style="font-size:14px;margin:0 0 8px">案例</h3>
      <div class="tree-list" style="margin-bottom:18px">
        ${(data.cases || []).length ? data.cases.map((c) => `<div class="rule-item">
          <div class="id">${esc(c.case_id)} · ${esc(c.kind)}</div>
          <div class="desc">期望根因：${esc(c.expected_root_cause || '—')}</div>
        </div>`).join('') : '<div class="muted small">没有案例；未经过回归验证</div>'}
      </div>
      <h3 style="font-size:14px;margin:0 0 8px">版本历史</h3>
      <div class="tree-list">
        ${(data.versions || []).length ? data.versions.map((v) => `<div class="version-row">
          <strong>v${esc(v.version)}</strong>${statusBadge(v.status === 'published' ? 'ready' : 'muted', v.status)}
          <span class="muted small" style="flex:1">${esc(v.note || '')}</span>
          <span class="tiny muted">${fmtDate(v.created_at)}</span>
        </div>`).join('') : '<div class="muted small">没有版本记录</div>'}
      </div>
    </div>
    <div class="drawer-foot">
      <a class="btn" href="/api/skills/${encodeURIComponent(data.name)}/export">${icon('download')}导出 zip</a>
      ${isAdmin ? `<button class="btn ${data.status === 'disabled' ? 'btn-primary' : 'btn-danger'}" data-act="toggle">
        ${data.status === 'disabled' ? '启用' : '停用'}</button>` : ''}
      <button class="btn" data-act="close">关闭</button>
    </div>
  </div>`;
  document.body.appendChild(drawer);
  const close = () => drawer.remove();
  drawer.addEventListener('click', async (e) => {
    if (e.target === drawer || e.target.closest('[data-act="close"]')) close();
    const toggle = e.target.closest('[data-act="toggle"]');
    if (toggle) {
      const disabled = data.status === 'disabled';
      try {
        await api(`/api/skills/${encodeURIComponent(data.name)}/${disabled ? 'enable' : 'disable'}`, { method: 'POST' });
        toast(disabled ? '技能已启用' : '技能已停用', 'ok');
        close();
        onChanged && onChanged();
      } catch (err) { toast(err.message, 'error'); }
    }
  });
}
