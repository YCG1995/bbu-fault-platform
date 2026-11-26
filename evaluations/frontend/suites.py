"""FE0-FE7 前端套件条款。"""
from __future__ import annotations

from typing import Any

FE_SUITES: dict[int, dict[str, Any]] = {
    0: {"title": "静态资源与路由可达", "clauses": [
        {"id": "FE0-A1", "title": "HTML/JS/CSS/图标资源全部 200", "severity": "P0", "check": "assets_200"},
        {"id": "FE0-A2", "title": "11 路由加载无 console error / pageerror", "severity": "P0", "check": "routes_no_console"},
        {"id": "FE0-A3", "title": "11 路由无 404 网络响应", "severity": "P1", "check": "routes_no_404"},
    ]},
    1: {"title": "页面契约与角色矩阵", "clauses": [
        {"id": "FE1-A1", "title": "11 路由 × 4 角色 44 格页面标题/权限正确", "severity": "P0", "check": "role_matrix"},
        {"id": "FE1-A2", "title": "无权访问不渲染管理按钮/数据", "severity": "P0", "check": "denied_isolation"},
        {"id": "FE1-A3", "title": "空态/加载/错误态不空白", "severity": "P1", "check": "empty_loading_error"},
    ]},
    2: {"title": "交互与表单", "clauses": [
        {"id": "FE2-A1", "title": "新建任务：描述草稿、上传、模式选择可交互", "severity": "P0", "check": "new_task_form"},
        {"id": "FE2-A2", "title": "键盘：全局搜索聚焦、Esc 关闭、焦点回收", "severity": "P1", "check": "keyboard_search"},
        {"id": "FE2-A3", "title": "重复提交/幂等：只产生一个任务", "severity": "P0", "check": "duplicate_submit"},
        {"id": "FE2-A4", "title": "离开新建页再返回，草稿恢复", "severity": "P1", "check": "draft_restore"},
    ]},
    3: {"title": "核心工作流 E2E", "clauses": [
        {"id": "FE3-A1", "title": "登录→dashboard→退出→重新登录", "severity": "P0", "check": "login_logout"},
        {"id": "FE3-A2", "title": "激活码开户；自助注册开关开/关", "severity": "P0", "check": "activation_register"},
        {"id": "FE3-A3", "title": "新建诊断→进度→任务详情→报告", "severity": "P0", "check": "new_diagnosis_report"},
        {"id": "FE3-A4", "title": "needs_data 补证→同 task 续跑", "severity": "P0", "check": "supplement_same_task"},
        {"id": "FE3-A5", "title": "任务详情四 tab / claim / iframe sandbox", "severity": "P0", "check": "detail_tabs"},
        {"id": "FE3-A6", "title": "恢复验证登记→回传→对照状态", "severity": "P1", "check": "recovery_ui"},
        {"id": "FE3-A7", "title": "工坊五步步骤条与资料输入", "severity": "P1", "check": "workshop_steps"},
        {"id": "FE3-A8", "title": "工坊字段来源/锁/局部 diff/发布", "severity": "P0", "method": "gap",
         "gap": "前端只展示 provenance 覆盖率与锁定按钮，没有字段级局部 diff/并发 patch/无来源阻断发布的完整 UI 流。"},
        {"id": "FE3-A9", "title": "技能库列表/manifest 状态/启停", "severity": "P1", "check": "skills_page"},
        {"id": "FE3-A10", "title": "案例列表 + 复核回流真实案例", "severity": "P1", "check": "cases_review"},
        {"id": "FE3-A11", "title": "模型接入角色路由与 Key 掩码", "severity": "P1", "check": "models_page"},
        {"id": "FE3-A12", "title": "系统六 tab + 离线升级入口", "severity": "P0", "check": "system_tabs"},
        {"id": "FE3-A13", "title": "全局搜索/通知/个人中心/改密入口", "severity": "P1", "check": "shell_tools"},
        {"id": "FE3-A14", "title": "375px 11 路由无整页横向滚动", "severity": "P0", "check": "mobile_no_overflow"},
    ]},
    4: {"title": "视觉与响应式", "clauses": [
        {"id": "FE4-A1", "title": "1440/1024/768/375 四视口 11 路由截图齐全", "severity": "P1", "check": "visual_screenshots"},
        {"id": "FE4-A2", "title": "375px 无横向溢出、卡片化可读", "severity": "P0", "check": "visual_mobile"},
        {"id": "FE4-A3", "title": "关键页面无空白/半截表格/遮挡", "severity": "P1", "check": "visual_sanity"},
    ]},
    5: {"title": "可访问性", "clauses": [
        {"id": "FE5-A1", "title": "axe WCAG 2.1 AA 11 路由 0 blocker", "severity": "P0", "check": "axe_wcag"},
        {"id": "FE5-A2", "title": "键盘路径：Tab 可达、Enter 打开、Esc 关闭", "severity": "P1", "check": "keyboard_path"},
        {"id": "FE5-A3", "title": "标签/错误关联与焦点陷阱", "severity": "P1", "check": "focus_trap"},
    ]},
    6: {"title": "容错、安全与性能", "clauses": [
        {"id": "FE6-A1", "title": "401 清会话回登录，不丢草稿", "severity": "P0", "check": "unauthorized"},
        {"id": "FE6-A2", "title": "403 显示无权访问，不泄露管理数据", "severity": "P0", "check": "forbidden"},
        {"id": "FE6-A3", "title": "409 revision 冲突提示且保留输入", "severity": "P0", "check": "conflict_409"},
        {"id": "FE6-A4", "title": "500/断网错误态可恢复", "severity": "P0", "check": "server_error"},
        {"id": "FE6-A5", "title": "SSE 断开自动重连/轮询，状态不倒退", "severity": "P1", "method": "gap",
         "gap": "Page 有 EventSource onerror 重连逻辑，但缺可注入的 SSE 断流探针和状态机断言。"},
        {"id": "FE6-A6", "title": "XSS 转义 + 报告 iframe sandbox", "severity": "P0", "check": "xss_iframe"},
        {"id": "FE6-A7", "title": "长列表 60 条分页不崩、不白屏", "severity": "P1", "check": "long_list"},
    ]},
    7: {"title": "真实模型页面表达", "clauses": [
        {"id": "FE7-A1", "title": "真实模型任务完成，报告 iframe 可打开", "severity": "P0", "check": "real_model_report"},
        {"id": "FE7-A2", "title": "模型不可用显示降级/配置提示，不伪造 AI 结论", "severity": "P0", "check": "model_failure_notice"},
        {"id": "FE7-A3", "title": "工坊真实模型五步全链路", "severity": "P1", "method": "gap",
         "gap": "工坊五步的 AI 分析/规则审查/案例回归全链路过长且依赖真实模型队列，当前只验证步骤条与资料入口。"},
    ]},
}
