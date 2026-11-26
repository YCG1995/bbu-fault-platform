"""真实评测任务卡 Schema 与标签哈希校验。

任务卡是评测数据的最小契约：没有输入哈希、来源、标签人和 split 的案例
不能进入 hidden/golden 评测。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA_VERSION = "bbu-task-card/1"
SPLITS = {"dev", "test", "hidden", "train"}
STATUS = {"golden", "labeled"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def label_hash(card: dict[str, Any]) -> str:
    payload = {key: card.get(key) for key in (
        "expected_roots", "excluded_roots", "required_evidence", "allowed_refusal",
        "forbidden_tools")}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def validate_task_card(card: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(card, dict):
        raise ValueError("任务卡必须是对象")
    if card.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version 必须是 {SCHEMA_VERSION}")
    for key in ("card_id", "title", "description", "device_type", "software_version",
                "labeler", "labeled_at"):
        if not str(card.get(key) or "").strip():
            raise ValueError(f"缺少必填字段：{key}")
    split = str(card.get("split") or "")
    if split not in SPLITS:
        raise ValueError(f"非法 split：{split}")
    status = str(card.get("status") or "")
    if status not in STATUS:
        raise ValueError(f"非法 status：{status}")
    if status == "golden" and split not in SPLITS:
        raise ValueError("golden 任务卡必须进入 dev/test/hidden/train")
    expected = card.get("expected_roots") or []
    excluded = card.get("excluded_roots") or []
    if not isinstance(expected, list) or not isinstance(excluded, list):
        raise ValueError("expected_roots/excluded_roots 必须是数组")
    if not expected and not excluded:
        raise ValueError("必须至少给出一个期望根因或排除根因")
    inputs = card.get("input_hashes") or []
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("input_hashes 至少需要一个输入")
    for item in inputs:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError("input_hashes 每项必须包含 name")
        if not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256") or "")):
            raise ValueError("input_hashes 的 sha256 必须是 64 位小写十六进制")
        if int(item.get("size") or 0) < 0:
            raise ValueError("input_hashes 的 size 不能为负")
    evidence = card.get("required_evidence") or []
    if not isinstance(evidence, list):
        raise ValueError("required_evidence 必须是数组")
    if expected and status == "golden" and not evidence:
        raise ValueError("golden 正例必须声明至少一条 required_evidence")
    for item in evidence:
        if not isinstance(item, dict) or not str(item.get("kind") or "").strip() \
                or not str(item.get("description") or "").strip():
            raise ValueError("required_evidence 每项必须包含 kind/description")
    if not isinstance(card.get("allowed_refusal"), bool):
        raise ValueError("allowed_refusal 必须是布尔值")
    if not isinstance(card.get("forbidden_tools") or [], list):
        raise ValueError("forbidden_tools 必须是数组")
    if split == "hidden":
        if not card.get("hidden_isolated"):
            raise ValueError("hidden 任务卡必须声明 hidden_isolated=true")
        if card.get("origin") in ("synthetic", "generated"):
            raise ValueError("hidden 任务卡不得使用合成/生成案例")
    return {
        "ok": True, "card_id": card["card_id"], "schema_version": SCHEMA_VERSION,
        "split": split, "status": status,
        "input_count": len(inputs), "required_evidence_count": len(evidence),
        "label_hash": label_hash(card),
        "public": {
            "card_id": card["card_id"], "title": card["title"], "split": split,
            "status": status, "device_type": card["device_type"],
            "software_version": card["software_version"], "site": card.get("site") or "",
            "expected_roots": expected, "excluded_roots": excluded,
            "label_hash": label_hash(card), "disputed": bool(card.get("disputed")),
        },
    }
