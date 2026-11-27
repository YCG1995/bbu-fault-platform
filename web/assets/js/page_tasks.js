// 任务列表与新建任务
import {
  api, bindActions, confirmDialog, esc, fmtDur, fmtShortDate, icon, mount, navigate, setBusy,
  state, statusBadge, toast,
} from './core.js';

const listState = { status: '', review_status: '', q: '', page: 1, page_size: 20 };
const MODE_LABELS = { fast: '快速定位', deep: '深度分析', skill: '指定技能' };
function modeLabel(mode) { return MODE_LABELS[mode] || mode || '—'; }

const TASK_TABS = [
  ['', '全部'], ['running', '运行中'], ['queued', '排队'], ['done', '已完成'],
  ['needs_data', '待补充'], ['failed', '失败'], ['cancelled', '已取消'],
];

function taskModeNav(kbMode) {
  return `<div class="kb-main-tabs" role="tablist">
    <a class="kb-main-tab ${kbMode ? '' : 'active'}" href="#/tasks" role="tab">诊断任务</a>
    <a class="kb-main-tab ${kbMode ? 'active' : ''}" href="#/tasks?tab=kb" role="tab">知识库操作</a>
  </div>`;
}

function operationKind(item) {
  if (item.kind === 'query') return '<span class="badge info">检索</span>';
  if (item.kind === 'revision') return '<span class="badge warn">修订</span>';
  return '<span class="badge">变更</span>';
}

async function renderKbOperationRecords(view, admin) {
  view.innerHTML = `${taskModeNav(true)}
    <div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let data;
  try { data = await api('/api/kb/operations?limit=200'); }
  catch (err) {
    view.innerHTML = `${taskModeNav(true)}<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return view;
  }
  const items = data.items || [];
  const renderTable = (rows) => `<div class="table-wrap"><table class="data responsive"><thead><tr>
    <th>时间</th><th>类型</th><th>动作</th><th>对象 / 问题</th><th>操作人</th><th>结果</th></tr></thead><tbody>
    ${rows.map((x) => `<tr>
      <td data-label="时间" class="tiny">${esc(x.created_at ? fmtShortDate(x.created_at) : '—')}</td>
      <td data-label="类型">${operationKind(x)}</td>
      <td data-label="动作" class="mono tiny">${esc(x.action || '—')}</td>
      <td data-label="对象 / 问题"><strong>${esc(x.kind === 'query' ? (x.query || '（空查询）') : (x.target || '—'))}</strong>
        ${x.kind === 'query' && x.target ? `<div class="tiny muted mono">${esc(x.target)}</div>` : ''}
        ${x.message ? `<div class="tiny muted">${esc(x.message)}</div>` : ''}</td>
      <td data-label="操作人">${esc(x.actor || '—')}</td>
      <td data-label="结果" class="tiny">${x.kind === 'query'
        ? `${esc(String((x.detail || {}).latency_ms || 0))} ms · ${Number((x.detail || {}).hits || 0)} 个引用${(x.detail || {}).zero_result ? ' · <span class="badge warn">零结果</span>' : ''}`
        : esc(x.kind === 'revision' ? '已写入修订链' : '已完成')}</td>
    </tr>`).join('') || '<tr><td colspan="6" class="empty small">暂无知识库操作记录</td></tr>'}
  </tbody></table></div>`;
  view.innerHTML = `${taskModeNav(true)}
    <div class="card">
      <div class="toolbar" style="padding:12px 14px">
        <div class="search">${icon('search')}<input class="input" id="kb-op-search" placeholder="搜索问题 / 对象 / 操作人"></div>
        <span class="small muted" id="kb-op-total">共 ${items.length} 条</span>
        ${admin ? '<a class="btn btn-sm" href="#/kb/docs?tab=audit" style="margin-left:auto">完整审计 →</a>' : ''}
        <button class="btn btn-sm" data-act="reload-kb-ops">${icon('refresh')}刷新</button>
      </div>
      <div class="card-body tight" id="kb-op-table">${renderTable(items)}</div>
      <div class="tiny muted" style="padding:0 14px 12px">${admin ? '管理员可见全库检索日志、案例修订与审计；' : '仅显示你本人发起的知识库检索；'}检索结果会写入任务记录，变更有哈希链可追溯。</div>
    </div>`;
  const search = view.querySelector('#kb-op-search');
  const applyFilter = () => {
    const q = search.value.trim().toLowerCase();
    const rows = q ? items.filter((x) => JSON.stringify(x).toLowerCase().includes(q)) : items;
    view.querySelector('#kb-op-table').innerHTML = renderTable(rows);
    view.querySelector('#kb-op-total').textContent = `共 ${rows.length} 条`;
  };
  search?.addEventListener('input', applyFilter);
  bindActions(view, { 'reload-kb-ops': () => renderKbOperationRecords(view, admin) });
  return view;
}

export async function renderTaskList() {
  const kbMode = new URLSearchParams(location.hash.split('?')[1] || '').get('tab') === 'kb';
  if (kbMode) {
    const view = mount('任务记录', '知识库检索与变更操作也会进入这里；管理员可见全库审计',
      `<a class="btn" href="#/kb">${icon('search')}去故障问答</a>`);
    return renderKbOperationRecords(view, state.user?.role === 'admin');
  }
  const view = mount('任务记录', '所有诊断任务的执行状态与结论',
    `<a class="btn btn-primary" href="#/tasks/new">${icon('plus')}新建诊断</a>`);
  const tabs = state.user?.role === 'reviewer'
    ? [['', '全部'], ['pending', '待我复核'], ...TASK_TABS.slice(1)]
    : TASK_TABS;
  view.innerHTML = `
    ${taskModeNav(false)}
    <div class="card">
      <div class="toolbar" style="padding:12px 14px">
        <div class="search">${icon('search')}<input class="input" id="task-search" placeholder="搜索任务 ID / 标题 / 故障描述" value="${esc(listState.q)}"></div>
        <span class="small muted" id="task-total"></span>
        <button class="btn btn-sm" style="margin-left:auto" data-act="reload">${icon('refresh')}刷新</button>
      </div>
      <div class="tabs" id="task-tabs">
        ${tabs.map(([key, label]) => `<button data-status="${key}" class="${(listState.status === key || (key === 'pending' && listState.review_status === 'pending')) ? 'active' : ''}">${label}</button>`).join('')}
      </div>
      <div class="card-body tight" id="task-table">${icon('refresh')} 加载中…</div>
      <div class="pager" id="task-pager"></div>
    </div>`;

  const tableEl = view.querySelector('#task-table');
  const pagerEl = view.querySelector('#task-pager');
  const totalEl = view.querySelector('#task-total');
  let timer = null;

  async function load() {
    tableEl.innerHTML = `<div class="empty"><div class="spin" style="border-color:#cbd5e1;border-top-color:#0f766e"></div><p>加载任务…</p></div>`;
    try {
      const params = new URLSearchParams({ page: listState.page, page_size: listState.page_size });
      if (listState.status) params.set('status', listState.status);
      if (listState.review_status) params.set('review_status', listState.review_status);
      if (listState.q.trim()) params.set('q', listState.q.trim());
      const data = await api(`/api/tasks?${params}`);
      totalEl.textContent = `共 ${data.total} 个任务`;
      if (!data.items.length) {
        tableEl.innerHTML = `<div class="empty">${icon('list')}<h3>没有符合条件的任务</h3>
          <p>${listState.q || listState.status ? '换个筛选条件，或新建一次诊断。' : '上传日志包，平台会并行调度子技能。'}</p>
          <button class="btn btn-primary" data-act="new-task">${icon('plus')}新建诊断</button></div>`;
      } else {
        tableEl.innerHTML = `<table class="data responsive"><thead><tr>
          <th>任务</th><th>状态</th><th>根因</th><th>置信度</th><th>耗时</th><th>提交时间</th></tr></thead>
          <tbody>${data.items.map((t) => `
            <tr class="row-hover" data-href="#/tasks/${encodeURIComponent(t.id)}">
              <td data-label="任务"><div style="font-weight:600">${esc(t.title)} <span class="badge muted" style="font-weight:400">${esc(modeLabel(t.mode))}</span></div>
                  <div class="mono muted tiny">${esc(t.id)}</div></td>
              <td data-label="状态">${statusBadge(t.status)}</td>
              <td data-label="根因"><div>${t.verdict?.primary && t.verdict.primary !== 'NONE'
                ? `<span class="mono">${esc(t.verdict.primary)}</span>${t.root_cause_name ? `<span class="tiny muted" style="margin-left:6px">${esc(t.root_cause_name)}</span>` : ''}`
                : '<span class="muted">—</span>'}</div></td>
              <td data-label="置信度">${t.confidence ? `${esc({ high: '高', medium: '中', low: '低' }[t.confidence] || t.confidence)}` : '<span class="muted">—</span>'}</td>
              <td data-label="耗时" class="nowrap">${fmtDur(t.duration_sec)}</td>
              <td data-label="提交时间" class="muted nowrap">${fmtShortDate(t.created_at)}</td>
            </tr>`).join('')}</tbody></table>`;
        tableEl.querySelectorAll('[data-href]').forEach((row) => {
          row.addEventListener('click', () => { location.hash = row.dataset.href; });
        });
      }
      const pages = Math.max(1, Math.ceil(data.total / listState.page_size));
      pagerEl.innerHTML = `<span class="small muted">第 ${listState.page} / ${pages} 页</span>
        <button class="btn btn-sm" data-page="${listState.page - 1}" ${listState.page <= 1 ? 'disabled' : ''}>上一页</button>
        <button class="btn btn-sm" data-page="${listState.page + 1}" ${listState.page >= pages ? 'disabled' : ''}>下一页</button>`;
      pagerEl.querySelectorAll('[data-page]').forEach((btn) => {
        btn.addEventListener('click', () => { listState.page = Number(btn.dataset.page); load(); });
      });
    } catch (err) {
      tableEl.innerHTML = `<div class="alert danger" style="margin:16px">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    }
  }

  bindActions(view, {
    'new-task': () => { location.hash = '#/tasks/new'; },
    reload: () => load(),
  });
  view.querySelector('#task-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-status]');
    if (!btn) return;
    const key = btn.dataset.status;
    listState.review_status = key === 'pending' ? 'pending' : '';
    listState.status = key === 'pending' ? '' : key;
    listState.page = 1;
    view.querySelectorAll('#task-tabs button').forEach((b) => b.classList.toggle('active', b === btn));
    load();
  });
  const search = view.querySelector('#task-search');
  search.addEventListener('input', () => {
    listState.q = search.value;
    listState.page = 1;
    clearTimeout(timer);
    timer = setTimeout(load, 350);
  });

  await load();
  state.cleanup = () => clearTimeout(timer);
  return view;
}

export async function renderNewTask() {
  const view = mount('新建诊断', '上传日志包并描述现场现象，平台会盘点日志并按诊断模式规划执行',
    `<span id="draft-status" class="small muted"></span><a class="btn" href="#/tasks">${icon('chevron')}返回任务列表</a>`);
  const isAdmin = state.user?.role === 'admin';
  let models = [];
  let restored = null;
  let skills = [];
  try {
    const data = await api('/api/models');
    models = (data.items || []).filter((m) => m.enabled);
  } catch { /* 工程师也能建任务，模型列表失败不阻塞 */ }
  try {
    const data = await api('/api/skills');
    skills = (data.items || []).filter((s) => ['ready', 'degraded'].includes(s.status));
  } catch { /* 技能列表失败不阻塞 */ }
  let preselected = '';
  try {
    preselected = sessionStorage.getItem('bbu.diagnose_skill') || '';
    if (preselected) sessionStorage.removeItem('bbu.diagnose_skill');
  } catch { /* ignore */ }
  try { restored = (await api('/api/task-drafts/current')).draft || null; } catch { restored = null; }
  const initialMode = preselected ? 'skill' : (['fast', 'deep', 'skill'].includes(restored?.mode) ? restored.mode : 'fast');
  const initialSkill = preselected || restored?.skill_name || '';

  const initFiles = (restored?.files || []).map((f, i) => ({
    key: `d${i}`, draftFile: f, file: null, name: f.name, size: f.size,
    status: 'done', progress: 100, xhr: null, error: '', speed: 0,
  }));
  view.innerHTML = `
    <div class="task-create-layout">
      <form class="card" id="task-form">
        <div class="card-head"><h2>现场信息</h2><span class="sub">AI 会根据描述和日志盘点自动生成任务标题</span></div>
        <div class="card-body">
          ${restored ? `<div class="alert info" style="margin-bottom:14px">${icon('info')}<div>已恢复 ${fmtShortDate(restored.updated_at)} 保存的草稿${initFiles.length ? `，包含 ${initFiles.length} 个已上传日志包` : ''}。可直接继续编辑或提交。</div></div>` : ''}
          <div class="form-row">
            <label class="label" for="f-desc">故障描述 <span class="hint">告警码、单板型号、发生时间、已做过的操作越具体越好；标题将在盘点后自动生成</span></label>
            <textarea class="textarea" id="f-desc" name="description" rows="6"
              placeholder="例如：今早 8:20 起 BBU 上报 CLK032 时钟告警，PLL 无法锁定，GPS 天线检查无异常；设备为 ITBBU，室外抱杆安装……">${esc(restored?.description || '')}</textarea>
          </div>
          <div class="form-row">
            <label class="label">日志包 <span class="hint">支持 zip / tar / tar.gz / 7z / gz / bz2 / xz / zst，可多选；单包上限 ${esc(state.publicConfig.max_upload_mb || 512)} MB</span></label>
            <div class="dropzone" id="dropzone">
              ${icon('upload')}
              <h3>拖拽日志包到这里，或点击选择文件</h3>
              <p>选择后立即在后台校验并上传；刷新页面后已上传文件仍可恢复</p>
              <input type="file" id="file-input" multiple accept=".zip,.tar,.tar.gz,.tgz,.7z,.gz,.bz2,.xz,.zst,application/zip,application/gzip" hidden>
            </div>
            <div class="upload-summary" id="upload-summary" hidden></div>
            <div class="file-list" id="file-list"></div>
          </div>
          <div class="form-row">
            <label class="label">诊断模式</label>
            <div class="grid grid-3" id="mode-group">
              <label class="radio-card ${initialMode === 'fast' ? 'active' : ''}"><input type="radio" name="mode" value="fast" ${initialMode === 'fast' ? 'checked' : ''}>
                <div><strong>快速定位</strong><span>命中规则先行，适合现场抢修；证据不足时明确提示补采</span></div></label>
              <label class="radio-card ${initialMode === 'deep' ? 'active' : ''}"><input type="radio" name="mode" value="deep" ${initialMode === 'deep' ? 'checked' : ''}>
                <div><strong>深度分析</strong><span>扩大检索与交叉验证，耗时略长，适合复盘和根因分析</span></div></label>
              <label class="radio-card ${initialMode === 'skill' ? 'active' : ''}"><input type="radio" name="mode" value="skill" ${initialMode === 'skill' ? 'checked' : ''}>
                <div><strong>指定技能</strong><span>只执行选定技能，不触发 AI 扩选与补规划，适合定向复核</span></div></label>
            </div>
          </div>
          <div class="form-row" id="skill-mode-box" ${initialMode === 'skill' ? '' : 'hidden'}>
            <label class="label" for="f-skill">选择技能 <span class="hint">指定技能模式必须且只能选择一个</span></label>
            <select class="select" id="f-skill" name="skill_name" ${skills.length ? '' : 'disabled'}>
              <option value="">请选择可用技能</option>
              ${skills.map((s) => `<option value="${esc(s.name)}" ${initialSkill === s.name ? 'selected' : ''}>${esc(s.title || s.name)} · v${esc(s.version)}</option>`).join('')}
            </select>
            <div class="help">${skills.length ? '只执行该技能，不自动追加其他技能；仍会生成证据、报告与执行轨迹。' : '当前没有 ready / degraded 状态的技能。'}</div>
          </div>
          ${models.length ? `<div class="form-row">
            <label class="label" for="f-model">本次使用的模型 <span class="hint">留空则按角色路由自动选择</span></label>
            <select class="select" id="f-model" name="model_id">
              <option value="">自动选择（按角色与优先级）</option>
              ${models.map((m) => `<option value="${esc(m.id)}" ${String(restored?.model_override || '') === String(m.id) ? 'selected' : ''}>${esc(m.name)} · ${esc(m.model)}（${esc(m.provider)}）</option>`).join('')}
            </select>
          </div>` : `<div class="alert info" style="margin-bottom:16px">${icon('info')}<div>还没有配置模型。平台会用确定性路径完成诊断；到「模型接入」添加 OpenAI 兼容或 Anthropic 服务后可启用 AI 研判。</div></div>`}
          ${isAdmin ? `<label class="check"><input type="checkbox" name="include_canary" value="1" ${restored?.include_canary ? 'checked' : ''}> 包含灰度技能（只在对照验证时勾选，普通任务默认只用稳定版）</label>` : ''}
          <div class="between" style="margin-top:18px">
            <span class="small muted" id="submit-hint">提交后自动进入队列，可在任务详情实时查看进度</span>
            <div class="inline">
              ${restored ? `<button class="btn btn-ghost" type="button" data-act="discard-draft">放弃草稿</button>` : ''}
              <button class="btn btn-primary" type="submit" id="submit-btn">${icon('play')}开始诊断</button>
            </div>
          </div>
        </div>
      </form>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>平台会做什么</h2></div><div class="card-body">
          <div class="kv">
            <div class="k">1. 盘点</div><div class="v">识别日志类型、文件名、告警码和快速签名</div>
            <div class="k">2. AI 规划</div><div class="v">模型生成动态波次计划，首波并行；证据不足最多补规划一次</div>
            <div class="k">3. 按计划执行</div><div class="v">波次内并行、波次间按证据门槛推进，各自产出证据与初判</div>
            <div class="k">4. 仲裁</div><div class="v">仅在结论冲突时调用一次模型，否则按评分与证据裁决</div>
            <div class="k">5. 报告</div><div class="v">确定性骨架 + 有界研判，逐条引用证据编号</div>
          </div>
        </div></div>
        <div class="card"><div class="card-head"><h2>上传建议</h2></div><div class="card-body small muted">
          <p style="margin-top:0">优先上传故障时间窗前后 30 分钟的日志，包含：</p>
          <ul style="padding-left:18px;margin:0">
            <li>时钟/告警/运行日志（clockstatuslog、alarmlog 等）</li>
            <li>单板与链路状态、光模块读数</li>
            <li>近期复位、升级、配置变更记录</li>
          </ul>
          <p style="margin-bottom:0">平台不会把原始日志发送给模型，只发送抽取后的证据片段与样例行。</p>
        </div></div>
      </div>
    </div>`;

  const form = view.querySelector('#task-form');
  const dropzone = view.querySelector('#dropzone');
  const fileInput = view.querySelector('#file-input');
  const fileList = view.querySelector('#file-list');
  const uploadSummary = view.querySelector('#upload-summary');
  const submit = view.querySelector('#submit-btn');
  const draftStatus = document.getElementById('draft-status');
  let files = initFiles;
  let draftId = restored?.id || null;
  let draftPromise = null;
  let draftTimer = null;
  let dirty = false;
  let submitting = false;

  function setDraftStatus(text) {
    if (draftStatus) draftStatus.textContent = text || '';
  }

  function draftPayload() {
    return {
      title: '',
      description: form.elements.description.value.trim(),
      mode: form.elements.mode.value,
      skill_name: form.elements.mode.value === 'skill' ? (form.elements.skill_name?.value || '') : '',
      model_override: form.elements.model_id?.value || null,
      include_canary: !!form.elements.include_canary?.checked,
    };
  }

  async function ensureDraft() {
    if (draftId) return draftId;
    if (!draftPromise) {
      draftPromise = api('/api/task-drafts', { method: 'POST', body: draftPayload() })
        .then((data) => {
          draftId = data.draft.id;
          setDraftStatus('草稿已创建');
          return draftId;
        })
        .finally(() => { draftPromise = null; });
    }
    return draftPromise;
  }

  async function saveDraft(force = false) {
    if (submitting && !force) return;
    clearTimeout(draftTimer);
    try {
      await ensureDraft();
      const data = await api(`/api/task-drafts/${encodeURIComponent(draftId)}`, {
        method: 'PUT', body: draftPayload(),
      });
      dirty = false;
      setDraftStatus(`草稿已保存 ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`);
      return data.draft;
    } catch (err) {
      setDraftStatus('草稿保存失败，请检查登录状态');
      throw err;
    }
  }

  function scheduleSave() {
    dirty = true;
    setDraftStatus('正在保存草稿…');
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => { saveDraft().catch(() => {}); }, 700);
  }

  function renderFiles() {
    fileList.innerHTML = files.map((it, i) => {
      const pct = Math.round(it.progress || 0);
      const size = `${(it.size / 1024 / 1024).toFixed(2)} MB`;
      const speed = it.status === 'uploading' && it.speed
        ? `${(it.speed / 1024 / 1024).toFixed(2)} MB/s` : '';
      const statusText = it.status === 'done' ? '已上传'
        : it.status === 'uploading' ? `${pct}% · ${speed}`
        : it.status === 'cancelled' ? '已取消'
        : it.status === 'error' ? '上传失败' : '等待上传';
      return `<div class="file-item upload-row ${it.status === 'error' ? 'error' : ''}">
        ${icon('file')}
        <div class="upload-main">
          <div class="upload-line"><span class="name">${esc(it.name)}</span>
            <span class="muted tiny">${size} · ${esc(statusText)}</span></div>
          ${it.status === 'uploading' ? `<div class="upload-progress"><div style="width:${pct}%"></div></div>` : ''}
          ${it.error ? `<div class="tiny danger-text">${esc(it.error)}</div>` : ''}
        </div>
        <div class="inline" style="gap:4px">
          ${it.status === 'uploading' ? `<button type="button" class="btn btn-sm" data-cancel="${i}">取消</button>` : ''}
          ${(it.status === 'error' || it.status === 'cancelled') && it.file ? `<button type="button" class="btn btn-sm" data-retry="${i}">重试</button>` : ''}
          <button type="button" class="icon-btn" data-remove="${i}" aria-label="移除 ${esc(it.name)}">${icon('x')}</button>
        </div>
      </div>`;
    }).join('');
    fileList.querySelectorAll('[data-remove]').forEach((btn) => {
      btn.addEventListener('click', () => removeFile(Number(btn.dataset.remove)));
    });
    fileList.querySelectorAll('[data-cancel]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const it = files[Number(btn.dataset.cancel)];
        if (it?.xhr) it.xhr.abort();
      });
    });
    fileList.querySelectorAll('[data-retry]').forEach((btn) => {
      btn.addEventListener('click', () => uploadItem(files[Number(btn.dataset.retry)]));
    });
    const total = files.reduce((n, it) => n + (it.size || 0), 0);
    const loaded = files.reduce((n, it) => n + (it.status === 'done' ? (it.size || 0) : (it.size || 0) * (it.progress || 0) / 100), 0);
    const pct = total ? Math.round(loaded / total * 100) : 0;
    uploadSummary.hidden = !files.length;
    uploadSummary.innerHTML = files.length
      ? `<div class="between"><span>总进度 ${pct}%</span><span class="muted tiny">${files.filter((f) => f.status === 'done').length}/${files.length} 个已上传</span></div>
         <div class="upload-progress"><div style="width:${pct}%"></div></div>` : '';
  }

  async function uploadItem(it) {
    if (!it || !it.file || it.status === 'uploading') return;
    try {
      await ensureDraft();
    } catch (err) { it.error = err.message; it.status = 'error'; renderFiles(); return; }
    const xhr = new XMLHttpRequest();
    it.xhr = xhr;
    it.status = 'uploading';
    it.progress = 0;
    it.error = '';
    const started = performance.now();
    const fd = new FormData();
    fd.append('file', it.file, it.name);
    xhr.open('POST', `/api/task-drafts/${encodeURIComponent(draftId)}/files`);
    xhr.setRequestHeader('X-CSRF', state.csrf || '');
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      it.progress = Math.min(99, e.loaded / e.total * 100);
      const seconds = Math.max(0.2, (performance.now() - started) / 1000);
      it.speed = e.loaded / seconds;
      renderFiles();
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300 && data.file) {
        it.draftFile = data.file;
        it.status = 'done';
        it.progress = 100;
        it.xhr = null;
        setDraftStatus('日志包已上传');
        renderFiles();
      } else {
        it.status = 'error';
        it.error = data.detail || data.message || `上传失败（HTTP ${xhr.status}）`;
        it.xhr = null;
        renderFiles();
      }
    };
    xhr.onerror = () => {
      it.status = 'error'; it.error = '网络中断，请重试'; it.xhr = null; renderFiles();
    };
    xhr.onabort = () => {
      if (it.status === 'uploading') { it.status = 'cancelled'; it.xhr = null; renderFiles(); }
    };
    xhr.send(fd);
  }

  async function removeFile(index) {
    const it = files[index];
    if (!it) return;
    if (it.xhr) { it.xhr.abort(); it.xhr = null; }
    if (it.draftFile && draftId) {
      try { await api(`/api/task-drafts/${encodeURIComponent(draftId)}/files/${encodeURIComponent(it.draftFile.id)}`, { method: 'DELETE' }); }
      catch (err) { toast(err.message, 'error'); return; }
    }
    files.splice(index, 1);
    renderFiles();
    if (draftId) scheduleSave();
  }

  function addFiles(list) {
    const max = (state.publicConfig.max_upload_mb || 512) * 1024 * 1024;
    for (const f of list) {
      if (files.length >= 20) { toast('单个草稿最多 20 个日志包', 'warn'); break; }
      if (!/\.(zip|tar|tar\.gz|tgz|7z|gz|bz2|xz|zst)$/i.test(f.name)) { toast(`${f.name} 不是支持的压缩格式`, 'warn'); continue; }
      if (f.size > max) { toast(`${f.name} 超过单包上限`, 'warn'); continue; }
      if (!f.size) { toast(`${f.name} 是空文件`, 'warn'); continue; }
      if (files.some((x) => x.name === f.name && x.size === f.size)) continue;
      const item = { key: `f${Date.now()}-${files.length}`, file: f, draftFile: null,
                     name: f.name, size: f.size, status: 'pending', progress: 0,
                     xhr: null, error: '', speed: 0 };
      files.push(item);
      uploadItem(item);
    }
    renderFiles();
    scheduleSave();
  }

  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('over'); });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('over'));
  dropzone.addEventListener('drop', (e) => {
    e.preventDefault(); dropzone.classList.remove('over');
    addFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });

  view.querySelector('#mode-group').addEventListener('change', () => {
    view.querySelectorAll('#mode-group .radio-card').forEach((card) => {
      card.classList.toggle('active', card.querySelector('input').checked);
    });
    const box = view.querySelector('#skill-mode-box');
    const skill = form.elements.skill_name;
    if (box) box.hidden = form.elements.mode.value !== 'skill';
    if (form.elements.mode.value !== 'skill' && skill) skill.value = '';
    scheduleSave();
  });
  ['title', 'description', 'model_id', 'include_canary', 'skill_name'].forEach((name) => {
    const el = form.elements[name];
    if (!el) return;
    el.addEventListener('input', scheduleSave);
    el.addEventListener('change', scheduleSave);
  });

  const beforeUnload = (e) => {
    if (submitting) return;
    if (!dirty && !files.some((f) => f.status === 'done' || f.status === 'uploading')) return;
    e.preventDefault();
    e.returnValue = '';
  };
  window.addEventListener('beforeunload', beforeUnload);
  state.cleanup = () => {
    window.removeEventListener('beforeunload', beforeUnload);
    clearTimeout(draftTimer);
  };

  bindActions(view, {
    back: () => { location.hash = '#/tasks'; },
    'discard-draft': async () => {
      if (!await confirmDialog('放弃当前草稿并删除已上传日志包？此操作不可恢复。', { danger: true, title: '放弃草稿' })) return;
      try {
        if (draftId) await api(`/api/task-drafts/${encodeURIComponent(draftId)}`, { method: 'DELETE' });
        dirty = false; draftId = null; files = []; renderFiles();
        location.hash = '#/tasks';
      } catch (err) { toast(err.message, 'error'); }
    },
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (submitting) return;
    const description = form.elements.description.value.trim();
    if (!description) { toast('请填写故障描述，至少包括现象和告警信息', 'warn'); return; }
    const mode = form.elements.mode.value;
    const skillName = form.elements.skill_name?.value || '';
    if (mode === 'skill' && !skillName) { toast('指定技能模式必须选择一个技能', 'warn'); return; }
    if (!files.length) { toast('请至少上传一个日志包', 'warn'); return; }
    if (files.some((f) => f.status === 'uploading')) { toast('日志包仍在上传，请等待完成或取消后重试', 'warn'); return; }
    if (files.some((f) => f.status !== 'done')) { toast('有日志包未上传成功，请重试或移除后再提交', 'warn'); return; }
    submitting = true;
    setBusy(submit, true, '正在提交…');
    try {
      await saveDraft(true);
      const fd = new FormData();
      fd.append('description', description);
      fd.append('mode', mode);
      if (mode === 'skill') fd.append('skill_name', skillName);
      fd.append('draft_id', draftId);
      const modelId = form.elements.model_id?.value;
      if (modelId) fd.append('model_id', modelId);
      if (form.elements.include_canary?.checked) fd.append('include_canary', '1');
      const data = await api('/api/tasks', { method: 'POST', body: fd });
      dirty = false;
      toast('任务已创建，正在进入诊断队列', 'ok');
      location.hash = `#/tasks/${encodeURIComponent(data.id)}`;
    } catch (err) {
      submitting = false;
      toast(err.message, 'error');
      setBusy(submit, false);
    }
  });

  renderFiles();
  if (restored) setDraftStatus(`已恢复草稿 ${fmtShortDate(restored.updated_at)}`);
  else setDraftStatus('编辑后自动保存草稿');
}
