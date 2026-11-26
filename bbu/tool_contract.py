"""工具合同、调用授权与审批。

把每个子 Skill 包装成可审计工具：版本、输入合同、风险、作用域、审批、
超时、数据/网络/文件系统声明。策略按 off → shadow → enforce 推进：
- off：只保留旧 runner 行为；
- shadow：计算拒绝原因并记账；涉及审批、权限、路径、Schema、网络等安全门禁时
  仍必须拒绝，普通策略原因只记录 would_deny；
- enforce：命中任意拒绝原因时不再启动子进程。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema_validate import validate_schema
from .util import iso_now, new_id, read_json, sha256_file, write_json

RISK_LEVELS = {"low", "medium", "high", "destructive"}
_RISK_ALIASES = {
    "read_only": "low", "readonly": "low", "read-only": "low", "collect": "low",
    "diagnose": "medium", "medium": "medium",
    "change": "high", "modify": "high", "high": "high",
    "reset": "destructive", "restore": "destructive", "upgrade": "destructive",
    "write": "high", "delete": "destructive", "destructive": "destructive",
}
# shadow 模式仍要阻断这些安全门禁；否则“只观察”的默认值会实际放行高风险动作。
_SHADOW_CRITICAL = {
    "tool_unavailable", "approval_contract", "approval_required", "approval_mismatch",
    "role_denied", "site_denied", "device_denied", "path_denied", "schema_invalid",
    "input_count", "input_empty", "network_unenforced",
}
MAX_INPUT_FILES = 64
DEFAULT_INPUT_SCHEMA = {
    "type": "object",
    "required": ["task_id", "inputs"],
    "properties": {
        "task_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "inputs": {
            "type": "array", "minItems": 1, "maxItems": MAX_INPUT_FILES,
            "items": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
        "description": {"type": "string", "maxLength": 6000},
        "session_revision": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}
DEFAULT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["ok", "status"],
    "properties": {
        "ok": {"type": "boolean"},
        "status": {"type": "string"},
        "error": {"type": "string"},
        "duration_ms": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": True,
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tree_hash(path: Path, limit: int = 5000) -> str:
    rows: list[str] = []
    count = 0
    if not path.is_dir():
        return ""
    for item in sorted(path.rglob("*")):
        if not item.is_file() or item.name.endswith((".pyc", ".pyo")):
            continue
        try:
            rows.append(f"{item.relative_to(path).as_posix()}:{sha256_file(item)}")
        except OSError:
            rows.append(f"{item.relative_to(path).as_posix()}:missing")
        count += 1
        if count >= limit:
            break
    return _sha_text("\n".join(rows))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "on", "required", "manual"):
        return True
    if text in ("0", "false", "no", "off", "none", ""):
        return default
    return default


@dataclass
class ToolManifest:
    tool_name: str
    version: str
    title: str
    source: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk: str
    approval: str
    scopes: list[str]
    timeout_sec: int
    data_policy: str
    network: str
    filesystem: dict[str, Any]
    process: dict[str, Any]
    min_role: str
    allowed_sites: list[str] = field(default_factory=list)
    allowed_device_types: list[str] = field(default_factory=list)
    content_sha256: str = ""
    contract_sha256: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name, "version": self.version, "title": self.title,
            "source": self.source, "risk": self.risk, "approval": self.approval,
            "scopes": self.scopes, "timeout_sec": self.timeout_sec,
            "data_policy": self.data_policy, "network": self.network,
            "filesystem": self.filesystem, "process": self.process,
            "min_role": self.min_role, "allowed_sites": self.allowed_sites,
            "allowed_device_types": self.allowed_device_types,
            "content_sha256": self.content_sha256,
            "contract_sha256": self.contract_sha256,
            "input_schema": self.input_schema, "output_schema": self.output_schema,
        }


def manifest_for_skill(skill: Any, config: Any) -> ToolManifest:
    profile = getattr(skill, "profile", {}) or {}
    raw = profile.get("tool") if isinstance(profile.get("tool"), dict) else {}
    if not raw and isinstance(profile.get("tool_contract"), dict):
        raw = profile["tool_contract"]
    risk = str(raw.get("risk") or profile.get("risk") or "low").strip().lower()
    risk = _RISK_ALIASES.get(risk, risk)
    if risk not in RISK_LEVELS:
        risk = "low"
    approval_raw = raw.get("approval", profile.get("approval", "none"))
    approval = "required" if _as_bool(approval_raw) else "none"
    if risk in ("high", "destructive") and str(approval_raw).strip().lower() in ("none", "", "false"):
        approval = "required"
    fs = raw.get("filesystem") if isinstance(raw.get("filesystem"), dict) else {}
    proc = raw.get("process") if isinstance(raw.get("process"), dict) else {}
    manifest = ToolManifest(
        tool_name=str(getattr(skill, "name", "")),
        version=str(getattr(skill, "version", "") or ""),
        title=str(getattr(skill, "title", "") or ""),
        source=str(getattr(skill, "source", "") or "imported"),
        input_schema=raw.get("input_schema") if isinstance(raw.get("input_schema"), dict)
        else DEFAULT_INPUT_SCHEMA,
        output_schema=raw.get("output_schema") if isinstance(raw.get("output_schema"), dict)
        else DEFAULT_OUTPUT_SCHEMA,
        risk=risk,
        approval=approval,
        scopes=[str(x) for x in (raw.get("scopes") or ["task:read", "task:output"])
                if str(x).strip()][:20],
        timeout_sec=max(1, int(raw.get("timeout_sec")
                              or getattr(config, "get", lambda *_: 900)("skill_timeout_sec", 900))),
        data_policy=str(raw.get("data_policy") or "local_only"),
        network=str(raw.get("network") or "none").lower(),
        filesystem={
            "read": [str(x) for x in (fs.get("read") or ["task_inputs", "skill_dir"])][:20],
            "write": [str(x) for x in (fs.get("write") or ["task_output"])][:20],
        },
        process={
            "allow_subprocess": _as_bool(proc.get("allow_subprocess"), True),
            "executables": [str(x) for x in (proc.get("executables") or [])][:30],
            "max_children": max(0, int(proc.get("max_children") or 32)),
        },
        min_role=str(raw.get("min_role") or "engineer"),
        allowed_sites=[str(x) for x in (raw.get("allowed_sites") or [])][:30],
        allowed_device_types=[str(x) for x in (raw.get("allowed_device_types") or [])][:30],
        content_sha256=_tree_hash(Path(getattr(skill, "path", ""))),
    )
    manifest.contract_sha256 = _sha_text(canonical_json(manifest.public()))
    return manifest


class ToolPolicy:
    def __init__(self, db: Any, config: Any) -> None:
        self.db = db
        self.config = config
        self.mode = str(config.get("tool_policy_mode", "shadow")).lower()
        if self.mode not in ("off", "shadow", "enforce"):
            self.mode = "shadow"
        self._manifests: dict[str, tuple[str, ToolManifest]] = {}

    def manifest(self, skill: Any) -> ToolManifest:
        cache_key = str(getattr(skill, "name", ""))
        content = _tree_hash(Path(getattr(skill, "path", "")))
        cached = self._manifests.get(cache_key)
        if cached and cached[0] == content:
            return cached[1]
        manifest = manifest_for_skill(skill, self.config)
        self._manifests[cache_key] = (content, manifest)
        return manifest

    def input_summary(self, inputs: list[Path]) -> list[dict[str, Any]]:
        rows = []
        for path in inputs:
            try:
                if path.is_file():
                    rows.append({"name": path.name, "size": path.stat().st_size,
                                 "sha256": sha256_file(path)})
                elif path.is_dir():
                    rows.append({"name": path.name, "size": 0, "sha256": "dir"})
                else:
                    rows.append({"name": path.name, "size": 0, "sha256": "missing"})
            except OSError:
                rows.append({"name": path.name, "size": 0, "sha256": "error"})
        return rows

    def current_revision(self, task_id: str) -> int:
        if not getattr(self, "db", None) or not task_id:
            return 0
        row = self.db.one(
            "SELECT COALESCE(MAX(revision),0) AS n FROM session_events WHERE task_id=?",
            (task_id,))
        return int((row or {}).get("n") or 0)

    def arguments(self, task_id: str, inputs: list[Path], description: str,
                  session_revision: int = 0) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "inputs": [str(p) for p in inputs],
            "input_files": self.input_summary(inputs),
            "description_sha256": _sha_text(description or ""),
            "session_revision": int(session_revision),
        }

    @staticmethod
    def arguments_hash(arguments: dict[str, Any]) -> str:
        return _sha_text(canonical_json(arguments))

    def check(self, *, task: dict[str, Any], skill: Any, inputs: list[Path],
              description: str, session_revision: int = 0,
              approval_id: str = "", actor_role: str = "engineer") -> dict[str, Any]:
        manifest = self.manifest(skill)
        arguments = self.arguments(str(task.get("id") or ""), inputs, description,
                                   session_revision)
        args_hash = self.arguments_hash(arguments)
        reasons: list[str] = []
        details: list[dict[str, Any]] = []

        def deny(code: str, message: str) -> None:
            reasons.append(code)
            details.append({"code": code, "message": message})

        if self.mode == "off":
            return {"allow": True, "decision": "allow", "mode": "off",
                    "manifest": manifest.public(), "arguments": arguments,
                    "arguments_hash": args_hash, "reasons": [], "details": []}

        if getattr(skill, "status", "") not in ("ready", "degraded"):
            deny("tool_unavailable", f"技能状态 {getattr(skill, 'status', '')}")
        role_rank = {"engineer": 1, "reviewer": 2, "admin": 3}
        if role_rank.get(actor_role, 0) < role_rank.get(manifest.min_role, 1):
            deny("role_denied", f"需要 {manifest.min_role} 权限")
        validation = validate_schema({
            "task_id": str(task.get("id") or ""),
            "inputs": [str(p) for p in inputs],
            "description": str(description or "")[:6000],
            "session_revision": int(session_revision),
        }, manifest.input_schema)
        if validation:
            deny("schema_invalid", "; ".join(validation[:6]))
        if len(inputs) > MAX_INPUT_FILES:
            deny("input_count", f"输入文件数 {len(inputs)} 超过上限 {MAX_INPUT_FILES}")
        if not inputs:
            deny("input_empty", "没有输入文件")
        roots = [Path(self.config.data_dir), Path(self.config.skills_dir),
                 Path(self.config.cases_dir)]
        from .util import within_directory
        task_inputs: list[Path] = []
        try:
            for item in json.loads(task.get("input_json") or "[]"):
                if isinstance(item, dict) and item.get("stored_path"):
                    task_inputs.append(Path(str(item["stored_path"])))
                elif isinstance(item, str):
                    task_inputs.append(Path(item))
        except json.JSONDecodeError:
            task_inputs = []
        for path in inputs:
            if any(within_directory(root, path) for root in roots):
                continue
            resolved = path.resolve()
            if any(resolved == p.resolve() for p in task_inputs):
                continue
            deny("path_denied", f"输入路径不在允许根目录或任务输入清单: {path.name}")
            break
        if manifest.network not in ("none", "allow"):
            deny("network_declared", f"未知网络声明 {manifest.network}")
        if manifest.network == "none" and self.mode == "enforce" \
                and str(self.config.get("tool_sandbox_mode", "audit")) == "off":
            deny("network_unenforced", "禁网工具不能在没有沙箱的模式下执行")
        if manifest.risk in ("high", "destructive") and manifest.approval != "required":
            deny("approval_contract", "高风险工具必须声明审批")
        if manifest.approval == "required":
            approval = self.approval_status(approval_id) if approval_id else None
            if not approval:
                deny("approval_required", "缺少审批")
            elif (approval.get("task_id") != str(task.get("id") or "")
                  or approval.get("tool_name") != manifest.tool_name
                  or approval.get("tool_version") != manifest.version
                  or approval.get("arguments_hash") != args_hash
                  or approval.get("status") not in ("approved", "used")
                  or (approval.get("expires_at") or "") <= iso_now()):
                deny("approval_mismatch", "审批与当前任务/工具版本/参数不匹配或已过期")
        options = {}
        try:
            options = json.loads(task.get("options_json") or "{}")
        except json.JSONDecodeError:
            options = {}
        site = str(options.get("site") or options.get("site_name") or task.get("site") or "")
        if manifest.allowed_sites and site and site not in manifest.allowed_sites:
            deny("site_denied", f"站点 {site} 不在工具作用域")
        device = str(options.get("device_type") or options.get("device") or "")
        if manifest.allowed_device_types and device \
                and device not in manifest.allowed_device_types:
            deny("device_denied", f"设备 {device} 不在工具适用域")

        decision = "deny" if reasons else "allow"
        if self.mode == "shadow":
            critical = [code for code in reasons if code in _SHADOW_CRITICAL]
            if critical:
                return {"allow": False, "decision": "deny", "mode": "shadow",
                        "would_deny": True, "shadow_critical": critical,
                        "reasons": reasons, "details": details,
                        "manifest": manifest.public(), "arguments": arguments,
                        "arguments_hash": args_hash}
            return {"allow": True, "decision": "shadow", "mode": "shadow",
                    "would_deny": bool(reasons), "reasons": reasons, "details": details,
                    "manifest": manifest.public(), "arguments": arguments,
                    "arguments_hash": args_hash}
        return {"allow": not reasons, "decision": decision, "mode": "enforce",
                "reasons": reasons, "details": details, "manifest": manifest.public(),
                "arguments": arguments, "arguments_hash": args_hash}

    def record_start(self, *, task_id: str, manifest: ToolManifest,
                     decision: dict[str, Any], approval_id: str = "",
                     session_revision: int = 0) -> str:
        call_id = new_id("TC")
        if not getattr(self, "db", None):
            return call_id
        risk = manifest.risk
        status = "running" if decision.get("allow") else "denied"
        self.db.execute(
            "INSERT INTO tool_calls(id,task_id,session_revision,tool_name,tool_version,"
            "manifest_hash,arguments_json,arguments_hash,decision,decision_json,risk,"
            "approval_id,status,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, task_id, int(session_revision), manifest.tool_name, manifest.version,
             manifest.contract_sha256, canonical_json(decision.get("arguments") or {}),
             str(decision.get("arguments_hash") or ""), str(decision.get("decision") or ""),
             canonical_json({"reasons": decision.get("reasons") or [],
                             "details": decision.get("details") or [],
                             "mode": decision.get("mode") or "",
                             "would_deny": decision.get("would_deny", False)}),
             risk, approval_id, status, iso_now(), iso_now() if status == "denied" else None))
        return call_id

    def record_finish(self, call_id: str, *, status: str, result: dict[str, Any] | None = None,
                      error: str = "", resource: dict[str, Any] | None = None) -> None:
        if not getattr(self, "db", None) or not call_id:
            return
        result = result or {}
        output_hash = _sha_text(canonical_json({
            k: result.get(k) for k in ("status", "primary", "verdict", "evidence")
        })) if result else ""
        self.db.execute(
            "UPDATE tool_calls SET status=?,output_hash=?,resource_json=?,error=?,finished_at=? "
            "WHERE id=?",
            (status, output_hash, canonical_json(resource or {}), (error or "")[:1000],
             iso_now(), call_id))
        approval_id = (self.db.one("SELECT approval_id FROM tool_calls WHERE id=?", (call_id,))
                       or {}).get("approval_id")
        if approval_id and status in ("done", "failed", "timeout", "cancelled"):
            self.db.execute("UPDATE tool_approvals SET status='used',used_at=COALESCE(used_at,?) "
                            "WHERE id=? AND status='approved'", (iso_now(), approval_id))

    def create_approval(self, *, task_id: str, manifest: ToolManifest,
                        arguments_hash: str, requested_by: str, reason: str = "",
                        ttl_minutes: int = 120) -> dict[str, Any]:
        approval_id = new_id("AP")
        now = time.time()
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + ttl_minutes * 60))
        self.db.execute(
            "INSERT INTO tool_approvals(id,task_id,tool_name,tool_version,arguments_hash,"
            "status,requested_by,reason,expires_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (approval_id, task_id, manifest.tool_name, manifest.version, arguments_hash,
             "pending", requested_by, reason[:600], expires, iso_now()))
        return self.approval_status(approval_id) or {}

    def decide_approval(self, approval_id: str, *, approved: bool, actor: str,
                        reason: str = "") -> dict[str, Any]:
        row = self.approval_status(approval_id)
        if not row:
            raise ValueError("审批不存在")
        if row["status"] != "pending":
            raise ValueError("审批已处理")
        if (row.get("expires_at") or "") <= iso_now():
            self.db.execute("UPDATE tool_approvals SET status='expired' WHERE id=?", (approval_id,))
            raise ValueError("审批已过期")
        self.db.execute(
            "UPDATE tool_approvals SET status=?,approved_by=?,decided_at=?,reason=? WHERE id=?",
            ("approved" if approved else "rejected", actor, iso_now(),
             (reason or row.get("reason") or "")[:600], approval_id))
        return self.approval_status(approval_id) or {}

    def find_approved(self, task_id: str, tool_name: str, tool_version: str,
                      arguments_hash: str) -> str:
        if not getattr(self, "db", None):
            return ""
        row = self.db.one(
            "SELECT id FROM tool_approvals WHERE task_id=? AND tool_name=? AND tool_version=? "
            "AND arguments_hash=? AND status='approved' AND expires_at>? "
            "ORDER BY decided_at DESC LIMIT 1",
            (task_id, tool_name, tool_version, arguments_hash, iso_now()))
        return str(row["id"]) if row else ""

    def approval_status(self, approval_id: str) -> dict[str, Any] | None:
        if not approval_id:
            return None
        if not getattr(self, "db", None):
            return None
        return self.db.one("SELECT * FROM tool_approvals WHERE id=?", (approval_id,))

    def list_calls(self, task_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tool_calls"
        args: list[Any] = []
        if task_id:
            sql += " WHERE task_id=?"
            args.append(task_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        rows = self.db.all(sql, tuple(args))
        for row in rows:
            for key in ("arguments_json", "decision_json", "resource_json"):
                try:
                    row[key[:-5]] = json.loads(row.pop(key) or "{}")
                except json.JSONDecodeError:
                    row[key[:-5]] = {}
        return rows

    def list_approvals(self, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tool_approvals"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return self.db.all(sql, tuple(args))
