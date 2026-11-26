"""REST API：认证、任务、技能、工坊、模型、用户、审计。

所有接口返回 (status, headers, body)；错误统一抛 HTTPError。
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import yaml

from . import __version__, updater
from .accounts import (activate_invite, create_invite, list_invites as list_invites_service,
                       normalize_status, public_user, register_user, revoke_invite)
from .assistant import AssistantError, AssistantService
from .backups import BackupManager
from .ai import AIGateway, list_models, AIError
from .config import Config
from .db import Database
from .evaluation import EvaluationService, RealCaseStore
from .evidence_store import EvidenceStore
from .migration import (
    workdir_supported,
    MaintenanceGate, MigrationConflict, MigrationError, MigrationManager,
    allowed_work_roots, assert_idle, delete_job, list_jobs, mark_interrupted,
    public_status, find_job_file, validate_target_root,
)
from .mcp_server import MCPService
from .metrics import collect as collect_metrics, prometheus as prometheus_metrics
from .model_roles import MODEL_ROLES
from .netguard import OutboundPolicyError
from .notifications import create_notification, notification_public
from .httpd import HTTPError, Router, response_file, response_json
from .learning import LearningService
from .pipeline import TaskPipeline
from .review import ReviewService
from .agent import BoundedAgent
from .calibration import CalibrationStore
from .provenance import audit_provenance, lock_fields
from .workshop_spec import parse_source, public_spec
from .recovery import (compare as recovery_compare, create_run as recovery_create,
                       get_run as recovery_get, list_runs as recovery_list,
                       observe as recovery_observe, review as recovery_review)
from .case_matrix import (build_matrix, decide_candidate, latest_matrix, save_matrix,
                          verify_matrix)
from .security import (ROLE_RANK, SESSION_COOKIE, SecretBox, bootstrap_admin, check_role,
                       cleanup_sessions, create_session, delete_other_sessions, delete_session,
                       delete_user_sessions, extend_session, get_session, hash_password,
                       list_user_sessions, revoke_user_session, rotate_session_csrf,
                       session_public_id, validate_initial_password, validate_password, verify_password)
from .skills import SkillRegistry, SkillRunner, parse_frontmatter
from .session import DiagnosticSession, RevisionConflict
from .tool_contract import ToolPolicy
from .trace import TraceStore
from .usage import UsageStore
from .storage import save_upload
from .util import (CST, iso_now, new_id, read_json, safe_filename, sha256_file, truncate,
                   unique_path, within_directory, write_json)
from .worker import TaskWorker
from .workshop import WorkshopService
from .workshop_jobs import WorkshopJobService
from .task_storage import TaskStorageService

LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_LOCK = threading.Lock()


def _loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except json.JSONDecodeError:
        return default


def _cookie(req, name: str) -> str | None:
    raw = req.headers.get("Cookie")
    if not raw:
        return None
    try:
        jar = SimpleCookie()
        jar.load(raw)
        return jar[name].value if name in jar else None
    except Exception:  # noqa: BLE001
        return None


def _client_ip(app: "App", req) -> str:
    """仅当直连地址命中显式配置的代理网段时，才信任 X-Forwarded-For。"""
    direct = req.ip or ""
    trusted = app.config.get("trusted_proxies") or []
    if not trusted:
        return direct
    try:
        address = ipaddress.ip_address(direct)
    except ValueError:
        return direct
    allowed = False
    for item in trusted:
        try:
            if address in ipaddress.ip_network(str(item), strict=False):
                allowed = True
                break
        except ValueError:
            continue
    if not allowed:
        return direct
    forwarded = (req.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    if not forwarded:
        return direct
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return direct


def _auth(app: "App", req, minimum: str = "engineer", csrf: bool = True,
          allow_password_change: bool = False) -> dict[str, Any]:
    token = _cookie(req, SESSION_COOKIE)
    session = get_session(app.db, token)
    if not session:
        raise HTTPError(401, "未登录或会话已过期")
    req.session = session
    req.user = {"id": session["user_id"], "username": session["username"],
                "display_name": session["display_name"], "role": session["role"],
                "must_change": session["must_change"]}
    if not check_role(req.user, minimum):
        raise HTTPError(403, "权限不足")
    if csrf and req.method in ("POST", "PUT", "PATCH", "DELETE"):
        if req.headers.get("X-CSRF", "") != session["csrf"]:
            raise HTTPError(403, "CSRF 校验失败，请刷新页面后重试")
    return req.user


def _admin(app: "App", req, csrf: bool = True,
           allow_password_change: bool = False) -> dict[str, Any]:
    return _auth(app, req, "admin", csrf, allow_password_change)


def _rate_limit(app: "App", key: str, default: int) -> int:
    try:
        return max(1, int(app.config.get(key, default)))
    except (TypeError, ValueError):
        return default


def _recent_attempts(key: str, now: float, window: int) -> list[float]:
    attempts = [t for t in LOGIN_ATTEMPTS.get(key, []) if now - t < window]
    if attempts:
        LOGIN_ATTEMPTS[key] = attempts
    else:
        LOGIN_ATTEMPTS.pop(key, None)
    return attempts


def _check_login_rate(app: "App", ip: str, username: str) -> None:
    now = time.time()
    window = _rate_limit(app, "login_rate_window_sec", 300)
    ip_limit = _rate_limit(app, "login_rate_ip_limit", 30)
    account_limit = _rate_limit(app, "login_rate_account_limit", 10)
    account_key = "user:" + (username or "").strip().lower()
    ip_key = "ip:" + (ip or "unknown")
    with LOGIN_LOCK:
        ip_attempts = _recent_attempts(ip_key, now, window)
        account_attempts = _recent_attempts(account_key, now, window)
        blocked = []
        if len(ip_attempts) >= ip_limit:
            blocked.append(ip_attempts)
        if username and len(account_attempts) >= account_limit:
            blocked.append(account_attempts)
        if blocked:
            oldest = min(min(items) for items in blocked)
            retry_after = max(1, int(oldest + window - now) + 1)
            raise HTTPError(
                429, "登录尝试过于频繁，请稍后再试",
                {"code": "login_rate_limited", "retry_after": retry_after},
            )


def _record_login_failure(app: "App", ip: str, username: str) -> None:
    now = time.time()
    window = _rate_limit(app, "login_rate_window_sec", 300)
    keys = ["ip:" + (ip or "unknown")]
    if username:
        keys.append("user:" + username.strip().lower())
    with LOGIN_LOCK:
        for key in keys:
            attempts = _recent_attempts(key, now, window)
            attempts.append(now)
            LOGIN_ATTEMPTS[key] = attempts[-max(1, _rate_limit(app, "login_rate_ip_limit", 30)):]


def _clear_login_failures(ip: str, username: str) -> None:
    with LOGIN_LOCK:
        if username:
            LOGIN_ATTEMPTS.pop("user:" + username.strip().lower(), None)


def _set_cookie(token: str, max_age: int, secure: bool = False) -> str:
    value = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
             f"Max-Age={max_age}")
    return value + ("; Secure" if secure else "")


def _task_for(app: "App", req, task_id: str) -> dict[str, Any]:
    task = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPError(404, "任务不存在")
    user = _auth(app, req)
    if user["role"] == "admin":
        return task
    if task["user_id"] == user["id"]:
        return task
    if user["role"] == "reviewer" and task.get("reviewer_id") == user["id"]:
        return task
    raise HTTPError(404, "任务不存在")


def _task_owner_for(app: "App", req, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    user = _auth(app, req)
    task = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task or (user["role"] != "admin" and task["user_id"] != user["id"]):
        raise HTTPError(404, "任务不存在")
    return task, user


def _duration(task: dict[str, Any]) -> float | None:
    if not task.get("started_at") or not task.get("finished_at"):
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        a = datetime.strptime(task["started_at"], fmt).replace(tzinfo=timezone.utc)
        b = datetime.strptime(task["finished_at"], fmt).replace(tzinfo=timezone.utc)
        return round((b - a).total_seconds(), 1)
    except ValueError:
        return None


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    verdict = _loads(task.get("verdict_json"), {}) or {}
    stats = _loads(task.get("stats_json"), {}) or {}
    return {
        "id": task["id"], "title": task["title"], "title_source": task.get("title_source") or "",
        "status": task["status"],
        "stage": task["stage"], "progress": task["progress"], "mode": task["mode"],
        "created_at": task["created_at"], "finished_at": task.get("finished_at"),
        "duration_sec": _duration(task),
        "verdict": verdict,
        "root_cause": verdict.get("primary") and verdict.get("primary") != "NONE",
        "root_cause_name": verdict.get("root_cause_name"),
        "confidence": verdict.get("confidence"),
        "tokens": stats.get("total_tokens", 0),
        "inputs_state": task.get("inputs_state") or "available",
        "raw_inputs_available": (task.get("inputs_state") or "available") == "available",
        "error": task.get("error"),
        "reviewer_id": task.get("reviewer_id"),
        "reviewer_assigned_at": task.get("reviewer_assigned_at"),
    }


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.secret_box = SecretBox(config.data_dir)
        self.start_time = time.time()
        self.http_server: Any = None
        self.restart_event = threading.Event()
        self.restart_reason = ""
        self.admin_password = bootstrap_admin(self.db, config.data_dir)
        cleanup_sessions(self.db)
        # 评测是进程内同步任务；重启后不可能还在运行，清理遗留状态避免阻塞迁移。
        self.db.execute(
            "UPDATE evaluations SET status='error', error=?, finished_at=? "
            "WHERE status='running'",
            ("interrupted: 平台重启导致评测中断", iso_now()))
        self.registry = SkillRegistry(self.db, config.skills_dir, data_dir=config.data_dir,
                                      manifest_mode=str(config.get("skill_manifest_mode", "shadow")))
        self.tools = ToolPolicy(self.db, config)
        self.runner = SkillRunner(config, db=self.db, policy=self.tools)
        self.gateway = AIGateway(self.db, config, secret_box=self.secret_box)
        self.usage = UsageStore(self.db, config)
        self.gateway.on_usage = self._record_llm_usage
        self.traces = TraceStore(self.db, config)
        try:
            self.traces.cleanup()
        except Exception:
            pass
        self.workshop = WorkshopService(self.db, config, self.registry, self.gateway)
        self.workshop_jobs = WorkshopJobService(self.db, config, self.workshop, self.gateway,
                                                traces=self.traces)
        self.storage = TaskStorageService(self.db, config)
        self.backups = BackupManager(self.db, config)
        self.learning = LearningService(self.db, config, self.registry, self.workshop)
        self.pipeline = TaskPipeline(self.db, config, self.registry,
                                     self.gateway, self.runner,
                                     experiences=self.learning.experiences,
                                     traces=self.traces)
        self.evidence_store = EvidenceStore(self.db)
        self.assistant = AssistantService(self)
        self.mcp = MCPService(self, self.assistant)
        self.worker = TaskWorker(self.db, config, self.pipeline, learning=self.learning)
        self.migration_gate = MaintenanceGate()
        self.migration = MigrationManager(self, self.migration_gate)
        mark_interrupted(self)
        self.router = Router()
        self._install_demos()
        self.registry.scan()
        self.learning.start()
        self.worker.start()
        self._register()

    # ---------- 生命周期 ----------
    def request_restart(self, reason: str = "") -> None:
        """停止当前 HTTP 服务，由 run.py 重新装配 App 并继续监听同一端口。"""
        server = self.http_server
        if server is None:
            raise RuntimeError("HTTP 服务尚未绑定，无法从网页重启")
        self.restart_reason = reason
        self.restart_event.set()
        threading.Timer(0.5, server.shutdown).start()

    def shutdown_services(self) -> None:
        self.worker.shutdown()
        self.learning.shutdown()
        self.workshop_jobs.shutdown()
        self.storage.shutdown()

    def _record_llm_usage(self, event: dict[str, Any]) -> None:
        try:
            self.usage.record(event)
        except Exception:
            # 用量统计是旁路，不能影响诊断主流程。
            pass

    # ---------- demo ----------
    def _install_demos(self) -> None:
        if not self.config.get("install_demo_skills", True):
            return
        if self.db.setting("demo_installed") == "1":
            return
        if any(p.is_dir() and (p / "SKILL.md").exists() for p in self.config.skills_dir.iterdir()):
            self.db.set_setting("demo_installed", "1")
            return
        demo_root = self.config.examples_dir / "skills"
        if not demo_root.exists():
            return
        for src in sorted(demo_root.iterdir()):
            if not src.is_dir() or not (src / "SKILL.md").exists():
                continue
            target = self.config.skills_dir / src.name
            if target.exists():
                continue
            shutil.copytree(src, target)
        self.db.set_setting("demo_installed", "1")

    # ---------- 路由 ----------
    def _register(self) -> None:
        r = self.router
        r.add("GET", r"/api/health", h_health)
        r.add("GET", r"/api/public/config", h_public_config)
        r.add("POST", r"/api/auth/login", h_login)
        r.add("POST", r"/api/auth/logout", h_logout)
        r.add("GET", r"/api/auth/me", h_me)
        r.add("POST", r"/api/auth/password", h_change_password)
        r.add("POST", r"/api/auth/activate", h_activate)
        r.add("POST", r"/api/auth/register", h_register)
        r.add("POST", r"/api/auth/refresh", h_session_refresh)
        r.add("GET", r"/api/auth/sessions", h_sessions)
        r.add("POST", r"/api/auth/sessions/revoke-others", h_revoke_other_sessions)
        r.add("DELETE", r"/api/auth/sessions/(?P<session_id>[a-f0-9]{12})", h_revoke_session)

        r.add("GET", r"/api/stats", h_stats)
        r.add("GET", r"/api/metrics", h_metrics)
        r.add("GET", r"/api/metrics/prometheus", h_metrics_prometheus)
        r.add("GET", r"/api/usage/overview", h_usage_overview)
        r.add("GET", r"/api/usage/tasks", h_usage_tasks)
        r.add("GET", r"/mcp", h_mcp_http)
        r.add("POST", r"/mcp", h_mcp_http)
        r.add("GET", r"/api/assistant/status", h_assistant_status)
        r.add("GET", r"/api/assistant/suggestions", h_assistant_suggestions)
        r.add("POST", r"/api/assistant/chat", h_assistant_chat)
        r.add("POST", r"/api/assistant/actions/(?P<action_id>[A-Za-z0-9_-]+)/confirm",
              h_assistant_confirm)
        r.add("POST", r"/api/assistant/actions/(?P<action_id>[A-Za-z0-9_-]+)/cancel",
              h_assistant_cancel)
        r.add("GET", r"/api/mcp/info", h_mcp_info)
        r.add("POST", r"/api/mcp/token", h_mcp_token_create)
        r.add("DELETE", r"/api/mcp/token", h_mcp_token_delete)
        r.add("GET", r"/api/cases", h_cases)
        r.add("GET", r"/api/cases/(?P<case_id>[A-Za-z0-9_-]+)", h_case_detail)
        r.add("POST", r"/api/cases/(?P<case_id>[A-Za-z0-9_-]+)/label", h_case_label)
        r.add("POST", r"/api/cases/(?P<case_id>[A-Za-z0-9_-]+)/assign-reviewer", h_case_assign)
        r.add("GET", r"/api/cases/(?P<case_id>[A-Za-z0-9_-]+)/history", h_case_history)
        r.add("GET", r"/api/evaluations", h_evaluations)
        r.add("POST", r"/api/evaluations/run", h_evaluation_run)
        r.add("GET", r"/api/search", h_global_search)
        r.add("GET", r"/api/notifications", h_notifications)
        r.add("POST", r"/api/notifications/(?P<note_id>[A-Za-z0-9_-]+)/read", h_notification_read)
        r.add("POST", r"/api/notifications/read-all", h_notifications_read_all)
        r.add("GET", r"/api/task-drafts/current", h_task_draft_current)
        r.add("POST", r"/api/task-drafts", h_task_draft_create)
        r.add("PUT", r"/api/task-drafts/(?P<draft_id>[Dd][A-Za-z0-9_-]+)", h_task_draft_update)
        r.add("DELETE", r"/api/task-drafts/(?P<draft_id>[Dd][A-Za-z0-9_-]+)", h_task_draft_delete)
        r.add("POST", r"/api/task-drafts/(?P<draft_id>[Dd][A-Za-z0-9_-]+)/files", h_task_draft_file)
        r.add("DELETE", r"/api/task-drafts/(?P<draft_id>[Dd][A-Za-z0-9_-]+)/files/(?P<file_id>[A-Za-z0-9_-]+)", h_task_draft_file_delete)
        r.add("GET", r"/api/tasks", h_tasks)
        r.add("POST", r"/api/tasks", h_task_create)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)", h_task_detail)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/questions", h_task_questions)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/session", h_task_session)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/evidence", h_task_evidence)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/agent-trace", h_agent_trace)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/recovery-runs", h_recovery_runs)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/recovery-runs", h_recovery_create)
        r.add("POST", r"/api/recovery-runs/(?P<run_id>[A-Za-z0-9_-]+)/observe", h_recovery_observe)
        r.add("POST", r"/api/recovery-runs/(?P<run_id>[A-Za-z0-9_-]+)/review", h_recovery_review)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/inputs", h_task_inputs)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/continue", h_task_continue)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/events", h_task_events)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/stream", h_task_stream)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/cancel", h_task_cancel)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/retry", h_task_retry)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/release", h_task_release)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/storage-jobs", h_task_storage_jobs)
        r.add("GET", r"/api/storage/overview", h_storage_overview)
        r.add("GET", r"/api/storage/jobs/(?P<job_id>[A-Za-z0-9_-]+)", h_storage_job)
        r.add("GET", r"/api/backups", h_backups)
        r.add("DELETE", r"/api/backups/(?P<name>[A-Za-z0-9._-]+)", h_backup_delete)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/report", h_task_report)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/review", h_task_review)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/assign-reviewer", h_task_assign_reviewer)
        r.add("GET", r"/api/quality", h_quality)
        r.add("GET", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/skills/(?P<skill>[A-Za-z0-9_-]+)/report",
              h_skill_report)

        r.add("GET", r"/api/skills", h_skills)
        r.add("GET", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)", h_skill_detail)
        r.add("POST", r"/api/skills/scan", h_skill_scan)
        r.add("POST", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)/disable", h_skill_disable)
        r.add("POST", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)/enable", h_skill_enable)
        r.add("POST", r"/api/skills/import", h_skill_import)
        r.add("GET", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)/tool", h_skill_tool)
        r.add("GET", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)/manifest", h_skill_manifest)
        r.add("POST", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)/manifest/sign",
              h_skill_manifest_sign)
        r.add("GET", r"/api/tool-calls", h_tool_calls)
        r.add("GET", r"/api/traces", h_traces)
        r.add("GET", r"/api/traces/(?P<run_id>[Tt][Rr][A-Za-z0-9_-]+)/summary", h_trace_summary)
        r.add("GET", r"/api/traces/(?P<run_id>[Tt][Rr][A-Za-z0-9_-]+)/events", h_trace_events)
        r.add("GET", r"/api/traces/(?P<run_id>[Tt][Rr][A-Za-z0-9_-]+)/download", h_trace_download)
        r.add("GET", r"/api/tool-approvals", h_tool_approvals)
        r.add("POST", r"/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/tool-approvals",
              h_tool_approval_create)
        r.add("POST", r"/api/tool-approvals/(?P<approval_id>[A-Za-z0-9_-]+)/decide",
              h_tool_approval_decide)
        r.add("GET", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)/export", h_skill_export)
        r.add("POST", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)/rollback", h_skill_rollback)
        r.add("POST", r"/api/skills/(?P<name>[A-Za-z0-9_-]+)/channel", h_skill_channel)

        r.add("GET", r"/api/workshop/recipes", h_workshop_recipes)
        r.add("GET", r"/api/workshop/drafts", h_workshop_list)
        r.add("GET", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/provenance", h_workshop_provenance)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/provenance/lock", h_workshop_provenance_lock)
        r.add("POST", r"/api/workshop/drafts", h_workshop_create)
        r.add("GET", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)", h_workshop_detail)
        r.add("PUT", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)", h_workshop_update)
        r.add("DELETE", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)", h_workshop_delete)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/logs", h_workshop_logs)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/analyze", h_workshop_analyze)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/refine", h_workshop_refine)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/check", h_workshop_check)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/rules/try", h_workshop_rules_try)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/verify-rules", h_workshop_verify_rules)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/build", h_workshop_build)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/publish", h_workshop_publish)
        r.add("POST", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/case", h_workshop_case)
        r.add("GET", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/cases", h_workshop_cases)
        r.add("GET", r"/api/workshop/jobs/(?P<job_id>[A-Za-z0-9_-]+)", h_workshop_job)
        r.add("GET", r"/api/workshop/drafts/(?P<draft_id>[A-Za-z0-9_-]+)/jobs", h_workshop_jobs)
        r.add("GET", r"/api/calibration", h_calibration)
        r.add("GET", r"/api/case-matrix", h_case_matrix)
        r.add("POST", r"/api/case-matrix/rebuild", h_case_matrix_rebuild)
        r.add("GET", r"/api/experience-candidates", h_experience_candidates)
        r.add("POST", r"/api/experience-candidates/(?P<candidate_id>[A-Za-z0-9_-]+)/decide", h_candidate_decide)
        r.add("POST", r"/api/calibration/fit", h_calibration_fit)
        r.add("GET", r"/api/skill-versions", h_versions)

        r.add("GET", r"/api/models", h_models)
        r.add("POST", r"/api/models", h_model_create)
        r.add("PUT", r"/api/models/(?P<model_id>[0-9]+)", h_model_update)
        r.add("DELETE", r"/api/models/(?P<model_id>[0-9]+)", h_model_delete)
        r.add("POST", r"/api/models/discover", h_model_discover)
        r.add("POST", r"/api/models/test", h_model_test_draft)
        r.add("POST", r"/api/models/(?P<model_id>[0-9]+)/test", h_model_test)
        r.add("POST", r"/api/models/(?P<model_id>[0-9]+)/capabilities", h_model_capabilities)

        r.add("GET", r"/api/users", h_users)
        r.add("GET", r"/api/users/invites", h_invites)
        r.add("POST", r"/api/users/invites", h_invite_create)
        r.add("DELETE", r"/api/users/invites/(?P<invite_id>[A-Za-z0-9_-]+)", h_invite_revoke)
        r.add("POST", r"/api/users/batch-disable", h_users_batch_disable)
        r.add("GET", r"/api/users/(?P<user_id>[0-9]+)", h_user_detail)
        r.add("POST", r"/api/users/(?P<user_id>[0-9]+)/force-logout", h_user_force_logout)
        r.add("POST", r"/api/users", h_user_create)
        r.add("PUT", r"/api/users/(?P<user_id>[0-9]+)", h_user_update)
        r.add("GET", r"/api/profile", h_profile)
        r.add("PUT", r"/api/profile", h_profile_update)
        r.add("GET", r"/api/profile/model-key", h_profile_model_key)
        r.add("PUT", r"/api/profile/model-key", h_profile_model_key_update)
        r.add("DELETE", r"/api/profile/model-key", h_profile_model_key_delete)
        r.add("POST", r"/api/profile/model-key/test", h_profile_model_key_test)
        r.add("GET", r"/api/audit", h_audit)
        r.add("GET", r"/api/audit/export", h_audit_export)
        r.add("GET", r"/api/system/info", h_system_info)
        r.add("GET", r"/api/system/runtime-config", h_runtime_config)
        r.add("PUT", r"/api/system/runtime-config", h_runtime_config_save)
        r.add("POST", r"/api/system/restart", h_system_restart)
        r.add("POST", r"/api/system/updates/upload", h_update_upload)
        r.add("GET", r"/api/system/updates", h_update_list)
        r.add("GET", r"/api/system/updates/(?P<update_id>[Uu][A-Za-z0-9_-]+)", h_update_detail)
        r.add("POST", r"/api/system/updates/(?P<update_id>[Uu][A-Za-z0-9_-]+)/verify",
              h_update_verify)
        r.add("POST", r"/api/system/updates/(?P<update_id>[Uu][A-Za-z0-9_-]+)/apply",
              h_update_apply)
        r.add("POST", r"/api/system/updates/rollback", h_update_rollback)

        r.add("GET", r"/api/system/migrations", h_migrations)
        r.add("POST", r"/api/system/migrations/workspace/preflight", h_migration_preflight)
        r.add("POST", r"/api/system/migrations/workspace", h_migration_workspace)
        r.add("POST", r"/api/system/migrations/export", h_migration_export)
        r.add("POST", r"/api/system/migrations/import", h_migration_import)
        r.add("GET", r"/api/system/migrations/(?P<migration_id>[Uu][A-Za-z0-9_-]+)",
              h_migration_detail)
        r.add("GET", r"/api/system/migrations/(?P<migration_id>[Uu][A-Za-z0-9_-]+)/download",
              h_migration_download)
        r.add("DELETE", r"/api/system/migrations/(?P<migration_id>[Uu][A-Za-z0-9_-]+)",
              h_migration_delete)


# ---------------------------- 基础接口 ----------------------------
def h_health(app: App, req):
    return response_json({"ok": True, "version": __version__})


def h_public_config(app: App, req):
    return response_json({"brand": app.config.get("brand"), "version": __version__,
                          "max_upload_mb": int(app.config.get("max_upload_mb", 2048)),
                          "password_min_length": app.config.password_min_length,
                          "session_warn_sec": int(app.config.get("session_warn_sec", 120)),
                          "allow_engineer_workshop": bool(app.config.get("allow_engineer_workshop", True)),
                          "allow_self_register": bool(app.config.get("allow_self_register", False)),
                          "assistant_enabled": bool(app.config.get("assistant_enabled", True)),
                          "mcp_endpoint": "/mcp",
                          "personal_model_base_url": app.config.get("personal_model_base_url"),
                          "personal_model_default": app.config.get("personal_model_default"),
                          "personal_model_models": app.config.get("personal_model_models") or [],
                          "demo_installed": app.db.setting("demo_installed") == "1"})


def h_login(app: App, req):
    req.ip = _client_ip(app, req)
    data = req.json()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        raise HTTPError(400, "请输入用户名和密码")
    _check_login_rate(app, req.ip, username)
    row = app.db.one("SELECT * FROM users WHERE username=?", (username,))
    if not row or not row["active"] or not verify_password(password, row["password_hash"]):
        _record_login_failure(app, req.ip, username)
        app.db.audit(row["id"] if row else None, username, "auth.login_failed", username, {}, req.ip)
        raise HTTPError(401, "用户名或密码错误")
    _clear_login_failures(req.ip, username)
    cleanup_sessions(app.db)
    session = create_session(app.db, row["id"], int(app.config.get("session_hours", 12)),
                             ip=req.ip, ua=req.headers.get("User-Agent", ""))
    app.db.execute("UPDATE users SET last_login_at=? WHERE id=?", (iso_now(), row["id"]))
    app.db.audit(row["id"], row["username"], "auth.login", username,
                 {"ua": str(req.headers.get("User-Agent") or "")[:200]}, req.ip)
    user = {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "role": row["role"], "must_change": row["must_change"]}
    max_age = int(app.config.get("session_hours", 12)) * 3600
    return response_json({"user": user, "csrf": session["csrf"], "expires_at": session["expires_at"]},
                         headers={"Set-Cookie": _set_cookie(
                             session["token"], max_age, app.config.cookie_secure)})


def h_logout(app: App, req):
    token = _cookie(req, SESSION_COOKIE)
    if token:
        delete_session(app.db, token)
    return response_json({"ok": True},
                         headers={"Set-Cookie": _set_cookie(
                             "", 0, app.config.cookie_secure)})


def h_me(app: App, req):
    user = _auth(app, req, csrf=False, allow_password_change=True)
    return response_json({"user": user, "csrf": req.session["csrf"],
                          "session_expires": req.session["expires_at"]})


def h_change_password(app: App, req):
    user = _auth(app, req, allow_password_change=True)
    data = req.json()
    old = str(data.get("old_password") or "")
    new = str(data.get("new_password") or "")
    try:
        validate_password(new, user["username"], app.config.password_min_length)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    row = app.db.one("SELECT password_hash FROM users WHERE id=?", (user["id"],))
    if not verify_password(old, row["password_hash"]):
        raise HTTPError(400, "原密码不正确")
    if verify_password(new, row["password_hash"]):
        raise HTTPError(400, "新密码不能与原密码相同")
    app.db.execute("UPDATE users SET password_hash=?, must_change=0 WHERE id=?",
                   (hash_password(new), user["id"]))
    revoked = delete_other_sessions(app.db, user["id"], req.session["token"])
    csrf = rotate_session_csrf(app.db, req.session["token"])
    app.db.audit(user["id"], user["username"], "auth.password_change", user["username"],
                 {"other_sessions_revoked": revoked}, req.ip)
    removed_file = False
    if user["username"] == "admin":
        path = app.config.data_dir / "初始管理员密码.txt"
        if path.exists():
            try:
                path.unlink()
                removed_file = True
            except OSError:
                pass
    return response_json({"ok": True, "csrf": csrf,
                          "other_sessions_revoked": revoked,
                          "bootstrap_password_file_removed": removed_file})


def h_session_refresh(app: App, req):
    user = _auth(app, req)
    expires = extend_session(app.db, req.session["token"],
                             int(app.config.get("session_hours", 12)))
    app.db.audit(user["id"], user["username"], "auth.session_refresh",
                 "self", {}, req.ip)
    return response_json({"ok": True, "expires_at": expires, "csrf": req.session["csrf"]})


def h_sessions(app: App, req):
    user = _auth(app, req, csrf=False)
    current = session_public_id(req.session["token"])
    items = []
    for item in list_user_sessions(app.db, user["id"]):
        items.append({**item, "current": item["id"] == current})
    return response_json({"items": items})


def h_revoke_other_sessions(app: App, req):
    user = _auth(app, req)
    revoked = delete_other_sessions(app.db, user["id"], req.session["token"])
    app.db.audit(user["id"], user["username"], "auth.sessions_revoke_others",
                 user["username"], {"revoked": revoked}, req.ip)
    return response_json({"ok": True, "revoked": revoked})


def h_revoke_session(app: App, req, session_id: str):
    user = _auth(app, req)
    if session_id == session_public_id(req.session["token"]):
        raise HTTPError(400, "不能撤销当前会话，请使用退出登录")
    if not revoke_user_session(app.db, user["id"], session_id):
        raise HTTPError(404, "会话不存在或已过期")
    app.db.audit(user["id"], user["username"], "auth.session_revoke", session_id, {}, req.ip)
    return response_json({"ok": True})



# ---------------------------- 任务 ----------------------------
def h_tasks(app: App, req):
    user = _auth(app, req, csrf=False)
    page = max(1, int(req.query.get("page", 1)))
    size = min(100, max(1, int(req.query.get("page_size", 20))))
    clauses = []
    args: list[Any] = []
    if user["role"] == "reviewer":
        clauses.append("(user_id=? OR reviewer_id=?)")
        args.extend([user["id"], user["id"]])
    elif user["role"] != "admin":
        clauses.append("user_id=?")
        args.append(user["id"])
    status = req.query.get("status")
    if status in ("queued", "running", "done", "failed", "cancelled", "needs_data"):
        clauses.append("status=?")
        args.append(status)
    review_status = req.query.get("review_status")
    if user["role"] == "reviewer" and review_status == "pending":
        clauses.append("reviewer_id=? AND status IN ('done','needs_data') "
                       "AND id NOT IN (SELECT task_id FROM task_reviews)")
        args.append(user["id"])
    q = (req.query.get("q") or "").strip()
    if q:
        clauses.append("(title LIKE ? OR description LIKE ? OR id LIKE ?)")
        args.extend([f"%{q}%"] * 3)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = app.db.one(f"SELECT COUNT(*) AS n FROM tasks {where}", tuple(args))["n"]
    rows = app.db.all(
        f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(args + [size, (page - 1) * size]))
    return response_json({"items": [_task_summary(r) for r in rows], "total": total,
                          "page": page, "page_size": size})


def h_global_search(app: App, req):
    user = _auth(app, req, csrf=False)
    q = (req.query.get("q") or "").strip()
    if len(q) < 2:
        return response_json({"q": q, "tasks": [], "cases": [], "skills": [], "users": []})
    like = f"%{q}%"
    task_clauses, task_args = [], [like, like]
    if user["role"] == "reviewer":
        task_clauses.append("(user_id=? OR reviewer_id=?)")
        task_args.extend([user["id"], user["id"]])
    elif user["role"] != "admin":
        task_clauses.append("user_id=?")
        task_args.append(user["id"])
    task_where = (" AND " + " AND ".join(task_clauses)) if task_clauses else ""
    tasks = app.db.all(
        f"SELECT id,title,status,created_at FROM tasks WHERE (title LIKE ? OR id LIKE ?){task_where} "
        "ORDER BY created_at DESC LIMIT 6", tuple(task_args))
    cases = []
    if True:
        case_where = "" if user["role"] == "admin" else " AND (created_by='' OR created_by=?)"
        case_args: list[Any] = [like, like, like]
        if user["role"] != "admin":
            case_args.append(user["username"])
        cases = app.db.all(
            f"SELECT id,name,title,status,split FROM real_cases WHERE (name LIKE ? OR title LIKE ? OR id LIKE ?){case_where} "
            "ORDER BY updated_at DESC LIMIT 6", tuple(case_args))
    skills = []
    if True:
        skills = app.db.all(
            "SELECT name,title,status,version FROM skills WHERE name LIKE ? OR title LIKE ? "
            "ORDER BY name LIMIT 6", (like, like))
    users = []
    if user["role"] == "admin":
        users = app.db.all(
            "SELECT id,username,display_name,role,status FROM users WHERE username LIKE ? "
            "OR display_name LIKE ? ORDER BY id DESC LIMIT 6", (like, like))
    return response_json({
        "q": q,
        "tasks": [{"id": r["id"], "title": r["title"], "status": r["status"],
                   "link": f"/#/tasks/{r['id']}"} for r in tasks],
        "cases": [{"id": r["id"], "name": r["name"], "title": r["title"],
                   "status": r["status"], "split": r["split"]} for r in cases],
        "skills": [{"name": r["name"], "title": r["title"], "status": r["status"],
                    "version": r["version"], "link": "/#/skills"} for r in skills],
        "users": [{"id": r["id"], "username": r["username"], "display_name": r["display_name"],
                   "role": r["role"], "status": r["status"], "link": "/#/users"} for r in users],
    })


def h_notifications(app: App, req):
    user = _auth(app, req, csrf=False)
    unread = req.query.get("unread") in ("1", "true")
    limit = min(50, max(1, int(req.query.get("limit", 20))))
    where = "user_id=? AND read_at IS NULL" if unread else "user_id=?"
    rows = app.db.all(
        f"SELECT * FROM notifications WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        (user["id"], limit))
    unread_count = app.db.one(
        "SELECT COUNT(*) AS n FROM notifications WHERE user_id=? AND read_at IS NULL",
        (user["id"],))["n"]
    return response_json({"items": [notification_public(r) for r in rows],
                          "unread": unread_count})


def h_notification_read(app: App, req, note_id: str):
    user = _auth(app, req)
    row = app.db.one("SELECT * FROM notifications WHERE id=? AND user_id=?", (note_id, user["id"]))
    if not row:
        raise HTTPError(404, "通知不存在")
    app.db.execute("UPDATE notifications SET read_at=COALESCE(read_at, ?) WHERE id=?",
                   (iso_now(), note_id))
    return response_json({"ok": True, "id": note_id})


def h_notifications_read_all(app: App, req):
    user = _auth(app, req)
    app.db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                   (iso_now(), user["id"]))
    return response_json({"ok": True})


def _task_draft_for(app: App, user: dict, draft_id: str) -> dict:
    row = app.db.one("SELECT * FROM task_drafts WHERE id=?", (draft_id,))
    if not row or row["user_id"] != user["id"]:
        raise HTTPError(404, "草稿不存在或不属于当前用户")
    return row


def _draft_files(row: dict) -> list[dict]:
    return _loads(row.get("files_json"), [])


def _draft_public(row: dict) -> dict:
    return {
        "id": row["id"], "title": row["title"], "description": row["description"],
        "mode": row["mode"], "skill_name": row.get("skill_name") or "",
        "model_override": row.get("model_override"),
        "include_canary": bool(row.get("include_canary")),
        "files": [{k: f.get(k) for k in ("id", "name", "size", "sha256")}
                  for f in _draft_files(row)],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _draft_edit_user(app: App, req) -> dict:
    return _auth(app, req)


def h_task_draft_current(app: App, req):
    user = _draft_edit_user(app, req)
    row = app.db.one("SELECT * FROM task_drafts WHERE user_id=? ORDER BY updated_at DESC, id DESC LIMIT 1",
                     (user["id"],))
    return response_json({"draft": _draft_public(row) if row else None})


def _draft_payload(app: App, req) -> dict:
    data = req.json()
    title = str(data.get("title") or "").strip()[:120]
    description = str(data.get("description") or "").strip()
    if len(description) > 20000:
        raise HTTPError(400, "故障描述过长（上限 20000 字）")
    mode = data.get("mode") if data.get("mode") in ("fast", "deep", "skill") else "fast"
    skill_name = str(data.get("skill_name") or "").strip()
    if mode != "skill":
        skill_name = ""
    elif skill_name:
        available = {r["name"] for r in app.db.all(
            "SELECT name FROM skills WHERE status IN ('ready','degraded')")}
        if skill_name not in available:
            raise HTTPError(400, "指定技能不存在或不可用")
    model_override = data.get("model_override")
    if model_override in ("", None):
        model_override = None
    else:
        try:
            model_override = int(model_override)
        except (TypeError, ValueError) as exc:
            raise HTTPError(400, "model_override 必须是整数") from exc
    include_canary = bool(data.get("include_canary")) and app and True
    return {"title": title, "description": description, "mode": mode,
            "skill_name": skill_name, "model_override": model_override,
            "include_canary": 1 if include_canary else 0}


def h_task_draft_create(app: App, req):
    user = _draft_edit_user(app, req)
    payload = _draft_payload(app, req)
    draft_id = new_id("D")
    now = iso_now()
    app.db.execute(
        "INSERT INTO task_drafts(id,user_id,title,description,mode,skill_name,model_override,"
        "include_canary,files_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (draft_id, user["id"], payload["title"], payload["description"], payload["mode"],
         payload["skill_name"], payload["model_override"],
         1 if user["role"] == "admin" and payload["include_canary"] else 0,
         "[]", now, now))
    app.db.audit(user["id"], user["username"], "task.draft_create", draft_id, {}, req.ip)
    row = app.db.one("SELECT * FROM task_drafts WHERE id=?", (draft_id,))
    return response_json({"draft": _draft_public(row)}, status=201)


def h_task_draft_update(app: App, req, draft_id: str):
    user = _draft_edit_user(app, req)
    row = _task_draft_for(app, user, draft_id)
    payload = _draft_payload(app, req)
    app.db.execute(
        "UPDATE task_drafts SET title=?,description=?,mode=?,skill_name=?,model_override=?,"
        "include_canary=?,updated_at=? WHERE id=?",
        (payload["title"], payload["description"], payload["mode"], payload["skill_name"],
         payload["model_override"],
         1 if user["role"] == "admin" and payload["include_canary"] else 0,
         iso_now(), row["id"]))
    row = app.db.one("SELECT * FROM task_drafts WHERE id=?", (row["id"],))
    return response_json({"draft": _draft_public(row)})


def h_task_draft_delete(app: App, req, draft_id: str):
    user = _draft_edit_user(app, req)
    row = _task_draft_for(app, user, draft_id)
    shutil.rmtree(app.config.data_dir / "draft_uploads" / row["id"], ignore_errors=True)
    app.db.execute("DELETE FROM task_drafts WHERE id=?", (row["id"],))
    app.db.audit(user["id"], user["username"], "task.draft_delete", row["id"], {}, req.ip)
    return response_json({"ok": True, "draft_id": row["id"]})


def h_task_draft_file(app: App, req, draft_id: str):
    user = _draft_edit_user(app, req)
    row = _task_draft_for(app, user, draft_id)
    files = _draft_files(row)
    if len(files) >= 20:
        raise HTTPError(400, "单个草稿最多上传 20 个日志包")
    max_mb = int(app.config.get("max_upload_mb", 2048))
    fields, uploads = req.parse_multipart(max_mb * 1024 * 1024, max_file_bytes=max_mb * 1024 * 1024)
    if not uploads:
        raise HTTPError(400, "请选择要上传的日志包")
    item = uploads[0]
    name = safe_filename(item.filename)
    if not re.search(r"\.(zip|tar|tar\.gz|tgz|7z|gz|bz2|xz|zst)$", name, re.IGNORECASE):
        raise HTTPError(400, "仅支持 zip / tar / tar.gz / tgz / 7z / gz / bz2 / xz / zst 日志包")
    if any(f.get("name") == item.filename and f.get("size") == item.size for f in files):
        for other in uploads[1:]:
            try: os.unlink(other.path)
            except OSError: pass
        return response_json({"file": next(f for f in files if f.get("name") == item.filename and f.get("size") == item.size),
                              "draft": _draft_public(row), "duplicate": True}, status=200)
    target_dir = app.config.data_dir / "draft_uploads" / row["id"]
    target_dir.mkdir(parents=True, exist_ok=True)
    file_id = new_id("F")
    target = unique_path(target_dir, f"{file_id}-{name}")
    os.replace(item.path, target)
    entry = {"id": file_id, "name": item.filename, "size": item.size,
             "sha256": sha256_file(target), "path": str(target)}
    files.append(entry)
    app.db.execute("UPDATE task_drafts SET files_json=?, updated_at=? WHERE id=?",
                   (json.dumps(files, ensure_ascii=False), iso_now(), row["id"]))
    app.db.audit(user["id"], user["username"], "task.draft_file", row["id"],
                 {"file": item.filename, "size": item.size}, req.ip)
    row = app.db.one("SELECT * FROM task_drafts WHERE id=?", (row["id"],))
    return response_json({"file": {k: entry[k] for k in ("id", "name", "size", "sha256")},
                          "draft": _draft_public(row)}, status=201)


def h_task_draft_file_delete(app: App, req, draft_id: str, file_id: str):
    user = _draft_edit_user(app, req)
    row = _task_draft_for(app, user, draft_id)
    files = _draft_files(row)
    keep, removed = [], None
    for f in files:
        if f.get("id") == file_id:
            removed = f
        else:
            keep.append(f)
    if not removed:
        raise HTTPError(404, "草稿文件不存在")
    path = Path(str(removed.get("path") or ""))
    try:
        if path.is_file() and within_directory(app.config.data_dir / "draft_uploads", path):
            path.unlink()
    except OSError:
        pass
    app.db.execute("UPDATE task_drafts SET files_json=?, updated_at=? WHERE id=?",
                   (json.dumps(keep, ensure_ascii=False), iso_now(), row["id"]))
    return response_json({"ok": True, "file_id": file_id})


def h_task_create(app: App, req):
    user = _auth(app, req)
    max_mb = int(app.config.get("max_upload_mb", 2048))
    fields, files = req.parse_multipart(max_mb * 1024 * 1024, max_file_bytes=max_mb * 1024 * 1024)
    description = (fields.get("description") or "").strip()
    if not description:
        raise HTTPError(400, "请填写故障描述")
    if len(description) > 20000:
        raise HTTPError(400, "故障描述过长（上限 20000 字）")
    supplied_title = (fields.get("title") or "").strip()
    title = supplied_title or truncate(description.splitlines()[0], 60)
    title_source = "manual" if supplied_title else "deterministic"
    mode = fields.get("mode") if fields.get("mode") in ("fast", "deep", "skill") else "fast"
    include_canary = fields.get("include_canary") in ("1", "true", "on") and user["role"] == "admin"
    skill_name = str(fields.get("skill_name") or "").strip()
    if mode == "skill":
        if not skill_name:
            raise HTTPError(400, "指定技能模式必须选择一个技能")
        available = {r["name"] for r in app.db.all(
            "SELECT name FROM skills WHERE status IN ('ready','degraded')")}
        if skill_name not in available:
            raise HTTPError(400, "指定技能不存在或不可用：" + skill_name)
        selected_skills = [skill_name]
    else:
        skill_name = ""
        selected_skills = []
    model_override = None
    if fields.get("model_id"):
        try:
            model_override = int(fields["model_id"])
        except ValueError:
            model_override = None
    stats = app.worker.stats()
    if stats["queued"] >= stats.get("max_queue", 32):
        raise HTTPError(429, f"任务队列已满（{stats['queued']}/{stats.get('max_queue')}），请稍后提交")
    draft_row = None
    draft_files: list[dict] = []
    if fields.get("draft_id"):
        draft_row = _task_draft_for(app, user, str(fields["draft_id"]))
        draft_files = _draft_files(draft_row)
    if not files and not draft_files:
        raise HTTPError(400, "请至少上传一个日志包")
    task_id = new_id("T")
    upload_dir = app.config.data_dir / "uploads" / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    inputs = []
    for item in files:
        target = unique_path(upload_dir, safe_filename(item.filename))
        os.replace(item.path, target)
        inputs.append({"name": item.filename, "stored_name": target.name,
                       "stored_path": str(target), "size": item.size,
                       "sha256": sha256_file(target)})
    for item in draft_files:
        src = Path(str(item.get("path") or ""))
        draft_root = app.config.data_dir / "draft_uploads"
        if not src.is_file() or not within_directory(draft_root, src):
            raise HTTPError(400, "草稿中的日志包已丢失，请删除后重新上传")
        target = unique_path(upload_dir, safe_filename(str(item.get("name") or src.name)))
        shutil.copy2(src, target)
        inputs.append({"name": item.get("name") or target.name, "stored_name": target.name,
                       "stored_path": str(target), "size": target.stat().st_size,
                       "sha256": sha256_file(target)})
    now = iso_now()
    app.db.execute(
        "INSERT INTO tasks(id,user_id,title,description,title_source,status,stage,progress,mode,model_override,"
        "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, user["id"], title, description, title_source, "queued", "queued", 0, mode,
         model_override, json.dumps(inputs, ensure_ascii=False),
         json.dumps({"mode": mode, "model_override": model_override,
                     "include_canary": include_canary, "skill_name": skill_name,
                     "skill_names": selected_skills}, ensure_ascii=False), now))
    app.worker.submit(task_id)
    if draft_row:
        shutil.rmtree(app.config.data_dir / "draft_uploads" / draft_row["id"], ignore_errors=True)
        app.db.execute("DELETE FROM task_drafts WHERE id=?", (draft_row["id"],))
    app.db.audit(user["id"], user["username"], "task.create", task_id,
                 {"files": len(inputs), "mode": mode,
                  "from_draft": bool(draft_row)}, req.ip)
    row = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    return response_json({"task": _task_summary(row), "id": task_id}, status=201)


TASK_STAGES = [
    ("queued", "受理"), ("intake", "盘点"), ("planning", "AI 规划"),
    ("fanout", "技能执行"), ("replanning", "补规划"), ("arbitration", "仲裁"),
    ("reporting", "报告"), ("done", "完成"),
]


def _stage_seconds(start: str | None, end: str | None) -> float | None:
    if not start:
        return None
    try:
        a = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        b = (datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
             if end else datetime.now(timezone.utc))
        return max(0, (b - a).total_seconds())
    except ValueError:
        return None


def _task_stage_timeline(app: App, task: dict) -> list[dict]:
    rows = {r["stage"]: dict(r) for r in app.db.all(
        "SELECT stage,MIN(ts) AS started,MAX(ts) AS ended,COUNT(*) AS events "
        "FROM task_events WHERE task_id=? AND stage<>'' GROUP BY stage", (task["id"],))}
    active = task.get("stage") or "queued"
    status = task.get("status") or "queued"
    active_index = next((i for i, (k, _) in enumerate(TASK_STAGES) if k == active), len(TASK_STAGES) - 1)
    timeline = []
    for i, (key, label) in enumerate(TASK_STAGES):
        row = rows.get(key)
        start = (row or {}).get("started")
        if key == "queued":
            start = start or task.get("created_at")
        end = (row or {}).get("ended")
        if key == active and status in ("queued", "running"):
            end = None
        if key == "done" and task.get("finished_at"):
            end = task.get("finished_at")
        if i > active_index and status not in ("done", "failed", "cancelled", "needs_data"):
            state = "pending"
        elif key == active and status in ("queued", "running"):
            state = "active"
        elif key == active and status in ("failed", "cancelled", "needs_data"):
            state = "failed"
        elif row:
            state = "done"
        else:
            state = "pending"
        timeline.append({
            "stage": key, "label": label, "status": state,
            "start": start, "end": end,
            "duration_sec": _stage_seconds(start, end) if state != "pending" else None,
            "events": int((row or {}).get("events") or 0),
        })
    return timeline


def _task_next_action(task: dict) -> str:
    status = task.get("status") or ""
    result = _loads(task.get("result_json"), {}) or {}
    verdict = result.get("verdict") or {}
    if status == "failed":
        return "查看执行时间线中的失败原因，修正日志包或模型配置后重跑；如持续失败，导出支持包。"
    if status == "needs_data":
        return "按诊断结论中的建议补充日志（时钟、告警、链路状态），然后重跑。"
    if status == "queued":
        return "任务已进入队列，无需重复提交；可离开页面稍后回来查看。"
    if status == "running":
        return "正在执行，可查看实时时间线；不要重复提交同一批日志。"
    if status == "done":
        if verdict.get("status") in ("insufficient", "needs_data", "unknown_fault"):
            return "补充独立日志或安排专家复核，确认后再作为经验沉淀。"
        if verdict.get("status") == "matched":
            return "可提交专家复核；确认后结论才进入经验库并参与后续规划。"
        return "查看报告与证据链；如现场已恢复，可归档任务。"
    return "查看任务时间线确认当前阶段。"


def _session_state_for_task(task: dict[str, Any]) -> str:
    status = task.get("status") or "intake"
    verdict = _loads(task.get("verdict_json"), {}) or {}
    if status == "done":
        if verdict.get("status") in ("matched", "multiple_causes", "temporal_conflict"):
            return "resolved"
        if verdict.get("status") in ("unknown_fault", "unmatched"):
            return "unknown_fault"
        return "closed"
    return {"queued": "queued", "running": "evaluating", "needs_data": "needs_data",
            "failed": "failed", "cancelled": "cancelled"}.get(status, "intake")


def _ensure_task_session(app: App, task: dict[str, Any]) -> DiagnosticSession:
    session = DiagnosticSession(app.db, task["id"])
    if session.revision == 0:
        session.append("session_created", {"state": _session_state_for_task(task)},
                       event_key="session-created")
    return session


def h_task_detail(app: App, req, task_id: str):
    task = _task_for(app, req, task_id)
    session = _ensure_task_session(app, task)
    snapshot = session.snapshot()
    latest_supplement = app.db.one(
        "SELECT * FROM task_supplements WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,))
    pending_supplement = None
    if latest_supplement and task["status"] == "needs_data" \
            and latest_supplement["revision"] == snapshot.get("revision"):
        pending_supplement = {
            "id": latest_supplement["id"], "revision": latest_supplement["revision"],
            "files_count": len(_loads(latest_supplement["files_json"], []) or []),
            "answers_count": len(_loads(latest_supplement["answers_json"], {}) or {}),
        }
    skills = app.db.all("SELECT * FROM task_skills WHERE task_id=? ORDER BY id", (task_id,))
    events = app.db.all(
        "SELECT seq,ts,level,stage,message,data_json FROM task_events WHERE task_id=? "
        "ORDER BY seq DESC LIMIT 60", (task_id,))
    events.reverse()
    detail = _task_summary(task)
    detail.update({
        "description": task["description"],
        "username": (app.db.one("SELECT username FROM users WHERE id=?",
                                (task["user_id"],)) or {}).get("username"),
        "reviewer_name": ((app.db.one("SELECT username FROM users WHERE id=?",
                                      (task["reviewer_id"],)) or {}).get("username")
                          if task.get("reviewer_id") else ""),
        "inputs": _loads(task["input_json"], []),
        "options": _loads(task["options_json"], {}),
        "result": _loads(task["result_json"], {}) or {},
        "stats": _loads(task["stats_json"], {}) or {},
        "review": ReviewService(app.db, app.config.data_dir).get(task["id"]),
        "has_report": bool(task.get("report_md")),
        "model_status": app.gateway.model_status(task["user_id"]),
        "stage_timeline": _task_stage_timeline(app, task),
        "next_action": _task_next_action(task),
        "continuation_count": int(task.get("continuation_count") or 0),
        "session": snapshot,
        "questions": session.questions(),
        "pending_supplement": pending_supplement,
        "evidence_count": app.evidence_store.count(task_id),
        "supplement_enabled": bool(app.config.get("session_continue_enabled", True)),
        "skills": [{
            "skill_name": s["skill_name"], "version": s["skill_version"],
            "status": s["status"], "score": s["score"], "error": s["error"],
            "verdict": _loads(s["verdict_json"], {}),
            "summary": _loads(s["summary_json"], {}),
            "result": _loads(s["result_json"], {}),
            "has_report": bool(s["report_md_path"]),
            "started_at": s["started_at"], "finished_at": s["finished_at"],
            "stats": _loads(s["stats_json"], {}),
        } for s in skills],
        "events": [{"seq": e["seq"], "ts": e["ts"], "level": e["level"],
                    "stage": e["stage"], "message": e["message"],
                    "data": _loads(e["data_json"], {})} for e in events],
    })
    return response_json(detail)


def h_task_questions(app: App, req, task_id: str):
    task = _task_for(app, req, task_id)
    session = _ensure_task_session(app, task)
    return response_json({
        "task_id": task_id, "revision": session.revision, "status": task["status"],
        "questions": session.questions(),
        "enabled": bool(app.config.get("session_continue_enabled", True)),
        "max_supplements": int(app.config.get("session_max_supplements", 3)),
    })


def h_task_session(app: App, req, task_id: str):
    task = _task_for(app, req, task_id)
    session = _ensure_task_session(app, task)
    snapshot = session.snapshot()
    snapshot.update({
        "status": task["status"], "can_continue": task["status"] == "needs_data"
        and bool(app.config.get("session_continue_enabled", True)),
        "questions": session.questions(),
        "events": session.events()[-80:],
    })
    return response_json(snapshot)


def h_task_evidence(app: App, req, task_id: str):
    _task_for(app, req, task_id)
    try:
        limit = max(1, min(1000, int(req.query.get("limit", 300))))
    except ValueError:
        limit = 300
    try:
        offset = max(0, int(req.query.get("offset", 0)))
    except ValueError:
        offset = 0
    items = app.evidence_store.list(task_id, limit, offset)
    return response_json({"items": items, "total": app.evidence_store.count(task_id),
                          "limit": limit, "offset": offset})


def _supplement_answers(fields: dict[str, str]) -> dict[str, str]:
    answers: dict[str, str] = {}
    raw = (fields.get("answers") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPError(400, "answers 必须是 JSON 对象") from exc
        if not isinstance(parsed, dict):
            raise HTTPError(400, "answers 必须是 JSON 对象")
        answers.update({str(k): str(v) for k, v in parsed.items()})
    for key, value in fields.items():
        if key.startswith("answer_"):
            answers[key[7:]] = value
    return {k: v.strip()[:4000] for k, v in answers.items() if str(v).strip()}


def _supplement_hash(files: list[dict[str, Any]], answers: dict[str, str]) -> str:
    payload = {"files": [{"name": f.get("name"), "size": f.get("size"),
                         "sha256": f.get("sha256")} for f in files],
               "answers": {k: answers[k] for k in sorted(answers)}}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _session_revision_conn(conn, task_id: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(revision),0) AS revision FROM session_events "
                       "WHERE task_id=?", (task_id,)).fetchone()
    return int(row["revision"] or 0)


def _input_members(stored: list[dict[str, Any]]) -> tuple[list[str], bool]:
    """把补充文件展开为可匹配路径；返回 (成员名, 是否全部可解析)。"""
    members: list[str] = []
    complete = True
    for item in stored:
        path = Path(str(item.get("stored_path") or ""))
        name = str(item.get("name") or item.get("stored_name") or path.name)
        members.append(name)
        members.append(path.name)
        try:
            if path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    members.extend(str(x).replace("\\", "/") for x in zf.namelist())
        except (OSError, zipfile.BadZipFile):
            complete = False
    return sorted(set(x for x in members if x)), complete


def _skill_globs(skill: Any) -> list[str]:
    globs: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "globs" and isinstance(value, list):
                    globs.extend(str(x) for x in value if str(x).strip())
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(getattr(skill, "profile", {}) or {})
    return sorted(set(globs))


def _glob_match(pattern: str, member: str) -> bool:
    pat = str(pattern or "").replace("\\", "/").lstrip("./")
    name = str(member or "").replace("\\", "/")
    variants = [pat]
    while variants[-1].startswith("**/"):
        variants.append(variants[-1][3:])
    base = name.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(base, p)
               for p in variants if p)


def _affected_skills(app: App, task_id: str,
                     stored: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    if not stored:
        return [], {}
    rows = app.db.all("SELECT skill_name FROM task_skills WHERE task_id=? ORDER BY id",
                      (task_id,))
    members, complete = _input_members(stored)
    affected: list[str] = []
    reasons: dict[str, Any] = {}
    for row in rows:
        name = str(row["skill_name"])
        skill = app.registry.get(name)
        if not skill:
            continue
        globs = _skill_globs(skill)
        if not globs:
            # 没有声明日志范围，无法证明无关；按保守策略重跑该技能。
            affected.append(name)
            reasons[name] = {"reason": "no_declared_globs"}
            continue
        matched = [m for m in members if any(_glob_match(p, m) for p in globs)]
        if matched:
            affected.append(name)
            reasons[name] = {"reason": "glob_match", "matched": matched[:8],
                             "members_complete": complete}
    return sorted(set(affected)), reasons


def h_task_inputs(app: App, req, task_id: str):
    task, user = _task_owner_for(app, req, task_id)
    if not app.config.get("session_continue_enabled", True):
        raise HTTPError(400, "同任务补证功能未启用")
    session = _ensure_task_session(app, task)
    ctype = (req.headers.get("Content-Type") or "").lower()
    files = []
    if "multipart/form-data" in ctype:
        max_mb = int(app.config.get("max_upload_mb", 2048))
        fields, files = req.parse_multipart(max_mb * 1024 * 1024,
                                            max_file_bytes=max_mb * 1024 * 1024)
        if len(files) > 10:
            raise HTTPError(400, "一次最多补充 10 个文件")
    elif "application/json" in ctype:
        data = req.json()
        if not isinstance(data, dict):
            raise HTTPError(400, "请求体必须是 JSON 对象")
        fields = {str(k): str(v) for k, v in data.items() if not isinstance(v, (dict, list))}
        raw_answers = data.get("answers")
        if isinstance(raw_answers, dict):
            fields["answers"] = json.dumps(raw_answers, ensure_ascii=False)
    else:
        raise HTTPError(400, "需要 multipart/form-data 或 application/json")
    answers = _supplement_answers(fields)
    if not files and not answers:
        raise HTTPError(400, "请至少补充一个日志文件或回答一个问题")
    if len(answers) > 12:
        raise HTTPError(400, "一次最多回答 12 个问题")
    try:
        expected = int(fields.get("expected_revision") or "")
    except ValueError as exc:
        raise HTTPError(400, "expected_revision 必须是整数") from exc
    idem = (req.headers.get("Idempotency-Key") or fields.get("idempotency_key") or "").strip()
    if not re.match(r"^[A-Za-z0-9_.:-]{8,128}$", idem):
        raise HTTPError(400, "缺少或非法的 Idempotency-Key（8-128 位字母数字/._:-）")
    temp_files = [{"name": item.filename, "size": item.size,
                   "sha256": sha256_file(item.path)} for item in files]
    request_hash = _supplement_hash(temp_files, answers)
    previous = app.db.one(
        "SELECT * FROM task_supplements WHERE task_id=? AND idempotency_key=?",
        (task_id, idem))
    if previous:
        if previous.get("request_hash") != request_hash:
            raise HTTPError(409, "同一 Idempotency-Key 已用于不同内容")
        return response_json(_supplement_replay(app, task, previous, answers), status=200)
    if task["status"] != "needs_data":
        raise HTTPError(409, "只有等待补充数据的任务可以提交补证")
    if expected != session.revision:
        raise HTTPError(409, f"会话版本冲突：当前 {session.revision}，提交基于 {expected}")
    current_round = int(session.snapshot().get("supplement_round") or 0)
    max_supplements = int(app.config.get("session_max_supplements", 3))
    if current_round >= max_supplements:
        raise HTTPError(429, f"最多补充 {max_supplements} 轮，请新建任务")

    supplement_id = new_id("S")
    target_dir = app.config.data_dir / "uploads" / task_id / "supplements" / supplement_id
    target_dir.mkdir(parents=True, exist_ok=True)
    stored: list[dict[str, Any]] = []
    try:
        for item in files:
            target = unique_path(target_dir, safe_filename(item.filename))
            os.replace(item.path, target)
            stored.append({"name": item.filename, "stored_name": target.name,
                           "stored_path": str(target), "size": target.stat().st_size,
                           "sha256": sha256_file(target)})
        if _supplement_hash(stored, answers) != request_hash:
            raise HTTPError(400, "补充文件在落盘时发生变化，请重新提交")
        old_inputs = _loads(task.get("input_json"), [])
        if not isinstance(old_inputs, list):
            old_inputs = []
        input_hash = hashlib.sha256(json.dumps(stored, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        invalidated, invalidation_reasons = _affected_skills(app, task_id, stored)
        gaps = session.questions()
        resolved = [g for g in gaps if str(g.get("question_id")) in answers]
        remaining = [g for g in gaps if str(g.get("question_id")) not in answers]
        answer_text = "；".join(f"{k}：{v}" for k, v in answers.items())[:3000]
        file_text = "、".join(f["name"] for f in stored)[:1000]
        block = f"\n\n【补充 {current_round + 1}】"
        if stored:
            block += f"\n新增日志：{file_text}"
        if answer_text:
            block += f"\n现场回答：{answer_text}"
        new_description = (task["description"] + block)[:20000]
        revision = expected + 1
        with app.db.tx() as conn:
            latest = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not latest or latest["status"] != "needs_data":
                raise RevisionConflict("任务状态已变化，不能提交补证")
            current = _session_revision_conn(conn, task_id)
            if current != expected:
                raise RevisionConflict(f"会话版本冲突：当前 {current}，提交基于 {expected}")
            existing = conn.execute(
                "SELECT * FROM task_supplements WHERE task_id=? AND idempotency_key=?",
                (task_id, idem)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise RevisionConflict("同一 Idempotency-Key 已用于不同内容")
                replay = dict(existing)
            else:
                replay = None
                conn.execute(
                    "INSERT INTO task_supplements(task_id,revision,idempotency_key,request_hash,"
                    "files_json,answers_json,invalidated_skills_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (task_id, revision, idem, request_hash,
                     json.dumps(stored, ensure_ascii=False), json.dumps(answers, ensure_ascii=False),
                     json.dumps(invalidated, ensure_ascii=False), iso_now()))
                payload = {"revision": revision, "input_hash": request_hash, "files": [
                    {"name": f["name"], "size": f["size"], "sha256": f["sha256"]} for f in stored],
                    "answers": answers, "invalidated_skills": invalidated,
                    "invalidation_reasons": invalidation_reasons,
                    "resolved_gaps": resolved, "remaining_gaps": remaining}
                conn.execute(
                    "INSERT INTO session_events(task_id,revision,event_type,payload_json,event_key,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (task_id, revision, "input_added",
                     json.dumps(payload, ensure_ascii=False, default=str), f"input-{supplement_id}",
                     iso_now()))
                if invalidated:
                    marks = ",".join("?" for _ in invalidated)
                    conn.execute(f"UPDATE task_skills SET invalidated=1 WHERE task_id=? "
                                 f"AND skill_name IN ({marks})",
                                 (task_id, *invalidated))
                conn.execute("UPDATE tasks SET input_json=?, description=? WHERE id=?",
                             (json.dumps(old_inputs + stored, ensure_ascii=False), new_description, task_id))
            if replay is not None:
                raise RevisionConflict("并发提交命中同一幂等键")
        if replay is not None:
            return response_json(_supplement_replay(app, task, replay, answers), status=200)
        new_rev = revision
        app.db.audit(user["id"], user["username"], "task.supplement", task_id,
                     {"revision": revision, "files": len(stored), "answers": len(answers),
                      "invalidated": len(invalidated), "idempotency_key": idem}, req.ip)
        return response_json({
            "ok": True, "task_id": task_id, "revision": new_rev, "duplicate": False,
            "supplement": {"id": supplement_id, "revision": new_rev,
                           "files": [{"name": f["name"], "size": f["size"]} for f in stored],
                           "answers": answers, "invalidated_skills": invalidated,
                           "invalidation_reasons": invalidation_reasons},
            "resolved_gaps": resolved, "remaining_gaps": remaining,
        }, status=201)
    except HTTPError:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    except RevisionConflict as exc:
        existing = app.db.one(
            "SELECT * FROM task_supplements WHERE task_id=? AND idempotency_key=?",
            (task_id, idem))
        if existing and existing.get("request_hash") != request_hash:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise HTTPError(409, "同一 Idempotency-Key 已用于不同内容") from exc
        if existing:
            shutil.rmtree(target_dir, ignore_errors=True)
            return response_json(_supplement_replay(app, task, existing, answers), status=200)
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPError(409, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        existing = app.db.one(
            "SELECT * FROM task_supplements WHERE task_id=? AND idempotency_key=?",
            (task_id, idem))
        shutil.rmtree(target_dir, ignore_errors=True)
        if existing and existing.get("request_hash") == request_hash:
            return response_json(_supplement_replay(app, task, existing, answers), status=200)
        if existing:
            raise HTTPError(409, "同一 Idempotency-Key 已用于不同内容") from exc
        raise HTTPError(409, "补充数据版本冲突，请刷新后重试") from exc
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


def _supplement_replay(app: App, task: dict[str, Any], row: dict[str, Any],
                       answers: dict[str, str]) -> dict[str, Any]:
    files = _loads(row.get("files_json"), []) or []
    invalidated = _loads(row.get("invalidated_skills_json"), []) or []
    return {
        "ok": True, "duplicate": True, "task_id": task["id"],
        "revision": row.get("revision"), "supplement": {
            "id": row.get("id"), "revision": row.get("revision"),
            "files": [{"name": f.get("name"), "size": f.get("size")} for f in files],
            "answers": answers, "invalidated_skills": invalidated},
        "status": task["status"],
    }


def h_task_continue(app: App, req, task_id: str):
    task, user = _task_owner_for(app, req, task_id)
    if not app.config.get("session_continue_enabled", True):
        raise HTTPError(400, "同任务续跑功能未启用")
    data = req.json() if req.headers.get("Content-Length") else {}
    if not isinstance(data, dict):
        raise HTTPError(400, "请求体必须是 JSON 对象")
    try:
        expected = int(data.get("expected_revision"))
    except (TypeError, ValueError) as exc:
        raise HTTPError(400, "expected_revision 必须是整数") from exc
    session = _ensure_task_session(app, task)
    event = session.event(f"continue-{expected}")
    if event:
        return response_json({"ok": True, "duplicate": True, "task": _task_summary(task),
                              "revision": event["revision"]}, status=200)
    if task["status"] != "needs_data":
        raise HTTPError(409, "任务不在待补证状态，无法续跑")
    if session.revision != expected:
        raise HTTPError(409, f"会话版本冲突：当前 {session.revision}，提交基于 {expected}")
    supplement = app.db.one(
        "SELECT * FROM task_supplements WHERE task_id=? AND revision=?",
        (task_id, expected))
    if not supplement:
        raise HTTPError(400, "没有待续跑的补充数据，请先提交补证")
    stats = app.worker.stats()
    if stats["queued"] >= stats.get("max_queue", 32):
        raise HTTPError(429, f"任务队列已满（{stats['queued']}/{stats.get('max_queue')}），请稍后重试")
    revision = expected + 1
    try:
        with app.db.tx() as conn:
            current = _session_revision_conn(conn, task_id)
            if current != expected:
                raise RevisionConflict(f"会话版本冲突：当前 {current}，提交基于 {expected}")
            latest = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not latest or latest["status"] != "needs_data":
                raise RevisionConflict("任务状态已变化，无法续跑")
            conn.execute(
                "INSERT INTO session_events(task_id,revision,event_type,payload_json,event_key,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (task_id, revision, "continue", json.dumps({
                    "state": "queued", "supplement_revision": expected,
                    "continuation": int(task.get("continuation_count") or 0) + 1,
                }, ensure_ascii=False), f"continue-{expected}", iso_now()))
            conn.execute(
                "UPDATE tasks SET status='queued', stage='queued', progress=0, finished_at=NULL, "
                "error=NULL, verdict_json=NULL, result_json=NULL, report_md=NULL, report_html=NULL, "
                "stats_json='{}', continuation_count=COALESCE(continuation_count,0)+1 WHERE id=?",
                (task_id,))
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM task_events WHERE task_id=?",
                               (task_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO task_events(task_id,seq,ts,level,stage,message,data_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (task_id, seq, iso_now(), "info", "queued",
                 "补充数据已提交，任务在同一任务 ID 下重新排队", "{}"))
    except RevisionConflict as exc:
        raise HTTPError(409, str(exc)) from exc
    if not app.worker.submit(task_id, fail_on_full=False):
        # 事务已提交为 queued；这里只负责在容量释放后补投递，重启恢复也会接住。
        def _retry() -> None:
            for delay in (0.5, 1, 2, 4, 8, 15, 30):
                time.sleep(delay)
                if app.worker.submit(task_id, fail_on_full=False):
                    return
        threading.Thread(target=_retry, daemon=True, name=f"requeue-{task_id}").start()
    app.db.audit(user["id"], user["username"], "task.continue", task_id,
                 {"revision": revision, "supplement_revision": expected}, req.ip)
    row = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    return response_json({"ok": True, "task": _task_summary(row), "revision": revision,
                          "supplement_revision": expected}, status=202)


def h_task_events(app: App, req, task_id: str):
    _task_for(app, req, task_id)
    after = int(req.query.get("after", 0))
    rows = app.db.all(
        "SELECT seq,ts,level,stage,message,data_json FROM task_events WHERE task_id=? AND seq>? "
        "ORDER BY seq ASC LIMIT 300", (task_id, after))
    return response_json({"items": [{"seq": r["seq"], "ts": r["ts"], "level": r["level"],
                                    "stage": r["stage"], "message": r["message"],
                                    "data": _loads(r["data_json"], {})} for r in rows]})


def h_task_stream(app: App, req, task_id: str):
    _task_for(app, req, task_id)
    last = req.headers.get("Last-Event-ID") or req.query.get("after") or "0"
    try:
        last_id = int(last)
    except ValueError:
        last_id = 0
    req.handler._sse(req, task_id, last_id)
    return None


def h_task_cancel(app: App, req, task_id: str):
    task, user = _task_owner_for(app, req, task_id)
    if task["status"] not in ("queued", "running"):
        raise HTTPError(400, "任务已结束，无法取消")
    app.worker.cancel(task_id)
    app.db.audit(user["id"], user["username"], "task.cancel", task_id, {}, req.ip)
    return response_json({"ok": True})


def h_task_retry(app: App, req, task_id: str):
    task, user = _task_owner_for(app, req, task_id)
    if (task.get("inputs_state") or "available") != "available":
        raise HTTPError(400, "原始日志未随精简迁移包迁移，无法重跑；请重新上传日志后新建任务")
    inputs = _loads(task["input_json"], [])
    missing = [i["name"] for i in inputs if not Path(i.get("stored_path", "")).exists()]
    if missing:
        raise HTTPError(400, "原始日志已被清理，无法重跑：" + "、".join(missing))
    stats = app.worker.stats()
    if stats["queued"] >= stats.get("max_queue", 32):
        raise HTTPError(429, f"任务队列已满（{stats['queued']}/{stats.get('max_queue')}），请稍后重试")
    new_task_id = new_id("T")
    app.db.execute(
        "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,model_override,"
        "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_task_id, user["id"], task["title"] + "（重跑）", task["description"],
         "queued", "queued", 0, task["mode"], task.get("model_override"),
         json.dumps(inputs, ensure_ascii=False), task["options_json"], iso_now()))
    app.worker.submit(new_task_id)
    app.db.audit(user["id"], user["username"], "task.retry", new_task_id,
                 {"from": task_id}, req.ip)
    row = app.db.one("SELECT * FROM tasks WHERE id=?", (new_task_id,))
    return response_json({"task": _task_summary(row), "id": new_task_id}, status=201)


def h_task_release(app: App, req, task_id: str):
    task, user = _task_owner_for(app, req, task_id)
    data = req.json() if req.headers.get("Content-Length") else {}
    targets = data.get("targets") if isinstance(data, dict) else None
    mode = str(data.get("mode") or "") if isinstance(data, dict) else ""
    if not mode and isinstance(targets, list):
        selected = {"raw_inputs" if "raw_inputs" in targets else "",
                    "extracted" if "extracted" in targets else ""}
        selected.discard("")
        mode = "both" if len(selected) == 2 else (selected.pop() if selected else "")
    try:
        job = app.storage.create(task_id, user["id"], mode or "raw_inputs")
    except ValueError as exc:
        message = str(exc)
        raise HTTPError(429 if "队列已满" in message else 400, message) from exc
    app.db.audit(user["id"], user["username"], "task.release", task_id,
                 {"job": job["id"], "mode": job.get("mode", "raw_inputs")}, req.ip)
    return response_json({"job": job}, status=202)


def h_task_storage_jobs(app: App, req, task_id: str):
    _task_owner_for(app, req, task_id)
    return response_json({"items": app.storage.list_for_task(task_id, 20)})


def h_storage_overview(app: App, req):
    user = _auth(app, req, csrf=False)
    limit = min(5000, max(1, int(req.query.get("limit", 1000))))
    return response_json(app.storage.overview(
        user["id"], is_admin=user["role"] == "admin", limit=limit))


def h_storage_job(app: App, req, job_id: str):
    user = _auth(app, req, csrf=False)
    try:
        job = app.storage.get(job_id)
    except ValueError as exc:
        raise HTTPError(404, str(exc)) from exc
    if user["role"] != "admin":
        task = app.db.one("SELECT user_id FROM tasks WHERE id=?", (job.get("task_id"),))
        if not task or task["user_id"] != user["id"]:
            raise HTTPError(404, "空间释放任务不存在")
    return response_json(job)


def h_backups(app: App, req):
    _admin(app, req, csrf=False)
    return response_json(app.backups.overview())


def h_backup_delete(app: App, req, name: str):
    user = _admin(app, req)
    try:
        result = app.backups.delete(name)
    except ValueError as exc:
        raise HTTPError(404 if "不存在" in str(exc) else 400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "backup.delete", name,
                 {"size_bytes": result.get("size_bytes"), "file_count": result.get("file_count")},
                 req.ip)
    return response_json(result)


def h_task_report(app: App, req, task_id: str):
    task = _task_for(app, req, task_id)
    fmt = req.query.get("format", "md")
    download = req.query.get("download") == "1"
    if fmt == "json":
        return response_json(_loads(task["result_json"], {}) or _task_summary(task))
    path = Path(task.get("report_html") if fmt == "html" else task.get("report_md") or "")
    if not path or not path.is_file():
        raise HTTPError(404, "报告尚未生成")
    if not within_directory(app.config.data_dir, path):
        raise HTTPError(403, "非法报告路径")
    ctype = "text/html; charset=utf-8" if fmt == "html" else "text/markdown; charset=utf-8"
    name = f"{task_id}.{'html' if fmt == 'html' else 'md'}"
    return response_file(path, ctype, name, inline=not download)


def h_task_review(app: App, req, task_id: str):
    task = _task_for(app, req, task_id)
    user = _auth(app, req)
    if user["role"] == "reviewer" and task.get("user_id") == user["id"] \
            and task.get("reviewer_id") != user["id"]:
        raise HTTPError(403, "不能复核自己创建的任务")
    payload = req.json()
    service = ReviewService(app.db, app.config.data_dir)
    try:
        review = service.submit(task, user, payload, req.ip)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    return response_json({"review": review})


def h_task_assign_reviewer(app: App, req, task_id: str):
    actor = _admin(app, req)
    task = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPError(404, "任务不存在")
    data = req.json()
    reviewer_id = data.get("reviewer_id")
    if reviewer_id in (None, "", 0, "0"):
        app.db.execute("UPDATE tasks SET reviewer_id=NULL, reviewer_assigned_at=NULL WHERE id=?",
                       (task_id,))
        app.db.audit(actor["id"], actor["username"], "task.unassign_reviewer", task_id,
                     {}, req.ip)
        return response_json({"ok": True, "reviewer_id": None})
    try:
        rid = int(reviewer_id)
    except (TypeError, ValueError) as exc:
        raise HTTPError(400, "reviewer_id 必须是整数") from exc
    target = app.db.one("SELECT * FROM users WHERE id=?", (rid,))
    if not target:
        raise HTTPError(404, "专家账号不存在")
    if target["role"] not in ("reviewer", "admin"):
        raise HTTPError(400, "只能分配给专家（reviewer）或管理员账号")
    if not target["active"] or normalize_status(target) != "active":
        raise HTTPError(400, "该账号当前不可用")
    now = iso_now()
    app.db.execute("UPDATE tasks SET reviewer_id=?, reviewer_assigned_at=? WHERE id=?",
                   (rid, now, task_id))
    create_notification(app.db, target["id"], "review_assigned", "收到新的复核任务",
                        f"{task['title']}（{task_id}）", f"/#/tasks/{task_id}")
    app.db.audit(actor["id"], actor["username"], "task.assign_reviewer", task_id,
                 {"reviewer_id": rid, "reviewer": target["username"]}, req.ip)
    return response_json({"ok": True, "reviewer_id": rid, "reviewer": target["username"]})


def h_quality(app: App, req):
    _admin(app, req, csrf=False)
    quality = ReviewService(app.db, app.config.data_dir).quality()
    quality["dataset_audit"] = EvaluationService(
        app.db, app.config, app.config.data_dir).audit_splits()
    return response_json(quality)


def h_skill_report(app: App, req, task_id: str, skill: str):
    _task_for(app, req, task_id)
    row = app.db.one("SELECT * FROM task_skills WHERE task_id=? AND skill_name=?",
                     (task_id, skill))
    if not row:
        raise HTTPError(404, "没有该技能的运行记录")
    fmt = req.query.get("format", "md")
    path = Path(row["report_html_path"] if fmt == "html" else row["report_md_path"] or "")
    if not path or not path.is_file():
        raise HTTPError(404, "子技能报告不存在")
    if not within_directory(app.config.data_dir, path):
        raise HTTPError(403, "非法报告路径")
    ctype = "text/html; charset=utf-8" if fmt == "html" else "text/markdown; charset=utf-8"
    return response_file(path, ctype, f"{skill}.{'html' if fmt == 'html' else 'md'}",
                         inline=req.query.get("download") != "1")


# ---------------------------- 统计 ----------------------------
def h_metrics(app: App, req):
    _admin(app, req)
    return response_json(collect_metrics(app))


def h_metrics_prometheus(app: App, req):
    token = str(app.config.get("metrics_token") or "")
    supplied = req.headers.get("X-Metrics-Token") or req.query.get("token") or ""
    if not token or supplied != token:
        _admin(app, req)
    body = prometheus_metrics(collect_metrics(app)).encode("utf-8")
    return 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8",
                 "Cache-Control": "no-store"}, body


def _usage_scope(app: App, req):
    user = _auth(app, req, csrf=False)
    raw = str(req.query.get("user_id") or "").strip()
    if raw:
        try:
            selected = int(raw)
        except ValueError as exc:
            raise HTTPError(400, "user_id 非法") from exc
        if user["role"] != "admin" and selected != int(user["id"]):
            raise HTTPError(403, "只能查看自己的用量")
        return user, selected, False
    if user["role"] == "admin":
        return user, None, True
    return user, int(user["id"]), False


def _usage_days(req, default: int = 30) -> int:
    try:
        days = int(str(req.query.get("days") or default))
    except ValueError as exc:
        raise HTTPError(400, "days 非法") from exc
    if days < 1 or days > 365:
        raise HTTPError(400, "days 必须在 1-365 之间")
    return days


def h_usage_overview(app: App, req):
    _user, user_id, all_users = _usage_scope(app, req)
    return response_json(app.usage.overview(
        days=_usage_days(req), user_id=user_id, all_users=all_users,
        include_directory=bool(_user["role"] == "admin")))


def h_usage_tasks(app: App, req):
    _user, user_id, all_users = _usage_scope(app, req)
    try:
        page = int(str(req.query.get("page") or 1))
        page_size = int(str(req.query.get("page_size") or 20))
    except ValueError as exc:
        raise HTTPError(400, "分页参数非法") from exc
    return response_json(app.usage.task_rows(
        days=_usage_days(req), user_id=user_id, all_users=all_users,
        page=page, page_size=page_size))


def h_stats(app: App, req):
    user = _auth(app, req, csrf=False)
    now_local = datetime.now(CST)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if user["role"] == "admin":
        own, args = "", ()
    elif user["role"] == "reviewer":
        own, args = " AND (user_id=? OR reviewer_id=?)", (user["id"], user["id"])
    else:
        own, args = " AND user_id=?", (user["id"],)
    today = app.db.one(f"SELECT COUNT(*) AS n FROM tasks WHERE created_at>=?{own}",
                       (start_utc,) + args)["n"]
    done = app.db.one(
        f"SELECT COUNT(*) AS n FROM tasks WHERE created_at>=? AND status='done'{own}",
        (start_utc,) + args)["n"]
    failed = app.db.one(
        f"SELECT COUNT(*) AS n FROM tasks WHERE created_at>=? AND status IN ('failed','needs_data'){own}",
        (start_utc,) + args)["n"]
    rows = app.db.all(
        f"SELECT started_at,finished_at,stats_json FROM tasks WHERE status='done' "
        f"AND started_at IS NOT NULL AND finished_at IS NOT NULL{own} ORDER BY finished_at DESC LIMIT 50",
        args)
    durations = [_duration(r) for r in rows]
    durations = [d for d in durations if d is not None]
    tokens = 0
    for r in rows:
        tokens += int((_loads(r["stats_json"], {}) or {}).get("total_tokens", 0))
    skills = app.db.all("SELECT status, COUNT(*) AS n FROM skills GROUP BY status")
    models_enabled = app.db.one(
        "SELECT COUNT(*) AS n FROM model_configs WHERE enabled=1")["n"]
    tasks_total = app.db.one(f"SELECT COUNT(*) AS n FROM tasks WHERE 1=1{own}", args)["n"]
    pending_reviews = 0
    pending_cases = 0
    if user["role"] == "reviewer":
        pending_reviews = app.db.one(
            "SELECT COUNT(*) AS n FROM tasks WHERE reviewer_id=? AND status IN ('done','needs_data') "
            "AND id NOT IN (SELECT task_id FROM task_reviews)", (user["id"],))["n"]
        pending_cases = app.db.one(
            "SELECT COUNT(*) AS n FROM real_cases WHERE reviewer_id=? AND review_status='pending'",
            (user["id"],))["n"]
    case_total = app.db.one(
        "SELECT COUNT(*) AS n FROM real_cases WHERE status IN ('labeled','golden') "
        "AND origin IN ('real','review')")["n"]
    recent = app.db.all(f"SELECT * FROM tasks WHERE 1=1{own} ORDER BY created_at DESC LIMIT 8", args)
    return response_json({
        "today": today, "done": done, "failed": failed,
        "success_rate": round(done / today * 100, 1) if today else 0,
        "avg_duration_sec": round(sum(durations) / len(durations), 1) if durations else 0,
        "tokens": tokens,
        "skills": {r["status"]: r["n"] for r in skills},
        "models_enabled": models_enabled,
        "tasks_total": tasks_total,
        "pending_reviews": pending_reviews,
        "pending_cases": pending_cases,
        "real_cases_labeled": case_total,
        "model_status": app.gateway.model_status(user["id"]),
        "workers": app.worker.stats(),
        "recent": [_task_summary(r) for r in recent],
    })


# ---------------------------- 真实案例与评测 ----------------------------
def _case_reviewer_name(db, reviewer_id):
    if not reviewer_id:
        return ""
    row = db.one("SELECT username FROM users WHERE id=?", (reviewer_id,))
    return (row or {}).get("username") or ""


def _case_can_view(user, case):
    if user["role"] == "admin":
        return True
    if case.get("created_by") in ("", user["username"]):
        return True
    return bool(case.get("reviewer_id")) and int(case["reviewer_id"]) == int(user["id"])


def h_cases(app: App, req):
    user = _auth(app, req, csrf=False)
    store = RealCaseStore(app.db, app.config.data_dir)
    split = req.query.get("split", "all")
    status = req.query.get("status", "all")
    review_status = req.query.get("review_status", "all")
    assigned = req.query.get("assigned", "")
    reviewer_id = None
    created_by = ""
    if user["role"] != "admin":
        if user["role"] == "reviewer" or assigned == "me":
            reviewer_id = int(user["id"])
        else:
            created_by = user["username"]
    rows = []
    if user["role"] == "reviewer":
        # reviewer 同时可见“分配给自己的”和“自己录入的”案例。
        rows = store.list(split, status, reviewer_id=reviewer_id,
                          review_status=review_status)
        rows += store.list(split, status, created_by=user["username"],
                           review_status=review_status)
        seen = set()
        rows = [r for r in rows if not (r["id"] in seen or seen.add(r["id"]))]
    else:
        rows = store.list(split, status, reviewer_id=reviewer_id,
                          review_status=review_status, created_by=created_by)
    for row in rows:
        row["reviewer_name"] = _case_reviewer_name(app.db, row.get("reviewer_id"))
    return response_json({"items": rows, "total": len(rows),
                          "review_status_counts": {
                              k: sum(1 for r in rows if r.get("review_status") == k)
                              for k in ("unassigned", "pending", "confirmed", "rejected")}})


def h_case_detail(app: App, req, case_id: str):
    user = _auth(app, req, csrf=False)
    store = RealCaseStore(app.db, app.config.data_dir)
    case = store.get(case_id)
    if not case:
        raise HTTPError(404, "案例不存在")
    if not _case_can_view(user, case):
        raise HTTPError(403, "无权查看该案例")
    case["reviewer_name"] = _case_reviewer_name(app.db, case.get("reviewer_id"))
    return response_json({"case": case})


def h_case_assign(app: App, req, case_id: str):
    user = _auth(app, req)
    if user["role"] != "admin":
        raise HTTPError(403, "只有管理员可以分配案例")
    store = RealCaseStore(app.db, app.config.data_dir)
    case = store.get(case_id)
    if not case:
        raise HTTPError(404, "案例不存在")
    data = req.json()
    raw = data.get("reviewer_id")
    reviewer_id = int(raw or 0)
    reviewer_name = ""
    if reviewer_id:
        row = app.db.one("SELECT id,username,role,status FROM users WHERE id=?", (reviewer_id,))
        if not row or row.get("status") != "active":
            raise HTTPError(400, "复核人不存在或已停用")
        if row["role"] not in ("reviewer", "admin"):
            raise HTTPError(400, "只能分配给复核专家或管理员")
        reviewer_name = row["username"]
    updated = store.assign_reviewer(case["id"], reviewer_id or None, reviewer_name,
                                     actor=user["username"], note=str(data.get("note") or ""))
    app.db.audit(user["id"], user["username"], "case.assign", case["id"],
                 {"reviewer_id": reviewer_id or None}, req.ip)
    updated["reviewer_name"] = _case_reviewer_name(app.db, updated.get("reviewer_id"))
    return response_json({"case": updated})


def h_case_history(app: App, req, case_id: str):
    user = _auth(app, req, csrf=False)
    store = RealCaseStore(app.db, app.config.data_dir)
    case = store.get(case_id)
    if not case:
        raise HTTPError(404, "案例不存在")
    if not _case_can_view(user, case):
        raise HTTPError(403, "无权查看该案例")
    return response_json({"items": store.history(case["id"])})


def h_case_label(app: App, req, case_id: str):
    user = _auth(app, req)
    store = RealCaseStore(app.db, app.config.data_dir)
    case = store.get(case_id)
    if not case:
        raise HTTPError(404, "案例不存在")
    if not _case_can_view(user, case):
        raise HTTPError(403, "只能标注自己录入或分配给自己的案例")
    data = req.json()
    kwargs: dict[str, Any] = {}
    if "expected_roots" in data:
        kwargs["expected_roots"] = [str(x).strip() for x in data.get("expected_roots") or [] if str(x).strip()]
    if "excluded_roots" in data:
        kwargs["excluded_roots"] = [str(x).strip() for x in data.get("excluded_roots") or [] if str(x).strip()]
    for key in ("split", "status", "notes"):
        if data.get(key):
            kwargs[key] = str(data[key])
    labels: dict[str, Any] = {}
    if isinstance(data.get("labels"), dict):
        labels.update(data["labels"])
    for key in ("evidence_window", "recovery_result", "decision_note", "source_note"):
        if key in data:
            labels[key] = data[key]
    if labels:
        kwargs["labels"] = labels
    action = str(data.get("action") or "label")
    if not kwargs and action == "label":
        raise HTTPError(400, "没有提供可更新的标签字段")
    if action not in ("label", "approve", "reject", "correct"):
        raise HTTPError(400, "非法复核动作")
    review_status = str(data.get("review_status") or "")
    if not review_status:
        review_status = {"approve": "confirmed", "reject": "rejected"}.get(action, "pending")
    if kwargs.get("status") == "golden" and kwargs.get("split", case.get("split")) == "unlabeled":
        raise HTTPError(400, "golden 案例必须切分到 dev/test/hidden，不能停留在 unlabeled")
    if user["role"] != "admin" and case.get("reviewer_id") != user["id"]:
        # 创建者可以补录标签，但不能替复核人确认。
        if action in ("approve", "reject") or review_status == "confirmed":
            raise HTTPError(403, "只有被分配的复核人或管理员可以确认复核")
    try:
        updated = store.update_labels(case["id"], actor_id=user["id"], actor_name=user["username"],
                                      action=action, note=str(data.get("decision_note") or ""),
                                      review_status=review_status, **kwargs)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "case.label", case["id"],
                 {"status": updated["status"], "split": updated["split"],
                  "review_status": updated["review_status"], "action": action}, req.ip)
    updated["reviewer_name"] = _case_reviewer_name(app.db, updated.get("reviewer_id"))
    return response_json({"case": updated})


def h_evaluation_run(app: App, req):
    user = _auth(app, req)
    if user["role"] != "admin":
        raise HTTPError(403, "只有管理员可以发起评测")
    data = req.json()
    split = str(data.get("split") or "test")
    if split not in ("dev", "test", "hidden"):
        raise HTTPError(400, "评测切分必须是 dev/test/hidden")
    try:
        limit = max(0, int(data.get("limit") or 0))
    except (TypeError, ValueError) as exc:
        raise HTTPError(400, "limit 必须是整数") from exc
    llm_mode = str(data.get("llm_mode") or "off").strip().lower()
    model_config_id = data.get("model_config_id", data.get("model_id"))
    if model_config_id in ("", None):
        model_config_id = None
    else:
        try:
            model_config_id = int(model_config_id)
        except (TypeError, ValueError) as exc:
            raise HTTPError(400, "model_config_id 必须是整数") from exc
    try:
        result = EvaluationService(app.db, app.config).evaluate(
            split=split, limit=limit, case_ids=[str(x) for x in (data.get("case_ids") or [])] or None,
            name=str(data.get("name") or ""), llm_mode=llm_mode,
            model_config_id=model_config_id)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "evaluation.run", result["id"],
                 {"split": split, "limit": limit, "cases": result["summary"].get("cases"),
                  "llm_mode": llm_mode, "model_config_id": model_config_id}, req.ip)
    return response_json(result)


def h_evaluations(app: App, req):
    user = _auth(app, req, csrf=False)
    rows = app.db.all(
        "SELECT id,name,split,status,summary_json,error,created_at,finished_at "
        "FROM evaluations ORDER BY created_at DESC LIMIT 20")
    items = []
    for row in rows:
        items.append({**{k: row[k] for k in ("id", "name", "split", "status", "error",
                                             "created_at", "finished_at")},
                      "summary": _loads(row.get("summary_json"), {}) or {}})
    if user["role"] != "admin":
        items = items[:5]
    return response_json({"items": items})


# ---------------------------- 技能库 ----------------------------
def _skill_row(app: App, name: str) -> dict[str, Any]:
    row = app.db.one("SELECT * FROM skills WHERE name=?", (name,))
    if not row:
        raise HTTPError(404, "技能不存在")
    return row


def _skill_public(row: dict[str, Any]) -> dict[str, Any]:
    profile = _loads(row.get("profile_json"), {}) or {}
    health = _loads(row.get("health_json"), {}) or {}
    components = _loads(row.get("components_json"), {}) or {}
    fault = profile.get("fault") or {}
    return {
        "name": row["name"], "title": row["title"], "version": row["version"],
        "category": row["category"], "description": row["description"],
        "status": row["status"], "verified": bool(row["verified"]),
        "execution_mode": row["execution_mode"], "source": row["source"],
        "channel": row.get("channel") or "stable",
        "fault": {"name": fault.get("name"), "aliases": fault.get("aliases") or [],
                  "alarm_codes": fault.get("alarm_codes") or [],
                  "device_types": (fault.get("domain") or {}).get("device_types") or []},
        "keywords": (profile.get("symptoms") or {}).get("description_keywords") or [],
        "root_causes": [{"id": rc.get("id"), "name": rc.get("name"),
                         "confidence": rc.get("confidence")} for rc in (profile.get("root_causes") or [])],
        "rules_count": health.get("rules", 0), "root_causes_count": health.get("root_causes", 0),
        "components": components, "health": health,
        "imported_at": row["imported_at"], "updated_at": row["updated_at"],
    }


def h_skills(app: App, req):
    _auth(app, req, csrf=False)
    rows = app.db.all("SELECT * FROM skills ORDER BY status, name")
    return response_json({"items": [_skill_public(r) for r in rows]})


def h_skill_detail(app: App, req, name: str):
    _auth(app, req, csrf=False)
    row = _skill_row(app, name)
    data = _skill_public(row)
    profile = _loads(row.get("profile_json"), {}) or {}
    data["profile"] = {
        "symptoms": profile.get("symptoms") or {},
        "fault": profile.get("fault") or {},
        "steps": profile.get("analysis", {}).get("steps") or [],
        "root_causes": profile.get("root_causes") or [],
        "temporal": profile.get("temporal") or [],
        "mode": profile.get("mode") or "normal",
    }
    data["versions"] = app.db.all(
        "SELECT version,status,note,created_at FROM skill_versions WHERE skill_name=? "
        "ORDER BY created_at DESC", (name,))
    data["cases"] = app.db.all(
        "SELECT case_id,kind,expected_root_cause,description,path FROM cases WHERE skill_name=?",
        (name,))
    return response_json(data)


def h_skill_scan(app: App, req):
    _admin(app, req)
    skills = app.registry.scan()
    return response_json({"ok": True, "count": len(skills)})


def h_skill_disable(app: App, req, name: str):
    _admin(app, req)
    _skill_row(app, name)
    app.registry.set_status(name, "disabled")
    return response_json({"ok": True})


def h_skill_enable(app: App, req, name: str):
    _admin(app, req)
    _skill_row(app, name)
    app.db.execute("UPDATE skills SET status='ready', updated_at=? WHERE name=?",
                   (iso_now(), name))
    app.registry.scan()
    return response_json({"ok": True})


def h_skill_import(app: App, req):
    user = _admin(app, req)
    max_mb = int(app.config.get("max_upload_mb", 2048))
    fields, files = req.parse_multipart(max_mb * 1024 * 1024)
    if not files:
        raise HTTPError(400, "请上传技能 zip 包")
    from .supply import ImportLimits, ImportRejected, quarantine_zip, sign_skill
    limits = ImportLimits.from_config(app.config)
    tmp = app.config.data_dir / "tmp" / f"import-{new_id('', with_time=False)}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        quarantined = []
        for f in files:
            try:
                quarantined.append(quarantine_zip(f.path, tmp, limits))
            except ImportRejected as exc:
                raise HTTPError(422, f"技能包未通过导入检查：{exc}") from exc
        if len(quarantined) != 1:
            raise HTTPError(400, "一次只能导入一个技能包")
        result = quarantined[0]
        skill_md = result.root / "SKILL.md"
        fm = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
        name = str(fm.get("name") or result.name)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", name):
            raise HTTPError(400, "技能名不合法")
        version = str((fm.get("metadata") or {}).get("profile_version") or "")
        target = app.config.skills_dir / name
        overwrite = fields.get("overwrite") == "1"
        backup = None
        if target.exists():
            if not overwrite:
                raise HTTPError(409, f"技能 {name} 已存在；如需覆盖请勾选 overwrite")
            backup = app.config.data_dir / "skill_versions" / name / f".import-{int(time.time())}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        try:
            shutil.copytree(result.root, target)
            signed = sign_skill(
                target, data_dir=app.config.data_dir, name=name, version=version,
                builder="import",
                verification={"status": "quarantined", "ok": False,
                              "reason": "导入包只完成隔离与完整性扫描，尚未执行案例回归"})
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            if backup and backup.exists() and not target.exists():
                shutil.move(str(backup), str(target))
            raise
        app.registry.scan()
        app.db.audit(user["id"], user["username"], "skill.import", name,
                     {"files": result.limits.get("files"), "bytes": result.total_bytes,
                      "content_sha256": signed["manifest"]["content_sha256"],
                      "overwrite": overwrite}, req.ip)
        return response_json({"ok": True, "skill": _skill_public(_skill_row(app, name)),
                              "manifest": signed["manifest"],
                              "quarantine": result.limits})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def h_recovery_runs(app: App, req, task_id: str):
    _task_for(app, req, task_id)
    return response_json({"runs": recovery_list(app.db, task_id)})


def h_recovery_create(app: App, req, task_id: str):
    task = _task_for(app, req, task_id)
    data = req.json()
    before = data.get("before") if isinstance(data.get("before"), dict) else {}
    if not before:
        before = _loads(task.get("verdict_json"), {}) or _loads(task.get("result_json"), {}) or {}
    if not before:
        raise HTTPError(409, "任务尚无基线结论，无法建立恢复验证")
    user = _auth(app, req)
    run = recovery_create(app.db, task_id, before, user["username"],
                          str(data.get("source") or "readonly_receipt"))
    app.db.audit(user["id"], user["username"], "recovery.create", task_id,
                 {"run_id": run["id"]}, req.ip)
    return response_json({"run": run}, status=201)


def h_recovery_observe(app: App, req, run_id: str):
    _auth(app, req)
    try:
        run = recovery_get(app.db, run_id)
    except ValueError as exc:
        raise HTTPError(404, str(exc)) from exc
    task = _task_for(app, req, run["task_id"])
    user = _auth(app, req)
    after = None
    upload = None
    if "multipart/form-data" in (req.headers.get("Content-Type") or "").lower():
        max_mb = min(64, int(app.config.get("max_upload_mb", 2048)))
        fields, files = req.parse_multipart(max_mb * 1024 * 1024,
                                            max_file_bytes=max_mb * 1024 * 1024)
        try:
            payload = json.loads(fields.get("after") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPError(400, "恢复后回传内容不是合法 JSON") from exc
        after = payload.get("after") if isinstance(payload.get("after"), dict) else payload
        upload = files[0] if files else None
    else:
        data = req.json()
        after = data.get("after") if isinstance(data.get("after"), dict) else data
    if not after:
        raise HTTPError(400, "缺少恢复后只读回传内容")
    if upload:
        digest = hashlib.sha256()
        total = 0
        try:
            with open(upload.path, "rb") as fh:
                raw = fh.read(1024 * 1024)
                digest.update(raw)
                total = len(raw)
                while True:
                    chunk = fh.read(512 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
        except OSError as exc:
            raise HTTPError(400, "无法读取回传文件") from exc
        text = raw.decode("utf-8", errors="replace")
        evidence = after.get("evidence") if isinstance(after.get("evidence"), list) else []
        if not evidence:
            evidence = [{"rule_id": "", "file": upload.filename, "text": "", "hit": True}]
        first = dict(evidence[0])
        first["file"] = upload.filename or first.get("file") or "after-evidence.txt"
        first["sha256"] = digest.hexdigest()
        first["size"] = total
        first["uploaded"] = True
        if not first.get("text"):
            first["text"] = "\n".join(text.splitlines()[:60])[:4000]
        evidence[0] = first
        after["evidence"] = evidence
    try:
        run = recovery_observe(app.db, run_id, after, user["username"])
    except ValueError as exc:
        raise HTTPError(409, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "recovery.observe", run["task_id"],
                 {"run_id": run_id, "status": run["comparison"].get("status")}, req.ip)
    return response_json({"run": run})


def h_recovery_review(app: App, req, run_id: str):
    user = _auth(app, req)
    if user["role"] not in ("reviewer", "admin"):
        raise HTTPError(403, "仅专家或管理员可复核恢复验证")
    try:
        run = recovery_get(app.db, run_id)
    except ValueError as exc:
        raise HTTPError(404, str(exc)) from exc
    task = _task_for(app, req, run["task_id"])
    if user["role"] == "reviewer" and task.get("reviewer_id") != user["id"]:
        raise HTTPError(403, "只能复核分配给自己的任务")
    data = req.json()
    try:
        run = recovery_review(app.db, run_id, user["username"],
                              str(data.get("decision") or ""), str(data.get("note") or ""))
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "recovery.review", run["task_id"],
                 {"run_id": run_id, "eligible": run["learning_gate"]["eligible"]}, req.ip)
    return response_json({"run": run})


def h_case_matrix(app: App, req):
    _auth(app, req, csrf=False)
    skill = str(req.query.get("skill") or "")
    snapshot = latest_matrix(app.db, skill)
    if not snapshot:
        raise HTTPError(404, "尚无案例矩阵快照")
    return response_json({"matrix": snapshot, "verify": verify_matrix(app.config.data_dir, snapshot)})


def h_case_matrix_rebuild(app: App, req):
    user = _admin(app, req)
    data = req.json()
    skill = str(data.get("skill") or "")[:120]
    build_sha = str(data.get("build_sha") or "")[:100]
    compatibility = data.get("compatibility") if isinstance(data.get("compatibility"), dict) else {}
    snapshot = build_matrix(app.db, skill, build_sha, compatibility)
    save_matrix(app.db, app.config.data_dir, snapshot)
    app.db.audit(user["id"], user["username"], "case_matrix.rebuild", skill,
                 {"build_sha": build_sha, "cells": snapshot["summary"]["cells"]}, req.ip)
    return response_json({"matrix": snapshot,
                          "verify": verify_matrix(app.config.data_dir, snapshot)})


def h_experience_candidates(app: App, req):
    _admin(app, req, csrf=False)
    status = str(req.query.get("status") or "")
    rows = app.db.all("SELECT * FROM experience_candidates"
                      + (" WHERE status=?" if status else "")
                      + " ORDER BY created_at DESC LIMIT 200", (status,) if status else ())
    return response_json({"candidates": [dict(x) for x in rows]})


def h_candidate_decide(app: App, req, candidate_id: str):
    user = _admin(app, req)
    data = req.json()
    try:
        result = decide_candidate(app.db, candidate_id, str(data.get("status") or ""),
                                  user["username"], str(data.get("note") or ""))
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "experience_candidate.decide", candidate_id,
                 {"status": result.get("status")}, req.ip)
    return response_json({"candidate": result})


def h_calibration(app: App, req):
    _auth(app, req, csrf=False)
    return response_json({"calibration": CalibrationStore(app.db, app.config).latest()})


def h_calibration_fit(app: App, req):
    user = _admin(app, req)
    split = str(req.json().get("split") or "dev")
    try:
        result = CalibrationStore(app.db, app.config).fit(split)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "calibration.fit", split,
                 {"samples": result.get("sample_count"), "status": result.get("status")},
                 req.ip)
    return response_json({"calibration": result})


def h_agent_trace(app: App, req, task_id: str):
    _task_for(app, req, task_id)
    trace = BoundedAgent(app.db, app.config).latest(task_id)
    if not trace:
        raise HTTPError(404, "尚无 Agent 轨迹")
    return response_json({"trace": trace})


def h_skill_tool(app: App, req, name: str):
    _auth(app, req, csrf=False)
    skill = app.registry.get(name)
    if not skill:
        raise HTTPError(404, "技能不存在")
    return response_json(app.tools.manifest(skill).public())


def h_skill_manifest(app: App, req, name: str):
    _auth(app, req, csrf=False)
    _skill_row(app, name)
    try:
        result = app.registry.manifest(name)
    except ValueError as exc:
        raise HTTPError(404, str(exc)) from exc
    return response_json(result)


def h_skill_manifest_sign(app: App, req, name: str):
    user = _admin(app, req)
    _skill_row(app, name)
    try:
        result = app.registry.sign_manifest(name)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "skill.manifest.sign", name,
                 {"content_sha256": (result.get("manifest") or {}).get("content_sha256")},
                 req.ip)
    return response_json({"ok": True, "manifest": result.get("manifest"),
                          "signature": result.get("signature")})


def _trace_public(app: App, row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item.pop("trace_path_abs", None)
    kind = str(item.get("kind") or "")
    ref_id = str(item.get("ref_id") or "")
    meta = item.get("meta") or {}
    title = ""
    if kind == "diagnosis":
        task = app.db.one("SELECT title FROM tasks WHERE id=?", (ref_id,)) or {}
        title = str(task.get("title") or "")
        item["ref_url"] = f"#/tasks/{ref_id}"
    elif kind == "workshop":
        draft_id = str(meta.get("draft_id") or "")
        draft = app.db.one("SELECT name FROM workshop_drafts WHERE id=?", (draft_id,)) or {}
        title = str(draft.get("name") or "")
        item["ref_url"] = "#/workshop"
        item["draft_id"] = draft_id
    item["ref_title"] = title or str(meta.get("title") or meta.get("draft_name") or "")
    return item


def h_traces(app: App, req):
    _admin(app, req, csrf=False)
    kind = str(req.query.get("kind") or "").strip()
    ref_id = str(req.query.get("ref_id") or "").strip()
    status = str(req.query.get("status") or "").strip()
    try:
        limit = max(1, min(200, int(req.query.get("limit") or 60)))
    except ValueError:
        limit = 60
    rows = app.traces.list(kind=kind, ref_id=ref_id, status=status, limit=limit)
    return response_json({"items": [_trace_public(app, row) for row in rows]})


def h_tool_calls(app: App, req):
    user = _auth(app, req, csrf=False)
    task_id = str(req.query.get("task_id") or "").strip()
    if task_id:
        _task_for(app, req, task_id)
    elif user["role"] != "admin":
        task_id = ""
    limit = req.query.get("limit") or "100"
    try:
        limit_n = max(1, min(500, int(limit)))
    except ValueError as exc:
        raise HTTPError(400, "limit 必须是整数") from exc
    return response_json({"items": app.tools.list_calls(task_id, limit_n),
                          "mode": app.tools.mode})


def _trace_for(app: App, run_id: str) -> dict[str, Any]:
    run = app.traces.get(run_id)
    if not run:
        raise HTTPError(404, "轨迹不存在")
    return run


def h_trace_summary(app: App, req, run_id: str):
    _admin(app, req, csrf=False)
    try:
        data = app.traces.summary(run_id)
    except KeyError as exc:
        raise HTTPError(404, "轨迹不存在") from exc
    data["run"] = _trace_public(app, data["run"])
    return response_json(data)


def h_trace_events(app: App, req, run_id: str):
    _admin(app, req, csrf=False)
    try:
        after = max(0, int(req.query.get("after") or 0))
    except ValueError:
        after = 0
    try:
        limit = max(1, min(1000, int(req.query.get("limit") or 300)))
    except ValueError:
        limit = 300
    type_filter = str(req.query.get("type") or "").strip()
    phase = str(req.query.get("phase") or "").strip()
    only_failed = str(req.query.get("only_failed") or "") in ("1", "true", "yes")
    try:
        data = app.traces.events(run_id, after=after, limit=limit,
                                 type_filter=type_filter, phase=phase,
                                 only_failed=only_failed)
    except KeyError as exc:
        raise HTTPError(404, "轨迹不存在") from exc
    data["run"] = _trace_public(app, data["run"])
    return response_json(data)


def h_trace_download(app: App, req, run_id: str):
    actor = _admin(app, req, csrf=False)
    run = _trace_for(app, run_id)
    path = Path(str(run.get("trace_path_abs") or "")).resolve()
    root = (Path(app.config.data_dir) / "traces").resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPError(404, "轨迹原文不存在")
    app.db.audit(actor["id"], actor["username"], "trace.download", run_id,
                 {"size": path.stat().st_size}, req.ip)
    return response_file(path, "application/x-ndjson; charset=utf-8",
                         download_name=f"{run_id}.jsonl", inline=False)


def h_tool_approvals(app: App, req):
    _admin(app, req, csrf=False)
    status = str(req.query.get("status") or "")
    return response_json({"items": app.tools.list_approvals(status, 200)})


def h_tool_approval_create(app: App, req, task_id: str):
    task, user = _task_owner_for(app, req, task_id)
    data = req.json()
    skill_name = str(data.get("skill") or "").strip()
    skill = app.registry.get(skill_name) if skill_name else None
    if not skill:
        raise HTTPError(404, "技能不存在")
    inputs = [Path(x["stored_path"]) for x in json.loads(task.get("input_json") or "[]")
              if x.get("stored_path")]
    manifest = app.tools.manifest(skill)
    args = app.tools.arguments(task_id, inputs, task.get("description") or "",
                               app.tools.current_revision(task_id))
    approval = app.tools.create_approval(
        task_id=task_id, manifest=manifest,
        arguments_hash=app.tools.arguments_hash(args),
        requested_by=user["username"], reason=str(data.get("reason") or ""),
        ttl_minutes=int(app.config.get("tool_approval_ttl_min", 120)))
    app.db.audit(user["id"], user["username"], "tool.approval.request", approval.get("id", ""),
                 {"task_id": task_id, "skill": skill_name, "risk": manifest.risk}, req.ip)
    return response_json({"approval": approval, "manifest": manifest.public()})


def h_tool_approval_decide(app: App, req, approval_id: str):
    user = _admin(app, req)
    data = req.json()
    try:
        approval = app.tools.decide_approval(
            approval_id, approved=bool(data.get("approved")),
            actor=user["username"], reason=str(data.get("reason") or ""))
    except ValueError as exc:
        raise HTTPError(409, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "tool.approval.decide", approval_id,
                 {"approved": bool(data.get("approved")), "task_id": approval.get("task_id")},
                 req.ip)
    return response_json({"approval": approval})


def h_skill_export(app: App, req, name: str):
    _auth(app, req, csrf=False)
    row = _skill_row(app, name)
    path = Path(row["path"])
    if not path.is_dir() or not within_directory(app.config.skills_dir, path):
        raise HTTPError(404, "技能目录不存在")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in path.rglob("*"):
            if file.is_file():
                zf.write(file, str(Path(name) / file.relative_to(path)))
    data = buf.getvalue()
    headers = {"Content-Type": "application/zip",
               "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.zip"}
    return 200, headers, data


def h_skill_rollback(app: App, req, name: str):
    user = _admin(app, req)
    data = req.json()
    version = str(data.get("version") or "")
    try:
        result = app.workshop.rollback(name, version, user["username"])
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    return response_json(result)


def h_skill_channel(app: App, req, name: str):
    user = _admin(app, req)
    data = req.json()
    channel = str(data.get("channel") or "")
    _skill_row(app, name)
    try:
        app.registry.set_channel(name, channel)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "skill.channel", name,
                 {"channel": channel}, req.ip)
    return response_json({"ok": True, "name": name, "channel": channel})


def h_versions(app: App, req):
    _auth(app, req, csrf=False)
    skill = req.query.get("skill") or None
    return response_json({"items": app.workshop.list_versions(skill)})


# ---------------------------- 技能工坊 ----------------------------
def _draft_for(app: App, req, draft_id: str) -> dict[str, Any]:
    user = _auth(app, req)
    row = app.db.one("SELECT * FROM workshop_drafts WHERE id=?", (draft_id,))
    if not row:
        raise HTTPError(404, "工坊草稿不存在")
    if row["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPError(404, "工坊草稿不存在")
    return row


def h_workshop_provenance(app: App, req, draft_id: str):
    _auth(app, req, csrf=False)
    _draft_for(app, req, draft_id)
    draft = app.workshop.get(draft_id)
    spec = ((draft.get("analysis") or {}).get("intake") or {})
    if not spec.get("provenance") and str(draft.get("source_text") or "").strip():
        spec = public_spec(parse_source(draft["source_text"]))
    return response_json({"provenance": spec.get("provenance") or {},
                          "source_revision": spec.get("source_revision") or "",
                          "audit": audit_provenance(spec)})


def h_workshop_provenance_lock(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    data = req.json()
    paths = data.get("paths") if isinstance(data.get("paths"), list) else []
    paths = [str(p) for p in paths if str(p).startswith("/")][:200]
    if not paths:
        raise HTTPError(400, "至少提供一个字段路径")
    draft = app.workshop.get(draft_id)
    analysis = draft.get("analysis") or {}
    spec = analysis.get("intake") or {}
    if not spec:
        raise HTTPError(409, "请先完成资料分析")
    if data.get("source_revision") and str(data["source_revision"]) != str(spec.get("source_revision") or ""):
        raise HTTPError(409, "原文版本已变化，请重新分析后再锁字段")
    report = lock_fields(spec, paths, bool(data.get("locked", True)))
    analysis["intake"] = spec
    app.db.execute("UPDATE workshop_drafts SET analysis_json=?, updated_at=? WHERE id=?",
                   (json.dumps(analysis, ensure_ascii=False), iso_now(), draft_id))
    app.db.execute("INSERT INTO workshop_patches(id,draft_id,path,old_value_json,new_value_json,"
                   "actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (new_id("WP"), draft_id, ",".join(paths)[:400],
                    json.dumps({"locked": False}),
                    json.dumps({"locked": bool(data.get("locked", True))}),
                    user["username"], str(data.get("reason") or "")[:500], iso_now()))
    app.db.audit(user["id"], user["username"], "workshop.provenance.lock", draft_id,
                 {"paths": paths[:20], "locked": bool(data.get("locked", True))}, req.ip)
    return response_json({"provenance": spec.get("provenance") or {},
                          "audit": report})


def h_workshop_recipes(app: App, req):
    user = _auth(app, req, csrf=False)
    return response_json({"items": app.workshop.recipes()})


def h_workshop_list(app: App, req):
    user = _auth(app, req, csrf=False)
    rows = app.workshop.list_drafts(user["id"], user["role"] == "admin")
    return response_json({"items": [{
        "id": r["id"], "name": r["name"], "title": r["title"], "fault_name": r["fault_name"],
        "category": r["category"], "status": r["status"], "skill_version": r["skill_version"],
        "recipe_id": r.get("recipe_id") or "",
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "owner_id": r["owner_id"], "last_error": r["last_error"],
    } for r in rows]})


def _async_requested(req) -> bool:
    return str(req.query.get("async") or "").lower() in ("1", "true", "yes", "on")


def _job_error(exc: Exception) -> HTTPError:
    message = str(exc)
    return HTTPError(429 if "队列已满" in message else 400, message)


def h_workshop_create(app: App, req):
    user = _auth(app, req)
    if user["role"] != "admin" and not app.config.get("allow_engineer_workshop", True):
        raise HTTPError(403, "当前配置只允许管理员创建技能草稿")
    data = req.json()
    async_mode = _async_requested(req)
    model_override = data.get("model_override") or data.get("model_id")
    try:
        draft = app.workshop.create_draft(
            user["id"], data, deferred=async_mode,
            model_override=int(model_override) if model_override else None)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    job = None
    if async_mode:
        try:
            job = app.workshop_jobs.create(
                user["id"], draft["id"], "analyze",
                model_override=int(model_override) if model_override else None)
        except ValueError as exc:
            raise _job_error(exc) from exc
    app.db.audit(user["id"], user["username"], "workshop.create", draft["id"],
                 {"name": draft["name"], "recipe_id": draft.get("recipe_id") or "",
                  "async": async_mode}, req.ip)
    return response_json({"draft": draft, "job": job}, status=202 if async_mode else 201)


def h_workshop_detail(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    draft = app.workshop.get(draft_id)
    draft["model_status"] = app.gateway.model_status(user["id"])
    draft["jobs"] = app.workshop_jobs.list_for_draft(draft_id, 5)
    return response_json(draft)


def h_workshop_update(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    try:
        draft = app.workshop.update_draft(draft_id, req.json())
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "workshop.update", draft_id, {}, req.ip)
    return response_json({"draft": draft})


def h_workshop_delete(app: App, req, draft_id: str):
    user = _auth(app, req)
    row = _draft_for(app, req, draft_id)
    try:
        result = app.workshop.delete_draft(draft_id)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "workshop.delete", draft_id,
                 {"title": row.get("title"), "status": row.get("status")}, req.ip)
    return response_json(result)


def h_workshop_logs(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    max_mb = int(app.config.get("max_upload_mb", 2048))
    fields, files = req.parse_multipart(max_mb * 1024 * 1024, max_file_bytes=max_mb * 1024 * 1024)
    if not files:
        raise HTTPError(400, "请上传日志/案例包")
    model_override = None
    if fields.get("model_id"):
        try:
            model_override = int(fields["model_id"])
        except ValueError:
            model_override = None
    payload = [{"stored_path": str(f.path), "name": f.filename} for f in files]
    if _async_requested(req):
        try:
            job = app.workshop_jobs.create(
                user["id"], draft_id, "logs", model_override=model_override, uploads=payload)
        except ValueError as exc:
            raise _job_error(exc) from exc
        app.db.audit(user["id"], user["username"], "workshop.logs", draft_id,
                     {"files": len(files), "async": True}, req.ip)
        return response_json({"job": job}, status=202)
    trace = app.traces.start(
        kind="workshop", ref_id=draft_id, user_id=int(user["id"]),
        meta={"trigger": "sync", "api": "logs", "files": len(files)})
    try:
        draft = app.workshop.attach_logs(draft_id, payload, model_override=model_override,
                                         trace=trace)
        trace.finish("success")
    except ValueError as exc:
        trace.error(phase="workshop", code="workshop_logs_failed",
                    message=str(exc), exc=exc, retryable=False)
        trace.finish("failed", error_code="workshop_logs_failed", error_summary=str(exc))
        raise HTTPError(400, str(exc)) from exc
    except Exception as exc:
        trace.error(phase="workshop", code="workshop_logs_failed",
                    message=str(exc), exc=exc, retryable=True)
        trace.finish("failed", error_code="workshop_logs_failed", error_summary=str(exc))
        raise
    app.db.audit(user["id"], user["username"], "workshop.logs", draft_id,
                 {"files": len(files)}, req.ip)
    return response_json({"draft": draft})


def h_workshop_analyze(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    data = req.json() if req.headers.get("Content-Length") else {}
    model_override = data.get("model_override") or data.get("model_id")
    if _async_requested(req):
        try:
            job = app.workshop_jobs.create(
                user["id"], draft_id, "analyze",
                model_override=int(model_override) if model_override else None)
        except ValueError as exc:
            raise _job_error(exc) from exc
        app.db.audit(user["id"], user["username"], "workshop.analyze", draft_id,
                     {"async": True}, req.ip)
        return response_json({"job": job}, status=202)
    trace = app.traces.start(
        kind="workshop", ref_id=draft_id, user_id=int(user["id"]),
        meta={"trigger": "sync", "api": "analyze"})
    try:
        draft = app.workshop.analyze(
            draft_id, model_override=int(model_override) if model_override else None,
            trace=trace)
        trace.finish("success")
    except ValueError as exc:
        trace.error(phase="workshop", code="workshop_analyze_failed",
                    message=str(exc), exc=exc, retryable=False)
        trace.finish("failed", error_code="workshop_analyze_failed", error_summary=str(exc))
        raise HTTPError(400, str(exc)) from exc
    except Exception as exc:
        trace.error(phase="workshop", code="workshop_analyze_failed",
                    message=str(exc), exc=exc, retryable=True)
        trace.finish("failed", error_code="workshop_analyze_failed", error_summary=str(exc))
        raise
    app.db.audit(user["id"], user["username"], "workshop.analyze", draft_id, {}, req.ip)
    return response_json({"draft": draft})


def h_workshop_refine(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    data = req.json()
    text = str(data.get("text") or data.get("answers") or "")
    model_override = data.get("model_override") or data.get("model_id")
    if _async_requested(req):
        try:
            job = app.workshop_jobs.create(
                user["id"], draft_id, "refine", {"text": text},
                model_override=int(model_override) if model_override else None)
        except ValueError as exc:
            raise _job_error(exc) from exc
        app.db.audit(user["id"], user["username"], "workshop.refine", draft_id,
                     {"async": True}, req.ip)
        return response_json({"job": job}, status=202)
    trace = app.traces.start(
        kind="workshop", ref_id=draft_id, user_id=int(user["id"]),
        meta={"trigger": "sync", "api": "refine"})
    try:
        draft = app.workshop.refine(
            draft_id, text, model_override=int(model_override) if model_override else None,
            trace=trace)
        trace.finish("success")
    except ValueError as exc:
        trace.error(phase="workshop", code="workshop_refine_failed",
                    message=str(exc), exc=exc, retryable=False)
        trace.finish("failed", error_code="workshop_refine_failed", error_summary=str(exc))
        raise HTTPError(400, str(exc)) from exc
    except Exception as exc:
        trace.error(phase="workshop", code="workshop_refine_failed",
                    message=str(exc), exc=exc, retryable=True)
        trace.finish("failed", error_code="workshop_refine_failed", error_summary=str(exc))
        raise
    app.db.audit(user["id"], user["username"], "workshop.refine", draft_id, {}, req.ip)
    return response_json({"draft": draft})


def h_workshop_check(app: App, req, draft_id: str):
    _draft_for(app, req, draft_id)
    data = req.json() if req.headers.get("Content-Length") else {}
    row = app.workshop.get(draft_id)
    if isinstance(data, dict) and data.get("profile_yaml"):
        try:
            app.workshop.update_draft(draft_id, {"profile_yaml": data["profile_yaml"]})
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from exc
    check = app.workshop.check(app.workshop.get(draft_id)["profile_yaml"])
    return response_json(check)


def h_workshop_build(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    if _async_requested(req):
        try:
            job = app.workshop_jobs.create(user["id"], draft_id, "build")
        except ValueError as exc:
            raise _job_error(exc) from exc
        app.db.audit(user["id"], user["username"], "workshop.build", draft_id,
                     {"async": True}, req.ip)
        return response_json({"job": job}, status=202)
    with app.gateway.user_context(user["id"]):
        result = app.workshop.build(draft_id)
    app.db.audit(user["id"], user["username"], "workshop.build", draft_id,
                 {"ok": result.get("ok"), "verified": result.get("verified")}, req.ip)
    return response_json(result)


def h_workshop_verify_rules(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    data = req.json() if req.headers.get("Content-Length") else {}
    rule_ids = data.get("rule_ids") if isinstance(data, dict) else None
    try:
        result = app.workshop.mark_rules_verified(draft_id, rule_ids)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "workshop.verify_rules", draft_id,
                 {"rules": result.get("changed")}, req.ip)
    return response_json(result)


def h_workshop_publish(app: App, req, draft_id: str):
    user = _admin(app, req)
    _draft_for(app, req, draft_id)
    data = req.json()
    try:
        result = app.workshop.publish(
            draft_id, user["username"],
            override_reason=str(data.get("override_reason") or ""),
            approvals=data.get("approvals") if isinstance(data.get("approvals"), list) else None,
            channel=str(data.get("channel") or "stable"),
            emergency_reason=str(data.get("emergency_reason") or ""))
    except ValueError as exc:
        status = 409 if "至少需要 2 名" in str(exc) else 400
        raise HTTPError(status, str(exc)) from exc
    return response_json(result)


def h_workshop_case(app: App, req, draft_id: str):
    user = _auth(app, req)
    row = _draft_for(app, req, draft_id)
    max_mb = int(app.config.get("max_upload_mb", 2048))
    fields, files = req.parse_multipart(max_mb * 1024 * 1024, max_file_bytes=max_mb * 1024 * 1024)
    if not files:
        raise HTTPError(400, "请上传案例日志包")
    expected = {
        "case_id": fields.get("case_id") or app.workshop.next_case_id(draft_id),
        "kind": fields.get("kind") or "positive",
        "description": fields.get("description") or "",
        "root_cause": fields.get("root_cause") or ("NONE" if fields.get("kind") == "negative" else ""),
        "hit_rules": [x.strip() for x in (fields.get("hit_rules") or "").split(",") if x.strip()],
        "not_hit_rules": [x.strip() for x in (fields.get("not_hit_rules") or "").split(",") if x.strip()],
        "device": fields.get("device") or "",
        "tags": [x.strip() for x in (fields.get("tags") or "").split(",") if x.strip()],
        "overwrite": fields.get("overwrite") in ("1", "true", "on"),
    }
    if fields.get("match_counts"):
        try:
            expected["match_counts"] = json.loads(fields["match_counts"])
        except json.JSONDecodeError as exc:
            raise HTTPError(400, f"match_counts 不是合法 JSON: {exc}") from exc
    if fields.get("evidence_contains"):
        try:
            expected["evidence_contains"] = json.loads(fields["evidence_contains"])
        except json.JSONDecodeError as exc:
            raise HTTPError(400, f"evidence_contains 不是合法 JSON: {exc}") from exc
    payload = [{"stored_path": str(f.path), "name": f.filename} for f in files]
    if _async_requested(req):
        try:
            job = app.workshop_jobs.create(
                user["id"], draft_id, "case", {"expected": expected}, uploads=payload)
        except ValueError as exc:
            raise _job_error(exc) from exc
        app.db.audit(user["id"], user["username"], "workshop.case", draft_id,
                     {"case": expected["case_id"], "async": True}, req.ip)
        return response_json({"job": job}, status=202)
    try:
        result = app.workshop.attach_case(draft_id, payload, expected)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "workshop.case", draft_id,
                 {"case": expected["case_id"]}, req.ip)
    return response_json(result)


def h_workshop_rules_try(app: App, req, draft_id: str):
    user = _auth(app, req)
    _draft_for(app, req, draft_id)
    try:
        result = app.workshop.try_rules(draft_id)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "workshop.rules_try", draft_id,
                 {"ok": result.get("ok"), "rules": result.get("rules")}, req.ip)
    return response_json(result)


def h_workshop_cases(app: App, req, draft_id: str):
    _draft_for(app, req, draft_id)
    return response_json({"items": app.workshop.list_cases(draft_id),
                          "next_id": app.workshop.next_case_id(draft_id)})


def h_workshop_job(app: App, req, job_id: str):
    job = app.workshop_jobs.get(job_id)
    _draft_for(app, req, job["draft_id"])
    return response_json(job)


def h_workshop_jobs(app: App, req, draft_id: str):
    _draft_for(app, req, draft_id)
    return response_json({"items": app.workshop_jobs.list_for_draft(draft_id, 20)})


# ---------------------------- 模型接入 ----------------------------


def _model_row(app: App, model_id: int) -> dict[str, Any]:
    row = app.db.one("SELECT * FROM model_configs WHERE id=?", (model_id,))
    if not row:
        raise HTTPError(404, "模型配置不存在")
    return row


def _model_public(app: App, row: dict[str, Any]) -> dict[str, Any]:
    try:
        plain = app.secret_box.decrypt(row.get("api_key") or "")
        key_view = SecretBox.mask(plain)
        has_key = bool(plain)
    except ValueError:
        key_view = "（解密失败）"
        has_key = True
    return {
        "id": row["id"], "name": row["name"], "provider": row["provider"],
        "base_url": row["base_url"], "model": row["model"],
        "api_key_masked": key_view, "has_key": has_key,
        "roles": _loads(row["roles"], []), "max_tokens": row["max_tokens"],
        "temperature": row["temperature"], "timeout_sec": row["timeout_sec"],
        "enabled": bool(row["enabled"]), "priority": row["priority"],
        "extra": _loads(row["extra"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _validate_model(app: App, data: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(data.get("name") or (existing or {}).get("name") or "").strip()
    provider = str(data.get("provider") or (existing or {}).get("provider") or "").strip()
    model = str(data.get("model") or (existing or {}).get("model") or "").strip()
    if not name:
        raise HTTPError(400, "请填写配置名称")
    if provider not in ("openai", "anthropic", "mock"):
        raise HTTPError(400, "provider 必须是 openai / anthropic / mock")
    if not model:
        raise HTTPError(400, "请填写模型名")
    base_url = str(data.get("base_url") or (existing or {}).get("base_url") or "").strip()
    if provider == "openai" and not base_url:
        base_url = str(app.config.get("personal_model_base_url") or "")
    if provider == "anthropic" and not base_url:
        base_url = "https://api.anthropic.com"
    roles = data.get("roles", (existing or {}).get("roles", []))
    if isinstance(roles, str):
        roles = [x.strip() for x in roles.split(",") if x.strip()]
    roles = [r for r in roles if r in MODEL_ROLES] or ["default"]
    try:
        max_tokens = int(data.get("max_tokens", (existing or {}).get("max_tokens", 4096)))
        timeout_sec = int(data.get("timeout_sec", (existing or {}).get("timeout_sec", 180)))
        priority = int(data.get("priority", (existing or {}).get("priority", 0)))
        temperature = float(data.get("temperature", (existing or {}).get("temperature", 0.2)))
    except (TypeError, ValueError) as exc:
        raise HTTPError(400, f"数值字段非法: {exc}") from exc
    if not (1 <= max_tokens <= 200000):
        raise HTTPError(400, "max_tokens 超出范围")
    if not (0 <= temperature <= 2):
        raise HTTPError(400, "temperature 超出范围")
    if not (5 <= timeout_sec <= 600):
        raise HTTPError(400, "timeout_sec 超出范围")
    extra = data.get("extra", (existing or {}).get("extra", {}))
    if isinstance(extra, str):
        try:
            extra = json.loads(extra) if extra.strip() else {}
        except json.JSONDecodeError as exc:
            raise HTTPError(400, f"extra 不是合法 JSON: {exc}") from exc
    if not isinstance(extra, dict):
        raise HTTPError(400, "extra 必须是 JSON 对象")
    api_key = data.get("api_key")
    if api_key is None or api_key == "":
        api_key = (existing or {}).get("api_key", "")
    else:
        api_key = app.secret_box.encrypt(str(api_key))
    if provider != "mock" and not api_key:
        raise HTTPError(400, "请填写 API Key（mock 除外）")
    enabled = data.get("enabled", (existing or {}).get("enabled", 1))
    return {
        "name": name, "provider": provider, "base_url": base_url,
        "api_key": api_key, "model": model, "roles": json.dumps(roles),
        "max_tokens": max_tokens, "temperature": temperature,
        "timeout_sec": timeout_sec, "enabled": 1 if enabled in (1, True, "1", "true") else 0,
        "priority": max(0, priority), "extra": json.dumps(extra, ensure_ascii=False),
    }


def h_models(app: App, req):
    user = _auth(app, req, csrf=False)
    if user["role"] == "reviewer":
        raise HTTPError(403, "专家账号不能查看或管理模型配置")
    rows = app.db.all("SELECT * FROM model_configs ORDER BY priority, id")
    return response_json({"items": [_model_public(app, r) for r in rows],
                          "roles": list(MODEL_ROLES)})


def h_model_create(app: App, req):
    user = _admin(app, req)
    payload = _validate_model(app, req.json())
    if app.db.one("SELECT id FROM model_configs WHERE name=?", (payload["name"],)):
        raise HTTPError(409, "配置名称已存在")
    now = iso_now()
    mid = app.db.execute(
        "INSERT INTO model_configs(name,provider,base_url,api_key,model,roles,max_tokens,"
        "temperature,timeout_sec,enabled,priority,extra,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (payload["name"], payload["provider"], payload["base_url"], payload["api_key"],
         payload["model"], payload["roles"], payload["max_tokens"], payload["temperature"],
         payload["timeout_sec"], payload["enabled"], payload["priority"],
         payload["extra"], now, now))
    app.db.audit(user["id"], user["username"], "model.create", str(mid),
                 {"name": payload["name"], "provider": payload["provider"]}, req.ip)
    return response_json({"model": _model_public(app, _model_row(app, mid))}, status=201)


def h_model_update(app: App, req, model_id: str):
    user = _admin(app, req)
    row = _model_row(app, int(model_id))
    payload = _validate_model(app, req.json(), row)
    if app.db.one("SELECT id FROM model_configs WHERE name=? AND id<>?",
                  (payload["name"], row["id"])):
        raise HTTPError(409, "配置名称已存在")
    app.db.execute(
        "UPDATE model_configs SET name=?,provider=?,base_url=?,api_key=?,model=?,roles=?,"
        "max_tokens=?,temperature=?,timeout_sec=?,enabled=?,priority=?,extra=?,updated_at=? WHERE id=?",
        (payload["name"], payload["provider"], payload["base_url"], payload["api_key"],
         payload["model"], payload["roles"], payload["max_tokens"], payload["temperature"],
         payload["timeout_sec"], payload["enabled"], payload["priority"],
         payload["extra"], iso_now(), row["id"]))
    app.db.audit(user["id"], user["username"], "model.update", str(row["id"]),
                 {"name": payload["name"]}, req.ip)
    return response_json({"model": _model_public(app, _model_row(app, row["id"]))})


def h_model_delete(app: App, req, model_id: str):
    user = _admin(app, req)
    row = _model_row(app, int(model_id))
    app.db.execute("DELETE FROM model_configs WHERE id=?", (row["id"],))
    app.db.audit(user["id"], user["username"], "model.delete", str(row["id"]),
                 {"name": row["name"]}, req.ip)
    return response_json({"ok": True})


def h_model_test(app: App, req, model_id: str):
    _admin(app, req)
    row = _model_row(app, int(model_id))
    model = dict(row)
    try:
        model["api_key"] = app.secret_box.decrypt(row["api_key"])
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    model["roles"] = _loads(row["roles"], [])
    model["extra"] = _loads(row["extra"], {})
    model["allow_private"] = bool(app.config.get("model_discovery_allow_private", False))
    result = app.gateway.test_model(model)
    return response_json(result)


def h_model_capabilities(app: App, req, model_id: str):
    user = _admin(app, req)
    row = _model_row(app, int(model_id))
    model = dict(row)
    model["extra"] = _loads(row.get("extra"), {})
    model["allow_private"] = bool(app.config.get("model_discovery_allow_private", False))
    try:
        model["api_key"] = app.secret_box.decrypt(row["api_key"]) if row.get("api_key") else ""
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    caps = app.gateway.capabilities.public(model)
    app.db.audit(user["id"], user["username"], "model.capabilities", str(row["id"]),
                 {"degraded": caps.get("degraded"), "json_mode": caps.get("json_mode")},
                 req.ip)
    return response_json({"capabilities": caps})


def h_model_discover(app: App, req):
    """列出当前 Base URL 可用的模型名，不创建/修改配置。"""
    user = _admin(app, req)
    data = req.json()
    existing = None
    if data.get("model_id"):
        try:
            existing = _model_row(app, int(data["model_id"]))
        except (TypeError, ValueError) as exc:
            raise HTTPError(400, "model_id 非法") from exc
    provider = str(data.get("provider") or (existing or {}).get("provider") or "openai")
    if provider not in ("openai", "anthropic", "mock"):
        raise HTTPError(400, "provider 必须是 openai / anthropic / mock")
    base_url = str(data.get("base_url") or (existing or {}).get("base_url") or "").strip()
    if provider == "openai" and not base_url:
        base_url = str(app.config.get("personal_model_base_url") or "")
    if provider == "anthropic" and not base_url:
        base_url = "https://api.anthropic.com"
    api_key = str(data.get("api_key") or "")
    if not api_key and existing and existing.get("api_key"):
        try:
            api_key = app.secret_box.decrypt(existing["api_key"])
        except ValueError as exc:
            raise HTTPError(400, f"API Key 解密失败: {exc}") from exc
    if provider != "mock" and not api_key:
        raise HTTPError(400, "请填写 API Key")
    if provider == "mock":
        return response_json({"models": ["mock"], "count": 1})
    extra = data.get("extra")
    if extra is None and existing:
        extra = _loads(existing.get("extra"), {})
    if extra is None:
        extra = {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra) if extra.strip() else {}
        except json.JSONDecodeError as exc:
            raise HTTPError(400, f"扩展参数不是合法 JSON: {exc}") from exc
    if not isinstance(extra, dict):
        raise HTTPError(400, "扩展参数必须是 JSON 对象")
    try:
        timeout_sec = int(data.get("timeout_sec") or 15)
    except (TypeError, ValueError) as exc:
        raise HTTPError(400, "timeout_sec 必须是整数") from exc
    model = {
        "provider": provider, "base_url": base_url, "api_key": api_key,
        "extra": extra, "timeout_sec": timeout_sec,
        "allow_private": bool(app.config.get("model_discovery_allow_private", False)),
    }
    try:
        names = list_models(model, timeout=timeout_sec)
    except OutboundPolicyError as exc:
        raise HTTPError(403, f"模型地址被安全策略拒绝: {str(exc)[:300]}") from exc
    except AIError as exc:
        text = str(exc)
        if api_key:
            text = text.replace(api_key, "***")
        if exc.http_status == 403:
            raise HTTPError(403, text[:600]) from exc
        if exc.http_status == 413:
            raise HTTPError(413, text[:600]) from exc
        if exc.http_status == 401 or "http 401" in text.lower():
            raise HTTPError(502, "上游模型服务认证失败（401），请检查 API Key") from exc
        if "timed out" in text.lower() or "timeout" in text.lower():
            raise HTTPError(504, "模型服务响应超时") from exc
        raise HTTPError(502, f"获取模型列表失败: {text[:600]}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise HTTPError(504, "模型服务响应超时") from exc
    except ValueError as exc:
        raise HTTPError(400, f"获取模型列表失败: {str(exc)[:600]}") from exc
    parsed = urlparse(base_url)
    app.db.audit(user["id"], user["username"], "model.discover", "",
                 {"provider": provider, "base_url_host": parsed.hostname or "",
                  "base_url_path": parsed.path[:300], "count": len(names)}, req.ip)
    return response_json({"models": names, "count": len(names)})

def h_model_test_draft(app: App, req):
    """Test the current form values without creating or updating a model row."""
    _admin(app, req)
    data = req.json()
    existing = None
    if data.get("model_id"):
        try:
            existing = _model_row(app, int(data["model_id"]))
        except (TypeError, ValueError) as exc:
            raise HTTPError(400, "model_id 非法") from exc
    payload = _validate_model(app, data, existing)
    if payload["provider"] != "mock" and not payload.get("api_key"):
        raise HTTPError(400, "请填写 API Key（mock 除外）")
    model = {
        "id": existing["id"] if existing else 0,
        "name": payload["name"],
        "provider": payload["provider"],
        "base_url": payload["base_url"],
        "model": payload["model"],
        "roles": _loads(payload["roles"], []),
        "max_tokens": payload["max_tokens"],
        "temperature": payload["temperature"],
        "timeout_sec": payload["timeout_sec"],
        "enabled": 1,
        "priority": payload["priority"],
        "extra": _loads(payload["extra"], {}),
        "allow_private": bool(app.config.get("model_discovery_allow_private", False)),
    }
    try:
        if payload["provider"] == "mock":
            model["api_key"] = ""
        else:
            model["api_key"] = app.secret_box.decrypt(payload["api_key"])
    except ValueError as exc:
        raise HTTPError(400, f"API Key 解密失败: {exc}") from exc
    return response_json(app.gateway.test_model(model))


# ---------------------------- 用户与审计 ----------------------------
def _user_query(req) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    q = (req.query.get("q") or "").strip()
    if q:
        clauses.append("(username LIKE ? OR display_name LIKE ? OR domain LIKE ?)")
        args.extend([f"%{q}%"] * 3)
    role = req.query.get("role")
    if role in ROLE_RANK:
        clauses.append("role=?")
        args.append(role)
    status = req.query.get("status")
    if status in ("invited", "active", "disabled", "expired"):
        if status == "expired":
            clauses.append("(status='expired' OR (status='active' AND expires_at IS NOT NULL AND expires_at<?))")
            args.append(iso_now())
        elif status == "active":
            clauses.append("status='active' AND (expires_at IS NULL OR expires_at>=?)")
            args.append(iso_now())
        else:
            clauses.append("status=?")
            args.append(status)
    return clauses, args


def h_users(app: App, req):
    _admin(app, req, csrf=False)
    page = max(1, int(req.query.get("page", 1)))
    size = min(100, max(1, int(req.query.get("page_size", 20))))
    clauses, args = _user_query(req)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = app.db.one(f"SELECT COUNT(*) AS n FROM users {where}", tuple(args))["n"]
    rows = app.db.all(
        f"SELECT * FROM users {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(args + [size, (page - 1) * size]))
    return response_json({"items": [public_user(r) for r in rows], "total": total,
                          "page": page, "page_size": size,
                          "roles": list(ROLE_RANK.keys())})


def h_user_detail(app: App, req, user_id: str):
    _admin(app, req, csrf=False)
    row = app.db.one("SELECT * FROM users WHERE id=?", (int(user_id),))
    if not row:
        raise HTTPError(404, "用户不存在")
    user = public_user(row)
    sessions = list_user_sessions(app.db, row["id"])
    audits = app.db.all(
        "SELECT id,action,target,detail_json,ip,ts FROM audit_logs WHERE user_id=? "
        "ORDER BY id DESC LIMIT 20", (row["id"],))
    last_login = app.db.one(
        "SELECT ip,detail_json FROM audit_logs WHERE user_id=? AND action='auth.login' "
        "ORDER BY id DESC LIMIT 1", (row["id"],))
    counts = {
        "tasks_created": app.db.one("SELECT COUNT(*) AS n FROM tasks WHERE user_id=?", (row["id"],))["n"],
        "reviews_submitted": app.db.one("SELECT COUNT(*) AS n FROM task_reviews WHERE reviewer_id=?",
                                        (row["id"],))["n"],
        "reviews_assigned": app.db.one("SELECT COUNT(*) AS n FROM tasks WHERE reviewer_id=?",
                                       (row["id"],))["n"],
    }
    user["last_login_ip"] = (last_login or {}).get("ip") or ""
    user["last_login_ua"] = _loads((last_login or {}).get("detail_json"), {}).get("ua") or ""
    return response_json({
        "user": user,
        "sessions": [{**s, "current": False} for s in sessions],
        "recent_audits": [{**dict(a), "detail": _loads(a["detail_json"], {})} for a in audits],
        "counts": counts,
    })


def h_user_create(app: App, req):
    actor = _admin(app, req)
    data = req.json()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip() or "bbu-skill"
    role = str(data.get("role") or "engineer")
    display_name = str(data.get("display_name") or username).strip()
    domain = str(data.get("domain") or "").strip()[:64]
    expires_at = str(data.get("expires_at") or "").strip() or None
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", username):
        raise HTTPError(400, "用户名只能包含字母/数字/._-，3–32 位")
    try:
        validate_initial_password(password, username)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    if role not in ROLE_RANK:
        raise HTTPError(400, "角色只能是 admin / reviewer / engineer")
    if app.db.one("SELECT id FROM users WHERE username=?", (username,)):
        raise HTTPError(409, "用户名已存在")
    now = iso_now()
    status = "expired" if expires_at and expires_at < now else "active"
    active = 1 if status == "active" else 0
    uid = app.db.execute(
        "INSERT INTO users(username,display_name,role,password_hash,must_change,active,created_at,"
        "status,domain,expires_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (username, display_name, role, hash_password(password), 1, active, now, status,
         domain, expires_at, now))
    app.db.audit(actor["id"], actor["username"], "user.create", username,
                 {"role": role, "domain": domain, "status": status}, req.ip)
    row = app.db.one("SELECT * FROM users WHERE id=?", (uid,))
    return response_json({"id": uid, "username": username, "role": role,
                          "user": public_user(row)}, status=201)


def h_user_update(app: App, req, user_id: str):
    actor = _admin(app, req)
    target = app.db.one("SELECT * FROM users WHERE id=?", (int(user_id),))
    if not target:
        raise HTTPError(404, "用户不存在")
    data = req.json()
    active = target["active"]
    role = target["role"]
    display_name = str(data.get("display_name", target["display_name"]))
    domain = str(data.get("domain", target.get("domain") or ""))[:64]
    expires_at = data.get("expires_at", target.get("expires_at"))
    if expires_at is not None:
        expires_at = str(expires_at).strip() or None
    status = normalize_status(target)
    if "active" in data:
        active = 1 if data["active"] in (1, True, "1", "true") else 0
        status = "active" if active else "disabled"
    if "role" in data:
        if data["role"] not in ROLE_RANK:
            raise HTTPError(400, "角色只能是 admin / reviewer / engineer")
        role = data["role"]
    if target["id"] == actor["id"] and not active:
        raise HTTPError(400, "不能停用当前登录账号")
    if status == "invited" and active and not data.get("password"):
        raise HTTPError(400, "待激活账号请通过激活码完成激活，或先设置密码")
    if target["role"] == "admin" and (role != "admin" or not active):
        admins = app.db.one(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 AND status='active'")["n"]
        if admins <= 1:
            raise HTTPError(400, "至少保留一个启用的管理员账号")
    password = data.get("password")
    if password:
        try:
            validate_password(str(password), target["username"], app.config.password_min_length)
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from exc
        app.db.execute("UPDATE users SET password_hash=?, must_change=1 WHERE id=?",
                       (hash_password(str(password)), target["id"]))
    if active and status == "disabled":
        status = "active"
    if status == "active" and expires_at and expires_at < iso_now():
        status = "expired"
        active = 0
    app.db.execute(
        "UPDATE users SET display_name=?, role=?, active=?, status=?, domain=?, "
        "expires_at=?, updated_at=? WHERE id=?",
        (display_name, role, active, status, domain, expires_at, iso_now(), target["id"]))
    revoked = 0
    if password or not active:
        revoked = delete_user_sessions(app.db, target["id"])
    app.db.audit(actor["id"], actor["username"], "user.update", target["username"],
                 {"role": role, "domain": domain, "active": active, "status": status,
                  "password_reset": bool(password), "sessions_revoked": revoked}, req.ip)
    if normalize_status(target) != status:
        labels = {"invited": "待激活", "active": "已启用", "disabled": "已停用", "expired": "已到期"}
        create_notification(app.db, target["id"], "account_status", "账号状态变化",
                            f"你的账号状态已变为{labels.get(status, status)}",
                            "/#/dashboard")
    row = app.db.one("SELECT * FROM users WHERE id=?", (target["id"],))
    return response_json({"ok": True, "sessions_revoked": revoked, "user": public_user(row)})


def h_user_force_logout(app: App, req, user_id: str):
    actor = _admin(app, req)
    target = app.db.one("SELECT * FROM users WHERE id=?", (int(user_id),))
    if not target:
        raise HTTPError(404, "用户不存在")
    revoked = delete_user_sessions(app.db, target["id"])
    app.db.audit(actor["id"], actor["username"], "user.force_logout", target["username"],
                 {"sessions_revoked": revoked}, req.ip)
    return response_json({"ok": True, "sessions_revoked": revoked})


def h_users_batch_disable(app: App, req):
    actor = _admin(app, req)
    data = req.json()
    ids = data.get("ids") if isinstance(data.get("ids"), list) else []
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError) as exc:
        raise HTTPError(400, "ids 必须是用户 ID 数组") from exc
    if not ids:
        raise HTTPError(400, "请选择要停用的用户")
    if actor["id"] in ids:
        raise HTTPError(400, "不能批量停用当前登录账号")
    targets = [app.db.one("SELECT * FROM users WHERE id=? AND active=1", (uid,)) for uid in ids]
    targets = [t for t in targets if t]
    admin_targets = [t for t in targets if t["role"] == "admin"]
    enabled_admins = app.db.one(
        "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 AND status='active'")["n"]
    if admin_targets and enabled_admins - len(admin_targets) < 1:
        raise HTTPError(400, "至少保留一个启用的管理员账号")
    count = 0
    for target in targets:
        app.db.execute("UPDATE users SET active=0, status='disabled', updated_at=? WHERE id=?",
                       (iso_now(), target["id"]))
        delete_user_sessions(app.db, target["id"])
        count += 1
    app.db.audit(actor["id"], actor["username"], "user.batch_disable", "users",
                 {"ids": ids, "disabled": count}, req.ip)
    return response_json({"ok": True, "disabled": count})


def h_invites(app: App, req):
    _admin(app, req, csrf=False)
    status = req.query.get("status", "all")
    return response_json({"items": list_invites_service(app.db, status)})


def h_invite_create(app: App, req):
    actor = _admin(app, req)
    data = req.json()
    try:
        invite, code = create_invite(
            app.db, actor["id"], str(data.get("username") or ""),
            str(data.get("display_name") or ""), str(data.get("role") or "engineer"),
            int(data.get("hours") or 24))
    except ValueError as exc:
        raise HTTPError(409 if "已存在" in str(exc) or "已有" in str(exc) else 400,
                        str(exc)) from exc
    app.db.audit(actor["id"], actor["username"], "invite.create", invite["username"],
                 {"role": invite["role"], "expires_at": invite["expires_at"]}, req.ip)
    return response_json({"invite": invite, "code": code,
                          "note": "激活码只显示一次，请通过可信渠道交给用户"}, status=201)


def h_invite_revoke(app: App, req, invite_id: str):
    actor = _admin(app, req)
    try:
        invite = revoke_invite(app.db, invite_id)
    except ValueError as exc:
        raise HTTPError(404 if "不存在" in str(exc) else 400, str(exc)) from exc
    app.db.audit(actor["id"], actor["username"], "invite.revoke", invite["username"],
                 {"invite_id": invite_id}, req.ip)
    return response_json({"ok": True, "invite_id": invite_id})


def h_activate(app: App, req):
    req.ip = _client_ip(app, req)
    _check_login_rate(app, req.ip, "activate")
    data = req.json()
    code = str(data.get("code") or "").strip()
    password = str(data.get("password") or "")
    try:
        row = activate_invite(app.db, code, password, app.config.password_min_length, req.ip)
    except ValueError as exc:
        if code:
            _record_login_failure(app, req.ip, "activate")
        app.db.audit(None, "invite", "invite.activate_failed", "", {"reason": str(exc)[:120]}, req.ip)
        raise HTTPError(400, str(exc)) from exc
    _clear_login_failures(req.ip, "activate")
    app.db.audit(row["id"], row["username"], "invite.activate", row["username"], {}, req.ip)
    return response_json({"ok": True, "username": row["username"],
                          "user": public_user(row)}, status=201)


def h_register(app: App, req):
    """免邀请码自助注册：配置开启后才生效，固定创建工程师账号。"""
    if not app.config.get("allow_self_register", False):
        raise HTTPError(403, "管理员未开启自助注册")
    req.ip = _client_ip(app, req)
    data = req.json()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        raise HTTPError(400, "请输入用户名和密码")
    # 注册只按 IP 限流，避免攻击者反复提交已存在的用户名而把该账号的登录限流打满。
    _check_login_rate(app, req.ip, "")
    try:
        row = register_user(app.db, username, password,
                            str(data.get("display_name") or ""),
                            str(data.get("domain") or ""),
                            app.config.password_min_length)
    except ValueError as exc:
        _record_login_failure(app, req.ip, "")
        app.db.audit(None, "register", "auth.register_failed", username,
                     {"reason": str(exc)[:120]}, req.ip)
        raise HTTPError(409 if "已存在" in str(exc) else 400, str(exc)) from exc
    _clear_login_failures(req.ip, username)
    app.db.audit(row["id"], row["username"], "auth.register", row["username"],
                 {"domain": row.get("domain") or ""}, req.ip)
    return response_json({"ok": True, "username": row["username"],
                          "user": public_user(row)}, status=201)


def h_profile(app: App, req):
    user = _auth(app, req, csrf=False)
    row = app.db.one("SELECT * FROM users WHERE id=?", (user["id"],))
    return response_json({"user": public_user(row)})


def _personal_model_view(app: App, user: dict[str, Any]) -> dict[str, Any]:
    models = [str(x) for x in (app.config.get("personal_model_models") or []) if str(x).strip()]
    row = app.db.one("SELECT * FROM user_model_configs WHERE user_id=?", (user["id"],))
    masked = ""
    if row:
        try:
            masked = SecretBox.mask(app.secret_box.decrypt(row.get("api_key") or ""))
        except ValueError:
            masked = "（解密失败）"
    status = app.gateway.model_status(user["id"])
    return {
        "configured": bool(row),
        "model": (row or {}).get("model") or str(app.config.get("personal_model_default") or ""),
        "base_url": (row or {}).get("base_url") or str(app.config.get("personal_model_base_url") or ""),
        "api_key_masked": masked,
        "models": models,
        "maas_url": str(app.config.get("personal_api_key_url") or ""),
        "status": status,
        "updated_at": (row or {}).get("updated_at"),
    }


def h_profile_model_key(app: App, req):
    user = _auth(app, req, csrf=False)
    return response_json(_personal_model_view(app, user))


def h_profile_model_key_update(app: App, req):
    user = _auth(app, req)
    data = req.json()
    model = str(data.get("model") or app.config.get("personal_model_default") or "").strip()
    allowed = [str(x) for x in (app.config.get("personal_model_models") or []) if str(x).strip()]
    if model not in allowed:
        raise HTTPError(400, "不支持的 MaaS 模型：" + model)
    row = app.db.one("SELECT * FROM user_model_configs WHERE user_id=?", (user["id"],))
    api_key = data.get("api_key")
    if api_key is None or str(api_key).strip() == "":
        if not row:
            raise HTTPError(400, "请填写个人 MaaS API Key")
        encrypted = row["api_key"]
    else:
        plain = str(api_key).strip()
        if len(plain) < 8:
            raise HTTPError(400, "API Key 长度异常")
        encrypted = app.secret_box.encrypt(plain)
    now = iso_now()
    base_url = str(app.config.get("personal_model_base_url") or "").rstrip("/")
    if row:
        app.db.execute(
            "UPDATE user_model_configs SET model=?,base_url=?,api_key=?,updated_at=? WHERE user_id=?",
            (model, base_url, encrypted, now, user["id"]))
        action = "profile.model_key.update"
    else:
        app.db.execute(
            "INSERT INTO user_model_configs(user_id,name,provider,base_url,api_key,model,"
            "max_tokens,temperature,timeout_sec,enabled,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], "个人 MaaS", "openai", base_url, encrypted, model,
             4096, 0.2, 180, 1, "{}", now, now))
        action = "profile.model_key.create"
    app.db.audit(user["id"], user["username"], action, user["username"],
                 {"model": model, "base_url": base_url}, req.ip)
    return response_json(_personal_model_view(app, user))


def h_profile_model_key_delete(app: App, req):
    user = _auth(app, req)
    row = app.db.one("SELECT id FROM user_model_configs WHERE user_id=?", (user["id"],))
    if row:
        app.db.execute("DELETE FROM user_model_configs WHERE user_id=?", (user["id"],))
        app.db.audit(user["id"], user["username"], "profile.model_key.delete",
                     user["username"], {}, req.ip)
    return response_json({"ok": True, "configured": False})


def h_profile_model_key_test(app: App, req):
    user = _auth(app, req)
    row = app.db.one("SELECT * FROM user_model_configs WHERE user_id=?", (user["id"],))
    if not row:
        raise HTTPError(400, "尚未配置个人 MaaS API Key")
    model = dict(row)
    model["roles"] = ["default"]
    model["extra"] = _loads(row.get("extra"), {}) or {}
    model["allow_private"] = bool(app.config.get("model_discovery_allow_private", False))
    try:
        model["api_key"] = app.secret_box.decrypt(row["api_key"])
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    result = app.gateway.test_model(model)
    app.db.audit(user["id"], user["username"], "profile.model_key.test", user["username"],
                 {"ok": bool(result.get("ok")), "model": row.get("model")}, req.ip)
    return response_json(result)


def h_profile_update(app: App, req):
    user = _auth(app, req)
    data = req.json()
    display_name = str(data.get("display_name") or user["display_name"]).strip()[:64]
    prefs = data.get("notification_prefs")
    if prefs is not None and not isinstance(prefs, dict):
        raise HTTPError(400, "notification_prefs 必须是对象")
    prefs_json = json.dumps(prefs or {}, ensure_ascii=False)
    app.db.execute("UPDATE users SET display_name=?, notification_prefs=?, updated_at=? WHERE id=?",
                   (display_name, prefs_json, iso_now(), user["id"]))
    app.db.audit(user["id"], user["username"], "profile.update", user["username"],
                 {"display_name": display_name}, req.ip)
    row = app.db.one("SELECT * FROM users WHERE id=?", (user["id"],))
    return response_json({"ok": True, "user": public_user(row)})


def _audit_filters(req) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    start = (req.query.get("start") or "").strip()
    end = (req.query.get("end") or "").strip()
    if start:
        clauses.append("ts>=?")
        args.append(start + "T00:00:00Z" if len(start) == 10 else start)
    if end:
        clauses.append("ts<=?")
        args.append(end + "T23:59:59Z" if len(end) == 10 else end)
    username = (req.query.get("username") or "").strip()
    if username:
        clauses.append("username LIKE ?")
        args.append(f"%{username}%")
    action = (req.query.get("action") or "").strip()
    if action:
        clauses.append("action LIKE ?")
        args.append(f"%{action}%")
    target = (req.query.get("target") or "").strip()
    if target:
        clauses.append("target LIKE ?")
        args.append(f"%{target}%")
    result = (req.query.get("result") or "").strip().lower()
    failed = ("(action LIKE '%fail%' OR action LIKE '%reject%' OR action LIKE '%deny%' "
              "OR action LIKE '%cancel%' OR detail_json LIKE '%error%')")
    if result == "failed":
        clauses.append(failed)
    elif result == "ok":
        clauses.append("NOT " + failed)
    return clauses, args


def h_audit(app: App, req):
    _admin(app, req, csrf=False)
    page = max(1, int(req.query.get("page", 1)))
    size = min(100, max(1, int(req.query.get("page_size", 30))))
    clauses, args = _audit_filters(req)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = app.db.one(f"SELECT COUNT(*) AS n FROM audit_logs {where}", tuple(args))["n"]
    rows = app.db.all(
        f"SELECT id,user_id,username,action,target,detail_json,ip,ts FROM audit_logs {where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?", tuple(args + [size, (page - 1) * size]))
    return response_json({"items": [{**dict(r), "detail": _loads(r["detail_json"], {})} for r in rows],
                          "total": total, "page": page, "page_size": size})


def h_audit_export(app: App, req):
    actor = _admin(app, req, csrf=False)
    clauses, args = _audit_filters(req)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = app.db.all(
        f"SELECT id,username,action,target,detail_json,ip,ts FROM audit_logs {where} "
        "ORDER BY id DESC LIMIT 10000", tuple(args))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "时间", "用户", "动作", "对象", "详情", "IP"])
    for row in rows:
        writer.writerow([row["id"], row["ts"], row["username"] or "", row["action"],
                         row["target"] or "", row["detail_json"], row["ip"] or ""])
    app.db.audit(actor["id"], actor["username"], "audit.export", "audit_logs",
                 {"rows": len(rows), "filters": dict(req.query)}, req.ip)
    body = ("﻿" + buf.getvalue()).encode("utf-8")
    name = "audit-export.csv"
    return 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store",
    }, body


# ---------------------------- 平台助手 / MCP ----------------------------
def h_assistant_status(app: App, req):
    user = _auth(app, req, csrf=False)
    return response_json({"status": app.assistant.status(user),
                          "suggestions": app.assistant.suggestions(user)})


def h_assistant_suggestions(app: App, req):
    user = _auth(app, req, csrf=False)
    return response_json({"items": app.assistant.suggestions(user)})


def h_assistant_chat(app: App, req):
    user = _auth(app, req)
    data = req.json()
    try:
        result = app.assistant.chat(user, data.get("messages"), req.ip)
    except AssistantError as exc:
        raise HTTPError(exc.status, str(exc)) from exc
    return response_json(result)


def h_assistant_confirm(app: App, req, action_id: str):
    user = _auth(app, req)
    try:
        action = app.assistant.action_public(user, action_id)
    except AssistantError as exc:
        raise HTTPError(exc.status, str(exc)) from exc
    data = req.json()
    if action.get("requires_password"):
        _verify_admin_password(app, req, user, str(data.get("password") or ""),
                               rate_key="assistant_action")
    try:
        result = app.assistant.confirm_action(user, action_id, req.ip)
    except AssistantError as exc:
        raise HTTPError(exc.status, str(exc)) from exc
    return response_json(result)


def h_assistant_cancel(app: App, req, action_id: str):
    user = _auth(app, req)
    return response_json(app.assistant.cancel_action(user, action_id))


def h_mcp_http(app: App, req):
    # 本机限制与 Bearer Token 在 MCPService 内校验；这里不使用网页会话。
    return app.mcp.handle_http(req)


def h_mcp_info(app: App, req):
    user = _auth(app, req, csrf=False)
    return response_json({"mcp": app.mcp.token_info(user)})


def h_mcp_token_create(app: App, req):
    user = _auth(app, req)
    data = req.json()
    return response_json({"mcp": app.mcp.rotate_token(user, str(data.get("mode") or "read"))},
                         status=201)


def h_mcp_token_delete(app: App, req):
    user = _auth(app, req)
    return response_json({"mcp": app.mcp.revoke_token(user)})


# ---------------------------- 系统信息 ----------------------------
def _verify_admin_password(app: App, req, user: dict[str, Any], password: str,
                           rate_key: str = "update") -> None:
    if not password:
        raise HTTPError(400, "请重新输入管理员密码")
    req.ip = _client_ip(app, req)
    key = f"{rate_key}:{user['username']}"
    _check_login_rate(app, req.ip, key)
    row = app.db.one("SELECT * FROM users WHERE id=?", (user["id"],))
    if not verify_password(password, row["password_hash"]):
        _record_login_failure(app, req.ip, key)
        raise HTTPError(403, "管理员密码不正确")
    _clear_login_failures(req.ip, key)


def _update_admin(app: App, req, need_password: bool = False) -> dict[str, Any]:
    user = _admin(app, req)
    if need_password:
        data = req.json()
        _verify_admin_password(app, req, user, str(data.get("password") or ""))
    return user


def _update_error(exc: Exception) -> HTTPError:
    status = 409 if isinstance(exc, updater.UpdateConflict) else 400
    return HTTPError(status, str(exc))


def h_update_upload(app: App, req):
    user = _admin(app, req)
    max_mb = int(app.config.get("max_update_mb", 512))
    fields, uploads = req.parse_multipart(max_mb * 1024 * 1024 + 16 * 1024 * 1024,
                                          max_file_bytes=max_mb * 1024 * 1024)
    if not uploads:
        raise HTTPError(400, "请选择 .tar.gz / .tgz / .zip 离线升级包")
    item = uploads[0]
    try:
        status = updater.create_job(
            app, item.path, item.filename,
            expected_sha256=str(fields.get("sha256") or ""),
            allow_same=fields.get("allow_same_version") in ("1", "true", "on"))
    except Exception as exc:
        raise _update_error(exc) from exc
    finally:
        for item in uploads:
            try:
                os.unlink(item.path)
            except OSError:
                pass
    app.db.audit(user["id"], user["username"], "system.update_upload", status["id"],
                 {"target": status.get("target_version"), "sha256": status.get("package_sha256"),
                  "size": status.get("size")}, req.ip)
    return response_json({"update": status}, status=201)


def h_update_list(app: App, req):
    _admin(app, req, csrf=False)
    active = updater.active_execution(app)
    return response_json({
        "current_version": updater.current_version(),
        "items": updater.history_public(app),
        "active": active["id"] if active else None,
        "running_tasks": updater.running_tasks(app),
        "disk_free_mb": updater.disk_free(app) // (1024 * 1024),
        "max_update_mb": int(app.config.get("max_update_mb", 512)),
        "supported": updater.supported(app),
        "mode": updater.apply_mode(app),
        "rollback_supported": updater.rollback_supported(app),
        "pending_update": (updater.pending_update(app) or {}).get("job_id") or "",
        "helper": ("Windows 网页上传 + 重启生效" if updater.apply_mode(app) == "restart"
                   else "自定义升级助手" if app.config.get("upgrade_helper_cmd")
                   else "root/systemd 一次性助手"),
    })


def h_update_detail(app: App, req, update_id: str):
    _admin(app, req, csrf=False)
    try:
        return response_json({"update": updater.status_public(app, update_id)})
    except Exception as exc:
        raise _update_error(exc) from exc


def h_update_verify(app: App, req, update_id: str):
    user = _admin(app, req)
    data = req.json()
    try:
        status = updater.verify_job(app, update_id,
                                    expected_sha256=str(data.get("sha256") or ""),
                                    allow_same=bool(data.get("allow_same_version")))
    except Exception as exc:
        raise _update_error(exc) from exc
    app.db.audit(user["id"], user["username"], "system.update_verify", update_id,
                 {"sha256": status.get("package_sha256")}, req.ip)
    return response_json({"update": status})


def h_update_apply(app: App, req, update_id: str):
    user = _update_admin(app, req, need_password=True)
    try:
        status = updater.apply_job(app, update_id)
    except Exception as exc:
        raise _update_error(exc) from exc
    app.db.audit(user["id"], user["username"], "system.update_apply", update_id,
                  {"target": status.get("target_version"), "sha256": status.get("package_sha256")},
                  req.ip)
    return response_json({"update": status}, status=202)


def h_update_rollback(app: App, req):
    user = _update_admin(app, req, need_password=True)
    data = req.json()
    try:
        status = updater.rollback_job(app, job_id=str(data.get("job_id") or ""),
                                      backup_dir=str(data.get("backup_dir") or ""))
    except Exception as exc:
        raise _update_error(exc) from exc
    app.db.audit(user["id"], user["username"], "system.update_rollback", status["id"],
                 {"backup": status.get("backup_dir"), "target": status.get("target_version")},
                 req.ip)
    return response_json({"update": status}, status=202)


def _migration_error(exc: Exception) -> HTTPError:
    status = 409 if isinstance(exc, MigrationConflict) else 400
    return HTTPError(status, str(exc))


def _migration_passphrase(value: Any) -> str:
    phrase = str(value or "")
    if len(phrase) < 12:
        raise HTTPError(400, "迁移口令至少 12 位")
    if len(phrase) > 256 or "\x00" in phrase or "\n" in phrase or "\r" in phrase:
        raise HTTPError(400, "迁移口令格式非法")
    return phrase


def h_migrations(app: App, req):
    _admin(app, req, csrf=False)
    work = Path(app.config.work_dir)
    usage = shutil.disk_usage(work if work.exists() else app.config.data_dir)
    return response_json({
        "items": list_jobs(app),
        "current_work_dir": str(work),
        "paths": {
            "data": str(app.config.data_dir),
            "skills": str(app.config.skills_dir),
            "cases": str(app.config.cases_dir),
        },
        "max_migration_mb": int(app.config.get("max_migration_mb", 4096)),
        "allowed_roots": allowed_work_roots(app),
        "disk_free_mb": usage.free // (1024 * 1024),
        "requires_restart_after_prepare": os.name == "nt",
        "helper": "Windows 重启激活" if os.name == "nt" else "root/systemd 维护助手",
        "workdir_supported": workdir_supported(app),
        "active_job": app.migration_gate.job_id if app.migration_gate.active else "",
        "input_policy": "原始上传、案例输入和解压数据不导出；迁移后历史任务只保留元数据、结论、报告和证据。",
    })


def h_migration_preflight(app: App, req):
    _admin(app, req)
    data = req.json()
    try:
        target = validate_target_root(app, str(data.get("target_root") or ""))
        free = shutil.disk_usage(
            next((p for p in [target, *target.parents] if p.exists()), app.config.data_dir)).free
        assert_idle(app)
    except Exception as exc:
        raise _migration_error(exc) from exc
    return response_json({
        "ok": True,
        "target_root": str(target),
        "disk_free_mb": free // (1024 * 1024),
        "allowed_roots": allowed_work_roots(app),
        "note": "预检只验证当前进程可见条件，Linux 最终仍由 root 助手校验路径和权限。",
    })


def h_migration_workspace(app: App, req):
    user = _update_admin(app, req, need_password=True)
    data = req.json()
    try:
        target = validate_target_root(app, str(data.get("target_root") or ""))
        assert_idle(app)
        status = app.migration.start_workspace(target, user["username"])
    except Exception as exc:
        raise _migration_error(exc) from exc
    app.db.audit(user["id"], user["username"], "migration.workspace_request", status["id"],
                 {"target_root": str(target)}, req.ip)
    return response_json({"migration": status}, status=202)


def h_migration_export(app: App, req):
    user = _update_admin(app, req, need_password=True)
    data = req.json()
    phrase = _migration_passphrase(data.get("passphrase"))
    try:
        assert_idle(app)
        status = app.migration.start_export(phrase, user["username"])
    except Exception as exc:
        raise _migration_error(exc) from exc
    app.db.audit(user["id"], user["username"], "migration.export_request", status["id"],
                 {"cipher": "chacha20-poly1305"}, req.ip)
    return response_json({"migration": status}, status=202)


def h_migration_import(app: App, req):
    user = _admin(app, req)
    max_mb = int(app.config.get("max_migration_mb", 4096))
    fields, uploads = req.parse_multipart(max_mb * 1024 * 1024 + 32 * 1024 * 1024,
                                          max_file_bytes=max_mb * 1024 * 1024)
    try:
        _verify_admin_password(app, req, user, str(fields.get("password") or ""), "migration")
        passphrase = _migration_passphrase(fields.get("passphrase"))
        if len(uploads) != 1:
            raise HTTPError(400, "请选择一个 .bbumig 迁移包")
        item = uploads[0]
        if not item.filename.lower().endswith(".bbumig"):
            raise HTTPError(400, "迁移包必须使用 .bbumig 后缀")
        assert_idle(app)
        status = app.migration.start_import(item.path, passphrase, user["username"])
        app.db.audit(user["id"], user["username"], "migration.import_request", status["id"],
                     {"size": item.size, "filename": item.filename}, req.ip)
        return response_json({"migration": status}, status=202)
    except Exception as exc:
        if isinstance(exc, HTTPError):
            raise
        raise _migration_error(exc) from exc
    finally:
        for item in uploads:
            try:
                os.unlink(item.path)
            except OSError:
                pass


def h_migration_detail(app: App, req, migration_id: str):
    _admin(app, req, csrf=False)
    try:
        return response_json({"migration": public_status(app, migration_id)})
    except Exception as exc:
        raise _migration_error(exc) from exc


def h_migration_download(app: App, req, migration_id: str):
    _admin(app, req, csrf=False)
    try:
        status = public_status(app, migration_id)
        if status.get("kind") != "export" or status.get("phase") != "ready":
            raise MigrationError("迁移包尚未生成")
        path = find_job_file(app, migration_id, "export.bbumig")
        expected = str(status.get("package_sha256") or "")
        if expected and sha256_file(path) != expected:
            raise MigrationError("迁移包校验失败，请重新导出")
        name = str(status.get("file_name") or f"bbu-migration-{migration_id}.bbumig")
        name = safe_filename(name)[:120]
        return response_file(path, "application/octet-stream", name, inline=False)
    except Exception as exc:
        raise _migration_error(exc) from exc


def h_migration_delete(app: App, req, migration_id: str):
    user = _update_admin(app, req, need_password=True)
    try:
        delete_job(app, migration_id)
    except Exception as exc:
        raise _migration_error(exc) from exc
    app.db.audit(user["id"], user["username"], "migration.delete", migration_id, {}, req.ip)
    return response_json({"ok": True, "id": migration_id})


def h_runtime_config(app: App, req):
    user = _auth(app, req, csrf=False)
    if user["role"] != "admin":
        raise HTTPError(403, "仅管理员可修改运行配置")
    return response_json({
        "items": app.config.runtime_items(),
        "config_file": str(app.config.config_file),
        "backup_file": str(app.config.config_file) + ".bak",
        "restart_required": True,
    })


def h_runtime_config_save(app: App, req):
    user = _auth(app, req)
    if user["role"] != "admin":
        raise HTTPError(403, "仅管理员可修改运行配置")
    data = req.json()
    values = data.get("values") if isinstance(data.get("values"), dict) else data
    try:
        saved = app.config.save_runtime(values)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from exc
    app.db.audit(user["id"], user["username"], "system.runtime_config", "",
                 {"keys": list(values)}, req.ip)
    return response_json({"ok": True, "saved": saved,
                          "restart_required": any(x.get("restart_required") for x in saved)})


def h_system_restart(app: App, req):
    user = _auth(app, req)
    if user["role"] != "admin":
        raise HTTPError(403, "仅管理员可重启服务")
    blockers = []
    stats = app.worker.stats()
    if stats.get("running") or stats.get("queued"):
        blockers.append(f"诊断任务运行 {stats.get('running', 0)} / 排队 {stats.get('queued', 0)}")
    active_workshop = app.db.one(
        "SELECT COUNT(*) AS n FROM workshop_jobs WHERE status IN ('queued','running')")["n"]
    if active_workshop:
        blockers.append(f"工坊任务 {active_workshop}")
    active_storage = app.db.one(
        "SELECT COUNT(*) AS n FROM storage_jobs WHERE status IN ('queued','running')")["n"]
    if active_storage:
        blockers.append(f"空间释放任务 {active_storage}")
    if app.migration.gate.active:
        blockers.append("工作目录迁移或导出")
    updater_job = updater.active_execution(app)
    if updater_job:
        blockers.append(f"网页升级 {updater_job.get('id', '')}")
    evaluation = app.db.one("SELECT COUNT(*) AS n FROM evaluations WHERE status='running'")["n"]
    if evaluation:
        blockers.append(f"评测 {evaluation}")
    if blockers:
        raise HTTPError(409, "存在运行中的任务，重启会中断：" + "；".join(blockers) + "。请等待完成或先取消。",
                        {"blockers": blockers})
    app.db.audit(user["id"], user["username"], "system.restart", "", {}, req.ip)
    try:
        app.request_restart(f"管理员 {user['username']} 网页重启")
    except RuntimeError as exc:
        raise HTTPError(409, str(exc)) from exc
    return response_json({"ok": True, "restarting": True,
                          "message": "服务正在重新加载配置，约 3–8 秒后自动恢复"},
                          status=202)


def h_system_info(app: App, req):
    user = _auth(app, req, csrf=False)
    import platform as _platform
    skills = app.db.all("SELECT status, COUNT(*) AS n FROM skills GROUP BY status")
    return response_json({
        "version": __version__, "brand": app.config.get("brand"),
        "python": _platform.python_version(), "platform": _platform.platform(),
        "uptime_sec": round(time.time() - app.start_time, 1),
        "paths": {
            "work_dir": str(app.config.work_dir),
            "data": str(app.config.data_dir),
            "skills": str(app.config.skills_dir),
            "cases": str(app.config.cases_dir),
            "meta_skill": str(app.config.meta_skill_dir),
            "updates": str(app.config.data_dir.parent / "updates"),
        },
        "skills": {r["status"]: r["n"] for r in skills},
        "workers": app.worker.stats(),
        "config": app.config.public(),
        "demo_installed": app.db.setting("demo_installed") == "1",
        "role": user["role"],
    })
