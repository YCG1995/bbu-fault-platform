"""运行配置：config.json + 环境变量覆盖，所有路径统一解析为绝对路径。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from . import BASE_DIR

DEFAULTS: dict[str, Any] = {
    "brand": "BBU 故障诊断平台",
    "host": "0.0.0.0",
    "port": 8080,
    "work_dir": "",
    "data_dir": "data",
    "skills_dir": "skills",
    "cases_dir": "cases",
    "examples_dir": "examples",
    "meta_skill_dir": "meta/bbu-fault-skill-builder",
    "domain_pack": "bbu",
    "max_workers": 4,
    "max_queue": 32,
    "max_skill_parallel": 4,
    "workshop_max_workers": 1,
    "workshop_max_queue": 16,
    "storage_max_workers": 1,
    "storage_max_queue": 8,
    "skill_timeout_sec": 900,
    "task_timeout_sec": 2400,
    "max_task_resume": 1,
    "llm_retry_attempts": 2,
    "llm_retry_backoff_sec": 2.0,
    "llm_health_window": 20,
    "metrics_token": "",
    "assistant_enabled": True,
    "assistant_rate_per_min": 12,
    "mcp_allow_remote": False,
    "max_llm_calls": 12,
    "max_total_tokens": 200000,
    "max_upload_mb": 2048,
    "max_update_mb": 512,
    "max_migration_mb": 4096,
    "workshop_source_mode": "excerpt",
    "workshop_egress_max_chars": 12000,
    "workshop_digest_max_chars": 12000,
    "workshop_digest_max_lines": 4000,
    "diagnostic_egress_max_chars": 8000,
    "session_continue_enabled": True,
    "session_max_supplements": 3,
    "evaluation_hidden_min_cases": 20,
    "evaluation_hidden_min_f1": 0.75,
    "tool_policy_mode": "shadow",
    "tool_sandbox_mode": "audit",
    "tool_sandbox_cpu_sec": 600,
    "tool_sandbox_memory_mb": 2048,
    "tool_sandbox_max_output_mb": 128,
    "tool_sandbox_max_children": 32,
    "tool_approval_ttl_min": 120,
    "skill_manifest_mode": "shadow",
    "model_schema_mode": "auto",
    "model_capability_cache_hours": 24,
    "model_discovery_allow_private": True,
    "model_request_max_bytes": 1048576,
    "rule_scope_v2": "shadow",
    "agent_engine_mode": "shadow",
    "agent_max_steps": 4,
    "agent_max_skills": 16,
    "agent_max_replans": 1,
    "calibration_min_samples": 20,
    "import_max_files": 5000,
    "import_max_total_mb": 512,
    "import_max_ratio": 100,
    "upgrade_service_name": "bbu-fault-platform",
    "upgrade_service_user": "bbu",
    "upgrade_helper_cmd": "",
    "upgrade_helper_trigger": "/usr/local/bin/bbu-upgrade-helper-trigger",
    "max_extract_files": 200000,
    "max_extract_mb": 8192,
    "session_hours": 12,
    "session_warn_sec": 120,
    "cookie_secure": False,
    "password_min_length": 10,
    "allow_self_register": False,
    "login_rate_window_sec": 300,
    "login_rate_ip_limit": 30,
    "login_rate_account_limit": 10,
    "trusted_proxies": [],
    "local_tz_offset": 8,
    "min_route_score": 30,
    "fast_max_skills": 2,
    "deep_max_skills": 4,
    "planner_enabled": True,
    "planner_replan": True,
    "planner_max_steps": 4,
    "planner_max_total_skills": 8,
    "planner_replan_max_skills": 4,
    "auto_distill": True,
    "experience_include_unreviewed": False,
    "report_llm": True,
    "allow_engineer_workshop": True,
    "install_demo_skills": True,
    "log_level": "info",
    "personal_model_base_url": "https://nebulacoder-maas.zte.com.cn/v1",
    "personal_model_default": "deepseek-v4-flash",
    "personal_model_models": [
        "deepseek-v4-flash",
        "nebulacoder-lite-v7.0",
        "nebulacoder-lite-cot-v7.0",
    ],
    "personal_api_key_url": "https://maas.zte.com.cn/zte-paas-zcloud-maasui/personal-api-key",
    "trace_enabled": True,
    "trace_retention_days": 90,
    "trace_max_event_mb": 4,
    "trace_max_run_mb": 64,
}

# 环境变量覆盖（部署时不必改 config.json）
def _env_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


ENV_OVERRIDES = {
    "BBU_HOST": ("host", str),
    "BBU_PORT": ("port", int),
    "BBU_WORK_DIR": ("work_dir", str),
    "BBU_DATA_DIR": ("data_dir", str),
    "BBU_SKILLS_DIR": ("skills_dir", str),
    "BBU_CASES_DIR": ("cases_dir", str),
    "BBU_META_DOMAIN_PACK": ("domain_pack", str),
    "BBU_MAX_WORKERS": ("max_workers", int),
    "BBU_RULE_SCOPE_V2": ("rule_scope_v2", str),
    "BBU_MODEL_DISCOVERY_ALLOW_PRIVATE": ("model_discovery_allow_private", _env_bool),
    "BBU_SESSION_HOURS": ("session_hours", int),
    "BBU_SESSION_WARN_SEC": ("session_warn_sec", int),
    "BBU_COOKIE_SECURE": ("cookie_secure", _env_bool),
    "BBU_PASSWORD_MIN_LENGTH": ("password_min_length", int),
    "BBU_LOGIN_RATE_IP_LIMIT": ("login_rate_ip_limit", int),
    "BBU_LOGIN_RATE_ACCOUNT_LIMIT": ("login_rate_account_limit", int),
    "BBU_TRUSTED_PROXIES": ("trusted_proxies", lambda v: [x.strip() for x in v.split(",") if x.strip()]),
    "BBU_METRICS_TOKEN": ("metrics_token", str),
    "BBU_ASSISTANT_ENABLED": ("assistant_enabled", _env_bool),
    "BBU_ASSISTANT_RATE_PER_MIN": ("assistant_rate_per_min", int),
    "BBU_MCP_ALLOW_REMOTE": ("mcp_allow_remote", _env_bool),
    "BBU_UPGRADE_HELPER_CMD": ("upgrade_helper_cmd", str),
    "BBU_PERSONAL_MODEL_BASE_URL": ("personal_model_base_url", str),
    "BBU_PERSONAL_MODEL_DEFAULT": ("personal_model_default", str),
    "BBU_PERSONAL_MODEL_MODELS": ("personal_model_models",
                                  lambda v: [x.strip() for x in v.split(",") if x.strip()]),
}

_PATH_KEYS = ("data_dir", "skills_dir", "cases_dir", "examples_dir", "meta_skill_dir")

RUNTIME_SETTINGS: dict[str, dict[str, Any]] = {
    "max_workers": {"label": "诊断任务并发", "group": "任务并发", "min": 1, "max": 32},
    "max_queue": {"label": "诊断任务队列上限", "group": "任务并发", "min": 1, "max": 256},
    "max_skill_parallel": {"label": "单任务技能扇出", "group": "任务并发", "min": 1, "max": 32},
    "workshop_max_workers": {"label": "工坊后台并发", "group": "后台任务", "min": 1, "max": 8},
    "workshop_max_queue": {"label": "工坊后台队列", "group": "后台任务", "min": 1, "max": 128},
    "storage_max_workers": {"label": "空间释放并发", "group": "后台任务", "min": 1, "max": 8},
    "storage_max_queue": {"label": "空间释放队列", "group": "后台任务", "min": 1, "max": 128},
    "max_llm_calls": {"label": "单任务 LLM 调用上限", "group": "模型预算", "min": 0, "max": 200},
    "max_total_tokens": {"label": "单任务 Token 上限", "group": "模型预算", "min": 0, "max": 50000000},
    "task_timeout_sec": {"label": "任务超时（秒）", "group": "模型预算", "min": 30, "max": 86400},
    "skill_timeout_sec": {"label": "技能超时（秒）", "group": "模型预算", "min": 5, "max": 86400},
    "max_task_resume": {"label": "最多补规划次数", "group": "模型预算", "min": 0, "max": 5},
    "llm_retry_attempts": {"label": "模型重试次数", "group": "模型预算", "min": 0, "max": 10},
    "llm_retry_backoff_sec": {"label": "模型重试退避（秒）", "group": "模型预算", "min": 0.1, "max": 60, "type": "float"},
    "llm_health_window": {"label": "模型健康窗口（秒）", "group": "模型预算", "min": 1, "max": 3600},
    "fast_max_skills": {"label": "快速模式技能数", "group": "规划范围", "min": 1, "max": 64},
    "deep_max_skills": {"label": "深度模式技能数", "group": "规划范围", "min": 1, "max": 128},
    "min_route_score": {"label": "最低路由分", "group": "规划范围", "min": 0, "max": 100},
    "planner_max_steps": {"label": "规划最大波次", "group": "规划范围", "min": 1, "max": 12},
    "planner_max_total_skills": {"label": "规划技能总数上限", "group": "规划范围", "min": 1, "max": 128},
    "planner_replan_max_skills": {"label": "补规划技能数上限", "group": "规划范围", "min": 0, "max": 64},
    "max_upload_mb": {"label": "单次上传上限（MB）", "group": "容量与解压", "min": 1, "max": 16384},
    "max_extract_mb": {"label": "解压总大小上限（MB）", "group": "容量与解压", "min": 1, "max": 65536},
    "max_extract_files": {"label": "解压文件数上限", "group": "容量与解压", "min": 100, "max": 2000000},
    "max_update_mb": {"label": "更新包上限（MB）", "group": "容量与解压", "min": 1, "max": 4096},
    "max_migration_mb": {"label": "迁移包上限（MB）", "group": "容量与解压", "min": 1, "max": 16384},
    "allow_self_register": {"label": "允许免邀请码自助注册", "group": "账号与注册",
                            "restart_required": False},
    "tool_policy_mode": {"label": "工具授权模式", "group": "工具与沙箱", "type": "enum",
                         "choices": ["off", "shadow", "enforce"],
                         "choice_labels": {"off": "关闭", "shadow": "影子模式（只审计）",
                                           "enforce": "强制拦截"}},
    "tool_sandbox_mode": {"label": "沙箱模式", "group": "工具与沙箱", "type": "enum",
                          "choices": ["off", "audit", "strict"],
                          "choice_labels": {"off": "关闭", "audit": "审计模式",
                                            "strict": "强制隔离"}},
    "tool_sandbox_cpu_sec": {"label": "沙箱 CPU 上限（秒）", "group": "工具与沙箱",
                             "min": 1, "max": 86400},
    "tool_sandbox_memory_mb": {"label": "沙箱内存上限（MB）", "group": "工具与沙箱",
                               "min": 64, "max": 32768},
    "tool_sandbox_max_output_mb": {"label": "沙箱输出上限（MB）", "group": "工具与沙箱",
                                   "min": 1, "max": 2048},
    "tool_sandbox_max_children": {"label": "沙箱子进程预算", "group": "工具与沙箱",
                                  "min": 4, "max": 512},
    "tool_approval_ttl_min": {"label": "工具审批有效期（分钟）", "group": "工具与沙箱",
                              "min": 5, "max": 10080},
    "skill_manifest_mode": {"label": "技能清单校验模式", "group": "工具与沙箱",
                            "type": "enum", "choices": ["off", "shadow", "enforce"],
                            "choice_labels": {"off": "关闭", "shadow": "影子模式（只审计）",
                                              "enforce": "强制拦截"}},
    "model_schema_mode": {"label": "模型 JSON 能力模式", "group": "工具与沙箱",
                          "type": "enum", "choices": ["auto", "probe", "off"],
                          "choice_labels": {"auto": "自动", "probe": "启动时探测",
                                            "off": "关闭"}},
    "model_capability_cache_hours": {"label": "模型能力缓存（小时）", "group": "工具与沙箱",
                                     "min": 1, "max": 720},
    "import_max_files": {"label": "技能导入文件数上限", "group": "工具与沙箱",
                         "min": 1, "max": 200000},
    "import_max_total_mb": {"label": "技能导入解压上限（MB）", "group": "工具与沙箱",
                            "min": 1, "max": 16384},
    "import_max_ratio": {"label": "技能导入压缩比上限", "group": "工具与沙箱",
                         "min": 1, "max": 10000},
    "agent_engine_mode": {"label": "有界 Agent 模式", "group": "诊断 Agent",
                          "type": "enum", "choices": ["off", "shadow", "canary", "bounded"],
                          "choice_labels": {"off": "关闭", "shadow": "影子模式",
                                            "canary": "灰度", "bounded": "强制有界"}},
    "agent_max_steps": {"label": "Agent 最大波次", "group": "诊断 Agent", "min": 1, "max": 12},
    "agent_max_skills": {"label": "Agent 最大技能数", "group": "诊断 Agent", "min": 1, "max": 64},
    "agent_max_replans": {"label": "Agent 最大重规划", "group": "诊断 Agent", "min": 0, "max": 2},
    "calibration_min_samples": {"label": "校准最小样本", "group": "诊断 Agent", "min": 1, "max": 500},
    "trace_enabled": {"label": "记录执行轨迹", "group": "执行轨迹", "restart_required": False},
    "trace_retention_days": {"label": "轨迹保留天数", "group": "执行轨迹", "min": 1, "max": 3650},
    "trace_max_event_mb": {"label": "单事件体积上限（MB）", "group": "执行轨迹", "min": 1, "max": 64},
    "trace_max_run_mb": {"label": "单轨迹体积上限（MB）", "group": "执行轨迹", "min": 1, "max": 512},
}
RUNTIME_ENV_LABELS: dict[str, str] = {
    "max_workers": "BBU_MAX_WORKERS",
    "max_llm_calls": "BBU_MAX_LLM_CALLS",
    "max_total_tokens": "BBU_MAX_TOTAL_TOKENS",
    "task_timeout_sec": "BBU_TASK_TIMEOUT_SEC",
    "max_upload_mb": "BBU_MAX_UPLOAD_MB",
}



def _path_items(values: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(key, values.get(key)) for key in _PATH_KEYS]


class Config:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or BASE_DIR).resolve()
        self.values: dict[str, Any] = copy.deepcopy(DEFAULTS)
        cfg_file = Path(os.environ.get("BBU_CONFIG_FILE") or (self.root / "config.json"))
        self.config_file = cfg_file
        if cfg_file.exists():
            try:
                loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.values.update(loaded)
            except (OSError, json.JSONDecodeError) as exc:
                raise SystemExit(f"config.json 解析失败: {exc}") from exc
        explicit_path_env = any(name in os.environ for name in
                                ("BBU_DATA_DIR", "BBU_SKILLS_DIR", "BBU_CASES_DIR"))
        work_env = "BBU_WORK_DIR" in os.environ
        for env, (key, cast) in ENV_OVERRIDES.items():
            if env in os.environ:
                try:
                    self.values[key] = cast(os.environ[env])
                except ValueError as exc:
                    raise SystemExit(f"环境变量 {env} 值非法: {exc}") from exc

        # 统一工作目录是主键；单项路径环境变量保留用于旧 systemd 单元兼容。
        raw_work = str(self.values.get("work_dir") or "").strip()
        if raw_work and (work_env or not explicit_path_env):
            work = Path(raw_work)
            work = (work if work.is_absolute() else (self.root / work)).resolve()
            self.values["work_dir"] = str(work)
            self.values["data_dir"] = str(work / "data")
            self.values["skills_dir"] = str(work / "skills")
            self.values["cases_dir"] = str(work / "cases")

        for key in _PATH_KEYS:
            raw = Path(str(self.values[key]))
            self.values[key] = str(raw if raw.is_absolute() else (self.root / raw).resolve())
        if explicit_path_env and not work_env:
            # 旧 systemd 单项路径变量优先于 config.json 中可能过期的 work_dir。
            self.values["work_dir"] = self._common_work_dir()
        if not str(self.values.get("work_dir") or "").strip():
            self.values["work_dir"] = self._common_work_dir()
        self._reject_root_paths()
        self.values["_root"] = str(self.root)
        self.ensure_dirs()

    def _reject_root_paths(self) -> None:
        """拒绝把运行目录解析到文件系统根，避免误写 /data、/skills 或 /。"""
        for key, raw in [("work_dir", self.values.get("work_dir")), *_path_items(self.values)]:
            if not raw:
                continue
            path = Path(str(raw))
            if path == Path(path.anchor):
                raise SystemExit(
                    f"{key} 不能指向文件系统根目录: {path}；请配置到独立工作目录")

    def _common_work_dir(self) -> str:
        try:
            common = Path(os.path.commonpath([
                self.values["data_dir"], self.values["skills_dir"], self.values["cases_dir"],
            ]))
            if common.name in ("data", "skills", "cases"):
                common = common.parent
            if common == Path(common.anchor):
                # 三个路径分处不同挂载点时 commonpath 会退化为根目录；
                # 此时以 data 的父目录作为工作目录，避免尝试写入 /updates。
                common = Path(self.values["data_dir"]).parent
            return str(common)
        except ValueError:
            return str(Path(self.values["data_dir"]).parent)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    @property
    def work_dir(self) -> Path:
        """统一运行数据根目录；旧部署未配置时按 data/skills/cases 的公共父目录推断。"""
        raw = str(self.values.get("work_dir") or "").strip()
        if raw:
            return Path(raw)
        try:
            common = Path(os.path.commonpath([
                str(self.data_dir), str(self.skills_dir), str(self.cases_dir),
            ]))
            if common.name in ("data", "skills", "cases"):
                common = common.parent
            return common
        except ValueError:
            return self.data_dir.parent

    @property
    def data_dir(self) -> Path:
        return Path(self.values["data_dir"])

    @property
    def skills_dir(self) -> Path:
        return Path(self.values["skills_dir"])

    @property
    def cases_dir(self) -> Path:
        return Path(self.values["cases_dir"])

    @property
    def examples_dir(self) -> Path:
        return Path(self.values["examples_dir"])

    @property
    def meta_skill_dir(self) -> Path:
        return Path(self.values["meta_skill_dir"])

    @property
    def domain_pack(self) -> str:
        """平台默认域包；子技能 manifest/profile 可覆盖。"""
        return str(self.values.get("domain_pack") or "bbu")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bbu.db"

    @property
    def password_min_length(self) -> int:
        try:
            return max(8, min(64, int(self.values.get("password_min_length", 10))))
        except (TypeError, ValueError):
            return 10

    @property
    def cookie_secure(self) -> bool:
        return bool(self.values.get("cookie_secure", False))

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir,
            self.data_dir / "uploads",
            self.data_dir / "tasks",
            self.data_dir / "workshop",
            self.data_dir / "skill_versions",
            self.data_dir / "tmp",
            self.data_dir / "logs",
            self.skills_dir,
            self.cases_dir,
            self.work_dir / "updates",
            self.work_dir / "backups",
        ):
            p.mkdir(parents=True, exist_ok=True)

    def _env_for_key(self, key: str) -> str:
        for env, (name, _cast) in ENV_OVERRIDES.items():
            if name == key and env in os.environ:
                return env
        return ""

    def runtime_items(self) -> list[dict[str, Any]]:
        """管理页可编辑的并发、预算和容量参数；全部在重启后重新装配。"""
        items = []
        for key, meta in RUNTIME_SETTINGS.items():
            default = DEFAULTS.get(key)
            value = self.values.get(key, default)
            value_type = meta.get("type") or (
                "bool" if isinstance(default, bool) else
                "float" if isinstance(default, float) else "int")
            items.append({
                "key": key, "label": meta["label"], "group": meta["group"],
                "value": value, "default": default, "type": value_type,
                "min": meta.get("min"), "max": meta.get("max"),
                "choices": meta.get("choices") or [],
                "choice_labels": meta.get("choice_labels") or {},
                "restart_required": bool(meta.get("restart_required", True)),
                "env_override": self._env_for_key(key),
            })
        return items

    def save_runtime(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        """原子保存白名单运行参数；返回保存后的字段快照。"""
        if not isinstance(values, dict) or not values:
            raise ValueError("没有需要保存的运行参数")
        unknown = sorted(set(values) - set(RUNTIME_SETTINGS))
        if unknown:
            raise ValueError("不允许修改的配置项：" + "、".join(unknown))
        updates: dict[str, Any] = {}
        for key, raw in values.items():
            meta = RUNTIME_SETTINGS[key]
            env = self._env_for_key(key)
            if env:
                raise ValueError(f"{key} 由环境变量 {env} 覆盖，请在服务环境配置中修改")
            default = DEFAULTS.get(key)
            try:
                if isinstance(default, bool):
                    if isinstance(raw, str):
                        if raw.lower() not in ("true", "false", "1", "0", "on", "off"):
                            raise ValueError
                        value = raw.lower() in ("true", "1", "on")
                    else:
                        value = bool(raw)
                elif isinstance(default, float):
                    value = float(raw)
                elif isinstance(default, str):
                    value = str(raw).strip().lower()
                    choices = meta.get("choices")
                    if choices and value not in choices:
                        raise ValueError(f"{key} 只能是 {'/'.join(choices)}")
                else:
                    value = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 需要 {meta.get('type', type(default).__name__)} 值") from exc
            low, high = meta.get("min"), meta.get("max")
            if low is not None and value < low:
                raise ValueError(f"{key} 不能小于 {low}")
            if high is not None and value > high:
                raise ValueError(f"{key} 不能大于 {high}")
            updates[key] = value
        cfg_file = self.config_file
        existing: dict[str, Any] = {}
        if cfg_file.exists():
            try:
                loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"现有 config.json 无法解析，拒绝覆盖：{exc}") from exc
        existing.pop("_root", None)
        existing.update(updates)
        if cfg_file.exists():
            try:
                (cfg_file.parent / (cfg_file.name + ".bak")).write_bytes(cfg_file.read_bytes())
            except OSError:
                pass
        tmp = cfg_file.parent / (cfg_file.name + ".tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, cfg_file)
        self.values.update(updates)
        saved = []
        for key, value in updates.items():
            meta = RUNTIME_SETTINGS[key]
            saved.append({"key": key, "label": meta["label"], "value": value,
                          "restart_required": bool(meta.get("restart_required", True))})
        return saved

    def public(self) -> dict[str, Any]:
        """可暴露给前端的配置（不含 key 等敏感信息）。"""
        keys = (
            "brand", "host", "port", "work_dir", "max_workers", "max_skill_parallel",
            "max_queue", "skill_timeout_sec", "task_timeout_sec", "max_llm_calls",
            "max_migration_mb",
            "llm_retry_attempts", "llm_retry_backoff_sec",
            "max_total_tokens", "max_upload_mb", "max_update_mb", "session_hours", "local_tz_offset",
            "session_warn_sec", "cookie_secure", "password_min_length",
            "allow_self_register",
            "tool_policy_mode", "tool_sandbox_mode", "tool_sandbox_cpu_sec",
            "tool_sandbox_memory_mb", "tool_sandbox_max_output_mb",
            "tool_sandbox_max_children", "tool_approval_ttl_min",
            "skill_manifest_mode", "model_schema_mode", "model_capability_cache_hours",
            "import_max_files", "import_max_total_mb", "import_max_ratio",
            "agent_engine_mode", "agent_max_steps", "agent_max_skills",
            "agent_max_replans", "calibration_min_samples",
            "min_route_score", "fast_max_skills", "deep_max_skills",
            "planner_enabled", "planner_replan", "planner_max_steps",
            "planner_max_total_skills", "planner_replan_max_skills",
            "auto_distill", "report_llm",
            "allow_engineer_workshop", "install_demo_skills",
        )
        return {k: self.values.get(k) for k in keys}
