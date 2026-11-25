// 任务详情：SSE 实时时间线、并行技能卡、证据链与报告
import {
  api, bindActions, confirmDialog, esc, fmtDate, fmtDur, fmtSize, icon, mount,
  modal, formValues, setBusy, statusBadge, confidenceBadge, state, toast,
} from './core.js';
import { modelNoticeHtml, openPersonalModelModal } from './personal_model.js';

const STAGES = [
  ['queued', '受理'], ['intake', '盘点'], ['planning', 'AI 规划'],
  ['fanout', '按计划执行'], ['replanning', '补规划'], ['arbitration', '仲裁'],
  ['reporting', '报告'], ['done', '完成'],
];
const STAGE_LABEL = { ...Object.fromEntries(STAGES), routing: '规划（旧版）' };
const GATE_LABEL = {
  always: '无条件', previous_unmatched: '前序未命中', previous_conflict: '前序冲突',
  previous_low_confidence: '前序低置信度', previous_gap: '前序证据缺口',
};
const TERMINAL = ['done', 'failed', 'cancelled', 'needs_data'];

function modeLabel(mode) {
  return { fast: '快速定位', deep: '深度分析', triage: '快速定位', verified: '深度分析' }[mode] || mode || '—';
}

function eventTime(ts) {
  try {
    return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false });
  } catch { return ts || ''; }
}

function stageIndex(task) {
  const stage = task.stage || (task.status === 'queued' ? 'queued' : '');
  const idx = STAGES.findIndex(([key]) => key === stage);
  return idx < 0 ? 0 : idx;
}

function stepperHtml(task) {
  const current = stageIndex(task);
  const terminal = TERMINAL.includes(task.status);
  return `<div class="stepper">${STAGES.map(([key, label], i) => {
    let cls = 'step';
    if (i < current || (terminal && task.status === 'done' && i <= current)) cls += ' done';
    if (i === current && !terminal) cls += ' active';
    if (i === current && terminal) cls += ' done';
    const finished = i < current || (task.status === 'done' && i <= current);
    return `<div class="${cls}"><span class="num">${finished ? '✓' : i + 1}</span>${label}</div>`;
  }).join('')}</div>`;
}

function currentStageText(task) {
  if (task.status === 'queued') return '等待调度';
  if (task.status === 'running') return `正在${STAGE_LABEL[task.stage] || '处理'}`;
  if (task.status === 'needs_data') return '证据不足，等待补充信息';
  if (task.status === 'failed') return '执行失败';
  if (task.status === 'cancelled') return '已取消';
  return '诊断完成';
}

function renderHeader(task) {
  const isAdmin = state.user?.role === 'admin';
  const isOwner = task.username && task.username === state.user?.username;
  const canManage = isAdmin || isOwner;
  const canCancel = canManage && ['queued', 'running'].includes(task.status);
  const canRetry = canManage && !canCancel && task.raw_inputs_available !== false;
  const verdict = task.result?.verdict || task.verdict || {};
  const arbitration = task.result?.arbitration || {};
  return `
  <div class="card">
    <div class="card-body">
      <div class="detail-head">
        <div style="min-width:0">
          <div class="inline" style="margin-bottom:6px">
            ${statusBadge(task.status)}
            <span class="id">${esc(task.id)}</span>
            ${verdict.confidence ? confidenceBadge(verdict.confidence) : ''}
          </div>
          <h2>${esc(task.title)}</h2>
          <div class="detail-meta">
            <span>${icon('clock')} 提交 ${fmtDate(task.created_at)}</span>
            <span>${icon('users')} ${esc(task.username || '—')}</span>
            <span>${icon('cpu')} ${esc(modeLabel(task.mode))}</span>
            <span>${icon('file')} ${(task.inputs || []).length} 个日志包${task.raw_inputs_available === false ? '（原始输入未迁移）' : ''}</span>
            ${task.duration_sec != null ? `<span>${icon('activity')} 耗时 ${fmtDur(task.duration_sec)}</span>` : ''}
            ${task.tokens ? `<span>${icon('zap')} ${task.tokens.toLocaleString()} tokens</span>` : ''}
            ${task.reviewer_name ? `<span>${icon('shield')} 复核专家 ${esc(task.reviewer_name)}</span>` : ''}
          </div>
        </div>
        <div class="detail-actions">
          ${isAdmin ? `<button class="btn" data-act="assign-reviewer">${icon('users')}${task.reviewer_name ? '调整专家' : '分配专家'}</button>` : ''}
          ${canCancel ? `<button class="btn btn-danger" data-act="cancel">${icon('x')}取消任务</button>` : ''}
          ${canRetry ? `<button class="btn" data-act="retry">${icon('refresh')}重跑</button>` : ''}
          ${task.has_report ? `<a class="btn" href="/api/tasks/${encodeURIComponent(task.id)}/report?format=md&download=1">${icon('download')}下载 Markdown</a>
          <a class="btn btn-primary" target="_blank" rel="noopener" href="/api/tasks/${encodeURIComponent(task.id)}/report?format=html">${icon('book')}打开报告</a>` : ''}
        </div>
      </div>
      ${task.error ? `<div class="alert danger" style="margin-top:14px">${icon('alert')}<div><strong>执行失败：</strong>${esc(task.error)}</div></div>` : ''}
      ${task.raw_inputs_available === false ? `<div class="alert warn" style="margin-top:14px">${icon('alert')}<div><strong>原始输入未随迁移包导出：</strong>历史结论、证据和报告可查看，但重跑、案例回流和评测依赖原始日志，已停用；如需重新分析请上传日志新建任务。</div></div>` : ''}
      ${modelNoticeHtml(task.model_status?.notice, { compact: true })}
      <div class="progress-track"><div class="progress-fill" style="width:${Math.max(2, Number(task.progress || 0))}%"></div></div>
      <div class="between" style="margin-top:7px">
        <span class="small muted">${esc(currentStageText(task))} · ${Number(task.progress || 0)}%</span>
        <span class="small muted">${arbitration.reason ? esc(arbitration.reason) : ''}</span>
      </div>
      ${stepperHtml(task)}
    </div>
  </div>`;
}

function planStepStatus(status) {
  const map = {
    done: ['完成', 'ok'], running: ['执行中', 'run'], skipped: ['跳过', 'skip'],
    failed: ['失败', 'bad'], planned: ['待执行', 'wait'],
  };
  return map[status] || [status || '待执行', 'wait'];
}

function planPanel(task) {
  const plan = task.result?.plan || {};
  const replan = plan.replan || task.result?.replan;
  const steps = plan.steps || [];
  const source = plan.source === 'ai' ? 'AI 自主规划' : plan.source === 'deterministic' ? '确定性回退' : '未生成';
  const refs = plan.experience_refs || task.result?.experiences || [];
  if (!steps.length && !replan) {
    return `<div class="card" style="margin-top:16px">
      <div class="card-head"><h2>AI 诊断计划</h2><span class="sub">${esc(source)}</span></div>
      <div class="card-body"><div class="empty" style="padding:22px">${icon('cpu')}
        <p>本次没有可执行计划；未配置真实模型时平台走确定性回退，或当前技能库无匹配能力。</p></div></div>
    </div>`;
  }
  return `<div class="card" style="margin-top:16px">
    <div class="card-head"><h2>AI 诊断计划</h2>
      <span class="sub">${esc(source)}${plan.model ? ` · ${esc(plan.model)}` : ''}${refs.length ? ` · 参考 ${refs.length} 条历史经验` : ''}</span>
    </div>
    <div class="card-body">
      <div class="plan-summary">${esc(plan.summary || '未给出计划摘要')}</div>
      ${(plan.hypothesis || []).length ? `<div class="plan-hypo">${(plan.hypothesis || []).map((h) => `<span class="chip">${esc(h)}</span>`).join('')}</div>` : ''}
      <div class="plan-flow">${steps.map((s) => {
        const [label, tone] = planStepStatus(s.status);
        return `<div class="plan-step ${esc(tone)}">
          <div class="plan-step-head"><span class="plan-id">${esc(s.id || 'P')}</span>
            <strong>${esc(s.title || '未命名步骤')}</strong>
            <span class="plan-state ${esc(tone)}">${esc(label)}</span></div>
          <div class="small muted">${esc(s.objective || '')}</div>
          <div class="plan-skills">${(s.skills || []).map((x) => `<span class="chip alias">${esc(x)}</span>`).join('')}</div>
          <div class="tiny muted">门槛：${esc(s.gate_label || GATE_LABEL[s.gate] || s.gate || '无条件')}${s.reason ? ` · ${esc(s.reason)}` : ''}</div>
        </div>`;
      }).join('')}</div>
      ${(replan?.steps || []).length ? `<div class="plan-replan-label">补规划新增波次</div>
        <div class="plan-flow">${(replan.steps || []).map((s) => {
          const [label, tone] = planStepStatus(s.status);
          return `<div class="plan-step ${esc(tone)}">
            <div class="plan-step-head"><span class="plan-id">${esc(s.id || 'P')}</span>
              <strong>${esc(s.title || '补规划步骤')}</strong>
              <span class="plan-state ${esc(tone)}">${esc(label)}</span></div>
            <div class="small muted">${esc(s.objective || '')}</div>
            <div class="plan-skills">${(s.skills || []).map((x) => `<span class="chip alias">${esc(x)}</span>`).join('')}</div>
            <div class="tiny muted">门槛：${esc(s.gate_label || GATE_LABEL[s.gate] || s.gate || '无条件')}${s.reason ? ` · ${esc(s.reason)}` : ''}</div>
          </div>`;
        }).join('')}</div>` : ''}
      ${replan ? `<div class="plan-replan ${esc(replan.status || '')}">
        <strong>补规划</strong><span>${esc({executed: '已执行可补证据的新技能', no_new_skills: '未发现可补技能，停止扩展', not_needed: '证据已足够，不补规划', failed: '补规划失败，按现有证据收敛', disabled: '未启用'}[replan.status] || replan.status || '—')}</span>
        <span class="muted">${esc(replan.reason || replan.summary || '')}</span></div>` : ''}
      ${((plan.plan_notes || []).length) ? `<div class="plan-notes">${(plan.plan_notes || []).slice(0, 3).map((n) => `<span>${esc(n)}</span>`).join('')}</div>` : ''}
      ${task.result?.learning ? `<div class="plan-replan ${esc(task.result.learning.status || '')}">
        <strong>自学习</strong><span>${esc({draft_created: '已创建工坊草稿，等待人工确认', existing: '该任务已沉淀过草稿', duplicate_root: '已有同根因草稿，不再重复创建', draft_created_analysis_failed: '草稿已建，日志分析失败', skipped: '未满足沉淀条件', failed: '沉淀失败'}[task.result.learning.status] || task.result.learning.status || '—')}</span>
        <span class="muted">${esc(task.result.learning.reason || '')}</span></div>` : ''}
    </div>
  </div>`;
}

function stageTimelinePanel(task) {
  const timeline = task.stage_timeline || [];
  if (!timeline.length) return '';
  return `<div class="card" style="margin-top:16px">
    <div class="card-head"><h2>阶段时间线</h2><span class="sub">受理 → 盘点 → 规划 → 执行 → 仲裁 → 报告 → 完成</span></div>
    <div class="card-body">
      <div class="stage-timeline">
        ${timeline.map((s, i) => `<div class="stage-step ${esc(s.status || 'pending')}">
          <div class="stage-dot">${s.status === 'done' ? icon('check') : s.status === 'active' ? icon('refresh') : `${i + 1}`}</div>
          <div class="stage-info">
            <div class="between"><strong>${esc(s.label || s.stage)}</strong>
              <span class="tiny muted">${s.duration_sec != null ? fmtDur(s.duration_sec) : s.status === 'active' ? '进行中' : '—'}</span></div>
            <div class="tiny muted">${s.start ? `开始 ${fmtDate(s.start)}` : '尚未开始'}${s.end ? ` · 结束 ${fmtDate(s.end)}` : ''}</div>
          </div>
        </div>`).join('')}
      </div>
      ${task.next_action ? `<div class="alert info" style="margin-top:14px">${icon('info')}<div><strong>下一步：</strong>${esc(task.next_action)}</div></div>` : ''}
    </div></div>`;
}

function verdictPanel(task) {
  const arbitration = task.result?.arbitration || {};
  const verdict = task.result?.verdict || task.verdict || {};
  const status = verdict.status || 'unmatched';
  const titles = {
    matched: '已定位根因', multiple_causes: '检测到多个独立根因',
    unknown_fault: '发现异常，但未匹配已知根因',
    temporal_conflict: '命中规则但时序不满足',
    insufficient: '证据不足以定论', unmatched: '未匹配到已知根因', needs_data: '需要补充现场信息',
  };
  const primary = arbitration.root_cause_name || verdict.primary;
  const skill = arbitration.chosen_skill || verdict.skill;
  const reason = arbitration.reason || '';
  const missing = arbitration.missing_info || [];
  const evCount = (arbitration.candidates || []).reduce((n, c) => n + (c.evidence || []).length, 0);
  const quality = arbitration.evidence_quality || {};
  const roots = arbitration.roots || [];
  return `<div class="card">
    <div class="card-head"><h2>诊断结论</h2>${statusBadge(status)}</div>
    <div class="card-body">
      <div class="verdict-box">
        <div class="label">${esc(titles[status] || '诊断结论')}</div>
        <div class="value">${esc(primary || (status === 'matched' ? '—' : '不做强行归因'))}</div>
        <div class="reason">${esc(reason || '尚未产生仲裁结论')}</div>
      </div>
      <div class="kv" style="margin-top:14px">
        <div class="k">采信技能</div><div class="v">${esc(skill || '—')}</div>
        <div class="k">置信度</div><div class="v">${verdict.confidence ? confidenceBadge(verdict.confidence) : '—'}</div>
        ${quality.quality_score != null ? `<div class="k">证据质量</div><div class="v">${quality.quality_score}/100 · ${quality.unique_clusters || 0} 组去重 · ${(quality.independent_files || []).length} 个来源文件</div>` : ''}
        ${roots.length > 1 ? `<div class="k">并行根因</div><div class="v">${roots.map((r) => `<span class="chip" style="margin:0 4px 4px 0">${esc(r.name)} · ${r.quality_score}/100</span>`).join('')}</div>` : ''}
        <div class="k">证据条目</div><div class="v">${evCount} 条（来自仲裁候选）</div>
        <div class="k">仲裁方式</div><div class="v">${arbitration.llm_used ? '模型仲裁（有界调用）' : '确定性仲裁'}</div>
        <div class="k">参与技能</div><div class="v">${(task.skills || []).length} 个</div>
      </div>
      ${missing.length ? `<div class="alert warn" style="margin-top:14px">${icon('alert')}<div><strong>建议补充：</strong><ul>${missing.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></div></div>` : ''}
      <details class="why-box">
        <summary>为什么得出这个结论？</summary>
        <div class="small" style="margin-top:8px">
          <p>${esc(reason || '当前没有仲裁理由；请结合证据链与技能命中情况判断。')}</p>
          <div class="kv">
            <div class="k">候选技能</div><div class="v">${(arbitration.candidates || []).length} 个</div>
            <div class="k">证据条目</div><div class="v">${evCount} 条</div>
            <div class="k">证据质量</div><div class="v">${quality.quality_score != null ? `${quality.quality_score}/100` : '未评估'}</div>
            <div class="k">独立来源</div><div class="v">${(quality.independent_files || []).length ? esc((quality.independent_files || []).join('、')) : '—'}</div>
            <div class="k">仲裁方式</div><div class="v">${arbitration.llm_used ? '模型仲裁（有界调用）' : '确定性规则仲裁'}</div>
          </div>
          <p class="muted tiny" style="margin-bottom:0">结论、置信度与证据链由仲裁器联合评分；低置信度或证据缺口不会被强行包装成确定根因。</p>
        </div>
      </details>
    </div>
  </div>`;
}

async function openAssignReviewer(task, onDone) {
  let users = [];
  try {
    const data = await api('/api/users?role=reviewer&status=active&page_size=100');
    users = data.items || [];
  } catch (err) { toast(err.message, 'error'); return; }
  const m = modal({
    title: '分配复核专家',
    body: `<div class="alert info" style="margin-bottom:12px">${icon('shield')}<div>专家只能查看被分配的任务并提交复核，不能创建任务或管理平台。</div></div>
      <form id="assign-form"><div class="form-row"><label class="label">复核专家</label>
        <select class="select" name="reviewer_id"><option value="">不分配</option>
          ${users.map((u) => `<option value="${u.id}" ${task.reviewer_id === u.id ? 'selected' : ''}>${esc(u.display_name || u.username)} (${esc(u.username)})${u.team ? ' · ' + esc(u.team) : ''}</option>`).join('')}
        </select></div></form>
      ${users.length ? '' : '<div class="alert warn">暂无启用的专家账号，请先在用户管理中邀请或创建 reviewer。</div>'}`,
    footer: `<button class="btn" data-act="cancel">取消</button><button class="btn btn-primary" data-act="save">保存</button>`,
  });
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel"]')) m.close();
    if (e.target.closest('[data-act="save"]')) {
      const value = m.body.querySelector('[name="reviewer_id"]').value;
      const btn = e.target.closest('[data-act="save"]'); setBusy(btn, true, '保存中…');
      try {
        await api(`/api/tasks/${encodeURIComponent(task.id)}/assign-reviewer`, { method: 'POST',
          body: { reviewer_id: value ? Number(value) : null } });
        toast(value ? '已分配复核专家' : '已取消分配', 'ok');
        m.close(); onDone && onDone();
      } catch (err) { toast(err.message, 'error'); setBusy(btn, false); }
    }
  });
}

function reviewPanel(task) {
  if (!['done', 'needs_data', 'failed'].includes(task.status)) return '';
  const review = task.review;
  const decisionLabel = { confirm: '确认结论', correct: '专家改判', reject: '驳回结论' };
  const actionLabel = { effective: '处置有效', ineffective: '处置无效', unknown: '尚未验证' };
  if (review) {
    return `<div class="card" style="margin-top:16px">
      <div class="card-head"><h2>专家复核</h2><span class="sub">未复核经验不会进入规划</span></div>
      <div class="card-body">
        <div class="kv">
          <div class="k">复核结论</div><div class="v"><strong>${esc(decisionLabel[review.decision] || review.decision)}</strong> · ${esc(review.reviewer_name || '')}</div>
          <div class="k">修正根因</div><div class="v">${esc(review.corrected_root || '—')}</div>
          <div class="k">处置结果</div><div class="v">${esc(actionLabel[review.action_result] || review.action_result || '—')}</div>
          <div class="k">复核时间</div><div class="v">${esc(fmtDate(review.updated_at))}</div>
          ${review.real_case_id ? `<div class="k">真实案例</div><div class="v"><span class="chip">${esc(review.real_case_id)}</span></div>` : ''}
        </div>
        ${review.comment ? `<div class="alert info" style="margin-top:12px">${icon('book')}<div>${esc(review.comment)}</div></div>` : ''}
        ${review.case_error ? `<div class="alert warn" style="margin-top:12px">${icon('alert')}<div>案例回流失败：${esc(review.case_error)}</div></div>` : ''}
        <div style="margin-top:12px"><button class="btn btn-sm" data-act="review">修改复核</button></div>
      </div>
    </div>`;
  }
  return `<div class="card" style="margin-top:16px">
    <div class="card-head"><h2>专家复核</h2><span class="sub">确认后才进入经验库</span></div>
    <div class="card-body">
      <div class="alert warn">${icon('alert')}<div>当前结论尚未经现场确认。请核对证据后选择确认、改判或驳回；勾选“回流案例”可把本次输入转为真实案例。</div></div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-primary" data-act="review" data-decision="confirm">${icon('check')}确认结论</button>
        <button class="btn" data-act="review" data-decision="correct">${icon('edit')}改判根因</button>
        <button class="btn btn-danger" data-act="review" data-decision="reject">${icon('x')}驳回结论</button>
      </div>
    </div>
  </div>`;
}

function openReviewModal(task, decision = 'confirm') {
  const arbitration = task.result?.arbitration || {};
  const root = arbitration.root_cause_name || task.verdict?.root_cause_name || '';
  const skill = arbitration.chosen_skill || '';
  const inputsAvailable = task.raw_inputs_available !== false;
  const m = modal({
    title: '专家复核',
    body: `<form id="review-form" class="stack" style="gap:12px">
      <div class="form-row"><label class="label">复核结论</label>
        <select name="decision" class="input">
          <option value="confirm" ${decision === 'confirm' ? 'selected' : ''}>确认结论</option>
          <option value="correct" ${decision === 'correct' ? 'selected' : ''}>改判根因</option>
          <option value="reject" ${decision === 'reject' ? 'selected' : ''}>驳回结论</option>
        </select></div>
      <div class="form-row"><label class="label">修正根因</label>
        <input class="input" name="corrected_root" value="${esc(root)}" placeholder="改判时必填；确认时默认为原根因">
        <div class="help">确认时留空则使用原结论；改判时请填写真实根因。</div></div>
      <div class="form-row"><label class="label">对应技能（可选）</label>
        <input class="input" name="corrected_skill" value="${esc(skill)}"></div>
      <div class="form-row"><label class="label">处置结果</label>
        <select name="action_result" class="input">
          <option value="unknown">尚未验证</option><option value="effective">处置有效</option>
          <option value="ineffective">处置无效</option>
        </select></div>
      <div class="form-row"><label class="label">复核说明</label>
        <textarea class="input" name="comment" rows="3" placeholder="现场现象、实际处置与判断依据；驳回时写明反证。"></textarea></div>
      <label class="check"><input type="checkbox" name="promote_to_case" value="1" ${inputsAvailable ? 'checked' : 'disabled'}>
        把本次输入回流为真实案例（dev 集，需后续提升为 golden）</label>
      ${inputsAvailable ? '' : `<div class="alert warn">${icon('alert')}<div>原始输入未随迁移包导出，本次复核不能回流案例；请取消勾选后只保存复核结论。</div></div>`}
    </form>`,
    footer: `<span class="small muted" id="review-draft-status" style="margin-right:auto">输入后自动保存复核草稿</span>
      <button class="btn" data-act="cancel-review">取消</button>
      <button class="btn btn-primary" data-act="submit-review">提交复核</button>`,
  });
  const reviewForm = m.body.querySelector('#review-form');
  const draftKey = `bbu.reviewDraft.${task.id}`;
  try {
    const saved = JSON.parse(localStorage.getItem(draftKey) || 'null');
    if (saved) {
      for (const [name, value] of Object.entries(saved)) {
        const el = reviewForm.querySelector(`[name="${name}"]`);
        if (el && name !== 'promote_to_case') el.value = value;
      }
      const promote = reviewForm.querySelector('[name="promote_to_case"]');
      if (promote) promote.checked = inputsAvailable && saved.promote_to_case !== false;
      m.foot.querySelector('#review-draft-status').textContent = '已恢复复核草稿';
    }
  } catch { /* ignore */ }
  let reviewTimer = null;
  const saveReviewDraft = () => {
    clearTimeout(reviewTimer);
    reviewTimer = setTimeout(() => {
      const values = formValues(reviewForm);
      values.promote_to_case = !!reviewForm.querySelector('[name="promote_to_case"]').checked;
      try { localStorage.setItem(draftKey, JSON.stringify(values)); } catch { /* ignore */ }
      const status = m.foot.querySelector('#review-draft-status');
      if (status) status.textContent = '复核草稿已自动保存';
    }, 300);
  };
  reviewForm.addEventListener('input', saveReviewDraft);
  reviewForm.addEventListener('change', saveReviewDraft);
  m.mask.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="cancel-review"]')) { m.close(); return; }
    const btn = e.target.closest('[data-act="submit-review"]');
    if (!btn) return;
    const form = m.body.querySelector('#review-form');
    const values = formValues(form);
    values.promote_to_case = !!form.querySelector('[name="promote_to_case"]').checked;
    if (!inputsAvailable && values.promote_to_case) { toast('原始输入未迁移，不能回流案例', 'error'); return; }
    if (values.decision === 'correct' && !values.corrected_root?.trim()) {
      toast('改判必须填写修正后的根因', 'error'); return;
    }
    setBusy(btn, true, '提交中');
    try {
      await api(`/api/tasks/${encodeURIComponent(task.id)}/review`, { method: 'POST', body: values });
      try { localStorage.removeItem(draftKey); } catch { /* ignore */ }
      m.close();
      toast('复核已保存', 'ok');
      task.review = (await api(`/api/tasks/${encodeURIComponent(task.id)}`)).review;
      location.reload();
    } catch (err) {
      setBusy(btn, false);
      toast(err.message, 'error');
    }
  });
}


function skillCards(task) {
  if (!task.skills?.length) {
    return `<div class="empty" style="padding:24px">${icon('layers')}<p>AI 计划生成后显示参与诊断的并行技能</p></div>`;
  }
  return `<div class="stack">${task.skills.map((s) => {
    const res = s.result || {};
    const verdict = s.verdict || res.verdict || {};
    const root = res.root_cause || {};
    const status = res.status || s.status;
    const failed = ['failed', 'timeout', 'incompatible'].includes(s.status);
    const rules = verdict.groups?.ALL?.hit_rules || [];
    const duration = s.stats?.duration_ms;
    return `<div class="skill-card ${failed ? 'failed' : ''}">
      <div class="top">
        <strong>${esc(s.skill_name)}</strong>
        ${statusBadge(s.status)}
        ${statusBadge(verdict.status || 'skipped', verdict.status === 'matched' ? '命中' : verdict.status === 'temporal_conflict' ? '时序冲突' : '未命中')}
        <span class="score" style="margin-left:auto">匹配分 ${Number(s.score || 0).toFixed(1)}</span>
      </div>
      <div class="small" style="margin-top:6px">
        ${root.name ? `<strong>${esc(root.name)}</strong>${root.confidence ? ` · ${esc({ high: '高', medium: '中', low: '低' }[root.confidence] || root.confidence)}置信` : ''}` : '<span class="muted">未给出根因结论</span>'}
      </div>
      ${rules.length ? `<div style="margin-top:6px">${rules.slice(0, 6).map((r) => `<span class="chip" style="margin:0 3px 3px 0">${esc(r)}</span>`).join('')}</div>` : ''}
      ${s.error ? `<div class="err">${icon('alert')} ${esc(s.error)}</div>` : ''}
      <div class="between" style="margin-top:8px">
        <span class="tiny muted">${duration != null ? `耗时 ${(duration / 1000).toFixed(1)}s` : ''}
          ${res.evidence?.length ? ` · ${res.evidence.length} 条证据` : ''}</span>
        ${s.has_report ? `<a class="link-btn" target="_blank" rel="noopener"
          href="/api/tasks/${encodeURIComponent(task.id)}/skills/${encodeURIComponent(s.skill_name)}/report?format=html">查看技能报告 ${icon('chevron')}</a>` : ''}
      </div>
    </div>`;
  }).join('')}</div>`;
}

function evidenceTable(task) {
  const candidates = task.result?.arbitration?.candidates || [];
  const rows = [];
  let n = 0;
  for (const c of candidates) {
    for (const e of c.evidence || []) {
      n += 1;
      rows.push({ n, skill: c.skill, ...e });
    }
  }
  if (!rows.length) {
    return `<div class="empty" style="padding:24px">${icon('search')}<p>暂无可定位证据，或技能未命中规则</p></div>`;
  }
  return `<div class="table-wrap"><table class="data evidence-table"><thead><tr>
    <th>#</th><th>规则</th><th>文件:行</th><th>片段</th></tr></thead><tbody>
    ${rows.slice(0, 30).map((r) => `<tr>
      <td><span class="cite">E${r.n}</span></td>
      <td><span class="mono">${esc(r.rule_id || '—')}</span><div class="tiny muted">${esc(r.skill)}</div></td>
      <td class="mono tiny">${esc(r.file || '—')}${r.line_no ? `:${r.line_no}` : ''}</td>
      <td class="small">${esc((r.text || '').slice(0, 180))}${(r.text || '').length > 180 ? '…' : ''}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

function timelineHtml(events) {
  if (!events?.length) return `<div class="timeline" id="timeline"><div class="ev"><span class="msg muted">等待调度事件…</span></div></div>`;
  return `<div class="timeline" id="timeline">${events.map(eventLine).join('')}</div>`;
}

function eventLine(ev) {
  const level = ev.level || 'info';
  return `<div class="ev" data-seq="${ev.seq}"><span class="t">${eventTime(ev.ts)}</span>
    <span class="lv ${esc(level)}">${esc(level)}</span>
    <span class="msg">${esc(ev.message)}${ev.stage ? ` <span class="stage-tag">[${esc(STAGE_LABEL[ev.stage] || ev.stage)}]</span>` : ''}</span></div>`;
}

const DETAIL_TABS = [
  ['overview', '诊断概览'],
  ['evidence', '证据与技能'],
  ['report', '诊断报告'],
  ['runtime', '运行详情'],
];

function renderBody(task, activeTab = 'overview') {
  const inventory = task.result?.inventory || {};
  const hints = inventory.detected_hints || [];
  const files = task.inputs || [];
  const tabBtn = (key, label) => `<button type="button" class="${activeTab === key ? 'active' : ''}" data-detail-tab="${key}">${label}</button>`;
  return `
  <nav class="tabs detail-tabs" aria-label="任务详情分区" style="margin-top:16px">${DETAIL_TABS.map(([k, v]) => tabBtn(k, v)).join('')}</nav>
  <section class="detail-panel ${activeTab === 'overview' ? '' : 'hide'}" data-detail-panel="overview">
    <div class="grid" style="grid-template-columns:minmax(0,1.45fr) minmax(0,.75fr);margin-top:16px">
      <div class="card">
        <div class="card-head"><h2>现场描述</h2><span class="sub">用户提交的原始信息</span></div>
        <div class="card-body"><div style="white-space:pre-wrap;font-size:13px;line-height:1.8">${esc(task.description || '（无描述）')}</div></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>日志盘点</h2><span class="sub">${inventory.total_members != null ? `${inventory.total_members} 个成员` : `${files.length} 个压缩包`}</span></div>
        <div class="card-body">
          ${files.length ? `<div class="stack" style="margin-bottom:10px">${files.map((f) => `<div class="file-item">${icon('file')}
            <span class="name">${esc(f.name)}</span><span class="muted tiny">${fmtSize(f.size)}</span></div>`).join('')}</div>` : `<div class="muted small">${task.raw_inputs_available === false ? '原始日志已释放或未随迁移包导出，仅保留盘点摘要与派生报告' : '没有日志包'}</div>`}
          ${hints.length ? `<div>${hints.slice(0, 12).map((h) => `<span class="chip alias" style="margin:0 4px 4px 0">${esc(h)}</span>`).join('')}</div>` : ''}
          ${inventory.total_uncompressed_bytes ? `<div class="help">解压后 ${fmtSize(inventory.total_uncompressed_bytes)}</div>` : ''}
        </div>
      </div>
    </div>
    ${verdictPanel(task)}
    ${planPanel(task)}
    ${reviewPanel(task)}
  </section>

  <section class="detail-panel ${activeTab === 'evidence' ? '' : 'hide'}" data-detail-panel="evidence">
    <div class="detail-grid">
      <div class="stack">
        <div class="card">
          <div class="card-head"><h2>并行技能</h2><span class="sub">${(task.skills || []).length} 个技能参与</span></div>
          <div class="card-body">${skillCards(task)}</div>
        </div>
      </div>
      <div class="stack">
        <div class="card">
          <div class="card-head"><h2>证据链</h2><span class="sub">可回查 文件:行</span></div>
          <div class="card-body tight">${evidenceTable(task)}</div>
        </div>
      </div>
    </div>
  </section>

  <section class="detail-panel ${activeTab === 'report' ? '' : 'hide'}" data-detail-panel="report">
    ${task.has_report ? reportPanel(task) : `<div class="card" style="margin-top:16px">
      <div class="card-head"><h2>诊断报告</h2><span class="sub">诊断完成后自动生成</span></div>
      <div class="card-body"><div class="empty" style="padding:28px">${icon('book')}<h3>报告尚未生成</h3>
        <p>${task.status === 'running' ? '任务运行中，完成后这里会出现完整报告。' : '未生成报告，请检查任务状态或重跑。'}</p></div></div>
    </div>`}
  </section>

  <section class="detail-panel ${activeTab === 'runtime' ? '' : 'hide'}" data-detail-panel="runtime">
    ${stageTimelinePanel(task)}
    <div class="card" style="margin-top:16px">
      <div class="card-head"><h2>执行时间线</h2><span class="sub">实时事件（SSE）</span></div>
      <div class="card-body">${timelineHtml(task.events)}</div>
    </div>
  </section>`;
}

function reportPanel(task) {
  return `<div class="card" style="margin-top:16px">
    <div class="card-head"><h2>诊断报告</h2>
      <div class="report-tabs" style="margin-left:auto">
        <button class="btn btn-sm btn-primary" data-report-tab="html">HTML 报告</button>
        <button class="btn btn-sm" data-report-tab="md">Markdown 原文</button>
        <a class="btn btn-sm" href="/api/tasks/${encodeURIComponent(task.id)}/report?format=html&download=1">${icon('download')}下载 HTML</a>
      </div>
    </div>
    <div class="card-body" id="report-html"><div class="empty" style="padding:24px" data-report-placeholder>${icon('book')}<h3>报告已生成</h3><p>切换到 HTML 视图后加载。</p></div></div>
    <div class="card-body hide" id="report-md">
      <pre class="check-output" id="report-md-text" style="max-height:720px;white-space:pre-wrap">加载中…</pre>
    </div>
  </div>`;
}

export async function renderTaskDetail(taskId) {
  const view = mount('任务详情', '证据链、并行技能与诊断报告', '');
  view.innerHTML = `<div class="skeleton" style="height:180px;margin-bottom:16px"></div>
    <div class="skeleton" style="height:320px"></div>`;
  let task = null;
  let stream = null;
  let pollTimer = null;
  let terminal = false;
  let seen = new Set();
  let lastSeq = 0;
  let activeDetailTab = null;
  let reportView = 'html';

  function loadReportHtml() {
    if (!task?.has_report) return;
    const host = view.querySelector('#report-html');
    if (!host || host.querySelector('iframe')) return;
    host.innerHTML = `<iframe class="report-frame" src="/api/tasks/${encodeURIComponent(task.id)}/report?format=html" title="诊断报告" sandbox=""></iframe>`;
  }

  async function loadReportMarkdown() {
    if (!task?.has_report) return;
    const pre = view.querySelector('#report-md-text');
    if (!pre || pre.dataset.loaded) return;
    try {
      const res = await api(`/api/tasks/${encodeURIComponent(task.id)}/report?format=md`, { raw: true });
      pre.textContent = await res.text();
      pre.dataset.loaded = '1';
    } catch (err) { pre.textContent = `加载失败：${err.message}`; }
  }

  function setReportView(mode) {
    reportView = mode;
    view.querySelectorAll('[data-report-tab]').forEach((b) => b.classList.toggle('btn-primary', b.dataset.reportTab === mode));
    view.querySelector('#report-html')?.classList.toggle('hide', mode !== 'html');
    view.querySelector('#report-md')?.classList.toggle('hide', mode !== 'md');
    if (mode === 'html') loadReportHtml();
    else loadReportMarkdown();
  }

  function setDetailTab(name) {
    activeDetailTab = name;
    view.querySelectorAll('[data-detail-tab]').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.detailTab === name);
    });
    view.querySelectorAll('[data-detail-panel]').forEach((panel) => {
      panel.classList.toggle('hide', panel.dataset.detailPanel !== name);
    });
    if (name === 'report') setReportView(reportView);
  }

  function render() {
    if (!task) return;
    if (!activeDetailTab) {
      activeDetailTab = task.status === 'done' && task.has_report ? 'report' : 'overview';
    }
    const oldTimeline = view.querySelector('#timeline');
    const wasAtBottom = oldTimeline ? (oldTimeline.scrollHeight - oldTimeline.scrollTop - oldTimeline.clientHeight < 40) : true;
    const scrollTop = oldTimeline ? oldTimeline.scrollTop : 0;
    const pageScroll = window.scrollY;
    view.innerHTML = renderHeader(task) + renderBody(task, activeDetailTab);
    const newTimeline = view.querySelector('#timeline');
    if (newTimeline) {
      newTimeline.scrollTop = wasAtBottom ? newTimeline.scrollHeight : scrollTop;
    }
    window.scrollTo(0, pageScroll);
    bindActions(view, {
      cancel: async () => {
        if (!(await confirmDialog('取消后任务会立即停止，已产生的中间结果会保留。确定取消？', { danger: true, title: '取消任务' }))) return;
        try { await api(`/api/tasks/${encodeURIComponent(task.id)}/cancel`, { method: 'POST' }); toast('已提交取消请求', 'warn'); }
        catch (err) { toast(err.message, 'error'); }
      },
      retry: async () => {
        try {
          const data = await api(`/api/tasks/${encodeURIComponent(task.id)}/retry`, { method: 'POST' });
          toast('已创建重跑任务', 'ok');
          location.hash = `#/tasks/${encodeURIComponent(data.id)}`;
        } catch (err) { toast(err.message, 'error'); }
      },
      review: (el) => openReviewModal(task, el.dataset.decision || 'confirm'),
      'assign-reviewer': () => openAssignReviewer(task, () => renderTaskDetail(task.id)),
      'model-key': () => openPersonalModelModal(() => renderTaskDetail(task.id)),
    });
    view.querySelectorAll('[data-detail-tab]').forEach((btn) => {
      btn.addEventListener('click', () => setDetailTab(btn.dataset.detailTab));
    });
    view.querySelectorAll('[data-report-tab]').forEach((btn) => {
      btn.addEventListener('click', () => setReportView(btn.dataset.reportTab));
    });
    setDetailTab(activeDetailTab);
  }

  function appendEvent(ev) {
    if (seen.has(ev.seq)) return;
    seen.add(ev.seq);
    lastSeq = Math.max(lastSeq, ev.seq);
    const timeline = view.querySelector('#timeline');
    if (!timeline) { refresh(); return; }
    const placeholder = timeline.querySelector('.msg.muted');
    if (placeholder && timeline.querySelectorAll('.ev').length === 1) timeline.innerHTML = '';
    timeline.insertAdjacentHTML('beforeend', eventLine(ev));
    const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 60;
    if (atBottom) timeline.scrollTop = timeline.scrollHeight;
    if (ev.stage === 'done' || ev.message.includes('诊断完成')) {
      terminal = true;
      if (stream) { stream.close(); stream = null; }
      setTimeout(refresh, 400);
    }
  }

  async function refresh() {
    try {
      const data = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
      task = data;
      const maxSeq = Math.max(0, ...(data.events || []).map((e) => e.seq));
      lastSeq = Math.max(lastSeq, maxSeq);
      (data.events || []).forEach((e) => seen.add(e.seq));
      render();
      if (TERMINAL.includes(data.status)) {
        terminal = true;
        if (stream) { stream.close(); stream = null; }
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      } else {
        connectStream();
      }
    } catch (err) {
      view.innerHTML = `<div class="alert danger">${icon('alert')}<div>${esc(err.message)}</div>
        <button class="btn btn-sm" data-act="back">返回任务列表</button></div>`;
      bindActions(view, { back: () => { location.hash = '#/tasks'; } });
    }
  }

  function connectStream() {
    if (stream || terminal) return;
    stream = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/stream?after=${lastSeq}`);
    stream.addEventListener('task', (e) => {
      try { appendEvent(JSON.parse(e.data)); } catch { /* ignore */ }
    });
    stream.onerror = () => {
      if (stream) { stream.close(); stream = null; }
      if (!terminal && !pollTimer) {
        pollTimer = setInterval(refresh, 4000);
      }
    };
  }

  state.cleanup = () => {
    if (stream) { stream.close(); stream = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };

  await refresh();
  return view;
}
