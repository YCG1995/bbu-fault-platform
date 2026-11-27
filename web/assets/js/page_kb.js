// 故障知识库：检索问答、引用案例、文档库、导入任务与审核队列
import {
  api, bindActions, confirmDialog, emptyState, esc, fmtDate, icon, mount, modal,
  setBusy, state, toast,
} from './core.js';

const STATUS_LABEL = { active: '生效', reviewing: '待审核', disabled: '已停用', deleted: '已删除' };
const STATUS_BADGE = { active: 'ok', reviewing: 'warn', disabled: '', deleted: 'danger' };
const JOB_LABEL = {
  queued: '排队中', processing: '解析中', completed: '已完成', partial: '部分完成',
  failed: '失败', cancelled: '已取消', cancelling: '取消中',
};

function statusBadge(status) {
  return `<span class="badge ${STATUS_BADGE[status] || ''}">${esc(STATUS_LABEL[status] || status || '—')}</span>`;
}

function jobBadge(status) {
  const cls = status === 'completed' ? 'ok' : status === 'failed' ? 'danger'
    : status === 'partial' ? 'warn' : '';
  return `<span class="badge ${cls}">${esc(JOB_LABEL[status] || status || '—')}</span>`;
}

function scoreText(value) {
  const n = Number(value || 0);
  return n > 1 ? `${Math.round(n)} 分` : `${Math.round(n * 100)}%`;
}

function versionLabel(value) {
  if (typeof value === 'string') return value;
  return value?.version ? `v${value.version}` : '';
}

function citationChips(citations, caseMap) {
  return (citations || []).map((citation) => {
    const item = caseMap[citation];
    const href = item ? `#/kb/docs/${encodeURIComponent(item.doc_id)}` : '';
    return href
      ? `<a class="kb-cite" href="${href}" title="${esc(item.title)}">${esc(citation)}</a>`
      : `<span class="kb-cite muted">${esc(citation)}</span>`;
  }).join(' ');
}

function renderResult(data) {
  const cases = data.cases || [];
  const caseMap = Object.fromEntries(cases.map((c) => [c.citation, c]));
  const noAnswer = data.no_answer;
  const parts = [];
  if (noAnswer) {
    parts.push(`<div class="alert warn">${icon('alert')}<div><strong>未找到足够相关的历史案例。</strong>
      <div class="tiny" style="margin-top:4px">${esc(data.answer || '')}</div></div></div>`);
  } else {
    parts.push(`<div class="card kb-answer">
      <div class="card-head"><h2>知识库回答</h2>
        <span class="badge ${data.degraded ? 'warn' : 'ok'}">${data.degraded ? '本地检索摘要' : '模型综合'}</span>
        <span class="tiny muted" style="margin-left:auto">${data.model ? `模型 ${esc(data.model)}` : '未调用模型'} · 置信度 ${scoreText(data.confidence)}</span>
      </div>
      <div class="card-body">
        <div class="kb-answer-text">${esc(data.answer || '').replace(/\n/g, '<br>')}</div>
        ${(data.conclusions || []).length ? `<div class="kb-subhead">结论</div>
          <ul class="kb-points">${data.conclusions.map((item) => `<li>${esc(item.text)} ${citationChips(item.citations, caseMap)}</li>`).join('')}</ul>` : ''}
        ${(data.steps || []).length ? `<div class="kb-subhead">排查/处理步骤</div>
          <ol class="kb-points">${data.steps.map((item) => `<li>${esc(item)}</li>`).join('')}</ol>` : ''}
        ${(data.cautions || []).length ? `<div class="kb-subhead">风险提示</div>
          <ul class="kb-points">${data.cautions.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}
      </div>
    </div>`);
  }
  if (cases.length) {
    parts.push(`<div class="card" style="margin-top:14px">
      <div class="card-head"><h2>引用案例</h2><span class="sub">共 ${cases.length} 个；点击打开完整记录</span></div>
      <div class="card-body kb-case-list">
        ${cases.map((c) => `<a class="kb-case-card" href="#/kb/docs/${encodeURIComponent(c.doc_id)}">
          <div class="inline"><span class="kb-cite">${esc(c.citation)}</span>
            <strong>${esc(c.title)}</strong><span class="badge info" style="margin-left:auto">${scoreText(c.score)}</span></div>
          <div class="tiny muted" style="margin-top:6px">${esc((c.boards || []).join('、') || '未识别板卡')} · ${esc((c.domains || []).join('、') || '未识别领域')} ${c.versions?.length ? '· ' + esc(c.versions.join('、')) : ''}</div>
          <div class="kb-snippet"><span>现象</span>${esc(c.symptom || '—')}</div>
          <div class="kb-snippet"><span>原因</span>${esc(c.cause || '—')}</div>
          <div class="kb-snippet"><span>方案</span>${esc(c.solution || '—')}</div>
        </a>`).join('')}
      </div>
    </div>`);
  }
  if (data.analysis) {
    const a = data.analysis;
    parts.push(`<details class="card kb-analysis"><summary>本次检索如何理解问题</summary>
      <div class="card-body tiny muted">
        意图：${esc(a.intent || '未识别')}；板卡：${esc((a.boards || []).join('、') || '—')}；
        领域：${esc((a.domains || []).join('、') || '—')}；版本：${esc((a.versions || []).join('、') || '—')}；
        扩展词：${esc((a.expansions || []).join('、') || '—')}
      </div></details>`);
  }
  return parts.join('');
}

async function loadFacets() {
  try { return await api('/api/kb/facets'); } catch { return { boards: [], domains: [] }; }
}

function boardOptions(facets) {
  return `<option value="">全部板卡</option>${(facets.boards || []).map(
    (x) => `<option value="${esc(x.value)}">${esc(x.value)}（${x.count}）</option>`).join('')}`;
}

function domainOptions(facets) {
  return `<option value="">全部领域</option>${(facets.domains || []).map(
    (x) => `<option value="${esc(x.value)}">${esc(x.value)}（${x.count}）</option>`).join('')}`;
}

export async function renderKb() {
  const view = mount('故障知识库', '先查现场历史案例；结论必须能回到具体文档和案例',
    `<a class="btn" href="#/kb/docs">${icon('inbox')} 文档库与导入</a>`);
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let status;
  let facets;
  try {
    status = (await api('/api/kb/status')).status;
    facets = await loadFacets();
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  if (!status.enabled) {
    view.innerHTML = `<div class="alert warn">${icon('alert')}<div>故障知识库已在运行时配置中关闭。</div></div>`;
    return;
  }
  const empty = Number(status.documents || 0) === 0;
  const recent = empty ? [] : (await api('/api/kb/docs?size=6&status=active')).items || [];
  view.innerHTML = `
    <div class="grid grid-4 kb-stats">
      <div class="card stat"><div><div class="stat-val">${status.documents || 0}</div>
        <div class="stat-label">生效文档</div><div class="stat-note">${status.cases || 0} 个可引用案例</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.index?.terms || 0}</div>
        <div class="stat-label">索引词项</div><div class="stat-note">重建 ${status.index?.build_ms || 0} ms · ${esc(status.index?.status || 'empty')}</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.drafts || 0}</div>
        <div class="stat-label">待审核</div><div class="stat-note">工程师导入后进入审核队列</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.top_k || 6}</div>
        <div class="stat-label">默认引用上限</div><div class="stat-note">${status.index?.bytes ? `${Math.round(status.index.bytes / 1024)} KB 索引` : '无模型也可检索'}</div></div></div>
    </div>
    <div class="card kb-ask-card" style="margin-top:14px">
      <div class="card-head"><h2>查故障</h2><span class="sub">描述现象即可；越具体越容易命中</span></div>
      <div class="card-body">
        ${empty ? `<div class="alert info">${icon('info')}<div>知识库还没有数据。管理员可以到
          <a href="#/kb/docs">文档库</a> 导入 CSV/TXT/MD，或先载入示例数据。</div></div>` : ''}
        <div class="form-row"><label for="kb-query">故障现象 / 问题</label>
          <textarea class="input kb-query" id="kb-query" rows="4" maxlength="2000"
            placeholder="例如：VBPE2 单板每 2~3 分钟复位一次，V4.30.20 升级后出现，网管有看门狗告警"></textarea>
          <div class="tiny muted">建议写：板卡型号 + 软件/逻辑版本 + 告警码 + 现象 + 已做过的操作。</div></div>
        <div class="form-row"><label>可选过滤</label>
          <div class="inline kb-filters">
            <select class="input" id="kb-board">${boardOptions(facets)}</select>
            <select class="input" id="kb-domain">${domainOptions(facets)}</select>
            <button class="btn btn-primary" type="button" data-act="ask">${icon('search')} 检索并回答</button>
            <button class="btn" type="button" data-act="search">只检索案例</button>
          </div>
        </div>
        <div class="inline kb-examples">
          <span class="tiny muted">示例：</span>
          ${['看门狗复位', '1588 时钟失锁', 'SSD 只读', 'SFP 收光低', 'EPLD 升级失败'].map(
            (x) => `<button class="btn btn-sm btn-ghost" type="button" data-act="example" data-text="${esc(x)}">${esc(x)}</button>`).join('')}
        </div>
      </div>
    </div>
    <div id="kb-result" class="kb-result">${empty ? emptyState('inbox', '知识库还是空的', '导入现场故障问答后，这里会显示带引用的回答。',
      state.user?.role === 'admin' ? '<button class="btn btn-sm" data-act="sample">载入示例数据</button>' : '') : ''}</div>
    ${recent.length ? `<div class="card" style="margin-top:14px">
      <div class="card-head"><h2>最近入库</h2><a class="tiny" href="#/kb/docs" style="margin-left:auto">查看全部</a></div>
      <div class="card-body kb-recent">${recent.map((d) => `<a class="kb-recent-row" href="#/kb/docs/${encodeURIComponent(d.id)}">
        <span>${esc(d.title)}</span><span class="tiny muted">${esc((d.boards || []).join('、') || '—')} · ${d.case_count || 0} 案例</span>
      </a>`).join('')}</div></div>` : ''}
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>回答边界：</strong>知识库回答只基于检索到的历史案例；无模型时显示本地摘要，有模型时也保留原文引用。
      历史案例不是厂商正式公告，执行前需结合当前版本、现场日志和变更记录复核。
    </div></div>`;

  const run = async (endpoint, button) => {
    const query = view.querySelector('#kb-query').value.trim();
    if (!query) { toast('请先描述故障现象', 'error'); return; }
    const filters = {};
    const board = view.querySelector('#kb-board').value;
    const domain = view.querySelector('#kb-domain').value;
    if (board) filters.board = board;
    if (domain) filters.domain = domain;
    setBusy(button, true, '检索中…');
    try {
      const data = endpoint === 'chat'
        ? await api('/api/kb/chat', { method: 'POST', body: { query, filters }, timeoutMs: 90000 })
        : await api(`/api/kb/search?q=${encodeURIComponent(query)}&board=${encodeURIComponent(board)}&domain=${encodeURIComponent(domain)}&limit=10`);
      if (endpoint === 'search') {
        const items = data.items || [];
        view.querySelector('#kb-result').innerHTML = items.length
          ? `<div class="card"><div class="card-head"><h2>检索结果</h2><span class="sub">${data.no_answer ? '低于拒答阈值，仅供参考' : `${items.length} 个案例`} · ${data.latency_ms || 0} ms</span></div>
            <div class="card-body kb-case-list">${items.map((c, i) => `<a class="kb-case-card" href="#/kb/docs/${encodeURIComponent(c.doc_id)}">
              <div class="inline"><span class="kb-cite">K${i + 1}</span><strong>${esc(c.title)}</strong><span class="badge info" style="margin-left:auto">${scoreText(c.score)}</span></div>
              <div class="kb-snippet"><span>现象</span>${esc(c.symptom || '—')}</div>
              <div class="kb-snippet"><span>原因</span>${esc(c.cause || '—')}</div>
              <div class="kb-snippet"><span>方案</span>${esc(c.solution || '—')}</div></a>`).join('')}</div></div>`
          : `<div class="alert warn">${icon('alert')}<div>${esc(data.reason || '没有命中案例')}</div></div>`;
      } else {
        view.querySelector('#kb-result').innerHTML = renderResult(data);
      }
    } catch (err) {
      view.querySelector('#kb-result').innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    } finally {
      setBusy(button, false);
    }
  };

  bindActions(view, {
    ask: (el) => run('chat', el),
    search: (el) => run('search', el),
    example: (el) => { view.querySelector('#kb-query').value = el.dataset.text; view.querySelector('#kb-query').focus(); },
    sample: async (el) => {
      if (!(await confirmDialog('载入 20 条脱敏示例案例？示例只用于演示检索，不代表现场结论。'))) return;
      setBusy(el, true, '载入中…');
      try {
        const r = await api('/api/kb/sample', { method: 'POST', body: {} });
        toast(`已载入 ${r.documents || 0} 个文档`, 'ok');
        renderKb();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
  return view;
}

export async function renderKbDocs() {
  const admin = state.user?.role === 'admin';
  const params = new URLSearchParams((location.hash.split('?')[1] || ''));
  const docStatus = params.get('status') || 'all';
  const docQ = params.get('q') || '';
  const view = mount('知识库文档', '导入、审核和停用都会重建索引；原始文本仅管理员可见',
    `<a class="btn" href="#/kb">${icon('search')} 返回问答</a>`);
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let status;
  let facets;
  let docs;
  let jobs = { items: [] };
  let drafts = { items: [] };
  try {
    status = (await api('/api/kb/status')).status;
    facets = await loadFacets();
    docs = await api(`/api/kb/docs?size=100&status=${encodeURIComponent(docStatus)}&q=${encodeURIComponent(docQ)}`);
    jobs = await api('/api/kb/import/jobs');
    if (admin) drafts = await api('/api/kb/drafts');
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const items = docs.items || [];
  view.innerHTML = `
    <div class="grid grid-4 kb-stats">
      <div class="card stat"><div><div class="stat-val">${status.documents || 0}</div>
        <div class="stat-label">生效文档</div><div class="stat-note">${status.cases || 0} 个案例</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.drafts || 0}</div>
        <div class="stat-label">待审核</div><div class="stat-note">管理员审核后才进入索引</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.index?.terms || 0}</div>
        <div class="stat-label">索引词项</div><div class="stat-note">${esc(status.index?.built_at || '尚未构建')}</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.index?.build_ms || 0} ms</div>
        <div class="stat-label">最近重建</div><div class="stat-note">${esc(status.index?.status || 'empty')}</div></div></div>
    </div>

    <div class="grid kb-admin-grid" style="margin-top:14px">
      <div class="card">
        <div class="card-head"><h2>导入文档</h2><span class="sub">CSV / TXT / MD / HTML / DOCX / XLSX / PDF / 图片，单文件 ≤ ${status.config?.max_file_mb || 50} MB</span></div>
        <div class="card-body">
          <form id="kb-import-form">
            <div class="form-row"><label>选择文件（可多选）</label>
              <input class="input" type="file" name="files" multiple accept=".csv,.tsv,.txt,.md,.markdown,.log,.html,.htm,.docx,.xlsx,.pdf,.png,.jpg,.jpeg,.webp" required>
              <div class="tiny muted">CSV/XLSX 按问题、答案列解析；DOCX/PDF 抽取文本；图片/扫描 PDF 进入人工转写队列。同一条答案里的多个【原因分析】会拆成多个派生案例。</div></div>
            ${admin ? `<div class="form-row"><label>入库方式</label>
              <select class="input" name="mode"><option value="direct">直接生效（管理员）</option>
              <option value="review">先进入审核队列</option></select></div>` : `<input type="hidden" name="mode" value="review">`}
            <div class="inline"><button class="btn btn-primary" type="button" data-act="import">${icon('upload')} 开始导入</button>
              <button class="btn" type="button" data-act="sample" ${items.length ? 'disabled' : ''}>载入示例</button></div>
          </form>
          <div id="kb-job-status" class="tiny muted" style="margin-top:10px"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>最近导入任务</h2></div>
        <div class="card-body tight">
          ${(jobs.items || []).length ? `<div class="kb-job-list">${jobs.items.slice(0, 8).map((j) => `<div class="kb-job-row">
            <span class="mono tiny">${esc(j.id)}</span>${jobBadge(j.status)}
            <span class="tiny muted">${j.done_files || 0}/${j.total_files || 0} 文件</span>
          </div>`).join('')}</div>` : '<div class="empty small">还没有导入任务</div>'}
        </div>
      </div>
    </div>

    ${admin && (drafts.items || []).length ? `<div class="card" style="margin-top:14px">
      <div class="card-head"><h2>待审核</h2><span class="sub">确认案例字段后再批准；批准后立即进入检索</span></div>
      <div class="card-body tight table-wrap"><table class="data responsive"><thead><tr>
        <th>标题</th><th>案例</th><th>置信度</th><th>来源</th><th>操作</th></tr></thead><tbody>
        ${drafts.items.map((d) => `<tr><td data-label="标题"><strong>${esc(d.title)}</strong>
          <div class="tiny muted">${esc((d.warnings || []).join('；') || '无警告')}</div></td>
          <td data-label="案例">${d.case_count || 0}</td>
          <td data-label="置信度">${scoreText(d.confidence)}</td>
          <td data-label="来源" class="tiny">${esc(d.created_at ? fmtDate(d.created_at) : '')}</td>
          <td data-label="操作"><button class="btn btn-sm" data-act="draft-view" data-id="${esc(d.id)}">查看</button>
            <button class="btn btn-sm btn-primary" data-act="draft-approve" data-id="${esc(d.id)}">批准</button>
            <button class="btn btn-sm btn-danger" data-act="draft-reject" data-id="${esc(d.id)}">退回</button></td></tr>`).join('')}
      </tbody></table></div></div>` : ''}

    <div class="card" style="margin-top:14px">
      <div class="card-head"><h2>文档库</h2><span class="sub">共 ${docs.total || items.length} 个；点击行查看案例</span>
        <div class="inline" style="margin-left:auto">
          <select class="input" id="kb-doc-status"><option value="all">全部状态</option>
            ${Object.entries(STATUS_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
          <input class="input" id="kb-doc-q" placeholder="搜索标题/正文" style="max-width:200px">
          <button class="btn btn-sm" data-act="filter-docs">筛选</button></div>
      </div>
      <div class="card-body tight table-wrap">
        ${items.length ? `<table class="data responsive"><thead><tr><th>标题</th><th>来源</th><th>板卡/领域</th>
          <th>案例</th><th>状态</th><th>更新时间</th></tr></thead><tbody>
          ${items.map((d) => `<tr class="row-hover" data-href="#/kb/docs/${encodeURIComponent(d.id)}">
            <td data-label="标题"><div style="font-weight:600">${esc(d.title)}</div>
              <div class="tiny muted mono">${esc(d.id)}</div></td>
            <td data-label="来源">${esc(d.source_type || '—')}<div class="tiny muted">${esc(d.source_name || '')}</div></td>
            <td data-label="板卡/领域">${esc((d.boards || []).join('、') || '—')}<div class="tiny muted">${esc((d.domains || []).join('、') || '')}</div></td>
            <td data-label="案例">${d.case_count || 0}</td>
            <td data-label="状态">${statusBadge(d.status)}</td>
            <td data-label="更新时间" class="tiny">${esc(d.updated_at ? fmtDate(d.updated_at) : '—')}</td>
          </tr>`).join('')}</tbody></table>`
          : emptyState('inbox', '暂无文档', '从上方导入 CSV/TXT/MD，或载入示例数据。')}
      </div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>数据纪律：</strong>原始文本只对管理员开放；普通用户和模型证据只使用脱敏文本。删除是软删除，版本仍保留用于审计。
    </div></div>`;

  const pollJob = (jobId) => {
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      try {
        const job = (await api(`/api/kb/import/jobs/${encodeURIComponent(jobId)}`)).job;
        const box = view.querySelector('#kb-job-status');
        if (box) box.innerHTML = `任务 ${esc(job.id)}：${jobBadge(job.status)} · ${job.done_files}/${job.total_files} 文件 · ${esc(job.error || '')}`;
        if (['completed', 'partial', 'failed', 'cancelled'].includes(job.status) || tries > 120) {
          clearInterval(timer);
          if (job.status === 'completed' || job.status === 'partial') {
            toast(job.status === 'partial' ? '部分文件失败，请查看任务' : '导入完成', job.status === 'partial' ? 'warn' : 'ok');
            // 导入完成时用户可能已切到问答/详情页，不能把旧页面重新挂回来。
            setTimeout(() => {
              if (location.hash === '#/kb/docs' || location.hash.startsWith('#/kb/docs?')) renderKbDocs();
            }, 600);
          }
        }
      } catch (err) {
        if (tries > 8) { clearInterval(timer); toast(err.message, 'error'); }
      }
    }, 1200);
    state.cleanup = () => clearInterval(timer);
  };

  const openDraft = async (id) => {
    try {
      const data = (await api(`/api/kb/drafts/${encodeURIComponent(id)}`)).draft;
      const payload = data.payload || {};
      const doc = payload.document || {};
      const cases = payload.cases || [];
      const m = modal({
        title: `审核：${doc.title || data.title}`,
        wide: true,
        body: `<div class="alert info">${icon('info')}<div>批准后将写入知识库并重建索引。当前 ${cases.length} 个案例；原始文本请确认无敏感信息。</div></div>
          <div class="form-row" style="margin-top:12px"><label>标题</label><input class="input" id="kb-draft-title" value="${esc(doc.title || data.title)}"></div>
          <div class="form-row"><label>案例预览</label><div class="kb-case-list">${cases.map((c, i) => `<div class="kb-case-card"><strong>K${i + 1} ${esc(c.title)}</strong>
            <div class="kb-snippet"><span>原因</span>${esc(c.cause || '—')}</div><div class="kb-snippet"><span>方案</span>${esc(c.solution || '—')}</div></div>`).join('')}</div></div>
          <details><summary class="tiny muted">查看脱敏原文</summary><pre class="kb-raw">${esc(doc.masked_text || doc.raw_text || '')}</pre></details>`,
        footer: `<button class="btn" data-act="cancel">关闭</button>
          <button class="btn btn-primary" data-act="approve">批准入库</button>`,
      });
      m.mask.addEventListener('click', async (e) => {
        if (e.target.closest('[data-act="cancel"]')) m.close();
        const btn = e.target.closest('[data-act="approve"]');
        if (!btn) return;
        setBusy(btn, true, '批准中…');
        try {
          const title = m.mask.querySelector('#kb-draft-title').value.trim();
          if (title && title !== doc.title) await api(`/api/kb/drafts/${encodeURIComponent(id)}`, { method: 'PUT', body: { title } });
          await api(`/api/kb/drafts/${encodeURIComponent(id)}/approve`, { method: 'POST', body: {} });
          toast('已批准', 'ok'); m.close(); renderKbDocs();
        } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
      });
    } catch (err) { toast(err.message, 'error'); }
  };

  bindActions(view, {
    import: async (el) => {
      const form = view.querySelector('#kb-import-form');
      const files = form.querySelector('input[type="file"]').files;
      if (!files || !files.length) { toast('请选择文件', 'error'); return; }
      const fd = new FormData();
      for (const f of files) fd.append('files', f, f.name);
      fd.append('mode', form.querySelector('[name="mode"]')?.value || 'review');
      setBusy(el, true, '上传中…');
      try {
        const r = await api('/api/kb/import', { method: 'POST', body: fd, timeoutMs: 600000 });
        toast('已创建导入任务', 'ok');
        const box = view.querySelector('#kb-job-status');
        if (box) box.innerHTML = `任务 ${esc(r.job.id)}：${jobBadge(r.job.status)}`;
        pollJob(r.job.id);
      } catch (err) { toast(err.message, 'error'); }
      finally { setBusy(el, false); }
    },
    sample: async (el) => {
      if (!(await confirmDialog('载入 20 条脱敏示例案例？'))) return;
      setBusy(el, true, '载入中…');
      try { await api('/api/kb/sample', { method: 'POST', body: {} }); toast('已载入示例', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'filter-docs': () => {
      const status = view.querySelector('#kb-doc-status').value;
      const q = view.querySelector('#kb-doc-q').value.trim();
      location.hash = `#/kb/docs?status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`;
    },
    'draft-view': (el) => openDraft(el.dataset.id),
    'draft-approve': async (el) => {
      if (!(await confirmDialog('批准该草稿并立即进入检索？'))) return;
      setBusy(el, true, '批准中…');
      try { await api(`/api/kb/drafts/${encodeURIComponent(el.dataset.id)}/approve`, { method: 'POST', body: {} }); toast('已批准', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'draft-reject': async (el) => {
      const reason = prompt('退回原因（可留空）') ?? null;
      if (reason === null) return;
      setBusy(el, true, '退回中…');
      try { await api(`/api/kb/drafts/${encodeURIComponent(el.dataset.id)}/reject`, { method: 'POST', body: { reason } }); toast('已退回', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
  view.querySelectorAll('tr[data-href]').forEach((tr) => {
    tr.addEventListener('click', (event) => {
      if (event.target.closest('button,a')) return;
      location.hash = tr.dataset.href;
    });
  });
  // 初始文档筛选从 hash 读取（仅本页使用）。
  if (params.get('status') && view.querySelector('#kb-doc-status')) {
    view.querySelector('#kb-doc-status').value = params.get('status');
  }
  if (params.get('q') && view.querySelector('#kb-doc-q')) {
    view.querySelector('#kb-doc-q').value = params.get('q');
  }
  return view;
}

export async function renderKbDoc(docId) {
  const view = mount('知识库文档详情', '案例字段、原文和版本；引用可回溯到这里', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let data;
  try { data = (await api(`/api/kb/docs/${encodeURIComponent(docId)}`)).doc; }
  catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  const admin = state.user?.role === 'admin';
  const doc = data;
  let versionItems = [];
  if (admin) {
    try { versionItems = (await api(`/api/kb/docs/${encodeURIComponent(docId)}/versions`)).items || []; }
    catch { versionItems = []; }
  }
  const versionsText = (doc.versions || []).map(versionLabel).filter(Boolean).join('、') || '—';
  view.innerHTML = `
    <div class="inline" style="margin-bottom:12px"><a class="btn btn-sm" href="#/kb/docs">← 返回文档库</a>
      <a class="btn btn-sm" href="#/kb">${icon('search')} 去提问</a>
      ${admin ? `<span class="badge ${STATUS_BADGE[doc.status] || ''}">${esc(STATUS_LABEL[doc.status] || doc.status)}</span>` : ''}
    </div>
    <div class="card">
      <div class="card-head"><h2>${esc(doc.title)}</h2>
        <span class="sub">${esc(doc.id)} · ${esc(doc.source_type)} · ${esc(doc.source_name || '')}</span></div>
      <div class="card-body">
        <div class="kv">
          <div class="k">板卡</div><div class="v">${esc((doc.boards || []).join('、') || '未识别')}</div>
          <div class="k">领域</div><div class="v">${esc((doc.domains || []).join('、') || '未识别')}</div>
          <div class="k">版本</div><div class="v mono tiny">${esc(versionsText)}</div>
          <div class="k">质量 / 置信度</div><div class="v">${scoreText(doc.quality)} / ${scoreText(doc.confidence)}</div>
          <div class="k">PII</div><div class="v">${doc.pii_masked ? '已脱敏（原文仅管理员可读）' : '未脱敏'}</div>
          <div class="k">创建 / 更新</div><div class="v tiny">${esc(doc.created_at || '')} / ${esc(doc.updated_at || '')}</div>
        </div>
        ${admin ? `<div class="inline" style="margin-top:12px">
          <button class="btn btn-sm" data-act="toggle-status">${doc.status === 'active' ? '停用' : '启用'}</button>
          <button class="btn btn-sm" data-act="reindex">重建索引</button>
          <button class="btn btn-sm btn-danger" data-act="delete">删除文档</button></div>` : ''}
      </div>
    </div>
    ${(doc.cases || []).length ? `<div class="card" style="margin-top:14px"><div class="card-head"><h2>案例（${doc.cases.length}）</h2>
      <span class="sub">${doc.cases.some((c) => c.is_derived) ? '含多原因派生案例' : '单案例'}</span></div>
      <div class="card-body kb-doc-cases">${doc.cases.map((c, i) => `<div class="kb-doc-case">
        <div class="inline"><span class="kb-cite">K${i + 1}</span><strong>${esc(c.title)}</strong>
          <span class="badge info" style="margin-left:auto">${scoreText(c.quality)}</span></div>
        <div class="kv" style="margin-top:10px">
          <div class="k">现象</div><div class="v">${esc(c.symptom || '—')}</div>
          <div class="k">环境/复现</div><div class="v">${esc(c.environment || '—')} ${esc(c.repro || '')}</div>
          <div class="k">原因</div><div class="v">${esc(c.cause_detail || c.cause || '—')}</div>
          <div class="k">处理方案</div><div class="v">${esc(c.solution || '—')}</div>
          <div class="k">预防</div><div class="v">${esc(c.prevention || '—')}</div>
        </div>
        ${(c.sections || []).length ? `<details><summary class="tiny muted">查看分节原文（${c.sections.length}）</summary>
          ${c.sections.map((s) => `<div class="kb-section"><div class="k">${esc(s.label || s.kind)}</div><div>${esc(s.text)}</div></div>`).join('')}</details>` : ''}
      </div>`).join('')}</div></div>` : ''}
    ${admin ? `<div class="card" style="margin-top:14px"><div class="card-head"><h2>编辑与原文</h2><span class="sub">保存后重建索引；旧版本保留</span></div>
      <div class="card-body"><form id="kb-doc-edit">
        <div class="form-row"><label>标题</label><input class="input" name="title" value="${esc(doc.title)}"></div>
        <div class="form-row"><label>正文</label><textarea class="input" name="raw_text" rows="10">${esc(doc.raw_text || '')}</textarea></div>
        <button class="btn btn-primary" type="button" data-act="save-doc">保存并重建</button>
      </form></div></div>` : ''}
    ${admin && versionItems.length ? `<div class="card" style="margin-top:14px">
      <div class="card-head"><h2>版本历史（${versionItems.length}）</h2>
        <span class="sub">恢复旧版本会重建案例；当前版本不会丢失</span></div>
      <div class="card-body tight table-wrap"><table class="data responsive"><thead><tr>
        <th>版本</th><th>时间</th><th>SHA256</th><th>操作</th></tr></thead><tbody>
        ${versionItems.map((v) => `<tr><td data-label="版本">v${esc(String(v.version))}</td>
          <td data-label="时间" class="tiny">${esc(v.created_at ? fmtDate(v.created_at) : '—')}</td>
          <td data-label="SHA256" class="mono tiny">${esc((v.sha256 || '').slice(0, 12) || '—')}</td>
          <td data-label="操作"><button class="btn btn-sm" data-act="restore-version" data-id="${esc(v.id)}">恢复</button></td></tr>`).join('')}
      </tbody></table></div></div>` : ''}
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>引用：</strong><span class="mono">faultkb://case/&lt;case_id&gt;</span>；页面链接为 <span class="mono">#/kb/docs/${esc(doc.id)}</span>。
      普通用户看不到原始文本，回答和工具也只使用脱敏文本。
    </div></div>`;

  bindActions(view, {
    'toggle-status': async (el) => {
      const next = doc.status === 'active' ? 'disabled' : 'active';
      setBusy(el, true, '处理中…');
      try { await api(`/api/kb/docs/${encodeURIComponent(doc.id)}/status`, { method: 'POST', body: { status: next } }); toast('已更新', 'ok'); renderKbDoc(docId); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    reindex: async (el) => {
      setBusy(el, true, '重建中…');
      try { await api('/api/kb/reindex', { method: 'POST', body: {} }); toast('索引已重建', 'ok'); }
      catch (err) { toast(err.message, 'error'); }
      finally { setBusy(el, false); }
    },
    'restore-version': async (el) => {
      if (!(await confirmDialog('恢复该历史版本？当前版本会保留，可再次恢复。'))) return;
      setBusy(el, true, '恢复中…');
      try {
        await api(`/api/kb/docs/${encodeURIComponent(docId)}/versions/${encodeURIComponent(el.dataset.id)}/restore`,
          { method: 'POST', body: {} });
        toast('已恢复历史版本', 'ok'); renderKbDoc(docId);
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    delete: async (el) => {
      if (!(await confirmDialog('删除后该文档不再参与检索，但版本记录保留。确认删除？', { danger: true, okText: '删除' }))) return;
      setBusy(el, true, '删除中…');
      try { await api(`/api/kb/docs/${encodeURIComponent(doc.id)}`, { method: 'DELETE' }); toast('已删除', 'ok'); location.hash = '#/kb/docs'; }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'save-doc': async (el) => {
      const form = view.querySelector('#kb-doc-edit');
      const title = form.querySelector('[name="title"]').value.trim();
      const raw = form.querySelector('[name="raw_text"]').value;
      setBusy(el, true, '保存中…');
      try { await api(`/api/kb/docs/${encodeURIComponent(doc.id)}`, { method: 'PUT', body: { title, raw_text: raw } }); toast('已保存并重建索引', 'ok'); renderKbDoc(docId); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
  return view;
}
