// 总览页
import { api, esc, fmtDate, fmtDur, fmtShortDate, fmtSize, icon, mount, state, statusBadge, shortId } from './core.js';
import { modelNoticeHtml, openPersonalModelModal } from './personal_model.js';

function qualityCard(q) {
  const gates = q.maturity_gate || [];
  const passed = gates.filter((g) => g.passed).length;
  const latest = q.latest_evaluation?.summary;
  return `<div class="card">
    <div class="card-head"><h2>成熟度门禁</h2>
      <span class="badge ${q.ready_for_real_data ? 'ok' : 'warn'}" style="margin-left:auto">${passed}/${gates.length} 项达标</span></div>
    <div class="card-body">
      <div class="kv">
        <div class="k">专家复核</div><div class="v">${q.reviews?.total || 0} 条 · 待复核 ${q.reviews?.pending || 0} 条 · 改判率 ${((q.reviews?.correction_rate || 0) * 100).toFixed(0)}%</div>
        <div class="k">真实案例</div><div class="v">${Object.entries(q.cases || {}).map(([k, v]) => `${esc(k)} ${v}`).join(' · ') || '尚未导入'}</div>
        <div class="k">最近评测</div><div class="v">${latest ? `完全命中 ${((latest.exact_rate || 0) * 100).toFixed(0)}% · P ${latest.precision} / R ${latest.recall}` : '尚未运行'}</div>
        <div class="k">案例复核</div><div class="v">待复核 ${q.case_review?.pending || 0} · 已复核 ${q.case_review?.confirmed || 0} · 未分配 ${q.case_review?.unassigned || 0}</div>
        <div class="k">未知故障</div><div class="v">${q.unknown?.unknown_fault || 0} 次 · 未匹配 ${q.unknown?.unmatched || 0} 次</div>
      </div>
      <div style="margin-top:12px" class="stack">
        ${gates.map((g) => `<div class="between tiny">
          <span>${g.passed ? '✓' : '○'} ${esc(g.name)}</span>
          <span class="muted">${g.current} / ${g.target}</span></div>`).join('')}
      </div>
      <div class="inline" style="margin-top:12px"><a class="btn btn-sm" href="#/cases">真实案例与标注</a>
        <span class="tiny muted">已标注真实案例 ${q.real_cases?.labeled || 0} 例（golden ${q.real_cases?.golden || 0} / test ${q.real_cases?.test || 0}）；未复核不计准确率</span></div>
      ${!q.ready_for_real_data ? `<div class="alert warn" style="margin-top:12px">${icon('alert')}<div>门禁未通过：合成回归通过只能说明规则闭环，不能对外宣称现网准确率。请优先补真实案例、专家复核和独立测试集。</div></div>` : ''}
    </div>
  </div>`;
}

function resourcePercent(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0;
}

function resourceText(value, suffix = '%') {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(1)}${suffix}` : '—';
}

function systemResourceCard(resources) {
  if (!resources) return '';
  const cpu = resources.cpu || {};
  const memory = resources.memory || null;
  const process = resources.process || {};
  const disk = resources.disk || null;
  const isAdmin = state.user?.role === 'admin';
  const cpuPct = resourcePercent(cpu.percent);
  const memPct = resourcePercent(memory?.percent);
  const diskPct = resourcePercent(disk?.percent);
  const appPct = resourcePercent(process.percent_of_total);
  return `<div class="card system-resource-card">
    <div class="card-head"><h2>系统资源</h2>
      <span class="sub">页面加载时采样 · ${esc(resources.as_of ? fmtShortDate(resources.as_of) : '')}</span>
      <div class="inline" style="margin-left:auto">
        <button class="btn btn-sm" data-act="refresh-resources">${icon('refresh')}刷新</button>
        ${isAdmin ? '<a class="btn btn-sm" href="#/system">系统信息</a>' : ''}
      </div></div>
    <div class="card-body">
      <div class="resource-grid">
        <div class="resource-tile">
          <div class="resource-label">${icon('cpu')} CPU 使用率</div>
          <div class="resource-value">${resourceText(cpu.percent)}</div>
          <div class="resource-bar"><span style="width:${cpuPct}%"></span></div>
          <div class="resource-note">${Number(cpu.cores || 1)} 核 · 1 分钟负载 ${cpu.load_1m == null ? '—' : Number(cpu.load_1m).toFixed(2)} · ${esc(cpu.source || '')}</div>
        </div>
        <div class="resource-tile">
          <div class="resource-label">${icon('activity')} 当前应用内存</div>
          <div class="resource-value">${fmtSize(process.rss_bytes)}</div>
          <div class="resource-bar"><span style="width:${appPct}%"></span></div>
          <div class="resource-note">总内存 ${memory ? fmtSize(memory.total_bytes) : '—'} · 占 ${resourceText(process.percent_of_total)}${process.threads ? ` · ${process.threads} 线程` : ''}</div>
        </div>
        <div class="resource-tile">
          <div class="resource-label">${icon('layers')} 系统内存</div>
          <div class="resource-value">${memory ? resourceText(memory.percent) : '—'}</div>
          <div class="resource-bar"><span style="width:${memPct}%"></span></div>
          <div class="resource-note">${memory ? `${fmtSize(memory.used_bytes)} 已用 · ${fmtSize(memory.available_bytes)} 可用` : '内存信息不可用'}</div>
        </div>
        ${disk ? `<div class="resource-tile">
          <div class="resource-label">${icon('inbox')} 数据盘</div>
          <div class="resource-value">${resourceText(disk.percent)}</div>
          <div class="resource-bar"><span style="width:${diskPct}%"></span></div>
          <div class="resource-note">${fmtSize(disk.free_bytes)} 可用 / ${fmtSize(disk.total_bytes)} · ${esc(disk.path || '')}</div>
        </div>` : ''}
      </div>
    </div></div>`;
}

function checklistCard(stats) {
  const role = state.user?.role;
  const items = [];
  if (role === 'admin') {
    items.push({ done: (stats.models_enabled || 0) > 0, label: '配置一个模型',
      text: 'OpenAI 兼容、Anthropic 或 Mock；未配置时走确定性路径。', link: '#/models', action: '去配置' });
    items.push({ done: (stats.skills?.ready || 0) > 0, label: '安装或启用技能',
      text: '技能库至少需要一个 ready 状态的子技能。', link: '#/skills', action: '去技能库' });
    items.push({ done: (stats.tasks_total || 0) > 0, label: '创建首个诊断任务',
      text: '上传日志包，验证上传、规划和报告闭环。', link: '#/tasks/new', action: '新建诊断' });
  } else if (role === 'reviewer') {
    items.push({ done: (stats.tasks_total || 0) > 0, label: '创建首个诊断任务',
      text: '专家也可以上传日志诊断；被管理员分配的任务另外出现在待复核。', link: '#/tasks/new', action: '新建诊断' });
    items.push({ done: (stats.pending_reviews || 0) === 0, label: '处理待复核',
      text: '确认、改判或驳回；不能复核自己创建的任务。', link: '#/tasks', action: '去复核' });
  } else {
    items.push({ done: (stats.tasks_total || 0) > 0, label: '创建首个诊断任务',
      text: '上传现场日志包，平台会自动盘点并调度技能。', link: '#/tasks/new', action: '新建诊断' });
  }
  const open = items.filter((it) => !it.done);
  if (!open.length) return '';
  return `<div class="card" style="margin-bottom:16px">
    <div class="card-head"><h2>开始使用</h2><span class="sub">按顺序完成，平台即可接管诊断</span></div>
    <div class="card-body"><div class="stack" style="gap:10px">
      ${items.map((it) => `<div class="between checklist-row">
        <div><span class="check-dot ${it.done ? 'done' : ''}">${it.done ? '✓' : '○'}</span>
          <strong>${esc(it.label)}</strong><div class="small muted" style="margin-left:24px">${esc(it.text)}</div></div>
        ${it.done ? '<span class="badge ok">已完成</span>' : `<a class="btn btn-sm" href="${it.link}">${esc(it.action)}</a>`}
      </div>`).join('')}
    </div></div></div>`;
}

export async function renderDashboard() {
  const view = mount('总览', '平台运行状态、最近的诊断任务与复核进度',
    `<a class="btn btn-primary" href="#/tasks/new">${icon('plus')}新建诊断</a>`);
  view.innerHTML = `<div class="skeleton" style="height:120px;margin-bottom:16px"></div>`;
  let stats;
  let kbStatus = null;
  try {
    const [statsResp, kbResp] = await Promise.all([
      api('/api/stats'),
      api('/api/kb/status').catch(() => null),
    ]);
    stats = statsResp;
    kbStatus = kbResp?.status || null;
  } catch (err) {
    view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div></div>`;
    return;
  }
  let quality = null;
  if (state.user?.role === 'admin') {
    try { quality = await api('/api/quality'); } catch { quality = null; }
  }
  const skillCounts = stats.skills || {};
  const ready = skillCounts.ready || 0;
  const degraded = skillCounts.degraded || 0;
  const bad = (skillCounts.incompatible || 0) + (skillCounts.disabled || 0);
  view.innerHTML = `
    ${modelNoticeHtml(stats.model_status?.notice, { compact: true })}
    ${checklistCard(stats)}
    ${state.user?.role === 'reviewer' && (stats.pending_reviews || stats.pending_cases) ? `<div class="alert info" style="margin-bottom:14px">${icon('shield')}<div><strong>有 ${stats.pending_reviews || 0} 个任务、${stats.pending_cases || 0} 个真实案例待复核。</strong><a href="#/tasks" style="margin-left:8px">复核任务</a><a href="#/cases" style="margin-left:12px">真实案例 →</a></div></div>` : ''}
    <div class="grid grid-4">
      <div class="card stat"><div class="stat-icon">${icon('activity')}</div>
        <div><div class="stat-val">${stats.today}</div><div class="stat-label">今日任务</div>
        <div class="stat-note">已完成 ${stats.done} · 失败/待补充 ${stats.failed}</div></div></div>
      <div class="card stat"><div class="stat-icon ok">${icon('check')}</div>
        <div><div class="stat-val">${stats.success_rate}%</div><div class="stat-label">今日成功率</div>
        <div class="stat-note">按已完成 / 今日任务计算</div></div></div>
      <div class="card stat"><div class="stat-icon info">${icon('clock')}</div>
        <div><div class="stat-val">${fmtDur(stats.avg_duration_sec)}</div><div class="stat-label">平均诊断耗时</div>
        <div class="stat-note">最近 50 个完成任务</div></div></div>
      <div class="card stat"><div class="stat-icon warn">${icon('zap')}</div>
        <div><div class="stat-val">${(stats.tokens || 0).toLocaleString()}</div><div class="stat-label">近期 Token 消耗</div>
        <div class="stat-note">模型调用 ${stats.workers?.running || 0} 个任务运行中</div></div></div>
    </div>

    ${systemResourceCard(stats.system)}
    <div class="grid grid-2" style="margin-top:16px;grid-template-columns:minmax(0,1.5fr) minmax(0,.9fr)">
      <div class="card">
        <div class="card-head"><h2>最近诊断</h2><span class="sub">点击行查看证据链与报告</span>
          <button class="btn btn-sm" style="margin-left:auto" data-act="all-tasks">全部任务</button></div>
        <div class="card-body tight table-wrap">
          ${stats.recent.length ? `<table class="data"><thead><tr>
            <th>任务</th><th>状态</th><th>根因</th><th>耗时</th><th>提交时间</th></tr></thead><tbody>
            ${stats.recent.map(t => `<tr class="row-hover" data-href="#/tasks/${encodeURIComponent(t.id)}">
              <td><div style="font-weight:600">${esc(t.title)}</div>
                  <div class="mono muted tiny">${esc(shortId(t.id))}</div></td>
              <td>${statusBadge(t.status)}</td>
              <td>${t.verdict?.primary && t.verdict.primary !== 'NONE'
                ? `<span class="mono">${esc(t.verdict.primary)}</span>` : '<span class="muted">—</span>'}</td>
              <td class="nowrap">${fmtDur(t.duration_sec)}</td>
              <td class="muted nowrap">${fmtShortDate(t.created_at)}</td></tr>`).join('')}
            </tbody></table>` : `<div class="empty">${icon('list')}<h3>还没有诊断任务</h3>
              <p>上传日志包并描述故障，平台会并行调用子技能给出证据链。</p>
              <button class="btn btn-primary" data-act="new-task">${icon('plus')}开始第一次诊断</button></div>`}
        </div>
      </div>
      <div class="stack">
        <div class="quick">
          <h2>先定位，再展开</h2>
          <p>先由 AI 基于故障描述和日志结构生成动态波次计划，技能按证据门槛并行执行；证据不足最多补规划一次，再由仲裁与报告收敛。</p>
          <div class="quick-actions">
            <button class="btn btn-primary" data-act="new-task">${icon('play')}新建诊断</button>
            <button class="btn btn-ghost" data-act="skills">${icon('layers')}技能库</button>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><h2>技能健康度</h2>
            <span class="badge ${bad ? 'warn' : 'ok'}" style="margin-left:auto">${ready} 可用 / ${degraded} 降级 / ${bad} 不可用</span></div>
          <div class="card-body">
            <div class="kv">
              <div class="k">并行调度</div><div class="v">${stats.workers?.running || 0} 运行中 · ${stats.workers?.queued || 0} 排队 · 上限 ${stats.workers?.max_workers || '-'}</div>
              <div class="k">技能状态</div><div class="v">${ready ? '脚本流水线就绪' : '暂无可用技能'}</div>
              <div class="k">数据隔离</div><div class="v">每个任务独立工作目录，日志不出本机</div>
            </div>
            ${bad ? `<div class="alert warn" style="margin-top:12px">${icon('alert')}<div>有 ${bad} 个技能不可用/停用，可能导致计划后无法执行。到技能库查看原因。</div></div>` : ''}
          </div>
        </div>
        ${kbStatus ? `<div class="card kb-dashboard-card">
          <div class="card-head"><h2>故障知识库</h2>
            <a class="tiny" href="#/kb" style="margin-left:auto">去查故障</a></div>
          <div class="card-body">
            <div class="grid grid-2" style="gap:10px">
              <div class="mini-stat"><strong>${kbStatus.documents || 0}</strong><span>生效文档</span></div>
              <div class="mini-stat"><strong>${kbStatus.cases || 0}</strong><span>可引用案例</span></div>
              <div class="mini-stat"><strong>${kbStatus.index?.terms || 0}</strong><span>索引词项</span></div>
              <div class="mini-stat"><strong>${kbStatus.drafts || 0}</strong><span>待审核</span></div>
            </div>
            <div class="tiny muted" style="margin-top:10px">索引 ${esc(kbStatus.index?.status || 'empty')} · ${kbStatus.index?.build_ms || 0} ms · ${kbStatus.index?.bytes ? `${Math.round(kbStatus.index.bytes / 1024)} KB` : '尚未构建'}</div>
            <div class="inline" style="margin-top:12px">
              <a class="btn btn-sm" href="#/kb">故障问答</a>
              <a class="btn btn-sm" href="${kbStatus.can_admin ? '#/kb/docs?tab=overview' : '#/kb/docs?tab=docs'}">${kbStatus.can_admin ? '知识库管理' : '浏览文档库'}</a>
              <a class="btn btn-sm btn-ghost" href="#/tasks?tab=kb">操作记录</a>
            </div>
          </div></div>` : ''}
        ${quality ? qualityCard(quality) : ''}
      </div>
    </div>
    <div class="footer-note">BBU FaultScope · 诊断结论均附证据编号，未验证规则会在报告中标注</div>`;

  view.querySelectorAll('[data-href]').forEach((row) => {
    row.addEventListener('click', () => { location.hash = row.dataset.href; });
  });
  view.addEventListener('click', (event) => {
    const act = event.target.closest('[data-act]')?.dataset.act;
    if (act === 'new-task') location.hash = '#/tasks/new';
    if (act === 'skills') location.hash = '#/skills';
    if (act === 'all-tasks') location.hash = '#/tasks';
    if (act === 'model-key') openPersonalModelModal(() => renderDashboard());
    if (act === 'refresh-resources') renderDashboard();
  });
}
