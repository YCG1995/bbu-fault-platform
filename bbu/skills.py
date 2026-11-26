"""子 Skill 注册、健康检查、描述匹配与隔离执行。

兼容元 Skill 生成的两种产物：
- 标准脚本型：scripts/run_analysis.py + assets/fault-profile.yaml；
- 旧版/裁剪版：缺少 run_analysis.py 时标记 incompatible，并在 UI/报告中说明原因，绝不静默假装能跑。
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .db import Database
from .util import env_with_vendor, iso_now, read_json, sha256_file, truncate, utc_now


def load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, yaml.YAMLError):
        return None


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def _version_tuple(value: str) -> tuple[int, ...]:
    """把 1.2.3 转成可比较元组；非数字段按 0。"""
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in parts[:3]) or (0,)


def _satisfies(version: str, spec: str) -> tuple[bool, str]:
    """极简 semver 约束：>=1.2,<3；空约束视为通过。"""
    if not spec:
        return True, ""
    v = _version_tuple(version)
    for item in re.split(r",", str(spec)):
        item = item.strip()
        if not item:
            continue
        m = re.match(r"(>=|<=|>|<|==|=)?\s*([0-9][0-9.\-_]*)", item)
        if not m:
            return False, f"无法解析约束 {item}"
        op, want = m.group(1) or ">=", _version_tuple(m.group(2))
        ok = (v >= want if op == ">=" else
              v <= want if op == "<=" else
              v > want if op == ">" else
              v < want if op == "<" else v == want)
        if not ok:
            return False, f"{version} 不满足 {item}"
    return True, ""


def manifest_compat_errors(manifest: dict[str, Any]) -> list[str]:
    """按 manifest 自带版本校验 contract/engine/domain 约束；返回错误码列表。"""
    if not manifest:
        return []
    req = manifest.get("requires") or {}
    errors: list[str] = []
    engine = manifest.get("engine") or {}
    ok, why = _satisfies(str(engine.get("version") or ""), str(req.get("engine") or ""))
    if not ok:
        errors.append(f"ENGINE_TOO_OLD: {why}")
    domain = manifest.get("domain") or {}
    ok, why = _satisfies(str(domain.get("version") or ""),
                         str(req.get("domain_pack") or req.get("domain") or ""))
    if not ok:
        errors.append(f"DOMAIN_PACK_TOO_OLD: {why}")
    contract = manifest.get("contract") or {}
    max_contract = req.get("contract_max")
    if max_contract and _version_tuple(str(contract.get("profile") or "0")) > _version_tuple(str(max_contract)):
        errors.append(f"CONTRACT_TOO_NEW: profile {contract.get('profile')} > {max_contract}")
    min_contract = req.get("contract")
    if min_contract and _version_tuple(str(contract.get("profile") or "0")) < _version_tuple(str(min_contract)):
        errors.append(f"CONTRACT_TOO_OLD: profile {contract.get('profile')} < {min_contract}")
    return errors


def _entrypoint_path(entry: dict[str, Any], skill_path: Path) -> Path | None:
    argv = entry.get("argv") or []
    for arg in argv:
        if isinstance(arg, str) and arg.endswith(".py") and not arg.startswith("{"):
            return skill_path / arg
    return None


def _render_entrypoint(entry: dict[str, Any], inputs: list[Path], description: str,
                       out_dir: Path) -> list[str]:
    argv = entry.get("argv") or []
    if not argv:
        return []
    out = str(out_dir)
    parts: list[str] = []
    for arg in argv:
        if not isinstance(arg, str):
            continue
        if arg == "{python}":
            parts.append(sys.executable)
        elif arg == "{inputs}":
            parts.extend(str(p) for p in inputs)
        else:
            parts.append(arg.replace("{out}", out).replace("{description}", description))
    return parts


@dataclass
class Skill:
    name: str
    path: Path
    title: str = ""
    version: str = ""
    category: str = ""
    description: str = ""
    status: str = "ready"
    verified: bool = False
    execution_mode: str = "script"
    profile: dict[str, Any] = field(default_factory=dict)
    components: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    alarm_codes: list[str] = field(default_factory=list)
    device_types: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    quick_signatures: list[dict[str, Any]] = field(default_factory=list)
    root_causes: list[dict[str, Any]] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    source: str = "imported"
    channel: str = "stable"
    manifest_status: str = "missing"
    manifest_error: str = ""
    content_sha256: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    domain_pack: dict[str, Any] = field(default_factory=dict)
    entrypoints: dict[str, Any] = field(default_factory=dict)

    def to_db(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "category": self.category,
            "description": self.description,
            "path": str(self.path),
            "status": self.status,
            "verified": 1 if self.verified else 0,
            "execution_mode": self.execution_mode,
            "profile_json": json.dumps(self.profile, ensure_ascii=False),
            "components_json": json.dumps(self.components, ensure_ascii=False),
            "source": self.source,
            "channel": self.channel,
            "health_json": json.dumps(self.health, ensure_ascii=False),
        }

    def catalog_entry(self) -> dict[str, Any]:
        reasoning = self.profile.get("diagnostic_reasoning") or {}
        return {
            "skill": self.name,
            "title": self.title,
            "fault": (self.profile.get("fault") or {}).get("name", ""),
            "aliases": self.aliases[:12],
            "keywords": self.keywords[:16],
            "alarm_codes": self.alarm_codes[:8],
            "device_types": self.device_types,
            "exclusions": self.exclusions[:4],
            "category": self.category,
            "verified": self.verified,
            "status": self.status,
            "root_causes": [str((r or {}).get("name") or "")[:80]
                            for r in self.root_causes[:6] if isinstance(r, dict)],
            "diagnostic_reasoning": {
                "strategy": str(reasoning.get("strategy") or "")[:300],
                "hypotheses": [
                    {"name": str(h.get("name") or "")[:80],
                     "root_cause": h.get("root_cause"),
                     "condition": str(h.get("condition") or "")[:120]}
                    for h in (reasoning.get("hypotheses") or [])[:4]],
                "decision_order": [
                    {"order": d.get("order"),
                     "action": str(d.get("action") or "")[:160],
                     "rule_ids": d.get("rule_ids") or []}
                    for d in (reasoning.get("decision_order") or [])[:5]],
                "discriminators": [
                    {"rule_id": d.get("rule_id"), "use": str(d.get("use") or "")[:180]}
                    for d in (reasoning.get("discriminators") or [])[:4]],
                "pitfalls": [str(x)[:160] for x in (reasoning.get("pitfalls") or [])[:3]],
            } if reasoning else {},
        }


class SkillRegistry:
    def __init__(self, db: Database, skills_dir: Path, data_dir: Path | None = None,
                 manifest_mode: str = "shadow") -> None:
        self.db = db
        self.skills_dir = Path(skills_dir)
        self.data_dir = Path(data_dir or (self.skills_dir.parent / "data"))
        self.manifest_mode = manifest_mode if manifest_mode in ("off", "shadow", "enforce") else "shadow"
        self._cache: dict[str, Skill] = {}
        self._lock = threading.Lock()

    def scan(self, persist: bool = True) -> list[Skill]:
        skills: list[Skill] = []
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        for entry in sorted(self.skills_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                skill = self._load(entry)
            except Exception as exc:  # noqa: BLE001 - 单个坏技能不能影响整表
                skill = Skill(name=entry.name, path=entry, status="incompatible",
                              health={"reasons": [f"加载失败: {exc}"]})
            # 保留管理员停用状态与发布通道
            existing = self.db.one("SELECT status,source,channel FROM skills WHERE name=?", (skill.name,))
            if existing:
                if existing["status"] == "disabled":
                    skill.status = "disabled"
                skill.channel = existing.get("channel") or skill.channel
                if skill.source == "imported":
                    skill.source = existing["source"]
            skills.append(skill)
            if persist:
                self._persist(skill)
        with self._lock:
            self._cache = {s.name: s for s in skills}
        return skills

    def _load(self, path: Path) -> Skill:
        skill_md = path / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.exists() else ""
        fm = parse_frontmatter(text)
        profile_path = path / "assets" / "fault-profile.yaml"
        profile = load_yaml(profile_path) if profile_path.exists() else None
        if profile is None:
            json_path = path / "assets" / "fault-profile.json"
            profile = read_json(json_path, {}) if json_path.exists() else {}
        profile = profile or {}

        # 新契约：assets/skill-manifest 是宿主第一事实源；旧技能无 manifest 时回退 profile。
        skill_manifest = load_yaml(path / "assets" / "skill-manifest.yaml") or {}
        if not skill_manifest:
            skill_manifest = read_json(path / "assets" / "skill-manifest.json", {}) or {}
        routing = skill_manifest.get("routing") or {}

        p_skill = profile.get("skill") or {}
        fault = profile.get("fault") or {}
        symptoms = profile.get("symptoms") or {}
        name = str(skill_manifest.get("name") or p_skill.get("name") or fm.get("name") or path.name)
        title = str(skill_manifest.get("title") or p_skill.get("title") or fm.get("name") or name)
        version = str(skill_manifest.get("version") or p_skill.get("version")
                      or (fm.get("metadata") or {}).get("profile_version") or "")
        category = str(routing.get("category") or profile.get("category") or "")
        description = str(skill_manifest.get("description") or fm.get("description") or "")
        execution_mode = str(p_skill.get("execution_mode") or "script")
        components = profile.get("components") or {}
        aliases = _str_list(routing.get("aliases")) or (
            _str_list(fault.get("aliases")) + _str_list(fault.get("name")))
        keywords = _str_list(routing.get("keywords")) or _str_list(symptoms.get("description_keywords"))
        alarm_codes = _str_list(routing.get("alarm_codes"))
        if not alarm_codes:
            for item in fault.get("alarm_codes") or []:
                if isinstance(item, dict) and item.get("code"):
                    alarm_codes.append(str(item["code"]))
                elif isinstance(item, str):
                    alarm_codes.append(item)
        domain = fault.get("domain") or {}
        device_types = _str_list(routing.get("device_types")) or _str_list(domain.get("device_types"))
        exclusions = _str_list(routing.get("exclusions")) or _str_list(fault.get("exclusions"))
        quick = profile.get("quick_signatures") or []
        qs_path = path / "assets" / "quick-signatures.yaml"
        if qs_path.exists():
            qs = load_yaml(qs_path) or {}
            quick = qs.get("quick_signatures") or qs.get("signatures") or quick
        root_causes = profile.get("root_causes") or []
        rules = profile.get("rules") or []
        rules_path = path / "assets" / "evidence-rules.yaml"
        if rules_path.exists():
            er = load_yaml(rules_path) or {}
            rules = er.get("rules") or rules
            if not root_causes:
                root_causes = er.get("root_causes") or root_causes
        if not rules:
            for step in (profile.get("analysis") or {}).get("steps") or []:
                rules.extend(step.get("rules") or [])

        # 健康检查
        reasons: list[str] = []
        if not skill_md.exists():
            reasons.append("缺少 SKILL.md")
        if not profile:
            reasons.append("缺少 assets/fault-profile.yaml")
        run_analysis = path / "scripts" / "run_analysis.py"
        run_entry = (skill_manifest.get("entrypoints") or {}).get("run") or {}
        run_target = _entrypoint_path(run_entry, path)
        has_run = run_analysis.exists() or bool(run_target and run_target.exists())
        if skill_manifest and not run_entry:
            reasons.append("manifest 缺少 entrypoints.run")
        elif execution_mode == "agent":
            reasons.append("execution_mode=agent：当前平台运行器只支持脚本型子 Skill")
        elif not has_run:
            reasons.append("缺少 scripts/run_analysis.py（旧版或组件裁剪版），请用技能工坊重建")
        required = [path / "scripts" / "search_evidence.py", path / "scripts" / "render_report.py"]
        for item in required:
            if not item.exists():
                reasons.append(f"缺少 {item.relative_to(path)}")
        if not rules:
            reasons.append("没有任何证据规则（evidence-rules 为空）")
        if not root_causes:
            reasons.append("没有根因定义（root_causes 为空）")
        compat_errors = manifest_compat_errors(skill_manifest)
        if compat_errors:
            reasons.extend(compat_errors)
        if reasons:
            status = "incompatible" if any(
                "缺少 scripts/run_analysis.py" in r or "entrypoints.run" in r
                or "execution_mode=agent" in r or "缺少 SKILL.md" in r
                or "fault-profile" in r for r in reasons
                or any(r.startswith(("ENGINE_", "DOMAIN_PACK_", "CONTRACT_")) for r in reasons)) else "degraded"
        else:
            status = "ready"

        # 只有“非 triage + 有根因 + 至少有案例”且签名清单记录了真实案例回归，
        # 才视为已验证；--skip-regression 草稿、导入隔离包和仅签名都不算。
        semantic_verified = (str(profile.get("mode") or "") != "triage"
                             and bool(root_causes)
                             and bool(profile.get("cases")))
        source = "workshop" if (path / ".bbu-workshop.json").exists() else "imported"
        from .supply import verify_skill
        manifest = verify_skill(path, data_dir=self.data_dir)
        manifest_status = str(manifest.get("status") or "missing")
        manifest_error = str(manifest.get("reason") or "")
        verification = manifest.get("verification") or {}
        attestation = read_json(path / "assets" / "build-attestation.json", {}) or {}
        attestation_verification = attestation.get("verification") or {}
        attestation_ok = bool(attestation_verification.get("status") == "case_regression"
                              and attestation_verification.get("ok"))
        verified = bool(
            semantic_verified and manifest_status == "verified"
            and verification.get("status") == "case_regression"
            and verification.get("ok")
            and (not skill_manifest or attestation_ok))
        if manifest_status == "verified":
            manifest_error = ""
        if self.manifest_mode == "enforce" and manifest_status != "verified":
            status = "incompatible"
            reasons.append(f"内容清单/签名校验未通过：{manifest_error or manifest_status}")
        elif manifest_status in ("tampered", "bad_signature", "invalid"):
            status = "incompatible"
            reasons.append(f"内容清单/签名异常，禁止执行：{manifest_error or manifest_status}")
        return Skill(
            name=name, path=path, title=title, version=version, category=category,
            description=description, status=status, verified=verified,
            execution_mode=execution_mode, profile=profile, components=components,
            aliases=[a for a in aliases if a], keywords=keywords, alarm_codes=alarm_codes,
            device_types=device_types, exclusions=exclusions,
            quick_signatures=quick if isinstance(quick, list) else [],
            root_causes=root_causes if isinstance(root_causes, list) else [],
            rules=rules if isinstance(rules, list) else [],
            health={"reasons": reasons, "has_run_analysis": has_run,
                    "skill_manifest": bool(skill_manifest),
                    "attestation_status": str(attestation_verification.get("status") or ""),
                    "attestation_ok": attestation_ok,
                    "domain_pack": dict(skill_manifest.get("domain") or {}),
                    "rules": len(rules), "root_causes": len(root_causes),
                    "manifest_status": manifest_status, "manifest_error": manifest_error,
                    "verification_status": str(verification.get("status") or ""),
                    "semantic_verified": semantic_verified},
            source=source, manifest_status=manifest_status,
            manifest_error=manifest_error,
            content_sha256=str(manifest.get("content_sha256") or ""),
            manifest=skill_manifest,
            domain_pack=dict(skill_manifest.get("domain") or {}),
            entrypoints=dict(skill_manifest.get("entrypoints") or {}),
        )

    def _persist(self, skill: Skill) -> None:
        row = skill.to_db()
        now = iso_now()
        self.db.execute(
            "INSERT INTO skills(name,title,version,category,description,path,status,verified,"
            "execution_mode,profile_json,components_json,source,channel,health_json,imported_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET title=excluded.title,version=excluded.version,"
            "category=excluded.category,description=excluded.description,path=excluded.path,"
            "status=excluded.status,verified=excluded.verified,execution_mode=excluded.execution_mode,"
            "profile_json=excluded.profile_json,components_json=excluded.components_json,"
            "source=CASE WHEN skills.status='disabled' THEN skills.source ELSE excluded.source END,"
            "channel=skills.channel,health_json=excluded.health_json,updated_at=excluded.updated_at",
            (row["name"], row["title"], row["version"], row["category"], row["description"],
             row["path"], row["status"], row["verified"], row["execution_mode"],
             row["profile_json"], row["components_json"], row["source"], row["channel"],
             row["health_json"], now, now),
        )
        # 保留停用状态
        if skill.status == "disabled":
            self.db.execute("UPDATE skills SET status='disabled' WHERE name=?", (skill.name,))

    def get(self, name: str) -> Skill | None:
        with self._lock:
            if name in self._cache:
                return self._cache[name]
        row = self.db.one("SELECT * FROM skills WHERE name=?", (name,))
        if not row:
            return None
        path = Path(row["path"])
        if not path.exists():
            return None
        skill = self._load(path)
        skill.status = row["status"] if row["status"] == "disabled" else skill.status
        skill.source = row["source"]
        skill.channel = row.get("channel") or "stable"
        with self._lock:
            self._cache[name] = skill
        return skill

    def set_channel(self, name: str, channel: str) -> None:
        if channel not in ("stable", "canary"):
            raise ValueError("channel 必须为 stable/canary")
        self.db.execute("UPDATE skills SET channel=?, updated_at=? WHERE name=?",
                        (channel, iso_now(), name))
        with self._lock:
            self._cache.pop(name, None)

    def list_skills(self, include_disabled: bool = True) -> list[Skill]:
        with self._lock:
            if self._cache:
                skills = list(self._cache.values())
            else:
                skills = []
        if not skills:
            skills = self.scan()
        if not include_disabled:
            skills = [s for s in skills if s.status != "disabled"]
        return sorted(skills, key=lambda s: (s.status != "ready", s.name))

    def set_status(self, name: str, status: str) -> None:
        self.db.execute("UPDATE skills SET status=?, updated_at=? WHERE name=?",
                        (status, iso_now(), name))
        with self._lock:
            self._cache.pop(name, None)

    def manifest(self, name: str) -> dict[str, Any]:
        row = self.db.one("SELECT path FROM skills WHERE name=?", (name,))
        if not row:
            raise ValueError("技能不存在")
        from .supply import verify_skill
        return verify_skill(Path(row["path"]), data_dir=self.data_dir)

    def sign_manifest(self, name: str) -> dict[str, Any]:
        row = self.db.one("SELECT path,version FROM skills WHERE name=?", (name,))
        if not row:
            raise ValueError("技能不存在")
        path = Path(row["path"])
        from .util import within_directory
        if not within_directory(self.skills_dir, path):
            raise ValueError("技能目录越界")
        from .supply import sign_skill
        result = sign_skill(path, data_dir=self.data_dir, name=name,
                            version=str(row.get("version") or ""), builder="admin-audit")
        with self._lock:
            self._cache.pop(name, None)
        self.scan()
        return result


def _str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    return []


@dataclass
class MatchResult:
    skill: Skill
    score: float
    reasons: list[str]
    signals: dict[str, Any] = field(default_factory=dict)


class Matcher:
    """确定性匹配：别名 > 关键词 > 告警码 > 设备类型 > 文件名/快速签名。"""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def match(self, description: str, file_names: list[str],
              archive_texts: dict[str, str] | None = None) -> list[MatchResult]:
        text = (description or "").lower()
        results: list[MatchResult] = []
        for skill in self.registry.list_skills(include_disabled=False):
            if skill.status not in ("ready", "degraded"):
                continue
            score = 0.0
            reasons: list[str] = []
            signals: dict[str, Any] = {"aliases": [], "keywords": [], "alarm_codes": [],
                                       "files": [], "signatures": [], "exclusions": []}
            for alias in skill.aliases:
                if alias and alias.lower() in text:
                    score += 12
                    reasons.append(f"命中故障别名“{alias}”")
                    signals["aliases"].append(alias)
                    break
            keyword_hits = 0
            for kw in skill.keywords:
                if kw and kw.lower() in text:
                    keyword_hits += 1
                    signals["keywords"].append(kw)
            if keyword_hits:
                score += min(30, keyword_hits * 5)
                reasons.append(f"命中描述关键词 {keyword_hits} 个：" + "、".join(signals["keywords"][:6]))
            for code in skill.alarm_codes:
                if code and _code_in_text(code, text):
                    score += 10
                    signals["alarm_codes"].append(code)
                    reasons.append(f"命中告警码 {code}")
            for dev in skill.device_types:
                if dev and dev.lower() in text:
                    score += 4
                    reasons.append(f"设备形态匹配 {dev}")
            names_lower = [n.lower() for n in file_names]
            for qs in skill.quick_signatures:
                hints = _str_list(qs.get("file_hints"))
                hit_hint = next((h for h in hints if any(h.lower().strip("*") in n for n in names_lower)), None)
                if hit_hint:
                    score += 4
                    signals["files"].append(hit_hint)
                    reasons.append(f"日志文件名命中 {hit_hint}")
                any_of = _str_list(qs.get("any_of"))
                if any_of and archive_texts:
                    blob = "\n".join(archive_texts.values()).lower()
                    for token in any_of:
                        if token.lower() in blob:
                            score += 6
                            signals["signatures"].append(token)
                            reasons.append(f"日志内容命中快速签名“{token}”")
                            break
            for excl in skill.exclusions:
                if excl and excl.lower() in text:
                    score -= 8
                    signals["exclusions"].append(excl)
                    reasons.append(f"命中排除条件“{excl}”")
            if score >= 5:
                results.append(MatchResult(skill=skill, score=min(100.0, score),
                                           reasons=reasons, signals=signals))
        return sorted(results, key=lambda r: r.score, reverse=True)


def _code_in_text(code: str, text: str) -> bool:
    if not code:
        return False
    return bool(re.search(rf"(?<![0-9A-Za-z]){re.escape(code.lower())}(?![0-9A-Za-z])", text))


def shallow_archive_text(path: Path, patterns: list[str], max_files: int = 24,
                         max_bytes: int = 4 * 1024 * 1024) -> dict[str, str]:
    """不解压、直接从 zip/tar 读取小文本成员，用于路由快速签名。"""
    out: dict[str, str] = {}
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist()[:max_files * 4]:
                    if len(out) >= max_files or info.file_size > 512 * 1024:
                        continue
                    if not info.filename.lower().endswith((".log", ".txt", ".csv", ".ini", ".cfg")):
                        continue
                    try:
                        raw = zf.read(info)[:max_bytes]
                        out[info.filename] = raw.decode("utf-8", errors="ignore")
                    except (KeyError, RuntimeError, OSError):
                        continue
            return out
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                for member in tf.getmembers()[:max_files * 4]:
                    if len(out) >= max_files or not member.isfile() or member.size > 512 * 1024:
                        continue
                    if not member.name.lower().endswith((".log", ".txt", ".csv", ".ini", ".cfg")):
                        continue
                    fh = tf.extractfile(member)
                    if fh:
                        out[member.name] = fh.read(max_bytes).decode("utf-8", errors="ignore")
    except (tarfile.TarError, zipfile.BadZipFile, OSError):
        return out
    return out


def inventory_inputs(paths: list[Path]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    total_members = 0
    total_bytes = 0
    names: list[str] = []
    for path in paths:
        item = {"name": path.name, "size": path.stat().st_size, "kind": "file"}
        try:
            if zipfile.is_zipfile(path):
                item["kind"] = "zip"
                with zipfile.ZipFile(path) as zf:
                    members = zf.infolist()
                    item["members"] = len(members)
                    total_members += len(members)
                    total_bytes += sum(m.file_size for m in members)
                    names.extend(m.filename for m in members[:200])
            elif tarfile.is_tarfile(path):
                item["kind"] = "tar"
                with tarfile.open(path) as tf:
                    members = tf.getmembers()
                    item["members"] = len(members)
                    total_members += len(members)
                    total_bytes += sum(m.size for m in members)
                    names.extend(m.name for m in members[:200])
            else:
                item["kind"] = "plain"
                names.append(path.name)
        except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            item["error"] = str(exc)[:200]
        files.append(item)
    top_names = [n for n in names if n and not n.endswith("/")][:80]
    hints = []
    for token in ("collectinfo", "clockstatuslog", "gnss", "ocxo", "alarm", "fm-active",
                  "bspmoni", "oamlog", "cloudmat", "summary.json"):
        if any(token in n.lower() for n in names):
            hints.append(token)
    return {
        "files": files,
        "total_input_bytes": sum(f["size"] for f in files),
        "total_members": total_members,
        "total_uncompressed_bytes": total_bytes,
        "top_members": top_names,
        "detected_hints": hints,
        "sample_archive_text": bool(names),
    }


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class SkillRunner:
    def __init__(self, config: Any, db: Any | None = None, policy: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.policy = policy

    def _session_revision(self, task_id: str) -> int:
        if not self.db or not task_id:
            return 0
        row = self.db.one("SELECT COALESCE(MAX(revision),0) AS n FROM session_events WHERE task_id=?",
                          (task_id,))
        return int((row or {}).get("n") or 0)

    def run(self, skill: Skill, inputs: list[Path], description: str,
            out_dir: Path, cancel_event: threading.Event | None = None,
            timeout: int | None = None, task_id: str = "",
            actor_role: str = "engineer", approval_id: str = "") -> dict[str, Any]:
        started = time.time()
        out_dir.mkdir(parents=True, exist_ok=True)
        run_entry = (skill.entrypoints or {}).get("run") or {}
        entry = _entrypoint_path(run_entry, skill.path) or (skill.path / "scripts" / "run_analysis.py")
        if not entry.exists():
            return {"ok": False, "status": "incompatible",
                    "error": "缺少运行入口（manifest entrypoints.run 或 scripts/run_analysis.py），"
                             "请用技能工坊重建该子 Skill",
                    "duration_ms": 0}
        if skill.status == "disabled":
            return {"ok": False, "status": "disabled", "error": "技能已被停用", "duration_ms": 0}

        call_id = ""
        manifest = None
        revision = self._session_revision(task_id)
        if self.policy and task_id and hasattr(self.policy, "check"):
            task = (self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)) if self.db else {}) or {}
            decision = self.policy.check(task=task, skill=skill, inputs=inputs,
                                         description=description,
                                         session_revision=revision,
                                         approval_id=approval_id, actor_role=actor_role)
            manifest = self.policy.manifest(skill)
            call_id = self.policy.record_start(task_id=task_id, manifest=manifest,
                                               decision=decision, approval_id=approval_id,
                                               session_revision=revision)
            if not decision.get("allow"):
                return {"ok": False, "status": "denied",
                        "error": "工具授权拒绝：" + "; ".join(decision.get("reasons") or []),
                        "duration_ms": int((time.time() - started) * 1000),
                        "tool_call_id": call_id, "tool_decision": decision,
                        "policy": decision}
        else:
            decision = None

        cmd = _render_entrypoint(run_entry, inputs, description, out_dir)
        if not cmd:
            cmd = [
                sys.executable, str(entry), *[str(p) for p in inputs],
                "--description", description,
                "--out", str(out_dir),
                "--local-tz-offset", str(self.config.get("local_tz_offset", 8)),
            ]
        env = env_with_vendor()
        preexec = None
        sandbox_meta: dict[str, Any] = {"mode": str(self.config.get("tool_sandbox_mode", "audit"))}
        if manifest is not None:
            from .sandbox import build_command, output_limit_bytes
            cmd, env, preexec = build_command(entry, cmd[2:], manifest, out_dir, inputs, self.config)
            sandbox_meta.update({"mode": str(self.config.get("tool_sandbox_mode", "audit")),
                                 "network": manifest.network,
                                 "allow_subprocess": bool(manifest.process.get("allow_subprocess")),
                                 "platform": "windows" if os.name == "nt" else "posix"})
        stdout_path = out_dir / "runner.out"
        stderr_path = out_dir / "runner.err"
        timeout = timeout or (int((run_entry.get("timeout_sec") or 0))
                              if run_entry else (manifest.timeout_sec if manifest else
                                                 int(self.config.get("skill_timeout_sec", 900))))

        def _finish(res: dict[str, Any], status: str, error: str = "") -> dict[str, Any]:
            res.setdefault("artifacts", {"stdout": str(stdout_path), "stderr": str(stderr_path)})
            if self.policy and call_id:
                resource = {"duration_ms": res.get("duration_ms"),
                            "output_bytes": _file_size(stdout_path) + _file_size(stderr_path),
                            "sandbox": sandbox_meta, "session_revision": revision}
                self.policy.record_finish(call_id, status=status, result=res,
                                          error=error or res.get("error") or "",
                                          resource=resource)
                res["tool_call_id"] = call_id
            return res

        with open(stdout_path, "wb") as out_fh, open(stderr_path, "wb") as err_fh:
            popen_kwargs: dict[str, Any] = {
                "cwd": str(skill.path), "stdout": out_fh, "stderr": err_fh, "env": env,
            }
            if os.name != "nt":
                # start_new_session 仅 POSIX 支持；Windows 用 taskkill /T 终止进程树。
                popen_kwargs["start_new_session"] = True
                if preexec is not None:
                    popen_kwargs["preexec_fn"] = preexec
            proc = subprocess.Popen(cmd, **popen_kwargs)
            deadline = time.time() + timeout
            cancelled = False
            timed_out = False
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    _kill_group(proc)
                    break
                if time.time() > deadline:
                    timed_out = True
                    _kill_group(proc)
                    break
                time.sleep(0.15)
        duration_ms = int((time.time() - started) * 1000)
        summary = read_json(out_dir / "run-summary.json", {}) or {}
        run_result = read_json(out_dir / "run-result.json", {}) or {}
        if cancelled:
            return _finish({"ok": False, "status": "cancelled", "error": "用户取消",
                            "duration_ms": duration_ms, "summary": summary, "exit_code": None,
                            "stdout_tail": _tail(stdout_path), "stderr_tail": _tail(stderr_path)}, "cancelled")
        if timed_out:
            return _finish({"ok": False, "status": "timeout",
                            "error": f"子 Skill 执行超过 {timeout}s（日志见 {stderr_path.name}）",
                            "duration_ms": duration_ms, "summary": summary, "exit_code": None,
                            "stdout_tail": _tail(stdout_path), "stderr_tail": _tail(stderr_path)}, "timeout")
        if manifest is not None:
            from .sandbox import output_limit_bytes
            if _file_size(stdout_path) + _file_size(stderr_path) > output_limit_bytes(self.config):
                return _finish({"ok": False, "status": "output_limited",
                                "error": "子 Skill 输出超过沙箱上限",
                                "duration_ms": duration_ms, "summary": summary,
                                "run_result": run_result,
                                "exit_code": proc.returncode,
                                "stdout_tail": _tail(stdout_path), "stderr_tail": _tail(stderr_path)}, "failed")
        if run_result and not run_result.get("ok"):
            return _finish({"ok": False, "status": str(run_result.get("status") or "failed"),
                            "error": str(run_result.get("error") or "run-result ok=false"),
                            "duration_ms": duration_ms, "summary": summary,
                            "result": run_result,
                            "exit_code": proc.returncode,
                            "stdout_tail": _tail(stdout_path),
                            "stderr_tail": _tail(stderr_path)}, "failed")
        if not run_result and (proc.returncode != 0 or not summary.get("ok")):
            return _finish({"ok": False, "status": "failed",
                            "error": (summary.get("error") or f"退出码 {proc.returncode}"),
                            "duration_ms": duration_ms, "summary": summary,
                            "exit_code": proc.returncode,
                            "stdout_tail": _tail(stdout_path), "stderr_tail": _tail(stderr_path)}, "failed")
        evidence = read_json(out_dir / "evidence.json", {}) or {}
        report_md = out_dir / "report.md"
        report_html = out_dir / "report.html"
        normalized = self._normalize(skill, summary, evidence, report_md, report_html,
                                     duration_ms, run_result=run_result)
        normalized["sandbox"] = sandbox_meta
        normalized["exit_code"] = proc.returncode
        normalized["stdout_tail"] = _tail(stdout_path)
        normalized["stderr_tail"] = _tail(stderr_path)
        if decision:
            normalized["tool_decision"] = decision.get("decision")
        return _finish(normalized, "done")

    def _normalize(self, skill: Skill, summary: dict[str, Any], evidence: dict[str, Any],
                   report_md: Path, report_html: Path, duration_ms: int,
                   run_result: dict[str, Any] | None = None) -> dict[str, Any]:
        result = run_result or {}
        verdict = result.get("verdict") or evidence.get("verdict") or {}
        groups = verdict.get("groups") or {}
        primary = result.get("primary") or verdict.get("primary") or summary.get("primary") or "NONE"
        status = result.get("status") or verdict.get("status") or summary.get("status") or "unmatched"
        rc_map = {str(rc.get("id")): rc for rc in skill.root_causes}
        root_cause = None
        if primary and primary != "NONE":
            raw = rc_map.get(primary) or {}
            root_cause = {
                "id": primary,
                "name": raw.get("name", primary),
                "confidence": raw.get("confidence", "medium"),
                "mechanism": raw.get("mechanism", ""),
                "actions": raw.get("actions") or [],
                "prerequisites": raw.get("prerequisites") or [],
                "risks": raw.get("risks", ""),
                "rollback": raw.get("rollback", ""),
                "verify": raw.get("verify") or [],
                "escalation": raw.get("escalation", ""),
                "condition": raw.get("condition", ""),
            }
        # 证据归一：保留文件、行号、时间、片段，限制总量防报告爆炸
        ev_items: list[dict[str, Any]] = []
        rule_results = evidence.get("rules") or evidence.get("results") or []
        match_counts: dict[str, int] = {}
        glob_fallback: list[str] = []
        for rule in rule_results:
            rid = str(rule.get("rule_id", "?"))
            match_counts[rid] = int(rule.get("match_count") or 0)
            for item in (rule.get("evidence") or [])[:8]:
                ev_items.append({
                    "rule_id": rid,
                    "file": str(item.get("file", "")).replace("\\", "/"),
                    "line_no": item.get("line_no"),
                    "timestamp": item.get("timestamp"),
                    "text": truncate(item.get("line", ""), 500),
                    "site": item.get("site") or "",
                    "associate": item.get("associate") or "",
                })
            for fb in (rule.get("glob_fallback_files") or []):
                glob_fallback.append(str(fb))
        ev_items = ev_items[:80]
        sites = evidence.get("detected_sites") or summary.get("sites") or []
        tokens = summary.get("tokens") or {}
        # 规则元数据用于计算“该证据是否支撑当前根因”，避免无关命中抬高分。
        rule_meta: dict[str, dict[str, Any]] = {}
        root_conditions = {str(rc.get("id")): str(rc.get("condition") or "")
                           for rc in skill.root_causes if isinstance(rc, dict)}
        for rule in skill.rules:
            if not isinstance(rule, dict):
                continue
            rid = str(rule.get("id") or "")
            if not rid:
                continue
            roots = [root_id for root_id, condition in root_conditions.items()
                     if re.search(rf"(?<![A-Za-z0-9_]){re.escape(rid)}(?![A-Za-z0-9_])", condition)]
            rule_meta[rid] = {
                "description": str(rule.get("description") or ""),
                "weight": float(rule.get("weight") or 10),
                "globs": [str(x) for x in rule.get("globs") or []][:4],
                "verified": bool(rule.get("verified", True)),
                "roots": roots,
            }
        return {
            "ok": True,
            "complete": bool(result.get("complete", True)),
            "status": status,
            "primary": primary,
            "coverage": result.get("coverage") or {},
            "degradations": result.get("degradations") or [],
            "result_schema": result.get("schema") or "",
            "domain": skill.domain_pack or {},
            "root_cause": root_cause,
            "verdict": {
                "status": status,
                "primary": primary,
                "groups": _compact_groups(groups),
                "truncated": verdict.get("truncated"),
            },
            "evidence": ev_items,
            "rule_meta": rule_meta,
            "match_counts": match_counts,
            "glob_fallback": glob_fallback[:40],
            "sites": sites,
            "diagnostic_reasoning": skill.profile.get("diagnostic_reasoning") or {},
            "reports": {
                "md": str(report_md) if report_md.exists() else "",
                "html": str(report_html) if report_html.exists() else "",
            },
            "summary": {k: summary.get(k) for k in (
                "ok", "cached", "mode", "status", "primary", "sites", "warnings",
                "unsupported_archives", "glob_fallback")},
            "duration_ms": duration_ms,
            "tokens": tokens,
            "limitations": _skill_limitations(skill, summary, evidence),
        }


def _compact_groups(groups: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, group in (groups or {}).items():
        out[key] = {
            "status": group.get("status"),
            "primary": group.get("primary"),
            "hit_rules": group.get("hit_rules") or [],
            "root_causes": [
                {k: rc.get(k) for k in ("id", "name", "value", "established", "confidence",
                                        "temporal_required", "temporal_ok", "note")}
                for rc in (group.get("root_causes") or [])
            ],
            "temporal_conflict": group.get("temporal_conflict") or [],
        }
    return out


def _skill_limitations(skill: Skill, summary: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    items: list[str] = []
    if summary.get("cached"):
        items.append("命中子 Skill 增量缓存，未重新解压/检索")
    if summary.get("glob_fallback"):
        items.append("存在 glob 兜底命中，需人工确认同名文件是否属于目标网元")
    if summary.get("unsupported_archives"):
        items.append(f"有 {summary['unsupported_archives']} 个压缩包格式不受支持")
    if evidence.get("truncated"):
        items.append("部分规则证据达到截断上限，站点计数可能偏低")
    if str(skill.profile.get("mode") or "") == "triage":
        items.append("该子 Skill 为候选规则（triage），结论未经真实案例验证")
    return items


def _kill_group(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        # Windows 没有 killpg/getpgid；taskkill /T 能连同子脚本一起结束。
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-limit:]
    except OSError:
        return ""
