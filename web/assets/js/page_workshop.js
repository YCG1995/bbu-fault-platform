// 技能工坊：整段资料 → 语义提取 → 确定性编译 → 审查修复 → 案例回归 → 发布
import {
  api, bindActions, bindTopbar, confirmDialog, esc, fmtDate, fmtSize, icon, modal, mount, setBusy,
  state, statusBadge, toast,
} from './core.js';
import { modelNoticeHtml, openPersonalModelModal } from './personal_model.js';

let selectedId = null;
let workshopPoll = null;

export async function renderWorkshop() {
  if (workshopPoll) { clearInterval(workshopPoll); workshopPoll = null; }
  const view = mount('技能工坊', '按步骤完成资料输入、AI 命名、规则审查、案例回归与发布；长任务在后台执行并实时显示进度',
    `<button class="btn btn-primary" data-act="new-draft">${icon('plus')}粘贴资料生成技能</button>`);
  view.innerHTML = `<div class="workshop-layout">
    <aside class="card" style="align-self:start">
      <div class="card-head"><h2>草稿</h2><span class="sub" id="draft-count"></span></div>
      <div class="card-body" id="draft-list">${icon('refresh')} 加载中…</div>
    </aside>
    <section id="draft-detail"><div class="card">${icon('flask')}<div class="empty"><h3>选择一个草稿</h3>
      <p>可以直接粘贴故障描述、日志样例、判断思路、根因和案例，无需逐格填写。</p></div></div></section>
  </div>`;

  let drafts = [];
  let models = [];
  try {
    drafts = (await api('/api/workshop/drafts')).items || [];
    try { models = ((await api('/api/models')).items || []).filter((m) => m.enabled); } catch { models = []; }
  } catch (err) {
    view.querySelector('#draft-detail').innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return view;
  }
  if (!selectedId || !drafts.some((d) => d.id === selectedId)) selectedId = drafts[0]?.id || null;
  const listEl = view.querySelector('#draft-list');
  view.querySelector('#draft-count').textContent = `${drafts.length} 个`;
  listEl.innerHTML = drafts.length ? `<div class="draft-list">${drafts.map((d) => `
    <div class="draft-item ${d.id === selectedId ? 'active' : ''}" data-draft="${esc(d.id)}">
      <strong>${esc(d.title)}</strong><div class="name">${esc(d.name)}</div>
      <div class="meta">${statusBadge(d.status)}<span>${fmtDate(d.updated_at)}</span></div>
      ${d.last_error ? `<div class="tiny" style="color:#b91c1c;margin-top:3px">${esc(d.last_error.slice(0, 100))}</div>` : ''}
    </div>`).join('')}</div>` : `<div class="empty" style="padding:24px">${icon('flask')}
      <h3>还没有草稿</h3><p>粘贴一段故障资料，系统会自动识别并生成。</p>
      <button class="btn btn-primary btn-sm" data-act="new-draft">${icon('plus')}开始生成</button></div>`;
  listEl.querySelectorAll('[data-draft]').forEach((el) => {
    el.addEventListener('click', () => { selectedId = el.dataset.draft; renderWorkshop(); });
  });
  if (!selectedId) {
    const handlers = { 'new-draft': () => openCreateModal(() => renderWorkshop(), models) };
    bindActions(view, handlers); bindTopbar(handlers); return view;
  }

  const draftId = selectedId;
  let draft;
  let caseData = { items: [], next_id: 'case-01' };
  try {
    draft = await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}`);
    try { caseData = await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/cases`); }
    catch { caseData = { items: [], next_id: 'case-01' }; }
  } catch (err) {
    view.querySelector('#draft-detail').innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return view;
  }
  if (selectedId !== draftId) return view;
  const detailEl = view.querySelector('#draft-detail');
  detailEl.innerHTML = renderDetail(draft, caseData, models);
  const profileValue = () => detailEl.querySelector('#profile-yaml').value;
  const saveProfileSilently = async () => {
    await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}`, { method: 'PUT', body: { profile_yaml: profileValue() } });
  };
  const refresh = () => { if (selectedId === draftId) renderWorkshop(); };
  const selectedModel = () => detailEl.querySelector('#workshop-model')?.value || '';

  const JOB_STAGE_LABELS = {
    queued: '排队中', starting: '启动中', unpack: '盘点/解压日志', extract: 'AI 语义提取',
    review: 'AI 规则审查', repair: '自动修复', 'review-final': '最终评审',
    check: '结构校验', done: '已完成', failed: '失败', interrupted: '已中断',
  };

  function renderJobProgress(job) {
    const box = detailEl.querySelector('#job-status');
    if (!box || !job) return;
    const terminal = ['done', 'failed', 'interrupted'].includes(job.status);
    box.hidden = false;
    box.className = `alert ${job.status === 'failed' || job.status === 'interrupted' ? 'danger' : terminal ? 'ok' : 'info'}`;
    box.innerHTML = `${icon(job.status === 'failed' || job.status === 'interrupted' ? 'alert' : 'activity')}
      <div style="flex:1;min-width:0"><div class="between"><strong>${esc(job.message || JOB_STAGE_LABELS[job.stage] || '工坊任务')}</strong>
        <span class="tiny muted">${esc(JOB_STAGE_LABELS[job.stage] || job.stage)} · ${Number(job.progress || 0)}%</span></div>
        <div class="upload-progress" style="margin-top:8px"><div style="width:${Math.max(2, Number(job.progress || 0))}%"></div></div>
        ${job.error ? `<div class="tiny" style="margin-top:7px;color:#b91c1c">${esc(job.error)}</div>` : ''}
        ${job.status === 'failed' ? `<button class="btn btn-sm" style="margin-top:8px" data-act="retry-job" data-kind="${esc(job.kind)}">重试当前步骤</button>
          <button class="btn btn-sm" style="margin-top:8px" data-act="model-key">配置可用模型</button>` : ''}
      </div>`;
  }

  function beginPoll(job) {
    if (workshopPoll) { clearInterval(workshopPoll); workshopPoll = null; }
    renderJobProgress(job);
    const tick = async () => {
      try {
        const next = await api(`/api/workshop/jobs/${encodeURIComponent(job.id)}`);
        renderJobProgress(next);
        if (next.status === 'done') {
          clearInterval(workshopPoll); workshopPoll = null;
          toast(next.message || '工坊任务完成', 'ok');
          setTimeout(refresh, 250);
        } else if (['failed', 'interrupted'].includes(next.status)) {
          clearInterval(workshopPoll); workshopPoll = null;
          toast(next.error || next.message || '工坊任务失败', 'error', 7000);
          setTimeout(refresh, 400);
        }
      } catch (err) {
        clearInterval(workshopPoll); workshopPoll = null;
        toast(err.message, 'error');
      }
    };
    workshopPoll = setInterval(tick, 900);
  }

  async function startJsonJob(path, body, btn, label) {
    setBusy(btn, true, label);
    try {
      const res = await api(path + (path.includes('?') ? '&' : '?') + 'async=1',
        { method: 'POST', body });
      if (!res.job) throw new Error('后端未返回工坊任务 ID');
      beginPoll(res.job);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(btn, false);
    }
  }

  function uploadForm(path, fd, btn, label, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', path + (path.includes('?') ? '&' : '?') + 'async=1');
      xhr.withCredentials = true;
      if (state.csrf) xhr.setRequestHeader('X-CSRF', state.csrf);
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable && onProgress) onProgress(Math.round(ev.loaded / ev.total * 100), ev);
      };
      xhr.onerror = () => reject(new Error('上传网络错误'));
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300 && data.job) resolve(data.job);
        else reject(new Error(data.error || `上传失败（HTTP ${xhr.status}）`));
      };
      setBusy(btn, true, label);
      xhr.send(fd);
    });
  }

  const handlers = {
    'new-draft': () => openCreateModal(() => renderWorkshop(), models),
    'upload-logs': () => detailEl.querySelector('#log-input').click(),
    reanalyze: (btn) => startJsonJob(
      `/api/workshop/drafts/${encodeURIComponent(draftId)}/analyze`,
      { model_override: selectedModel() || null }, btn, '提交后台分析…'),
    refine: async (btn) => {
      const text = detailEl.querySelector('#refine-text').value.trim();
      if (!text) { toast('请填写补充或纠正内容', 'warn'); return; }
      await startJsonJob(
        `/api/workshop/drafts/${encodeURIComponent(draftId)}/refine`,
        { text, model_override: selectedModel() || null }, btn, '提交后台重建…');
    },
    save: async (btn) => {
      setBusy(btn, true, '保存中…');
      try { await saveProfileSilently(); toast('已保存', 'ok'); }
      catch (err) { toast(err.message, 'error'); } finally { setBusy(btn, false); }
    },
    check: async (btn) => {
      setBusy(btn, true, '校验中…');
      try {
        const res = await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/check`, { method: 'POST', body: { profile_yaml: profileValue() } });
        toast(res.ok ? '结构与质量校验通过' : '校验未通过，请查看问题', res.ok ? 'ok' : 'warn'); refresh();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    },
    build: async (btn) => {
      try { await saveProfileSilently(); }
      catch (err) { toast(err.message, 'error'); return; }
      await startJsonJob(
        `/api/workshop/drafts/${encodeURIComponent(draftId)}/build`, {}, btn, '提交后台构建…');
    },
    'verify-rules': async (btn) => {
      const ids = String(btn.dataset.ids || '').split(',').filter(Boolean);
      if (!ids.length) return;
      const ok = await confirmDialog(`确认已人工核对规则 ${ids.join('、')}？标记后仍需真实案例回归才能发布。`, { title: '确认规则', okText: '确认并重建' });
      if (!ok) return;
      setBusy(btn, true, '标记中…');
      try {
        await saveProfileSilently();
        await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/verify-rules`, { method: 'POST', body: { rule_ids: ids } });
        await startJsonJob(
          `/api/workshop/drafts/${encodeURIComponent(draftId)}/build`, {}, btn, '提交后台构建…');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    },
    publish: async (btn) => {
      const ok = await confirmDialog('该技能已通过正例和反例回归。发布后进入正式技能库并参与规划，确定发布？', { title: '发布技能', okText: '发布' });
      if (!ok) return;
      setBusy(btn, true, '发布中…');
      try {
        const res = await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/publish`, { method: 'POST' });
        toast(`已发布 ${res.skill} v${res.version}`, 'ok'); refresh();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    },
    'model-key': () => openPersonalModelModal(() => refresh()),
    'retry-job': (btn) => {
      const kind = btn.dataset.kind;
      if (kind === 'analyze') {
        handlers.reanalyze(detailEl.querySelector('[data-act="reanalyze"]') || btn);
      } else if (kind === 'build') {
        handlers.build(detailEl.querySelector('[data-act="build"]') || btn);
      } else {
        toast('该步骤包含上传文件或补充文本，请在上方重新提交', 'warn');
      }
    },
  };

  bindActions(view, handlers); bindTopbar(handlers);

  const logInput = detailEl.querySelector('#log-input');
  logInput.addEventListener('change', async () => {
    if (!logInput.files.length) return;
    const btn = detailEl.querySelector('[data-act="upload-logs"]');
    const fd = new FormData(); [...logInput.files].forEach((f) => fd.append('files', f, f.name));
    if (selectedModel()) fd.append('model_id', selectedModel());
    try {
      const job = await uploadForm(
        `/api/workshop/drafts/${encodeURIComponent(draftId)}/logs`, fd, btn, '上传中…',
        (pct) => setBusy(btn, true, `上传 ${pct}%…`));
      beginPoll(job);
    } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    logInput.value = '';
  });

  const caseForm = detailEl.querySelector('#case-form');
  caseForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const file = caseForm.querySelector('#case-file').files[0];
    if (!file) { toast('请选择案例日志包', 'warn'); return; }
    const btn = caseForm.querySelector('button[type="submit"]');
    const fd = new FormData(); fd.append('files', file, file.name);
    ['case_id', 'kind', 'description', 'root_cause', 'hit_rules', 'not_hit_rules', 'device', 'tags', 'overwrite']
      .forEach((k) => {
        const el = caseForm.elements[k];
        if (el && (el.type === 'checkbox' ? el.checked : el.value)) fd.append(k, el.type === 'checkbox' ? '1' : el.value);
      });
    try {
      const job = await uploadForm(
        `/api/workshop/drafts/${encodeURIComponent(draftId)}/case`, fd, btn, '上传案例…',
        (pct) => setBusy(btn, true, `上传 ${pct}%…`));
      beginPoll(job);
    } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    caseForm.querySelector('#case-file').value = '';
  });

  const activeJob = (draft.jobs || []).find((j) => ['queued', 'running'].includes(j.status));
  if (activeJob) beginPoll(activeJob);
  return view;
}

function issueHtml(items, type) {
  return (items || []).map((x) => `<div class="tiny" style="margin:4px 0;color:${type === 'error' ? '#b91c1c' : '#92400e'}">
    <strong>${esc(x.field || '')}</strong> ${esc(x.message || x.suggestion || '')}</div>`).join('');
}

function renderDetail(draft, caseData = {}, models = []) {
  const analysis = draft.analysis || {};
  const intake = analysis.intake || {};
  const quality = analysis.quality || draft.check?.quality || {};
  const check = draft.check || {};
  const build = draft.build || {};
  const extract = analysis.extract || {};
  const suggestions = analysis.rule_candidates?.rule_suggestions || [];
  const samples = analysis.samples || [];
  const counts = quality.counts || {};
  const cases = caseData.items || [];
  const isAdmin = state.user?.role === 'admin';
  const canPublish = build.ok && build.verified && quality.ok !== false && isAdmin && draft.status !== 'published';
  const aiLabels = { success: 'AI 提取成功', failed: 'AI 失败，已降级为确定性解析', not_configured: '未配置模型，使用确定性解析', unavailable: '模型暂不可用，已降级', not_applicable: '人工草稿' };
  const roots = intake.root_causes || [];
  const rules = intake.evidence_rules || suggestions.filter((s) => s.rule).map((s) => s.rule);
  const activeJob = (draft.jobs || []).find((j) => ['queued', 'running'].includes(j.status));
  const hasPositive = cases.some((c) => c.kind === 'positive');
  const hasNegative = cases.some((c) => c.kind === 'negative');
  const nextStep = !analysis.intake ? '等待 AI 解析资料，或补充文字后重新分析'
    : quality.ok === false ? '先处理质量审查中的错误，再继续案例回归'
    : !build.verified ? (hasPositive && hasNegative ? '案例已齐备，执行构建回归' : '上传至少一个故障正例和一个正常反例')
    : draft.status === 'published' ? '已发布' : '回归已通过，可由管理员发布';
  return `<div class="card">
    <div class="card-head">
      <div style="flex:1;min-width:0"><h2 style="display:flex;align-items:center;gap:8px">${esc(draft.title)} ${statusBadge(draft.status)}
        ${draft.skill_version ? `<span class="badge muted">v${esc(draft.skill_version)}</span>` : ''}</h2>
        <div class="mono muted tiny">${esc(draft.name)} ${draft.name_auto ? '<span class="badge muted">AI 自动命名</span>' : ''} · ${esc(draft.fault_name)} · 更新 ${fmtDate(draft.updated_at)}</div>
        <div class="tiny muted" style="margin-top:4px">技能名和中文标题由 AI 自动生成；首次写入案例或构建后锁定，避免后续升级改名。</div></div>
      <div class="inline">
        <select class="select" id="workshop-model" title="本步骤使用的模型" style="max-width:210px">
          <option value="">自动选择默认模型</option>
          ${models.map((m) => `<option value="${m.id}">${esc(m.name || m.model || ('模型 ' + m.id))}</option>`).join('')}
        </select>
        <button class="btn" data-act="build">${icon('play')}校验并构建</button>
        <button class="btn ${canPublish ? 'btn-primary' : ''}" data-act="publish" ${canPublish ? '' : 'disabled'}
          title="${!isAdmin ? '仅管理员可发布' : !build.verified ? '需正例和反例回归通过' : ''}">${icon('upload')}发布</button>
      </div>
    </div>
    <div class="card-body">
      <div class="wizard-steps">
        <span class="ws ${draft.source_text ? 'active' : ''}"><span class="n">1</span>资料输入</span>
        <span class="ws ${analysis.intake ? 'active' : ''}"><span class="n">2</span>AI 解析与命名</span>
        <span class="ws ${quality.ok ? 'active' : ''}"><span class="n">3</span>规则审查</span>
        <span class="ws ${build.verified ? 'active' : ''}"><span class="n">4</span>案例回归</span>
        <span class="ws ${draft.status === 'published' ? 'active' : ''}"><span class="n">5</span>构建发布</span>
      </div>
      <div id="job-status" class="alert info" ${activeJob ? '' : 'hidden'}>${activeJob ? `${icon('activity')}<div>${esc(activeJob.message || '后台任务执行中')} · ${Number(activeJob.progress || 0)}%</div>` : ''}</div>
      ${modelNoticeHtml(draft.model_status?.notice, { compact: true })}
      <div class="alert ${quality.ok === false ? 'warn' : 'info'}">${icon('info')}<div><strong>当前下一步：</strong>${esc(nextStep)}
        <span class="tiny muted"> · ${esc(aiLabels[analysis.ai_status] || analysis.ai_status || '人工编辑')}${analysis.model ? ` · ${esc(analysis.model)}` : ''}</span></div></div>
      <div class="analysis-cards" style="margin-bottom:14px">
        <div class="mini-stat"><div class="v">${quality.score ?? '—'}</div><div class="k">质量分</div></div>
        <div class="mini-stat"><div class="v">${counts.rules ?? 0}</div><div class="k">证据规则</div></div>
        <div class="mini-stat"><div class="v">${counts.root_causes ?? 0}</div><div class="k">根因分支</div></div>
        <div class="mini-stat"><div class="v">${cases.length}</div><div class="k">已写入案例</div></div>
      </div>
      <div class="alert ${quality.ok === false ? 'danger' : (quality.warnings || []).length ? 'warn' : 'ok'}">
        ${icon(quality.ok === false ? 'alert' : 'check')}<div style="flex:1"><strong>${quality.ok === false ? '需要补充或修正' : '结构与质量审查通过'}</strong>
        · ${esc(aiLabels[analysis.ai_status] || analysis.ai_status || '人工编辑')}${analysis.model ? ` · ${esc(analysis.model)}` : ''}
        ${issueHtml(quality.errors, 'error')}${issueHtml(quality.warnings, 'warning')}</div></div>
      ${(analysis.notes || []).length ? `<div class="alert warn" style="margin-top:10px">${icon('alert')}<div>${analysis.notes.map((n) => esc(n)).join('<br>')}</div></div>` : ''}

      <div class="hr"></div>
      <h3 style="font-size:14px;margin:0 0 8px">步骤 2 · AI 解析、自动命名与中途确认</h3>
      <div class="grid grid-2" style="gap:16px">
        <div>
          <div class="kv">
            <div class="k">技能名</div><div class="v mono">${esc(draft.name)}</div>
            <div class="k">故障名称</div><div class="v">${esc(intake.fault_name || draft.fault_name)}</div>
            <div class="k">适用范围</div><div class="v">${esc(intake.domain?.boards || '待补充')}</div>
            <div class="k">现场现象</div><div class="v">${esc((intake.symptoms?.observable || []).join('；') || '待补充')}</div>
          </div>
          ${(quality.questions || []).length ? `<div class="alert info" style="margin-top:10px">${icon('info')}<div><strong>建议确认</strong>
            <ol style="margin:6px 0 0;padding-left:20px">${quality.questions.map((q) => `<li>${esc(q.question)}</li>`).join('')}</ol></div></div>` : ''}
          <textarea class="textarea" id="refine-text" rows="4" placeholder="直接补充或纠正，例如：0x634=0x10 是 caterr；C1 是正常反例。无需按字段填写。"></textarea>
          <div class="inline" style="margin-top:8px"><button class="btn btn-primary" data-act="refine">${icon('refresh')}按补充重新生成</button>
            <button class="btn" data-act="reanalyze">重新分析原文</button></div>
          ${draft.source_text ? `<details style="margin-top:10px"><summary class="small muted" style="cursor:pointer">查看原始资料</summary>
            <div class="check-output" style="max-height:220px;margin-top:6px">${esc(draft.source_text)}</div></details>` : ''}
        </div>
        <div>
          <h3 style="font-size:14px;margin:0 0 8px">真实日志与案例</h3>
          <div class="help">文本可以生成技能，但发布必须上传至少 1 个正例和 1 个正常/反例并通过回归。</div>
          <div class="inline" style="margin:10px 0"><button class="btn" data-act="upload-logs">${icon('upload')}上传日志并分析</button>
            <input type="file" id="log-input" accept=".zip,.tar.gz,.tgz,.7z,.tar,.gz,.log" multiple hidden></div>
          ${analysis.extract ? `<div class="tiny muted">解压 ${extract.files ?? 0} 个文件 · ${suggestions.length} 条规则候选</div>` : ''}
          ${(intake.cases || []).length ? `<div class="stack" style="max-height:180px;overflow:auto;margin-top:10px">${intake.cases.map((c) => `
            <div class="suggestion"><strong>${esc(c.id)} · ${c.kind === 'negative' ? '正常/反例' : '故障正例'}</strong>
              <div class="tiny">${esc(c.description || '')} → ${esc(c.expected_root_cause || '待确认')}</div></div>`).join('')}</div>` : ''}
          ${samples.length ? `<details style="margin-top:10px"><summary class="small muted">查看日志样例</summary>
            <div class="check-output" style="max-height:160px">${samples.map((s) => `### ${esc(s.file)}\n${esc((s.lines || '').slice(0, 700))}`).join('\n\n')}</div></details>` : ''}
        </div>
      </div>

      <div class="hr"></div>
      <div class="grid grid-2" style="gap:18px">
        <div><h3 style="font-size:14px;margin:0 0 8px">步骤 4 · 上传并写入回归案例</h3>
          <div class="help" style="margin-bottom:8px">至少 1 个故障正例和 1 个正常反例；正例根因必填，反例根因固定为 NONE。</div>
          ${cases.length ? `<div class="stack" style="max-height:220px;overflow:auto;margin-bottom:10px">${cases.map((c) => `
            <div class="suggestion"><div class="between"><strong>${esc(c.case_id)} · ${c.kind === 'negative' ? '正常/反例' : '故障正例'}</strong>
              <span class="badge ${c.kind === 'negative' ? 'muted' : 'ok'}">${esc(c.expected?.root_cause || c.expected_root_cause || 'NONE')}</span></div>
              <div class="tiny">${esc(c.description || '')}${((c.expected?.hit_rules || c.hit_rules || []).length) ? ` · 命中 ${esc((c.expected?.hit_rules || c.hit_rules || []).join(', '))}` : ''}</div></div>`).join('')}</div>`
            : `<div class="empty" style="padding:16px"><div class="tiny">尚未写入可回归的真实日志案例</div></div>`}
          <details ${cases.length ? '' : 'open'}><summary class="small muted" style="cursor:pointer">新增案例</summary>
          <form id="case-form" style="margin-top:10px"><div class="form-grid">
            <div class="form-row"><label class="label">案例 ID（自动递增，可修改）</label><input class="input" name="case_id" value="${esc(caseData.next_id || 'case-01')}"></div>
            <div class="form-row"><label class="label">类型</label><select class="select" name="kind"><option value="positive">故障正例</option><option value="negative">正常/反例</option></select></div></div>
            <div class="form-row"><label class="label">期望根因 ID（反例填 NONE）</label>
              <input class="input" name="root_cause" list="workshop-roots" placeholder="从根因下拉选择，正例必填">
              <datalist id="workshop-roots">${roots.map((r) => `<option value="${esc(r.id)}">${esc(r.name || r.title || '')}</option>`).join('')}<option value="NONE">正常/反例</option></datalist></div>
            <div class="form-row"><label class="label">应命中规则（可多选，逗号分隔）</label>
              <input class="input" name="hit_rules" list="workshop-rules" placeholder="E1,E2">
              <datalist id="workshop-rules">${rules.map((r) => `<option value="${esc(r.id)}">${esc(r.description || '')}</option>`).join('')}</datalist></div>
            <div class="form-row"><label class="label">案例日志包</label><input class="input" type="file" id="case-file" accept=".zip,.tar.gz,.tgz,.7z,.tar,.gz,.log" required></div>
            <div class="form-row"><label class="label">说明</label><input class="input" name="description" placeholder="现场现象、期望结论"></div>
            <label class="inline tiny" style="margin:6px 0"><input type="checkbox" name="overwrite"> 如案例 ID 已存在，明确覆盖旧案例</label>
            <button class="btn" type="submit">${icon('upload')}写入案例</button></form></details></div>
        <div><h3 style="font-size:14px;margin:0 0 8px">步骤 5 · 构建门禁与发布</h3>
          ${build.ok ? `<div class="alert ${build.verified ? 'ok' : 'warn'}">${icon(build.verified ? 'check' : 'alert')}<div>
            <strong>${build.verified ? '构建和案例回归通过' : '仅构建成功，不能发布'}</strong><br>v${esc(build.version || '—')} · ${build.verified ? '可以发布' : '请补真实正例和反例'}</div></div>`
            : `<div class="alert info">${icon('info')}<div>生成不等于验证。完成真实案例回归后，发布按钮才会开放。</div></div>`}
          ${build.output ? `<details style="margin-top:10px"><summary class="small muted">构建输出</summary><div class="check-output">${esc(build.output)}</div></details>` : ''}
        </div>
      </div>

      <details style="margin-top:18px"><summary class="small muted" style="cursor:pointer">高级：手工检查或编辑 Profile YAML（通常无需修改）</summary>
        <textarea class="textarea mono" id="profile-yaml" style="min-height:420px;margin-top:10px" spellcheck="false">${esc(draft.profile_yaml || '')}</textarea>
        <div class="between" style="margin-top:10px"><span class="small muted">优先用上方“补充/纠正”让 AI 重建；手工修改仅用于排障。</span>
          <div class="inline"><button class="btn" data-act="save">保存</button><button class="btn" data-act="check">${icon('check')}校验</button></div></div>
        ${check.output ? `<details style="margin-top:10px"><summary class="small muted">校验输出</summary><div class="check-output">${esc(check.output)}</div></details>` : ''}
      </details>
    </div></div>`;
}

function openCreateModal(onDone, models = []) {
  const m = modal({
    title: '粘贴资料生成技能', wide: true,
    body: `<form id="create-form">
      <div class="alert info">${icon('info')}<div>直接粘贴整段故障资料。系统会自主识别故障名称、日志路径、关键字、阈值、根因、处理动作和案例；缺信息时再集中确认。</div></div>
      <div class="form-row"><label class="label">故障资料</label>
        <textarea class="textarea" name="source_text" rows="16" required placeholder="例如：&#10;故障名称：VSW 看门狗复位&#10;现场现象：……&#10;日志路径：……&#10;关键日志：……&#10;判断思路：……&#10;根因与处理：……&#10;案例：……"></textarea></div>
      <div class="form-row"><label class="label">本步骤模型（可选）</label>
        <select class="select" name="model_id"><option value="">自动选择默认模型</option>
          ${models.map((m) => `<option value="${m.id}">${esc(m.name || m.model || ('模型 ' + m.id))}</option>`).join('')}
        </select></div>
      <div class="alert info">${icon('cpu')}<div>技能名和中文标题由 AI 根据资料自动生成，无需手工填写；提交后转入后台解析，页面实时显示进度。</div></div>
    </form>`,
    footer: `<button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="create">${icon('play')}识别并生成</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    if (!e.target.closest('[data-act="create"]')) return;
    const form = m.body.querySelector('#create-form');
    const sourceText = form.elements.source_text.value.trim();
    if (sourceText.length < 20) { toast('请粘贴完整故障资料', 'warn'); return; }
    const btn = e.target.closest('[data-act="create"]'); btn.disabled = true; setBusy(btn, true, '提交中…');
    try {
      const res = await api('/api/workshop/drafts?async=1', { method: 'POST', body: {
        source_text: sourceText, model_id: form.elements.model_id.value || null,
      } });
      selectedId = res.draft.id;
      toast('已提交后台解析，正在自动识别并命名', 'ok');
      m.close(); onDone && onDone();
    } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
  });
}
