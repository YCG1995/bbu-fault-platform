"""马甲包报告：确定性骨架 + 有界 LLM 综合研判 + 自包含 HTML。"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .util import human_size, local_display, truncate

CITE_RE = re.compile(r"\[E(\d+)\]")


def compose_markdown(task: dict[str, Any], results: list[dict[str, Any]],
                     arbitration: dict[str, Any], inventory: dict[str, Any],
                     stats: dict[str, Any], narrative: dict[str, Any] | None = None,
                     plan: dict[str, Any] | None = None) -> str:
    chosen = arbitration.get("chosen_skill") or ""
    chosen_result = next((r for r in results if r["skill_name"] == chosen), None)
    root = (chosen_result or {}).get("root_cause") or {}
    status = arbitration.get("status") or "unmatched"
    confidence = arbitration.get("confidence") or root.get("confidence") or "medium"
    verdict_label = {
        "matched": "已匹配已知根因",
        "multiple_causes": "检测到多个独立根因",
        "unknown_fault": "发现异常但未匹配已知根因",
        "temporal_conflict": "条件成立但时序冲突",
        "unmatched": "未匹配已知根因",
        "insufficient": "证据不足，无法定论",
        "needs_data": "缺少关键数据",
    }.get(status, status)

    lines: list[str] = []
    lines.append("# BBU 故障诊断报告")
    lines.append("")
    lines.append(f"> 任务 `{task.get('id')}` ｜ 提交人 {task.get('username') or '-'} ｜ "
                 f"提交时间 {local_display(task.get('created_at'))} ｜ 模式 "
                 f"{ {'deep': '深度分析', 'skill': '指定技能'}.get(task.get('mode'), '快速定位') }")
    lines.append("")
    lines.append("## 一、结论")
    lines.append("")
    if chosen_result and root:
        lines.append(f"- **根因**：{root.get('name')}（`{root.get('id')}`，技能 `{chosen}`）")
        if status == "multiple_causes":
            roots = arbitration.get("roots") or []
            if roots:
                lines.append("- **并行根因**：")
                for idx, item in enumerate(roots[:4], 1):
                    lines.append(
                        f"  {idx}. {item.get('name')}（技能 `{item.get('skill')}`，"
                        f"证据质量 {item.get('quality_score', 0)}/100，{item.get('confidence')}）")
        lines.append(f"- **判定**：{verdict_label}")
        lines.append(f"- **置信度**：{confidence}")
        cal = arbitration.get("calibration") or {}
        if cal:
            acc = cal.get("accuracy")
            line = f"- **置信度校准**：{cal.get('status')}"
            if acc is not None:
                ci = cal.get("accuracy_ci95") or []
                line += (f"（{cal.get('level')} 档 dev 准确率 {acc}"
                         + (f"，95% CI {ci[0]}–{ci[1]}" if len(ci) == 2 else "")
                         + f"，n={cal.get('sample_count', 0)}）")
            else:
                line += f"（{cal.get('note') or '样本不足，不显示伪精确概率'}）"
            lines.append(line)
        cards = arbitration.get("decision_cards") or []
        if cards:
            lines.append("")
            lines.append("### 根因决策卡")
            for card in cards[:4]:
                lines.append(f"- **{card.get('root_cause')}**（{card.get('state')}，"
                             f"必要条件 {len(card.get('necessary_evidence') or [])} 条，"
                             f"反证 {len(card.get('counter_evidence') or [])} 条）")
                for item in (card.get("necessary_evidence") or [])[:4]:
                    lines.append(f"  - {item.get('rule')}：{'已命中' if item.get('present') else '未命中'} "
                                 f"{item.get('description') or ''}")
                for item in card.get("near_miss") or []:
                    lines.append(f"  - 近邻：{item.get('fault')} — {item.get('differentiator')}")
                if card.get("missing_evidence"):
                    lines.append("  - 缺口：" + "、".join(card.get("missing_evidence") or []))
        lines.append(f"- **一句话**：{narrative.get('headline') if narrative else _default_headline(chosen_result, root)}")
        if arbitration.get("reason"):
            lines.append(f"- **仲裁依据**：{arbitration['reason']}")
    else:
        lines.append("- **根因**：未形成确定性结论")
        lines.append(f"- **判定**：{verdict_label}")
        lines.append(f"- **置信度**：{confidence}")
        if arbitration.get("reason"):
            lines.append(f"- **说明**：{arbitration['reason']}")
    others = [r for r in results if r["skill_name"] != chosen and r.get("root_cause")]
    if others:
        names = "、".join(f"{r['skill_name']}/{r['root_cause'].get('name')}" for r in others[:3])
        lines.append(f"- **其他候选**：{names}（未采信，见第七节对比）")
    lines.append("")

    # 证据链
    lines.append("## 二、证据链")
    lines.append("")
    ev = (chosen_result or {}).get("evidence") or []
    ev_map = {f"E{i}": item for i, item in enumerate(ev, 1)}
    if ev_map:
        lines.append("| 编号 | 规则 | 文件:行 | 时间 | 证据片段 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for eid, item in list(ev_map.items())[:24]:
            loc = f"{item.get('file') or '-'}:{item.get('line_no') or '-'}"
            text = (item.get("text") or "").replace("|", "\\|")
            lines.append(f"| {eid} | {item.get('rule_id')} | `{_md_code(loc)}` | {item.get('timestamp') or '-'} | {text} |")
        if len(ev_map) > 24:
            lines.append("")
            lines.append(f"> 证据较多，仅列前 24 条，其余 {len(ev_map) - 24} 条见附录原始报告。")
    else:
        lines.append("_无可用证据条目。未匹配到规则时不做根因推断。_")
    quality = (chosen_result or {}).get("evidence_quality") or arbitration.get("evidence_quality") or {}
    if quality:
        lines.append("")
        lines.append(f"**证据质量**：{quality.get('quality_score', 0)}/100，"
                     f"{quality.get('unique_clusters', 0)} 组去重证据，"
                     f"{len(quality.get('independent_files') or [])} 个独立来源文件，"
                     f"重复 {quality.get('duplicate_occurrences', 0)} 条，"
                     f"质量置信度 **{quality.get('confidence', 'low')}**。")
        reasons = quality.get("reasons") or []
        if reasons:
            lines.append("")
            lines.extend(f"- {x}" for x in reasons[:6])
    lines.append("")

    # 研判路径：由技能登记的专家推理路径 + 本次真实命中结果生成，不是自由发挥的思维链
    reasoning = (chosen_result or {}).get("diagnostic_reasoning") or {}
    if reasoning:
        lines.append("## 三、研判路径")
        lines.append("")
        lines.append("> 下面是按技能登记的诊断策略和证据顺序，对本次真实命中结果做的复核轨迹；"
                     "未命中的规则不会被补写成“已排除原因”。")
        lines.append("")
        if reasoning.get("strategy"):
            lines.append(f"- **总体策略**：{reasoning['strategy']}")
        counts = (chosen_result or {}).get("match_counts") or {}
        if not counts:
            counts = {rid: 1 for rid in ((chosen_result or {}).get("verdict", {})
                                         .get("groups", {}).get("ALL", {}).get("hit_rules") or [])}
        order = reasoning.get("decision_order") or []
        if order:
            lines.append("")
            lines.append("| 顺序 | 检查动作 | 规则 | 本次结果 |")
            lines.append("| --- | --- | --- | --- |")
            for step in order[:10]:
                ids = "、".join(step.get("rule_ids") or []) or "-"
                hits = [f"{rid}:{int(counts.get(rid, 0))}" for rid in (step.get("rule_ids") or [])]
                result = "，".join(hits) if hits else "未绑定规则"
                lines.append(f"| {step.get('order') or '-'} | {str(step.get('action') or '').replace('|', '\\|')} "
                             f"| {ids} | {result} |")
        hypotheses = reasoning.get("hypotheses") or []
        if hypotheses:
            rc_states: dict[str, Any] = {}
            for item in ((chosen_result or {}).get("verdict", {}).get("groups", {}).get("ALL", {})
                         .get("root_causes") or []):
                rc_states[str(item.get("id"))] = item.get("established")
            lines.append("")
            lines.append("| 假设 | 根因 | 判定条件 | 本次结果 |")
            lines.append("| --- | --- | --- | --- |")
            for h in hypotheses[:6]:
                rcid = str(h.get("root_cause") or "")
                if root and rcid == str(root.get("id")):
                    result = "采信"
                elif rc_states.get(rcid) is True:
                    result = "条件成立但与更高优先级冲突"
                elif rc_states.get(rcid) is False:
                    result = "条件不成立，排除"
                else:
                    result = "证据不足，未评估"
                lines.append(f"| {h.get('id') or '-'} | {h.get('name') or '-'} "
                             f"| `{h.get('condition') or '-'}` | {result} |")
        if reasoning.get("discriminators"):
            lines.append("")
            lines.append("**关键鉴别点**：")
            for d in reasoning["discriminators"][:6]:
                lines.append(f"- `{d.get('rule_id')}`：{d.get('meaning') or ''}；{d.get('use') or ''}")
        if reasoning.get("pitfalls"):
            lines.append("")
            lines.append("**易误判**：")
            lines.extend(f"- {x}" for x in reasoning["pitfalls"][:6])
        if reasoning.get("stop_conditions"):
            lines.append("")
            lines.append("**停止/升级条件**：")
            lines.extend(f"- {x}" for x in reasoning["stop_conditions"][:6])
        lines.append("")

    # 机理
    lines.append("## 四、根因机理")
    lines.append("")
    if root.get("mechanism"):
        lines.append(root["mechanism"])
    else:
        lines.append("_该根因的机理尚未登记，需人工复核，禁止据此直接下结论。_")
    if narrative and narrative.get("analysis"):
        lines.append("")
        lines.append("**综合研判**：" + narrative["analysis"])
    lines.append("")

    # 处置
    lines.append("## 五、处置步骤")
    lines.append("")
    actions = root.get("actions") or []
    if actions:
        for idx, action in enumerate(actions, 1):
            lines.append(f"{idx}. {action}")
    else:
        lines.append("_该根因未登记处置动作，请按现网流程人工制定。_")
    lines.append("")
    if root.get("prerequisites"):
        lines.append("- **前提条件**：" + "；".join(str(x) for x in root["prerequisites"]))
    if root.get("risks"):
        lines.append(f"- **风险与注意**：{root['risks']}")
    if root.get("rollback"):
        lines.append(f"- **回退方案**：{root['rollback']}")
    if root.get("escalation"):
        lines.append(f"- **升级条件**：{root['escalation']}")
    lines.append("")

    # 验证
    lines.append("## 六、恢复验证")
    lines.append("")
    verify = root.get("verify") or []
    if verify:
        for item in verify:
            if isinstance(item, dict):
                globs = "、".join(item.get("globs") or []) or "-"
                patterns = "、".join(item.get("patterns") or []) or "-"
                expect = "应消失" if item.get("expect") == "absent" else "应出现"
                hold = item.get("hold_time") or ""
                lines.append(f"- `{globs}` 中 `{patterns}` {expect}" + (f"，{hold}" if hold else ""))
            else:
                lines.append(f"- {item}")
    else:
        lines.append("_未登记自动验证项，按现网告警/性能指标人工观察。_")
    lines.append("")

    # 技能对比
    lines.append("## 七、并行技能对比")
    lines.append("")
    lines.append("| 技能 | 匹配分 | 状态 | 根因 | 置信度 | 命中规则 | 耗时 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        rc = r.get("root_cause") or {}
        lines.append(
            f"| {r['skill_name']} | {r.get('score', 0):.0f} | {r.get('status', '-')} | "
            f"{rc.get('name') or '-'} | {rc.get('confidence') or '-'} | "
            f"{len(r.get('verdict', {}).get('groups', {}).get('ALL', {}).get('hit_rules', []))} | "
            f"{(r.get('duration_ms') or 0) / 1000:.1f}s |")
    lines.append("")

    # 局限
    lines.append("## 八、局限与补充数据")
    lines.append("")
    limits: list[str] = []
    for r in results:
        for item in r.get("limitations") or []:
            limits.append(f"`{r['skill_name']}`：{item}")
    if narrative:
        for item in narrative.get("limitations") or []:
            limits.append(str(item))
    if arbitration.get("missing_info"):
        for item in arbitration["missing_info"]:
            limits.append(f"需要补充：{item}")
    if inventory.get("total_members") == 0:
        limits.append("输入包内未发现可解析成员，请确认采集包是否完整")
    lines.extend(f"- {x}" for x in limits) if limits else lines.append("_无。_")
    lines.append("")

    # 审计
    lines.append("## 九、执行审计")
    lines.append("")
    lines.append(f"- 输入：{len(inventory.get('files') or [])} 个文件，"
                 f"{human_size(inventory.get('total_input_bytes') or 0)}，"
                 f"解压后 {inventory.get('total_members') or 0} 个成员")
    lines.append(f"- 技能并行：{len(results)} 个，墙钟 {stats.get('duration_sec', 0):.1f}s")
    lines.append(f"- 模型调用：{stats.get('llm_calls', 0)} 次，"
                 f"token {stats.get('total_tokens', 0)}")
    if stats.get("models"):
        lines.append(f"- 参与模型：{'、'.join(stats['models'])}")
    lines.append("")

    lines.append("## 附录 A AI 诊断计划")
    lines.append("")
    if plan and plan.get("steps"):
        lines.append(f"- **计划来源**：{plan.get('source') or '-'} ｜ "
                     f"**摘要**：{plan.get('summary') or '-'}")
        hypotheses = plan.get("hypothesis") or []
        if hypotheses:
            lines.append("- **竞争假设**：" + "；".join(str(x) for x in hypotheses[:3]))
        lines.append("")
        lines.append("| 波次 | 目标 | 技能 | 门槛 | 执行状态 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for step in plan.get("steps") or []:
            lines.append(f"| {step.get('id')} {step.get('title')} | {step.get('objective') or '-'} | "
                         f"{'、'.join(step.get('skills') or [])} | "
                         f"{step.get('gate_label') or step.get('gate')} | {step.get('status') or 'planned'} |")
        replan = plan.get("replan") if isinstance(plan.get("replan"), dict) else None
        if replan:
            lines.append("")
            lines.append(f"- **补规划**：{replan.get('status')} ｜ {replan.get('reason') or '-'}")
        refs = plan.get("experience_refs") or []
        if refs:
            lines.append("- **历史经验参考**：" + "；".join(
                f"{r.get('task_id') or '-'}/{r.get('root_cause') or '-'}" for r in refs[:3]))
    else:
        lines.append("_本次未生成可执行 AI 计划，使用确定性回退路径。_")
    lines.append("")

    lines.append("## 附录 B 子技能原始报告")
    lines.append("")
    for r in results:
        lines.append(f"- `{r['skill_name']}`：状态 {r.get('status')}，"
                     f"报告 {r.get('reports', {}).get('md') or '未生成'}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_本报告由 BBU 故障诊断平台依据子 Skill 规则检索与证据链生成；"
                 "未验证规则、未覆盖机型/版本、未解析二进制项均在“局限”中明示。"
                 "涉及现网操作前请按变更流程复核。_")
    return "\n".join(lines)


def _default_headline(result: dict[str, Any], root: dict[str, Any]) -> str:
    hits = result.get("verdict", {}).get("groups", {}).get("ALL", {}).get("hit_rules") or []
    count = len(result.get("evidence") or [])
    return f"依据 {len(hits)} 条规则命中、{count} 条可定位证据，判定为{root.get('name')}。"


def _md_code(text: str) -> str:
    return text.replace("`", "'")


def validate_citations(text: str, max_id: int) -> list[str]:
    ids = [int(x) for x in CITE_RE.findall(text or "")]
    bad = sorted({i for i in ids if i < 1 or i > max_id})
    return [f"[E{i}] 无对应证据" for i in bad]


def render_html(markdown_text: str, title: str, brand: str = "BBU 故障诊断平台") -> str:
    body = markdown_to_html(markdown_text)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 32px 20px 64px; background: #f4f5f7; color: #17202a;
  font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }}
.report {{ max-width: 960px; margin: 0 auto; background: #fff; border: 1px solid #dde3ea;
  border-radius: 10px; padding: 40px 48px; box-shadow: 0 10px 30px rgba(15,23,42,.06); }}
.report-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px;
  border-bottom: 2px solid #0f766e; padding-bottom: 14px; margin-bottom: 24px; }}
.report-head .brand {{ font-weight: 700; color: #0f766e; letter-spacing: .04em; font-size: 13px; }}
h1 {{ font-size: 26px; margin: 0 0 8px; letter-spacing: .01em; }}
h2 {{ font-size: 18px; margin: 28px 0 10px; padding-left: 10px; border-left: 4px solid #0f766e; }}
h3 {{ font-size: 16px; margin: 18px 0 8px; }}
p {{ margin: 8px 0; }}
blockquote {{ margin: 10px 0; padding: 10px 14px; background: #f6f8fa; border-left: 3px solid #94a3b8; color: #475569; font-size: 13px; }}
table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
th, td {{ border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ background: #f1f5f9; font-weight: 600; }}
code {{ background: #f1f5f9; padding: 1px 5px; border-radius: 4px; font-family: "SFMono-Regular", Consolas, monospace; font-size: 12.5px; }}
pre {{ background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 8px; overflow: auto; }}
pre code {{ background: transparent; color: inherit; }}
ul, ol {{ margin: 8px 0; padding-left: 22px; }}
li {{ margin: 4px 0; }}
hr {{ border: 0; border-top: 1px solid #e2e8f0; margin: 28px 0; }}
.cite {{ display:inline-block; background:#ecfdf5; color:#0f766e; border:1px solid #a7f3d0;
  border-radius: 4px; padding: 0 4px; font-size: 11px; font-weight: 600; }}
.strong {{ font-weight: 700; }}
@media print {{ body {{ background:#fff; padding:0; }} .report {{ border:0; box-shadow:none; padding:0; }} }}
</style>
</head>
<body>
<div class="report">
<div class="report-head"><span class="brand">{html.escape(brand)}</span><span>{html.escape(title)}</span></div>
{body}
</div>
</body>
</html>"""


def markdown_to_html(md: str) -> str:
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    in_code = False
    code_lines: list[str] = []
    list_mode: str | None = None
    table_lines: list[str] = []

    def close_list() -> None:
        nonlocal list_mode
        if list_mode:
            out.append(f"</{list_mode}>")
            list_mode = None

    def flush_table() -> None:
        nonlocal table_lines
        if not table_lines:
            return
        rows = [[c.strip() for c in row.strip().strip("|").split("|")] for row in table_lines]
        if len(rows) >= 2 and re.match(r"^:?-{2,}:?$", rows[1][0].replace(" ", "")):
            head, body = rows[0], rows[2:]
        else:
            head, body = rows[0], rows[1:]
        out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
        for row in body:
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
        out.append("</tbody></table>")
        table_lines = []

    while i < len(lines):
        line = lines[i]
        if in_code:
            if line.strip().startswith("```"):
                out.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                in_code = False
                code_lines = []
            else:
                code_lines.append(line)
            i += 1
            continue
        if line.strip().startswith("```"):
            close_list()
            flush_table()
            in_code = True
            i += 1
            continue
        if line.strip().startswith("|"):
            close_list()
            table_lines.append(line)
            i += 1
            continue
        flush_table()
        stripped = line.strip()
        if not stripped:
            close_list()
            out.append("")
        elif stripped.startswith("#"):
            close_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            content = stripped[level:].strip()
            level = min(6, max(1, level))
            out.append(f"<h{level}>{inline(content)}</h{level}>")
        elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            close_list()
            out.append("<hr>")
        elif stripped.startswith(">"):
            close_list()
            out.append(f"<blockquote>{inline(stripped.lstrip('>').strip())}</blockquote>")
        elif re.match(r"^[-*+] ", stripped):
            if list_mode != "ul":
                close_list()
                out.append("<ul>")
                list_mode = "ul"
            out.append(f"<li>{inline(stripped[2:].strip())}</li>")
        elif re.match(r"^\d+\. ", stripped):
            if list_mode != "ol":
                close_list()
                out.append("<ol>")
                list_mode = "ol"
            out.append(f"<li>{inline(re.sub(r'^\\d+\\. ', '', stripped).strip())}</li>")
        else:
            close_list()
            out.append(f"<p>{inline(stripped)}</p>")
        i += 1
    flush_table()
    close_list()
    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    return "\n".join(x for x in out if x != "")


def inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\[E(\d+)\]", r'<span class="cite">E\1</span>', escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r'<span class="strong">\1</span>', escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)

    def link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if not re.match(r"^(https?://|#|\\./)", url):
            return label
        return f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{label}</a>'

    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|#[^)\s]+|\\./[^)\s]+)\)", link, escaped)
    return escaped


def build_narrative_facts(task: dict[str, Any], result: dict[str, Any],
                          arbitration: dict[str, Any]) -> dict[str, Any]:
    root = result.get("root_cause") or {}
    return {
        "故障描述": task.get("description"),
        "采信技能": result.get("skill_name"),
        "根因": root.get("name"),
        "根因ID": root.get("id"),
        "判定状态": arbitration.get("status"),
        "置信度": arbitration.get("confidence"),
        "命中规则": result.get("verdict", {}).get("groups", {}).get("ALL", {}).get("hit_rules") or [],
        "匹配次数": result.get("match_counts") or {},
        "机理": truncate(root.get("mechanism") or "", 800),
        "处置动作": root.get("actions") or [],
        "风险": root.get("risks") or "",
        "回退": root.get("rollback") or "",
        "局限": result.get("limitations") or [],
        "并行技能": [],
    }


def evidence_for_prompt(result: dict[str, Any], limit: int = 16) -> list[dict[str, Any]]:
    ev = (result.get("evidence") or [])[:limit]
    return [{"id": f"E{i}", "rule": e.get("rule_id"), "file": e.get("file"),
             "line": e.get("line_no"), "time": e.get("timestamp"),
             "text": truncate(e.get("text") or "", 240)} for i, e in enumerate(ev, 1)]
