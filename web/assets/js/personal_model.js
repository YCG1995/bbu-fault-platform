// 个人 MaaS API Key：成员自助配置，未配置时回退管理员默认模型。
import {
  api, confirmDialog, esc, icon, modal, setBusy, toast,
} from './core.js';

function statusText(data) {
  const personal = data.status?.personal || {};
  const fallback = data.status?.default || {};
  if (personal.configured && personal.available) {
    return { tone: 'ok', label: '个人模型可用', text: `诊断将优先使用 ${personal.model || data.model}` };
  }
  if (personal.configured && !personal.available && fallback.available) {
    return { tone: 'warn', label: '个人模型暂不可用', text: '当前自动回退管理员默认模型；建议检查或更新个人 Key' };
  }
  if (fallback.available) {
    return { tone: 'info', label: '未配置个人 Key', text: '当前使用管理员默认模型；配置后可优先使用个人额度' };
  }
  return { tone: 'warn', label: '暂无可用模型', text: '管理员默认模型不可用，请配置个人 Key 后重试' };
}

let personalModalState = 'idle';

export async function openPersonalModelModal(onDone) {
  // 异步拉取期间如果用户连续点击，不能并发创建多个相同弹窗；否则关闭时
  // 一次只移除最上层，用户会感觉“点很多下才关掉”。
  if (personalModalState !== 'idle') {
    const existing = document.querySelector('.modal-mask[data-modal="personal-model"]');
    if (existing) existing.querySelector('[data-act="close"]')?.focus();
    return;
  }
  personalModalState = 'loading';
  let data;
  try {
    data = await api('/api/profile/model-key');
  } catch (err) {
    personalModalState = 'idle';
    toast(err.message, 'error');
    return;
  }
  const status = statusText(data);
  const configured = !!data.configured;
  const models = data.models || [];
  const options = models.map((m) => `<option value="${esc(m)}" ${m === data.model ? 'selected' : ''}>${esc(m)}</option>`).join('');
  const notice = data.status?.notice;
  const m = modal({
    title: '个人模型配置（公司内网 MaaS）',
    wide: true,
    body: `
      ${notice ? `<div class="alert danger">${icon('alert')}<div>
        <strong>${esc(notice.title || '模型异常')}</strong><br>${esc(notice.message || '')}
        ${notice.url ? `<div style="margin-top:6px"><a href="${esc(notice.url)}" target="_blank" rel="noopener">打开个人 API Key 页面 →</a></div>` : ''}
      </div></div>` : `<div class="alert ${status.tone === 'ok' ? 'info' : 'warn'}">
        ${icon(status.tone === 'ok' ? 'check' : 'alert')}
        <div><strong>${esc(status.label)}</strong><br>${esc(status.text)}</div>
      </div>`}
      <div class="form-grid" style="margin-top:14px">
        <div class="form-row"><label class="label">MaaS 接入地址</label>
          <input class="input mono" value="${esc(data.base_url || '')}" disabled></div>
        <div class="form-row"><label class="label">模型</label>
          <select class="input" name="model">${options}</select>
          <div class="help">仅列出管理员预置的公司内网模型；如需新增，请联系管理员。</div></div>
        <div class="form-row"><label class="label">个人 API Key</label>
          <input class="input mono" name="api_key" type="password" autocomplete="off"
            placeholder="${configured ? `已保存 ${esc(data.api_key_masked || '****')}，留空表示不修改` : '粘贴个人 MaaS API Key'}">
          <div class="help">Key 加密存储，页面与审计日志均不展示明文。
            <a href="${esc(data.maas_url || '')}" target="_blank" rel="noopener">前往 MaaS 个人 Key 页面获取 →</a>
          </div></div>
      </div>
      <div class="kv" style="margin-top:12px">
        <div class="k">管理员默认模型</div>
        <div class="v">${data.status?.default?.configured
          ? `${data.status.default.count || 0} 个已启用 · ${data.status.default.available ? '当前可用' : '当前不可用'}`
          : '未配置'}</div>
        <div class="k">最近更新</div><div class="v">${esc(data.updated_at || '—')}</div>
      </div>
      <div class="footer-note" style="margin-top:12px">个人 Key 仅用于你自己的诊断与工坊调用；任务日志和报告不会记录 Key。管理员默认模型异常时，平台会提示你配置个人 Key。</div>`,
    footer: `
      ${configured ? '<button class="btn btn-danger" data-act="delete-key">清除个人 Key</button>' : ''}
      <button class="btn" data-act="close">关闭</button>
      <button class="btn" data-act="test-key">测试连接</button>
      <button class="btn btn-primary" data-act="save-key">保存</button>`,
    onClose: () => { personalModalState = 'idle'; },
  });
  m.mask.dataset.modal = 'personal-model';
  personalModalState = 'open';

  const save = async (button, silent = false) => {
    const keyInput = m.body.querySelector('[name="api_key"]');
    const model = m.body.querySelector('[name="model"]')?.value || data.model;
    const payload = { model };
    if (keyInput && keyInput.value.trim()) payload.api_key = keyInput.value.trim();
    if (!configured && !payload.api_key) { toast('请填写个人 MaaS API Key', 'warn'); return false; }
    if (!silent) setBusy(button, true, '保存中…');
    try {
      const saved = await api('/api/profile/model-key', { method: 'PUT', body: payload });
      if (!silent) {
        toast('个人模型配置已保存', 'ok');
        m.close();
        if (onDone) onDone();
      }
      return saved;
    } catch (err) {
      toast(err.message, 'error');
      if (!silent) setBusy(button, false);
      return false;
    }
  };

  // 关闭按钮用捕获阶段处理：即使按钮内部有图标/文本子节点，或点击事件
  // 继续向页面冒泡，也只触发一次并立即销毁当前弹窗。
  m.mask.addEventListener('click', (e) => {
    if (!e.target.closest('[data-act="close"]')) return;
    e.preventDefault();
    e.stopPropagation();
    m.close();
  }, true);
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="save-key"]')) {
      await save(e.target.closest('[data-act="save-key"]'));
    }
    if (e.target.closest('[data-act="test-key"]')) {
      const button = e.target.closest('[data-act="test-key"]');
      const saved = await save(button, true);
      if (!saved) return;
      setBusy(button, true, '测试中…');
      try {
        const result = await api('/api/profile/model-key/test', { method: 'POST' });
        if (result.ok) toast(`连接成功 · ${result.latency_ms || 0}ms · ${result.reply || 'OK'}`, 'ok');
        else toast(`连接失败：${result.error || '未知错误'}`, 'error', 6000);
      } catch (err) { toast(err.message, 'error'); }
      finally { setBusy(button, false); }
    }
    if (e.target.closest('[data-act="delete-key"]')) {
      if (!await confirmDialog('清除个人 Key 后将回退管理员默认模型，确定？', { danger: true, okText: '清除' })) return;
      try {
        await api('/api/profile/model-key', { method: 'DELETE' });
        toast('个人 Key 已清除', 'ok');
        m.close();
        if (onDone) onDone();
      } catch (err) { toast(err.message, 'error'); }
    }
  });
}

export function modelNoticeHtml(notice, { compact = false } = {}) {
  if (!notice) return '';
  const link = notice.url
    ? `<a href="${esc(notice.url)}" target="_blank" rel="noopener">${esc(notice.action === 'configure_personal_key' ? '去配置个人 API Key →' : '打开 Key 页面 →')}</a>`
    : '';
  if (compact) {
    return `<div class="alert ${notice.code === 'default_model_error' ? 'warn' : 'info'}" style="margin-bottom:14px">
      ${icon('alert')}<div><strong>${esc(notice.title || '模型不可用')}</strong> ${esc(notice.message || '')}
      <button class="btn btn-sm" style="margin-left:8px" data-act="model-key">配置个人 Key</button></div></div>`;
  }
  return `<div class="alert danger" style="margin-top:14px">${icon('alert')}<div>
    <strong>${esc(notice.title || '模型不可用')}</strong><br>${esc(notice.message || '')}
    ${link ? `<div style="margin-top:6px">${link}</div>` : ''}
    <div style="margin-top:8px"><button class="btn btn-sm" data-act="model-key">配置个人 API Key</button></div>
  </div></div>`;
}
