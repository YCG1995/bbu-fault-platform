// 故障知识库：检索问答、引用案例、文档库、导入任务与审核队列
import {
  api, bindActions, confirmDialog, emptyState, esc, fmtDate, icon, mount, modal,
  setBusy, state, toast,
} from './core.js';

const STATUS_LABEL = { active: '生效', reviewing: '待审核', disabled: '已停用', deleted: '已删除' };
const STATUS_BADGE = { active: 'ok', reviewing: 'warn', disabled: '', deleted: 'danger' };
const JOB_LABEL = {
  queued: '排队中', processing: '解析中', completed: '已完成', partial: '部分完成',
  failed: '失败', cancelled: '已取消', cancelling: '取消中', rolled_back: '已回退',
};

const KB_PROGRESS_STEPS = {
  search: [['parse', '解析问题与过滤条件'], ['retrieve', '检索索引与候选案例'], ['rank', '融合排序并生成结果']],
  chat: [['parse', '解析问题与过滤条件'], ['retrieve', '检索索引与候选案例'], ['rank', '融合排序'], ['answer', '整理回答与引用']],
};

function kbProgressHtml(endpoint, query, filters) {
  const steps = KB_PROGRESS_STEPS[endpoint] || KB_PROGRESS_STEPS.search;
  return `<div class="card kb-progress" id="kb-progress" data-endpoint="${endpoint}">
    <div class="card-head"><h2>检索进度</h2><span class="sub">后台操作会逐步显示；完成前请不要重复提交</span>
      <span class="mono tiny" id="kb-progress-clock" style="margin-left:auto">0.0s</span></div>
    <div class="card-body">
      <div class="kb-progress-bar"><span id="kb-progress-fill" style="width:4%"></span></div>
      <div class="kb-progress-steps">${steps.map(([key, label], i) => `<div class="kb-progress-step ${i === 0 ? 'active' : ''}" data-step="${key}">
        <span class="kb-progress-dot"></span><strong>${label}</strong><span class="tiny muted kb-progress-state">${i === 0 ? '进行中' : '等待'}</span>
      </div>`).join('')}</div>
      <details class="kb-progress-detail"><summary>查看后台操作</summary>
        <div class="tiny muted">请求：${endpoint === 'chat' ? 'POST /api/kb/chat' : 'GET /api/kb/search'}；
        查询：${esc(query.slice(0, 160))}${query.length > 160 ? '…' : ''}；
        过滤：${esc(JSON.stringify(filters || {}))}</div>
        <div class="tiny muted">本次检索完成后会写入知识库查询日志，并可在「任务记录 → 知识库操作」查看。</div>
      </details>
      <div id="kb-progress-note" class="tiny muted">正在准备检索…</div>
    </div></div>`;
}

function startKbProgress(view, endpoint, query, filters) {
  const target = view.querySelector('#kb-result');
  if (!target) return { finish() {}, fail() {} };
  target.innerHTML = kbProgressHtml(endpoint, query, filters);
  const started = performance.now();
  const steps = KB_PROGRESS_STEPS[endpoint] || KB_PROGRESS_STEPS.search;
  let index = 0;
  const stepEls = target.querySelectorAll('.kb-progress-step');
  const fill = target.querySelector('#kb-progress-fill');
  const clock = target.querySelector('#kb-progress-clock');
  const note = target.querySelector('#kb-progress-note');
  const advance = () => {
    if (!target.isConnected) return;
    index = Math.min(index + 1, steps.length - 1);
    stepEls.forEach((el, i) => {
      el.classList.toggle('active', i === index);
      el.classList.toggle('done', i < index);
      const state = el.querySelector('.kb-progress-state');
      if (state) state.textContent = i < index ? '完成' : i === index ? '进行中' : '等待';
    });
    if (fill) fill.style.width = `${Math.max(8, Math.round(((index + 1) / steps.length) * 88))}%`;
    const label = steps[index]?.[1] || '';
    if (note) note.textContent = `后台操作：${label}…`;
  };
  const timer = setInterval(advance, endpoint === 'chat' ? 1800 : 450);
  const clockTimer = setInterval(() => {
    if (clock && target.isConnected) clock.textContent = `${((performance.now() - started) / 1000).toFixed(1)}s`;
  }, 200);
  const clear = () => { clearInterval(timer); clearInterval(clockTimer); };
  return {
    finish() {
      clear();
      if (fill) fill.style.width = '100%';
      stepEls.forEach((el) => { el.classList.remove('active'); el.classList.add('done');
        const state = el.querySelector('.kb-progress-state'); if (state) state.textContent = '完成'; });
    },
    fail() {
      clear();
      const active = target.querySelector('.kb-progress-step.active');
      if (active) { active.classList.add('failed'); const state = active.querySelector('.kb-progress-state');
        if (state) state.textContent = '失败'; }
    },
  };
}


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
  return parts.join('') + kbRecordFooter(data);
}

function kbRecordFooter(data) {
  if (!data?.query_id) return '';
  return `<div class="kb-record-note tiny muted">${icon('list')} 本次检索已写入任务记录
    <span class="mono">${esc(data.query_id)}</span>
    <a href="#/tasks?tab=kb">查看知识库操作 →</a></div>`;
}

async function loadFacets(kbId = 1) {
  try { return await api(`/api/kb/facets?kb=${encodeURIComponent(kbId)}`); }
  catch { return { boards: [], domains: [] }; }
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
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const kbId = Math.max(1, Number(params.get('kb') || 1));
  const view = mount('故障问答', '先查现场历史案例；结论必须能回到具体文档和案例', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  let status;
  let facets;
  let recent = [];
  try {
    const [statusResp, facetData, recentResp] = await Promise.all([
      api(`/api/kb/status?kb=${kbId}`), loadFacets(kbId),
      api(`/api/kb/docs?size=6&status=active&kb=${kbId}`).catch(() => ({ items: [] })),
    ]);
    status = statusResp.status;
    facets = facetData;
    recent = recentResp.items || [];
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  if (!status.enabled) {
    view.innerHTML = `<div class="alert warn">${icon('alert')}<div>故障知识库已在运行时配置中关闭。</div></div>`;
    return;
  }
  const empty = Number(status.documents || 0) === 0;
  const libraryOptions = (status.libraries || []).filter((x) => x.status === 'active')
    .map((x) => `<option value="${esc(x.id)}" ${Number(x.id) === Number(status.kb_id || 1) ? 'selected' : ''}>${esc(x.name)} · ${x.documents || 0} 文档</option>`).join('')
    || '<option value="1">故障知识库</option>';
  view.innerHTML = `
    <div class="card"><div class="card-body inline" style="gap:12px;flex-wrap:wrap">
      <label class="small" for="kb-library-select">知识库</label>
      <select class="input" id="kb-library-select" style="max-width:260px">${libraryOptions}</select>
      <label class="check"><input type="checkbox" id="kb-cross" ${params.get('cross') === '1' ? 'checked' : ''}> 跨库检索（融合排序）</label>
      <span class="tiny muted">专家库、语料库与故障库逻辑隔离；跨库检索只合并结果，不合并数据。</span>
    </div></div>
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
          <a href="#/kb/docs?tab=import">导入页</a> 导入 CSV/TXT/MD，或先载入示例数据。</div></div>` : ''}
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
      <div class="card-head"><h2>最近入库</h2><a class="tiny" href="#/kb/docs?tab=docs" style="margin-left:auto">查看全部</a></div>
      <div class="card-body kb-recent">${recent.map((d) => `<a class="kb-recent-row" href="#/kb/docs/${encodeURIComponent(d.id)}">
        <span>${esc(d.title)}</span><span class="tiny muted">${esc((d.boards || []).join('、') || '—')} · ${d.case_count || 0} 案例</span>
      </a>`).join('')}</div></div>` : ''}
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>回答边界：</strong>知识库回答只基于检索到的历史案例；无模型时显示本地摘要，有模型时也保留原文引用。
      历史案例不是厂商正式公告，执行前需结合当前版本、现场日志和变更记录复核。
    </div></div>`;

  let running = false;
  let progress = null;
  const run = async (endpoint, button) => {
    const query = view.querySelector('#kb-query').value.trim();
    if (!query) { toast('请先描述故障现象', 'error'); return; }
    if (running) { toast('当前检索仍在进行，请等待本次结果', 'warn'); return; }
    const selectedKb = Number(view.querySelector('#kb-library-select')?.value || kbId);
    const cross = !!view.querySelector('#kb-cross')?.checked;
    const filters = {};
    const board = view.querySelector('#kb-board').value;
    const domain = view.querySelector('#kb-domain').value;
    const kbIds = cross ? [...view.querySelectorAll('#kb-library-select option')].map((x) => Number(x.value)).filter((x) => x > 0) : [selectedKb];
    if (board) filters.board = board;
    if (domain) filters.domain = domain;
    running = true;
    progress = startKbProgress(view, endpoint, query, filters);
    setBusy(button, true, '检索中…');
    try {
      const data = endpoint === 'chat'
        ? await api('/api/kb/chat', { method: 'POST', body: { query, filters, kb_id: selectedKb, kb_ids: kbIds }, timeoutMs: 90000 })
        : await api(`/api/kb/search?q=${encodeURIComponent(query)}&board=${encodeURIComponent(board)}&domain=${encodeURIComponent(domain)}&limit=10&kb_ids=${kbIds.join(',')}`);
      if (!view.isConnected) return;
      progress.finish();
      if (endpoint === 'search') {
        const items = data.items || [];
        view.querySelector('#kb-result').innerHTML = items.length
          ? `<div class="card"><div class="card-head"><h2>检索结果</h2><span class="sub">${data.no_answer ? '低于拒答阈值，仅供参考' : `${items.length} 个案例`} · ${data.latency_ms || 0} ms</span></div>
            <div class="card-body kb-case-list">${items.map((c, i) => `<a class="kb-case-card" href="#/kb/docs/${encodeURIComponent(c.doc_id)}">
              <div class="inline"><span class="kb-cite">K${i + 1}</span><strong>${esc(c.title)}</strong><span class="badge info" style="margin-left:auto">${scoreText(c.score)}</span></div>
              <div class="kb-snippet"><span>现象</span>${esc(c.symptom || '—')}</div>
              <div class="kb-snippet"><span>原因</span>${esc(c.cause || '—')}</div>
              <div class="kb-snippet"><span>方案</span>${esc(c.solution || '—')}</div></a>`).join('')}</div></div>${kbRecordFooter(data)}`
          : `<div class="alert warn">${icon('alert')}<div>${esc(data.reason || '没有命中案例')}</div></div>${kbRecordFooter(data)}`;
      } else {
        view.querySelector('#kb-result').innerHTML = renderResult(data);
      }
    } catch (err) {
      if (progress) progress.fail();
      if (view.isConnected) {
        view.querySelector('#kb-result').innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>${kbRecordFooter({ query_id: '' })}`;
      }
    } finally {
      running = false;
      progress = null;
      setBusy(button, false);
    }
  };

  view.querySelector('#kb-library-select')?.addEventListener('change', (e) => {
    location.hash = `#/kb?kb=${encodeURIComponent(e.target.value)}`;
  });
  bindActions(view, {
    ask: (el) => run('chat', el),
    search: (el) => run('search', el),
    example: (el) => { view.querySelector('#kb-query').value = el.dataset.text; view.querySelector('#kb-query').focus(); },
    sample: async (el) => {
      if (!(await confirmDialog('载入 20 条脱敏示例案例？示例只用于演示检索，不代表现场结论。'))) return;
      setBusy(el, true, '载入中…');
      try {
        const r = await api('/api/kb/sample', { method: 'POST', body: { kb_id: kbId } });
        toast(`已载入 ${r.documents || 0} 个文档`, 'ok');
        renderKb();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
  return view;
}

const KB_TABS = [
  ['overview', '概览'], ['stats', '统计'], ['docs', '文档'], ['import', '导入'],
  ['review', '审核'], ['audit', '变更与审计'], ['terms', '词典'],
  ['libraries', '知识库'], ['eval', '评测'], ['settings', '设置'],
];

function kbLibraryBar(status, kbId = 1) {
  const items = (status.libraries || []).filter((x) => x.status === 'active');
  if (!items.length) return '';
  return `<div class="card" style="margin-top:10px"><div class="card-body inline" style="gap:10px;flex-wrap:wrap">
    <span class="small muted">当前知识库</span>
    <select class="input" id="kb-library-switch" style="max-width:280px">
      ${items.map((x) => `<option value="${esc(x.id)}" ${Number(x.id) === Number(kbId) ? 'selected' : ''}>${esc(x.name)} · ${x.documents || 0} 文档 / ${x.cases || 0} 案例</option>`).join('')}
    </select>
    <a class="btn btn-sm" href="#/kb/docs?tab=libraries">${icon('layers')} 管理知识库</a>
    <span class="tiny muted">逻辑隔离；每库独立索引与导入任务。</span>
  </div></div>`;
}

const REVIEW_STATUS = {
  queued: '排队中', running: '审核中', done: '已完成', manual: '转人工',
  failed: '失败', decided: '已终审',
};
const REVIEW_STATUS_BADGE = { queued: 'warn', running: 'warn', done: 'ok', manual: 'warn',
  failed: 'danger', decided: 'ok' };

function kbTabNav(active, kbId = 1) {
  const suffix = Number(kbId) && Number(kbId) !== 1 ? `&kb=${Number(kbId)}` : '';
  return `<div class="kb-tabbar">${KB_TABS.map(([key, label]) =>
    `<a class="kb-tab ${key === active ? 'active' : ''}" href="#/kb/docs?tab=${key}${suffix}">${label}</a>`).join('')}</div>`;
}

function reviewBadge(status) {
  return `<span class="badge ${REVIEW_STATUS_BADGE[status] || ''}">${esc(REVIEW_STATUS[status] || status || '—')}</span>`;
}

function trendBars(trend, days = 30) {
  const rows = (trend || []).slice(-Math.max(1, Number(days || 30)));
  const max = Math.max(1, ...rows.map((x) => Number(x.queries || 0)));
  return `<div class="kb-trend">${rows.map((x) => `<div class="kb-trend-col" title="${esc(x.day)} 查询 ${x.queries}">
    <div class="kb-trend-bar" style="height:${Math.max(2, Math.round((Number(x.queries || 0) / max) * 100))}%"></div>
    <span class="tiny muted">${esc(String(x.day).slice(5))}</span></div>`).join('')}</div>`;
}

function miniBars(items, key = 'n') {
  const rows = items || [];
  const max = Math.max(1, ...rows.map((x) => Number(x[key] || 0)));
  return rows.slice(0, 10).map((x) => `<div class="kb-meter"><span class="tiny">${esc(x.query || x.title || x.day || '')}</span>
    <div class="kb-meter-track"><div class="kb-meter-fill" style="width:${Math.round((Number(x[key] || 0) / max) * 100)}%"></div></div>
    <span class="mono tiny">${x[key] || 0}</span></div>`).join('');
}

function kbReloadTab(tab) {
  location.hash = `#/kb/docs?tab=${encodeURIComponent(tab)}&_=${Date.now()}`;
}

async function openReviewModal(id) {
  let review;
  try { review = (await api(`/api/kb/reviews/${encodeURIComponent(id)}`)).review; }
  catch (err) { toast(err.message, 'error'); return; }
  const dims = review.dimensions || [];
  const issues = review.issues || [];
  const patchText = Object.keys(review.suggested_patch || {}).length
    ? JSON.stringify(review.suggested_patch, null, 2) : '';
  const m = modal({
    title: `审核：${review.target_title || review.target_id}`,
    wide: true,
    body: `<div class="inline"><span class="badge ${review.status === 'done' ? 'ok' : 'warn'}">${esc(REVIEW_STATUS[review.status] || review.status)}</span>
      <span class="badge ${review.decision === 'pass' ? 'ok' : review.decision === 'reject' ? 'danger' : 'warn'}">${esc(review.decision || '待模型')}</span>
      <span class="mono tiny">${esc(review.model || '')}</span><span class="tiny muted" style="margin-left:auto">${review.score ? Math.round(review.score) + ' 分' : ''} · 置信度 ${Math.round((review.confidence || 0) * 100)}%</span></div>
      <div class="kb-subhead">六维评分</div>
      ${dims.map((d) => `<div class="kb-meter"><span class="tiny">${esc(d.label || d.key)}</span>
        <div class="kb-meter-track"><div class="kb-meter-fill" style="width:${Math.round((Number(d.score || 0) / Number(d.max || 1)) * 100)}%"></div></div>
        <span class="mono tiny">${d.score}/${d.max}</span></div>
        <div class="tiny muted">${esc(d.comment || '')}</div>`).join('') || '<div class="muted small">模型未返回评分</div>'}
      <div class="kb-subhead">问题</div>
      ${issues.length ? `<div class="kb-issues">${issues.map((i) => `<div class="kb-issue ${i.severity === 'blocker' ? 'danger' : ''}">
        <span class="badge ${i.severity === 'blocker' ? 'danger' : i.severity === 'warn' ? 'warn' : 'info'}">${esc(i.severity)}</span>
        <strong>${esc(i.code)}</strong> ${esc(i.field || '')}<div class="tiny muted">${esc(i.message)}${i.suggestion ? ` · 建议：${esc(i.suggestion)}` : ''}</div></div>`).join('')}</div>` : '<div class="muted small">未发现问题</div>'}
      ${patchText ? `<div class="kb-subhead">建议 patch（人工采纳后才写入）</div><pre class="kb-raw">${esc(patchText)}</pre>` : ''}
      ${review.error ? `<div class="alert warn" style="margin-top:10px">${esc(review.error)}</div>` : ''}`,
    footer: `<button class="btn" data-act="close">关闭</button>
      ${review.status !== 'decided' ? `<button class="btn" data-act="retry">${icon('refresh')} 重新审核</button>
      <button class="btn btn-danger" data-act="reject">驳回</button>
      ${patchText ? '<button class="btn" data-act="adopt">采纳 patch</button>' : ''}
      <button class="btn btn-primary" data-act="approve">通过并生效</button>` : ''}`,
  });
  m.mask.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn || btn.dataset.act === 'close') { if (btn) m.close(); return; }
    const act = btn.dataset.act;
    if (act === 'retry') {
      setBusy(btn, true, '审核中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/retry`, { method: 'POST', body: {} });
        toast('审核已重试', 'ok'); m.close(); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
      return;
    }
    if (act === 'reject') {
      const note = prompt('驳回原因（会写入审计）') ?? null;
      if (note === null) return;
      setBusy(btn, true, '驳回中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/decision`,
        { method: 'POST', body: { decision: 'reject', note } });
        toast('已驳回', 'ok'); m.close(); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
      return;
    }
    if (act === 'adopt') {
      if (!(await confirmDialog('采纳建议 patch？会先写入目标案例/草稿，再等待人工发布。'))) return;
      setBusy(btn, true, '应用中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/decision`,
        { method: 'POST', body: { decision: 'adopt_patch', apply_patch: true } });
        toast('patch 已采纳', 'ok'); m.close(); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
      return;
    }
    if (act === 'approve') {
      const note = prompt('终审备注（可留空）') ?? '';
      setBusy(btn, true, '发布中…');
      try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/decision`,
        { method: 'POST', body: { decision: 'approve', note } });
        toast('已通过', 'ok'); m.close(); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

async function openDraft(id) {
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
}

function selectedJobIds(view) {
  return [...view.querySelectorAll('.kb-job-check:checked')].map((el) => el.value).filter(Boolean);
}

async function batchJobAction(view, el, path, confirmText, busyText) {
  const ids = selectedJobIds(view);
  if (!ids.length) { toast('请先勾选导入任务', 'error'); return; }
  if (!(await confirmDialog(confirmText.replace('{n}', ids.length)))) return;
  setBusy(el, true, busyText);
  try {
    const r = await api(path, { method: 'POST', body: { job_ids: ids } });
    toast(`已处理 ${ids.length} 个任务`, 'ok');
    if (r.items?.some((x) => !x.ok)) {
      toast(r.items.filter((x) => !x.ok).map((x) => x.error).join('；'), 'error');
    }
    renderKbDocs();
  } catch (err) { toast(err.message, 'error'); }
  finally { setBusy(el, false); }
}

function bindKbTabActions(view) {
  bindActions(view, {
    'review-view': (el) => openReviewModal(el.dataset.id),
    'review-decision': (el) => openReviewModal(el.dataset.id),
    'review-batch': async (el) => {
      setBusy(el, true, '批量审核中…');
      try { const r = await api('/api/kb/reviews/run', { method: 'POST', body: { limit: 20 } });
        toast(`处理 ${r.processed} 条`, 'ok'); kbReloadTab('review');
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'audit-filter': () => {
      const actor = view.querySelector('#kb-audit-actor')?.value.trim() || '';
      const action = view.querySelector('#kb-audit-action')?.value.trim() || '';
      location.hash = `#/kb/docs?tab=audit&actor=${encodeURIComponent(actor)}&action=${encodeURIComponent(action)}`;
    },
    'library-create': async (el) => {
      const name = view.querySelector('#kb-lib-name')?.value.trim();
      if (!name) { toast('请填写知识库名称', 'error'); return; }
      setBusy(el, true, '创建中…');
      try {
        await api('/api/kb/libraries', { method: 'POST', body: {
          name, kind: view.querySelector('#kb-lib-kind')?.value || 'fault',
          description: view.querySelector('#kb-lib-desc')?.value.trim() || '',
        } });
        toast('已创建知识库', 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'library-archive': async (el) => {
      if (!(await confirmDialog('归档该知识库？归档后不再出现在问答库列表；数据保留，可由管理员离线清理。'))) return;
      setBusy(el, true, '归档中…');
      try {
        await api(`/api/kb/libraries/${encodeURIComponent(el.dataset.id)}/archive`, { method: 'POST', body: {} });
        toast('已归档', 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'library-restore': async (el) => {
      const file = view.querySelector('#kb-lib-file')?.files?.[0];
      if (!file) { toast('请选择备份包', 'error'); return; }
      const fd = new FormData();
      fd.append('file', file, file.name);
      const name = view.querySelector('#kb-lib-restore-name')?.value.trim() || '';
      if (name) fd.append('name', name);
      setBusy(el, true, '恢复中…');
      try {
        const r = await api('/api/kb/libraries/restore', { method: 'POST', body: fd, timeoutMs: 300000 });
        toast(`已恢复到新库：${r.documents || 0} 个文档`, 'ok'); renderKbDocs();
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'job-rollback': (el) => batchJobAction(view, el, '/api/kb/import/jobs/rollback',
      '回退所选 {n} 个导入任务？本批新增文档会被停用，未审草稿会退回；版本与修订链保留。', '回退中…'),
    'job-retry': (el) => batchJobAction(view, el, '/api/kb/import/jobs/retry',
      '重试所选 {n} 个任务中的失败文件？', '重试中…'),
    'job-delete': (el) => batchJobAction(view, el, '/api/kb/import/jobs/delete',
      '删除所选 {n} 个导入任务记录？已入库文档不会删除，仅删除任务和暂存文件。', '删除中…'),
    'draft-view': (el) => openDraft(el.dataset.id),
    'draft-approve': async (el) => {
      if (!(await confirmDialog('批准该草稿并立即进入检索？'))) return;
      setBusy(el, true, '批准中…');
      try { await api(`/api/kb/drafts/${encodeURIComponent(el.dataset.id)}/approve`, { method: 'POST', body: {} }); toast('已批准', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
    'draft-bulk-approve': async (el) => {
      const ids = [...view.querySelectorAll('.kb-draft-check:checked')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选草稿', 'error'); return; }
      if (!(await confirmDialog(`批量批准 ${ids.length} 个草稿并写入知识库？`))) return;
      setBusy(el, true, '批量批准中…');
      let ok = 0; let failed = 0;
      for (const id of ids) {
        try { await api(`/api/kb/drafts/${encodeURIComponent(id)}/approve`, { method: 'POST', body: {} }); ok += 1; }
        catch { failed += 1; }
      }
      toast(`已批准 ${ok} 个${failed ? `，${failed} 个失败` : ''}`, failed ? 'error' : 'ok');
      renderKbDocs();
    },
    'draft-bulk-reject': async (el) => {
      const ids = [...view.querySelectorAll('.kb-draft-check:checked')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选草稿', 'error'); return; }
      const reason = prompt('批量退回原因（可留空）') ?? null;
      if (reason === null) return;
      setBusy(el, true, '批量退回中…');
      let ok = 0; let failed = 0;
      for (const id of ids) {
        try { await api(`/api/kb/drafts/${encodeURIComponent(id)}/reject`, { method: 'POST', body: { reason } }); ok += 1; }
        catch { failed += 1; }
      }
      toast(`已退回 ${ok} 个${failed ? `，${failed} 个失败` : ''}`, failed ? 'error' : 'ok');
      renderKbDocs();
    },
    'review-bulk-approve': async (el) => {
      const ids = [...view.querySelectorAll('.kb-review-check:checked:not(:disabled)')].map((x) => x.value);
      if (!ids.length) { toast('请先勾选审核记录', 'error'); return; }
      if (!(await confirmDialog(`批量通过 ${ids.length} 条审核？存在 blocker 的记录会被拒绝。`))) return;
      setBusy(el, true, '批量通过中…');
      let ok = 0; let failed = 0;
      for (const id of ids) {
        try { await api(`/api/kb/reviews/${encodeURIComponent(id)}/decision`, { method: 'POST', body: { decision: 'approve', note: '批量通过' } }); ok += 1; }
        catch { failed += 1; }
      }
      toast(`已通过 ${ok} 条${failed ? `，${failed} 条被拦截或失败` : ''}`, failed ? 'error' : 'ok');
      renderKbDocs();
    },
    'draft-reject': async (el) => {
      const reason = prompt('退回原因（可留空）') ?? null;
      if (reason === null) return;
      setBusy(el, true, '退回中…');
      try { await api(`/api/kb/drafts/${encodeURIComponent(el.dataset.id)}/reject`, { method: 'POST', body: { reason } }); toast('已退回', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
}

function importJobProgress(job) {
  const total = Math.max(0, Number(job.total_files || 0));
  const done = Math.max(0, Number(job.done_files || 0));
  const status = String(job.status || 'queued');
  let pct = 4;
  if (status === 'completed') pct = 100;
  else if (status === 'partial') pct = 92;
  else if (status === 'failed') pct = 88;
  else if (status === 'processing') pct = total ? Math.max(12, Math.min(88, Math.round((done / total) * 88))) : 18;
  else if (status === 'queued') pct = 5;
  const files = job.files || [];
  return `<div class="kb-job-progress">
    <div class="kb-job-progress-head"><span>${jobBadge(status)}</span>
      <span class="tiny muted">${done}/${total || '—'} 个文件 · ${esc(job.id)}</span>
      <span class="mono tiny" style="margin-left:auto">${pct}%</span></div>
    <div class="kb-progress-bar"><span style="width:${pct}%"></span></div>
    <div class="tiny muted" style="margin-top:6px">${esc(job.error || (status === 'processing' ? '正在解析文件并写入案例；页面会持续刷新进度。' : '等待后台任务开始…'))}</div>
    ${files.length ? `<div class="kb-job-files">${files.map((f) => `<div class="kb-job-file">
      <span class="mono tiny">${esc(f.name || f.rel_path || '')}</span>${f.status === 'done' ? '<span class="badge ok">完成</span>' : f.status === 'skipped' ? '<span class="badge warn">跳过</span>' : f.status === 'failed' ? '<span class="badge danger">失败</span>' : '<span class="badge warn">处理中</span>'}
      ${f.error ? `<span class="tiny danger-text">${esc(f.error)}</span>` : ''}</div>`).join('')}</div>` : ''}
  </div>`;
}

function pollImportJob(view, jobId) {
  let tries = 0;
  const timer = setInterval(async () => {
    tries += 1;
    try {
      const job = (await api(`/api/kb/import/jobs/${encodeURIComponent(jobId)}`)).job;
      const box = view.querySelector('#kb-job-status');
      if (box) box.innerHTML = importJobProgress(job);
      const cancel = view.querySelector('[data-act="cancel-job"]');
      if (cancel) cancel.disabled = ['completed', 'partial', 'failed', 'cancelled'].includes(job.status);
      if (['completed', 'partial', 'failed', 'cancelled'].includes(job.status) || tries > 180) {
        clearInterval(timer);
        if (job.status === 'completed' || job.status === 'partial') {
          toast(job.status === 'partial' ? '部分文件失败，请查看任务详情' : '导入完成', job.status === 'partial' ? 'warn' : 'ok');
          setTimeout(() => {
            const params = new URLSearchParams(location.hash.split('?')[1] || '');
            if (location.hash.startsWith('#/kb/docs') && params.get('tab') === 'import') renderKbDocs();
          }, 600);
        }
      }
    } catch (err) {
      if (tries > 8) { clearInterval(timer); toast(err.message, 'error'); }
    }
  }, 1100);
  state.cleanup = () => clearInterval(timer);
}

async function renderKbImport(view, header, status, kbId = 1) {
  let jobs = { items: [] };
  try { jobs = await api(`/api/kb/import/jobs?kb=${kbId}`); } catch { /* optional */ }
  view.innerHTML = `${header}
    <div class="grid kb-admin-grid">
      <div class="card">
        <div class="card-head"><h2>导入文档</h2><span class="sub">CSV / TXT / MD / HTML / DOCX / XLSX / PDF / 图片，单文件 ≤ ${status.config?.max_file_mb || 50} MB</span></div>
        <div class="card-body">
          <div class="kb-import-steps"><span>1 选择文件</span><span>2 解析与拆案例</span><span>3 写库并重建索引</span></div>
          <form id="kb-import-form">
            <div class="form-row"><label>选择文件（可多选）</label>
              <input class="input" type="file" name="files" multiple accept=".csv,.tsv,.txt,.md,.markdown,.log,.html,.htm,.docx,.xlsx,.pdf,.png,.jpg,.jpeg,.webp" required>
              <div class="tiny muted">CSV/XLSX 按问题、答案列解析；DOCX/PDF 抽取文本；图片/扫描 PDF 进入人工转写队列。同一条答案里的多个【原因分析】会拆成多个派生案例。</div></div>
            ${state.user?.role === 'admin' ? `<div class="form-row"><label>入库方式</label>
              <select class="input" name="mode"><option value="direct">直接生效（管理员）</option>
              <option value="review">先进入审核队列</option></select></div>` : `<input type="hidden" name="mode" value="review">`}
            <div class="form-row"><label>目标知识库</label><select class="input" name="kb_id">
              ${(status.libraries || []).filter((x) => x.status === 'active').map((x) => `<option value="${esc(x.id)}" ${Number(x.id) === Number(status.kb_id || 1) ? 'selected' : ''}>${esc(x.name)}</option>`).join('') || '<option value="1">故障知识库</option>'}
            </select></div>
            <div class="inline"><button class="btn btn-primary" type="button" data-act="import">${icon('upload')} 开始导入</button>
              <button class="btn" type="button" data-act="sample" ${(status.documents || 0) ? 'disabled' : ''}>载入示例</button></div>
          </form>
          <div id="kb-job-status" class="kb-job-status" style="margin-top:12px"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>最近导入任务</h2><span class="sub">勾选后可批量回退、重试或删除记录</span></div>
        <div class="card-body tight">
          ${(jobs.items || []).length ? `<div class="kb-job-list">${jobs.items.slice(0, 10).map((j) => `<div class="kb-job-row">
            <label class="check"><input type="checkbox" class="kb-job-check" value="${esc(j.id)}"></label>
            <span style="font-weight:600">${esc(j.display_name || j.id)}</span>${jobBadge(j.status)}
            <span class="tiny muted">${j.done_files || 0}/${j.total_files || 0} 文件</span>
            ${j.stats?.skipped ? `<span class="tiny muted">跳过 ${j.stats.skipped}</span>` : ''}
            <span class="tiny muted">${esc(j.created_at ? fmtDate(j.created_at) : '')}</span>
            ${j.rollback_supported === false ? '<span class="badge warn">旧任务</span>' : ''}
          </div>`).join('')}</div>
          <div class="inline" style="margin-top:10px;flex-wrap:wrap">
            <button class="btn btn-sm" data-act="job-rollback">${icon('history')} 回退所选</button>
            <button class="btn btn-sm" data-act="job-retry">${icon('refresh')} 重试失败</button>
            <button class="btn btn-sm btn-danger" data-act="job-delete">${icon('trash')} 删除记录</button>
            <span class="tiny muted">回退会停用本批新增文档、退回未审草稿；版本与修订链保留。</span>
          </div>` : '<div class="empty small">还没有导入任务</div>'}
        </div>
      </div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>导入说明：</strong>导入是后台任务，文件完成后才进入索引；原始文本仅管理员可见。
      大批量导入时可以先完成上传，再离开本页，任务会在后台继续。
    </div></div>`;
  bindActions(view, {
    import: async (el) => {
      const form = view.querySelector('#kb-import-form');
      const files = form.querySelector('input[type="file"]').files;
      if (!files || !files.length) { toast('请选择文件', 'error'); return; }
      const fd = new FormData();
      for (const f of files) fd.append('files', f, f.name);
      fd.append('mode', form.querySelector('[name="mode"]')?.value || 'review');
      fd.append('kb_id', form.querySelector('[name="kb_id"]')?.value || '1');
      setBusy(el, true, '上传中…');
      try {
        const r = await api('/api/kb/import', { method: 'POST', body: fd, timeoutMs: 600000 });
        toast('已创建导入任务，后台解析中', 'ok');
        const box = view.querySelector('#kb-job-status');
        if (box) box.innerHTML = importJobProgress({ ...r.job, files: [], error: '' });
        pollImportJob(view, r.job.id);
      } catch (err) { toast(err.message, 'error'); }
      finally { setBusy(el, false); }
    },
    sample: async (el) => {
      if (!(await confirmDialog('载入 20 条脱敏示例案例？'))) return;
      setBusy(el, true, '载入中…');
      try { await api('/api/kb/sample', { method: 'POST', body: {} }); toast('已载入示例', 'ok'); renderKbDocs(); }
      catch (err) { toast(err.message, 'error'); setBusy(el, false); }
    },
  });
}

async function renderKbTab(tab, view, status, facets, kbId = 1) {
  const admin = state.user?.role === 'admin';
  bindKbTabActions(view);
  view.addEventListener('change', (e) => {
    if (e.target.id === 'kb-library-switch') {
      location.hash = `#/kb/docs?tab=overview&kb=${encodeURIComponent(e.target.value)}`;
    }
  });
  const tabs = kbTabNav(tab, kbId);
  const kbSuffix = Number(kbId) && Number(kbId) !== 1 ? `&kb=${Number(kbId)}` : '';
  const header = `${tabs}${kbLibraryBar(status, kbId)}`;
  if (!admin) {
    view.innerHTML = `${header}<div class="alert warn">${icon('alert')}<div>仅管理员可进入知识库管理。</div></div>`;
    return;
  }
  if (tab === 'import') {
    await renderKbImport(view, header, status, kbId);
    return;
  }
  const statsDays = Math.max(1, Math.min(365, Number(
    new URLSearchParams(location.hash.split('?')[1] || '').get('days') || 30)));
  let stats = null;
  if (['overview', 'stats', 'eval'].includes(tab)) {
    try { stats = await api(`/api/kb/stats?days=${statsDays}`); }
    catch (err) { view.innerHTML = `${header}<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`; return; }
  }
  if (tab === 'libraries') {
    let libraries = { items: [] };
    try { libraries = await api('/api/kb/libraries?include_archived=1'); }
    catch (err) { view.innerHTML = `${header}<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`; return; }
    const kindLabel = { fault: '故障库', expert: '专家库', corpus: '语料库' };
    view.innerHTML = `${header}
      <div class="grid grid-2">
        <div class="card"><div class="card-head"><h2>知识库列表</h2><span class="sub">故障库 / 专家库 / 语料库同构；默认库不可归档</span></div>
          <div class="card-body tight"><div class="kb-job-list">
            ${(libraries.items || []).map((x) => `<div class="kb-job-row" style="flex-wrap:wrap">
              <span style="font-weight:600">${esc(x.name)}</span>
              <span class="badge ${x.status === 'active' ? 'ok' : 'warn'}">${x.status === 'active' ? '生效' : '已归档'}</span>
              <span class="badge info">${esc(kindLabel[x.kind] || x.kind)}</span>
              <span class="tiny muted">${x.documents || 0} 文档 / ${x.cases || 0} 案例</span>
              ${x.is_default ? '<span class="badge">默认</span>' : ''}
              <span style="margin-left:auto" class="inline">
                <a class="btn btn-sm" href="/api/kb/libraries/${encodeURIComponent(x.id)}/backup">${icon('download')} 备份</a>
                ${x.status === 'active' && !x.is_default ? `<button class="btn btn-sm btn-danger" data-act="library-archive" data-id="${esc(x.id)}">归档</button>` : ''}
              </span>
              ${x.description ? `<div class="tiny muted" style="flex-basis:100%">${esc(x.description)}</div>` : ''}
            </div>`).join('') || '<div class="empty small">还没有知识库</div>'}
          </div></div></div>
        <div class="card"><div class="card-head"><h2>新建 / 恢复</h2><span class="sub">备份格式 *.kbbackup.zip，含文档与案例 JSONL</span></div>
          <div class="card-body">
            <div class="form-row"><label>名称</label><input class="input" id="kb-lib-name" maxlength="80" placeholder="例如：专家经验库"></div>
            <div class="form-row"><label>类型</label><select class="input" id="kb-lib-kind">
              <option value="fault">故障库</option><option value="expert">专家库</option><option value="corpus">语料库</option></select></div>
            <div class="form-row"><label>说明</label><input class="input" id="kb-lib-desc" maxlength="500" placeholder="可选"></div>
            <button class="btn btn-primary" data-act="library-create">${icon('plus')} 创建知识库</button>
            <hr style="margin:16px 0;border:0;border-top:1px solid var(--line)">
            <div class="form-row"><label>从备份恢复</label><input class="input" id="kb-lib-file" type="file" accept=".zip,.kbbackup.zip">
              <input class="input" id="kb-lib-restore-name" placeholder="恢复后的名称（可选）" style="margin-top:6px"></div>
            <button class="btn" data-act="library-restore">${icon('upload')} 上传并恢复</button>
          </div></div>
      </div>`;
    return;
  }
  if (tab === 'overview') {
    let reviews = { items: [] };
    let jobs = { items: [] };
    try { reviews = await api(`/api/kb/reviews?status=pending&limit=6&kb=${kbId}`); } catch { /* optional */ }
    try { jobs = await api(`/api/kb/import/jobs?kb=${kbId}`); } catch { /* optional */ }
    const attention = [];
    if (reviews.items?.length) attention.push(`<div class="kb-attention"><strong>${reviews.items.length} 条审核待处理</strong>
      <a href="#/kb/docs?tab=review${kbSuffix}">去审核</a></div>`);
    if (Number(stats.review?.pending || 0)) attention.push(`<div class="kb-attention"><strong>${stats.review.pending} 条 AI/人工审核未完成</strong>
      <a href="#/kb/docs?tab=review${kbSuffix}">查看</a></div>`);
    if (Number(stats.inventory?.drafts || 0)) attention.push(`<div class="kb-attention"><strong>${stats.inventory.drafts} 个草稿待入库</strong>
      <a href="#/kb/docs?tab=review${kbSuffix}">处理</a></div>`);
    if (!attention.length) attention.push(`<div class="kb-attention ok">当前没有待处理事项</div>`);
    view.innerHTML = `${header}
      <div class="grid grid-4 kb-stats">
        <div class="card stat"><div><div class="stat-val">${stats.cards.documents}</div><div class="stat-label">生效文档</div><div class="stat-note">${stats.cards.cases} 个可引用案例</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.cards.queries}</div><div class="stat-label">30 天查询</div><div class="stat-note">零结果 ${stats.cards.zero_result_rate}%</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.cards.pending_reviews}</div><div class="stat-label">待审核</div><div class="stat-note">AI 预审 + 人工终审</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.cards.revisions}</div><div class="stat-label">30 天修订</div><div class="stat-note">哈希链可校验</div></div></div>
      </div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><div class="card-head"><h2>需要关注</h2></div><div class="card-body">${attention.join('')}</div></div>
        <div class="card"><div class="card-head"><h2>最近导入</h2><a class="tiny" href="#/kb/docs?tab=import${kbSuffix}" style="margin-left:auto">全部</a></div>
          <div class="card-body tight">${(jobs.items || []).slice(0, 5).map((j) => `<div class="kb-job-row">
            <span>${esc(j.display_name || j.id)}</span>${jobBadge(j.status)}<span class="tiny muted">${j.done_files || 0}/${j.total_files || 0} 文件</span></div>`).join('') || emptyState('inbox', '还没有导入', '导入 CSV/文档或载入示例。')}</div></div>
      </div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><div class="card-head"><h2>索引状态</h2></div><div class="card-body small">
          <div class="kv"><div class="k">状态</div><div class="v">${esc(stats.index.status)}</div>
          <div class="k">文档 / 案例</div><div class="v">${stats.index.documents} / ${stats.index.cases}</div>
          <div class="k">词项 / 体积</div><div class="v">${stats.index.terms} / ${Math.round((stats.index.bytes || 0) / 1024)} KB</div>
          <div class="k">最近构建</div><div class="v tiny">${esc(stats.index.built_at || '—')} · ${stats.index.build_ms} ms</div></div></div></div>
        <div class="card"><div class="card-head"><h2>最近变更</h2><a class="tiny" href="#/kb/docs?tab=audit${kbSuffix}" style="margin-left:auto">审计</a></div>
          <div class="card-body tight">${stats.changes.top_cases.slice(0, 6).map((x) => `<div class="kb-job-row">
            <span>${esc(x.title || x.case_id)}</span><span class="tiny muted">${x.revisions} 次修订</span></div>`).join('') || '<div class="empty small">暂无修订</div>'}</div></div>
      </div>`;
    return;
  }
  if (tab === 'stats') {
    view.innerHTML = `${header}
      <div class="inline" style="margin-bottom:10px"><span class="tiny muted">统计范围：最近 ${stats.days} 天；库存与索引为当前真值</span>
        <span class="kb-range">${[7, 30, 90].map((d) => `<a class="btn btn-sm ${stats.days === d ? 'btn-primary' : ''}"
          href="#/kb/docs?tab=stats&days=${d}${kbSuffix}">${d} 天</a>`).join('')}</span>
        <a class="btn btn-sm" href="/api/kb/stats?format=csv&days=${stats.days}" style="margin-left:auto">${icon('download')} 导出 CSV</a></div>
      <div class="grid grid-4 kb-stats">
        <div class="card stat"><div><div class="stat-val">${stats.inventory.active_documents}</div><div class="stat-label">生效文档</div><div class="stat-note">${stats.inventory.active_cases} 案例 · ${stats.inventory.drafts} 待审草稿</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.usage.queries}</div><div class="stat-label">查询次数</div><div class="stat-note">零结果率 ${stats.usage.zero_result_rate}% · P95 ${stats.usage.p95_latency_ms} ms</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.usage.feedback.helpful_rate}%</div><div class="stat-label">有帮助率</div><div class="stat-note">${stats.usage.feedback.helpful} 有帮助 / ${stats.usage.feedback.unhelpful} 无帮助</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.review.pending}</div><div class="stat-label">审核待处理</div><div class="stat-note">AI 覆盖 ${stats.review.ai_coverage}% · 人工通过 ${stats.review.human_approval_rate}%</div></div></div>
      </div>
      <div class="card" style="margin-top:14px"><div class="card-head"><h2>${stats.days} 天查询趋势</h2><span class="sub">柱高按当日查询量归一化</span></div><div class="card-body">${trendBars(stats.trend, stats.days)}</div></div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><div class="card-head"><h2>高频问题</h2></div><div class="card-body">${miniBars(stats.usage.top_queries) || '<div class="empty small">暂无查询</div>'}</div></div>
        <div class="card"><div class="card-head"><h2>被引用案例</h2></div><div class="card-body">${miniBars(stats.usage.cited_cases, 'citation_count') || '<div class="empty small">暂无引用</div>'}</div></div>
      </div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><div class="card-head"><h2>覆盖</h2></div><div class="card-body small">
          <div class="kv"><div class="k">原因覆盖</div><div class="v">${stats.coverage.cause_coverage}%</div>
          <div class="k">方案覆盖</div><div class="v">${stats.coverage.solution_coverage}%</div>
          <div class="k">低质量</div><div class="v">${stats.coverage.low_quality}</div></div>
          <div class="kb-subhead">板卡 × 领域</div>
          <div class="kb-matrix">${(stats.coverage.board_domain_matrix || []).slice(0, 12).map((x) =>
            `<span class="badge info">${esc(x.board)} / ${esc(x.domain)} · ${x.count}</span>`).join('') || '<span class="muted tiny">暂无标签</span>'}</div></div></div>
        <div class="card"><div class="card-head"><h2>审核与变更</h2></div><div class="card-body small">
          <div class="kv"><div class="k">AI 覆盖</div><div class="v">${stats.review.ai_coverage}%</div>
          <div class="k">AI 结论</div><div class="v">通过 ${stats.review.ai_pass} · 转人工 ${stats.review.ai_needs_review + stats.review.fallback_manual} · 驳回 ${stats.review.ai_reject}</div>
          <div class="k">人工推翻 AI</div><div class="v">${stats.review.human_overrides}</div>
          <div class="k">修订 / 恢复</div><div class="v">${stats.changes.revisions} / ${stats.changes.restores}</div>
          <div class="k">导入成功率</div><div class="v">${stats.imports.success_rate}%</div></div>
          <div class="kb-subhead">最近变更人</div>
          ${(stats.changes.actors || []).slice(0, 5).map((x) => `<div class="kb-meter"><span class="tiny">${esc(x.actor)}</span><span class="mono tiny">${x.count}</span></div>`).join('') || '<span class="muted tiny">暂无</span>'}
        </div></div>
      </div>`;
    return;
  }
  if (tab === 'review') {
    const params = new URLSearchParams(location.hash.split('?')[1] || '');
    const activeView = params.get('view') === 'reviews' ? 'reviews' : 'drafts';
    let drafts = { items: [] };
    let reviews = { items: [] };
    try { drafts = await api(`/api/kb/drafts?kb=${kbId}`); } catch { /* optional */ }
    try { reviews = await api(`/api/kb/reviews?status=pending&limit=100&kb=${kbId}`); } catch { /* optional */ }
    const reviewTabs = [
      ['drafts', `待入库草稿 ${drafts.items?.length || 0}`],
      ['reviews', `AI / 人工审核 ${reviews.items?.length || 0}`],
    ];
    const panels = {
      drafts: `${(drafts.items || []).length ? `<table class="data responsive"><thead><tr>
        <th><input type="checkbox" id="kb-draft-all" aria-label="全选草稿"></th><th>标题</th><th>案例</th><th>操作</th></tr></thead><tbody>
        ${drafts.items.map((d) => `<tr><td data-label="选择"><input type="checkbox" class="kb-draft-check" value="${esc(d.id)}"></td>
        <td data-label="标题"><strong>${esc(d.title)}</strong><div class="tiny muted">${esc((d.warnings || []).join('；') || '无警告')}</div></td>
        <td data-label="案例">${d.case_count || 0}</td>
        <td data-label="操作"><button class="btn btn-sm" data-act="draft-view" data-id="${esc(d.id)}">查看</button>
        <button class="btn btn-sm btn-primary" data-act="draft-approve" data-id="${esc(d.id)}">批准</button>
        <button class="btn btn-sm btn-danger" data-act="draft-reject" data-id="${esc(d.id)}">退回</button></td></tr>`).join('')}
        </tbody></table>` : emptyState('check', '没有待审草稿', '工程师导入的文档会先进入这里。')}
        <div class="inline" style="margin-top:10px"><button class="btn btn-sm btn-primary" data-act="draft-bulk-approve">批量批准</button>
        <button class="btn btn-sm btn-danger" data-act="draft-bulk-reject">批量退回</button>
        <span class="tiny muted">勾选后批量处理；每条仍会走 blocker 检查与修订链。</span></div>`,
      reviews: `${(reviews.items || []).length ? `<table class="data responsive"><thead><tr>
        <th><input type="checkbox" id="kb-review-all" aria-label="全选审核"></th><th>对象</th><th>结论</th><th>状态</th><th>操作</th></tr></thead><tbody>
        ${reviews.items.map((r) => `<tr><td data-label="选择"><input type="checkbox" class="kb-review-check" value="${esc(r.id)}" ${r.status === 'decided' ? 'disabled' : ''}></td>
        <td data-label="对象"><strong>${esc(r.target_title || r.target_id)}</strong><div class="tiny muted">${esc(r.target_type)} · ${esc(r.target_id)}</div></td>
        <td data-label="结论">${r.decision ? `<span class="badge ${r.decision === 'pass' ? 'ok' : r.decision === 'reject' ? 'danger' : 'warn'}">${esc(r.decision)}</span> <span class="tiny">${r.score ? Math.round(r.score) + ' 分' : ''}</span>` : '—'}</td>
        <td data-label="状态">${reviewBadge(r.status)}</td>
        <td data-label="操作"><button class="btn btn-sm" data-act="review-view" data-id="${esc(r.id)}">详情</button>
        ${r.status !== 'decided' ? `<button class="btn btn-sm" data-act="review-decision" data-id="${esc(r.id)}">终审</button>` : ''}</td></tr>`).join('')}
        </tbody></table>` : emptyState('check', '没有待处理审核', '导入或编辑后会生成审核记录。')}
        <div class="inline" style="margin-top:10px"><button class="btn btn-sm btn-primary" data-act="review-bulk-approve">批量通过</button>
        <button class="btn btn-sm" data-act="review-batch">${icon('refresh')} 处理排队中的 AI 预审</button>
        <span class="tiny muted">批量通过仍会拒绝 blocker；需要逐条备注时点“终审”。</span></div>`,
    };
    view.innerHTML = `${header}
      <div class="card"><div class="card-head"><h2>审核队列</h2><span class="sub">草稿与 AI 预审分开展示；处理后再发布</span></div>
        <div class="card-body">
          <div class="kb-tabbar" role="tablist">${reviewTabs.map(([key, label]) =>
            `<a class="kb-tab ${key === activeView ? 'active' : ''}" href="#/kb/docs?tab=review&view=${key}${kbSuffix}" role="tab">${label}</a>`).join('')}</div>
          <div class="table-wrap" style="margin-top:10px">${panels[activeView]}</div>
        </div></div>`;
    const checks = view.querySelectorAll('.kb-draft-check,.kb-review-check');
    view.querySelector('#kb-draft-all')?.addEventListener('change', (e) => {
      view.querySelectorAll('.kb-draft-check').forEach((x) => { x.checked = e.target.checked; });
    });
    view.querySelector('#kb-review-all')?.addEventListener('change', (e) => {
      view.querySelectorAll('.kb-review-check:not(:disabled)').forEach((x) => { x.checked = e.target.checked; });
    });
    return;
  }
  if (tab === 'audit') {
    const params = new URLSearchParams(location.hash.split('?')[1] || '');
    const actor = params.get('actor') || '';
    const action = params.get('action') || '';
    let audit = { items: [] };
    try { audit = await api(`/api/kb/audit?limit=100&actor=${encodeURIComponent(actor)}&action=${encodeURIComponent(action)}&kb=${kbId}`); }
    catch (err) { view.innerHTML = `${header}<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`; return; }
    view.innerHTML = `${header}
      <div class="card"><div class="card-head"><h2>变更与审计</h2><span class="sub">案例字段级 diff、文档操作与审核决策</span>
        <a class="btn btn-sm" style="margin-left:auto" href="/api/kb/audit?format=csv&limit=500&kb=${kbId}">${icon('download')} 导出 CSV</a></div>
        <div class="card-body">
          <div class="inline"><input class="input" id="kb-audit-actor" placeholder="操作人" value="${esc(actor)}" style="max-width:160px">
            <input class="input" id="kb-audit-action" placeholder="动作，如 case.update" value="${esc(action)}" style="max-width:220px">
            <button class="btn btn-sm" data-act="audit-filter">筛选</button></div>
          <div class="table-wrap" style="margin-top:10px"><table class="data responsive"><thead><tr><th>时间</th><th>动作</th><th>对象</th><th>操作人</th><th>原因</th></tr></thead><tbody>
            ${(audit.items || []).map((x) => `<tr><td data-label="时间" class="tiny">${esc(x.created_at ? fmtDate(x.created_at) : '—')}</td>
              <td data-label="动作"><span class="mono tiny">${esc(x.action)}</span></td>
              <td data-label="对象"><div>${esc(x.target_title || x.target || '')}</div><div class="tiny muted mono">${esc(x.target || '')}</div></td>
              <td data-label="操作人">${esc(x.actor || '—')}</td>
              <td data-label="原因" class="tiny">${esc(x.message || (Array.isArray(x.detail) ? x.detail.map((d) => d.field).join('、') : ''))}</td></tr>`).join('') || '<tr><td colspan="5" class="empty small">暂无审计记录</td></tr>'}
          </tbody></table></div>
        </div></div>`;
    return;
  }
  if (tab === 'terms') {
    view.innerHTML = `${header}<div class="card"><div class="card-head"><h2>术语与词典</h2><span class="sub">当前从已入库标签抽取；同义词/停用词编辑将在 1.31.0 开放</span></div>
      <div class="card-body"><div class="kb-subhead">板卡</div><div class="kb-matrix">${(facets.boards || []).map((x) => `<span class="badge info">${esc(x.value)} · ${x.count}</span>`).join('') || '<span class="muted tiny">暂无</span>'}</div>
      <div class="kb-subhead">领域</div><div class="kb-matrix">${(facets.domains || []).map((x) => `<span class="badge">${esc(x.value)} · ${x.count}</span>`).join('') || '<span class="muted tiny">暂无</span>'}</div></div></div>`;
    return;
  }
  if (tab === 'eval') {
    view.innerHTML = `${header}<div class="card"><div class="card-head"><h2>评测</h2><span class="sub">示例集结果；真实 2268 条语料与 300 条专家金标准仍待验收</span></div>
      <div class="card-body"><div class="grid grid-4 kb-stats">
        <div class="card stat"><div><div class="stat-val">12/12</div><div class="stat-label">示例 Top1/Top5</div><div class="stat-note">20 文档 / 21 案例</div></div></div>
        <div class="card stat"><div><div class="stat-val">3.5 ms</div><div class="stat-label">平均检索</div><div class="stat-note">P95 3.7 ms</div></div></div>
        <div class="card stat"><div><div class="stat-val">2/2</div><div class="stat-label">库外拒答</div><div class="stat-note">不生成推测根因</div></div></div>
        <div class="card stat"><div><div class="stat-val">${stats.coverage.duplicate_candidates.length}</div><div class="stat-label">近重复候选</div><div class="stat-note">标题归一化后重复</div></div></div>
      </div><div class="alert info" style="margin-top:12px">${icon('info')}<div>评测台、Recall/MRR 页面和专家金标准导入在 1.31.0 开放；当前报告见 docs/故障知识库评测报告-1.30.0.md。</div></div></div></div>`;
    return;
  }
  if (tab === 'settings') {
    view.innerHTML = `${header}<div class="card"><div class="card-head"><h2>知识库设置</h2><span class="sub">运行时配置在“系统管理 → 运行时配置”修改</span></div>
      <div class="card-body"><div class="kv">
        <div class="k">审核开关</div><div class="v">${status.config.review_enabled ? '开启' : '关闭'}</div>
        <div class="k">导入自动预审</div><div class="v">${status.config.review_auto_on_import ? '开启' : '关闭'}</div>
        <div class="k">AI 自动发布</div><div class="v">${status.config.review_auto_publish ? '开启' : '关闭'}（默认关闭）</div>
        <div class="k">PII 脱敏</div><div class="v">${status.config.mask_pii ? '开启' : '关闭'}</div>
        <div class="k">单文件 / 单批</div><div class="v">${status.config.max_file_mb} MB / ${status.config.max_batch_mb} MB</div>
        <div class="k">问答引用上限</div><div class="v">${status.top_k}</div></div>
        <div class="inline" style="margin-top:12px"><a class="btn" href="#/admin">${icon('settings')} 去系统管理</a></div></div></div>`;
    return;
  }
  // docs 之外的兜底
  view.innerHTML = `${header}<div class="alert warn">未知分区</div>`;
}


export async function renderKbDocs() {
  const admin = state.user?.role === 'admin';
  const params = new URLSearchParams((location.hash.split('?')[1] || ''));
  const tab = params.get('tab') || 'docs';
  const docStatus = params.get('status') || 'all';
  const docQ = params.get('q') || '';
  const page = Math.max(1, Number(params.get('page') || 1));
  const kbId = Math.max(1, Number(params.get('kb') || 1));
  const view = mount('知识库管理', '文档库、导入、审核、变更与审计；原始文本仅管理员可见', '');
  view.innerHTML = `<div class="card"><div class="card-body">${icon('refresh')} 加载中…</div></div>`;
  if (tab !== 'docs') {
    try {
      const [statusResp, facets] = await Promise.all([
        api(`/api/kb/status?kb=${kbId}`), loadFacets(kbId)]);
      await renderKbTab(tab, view, statusResp.status, facets, kbId);
    } catch (err) {
      view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    }
    return view;
  }
  let status;
  let docs;
  try {
    [status, docs] = await Promise.all([
      api(`/api/kb/status?kb=${kbId}`).then((r) => r.status),
      api(`/api/kb/docs?size=50&page=${page}&status=${encodeURIComponent(docStatus)}&q=${encodeURIComponent(docQ)}&kb=${kbId}`),
    ]);
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return view;
  }
  const items = docs.items || [];
  const pages = Math.max(1, Math.ceil(Number(docs.total || items.length) / 50));
  view.innerHTML = `
    ${kbTabNav('docs', kbId)}
    <div class="grid grid-4 kb-stats">
      <div class="card stat"><div><div class="stat-val">${status.documents || 0}</div>
        <div class="stat-label">生效文档</div><div class="stat-note">${status.cases || 0} 个案例</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.drafts || 0}</div>
        <div class="stat-label">待审核</div><div class="stat-note">在“审核”标签处理</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.index?.terms || 0}</div>
        <div class="stat-label">索引词项</div><div class="stat-note">${esc(status.index?.built_at || '尚未构建')}</div></div></div>
      <div class="card stat"><div><div class="stat-val">${status.index?.build_ms || 0} ms</div>
        <div class="stat-label">最近重建</div><div class="stat-note">${esc(status.index?.status || 'empty')}</div></div></div>
    </div>
    <div class="card" style="margin-top:14px">
      <div class="card-head"><h2>文档库</h2><span class="sub">共 ${docs.total || items.length} 个；点击行查看案例；本页只展示文档，不在此导入</span>
        <div class="inline" style="margin-left:auto">
          <select class="input" id="kb-doc-status"><option value="all">全部状态</option>
            ${Object.entries(STATUS_LABEL).map(([k, v]) => `<option value="${k}" ${docStatus === k ? 'selected' : ''}>${v}</option>`).join('')}</select>
          <input class="input" id="kb-doc-q" placeholder="搜索标题/正文" value="${esc(docQ)}" style="max-width:200px">
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
          : emptyState('inbox', '暂无文档', '管理员可在“导入”标签导入 CSV/TXT/MD，或载入示例数据。',
            admin ? `<a class="btn btn-sm" href="#/kb/docs?tab=import${kbParam}">去导入</a>` : '')}
      </div>
      ${pages > 1 ? `<div class="pager"><span class="small muted">第 ${page} / ${pages} 页</span>
        <button class="btn btn-sm" data-act="page-docs" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
        <button class="btn btn-sm" data-act="page-docs" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>下一页</button></div>` : ''}
    </div>
    <div class="card" style="margin-top:14px"><div class="card-body small muted">
      <strong>数据纪律：</strong>原始文本只对管理员开放；普通用户和模型证据只使用脱敏文本。删除是软删除，版本仍保留用于审计。
    </div></div>`;

  const kbParam = Number(kbId) !== 1 ? `&kb=${Number(kbId)}` : '';
  const goDocs = (nextPage = page) => {
    location.hash = `#/kb/docs?tab=docs&status=${encodeURIComponent(docStatus)}&q=${encodeURIComponent(docQ)}&page=${nextPage}${kbParam}`;
  };
  bindActions(view, {
    'filter-docs': () => {
      const statusVal = view.querySelector('#kb-doc-status').value;
      const q = view.querySelector('#kb-doc-q').value.trim();
      location.hash = `#/kb/docs?tab=docs&status=${encodeURIComponent(statusVal)}&q=${encodeURIComponent(q)}&page=1&kb=${Number(kbId)}`;
    },
    'page-docs': (el) => goDocs(Number(el.dataset.page || 1)),
  });
  view.querySelectorAll('tr[data-href]').forEach((tr) => {
    tr.addEventListener('click', (event) => {
      if (event.target.closest('button,a')) return;
      location.hash = tr.dataset.href;
    });
  });
  return view;
}

async function openCaseRevisions(caseId, docId, caseTitle) {
  let data;
  let verify;
  try {
    data = await api(`/api/kb/cases/${encodeURIComponent(caseId)}/revisions?size=50`);
    verify = await api(`/api/kb/cases/${encodeURIComponent(caseId)}/revisions/verify`);
  } catch (err) { toast(err.message, 'error'); return; }
  const items = data.items || [];
  const diffText = (diff) => (diff || []).map((d) => `${d.field}: ${JSON.stringify(d.before)} → ${JSON.stringify(d.after)}`).join('\n') || '字段无变化';
  const m = modal({
    title: `修订链：${caseTitle || caseId}`,
    wide: true,
    body: `<div class="alert ${verify.ok ? 'info' : 'danger'}">${icon(verify.ok ? 'check' : 'alert')}
      <div>${verify.ok ? `哈希链校验通过（${verify.checked} 条）` : `校验失败：r${verify.broken?.revision} ${verify.broken?.reason}`}</div></div>
      <div class="tiny muted" style="margin:8px 0">每次人工变更都追加一条修订；快照和哈希不可改写。恢复会生成新修订，不删除历史。</div>
      <div class="kb-rev-list">${items.map((r) => `<div class="kb-rev-row">
        <div class="inline"><strong>r${r.revision}</strong><span class="badge info">${esc(r.action)}</span>
          <span class="tiny muted">${esc(r.actor_name || '')} · ${esc(r.created_at ? fmtDate(r.created_at) : '')}</span>
          <span class="mono tiny" style="margin-left:auto">${esc((r.entry_hash || '').slice(0, 12))}</span></div>
        <div class="tiny">${esc(r.reason || '')}</div>
        <details><summary class="tiny muted">查看 diff</summary><pre class="kb-raw">${esc(diffText(r.diff))}</pre></details>
        <div class="inline"><button class="btn btn-sm" data-act="case-restore" data-id="${esc(r.revision)}">恢复此修订</button></div>
      </div>`).join('') || '<div class="empty small">暂无修订</div>'}</div>`,
    footer: `<button class="btn" data-act="close">关闭</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'close') { m.close(); return; }
    if (btn.dataset.act === 'case-restore') {
      const reason = prompt('恢复原因（写入审计）') ?? null;
      if (reason === null) return;
      setBusy(btn, true, '恢复中…');
      try {
        await api(`/api/kb/cases/${encodeURIComponent(caseId)}/revisions/${encodeURIComponent(btn.dataset.id)}/restore`,
          { method: 'POST', body: { reason } });
        toast('已恢复该修订', 'ok'); m.close(); renderKbDoc(docId);
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

function openCaseEdit(caseData, docId) {
  const m = modal({
    title: `编辑案例：${caseData.title}`,
    wide: true,
    body: `<div class="alert warn">${icon('alert')}<div>保存会新增修订并重建索引；必须填写变更原因。</div></div>
      <div class="form-row"><label>标题</label><input class="input" id="kb-case-title" value="${esc(caseData.title)}"></div>
      <div class="form-row"><label>现象</label><textarea class="input" id="kb-case-symptom" rows="2">${esc(caseData.symptom || '')}</textarea></div>
      <div class="form-row"><label>原因</label><textarea class="input" id="kb-case-cause" rows="3">${esc(caseData.cause_detail || caseData.cause || '')}</textarea></div>
      <div class="form-row"><label>处理方案</label><textarea class="input" id="kb-case-solution" rows="4">${esc(caseData.solution || '')}</textarea></div>
      <div class="form-row"><label>预防</label><textarea class="input" id="kb-case-prevention" rows="2">${esc(caseData.prevention || '')}</textarea></div>
      <div class="form-row"><label>变更原因 <span class="badge warn">必填</span></label><input class="input" id="kb-case-reason" placeholder="例如：补充现场验证结果"></div>`,
    footer: `<button class="btn" data-act="cancel">取消</button>
      <button class="btn btn-primary" data-act="save">保存并生成修订</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'cancel') { m.close(); return; }
    if (btn.dataset.act !== 'save') return;
    const body = {
      title: m.mask.querySelector('#kb-case-title').value.trim(),
      symptom: m.mask.querySelector('#kb-case-symptom').value.trim(),
      cause_detail: m.mask.querySelector('#kb-case-cause').value.trim(),
      solution: m.mask.querySelector('#kb-case-solution').value.trim(),
      prevention: m.mask.querySelector('#kb-case-prevention').value.trim(),
      reason: m.mask.querySelector('#kb-case-reason').value.trim(),
    };
    if (!body.reason) { toast('请填写变更原因', 'error'); return; }
    setBusy(btn, true, '保存中…');
    try {
      await api(`/api/kb/cases/${encodeURIComponent(caseData.id)}`, { method: 'PUT', body });
      toast('已保存并生成新修订', 'ok'); m.close(); renderKbDoc(docId);
    } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
  });
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
          <span class="badge ${c.review_status === 'pass' ? 'ok' : c.review_status === 'reject' ? 'danger' : 'info'}">${esc(c.review_status || '未审核')}</span>
          <span class="badge info">r${c.revision || 1}</span>
          ${admin ? `<span style="margin-left:auto"></span><button class="btn btn-sm" data-act="case-rev" data-id="${esc(c.id)}" data-title="${esc(c.title)}">修订</button>
            <button class="btn btn-sm" data-act="case-edit" data-id="${esc(c.id)}">编辑</button>
            <button class="btn btn-sm" data-act="case-status" data-id="${esc(c.id)}" data-status="${esc(c.status || 'active')}">${c.status === 'active' ? '停用' : '启用'}</button>` : ''}
          <span class="badge info" style="margin-left:${admin ? '0' : 'auto'}">${scoreText(c.quality)}</span></div>
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
    'case-rev': (el) => openCaseRevisions(el.dataset.id, doc.id, el.dataset.title),
    'case-edit': (el) => {
      const c = (doc.cases || []).find((x) => x.id === el.dataset.id);
      if (c) openCaseEdit(c, doc.id);
    },
    'case-status': async (el) => {
      const next = el.dataset.status === 'active' ? 'disabled' : 'active';
      setBusy(el, true, '处理中…');
      try {
        await api(`/api/kb/cases/${encodeURIComponent(el.dataset.id)}/status`,
          { method: 'POST', body: { status: next, reason: next === 'active' ? '管理员启用' : '管理员停用' } });
        toast('已更新案例状态', 'ok'); renderKbDoc(docId);
      } catch (err) { toast(err.message, 'error'); setBusy(el, false); }
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
