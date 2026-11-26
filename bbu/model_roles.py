"""模型角色常量：API 校验、助手规划与个人模型共用，避免多处定义漂移。"""
from __future__ import annotations

MODEL_ROLES = ("default", "planner", "router", "arbiter", "writer", "workshop", "assistant")
