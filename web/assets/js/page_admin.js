// 管理页：模型接入、用户、审计、系统信息
import {
  api, bindActions, bindTopbar, confirmDialog, esc, fmtDate, fmtDur, fmtSize, icon, modal, mount,
  providerName, roleLabel, setBusy, state, statusBadge, toast,
} from './core.js';
import { PLATFORM_PRESETS, allPopularModels } from './model_presets.js';

const ROLE_LABELS = { default: '默认', planner: '规划', router: '路由', arbiter: '仲裁', writer: '报告', workshop: '工坊', assistant: '助手' };

// ---------------------------- 模型接入 ----------------------------
export async function renderModels() {
  const view = mount('模型接入', 'OpenAI 兼容 / Anthropic 原生 / Mock；API Key 在数据库内加密存储',
    `<button class="btn btn-primary" data-act="new-model">${icon('plus')}添加模型</button>`);
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let data;
  try {
    data = await api('/api/models');
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const models = data.items || [];
  view.innerHTML = `
    <div class="alert info" style="margin-bottom:14px">${icon('info')}<div>
      <strong>协议说明：</strong>OpenAI 兼容填到 <code>/v1</code> 结尾（如 https://api.openai.com/v1 或内网 vLLM/OneAPI 地址）；
      Anthropic 使用原生 <code>/v1/messages</code> 协议，Base URL 填 https://api.anthropic.com。密钥只在服务端解密，前端仅显示掩码。
      <div style="margin-top:6px">成员在个人中心配置的公司内网 MaaS Key 优先于这里的默认模型；未配置时按角色与优先级选择默认模型。</div>
    </div></div>
    <div class="card"><div class="card-body tight table-wrap">
      ${models.length ? `<table class="data"><thead><tr>
        <th>配置</th><th>协议 / 模型</th><th>执行位置</th><th>角色</th><th>优先级</th><th>密钥</th><th>状态</th><th>操作</th>
      </tr></thead><tbody>${models.map((m) => `<tr>
        <td><div style="font-weight:600">${esc(m.name)}</div><div class="mono tiny muted">${esc(m.base_url || '—')}</div></td>
        <td>${esc(providerName(m.provider))}<div class="mono tiny">${esc(m.model)}</div></td>
        <td>${m.transport === 'browser'
          ? '<span class="badge info"><i class="dot"></i>浏览器代理</span>'
          : '<span class="badge muted"><i class="dot"></i>服务端</span>'}</td>
        <td>${(m.roles || []).map((r) => `<span class="role-tag">${esc(ROLE_LABELS[r] || r)}</span>`).join('') || '—'}</td>
        <td>${m.priority}</td>
        <td class="mono tiny">${esc(m.api_key_masked || '—')}</td>
        <td><button class="switch ${m.enabled ? 'on' : ''}" data-act="toggle" data-id="${m.id}" aria-label="启用/停用"></button></td>
        <td><div class="inline" style="gap:4px">
          <button class="btn btn-sm" data-act="test" data-id="${m.id}">${m.transport === 'browser' ? '浏览器内自检' : '测试'}</button>
          <button class="btn btn-sm" data-act="edit" data-id="${m.id}">编辑</button>
          <button class="btn btn-sm btn-danger" data-act="delete" data-id="${m.id}">删除</button>
        </div></td></tr>`).join('')}</tbody></table>`
        : `<div class="empty">${icon('cpu')}<h3>还没有模型配置</h3>
          <p>添加一个 OpenAI 兼容或 Anthropic 服务后，规划、仲裁、报告和工坊才能用 AI；未配置时平台走确定性路径。</p>
          <button class="btn btn-primary" data-act="new-model">${icon('plus')}添加模型</button></div>`}
    </div></div>
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>角色路由：</strong>default 兜底；planner 负责生成动态波次计划（一次规划 + 最多一次补规划）；router 可作为老部署的规划回退；arbiter 只在结论冲突时调用一次；writer 负责报告综合研判；workshop 只做技能起草；assistant 负责悬浮助手的工具规划与结果总结，未配置时回退 default。
      同一角色多个配置时按优先级数字从小到大尝试，失败会自动回退。
    </div></div>`;

  const handlers = {
    'new-model': () => openModelModal(null, () => renderModels()),
    edit: (el) => openModelModal(models.find((m) => String(m.id) === el.dataset.id), () => renderModels()),
    test: async (el) => {
      const m = models.find((x) => String(x.id) === el.dataset.id);
      if (m?.transport === 'browser') return runRelayTest(m, el);
      setBusy(el, true, '测试中…');
      try {
        const res = await api(`/api/models/${el.dataset.id}/test`, { method: 'POST' });
        if (res.ok) toast(`连通成功：${res.reply || 'OK'} · ${res.usage || 0} tokens`, 'ok');
        else toast(`连通失败：${res.error}`, 'error', 6000);
      } catch (err) { toast(err.message, 'error', 6000); }
      finally { setBusy(el, false); }
    },
    toggle: async (el) => {
      const m = models.find((x) => String(x.id) === el.dataset.id);
      try {
        await api(`/api/models/${m.id}`, { method: 'PUT', body: { enabled: !m.enabled } });
        toast(m.enabled ? '已停用' : '已启用', 'ok');
        renderModels();
      } catch (err) { toast(err.message, 'error'); }
    },
    delete: async (el) => {
      const m = models.find((x) => String(x.id) === el.dataset.id);
      if (!(await confirmDialog(`删除模型配置「${m.name}」？该操作不可恢复。`, { danger: true, title: '删除模型' }))) return;
      try { await api(`/api/models/${m.id}`, { method: 'DELETE' }); toast('已删除', 'ok'); renderModels(); }
      catch (err) { toast(err.message, 'error'); }
    },
  };
  bindActions(view, handlers);
  bindTopbar(handlers);
  return view;
}

async function runRelayTest(model, button) {
  setBusy(button, true, '自检中…');
  try {
    const start = await api(`/api/models/${model.id}/relay-test`, { method: 'POST' });
    const testId = start.test_id;
    for (let i = 0; i < 90; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const test = await api(`/api/models/${model.id}/relay-test/${encodeURIComponent(testId)}`);
      if (test.state === 'pending') continue;
      if (test.state === 'done' && test.ok) {
        toast(`浏览器内自检成功：${test.reply || 'OK'} · ${test.latency_ms ?? '—'} ms`, 'ok');
      } else {
        toast(`浏览器内自检失败：${test.error || test.code || '未返回有效响应'}`, 'error', 8000);
      }
      return;
    }
    toast('浏览器内自检超时：请确认本页保持打开且有可用模型', 'error', 8000);
  } catch (err) {
    toast(err.message, 'error', 8000);
  } finally {
    setBusy(button, false);
  }
}

function openModelModal(model, onDone) {
  const maasBase = state.publicConfig?.personal_model_base_url || 'https://nebulacoder-maas.zte.com.cn/v1';
  const maasModel = state.publicConfig?.personal_model_default || 'deepseek-v4-flash';
  const maasModels = state.publicConfig?.personal_model_models || [maasModel];
  const companyPreset = {
    ...PLATFORM_PRESETS[0],
    base_url: maasBase,
    models: [...new Set(maasModels.map(String))].map((name) => [name, 8192]),
  };
  const presets = [companyPreset, ...PLATFORM_PRESETS.slice(1)];
  const allCandidates = () => {
    const seen = new Set();
    const out = [];
    for (const preset of presets) {
      for (const [name, maxTokens] of preset.models) {
        if (!seen.has(name)) { seen.add(name); out.push([name, maxTokens]); }
      }
    }
    for (const [name, maxTokens] of allPopularModels()) {
      if (!seen.has(name)) { seen.add(name); out.push([name, maxTokens]); }
    }
    return out;
  };
  const normalize = (value) => String(value || '').trim().replace(/\/+$/, '');
  const findPreset = (baseUrl) => presets.find((preset) => normalize(preset.base_url) === normalize(baseUrl)) || null;
  const findById = (id) => presets.find((preset) => preset.id === id) || null;
  const findModel = (baseUrl, name) => {
    const preset = findPreset(baseUrl);
    const candidates = preset ? preset.models : allCandidates();
    return candidates.find(([modelName]) => modelName === name) || null;
  };
  const initialBase = model?.base_url || maasBase;
  const initialPreset = findPreset(initialBase);
  const initialModels = initialPreset ? initialPreset.models : allCandidates();
  const initialModel = model?.model || maasModel;
  const initialSuggestion = findModel(initialBase, initialModel);
  const initialMax = model?.max_tokens ?? (initialSuggestion ? initialSuggestion[1] : 8192);
  const platformValue = model ? (initialPreset?.id || 'custom') : companyPreset.id;
  const initialExtra = model?.extra || {};
  const browserRelay = model?.browser_relay || initialExtra.browser_relay || {};
  const isBrowser = (model?.transport || initialExtra.transport || 'server') === 'browser';
  const browserRoles = Array.isArray(browserRelay.allow_roles) ? browserRelay.allow_roles : [];

  const m = modal({
    title: model ? '编辑模型配置' : '添加模型配置',
    wide: true,
    body: `<form id="model-form">
      <div class="alert info" style="margin-bottom:14px">${icon('info')}<div>
        可直接选择常用平台，自动带出 Base URL、模型名和建议 <code>max_tokens</code>；仍可手动修改，或点击“获取”读取服务端模型列表。
      </div></div>
      <div class="form-row"><label class="label">配置名称</label>
        <input class="input" name="name" id="model-config-name" value="${esc(model?.name || '公司 MaaS')}" placeholder="例如：公司 MaaS" required>
      </div>
      <div class="form-grid">
        <div class="form-row"><label class="label">平台预设 <span class="hint">选择后自动填充，可再修改</span></label>
          <select class="select" id="model-platform">
            ${presets.map((p) => `<option value="${p.id}" ${platformValue === p.id ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}
            <option value="custom" ${platformValue === 'custom' ? 'selected' : ''}>自定义 / 手动填写</option>
          </select>
        </div>
        <div class="form-row"><label class="label">协议</label>
          <select class="select" name="provider" id="provider-select">
            ${['openai', 'anthropic', 'mock'].map((p) => `<option value="${p}" ${(model?.provider || 'openai') === p ? 'selected' : ''}>${providerName(p)}</option>`).join('')}
          </select></div>
      </div>
      <div class="form-row">
        <label class="check" style="font-weight:600"><input type="checkbox" id="browser-transport" ${isBrowser ? 'checked' : ''}>
          浏览器代理执行 <span class="hint">请求由当前打开的浏览器发起，服务端只解析响应</span></label>
        <div class="help">适用于平台服务器无法直连模型网关、但浏览器可经系统代理访问的现场。Base URL 必须是浏览器视角可达地址；Key 只在执行瞬间下发到浏览器内存，不进入 URL、日志或轨迹。</div>
      </div>
      <div class="card" id="browser-relay-box" style="box-shadow:none;border-color:#c7d2fe;margin-bottom:14px;${isBrowser ? '' : 'display:none'}">
        <div class="card-body">
          <div class="form-grid">
            <div class="form-row"><label class="label">单窗口并发</label><input class="input" type="number" id="browser-max-concurrent" min="1" max="4" value="${Number(browserRelay.max_concurrent) || 2}"><div class="help">1–4，默认 2</div></div>
            <div class="form-row"><label class="label">认领超时（秒）</label><input class="input" type="number" id="browser-claim-timeout" min="5" max="60" value="${Number(browserRelay.claim_timeout_sec) || 10}"><div class="help">5–60，默认 10</div></div>
            <div class="form-row"><label class="label">执行超时（秒）</label><input class="input" type="number" id="browser-exec-timeout" min="30" max="120" value="${Number(browserRelay.exec_timeout_sec) || 90}"><div class="help">30–120，默认 90</div></div>
          </div>
          <div class="form-row" style="margin-bottom:0"><label class="label">允许执行的用户角色</label>
            <div class="inline" style="gap:14px;flex-wrap:wrap">
              ${[['engineer', '工程师'], ['reviewer', '专家'], ['admin', '管理员']].map(([value, label]) =>
                `<label class="check"><input type="checkbox" data-browser-role="${value}" ${browserRoles.includes(value) ? 'checked' : ''}> ${label}</label>`).join('')}
            </div>
            <div class="help">不勾选=所有登录角色均可承接；勾选后仅这些角色能看到并领取该模型。</div>
          </div>
        </div>
      </div>
      <div class="form-row"><label class="label">Base URL <span class="hint">可手动输入，也可从常用平台下拉选择；浏览器代理不要填 127.0.0.1 / localhost</span></label>
        <input class="input mono" name="base_url" id="base-url" list="base-url-options" value="${esc(initialBase)}" placeholder="${esc(maasBase)}">
        <datalist id="base-url-options">${presets.filter((p) => p.base_url).map((p) => `<option value="${esc(p.base_url)}" label="${esc(p.label)}"></option>`).join('')}</datalist>
        <div class="help" id="platform-note">${esc(initialPreset?.note || '自定义地址：按服务端实际 OpenAI 兼容或 Anthropic 端点填写。')}</div>
      </div>
      <div class="form-grid">
        <div class="form-row"><label class="label">模型名 <span class="hint">选择常用模型，或点击获取</span></label>
          <div class="inline" style="gap:8px;align-items:stretch">
            <input class="input mono" name="model" id="model-name" list="model-options" value="${esc(initialModel)}" placeholder="${esc(maasModel)}" required style="flex:1;min-width:0">
            <button class="btn" type="button" id="model-discover" data-act="discover">${icon('refresh')}获取</button>
          </div>
          <select class="select" id="model-select" aria-label="从模型列表选择模型" style="margin-top:8px"></select>
          <datalist id="model-options">${initialModels.map(([name, maxTokens]) => `<option value="${esc(name)}" label="${esc(`${name} · 建议 ${maxTokens}`)}"></option>`).join('')}</datalist>
          <div class="help" id="model-discover-hint">${initialSuggestion ? `建议 max_tokens ${initialSuggestion[1]}` : '获取后可下拉选择，也可直接输入模型名。'}</div>
        </div>
        <div class="form-row"><label class="label">max_tokens <span class="hint">选择模型自动填入，可修改</span></label>
          <input class="input" type="number" name="max_tokens" id="max-tokens" value="${initialMax}" min="1" max="200000">
        </div>
      </div>
      <div class="form-row"><label class="label">API Key <span class="hint">编辑时留空=保持原密钥</span></label>
          <input class="input mono" name="api_key" type="password" autocomplete="new-password" placeholder="${model?.api_key_masked ? esc(model.api_key_masked) : '公司 MaaS 个人 API Key'}">
      </div>
      <div class="form-row"><label class="label">承担角色</label>
        <div class="inline" style="gap:14px;flex-wrap:wrap">
          ${Object.entries(ROLE_LABELS).map(([k, label]) => `<label class="check"><input type="checkbox" data-role="${k}"
            ${(model?.roles || ['default']).includes(k) ? 'checked' : ''}> ${label}</label>`).join('')}
        </div>
        <div class="help">不选则默认 default。平台按角色选择模型，没配对应角色时回退到 default。</div>
      </div>
      <div class="form-grid">
        <div class="form-row"><label class="label">temperature</label><input class="input" type="number" step="0.1" name="temperature" value="${model?.temperature ?? 0.2}" min="0" max="2"></div>
        <div class="form-row"><label class="label">超时（秒）</label><input class="input" type="number" name="timeout_sec" value="${model?.timeout_sec ?? 180}" min="5" max="600"></div>
        <div class="form-row"><label class="label">优先级 <span class="hint">数字越小越先尝试</span></label><input class="input" type="number" name="priority" value="${model?.priority ?? 0}" min="0"></div>
      </div>
      <div class="form-row"><label class="label">扩展参数 JSON <span class="hint">可选，如自定义 headers</span></label>
        <textarea class="textarea mono" name="extra" rows="3">${esc(JSON.stringify(model?.extra || {}, null, 2))}</textarea></div>
    </form>`,
    footer: `<button class="btn" data-act="cancel">取消</button>
      <button class="btn" data-act="test" id="model-test">测试当前填写值</button>
      <button class="btn btn-primary" data-act="save">${model ? '保存' : '添加'}</button>`,
  });
  const form = m.body.querySelector('#model-form');
  const nameInput = m.body.querySelector('#model-config-name');
  const baseInput = m.body.querySelector('#base-url');
  const providerSelect = m.body.querySelector('#provider-select');
  const platformSelect = m.body.querySelector('#model-platform');
  const browserCheck = m.body.querySelector('#browser-transport');
  const browserBox = m.body.querySelector('#browser-relay-box');
  const modelInput = m.body.querySelector('#model-name');
  const modelOptions = m.body.querySelector('#model-options');
  const modelSelect = m.body.querySelector('#model-select');
  const maxInput = m.body.querySelector('#max-tokens');
  const platformNote = m.body.querySelector('#platform-note');
  const discoverHint = m.body.querySelector('#model-discover-hint');
  const discoverButton = m.body.querySelector('#model-discover');
  let discovering = false;

  function renderModelOptions(items) {
    const rows = [];
    const seen = new Set();
    for (const item of items || []) {
      const name = String(item?.[0] || '').trim();
      if (!name || seen.has(name)) continue;
      seen.add(name);
      rows.push([name, item?.[1] || '']);
    }
    const current = modelInput.value.trim();
    if (current && !seen.has(current)) rows.push([current, '']);
    modelOptions.innerHTML = rows.map(([name, maxTokens]) => `<option value="${esc(name)}" label="${esc(maxTokens ? `${name} · 建议 ${maxTokens}` : name)}"></option>`).join('');
    // 原生 datalist 在已有值时只会过滤出当前值，编辑配置时用户看不到
    // 刚获取的其他模型；这里额外提供真实 select，确保所有结果都可选。
    modelSelect.innerHTML = `<option value="">选择模型…</option>` + rows.map(([name, maxTokens]) =>
      `<option value="${esc(name)}">${esc(maxTokens ? `${name} · 建议 ${maxTokens}` : name)}</option>`).join('');
    modelSelect.value = current && rows.some(([name]) => name === current) ? current : '';
  }

  function syncModelSelect() {
    const current = modelInput.value.trim();
    modelSelect.value = current && [...modelSelect.options].some((opt) => opt.value === current)
      ? current : '';
  }

  function applyModelMax() {
    const suggestion = findModel(baseInput.value, modelInput.value);
    if (suggestion) {
      maxInput.value = suggestion[1];
      discoverHint.textContent = `建议 max_tokens ${suggestion[1]}，可按账号额度修改。`;
    } else if (modelInput.value) {
      discoverHint.textContent = '自定义模型名，max_tokens 请按服务端限制填写。';
    }
    syncModelSelect();
  }

  function autoName(preset) {
    if (!preset) return;
    const current = nameInput.value.trim();
    const isAuto = !current || current === '公司 MaaS'
      || presets.some((item) => item.label === current);
    if (isAuto) nameInput.value = preset.id === 'zte-maas' ? '公司 MaaS' : preset.label;
  }

  function usePlatform(preset, { setModel = true } = {}) {
    if (!preset) return;
    platformSelect.value = preset.id;
    providerSelect.value = preset.provider;
    baseInput.value = preset.base_url;
    platformNote.textContent = preset.note || '';
    renderModelOptions(preset.models);
    autoName(preset);
    if (setModel) {
      modelInput.value = preset.models[0]?.[0] || '';
      applyModelMax();
    }
  }

  function syncFromBase() {
    const preset = findPreset(baseInput.value);
    if (preset) {
      platformSelect.value = preset.id;
      providerSelect.value = preset.provider;
      platformNote.textContent = preset.note || '';
      renderModelOptions(preset.models);
      autoName(preset);
      if (!modelInput.value) {
        modelInput.value = preset.models[0]?.[0] || '';
      }
      applyModelMax();
    } else {
      platformSelect.value = 'custom';
      platformNote.textContent = '自定义地址：按服务端实际 OpenAI 兼容或 Anthropic 端点填写。';
      renderModelOptions(allCandidates());
      applyModelMax();
    }
  }

  platformSelect.addEventListener('change', () => {
    if (platformSelect.value === 'custom') {
      platformNote.textContent = '自定义地址：按服务端实际 OpenAI 兼容或 Anthropic 端点填写。';
      renderModelOptions(allCandidates());
      return;
    }
    usePlatform(findById(platformSelect.value));
  });
  baseInput.addEventListener('change', syncFromBase);
  modelInput.addEventListener('change', applyModelMax);
  modelInput.addEventListener('input', syncModelSelect);
  modelSelect.addEventListener('change', () => {
    if (!modelSelect.value) return;
    modelInput.value = modelSelect.value;
    applyModelMax();
  });
  renderModelOptions(initialModels);

  providerSelect.addEventListener('change', () => {
    if (browserCheck.checked && providerSelect.value === 'mock') {
      providerSelect.value = 'openai';
      toast('浏览器代理只支持 OpenAI 兼容或 Anthropic 协议，已切回 OpenAI', 'warn');
    }
    const defaults = { openai: maasBase, anthropic: 'https://api.anthropic.com', mock: '' };
    if (!baseInput.value || Object.values(defaults).includes(baseInput.value) || baseInput.value === 'https://api.openai.com/v1') {
      baseInput.value = defaults[providerSelect.value] || '';
      syncFromBase();
    }
  });
  browserCheck.addEventListener('change', () => {
    browserBox.style.display = browserCheck.checked ? '' : 'none';
    if (browserCheck.checked && providerSelect.value === 'mock') {
      providerSelect.value = 'openai';
      toast('浏览器代理只支持 OpenAI 兼容或 Anthropic 协议，已切回 OpenAI', 'warn');
    }
  });

  function collect() {
    const out = {};
    new FormData(form).forEach((v, k) => { out[k] = v; });
    out.roles = [...form.querySelectorAll('[data-role]:checked')].map((el) => el.dataset.role);
    if (!out.roles.length) out.roles = ['default'];
    out.enabled = model?.enabled ?? 1;
    try { out.extra = JSON.parse(out.extra || '{}'); }
    catch (e) { throw new Error('扩展参数不是合法 JSON：' + e.message); }
    if (!out.extra || typeof out.extra !== 'object' || Array.isArray(out.extra)) {
      throw new Error('扩展参数必须是 JSON 对象');
    }
    const browser = !!browserCheck.checked;
    if (browser && (out.provider === 'mock' || out.provider === '')) {
      throw new Error('浏览器代理只支持 OpenAI 兼容或 Anthropic 协议');
    }
    out.extra.transport = browser ? 'browser' : 'server';
    if (browser) {
      const maxConcurrent = Number(m.body.querySelector('#browser-max-concurrent').value || 2);
      const claimTimeout = Number(m.body.querySelector('#browser-claim-timeout').value || 10);
      const execTimeout = Number(m.body.querySelector('#browser-exec-timeout').value || 90);
      if (!(maxConcurrent >= 1 && maxConcurrent <= 4)) throw new Error('单窗口并发必须在 1–4');
      if (!(claimTimeout >= 5 && claimTimeout <= 60)) throw new Error('认领超时必须在 5–60 秒');
      if (!(execTimeout >= 30 && execTimeout <= 120)) throw new Error('执行超时必须在 30–120 秒');
      out.extra.browser_relay = {
        max_concurrent: maxConcurrent,
        claim_timeout_sec: claimTimeout,
        exec_timeout_sec: execTimeout,
        allow_roles: [...form.querySelectorAll('[data-browser-role]:checked')].map((el) => el.dataset.browserRole),
      };
    }
    if (!out.api_key) delete out.api_key;
    return out;
  }

  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    if (e.target.closest('#model-discover')) {
      if (discovering) return;
      let payload;
      try { payload = collect(); } catch (err) { toast(err.message, 'error'); return; }
      if (payload.provider === 'mock') { toast('Mock 模型不需要获取列表', 'info'); return; }
      if (!payload.base_url) { toast('请先填写 Base URL', 'error'); return; }
      if (!payload.api_key && !model) { toast('请先填写 API Key', 'error'); return; }
      if (model) payload.model_id = model.id;
      discovering = true;
      setBusy(discoverButton, true, '获取中…');
      try {
        const res = await api('/api/models/discover', { method: 'POST', body: payload });
        if (!document.body.contains(m.mask)) return;
        if (normalize(baseInput.value) !== normalize(payload.base_url)
            || providerSelect.value !== payload.provider) {
          toast('Base URL 或协议已变更，已忽略上一次获取结果', 'warn');
          return;
        }
        const items = (res.models || []).map((name) => {
          const suggestion = findModel(payload.base_url, name);
          return [name, suggestion ? suggestion[1] : ''];
        });
        if (!items.length) { toast('接口未返回模型列表', 'error'); return; }
        renderModelOptions(items);
        if (!modelInput.value) {
          modelInput.value = items[0][0];
          applyModelMax();
        }
        discoverHint.textContent = `获取到 ${items.length} 个模型，可从下拉选择或手动输入。`;
        toast(`获取到 ${items.length} 个模型`, 'ok');
      } catch (err) {
        if (document.body.contains(m.mask)) toast(err.message, 'error', 8000);
      } finally {
        discovering = false;
        setBusy(discoverButton, false);
      }
    }
    if (e.target.closest('[data-act="save"]')) {
      const btn = e.target.closest('[data-act="save"]');
      let payload;
      try { payload = collect(); } catch (err) { toast(err.message, 'error'); return; }
      setBusy(btn, true, '保存中…');
      try {
        if (model) await api(`/api/models/${model.id}`, { method: 'PUT', body: payload });
        else await api('/api/models', { method: 'POST', body: payload });
        toast(model ? '模型配置已更新' : '模型配置已添加', 'ok');
        m.close();
        onDone && onDone();
      } catch (err) { toast(err.message, 'error', 6000); setBusy(btn, false); }
    }
    if (e.target.closest('#model-test')) {
      const btn = e.target.closest('#model-test');
      let payload;
      try { payload = collect(); } catch (err) { toast(err.message, 'error'); return; }
      if (payload.extra?.transport === 'browser') {
        toast('浏览器代理模型请先保存，再点击列表中的“浏览器内自检”', 'warn');
        return;
      }
      if (model) payload.model_id = model.id;
      setBusy(btn, true, '测试中…');
      try {
        const res = await api('/api/models/test', { method: 'POST', body: payload });
        if (res.ok) toast(`连通成功：${res.reply || 'OK'} · ${res.latency_ms ?? '—'} ms`, 'ok');
        else toast(`连通失败：${res.error}`, 'error', 8000);
      } catch (err) { toast(err.message, 'error', 8000); }
      finally { setBusy(btn, false); }
    }
  });
}

// ---------------------------- 用户管理 ----------------------------
const USER_ROLES = [
  ['engineer', '工程师'], ['reviewer', '专家（复核）'], ['admin', '管理员'],
];
const USER_STATUS = [
  ['invited', '待激活'], ['active', '启用'], ['disabled', '停用'], ['expired', '已到期'],
];
let userQuery = { page: 1, page_size: 20, q: '', role: '', status: '' };

function userRoleTag(role) {
  if (role === 'admin') return '<span class="badge brand"><i class="dot"></i>管理员</span>';
  if (role === 'reviewer') return '<span class="badge info"><i class="dot"></i>专家</span>';
  return '<span class="badge muted"><i class="dot"></i>工程师</span>';
}

export async function renderUsers() {
  const view = mount('用户管理', '维护登录名、角色与专业领域；新建账号统一使用一次性初始密码',
    `<button class="btn btn-primary" data-act="new-user">${icon('plus')}添加用户</button>`);
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let data;
  try {
    data = await api(`/api/users?${new URLSearchParams(userQuery).toString()}`);
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const users = data.items || [];
  const pages = Math.max(1, Math.ceil((data.total || 0) / (data.page_size || 20)));
  view.innerHTML = `
    <div class="card"><div class="card-body">
      <div class="toolbar">
        <div class="search">${icon('search')}<input class="input" id="user-search" placeholder="用户名 / 领域" value="${esc(userQuery.q)}"></div>
        <select class="select" id="user-role" aria-label="按角色筛选用户" style="width:150px">
          <option value="">全部角色</option>${USER_ROLES.map(([v, t]) => `<option value="${v}" ${userQuery.role === v ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
        <select class="select" id="user-status" aria-label="按状态筛选用户" style="width:130px">
          <option value="">全部状态</option>${USER_STATUS.map(([v, t]) => `<option value="${v}" ${userQuery.status === v ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
        <button class="btn" data-act="search">筛选</button>
        <button class="btn btn-ghost" data-act="reset">重置</button>
        <span class="small muted" style="margin-left:auto">共 ${data.total || 0} 个账号</span>
        <button class="btn btn-danger btn-sm" data-act="batch-disable">批量停用</button>
      </div>
    </div></div>
    <div class="card" style="margin-top:12px"><div class="card-body tight table-wrap">
      <table class="data responsive"><thead><tr><th style="width:32px"><input type="checkbox" id="user-check-all" aria-label="全选用户"></th>
        <th>用户</th><th>角色</th><th>状态</th><th>领域</th><th>最近登录</th><th>到期</th><th>操作</th></tr></thead>
      <tbody>${users.length ? users.map((u) => `<tr>
        <td data-label="选择"><input type="checkbox" class="user-check" aria-label="选择用户" value="${u.id}"></td>
        <td data-label="用户"><div style="font-weight:600">${esc(u.display_name || u.username)}</div><div class="mono tiny muted">${esc(u.username)}</div></td>
        <td data-label="角色">${userRoleTag(u.role)}</td>
        <td data-label="状态">${statusBadge(u.status, { active: '启用', invited: '待激活', disabled: '停用', expired: '已到期' }[u.status] || u.status)}${u.must_change ? ' <span class="badge warn"><i class="dot"></i>待改密</span>' : ''}</td>
        <td data-label="领域" class="small">${esc(u.domain || '—')}</td>
        <td data-label="最近登录" class="muted nowrap">${u.last_login_at ? fmtDate(u.last_login_at) : '—'}</td>
        <td data-label="到期" class="muted nowrap">${u.expires_at ? fmtDate(u.expires_at) : '长期'}</td>
        <td data-label="操作"><div class="inline"><button class="btn btn-sm" data-act="detail-user" data-id="${u.id}">详情</button>
          <button class="btn btn-sm" data-act="edit-user" data-id="${u.id}">编辑</button></div></td></tr>`).join('')
        : '<tr><td colspan="8" class="muted" style="text-align:center;padding:28px">没有匹配的账号</td></tr>'}</tbody></table>
    </div>
    <div class="pager"><span class="small muted">第 ${data.page || 1} / ${pages} 页</span>
      <button class="btn btn-sm" data-act="prev" ${(data.page || 1) <= 1 ? 'disabled' : ''}>上一页</button>
      <button class="btn btn-sm" data-act="next" ${(data.page || 1) >= pages ? 'disabled' : ''}>下一页</button></div></div>
    <div class="card" style="margin-top:12px"><div class="card-body small muted">
      <strong>权限矩阵：</strong>管理员拥有全部管理能力；专家只能查看被分配的任务并提交复核，不能创建任务、管理用户/模型、发布技能或升级系统；工程师可创建并查看自己的任务、维护自己的工坊草稿。
    </div></div>`;

  const readFilters = () => {
    userQuery.q = view.querySelector('#user-search').value.trim();
    userQuery.role = view.querySelector('#user-role').value;
    userQuery.status = view.querySelector('#user-status').value;
    userQuery.page = 1;
  };
  const handlers = {
    search: () => { readFilters(); renderUsers(); },
    reset: () => { userQuery = { page: 1, page_size: 20, q: '', role: '', status: '' }; renderUsers(); },
    prev: () => { userQuery.page = Math.max(1, userQuery.page - 1); renderUsers(); },
    next: () => { userQuery.page += 1; renderUsers(); },
    'new-user': () => openUserModal(null, () => renderUsers()),
    'edit-user': (el) => openUserModal(users.find((u) => String(u.id) === el.dataset.id), () => renderUsers()),
    'detail-user': (el) => openUserDetail(Number(el.dataset.id), () => renderUsers()),
    'batch-disable': async () => {
      const ids = [...view.querySelectorAll('.user-check:checked')].map((el) => Number(el.value));
      if (!ids.length) { toast('请先勾选要停用的账号', 'warn'); return; }
      if (!await confirmDialog(`将停用 ${ids.length} 个账号，并立即撤销其全部会话。审计关联账号不会被删除。`, { danger: true, okText: '确认停用' })) return;
      try {
        const res = await api('/api/users/batch-disable', { method: 'POST', body: { ids } });
        toast(`已停用 ${res.disabled} 个账号`, 'ok'); renderUsers();
      } catch (err) { toast(err.message, 'error'); }
    },
  };
  bindActions(view, handlers);
  bindTopbar(handlers);
  view.querySelector('#user-search').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); readFilters(); renderUsers(); }
  });
  view.querySelector('#user-check-all')?.addEventListener('change', (e) => {
    view.querySelectorAll('.user-check').forEach((el) => { el.checked = e.target.checked; });
  });
  return view;
}

function openInviteModal(onDone) {
  const m = modal({
    title: '邀请开户',
    body: `<form id="invite-form">
      <div class="alert info" style="margin-bottom:14px">${icon('info')}<div>生成一次性激活码，默认 24 小时有效。激活码只显示一次，数据库只保存哈希。</div></div>
      <div class="form-grid">
        <div class="form-row"><label class="label">用户名</label><input class="input mono" name="username" required pattern="[A-Za-z0-9._-]{3,32}" placeholder="zhangsan"></div>
        <div class="form-row"><label class="label">显示名</label><input class="input" name="display_name" placeholder="张三"></div>
        <div class="form-row"><label class="label">角色</label><select class="select" name="role">${USER_ROLES.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select></div>
        <div class="form-row"><label class="label">有效期</label><select class="select" name="hours"><option value="24">24 小时</option><option value="72">3 天</option><option value="168">7 天</option></select></div>
      </div>
    </form>`,
    footer: `<button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="create">生成激活码</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    if (e.target.closest('[data-act="create"]')) {
      const form = m.body.querySelector('#invite-form');
      const btn = e.target.closest('[data-act="create"]');
      const payload = Object.fromEntries(new FormData(form).entries());
      payload.hours = Number(payload.hours || 24);
      if (!payload.username) { toast('用户名必填', 'warn'); return; }
      setBusy(btn, true, '生成中…');
      try {
        const data = await api('/api/users/invites', { method: 'POST', body: payload });
        m.body.innerHTML = `<div class="alert info">${icon('info')}<div>请将激活码通过可信渠道交给 <strong>${esc(data.invite.username)}</strong>。关闭后无法再次查看。</div></div>
          <div class="invite-code">${esc(data.code)}</div>
          <div class="help">有效期至 ${fmtDate(data.invite.expires_at)} · 角色 ${esc(data.invite.role)}</div>`;
        m.foot.innerHTML = `<button class="btn" data-act="copy">复制激活码</button><button class="btn btn-primary" data-act="done">完成</button>`;
        m.mask.addEventListener('click', (ev) => {
          if (ev.target.closest('[data-act="copy"]')) {
            navigator.clipboard?.writeText(data.code).then(() => toast('已复制', 'ok')).catch(() => toast('复制失败，请手工选择', 'warn'));
          }
          if (ev.target.closest('[data-act="done"]')) { m.close(); onDone && onDone(); }
        });
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

const USER_DOMAINS = ['时钟同步', '电源', '光模块', '链路', '复位', '升级', '射频', '传输', '其他'];

function openUserModal(user, onDone) {
  const domains = [...new Set([...USER_DOMAINS, user?.domain].filter(Boolean))];
  const m = modal({
    title: user ? `编辑用户 · ${user.username}` : '添加用户',
    body: `<form id="user-form">
      <div class="alert info" style="margin-bottom:14px">${icon('info')}<div>用户名同时作为显示名。新建账号会强制在首次登录时修改初始密码。</div></div>
      <div class="form-row"><label class="label">用户名（显示名）</label>
        <input class="input mono" name="username" value="${esc(user?.username || '')}" ${user ? 'disabled' : 'required'} placeholder="zhangsan" pattern="[A-Za-z0-9._-]{3,32}">
        <div class="help">3–32 位，可使用字母、数字、点、下划线或短横线。</div></div>
      <div class="form-grid">
        <div class="form-row"><label class="label">角色</label>
          <select class="select" name="role">${USER_ROLES.map(([v, t]) => `<option value="${v}" ${user?.role === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
        <div class="form-row"><label class="label">领域</label>
          <input class="input" name="domain" list="user-domain-options" value="${esc(user?.domain || '')}" placeholder="如：时钟同步">
          <datalist id="user-domain-options">${domains.map((d) => `<option value="${esc(d)}"></option>`).join('')}</datalist>
          <div class="help">可从常用领域选择，也可直接输入。</div></div>
      </div>
      <div class="form-row"><label class="label">${user ? '重置密码（留空不修改）' : '初始密码'}</label>
        <input class="input" type="text" name="password" ${user ? '' : 'required'} minlength="9" autocomplete="off"
          value="${user ? '' : 'bbu-skill'}" placeholder="${user ? '留空保持不变' : 'bbu-skill'}">
        <div class="help">${user ? '重置后全部会话失效，并要求重新登录改密。' : '默认 bbu-skill，可直接修改；首次登录必须改密，之后密码至少 10 位。'}</div></div>
    </form>`,
    footer: `<button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="save">${user ? '保存' : '添加'}</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    if (e.target.closest('[data-act="save"]')) {
      const form = m.body.querySelector('#user-form');
      const btn = e.target.closest('[data-act="save"]');
      const username = form.elements.username.value.trim();
      const payload = {
        role: form.elements.role.value,
        domain: form.elements.domain.value.trim(),
      };
      const password = form.elements.password.value.trim();
      if (password) payload.password = password;
      if (user) {
        if (!username) { toast('用户名不能为空', 'warn'); return; }
      } else {
        payload.username = username;
        payload.display_name = username;
        if (!username) { toast('用户名不能为空', 'warn'); return; }
        if (!password) { toast('初始密码不能为空', 'warn'); return; }
      }
      setBusy(btn, true, '保存中…');
      try {
        if (user) await api(`/api/users/${user.id}`, { method: 'PUT', body: payload });
        else await api('/api/users', { method: 'POST', body: payload });
        toast(user ? '用户已更新' : '用户已添加', 'ok');
        m.close(); onDone && onDone();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

async function openUserDetail(userId, onDone) {
  let data;
  try { data = await api(`/api/users/${userId}`); } catch (err) { toast(err.message, 'error'); return; }
  const u = data.user;
  const m = modal({
    title: `账号详情 · ${u.username}`,
    wide: true,
    body: `<div class="form-grid">
        <div class="mini-stat"><div class="k">状态</div><div class="v">${esc({ active: '启用', invited: '待激活', disabled: '停用', expired: '已到期' }[u.status] || u.status)}</div></div>
        <div class="mini-stat"><div class="k">角色</div><div class="v">${esc({ admin: '管理员', reviewer: '专家', engineer: '工程师' }[u.role] || u.role)}</div></div>
        <div class="mini-stat"><div class="k">创建任务</div><div class="v">${data.counts.tasks_created}</div></div>
        <div class="mini-stat"><div class="k">已提交复核</div><div class="v">${data.counts.reviews_submitted}</div></div>
      </div>
      <div class="detail-meta" style="margin:14px 0 6px">
        <span>用户名：${esc(u.username)}</span><span>领域：${esc(u.domain || '—')}</span>
        <span>最近登录：${u.last_login_at ? fmtDate(u.last_login_at) : '—'} ${u.last_login_ip ? '(' + esc(u.last_login_ip) + ')' : ''}</span>
        <span>到期：${u.expires_at ? fmtDate(u.expires_at) : '长期'}</span>
      </div>
      <h3 style="margin:18px 0 8px;font-size:14px">活跃会话（${data.sessions.length}）</h3>
      <div class="table-wrap"><table class="data"><thead><tr><th>创建</th><th>到期</th><th>IP</th><th>UA</th></tr></thead>
        <tbody>${data.sessions.length ? data.sessions.map((s) => `<tr><td class="tiny muted">${fmtDate(s.created_at)}</td><td class="tiny muted">${fmtDate(s.expires_at)}</td><td class="mono tiny">${esc(s.ip || '—')}</td><td class="tiny muted" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.ua || '—')}</td></tr>`).join('') : '<tr><td colspan="4" class="muted">无活跃会话</td></tr>'}</tbody></table></div>
      <h3 style="margin:18px 0 8px;font-size:14px">最近审计</h3>
      <div class="table-wrap"><table class="data"><thead><tr><th>时间</th><th>动作</th><th>对象</th><th>IP</th></tr></thead>
        <tbody>${(data.recent_audits || []).map((a) => `<tr><td class="tiny muted">${fmtDate(a.ts)}</td><td><span class="chip">${esc(a.action)}</span></td><td class="mono tiny">${esc(a.target || '—')}</td><td class="mono tiny">${esc(a.ip || '—')}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">暂无记录</td></tr>'}</tbody></table></div>`,
    footer: `<button class="btn" data-act="close">关闭</button>
      <button class="btn btn-danger" data-act="disable" ${u.active ? '' : 'disabled'}>停用账号</button>
      <button class="btn" data-act="force">强制下线</button>
      <button class="btn" data-act="reset">重置密码</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="close"]')) m.close();
    if (e.target.closest('[data-act="force"]')) {
      if (!await confirmDialog('立即撤销该账号全部登录会话？用户下次请求会返回 401。', { danger: true })) return;
      try { const res = await api(`/api/users/${u.id}/force-logout`, { method: 'POST' }); toast(`已撤销 ${res.sessions_revoked} 个会话`, 'ok'); m.close(); openUserDetail(u.id, onDone); }
      catch (err) { toast(err.message, 'error'); }
    }
    if (e.target.closest('[data-act="disable"]')) {
      if (!await confirmDialog('停用后该账号立即失效，已有会话全部撤销。审计记录和任务不会删除。', { danger: true })) return;
      try { await api(`/api/users/${u.id}`, { method: 'PUT', body: { active: 0 } }); toast('账号已停用', 'ok'); m.close(); onDone && onDone(); }
      catch (err) { toast(err.message, 'error'); }
    }
    if (e.target.closest('[data-act="reset"]')) { m.close(); openResetPassword(u, () => openUserDetail(u.id, onDone)); }
  });
}

function openResetPassword(user, onDone) {
  const m = modal({
    title: `重置密码 · ${user.username}`,
    body: `<div class="alert warn">${icon('alert')}<div>重置后该账号全部会话立即失效，下次登录必须改密。</div></div>
      <form id="reset-form" style="margin-top:14px"><div class="form-row"><label class="label">新临时密码</label>
        <input class="input" type="password" name="password" minlength="10" required autocomplete="new-password" placeholder="至少 10 位，需包含两类字符"></div></form>`,
    footer: `<button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="save">重置</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    if (e.target.closest('[data-act="save"]')) {
      const pwd = m.body.querySelector('#reset-form').elements.password.value;
      if (!pwd) { toast('请输入临时密码', 'warn'); return; }
      const btn = e.target.closest('[data-act="save"]'); setBusy(btn, true, '保存中…');
      try { await api(`/api/users/${user.id}`, { method: 'PUT', body: { password: pwd } }); toast('密码已重置', 'ok'); m.close(); onDone && onDone(); }
      catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

// ---------------------------- 审计日志 ----------------------------
let auditPage = 1;
let auditFilter = { start: '', end: '', username: '', action: '', target: '', result: '' };

function auditQueryString() {
  const params = new URLSearchParams({ page: auditPage, page_size: 30 });
  Object.entries(auditFilter).forEach(([k, v]) => { if (v) params.set(k, v); });
  return params.toString();
}

export async function renderAudit() {
  const view = mount('审计日志', '登录、任务、技能、模型与用户操作全部留痕；支持筛选与 CSV 导出', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let data;
  try {
    data = await api(`/api/audit?${auditQueryString()}`);
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const pages = Math.max(1, Math.ceil((data.total || 0) / (data.page_size || 30)));
  view.innerHTML = `
    <div class="card"><div class="card-body">
      <div class="toolbar">
        <input class="input" type="date" id="audit-start" aria-label="审计开始日期" value="${esc(auditFilter.start)}" style="width:150px">
        <input class="input" type="date" id="audit-end" aria-label="审计结束日期" value="${esc(auditFilter.end)}" style="width:150px">
        <input class="input" id="audit-user" placeholder="用户" value="${esc(auditFilter.username)}" style="width:130px">
        <input class="input" id="audit-action" placeholder="动作，如 auth.login" value="${esc(auditFilter.action)}" style="width:180px">
        <input class="input" id="audit-target" placeholder="对象" value="${esc(auditFilter.target)}" style="width:150px">
        <select class="select" id="audit-result" aria-label="按结果筛选审计" style="width:120px"><option value="">全部结果</option><option value="ok" ${auditFilter.result === 'ok' ? 'selected' : ''}>成功</option><option value="failed" ${auditFilter.result === 'failed' ? 'selected' : ''}>失败/拒绝</option></select>
        <button class="btn" data-act="search">筛选</button>
        <button class="btn btn-ghost" data-act="reset">重置</button>
        <button class="btn" data-act="export">${icon('download')}导出 CSV</button>
      </div>
    </div></div>
    <div class="card" style="margin-top:12px"><div class="card-body tight table-wrap"><table class="data responsive"><thead><tr>
      <th>时间</th><th>操作</th><th>用户</th><th>对象</th><th>详情</th><th>IP</th></tr></thead><tbody>
      ${(data.items || []).length ? data.items.map((a) => `<tr>
        <td data-label="时间" class="muted nowrap tiny">${fmtDate(a.ts)}</td>
        <td data-label="操作"><span class="chip">${esc(a.action)}</span></td>
        <td data-label="用户">${esc(a.username || '—')}</td>
        <td data-label="对象" class="mono tiny">${esc(a.target || '—')}</td>
        <td data-label="详情" class="small" style="max-width:360px">${Object.keys(a.detail || {}).length
          ? `<details><summary class="muted tiny" style="cursor:pointer">查看</summary><pre class="mono tiny" style="white-space:pre-wrap;margin:4px 0 0">${esc(JSON.stringify(a.detail, null, 2))}</pre></details>`
          : '<span class="muted">—</span>'}</td>
        <td data-label="IP" class="mono tiny muted">${esc(a.ip || '—')}</td></tr>`).join('')
        : '<tr><td colspan="6" class="muted" style="text-align:center;padding:28px">没有匹配的审计记录</td></tr>'}
    </tbody></table></div>
    <div class="pager"><span class="small muted">共 ${data.total || 0} 条 · 第 ${data.page || 1} / ${pages} 页</span>
      <button class="btn btn-sm" data-act="prev" ${(data.page || 1) <= 1 ? 'disabled' : ''}>上一页</button>
      <button class="btn btn-sm" data-act="next" ${(data.page || 1) >= pages ? 'disabled' : ''}>下一页</button>
      <button class="btn btn-sm" data-act="refresh">${icon('refresh')}刷新</button></div></div>`;
  const applyFilters = () => {
    auditFilter.start = view.querySelector('#audit-start').value;
    auditFilter.end = view.querySelector('#audit-end').value;
    auditFilter.username = view.querySelector('#audit-user').value.trim();
    auditFilter.action = view.querySelector('#audit-action').value.trim();
    auditFilter.target = view.querySelector('#audit-target').value.trim();
    auditFilter.result = view.querySelector('#audit-result').value;
    auditPage = 1;
    renderAudit();
  };
  bindActions(view, {
    search: applyFilters,
    reset: () => { auditFilter = { start: '', end: '', username: '', action: '', target: '', result: '' }; auditPage = 1; renderAudit(); },
    prev: () => { auditPage = Math.max(1, auditPage - 1); renderAudit(); },
    next: () => { auditPage += 1; renderAudit(); },
    refresh: () => renderAudit(),
    export: () => { location.href = `/api/audit/export?${auditQueryString().replace(/page=\d+&page_size=\d+&?/, '')}`; },
  });
  view.querySelectorAll('.toolbar input, .toolbar select').forEach((el) => el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); applyFilters(); }
  }));
  return view;
}


// ---------------------------- 系统信息 ----------------------------
export async function renderSystem() {
  const view = mount('系统信息', '版本、路径、并发与运行配置', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let info;
  try { info = await api('/api/system/info'); }
  catch (err) { view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`; return; }
  const config = info.config || {};
  const isAdmin = state.user?.role === 'admin';
  let updates = null;
  let migrations = null;
  if (isAdmin) {
    try { updates = await api('/api/system/updates'); }
    catch (err) { updates = { error: err.message, items: [] }; }
    try { migrations = await api('/api/system/migrations'); }
    catch (err) { migrations = { error: err.message, items: [] }; }
  }
  let mcp = null;
  try { mcp = await api('/api/mcp/info'); }
  catch (err) { mcp = { error: err.message }; }
  return renderSystemView(view, info, config, updates, migrations, mcp);
}


// ---------------------------- 工作目录与迁移 ----------------------------
const MIGRATION_PHASES = {
  queued: '排队', preparing: '准备数据', packaging: '打包加密', ready: '可下载',
  switching: '切换中', restoring: '恢复中', waiting_restart: '等待重启',
  succeeded: '成功', failed: '失败', rolled_back: '已回滚',
};
let migrationPollTimer = null;

function migrationPhaseLabel(phase) {
  return MIGRATION_PHASES[phase] || phase || '—';
}

function migrationKindLabel(item) {
  return { export: '导出迁移包', workspace: '切换工作目录', import: '覆盖恢复' }[item.kind || item.action] || '迁移任务';
}

function migrationJobCard(item) {
  const active = ['queued', 'preparing', 'packaging', 'switching', 'restoring'].includes(item.phase);
  const canDelete = !active && item.phase !== 'waiting_restart';
  const download = item.kind === 'export' && item.phase === 'ready' && item.download_url;
  const counts = item.db_counts || {};
  const countText = Object.keys(counts).length
    ? Object.entries(counts).slice(0, 6).map(([k, v]) => `${k}=${v}`).join(' · ') : '';
  return `<div class="card" data-job="${esc(item.id)}" style="margin-top:12px;background:#f8fafc">
    <div class="card-body">
      <div class="between" style="gap:12px;flex-wrap:wrap">
        <div><strong>${esc(migrationKindLabel(item))} ${esc(item.id)}</strong>
          <span class="badge ${item.phase === 'failed' ? 'danger' : item.phase === 'succeeded' ? 'ok' : 'info'}">${esc(migrationPhaseLabel(item.phase))}</span>
          ${item.requires_restart ? '<span class="badge warn">需重启</span>' : ''}</div>
        <span class="tiny muted">${fmtDate(item.created_at)}</span>
      </div>
      <div class="upload-progress" style="margin-top:10px"><div style="width:${Number(item.progress || 0)}%"></div></div>
      <div class="between tiny muted" style="margin-top:5px"><span class="migration-message">${esc(item.message || '')}</span><span class="migration-percent">${Number(item.progress || 0)}%</span></div>
      ${item.error ? `<div class="alert danger" style="margin-top:10px">${icon('alert')}<div>${esc(item.error)}</div></div>` : ''}
      ${item.target_root ? `<div class="kv small" style="margin-top:8px"><div class="k">目标工作目录</div><div class="v mono">${esc(item.target_root)}</div></div>` : ''}
      ${countText ? `<div class="tiny muted mono" style="margin-top:8px">${esc(countText)}</div>` : ''}
      ${item.package_sha256 ? `<div class="kv small" style="margin-top:8px"><div class="k">SHA256</div><div class="v mono tiny">${esc(item.package_sha256)}</div>
        <div class="k">包大小</div><div class="v">${((item.size || 0) / 1024 / 1024).toFixed(2)} MB</div></div>` : ''}
      ${item.backup_dir ? `<div class="tiny muted" style="margin-top:8px">旧数据备份：<span class="mono">${esc(item.backup_dir)}</span></div>` : ''}
      <div class="inline" style="margin-top:10px;flex-wrap:wrap">
        ${download ? `<a class="btn btn-primary" href="${esc(download)}">${icon('download')}下载迁移包</a>` : ''}
        ${canDelete ? `<button class="btn btn-sm btn-danger" data-act="delete-migration" data-id="${esc(item.id)}">${icon('trash')}删除记录</button>` : ''}
      </div>
    </div></div>`;
}

function renderMigrationPanel(panel, data) {
  if (!panel) return;
  if (!data) {
    panel.innerHTML = `<div class="card"><div class="card-body"><div class="alert danger">${icon('alert')}<div>无法读取迁移状态</div></div></div></div>`;
    return;
  }
  if (data.error) {
    panel.innerHTML = `<div class="card"><div class="card-head"><h2>工作目录与迁移</h2></div>
      <div class="card-body"><div class="alert danger">${icon('alert')}<div>${esc(data.error)}</div></div></div></div>`;
    return;
  }
  const active = data.active_job || '';
  const items = data.items || [];
  const roots = (data.allowed_roots || []).join('、') || '由部署配置决定';
  const restart = data.requires_restart_after_prepare
    ? 'Windows 会在准备完成后写入待激活文件，需要手动停止并重新启动平台才会生效。'
    : 'Linux 由 root/systemd 助手自动停服、切换并重启；页面会短暂断线。';
  panel.innerHTML = `
    <div class="card" id="migration-card" style="margin-top:16px">
      <div class="card-head"><h2>工作目录与迁移</h2><span class="sub">${esc(data.helper || '')}</span></div>
      <div class="card-body">
        <div class="alert info">${icon('shield')}<div>
          ${esc(data.input_policy || '')}<br>
          迁移包使用独立口令加密，包含用户与密码哈希、模型 Key、master.key、技能、案例/任务元数据和报告；不含原始上传、案例输入、解压目录、登录会话、登录挑战和用户邀请码。
        </div></div>
        <div class="kv small" style="margin-top:10px">
          <div class="k">当前工作目录</div><div class="v mono">${esc(data.current_work_dir)}</div>
          <div class="k">data / skills / cases</div><div class="v mono tiny">${esc(data.paths?.data)}<br>${esc(data.paths?.skills)}<br>${esc(data.paths?.cases)}</div>
          <div class="k">允许根目录</div><div class="v">${esc(roots)}</div>
          <div class="k">磁盘可用</div><div class="v">${data.disk_free_mb ?? '—'} MB · 单包上限 ${data.max_migration_mb || '—'} MB</div>
        </div>
        ${active ? `<div class="alert warn" style="margin-top:10px">${icon('activity')}<div>迁移任务 <span class="mono">${esc(active)}</span> 正在执行；所有写请求会被暂停，只保留状态查询和下载。</div></div>` : ''}
        <div class="grid grid-2" style="margin-top:14px;align-items:start">
          <div class="card" style="background:#f8fafc"><div class="card-head"><h2 class="small">切换工作目录</h2></div><div class="card-body">
            <div class="form-row"><label class="label" for="migration-target">新工作目录绝对路径</label>
              <input class="input mono" id="migration-target" placeholder="/srv/bbu-workdir" ${active ? 'disabled' : ''}>
              <div class="help">目标必须为空或尚不存在；只迁移精简业务数据，旧目录会保留。${esc(restart)}</div>
            </div>
            <div class="inline" style="margin-top:10px;flex-wrap:wrap">
              <button class="btn" id="migration-preflight-btn" ${active ? 'disabled' : ''}>${icon('check')}预检路径</button>
              <button class="btn btn-primary" id="migration-workspace-btn" ${active ? 'disabled' : ''}>${icon('play')}开始切换</button>
            </div>
            <div id="migration-preflight-state" class="small muted" style="margin-top:8px"></div>
          </div></div>
          <div class="card" style="background:#f8fafc"><div class="card-head"><h2 class="small">导出迁移包</h2></div><div class="card-body">
            <div class="form-row"><label class="label" for="migration-export-pass">独立迁移口令 <span class="hint">至少 12 位，仅本次使用</span></label>
              <input class="input mono" id="migration-export-pass" type="password" autocomplete="new-password" ${active ? 'disabled' : ''}></div>
            <div class="form-row"><label class="label" for="migration-export-pass2">再次输入迁移口令</label>
              <input class="input mono" id="migration-export-pass2" type="password" autocomplete="new-password" ${active ? 'disabled' : ''}></div>
            <button class="btn btn-primary" id="migration-export-btn" style="margin-top:10px" ${active ? 'disabled' : ''}>${icon('upload')}导出并加密</button>
            <div class="help">导出在后台执行，完成后在下方任务记录中下载 .bbumig；请通过安全渠道传递迁移包和口令。</div>
          </div></div>
        </div>
        <section class="migration-danger">
          <div class="migration-danger-aside">
            <div class="migration-danger-icon">${icon('alert')}</div>
            <div class="migration-danger-title"><h3>覆盖恢复</h3><span class="badge danger">高风险</span></div>
            <p>导入会用迁移包替换目标工作目录的 data / skills / cases，不做增量合并。请确认这是新实例或已做好停机准备。</p>
            <ul class="migration-danger-list">
              <li>当前业务数据先备份到 <code>backups/import-任务ID</code></li>
              <li>健康检查失败自动恢复旧目录</li>
              <li>迁移包与口令确认后才开始上传</li>
            </ul>
            <div class="migration-danger-tip">${icon('shield')}建议先在测试实例验证迁移包</div>
          </div>
          <div class="migration-danger-form">
            <label class="migration-dropzone ${active ? 'is-disabled' : ''}" id="migration-import-file-box">
              <input class="migration-file-input" type="file" id="migration-import-file"
                accept=".bbumig,application/octet-stream" ${active ? 'disabled' : ''}>
              <span class="migration-drop-icon">${icon('upload')}</span>
              <span class="migration-drop-copy">
                <strong id="migration-import-file-name">选择迁移包</strong>
                <span id="migration-import-file-meta">支持 .bbumig，单包上限 ${data.max_migration_mb || '—'} MB</span>
              </span>
              <span class="btn btn-sm">浏览文件</span>
            </label>
            <div class="migration-pass-grid">
              <div class="form-row"><label class="label" for="migration-import-pass">迁移口令</label>
                <input class="input mono" id="migration-import-pass" type="password" autocomplete="new-password" ${active ? 'disabled' : ''}></div>
              <div class="form-row"><label class="label" for="migration-import-pass2">再次输入迁移口令</label>
                <input class="input mono" id="migration-import-pass2" type="password" autocomplete="new-password" ${active ? 'disabled' : ''}></div>
            </div>
            <div class="migration-import-actions">
              <div id="migration-import-state" class="small muted">选择迁移包并填写两次口令后上传。</div>
              <button class="btn btn-danger" id="migration-import-btn" ${active ? 'disabled' : ''}>${icon('upload')}上传并恢复</button>
            </div>
          </div>
        </section>
        <div class="migration-records">
          <div class="migration-records-head"><div><h3>迁移记录</h3><span class="sub">导出、切换与恢复任务</span></div><span class="tiny muted">保留最近 30 条 · 导出包需手动删除</span></div>
          ${items.length ? `<div class="migration-record-list">${items.map(migrationJobCard).join('')}</div>` : `<div class="migration-empty">${icon('history')}<div><strong>暂无迁移任务</strong><p>导出、切换工作目录或覆盖恢复后，任务进度会显示在这里。</p></div></div>`}
        </div>
      </div>
    </div>`;
  bindMigrationActions(panel, data);
  if (active) startMigrationPoll(active, panel);
}

function bindMigrationActions(panel, data) {
  const targetInput = panel.querySelector('#migration-target');
  const preflightBtn = panel.querySelector('#migration-preflight-btn');
  preflightBtn?.addEventListener('click', async () => {
    const target = targetInput?.value.trim();
    if (!target) { toast('请输入目标工作目录绝对路径', 'error'); return; }
    setBusy(preflightBtn, true, '预检中');
    try {
      const res = await api('/api/system/migrations/workspace/preflight', {
        method: 'POST', body: { target_root: target },
      });
      panel.querySelector('#migration-preflight-state').innerHTML =
        `<span class="ok-text">预检通过：${esc(res.target_root)}，剩余 ${res.disk_free_mb} MB</span><div class="tiny muted">${esc(res.note || '')}</div>`;
    } catch (err) {
      panel.querySelector('#migration-preflight-state').innerHTML = `<span class="danger-text">${esc(err.message)}</span>`;
    } finally { setBusy(preflightBtn, false); }
  });

  panel.querySelector('#migration-workspace-btn')?.addEventListener('click', () => {
    const target = targetInput?.value.trim();
    if (!target) { toast('请输入目标工作目录绝对路径', 'error'); return; }
    askMigrationPassword({
      title: '切换工作目录',
      message: `将把精简业务数据迁移到 ${target}；旧目录保留。${data.requires_restart_after_prepare ? '准备完成后需要手动重启平台。' : 'root 助手会停止服务、切换目录并重启。'}`,
      confirmText: '确认切换',
      danger: true,
      onSubmit: async (password, btn) => {
        setBusy(btn, true, '提交中');
        const res = await api('/api/system/migrations/workspace', {
          method: 'POST', body: { target_root: target, password },
        });
        toast('已提交工作目录切换', 'ok');
        await refreshMigrationPanel(panel);
        startMigrationPoll(res.migration.id, panel);
      },
    });
  });

  panel.querySelector('#migration-export-btn')?.addEventListener('click', () => {
    const pass = panel.querySelector('#migration-export-pass')?.value || '';
    const pass2 = panel.querySelector('#migration-export-pass2')?.value || '';
    if (pass.length < 12) { toast('迁移口令至少 12 位', 'error'); return; }
    if (pass !== pass2) { toast('两次输入的迁移口令不一致', 'error'); return; }
    askMigrationPassword({
      title: '导出加密迁移包',
      message: '导出会生成包含用户、密钥、技能与业务元数据的加密包，不含原始上传、案例输入和解压数据。请妥善保管迁移口令。',
      confirmText: '开始导出',
      onSubmit: async (password, btn) => {
        setBusy(btn, true, '提交中');
        const res = await api('/api/system/migrations/export', {
          method: 'POST', body: { passphrase: pass, password },
        });
        toast('已开始导出，完成后可下载', 'ok');
        await refreshMigrationPanel(panel);
        startMigrationPoll(res.migration.id, panel);
      },
    });
  });

  const fileInput = panel.querySelector('#migration-import-file');
  const fileBox = panel.querySelector('#migration-import-file-box');
  const fileName = panel.querySelector('#migration-import-file-name');
  const fileMeta = panel.querySelector('#migration-import-file-meta');
  const importHint = `支持 .bbumig，单包上限 ${data.max_migration_mb || '—'} MB`;
  const renderImportFile = () => {
    const file = fileInput?.files?.[0];
    fileBox?.classList.toggle('has-file', Boolean(file));
    if (fileName) fileName.textContent = file ? file.name : '选择迁移包';
    if (fileMeta) fileMeta.textContent = file
      ? `${(file.size / 1024 / 1024).toFixed(2)} MB · 点击可重新选择` : importHint;
    const stateEl = panel.querySelector('#migration-import-state');
    if (stateEl) stateEl.textContent = file
      ? '已选择迁移包，填写两次口令后上传并恢复。' : '选择迁移包并填写两次口令后上传。';
  };
  fileInput?.addEventListener('change', renderImportFile);
  panel.querySelector('#migration-import-btn')?.addEventListener('click', () => {
    const file = fileInput?.files?.[0];
    const pass = panel.querySelector('#migration-import-pass')?.value || '';
    const pass2 = panel.querySelector('#migration-import-pass2')?.value || '';
    if (!file) { toast('请选择 .bbumig 迁移包', 'error'); return; }
    if (!file.name.toLowerCase().endsWith('.bbumig')) { toast('迁移包必须使用 .bbumig 后缀', 'error'); return; }
    if (pass.length < 12) { toast('迁移口令至少 12 位', 'error'); return; }
    if (pass !== pass2) { toast('两次输入的迁移口令不一致', 'error'); return; }
    askMigrationPassword({
      title: '覆盖恢复迁移包',
      message: `即将用 ${file.name} 覆盖当前 data / skills / cases。当前数据会先备份，健康检查失败会自动回滚。此操作不可增量合并。`,
      confirmText: '确认覆盖恢复',
      danger: true,
      onSubmit: async (password) => {
        uploadMigrationPackage(file, pass, password, panel);
      },
    });
  });

  panel.querySelectorAll('[data-act="delete-migration"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.id;
      askMigrationPassword({
        title: '删除迁移记录',
        message: `删除任务 ${id} 的状态与文件？已下载的迁移包不受影响。`,
        confirmText: '删除',
        danger: true,
        onSubmit: async (password, actionBtn) => {
          setBusy(actionBtn, true, '删除中');
          await api(`/api/system/migrations/${encodeURIComponent(id)}`, { method: 'DELETE', body: { password } });
          toast('迁移记录已删除', 'ok');
          await refreshMigrationPanel(panel);
        },
      });
    });
  });
}

function askMigrationPassword({ title, message, confirmText, danger = false, onSubmit }) {
  const m = modal({
    title,
    body: `<div class="alert ${danger ? 'danger' : 'info'}">${icon('alert')}<div>${esc(message)}</div></div>
      <div class="form-row" style="margin-top:12px"><label class="label">重新输入管理员密码</label>
        <input class="input" id="migration-admin-password" type="password" autocomplete="current-password"></div>`,
    footer: `<button class="btn" data-act="cancel">取消</button>
      <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-act="submit">${esc(confirmText)}</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]') || e.target.closest('[data-act="close-modal"]')) { m.close(); return; }
    const btn = e.target.closest('[data-act="submit"]');
    if (!btn) return;
    const password = m.body.querySelector('#migration-admin-password')?.value || '';
    if (!password) { toast('请输入管理员密码', 'error'); return; }
    try {
      const result = await onSubmit(password, btn);
      if (result !== false) m.close();
    } catch (err) {
      toast(err.message, 'error', 7000);
      setBusy(btn, false);
    }
  });
}

async function refreshMigrationPanel(panel) {
  try {
    const data = await api('/api/system/migrations');
    renderMigrationPanel(panel, data);
  } catch (err) {
    panel.innerHTML = `<div class="card"><div class="card-body"><div class="alert warn">${icon('refresh')}<div>服务可能正在重启：${esc(err.message)}</div></div></div></div>`;
  }
}

function uploadMigrationPackage(file, passphrase, password, panel) {
  const stateEl = panel.querySelector('#migration-import-state');
  const btn = panel.querySelector('#migration-import-btn');
  const fd = new FormData();
  fd.append('file', file, file.name);
  fd.append('passphrase', passphrase);
  fd.append('password', password);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/system/migrations/import');
  xhr.setRequestHeader('X-CSRF', state.csrf || '');
  if (btn) btn.disabled = true;
  if (stateEl) stateEl.textContent = '上传中 0%';
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable && stateEl) stateEl.textContent = `上传中 ${Math.round(e.loaded / e.total * 100)}%`;
  };
  xhr.onload = async () => {
    if (btn) btn.disabled = false;
    let data = {};
    try { data = JSON.parse(xhr.responseText || '{}'); } catch { /* ignore */ }
    if (xhr.status >= 200 && xhr.status < 300 && data.migration) {
      toast('迁移包已上传，正在校验并准备恢复', 'ok');
      await refreshMigrationPanel(panel);
      startMigrationPoll(data.migration.id, panel);
    } else {
      if (stateEl) stateEl.innerHTML = `<span class="danger-text">${esc(data.error || data.detail || `上传失败（HTTP ${xhr.status}）`)}</span>`;
    }
  };
  xhr.onerror = () => {
    if (btn) btn.disabled = false;
    if (stateEl) stateEl.innerHTML = '<span class="danger-text">网络中断，上传失败</span>';
  };
  xhr.send(fd);
}

function startMigrationPoll(jobId, panel) {
  if (migrationPollTimer) clearInterval(migrationPollTimer);
  const tick = async () => {
    try {
      const data = await api(`/api/system/migrations/${encodeURIComponent(jobId)}`);
      const job = data.migration || {};
      const card = panel.querySelector(`[data-job="${jobId}"]`);
      if (card) {
        const bar = card.querySelector('.upload-progress div');
        const msg = card.querySelector('.migration-message');
        const pct = card.querySelector('.migration-percent');
        if (bar) bar.style.width = `${Number(job.progress || 0)}%`;
        if (msg && job.message) msg.textContent = job.message;
        if (pct) pct.textContent = `${Number(job.progress || 0)}%`;
      }
      const done = ['succeeded', 'failed', 'rolled_back', 'waiting_restart'].includes(job.phase)
        || (job.kind === 'export' && job.phase === 'ready');
      if (done) {
        clearInterval(migrationPollTimer); migrationPollTimer = null;
        await refreshMigrationPanel(panel);
        if (job.phase === 'waiting_restart') toast('准备完成，请手动重启平台激活', 'warn', 10000);
        if (job.kind === 'export' && job.phase === 'ready') toast('迁移包已生成，可下载', 'ok', 8000);
        if (job.phase === 'failed') toast(`迁移失败：${job.error || ''}`, 'error', 10000);
      }
    } catch { /* 服务重启期间保持页面，用户刷新后重新读取 */ }
  };
  migrationPollTimer = setInterval(tick, 2500);
  tick();
  state.cleanup = () => {
    if (migrationPollTimer) { clearInterval(migrationPollTimer); migrationPollTimer = null; }
  };
}

function updatePhaseLabel(phase) {
  return {
    uploaded: '已上传', verifying: '预检中', ready: '可升级', queued: '等待 root 助手',
    backup: '备份中', migrating: '迁移/替换中', restarting: '重启中',
    health_check: '健康检查', waiting_restart: '待重启生效',
    succeeded: '升级成功', failed: '失败', rolled_back: '已回滚',
  }[phase] || phase || '—';
}

function updateActions(item) {
  return item.action === 'rollback' ? '回滚' : '升级';
}

function renderUpdatePanel(panel, updates, currentVersion) {
  if (!panel) return;
  if (updates.supported === false) {
    panel.innerHTML = `
      <div class="card" id="update-card">
        <div class="card-head"><h2>离线升级</h2><span class="sub">当前 v${esc(currentVersion)}</span></div>
        <div class="card-body">
          <div class="alert info">${icon('shield')}<div>
            当前环境未启用网页升级。请停止平台，备份 <code>data/</code> 与
            <code>config.json</code>，解压新版本后覆盖程序目录，再重新启动即可。
          </div></div>
        </div>
      </div>`;
    return;
  }
  const items = (updates.items || []).slice(0, 10);
  const active = updates.active ? items.find((x) => x.id === updates.active) : null;
  const ready = items.find((x) => x.phase === 'ready') || null;
  const focus = active || ready || items[0] || null;
  const busy = !!active;
  const locked = busy || !!updates.pending_update;
  const restart = updates.mode === 'restart';
  const rollbackEnabled = updates.rollback_supported !== false;
  const helper = updates.helper || (restart ? 'Windows 网页上传 + 重启生效' : 'root/systemd 一次性助手');
  const history = items.filter((x) => x !== focus);
  panel.innerHTML = `
    <div class="card" style="margin-bottom:16px" id="update-card">
      <div class="card-head"><h2>离线升级</h2><span class="sub">当前 v${esc(currentVersion)} · ${esc(helper)}</span></div>
      <div class="card-body">
        <div class="alert ${updates.error ? 'danger' : 'info'}">${icon(updates.error ? 'alert' : 'shield')}<div>
          ${updates.error ? esc(updates.error) : (restart
            ? `Windows 网页上传只做校验和解压；确认后点击“准备重启生效”，关闭平台并重新启动，启动时会自动替换代码并保留 <code>data/</code> 与 <code>config.json</code>。`
            : `升级只接受管理员上传的 .tar.gz 离线包，由 root/systemd 一次性助手执行固定脚本；主 Web 进程不提权。`)}
          ${updates.running_tasks ? `<br><strong>当前有 ${updates.running_tasks} 个诊断任务在排队/运行，升级会被阻止。</strong>` : ''}
          ${updates.disk_free_mb != null ? `<br>磁盘可用 ${updates.disk_free_mb} MB · 单包上限 ${updates.max_update_mb || '—'} MB` : ''}
        </div></div>
        <div class="between" style="margin-top:14px;gap:12px;flex-wrap:wrap">
          <div>
            <label class="label" for="update-file">选择离线升级包（${restart ? '.zip / .tar.gz' : '.tar.gz'}）</label>
            <input type="file" id="update-file" accept=".tar.gz,.tgz,.zip,application/gzip,application/zip" ${locked ? 'disabled' : ''}>
            <div class="tiny muted" id="update-file-name">上传后服务端会计算 SHA256 并检查版本/结构，不会执行包内脚本。</div>
          </div>
          <button class="btn btn-primary" data-act="upload-update" id="update-upload-btn" ${locked ? 'disabled' : ''}>${icon('upload')}上传并预检</button>
        </div>
        <div id="update-upload-state" class="small muted" style="margin-top:8px"></div>
        ${focus ? `<div class="card" style="margin-top:14px;background:#f8fafc">
          <div class="card-body">
            <div class="between"><div><strong>${esc(updateActions(focus))}任务 ${esc(focus.id)}</strong>
              <span class="badge ${focus.phase === 'failed' ? 'danger' : focus.phase === 'succeeded' || focus.phase === 'rolled_back' ? 'ok' : 'info'}">${esc(updatePhaseLabel(focus.phase))}</span></div>
              <span class="small muted">${focus.target_version ? `目标 v${esc(focus.target_version)}` : ''}</span></div>
            <div class="upload-progress" style="margin-top:10px"><div style="width:${Number(focus.progress || 0)}%"></div></div>
            <div class="between tiny muted" style="margin-top:5px"><span>${esc(focus.message || '')}</span><span>${Number(focus.progress || 0)}%</span></div>
            ${focus.error ? `<div class="alert danger" style="margin-top:10px">${icon('alert')}<div>${esc(focus.error)}</div></div>` : ''}
            ${focus.phase === 'waiting_restart' ? `<div class="alert warn" style="margin-top:10px">${icon('refresh')}<div>
              <strong>已准备，等待重启。</strong>关闭当前平台窗口后重新运行 <code>start_platform.bat</code> 或 <code>python run.py</code>；
              启动时会自动替换程序目录并加载新版本。当前页面连接的是旧进程，重启后再刷新。
            </div></div>` : ''}
            ${focus.package_sha256 ? `<div class="kv small" style="margin-top:8px"><div class="k">SHA256</div><div class="v mono">${esc(focus.package_sha256)}</div>
              <div class="k">包大小</div><div class="v">${((focus.size || 0) / 1024 / 1024).toFixed(2)} MB</div>
              ${focus.backup_dir ? `<div class="k">回滚快照</div><div class="v mono">${esc(focus.backup_dir)}</div>` : ''}</div>` : ''}
            ${focus.release_notes ? `<details style="margin-top:8px"><summary class="small muted" style="cursor:pointer">查看版本说明</summary><pre class="small" style="white-space:pre-wrap;max-height:260px;overflow:auto">${esc(focus.release_notes)}</pre></details>` : ''}
            <div class="inline" style="margin-top:10px;flex-wrap:wrap">
              ${focus.phase === 'ready' ? `<button class="btn btn-primary" data-act="apply-update" data-id="${esc(focus.id)}">${icon(restart ? 'refresh' : 'play')}${restart ? '准备重启生效' : '执行升级'}</button>` : ''}
              ${(focus.phase === 'failed' || focus.phase === 'succeeded') && focus.backup_dir && rollbackEnabled ? `<button class="btn btn-danger" data-act="rollback-update" data-id="${esc(focus.id)}">回滚到快照</button>` : ''}
              ${focus.phase === 'failed' || focus.phase === 'ready' ? `<button class="btn btn-sm" data-act="verify-update" data-id="${esc(focus.id)}">重新预检</button>` : ''}
              ${focus.phase === 'succeeded' || focus.phase === 'rolled_back' ? `<button class="btn btn-sm" data-act="reload-after-update">刷新页面</button>` : ''}
            </div>
            ${focus.log_tail ? `<details style="margin-top:10px"><summary class="small muted" style="cursor:pointer">查看助手日志</summary><pre class="mono tiny" style="white-space:pre-wrap;max-height:220px;overflow:auto">${esc(focus.log_tail)}</pre></details>` : ''}
          </div></div>` : ''}
        <div style="margin-top:16px"><div class="between"><strong class="small">升级历史</strong><span class="tiny muted">保留最近 10 条</span></div>
          <div class="table-wrap" style="margin-top:8px"><table class="data"><thead><tr><th>时间</th><th>动作</th><th>版本</th><th>状态</th><th>操作</th></tr></thead><tbody>
            ${history.length ? history.map((x) => `<tr><td class="tiny muted nowrap">${fmtDate(x.created_at)}</td>
              <td>${esc(updateActions(x))}</td>
              <td class="small">v${esc(x.current_version || '—')} → v${esc(x.target_version || '—')}</td>
              <td>${esc(updatePhaseLabel(x.phase))}${x.error ? `<div class="tiny danger-text">${esc(x.error).slice(0, 120)}</div>` : ''}</td>
              <td>${x.phase === 'succeeded' && x.backup_dir && rollbackEnabled ? `<button class="btn btn-sm" data-act="rollback-update" data-id="${esc(x.id)}">回滚</button>` : '—'}</td></tr>`).join('')
              : '<tr><td colspan="5" class="muted" style="text-align:center;padding:18px">暂无升级记录</td></tr>'}
          </tbody></table></div></div>
      </div>
    </div>`;
  const fileInput = panel.querySelector('#update-file');
  fileInput?.addEventListener('change', () => {
    const file = fileInput.files?.[0];
    panel.querySelector('#update-file-name').textContent = file
      ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB` : '上传后服务端会计算 SHA256 并检查版本/结构，不会执行包内脚本。';
  });
  panel.querySelector('[data-act="upload-update"]')?.addEventListener('click', () => {
    const file = fileInput?.files?.[0];
    if (!file) { toast('请选择 .tar.gz / .zip 升级包', 'error'); return; }
    uploadUpdatePackage(file, panel, currentVersion);
  });
  panel.querySelector('[data-act="apply-update"]')?.addEventListener('click', (e) => openApplyUpdate(e.currentTarget.dataset.id, updates.mode));
  panel.querySelectorAll('[data-act="rollback-update"]').forEach((btn) => btn.addEventListener('click', () => openRollbackUpdate(btn.dataset.id)));
  panel.querySelectorAll('[data-act="verify-update"]').forEach((btn) => btn.addEventListener('click', () => verifyUpdate(btn.dataset.id, panel, currentVersion)));
  panel.querySelector('[data-act="reload-after-update"]')?.addEventListener('click', () => location.reload());
  if (active && !updatePollTimer) startUpdatePoll(active.id, panel, currentVersion);
}

async function refreshUpdatePanel(panel, currentVersion) {
  try {
    const data = await api('/api/system/updates');
    renderUpdatePanel(panel, data, currentVersion);
    return data;
  } catch (err) {
    panel.innerHTML = `<div class="alert warn">${icon('refresh')}<div>服务可能正在重启：${esc(err.message)}<br>页面会继续尝试查询升级状态。</div></div>`;
    return null;
  }
}

function uploadUpdatePackage(file, panel, currentVersion) {
  const stateEl = panel.querySelector('#update-upload-state');
  const btn = panel.querySelector('[data-act="upload-update"]');
  const fd = new FormData(); fd.append('file', file, file.name);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/system/updates/upload');
  xhr.setRequestHeader('X-CSRF', state.csrf || '');
  btn.disabled = true; stateEl.textContent = '上传中 0%';
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) stateEl.textContent = `上传中 ${Math.round(e.loaded / e.total * 100)}%`; };
  xhr.onload = async () => {
    btn.disabled = false;
    let data = {}; try { data = JSON.parse(xhr.responseText || '{}'); } catch { /* ignore */ }
    if (xhr.status >= 200 && xhr.status < 300) {
      toast('上传与预检通过', 'ok'); await refreshUpdatePanel(panel, currentVersion);
    } else {
      stateEl.innerHTML = `<span class="danger-text">${esc(data.detail || data.message || `上传失败（HTTP ${xhr.status}）`)}</span>`;
    }
  };
  xhr.onerror = () => { btn.disabled = false; stateEl.innerHTML = '<span class="danger-text">网络中断，上传失败</span>'; };
  xhr.send(fd);
}

function openApplyUpdate(jobId, mode = 'helper') {
  const restart = mode === 'restart';
  const m = modal({
    title: restart ? '准备重启生效' : '执行离线升级',
    body: restart
      ? `<div class="alert info">${icon('refresh')}<div>
          <strong>确认后只会准备重启，不会立即替换代码。</strong><br>
          关闭当前平台窗口并重新启动，启动时会先备份旧程序，再替换代码并加载新版本；<code>data/</code> 与 <code>config.json</code> 保留。
        </div></div>
        <div class="form-row" style="margin-top:12px"><label class="label">重新输入管理员密码</label>
          <input class="input" type="password" id="apply-password" autocomplete="current-password"></div>`
      : `<div class="alert warn">${icon('alert')}<div>升级会停止服务、备份数据、迁移数据库并替换代码。请确认当前没有诊断任务运行，并已保留升级包。</div></div>
        <div class="form-row" style="margin-top:12px"><label class="label">重新输入管理员密码</label>
          <input class="input" type="password" id="apply-password" autocomplete="current-password"></div>`,
    footer: `<button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="apply">${restart ? '准备重启' : '确认升级'}</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) { m.close(); return; }
    const btn = e.target.closest('[data-act="apply"]'); if (!btn) return;
    const password = m.body.querySelector('#apply-password').value;
    if (!password) { toast('请输入管理员密码', 'error'); return; }
    setBusy(btn, true, restart ? '准备中' : '提交中');
    try {
      const data = await api(`/api/system/updates/${encodeURIComponent(jobId)}/apply`, { method: 'POST', body: { password } });
      const update = data.update || {};
      const waiting = update.phase === 'waiting_restart';
      m.close();
      toast(waiting ? '已准备，重启平台后自动生效' : '已提交升级，正在等待 root 助手',
            waiting ? 'warn' : 'ok', waiting ? 10000 : 4000);
      updatePollTimer = null;
      const panel = document.querySelector('#update-panel');
      await refreshUpdatePanel(panel, state.publicConfig.version || '');
      if (!waiting) startUpdatePoll(jobId, panel, state.publicConfig.version || '');
    } catch (err) { toast(err.message, 'error', 6000); setBusy(btn, false); }
  });
}

function openRollbackUpdate(jobId) {
  const m = modal({
    title: '回滚到升级前快照',
    body: `<div class="alert danger">${icon('alert')}<div>回滚会恢复旧代码与升级前数据，当前数据移入 rollback-safety。此操作需要 root 助手执行，期间服务会短暂中断。</div></div>
      <div class="form-row" style="margin-top:12px"><label class="label">重新输入管理员密码</label>
        <input class="input" type="password" id="rollback-password" autocomplete="current-password"></div>`,
    footer: `<button class="btn" data-act="cancel">取消</button><button class="btn btn-danger" data-act="rollback">确认回滚</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) { m.close(); return; }
    const btn = e.target.closest('[data-act="rollback"]'); if (!btn) return;
    const password = m.body.querySelector('#rollback-password').value;
    if (!password) { toast('请输入管理员密码', 'error'); return; }
    setBusy(btn, true, '提交中');
    try {
      const data = await api('/api/system/updates/rollback', { method: 'POST', body: { password, job_id: jobId } });
      m.close(); toast('已提交回滚', 'ok');
      updatePollTimer = null;
      const panel = document.querySelector('#update-panel');
      await refreshUpdatePanel(panel, state.publicConfig.version || '');
      startUpdatePoll(data.update.id, panel, state.publicConfig.version || '');
    } catch (err) { toast(err.message, 'error', 6000); setBusy(btn, false); }
  });
}

async function verifyUpdate(jobId, panel, currentVersion) {
  try {
    await api(`/api/system/updates/${encodeURIComponent(jobId)}/verify`, { method: 'POST', body: {} });
    toast('预检通过', 'ok'); await refreshUpdatePanel(panel, currentVersion);
  } catch (err) { toast(err.message, 'error', 6000); }
}

let updatePollTimer = null;
function startUpdatePoll(jobId, panel, currentVersion) {
  if (updatePollTimer) return;
  const tick = async () => {
    try {
      const data = await api(`/api/system/updates/${encodeURIComponent(jobId)}`);
      const line = data.update;
      if (line.phase === 'succeeded' || line.phase === 'failed'
          || line.phase === 'rolled_back' || line.phase === 'waiting_restart') {
        clearInterval(updatePollTimer); updatePollTimer = null;
        await refreshUpdatePanel(panel, currentVersion);
        if (line.phase === 'succeeded') toast('升级成功，请刷新页面使用新版本', 'ok', 8000);
        if (line.phase === 'waiting_restart') toast('已准备，重启平台后自动生效', 'warn', 10000);
        return;
      }
      if (panel.querySelector('#update-card')) {
        const msg = panel.querySelector('#update-card .alert.info div');
        if (msg && line.message) msg.innerHTML = `<strong>${esc(line.message)}</strong>`;
        const bar = panel.querySelector('#update-card .upload-progress div');
        if (bar) bar.style.width = `${Number(line.progress || 0)}%`;
      }
    } catch { /* 服务重启期间继续轮询 */ }
  };
  updatePollTimer = setInterval(tick, 2000);
  tick();
  state.cleanup = () => { if (updatePollTimer) { clearInterval(updatePollTimer); updatePollTimer = null; } };
}

// ---------------------------- 运行配置与网页重启 ----------------------------
let storagePollTimer = null;

function runtimeFieldHtml(item) {
  const disabled = item.env_override ? 'disabled' : '';
  const effect = item.restart_required === false ? '保存后立即生效' : '重启后生效';
  if (item.type === 'bool') {
    return `<div class="form-row"><label class="label" for="rt-${esc(item.key)}">${esc(item.label)}
        ${item.env_override ? `<span class="badge warn" title="该值由环境变量覆盖，网页保存不会生效">${esc(item.env_override)}</span>` : ''}</label>
      <label class="check"><input type="checkbox" id="rt-${esc(item.key)}" data-runtime-key="${esc(item.key)}"
        data-runtime-type="bool" ${item.value ? 'checked' : ''} ${disabled}> 开启</label>
      <div class="help">默认 ${item.value ? '开启' : '关闭'} · ${effect}${item.env_override ? ' · 环境变量优先' : ''}</div></div>`;
  }
  if (item.type === 'enum') {
    const options = (item.choices || []).map((value) => {
      const label = (item.choice_labels || {})[value] || value;
      return `<option value="${esc(value)}" ${String(item.value) === String(value) ? 'selected' : ''}>${esc(label)}</option>`;
    }).join('');
    return `<div class="form-row"><label class="label" for="rt-${esc(item.key)}">${esc(item.label)}
        ${item.env_override ? `<span class="badge warn" title="该值由环境变量覆盖，网页保存不会生效">${esc(item.env_override)}</span>` : ''}</label>
      <select class="input" id="rt-${esc(item.key)}" data-runtime-key="${esc(item.key)}"
        data-runtime-type="enum" ${disabled}>${options}</select>
      <div class="help">默认 ${esc((item.choice_labels || {})[item.default] || item.default)} · ${effect}${item.env_override ? ' · 环境变量优先' : ''}</div></div>`;
  }
  if (item.type === 'str') {
    return `<div class="form-row"><label class="label" for="rt-${esc(item.key)}">${esc(item.label)}
        ${item.env_override ? `<span class="badge warn" title="该值由环境变量覆盖，网页保存不会生效">${esc(item.env_override)}</span>` : ''}</label>
      <input class="input mono" id="rt-${esc(item.key)}" data-runtime-key="${esc(item.key)}"
        data-runtime-type="str" type="text" value="${esc(item.value ?? '')}" ${disabled}>
      <div class="help">默认 ${esc(item.default || '（空）')} · ${effect}${item.env_override ? ' · 环境变量优先' : ''}</div></div>`;
  }
  const step = item.type === 'float' ? '0.1' : '1';
  return `<div class="form-row"><label class="label" for="rt-${esc(item.key)}">${esc(item.label)}
      ${item.env_override ? `<span class="badge warn" title="该值由环境变量覆盖，网页保存不会生效">${esc(item.env_override)}</span>` : ''}</label>
    <input class="input mono" id="rt-${esc(item.key)}" data-runtime-key="${esc(item.key)}"
      data-runtime-type="${esc(item.type)}" type="number" step="${step}"
      min="${item.min ?? ''}" max="${item.max ?? ''}" value="${esc(item.value)}" ${disabled}>
    <div class="help">默认 ${esc(item.default)} · ${effect}${item.env_override ? ' · 环境变量优先' : ''}</div></div>`;
}

async function renderRuntimeConfigPanel(panel) {
  if (!panel) return;
  let data;
  try { data = await api('/api/system/runtime-config'); }
  catch (err) {
    panel.innerHTML = `<div class="card"><div class="card-head"><h2>运行参数</h2></div>
      <div class="card-body"><div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div></div></div>`;
    return;
  }
  const groups = [];
  for (const item of data.items || []) {
    let group = groups.find((g) => g.name === item.group);
    if (!group) { group = { name: item.group, items: [] }; groups.push(group); }
    group.items.push(item);
  }
  panel.innerHTML = `<div class="card" id="runtime-card">
    <div class="card-head"><div><h2>并发、预算与容量参数</h2>
      <span class="sub">仅管理员可修改；保存写入 config.json，容量与并发参数重启后重新装配生效</span></div>
      <div class="inline"><button class="btn" id="rt-save">${icon('save')}保存配置</button>
        <button class="btn btn-primary" id="rt-save-restart">${icon('refresh')}保存并重启</button></div></div>
    <div class="card-body">
      <div class="alert info">${icon('info')}<div>配置文件：<span class="mono">${esc(data.config_file)}</span>。保存前会备份为 <span class="mono">config.json.bak</span>。
        环境变量覆盖的字段在页面禁用；网页重启只在无运行任务时允许，重启期间页面会短暂断线并自动恢复。</div></div>
      ${groups.map((g) => `<div style="margin-top:14px"><h3 style="font-size:13px;margin:0 0 10px">${esc(g.name)}</h3>
        <div class="form-grid">${g.items.map(runtimeFieldHtml).join('')}</div></div>`).join('')}
      <div id="restart-status" style="margin-top:10px"></div>
    </div></div>`;

  const collect = () => {
    const values = {};
    panel.querySelectorAll('[data-runtime-key]').forEach((input) => {
      if (input.disabled) return;
      values[input.dataset.runtimeKey] = input.dataset.runtimeType === 'bool'
        ? Boolean(input.checked)
        : input.dataset.runtimeType === 'float'
          ? Number(input.value)
          : input.dataset.runtimeType === 'int'
            ? Number.parseInt(input.value, 10)
            : input.value;
    });
    return values;
  };
  const save = async (btn) => {
    setBusy(btn, true, '保存中…');
    try {
      const res = await api('/api/system/runtime-config', { method: 'PUT', body: { values: collect() } });
      toast('配置已保存', 'ok');
      const needsRestart = (res.saved || []).some((x) => x.restart_required);
      panel.querySelector('#restart-status').innerHTML = needsRestart
        ? `<div class="alert warn">${icon('alert')}<div>已保存 ${(res.saved || []).length} 项；容量/并发参数需执行“重启服务”后生效，账号开关已即时生效。</div></div>`
        : `<div class="alert info">${icon('check')}<div>已保存 ${(res.saved || []).length} 项，账号与注册开关已即时生效，无需重启。</div></div>`;
      return true;
    } catch (err) { toast(err.message, 'error', 6000); return false; }
    finally { setBusy(btn, false); }
  };
  panel.querySelector('#rt-save').addEventListener('click', (e) => save(e.currentTarget));
  panel.querySelector('#rt-save-restart').addEventListener('click', async (e) => {
    const ok = await save(e.currentTarget);
    if (ok) await restartWebService(panel);
  });
  const restartBtn = document.createElement('button');
  restartBtn.className = 'btn btn-danger';
  restartBtn.innerHTML = `${icon('refresh')}仅重启服务`;
  restartBtn.addEventListener('click', () => restartWebService(panel));
  panel.querySelector('#runtime-card .card-head .inline').appendChild(restartBtn);
}

async function restartWebService(panel) {
  if (!(await confirmDialog('重启会重新加载 config.json，并短暂停止所有网页访问。请确保当前没有诊断、工坊、迁移或升级任务。确定重启？',
    { title: '重启平台服务', okText: '立即重启', danger: true }))) return;
  const status = panel.querySelector('#restart-status');
  try {
    await api('/api/system/restart', { method: 'POST' });
  } catch (err) {
    const blockers = err.detail?.blockers || [];
    status.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}
      ${blockers.length ? `<ul style="margin:6px 0 0;padding-left:18px">${blockers.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div></div>`;
    return;
  }
  const overlay = modal({ title: '服务重启中', body: `<div class="alert info">${icon('activity')}<div>
    <strong>正在停止旧服务并重新加载配置…</strong><div class="tiny muted" style="margin-top:5px">页面会自动检测恢复状态，请勿关闭浏览器。</div>
    <div class="upload-progress" style="margin-top:12px"><div id="restart-progress" style="width:5%"></div></div></div></div>`, footer: '' });
  let attempts = 0;
  const timer = setInterval(async () => {
    attempts += 1;
    const pct = Math.min(92, 5 + attempts * 6);
    const bar = overlay.body.querySelector('#restart-progress');
    if (bar) bar.style.width = `${pct}%`;
    if (attempts < 6) return;
    try {
      const health = await api('/api/health', { csrf: false });
      if (health && (health.ok !== false)) {
        clearInterval(timer);
        const done = overlay.body.querySelector('#restart-progress');
        if (done) done.style.width = '100%';
        setTimeout(() => window.location.reload(), 800);
      }
    } catch { /* 服务尚未恢复，继续等待 */ }
    if (attempts > 40) {
      clearInterval(timer);
      overlay.body.innerHTML = `<div class="alert danger">${icon('alert')}<div>重启后 40 秒仍未恢复。请检查服务日志或进程状态后手动重启。</div></div>`;
    }
  }, 1000);
}

// ---------------------------- 任务空间统计与释放 ----------------------------
const INPUT_STATE_LABELS = {
  available: '原始输入可用', releasing: '释放中',
  released: '原始输入已释放', release_interrupted: '释放中断，可重试',
  excluded_by_migration: '未随迁移导出',
};
const RELEASE_MODE_LABELS = {
  raw_inputs: '原始输入', extracted: '解压文件', both: '原始输入 + 解压文件',
};

const BACKUP_KIND_LABELS = { upgrade: '升级快照', import: '迁移恢复前备份', manual: '手工/其它备份' };

async function renderBackupPanel(panel) {
  if (!panel) return;
  let data;
  try { data = await api('/api/backups'); }
  catch (err) {
    panel.innerHTML = `<div class="card"><div class="card-head"><h2>历史备份</h2></div>
      <div class="card-body"><div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div></div></div>`;
    return;
  }
  const items = data.items || [];
  const summary = data.summary || {};
  const rows = items.map((item, index) => {
    const version = item.old_version || item.new_version
      ? `${esc(item.old_version || '?')} → ${esc(item.new_version || '?')}` : '—';
    const refs = (item.referenced_by || []).length
      ? `<div class="tiny muted">升级记录 ${esc((item.referenced_by || []).join('、'))}</div>` : '';
    return `<tr>
      <td><div style="font-weight:600;word-break:break-all">${esc(item.name)}</div>
        <div class="mono tiny muted">${esc(item.path)}</div>${refs}</div></td>
      <td><span class="chip">${esc(BACKUP_KIND_LABELS[item.kind] || item.kind || '其它')}</span>
        <div class="tiny muted">${version}${item.has_state_backup ? '' : ' · 不含状态快照'}</div></td>
      <td class="mono nowrap">${fmtDate(item.created_at)}</td>
      <td class="mono">${fmtSize(item.size_bytes)}</td>
      <td class="mono">${Number(item.file_count || 0).toLocaleString()} 个文件</td>
      <td><button class="btn btn-sm btn-danger" data-backup-delete="${esc(item.name)}" ${item.in_use ? 'disabled title="正在被升级任务使用"' : ''}>${icon('trash')}删除</button>
        ${item.in_use ? '<div class="tiny warn">升级任务使用中</div>' : ''}</td>
    </tr>`;
  }).join('');
  panel.innerHTML = `<div class="card">
    <div class="card-head"><div><h2>历史备份</h2>
      <span class="sub">显示 STATE_DIR/backups 下的升级快照、迁移恢复前备份和手工备份；删除不可恢复</span></div>
      <button class="btn" id="backup-refresh">${icon('refresh')}重新扫描</button></div>
    <div class="card-body">
      <div class="alert info">${icon('shield')}<div>备份根目录：<span class="mono">${esc(data.root)}</span>。
        正在被升级任务引用的备份不能删除；普通历史快照可删除以释放空间。</div></div>
      <div class="grid grid-3" style="margin:12px 0">
        <div class="card stat"><div class="stat-icon info">${icon('layers')}</div><div><div class="stat-val">${Number(summary.count || 0)}</div><div class="stat-label">备份记录</div></div></div>
        <div class="card stat"><div class="stat-icon warn">${icon('database')}</div><div><div class="stat-val">${fmtSize(summary.total_bytes)}</div><div class="stat-label">占用空间</div></div></div>
        <div class="card stat"><div class="stat-icon ok">${icon('check')}</div><div><div class="stat-val">${Number(summary.deletable_count || 0)}</div><div class="stat-label">可删除</div></div></div>
      </div>
      <div class="table-wrap"><table class="data responsive"><thead><tr>
        <th>备份名称</th><th>类型 / 版本</th><th>创建时间</th><th>大小</th><th>文件数</th><th>操作</th>
      </tr></thead><tbody>${rows || '<tr><td colspan="6"><div class="empty">暂无历史备份</div></td></tr>'}</tbody></table></div>
    </div></div>`;

  panel.querySelector('#backup-refresh').addEventListener('click', () => renderBackupPanel(panel));
  panel.querySelectorAll('[data-backup-delete]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.backupDelete;
      const item = items.find((x) => x.name === name);
      const warning = item?.referenced_by?.length
        ? '该备份被历史升级记录引用，删除后对应回滚入口将失效。'
        : '删除后不可恢复。';
      if (!await confirmDialog(`删除备份「${name}」？${warning}`, { title: '删除备份', okText: '确认删除', danger: true })) return;
      try {
        await api(`/api/backups/${encodeURIComponent(name)}`, { method: 'DELETE' });
        toast('备份已删除', 'ok'); renderBackupPanel(panel);
      } catch (err) { toast(err.message, 'error', 6000); }
    });
  });
}

async function renderStoragePanel(panel) {
  if (!panel) return;
  if (storagePollTimer) { clearInterval(storagePollTimer); storagePollTimer = null; }
  let data;
  let skills = [];
  try {
    const skillData = await api('/api/skills');
    skills = (skillData.items || []).filter((s) => ['ready', 'degraded'].includes(s.status));
  } catch { /* 技能列表失败不阻塞空间统计 */ }
  try { data = await api('/api/storage/overview'); }
  catch (err) {
    panel.innerHTML = `<div class="card"><div class="card-head"><h2>任务空间</h2></div>
      <div class="card-body"><div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div></div></div>`;
    return;
  }
  const summary = data.summary || {};
  const items = data.items || [];
  const disk = summary.disk || {};
  const rows = items.map((item) => `<tr>
    <td class="storage-check-cell"><input type="checkbox" class="storage-check" data-task="${esc(item.id)}"
      ${(item.can_release || item.can_release_extracted) ? '' : 'disabled'}></td>
    <td data-label="任务"><div style="font-weight:600">${esc(item.title || item.id)}</div>
      <div class="mono tiny muted">${esc(item.id)} · ${fmtDate(item.created_at)}</div></td>
    <td data-label="状态 / 输入">${statusBadge(item.status)}<div class="tiny muted">${esc(INPUT_STATE_LABELS[item.inputs_state] || item.inputs_state)}</div></td>
    <td data-label="原始输入" class="mono">${fmtSize(item.raw_input_bytes)}
      ${item.shared_input_paths ? `<div class="tiny"><span class="badge warn">${item.shared_input_paths} 个共享</span></div>` : ''}</td>
    <td data-label="解压文件" class="mono ${item.extracted_bytes ? '' : 'muted'}">${fmtSize(item.extracted_bytes)}
      ${item.extracted_dirs ? `<div class="tiny muted">${item.extracted_dirs} 个目录</div>` : ''}</td>
    <td data-label="其它产物" class="mono muted">${fmtSize(item.retained_bytes)}</td>
    <td data-label="合计" class="mono">${fmtSize(item.total_bytes)}</td>
    <td data-label="操作" class="${(!item.can_release && !item.can_release_extracted) ? 'storage-no-action' : ''}"><div class="inline" style="gap:4px;flex-wrap:wrap">
      ${item.can_release_extracted ? `<button class="btn btn-sm" data-release="${esc(item.id)}" data-release-mode="extracted">${icon('trash')}释放解压</button>` : ''}
      ${item.can_release ? `<button class="btn btn-sm btn-danger" data-release="${esc(item.id)}" data-release-mode="raw_inputs">${icon('trash')}释放原始</button>` : ''}
      ${(!item.can_release && !item.can_release_extracted) ? '<span class="muted">—</span>' : ''}
    </div></td>
  </tr>`).join('');
  const storageList = items.length ? `
      <div class="storage-list-head"><div><h3>任务清单</h3><span class="sub">共 ${items.length} 个任务，按创建时间倒序</span></div></div>
      <div class="table-wrap"><table class="data storage-table"><thead><tr>
        <th><input type="checkbox" id="storage-check-all" aria-label="全选可释放任务"></th>
        <th>任务</th><th>状态 / 输入</th><th>原始输入</th><th>解压文件</th><th>其它产物</th><th>合计</th><th>操作</th>
      </tr></thead><tbody>${rows}</tbody></table></div>` : `
      <div class="storage-empty">${icon('layers')}<h3>暂无任务空间记录</h3>
        <p>新建诊断或技能工坊任务后，这里会显示原始输入、解压文件和可释放空间。</p>
        <button class="btn" data-storage-refresh>${icon('refresh')}重新统计</button></div>`;
  panel.innerHTML = `<div class="card">
    <div class="card-head storage-head">
      <div class="storage-head-copy"><h2>任务空间统计与释放</h2>
        <span class="sub">可单独释放解压文件；报告、证据、案例和数据库记录始终保留</span></div>
      <div class="storage-toolbar">
        <select class="select storage-mode-select" id="storage-release-mode" title="批量释放内容">
          <option value="extracted" selected>解压文件（推荐）</option>
          <option value="raw_inputs">原始输入</option>
          <option value="both">原始输入 + 解压文件</option>
        </select>
        <button class="btn btn-danger" id="storage-release-selected">${icon('trash')}释放选中</button>
        <button class="btn" id="storage-refresh">${icon('refresh')}重新统计</button>
      </div></div>
    <div class="card-body">
      <div class="storage-skill-bar">
        <div class="storage-skill-copy">
          <div class="storage-skill-icon">${icon('play')}</div>
          <div><strong>按技能诊断</strong><p>选择可用技能后，直接进入只执行该技能的诊断任务</p></div>
        </div>
        <div class="storage-skill-actions">
          <select class="select" id="storage-skill" ${skills.length ? '' : 'disabled'}>
            <option value="">${skills.length ? '选择技能' : '暂无可用技能'}</option>
            ${skills.map((s) => `<option value="${esc(s.name)}">${esc(s.title || s.name)} · v${esc(s.version)}</option>`).join('')}
          </select>
          <button class="btn btn-primary" id="storage-skill-diagnose" ${skills.length ? '' : 'disabled'}>${icon('play')}按技能诊断</button>
        </div>
      </div>
      <div class="storage-stats">
        <div class="storage-stat"><div class="stat-icon">${icon('file')}</div><div><div class="stat-val">${fmtSize(summary.raw_input_bytes)}</div><div class="stat-label">原始输入</div></div></div>
        <div class="storage-stat"><div class="stat-icon warn">${icon('layers')}</div><div><div class="stat-val">${fmtSize(summary.extracted_bytes)}</div><div class="stat-label">解压文件（可释放）</div></div></div>
        <div class="storage-stat"><div class="stat-icon ok">${icon('shield')}</div><div><div class="stat-val">${fmtSize(summary.retained_bytes)}</div><div class="stat-label">报告/证据等保留</div></div></div>
        <div class="storage-stat"><div class="stat-icon info">${icon('activity')}</div><div><div class="stat-val">${fmtSize(summary.deletable_bytes)}</div><div class="stat-label">当前可释放</div></div></div>
      </div>
      <div class="storage-notice">
        <div class="storage-notice-icon">${icon('shield')}</div>
        <div class="storage-notice-text"><strong>释放规则</strong>
          <p><b>解压文件</b>只删除任务目录中的 <span class="mono">skills/*/unpacked</span>，报告、证据和案例不受影响；重跑会从原始日志重新解压。</p>
          <p><b>原始输入</b>会删除上传日志；共享文件只有最后一个引用任务释放时才删除。释放原始输入后该任务不能再重跑。</p>
        </div>
        <span class="storage-disk">磁盘剩余 ${fmtSize(disk.free_bytes)}</span>
      </div>
      <div id="storage-status" class="small muted" style="margin:8px 0"></div>
      ${storageList}
    </div></div>`;

  const checkAll = panel.querySelector('#storage-check-all');
  checkAll?.addEventListener('change', () => {
    panel.querySelectorAll('.storage-check:not(:disabled)').forEach((c) => { c.checked = checkAll.checked; });
  });
  panel.querySelectorAll('#storage-refresh, [data-storage-refresh]').forEach((btn) => btn.addEventListener('click', () => renderStoragePanel(panel)));
  panel.querySelector('#storage-skill-diagnose')?.addEventListener('click', () => {
    const name = panel.querySelector('#storage-skill')?.value || '';
    if (!name) { toast('请先选择一个可用技能', 'warn'); return; }
    try { sessionStorage.setItem('bbu.diagnose_skill', name); } catch { /* ignore */ }
    location.hash = '#/tasks/new';
  });
  panel.querySelector('#storage-release-selected').addEventListener('click', () => {
    const ids = [...panel.querySelectorAll('.storage-check:checked')].map((c) => c.dataset.task);
    releaseTasks(panel, ids, panel.querySelector('#storage-release-mode').value);
  });
  panel.querySelectorAll('[data-release]').forEach((btn) => {
    btn.addEventListener('click', () => releaseTasks(panel, [btn.dataset.release], btn.dataset.releaseMode));
  });
  const activeJobs = items.map((x) => x.last_storage_job).filter((j) => j && ['queued', 'running'].includes(j.status));
  if (activeJobs.length) pollStorageJobs(panel, activeJobs.map((j) => j.id));
}

async function releaseTasks(panel, ids, mode = 'extracted') {
  if (!ids.length) { toast('请先选择可释放的任务', 'warn'); return; }
  const modeLabel = RELEASE_MODE_LABELS[mode] || '解压文件';
  const detail = mode === 'raw_inputs'
    ? '释放后不能重跑，历史报告和证据仍保留；共享文件会保留到最后一个引用释放。'
    : mode === 'both'
      ? '将同时删除解压文件和原始上传日志；报告和证据保留，但释放原始输入后不能重跑。'
      : '只删除中间产物中的解压文件；报告、证据和案例保留，重跑会重新解压。';
  const ok = await confirmDialog(`将释放 ${ids.length} 个任务的${modeLabel}。${detail} 确定继续？`,
    { title: `释放${modeLabel}`, okText: '确认释放', danger: mode !== 'extracted' });
  if (!ok) return;
  const status = panel.querySelector('#storage-status');
  if (status) status.textContent = `正在提交${modeLabel}释放任务…`;
  const jobs = [];
  for (const id of ids) {
    try {
      const res = await api(`/api/tasks/${encodeURIComponent(id)}/release`, {
        method: 'POST', body: { mode },
      });
      jobs.push(res.job.id);
    } catch (err) {
      toast(`${id}: ${err.message}`, 'error', 6000);
    }
  }
  if (jobs.length) pollStorageJobs(panel, jobs);
}


function pollStorageJobs(panel, jobIds) {
  if (storagePollTimer) clearInterval(storagePollTimer);
  const status = panel.querySelector('#storage-status');
  const tick = async () => {
    const states = [];
    for (const id of jobIds) {
      try { states.push(await api(`/api/storage/jobs/${encodeURIComponent(id)}`)); }
      catch { /* 单个查询失败不影响其它任务 */ }
    }
    const running = states.filter((j) => ['queued', 'running'].includes(j.status)).length;
    const done = states.filter((j) => j.status === 'done').length;
    const failed = states.filter((j) => ['failed', 'interrupted'].includes(j.status)).length;
    const freed = states.reduce((n, j) => n + Number(j.freed_bytes || 0), 0);
    if (status) status.innerHTML = `<span>${icon('activity')} 后台释放：完成 ${done} / 进行中 ${running} / 失败 ${failed}，累计释放 ${fmtSize(freed)}</span>`;
    if (!running) {
      clearInterval(storagePollTimer); storagePollTimer = null;
      toast(`空间释放结束：完成 ${done}，失败 ${failed}，释放 ${fmtSize(freed)}`, failed ? 'warn' : 'ok', 6000);
      setTimeout(() => renderStoragePanel(panel), 300);
    }
  };
  tick();
  storagePollTimer = setInterval(tick, 900);
}


function mcpEndpoint() {
  return `${location.origin}/mcp`;
}

function copyText(text, btn) {
  const done = () => { toast('已复制', 'ok'); if (btn) setBusy(btn, false); };
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallback());
  } else fallback();
  function fallback() {
    const box = document.createElement('textarea');
    box.value = text; box.style.position = 'fixed'; box.style.opacity = '0';
    document.body.appendChild(box); box.select();
    try { document.execCommand('copy'); done(); }
    catch { toast('复制失败，请手动选择文本', 'error'); if (btn) setBusy(btn, false); }
    box.remove();
  }
}

async function openMCPTokenModal(panel, current) {
  const mode = current?.mode || 'read';
  const m = modal({
    title: '生成 MCP Token',
    body: `<div class="alert warn">${icon('alert')}<div>
      Token 只在生成时显示一次，平台只保存 SHA-256 哈希。生成新 Token 会立即使旧 Token 失效。
    </div></div>
    <div class="field"><label>访问模式</label>
      <select id="mcp-token-mode">
        <option value="read" ${mode === 'read' ? 'selected' : ''}>read · 只读（推荐）</option>
        <option value="write" ${mode === 'write' ? 'selected' : ''}>write · 允许执行当前账号可用的白名单写工具</option>
      </select>
      <div class="hint">read 仅暴露当前账号可用的查询类工具；write 还允许当前账号权限内的删除备份、释放任务空间、修改通知偏好等低风险写操作。升级/回滚、用户和模型变更永不通过 MCP 执行。</div>
    </div>
    <div class="field"><label>客户端连接地址</label>
      <input readonly value="${esc(mcpEndpoint())}"></div>`,
    footer: `<button class="btn btn-ghost" data-act="close-modal">取消</button>
      <button class="btn btn-primary" data-create-token>${icon('key')}生成 Token</button>`,
    wide: true,
  });
  const btn = m.foot.querySelector('[data-create-token]');
  btn.addEventListener('click', async () => {
    const selected = m.body.querySelector('#mcp-token-mode').value;
    setBusy(btn, true, '生成中');
    try {
      const res = await api('/api/mcp/token', { method: 'POST', body: { mode: selected } });
      const info = res.mcp || {};
      const token = info.token || '';
      const cfg = `{
  "mcpServers": {
    "bbu-faultscope": {
      "type": "http",
      "url": "${mcpEndpoint()}",
      "headers": { "Authorization": "Bearer ${token}" }
    }
  }
}`;
      m.body.innerHTML = `<div class="alert ok">${icon('check')}<div>Token 已生成（${esc(info.mode)} 模式）。请立即复制，关闭后无法再次查看。</div></div>
        <div class="field"><label>Token</label>
          <textarea id="mcp-new-token" readonly rows="3" class="mono">${esc(token)}</textarea>
          <div style="margin-top:6px"><button class="btn btn-sm" data-copy-token>${icon('copy')}复制 Token</button></div>
        </div>
        <div class="field"><label>客户端配置示例</label>
          <textarea id="mcp-client-config" readonly rows="8" class="mono">${esc(cfg)}</textarea>
          <div style="margin-top:6px"><button class="btn btn-sm" data-copy-config>${icon('copy')}复制配置</button></div>
        </div>
        <div class="alert info">${icon('info')}<div>MCP 默认只接受本机 loopback 请求；外部 AI 客户端请与平台运行在同一台机器。需要内网访问时由管理员显式开启 <code>mcp_allow_remote</code>。</div></div>`;
      btn.remove();
      m.foot.innerHTML = `<button class="btn btn-primary" data-act="close-modal">完成</button>`;
      m.body.querySelector('[data-copy-token]').addEventListener('click', (e) => copyText(token, e.currentTarget));
      m.body.querySelector('[data-copy-config]').addEventListener('click', (e) => copyText(cfg, e.currentTarget));
      m.mask.addEventListener('click', (event) => {
        if (event.target.closest('[data-act="close-modal"]')) {
          setTimeout(() => renderMCPPanel(panel), 40);
        }
      });
    } catch (err) {
      toast(err.message, 'error');
      setBusy(btn, false);
    }
  });
}

function renderMCPPanel(panel, payload) {
  const info = payload?.mcp || payload || {};
  if (payload?.error) {
    panel.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(payload.error)}</div></div>`;
    return;
  }
  const token = info.configured ? `<span class="badge ok">已配置</span>` : `<span class="badge warn">未生成</span>`;
  const names = info.tool_names || [];
  panel.innerHTML = `
    <div class="grid grid-2" style="margin-bottom:14px">
      <div class="card"><div class="card-head"><h2>MCP Server</h2>${token}</div><div class="card-body">
        <div class="kv">
          <div class="k">端点</div><div class="v mono">${esc(info.endpoint || '/mcp')}</div>
          <div class="k">传输</div><div class="v">${esc(info.transport || 'HTTP JSON-RPC')}</div>
          <div class="k">访问范围</div><div class="v">${info.local_only ? '仅本机 loopback' : '允许内网（已显式开启）'}</div>
          <div class="k">协议版本</div><div class="v mono small">${esc((info.protocol_versions || []).join(' / '))}</div>
          <div class="k">当前模式可见工具</div><div class="v">${info.tools ?? 0} 个</div>
          <div class="k">文档资源</div><div class="v">${info.resources ?? 0} 份</div>
        </div>
      </div></div>
      <div class="card"><div class="card-head"><h2>访问 Token</h2></div><div class="card-body">
        ${info.configured ? `<div class="kv">
          <div class="k">模式</div><div class="v">${esc(info.mode || '—')}</div>
          <div class="k">绑定账号</div><div class="v">${esc(info.username || '—')}</div>
          <div class="k">创建时间</div><div class="v">${esc(fmtDate(info.created_at))}</div>
          <div class="k">最近使用</div><div class="v">${info.last_used_at ? esc(fmtDate(info.last_used_at)) : '尚未使用'}</div>
        </div>` : `<div class="alert warn">${icon('alert')}<div>${esc(info.hint || '尚未生成 MCP Token，外部 AI 客户端暂时无法连接。')}</div></div>`}
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <button class="btn btn-primary" id="mcp-generate">${icon('key')}${info.configured ? '轮换 Token' : '生成 Token'}</button>
          ${info.configured ? `<button class="btn btn-danger" id="mcp-revoke">${icon('trash')}撤销 Token</button>` : ''}
        </div>
        <div class="tiny muted" style="margin-top:8px">Token 仅保存 SHA-256 哈希；轮换后旧 Token 立即失效。账号停用后 Token 同步失效。</div>
      </div></div>
    </div>
    <div class="card mcp-client-card" style="margin-bottom:14px">
      <div class="card-head"><div class="mcp-client-heading"><h2>客户端配置</h2><span class="sub">Claude Desktop / Cursor / 其他 MCP 客户端</span></div></div>
      <div class="card-body">
        <div class="mcp-client-layout">
          <div class="mcp-client-notes">
            <div class="mcp-config-note">${icon('info')}<div>推荐使用 <strong>read</strong> 模式；Token 绑定当前账号，只能看到当前账号权限内的工具与数据。</div></div>
            <ol class="mcp-steps">
              <li><span>1</span><div><strong>复制配置</strong><p>右侧 JSON 已填入当前 MCP 端点，复制后替换 Bearer 占位 Token。</p></div></li>
              <li><span>2</span><div><strong>写入客户端</strong><p>粘贴到 Claude Desktop / Cursor 的 <code>mcpServers</code> 配置中。</p></div></li>
              <li><span>3</span><div><strong>重启并验证</strong><p>重新打开客户端，确认工具列表出现 <code>bbu-faultscope</code>。</p></div></li>
            </ol>
            <div class="mcp-token-tip">${icon('key')}<span>Token 绑定当前账号；轮换后旧 Token 立即失效，需要同步更新客户端。</span></div>
          </div>
          <div class="mcp-code-panel">
            <div class="mcp-code-head"><div class="mcp-code-title">${icon('file')} mcp.json <span class="badge">JSON</span></div>
              <button class="btn btn-sm" id="mcp-copy-config">${icon('copy')}复制 JSON</button></div>
            <pre class="mcp-code-block"><code id="mcp-config-example">{
  "mcpServers": {
    "bbu-faultscope": {
      "type": "http",
      "url": "${mcpEndpoint()}",
      "headers": { "Authorization": "Bearer &lt;YOUR_TOKEN&gt;" }
    }
  }
}</code></pre>
          </div>
        </div>
      </div>
    </div>
    <div class="card"><div class="card-head"><h2>当前模式工具</h2><span class="sub">${info.mode || 'read'} 模式</span></div>
      <div class="card-body"><div class="assistant-suggestions">${names.length ? names.map((n) => `<span class="badge" style="font-family:var(--mono)">${esc(n)}</span>`).join('') : '<span class="muted">暂无</span>'}</div>
        <div class="tiny muted" style="margin-top:8px">升级/回滚、用户与模型变更不通过 MCP 暴露；写工具仍要求 <code>confirm=true</code>。</div>
      </div></div>`;
  panel.querySelector('#mcp-generate').addEventListener('click', () => openMCPTokenModal(panel, info));
  panel.querySelector('#mcp-revoke')?.addEventListener('click', async () => {
    if (!await confirmDialog('撤销后所有外部 MCP 客户端立即断开，确定继续？', { danger: true, title: '撤销 MCP Token' })) return;
    try {
      await api('/api/mcp/token', { method: 'DELETE' });
      toast('Token 已撤销', 'ok');
      renderMCPPanel(panel);
    } catch (err) { toast(err.message, 'error'); }
  });
  const cfg = panel.querySelector('#mcp-config-example');
  panel.querySelector('#mcp-copy-config').addEventListener('click', (e) => copyText(
    cfg.textContent.trim().replace('&lt;YOUR_TOKEN&gt;', '<YOUR_TOKEN>'), e.currentTarget));
}

function renderSystemView(view, info, config, updates, migrations, mcp) {
  const isAdmin = state.user?.role === 'admin';
  const tabs = isAdmin
    ? [['overview', '概览'], ['runtime', '运行参数'], ['storage', '任务空间'], ['backups', '备份'], ['migration', '迁移'], ['update', '升级'], ['mcp', 'MCP / 助手']]
    : [['overview', '概览'], ['mcp', 'MCP / 助手']];
  view.innerHTML = `
    <div class="system-tabs-bar"><div class="tabs system-tabs" role="tablist">
      ${tabs.map(([key, label], index) => `<button class="${index === 0 ? 'active' : ''}" role="tab"
        aria-selected="${index === 0 ? 'true' : 'false'}" data-system-tab="${key}">${label}</button>`).join('')}
    </div></div>
    <div class="system-panel" data-system-panel="overview">
      <div class="grid grid-4" style="margin-bottom:16px">
        <div class="card stat"><div class="stat-icon">${icon('info')}</div><div><div class="stat-val">v${esc(info.version)}</div><div class="stat-label">平台版本</div></div></div>
        <div class="card stat"><div class="stat-icon info">${icon('cpu')}</div><div><div class="stat-val">${esc(info.python)}</div><div class="stat-label">Python</div></div></div>
        <div class="card stat"><div class="stat-icon ok">${icon('clock')}</div><div><div class="stat-val">${fmtDur(info.uptime_sec)}</div><div class="stat-label">已运行</div></div></div>
        <div class="card stat"><div class="stat-icon warn">${icon('activity')}</div><div><div class="stat-val">${info.workers?.running || 0} / ${info.workers?.max_workers || '-'}</div><div class="stat-label">运行 / 并发上限</div></div></div>
      </div>
      <div class="grid grid-2">
        <div class="card"><div class="card-head"><h2>运行环境</h2></div><div class="card-body">
          <div class="kv">
            <div class="k">平台</div><div class="v">${esc(info.platform)}</div>
            <div class="k">品牌</div><div class="v">${esc(info.brand || '—')}</div>
            <div class="k">数据目录</div><div class="v mono">${esc(info.paths?.data)}</div>
            <div class="k">技能目录</div><div class="v mono">${esc(info.paths?.skills)}</div>
            <div class="k">案例目录</div><div class="v mono">${esc(info.paths?.cases)}</div>
            <div class="k">元 Skill</div><div class="v mono">${esc(info.paths?.meta_skill)}</div>
            <div class="k">示例技能</div><div class="v">${info.demo_installed ? '已安装（可在技能库停用）' : '未安装'}</div>
          </div>
        </div></div>
        <div class="card"><div class="card-head"><h2>并发与预算</h2></div><div class="card-body">
          <div class="kv">
            <div class="k">任务并发</div><div class="v">${info.workers?.max_workers ?? '—'} 个任务</div>
            <div class="k">技能扇出</div><div class="v">单任务最多 ${config.max_skill_parallel ?? info.workers?.max_skill_parallel ?? '—'} 个技能并行</div>
            <div class="k">LLM 调用上限</div><div class="v">每任务 ${config.max_llm_calls ?? '—'} 次</div>
            <div class="k">Token 上限</div><div class="v">${(config.max_total_tokens || 0).toLocaleString()}</div>
            <div class="k">任务超时</div><div class="v">${config.task_timeout_sec ?? '—'} 秒</div>
            <div class="k">技能超时</div><div class="v">${config.skill_timeout_sec ?? '—'} 秒</div>
            <div class="k">上传上限</div><div class="v">${config.max_upload_mb ?? '—'} MB</div>
          </div>
          <div class="alert info" style="margin-top:12px">${icon('info')}<div>预算由任务级 Budget 强制执行，模型调用次数/Token/墙钟任一超限即停止 AI 路径并回退确定性结果，不会无限循环。</div></div>
        </div></div>
      </div>
      <div class="card" style="margin-top:16px"><div class="card-head"><h2>公开配置</h2><span class="sub">只读视图（密钥不在此列）</span></div>
        <div class="card-body tight table-wrap"><table class="data"><thead><tr><th>键</th><th>值</th></tr></thead><tbody>
          ${Object.entries(config).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td>
            <td class="mono small">${esc(typeof v === 'object' ? JSON.stringify(v) : String(v))}</td></tr>`).join('')}
        </tbody></table></div></div>
      <div class="card" style="margin-top:16px"><div class="card-body small muted">
        <strong>离线部署：</strong>平台后端仅使用 Python 3.12 标准库，PyYAML 已 vendor 到 vendor/yaml；前端为免构建原生 JS，不依赖 npm/CDN。
        数据、技能、案例与密钥均保存在本机 data 目录。
      </div></div>
    </div>
    <div class="system-panel" data-system-panel="runtime" hidden>
      <div id="runtime-config-panel"><div class="card"><div class="card-body">${icon('refresh')} 运行配置加载中…</div></div></div>
    </div>
    <div class="system-panel" data-system-panel="storage" hidden>
      <div id="storage-panel"><div class="card"><div class="card-body">${icon('refresh')} 任务空间加载中…</div></div></div>
    </div>
    <div class="system-panel" data-system-panel="backups" hidden>
      <div id="backup-panel"><div class="card"><div class="card-body">${icon('refresh')} 备份记录加载中…</div></div></div>
    </div>
    <div class="system-panel" data-system-panel="migration" hidden><div id="migration-panel"></div></div>
    <div class="system-panel" data-system-panel="update" hidden><div id="update-panel"></div></div>
    <div class="system-panel" data-system-panel="mcp" hidden><div id="mcp-panel"></div></div>`;
  const loaded = { overview: true, runtime: false, storage: false, backups: false, migration: false, update: false, mcp: false };
  const show = (name) => {
    view.querySelectorAll('[data-system-tab]').forEach((tab) => {
      const active = tab.dataset.systemTab === name;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    view.querySelectorAll('[data-system-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.systemPanel !== name;
    });
    if (name === 'runtime' && !loaded.runtime) {
      loaded.runtime = true;
      renderRuntimeConfigPanel(view.querySelector('#runtime-config-panel'));
    } else if (name === 'storage' && !loaded.storage) {
      loaded.storage = true;
      renderStoragePanel(view.querySelector('#storage-panel'));
    } else if (name === 'backups' && !loaded.backups) {
      loaded.backups = true;
      renderBackupPanel(view.querySelector('#backup-panel'));
    } else if (name === 'migration' && !loaded.migration) {
      loaded.migration = true;
      renderMigrationPanel(view.querySelector('#migration-panel'), migrations || { items: [] });
    } else if (name === 'update' && !loaded.update) {
      loaded.update = true;
      renderUpdatePanel(view.querySelector('#update-panel'), updates || { items: [] }, info.version);
    } else if (name === 'mcp' && !loaded.mcp) {
      loaded.mcp = true;
      renderMCPPanel(view.querySelector('#mcp-panel'), mcp || {});
    }
  };
  view.querySelectorAll('[data-system-tab]').forEach((tab) => {
    tab.addEventListener('click', () => show(tab.dataset.systemTab));
  });
  show('overview');
  state.cleanup = () => {
    if (updatePollTimer) { clearInterval(updatePollTimer); updatePollTimer = null; }
    if (migrationPollTimer) { clearInterval(migrationPollTimer); migrationPollTimer = null; }
    if (storagePollTimer) { clearInterval(storagePollTimer); storagePollTimer = null; }
  };
}
