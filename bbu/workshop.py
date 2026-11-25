"""平台内技能工坊：AI 依据元 Skill 规范起草 → 校验 → 案例回归 → 构建 → 发布 → 版本回退。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .ai import AIGateway, AIError, Budget, NoModelError
from .db import Database
from .prompts import (WORKSHOP_EXTRACT_SYSTEM, WORKSHOP_REVIEW_SCHEMA,
                      WORKSHOP_REVIEW_SYSTEM, WORKSHOP_SPEC_SCHEMA,
                      workshop_extract_user, workshop_repair_user,
                      workshop_review_user)
from .skills import SkillRegistry
from .workshop_spec import (audit_spec, compile_profile, derive_skill_name,
                            merge_specs, parse_source, public_spec)
from .storage import extract_inputs, sample_text_files
from .util import (env_with_vendor, iso_now, new_id, read_json, safe_filename,
                   unique_path, write_json, write_text)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

SCHEMA_HINT = """\
skill: {name, title, version, execution_mode: script}
category: 故障大类
fault:
  name, aliases[], alarm_codes[{code,name}], domain{device_types[],modes,boards,version_scope},
  collection_sources[device-tar|sdr-zip|dcm-zip|cloudmat-text], triggers[], exclusions[]
symptoms: {observable[], description_keywords[], lookalikes[{fault,differentiator}], impact, time_pattern}
analysis:
  steps:
    - id: S1
      title, objective
      targets[{globs[], format: text|csv|xlsx|json|xml|gz-text|binary, encoding}]
      rules:
        - id: E1
          step: S1
          description
          type: text | numeric_trend
          globs[]                    # ** 跨目录；给多个可能路径
          encoding: auto
          format: text | csv | xlsx
          all_of[] any_of[] none_of[]
          min_matches: 1
          context_lines: 2
          time_extract{pattern, group, formats[]}   # 可选
          associate_by{pattern, group} 或 {site: true}  # 可选
          verified: true|false       # false 不得被 root_causes.condition 引用
      decision: {on_hit{conclusion,goto}, on_miss{conclusion,goto}}
root_causes:
  - id: R1
    name
    condition: "E1 and E2"          # 只允许规则ID/and/or/not/括号
    mechanism: 已确认的故障机理（未确认写“待补充”）
    confidence: high|medium|low
    temporal: [T1]                  # 可选
    actions[] prerequisites[] risks rollback
    verify[{globs[],patterns[],expect: absent|present,hold_time}]
    escalation
temporal: [{id: T1, first: E1, second: E2, min_gap_minutes, max_gap_minutes}]
components: {unpack: true, search: true, report: true}
"""

EXAMPLE_PROFILE = """\
skill:
  name: bbu-fault-example
  title: 示例故障
  version: 0.1.0
  execution_mode: script
category: 示例
fault:
  name: 示例故障
  aliases: [示例]
  alarm_codes: []
  domain: {device_types: [BBU], modes: 待补充, boards: 待补充, version_scope: 待补充}
  collection_sources: [device-tar]
  triggers: []
  exclusions: []
symptoms:
  observable: []
  description_keywords: [示例]
  lookalikes: []
  impact: 待补充
  time_pattern: 待补充
analysis:
  steps:
    - id: S1
      title: 检索直接证据
      objective: 确认日志中是否存在该故障的直接证据
      targets: [{globs: ["**/*.log"], format: text, encoding: auto}]
      rules:
        - id: E1
          step: S1
          description: 命中故障关键字
          type: text
          globs: ["**/*.log", "**/*.txt"]
          any_of: ["示例关键字"]
          min_matches: 1
          verified: true
      decision:
        on_hit: {conclusion: 命中直接证据, goto: R1}
        on_miss: {conclusion: 未命中, goto: EXCLUDE}
root_causes:
  - id: R1
    name: 示例根因
    condition: E1
    mechanism: 待补充
    confidence: medium
    actions: []
    prerequisites: []
    risks: 待补充
    rollback: 待补充
    verify: []
    escalation: 待补充
components: {unpack: true, search: true, report: true}
"""


class WorkshopService:
    def __init__(self, db: Database, config: Any, registry: SkillRegistry,
                 gateway: AIGateway) -> None:
        self.db = db
        self.config = config
        self.registry = registry
        self.gateway = gateway
        self.meta = Path(config.meta_skill_dir)
        # 生成是同步执行的；服务启动时仍停留在 generating/analyzing，说明上次进程异常中断。
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        try:
            rows = self.db.all(
                "SELECT id FROM workshop_drafts WHERE status IN ('generating','analyzing')")
            for row in rows:
                self.db.execute(
                    "UPDATE workshop_drafts SET status='needs_confirmation', last_error=?, updated_at=? "
                    "WHERE id=?",
                    ("上次生成被服务中断，请点击“重新生成”或补充资料后继续", iso_now(), row["id"]))
        except Exception:  # 启动容错，不让旧库异常阻断主服务
            pass

    @staticmethod
    def _progress(callback: Any, stage: str, percent: int, message: str) -> None:
        if callable(callback):
            try:
                callback(stage, percent, message)
            except Exception:  # 进度回报不能影响生成主链
                pass

    # ---------- 草稿 ----------
    def create_draft(self, user_id: int, data: dict[str, Any], *,
                     deferred: bool = False, model_override: int | None = None,
                     progress: Any = None) -> dict[str, Any]:
        source_text = str(data.get("source_text") or "").strip()
        if source_text:
            return self._create_from_text(user_id, data, source_text, deferred=deferred,
                                          model_override=model_override, progress=progress)
        name = str(data.get("name") or "").strip()
        if not SLUG_RE.fullmatch(name) or not name.startswith("bbu-fault-"):
            raise ValueError("技能名必须形如 bbu-fault-xxx，或直接粘贴故障资料由系统生成")
        self._ensure_new_name(name)
        title = str(data.get("title") or "").strip() or name
        fault_name = str(data.get("fault_name") or title).strip()
        category = str(data.get("category") or "").strip()
        description = str(data.get("description") or "").strip()
        device_types = data.get("device_types") or ["BBU"]
        keywords = data.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [x.strip() for x in re.split(r"[,，、\s]+", keywords) if x.strip()]
        verified = bool(data.get("verified"))
        draft_id = new_id("W", with_time=True)
        profile = self._skeleton(name, title, fault_name, category, device_types,
                                 keywords, verified, data.get("root_cause_name"))
        now = iso_now()
        self.db.execute(
            "INSERT INTO workshop_drafts(id, owner_id, name, title, fault_name, category, status, "
            "profile_yaml, description, source_text, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (draft_id, user_id, name, title, fault_name, category, "editing",
             yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
             description, "", now, now))
        return self.get(draft_id)

    def _ensure_new_name(self, name: str) -> None:
        if self.db.one("SELECT name FROM skills WHERE name=?", (name,)) or self.db.one(
                "SELECT id FROM workshop_drafts WHERE name=?", (name,)):
            raise ValueError(f"技能 {name} 已存在；请改名，或走版本更新")

    def _create_from_text(self, user_id: int, data: dict[str, Any], source_text: str,
                          *, deferred: bool = False, model_override: int | None = None,
                          progress: Any = None) -> dict[str, Any]:
        if len(source_text) < 20:
            raise ValueError("请粘贴完整故障资料，至少需要故障名称、日志特征和判断思路")
        spec = parse_source(source_text)
        if data.get("fault_name"):
            spec["fault_name"] = str(data["fault_name"]).strip()
        if data.get("title"):
            spec["title"] = str(data["title"]).strip()
        if data.get("category"):
            spec["category"] = str(data["category"]).strip()
        name = derive_skill_name(spec, str(data.get("name") or ""))
        name = self._unique_draft_name(name)
        draft_id = new_id("W", with_time=True)
        now = iso_now()
        profile = compile_profile(spec, name)
        self.db.execute(
            "INSERT INTO workshop_drafts(id, owner_id, name, title, fault_name, category, status, "
            "profile_yaml, description, source_text, name_auto, name_locked, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (draft_id, user_id, name, spec["title"], spec["fault_name"], spec.get("category") or "硬件故障",
             "queued" if deferred else "generating",
             yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
             spec.get("summary") or "", source_text, 1, 0, now, now))
        if deferred:
            return self.get(draft_id)
        try:
            return self._generate(draft_id, progress=progress, model_override=model_override)
        except Exception as exc:  # 保留已写入的确定性 profile，明确标注未完成
            self.db.execute(
                "UPDATE workshop_drafts SET status='needs_confirmation', last_error=?, updated_at=? WHERE id=?",
                (f"生成中断：{exc}", iso_now(), draft_id))
            raise

    def _unique_draft_name(self, desired: str, exclude_id: str = "") -> str:
        """按最终 AI 结果生成唯一名称；仅替换自动命名且尚未锁定的草稿。"""
        desired = desired if desired.startswith("bbu-fault-") else "bbu-fault-" + desired.lstrip("-")
        taken = bool(self.db.one("SELECT name FROM skills WHERE name=?", (desired,)))
        if not taken:
            row = self.db.one(
                "SELECT id FROM workshop_drafts WHERE name=? AND id<>?", (desired, exclude_id))
            taken = bool(row)
        if not taken:
            return desired
        suffix = new_id("", with_time=False).lower().replace("_", "-")[:6]
        base = desired[:52].rstrip("-")
        candidate = f"{base}-{suffix}"
        while self.db.one("SELECT id FROM workshop_drafts WHERE name=? AND id<>?",
                          (candidate, exclude_id)):
            candidate = f"{base}-{new_id('', with_time=False).lower().replace('_', '-')[:6]}"
        return candidate

    @staticmethod
    def _clean_title(spec: dict[str, Any]) -> str:
        title = str(spec.get("title") or "").strip()
        fault_name = str(spec.get("fault_name") or "").strip()
        generic = {"", "未命名故障", "未命名故障分析", "故障分析", "诊断分析", "BBU故障", "基站故障"}
        if title in generic or len(title) < 4:
            title = f"{fault_name}现场故障诊断" if fault_name else ""
        return title[:80]


    def _skeleton(self, name: str, title: str, fault_name: str, category: str,
                  device_types: list[str], keywords: list[str], verified: bool,
                  root_cause_name: str | None) -> dict[str, Any]:
        profile: dict[str, Any] = {
            "skill": {"name": name, "title": title, "version": "0.1.0",
                      "execution_mode": "script"},
            "category": category or "待补充",
            "fault": {
                "name": fault_name,
                "aliases": [fault_name],
                "alarm_codes": [],
                "domain": {"device_types": device_types, "modes": "待补充",
                           "boards": "待补充", "version_scope": "待补充",
                           "collection_sources": ["device-tar"]},
                "triggers": [],
                "exclusions": [],
            },
            "symptoms": {
                "observable": [],
                "description_keywords": keywords or [fault_name],
                "lookalikes": [],
                "impact": "待补充",
                "time_pattern": "待补充",
            },
            "components": {"unpack": True, "search": True, "report": True},
        }
        if not verified:
            profile["mode"] = "triage"
            profile["analysis"] = {"steps": [{
                "id": "S1", "title": "检索故障直接证据",
                "objective": "确认日志中是否存在该故障的可定位证据",
                "targets": [{"globs": ["**/*.log", "**/*.txt"], "format": "text", "encoding": "auto"}],
                "rules": [{
                    "id": "E1", "step": "S1", "description": "待补充：命中本故障的关键字",
                    "type": "text", "globs": ["**/*.log", "**/*.txt"],
                    "any_of": keywords or [fault_name], "min_matches": 1, "verified": False,
                }],
                "decision": {"on_hit": {"conclusion": "发现疑似证据，需人工确认"},
                             "on_miss": {"conclusion": "未命中本故障", "goto": "EXCLUDE"}},
            }]}
            return profile
        profile["analysis"] = {"steps": [{
            "id": "S1", "title": "检索故障直接证据",
            "objective": "确认日志中是否存在该故障的直接证据",
            "targets": [{"globs": ["**/*.log", "**/*.txt"], "format": "text", "encoding": "auto"}],
            "rules": [{
                "id": "E1", "step": "S1", "description": "命中故障关键字",
                "type": "text", "globs": ["**/*.log", "**/*.txt"],
                "any_of": keywords or [fault_name], "min_matches": 1, "verified": True,
            }],
            "decision": {"on_hit": {"conclusion": "命中直接证据", "goto": "R1"},
                         "on_miss": {"conclusion": "未命中", "goto": "EXCLUDE"}},
        }]}
        profile["root_causes"] = [{
            "id": "R1", "name": root_cause_name or fault_name,
            "condition": "E1", "mechanism": "待补充", "confidence": "medium",
            "actions": [], "prerequisites": [], "risks": "待补充",
            "rollback": "待补充", "verify": [], "escalation": "待补充",
        }]
        return profile

    def get(self, draft_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM workshop_drafts WHERE id=?", (draft_id,))
        if not row:
            raise ValueError("草稿不存在")
        for key in ("analysis_json", "check_json", "build_json"):
            try:
                row[key.replace("_json", "")] = json.loads(row.get(key) or "{}")
            except json.JSONDecodeError:
                row[key.replace("_json", "")] = {}
        return row

    def list_drafts(self, user_id: int | None = None, is_admin: bool = False) -> list[dict[str, Any]]:
        if is_admin or user_id is None:
            rows = self.db.all("SELECT * FROM workshop_drafts ORDER BY updated_at DESC")
        else:
            rows = self.db.all(
                "SELECT * FROM workshop_drafts WHERE owner_id=? ORDER BY updated_at DESC",
                (user_id,))
        return rows

    def update_draft(self, draft_id: str, data: dict[str, Any]) -> dict[str, Any]:
        row = self.get(draft_id)
        if row["status"] == "published":
            raise ValueError("已发布的草稿不可直接编辑，请基于新版本重新构建")
        profile_yaml = data.get("profile_yaml", row["profile_yaml"])
        try:
            parsed = yaml.safe_load(profile_yaml)
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML 解析失败: {exc}") from exc
        if not isinstance(parsed, dict) or "skill" not in parsed:
            raise ValueError("profile 必须是对象且含 skill 段")
        fields = {
            "title": data.get("title", row["title"]),
            "fault_name": data.get("fault_name", row["fault_name"]),
            "description": data.get("description", row["description"]),
            "profile_yaml": profile_yaml,
            "updated_at": iso_now(),
        }
        self.db.execute(
            "UPDATE workshop_drafts SET title=?, fault_name=?, description=?, profile_yaml=?, "
            "updated_at=?, status='editing' WHERE id=?",
            (fields["title"], fields["fault_name"], fields["description"],
             fields["profile_yaml"], fields["updated_at"], draft_id))
        return self.get(draft_id)

    # ---------- 日志与 AI 起草 ----------
    def attach_logs(self, draft_id: str, files: list[dict[str, Any]], *,
                    progress: Any = None, model_override: int | None = None) -> dict[str, Any]:
        row = self.get(draft_id)
        draft_dir = self.config.data_dir / "workshop" / draft_id
        log_dir = draft_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        for item in files:
            src = Path(item["stored_path"])
            shutil.move(str(src), unique_path(log_dir, src.name))
        self.db.execute("UPDATE workshop_drafts SET log_path=?, updated_at=? WHERE id=?",
                        (str(log_dir), iso_now(), draft_id))
        return self.analyze(draft_id, progress=progress, model_override=model_override)

    def analyze(self, draft_id: str, *, progress: Any = None,
                model_override: int | None = None) -> dict[str, Any]:
        row = self.get(draft_id)
        draft_dir = self.config.data_dir / "workshop" / draft_id
        log_dir = Path(row["log_path"]) if row.get("log_path") else draft_dir / "logs"
        self.db.execute("UPDATE workshop_drafts SET status='analyzing', updated_at=? WHERE id=?",
                        (iso_now(), draft_id))
        self._progress(progress, "unpack", 12, "正在盘点并解压日志包")
        extras: dict[str, Any] = {}
        if log_dir.exists() and any(p.is_file() for p in log_dir.iterdir()):
            extracted = draft_dir / "extracted"
            if extracted.exists():
                shutil.rmtree(extracted)
            limits = {"max_files": int(self.config.get("max_extract_files", 200000)),
                      "max_bytes": int(self.config.get("max_extract_mb", 8192)) * 1024 * 1024,
                      "max_depth": 3}
            try:
                extract_stats = extract_inputs([p for p in log_dir.iterdir() if p.is_file()],
                                               extracted, limits)
            except Exception as exc:  # noqa: BLE001
                self.db.execute("UPDATE workshop_drafts SET status='needs_confirmation', last_error=? WHERE id=?",
                                (f"日志解压失败: {exc}", draft_id))
                raise ValueError(f"日志解压失败: {exc}") from exc
            extras = {"extract": extract_stats, "rule_candidates": self._scan_rules(extracted),
                      "samples": sample_text_files(extracted, max_files=6, max_lines=30, max_chars=5000)}
        elif not row.get("source_text"):
            raise ValueError("请先粘贴故障资料或上传日志/案例包")
        self._progress(progress, "extract", 22, "日志盘点完成，开始语义提取")
        return self._generate(draft_id, extras, progress=progress, model_override=model_override)

    def _generate(self, draft_id: str, extras: dict[str, Any] | None = None, *,
                  progress: Any = None, model_override: int | None = None) -> dict[str, Any]:
        row = self.get(draft_id)
        source_text = str(row.get("source_text") or "").strip()
        analysis: dict[str, Any] = dict(extras or {})
        analysis.update({"model": "", "source": "deterministic", "notes": [], "attempts": []})
        if not source_text:
            # 兼容自学习和手工旧草稿：没有原始需求文本时保留人工 profile，不用空骨架覆盖。
            analysis["ai_status"] = "not_applicable"
            analysis["notes"].append("该草稿没有整段需求文本，保留现有人工 profile；可在“补充/纠正”中粘贴资料后重建。")
            self.db.execute("UPDATE workshop_drafts SET analysis_json=?, status='analyzed', updated_at=? WHERE id=?",
                            (json.dumps(analysis, ensure_ascii=False, default=str), iso_now(), draft_id))
            check = self.check(row["profile_yaml"])
            self.db.execute("UPDATE workshop_drafts SET check_json=?, updated_at=? WHERE id=?",
                            (json.dumps(check, ensure_ascii=False), iso_now(), draft_id))
            return self.get(draft_id)

        spec = parse_source(source_text)
        ai_status = "not_configured"
        budget = Budget(4, int(self.config.get("max_total_tokens", 200000)))

        def record(result: Any, stage: str) -> None:
            analysis["attempts"].append({
                "stage": stage, "model": result.model_name, "latency_ms": result.latency_ms,
                "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
            })

        if self.gateway.has_real_model("workshop") and self.gateway.available("workshop"):
            try:
                self._progress(progress, "extract", 30, "AI 正在提取故障语义与证据规则")
                result = self.gateway.complete(
                    "workshop", WORKSHOP_EXTRACT_SYSTEM,
                    workshop_extract_user(source_text, public_spec(spec)),
                    json_schema=WORKSHOP_SPEC_SCHEMA, purpose="workshop:extract", budget=budget,
                    max_tokens=min(12000, int(self.config.get("max_tokens", 16384))),
                    model_override=model_override)
                record(result, "extract")
                spec = merge_specs(spec, result.data or {})
                analysis["model"] = result.model_name
                analysis["source"] = "ai"
                ai_status = "success"
            except (AIError, NoModelError, ValueError, TypeError) as exc:
                ai_status = "failed"
                analysis["notes"].append(f"AI 语义提取失败，当前展示确定性解析结果：{exc}")
        else:
            if self.gateway.configured("workshop"):
                ai_status = "unavailable"
                analysis["notes"].append("workshop 模型已配置但暂不可用（可能处于熔断或故障状态），本次使用确定性解析；不会把该结果标记为 AI 生成。")
            else:
                analysis["notes"].append("未配置 workshop 真实模型，本次使用确定性解析；不会把该结果标记为 AI 生成。")

        quality = audit_spec(spec)
        review: dict[str, Any] = {}
        if ai_status == "success":
            try:
                self._progress(progress, "review", 58, "AI 正在审查规则与根因一致性")
                result = self.gateway.complete(
                    "workshop", WORKSHOP_REVIEW_SYSTEM,
                    workshop_review_user(source_text, public_spec(spec), quality),
                    json_schema=WORKSHOP_REVIEW_SCHEMA, purpose="workshop:review", budget=budget,
                    max_tokens=8000, temperature=0.1, model_override=model_override)
                record(result, "review")
                review = result.data or {}
                quality = self._merge_review(quality, review)
                if any(x.get("severity") == "error" for x in review.get("issues") or []):
                    self._progress(progress, "repair", 70, "根据评审问题自动修复技能规格")
                    repair = self.gateway.complete(
                        "workshop", WORKSHOP_EXTRACT_SYSTEM,
                        workshop_repair_user(source_text, public_spec(spec), review),
                        json_schema=WORKSHOP_SPEC_SCHEMA, purpose="workshop:repair", budget=budget,
                        max_tokens=min(12000, int(self.config.get("max_tokens", 16384))),
                        model_override=model_override)
                    record(repair, "repair")
                    spec = merge_specs(spec, repair.data or {})
                    quality = audit_spec(spec)
                    analysis["source"] = "ai+repair"
                    # 修复后重新评审一次；评审仍有问题就明确请求确认，不静默通过。
                    self._progress(progress, "review-final", 82, "正在做最终质量评审")
                    result = self.gateway.complete(
                        "workshop", WORKSHOP_REVIEW_SYSTEM,
                        workshop_review_user(source_text, public_spec(spec), quality),
                        json_schema=WORKSHOP_REVIEW_SCHEMA, purpose="workshop:review-final", budget=budget,
                        max_tokens=8000, temperature=0.1, model_override=model_override)
                    record(result, "review-final")
                    review = result.data or {}
                    quality = self._merge_review(quality, review)
            except (AIError, NoModelError, ValueError, TypeError) as exc:
                analysis["notes"].append(f"AI 评审/修复未完成，保留当前确定性校验结果：{exc}")
                quality = audit_spec(spec)

        analysis["intake"] = public_spec(spec)
        analysis["quality"] = quality
        analysis["review"] = review
        analysis["ai_status"] = ai_status
        analysis["generation"] = {
            "source": analysis["source"], "model": analysis["model"], "ai_status": ai_status,
            "attempts": analysis["attempts"], "generated_at": iso_now(),
        }
        spec["title"] = self._clean_title(spec)
        final_name = row["name"]
        if bool(row.get("name_auto")) and not bool(row.get("name_locked")):
            desired = derive_skill_name(spec, str((spec.get("skill_name") or "").strip()))
            final_name = self._unique_draft_name(desired, exclude_id=draft_id)
        profile = compile_profile(spec, final_name)
        profile_yaml = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)
        self._progress(progress, "check", 90, "正在执行技能结构与规则校验")
        check = self.check(profile_yaml)
        check["quality"] = quality
        blocking = any(q.get("blocking") for q in quality.get("questions") or [])
        status = "needs_confirmation" if not quality["ok"] or not check["ok"] or blocking else "generated"
        last_error = "" if check["ok"] else "；".join(check.get("errors") or ["profile 校验未通过"])
        self.db.execute(
            "UPDATE workshop_drafts SET name=?, title=?, fault_name=?, category=?, description=?, profile_yaml=?, "
            "analysis_json=?, check_json=?, status=?, last_error=?, updated_at=? WHERE id=?",
            (final_name, spec["title"], spec["fault_name"], spec.get("category") or "硬件故障",
             spec.get("summary") or row["description"], profile_yaml,
             json.dumps(analysis, ensure_ascii=False, default=str),
             json.dumps(check, ensure_ascii=False, default=str), status, last_error, iso_now(), draft_id))
        self._progress(progress, "done", 96, "技能草稿已生成，等待确认")
        return self.get(draft_id)

    @staticmethod
    def _merge_review(quality: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
        merged = dict(quality)
        merged["errors"] = list(quality.get("errors") or [])
        merged["warnings"] = list(quality.get("warnings") or [])
        merged["questions"] = list(quality.get("questions") or [])
        for item in review.get("issues") or []:
            target = merged["errors"] if item.get("severity") == "error" else merged["warnings"]
            target.append({"field": str(item.get("field") or "review"),
                           "message": str(item.get("message") or item.get("suggestion") or "AI 评审问题")})
        for item in review.get("questions") or []:
            if item.get("question"):
                merged["questions"].append(item)
        merged["ok"] = not merged["errors"]
        if isinstance(review.get("score"), int):
            merged["score"] = min(int(quality.get("score") or 0), max(0, min(100, review["score"])))
        merged["questions"] = merged["questions"][:4]
        return merged

    def refine(self, draft_id: str, text: str, *, progress: Any = None,
               model_override: int | None = None) -> dict[str, Any]:
        row = self.get(draft_id)
        if row["status"] == "published":
            raise ValueError("已发布草稿不可重建，请新建版本")
        addition = str(text or "").strip()
        if len(addition) < 2:
            raise ValueError("请填写补充或纠正内容")
        source_text = (str(row.get("source_text") or "").strip()
                       + "\n\n## 用户补充确认\n" + addition).strip()
        self.db.execute("UPDATE workshop_drafts SET source_text=?, status='generating', updated_at=? WHERE id=?",
                        (source_text, iso_now(), draft_id))
        return self._generate(draft_id, progress=progress, model_override=model_override)


    def _scan_rules(self, root: Path) -> dict[str, Any]:
        script = self.meta / "scripts" / "scan_rule_candidates.py"
        if not script.exists():
            return {"rule_suggestions": [], "error": "元 Skill 缺少 scan_rule_candidates.py"}
        out = root.parent / "rule_candidates.json"
        try:
            proc = subprocess.run(
                [sys.executable, str(script), str(root), "--json", str(out), "--top", "8"],
                cwd=str(self.meta), env=env_with_vendor(), capture_output=True,
                text=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"rule_suggestions": [], "error": str(exc)}
        data = read_json(out, {}) or {}
        if proc.returncode != 0:
            data.setdefault("error", (proc.stderr or proc.stdout)[-800:])
        return data

    # ---------- 校验与构建 ----------
    def check(self, profile_yaml: str) -> dict[str, Any]:
        draft_dir = self.config.data_dir / "workshop" / "_check"
        draft_dir.mkdir(parents=True, exist_ok=True)
        path = write_text(draft_dir / f"profile-{int(time.time() * 1000)}.yaml", profile_yaml)
        script = self.meta / "scripts" / "scaffold_child_skill.py"
        if not script.exists():
            return {"ok": False, "output": "元 Skill 缺少 scaffold_child_skill.py"}
        try:
            proc = subprocess.run(
                [sys.executable, str(script), "--profile", str(path), "--check"],
                cwd=str(self.meta), env=env_with_vendor(), capture_output=True,
                text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "output": str(exc)}
        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": output[:6000],
            "errors": self._extract_errors(output),
            "unverified_rules": self._unverified_rules(profile_yaml),
        }
        # path 保留用于排错；正常流程由 /check 复用

    def _unverified_rules(self, profile_yaml: str) -> list[str]:
        """列出 profile 中尚未标记 verified 的规则，用于工坊一键修正引导。"""
        try:
            parsed = yaml.safe_load(profile_yaml) or {}
        except yaml.YAMLError:
            return []
        ids: list[str] = []
        for step in (parsed.get("analysis") or {}).get("steps") or []:
            for rule in (step or {}).get("rules") or []:
                rid = str((rule or {}).get("id") or "").strip()
                if rid and not (rule or {}).get("verified"):
                    ids.append(rid)
        return ids

    def mark_rules_verified(self, draft_id: str, rule_ids: list[str] | None = None) -> dict[str, Any]:
        """人工确认后把指定规则标记为 verified。

        仅在工坊草稿阶段可用；规则是否真的成立仍需通过案例回归或人工复核。
        """
        row = self.get(draft_id)
        if row["status"] == "published":
            raise ValueError("已发布的草稿不可修改规则，请新建版本")
        profile_yaml = row["profile_yaml"]
        try:
            parsed = yaml.safe_load(profile_yaml) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML 解析失败: {exc}") from exc
        wanted = {str(x).strip() for x in (rule_ids or []) if str(x).strip()}
        changed: list[str] = []
        for step in (parsed.get("analysis") or {}).get("steps") or []:
            for rule in (step or {}).get("rules") or []:
                rid = str((rule or {}).get("id") or "").strip()
                if not rid or (rule or {}).get("verified"):
                    continue
                if wanted and rid not in wanted:
                    continue
                rule["verified"] = True
                changed.append(rid)
        if not changed:
            raise ValueError("没有需要标记的未验证规则")
        new_yaml = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
        self.update_draft(draft_id, {"profile_yaml": new_yaml})
        check = self.check(new_yaml)
        self.db.execute("UPDATE workshop_drafts SET check_json=?, updated_at=? WHERE id=?",
                        (json.dumps(check, ensure_ascii=False), iso_now(), draft_id))
        return {"ok": True, "changed": changed, "profile_yaml": new_yaml, "check": check}

    def _extract_errors(self, output: str) -> list[str]:
        lines = []
        for line in output.splitlines():
            s = line.strip()
            if not s:
                continue
            if re.search(r"(错误|ERROR|Error|失败|非法|缺少|为空)", s):
                lines.append(s[:300])
        return lines[:20]

    def build(self, draft_id: str) -> dict[str, Any]:
        row = self.get(draft_id)
        quality = (row.get("analysis") or {}).get("quality") or {}
        if quality and not quality.get("ok", True):
            return {"ok": False, "stage": "quality", "error": "技能质量审查未通过，请先处理错误或补充确认",
                    "quality": quality}
        draft_dir = self.config.data_dir / "workshop" / draft_id
        profile_path = draft_dir / "profile.yaml"
        write_text(profile_path, row["profile_yaml"])
        check = self.check(row["profile_yaml"])
        self.db.execute("UPDATE workshop_drafts SET check_json=?, updated_at=? WHERE id=?",
                        (json.dumps(check, ensure_ascii=False), iso_now(), draft_id))
        if not check["ok"]:
            self.db.execute("UPDATE workshop_drafts SET status='editing' WHERE id=?", (draft_id,))
            return {"ok": False, "stage": "check", "check": check}
        self.db.execute("UPDATE workshop_drafts SET name_locked=1, updated_at=? WHERE id=?",
                        (iso_now(), draft_id))
        parsed = yaml.safe_load(row["profile_yaml"]) or {}
        name = (parsed.get("skill") or {}).get("name") or row["name"]
        version = str((parsed.get("skill") or {}).get("version") or "0.1.0")
        staging = draft_dir / "build"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        build_json = draft_dir / "build-result.json"
        cases = self.config.cases_dir / name
        verified_mode = bool(parsed.get("root_causes")) and cases.exists()
        cmd = [sys.executable, str(self.meta / "scripts" / "build_child_skill.py"),
               "--profile", str(profile_path), "--path", str(staging),
               "--json", str(build_json)]
        if verified_mode:
            cmd += ["--cases-dir", str(self.config.cases_dir)]
        else:
            cmd += ["--skip-regression"]
        try:
            proc = subprocess.run(cmd, cwd=str(self.meta), env=env_with_vendor(),
                                  capture_output=True, text=True, timeout=900)
        except (subprocess.TimeoutExpired, OSError) as exc:
            self.db.execute("UPDATE workshop_drafts SET status='failed', last_error=? WHERE id=?",
                            (str(exc), draft_id))
            return {"ok": False, "stage": "build", "error": str(exc)}
        result = read_json(build_json, {}) or {}
        output = (proc.stdout or "") + (proc.stderr or "")
        skill_dir = staging / name
        ok = proc.returncode == 0 and skill_dir.exists()
        payload = {
            "ok": ok,
            "verified": bool(ok and verified_mode),
            "version": version,
            "exit_code": proc.returncode,
            "output": output[-6000:],
            "result": result,
            "skill_dir": str(skill_dir) if skill_dir.exists() else "",
        }
        if not ok:
            self.db.execute("UPDATE workshop_drafts SET status='failed', build_json=?, "
                            "last_error=?, updated_at=? WHERE id=?",
                            (json.dumps(payload, ensure_ascii=False, default=str),
                             output[-2000:], iso_now(), draft_id))
            return payload
        # 归档版本
        version_dir = self.config.data_dir / "skill_versions" / name / version
        if version_dir.exists():
            shutil.rmtree(version_dir)
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dir, version_dir)
        write_json(version_dir / ".bbu-workshop.json",
                   {"draft_id": draft_id, "built_at": iso_now(),
                    "verified": verified_mode, "name": name, "version": version})
        self.db.execute(
            "INSERT INTO skill_versions(skill_name, version, path, status, build_json, note, created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(skill_name, version) DO UPDATE SET "
            "path=excluded.path, status='built', build_json=excluded.build_json, note=excluded.note",
            (name, version, str(version_dir), "built",
             json.dumps(result, ensure_ascii=False, default=str),
             "案例回归通过" if verified_mode else "草稿构建（未跑案例回归）", iso_now()))
        self.db.execute(
            "UPDATE workshop_drafts SET status='built', build_json=?, skill_version=?, "
            "last_error='', updated_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False, default=str), version,
             iso_now(), draft_id))
        return payload

    def publish(self, draft_id: str, username: str) -> dict[str, Any]:
        row = self.get(draft_id)
        build = row.get("build") or {}
        if not build.get("ok"):
            raise ValueError("尚未成功构建，不能发布")
        if not build.get("verified"):
            raise ValueError("技能尚未通过真实案例回归，只能保留为草稿，不能发布")
        quality = (row.get("analysis") or {}).get("quality") or {}
        if quality and not quality.get("ok", True):
            raise ValueError("技能质量审查未通过，请先处理错误或补充确认")
        name = row["name"]
        kinds = set()
        case_root = self.config.cases_dir / name
        if case_root.exists():
            for path in case_root.glob("*/case.yaml"):
                try:
                    kinds.add(str((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("kind") or ""))
                except (OSError, yaml.YAMLError):
                    continue
        if "positive" not in kinds or "negative" not in kinds:
            raise ValueError("发布前至少需要 1 个故障正例和 1 个正常/反例，并全部通过回归")
        version = build.get("version") or "0.1.0"
        src = self.config.data_dir / "skill_versions" / name / version
        if not src.exists():
            raise ValueError("构建产物不存在，请重新构建")
        target = self.config.skills_dir / name
        backup = None
        if target.exists():
            backup = self.config.data_dir / "skill_versions" / name / f".backup-{int(time.time())}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        try:
            shutil.copytree(src, target)
            write_json(target / ".bbu-workshop.json",
                       {"draft_id": draft_id, "published_at": iso_now(),
                        "published_by": username, "verified": build.get("verified")})
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            if backup:
                shutil.move(str(backup), str(target))
            raise
        self.db.execute(
            "UPDATE skill_versions SET status='published' WHERE skill_name=? AND version=?",
            (name, version))
        self.db.execute("UPDATE workshop_drafts SET status='published', last_error='', updated_at=? WHERE id=?",
                        (iso_now(), draft_id))
        self.registry.scan()
        self.db.audit(None, username, "skill.publish", name,
                      {"version": version, "verified": bool(build.get("verified"))})
        return {"ok": True, "skill": name, "version": version,
                "verified": bool(build.get("verified"))}

    def rollback(self, skill_name: str, version: str, username: str) -> dict[str, Any]:
        src = self.config.data_dir / "skill_versions" / skill_name / version
        if not src.exists():
            raise ValueError("目标版本不存在")
        target = self.config.skills_dir / skill_name
        backup = None
        if target.exists():
            backup = self.config.data_dir / "skill_versions" / skill_name / f".rollback-{int(time.time())}"
            shutil.move(str(target), str(backup))
        try:
            shutil.copytree(src, target)
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            if backup:
                shutil.move(str(backup), str(target))
            raise
        self.db.execute(
            "UPDATE skill_versions SET status='rolled_back' WHERE skill_name=? AND version<>?",
            (skill_name, version))
        self.db.execute(
            "UPDATE skill_versions SET status='published' WHERE skill_name=? AND version=?",
            (skill_name, version))
        self.registry.scan()
        self.db.audit(None, username, "skill.rollback", skill_name, {"version": version})
        return {"ok": True, "skill": skill_name, "version": version}

    # ---------- 案例 ----------
    def attach_case(self, draft_id: str, files: list[dict[str, Any]],
                    expected: dict[str, Any]) -> dict[str, Any]:
        row = self.get(draft_id)
        name = row["name"]
        case_id = str(expected.get("case_id") or "").strip() or self.next_case_id(draft_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", case_id):
            raise ValueError("案例 ID 只能是小写字母/数字/连字符")
        kind = str(expected.get("kind") or "positive").strip()
        if kind not in ("positive", "negative"):
            raise ValueError("案例类型只能是正例（positive）或反例（negative）")
        root_cause = str(expected.get("root_cause") or ("NONE" if kind == "negative" else "")).strip()
        hit_rules = self._case_rules(expected.get("hit_rules"))
        not_hit_rules = self._case_rules(expected.get("not_hit_rules"))
        if kind == "negative":
            root_cause = "NONE"
            hit_rules = []
        elif not root_cause or root_cause == "NONE":
            raise ValueError("正例必须选择期望根因，不能为 NONE")
        existing = self.db.one(
            "SELECT id FROM cases WHERE skill_name=? AND case_id=?", (name, case_id))
        existing_dir = self.config.cases_dir / name / case_id
        overwrite = bool(expected.get("overwrite"))
        if (existing or existing_dir.exists()) and not overwrite:
            raise ValueError(f"案例 {case_id} 已存在；如需覆盖，请明确勾选“覆盖已有案例”")
        script = self.meta / "scripts" / "init_case.py"
        cmd = [sys.executable, str(script), "--cases-dir", str(self.config.cases_dir),
               "--skill", name, "--id", case_id, "--kind", kind]
        if overwrite:
            cmd += ["--force"]
        try:
            proc = subprocess.run(cmd, cwd=str(self.meta), env=env_with_vendor(),
                                  capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ValueError(f"案例初始化失败: {exc}") from exc
        if proc.returncode != 0:
            raise ValueError("案例初始化失败: " + ((proc.stderr or proc.stdout or "未知错误")[-800:]))
        case_dir = self.config.cases_dir / name / case_id
        input_dir = case_dir / "input"
        if overwrite and input_dir.exists():
            shutil.rmtree(input_dir)
        input_dir.mkdir(parents=True, exist_ok=True)
        archive_path = ""
        for item in files:
            src = Path(item["stored_path"])
            original = safe_filename(str(item.get("name") or src.name))
            dest = unique_path(input_dir, original)
            shutil.move(str(src), dest)
            if not archive_path:
                archive_path = f"input/{dest.name}"
        case_yaml = case_dir / "case.yaml"
        data = yaml.safe_load(case_yaml.read_text(encoding="utf-8")) if case_yaml.exists() else {}
        data = data or {}
        data.update({
            "id": case_id,
            "skill": name,
            "description": str(expected.get("description") or row["title"]),
            "kind": kind,
            "device": str(expected.get("device") or ""),
            "tags": expected.get("tags") or [],
            "input": {"archive": archive_path},
            "pipeline": ["unpack", "search"],
        })
        if expected.get("unpacked_dir"):
            data["input"] = {"unpacked_dir": str(expected["unpacked_dir"])}
        exp = {
            "root_cause": root_cause,
            "hit_rules": hit_rules,
            "not_hit_rules": not_hit_rules,
        }
        if expected.get("match_counts"):
            exp["match_counts"] = expected["match_counts"]
        if expected.get("evidence_contains"):
            exp["evidence_contains"] = expected["evidence_contains"]
        data["expected"] = exp
        write_text(case_yaml, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        self.db.execute(
            "INSERT INTO cases(skill_name, case_id, kind, path, expected_root_cause, description, created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(skill_name, case_id) DO UPDATE SET "
            "kind=excluded.kind, path=excluded.path, expected_root_cause=excluded.expected_root_cause, "
            "description=excluded.description",
            (name, case_id, kind, str(case_dir), exp["root_cause"],
             data["description"], iso_now()))
        self.db.execute("UPDATE workshop_drafts SET case_path=?, name_locked=1, updated_at=? WHERE id=?",
                        (str(case_dir), iso_now(), draft_id))
        unknown = self._unknown_case_refs(row["profile_yaml"], root_cause, hit_rules, not_hit_rules)
        return {"ok": True, "case_id": case_id, "path": str(case_dir),
                "warning": ("；".join(unknown) if unknown else "")}

    @staticmethod
    def _case_rules(value: Any) -> list[str]:
        if isinstance(value, str):
            value = re.split(r"[,，、\s]+", value)
        return [str(x).strip() for x in (value or []) if str(x).strip()]

    @staticmethod
    def _unknown_case_refs(profile_yaml: str, root_cause: str,
                           hit_rules: list[str], not_hit_rules: list[str]) -> list[str]:
        try:
            profile = yaml.safe_load(profile_yaml) or {}
        except yaml.YAMLError:
            return []
        root_ids = {str(x.get("id") or "") for x in profile.get("root_causes") or []}
        rule_ids = {str(r.get("id") or "") for step in (profile.get("analysis") or {}).get("steps") or []
                    for r in (step or {}).get("rules") or []}
        warnings = []
        if root_cause != "NONE" and root_ids and root_cause not in root_ids:
            warnings.append(f"根因 {root_cause} 不在当前技能根因列表中，回归可能无法判定")
        for rid in hit_rules + not_hit_rules:
            if rule_ids and rid not in rule_ids:
                warnings.append(f"规则 {rid} 不在当前技能规则列表中")
        return warnings

    def next_case_id(self, draft_id: str) -> str:
        row = self.get(draft_id)
        existing = {r["case_id"] for r in self.db.all(
            "SELECT case_id FROM cases WHERE skill_name=?", (row["name"],))}
        idx = 1
        while f"case-{idx:02d}" in existing:
            idx += 1
        return f"case-{idx:02d}"

    def list_cases(self, draft_id: str) -> list[dict[str, Any]]:
        row = self.get(draft_id)
        rows = self.db.all(
            "SELECT case_id, kind, path, expected_root_cause, description, created_at "
            "FROM cases WHERE skill_name=? ORDER BY created_at DESC, case_id", (row["name"],))
        out: list[dict[str, Any]] = []
        for item in rows:
            path = Path(item["path"])
            expected: dict[str, Any] = {}
            files: list[dict[str, Any]] = []
            yaml_path = path / "case.yaml"
            if yaml_path.exists():
                try:
                    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
                    expected = data.get("expected") or {}
                except (OSError, yaml.YAMLError):
                    expected = {}
            input_dir = path / "input"
            if input_dir.exists():
                for f in sorted(p for p in input_dir.iterdir() if p.is_file()):
                    files.append({"name": f.name, "size": f.stat().st_size})
            out.append({**item, "expected": expected, "files": files,
                        "total_bytes": sum(f["size"] for f in files)})
        return out

    # ---------- 版本 ----------
    def list_versions(self, skill_name: str | None = None) -> list[dict[str, Any]]:
        if skill_name:
            return self.db.all(
                "SELECT * FROM skill_versions WHERE skill_name=? ORDER BY created_at DESC",
                (skill_name,))
        return self.db.all("SELECT * FROM skill_versions ORDER BY created_at DESC LIMIT 200")
