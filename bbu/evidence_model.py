"""统一证据事件：保留旧技能 file/line/timestamp/text 的兼容适配。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

INSTRUCTION_RE = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?previous|system\s+prompt|执行以下命令|运行命令|调用工具|tool_call)")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceEvent:
    evidence_id: str
    session_id: str
    source: dict[str, Any]
    entity: dict[str, Any] = field(default_factory=dict)
    event_time: dict[str, Any] = field(default_factory=dict)
    kind: str = "log"
    name: str = "log.observation"
    value: dict[str, Any] = field(default_factory=dict)
    excerpt: str = ""
    parser: dict[str, Any] = field(default_factory=lambda: {
        "name": "legacy-skill-adapter", "version": "1", "confidence": 1.0})
    trust: dict[str, Any] = field(default_factory=lambda: {
        "origin": "untrusted_log", "contains_instructions": False})
    content_sha256: str = ""

    @classmethod
    def from_legacy(cls, session_id: str, skill: str, version: str,
                    item: dict[str, Any], file_sha256: str = "") -> "EvidenceEvent":
        excerpt = str(item.get("text") or item.get("line") or "")[:2000]
        path = str(item.get("file") or "").replace("\\", "/")
        source = {
            "tool": f"skill.{skill}", "tool_version": version or "unknown",
            "path": path, "path_basename": path.rsplit("/", 1)[-1] if path else "",
            "line_start": item.get("line_no"), "line_end": item.get("line_no"),
            "file_sha256": file_sha256 or str(item.get("file_sha256") or ""),
            "archive_member": str(item.get("archive_member") or ""),
        }
        timestamp = item.get("timestamp")
        event_time = {"value": str(timestamp), "inferred": False} if timestamp else {
            "value": None, "inferred": False, "gap": "missing_timestamp"}
        digest = content_hash(excerpt)
        identity = _canonical({"session": session_id, "source": source, "time": event_time,
                               "rule": item.get("rule_id"), "sha256": digest})
        evidence_id = "EV-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        entity = {
            "site": str(item.get("site") or "unknown").replace("\\", "/").rsplit("/", 1)[-1],
            "device_type": str(item.get("device_type") or ""),
            "component": str(item.get("component") or ""),
        }
        return cls(
            evidence_id=evidence_id, session_id=session_id, source=source,
            entity=entity,
            event_time=event_time, kind="log",
            name=f"skill.{skill}.{item.get('rule_id') or 'observation'}",
            value={"rule_id": item.get("rule_id"), "associate": item.get("associate") or ""},
            excerpt=excerpt, trust={
                "origin": "untrusted_log",
                "contains_instructions": bool(INSTRUCTION_RE.search(excerpt)),
            },
            content_sha256=digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_time(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            parsed = datetime.strptime(text.replace("Z", ""), fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def timeline(events: Iterable[EvidenceEvent]) -> list[EvidenceEvent]:
    """按可解析时间排序；缺时间事件稳定置后，不把先后相关推断为因果。"""
    def key(event: EvidenceEvent) -> tuple[int, str, str]:
        raw = event.event_time.get("value")
        parsed = _parse_time(raw)
        if parsed is None:
            return (1, str(raw or ""), event.evidence_id)
        return (0, parsed.astimezone(timezone.utc).isoformat(), event.evidence_id)
    return sorted(events, key=key)
