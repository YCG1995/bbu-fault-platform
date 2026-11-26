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
from .approvals import publish_gate
from .data_egress import DataEgressPolicy
from .db import Database
from .evidence_digest import build_digest
from .prompts import (WORKSHOP_EXTRACT_SYSTEM, WORKSHOP_REVIEW_SCHEMA,
                      WORKSHOP_REVIEW_SYSTEM, WORKSHOP_SPEC_SCHEMA,
                      workshop_extract_user, workshop_repair_user,
                      workshop_review_final_user, workshop_review_user)
from .skills import SkillRegistry
from .workshop_spec import (audit_spec, compile_profile, derive_skill_name,
                            merge_specs, model_spec, parse_source, public_spec)
from .workshop_recipes import list_recipes
from .storage import extract_inputs, sample_text_files
from .util import (env_with_vendor, iso_now, new_id, read_json, safe_filename,
                   sha256_file, unique_path, write_json, write_text)

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
        self.domain_pack = str(getattr(config, "domain_pack", "bbu") or "bbu")
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

    def _invalidate_build(self, draft_id: str) -> None:
        """分析、规则或案例变化后，旧构建/旧版本结论必须失效。"""
        self.db.execute(
            "UPDATE workshop_drafts SET build_json='{}', skill_version=NULL WHERE id=?",
            (draft_id,))

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

    @staticmethod
    def _normalize_recipe_id(value: Any) -> str:
        rid = str(value or "").strip()
        if rid in ("", "auto", "none", "generic", "off"):
            return rid
        if not any(recipe.get("id") == rid for recipe in list_recipes()):
            raise ValueError(f"未知领域配方: {rid}")
        return rid

    def recipes(self) -> list[dict[str, Any]]:
        return list_recipes()

    def _ensure_new_name(self, name: str) -> None:
        if self.db.one("SELECT name FROM skills WHERE name=?", (name,)) or self.db.one(
                "SELECT id FROM workshop_drafts WHERE name=?", (name,)):
            raise ValueError(f"技能 {name} 已存在；请改名，或走版本更新")

    def _create_from_text(self, user_id: int, data: dict[str, Any], source_text: str,
                          *, deferred: bool = False, model_override: int | None = None,
                          progress: Any = None) -> dict[str, Any]:
        if len(source_text) < 20:
            raise ValueError("请粘贴完整故障资料，至少需要故障名称、日志特征和判断思路")
        recipe_id = self._normalize_recipe_id(data.get("recipe_id"))
        spec = parse_source(source_text, recipe_id=recipe_id)
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
        stored_recipe = recipe_id or "auto"
        self.db.execute(
            "INSERT INTO workshop_drafts(id, owner_id, name, title, fault_name, category, status, "
            "profile_yaml, description, source_text, recipe_id, name_auto, name_locked, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (draft_id, user_id, name, spec["title"], spec["fault_name"], spec.get("category") or "硬件故障",
             "queued" if deferred else "generating",
             yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
             spec.get("summary") or "", source_text, stored_recipe, 1, 0, now, now))
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
        fields: dict[str, Any] = {
            "title": data.get("title", row["title"]),
            "fault_name": data.get("fault_name", row["fault_name"]),
            "description": data.get("description", row["description"]),
            "profile_yaml": profile_yaml,
            "updated_at": iso_now(),
        }
        status = "editing"
        if "recipe_id" in data:
            recipe_id = self._normalize_recipe_id(data.get("recipe_id"))
            fields["recipe_id"] = recipe_id
            if recipe_id != (row.get("recipe_id") or ""):
                # 换配方等同换领域规则，旧分析/校验/构建结论不可复用。
                fields["analysis_json"] = "{}"
                fields["check_json"] = "{}"
                fields["build_json"] = "{}"
                fields["skill_version"] = None
                fields["last_error"] = None
                status = "editing"
        if profile_yaml != (row.get("profile_yaml") or ""):
            fields["check_json"] = "{}"
            self._invalidate_build(draft_id)
        if "source_text" in data:
            source_text = str(data.get("source_text") or "").strip()
            if len(source_text) < 20:
                raise ValueError("请粘贴完整故障资料，至少需要故障名称、日志特征和判断思路")
            fields["source_text"] = source_text
            if source_text != (row.get("source_text") or "").strip():
                # 原文变化后，后续规则审查/构建/回归结论都失效，必须重新分析。
                fields["analysis_json"] = "{}"
                fields["check_json"] = "{}"
                fields["build_json"] = "{}"
                fields["last_error"] = None
                status = "editing"
        columns = list(fields)
        self.db.execute(
            "UPDATE workshop_drafts SET " + ", ".join(f"{name}=?" for name in columns) +
            ", status=? WHERE id=?",
            (*(fields[name] for name in columns), status, draft_id))
        return self.get(draft_id)

    def delete_draft(self, draft_id: str) -> dict[str, Any]:
        """删除草稿记录及其工作目录；已发布技能本身不回滚、不删除。"""
        row = self.get(draft_id)
        active = self.db.one(
            "SELECT id FROM workshop_jobs WHERE draft_id=? AND status IN ('queued','running') "
            "ORDER BY created_at DESC LIMIT 1", (draft_id,))
        if active:
            raise ValueError("草稿有后台任务正在执行，请等待完成或重启服务后再删除")
        published = row.get("status") == "published"
        self.db.execute("DELETE FROM workshop_jobs WHERE draft_id=?", (draft_id,))
        self.db.execute("DELETE FROM workshop_drafts WHERE id=?", (draft_id,))
        draft_dir = (Path(self.config.data_dir) / "workshop" / draft_id).resolve()
        try:
            draft_dir.relative_to((Path(self.config.data_dir) / "workshop").resolve())
            if draft_dir.is_dir():
                shutil.rmtree(draft_dir, ignore_errors=True)
        except (OSError, ValueError):
            pass
        return {"ok": True, "id": draft_id, "published_skill_kept": published}

    # ---------- 日志与 AI 起草 ----------
    def attach_logs(self, draft_id: str, files: list[dict[str, Any]], *,
                    progress: Any = None, model_override: int | None = None,
                    trace: Any = None) -> dict[str, Any]:
        row = self.get(draft_id)
        draft_dir = self.config.data_dir / "workshop" / draft_id
        log_dir = draft_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        for item in files:
            src = Path(item["stored_path"])
            shutil.move(str(src), unique_path(log_dir, src.name))
        self.db.execute("UPDATE workshop_drafts SET log_path=?, updated_at=? WHERE id=?",
                        (str(log_dir), iso_now(), draft_id))
        return self.analyze(draft_id, progress=progress, model_override=model_override,
                            trace=trace)

    def analyze(self, draft_id: str, *, progress: Any = None,
                model_override: int | None = None, trace: Any = None) -> dict[str, Any]:
        row = self.get(draft_id)
        self._invalidate_build(draft_id)
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
            rule_candidates = self._scan_rules(extracted)
            digest = build_digest(
                extracted,
                max_chars=int(self.config.get("workshop_digest_max_chars",
                                              self.config.get("workshop_egress_max_chars", 12000))),
                max_lines=int(self.config.get("workshop_digest_max_lines", 4000)),
                candidates=rule_candidates)
            extras = {"extract": extract_stats, "rule_candidates": rule_candidates,
                      "digest": digest,
                      "samples": sample_text_files(
                          extracted, max_files=6, max_lines=30, max_chars=5000, digest=digest)}
        elif not row.get("source_text"):
            raise ValueError("请先粘贴故障资料或上传日志/案例包")
        self._progress(progress, "extract", 22, "日志盘点完成，开始语义提取")
        if trace:
            trace.append("stage", phase="extract", status="running", progress=22,
                         message="日志盘点完成，开始语义提取")
        return self._generate(draft_id, extras, progress=progress,
                              model_override=model_override, trace=trace)

    def _generate(self, draft_id: str, extras: dict[str, Any] | None = None, *,
                  progress: Any = None, model_override: int | None = None,
                  trace: Any = None) -> dict[str, Any]:
        row = self.get(draft_id)
        self._invalidate_build(draft_id)
        source_text = str(row.get("source_text") or "").strip()
        analysis: dict[str, Any] = dict(extras or {})
        analysis.update({"model": "", "source": "deterministic", "notes": [], "attempts": [],
                         "extract_status": "not_run", "review_status": "not_run",
                         "repair_status": "not_run"})
        digest = analysis.get("digest") or {}
        if not source_text and digest.get("summary_text"):
            source_text = str(digest["summary_text"])
            analysis["upload_only"] = True
            analysis["notes"].append("未粘贴需求文本，使用日志证据摘要作为确定性来源；请人工确认技能语义。")
        if not source_text:
            # 兼容自学习和手工旧草稿：没有原始需求文本时保留人工 profile，不用空骨架覆盖。
            analysis["ai_status"] = "not_applicable"
            analysis["extract_status"] = "not_applicable"
            analysis["review_status"] = "not_applicable"
            analysis["repair_status"] = "not_applicable"
            analysis["notes"].append("该草稿没有整段需求文本，保留现有人工 profile；可在“补充/纠正”中粘贴资料后重建。")
            self.db.execute("UPDATE workshop_drafts SET analysis_json=?, status='analyzed', updated_at=? WHERE id=?",
                            (json.dumps(analysis, ensure_ascii=False, default=str), iso_now(), draft_id))
            check = self.check(row["profile_yaml"])
            self.db.execute("UPDATE workshop_drafts SET check_json=?, updated_at=? WHERE id=?",
                            (json.dumps(check, ensure_ascii=False), iso_now(), draft_id))
            return self.get(draft_id)

        spec = parse_source(source_text, recipe_id=str(row.get("recipe_id") or ""))
        if analysis.get("upload_only"):
            # 纯日志草稿没有需求文本，必须从确定性摘要生成可执行规则，避免空 profile。
            candidates = analysis.get("rule_candidates") or {}
            suggestions = (candidates.get("rule_suggestions")
                           if isinstance(candidates, dict) else []) or []
            rules = []
            for idx, item in enumerate(suggestions[:8], 1):
                if not isinstance(item, dict):
                    continue
                any_of = [str(x) for x in item.get("any_of") or [] if str(x).strip()]
                if not any_of:
                    continue
                rules.append({
                    "id": f"E{idx}", "description": str(item.get("description") or "")[:120],
                    "globs": [str(x) for x in item.get("globs") or []][:4] or ["**/*.log*"],
                    "all_of": [], "any_of": any_of[:6], "none_of": [],
                    "min_matches": 1, "verified": False, "critical": False,
                })
            if rules and not spec.get("evidence_rules"):
                spec["evidence_rules"] = rules
                spec["compiler"] = "log-digest"
                spec["summary"] = spec.get("summary") or "根据上传日志的确定性摘要生成候选证据规则"
            sources = []
            seen = set()
            for sample in digest.get("samples") or []:
                file = str(sample.get("file") or "").replace("\\", "/")
                base = file.rsplit("/", 1)[-1]
                key = sample.get("class") or base
                if not base or key in seen:
                    continue
                seen.add(key)
                sources.append({"name": str(sample.get("class") or "日志"),
                                "globs": [f"**/{base}", f"**/*{Path(base).suffix}"],
                                "paths": [file], "notes": "来自日志摘要的代表样本"})
            if sources and not spec.get("log_sources"):
                spec["log_sources"] = sources
        ai_status = "not_configured"
        budget = Budget(4, int(self.config.get("max_total_tokens", 200000)))
        egress = DataEgressPolicy.for_workshop(self.config)
        egress_audit = egress.describe()
        analysis["data_egress"] = egress_audit

        def safe_spec(value: dict[str, Any]) -> dict[str, Any]:
            # 模型只看业务字段；provenance/diagnostic_reasoning 等只留给 UI 和审计。
            return egress.sanitize_structure(model_spec(value))

        def attempt_sink(entry: dict[str, Any]) -> None:
            analysis["attempts"].append(entry)
            if entry.get("status") == "success":
                egress_audit.setdefault("stages", []).append({
                    "stage": entry.get("stage") or "model",
                    **egress.signal_audit({"model": entry.get("model") or ""}),
                })

        if self.gateway.has_real_model("workshop") and self.gateway.available("workshop"):
            source_context, excerpt_audit = egress.excerpt(source_text)
            egress_audit.update(excerpt_audit)
            egress_audit["stages"] = []
            self.db.audit(row.get("owner_id"), None, "workshop_model_egress", draft_id,
                          egress_audit)
            try:
                self._progress(progress, "extract", 30, "AI 正在提取故障语义与证据规则")
                result = self.gateway.complete(
                    "workshop", WORKSHOP_EXTRACT_SYSTEM,
                    workshop_extract_user(source_context, safe_spec(spec),
                                          digest if digest else None),
                    json_schema=WORKSHOP_SPEC_SCHEMA, purpose="workshop:extract", budget=budget,
                    max_tokens=min(12000, int(self.config.get("max_tokens", 16384))),
                    model_override=model_override, stage="extract", stream=True,
                    attempt_sink=attempt_sink, trace=trace)
                spec = merge_specs(spec, result.data or {})
                analysis["model"] = result.model_name
                analysis["source"] = "ai"
                analysis["extract_status"] = "success"
                ai_status = "success"
            except (AIError, NoModelError, ValueError, TypeError) as exc:
                ai_status = "failed"
                analysis["extract_status"] = "failed"
                analysis["notes"].append(f"AI 语义提取失败，当前展示确定性解析结果：{exc}")
        else:
            if self.gateway.configured("workshop"):
                ai_status = "unavailable"
                analysis["extract_status"] = "unavailable"
                analysis["review_status"] = "skipped"
                analysis["repair_status"] = "skipped"
                analysis["notes"].append("workshop 模型已配置但暂不可用（可能处于熔断或故障状态），本次使用确定性解析；不会把该结果标记为 AI 生成。")
            else:
                analysis["extract_status"] = "not_configured"
                analysis["review_status"] = "skipped"
                analysis["repair_status"] = "skipped"
                analysis["notes"].append("未配置 workshop 真实模型，本次使用确定性解析；不会把该结果标记为 AI 生成。")

        quality = audit_spec(spec)
        review: dict[str, Any] = {}
        # 评审只输出问题列表，不需要完整 spec；实测 4k 会在复杂配方上截断，
        # 5k 足够容纳 6 个问题，同时避免 8k 长响应更容易触发 IncompleteRead。
        review_budget = min(5000, int(self.config.get("max_tokens", 16384)))
        if ai_status == "success":
            try:
                self._progress(progress, "review", 58, "AI 正在审查规则与根因一致性")
                analysis["review_status"] = "failed"
                result = self.gateway.complete(
                    "workshop", WORKSHOP_REVIEW_SYSTEM,
                    workshop_review_user(source_context, safe_spec(spec), quality),
                    json_schema=WORKSHOP_REVIEW_SCHEMA, purpose="workshop:review", budget=budget,
                    max_tokens=review_budget, temperature=0.1, model_override=model_override,
                    stage="review", stream=True, attempt_sink=attempt_sink, trace=trace)
                review = result.data or {}
                analysis["review_status"] = "success"
                quality = self._merge_review(quality, review)
                if any(x.get("severity") == "error" for x in review.get("issues") or []):
                    self._progress(progress, "repair", 70, "根据评审问题自动修复技能规格")
                    analysis["repair_status"] = "failed"
                    repair = self.gateway.complete(
                        "workshop", WORKSHOP_EXTRACT_SYSTEM,
                        workshop_repair_user(source_context, safe_spec(spec), review),
                        json_schema=WORKSHOP_SPEC_SCHEMA, purpose="workshop:repair", budget=budget,
                        max_tokens=min(12000, int(self.config.get("max_tokens", 16384))),
                        model_override=model_override, stage="repair", stream=True,
                        attempt_sink=attempt_sink, trace=trace)
                    spec = merge_specs(spec, repair.data or {})
                    analysis["repair_status"] = "success"
                    quality = audit_spec(spec)
                    analysis["source"] = "ai+repair"
                    # 修复后重新评审一次；评审仍有问题就明确请求确认，不静默通过。
                    self._progress(progress, "review-final", 82, "正在做最终质量评审")
                    analysis["review_status"] = "failed"
                    result = self.gateway.complete(
                        "workshop", WORKSHOP_REVIEW_SYSTEM,
                        workshop_review_final_user(safe_spec(spec), quality, review),
                        json_schema=WORKSHOP_REVIEW_SCHEMA, purpose="workshop:review-final",
                        budget=budget, max_tokens=min(3000, review_budget),
                        temperature=0.1, model_override=model_override, stage="review-final",
                        stream=True, attempt_sink=attempt_sink, trace=trace)
                    review = result.data or {}
                    analysis["review_status"] = "success"
                    quality = self._merge_review(quality, review)
                else:
                    analysis["repair_status"] = "skipped"
            except (AIError, NoModelError, ValueError, TypeError) as exc:
                analysis["notes"].append(f"AI 评审/修复未完成，保留当前确定性校验结果：{exc}")
                # 评审已经给出结论时，修复失败不能把评审 error 丢掉；否则可能误放行。
                quality = self._merge_review(audit_spec(spec), review) if review else audit_spec(spec)
        else:
            analysis["review_status"] = "skipped"
            analysis["repair_status"] = "skipped"

        if digest:
            analysis["digest"] = {
                "inventory": digest.get("inventory") or {},
                "timeline": digest.get("timeline") or [],
                "candidates": digest.get("candidates") or [],
                "samples": digest.get("samples") or [],
                "coverage_notes": digest.get("coverage_notes") or [],
                "anomaly_count": len(digest.get("anomaly_lines") or []),
            }
        analysis["intake"] = public_spec(spec)
        analysis["quality"] = quality
        analysis["review"] = review
        analysis["ai_status"] = ai_status
        analysis["generation"] = {
            "source": analysis["source"], "model": analysis["model"], "ai_status": ai_status,
            "extract_status": analysis["extract_status"],
            "review_status": analysis["review_status"],
            "repair_status": analysis["repair_status"],
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
        if trace:
            quality_errors = [str(x.get("message") or x) for x in quality.get("errors") or []]
            trace.validation(
                phase="check", name="workshop_profile_and_quality",
                ok=bool(quality.get("ok")) and bool(check.get("ok")) and not blocking,
                errors=(quality_errors + list(check.get("errors") or []))[:30],
                details={"quality_score": quality.get("score"),
                         "quality_ok": quality.get("ok"), "profile_ok": check.get("ok"),
                         "blocking": blocking, "status": status})
        self.db.execute(
            "UPDATE workshop_drafts SET name=?, title=?, fault_name=?, category=?, description=?, profile_yaml=?, "
            "analysis_json=?, check_json=?, status=?, last_error=?, updated_at=? WHERE id=?",
            (final_name, spec["title"], spec["fault_name"],
             "自学习" if str(row.get("category") or "") == "自学习"
             else (spec.get("category") or "硬件故障"),
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
               model_override: int | None = None, trace: Any = None) -> dict[str, Any]:
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
    def try_rules(self, draft_id: str) -> dict[str, Any]:
        """用当前 profile 的规则对已解压日志做一次只读试跑。"""
        row = self.get(draft_id)
        draft_dir = self.config.data_dir / "workshop" / draft_id
        extracted = draft_dir / "extracted"
        if not extracted.exists() or not any(extracted.rglob("*")):
            return {"ok": False, "error": "还没有可试跑的日志，请先在资料输入上传日志包并完成解析"}
        try:
            profile = yaml.safe_load(row.get("profile_yaml") or "") or {}
        except yaml.YAMLError as exc:
            return {"ok": False, "error": f"profile 不是合法 YAML: {exc}"}
        rules = []
        for step in (profile.get("analysis") or {}).get("steps") or []:
            for rule in (step or {}).get("rules") or []:
                item = {k: rule.get(k) for k in (
                    "id", "description", "type", "globs", "all_of", "any_of", "none_of",
                    "min_matches", "min_files", "file_scope", "critical",
                    "context_lines", "case_sensitive", "value_pattern",
                    "threshold", "abs_threshold", "sustained_count", "window_minutes",
                    "trend", "associate_by") if rule.get(k) not in (None, "", [])}
                item.setdefault("type", "text")
                item.setdefault("case_sensitive", False)
                item.setdefault("min_matches", 1)
                item.setdefault("context_lines", 2)
                item["step"] = step.get("id")
                rules.append(item)
        if not rules:
            return {"ok": False, "error": "当前 profile 没有可执行规则"}
        rules_path = draft_dir / "try-rules.yaml"
        write_text(rules_path, yaml.safe_dump({"rules": rules}, allow_unicode=True, sort_keys=False))
        out_json = draft_dir / "try-evidence.json"
        script = self.meta / "scripts" / "search_evidence.py"
        if not script.exists():
            return {"ok": False, "error": "元 Skill 缺少 search_evidence.py"}
        try:
            proc = subprocess.run(
                [sys.executable, str(script), str(extracted), "--rules", str(rules_path),
                 "--json", str(out_json)], cwd=str(self.meta), env=env_with_vendor(),
                capture_output=True, text=True, timeout=180)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        if proc.returncode != 0:
            return {"ok": False, "error": ((proc.stderr or proc.stdout or "未知错误")[-1200:])}
        try:
            data = json.loads(out_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"试跑结果无法解析: {exc}"}
        raw_results = data.get("results") or []
        hits = []
        details = []
        for r in raw_results:
            rid = r.get("rule_id") or r.get("id")
            evidence = r.get("evidence") or []
            detail = {
                "id": rid, "hit": bool(r.get("hit")),
                "match_count": r.get("match_count") or 0,
                "files_eligible": r.get("files_eligible", r.get("files_scanned", 0)),
                "files_hit": r.get("files_hit") or [],
                "files_missed": r.get("files_missed") or [],
                "hits_by_file": r.get("hits_by_file") or {},
                "file_coverage": r.get("file_coverage", 0),
                "file_scope": r.get("file_scope", "any"),
                "min_files": r.get("min_files", 0),
                "skipped_count": r.get("skipped_count", 0),
                "near_misses": r.get("near_misses") or [],
            }
            details.append(detail)
            if not r.get("hit"):
                continue
            hits.append({
                "id": rid, "match_count": r.get("match_count") or 0,
                "files": (r.get("files_hit") or [e.get("file") for e in evidence
                                                  if e.get("file")])[:5],
                "excerpts": [(e.get("text") or e.get("line") or "") for e in evidence][:5],
            })
        missed = [r.get("rule_id") or r.get("id") for r in raw_results if not r.get("hit")]
        missed_files = sorted({f for d in details for f in d["files_missed"]})
        return {"ok": True, "rules": len(rules), "hits": hits, "missed": missed,
                "rule_details": details, "missed_files": missed_files,
                "near_misses": [{"rule_id": d["id"], "lines": d["near_misses"]}
                                for d in details if d["near_misses"]],
                "root_causes": data.get("root_causes") or [],
                "excerpt": (proc.stdout or "")[:5000]}

    def check(self, profile_yaml: str) -> dict[str, Any]:
        draft_dir = self.config.data_dir / "workshop" / "_check"
        draft_dir.mkdir(parents=True, exist_ok=True)
        path = write_text(draft_dir / f"profile-{int(time.time() * 1000)}.yaml", profile_yaml)
        script = self.meta / "scripts" / "scaffold_child_skill.py"
        if not script.exists():
            return {"ok": False, "output": "元 Skill 缺少 scaffold_child_skill.py"}
        try:
            proc = subprocess.run(
                [sys.executable, str(script), "--profile", str(path), "--check", "--json",
                 "--domain-pack", self.domain_pack],
                cwd=str(self.meta), env=env_with_vendor(), capture_output=True,
                text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "output": str(exc)}
        output = (proc.stdout or "") + (proc.stderr or "")
        payload = {}
        try:
            payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            payload = {}
        return {
            "ok": proc.returncode == 0 and bool(payload.get("ok", True)),
            "exit_code": proc.returncode,
            "output": output[:6000],
            "errors": payload.get("errors") or self._extract_errors(output),
            "warnings": payload.get("warnings") or [],
            "domain_pack": payload.get("domain_pack") or {},
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
        check = self.check(new_yaml)

        # 规则确认必须同时更新草稿的“当前状态”，否则页面仍会从旧分析快照
        # 读取 verified=false 和历史质量错误，出现“已确认但仍待确认”的矛盾。
        analysis = dict(row.get("analysis") or {})
        intake = dict(analysis.get("intake") or {})
        for rule in intake.get("evidence_rules") or []:
            rid = str((rule or {}).get("id") or "")
            if rid in changed:
                rule["verified"] = True
        for suggestion in (analysis.get("rule_candidates") or {}).get("rule_suggestions") or []:
            rule = (suggestion or {}).get("rule") or {}
            rid = str(rule.get("id") or "")
            if rid in changed:
                rule["verified"] = True
        quality = dict(analysis.get("quality") or {})
        errors = list(quality.get("errors") or [])
        kept_errors, resolved = [], []
        for item in errors:
            field = str((item or {}).get("field") or "")
            if any(re.search(rf"(^|[^A-Za-z0-9_]){re.escape(rid)}($|[^A-Za-z0-9_])", field)
                   for rid in changed):
                resolved.append({**(item or {}), "resolved_by": "rule_confirmed"})
            else:
                kept_errors.append(item)
        quality["errors"] = kept_errors
        quality["resolved"] = list(quality.get("resolved") or []) + resolved
        quality["ok"] = not kept_errors
        if isinstance(quality.get("counts"), dict):
            counts = dict(quality["counts"])
            counts["errors"] = len(kept_errors)
            quality["counts"] = counts
        analysis["intake"] = intake
        analysis["quality"] = quality
        check["quality"] = quality
        blocking = any(q.get("blocking") for q in quality.get("questions") or [])
        status = "generated" if check.get("ok") and quality.get("ok") and not blocking else "needs_confirmation"
        last_error = "" if check.get("ok") else "；".join(check.get("errors") or ["profile 校验未通过"])
        self.db.execute(
            "UPDATE workshop_drafts SET profile_yaml=?, analysis_json=?, check_json=?, status=?, "
            "last_error=?, updated_at=? WHERE id=?",
            (new_yaml, json.dumps(analysis, ensure_ascii=False, default=str),
             json.dumps(check, ensure_ascii=False), status, last_error, iso_now(), draft_id))
        self._invalidate_build(draft_id)
        return {"ok": True, "changed": changed, "profile_yaml": new_yaml, "check": check,
                "analysis": analysis, "status": status}

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
        parsed_profile = yaml.safe_load(row.get("profile_yaml") or "") or {}
        if not isinstance(parsed_profile, dict):
            parsed_profile = {}
        profile_recipe_id = str((parsed_profile.get("recipe") or {}).get("id") or "")
        stored_recipe = str(row.get("recipe_id") or "")
        expected_recipe = None
        if stored_recipe in ("none", "generic", "off"):
            expected_recipe = ""
        elif stored_recipe not in ("", "auto"):
            expected_recipe = stored_recipe
        if expected_recipe is not None and profile_recipe_id != expected_recipe:
            return {"ok": False, "stage": "analysis",
                    "error": "领域配方已变更，当前 profile 仍是旧配方生成的规则；请先重新分析后再构建"}
        draft_dir = self.config.data_dir / "workshop" / draft_id
        profile_path = draft_dir / "profile.yaml"
        parsed_profile.setdefault("rule_scope_v2",
                                  str(self.config.get("rule_scope_v2", "shadow")))
        write_text(profile_path, yaml.safe_dump(parsed_profile, allow_unicode=True,
                                                sort_keys=False))
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
        regression_run = bool(parsed.get("root_causes")) and cases.exists()
        cmd = [sys.executable, str(self.meta / "scripts" / "build_child_skill.py"),
               "--profile", str(profile_path), "--path", str(staging),
               "--json", str(build_json)]
        domain_id = str((parsed.get("domain_pack") or {}).get("id") or self.domain_pack)
        cmd += ["--domain-pack", domain_id]
        if regression_run:
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
        regression = result.get("regression") if isinstance(result.get("regression"), dict) else {}
        regression_ok = bool(
            regression_run and result.get("verified")
            and int(regression.get("total") or 0) > 0
            and int(regression.get("passed") or 0) == int(regression.get("total") or 0)
            and int(regression.get("failed") or 0) == 0
            and int(regression.get("errors") or 0) == 0)
        attestation = read_json(skill_dir / "assets" / "build-attestation.json", {}) or {}
        attestation_verification = attestation.get("verification") or {}
        attestation_ok = bool(attestation_verification.get("status") == "case_regression"
                              and attestation_verification.get("ok"))
        verified_mode = bool(ok and regression_ok and attestation_ok)
        payload = {
            "ok": ok,
            "verified": verified_mode,
            "version": version,
            "exit_code": proc.returncode,
            "output": output[-6000:],
            "result": result,
            "regression": regression,
            "attestation": attestation,
            "domain": (read_json(skill_dir / "assets" / "skill-manifest.json", {}) or {}).get("domain") or {},
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

    def _publish_coverage(self, row: dict[str, Any]) -> dict[str, Any]:
        """O4：发布前重新计算案例覆盖，不信任 build.verified。"""
        name = row["name"]
        profile = yaml.safe_load(row.get("profile_yaml") or "") or {}
        rules = {}
        for step in (profile.get("analysis") or {}).get("steps") or []:
            for rule in step.get("rules") or []:
                if rule.get("id"):
                    rules[str(rule["id"])] = rule
        cases = []
        case_root = self.config.cases_dir / name
        if case_root.exists():
            for p in sorted(case_root.glob("*/case.yaml")):
                try:
                    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError):
                    continue
                cases.append(data)
        roots = profile.get("root_causes") or []
        positive_by_root = {str(rc.get("id")): [] for rc in roots}
        covered_by_root = {str(rc.get("id")): set() for rc in roots}
        for case in cases:
            expected = case.get("expected") or {}
            rid = str(expected.get("root_cause") or "")
            kind = str(case.get("kind") or "positive")
            if rid in positive_by_root and kind != "negative":
                positive_by_root[rid].append(str(case.get("id") or ""))
                covered_by_root[rid].update(str(x) for x in expected.get("hit_rules") or [])
                covered_by_root[rid].update(str(x) for x in expected.get("not_hit_rules") or [])
        blocking, warnings, rows = [], [], {}
        for rc in roots:
            rid = str(rc.get("id"))
            required = set(re.findall(r"\b[A-Z]\d+\b", rc.get("condition", "") or ""))
            missing = sorted(required - covered_by_root.get(rid, set()))
            if not positive_by_root.get(rid):
                blocking.append(f"根因 {rid} 没有非负例案例")
            if missing:
                blocking.append(f"根因 {rid} 的 condition 规则未被其案例验证: {missing}")
            rows[rid] = {"cases": positive_by_root.get(rid, []),
                         "required_rules": sorted(required), "missing_rules": missing}
        critical = {rid for rid, r in rules.items() if r.get("critical") is True}
        for rid in sorted(critical):
            pos_hit = [c.get("id") for c in cases
                       if str((c.get("expected") or {}).get("root_cause") or "") != "NONE"
                       and rid in {str(x) for x in (c.get("expected") or {}).get("hit_rules") or []}]
            neg = [c.get("id") for c in cases
                   if str((c.get("expected") or {}).get("root_cause") or "NONE") == "NONE"
                   or str(c.get("kind") or "") == "negative"]
            if not pos_hit or not neg:
                blocking.append(f"关键规则 {rid} 需要至少 1 个正例和 1 个反例")
        if not [c for c in cases
                if str((c.get("expected") or {}).get("root_cause") or "NONE") == "NONE"]:
            blocking.append("发布前至少需要 1 个故障正例和 1 个正常/反例，并全部通过回归；"
                            "根因缺少 negative 反证案例")
        required_sources = (profile.get("evidence") or {}).get("required_sources") or []
        source_rows = []
        for src in required_sources:
            if not isinstance(src, dict):
                continue
            sid = str(src.get("id") or "")
            title = str(src.get("title") or sid)
            source_rows.append({"id": sid, "title": title, "cases": len(cases),
                                "required": bool(src.get("required", True))})
            if not cases:
                warnings.append(f"required source {sid or title} 无任何案例输入，无法验证覆盖")
        for rid, rule in sorted(rules.items()):
            if cases and rule.get("globs"):
                continue
            if rule.get("globs"):
                warnings.append(f"规则 {rid} glob 无案例输入，无法验证覆盖")
        return {"ok": not blocking, "blocking": blocking, "warnings": warnings,
                "root_causes": rows, "critical_rules": sorted(critical),
                "required_sources": source_rows, "cases": len(cases)}

    def publish(self, draft_id: str, username: str, override_reason: str = "",
                approvals: list[dict[str, Any]] | None = None,
                channel: str = "stable", emergency_reason: str = "") -> dict[str, Any]:
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
        if approvals or emergency_reason:
            gate = publish_gate(channel=channel, approvals=approvals,
                                emergency_reason=emergency_reason, actor=username)
            if not gate.get("ok"):
                raise ValueError(str(gate.get("message") or "发布审批未通过"))
            if gate.get("emergency"):
                self.db.audit(None, username, "skill.publish.emergency", draft_id,
                              {"reason": emergency_reason, "channel": channel,
                               "follow_up_within_hours": 24})
        coverage = self._publish_coverage(row)
        override = ""
        if not coverage["ok"]:
            reason = str(override_reason or "").strip()
            if len(reason) < 10:
                raise ValueError("发布覆盖不足：" + "；".join(coverage["blocking"])
                                 + "。如确需发布，请填写不少于 10 字的理由")
            override = reason
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
            from .supply import sign_skill
            attestation_path = target / "assets" / "build-attestation.json"
            attestation = read_json(attestation_path, {}) or {}
            sign_skill(
                target, data_dir=self.config.data_dir, name=name, version=version,
                builder="workshop",
                verification={"status": "case_regression", "ok": bool(build.get("verified")),
                              "attestation_sha256": sha256_file(attestation_path)
                              if attestation_path.exists() else "",
                              "engine_digest": str((attestation.get("engine") or {}).get("digest") or ""),
                              "domain": attestation.get("domain") or {},
                              "regression": build.get("regression") or
                              (build.get("result") or {}).get("regression") or {},
                              "draft_id": draft_id, "published_by": username})
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
        self.db.audit(None, username, "skill.publish.override" if override else "skill.publish",
                      name, {"version": version, "verified": bool(build.get("verified")),
                             "coverage": coverage, "override_reason": override})
        return {"ok": True, "skill": name, "version": version,
                "verified": bool(build.get("verified")),
                "coverage": coverage, "override": bool(override)}

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
            row = self.db.one(
                "SELECT build_json FROM skill_versions WHERE skill_name=? AND version=?",
                (skill_name, version)) or {}
            try:
                built = json.loads(row.get("build_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                built = {}
            regression = built.get("regression") if isinstance(built.get("regression"), dict) else {}
            verified = bool(built.get("verified") and int(regression.get("total") or 0) > 0
                            and int(regression.get("passed") or 0) == int(regression.get("total") or 0)
                            and int(regression.get("failed") or 0) == 0)
            from .supply import sign_skill
            sign_skill(target, data_dir=self.config.data_dir, name=skill_name,
                       version=version, builder="workshop-rollback",
                       verification={"status": "case_regression" if verified else "rollback_unverified",
                                     "ok": verified, "regression": regression})
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
            "input": {"archive": "input" if len(files) > 1 else archive_path},
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
        self._invalidate_build(draft_id)
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
                for f in sorted(p for p in input_dir.rglob("*") if p.is_file()):
                    files.append({"name": f.relative_to(input_dir).as_posix(),
                                  "size": f.stat().st_size})
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
