// 技能工坊：整段资料 → 语义提取 → 确定性编译 → 审查修复 → 案例回归 → 发布
import {
  api, bindActions, bindTopbar, confirmDialog, esc, fmtDate, fmtSize, icon, modal, mount, setBusy,
  state, statusBadge, toast,
} from './core.js';
import { modelNoticeHtml, openPersonalModelModal } from './personal_model.js';

let selectedId = null;
let workshopPoll = null;
let workshopStep = 1;
let workshopStepDraftId = null;

export async function renderWorkshop() {
  if (workshopPoll) { clearInterval(workshopPoll); workshopPoll = null; }
  const view = mount('技能工坊', '按步骤完成资料输入、AI 命名、规则审查、案例回归与发布；长任务在后台执行并实时显示进度',
    `<button class="btn btn-primary" data-act="new-draft">${icon('plus')}粘贴资料生成技能</button>`);
  view.innerHTML = `<div class="workshop-layout">
    <aside class="card" style="align-self:start">
      <div class="card-head"><h2>草稿</h2><span class="sub" id="draft-count"></span></div>
      <div class="card-body" id="draft-list">${icon('refresh')} 加载中…</div>
    </aside>
    <section id="draft-detail"><div class="card"><div class="empty">${icon('flask')}<h3>选择一个草稿</h3>
      <p>可以直接粘贴故障描述、日志样例、判断思路、根因和案例，无需逐格填写。</p></div></div></section>
  </div>`;

  let drafts = [];
  let models = [];
  let recipes = [];
  try {
    const draftsData = await api('/api/workshop/drafts');
    drafts = Array.isArray(draftsData) ? draftsData : (draftsData?.items || []);
    try {
      const modelsData = await api('/api/models');
      models = (Array.isArray(modelsData) ? modelsData : (modelsData?.items || []))
        .filter((m) => m.enabled);
    } catch { models = []; }
    try {
      const recipesData = await api('/api/workshop/recipes');
      recipes = Array.isArray(recipesData) ? recipesData : (recipesData?.items || []);
    } catch { recipes = []; }
  } catch (err) {
    view.querySelector('#draft-detail').innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return view;
  }
  if (!drafts.length) {
    view.innerHTML = `<div class="card workshop-starter">
      <div class="workshop-starter-main">
        <div class="workshop-starter-icon">${icon('flask')}</div>
        <div class="workshop-starter-copy">
          <div class="eyebrow">技能工坊 · 0 个草稿</div>
          <h2>从一段真实故障资料开始</h2>
          <p>粘贴故障描述、日志样例、判断思路、根因和案例，系统会自动识别资料并生成可审查、可回归的技能草稿；无需逐格填写。</p>
          <div class="workshop-starter-actions">
            <button class="btn btn-primary" data-act="new-draft">${icon('plus')}粘贴资料生成技能</button>
            <span class="sub">支持文本、日志片段与 ZIP / tar.gz 日志包</span>
          </div>
        </div>
      </div>
      <ol class="workshop-starter-steps">
        <li><span>01</span><div><strong>粘贴资料</strong><p>故障描述、日志样例、判断思路、根因与案例</p></div></li>
        <li><span>02</span><div><strong>AI 语义提取</strong><p>识别故障对象、证据线索和候选根因</p></div></li>
        <li><span>03</span><div><strong>确定性编译</strong><p>生成规则、正则、阈值和证据门槛</p></div></li>
        <li><span>04</span><div><strong>案例回归</strong><p>用真实日志验证命中与误报</p></div></li>
        <li><span>05</span><div><strong>发布技能</strong><p>通过质量门禁后进入技能库</p></div></li>
      </ol>
    </div>`;
    const handlers = { 'new-draft': () => openCreateModal(() => renderWorkshop(), models, recipes, { uploadForm }) };
    bindActions(view, handlers); bindTopbar(handlers);
    return view;
  }

  if (!selectedId || !drafts.some((d) => d.id === selectedId)) selectedId = drafts[0]?.id || null;
  const listEl = view.querySelector('#draft-list');
  view.querySelector('#draft-count').textContent = `${drafts.length} 个`;
  listEl.innerHTML = drafts.length ? `<div class="draft-list">${drafts.map((d) => `
    <div class="draft-item ${d.id === selectedId ? 'active' : ''}" data-draft="${esc(d.id)}">
      <button class="icon-btn draft-delete" data-act="delete-draft" data-draft="${esc(d.id)}"
        title="删除草稿" aria-label="删除草稿">${icon('trash')}</button>
      <strong>${esc(d.title)}</strong><div class="name">${esc(d.name)}</div>
      <div class="meta">${statusBadge(d.status)}<span>${fmtDate(d.updated_at)}</span></div>
      ${d.last_error ? `<div class="tiny" style="color:#b91c1c;margin-top:3px">${esc(d.last_error.slice(0, 100))}</div>` : ''}
    </div>`).join('')}</div>` : `<div class="empty" style="padding:24px">${icon('flask')}
      <h3>还没有草稿</h3><p>粘贴一段故障资料，系统会自动识别并生成。</p>
      <button class="btn btn-primary btn-sm" data-act="new-draft">${icon('plus')}开始生成</button></div>`;
  listEl.querySelectorAll('[data-draft]').forEach((el) => {
    el.addEventListener('click', (event) => {
      if (event.target.closest('[data-act="delete-draft"]')) return;
      const nextId = el.dataset.draft;
      if (nextId !== selectedId) { workshopStepDraftId = null; workshopStep = 1; }
      selectedId = nextId;
      renderWorkshop();
    });
  });
  if (!selectedId) {
    const handlers = { 'new-draft': () => openCreateModal(() => renderWorkshop(), models, recipes, { uploadForm }) };
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
  if (workshopStepDraftId !== draftId) {
    workshopStepDraftId = draftId;
    workshopStep = suggestedStep(draft, caseData);
  }
  const detailEl = view.querySelector('#draft-detail');
  detailEl.innerHTML = renderDetail(draft, caseData, models, recipes);
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
          workshopStep = ({ analyze: 2, logs: 2, refine: 2, case: 4, build: 5 })[next.kind] || workshopStep;
          toast(next.message || '工坊任务完成', 'ok');
          setTimeout(refresh, 250);
        } else if (['failed', 'interrupted'].includes(next.status)) {
          clearInterval(workshopPoll); workshopPoll = null;
          if (next.kind === 'analyze' || next.kind === 'logs' || next.kind === 'refine') workshopStep = 2;
          if (next.kind === 'build') workshopStep = 5;
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
    'new-draft': () => openCreateModal(() => renderWorkshop(), models, recipes, { uploadForm }),
    'view-job-trace': async (btn) => {
      try {
        const jobId = btn.dataset.job;
        const data = await api(`/api/traces?kind=workshop&ref_id=${encodeURIComponent(jobId)}&limit=5`);
        const items = Array.isArray(data) ? data : (data?.items || []);
        const item = items[0];
        if (!item) { toast('该工坊任务还没有轨迹记录', 'warn'); return; }
        location.hash = `#/traces/${encodeURIComponent(item.id)}`;
      } catch (err) { toast(err.message, 'error'); }
    },
    'workshop-step': (btn) => {
      workshopStep = Math.max(1, Math.min(5, Number(btn.dataset.step) || 1));
      detailEl.querySelectorAll('[data-workshop-panel]').forEach((panel) => {
        panel.hidden = Number(panel.dataset.workshopPanel) !== workshopStep;
      });
      detailEl.querySelectorAll('[data-act="workshop-step"]').forEach((tab) => {
        tab.classList.toggle('active', Number(tab.dataset.step) === workshopStep);
        tab.setAttribute('aria-selected', Number(tab.dataset.step) === workshopStep ? 'true' : 'false');
      });
      const ruleId = btn.dataset.rule;
      if (ruleId) requestAnimationFrame(() => {
        const target = detailEl.querySelector(`#rule-${CSS.escape(ruleId)}`);
        if (target) { target.scrollIntoView({ block: 'center', behavior: 'smooth' }); target.focus?.(); }
      });
    },
    'delete-draft': async (btn) => {
      const id = btn.dataset.draft;
      const item = drafts.find((d) => d.id === id);
      if (!item) return;
      const ok = await confirmDialog(
        `删除草稿「${item.title}」及其日志/解压副本？${item.status === 'published'
          ? '该草稿已发布，删除草稿记录不会删除已发布技能。' : '此操作不可恢复。'}`,
        { title: '删除草稿', okText: '确认删除', danger: true });
      if (!ok) return;
      try {
        await api(`/api/workshop/drafts/${encodeURIComponent(id)}`, { method: 'DELETE' });
        toast('草稿已删除', 'ok');
        if (selectedId === id) { selectedId = null; workshopStepDraftId = null; workshopStep = 1; }
        renderWorkshop();
      } catch (err) { toast(err.message, 'error'); }
    },
    'upload-logs': () => detailEl.querySelector('#log-input').click(),
    'case-folder': () => detailEl.querySelector('#case-dir')?.click(),
    'save-source': async (btn) => {
      if (draft.status === 'published') { toast('已发布草稿不可编辑，请新建版本', 'warn'); return; }
      const sourceText = detailEl.querySelector('#source-text').value.trim();
      if (sourceText.length < 20) { toast('请粘贴完整故障资料，至少 20 字', 'warn'); return; }
      if (sourceText !== (draft.source_text || '').trim() && analysis.intake) {
        const ok = await confirmDialog(
          '修改资料会清空现有的规则审查、案例回归和构建结果；原始日志仍保留。确定重新分析？',
          { title: '重新分析资料', okText: '保存并重新分析' });
        if (!ok) return;
      }
      setBusy(btn, true, '保存资料中…');
      try {
        await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}`, {
          method: 'PUT', body: { source_text: sourceText },
        });
        workshopStep = 2;
        await startJsonJob(
          `/api/workshop/drafts/${encodeURIComponent(draftId)}/analyze`,
          { model_override: selectedModel() || null }, btn, '提交后台分析…');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    },
    'save-recipe': async (btn) => {
      if (draft.status === 'published') { toast('已发布草稿不可编辑，请新建版本', 'warn'); return; }
      const recipeId = detailEl.querySelector('#recipe-select').value;
      const changed = recipeId !== (draft.recipe_id || 'auto');
      if (!changed) { toast('配方未变化', 'info'); return; }
      if (analysis.intake) {
        const ok = await confirmDialog(
          '切换领域配方会清空现有规则审查、案例回归和构建结果，并按新配方重新分析。确定继续？',
          { title: '切换领域配方', okText: '保存并重新分析' });
        if (!ok) return;
      }
      setBusy(btn, true, '保存配方中…');
      try {
        await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}`, {
          method: 'PUT', body: { recipe_id: recipeId },
        });
        if (!draft.source_text) { toast('配方已保存；补充资料后再分析', 'ok'); refresh(); return; }
        workshopStep = 2;
        await startJsonJob(
          `/api/workshop/drafts/${encodeURIComponent(draftId)}/analyze`,
          { model_override: selectedModel() || null }, btn, '提交后台分析…');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    },
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
    'rules-try': async (btn) => {
      setBusy(btn, true, '试跑中…');
      try {
        const res = await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/rules/try`, { method: 'POST' });
        const hits = res.hits || [];
        modal({
          title: '规则试跑结果',
          wide: true,
          body: res.ok === false ? `<div class="alert danger">${icon('alert')}<div>${esc(res.error || '试跑失败')}</div></div>`
            : (() => {
                const details = res.rule_details || [];
                const pct = (v) => `${Math.round(Number(v || 0) * 100)}%`;
                return `<div class="alert ${hits.length ? 'ok' : 'warn'}">${icon(hits.length ? 'check' : 'alert')}<div>
                <strong>${res.rules || 0} 条规则，命中 ${hits.length} 条</strong>
                ${res.missed?.length ? ` · 未命中：${esc(res.missed.join('、'))}` : ' · 全部命中'}
                ${res.missed_files?.length ? ` · 未命中文件 ${res.missed_files.length} 个` : ''}</div></div>
              <div class="table-wrap"><table class="data"><thead><tr><th>规则</th><th>判定</th><th>文件命中/可用</th><th>覆盖率</th><th>未命中文件</th><th>近似行</th></tr></thead><tbody>
                ${details.slice(0, 40).map((d) => `<tr><td class="mono">${esc(d.id)}</td>
                  <td>${d.hit ? '<span class="badge ok">命中</span>' : '<span class="badge muted">未命中</span>'}</td>
                  <td>${(d.files_hit || []).length}/${Number(d.files_eligible || 0)}${d.file_scope && d.file_scope !== 'any' ? ` · ${esc(d.file_scope)}${d.min_files ? ` ${d.min_files}` : ''}` : ''}</td>
                  <td>${pct(d.file_coverage)}</td>
                  <td class="tiny">${esc((d.files_missed || []).slice(0, 6).join('、') || '—')}${(d.files_missed || []).length > 6 ? '…' : ''}</td>
                  <td class="small">${esc(((d.near_misses || [])[0] || '').slice(0, 120) || '—')}${(d.near_misses || []).length > 1 ? ` +${d.near_misses.length - 1}` : ''}</td></tr>`).join('')}
              </tbody></table></div>
              <div class="tiny muted" style="margin-top:6px">覆盖率 = 达到 min_matches 的文件 / 可用文本文件；binary/ciphered/空文件不计入分母。</div>`;
              })(),
        });
      } catch (err) { toast(err.message, 'error'); }
      setBusy(btn, false);
    },
    'lock-provenance': async (btn) => {
      setBusy(btn, true, '锁定中…');
      try {
        const proof = await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/provenance`);
        const paths = Object.values(proof.provenance || {}).filter((x) => x.hard && !x.locked).map((x) => x.path);
        if (!paths.length) { toast('没有可锁定的关键字段', 'warn'); return; }
        const ok = await confirmDialog(`锁定 ${paths.length} 个关键字段？锁定后模型修复不能覆盖其值。`,
          { title: '锁定字段来源', okText: '锁定' });
        if (!ok) return;
        await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/provenance/lock`,
          { method: 'POST', body: { paths, source_revision: proof.source_revision, locked: true } });
        toast(`已锁定 ${paths.length} 个字段`, 'ok'); refresh();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
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
    'rule-check-all': (el) => {
      detailEl.querySelectorAll('.rule-check').forEach((check) => { check.checked = el.checked; });
    },
    'verify-rules': async (btn) => {
      const ids = [...detailEl.querySelectorAll('.rule-check:checked')].map((el) => el.value);
      if (!ids.length) { toast('请先勾选已核对过的规则', 'warn'); return; }
      const ok = await confirmDialog(`确认已人工核对规则 ${ids.join('、')}？标记后仍需真实案例回归才能发布。`,
        { title: '确认规则', okText: '确认规则' });
      if (!ok) return;
      setBusy(btn, true, '标记中…');
      try {
        await saveProfileSilently();
        await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/verify-rules`,
          { method: 'POST', body: { rule_ids: ids } });
        toast(`已确认 ${ids.length} 条规则，可继续案例回归`, 'ok');
        refresh();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    },
    publish: async (btn) => {
      const ok = await confirmDialog('该技能已通过正例和反例回归。发布后进入正式技能库并参与规划，确定发布？', { title: '发布技能', okText: '发布' });
      if (!ok) return;
      setBusy(btn, true, '发布中…');
      try {
        const res = await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/publish`, { method: 'POST' });
        toast(`已发布 ${res.skill} v${res.version}`, 'ok'); refresh();
      } catch (err) {
        setBusy(btn, false);
        if (!String(err.message || '').includes('发布覆盖不足')) { toast(err.message, 'error'); return; }
        const m = modal({
          title: '发布覆盖不足',
          body: `<div class="alert warn">${icon('alert')}<div>${esc(err.message)}</div></div>
            <label class="label" style="margin-top:12px">带警告发布理由（不少于 10 字，将写入审计）</label>
            <textarea class="textarea" id="publish-override" rows="4" placeholder="例如：现场只有单站点日志，已由专家人工核对根因与反例"></textarea>`,
          footer: `<button class="btn" data-act="cancel">返回修复</button>
            <button class="btn btn-primary" data-act="override" disabled>带警告发布</button>`,
        });
        const ta = m.body.querySelector('#publish-override');
        const go = m.foot.querySelector('[data-act="override"]');
        ta.addEventListener('input', () => { go.disabled = ta.value.trim().length < 10; });
        m.mask.addEventListener('click', async (e) => {
          if (e.target.closest('[data-act="cancel"]')) m.close();
          if (!e.target.closest('[data-act="override"]') || go.disabled) return;
          setBusy(go, true, '发布中…');
          try {
            const res = await api(`/api/workshop/drafts/${encodeURIComponent(draftId)}/publish`,
              { method: 'POST', body: { override_reason: ta.value.trim() } });
            m.close(); toast(`已带警告发布 ${res.skill} v${res.version}`, 'warn'); refresh();
          } catch (err2) { toast(err2.message, 'error'); setBusy(go, false); }
        });
      }
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
  const caseDir = caseForm.querySelector('#case-dir');
  if (caseDir) caseDir.addEventListener('change', () => {
    if (!caseDir.files.length) return;
    const target = caseForm.querySelector('#case-file');
    const dt = new DataTransfer();
    [...caseDir.files].forEach((f) => dt.items.add(f));
    target.files = dt.files;
    toast(`已选择文件夹中的 ${caseDir.files.length} 个文件`, 'ok');
  });
  caseForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const picked = [...caseForm.querySelector('#case-file').files];
    if (!picked.length) { toast('请选择案例日志包或文件夹', 'warn'); return; }
    const btn = caseForm.querySelector('button[type="submit"]');
    const fd = new FormData();
    picked.forEach((file) => fd.append('files', file, file.name));
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
    if (caseForm.querySelector('#case-dir')) caseForm.querySelector('#case-dir').value = '';
  });

  const activeJob = (draft.jobs || []).find((j) => ['queued', 'running'].includes(j.status));
  if (activeJob) beginPoll(activeJob);
  return view;
}

function issueHtml(items, type) {
  return (items || []).map((x) => {
    const field = String(x.field || '');
    const ruleId = (field.match(/\b[A-Za-z]\d{1,3}\b/) || [field.split('.').pop() || ''])[0];
    return `<div class="tiny" style="margin:4px 0;color:${type === 'error' ? '#b91c1c' : '#92400e'};display:flex;gap:6px;align-items:baseline">
      <strong>${esc(field)}</strong><span style="flex:1">${esc(x.message || x.suggestion || '')}</span>
      ${ruleId ? `<button class="btn btn-sm" type="button" data-act="workshop-step" data-step="3" data-rule="${esc(ruleId)}">定位</button>` : ''}
    </div>`;
  }).join('');
}

function suggestedStep(draft, caseData = {}) {
  const analysis = draft.analysis || {};
  const quality = analysis.quality || draft.check?.quality || {};
  if (!analysis.intake) return (draft.source_text || draft.log_path) ? 2 : 1;
  if (quality.ok === false) return 3;
  if (!(draft.build || {}).verified) return 4;
  return 5;
}

function renderDetail(draft, caseData = {}, models = [], recipes = []) {
  const analysis = draft.analysis || {};
  const intake = analysis.intake || {};
  const quality = analysis.quality || draft.check?.quality || {};
  const check = draft.check || {};
  const build = draft.build || {};
  const extract = analysis.extract || {};
  const suggestions = analysis.rule_candidates?.rule_suggestions || [];
  const samples = analysis.samples || [];
  const counts = quality.counts || {};
  const cases = Array.isArray(caseData) ? caseData : (caseData?.items || []);
  const autoCases = analysis.auto_cases || null;
  const autoMatched = Array.isArray(autoCases?.matched) ? autoCases.matched : [];
  const autoUnmatched = Array.isArray(autoCases?.unmatched_cases) ? autoCases.unmatched_cases : [];
  const autoUnassigned = Array.isArray(autoCases?.unassigned_files) ? autoCases.unassigned_files : [];
  const autoWarnings = Array.isArray(autoCases?.warnings) ? autoCases.warnings : [];
  const autoSummary = autoCases?.enabled ? `<div class="alert ${autoMatched.length ? 'ok' : 'warn'}" style="margin-top:10px">
    ${icon(autoMatched.length ? 'check' : 'alert')}<div><strong>自动案例匹配：已建立 ${autoMatched.length} 个案例</strong>
    ${autoUnmatched.length ? ` · ${autoUnmatched.length} 个资料案例未匹配到文件` : ''}
    ${autoUnassigned.length ? ` · ${autoUnassigned.length} 个文件未归入案例` : ''}
    <span class="tiny muted"> · 可在“案例验证”步骤查看文件对应关系</span></div></div>` : '';
  const isAdmin = state.user?.role === 'admin';
  const published = draft.status === 'published';
  const canPublish = build.ok && build.verified && quality.ok !== false && isAdmin && !published;
  const aiLabels = { success: 'AI 提取成功', failed: 'AI 失败，已降级为确定性解析', not_configured: '未配置模型，使用确定性解析', unavailable: '模型暂不可用，已降级', not_applicable: '人工草稿' };
  const aiStatusText = () => {
    if (analysis.ai_status === 'success') {
      if (analysis.repair_status === 'failed') return 'AI 提取成功，修复未完成';
      if (analysis.review_status === 'failed') return 'AI 提取成功，评审未完成';
      if (analysis.review_status === 'success' && analysis.repair_status === 'success') return 'AI 提取、评审与修复成功';
      if (analysis.review_status === 'success') return 'AI 提取与评审成功';
    }
    return aiLabels[analysis.ai_status] || analysis.ai_status || '人工编辑';
  };
  const roots = intake.root_causes || [];
  const rawRules = (intake.evidence_rules || []).length
    ? intake.evidence_rules
    : suggestions.filter((s) => s.rule).map((s) => s.rule);
  const ruleItems = [...new Map((rawRules || []).map((rule) => [String(rule.id || ''), rule])).values()];
  // 后端的 unverified_rules 是当前 profile 的唯一权威快照；不要在确认后
  // 再从旧 analysis.intake 的 rule.verified 重新添加待确认项。
  const hasCheck = Array.isArray(check.unverified_rules);
  const unverified = new Set((check.unverified_rules || []).map(String));
  if (!hasCheck) ruleItems.forEach((rule) => { if (rule.verified === false && rule.id) unverified.add(String(rule.id)); });
  const activeJob = (draft.jobs || []).find((j) => ['queued', 'running'].includes(j.status));
  const hasPositive = cases.some((c) => c.kind === 'positive');
  const hasNegative = cases.some((c) => c.kind === 'negative');
  const stageStatus = !analysis.intake ? 2
    : quality.ok === false ? 3
    : unverified.size ? 3
    : !build.verified ? 4 : 5;
  const nextStep = stageStatus === 2 ? '等待 AI 解析资料，或补充文字后重新分析'
    : stageStatus === 3 ? (quality.ok === false ? '先处理质量审查中的错误，再确认规则'
      : `${unverified.size} 条规则待人工确认`)
    : stageStatus === 4 ? (hasPositive && hasNegative ? '案例已齐备，执行构建回归' : '上传至少一个故障案例和一个正常案例')
    : published ? '已发布' : '回归已通过，可由管理员发布';
  const currentStep = Math.max(1, Math.min(5, Number(workshopStep) || 1));
  const done = {
    1: Boolean(draft.source_text || draft.log_path),
    2: Boolean(analysis.intake),
    3: Boolean(analysis.intake) && quality.ok !== false && unverified.size === 0,
    4: Boolean(build.verified),
    5: published,
  };
  const stepButton = (num, label) => `<button class="workshop-tab ${num === currentStep ? 'active' : ''} ${done[num] ? 'done' : ''}"
    role="tab" aria-selected="${num === currentStep ? 'true' : 'false'}" data-act="workshop-step" data-step="${num}">
    <span class="n">${num}</span><span>${label}</span>${done[num] ? '<span class="tick">✓</span>' : ''}</button>`;
  const panel = (num, html) => `<div class="workshop-step-panel" data-workshop-panel="${num}" ${num === currentStep ? '' : 'hidden'}>${html}</div>`;
  const nextButton = (num, label) => `<button class="btn" data-act="workshop-step" data-step="${num}">${label}${icon('chevron')}</button>`;
  const prevButton = (num) => `<button class="btn" data-act="workshop-step" data-step="${num}">←返回上一步</button>`;
  const conditionText = (rule) => {
    const bits = [];
    if ((rule.all_of || []).length) bits.push(`全部：${rule.all_of.join('、')}`);
    if ((rule.any_of || []).length) bits.push(`任一：${rule.any_of.join('、')}`);
    if ((rule.none_of || []).length) bits.push(`排除：${rule.none_of.join('、')}`);
    return bits.join('；') || '未填写匹配条件';
  };
  const issue = issueHtml(quality.errors, 'error') + issueHtml(quality.warnings, 'warning');
  const qualityAlert = `<div class="alert ${quality.ok === false ? 'danger' : quality.warnings?.length ? 'warn' : 'ok'}">
    ${icon(quality.ok === false ? 'alert' : 'check')}<div style="flex:1"><strong>${quality.ok === false ? '需要补充或修正' : '结构与质量审查通过'}</strong>
    · ${esc(aiStatusText())}${analysis.model ? ` · ${esc(analysis.model)}` : ''}
    ${issue}</div></div>`;

  const header = `<div class="card">
    <div class="card-head">
      <div style="flex:1;min-width:0"><h2 style="display:flex;align-items:center;gap:8px">${esc(draft.title)} ${statusBadge(draft.status)}
        ${draft.skill_version ? `<span class="badge muted">v${esc(draft.skill_version)}</span>` : ''}</h2>
        <div class="mono muted tiny">${esc(draft.name)} ${draft.name_auto ? '<span class="badge muted">AI 自动命名</span>' : ''} · ${esc(draft.fault_name)} · 更新 ${fmtDate(draft.updated_at)}</div>
        <div class="tiny muted" style="margin-top:4px">5 个步骤可随时切换：回到任一步修改后，再继续执行后续步骤。</div></div>
      <div class="inline">
        <select class="select" id="workshop-model" title="本步骤使用的模型" style="max-width:210px">
          <option value="">自动选择默认模型</option>
          ${models.map((m) => `<option value="${m.id}">${esc(m.name || m.model || ('模型 ' + m.id))}</option>`).join('')}
        </select>
        <button class="btn ${canPublish ? 'btn-primary' : ''}" data-act="publish" ${canPublish ? '' : 'disabled'}
          title="${!isAdmin ? '仅管理员可发布' : !build.verified ? '需正例和反例回归通过' : ''}">${icon('upload')}发布</button>
      </div>
    </div>
    <div class="card-body">
      <div class="tabs workshop-tabs" role="tablist">
        ${stepButton(1, '资料输入')}${stepButton(2, '故障解析')}${stepButton(3, '规则确认')}${stepButton(4, '案例验证')}${stepButton(5, '构建发布')}
      </div>
      ${(models || []).some((m) => m.transport === 'browser') ? `<div class="alert info" style="margin-bottom:10px">${icon('cpu')}<div>本次 AI 环节可能由你的浏览器执行，请保持本页打开；浏览器不在线时会自动回退服务端模型。</div></div>` : ''}
      <div id="job-status" class="alert info" ${activeJob ? '' : 'hidden'}>${activeJob ? `${icon('activity')}<div>${esc(activeJob.message || '后台任务执行中')} · ${Number(activeJob.progress || 0)}%</div>` : ''}</div>
      ${(draft.jobs || []).length ? `<details style="margin-bottom:10px"><summary class="small muted">后台任务详情（${draft.jobs.length}）</summary>
        <div class="table-wrap"><table class="data"><thead><tr><th>类型</th><th>状态</th><th>阶段</th><th>进度</th><th>更新时间</th><th>错误</th>${isAdmin ? '<th>过程</th>' : ''}</tr></thead><tbody>
        ${draft.jobs.slice(0, 8).map((j) => `<tr><td>${esc(j.kind || '—')}</td><td>${statusBadge(j.status)}</td>
          <td>${esc(j.stage || '—')}</td><td>${Number(j.progress || 0)}%</td><td class="tiny">${fmtDate(j.updated_at || j.created_at)}</td>
          <td class="tiny" style="color:#b91c1c">${esc((j.error || '').slice(0, 120))}</td>
          ${isAdmin ? `<td><button class="btn btn-sm" data-act="view-job-trace" data-job="${esc(j.id)}">${icon('history')}查看过程</button></td>` : ''}</tr>`).join('')}
        </tbody></table></div></details>` : ''}
      ${modelNoticeHtml(draft.model_status?.notice, { compact: true })}
      <div class="alert ${quality.ok === false ? 'warn' : 'info'}">${icon('info')}<div><strong>当前下一步：</strong>${esc(nextStep)}
        <span class="tiny muted"> · ${esc(aiStatusText())}${analysis.model ? ` · ${esc(analysis.model)}` : ''}</span></div></div>
      ${autoSummary}
    </div>
  </div>`;

  const step1 = panel(1, `<div class="alert info">${icon('info')}<div>直接粘贴整段故障资料即可，字段不完整也可以。修改资料并重新分析后，后续规则审查、案例回归和构建结果会重置，原始日志不会删除。</div></div>
    <div class="form-row"><label class="label" for="source-text">故障资料</label>
      <textarea class="textarea" id="source-text" rows="16" ${published ? 'readonly' : ''}
        placeholder="例如：故障名称、现场现象、日志路径、关键日志、判断思路、根因与处理、案例……">${esc(draft.source_text || '')}</textarea></div>
    <div class="between tiny muted"><span>${draft.source_text ? '可以直接补改原文；保存后重新进入 AI 解析。' : '至少需要故障名称、日志特征和判断思路，约 20 字以上。'}</span><span>${draft.source_text ? `${draft.source_text.length} 字` : ''}</span></div>
    <div class="inline" style="margin-top:12px;flex-wrap:wrap">
      <button class="btn btn-primary" data-act="save-source" ${published ? 'disabled' : ''}>${icon('play')}保存资料并重新分析</button>
      <button class="btn" data-act="upload-logs" ${published ? 'disabled' : ''}>${icon('upload')}上传日志并分析</button>
      <input type="file" id="log-input" accept=".zip,.tar,.tar.gz,.tgz,.7z,.rar,.gz,.bz2,.xz,.zst,.log,.txt" multiple hidden>
    </div>
    ${extract.files ? `<div class="alert info" style="margin-top:12px">${icon('folder')}<div>最近一次分析已解压 ${extract.files} 个文件 · ${(extract.bytes || 0) / 1024 / 1024} MB；中间解压文件可在“系统信息 → 任务空间”单独释放。</div></div>` : ''}
    ${(analysis.notes || []).length ? `<div class="alert warn" style="margin-top:10px">${icon('alert')}<div>${analysis.notes.map((n) => esc(n)).join('<br>')}</div></div>` : ''}
    <div class="inline" style="margin-top:14px">${draft.source_text || draft.log_path ? nextButton(2, '进入 AI 解析') : ''}</div>`);

  const step2 = panel(2, !analysis.intake
    ? `<div class="empty">${icon('cpu')}<h3>还没有解析结果</h3><p>先在“资料输入”粘贴故障资料，或上传日志包。</p>${prevButton(1)}</div>`
    : `<div class="analysis-cards" style="margin-bottom:14px">
        <div class="mini-stat"><div class="v">${quality.score ?? '—'}</div><div class="k">质量分</div></div>
        <div class="mini-stat"><div class="v">${counts.rules ?? ruleItems.length}</div><div class="k">证据规则</div></div>
        <div class="mini-stat"><div class="v">${counts.root_causes ?? roots.length}</div><div class="k">根因分支</div></div>
        <div class="mini-stat"><div class="v">${cases.length}</div><div class="k">已写入案例</div></div>
      </div>
      ${qualityAlert}
      ${(() => {
        const dg = analysis.digest || {};
        if (!dg.inventory) return '';
        const inv = dg.inventory || {};
        const cls = Object.entries(inv.classes || {}).map(([k, v]) => `${k} ${v}`).join('、') || '—';
        return `<div class="alert info" style="margin-top:10px">${icon('search')}<div style="flex:1">
          <strong>资料盘点 ${inv.files || 0} 个文件</strong> · ${Number(inv.bytes || 0).toLocaleString()} 字节 · ${esc(cls)}
          · 异常行 ${dg.anomaly_count || 0}
          <div class="tiny muted">${(dg.coverage_notes || []).map(esc).join('；') || '摘要未截断'}</div>
          ${(dg.candidates || []).length ? `<div class="tiny" style="margin-top:4px">候选规则：${dg.candidates.slice(0, 4).map((c) => `${esc(c.rule_id)}（${c.files} 文件/${c.hits} 次）`).join('、')}</div>` : ''}
        </div></div>`;
      })()}
      ${(() => {
        const prov = intake.provenance || {};
        const hard = (intake.hard_fields || []).map((k) => prov[k]).filter(Boolean);
        const verified = hard.filter((x) => x.validation_status === 'verified').length;
        const derived = hard.filter((x) => x.validation_status === 'derived').length;
        const sourced = verified + derived;
        const missing = hard.filter((x) => x.validation_status !== 'verified' && x.validation_status !== 'derived');
        const locked = Object.values(prov).filter((x) => x.locked).length;
        return `<div class="alert ${missing.length ? 'warn' : 'ok'}" style="margin-top:12px">${icon(missing.length ? 'alert' : 'check')}<div style="flex:1">
          <strong>字段来源 ${sourced}/${hard.length}</strong>${derived ? `（原文直引 ${verified}，可追溯推导 ${derived}）` : ''} · 锁定 ${locked} 个
          <div class="tiny muted">来源修订 ${esc(intake.source_revision || '—')}；引用区间与 quote hash 随原文保存，模型改写不会静默覆盖已锁字段。</div>
          ${missing.length ? `<div class="tiny" style="margin-top:4px">未定位：${missing.slice(0, 6).map((x) => esc(x.path)).join('、')}</div>` : ''}
          <div class="inline" style="margin-top:8px"><button class="btn btn-sm" data-act="lock-provenance" ${locked || !hard.length ? 'disabled' : ''}>${icon('lock')}锁定关键字段</button></div>
        </div></div>`;
      })()}
      <div class="grid grid-2" style="gap:16px;margin-top:14px">
        <div><div class="kv">
          <div class="k">技能名</div><div class="v mono">${esc(draft.name)}</div>
          <div class="k">故障名称</div><div class="v">${esc(intake.fault_name || draft.fault_name)}</div>
          <div class="k">适用范围</div><div class="v">${esc(intake.domain?.boards || '待补充')}</div>
          <div class="k">现场现象</div><div class="v">${esc((intake.symptoms?.observable || []).join('；') || '待补充')}</div>
        </div>
        ${(quality.questions || []).length ? `<div class="alert info" style="margin-top:10px">${icon('info')}<div><strong>建议确认</strong>
          <ol style="margin:6px 0 0;padding-left:20px">${quality.questions.map((q) => `<li>${esc(q.question)}</li>`).join('')}</ol></div></div>` : ''}
        </div>
        <div><h3 style="font-size:14px;margin:0 0 8px">补充或纠正</h3>
          <textarea class="textarea" id="refine-text" rows="6" placeholder="直接补充或纠正，例如：0x634=0x10 是 caterr；C1 是正常反例。无需按字段填写。"></textarea>
          <div class="inline" style="margin-top:8px;flex-wrap:wrap"><button class="btn btn-primary" data-act="refine">${icon('refresh')}按补充重新生成</button>
            <button class="btn" data-act="reanalyze">重新分析原文</button></div>
          <div class="help" style="margin-top:8px">补充内容会追加到原始资料后重新生成；原始资料可在“资料输入”中修改。</div>
        </div>
      </div>
      ${samples.length ? `<details style="margin-top:10px"><summary class="small muted">查看日志样例</summary>
        <div class="check-output" style="max-height:160px">${samples.map((s) => `### ${esc(s.file)}\n${esc((s.lines || '').slice(0, 700))}`).join('\n\n')}</div></details>` : ''}
      <div class="inline" style="margin-top:14px">${prevButton(1)}${nextButton(3, '进入规则审查')}</div>`);

  const step3 = panel(3, !analysis.intake
    ? `<div class="empty">${icon('list')}<h3>尚未生成规则</h3><p>先完成资料输入和 AI 解析。</p>${prevButton(1)}</div>`
    : `${ruleItems.length ? `<div class="alert ${unverified.size ? 'warn' : 'ok'}">${icon(unverified.size ? 'alert' : 'check')}<div>
        <strong>${ruleItems.length} 条证据规则</strong> · ${unverified.size ? `${unverified.size} 条待人工确认` : '全部已确认'}
        <div class="tiny muted">人工确认仅表示规则表达式经过核对；发布仍需真实正例和反例回归。</div></div></div>
      <div class="between" style="margin:10px 0"><label class="check"><input type="checkbox" data-change="rule-check-all"> 全选待确认规则</label>
        <span class="inline"><button class="btn btn-sm" data-act="rules-try">${icon('play')}用现有日志试跑</button>
        <button class="btn btn-sm" data-act="check">${icon('check')}校验规则结构</button></span></div>
      <div class="stack">${ruleItems.map((rule) => {
        const id = String(rule.id || '');
        const pending = unverified.has(id);
        return `<div class="suggestion" id="rule-${esc(id)}" tabindex="-1">
          <div class="between"><strong>${esc(id || '未命名')} · ${esc(rule.description || '')}</strong>
            <span class="badge ${pending ? 'warn' : 'ok'}">${pending ? '待人工确认' : '已确认'}</span></div>
          <div class="tiny muted" style="margin-top:4px">${esc(conditionText(rule))}</div>
          <div class="tiny muted mono" style="margin-top:3px">${esc((rule.globs || []).join(', ') || '未限定文件')}</div>
          <div style="margin-top:6px;display:flex;gap:5px;flex-wrap:wrap">
            ${rule.type ? `<span class="badge muted">${esc(rule.type)}</span>` : ''}
            ${(rule.all_of || []).length ? '<span class="badge info">all_of</span>' : ''}
            ${(rule.any_of || []).length ? '<span class="badge info">any_of</span>' : ''}
            ${(rule.none_of || []).length ? '<span class="badge muted">none_of</span>' : ''}
            ${rule.numeric || rule.threshold ? '<span class="badge warn">数值阈值</span>' : ''}
            ${rule.temporal || rule.window_minutes ? '<span class="badge warn">时间窗</span>' : ''}
            ${rule.verified ? '<span class="badge ok">已人工确认</span>' : ''}
          </div>
          <details style="margin-top:6px"><summary class="tiny muted">规则表达式</summary>
            <pre class="check-output" style="max-height:180px;margin-top:6px">${esc(JSON.stringify(rule, null, 2))}</pre></details>
          ${pending ? `<label class="check tiny" style="margin-top:7px"><input type="checkbox" class="rule-check" value="${esc(id)}"> 已核对这条规则</label>` : ''}
        </div>`;
      }).join('')}</div>
      ${unverified.size ? `<div class="inline" style="margin-top:12px"><button class="btn btn-primary" data-act="verify-rules">${icon('check')}确认选中的规则</button></div>` : ''}`
      : `<div class="empty">${icon('list')}<h3>没有可审查的规则</h3><p>AI 解析未生成证据规则，请回到资料输入补充日志特征或判断思路。</p></div>`}
      ${check.output ? `<details style="margin-top:10px"><summary class="small muted">校验输出</summary><div class="check-output">${esc(check.output)}</div></details>` : ''}
      <div class="inline" style="margin-top:14px">${prevButton(2)}${nextButton(4, '进入案例回归')}</div>`);

  const step4 = panel(4, `<div class="alert info">${icon('info')}<div>至少上传 1 个故障案例和 1 个正常案例；故障案例根因必填，正常案例根因固定为 NONE。可一次多选文件，或直接上传文件夹。</div></div>
    ${autoCases?.enabled ? `<div class="alert ${autoMatched.length ? 'ok' : 'warn'}" style="margin-top:10px">${icon(autoMatched.length ? 'check' : 'alert')}<div>
      <strong>自动案例匹配：已建立 ${autoMatched.length} 个案例</strong>
      ${autoUnmatched.length ? ` · ${autoUnmatched.length} 个资料案例未匹配到文件` : ''}
      ${autoUnassigned.length ? ` · ${autoUnassigned.length} 个文件未归入案例` : ''}
      <div class="tiny muted" style="margin-top:4px">${autoMatched.slice(0, 6).map((item) =>
        `${esc(item.case_id)} ← ${esc((item.files || []).join('、'))}`).join('<br>') || '资料中没有可自动落库的案例。'}</div>
      ${autoWarnings.length ? `<div class="tiny" style="margin-top:4px;color:#92400e">${esc(autoWarnings.slice(0, 4).join('；'))}</div>` : ''}
    </div></div>` : ''}
    <div class="grid grid-2" style="gap:18px;align-items:start">
      <div><h3 style="font-size:14px;margin:0 0 8px">已写入案例（${cases.length}）</h3>
        ${cases.length ? `<div class="stack" style="max-height:360px;overflow:auto">${cases.map((c) => `
          <div class="suggestion"><div class="between"><strong>${esc(c.case_id)} · ${c.kind === 'negative' ? '正常案例' : '故障案例'}</strong>
            <span class="badge ${c.kind === 'negative' ? 'muted' : 'ok'}">${esc(c.expected?.root_cause || c.expected_root_cause || 'NONE')}</span></div>
            <div class="tiny">${esc(c.description || '')}${((c.expected?.hit_rules || c.hit_rules || []).length) ? ` · 命中 ${esc((c.expected?.hit_rules || c.hit_rules || []).join(', '))}` : ''}</div></div>`).join('')}</div>`
          : `<div class="empty" style="padding:18px">${icon('flask')}<p>尚未写入可回归的真实日志案例。</p></div>`}
        <div class="inline" style="margin-top:12px;flex-wrap:wrap">
          <button class="btn" data-act="build">${icon('play')}执行构建与案例回归</button>
          <span class="tiny muted">构建完成后自动进入第 5 步</span></div>
      </div>
      <div><h3 style="font-size:14px;margin:0 0 8px">新增案例</h3>
        <form id="case-form"><div class="form-grid">
          <div class="form-row"><label class="label">案例 ID（自动递增，可修改）</label><input class="input" name="case_id" value="${esc(caseData.next_id || 'case-01')}"></div>
          <div class="form-row"><label class="label">类型</label><select class="select" name="kind"><option value="positive">故障案例</option><option value="negative">正常案例</option></select></div></div>
          <div class="form-row"><label class="label">期望根因 ID（正常案例填 NONE）</label>
            <input class="input" name="root_cause" list="workshop-roots" placeholder="从根因下拉选择，正例必填">
            <datalist id="workshop-roots">${roots.map((r) => `<option value="${esc(r.id)}">${esc(r.name || r.title || '')}</option>`).join('')}<option value="NONE">正常案例</option></datalist></div>
          <div class="form-row"><label class="label">应命中规则（可多选，逗号分隔）</label>
            <input class="input" name="hit_rules" list="workshop-rules" placeholder="E1,E2">
            <datalist id="workshop-rules">${ruleItems.map((r) => `<option value="${esc(r.id)}">${esc(r.description || '')}</option>`).join('')}</datalist></div>
          <div class="form-row"><label class="label">案例日志包 <span class="hint">可多选；多个文件会作为同一案例的输入</span></label>
            <input class="input" type="file" id="case-file" accept=".zip,.tar,.tar.gz,.tgz,.7z,.rar,.gz,.bz2,.xz,.zst,.log,.txt" multiple required>
            <div style="margin-top:6px"><button class="btn btn-sm" type="button" data-act="case-folder">${icon('folder')}上传文件夹</button>
            <input type="file" id="case-dir" webkitdirectory directory multiple hidden></div></div>
          <div class="form-row"><label class="label">说明</label><input class="input" name="description" placeholder="现场现象、期望结论"></div>
          <label class="inline tiny" style="margin:6px 0"><input type="checkbox" name="overwrite"> 如案例 ID 已存在，明确覆盖旧案例</label>
          <button class="btn" type="submit">${icon('upload')}写入案例</button></form>
      </div>
    </div>
    ${build.ok ? `<div class="alert ${build.verified ? 'ok' : 'warn'}" style="margin-top:12px">${icon(build.verified ? 'check' : 'alert')}<div>
      <strong>${build.verified ? '构建和案例回归通过' : '仅构建成功，不能发布'}</strong><br>v${esc(build.version || '—')} · ${build.verified ? '可以发布' : '请补故障案例和正常案例'}</div></div>` : ''}
    <div class="inline" style="margin-top:14px">${prevButton(3)}${nextButton(5, '进入构建发布')}</div>`);

  const step5 = panel(5, `<div class="alert ${build.verified ? 'ok' : build.ok ? 'warn' : 'info'}">${icon(build.verified ? 'check' : build.ok ? 'alert' : 'info')}<div>
      <strong>${published ? '已发布' : build.verified ? '构建和案例回归通过，可以发布' : build.ok ? '仅构建成功，不能发布' : '尚未构建'}</strong><br>
      ${build.ok ? `v${esc(build.version || '—')} · ${build.verified ? '故障/正常案例回归已验证' : '需要至少 1 个故障案例和 1 个正常案例通过'}`
        : '完成前 4 步后执行构建；生成不等于验证。'}
      ${published && draft.skill_version ? ` · 已发布 v${esc(draft.skill_version)}` : ''}
    </div></div>
    ${!isAdmin ? `<div class="alert warn">${icon('shield')}<div>仅管理员可发布；你可以完成规则审查和案例回归后交由管理员发布。</div></div>` : ''}
    <div class="inline" style="margin-top:12px;flex-wrap:wrap">
      <button class="btn" data-act="build">${icon('play')}${build.ok ? '重新构建回归' : '执行构建回归'}</button>
      <button class="btn ${canPublish ? 'btn-primary' : ''}" data-act="publish" ${canPublish ? '' : 'disabled'}>${icon('upload')}发布技能</button>
    </div>
    ${build.output ? `<details style="margin-top:12px"><summary class="small muted">构建输出</summary><div class="check-output">${esc(build.output)}</div></details>` : ''}
    <div class="hr"></div>
    <details><summary class="small muted" style="cursor:pointer">高级：手工检查或编辑 Profile YAML（通常无需修改）</summary>
      <textarea class="textarea mono" id="profile-yaml" style="min-height:420px;margin-top:10px" spellcheck="false" ${published ? 'readonly' : ''}>${esc(draft.profile_yaml || '')}</textarea>
      <div class="between" style="margin-top:10px"><span class="small muted">优先用前面步骤的补充/纠正重建；手工修改仅用于排障。</span>
        <div class="inline"><button class="btn" data-act="save" ${published ? 'disabled' : ''}>保存</button><button class="btn" data-act="check">${icon('check')}校验</button></div></div>
      ${check.output ? `<details style="margin-top:10px"><summary class="small muted">校验输出</summary><div class="check-output">${esc(check.output)}</div></details>` : ''}
    </details>
    <div class="inline" style="margin-top:14px">${prevButton(4)}</div>`);

  return header + step1 + step2 + step3 + step4 + step5;
}


function openCreateModal(onDone, models = [], recipes = [], helpers = {}) {
  const m = modal({
    title: '粘贴资料生成技能', wide: true,
    body: `<form id="create-form">
      <div class="alert info">${icon('info')}<div>直接粘贴整段故障资料，或同时选择日志/案例文件与文件夹。系统会自主识别故障名称、日志路径、关键字、阈值、根因、处理动作和案例，并把资料中的案例自动匹配到上传文件；一个案例可以包含多个文件。</div></div>
      <div class="form-row"><label class="label">故障资料</label>
        <textarea class="textarea" name="source_text" rows="13" placeholder="例如：&#10;故障名称：VSW 看门狗复位&#10;现场现象：……&#10;日志路径：……&#10;关键日志：……&#10;判断思路：……&#10;根因与处理：……&#10;案例：……"></textarea>
        <div class="hint">可只选文件；只选文件时系统会从文件名/目录和日志摘要自动归组案例。</div></div>
      <div class="form-row"><label class="label">日志 / 案例文件 <span class="hint">支持多选、文件夹和压缩包</span></label>
        <input class="input" type="file" id="create-files" multiple
          accept=".zip,.tar,.tar.gz,.tgz,.7z,.rar,.gz,.bz2,.xz,.zst,.log,.txt,.bak">
        <div class="inline" style="margin-top:6px;flex-wrap:wrap">
          <button class="btn btn-sm" type="button" data-act="pick-folder">${icon('folder')}选择文件夹</button>
          <button class="btn btn-sm" type="button" data-act="clear-files">清空已选文件</button>
          <span class="tiny muted">文件夹会保留相对路径，用于按目录自动归组案例</span>
        </div>
        <input type="file" id="create-folder" webkitdirectory directory multiple hidden>
        <div id="create-file-list" class="tiny muted" style="margin-top:6px">尚未选择文件</div></div>
      <div class="form-row"><label class="label">本步骤模型（可选）</label>
        <select class="select" name="model_id"><option value="">自动选择默认模型</option>
          ${models.map((m) => `<option value="${m.id}">${esc(m.name || m.model || ('模型 ' + m.id))}</option>`).join('')}
        </select>
        <div class="hint">技能工坊自动选择按管理员模型优先级排序；个人模型仅在全局模型不可用时兜底。</div></div>
      <div class="alert info">${icon('cpu')}<div>技能名和中文标题由 AI 根据资料自动生成，无需手工填写；提交后转入后台解析，页面实时显示进度。</div></div>
    </form>`,
    footer: `<button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="create">${icon('play')}识别并生成</button>`,
  });
  const form = m.body.querySelector('#create-form');
  const filesInput = m.body.querySelector('#create-files');
  const folderInput = m.body.querySelector('#create-folder');
  const listEl = m.body.querySelector('#create-file-list');
  const picked = new Map();

  const renderPicked = () => {
    const items = [...picked.values()];
    if (!items.length) { listEl.textContent = '尚未选择文件'; return; }
    const total = items.reduce((sum, item) => sum + Number(item.file.size || 0), 0);
    listEl.innerHTML = `<strong>已选 ${items.length} 个文件 · ${(total / 1024 / 1024).toFixed(1)} MB</strong>
      <div style="max-height:110px;overflow:auto;margin-top:4px">${items.slice(0, 20).map((item) =>
        `<div class="mono" style="word-break:break-all">${esc(item.relative)}</div>`).join('')}
        ${items.length > 20 ? `<div>… 另有 ${items.length - 20} 个文件</div>` : ''}</div>`;
  };
  const addFiles = (files) => {
    for (const file of files) {
      const relative = file.webkitRelativePath || file.name || 'file';
      const key = `${relative}|${file.size}|${file.lastModified}`;
      picked.set(key, { file, relative });
    }
    renderPicked();
  };
  filesInput.addEventListener('change', () => {
    addFiles([...filesInput.files]);
    filesInput.value = '';
  });
  folderInput.addEventListener('change', () => {
    addFiles([...folderInput.files]);
    folderInput.value = '';
  });

  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) { m.close(); return; }
    if (e.target.closest('[data-act="pick-folder"]')) { folderInput.click(); return; }
    if (e.target.closest('[data-act="clear-files"]')) { picked.clear(); renderPicked(); return; }
    if (!e.target.closest('[data-act="create"]')) return;
    const sourceText = form.elements.source_text.value.trim();
    const selected = [...picked.values()];
    if (sourceText.length < 20 && !selected.length) {
      toast('请粘贴至少 20 字的故障资料，或选择日志/案例文件', 'warn');
      return;
    }
    const btn = e.target.closest('[data-act="create"]');
    btn.disabled = true;
    let createdDraftId = '';
    try {
      const modelId = form.elements.model_id.value || null;
      if (!selected.length) {
        setBusy(btn, true, '提交中…');
        const res = await api('/api/workshop/drafts?async=1', { method: 'POST', body: {
          source_text: sourceText, model_id: modelId, recipe_id: 'none',
        } });
        if (!res.job) throw new Error('后端未返回工坊任务 ID');
        selectedId = res.draft.id;
        workshopStepDraftId = res.draft.id;
        workshopStep = 2;
        toast('已提交后台解析，正在自动识别并命名', 'ok');
        m.close(); onDone && onDone();
        return;
      }
      setBusy(btn, true, '创建草稿…');
      const payload = sourceText.length >= 20 ? {
        source_text: sourceText, model_id: modelId, recipe_id: 'none',
      } : {
        name: `bbu-fault-upload-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
        title: '待自动识别故障', fault_name: '待自动识别故障', description: '',
        model_id: modelId,
      };
      const res = await api('/api/workshop/drafts?defer=1', { method: 'POST', body: payload });
      createdDraftId = res.draft.id;
      const fd = new FormData();
      selected.forEach((item) => fd.append('files', item.file, item.file.name));
      fd.append('file_paths', JSON.stringify(selected.map((item) => item.relative)));
      if (modelId) fd.append('model_id', modelId);
      fd.append('auto_cases', '1');
      if (!helpers.uploadForm) throw new Error('上传组件未就绪，请刷新页面重试');
      setBusy(btn, true, '上传文件…');
      const job = await helpers.uploadForm(
        `/api/workshop/drafts/${encodeURIComponent(createdDraftId)}/logs?auto_cases=1`,
        fd, btn, '上传文件并自动匹配案例…',
        (pct) => setBusy(btn, true, `上传 ${pct}%…`));
      if (!job) throw new Error('后端未返回工坊任务 ID');
      selectedId = createdDraftId;
      workshopStepDraftId = createdDraftId;
      workshopStep = 2;
      toast('已上传文件，正在解析资料并自动匹配案例', 'ok');
      m.close(); onDone && onDone();
    } catch (err) {
      toast(err.message, 'error');
      setBusy(btn, false);
      btn.disabled = false;
      if (createdDraftId) {
        try { await api(`/api/workshop/drafts/${encodeURIComponent(createdDraftId)}`, { method: 'DELETE' }); }
        catch { /* 保留草稿，避免误删用户资料 */ }
      }
    }
  });
}
