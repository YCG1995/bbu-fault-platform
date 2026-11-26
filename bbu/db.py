"""SQLite 数据层：WAL、忙等待、行工厂、事务。所有写操作串行化由 SQLite 自身保证。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .util import utc_now

SCHEMA_VERSION = 22

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'engineer',
  password_hash TEXT NOT NULL,
  must_change INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_login_at TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  expires_at TEXT,
  invited_by INTEGER,
  invited_at TEXT,
  domain TEXT NOT NULL DEFAULT '',
  updated_at TEXT,
  notification_prefs TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  ip TEXT,
  ua TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_invites (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'engineer',
  code_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by INTEGER,
  used_at TEXT,
  used_by INTEGER,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_invites_expiry ON user_invites(expires_at, used_at, revoked_at);

CREATE TABLE IF NOT EXISTS mcp_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL DEFAULT 'read',
  created_at TEXT NOT NULL,
  last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_hash ON mcp_tokens(token_hash);

CREATE TABLE IF NOT EXISTS task_drafts (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT 'fast',
  skill_name TEXT NOT NULL DEFAULT '',
  model_override INTEGER,
  include_canary INTEGER NOT NULL DEFAULT 0,
  files_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_drafts_user ON task_drafts(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'system',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  read_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at, created_at DESC);

CREATE TABLE IF NOT EXISTS model_configs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  provider TEXT NOT NULL,
  base_url TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  roles TEXT NOT NULL DEFAULT '[]',
  max_tokens INTEGER NOT NULL DEFAULT 4096,
  temperature REAL NOT NULL DEFAULT 0.2,
  timeout_sec INTEGER NOT NULL DEFAULT 180,
  enabled INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 0,
  extra TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_model_configs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT '个人 MaaS',
  provider TEXT NOT NULL DEFAULT 'openai',
  base_url TEXT NOT NULL,
  api_key TEXT NOT NULL,
  model TEXT NOT NULL,
  max_tokens INTEGER NOT NULL DEFAULT 4096,
  temperature REAL NOT NULL DEFAULT 0.2,
  timeout_sec INTEGER NOT NULL DEFAULT 180,
  enabled INTEGER NOT NULL DEFAULT 1,
  extra TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_model_user ON user_model_configs(user_id, enabled);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  title_source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT NOT NULL DEFAULT 'queued',
  progress INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'fast',
  model_override INTEGER,
  verdict_json TEXT,
  result_json TEXT,
  report_md TEXT,
  report_html TEXT,
  error TEXT,
  input_json TEXT NOT NULL DEFAULT '[]',
  inputs_state TEXT NOT NULL DEFAULT 'available',
  options_json TEXT NOT NULL DEFAULT '{}',
  stats_json TEXT NOT NULL DEFAULT '{}',
  resume_count INTEGER NOT NULL DEFAULT 0,
  continuation_count INTEGER NOT NULL DEFAULT 0,
  session_budget_json TEXT NOT NULL DEFAULT '{}',
  finish_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  reviewer_id INTEGER,
  reviewer_assigned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS task_skills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  skill_version TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  score REAL NOT NULL DEFAULT 0,
  stage TEXT,
  verdict_json TEXT,
  summary_json TEXT,
  result_json TEXT,
  report_md_path TEXT,
  report_html_path TEXT,
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  stats_json TEXT NOT NULL DEFAULT '{}',
  invalidated INTEGER NOT NULL DEFAULT 0,
  UNIQUE(task_id, skill_name)
);
CREATE INDEX IF NOT EXISTS idx_task_skills_task ON task_skills(task_id);

CREATE TABLE IF NOT EXISTS task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  stage TEXT,
  message TEXT NOT NULL,
  data_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, seq);

CREATE TABLE IF NOT EXISTS evidence_events (
  evidence_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  source_json TEXT NOT NULL DEFAULT '{}',
  entity_json TEXT NOT NULL DEFAULT '{}',
  event_time_json TEXT NOT NULL DEFAULT '{}',
  kind TEXT NOT NULL DEFAULT 'log',
  name TEXT NOT NULL DEFAULT 'log.observation',
  value_json TEXT NOT NULL DEFAULT '{}',
  excerpt TEXT NOT NULL DEFAULT '',
  parser_json TEXT NOT NULL DEFAULT '{}',
  trust_json TEXT NOT NULL DEFAULT '{}',
  content_sha256 TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_session ON evidence_events(session_id, created_at, evidence_id);

CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  event_key TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(task_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_event_key
  ON session_events(task_id, event_key) WHERE event_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_supplements (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL DEFAULT '',
  answers_json TEXT NOT NULL DEFAULT '{}',
  files_json TEXT NOT NULL DEFAULT '[]',
  invalidated_skills_json TEXT NOT NULL DEFAULT '[]',
  input_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(task_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_task_supplements_task ON task_supplements(task_id, revision);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_supplements_revision ON task_supplements(task_id, revision);

CREATE TABLE IF NOT EXISTS skills (
  name TEXT PRIMARY KEY,
  title TEXT,
  version TEXT,
  category TEXT,
  description TEXT,
  path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ready',
  verified INTEGER NOT NULL DEFAULT 0,
  execution_mode TEXT,
  profile_json TEXT NOT NULL DEFAULT '{}',
  components_json TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT 'imported',
  channel TEXT NOT NULL DEFAULT 'stable',
  health_json TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_name TEXT NOT NULL,
  version TEXT NOT NULL,
  path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'built',
  build_json TEXT NOT NULL DEFAULT '{}',
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(skill_name, version)
);

CREATE TABLE IF NOT EXISTS workshop_drafts (
  id TEXT PRIMARY KEY,
  owner_id INTEGER,
  name TEXT NOT NULL,
  title TEXT NOT NULL,
  fault_name TEXT,
  category TEXT,
  status TEXT NOT NULL DEFAULT 'editing',
  profile_yaml TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  source_text TEXT NOT NULL DEFAULT '',
  recipe_id TEXT NOT NULL DEFAULT '',
  analysis_json TEXT NOT NULL DEFAULT '{}',
  check_json TEXT NOT NULL DEFAULT '{}',
  build_json TEXT NOT NULL DEFAULT '{}',
  log_path TEXT,
  case_path TEXT,
  skill_version TEXT,
  source_task_id TEXT,
  name_auto INTEGER NOT NULL DEFAULT 0,
  name_locked INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workshop_jobs (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT NOT NULL DEFAULT 'queued',
  progress INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  args_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workshop_jobs_draft ON workshop_jobs(draft_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workshop_jobs_status ON workshop_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS storage_jobs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT NOT NULL DEFAULT 'queued',
  progress INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '{}',
  summary_json TEXT NOT NULL DEFAULT '{}',
  freed_bytes INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_storage_jobs_task ON storage_jobs(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS plan_experiences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL UNIQUE,
  skill_name TEXT,
  root_cause_name TEXT,
  status TEXT,
  confidence TEXT,
  description TEXT NOT NULL,
  hints_json TEXT NOT NULL DEFAULT '[]',
  plan_json TEXT NOT NULL DEFAULT '{}',
  outcome_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiences_root ON plan_experiences(root_cause_name);
CREATE INDEX IF NOT EXISTS idx_experiences_created ON plan_experiences(created_at DESC);

CREATE TABLE IF NOT EXISTS calibration_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  split TEXT NOT NULL,
  sample_count INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calibration_created ON calibration_snapshots(created_at DESC);

CREATE TABLE IF NOT EXISTS case_matrix_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_name TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_matrix_skill ON case_matrix_snapshots(skill_name, created_at DESC);

CREATE TABLE IF NOT EXISTS workshop_patches (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL,
  path TEXT NOT NULL,
  old_value_json TEXT NOT NULL DEFAULT 'null',
  new_value_json TEXT NOT NULL DEFAULT 'null',
  actor TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workshop_patches_draft ON workshop_patches(draft_id, created_at DESC);

CREATE TABLE IF NOT EXISTS experience_candidates (
  id TEXT PRIMARY KEY,
  input_hash TEXT NOT NULL,
  skill_name TEXT NOT NULL DEFAULT '',
  root_cause_name TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'new_variant',
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  reviewed_by TEXT NOT NULL DEFAULT '',
  review_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  reviewed_at TEXT,
  UNIQUE(input_hash, skill_name, root_cause_name)
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON experience_candidates(status, created_at DESC);

CREATE TABLE IF NOT EXISTS recovery_runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  before_json TEXT NOT NULL DEFAULT '{}',
  after_json TEXT NOT NULL DEFAULT '{}',
  comparison_json TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT 'readonly_receipt',
  requested_by TEXT NOT NULL DEFAULT '',
  reviewed_by TEXT NOT NULL DEFAULT '',
  learning_eligible INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  observed_at TEXT,
  reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recovery_task ON recovery_runs(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_traces (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'shadow',
  action_count INTEGER NOT NULL DEFAULT 0,
  stop_reason TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_traces_task ON agent_traces(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  username TEXT,
  action TEXT NOT NULL,
  target TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  ip TEXT,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_name TEXT NOT NULL,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  expected_root_cause TEXT,
  description TEXT,
  inputs_state TEXT NOT NULL DEFAULT 'available',
  created_at TEXT NOT NULL,
  UNIQUE(skill_name, case_id)
);

-- 真实案例：先允许 unlabeled 入库，补标签后无需迁移即可进入评测。
CREATE TABLE IF NOT EXISTS real_cases (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  site TEXT NOT NULL DEFAULT '',
  device_type TEXT NOT NULL DEFAULT '',
  software_version TEXT NOT NULL DEFAULT '',
  split TEXT NOT NULL DEFAULT 'unlabeled',
  status TEXT NOT NULL DEFAULT 'unlabeled',
  expected_roots_json TEXT NOT NULL DEFAULT '[]',
  excluded_roots_json TEXT NOT NULL DEFAULT '[]',
  labels_json TEXT NOT NULL DEFAULT '{}',
  origin TEXT NOT NULL DEFAULT 'real',
  input_paths_json TEXT NOT NULL DEFAULT '[]',
  inputs_state TEXT NOT NULL DEFAULT 'available',
  dataset_sha256 TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_real_cases_split ON real_cases(split, status);

CREATE TABLE IF NOT EXISTS case_label_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  actor_id INTEGER,
  actor_name TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  old_json TEXT NOT NULL DEFAULT '{}',
  new_json TEXT NOT NULL DEFAULT '{}',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_history_case ON case_label_history(case_id, id DESC);

CREATE TABLE IF NOT EXISTS evaluations (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  split TEXT NOT NULL DEFAULT 'test',
  status TEXT NOT NULL DEFAULT 'running',
  config_json TEXT NOT NULL DEFAULT '{}',
  summary_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS evaluation_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluation_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  case_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  expected_json TEXT NOT NULL DEFAULT '[]',
  predicted_json TEXT NOT NULL DEFAULT '{}',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  duration_sec REAL,
  error TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(evaluation_id, case_id)
);
CREATE INDEX IF NOT EXISTS idx_eval_cases_eval ON evaluation_cases(evaluation_id);

-- 专家复核：确认/改判/驳回都必须留痕，未复核经验不参与规划。
CREATE TABLE IF NOT EXISTS task_reviews (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL UNIQUE,
  reviewer_id INTEGER,
  reviewer_name TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL,
  original_root TEXT NOT NULL DEFAULT '',
  original_skill TEXT NOT NULL DEFAULT '',
  original_skill_version TEXT NOT NULL DEFAULT '',
  corrected_root TEXT NOT NULL DEFAULT '',
  corrected_skill TEXT NOT NULL DEFAULT '',
  comment TEXT NOT NULL DEFAULT '',
  action_result TEXT NOT NULL DEFAULT 'unknown',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'candidate',
  real_case_id TEXT,
  promoted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  session_revision INTEGER NOT NULL DEFAULT 0,
  tool_name TEXT NOT NULL,
  tool_version TEXT NOT NULL DEFAULT '',
  manifest_hash TEXT NOT NULL DEFAULT '',
  arguments_json TEXT NOT NULL DEFAULT '{}',
  arguments_hash TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL DEFAULT '',
  decision_json TEXT NOT NULL DEFAULT '{}',
  risk TEXT NOT NULL DEFAULT 'low',
  approval_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  output_hash TEXT,
  resource_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_decision ON tool_calls(decision, started_at DESC);

CREATE TABLE IF NOT EXISTS llm_usage_events (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  task_kind TEXT NOT NULL DEFAULT 'direct',
  skill_name TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL DEFAULT '',
  model_name TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'admin',
  purpose TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'success',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_day ON llm_usage_events(created_at, user_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_task ON llm_usage_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_usage_events(model_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trace_runs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  user_id INTEGER,
  attempt_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  event_count INTEGER NOT NULL DEFAULT 0,
  model_calls INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  error_code TEXT NOT NULL DEFAULT '',
  error_summary TEXT NOT NULL DEFAULT '',
  trace_path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  meta_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trace_runs_ref ON trace_runs(kind, ref_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_runs_status ON trace_runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS tool_approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_version TEXT NOT NULL DEFAULT '',
  arguments_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_by TEXT NOT NULL DEFAULT '',
  approved_by TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_approvals_task ON tool_approvals(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_approvals_status ON tool_approvals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS model_capabilities (
  endpoint_key TEXT PRIMARY KEY,
  provider TEXT NOT NULL DEFAULT '',
  base_url TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  checked_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'probe'
);
CREATE TABLE IF NOT EXISTS skill_approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill TEXT NOT NULL,
  version TEXT NOT NULL,
  approver_id INTEGER NOT NULL,
  approver_name TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(skill, version, approver_id)
);
CREATE INDEX IF NOT EXISTS idx_skill_approvals ON skill_approvals(skill, version, decision);

"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.tx() as conn:
            conn.executescript(SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(workshop_drafts)")}
            if "source_task_id" not in cols:
                conn.execute("ALTER TABLE workshop_drafts ADD COLUMN source_task_id TEXT")
            if "source_text" not in cols:
                conn.execute("ALTER TABLE workshop_drafts ADD COLUMN source_text TEXT NOT NULL DEFAULT ''")
            if "recipe_id" not in cols:
                conn.execute("ALTER TABLE workshop_drafts ADD COLUMN recipe_id TEXT NOT NULL DEFAULT ''")
            if "name_auto" not in cols:
                conn.execute("ALTER TABLE workshop_drafts ADD COLUMN name_auto INTEGER NOT NULL DEFAULT 0")
            if "name_locked" not in cols:
                conn.execute("ALTER TABLE workshop_drafts ADD COLUMN name_locked INTEGER NOT NULL DEFAULT 0")
            # 经验必须先经专家复核才参与规划，防止未确认结论自我强化。
            exp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plan_experiences)")}
            if "trust_level" not in exp_cols:
                conn.execute("ALTER TABLE plan_experiences ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'unreviewed'")
            for col, ddl in (
                ("invalidated", "ALTER TABLE plan_experiences ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0"),
                ("device_type", "ALTER TABLE plan_experiences ADD COLUMN device_type TEXT NOT NULL DEFAULT ''"),
                ("software_version", "ALTER TABLE plan_experiences ADD COLUMN software_version TEXT NOT NULL DEFAULT ''"),
                ("source_revision", "ALTER TABLE plan_experiences ADD COLUMN source_revision TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in exp_cols:
                    conn.execute(ddl)
            if "review_id" not in exp_cols:
                conn.execute("ALTER TABLE plan_experiences ADD COLUMN review_id TEXT")
            if "corrected_root_name" not in exp_cols:
                conn.execute("ALTER TABLE plan_experiences ADD COLUMN corrected_root_name TEXT")
            if "reviewed_at" not in exp_cols:
                conn.execute("ALTER TABLE plan_experiences ADD COLUMN reviewed_at TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiences_trust ON plan_experiences(trust_level, id DESC)")
            conn.execute(
                "UPDATE plan_experiences SET trust_level='verified', reviewed_at=COALESCE(reviewed_at, created_at) "
                "WHERE task_id LIKE 'CURATED-%' AND trust_level='unreviewed'")
            review_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_reviews)")}
            for name, ddl in (
                ("original_skill", "ALTER TABLE task_reviews ADD COLUMN original_skill TEXT NOT NULL DEFAULT ''"),
                ("original_skill_version", "ALTER TABLE task_reviews ADD COLUMN original_skill_version TEXT NOT NULL DEFAULT ''"),
                ("promoted_at", "ALTER TABLE task_reviews ADD COLUMN promoted_at TEXT"),
            ):
                if name not in review_cols:
                    conn.execute(ddl)
            skill_cols = {r["name"] for r in conn.execute("PRAGMA table_info(skills)")}
            if "channel" not in skill_cols:
                conn.execute("ALTER TABLE skills ADD COLUMN channel TEXT NOT NULL DEFAULT 'stable'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_channel ON skills(channel, status)")
            draft_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_drafts)")}
            if "skill_name" not in draft_cols:
                conn.execute("ALTER TABLE task_drafts ADD COLUMN skill_name TEXT NOT NULL DEFAULT ''")
            task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "resume_count" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0")
            if "inputs_state" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN inputs_state TEXT NOT NULL DEFAULT 'available'")
            if "title_source" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN title_source TEXT NOT NULL DEFAULT ''")
            if "continuation_count" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN continuation_count INTEGER NOT NULL DEFAULT 0")
            if "session_budget_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN session_budget_json TEXT NOT NULL DEFAULT '{}'")
            for name, ddl in (
                ("archive_path", "ALTER TABLE tasks ADD COLUMN archive_path TEXT NOT NULL DEFAULT ''"),
                ("archive_sha256", "ALTER TABLE tasks ADD COLUMN archive_sha256 TEXT NOT NULL DEFAULT ''"),
                ("archived_at", "ALTER TABLE tasks ADD COLUMN archived_at TEXT"),
                ("retention_state", "ALTER TABLE tasks ADD COLUMN retention_state TEXT NOT NULL DEFAULT 'available'"),
                ("retained_reason", "ALTER TABLE tasks ADD COLUMN retained_reason TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in task_cols:
                    conn.execute(ddl)
            task_skill_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_skills)")}
            if "invalidated" not in task_skill_cols:
                conn.execute("ALTER TABLE task_skills ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0")
            supplement_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_supplements)")}
            if "invalidated_skills_json" not in supplement_cols:
                conn.execute("ALTER TABLE task_supplements ADD COLUMN invalidated_skills_json TEXT NOT NULL DEFAULT '[]'")
            for name, ddl in (
                ("reviewer_id", "ALTER TABLE tasks ADD COLUMN reviewer_id INTEGER"),
                ("reviewer_assigned_at", "ALTER TABLE tasks ADD COLUMN reviewer_assigned_at TEXT"),
            ):
                if name not in task_cols:
                    conn.execute(ddl)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_reviewer ON tasks(reviewer_id, created_at DESC)")
            user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            for name, ddl in (
                ("status", "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
                ("expires_at", "ALTER TABLE users ADD COLUMN expires_at TEXT"),
                ("invited_by", "ALTER TABLE users ADD COLUMN invited_by INTEGER"),
                ("invited_at", "ALTER TABLE users ADD COLUMN invited_at TEXT"),
                ("domain", "ALTER TABLE users ADD COLUMN domain TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "ALTER TABLE users ADD COLUMN updated_at TEXT"),
                ("notification_prefs", "ALTER TABLE users ADD COLUMN notification_prefs TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in user_cols:
                    conn.execute(ddl)
            # 1.12.0 彻底移除用户与邀请中的班组/站点；旧库直接删列，保留领域。
            for table in ("users", "user_invites"):
                table_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for name in ("team", "site"):
                    if name in table_cols:
                        conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{name}"')
            conn.execute("UPDATE users SET status='disabled' WHERE active=0 AND status='active'")
            conn.execute("UPDATE users SET updated_at=COALESCE(updated_at, created_at)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invites_code ON user_invites(code_hash)")
            case_cols = {r["name"] for r in conn.execute("PRAGMA table_info(real_cases)")}
            if "origin" not in case_cols:
                conn.execute("ALTER TABLE real_cases ADD COLUMN origin TEXT NOT NULL DEFAULT 'real'")
            for name, ddl in (
                ("reviewer_id", "ALTER TABLE real_cases ADD COLUMN reviewer_id INTEGER"),
                ("review_status", "ALTER TABLE real_cases ADD COLUMN review_status TEXT NOT NULL DEFAULT 'unassigned'"),
                ("assigned_at", "ALTER TABLE real_cases ADD COLUMN assigned_at TEXT"),
                ("inputs_state", "ALTER TABLE real_cases ADD COLUMN inputs_state TEXT NOT NULL DEFAULT 'available'"),
            ):
                if name not in case_cols:
                    conn.execute(ddl)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_real_cases_reviewer ON real_cases(reviewer_id, review_status)")
            case_table_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cases)")}
            if "inputs_state" not in case_table_cols:
                conn.execute("ALTER TABLE cases ADD COLUMN inputs_state TEXT NOT NULL DEFAULT 'available'")
            # 同一任务只自动沉淀一次；旧库升级后补建索引
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_source_task "
                "ON workshop_drafts(source_task_id) WHERE source_task_id IS NOT NULL")
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            conn.execute("UPDATE settings SET value=? WHERE key='schema_version'",
                         (str(SCHEMA_VERSION),))

    # ---------- 便捷查询 ----------
    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def audit(self, user_id: int | None, username: str | None, action: str,
              target: str = "", detail: dict | None = None, ip: str = "") -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO audit_logs(user_id, username, action, target, detail_json, ip, ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (user_id, username, action, target,
                 __import__("json").dumps(detail or {}, ensure_ascii=False),
                 ip, utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
