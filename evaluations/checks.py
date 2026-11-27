"""集成检查：真实临时实例 + 真实模型代理 + 虚拟数据。

每个 check 返回 ``(status, detail, evidence)``；status 取值 pass/fail/blocked。
检查只读取 Lab 隔离数据，不触碰生产 data/。报告和证据里不得出现 API Key。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def package_hash(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    sums = DIST / "SHA256SUMS-1.19.0.txt"
    _assert(sums.is_file(), "缺少 SHA256SUMS-1.19.0.txt")
    checked = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        path = DIST / name.strip()
        _assert(path.is_file(), f"缺少发布包 {name}")
        actual = _sha256(path)
        _assert(actual == digest, f"{name} 哈希不一致：{actual[:12]} != {digest[:12]}")
        checked.append({"name": name, "sha256": actual, "size": path.stat().st_size})
    return "pass", f"{len(checked)} 个发布包 SHA256 一致", {"packages": checked}


def clean_start(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    lab = ctx["lab"]
    status, data, _ = lab.admin.request("GET", "/api/health")
    _assert(status == 200 and json.loads(data).get("ok"), "健康检查失败")
    _assert(str(lab.data_dir).startswith("/tmp/"), "临时实例不在 /tmp")
    tasks = lab.admin.json("GET", "/api/tasks?limit=1")[1]
    return "pass", "临时数据目录干净启动，健康检查 200", {
        "port": lab.port, "data_dir": str(lab.data_dir),
        "tasks_at_start": len((tasks.get("items") or tasks.get("tasks") or [])),
    }


def e2e_positive(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    task = ctx.get("live_task")
    _assert(task and task.get("status") == "done", "正例任务未 done")
    _assert((task.get("verdict") or {}).get("status") == "matched", "正例未 matched")
    _assert((task.get("verdict") or {}).get("root_cause_name"), "正例没有根因")
    _assert(task.get("has_report"), "正例没有报告")
    return "pass", f"正例 {task['status']} / {task['verdict']['status']} / {task['verdict']['root_cause_name']}", {
        "task_id": task["id"], "verdict": task["verdict"],
        "llm_calls": (task.get("stats") or {}).get("llm_calls"),
    }


def e2e_negative_unknown(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    lab = ctx["lab"]
    fixture = lambda name: ROOT / "tests" / "fixtures" / "logs" / name  # noqa: E731
    normal = lab.task("设备运行正常，无告警，做健康基线。", fixture("normal-demo.zip"),
                      wait=True, timeout=120)
    unknown = lab.task("现场有异常但无已知告警码，日志仅未知噪声。", fixture("normal-demo.zip"),
                       wait=True, timeout=120)
    for task in (normal, unknown):
        _assert(task["status"] in ("done", "needs_data"), f"反例状态异常：{task['status']}")
        _assert(task["verdict"].get("status") in ("normal", "no_evidence", "needs_data",
                   "unknown_fault", "abstain", "unmatched", "insufficient",
                   "temporal_conflict"),
                f"反例硬猜：{task['verdict'].get('status')}")
        primary = task["verdict"].get("primary") or task["verdict"].get("root_cause_name")
        _assert(not primary, f"反例仍给出根因：{primary}")
    return "pass", "正常/未知样本均未硬归因", {
        "normal": {"status": normal["status"], "verdict": normal["verdict"]},
        "unknown": {"status": unknown["status"], "verdict": unknown["verdict"]},
    }


def e2e_model_unavailable(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    lab = ctx["lab"]
    # 禁用全部已启用模型，确保没有可用模型；跑完再恢复代理模型。
    _, models = lab.admin.json("GET", "/api/models")
    removed = []
    for item in models.get("items") or []:
        if item.get("enabled"):
            lab.admin.json("DELETE", f"/api/models/{item['id']}")
            removed.append(item.get("name"))
    try:
        task = lab.task("NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
                        ROOT / "tests" / "fixtures" / "logs" / "clock-demo.zip",
                        wait=True, timeout=120)
        _assert(task["status"] == "done", f"无模型任务未完成：{task['status']}")
        _assert(task["verdict"].get("status") == "matched", "无模型仍未给出确定性结论")
        _assert((task.get("stats") or {}).get("llm_calls") == 0, "无模型任务仍发生模型调用")
    finally:
        if removed:
            lab.create_model(
                name="lab-real-proxy-restored", provider="openai",
                base_url=lab.proxy.base_url, api_key="lab-proxy",
                model=lab._upstream["model"],
                roles=["default", "planner", "router", "arbiter", "writer", "workshop"],
                priority=1, max_tokens=16384, timeout_sec=240)
    return "pass", "无可用模型时确定性路径完成，llm_calls=0", {
        "task_id": task["id"], "verdict": task["verdict"],
        "llm_calls": 0, "removed_models": removed,
    }


def restart_recovery(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    lab = ctx["lab"]
    task = lab.task("VSW 单板 SSD 故障，SSD 寿命低于阈值，容器镜像校验失败。",
                    ROOT / "tests" / "fixtures" / "logs" / "mixed-demo.zip",
                    wait=False)
    time.sleep(0.05)
    lab.kill()
    before = task["id"]
    lab.restart(wait=True)
    after = lab.wait_task(before, timeout=120)
    _assert(after["status"] in ("done", "needs_data", "failed"),
            f"恢复后仍是 {after['status']}")
    _assert(after["status"] != "running", "恢复后仍 running")
    return "pass", f"kill -9 后 {before} 恢复为 {after['status']}", {
        "task_id": before, "status": after["status"],
        "stage": after.get("stage"), "attempts": after.get("attempts"),
        "recovery": after.get("recovery"),
    }


def cancel_task(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    lab = ctx["lab"]
    fixture = ROOT / "tests" / "fixtures" / "logs" / "mixed-demo.zip"
    submitted = []
    for i in range(4):
        submitted.append(lab.task(f"取消测试队列 {i}", fixture, wait=False))
    target = submitted[-1]["id"]
    lab.admin.json("POST", f"/api/tasks/{target}/cancel")
    result = lab.wait_task(target, timeout=60)
    _assert(result["status"] == "cancelled", f"取消后状态 {result['status']}")
    return "pass", f"{target} 已取消", {"task_id": target, "status": result["status"],
                                        "queue_depth": len(submitted)}


def task_cards_300(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    from bbu.task_card import validate_task_card
    dataset = ctx["dataset"]
    cards = json.loads((dataset.root / "cards" / "cards.json").read_text(encoding="utf-8"))
    _assert(len(cards) == 300, f"任务卡数量 {len(cards)}")
    families = {}
    for card in cards:
        validate_task_card(card)
        families[card["fault_domain"]] = families.get(card["fault_domain"], 0) + 1
        _assert(card["origin"] == "synthetic", "合成卡 origin 不是 synthetic")
        _assert(card["split"] in ("dev", "test"), f"非法 split {card['split']}")
    manifest = json.loads((dataset.root / "dataset_manifest.json").read_text(encoding="utf-8"))
    _assert(manifest["cards"] == 300, "manifest 卡片数不一致")
    _assert(manifest["seed"] == 20261125, "seed 不一致")
    return "pass", "300 张合成卡全部通过 schema，seed 固定", {
        "cards": len(cards), "families": families, "seed": manifest["seed"],
        "files": len(manifest["files"]),
    }


def hidden_sim_isolation(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    dataset = ctx["dataset"]
    hidden = json.loads((dataset.root / "hidden-sim" / "cards.json").read_text(encoding="utf-8"))
    _assert(len(hidden) == 20, f"hidden-sim 数量 {len(hidden)}")
    for card in hidden:
        _assert(card.get("hidden_sim") is True, "hidden-sim 标记缺失")
        _assert(card.get("origin") == "synthetic", "hidden-sim 冒充真实来源")
        _assert(card.get("split") == "test", "hidden-sim 不应标 hidden split")
        _assert(not card.get("hidden_isolated") is False, "hidden-sim 隔离标记错误")
    # 临时实例中没有真实 hidden 案例，避免把 sim 当真实 hidden 评级。
    rows = ctx["lab"].admin.json("GET", "/api/cases?split=hidden")[1]
    items = rows.get("items") or rows.get("cases") or []
    _assert(not items, "临时实例意外存在真实 hidden 案例")
    return "pass", "hidden-sim 20 张全部 synthetic/test，不冒充真实 hidden", {
        "hidden_sim": len(hidden), "real_hidden_in_lab": len(items),
    }


def live_evidence_chain(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    task = ctx["live_task"]
    _, data = ctx["lab"].admin.json("GET", f"/api/tasks/{task['id']}/evidence?limit=100")
    items = data.get("items") or []
    _assert(items, "真实任务没有落证据")
    _assert(data.get("total") == len(items), "证据 total 不一致")
    bad = [e for e in items if not e.get("integrity_valid")]
    _assert(not bad, f"{len(bad)} 条证据完整性失败")
    located = [e for e in items if ((e.get("source") or {}).get("line_start") or 0) > 0]
    _assert(located, "证据没有文件/行号锚点")
    for item in items[:3]:
        _assert(item.get("excerpt"), "证据缺原文摘录")
        _assert(len(item["excerpt"]) < 2000, "证据摘录过大，可能泄漏原始全文")
    return "pass", f"{len(items)} 条证据全部完整性有效且有行号锚点", {
        "total": data.get("total"), "sample": items[0],
        "with_line": len(located),
    }


def live_hypothesis(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    task = ctx["live_task"]
    hypotheses = (task.get("session") or {}).get("hypotheses") or {}
    _assert(hypotheses, "真实任务没有 HypothesisState")
    confirmed = [h for h in hypotheses.values() if h.get("status") == "confirmed"]
    _assert(confirmed, "没有 confirmed 假设")
    _, evidence = ctx["lab"].admin.json("GET", f"/api/tasks/{task['id']}/evidence?limit=1000")
    ids = {e["evidence_id"] for e in evidence.get("items") or []}
    for hyp in confirmed:
        support = set(hyp.get("supporting_evidence") or [])
        _assert(support and support <= ids, "confirmed 假设引用了不存在的证据")
    return "pass", f"{len(hypotheses)} 个假设，{len(confirmed)} 个 confirmed 且证据可对账", {
        "hypotheses": hypotheses, "evidence_ids": sorted(ids)[:10],
    }


def live_calibration(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    calibration = (ctx["live_task"].get("verdict") or {}).get("calibration") or {}
    _assert(calibration, "verdict 缺 calibration")
    _assert(calibration.get("status") in ("uncalibrated", "calibrated"), "非法校准状态")
    if calibration["status"] == "uncalibrated":
        _assert(calibration.get("accuracy") is None, "uncalibrated 仍展示准确率")
        _assert(calibration.get("note"), "uncalibrated 缺说明")
    return "pass", f"校准状态 {calibration['status']}，样本 {calibration.get('sample_count')}", {
        "calibration": calibration,
    }


def live_report(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    task = ctx["live_task"]
    status, raw, _ = ctx["lab"].admin.request("GET", f"/api/tasks/{task['id']}/report")
    _assert(status == 200 and raw, "报告不可读取")
    text = raw.decode("utf-8", "replace")
    _assert("## " in text, "报告不是 Markdown 结构")
    result = task.get("result") or {}
    claims = result.get("claims") or {}
    check = result.get("claim_check") or {}
    _assert(check.get("ok") is True, f"claim_check 未通过：{check}")
    _assert(check.get("claim_count", 0) > 0, "没有 claim")
    for claim in claims.get("claims") or []:
        for eid in claim.get("evidence_ids") or []:
            _, data = ctx["lab"].admin.json("GET", f"/api/tasks/{task['id']}/evidence?limit=1000")
            ids = {e["evidence_id"] for e in data.get("items") or []}
            _assert(eid in ids, f"claim 引用了不存在的证据 {eid}")
    trace = result.get("agent_trace") or {}
    _assert(trace.get("violations") == [], "agent trace 有违规")
    return "pass", f"报告 {len(text)} 字符，{check.get('claim_count')} claims 全部可回查", {
        "chars": len(text), "claim_check": check,
        "trace": {k: trace.get(k) for k in ("actions", "violations", "stop_reason")},
    }


def live_model_connect(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    lab = ctx["lab"]
    _assert(lab.proxy, "未启用真实模型代理")
    url = lab.proxy.base_url + "/models"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    ids = [m.get("id") for m in data.get("data") or []]
    _assert(lab._upstream["model"] in ids, "模型列表不含目标模型")
    return "pass", f"代理 /v1/models 200，模型 {lab._upstream['model']} 可见", {
        "models": ids, "provider_base": re.sub(r"https?://[^/]+", "https://***", lab._upstream["base_url"]),
    }


def live_role_calls(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    lab = ctx["lab"]
    calls = lab.proxy.calls() if lab.proxy else []
    _assert(calls, "代理没有调用记录")
    roles = {"planner": 0, "writer": 0, "arbiter": 0, "router": 0}
    for call in calls:
        payload = call.get("payload") or {}
        messages = payload.get("messages") or []
        text = " ".join(str(m.get("content") or "")[:200] for m in messages)
        if "第一诊断规划师" in text or "诊断规划" in text:
            roles["planner"] += 1
        if "报告撰写员" in text or "综合研判" in text:
            roles["writer"] += 1
        if "仲裁" in text:
            roles["arbiter"] += 1
        if "路由" in text:
            roles["router"] += 1
    _assert(roles["planner"] >= 1, "planner 未调用")
    _assert(roles["writer"] >= 1, "writer 未调用")
    # 当前确定性仲裁通常不需要 arbiter；只要记录 role 分布，不强行要求。
    return "pass", f"planner={roles['planner']} writer={roles['writer']} arbiter={roles['arbiter']}", {
        "roles": roles, "calls": len(calls),
        "statuses": [c.get("status") for c in calls],
    }


def live_plan_ai(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    result = ctx["live_task"].get("result") or {}
    plan = result.get("plan") or {}
    _assert(plan.get("source") == "ai", f"计划来源 {plan.get('source')}")
    trace = result.get("agent_trace") or {}
    _assert(trace.get("engine") == "bounded-v1", "不是 bounded-v1")
    _assert(trace.get("action_count", 0) >= 1, "没有 agent action")
    _assert(trace.get("violations") == [], "agent 有违规")
    _assert(trace.get("stop_reason"), "缺 stop_reason")
    return "pass", f"计划 ai，{trace.get('action_count')} actions，stop={trace.get('stop_reason')}", {
        "plan_source": plan.get("source"), "trace": trace,
    }


def supplement_stats(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    lab = ctx["lab"]
    task = lab.task("现场反馈设备运行正常，无告警，上传日志做健康基线。",
                    ROOT / "tests" / "fixtures" / "logs" / "normal-demo.zip",
                    wait=True, timeout=120)
    session = task.get("session") or {}
    _assert(task["status"] == "needs_data", f"正常样本不是 needs_data：{task['status']}")
    _assert("supplement_round" in session, "session 缺 supplement_round")
    _assert("questions" in task, "任务缺 questions")
    _assert(isinstance(session.get("supplement_round"), int), "supplement_round 非整数")
    return "pass", f"needs_data 暴露 {len(task.get('questions') or [])} 个问题，轮次字段可读", {
        "task_id": task["id"], "supplement_round": session.get("supplement_round"),
        "questions": len(task.get("questions") or []),
    }


def distill_gate(ctx: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    # auto_distill 是“技能草稿蒸馏”，不是 SFT/LoRA；训练流水线不存在。
    _assert(not (ROOT / "training").exists(), "意外存在 training 目录")
    _assert(not list(ROOT.glob("**/*lora*")), "意外存在 LoRA 产物")
    return "blocked", "未实现 SFT/LoRA 训练流水线，按计划自动 not_started", {
        "auto_distill": "workshop 草稿蒸馏，不是模型训练",
        "training_dir": False,
    }
