# BBU 故障诊断平台：接口与 UI 测试清单（基线 1.24.8）

> 用途：为“全接口、全参数、全按钮”测试门禁提供当前基线清单。
> 生成方式：从 `bbu/api.py` 路由注册表、`bbu/assistant_tools.py` 工具注册表、`web/assets/js` 的 `data-act` / `form` / `modal` 静态扫描生成。
> 注意：静态清单只证明“对象被识别”，不证明行为已验证；参数与状态用例由 `tests/manifests` 逐项绑定，缺一项即 CI 失败。

## 1. REST 路由总览

- 路由总数：**162**
- `tests/api_full_smoke.py` 的 `EXPECTED_ROUTES`：**153**
- 冒烟清单未覆盖：**9**
  - `POST /api/workshop/drafts/{draft_id}/rules/try`（`h_workshop_rules_try`）
  - `GET /api/system/migrations`（`h_migrations`）
  - `POST /api/system/migrations/workspace/preflight`（`h_migration_preflight`）
  - `POST /api/system/migrations/workspace`（`h_migration_workspace`）
  - `POST /api/system/migrations/export`（`h_migration_export`）
  - `POST /api/system/migrations/import`（`h_migration_import`）
  - `GET /api/system/migrations/{migration_id}`（`h_migration_detail`）
  - `GET /api/system/migrations/{migration_id}/download`（`h_migration_download`）
  - `DELETE /api/system/migrations/{migration_id}`（`h_migration_delete`）

## 2. 按模块分组

| 模块 | 路由数 |
|---|---:|
| `/api` | 27 |
| `/api/tasks` | 20 |
| `/api/workshop` | 20 |
| `/api/system` | 18 |
| `/api/skills` | 11 |
| `/api/auth` | 10 |
| `/api/users` | 7 |
| `/api/models` | 6 |
| `/api/assistant` | 5 |
| `/api/task-drafts` | 5 |
| `/api/cases` | 4 |
| `/api/profile` | 4 |
| `/api/mcp` | 3 |
| `/api/traces` | 3 |
| `/mcp` | 2 |
| `/api/notifications` | 2 |
| `/api/recovery-runs` | 2 |
| `/api/storage` | 2 |
| `/api/usage` | 2 |
| `/api/audit` | 1 |
| `/api/backups` | 1 |
| `/api/calibration` | 1 |
| `/api/case-matrix` | 1 |
| `/api/evaluations` | 1 |
| `/api/experience-candidates` | 1 |
| `/api/metrics` | 1 |
| `/api/public` | 1 |
| `/api/tool-approvals` | 1 |

## 3. 全量 REST 路由

### `/api`（27）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/audit` | `h_audit` | 是 |
| `GET` | `/api/backups` | `h_backups` | 是 |
| `GET` | `/api/calibration` | `h_calibration` | 是 |
| `GET` | `/api/case-matrix` | `h_case_matrix` | 是 |
| `GET` | `/api/cases` | `h_cases` | 是 |
| `GET` | `/api/evaluations` | `h_evaluations` | 是 |
| `GET` | `/api/experience-candidates` | `h_experience_candidates` | 是 |
| `GET` | `/api/health` | `h_health` | 是 |
| `GET` | `/api/metrics` | `h_metrics` | 是 |
| `GET` | `/api/models` | `h_models` | 是 |
| `POST` | `/api/models` | `h_model_create` | 是 |
| `GET` | `/api/notifications` | `h_notifications` | 是 |
| `GET` | `/api/profile` | `h_profile` | 是 |
| `PUT` | `/api/profile` | `h_profile_update` | 是 |
| `GET` | `/api/quality` | `h_quality` | 是 |
| `GET` | `/api/search` | `h_global_search` | 是 |
| `GET` | `/api/skill-versions` | `h_versions` | 是 |
| `GET` | `/api/skills` | `h_skills` | 是 |
| `GET` | `/api/stats` | `h_stats` | 是 |
| `POST` | `/api/task-drafts` | `h_task_draft_create` | 是 |
| `GET` | `/api/tasks` | `h_tasks` | 是 |
| `POST` | `/api/tasks` | `h_task_create` | 是 |
| `GET` | `/api/tool-approvals` | `h_tool_approvals` | 是 |
| `GET` | `/api/tool-calls` | `h_tool_calls` | 是 |
| `GET` | `/api/traces` | `h_traces` | 是 |
| `GET` | `/api/users` | `h_users` | 是 |
| `POST` | `/api/users` | `h_user_create` | 是 |

### `/api/tasks`（20）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/tasks/{task_id}` | `h_task_detail` | 是 |
| `GET` | `/api/tasks/{task_id}/agent-trace` | `h_agent_trace` | 是 |
| `POST` | `/api/tasks/{task_id}/assign-reviewer` | `h_task_assign_reviewer` | 是 |
| `POST` | `/api/tasks/{task_id}/cancel` | `h_task_cancel` | 是 |
| `POST` | `/api/tasks/{task_id}/continue` | `h_task_continue` | 是 |
| `GET` | `/api/tasks/{task_id}/events` | `h_task_events` | 是 |
| `GET` | `/api/tasks/{task_id}/evidence` | `h_task_evidence` | 是 |
| `POST` | `/api/tasks/{task_id}/inputs` | `h_task_inputs` | 是 |
| `GET` | `/api/tasks/{task_id}/questions` | `h_task_questions` | 是 |
| `GET` | `/api/tasks/{task_id}/recovery-runs` | `h_recovery_runs` | 是 |
| `POST` | `/api/tasks/{task_id}/recovery-runs` | `h_recovery_create` | 是 |
| `POST` | `/api/tasks/{task_id}/release` | `h_task_release` | 是 |
| `GET` | `/api/tasks/{task_id}/report` | `h_task_report` | 是 |
| `POST` | `/api/tasks/{task_id}/retry` | `h_task_retry` | 是 |
| `POST` | `/api/tasks/{task_id}/review` | `h_task_review` | 是 |
| `GET` | `/api/tasks/{task_id}/session` | `h_task_session` | 是 |
| `GET` | `/api/tasks/{task_id}/skills/{skill}/report` | `h_skill_report` | 是 |
| `GET` | `/api/tasks/{task_id}/storage-jobs` | `h_task_storage_jobs` | 是 |
| `GET` | `/api/tasks/{task_id}/stream` | `h_task_stream` | 是 |
| `POST` | `/api/tasks/{task_id}/tool-approvals` | `h_tool_approval_create` | 是 |

### `/api/workshop`（20）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/workshop/drafts` | `h_workshop_list` | 是 |
| `POST` | `/api/workshop/drafts` | `h_workshop_create` | 是 |
| `DELETE` | `/api/workshop/drafts/{draft_id}` | `h_workshop_delete` | 是 |
| `GET` | `/api/workshop/drafts/{draft_id}` | `h_workshop_detail` | 是 |
| `PUT` | `/api/workshop/drafts/{draft_id}` | `h_workshop_update` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/analyze` | `h_workshop_analyze` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/build` | `h_workshop_build` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/case` | `h_workshop_case` | 是 |
| `GET` | `/api/workshop/drafts/{draft_id}/cases` | `h_workshop_cases` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/check` | `h_workshop_check` | 是 |
| `GET` | `/api/workshop/drafts/{draft_id}/jobs` | `h_workshop_jobs` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/logs` | `h_workshop_logs` | 是 |
| `GET` | `/api/workshop/drafts/{draft_id}/provenance` | `h_workshop_provenance` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/provenance/lock` | `h_workshop_provenance_lock` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/publish` | `h_workshop_publish` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/refine` | `h_workshop_refine` | 是 |
| `POST` | `/api/workshop/drafts/{draft_id}/rules/try` | `h_workshop_rules_try` | **否** |
| `POST` | `/api/workshop/drafts/{draft_id}/verify-rules` | `h_workshop_verify_rules` | 是 |
| `GET` | `/api/workshop/jobs/{job_id}` | `h_workshop_job` | 是 |
| `GET` | `/api/workshop/recipes` | `h_workshop_recipes` | 是 |

### `/api/system`（18）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/system/info` | `h_system_info` | 是 |
| `GET` | `/api/system/migrations` | `h_migrations` | **否** |
| `POST` | `/api/system/migrations/export` | `h_migration_export` | **否** |
| `POST` | `/api/system/migrations/import` | `h_migration_import` | **否** |
| `POST` | `/api/system/migrations/workspace` | `h_migration_workspace` | **否** |
| `POST` | `/api/system/migrations/workspace/preflight` | `h_migration_preflight` | **否** |
| `DELETE` | `/api/system/migrations/{migration_id}` | `h_migration_delete` | **否** |
| `GET` | `/api/system/migrations/{migration_id}` | `h_migration_detail` | **否** |
| `GET` | `/api/system/migrations/{migration_id}/download` | `h_migration_download` | **否** |
| `POST` | `/api/system/restart` | `h_system_restart` | 是 |
| `GET` | `/api/system/runtime-config` | `h_runtime_config` | 是 |
| `PUT` | `/api/system/runtime-config` | `h_runtime_config_save` | 是 |
| `GET` | `/api/system/updates` | `h_update_list` | 是 |
| `POST` | `/api/system/updates/rollback` | `h_update_rollback` | 是 |
| `POST` | `/api/system/updates/upload` | `h_update_upload` | 是 |
| `GET` | `/api/system/updates/{update_id}` | `h_update_detail` | 是 |
| `POST` | `/api/system/updates/{update_id}/apply` | `h_update_apply` | 是 |
| `POST` | `/api/system/updates/{update_id}/verify` | `h_update_verify` | 是 |

### `/api/skills`（11）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/skills/import` | `h_skill_import` | 是 |
| `POST` | `/api/skills/scan` | `h_skill_scan` | 是 |
| `GET` | `/api/skills/{name}` | `h_skill_detail` | 是 |
| `POST` | `/api/skills/{name}/channel` | `h_skill_channel` | 是 |
| `POST` | `/api/skills/{name}/disable` | `h_skill_disable` | 是 |
| `POST` | `/api/skills/{name}/enable` | `h_skill_enable` | 是 |
| `GET` | `/api/skills/{name}/export` | `h_skill_export` | 是 |
| `GET` | `/api/skills/{name}/manifest` | `h_skill_manifest` | 是 |
| `POST` | `/api/skills/{name}/manifest/sign` | `h_skill_manifest_sign` | 是 |
| `POST` | `/api/skills/{name}/rollback` | `h_skill_rollback` | 是 |
| `GET` | `/api/skills/{name}/tool` | `h_skill_tool` | 是 |

### `/api/auth`（10）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/auth/activate` | `h_activate` | 是 |
| `POST` | `/api/auth/login` | `h_login` | 是 |
| `POST` | `/api/auth/logout` | `h_logout` | 是 |
| `GET` | `/api/auth/me` | `h_me` | 是 |
| `POST` | `/api/auth/password` | `h_change_password` | 是 |
| `POST` | `/api/auth/refresh` | `h_session_refresh` | 是 |
| `POST` | `/api/auth/register` | `h_register` | 是 |
| `GET` | `/api/auth/sessions` | `h_sessions` | 是 |
| `POST` | `/api/auth/sessions/revoke-others` | `h_revoke_other_sessions` | 是 |
| `DELETE` | `/api/auth/sessions/{session_id}` | `h_revoke_session` | 是 |

### `/api/users`（7）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/users/batch-disable` | `h_users_batch_disable` | 是 |
| `GET` | `/api/users/invites` | `h_invites` | 是 |
| `POST` | `/api/users/invites` | `h_invite_create` | 是 |
| `DELETE` | `/api/users/invites/{invite_id}` | `h_invite_revoke` | 是 |
| `GET` | `/api/users/{user_id}` | `h_user_detail` | 是 |
| `PUT` | `/api/users/{user_id}` | `h_user_update` | 是 |
| `POST` | `/api/users/{user_id}/force-logout` | `h_user_force_logout` | 是 |

### `/api/models`（6）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/models/discover` | `h_model_discover` | 是 |
| `POST` | `/api/models/test` | `h_model_test_draft` | 是 |
| `DELETE` | `/api/models/{model_id}` | `h_model_delete` | 是 |
| `PUT` | `/api/models/{model_id}` | `h_model_update` | 是 |
| `POST` | `/api/models/{model_id}/capabilities` | `h_model_capabilities` | 是 |
| `POST` | `/api/models/{model_id}/test` | `h_model_test` | 是 |

### `/api/assistant`（5）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/assistant/actions/{action_id}/cancel` | `h_assistant_cancel` | 是 |
| `POST` | `/api/assistant/actions/{action_id}/confirm` | `h_assistant_confirm` | 是 |
| `POST` | `/api/assistant/chat` | `h_assistant_chat` | 是 |
| `GET` | `/api/assistant/status` | `h_assistant_status` | 是 |
| `GET` | `/api/assistant/suggestions` | `h_assistant_suggestions` | 是 |

### `/api/task-drafts`（5）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/task-drafts/current` | `h_task_draft_current` | 是 |
| `DELETE` | `/api/task-drafts/{draft_id}` | `h_task_draft_delete` | 是 |
| `PUT` | `/api/task-drafts/{draft_id}` | `h_task_draft_update` | 是 |
| `POST` | `/api/task-drafts/{draft_id}/files` | `h_task_draft_file` | 是 |
| `DELETE` | `/api/task-drafts/{draft_id}/files/{file_id}` | `h_task_draft_file_delete` | 是 |

### `/api/cases`（4）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/cases/{case_id}` | `h_case_detail` | 是 |
| `POST` | `/api/cases/{case_id}/assign-reviewer` | `h_case_assign` | 是 |
| `GET` | `/api/cases/{case_id}/history` | `h_case_history` | 是 |
| `POST` | `/api/cases/{case_id}/label` | `h_case_label` | 是 |

### `/api/profile`（4）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `DELETE` | `/api/profile/model-key` | `h_profile_model_key_delete` | 是 |
| `GET` | `/api/profile/model-key` | `h_profile_model_key` | 是 |
| `PUT` | `/api/profile/model-key` | `h_profile_model_key_update` | 是 |
| `POST` | `/api/profile/model-key/test` | `h_profile_model_key_test` | 是 |

### `/api/mcp`（3）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/mcp/info` | `h_mcp_info` | 是 |
| `DELETE` | `/api/mcp/token` | `h_mcp_token_delete` | 是 |
| `POST` | `/api/mcp/token` | `h_mcp_token_create` | 是 |

### `/api/traces`（3）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/traces/{run_id}/download` | `h_trace_download` | 是 |
| `GET` | `/api/traces/{run_id}/events` | `h_trace_events` | 是 |
| `GET` | `/api/traces/{run_id}/summary` | `h_trace_summary` | 是 |

### `/mcp`（2）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/mcp` | `h_mcp_http` | 是 |
| `POST` | `/mcp` | `h_mcp_http` | 是 |

### `/api/notifications`（2）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/notifications/read-all` | `h_notifications_read_all` | 是 |
| `POST` | `/api/notifications/{note_id}/read` | `h_notification_read` | 是 |

### `/api/recovery-runs`（2）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/recovery-runs/{run_id}/observe` | `h_recovery_observe` | 是 |
| `POST` | `/api/recovery-runs/{run_id}/review` | `h_recovery_review` | 是 |

### `/api/storage`（2）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/storage/jobs/{job_id}` | `h_storage_job` | 是 |
| `GET` | `/api/storage/overview` | `h_storage_overview` | 是 |

### `/api/usage`（2）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/usage/overview` | `h_usage_overview` | 是 |
| `GET` | `/api/usage/tasks` | `h_usage_tasks` | 是 |

### `/api/audit`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/audit/export` | `h_audit_export` | 是 |

### `/api/backups`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `DELETE` | `/api/backups/{name}` | `h_backup_delete` | 是 |

### `/api/calibration`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/calibration/fit` | `h_calibration_fit` | 是 |

### `/api/case-matrix`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/case-matrix/rebuild` | `h_case_matrix_rebuild` | 是 |

### `/api/evaluations`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/evaluations/run` | `h_evaluation_run` | 是 |

### `/api/experience-candidates`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/experience-candidates/{candidate_id}/decide` | `h_candidate_decide` | 是 |

### `/api/metrics`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/metrics/prometheus` | `h_metrics_prometheus` | 是 |

### `/api/public`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `GET` | `/api/public/config` | `h_public_config` | 是 |

### `/api/tool-approvals`（1）

| 方法 | 路径 | Handler | 冒烟覆盖 |
|---|---|---|---|
| `POST` | `/api/tool-approvals/{approval_id}/decide` | `h_tool_approval_decide` | 是 |

## 4. 悬浮助手 / MCP 工具（21）

| 工具 | 标题 | 风险 | 作用域 | MCP 通道 |
|---|---|---|---|---|
| `search_docs` | 检索平台文档 | `read` | `user` | `read` |
| `search_messages` | 检索平台消息 | `read` | `user` | `read` |
| `get_task` | 查看任务 | `read` | `user` | `read` |
| `list_tasks` | 列出任务 | `read` | `user` | `read` |
| `create_skill_diagnosis` | 创建指定技能诊断 | `medium` | `user` | `write` |
| `search_skills` | 检索技能 | `read` | `user` | `read` |
| `get_system_status` | 查看系统状态 | `read` | `admin` | `read` |
| `list_backups` | 列出备份 | `read` | `admin` | `read` |
| `delete_backup` | 删除备份 | `medium` | `admin` | `write` |
| `storage_overview` | 查看任务空间 | `read` | `user` | `read` |
| `release_task_storage` | 释放任务空间 | `medium` | `user` | `write` |
| `get_update_status` | 查看升级状态 | `read` | `admin` | `read` |
| `apply_update` | 应用升级包 | `high` | `admin` | `never` |
| `rollback_update` | 回滚升级 | `high` | `admin` | `never` |
| `list_users` | 检索用户 | `read` | `admin` | `read` |
| `update_user` | 修改用户 | `high` | `admin` | `never` |
| `list_models` | 列出模型配置 | `read` | `admin` | `read` |
| `update_model` | 修改模型配置 | `medium` | `admin` | `never` |
| `get_notification_prefs` | 查看通知偏好 | `read` | `user` | `read` |
| `update_notification_prefs` | 修改通知偏好 | `medium` | `user` | `write` |
| `mark_notifications_read` | 标记通知已读 | `medium` | `user` | `write` |

## 5. MCP JSON-RPC 方法（14）

- `initialize`
- `ping`
- `logging/setLevel`
- `tools/list`
- `tools/call`
- `resources/list`
- `resources/read`
- `prompts/list`
- `prompts/get`
- `roots/list`
- `sampling/createMessage`
- `completion/complete`
- `notifications/initialized`
- `notifications/cancelled`

## 6. UI 动作（98 个动作名 / 252 个实例 / 211 个静态 `<button>`）

> `data-act` 实例包含按钮、链接和动态模板；静态 `<button>` 标签用于检查“没有 `data-act` 的按钮”是否被漏登记。
> 模块内唯一动作名合计 124，跨模块去重后全局 98；测试按“页面实例”执行，不按全局动作名去重。

| 模块 | 模块内动作名 | 实例数 | 模块内全部动作 |
|---|---:|---:|---|
| `app.js` | 15 | 38 | `cancel`, `close`, `login`, `logout`, `model-key`, `notifications`, `password`, `profile`, `read-all`, `relogin`, `renew`, `revoke-others`, `revoke-session`, `save`, `search` |
| `core.js` | 3 | 6 | `cancel`, `close-modal`, `ok` |
| `page_admin.js` | 34 | 84 | `apply`, `apply-update`, `batch-disable`, `cancel`, `close`, `close-modal`, `copy`, `create`, `delete`, `delete-migration`, `detail-user`, `disable`, `discover`, `done`, `edit`, `edit-user`, `export`, `force`, `new-model`, `new-user`, `next`, `prev`, `refresh`, `reload-after-update`, `reset`, `rollback`, `rollback-update`, `save`, `search`, `submit`, `test`, `toggle`, `upload-update`, `verify-update` |
| `page_cases.js` | 9 | 14 | `approve`, `assign`, `close`, `filter`, `reject`, `run`, `run-eval`, `save-label`, `submit` |
| `page_dashboard.js` | 3 | 4 | `all-tasks`, `new-task`, `skills` |
| `page_skills.js` | 10 | 18 | `cancel`, `channel`, `close`, `detail`, `diagnose`, `diagnose-skill`, `do-import`, `import`, `scan`, `toggle` |
| `page_task_detail.js` | 14 | 24 | `assign-reviewer`, `back`, `cancel`, `cancel-modal`, `cancel-review`, `continue-task`, `recovery-create`, `recovery-observe`, `recovery-review`, `retry`, `review`, `save`, `submit-review`, `supplement` |
| `page_tasks.js` | 3 | 3 | `discard-draft`, `new-task`, `reload` |
| `page_traces.js` | 3 | 5 | `apply`, `refresh`, `reset` |
| `page_usage.js` | 5 | 7 | `metric`, `next`, `prev`, `refresh`, `user-metric` |
| `page_workshop.js` | 20 | 36 | `build`, `cancel`, `case-folder`, `check`, `create`, `delete-draft`, `lock-provenance`, `model-key`, `new-draft`, `publish`, `reanalyze`, `refine`, `retry-job`, `rules-try`, `save`, `save-source`, `upload-logs`, `verify-rules`, `view-job-trace`, `workshop-step` |
| `personal_model.js` | 5 | 13 | `close`, `delete-key`, `model-key`, `save-key`, `test-key` |

## 7. 表单与弹窗

- 表单：**18** 个
  - `activate-form`
  - `assign-form`
  - `assistant-form`
  - `case-form`
  - `case-label-form`
  - `create-form`
  - `invite-form`
  - `login-form`
  - `model-form`
  - `pwd-form`
  - `reauth-form`
  - `recovery-form`
  - `register-form`
  - `reset-form`
  - `review-form`
  - `setup-form`
  - `task-form`
  - `user-form`

- 弹窗调用：**28** 处
  - `app.js`：6
  - `assistant.js`：1
  - `core.js`：2
  - `page_admin.js`：10
  - `page_cases.js`：2
  - `page_skills.js`：1
  - `page_task_detail.js`：3
  - `page_workshop.js`：2
  - `personal_model.js`：1

## 8. 元技能 / 运维 CLI

- 元技能脚本：**21** 个
  - `build_child_skill.py`
  - `check_env.py`
  - `fm_alarm_align.py`
  - `gen_specialized.py`
  - `import_rule_library.py`
  - `init_case.py`
  - `log_format.py`
  - `package_skill.py`
  - `parse_clock.py`
  - `parse_cloudmat.py`
  - `publish.py`
  - `render_report.py`
  - `rule_library_diff.py`
  - `run_analysis.py`
  - `run_regression.py`
  - `scaffold_child_skill.py`
  - `scan_rule_candidates.py`
  - `search_evidence.py`
  - `skill_sync.py`
  - `unpack_logs.py`
  - `validate_skill_md.py`
- 平台 tools：**7** 个
  - `bbu_ops.py`
  - `eval_kb_retrieval.py`
  - `generate_diagnostic_dataset.py`
  - `real_cases.py`
  - `reproduce_task.py`
  - `seed_reasoning_experiences.py`
  - `support_bundle.py`
- 部署脚本：**10** 个
  - `backup.sh`
  - `bbu-upgrade-helper.sh`
  - `fix_upgrade_helper.sh`
  - `install_offline.sh`
  - `install_upgrade_helper.sh`
  - `make_oneclick.sh`
  - `make_release.sh`
  - `make_windows_zip.sh`
  - `rollback_offline.sh`
  - `upgrade_offline.sh`

## 9. 覆盖判定

- REST：162/162 路由必须有成功契约用例、4 类身份权限用例、参数变体用例；缺一即失败。
- 工具：21/21 必须有 schema 合法/缺参/错型/越权/确认用例。
- MCP：14/14 必须有正常、错误、批量、通知、Token 边界用例。
- UI：98/98 动作名、252/252 实例、211/211 静态按钮、18/18 表单、28/28 弹窗必须有状态矩阵用例；纯展示按钮需在清单标注 `display-only`。
- CLI：21/21 元技能脚本、6/6 tools、9/9 部署脚本必须有 `--help` 与最小真实调用/干跑用例。
